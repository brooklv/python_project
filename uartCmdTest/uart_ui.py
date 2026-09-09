"""日志、终端配色、底部状态栏。

设计要点：

* 日志文件里始终是**完整数据且不带颜色码**，终端才截断和上色。
  否则 grep 日志会被转义序列干扰。
* 颜色和状态栏只在 stdout 是终端时启用，重定向/管道时自动关闭。
* 状态栏是**上下文管理器**：``with StatusBar() as bar:``。
  ``with`` 的 try/finally 语义保证退出时一定恢复终端，
  包括 Ctrl+C（KeyboardInterrupt）—— 不需要注册信号处理函数。
"""

import shutil
import sys
from datetime import datetime
from typing import Optional, TextIO

# ---------------------------------------------------------------- 颜色
C_TX   = "\033[36m"     # 发送: 青色
C_RX   = "\033[33m"     # 接收: 黄色
C_OK   = "\033[32m"     # ✓ 绿
C_ERR  = "\033[31m"     # ✗ 红
C_WARN = "\033[1;33m"   # ⚠ 亮黄，跟 RX 的普通黄区分开
C_STOP = "\033[35m"     # ■ 洋红
C_OFF  = "\033[0m"

_MARKER_COLOR = {"✓": C_OK, "✗": C_ERR, "⚠": C_WARN, "■": C_STOP}

HEX_COLS = 0            # 每行显示多少字节。0 = 按终端宽度自适应；
                        # 想固定就改成 16 / 32 / 64
HEX_GROUP = 8           # 每几个字节一组，组间空一格，方便数偏移

_color = False
_log_file: Optional[TextIO] = None


def force_utf8() -> None:
    """把 stdout/stderr 强制成 UTF-8。

    树莓派上 LANG 常常没设成 UTF-8，Python 会把 stdout 的编码猜成
    latin-1，打印中文直接抛 UnicodeEncodeError。终端本身能显示 UTF-8，
    只是 Python 的 locale 探测不准，所以这里直接指定。

    必须在**任何输出之前**调用，包括 argparse 的 -h 帮助文本。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass        # 不是 TextIOWrapper，或已经有内容写出去了


def init(log_path: Optional[str] = None) -> None:
    global _color, _log_file

    force_utf8()
    _color = sys.stdout.isatty()

    if log_path:
        _log_file = open(log_path, "w", encoding="utf-8", buffering=1)


def close() -> None:
    global _log_file
    if _log_file:
        _log_file.close()
        _log_file = None


def _stamp() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S.") + \
        f"{datetime.now().microsecond // 1000:03d}"


def log(msg: str) -> None:
    """写日志：终端按开头的标记符号上色，文件里是纯文本。"""
    color = _MARKER_COLOR.get(msg.lstrip("\n")[:1]) if _color else None

    if color:
        print(f"{color}{msg}{C_OFF}")
    else:
        print(msg)

    if _log_file:
        _log_file.write(f"{_stamp()} {msg}\n")


def _ascii_col(chunk: bytes) -> str:
    """可打印字符原样显示，其余一律 '.'，跟 hexdump 一致。"""
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)


def _hex_line_width(cols: int) -> int:
    """一行 hexdump 占多少显示列：前缀 5 + 十六进制 + 分隔 1 + |ASCII|。"""
    groups = (cols + HEX_GROUP - 1) // HEX_GROUP
    hexw = cols * 3 - 1 + (groups - 1)      # 每字节 "XX " 去掉尾空格，组间各多一格
    return 5 + hexw + 1 + cols + 2


def _auto_cols() -> int:
    """按终端宽度选每行字节数，取 HEX_GROUP 的倍数，范围 16~64。

    固定 16 在宽屏上浪费竖向空间，固定 64 在 80 列终端会折行错乱，
    所以按实际宽度挑最大能放下的。
    """
    if HEX_COLS:
        return HEX_COLS
    if not sys.stdout.isatty():
        return 16                            # 重定向时没有窗口概念

    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    best = 16
    for n in range(16, 65, HEX_GROUP):
        if _hex_line_width(n) <= width:
            best = n
    return best


def _hexdump(data: bytes, cols: int):
    """按 hexdump -C 的样式逐行产出 ``(十六进制, ASCII)``。

    每 HEX_GROUP 字节一组、组间空一格。不足一组时补空格保持列对齐。
    """
    gw = HEX_GROUP * 3 - 1                   # 一组带空格占多少列

    for off in range(0, len(data), cols):
        chunk = data[off:off + cols]
        groups = [
            " ".join(f"{b:02X}" for b in chunk[g:g + HEX_GROUP]).ljust(gw)
            for g in range(0, cols, HEX_GROUP)
        ]
        yield "  ".join(groups), _ascii_col(chunk)


def log_hex(prefix: str, data: bytes) -> None:
    """写十六进制。

    终端按 hexdump 样式**完整**显示，右边带 ASCII 对照，
    每行字节数按终端宽度自适应。

    日志文件里保持**单行完整十六进制** —— 多行会破坏
    ``grep '[TX]'`` / ``sort | uniq -c`` 这类分析，而排查流错位时
    那是最有效的手段。
    """
    if not data:
        return

    # 日志文件：单行，完整，便于 grep 和去重统计
    if _log_file:
        _log_file.write(f"{_stamp()} [{prefix}] {data.hex(' ').upper()}\n")

    if _color:
        color, off = (C_TX if prefix == "TX" else C_RX), C_OFF
    else:
        color, off = "", ""

    cols = _auto_cols()

    for i, (hx, txt) in enumerate(_hexdump(data, cols)):
        tag = f"[{prefix}]" if i == 0 else "    "
        print(f"{color}{tag} {hx} |{txt}|{off}")


def _disp_width(s: str) -> int:
    """字符串占多少个终端显示列。

    不能拿 len() 当宽度：中文一个字占 2 列，右对齐会偏。
    """
    return sum(2 if ord(c) > 0x1100 else 1 for c in s)


class StatusBar:
    """屏幕底部的状态栏。

    用 ANSI 滚动区把最后一行排除在滚动之外，日志在上方正常滚动，
    状态栏原地刷新。

    退出时必须恢复滚动区，否则终端会一直卡在"最后一行不滚动"。
    而且 ``\\033[r``（重置滚动区）按 DECSTBM 规范会把光标归位到左上角，
    所以后面必须紧跟着把光标移回底部 —— 不然退出后 shell 提示符会
    出现在第 1 行、覆盖已有内容，整屏看起来是脏的。
    """

    def __init__(self) -> None:
        self.on = False
        self.rows = 0
        self.cols = 0

    def __enter__(self) -> "StatusBar":
        if not sys.stdout.isatty():
            return self

        self._measure()
        if self.rows < 3 or self.cols < 24:       # 太小了放不下
            return self

        self.on = True
        # 滚动区 = 第 1 行 ~ 倒数第 2 行，光标移到滚动区底部
        sys.stdout.write(f"\033[1;{self.rows - 1}r\033[{self.rows - 1};1H")
        sys.stdout.flush()
        return self

    def __exit__(self, *_exc) -> None:
        if not self.on:
            return
        self.on = False
        # 重置滚动区 → 光标移到最后一行 → 清掉状态栏
        sys.stdout.write(f"\033[r\033[{self.rows};1H\033[K")
        sys.stdout.flush()

    def _measure(self) -> None:
        size = shutil.get_terminal_size(fallback=(80, 24))
        self.cols, self.rows = size.columns, size.lines

    def draw(self, **stats) -> None:
        """刷新状态栏。stats 以 ``键=值`` 形式给出，按顺序显示。"""
        if not self.on:
            return

        rows_was = self.rows
        self._measure()
        if self.rows != rows_was:                 # 终端被改过大小
            sys.stdout.write(f"\033[1;{self.rows - 1}r")

        txt = " " + " | ".join(f"{k} {v}" for k, v in stats.items()) + " "
        col = max(1, self.cols - _disp_width(txt) + 1)

        sys.stdout.write(
            "\033[s"                              # 存光标
            f"\033[{self.rows};1H\033[K"          # 到状态栏行并清空
            f"\033[{self.rows};{col}H\033[7m{txt}\033[0m"   # 右下角反显
            "\033[u"                              # 复原光标
        )
        sys.stdout.flush()


def ask_continue(what: str) -> bool:
    """该收到的可选应答没收到时，停下来问一句。返回 True 继续。

    stdin 不是终端时（后台跑、重定向）没法交互，记录告警后按继续处理，
    不会把无人值守的长跑卡死。
    """
    log(f"⚠ {what}")

    if not sys.stdin.isatty():
        log("  (非交互环境，自动继续)")
        return True

    while True:
        try:
            ans = input("  >> 是否继续测试? [y=继续 / n=中止]: ").strip().lower()
        except EOFError:
            print()
            log("  (stdin 已关闭，按继续处理)")
            return True

        if ans.startswith("y"):
            log("  -> 用户选择继续")
            return True
        if ans.startswith("n"):
            log("  -> 用户选择中止")
            return False
