# RTC Client Example

这个程序只使用公开 `tirtc` API，验证主动连接、decoded/encoded 音视频、command/stream message、关键帧、截图、边拉边录和逆序释放。

External Token 模式用于只持有短期连接 Token 的应用。凭据从环境变量读取：

```bash
export TIRTC_APP_ID=<app-id>
export TIRTC_TOKEN=<token>
python main.py \
  --auth-mode external-token \
  --device-id <device-id> \
  --cache-dir /absolute/cache \
  --output-dir /absolute/output
```

能够安全持有长期密钥的可信进程可以使用 Access Key 模式：

```bash
export TIRTC_APP_ID=<app-id>
export TIRTC_ACCESS_KEY_ID=<access-key-id>
export TIRTC_SECRET_KEY_ID=<access-key-secret>
python main.py \
  --auth-mode access-key \
  --device-id <device-id> \
  --cache-dir /absolute/cache \
  --output-dir /absolute/output
```

`--help` 不需要凭据。所有 Token 和 Access Key 凭据只通过环境变量传入，不进入命令行。默认音频流是 10，视频流是 11；两者必须是不同的 0..15 stream ID。程序在 90 秒内没有取得完整回调或终态时以非零状态退出，凭据内容不会写入普通输出。
