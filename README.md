# TiRTC Python SDK

`tirtc` 提供 headless RTC 接收与 Ti Cloud Storage 查询、回放和导出。它直接消费 Runtime public C，不在 Python 中重新实现连接、媒体处理、录像或云端协议。

当前支持普通 CPython 3.11 及之后的稳定版本，以及 macOS 11.5+ arm64、glibc Linux x86_64。binary wheel 已包含 Native 动态库和第三方许可证；安装和运行都不需要另外下载 Runtime，也不需要设置 `DYLD_LIBRARY_PATH` 或 `LD_LIBRARY_PATH`。

## 安装

公开发布后使用精确版本安装：

```bash
python -m pip install --only-binary=:all: tirtc==<version>
```

RTC 主链路从初始化开始，按 Output、Connection 的反向顺序释放：

```python
from pathlib import Path
import tirtc

tirtc.initialize(APP_ID, Path("/absolute/cache"))
try:
    with tirtc.Connection(on_state_changed=on_state) as connection:
        with tirtc.VideoOutput(on_frame=on_frame, on_error=on_error) as video:
            video.attach(connection, stream_id=0)
            connection.connect(DEVICE_ID, TOKEN)
            connection.subscribe_video(0)
            wait_for_frame()
            connection.unsubscribe_video(0)
            video.detach()
            connection.disconnect()
finally:
    tirtc.shutdown()
```

持续事件与媒体帧通过 callback 交付。Frame 是不可变 value，数据为 read-only `memoryview`；需要独立的可变或长期缓冲时显式调用 `bytes(frame.data)`。所有 Native 资源都提供明确的 `close()`、`stop()`、`wait()` 或 `delete()`，正常释放不依赖垃圾回收。

完整 RTC 程序见 [`example/client`](example/client)，Ti Cloud Storage 程序见 [`example/storage`](example/storage)。公开声明分别见仓库的 Python RTC 与 Ti Cloud Storage API Reference。
