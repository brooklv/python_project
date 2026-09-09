"""指令表（-i 指令模式）的回归测试。不需要 pyserial，也不需要接设备。

    python3 -m unittest discover -v
    python3 test_commands.py

这张表是人手点着发命令用的，所以卡的是"别让人一按就踩雷"：

* 有副作用的必须标 ``!!``（改持久配置、重启、**开 log**）
* 没有合理默认值的必须现场问（SSID、密码），不能摆一个示例值假装能发
* Remote 指令必须标 TAG —— 发错对象时症状只是超时，光看名字分不出来
"""

import unittest

import uart_commands as cmds
import uart_stress_test as app
from uart_protocol import Cmd, Packet, Tag, parse_hex, tag_bytes


class TestTableIntegrity(unittest.TestCase):
    def test_every_entry_has_group_and_name(self):
        for c in cmds.COMMANDS:
            self.assertTrue(c.group, f"{c.name} 没有分组")
            self.assertTrue(c.name.strip(), f"{c.cmd!r} 没有名字")

    def test_names_unique_within_group(self):
        """同组内重名会让人分不清该按哪个。"""
        seen = set()
        for c in cmds.COMMANDS:
            key = (c.group, c.name)
            self.assertNotIn(key, seen, f"重名: {c.group} / {c.name}")
            seen.add(key)

    def test_hex_is_derived_not_hardcoded(self):
        """十六进制必须能反解回同一条命令。

        跟 C 版的区别就在这儿：那边是手写字符串，命令码一改就脱节。
        """
        for c in cmds.COMMANDS:
            if c.ask:
                continue                # 没有固定字节
            with self.subTest(name=c.name):
                pkt = Packet.unpack(parse_hex(c.hex))
                self.assertEqual(pkt.cmd_id, int(c.cmd))
                self.assertEqual(pkt.data, c.data)
                self.assertEqual(pkt.tag, tag_bytes(c.tag))

    def test_grouped_covers_everything(self):
        flat = [c for items in cmds.grouped().values() for _i, c in items]
        self.assertEqual(len(flat), len(cmds.COMMANDS))

    def test_grouped_numbering_matches_list_order(self):
        """显示的编号就是用户输入的编号，必须和 COMMANDS 下标对得上。"""
        for items in cmds.grouped().values():
            for i, c in items:
                self.assertIs(cmds.COMMANDS[i - 1], c)


class TestDangerMarking(unittest.TestCase):
    """有副作用的指令必须一眼看得出来。"""

    def test_danger_entries_are_labelled(self):
        for c in cmds.COMMANDS:
            if c.danger:
                self.assertTrue(c.label.startswith("!! "),
                                f"{c.name} 标了 danger 但 label 没有 !!")

    def test_enabling_log_is_danger(self):
        """开 log 之后设备不再回应任何 UART 命令，连"关闭 log"都发不进去。

        所以开 log 的必须标 !!；关 log 是安全方向，不该标。
        """
        for c in cmds.COMMANDS:
            if c.cmd != Cmd.SET_LOG_STATUS or not c.data:
                continue
            with self.subTest(name=c.name):
                if c.data[0] != 0:      # 0 = 全关
                    self.assertTrue(c.danger, f"{c.name} 会开 log，必须标 !!")
                else:
                    self.assertFalse(c.danger, f"{c.name} 是关 log，不该标 !!")

    def test_reboot_and_reset_are_danger(self):
        for c in cmds.COMMANDS:
            if c.cmd == Cmd.RESET_TO_DEFAULT or c.data == b"reboot":
                self.assertTrue(c.danger, f"{c.name} 必须标 !!")


class TestAskEntries(unittest.TestCase):
    """SSID / 密码这类没有合理默认值的，必须现场问。

    原来表里写死了示例值（SSID=L-5G / psk=13456789 / abcd / 12345678），
    选中直接就发出去 —— 结果是连一个不存在的网络，或者把设备热点
    改成示例值。
    """

    def test_every_ask_has_a_handler(self):
        for c in cmds.COMMANDS:
            if c.ask:
                self.assertIn(c.ask, app.ASK_HANDLERS,
                              f"{c.name} 的 ask={c.ask!r} 没有对应 handler")

    def test_no_orphan_handlers(self):
        used = {c.ask for c in cmds.COMMANDS if c.ask}
        self.assertEqual(set(app.ASK_HANDLERS), used)

    def test_ask_entries_carry_no_fixed_data(self):
        """data 和 ask 并存会让人以为那份 data 会被发出去。"""
        for c in cmds.COMMANDS:
            if c.ask:
                self.assertEqual(c.data, b"", f"{c.name} 同时有 data 和 ask")
                self.assertEqual(c.hex, "", f"{c.name} 是 ask 类，不该有固定字节")

    def test_credential_commands_all_ask(self):
        """凡是**写** SSID / 密码的，都必须是 ask 类。

        查询（data 为空）不算 —— 它不带值。
        """
        for c in cmds.COMMANDS:
            if c.cmd in (Cmd.AP_SSID, Cmd.AP_PWD) and (c.data or c.ask):
                self.assertTrue(c.ask, f"{c.name} 在写热点凭据，值必须现场问")

    def test_connect_command_asks(self):
        connect = [c for c in cmds.COMMANDS if c.ask == "wifi_connect"]
        self.assertEqual(len(connect), 1)
        self.assertEqual(connect[0].cmd, Cmd.NET_STA_CTRL)


class TestRemoteEntries(unittest.TestCase):
    def test_remote_entries_show_tag(self):
        """发错对象时症状只是超时，光看名字分不出来，所以必须标 TAG。"""
        for c in cmds.COMMANDS:
            if c.tag != Tag.TG:
                self.assertIn(f"[{Tag(c.tag).name}]", c.label,
                              f"{c.name} 是 Remote 指令但 label 没标 TAG")

    def test_local_entries_have_no_tag_suffix(self):
        """本地指令是绝大多数，标 TAG 只会刷屏。"""
        for c in cmds.COMMANDS:
            if c.tag == Tag.TG:
                self.assertNotIn("[", c.label, f"{c.name} 多标了 TAG")

    def test_remote_bytes_match_ptp(self):
        """字节和 Remote_Rx.ptp / Remote_Tx.ptp 一致。"""
        want = {
            (int(Cmd.WIFI_MAC_ADDR), b""): "50 4C 00 00 10 82 FF FF",
            (int(Cmd.TX_SSID), b""):       "50 4C 00 00 0D 82 FF FF",
            (int(Cmd.ROTATION), b""):      "50 4C 00 00 23 88 FF FF",
            (int(Cmd.SET_OVERSCAN), b""):  "50 4C 00 00 29 88 FF FF",
        }
        for c in cmds.COMMANDS:
            if c.tag != Tag.PL:
                continue
            key = (int(c.cmd), c.data)
            if key in want:
                with self.subTest(name=c.name):
                    self.assertEqual(c.hex, want[key])


if __name__ == "__main__":
    unittest.main(verbosity=2)
