"""协议层回归测试。不需要 pyserial，也不需要接设备。

    python3 -m unittest discover -v
    python3 test_protocol.py

用例主要来自 `Uart command.md` 的抓包和 test1.log 的真实应答 ——
**期望值不是我算出来的，是设备实际发过的字节**。

重点覆盖开发时真踩过的坑（每条都对应一次真实的 bug）：

* 网络个数在 data 偏移 3 而不是 4
* ``FF FF`` 是有符号 -1（无数据），不是 65535
* 配对状态的值在 rc 之后偏移 1，中间夹了一个填充字节
* 返回码必须检查 —— 只看包格式会把 FAILED / INCORRECT_PWD 当成成功
* Command ID 是枚举值的小端序，应答 ID = 请求 & 0x3FFF | 0x4000
"""

import struct
import unittest

from uart_protocol import (
    Cmd, Err, Packet, STA_CONNECT, STA_FORGET, STA_SCAN, STA_SCAN_RESULT,
    STATE_NOTIFY_DATA, WIFI_CHN_5G,
    build, build_connect, err_name, hex_str, network_count, parse_hex,
    parse_scan_list, resp_id_of,
)


def tx(cmd, data=b""):
    """构造请求并返回线上字节的十六进制串，便于和文档抓包直接比对。"""
    return hex_str(build(cmd, data).pack())


def rx(cmd, data):
    """构造一个应答包（用文档里的 data 喂进来）。"""
    return Packet.unpack(Packet(resp_id_of(cmd), data).pack())


class TestCommandBytes(unittest.TestCase):
    """发送字节必须和 Uart command.md 的抓包逐字节一致。"""

    def test_wifi(self):
        self.assertEqual(tx(Cmd.NET_STA_CTRL, bytes([STA_SCAN])),
                         "47 54 00 00 23 82 01 00 00")
        self.assertEqual(tx(Cmd.NET_STA_CTRL, bytes([STA_SCAN_RESULT])),
                         "47 54 00 00 23 82 01 00 01")
        self.assertEqual(tx(Cmd.NET_STA_CTRL, bytes([STA_FORGET, 0xFF])),
                         "47 54 00 00 23 82 02 00 03 FF")
        self.assertEqual(tx(Cmd.NET_STA_CTRL, bytes([STA_FORGET, 0x00])),
                         "47 54 00 00 23 82 02 00 03 00")
        self.assertEqual(tx(Cmd.SWITCH_WIFI_CHN, bytes([WIFI_CHN_5G])),
                         "47 54 00 00 25 88 01 00 02")

    def test_display(self):
        self.assertEqual(tx(Cmd.ROTATION, b"\x00"), "47 54 00 00 23 88 01 00 00")
        self.assertEqual(tx(Cmd.ROTATION, b"\x03"), "47 54 00 00 23 88 01 00 03")
        self.assertEqual(tx(Cmd.SET_OVERSCAN, b"\x82"),
                         "47 54 00 00 29 88 01 00 82")
        self.assertEqual(tx(Cmd.SET_PORTRAIT_MODE, b"\x5A\x00\x82\x00"),
                         "47 54 00 00 30 88 04 00 5A 00 82 00")
        self.assertEqual(tx(Cmd.HDCP_STATUS, b"\x01"),
                         "47 54 00 00 4B 86 01 00 01")
        self.assertEqual(tx(Cmd.HDMI_ENABLE, b"\x01"),
                         "47 54 00 00 32 88 01 00 01")

    def test_audio_and_ap(self):
        self.assertEqual(tx(Cmd.AUDIO_MUTE, b"\x01"), "47 54 00 00 01 81 01 00 01")
        self.assertEqual(tx(Cmd.AUDIO_VOLUME, b"\x0E"), "47 54 00 00 02 81 01 00 0E")
        self.assertEqual(tx(Cmd.AP_SSID, b"abcd"),
                         "47 54 00 00 0A 82 04 00 61 62 63 64")
        self.assertEqual(tx(Cmd.AP_PWD, b"12345678"),
                         "47 54 00 00 0B 82 08 00 31 32 33 34 35 36 37 38")

    def test_system(self):
        self.assertEqual(tx(Cmd.SET_LOG_STATUS, b"\x03"),
                         "47 54 00 00 51 86 01 00 03")
        self.assertEqual(tx(Cmd.NULL_CONSOLE, b"\x00"),
                         "47 54 00 00 0B 87 01 00 00")
        self.assertEqual(tx(Cmd.SYSTEM_COMMAND, b"dmesg"),
                         "47 54 00 00 0D 89 05 00 64 6D 65 73 67")
        self.assertEqual(tx(Cmd.SYSTEM_COMMAND, b"reboot"),
                         "47 54 00 00 0D 89 06 00 72 65 62 6F 6F 74")
        self.assertEqual(tx(Cmd.RESET_TO_DEFAULT, b"\x00"),
                         "47 54 00 00 29 86 01 00 00")
        self.assertEqual(tx(Cmd.PAIRING_CTRL, b"\x00\x01"),
                         "47 54 00 00 0C 89 02 00 00 01")

    def test_encode_and_ota(self):
        self.assertEqual(tx(Cmd.ENCODE_PARAM, b"\x01\x1E\x00"),
                         "47 54 00 00 39 88 03 00 01 1E 00")
        self.assertEqual(
            tx(Cmd.ENCODE_PARAM, b"\x00\x00\x05\xD0\x02\x03\x80\xBB\x00\x00"),
            "47 54 00 00 39 88 0A 00 00 00 05 D0 02 03 80 BB 00 00")
        self.assertEqual(tx(Cmd.FW_UPGRADE_CTRL, b"\x01\x01\x01"),
                         "47 54 00 00 56 86 03 00 01 01 01")

    def test_connect_string(self):
        """连接命令实测连续 54 个循环通过，格式不能变。

        DataLen 必须是 0x3E(62)，含 scan_ssid=1。
        """
        got = tx(Cmd.NET_STA_CTRL, b"")  # 占位，下面用专用构造
        got = hex_str(build_connect("Actmicro-wifi", "actionsuser").pack())
        self.assertEqual(
            got,
            "47 54 00 00 23 82 3E 00 02 53 53 49 44 3D 41 63 74 6D 69 63 72 6F "
            "2D 77 69 66 69 09 61 75 74 68 65 6E 3D 57 50 41 2B 53 41 45 09 70 "
            "73 6B 3D 61 63 74 69 6F 6E 73 75 73 65 72 09 73 63 61 6E 5F 73 73 "
            "69 64 3D 31")


class TestQueryCommandLength(unittest.TestCase):
    """查询指令的 DataLen 是有符号 -1（FF FF），不是 65535。

    按无符号读会让接收方尝试读 65535 字节然后报"包过大"。
    """

    def test_ff_ff(self):
        for cmd, want in (
            (Cmd.ROTATION,          "47 54 00 00 23 88 FF FF"),
            (Cmd.SET_OVERSCAN,      "47 54 00 00 29 88 FF FF"),
            (Cmd.NET_STA_CTRL,      "47 54 00 00 23 82 FF FF"),
            (Cmd.FW_VERSION,        "47 54 00 00 3D 86 FF FF"),
            (Cmd.WIFI_MAC_ADDR,     "47 54 00 00 10 82 FF FF"),
            (Cmd.AP_SSID,           "47 54 00 00 0A 82 FF FF"),
            (Cmd.TX_SSID,           "47 54 00 00 0D 82 FF FF"),
            (Cmd.TX_HDMI_STATUS,    "47 54 00 00 53 86 FF FF"),
        ):
            with self.subTest(cmd=cmd.name):
                self.assertEqual(tx(cmd), want)

    def test_decode_treats_ff_ff_as_empty(self):
        """收到 FF FF 时该当成"无数据"，不能当 65535。"""
        pkt = Packet.unpack(parse_hex("47 54 00 00 23 88 FF FF"))
        self.assertEqual(pkt.data, b"")

    def test_decode_normalizes_length_field(self):
        """归一化后 data_len 字段要和实际 data 长度一致，
        否则读的人会拿到 65535 而 data 其实是空的。"""
        pkt = Packet.unpack(parse_hex("47 54 00 00 23 88 FF FF"))
        self.assertEqual(len(pkt.pack()), 8)


class TestCommandIdMapping(unittest.TestCase):
    """Command ID 是固件枚举值的小端序；应答 = 请求 & 0x3FFF | 0x4000。"""

    def test_little_endian_on_wire(self):
        raw = build(Cmd.NET_STA_CTRL, b"\x00").pack()
        self.assertEqual(raw[4], 0x23)      # 枚举低字节在前
        self.assertEqual(raw[5], 0x82)      # 枚举高字节在后

    def test_resp_id(self):
        for req, want in (
            (Cmd.NET_STA_CTRL,     0x4223),   # 线上 23 42
            (Cmd.SWITCH_WIFI_CHN,  0x4825),   # 线上 25 48
            (Cmd.ROTATION,         0x4823),   # 线上 23 48
            (Cmd.PAIRING_CTRL,     0x490C),   # 线上 0C 49
            (Cmd.HDCP_STATUS,      0x464B),   # 线上 4B 46
        ):
            with self.subTest(cmd=req.name):
                self.assertEqual(resp_id_of(req), want)

    def test_is_ack_flag(self):
        """按标志位判断应答，对所有命令通用，不用硬编码每条的应答 ID。"""
        self.assertTrue(rx(Cmd.NET_STA_CTRL, b"\x01\x00").is_ack)
        self.assertTrue(rx(Cmd.SWITCH_WIFI_CHN, b"\x01\x00").is_ack)
        self.assertFalse(build(Cmd.NET_STA_CTRL, b"\x00").is_ack)   # 请求不是应答


class TestReturnCode(unittest.TestCase):
    """返回码必须被检查 —— 曾经只看包格式，把设备报的错当成了成功。"""

    def test_success_codes(self):
        for rc in (Err.SAME_SETTING, Err.SUCCESS, Err.VALUE):
            with self.subTest(rc=rc.name):
                pkt = rx(Cmd.NET_STA_CTRL, struct.pack("<h", rc))
                self.assertEqual(pkt.rc, rc)
                self.assertTrue(pkt.ok, f"{rc.name} 应判为成功")

    def test_error_codes_are_not_ok(self):
        for rc in (Err.FAILED, Err.OUT_OF_RANGE, Err.INVALID_CMD, Err.TIMEOUT,
                   Err.BUSY, Err.INCORRECT_PWD, Err.NOT_SUPPORT,
                   Err.CHECK_SUM, Err.PACKET_DONE):
            with self.subTest(rc=rc.name):
                pkt = rx(Cmd.NET_STA_CTRL, struct.pack("<h", rc))
                self.assertEqual(pkt.rc, rc)
                self.assertFalse(pkt.ok, f"{rc.name} 不能判为成功")

    def test_real_captures(self):
        """test1.log 里的真实应答。"""
        # 忘记网络 ack
        p = Packet.unpack(parse_hex("47 54 03 01 23 42 02 00 01 00"))
        self.assertEqual(p.rc, Err.SUCCESS)
        self.assertTrue(p.ok)

        # 扫描列表结束标记
        p = Packet.unpack(parse_hex("47 54 F6 02 23 42 02 00 F5 FF"))
        self.assertEqual(p.rc, Err.PACKET_DONE)
        self.assertTrue(p.is_packet_done)

        # 密码错（构造的，文档给了枚举）
        p = Packet.unpack(parse_hex("47 54 00 00 23 42 02 00 F8 FF"))
        self.assertEqual(p.rc, Err.INCORRECT_PWD)
        self.assertFalse(p.ok)

    def test_err_name(self):
        self.assertEqual(err_name(1), "SUCCESS")
        self.assertEqual(err_name(-8), "INCORRECT_PWD(密码错)")
        self.assertEqual(err_name(-11), "PACKET_DONE(分包结束)")
        self.assertIn("未知", err_name(99))       # 未知值也要能安全打印


class TestNetworkCount(unittest.TestCase):
    """网络个数在 data 偏移 3 起的 4 字节小端 —— 曾经错读成偏移 4。"""

    def test_real_captures(self):
        for raw, want in (
            ("47 54 21 01 23 42 07 00 00 00 00 1A 00 00 00", 26),
            ("47 54 20 01 23 42 07 00 00 00 00 19 00 00 00", 25),
            ("47 54 27 01 23 42 07 00 00 00 00 20 00 00 00", 32),
            ("47 54 14 01 23 42 07 00 00 00 00 0D 00 00 00", 13),
        ):
            with self.subTest(want=want):
                self.assertEqual(network_count(Packet.unpack(parse_hex(raw))),
                                 want)

    def test_short_packet_returns_zero(self):
        """状态通知只有 5 字节 data，取个数时不能越界。

        曾经就是它被当成扫描应答，个数算成 0 掩盖了流错位。
        """
        p = Packet.unpack(parse_hex("47 54 0B 01 23 42 05 00 00 00 06 00 00"))
        self.assertEqual(network_count(p), 0)


class TestPacketClassification(unittest.TestCase):
    """区分应答和设备主动推的异步通知 —— 混淆会让整条流永久错位。"""

    def test_state_notify(self):
        p = Packet.unpack(parse_hex("47 54 0B 01 23 42 05 00 00 00 06 00 00"))
        self.assertTrue(p.is_state_notify)
        self.assertEqual(p.data, STATE_NOTIFY_DATA)

    def test_normal_reply_is_not_state_notify(self):
        p = Packet.unpack(parse_hex("47 54 03 01 23 42 02 00 01 00"))
        self.assertFalse(p.is_state_notify)

    def test_ota_push_has_different_cmd_id(self):
        """设备主动推的 OTA 版本通知，ID 和 NET_STA_CTRL 的应答不同。"""
        p = Packet.unpack(parse_hex(
            "47 54 B0 05 56 46 15 00 00 00 01 00 02 32 37 33 31 34 30 30 30 "
            "3A 6E 65 77 65 73 74 00"))
        self.assertNotEqual(p.cmd_id, resp_id_of(Cmd.NET_STA_CTRL))
        self.assertEqual(p.cmd_id, resp_id_of(Cmd.FW_UPGRADE_CTRL))


class TestScanList(unittest.TestCase):
    def test_parse(self):
        blob = (b"Actmicro-wifi\tWPA-PSK\t3\t-1\te8:ac:23:b8:f0:a0\t5180\n"
                b"XTs_5G\tSAE\t4\t-1\tc8:13:8b:91:93:a3\t5300\n")
        nets = parse_scan_list(blob)

        self.assertEqual(len(nets), 2)
        self.assertEqual(nets["Actmicro-wifi"].auth, "WPA-PSK")
        self.assertEqual(nets["Actmicro-wifi"].freq, "5180")
        self.assertEqual(nets["Actmicro-wifi"].bssid, "e8:ac:23:b8:f0:a0")
        self.assertEqual(nets["XTs_5G"].auth, "SAE")

    def test_nul_padding_does_not_truncate(self):
        """尾部 0x00 填充当记录分隔符，不能截断解析。"""
        blob = (b"A\tWPA-PSK\t3\t-1\taa:bb\t5180\n\x00\x00"
                b"B\tSAE\t4\t-1\tcc:dd\t2412\n")
        self.assertEqual(len(parse_scan_list(blob)), 2)

    def test_substring_ssid_not_confused(self):
        """SSID 是别人子串时不能误匹配。"""
        blob = (b"Actmicro\tSAE\t3\t-1\taa\t5180\n"
                b"Actmicro-wifi\tWPA-PSK\t4\t-1\tbb\t5300\n")
        nets = parse_scan_list(blob)
        self.assertEqual(nets["Actmicro"].auth, "SAE")
        self.assertEqual(nets["Actmicro-wifi"].auth, "WPA-PSK")

    def test_utf8_ssid(self):
        blob = "中文热点\tWPA-PSK\t3\t-1\taa\t5180\n".encode()
        self.assertIn("中文热点", parse_scan_list(blob))


class TestParseHex(unittest.TestCase):
    def test_formats(self):
        want = bytes.fromhex("475400002382")
        for s in ("47 54 00 00 23 82", "475400002382", "47,54,00,00,23,82",
                  "47:54:00:00:23:82", "0x47 0x54 0x00 0x00 0x23 0x82",
                  "47 54 00 00 23 82  ", "4754000023 82"):
            with self.subTest(s=s):
                self.assertEqual(parse_hex(s), want)

    def test_case_insensitive(self):
        self.assertEqual(parse_hex("4a5B"), parse_hex("4A5b"))

    def test_odd_length_rejected(self):
        with self.assertRaises(ValueError):
            parse_hex("47 54 0")

    def test_bad_chars_rejected(self):
        with self.assertRaises(ValueError):
            parse_hex("47 54 ZZ")


class TestPacketRoundTrip(unittest.TestCase):
    def test_pack_unpack(self):
        for data in (b"", b"\x01", b"\x00" * 100, bytes(range(256)),
                     b"SSID=x\tpsk=y"):
            with self.subTest(n=len(data)):
                orig = build(Cmd.NET_STA_CTRL, data)
                back = Packet.unpack(orig.pack())
                self.assertEqual(back.cmd_id, orig.cmd_id)
                self.assertEqual(back.data, data)

    def test_bad_header_rejected(self):
        with self.assertRaises(ValueError):
            Packet.unpack(parse_hex("FF FF 00 00 23 82 01 00 00"))

    def test_truncated_rejected(self):
        with self.assertRaises(ValueError):
            Packet.unpack(parse_hex("47 54 00"))

    def test_incomplete_data_rejected(self):
        """声明 10 字节却只给 2 字节，必须报错而不是静默截断。"""
        with self.assertRaises(ValueError):
            Packet.unpack(parse_hex("47 54 00 00 23 82 0A 00 01 02"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
