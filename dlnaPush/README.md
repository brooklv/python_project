# dlnaPush — DLNA 媒体推流控制点 (DMC)，Python 版

把树莓派/PC 上的视频、音频、图片推到局域网里的电视或投屏盒子（DLNA
MediaRenderer），在终端里控制播放、音量、进度；也能对着设备跑音量老化压测。

C 版在 [`linux_c-programe/dlna_push`](../../linux_c-programe/dlna_push)，
**两边并存**：C 版继续维护，这边是同功能的 Python 实现。命令、参数名、交互
命令集刻意保持一致，两个版本可以互换着用。

最大的实现差异是 **不依赖 libupnp，只用 Python 标准库**：SSDP 是自己发的 UDP
组播，SOAP 是自己拼的 XML，HTTP 服务是 `http.server`。所以在装不了 pip 包的
树莓派上（`error: externally-managed-environment`）也是开箱即用 —— 不需要
venv，不需要 `apt install libupnp-dev`，也不需要编译。

**目录**

- [一、依赖与准备](#一依赖与准备)
- [二、用法总览](#二用法总览)
- [三、用法详解](#三用法详解)
  - [3.1 列出周围的设备](#31-列出周围的设备)
  - [3.2 推一个本地视频](#32-推一个本地视频)
  - [3.3 指定设备的三种写法](#33-指定设备的三种写法)
  - [3.4 推一个网络 URL](#34-推一个网络-url)
  - [3.5 推音频](#35-推音频)
  - [3.6 推图片](#36-推图片)
  - [3.7 转封装推流](#37-转封装推流)
  - [3.8 交互控制台：每条命令](#38-交互控制台每条命令)
  - [3.9 老化压测](#39-老化压测)
  - [3.10 崩溃复现](#310-崩溃复现)
  - [3.11 不推流直接压](#311-不推流直接压)
  - [3.12 SSDP 搜不到时按 IP 直连](#312-ssdp-搜不到时按-ip-直连)
  - [3.13 配合 nginx 等外部 HTTP 服务器](#313-配合-nginx-等外部-http-服务器)
  - [3.14 排查：电视到底有没有来拉流](#314-排查电视到底有没有来拉流)
- [四、参数速查](#四参数速查)
- [五、环境变量](#五环境变量)
- [六、退出码](#六退出码)
- [七、模块划分](#七模块划分)
- [八、和 C 版的实现差异](#八和-c-版的实现差异)
- [九、测试](#九测试)
- [十、已知限制](#十已知限制)

---

## 一、依赖与准备

* **Python 3.7+**（用到 dataclass 和 f-string；树莓派 OS 自带的就够）
* **ffmpeg** —— 只有用 `-f hls/ts/mp4` 转封装时才需要。默认的 `-f raw`
  直推完全用不到，不装也能跑。

```bash
sudo apt install ffmpeg      # 只有要转封装才装

chmod +x dlna_push.py
./dlna_push.py --help
```

没有任何第三方 Python 包。

**开跑前先确认一件事**：树莓派和电视必须在同一个网段，且 `-i` 要指对网卡。
有线和 WiFi 同时在线时不指定网卡，SSDP 很可能从错的网段发出去 —— 症状是
**一台设备都搜不到**，看起来像电视不支持 DLNA。

```bash
ip addr show wlan0           # 确认网卡有 IP，和电视同网段
```

---

## 二、用法总览

| 我想…… | 命令 |
|---|---|
| 看看周围有哪些设备 | `./dlna_push.py -i wlan0 list` |
| 推个视频，然后手动控制 | `./dlna_push.py -i wlan0 a.mp4 0` |
| 电视不认这个 mkv | `./dlna_push.py -i wlan0 -f ts a.mkv 0` |
| 长片想秒开 | `./dlna_push.py -i wlan0 -f hls --live --work-dir /媒体盘/hls a.mkv 0` |
| 推个已有的网络地址 | `./dlna_push.py -i wlan0 http://1.2.3.4/a.mp4 0` |
| 跑音量老化 | `./dlna_push.py -i wlan0 a.mp4 0 --stress --subscribe` |
| 复现 826x 的 SetVolume 崩溃 | 同上，再加 `--expect-crash` |
| 不推流，直接压当前播放 | `./dlna_push.py -i wlan0 --stress --no-push --subscribe 192.168.50.17` |
| 搜不到但知道 IP | 直接把 IP 当设备参数给它，会自动转直连 |
| 排查电视到底有没有来拉流 | 任何命令加 `--http-log` |

位置参数永远是这个顺序：

```
./dlna_push.py [选项...] <媒体路径|URL|list> [设备: 序号|名称|IP]
```

设备参数可以省略 —— 省略时会列出设备并提示你选。

---

## 三、用法详解

### 3.1 列出周围的设备

**什么时候用**：第一次接一台新电视，或者想知道设备的序号/名字/IP。

```bash
./dlna_push.py -i wlan0 list
```

```
[*] 绑定网卡: wlan0, 本机地址: 192.168.50.133
[*] 行编辑: readline 已启用
[*] 内置 HTTP 服务: http://192.168.50.133:41263/
[*] 正在搜索周围的 DLNA 渲染设备 (MediaRenderer)...
  [发现] ZIP Pro_5603061B             http://192.168.50.69:60099/

===== 已发现 1 台 DLNA 渲染设备 =====
  [0] ZIP Pro_5603061B         192.168.50.69:60099
(可用 序号 / 名称子串 / IP 来指定设备)
=====================================
```

逐行说明：

| 行 | 含义 |
|---|---|
| `绑定网卡` | SSDP 从这个地址发出去，媒体也从这个地址提供。不对就换 `-i` |
| `行编辑` | `readline 已启用` 表示提示符支持退格/方向键/历史 |
| `内置 HTTP 服务` | 端口由内核分配，每次运行都不同；推本地文件时电视来这里拉 |
| `[发现] xxx` | 边搜边打，一发现就显示。名字后若有 `(不支持音量)` 表示这台设备没有 RenderingControl，音量命令会失败 |
| `[0]` | 序号。**序号会随搜索顺序变**，脚本里别写死 |

搜不到设备时列表是 0 台，退出码仍是 `0`（`list` 只是查询，不是断言）。

设备多或网络差可以把每轮等待调长：

```bash
./dlna_push.py -i wlan0 --scan-time 8 list
```

### 3.2 推一个本地视频

**什么时候用**：最常见的用法。文件在树莓派上，让电视来拉。

```bash
./dlna_push.py -i wlan0 /media/pie/Disk/tmp/nature.mp4 0
```

```
[*] 绑定网卡: wlan0, 本机地址: 192.168.50.133
[*] 内置 HTTP 服务: http://192.168.50.133:41263/
[*] 正在搜索周围的 DLNA 渲染设备 (MediaRenderer)...
  [发现] ZIP Pro_5603061B             http://192.168.50.69:60099/

===== 已发现 1 台 DLNA 渲染设备 =====
  [0] ZIP Pro_5603061B         192.168.50.69:60099
=====================================
[*] 目标设备: ZIP Pro_5603061B
[*] 本地文件由内置 HTTP 服务提供 (根目录: /media/pie/Disk/tmp)
[*] 媒体类型: 视频, 地址: http://192.168.50.133:41263/media/nature.mp4 (video/mp4)
[+] 已在 "ZIP Pro_5603061B" 上开始播放
[i] 当前音量: 30

可用命令 (输入后回车):
  ...
dlna>
```

发生了什么：

1. **起 HTTP 服务**，根目录切到这个文件所在的目录（`/media/pie/Disk/tmp`）。
   只暴露这一层目录里的文件，不能穿越到别处。
2. **按扩展名判 MIME**（`.mp4` → `video/mp4`）。这个值同时写进 HTTP 响应头和
   DIDL 的 `protocolInfo` —— 两处必须一致，否则电视会静默拒播。同理，
   `DLNA.ORG_OP`/`DLNA.ORG_FLAGS` 也是 `protocolInfo` 第四栏和响应头
   `contentFeatures.dlna.org` 共用一份，它决定电视认不认这个源可以拖进度。
3. **发 SetAVTransportURI + Play**，然后进交互控制台。

文件名带空格或中文没问题，URL 会做百分号编码：

```bash
./dlna_push.py -i wlan0 "/media/pie/Disk/我的 假期.mp4" 0
# → http://192.168.50.133:41263/media/%E6%88%91%E7%9A%84%20%E5%81%87%E6%9C%9F.mp4
```

**不给设备参数**就会提示你选：

```bash
./dlna_push.py -i wlan0 /media/pie/Disk/tmp/nature.mp4
```

```
选择设备(序号/名称/IP, rescan 重扫, q 退出):
```

这里可以输 `rescan` 重新搜一轮，或 `q` 退出。**输错不会退出**，会继续提示 ——
名字打错就得从头搜一遍设备，代价太大。

### 3.3 指定设备的三种写法

三种写法并存，因为各有各的场合：

```bash
# 1) 序号：最短，适合手敲
./dlna_push.py -i wlan0 a.mp4 0

# 2) 名称子串：人记得住的东西
./dlna_push.py -i wlan0 a.mp4 客厅
./dlna_push.py -i wlan0 a.mp4 ZIP

# 3) IP：脚本里唯一可靠的写法
./dlna_push.py -i wlan0 a.mp4 192.168.50.69
```

* **序号会变**。它就是搜索结果里的下标，设备上线顺序不同就会变，所以不要写进
  脚本或 CI。
* **名称是子串匹配**，大小写敏感，匹配到的第一台就用。家里有两台
  "小米电视" 时会选到哪台是不确定的。
* **IP 最稳**，而且 SSDP 搜不到时会自动转直连（见
  [3.12](#312-ssdp-搜不到时按-ip-直连)）。

### 3.4 推一个网络 URL

**什么时候用**：文件已经在别的 HTTP 服务器上（NAS、nginx、另一台机器），
不需要树莓派转发。

```bash
./dlna_push.py -i wlan0 http://192.168.50.5:8080/movie.mp4 0
```

```
[*] 使用外部 HTTP 服务器提供的媒体 URL: http://192.168.50.5:8080/movie.mp4
[*] 媒体类型: 视频, 地址: http://192.168.50.5:8080/movie.mp4 (video/mp4)
```

内置 HTTP 服务照常启动（进程需要它待命），但这次不会用到。MIME 仍按 URL 的
扩展名判定，`?token=...` 这类 query 不会被当成扩展名。

### 3.5 推音频

和视频一样，按扩展名自动识别。区别在 `upnp:class` 会写成
`object.item.audioItem.musicTrack`，电视/音箱会按音乐条目处理。

```bash
./dlna_push.py -i wlan0 ~/Music/song.flac 客厅
```

```
[*] 媒体类型: 音频, 地址: http://192.168.50.133:41263/media/song.flac (audio/flac)
[+] 已在 "客厅音箱" 上开始播放
```

支持的音频扩展名：`.mp3 .flac .wav .m4a .aac .ogg`。

> **音频 + 转封装的细节**：`-f ts` 会把 flac 重封装成 `out.ts`，扩展名变成视频
> 类。这时 `upnp:class` 会**保留** `audioItem`，不会因为产物是 `.ts` 就变成视频
> 条目 —— 否则设备会按视频去处理一路只有音轨的流。

### 3.6 推图片

```bash
./dlna_push.py -i wlan0 ~/Pictures/photo.jpg 192.168.50.69
```

```
[*] 媒体类型: 图片, 地址: http://192.168.50.133:41263/media/photo.jpg (image/jpeg)
[+] 已在 "ZIP Pro_5603061B" 上开始显示
[i] 图片模式: 音量/进度命令无效
```

图片没有进度和音量可言，交互控制台里有意义的只剩 `stop` 和 `q`。
支持 `.jpg .jpeg .png .gif .bmp .webp`。

图片**不会**走 ffmpeg：即使加了 `-f ts`，也会打印
`[!] 图片不需要转封装, 按原始格式直推` 然后直推。

### 3.7 转封装推流

**什么时候用**：电视认不了原始文件（最典型的是 mkv）。`-f` 决定电视拉到的是
原始文件还是 ffmpeg 处理过的产物。

默认 `--vcodec copy --acodec copy`，**只换容器不重编码**，CPU 几乎不动。

#### `-f raw`（默认）— 原始文件直推

不起 ffmpeg，能不能播全看电视的解码能力。速度最快，也最省电。

#### `-f ts` — 重封装成单个 MPEG-TS

**老电视兼容性最好，认不了 mkv 时的第一选择。**

```bash
./dlna_push.py -i wlan0 -f ts /media/pie/Disk/tmp/a.mkv 0
```

```
[*] 推流格式: ts (视频 copy / 音频 copy)
[*] 目标设备: ZIP Pro_5603061B
[*] 转换格式: ts (视频 copy / 音频 copy)
[*] 工作目录: /tmp/dlna_push_17108
[*] ffmpeg 命令: ffmpeg -nostdin -hide_banner -loglevel error -stats -y -i /media/pie/Disk/tmp/a.mkv -map 0:v:0? -map 0:a:0? -sn -dn -c:v copy -c:a copy -f mpegts /tmp/dlna_push_17108/out.ts
[*] 正在转换, 请稍候 (-c copy 只重封装, 速度取决于磁盘/网络)...
[+] 转换完成: /tmp/dlna_push_17108/out.ts
[*] 媒体类型: 视频, 地址: http://192.168.50.133:41263/media/out.ts (video/mpeg)
```

注意几点：

* **要等 ffmpeg 跑完才开播**。单文件格式没法边写边播（原因见下面的 `--live`）。
* 字幕和数据流会被丢掉（`-sn -dn`）—— mkv 里的字幕流塞进 mpegts 常常直接让
  `-c copy` 失败。
* **退出时产物会被删掉**，电视随即拉不到流。所以转封装模式下退出前最好先在
  `dlna>` 里 `stop`。

#### `-f mp4` — 重封装成 MP4 + faststart

```bash
./dlna_push.py -i wlan0 -f mp4 a.mkv 0
```

加了 `-movflags +faststart`，moov 前置，电视不用下完整个文件才能起播。

#### `-f hls` — 切片，转完再播

```bash
./dlna_push.py -i wlan0 -f hls --hls-time 6 a.mkv 0
```

产物是 `index.m3u8` + `seg00000.ts`、`seg00001.ts`……播放列表写
`#EXT-X-ENDLIST`，电视可以随意拖进度。代价是要等整部片转完。

> **HLS 不是 DLNA 标准**，能不能播完全取决于电视自己支不支持 HLS。程序会提示
> `[!] HLS 不是 DLNA 标准 ... 不行就换 -f ts`。

#### `-f hls --live` — 边转边播（秒开）

**什么时候用**：长片，不想等。

```bash
./dlna_push.py -i wlan0 -f hls --live --work-dir /media/pie/Disk/hls a.mkv 0
```

```
[*] 推流格式: hls 边转边播 (视频 copy / 音频 copy)
[*] 工作目录: /media/pie/Disk/hls
[*] 边转边播: 等前 3 个分片 (日志: /media/pie/Disk/hls/ffmpeg.log)...
[+] 分片已就绪, 开始推流 (ffmpeg 继续在后台转)
```

攒够 3 个分片就开播，ffmpeg 继续在后台转。播放列表标成 EVENT 持续追加，
**只能拖到已经生成的位置**。ffmpeg 的输出写进 `ffmpeg.log`，不会刷屏盖掉交互
提示符。

`--live` 只对 hls 有效。用在 ts/mp4 上会提示
`[!] --live 只对 -f hls 有效, ts 改为转完再播` 然后按转完再播处理 —— 它们是
单文件，HTTP 的 Content-Length 来自 `stat`，边写边播会让电视以为文件就那么长，
播到一半就判定结束。

#### 真需要重编码

`copy` 只换容器；如果电视连编码本身都不认（HEVC、DTS），才需要真转码：

```bash
# Pi 4 及更早：用硬件编码器
./dlna_push.py -i wlan0 -f mp4 --vcodec h264_v4l2m2m --acodec aac a.mkv 0
```

**树莓派上的现实约束**：1080p 软编（libx264）远达不到实时，别指望能一边转一边
看。Pi 5 去掉了硬件 H.264 编码器，只能软编。

#### 工作目录与清理

| | |
|---|---|
| 默认位置 | `/tmp/dlna_push_<pid>` |
| 换成别处 | `--work-dir /media/pie/Disk/hls` |
| 退出时删什么 | 只删**自己生成的**：`index.m3u8`、`seg*.ts`、`out.ts`、`out.mp4`、`ffmpeg.log` |
| 目录本身 | 是程序建的才删；`--work-dir` 指进来的已有目录会保留 |

**`/tmp` 在 SD 卡上**，长片务必用 `--work-dir` 指到 U 盘或移动硬盘，否则一部
电影的分片就能把卡塞满，而且写卡还慢。

#### 透传 ffmpeg 参数

```bash
./dlna_push.py -i wlan0 -f ts --ff-extra "-bsf:a aac_adtstoasc" a.mkv 0
```

按空格切分，插在输出文件**之前**（放在后面 ffmpeg 会当成又一个输出）。

### 3.8 交互控制台：每条命令

推流成功后进入 `dlna>` 提示符。退格/方向键/历史靠标准库 `readline`。

#### 播放控制

| 命令 | 作用 | 说明 |
|---|---|---|
| `p` / `play` | 播放 | |
| `pause` | 暂停 | |
| `pp` | 播放/暂停切换 | **先查状态再决定**。盲发 Play 会把正在播的从头开始 |
| `s` / `stop` | 停止 | 转封装模式下退出前最好先执行它 |

#### 音量

```
dlna> vol 40
[+] 音量 -> 40
dlna> +
[+] 音量 -> 45
dlna> -
[+] 音量 -> 40
```

`vol` 的值会钳到 0~100，越界不会报错。设备没有 RenderingControl 时会提示
`[-] 该设备不支持音量控制`，不影响其它命令。

#### 进度

```
dlna> seek 0:01:30      # 时:分:秒
[+] 跳转到 0:01:30
dlna> seek 90           # 直接给秒数，自动换算
[+] 跳转到 0:01:30
```

拖进度依赖 HTTP Range —— 本工具的内置服务是支持的（见
[八、和 C 版的实现差异](#八和-c-版的实现差异)）。用 `-f hls --live` 时只能拖到
已经生成的分片位置。

光有 Range 还不够：电视是看 DIDL 的 `protocolInfo` 第四栏和响应头
`contentFeatures.dlna.org` 里的 `DLNA.ORG_OP` 来判断"这个源能不能 seek"的。
声明缺失时电视不会去发 Range，而是把 seek 实现成重新 `SetAVTransportURI`，
表现就是**每次拖进度都从头开始载**。两处都由
[dlna_media.py](dlna_media.py) 的 `dlna_features()` 生成，保证一致。

#### 查看状态

```
dlna> i
[i] 设备: ZIP Pro_5603061B  状态: PLAYING  进度: 0:01:07 / 0:05:13  音量: 40
```

状态是设备报的原始值：`PLAYING` / `PAUSED_PLAYBACK` / `STOPPED` /
`TRANSITIONING`。

#### 换片

```
dlna> open /media/pie/Disk/tmp/other.mkv     # 换一个文件
dlna> open http://192.168.50.5:8080/a.mp4    # 换成网络地址
dlna> open                                   # 不带参数：把当前媒体重推一遍
```

不带参数的 `open` 最常用在**切完设备之后** —— 把手上这个媒体推到新设备上。

换片会做完整的一轮：转封装模式下先杀掉上一个 ffmpeg、删掉上一批产物，再转
新的；HTTP 根目录也跟着切到新文件所在目录（**切完旧文件立刻取不到**）。

#### 换设备

```
dlna> dev 1
[*] 目标设备切换为: 客厅电视
[i] 输入 open 可把当前媒体推到该设备, 或 open <新文件> 换片

dlna> dev                    # 不带参数：列出来再选
===== 已发现 2 台 DLNA 渲染设备 =====
  [0] ZIP Pro_5603061B         192.168.50.69:60099
  [1] 客厅电视                  192.168.50.10:60099
=====================================
选择设备(序号/名称/IP):
```

**切设备不会自动推流**，只是把后续命令的目标换掉。要在新设备上播，切完再
`open`。

#### 重新搜索

```
dlna> scan
[*] 正在搜索周围的 DLNA 渲染设备 (MediaRenderer)...
  [发现] 客厅电视                     http://192.168.50.10:60099/
===== 已发现 2 台 DLNA 渲染设备 =====
...
[i] 可用 dev <序号> 切换到新发现的设备
```

搜到的设备**累加**进列表，不会清空已有的。等待时间跟 `--scan-time` 走。

#### 退出

| 输入 | 结果 |
|---|---|
| `q` / `quit` | 正常退出 |
| `Ctrl-D` | 同上 |
| `Ctrl-C` | 同上（会走完清理，终端不留半截行） |

```
dlna> q
[*] 退出 (设备继续播放中; 如需停止请先执行 stop)
```

转封装模式下退出会额外打印 `[*] 清理转换产物 (电视将无法继续拉流)` ——
产物删了电视就拉不到后续内容。

### 3.9 老化压测

**什么时候用**：想知道设备连续被改音量会不会崩。

推完流不进 `dlna>` 控制台，改跑音量循环：

```bash
./dlna_push.py -i wlan0 /media/pie/Disk/tmp/nature.mp4 192.168.50.17 \
    --stress --subscribe --burst 4 --max-rounds 120 --interval 2.0
```

```
[*] 目标设备: ZIP Pro_5603061B
[*] 媒体类型: 视频, 地址: http://192.168.50.133:41263/media/nature.mp4 (video/mp4)
[+] 已在 "ZIP Pro_5603061B" 上开始播放
[+] 事件订阅成功 SID=uuid:xxxx, 回调 http://192.168.50.133:44311/rc
[*] 发送初始探活请求...
[*] 开始老化: 音量 10 <-> 21, 每组连发 4 次(间隔 0.13s), 组间隔 2.00s, 订阅=开, churn=关, 共 120 轮
[001.1] SetVolume(10) -> ok
[001.2] SetVolume(21) -> ok
[001.3] SetVolume(10) -> ok
[001.4] SetVolume(21) -> ok
[002.1] SetVolume(10) -> ok
...
[+] 老化完成: 120 轮, 成功发送 480 次(0 次发送异常), 设备未发生崩溃
[i] 测试期间共收到 63 个 NOTIFY 事件
```

#### 压力的形状不是随便刷命令

三条**必须照做**的规则，做错了跑完全程也等于没测：

1. **两个不同的音量交替发**。相同音量会被设备自己拦掉（`the same volume
   detected, block !`），一次都到不了事件链路 —— 而日志上每条都显示 `-> ok`。
   所以 `--vol-a` 和 `--vol-b` 必须不同（相同会在开扫之前就报错）。
   **组内连发也在换值**，不是只在组之间换。
2. **组间隔要跨过设备的 200ms 定时器周期**（`--interval`，默认 0.5），同时组内
   密集连发（`--burst`）在定时器窗口里制造并发。
3. **`--subscribe` 基本是必开的**。崩溃走的是 LastChange 事件路径，不订阅设备
   根本不会去发事件，只刷控制面很可能一直复现不出来。

#### 各参数怎么调

| 参数 | 默认 | 调它的理由 |
|---|---|---|
| `--vol-a` / `--vol-b` | 10 / 21 | 一般不用改；只要两个值不同即可 |
| `--burst` | 1 | 调到 3~5 逼近真实控制端连续拖音量条的节奏 |
| `--burst-gap` | 0.13 | 组内间隔，默认值就是照着真实控制端的 ~130ms 定的 |
| `--interval` | 0.5 | 组间隔。**要 > 0.2** 才跨得过定时器周期 |
| `--max-rounds` | 60 | 总轮数。`120 轮 × 2.0s ≈ 4 分钟` |
| `--churn` | 关 | 每 10 轮插一次 `Stop → 0.3s → Play`，让状态机和定时器撞在一起 |
| `--subscribe` | 关 | 见上，基本必开 |

#### 崩溃是怎么判定的

误报和漏报都要防：

* **探活失败分类**。用 `GetVolume` 探活，失败原因分三类：
  * 收到非 200 的 HTTP 响应 → 进程明明活着，**不算崩**，继续跑
  * 连接被拒 / 重置 → 进程死亡的最强特征
  * 超时 → 可能死了，也可能只是忙
* **连续 `--confirm` 次失败**（默认 3）才算失联，防网络抖动误报。中间只要有一次
  探活成功就重新开始。
* **判定失联后继续盯 `--recover-wait` 秒**（默认 60）。actui 崩了会被 init 自动
  拉起，**"死过又复活"是崩溃的证据，不是没崩**。

设备崩了会提前结束，不会跑满轮数：

```
[037] 探活失败 [1/3]: refused (GetVolume 发送失败: <urlopen error [Errno 111] Connection refused>)
[037] 探活失败 [2/3]: refused (...)
[037] 探活失败 [3/3]: refused (...)
设备失联确认（[037]: 连接被拒/重置（DMR 进程死亡特征）），等待设备恢复...
==================================================
 FAILED: 设备在老化中失联！设备在 SetVolume 压力测试期间失联。
 失联原因: 连接被拒/重置（DMR 进程死亡特征）
 设备在 12.3 秒后恢复（符合崩溃后被 init 自动重启的特征）。
 已跑 37 轮, 成功发送 148 次音量指令。
==================================================
```

退出码 `3`（老化语义：设备失联 = 测试失败）。

### 3.10 崩溃复现

**什么时候用**：目标不是"确认设备稳定"，而是"把那个已知的崩溃复现出来"。
这时崩了才叫成功，退出码要反过来。

这块搬自 C 版目录里的
[`dlna_crash_repro.py`](../../linux_c-programe/dlna_push/dlna_crash_repro.py)
（那个脚本还在原地，没动）。原来的命令加两个词就能用：

```bash
# 旧
./dlna_crash_repro.py -i wlan0 nature.mp4 192.168.50.17 \
    --subscribe --burst 4 --max-rounds 120 --interval 2.0

# 新（多了 --stress 和 --expect-crash）
./dlna_push.py -i wlan0 nature.mp4 192.168.50.17 --stress --expect-crash \
    --subscribe --burst 4 --max-rounds 120 --interval 2.0
```

`--vol-a` / `--vol-b` / `--burst` / `--burst-gap` / `--interval` /
`--max-rounds` / `--churn` / `--confirm` / `--recover-wait` / `--no-push` /
`--subscribe` 全部同名同默认值，可以照抄。

`--expect-crash` 只做一件事：**反转退出码**。

| | 设备存活 | 设备失联 |
|---|---|---|
| 默认（老化语义） | `0` 通过 | `3` 失败 |
| `--expect-crash`（复现语义） | `2` 没复现 | `0` 复现成功 |

措辞也会跟着变（`SUCCESS: 复现成功！` 对 `FAILED: 设备在老化中失联！`），
但事实判定完全一样。

#### 它在压什么

`DMR_LastChangeTimerEvent()` 每 200ms 跑一次，看到 `LastChangeMask != 0` 就触发
`FireGenaLastChangeEvent()`，那里读 `DMR_microStack` 的偏移 `0x50`；运行期间这个
指针会变坏（疑似 `DMRDestroyFromChain` 释放了 state 却没取消自我 re-arm 的
200ms 定时器），于是 SIGSEGV。

#### CI 里怎么用

```bash
./dlna_push.py -i wlan0 a.mp4 192.168.50.17 --stress --subscribe \
    --burst 4 --max-rounds 300 --interval 1.0
case $? in
  0) echo "老化通过" ;;
  3) echo "设备崩了" ; exit 1 ;;
  1) echo "参数/连接问题" ; exit 1 ;;
esac
```

### 3.11 不推流直接压

**什么时候用**：设备上已经在播别的东西（手机投的、U 盘里的），只想压音量。

```bash
./dlna_push.py -i wlan0 --stress --no-push --subscribe 192.168.50.17
```

`--no-push` 有两个便利处理：

* **隐含 `--stress`** —— 不推流又不压测就没事可做了，所以 `--stress` 可以省：
  ```bash
  ./dlna_push.py -i wlan0 --no-push --subscribe 192.168.50.17
  ```
* **唯一的位置参数就是设备**。没有媒体可给，所以 `192.168.50.17` 会被当成设备
  而不是媒体路径。

输出里会看到：

```
[*] 已指定 --no-push, 跳过推流
[*] 发送初始探活请求...
```

### 3.12 SSDP 搜不到时按 IP 直连

**什么时候用**：设备能 ping 通，但搜不到。组播被 AP 的客户端隔离策略吃掉、
跨 VLAN、或者干脆是 WiFi 丢包，这都是常态。

不需要额外的开关 —— **只要设备参数写成 IP**，SSDP 没匹配到就会自动转直连：

```bash
./dlna_push.py -i wlan0 a.mp4 192.168.50.17
```

```
===== 已发现 0 台 DLNA 渲染设备 =====
=====================================
[*] SSDP 未匹配到, 尝试直连: http://192.168.50.17:49152/description.xml
[*] 目标设备: ZIP Pro_5603061B
```

端口和描述路径可调：

```bash
./dlna_push.py -i wlan0 a.mp4 192.168.50.17 --port 60099 --desc-path /dd.xml
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | `49152` | 设备描述的端口 |
| `--desc-path` | `/description.xml` | 设备描述的路径 |

直连失败会明确报出来，不会退回"没找到设备"这种没指向性的提示：

```
[-] 直连 http://192.168.50.17:49152/description.xml 失败 (可用 --port / --desc-path 调整端口和路径)
```

这条兜底对普通推流和老化压测都生效。

### 3.13 配合 nginx 等外部 HTTP 服务器

**什么时候用**：怀疑内置 HTTP 服务是卡顿的瓶颈，想拿一个成熟的服务器对比；
或者媒体本来就在 NAS 上。

不需要任何特殊支持 —— 直接把 http URL 当媒体参数给它，内置服务就不会被用到：

```bash
./dlna_push.py -i wlan0 http://192.168.50.133:8080/movie.mp4 0
```

C 版目录下的
[`nginx_dlna.conf`](../../linux_c-programe/dlna_push/nginx_dlna.conf) 和
[`start_nginx_dlna.sh`](../../linux_c-programe/dlna_push/start_nginx_dlna.sh)
在这边同样可用 —— 它们和控制点是解耦的，只是一个提供文件的 HTTP 服务器。

> **注意**：必须给 nginx 的 URL，而不是本地文件路径。给本地路径就还是走内置
> 服务，对比不出任何东西。

### 3.14 排查：电视到底有没有来拉流

`--http-log` 打印内置 HTTP 服务收到的每条请求。这是**判断问题在哪一半**最直接
的手段：

```bash
./dlna_push.py -i wlan0 --http-log /media/pie/Disk/tmp/nature.mp4 0
```

```
[http] 192.168.50.69 "HEAD /media/nature.mp4 HTTP/1.1" 200 -
[http] 192.168.50.69 "GET /media/nature.mp4 HTTP/1.1" 206 -
```

按这个顺序缩小范围：

| 现象 | 结论 | 下一步 |
|---|---|---|
| 一条 `[http]` 都没有 | 电视根本没来拉 | SOAP 成功了但电视没动 —— 检查 URL 里的 IP 电视是否可达（同网段？AP 隔离？） |
| 有 `HEAD` 没有 `GET` | 电视探了一下就放弃了 | MIME 或文件大小不对它胃口，换 `-f ts` |
| 有 `GET` 且 200/206，画面不动 | 拉到了但解不了 | 封装/编码电视不认，换 `-f ts`；还不行就真转码 |
| `GET` 之后马上断开重连 | 多半是带宽/卡顿 | 测速，或换外部 nginx 对比 |

自己先在树莓派上验证 URL 能取（地址用程序打印出来的那个）：

```bash
curl -I 'http://192.168.50.133:41263/media/nature.mp4'
#   期望: 200, Content-Type: video/mp4, Accept-Ranges: bytes

curl -r 0-1023 -o /dev/null -w '%{http_code}\n' 'http://192.168.50.133:41263/media/nature.mp4'
#   期望: 206  ← 拖进度全靠它

curl -sI 'http://192.168.50.133:41263/media/nature.mp4' | grep -i dlna
#   期望: contentFeatures.dlna.org: DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=017...
#         transferMode.dlna.org: Streaming
#   ← 少了 DLNA.ORG_OP=01, 电视就不会用 Range 去 seek, 而是整档重拉
```

**一台设备都搜不到**：多半是网卡选错。不带 `-i` 时用的是默认路由的地址，
有线 + WiFi 共存时基本一定是错的。

```bash
./dlna_push.py -i wlan0 --scan-time 8 list      # 换网卡 + 加长等待
ip addr show wlan0                              # 确认和电视同网段
```

网卡名写错会直接报错并列出可用网卡，不会悄悄退回自动选：

```
[-] 网卡 wlan1 没有 IPv4 地址 (No such device); 确认它存在且已联网: ip addr show wlan1
    当前有 IPv4 的网卡: lo, eth0, wlan0
```

---

## 四、参数速查

### 通用

| 参数 | 默认 | 说明 |
|---|---|---|
| `-i` / `--iface <网卡>` | 自动 | 绑定网卡。**有线+WiFi 共存时必给** |
| `--scan-time <秒>` | `4` | 每轮 SSDP 等待时间。交互里的 `scan` 也用这个值 |
| `--http-log` | 关 | 打印内置 HTTP 服务的每条请求 |
| `--port <端口>` | `49152` | 按 IP 直连时设备描述的端口 |
| `--desc-path <路径>` | `/description.xml` | 按 IP 直连时设备描述的路径 |

### 转封装

| 参数 | 默认 | 说明 |
|---|---|---|
| `-f` / `--format` | `raw` | `raw` \| `hls`(=`m3u8`) \| `ts`(=`mpegts`) \| `mp4` |
| `--vcodec <编码>` | `copy` | `-c:v`。只换容器不重编码 |
| `--acodec <编码>` | `copy` | `-c:a` |
| `--hls-time <秒>` | `6` | HLS 分片时长 |
| `--live` | 关 | **仅 hls**：边转边播 |
| `--work-dir <目录>` | `/tmp/dlna_push_<pid>` | 产物目录，退出时清理自己的产物 |
| `--ffmpeg <路径>` | `ffmpeg` | ffmpeg 可执行文件 |
| `--ff-extra "<参数>"` | 空 | 透传给 ffmpeg，按空格切分，插在输出文件之前 |

### 老化 / 压测

| 参数 | 默认 | 说明 |
|---|---|---|
| `--stress` | 关 | 推流后跑老化循环，不进交互控制台 |
| `--no-push` | 关 | 跳过推流。隐含 `--stress`；此时唯一位置参数是设备 |
| `--subscribe` | 关 | 订阅 GENA 事件并接 NOTIFY。**基本必开** |
| `--churn` | 关 | 每 10 轮插一次 Stop/Play 扰动 |
| `--vol-a <音量>` | `10` | 交替音量 A |
| `--vol-b <音量>` | `21` | 交替音量 B，必须与 A 不同 |
| `--burst <次数>` | `1` | 每组连发次数 |
| `--burst-gap <秒>` | `0.13` | 组内连发间隔 |
| `--interval <秒>` | `0.5` | 组间隔，要 > 0.2 |
| `--max-rounds <轮数>` | `60` | 最大轮数 |
| `--confirm <次数>` | `3` | 判定失联所需的连续探活失败次数 |
| `--recover-wait <秒>` | `60` | 判定失联后等复活的秒数 |
| `--expect-crash` | 关 | 反转退出码语义 |

---

## 五、环境变量

| 变量 | 等价参数 |
|---|---|
| `DLNA_IFACE` | `-i` / `--iface` |
| `DLNA_FFMPEG` | `--ffmpeg` |

命令行参数优先于环境变量。

```bash
export DLNA_IFACE=wlan0
./dlna_push.py list                # 不用每次写 -i
```

---

## 六、退出码

| 码 | 含义 |
|---|---|
| `0` | 成功。推流模式=正常退出；老化=设备全程存活；`--expect-crash`=复现成功 |
| `1` | 前置错误：参数不对、网卡查不到、没发现设备、推流失败 |
| `2` | **仅 `--expect-crash`**：跑完了但设备没崩（没复现出来） |
| `3` | **仅老化语义**：设备失联/崩溃 |
| `130` | 老化跑到一半被 Ctrl-C 打断 |

在 `dlna>` 提示符按 Ctrl-C 属于正常退出，返回 `0`，不是 `130`。

---

## 七、模块划分

| 文件 | 职责 |
|---|---|
| [dlna_push.py](dlna_push.py) | 命令行入口：参数解析、装配、退出清理、退出码 |
| [dlna_net.py](dlna_net.py) | 网卡名 → 本机 IPv4 |
| [dlna_ssdp.py](dlna_ssdp.py) | SSDP M-SEARCH 组播搜索、设备表、按 序号/名称/IP 选设备 |
| [dlna_device.py](dlna_device.py) | `Renderer` 模型、设备描述 XML 解析、controlURL/eventSubURL 拼绝对地址 |
| [dlna_soap.py](dlna_soap.py) | SOAP 信封拼装/发送，AVTransport + RenderingControl 动作 |
| [dlna_http.py](dlna_http.py) | 内置 HTTP 服务：Range、Content-Type、DLNA 响应头、路径安全 |
| [dlna_media.py](dlna_media.py) | MIME / 媒体大类 / upnp:class 表，DIDL-Lite 生成 |
| [dlna_transcode.py](dlna_transcode.py) | ffmpeg 流水线与产物清理 |
| [dlna_session.py](dlna_session.py) | 把上面串成一次推流；唯一有状态的地方 |
| [dlna_shell.py](dlna_shell.py) | `dlna>` 交互控制台 |
| [dlna_stress.py](dlna_stress.py) | 老化 / 压力测试：音量循环、GENA 订阅、崩溃判定 |

一次推流的顺序是固定的，且**顺序本身有意义**：

```
先转封装 → 再切 HTTP 根目录、定 URL → 最后发 SetAVTransportURI + Play
```

反过来做的症状是电视拉到 404，而 SOAP 那边一切正常，日志上看不出任何异常 ——
电视收到 URI 后可能立刻就来取文件。

---

## 八、和 C 版的实现差异

功能和命令一致，实现上有几处**必须自己补的坑**：

### 1. Range 请求是自己实现的

libupnp 的 webserver 自带 Range 支持，Python 的 `SimpleHTTPRequestHandler`
**没有**。电视几乎一定会发 `Range: bytes=0-`，拖进度更是全靠它；不支持的症状
不是报错，是拖进度条没反应，有些电视看到没有 `Accept-Ranges` 干脆拒播。
所以 [dlna_http.py](dlna_http.py) 自己解析 Range、回 206 / 416，并对多段 Range
按 RFC 退回整文件。

### 2. 虚拟目录 → 自建 HTTP 服务

C 版为了控制 Content-Type，放弃 `UpnpSetWebServerRootDir` 改用 libupnp 虚拟目录
（内置 MIME 表里没有 m3u8/mkv，一律回落成 `application/octet-stream`，HLS
播放列表拿到这个 MIME 电视直接拒播）。Python 这边整个服务本来就是自己的，
Content-Type 直接由 [dlna_media.py](dlna_media.py) 那张表决定，没有回落分支。

顺带多了几样 C 版没有的：`Accept-Ranges`、`contentFeatures.dlna.org`，以及
`transferMode.dlna.org`（电视带了就原样回，没带也给缺省值 —— 音视频
`Streaming`、图片 `Interactive`）。

### 3. 网卡绑定要自己查地址

C 版把网卡名直接交给 `UpnpInit2(iface, 0)`。Python 这边用 ioctl
（`SIOCGIFADDR`）查，查不到就**直接报错并列出当前有 IPv4 的网卡**，不会悄悄
退回自动选 —— 悄悄退回的结果是搜不到设备，而人会以为参数生效了。

### 4. SSDP 细节

`UpnpSearchAsync` 换成手写 M-SEARCH，需要自己处理：每个 ST 重发 2 次对抗组播
丢包、设置 `IP_MULTICAST_IF` 指定出口网卡、两种 ST（MediaRenderer 设备类型和
AVTransport 服务类型）都搜、拉设备描述用线程池以免一台慢设备挡住整轮。

### 5. 合并了 `dlna_crash_repro.py`

C 版目录里那个 977 行的崩溃复现脚本，大半是重复实现的 SSDP / SOAP / 本地 HTTP /
设备选择。搬过来时把重复的全删了，只留真正属于压测的部分
（[dlna_stress.py](dlna_stress.py)）：音量循环、GENA 订阅、崩溃判定。
原脚本仍在原地，没有改动。

### 6. C 版没有的参数

| 参数 | 用途 |
|---|---|
| `--scan-time` | 每轮 SSDP 等待时间（C 版写死 4 秒） |
| `--http-log` | 打印内置 HTTP 服务的每条请求 |
| `--port` / `--desc-path` | 按 IP 直连设备描述 |
| `--stress` 一族 | 老化 / 压测（来自 `dlna_crash_repro.py`） |

---

## 九、测试

脱机跑，不需要电视、不需要网卡、不需要 ffmpeg。会起本地 HTTP 服务但只绑
`127.0.0.1`。

```bash
python3 -m unittest discover -v      # 146 个用例
python3 -m unittest test_http -v     # 单跑某一组
```

| 文件 | 卡的是什么 |
|---|---|
| [test_media.py](test_media.py) | MIME 表；DIDL 转义（文件名里一个 `&` 就能让整份 XML 非法） |
| [test_discovery.py](test_discovery.py) | 相对 controlURL 的拼接；SSDP 头部大小写；UDN 去重 |
| [test_soap.py](test_soap.py) | **入参顺序**（SOAP 按位置匹配，不按名字）；Fault 正文提取 |
| [test_http.py](test_http.py) | Range / 206 / 416 / HEAD；DLNA 头和 DIDL 是否一致；目录穿越拦截 |
| [test_transcode.py](test_transcode.py) | ffmpeg 参数顺序；清理只删自己生成的文件 |
| [test_session.py](test_session.py) | 端到端：发 SOAP 那一刻 URL 必须已经能取到 |
| [test_stress.py](test_stress.py) | 探活分类（误报/漏报两个方向）；音量交替；两套退出码 |

`test_soap.py` / `test_session.py` / `test_stress.py` 会起一个假 MediaRenderer，
真发 SOAP 请求。`test_session.py` 里的假电视收到 `SetAVTransportURI` 后会
**立刻去拉那个 URL**，用来卡住第七节说的顺序不变量；`test_stress.py` 里的假设备
会真的处理 `SUBSCRIBE` / `UNSUBSCRIBE`，并收一条真的 `NOTIFY`。

---

## 十、已知限制

* **还没在真机上跑过。** 单元测试覆盖到假电视和本地 HTTP 服务，但没有对着真实的
  DLNA 设备验证过一次完整推流或一轮老化。
* **只支持字节 seek，没有时间轴 seek。** `protocolInfo` 声明的是
  `DLNA.ORG_OP=01`（高位 0 = 不支持 time-seek），电视得自己把时间换算成字节
  位移。mp4/ts 一般没问题；mkv、avi 有些电视不解析 index，算不出位移就仍会
  整档重拉。要根治得实现 `TimeSeekRange.dlna.org` 请求头，目前没做。
  C 版（`dlna_push/`）还停在 `http-get:*:<mime>:*`，没有同步这一处。
* SSDP 只做主动搜索（M-SEARCH），不监听设备主动发的 `NOTIFY`。开着的时候有新
  设备上线不会自动出现在列表里，得手动 `scan`。
* 只处理 IPv4。
* 组播搜索、`SIOCGIFADDR` 查网卡都是 Linux 行为；在 Windows 上模块能导入、
  测试能跑，但 `-i 网卡` 用不了。
