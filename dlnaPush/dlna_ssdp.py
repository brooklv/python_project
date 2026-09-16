"""SSDP 搜索：发 M-SEARCH，收应答，拉设备描述，得到一张可推流设备表。

C 版这一步是 libupnp 的 `UpnpSearchAsync()` + 回调。纯 Python 只能自己发
UDP 组播，几个坑必须自己填：

* **UDP 会丢**。组播尤其容易被 WiFi 丢掉，所以同一个 ST 要重发几次；
  设备重复应答是正常的，靠 UDN 去重即可。
* **必须指定从哪块网卡发**。不设 IP_MULTICAST_IF，内核按默认路由选，
  有线 + WiFi 共存时就发错网段，结果是一台设备都搜不到。
* **两种 ST 都要搜**。有的设备只应 MediaRenderer 设备类型，有的只应
  AVTransport 服务类型，只发一种会漏设备。
* **拉描述不能串行等**。一台没响应的设备最多拖 5 秒；边收边用线程池拉，
  慢设备不会挡住快设备的显示。
"""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from dlna_device import Renderer, fetch_description

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# 两种搜索目标，兼容只应答其中一种的设备
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
)

MSEARCH_REPEAT = 2          # 每个 ST 重发几次，对抗组播丢包
MSEARCH_GAP = 0.15          # 两次之间隔一下，别把接收端的队列打爆

_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: {addr}:{port}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: {mx}\r\n"
    "ST: {st}\r\n"
    "USER-AGENT: Linux/UPnP/1.0 dlna_push/1.0\r\n"
    "\r\n"
)


def parse_headers(raw: bytes) -> Dict[str, str]:
    """把 SSDP 应答拆成小写键的头部字典。首行（状态行）丢弃。"""
    out: Dict[str, str] = {}
    text = raw.decode("utf-8", errors="replace")
    for line in text.split("\r\n")[1:]:
        if not line.strip():
            continue
        key, _, val = line.partition(":")
        if _:
            out[key.strip().lower()] = val.strip()
    return out


class Discovery:
    """已发现设备表。跨多轮搜索累积，按 UDN 去重。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_udn: Dict[str, Renderer] = {}

    @property
    def renderers(self) -> List[Renderer]:
        with self._lock:
            return list(self._by_udn.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_udn)

    def add(self, rend: Renderer) -> bool:
        """新设备返回 True；已有的返回 False（同一台设备会应答很多次）。"""
        with self._lock:
            if rend.udn in self._by_udn:
                return False
            self._by_udn[rend.udn] = rend
            return True

    def search(self, bind_ip: str, seconds: int = 4,
               on_found: Optional[Callable[[Renderer], None]] = None) -> int:
        """搜索一轮，返回累计已知设备数（不是本轮新增数）。"""
        seconds = max(2, seconds)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(bind_ip))
        sock.bind((bind_ip, 0))
        sock.settimeout(0.5)

        seen_loc = set()
        # 拉描述的并发度：够覆盖一个家庭网段，又不会在树莓派上开一堆线程
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="desc")
        futures = []

        try:
            for _ in range(MSEARCH_REPEAT):
                for st in SEARCH_TARGETS:
                    msg = _MSEARCH.format(addr=SSDP_ADDR, port=SSDP_PORT,
                                          mx=seconds, st=st)
                    try:
                        sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
                    except OSError:
                        pass                      # 网卡瞬断，下一轮再发
                    time.sleep(MSEARCH_GAP)

            # MX 是设备的随机延迟上限，所以要等满 MX 再多留 1 秒收尾包
            deadline = time.monotonic() + seconds + 1
            while time.monotonic() < deadline:
                try:
                    raw, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                loc = parse_headers(raw).get("location")
                if not loc or loc in seen_loc:
                    continue
                seen_loc.add(loc)
                futures.append(pool.submit(self._fetch_and_add, loc, on_found))

            for fut in futures:
                fut.result()
        finally:
            pool.shutdown(wait=True)
            sock.close()

        return len(self)

    def _fetch_and_add(self, location: str,
                       on_found: Optional[Callable[[Renderer], None]]) -> None:
        rend = fetch_description(location)
        if rend and self.add(rend) and on_found:
            on_found(rend)

    def pick(self, sel: str) -> Optional[Renderer]:
        """按 序号 / 名称子串 / IP 选设备。找不到返回 None。

        三种写法并存是因为它们各有各的场合：脚本里用序号最短，人记得住的是
        名字，而序号会随搜索顺序变 —— 想稳定指向一台设备就得用 IP。
        """
        sel = sel.strip()
        if not sel:
            return None
        rends = self.renderers
        if sel.isdigit():
            idx = int(sel)
            return rends[idx] if 0 <= idx < len(rends) else None
        for r in rends:
            if sel in r.friendly or sel in r.location:
                return r
        return None
