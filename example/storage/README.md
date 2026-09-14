# Ti Cloud Storage Example

这个程序只使用公开 `tirtc.storage` API，验证录像日期与范围查询、Token 过期后的显式更新和重试、四类媒体 Output、Replay 控制、截图、边播边录、范围导出和逆序释放。

External Token 模式用于由应用服务端下发绑定设备的短期 APP Access Token：

```bash
export TI_CLOUD_STORAGE_APP_ID=<app-id>
export TI_CLOUD_STORAGE_ACCESS_TOKEN=<access-token>
python main.py \
  --auth-mode external-token \
  --cache-dir /absolute/cache \
  --output-dir /absolute/output \
  --start-ms <unix-ms> \
  --end-ms <unix-ms>
```

能够安全持有长期密钥的可信进程可以使用 Access Key 模式；Runtime 为这个模式签发和刷新 Token：

```bash
export TI_CLOUD_STORAGE_APP_ID=<app-id>
export TI_CLOUD_STORAGE_ACCESS_KEY_ID=<access-key-id>
export TI_CLOUD_STORAGE_ACCESS_KEY_SECRET=<access-key-secret>
export TI_CLOUD_STORAGE_DEVICE_ID=<device-id>
python main.py \
  --auth-mode access-key \
  --cache-dir /absolute/cache \
  --output-dir /absolute/output \
  --start-ms <unix-ms> \
  --end-ms <unix-ms>
```

External Token 模式遇到 Token 过期时，从 `TI_CLOUD_STORAGE_REFRESHED_ACCESS_TOKEN` 读取新 Token，调用 `update_token()` 后显式重试。Access Key 模式不接受 `update_token()`，Token 刷新由 Runtime 完成。`--help` 不需要凭据。所有 Token 和 Access Key 凭据只通过环境变量传入，不进入命令行。默认音频 channel 是 0，视频 channel 是 1；两者都是 `0..255`，也可以使用相同数值。程序在三分钟内没有取得完整回调或终态时以非零状态退出，凭据内容不会写入普通输出。
