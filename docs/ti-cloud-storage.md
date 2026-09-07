# Python Ti Cloud Storage API Reference

Ti Cloud Storage 位于 `tirtc.storage`。它与根 `tirtc` package 复用 Frame、媒体 enum、临时文件和异常，但拥有独立的产品生命周期、授权对象、Replay 与四类 Output。Cloud Output 只能绑定 Replay，RTC Output 只能绑定 Connection。

完整查询、回放、截图、边播边录和范围导出程序位于 [`python/example/storage`](../example/storage)。

接入前由应用准备 Ti Cloud Storage App ID、应用服务端下发且绑定目标设备的短期 APP Access Token，以及设备配置或业务服务提供的音视频 Channel ID。录像列表返回设备级可用时间段，不负责发现 Channel。

## 初始化与授权对象

```python
import tirtc.storage as storage

storage.initialize(APP_ID, CACHE_DIR, endpoint=ENDPOINT)
try:
    with storage.CloudStorage(APP_ACCESS_TOKEN) as cloud:
        ranges = cloud.list_recordings(start, end, timeout=30)
finally:
    storage.shutdown()
```

```python
def initialize(app_id: str, cache_dir: str | os.PathLike[str], *,
               endpoint: str | None = None,
               console_log_enabled: bool = False) -> None: ...
def shutdown() -> None: ...

class CloudStorage:
    def __init__(self, token: str) -> None: ...
    def update_token(self, token: str) -> None: ...
    def close(self) -> None: ...
```

Token 由应用服务端签发并绑定一台设备。Token 过期由当前操作抛 `TokenExpiredError`；SDK 不自动刷新或重试。应用取得新 Token 后调用 `update_token()`，再显式重做失败操作。CloudStorage 仍有 List、Replay 或 ExportTask 时 `close()` 抛 `InUseError`，状态保持不变。

平台、默认 console、进程与 fork 限制与根 `tirtc` 相同：支持 macOS 11.5+ arm64、glibc 2.35+ Linux x86_64 和普通 CPython 3.11+；默认 console 关闭；initialize 或创建资源后 fork 的子进程首次调用抛 `UnsupportedError(name="forked_process")`，且必须立即 `exec` 或 `os._exit()`，不能执行普通 Python 解释器 teardown。

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

def list_recording_days(self, start_date: date, end_date: date, *,
                        timezone: str | ZoneInfo = "Asia/Shanghai",
                        timeout: float | None = None) -> list[RecordingDay]: ...
def list_recordings(self, start_time: datetime, end_time: datetime, *,
                    timeout: float | None = None) -> list[RecordingRange]: ...
```

日期查询使用显式 IANA 时区，不读取系统时区；起止日期都包含，结果覆盖完整日期区间并按日期升序返回。录像查询采用 `[start_time, end_time)`，返回结果按时间升序、裁剪到查询窗口并合并相邻或重叠范围；没有录像时返回空 list，不是错误。

录像时间必须是 aware `datetime` 且精确到整毫秒，SDK 规范为 UTC；naive 或亚毫秒 datetime 抛 `ValueError`。所有时间使用整数 UTC epoch 运算，不经过 float timestamp。`timeout` 单位为秒，`None` 表示没有调用方 deadline；NaN、正负 Infinity 和非正数抛 `ValueError`。

同一 CloudStorage 同时只允许一个 List，第二个抛 `InUseError`。达到 deadline 后 SDK 取消 Native request，等待 terminal callback 与销毁 barrier 完成，再抛 `OperationTimeoutError(code=None)`。

## Replay

```python
class CloudStorage:
    def create_replay(self, *, on_time_changed=None,
                      on_completed=None, on_error=None) -> Replay: ...

class Replay:
    def play(self, start_time: datetime, end_time: datetime, *,
             initial_time: datetime | None = None) -> None: ...
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

播放范围是 `[start_time, end_time)`，`initial_time` 与后续 `seek()` 目标必须位于这个左闭右开范围。倍速包括 `0.125x`、`0.25x`、`0.5x`、`1x`、`2x`、`4x`、`8x`。`current_time` 在尚无有效播放位置时为 `None`。范围自然耗尽只调用一次 `on_completed`；主动 Stop、被新 Play 替换或来源失败不调用 completed。来源失败通过 `on_error` 报告。

外部线程调用 pause、resume、seek、set_speed 或 Output detach 时，SDK 会在私有 5 秒预算内吸收 Native 的瞬态 `InUse`；超限抛 `OperationTimeoutError`。关联 callback 内不会轮询，避免自锁。

Replay 仍有 Output 或 RecordingTask 时不能关闭。在 Replay 自己或直接关联 Output 的 callback 内关闭也会抛 `InUseError`。

同一资源的并发 `close()` 收敛到一次 Native 释放；关闭开始后进入的新操作抛 `ClosedError`。被接受的关闭在返回前完成 Python callback barrier。

## Output 与 Frame

`tirtc.storage` 独立提供 `AudioOutput`、`VideoOutput`、`EncodedAudioOutput`、`EncodedVideoOutput`。四类 Output 都提供：

```python
def attach(self, replay: Replay, channel_id: int) -> None: ...
def detach(self) -> None: ...
@property
def state(self) -> tirtc.OutputState: ...
def close(self) -> None: ...
```

四类构造器的第一个 `on_frame` 参数必填且不能为 `None`；state/error handler 可空。未提供且没有内部状态职责的 callback 不会注册到 Native。video 每对象最多排队 2 帧，audio 最多排队 64 帧；同一对象严格串行，不同对象的 callback 可能并发执行且不承诺彼此顺序，共享应用状态时由调用方同步。长工作应交给应用自己的 executor 或 queue。

Channel ID 范围是 `0..255`。Video Output 另提供 `take_snapshot()`。Frame、codec、bitstream、pixel format 与 OutputState 复用根包；Cloud Frame 的 `source_time` 是录像 UTC 时间。

媒体 callback 在私有 Python executor 中按对象串行交付。Frame 数据是 read-only `memoryview` 并持有 Native retain；背压丢帧后下一帧设置 `discontinuity=True`，错误和自然终态保留。callback 异常交给 `sys.unraisablehook`，不会改变 Replay/Output 状态。

## 播放中保存、导出与截图

```python
class Replay:
    def start_recording(self, *, video_channel_id: int,
                        audio_channel_id: int | None = None) -> RecordingTask: ...

class RecordingTask:
    def stop(self) -> tirtc.RecordingFile: ...

class CloudStorage:
    def export_recording(self, start_time: datetime, end_time: datetime, *,
                         video_channel_id: int,
                         audio_channel_id: int | None = None) -> ExportTask: ...

class ExportTask:
    @property
    def progress(self) -> float: ...
    def wait(self, *, timeout: float | None = None) -> tirtc.RecordingFile: ...
    def stop(self) -> tirtc.RecordingFile: ...
```

RecordingTask 只保存创建后进入当前 Replay 的媒体。音视频 Channel ID 都位于 `0..255`，可以使用相同数值。`stop()` 返回唯一终局，重复或并发调用不重复停止 Native task。

ExportTask 与 Replay 独立，音视频 Channel ID 也可以相同。`progress` 直接查询 Native，单调位于 `0..1`，成功终局为 `1.0`。`wait(timeout=...)` 达到调用方 deadline 时只停止等待并抛 `OperationTimeoutError`，Task 保持活动；应用随后可以继续 `wait()` 或调用 `stop()`。并发 Wait/Stop 共享同一个终局。

Snapshot、Recording 和 Export 返回根包的 `SnapshotFile` / `RecordingFile`。路径位于 SDK cache；应用复制到自己的持久目录后调用幂等 `delete()`，或用 context manager 在退出时删除临时源。

## 释放顺序与 Error

退出时先完成 RecordingTask 与 ExportTask，再 Detach/关闭四类 Output，Stop/关闭 Replay，关闭 CloudStorage，最后调用 `storage.shutdown()`。正常释放不依赖 finalizer。遗失的 Output、Replay、RecordingTask 或 ExportTask 会发出 `ResourceWarning` 并 best-effort 完成终局、销毁 Native child、删除无人接收的临时文件；只有 checked destroy 成功后才归还 parent 计数。

类型和值域错误分别使用 `TypeError`、`ValueError`。Native 与调用方 deadline 使用根包的 `TiRTCError` 体系；Cloud 常见恢复类别包括 `TokenExpiredError`、`PermissionDeniedError`、`CancelledError`、`StoppedError`、`RangeTooLargeError`、`RecordingUnreadableError`、`RecordingNotFoundError`、`RecordingDownloadFailedError`、`UnavailableError`、`NetworkUnavailableError`、`EndpointDNSResolutionFailedError`、`NoFrameError`、`NoRecordableMediaError` 与 `OperationTimeoutError`。不要解析异常文本或 Runtime 日志驱动业务逻辑。
