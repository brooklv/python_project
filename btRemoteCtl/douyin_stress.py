#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HID 遥控的自动化老化/压力测试。

复用 douyin_remote.py 的 HID 传输层 (import 进来, 不改动它)。

前提: 系统侧准备已经由 switch2hid.sh 做完 (禁用 input 插件、设 CoD/Device ID、
起配对代理、手机已配对)。本脚本不重复那些工作, 而是:

  1. 自动从适配器 CoD 反推该用哪个描述符模式 —— switch2hid.sh 设的就是它,
     两边必须一致, 否则手机看到的"我是什么设备"和"报告长什么样"是矛盾的
  2. 起 L2CAP 监听 + 注册 SDP 记录, 等手机连上来
     (要求【两条通道都建立】才开始, 没连上就不做无意义的发送)
  3. 自动识别连上来的是 Android 还是 iOS, 据此自动选择刷视频的按键方案:
       Android -> 多媒体键 (Scan Next/Previous Track), 实测有效
       iOS     -> 鼠标拖拽 (真实触摸事件); iOS 的抖音不接媒体键
  4. 按指定间隔循环发送, 统计成功/失败/断连; 掉线自动等重连并计数
  5. Ctrl+C 或到达次数/时长上限后打印总结报告

注意: HID 的 L2CAP 端口 (17/19) 同一时间只能被一个进程占用, 所以跑本脚本前
要先停掉 douyin_remote.py —— 脚本会自己检查并提示。

用法示例:
  # 全自动: 模式看 CoD, 方案看设备类型, 每 3 秒一次, 跑到 Ctrl+C
  sudo PYTHONIOENCODING=utf-8 python3 douyin_stress.py

  # 每 1.5 秒一次, 共 2000 次, 明细写 CSV
  sudo PYTHONIOENCODING=utf-8 python3 douyin_stress.py \\
       --interval 1.5 --count 2000 --log /tmp/aging.csv

  # 上下交替, 跑 8 小时, 间隔带 ±20% 抖动
  sudo PYTHONIOENCODING=utf-8 python3 douyin_stress.py \\
       --direction alternate --duration 8h --jitter 0.2

  # 手动指定 (自动识别不准时)
  sudo PYTHONIOENCODING=utf-8 python3 douyin_stress.py \\
       --mode composite --platform ios --plan drag
"""

import argparse
import contextlib
import io
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dbus                                  # noqa: E402
import dbus.mainloop.glib                    # noqa: E402
from gi.repository import GLib               # noqa: E402

import douyin_remote as dr                   # noqa: E402  复用传输层, 不修改


MODE_KEYS = ("auto",) + tuple(dr.MODES)      # auto / mouse / keyboard / kbdmedia / composite

# --plan 关键字 -> build_strategies() 返回的方案名里包含的字样
PLAN_KEYWORDS = {
    "consumer": "多媒体键",
    "wheel": "滚轮",
    "drag": "拖拽",
    "key": "F 键",
}

# 各平台默认按哪套方案刷视频, 按优先级排列 (取第一个当前模式支持的)
PLATFORM_PLANS = {
    # Android: 多媒体键实测有效 (对标罗技 K580 的 F 行); 没有 consumer 就退滚轮
    "android": ("consumer", "wheel", "drag", "key"),
    # iOS: 抖音 iOS 版不接媒体键, 只有鼠标注入的真实触摸有用; 拖拽比滚轮更确定
    "ios": ("drag", "wheel", "consumer", "key"),
}

# 蓝牙 SIG 分配的厂商 ID: 76 = Apple, Inc.
APPLE_BT_VENDOR = 0x004C
# 几个 Apple 专有服务 UUID 的前缀 (ANCS / AMS / Continuity 等), 弱特征
APPLE_UUID_PREFIXES = ("9fa480e0", "7905f431", "89d3502b", "d0611e78")


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

def parse_duration(text):
    """支持 90 / 90s / 15m / 8h 这几种写法, 统一返回秒。"""
    text = str(text).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def build_args():
    ap = argparse.ArgumentParser(
        description="蓝牙 HID 遥控的老化/压力测试 (复用 douyin_remote.py)")

    ap.add_argument("--mode", choices=MODE_KEYS, default="auto",
                    help="HID 模式; auto = 从适配器 CoD 反推 (默认 auto)")
    ap.add_argument("--platform", choices=("auto", "android", "ios"), default="auto",
                    help="手机平台; auto = 连上后从 BlueZ 设备属性识别 (默认 auto)")
    ap.add_argument("--plan", choices=("auto",) + tuple(PLAN_KEYWORDS), default="auto",
                    help="用哪套动作发按键; auto = 按平台自动选 (默认 auto)")
    ap.add_argument("--direction", choices=("next", "prev", "alternate"), default="next",
                    help="发下一个 / 上一个 / 上下交替 (默认 next)")

    ap.add_argument("--interval", type=float, default=3.0,
                    help="两次按键之间的间隔秒数 (默认 3.0)")
    ap.add_argument("--jitter", type=float, default=0.0,
                    help="间隔的随机抖动比例 0~1, 例如 0.2 表示 ±20%% (默认 0)")
    ap.add_argument("--count", type=int, default=0,
                    help="总共发多少次, 0 = 不限 (默认 0)")
    ap.add_argument("--duration", default="0",
                    help="总时长, 支持 90s/15m/8h, 0 = 不限 (默认 0)")

    ap.add_argument("--agent", nargs="?", const="KeyboardDisplay", default=None,
                    metavar="CAP",
                    help="额外注册自带配对代理; 一般不需要 (switch2hid.sh 已起了代理)")
    ap.add_argument("--wait", type=float, default=120.0,
                    help="等待手机连接/重连的超时秒数 (默认 120)")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="连上后先等几秒再开始发送 (默认 3)")
    ap.add_argument("--no-center", action="store_true",
                    help="鼠标方案默认会先把指针定位到屏幕中上部, 加这个跳过")

    ap.add_argument("--report-every", type=int, default=10,
                    help="每多少次打印一行统计 (默认 10)")
    ap.add_argument("--log", metavar="FILE",
                    help="把每一次的结果追加写入 CSV 文件")
    ap.add_argument("--verbose", action="store_true",
                    help="显示每次发送的底层输出 (默认只在出错时显示)")
    ap.add_argument("--force", action="store_true",
                    help="即使检测到 douyin_remote.py 在跑也继续 (会抢不到端口)")
    return ap.parse_args()


# ---------------------------------------------------------------------------
# 自动识别: 模式 (看 CoD) 与平台 (看 BlueZ 设备属性)
# ---------------------------------------------------------------------------

def read_adapter_cod():
    """读适配器当前的 CoD。hciconfig 短格式不含 Class, 必须用 -a。"""
    for cmd, pattern in ((["hciconfig", "-a", "hci0"], r"Class:\s*(0x[0-9a-fA-F]{6})"),
                         (["bluetoothctl", "show"], r"Class:\s*(0x[0-9a-fA-F]{6})")):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(pattern, out)
        if m:
            return m.group(1).lower()
    return None


def detect_mode(requested):
    """把适配器 CoD 反推成描述符模式。CoD 是 switch2hid.sh 设的, 两边必须一致。"""
    if requested != "auto":
        return dr.MODES[requested], f"命令行指定 --mode {requested}"

    cod = read_adapter_cod()
    if cod is None:
        print("[WARN] 读不到适配器 CoD, 回退到 mouse 模式; 不对请用 --mode 指定")
        return dr.MODES["mouse"], "读不到 CoD, 回退默认"

    # 0x000540 被 keyboard 和 kbdmedia 共用, 取功能是超集的 kbdmedia
    preference = ("kbdmedia", "composite", "mouse", "keyboard")
    for key in preference:
        if dr.MODES[key]["cod"].lower() == cod:
            note = f"适配器 CoD {cod} -> {key}"
            if cod == "0x000540":
                note += " (0x000540 由 keyboard/kbdmedia 共用, 取超集的 kbdmedia;"
                note += " 若配对时用的是 --keyboard-only 请加 --mode keyboard)"
            return dr.MODES[key], note

    print(f"[WARN] CoD {cod} 不对应任何模式, 回退到 mouse; 不对请用 --mode 指定")
    return dr.MODES["mouse"], f"CoD {cod} 无匹配, 回退默认"


def find_connected_device(bus):
    """从 BlueZ 找一个 Connected=True 的设备, 返回 (路径, 属性字典)。"""
    try:
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
    except dbus.DBusException as e:
        print(f"[WARN] 读取 BlueZ 设备列表失败: {e}")
        return None, None
    for path, ifaces in objects.items():
        props = ifaces.get("org.bluez.Device1")
        if props and bool(props.get("Connected", False)):
            return str(path), props
    return None, None


def detect_platform(props):
    """判断对端是 iOS 还是 Android, 返回 (平台, 依据)。

    优先级: Modalias 厂商 ID (最可靠, 来自对端的 PnP 记录) > 设备名关键字 >
    Apple 专有服务 UUID。都不命中就按 Android 处理 —— 注意这只是启发式,
    识别不准时用 --platform 手动指定。
    (顺带一提: 光看设备名并不可靠, 实测那台 iPhone 的名字叫 "Emoji"。)"""
    if props is None:
        return "android", "没找到已连接设备, 按 Android 处理"

    name = str(props.get("Alias") or props.get("Name") or "")
    modalias = str(props.get("Modalias") or "")

    m = re.match(r"bluetooth:v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})", modalias)
    if m:
        vendor = int(m.group(1), 16)
        if vendor == APPLE_BT_VENDOR:
            return "ios", f"Modalias 厂商 ID 0x{vendor:04X} = Apple ({modalias})"

    low = name.lower()
    for kw in ("iphone", "ipad", "ipod", "macbook"):
        if kw in low:
            return "ios", f"设备名包含 '{kw}' ({name})"

    for uuid in props.get("UUIDs", []):
        head = str(uuid).lower().split("-")[0]
        if head in APPLE_UUID_PREFIXES:
            return "ios", f"包含 Apple 专有服务 UUID {uuid}"

    if m:
        return "android", (f"Modalias 厂商 ID 0x{int(m.group(1), 16):04X} 非 Apple"
                           f" ({modalias})")
    return "android", f"未发现 Apple 特征 (name={name!r}), 按 Android 处理"


def pick_plan(strategies, requested, platform):
    """挑一套动作方案。返回 (方案, 依据)。"""
    if not strategies:
        return None, "当前模式没有任何可用方案"

    if requested != "auto":
        keyword = PLAN_KEYWORDS[requested]
        for st in strategies:
            if keyword in st["name"]:
                return st, f"命令行指定 --plan {requested}"
        return None, (f"当前模式没有 '{requested}' 这套方案, 可用: "
                      f"{[s['name'] for s in strategies]}")

    for want in PLATFORM_PLANS[platform]:
        keyword = PLAN_KEYWORDS[want]
        for st in strategies:
            if keyword in st["name"]:
                return st, f"{platform} 平台首选 {want}"
    return strategies[0], f"{platform} 的首选方案本模式都没有, 退到第一套"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def captured_stdout(enabled):
    """临时吞掉标准输出, 只把其中的 ERROR/WARN 行捞出来。

    压力测试动辄几千次, 底层每次都打印 [OK] 会把统计信息刷没。
    注意: 期间其它线程 (比如 control 通道的 [RX ...]) 的输出也会被一起吞掉,
    所以 --verbose 时不做这层拦截。"""
    if not enabled:
        yield None
        return
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def channels_up(transport):
    """要求【两条通道都在】才算就绪。

    只有 control 连上是不能用的 —— 输入报告走 interrupt 通道。
    douyin_remote 的 is_connected() 是"有一条就行", 这里比它严格。"""
    with transport.lock:
        both = (transport.ctrl_conn is not None and transport.intr_conn is not None)
        return both or bool(transport.fallback_fds)


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def preflight(args):
    """跑之前的检查: 端口只能被一个进程占, douyin_remote.py 得先停。"""
    try:
        out = subprocess.run(["pgrep", "-af", "douyin_remote.py"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    if not out:
        return True
    print("[ERROR] 检测到 douyin_remote.py 正在运行:")
    for line in out.splitlines():
        print(f"        {line}")
    print("        HID 的 L2CAP 端口 17/19 同一时间只能被一个进程占用, 先停掉它:")
    print("          sudo pkill -f douyin_remote.py")
    if args.force:
        print("[WARN] --force 已指定, 继续尝试 (大概率会 EADDRINUSE)")
        return True
    return False


# ---------------------------------------------------------------------------
# 测试主体
# ---------------------------------------------------------------------------

class StressTest:
    def __init__(self, args, bus, transport, mode_note):
        self.args = args
        self.bus = bus
        self.transport = transport
        self.mode_note = mode_note
        self.stop = threading.Event()

        self.plan = None
        self.plan_note = ""
        self.platform = args.platform
        self.platform_note = ""

        self.sent = 0
        self.failed = 0
        self.disconnects = 0
        self.reconnect_waits = 0.0
        self.max_send_ms = 0.0
        self.total_send_ms = 0.0
        self.errors = {}          # 错误文本 -> 次数
        self.peer = ""
        self.started_at = None
        self.log_fp = None

    # ---------- 连接 ----------
    def wait_for_connection(self, timeout, first_time=False):
        if channels_up(self.transport):
            return True
        if first_time:
            print(f"[WAIT] 等待手机连接 (最多 {fmt_duration(timeout)})...")
            print("       在手机上点击树莓派发起连接即可 (配对已由 switch2hid.sh 完成)")
        else:
            print(f"\n[WAIT] 连接断开, 等待重连 (最多 {fmt_duration(timeout)})...")
        t0 = time.time()
        while not self.stop.is_set():
            if channels_up(self.transport):
                waited = time.time() - t0
                if not first_time:
                    self.reconnect_waits += waited
                print(f"[OK] 两条通道均已建立 (等待 {waited:.1f}s)")
                return True
            if time.time() - t0 >= timeout:
                print(f"[ERROR] 等待超时 ({fmt_duration(timeout)}) —— 手机始终没连上来")
                return False
            self.stop.wait(0.5)
        return False

    # ---------- 自动识别 ----------
    def resolve_plan(self):
        """连上之后再做: 识别平台 -> 选按键方案。"""
        path, props = find_connected_device(self.bus)
        if props is not None:
            self.peer = (str(props.get("Address") or "?") + " / "
                         + str(props.get("Alias") or props.get("Name") or "?"))

        if self.args.platform == "auto":
            self.platform, self.platform_note = detect_platform(props)
        else:
            self.platform_note = f"命令行指定 --platform {self.args.platform}"

        strategies = dr.build_strategies(self.transport)
        self.plan, self.plan_note = pick_plan(strategies, self.args.plan, self.platform)

        print("-" * 58)
        print(f"  对端设备   : {self.peer or '未能从 BlueZ 读到'}")
        print(f"  平台识别   : {self.platform}   ({self.platform_note})")
        print(f"  描述符模式 : {self.mode_note}")
        if self.plan is None:
            print(f"  按键方案   : [失败] {self.plan_note}")
            return False
        print(f"  按键方案   : {self.plan['name']}   ({self.plan_note})")
        if self.platform == "ios" and "拖拽" in self.plan["name"]:
            print("  iOS 提醒   : 鼠标要先开 设置->辅助功能->触控->辅助触控")
        if self.platform == "ios" and "多媒体键" in self.plan["name"]:
            print("  iOS 警告   : 抖音 iOS 版不接媒体键, 这套大概率无效")
        print("-" * 58)
        return True

    # ---------- 单次动作 ----------
    def do_action(self, action):
        """执行一次按键, 返回 (是否成功, 耗时毫秒, 错误文本或 None)。

        底层方法自己 catch 了 OSError 并只是打印, 所以成功与否要靠抓输出判断,
        再加一条"发完之后通道还在"。"""
        fn = self.plan.get(action)
        if fn is None:
            return False, 0.0, f"当前方案不支持动作 {action}"

        t0 = time.time()
        with captured_stdout(not self.args.verbose) as buf:
            fn()
        elapsed_ms = (time.time() - t0) * 1000.0

        problem = None
        if buf is not None:
            for line in buf.getvalue().splitlines():
                if "[ERROR]" in line or "[WARN]" in line:
                    problem = line.strip()
                    break
        if problem is None and not channels_up(self.transport):
            problem = "发送后通道已断开"
        return problem is None, elapsed_ms, problem

    # ---------- 日志 ----------
    def open_log(self):
        if not self.args.log:
            return
        new_file = not os.path.exists(self.args.log)
        self.log_fp = open(self.args.log, "a", encoding="utf-8")
        if new_file:
            self.log_fp.write("timestamp,iteration,action,result,elapsed_ms,note\n")
        self.log_fp.write(f"# start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"platform={self.platform} plan={self.plan['name']} "
                          f"peer={self.peer}\n")
        self.log_fp.flush()

    def log_row(self, action, ok, elapsed_ms, note):
        if not self.log_fp:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        note = (note or "").replace(",", ";").replace("\n", " ")
        self.log_fp.write(f"{stamp},{self.sent},{action},"
                          f"{'OK' if ok else 'FAIL'},{elapsed_ms:.1f},{note}\n")
        self.log_fp.flush()

    # ---------- 统计 ----------
    def print_progress(self):
        elapsed = time.time() - self.started_at
        rate = elapsed / self.sent if self.sent else 0.0
        print(f"[STAT] 已发送 {self.sent} 次 (成功 {self.sent - self.failed} / "
              f"失败 {self.failed}), 断连 {self.disconnects} 次, "
              f"已运行 {fmt_duration(elapsed)}, 平均 {rate:.2f}s/次")

    def print_report(self):
        elapsed = time.time() - self.started_at if self.started_at else 0.0
        avg_send = self.total_send_ms / self.sent if self.sent else 0.0
        print("\n" + "=" * 58)
        print(" 老化测试报告")
        print("=" * 58)
        print(f"  对端设备      : {self.peer or '?'}")
        print(f"  平台 / 方案   : {self.platform} / "
              f"{self.plan['name'] if self.plan else '?'}")
        print(f"  方向          : {self.args.direction}")
        print(f"  间隔          : {self.args.interval}s"
              + (f" (抖动 ±{self.args.jitter:.0%})" if self.args.jitter else ""))
        print(f"  总时长        : {fmt_duration(elapsed)}")
        print(f"  发送次数      : {self.sent}")
        print(f"  成功 / 失败   : {self.sent - self.failed} / {self.failed}"
              + (f"  (失败率 {self.failed / self.sent:.2%})" if self.sent else ""))
        print(f"  断连次数      : {self.disconnects}")
        if self.disconnects:
            print(f"  重连累计等待  : {fmt_duration(self.reconnect_waits)}")
        print(f"  单次发送耗时  : 平均 {avg_send:.1f}ms / 最大 {self.max_send_ms:.1f}ms")
        if self.errors:
            print("  错误分类:")
            for text, n in sorted(self.errors.items(), key=lambda kv: -kv[1]):
                print(f"    {n:6d} x  {text}")
        if self.args.log:
            print(f"  CSV 明细      : {self.args.log}")
        print("=" * 58)

    # ---------- 主循环 ----------
    def run(self):
        args = self.args
        limit_count = args.count if args.count > 0 else None
        limit_seconds = parse_duration(args.duration)
        limit_seconds = limit_seconds if limit_seconds > 0 else None

        if not self.wait_for_connection(args.wait, first_time=True):
            return False
        if not self.resolve_plan():
            return False

        # 鼠标那几套依赖指针位置: 滚轮事件派发给指针下方的 View, 拖拽起点贴着
        # 屏幕底部还会被系统的边缘手势吃掉。所以先把指针定位一次。
        if not args.no_center and self.transport.has_mouse and \
                ("滚轮" in self.plan["name"] or "拖拽" in self.plan["name"]):
            print("[..] 先把鼠标指针定位到屏幕中上部...")
            with captured_stdout(not args.verbose):
                self.transport.center_pointer()

        if args.settle > 0:
            print(f"[..] 等待 {args.settle}s 让链路稳定...")
            self.stop.wait(args.settle)

        print("-" * 58)
        print(f"[START] 方向 {args.direction}, 间隔 {args.interval}s"
              + (f", 上限 {limit_count} 次" if limit_count else "")
              + (f", 上限 {fmt_duration(limit_seconds)}" if limit_seconds else "")
              + "   (Ctrl+C 停止并出报告)")
        print("-" * 58)

        self.started_at = time.time()
        self.open_log()
        toggle = False

        try:
            while not self.stop.is_set():
                if limit_count and self.sent >= limit_count:
                    print(f"\n[DONE] 已达次数上限 {limit_count}")
                    break
                if limit_seconds and (time.time() - self.started_at) >= limit_seconds:
                    print(f"\n[DONE] 已达时长上限 {fmt_duration(limit_seconds)}")
                    break

                # 每次发送前确认链路; 断了就等重连, 不把失败算在按键头上
                if not channels_up(self.transport):
                    self.disconnects += 1
                    if not self.wait_for_connection(args.wait):
                        print("[ABORT] 重连失败, 结束测试")
                        break
                    continue

                if args.direction == "alternate":
                    action = "prev" if toggle else "next"
                    toggle = not toggle
                else:
                    action = args.direction

                ok, elapsed_ms, problem = self.do_action(action)
                self.sent += 1
                self.total_send_ms += elapsed_ms
                self.max_send_ms = max(self.max_send_ms, elapsed_ms)
                if not ok:
                    self.failed += 1
                    self.errors[problem] = self.errors.get(problem, 0) + 1
                    print(f"[FAIL] 第 {self.sent} 次 ({action}): {problem}")
                self.log_row(action, ok, elapsed_ms, problem)

                if args.report_every > 0 and self.sent % args.report_every == 0:
                    self.print_progress()

                delay = args.interval
                if args.jitter > 0:
                    delay = max(0.0, delay * (1.0 + random.uniform(-args.jitter,
                                                                   args.jitter)))
                self.stop.wait(delay)
        finally:
            self.print_report()
            if self.log_fp:
                self.log_fp.close()
        return True


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def main():
    args = build_args()

    if not preflight(args):
        return 1

    mode, mode_note = detect_mode(args.mode)
    descriptor = "".join(item for item, _c in mode["items"])
    print(f"[INFO] Mode: {mode['desc']}")
    print(f"[INFO] {mode_note}")
    print(f"[INFO] HID descriptor: {len(descriptor) // 2} bytes, CoD {mode['cod']}")

    # switch2hid.sh 应该已经禁用了 input 插件, 这里只是复查一遍
    if not dr.check_bluetoothd_plugin():
        return 1

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    dr.watch_device_events(bus)

    if args.agent:
        dr.register_agent(bus, args.agent)

    transport = dr.HidTransport(mode)
    if not transport.start_servers():       # 先占端口, 再注册 SDP
        return 1

    profile_path = "/org/bluez/hci0/profile_hid_stress"
    dr.Profile(bus, profile_path, transport)
    profile_mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                                 "org.bluez.ProfileManager1")
    opts = {
        "ServiceRecord": dr.build_sdp_record(descriptor, mode["subclass"]),
        "Role": "server",
        "RequireAuthentication": True,
        "RequireAuthorization": False,
        "AutoConnect": True,
    }
    try:
        profile_mgr.RegisterProfile(profile_path,
                                    "00001124-0000-1000-8000-00805f9b34fb", opts)
        print("[OK] HID SDP record registered successfully!")
    except Exception as e:
        print(f"[ERROR] Failed to Register Profile: {e}")
        return 1

    test = StressTest(args, bus, transport, mode_note)
    loop = GLib.MainLoop()

    def on_signal(*_):
        # GLib.unix_signal_add 比裸 signal 可靠: MainLoop.run() 阻塞在 C 里,
        # Python 的信号处理函数不一定能及时跑到
        print("\n[INFO] 收到中断信号, 正在收尾...")
        test.stop.set()
        return GLib.SOURCE_CONTINUE

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, on_signal)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, on_signal)

    result = {}

    def worker():
        try:
            result["ok"] = test.run()
        except Exception as e:                       # noqa: BLE001
            print(f"\n[ERROR] 测试线程异常: {e}")
            result["ok"] = False
        finally:
            loop.quit()

    threading.Thread(target=worker, daemon=True).start()
    loop.run()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
