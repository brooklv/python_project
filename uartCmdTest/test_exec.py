"""执行机制的回归测试：测试工厂、依赖跳过、tag 筛选。

不需要 pyserial，也不需要接设备 —— 用假 client 喂应答。

    python3 -m unittest discover -v
    python3 test_exec.py
"""

import io
import struct
import time
import sys
import unittest
from contextlib import redirect_stdout

import uart_tests
from uart_exec import (
    Aborted, Ctx, Result, Test, TestFailed,
    filter_tests, query_hex, query_int, query_str, restore_baseline,
    run_suite, set_and_restore, set_and_verify, simple,
)
from uart_protocol import (
    Cmd, Err, Packet, TAG_LOCAL, Tag, resp_id_of, tag_bytes,
)


class FakeClient:
    """按预设队列回应答。

    :param replies: ``{请求的 (cmd_id, data): 应答 data}``；
                    也可以给 ``{cmd_id: data}`` 忽略请求内容
    :param log:     收到的请求会记在这里，供断言"发了什么"
    """

    def __init__(self, replies=None, default=None):
        self.replies = replies or {}
        self.default = default
        self.sent = []
        self._queue = []

    def flush_input(self):
        self._queue.clear()

    def send(self, pkt):
        self.sent.append((pkt.cmd_id, pkt.data))

        key = (pkt.cmd_id, pkt.data)
        data = self.replies.get(key)
        if data is None:
            data = self.replies.get(pkt.cmd_id, self.default)

        if data is not None:
            if not isinstance(data, list):
                data = [data]
            for d in data:
                # 应答带和请求一样的 TAG（spec 的 TAG 表里一个值同时是
                # request 和 ack 的标记）
                self._queue.append(
                    Packet(resp_id_of(pkt.cmd_id), d, tag=pkt.tag)
                    .pack(with_checksum=True))

        return pkt.pack()

    def recv(self, timeout=None, quiet=False):
        if not self._queue:
            return None
        raw = self._queue.pop(0)
        return Packet.unpack(raw), raw


def quiet_run(fn, *a, **kw):
    """跑一个会打日志的函数，把输出吞掉。"""
    with redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


OK = struct.pack("<h", Err.SUCCESS)


class TestSimple(unittest.TestCase):
    def test_pass_on_success(self):
        c = FakeClient(default=OK)
        quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), Ctx(client=c))
        self.assertEqual(c.sent, [(int(Cmd.AUDIO_MUTE), b"\x01")])

    def test_fail_on_error_rc(self):
        c = FakeClient(default=struct.pack("<h", Err.NOT_SUPPORT))
        with self.assertRaises(TestFailed) as e:
            quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), Ctx(client=c))
        self.assertIn("NOT_SUPPORT", str(e.exception))

    def test_fail_on_no_reply(self):
        c = FakeClient()                      # 什么都不回
        with self.assertRaises(TestFailed) as e:
            quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), Ctx(client=c))
        self.assertIn("没有收到应答", str(e.exception))


class TestSetAndVerify(unittest.TestCase):
    """设置 → 回读 → 断言。这是 simple() 抓不到的那类问题。"""

    def test_pass_when_readback_matches(self):
        c = FakeClient({
            (int(Cmd.ROTATION), b"\x01"): OK,          # 设置成功
            (int(Cmd.ROTATION), b""):     b"\x00\x00\x01",   # 回读 = 1
        })
        quiet_run(set_and_verify(Cmd.ROTATION, b"\x01", 1), Ctx(client=c))

        # 确认真的发了两条: 先设置后查询
        self.assertEqual(c.sent, [(int(Cmd.ROTATION), b"\x01"),
                                  (int(Cmd.ROTATION), b"")])

    def test_catches_silent_no_op(self):
        """**核心用例**: 设备回 rc=SUCCESS 但实际没生效。

        simple() 会放过这种情况，set_and_verify 必须抓住。
        """
        c = FakeClient({
            (int(Cmd.ROTATION), b"\x01"): OK,          # 说成功了
            (int(Cmd.ROTATION), b""):     b"\x00\x00\x00",   # 但回读还是 0
        })

        # simple() 放过
        quiet_run(simple(Cmd.ROTATION, b"\x01"), Ctx(client=FakeClient(default=OK)))

        # set_and_verify 抓住
        with self.assertRaises(TestFailed) as e:
            quiet_run(set_and_verify(Cmd.ROTATION, b"\x01", 1), Ctx(client=c))
        self.assertIn("设置了 1 但回读是 0", str(e.exception))

    def test_error_message_uses_names(self):
        c = FakeClient({
            (int(Cmd.ROTATION), b"\x01"): OK,
            (int(Cmd.ROTATION), b""):     b"\x00\x00\x03",
        })
        with self.assertRaises(TestFailed) as e:
            quiet_run(set_and_verify(Cmd.ROTATION, b"\x01", 1,
                                     names=uart_tests.ROTATION_ANGLE),
                      Ctx(client=c))
        self.assertIn("90度", str(e.exception))     # 期望值的含义
        self.assertIn("270度", str(e.exception))    # 实际值的含义

    def test_two_byte_value(self):
        """缩放比例是 2 字节小端: 00 00 78 00 = 120"""
        c = FakeClient({
            (int(Cmd.SET_OVERSCAN), b"\x78"): OK,
            (int(Cmd.SET_OVERSCAN), b""):     b"\x00\x00\x78\x00",
        })
        quiet_run(set_and_verify(Cmd.SET_OVERSCAN, b"\x78", 120, size=2),
                  Ctx(client=c))

    def test_fails_if_readback_too_short(self):
        c = FakeClient({
            (int(Cmd.ROTATION), b"\x01"): OK,
            (int(Cmd.ROTATION), b""):     b"\x00\x00",     # 没有值
        })
        with self.assertRaises(TestFailed) as e:
            quiet_run(set_and_verify(Cmd.ROTATION, b"\x01", 1), Ctx(client=c))
        self.assertIn("回读失败", str(e.exception))


class StatefulDev:
    """带真实状态的假设备：设置会改状态，查询会读回来。

    用来验证 set_and_restore 真的把状态放回去了 ——
    这是无状态的 FakeClient 验证不了的。
    """

    def __init__(self, state=None):
        self.state = dict(state or {})
        self._q = []

    def flush_input(self):
        self._q.clear()

    def send(self, pkt):
        c, d = pkt.cmd_id, pkt.data
        if d:                                   # 设置
            self.state[c] = d[0]
            self._q.append(Packet(resp_id_of(c), OK).pack(with_checksum=True))
        else:                                   # 查询
            v = self.state.get(c, 0)
            self._q.append(
                Packet(resp_id_of(c), b"\x00\x00" + bytes([v])
                       ).pack(with_checksum=True))
        return pkt.pack()

    def recv(self, timeout=None, quiet=False):
        if not self._q:
            return None
        raw = self._q.pop(0)
        return Packet.unpack(raw), raw


class TestSetAndRestore(unittest.TestCase):
    """测试隔离：跑完把状态放回去，别污染后续测试。"""

    def test_restores_original(self):
        dev = StatefulDev({int(Cmd.NET_ROLE): 1})       # 原本是三合一
        quiet_run(set_and_restore(Cmd.NET_ROLE, b"\x00", 0), Ctx(client=dev))

        self.assertEqual(dev.state[int(Cmd.NET_ROLE)], 1,
                         "跑完必须恢复成原来的 1，否则会毒害后续 WiFi 测试")

    def test_set_and_verify_does_leak(self):
        """对照：set_and_verify 不恢复，状态会留下来。"""
        dev = StatefulDev({int(Cmd.NET_ROLE): 1})
        quiet_run(set_and_verify(Cmd.NET_ROLE, b"\x00", 0), Ctx(client=dev))
        self.assertEqual(dev.state[int(Cmd.NET_ROLE)], 0)

    def test_no_restore_needed_when_already_equal(self):
        """原值就等于目标值时不用多发一条恢复命令。"""
        dev = StatefulDev({int(Cmd.NET_ROLE): 0})
        quiet_run(set_and_restore(Cmd.NET_ROLE, b"\x00", 0), Ctx(client=dev))
        self.assertEqual(dev.state[int(Cmd.NET_ROLE)], 0)

    def test_restores_even_when_assertion_fails(self):
        """回读不符时也要先把状态放回去，别把设备留在半途。"""
        dev = StatefulDev({int(Cmd.ROTATION): 1})
        with self.assertRaises(TestFailed):
            # 设 3 但断言期望 2 —— 必然失败
            quiet_run(set_and_restore(Cmd.ROTATION, b"\x03", 2),
                      Ctx(client=dev))
        self.assertEqual(dev.state[int(Cmd.ROTATION)], 1)

    def test_fails_if_cannot_read_original(self):
        c = FakeClient({int(Cmd.ROTATION): b"\x00\x00"})    # 读不到值
        with self.assertRaises(TestFailed) as e:
            quiet_run(set_and_restore(Cmd.ROTATION, b"\x01", 1), Ctx(client=c))
        self.assertIn("读取原值失败", str(e.exception))


class TestRestoreBaseline(unittest.TestCase):
    """收尾恢复：覆盖那些没有查询指令、set_and_restore 读不回来的状态。"""

    def test_sets_all(self):
        dev = StatefulDev()
        n = quiet_run(restore_baseline, Ctx(client=dev), [
            ("区域码", Cmd.NET_AP_PARAM, b"\x06"),
            ("HDMI", Cmd.HDMI_ENABLE, b"\x01"),
        ])
        self.assertEqual(n, 2)
        self.assertEqual(dev.state[int(Cmd.HDMI_ENABLE)], 1)

    def test_lenient_on_failure(self):
        """设备挂了时恢复会失败，但不能因此抛异常 ——
        那会把已经通过的测试结果变成失败。"""
        c = FakeClient()                        # 什么都不回
        n = quiet_run(restore_baseline, Ctx(client=c), [
            ("a", Cmd.HDMI_ENABLE, b"\x01"),
            ("b", Cmd.ROTATION, b"\x00"),
        ])
        self.assertEqual(n, 0)                  # 一条都没成，但没抛

    def test_empty_baseline_is_noop(self):
        self.assertEqual(quiet_run(restore_baseline,
                                   Ctx(client=FakeClient()), []), 0)


class TestBaselineDefinition(unittest.TestCase):
    def test_covers_states_without_query(self):
        """没有查询指令的状态必须被基线覆盖，否则没法恢复。"""
        covered = {c for _n, c, _d in uart_tests.BASELINE}
        for cmd in (Cmd.NET_AP_PARAM, Cmd.SWITCH_WIFI_CHN, Cmd.SCREEN_CAST):
            self.assertIn(cmd, covered,
                          f"{cmd.name} 没有查询指令，必须列进 BASELINE")

    def test_entries_wellformed(self):
        for entry in uart_tests.BASELINE:
            self.assertEqual(len(entry), 3, f"BASELINE 项应为 (名字, cmd, data): {entry}")
            name, cmd, data = entry
            self.assertIsInstance(name, str)
            self.assertIsInstance(data, bytes)
            self.assertGreater(len(data), 0)


class TestQueryFactories(unittest.TestCase):
    def test_query_int_offset(self):
        """配对状态的值在 rc 之后偏移 1: 00 00 | 00 | 03"""
        c = FakeClient({int(Cmd.PAIRING_CTRL): b"\x00\x00\x00\x03"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            query_int(Cmd.PAIRING_CTRL, offset=1,
                      names=uart_tests.WPS_STATUS)(Ctx(client=c))
        self.assertIn("3", buf.getvalue())
        self.assertIn("配对超时", buf.getvalue())

    def test_query_str(self):
        c = FakeClient({int(Cmd.FW_VERSION): b"\x00\x00" + b"27243000\x00\x00"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            query_str(Cmd.FW_VERSION)(Ctx(client=c))
        self.assertIn("27243000", buf.getvalue())

    def test_query_fails_on_empty_payload(self):
        c = FakeClient({int(Cmd.FW_VERSION): b"\x00\x00"})
        with self.assertRaises(TestFailed):
            quiet_run(query_str(Cmd.FW_VERSION), Ctx(client=c))


class TestQueryAssertions(unittest.TestCase):
    """给查询加期望值 —— 不加断言的话它只是观测不是测试。"""

    def _q(self, factory, payload):
        c = FakeClient({int(Cmd.ROTATION): b"\x00\x00" + payload})
        return lambda: quiet_run(factory, Ctx(client=c))

    # ---------------------------------------------------------- allowed
    def test_allowed_accepts_valid(self):
        self._q(query_int(Cmd.ROTATION, names=uart_tests.ROTATION_ANGLE,
                          allowed=True), b"\x03")()

    def test_allowed_rejects_out_of_enum(self):
        """设备返回枚举外的值 —— 常见于流错位或固件不匹配。"""
        with self.assertRaises(TestFailed) as e:
            self._q(query_int(Cmd.ROTATION, names=uart_tests.ROTATION_ANGLE,
                              allowed=True), b"\x09")()
        self.assertIn("不在合法值", str(e.exception))

    def test_allowed_true_requires_names(self):
        with self.assertRaises(ValueError):
            query_int(Cmd.ROTATION, allowed=True)

    def test_allowed_explicit_set(self):
        with self.assertRaises(TestFailed):
            self._q(query_int(Cmd.ROTATION, allowed=(0, 1)), b"\x05")()

    # ---------------------------------------------------------- 范围
    def test_range_ok(self):
        self._q(query_int(Cmd.ROTATION, lo=0, hi=100), b"\x07")()

    def test_below_lo(self):
        with self.assertRaises(TestFailed) as e:
            self._q(query_int(Cmd.ROTATION, size=2, lo=50), b"\x0A\x00")()
        self.assertIn("低于下界", str(e.exception))

    def test_above_hi(self):
        with self.assertRaises(TestFailed) as e:
            self._q(query_int(Cmd.ROTATION, hi=15), b"\xFF")()
        self.assertIn("高于上界", str(e.exception))

    def test_expect_exact(self):
        self._q(query_int(Cmd.ROTATION, expect=3), b"\x03")()
        with self.assertRaises(TestFailed) as e:
            self._q(query_int(Cmd.ROTATION, expect=3), b"\x02")()
        self.assertIn("期望 3", str(e.exception))

    # ---------------------------------------------------------- 字符串格式
    def _s(self, factory, text):
        c = FakeClient({int(Cmd.WIFI_MAC_ADDR): b"\x00\x00" + text})
        return lambda: quiet_run(factory, Ctx(client=c))

    def test_mac_pattern_ok(self):
        self._s(query_str(Cmd.WIFI_MAC_ADDR, pattern=uart_tests.RE_MAC),
                b"FC:19:28:36:95:69")()

    def test_mac_pattern_rejects_garbage(self):
        """流错位时设备"回"的可能是别的命令的数据，格式一卡就露。"""
        for bad in (b"27243000", b"FC:19:28", b"not-a-mac", b"FC-19-28-36-95-69"):
            with self.subTest(bad=bad):
                with self.assertRaises(TestFailed) as e:
                    self._s(query_str(Cmd.WIFI_MAC_ADDR,
                                      pattern=uart_tests.RE_MAC), bad)()
                self.assertIn("不符合格式", str(e.exception))

    def test_version_pattern(self):
        self._s(query_str(Cmd.WIFI_MAC_ADDR, pattern=uart_tests.RE_VERSION),
                b"27243000")()
        with self.assertRaises(TestFailed):
            self._s(query_str(Cmd.WIFI_MAC_ADDR,
                              pattern=uart_tests.RE_VERSION), b"v1.2.3")()

    def test_min_len(self):
        self._s(query_str(Cmd.WIFI_MAC_ADDR, min_len=8), b"12345678")()
        with self.assertRaises(TestFailed) as e:
            self._s(query_str(Cmd.WIFI_MAC_ADDR, min_len=8), b"1234")()
        self.assertIn("短于 8", str(e.exception))

    def test_expect_exact_str(self):
        with self.assertRaises(TestFailed) as e:
            self._s(query_str(Cmd.WIFI_MAC_ADDR, expect="AA"), b"BB")()
        self.assertIn("期望 'AA'", str(e.exception))

    # ---------------------------------------------------------- hex 长度
    def test_hex_expect_len(self):
        c = FakeClient({int(Cmd.ROTATION): b"\x00\x00\x01\x02\x03\x04"})
        quiet_run(query_hex(Cmd.ROTATION, expect_len=4), Ctx(client=c))

        c = FakeClient({int(Cmd.ROTATION): b"\x00\x00\x01\x02"})
        with self.assertRaises(TestFailed) as e:
            quiet_run(query_hex(Cmd.ROTATION, expect_len=4), Ctx(client=c))
        self.assertIn("期望 4", str(e.exception))


class TestAsyncNotificationSkipping(unittest.TestCase):
    """设备主动推的通知必须跳过，否则整条流永久错位。"""

    def test_skips_state_notify(self):
        from uart_protocol import STATE_NOTIFY_DATA
        c = FakeClient({
            int(Cmd.NET_STA_CTRL): [STATE_NOTIFY_DATA, OK],   # 先来个通知
        })
        pkt = quiet_run(Ctx(client=c).step, Cmd.NET_STA_CTRL, b"\x00")
        self.assertEqual(pkt.rc, Err.SUCCESS)      # 拿到的是真应答不是通知

    def test_skips_wrong_cmd_id(self):
        """OTA 版本推送的 cmd_id 不匹配，要跳过。"""
        c = FakeClient()
        c.send(Packet(Cmd.NET_STA_CTRL, b"\x00"))
        c._queue = [
            Packet(resp_id_of(Cmd.FW_UPGRADE_CTRL),
                   b"\x00\x00ver").pack(with_checksum=True),
            Packet(resp_id_of(Cmd.NET_STA_CTRL), OK).pack(with_checksum=True),
        ]
        ctx = Ctx(client=c)
        pkt = quiet_run(ctx.recv_reply, resp_id_of(Cmd.NET_STA_CTRL))
        self.assertEqual(pkt.rc, Err.SUCCESS)


class TestRunSuite(unittest.TestCase):
    def _suite(self, results_map):
        """按 {测试名: 是否通过} 造一组测试。"""
        def mk(name, ok):
            def run(ctx):
                if not ok:
                    raise TestFailed("故意失败")
            return run
        return [Test(n, mk(n, ok)) for n, ok in results_map.items()]

    def test_all_pass(self):
        tests = self._suite({"a": True, "b": True})
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)
        self.assertEqual([rec.result for rec in r.values()],
                         [Result.PASS, Result.PASS])

    def test_records_duration_and_error(self):
        tests = [Test("bad", self._suite({"bad": False})[0].run)]
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)
        self.assertEqual(r["bad"].error, "故意失败")
        self.assertGreaterEqual(r["bad"].duration, 0.0)

    def test_records_tags(self):
        tests = [Test("x", lambda c: None, tags=("audio", "q"))]
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)
        self.assertEqual(r["x"].tags, ("audio", "q"))

    def test_dependent_is_skipped_not_failed(self):
        """依赖失败时下游是 SKIP 而不是 FAIL —— 这样报告能指出真正的失败点。"""
        tests = [
            Test("a", self._suite({"a": False})[0].run),
            Test("b", self._suite({"b": True})[0].run, needs=("a",)),
            Test("c", self._suite({"c": True})[0].run, needs=("b",)),
            Test("d", self._suite({"d": True})[0].run),      # 不依赖 a
        ]
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)

        self.assertEqual(r["a"].result, Result.FAIL)
        self.assertEqual(r["b"].result, Result.SKIP)
        self.assertEqual(r["c"].result, Result.SKIP)   # 多层依赖也要跳
        self.assertEqual(r["d"].result, Result.PASS)   # 无关的照跑
        self.assertIn("依赖未通过", r["b"].error)

    def test_abort_propagates(self):
        """Aborted 不能被 runner 吞掉 —— 否则重试机制会把中止重跑一遍。"""
        def boom(ctx):
            raise Aborted()

        with self.assertRaises(Aborted):
            quiet_run(run_suite, Ctx(client=FakeClient()), [Test("x", boom)])


class TestTagMatching(unittest.TestCase):
    """应答按 (Command ID, TAG) 两者匹配。

    1 对多时对端们的应答会和本地应答挤在同一条串口上，只比 ID 就会
    把对端的应答当成本地的 —— 那之后每一步都在读上一条的应答。
    """

    class TagDev:
        """按指定的 TAG 列表依次回应答，Command ID 一律匹配。"""

        def __init__(self, tags):
            self.tags = list(tags)
            self._q = []

        def flush_input(self):
            self._q.clear()

        def send(self, pkt):
            for t in self.tags:
                self._q.append(
                    Packet(resp_id_of(pkt.cmd_id), OK, tag=tag_bytes(t))
                    .pack(with_checksum=True))
            return pkt.pack()

        def recv(self, timeout=None, quiet=False):
            if not self._q:
                return None
            raw = self._q.pop(0)
            return Packet.unpack(raw), raw

    def test_local_request_ignores_remote_ack(self):
        """只有对端应答时，本地请求必须超时失败，不能拿它当成功。"""
        ctx = Ctx(client=self.TagDev([Tag.PL, Tag.PP]))
        with self.assertRaises(TestFailed):
            quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), ctx)

    def test_local_ack_found_after_remote_ones(self):
        """对端应答先到也不该乱 —— 跳过它们，继续等本地的。"""
        ctx = Ctx(client=self.TagDev([Tag.PL, Tag.PP, Tag.TG]))
        quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), ctx)

    def test_remote_request_matches_remote_ack(self):
        ctx = Ctx(client=self.TagDev([Tag.PL]))
        pkt = quiet_run(ctx.remote_step, Cmd.AUDIO_MUTE, b"\x01")
        self.assertEqual(pkt.tag, tag_bytes(Tag.PL))

    def test_remote_request_ignores_local_ack(self):
        """反向也要成立：发给对端的命令不能被本地 AM 的应答顶掉。"""
        ctx = Ctx(client=self.TagDev([Tag.TG]))
        with self.assertRaises(TestFailed):
            quiet_run(ctx.remote_step, Cmd.AUDIO_MUTE, b"\x01")

    def test_remote_to_mcu_uses_pp(self):
        c = FakeClient(default=OK)
        ctx = Ctx(client=c)
        pkt = quiet_run(ctx.remote_step, Cmd.AUDIO_MUTE, b"\x01", to_mcu=True)
        self.assertEqual(pkt.tag, tag_bytes(Tag.PP))

    def test_step_defaults_to_local(self):
        c = FakeClient(default=OK)
        pkt = quiet_run(Ctx(client=c).step, Cmd.AUDIO_MUTE, b"\x01")
        self.assertEqual(pkt.tag, TAG_LOCAL)


class TestChecksumVerification(unittest.TestCase):
    """接收方向的校验和校验。

    不校验的话会拿着错数据往下跑，而且症状会伪装成"设备返回了奇怪的值"，
    非常难查 —— 所以这一层必须在 Ctx.recv 里拦住。
    """

    class CorruptDev:
        """回一个校验和不对的包（模拟线上字节损坏）。"""

        def __init__(self, corrupt=True):
            self.corrupt = corrupt
            self._q = []

        def flush_input(self):
            self._q.clear()

        def send(self, pkt):
            raw = bytearray(Packet(resp_id_of(pkt.cmd_id), OK)
                            .pack(with_checksum=True))
            if self.corrupt:
                raw[-1] ^= 0x01          # 翻一个数据位，校验和就对不上
            self._q.append(bytes(raw))
            return pkt.pack()

        def recv(self, timeout=None, quiet=False):
            if not self._q:
                return None
            raw = self._q.pop(0)
            return Packet.unpack(raw), raw

    def test_corrupt_packet_rejected(self):
        ctx = Ctx(client=self.CorruptDev(corrupt=True))
        with self.assertRaises(TestFailed):
            quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), ctx)
        self.assertEqual(ctx.checksum_errors, 1)

    def test_good_packet_passes(self):
        ctx = Ctx(client=self.CorruptDev(corrupt=False))
        quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), ctx)
        self.assertEqual(ctx.checksum_errors, 0)

    def test_error_counted_per_packet(self):
        ctx = Ctx(client=self.CorruptDev(corrupt=True))
        for _ in range(3):
            with self.assertRaises(TestFailed):
                quiet_run(simple(Cmd.AUDIO_MUTE, b"\x01"), ctx)
        self.assertEqual(ctx.checksum_errors, 3)


class TestMaxDuration(unittest.TestCase):
    """时延也是一种失败：设备还在回应、返回码也对，但慢了 5 倍。"""

    def test_slow_test_fails(self):
        def slow(ctx):
            time.sleep(0.05)

        tests = [Test("slow", slow, max_duration=0.01)]
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)

        self.assertEqual(r["slow"].result, Result.FAIL)
        self.assertIn("超过上限", r["slow"].error)

    def test_fast_test_passes(self):
        tests = [Test("fast", lambda c: None, max_duration=5.0)]
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)
        self.assertEqual(r["fast"].result, Result.PASS)

    def test_no_limit_means_no_check(self):
        def slow(ctx):
            time.sleep(0.05)

        tests = [Test("slow", slow)]        # 没给 max_duration
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)
        self.assertEqual(r["slow"].result, Result.PASS)

    def test_functional_failure_takes_precedence(self):
        """功能失败时报功能原因，不要被时延盖掉。"""
        def boom(ctx):
            raise TestFailed("真正的原因")

        tests = [Test("x", boom, max_duration=0.0001)]
        r = quiet_run(run_suite, Ctx(client=FakeClient()), tests)
        self.assertEqual(r["x"].error, "真正的原因")

    def test_core_tests_have_limits(self):
        """WiFi 主流程必须都有时延上限 —— 老化测试的性能退化靠它抓。"""
        core = filter_tests(uart_tests.TESTS, ["core"])
        for t in core:
            self.assertIsNotNone(t.max_duration,
                                 f"{t.name} 没设 max_duration")


class TestFilterTests(unittest.TestCase):
    def test_by_tag(self):
        sel = filter_tests(uart_tests.TESTS, ["audio"])
        self.assertTrue(all("audio" in t.tags for t in sel))
        self.assertGreater(len(sel), 0)

    def test_union_of_tags(self):
        a = len(filter_tests(uart_tests.TESTS, ["audio"]))
        n = len(filter_tests(uart_tests.TESTS, ["net"]))
        both = len(filter_tests(uart_tests.TESTS, ["audio", "net"]))
        self.assertGreaterEqual(both, max(a, n))

    def test_dependencies_pulled_in(self):
        """选中的测试依赖了没选中的，依赖要自动带上 ——
        否则依赖会被判成"未通过"导致整条链全跳过。"""
        tests = [
            Test("dep", lambda c: None, tags=("other",)),
            Test("main", lambda c: None, needs=("dep",), tags=("want",)),
        ]
        sel = filter_tests(tests, ["want"])
        self.assertEqual([t.name for t in sel], ["dep", "main"])

    def test_multilevel_dependencies(self):
        tests = [
            Test("a", lambda c: None, tags=("x",)),
            Test("b", lambda c: None, needs=("a",), tags=("x",)),
            Test("c", lambda c: None, needs=("b",), tags=("want",)),
        ]
        sel = filter_tests(tests, ["want"])
        self.assertEqual([t.name for t in sel], ["a", "b", "c"])

    def test_no_tags_returns_all(self):
        self.assertEqual(len(filter_tests(uart_tests.TESTS, None)),
                         len(uart_tests.TESTS))


class TestSuiteDefinition(unittest.TestCase):
    """测试集本身的自洽性检查。"""

    def test_names_unique(self):
        names = [t.name for t in uart_tests.TESTS]
        dupes = {n for n in names if names.count(n) > 1}
        self.assertEqual(dupes, set(), f"测试名重复: {dupes}")

    def test_needs_reference_existing_tests(self):
        names = {t.name for t in uart_tests.TESTS}
        for t in uart_tests.TESTS:
            for n in t.needs:
                self.assertIn(n, names, f"{t.name} 依赖了不存在的 {n}")

    def test_needs_declared_before_use(self):
        """依赖必须排在前面，否则 runner 按顺序跑时会把它判成未通过。"""
        seen = set()
        for t in uart_tests.TESTS:
            for n in t.needs:
                self.assertIn(n, seen, f"{t.name} 的依赖 {n} 排在它后面")
            seen.add(t.name)

    def test_danger_tests_are_isolated(self):
        """danger 测试不能带别的 tag，否则会被 --tags display 之类误触。"""
        for t in uart_tests.TESTS:
            if "danger" in t.tags:
                self.assertEqual(
                    set(t.tags), {"danger"},
                    f"{t.name} 除 danger 外还带了 {set(t.tags) - {'danger'}}，"
                    f"可能被误触发")

    def test_default_tags_select_something(self):
        sel = filter_tests(uart_tests.TESTS, uart_tests.DEFAULT_TAGS)
        self.assertGreater(len(sel), 0)

    def test_all_exclusive_tags_are_isolated(self):
        """DANGER_TAGS 里的每个 tag 都必须是独占的。

        一身兼两职的 tag 是真踩过的坑（wifi 既表示分类又表示"要凭据"）。
        这里对所有需要显式点名的 tag 统一卡住，加新的也自动被覆盖。
        """
        exclusive = set(uart_tests.DANGER_TAGS)
        for t in uart_tests.TESTS:
            extra = set(t.tags) & exclusive
            if extra:
                self.assertEqual(
                    set(t.tags), extra,
                    f"{t.name} 带了独占 tag {extra}，却还有 "
                    f"{set(t.tags) - extra} —— 会被别的 --tags 误触发")

    def test_logmode_never_reachable_by_other_tags(self):
        """打开 log 后设备不再回应任何命令，整轮测试就地死掉。

        所以除了显式 --tags logmode，任何 tag 都不能把它们拉进来 ——
        包括依赖自动展开这条路径。
        """
        for tag in uart_tests.all_tags():
            if tag == "logmode":
                continue
            hit = [t.name for t in filter_tests(uart_tests.TESTS, [tag])
                   if "logmode" in t.tags]
            self.assertEqual(hit, [], f"--tags {tag} 会跑到 log 设置: {hit}")

    def test_baseline_never_enables_log(self):
        """基线里绝不能出现 log 设置。

        原来有一条"恢复成 log 全开"，等于每轮收尾都把命令通道弄死 ——
        而且是在所有测试都跑完之后，报告一切正常，设备已经失联。
        """
        for name, cmd, _data in uart_tests.BASELINE:
            self.assertNotIn(
                int(cmd), (int(Cmd.SET_LOG_STATUS), int(Cmd.NULL_CONSOLE)),
                f"基线项 {name!r} 在设置 log")

    def test_baseline_data_is_explicit_bytes(self):
        """基线的 data 必须是实际字节，不能是空的。

        这张表全是控制字节，历史上被写成过裸 0x03（编辑器里看不见），
        也被写成过空 data —— 后者会把"设置"悄悄变成"查询"，
        恢复根本没发生，而日志照样报成功。
        """
        for name, _cmd, data in uart_tests.BASELINE:
            self.assertGreater(len(data), 0, f"基线项 {name!r} 的 data 是空的")

    def test_only_connect_needs_creds(self):
        """needs_creds 要按实际用途标，不能靠 tag 推断。"""
        need = [t.name for t in uart_tests.TESTS if t.needs_creds]
        self.assertEqual(need, ["连接网络"])

    def test_default_run_does_not_include_danger(self):
        sel = filter_tests(uart_tests.TESTS, uart_tests.DEFAULT_TAGS)
        for t in sel:
            self.assertNotIn("danger", t.tags,
                             f"默认组里混进了危险测试 {t.name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
