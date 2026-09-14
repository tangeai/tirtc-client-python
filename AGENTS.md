# Python SDK AGENTS

- `python/` 是 `tirtc` distribution 与 import package 的唯一 owner。根包的 `Client` 提供主动 RTC 接收，`tirtc.storage.Client` 提供 Ti Cloud Storage；两者复用根包 `ClientOptions`，并分别拥有自己的 child 资源树。
- RTC 与 Cloud Client 都直接用 `ClientOptions` 构造，并以成对的可选 AK/SK 固定鉴权模式。AK/SK 不进入命令行、日志或保留 evidence；未传 AK/SK 时，External Token 由应用传给 RTC Connect 或 Cloud per-device open。模块级 initialize/shutdown、独立 Connection 和独立 CloudStorage 构造不属于公开面。
- Python 只消费 Runtime public C。Native 接线只位于 `native/extension.c`；Python 层不得重写连接、媒体处理、录像或云端协议。
- 当前发布 tuple 是 `darwin/arm64` 与 `linux/amd64`，支持普通 CPython 3.11 及之后的稳定版本。wheel 使用 CPython Limited API，发布 tag 为 `cp311-abi3`。
- wheel 必须携带对应 Runtime 动态库和许可证，只从包内相对路径加载。不得在安装或 import 时下载 Runtime，也不得要求 loader 环境变量。
- Runtime callback 只把事件交给按进程惰性创建的私有有界 callback pool；同一对象的用户 callback 串行执行。video 每对象最多排队 2 帧，audio 最多 64 帧；丢帧后下一帧设置 `discontinuity=True`，终态不能被 frame/time 事件挤掉。
- Replay/Export 的 pool 排队并执行 Runtime-owned opaque callback task，使用户回调留在 Native generation 与停止屏障内；每个已接受 task 必须执行一次，不能二次排队其用户事件，也不能因 handler 释放而丢弃 task。Replay 自身或关联 Output 回调内的非阻塞操作遇到外部操作占锁时直接返回 `InUseError`。
- `Client`、`Connection`、Output、`CloudStorage` 与 `Replay` 的 `close()` 幂等。Client 级联关闭自己的 child 并自动 Detach 独立 Output；关联 callback 内会形成自等待的关闭先返回 `InUseError`，不得先做部分 teardown。外部关闭等待 callback barrier，并发调用共享同一次终局。Recording/Export 的终态只由 `stop()` / `wait()` 取得。
- `example/client/` 与 `example/storage/` 是唯一 canonical Examples，也是 owner real smoke client。测试不得另建私有 acceptance 程序替代它们。
- `script/build_candidate.sh` 只接受同源 clean-unpack Runtime 输入，`script/python_verify.sh --platform all --mode full` 串行执行两个平台各一条 contract、两种 RTC auth smoke 和两种 Cloud auth smoke，共十条 lane。两种 auth lane 都运行同一个 canonical Example，凭据只从环境变量传入。外部写入按当前用户目标和 owner 工作流执行。`python-test` / `full-test` 合同要求的有界录像上传与日志 roundtrip 属于测试；制品发布、公开仓写入、Samples Push 和测试合同外上传仍须有匹配目标。
