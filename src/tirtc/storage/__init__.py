from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
import os
import threading
import time
from typing import Self
import weakref
from zoneinfo import ZoneInfo

import tirtc
from tirtc import _native
from tirtc._core import _OutputBase
from tirtc._dispatch import _Dispatcher, _active_callback, _callback_owner, _native_sink
from tirtc._errors import (
    _closed_error,
    _error_from_code,
    _in_use_error,
    _timeout_error,
)
from tirtc._runtime import _error_name, _initialize, _shutdown
from tirtc._util import (
    _aware_datetime,
    _callable,
    _channel_id,
    _check_code,
    _datetime_from_ms,
    _datetime_ms,
    _integer,
    _nonempty_string,
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


def _timeout(value: float | None) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("timeout must be float or None")
    value = float(value)
    if value <= 0:
        raise ValueError("timeout must be positive")
    return value


def _destroy_with_barrier(handle: object) -> None:
    deadline = time.monotonic() + 2.0
    delay = 0.001
    while True:
        code = int(_native.close(handle))
        if code != 6026:
            _check_code(code)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _check_code(code)
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.02)


class CloudStorage:
    def __init__(self, token: str) -> None:
        token = _nonempty_string(token, "token")
        code, handle = _native.storage_create(token)
        _check_code(code)
        self._handle = handle
        self._closed = False
        self._list_active = False
        self._dependencies = 0
        self._tasks = 0
        self._lock = threading.RLock()

    def update_token(self, token: str) -> None:
        token = _nonempty_string(token, "token")
        with self._lock:
            if self._closed:
                raise _closed_error()
            _check_code(_native.storage_update_token(self._handle, token))

    def _list(
        self,
        kind: str,
        first: object,
        second: object,
        timezone: str | None,
        timeout: float | None,
    ) -> list[tuple[object, ...]]:
        deadline = _timeout(timeout)
        completed = threading.Event()

        def on_completed(event: str) -> None:
            if event == "completed":
                completed.set()

        with self._lock:
            if self._closed:
                raise _closed_error()
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
        _callable(on_time_changed, "on_time_changed")
        _callable(on_completed, "on_completed")
        _callable(on_error, "on_error")
        with self._lock:
            if self._closed:
                raise _closed_error()
            replay = Replay.__new__(Replay)
            replay._prepare(self, on_time_changed, on_completed, on_error)
            code, handle = _native.replay_create(
                self._handle, _native_sink(replay._dispatcher)
            )
            _check_code(code)
            replay._handle = handle
            self._dependencies += 1
            return replay

    def export_recording(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        video_channel_id: int,
        audio_channel_id: int | None = None,
    ) -> ExportTask:
        start = _datetime_ms(start_time, "start_time")
        end = _datetime_ms(end_time, "end_time")
        if start >= end:
            raise ValueError("start_time must be before end_time")
        video = _channel_id(video_channel_id, "video_channel_id")
        audio = -1 if audio_channel_id is None else _channel_id(audio_channel_id, "audio_channel_id")
        if audio == video:
            raise ValueError("audio_channel_id must differ from video_channel_id")
        with self._lock:
            if self._closed:
                raise _closed_error()
            task = ExportTask.__new__(ExportTask)
            task._prepare(self)
            code, handle = _native.export_create(
                self._handle,
                start,
                end,
                video,
                audio,
                task._native_sink(),
            )
            _check_code(code)
            task._handle = handle
            self._tasks += 1
            return task

    def _finish_dependency(self) -> None:
        with self._lock:
            self._dependencies = max(0, self._dependencies - 1)

    def _finish_task(self) -> None:
        with self._lock:
            self._tasks = max(0, self._tasks - 1)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._list_active or self._dependencies or self._tasks:
                raise _in_use_error()
            _check_code(_native.close(self._handle))
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class Replay:
    def __init__(self) -> None:
        raise TypeError("Replay values are created by CloudStorage.create_replay()")

    def _prepare(self, parent: CloudStorage, on_time_changed, on_completed, on_error) -> None:
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
        self._closed = False
        self._closing = False
        self._close_condition = threading.Condition(self._lock)

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
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            _check_code(function(self._handle, *arguments))

    def play(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        initial_time: datetime | None = None,
    ) -> None:
        start = _datetime_ms(start_time, "start_time")
        end = _datetime_ms(end_time, "end_time")
        if start >= end:
            raise ValueError("start_time must be before end_time")
        initial = start if initial_time is None else _datetime_ms(initial_time, "initial_time")
        if initial < start or initial > end:
            raise ValueError("initial_time must be inside the playback range")
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            self._active = True
            try:
                _check_code(_native.replay_play(self._handle, start, end, initial))
            except BaseException:
                self._active = False
                raise

    def pause(self) -> None:
        self._operation(_native.replay_pause)

    def resume(self) -> None:
        self._operation(_native.replay_resume)

    def seek(self, target: datetime) -> None:
        self._operation(_native.replay_seek, _datetime_ms(target, "target"))

    def set_speed(self, speed: ReplaySpeed) -> None:
        if not isinstance(speed, ReplaySpeed):
            raise TypeError("speed must be ReplaySpeed")
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            _check_code(
                _native.replay_set_speed(self._handle, _REPLAY_SPEED_TO_NATIVE[speed])
            )
            self._speed = speed

    @property
    def speed(self) -> ReplaySpeed:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            code, value = _native.replay_get_speed(self._handle)
            _check_code(code)
            self._speed = _REPLAY_SPEED_FROM_NATIVE[int(value)]
            return self._speed

    @property
    def current_time(self) -> datetime | None:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            code, present, value = _native.replay_current_time(self._handle)
        _check_code(code)
        return _datetime_from_ms(int(value)) if present else None

    def stop(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            _check_code(_native.replay_stop(self._handle))
            self._active = False

    def start_recording(
        self, *, video_channel_id: int, audio_channel_id: int | None = None
    ) -> RecordingTask:
        video = _channel_id(video_channel_id, "video_channel_id")
        audio = -1 if audio_channel_id is None else _channel_id(audio_channel_id, "audio_channel_id")
        if audio == video:
            raise ValueError("audio_channel_id must differ from video_channel_id")
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            code, handle = _native.storage_recording_start(self._handle, video, audio)
            _check_code(code)
            self._tasks += 1
            return RecordingTask._create(handle, self)

    def _attach(self) -> object:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
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
        with self._close_condition:
            callback = _callback_owner()
            if (
                callback is self
                or getattr(callback, "_bound", None) is self
            ):
                raise _in_use_error()
            while self._closing:
                self._close_condition.wait()
            if self._closed:
                return
            if (
                self._dispatcher.in_callback
                or self._dependencies
                or self._tasks
            ):
                raise _in_use_error()
            self._closing = True
            active = self._active
        try:
            if active:
                _check_code(_native.replay_stop(self._handle))
                with self._lock:
                    self._active = False
            _check_code(_native.close(self._handle))
            self._dispatcher.close()
        except BaseException:
            with self._close_condition:
                self._closing = False
                self._close_condition.notify_all()
            raise
        with self._close_condition:
            self._closed = True
            self._closing = False
            self._close_condition.notify_all()
        self._parent._finish_dependency()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class RecordingTask:
    def __init__(self) -> None:
        raise TypeError("RecordingTask values are created by Replay.start_recording()")

    @classmethod
    def _create(cls, handle: object, replay: Replay) -> RecordingTask:
        self = object.__new__(cls)
        self._handle = handle
        self._replay = replay
        self._lock = threading.Lock()
        self._result: tirtc.RecordingFile | None = None
        self._error: BaseException | None = None
        self._finished = False
        return self

    def stop(self) -> tirtc.RecordingFile:
        with self._lock:
            if self._finished:
                if self._error is not None:
                    raise self._error
                assert self._result is not None
                return self._result
            code, path, duration_ms, destroyed = _native.recording_stop(self._handle)
            error = _error_from_code(code, _error_name(code)) if code else None
            if destroyed:
                self._finished = True
                self._result = (
                    tirtc.RecordingFile._create(str(path), int(duration_ms)) if not code else None
                )
                self._error = error
                self._replay._finish_task()
            if error is not None:
                raise error
            if self._result is not None:
                return self._result
            return tirtc.RecordingFile._create(str(path), int(duration_ms))


class _StorageOutputBase(_OutputBase):
    _bound: Replay | None

    def attach(self, replay: Replay, channel_id: int) -> None:
        if not isinstance(replay, Replay):
            raise TypeError("replay must be Replay")
        channel = _channel_id(channel_id, "channel_id")
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
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
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            if self._bound is None:
                _check_code(6029)
            bound = self._bound
            _check_code(_native.output_detach_storage(self._handle))
            self._bound = None
            self._state = OutputState.IDLE
        assert bound is not None
        bound._detach()

    def close(self) -> None:
        with self._close_condition:
            callback = _callback_owner()
            if (
                callback is self
                or (self._bound is not None and callback is self._bound)
            ):
                raise _in_use_error()
            while self._closing:
                self._close_condition.wait()
            if self._closed:
                return
            if self._dispatcher.in_callback:
                raise _in_use_error()
            self._closing = True
            bound = self._bound
        try:
            if bound is not None:
                _check_code(_native.output_detach_storage(self._handle))
                with self._lock:
                    self._bound = None
                    self._state = OutputState.IDLE
                bound._detach()
            _check_code(_native.close(self._handle))
            self._dispatcher.close()
        except BaseException:
            with self._close_condition:
                self._closing = False
                self._close_condition.notify_all()
            raise
        with self._close_condition:
            self._closed = True
            self._closing = False
            self._close_condition.notify_all()


class AudioOutput(_StorageOutputBase):
    _native_kind = "audio"

    def __init__(
        self,
        on_frame: Callable[[tirtc.AudioFrame], None] | None = None,
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


class VideoOutput(_StorageOutputBase):
    _native_kind = "video"

    def __init__(
        self,
        on_frame: Callable[[tirtc.VideoFrame], None] | None = None,
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)

    def take_snapshot(self) -> tirtc.SnapshotFile:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            code, path = _native.output_snapshot(self._handle)
        _check_code(code)
        return tirtc.SnapshotFile._create(str(path))


class EncodedAudioOutput(_StorageOutputBase):
    _native_kind = "encoded_audio"

    def __init__(
        self,
        on_frame: Callable[[tirtc.EncodedAudioFrame], None] | None = None,
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


class EncodedVideoOutput(_StorageOutputBase):
    _native_kind = "encoded_video"

    def __init__(
        self,
        on_frame: Callable[[tirtc.EncodedVideoFrame], None] | None = None,
        *,
        on_state_changed: Callable[[tirtc.OutputState], None] | None = None,
        on_error: Callable[[tirtc.TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


class ExportTask:
    def __init__(self) -> None:
        raise TypeError("ExportTask values are created by CloudStorage.export_recording()")

    def _prepare(self, parent: CloudStorage) -> None:
        self._parent = parent
        self._handle = None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop_lock = threading.Lock()
        self._stopping = False
        self._done = threading.Event()
        self._progress = 0.0
        self._terminal = False
        self._finalized = False
        self._file: tirtc.RecordingFile | None = None
        self._error: BaseException | None = None

    def _native_sink(self):
        reference = weakref.ref(self)

        def sink(kind: str, *values: object) -> None:
            task = reference()
            if task is None:
                return
            with task._lock:
                if kind == "progress" and not task._terminal:
                    task._progress = max(task._progress, min(float(values[0]), 1.0))
                elif kind == "completed" and not task._terminal:
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
        with self._lock:
            if not self._terminal and self._handle is not None:
                code, value = _native.export_progress(self._handle)
                _check_code(code)
                self._progress = max(self._progress, min(float(value), 1.0))
            return self._progress

    def _finish(self) -> tirtc.RecordingFile:
        with self._condition:
            while self._stopping:
                self._condition.wait()
            if not self._finalized:
                _destroy_with_barrier(self._handle)
                self._finalized = True
                self._parent._finish_task()
            error = self._error
            result = self._file
        if error is not None:
            raise error
        assert result is not None
        return result

    def wait(self, *, timeout: float | None = None) -> tirtc.RecordingFile:
        if not self._done.wait(_timeout(timeout)):
            raise _timeout_error()
        return self._finish()

    def stop(self) -> tirtc.RecordingFile:
        with self._stop_lock:
            with self._condition:
                terminal = self._terminal
                if not terminal:
                    self._stopping = True
            if not terminal:
                try:
                    code, path, duration_ms = _native.export_stop(self._handle)
                    with self._condition:
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
                finally:
                    with self._condition:
                        self._stopping = False
                        self._condition.notify_all()
        return self._finish()


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
