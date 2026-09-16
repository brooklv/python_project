"""内置 HTTP 服务的回归测试。会真的起一个服务，但只绑 127.0.0.1。

    python3 -m unittest test_http -v

这里卡的是两件**做错了也不报错、只是电视不动**的事：

* **Range**。libupnp 有，标准库的 SimpleHTTPRequestHandler 没有。
  丢了 Range 支持的症状不是报错，是拖进度条没反应。
* **路径安全**。这个服务绑在局域网地址上，同网段谁都能访问；
  一个 ``../../etc/passwd`` 就能把整台机器读走。
"""

import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request

from dlna_http import MediaRoot, MediaServer, parse_range

BODY = bytes(range(256)) * 8        # 2048 字节，内容可按下标校验


class TestParseRange(unittest.TestCase):

    def test_no_header_means_whole_file(self):
        self.assertIsNone(parse_range(None, 100))
        self.assertIsNone(parse_range("", 100))

    def test_open_ended(self):
        self.assertEqual(parse_range("bytes=0-", 100), (0, 99))
        self.assertEqual(parse_range("bytes=30-", 100), (30, 99))

    def test_closed_range(self):
        self.assertEqual(parse_range("bytes=10-19", 100), (10, 19))

    def test_end_beyond_eof_is_clamped(self):
        """电视常直接要 bytes=0-99999999，按 RFC 要钳到文件末尾而不是报错。"""
        self.assertEqual(parse_range("bytes=50-999999", 100), (50, 99))

    def test_suffix_range(self):
        self.assertEqual(parse_range("bytes=-20", 100), (80, 99))

    def test_multi_range_falls_back_to_whole_file(self):
        """多段 Range 不支持；按 RFC 回整个文件是合法的，比报错强。"""
        self.assertIsNone(parse_range("bytes=0-1,5-9", 100))

    def test_unsatisfiable_raises(self):
        for bad in ("bytes=200-", "bytes=abc-", "bytes=-0", "bytes=50-10"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_range(bad, 100)


class TestMediaRootSafety(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dlna_root_")
        self.root = MediaRoot()
        self.root.set(self.dir)
        with open(os.path.join(self.dir, "a b.mp4"), "wb") as fp:
            fp.write(b"x")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_percent_encoded_name_resolves(self):
        self.assertIsNotNone(self.root.resolve("/media/a%20b.mp4"))

    def test_traversal_is_rejected(self):
        for bad in ("/media/../../etc/passwd", "/media/..%2f..%2fetc%2fpasswd",
                    "/media/sub/dir.mp4", "/media/..", "/media/"):
            with self.subTest(bad=bad):
                self.assertIsNone(self.root.resolve(bad))

    def test_missing_file_is_none(self):
        self.assertIsNone(self.root.resolve("/media/nope.mp4"))

    def test_no_root_set_is_none(self):
        self.assertIsNone(MediaRoot().resolve("/media/a b.mp4"))

    def test_query_string_is_stripped(self):
        """有的电视会在 URL 后面挂参数；那不属于文件名。"""
        self.assertIsNotNone(self.root.resolve("/media/a%20b.mp4?x=1"))


class TestServerLive(unittest.TestCase):
    """真起服务、真发请求。绑 127.0.0.1，不碰局域网。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="dlna_srv_")
        with open(os.path.join(cls.dir, "movie.mkv"), "wb") as fp:
            fp.write(BODY)
        with open(os.path.join(cls.dir, "index.m3u8"), "wb") as fp:
            fp.write(b"#EXTM3U\n")
        cls.srv = MediaServer("127.0.0.1")
        cls.srv.start()
        cls.srv.serve_dir(cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, name, headers=None, method="GET"):
        req = urllib.request.Request(self.srv.url_for(name),
                                     headers=headers or {}, method=method)
        return urllib.request.urlopen(req, timeout=5)

    def test_whole_file(self):
        with self.get("movie.mkv") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "video/x-matroska")
            self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
            self.assertEqual(int(resp.headers["Content-Length"]), len(BODY))
            self.assertEqual(resp.read(), BODY)

    def test_m3u8_content_type(self):
        """播放列表的 Content-Type 错了 HLS 就整个推不动 —— 这是移植的重点之一。"""
        with self.get("index.m3u8") as resp:
            self.assertEqual(resp.headers["Content-Type"],
                             "application/vnd.apple.mpegurl")

    def test_range_returns_206_and_exact_bytes(self):
        with self.get("movie.mkv", {"Range": "bytes=100-199"}) as resp:
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.headers["Content-Range"],
                             f"bytes 100-199/{len(BODY)}")
            self.assertEqual(int(resp.headers["Content-Length"]), 100)
            self.assertEqual(resp.read(), BODY[100:200])

    def test_open_ended_range(self):
        with self.get("movie.mkv", {"Range": "bytes=2000-"}) as resp:
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.read(), BODY[2000:])

    def test_head_has_headers_but_no_body(self):
        """电视常先 HEAD 探一下大小和类型；这一步失败就不会再来 GET。"""
        with self.get("movie.mkv", method="HEAD") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(int(resp.headers["Content-Length"]), len(BODY))
            self.assertEqual(resp.read(), b"")

    def test_unsatisfiable_range_is_416(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("movie.mkv", {"Range": "bytes=99999-"})
        self.assertEqual(cm.exception.code, 416)
        self.assertEqual(cm.exception.headers["Content-Range"],
                         f"bytes */{len(BODY)}")

    def test_missing_file_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("nope.mp4")
        self.assertEqual(cm.exception.code, 404)

    def test_transfer_mode_is_echoed(self):
        with self.get("movie.mkv", {"transferMode.dlna.org": "Streaming"}) as resp:
            self.assertEqual(resp.headers["transferMode.dlna.org"], "Streaming")

    def test_transfer_mode_has_default_when_not_asked(self):
        """规范要求这个头始终存在，电视没带也得给缺省值。"""
        with self.get("movie.mkv") as resp:
            self.assertEqual(resp.headers["transferMode.dlna.org"], "Streaming")

    def test_content_features_declares_byte_seek(self):
        """只有 Range 能用还不够：电视看 DLNA.ORG_OP 决定要不要发 Range 去
        seek，这里不声明就会退化成整档重拉。"""
        with self.get("movie.mkv") as resp:
            self.assertIn("DLNA.ORG_OP=01", resp.headers["contentFeatures.dlna.org"])

    def test_content_features_matches_didl(self):
        """HTTP 头和 DIDL 的 protocolInfo 对不上时，严格的机型以 HTTP 头为准。"""
        from dlna_media import build_didl, media_info
        with self.get("movie.mkv") as resp:
            feat = resp.headers["contentFeatures.dlna.org"]
        mime, kind, cls = media_info("movie.mkv")
        self.assertIn(f"http-get:*:{mime}:{feat}",
                      build_didl("movie", mime, "http://h/movie.mkv", cls, kind))

    def test_range_response_also_carries_dlna_headers(self):
        """206 是 seek 过程中最常见的响应；头掉在这里等于没支持。"""
        with self.get("movie.mkv", {"Range": "bytes=2-5"}) as resp:
            self.assertEqual(resp.status, 206)
            self.assertIn("DLNA.ORG_OP=01", resp.headers["contentFeatures.dlna.org"])
            self.assertEqual(resp.headers["transferMode.dlna.org"], "Streaming")

    def test_serve_dir_switch_takes_effect(self):
        """open 换片会切根目录；切完旧目录的文件必须立刻取不到。"""
        other = tempfile.mkdtemp(prefix="dlna_srv2_")
        try:
            with open(os.path.join(other, "song.mp3"), "wb") as fp:
                fp.write(b"id3")
            self.srv.serve_dir(other)
            with self.get("song.mp3") as resp:
                self.assertEqual(resp.headers["Content-Type"], "audio/mpeg")
            with self.assertRaises(urllib.error.HTTPError):
                self.get("movie.mkv")
        finally:
            self.srv.serve_dir(self.dir)
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
