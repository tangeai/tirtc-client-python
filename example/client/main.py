from __future__ import annotations

import argparse
import hashlib
from contextlib import ExitStack
import json
from datetime import timedelta
import math
import os
from pathlib import Path
import shutil
import secrets
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


class MessageProof:
    """Exact application bytes; the private stream fixture is explicitly selected."""
    PREFIX = b"tirtc-stream-v1\0"

    def __init__(self, stream_ids: list[int], fixture: bool) -> None:
        nonce = secrets.token_bytes(16)
        self.fixture = fixture
        self._condition = threading.Condition()
        self._failure: str | None = None
        self.commands = [(command, nonce + bytes((index, 0, 255, 128)))
                         for index, command in enumerate((0x2001, 0xFFFFFFFF))]
        self.streams = [(stream_ids[index % len(stream_ids)], timestamp,
                         self.PREFIX + b"\0" + nonce + index.to_bytes(4, "big") + b"\0\xff\x80")
                        for index, timestamp in enumerate((0, 0xFFFFFFFF))]
        self.received_commands: list[tuple[int, bytes]] = []
        self.received_streams: list[tuple[int, int, bytes]] = []

    def command(self, command: int, data: bytes) -> None:
        with self._condition:
            index = len(self.received_commands)
            if index >= len(self.commands) or (command, data) != self.commands[index]:
                self._failure = "command ID/order/binary mismatch"
            self.received_commands.append((command, data))
            self._condition.notify_all()

    def stream(self, stream: int, timestamp: timedelta, data: bytes) -> None:
        if not self.fixture:
            return
        # The normal peer's periodic epoch message is unrelated to this exchange.
        if not data.startswith(self.PREFIX):
            return
        with self._condition:
            index = len(self.received_streams)
            actual = (stream, round(timestamp.total_seconds() * 1000), data)
            if index >= len(self.streams):
                self._failure = "duplicate stream response"
            else:
                stream_id, stamp, request = self.streams[index]
                expected = (stream_id, stamp, self.PREFIX + b"\1" + request[len(self.PREFIX) + 1:])
                if actual != expected:
                    self._failure = "stream ID/timestamp/order/binary mismatch"
            self.received_streams.append(actual)
            self._condition.notify_all()

    def wait(self, deadline: float) -> None:
        with self._condition:
            while True:
                if self._failure:
                    raise RuntimeError(self._failure)
                if len(self.received_commands) == len(self.commands) and (
                    not self.fixture or len(self.received_streams) == len(self.streams)):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("message acceptance did not become exact remote delivery")
                self._condition.wait(remaining)

    def evidence(self) -> dict[str, object]:
        self.wait(time.monotonic())
        return {"status": "passed", "peer_mode": "stream-echo" if self.fixture else "device",
                "commands": [{"id": item[0], "bytes_hex": item[1].hex()} for item in self.received_commands],
                "streams": [{"id": item[0], "timestamp_ms": item[1], "bytes_hex": item[2].hex()}
                            for item in self.received_streams]}


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
    parser.add_argument("--verify-message-echo", action="store_true", help=argparse.SUPPRESS)
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


def save_temporary_media(media: tirtc.RecordingFile | tirtc.SnapshotFile, destination: Path, signature: bytes, offset: int) -> None:
    source = media.path
    size = source.stat().st_size
    if size <= offset + len(signature) or size > MAXIMUM_MEDIA_FILE_SIZE:
        raise RuntimeError(f"unexpected temporary media size: {source} ({size} bytes)")
    with source.open("rb") as stream:
        stream.seek(offset)
        if stream.read(len(signature)) != signature:
            raise RuntimeError(f"unexpected temporary media signature: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    digest = hashlib.sha256()
    with destination.open("rb") as saved:
        for chunk in iter(lambda: saved.read(1024 * 1024), b""):
            digest.update(chunk)
    media.delete()
    if source.exists():
        raise RuntimeError("SDK temporary media remains after delete")
    destination.with_suffix(destination.suffix + ".receipt.json").write_text(json.dumps({
        "source_path": str(source), "deleted": True, "sha256": digest.hexdigest(),
        "bytes": size, "saved_name": destination.name,
    }, sort_keys=True) + "\n", encoding="utf-8")


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
    messages = MessageProof(args.video_stream_ids or [args.audio_stream_id or 0], args.verify_message_echo)

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
                on_command=messages.command,
                on_stream_message=messages.stream,
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
            try:
                early_snapshot = video.take_snapshot()
            except tirtc.NoFrameError:
                pass
            else:
                early_snapshot.delete()
                raise RuntimeError("zero-frame snapshot unexpectedly succeeded")
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
        route_evidence: dict[str, object] = {"zero_frame_rejected": [item[0] for item in video_outputs]}
        if len(video_outputs) >= 2:
            first_id, first_video, first_encoded = video_outputs[0]
            second_id = video_outputs[1][0]
            route_names = (f"video:{first_id}", f"encoded_video:{first_id}",
                           f"video:{second_id}", f"encoded_video:{second_id}")
            common_before = signals.snapshot(route_names)
            signals.wait_after(route_names, common_before, deadline)
            common_after = signals.snapshot(route_names)
            first_encoded.detach()
            first_video.detach()
            isolated_before = signals.snapshot(route_names)
            signals.wait_after(route_names[2:], isolated_before, deadline)
            isolated_after = signals.snapshot(route_names)
            if any(isolated_after[name] != isolated_before[name] for name in route_names[:2]):
                raise RuntimeError("detached route continued to deliver callbacks")
            first_video.attach(connection, first_id)
            first_encoded.attach(connection, first_id)
            restored_before = signals.snapshot(route_names)
            connection.request_video_keyframe(first_id)
            signals.wait_after(route_names, restored_before, deadline)
            route_evidence.update(status="passed", ids=[first_id, second_id],
                common_before=common_before, common_after=common_after,
                isolated_before=isolated_before, isolated_after=isolated_after,
                restored_before=restored_before, restored_after=signals.snapshot(route_names))
        (args.output_dir / "route-proof.json").write_text(json.dumps(route_evidence, sort_keys=True) + "\n")

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
                        recording_file,
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
                    snapshot,
                    args.output_dir / f"rtc-snapshot-stream-{stream_id}.jpg",
                    b"\xff\xd8",
                    0,
                )
            print(f"consumed video stream {stream_id}")

        for command_id, payload in messages.commands:
            connection.send_command(command_id, payload)
        if messages.fixture:
            for stream_id, stamp, payload in messages.streams:
                connection.send_stream_message(stream_id, timedelta(milliseconds=stamp), payload)
        messages.wait(deadline)
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
    (args.output_dir / "message-proof.json").write_text(json.dumps(messages.evidence(), sort_keys=True) + "\n")


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"tirtc RTC example failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
