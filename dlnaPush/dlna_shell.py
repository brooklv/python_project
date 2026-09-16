"""交互控制台：推流之后调音量、拖进度、换片、换设备。

命令集和 C 版一一对应，因为肌肉记忆比"更合理的命名"值钱。

行编辑（退格/方向键/历史）靠标准库的 ``readline``。C 版要 link
libreadline 并用 ``-DUSE_READLINE`` 开关控制，Python 这边 import 上就有 ——
导不进来（Windows 上就没有）也只是退化成没有历史，不影响任何功能。

**每条命令都自己兜住异常**：设备中途掉线、SOAP 报错、文件不存在都不该
把整个会话打死 —— 会话里可能正跑着一个 ffmpeg，进程一死产物就留在磁盘上了。
"""

from typing import Optional

import dlna_soap as soap
from dlna_session import Session, sec_to_hms
from dlna_transcode import TranscodeError

try:                    # 有就有行编辑，没有也能用
    import readline     # noqa: F401  (import 上就生效，不需要显式调用)
    HAVE_READLINE = True
except ImportError:     # pragma: no cover
    HAVE_READLINE = False


HELP = """
可用命令 (输入后回车):
  p / play         播放
  pause            暂停
  pp               播放/暂停 切换
  s / stop         停止
  vol <0-100>      设置音量
  +                音量 +5
  -                音量 -5
  seek <时:分:秒>   跳转到指定位置 (也可直接给秒数, 如 seek 90)
  i / info         查看播放状态/进度/音量
  open <文件|URL>   换一个媒体推到当前设备 (不带参数则重推当前媒体)
  dev [序号|名称|IP] 切换目标设备 (不带参数则列表并提示选择)
  scan             重新搜索设备
  h / help         显示本帮助
  q / quit         退出程序
"""


class Shell:
    """读一行、执行一条。没有子命令嵌套，所以不用 cmd 模块。"""

    def __init__(self, session: Session):
        self.s = session

    # ------------------------------------------------------------------ 输入
    @staticmethod
    def read_line(prompt: str) -> Optional[str]:
        """读一行。Ctrl-C / Ctrl-D 都返回 None，由调用方决定怎么收场。"""
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()             # 让提示符和后面的输出各占一行，终端不留半截行
            return None

    def run(self) -> None:
        s = self.s
        if s.renderer and s.renderer.has_rc:
            print(f"[i] 当前音量: {s.volume}")
        print(HELP)

        while True:
            line = self.read_line("dlna> ")
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            cmd, _, arg = line.partition(" ")
            arg = arg.strip()
            if cmd in ("q", "quit"):
                break
            try:
                self.dispatch(cmd, arg)
            except soap.SoapError as exc:
                print(f"[-] {exc}")
            except TranscodeError as exc:
                print(f"[-] 转换失败: {exc}")
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"[-] {exc}")

    # ------------------------------------------------------------------ 分发
    def dispatch(self, cmd: str, arg: str) -> None:
        s = self.s
        r = s.renderer

        if cmd in ("h", "help"):
            print(HELP)
        elif cmd in ("p", "play"):
            soap.play(r)
            print("[+] 播放")
        elif cmd == "pause":
            soap.pause(r)
            print("[+] 暂停")
        elif cmd == "pp":
            # 先问状态再决定：盲发 Play 会把正在播的重头开始
            if soap.get_state(r) == "PLAYING":
                soap.pause(r)
                print("[+] 暂停")
            else:
                soap.play(r)
                print("[+] 播放")
        elif cmd in ("s", "stop"):
            soap.stop(r)
            print("[+] 停止")
        elif cmd == "vol":
            if not arg.lstrip("-").isdigit():
                print("用法: vol <0-100>")
                return
            s.volume = soap.set_volume(r, int(arg))
            print(f"[+] 音量 -> {s.volume}")
        elif cmd in ("+", "-"):
            step = 5 if cmd == "+" else -5
            s.volume = soap.set_volume(r, s.volume + step)
            print(f"[+] 音量 -> {s.volume}")
        elif cmd == "seek":
            self._seek(arg)
        elif cmd in ("i", "info"):
            self._info()
        elif cmd == "open":
            self._open(arg)
        elif cmd == "dev":
            self._dev(arg)
        elif cmd in ("scan", "rescan"):
            s.scan()
            print(s.device_list_text())
            print("[i] 可用 dev <序号> 切换到新发现的设备")
        else:
            print(f"未知命令: {cmd} (输入 h 查看帮助)")

    # ------------------------------------------------------------------ 各命令
    def _seek(self, arg: str) -> None:
        if not arg:
            print("用法: seek <时:分:秒> 或 seek <秒>")
            return
        target = arg if ":" in arg else sec_to_hms(int(arg))
        soap.seek(self.s.renderer, target)
        print(f"[+] 跳转到 {target}")

    def _info(self) -> None:
        s, r = self.s, self.s.renderer
        state = soap.get_state(r)
        rel, dur = soap.get_position(r)
        vol = s.volume
        if r.has_rc:
            try:
                vol = s.volume = soap.get_volume(r)
            except soap.SoapError:
                pass
        print(f"[i] 设备: {r.friendly}  状态: {state}  "
              f"进度: {rel} / {dur}  音量: {vol}")

    def _open(self, arg: str) -> None:
        """换文件（带参数），或重推当前媒体（不带参数，常用于切设备之后）。"""
        media = arg or self.s.media
        if not media:
            print("用法: open <文件路径|URL>")
            return
        self.s.push(media)

    def _dev(self, arg: str) -> None:
        s = self.s
        sel = arg
        if not sel:
            print(s.device_list_text())
            sel = self.read_line("选择设备(序号/名称/IP): ") or ""
        if not s.select(sel.strip()):
            print("[-] 无效的设备选择")
            return
        print(f"[*] 目标设备切换为: {s.renderer.friendly}")
        if s.media:
            print("[i] 输入 open 可把当前媒体推到该设备, 或 open <新文件> 换片")


def choose_device(session: Session, sel: Optional[str]) -> bool:
    """开推之前选设备：命令行给了先试，没给或选错就循环提示。

    选错不退出 —— 名字打错就得从头搜一遍设备，代价太大。
    """
    if sel and session.select(sel):
        return True
    if sel:
        print("[-] 未找到匹配的设备, 请重试 (可输入 rescan 重新扫描, q 退出)")

    while True:
        line = Shell.read_line("选择设备(序号/名称/IP, rescan 重扫, q 退出): ")
        if line is None:
            return False                # Ctrl-C / Ctrl-D
        line = line.strip()
        if line in ("q", "quit"):
            return False
        if line in ("scan", "rescan"):
            session.scan()
            print(session.device_list_text())
            continue
        if not line:
            continue
        if session.select(line):
            return True
        print("[-] 未找到匹配的设备, 请重试 (可输入 rescan 重新扫描, q 退出)")
