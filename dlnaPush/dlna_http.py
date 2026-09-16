"""内置 HTTP 服务：把本地文件喂给电视。

对应 C 版的 libupnp 虚拟目录（httpserve.c）。用标准库重写时有两件事
**必须自己做**，`SimpleHTTPRequestHandler` 都没有：

1. **Range 请求**。电视几乎一定会发 `Range: bytes=0-`，拖进度更是全靠它。
   不支持 Range 就只能从头播到尾，拖动直接失效；有些电视看到没有
   `Accept-Ranges` 干脆拒播。libupnp 的 webserver 有这一层，标准库没有。
2. **Content-Type 由我们说了算**。这正是 C 版放弃 `UpnpSetWebServerRootDir`
   改用虚拟目录的原因 —— MIME 表里没有 m3u8/mkv 就会回落成
   application/octet-stream，HLS 播放列表拿到这个 MIME 电视直接拒播。
3. **DLNA 的两个响应头**。`contentFeatures.dlna.org` 要和 DIDL 里
   protocolInfo 的第四栏说同一串（都由 `dlna_media.dlna_features()` 生成）：
   只有 HTTP 支持 Range、DIDL 却不声明 `DLNA.ORG_OP=01`，电视仍然当作不可
   seek，拖进度会退化成整档重拉。`transferMode.dlna.org` 则是电视没带也要
   主动给缺省值。

安全上只做一件事但要做死：**只允许根目录下的单个文件名**，不接受 `..`、
不接受任何路径分隔符。这个服务会绑在局域网地址上，同网段的任何人都能访问。
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Tuple
from urllib.parse import unquote, quote

from dlna_media import dlna_features, media_info, transfer_mode

# URL 前缀，和 C 版的虚拟目录名保持一致
URL_PREFIX = "/media"

CHUNK = 64 * 1024       # 每次 sendfile 的块大小；再大对树莓派的内存不划算


class MediaRoot:
    """当前对外提供的目录。换片（open 命令）时会被改，所以要加锁。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dir = ""

    def set(self, path: str) -> None:
        with self._lock:
            self._dir = path

    def get(self) -> str:
        with self._lock:
            return self._dir

    def resolve(self, url_path: str) -> Optional[str]:
        """把 URL 路径映射成本地文件的绝对路径。不合法/不存在返回 None。"""
        root = self.get()
        if not root:
            return None

        path = url_path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith(URL_PREFIX):
            path = path[len(URL_PREFIX):]
        name = path.lstrip("/")
        if not name:
            return None

        # 先按解码后的名字找（正常情况），找不到再按原样找 —— 文件名里
        # 真带 '%' 时，解码会把 "100%.mp4" 变成别的东西。
        for cand in (unquote(name), name):
            if not cand or _unsafe(cand):
                continue
            full = os.path.join(root, cand)
            if os.path.isfile(full):
                return full
        return None


def _unsafe(name: str) -> bool:
    """文件名里不允许出现目录穿越和任何路径分隔符。

    只挡这两样就够：没有分隔符就出不了根目录。不额外挡 "." 开头的名字 ——
    那会把 ".hidden.mp4" 这种合法文件一起拒掉。
    """
    return ".." in name or "/" in name or "\\" in name


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """解析 Range 头，返回闭区间 (start, end)。

    返回 None 表示"整文件"（没有 Range 头，或头里是我们不支持的形式，
    比如多段 Range —— 这时按 RFC 回整个文件是合法的）。
    区间非法（越界/倒置）时抛 ValueError，调用方回 416。
    """
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].strip()
    if "," in spec:
        return None                      # 多段 Range：不支持，退回整文件
    start_s, _, end_s = spec.partition("-")

    if not start_s:                      # bytes=-N: 最后 N 字节
        if not end_s.isdigit():
            raise ValueError("Range 语法错误")
        n = int(end_s)
        if n == 0:
            raise ValueError("Range 长度为 0")
        return max(0, size - n), size - 1

    if not start_s.isdigit():
        raise ValueError("Range 语法错误")
    start = int(start_s)
    end = int(end_s) if end_s.isdigit() else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        raise ValueError("Range 超出文件范围")
    return start, end


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 才有长连接。HLS 一部片子几百个分片，每个都重开 TCP
    # 在树莓派上是实打实的延迟。前提是每个响应都给准确的 Content-Length。
    protocol_version = "HTTP/1.1"
    server_version = "dlna_push/1.0"

    # 由 MediaServer 注入
    root: MediaRoot
    log_fn: Callable[[str], None]

    def log_message(self, fmt: str, *args) -> None:
        self.log_fn(f"[http] {self.address_string()} {fmt % args}")

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def do_GET(self) -> None:
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        full = self.root.resolve(self.path)
        if not full:
            self.send_error(404, "Not Found")
            return

        try:
            size = os.path.getsize(full)
        except OSError:
            self.send_error(404, "Not Found")
            return

        try:
            rng = parse_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, end = rng if rng else (0, size - 1)
        length = end - start + 1 if size else 0
        mime, kind, _cls = media_info(full)

        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # 电视会带 getcontentFeatures.dlna.org: 1 来问能力，按规范这时必须回。
        # 但我们无条件回：有些机型根本不问，只看响应里有没有这个头，而多回一个
        # 头没有任何机型会介意。内容和 DIDL 里 protocolInfo 第四栏是同一串 ——
        # 两边对不上时，严格的机型以 HTTP 头为准。
        self.send_header("contentFeatures.dlna.org", dlna_features(mime, kind))
        # 电视用 transferMode 声明这是流式播放还是后台下载；它带了就原样回，
        # 没带也得给缺省值（音视频 Streaming / 图片 Interactive），规范要求这个
        # 头始终存在。
        self.send_header("transferMode.dlna.org",
                         self.headers.get("transferMode.dlna.org")
                         or transfer_mode(kind))
        self.end_headers()

        if not body or length <= 0:
            return
        self._send_file(full, start, length)

    def _send_file(self, path: str, start: int, length: int) -> None:
        try:
            with open(path, "rb") as fp:
                fp.seek(start)
                left = length
                while left > 0:
                    buf = fp.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            # 电视换台/停止播放就是这样断的，属于正常收尾，不是错误。
            # C 版靠 signal(SIGPIPE, SIG_IGN) 达到同样效果。
            pass
        except OSError as exc:
            self.log_fn(f"[http] 读文件失败 {path}: {exc}")


class MediaServer:
    """绑在指定网卡地址上的只读文件服务。端口由内核分配。"""

    def __init__(self, bind_ip: str, log: Optional[Callable[[str], None]] = None,
                 verbose: bool = False):
        self.bind_ip = bind_ip
        self.root = MediaRoot()
        self._log = log or (lambda _m: None)
        self._verbose = verbose
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        root, log = self.root, (self._log if self._verbose else (lambda _m: None))
        handler = type("MediaHandler", (_Handler,), {"root": root, "log_fn": staticmethod(log)})

        class _Server(ThreadingHTTPServer):
            daemon_threads = True           # 主线程退出时不等在途连接
            allow_reuse_address = True

        # 端口给 0：内核挑一个空闲端口，和 libupnp 的行为一致
        self._httpd = _Server((self.bind_ip, 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="http", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else 0

    def url_for(self, filename: str) -> str:
        """给根目录下的一个文件名，拼出电视能拉的 URL。"""
        return (f"http://{self.bind_ip}:{self.port}{URL_PREFIX}/"
                f"{quote(filename, safe='')}")

    def serve_dir(self, path: str) -> None:
        self.root.set(path)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
