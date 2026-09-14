from __future__ import annotations

import atexit
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import sys
import threading
import time
from typing import Literal

from ._errors import (
    AlreadyInitializedError,
    UnsupportedError,
    _error_from_code,
    _in_use_error,
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


_AuthMode = Literal["access_key", "external_token"]
_Product = Literal["rtc", "storage"]


@dataclass(frozen=True, slots=True)
class ClientOptions:
    app_id: str
    cache_dir: str | os.PathLike[str]
    endpoint: str | None = None
    console_log_enabled: bool = False


@dataclass(frozen=True, slots=True)
class _Configuration:
    app_id: str
    cache_dir: str
    endpoint: str | None
    console_log_enabled: bool


class _RuntimeLease:
    __slots__ = ("configuration", "handle", "mode", "product", "_closed")

    def __init__(
        self,
        product: _Product,
        mode: _AuthMode,
        configuration: _Configuration,
        handle: object | None,
    ) -> None:
        self.product = product
        self.mode = mode
        self.configuration = configuration
        self.handle = handle
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        _release(self)


_lock = threading.RLock()
_active_leases: set[_RuntimeLease] = set()
_rtc_lease: _RuntimeLease | None = None
_external_storage_configuration: tuple[str, str | None] | None = None
_external_storage_count = 0
_shared_configuration: tuple[str, bool] | None = None
_process_id = os.getpid()
_forked_with_runtime = False
_interpreter_finalizing = False


def _mark_finalizing() -> None:
    global _interpreter_finalizing
    _native.mark_finalizing()
    _interpreter_finalizing = True


atexit.register(_mark_finalizing)


def _after_fork_child() -> None:
    global _forked_with_runtime, _lock, _process_id
    _forked_with_runtime = bool(_active_leases)
    _process_id = os.getpid()
    _lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def _process_is_current() -> bool:
    return (
        os.getpid() == _process_id
        and not _forked_with_runtime
        and not _interpreter_finalizing
    )


def _check_process() -> None:
    if not _process_is_current():
        if _interpreter_finalizing:
            raise _new_error(
                UnsupportedError,
                code=None,
                name="interpreter_finalizing",
                message="TiRTC cannot be used while the Python interpreter is finalizing",
            )
        raise _new_error(
            UnsupportedError,
            code=None,
            name="forked_process",
            message="create the TiRTC Client inside the worker process after fork",
        )


def _error_name(code: int) -> str:
    _check_process()
    return str(_native.error_name(code))


def _check_code(code: int) -> None:
    if code:
        raise _error_from_code(code, _error_name(code))


def _validated_string(value: object, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str" + (" or None" if optional else ""))
    if not value:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    return value


def _normalize_options(options: ClientOptions) -> _Configuration:
    if not isinstance(options, ClientOptions):
        raise TypeError("options must be ClientOptions")
    app_id = _validated_string(options.app_id, "options.app_id")
    try:
        raw_path = os.fspath(options.cache_dir)
    except TypeError:
        raise TypeError("options.cache_dir must be str or os.PathLike[str]") from None
    if not isinstance(raw_path, str):
        raise TypeError("options.cache_dir must resolve to str")
    if "\x00" in raw_path:
        raise ValueError("options.cache_dir must not contain NUL")
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("options.cache_dir must be absolute")
    endpoint = _validated_string(options.endpoint, "options.endpoint", optional=True)
    if not isinstance(options.console_log_enabled, bool):
        raise TypeError("options.console_log_enabled must be bool")
    assert app_id is not None
    return _Configuration(
        app_id,
        str(path),
        endpoint,
        options.console_log_enabled,
    )


def _validate_access_keys(
    access_key_id: object, access_key_secret: object
) -> tuple[str, str]:
    key_id = _validated_string(access_key_id, "access_key_id")
    secret = _validated_string(access_key_secret, "access_key_secret")
    assert key_id is not None and secret is not None
    return key_id, secret


def _prepare_cache(configuration: _Configuration) -> None:
    try:
        path = Path(configuration.cache_dir)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = path / f".tirtc-write-probe-{os.getpid()}-{threading.get_ident()}"
        with probe.open("xb"):
            pass
        probe.unlink()
    except OSError as error:
        raise _io_error(f"cache_dir is not writable: {configuration.cache_dir}") from error


def _preflight_shared(configuration: _Configuration) -> None:
    shared = (configuration.cache_dir, configuration.console_log_enabled)
    if _shared_configuration is not None and _shared_configuration != shared:
        raise _new_error(
            AlreadyInitializedError,
            code=6022,
            name="already_initialized",
            message="all TiRTC Clients must share cache_dir and console logging",
        )


def _acquire_rtc(
    options: ClientOptions,
    mode: _AuthMode,
    *,
    access_key_id: object | None = None,
    access_key_secret: object | None = None,
) -> _RuntimeLease:
    global _rtc_lease, _shared_configuration
    _check_process()
    configuration = _normalize_options(options)
    credentials = (
        _validate_access_keys(access_key_id, access_key_secret)
        if mode == "access_key"
        else None
    )
    with _lock:
        if _rtc_lease is not None:
            raise _in_use_error()
        _preflight_shared(configuration)
        _prepare_cache(configuration)
        handle = None
        if mode == "access_key":
            assert credentials is not None
            code, handle = _native.rtc_client_create(
                configuration.app_id,
                credentials[0],
                credentials[1],
                configuration.endpoint,
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
        else:
            code = _native.rtc_initialize(
                configuration.app_id,
                configuration.endpoint,
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
        _check_code(code)
        lease = _RuntimeLease("rtc", mode, configuration, handle)
        _rtc_lease = lease
        _active_leases.add(lease)
        if _shared_configuration is None:
            _shared_configuration = (
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
        return lease


def _acquire_storage(
    options: ClientOptions,
    mode: _AuthMode,
    *,
    access_key_id: object | None = None,
    access_key_secret: object | None = None,
) -> _RuntimeLease:
    global _external_storage_configuration, _external_storage_count, _shared_configuration
    _check_process()
    configuration = _normalize_options(options)
    credentials = (
        _validate_access_keys(access_key_id, access_key_secret)
        if mode == "access_key"
        else None
    )
    with _lock:
        _preflight_shared(configuration)
        legacy_configuration = (configuration.app_id, configuration.endpoint)
        if (
            mode == "external_token"
            and _external_storage_configuration is not None
            and _external_storage_configuration != legacy_configuration
        ):
            raise _new_error(
                AlreadyInitializedError,
                code=6022,
                name="already_initialized",
                message="external-token Cloud Clients must share app_id and endpoint",
            )
        _prepare_cache(configuration)
        handle = None
        if mode == "access_key":
            assert credentials is not None
            code, handle = _native.storage_client_create(
                configuration.app_id,
                credentials[0],
                credentials[1],
                configuration.endpoint,
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
            _check_code(code)
        elif _external_storage_count == 0:
            code = _native.storage_initialize(
                configuration.app_id,
                configuration.endpoint,
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
            _check_code(code)
        lease = _RuntimeLease("storage", mode, configuration, handle)
        _active_leases.add(lease)
        if mode == "external_token":
            _external_storage_configuration = legacy_configuration
            _external_storage_count += 1
        if _shared_configuration is None:
            _shared_configuration = (
                configuration.cache_dir,
                configuration.console_log_enabled,
            )
        return lease


def _close_handle(handle: object) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        code = int(_native.close(handle))
        if code != 6026 or time.monotonic() >= deadline:
            _check_code(code)
            return
        time.sleep(0.005)


def _release(lease: _RuntimeLease) -> None:
    global _external_storage_configuration, _external_storage_count
    global _rtc_lease, _shared_configuration
    _check_process()
    with _lock:
        if lease._closed:
            return
        if lease not in _active_leases:
            raise _in_use_error()
        if lease.mode == "access_key":
            assert lease.handle is not None
            _close_handle(lease.handle)
        elif lease.product == "rtc":
            _check_code(_native.rtc_shutdown())
        else:
            if _external_storage_count <= 0:
                raise RuntimeError("invalid external-token Cloud Client lease count")
            if _external_storage_count == 1:
                _check_code(_native.storage_shutdown())
        _active_leases.remove(lease)
        if lease.product == "rtc":
            _rtc_lease = None
        elif lease.mode == "external_token":
            _external_storage_count -= 1
            if _external_storage_count == 0:
                _external_storage_configuration = None
        lease._closed = True
        lease.handle = None
        if not _active_leases:
            _shared_configuration = None


def _upload_logs(lease: _RuntimeLease) -> str:
    _check_process()
    with _lock:
        if lease._closed or lease not in _active_leases:
            from ._errors import _closed_error

            raise _closed_error()
        code, log_id = _native.upload_logs()
    _check_code(code)
    return str(log_id)
