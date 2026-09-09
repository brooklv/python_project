"""测试执行机制：上下文、测试定义、跑测试集的 runner。

这个文件是机制，不含具体测试内容 —— 测试写在 uart_tests.py 里。

为什么测试是"小函数"而不是纯数据表：各条测试的成功判定差异是逻辑种类的
差异，不是参数的差异。只看返回码的、要循环收多个分包的、要在应答里找
字节序列的、缺了可选包只告警的 —— 硬塞进配置表会长出一套比 Python
本身更难读的自定义语法。所以简单的用 ``simple()`` 一行生成，
复杂的自己写函数。
"""

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import uart_ui as ui
from uart_protocol import (
    Cmd, Packet, STA_FORGET, TAG_LOCAL, Tag, build, checksum_of, err_name,
    resp_id_of, tag_name, tag_value,
)

# 刻意不 import uart_serial：Ctx 只用到 client 的 send/recv/flush_input 三个方法，
# 按鸭子类型接受就行。这样执行逻辑不依赖 pyserial，测试里可以直接喂假 client。

MAX_LIST_PKTS = 64             # 分包应答最多读几个，防止流错位时死等
MAX_REPLY_PKTS = 8             # 单条命令最多读几个应答包

NAME_CONFIRM_TIMEOUT = 15.0    # 等网络名确认。实测连接应答要 5.2s，留 3 倍余量
NOTIFY_TIMEOUT = 5.0           # 等可选的状态通知。实测 0.5s 内到


class TestFailed(Exception):
    """一条测试失败。消息会直接打进日志。"""


class Aborted(Exception):
    """用户在确认提示里选了中止。

    用异常而不是返回值，是为了让"中止"能穿过 runner 和重试逻辑直达顶层 ——
    否则会被当成普通失败，重试机制立刻把它重跑一遍，中止就失效了。
    """


class Result(Enum):
    PASS = "通过"
    FAIL = "失败"
    SKIP = "跳过"


@dataclass
class TestRecord:
    """一条测试跑完的结果。耗时是为了做时延回归和 CI 报告。"""

    name: str
    result: Result
    duration: float = 0.0
    error: str = ""
    tags: tuple = ()


@dataclass(frozen=True)
class Test:
    """一条测试。

    :param run:   接收 Ctx，失败时抛 TestFailed
    :param needs: 依赖的测试名。任一依赖没通过就跳过本测试 ——
                  这样报告里能区分"真正的失败点"和"被连带的",
                  而不是一串级联失败掩盖真因。
    :param tags:  用于 --tags 筛选，纯归类用途
    :param needs_creds: 这条测试真的要用 ctx.ssid / ctx.password。
                  必须显式标注而不能从 tags 推断 —— "查询 WiFi状态"
                  归类上属于 wifi，却是只读查询不需要任何凭据。
    :param max_duration: 耗时上限（秒）。超了算失败。

                  老化测试里性能退化是真实的失效模式：设备还在回应、
                  返回码也对，但连接从 5 秒变成 25 秒。只看通过/失败
                  发现不了，必须卡时延。

    下面三个是**角色前提**：不满足时这条测试根本不该跑，而不是跑了算失败。
    被排除的测试在报告里不出现 —— "这台设备本来就不做这件事"不是缺陷。

    :param roles: 限定 Role。空 = 不限。例如查 Tx 版本只在 Tx 上有意义。
    :param needs_ap:  需要本机开热点（改热点 SSID/密码这类）
    :param needs_sta: 需要本机作为 station 连路由（扫描/连接/忘记网络）。
                  只有开热点的那一方才连路由，另一方连的是对端的热点。
    """
    name: str
    run: Callable[["Ctx"], None]
    needs: tuple = ()
    tags: tuple = ()
    needs_creds: bool = False
    max_duration: Optional[float] = None
    roles: tuple = ()
    needs_ap: bool = False
    needs_sta: bool = False

    def applies_to(self, setup) -> bool:
        """这条测试在给定角色下有没有意义。setup 为 None 时一律有。"""
        if setup is None:
            return True
        if self.roles and setup.role.value not in self.roles:
            return False
        if self.needs_ap and not setup.is_ap:
            return False
        if self.needs_sta and not setup.is_sta:
            return False
        return True

    def why_excluded(self, setup) -> str:
        """被角色排除的原因，用于告诉用户少跑了什么、为什么。"""
        if self.roles and setup.role.value not in self.roles:
            want = "/".join(r.upper() for r in self.roles)
            return f"只适用于 {want}"
        if self.needs_ap and not setup.is_ap:
            return "需要本机开热点"
        if self.needs_sta and not setup.is_sta:
            return "需要本机连路由"
        return ""


def simple(cmd: int, data: bytes = b"", tag: int = Tag.TG):
    """只看返回码的测试，一行生成。data 留空即查询指令。

    设置类命令用它就够了 —— 应答是 ``rc=1 SUCCESS``，没有别的内容。
    """
    return lambda ctx: ctx.step(cmd, data, tag=tag)


def query_int(cmd: int, size: int = 1, offset: int = 0,
              unit: str = "", names: Optional[dict] = None,
              expect: Optional[int] = None, allowed=None,
              lo: Optional[int] = None, hi: Optional[int] = None,
              tag: int = Tag.TG):
    """查询指令，解析返回的整数并可选地断言。

    应答布局是 ``rc(2) + [填充] + 值(小端)``::

        获取缩放比例  29 48 04 00 | 00 00 78 00      -> 偏移0, 2字节 = 120
        获取旋转角度  23 48 03 00 | 00 00 03         -> 偏移0, 1字节 = 3
        配对状态查询  0C 49 04 00 | 00 00 00 03      -> 偏移1, 1字节 = 3

    ``offset`` 是值相对 rc 之后的偏移 —— 有些命令中间夹了一个填充字节。
    ``names`` 给出"值 → 含义"的映射，打印时一并显示。

    断言参数都是可选的，给了才检查：

    :param expect:  精确期望值
    :param allowed: 合法值集合。传 ``True`` 时直接用 ``names`` 的键 ——
                    枚举表本身就是合法集合，不用写两遍
    :param lo:      下界（闭区间）
    :param hi:      上界（闭区间）

    不加断言时它只是"观测"而不是"测试" —— 音量返回 7 还是 255 都算通过。
    """
    if allowed is True:
        if not names:
            raise ValueError("allowed=True 需要同时给 names")
        allowed = set(names)
    elif allowed is not None:
        allowed = set(allowed)

    def run(ctx: "Ctx") -> None:
        pkt = ctx.step(cmd, tag=tag)
        start = 2 + offset
        raw = pkt.data[start:start + size]

        if len(raw) < size:
            raise TestFailed(
                f"应答数据不够: 需要偏移 {start} 起 {size} 字节，"
                f"实际 data 只有 {len(pkt.data)} 字节")

        n = int.from_bytes(raw, "little")
        label = f"  → {names[n]}" if names and n in names else ""
        shown = f"{n}{unit}{label}"

        if expect is not None and n != expect:
            raise TestFailed(f"期望 {expect}{unit} 但取回 {shown}")

        if allowed and n not in allowed:
            raise TestFailed(
                f"取回 {shown}，不在合法值 {sorted(allowed)} 内")

        if lo is not None and n < lo:
            raise TestFailed(f"取回 {shown}，低于下界 {lo}{unit}")

        if hi is not None and n > hi:
            raise TestFailed(f"取回 {shown}，高于上界 {hi}{unit}")

        ui.log(f"✓ 取回值: {shown}")
    return run


def query_str(cmd: int, expect: Optional[str] = None,
              pattern: Optional[str] = None, min_len: int = 1,
              tag: int = Tag.TG):
    """查询指令，解析返回的 ASCII 串并可选地断言。

    例如获取 Mac 地址 ``10 42 13 00 | 00 00 'FC:19:28:36:95:69'``。
    以第一个 0x00 截断（设备会用 0 填充尾部）。

    :param expect:  精确期望值
    :param pattern: 正则。卡格式最有效 —— Mac 地址、版本号这类只要
                    形状对不上，就说明设备回的是垃圾或者流已经错位了
    :param min_len: 最短长度，默认 1（非空）
    """
    rx = re.compile(pattern) if pattern else None

    def run(ctx: "Ctx") -> None:
        pkt = ctx.step(cmd, tag=tag)
        raw = pkt.data[2:].split(b"\x00")[0]

        if not raw:
            raise TestFailed(f"应答里没有数据 (data 长度 {len(pkt.data)})")

        s = raw.decode("utf-8", errors="replace")

        if len(s) < min_len:
            raise TestFailed(f"取回 {s!r} 长度 {len(s)}，短于 {min_len}")

        if expect is not None and s != expect:
            raise TestFailed(f"期望 {expect!r} 但取回 {s!r}")

        if rx and not rx.fullmatch(s):
            raise TestFailed(f"取回 {s!r} 不符合格式 {pattern!r}")

        ui.log(f"✓ 取回: {s}")
    return run


def set_and_verify(cmd: int, data: bytes, expect: int,
                   size: int = 1, offset: int = 0,
                   names: Optional[dict] = None):
    """设置 → 回读 → 断言值对上。**这才是功能测试。**

    ``simple()`` 只能确认"设备没报错"，无法确认功能真的生效了 ——
    发"旋转90度"收到 rc=1，屏幕可能根本没转。这里发完再用同一条命令的
    查询形式（空 data）读回来比对。

    前提是该命令支持查询（DataLen=FF FF）。不支持的只能用 simple()。
    """
    def run(ctx: "Ctx") -> None:
        ctx.step(cmd, data)                     # 设置，rc 不对会直接抛

        pkt = ctx.step(cmd)                     # 空 data = 查询
        start = 2 + offset
        raw = pkt.data[start:start + size]

        if len(raw) < size:
            raise TestFailed(
                f"回读失败: 需要偏移 {start} 起 {size} 字节，"
                f"应答 data 只有 {len(pkt.data)} 字节")

        got = int.from_bytes(raw, "little")
        if got != expect:
            want_s = f"{expect}" + (f"({names[expect]})"
                                    if names and expect in names else "")
            got_s = f"{got}" + (f"({names[got]})" if names and got in names else "")
            raise TestFailed(f"设置了 {want_s} 但回读是 {got_s}")

        label = f"  → {names[got]}" if names and got in names else ""
        ui.log(f"✓ 设置并回读确认: {got}{label}")
    return run


def set_and_restore(cmd: int, data: bytes, expect: int,
                    size: int = 1, offset: int = 0,
                    names: Optional[dict] = None):
    """读原值 → 设置 → 回读断言 → **恢复原值**。

    比 ``set_and_verify`` 多了恢复这一步，用于会影响其它测试的状态。

    为什么需要：测试之间会通过设备状态互相污染。典型的是网络模式 ——
    切到 P2P 之后 WiFi station 测试全会失败；多轮循环时上一轮遗留的
    状态会毒害下一轮。区域码同理，改了之后可用信道就变了。

    代价是每条测试 4 次收发而不是 2 次，所以只在状态真会外溢时用。
    前提同样是该命令支持查询（DataLen=FF FF）。
    """
    def _read(ctx: "Ctx") -> int:
        pkt = ctx.step(cmd)
        start = 2 + offset
        raw = pkt.data[start:start + size]
        if len(raw) < size:
            raise TestFailed(
                f"读取原值失败: 需要偏移 {start} 起 {size} 字节，"
                f"应答 data 只有 {len(pkt.data)} 字节")
        return int.from_bytes(raw, "little")

    def run(ctx: "Ctx") -> None:
        orig = _read(ctx)

        ctx.step(cmd, data)                     # 设置
        got = _read(ctx)                        # 回读

        if got != expect:
            # 断言失败前先把状态放回去，别把设备留在半途
            try:
                ctx.step(cmd, orig.to_bytes(size, "little"))
            except TestFailed:
                pass
            raise TestFailed(f"设置了 {expect} 但回读是 {got}")

        label = f"  → {names[got]}" if names and got in names else ""
        ui.log(f"✓ 设置并回读确认: {got}{label}")

        if orig != got:
            ctx.step(cmd, orig.to_bytes(size, "little"))
            back = f"  → {names[orig]}" if names and orig in names else ""
            ui.log(f"↩ 已恢复原值: {orig}{back}")
    return run


def query_hex(cmd: int, min_len: int = 1, expect_len: Optional[int] = None,
              tag: int = Tag.TG):
    """查询指令，返回内容格式未知时直接打十六进制。

    用于结构还没搞清楚的查询（编码参数、AP 参数这类），
    至少能把设备的回复留在日志里供以后分析。

    :param min_len:    payload 最少几字节，默认 1（非空）
    :param expect_len: payload 精确长度。结构固定的查询可以卡这个 ——
                       长度变了说明协议改了或者流错位了
    """
    def run(ctx: "Ctx") -> None:
        pkt = ctx.step(cmd, tag=tag)
        payload = pkt.data[2:]

        if len(payload) < min_len:
            raise TestFailed(
                f"payload {len(payload)} 字节，少于 {min_len} "
                f"(data 长度 {len(pkt.data)})")

        if expect_len is not None and len(payload) != expect_len:
            raise TestFailed(f"payload {len(payload)} 字节，期望 {expect_len}")

        ui.log(f"✓ 取回 {len(payload)} 字节: {payload.hex(' ').upper()}")
    return run


@dataclass
class Ctx:
    """跑测试时的上下文：串口 + 参数 + 测试之间共享的状态。"""

    client: Any                 # 需要 send / recv / flush_input 三个方法
    ssid: str = ""
    password: str = ""
    cycle: int = 0
    # 被测设备的角色（uart_role.Setup）。测试函数要按角色分叉时读它。
    # None = 没指定，测试一律照跑。
    setup: Any = None

    # 测试之间传递的状态
    scan_count: int = 0
    networks: dict = field(default_factory=dict)

    # 统计
    warns: int = 0
    checksum_errors: int = 0
    on_warn: Optional[Callable[[], None]] = None    # 告警后刷新状态栏用

    # ---------------------------------------------------------------- 收发
    def send(self, pkt: Packet) -> None:
        ui.log_hex("TX", self.client.send(pkt))

    def recv(self, timeout: Optional[float] = None,
             quiet: bool = False) -> Optional[Packet]:
        got = self.client.recv(timeout, quiet)
        if got is None:
            return None

        pkt, raw = got
        ui.log_hex("RX", raw)        # 先打原始字节，损坏的包也要留证据

        # 校验和不对说明线上有字节损坏。不校验的话会拿着错数据往下跑，
        # 而且症状会伪装成"设备返回了奇怪的值"，非常难查。
        if not pkt.checksum_ok:
            self.checksum_errors += 1
            ui.log(f"✗ 校验和错误: 字段 0x{pkt.checksum:04X} ≠ "
                   f"重算 0x{checksum_of(raw):04X}（线路有字节损坏）")
            return None

        return pkt

    def recv_reply(self, want_id: int, timeout: Optional[float] = None,
                   want_tag: bytes = TAG_LOCAL) -> Optional[Packet]:
        """读当前命令的应答，途中的异步通知记录下来并跳过。

        设备会主动推送不对应任何请求的通知（状态变化、OTA 版本），
        当成应答收下会让整条流永久错位。跳过的判据三条：

        1. Command ID 不匹配 —— 明显是别的命令的通知
        2. **TAG 不匹配** —— 应答带的 TAG 和请求一样，所以本地请求的应答
           不能拿对端的应答来顶。1 对多时对端们的应答会和本地应答挤在
           同一条串口上，只比 ID 就会认错
        3. ID 相同但内容是状态变化通知 —— NET_STA_CTRL 的应答和状态通知
           共用 0x4223，只能靠内容区分
        """
        for _ in range(MAX_REPLY_PKTS):
            pkt = self.recv(timeout)
            if pkt is None:
                return None

            if (pkt.cmd_id == want_id and pkt.tag == want_tag
                    and not pkt.is_state_notify):
                return pkt

            ui.log(f"  (跳过异步通知: {self._describe(pkt)})")

        ui.log(f"✗ 连续 {MAX_REPLY_PKTS} 个包都不是期望的应答 "
               f"0x{want_id:04X}[{tag_name(tag_value(want_tag))}]")
        return None

    def wait_optional(self, want_id: int, accept: Callable[[Packet], bool],
                      timeout: float,
                      want_tag: bytes = TAG_LOCAL) -> Optional[Packet]:
        """等一个可选的应答包。收不到返回 None，由调用方决定告警还是忽略。

        超时是正常结束条件，所以用 quiet 抑制底层的 ✗ 超时提示 ——
        那会把正常情况打印成错误，误导人。
        """
        for _ in range(MAX_REPLY_PKTS):
            pkt = self.recv(timeout, quiet=True)
            if pkt is None:
                return None

            if pkt.cmd_id == want_id and pkt.tag == want_tag and accept(pkt):
                return pkt

            ui.log(f"  (跳过不匹配的包: {self._describe(pkt)})")
        return None

    @staticmethod
    def _describe(pkt: Packet) -> str:
        """跳过包时的一行描述。TAG 一定要打 —— 光看 ID 分不出是本地还是
        对端的应答，而这两种混在一起正是最难查的错位来源。"""
        return (f"cmd=0x{pkt.cmd_id:04X}[{pkt.tag_name}] "
                f"len={len(pkt.data)}")

    # ---------------------------------------------------------------- 原语
    def step(self, cmd: int, data: bytes = b"",
             tag: int = Tag.TG) -> Packet:
        """发命令 → 读匹配的应答 → 查返回码。失败抛 TestFailed。

        测试名由 runner 打日志，这里只管收发和判定。

        :param tag: 默认本地执行。传 ``Tag.PL`` / ``Tag.PP`` 就是 Remote
            指令，应答按同一个 TAG 匹配。
        """
        pkt_out = build(cmd, data, tag=tag)
        self.send(pkt_out)

        pkt = self.recv_reply(resp_id_of(cmd), want_tag=pkt_out.tag)
        if pkt is None:
            raise TestFailed("没有收到应答")
        if not pkt.ok:
            raise TestFailed(f"rc={pkt.rc} {err_name(pkt.rc)}")

        return pkt

    def remote_step(self, cmd: int, data: bytes = b"",
                    to_mcu: bool = False) -> Packet:
        """Remote 指令：本地 AM 透过 WiFi 送给对端执行。

        :param to_mcu: False 对端 AM 执行；True 对端 AM 再下发给它的 MCU。
        """
        return self.step(cmd, data, tag=Tag.PP if to_mcu else Tag.PL)

    def warn(self, what: str) -> None:
        """打告警并停下来问。选中止就抛 Aborted。

        用于"命令执行了但可选的确认包没来"这类情况 —— 不算失败，
        但值得让人看一眼再决定。
        """
        self.warns += 1
        if self.on_warn:
            self.on_warn()
        if not ui.ask_continue(what):
            raise Aborted()

    # ---------------------------------------------------------------- 清理
    def forget_all(self, stage: str) -> None:
        """忘记所有网络，把设备拉回"未连接"状态。

        起跑前和每次重试前都要做：设备如果已经连在目标网络上，
        连接命令不产生状态变化，设备**完全不回应**，那一轮必然超时失败 ——
        不清理的话重试也是白重试。

        应答宽容处理：设备本来没连网络时只回 ack、不发断开通知，
        第 2 个包必然超时，那是正常的而不是故障。
        """
        ui.log(f"--- {stage}: 忘记所有网络 ---")
        self.send(build(Cmd.NET_STA_CTRL, bytes([STA_FORGET, 0xFF])))

        got = 0
        for _ in range(2):                     # 正常回 2 包: ack + 断开通知
            if self.recv(NOTIFY_TIMEOUT, quiet=True) is None:
                break
            got += 1

        self.client.flush_input()              # 丢掉可能迟到的多余通知

        if got == 0:
            ui.log(f"⚠ {stage}: 设备没有任何应答")
        else:
            ui.log(f"✓ {stage}完成，已忘记所有网络 (收到 {got} 个应答包)")


# ---------------------------------------------------------------- runner
def run_suite(ctx: Ctx, tests: list) -> dict:
    """按列表顺序跑测试集，返回 ``{测试名: TestRecord}``。

    某条测试失败后，依赖它的测试自动跳过而不是级联失败 —— 这样报告里
    能一眼看出真正的失败点在哪。

    Aborted 不在这里捕获，让它穿到顶层去（绕过重试）。
    """
    out: dict = {}

    for t in tests:
        blocked = [n for n in t.needs
                   if n not in out or out[n].result is not Result.PASS]

        if blocked:
            out[t.name] = TestRecord(t.name, Result.SKIP, tags=t.tags,
                                     error=f"依赖未通过: {'、'.join(blocked)}")
            ui.log(f"— 跳过 {t.name}（依赖未通过: {'、'.join(blocked)}）")
            continue

        ui.log(f"--- Cycle {ctx.cycle} · {t.name} ---")
        print(f"\n[{t.name}]")

        t0 = time.monotonic()
        try:
            t.run(ctx)
            dur = time.monotonic() - t0

            # 时延也是一种失败：设备还在回应、返回码也对，但从 5 秒
            # 变成 25 秒。只看通过/失败发现不了这种退化。
            if t.max_duration is not None and dur > t.max_duration:
                msg = f"耗时 {dur:.1f}s 超过上限 {t.max_duration:.1f}s"
                ui.log(f"✗ {t.name}: {msg}")
                out[t.name] = TestRecord(t.name, Result.FAIL, dur, msg, t.tags)
            else:
                out[t.name] = TestRecord(t.name, Result.PASS, dur, tags=t.tags)

        except TestFailed as e:
            ui.log(f"✗ {t.name}: {e}")
            out[t.name] = TestRecord(t.name, Result.FAIL,
                                     time.monotonic() - t0, str(e), t.tags)

    return out


def restore_baseline(ctx: Ctx, baseline: list) -> int:
    """把设备恢复到已知的基线状态。全部跑完后做一次。

    为什么需要：有些状态没有查询指令（区域码、频段、投屏），
    ``set_and_restore`` 读不回来，只能在收尾时统一设成已知值。
    否则跑完 ``--tags net`` 设备可能停在日本区、2.4G、P2P 模式上。

    **刻意宽容**：这是收尾清理，不是测试。设备已经挂了的时候恢复
    必然失败，那不该把已经通过的测试结果变成失败。
    返回成功恢复的条数。
    """
    if not baseline:
        return 0

    ui.log(f"--- 收尾: 恢复 {len(baseline)} 项基线状态 ---")
    ok = 0

    for name, cmd, data in baseline:
        try:
            ctx.step(cmd, data)
            ok += 1
        except TestFailed as e:
            ui.log(f"  ⚠ {name} 恢复失败: {e}")

    if ok == len(baseline):
        ui.log(f"✓ 基线已恢复 ({ok} 项)")
    else:
        ui.log(f"⚠ 基线部分恢复 ({ok}/{len(baseline)} 项)")

    return ok


def filter_tests(tests: list, tags: Optional[list]) -> list:
    """按 tag 筛选。

    被选中的测试如果依赖了没被选中的测试，那些依赖会**自动带上** ——
    否则依赖会被判成"未通过"导致整条链全跳过。
    """
    if not tags:
        return tests

    want = set(tags)
    keep = {t.name for t in tests if want & set(t.tags)}

    changed = True
    while changed:                              # 依赖可能是多层的
        changed = False
        for t in tests:
            if t.name in keep:
                for n in t.needs:
                    if n not in keep:
                        keep.add(n)
                        changed = True

    return [t for t in tests if t.name in keep]


def filter_by_setup(tests: list, setup) -> tuple:
    """按角色前提排除测试。返回 ``(保留的, [(名字, 原因), ...])``。

    **排除而不是跳过**：一台 Tx 在一对一里不连路由，"扫描网络"对它
    不适用 —— 这不是缺陷，不该出现在报告里。跳过会让人以为漏测了。

    依赖会跟着断：如果"获取扫描结果"被排除，依赖它的"连接网络"也留不住。
    所以排除之后要再收一遍，把依赖已经没了的一并去掉 —— 否则它们会
    因为"依赖未通过"被判成跳过，看起来像是出了问题。
    """
    if setup is None:
        return tests, []

    excluded = [(t.name, t.why_excluded(setup))
                for t in tests if not t.applies_to(setup)]
    keep = {t.name for t in tests if t.applies_to(setup)}

    changed = True
    while changed:
        changed = False
        for t in tests:
            if t.name in keep and any(n not in keep for n in t.needs):
                keep.discard(t.name)
                excluded.append((t.name, "依赖的测试已被角色排除"))
                changed = True

    return [t for t in tests if t.name in keep], excluded
