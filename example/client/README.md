# RTC Client Example

这个程序只使用公开 `tirtc` API，验证主动连接、decoded/encoded 音视频、command/stream message、关键帧、截图、边拉边录和逆序释放。

先设置凭据，再从已安装 `tirtc` wheel 的环境运行：

```bash
export TIRTC_APP_ID=<app-id>
export TIRTC_TOKEN=<token>
python main.py \
  --remote-id <device-id> \
  --cache-dir /absolute/cache \
  --output-dir /absolute/output
```

`--help` 不需要凭据。默认音频流是 10，视频流是 11；两者必须是不同的 0..15 stream ID。程序在 90 秒内没有取得完整回调或终态时以非零状态退出，凭据内容不会写入普通输出。
