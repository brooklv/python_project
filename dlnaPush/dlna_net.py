"""把网卡名解析成本机 IPv4，并给出对外可达的默认地址。

C 版把网卡名直接交给 `UpnpInit2(iface, 0)`，libupnp 内部去查地址。
纯 Python 没有这一层，得自己查 —— 而**查错网卡是这类工具最常见的故障**：
树莓派上有线和 WiFi 同时在线时，SSDP 从 eth0 发出去、电视在 wlan0 网段，
搜索结果永远是空的，看起来像电视不支持 DLNA。

所以这里的规则是：显式指定的网卡查不到地址就直接报错，绝不悄悄退回自动选。
"""

import socket
import struct
from typing import List, Optional

try:  # Linux 专有；在别的平台上只是没法按名字查网卡，不影响其余功能
    import fcntl
except ImportError:  # pragma: no cover - 仅非 Linux 平台走到
    fcntl = None

_SIOCGIFADDR = 0x8915   # <linux/sockios.h>


class IfaceError(RuntimeError):
    """网卡不存在，或存在但还没拿到 IPv4 地址。"""


def iface_ipv4(name: str) -> str:
    """查网卡的 IPv4 地址，如 "wlan0" -> "192.168.50.133"。"""
    if fcntl is None:
        raise IfaceError(f"当前平台不支持按网卡名查地址 (需要 Linux): {name}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # ifreq 结构: char ifr_name[16]; struct sockaddr ifr_addr;
        # 地址在偏移 20 处的 4 字节 (sockaddr_in.sin_addr)
        req = struct.pack("256s", name.encode()[:15])
        res = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, req)
        return socket.inet_ntoa(res[20:24])
    except OSError as exc:
        raise IfaceError(
            f"网卡 {name} 没有 IPv4 地址 ({exc.strerror}); "
            f"确认它存在且已联网: ip addr show {name}"
        ) from exc
    finally:
        sock.close()


def default_ipv4() -> str:
    """不指定网卡时用哪个地址：走默认路由出去的那个源地址。

    连一个不会真的发包的 UDP 目标即可 —— UDP connect 只是让内核选路，
    不产生任何流量，所以断网环境下也不会卡住。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def list_ifaces() -> List[str]:
    """列出有 IPv4 地址的网卡名，用于网卡指定错时给提示。"""
    try:
        names = [n for _idx, n in socket.if_nameindex()]
    except (AttributeError, OSError):   # 非 Linux / 取不到
        return []
    out = []
    for n in names:
        try:
            iface_ipv4(n)
        except IfaceError:
            continue                    # 网卡在, 但没 IPv4 (没插网线/没连上)
        out.append(n)
    return out


def resolve_bind_ip(iface: Optional[str]) -> str:
    """决定 SSDP 与内置 HTTP 服务绑哪个地址。"""
    if iface:
        return iface_ipv4(iface)
    return default_ipv4()
