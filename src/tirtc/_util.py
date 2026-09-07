from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any

from ._errors import _error_from_code, _timeout_error
from ._runtime import _error_name


def _check_code(code: int) -> None:
    if code:
        raise _error_from_code(code, _error_name(code))


def _callable(value: object, name: str) -> None:
    if value is not None and not callable(value):
        raise TypeError(f"{name} must be callable or None")


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _stream_id(value: object, name: str = "stream_id") -> int:
    return _integer(value, name, minimum=0, maximum=15)


def _channel_id(value: object, name: str = "channel_id") -> int:
    return _integer(value, name, minimum=0, maximum=255)


def _buffer(value: object, name: str = "data") -> memoryview:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be bytes, bytearray, or memoryview")
    view = memoryview(value)
    if not view.contiguous:
        raise ValueError(f"{name} must be contiguous")
    return view.cast("B")


def _duration_us(value: object, name: str) -> int:
    if not isinstance(value, timedelta):
        raise TypeError(f"{name} must be datetime.timedelta")
    return (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _duration_ms(value: object, name: str) -> int:
    microseconds = _duration_us(value, name)
    if microseconds % 1000:
        raise ValueError(f"{name} must use whole milliseconds")
    return microseconds // 1000


def _aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime.datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include timezone information")
    return value.astimezone(timezone.utc)


def _datetime_ms(value: object, name: str) -> int:
    moment = _aware_datetime(value, name)
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    microseconds = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if microseconds % 1000:
        raise ValueError(f"{name} must use whole milliseconds")
    milliseconds = microseconds // 1000
    if milliseconds < 0:
        raise ValueError(f"{name} must be at or after the Unix epoch")
    return milliseconds


def _datetime_from_us(value: int, present: bool) -> datetime | None:
    if not present:
        return None
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value)


def _datetime_from_ms(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)


def _timeout(value: float | None, name: str = "timeout") -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be float or None")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return timeout


def _retry_in_use(
    operation,
    *arguments: object,
    _timeout_seconds: float = 5.0,
    _clock=time.monotonic,
    _sleep=time.sleep,
) -> None:
    result = _retry_in_use_result(
        operation,
        *arguments,
        _timeout_seconds=_timeout_seconds,
        _clock=_clock,
        _sleep=_sleep,
    )
    _check_code(int(result))


def _retry_in_use_result(
    operation,
    *arguments: object,
    _timeout_seconds: float = 5.0,
    _clock=time.monotonic,
    _sleep=time.sleep,
) -> Any:
    deadline = _clock() + _timeout_seconds
    delay = 0.001
    while True:
        result = operation(*arguments)
        code = int(result[0] if isinstance(result, tuple) else result)
        if code != 6026:
            return result
        remaining = deadline - _clock()
        if remaining <= 0:
            raise _timeout_error()
        _sleep(min(delay, remaining))
        delay = min(delay * 2, 0.02)


def _enum(value: object, enum_type: type, name: str) -> Any:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be {enum_type.__name__}")
    return value
