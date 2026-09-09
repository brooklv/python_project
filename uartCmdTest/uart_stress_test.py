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
    Aborted, Ctx, Result, filter_tests, restore_baseline, run_suite,
)
from uart_report import Report
from uart_protocol import parse_hex
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


def run_stress(client: UartClient, args) -> int:
    tests = filter_tests(uart_tests.TESTS, args.tags or uart_tests.DEFAULT_TAGS)

    if not tests:
        ui.log("✗ 按 --tags 筛选后没有可跑的测试")
        return 1

    ui.log(f"本次要跑 {len(tests)} 条测试: " + "、".join(t.name for t in tests))

    st = Stats()
    rep = Report(meta={
        "port": args.port, "baud": args.baud, "ssid": args.ssid or "",
        "tags": ",".join(args.tags or uart_tests.DEFAULT_TAGS),
        "cycles": args.cycles, "retry": args.retry, "timeout": args.timeout,
    })

    failed_cycle = 0
    aborted = False
    interrupted = False
    last: dict = {}

    with ui.StatusBar() as bar:
        ctx = Ctx(client=client, ssid=args.ssid or "",
                  password=args.password or "",
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
        print(f" {mark}{i:3d}) {t.name:<18} [{','.join(t.tags)}]{creds}{needs}")

    print("\n各 tag 下的测试数:")
    line = []
    for g, n in uart_tests.tag_counts().items():
        flag = " (!!有副作用)" if g in danger else ""
        line.append(f"{g}={n}{flag}")
    for i in range(0, len(line), 4):
        print("  " + "  ".join(line[i:i + 4]))

    print("\n  --tags query          只跑只读查询，最安全")
    print("  --tags display,audio  多个 tag 取并集")
    print("  --tags danger         !! 会改配置/重启设备，谨慎")
    print("  依赖会自动带上，不用手动列\n")
    return 0


# ---------------------------------------------------------------- 指令模式
def edit_line(prompt: str, initial: str) -> str:
    """带预填内容的输入行，可退格修改、Enter 确认。

    readline 直接支持预填，还附赠方向键、Ctrl+A/E、历史记录 ——
    C 版为此手写了 60 行 termios raw 模式的行编辑器。
    """
    if readline is None:
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
            print(f"   {i:2d}) {c.label:<26} {c.hex}")
    print("\n  (!! 开头的指令有副作用)\n")


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
    n = 0
    for i in range(MAX_LIST_PKTS):
        got = client.recv(NOTIFY_TIMEOUT if i else None, quiet=bool(i))
        if got is None:
            break
        _pkt, rx = got
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
            print("  可修改，Enter 发送，Ctrl+C 取消")
            try:
                send_hex(client, edit_line("  hex> ", c.hex))
            except KeyboardInterrupt:
                print("  ^取消")
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

  %(prog)s -p /dev/ttyUSB0 -s MyWiFi -w pw123
        默认的 WiFi 老化流程（扫描→取结果→连接→忘记→切5G）跑 100 轮

  %(prog)s -p /dev/ttyUSB0 -s MyWiFi -w pw123 -c 500 -r 3
        长跑 500 轮，单轮失败最多重试 3 次，用于统计偶发失败率

  %(prog)s -p /dev/ttyUSB0 -i
        指令模式，手发十六进制调协议

按分类跑:
  --tags query              只读查询，最安全
  --tags display            旋转/缩放/HDMI/HDCP
  --tags audio,net          多个 tag 取并集
  --tags core -c 500 -r 3   WiFi 老化长跑
  --tags danger             !! 会改配置或重启设备，谨慎

  完整 tag 列表和每组条数见 --list-tests。
  被选中的测试如果依赖了别的测试，依赖会自动带上。

其它:
  -t 45          设备慢时放宽单命令超时
  -l run1.log    分开保存日志，便于对比多次运行
  -b 9600        非默认波特率

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
    p.add_argument("--no-restore", action="store_true",
                   help="跑完不恢复基线状态。默认会把区域码/频段/模式/HDMI/"
                        "旋转/缩放/静音/log 设回已知值，避免设备停在被改过的状态")
    p.add_argument("--report-json", metavar="文件",
                   help="把结果写成 JSON，含每条测试的通过率和耗时统计，"
                        "供脚本分析或趋势对比")
    p.add_argument("--report-junit", metavar="文件",
                   help="把结果写成 JUnit XML，Jenkins / GitLab CI 可直接展示")
    p.add_argument("--list-tests", action="store_true",
                   help="列出全部测试、依赖关系和 tag 后退出，不需要接设备")

    args = p.parse_args(argv)

    if args.list_tests:
        return args
    if not args.port:
        p.error("需要 -p 指定串口（或用 --list-tests 只看测试列表）")

    # 只有真的会用到 ssid/password 的测试才强制要它们。
    # 按 needs_creds 判断而不是按 tag —— "查询 WiFi状态"归类是 wifi，
    # 但它是只读查询，不需要任何凭据。
    if not args.interactive:
        selected = filter_tests(uart_tests.TESTS,
                                args.tags or uart_tests.DEFAULT_TAGS)
        need = [t.name for t in selected if t.needs_creds]

        if need and not (args.ssid and args.password):
            p.error(f"这些测试需要 -s 和 -w: {'、'.join(need)}\n"
                    f"（想跳过就别选它们，比如 --tags query 只跑只读查询）")

    if args.retry < 0:
        args.retry = 0

    return args


def main(argv=None) -> int:
    # 必须在任何输出之前，argparse 的 -h 帮助文本也是中文
    ui.force_utf8()

    args = parse_args(argv)

    if args.list_tests:
        return list_tests()

    ui.init(args.log)

    ui.log(f"UART 测试 | Port: {args.port} | Baud: {args.baud}")
    if args.interactive:
        ui.log("模式: 指令模式")
    else:
        ui.log(f"模式: 测试集 | SSID: {args.ssid} | "
               f"tags={args.tags or uart_tests.DEFAULT_TAGS}")
        ui.log(f"Timeout: {args.timeout}s | Cycles: {args.cycles} | "
               f"Retry: {args.retry}")
    ui.log("=" * 60)

    try:
        with UartClient(args.port, args.baud, args.timeout, log=ui.log) as c:
            if args.interactive:
                return run_cmd_mode(c)

            print("\n" + "=" * 60)
            print(f"启动测试\n目标设备: {args.port}\nWiFi SSID: {args.ssid}")
            print(f"最大循环数: {args.cycles}\n单命令超时: {args.timeout}s")
            print(f"失败重试次数: {args.retry}")
            print("=" * 60 + "\n")

            return run_stress(c, args)

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
