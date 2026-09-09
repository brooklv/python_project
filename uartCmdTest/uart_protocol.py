"""UART 协议：打包、解包、返回码解析。

对应的 C 版在 linux_c-programe/uartCmdTest/。

包结构（对应固件 amUartCmdHead）::

    tag[2]  check_sum  id      data_len  data[]
    47 54   小端 H     小端 H  小端 h    变长

三个容易踩的点，都由 struct 格式串直接表达，不用手工位运算：

* **Command ID 是固件 uartCmdID 枚举值的小端序**
  ``NET_STA_CTRL = 0x8223`` → 线上字节 ``23 82``
* **应答 ID = 请求 & 0x3FFF | 0x4000**
  ``0x8223`` → ``0x4223``（线上 ``23 42``）。高字节 bit7=1 请求 / bit6=1 应答
* **data_len 是有符号 short**
  查询指令用 ``-1``（``FF FF``）表示"无数据"，不是 65535

data 的前 2 字节是 16 位小端有符号的 UART_ERR 返回码。
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum

HEADER = b"GT"          # 47 54
HEAD_LEN = 8

# tag, checksum, cmd_id, data_len —— 小端，data_len 有符号
_HEAD = struct.Struct("<2sHHh")

MAX_DATA = 32767        # data_len 是 short，理论上限；超过就是脏数据


class Cmd(IntEnum):
    """Command ID，取自固件 uartCmdID 枚举。

    末尾两条标了 ``# 文档`` 的不在固件枚举里，是从 Uart command.md
    的抓包反推的，用之前最好确认一下。
    """

    AUDIO_MUTE          = 0x8101
    AUDIO_VOLUME        = 0x8102

    AP_SSID             = 0x820A
    AP_PWD              = 0x820B
    WIFI_MAC_ADDR       = 0x8210
    WIFI_STATUS         = 0x8211
    NET_ROLE            = 0x8220
    NET_ROLE_STATUS     = 0x8221
    NET_MODE_ONOFF      = 0x8222
    NET_STA_CTRL        = 0x8223
    NET_AP_PARAM        = 0x8224

    KEY_STATUS          = 0x8401
    TOUCH_STATUS        = 0x8402

    DEV_NAME            = 0x861D
    LANGUAGE            = 0x8625
    SOURCE_STA          = 0x8627
    POWER_STATUS        = 0x8628
    RESET_TO_DEFAULT    = 0x8629
    FW_UPGRADE          = 0x8632
    FW_VERSION          = 0x863D
    NATIVE_RESOLUTION   = 0x8644
    HDCP_STATUS         = 0x864B
    BURN_IN_TEST        = 0x864C
    FACTORY_TEST        = 0x864D
    SET_LOG_STATUS      = 0x8651
    GET_TX_VERSION      = 0x8652
    UART_READY          = 0x8654
    PAIRED_DEV_STATUS   = 0x8655
    FW_UPGRADE_CTRL     = 0x8656

    NULL_CONSOLE        = 0x870B
    PRODUCT_NUM         = 0x870C

    ROTATION            = 0x8823
    SWITCH_USB_MODE     = 0x8824
    SWITCH_WIFI_CHN     = 0x8825
    SET_OVERSCAN        = 0x8829
    SET_PORTRAIT_MODE   = 0x8830
    UI_CTRL             = 0x8831
    HDMI_ENABLE         = 0x8832
    SET_PORTRAIT_ZOOM   = 0x8833
    EDID_PASSTHROUGH    = 0x8834
    MIRROR_ROTATE_EN    = 0x8835
    CAMERA_PORTRAIT     = 0x8836
    SCREEN_CAST         = 0x8838
    ENCODE_PARAM        = 0x8839

    PAIRING_CTRL        = 0x890C
    SYSTEM_COMMAND      = 0x890D
    EZAIR_MODE          = 0x890E
    WALLPAPER           = 0x890F
    LOW_POWER_MODE      = 0x8911
    CAMERA_SET_CTRL     = 0x8940

    TX_SSID             = 0x820D   # 文档 2.1，固件枚举里没有
    TX_HDMI_STATUS      = 0x8653   # 文档 2.6，固件枚举里没有


class Err(IntEnum):
    """应答 data 前 2 字节的返回码，取自固件 UART_ERR 枚举。

    ``>= 0`` 都表示正常：VALUE 是"本包带数据"，SUCCESS 是"指令执行成功"。
    负数一律是错误。PACKET_DONE 被用作分包传输的结束标记。
    """

    SAME_SETTING      = 2
    SUCCESS           = 1
    VALUE             = 0
    FAILED            = -1
    OUT_OF_RANGE      = -2
    INVALID_CMD       = -3
    TIMEOUT           = -4
    UNKNOWN           = -5
    BUSY              = -6
    INCORRECT_SOURCE  = -7
    INCORRECT_PWD     = -8
    NOT_SUPPORT       = -9
    CHECK_SUM         = -10
    PACKET_DONE       = -11


_ERR_DESC = {
    Err.SAME_SETTING:  "设置未变",
    Err.VALUE:         "带数据",
    Err.INCORRECT_PWD: "密码错",
    Err.PACKET_DONE:   "分包结束",
}


def err_name(rc: int) -> str:
    """返回码转可读名字。未知值也能安全打印。"""
    try:
        e = Err(rc)
    except ValueError:
        return f"未知返回码({rc})"
    desc = _ERR_DESC.get(e)
    return f"{e.name}({desc})" if desc else e.name


# ---------------------------------------------------------------- NET_STA_CTRL
STA_SCAN        = 0x00
STA_SCAN_RESULT = 0x01
STA_CONNECT     = 0x02
STA_FORGET      = 0x03

FORGET_ALL = 0xFF

# ------------------------------------------------------------ SWITCH_WIFI_CHN
WIFI_CHN_2G4  = 0x01
WIFI_CHN_5G   = 0x02
WIFI_CHN_AUTO = 0x03

# 状态变化通知的 data。只有 WiFi 状态真的变了才发，所以它的缺席
# 不代表命令失败 —— 设备本来就没连网络时，忘记网络不会有断开通知。
STATE_NOTIFY_DATA = bytes([0x00, 0x00, 0x06, 0x00, 0x00])


def resp_id_of(req_id: int) -> int:
    """请求 ID 对应的应答 ID：bit15 清零、bit14 置一。"""
    return (req_id & 0x3FFF) | 0x4000


@dataclass
class Packet:
    cmd_id: int
    data: bytes = b""
    checksum: int = 0

    # ---------------------------------------------------------------- 解析
    @property
    def rc(self) -> int:
        """前 2 字节的返回码。不足 2 字节时返回 UNKNOWN。"""
        if len(self.data) < 2:
            return int(Err.UNKNOWN)
        return struct.unpack_from("<h", self.data)[0]

    @property
    def is_ack(self) -> bool:
        """是应答包而不是请求。按标志位判断，对所有命令通用，
        不需要给每条命令硬编码应答 ID。"""
        return ((self.cmd_id >> 8) & 0xC0) == 0x40

    @property
    def ok(self) -> bool:
        """应答且返回码非负。

        这里必须检查返回码 —— 只看"包格式对"会把 FAILED、
        INCORRECT_PWD 这类错误当成成功。
        """
        return self.is_ack and len(self.data) >= 2 and self.rc >= 0

    @property
    def is_state_notify(self) -> bool:
        return self.data == STATE_NOTIFY_DATA

    @property
    def is_packet_done(self) -> bool:
        """分包传输的结束标记（扫描列表末包）。"""
        return len(self.data) == 2 and self.rc == Err.PACKET_DONE

    # ---------------------------------------------------------------- 编解码
    def pack(self) -> bytes:
        # data 为空表示查询指令，data_len 填 -1 (FF FF)
        dlen = len(self.data) if self.data else -1
        return _HEAD.pack(HEADER, self.checksum, self.cmd_id, dlen) + self.data

    @classmethod
    def unpack(cls, raw: bytes) -> "Packet":
        if len(raw) < HEAD_LEN:
            raise ValueError(f"包太短: {len(raw)} 字节")

        tag, checksum, cmd_id, dlen = _HEAD.unpack_from(raw)
        if tag != HEADER:
            raise ValueError(f"包头不对: {tag.hex(' ').upper()}")

        n = dlen if dlen > 0 else 0     # 有符号，负值表示无数据
        if len(raw) < HEAD_LEN + n:
            raise ValueError(
                f"数据不完整: 声明 {n} 字节，实收 {len(raw) - HEAD_LEN}")

        return cls(cmd_id=cmd_id, data=raw[HEAD_LEN:HEAD_LEN + n],
                   checksum=checksum)

    def __str__(self) -> str:
        return f"cmd=0x{self.cmd_id:04X} rc={self.rc} len={len(self.data)}"


def build(cmd: int, data: bytes = b"") -> Packet:
    """通用命令构造。data 留空即构造查询指令（data_len = FF FF）。

    加新指令只用调它，不需要再写一个 builder。
    """
    data = bytes(data)
    if len(data) > MAX_DATA:
        raise ValueError(f"data 过长: {len(data)} 字节")
    return Packet(cmd_id=int(cmd), data=data)


def build_connect(ssid: str, password: str, authen: str = "WPA+SAE") -> Packet:
    """连接 WiFi。这个格式实测连续 54 个循环通过，不要改。

    authen 用 ``WPA+SAE`` 是设备接受的写法，即使扫描结果报的是 ``WPA-PSK``。
    """
    cfg = f"SSID={ssid}\tauthen={authen}\tpsk={password}\tscan_ssid=1"
    return build(Cmd.NET_STA_CTRL, bytes([STA_CONNECT]) + cfg.encode())


# ---------------------------------------------------------------- 应答解析
def network_count(pkt: Packet) -> int:
    """扫描应答里的网络个数。

    data 布局是 ``rc(2) + 1 字节 + count(4 字节小端)``，所以个数在偏移 3。
    """
    if len(pkt.data) < 7:
        return 0
    return struct.unpack_from("<I", pkt.data, 3)[0]


@dataclass
class ScanEntry:
    ssid: str
    auth: str
    bssid: str = ""
    freq: str = ""
    fields: list = field(default_factory=list)


def parse_scan_list(blob: bytes) -> dict:
    """把拼接好的扫描列表解析成 ``{ssid: ScanEntry}``。

    每条记录形如 ``SSID\\tauth\\t?\\t?\\tBSSID\\t频率\\n``。
    尾部填充的 ``0x00`` 当记录分隔符处理，否则会截断解析。
    """
    text = blob.replace(b"\x00", b"\n").decode("utf-8", errors="replace")
    out = {}

    for line in text.split("\n"):
        f = line.split("\t")
        if len(f) >= 6 and f[0]:
            out[f[0]] = ScanEntry(
                ssid=f[0], auth=f[1], bssid=f[4], freq=f[5], fields=f)
    return out


def hex_str(data: bytes) -> str:
    """``b'GT'`` → ``'47 54'``"""
    return data.hex(" ").upper()


def parse_hex(text: str) -> bytes:
    """把 ``'47 54 00 00'`` / ``'475400'`` / ``'0x47,0x54'`` 解析成字节。

    空格、逗号、冒号、连字符都可省略，大小写均可，支持 0x 前缀。
    位数不成对或含非法字符时抛 ValueError。
    """
    cleaned = text.lower()
    for ch in " \t,:-":
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.replace("0x", "")

    if len(cleaned) % 2:
        raise ValueError("十六进制位数不成对")

    return bytes.fromhex(cleaned)     # 非法字符由 fromhex 报错
