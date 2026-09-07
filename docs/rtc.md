# Python RTC API Reference

Python RTC SDK 的 distribution 与 import package 都是 `tirtc`。它面向 headless 主动连接与接收场景，支持普通 CPython 3.11 及之后的稳定版本，以及 macOS 11.5+ arm64、glibc 2.35+ Linux x86_64。发布 wheel 使用 `cp311-abi3`，已经包含当前平台的 Runtime 动态库闭包和许可证；应用不下载 Runtime，也不设置 loader 环境变量。macOS x86_64、Windows、Linux arm64、subinterpreter 与 free-threaded CPython 不受支持。

接入前由应用准备 App ID、目标设备的 Remote ID 与连接 Token，以及要接收的音视频 Stream ID。SDK 不签发 Token，也不推断目标设备或 Stream。

下面的代码只展示对象与调用顺序；完整可运行程序会同时等待连接成功或原始连接错误，见 [`python/example/client`](../example/client)。

```python
from pathlib import Path
import tirtc

tirtc.initialize(APP_ID, Path("/absolute/cache"), endpoint=ENDPOINT)
try:
    with tirtc.Connection(on_state_changed=on_state) as connection:
        with tirtc.VideoOutput(on_frame=on_frame, on_error=on_error) as video:
            video.attach(connection, stream_id=0)
            connection.connect(DEVICE_ID, TOKEN)
            wait_until_connected_or_error()
            connection.subscribe_video(0)
            wait_for_frame()
            connection.unsubscribe_video(0)
            video.detach()
            connection.disconnect()
finally:
    tirtc.shutdown()
```

## 生命周期

```python
def initialize(app_id: str, cache_dir: str | os.PathLike[str], *,
               endpoint: str | None = None,
               console_log_enabled: bool = False) -> None: ...
def shutdown() -> None: ...
def upload_logs() -> str: ...
def error_name(code: int) -> str: ...
```

`app_id` 和绝对 `cache_dir` 必填。相同配置重复初始化成功；已有不同 RTC 配置时抛 `AlreadyInitializedError`，现有状态不变。RTC 与 `tirtc.storage` 可以使用不同 App ID 和 endpoint，但必须共享 cache dir 与 console log 设置。默认 `console_log_enabled=False` 关闭 Python 与 Runtime 自有 console sink；当前 Nano 依赖可能在初始化时直接输出一条带 ANSI 的 Build Info，这不受该开关控制。

`shutdown()` 在 Connection、Output 或 RecordingTask 仍活动时抛 `InUseError`。`upload_logs()` 会发生网络上传，应用应先满足自己的授权与隐私政策。

## Connection

```python
class Connection:
    def __init__(self, *, on_state_changed=None,
                 on_command=None, on_stream_message=None) -> None: ...
    @property
    def state(self) -> ConnectionState: ...
    def connect(self, remote_id: str, token: str) -> None: ...
    def disconnect(self) -> None: ...
    def send_command(self, command_id: int, data: bytes | bytearray | memoryview) -> None: ...
    def send_stream_message(self, stream_id: int, timestamp: timedelta,
                            data: bytes | bytearray | memoryview) -> None: ...
    def subscribe_audio(self, stream_id: int) -> None: ...
    def unsubscribe_audio(self, stream_id: int) -> None: ...
    def subscribe_video(self, stream_id: int) -> None: ...
    def unsubscribe_video(self, stream_id: int) -> None: ...
    def request_video_keyframe(self, stream_id: int) -> None: ...
    def start_recording(self, *, video_stream_id: int,
                        audio_stream_id: int | None = None) -> RecordingTask: ...
    def close(self) -> None: ...
```

Stream ID 范围是 `0..15`。`send_command()` 的 Command ID 位于客户可用区间 `0x2001..0xffffffff`，较低区间由底层协议保留；Stream Message timestamp 是精确到整毫秒、位于 `0..0xffffffff` 毫秒的 `timedelta`。buffer 入参在同步调用返回前读完。Output 可以先 Attach；`connect()` 成功返回只表示请求已受理，应用必须等待 `on_state_changed` 报告 `connected` 后再显式 Subscribe。Connection 仍被 Output 或 RecordingTask 使用时，`close()` 抛 `InUseError`，不会先做部分释放；重复关闭成功。

同一对象的并发 `close()` 共享一次 Native 释放及其成功或失败结果；关闭开始后进入的新操作抛 `ClosedError`。被接受的 `close()` 先停止接收新 Python callback，等待已经进入的 callback 返回，再执行 Native 终局，不会在用户代码仍使用对象时销毁句柄。终局阶段失败后对象保持关闭操作态，下一次显式 `close()` 只重试尚未完成的步骤。

`ConnectionState` 的值为 `idle`、`connecting`、`connected`、`disconnected`。连接结果、command 和 stream message 通过构造器 callback 交付。

## Output 与 Frame

根包提供 `AudioOutput`、`VideoOutput`、`EncodedAudioOutput`、`EncodedVideoOutput`。它们都提供 `attach(connection, stream_id)`、`detach()`、只读 `state`、幂等 `close()` 和 context manager；只有 decoded `VideoOutput` 另外提供 `take_snapshot()`。

四类 Output 的第一个 `on_frame` 参数必填且不能为 `None`；`on_state_changed` 与 `on_error` 是 keyword-only 可选 callback。没有用户 handler 且不承担内部状态职责的 callback 不会注册到 Native。

decoded Output 可以配置：

```python
@dataclass(frozen=True, slots=True)
class OutputBufferOptions:
    strategy: OutputBufferStrategy = OutputBufferStrategy.AUTOMATIC
    max_buffer_watermark: timedelta | None = None
```

`AudioOutput` 另接受 `agc_level` 与 `ans_level`，取值为 `disabled`、`low`、`medium`、`high`。`VideoOutput` 的 decoder preference 为 `auto`、`software`、`hardware`。Output state 为 `idle`、`buffering`、`delivering`、`failed`、`paused`、`completed`。

Frame 是 SDK 创建的不可变 value：

- `AudioFrame`：`data`、`pts`、`source_time`、`sample_format`、`sample_rate_hz`、`channels`、`samples_per_channel`、`discontinuity`；
- `VideoFrame`：`pts`、`source_time`、`pixel_format`、`width`、`height`、`planes`、`discontinuity`；
- `EncodedAudioFrame`：`data`、`codec_config`、`pts`、`source_time`、`codec`、`bitstream_format`、`sample_rate_hz`、`channels`、`discontinuity`；
- `EncodedVideoFrame`：在通用编码字段之外提供 `width`、`height`、`key_frame` 与 `discontinuity`。

媒体数据是 read-only `memoryview`，最后一个 Python Frame 引用释放时归还 Native retain。需要可变或脱离 Frame 生命周期的缓冲时显式复制为 `bytes(...)`。`source_time` 是 UTC-aware `datetime` 或 `None`。

callback pool 在第一次有事件时按进程惰性创建；同一对象的 callback 串行执行，不同对象的 callback 可能并发执行且不承诺彼此顺序，共享应用状态时由调用方同步。一个阻塞 callback 不会阻塞另一个对象的下一项工作。Runtime callback 线程不执行用户 Python 代码。video 每对象最多排队 2 帧，audio 最多排队 64 帧；背压只丢完整 Frame，下一帧设置 `discontinuity=True`，错误和终态不会被 Frame 挤掉。callback 应快速返回，长工作交给应用自己的 executor 或 queue。用户 callback 抛出的异常交给 `sys.unraisablehook`，不会穿过 Native 边界，也不会停止后续 callback。在对象自己的 callback 中同步关闭该对象或直接关联对象会抛 `InUseError`。

## Recording、Snapshot 与临时文件

`Connection.start_recording(...)` 返回只公开 `stop()` 的 `RecordingTask`。`stop()` 同步等待唯一终局；重复或并发调用返回同一个 `RecordingFile` 或重抛同一个异常。Recording 必须选择一路视频，可选一路不同 ID 的音频。

`VideoOutput.take_snapshot()` 返回 `SnapshotFile`。两个文件类型都提供只读 `path: Path`，Recording 另提供 `duration: timedelta`。路径位于 SDK cache；应用需要长期保存时先复制到自己的目录，再调用幂等 `delete()`。文件对象可以作为 context manager，退出时删除临时源。

正常退出顺序是 RecordingTask `stop()` → Output `detach()` / `close()` → Connection `disconnect()` / `close()` → `tirtc.shutdown()`。正常资源管理不依赖 finalizer。遗失的已绑定 Output 或活动 RecordingTask 会发出 `ResourceWarning` 并 best-effort 安全终结；只有 Native checked destroy 成功后才归还 parent 占用，无法安全终结时 parent 继续正确地抛 `InUseError`。

## Error

Python 可在进入 Native 前判断的类型和值域问题分别抛 `TypeError` 与 `ValueError`。Native 失败抛 `TiRTCError` 或稳定子类；所有公开异常都可由应用按 `TiRTCError(message="", *, code: int | None = None, name: str = "unknown")` 的签名构造，便于 mock 与异常分支测试。SDK 仍独占 Native code 到稳定子类的映射；`code` 为原始整数或 `None`，`name` 是稳定错误名。调用方 deadline 使用 `OperationTimeoutError(code=None)`，未知 Native code 保留为普通 `TiRTCError`。

import `tirtc` 不创建线程、目录、网络连接或 Runtime 状态。父进程仅 import 后 fork 时，子进程可按需创建自己的 callback pool；父进程已经 initialize 或持有资源后 fork，子进程首次调用 SDK 抛 `UnsupportedError(code=None, name="forked_process")`。这种子进程不能执行普通 Python 解释器 teardown，必须立即 `exec` 或 `os._exit()`；多进程程序应改为在 worker 内 initialize 或使用 `spawn`。

稳定子类包括参数与生命周期、连接授权、资源媒体、日志和 Cloud 共用类别：`InvalidArgumentError`、`NotInitializedError`、`AlreadyInitializedError`、`InUseError`、`NotStartedError`、`NotConnectedError`、`NotBoundError`、`NotConfiguredError`、`ClosedError`、`AuthenticationError`、`OperationTimeoutError`、`RemoteClosedError`、`TokenExpiredError`、`PermissionDeniedError`、`ResourceExhaustedError`、`UnsupportedError`、`UnsupportedFormatError`、`TiRTCIOError`、`NoFrameError`、`NoRecordableMediaError`、`RecordingOverrunError`、`LogExportError`、`LogUploadError`、`CancelledError`、`RangeTooLargeError`、`RecordingUnreadableError`、`UnavailableError`、`NetworkUnavailableError`、`EndpointDNSResolutionFailedError` 与 `StoppedError`。
