"""指令表：纯数据，与逻辑分离。加指令只改这个文件。

跟 C 版的区别：那边每条指令是手写的十六进制字符串，命令码一改就会
和字符串脱节；这里的 ``hex`` 是从 ``cmd`` + ``data`` **算出来的**，
不可能不一致。空 ``data`` 自动构造成查询指令（data_len = FF FF）。

``ask`` 标记"这条指令的 data 得现问用户"——SSID、密码这类没有合理默认值的
东西，写死一个示例值只会让人以为能直接发。具体怎么问在 uart_stress_test.py
的 ``ASK_HANDLERS`` 里，这个文件只放**名字**，保持纯数据不掺交互逻辑。
"""

from dataclasses import dataclass, field

from uart_protocol import (
    Cmd, STA_CONNECT, STA_FORGET, STA_SCAN, STA_SCAN_RESULT, Tag,
    WIFI_CHN_2G4, WIFI_CHN_5G, WIFI_CHN_AUTO,
    build, hex_str, tag_name,
)


@dataclass(frozen=True)
class CmdDef:
    group: str
    name: str
    cmd: Cmd
    data: bytes = b""       # 留空 = 查询指令
    danger: bool = False    # 有副作用（重启、恢复出厂、开 log）
    tag: int = Tag.TG       # 非 TG 就是 Remote 指令，由对端执行
    ask: str = ""           # 非空 = data 现场问用户，值是 handler 名字

    @property
    def hex(self) -> str:
        """线上字节。``ask`` 类返回空串 —— 它的 data 还不存在。"""
        if self.ask:
            return ""
        return hex_str(build(self.cmd, self.data, tag=self.tag).pack())

    @property
    def label(self) -> str:
        # Remote 指令要在名字上标出来 —— 发错对象（本地当对端）时
        # 症状是超时，光看名字分不出来
        mark = "!! " if self.danger else ""
        via = "" if self.tag == Tag.TG else f" [{tag_name(self.tag)}]"
        return mark + self.name + via


def _b(*vals: int) -> bytes:
    return bytes(vals)


COMMANDS: list = [
    # ------------------------------------------------------------ 旋转缩放
    CmdDef("旋转缩放", "旋转 0 度",        Cmd.ROTATION, _b(0x00)),
    CmdDef("旋转缩放", "旋转 90 度",       Cmd.ROTATION, _b(0x01)),
    CmdDef("旋转缩放", "旋转 270 度",      Cmd.ROTATION, _b(0x03)),
    CmdDef("旋转缩放", "查询 旋转角度",     Cmd.ROTATION),
    CmdDef("旋转缩放", "缩放 100%",        Cmd.SET_OVERSCAN, _b(0x64)),
    CmdDef("旋转缩放", "缩放 120%",        Cmd.SET_OVERSCAN, _b(0x78)),
    CmdDef("旋转缩放", "缩放 130%",        Cmd.SET_OVERSCAN, _b(0x82)),
    CmdDef("旋转缩放", "查询 缩放比例",     Cmd.SET_OVERSCAN),
    CmdDef("旋转缩放", "旋转0 + 缩放100",  Cmd.SET_PORTRAIT_MODE,
           _b(0x00, 0x00, 0x64, 0x00)),
    CmdDef("旋转缩放", "旋转90 + 缩放100", Cmd.SET_PORTRAIT_MODE,
           _b(0x5A, 0x00, 0x64, 0x00)),
    CmdDef("旋转缩放", "旋转270 + 缩放100", Cmd.SET_PORTRAIT_MODE,
           _b(0x0E, 0x01, 0x64, 0x00)),
    CmdDef("旋转缩放", "查询 旋转缩放",     Cmd.SET_PORTRAIT_MODE),
    CmdDef("旋转缩放", "查询 旋转使能",     Cmd.MIRROR_ROTATE_EN),

    # ---------------------------------------------------------------- 网络
    CmdDef("网络", "扫描网络",         Cmd.NET_STA_CTRL, _b(STA_SCAN)),
    CmdDef("网络", "获取扫描结果",      Cmd.NET_STA_CTRL, _b(STA_SCAN_RESULT)),
    CmdDef("网络", "查询 网络状态",     Cmd.NET_STA_CTRL),
    CmdDef("网络", "忘记所有网络",      Cmd.NET_STA_CTRL, _b(STA_FORGET, 0xFF)),
    CmdDef("网络", "忘记网络 id=0",    Cmd.NET_STA_CTRL, _b(STA_FORGET, 0x00)),
    # SSID / 加密方式 / 密码现场问 —— 原来这里写死了一个示例网络的
    # SSID 和密码，直接发只会连一个不存在的网络
    CmdDef("网络", "连接WiFi", Cmd.NET_STA_CTRL, ask="wifi_connect"),

    # ------------------------------------------------------------ 模式频段
    CmdDef("模式频段", "点对点模式 P2P",    Cmd.NET_ROLE, _b(0x00)),
    CmdDef("模式频段", "三合一 AP+STA",    Cmd.NET_ROLE, _b(0x01)),
    CmdDef("模式频段", "查询 当前模式",     Cmd.NET_ROLE),
    CmdDef("模式频段", "WiFi 切 2.4G",     Cmd.SWITCH_WIFI_CHN, _b(WIFI_CHN_2G4)),
    CmdDef("模式频段", "WiFi 切 5G",       Cmd.SWITCH_WIFI_CHN, _b(WIFI_CHN_5G)),
    CmdDef("模式频段", "WiFi 互斥切换",     Cmd.SWITCH_WIFI_CHN, _b(WIFI_CHN_AUTO)),
    CmdDef("模式频段", "区域码 中国 CN",    Cmd.NET_AP_PARAM, _b(0x06, 0x03)),
    CmdDef("模式频段", "区域码 日本 JP",    Cmd.NET_AP_PARAM, _b(0x06, 0x05)),
    CmdDef("模式频段", "查询 区域码",       Cmd.NET_AP_PARAM),

    # ------------------------------------------------------------ 热点信息
    CmdDef("热点信息", "查询 热点 SSID",    Cmd.AP_SSID),
    # 改 SSID / 改密码都是改持久配置，值必须用户自己给：
    # 原来写死 abcd / 12345678，一按就把设备热点改成示例值了
    CmdDef("热点信息", "改 热点 SSID",      Cmd.AP_SSID,
           danger=True, ask="ap_ssid"),
    CmdDef("热点信息", "查询 热点密码",     Cmd.AP_PWD),
    CmdDef("热点信息", "改 热点密码",       Cmd.AP_PWD,
           danger=True, ask="ap_pwd"),
    CmdDef("热点信息", "查询 设备名称",     Cmd.DEV_NAME),
    CmdDef("热点信息", "查询 Mac 地址",     Cmd.WIFI_MAC_ADDR),
    CmdDef("热点信息", "查询 WiFi 状态",    Cmd.WIFI_STATUS),

    # ------------------------------------------------------------ 显示输出
    CmdDef("显示输出", "HDCP 禁用",        Cmd.HDCP_STATUS, _b(0x00)),
    CmdDef("显示输出", "HDCP 使能",        Cmd.HDCP_STATUS, _b(0x01)),
    CmdDef("显示输出", "查询 HDCP 状态",    Cmd.HDCP_STATUS),
    CmdDef("显示输出", "开启 HDMI 输出",    Cmd.HDMI_ENABLE, _b(0x01)),
    CmdDef("显示输出", "关闭 HDMI 输出",    Cmd.HDMI_ENABLE, _b(0x00)),
    CmdDef("显示输出", "查询 HDMI 状态",    Cmd.HDMI_ENABLE),
    CmdDef("显示输出", "查询 分辨率",       Cmd.NATIVE_RESOLUTION),

    # ---------------------------------------------------------------- 音量
    CmdDef("音量", "静音 开",       Cmd.AUDIO_MUTE, _b(0x01)),
    CmdDef("音量", "静音 关",       Cmd.AUDIO_MUTE, _b(0x00)),
    CmdDef("音量", "音量 设为 7",   Cmd.AUDIO_VOLUME, _b(0x07)),
    CmdDef("音量", "音量 设为 14",  Cmd.AUDIO_VOLUME, _b(0x0E)),
    CmdDef("音量", "查询 音量",     Cmd.AUDIO_VOLUME),

    # ------------------------------------------------------------ 投屏配对
    CmdDef("投屏配对", "开始投屏",       Cmd.SCREEN_CAST, _b(0x01)),
    CmdDef("投屏配对", "断开投屏",       Cmd.SCREEN_CAST, _b(0x00)),
    CmdDef("投屏配对", "查询 投屏状态",   Cmd.SCREEN_CAST),
    CmdDef("投屏配对", "执行配对",       Cmd.PAIRING_CTRL, _b(0x00, 0x01)),
    CmdDef("投屏配对", "查询 配对状态",   Cmd.PAIRING_CTRL),
    CmdDef("投屏配对", "查询 UI/同屏状态", Cmd.SOURCE_STA),

    # ------------------------------------------------------------ 版本升级
    CmdDef("版本升级", "查询 Rx 版本",     Cmd.FW_VERSION),
    CmdDef("版本升级", "查询 配对Tx 版本",  Cmd.GET_TX_VERSION),
    CmdDef("版本升级", "本地版本检测",      Cmd.FW_UPGRADE_CTRL, _b(0x01, 0x00, 0x01)),
    CmdDef("版本升级", "执行本地升级",      Cmd.FW_UPGRADE_CTRL, _b(0x02, 0x00, 0x01),
           danger=True),
    CmdDef("版本升级", "远端Tx版本检查",    Cmd.FW_UPGRADE_CTRL, _b(0x01, 0x01, 0x01)),

    # ---------------------------------------------------------------- 日志
    # !! 打开 log 之后设备**不再回应任何 UART 命令**，而且没法靠再发命令
    # 救回来（连"关闭 log"也发不进去）。它只在 debug 时用，所以开 log 的
    # 三条全标 danger；关 log 是安全方向，不标。
    CmdDef("日志", "打开所有 log",   Cmd.SET_LOG_STATUS, _b(0x03), danger=True),
    CmdDef("日志", "只开 actui log", Cmd.SET_LOG_STATUS, _b(0x01), danger=True),
    CmdDef("日志", "只开内核 log",    Cmd.SET_LOG_STATUS, _b(0x02), danger=True),
    CmdDef("日志", "关闭所有 log",    Cmd.SET_LOG_STATUS, _b(0x00)),
    CmdDef("日志", "查询 log 设置",   Cmd.SET_LOG_STATUS),

    # ---------------------------------------------------------------- 系统
    CmdDef("系统", "查询 编码参数",  Cmd.ENCODE_PARAM),
    CmdDef("系统", "dmesg",         Cmd.SYSTEM_COMMAND, b"dmesg"),
    CmdDef("系统", "重启设备",       Cmd.SYSTEM_COMMAND, b"reboot", danger=True),
    CmdDef("系统", "恢复出厂设置",   Cmd.RESET_TO_DEFAULT, _b(0x00), danger=True),

    # ------------------------------------------------------------ Remote
    # TAG=PL：本地 AM 透过 WiFi 送给对端 AM 执行。**要先和对端配对上**，
    # 没配对时就是超时。字节和 Remote_Rx.ptp / Remote_Tx.ptp 一致。
    CmdDef("Remote", "查询 对端Mac地址", Cmd.WIFI_MAC_ADDR, tag=Tag.PL),
    CmdDef("Remote", "查询 对端SSID",    Cmd.TX_SSID,       tag=Tag.PL),
    CmdDef("Remote", "查询 对端旋转角度", Cmd.ROTATION,      tag=Tag.PL),
    CmdDef("Remote", "查询 对端缩放比例", Cmd.SET_OVERSCAN,  tag=Tag.PL),
    CmdDef("Remote", "对端旋转 90 度",   Cmd.ROTATION, _b(0x01), tag=Tag.PL),
    CmdDef("Remote", "对端旋转 270 度",  Cmd.ROTATION, _b(0x03), tag=Tag.PL),
]


def grouped() -> dict:
    """按分组返回 ``{组名: [(序号, CmdDef), ...]}``，序号从 1 开始。"""
    out: dict = {}
    for i, c in enumerate(COMMANDS, 1):
        out.setdefault(c.group, []).append((i, c))
    return out
