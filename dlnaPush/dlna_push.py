#!/usr/bin/env python3
"""DLNA 媒体推流控制点 (DMC) —— 支持 视频/音频/图片。

C 版（linux_c-programe/dlna_push）的 Python 移植。行为、命令集、参数名
都保持一致，区别只在实现：**不依赖 libupnp，只用标准库**。
SSDP 是自己发的 UDP 组播，SOAP 是自己拼的 XML，HTTP 服务是 http.server。
装不了 pip 包的树莓派（externally-managed-environment）上开箱即用。

    python3 dlna_push.py -i wlan0 list
    python3 dlna_push.py -i wlan0 /media/pie/Disk/tmp/nature.mp4 0
    python3 dlna_push.py -i wlan0 -f ts /media/pie/Disk/tmp/a.mkv 客厅
"""

import argparse
import os
import signal
import sys

from dlna_net import IfaceError, list_ifaces, resolve_bind_ip
from dlna_session import Session
from dlna_shell import HAVE_READLINE, Shell, choose_device
from dlna_soap import SoapError
from dlna_stress import StressConfig, report, run_stress
from dlna_transcode import OutFormat, TranscodeError, XcodeConfig, parse_format

# 退出码。老化模式默认按"测试"的语义：设备活着就是通过。
# --expect-crash 反过来，和旧的 dlna_crash_repro.py 一致（复现到崩溃才算成功）。
EXIT_OK = 0
EXIT_SETUP_ERROR = 1
EXIT_NOT_REPRODUCED = 2     # 仅 --expect-crash: 跑完了但没崩
EXIT_CRASHED = 3            # 老化语义: 设备失联
EXIT_INTERRUPTED = 130

EPILOG = """\
说明:
  - 媒体可为本地文件(自动起 HTTP 服务) 或 http(s):// 地址
  - 按扩展名自动识别: 视频(mp4/mkv/...) / 音频(mp3/flac/wav/...) / 图片(jpg/png/...)
  - 音频/视频可控音量与进度; 图片仅显示(stop/quit)
  - 有线与 WiFi 共存时务必用 -i 指定网卡, 否则 SSDP 可能从错的网段发出去,
    症状是一台设备都搜不到

推流格式 (决定电视拉到的是原始文件还是 ffmpeg 处理后的产物):
  raw  : 原始文件直推, 不起 ffmpeg, 能不能播全看电视解码能力
  hls  : 切成 index.m3u8 + seg*.ts (电视需支持 HLS, 并非 DLNA 标准)
  ts   : 重封装成单个 MPEG-TS, 老电视兼容性最好
  mp4  : 重封装成 MP4 + faststart

树莓派提示:
  默认 copy 只重封装, CPU 几乎不动; 真要重编码 1080p 软编远达不到实时,
  Pi 4 及更早可试 --vcodec h264_v4l2m2m (Pi 5 无硬件 H.264 编码器)。
  /tmp 在 SD 卡上, 长片建议 --work-dir 指到 U 盘/移动硬盘。

老化 / 崩溃复现 (--stress):
  默认按测试语义给退出码: 0=设备全程存活, 3=设备失联。
  加 --expect-crash 则与旧的 dlna_crash_repro.py 一致: 0=复现成功, 2=没崩。
  崩溃走的是 LastChange 事件路径, 所以 --subscribe 基本是必开的。

示例:
  %(prog)s -i wlan0 list
  %(prog)s -i wlan0 /home/user/movie.mp4 0
  %(prog)s -i wlan0 /home/user/song.mp3 客厅
  %(prog)s -i wlan0 -f ts /media/pie/Disk/tmp/a.mkv 0
  %(prog)s -i wlan0 -f hls --live --work-dir /media/pie/Disk/hls a.mkv 0
  %(prog)s -i wlan0 -f mp4 --vcodec h264_v4l2m2m --acodec aac a.mkv 0

  # 老化: 推流后反复改音量, 盯设备会不会崩
  %(prog)s -i wlan0 nature.mp4 192.168.50.17 --stress --subscribe \\
      --burst 4 --max-rounds 120 --interval 2.0

  # 不推流, 直接在设备当前播放状态下压
  %(prog)s -i wlan0 --stress --no-push --subscribe 192.168.50.17
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dlna_push.py",
        description="DLNA 媒体推流控制点 (DMC) —— 支持 视频/音频/图片",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("media", metavar="媒体|list", nargs="?",
                   help="本地文件路径 / http(s) 地址; 写 list 则只列出设备; "
                        "配合 --no-push 时可省略(此时第一个位置参数是设备)")
    p.add_argument("device", metavar="序号|名称|IP", nargs="?",
                   help="目标设备; 不给则搜索后交互选择")

    p.add_argument("-i", "--iface", metavar="网卡",
                   default=os.environ.get("DLNA_IFACE"),
                   help="绑定的网卡(如 wlan0), 决定从哪个网段搜索设备并提供媒体 "
                        "(也可用环境变量 DLNA_IFACE)")
    p.add_argument("-f", "--format", dest="fmt", metavar="格式", default="raw",
                   help="raw(默认) | hls(=m3u8) | ts(=mpegts) | mp4")
    p.add_argument("--vcodec", metavar="编码", default="copy",
                   help="-c:v, 默认 copy (只换容器不重编码)")
    p.add_argument("--acodec", metavar="编码", default="copy",
                   help="-c:a, 默认 copy")
    p.add_argument("--hls-time", metavar="秒", type=int, default=6,
                   help="HLS 分片时长, 默认 6")
    p.add_argument("--live", action="store_true",
                   help="仅 hls: 边转边播(秒开, 只能拖到已生成处); "
                        "默认转完再播(可随意拖)")
    p.add_argument("--work-dir", metavar="目录", default="",
                   help="产物输出目录, 默认 /tmp/dlna_push_<pid>; 退出时自动清理")
    p.add_argument("--ffmpeg", metavar="路径",
                   default=os.environ.get("DLNA_FFMPEG", "ffmpeg"),
                   help="ffmpeg 可执行文件 (也可用环境变量 DLNA_FFMPEG)")
    p.add_argument("--ff-extra", metavar="参数", default="",
                   help="透传给 ffmpeg 的额外参数, 按空格切分, 放在输出文件之前")
    p.add_argument("--scan-time", metavar="秒", type=int, default=4,
                   help="每轮 SSDP 搜索等待的秒数, 默认 4; 设备多或网络差可调大")
    p.add_argument("--http-log", action="store_true",
                   help="打印内置 HTTP 服务的每条请求; 排查电视到底有没有来拉流")

    d = p.add_argument_group("SSDP 搜不到时直连设备")
    d.add_argument("--port", metavar="端口", type=int, default=49152,
                   help="直连时设备描述的端口, 默认 49152")
    d.add_argument("--desc-path", metavar="路径", default="/description.xml",
                   help="直连时设备描述的路径, 默认 /description.xml")

    g = p.add_argument_group(
        "老化 / 压力测试 (--stress)",
        "反复交替设置音量, 盯着设备会不会崩; 用来复现 826x 的 "
        "SetVolume LastChange 崩溃")
    g.add_argument("--stress", action="store_true",
                   help="推流后不进交互控制台, 改跑老化循环")
    g.add_argument("--no-push", action="store_true",
                   help="跳过推流, 在设备当前播放状态下直接测音量")
    g.add_argument("--subscribe", action="store_true",
                   help="发起 GENA 事件订阅并接 NOTIFY; "
                        "崩溃走的是事件路径, 不订阅可能一直复现不出来")
    g.add_argument("--churn", action="store_true",
                   help="每 10 轮插一次 Stop/Play 扰动(状态机 + 定时器并发)")
    g.add_argument("--vol-a", metavar="音量", type=int, default=10,
                   help="交替音量 A, 默认 10")
    g.add_argument("--vol-b", metavar="音量", type=int, default=21,
                   help="交替音量 B, 默认 21; 必须与 A 不同, "
                        "否则会被设备的 same-volume 检查全部拦掉")
    g.add_argument("--burst", metavar="次数", type=int, default=1,
                   help="每组连发次数, 默认 1; >1 时在 200ms 定时器窗口内密集连发")
    g.add_argument("--burst-gap", metavar="秒", type=float, default=0.13,
                   help="组内连发间隔, 默认 0.13 (逼近真实控制端节奏)")
    g.add_argument("--interval", metavar="秒", type=float, default=0.5,
                   help="组间隔, 默认 0.5; 要 > 0.2 才跨得过设备的定时器周期")
    g.add_argument("--max-rounds", metavar="轮数", type=int, default=60,
                   help="最大轮数, 默认 60")
    g.add_argument("--confirm", metavar="次数", type=int, default=3,
                   help="判定设备失联所需的连续探活失败次数, 默认 3 (防误报)")
    g.add_argument("--recover-wait", metavar="秒", type=float, default=60.0,
                   help="判定失联后等待设备复活的秒数, 默认 60 (防漏报)")
    g.add_argument("--expect-crash", action="store_true",
                   help="按崩溃复现的语义给退出码: 0=复现成功, 2=没崩; "
                        "不给则按老化语义: 0=设备存活, 3=设备失联")
    return p


def normalize(args) -> None:
    """位置参数的两处收拾，都是为了让常见写法不用记规则。

    --no-push 时没有媒体可给，那唯一的位置参数就是设备 ——
    `--stress --no-push 192.168.50.17` 应该按人的直觉工作。
    """
    if args.no_push and args.media and args.device is None and args.media != "list":
        args.media, args.device = None, args.media
    if args.no_push and not args.stress:
        args.stress = True      # --no-push 只有在老化模式下才有意义


def build_stress_cfg(args) -> StressConfig:
    return StressConfig(
        vol_a=args.vol_a, vol_b=args.vol_b,
        burst=args.burst, burst_gap=args.burst_gap,
        interval=args.interval, max_rounds=args.max_rounds,
        churn=args.churn, subscribe=args.subscribe,
        confirm=args.confirm, recover_wait=args.recover_wait,
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    normalize(args)

    if args.media is None and not args.no_push:
        print("[-] 缺少媒体参数 (要跳过推流请加 --no-push)", file=sys.stderr)
        return EXIT_SETUP_ERROR

    # 老化参数在开扫之前就校验完：搜索要花好几秒，之后才告诉人"两个音量
    # 不能一样"，那几秒纯属白等。
    if args.stress:
        try:
            build_stress_cfg(args).validate()
        except ValueError as exc:
            print(f"[-] {exc}", file=sys.stderr)
            return EXIT_SETUP_ERROR

    try:
        cfg = XcodeConfig(
            fmt=parse_format(args.fmt),
            ffmpeg=args.ffmpeg,
            vcodec=args.vcodec,
            acodec=args.acodec,
            hls_time=args.hls_time,
            live=args.live,
            work_dir=args.work_dir,
            extra=args.ff_extra.split(),
        )
    except ValueError as exc:
        print(f"[-] {exc}", file=sys.stderr)
        return EXIT_SETUP_ERROR

    try:
        bind_ip = resolve_bind_ip(args.iface)
    except IfaceError as exc:
        print(f"[-] {exc}", file=sys.stderr)
        names = list_ifaces()
        if names:
            print(f"    当前有 IPv4 的网卡: {', '.join(names)}", file=sys.stderr)
        return EXIT_SETUP_ERROR

    # SIGTERM 默认直接结束进程，finally 不会跑 —— 那样 ffmpeg 会变孤儿、
    # 产物留在磁盘上。转成 SystemExit 让清理照常走完。
    signal.signal(signal.SIGTERM, lambda _s, _f: sys.exit(143))

    print(f"[*] 绑定网卡: {args.iface or '(自动)'}, 本机地址: {bind_ip}")
    print(f"[*] 行编辑: {'readline 已启用' if HAVE_READLINE else '不可用(退格依赖终端设置)'}")
    if cfg.enabled:
        print(f"[*] 推流格式: {cfg.describe()}")
        if cfg.fmt is OutFormat.HLS:
            print("[!] HLS 不是 DLNA 标准, 能不能播取决于电视自身是否支持 HLS; "
                  "不行就换 -f ts")

    session = Session(bind_ip, cfg, http_verbose=args.http_log)
    session.scan_seconds = args.scan_time
    try:
        session.start()
        return run(session, args)
    except KeyboardInterrupt:
        print("\n[*] 已中断")
        return 130
    finally:
        # 只在真推过流时才提这句：list 模式下没有任何产物，说"清理"会让人
        # 以为刚才干了什么。
        if cfg.enabled and session.media:
            print("[*] 清理转换产物 (电视将无法继续拉流)")
        session.close()


def run(session: Session, args) -> int:
    if args.media == "list":
        session.scan()
        print(session.device_list_text())
        return EXIT_OK

    found = session.scan()
    print(session.device_list_text())
    if found == 0 and not _looks_like_ip(args.device):
        print("[-] 未发现任何 DLNA 渲染设备", file=sys.stderr)
        return EXIT_SETUP_ERROR

    if not pick(session, args):
        return EXIT_SETUP_ERROR
    print(f"[*] 目标设备: {session.renderer.friendly}")

    if not args.no_push:
        try:
            session.push(args.media)
        except (SoapError, TranscodeError, OSError) as exc:
            print(f"[-] 推流失败: {exc}", file=sys.stderr)
            return EXIT_SETUP_ERROR
    else:
        print("[*] 已指定 --no-push, 跳过推流")

    if args.stress:
        return stress(session, args)

    Shell(session).run()
    print("[*] 退出 (设备继续播放中; 如需停止请先执行 stop)")
    return EXIT_OK


def _looks_like_ip(text) -> bool:
    if not text:
        return False
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) < 256 for p in parts)


def pick(session: Session, args) -> bool:
    """选设备。给的是 IP 而 SSDP 没搜到时自动转直连 —— 组播被 AP 隔离
    或丢包时，设备明明能 ping 通却搜不到是常态。"""
    if args.device and session.select(args.device):
        return True
    if _looks_like_ip(args.device):
        return session.connect_direct(args.device, args.port, args.desc_path)
    return choose_device(session, args.device)


def stress(session: Session, args) -> int:
    try:
        result = run_stress(session.renderer, build_stress_cfg(args),
                            session.bind_ip)
    except (ValueError, RuntimeError) as exc:
        print(f"[-] {exc}", file=sys.stderr)
        return EXIT_SETUP_ERROR

    report(result, expect_crash=args.expect_crash)
    if result.crashed:
        return EXIT_OK if args.expect_crash else EXIT_CRASHED
    if result.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_NOT_REPRODUCED if args.expect_crash else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
