from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import queue
import sys
import threading
import types
import weakref
from collections.abc import Callable, Iterator
from typing import Any


_callback_state = threading.local()


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
        self._ready: queue.SimpleQueue[weakref.ReferenceType[_Dispatcher]] = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run, name="tirtc-callbacks", daemon=True
        )
        self._thread.start()

    def schedule(self, dispatcher: _Dispatcher) -> None:
        self._ready.put(weakref.ref(dispatcher))

    def _run(self) -> None:
        while True:
            reference = self._ready.get()
            dispatcher = reference()
            if dispatcher is not None:
                dispatcher._drain_one()


_EXECUTOR = _CallbackExecutor()


class _Dispatcher:
    __slots__ = (
        "__weakref__",
        "_capacity",
        "_closed",
        "_dropped_frame",
        "_events",
        "_executing",
        "_handler",
        "_lock",
        "_scheduled",
    )

    def __init__(self, handler: Callable[..., None], *, capacity: int = 64) -> None:
        self._capacity = capacity
        self._closed = False
        self._dropped_frame = False
        self._events: deque[tuple[str, tuple[Any, ...], bool, bool]] = deque()
        self._executing = False
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
            if len(self._events) >= self._capacity:
                if frame:
                    self._dropped_frame = True
                    return False
                for index, current in enumerate(self._events):
                    if not current[3]:
                        del self._events[index]
                        if current[2]:
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
            if not self._scheduled:
                self._scheduled = True
                schedule = True
        if schedule:
            _EXECUTOR.schedule(self)
        return True

    def _drain_one(self) -> None:
        with self._lock:
            if self._closed or not self._events:
                self._scheduled = False
                return
            kind, values, _, _ = self._events.popleft()
            self._executing = True
        handler = self._handler()
        if handler is not None:
            try:
                handler(kind, *values)
            except BaseException as error:
                _report_unraisable(error, handler)
        with self._lock:
            self._executing = False
            pending = bool(self._events) and not self._closed
            if not pending:
                self._scheduled = False
            self._lock.notify_all()
        if pending:
            _EXECUTOR.schedule(self)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._events.clear()
            self._scheduled = False
            if threading.current_thread() is _EXECUTOR._thread:
                return
            while self._executing:
                self._lock.wait()


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
            latest=kind in {"state", "time", "progress"},
        )

    return sink
