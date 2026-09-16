"""老化 / 压力测试：反复改音量，盯着设备会不会死。

来源是 C 版目录里的 `dlna_crash_repro.py`，用来复现 826x DMR 的
SetVolume 崩溃：

    DMR_LastChangeTimerEvent() 每 200ms 跑一次，看到 LastChangeMask != 0
    就触发 FireGenaLastChangeEvent()，那里读 DMR_microStack 的偏移 0x50。
    运行期间这个指针会变坏（疑似 DMRDestroyFromChain 释放了 state 却没有
    取消自我 re-arm 的 200ms 定时器），于是 SIGSEGV。

所以压力的形状不是随便刷命令，有三条**必须照做**的规则：

1. **两个不同的音量交替发**。相同音量会被设备自己拦掉
   （"the same volume detected, block !"），一次都到不了事件链路。
2. **间隔要跨过 200ms 定时器周期**，同时在组内密集连发（`burst`），
   在定时器窗口里制造并发。
3. **订阅 GENA 事件**（`subscribe`），让 LastChange / NOTIFY 这条链路真的
   跑起来，而不只是控制面。

判定同样有讲究，**误报和漏报都要防**：

* 探活失败要分类。连接被拒/重置是进程死亡的最强特征；超时可能只是设备忙；
  收到非 200 的 HTTP 响应说明进程明明还活着，不算崩。
* 要连续失败 `confirm` 次才算失联，防网络抖动误报。
* 判定失联后继续盯 `recover_wait` 秒 —— actui 崩了会被 init 自动拉起，
  "死过又复活"是复现成功的证据，不是没崩。
"""

import http.client
import socket
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Tuple

import dlna_soap as soap
from dlna_device import Renderer


class Probe(Enum):
    """探活结果。只有 REFUSED / TIMEOUT / OTHER 才可能是崩溃。"""

    OK = "ok"
    REFUSED = "refused"     # 连接被拒/重置 —— 进程死亡特征最强
    TIMEOUT = "timeout"     # 超时 —— 可能死了，也可能只是忙
    HTTP_ERR = "http"       # 有 HTTP 响应但不是 200 —— 进程还活着，不算崩
    OTHER = "other"

    @property
    def alive(self) -> bool:
        return self in (Probe.OK, Probe.HTTP_ERR)


def classify(exc: soap.SoapError) -> Probe:
    """把一个 SoapError 归到探活分类里。

    urllib 会把底层 OSError 包进 URLError，所以要往里剥一层才能看到
    ConnectionRefusedError —— 不剥的话所有失败都会落到 OTHER，
    "设备进程死了"和"网线松了"就分不开。
    """
    cause = exc.cause
    if isinstance(cause, urllib.error.HTTPError):
        return Probe.HTTP_ERR       # 有 HTTP 响应 = 进程活着
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    if isinstance(cause, (ConnectionRefusedError, ConnectionResetError)):
        return Probe.REFUSED
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return Probe.TIMEOUT
    return Probe.OTHER


def probe_alive(rend: Renderer, timeout: float = 2.0) -> Tuple[Probe, str]:
    """用 GetVolume 探活，返回 (分类, 详情)。"""
    try:
        soap.get_volume(rend, timeout=timeout)
        return Probe.OK, ""
    except soap.SoapError as exc:
        return classify(exc), str(exc)


class CrashDetector:
    """连续确认失联 + 等待复活。复活也算崩溃证据。"""

    def __init__(self, rend: Renderer, confirm: int = 3,
                 confirm_gap: float = 0.7, recover_wait: float = 60.0,
                 log: Optional[Callable[[str], None]] = None):
        self.rend = rend
        self.confirm = confirm
        self.confirm_gap = confirm_gap
        self.recover_wait = recover_wait
        self.log = log or print

    def check(self, context: str) -> Optional[Tuple[str, Optional[float]]]:
        """存活返回 None；失联返回 (死因, 停机秒数 / None 表示没等到复活)。"""
        seen = []
        for i in range(self.confirm):
            kind, detail = probe_alive(self.rend)
            seen.append(kind)
            if kind.alive:
                if kind is Probe.HTTP_ERR:
                    self.log(f"{context} 探活收到非 200（{detail}），"
                             f"进程仍存活，继续测试")
                return None
            self.log(f"{context} 探活失败 [{i + 1}/{self.confirm}]: "
                     f"{kind.value} ({detail})")
            time.sleep(self.confirm_gap)

        if Probe.REFUSED in seen:
            cause = "连接被拒/重置（DMR 进程死亡特征）"
        elif Probe.TIMEOUT in seen:
            cause = "连续超时无响应"
        else:
            cause = "连续网络异常"
        self.log(f"设备失联确认（{context}: {cause}），等待设备恢复...")

        dead_since = time.monotonic()
        deadline = dead_since + self.recover_wait
        while time.monotonic() < deadline:
            kind, _ = probe_alive(self.rend, timeout=3.0)
            if kind.alive:
                return cause, time.monotonic() - dead_since
            time.sleep(1.0)
        return cause, None


# ---------------------------------------------------------------- GENA 事件订阅

class _NotifyHandler(BaseHTTPRequestHandler):
    """接设备发来的 NOTIFY。内容不解析 —— 我们只关心链路有没有在动。"""

    protocol_version = "HTTP/1.1"
    counter = None          # 由 GenaSubscriber 注入

    def log_message(self, *_a):
        pass

    def do_NOTIFY(self):    # noqa: N802 - HTTP 方法名由基类约定
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.counter is not None:
            self.counter.bump()
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Counter:
    def __init__(self):
        self._lock = threading.Lock()
        self.n = 0

    def bump(self):
        with self._lock:
            self.n += 1


class GenaSubscriber:
    """订阅 RenderingControl 事件，并起一个 NOTIFY 接收服务。

    崩溃走的是 LastChange 事件路径，不订阅设备就不会真的去发事件 ——
    只刷控制面很可能一直复现不出来。
    """

    def __init__(self, rend: Renderer, callback_ip: str,
                 log: Optional[Callable[[str], None]] = None):
        self.rend = rend
        self.callback_ip = callback_ip
        self.log = log or print
        self.sid: Optional[str] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._counter = _Counter()

    @property
    def notify_count(self) -> int:
        return self._counter.n

    def start(self, timeout_sec: int = 1800) -> bool:
        if not self.rend.can_subscribe:
            self.log("[!] 设备未提供 RenderingControl 的 eventSubURL, 无法订阅")
            return False

        handler = type("NotifyHandler", (_NotifyHandler,),
                       {"counter": self._counter})

        class _Srv(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        # 绑 0.0.0.0：回调地址给的是本机在该网段的 IP，但设备可能从别的
        # 接口回来，绑死单个地址会收不到。
        self._httpd = _Srv(("0.0.0.0", 0), handler)
        threading.Thread(target=self._httpd.serve_forever,
                         name="gena", daemon=True).start()
        port = self._httpd.server_address[1]

        try:
            self.sid = self._subscribe(port, timeout_sec)
        except OSError as exc:
            self.log(f"[!] SUBSCRIBE 发送失败: {exc}")
            self.sid = None

        if self.sid:
            self.log(f"[+] 事件订阅成功 SID={self.sid}, "
                     f"回调 http://{self.callback_ip}:{port}/rc")
            return True
        self.stop()
        return False

    def _subscribe(self, port: int, timeout_sec: int) -> Optional[str]:
        parsed = urllib.parse.urlparse(self.rend.rc_event)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80,
                                          timeout=5)
        try:
            conn.request("SUBSCRIBE", parsed.path or "/", headers={
                "CALLBACK": f"<http://{self.callback_ip}:{port}/rc>",
                "NT": "upnp:event",
                "TIMEOUT": f"Second-{timeout_sec}",
            })
            resp = conn.getresponse()
            resp.read()
            if resp.status == 200 and resp.getheader("SID"):
                return resp.getheader("SID")
            self.log(f"[!] SUBSCRIBE 被拒: HTTP {resp.status}"
                     f"（设备可能不支持/拒绝订阅）")
            return None
        finally:
            conn.close()

    def stop(self) -> None:
        if self.sid:
            try:
                parsed = urllib.parse.urlparse(self.rend.rc_event)
                conn = http.client.HTTPConnection(parsed.hostname,
                                                  parsed.port or 80, timeout=3)
                conn.request("UNSUBSCRIBE", parsed.path or "/",
                             headers={"SID": self.sid})
                conn.getresponse().read()
                conn.close()
            except OSError:
                pass        # 设备可能已经死了，退订失败无所谓
            self.sid = None
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ------------------------------------------------------------------ 压力测试

@dataclass
class StressConfig:
    """老化 / 压力测试参数。默认值沿用 dlna_crash_repro.py。"""

    vol_a: int = 10             # 交替音量 A
    vol_b: int = 21             # 交替音量 B（必须与 A 不同）
    burst: int = 1              # 每组连发次数
    burst_gap: float = 0.13     # 组内连发间隔，逼近真实控制端 ~130ms 节奏
    interval: float = 0.5       # 组间隔，要 > 0.2s 才跨得过设备的定时器周期
    max_rounds: int = 60
    churn: bool = False         # 每 10 轮插一次 Stop/Play 扰动
    subscribe: bool = False     # 订阅 GENA 事件
    confirm: int = 3            # 判定失联所需的连续探活失败次数
    recover_wait: float = 60.0  # 判定失联后等待复活的秒数

    def validate(self) -> None:
        if self.vol_a == self.vol_b:
            raise ValueError("--vol-a 与 --vol-b 必须不同, "
                             "否则全部命中设备的 same-volume 拦截")
        if self.max_rounds < 1:
            raise ValueError("--max-rounds 至少为 1")
        if self.burst < 1:
            raise ValueError("--burst 至少为 1")


@dataclass
class StressResult:
    """跑完之后的结论。"""

    crashed: bool
    cause: str = ""
    downtime: Optional[float] = None    # 停机秒数；None = 观察窗口内没复活
    rounds: int = 0
    sent_ok: int = 0
    send_fail: int = 0
    notify_count: int = 0
    interrupted: bool = False


def run_stress(rend: Renderer, cfg: StressConfig, bind_ip: str,
               log: Optional[Callable[[str], None]] = None) -> StressResult:
    """跑老化循环。设备失联就提前返回。"""
    log = log or print
    cfg.validate()

    gena: Optional[GenaSubscriber] = None
    if cfg.subscribe:
        gena = GenaSubscriber(rend, bind_ip, log=log)
        if not gena.start():
            log("[!] 事件订阅失败, 继续仅控制面测试 "
                "(崩溃走的是事件路径, 不订阅可能一直复现不出来)")

    result = StressResult(crashed=False)
    try:
        log("[*] 发送初始探活请求...")
        kind, detail = probe_alive(rend)
        if not kind.alive:
            raise RuntimeError(f"初始探活失败 ({kind.value}: {detail}), "
                               f"请确认设备状态与网络连通性")

        log(f"[*] 开始老化: 音量 {cfg.vol_a} <-> {cfg.vol_b}, "
            f"每组连发 {cfg.burst} 次(间隔 {cfg.burst_gap:.2f}s), "
            f"组间隔 {cfg.interval:.2f}s, "
            f"订阅={'开' if (gena and gena.sid) else '关'}, "
            f"churn={'开' if cfg.churn else '关'}, "
            f"共 {cfg.max_rounds} 轮")

        detector = CrashDetector(rend, confirm=cfg.confirm,
                                 recover_wait=cfg.recover_wait, log=log)
        cur, other = cfg.vol_a, cfg.vol_b

        for round_no in range(1, cfg.max_rounds + 1):
            result.rounds = round_no
            for burst_no in range(cfg.burst):
                try:
                    soap.set_volume(rend, cur)
                    result.sent_ok += 1
                    log(f"[{round_no:03d}.{burst_no + 1}] SetVolume({cur}) -> ok")
                except soap.SoapError as exc:
                    result.send_fail += 1
                    log(f"[{round_no:03d}.{burst_no + 1}] "
                        f"SetVolume({cur}) 发送异常: {exc}")
                    dead = detector.check("SetVolume 发送失败后")
                    if dead:
                        return _finish(result, dead, gena)
                    log("      设备仍存活（HTTP 层可达）, 判为网络抖动, 继续")
                if burst_no + 1 < cfg.burst:
                    time.sleep(cfg.burst_gap)
                # 组内也要换值：连发同一个音量会被设备的 same-volume 检查拦掉
                cur, other = other, cur

            if cfg.churn and round_no % 10 == 0:
                _churn(rend, round_no, log)

            time.sleep(cfg.interval)

            dead = detector.check(f"[{round_no:03d}]")
            if dead:
                return _finish(result, dead, gena)
    except KeyboardInterrupt:
        log("\n[*] 用户中断")
        result.interrupted = True
    finally:
        if gena:
            result.notify_count = gena.notify_count
            gena.stop()

    return result


def _churn(rend: Renderer, round_no: int, log: Callable[[str], None]) -> None:
    """Stop/Play 扰动：让状态机和 200ms 定时器撞在一起。"""
    try:
        soap.stop(rend)
        time.sleep(0.3)
        soap.play(rend)
        log(f"[{round_no:03d}] churn: Stop -> Play 完成")
    except soap.SoapError as exc:
        log(f"[{round_no:03d}] churn 操作异常: {exc}")


def _finish(result: StressResult, dead: Tuple[str, Optional[float]],
            gena: Optional[GenaSubscriber]) -> StressResult:
    result.crashed = True
    result.cause, result.downtime = dead
    if gena:
        result.notify_count = gena.notify_count
        gena.stop()
    return result


def report(result: StressResult, log: Optional[Callable[[str], None]] = None,
           expect_crash: bool = False) -> None:
    """把结论打出来。expect_crash 只影响措辞，不影响事实。"""
    log = log or print
    line = "=" * 50
    if result.crashed:
        log(line)
        head = "SUCCESS: 复现成功！" if expect_crash else "FAILED: 设备在老化中失联！"
        log(f" {head}设备在 SetVolume 压力测试期间失联。")
        log(f" 失联原因: {result.cause}")
        if result.downtime is not None:
            log(f" 设备在 {result.downtime:.1f} 秒后恢复"
                f"（符合崩溃后被 init 自动重启的特征）。")
        else:
            log(" 设备在观察窗口内未恢复。")
        log(f" 已跑 {result.rounds} 轮, 成功发送 {result.sent_ok} 次音量指令。")
        log(line)
        return

    if result.interrupted:
        log(f"[*] 已中断: 跑了 {result.rounds} 轮, "
            f"成功发送 {result.sent_ok} 次, 设备存活")
    else:
        log(f"[+] 老化完成: {result.rounds} 轮, 成功发送 {result.sent_ok} 次"
            f"({result.send_fail} 次发送异常), 设备未发生崩溃")
    if result.notify_count:
        log(f"[i] 测试期间共收到 {result.notify_count} 个 NOTIFY 事件")
