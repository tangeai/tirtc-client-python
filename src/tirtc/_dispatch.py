from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import os
import queue
import sys
import threading
import types
import weakref
from collections.abc import Callable, Iterator
from typing import Any


_callback_state = threading.local()
_executor_lock = threading.Lock()
_executor: _CallbackExecutor | None = None


@contextmanager
def _active_callback(owner: object) -> Iterator[None]:
    previous = getattr(_callback_state, "owner", None)
    _callback_state.owner = owner
    try:
        yield
    finally:
        _callback_state.owner = previous


def _callback_owner() -> object | None:
    return getattr(_callback_state, "owner", None)


def _report_unraisable(error: BaseException, callback: object) -> None:
    try:
        sys.unraisablehook(
            types.SimpleNamespace(
                exc_type=type(error),
                exc_value=error,
                exc_traceback=error.__traceback__,
                err_msg="Exception ignored in TiRTC callback",
                object=callback,
            )
        )
    except BaseException:
        pass


class _CallbackExecutor:
    def __init__(self) -> None:
        self._pid = os.getpid()
        self._ready: queue.SimpleQueue[weakref.ReferenceType[_Dispatcher]] = queue.SimpleQueue()
        self._threads = tuple(
            threading.Thread(
                target=self._run,
                name=f"tirtc-callback-{index}",
                daemon=True,
            )
            for index in range(2)
        )
        for thread in self._threads:
            thread.start()

    @property
    def current_process(self) -> bool:
        return self._pid == os.getpid()

    def schedule(self, dispatcher: _Dispatcher) -> None:
        self._ready.put(weakref.ref(dispatcher))

    def _run(self) -> None:
        while True:
            reference = self._ready.get()
            dispatcher = reference()
            if dispatcher is None:
                continue
            try:
                dispatcher._drain_one()
            except BaseException as error:
                _report_unraisable(error, dispatcher)


def _get_executor() -> _CallbackExecutor:
    global _executor
    with _executor_lock:
        if _executor is None or not _executor.current_process:
            _executor = _CallbackExecutor()
        return _executor


def _after_fork_child() -> None:
    global _callback_state, _executor, _executor_lock
    _callback_state = threading.local()
    _executor = None
    _executor_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


class _Dispatcher:
    __slots__ = (
        "__weakref__",
        "_capacity",
        "_closed",
        "_dropped_frame",
        "_events",
        "_executing",
        "_frame_capacity",
        "_frame_count",
        "_handler",
        "_lock",
        "_scheduled",
    )

    def __init__(
        self,
        handler: Callable[..., None],
        *,
        capacity: int = 64,
        frame_capacity: int | None = None,
    ) -> None:
        self._capacity = capacity
        self._frame_capacity = capacity if frame_capacity is None else frame_capacity
        self._closed = False
        self._dropped_frame = False
        self._events: deque[tuple[str, tuple[Any, ...], bool, bool]] = deque()
        self._executing = False
        self._frame_count = 0
        self._handler = weakref.WeakMethod(handler)
        self._lock = threading.Condition()
        self._scheduled = False

    @property
    def in_callback(self) -> bool:
        with self._lock:
            return self._executing

    def post(
        self,
        kind: str,
        *values: Any,
        frame: bool = False,
        terminal: bool = False,
        latest: bool = False,
    ) -> bool:
        schedule = False
        with self._lock:
            if self._closed:
                return False
            if latest:
                for index in range(len(self._events) - 1, -1, -1):
                    current = self._events[index]
                    if current[0] == kind and not current[2]:
                        self._events[index] = (kind, values, frame, terminal)
                        return True
            if frame and self._frame_count >= self._frame_capacity:
                self._dropped_frame = True
                return False
            if len(self._events) >= self._capacity:
                if frame:
                    self._dropped_frame = True
                    return False
                for index, current in enumerate(self._events):
                    if not current[3]:
                        del self._events[index]
                        if current[2]:
                            self._frame_count -= 1
                            self._dropped_frame = True
                        break
                else:
                    if not terminal or any(
                        current[0] == kind and current[3] for current in self._events
                    ):
                        return False
            if frame and self._dropped_frame:
                values = (*values[:-1], True)
                self._dropped_frame = False
            self._events.append((kind, values, frame, terminal))
            if frame:
                self._frame_count += 1
            if not self._scheduled:
                self._scheduled = True
                schedule = True
        if schedule:
            _get_executor().schedule(self)
        return True

    def _drain_one(self) -> None:
        with self._lock:
            if not self._events:
                self._scheduled = False
                return
            kind, values, frame, _ = self._events.popleft()
            if frame:
                self._frame_count -= 1
            self._executing = True
        handler = self._handler()
        if handler is not None:
            try:
                handler(kind, *values)
            except BaseException as error:
                _report_unraisable(error, handler)
        with self._lock:
            self._executing = False
            pending = bool(self._events)
            if not pending:
                self._scheduled = False
            self._lock.notify_all()
        if pending:
            _get_executor().schedule(self)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            while self._executing or self._events:
                self._lock.wait()

    def discard(self) -> None:
        with self._lock:
            self._closed = True
            self._events.clear()
            self._frame_count = 0
            self._scheduled = False
            self._lock.notify_all()


def _native_sink(dispatcher: _Dispatcher) -> Callable[..., None]:
    reference = weakref.ref(dispatcher)

    def sink(kind: str, *values: Any) -> None:
        target = reference()
        if target is None:
            return
        target.post(
            kind,
            *values,
            frame=kind.endswith("_frame"),
            terminal=kind in {"completed", "error"},
            latest=kind in {"state", "time"},
        )

    return sink
