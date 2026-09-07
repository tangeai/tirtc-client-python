from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import timedelta
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import tirtc


MAXIMUM_MEDIA_FILE_SIZE = 512 << 20
MINIMUM_RECORDING_SECONDS = 3.0
RESOURCE_CLOSE_TIMEOUT_SECONDS = 5.0


class Signals:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._counts: dict[str, int] = {}
        self._failure: BaseException | None = None

    def notify(self, name: str) -> None:
        with self._condition:
            self._counts[name] = self._counts.get(name, 0) + 1
            self._condition.notify_all()

    def fail(self, error: BaseException) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = error
            self._condition.notify_all()

    def snapshot(self, names: tuple[str, ...]) -> dict[str, int]:
        with self._condition:
            return {name: self._counts.get(name, 0) for name in names}

    def wait_after(
        self, names: tuple[str, ...], baseline: dict[str, int], deadline: float
    ) -> None:
        with self._condition:
            while any(
                self._counts.get(name, 0) <= baseline.get(name, 0) for name in names
            ):
                if self._failure is not None:
                    raise RuntimeError("media callback failed") from self._failure
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = [
                        name
                        for name in names
                        if self._counts.get(name, 0) <= baseline.get(name, 0)
                    ]
                    raise TimeoutError(f"timed out waiting for {', '.join(missing)}")
                self._condition.wait(remaining)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Receive RTC media with the public tirtc package."
    )
    parser.add_argument("--endpoint", default=None, help="optional TiRTC endpoint")
    parser.add_argument("--remote-id", required=True, help="remote device ID")
    parser.add_argument(
        "--cache-dir", required=True, type=Path, help="absolute writable SDK cache directory"
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="absolute application output directory"
    )
    parser.add_argument("--audio-stream-id", type=int, default=10)
    parser.add_argument("--video-stream-id", type=int, default=11)
    parser.add_argument("--timeout", type=float, default=90.0, help="overall timeout in seconds")
    args = parser.parse_args()
    if not args.cache_dir.is_absolute() or not args.output_dir.is_absolute():
        parser.error("--cache-dir and --output-dir must be absolute")
    if not 0 <= args.audio_stream_id <= 15 or not 0 <= args.video_stream_id <= 15:
        parser.error("stream IDs must be between 0 and 15")
    if args.audio_stream_id == args.video_stream_id:
        parser.error("audio and video stream IDs must differ")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def save_temporary_media(source: Path, destination: Path, signature: bytes, offset: int) -> None:
    size = source.stat().st_size
    if size <= offset + len(signature) or size > MAXIMUM_MEDIA_FILE_SIZE:
        raise RuntimeError(f"unexpected temporary media size: {source} ({size} bytes)")
    with source.open("rb") as stream:
        stream.seek(offset)
        if stream.read(len(signature)) != signature:
            raise RuntimeError(f"unexpected temporary media signature: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def close_when_idle(resource: object) -> None:
    deadline = time.monotonic() + RESOURCE_CLOSE_TIMEOUT_SECONDS
    while True:
        try:
            resource.close()
            return
        except tirtc.InUseError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def run() -> None:
    args = parse_args()
    app_id = os.environ.get("TIRTC_APP_ID", "")
    token = os.environ.get("TIRTC_TOKEN", "")
    if not app_id or not token:
        raise RuntimeError("TIRTC_APP_ID and TIRTC_TOKEN are required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.timeout
    signals = Signals()
    frame_names = ("audio", "video", "encoded_audio", "encoded_video")
    connected = threading.Event()
    command_received = threading.Event()
    message_received = threading.Event()

    def on_state(state: tirtc.ConnectionState, error: tirtc.TiRTCError | None) -> None:
        if error is not None:
            signals.fail(error)
        if state is tirtc.ConnectionState.CONNECTED:
            connected.set()

    def on_output_error(error: tirtc.TiRTCError) -> None:
        signals.fail(error)

    tirtc.initialize(app_id, args.cache_dir, endpoint=args.endpoint)
    try:
        with ExitStack() as stack:
            connection = tirtc.Connection(
                on_state_changed=on_state,
                on_command=lambda command_id, data: command_received.set(),
                on_stream_message=lambda stream_id, timestamp, data: message_received.set(),
            )
            stack.callback(close_when_idle, connection)
            audio = tirtc.AudioOutput(
                lambda frame: signals.notify("audio"), on_error=on_output_error
            )
            stack.callback(close_when_idle, audio)
            video = tirtc.VideoOutput(
                lambda frame: signals.notify("video"), on_error=on_output_error
            )
            stack.callback(close_when_idle, video)
            encoded_audio = tirtc.EncodedAudioOutput(
                lambda frame: signals.notify("encoded_audio"),
                on_error=on_output_error,
            )
            stack.callback(close_when_idle, encoded_audio)

            def on_encoded_video(frame: tirtc.EncodedVideoFrame) -> None:
                signals.notify("encoded_video")
                if frame.key_frame:
                    signals.notify("encoded_video_key")

            encoded_video = tirtc.EncodedVideoOutput(
                on_encoded_video, on_error=on_output_error
            )
            stack.callback(close_when_idle, encoded_video)
            audio.attach(connection, args.audio_stream_id)
            video.attach(connection, args.video_stream_id)
            encoded_audio.attach(connection, args.audio_stream_id)
            encoded_video.attach(connection, args.video_stream_id)
            connection.connect(args.remote_id, token)
            if not connected.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("timed out waiting for RTC connection")
            connection.subscribe_audio(args.audio_stream_id)
            connection.subscribe_video(args.video_stream_id)

            recording = connection.start_recording(
                video_stream_id=args.video_stream_id,
                audio_stream_id=args.audio_stream_id,
            )
            recording_ready_at = time.monotonic() + MINIMUM_RECORDING_SECONDS
            try:
                connection.request_video_keyframe(args.video_stream_id)
                signals.wait_after((*frame_names, "encoded_video_key"), {}, deadline)
                remaining = recording_ready_at - time.monotonic()
                if remaining > 0:
                    if time.monotonic() + remaining > deadline:
                        raise TimeoutError("timed out waiting for recordable media")
                    time.sleep(remaining)
                with recording.stop() as recording_file:
                    save_temporary_media(
                        recording_file.path,
                        args.output_dir / "rtc-recording.mp4",
                        b"ftyp",
                        4,
                    )
            except BaseException:
                try:
                    recording.stop().delete()
                except BaseException:
                    pass
                raise

            connection.send_command(0x2001, b"python-client-command")
            timestamp = timedelta(milliseconds=int(time.time() * 1000) & 0xFFFFFFFF)
            connection.send_stream_message(
                args.video_stream_id, timestamp, b"python-client-message"
            )
            connection.request_video_keyframe(args.video_stream_id)
            if not command_received.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("timed out waiting for remote command")
            if not message_received.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("timed out waiting for remote stream message")

            with video.take_snapshot() as snapshot:
                save_temporary_media(
                    snapshot.path, args.output_dir / "rtc-snapshot.jpg", b"\xff\xd8", 0
                )
            connection.unsubscribe_video(args.video_stream_id)
            connection.unsubscribe_audio(args.audio_stream_id)
            encoded_video.detach()
            encoded_audio.detach()
            video.detach()
            audio.detach()
            connection.disconnect()
    finally:
        tirtc.shutdown()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"tirtc RTC example failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
