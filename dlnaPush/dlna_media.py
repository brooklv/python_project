"""按扩展名判定 MIME / 媒体大类 / upnp:class，并拼 DIDL-Lite 元数据。

**一张表两处用**：HTTP 响应头的 Content-Type 和 DIDL 里的 protocolInfo
必须说同一件事。C 版把这张表放在 httpserve.c 里就是为了这个 —— 两边各写
一份迟早会对不上，而症状是电视静默拒播，不给任何理由。

特别是 `.m3u8`：MIME 不是 application/vnd.apple.mpegurl 的话，多数电视会
把播放列表当普通文件下载而不是当播放列表解析，HLS 直接推不动。
"""

from enum import Enum
from typing import Dict, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr


class MediaKind(Enum):
    """媒体大类。决定 upnp:class，也决定哪些控制命令有意义。"""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"

    @property
    def label(self) -> str:
        return {"video": "视频", "audio": "音频", "image": "图片"}[self.value]

    @property
    def seekable(self) -> bool:
        """图片没有进度和音量可言 —— 对它发 seek/vol 只会拿到一堆 SOAP 错误。"""
        return self is not MediaKind.IMAGE


_CLS_VIDEO = "object.item.videoItem"
_CLS_AUDIO = "object.item.audioItem.musicTrack"
_CLS_IMAGE = "object.item.imageItem.photo"

# 扩展名（小写，带点） -> (MIME, 大类, upnp:class)
_TABLE: Dict[str, Tuple[str, MediaKind, str]] = {
    # 图片
    ".jpg":  ("image/jpeg", MediaKind.IMAGE, _CLS_IMAGE),
    ".jpeg": ("image/jpeg", MediaKind.IMAGE, _CLS_IMAGE),
    ".png":  ("image/png",  MediaKind.IMAGE, _CLS_IMAGE),
    ".gif":  ("image/gif",  MediaKind.IMAGE, _CLS_IMAGE),
    ".bmp":  ("image/bmp",  MediaKind.IMAGE, _CLS_IMAGE),
    ".webp": ("image/webp", MediaKind.IMAGE, _CLS_IMAGE),
    # 音频
    ".mp3":  ("audio/mpeg", MediaKind.AUDIO, _CLS_AUDIO),
    ".flac": ("audio/flac", MediaKind.AUDIO, _CLS_AUDIO),
    ".wav":  ("audio/wav",  MediaKind.AUDIO, _CLS_AUDIO),
    ".m4a":  ("audio/mp4",  MediaKind.AUDIO, _CLS_AUDIO),
    ".aac":  ("audio/aac",  MediaKind.AUDIO, _CLS_AUDIO),
    ".ogg":  ("audio/ogg",  MediaKind.AUDIO, _CLS_AUDIO),
    # 视频
    ".m3u8": ("application/vnd.apple.mpegurl", MediaKind.VIDEO, _CLS_VIDEO),
    ".mkv":  ("video/x-matroska", MediaKind.VIDEO, _CLS_VIDEO),
    ".avi":  ("video/avi",        MediaKind.VIDEO, _CLS_VIDEO),
    ".mov":  ("video/quicktime",  MediaKind.VIDEO, _CLS_VIDEO),
    ".ts":   ("video/mpeg",       MediaKind.VIDEO, _CLS_VIDEO),
    ".m2ts": ("video/mpeg",       MediaKind.VIDEO, _CLS_VIDEO),
    ".mpg":  ("video/mpeg",       MediaKind.VIDEO, _CLS_VIDEO),
    ".mpeg": ("video/mpeg",       MediaKind.VIDEO, _CLS_VIDEO),
    ".webm": ("video/webm",       MediaKind.VIDEO, _CLS_VIDEO),
    ".flv":  ("video/x-flv",      MediaKind.VIDEO, _CLS_VIDEO),
}

# 认不出的扩展名一律按 mp4 视频处理（mp4/m4v 也走这条）。
_DEFAULT = ("video/mp4", MediaKind.VIDEO, _CLS_VIDEO)


def media_info(path: str) -> Tuple[str, MediaKind, str]:
    """返回 (MIME, 媒体大类, upnp:class)。只看扩展名，不读文件内容。"""
    # 用 rfind 而不是 os.path.splitext：路径可能是 URL，带 ?query 时
    # splitext 会把 query 一起吞进扩展名里。
    dot = path.rfind(".")
    slash = max(path.rfind("/"), path.rfind("\\"))
    ext = path[dot:].lower() if dot > slash else ""
    # URL 上的 ?/# 不属于扩展名
    for cut in ("?", "#"):
        if cut in ext:
            ext = ext.split(cut, 1)[0]
    return _TABLE.get(ext, _DEFAULT)


# ------------------------------------------------------- DLNA.ORG_* 能力协商

# DLNA.ORG_FLAGS 是 32 个十六进制位，只有开头 8 位有意义，后 24 位保留全 0：
#   bit24 streaming-transfer   bit23 interactive-transfer   bit22 background-transfer
#   bit21 connection-stalling  bit20 dlna-v1.5
_FLAGS_STREAMING = "01700000"     # 24|22|21|20，音视频用
_FLAGS_INTERACTIVE = "00D00000"   # 23|22|20，图片用
_FLAGS_PAD = "0" * 24

# DLNA.ORG_OP 是两位十六进制：高位 = 时间轴 seek，低位 = 字节 seek。
# 我们只实现了 HTTP Range，所以是 01。不要图省事写成 11 —— 那等于声称支持
# TimeSeekRange.dlna.org，电视会真的发这个请求头，拿不到就判定播放失败，
# 比老老实实说"只能字节 seek"更糟。
_OP_BYTE_SEEK = "01"
_OP_NONE = "00"

# HLS 的进度是 playlist 自己的事，对 .m3u8 做字节 seek 毫无意义。
_HLS_MIME = "application/vnd.apple.mpegurl"


def dlna_features(mime: str, kind: MediaKind) -> str:
    """生成 DLNA.ORG_* 串。

    **一处生成两处用**：DIDL 里 protocolInfo 的第四栏，和 HTTP 响应的
    contentFeatures.dlna.org 头，说的必须是同一串 —— 和 MIME 表同样的道理。

    第四栏图省事留 `*` 是最容易踩的坑：电视读不到 DLNA.ORG_OP 就认定"这个源
    不能 seek"，于是把拖进度实现成重新 SetAVTransportURI，表现就是每次 seek
    都从头开始载 —— HTTP 层的 Range 支持得再完整也用不上。
    """
    if kind is MediaKind.IMAGE:
        op, flags = _OP_NONE, _FLAGS_INTERACTIVE
    elif mime == _HLS_MIME:
        op, flags = _OP_NONE, _FLAGS_STREAMING
    else:
        op, flags = _OP_BYTE_SEEK, _FLAGS_STREAMING
    # 故意不写 DLNA.ORG_PN（具体 profile）：那要真的去探码流参数，而探错了比
    # 不写更糟 —— 电视拿 PN 当准信，和实际码流对不上就直接拒播。
    return f"DLNA.ORG_OP={op};DLNA.ORG_CI=0;DLNA.ORG_FLAGS={flags}{_FLAGS_PAD}"


def transfer_mode(kind: MediaKind) -> str:
    """transferMode.dlna.org 的缺省值。电视没带这个头时也该主动回。"""
    return "Interactive" if kind is MediaKind.IMAGE else "Streaming"


_DIDL_TMPL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    '<item id="0" parentID="-1" restricted="1">'
    "<dc:title>{title}</dc:title>"
    "<upnp:class>{cls}</upnp:class>"
    "<res protocolInfo={proto}>{url}</res>"
    "</item></DIDL-Lite>"
)


def build_didl(title: str, mime: str, url: str, upnp_class: str,
               kind: Optional[MediaKind] = None) -> str:
    """最小 DIDL-Lite 元数据。传空字符串多数设备也能播，但兼容性明显更差。

    标题和 URL 必须转义 —— 文件名里一个 `&`（"Tom & Jerry.mp4"）就能让
    整份 XML 变成非法文档，设备直接报 SOAP 错误，而报错信息里不会提到 `&`。

    kind 只影响 DLNA.ORG_* 串；不传就按 MIME 粗判（够用：影响结果的只有
    "是不是图片"这一件事）。
    """
    if kind is None:
        kind = MediaKind.IMAGE if mime.startswith("image/") else MediaKind.VIDEO
    return _DIDL_TMPL.format(
        title=escape(title),
        cls=escape(upnp_class),
        proto=quoteattr(f"http-get:*:{mime}:{dlna_features(mime, kind)}"),
        url=escape(url),
    )
