"""角色模型的回归测试。不需要 pyserial，也不需要接设备。

    python3 -m unittest discover -v
    python3 test_role.py

这里卡的是**谁开热点、谁连路由**这条推导 —— 搞错了整批 WiFi 测试会
跑到不该跑的设备上，而症状是全部超时，看起来像设备坏了。
"""

import unittest

import uart_tests
from uart_exec import Test, filter_by_setup, filter_tests
from uart_role import (
    ALL_SETUPS, Role, Setup, Topology, menu_lines, parse_setup,
)


def _noop(_ctx):
    pass


class TestTopologyRule(unittest.TestCase):
    """一对一 Rx 开热点、一对多 Tx 开热点，开热点的同时连路由。"""

    # (role, topology) -> 开热点
    EXPECT_AP = {
        (Role.RX, Topology.ONE_TO_ONE): True,
        (Role.TX, Topology.ONE_TO_ONE): False,
        (Role.TX, Topology.ONE_TO_MANY): True,
        (Role.RX, Topology.ONE_TO_MANY): False,
    }

    def test_who_is_ap(self):
        for (role, topo), want in self.EXPECT_AP.items():
            with self.subTest(role=role.value, topology=topo.value):
                self.assertEqual(Setup(role, topo).is_ap, want)

    def test_ap_is_also_station(self):
        """开热点的那一方同时连路由，给自己和对端提供上网/OTA。"""
        for s in ALL_SETUPS:
            with self.subTest(setup=s.label):
                self.assertEqual(s.is_sta, s.is_ap)

    def test_topology_flips_the_roles(self):
        """同一个角色，换拓扑就换身份 —— 这正是必须区分拓扑的原因。"""
        tx_1to1 = Setup(Role.TX, Topology.ONE_TO_ONE)
        tx_many = Setup(Role.TX, Topology.ONE_TO_MANY)
        self.assertNotEqual(tx_1to1.is_ap, tx_many.is_ap)

        rx_1to1 = Setup(Role.RX, Topology.ONE_TO_ONE)
        rx_many = Setup(Role.RX, Topology.ONE_TO_MANY)
        self.assertNotEqual(rx_1to1.is_ap, rx_many.is_ap)

    def test_exactly_one_side_is_ap(self):
        """同一个拓扑里，Tx 和 Rx 不能都开热点，也不能都不开。"""
        for topo in Topology:
            aps = [Setup(r, topo).is_ap for r in Role]
            self.assertEqual(sum(aps), 1, f"{topo.value} 下开热点的方数不对")

    def test_peer_role(self):
        self.assertIs(Setup(Role.TX, Topology.ONE_TO_ONE).peer_role, Role.RX)
        self.assertIs(Setup(Role.RX, Topology.ONE_TO_ONE).peer_role, Role.TX)

    def test_peer_count(self):
        """只有开热点的那一方会带多个对端。"""
        self.assertEqual(Setup(Role.TX, Topology.ONE_TO_MANY).peer_count, "N")
        self.assertEqual(Setup(Role.RX, Topology.ONE_TO_MANY).peer_count, "1")
        for r in Role:
            self.assertEqual(Setup(r, Topology.ONE_TO_ONE).peer_count, "1")


class TestSetupParsing(unittest.TestCase):
    def test_both_given(self):
        s = parse_setup("rx", "1to1")
        self.assertIs(s.role, Role.RX)
        self.assertIs(s.topology, Topology.ONE_TO_ONE)

    def test_missing_either_returns_none(self):
        """缺一个就不能瞎猜 —— 猜错了是整批测试超时，得去提示用户。"""
        self.assertIsNone(parse_setup("rx", None))
        self.assertIsNone(parse_setup(None, "1to1"))
        self.assertIsNone(parse_setup(None, None))

    def test_bad_value_raises(self):
        with self.assertRaises(ValueError):
            parse_setup("sink", "1to1")
        with self.assertRaises(ValueError):
            parse_setup("rx", "1to9")

    def test_all_setups_covers_every_combination(self):
        self.assertEqual(len(ALL_SETUPS), len(Role) * len(Topology))
        self.assertEqual(len({(s.role, s.topology) for s in ALL_SETUPS}),
                         len(ALL_SETUPS))

    def test_menu_lists_ap_setups_first(self):
        """能跑完整 WiFi 流程的排前面，选的人一眼看到。"""
        flags = [s.is_ap for s in ALL_SETUPS]
        self.assertEqual(flags, sorted(flags, reverse=True))

    def test_menu_lines_mention_every_setup(self):
        text = "\n".join(menu_lines())
        for s in ALL_SETUPS:
            self.assertIn(s.label, text)


class TestAppliesTo(unittest.TestCase):
    AP = Setup(Role.RX, Topology.ONE_TO_ONE)        # 开热点 + 连路由
    CLIENT = Setup(Role.TX, Topology.ONE_TO_ONE)    # 都不是

    def test_no_prerequisite_applies_everywhere(self):
        t = Test("x", _noop)
        for s in ALL_SETUPS:
            self.assertTrue(t.applies_to(s))

    def test_needs_sta(self):
        t = Test("扫描", _noop, needs_sta=True)
        self.assertTrue(t.applies_to(self.AP))
        self.assertFalse(t.applies_to(self.CLIENT))

    def test_needs_ap(self):
        t = Test("改热点", _noop, needs_ap=True)
        self.assertTrue(t.applies_to(self.AP))
        self.assertFalse(t.applies_to(self.CLIENT))

    def test_roles(self):
        t = Test("Tx 专用", _noop, roles=("tx",))
        self.assertTrue(t.applies_to(self.CLIENT))      # CLIENT 是 Tx
        self.assertFalse(t.applies_to(self.AP))         # AP 是 Rx

    def test_no_setup_means_no_filtering(self):
        """没指定角色时一律照跑 —— 不能因为加了这个维度就默默少测。"""
        t = Test("x", _noop, roles=("tx",), needs_ap=True, needs_sta=True)
        self.assertTrue(t.applies_to(None))

    def test_why_excluded_is_specific(self):
        self.assertIn("TX", Test("a", _noop, roles=("tx",))
                      .why_excluded(self.AP))
        self.assertIn("热点", Test("b", _noop, needs_ap=True)
                      .why_excluded(self.CLIENT))
        self.assertIn("路由", Test("c", _noop, needs_sta=True)
                      .why_excluded(self.CLIENT))


class TestFilterBySetup(unittest.TestCase):
    def test_none_setup_passes_everything(self):
        keep, exc = filter_by_setup(uart_tests.TESTS, None)
        self.assertEqual(len(keep), len(uart_tests.TESTS))
        self.assertEqual(exc, [])

    def test_dependents_of_excluded_are_also_excluded(self):
        """依赖被排除了，依赖它的也留不住。

        否则它们会因为"依赖未通过"被判成跳过 —— 看起来像出了问题，
        其实是这台设备本来就不做这件事。
        """
        tests = [
            Test("扫描", _noop, needs_sta=True),
            Test("取结果", _noop, needs=("扫描",)),
            Test("连接", _noop, needs=("取结果",)),
            Test("无关", _noop),
        ]
        keep, exc = filter_by_setup(tests, Setup(Role.TX, Topology.ONE_TO_ONE))
        self.assertEqual([t.name for t in keep], ["无关"])

        why = dict(exc)
        self.assertIn("路由", why["扫描"])
        self.assertIn("依赖", why["取结果"])
        self.assertIn("依赖", why["连接"])   # 多层也要断掉

    def test_ap_setup_keeps_the_whole_wifi_flow(self):
        core = filter_tests(uart_tests.TESTS, ["core"])
        keep, exc = filter_by_setup(core, Setup(Role.RX, Topology.ONE_TO_ONE))
        self.assertEqual(len(keep), len(core))
        self.assertEqual(exc, [])

    def test_client_setup_drops_the_router_flow(self):
        core = filter_tests(uart_tests.TESTS, ["core"])
        keep, _exc = filter_by_setup(core, Setup(Role.TX, Topology.ONE_TO_ONE))
        names = [t.name for t in keep]
        for gone in ("扫描网络", "获取扫描结果", "连接网络", "忘记网络"):
            self.assertNotIn(gone, names)
        # 切频段是本机自己的射频设置，两种角色都做得到
        self.assertIn("切换到5G", names)

    def test_every_setup_keeps_something(self):
        """任何角色下都得有测试可跑，否则这个角色等于不支持。"""
        for s in ALL_SETUPS:
            keep, _ = filter_by_setup(uart_tests.TESTS, s)
            self.assertGreater(len(keep), 0, f"{s.label} 一条都跑不了")

    def test_excluded_plus_kept_is_everything(self):
        """不能有测试既没保留也没记进排除列表 —— 那就是静默丢失。"""
        for s in ALL_SETUPS:
            keep, exc = filter_by_setup(uart_tests.TESTS, s)
            self.assertEqual(len(keep) + len(exc), len(uart_tests.TESTS),
                             f"{s.label} 有测试被静默丢掉")


class TestCredentialsRespectRole(unittest.TestCase):
    """-s/-w 只有在真会用到时才该要求。

    这是踩过的坑的重演：以前用 tag 推断凭据需求，导致 --tags query
    也被要求给 -s/-w。角色维度会重新引入同一个陷阱 —— 一台不连路由的
    设备压根不跑"连接网络"，却被要求给凭据就很莫名。
    """

    def test_client_role_needs_no_credentials(self):
        import uart_stress_test as app

        args = app.parse_args(["-p", "X", "--role", "tx",
                               "--topology", "1to1", "--tags", "core"])
        setup = app.resolve_setup(args)
        tests, _exc = app.select_tests(args, setup)
        self.assertEqual([t.name for t in tests if t.needs_creds], [])
        app.check_creds(args, tests)            # 不该抛

    def test_ap_role_still_needs_credentials(self):
        import uart_stress_test as app

        args = app.parse_args(["-p", "X", "--role", "rx",
                               "--topology", "1to1", "--tags", "core"])
        setup = app.resolve_setup(args)
        tests, _exc = app.select_tests(args, setup)
        with self.assertRaises(SystemExit):
            app.check_creds(args, tests)


class TestRoleTaggedTests(unittest.TestCase):
    """测试集里的角色标注要自洽。"""

    def test_router_flow_marked_needs_sta(self):
        """扫描/连接/忘记网络这套只有连路由的一方做得到。"""
        by_name = {t.name: t for t in uart_tests.TESTS}
        for name in ("扫描网络", "获取扫描结果", "连接网络", "忘记网络"):
            self.assertTrue(by_name[name].needs_sta,
                            f"{name} 是连路由的流程，必须标 needs_sta")

    def test_ap_config_marked_needs_ap(self):
        """读写本机热点参数的，必须标 needs_ap。"""
        want = {"查询 热点SSID", "查询 热点密码",
                "改SSID为abcd", "改密码为12345678"}
        by_name = {t.name: t for t in uart_tests.TESTS}
        for name in want:
            self.assertIn(name, by_name, f"{name} 不在测试集里了，请同步本测试")
            self.assertTrue(by_name[name].needs_ap,
                            f"{name} 在读写本机热点，必须标 needs_ap")

    def test_roles_values_are_valid(self):
        valid = {r.value for r in Role}
        for t in uart_tests.TESTS:
            for r in t.roles:
                self.assertIn(r, valid, f"{t.name} 的 roles 里有未知值 {r!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
