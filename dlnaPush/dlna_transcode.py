"""用外部 ffmpeg 把源媒体转成推给电视的目标格式。

设计取舍（和 C 版一致）：

* 默认 ``-c:v copy -c:a copy``，**只换容器不重编码**。树莓派上 libx264 编
  1080p 远达不到实时；真要重编码，Pi 4 及更早可试 ``--vcodec h264_v4l2m2m``，
  Pi 5 去掉了硬件 H.264 编码器只能软编。
* HLS 有两种节奏：**转完再播**（默认）写 ``#EXT-X-ENDLIST``，电视能随意拖，
  代价是等 ffmpeg 跑完；**边转边播**（``--live``）播放列表标成 EVENT 持续追加，
  秒开，但只能拖到已生成的位置。
* TS/MP4 只支持转完再播。它们是单文件，HTTP 的 Content-Length 来自 stat，
  边写边播会让电视以为文件就那么长，提前判定播放结束。要秒开就用 HLS。

清理是这个模块的另一半职责：``-f hls`` 一部长片能在磁盘上留几百个分片，
退出前不删就等着把 SD 卡塞满。所以只删**自己生成的那几类文件名**，
绝不对用户用 ``--work-dir`` 指定的目录做通配删除。
"""

import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Tuple


class OutFormat(Enum):
    """推给 sink 的格式：决定电视拉到的是原始文件还是 ffmpeg 处理后的产物。"""

    RAW = "raw"     # 原始文件直推，不起 ffmpeg（默认）
    HLS = "hls"     # 切成 index.m3u8 + seg*.ts
    TS = "ts"       # 重封装成单个 MPEG-TS
    MP4 = "mp4"     # 重封装成 MP4 + faststart


# 命令行上的别名。写 m3u8 和写 hls 是一回事，写 mpegts 和写 ts 也是。
_FORMAT_ALIASES = {
    "raw": OutFormat.RAW, "none": OutFormat.RAW,
    "direct": OutFormat.RAW, "原始": OutFormat.RAW,
    "hls": OutFormat.HLS, "m3u8": OutFormat.HLS,
    "ts": OutFormat.TS, "mpegts": OutFormat.TS,
    "mp4": OutFormat.MP4,
}


def parse_format(text: str) -> OutFormat:
    """解析 -f 的取值。不认识就抛 ValueError。"""
    try:
        return _FORMAT_ALIASES[text.strip().lower()]
    except KeyError:
        raise ValueError(
            f"不认识的格式: {text} (可用 raw/hls/m3u8/ts/mpegts/mp4)") from None


HLS_PLAYLIST = "index.m3u8"
HLS_SEG_FMT = "seg%05d.ts"          # 里面的 %05d 是给 ffmpeg 的，不是给 Python 的
HLS_SEG_PREFIX = "seg"
TS_OUT = "out.ts"
MP4_OUT = "out.mp4"
FF_LOG = "ffmpeg.log"

LIVE_MIN_SEG = 3        # 边转边播：攒够几个分片才开始推
LIVE_WAIT_SEC = 180     # 边转边播：等第一批分片的上限
STOP_GRACE_SEC = 2.0    # SIGTERM 之后给 ffmpeg 多久收尾，超了就 SIGKILL


@dataclass
class XcodeConfig:
    """ffmpeg 转换参数，来自命令行 -f / --vcodec / ...。"""

    fmt: OutFormat = OutFormat.RAW
    ffmpeg: str = "ffmpeg"
    vcodec: str = "copy"            # 树莓派上别轻易改
    acodec: str = "copy"
    hls_time: int = 6
    live: bool = False              # True=边转边播（仅 HLS）
    work_dir: str = ""              # 空 = /tmp/dlna_push_<pid>
    extra: List[str] = field(default_factory=list)   # --ff-extra 透传

    @property
    def enabled(self) -> bool:
        return self.fmt is not OutFormat.RAW

    def describe(self) -> str:
        live = " 边转边播" if self.live and self.fmt is OutFormat.HLS else ""
        return f"{self.fmt.value}{live} (视频 {self.vcodec} / 音频 {self.acodec})"


class TranscodeError(RuntimeError):
    """ffmpeg 起不来、跑失败或没产出。"""


class Transcoder:
    """同一时刻只跑一个 ffmpeg。换片时先把上一次的进程和产物清掉。"""

    def __init__(self, cfg: XcodeConfig, log: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self._log = log or print
        self._proc: Optional[subprocess.Popen] = None
        self._logfile = None
        self._dir = ""
        self._dir_ours = False      # 目录是我们建的 -> 清理时连目录一起删

    # ------------------------------------------------------------------ 对外
    def start(self, src: str) -> Tuple[str, str]:
        """把 src 转成目标格式。返回 (产物目录, 入口文件名)。"""
        if not self.cfg.enabled:
            raise TranscodeError("raw 模式不需要转换")

        self.stop()                          # 换片：先清上一次的

        self._dir = self.cfg.work_dir or f"/tmp/dlna_push_{os.getpid()}"
        self._dir_ours = not os.path.isdir(self._dir)
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError as exc:
            self._dir = ""
            raise TranscodeError(f"无法创建工作目录: {exc}") from exc

        live = self.cfg.live
        if live and self.cfg.fmt is not OutFormat.HLS:
            self._log(f"[!] --live 只对 -f hls 有效, {self.cfg.fmt.value} 改为转完再播")
            live = False

        entry = {OutFormat.HLS: HLS_PLAYLIST,
                 OutFormat.TS: TS_OUT}.get(self.cfg.fmt, MP4_OUT)
        outpath = os.path.join(self._dir, entry)
        argv = self.build_argv(src, outpath, live)

        self._log(f"[*] 转换格式: {self.cfg.describe()}")
        self._log(f"[*] 工作目录: {self._dir}")
        self._log("[*] ffmpeg 命令: " + " ".join(argv))

        logpath = os.path.join(self._dir, FF_LOG)
        try:
            if live:
                # 边转边播时 ffmpeg 一直在后台跑，-stats 会不停刷屏盖掉交互提示符，
                # 所以把输出丢进日志文件；转完再播则继承终端，进度看得见。
                self._logfile = open(logpath, "wb")
                self._proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL,
                    stdout=self._logfile, stderr=subprocess.STDOUT)
            else:
                self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            self._wipe()
            raise TranscodeError(
                f"找不到 {self.cfg.ffmpeg}: 装一个 (apt install ffmpeg) "
                f"或用 --ffmpeg 指定路径") from exc
        except OSError as exc:
            self._wipe()
            raise TranscodeError(f"无法启动 ffmpeg: {exc}") from exc

        if live:
            self._wait_live(outpath, logpath)
        else:
            self._wait_done(outpath)

        return self._dir, entry

    def build_argv(self, src: str, outpath: str, live: bool) -> List[str]:
        """拼 ffmpeg 命令行。单独拆出来是为了能脱机测参数顺序。"""
        cfg = self.cfg
        argv = [
            cfg.ffmpeg or "ffmpeg",
            "-nostdin",                      # 别抢我们的 stdin，否则交互命令行会乱
            "-hide_banner",
            "-loglevel", "error",
            "-stats",
            "-y",
            "-i", src,
            # 只取第一路视频 + 第一路音频，丢字幕/数据流：mkv 里的字幕流
            # 塞进 mpegts/mp4 常常直接让 -c copy 失败
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-sn", "-dn",
            "-c:v", cfg.vcodec or "copy",
            "-c:a", cfg.acodec or "copy",
        ]

        if cfg.fmt is OutFormat.HLS:
            segpath = os.path.join(os.path.dirname(outpath), HLS_SEG_FMT)
            argv += [
                "-f", "hls",
                "-hls_time", str(cfg.hls_time if cfg.hls_time > 0 else 6),
                "-hls_list_size", "0",
                "-hls_flags", "independent_segments",
                "-hls_playlist_type", "event" if live else "vod",
                "-hls_segment_filename", segpath,
                "-start_number", "0",
            ]
        elif cfg.fmt is OutFormat.TS:
            argv += ["-f", "mpegts"]
        else:
            argv += ["-f", "mp4", "-movflags", "+faststart"]

        argv += list(cfg.extra)
        argv.append(outpath)
        return argv

    def stop(self) -> None:
        """杀掉 ffmpeg 并清理本次产物。可重复调用。"""
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_GRACE_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._proc = None
        if self._logfile:
            self._logfile.close()
            self._logfile = None
        self._wipe()

    # ------------------------------------------------------------------ 内部
    def _wait_done(self, outpath: str) -> None:
        self._log("[*] 正在转换, 请稍候 (-c copy 只重封装, 速度取决于磁盘/网络)...")
        code = self._proc.wait()
        self._proc = None
        if code != 0:
            self._wipe()
            raise TranscodeError(
                f"ffmpeg 失败 (退出码 {code}); 试试 -f raw 直推, "
                f"或换 --vcodec/--acodec 真转码")
        if not os.path.isfile(outpath) or os.path.getsize(outpath) == 0:
            self._wipe()
            raise TranscodeError(f"ffmpeg 没有产出 {outpath}")
        self._log(f"[+] 转换完成: {outpath}")

    def _wait_live(self, playlist: str, logpath: str) -> None:
        self._log(f"[*] 边转边播: 等前 {LIVE_MIN_SEG} 个分片 (日志: {logpath})...")
        deadline = time.monotonic() + LIVE_WAIT_SEC
        while time.monotonic() < deadline:
            code = self._proc.poll() if self._proc else 0
            if count_segments(playlist) >= LIVE_MIN_SEG:
                self._log("[+] 分片已就绪, 开始推流 (ffmpeg 继续在后台转)")
                return
            if code is not None:
                self._proc = None
                if code != 0:
                    self._wipe()
                    raise TranscodeError(f"ffmpeg 失败 (退出码 {code}), 详见 {logpath}")
                # 短片可能整段都转完了，分片数不到 LIVE_MIN_SEG 也算成功
                if count_segments(playlist) > 0:
                    self._log("[+] 转换已完成 (片子比预热窗口还短)")
                    return
                self._wipe()
                raise TranscodeError(f"ffmpeg 正常退出但没产出分片, 详见 {logpath}")
            time.sleep(0.2)
        self.stop()
        raise TranscodeError(f"等分片超时 ({LIVE_WAIT_SEC} 秒), 详见 {logpath}")

    def _wipe(self) -> None:
        """只删自己生成的文件；目录是我们建的才连目录一起删。"""
        if not self._dir:
            return
        try:
            names = os.listdir(self._dir)
        except OSError:
            names = []
        for name in names:
            if not is_artifact(name):
                continue            # 不碰用户目录里的其它文件
            try:
                os.unlink(os.path.join(self._dir, name))
            except OSError:
                pass
        if self._dir_ours:
            try:
                os.rmdir(self._dir)     # 只删空目录：里面还有别人的东西就留着
            except OSError:
                pass
        self._dir = ""
        self._dir_ours = False


def is_artifact(name: str) -> bool:
    """这个文件名是不是本工具生成的产物。清理时只认这几种。"""
    if name in (HLS_PLAYLIST, TS_OUT, MP4_OUT, FF_LOG):
        return True
    return name.startswith(HLS_SEG_PREFIX) and name.lower().endswith(".ts")


def count_segments(playlist: str) -> int:
    """数播放列表里已经写进去的分片数（非 # 开头的非空行）。"""
    try:
        with open(playlist, "r", encoding="utf-8", errors="replace") as fp:
            return sum(1 for line in fp if line.strip() and not line.startswith("#"))
    except OSError:
        return 0
