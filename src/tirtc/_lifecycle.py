from __future__ import annotations

import threading

from ._errors import TiRTCError, _closed_error, _in_use_error


def _copy_error(error: BaseException) -> BaseException:
    if isinstance(error, TiRTCError):
        return type(error)(str(error), code=error.code, name=error.name)
    return error


class _CloseCoordinator:
    def __init__(self, lock: threading.RLock) -> None:
        self._condition = threading.Condition(lock)
        self._status = "open"
        self._generation = 0
        self._outcomes: dict[int, BaseException | None] = {}
        self._waiters: dict[int, int] = {}

    @property
    def status(self) -> str:
        return self._status

    def require_open(self) -> None:
        if self._status != "open":
            raise _closed_error()

    def begin(self, *, preflight_in_use: bool = False) -> int | None:
        with self._condition:
            if self._status == "closing":
                generation = self._generation
                self._waiters[generation] = self._waiters.get(generation, 0) + 1
                try:
                    while generation not in self._outcomes:
                        self._condition.wait()
                    error = self._outcomes[generation]
                finally:
                    remaining = self._waiters[generation] - 1
                    if remaining:
                        self._waiters[generation] = remaining
                    else:
                        del self._waiters[generation]
                        self._outcomes.pop(generation, None)
                if error is not None:
                    raise _copy_error(error)
                return None
            if self._status == "closed":
                return None
            if self._status == "open" and preflight_in_use:
                raise _in_use_error()
            self._generation += 1
            self._status = "closing"
            return self._generation

    def fail(self, generation: int, error: BaseException) -> None:
        with self._condition:
            if self._waiters.get(generation, 0):
                self._outcomes[generation] = error
            self._status = "close_failed"
            self._condition.notify_all()

    def succeed(self, generation: int, error: BaseException | None = None) -> None:
        with self._condition:
            if self._waiters.get(generation, 0):
                self._outcomes[generation] = error
            self._status = "closed"
            self._condition.notify_all()
