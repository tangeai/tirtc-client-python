from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Callable
from datetime import date, datetime
from enum import IntEnum, StrEnum
import sys
import threading
from typing import Any, Self, final
import warnings
from zoneinfo import ZoneInfo

import tirtc
from tirtc import _native
from tirtc import _build_identity
from tirtc._core import _OutputBase
from tirtc._dispatch import _Dispatcher, _active_callback, _callback_owner, _native_sink, _native_task_sink
from tirtc._errors import (
    _error_from_code,
    _in_use_error,
    _timeout_error,
)
from tirtc._lifecycle import _CloseCoordinator, _copy_error
from tirtc._raw_dump import RawDump, normalize_raw_dump_ids
from tirtc._runtime import (
    ClientOptions,
    _RuntimeLease,
    _acquire_storage,
    _check_process,
    _error_name,
    _process_is_current,
    _upload_logs,
)
from tirtc._util import (
    _aware_datetime,
    _callable,
    _channel_id,
    _check_code,
    _datetime_from_ms,
    _datetime_ms,
    _nonempty_string,
    _retry_in_use,
    _retry_in_use_result,
    _timeout,
)
from tirtc._values import OutputState


class ReplaySpeed(StrEnum):
    X0_125 = "0.125x"
    X0_25 = "0.25x"
    X0_5 = "0.5x"
    X1 = "1x"
    X2 = "2x"
    X4 = "4x"
    X8 = "8x"


@dataclass(frozen=True, slots=True)
class RawDumpOptions:
    audio_channel_ids: tuple[int, ...] = ()
    video_channel_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        audio = normalize_raw_dump_ids(self.audio_channel_ids, "audio_channel_ids", 255)
        video = normalize_raw_dump_ids(self.video_channel_ids, "video_channel_ids", 255)
        if not audio and not video:
            raise ValueError("at least one audio or video channel must be selected")
        object.__setattr__(self, "audio_channel_ids", audio)
        object.__setattr__(self, "video_channel_ids", video)


_REPLAY_SPEED_TO_NATIVE = {
    ReplaySpeed.X0_125: 6,
    ReplaySpeed.X0_25: 5,
    ReplaySpeed.X0_5: 4,
    ReplaySpeed.X1: 0,
    ReplaySpeed.X2: 1,
    ReplaySpeed.X4: 2,
    ReplaySpeed.X8: 3,
}
_REPLAY_SPEED_FROM_NATIVE = {value: key for key, value in _REPLAY_SPEED_TO_NATIVE.items()}


@dataclass(frozen=True, slots=True)
class RecordingDay:
    date: date
    has_recording: bool


@dataclass(frozen=True, slots=True)
class RecordingRange:
    start_time: datetime
    end_time: datetime


class RecordingTrackKind(IntEnum):
    VIDEO = 1
    AUDIO = 2


class RecordingGapReason(IntEnum):
    UNKNOWN = 0
    NOT_FOUND = 1
    DOWNLOAD_FAILED = 2
    INTEGRITY_FAILED = 3
    MEDIA_UNREADABLE = 4
    NO_KEY_FRAME = 5
    NO_RECORDING = 6
    DECRYPTION_FAILED = 7
    UNSUPPORTED_MEDIA = 8
    TRACK_UNAVAILABLE = 9


class ExportTermination(IntEnum):
    EXHAUSTED = 0
    INTERRUPTED = 1
    CANCELLED = 2
    FAILED = 3


@dataclass(frozen=True, slots=True)
class RecordingTrack:
    kind: RecordingTrackKind
    channel_id: int


@dataclass(frozen=True, slots=True)
class RecordingGap:
    range: RecordingRange
    tracks: tuple[RecordingTrack, ...]
    reasons: tuple[RecordingGapReason, ...]


@dataclass(frozen=True, slots=True)
class ExportProgress:
    fraction: float
    covered_duration_ms: int


@dataclass(frozen=True, slots=True)
class ExportSegment:
    source_range: RecordingRange
    output_start_ms: int
    output_end_ms: int


@dataclass(frozen=True, slots=True)
class ExportReport:
    requested_range: RecordingRange
    covered_duration_ms: int
    segments: tuple[ExportSegment, ...]
    gaps: tuple[RecordingGap, ...]
    unprocessed_ranges: tuple[RecordingRange, ...]
    complete: bool
    termination: ExportTermination
    cause: tirtc.TiRTCError | None


def _recording_gap_from_native(value: Any) -> RecordingGap:
    start, end, tracks, reasons = value
    return RecordingGap(
        RecordingRange(_datetime_from_ms(int(start)), _datetime_from_ms(int(end))),
        tuple(RecordingTrack(RecordingTrackKind(int(kind)), int(channel)) for kind, channel in tracks),
        tuple(RecordingGapReason(int(reason)) for reason in reasons),
    )


def _export_report_from_native(value: Any) -> ExportReport:
    start, end, covered, complete, termination, cause, segments, gaps, unprocessed = value
    cause_code = int(cause)
    return ExportReport(
        requested_range=RecordingRange(_datetime_from_ms(int(start)), _datetime_from_ms(int(end))),
        covered_duration_ms=int(covered),
        segments=tuple(
            ExportSegment(
                RecordingRange(_datetime_from_ms(int(item[0])), _datetime_from_ms(int(item[1]))),
                int(item[2]),
                int(item[3]),
            )
            for item in segments
        ),
        gaps=tuple(_recording_gap_from_native(item) for item in gaps),
        unprocessed_ranges=tuple(
            RecordingRange(_datetime_from_ms(int(item[0])), _datetime_from_ms(int(item[1])))
            for item in unprocessed
        ),
        complete=bool(complete),
        termination=ExportTermination(int(termination)),
        cause=_error_from_code(cause_code, _error_name(cause_code)) if cause_code else None,
    )


def _destroy_with_barrier(handle: object) -> None:
    _retry_in_use(_native.close, handle)


def _callback_descends_from(owner: object | None, target: object) -> bool:
    pending = [owner]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        if current is target:
            return True
        seen.add(id(current))
        pending.extend(
            getattr(current, name, None)
            for name in ("_client", "_parent", "_replay", "_bound")
        )
    return False


@final
class Client:
    def __init__(
        self,
        options: ClientOptions,
        *,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
    ) -> None:
        if (access_key_id is None) != (access_key_secret is None):
            raise ValueError("access_key_id and access_key_secret must be provided together")
        if access_key_id is None:
            lease = _acquire_storage(options, "external_token")
        else:
            lease = _acquire_storage(
                options,
                "access_key",
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            )
        self._lease = lease
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._clouds: set[CloudStorage] = set()
        _build_identity.log("cloud_storage", _native)

    @property
    def _mode(self) -> str:
        return self._lease.mode

    def _open(self, mode: str, value: str) -> CloudStorage:
        _check_process()
        with self._lock:
            self._close.require_open()
            if self._mode != mode:
                raise _in_use_error()
            cloud = CloudStorage._prepare(self)
            if mode == "access_key":
                code, handle = _native.storage_client_open_device(
                    self._lease.handle, value
                )
            else:
                code, handle = _native.storage_create(value)
            _check_code(code)
            cloud._handle = handle
            self._clouds.add(cloud)
            return cloud

    def open_device(self, device_id: str) -> CloudStorage:
        device = _nonempty_string(device_id, "device_id")
        return self._open("access_key", device)

    def open_with_token(self, token: str) -> CloudStorage:
        access_token = _nonempty_string(token, "token")
        return self._open("external_token", access_token)

    def _finish_cloud(self, cloud: CloudStorage) -> None:
        with self._lock:
            self._clouds.discard(cloud)

    def upload_logs(self) -> str:
        _check_process()
        with self._lock:
            self._close.require_open()
            return _upload_logs(self._lease)

    def close(self) -> None:
        _check_process()
        with self._lock:
            if _callback_descends_from(_callback_owner(), self):
                raise _in_use_error()
            generation = self._close.begin()
            if generation is None:
                return
            clouds = tuple(self._clouds)
        try:
            child_error: BaseException | None = None
            for cloud in clouds:
                try:
                    cloud._close_from_client()
                except BaseException as error:
                    if cloud._close.status != "closed":
                        raise
                    if child_error is None:
                        child_error = error
            self._lease.close()
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, child_error)
        if child_error is not None:
            raise child_error

    def __del__(self) -> None:
        close = getattr(self, "_close", None)
        if close is None or close.status == "closed":
            return
        try:
            warnings.warn("unclosed TiRTC Cloud Client", ResourceWarning, stacklevel=2)
            if sys.is_finalizing() or not _process_is_current():
                return
            self.close()
        except BaseException:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _ListRequest:
    def __init__(self) -> None:
        self.handle: object | None = None
        self.completed = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._cancel_started = False
        self._cancel_finished = threading.Event()

    def on_completed(self, event: str) -> None:
        if event == "completed":
            self.completed.set()

    def set_handle(self, handle: object | None) -> None:
        self.handle = handle
        self.started.set()

    def cancel(self) -> None:
        self.started.wait()
        with self._lock:
            if self.handle is None or self.completed.is_set():
                return
            if self._cancel_started:
                perform = False
            else:
                self._cancel_started = True
                perform = True
        if not perform:
            self._cancel_finished.wait()
            return
        try:
            _check_code(_native.request_cancel(self.handle))
        finally:
            self._cancel_finished.set()

    def cancel_and_wait(self) -> None:
        self.cancel()
        while not self.completed.is_set() and not self.finished.wait(0.01):
            pass
        self.finished.wait()


@final
class CloudStorage:
    def __init__(self) -> None:
        raise TypeError("CloudStorage values are created by Client open methods")

    @classmethod
    def _prepare(cls, client: Client) -> CloudStorage:
        self = object.__new__(cls)
        self._handle = None
        self._client = client
        self._mode = client._mode
        self._requests: set[_ListRequest] = set()
        self._replays: set[Replay] = set()
        self._exports: set[ExportTask] = set()
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        return self

    def update_token(self, token: str) -> None:
        _check_process()
        token = _nonempty_string(token, "token")
        with self._lock:
            self._close.require_open()
            if self._mode != "external_token":
                raise _in_use_error()
            _check_code(_native.storage_update_token(self._handle, token))

    def _list(
        self,
        kind: str,
        first: object,
        second: object,
        timezone: str | None,
        timeout: float | None,
    ) -> list[tuple[object, ...]]:
        _check_process()
        deadline = _timeout(timeout)
        state = _ListRequest()
        with self._lock:
            self._close.require_open()
            if self._requests:
                raise _in_use_error()
            self._requests.add(state)
            handle = self._handle
        timed_out = False
        try:
            try:
                code, request = _native.storage_list_start(
                    handle, kind, first, second, timezone, state.on_completed
                )
                _check_code(code)
            except BaseException:
                state.set_handle(None)
                raise
            state.set_handle(request)
            if not state.completed.wait(deadline):
                timed_out = True
                state.cancel()
                state.completed.wait()
            code, values = _native.request_result(request)
            if timed_out:
                raise _timeout_error()
            _check_code(code)
            return list(values)
        finally:
            try:
                if state.handle is not None:
                    _destroy_with_barrier(state.handle)
            finally:
                with self._lock:
                    self._requests.discard(state)
                state.finished.set()

    def list_recording_days(
        self,
        start_date: date,
        end_date: date,
        *,
        timezone: str | ZoneInfo = "Asia/Shanghai",
        timeout: float | None = None,
    ) -> list[RecordingDay]:
        _check_process()
        if not isinstance(start_date, date) or isinstance(start_date, datetime):
            raise TypeError("start_date must be datetime.date")
        if not isinstance(end_date, date) or isinstance(end_date, datetime):
            raise TypeError("end_date must be datetime.date")
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        if isinstance(timezone, ZoneInfo):
            zone = timezone.key
        elif isinstance(timezone, str):
            if not timezone:
                raise ValueError("timezone must not be empty")
            try:
                ZoneInfo(timezone)
            except (KeyError, ValueError) as error:
                raise ValueError(f"unknown timezone: {timezone}") from error
            zone = timezone
        else:
            raise TypeError("timezone must be str or ZoneInfo")
        values = self._list(
            "days", start_date.isoformat(), end_date.isoformat(), zone, timeout
        )
        return [RecordingDay(date.fromisoformat(str(day)), bool(present)) for day, present in values]

    def list_recordings(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        timeout: float | None = None,
    ) -> list[RecordingRange]:
        _check_process()
        start = _datetime_ms(start_time, "start_time")
        end = _datetime_ms(end_time, "end_time")
        if start >= end:
            raise ValueError("start_time must be before end_time")
        values = self._list("recordings", start, end, None, timeout)
        return [
            RecordingRange(_datetime_from_ms(int(first)), _datetime_from_ms(int(second)))
            for first, second in values
        ]

    def create_replay(
        self,
        *,
        on_time_changed: Callable[[datetime], None] | None = None,
        on_completed: Callable[[], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
        on_recording_gap: Callable[[RecordingGap], None] | None = None,
    ) -> Replay:
        _check_process()
        _callable(on_time_changed, "on_time_changed")
        _callable(on_completed, "on_completed")
        _callable(on_error, "on_error")
        _callable(on_recording_gap, "on_recording_gap")
        with self._lock:
            self._close.require_open()
            replay = Replay.__new__(Replay)
            replay._prepare(self, on_time_changed, on_completed, on_error, on_recording_gap)
            self._replays.add(replay)
            try:
                code, handle = _native.replay_create(
                    self._handle,
                    _native_task_sink(replay._dispatcher),
                    on_time_changed is not None,
                    on_recording_gap is not None,
                )
                _check_code(code)
            except BaseException:
                self._replays.discard(replay)
                replay._parent_finished = True
                raise
            replay._handle = handle
            return replay

    def export_recording(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        video_channel_id: int,
        audio_channel_id: int | None = None,
        on_progress: Callable[[ExportProgress], None] | None = None,
        on_recording_gap: Callable[[RecordingGap], None] | None = None,
    ) -> ExportTask:
        _check_process()
        start = _datetime_ms(start_time, "start_time")
        end = _datetime_ms(end_time, "end_time")
        if start >= end:
            raise ValueError("start_time must be before end_time")
        video = _channel_id(video_channel_id, "video_channel_id")
        audio = -1 if audio_channel_id is None else _channel_id(audio_channel_id, "audio_channel_id")
        _callable(on_progress, "on_progress")
        _callable(on_recording_gap, "on_recording_gap")
        with self._lock:
            self._close.require_open()
            task = ExportTask.__new__(ExportTask)
            task._prepare(self, on_progress, on_recording_gap)
            self._exports.add(task)
            try:
                code, handle = _native.export_create(
                    self._handle,
                    start,
                    end,
                    video,
                    audio,
                    task._native_sink(),
                    on_progress is not None,
                    on_recording_gap is not None,
                )
                _check_code(code)
            except BaseException:
                self._exports.discard(task)
                task._parent_finished = True
                raise
            task._handle = handle
            return task

    def _finish_replay(self, replay: Replay) -> None:
        with self._lock:
            self._replays.discard(replay)

    def _finish_export(self, task: ExportTask) -> None:
        with self._lock:
            self._exports.discard(task)

    def close(self) -> None:
        self._close_cloud()

    def _close_from_client(self) -> None:
        self._close_cloud()

    def _close_cloud(self) -> None:
        _check_process()
        with self._lock:
            if _callback_descends_from(_callback_owner(), self):
                raise _in_use_error()
            generation = self._close.begin()
            if generation is None:
                return
            requests = tuple(self._requests)
            replays = tuple(self._replays)
            exports = tuple(self._exports)
        try:
            child_error: BaseException | None = None
            for request in requests:
                request.cancel_and_wait()
            for replay in replays:
                try:
                    replay._close_from_parent()
                except BaseException as error:
                    if replay._close.status != "closed":
                        raise
                    if child_error is None:
                        child_error = error
            for task in exports:
                task._close_from_parent()
            _retry_in_use(_native.close, self._handle)
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, child_error)
        self._client._finish_cloud(self)
        if child_error is not None:
            raise child_error

    def __del__(self) -> None:
        close = getattr(self, "_close", None)
        if (
            close is None
            or close.status == "closed"
            or getattr(self, "_handle", None) is None
        ):
            return
        try:
            warnings.warn("unclosed TiRTC CloudStorage", ResourceWarning, stacklevel=2)
            if sys.is_finalizing() or not _process_is_current():
                return
            self._close_cloud()
        except BaseException:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@final
class Replay:
    def __init__(self) -> None:
        raise TypeError("Replay values are created by CloudStorage.create_replay()")

    def _prepare(
        self,
        parent: CloudStorage,
        on_time_changed,
        on_completed,
        on_error,
        on_recording_gap,
    ) -> None:
        self._handle = None
        self._parent = parent
        self._on_time_changed = on_time_changed
        self._on_completed = on_completed
        self._on_error = on_error
        self._on_recording_gap = on_recording_gap
        self._lock = threading.RLock()
        self._dispatcher = _Dispatcher(self._handle_event)
        self._speed = ReplaySpeed.X1
        self._outputs: set[_StorageOutputBase] = set()
        self._tasks: set[RecordingTask] = set()
        self._dumps: set[RawDump] = set()
        self._close = _CloseCoordinator(self._lock)
        self._native_stopped = False
        self._parent_finished = False
        self._playback_range: tuple[int, int] | None = None

    def _handle_event(self, kind: str, *values: object) -> None:
        with _active_callback(self):
            if kind == "time":
                if self._on_time_changed is not None:
                    self._on_time_changed(_datetime_from_ms(int(values[0])))
            elif kind == "completed":
                if self._on_completed is not None:
                    self._on_completed()
            elif kind == "error":
                if self._on_error is not None:
                    code = int(values[0])
                    self._on_error(_error_from_code(code, _error_name(code)))
            elif kind == "gap" and self._on_recording_gap is not None:
                self._on_recording_gap(_recording_gap_from_native(values[0]))

    def _in_related_callback(self) -> bool:
        callback = _callback_owner()
        return callback is self or getattr(callback, "_bound", None) is self

    @contextmanager
    def _operation_lock(self):
        if not self._lock.acquire(blocking=not self._in_related_callback()):
            raise _in_use_error()
        try:
            yield
        finally:
            self._lock.release()

    def _reject_blocking_callback(self) -> None:
        if self._in_related_callback():
            raise _in_use_error()

    def _operation(self, function, *arguments: object) -> None:
        _check_process()
        with self._operation_lock():
            self._close.require_open()
            if self._in_related_callback():
                _check_code(function(self._handle, *arguments))
            else:
                _retry_in_use(function, self._handle, *arguments)

    def play(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        initial_time: datetime | None = None,
    ) -> None:
        _check_process()
        self._reject_blocking_callback()
        start = _datetime_ms(start_time, "start_time")
        end = _datetime_ms(end_time, "end_time")
        if start >= end:
            raise ValueError("start_time must be before end_time")
        initial = start if initial_time is None else _datetime_ms(initial_time, "initial_time")
        if initial < start or initial >= end:
            raise ValueError("initial_time must be inside the playback range")
        with self._operation_lock():
            self._close.require_open()
            _check_code(_native.replay_play(self._handle, start, end, initial))
            self._playback_range = (start, end)
            self._native_stopped = False

    def pause(self) -> None:
        self._operation(_native.replay_pause)

    def resume(self) -> None:
        self._operation(_native.replay_resume)

    def seek(self, target: datetime) -> None:
        _check_process()
        self._reject_blocking_callback()
        target_ms = _datetime_ms(target, "target")
        with self._operation_lock():
            self._close.require_open()
            if self._playback_range is not None:
                start, end = self._playback_range
                if target_ms < start or target_ms >= end:
                    raise ValueError("target must be inside the playback range")
            _retry_in_use(_native.replay_seek, self._handle, target_ms)

    def set_speed(self, speed: ReplaySpeed) -> None:
        _check_process()
        if not isinstance(speed, ReplaySpeed):
            raise TypeError("speed must be ReplaySpeed")
        with self._operation_lock():
            self._close.require_open()
            if self._in_related_callback():
                _check_code(
                    _native.replay_set_speed(self._handle, _REPLAY_SPEED_TO_NATIVE[speed])
                )
            else:
                _retry_in_use(
                    _native.replay_set_speed,
                    self._handle,
                    _REPLAY_SPEED_TO_NATIVE[speed],
                )
            self._speed = speed

    @property
    def speed(self) -> ReplaySpeed:
        _check_process()
        with self._operation_lock():
            self._close.require_open()
            code, value = _native.replay_get_speed(self._handle)
            _check_code(code)
            self._speed = _REPLAY_SPEED_FROM_NATIVE[int(value)]
            return self._speed

    @property
    def current_time(self) -> datetime | None:
        _check_process()
        with self._operation_lock():
            self._close.require_open()
            code, present, value = _native.replay_current_time(self._handle)
        _check_code(code)
        return _datetime_from_ms(int(value)) if present else None

    def stop(self) -> None:
        _check_process()
        self._reject_blocking_callback()
        with self._operation_lock():
            self._close.require_open()
            _retry_in_use(_native.replay_stop, self._handle)
            self._native_stopped = True

    def start_recording(
        self, *, video_channel_id: int, audio_channel_id: int | None = None
    ) -> RecordingTask:
        _check_process()
        video = _channel_id(video_channel_id, "video_channel_id")
        audio = -1 if audio_channel_id is None else _channel_id(audio_channel_id, "audio_channel_id")
        with self._operation_lock():
            self._close.require_open()
            task = RecordingTask._prepare(self)
            self._tasks.add(task)
            try:
                code, handle = _native.storage_recording_start(self._handle, video, audio)
                _check_code(code)
            except BaseException:
                self._tasks.discard(task)
                task._parent_finished = True
                raise
            task._handle = handle
            return task

    def start_raw_dump(self, options: RawDumpOptions) -> RawDump:
        _check_process()
        if not isinstance(options, RawDumpOptions):
            raise TypeError("options must be RawDumpOptions")
        self._reject_blocking_callback()
        with self._operation_lock():
            self._close.require_open()
            code, handle = _native.storage_raw_dump_start(
                self._handle,
                bytes(options.audio_channel_ids),
                bytes(options.video_channel_ids),
            )
            _check_code(code)
            dump = RawDump._create(self, handle)
            self._dumps.add(dump)
            return dump

    def _attach(self, output: _StorageOutputBase) -> object:
        _check_process()
        with self._operation_lock():
            self._close.require_open()
            self._outputs.add(output)
            return self._handle

    def _attach_failed(self, output: _StorageOutputBase) -> None:
        with self._lock:
            self._outputs.discard(output)

    def _detach(self, output: _StorageOutputBase) -> None:
        with self._lock:
            self._outputs.discard(output)

    def _finish_task(self, task: RecordingTask) -> None:
        with self._lock:
            self._tasks.discard(task)

    def _finish_raw_dump(self, dump: RawDump) -> None:
        with self._lock:
            self._dumps.discard(dump)

    def close(self) -> None:
        _check_process()
        self._close_replay()

    def _close_from_parent(self) -> None:
        self._close_replay()

    def _close_replay(self) -> None:
        self._reject_blocking_callback()
        with self._lock:
            generation = self._close.begin()
            if generation is None:
                return
            outputs = tuple(self._outputs)
            tasks = tuple(self._tasks)
            dumps = tuple(self._dumps)
        try:
            raw_dump_error: BaseException | None = None
            for dump in dumps:
                try:
                    dump.close()
                except BaseException as error:
                    if not dump._closed:
                        raise
                    if raw_dump_error is None:
                        raw_dump_error = error
            for task in tasks:
                result = task.stop()
                result.delete()
            for output in outputs:
                output._detach_from_parent(self)
            if not self._native_stopped:
                _retry_in_use(_native.replay_stop, self._handle)
                self._native_stopped = True
            self._dispatcher.close()
            _retry_in_use(_native.close, self._handle)
            if not self._parent_finished:
                self._parent._finish_replay(self)
                self._parent_finished = True
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, raw_dump_error)
        if raw_dump_error is not None:
            raise raw_dump_error

    def __del__(self) -> None:
        close = getattr(self, "_close", None)
        if (
            close is None
            or close.status == "closed"
            or getattr(self, "_handle", None) is None
        ):
            return
        try:
            warnings.warn("unclosed TiRTC Replay", ResourceWarning, stacklevel=2)
            if sys.is_finalizing() or not _process_is_current():
                return
            self._dispatcher.discard()
            self._close_replay()
        except BaseException:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@final
class RecordingTask:
    def __init__(self) -> None:
        raise TypeError("RecordingTask values are created by Replay.start_recording()")

    @classmethod
    def _prepare(cls, replay: Replay) -> RecordingTask:
        self = object.__new__(cls)
        self._handle = None
        self._replay = replay
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._result: tirtc.RecordingFile | None = None
        self._error: BaseException | None = None
        self._stopped = False
        self._parent_finished = False
        return self

    @classmethod
    def _create(cls, handle: object, replay: Replay) -> RecordingTask:
        self = cls._prepare(replay)
        self._handle = handle
        return self

    def stop(self) -> tirtc.RecordingFile:
        _check_process()
        if _callback_owner() is self._replay:
            raise _in_use_error()
        with self._lock:
            generation = self._close.begin()
            if generation is None:
                if self._error is not None:
                    raise _copy_error(self._error)
                assert self._result is not None
                return self._result
        try:
            if not self._stopped:
                code, path, duration_ms = _retry_in_use_result(
                    _native.recording_stop, self._handle
                )
                self._error = _error_from_code(code, _error_name(code)) if code else None
                self._result = (
                    tirtc.RecordingFile._create(str(path), int(duration_ms)) if not code else None
                )
                self._stopped = True
            _retry_in_use(_native.close, self._handle)
            if not self._parent_finished:
                self._replay._finish_task(self)
                self._parent_finished = True
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, self._error)
        if self._error is not None:
            raise _copy_error(self._error)
        assert self._result is not None
        return self._result

    def __del__(self) -> None:
        close = getattr(self, "_close", None)
        if close is None or close.status == "closed" or getattr(self, "_handle", None) is None:
            return
        try:
            warnings.warn("unclosed TiRTC RecordingTask", ResourceWarning, stacklevel=2)
            if sys.is_finalizing() or not _process_is_current():
                return
            result = self.stop()
            result.delete()
        except BaseException:
            pass


class _StorageOutputBase(_OutputBase):
    _bound: Replay | None

    def _native_detach(self) -> int:
        return int(_native.output_detach_storage(self._handle))

    def attach(self, replay: Replay, channel_id: int) -> None:
        _check_process()
        if not isinstance(replay, Replay):
            raise TypeError("replay must be Replay")
        channel = _channel_id(channel_id, "channel_id")
        with self._lock:
            self._close.require_open()
            if self._bound is not None:
                raise _in_use_error()
            replay_handle = replay._attach(self)
            try:
                _check_code(
                    _native.output_attach_storage(self._handle, replay_handle, channel)
                )
            except BaseException:
                replay._attach_failed(self)
                raise
            self._bound = replay
            self._native_detached = False

    def detach(self) -> None:
        _check_process()
        with self._lock:
            self._close.require_open()
            if self._bound is None:
                _check_code(6029)
            if self._detaching:
                raise _in_use_error()
            bound = self._bound
            callback = _callback_owner()
            if callback is self or callback is bound:
                raise _in_use_error()
            _retry_in_use(self._native_detach)
            self._detaching = True
            self._state = OutputState.IDLE
        self._dispatcher.barrier()
        with self._lock:
            self._bound = None
            self._detaching = False
        assert bound is not None
        bound._detach(self)

    def _detach_from_parent(self, replay: Replay) -> None:
        with self._lock:
            if self._bound is not replay:
                return
            if self._detaching:
                raise _in_use_error()
            callback = _callback_owner()
            if callback is self or callback is replay:
                raise _in_use_error()
            _retry_in_use(self._native_detach)
            self._detaching = True
            self._state = OutputState.IDLE
        self._dispatcher.barrier()
        with self._lock:
            self._bound = None
            self._detaching = False
        replay._detach(self)


@final
class AudioOutput(_StorageOutputBase):
    _native_kind = "audio"

    def __init__(
        self,
        on_frame: Callable[[tirtc.AudioFrame], None],
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


@final
class VideoOutput(_StorageOutputBase):
    _native_kind = "video"

    def __init__(
        self,
        on_frame: Callable[[tirtc.VideoFrame], None],
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)

    def take_snapshot(self) -> tirtc.SnapshotFile:
        _check_process()
        with self._lock:
            self._close.require_open()
            code, path = _native.output_snapshot(self._handle)
        _check_code(code)
        return tirtc.SnapshotFile._create(str(path))


@final
class EncodedAudioOutput(_StorageOutputBase):
    _native_kind = "encoded_audio"

    def __init__(
        self,
        on_frame: Callable[[tirtc.EncodedAudioFrame], None],
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


@final
class EncodedVideoOutput(_StorageOutputBase):
    _native_kind = "encoded_video"

    def __init__(
        self,
        on_frame: Callable[[tirtc.EncodedVideoFrame], None],
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


@final
class ExportTask:
    def __init__(self) -> None:
        raise TypeError("ExportTask values are created by CloudStorage.export_recording()")

    def _prepare(
        self,
        parent: CloudStorage,
        on_progress: Callable[[ExportProgress], None] | None,
        on_recording_gap: Callable[[RecordingGap], None] | None,
    ) -> None:
        self._parent = parent
        self._handle = None
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._done = threading.Event()
        self._progress = ExportProgress(0.0, 0)
        self._report: ExportReport | None = None
        self._on_progress = on_progress
        self._on_recording_gap = on_recording_gap
        self._dispatcher = _Dispatcher(self._handle_event)
        self._terminal = False
        self._parent_finished = False
        self._file: tirtc.RecordingFile | None = None
        self._error: BaseException | None = None

    def _native_sink(self):
        return _native_task_sink(self._dispatcher)

    def _handle_event(self, kind: str, *values: object) -> None:
        with _active_callback(self):
            if kind == "progress":
                progress = ExportProgress(float(values[0]), int(values[1]))
                with self._lock:
                    if self._terminal or progress.fraction < self._progress.fraction:
                        return
                    self._progress = progress
                if self._on_progress is not None:
                    self._on_progress(progress)
            elif kind == "gap":
                if self._on_recording_gap is not None:
                    self._on_recording_gap(_recording_gap_from_native(values[0]))
            elif kind == "completed":
                with self._lock:
                    if self._terminal:
                        return
                    code = int(values[0])
                    self._terminal = True
                    self._file = (
                        tirtc.RecordingFile._create(str(values[1]), int(values[2]))
                        if code == 0
                        else None
                    )
                    self._error = (
                        _error_from_code(code, _error_name(code)) if code else None
                    )
                    self._done.set()

    @property
    def progress(self) -> float:
        return self.progress_detail.fraction

    @property
    def progress_detail(self) -> ExportProgress:
        _check_process()
        with self._lock:
            if self._close.status == "open" and self._handle is not None:
                code, fraction, covered = _native.export_progress(self._handle)
                _check_code(code)
                current = ExportProgress(min(max(float(fraction), 0.0), 1.0), int(covered))
                if current.fraction >= self._progress.fraction:
                    self._progress = current
            return self._progress

    def _load_report(self) -> ExportReport:
        with self._lock:
            if self._report is not None:
                return self._report
            code, value = _native.export_report(self._handle)
            _check_code(code)
            progress_code, fraction, covered = _native.export_progress(self._handle)
            _check_code(progress_code)
            self._progress = ExportProgress(float(fraction), int(covered))
            self._report = _export_report_from_native(value)
            return self._report

    @property
    def report(self) -> ExportReport:
        _check_process()
        with self._lock:
            if not self._terminal:
                raise _in_use_error()
        return self._load_report()

    def _result_or_error(self) -> tirtc.RecordingFile:
        error = self._error
        result = self._file
        if error is not None:
            raise _copy_error(error)
        assert result is not None
        return result

    def _finish(self) -> tirtc.RecordingFile:
        with self._lock:
            generation = self._close.begin()
            if generation is None:
                return self._result_or_error()
        try:
            self._load_report()
            self._dispatcher.close()
            _destroy_with_barrier(self._handle)
            if not self._parent_finished:
                self._parent._finish_export(self)
                self._parent_finished = True
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, self._error)
        return self._result_or_error()

    def wait(self, *, timeout: float | None = None) -> tirtc.RecordingFile:
        _check_process()
        if _callback_owner() is self:
            raise _in_use_error()
        if not self._done.wait(_timeout(timeout)):
            raise _timeout_error()
        return self._finish()

    def cancel(self) -> None:
        _check_process()
        with self._lock:
            if self._terminal:
                return
            self._close.require_open()
            handle = self._handle
        _check_code(_native.export_cancel(handle))

    def _close_from_parent(self) -> None:
        with self._lock:
            terminal = self._terminal
        if not terminal:
            self.cancel()
        try:
            result = self.wait()
        except tirtc.TiRTCError:
            with self._lock:
                if self._close.status != "closed":
                    raise
        else:
            result.delete()

    def stop(self) -> tirtc.RecordingFile:
        _check_process()
        if _callback_owner() is self:
            raise _in_use_error()
        with self._lock:
            generation = self._close.begin()
            if generation is None:
                return self._result_or_error()
            terminal = self._terminal
        try:
            if not terminal:
                code, path, duration_ms = _retry_in_use_result(
                    _native.export_stop, self._handle
                )
                with self._lock:
                    if not self._terminal:
                        self._terminal = True
                        self._file = (
                            tirtc.RecordingFile._create(str(path), int(duration_ms))
                            if code == 0
                            else None
                        )
                        self._error = (
                            _error_from_code(code, _error_name(code)) if code else None
                        )
                        self._done.set()
            self._load_report()
            self._dispatcher.close()
            _destroy_with_barrier(self._handle)
            if not self._parent_finished:
                self._parent._finish_export(self)
                self._parent_finished = True
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, self._error)
        return self._result_or_error()

    def __del__(self) -> None:
        close = getattr(self, "_close", None)
        if (
            close is None
            or close.status == "closed"
            or getattr(self, "_handle", None) is None
        ):
            return
        try:
            warnings.warn("unclosed TiRTC ExportTask", ResourceWarning, stacklevel=2)
            if sys.is_finalizing() or not _process_is_current():
                return
            result = self.stop()
            result.delete()
        except BaseException:
            pass


__all__ = [
    "AudioOutput",
    "Client",
    "CloudStorage",
    "EncodedAudioOutput",
    "EncodedVideoOutput",
    "ExportProgress",
    "ExportReport",
    "ExportSegment",
    "ExportTermination",
    "ExportTask",
    "RecordingGap",
    "RecordingGapReason",
    "RecordingTrack",
    "RecordingTrackKind",
    "RecordingDay",
    "RecordingRange",
    "RecordingTask",
    "Replay",
    "ReplaySpeed",
    "RawDump",
    "RawDumpOptions",
    "VideoOutput",
]
