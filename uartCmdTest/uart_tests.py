"""测试集定义。**加/删测试只改这个文件。**

指令来源是 C 版目录下的 `Uart command.md`，按功能分组。

四种写法：

* ``simple(cmd, data)``  —— 设置类，只看 ``rc >= 0``
* ``query_int(cmd, n)``  —— 查询类，解析返回的整数并打印
* ``query_str(cmd)``     —— 查询类，解析返回的 ASCII 串并打印
* ``query_hex(cmd)``     —— 查询类，返回结构未知时打十六进制
* 自己写函数            —— 判定逻辑特殊的（多分包、找字节序列、可选包）

``needs`` 声明依赖：依赖没通过时本测试自动跳过，报告里区分"失败"和
"跳过"，一眼能看出真正的失败点。

``tags`` 用于 ``--tags`` 筛选，被选中的测试会自动带上它的依赖。

.. warning::
   ``danger`` tag 的测试会改设备持久配置或重启设备（改 SSID/密码、
   恢复出厂、重启、执行升级）。

   ``logmode`` tag 的测试会打开设备 log，**之后所有 UART 命令都会失效**，
   整轮测试就地死掉，而且没法靠再发命令救回来。log 只在 debug 时手动用，
   测试里对 log 只做查询。

   这两类**都不带任何其它 tag**，所以只有显式 ``--tags danger`` /
   ``--tags logmode`` 才会跑到。
"""

import uart_ui as ui
from uart_exec import (
    MAX_LIST_PKTS, NAME_CONFIRM_TIMEOUT, NOTIFY_TIMEOUT,
    Ctx, Test, TestFailed, query_hex, query_int, query_str,
    set_and_restore, set_and_verify, simple,
)
from uart_protocol import (
    Cmd, STA_FORGET, STA_SCAN, STA_SCAN_RESULT, Tag,
    WIFI_CHN_2G4, WIFI_CHN_5G, WIFI_CHN_AUTO,
    build_connect, err_name, network_count, parse_scan_list, resp_id_of,
)

FORGET_ALL = 0xFF

# 查询应答里数值的含义，取自 Uart command.md 的枚举表。
# 有了这些，查询结果会打成 "3 → WPS_STATUS_TIMEOUT(配对超时)" 而不是干巴巴一个 3。
WPS_STATUS = {
    0: "WPS_STATUS_START(配对开始)",
    1: "WPS_STATUS_EAP(EAP认证)",
    2: "WPS_STATUS_FAILED(配对失败)",
    3: "WPS_STATUS_TIMEOUT(配对超时)",
    4: "WPS_STATUS_SUCCESS(配对成功)",
    5: "WPS_STATUS_EXIT(配对退出)",
}

PAIRED_DEV_STATUS = {
    0: "DISCONNECT(设备断开)",
    1: "CONNECTED(设备已连接)",
    2: "CAST_ON(投屏开始)",
    3: "CAST_OFF(停止投屏)",
    4: "UNKNOWN(状态未知)",
}

ROTATION_ANGLE = {0: "0度", 1: "90度", 2: "180度", 3: "270度"}

NET_ROLE_NAME = {0: "点对点 P2P", 1: "三合一 AP+STA"}

LOG_STATUS = {0: "全关", 1: "仅 actui", 2: "仅内核", 3: "全开"}

ON_OFF = {0: "关", 1: "开"}

# 查询结果的格式约束。卡格式最能抓到"设备回了垃圾"或"流已错位" ——
# 这两种情况下返回值往往还是能解析成某个数，只有形状对不上才暴露。
RE_MAC = r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}"   # FC:19:28:36:95:69
RE_VERSION = r"\d{6,10}"                            # 27243000

# 音量范围。文档只演示了设 7 和 14，没给上限，所以放宽到 0..100，
# 只用来卡明显异常（比如返回 255 或负数解析结果）。
VOLUME_MAX = 100

# 缩放比例。文档给了 100/120/130 三档，放宽到 50..300 卡异常值。
ZOOM_LO, ZOOM_HI = 50, 300

# 耗时上限（秒）。依据是 test1.log 的实测延迟，各留约 3 倍余量：
#   扫描约 4.9s / 获取结果约 0.9s / 连接约 5.2s / 忘记 ack 约 25ms
# 卡这个是为了抓性能退化 —— 设备还在回应、返回码也对，但连接从
# 5 秒变成 25 秒。只看通过/失败发现不了。
MAX_SCAN      = 15.0
MAX_SCAN_RES  = 10.0
MAX_CONNECT   = 20.0
MAX_FORGET    = 10.0
MAX_QUICK     = 5.0      # 单条设置/查询类命令


# ============================================================ WiFi 主流程
def t_scan(ctx: Ctx) -> None:
    pkt = ctx.step(Cmd.NET_STA_CTRL, bytes([STA_SCAN]))
    ctx.scan_count = network_count(pkt)
    ui.log(f"✓ 扫描完成，发现 {ctx.scan_count} 个网络")

    # 扫到 0 个时目标 SSID 不可能在列表里，后面的连接必然失败，
    # 而且大概率是"设备不回应"那种长超时。早失败早报错。
    if ctx.scan_count <= 0:
        raise TestFailed("扫描结果为 0 个网络，无法继续连接")


def t_scan_result(ctx: Ctx) -> None:
    """扫描列表分多个包发（单包 data 上限 1016 字节）。

    必须一直读到 PACKET_DONE 结束标记：少读一个包，残留数据就会被
    后面的命令误当成自己的应答，整条流从此错位。
    """
    pkt = ctx.step(Cmd.NET_STA_CTRL, bytes([STA_SCAN_RESULT]))

    blob, n = bytearray(), 1
    while not pkt.is_packet_done and n < MAX_LIST_PKTS:
        blob += pkt.data[2:]        # 剥掉前 2 字节返回码，其余是列表文本
        n += 1

        pkt = ctx.recv()
        if pkt is None:
            raise TestFailed(f"接收第 {n} 个分包失败")

    if not pkt.is_packet_done:
        raise TestFailed(f"未收到 PACKET_DONE 结束标记（已读 {MAX_LIST_PKTS} 包）")

    ctx.networks = parse_scan_list(bytes(blob))
    head = f"✓ {n} 个包 {len(blob)} 字节 | 解析出 {len(ctx.networks)} 个网络"

    if not ctx.ssid:                    # 没给 -s 时只报总数，不算问题
        ui.log(head)
        return

    target = ctx.networks.get(ctx.ssid)
    if target:
        ui.log(f"{head} | {ctx.ssid} 加密={target.auth} "
               f"频率={target.freq} BSSID={target.bssid}")
    else:
        ui.log(f"{head} | ⚠ 其中没有 {ctx.ssid}")


def t_connect(ctx: Ctx) -> None:
    """判定标准是应答里出现目标 SSID 的字节。

    data 含 0x00，所以按字节序列找而不是当字符串处理。
    网络名确认包收不到只告警不失败 —— 命令本身可能已经执行了。
    """
    ctx.send(build_connect(ctx.ssid, ctx.password))

    want = resp_id_of(Cmd.NET_STA_CTRL)
    pkt = ctx.recv_reply(want)
    if pkt is None:
        raise TestFailed("没有收到应答")
    if not pkt.ok:
        raise TestFailed(f"rc={pkt.rc} {err_name(pkt.rc)}")

    needle = ctx.ssid.encode()
    confirmed = needle in pkt.data or ctx.wait_optional(
        want, lambda p: needle in p.data, NAME_CONFIRM_TIMEOUT) is not None

    if confirmed:
        ui.log(f"✓ 连接成功，网络名已确认: {ctx.ssid}")
    else:
        ctx.warn(f"连接已执行，但未收到 {ctx.ssid} 的网络名确认")


def t_forget(ctx: Ctx) -> None:
    """ack 必需；断开通知是可选包。

    ``00 00 06 00 00`` 是状态变化通知，设备本来没连网络时不会发，
    当必需包会干等到超时误判失败。
    """
    pkt = ctx.step(Cmd.NET_STA_CTRL, bytes([STA_FORGET, FORGET_ALL]))
    rc = pkt.rc

    got = ctx.wait_optional(resp_id_of(Cmd.NET_STA_CTRL),
                            lambda p: p.is_state_notify, NOTIFY_TIMEOUT)

    if got is not None:
        ui.log(f"✓ 忘记网络成功 (rc={rc} {err_name(rc)})，已收到断开通知")
    else:
        ctx.warn(f"忘记网络已执行 (rc={rc})，但未收到断开通知")


def t_switch_5g(ctx: Ctx) -> None:
    """已经在 5G 时设备可能回 SAME_SETTING(2)，也算成功。"""
    pkt = ctx.step(Cmd.SWITCH_WIFI_CHN, bytes([WIFI_CHN_5G]))
    ui.log(f"✓ 已切换到 5G (rc={pkt.rc} {err_name(pkt.rc)})")


def t_net_status(ctx: Ctx) -> None:
    """网络状态查询。应答里带当前连上的网络名（文档 1.20 Step 5）。"""
    pkt = ctx.step(Cmd.NET_STA_CTRL)

    # 布局是 rc(2) + 若干字段 + 网络名，名字位置不固定，
    # 取可打印的连续片段当结果
    tail = pkt.data[2:].split(b"\x00")
    names = [s.decode("utf-8", errors="replace") for s in tail
             if len(s) >= 2 and all(0x20 <= b < 0x7F for b in s)]

    if names:
        ui.log(f"✓ 当前网络: {' / '.join(names)}")
    else:
        ui.log(f"✓ 应答 {len(pkt.data)} 字节（未连接或无网络名）")


# ============================================================ 测试集
# 列表顺序 = 执行顺序。删减、调序就是编辑这个列表。
TESTS = [
    # -------------------------------------------------- WiFi 老化主流程
    # 默认跑的就是这 5 条（DEFAULT_TAGS = core）
    # max_duration 卡时延退化，依据是实测延迟留 3 倍余量
    #
    # needs_sta: 这四条是"连路由"的流程，只有开热点的那一方才做 ——
    # 一对一的 Tx / 一对多的 Rx 连的是对端热点，压根没有连路由这回事，
    # 对它们跑这四条只会全部超时。
    Test("扫描网络",       t_scan,        tags=("core", "wifi"),
         needs_sta=True, max_duration=MAX_SCAN),
    Test("获取扫描结果",    t_scan_result, needs=("扫描网络",),
         tags=("core", "wifi"), needs_sta=True, max_duration=MAX_SCAN_RES),
    # needs_creds: 只有这条真的要用 ssid/password
    Test("连接网络",       t_connect,     needs=("获取扫描结果",),
         tags=("core", "wifi"), needs_creds=True, needs_sta=True,
         max_duration=MAX_CONNECT),
    Test("忘记网络",       t_forget,      needs=("连接网络",),
         tags=("core", "wifi"), needs_sta=True, max_duration=MAX_FORGET),
    # 切频段是本机自己的射频设置，两种角色都做得到，不卡 needs_sta
    Test("切换到5G",       t_switch_5g,   tags=("core", "wifi", "band"),
         max_duration=MAX_QUICK),

    # -------------------------------------------------- 只读查询（安全）
    Test("查询 网络状态",   t_net_status,                     tags=("query", "wifi")),
    Test("查询 WiFi状态",   query_hex(Cmd.WIFI_STATUS),       tags=("query", "wifi")),
    # 格式明确的用正则卡形状
    Test("查询 Mac地址",
         query_str(Cmd.WIFI_MAC_ADDR, pattern=RE_MAC),  tags=("query",)),
    Test("查询 设备名称",   query_str(Cmd.DEV_NAME),          tags=("query",)),
    Test("查询 Rx版本",
         query_str(Cmd.FW_VERSION, pattern=RE_VERSION),  tags=("query",)),
    # 问的是"配对上的那个 Tx 的版本"，所以要求本机是 Rx
    Test("查询 Tx版本",
         query_str(Cmd.GET_TX_VERSION, pattern=RE_VERSION),
         tags=("query",), roles=("rx",)),
    # 热点参数只有开热点的那一方才有意义
    Test("查询 热点SSID",   query_str(Cmd.AP_SSID),
         tags=("query", "ap"), needs_ap=True),
    # 热点密码按 WPA 规范至少 8 位
    Test("查询 热点密码",
         query_str(Cmd.AP_PWD, min_len=8),
         tags=("query", "ap"), needs_ap=True),
    # allowed=True 直接复用 names 的键当合法集合，不用把枚举写两遍
    Test("查询 当前模式",
         query_int(Cmd.NET_ROLE, names=NET_ROLE_NAME, allowed=True),
         tags=("query", "net")),
    Test("查询 区域码",     query_hex(Cmd.NET_AP_PARAM),      tags=("query", "net")),
    Test("查询 UI同屏状态", query_hex(Cmd.SOURCE_STA),        tags=("query", "cast")),
    # 应答 00 00 00 03: rc 之后夹了一个字节，状态值在偏移 1
    Test("查询 配对状态",
         query_int(Cmd.PAIRING_CTRL, offset=1, names=WPS_STATUS, allowed=True),
         tags=("query", "cast")),
    Test("查询 配对设备状态",
         query_int(Cmd.PAIRED_DEV_STATUS, names=PAIRED_DEV_STATUS, allowed=True),
         tags=("query", "cast")),
    Test("查询 投屏状态",   query_hex(Cmd.SCREEN_CAST),       tags=("query", "cast")),
    Test("查询 HDCP状态",
         query_int(Cmd.HDCP_STATUS, names=ON_OFF, allowed=True),
         tags=("query", "display")),
    Test("查询 HDMI输出",
         query_int(Cmd.HDMI_ENABLE, names=ON_OFF, allowed=True),
         tags=("query", "display")),
    Test("查询 分辨率",     query_hex(Cmd.NATIVE_RESOLUTION), tags=("query", "display")),
    Test("查询 旋转角度",
         query_int(Cmd.ROTATION, names=ROTATION_ANGLE, allowed=True),
         tags=("query", "display")),
    Test("查询 缩放比例",
         query_int(Cmd.SET_OVERSCAN, size=2, unit="%", lo=ZOOM_LO, hi=ZOOM_HI),
         tags=("query", "display")),
    Test("查询 旋转缩放",   query_hex(Cmd.SET_PORTRAIT_MODE), tags=("query", "display")),
    Test("查询 旋转使能",
         query_int(Cmd.MIRROR_ROTATE_EN, names=ON_OFF, allowed=True),
         tags=("query", "display")),
    Test("查询 音量",
         query_int(Cmd.AUDIO_VOLUME, lo=0, hi=VOLUME_MAX), tags=("query", "audio")),
    Test("查询 log设置",
         query_int(Cmd.SET_LOG_STATUS, names=LOG_STATUS, allowed=True),
         tags=("query", "log")),
    Test("查询 编码参数",   query_hex(Cmd.ENCODE_PARAM),      tags=("query", "encode")),

    # -------------------------------------------------- 显示（旋转/缩放）
    # 这批用 set_and_verify: 设置后回读确认真的生效了，而不是只看 rc
    Test("旋转 0度",
         set_and_verify(Cmd.ROTATION, b"\x00", 0, names=ROTATION_ANGLE),
         tags=("display", "rotate")),
    Test("旋转 90度",
         set_and_verify(Cmd.ROTATION, b"\x01", 1, names=ROTATION_ANGLE),
         tags=("display", "rotate")),
    Test("旋转 270度",
         set_and_restore(Cmd.ROTATION, b"\x03", 3, names=ROTATION_ANGLE),
         tags=("display", "rotate")),
    Test("缩放 100%",
         set_and_verify(Cmd.SET_OVERSCAN, b"\x64", 100, size=2),
         tags=("display", "zoom")),
    Test("缩放 120%",
         set_and_verify(Cmd.SET_OVERSCAN, b"\x78", 120, size=2),
         tags=("display", "zoom")),
    Test("缩放 130%",
         set_and_verify(Cmd.SET_OVERSCAN, b"\x82", 130, size=2),
         tags=("display", "zoom")),

    # 旋转+缩放组合：data = 角度(2字节小端) + 比例(2字节小端)
    Test("旋转0+缩放100",
         simple(Cmd.SET_PORTRAIT_MODE, b"\x00\x00\x64\x00"), tags=("display",)),
    Test("旋转90+缩放100",
         simple(Cmd.SET_PORTRAIT_MODE, b"\x5A\x00\x64\x00"), tags=("display",)),
    Test("旋转270+缩放100",
         simple(Cmd.SET_PORTRAIT_MODE, b"\x0E\x01\x64\x00"), tags=("display",)),
    Test("旋转90+缩放130",
         simple(Cmd.SET_PORTRAIT_MODE, b"\x5A\x00\x82\x00"), tags=("display",)),

    # -------------------------------------------------- 显示（HDMI/HDCP）
    Test("HDMI输出 开",
         set_and_verify(Cmd.HDMI_ENABLE, b"\x01", 1, names=ON_OFF),
         tags=("display", "hdmi")),
    Test("HDMI输出 关",
         set_and_restore(Cmd.HDMI_ENABLE, b"\x00", 0, names=ON_OFF),
         tags=("display", "hdmi")),
    Test("HDCP 使能",
         set_and_verify(Cmd.HDCP_STATUS, b"\x01", 1, names=ON_OFF),
         tags=("display", "hdcp")),
    Test("HDCP 禁用",
         set_and_restore(Cmd.HDCP_STATUS, b"\x00", 0, names=ON_OFF),
         tags=("display", "hdcp")),

    # -------------------------------------------------- 音量
    # 静音没有对应的查询指令（文档 1.22 只给了音量查询），所以只能 simple()
    Test("静音 开",     simple(Cmd.AUDIO_MUTE, b"\x01"),   tags=("audio",)),
    Test("静音 关",     simple(Cmd.AUDIO_MUTE, b"\x00"),   tags=("audio",)),
    Test("音量 设为7",
         set_and_verify(Cmd.AUDIO_VOLUME, b"\x07", 7),  tags=("audio",)),
    Test("音量 设为14",
         set_and_verify(Cmd.AUDIO_VOLUME, b"\x0E", 14), tags=("audio",)),

    # -------------------------------------------------- 网络模式 / 频段
    Test("点对点模式 P2P",
         set_and_restore(Cmd.NET_ROLE, b"\x00", 0, names=NET_ROLE_NAME),
         tags=("net", "role")),
    Test("三合一 AP+STA",
         set_and_restore(Cmd.NET_ROLE, b"\x01", 1, names=NET_ROLE_NAME),
         tags=("net", "role")),
    Test("切换到2.4G",
         simple(Cmd.SWITCH_WIFI_CHN, bytes([WIFI_CHN_2G4])), tags=("net", "band")),
    Test("频段互斥切换",
         simple(Cmd.SWITCH_WIFI_CHN, bytes([WIFI_CHN_AUTO])), tags=("net", "band")),
    Test("区域码 中国CN", simple(Cmd.NET_AP_PARAM, b"\x06\x03"), tags=("net", "region")),
    Test("区域码 日本JP", simple(Cmd.NET_AP_PARAM, b"\x06\x05"), tags=("net", "region")),

    # -------------------------------------------------- 投屏 / 配对
    Test("开始投屏", simple(Cmd.SCREEN_CAST, b"\x01"),   tags=("cast",)),
    Test("断开投屏", simple(Cmd.SCREEN_CAST, b"\x00"),   tags=("cast",)),
    Test("执行配对", simple(Cmd.PAIRING_CTRL, b"\x00\x01"), tags=("cast", "pair")),

    # -------------------------------------------------- log 控制
    # !! 打开 log 会让**后续所有命令失效** —— 设备被 log 刷屏之后不再
    # 回应 UART 命令，整轮测试就地死掉，而且没法靠再发命令救回来。
    # log 只在 debug 时手动用。
    #
    # 所以这几条只挂 logmode 一个 tag，**故意不挂 log / query** ——
    # 只有显式 --tags logmode 才跑得到。测试里对 log 只做查询。
    Test("log 全开",
         set_and_verify(Cmd.SET_LOG_STATUS, b"\x03", 3, names=LOG_STATUS),
         tags=("logmode",)),
    Test("log 仅actui",
         set_and_verify(Cmd.SET_LOG_STATUS, b"\x01", 1, names=LOG_STATUS),
         tags=("logmode",)),
    Test("log 仅内核",
         set_and_verify(Cmd.SET_LOG_STATUS, b"\x02", 2, names=LOG_STATUS),
         tags=("logmode",)),
    # 全关是唯一"安全方向"的设置 —— 它把命令通道从 log 里救出来
    Test("log 全关",
         set_and_restore(Cmd.SET_LOG_STATUS, b"\x00", 0, names=LOG_STATUS),
         tags=("logmode",)),
    Test("临时开log",    simple(Cmd.NULL_CONSOLE, b"\x00"), tags=("logmode",)),
    Test("临时关log",    simple(Cmd.NULL_CONSOLE, b"\x01"), tags=("logmode",)),

    # -------------------------------------------------- OTA（只做版本检测）
    # 真正执行升级的在 danger 组里
    Test("本地版本检测",
         simple(Cmd.FW_UPGRADE_CTRL, b"\x01\x00\x01"), tags=("ota",)),
    Test("远端Tx版本检测",
         simple(Cmd.FW_UPGRADE_CTRL, b"\x01\x01\x01"), tags=("ota",)),

    # -------------------------------------------------- 编码参数（文档 2.15）
    Test("编码 帧率30",
         simple(Cmd.ENCODE_PARAM, b"\x01\x1E\x00"), tags=("encode",)),
    Test("编码 帧率60",
         simple(Cmd.ENCODE_PARAM, b"\x01\x3C\x00"), tags=("encode",)),
    Test("编码 分辨率1280x720",
         simple(Cmd.ENCODE_PARAM, b"\x00\x00\x05\xD0\x02\x03\x80\xBB\x00\x00"),
         tags=("encode",)),
    Test("编码 码率",
         simple(Cmd.ENCODE_PARAM, b"\x02\x00\x12\x7A\x00"), tags=("encode",)),

    # -------------------------------------------------- Tx 端专用（文档第 2 章）
    # roles=("tx",)：这两条问的是本机自己的 Tx 状态，只有本机是 Tx 才有意义。
    # 要问对端 Tx 的同样信息，走下面 Remote 那组。
    Test("Tx 查询SSID",  query_str(Cmd.TX_SSID),
         tags=("tx", "query"), roles=("tx",)),
    Test("Tx 查询HDMI",  query_hex(Cmd.TX_HDMI_STATUS),
         tags=("tx", "query"), roles=("tx",)),

    # -------------------------------------------------- 系统信息
    Test("dmesg", simple(Cmd.SYSTEM_COMMAND, b"dmesg"), tags=("sys",)),

    # -------------------------------------------------- Remote（对端执行）
    # TAG=PL：本地 AM 透过 WiFi 把命令送给对端 AM 执行，应答带同一个 TAG
    # 送回来。**前提是已经和对端配对上**，没配对时会超时 —— 所以不在
    # 默认集合里，要显式 --tags remote。
    #
    # 这几条抄自 Remote_Rx.ptp / Remote_Tx.ptp（doclight 实测用例），
    # 全是只读查询：改对端的持久配置得靠本地那套 danger 测试，不从这里做。
    #
    # 只挂 remote 一个 tag，**故意不挂 query / display** —— 否则
    # --tags query（号称"最安全"）会把它们拉进来，没配对时超时报成假失败。
    # 和 danger 一样：需要前置条件的测试只能显式点名。
    Test("Remote 查询对端Mac",
         query_str(Cmd.WIFI_MAC_ADDR, pattern=RE_MAC, tag=Tag.PL),
         tags=("remote",)),
    Test("Remote 查询对端SSID",
         query_str(Cmd.TX_SSID, tag=Tag.PL),
         tags=("remote",)),
    Test("Remote 查询对端旋转角度",
         query_int(Cmd.ROTATION, names=ROTATION_ANGLE, allowed=True,
                   tag=Tag.PL),
         tags=("remote",)),
    Test("Remote 查询对端缩放比例",
         query_int(Cmd.SET_OVERSCAN, size=2, unit="%",
                   lo=ZOOM_LO, hi=ZOOM_HI, tag=Tag.PL),
         tags=("remote",)),

    # ================================================== 有副作用的
    # 这些不带任何其它 tag，只有显式 --tags danger 才会跑到
    # 改的是本机热点，只有开热点的那一方才有热点可改
    Test("改SSID为abcd",
         simple(Cmd.AP_SSID, b"abcd"),                 tags=("danger",),
         needs_ap=True),
    Test("改密码为12345678",
         simple(Cmd.AP_PWD, b"12345678"),              tags=("danger",),
         needs_ap=True),
    Test("执行本地升级",
         simple(Cmd.FW_UPGRADE_CTRL, b"\x02\x00\x01"), tags=("danger",)),
    Test("恢复出厂设置",
         simple(Cmd.RESET_TO_DEFAULT, b"\x00"),        tags=("danger",)),
    Test("重启设备",
         simple(Cmd.SYSTEM_COMMAND, b"reboot"),        tags=("danger",)),
]

# 不给 --tags 时跑哪些。core = WiFi 老化那 5 条。
DEFAULT_TAGS = ["core"]

# 必须显式点名才会跑的 tag，--list-tests 里标 !!：
#   danger  —— 改设备持久配置或重启
#   logmode —— 打开 log 后设备不再回应任何命令，整轮测试就地死掉
DANGER_TAGS = ["danger", "logmode"]


# 全部跑完后恢复到这些已知值。
# 覆盖的是**没有查询指令、set_and_restore 读不回来**的状态 ——
# 否则跑完 --tags net 设备可能停在日本区、2.4G、P2P 模式上。
#
# data 一律写成 bytes([...])：这张表全是不可打印的控制字节，用转义写会被
# 各种编辑/生成环节悄悄变成裸字节（这里真发生过 —— 一度写成了裸 0x03，
# 在编辑器里完全看不见），用数字列表就没有这个歧义。
BASELINE = [
    ("区域码 中国CN",  Cmd.NET_AP_PARAM,    bytes([0x06, 0x03])),
    ("频段 5G",        Cmd.SWITCH_WIFI_CHN, bytes([WIFI_CHN_5G])),
    ("三合一模式",     Cmd.NET_ROLE,        bytes([0x01])),
    ("HDMI 开",        Cmd.HDMI_ENABLE,     bytes([0x01])),
    ("旋转 0度",       Cmd.ROTATION,        bytes([0x00])),
    ("缩放 100%",      Cmd.SET_OVERSCAN,    bytes([100])),
    ("静音 关",        Cmd.AUDIO_MUTE,      bytes([0x00])),
    # log 刻意不进基线。原来这里恢复成"全开"(0x03)，等于每轮收尾都把命令
    # 通道弄死 —— 打开 log 之后设备不再回应任何 UART 命令。恢复成全关又会
    # 盖掉用户自己设的 debug 状态，所以 log 状态归用户管，测试不碰。
    ("断开投屏",       Cmd.SCREEN_CAST,     bytes([0x00])),
]


def all_tags() -> list:
    """收集所有用到的 tag，供 --list-tests 显示。"""
    tags: set = set()
    for t in TESTS:
        tags |= set(t.tags)
    return sorted(tags)


def tag_counts() -> dict:
    """每个 tag 下有几条测试。"""
    out: dict = {}
    for t in TESTS:
        for g in t.tags:
            out[g] = out.get(g, 0) + 1
    return dict(sorted(out.items()))
