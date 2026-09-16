"""SOAP 控制：AVTransport（推流/播放/进度）与 RenderingControl（音量）。

C 版用 `UpnpMakeAction()` + `UpnpSendAction()`，这里手工拼 XML 发 POST。
两个必须做对、做错就静默失败的地方：

* **参数顺序**。SOAP 动作的入参是**有序**的，不是按名字匹配。
  `SetAVTransportURI` 必须是 InstanceID → CurrentURI → CurrentURIMetaData，
  换了顺序不少设备直接当成非法请求，而回的错误码毫无指向性。
* **参数要转义**。CurrentURIMetaData 里塞的是整份 DIDL-Lite XML，
  不转义就等于把标签直接嵌进信封，整份文档变成非法 XML。

出错一律抛 `SoapError`，由调用方决定是打印还是重试 —— C 版在库里
`fprintf(stderr)` 的做法在交互界面上会打断提示符。
"""

import urllib.error
import urllib.request
from typing import Dict, Optional, Sequence, Tuple
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from dlna_device import Renderer

SOAP_TIMEOUT = 10.0

_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    '<u:{action} xmlns:u="{stype}">{args}</u:{action}>'
    "</s:Body></s:Envelope>"
)


class SoapError(RuntimeError):
    """SOAP 动作失败。code 是 UPnP 错误码（拿不到时为 None）。

    cause 保留底层异常。压力测试靠它区分"设备进程死了"（连接被拒/超时）
    和"HTTP 层还活着只是这条动作被拒"（500 + Fault）—— 两者都会走到
    这个异常，但只有前者算崩溃。
    """

    def __init__(self, message: str, code: Optional[int] = None,
                 cause: Optional[BaseException] = None):
        super().__init__(message)
        self.code = code
        self.cause = cause


def build_envelope(action: str, stype: str,
                   args: Sequence[Tuple[str, str]]) -> str:
    """拼 SOAP 信封。args 必须按服务定义的顺序给。"""
    body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args)
    return _ENVELOPE.format(action=action, stype=stype, args=body)


def parse_response(raw: bytes) -> Dict[str, str]:
    """把动作应答里的出参取成 {名字: 文本}。按 localName 匹配，忽略命名空间。"""
    root = ElementTree.fromstring(raw)
    out: Dict[str, str] = {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if el.text is not None and len(el) == 0:
            out[tag] = el.text
    return out


def parse_fault(raw: bytes) -> Tuple[Optional[int], str]:
    """从 SOAP Fault 里挖出 UPnP 的 errorCode / errorDescription。

    设备回 500 时正文才是有用信息（"701 Transition not available" 之类），
    只报 HTTP 500 等于把唯一的线索丢掉。
    """
    try:
        fields = parse_response(raw)
    except ElementTree.ParseError:
        return None, raw.decode("utf-8", errors="replace")[:200]
    code = fields.get("errorCode")
    desc = fields.get("errorDescription") or fields.get("faultstring") or "未知错误"
    try:
        return int(code), desc
    except (TypeError, ValueError):
        return None, desc


def send_action(control_url: str, stype: str, action: str,
                args: Sequence[Tuple[str, str]],
                timeout: Optional[float] = None) -> Dict[str, str]:
    """发一个 SOAP 动作，返回出参字典。失败抛 SoapError。

    timeout 单独可调是给探活用的：老化测试要在 2 秒内判断设备是否还在，
    不能等默认的 10 秒。
    """
    body = build_envelope(action, stype, args).encode("utf-8")
    req = urllib.request.Request(
        control_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{stype}#{action}"',
            "User-Agent": "Linux/UPnP/1.0 dlna_push/1.0",
            "Connection": "close",
        },
    )
    try:
        with urllib.request.urlopen(
                req, timeout=SOAP_TIMEOUT if timeout is None else timeout) as resp:
            return parse_response(resp.read())
    except urllib.error.HTTPError as exc:
        code, desc = parse_fault(exc.read())
        raise SoapError(f"{action} 失败: {desc}"
                        + (f" (UPnP {code})" if code else ""),
                        code, cause=exc) from exc
    except ElementTree.ParseError as exc:
        raise SoapError(f"{action} 的应答不是合法 XML", cause=exc) from exc
    except OSError as exc:
        raise SoapError(f"{action} 发送失败: {exc}", cause=exc) from exc


# ------------------------------------------------------------------ AVTransport

def _av(r: Renderer, action: str, args: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    return send_action(r.av_control, r.av_type, action, args)


def set_uri(r: Renderer, uri: str, metadata: str = "") -> None:
    _av(r, "SetAVTransportURI", [
        ("InstanceID", "0"),
        ("CurrentURI", uri),
        ("CurrentURIMetaData", metadata),
    ])


def play(r: Renderer) -> None:
    _av(r, "Play", [("InstanceID", "0"), ("Speed", "1")])


def pause(r: Renderer) -> None:
    _av(r, "Pause", [("InstanceID", "0")])


def stop(r: Renderer) -> None:
    _av(r, "Stop", [("InstanceID", "0")])


def seek(r: Renderer, hhmmss: str) -> None:
    _av(r, "Seek", [("InstanceID", "0"), ("Unit", "REL_TIME"), ("Target", hhmmss)])


def get_position(r: Renderer) -> Tuple[str, str]:
    """返回 (当前进度, 总时长)，都是 H:MM:SS。"""
    out = _av(r, "GetPositionInfo", [("InstanceID", "0")])
    return out.get("RelTime", "0:00:00"), out.get("TrackDuration", "0:00:00")


def get_state(r: Renderer) -> str:
    """PLAYING / PAUSED_PLAYBACK / STOPPED / TRANSITIONING ..."""
    out = _av(r, "GetTransportInfo", [("InstanceID", "0")])
    return out.get("CurrentTransportState", "UNKNOWN")


# ------------------------------------------------------------ RenderingControl

def set_volume(r: Renderer, vol: int) -> int:
    """设音量，返回实际设置的值（已钳到 0~100）。"""
    if not r.has_rc:
        raise SoapError("该设备不支持音量控制")
    vol = max(0, min(100, vol))
    send_action(r.rc_control, r.rc_type, "SetVolume", [
        ("InstanceID", "0"),
        ("Channel", "Master"),
        ("DesiredVolume", str(vol)),
    ])
    return vol


def get_volume(r: Renderer, timeout: Optional[float] = None) -> int:
    if not r.has_rc:
        raise SoapError("该设备不支持音量控制")
    out = send_action(r.rc_control, r.rc_type, "GetVolume", [
        ("InstanceID", "0"), ("Channel", "Master"),
    ], timeout=timeout)
    try:
        return int(out.get("CurrentVolume", "0"))
    except ValueError:
        return 0
