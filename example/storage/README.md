# Ti Cloud Storage Example

这个程序只使用公开 `tirtc.storage` API，验证录像日期与范围查询、Token 过期后的显式更新和重试、四类媒体 Output、Replay 控制、截图、边播边录、范围导出和逆序释放。

先设置凭据，再从已安装 `tirtc` wheel 的环境运行：

```bash
export TI_CLOUD_STORAGE_APP_ID=<app-id>
export TI_CLOUD_STORAGE_ACCESS_TOKEN=<access-token>
python main.py \
  --cache-dir /absolute/cache \
  --output-dir /absolute/output \
  --start-ms <unix-ms> \
  --end-ms <unix-ms>
```

服务返回 Token 过期时，程序从 `TI_CLOUD_STORAGE_REFRESHED_ACCESS_TOKEN` 读取新 Token，调用 `update_token()` 后显式重试；SDK 不自动刷新。`--help` 不需要凭据。默认音频 channel 是 0，视频 channel 是 1；两者必须是不同的 0..255 channel ID。程序在三分钟内没有取得完整回调或终态时以非零状态退出，凭据内容不会写入普通输出。
