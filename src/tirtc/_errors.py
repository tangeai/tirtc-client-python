from __future__ import annotations

from typing import NoReturn


class TiRTCError(Exception):
    __slots__ = ("_code", "_name")

    def __init__(
        self,
        message: str = "",
        *,
        code: int | None = None,
        name: str = "unknown",
    ) -> None:
        super().__init__(message)
        self._code = code
        self._name = name

    @property
    def code(self) -> int | None:
        return self._code

    @property
    def name(self) -> str:
        return self._name


class InvalidArgumentError(TiRTCError):
    __slots__ = ()


class NotInitializedError(TiRTCError):
    __slots__ = ()


class AlreadyInitializedError(TiRTCError):
    __slots__ = ()


class InUseError(TiRTCError):
    __slots__ = ()


class NotStartedError(TiRTCError):
    __slots__ = ()


class NotConnectedError(TiRTCError):
    __slots__ = ()


class NotBoundError(TiRTCError):
    __slots__ = ()


class NotConfiguredError(TiRTCError):
    __slots__ = ()


class ClosedError(TiRTCError):
    __slots__ = ()


class AuthenticationError(TiRTCError):
    __slots__ = ()


class OperationTimeoutError(TiRTCError):
    __slots__ = ()


class RemoteClosedError(TiRTCError):
    __slots__ = ()


class TokenExpiredError(TiRTCError):
    __slots__ = ()


class PermissionDeniedError(TiRTCError):
    __slots__ = ()


class ResourceExhaustedError(TiRTCError):
    __slots__ = ()


class UnsupportedError(TiRTCError):
    __slots__ = ()


class UnsupportedFormatError(TiRTCError):
    __slots__ = ()


class TiRTCIOError(TiRTCError):
    __slots__ = ()


class NoFrameError(TiRTCError):
    __slots__ = ()


class NoRecordableMediaError(TiRTCError):
    __slots__ = ()


class RecordingOverrunError(TiRTCError):
    __slots__ = ()


class LogExportError(TiRTCError):
    __slots__ = ()


class LogUploadError(TiRTCError):
    __slots__ = ()


class CancelledError(TiRTCError):
    __slots__ = ()


class RangeTooLargeError(TiRTCError):
    __slots__ = ()


class RecordingUnreadableError(TiRTCError):
    __slots__ = ()


class RecordingNotFoundError(TiRTCError):
    __slots__ = ()


class RecordingDownloadFailedError(TiRTCError):
    __slots__ = ()


class UnavailableError(TiRTCError):
    __slots__ = ()


class StoppedError(TiRTCError):
    __slots__ = ()


class NetworkUnavailableError(TiRTCError):
    __slots__ = ()


class EndpointDNSResolutionFailedError(TiRTCError):
    __slots__ = ()


_ERROR_TYPES: dict[int, type[TiRTCError]] = {
    6000: InvalidArgumentError,
    6001: NotInitializedError,
    6008: AuthenticationError,
    6009: OperationTimeoutError,
    6012: RemoteClosedError,
    6014: TokenExpiredError,
    6022: AlreadyInitializedError,
    6024: PermissionDeniedError,
    6026: InUseError,
    6027: NotStartedError,
    6028: NotConnectedError,
    6029: NotBoundError,
    6030: NotConfiguredError,
    6032: InvalidArgumentError,
    6043: ResourceExhaustedError,
    6044: TiRTCIOError,
    6045: TiRTCIOError,
    6046: TiRTCIOError,
    6048: LogExportError,
    6049: LogUploadError,
    6073: LogExportError,
    6107: UnsupportedError,
    6111: PermissionDeniedError,
    6112: ResourceExhaustedError,
    6113: UnsupportedFormatError,
    6114: TiRTCIOError,
    6115: CancelledError,
    6117: RangeTooLargeError,
    6118: NoFrameError,
    6119: NoRecordableMediaError,
    6120: RecordingOverrunError,
    6121: TiRTCIOError,
    6122: RecordingUnreadableError,
    6123: UnavailableError,
    6124: StoppedError,
    6125: LogExportError,
    6126: LogExportError,
    6127: LogUploadError,
    6128: LogUploadError,
    6129: LogUploadError,
    6130: OperationTimeoutError,
    6131: LogUploadError,
    6132: LogUploadError,
    6133: LogUploadError,
    6134: RecordingNotFoundError,
    6135: RecordingDownloadFailedError,
    6136: NetworkUnavailableError,
    6137: EndpointDNSResolutionFailedError,
}


def _new_error(
    error_type: type[TiRTCError],
    *,
    code: int | None,
    name: str,
    message: str | None = None,
) -> TiRTCError:
    detail = message or name
    if code is not None:
        detail = f"{detail} ({code})"
    return error_type(detail, code=code, name=name)


def _error_from_code(code: int, name: str) -> TiRTCError:
    return _new_error(_ERROR_TYPES.get(code, TiRTCError), code=code, name=name)


def _raise_code(code: int, name: str) -> NoReturn:
    raise _error_from_code(code, name)


def _closed_error() -> ClosedError:
    return _new_error(ClosedError, code=None, name="closed")  # type: ignore[return-value]


def _in_use_error() -> InUseError:
    return _new_error(InUseError, code=6026, name="in_use")  # type: ignore[return-value]


def _timeout_error() -> OperationTimeoutError:
    return _new_error(
        OperationTimeoutError, code=None, name="operation_timeout"
    )  # type: ignore[return-value]


def _io_error(message: str) -> TiRTCIOError:
    return _new_error(TiRTCIOError, code=None, name="io_failed", message=message)  # type: ignore[return-value]
