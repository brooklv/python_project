"""MediaRenderer 设备模型，以及设备描述 XML 的解析。

设备描述里的 controlURL 常常是相对路径（"/ctl/AVTransport"，甚至
"ctl/AVTransport"），必须用 URLBase 或描述文档自身的地址拼成绝对地址。
C 版靠 `UpnpResolveURL()`，这里用 `urljoin` —— 规则一致。

只收录带 AVTransport 的设备：没有它就推不了流，列出来只会让人白选一次。
"""

import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

# 设备描述文档一般几 KB；超时给短一点，免得一台没响应的设备拖慢整轮搜索
DESC_TIMEOUT = 5.0
DESC_MAX_BYTES = 512 * 1024


@dataclass
class Renderer:
    """一台 DLNA 渲染设备（电视/盒子/音箱）。"""

    udn: str                       # 唯一标识，用于跨多次 SSDP 应答去重
    friendly: str
    location: str                  # 设备描述 XML 的 URL
    base: str                      # 解析相对 URL 的基址
    av_type: str = ""              # AVTransport serviceType
    av_control: str = ""           # AVTransport 控制 URL（绝对）
    rc_type: str = ""              # RenderingControl serviceType
    rc_control: str = ""           # RenderingControl 控制 URL（绝对）
    av_event: str = ""             # AVTransport eventSubURL（绝对）
    rc_event: str = ""             # RenderingControl eventSubURL（绝对）

    @property
    def has_av(self) -> bool:
        """能不能推流、控播放/进度。"""
        return bool(self.av_type and self.av_control)

    @property
    def can_subscribe(self) -> bool:
        """能不能订阅 RenderingControl 的 LastChange 事件。

        不少设备只给 controlURL 不给 eventSubURL，订阅就无从谈起 ——
        这不是错误，只是压力测试少了一条事件链路。
        """
        return bool(self.rc_event)

    @property
    def has_rc(self) -> bool:
        """能不能控音量。不少投屏盒子只有 AVTransport。"""
        return bool(self.rc_type and self.rc_control)

    @property
    def host(self) -> str:
        """从 location 取 host[:port]，用于显示和按 IP 匹配。"""
        return urlparse(self.location).netloc


def _local(tag: str) -> str:
    """去掉 ElementTree 给的 {namespace} 前缀。

    设备描述的默认命名空间各家写法不一，按带命名空间的全名去找标签
    会在某些设备上一个也匹配不到 —— 按 localName 匹配才稳。
    """
    return tag.rsplit("}", 1)[-1]


def _first_text(root: ElementTree.Element, tag: str) -> Optional[str]:
    for el in root.iter():
        if _local(el.tag) == tag and el.text:
            return el.text.strip()
    return None


def _child_text(parent: ElementTree.Element, tag: str) -> Optional[str]:
    for el in parent.iter():
        if _local(el.tag) == tag and el.text:
            return el.text.strip()
    return None


def fetch_description(location: str) -> Optional[Renderer]:
    """拉设备描述并解析。不是可用 sink（没有 AVTransport）就返回 None。"""
    try:
        req = urllib.request.Request(location, headers={"User-Agent": "dlna_push/1.0"})
        with urllib.request.urlopen(req, timeout=DESC_TIMEOUT) as resp:
            raw = resp.read(DESC_MAX_BYTES)
    except Exception:
        return None                       # 设备下线/超时/返回垃圾，跳过就是
    return parse_description(location, raw)


def parse_description(location: str, raw: bytes) -> Optional[Renderer]:
    """解析设备描述 XML。单独拆出来是为了能脱机测。"""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return None

    udn = _first_text(root, "UDN")
    if not udn:
        return None
    friendly = _first_text(root, "friendlyName") or "(未命名设备)"
    urlbase = _first_text(root, "URLBase")
    base = urlbase if urlbase else location

    rend = Renderer(udn=udn, friendly=friendly, location=location, base=base)

    for svc in root.iter():
        if _local(svc.tag) != "service":
            continue
        stype = _child_text(svc, "serviceType")
        ctrl = _child_text(svc, "controlURL")
        if not stype or not ctrl:
            continue
        abs_url = urljoin(base, ctrl)
        evt = _child_text(svc, "eventSubURL")
        abs_evt = urljoin(base, evt) if evt else ""
        if "AVTransport" in stype:
            rend.av_type, rend.av_control = stype, abs_url
            rend.av_event = abs_evt
        elif "RenderingControl" in stype:
            rend.rc_type, rend.rc_control = stype, abs_url
            rend.rc_event = abs_evt

    return rend if rend.has_av else None
