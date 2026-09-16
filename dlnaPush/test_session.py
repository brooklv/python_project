"""一次完整推流的回归测试：假电视 + 真 HTTP 服务 + 真文件。不调 ffmpeg。

    python3 -m unittest test_session -v

这里卡的是**顺序**。电视收到 SetAVTransportURI 后可能立刻就来取文件，
所以内置 HTTP 服务的根目录必须在发 SOAP 之前就切好。反过来做的症状是
电视拉到 404 —— 而 SOAP 那边一切正常，日志上看不出任何异常。
"""

import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

from dlna_device import Renderer
from dlna_media import MediaKind
from dlna_session import Session, is_http_url, sec_to_hms
from dlna_transcode import OutFormat, XcodeConfig

AV = "urn:schemas-upnp-org:service:AVTransport:1"

CALLS = []          # [(action, {入参名: 值})]
# 收到 SetAVTransportURI 的当下就去拉一次 URL，验证"发 SOAP 时文件已可取"
FETCHED = {}


class _FakeRenderer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        action = self.headers.get("SOAPAction", "").strip('"').rsplit("#", 1)[-1]
        root = ElementTree.fromstring(body)
        args = {}
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1].endswith(action):
                args = {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in el}
                break
        CALLS.append((action, args))

        if action == "SetAVTransportURI":
            url = args.get("CurrentURI", "")
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    FETCHED[url] = (r.status, r.headers["Content-Type"], r.read())
            except Exception as exc:            # noqa: BLE001 - 要的就是记下失败
                FETCHED[url] = ("ERR", str(exc), b"")

        payload = (f'<?xml version="1.0"?>'
                   f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                   f'<s:Body><u:{action}Response xmlns:u="{AV}"/>'
                   f"</s:Body></s:Envelope>").encode()
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _StubTranscoder:
    """假装 ffmpeg 跑完了，直接在工作目录里放好产物。"""

    def __init__(self, work_dir, entry):
        self.work_dir, self.entry = work_dir, entry
        self.started_with = None

    def start(self, src):
        self.started_with = src
        with open(os.path.join(self.work_dir, self.entry), "wb") as fp:
            fp.write(b"#EXTM3U\n" if self.entry.endswith(".m3u8") else b"\x47" * 188)
        return self.work_dir, self.entry

    def stop(self):
        pass


class SessionTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        class _Srv(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        cls.httpd = _Srv(("127.0.0.1", 0), _FakeRenderer)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.rend = Renderer(udn="uuid:t", friendly="假电视",
                            location=f"{base}/dd.xml", base=base,
                            av_type=AV, av_control=f"{base}/ctl/av")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        CALLS.clear()
        FETCHED.clear()
        self.dir = tempfile.mkdtemp(prefix="dlna_sess_")
        self.session = Session("127.0.0.1", XcodeConfig(), log=lambda _m: None)
        self.session.start()
        self.session.renderer = self.rend
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(self.session.close)

    def make(self, name, data=b"payload"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fp:
            fp.write(data)
        return path

    @staticmethod
    def didl_of(args):
        return ElementTree.fromstring(args["CurrentURIMetaData"])

    @staticmethod
    def text_of(didl, tag):
        for el in didl.iter():
            if el.tag.rsplit("}", 1)[-1] == tag:
                return el.text
        return None


class TestPushLocalFile(SessionTestBase):

    def test_url_is_reachable_at_the_moment_soap_is_sent(self):
        """核心不变量：发 SetAVTransportURI 时文件就必须能取到。"""
        path = self.make("nature.mp4", b"video-bytes")
        res = self.session.push(path)
        self.assertEqual(FETCHED[res.url][0], 200)
        self.assertEqual(FETCHED[res.url][2], b"video-bytes")

    def test_play_follows_set_uri(self):
        self.session.push(self.make("a.mp4"))
        self.assertEqual([c[0] for c in CALLS], ["SetAVTransportURI", "Play"])

    def test_content_type_matches_didl_protocolinfo(self):
        """两处说的必须是同一件事，对不上电视会静默拒播。"""
        res = self.session.push(self.make("a.mkv"))
        self.assertEqual(FETCHED[res.url][1], "video/x-matroska")
        _action, args = CALLS[0]
        self.assertIn("http-get:*:video/x-matroska:DLNA.ORG_OP=",
                      args["CurrentURIMetaData"])

    def test_didl_declares_byte_seek(self):
        """protocolInfo 第四栏留 `*` 的话，电视认定源不可 seek，拖进度会被
        实现成重新 SetAVTransportURI —— 表现就是每次 seek 都从头开始载。"""
        self.session.push(self.make("a.mkv"))
        _action, args = CALLS[0]
        self.assertIn("DLNA.ORG_OP=01", args["CurrentURIMetaData"])

    def test_image_didl_does_not_claim_seek(self):
        """图片没有进度可言，声称可 seek 只会招来无意义的 Range 试探。"""
        self.session.push(self.make("a.jpg", b"jpg"))
        _action, args = CALLS[0]
        self.assertIn("DLNA.ORG_OP=00", args["CurrentURIMetaData"])

    def test_spaces_and_chinese_in_filename(self):
        """文件名带空格/中文时 URL 必须编码，且服务端要能解回来。"""
        path = self.make("我的 假期.mp4", b"cn")
        res = self.session.push(path)
        self.assertNotIn(" ", res.url)
        self.assertEqual(FETCHED[res.url][0], 200)

    def test_title_is_the_original_filename(self):
        self.session.push(self.make("nature.mp4"))
        self.assertEqual(self.text_of(self.didl_of(CALLS[0][1]), "title"),
                         "nature.mp4")

    def test_missing_file_raises_before_any_soap(self):
        """文件不存在时不该先把 URI 推给电视再发现问题。"""
        with self.assertRaises(FileNotFoundError):
            self.session.push(os.path.join(self.dir, "nope.mp4"))
        self.assertEqual(CALLS, [])

    def test_http_url_is_passed_through(self):
        res = self.session.push("http://example.invalid/v.mkv")
        self.assertEqual(res.url, "http://example.invalid/v.mkv")
        self.assertEqual(res.kind, MediaKind.VIDEO)

    def test_image_kind(self):
        res = self.session.push(self.make("photo.jpg"))
        self.assertIs(res.kind, MediaKind.IMAGE)
        self.assertEqual(self.text_of(self.didl_of(CALLS[0][1]), "class"),
                         "object.item.imageItem.photo")

    def test_open_switches_root_dir(self):
        """换片到另一个目录后，两个文件不能同时可取 —— 根目录只有一个。"""
        first = self.make("one.mp4", b"1")
        other = tempfile.mkdtemp(prefix="dlna_sess2_")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        with open(os.path.join(other, "two.mp4"), "wb") as fp:
            fp.write(b"2")

        r1 = self.session.push(first)
        r2 = self.session.push(os.path.join(other, "two.mp4"))
        self.assertEqual(FETCHED[r2.url][2], b"2")
        with self.assertRaises(Exception):
            urllib.request.urlopen(r1.url, timeout=3)


class TestPushWithTranscode(SessionTestBase):

    def use_stub(self, fmt, entry):
        self.session.cfg = XcodeConfig(fmt=fmt)
        self.session.xcode = _StubTranscoder(self.dir, entry)
        return self.session.xcode

    def test_hls_entry_becomes_the_url_and_mime(self):
        """转封装后一切按产物走：URL / MIME / upnp:class 都落到 .m3u8 上。"""
        self.use_stub(OutFormat.HLS, "index.m3u8")
        res = self.session.push(self.make("movie.mkv"))
        self.assertTrue(res.url.endswith("index.m3u8"))
        self.assertEqual(FETCHED[res.url][1], "application/vnd.apple.mpegurl")

    def test_title_stays_the_original_not_the_entry(self):
        """标题若跟着产物走，电视上会显示成 "index.m3u8"。"""
        self.use_stub(OutFormat.HLS, "index.m3u8")
        self.session.push(self.make("movie.mkv"))
        self.assertEqual(self.text_of(self.didl_of(CALLS[0][1]), "title"),
                         "movie.mkv")

    def test_audio_source_keeps_audio_class(self):
        """产物是 .ts 会被判成视频；纯音频源必须保住 audioItem 这个 class，
        否则电视会按视频条目去处理一路只有音轨的流。"""
        self.use_stub(OutFormat.TS, "out.ts")
        res = self.session.push(self.make("song.flac"))
        self.assertIs(res.kind, MediaKind.AUDIO)
        self.assertEqual(self.text_of(self.didl_of(CALLS[0][1]), "class"),
                         "object.item.audioItem.musicTrack")

    def test_image_skips_transcode(self):
        """图片没有容器可换，硬送进 ffmpeg 只会失败。"""
        stub = self.use_stub(OutFormat.TS, "out.ts")
        res = self.session.push(self.make("photo.png"))
        self.assertIsNone(stub.started_with)
        self.assertIs(res.kind, MediaKind.IMAGE)


class TestHelpers(unittest.TestCase):

    def test_sec_to_hms(self):
        self.assertEqual(sec_to_hms(0), "0:00:00")
        self.assertEqual(sec_to_hms(90), "0:01:30")
        self.assertEqual(sec_to_hms(3725), "1:02:05")
        self.assertEqual(sec_to_hms(-5), "0:00:00")

    def test_is_http_url(self):
        self.assertTrue(is_http_url("http://h/a"))
        self.assertTrue(is_http_url("https://h/a"))
        self.assertFalse(is_http_url("/home/user/a.mp4"))
        self.assertFalse(is_http_url("ftp://h/a"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
