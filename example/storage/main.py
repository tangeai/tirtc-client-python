from __future__ import annotations

import argparse
import hashlib
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import math
import json
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
        description="Query and replay Ti Cloud Storage with the public tirtc.storage package."
    )
    parser.add_argument(
        "--auth-mode",
        choices=("access-key", "external-token"),
        required=True,
        help="credential owner used to create the Ti Cloud Storage client",
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
    parser.add_argument("--audio-channel-id", type=int)
    parser.add_argument("--video-channel-id", type=int, action="append")
    parser.add_argument("--no-receive-audio", action="store_true")
    parser.add_argument("--no-receive-video", action="store_true")
    parser.add_argument(
        "--raw-dump",
        action="store_true",
        help="capture selected raw objects and encoded inputs and return the diagnostic ZIP",
    )
    parser.add_argument("--raw-dump-seconds", type=float, default=10.0)
    parser.add_argument("--raw-dump-copy-to", type=Path)
    parser.add_argument("--upload-logs", action="store_true")
    parser.add_argument(
        "--case-id",
        choices=(
            "ti-cloud-storage.raw-dump.smoke-upload",
            "ti-cloud-storage.raw-dump.integration-recovery",
        ),
        help="run one registered raw dump Cloud Storage case through this public Example",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--verify-permission-rejection", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.case_id is not None:
        args.raw_dump = True
        args.upload_logs = True
    if not args.cache_dir.is_absolute() or not args.output_dir.is_absolute():
        parser.error("--cache-dir and --output-dir must be absolute")
    if args.start_ms < 0 or args.start_ms >= args.end_ms:
        parser.error("--start-ms must be non-negative and earlier than --end-ms")
    if args.no_receive_audio and args.audio_channel_id is not None:
        parser.error("--no-receive-audio conflicts with --audio-channel-id")
    if args.no_receive_video and args.video_channel_id is not None:
        parser.error("--no-receive-video conflicts with --video-channel-id")
    args.audio_channel_id = None if args.no_receive_audio else (
        0 if args.audio_channel_id is None else args.audio_channel_id
    )
    args.video_channel_ids = [] if args.no_receive_video else (
        [1] if args.video_channel_id is None else args.video_channel_id
    )
    if args.audio_channel_id is not None and not 0 <= args.audio_channel_id <= 255:
        parser.error("channel IDs must be between 0 and 255")
    if len(args.video_channel_ids) > 3 or any(
        not 0 <= channel_id <= 255 for channel_id in args.video_channel_ids
    ):
        parser.error("provide at most three video channel IDs between 0 and 255")
    if len(set(args.video_channel_ids)) != len(args.video_channel_ids):
        parser.error("video channel IDs must be distinct")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if not math.isfinite(args.raw_dump_seconds) or not 0 < args.raw_dump_seconds <= 300:
        parser.error("--raw-dump-seconds must be finite and from 0 through 300 seconds")
    if args.raw_dump_copy_to is not None and (
        not args.raw_dump or not args.raw_dump_copy_to.is_absolute()
    ):
        parser.error("--raw-dump-copy-to requires --raw-dump and an absolute path")
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


def refresh_token(cloud: storage.CloudStorage) -> None:
    refreshed = os.environ.get("TI_CLOUD_STORAGE_REFRESHED_ACCESS_TOKEN", "")
    if not refreshed:
        raise RuntimeError(
            "token expired; set TI_CLOUD_STORAGE_REFRESHED_ACCESS_TOKEN and retry"
        )
    cloud.update_token(refreshed)


def verify_permission_rejection(cloud: storage.CloudStorage, valid_token: str,
                                start: datetime, end: datetime,
                                deadline: float) -> dict[str, object]:
    # Construction must have succeeded. The rejected remote list is the proof,
    # not a local argument error; this synchronous API has one returned terminal.
    cloud.update_token("invalid-token")
    try:
        cloud.list_recordings(start, end, timeout=max(0.001, deadline - time.monotonic()))
    except tirtc.PermissionDeniedError as failure:
        if failure.code != 6024:
            raise
        terminal = (type(failure).__name__, failure.code)
    else:
        raise RuntimeError("invalid credential did not fail remote authorization")
    cloud.update_token(valid_token)
    ranges = cloud.list_recordings(start, end, timeout=max(0.001, deadline - time.monotonic()))
    if not ranges:
        raise RuntimeError("explicit token recovery retry returned no uploaded recording")
    return {
        "status": "passed",
        "case": "invalid-token-permission-rejection",
        "same_store": True,
        "failed_operation_code": terminal[1],
        "failed_operation_terminal_returns": 1,
        "explicit_retry": True,
        "retry_ranges": len(ranges),
    }


def run() -> None:
    args = parse_args()
    app_id = os.environ.get("TI_CLOUD_STORAGE_APP_ID", "")
    if not app_id:
        raise RuntimeError("TI_CLOUD_STORAGE_APP_ID is required")
    if args.auth_mode == "external-token":
        token = os.environ.get("TI_CLOUD_STORAGE_ACCESS_TOKEN", "")
        if not token:
            raise RuntimeError(
                "TI_CLOUD_STORAGE_ACCESS_TOKEN is required for external-token mode"
            )
        access_key_id = ""
        access_key_secret = ""
        device_id = ""
    else:
        token = ""
        access_key_id = os.environ.get("TI_CLOUD_STORAGE_ACCESS_KEY_ID", "")
        access_key_secret = os.environ.get("TI_CLOUD_STORAGE_ACCESS_KEY_SECRET", "")
        device_id = os.environ.get("TI_CLOUD_STORAGE_DEVICE_ID", "")
        if not access_key_id or not access_key_secret or not device_id:
            raise RuntimeError(
                "TI_CLOUD_STORAGE_ACCESS_KEY_ID, "
                "TI_CLOUD_STORAGE_ACCESS_KEY_SECRET, and "
                "TI_CLOUD_STORAGE_DEVICE_ID are required for access-key mode"
            )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    start = epoch + timedelta(milliseconds=args.start_ms)
    end = epoch + timedelta(milliseconds=args.end_ms)
    deadline = time.monotonic() + args.timeout
    signals = Signals()
    terminal = threading.Event()
    terminal_error: list[tirtc.TiRTCError] = []

    options = tirtc.ClientOptions(
        app_id=app_id,
        cache_dir=args.cache_dir,
        endpoint=args.endpoint,
    )
    if args.auth_mode == "external-token":
        client_owner = storage.Client(options)
    else:
        client_owner = storage.Client(
            options,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )
    with client_owner as client:
        if args.auth_mode == "external-token":
            cloud_owner = client.open_with_token(token)
        else:
            cloud_owner = client.open_device(device_id)
        with cloud_owner as cloud:
            if args.verify_permission_rejection:
                if args.auth_mode != "external-token":
                    raise RuntimeError("permission rejection verification requires external-token auth")
                rejection = verify_permission_rejection(cloud, token, start, end, deadline)
                (args.output_dir / "permission-rejection.json").write_text(
                    json.dumps(rejection, sort_keys=True) + "\n", encoding="utf-8"
                )
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
                if args.auth_mode != "external-token":
                    raise
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
            if args.audio_channel_id is None and not args.video_channel_ids:
                print("recording query completed; no media selected")
                return

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
                stack.callback(replay.close)
                audio: storage.AudioOutput | None = None
                encoded_audio: storage.EncodedAudioOutput | None = None
                if args.audio_channel_id is not None:
                    audio = storage.AudioOutput(
                        lambda frame: signals.notify("audio"), on_error=on_output_error
                    )
                    stack.callback(audio.close)
                    encoded_audio = storage.EncodedAudioOutput(
                        lambda frame: signals.notify("encoded_audio"),
                        on_error=on_output_error,
                    )
                    stack.callback(encoded_audio.close)
                    audio.attach(replay, args.audio_channel_id)
                    encoded_audio.attach(replay, args.audio_channel_id)

                video_outputs: list[
                    tuple[int, storage.VideoOutput, storage.EncodedVideoOutput]
                ] = []
                for channel_id in args.video_channel_ids:
                    decoded_name = f"video:{channel_id}"
                    encoded_name = f"encoded_video:{channel_id}"
                    key_name = f"encoded_video_key:{channel_id}"

                    def on_video(
                        frame: tirtc.VideoFrame, name: str = decoded_name
                    ) -> None:
                        del frame
                        signals.notify(name)

                    video = storage.VideoOutput(
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

                    encoded_video = storage.EncodedVideoOutput(
                        on_encoded_video, on_error=on_output_error
                    )
                    stack.callback(encoded_video.close)
                    video.attach(replay, channel_id)
                    encoded_video.attach(replay, channel_id)
                    try:
                        early_snapshot = video.take_snapshot()
                    except tirtc.NoFrameError:
                        pass
                    else:
                        early_snapshot.delete()
                        raise RuntimeError("zero-frame snapshot unexpectedly succeeded")
                    video_outputs.append((channel_id, video, encoded_video))

                replay.play(selected.start_time, selected.end_time)
                required_frames: list[str] = []
                if args.audio_channel_id is not None:
                    required_frames.extend(("audio", "encoded_audio"))
                for channel_id, _, _ in video_outputs:
                    required_frames.extend(
                        (f"video:{channel_id}", f"encoded_video:{channel_id}")
                    )
                dump: tirtc.RawDump | None = None
                capture_deadline = 0.0
                if args.raw_dump:
                    raw_dump_options = storage.RawDumpOptions(
                        audio_channel_ids=(
                            ()
                            if args.audio_channel_id is None
                            else (args.audio_channel_id,)
                        ),
                        video_channel_ids=tuple(args.video_channel_ids),
                    )
                    dump = replay.start_raw_dump(raw_dump_options)
                    stack.callback(dump.close)
                    if args.case_id == "ti-cloud-storage.raw-dump.integration-recovery":
                        try:
                            replay.start_raw_dump(raw_dump_options)
                        except tirtc.InUseError:
                            pass
                        else:
                            raise RuntimeError("duplicate raw dump start was accepted")
                        replay.pause()
                        replay.resume()
                        replay.seek(selected.start_time)
                    capture_deadline = time.monotonic() + args.raw_dump_seconds
                    if capture_deadline > deadline:
                        raise TimeoutError(
                            "raw dump duration exceeds the remaining example timeout"
                        )
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
                    first_video.attach(replay, first_id)
                    first_encoded.attach(replay, first_id)
                    restored_before = signals.snapshot(route_names)
                    # Observe the next keyframe from this same replay.
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
                        raise RuntimeError(
                            "raw dump archive size does not match its metadata"
                        )
                    if args.raw_dump_copy_to is not None:
                        args.raw_dump_copy_to.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(archive.path, args.raw_dump_copy_to)
                    if (
                        args.case_id == "ti-cloud-storage.raw-dump.integration-recovery"
                        and dump.stop() is not archive
                    ):
                        raise RuntimeError("repeated raw dump stop changed the terminal result")
                    print(json.dumps({
                        "raw_dump": {
                            "capture_id": archive.capture_id,
                            "path": str(archive.path),
                            "size": archive.size,
                            "sha256": archive.sha256,
                            "captured_duration_ms": int(
                                archive.captured_duration.total_seconds() * 1000
                            ),
                            "capture_complete": archive.capture_complete,
                            "stop_reason": archive.stop_reason.value,
                            "unsaved_packet_count": archive.unsaved_packet_count,
                            "unsaved_byte_count": archive.unsaved_byte_count,
                            "empty": archive.empty,
                        }
                    }, sort_keys=True))
                if args.upload_logs:
                    print(json.dumps({"log_id": client.upload_logs()}, sort_keys=True))

                for channel_id, video, _ in video_outputs:
                    recording = replay.start_recording(
                        video_channel_id=channel_id,
                        audio_channel_id=args.audio_channel_id,
                    )
                    try:
                        key_name = f"encoded_video_key:{channel_id}"
                        encoded_name = f"encoded_video:{channel_id}"
                        baseline = signals.snapshot((key_name, encoded_name))
                        signals.wait_after((key_name,), baseline, deadline)
                        after_key = signals.snapshot((encoded_name,))
                        signals.wait_after((encoded_name,), after_key, deadline)
                        with recording.stop() as recording_file:
                            save_temporary_media(
                                recording_file,
                                args.output_dir / f"ti-cloud-storage-replay-recording-channel-{channel_id}.mp4",
                                b"ftyp",
                                4,
                            )
                    except BaseException:
                        try:
                            recording.stop().delete()
                        except BaseException:
                            pass
                        raise

                    while True:
                        try:
                            snapshot = video.take_snapshot()
                            break
                        except tirtc.NoFrameError:
                            name = f"video:{channel_id}"
                            baseline = signals.snapshot((name,))
                            signals.wait_after((name,), baseline, deadline)
                    with snapshot:
                        save_temporary_media(
                            snapshot,
                            args.output_dir / f"ti-cloud-storage-snapshot-channel-{channel_id}.jpg",
                            b"\xff\xd8",
                            0,
                        )
                    print(f"consumed video channel {channel_id}")

                replay.pause()
                if video_outputs:
                    with video_outputs[0][1].take_snapshot() as paused_snapshot:
                        save_temporary_media(paused_snapshot, args.output_dir / "ti-cloud-storage-paused-snapshot.jpg", b"\xff\xd8", 0)
                replay.resume()

                if audio is not None and video_outputs:
                    audio.detach()
                    audio = None

                span = selected.end_time - selected.start_time
                span_ms = (
                    span.days * 86_400_000
                    + span.seconds * 1_000
                    + span.microseconds // 1_000
                )
                replay.seek(
                    selected.start_time + timedelta(milliseconds=span_ms // 5)
                )
                progress_name = (
                    f"video:{video_outputs[0][0]}" if video_outputs else "audio"
                )
                baseline = signals.snapshot((progress_name,))
                replay.set_speed(storage.ReplaySpeed.X0_5)
                signals.wait_after((progress_name,), baseline, deadline)
                if replay.speed is not storage.ReplaySpeed.X0_5:
                    raise RuntimeError("replay speed did not change to 0.5x")
                baseline = signals.snapshot((progress_name,))
                replay.set_speed(storage.ReplaySpeed.X1)
                signals.wait_after((progress_name,), baseline, deadline)

                if not terminal.wait(max(0, deadline - time.monotonic())):
                    raise TimeoutError("timed out waiting for replay completion")
                if terminal_error:
                    raise terminal_error[0]

                full_exports: list[dict[str, object]] = []
                for channel_id, video, encoded_video in video_outputs:
                    export = cloud.export_recording(
                        selected.start_time,
                        selected.end_time,
                        video_channel_id=channel_id,
                        audio_channel_id=args.audio_channel_id,
                    )
                    try:
                        exported = export.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except tirtc.OperationTimeoutError:
                        exported = export.stop()
                    with exported:
                        if export.progress != 1.0:
                            raise RuntimeError(f"export completed with progress {export.progress:.3f}")
                        report = export.report
                        if (
                            report.termination is not storage.ExportTermination.EXHAUSTED
                            or report.unprocessed_ranges
                            or report.covered_duration_ms <= 0
                        ):
                            raise RuntimeError("range export did not finish scanning usable video")
                        if not report.complete:
                            raise RuntimeError("range export is incomplete")
                        full_exports.append({
                            "channel_id": channel_id,
                            "complete": True,
                            "termination": "EXHAUSTED",
                            "unprocessed_ranges": 0,
                            "saved_name": f"ti-cloud-storage-range-export-channel-{channel_id}.mp4",
                            "source_deleted": True,
                        })
                        print(
                            f"export channel={channel_id} complete={report.complete} "
                            f"termination={report.termination.name} "
                            f"covered_ms={report.covered_duration_ms} "
                            f"gaps={len(report.gaps)} "
                            f"unprocessed={len(report.unprocessed_ranges)}"
                        )
                        save_temporary_media(
                            exported,
                            args.output_dir / f"ti-cloud-storage-range-export-channel-{channel_id}.mp4",
                            b"ftyp",
                            4,
                        )
                    encoded_video.detach()
                    video.detach()

                if not video_outputs:
                    if encoded_audio is not None:
                        encoded_audio.detach()
                    if audio is not None:
                        audio.detach()
                    return

                media_tail = selected
                gap_tail = selected
                empty_start = gap_tail.end_time
                empty_end = empty_start + timedelta(seconds=60)
                following = cloud.list_recordings(
                    empty_start,
                    empty_end,
                    timeout=max(0.001, deadline - time.monotonic()),
                )
                if following:
                    gap_tail = max(
                        following, key=lambda item: (item.end_time, item.start_time)
                    )
                    empty_start = gap_tail.end_time
                    empty_end = empty_start + timedelta(seconds=60)
                    following = cloud.list_recordings(
                        empty_start,
                        empty_end,
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                if following:
                    raise RuntimeError(
                        "partial export fixture needs a confirmed empty following minute"
                    )
                partial_start = max(
                    media_tail.start_time,
                    media_tail.end_time - timedelta(seconds=12),
                )
                if partial_start >= media_tail.end_time:
                    raise RuntimeError("partial export fixture has no valid recorded tail")
                partial_channel = video_outputs[0][0]
                partial_task = cloud.export_recording(
                    partial_start,
                    empty_end,
                    video_channel_id=partial_channel,
                    audio_channel_id=args.audio_channel_id,
                )
                try:
                    partial_file = partial_task.wait(
                        timeout=max(0.001, deadline - time.monotonic())
                    )
                except tirtc.OperationTimeoutError:
                    partial_file = partial_task.stop()
                partial_name = (
                    f"ti-cloud-storage-partial-export-channel-{partial_channel}.mp4"
                )
                with partial_file:
                    partial_report = partial_task.report
                    if (
                        partial_task.progress != 1.0
                        or partial_report.complete
                        or partial_report.termination
                        is not storage.ExportTermination.EXHAUSTED
                        or not partial_report.segments
                        or not partial_report.gaps
                        or partial_report.unprocessed_ranges
                        or partial_report.covered_duration_ms <= 0
                    ):
                        raise RuntimeError(
                            "partial export did not exhaust the recorded tail and confirmed empty window"
                        )
                    save_temporary_media(
                        partial_file,
                        args.output_dir / partial_name,
                        b"ftyp",
                        4,
                    )
                (args.output_dir / "export-proof.json").write_text(
                    json.dumps({
                        "full": {
                            "status": "passed",
                            "exports": full_exports,
                        },
                        "partial": {
                            "status": "passed",
                            "complete": False,
                            "termination": "EXHAUSTED",
                            "segments": len(partial_report.segments),
                            "gaps": len(partial_report.gaps),
                            "unprocessed_ranges": 0,
                            "confirmed_empty_window": True,
                            "channel_id": partial_channel,
                            "saved_name": partial_name,
                            "source_deleted": True,
                        },
                    }, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                if encoded_audio is not None:
                    encoded_audio.detach()
                if audio is not None:
                    audio.detach()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"tirtc Ti Cloud Storage example failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
