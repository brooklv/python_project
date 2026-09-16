"""设备描述解析与 SSDP 应答解析的回归测试。不发任何组播。

    python3 -m unittest test_discovery -v

这里卡的是**相对 controlURL 的拼接**。设备描述里写 "ctl/AVTransport"
是完全合法的，拼错了就会拿去 POST 一个不存在的地址，报出来是连接失败，
看起来像设备不在线 —— 和真的掉线分不开。
"""

import unittest

from dlna_device import Renderer, parse_description
from dlna_ssdp import Discovery, parse_headers

DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  {urlbase}
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <UDN>uuid:{udn}</UDN>
    <serviceList>
      {services}
    </serviceList>
  </device>
</root>"""

SVC = """<service>
  <serviceType>{stype}</serviceType>
  <serviceId>urn:upnp-org:serviceId:{sid}</serviceId>
  <controlURL>{ctrl}</controlURL>
</service>"""

AV = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"


def desc(services, urlbase="", name="ZIP Pro", udn="abc"):
    return DESC.format(
        urlbase=f"<URLBase>{urlbase}</URLBase>" if urlbase else "",
        name=name, udn=udn,
        services="".join(SVC.format(stype=s, sid=s.split(":")[-2], ctrl=c)
                         for s, c in services),
    ).encode()


class TestParseDescription(unittest.TestCase):

    LOC = "http://192.168.50.69:60099/dd.xml"

    def test_relative_control_url_resolves_against_urlbase(self):
        r = parse_description(self.LOC, desc([(AV, "ctl/AVTransport")],
                                             urlbase="http://192.168.50.69:60099/"))
        self.assertEqual(r.av_control, "http://192.168.50.69:60099/ctl/AVTransport")

    def test_relative_control_url_without_urlbase_uses_location(self):
        """没有 URLBase 时基址是描述文档自身的地址 —— 少了这条会拼出错的路径。"""
        r = parse_description(self.LOC, desc([(AV, "ctl/AVTransport")]))
        self.assertEqual(r.av_control, "http://192.168.50.69:60099/ctl/AVTransport")

    def test_absolute_path_control_url(self):
        r = parse_description(self.LOC, desc([(AV, "/upnp/control/av")]))
        self.assertEqual(r.av_control, "http://192.168.50.69:60099/upnp/control/av")

    def test_full_url_control_url_is_kept(self):
        r = parse_description(self.LOC, desc([(AV, "http://10.0.0.5:80/av")]))
        self.assertEqual(r.av_control, "http://10.0.0.5:80/av")

    def test_both_services(self):
        r = parse_description(self.LOC, desc([(AV, "/av"), (RC, "/rc")]))
        self.assertTrue(r.has_av)
        self.assertTrue(r.has_rc)

    def test_without_avtransport_is_rejected(self):
        """没有 AVTransport 就推不了流，列出来只会让人白选一次。"""
        self.assertIsNone(parse_description(self.LOC, desc([(RC, "/rc")])))

    def test_missing_udn_is_rejected(self):
        """没有 UDN 就没法去重，同一台设备会在列表里出现好几次。"""
        bad = b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device/></root>'
        self.assertIsNone(parse_description(self.LOC, bad))

    def test_garbage_xml_is_rejected(self):
        self.assertIsNone(parse_description(self.LOC, b"<html>404</html>ne"))

    def test_unnamed_device_gets_placeholder(self):
        raw = desc([(AV, "/av")]).replace(b"<friendlyName>ZIP Pro</friendlyName>", b"")
        self.assertEqual(parse_description(self.LOC, raw).friendly, "(未命名设备)")

    def test_host_property(self):
        r = parse_description(self.LOC, desc([(AV, "/av")]))
        self.assertEqual(r.host, "192.168.50.69:60099")


class TestSsdpHeaders(unittest.TestCase):

    RESP = (b"HTTP/1.1 200 OK\r\n"
            b"CACHE-CONTROL: max-age=1800\r\n"
            b"LOCATION: http://192.168.50.69:60099/dd.xml\r\n"
            b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
            b"USN: uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")

    def test_keys_are_lowercased(self):
        """各家设备大小写写法不一，按原样取 header 会漏掉一半设备。"""
        h = parse_headers(self.RESP)
        self.assertEqual(h["location"], "http://192.168.50.69:60099/dd.xml")
        self.assertIn("usn", h)

    def test_status_line_is_not_a_header(self):
        self.assertNotIn("http/1.1 200 ok", parse_headers(self.RESP))

    def test_garbage_does_not_raise(self):
        self.assertEqual(parse_headers(b"\xff\xfe not http at all"), {})


class TestPick(unittest.TestCase):

    def setUp(self):
        self.d = Discovery()
        for i, (name, ip) in enumerate([("客厅电视", "192.168.50.10"),
                                        ("ZIP Pro_5603061B", "192.168.50.69")]):
            self.d.add(Renderer(udn=f"uuid:{i}", friendly=name,
                                 location=f"http://{ip}:60099/dd.xml",
                                 base=f"http://{ip}:60099/",
                                 av_type=AV, av_control=f"http://{ip}/av"))

    def test_pick_by_index(self):
        self.assertEqual(self.d.pick("0").friendly, "客厅电视")
        self.assertEqual(self.d.pick("1").friendly, "ZIP Pro_5603061B")

    def test_pick_by_name_substring(self):
        self.assertEqual(self.d.pick("客厅").friendly, "客厅电视")
        self.assertEqual(self.d.pick("ZIP").friendly, "ZIP Pro_5603061B")

    def test_pick_by_ip(self):
        """序号会随搜索顺序变；想稳定指向一台设备只能靠 IP。"""
        self.assertEqual(self.d.pick("192.168.50.69").friendly, "ZIP Pro_5603061B")

    def test_out_of_range_index_is_none(self):
        self.assertIsNone(self.d.pick("9"))

    def test_unknown_is_none(self):
        self.assertIsNone(self.d.pick("卧室"))
        self.assertIsNone(self.d.pick(""))

    def test_duplicate_udn_is_ignored(self):
        """同一台设备会应答很多次（两个 ST × 重发），不去重列表会全是重复项。"""
        before = len(self.d)
        self.d.add(Renderer(udn="uuid:0", friendly="客厅电视(重复)",
                             location="http://x/dd.xml", base="http://x/",
                             av_type=AV, av_control="http://x/av"))
        self.assertEqual(len(self.d), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
