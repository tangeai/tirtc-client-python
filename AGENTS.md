# Python SDK AGENTS

- `python/` 是 `tirtc` distribution 与 import package 的唯一 owner。根包提供主动 RTC 接收，`tirtc.storage` 提供独立的 Ti Cloud Storage 生命周期与 API。
- Python 只消费 Runtime public C。Native 接线只位于 `native/extension.c`；Python 层不得重写连接、媒体处理、录像或云端协议。
- 当前发布 tuple 是 `darwin/arm64` 与 `linux/amd64`，支持普通 CPython 3.11 及之后的稳定版本。wheel 使用 CPython Limited API，发布 tag 为 `cp311-abi3`。
- wheel 必须携带对应 Runtime 动态库和许可证，只从包内相对路径加载。不得在安装或 import 时下载 Runtime，也不得要求 loader 环境变量。
- Runtime callback 只把事件交给私有有界 dispatcher；用户 callback 在 SDK callback 线程串行执行。丢帧后下一帧设置 `discontinuity=True`，终态不能被 frame/time 事件挤掉。
- `Connection`、Output、`CloudStorage` 与 `Replay` 的 `close()` 幂等。占用关系和 callback 内关闭先返回 `InUseError`，不得先做部分 teardown。Recording/Export 的终态只由 `stop()` / `wait()` 取得。
- `example/client/` 与 `example/storage/` 是唯一 canonical Examples，也是 owner real smoke client。测试不得另建私有 acceptance 程序替代它们。
- `script/build_candidate.sh` 只接受同源 clean-unpack Runtime 输入，`script/python_verify.sh` 串行执行 contract 与两个 Example smoke。发布、上传、公开仓写入和 API Samples Push 仍需用户明确授权。
