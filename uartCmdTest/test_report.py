"""报告输出的回归测试：跨轮次累计、JSON、JUnit XML。

    python3 -m unittest discover -v
"""

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from uart_exec import Result, TestRecord
from uart_report import Report, TestStat


def rec(name, result, dur=0.1, err="", tags=("core",)):
    return TestRecord(name, result, dur, err, tags)


class TestStatAggregation(unittest.TestCase):
    def test_pass_rate_excludes_skips(self):
        """跳过的不计入分母 —— 它没真跑，算进去会稀释真实通过率。"""
        s = TestStat("x", passed=8, failed=2, skipped=90)
        self.assertEqual(s.pass_rate, 0.8)
        self.assertEqual(s.runs, 100)

    def test_pass_rate_none_when_never_ran(self):
        s = TestStat("x", skipped=5)
        self.assertIsNone(s.pass_rate)

    def test_duration_stats(self):
        s = TestStat("x", passed=3, durations=[1.0, 2.0, 6.0])
        d = s.as_dict()["duration"]
        self.assertEqual(d["min"], 1.0)
        self.assertEqual(d["max"], 6.0)
        self.assertEqual(d["avg"], 3.0)

    def test_no_duration_key_when_never_ran(self):
        self.assertNotIn("duration", TestStat("x", skipped=1).as_dict())


class TestReportAccumulation(unittest.TestCase):
    def setUp(self):
        self.rep = Report(meta={"port": "/dev/fake"})

    def test_counts_across_cycles(self):
        for i in (1, 2, 3):
            self.rep.add_cycle(i, {
                "a": rec("a", Result.PASS),
                "b": rec("b", Result.FAIL if i == 2 else Result.PASS,
                         err="boom" if i == 2 else ""),
            })

        t = self.rep.totals
        self.assertEqual(t["cycles"], 3)
        self.assertEqual(t["tests"], 2)
        self.assertEqual(t["pass"], 5)
        self.assertEqual(t["fail"], 1)

        self.assertEqual(self.rep.stats["b"].pass_rate, round(2 / 3, 4))

    def test_failed_cycles_recorded_once(self):
        """同一轮里两条失败，只记一次轮号。"""
        self.rep.add_cycle(7, {
            "a": rec("a", Result.FAIL, err="x"),
            "b": rec("b", Result.FAIL, err="y"),
        })
        self.assertEqual(self.rep.failed_cycles, [7])

    def test_failure_detail_keeps_cycle_and_error(self):
        self.rep.add_cycle(4, {"a": rec("a", Result.FAIL, err="rc=-8")})
        f = self.rep.stats["a"].failures
        self.assertEqual(f, [{"cycle": 4, "error": "rc=-8"}])

    def test_worst_sorted_by_failures(self):
        for i in range(1, 6):
            self.rep.add_cycle(i, {
                "often": rec("often", Result.FAIL, err="e"),
                "rare": rec("rare", Result.FAIL if i == 1 else Result.PASS,
                            err="e" if i == 1 else ""),
                "never": rec("never", Result.PASS),
            })

        worst = self.rep.worst(2)
        self.assertEqual([s.name for s in worst], ["often", "rare"])
        self.assertNotIn("never", [s.name for s in worst])

    def test_skip_does_not_count_as_run(self):
        self.rep.add_cycle(1, {"a": rec("a", Result.SKIP)})
        self.assertEqual(self.rep.totals["skip"], 1)
        self.assertEqual(self.rep.totals["pass"], 0)
        self.assertIsNone(self.rep.stats["a"].pass_rate)


class TestDurationDrift(unittest.TestCase):
    """耗时随轮次变长的检测。性能退化是渐进的，单轮不超时也可能已慢一倍。"""

    def _rep(self, durations):
        rep = Report()
        for i, d in enumerate(durations, 1):
            rep.add_cycle(i, {"a": rec("a", Result.PASS, d)})
        return rep

    def test_detects_slowdown(self):
        # 前 4 轮 1 秒，后 4 轮 3 秒 —— 慢了 3 倍
        rep = self._rep([1, 1, 1, 1, 3, 3, 3, 3])
        d = rep.stats["a"].drift()

        self.assertEqual(d["first_half_avg"], 1.0)
        self.assertEqual(d["second_half_avg"], 3.0)
        self.assertEqual(d["ratio"], 3.0)

    def test_stable_is_not_flagged(self):
        rep = self._rep([2.0, 2.1, 1.9, 2.0, 2.05, 1.95, 2.0, 2.0])
        self.assertEqual(rep.slowing(), [])

    def test_slowing_threshold(self):
        rep = self._rep([1, 1, 1, 2, 2, 2])          # ratio = 2.0
        self.assertEqual(len(rep.slowing(ratio=1.5)), 1)
        self.assertEqual(len(rep.slowing(ratio=3.0)), 0)

    def test_too_few_samples_returns_none(self):
        """样本 <6 轮时噪声比信号大，不给结论。"""
        self.assertIsNone(self._rep([1, 5]).stats["a"].drift())
        self.assertIsNone(self._rep([1, 1, 1, 5, 5]).stats["a"].drift())

    def test_drift_appears_in_json(self):
        rep = self._rep([1, 1, 1, 4, 4, 4])
        rep.finish(0)
        t = rep.as_dict()["tests"][0]
        self.assertIn("drift", t["duration"])
        self.assertEqual(t["duration"]["drift"]["ratio"], 4.0)

    def test_zero_baseline_returns_none(self):
        """前半段全是 0 时算不出比例，别除零。"""
        self.assertIsNone(self._rep([0, 0, 0, 1, 1, 1]).stats["a"].drift())

    def test_slowing_sorted_by_ratio(self):
        rep = Report()
        for i in range(1, 7):
            slow = 1.0 if i <= 3 else 5.0
            mild = 1.0 if i <= 3 else 2.0
            rep.add_cycle(i, {
                "very": rec("very", Result.PASS, slow),
                "mild": rec("mild", Result.PASS, mild),
            })
        self.assertEqual([s.name for s, _d in rep.slowing()], ["very", "mild"])


class TestJsonOutput(unittest.TestCase):
    def _write(self, rep):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "r.json")
        rep.write_json(p)
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def test_structure(self):
        rep = Report(meta={"port": "/dev/ttyUSB0", "tags": "core"})
        rep.add_cycle(1, {"a": rec("a", Result.PASS, 1.5)})
        rep.finish(0)

        j = self._write(rep)
        for key in ("tool", "started_at", "duration_sec", "exit_code",
                    "meta", "totals", "failed_cycles", "tests"):
            self.assertIn(key, j)

        self.assertEqual(j["exit_code"], 0)
        self.assertEqual(j["meta"]["port"], "/dev/ttyUSB0")
        self.assertEqual(j["tests"][0]["name"], "a")

    def test_chinese_not_escaped(self):
        """ensure_ascii=False —— 报告要能直接看，不能全是 \\uXXXX。"""
        rep = Report()
        rep.add_cycle(1, {"扫描网络": rec("扫描网络", Result.FAIL,
                                       err="rc=-8 密码错")})
        rep.finish(1)

        d = tempfile.mkdtemp()
        p = os.path.join(d, "r.json")
        rep.write_json(p)
        raw = open(p, encoding="utf-8").read()

        self.assertIn("扫描网络", raw)
        self.assertNotIn("\\u", raw)

    def test_exit_code_recorded(self):
        rep = Report()
        rep.add_cycle(1, {"a": rec("a", Result.FAIL, err="x")})
        rep.finish(1)
        self.assertEqual(self._write(rep)["exit_code"], 1)


class TestJunitOutput(unittest.TestCase):
    def _write(self, rep):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "r.xml")
        rep.write_junit(p)
        return ET.parse(p).getroot()

    def test_basic_structure(self):
        rep = Report(meta={"port": "/dev/fake"})
        rep.add_cycle(1, {
            "ok": rec("ok", Result.PASS, 2.0),
            "bad": rec("bad", Result.FAIL, 1.0, "rc=-1 FAILED"),
            "sk": rec("sk", Result.SKIP),
        })
        rep.finish(1)

        root = self._write(rep)
        self.assertEqual(root.tag, "testsuites")

        suite = root.find("testsuite")
        self.assertEqual(suite.get("tests"), "3")
        self.assertEqual(suite.get("failures"), "1")
        self.assertEqual(suite.get("skipped"), "1")

        cases = {c.get("name"): c for c in suite.findall("testcase")}
        self.assertIsNone(cases["ok"].find("failure"))
        self.assertIsNotNone(cases["bad"].find("failure"))
        self.assertIsNotNone(cases["sk"].find("skipped"))

    def test_failure_message_and_cycles(self):
        rep = Report()
        for i in (2, 5, 9):
            rep.add_cycle(i, {"a": rec("a", Result.FAIL, err=f"e{i}")})
        rep.finish(1)

        f = self._write(rep).find("testsuite/testcase/failure")
        self.assertEqual(f.get("message"), "e2")       # 第一次的错误
        self.assertIn("3/3 轮失败", f.text)
        for c in ("2", "5", "9"):
            self.assertIn(c, f.text)

    def test_aggregates_not_one_case_per_cycle(self):
        """500 轮不能生成 500 个 testcase —— CI 界面会没法看。"""
        rep = Report()
        for i in range(1, 501):
            rep.add_cycle(i, {"a": rec("a", Result.PASS)})
        rep.finish(0)

        cases = self._write(rep).findall("testsuite/testcase")
        self.assertEqual(len(cases), 1)

    def test_meta_becomes_properties(self):
        rep = Report(meta={"port": "/dev/ttyUSB0", "cycles": 500})
        rep.add_cycle(1, {"a": rec("a", Result.PASS)})
        rep.finish(0)

        props = {p.get("name"): p.get("value")
                 for p in self._write(rep).findall("testsuite/property")}
        self.assertEqual(props["port"], "/dev/ttyUSB0")
        self.assertEqual(props["cycles"], "500")

    def test_classname_uses_first_tag(self):
        rep = Report()
        rep.add_cycle(1, {"a": rec("a", Result.PASS, tags=("audio", "x"))})
        rep.finish(0)
        case = self._write(rep).find("testsuite/testcase")
        self.assertEqual(case.get("classname"), "uartCmdTest.audio")

    def test_time_is_average_duration(self):
        rep = Report()
        rep.add_cycle(1, {"a": rec("a", Result.PASS, 1.0)})
        rep.add_cycle(2, {"a": rec("a", Result.PASS, 3.0)})
        rep.finish(0)
        case = self._write(rep).find("testsuite/testcase")
        self.assertEqual(case.get("time"), "2.000")

    def test_xml_is_wellformed_with_chinese(self):
        rep = Report()
        rep.add_cycle(1, {"扫描网络": rec("扫描网络", Result.FAIL,
                                       err="rc=-8 INCORRECT_PWD(密码错)")})
        rep.finish(1)

        root = self._write(rep)      # 解析不报错就说明 XML 合法
        case = root.find("testsuite/testcase")
        self.assertEqual(case.get("name"), "扫描网络")
        self.assertIn("密码错", case.find("failure").get("message"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
