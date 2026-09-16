---
name: dlna-push
description: 把本地媒体推到局域网的 DLNA 渲染设备(电视/投屏盒子/EZCast)并排查推流故障。用于：搜不到设备、电视静默拒播、拖进度每次从头开始载、HLS 推不动、老化测试把设备跑崩。当出现 DLNA、UPnP、DMC、投屏、MediaRenderer、SetAVTransportURI、DIDL、protocolInfo、SSDP、M-SEARCH、AVTransport、EZCast、乐播 等字样，或用户要把视频/音频/图片推到电视上播放时使用。
---

# DLNA 推流与排障

驱动 `dlna_push.py`（纯标准库的 DLNA 控制点，无 pip 依赖）完成推流、播放控制、
故障定位和老化测试。

## 先决条件

- Linux 主机（SSDP 组播 + `SIOCGIFADDR` 查网卡地址都是 Linux 行为）。树莓派是主要目标。
- Python 3.8+，无第三方包。转封装功能另需 `ffmpeg`。
- **主机和电视必须在同一个二层网段**。有线和 WiFi 共存时必须用 `-i` 指定网卡，
  否则 SSDP 从错的网段发出去，症状是一台设备都搜不到。

## 定位工具

按顺序找 `dlna_push.py`：

1. 环境变量 `$DLNA_PUSH`
2. 本 skill 目录的上一层（`dlna_skill/` 的父目录就是项目根）
3. `command -v dlna_push.py`

找不到就告诉用户路径，不要猜。

## 非交互驱动（重要）

推流成功后程序会进交互控制台。agent 场景下**必须用管道喂命令**，否则会挂住：

```bash
printf 'i\nq\n' | ./dlna_push.py -i wlan0 /path/to/a.mp4 0
```

控制台读到 EOF 会干净退出（关 HTTP 服务、清 ffmpeg 产物）。可用命令见
`h`，常用的是 `seek <秒>`、`i`（状态/进度/音量）、`vol <0-100>`、`stop`、
`open <文件>`（换片）。

只列设备不推流用 `list`，这个本来就不进控制台：

```bash
./dlna_push.py -i wlan0 list
```

## 常用配方

| 目的 | 命令 |
|---|---|
| 列设备 | `./dlna_push.py -i wlan0 list` |
| 推一个视频后退出 | `printf 'q\n' \| ./dlna_push.py -i wlan0 /p/a.mp4 0` |
| 推完看一眼进度 | `printf 'i\nq\n' \| ./dlna_push.py -i wlan0 /p/a.mp4 0` |
| 验证拖进度 | `printf 'seek 90\ni\nq\n' \| ./dlna_push.py -i wlan0 /p/a.mp4 0 --http-log` |
| 老电视放不了 mkv | 加 `-f ts`（重封装成 MPEG-TS，只换容器不重编码） |
| 搜不到但能 ping 通 | 设备位置直接给 IP，会自动转直连；端口不是 49152 时加 `--port` |
| 老化测试 | `./dlna_push.py -i wlan0 /p/a.mp4 <ip> --stress --subscribe --burst 4 --max-rounds 120 --interval 2.0` |

设备可以用 **序号 / 名称子串 / IP** 指定，三者等价。

## 排障入口

先按症状定位，细节和判读方法在 [references/troubleshooting.md](references/troubleshooting.md)：

| 症状 | 先看哪里 |
|---|---|
| 一台设备都搜不到 | 网卡选错（`-i`）；或 AP 隔离了组播 —— 直接给 IP 转直连 |
| `找不到本地文件` | 相对路径按**当前工作目录**解析，没有默认媒体目录。给绝对路径 |
| 电视收了但不播，无任何报错 | MIME / `upnp:class` 对不上，典型是 `.m3u8` 或 `.mkv` |
| **拖进度每次从头开始载** | `DLNA.ORG_OP`。**这是最常被误判成"HTTP 不支持 Range"的一类** |
| HLS 推不动 | 电视不一定支持 HLS（非 DLNA 标准），换 `-f ts` |
| 老化跑着跑着设备失联 | 退出码 3，看 `--subscribe` 的 LastChange 事件路径 |

## 判读 `--http-log`

`--http-log` 会打印电视发来的每个 HTTP 请求。**状态码就是结论**：

- 起播时 `200` = 电视没带 Range，整档从 0 拉（多数设备起播就是这样）。
- 拖进度后出现 `206` = 电视在发 `Range: bytes=<非零>-`，byte-seek 真的生效。
- 拖进度后又是 `200`（或行为和起播一模一样）= 整档重拉，seek 没生效。

单独用 `curl` 只能验证 server 端，**不能**证明电视会用：

```bash
curl -sSI "http://<本机IP>:<端口>/media/<文件名>" | grep -iE 'accept-ranges|dlna'
```

端口每次启动都不一样（绑 port 0 由内核分配），从程序打印的
`[*] 内置 HTTP 服务: http://...:PORT/` 那行取。curl 必须在程序还在跑的时候执行。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功；`--stress` 下表示设备全程存活；`--expect-crash` 下表示崩溃复现成功 |
| 1 | 参数/环境错误（网卡、文件、设备没选中） |
| 2 | 仅 `--expect-crash`：跑完了但没崩 |
| 3 | `--stress`：设备失联 |
| 130 | 被 Ctrl-C 中断 |

## 注意事项

- **不要主动跑 `--stress`**。它会反复冲击设备，可能真的把电视/盒子打崩，只在用户明确要求老化或复现崩溃时用。
- 验证 seek 时别同时挂 `--stress`：音量连发会把 HTTP log 冲得难读。（`--churn` 才会重推，不带就不会覆盖播放位置。）
- 退出程序不会停止电视播放，需要先 `stop`。
- 改动媒体能力声明时，`dlna_media.py` 的 `dlna_features()` 是唯一出处 ——
  DIDL 的 `protocolInfo` 第四栏和 HTTP 的 `contentFeatures.dlna.org` 共用它，
  两处对不上时严格的机型以 HTTP 头为准。
