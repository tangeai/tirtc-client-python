from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ._errors import _error_from_code
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
    milliseconds = int(moment.timestamp() * 1000)
    if milliseconds < 0:
        raise ValueError(f"{name} must be at or after the Unix epoch")
    return milliseconds


def _datetime_from_us(value: int, present: bool) -> datetime | None:
    if not present:
        return None
    return datetime.fromtimestamp(value / 1_000_000, timezone.utc)


def _datetime_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def _enum(value: object, enum_type: type, name: str) -> Any:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be {enum_type.__name__}")
    return value
