# Python Ti Cloud Storage API Reference

Ti Cloud Storage 位于 `tirtc.storage`。Cloud Client 是服务调用方和资源树 owner，可以创建多个每设备 `CloudStorage` child。它与根 `tirtc` package 复用同一个 `ClientOptions`、Frame、媒体 enum、临时文件和异常；Cloud Output 只能绑定 Replay，RTC Output 只能绑定 Connection。

完整查询、回放、截图、边播边录和范围导出程序位于 [`python/example/storage`](../example/storage)。

## Client 与每设备 CloudStorage

```python
from tirtc import ClientOptions
import tirtc.storage as storage

class Client:
    def __init__(
        self,
        options: ClientOptions,
        *,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
    ) -> None: ...
    def open_with_token(self, token: str) -> CloudStorage: ...
    def open_device(self, device_id: str) -> CloudStorage: ...
    def upload_logs(self) -> str: ...
    def close(self) -> None: ...

class CloudStorage:
    def update_token(self, token: str) -> None: ...
    def close(self) -> None: ...
```

ClientOptions 的 `cache_dir` 必须是绝对路径，且不包含凭据。省略 AK/SK 时 Client 使用 External Token 模式；同时提供两项时使用 Access Key 模式。只提供一项、传入空字符串或 NUL 会在 Native 副作用前抛 `ValueError`。External Token 模式接收应用服务端下发、已经绑定设备的短期 APP Access Token，并使用 `open_with_token()`。Access Key 模式只供能够安全保管 AK/SK 的可信进程使用，并用 `open_device()` 打开明确设备。两个入口返回同一种 CloudStorage，后续查询、Replay、Export、Output 和文件 API 完全相同。

External Token child 遇到 Token 过期时，当前操作抛 `TokenExpiredError`。应用取得新 Token、调用 `update_token()`，再显式重做失败操作。Access Key child 的 Token 签发与刷新由 Runtime 管理；调用 `update_token()` 会在 Native 副作用前抛 `InUseError`。两种模式的反向 open 方法也在副作用前抛 `InUseError`。

Cloud Client 可以多实例。Access Key Client 可以使用不同 App ID、AK/SK 和 endpoint 并存。External Token Client 复用 Runtime legacy init，因此同时活跃的实例必须使用相同 App ID 和 endpoint；关闭一个实例不影响其他实例及其 child，关闭最后一个实例才 uninit，之后可以用不同配置重建。所有产品仍须共享进程级 cache 目录和 console 设置。

`Client.close()` 取消在途 query、Replay、Recording 和 Export，Detach 关联 Output，关闭自己的 CloudStorage 树，并等待已接受的 callback task 退出。它不影响其他 Cloud Client，也不关闭应用独立持有的 Output 或已经返回的媒体文件。`CloudStorage` 只能由 Client 打开，直接调用 `CloudStorage()` 会抛 `TypeError`。

## 查询日期与录像范围

```python
@dataclass(frozen=True, slots=True)
class RecordingDay:
    date: date
    has_recording: bool

@dataclass(frozen=True, slots=True)
class RecordingRange:
    start_time: datetime
    end_time: datetime

def list_recording_days(
    self,
    start_date: date,
    end_date: date,
    *,
    timezone: str | ZoneInfo = "Asia/Shanghai",
    timeout: float | None = None,
) -> list[RecordingDay]: ...

def list_recordings(
    self,
    start_time: datetime,
    end_time: datetime,
    *,
    timeout: float | None = None,
) -> list[RecordingRange]: ...
```

日期查询使用显式 IANA 时区，不读取系统时区；起止日期都包含，结果按日期升序返回。录像查询采用 `[start_time, end_time)`，返回结果按时间升序、裁剪到查询窗口并合并相邻或重叠范围；没有录像时返回空 list。

录像时间必须是 aware `datetime` 且精确到整毫秒。所有时间使用整数 UTC epoch 运算。`timeout` 单位为秒，`None` 表示没有调用方 deadline；NaN、正负 Infinity 和非正数抛 `ValueError`。

同一 CloudStorage 同时只允许一个 List，第二个抛 `InUseError`。达到 deadline 后 SDK 取消 Native request，等待 terminal callback 与销毁 barrier 完成，再抛 `OperationTimeoutError(code=None)`。

## Replay

```python
class CloudStorage:
    def create_replay(
        self,
        *,
        on_time_changed=None,
        on_completed=None,
        on_error=None,
        on_recording_gap=None,
    ) -> Replay: ...

class Replay:
    def play(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        initial_time: datetime | None = None,
    ) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def seek(self, target: datetime) -> None: ...
    def set_speed(self, speed: ReplaySpeed) -> None: ...
    @property
    def speed(self) -> ReplaySpeed: ...
    @property
    def current_time(self) -> datetime | None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
```

播放范围是 `[start_time, end_time)`，`initial_time` 与后续 `seek()` 目标必须位于这个范围。倍速包括 `0.125x`、`0.25x`、`0.5x`、`1x`、`2x`、`4x` 和 `8x`。

范围自然耗尽只调用一次 `on_completed`；主动 Stop、被新 Play 替换或来源失败不调用 completed。来源失败通过 `on_error` 报告。`on_recording_gap(RecordingGap)` 交付已确认的录像缺口，可恢复缺口之后继续播放。

Replay 的 Seek 和新 Play 使用 Runtime generation 屏障，旧 generation 的位置、缺口和终态不会进入新播放。Replay 或关联 Output callback 内同步 play、seek、stop、close 抛 `InUseError`。

## Output 与 Frame

`tirtc.storage` 提供独立的 `AudioOutput`、`VideoOutput`、`EncodedAudioOutput` 和 `EncodedVideoOutput`。四类 Output 都提供 `attach(replay, channel_id)`、`detach()`、只读 `state` 和幂等 `close()`；VideoOutput 另外提供 `take_snapshot()`。Channel ID 范围是 `0..255`，音视频可以使用相同数值。

Attach 只建立 binding，不转移 ownership。Replay、CloudStorage 或 Client 关闭时会 Detach 相关 Output 并等待已接受 callback 退出，Output 本身保持 open。显式或自动 Detach 后可以绑定到另一个仍存活的 Replay，应用最终负责关闭它。

Frame、codec、bitstream、pixel format 与 OutputState 复用根包。Cloud Frame 的 `source_time` 是录像 UTC 时间。媒体数据是 read-only `memoryview` 并持有 Native retain；背压丢帧后下一帧设置 `discontinuity=True`，错误和自然终态保留。

## 播放中保存、导出与截图

```python
class Replay:
    def start_recording(
        self, *, video_channel_id: int, audio_channel_id: int | None = None
    ) -> RecordingTask: ...

class RecordingTask:
    def stop(self) -> tirtc.RecordingFile: ...

class CloudStorage:
    def export_recording(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        video_channel_id: int,
        audio_channel_id: int | None = None,
        on_progress: Callable[[ExportProgress], None] | None = None,
        on_recording_gap: Callable[[RecordingGap], None] | None = None,
    ) -> ExportTask: ...

class ExportTask:
    @property
    def progress(self) -> float: ...
    @property
    def progress_detail(self) -> ExportProgress: ...
    @property
    def report(self) -> ExportReport: ...
    def cancel(self) -> None: ...
    def wait(self, *, timeout: float | None = None) -> tirtc.RecordingFile: ...
    def stop(self) -> tirtc.RecordingFile: ...
```

RecordingTask 只保存创建后进入当前 Replay 的媒体。ExportTask 与 Replay 独立。`wait(timeout=...)` 达到调用方 deadline 时只停止等待，Task 保持活动；应用可以继续等待。`cancel()` 请求取消，随后调用 `wait()` 收取终局与报告。并发 Wait/Stop 共享同一个终局。

部分可播放文件也是成功结果，完整性由 `report.complete` 判断。报告还包含 `requested_range`、`covered_duration_ms`、`segments`、`gaps`、`unprocessed_ranges`、`termination` 和 `cause`。即使 `wait()` 因终局失败或取消抛异常，仍可读取报告。

Snapshot、Recording 和 Export 返回根包的 `SnapshotFile` 或 `RecordingFile`。路径位于 SDK cache；应用在文件 context 内检查 Export 完整性并复制到自己的持久目录。已经返回的文件可以越过 Client 关闭继续保存或删除。

## 释放、Error 与进程

类型和值域错误分别使用 `TypeError`、`ValueError`。Native 与调用方 deadline 使用根包的 `TiRTCError` 体系。不要解析异常文本或 Runtime 日志驱动业务逻辑。

正常释放顺序是完成 RecordingTask 和 ExportTask、Detach/关闭 Output、Stop/关闭 Replay、关闭 CloudStorage、关闭 Client。Client 级联关闭也会完成自己的资源树收口。在当前 Client 或 opaque Cloud task callback 中同步关闭 Client 会在任何 teardown 前抛 `InUseError`。

import 不创建线程、目录、网络连接或 Runtime 状态。父进程仅 import 后 fork 时，子进程可以创建自己的 Client；任一 RTC 或 Cloud Client 活跃后 fork，子进程首次调用 SDK 抛 `UnsupportedError(name="forked_process")`，并且必须立即 `exec` 或 `os._exit()`。
