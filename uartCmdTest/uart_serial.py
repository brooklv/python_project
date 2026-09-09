"""串口收发：整包读取、超时、缓冲清理。

**整包读取是硬要求**：先读满 8 字节包头，再按包头声明的长度读完 data。
少读的字节会滞留在串口缓冲里，被下一条命令误当成自己的应答，
整条流从此错位 —— 之后每一步都在读上一步的应答。
所以包读不完时直接报错，绝不截断。
"""

import struct
from typing import Callable, Optional

import serial

from uart_protocol import HEAD_LEN, MAX_DATA, Packet

# 一旦有字节开始流入，包内字节间隔超过这个时间就认为设备半路死了。
# 115200 波特率下 1024 字节只需约 90ms，1 秒余量充足。
INTER_BYTE_TIMEOUT = 1.0


class UartClient:
    """串口客户端。固定 8 数据位 / 1 停止位 / 无奇偶校验 / 无流控。"""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 30.0,
                 log: Optional[Callable[[str], None]] = None,
                 tx_checksum: bool = False):
        self.port = port
        self.baud = baud
        self.timeout = timeout          # 等第一个字节的超时
        # 发送时填真校验和。默认关：接收端忽略校验和，而 00 00 的格式
        # 实测连续 54 个循环通过，没必要拿已验证的通路去换零收益。
        self.tx_checksum = tx_checksum
        self._log = log or (lambda _m: None)
        self._ser: Optional[serial.Serial] = None

    # ---------------------------------------------------------------- 生命周期
    def open(self) -> None:
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False, rtscts=False, dsrdtr=False,
            timeout=self.timeout,
        )
        self.flush_input()
        self._log(f"✓ 串口连接成功: {self.port} @ {self.baud} bps")

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
            self._log("✓ 串口已关闭")
        self._ser = None

    def __enter__(self) -> "UartClient":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 收发
    def flush_input(self) -> None:
        """丢弃已收到但还没读走的数据。

        超时重试前、每轮循环开头都要调：设备可能在超时之后才把应答发出来，
        这些迟到的字节留着会让下一条命令从一开始就错位。
        """
        if self._ser:
            self._ser.reset_input_buffer()

    def send(self, pkt: Packet) -> bytes:
        """发送一个包，返回实际写出去的字节（供调用方打 TX 日志）。"""
        raw = pkt.pack(with_checksum=self.tx_checksum)
        self._ser.write(raw)
        self._ser.flush()               # 等硬件发完
        return raw

    def send_raw(self, raw: bytes) -> bytes:
        """直接发送字节，不经过 Packet（指令模式手输十六进制时用）。"""
        self._ser.write(raw)
        self._ser.flush()
        return raw

    def _read_exact(self, n: int, timeout: float) -> bytes:
        """尽力读满 n 字节。超时就返回已读到的部分（可能不足 n）。"""
        self._ser.timeout = timeout
        buf = bytearray()

        while len(buf) < n:
            chunk = self._ser.read(n - len(buf))
            if not chunk:               # 超时
                break
            buf += chunk

        return bytes(buf)

    def recv_raw(self, timeout: Optional[float] = None,
                 quiet: bool = False) -> Optional[bytes]:
        """读回一个完整包的原始字节。超时或包不完整返回 None。

        quiet=True 时不打印超时提示 —— 有些场景超时是正常的结束条件
        （可选的通知包没来、指令模式下应答已收完），不该报成错误。
        """
        first_wait = self.timeout if timeout is None else timeout

        head = self._read_exact(HEAD_LEN, first_wait)

        if not head:
            if not quiet:
                self._log(f"✗ 超时: 超过 {first_wait:.1f} 秒无响应")
            return None

        if len(head) < HEAD_LEN:
            self._log(f"✗ 包头不完整: 只收到 {len(head)}/{HEAD_LEN} 字节")
            return None

        # data_len 有符号，负值（查询指令的 FF FF = -1）表示无数据
        dlen = struct.unpack_from("<h", head, 6)[0]
        n = dlen if dlen > 0 else 0

        if n > MAX_DATA:
            self._log(f"✗ 包声明长度异常: {n} 字节")
            return None
        if n == 0:
            return head

        body = self._read_exact(n, INTER_BYTE_TIMEOUT)
        if len(body) < n:
            self._log(f"✗ 包数据不完整: {len(body)}/{n} 字节")
            return None

        return head + body

    def recv(self, timeout: Optional[float] = None,
             quiet: bool = False) -> Optional[tuple]:
        """读一个包并解析。返回 ``(Packet, 原始字节)``，失败返回 None。

        原始字节一并返回，让调用方能打完整的 RX 十六进制日志 ——
        包括解析失败的情况，那时候原始数据恰恰最有诊断价值。
        """
        raw = self.recv_raw(timeout, quiet)
        if raw is None:
            return None

        try:
            return Packet.unpack(raw), raw
        except ValueError as e:
            self._log(f"✗ 解析应答失败: {e}")
            return None
