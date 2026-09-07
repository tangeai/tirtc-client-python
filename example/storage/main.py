from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from zoneinfo import ZoneInfo

import tirtc
import tirtc.storage as storage


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
                    raise RuntimeError("playback output failed") from self._failure
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
        description="Query and replay Ti Cloud Storage with the public tirtc.storage package."
    )
    parser.add_argument("--endpoint", default=None, help="optional Ti Cloud Storage endpoint")
    parser.add_argument(
        "--cache-dir", required=True, type=Path, help="absolute writable SDK cache directory"
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="absolute application output directory"
    )
    parser.add_argument("--start-ms", required=True, type=int, help="query start, Unix ms")
    parser.add_argument("--end-ms", required=True, type=int, help="query end, Unix ms")
    parser.add_argument("--audio-channel-id", type=int, default=0)
    parser.add_argument("--video-channel-id", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    if not args.cache_dir.is_absolute() or not args.output_dir.is_absolute():
        parser.error("--cache-dir and --output-dir must be absolute")
    if args.start_ms < 0 or args.start_ms >= args.end_ms:
        parser.error("--start-ms must be non-negative and earlier than --end-ms")
    if not 0 <= args.audio_channel_id <= 255 or not 0 <= args.video_channel_id <= 255:
        parser.error("channel IDs must be between 0 and 255")
    if args.audio_channel_id == args.video_channel_id:
        parser.error("audio and video channel IDs must differ")
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


def retry_while_in_use(
    name: str, operation: Callable[[], None], deadline: float
) -> None:
    while True:
        try:
            operation()
            return
        except tirtc.InUseError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} remained in use") from error
            time.sleep(0.01)


def close_when_idle(resource: object) -> None:
    retry_while_in_use(
        f"{type(resource).__name__}.close",
        resource.close,
        time.monotonic() + RESOURCE_CLOSE_TIMEOUT_SECONDS,
    )


def refresh_token(cloud: storage.CloudStorage) -> None:
    refreshed = os.environ.get("TI_CLOUD_STORAGE_REFRESHED_ACCESS_TOKEN", "")
    if not refreshed:
        raise RuntimeError(
            "token expired; set TI_CLOUD_STORAGE_REFRESHED_ACCESS_TOKEN and retry"
        )
    cloud.update_token(refreshed)


def run() -> None:
    args = parse_args()
    app_id = os.environ.get("TI_CLOUD_STORAGE_APP_ID", "")
    token = os.environ.get("TI_CLOUD_STORAGE_ACCESS_TOKEN", "")
    if not app_id or not token:
        raise RuntimeError(
            "TI_CLOUD_STORAGE_APP_ID and TI_CLOUD_STORAGE_ACCESS_TOKEN are required"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    start = datetime.fromtimestamp(args.start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(args.end_ms / 1000, timezone.utc)
    deadline = time.monotonic() + args.timeout
    signals = Signals()
    frame_names = ("audio", "video", "encoded_audio", "encoded_video")
    terminal = threading.Event()
    terminal_error: list[tirtc.TiRTCError] = []

    storage.initialize(app_id, args.cache_dir, endpoint=args.endpoint)
    try:
        with storage.CloudStorage(token) as cloud:
            shanghai = ZoneInfo("Asia/Shanghai")
            try:
                cloud.list_recording_days(
                    start.astimezone(shanghai).date(),
                    end.astimezone(shanghai).date(),
                    timezone=shanghai,
                    timeout=max(0.001, deadline - time.monotonic()),
                )
                ranges = cloud.list_recordings(
                    start, end, timeout=max(0.001, deadline - time.monotonic())
                )
            except tirtc.TokenExpiredError:
                refresh_token(cloud)
                cloud.list_recording_days(
                    start.astimezone(shanghai).date(),
                    end.astimezone(shanghai).date(),
                    timezone=shanghai,
                    timeout=max(0.001, deadline - time.monotonic()),
                )
                ranges = cloud.list_recordings(
                    start, end, timeout=max(0.001, deadline - time.monotonic())
                )
            if not ranges:
                raise RuntimeError("no recording is available in the requested window")
            selected = max(ranges, key=lambda item: (item.end_time, item.start_time))

            def on_completed() -> None:
                terminal.set()

            def on_replay_error(error: tirtc.TiRTCError) -> None:
                terminal_error.append(error)
                terminal.set()

            def on_output_error(error: tirtc.TiRTCError) -> None:
                signals.fail(error)

            with ExitStack() as stack:
                replay = cloud.create_replay(
                    on_completed=on_completed, on_error=on_replay_error
                )
                stack.callback(close_when_idle, replay)
                audio = storage.AudioOutput(
                    lambda frame: signals.notify("audio"), on_error=on_output_error
                )
                stack.callback(close_when_idle, audio)
                video = storage.VideoOutput(
                    lambda frame: signals.notify("video"), on_error=on_output_error
                )
                stack.callback(close_when_idle, video)
                encoded_audio = storage.EncodedAudioOutput(
                    lambda frame: signals.notify("encoded_audio"),
                    on_error=on_output_error,
                )
                stack.callback(close_when_idle, encoded_audio)

                def on_encoded_video(frame: tirtc.EncodedVideoFrame) -> None:
                    signals.notify("encoded_video")
                    if frame.key_frame:
                        signals.notify("encoded_video_key")

                encoded_video = storage.EncodedVideoOutput(
                    on_encoded_video, on_error=on_output_error
                )
                stack.callback(close_when_idle, encoded_video)
                audio.attach(replay, args.audio_channel_id)
                video.attach(replay, args.video_channel_id)
                encoded_audio.attach(replay, args.audio_channel_id)
                encoded_video.attach(replay, args.video_channel_id)

                replay.play(selected.start_time, selected.end_time)
                recording = replay.start_recording(
                    video_channel_id=args.video_channel_id,
                    audio_channel_id=args.audio_channel_id,
                )
                recording_ready_at = time.monotonic() + MINIMUM_RECORDING_SECONDS
                try:
                    signals.wait_after((*frame_names, "encoded_video_key"), {}, deadline)
                    _ = replay.current_time
                    remaining = recording_ready_at - time.monotonic()
                    if remaining > 0:
                        if time.monotonic() + remaining > deadline:
                            raise TimeoutError("timed out waiting for recordable media")
                        time.sleep(remaining)
                    with recording.stop() as recording_file:
                        save_temporary_media(
                            recording_file.path,
                            args.output_dir / "ti-cloud-storage-replay-recording.mp4",
                            b"ftyp",
                            4,
                        )
                except BaseException:
                    try:
                        recording.stop().delete()
                    except BaseException:
                        pass
                    raise

                retry_while_in_use("Replay.pause", replay.pause, deadline)
                retry_while_in_use("Replay.resume", replay.resume, deadline)

                retry_while_in_use("AudioOutput.detach", audio.detach, deadline)
                span = selected.end_time - selected.start_time
                retry_while_in_use(
                    "Replay.seek",
                    lambda: replay.seek(selected.start_time + span / 5),
                    deadline,
                )
                baseline = signals.snapshot(("video",))
                retry_while_in_use(
                    "Replay.set_speed(0.5x)",
                    lambda: replay.set_speed(storage.ReplaySpeed.X0_5),
                    deadline,
                )
                signals.wait_after(("video",), baseline, deadline)
                if replay.speed is not storage.ReplaySpeed.X0_5:
                    raise RuntimeError("replay speed did not change to 0.5x")
                baseline = signals.snapshot(("video",))
                retry_while_in_use(
                    "Replay.set_speed(1x)",
                    lambda: replay.set_speed(storage.ReplaySpeed.X1),
                    deadline,
                )
                signals.wait_after(("video",), baseline, deadline)

                while True:
                    try:
                        snapshot = video.take_snapshot()
                        break
                    except tirtc.NoFrameError:
                        baseline = signals.snapshot(("video",))
                        signals.wait_after(("video",), baseline, deadline)
                with snapshot:
                    save_temporary_media(
                        snapshot.path,
                        args.output_dir / "ti-cloud-storage-snapshot.jpg",
                        b"\xff\xd8",
                        0,
                    )

                if not terminal.wait(max(0, deadline - time.monotonic())):
                    raise TimeoutError("timed out waiting for replay completion")
                if terminal_error:
                    raise terminal_error[0]

                export = cloud.export_recording(
                    selected.start_time,
                    selected.end_time,
                    video_channel_id=args.video_channel_id,
                    audio_channel_id=args.audio_channel_id,
                )
                try:
                    exported = export.wait(timeout=max(0.001, deadline - time.monotonic()))
                except tirtc.OperationTimeoutError:
                    exported = export.stop()
                if export.progress != 1.0:
                    raise RuntimeError(f"export completed with progress {export.progress:.3f}")
                with exported:
                    save_temporary_media(
                        exported.path,
                        args.output_dir / "ti-cloud-storage-range-export.mp4",
                        b"ftyp",
                        4,
                    )

                retry_while_in_use(
                    "EncodedVideoOutput.detach", encoded_video.detach, deadline
                )
                retry_while_in_use(
                    "EncodedAudioOutput.detach", encoded_audio.detach, deadline
                )
                retry_while_in_use("VideoOutput.detach", video.detach, deadline)
    finally:
        storage.shutdown()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"tirtc Ti Cloud Storage example failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
