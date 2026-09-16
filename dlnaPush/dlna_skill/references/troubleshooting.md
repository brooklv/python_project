# DLNA 推流排障手册

按症状组织。每节的顺序都是：**怎么确认 → 根因 → 怎么修**。

---

## 1. 一台设备都搜不到

**先确认**：设备能不能 ping 通。

- ping 不通 → 网络问题，不是 DLNA 问题。
- ping 得通但搜不到 → 组播没到，继续往下。

**两个常见根因**：

1. **网卡选错**。有线和 WiFi 同时在线时，不带 `-i` 会用默认路由的地址，
   M-SEARCH 从错的网段发出去。用 `-i wlan0` 明确指定。
   程序启动时打印的 `本机地址` 要和电视在同一网段。
2. **AP 隔离了组播**。很多家用路由器的"AP 隔离""IGMP snooping"会吃掉 SSDP。

**绕过**：设备位置直接给 IP，程序会自动转直连（跳过 SSDP，直接拉设备描述 XML）：

```bash
./dlna_push.py -i wlan0 /p/a.mp4 192.168.50.17
```

设备描述不在默认的 `49152/description.xml` 时，用 `--port` / `--desc-path` 指定。
这两个值可以从电视的 UPnP 设置页，或用 `tcpdump -i wlan0 udp port 1900` 抓一条
设备自己发的 NOTIFY 得到。

**注意**：SSDP 只做主动搜索（M-SEARCH），不监听设备主动发的 NOTIFY。
程序开着的时候有新设备上线不会自动出现，要手动 `scan`。

---

## 2. 找不到本地文件

没有默认媒体目录。相对路径按**进程的当前工作目录**解析
（`dlna_session.py` 里就是一句 `os.path.realpath(target)`）。

HTTP 服务的根目录不是预先设好的，而是推流时由你给的那个文件**反推**出来的：
取文件所在目录，`serve_dir()` 进去，只暴露那一层，不接受子路径和 `..`。

所以推流成功会多打印一行：

```
[*] 本地文件由内置 HTTP 服务提供 (根目录: /media/pie/Disk/tmp)
```

没看到这行就是根本没走到那一步。**给绝对路径最省事。**

---

## 3. 电视收下了但不播，而且不报任何错

DLNA 设备拒播时基本不给理由，SOAP 也回成功。按可能性排：

### 3.1 MIME 对不上

`Content-Type` 和 DIDL 里 `protocolInfo` 的第二段必须说同一件事。
两边都由 `dlna_media.py` 的同一张表生成，正常不会不一致——
除非有人在别处硬编码了 MIME。

最容易踩的是 **HLS 的 `.m3u8`**：不是 `application/vnd.apple.mpegurl` 的话，
多数电视会当普通文件下载而不是当播放列表解析，直接拒播。

### 3.2 电视根本不支持这个容器/编码

`-f raw`（默认）是原样直推，能不能播全看电视的解码能力。mkv、HEVC、
10-bit 都是常见的翻车点。

**修**：`-f ts` 重封装成 MPEG-TS（老电视兼容性最好），或 `-f mp4`。
默认 `-c:v copy -c:a copy` 只换容器不重编码，树莓派上 CPU 几乎不动。

真要重编码：1080p 软编远达不到实时。Pi 4 及更早可试
`--vcodec h264_v4l2m2m`，Pi 5 去掉了硬件 H.264 编码器只能软编。

### 3.3 HLS 本身

HLS **不是 DLNA 标准**。`-f hls` 能不能播完全取决于电视自身支不支持。
推不动就换 `-f ts`，不要在 HLS 上耗时间。

---

## 4. 拖进度每次从头开始载

**这一类最容易误判成"HTTP server 不支持 Range"。先排除这个误判。**

### 4.1 确认 server 端

程序跑着的时候，另开一个 shell：

```bash
curl -sSI "http://192.168.50.133:36025/media/nature.mp4" | grep -iE 'accept-ranges|dlna'
```

期望三行：

```
Accept-Ranges: bytes
contentFeatures.dlna.org: DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000
transferMode.dlna.org: Streaming
```

端口每次启动都不同（绑 port 0 由内核分配），从程序打印的
`[*] 内置 HTTP 服务:` 那行取。curl 必须在程序还在跑的时候执行，
程序一退出 HTTP 服务就跟着关了。

**这一步通过只说明 server 没问题，不代表电视会用它。**

### 4.2 根因：`DLNA.ORG_OP` 没声明

电视不是看到 `Accept-Ranges` 就会用 Range 的。它先看两处：

- DIDL 里 `<res protocolInfo="http-get:*:<mime>:<这里>">`
- HTTP 响应头 `contentFeatures.dlna.org`

这两处都留 `*` 的话，电视认定"这个源不能 seek"，于是把拖进度实现成
**重新 `SetAVTransportURI`** —— 表现就是每次拖都从头开始载。
HTTP 层的 Range 支持得再完整也用不上。

正确的串（由 `dlna_media.py` 的 `dlna_features()` 生成，两处共用同一份）：

```
DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000
```

- `OP` 两位十六进制：**高位 = 时间轴 seek，低位 = 字节 seek**。
  只实现了 HTTP Range 就必须是 `01`。
  **不要图省事写成 `11`** —— 那等于声称支持 `TimeSeekRange.dlna.org`，
  电视会真的发这个请求头，拿不到就直接判定播放失败，比诚实说"只能字节 seek"更糟。
- `FLAGS` 是 32 个十六进制位，只有开头 8 位有意义：
  `01700000` = streaming-transfer | background-transfer | connection-stalling | dlna-v1.5。
  图片用 `00D00000`（interactive）。位数不对，一部分机型会整条 `protocolInfo` 解析失败。
- 故意**不写** `DLNA.ORG_PN`（具体 profile）：那要真去探码流参数，探错了比不写更糟——
  电视拿 PN 当准信，和实际码流对不上就直接拒播。

图片和 `.m3u8` 用 `OP=00`：图片没有进度可言；HLS 的进度是 playlist 自己的事，
对播放列表做字节 seek 毫无意义。

### 4.3 确认电视真的在用（唯一的决定性证据）

```bash
printf 'seek 90\ni\nq\n' | ./dlna_push.py -i wlan0 /p/a.mp4 0 --http-log
```

看 `[http]` 行的状态码：

| 时机 | 状态码 | 判读 |
|---|---|---|
| 起播 | `200` | 电视没带 Range，整档从 0 拉。**多数设备起播就是这样，不说明问题** |
| seek 后 | `206` | 电视发了 `Range: bytes=<非零>-`，**byte-seek 生效** |
| seek 后 | `200`，和起播一模一样 | 整档重拉，seek 没生效 |

起播 `200` / seek 后 `206` 的对比是决定性的：如果电视的 seek 是"整档重拉"，
行为会和起播完全一致（又一个 `200`）。出现 `206` 就说明它带了非零 offset。

日志目前只打状态码不打 Range 的值。要看确切 offset：

```bash
sudo tcpdump -i wlan0 -A -s0 -l 'tcp port 36025 and tcp[((tcp[12:1]&0xf0)>>2):4] = 0x47455420' | grep -iE '^(GET|Range)'
```

### 4.4 声明对了但还是重拉

`DLNA.ORG_OP=01` 只宣告 **byte**-seek，电视得自己把时间换算成字节位移。

- **mp4 / ts**：一般没问题（实测 EZCast 系列可以）。
- **mkv / avi**：有些电视不解析 index，算不出位移，仍会整档重拉。

**修**：`-f mp4`（带 faststart）或 `-f ts` 重封装。

根治要实现 `TimeSeekRange.dlna.org` 请求头（回 `TimeSeekRange.dlna.org: npt=...`
加上对应的 byte range），**当前没有实现**，而且那需要能把时间映射到字节，
实务上得靠 ffprobe 建索引。

### 4.5 HLS 的情况不一样

`-f hls` 走的是另一条路，靠 playlist 本身 seek，不吃 `DLNA.ORG_OP` 这套。

- 默认（转完再播）：写了 `#EXT-X-ENDLIST`，可以随意拖，但要等 ffmpeg 跑完。
- `--live`（边转边播）：秒开，但只能拖到已经生成的分片位置。

---

## 5. 老化测试 / 崩溃复现

```bash
./dlna_push.py -i wlan0 /p/a.mp4 192.168.50.17 --stress --subscribe --burst 4 --max-rounds 120 --interval 2.0
```

**不要在用户没要求时主动跑**，它可能真的把设备打崩。

- 循环内容：推流一次，然后反复连发 `SetVolume`（在 `--vol-a` / `--vol-b` 之间跳）。
- `--churn` 才会每 10 轮做一次 Stop→Play。**不带 `--churn` 时循环不会重推**，
  所以老化跑着的时候手动拖进度是安全的，只是音量会一直跳。
- 崩溃判定走 `LastChange` 事件路径，所以 `--subscribe` 基本是必开的。
- `--interval` 要 > 0.2s 才跨得过设备的定时器周期。
- 退出码：默认按测试语义（0=存活，3=失联）；加 `--expect-crash` 反过来
  （0=复现成功，2=没崩）。

耗时约 `max_rounds × (burst × burst_gap + interval)`。上面那组约 5 分钟。

---

## 6. C 版的差异

同一个 workspace 下还有一个 C 实现（`linux_c-programe/dlna_push/`，和
`python_project/dlnaPush/` 是并列的两个仓库），依赖 libupnp。
行为和命令集一致，排障时注意两点：

- Range 由 libupnp 的 webserver 处理（回调 `vd_seek`/`vd_read`），不是自己实现的。
- `contentFeatures.dlna.org` / `transferMode.dlna.org` 要往 `UpnpFileInfo` 上挂
  `UpnpExtraHeaders`（libupnp 1.8 起才有）。Makefile 用一次实地编译探测决定开不开，
  `make` 时看 `[make] DLNA 响应头: ...` 印的是"启用"还是"未启用"。
  未启用时只是少这两个头，DIDL 那半边照常生效。
