# TiRTC Python SDK

`tirtc` 提供 headless RTC 接收与 Ti Cloud Storage 查询、回放和导出。根包承接 RTC，`tirtc.storage` 承接 Ti Cloud Storage；两者直接消费包内 Runtime public C，不在 Python 中重新实现媒体或云端协议。

支持普通 CPython 3.11 及之后的稳定版本、macOS 11.5+ arm64，以及 glibc 2.35+ Linux x86_64。binary wheel 已包含对应 Runtime 动态库和第三方许可证，不下载 Runtime，也不需要 loader 环境变量。macOS x86_64、Windows、Linux arm64、subinterpreter 和 free-threaded CPython 不在支持范围。

## 安装

只安装 binary wheel：

```bash
python -m pip install --only-binary=:all: tirtc==2.5.0a1

```

## RTC quickstart

开始前由应用准备 App ID、目标设备 ID、连接 Token 和要接收的 Stream ID。`connect()` 返回只表示连接请求已受理；收到 `CONNECTED` 状态后再订阅媒体。

```python
import os
from pathlib import Path
from threading import Event

import tirtc

connected = Event()
frame_received = Event()
failure = []


def on_state_changed(state, error):
    if error is not None:
        failure.append(error)
        connected.set()
        frame_received.set()
    elif state is tirtc.ConnectionState.CONNECTED:
        connected.set()


def on_output_error(error):
    failure.append(error)
    frame_received.set()


tirtc.initialize(
    os.environ["TIRTC_APP_ID"], Path(os.environ["TIRTC_CACHE_DIR"]).resolve()
)
try:
    with tirtc.Connection(on_state_changed=on_state_changed) as connection:
        with tirtc.VideoOutput(
            lambda frame: frame_received.set(), on_error=on_output_error
        ) as video:
            stream_id = int(os.environ["TIRTC_VIDEO_STREAM_ID"])
            video.attach(connection, stream_id)
            connection.connect(
                os.environ["TIRTC_REMOTE_ID"], os.environ["TIRTC_TOKEN"]
            )
            if not connected.wait(30):
                raise TimeoutError("timed out connecting to the device")
            if failure:
                raise failure[0]
            connection.subscribe_video(stream_id)
            if not frame_received.wait(30):
                raise TimeoutError("timed out waiting for a video frame")
            if failure:
                raise failure[0]
finally:
    tirtc.shutdown()
```

## Ti Cloud Storage quickstart

开始前由应用服务端下发绑定目标设备的短期 APP Access Token，并从设备配置或业务服务取得要播放的 Channel ID。查询成功但没有录像时返回空 list。

```python
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import tirtc.storage as storage

outcome = Event()
frame_received = Event()
failure = []


def on_replay_error(error):
    failure.append(error)
    outcome.set()


def on_frame(frame):
    frame_received.set()
    outcome.set()


storage.initialize(
    os.environ["TI_CLOUD_STORAGE_APP_ID"],
    Path(os.environ["TI_CLOUD_STORAGE_CACHE_DIR"]).resolve(),
)
try:
    with storage.CloudStorage(
        os.environ["TI_CLOUD_STORAGE_ACCESS_TOKEN"]
    ) as cloud:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        start = epoch + timedelta(
            milliseconds=int(os.environ["TI_CLOUD_STORAGE_START_MS"])
        )
        end = epoch + timedelta(
            milliseconds=int(os.environ["TI_CLOUD_STORAGE_END_MS"])
        )
        ranges = cloud.list_recordings(start, end, timeout=30)
        if not ranges:
            raise RuntimeError("no recording is available in the requested window")
        selected = ranges[0]
        with cloud.create_replay(
            on_completed=outcome.set, on_error=on_replay_error
        ) as replay:
            with storage.VideoOutput(
                on_frame, on_error=on_replay_error
            ) as video:
                video.attach(
                    replay, int(os.environ["TI_CLOUD_STORAGE_VIDEO_CHANNEL_ID"])
                )
                replay.play(selected.start_time, selected.end_time)
                if not outcome.wait(30):
                    raise TimeoutError("timed out waiting for replay media")
                if failure:
                    raise failure[0]
                if not frame_received.is_set():
                    raise RuntimeError("the selected channel has no video media")
                replay.stop()
finally:
    storage.shutdown()
```

所有 Output 的 frame callback 都是必填项。持续事件由私有 callback pool 交付，同一对象保持串行，不同对象的 callback 可能并发执行且不承诺彼此顺序；共享应用状态时自行同步。callback 应快速返回，长工作交给应用自己的 executor 或 queue。Frame 是不可变 value，媒体数据为 read-only `memoryview`；需要脱离 Frame 生命周期时显式复制为 `bytes(frame.data)`。

外部线程调用 `close()` 会停止接收新 callback，等待已经进入的 callback 返回，再完成一次 Native 释放。在对象自己或直接关联对象的 callback 中同步关闭会抛 `InUseError`。显式 `close()`、`stop()`、`wait()` 和 `delete()` 仍是正常释放路径；遗失的子资源只做带 `ResourceWarning` 的 best-effort 安全终结。

import 不创建线程、目录、网络连接或 Runtime 状态。父进程仅 import 后可以安全 `fork()`，子进程按需创建自己的 callback pool；父进程已经 initialize 或创建资源后再 fork，子进程首次调用 SDK 会抛 `UnsupportedError(name="forked_process")`。这种子进程不支持普通 Python 解释器 teardown，必须立即 `exec` 或 `os._exit()`；需要多进程时应在 worker 内 initialize，或使用 `spawn`。

完整程序见 [`example/client`](example/client) 与 [`example/storage`](example/storage)。公开合同见 [RTC API Reference](docs/rtc.md) 和 [Ti Cloud Storage API Reference](docs/ti-cloud-storage.md)。
