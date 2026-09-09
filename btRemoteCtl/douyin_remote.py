#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""树莓派伪装成蓝牙 HID 设备, 用来遥控手机刷视频。

四种模式 (命令行参数选择, 默认 --mouse-only)。想 Android 和 iOS 共用一份配置就用
--composite: 描述符里键盘/鼠标/多媒体全都有, 运行时按 'o' 键切换主操作方案 ——
Android 用多媒体键, iOS 用鼠标拖拽, 同一次配对不用重启也不用重配。

  --mouse-only    纯鼠标。单个 collection、不使用 Report ID, 和一个真实蓝牙鼠标
                  的描述符几乎一致。实测真鼠标的滚轮可以刷抖音, 所以这是首选。
                  CoD 应为 0x000580 (Peripheral + Pointing device)。
  --keyboard-media 键盘 + 多媒体键(consumer control), 无鼠标。实测罗技 K580 的
                  F 行发的就是 consumer 码 (F1=home F3=返回 F5/F6/F7=上一个/暂停/
                  下一个), 所以要复现键盘遥控就用这个模式。CoD 应为 0x000540。
  --keyboard-only 纯键盘。最早那套配置, 原样保留。CoD 应为 0x000540。
  --composite     键盘 + 鼠标 + 多媒体键, 用 Report ID 区分。【Android/iOS 共用】
                  功能最全, 代价是手机对复合描述符的处理差异更大。CoD 应为 0x0005c0。

另有一个配对相关的开关:

  --agent[=CAP]   在本脚本内注册配对代理 (org.bluez.Agent1), 默认 capability 为
                  KeyboardDisplay。iPhone/iPad 要求已认证配对(要输 6 位配对码),
                  用这个就能在本终端直接输入, 不需要另开 bt-agent / bluetoothctl。
                  启用时别再跑 bt-agent —— 默认代理同一时间只能有一个。

为什么要自己监听 L2CAP 端口、为什么要禁用 BlueZ 的 input 插件, 见 README.md。
"""

import os
import sys
import time
import select
import socket
import struct
import errno
import threading
import termios
import tty
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

# HID over BR/EDR 的两个固定 L2CAP 端口
P_CTRL = 17   # control 通道
P_INTR = 19   # interrupt 通道 (输入报告走这条)

# L2CAP 安全级别。真实 HID 设备都要求链路加密, 未加密链路上的输入报告有可能被
# 主机直接丢弃, 所以这里要求 medium (认证 + 加密)。出问题可以临时设为 False。
REQUIRE_ENCRYPTION = True
SOL_BLUETOOTH = getattr(socket, "SOL_BLUETOOTH", 274)
BT_SECURITY = 4
BT_SECURITY_MEDIUM = 2

# 手机屏幕尺寸, 只用于把鼠标指针挪到屏幕中间。
# Android 对相对鼠标位移有指针加速, 所以这里的单位不完全等于像素, 属于估值。
SCREEN_W = 1080
SCREEN_H = 2400
SWIPE_DIST = 700    # 一次拖拽滑动的纵向位移量 (报告单位)
SWIPE_STEP = 70     # 每个报告最多带多少位移, 越大越"快", 越容易触发 fling

# 指针"定位"时用的小步长。系统的指针加速会放大【快速】移动, 小步慢移基本 1:1。
# 用大步长定位会被放大到冲出屏幕、被边界夹在底部 —— 而从屏幕底部边缘向上拖,
# iOS 会当成"上滑回主屏"手势 (现象: 视频缩成小窗), 应用根本收不到这个事件。
POINTER_STEP = 12
POINTER_DELAY = 0.008
# 滑动起点在屏幕上的位置 (占宽/高的比例)。纵向刻意取偏上, 远离底部手势区
SWIPE_START_X_FRAC = 0.5
SWIPE_START_Y_FRAC = 0.45
NUDGE_STEP = 120    # 手动微调指针时每次移动多少

# 鼠标按键位 (报告第 1 字节的低 3 位)
BTN_LEFT = 0x01
BTN_RIGHT = 0x02
BTN_MIDDLE = 0x04
DOUBLE_CLICK_GAP = 0.08   # 双击间隔要短于系统的双击判定阈值 (一般 ~300ms)
LONG_PRESS_MS = 700       # 长按时长; 抖音里长按会弹倍速/菜单

# Report ID (纯鼠标模式下描述符里没有 Report ID, 这几个值不参与)
RID_KEYBOARD = 0x01
RID_MOUSE = 0x02
RID_CONSUMER = 0x03

# HIDP 传输头的 transaction type (高 4 位)
HIDP_NAMES = {
    0x0: "HANDSHAKE", 0x1: "HID_CONTROL", 0x4: "GET_REPORT", 0x5: "SET_REPORT",
    0x6: "GET_PROTOCOL", 0x7: "SET_PROTOCOL", 0x8: "GET_IDLE", 0x9: "SET_IDLE",
    0xA: "DATA", 0xB: "DATC",
}

# HIDP HANDSHAKE 结果码
HS_SUCCESSFUL = 0x00
HS_ERR_INVALID_REPORT_ID = 0x02
HS_ERR_UNSUPPORTED_REQUEST = 0x03

# 键盘 usage code (HID Usage Table, Keyboard/Keypad Page 0x07)
KEY_A = 0x04
KEY_UP = 0x52
KEY_DOWN = 0x51
KEY_PAGEUP = 0x4B
KEY_PAGEDOWN = 0x4E
KEY_SPACE = 0x2C

# F1..F12 是连续的 usage code: F1 = 0x3A ... F12 = 0x45
KEY_F1 = 0x3A
KEY_F = {n: KEY_F1 + n - 1 for n in range(1, 13)}

# 实测: 外接无线键盘上 F5 = 上一个视频, F6 = 暂停/播放, F7 = 下一个视频。
# 这三个键正好是键盘上多媒体键的经典位置, 所以有两种可能: 键盘发的是普通 F 键码,
# 或者发的是 consumer control (Scan Prev / Play-Pause / Scan Next)。两套都保留,
# 键盘这套作为默认。
KEY_PREV_VIDEO = KEY_F[5]
KEY_PLAY_PAUSE = KEY_F[6]
KEY_NEXT_VIDEO = KEY_F[7]

# 多媒体键 (consumer control) 在报告里的 bit 序号, 顺序必须和描述符里 Usage 的
# 声明顺序完全一致。报告是 2 字节小端: bit0..7 在低字节, bit8..15 在高字节。
#
# 实测罗技 K580 无线键盘 (Android 布局的 F 行):
#   F1 = home, F2 = 多任务, F3 = 返回, F5 = 上一个, F6 = 暂停/播放, F7 = 下一个
# Android 本身没有 F1 -> home 这种映射, 但 consumer 里的 AC Home (0x0223) 正是
# Android 标准映射到的 KEYCODE_HOME —— 所以 K580 的 F 行发的是这套 consumer 码,
# 不是普通 F 键码。这也是为什么把 consumer 做成默认方案。
CONSUMER_BITS = {
    "next": 0,      # Scan Next Track     0x00B5  <- K580 的 F7
    "prev": 1,      # Scan Previous Track 0x00B6  <- K580 的 F5
    "stop": 2,      # Stop                0x00B7
    "play": 3,      # Play/Pause          0x00CD  <- K580 的 F6
    "mute": 4,      # Mute                0x00E2
    "vol+": 5,      # Volume Up           0x00E9
    "vol-": 6,      # Volume Down         0x00EA
    "home": 7,      # AC Home             0x0223  <- K580 的 F1
    "back": 8,      # AC Back             0x0224  <- K580 的 F3
    "forward": 9,   # AC Forward          0x0225
    "search": 10,   # AC Search           0x0221
    "tasks": 11,    # AL Task Manager     0x01A2  <- K580 的 F2 (可能)
    "allapps": 12,  # AC Show All Apps    0x029F  <- K580 的 F2 (另一种可能)
    "ff": 13,       # Fast Forward        0x00B3
    "rewind": 14,   # Rewind              0x00B4
    "menu": 15,     # Menu                0x0040
}

# ---------------------------------------------------------------------------
# HID 报告描述符。每行一个 item, 注释是它的含义, 拼起来就是 SDP 属性 0x0206
# 里的那串 hex。
# ---------------------------------------------------------------------------

# 键盘本体: 1 字节修饰键 + 1 字节保留 + 6 字节键码
_KEYBOARD_BODY = [
    ("0507", "  Usage Page (Keyboard/Keypad)"),
    ("19e0", "  Usage Minimum (LeftControl)"),
    ("29e7", "  Usage Maximum (Right GUI)"),
    ("1500", "  Logical Minimum (0)"),
    ("2501", "  Logical Maximum (1)"),
    ("7501", "  Report Size (1)"),
    ("9508", "  Report Count (8)"),
    ("8102", "  Input (Data,Var,Abs)   -> 1 字节修饰键位图"),
    ("9501", "  Report Count (1)"),
    ("7508", "  Report Size (8)"),
    ("8101", "  Input (Const)          -> 1 字节保留位"),
    ("9506", "  Report Count (6)"),
    ("7508", "  Report Size (8)"),
    ("1500", "  Logical Minimum (0)"),
    ("2565", "  Logical Maximum (101)"),
    ("1900", "  Usage Minimum (0)"),
    ("2965", "  Usage Maximum (101)"),
    ("8100", "  Input (Data,Array)     -> 6 字节同时按下的键码"),
]

# 鼠标本体: 按键 3bit + 5bit 补齐 + X/Y/Wheel 各 1 字节相对位移
_MOUSE_BODY = [
    ("0901", "  Usage (Pointer)"),
    ("a100", "  Collection (Physical)"),
    ("0509", "    Usage Page (Button)"),
    ("1901", "    Usage Minimum (Button 1)"),
    ("2903", "    Usage Maximum (Button 3)"),
    ("1500", "    Logical Minimum (0)"),
    ("2501", "    Logical Maximum (1)"),
    ("7501", "    Report Size (1)"),
    ("9503", "    Report Count (3)"),
    ("8102", "    Input (Data,Var,Abs)  -> 左/右/中键 3 bit"),
    ("7505", "    Report Size (5)"),
    ("9501", "    Report Count (1)"),
    ("8101", "    Input (Const)         -> 补齐到 1 字节"),
    ("0501", "    Usage Page (Generic Desktop)"),
    ("0930", "    Usage (X)"),
    ("0931", "    Usage (Y)"),
    ("0938", "    Usage (Wheel)"),
    ("1581", "    Logical Minimum (-127)"),
    ("257f", "    Logical Maximum (127)"),
    ("7508", "    Report Size (8)"),
    ("9503", "    Report Count (3)"),
    ("8106", "    Input (Data,Var,Rel)  -> X / Y / 滚轮, 相对位移"),
    ("c0", "  End Collection"),
]

# 多媒体键本体: 16 个 1bit 开关 = 2 字节。
# 1 字节的 usage 用 09 XX, 2 字节的用 0A XXXX (小端)。
_CONSUMER_BODY = [
    ("1500", "  Logical Minimum (0)"),
    ("2501", "  Logical Maximum (1)"),
    ("7501", "  Report Size (1)"),
    ("9510", "  Report Count (16)"),
    ("09b5", "  Usage (Scan Next Track)      bit0"),
    ("09b6", "  Usage (Scan Previous Track)  bit1"),
    ("09b7", "  Usage (Stop)                 bit2"),
    ("09cd", "  Usage (Play/Pause)           bit3"),
    ("09e2", "  Usage (Mute)                 bit4"),
    ("09e9", "  Usage (Volume Up)            bit5"),
    ("09ea", "  Usage (Volume Down)          bit6"),
    ("0a2302", "  Usage (AC Home)              bit7"),
    ("0a2402", "  Usage (AC Back)              bit8"),
    ("0a2502", "  Usage (AC Forward)           bit9"),
    ("0a2102", "  Usage (AC Search)            bit10"),
    ("0aa201", "  Usage (AL Task Manager)      bit11"),
    ("0a9f02", "  Usage (AC Show All Apps)     bit12"),
    ("09b3", "  Usage (Fast Forward)         bit13"),
    ("09b4", "  Usage (Rewind)               bit14"),
    ("0940", "  Usage (Menu)                 bit15"),
    ("8102", "  Input (Data,Var,Abs)   -> 2 字节开关位图"),
]

# 纯鼠标: 不声明 Report ID —— 经典蓝牙鼠标就是这样, 兼容性最好。
# 报告 = 4 字节 (按键, X, Y, 滚轮), 前面只加一个 HIDP 头。
MOUSE_ONLY_ITEMS = (
    [("0501", "Usage Page (Generic Desktop)"),
     ("0902", "Usage (Mouse)"),
     ("a101", "Collection (Application)")]
    + _MOUSE_BODY
    + [("c0", "End Collection")]
)

# 纯键盘: 最早那套配置, Report ID 1
KEYBOARD_ONLY_ITEMS = (
    [("0501", "Usage Page (Generic Desktop)"),
     ("0906", "Usage (Keyboard)"),
     ("a101", "Collection (Application)"),
     ("8501", "  Report ID (1)")]
    + _KEYBOARD_BODY
    + [("c0", "End Collection")]
)

# 键盘 + 多媒体键 (不带鼠标)。先把键盘这条线走通, 用的就是这个模式:
# CoD 仍然报成纯键盘, 描述符里只多了一个 consumer collection。
KEYBOARD_MEDIA_ITEMS = (
    [("0501", "Usage Page (Generic Desktop)"),
     ("0906", "Usage (Keyboard)"),
     ("a101", "Collection (Application)"),
     ("8501", "  Report ID (1)")]
    + _KEYBOARD_BODY
    + [("c0", "End Collection"),
       ("050c", "Usage Page (Consumer)"),
       ("0901", "Usage (Consumer Control)"),
       ("a101", "Collection (Application)"),
       ("8503", "  Report ID (3)")]
    + _CONSUMER_BODY
    + [("c0", "End Collection")]
)

# 复合: 键盘 + 鼠标 + 多媒体, 用 Report ID 区分
COMPOSITE_ITEMS = (
    [("0501", "Usage Page (Generic Desktop)"),
     ("0906", "Usage (Keyboard)"),
     ("a101", "Collection (Application)"),
     ("8501", "  Report ID (1)")]
    + _KEYBOARD_BODY
    + [("c0", "End Collection"),
       ("0501", "Usage Page (Generic Desktop)"),
       ("0902", "Usage (Mouse)"),
       ("a101", "Collection (Application)"),
       ("8502", "  Report ID (2)")]
    + _MOUSE_BODY
    + [("c0", "End Collection"),
       ("050c", "Usage Page (Consumer)"),
       ("0901", "Usage (Consumer Control)"),
       ("a101", "Collection (Application)"),
       ("8503", "  Report ID (3)")]
    + _CONSUMER_BODY
    + [("c0", "End Collection")]
)

# 模式表。subclass 是 SDP 属性 0x0202, 要和适配器 CoD 的 minor 一致。
MODES = {
    "mouse": {
        "desc": "MOUSE-ONLY (纯鼠标, 无 Report ID, 兼容性最好)",
        "items": MOUSE_ONLY_ITEMS, "subclass": "0x80", "cod": "0x000580",
        "report_ids": False, "has_keyboard": False, "has_mouse": True,
        "has_consumer": False,
    },
    "keyboard": {
        "desc": "KEYBOARD-ONLY (纯键盘, 最早那套配置)",
        "items": KEYBOARD_ONLY_ITEMS, "subclass": "0x40", "cod": "0x000540",
        "report_ids": True, "has_keyboard": True, "has_mouse": False,
        "has_consumer": False,
    },
    "kbdmedia": {
        "desc": "KEYBOARD + MEDIA (键盘 + 多媒体键, 无鼠标; 对标罗技 K580)",
        "items": KEYBOARD_MEDIA_ITEMS, "subclass": "0x40", "cod": "0x000540",
        "report_ids": True, "has_keyboard": True, "has_mouse": False,
        "has_consumer": True,
    },
    "composite": {
        "desc": "COMPOSITE (键盘 + 鼠标 + 多媒体键)",
        "items": COMPOSITE_ITEMS, "subclass": "0xc0", "cod": "0x0005c0",
        "report_ids": True, "has_keyboard": True, "has_mouse": True,
        "has_consumer": True,
    },
}


def build_sdp_record(descriptor_hex, subclass):
    """拼 HID 的 SDP 服务记录。

    0x0202 HIDDeviceSubclass 要和 CoD 的 minor 一致 (0x40 键盘 / 0x80 指针 /
    0xc0 键鼠二合一)。0x020e HIDBootDevice 必须为 false —— 为 true 时主机可能改用
    boot protocol, 那套报告格式是固定的、不带 Report ID。"""
    return f"""
<record>
  <attribute id="0x0001">
    <sequence>
      <uuid value="0x1124" />
    </sequence>
  </attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence>
        <uuid value="0x0100" />
        <uint16 value="0x0011" />
      </sequence>
      <sequence>
        <uuid value="0x0011" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005">
    <sequence>
      <uuid value="0x1002" />
    </sequence>
  </attribute>
  <attribute id="0x0006">
    <sequence>
      <uint16 value="0x656e" />
      <uint16 value="0x006a" />
      <uint16 value="0x0100" />
    </sequence>
  </attribute>
  <attribute id="0x0009">
    <sequence>
      <sequence>
        <uuid value="0x1124" />
        <uint16 value="0x0100" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x000d">
    <sequence>
      <sequence>
        <sequence>
          <uuid value="0x0100" />
          <uint16 value="0x0013" />
        </sequence>
        <sequence>
          <uuid value="0x0011" />
        </sequence>
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0100">
    <text value="Raspberry Pi HID Remote" />
  </attribute>
  <attribute id="0x0201">
    <uint16 value="0x0111" />
  </attribute>
  <attribute id="0x0202">
    <uint8 value="{subclass}" />
  </attribute>
  <attribute id="0x0203">
    <uint8 value="0x00" />
  </attribute>
  <attribute id="0x0204">
    <bool value="true" />
  </attribute>
  <attribute id="0x0205">
    <bool value="true" />
  </attribute>
  <attribute id="0x0206">
    <sequence>
      <sequence>
        <uint8 value="0x22" />
        <text encoding="hex" value="{descriptor_hex}" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0207">
    <sequence>
      <sequence>
        <uint16 value="0x0409" />
        <uint16 value="0x0100" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x020b">
    <uint16 value="0x0100" />
  </attribute>
  <attribute id="0x020c">
    <uint16 value="0x0c80" />
  </attribute>
  <attribute id="0x020d">
    <bool value="true" />
  </attribute>
  <attribute id="0x020e">
    <bool value="false" />
  </attribute>
</record>
"""


def check_bluetoothd_plugin():
    """BlueZ 自带的 input 插件实现的是 HID 主机(Host)角色, 会占用 L2CAP
    PSM 17/19。不禁用它, 本脚本就无法 bind 这两个端口。"""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv = f.read().split(b"\x00")
        except OSError:
            continue
        if not argv or not argv[0].endswith(b"bluetoothd"):
            continue
        args = b" ".join(argv).decode("utf-8", "replace")
        if "-P input" in args or "--noplugin=input" in args:
            return True
        print("[ERROR] bluetoothd is running WITH the built-in 'input' plugin.")
        print("        It owns L2CAP PSM 17/19, so this script cannot listen.")
        print("        Fix: sudo ./switch2hid.sh -> option 1 (adds '-P input'),")
        print("        then run this script again.")
        return False
    print("[WARN] bluetoothd process not found - is the bluetooth service running?")
    return False


class HidTransport:
    """自己持有 HID 的两条 L2CAP 通道, 并负责拼装/发送 HID 报告。

    ProfileManager1.RegisterProfile 对 HID(0x1124) 只负责把 SDP 记录发布出去,
    BlueZ 不会替我们监听 L2CAP 端口 (profile 选项也只能带一个 PSM, 而 HID 设备
    角色需要 control 17 + interrupt 19 两条)。所以端口必须由本进程 bind/listen,
    否则手机做完服务发现后连 PSM 17 会被拒绝, HID 会话根本建立不起来。"""

    def __init__(self, mode):
        self.mode = mode
        self.report_ids = mode["report_ids"]
        self.has_keyboard = mode["has_keyboard"]
        self.has_mouse = mode["has_mouse"]
        self.has_consumer = mode["has_consumer"]
        self.lock = threading.Lock()
        self.ctrl_conn = None
        self.intr_conn = None
        self.fallback_fds = []   # 万一 BlueZ 自己占了端口, 退回用 profile 的 fd
        self.homed = False       # 指针是否已经定位过 (定位一次要一秒多, 别每次都做)

    # ---------- 服务端 ----------
    def start_servers(self):
        try:
            ctrl_srv = self._listen(P_CTRL)
            intr_srv = self._listen(P_INTR)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                print(f"[ERROR] L2CAP port already in use ({e}).")
                print("        Something else still owns PSM 17/19 - most likely the")
                print("        BlueZ 'input' plugin or another copy of this script.")
                print("        Check: sudo pgrep -a bluetoothd ; sudo pgrep -af douyin_remote")
            elif e.errno == errno.EPERM:
                print(f"[ERROR] Permission denied binding L2CAP ({e}). Run with sudo.")
            else:
                print(f"[ERROR] Cannot listen on L2CAP: {e}")
            return False

        for srv, name in ((ctrl_srv, "control"), (intr_srv, "interrupt")):
            threading.Thread(target=self._serve, args=(srv, name),
                             daemon=True).start()
        print(f"[OK] Listening on L2CAP PSM {P_CTRL} (control) and {P_INTR} (interrupt)"
              + (", link encryption required" if REQUIRE_ENCRYPTION else ""))
        return True

    def _listen(self, psm):
        srv = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                            socket.BTPROTO_L2CAP)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass   # L2CAP 不一定支持, 忽略
        if REQUIRE_ENCRYPTION:
            # struct bt_security { __u8 level; __u8 key_size; }
            try:
                srv.setsockopt(SOL_BLUETOOTH, BT_SECURITY,
                               struct.pack("BB", BT_SECURITY_MEDIUM, 0))
            except OSError as e:
                print(f"[WARN] Could not set BT_SECURITY on PSM {psm}: {e}")
        srv.bind((socket.BDADDR_ANY, psm))
        srv.listen(1)
        return srv

    def _serve(self, srv, name):
        """接受连接, 然后一直读到对端断开, 再回到 accept 等下一次连接。"""
        while True:
            try:
                conn, addr = srv.accept()
            except OSError as e:
                print(f"\n[ERROR] accept() on {name} failed: {e}")
                return

            mac = addr[0] if isinstance(addr, tuple) else str(addr)
            with self.lock:
                if name == "control":
                    self.ctrl_conn = conn
                else:
                    self.intr_conn = conn
                both = self.ctrl_conn is not None and self.intr_conn is not None
            print(f"\n[OK] {name} channel connected from {mac}")
            if both:
                print("[OK] Phone connected! Both HID channels are up.")

            try:
                while True:
                    data = conn.recv(128)
                    if not data:
                        break
                    kind = HIDP_NAMES.get(data[0] >> 4, "?")
                    print(f"\n[RX {name}] {data.hex(' ')}  ({kind})")
                    if name == "control":
                        self._handle_control(conn, data)
            except OSError:
                pass
            finally:
                with self.lock:
                    if name == "control":
                        self.ctrl_conn = None
                    else:
                        self.intr_conn = None
                try:
                    conn.close()
                except OSError:
                    pass
                print(f"\n[INFO] {name} channel disconnected ({mac})")

    def _handle_control(self, conn, data):
        """回应 control 通道上的 HIDP 请求。

        主机(手机)发完请求会等一个 HANDSHAKE 或 DATA 回应, 不回的话有的主机
        会认为设备无响应, 从而忽略后续输入报告甚至断开会话。"""
        msg_type = data[0] >> 4
        param = data[0] & 0x0F
        try:
            if msg_type == 0x4:      # GET_REPORT: 回一份空报告
                rtype = param & 0x03
                rid = data[1] if (len(data) > 1 and self.report_ids) else None
                payload = self._blank_report(rid)
                if payload is None:
                    conn.send(bytes([HS_ERR_INVALID_REPORT_ID]))
                    print(f"[TX control] HANDSHAKE invalid report id {rid}")
                else:
                    conn.send(bytes([0xA0 | rtype]) + bytes(payload))
                    print(f"[TX control] DATA blank report (id={rid})")
            elif msg_type == 0x6:    # GET_PROTOCOL: 1 = report protocol
                conn.send(bytes([0xA0, 0x01]))
                print("[TX control] DATA protocol=report")
            elif msg_type == 0x8:    # GET_IDLE
                conn.send(bytes([0xA0, 0x00]))
                print("[TX control] DATA idle=0")
            elif msg_type in (0x5, 0x7, 0x9):   # SET_REPORT / SET_PROTOCOL / SET_IDLE
                conn.send(bytes([HS_SUCCESSFUL]))
                print("[TX control] HANDSHAKE successful")
            elif msg_type == 0x1:    # HID_CONTROL (suspend / virtual cable unplug)
                print(f"[INFO] HID_CONTROL param=0x{param:x} (no response needed)")
            else:
                conn.send(bytes([HS_ERR_UNSUPPORTED_REQUEST]))
                print(f"[TX control] HANDSHAKE unsupported (type=0x{msg_type:x})")
        except OSError as e:
            print(f"[ERROR] control channel reply failed: {e}")

    def _blank_report(self, report_id):
        """GET_REPORT 时回的空报告, payload 长度必须和描述符声明一致。"""
        if not self.report_ids:
            return [0x00] * 4          # 纯鼠标: 按键 + X + Y + 滚轮
        sizes = {RID_KEYBOARD: 8, RID_MOUSE: 4, RID_CONSUMER: 2}
        if report_id not in sizes:
            return None
        return [report_id] + [0x00] * sizes[report_id]

    # ---------- 底层发送 ----------
    def send_report(self, payload):
        """payload 不含 HIDP 头; 前面补 0xA1 = (DATA << 4) | INPUT"""
        pkt = bytes([0xA1]) + bytes(payload)
        with self.lock:
            conn = self.intr_conn or self.ctrl_conn
            fd = self.fallback_fds[-1] if (conn is None and self.fallback_fds) else None
        if conn is not None:
            conn.send(pkt)
            return True
        if fd is not None:
            os.write(fd, pkt)
            return True
        return False

    def is_connected(self):
        with self.lock:
            return (self.intr_conn is not None or self.ctrl_conn is not None
                    or bool(self.fallback_fds))

    # ---------- 鼠标 ----------
    def _mouse(self, buttons=0, dx=0, dy=0, wheel=0):
        body = [buttons & 0x07, dx & 0xFF, dy & 0xFF, wheel & 0xFF]
        if self.report_ids:
            return self.send_report([RID_MOUSE] + body)
        return self.send_report(body)

    def _move(self, dx, dy, buttons=0, step=SWIPE_STEP, delay=0.008):
        """相对位移一次最多 ±127, 所以要拆成多个报告连续发。"""
        while dx or dy:
            sx = max(-step, min(step, dx))
            sy = max(-step, min(step, dy))
            dx -= sx
            dy -= sy
            self._mouse(buttons, sx, sy)
            time.sleep(delay)

    def _home_pointer(self, x_frac=SWIPE_START_X_FRAC, y_frac=SWIPE_START_Y_FRAC):
        """相对鼠标不知道指针在哪, 所以先往左上角猛推, 把指针顶到 (0,0) —— 指针
        会被屏幕边界夹住, 推多了没有副作用 —— 然后再挪到目标位置。

        第二步必须【小步慢移】: 系统的指针加速只放大快速移动, 大步长会被放大到
        冲过目标、被夹在屏幕底部。而起点贴着底部边缘再向上拖, iOS 会判定成
        "上滑回主屏"手势 (视频缩成小窗), 应用收不到这个事件。"""
        for _ in range(max(SCREEN_W, SCREEN_H) // 100 + 4):
            self._mouse(0, -120, -120)
            time.sleep(0.004)
        self._move(int(SCREEN_W * x_frac), int(SCREEN_H * y_frac),
                   step=POINTER_STEP, delay=POINTER_DELAY)
        self.homed = True

    def nudge(self, dx, dy):
        """手动微调指针位置。自动定位靠的是估算的屏幕尺寸和指针加速, 不一定准,
        看着屏幕上的指针手动挪几下最可靠。"""
        if not (self._require_mouse() and self._require_link()):
            return
        try:
            self._move(dx, dy, step=POINTER_STEP, delay=POINTER_DELAY)
            print(f"[OK] Pointer nudged ({dx:+d}, {dy:+d})")
        except OSError as e:
            print(f"\n[ERROR] Nudge failed: {e}")

    def wheel(self, notches, label=None):
        """滚轮。实测真实蓝牙鼠标的滚轮可以刷抖音, 所以这是首选方案:
        Android 把滚轮事件作为 ACTION_SCROLL 派发给指针下方的 View,
        RecyclerView/ViewPager2 是处理这个事件的。"""
        if not (self._require_mouse() and self._require_link()):
            return
        try:
            unit = 1 if notches > 0 else -1
            for _ in range(abs(notches)):
                self._mouse(wheel=unit)     # 一格滚轮 = 一个 ±1 的报告
                time.sleep(0.04)
            print(f"[OK] {label or 'Wheel'} {notches:+d} notch sent")
        except OSError as e:
            print(f"\n[ERROR] Wheel failed: {e}")

    def swipe(self, direction, label):
        """direction = -1 向上滑(下一个视频), +1 向下滑。

        Android 把鼠标左键的按下/移动/抬起按 ACTION_DOWN/MOVE/UP 派发给 View,
        普通 View 的 onTouchEvent 照常处理, 所以一次鼠标拖拽等价于一次滑动。"""
        if not (self._require_mouse() and self._require_link()):
            return
        try:
            if not self.homed:
                self._home_pointer()
            self._mouse(buttons=0x01)                 # 按下左键
            time.sleep(0.03)
            self._move(0, direction * SWIPE_DIST, buttons=0x01)
            time.sleep(0.02)
            self._mouse(buttons=0x00)                 # 抬起
            # 松手后把指针挪回起点: 连续滑动会把指针一路推到屏幕边缘, 而贴边起手
            # 会被系统当成边缘手势 (iOS 上滑回主屏就是这么触发的)
            self._move(0, -direction * SWIPE_DIST,
                       step=POINTER_STEP, delay=POINTER_DELAY)
            print(f"[OK] {label} (mouse drag) sent")
        except OSError as e:
            print(f"\n[ERROR] Swipe failed: {e}")

    def click(self, button=BTN_LEFT, count=1, label=None):
        """点击 = 按下再抬起, 手机侧就是一次轻触 (tap)。
        抖音里: 单击 = 暂停/播放, 双击 = 点赞。"""
        if not (self._require_mouse() and self._require_link()):
            return
        try:
            if not self.homed:
                self._home_pointer()
            for i in range(count):
                self._mouse(buttons=button)
                time.sleep(0.04)
                self._mouse(buttons=0x00)
                if i + 1 < count:
                    time.sleep(DOUBLE_CLICK_GAP)
            print(f"[OK] {label or 'Click'} sent (button=0x{button:02X}, x{count})")
        except OSError as e:
            print(f"\n[ERROR] Click failed: {e}")

    def long_press(self, ms=LONG_PRESS_MS, button=BTN_LEFT):
        """长按: 按住不放一段时间再抬起。抖音里会弹出倍速/菜单。"""
        if not (self._require_mouse() and self._require_link()):
            return
        try:
            if not self.homed:
                self._home_pointer()
            self._mouse(buttons=button)
            time.sleep(ms / 1000.0)
            self._mouse(buttons=0x00)
            print(f"[OK] Long press {ms}ms sent")
        except OSError as e:
            print(f"\n[ERROR] Long press failed: {e}")

    def center_pointer(self):
        """把指针挪到屏幕中间。滚轮事件是派发给指针下方那个 View 的,
        指针要是停在状态栏或某个控件上, 滚轮就滚不到视频列表。"""
        if not (self._require_mouse() and self._require_link()):
            return
        try:
            self._home_pointer()
            print(f"[OK] Pointer moved to about ({SWIPE_START_X_FRAC:.0%}, "
                  f"{SWIPE_START_Y_FRAC:.0%}) of the screen - 屏幕上确认一下位置, "
                  f"不准就用 I/K/J/L 微调")
        except OSError as e:
            print(f"\n[ERROR] Centering pointer failed: {e}")

    # ---------- 键盘 / 多媒体 ----------
    def send_key(self, keycode, modifier=0x00, label=None):
        if not (self._require_keyboard() and self._require_link()):
            return
        # payload 必须是 8 字节: 修饰键 + 保留 + 6 个键码槽, 和描述符里
        # "Report Count (6) / Report Size (8)" 对应, 少一个字节主机会解析错位
        press = [RID_KEYBOARD, modifier, 0x00, keycode, 0, 0, 0, 0, 0]
        release = [RID_KEYBOARD, 0x00, 0x00, 0x00, 0, 0, 0, 0, 0]
        try:
            self.send_report(press)
            time.sleep(0.05)
            self.send_report(release)
            print(f"[OK] Key 0x{keycode:02X}"
                  + (f" ({label})" if label else "") + " sent")
        except OSError as e:
            print(f"\n[ERROR] Send key failed: {e}")

    def send_consumer(self, name):
        if not self.has_consumer:
            print("[SKIP] 当前模式没有多媒体键, 用 --composite 启动")
            return
        if not self._require_link():
            return
        bit = CONSUMER_BITS.get(name)
        if bit is None:
            print(f"[SKIP] 未定义的多媒体键: {name}")
            return
        mask = 1 << bit
        try:
            self.send_report([RID_CONSUMER, mask & 0xFF, (mask >> 8) & 0xFF])
            time.sleep(0.05)
            self.send_report([RID_CONSUMER, 0x00, 0x00])
            print(f"[OK] Consumer key '{name}' (bit{bit}) sent")
        except OSError as e:
            print(f"\n[ERROR] Send consumer key failed: {e}")

    # ---------- 诊断 ----------
    def type_test(self):
        """连打 a b c。在手机的任意输入框里试: 打得出字, 说明键盘通道确实通了;
        打不出, 说明手机压根没采纳我们的报告。"""
        if not self._require_keyboard():
            return
        print("[..] typing 'abc' - 请在手机上先点开一个输入框")
        for offset in range(3):
            self.send_key(KEY_A + offset)
            time.sleep(0.15)

    def pointer_test(self):
        """只移动指针, 不点击。手机屏幕上应该能看到鼠标指针在动;
        看不到指针, 说明手机没把我们识别成鼠标。"""
        if not (self._require_mouse() and self._require_link()):
            return
        try:
            for _ in range(12):
                self._mouse(0, 25, 25)
                time.sleep(0.03)
            print("[OK] Pointer nudged toward bottom-right - 手机上看到指针了吗?")
        except OSError as e:
            print(f"\n[ERROR] Pointer test failed: {e}")

    # ---------- 前置检查 ----------
    def _require_link(self):
        if self.is_connected():
            return True
        print("\n[WARN] Phone not connected yet!")
        return False

    def _require_mouse(self):
        if self.has_mouse:
            return True
        print("[SKIP] 当前模式没有鼠标, 用 --mouse-only 或 --composite 启动")
        return False

    def _require_keyboard(self):
        if self.has_keyboard:
            return True
        print("[SKIP] 当前模式没有键盘, 用 --keyboard-only 或 --composite 启动")
        return False


class Profile(dbus.service.Object):
    """只用来承载 SDP 记录。正常情况下 BlueZ 不会回调 NewConnection
    (端口是本进程在监听), 但万一某个 BlueZ 版本自己监听了, 这里把 fd 收下
    作为兜底路径。"""

    def __init__(self, bus, path, transport):
        super().__init__(bus, path)
        self.transport = transport

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        print("[INFO] Profile Released")

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, path, fd, properties):
        new_fd = fd.take()
        self.transport.fallback_fds.append(new_fd)
        print(f"\n[OK] Profile1.NewConnection, FD: {new_fd} "
              f"(channels: {len(self.transport.fallback_fds)})")

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, path):
        for old_fd in self.transport.fallback_fds:
            try:
                os.close(old_fd)
            except OSError:
                pass
        self.transport.fallback_fds = []
        print("[INFO] Profile1.RequestDisconnection")


def watch_device_events(bus):
    """监听 BlueZ 设备属性变化, 顺便在配对完成后自动把手机设为 Trusted,
    免得每次重连都要重新授权。"""
    def on_props_changed(interface, changed, invalidated, path=None):
        if interface != "org.bluez.Device1":
            return
        fields = [f"{k}={bool(changed[k])}"
                  for k in ("Paired", "Connected", "ServicesResolved")
                  if k in changed]
        if not fields:
            return
        mac = path.split("/")[-1].replace("dev_", "").replace("_", ":")
        print(f"\n[BT] {mac} {' '.join(fields)}")

        if changed.get("Paired"):
            try:
                props = dbus.Interface(bus.get_object("org.bluez", path),
                                       "org.freedesktop.DBus.Properties")
                if not props.Get("org.bluez.Device1", "Trusted"):
                    props.Set("org.bluez.Device1", "Trusted", True)
                    print(f"[OK] {mac} marked as Trusted")
            except dbus.DBusException as e:
                print(f"[WARN] Could not set Trusted on {mac}: {e}")

    bus.add_signal_receiver(
        on_props_changed,
        dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged",
        arg0="org.bluez.Device1",
        path_keyword="path",
    )


# 按键循环会把终端切进 cbreak (逐字符读取), 而输入配对码需要规范模式才有
# 退格/行编辑。这两个全局量负责让两者错开: 提示配对码时先置位 PROMPTING,
# 按键循环看到就停手, 然后临时切回 TERM_ORIG 保存的规范模式读一行。
TERM_ORIG = None
PROMPTING = threading.Event()


def prompt_line(prompt):
    """在按键循环运行期间安全地读一行 (退格、行编辑都正常)。"""
    fd = sys.stdin.fileno()
    PROMPTING.set()
    time.sleep(0.3)          # 等按键循环的 select 超时退出, 免得两边抢同一个 fd
    try:
        if TERM_ORIG is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, TERM_ORIG)
            try:
                termios.tcflush(fd, termios.TCIFLUSH)   # 丢掉切换期间的杂字符
            except termios.error:
                pass
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    finally:
        if TERM_ORIG is not None:
            tty.setcbreak(fd)
        PROMPTING.clear()


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class Agent(dbus.service.Object):
    """自带的配对代理 (org.bluez.Agent1)。

    为什么不用外部工具: bt-agent 不支持 KeyboardDisplay; 而把命令用管道喂给
    bluetoothctl 会让它的 stdin 不是 TTY, readline 行编辑失效 —— 退格会变成字面量
    ^H 混进配对码里, 配对必然失败。自己实现代理就没有这些问题, 而且只需一个终端。"""

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        print("\n[PAIR] Agent released")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        print(f"\n[PAIR] {device} 要求输入配对码")
        while True:
            text = prompt_line("请输入手机屏幕上显示的 6 位配对码 (可退格; 直接回车取消): ")
            if not text:
                print("[PAIR] 已取消")
                raise Rejected("cancelled by user")
            if text.isdigit():
                return dbus.UInt32(int(text))
            print("[WARN] 只能是数字, 请重新输入")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        print(f"\n[PAIR] {device} 要求输入 PIN 码")
        text = prompt_line("请输入 PIN 码 (可退格; 直接回车取消): ")
        if not text:
            raise Rejected("cancelled by user")
        return text

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        print(f"\n[PAIR] 请在手机上输入这个配对码: {int(passkey):06d}"
              f"   (手机已输入 {int(entered)} 位)")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        print(f"\n[PAIR] 请在手机上输入这个 PIN 码: {pincode}")

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"\n[PAIR] {device} 的配对码是 {int(passkey):06d}")
        ans = prompt_line("和手机上显示的一致吗? [Y/n]: ").lower()
        if ans in ("", "y", "yes"):
            print("[PAIR] 已确认")
            return
        raise Rejected("rejected by user")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        print(f"\n[PAIR] 允许 {device} 配对")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print(f"\n[PAIR] 允许 {device} 使用服务 {uuid}")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        print("\n[PAIR] 对方取消了配对")


AGENT_PATH = "/org/bluez/agent_douyin_remote"
AGENT_CAPS = ("KeyboardDisplay", "KeyboardOnly", "DisplayYesNo",
              "DisplayOnly", "NoInputNoOutput")


def register_agent(bus, capability):
    """注册自带代理并设为默认代理。

    同一时间只能有一个默认代理, 所以用这个的时候别再跑 bt-agent / bluetoothctl 的代理。"""
    try:
        Agent(bus, AGENT_PATH)
        mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                             "org.bluez.AgentManager1")
        mgr.RegisterAgent(AGENT_PATH, capability)
        mgr.RequestDefaultAgent(AGENT_PATH)
        print(f"[OK] 自带配对代理已注册并设为默认 (capability = {capability})")
        print("     配对码提示会出现在本终端, 退格可用。别再另开 bt-agent。")
        return True
    except dbus.DBusException as e:
        print(f"[ERROR] 注册配对代理失败: {e}")
        print("        通常是已经有别的默认代理了, 先执行: sudo pkill -f bt-agent")
        return False


def build_strategies(transport):
    """把「下一个/上一个/暂停」这三个主操作绑到具体实现上。

    平台差异摆在这里: Android 认多媒体键 (实测罗技 K580 的 F 行有效), iOS 的抖音
    不接媒体命令、只能靠鼠标注入真实触摸。复合模式下两套都在描述符里, 所以运行时
    用 'o' 键切换即可 —— 同一次配对, 接哪台手机就用哪套, 不用重启也不用重配。"""
    out = []
    if transport.has_consumer:
        out.append({
            "name": "多媒体键 (Android 首选)",
            "next": lambda: transport.send_consumer("next"),
            "prev": lambda: transport.send_consumer("prev"),
            "play": lambda: transport.send_consumer("play"),
        })
    if transport.has_mouse:
        # 抖音里单击就是暂停/播放, 所以鼠标方案的 play 用单击实现
        out.append({
            "name": "鼠标滚轮",
            "next": lambda: transport.wheel(-1, "Next video"),
            "prev": lambda: transport.wheel(+1, "Previous video"),
            "play": lambda: transport.click(label="Tap (play/pause)"),
        })
        out.append({
            "name": "鼠标拖拽 (iOS 首选)",
            "next": lambda: transport.swipe(-1, "Next video"),
            "prev": lambda: transport.swipe(+1, "Previous video"),
            "play": lambda: transport.click(label="Tap (play/pause)"),
        })
    if transport.has_keyboard:
        out.append({
            "name": "键盘 F 键",
            "next": lambda: transport.send_key(KEY_NEXT_VIDEO, label="F7"),
            "prev": lambda: transport.send_key(KEY_PREV_VIDEO, label="F5"),
            "play": lambda: transport.send_key(KEY_PLAY_PAUSE, label="F6"),
        })
    return out


def print_help(transport, strategies, idx):
    mouse = transport.has_mouse
    keyboard = transport.has_keyboard
    consumer = transport.has_consumer
    print("\n--- Controls ---")
    print(f" 【当前主操作方案】{strategies[idx]['name']}"
          f"   (共 {len(strategies)} 套, 按 'o' 循环切换)")
    print(" Enter / 'n' -> 下一个视频")
    print(" 'p'         -> 上一个视频")
    print(" space       -> 暂停 / 播放")
    print(" 'o'         -> 切换主操作方案 (接 Android 还是 iPhone 就切到对应那套)")
    if consumer:
        print(" --- 多媒体键 (Android 认这套; iOS 只有 Home 有效) ---")
        print(" 'H'         -> Home        (AC Home,  K580 的 F1)")
        print(" 'B'         -> 返回        (AC Back,  K580 的 F3)")
        print(" 'R' / 'A'   -> 多任务      (AL Task Manager / AC Show All Apps)")
    if mouse:
        print(" --- 鼠标 (iOS 需先开 设置->辅助功能->触控->辅助触控) ---")
        print(" 'w' / 's'   -> 滚轮 上/下 3 格")
        print(" 'u' / 'd'   -> 鼠标拖拽 上滑/下滑")
        print(" '.'         -> 单击 (轻触; 抖音里 = 暂停/播放)")
        print(" ','         -> 双击 (抖音里 = 点赞)")
        print(" ';'         -> 右键 (Android 上相当于返回)")
        print(" '/'         -> 长按 700ms (抖音里 = 倍速/菜单)")
        print(" 'c'         -> 指针定位到屏幕 50%/45% 处 (拖拽/滚轮不灵先按这个)")
        print(" I / K / J / L -> 指针微调 上/下/左/右 (大写; 看着屏幕上的指针挪)")
        print("                起点贴着屏幕底部再向上拖, 会被 iOS 判定成")
        print("                '上滑回主屏'手势 (视频缩成小窗), 应用收不到事件")
    if keyboard:
        print(" --- 键盘 (实测抖音不理方向键/翻页键) ---")
        print(" '1'..'9' '0' '-' '='  -> F1..F12")
        print(" 'k' / 'j'   -> 方向键 上/下")
        print(" 'b' / 'f'   -> PageUp / PageDown")
    print(" --- 诊断用 ---")
    if mouse:
        print(" 'x'         -> 只移动鼠标指针 (手机上应该能看到指针在动)")
    if keyboard:
        print(" 't'         -> 连打 'abc' (在手机输入框里验证键盘通道)")
    print(" 'h'         -> 显示本帮助")
    print(" 'q'         -> 退出")
    print("----------------")


def input_loop(transport):
    """终端按键读取。

    注意: 不能像早先那样每次轮询才临时切一下 raw 模式 —— 两次轮询之间终端处于
    规范模式, 按键会被行缓冲并回显, 读到的往往是回车而不是你按的那个字母
    (表现为按 'k' 却触发了 Enter 对应的动作)。这里进循环时一次性切 cbreak,
    并用 os.read 绕过 sys.stdin 的 Python 层缓冲 (它会预读, 和 select 混用会丢键)。"""
    strategies = build_strategies(transport)
    state = {"idx": 0}
    print_help(transport, strategies, state["idx"])

    def run(action):
        fn = strategies[state["idx"]].get(action)
        if fn is None:
            print(f"[SKIP] 当前方案「{strategies[state['idx']]['name']}」没有这个动作")
            return
        fn()

    def cycle():
        state["idx"] = (state["idx"] + 1) % len(strategies)
        print(f"[OK] 主操作方案已切换为: {strategies[state['idx']]['name']}")

    actions = {
        'o': cycle,
        # 多媒体键里的系统导航键: home/返回 一按就能看出通路通不通, 比盯视频可靠
        'H': lambda: transport.send_consumer("home"),
        'B': lambda: transport.send_consumer("back"),
        'R': lambda: transport.send_consumer("tasks"),
        'A': lambda: transport.send_consumer("allapps"),
        # 鼠标
        'c': transport.center_pointer,
        'I': lambda: transport.nudge(0, -NUDGE_STEP),
        'K': lambda: transport.nudge(0, +NUDGE_STEP),
        'J': lambda: transport.nudge(-NUDGE_STEP, 0),
        'L': lambda: transport.nudge(+NUDGE_STEP, 0),
        'w': lambda: transport.wheel(+3),
        's': lambda: transport.wheel(-3),
        'u': lambda: transport.swipe(-1, "Swipe up"),
        'd': lambda: transport.swipe(+1, "Swipe down"),
        # 点击: 手机侧就是轻触。抖音单击=暂停/播放, 双击=点赞, 长按=倍速菜单
        '.': lambda: transport.click(label="Tap"),
        ',': lambda: transport.click(count=2, label="Double tap (like)"),
        ';': lambda: transport.click(button=BTN_RIGHT, label="Right click"),
        '/': transport.long_press,
        'x': transport.pointer_test,
        # 键盘
        'k': lambda: transport.send_key(KEY_UP),
        'j': lambda: transport.send_key(KEY_DOWN),
        'b': lambda: transport.send_key(KEY_PAGEUP),
        'f': lambda: transport.send_key(KEY_PAGEDOWN),
        't': transport.type_test,
        'h': lambda: print_help(transport, strategies, state["idx"]),
    }
    # 数字键逐个探测 F1..F12 (普通 F 键码那套假设)
    for idx, ch in enumerate("1234567890-=", start=1):
        actions[ch] = (lambda n=idx: transport.send_key(KEY_F[n], label=f"F{n}"))

    global TERM_ORIG
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    TERM_ORIG = old_settings      # 输入配对码时要用它临时切回规范模式
    try:
        tty.setcbreak(fd)
        while True:
            if PROMPTING.is_set():
                # 配对码输入正在独占终端, 不能跟它抢同一个 fd
                time.sleep(0.1)
                continue
            rlist, _, _ = select.select([fd], [], [], 0.2)
            if not rlist:
                continue
            raw = os.read(fd, 1)
            if not raw:
                break
            ch = raw.decode("utf-8", "ignore")
            if ch in ('\r', '\n', 'n'):
                print("\n[CMD] Next video...")
                run("next")
            elif ch == 'p':
                print("\n[CMD] Previous video...")
                run("prev")
            elif ch == ' ':
                print("\n[CMD] Play / Pause...")
                run("play")
            elif ch == 'q':
                print("\n[INFO] Exiting program...")
                break
            elif ch in actions:
                print(f"\n[CMD] '{ch}'...")
                actions[ch]()
    except Exception as e:
        print(f"\n[ERROR] input loop: {e}")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        os._exit(0)


def pick_agent_cap(argv):
    """--agent 打开自带配对代理; --agent=KeyboardOnly 之类可以指定 capability。
    默认不开, 免得和 switch2hid.sh 起的后台 bt-agent 抢默认代理。"""
    for a in argv[1:]:
        if a == "--agent":
            return "KeyboardDisplay"
        if a.startswith("--agent="):
            cap = a.split("=", 1)[1]
            if cap not in AGENT_CAPS:
                print(f"[WARN] 未知 capability {cap}, 可选: {', '.join(AGENT_CAPS)}")
                return "KeyboardDisplay"
            return cap
    return None


def pick_mode(argv):
    if "--composite" in argv:
        return MODES["composite"]
    if "--keyboard-media" in argv:
        return MODES["kbdmedia"]
    if "--keyboard-only" in argv:
        return MODES["keyboard"]
    return MODES["mouse"]


def main():
    mode = pick_mode(sys.argv)
    descriptor = "".join(item for item, _comment in mode["items"])

    print(f"[INFO] Mode: {mode['desc']}")
    print(f"[INFO] HID descriptor: {len(descriptor) // 2} bytes, "
          f"适配器 CoD 应为 {mode['cod']}")
    if mode["has_mouse"]:
        # iOS 默认不启用外接指针设备, 不开这个开关鼠标报告发过去也没有任何反应
        print("[INFO] iPhone/iPad 用鼠标需要先开: 设置 -> 辅助功能 -> 触控")
        print("       -> 辅助触控(AssistiveTouch) 打开; Android 不需要。")
    if mode["has_consumer"]:
        # 实测 iOS: AC Home 有效(系统级), 但 Scan Next/Prev、Play/Pause 无效 ——
        # 那几个是应用级媒体命令, iOS 路由给 now-playing 会话, 抖音 iOS 版没接
        print("[INFO] 实测 iOS 上只有 'H'(AC Home) 有效, 媒体键无效 (抖音 iOS 版")
        print("       没有注册 remote command)。iOS 刷视频请用鼠标那套方案。")
    if mode["has_consumer"] and mode["has_mouse"]:
        print("[INFO] 本模式 Android/iOS 共用: 按 'o' 切换主操作方案")
        print("       (Android -> 多媒体键, iOS -> 鼠标拖拽)")

    if not check_bluetoothd_plugin():
        return

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    watch_device_events(bus)

    agent_cap = pick_agent_cap(sys.argv)
    if agent_cap:
        register_agent(bus, agent_cap)
    else:
        print("[INFO] 未启用自带配对代理 (需要输配对码的设备如 iPhone 请加 --agent)")

    transport = HidTransport(mode)
    # 先占住端口, 再发布 SDP 记录, 免得手机在端口还没就绪时就来连
    if not transport.start_servers():
        return

    profile_path = "/org/bluez/hci0/profile_hid_remote"
    profile = Profile(bus, profile_path, transport)

    profile_mgr = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.ProfileManager1"
    )

    opts = {
        "ServiceRecord": build_sdp_record(descriptor, mode["subclass"]),
        "Role": "server",
        "RequireAuthentication": True,
        "RequireAuthorization": False,
        "AutoConnect": True,
    }

    try:
        profile_mgr.RegisterProfile(profile_path,
                                    "00001124-0000-1000-8000-00805f9b34fb", opts)
        print("[OK] HID SDP record registered successfully!")
        print("[INFO] Waiting for the phone to connect (start from the phone side)...")
    except Exception as e:
        print(f"[ERROR] Failed to Register Profile: {e}")
        return

    threading.Thread(target=input_loop, args=(transport,), daemon=True).start()

    loop = GLib.MainLoop()
    loop.run()


if __name__ == "__main__":
    main()
