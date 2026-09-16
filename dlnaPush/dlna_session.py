"""把发现、转封装、HTTP 服务和 SOAP 串成一次"推流"。

这里是整个工具唯一有状态的地方：当前设备、当前媒体、内置 HTTP 服务的
根目录、正在跑的 ffmpeg。交互命令（open/dev/scan）改的都是这些状态。

一次推流的顺序是固定的，且**顺序本身有意义**：

1. 先转封装（如果 ``-f`` 不是 raw）—— 之后一切都按"产物这个本地文件"走，
   URL、MIME、upnp:class 自动落到产物的扩展名上（.m3u8/.ts/.mp4）。
2. 再定 URL —— 本地文件要先把 HTTP 服务的根目录切到它所在目录。
3. 最后才发 SetAVTransportURI + Play。**顺序反了电视会拉到 404**：
   它收到 URI 后可能立刻就来取文件，这时 HTTP 服务必须已经指向对的目录。
"""

import os
from dataclasses import dataclass
from typing import Callable, Optional

import dlna_soap as soap
from dlna_device import Renderer, fetch_description
from dlna_http import MediaServer
from dlna_media import MediaKind, build_didl, media_info
from dlna_ssdp import Discovery
from dlna_transcode import Transcoder, XcodeConfig


def is_http_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


def sec_to_hms(seconds: int) -> str:
    """秒 -> H:MM:SS。seek 命令允许直接给秒数。"""
    seconds = max(0, seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@dataclass
class PushResult:
    """一次成功推流的结果，用于显示。"""

    url: str
    mime: str
    kind: MediaKind
    title: str


class Session:
    """一次运行期间的全部状态。"""

    def __init__(self, bind_ip: str, cfg: XcodeConfig,
                 log: Optional[Callable[[str], None]] = None,
                 http_verbose: bool = False):
        self.bind_ip = bind_ip
        self.cfg = cfg
        self.log = log or print
        self.discovery = Discovery()
        self.http = MediaServer(bind_ip, log=self.log, verbose=http_verbose)
        self.xcode = Transcoder(cfg, log=self.log)
        self.renderer: Optional[Renderer] = None
        self.media = ""             # 用户给的原始路径/URL，不是产物路径
        self.volume = 50
        self.scan_seconds = 4       # 交互里的 scan/rescan 也用这个值

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.http.start()
        self.log(f"[*] 内置 HTTP 服务: http://{self.bind_ip}:{self.http.port}/")

    def close(self) -> None:
        self.xcode.stop()
        self.http.stop()

    def __enter__(self) -> "Session":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 设备
    def scan(self, seconds: Optional[int] = None) -> int:
        seconds = self.scan_seconds if seconds is None else seconds

        def found(r: Renderer) -> None:
            tag = "" if r.has_rc else "(不支持音量) "
            self.log(f"  [发现] {r.friendly:<28} {tag}{r.location}")

        self.log("[*] 正在搜索周围的 DLNA 渲染设备 (MediaRenderer)...")
        return self.discovery.search(self.bind_ip, seconds, on_found=found)

    def device_list_text(self) -> str:
        rends = self.discovery.renderers
        lines = [f"\n===== 已发现 {len(rends)} 台 DLNA 渲染设备 ====="]
        for i, r in enumerate(rends):
            tag = "" if r.has_rc else "(不支持音量控制)"
            lines.append(f"  [{i}] {r.friendly:<24} {r.host:<22} {tag}")
        lines.append("(可用 序号 / 名称子串 / IP 来指定设备)")
        lines.append("=====================================")
        return "\n".join(lines)

    def connect_direct(self, ip: str, port: int = 49152,
                       desc_path: str = "/description.xml") -> bool:
        """跳过 SSDP，直接按 IP 拉设备描述。

        SSDP 是组播，最容易被 AP 的隔离策略、VLAN、或者干脆是丢包吃掉 ——
        设备明明能 ping 通却搜不到是常态。知道 IP 时直连比反复重扫可靠得多。
        """
        location = f"http://{ip}:{port}{desc_path}"
        self.log(f"[*] SSDP 未匹配到, 尝试直连: {location}")
        rend = fetch_description(location)
        if not rend:
            self.log(f"[-] 直连 {location} 失败 "
                     f"(可用 --port / --desc-path 调整端口和路径)")
            return False
        self.discovery.add(rend)
        self.renderer = rend
        try:
            self.volume = soap.get_volume(rend)
        except soap.SoapError:
            pass
        return True

    def select(self, sel: str) -> bool:
        """按 序号/名称/IP 切换当前设备。成功返回 True。"""
        r = self.discovery.pick(sel)
        if not r:
            return False
        self.renderer = r
        try:
            self.volume = soap.get_volume(r)
        except soap.SoapError:
            pass                # 没有 RenderingControl 的设备很常见，不是错误
        return True

    # ------------------------------------------------------------------ 推流
    def push(self, media: str) -> PushResult:
        """把 media 推到当前设备并开始播放。失败抛异常。"""
        if not self.renderer:
            raise RuntimeError("还没有选择目标设备")
        r = self.renderer
        self.media = media

        # 标题始终取用户给的原始文件名：转封装后的入口叫 index.m3u8，
        # 拿它当标题会在电视上显示成 "index.m3u8"。
        # URL 上的 ?query 也得去掉，不然会连着一串 token 显示在电视上。
        title = os.path.basename(
            media.split("?", 1)[0].split("#", 1)[0].rstrip("/")) or media

        _src_mime, src_kind, src_cls = media_info(media)
        target = media
        keep_cls: Optional[str] = None

        if self.cfg.enabled:
            if src_kind is MediaKind.IMAGE:
                self.log("[!] 图片不需要转封装, 按原始格式直推")
            else:
                out_dir, entry = self.xcode.start(media)
                target = os.path.join(out_dir, entry)
                # 产物扩展名（.m3u8/.ts/.mp4）一律被判成视频；纯音频源要保住
                # 原来的 upnp:class，否则电视会按视频条目处理一路只有音轨的流
                if src_kind is MediaKind.AUDIO:
                    keep_cls = src_cls

        url = self._build_url(target)
        mime, kind, cls = media_info(target)
        if keep_cls:
            cls, kind = keep_cls, MediaKind.AUDIO

        self.log(f"[*] 媒体类型: {kind.label}, 地址: {url} ({mime})")

        soap.set_uri(r, url, build_didl(title, mime, url, cls, kind))
        soap.play(r)
        self.log(f'[+] 已在 "{r.friendly}" 上开始'
                 f'{"显示" if kind is MediaKind.IMAGE else "播放"}')
        if kind is MediaKind.IMAGE:
            self.log("[i] 图片模式: 音量/进度命令无效")
        return PushResult(url=url, mime=mime, kind=kind, title=title)

    def _build_url(self, target: str) -> str:
        """本地文件交给内置 HTTP 服务；http(s) 地址原样用。"""
        if is_http_url(target):
            self.log(f"[*] 使用外部 HTTP 服务器提供的媒体 URL: {target}")
            return target

        path = os.path.realpath(target)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"找不到本地文件: {target}")
        directory, name = os.path.split(path)
        self.http.serve_dir(directory)
        self.log(f"[*] 本地文件由内置 HTTP 服务提供 (根目录: {directory})")
        return self.http.url_for(name)
