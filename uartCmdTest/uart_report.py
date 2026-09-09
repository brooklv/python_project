"""测试结果报告：跨轮次累计，输出 JSON 和 JUnit XML。

日志是给人看的，报告是给机器看的：

* **JSON** —— 脚本分析、趋势对比。老化测试最关心的两个问题都在里面：
  哪一步失败最多、耗时有没有随轮次变长
* **JUnit XML** —— Jenkins / GitLab CI 能直接展示哪条测试失败

只用标准库（json + xml.etree），不引额外依赖。
"""

import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from uart_exec import Result


@dataclass
class TestStat:
    """一条测试在所有轮次里的累计结果。"""

    name: str
    tags: tuple = ()
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    durations: list = field(default_factory=list)
    failures: list = field(default_factory=list)   # [{cycle, error}]
    # (轮次, 耗时) 序列，用于看耗时有没有随轮次变长
    timeline: list = field(default_factory=list)

    @property
    def runs(self) -> int:
        return self.passed + self.failed + self.skipped

    @property
    def pass_rate(self) -> Optional[float]:
        """通过率。跳过的不计入分母 —— 它没真跑。"""
        ran = self.passed + self.failed
        return round(self.passed / ran, 4) if ran else None

    def drift(self) -> Optional[dict]:
        """耗时有没有随轮次变长：拿后半段的均值比前半段。

        老化测试里性能退化是渐进的 —— 单看某一轮不超时，但跑到 400 轮
        时已经比第 1 轮慢了一倍。只有前后对比才看得出来。

        样本太少（<6 轮）时返回 None，那时候噪声比信号大。
        """
        if len(self.timeline) < 6:
            return None

        vals = [d for _c, d in sorted(self.timeline)]
        half = len(vals) // 2
        first = sum(vals[:half]) / half
        second = sum(vals[half:]) / (len(vals) - half)

        if first <= 0:
            return None

        return {
            "first_half_avg": round(first, 3),
            "second_half_avg": round(second, 3),
            "ratio": round(second / first, 2),
        }

    def as_dict(self) -> dict:
        d = {
            "name": self.name,
            "tags": list(self.tags),
            "pass": self.passed,
            "fail": self.failed,
            "skip": self.skipped,
            "pass_rate": self.pass_rate,
        }
        if self.durations:
            d["duration"] = {
                "min": round(min(self.durations), 3),
                "max": round(max(self.durations), 3),
                "avg": round(sum(self.durations) / len(self.durations), 3),
            }
        dr = self.drift()
        if dr:
            d["duration"]["drift"] = dr
        if self.failures:
            d["failures"] = self.failures
        return d


class Report:
    """跨轮次累计测试结果。

    用法::

        rep = Report(meta={...})
        for cycle in ...:
            rep.add_cycle(cycle, run_suite(ctx, tests))
        rep.finish(exit_code)
        rep.write_json("report.json")
        rep.write_junit("report.xml")
    """

    def __init__(self, meta: Optional[dict] = None):
        self.meta = dict(meta or {})
        self.stats: dict = {}          # {测试名: TestStat}
        self.cycles = 0
        self.failed_cycles: list = []  # 哪几轮里出现过失败
        self._t0 = time.monotonic()
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.duration = 0.0
        self.exit_code = 0

    # ---------------------------------------------------------------- 采集
    def add_cycle(self, cycle: int, records: dict) -> None:
        self.cycles = max(self.cycles, cycle)
        had_fail = False

        for rec in records.values():
            st = self.stats.setdefault(rec.name,
                                       TestStat(rec.name, tuple(rec.tags)))
            if not st.tags:
                st.tags = tuple(rec.tags)

            if rec.result is Result.PASS:
                st.passed += 1
                st.durations.append(rec.duration)
                st.timeline.append((cycle, rec.duration))
            elif rec.result is Result.FAIL:
                st.failed += 1
                st.durations.append(rec.duration)
                st.timeline.append((cycle, rec.duration))
                st.failures.append({"cycle": cycle, "error": rec.error})
                had_fail = True
            else:
                st.skipped += 1

        if had_fail and cycle not in self.failed_cycles:
            self.failed_cycles.append(cycle)

    def finish(self, exit_code: int = 0) -> None:
        self.duration = round(time.monotonic() - self._t0, 3)
        self.exit_code = exit_code

    # ---------------------------------------------------------------- 汇总
    @property
    def totals(self) -> dict:
        return {
            "tests": len(self.stats),
            "cycles": self.cycles,
            "pass": sum(s.passed for s in self.stats.values()),
            "fail": sum(s.failed for s in self.stats.values()),
            "skip": sum(s.skipped for s in self.stats.values()),
        }

    def slowing(self, ratio: float = 1.5) -> list:
        """耗时明显变长的测试。默认后半段比前半段慢 50% 以上就算。"""
        out = []
        for s_ in self.stats.values():
            d = s_.drift()
            if d and d["ratio"] >= ratio:
                out.append((s_, d))
        out.sort(key=lambda x: -x[1]["ratio"])
        return out

    def worst(self, n: int = 3) -> list:
        """失败次数最多的几条测试。老化测试最想先知道这个。"""
        bad = [s for s in self.stats.values() if s.failed]
        bad.sort(key=lambda s: (-s.failed, s.name))
        return bad[:n]

    # ---------------------------------------------------------------- 输出
    def as_dict(self) -> dict:
        return {
            "tool": "uartCmdTest",
            "started_at": self.started_at,
            "duration_sec": self.duration,
            "exit_code": self.exit_code,
            "meta": self.meta,
            "totals": self.totals,
            "failed_cycles": self.failed_cycles,
            "tests": [s.as_dict() for s in self.stats.values()],
        }

    def write_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.as_dict(), f, ensure_ascii=False, indent=2)

    def write_junit(self, path: str) -> None:
        """JUnit XML。每条测试一个 testcase，跨轮次聚合。

        某条测试在 500 轮里失败了 2 次，就报成一个 failure，
        message 里说明失败于哪些轮 —— 展开成 500 个 testcase 对 CI 没用。
        """
        t = self.totals
        n_fail = sum(1 for s in self.stats.values() if s.failed)
        n_skip = sum(1 for s in self.stats.values()
                     if s.skipped and not s.passed and not s.failed)

        suites = ET.Element("testsuites", {
            "name": "uartCmdTest",
            "tests": str(t["tests"]),
            "failures": str(n_fail),
            "time": f"{self.duration:.3f}",
        })
        suite = ET.SubElement(suites, "testsuite", {
            "name": "uartCmdTest",
            "tests": str(t["tests"]),
            "failures": str(n_fail),
            "skipped": str(n_skip),
            "time": f"{self.duration:.3f}",
            "timestamp": self.started_at,
        })

        for k, v in self.meta.items():
            ET.SubElement(suite, "property", {"name": str(k), "value": str(v)})

        for s in self.stats.values():
            avg = (sum(s.durations) / len(s.durations)) if s.durations else 0.0
            case = ET.SubElement(suite, "testcase", {
                "name": s.name,
                "classname": f"uartCmdTest.{s.tags[0] if s.tags else 'misc'}",
                "time": f"{avg:.3f}",
            })

            if s.failed:
                cycles = [str(f["cycle"]) for f in s.failures]
                first = s.failures[0]["error"]
                fail = ET.SubElement(case, "failure", {
                    "message": first,
                    "type": "TestFailed",
                })
                fail.text = (f"{s.failed}/{s.passed + s.failed} 轮失败"
                             f"（第 {'、'.join(cycles[:20])} 轮"
                             f"{' 等' if len(cycles) > 20 else ''}）\n" +
                             "\n".join(f"  轮 {f['cycle']}: {f['error']}"
                                       for f in s.failures[:20]))
            elif s.skipped and not s.passed:
                ET.SubElement(case, "skipped",
                              {"message": "依赖未通过，未执行"})

        ET.ElementTree(suites).write(path, encoding="utf-8",
                                     xml_declaration=True)
