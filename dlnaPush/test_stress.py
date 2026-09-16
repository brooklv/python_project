"""老化 / 压力测试的回归测试。不需要真设备，也不会真的等 60 秒。

    python3 -m unittest test_stress -v

这里卡三件**错了就整轮白跑**的事：

* **探活分类**。收到 HTTP 500 说明设备进程明明活着，判成崩溃就是误报；
  连接被拒判成"网络抖动"就是漏报。两个方向都会让结论毫无价值。
* **音量必须交替**。连发同一个值会被设备的 same-volume 检查全部拦掉，
  一次都到不了事件链路，测了等于没测 —— 而日志上每条都是"发送成功"。
* **退出码的两套语义**。老化跑通是 0，崩溃复现成功也是 0，方向正好相反。
"""

import contextlib
import io as _io
import socket
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import dlna_push
import dlna_soap as soap
import dlna_stress as st
from dlna_device import Renderer, parse_description
from dlna_stress import (
    CrashDetector, Probe, StressConfig, StressResult, classify, run_stress,
)

AV = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"


def make_renderer(base="http://127.0.0.1:1", rc_event="") -> Renderer:
    return Renderer(udn="uuid:t", friendly="假电视", location=f"{base}/dd.xml",
                    base=base, av_type=AV, av_control=f"{base}/av",
                    rc_type=RC, rc_control=f"{base}/rc", rc_event=rc_event)


def soap_error(cause) -> soap.SoapError:
    return soap.SoapError("boom", cause=cause)


class TestClassify(unittest.TestCase):

    def test_http_error_means_alive(self):
        """有 HTTP 响应 = 进程还在。判成崩溃就是误报。"""
        err = urllib.error.HTTPError("http://h/rc", 500, "err", {}, None)
        self.assertIs(classify(soap_error(err)), Probe.HTTP_ERR)
        self.assertTrue(classify(soap_error(err)).alive)

    def test_refused_is_unwrapped_from_urlerror(self):
        """urllib 把 OSError 包进 URLError；不剥这层，所有失败都落到 OTHER。"""
        wrapped = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        self.assertIs(classify(soap_error(wrapped)), Probe.REFUSED)

    def test_reset_is_refused_class(self):
        wrapped = urllib.error.URLError(ConnectionResetError(104, "reset"))
        self.assertIs(classify(soap_error(wrapped)), Probe.REFUSED)

    def test_timeout(self):
        wrapped = urllib.error.URLError(socket.timeout("timed out"))
        self.assertIs(classify(soap_error(wrapped)), Probe.TIMEOUT)

    def test_bare_oserror_is_other(self):
        self.assertIs(classify(soap_error(OSError("no route to host"))),
                      Probe.OTHER)

    def test_none_cause_is_other(self):
        self.assertIs(classify(soap_error(None)), Probe.OTHER)

    def test_only_ok_and_http_err_count_as_alive(self):
        alive = {p for p in Probe if p.alive}
        self.assertEqual(alive, {Probe.OK, Probe.HTTP_ERR})


class TestConfig(unittest.TestCase):

    def test_same_volume_is_rejected(self):
        """相同音量会被设备拦掉，跑完全程一条都不会到事件链路。"""
        with self.assertRaises(ValueError) as cm:
            StressConfig(vol_a=20, vol_b=20).validate()
        self.assertIn("same-volume", str(cm.exception))

    def test_defaults_match_the_old_repro_script(self):
        cfg = StressConfig()
        self.assertEqual((cfg.vol_a, cfg.vol_b), (10, 21))
        self.assertEqual(cfg.burst, 1)
        self.assertAlmostEqual(cfg.burst_gap, 0.13)
        self.assertAlmostEqual(cfg.interval, 0.5)
        self.assertEqual(cfg.max_rounds, 60)
        self.assertEqual(cfg.confirm, 3)

    def test_bad_counts_are_rejected(self):
        for kw in ({"max_rounds": 0}, {"burst": 0}):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    StressConfig(**kw).validate()


class ProbeStub:
    """按脚本喂探活结果，用来把 60 秒的等待压成毫秒。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, _rend, timeout=2.0):
        self.calls += 1
        kind = self.script.pop(0) if self.script else Probe.OK
        return kind, "stub"


class DetectorTestBase(unittest.TestCase):

    def use(self, script):
        stub = ProbeStub(script)
        self._orig = st.probe_alive
        st.probe_alive = stub
        self.addCleanup(lambda: setattr(st, "probe_alive", self._orig))
        return stub


class TestCrashDetector(DetectorTestBase):

    def det(self, **kw):
        kw.setdefault("confirm_gap", 0.0)
        kw.setdefault("recover_wait", 0.05)
        return CrashDetector(make_renderer(), log=lambda _m: None, **kw)

    def test_alive_returns_none_immediately(self):
        stub = self.use([Probe.OK])
        self.assertIsNone(self.det(confirm=3).check("ctx"))
        self.assertEqual(stub.calls, 1)      # 活着就不该再探两次

    def test_http_error_is_alive_not_a_crash(self):
        """设备回 500 说明进程在跑，只是这条动作被拒 —— 不是崩溃。"""
        self.use([Probe.HTTP_ERR])
        self.assertIsNone(self.det(confirm=3).check("ctx"))

    def test_single_failure_does_not_trip(self):
        """一次失败就报崩溃 = 把网络抖动当成设备死了。"""
        self.use([Probe.TIMEOUT, Probe.OK])
        self.assertIsNone(self.det(confirm=3).check("ctx"))

    def test_consecutive_failures_trip(self):
        self.use([Probe.REFUSED] * 3 + [Probe.REFUSED] * 50)
        result = self.det(confirm=3).check("ctx")
        self.assertIsNotNone(result)
        self.assertIn("连接被拒", result[0])
        self.assertIsNone(result[1])         # 观察窗口内没复活

    def test_recovery_is_still_a_crash(self):
        """死过又活 = actui 被 init 拉起来了，这是崩溃的证据，不是没崩。"""
        self.use([Probe.REFUSED] * 3 + [Probe.OK])
        cause, downtime = self.det(confirm=3, recover_wait=5.0).check("ctx")
        self.assertIn("连接被拒", cause)
        self.assertIsNotNone(downtime)

    def test_timeout_only_gets_its_own_cause(self):
        self.use([Probe.TIMEOUT] * 3 + [Probe.TIMEOUT] * 50)
        cause, _ = self.det(confirm=3).check("ctx")
        self.assertIn("超时", cause)


class VolumeRecorder:
    """记录 set_volume 的调用序列，可选在第 N 次之后开始抛异常。"""

    def __init__(self, die_after=None, exc=None):
        self.seen = []
        self.die_after = die_after
        self.exc = exc or soap_error(
            urllib.error.URLError(ConnectionRefusedError(111, "refused")))

    def __call__(self, _rend, vol):
        self.seen.append(vol)
        if self.die_after is not None and len(self.seen) > self.die_after:
            raise self.exc
        return vol


class TestRunStress(DetectorTestBase):

    def setUp(self):
        self.vol = VolumeRecorder()
        orig = soap.set_volume
        soap.set_volume = self.vol
        self.addCleanup(lambda: setattr(soap, "set_volume", orig))

    def cfg(self, **kw):
        kw.setdefault("burst_gap", 0.0)
        kw.setdefault("interval", 0.0)
        kw.setdefault("max_rounds", 3)
        return StressConfig(**kw)

    def run_it(self, cfg):
        return run_stress(make_renderer(), cfg, "127.0.0.1", log=lambda _m: None)

    def test_volumes_alternate_within_a_burst(self):
        """组内不换值就会被 same-volume 拦掉 —— 这是整个测试的前提。"""
        self.use([Probe.OK] * 50)
        self.run_it(self.cfg(vol_a=10, vol_b=21, burst=4, max_rounds=1))
        self.assertEqual(self.vol.seen, [10, 21, 10, 21])

    def test_alternation_continues_across_rounds(self):
        self.use([Probe.OK] * 50)
        self.run_it(self.cfg(vol_a=10, vol_b=21, burst=1, max_rounds=4))
        self.assertEqual(self.vol.seen, [10, 21, 10, 21])

    def test_clean_run_reports_no_crash(self):
        self.use([Probe.OK] * 50)
        res = self.run_it(self.cfg(burst=2, max_rounds=3))
        self.assertFalse(res.crashed)
        self.assertEqual(res.rounds, 3)
        self.assertEqual(res.sent_ok, 6)
        self.assertEqual(res.send_fail, 0)

    def test_crash_detected_between_rounds_stops_early(self):
        self.use([Probe.OK, Probe.REFUSED, Probe.REFUSED, Probe.REFUSED]
                 + [Probe.REFUSED] * 50)
        res = self.run_it(self.cfg(max_rounds=10, confirm=3, recover_wait=0.05))
        self.assertTrue(res.crashed)
        self.assertLess(res.rounds, 10)      # 崩了就该停，不是跑满
        self.assertIn("连接被拒", res.cause)

    def test_send_failure_with_device_alive_keeps_going(self):
        """发送异常但设备探活正常 = 网络抖动，不该判成崩溃收工。"""
        self.vol.die_after = 1
        self.use([Probe.OK] * 50)
        res = self.run_it(self.cfg(burst=1, max_rounds=3))
        self.assertFalse(res.crashed)
        self.assertEqual(res.send_fail, 2)
        self.assertEqual(res.rounds, 3)

    def test_send_failure_with_device_dead_is_a_crash(self):
        self.vol.die_after = 1
        self.use([Probe.OK, Probe.REFUSED, Probe.REFUSED, Probe.REFUSED]
                 + [Probe.REFUSED] * 50)
        res = self.run_it(self.cfg(burst=1, max_rounds=5, confirm=3,
                                   recover_wait=0.05))
        self.assertTrue(res.crashed)

    def test_initial_probe_failure_aborts(self):
        """开跑前设备就不通，继续跑只会得到一份没有意义的日志。"""
        self.use([Probe.REFUSED])
        with self.assertRaises(RuntimeError) as cm:
            self.run_it(self.cfg())
        self.assertIn("初始探活失败", str(cm.exception))
        self.assertEqual(self.vol.seen, [])


class TestGenaSubscribe(unittest.TestCase):
    """真起一个假设备接 SUBSCRIBE，再真发一条 NOTIFY 回来。"""

    ACCEPT = {"sid": "uuid:sub-1", "status": 200}

    @classmethod
    def setUpClass(cls):
        outer = cls

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_SUBSCRIBE(self):     # noqa: N802
                outer.last_callback = self.headers.get("CALLBACK")
                self.send_response(outer.ACCEPT["status"])
                if outer.ACCEPT["sid"]:
                    self.send_header("SID", outer.ACCEPT["sid"])
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_UNSUBSCRIBE(self):   # noqa: N802
                outer.unsubscribed = self.headers.get("SID")
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        class Srv(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        cls.httpd = Srv(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        type(self).last_callback = None
        type(self).unsubscribed = None
        self.ACCEPT.update(sid="uuid:sub-1", status=200)

    def test_subscribe_and_unsubscribe(self):
        rend = make_renderer(self.base, rc_event=f"{self.base}/evt/rc")
        sub = st.GenaSubscriber(rend, "127.0.0.1", log=lambda _m: None)
        self.assertTrue(sub.start())
        self.assertEqual(sub.sid, "uuid:sub-1")
        self.assertTrue(self.last_callback.startswith("<http://127.0.0.1:"))
        sub.stop()
        self.assertEqual(self.unsubscribed, "uuid:sub-1")

    def test_notify_is_counted(self):
        import urllib.request
        rend = make_renderer(self.base, rc_event=f"{self.base}/evt/rc")
        sub = st.GenaSubscriber(rend, "127.0.0.1", log=lambda _m: None)
        self.assertTrue(sub.start())
        self.addCleanup(sub.stop)

        cb = self.last_callback.strip("<>")
        req = urllib.request.Request(cb, data=b"<propertyset/>", method="NOTIFY")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(sub.notify_count, 1)

    def test_refused_subscription_is_not_fatal(self):
        """不少设备不支持订阅；那只是少一条链路，不该让整轮测试失败。"""
        self.ACCEPT.update(sid=None, status=412)
        rend = make_renderer(self.base, rc_event=f"{self.base}/evt/rc")
        sub = st.GenaSubscriber(rend, "127.0.0.1", log=lambda _m: None)
        self.assertFalse(sub.start())
        self.assertIsNone(sub.sid)

    def test_no_event_url_is_reported_not_crashed(self):
        sub = st.GenaSubscriber(make_renderer(self.base), "127.0.0.1",
                                log=lambda _m: None)
        self.assertFalse(sub.start())


class TestEventSubUrlParsing(unittest.TestCase):

    LOC = "http://192.168.50.69:60099/dd.xml"
    XML = ('<?xml version="1.0"?>'
           '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
           "<UDN>uuid:x</UDN><friendlyName>TV</friendlyName><serviceList>"
           f"<service><serviceType>{AV}</serviceType>"
           "<controlURL>/av</controlURL><eventSubURL>/evt/av</eventSubURL></service>"
           f"<service><serviceType>{RC}</serviceType>"
           "<controlURL>/rc</controlURL><eventSubURL>evt/rc</eventSubURL></service>"
           "</serviceList></device></root>").encode()

    def test_event_urls_are_absolute(self):
        r = parse_description(self.LOC, self.XML)
        self.assertEqual(r.rc_event, "http://192.168.50.69:60099/evt/rc")
        self.assertEqual(r.av_event, "http://192.168.50.69:60099/evt/av")
        self.assertTrue(r.can_subscribe)

    def test_missing_event_url_means_cannot_subscribe(self):
        xml = self.XML.replace(b"<eventSubURL>evt/rc</eventSubURL>", b"")
        self.assertFalse(parse_description(self.LOC, xml).can_subscribe)


class TestExitCodes(unittest.TestCase):
    """两套语义方向相反，搞混了 CI 会把崩溃当成通过。"""

    def setUp(self):
        self.orig = dlna_push.run_stress
        self.addCleanup(lambda: setattr(dlna_push, "run_stress", self.orig))

    def code_for(self, result, expect_crash):
        dlna_push.run_stress = lambda *_a, **_k: result
        args = SimpleNamespace(
            vol_a=10, vol_b=21, burst=1, burst_gap=0.1, interval=0.1,
            max_rounds=1, churn=False, subscribe=False, confirm=3,
            recover_wait=1.0, expect_crash=expect_crash)
        session = SimpleNamespace(renderer=make_renderer(), bind_ip="127.0.0.1")
        with contextlib.redirect_stdout(_io.StringIO()):
            return dlna_push.stress(session, args)

    def test_aging_semantics(self):
        self.assertEqual(self.code_for(StressResult(crashed=False), False),
                         dlna_push.EXIT_OK)
        self.assertEqual(self.code_for(StressResult(crashed=True), False),
                         dlna_push.EXIT_CRASHED)

    def test_repro_semantics_are_inverted(self):
        """--expect-crash 下崩溃才是成功 —— 和旧 dlna_crash_repro.py 一致。"""
        self.assertEqual(self.code_for(StressResult(crashed=True), True),
                         dlna_push.EXIT_OK)
        self.assertEqual(self.code_for(StressResult(crashed=False), True),
                         dlna_push.EXIT_NOT_REPRODUCED)

    def test_interrupt_is_neither(self):
        """Ctrl-C 不代表通过，也不代表崩了。"""
        res = StressResult(crashed=False, interrupted=True)
        self.assertEqual(self.code_for(res, False), dlna_push.EXIT_INTERRUPTED)
        self.assertEqual(self.code_for(res, True), dlna_push.EXIT_INTERRUPTED)

    def test_bad_config_is_setup_error(self):
        dlna_push.run_stress = lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("vol 相同"))
        args = SimpleNamespace(
            vol_a=10, vol_b=10, burst=1, burst_gap=0.1, interval=0.1,
            max_rounds=1, churn=False, subscribe=False, confirm=3,
            recover_wait=1.0, expect_crash=False)
        session = SimpleNamespace(renderer=make_renderer(), bind_ip="127.0.0.1")
        with contextlib.redirect_stdout(_io.StringIO()):
            code = dlna_push.stress(session, args)
        self.assertEqual(code, dlna_push.EXIT_SETUP_ERROR)


class TestNormalize(unittest.TestCase):

    @staticmethod
    def parse(argv):
        args = dlna_push.build_parser().parse_args(argv)
        dlna_push.normalize(args)
        return args

    def test_no_push_promotes_the_lone_positional_to_device(self):
        """--no-push 时没有媒体可给，那唯一的位置参数只能是设备。"""
        args = self.parse(["--stress", "--no-push", "192.168.50.17"])
        self.assertIsNone(args.media)
        self.assertEqual(args.device, "192.168.50.17")

    def test_no_push_implies_stress(self):
        """不推流又不压测就没事可做了。"""
        self.assertTrue(self.parse(["--no-push", "192.168.50.17"]).stress)

    def test_normal_push_keeps_positions(self):
        args = self.parse(["a.mp4", "0"])
        self.assertEqual((args.media, args.device), ("a.mp4", "0"))
        self.assertFalse(args.stress)

    def test_list_is_untouched_by_no_push(self):
        args = self.parse(["--no-push", "list"])
        self.assertEqual(args.media, "list")

    def test_stress_defaults_match_config_defaults(self):
        args = self.parse(["a.mp4", "0", "--stress"])
        cfg = StressConfig()
        self.assertEqual(args.vol_a, cfg.vol_a)
        self.assertEqual(args.vol_b, cfg.vol_b)
        self.assertEqual(args.max_rounds, cfg.max_rounds)
        self.assertEqual(args.confirm, cfg.confirm)


class TestLooksLikeIp(unittest.TestCase):

    def test_ip(self):
        self.assertTrue(dlna_push._looks_like_ip("192.168.50.17"))

    def test_not_ip(self):
        for bad in ("客厅", "0", "192.168.50", "192.168.50.999", "", None):
            with self.subTest(bad=bad):
                self.assertFalse(dlna_push._looks_like_ip(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
