# TiRTC Python SDK

`tirtc` 提供 headless RTC 接收与 Ti Cloud Storage 查询、回放和导出。根包承接 RTC，`tirtc.storage` 承接 Ti Cloud Storage；两者直接消费包内 Runtime public C，不在 Python 中重新实现媒体、鉴权或云端协议。

SDK 支持两种鉴权模式。External Token 模式面向只持有短期 Token 的桌面、边缘和业务应用；Access Key 模式面向能够安全持有长期 AK/SK 的可信进程。两种模式使用相同的 Client、child、Output 和文件 API。Client 表示服务调用方和本地资源树 owner，不表示部署位置或媒体方向。

支持普通 CPython 3.11 及之后的稳定版本、macOS 11.5+ arm64，以及 glibc 2.35+ Linux x86_64。binary wheel 已包含对应 Runtime 动态库和第三方许可证，不下载 Runtime，也不需要 loader 环境变量。macOS x86_64、Windows、Linux arm64、subinterpreter 和 free-threaded CPython 不在支持范围。

## 安装

只安装 binary wheel：

```bash
python -m pip install --only-binary=:all: tirtc==2.6.0a3

```

## RTC

External Token 模式从应用服务端取得短期连接 Token。同步 `connect()` 返回时，Connection 已经到达 `CONNECTED`；初始连接状态不重复进入用户 callback。

```python
import os
from pathlib import Path
from threading import Event

import tirtc

frame_received = Event()
failure: list[tirtc.TiRTCError] = []


def on_later_state(
    state: tirtc.ConnectionState, error: tirtc.TiRTCError | None
) -> None:
    if error is not None:
        failure.append(error)


def on_output_error(error: tirtc.TiRTCError) -> None:
    failure.append(error)
    frame_received.set()


options = tirtc.ClientOptions(
    app_id=os.environ["TIRTC_APP_ID"],
    cache_dir=Path(os.environ["TIRTC_CACHE_DIR"]).resolve(),
)
with tirtc.Client(options) as client:
    with client.create_connection(on_state_changed=on_later_state) as connection:
        with tirtc.VideoOutput(
            lambda frame: frame_received.set(), on_error=on_output_error
        ) as video:
            stream_id = int(os.environ["TIRTC_VIDEO_STREAM_ID"])
            video.attach(connection, stream_id)
            connection.connect(
                os.environ["TIRTC_DEVICE_ID"],
                token=os.environ["TIRTC_TOKEN"],
            )
            connection.subscribe_video(stream_id)
            if not frame_received.wait(30):
                raise TimeoutError("timed out waiting for a video frame")
            if failure:
                raise failure[0]
            connection.unsubscribe_video(stream_id)
            video.detach()
            connection.disconnect()
```

可信进程在构造同一种 Client 时提供 Access Key，后续媒体 API 不变：

```python
with tirtc.Client(
    options,
    access_key_id=os.environ["TIRTC_ACCESS_KEY_ID"],
    access_key_secret=os.environ["TIRTC_SECRET_KEY_ID"],
) as client:
    with client.create_connection() as connection:
        connection.connect(os.environ["TIRTC_DEVICE_ID"])
```

## Ti Cloud Storage

External Token 模式接收应用服务端下发、已绑定设备的短期 APP Access Token。Cloud Client 打开每设备 `CloudStorage` child；查询、Replay 和 Export 都由这个 child 创建。

```python
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil

import tirtc
import tirtc.storage as storage

options = tirtc.ClientOptions(
    app_id=os.environ["TI_CLOUD_STORAGE_APP_ID"],
    cache_dir=Path(os.environ["TI_CLOUD_STORAGE_CACHE_DIR"]).resolve(),
)
epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
start = epoch + timedelta(
    milliseconds=int(os.environ["TI_CLOUD_STORAGE_START_MS"])
)
end = epoch + timedelta(milliseconds=int(os.environ["TI_CLOUD_STORAGE_END_MS"]))

with storage.Client(options) as client:
    with client.open_with_token(
        os.environ["TI_CLOUD_STORAGE_ACCESS_TOKEN"]
    ) as cloud:
        ranges = cloud.list_recordings(start, end, timeout=30)
        if not ranges:
            raise RuntimeError("no recording is available in the requested window")
        selected = ranges[0]
        task = cloud.export_recording(
            selected.start_time,
            selected.end_time,
            video_channel_id=int(
                os.environ["TI_CLOUD_STORAGE_VIDEO_CHANNEL_ID"]
            ),
        )
        with task.wait(timeout=120) as recording:
            if not task.report.complete:
                raise RuntimeError("recording export is incomplete")
            shutil.copyfile(recording.path, Path("recording.mp4"))
```

可信进程用 AK/SK 打开明确设备，之后复用同一个 `CloudStorage` API：

```python
with storage.Client(
    options,
    access_key_id=os.environ["TI_CLOUD_STORAGE_ACCESS_KEY_ID"],
    access_key_secret=os.environ["TI_CLOUD_STORAGE_ACCESS_KEY_SECRET"],
) as client:
    with client.open_device(os.environ["TI_CLOUD_STORAGE_DEVICE_ID"]) as cloud:
        ranges = cloud.list_recordings(start, end, timeout=30)
```

External Token child 遇到 Token 过期时，由应用取得新 Token、调用 `update_token()` 并显式重试。Access Key child 的签发与刷新由 Runtime 管理，不接受 `update_token()`。

## 生命周期与 callback

`ClientOptions` 只保存 App ID、绝对 cache 目录、可选 endpoint 和 console 开关，不保存凭据。RTC Client 在同一进程中单实例；Cloud Client 可以多实例。RTC 与 Cloud 共用进程级 cache 目录和 console 配置。关闭 Client 会取消在途工作、关闭它创建的 Connection 或 CloudStorage 树，并等待已接受的 callback 退出。

四类 Output 由应用独立创建和持有。Attach 只建立 binding；关闭 Connection、Replay、CloudStorage 或 Client 会 Detach 对应 Output，但不会关闭它。Output 可以重新绑定到仍存活的同类来源，应用最终用 context manager 或幂等 `close()` 释放。

持续事件由私有 callback pool 交付。同一对象的 callback 串行执行，不同对象的 callback 可能并发执行且不承诺彼此顺序；共享应用状态时自行同步。callback 应快速返回，长工作交给应用自己的 executor 或 queue。Frame 是不可变 value，媒体数据为 read-only `memoryview`；需要脱离 Frame 生命周期时显式复制为 `bytes(frame.data)`。

`connect()` 是有界同步调用。没有 AK/SK 的 Client 必须通过 keyword-only `token` 传入短期 Token；已经提供 AK/SK 的 Client 不接受 Token。这两种错误都会在 Native 副作用前抛 `ValueError`。在任何 TiRTC callback 内调用 `connect()` 会在 Native 副作用前抛 `InUseError`。外部线程调用 `close()` 会停止新 callback，等待已经进入的 callback 返回，再完成 Native 释放；callback 内会形成自等待的关闭也会在部分 teardown 前抛 `InUseError`。

import 不创建线程、目录、网络连接或 Runtime 状态。父进程仅 import 后可以安全 `fork()`；任一 Client 活跃后 fork，子进程首次调用 SDK 会抛 `UnsupportedError(name="forked_process")`。这种子进程必须立即 `exec` 或 `os._exit()`；需要多进程时在 worker 内创建 Client，或使用 `spawn`。

完整程序见 [RTC Example](example/client) 与 [Ti Cloud Storage Example](example/storage)。公开合同由本 README、包内类型声明和这两个 canonical Example 共同承接。

## 仓库入口

测试责任、实现、候选证据与缺口见 [源仓库 TEST.html](https://gitlab.tange-ai.com/tirtc-nexus/tirtc/-/blob/develop/python/TEST.html)，准备与执行使用 [python-test](https://gitlab.tange-ai.com/tirtc-nexus/tirtc/-/blob/develop/.agents/skills/python-test/SKILL.md)。公开发布使用 [sdk-release-publish](https://gitlab.tange-ai.com/tirtc-nexus/tirtc/-/blob/develop/.agents/skills/sdk-release-publish/SKILL.md)。
