from __future__ import annotations

from datetime import timedelta
from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import sys
import threading
import time
from typing import Self, final
import warnings

from . import _native
from . import _build_identity
from ._dispatch import _Dispatcher, _active_callback, _callback_owner, _native_sink
from ._errors import (
    TiRTCError,
    _error_from_code,
    _in_use_error,
    _timeout_error,
)
from ._lifecycle import _CloseCoordinator, _copy_error
from ._raw_dump import RawDump, normalize_raw_dump_ids
from ._runtime import (
    ClientOptions,
    _RuntimeLease,
    _acquire_rtc,
    _check_process,
    _error_name,
    _process_is_current,
    _upload_logs,
)
from ._util import (
    _buffer,
    _callable,
    _check_code,
    _connect_timeout_ms,
    _duration_ms,
    _duration_us,
    _enum,
    _integer,
    _nonempty_string,
    _retry_in_use,
    _retry_in_use_result,
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
    RawDumpArchive,
    RawDumpStopReason,
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


@dataclass(frozen=True, slots=True)
class RawDumpOptions:
    audio_stream_ids: tuple[int, ...] = ()
    video_stream_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        audio = normalize_raw_dump_ids(self.audio_stream_ids, "audio_stream_ids", 15)
        video = normalize_raw_dump_ids(self.video_stream_ids, "video_stream_ids", 15)
        if not audio and not video:
            raise ValueError("at least one audio or video stream must be selected")
        object.__setattr__(self, "audio_stream_ids", audio)
        object.__setattr__(self, "video_stream_ids", video)


def error_name(code: int) -> str:
    _check_process()
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
        _check_process()
        with self._lock:
            if self._deleted:
                return
            _check_code(_native.delete_media_file(str(self._path)))
            self._deleted = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.delete()


@final
class RecordingFile(_TemporaryMediaFile):
    @classmethod
    def _create(cls, path: str, duration_ms: int) -> RecordingFile:
        value = super()._create(path)
        value._duration = timedelta(milliseconds=duration_ms)
        return value

    @property
    def duration(self) -> timedelta:
        return self._duration


@final
class SnapshotFile(_TemporaryMediaFile):
    pass


@final
class RecordingTask:
    def __init__(self) -> None:
        raise TypeError("RecordingTask values are created by Connection.start_recording()")

    @classmethod
    def _prepare(cls, parent: Connection) -> RecordingTask:
        self = object.__new__(cls)
        self._handle = None
        self._parent = parent
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._result: RecordingFile | None = None
        self._error: BaseException | None = None
        self._stopped = False
        self._parent_finished = False
        return self

    @classmethod
    def _create(cls, handle: object, parent: Connection) -> RecordingTask:
        self = cls._prepare(parent)
        self._handle = handle
        return self

    def stop(self) -> RecordingFile:
        _check_process()
        if _callback_owner() is self._parent:
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
                    RecordingFile._create(str(path), int(duration_ms)) if not code else None
                )
                self._stopped = True
            _retry_in_use(_native.close, self._handle)
            if not self._parent_finished:
                self._parent._finish_task(self)
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


class _ConnectState:
    __slots__ = (
        "cancel_error",
        "cancel_finished",
        "cancelling",
        "deadline",
        "done",
        "error",
        "finished",
        "generation",
        "managed",
        "managed_handle",
        "native_started",
        "terminal_code",
    )

    def __init__(self, generation: int, *, managed: bool, deadline: float) -> None:
        self.generation = generation
        self.managed = managed
        self.deadline = deadline
        self.done = threading.Event()
        self.finished = threading.Event()
        self.native_started = threading.Event()
        self.error: BaseException | None = None
        self.terminal_code: int | None = None
        self.managed_handle: object | None = None
        self.cancelling = False
        self.cancel_error: BaseException | None = None
        self.cancel_finished = threading.Event()


def _rtc_callback_client(owner: object | None) -> Client | None:
    seen: set[int] = set()
    while owner is not None and id(owner) not in seen:
        seen.add(id(owner))
        if isinstance(owner, Client):
            return owner
        client = getattr(owner, "_client", None)
        if isinstance(client, Client):
            return client
        owner = getattr(owner, "_parent", None) or getattr(owner, "_bound", None)
    return None


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
            lease = _acquire_rtc(options, "external_token")
        else:
            lease = _acquire_rtc(
                options,
                "access_key",
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            )
        self._lease = lease
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._connections: set[Connection] = set()
        _build_identity.log("rtc", _native)
        _LOGGER.info("RTC Client created")

    @property
    def _mode(self) -> str:
        return self._lease.mode

    def create_connection(
        self,
        *,
        on_state_changed: Callable[[ConnectionState, TiRTCError | None], None] | None = None,
        on_command: Callable[[int, bytes], None] | None = None,
        on_stream_message: Callable[[int, timedelta, bytes], None] | None = None,
    ) -> Connection:
        _check_process()
        _callable(on_state_changed, "on_state_changed")
        _callable(on_command, "on_command")
        _callable(on_stream_message, "on_stream_message")
        with self._lock:
            self._close.require_open()
            connection = Connection._create(
                self,
                on_state_changed=on_state_changed,
                on_command=on_command,
                on_stream_message=on_stream_message,
            )
            self._connections.add(connection)
            return connection

    def _finish_connection(self, connection: Connection) -> None:
        with self._lock:
            self._connections.discard(connection)

    def upload_logs(self) -> str:
        _check_process()
        with self._lock:
            self._close.require_open()
            return _upload_logs(self._lease)

    def close(self) -> None:
        _check_process()
        with self._lock:
            if _rtc_callback_client(_callback_owner()) is self:
                raise _in_use_error()
            generation = self._close.begin()
            if generation is None:
                return
            connections = tuple(self._connections)
        try:
            child_error: BaseException | None = None
            for connection in connections:
                try:
                    connection._close_from_client()
                except BaseException as error:
                    if connection._close.status != "closed":
                        raise
                    if child_error is None:
                        child_error = error
            self._lease.close()
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        self._close.succeed(generation, child_error)
        _LOGGER.info("RTC Client closed")
        if child_error is not None:
            raise child_error

    def __del__(self) -> None:
        close = getattr(self, "_close", None)
        if close is None or close.status == "closed":
            return
        try:
            warnings.warn("unclosed TiRTC Client", ResourceWarning, stacklevel=2)
            if sys.is_finalizing() or not _process_is_current():
                return
            self.close()
        except BaseException:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@final
class Connection:
    def __init__(self) -> None:
        raise TypeError("Connection values are created by Client.create_connection()")

    @classmethod
    def _create(
        cls,
        client: Client,
        *,
        on_state_changed: Callable[[ConnectionState, TiRTCError | None], None] | None,
        on_command: Callable[[int, bytes], None] | None,
        on_stream_message: Callable[[int, timedelta, bytes], None] | None,
    ) -> Connection:
        _check_process()
        self = object.__new__(cls)
        self._client = client
        self._on_state_changed = on_state_changed
        self._on_command = on_command
        self._on_stream_message = on_stream_message
        self._state = ConnectionState.IDLE
        self._outputs: set[_OutputBase] = set()
        self._tasks: set[RecordingTask] = set()
        self._dumps: set[RawDump] = set()
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._disconnect_finished = False
        self._connect_generation = 0
        self._active_connect: _ConnectState | None = None
        self._dispatcher = _Dispatcher(self._handle_event)
        self._handle = None
        if client._mode == "access_key":
            code, handle = _native.rtc_client_create_connection(
                client._lease.handle,
                _native_sink(self._dispatcher),
                on_command is not None,
                on_stream_message is not None,
            )
        else:
            code, handle = _native.conn_create(
                _native_sink(self._dispatcher),
                on_command is not None,
                on_stream_message is not None,
            )
        _check_code(code)
        self._handle = handle
        _LOGGER.debug("connection created")
        return self

    def _handle_event(self, kind: str, *values: object) -> None:
        with _active_callback(self):
            if kind == "state":
                state = _CONNECTION_STATES[int(values[0])]
                code = int(values[1])
                with self._lock:
                    previous = self._state
                    attempt = self._active_connect
                    if attempt is not None:
                        terminal = attempt.terminal_code
                        if terminal is None or (
                            terminal == 0
                            and (code or state is ConnectionState.DISCONNECTED)
                        ):
                            self._state = state
                            if state is ConnectionState.CONNECTED and not code:
                                attempt.terminal_code = 0
                                attempt.done.set()
                            elif code or state is ConnectionState.DISCONNECTED:
                                attempt.terminal_code = code or 6012
                                attempt.done.set()
                        return
                    if previous is ConnectionState.CONNECTED and state is ConnectionState.CONNECTING:
                        return
                    changed = previous is not state
                    self._state = state
                if self._on_state_changed is not None and (changed or code):
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
        _check_process()
        with self._lock:
            self._close.require_open()
            return self._state

    def _native_operation(self, function, *arguments: object) -> None:
        _check_process()
        with self._lock:
            self._close.require_open()
            _check_code(function(self._handle, *arguments))

    def _managed_complete(self, attempt: _ConnectState, code: int) -> None:
        with self._lock:
            terminal = attempt.terminal_code
            if self._active_connect is attempt and (
                terminal is None or (terminal == 0 and code)
            ):
                attempt.terminal_code = int(code)
                attempt.done.set()

    def _begin_connect(self, expected_mode: str, timeout: float) -> tuple[_ConnectState, int]:
        _check_process()
        if _callback_owner() is not None:
            raise _in_use_error()
        timeout_ms = _connect_timeout_ms(timeout)
        with self._lock:
            self._close.require_open()
            if self._client._mode != expected_mode or self._active_connect is not None:
                raise _in_use_error()
            self._connect_generation += 1
            attempt = _ConnectState(
                self._connect_generation,
                managed=expected_mode == "access_key",
                deadline=time.monotonic() + timeout_ms / 1000.0,
            )
            self._active_connect = attempt
            self._state = ConnectionState.CONNECTING
            self._disconnect_finished = False
        return attempt, timeout_ms

    def _finish_connect(self, attempt: _ConnectState) -> None:
        self._dispatcher.barrier()
        with self._lock:
            if self._active_connect is attempt:
                self._active_connect = None
            code = attempt.terminal_code
            if code == 0 and attempt.error is None:
                self._state = ConnectionState.CONNECTED
            else:
                self._state = ConnectionState.DISCONNECTED
                self._disconnect_finished = True
            attempt.finished.set()

    def _wait_connect(self, attempt: _ConnectState, timeout_ms: int) -> None:
        if not attempt.done.wait(max(0.0, attempt.deadline - time.monotonic())):
            self._cancel_connect(attempt, _timeout_error())
        try:
            with self._lock:
                cancelling = attempt.cancelling
            if cancelling:
                attempt.cancel_finished.wait()
            if attempt.cancel_error is not None:
                raise _copy_error(attempt.cancel_error)
            if attempt.managed_handle is not None:
                _retry_in_use(_native.close, attempt.managed_handle)
            self._finish_connect(attempt)
        except BaseException:
            attempt.finished.set()
            raise
        if attempt.error is not None:
            raise _copy_error(attempt.error)
        code = attempt.terminal_code
        if code:
            raise _error_from_code(code, _error_name(code))

    def _cancel_connect(self, attempt: _ConnectState, error: BaseException) -> None:
        perform = False
        wait = False
        with self._lock:
            if self._active_connect is not attempt:
                return
            if attempt.terminal_code is not None:
                return
            if attempt.cancelling:
                wait = True
            else:
                attempt.error = error
                attempt.cancelling = True
                perform = True
        if wait:
            attempt.cancel_finished.wait()
            if attempt.cancel_error is not None:
                raise _copy_error(attempt.cancel_error)
            return
        if perform:
            try:
                attempt.native_started.wait()
                if attempt.finished.is_set():
                    return
                if attempt.managed:
                    if attempt.managed_handle is None:
                        return
                    _check_code(_native.connect_attempt_cancel(attempt.managed_handle))
                    attempt.done.wait()
                    with self._lock:
                        connected = attempt.terminal_code == 0
                    if connected:
                        _check_code(_native.conn_disconnect(self._handle))
                        self._dispatcher.barrier()
                else:
                    _check_code(_native.conn_disconnect(self._handle))
                    self._dispatcher.barrier()
                    with self._lock:
                        if attempt.terminal_code is None:
                            attempt.terminal_code = 6115
                        attempt.done.set()
                with self._lock:
                    self._disconnect_finished = True
            except BaseException as cancel_error:
                attempt.cancel_error = cancel_error
                raise
            finally:
                attempt.cancel_finished.set()

    def connect(
        self,
        device_id: str,
        *,
        token: str | None = None,
        timeout: float = 30,
    ) -> None:
        device_id = _nonempty_string(device_id, "device_id")
        mode = self._client._mode
        if mode == "access_key":
            if token is not None:
                raise ValueError("token cannot be used with an Access Key Client")
            access_token = None
        else:
            if token is None:
                raise ValueError("token is required when the Client has no access keys")
            access_token = _nonempty_string(token, "token")
        attempt, timeout_ms = self._begin_connect(mode, timeout)
        try:
            if mode == "access_key":
                code, handle = _native.conn_connect_managed(
                    self._handle,
                    device_id,
                    timeout_ms,
                    lambda code: self._managed_complete(attempt, int(code)),
                )
                _check_code(code)
                attempt.managed_handle = handle
            else:
                assert access_token is not None
                _check_code(_native.conn_connect(self._handle, device_id, access_token))
            attempt.native_started.set()
        except BaseException:
            attempt.native_started.set()
            with self._lock:
                if self._active_connect is attempt:
                    self._active_connect = None
                    self._state = ConnectionState.IDLE
            attempt.finished.set()
            raise
        self._wait_connect(attempt, timeout_ms)
        _LOGGER.info("connection connected")

    def disconnect(self) -> None:
        _check_process()
        callback = _callback_owner()
        if callback is self or getattr(callback, "_bound", None) is self:
            raise _in_use_error()
        with self._lock:
            self._close.require_open()
            attempt = self._active_connect
        if attempt is not None:
            self._cancel_connect(
                attempt, _error_from_code(6115, _error_name(6115))
            )
            attempt.finished.wait()
            if attempt.error is None and attempt.terminal_code == 0:
                _check_code(_native.conn_disconnect(self._handle))
                self._dispatcher.barrier()
        else:
            _check_code(_native.conn_disconnect(self._handle))
            self._dispatcher.barrier()
        with self._lock:
            self._state = ConnectionState.DISCONNECTED
            self._disconnect_finished = True
        _LOGGER.info("connection disconnected")

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
        _check_process()
        video = _stream_id(video_stream_id, "video_stream_id")
        audio = -1 if audio_stream_id is None else _stream_id(audio_stream_id, "audio_stream_id")
        if audio == video:
            raise ValueError("audio_stream_id must differ from video_stream_id")
        with self._lock:
            self._close.require_open()
            task = RecordingTask._prepare(self)
            self._tasks.add(task)
            try:
                code, handle = _native.rtc_recording_start(self._handle, video, audio)
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
        callback = _callback_owner()
        if callback is self or getattr(callback, "_bound", None) is self:
            raise _in_use_error()
        with self._lock:
            self._close.require_open()
            code, handle = _native.rtc_raw_dump_start(
                self._handle,
                bytes(options.audio_stream_ids),
                bytes(options.video_stream_ids),
            )
            _check_code(code)
            dump = RawDump._create(self, handle)
            self._dumps.add(dump)
            return dump

    def _finish_raw_dump(self, dump: RawDump) -> None:
        with self._lock:
            self._dumps.discard(dump)

    def _finish_task(self, task: RecordingTask) -> None:
        with self._lock:
            self._tasks.discard(task)

    def _attach(self, output: _OutputBase) -> object:
        _check_process()
        with self._lock:
            self._close.require_open()
            self._outputs.add(output)
            return self._handle

    def _attach_failed(self, output: _OutputBase) -> None:
        with self._lock:
            self._outputs.discard(output)

    def _detach(self, output: _OutputBase) -> None:
        with self._lock:
            self._outputs.discard(output)

    def close(self) -> None:
        self._close_connection()

    def _close_from_client(self) -> None:
        self._close_connection()

    def _close_connection(self) -> None:
        _check_process()
        with self._lock:
            callback = _callback_owner()
            if callback is self or getattr(callback, "_bound", None) is self:
                raise _in_use_error()
            generation = self._close.begin()
            if generation is None:
                return
            outputs = tuple(self._outputs)
            tasks = tuple(self._tasks)
            dumps = tuple(self._dumps)
            attempt = self._active_connect
        try:
            if attempt is not None:
                self._cancel_connect(
                    attempt, _error_from_code(6115, _error_name(6115))
                )
                attempt.finished.wait()
            for output in outputs:
                output._detach_from_parent(self)
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
            self._dispatcher.close()
            with self._lock:
                state = self._state
            if (
                not self._disconnect_finished
                and state in {ConnectionState.CONNECTING, ConnectionState.CONNECTED}
            ):
                _check_code(_native.conn_disconnect(self._handle))
                self._disconnect_finished = True
            _retry_in_use(_native.close, self._handle)
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        with self._lock:
            self._state = ConnectionState.DISCONNECTED
        self._close.succeed(generation, raw_dump_error)
        self._client._finish_connection(self)
        _LOGGER.debug("connection closed")
        if raw_dump_error is not None:
            raise raw_dump_error

    def __del__(self) -> None:
        close = getattr(self, "_close", None)
        if close is None or close.status == "closed" or getattr(self, "_handle", None) is None:
            return
        try:
            warnings.warn("unclosed TiRTC Connection", ResourceWarning, stacklevel=2)
            if sys.is_finalizing() or not _process_is_current():
                return
            self._close_connection()
        except BaseException:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _OutputBase:
    _native_kind: str

    def _native_detach(self) -> int:
        return int(_native.output_detach_rtc(self._handle))

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
        _check_process()
        if not callable(on_frame):
            raise TypeError("on_frame must be callable")
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
        self._bound: Connection | None = None
        self._lock = threading.RLock()
        self._close = _CloseCoordinator(self._lock)
        self._native_detached = False
        self._detaching = False
        frame_capacity = 2 if self._native_kind in {"video", "encoded_video"} else 64
        self._dispatcher = _Dispatcher(
            self._handle_event, frame_capacity=frame_capacity
        )
        self._handle = None
        code, handle = _native.output_create(
            self._native_kind,
            _native_sink(self._dispatcher),
            on_state_changed is not None,
            on_error is not None,
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
            elif kind == "audio_frame":
                self._on_frame(self._audio_frame(values))
            elif kind == "video_frame":
                self._on_frame(self._video_frame(values))
            elif kind == "encoded_audio_frame":
                self._on_frame(self._encoded_audio_frame(values))
            elif kind == "encoded_video_frame":
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
        _check_process()
        if not isinstance(connection, Connection):
            raise TypeError("connection must be Connection")
        stream = _stream_id(stream_id)
        with self._lock:
            self._close.require_open()
            if self._bound is not None:
                raise _in_use_error()
            connection_handle = connection._attach(self)
            try:
                _check_code(
                    _native.output_attach_rtc(self._handle, connection_handle, stream)
                )
            except BaseException:
                connection._attach_failed(self)
                raise
            self._bound = connection
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

    def _detach_from_parent(self, parent: Connection) -> None:
        with self._lock:
            if self._bound is not parent:
                return
            if self._detaching:
                raise _in_use_error()
            callback = _callback_owner()
            if callback is self or callback is parent:
                raise _in_use_error()
            _retry_in_use(self._native_detach)
            self._detaching = True
            self._state = OutputState.IDLE
        self._dispatcher.barrier()
        with self._lock:
            self._bound = None
            self._detaching = False
        parent._detach(self)

    @property
    def state(self) -> OutputState:
        _check_process()
        with self._lock:
            self._close.require_open()
            code, state = _native.output_state(self._handle)
            _check_code(code)
            self._state = _OUTPUT_STATES[int(state)]
            return self._state

    def close(self) -> None:
        _check_process()
        self._close_output()

    def _close_output(self) -> None:
        with self._lock:
            if self._detaching:
                raise _in_use_error()
            callback = _callback_owner()
            if callback is self or (self._bound is not None and callback is self._bound):
                raise _in_use_error()
            generation = self._close.begin()
            if generation is None:
                return
            bound = self._bound
        try:
            self._dispatcher.close()
            if bound is not None and not self._native_detached:
                _retry_in_use(self._native_detach)
                self._native_detached = True
            _retry_in_use(_native.close, self._handle)
        except BaseException as error:
            self._close.fail(generation, error)
            raise
        with self._lock:
            self._bound = None
            self._state = OutputState.IDLE
        if bound is not None:
            bound._detach(self)
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
            warnings.warn(
                f"unclosed TiRTC {type(self).__name__}", ResourceWarning, stacklevel=2
            )
            if sys.is_finalizing() or not _process_is_current():
                return
            self._dispatcher.discard()
            self._close_output()
        except BaseException:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@final
class AudioOutput(_OutputBase):
    _native_kind = "audio"

    def __init__(
        self,
        on_frame: Callable[[AudioFrame], None],
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


@final
class VideoOutput(_OutputBase):
    _native_kind = "video"

    def __init__(
        self,
        on_frame: Callable[[VideoFrame], None],
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
        _check_process()
        with self._lock:
            self._close.require_open()
            code, path = _native.output_snapshot(self._handle)
        _check_code(code)
        return SnapshotFile._create(str(path))


@final
class EncodedAudioOutput(_OutputBase):
    _native_kind = "encoded_audio"

    def __init__(
        self,
        on_frame: Callable[[EncodedAudioFrame], None],
        *,
        on_state_changed: Callable[[OutputState], None] | None = None,
        on_error: Callable[[TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)


@final
class EncodedVideoOutput(_OutputBase):
    _native_kind = "encoded_video"

    def __init__(
        self,
        on_frame: Callable[[EncodedVideoFrame], None],
        *,
        on_state_changed: Callable[[OutputState], None] | None = None,
        on_error: Callable[[TiRTCError], None] | None = None,
    ) -> None:
        self._initialize_output(on_frame, on_state_changed, on_error)
