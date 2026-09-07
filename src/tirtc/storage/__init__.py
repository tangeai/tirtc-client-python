from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
import os
import sys
import threading
from typing import Self, final
import weakref
import warnings
from zoneinfo import ZoneInfo

import tirtc
from tirtc import _native
from tirtc import _build_identity
from tirtc._core import _OutputBase
from tirtc._dispatch import _Dispatcher, _active_callback, _callback_owner, _native_sink
from tirtc._errors import (
    _error_from_code,
    _in_use_error,
    _timeout_error,
)
from tirtc._lifecycle import _CloseCoordinator, _copy_error
from tirtc._runtime import (
    _check_process,
    _error_name,
    _initialize,
    _process_is_current,
    _shutdown,
)
from tirtc._util import (
    _aware_datetime,
    _callable,
    _channel_id,
    _check_code,
    _datetime_from_ms,
    _datetime_ms,
    _integer,
    _nonempty_string,
    _retry_in_use,
    _retry_in_use_result,
    _timeout,
)
from tirtc._values import OutputState


def initialize(
    app_id: str,
    cache_dir: str | os.PathLike[str],
    *,
    endpoint: str | None = None,
    console_log_enabled: bool = False,
) -> None:
    _initialize("storage", app_id, cache_dir, endpoint, console_log_enabled)
    _build_identity.log("cloud_storage", _native)


def shutdown() -> None:
    _shutdown("storage")


class ReplaySpeed(StrEnum):
    X0_125 = "0.125x"
    X0_25 = "0.25x"
    X0_5 = "0.5x"
    X1 = "1x"
    X2 = "2x"
    X4 = "4x"
    X8 = "8x"


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


def _destroy_with_barrier(handle: object) -> None:
    _retry_in_use(_native.close, handle)


@final
class CloudStorage:
    def __init__(self, token: str) -> None:
        _check_process()
        token = _nonempty_string(token, "token")
        self._handle = None
        self._list_active = False
        self._dependencies = 0
        self._tasks = 0
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        code, handle = _native.storage_create(token)
        _check_code(code)
        self._handle = handle

    def update_token(self, token: str) -> None:
        _check_process()
        token = _nonempty_string(token, "token")
        with self._lock:
            self._close.require_open()
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
        completed = threading.Event()

        def on_completed(event: str) -> None:
            if event == "completed":
                completed.set()

        with self._lock:
            self._close.require_open()
            if self._list_active:
                raise _in_use_error()
            self._list_active = True
            handle = self._handle
        request = None
        timed_out = False
        try:
            code, request = _native.storage_list_start(
                handle, kind, first, second, timezone, on_completed
            )
            _check_code(code)
            if not completed.wait(deadline):
                timed_out = True
                _check_code(_native.request_cancel(request))
                completed.wait()
            code, values = _native.request_result(request)
            if timed_out:
                raise _timeout_error()
            _check_code(code)
            return list(values)
        finally:
            try:
                if request is not None:
                    _destroy_with_barrier(request)
            finally:
                with self._lock:
                    self._list_active = False

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
    ) -> Replay:
        _check_process()
        _callable(on_time_changed, "on_time_changed")
        _callable(on_completed, "on_completed")
        _callable(on_error, "on_error")
        with self._lock:
            self._close.require_open()
            replay = Replay.__new__(Replay)
            replay._prepare(self, on_time_changed, on_completed, on_error)
            self._dependencies += 1
            try:
                code, handle = _native.replay_create(
                    self._handle,
                    _native_sink(replay._dispatcher),
                    on_time_changed is not None,
                )
                _check_code(code)
            except BaseException:
                self._dependencies -= 1
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
    ) -> ExportTask:
        _check_process()
        start = _datetime_ms(start_time, "start_time")
        end = _datetime_ms(end_time, "end_time")
        if start >= end:
            raise ValueError("start_time must be before end_time")
        video = _channel_id(video_channel_id, "video_channel_id")
        audio = -1 if audio_channel_id is None else _channel_id(audio_channel_id, "audio_channel_id")
        with self._lock:
            self._close.require_open()
            task = ExportTask.__new__(ExportTask)
            task._prepare(self)
            self._tasks += 1
            try:
                code, handle = _native.export_create(
                    self._handle,
                    start,
                    end,
                    video,
                    audio,
                    task._native_sink(),
                )
                _check_code(code)
            except BaseException:
                self._tasks -= 1
                task._parent_finished = True
                raise
            task._handle = handle
            return task

    def _finish_dependency(self) -> None:
        with self._lock:
            self._dependencies = max(0, self._dependencies - 1)

    def _finish_task(self) -> None:
        with self._lock:
            self._tasks = max(0, self._tasks - 1)

    def close(self) -> None:
        _check_process()
        with self._lock:
            generation = self._close.begin(
                preflight_in_use=bool(
                    self._list_active or self._dependencies or self._tasks
                )
            )
            if generation is None:
                return
        try:
            _retry_in_use(_native.close, self._handle)
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@final
class Replay:
    def __init__(self) -> None:
        raise TypeError("Replay values are created by CloudStorage.create_replay()")

    def _prepare(self, parent: CloudStorage, on_time_changed, on_completed, on_error) -> None:
        self._handle = None
        self._parent = parent
        self._on_time_changed = on_time_changed
        self._on_completed = on_completed
        self._on_error = on_error
        self._lock = threading.RLock()
        self._dispatcher = _Dispatcher(self._handle_event)
        self._speed = ReplaySpeed.X1
        self._active = False
        self._dependencies = 0
        self._tasks = 0
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
                with self._lock:
                    self._active = False
                if self._on_completed is not None:
                    self._on_completed()
            elif kind == "error":
                with self._lock:
                    self._active = False
                if self._on_error is not None:
                    code = int(values[0])
                    self._on_error(_error_from_code(code, _error_name(code)))

    def _operation(self, function, *arguments: object) -> None:
        _check_process()
        with self._lock:
            self._close.require_open()
            if _callback_owner() is self:
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
        start = _datetime_ms(start_time, "start_time")
        end = _datetime_ms(end_time, "end_time")
        if start >= end:
            raise ValueError("start_time must be before end_time")
        initial = start if initial_time is None else _datetime_ms(initial_time, "initial_time")
        if initial < start or initial >= end:
            raise ValueError("initial_time must be inside the playback range")
        with self._lock:
            self._close.require_open()
            _check_code(_native.replay_play(self._handle, start, end, initial))
            self._playback_range = (start, end)
            self._active = True
            self._native_stopped = False

    def pause(self) -> None:
        self._operation(_native.replay_pause)

    def resume(self) -> None:
        self._operation(_native.replay_resume)

    def seek(self, target: datetime) -> None:
        _check_process()
        target_ms = _datetime_ms(target, "target")
        with self._lock:
            self._close.require_open()
            if self._playback_range is not None:
                start, end = self._playback_range
                if target_ms < start or target_ms >= end:
                    raise ValueError("target must be inside the playback range")
            if _callback_owner() is self:
                _check_code(_native.replay_seek(self._handle, target_ms))
            else:
                _retry_in_use(_native.replay_seek, self._handle, target_ms)

    def set_speed(self, speed: ReplaySpeed) -> None:
        _check_process()
        if not isinstance(speed, ReplaySpeed):
            raise TypeError("speed must be ReplaySpeed")
        with self._lock:
            self._close.require_open()
            if _callback_owner() is self:
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
        with self._lock:
            self._close.require_open()
            code, value = _native.replay_get_speed(self._handle)
            _check_code(code)
            self._speed = _REPLAY_SPEED_FROM_NATIVE[int(value)]
            return self._speed

    @property
    def current_time(self) -> datetime | None:
        _check_process()
        with self._lock:
            self._close.require_open()
            code, present, value = _native.replay_current_time(self._handle)
        _check_code(code)
        return _datetime_from_ms(int(value)) if present else None

    def stop(self) -> None:
        _check_process()
        with self._lock:
            self._close.require_open()
            callback = _callback_owner()
            if callback is self or getattr(callback, "_bound", None) is self:
                raise _in_use_error()
            _retry_in_use(_native.replay_stop, self._handle)
            self._active = False
            self._native_stopped = True

    def start_recording(
        self, *, video_channel_id: int, audio_channel_id: int | None = None
    ) -> RecordingTask:
        _check_process()
        video = _channel_id(video_channel_id, "video_channel_id")
        audio = -1 if audio_channel_id is None else _channel_id(audio_channel_id, "audio_channel_id")
        with self._lock:
            self._close.require_open()
            task = RecordingTask._prepare(self)
            self._tasks += 1
            try:
                code, handle = _native.storage_recording_start(self._handle, video, audio)
                _check_code(code)
            except BaseException:
                self._tasks -= 1
                task._parent_finished = True
                raise
            task._handle = handle
            return task

    def _attach(self) -> object:
        _check_process()
        with self._lock:
            self._close.require_open()
            self._dependencies += 1
            return self._handle

    def _attach_failed(self) -> None:
        with self._lock:
            self._dependencies -= 1

    def _detach(self) -> None:
        with self._lock:
            self._dependencies = max(0, self._dependencies - 1)

    def _finish_task(self) -> None:
        with self._lock:
            self._tasks = max(0, self._tasks - 1)

    def close(self) -> None:
        _check_process()
        self._close_replay()

    def _close_replay(self) -> None:
        with self._lock:
            callback = _callback_owner()
            if callback is self or getattr(callback, "_bound", None) is self:
                raise _in_use_error()
            generation = self._close.begin(
                preflight_in_use=bool(self._dependencies or self._tasks)
            )
            if generation is None:
                return
        try:
            self._dispatcher.close()
            with self._lock:
                active = self._active
            if active and not self._native_stopped:
                _retry_in_use(_native.replay_stop, self._handle)
                with self._lock:
                    self._active = False
                    self._native_stopped = True
            _retry_in_use(_native.close, self._handle)
            if not self._parent_finished:
                self._parent._finish_dependency()
                self._parent_finished = True
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation)

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
                self._replay._finish_task()
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
            replay_handle = replay._attach()
            try:
                _check_code(
                    _native.output_attach_storage(self._handle, replay_handle, channel)
                )
            except BaseException:
                replay._attach_failed()
                raise
            self._bound = replay

    def detach(self) -> None:
        _check_process()
        with self._lock:
            self._close.require_open()
            if self._bound is None:
                _check_code(6029)
            bound = self._bound
            callback = _callback_owner()
            if callback is self or callback is bound:
                raise _in_use_error()
            _retry_in_use(self._native_detach)
            self._bound = None
            self._state = OutputState.IDLE
        assert bound is not None
        bound._detach()


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

    def _prepare(self, parent: CloudStorage) -> None:
        self._parent = parent
        self._handle = None
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._done = threading.Event()
        self._progress = 0.0
        self._terminal = False
        self._parent_finished = False
        self._file: tirtc.RecordingFile | None = None
        self._error: BaseException | None = None

    def _native_sink(self):
        reference = weakref.ref(self)

        def sink(kind: str, *values: object) -> None:
            task = reference()
            if task is None:
                return
            with task._lock:
                if kind == "completed" and not task._terminal:
                    code = int(values[0])
                    task._terminal = True
                    task._progress = 1.0 if code == 0 else task._progress
                    task._file = (
                        tirtc.RecordingFile._create(str(values[1]), int(values[2]))
                        if code == 0
                        else None
                    )
                    task._error = (
                        _error_from_code(code, _error_name(code)) if code else None
                    )
                    task._done.set()

        return sink

    @property
    def progress(self) -> float:
        _check_process()
        with self._lock:
            if self._close.status == "open" and not self._terminal and self._handle is not None:
                code, value = _native.export_progress(self._handle)
                _check_code(code)
                self._progress = max(self._progress, min(float(value), 1.0))
            return self._progress

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
            _destroy_with_barrier(self._handle)
            if not self._parent_finished:
                self._parent._finish_task()
                self._parent_finished = True
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, self._error)
        return self._result_or_error()

    def wait(self, *, timeout: float | None = None) -> tirtc.RecordingFile:
        _check_process()
        if not self._done.wait(_timeout(timeout)):
            raise _timeout_error()
        return self._finish()

    def stop(self) -> tirtc.RecordingFile:
        _check_process()
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
                        self._progress = 1.0 if code == 0 else self._progress
                        self._file = (
                            tirtc.RecordingFile._create(str(path), int(duration_ms))
                            if code == 0
                            else None
                        )
                        self._error = (
                            _error_from_code(code, _error_name(code)) if code else None
                        )
                        self._done.set()
            _destroy_with_barrier(self._handle)
            if not self._parent_finished:
                self._parent._finish_task()
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
    "CloudStorage",
    "EncodedAudioOutput",
    "EncodedVideoOutput",
    "ExportTask",
    "RecordingDay",
    "RecordingRange",
    "RecordingTask",
    "Replay",
    "ReplaySpeed",
    "VideoOutput",
    "initialize",
    "shutdown",
]
