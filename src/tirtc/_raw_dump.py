from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import threading
from typing import Self, final
import warnings

from . import _native
from ._dispatch import _callback_owner
from ._errors import _in_use_error
from ._runtime import _check_process, _process_is_current
from ._util import _check_code, _retry_in_use
from ._values import (
    RawDumpArchive,
    _RAW_DUMP_STOP_REASONS,
    _value,
)


def normalize_raw_dump_ids(
    values: object, name: str, maximum: int
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be an iterable of integers")
    try:
        source = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of integers") from error
    result: list[int] = []
    seen: set[int] = set()
    for value in source:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} values must be integers")
        if value < 0 or value > maximum:
            raise ValueError(f"{name} values must be from 0 through {maximum}")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


@final
class RawDump:
    def __init__(self) -> None:
        raise TypeError("RawDump values are created by Connection or Replay")

    @classmethod
    def _create(cls, parent: object, handle: object) -> RawDump:
        self = object.__new__(cls)
        self._parent = parent
        self._handle = handle
        self._archive: RawDumpArchive | None = None
        self._closed = False
        self._lock = threading.RLock()
        return self

    def _reject_blocking_callback(self) -> None:
        callback = _callback_owner()
        if callback is not None and (
            callback is self._parent or getattr(callback, "_bound", None) is self._parent
        ):
            raise _in_use_error()

    def stop(self) -> RawDumpArchive:
        _check_process()
        self._reject_blocking_callback()
        with self._lock:
            if self._closed:
                from ._errors import _closed_error

                raise _closed_error()
            if self._archive is None:
                values = _native.raw_dump_stop(self._handle)
                _check_code(int(values[0]))
                self._archive = _value(
                    RawDumpArchive,
                    capture_id=str(values[1]),
                    path=Path(str(values[2])),
                    size=int(values[3]),
                    sha256=str(values[4]),
                    captured_duration=timedelta(milliseconds=int(values[5])),
                    capture_complete=bool(values[6]),
                    stop_reason=_RAW_DUMP_STOP_REASONS.get(
                        int(values[7]), _RAW_DUMP_STOP_REASONS[0]
                    ),
                    unsaved_packet_count=int(values[8]),
                    unsaved_byte_count=int(values[9]),
                    empty=bool(values[10]),
                )
            return self._archive

    def close(self) -> None:
        _check_process()
        self._reject_blocking_callback()
        with self._lock:
            if self._closed:
                return
            stop_error: BaseException | None = None
            try:
                self.stop()
            except BaseException as error:
                stop_error = error
            _retry_in_use(_native.close, self._handle)
            self._closed = True
            parent = self._parent
            self._parent = None
        if parent is not None:
            parent._finish_raw_dump(self)  # type: ignore[attr-defined]
        if stop_error is not None:
            raise stop_error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_closed", True) or getattr(self, "_handle", None) is None:
            return
        try:
            warnings.warn("unclosed TiRTC RawDump", ResourceWarning, stacklevel=2)
            if _process_is_current():
                self.close()
        except BaseException:
            pass
