#!/usr/bin/env python3
"""UART 协议测试工具 —— 测试集驱动的压力测试 + 指令模式。

测试内容在 uart_tests.py（加/删测试改那个文件），
执行机制在 uart_exec.py，这里只管 CLI 和外层循环。

WiFi 老化循环默认跑这 5 条，全程由应答驱动、无固定 sleep::

    扫描 → 获取扫描结果 → 连接 → 忘记网络 → 切换 5G

关键约束（都是实测踩出来的）：

* **必须做应答匹配**。设备会主动推送不对应任何请求的异步通知
  （状态变化、OTA 版本），当成应答收下会让整条流永久错位。
* **扫描结果分多个包**，要读到 PACKET_DONE 结束标记为止。
* **状态变化通知是可选包**，设备本来没连网络时不会发，
  当必需包会干等到超时误判失败。
"""

import argparse
import sys

import uart_commands as cmds
import uart_tests
import uart_ui as ui
from uart_exec import (
    MAX_LIST_PKTS, NOTIFY_TIMEOUT,
    Aborted, Ctx, Result, filter_by_setup, filter_tests, restore_baseline,
    run_suite,
)
from uart_report import Report
from uart_protocol import build, build_connect, hex_str, parse_hex
from uart_role import (
    ALL_SETUPS, Role, Setup, Topology,
    guidance as role_guidance, menu_lines as role_menu_lines, parse_setup,
)
from uart_serial import UartClient

try:
    import readline            # 指令模式的可编辑预填靠它
except ImportError:
    readline = None            # Windows 上没有，降级成普通输入


class Stats:
    def __init__(self) -> None:
        self.cycle = 0
        self.ok = 0
        self.warn = 0
        self.retry = 0
        self.retried = 0

    def as_bar(self) -> dict:
        return {"循环": self.cycle, "成功": self.ok,
                "告警": self.warn, "重试": self.retry}


def _tally(records: dict) -> str:
    n = {r: 0 for r in Result}
    for rec in records.values():
        n[rec.result] += 1
    return " / ".join(f"{r.value} {n[r]}" for r in Result if n[r])


def _failed_names(records: dict) -> list:
    return [r.name for r in records.values() if r.result is Result.FAIL]


def run_stress(client: UartClient, args, tests: list,
               setup: Setup, excluded: list) -> int:
    if excluded:
        ui.log(f"按角色（{setup.label}）排除 {len(excluded)} 条:")
        for name, why in excluded:
            ui.log(f"  - {name}（{why}）")

    if not tests:
        ui.log("✗ 筛选后没有可跑的测试")
        if excluded:
            # 全被角色排掉时最容易让人以为工具坏了，直接说清楚怎么办
            ui.log(f"  所选测试都不适用于 {setup.label}。"
                   f"换个 --tags，或确认 --role/--topology 给对了")
        return 1

    ui.log(f"本次要跑 {len(tests)} 条测试: " + "、".join(t.name for t in tests))

    st = Stats()
    rep = Report(meta={
        "port": args.port, "baud": args.baud, "ssid": args.ssid or "",
        "tags": ",".join(args.tags or uart_tests.DEFAULT_TAGS),
        "cycles": args.cycles, "retry": args.retry, "timeout": args.timeout,
        "role": setup.role.value, "topology": setup.topology.value,
    })

    failed_cycle = 0
    aborted = False
    interrupted = False
    last: dict = {}

    with ui.StatusBar() as bar:
        ctx = Ctx(client=client, ssid=args.ssid or "",
                  password=args.password or "", setup=setup,
                  on_warn=lambda: bar.draw(**st.as_bar()))

        # 设备可能还连着上次的网络，先清干净再开始
        ctx.forget_all("初始化")

        try:
            for i in range(1, args.cycles + 1):
                st.cycle = ctx.cycle = i
                bar.draw(**st.as_bar())

                print("\n" + "=" * 60)
                print(f"循环 {i} 开始...")
                print("=" * 60)

                done = False
                for attempt in range(args.retry + 1):
                    if attempt > 0:
                        st.retry += 1
                        ui.log(f"⚠ 循环 {i} 失败，第 {attempt}/{args.retry} 次重试")
                        bar.draw(**st.as_bar())

                        # 先丢掉设备超时后才发的迟到应答，再清网络状态。
                        # 两步都必须做，缺一个重试就是白重试。
                        client.flush_input()
                        ctx.forget_all("重试前清理")

                    # 循环边界是天然的同步点：此刻不该有在途应答，缓冲里剩的
                    # 都是上一轮迟到的或设备主动推的，留着会让这一轮错位
                    client.flush_input()

                    try:
                        last = run_suite(ctx, tests)
                    except Aborted:
                        aborted = True
                        ui.log(f"\n■ 用户中止测试，停在第 {i} 个循环")
                        break

                    st.warn = ctx.warns
                    rep.add_cycle(i, last)

                    if not _failed_names(last):
                        done = True
                        ui.log(f"✓ Cycle {i} SUCCESS ({_tally(last)})")
                        if attempt > 0:
                            st.retried += 1
                            ui.log(f"⚠ 循环 {i} 重试 {attempt} 次后成功")
                        break

                    ui.log(f"✗ Cycle {i} 结果: {_tally(last)}")

                if aborted:
                    break

                if not done:
                    failed_cycle = i
                    bad = _failed_names(last)
                    ui.log(f"\n✗ 测试失败! 第 {i} 个循环失败于: {'、'.join(bad)}")
                    if args.retry:
                        ui.log(f"  已重试 {args.retry} 次仍失败，中止")
                    break

                st.ok += 1
                bar.draw(**st.as_bar())

        except KeyboardInterrupt:
            # 长跑靠 Ctrl+C 结束是常规操作，不能因此丢掉报告
            interrupted = True

        # 收尾恢复基线。有些状态没有查询指令（区域码、频段、投屏），
        # set_and_restore 读不回来，只能在这里统一设成已知值 ——
        # 否则跑完 --tags net 设备可能停在日本区、2.4G、P2P 模式上。
        if not args.no_restore:
            try:
                restore_baseline(ctx, uart_tests.BASELINE)
            except KeyboardInterrupt:
                ui.log("⚠ 基线恢复被打断，设备可能停在非默认状态")

    print("\n" + "=" * 60)
    if interrupted:
        ui.log("■ 被 Ctrl+C 中断")
    elif aborted:
        ui.log("■ 测试被用户中止")
    elif failed_cycle:
        ui.log("✗ 测试失败")
    else:
        ui.log("✓ 测试完成!")

    ui.log(f"成功循环: {st.ok} / {args.cycles}")
    ui.log(f"告警次数: {st.warn}")
    ui.log(f"重试后成功的循环: {st.retried}")
    ui.log(f"累计重试次数: {st.retry}")
    if ctx.checksum_errors:
        ui.log(f"✗ 校验和错误: {ctx.checksum_errors} 次（线路质量问题）")

    if last:
        ui.log("最后一轮各条测试:")
        mark = {Result.PASS: "✓", Result.FAIL: "✗", Result.SKIP: "—"}
        for name, rec in last.items():
            ui.log(f"  {mark[rec.result]} {name}  ({rec.duration:.2f}s)"
                   + (f"  {rec.error}" if rec.error else ""))

    if failed_cycle:
        ui.log(f"失败于第 {failed_cycle} 个循环")

    # 失败次数最多的几条 —— 老化测试最想先知道这个
    worst = rep.worst(3)
    if worst:
        ui.log("失败最多的测试:")
        for s_ in worst:
            ui.log(f"  ✗ {s_.name}: {s_.failed}/{s_.passed + s_.failed} 轮失败")

    # 耗时随轮次变长的 —— 性能退化是渐进的，单轮不超时也可能已经慢了一倍
    slow = rep.slowing()
    if slow:
        ui.log("耗时明显变长的测试:")
        for s_, d in slow:
            ui.log(f"  ⚠ {s_.name}: 前半段 {d['first_half_avg']}s "
                   f"→ 后半段 {d['second_half_avg']}s "
                   f"（慢了 {d['ratio']:.1f} 倍）")

    print("=" * 60)

    code = 130 if interrupted else (1 if (failed_cycle or aborted) else 0)
    rep.finish(code)

    if args.report_json:
        rep.write_json(args.report_json)
        ui.log(f"JSON 报告: {args.report_json}")
    if args.report_junit:
        rep.write_junit(args.report_junit)
        ui.log(f"JUnit 报告: {args.report_junit}")

    return code


def list_tests() -> int:
    tests = uart_tests.TESTS
    danger = set(uart_tests.DANGER_TAGS)

    print(f"\n共 {len(tests)} 条测试。默认跑 tags={uart_tests.DEFAULT_TAGS}"
          f"（{len(filter_tests(tests, uart_tests.DEFAULT_TAGS))} 条）\n")

    for i, t in enumerate(tests, 1):
        needs = f"  ← 依赖 {'、'.join(t.needs)}" if t.needs else ""
        mark = " !!" if danger & set(t.tags) else "   "
        creds = " [需 -s/-w]" if t.needs_creds else ""
        # 角色前提。写出来才看得懂为什么某个角色下这条不跑
        pre = []
        if t.roles:
            pre.append("仅 " + "/".join(r.upper() for r in t.roles))
        if t.needs_ap:
            pre.append("需开热点")
        if t.needs_sta:
            pre.append("需连路由")
        role = f" [{'、'.join(pre)}]" if pre else ""
        print(f" {mark}{i:3d}) {t.name:<18} "
              f"[{','.join(t.tags)}]{creds}{role}{needs}")

    print("\n各 tag 下的测试数:")
    line = []
    for g, n in uart_tests.tag_counts().items():
        flag = " (!!有副作用)" if g in danger else ""
        line.append(f"{g}={n}{flag}")
    for i in range(0, len(line), 4):
        print("  " + "  ".join(line[i:i + 4]))

    print("\n  --tags query          只跑只读查询，最安全")
    print("  --tags display,audio  多个 tag 取并集")
    print("  --tags remote         需先和对端配对，否则全超时")
    print("  --tags danger         !! 会改配置/重启设备，谨慎")
    print("  --tags logmode        !! 开 log 后设备不再回应任何命令")
    print("  依赖会自动带上，不用手动列")

    print("\n角色前提（--role / --topology 决定，不满足的直接不跑）:")
    for s in ALL_SETUPS:
        keep, exc = filter_by_setup(
            filter_tests(tests, uart_tests.DEFAULT_TAGS), s)
        allk, allx = filter_by_setup(tests, s)
        print(f"  --role {s.role.value:<3} --topology {s.topology.value:<8} "
              f"{s.label:<16} 全部 {len(allk):2}/{len(tests)} 条，"
              f"默认组 {len(keep)}/{len(filter_tests(tests, uart_tests.DEFAULT_TAGS))} 条")
    print("  开热点的那一方同时连路由，才跑得了完整 WiFi 流程。\n")
    return 0


# ---------------------------------------------------------------- 指令模式
def edit_line(prompt: str, initial: str) -> str:
    """带预填内容的输入行，可退格修改、Enter 确认。

    readline 直接支持预填，还附赠方向键、Ctrl+A/E、历史记录 ——
    C 版为此手写了 60 行 termios raw 模式的行编辑器。
    """
    if readline is None:
        if initial:                 # 空预填没什么可提示的，别刷屏
            print(f"  预填: {initial}")
        return input(prompt)

    def hook():
        readline.insert_text(initial)
        readline.redisplay()

    readline.set_pre_input_hook(hook)
    try:
        return input(prompt)
    finally:
        readline.set_pre_input_hook(None)


def show_list() -> None:
    print()
    for group, items in cmds.grouped().items():
        print(f"\n  【{group}】")
        for i, c in items:
            # ask 类没有固定字节，别摆一串示例十六进制假装能直接发
            print(f"   {i:2d}) {c.label:<26} {c.hex or '<按提示输入>'}")
    print("\n  (!! 开头的指令有副作用)\n")


# ---------------------------------------------------------------- 交互输入
# SSID、密码这类东西没有合理的默认值。原来指令表里写死了示例值
# （SSID=L-5G / psk=13456789 / abcd / 12345678），选中就直接发出去 ——
# 结果是连一个不存在的网络，或者把设备热点改成示例值。这里改成现场问。

# 加密方式。WPA+SAE 是实测设备接受的写法（WPA2-PSK + WPA3-SAE 兼容），
# 即使扫描结果报的是 WPA-PSK 也用它 —— 所以放第一个当默认。
AUTHEN_CHOICES = [
    ("WPA+SAE", "WPA2/WPA3 兼容，实测可用（推荐）"),
    ("WPA-PSK", "仅 WPA2"),
]


def ask_text(label: str, *, min_len: int = 1, max_len: int = 32,
             default: str = "") -> str:
    """问一个字符串，卡长度。Ctrl+C 抛 KeyboardInterrupt 由调用方兜。"""
    while True:
        s = edit_line(f"    {label}: ", default).strip()

        if len(s) < min_len:
            print(f"    ✗ 至少 {min_len} 个字符")
            continue
        if len(s) > max_len:
            print(f"    ✗ 最多 {max_len} 个字符（输了 {len(s)}）")
            continue
        # data 是按字节发的，非 ASCII 会让长度和字符数不一致，
        # 而设备侧的字段长度限制是按字节算的
        if not s.isascii():
            print("    ✗ 只支持 ASCII 字符")
            continue
        return s


def ask_authen() -> str:
    print("    加密方式:")
    for i, (val, desc) in enumerate(AUTHEN_CHOICES, 1):
        print(f"      {i}) {val:<9} {desc}")

    while True:
        s = input(f"    选择 [1-{len(AUTHEN_CHOICES)}, 默认 1]: ").strip()
        if not s:
            return AUTHEN_CHOICES[0][0]
        if s.isdigit() and 1 <= int(s) <= len(AUTHEN_CHOICES):
            return AUTHEN_CHOICES[int(s) - 1][0]
        print("    ✗ 输入 1 或 2")


def ask_wifi_connect() -> bytes:
    ssid = ask_text("WiFi 名称 (SSID)")
    authen = ask_authen()
    # WPA 规范要求 8~63 位
    pwd = ask_text("WiFi 密码", min_len=8, max_len=63)
    return build_connect(ssid, pwd, authen).data


def ask_ap_ssid() -> bytes:
    print("    !! 这会改设备热点的持久配置")
    return ask_text("新的热点 SSID").encode()


def ask_ap_pwd() -> bytes:
    print("    !! 这会改设备热点的持久配置")
    return ask_text("新的热点密码", min_len=8, max_len=63).encode()


# CmdDef.ask 里的名字 → 取 data 的函数。指令表只放名字，
# 交互逻辑留在这个文件，两边不掺。
ASK_HANDLERS = {
    "wifi_connect": ask_wifi_connect,
    "ap_ssid": ask_ap_ssid,
    "ap_pwd": ask_ap_pwd,
}


def send_hex(client: UartClient, text: str) -> None:
    """解析并发送十六进制，把应答全部打印出来。"""
    try:
        raw = parse_hex(text)
    except ValueError as e:
        ui.log(f"✗ 十六进制格式不对: {e}")
        return

    if not raw:
        return

    ui.log_hex("TX", client.send_raw(raw))

    # 应答可能是多个包（比如扫描结果）。第一个包用正常超时等，后续包用短超时；
    # 读到超时就说明这次应答收完了 —— 所以后续超时要静默，它不是错误。
    #
    # 用 recv_raw 而不是 recv：指令模式手输的可能是任意 TAG，包括透传
    # 那种结构完全不同的帧（57 AC ...）。走 recv 的话解析失败会把
    # 应答字节整个丢掉，报成"没有收到应答" —— 而这里字节本身才是要看的东西。
    n = 0
    for i in range(MAX_LIST_PKTS):
        rx = client.recv_raw(NOTIFY_TIMEOUT if i else None, quiet=bool(i))
        if rx is None:
            break
        ui.log_hex("RX", rx)
        n += 1

    if n == 0:
        ui.log("✗ 没有收到应答")
    else:
        ui.log(f"✓ 收到 {n} 个应答包")


def run_cmd_mode(client: UartClient) -> int:
    print("\n" + "=" * 60)
    print("指令模式")
    print(f"  l          列出全部 {len(cmds.COMMANDS)} 条指令")
    print("  <编号>     选指令，自动预填十六进制，可修改后 Enter 发送")
    print("             标 <按提示输入> 的会先问 SSID/密码这类参数")
    print("  <十六进制>  直接发送，如 47 54 00 00 25 86 01 00 0b")
    print("  q          退出")
    print("=" * 60)
    show_list()

    while True:
        try:
            line = input("cmd> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue
        if line in ("q", "quit", "exit"):
            break
        if line in ("l", "list", "?"):
            show_list()
            continue

        if line.isdigit() and 1 <= int(line) <= len(cmds.COMMANDS):
            c = cmds.COMMANDS[int(line) - 1]
            print(f"\n  [{c.group}] {c.label}")
            try:
                hex_text = c.hex
                if c.ask:
                    # 先问出 data，再拼成十六进制交给下面统一编辑/发送 ——
                    # 这样 ask 类指令也保留"发之前能看能改"的那一步
                    data = ASK_HANDLERS[c.ask]()
                    hex_text = hex_str(build(c.cmd, data, tag=c.tag).pack())

                print("  可修改，Enter 发送，Ctrl+C 取消")
                send_hex(client, edit_line("  hex> ", hex_text))
            except KeyboardInterrupt:
                print("\n  ^取消")
            print()
            continue

        send_hex(client, line)
        print()

    ui.log("指令模式退出")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=f"""UART 协议测试工具

两种模式:
  测试集模式  跑一组可增删的测试，支持循环老化、失败重试、依赖跳过
  指令模式    -i，从 {len(cmds.COMMANDS)} 条指令里选或手输十六进制，单条发送看应答

共 {len(uart_tests.TESTS)} 条测试，用 tag 分组，默认跑 {uart_tests.DEFAULT_TAGS}。
测试定义在 uart_tests.py，加/删测试改那个文件。""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
常用:
  %(prog)s --list-tests
        列出全部测试和 tag，不需要接设备

  %(prog)s -p /dev/ttyUSB0 --tags query -c 1
        只跑只读查询，最安全，不需要 -s/-w

  %(prog)s -p /dev/ttyUSB0 --role rx --topology 1to1 -s MyWiFi -w pw123
        默认的 WiFi 老化流程（扫描→取结果→连接→忘记→切5G）跑 100 轮
        不给 --role/--topology 会先提示选择

  %(prog)s -p /dev/ttyUSB0 --role tx --topology 1tomany -s MyWiFi -w pw123 -c 500 -r 3
        长跑 500 轮，单轮失败最多重试 3 次，用于统计偶发失败率

  %(prog)s -p /dev/ttyUSB0 -i
        指令模式，手发十六进制调协议

被测设备角色（必须，不给会在运行时提示选择）:
  配对关系决定谁开热点，开热点的那一方同时也是 station 连路由，
  给自己和连上它的对端提供上网和 OTA。另一方只连对端热点，不连路由。

  --role rx --topology 1to1      Rx 开热点，Tx 连它        [完整 WiFi 流程]
  --role tx --topology 1tomany   Tx 开 softap，多个 Rx 连它 [完整 WiFi 流程]
  --role tx --topology 1to1      Tx 连 Rx 的热点            [不连路由]
  --role rx --topology 1tomany   Rx 连 Tx 的热点            [不连路由]

  不连路由的角色会自动跳过扫描/连接/忘记网络这套 —— 它们对这台设备
  不适用，不是缺陷，所以直接不跑也不进报告。各角色能跑多少条见
  --list-tests。

按分类跑:
  --tags query              只读查询，最安全
  --tags display            旋转/缩放/HDMI/HDCP
  --tags audio,net          多个 tag 取并集
  --tags core -c 500 -r 3   WiFi 老化长跑
  --tags remote             Remote 指令，需先和对端配对否则全超时
  --tags danger             !! 会改配置或重启设备，谨慎
  --tags logmode            !! 开 log 后设备不再回应任何命令

  完整 tag 列表和每组条数见 --list-tests。
  被选中的测试如果依赖了别的测试，依赖会自动带上。

其它:
  -t 45          设备慢时放宽单命令超时
  -l run1.log    分开保存日志，便于对比多次运行
  -b 9600        非默认波特率
  --tx-checksum  发送时填真校验和（默认 00 00，接收端忽略）

日志:
  所有收发带毫秒时间戳写入 test.log（单行完整十六进制，便于 grep）。
  grep '\\[TX\\]' test.log | sort -u     确认发送的命令是否逐字节相同
  grep '✗' test.log                    看所有失败
  grep '⚠' test.log                    看所有告警

退出码: 0 全部成功 / 1 有失败或被中止 / 130 Ctrl+C
""")

    p.add_argument("-p", "--port", metavar="设备",
                   help="串口号，如 /dev/ttyUSB0。除 --list-tests 外都要")
    p.add_argument("-s", "--ssid", metavar="名称",
                   help="WiFi SSID。只有真会连网的测试才需要，"
                        "--tags query 之类用不到")
    p.add_argument("-w", "--password", metavar="密码",
                   help="WiFi 密码，跟 -s 一起给")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="指令模式：从指令表选或手输十六进制，只需要 -p")
    p.add_argument("-b", "--baud", type=int, default=115200, metavar="波特率",
                   help="波特率，默认 115200。可选 9600/19200/38400/57600")
    p.add_argument("-c", "--cycles", type=int, default=100, metavar="N",
                   help="整个测试集循环几遍，默认 100。跑一遍是功能验证，"
                        "循环跑是老化测试")
    p.add_argument("-t", "--timeout", type=float, default=30.0, metavar="秒",
                   help="等应答第一个字节的超时，默认 30。设备慢时放宽")
    p.add_argument("-r", "--retry", type=int, default=0, metavar="N",
                   help="单轮失败后最多重试几次，默认 0（失败即退出）。"
                        "重试前会先清缓冲+忘记网络")
    p.add_argument("-l", "--log", default="test.log", metavar="文件",
                   help="日志文件，默认 test.log")
    p.add_argument("--tags", type=lambda s: s.split(","), metavar="a,b",
                   help="只跑带这些 tag 的测试，逗号分隔取并集。"
                        "依赖会自动带上。不给就跑默认组")
    p.add_argument("--tx-checksum", action="store_true",
                   help="发送时填真校验和（spec 3.2 规则）。默认填 00 00 ——"
                        "接收端会忽略它，而 00 00 的格式实测长跑通过。"
                        "接收方向的校验和**始终**会验")
    p.add_argument("--no-restore", action="store_true",
                   help="跑完不恢复基线状态。默认会把区域码/频段/模式/HDMI/"
                        "旋转/缩放/静音/log 设回已知值，避免设备停在被改过的状态")
    p.add_argument("--report-json", metavar="文件",
                   help="把结果写成 JSON，含每条测试的通过率和耗时统计，"
                        "供脚本分析或趋势对比")
    p.add_argument("--report-junit", metavar="文件",
                   help="把结果写成 JUnit XML，Jenkins / GitLab CI 可直接展示")
    p.add_argument("--role", choices=[r.value for r in Role], metavar="tx|rx",
                   help="被测设备是发送端还是接收端。不给会在运行时提示选择")
    p.add_argument("--topology", choices=[t.value for t in Topology],
                   metavar="1to1|1tomany",
                   help="配对拓扑。决定谁开热点：一对一 Rx 开、一对多 Tx 开。"
                        "不给会在运行时提示选择")
    p.add_argument("--list-tests", action="store_true",
                   help="列出全部测试、依赖关系和 tag 后退出，不需要接设备")

    args = p.parse_args(argv)

    if args.list_tests:
        return args
    if not args.port:
        p.error("需要 -p 指定串口（或用 --list-tests 只看测试列表）")

    if args.retry < 0:
        args.retry = 0

    return args


# ------------------------------------------------------------ 角色
def ask_setup() -> Setup:
    """没给 --role / --topology 时提示选择。

    这个选择必须问清楚：选错了整批 WiFi 测试会全部超时，而超时看起来
    像设备坏了，不像参数给错了。所以先把配对关系讲明白再让人选。
    """
    print("\n" + "=" * 60)
    print("被测设备的角色")
    print(role_guidance().rstrip())
    print()
    for line in role_menu_lines():
        print(line)
    print("=" * 60)

    while True:
        try:
            s = input(f"选择 [1-{len(ALL_SETUPS)}]: ").strip()
        except EOFError:
            # 读不到输入就没法猜 —— 猜错了是整批 WiFi 测试超时。
            # isatty 在 Windows 上对 `< /dev/null` 会误报成终端，
            # 所以这里必须再兜一层。
            raise SystemExit("\n" + NO_ROLE_HELP)

        if s.isdigit() and 1 <= int(s) <= len(ALL_SETUPS):
            return ALL_SETUPS[int(s) - 1]
        print(f"  ✗ 输入 1 到 {len(ALL_SETUPS)}")


NO_ROLE_HELP = (
    "✗ 没有指定被测设备角色，也读不到输入。请加参数:\n"
    "    --role rx --topology 1to1      Rx 开热点，Tx 连它\n"
    "    --role tx --topology 1tomany   Tx 开 softap，多个 Rx 连它\n"
    "    --role tx --topology 1to1      Tx 连 Rx 的热点，不连路由\n"
    "    --role rx --topology 1tomany   Rx 连 Tx 的热点，不连路由\n"
    "  开热点的那一方同时连路由，才跑得了完整 WiFi 流程。")


def resolve_setup(args) -> Setup:
    """定出被测设备的角色：命令行给了就用，没给就提示。

    非交互环境（CI、管道）不能停下来等输入 —— 那会挂住或者读到 EOF，
    症状比"参数没给"难查得多。所以直接报错并说清楚该加什么参数。
    """
    setup = parse_setup(args.role, args.topology)
    if setup is not None:
        return setup

    if not sys.stdin.isatty():
        raise SystemExit(NO_ROLE_HELP)

    return ask_setup()


def select_tests(args, setup: Setup) -> tuple:
    """按 --tags 再按角色筛出本次要跑的测试。返回 ``(tests, excluded)``。"""
    tests = filter_tests(uart_tests.TESTS, args.tags or uart_tests.DEFAULT_TAGS)
    return filter_by_setup(tests, setup)


def check_creds(args, tests: list) -> None:
    """只有真的会用到 ssid/password 的测试才强制要它们。

    按 needs_creds 判断而不是按 tag —— "查询 WiFi状态"归类是 wifi，
    但它是只读查询，不需要任何凭据。

    而且必须在**角色筛选之后**才检查：一台不连路由的设备（一对一的 Tx、
    一对多的 Rx）压根不会跑"连接网络"，却被要求给 -s/-w 就很莫名。
    """
    need = [t.name for t in tests if t.needs_creds]
    if need and not (args.ssid and args.password):
        raise SystemExit(
            f"✗ 这些测试需要 -s 和 -w: {'、'.join(need)}\n"
            f"  想跳过就别选它们，比如 --tags query 只跑只读查询")


def main(argv=None) -> int:
    # 必须在任何输出之前，argparse 的 -h 帮助文本也是中文
    ui.force_utf8()

    args = parse_args(argv)

    if args.list_tests:
        return list_tests()

    # 角色要在开日志、开串口之前定下来：它决定跑哪些测试，也决定
    # 要不要 -s/-w。提示菜单不该混在测试日志里。
    setup, tests, excluded = None, [], []
    if not args.interactive:
        setup = resolve_setup(args)
        tests, excluded = select_tests(args, setup)
        check_creds(args, tests)

    ui.init(args.log)

    ui.log(f"UART 测试 | Port: {args.port} | Baud: {args.baud}")
    if args.interactive:
        ui.log("模式: 指令模式")
    else:
        ui.log(f"模式: 测试集 | SSID: {args.ssid} | "
               f"tags={args.tags or uart_tests.DEFAULT_TAGS}")
        ui.log(f"角色: {setup.label}")
        ui.log(f"  {setup.describe()}")
        ui.log(f"Timeout: {args.timeout}s | Cycles: {args.cycles} | "
               f"Retry: {args.retry}")
    ui.log("=" * 60)

    try:
        with UartClient(args.port, args.baud, args.timeout, log=ui.log,
                        tx_checksum=args.tx_checksum) as c:
            if args.interactive:
                return run_cmd_mode(c)

            print("\n" + "=" * 60)
            print(f"启动测试\n目标设备: {args.port}\nWiFi SSID: {args.ssid}")
            print(f"角色: {setup.label}")
            print(f"最大循环数: {args.cycles}\n单命令超时: {args.timeout}s")
            print(f"失败重试次数: {args.retry}")
            print("=" * 60 + "\n")

            return run_stress(c, args, tests, setup, excluded)

    except KeyboardInterrupt:
        ui.log("\n■ 被 Ctrl+C 中断")
        return 130
    except Exception as e:                       # 串口打不开等
        ui.log(f"✗ {type(e).__name__}: {e}")
        return 1
    finally:
        ui.close()


if __name__ == "__main__":
    sys.exit(main())
