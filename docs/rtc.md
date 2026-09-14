# Python RTC API Reference

TiRTC Python SDK 的 RTC 入口位于根 `tirtc` package。Client 是服务调用方和资源树 owner，可以创建多个 Connection。应用独立创建并持有 Output；媒体接收方向不改变 Client 的角色。

完整示例见 [`python/example/client`](../example/client)。

## Client 配置与鉴权

```python
@dataclass(frozen=True, slots=True)
class ClientOptions:
    app_id: str
    cache_dir: str | os.PathLike[str]
    endpoint: str | None = None
    console_log_enabled: bool = False

class Client:
    def __init__(
        self,
        options: ClientOptions,
        *,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
    ) -> None: ...
    def create_connection(
        self,
        *,
        on_state_changed=None,
        on_command=None,
        on_stream_message=None,
    ) -> Connection: ...
    def upload_logs(self) -> str: ...
    def close(self) -> None: ...
```

`cache_dir` 必须是绝对路径。ClientOptions 不包含凭据；AK/SK 只作为 Client 构造参数传入，不会成为 Client 字段、异常或公开结果。省略 AK/SK 时 Client 使用 External Token 模式；同时提供两项时使用 Access Key 模式。只提供一项、传入空字符串或 NUL 会在 Native 副作用前抛 `ValueError`。External Token 模式适用于只持有短期 Token 的应用；Access Key 模式只适用于能够安全保管长期密钥的可信进程。

同一进程同时只能有一个 RTC Client。Access Key 与 External Token 模式不能并存；前一个 Client 完整关闭后可以创建另一模式。RTC 与 Cloud Client 必须共享 cache 目录和 console 设置，但 App ID 与 endpoint 分别属于各 Client。

`close()` 幂等。它拒绝新资源和操作，取消在途 Connect，Detach 仍绑定的 RTC Output，关闭所有 Connection，并等待已经接受的 callback 退出。调用已关闭 Client 的 `upload_logs()` 抛 `ClosedError`。

## Connection

```python
class Connection:
    @property
    def state(self) -> ConnectionState: ...
    def connect(
        self,
        device_id: str,
        *,
        token: str | None = None,
        timeout: float = 30,
    ) -> None: ...
    def disconnect(self) -> None: ...
    def send_command(
        self, command_id: int, data: bytes | bytearray | memoryview
    ) -> None: ...
    def send_stream_message(
        self,
        stream_id: int,
        timestamp: timedelta,
        data: bytes | bytearray | memoryview,
    ) -> None: ...
    def subscribe_audio(self, stream_id: int) -> None: ...
    def unsubscribe_audio(self, stream_id: int) -> None: ...
    def subscribe_video(self, stream_id: int) -> None: ...
    def unsubscribe_video(self, stream_id: int) -> None: ...
    def request_video_keyframe(self, stream_id: int) -> None: ...
    def start_recording(
        self, *, video_stream_id: int, audio_stream_id: int | None = None
    ) -> RecordingTask: ...
    def close(self) -> None: ...
```

Connection 只能由 `Client.create_connection()` 创建，直接调用 `Connection()` 会抛 `TypeError`。Access Key Client 调用 `connect(device_id)`；External Token Client 调用 `connect(device_id, token=token)`。Access Key Client 传 Token，或 External Token Client 未传 Token，都会在 Native 副作用和状态变化前抛 `ValueError`。

`connect()` 是有界同步调用。成功返回时 `state is ConnectionState.CONNECTED`，应用可以立即 Subscribe。当前调用的 CONNECTING、CONNECTED、初始失败和取消不进入 `on_state_changed`；成功后的断线与后续状态变化才交给 callback。

`timeout` 接受正有限秒值，最大 120 秒。显式值向上取整为至少 1 ms；NaN、正负 Infinity、非正数和超过 120 秒的值在 Native 副作用前抛 `ValueError`。同一 Connection 的并发 Connect 抛 `InUseError`。失败、取消或断开后可以按原鉴权模式重连。

外部线程在 Connect 期间调用 `disconnect()` 或 `close()` 会取消当前 generation，等待已经接受的初始状态 callback 退出，再返回唯一终局。旧 generation 的晚到 CONNECTED 不会改变失败结果，也不能满足下一次 Connect。在任一 TiRTC callback 内调用同步 Connect 都会在状态或 Native 副作用前抛 `InUseError`。

Stream ID 范围是 `0..15`。Command ID 位于客户可用区间 `0x2001..0xffffffff`；较低区间由底层协议保留。Stream Message timestamp 是精确到整毫秒、位于 `0..0xffffffff` 毫秒的 `timedelta`。buffer 入参在同步调用返回前读完。

## Output 与 Frame

根包提供 `AudioOutput`、`VideoOutput`、`EncodedAudioOutput` 和 `EncodedVideoOutput`。它们都提供 `attach(connection, stream_id)`、`detach()`、只读 `state`、幂等 `close()` 和 context manager；decoded VideoOutput 另外提供 `take_snapshot()`。

四类 Output 的第一个 `on_frame` 参数必填。Attach 只建立 binding，不转移 ownership。Connection 或 Client 关闭时会先 Detach Output 并等待已接受 callback 退出，Output 本身保持 open。显式 Detach 或 parent 自动 Detach 后，Output 可以绑定到另一个仍存活的 Connection；应用最终负责关闭它。

decoded Output 可以配置：

```python
@dataclass(frozen=True, slots=True)
class OutputBufferOptions:
    strategy: OutputBufferStrategy = OutputBufferStrategy.AUTOMATIC
    max_buffer_watermark: timedelta | None = None
```

`AudioOutput` 的 `agc_level` 与 `ans_level` 可取 `disabled`、`low`、`medium`、`high`。`VideoOutput` decoder preference 可取 `auto`、`software`、`hardware`。

Frame 是 SDK 创建的不可变 value。媒体数据是 read-only `memoryview`；最后一个 Python Frame 引用释放时归还 Native retain。需要可变或脱离 Frame 生命周期的缓冲时显式复制为 `bytes(...)`。`source_time` 是 UTC-aware `datetime` 或 `None`。

callback pool 在第一次事件时按进程惰性创建。同一对象的 callback 串行执行，不同对象可能并发执行。video 每对象最多排队 2 帧，audio 最多 64 帧；背压只丢完整 Frame，下一帧设置 `discontinuity=True`，错误和终态不会被 Frame 挤掉。

## Recording、Snapshot 与临时文件

`Connection.start_recording()` 返回只公开 `stop()` 的 `RecordingTask`。`stop()` 同步等待唯一终局；重复或并发调用返回同一个 `RecordingFile` 或重抛同一个异常。

`VideoOutput.take_snapshot()` 返回 `SnapshotFile`。两个文件类型都提供只读 `path: Path`，Recording 另提供 `duration: timedelta`。路径位于 SDK cache；应用需要长期保存时先复制到自己的目录，再调用幂等 `delete()`，或使用 context manager 删除临时源。已经返回给应用的文件可以越过 Client 关闭继续保存或删除。

## Error、进程与释放

Python 可在进入 Native 前判断的类型和值域问题分别抛 `TypeError` 与 `ValueError`。Native 失败抛 `TiRTCError` 或稳定子类。调用方 deadline 使用 `OperationTimeoutError(code=None)`，未知 Native code 保留为普通 `TiRTCError`。

在当前 Client 或 descendant callback 中同步关闭 Client 会形成自等待时，`close()` 在任何 teardown 前抛 `InUseError`。并发 close 共享同一终局。正常释放顺序是完成 RecordingTask、Detach/关闭 Output、Disconnect/关闭 Connection、关闭 Client；Client 级联关闭也能完成同一资源收口。

import `tirtc` 不创建线程、目录、网络连接或 Runtime 状态。父进程仅 import 后 fork 时，子进程可以创建自己的 Client；任一 Client 活跃后 fork，子进程首次调用 SDK 抛 `UnsupportedError(code=None, name="forked_process")`，并且必须立即 `exec` 或 `os._exit()`。
