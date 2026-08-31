from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import sys
import threading
from typing import Literal

from ._errors import (
    AlreadyInitializedError,
    UnsupportedError,
    _error_from_code,
    _io_error,
    _new_error,
)


if sys.platform == "darwin":
    version = platform.mac_ver()[0]
    if version:
        parts = tuple(int(part) for part in version.split(".")[:2])
        if parts < (11, 5):
            raise _new_error(
                UnsupportedError,
                code=6107,
                name="unsupported",
                message="tirtc requires macOS 11.5 or later",
            )

from . import _native  # noqa: E402


_Owner = Literal["rtc", "storage"]


@dataclass(frozen=True, slots=True)
class _Configuration:
    app_id: str
    cache_dir: str
    endpoint: str | None
    console_log_enabled: bool


_lock = threading.RLock()
_configurations: dict[_Owner, _Configuration] = {}


def _error_name(code: int) -> str:
    return str(_native.error_name(code))


def _check_code(code: int) -> None:
    if code:
        raise _error_from_code(code, _error_name(code))


def _normalize_configuration(
    app_id: str,
    cache_dir: str | os.PathLike[str],
    endpoint: str | None,
    console_log_enabled: bool,
) -> _Configuration:
    if not isinstance(app_id, str):
        raise TypeError("app_id must be str")
    if not app_id:
        raise ValueError("app_id must not be empty")
    try:
        raw_path = os.fspath(cache_dir)
    except TypeError:
        raise TypeError("cache_dir must be str or os.PathLike[str]") from None
    if not isinstance(raw_path, str):
        raise TypeError("cache_dir must resolve to str")
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("cache_dir must be absolute")
    if endpoint is not None and not isinstance(endpoint, str):
        raise TypeError("endpoint must be str or None")
    if endpoint == "":
        raise ValueError("endpoint must not be empty")
    if not isinstance(console_log_enabled, bool):
        raise TypeError("console_log_enabled must be bool")
    return _Configuration(app_id, str(path), endpoint, console_log_enabled)


def _initialize(
    owner: _Owner,
    app_id: str,
    cache_dir: str | os.PathLike[str],
    endpoint: str | None,
    console_log_enabled: bool,
) -> None:
    configuration = _normalize_configuration(
        app_id, cache_dir, endpoint, console_log_enabled
    )
    with _lock:
        current = _configurations.get(owner)
        if current == configuration:
            return
        if current is not None:
            raise _new_error(
                AlreadyInitializedError,
                code=6022,
                name="already_initialized",
            )
        for other in _configurations.values():
            if (
                other.cache_dir != configuration.cache_dir
                or other.console_log_enabled != configuration.console_log_enabled
            ):
                raise _new_error(
                    AlreadyInitializedError,
                    code=6022,
                    name="already_initialized",
                    message="RTC and Ti Cloud Storage must share cache_dir and console logging",
                )
        try:
            Path(configuration.cache_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
            probe = Path(configuration.cache_dir) / f".tirtc-write-probe-{os.getpid()}"
            with probe.open("xb"):
                pass
            probe.unlink()
        except OSError as error:
            raise _io_error(f"cache_dir is not writable: {configuration.cache_dir}") from error
        code = (
            _native.rtc_initialize(
                configuration.app_id,
                configuration.endpoint,
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
            if owner == "rtc"
            else _native.storage_initialize(
                configuration.app_id,
                configuration.endpoint,
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
        )
        _check_code(code)
        _configurations[owner] = configuration


def _shutdown(owner: _Owner) -> None:
    with _lock:
        if owner not in _configurations:
            return
        code = _native.rtc_shutdown() if owner == "rtc" else _native.storage_shutdown()
        _check_code(code)
        del _configurations[owner]
