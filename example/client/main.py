from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from datetime import timedelta
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import tirtc


MAXIMUM_MEDIA_FILE_SIZE = 512 << 20
MINIMUM_RECORDING_SECONDS = 3.0


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
                    raise self._failure
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
    parser.add_argument(
        "--auth-mode",
        choices=("access-key", "external-token"),
        required=True,
        help="credential owner used to create the TiRTC client",
    )
    parser.add_argument("--endpoint", default=None, help="optional TiRTC endpoint")
    parser.add_argument("--device-id", required=True, help="remote device ID")
    parser.add_argument(
        "--cache-dir", required=True, type=Path, help="absolute writable SDK cache directory"
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="absolute application output directory"
    )
    parser.add_argument("--audio-stream-id", type=int)
    parser.add_argument("--video-stream-id", type=int, action="append")
    parser.add_argument("--no-receive-audio", action="store_true")
    parser.add_argument("--no-receive-video", action="store_true")
    parser.add_argument(
        "--raw-dump",
        action="store_true",
        help="capture the selected encoded inputs and return the diagnostic ZIP",
    )
    parser.add_argument("--raw-dump-seconds", type=float, default=10.0)
    parser.add_argument("--raw-dump-copy-to", type=Path)
    parser.add_argument("--upload-logs", action="store_true")
    parser.add_argument(
        "--case-id",
        choices=("smoke.raw-dump-upload", "integration.raw-dump-recovery"),
        help="run one registered raw dump RTC case through this public Example",
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="overall timeout in seconds")
    args = parser.parse_args()
    if args.case_id is not None:
        args.raw_dump = True
        args.upload_logs = True
    if not args.cache_dir.is_absolute() or not args.output_dir.is_absolute():
        parser.error("--cache-dir and --output-dir must be absolute")
    if args.no_receive_audio and args.audio_stream_id is not None:
        parser.error("--no-receive-audio conflicts with --audio-stream-id")
    if args.no_receive_video and args.video_stream_id is not None:
        parser.error("--no-receive-video conflicts with --video-stream-id")
    args.audio_stream_id = None if args.no_receive_audio else (
        10 if args.audio_stream_id is None else args.audio_stream_id
    )
    args.video_stream_ids = [] if args.no_receive_video else (
        [11] if args.video_stream_id is None else args.video_stream_id
    )
    if args.audio_stream_id is not None and not 0 <= args.audio_stream_id <= 15:
        parser.error("stream IDs must be between 0 and 15")
    if len(args.video_stream_ids) > 3 or any(
        not 0 <= stream_id <= 15 for stream_id in args.video_stream_ids
    ):
        parser.error("provide at most three video stream IDs between 0 and 15")
    if len(set(args.video_stream_ids)) != len(args.video_stream_ids):
        parser.error("video stream IDs must be distinct")
    if args.audio_stream_id in args.video_stream_ids:
        parser.error("audio and video stream IDs must differ")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if not math.isfinite(args.raw_dump_seconds) or not 0 < args.raw_dump_seconds <= 300:
        parser.error("--raw-dump-seconds must be finite and from 0 through 300 seconds")
    if args.raw_dump_copy_to is not None and (
        not args.raw_dump or not args.raw_dump_copy_to.is_absolute()
    ):
        parser.error("--raw-dump-copy-to requires --raw-dump and an absolute path")
    if args.raw_dump and args.audio_stream_id is None and not args.video_stream_ids:
        parser.error("--raw-dump requires at least one selected audio or video stream")
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


def run() -> None:
    args = parse_args()
    app_id = os.environ.get("TIRTC_APP_ID", "")
    if not app_id:
        raise RuntimeError("TIRTC_APP_ID is required")
    if args.auth_mode == "external-token":
        token = os.environ.get("TIRTC_TOKEN", "")
        if not token:
            raise RuntimeError("TIRTC_TOKEN is required for external-token mode")
        access_key_id = ""
        access_key_secret = ""
    else:
        token = ""
        access_key_id = os.environ.get("TIRTC_ACCESS_KEY_ID", "")
        access_key_secret = os.environ.get("TIRTC_SECRET_KEY_ID", "")
        if not access_key_id or not access_key_secret:
            raise RuntimeError(
                "TIRTC_ACCESS_KEY_ID and TIRTC_SECRET_KEY_ID are required "
                "for access-key mode"
            )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.timeout
    signals = Signals()
    command_received = threading.Event()
    message_received = threading.Event()

    def on_state(state: tirtc.ConnectionState, error: tirtc.TiRTCError | None) -> None:
        del state
        if error is not None:
            signals.fail(error)

    def on_output_error(error: tirtc.TiRTCError) -> None:
        signals.fail(error)

    options = tirtc.ClientOptions(
        app_id=app_id,
        cache_dir=args.cache_dir,
        endpoint=args.endpoint,
    )
    with ExitStack() as stack:
        if args.auth_mode == "external-token":
            client = stack.enter_context(tirtc.Client(options))
        else:
            client = stack.enter_context(
                tirtc.Client(
                    options,
                    access_key_id=access_key_id,
                    access_key_secret=access_key_secret,
                )
            )
        connection = stack.enter_context(
            client.create_connection(
                on_state_changed=on_state,
                on_command=lambda command_id, data: command_received.set(),
                on_stream_message=lambda stream_id, timestamp, data: message_received.set(),
            )
        )
        audio: tirtc.AudioOutput | None = None
        encoded_audio: tirtc.EncodedAudioOutput | None = None
        if args.audio_stream_id is not None:
            audio = tirtc.AudioOutput(
                lambda frame: signals.notify("audio"), on_error=on_output_error
            )
            stack.callback(audio.close)
            encoded_audio = tirtc.EncodedAudioOutput(
                lambda frame: signals.notify("encoded_audio"),
                on_error=on_output_error,
            )
            stack.callback(encoded_audio.close)
            audio.attach(connection, args.audio_stream_id)
            encoded_audio.attach(connection, args.audio_stream_id)

        video_outputs: list[tuple[int, tirtc.VideoOutput, tirtc.EncodedVideoOutput]] = []
        for stream_id in args.video_stream_ids:
            decoded_name = f"video:{stream_id}"
            encoded_name = f"encoded_video:{stream_id}"
            key_name = f"encoded_video_key:{stream_id}"

            def on_video(
                frame: tirtc.VideoFrame, name: str = decoded_name
            ) -> None:
                del frame
                signals.notify(name)

            video = tirtc.VideoOutput(
                on_video,
                on_error=on_output_error,
            )
            stack.callback(video.close)

            def on_encoded_video(
                frame: tirtc.EncodedVideoFrame,
                name: str = encoded_name,
                key: str = key_name,
            ) -> None:
                signals.notify(name)
                if frame.key_frame:
                    signals.notify(key)

            encoded_video = tirtc.EncodedVideoOutput(
                on_encoded_video, on_error=on_output_error
            )
            stack.callback(encoded_video.close)
            video.attach(connection, stream_id)
            encoded_video.attach(connection, stream_id)
            video_outputs.append((stream_id, video, encoded_video))
        if args.auth_mode == "external-token":
            connection.connect(
                args.device_id,
                token=token,
                timeout=min(120.0, max(0.001, deadline - time.monotonic())),
            )
        else:
            connection.connect(
                args.device_id,
                timeout=min(120.0, max(0.001, deadline - time.monotonic())),
            )
        if connection.state is not tirtc.ConnectionState.CONNECTED:
            raise RuntimeError("connect returned before the connection reached CONNECTED")
        required_frames: list[str] = []
        if args.audio_stream_id is not None:
            connection.subscribe_audio(args.audio_stream_id)
            required_frames.extend(("audio", "encoded_audio"))
        for stream_id, _, _ in video_outputs:
            connection.subscribe_video(stream_id)
            connection.request_video_keyframe(stream_id)
            required_frames.extend((f"video:{stream_id}", f"encoded_video:{stream_id}"))
        dump: tirtc.RawDump | None = None
        capture_deadline = 0.0
        if args.raw_dump:
            raw_dump_options = tirtc.RawDumpOptions(
                audio_stream_ids=(
                    () if args.audio_stream_id is None else (args.audio_stream_id,)
                ),
                video_stream_ids=tuple(args.video_stream_ids),
            )
            dump = connection.start_raw_dump(raw_dump_options)
            stack.callback(dump.close)
            if args.case_id == "integration.raw-dump-recovery":
                try:
                    connection.start_raw_dump(raw_dump_options)
                except tirtc.InUseError:
                    pass
                else:
                    raise RuntimeError("duplicate raw dump start was accepted")
            capture_deadline = time.monotonic() + args.raw_dump_seconds
            if capture_deadline > deadline:
                raise TimeoutError("raw dump duration exceeds the remaining example timeout")
        if required_frames:
            signals.wait_after(tuple(required_frames), {}, deadline)

        if dump is not None:
            time.sleep(max(0, capture_deadline - time.monotonic()))
            archive = dump.stop()
            if not archive.capture_id or not archive.path.is_file():
                raise RuntimeError("raw dump did not return a readable archive")
            if archive.path.stat().st_size != archive.size:
                raise RuntimeError("raw dump archive size does not match its metadata")
            if args.raw_dump_copy_to is not None:
                args.raw_dump_copy_to.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive.path, args.raw_dump_copy_to)
            if args.case_id == "integration.raw-dump-recovery" and dump.stop() is not archive:
                raise RuntimeError("repeated raw dump stop changed the terminal result")
            print(json.dumps({
                "raw_dump": {
                    "capture_id": archive.capture_id,
                    "path": str(archive.path),
                    "size": archive.size,
                    "sha256": archive.sha256,
                    "captured_duration_ms": int(archive.captured_duration.total_seconds() * 1000),
                    "capture_complete": archive.capture_complete,
                    "stop_reason": archive.stop_reason.value,
                    "unsaved_packet_count": archive.unsaved_packet_count,
                    "unsaved_byte_count": archive.unsaved_byte_count,
                    "empty": archive.empty,
                }
            }, sort_keys=True))

        for stream_id, video, _ in video_outputs:
            recording = connection.start_recording(
                video_stream_id=stream_id,
                audio_stream_id=args.audio_stream_id,
            )
            recording_ready_at = time.monotonic() + MINIMUM_RECORDING_SECONDS
            try:
                key_name = f"encoded_video_key:{stream_id}"
                baseline = signals.snapshot((key_name, f"encoded_video:{stream_id}"))
                connection.request_video_keyframe(stream_id)
                signals.wait_after((key_name,), baseline, deadline)
                remaining = recording_ready_at - time.monotonic()
                if remaining > 0:
                    if time.monotonic() + remaining > deadline:
                        raise TimeoutError("timed out waiting for recordable media")
                    time.sleep(remaining)
                with recording.stop() as recording_file:
                    save_temporary_media(
                        recording_file.path,
                        args.output_dir / f"rtc-recording-stream-{stream_id}.mp4",
                        b"ftyp",
                        4,
                    )
            except BaseException:
                try:
                    recording.stop().delete()
                except BaseException:
                    pass
                raise
            with video.take_snapshot() as snapshot:
                save_temporary_media(
                    snapshot.path,
                    args.output_dir / f"rtc-snapshot-stream-{stream_id}.jpg",
                    b"\xff\xd8",
                    0,
                )
            print(f"consumed video stream {stream_id}")

        connection.send_command(0x2001, b"python-client-command")
        timestamp = timedelta(milliseconds=int(time.time() * 1000) & 0xFFFFFFFF)
        message_stream_id = args.video_stream_ids[0] if args.video_stream_ids else (
            args.audio_stream_id if args.audio_stream_id is not None else 0
        )
        connection.send_stream_message(message_stream_id, timestamp, b"python-client-message")
        if not command_received.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError("timed out waiting for remote command")
        if not message_received.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError("timed out waiting for remote stream message")
        if args.upload_logs:
            print(json.dumps({"log_id": client.upload_logs()}, sort_keys=True))

        for stream_id, video, encoded_video in reversed(video_outputs):
            connection.unsubscribe_video(stream_id)
            encoded_video.detach()
            video.detach()
        if args.audio_stream_id is not None:
            connection.unsubscribe_audio(args.audio_stream_id)
            assert encoded_audio is not None and audio is not None
            encoded_audio.detach()
            audio.detach()
        connection.disconnect()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"tirtc RTC example failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
