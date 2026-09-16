"""SOAP 控制的回归测试。起一个假渲染器（只绑 127.0.0.1）真发请求。

    python3 -m unittest test_soap -v

这里卡的是**参数顺序**。SOAP 动作的入参是有序的，不是按名字匹配 ——
SetAVTransportURI 把 CurrentURI 和 CurrentURIMetaData 写反了，不少设备
只回一句没有指向性的错误码，从现象上根本看不出是顺序问题。
"""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

import dlna_soap as soap
from dlna_device import Renderer

AV_TYPE = "urn:schemas-upnp-org:service:AVTransport:1"
RC_TYPE = "urn:schemas-upnp-org:service:RenderingControl:1"

_OK_BODY = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body><u:{action}Response xmlns:u="{stype}">{args}</u:{action}Response>'
    "</s:Body></s:Envelope>"
)

_FAULT_BODY = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    "<s:Body><s:Fault><faultcode>s:Client</faultcode>"
    "<faultstring>UPnPError</faultstring><detail>"
    '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
    "<errorCode>701</errorCode>"
    "<errorDescription>Transition not available</errorDescription>"
    "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
)

# 动作 -> 出参。没列的动作回空应答。
_RESPONSES = {
    "GetPositionInfo": "<TrackDuration>0:05:13</TrackDuration>"
                       "<RelTime>0:01:07</RelTime>",
    "GetTransportInfo": "<CurrentTransportState>PLAYING</CurrentTransportState>",
    "GetVolume": "<CurrentVolume>42</CurrentVolume>",
}

RECORDED = []           # [(soapaction, body_bytes)]
FAIL_NEXT = []          # 非空时下一个请求回 SOAP Fault


class _FakeRenderer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        action_hdr = self.headers.get("SOAPAction", "").strip('"')
        RECORDED.append((action_hdr, body))
        action = action_hdr.rsplit("#", 1)[-1]

        if FAIL_NEXT:
            FAIL_NEXT.pop()
            payload = _FAULT_BODY.encode()
            self.send_response(500)
        else:
            stype = action_hdr.rsplit("#", 1)[0]
            payload = _OK_BODY.format(action=action, stype=stype,
                                      args=_RESPONSES.get(action, "")).encode()
            self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class SoapTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        class _Srv(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        cls.httpd = _Srv(("127.0.0.1", 0), _FakeRenderer)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.rend = Renderer(
            udn="uuid:test", friendly="假电视", location=f"{base}/desc.xml",
            base=base, av_type=AV_TYPE, av_control=f"{base}/ctl/av",
            rc_type=RC_TYPE, rc_control=f"{base}/ctl/rc")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        RECORDED.clear()
        FAIL_NEXT.clear()

    @staticmethod
    def arg_order(body: bytes):
        """取出请求里入参的先后顺序。"""
        root = ElementTree.fromstring(body)
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1].startswith(("SetAVTransportURI",
                                                     "Play", "Seek", "SetVolume",
                                                     "Pause", "Stop", "Get")):
                return [c.tag.rsplit("}", 1)[-1] for c in el]
        return []


class TestEnvelope(SoapTestBase):

    def test_set_uri_argument_order(self):
        """必须是 InstanceID -> CurrentURI -> CurrentURIMetaData，不能换。"""
        soap.set_uri(self.rend, "http://h/a.mp4", "<DIDL/>")
        _action, body = RECORDED[0]
        self.assertEqual(self.arg_order(body),
                         ["InstanceID", "CurrentURI", "CurrentURIMetaData"])

    def test_seek_argument_order(self):
        soap.seek(self.rend, "0:01:30")
        self.assertEqual(self.arg_order(RECORDED[0][1]),
                         ["InstanceID", "Unit", "Target"])

    def test_set_volume_argument_order(self):
        soap.set_volume(self.rend, 30)
        self.assertEqual(self.arg_order(RECORDED[0][1]),
                         ["InstanceID", "Channel", "DesiredVolume"])

    def test_soapaction_header_format(self):
        """必须是 "serviceType#Action" 且带引号，少一样设备就当非法请求。"""
        soap.play(self.rend)
        self.assertEqual(RECORDED[0][0], f"{AV_TYPE}#Play")

    def test_metadata_is_escaped_and_body_is_valid_xml(self):
        didl = '<DIDL-Lite><dc:title>a &amp; b</dc:title></DIDL-Lite>'
        soap.set_uri(self.rend, "http://h/a.mp4", didl)
        body = RECORDED[0][1]
        ElementTree.fromstring(body)          # 不转义这里就直接炸
        self.assertIn(b"&lt;DIDL-Lite&gt;", body)


class TestActions(SoapTestBase):

    def test_get_position(self):
        self.assertEqual(soap.get_position(self.rend), ("0:01:07", "0:05:13"))

    def test_get_state(self):
        self.assertEqual(soap.get_state(self.rend), "PLAYING")

    def test_get_volume(self):
        self.assertEqual(soap.get_volume(self.rend), 42)

    def test_set_volume_clamps(self):
        """C 版在发之前钳到 0~100；越界值有的设备会整条拒掉。"""
        self.assertEqual(soap.set_volume(self.rend, 200), 100)
        self.assertEqual(soap.set_volume(self.rend, -30), 0)
        sent = [b for _a, b in RECORDED]
        self.assertIn(b"<DesiredVolume>100</DesiredVolume>", sent[0])
        self.assertIn(b"<DesiredVolume>0</DesiredVolume>", sent[1])

    def test_no_rendering_control_raises(self):
        """没有 RenderingControl 的设备很常见，要给一句人话而不是 500。"""
        r = Renderer(udn="u", friendly="无音量", location="http://h/d.xml",
                     base="http://h/", av_type=AV_TYPE, av_control="http://h/av")
        with self.assertRaises(soap.SoapError):
            soap.set_volume(r, 50)
        with self.assertRaises(soap.SoapError):
            soap.get_volume(r)


class TestFault(SoapTestBase):

    def test_fault_detail_reaches_the_message(self):
        """设备回 500 时正文才是唯一线索，只报 HTTP 500 等于把它丢了。"""
        FAIL_NEXT.append(1)
        with self.assertRaises(soap.SoapError) as cm:
            soap.play(self.rend)
        self.assertIn("Transition not available", str(cm.exception))
        self.assertEqual(cm.exception.code, 701)

    def test_unreachable_device_raises_soap_error(self):
        """设备中途掉线不该抛 URLError 把交互界面打死。"""
        dead = Renderer(udn="u", friendly="掉线", location="http://127.0.0.1:9/d",
                        base="http://127.0.0.1:9/", av_type=AV_TYPE,
                        av_control="http://127.0.0.1:9/av")
        with self.assertRaises(soap.SoapError):
            soap.play(dead)


if __name__ == "__main__":
    unittest.main(verbosity=2)
