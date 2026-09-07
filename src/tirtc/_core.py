from __future__ import annotations

from datetime import timedelta
from collections.abc import Callable
import logging
import os
from pathlib import Path
import threading
from typing import Self

from . import _native
from ._dispatch import _Dispatcher, _active_callback, _callback_owner, _native_sink
from ._errors import (
    InUseError,
    TiRTCError,
    _closed_error,
    _error_from_code,
    _in_use_error,
)
from ._runtime import _error_name, _initialize, _shutdown
from ._util import (
    _buffer,
    _callable,
    _check_code,
    _duration_ms,
    _duration_us,
    _enum,
    _integer,
    _nonempty_string,
    _stream_id,
)
from ._values import (
    AudioBitstreamFormat,
    AudioCodec,
    AudioFrame,
    AudioProcessingLevel,
    AudioSampleFormat,
    ConnectionState,
    EncodedAudioFrame,
    EncodedVideoFrame,
    OutputBufferOptions,
    OutputBufferStrategy,
    OutputState,
    PixelFormat,
    VideoBitstreamFormat,
    VideoCodec,
    VideoDecoderPreference,
    VideoFrame,
    VideoPlane,
    _AUDIO_BITSTREAM_FORMATS,
    _AUDIO_CODECS,
    _AUDIO_SAMPLE_FORMATS,
    _CONNECTION_STATES,
    _OUTPUT_STATES,
    _PIXEL_FORMATS,
    _VIDEO_BITSTREAM_FORMATS,
    _VIDEO_CODECS_BY_NATIVE,
    _value,
)


_LOGGER = logging.getLogger("tirtc")


def initialize(
    app_id: str,
    cache_dir: str | os.PathLike[str],
    *,
    endpoint: str | None = None,
    console_log_enabled: bool = False,
) -> None:
    _initialize("rtc", app_id, cache_dir, endpoint, console_log_enabled)
    _LOGGER.info("RTC initialized")


def shutdown() -> None:
    _shutdown("rtc")
    _LOGGER.info("RTC shut down")


def upload_logs() -> str:
    code, log_id = _native.upload_logs()
    _check_code(code)
    return str(log_id)


def error_name(code: int) -> str:
    code = _integer(code, "code", minimum=-(2**31), maximum=2**31 - 1)
    return _error_name(code)


class _TemporaryMediaFile:
    def __init__(self) -> None:
        raise TypeError(f"{type(self).__name__} values are created by the SDK")

    @classmethod
    def _create(cls, path: str):
        value = object.__new__(cls)
        value._path = Path(path)
        value._deleted = False
        value._lock = threading.Lock()
        return value

    @property
    def path(self) -> Path:
        return self._path

    def delete(self) -> None:
        with self._lock:
            if self._deleted:
                return
            _check_code(_native.delete_media_file(str(self._path)))
            self._deleted = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.delete()


class RecordingFile(_TemporaryMediaFile):
    @classmethod
    def _create(cls, path: str, duration_ms: int) -> RecordingFile:
        value = super()._create(path)
        value._duration = timedelta(milliseconds=duration_ms)
        return value

    @property
    def duration(self) -> timedelta:
        return self._duration


class SnapshotFile(_TemporaryMediaFile):
    pass


class RecordingTask:
    def __init__(self) -> None:
        raise TypeError("RecordingTask values are created by Connection.start_recording()")

    @classmethod
    def _create(cls, handle: object, parent: Connection) -> RecordingTask:
        self = object.__new__(cls)
        self._handle = handle
        self._parent = parent
        self._lock = threading.Lock()
        self._result: RecordingFile | None = None
        self._error: BaseException | None = None
        self._finished = False
        return self

    def stop(self) -> RecordingFile:
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
                self._result = RecordingFile._create(str(path), int(duration_ms)) if not code else None
                self._error = error
                self._parent._finish_task()
            if error is not None:
                raise error
            if self._result is not None:
                return self._result
            return RecordingFile._create(str(path), int(duration_ms))


class Connection:
    def __init__(
        self,
        *,
        on_state_changed: Callable[[ConnectionState, TiRTCError | None], None] | None = None,
        on_command: Callable[[int, bytes], None] | None = None,
        on_stream_message: Callable[[int, timedelta, bytes], None] | None = None,
    ) -> None:
        _callable(on_state_changed, "on_state_changed")
        _callable(on_command, "on_command")
        _callable(on_stream_message, "on_stream_message")
        self._on_state_changed = on_state_changed
        self._on_command = on_command
        self._on_stream_message = on_stream_message
        self._state = ConnectionState.IDLE
        self._closed = False
        self._closing = False
        self._dependencies = 0
        self._tasks = 0
        self._lock = threading.RLock()
        self._close_condition = threading.Condition(self._lock)
        self._dispatcher = _Dispatcher(self._handle_event)
        code, handle = _native.conn_create(_native_sink(self._dispatcher))
        _check_code(code)
        self._handle = handle
        _LOGGER.debug("connection created")

    def _handle_event(self, kind: str, *values: object) -> None:
        with _active_callback(self):
            if kind == "state":
                state = _CONNECTION_STATES[int(values[0])]
                code = int(values[1])
                with self._lock:
                    self._state = state
                if self._on_state_changed is not None:
                    error = _error_from_code(code, _error_name(code)) if code else None
                    self._on_state_changed(state, error)
            elif kind == "command" and self._on_command is not None:
                self._on_command(int(values[0]), bytes(values[1]))
            elif kind == "message" and self._on_stream_message is not None:
                self._on_stream_message(
                    int(values[0]), timedelta(milliseconds=int(values[1])), bytes(values[2])
                )

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    def _native_operation(self, function, *arguments: object) -> None:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            _check_code(function(self._handle, *arguments))

    def connect(self, remote_id: str, token: str) -> None:
        remote_id = _nonempty_string(remote_id, "remote_id")
        token = _nonempty_string(token, "token")
        self._native_operation(_native.conn_connect, remote_id, token)
        _LOGGER.info("connection connect requested")

    def disconnect(self) -> None:
        self._native_operation(_native.conn_disconnect)
        _LOGGER.info("connection disconnect requested")

    def send_command(self, command_id: int, data: bytes | bytearray | memoryview) -> None:
        command_id = _integer(command_id, "command_id", minimum=0x2001, maximum=2**32 - 1)
        payload = _buffer(data)
        self._native_operation(_native.conn_send_command, command_id, payload)

    def send_stream_message(
        self,
        stream_id: int,
        timestamp: timedelta,
        data: bytes | bytearray | memoryview,
    ) -> None:
        stream_id = _stream_id(stream_id)
        timestamp_ms = _duration_ms(timestamp, "timestamp")
        if timestamp_ms < 0 or timestamp_ms > 2**32 - 1:
            raise ValueError("timestamp is outside the supported range")
        self._native_operation(
            _native.conn_send_message, stream_id, timestamp_ms, _buffer(data)
        )

    def subscribe_audio(self, stream_id: int) -> None:
        self._native_operation(_native.conn_subscribe_audio, _stream_id(stream_id))

    def unsubscribe_audio(self, stream_id: int) -> None:
        self._native_operation(_native.conn_unsubscribe_audio, _stream_id(stream_id))

    def subscribe_video(self, stream_id: int) -> None:
        self._native_operation(_native.conn_subscribe_video, _stream_id(stream_id))

    def unsubscribe_video(self, stream_id: int) -> None:
        self._native_operation(_native.conn_unsubscribe_video, _stream_id(stream_id))

    def request_video_keyframe(self, stream_id: int) -> None:
        self._native_operation(_native.conn_request_video_keyframe, _stream_id(stream_id))

    def start_recording(
        self, *, video_stream_id: int, audio_stream_id: int | None = None
    ) -> RecordingTask:
        video = _stream_id(video_stream_id, "video_stream_id")
        audio = -1 if audio_stream_id is None else _stream_id(audio_stream_id, "audio_stream_id")
        if audio == video:
            raise ValueError("audio_stream_id must differ from video_stream_id")
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            code, handle = _native.rtc_recording_start(self._handle, video, audio)
            _check_code(code)
            self._tasks += 1
            return RecordingTask._create(handle, self)

    def _finish_task(self) -> None:
        with self._lock:
            self._tasks = max(0, self._tasks - 1)

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
            state = self._state
        try:
            if state in {ConnectionState.CONNECTING, ConnectionState.CONNECTED}:
                _check_code(_native.conn_disconnect(self._handle))
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
            self._state = ConnectionState.DISCONNECTED
            self._close_condition.notify_all()
        _LOGGER.debug("connection closed")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _OutputBase:
    _native_kind: str

    def _initialize_output(
        self,
        on_frame,
        on_state_changed,
        on_error,
        *,
        agc_level: AudioProcessingLevel = AudioProcessingLevel.DISABLED,
        ans_level: AudioProcessingLevel = AudioProcessingLevel.DISABLED,
        decoder_preference: VideoDecoderPreference = VideoDecoderPreference.AUTO,
        buffer: OutputBufferOptions = OutputBufferOptions(),
    ) -> None:
        _callable(on_frame, "on_frame")
        _callable(on_state_changed, "on_state_changed")
        _callable(on_error, "on_error")
        _enum(agc_level, AudioProcessingLevel, "agc_level")
        _enum(ans_level, AudioProcessingLevel, "ans_level")
        _enum(decoder_preference, VideoDecoderPreference, "decoder_preference")
        if not isinstance(buffer, OutputBufferOptions):
            raise TypeError("buffer must be OutputBufferOptions")
        _enum(buffer.strategy, OutputBufferStrategy, "buffer.strategy")
        watermark_ms = -1
        if buffer.max_buffer_watermark is not None:
            watermark_ms = _duration_ms(
                buffer.max_buffer_watermark, "buffer.max_buffer_watermark"
            )
            if watermark_ms <= 0 or watermark_ms > 2**31 - 1:
                raise ValueError("buffer.max_buffer_watermark must be positive")
        self._on_frame = on_frame
        self._on_state_changed = on_state_changed
        self._on_error = on_error
        self._state = OutputState.IDLE
        self._closed = False
        self._closing = False
        self._bound: Connection | None = None
        self._lock = threading.RLock()
        self._close_condition = threading.Condition(self._lock)
        self._dispatcher = _Dispatcher(self._handle_event)
        code, handle = _native.output_create(
            self._native_kind,
            _native_sink(self._dispatcher),
            list(AudioProcessingLevel).index(agc_level),
            list(AudioProcessingLevel).index(ans_level),
            list(VideoDecoderPreference).index(decoder_preference),
            list(OutputBufferStrategy).index(buffer.strategy),
            watermark_ms,
        )
        _check_code(code)
        self._handle = handle

    def _handle_event(self, kind: str, *values: object) -> None:
        with _active_callback(self):
            if kind == "state":
                state = _OUTPUT_STATES[int(values[0])]
                with self._lock:
                    self._state = state
                if self._on_state_changed is not None:
                    self._on_state_changed(state)
            elif kind == "error" and self._on_error is not None:
                code = int(values[0])
                self._on_error(_error_from_code(code, _error_name(code)))
            elif kind == "audio_frame" and self._on_frame is not None:
                self._on_frame(self._audio_frame(values))
            elif kind == "video_frame" and self._on_frame is not None:
                self._on_frame(self._video_frame(values))
            elif kind == "encoded_audio_frame" and self._on_frame is not None:
                self._on_frame(self._encoded_audio_frame(values))
            elif kind == "encoded_video_frame" and self._on_frame is not None:
                self._on_frame(self._encoded_video_frame(values))

    @staticmethod
    def _audio_frame(values: tuple[object, ...]) -> AudioFrame:
        from ._util import _datetime_from_us

        return _value(
            AudioFrame,
            data=values[0],
            pts=timedelta(microseconds=int(values[1])),
            source_time=_datetime_from_us(int(values[2]), bool(values[3])),
            sample_format=_AUDIO_SAMPLE_FORMATS[int(values[4])],
            sample_rate_hz=int(values[5]),
            channels=int(values[6]),
            samples_per_channel=int(values[7]),
            discontinuity=bool(values[8]),
        )

    @staticmethod
    def _video_frame(values: tuple[object, ...]) -> VideoFrame:
        from ._util import _datetime_from_us

        planes = tuple(
            _value(VideoPlane, stride=int(stride), data=data)
            for stride, data in values[0]
        )
        return _value(
            VideoFrame,
            pts=timedelta(microseconds=int(values[1])),
            source_time=_datetime_from_us(int(values[2]), bool(values[3])),
            pixel_format=_PIXEL_FORMATS[int(values[4])],
            width=int(values[5]),
            height=int(values[6]),
            planes=planes,
            discontinuity=bool(values[7]),
        )

    @staticmethod
    def _encoded_audio_frame(values: tuple[object, ...]) -> EncodedAudioFrame:
        from ._util import _datetime_from_us

        return _value(
            EncodedAudioFrame,
            data=values[0],
            codec_config=values[1],
            pts=timedelta(microseconds=int(values[2])),
            source_time=_datetime_from_us(int(values[3]), bool(values[4])),
            codec=_AUDIO_CODECS[int(values[5])],
            bitstream_format=_AUDIO_BITSTREAM_FORMATS[int(values[6])],
            sample_rate_hz=int(values[7]),
            channels=int(values[8]),
            discontinuity=bool(values[9]),
        )

    @staticmethod
    def _encoded_video_frame(values: tuple[object, ...]) -> EncodedVideoFrame:
        from ._util import _datetime_from_us

        return _value(
            EncodedVideoFrame,
            data=values[0],
            codec_config=values[1],
            pts=timedelta(microseconds=int(values[2])),
            source_time=_datetime_from_us(int(values[3]), bool(values[4])),
            codec=_VIDEO_CODECS_BY_NATIVE[int(values[5])],
            bitstream_format=_VIDEO_BITSTREAM_FORMATS[int(values[6])],
            width=int(values[7]),
            height=int(values[8]),
            key_frame=bool(values[9]),
            discontinuity=bool(values[10]),
        )

    def attach(self, connection: Connection, stream_id: int) -> None:
        if not isinstance(connection, Connection):
            raise TypeError("connection must be Connection")
        stream = _stream_id(stream_id)
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            if self._bound is not None:
                raise _in_use_error()
            connection_handle = connection._attach()
            try:
                _check_code(
                    _native.output_attach_rtc(self._handle, connection_handle, stream)
                )
            except BaseException:
                connection._attach_failed()
                raise
            self._bound = connection

    def detach(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            if self._bound is None:
                _check_code(6029)
            bound = self._bound
            _check_code(_native.output_detach_rtc(self._handle))
            self._bound = None
            self._state = OutputState.IDLE
        assert bound is not None
        bound._detach()

    @property
    def state(self) -> OutputState:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            code, state = _native.output_state(self._handle)
            _check_code(code)
            self._state = _OUTPUT_STATES[int(state)]
            return self._state

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
                _check_code(_native.output_detach_rtc(self._handle))
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class AudioOutput(_OutputBase):
    _native_kind = "audio"

    def __init__(
        self,
        on_frame: Callable[[AudioFrame], None] | None = None,
        *,
        on_state_changed: Callable[[OutputState], None] | None = None,
        on_error: Callable[[TiRTCError], None] | None = None,
        agc_level: AudioProcessingLevel = AudioProcessingLevel.DISABLED,
        ans_level: AudioProcessingLevel = AudioProcessingLevel.DISABLED,
        buffer: OutputBufferOptions = OutputBufferOptions(),
    ) -> None:
        self._initialize_output(
            on_frame,
            on_state_changed,
            on_error,
            agc_level=agc_level,
            ans_level=ans_level,
            buffer=buffer,
        )


class VideoOutput(_OutputBase):
    _native_kind = "video"

    def __init__(
        self,
        on_frame: Callable[[VideoFrame], None] | None = None,
        *,
        on_state_changed: Callable[[OutputState], None] | None = None,
        on_error: Callable[[TiRTCError], None] | None = None,
        decoder_preference: VideoDecoderPreference = VideoDecoderPreference.AUTO,
        buffer: OutputBufferOptions = OutputBufferOptions(),
    ) -> None:
        self._initialize_output(
            on_frame,
            on_state_changed,
            on_error,
            decoder_preference=decoder_preference,
            buffer=buffer,
        )

    def take_snapshot(self) -> SnapshotFile:
        with self._lock:
            if self._closed or self._closing:
                raise _closed_error()
            code, path = _native.output_snapshot(self._handle)
        _check_code(code)
        return SnapshotFile._create(str(path))


class EncodedAudioOutput(_OutputBase):
    _native_kind = "encoded_audio"

    def __init__(
        self,
        on_frame: Callable[[EncodedAudioFrame], None] | None = None,
        *,
        on_state_changed: Callable[[OutputState], None] | None = None,
        on_error: Callable[[TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


class EncodedVideoOutput(_OutputBase):
    _native_kind = "encoded_video"

    def __init__(
        self,
        on_frame: Callable[[EncodedVideoFrame], None] | None = None,
        *,
        on_state_changed: Callable[[OutputState], None] | None = None,
        on_error: Callable[[TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)
