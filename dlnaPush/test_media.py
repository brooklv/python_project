"""MIME 表与 DIDL 元数据的回归测试。不需要网络，也不需要设备。

    python3 -m unittest discover -v
    python3 test_media.py

这里卡的是**同一份判断在两处必须一致**：HTTP 响应头的 Content-Type
和 DIDL 里的 protocolInfo 说的得是同一件事，对不上电视就静默拒播。
"""

import unittest

from dlna_media import MediaKind, build_didl, dlna_features, media_info, transfer_mode


class TestMediaInfo(unittest.TestCase):

    def test_m3u8_mime_is_hls_playlist(self):
        """HLS 的命根子。不是这个 MIME，电视会把播放列表当文件下载。"""
        mime, kind, _cls = media_info("/tmp/d/index.m3u8")
        self.assertEqual(mime, "application/vnd.apple.mpegurl")
        self.assertIs(kind, MediaKind.VIDEO)

    def test_kind_by_extension(self):
        cases = {
            "a.mp4": MediaKind.VIDEO, "a.mkv": MediaKind.VIDEO,
            "a.ts": MediaKind.VIDEO, "a.mp3": MediaKind.AUDIO,
            "a.flac": MediaKind.AUDIO, "a.jpg": MediaKind.IMAGE,
            "a.png": MediaKind.IMAGE,
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertIs(media_info(name)[1], want)

    def test_case_insensitive(self):
        """C 版用 strcasecmp；大写扩展名在 Windows 拷来的文件上很常见。"""
        self.assertEqual(media_info("A.MKV")[0], "video/x-matroska")
        self.assertEqual(media_info("A.JPG")[0], "image/jpeg")

    def test_unknown_falls_back_to_mp4(self):
        self.assertEqual(media_info("a.xyz")[0], "video/mp4")
        self.assertEqual(media_info("noext")[0], "video/mp4")

    def test_url_query_is_not_part_of_extension(self):
        """URL 带 ?token=... 时不能把 query 当扩展名，否则一律回落成 mp4。"""
        self.assertEqual(media_info("http://h/v.mkv?token=abc")[0],
                         "video/x-matroska")
        self.assertEqual(media_info("http://h/v.m3u8#x")[0],
                         "application/vnd.apple.mpegurl")

    def test_dot_in_directory_is_not_an_extension(self):
        """/media/v1.0/movie 里的 .0 属于目录名，不是这个文件的扩展名。"""
        self.assertEqual(media_info("/media/v1.0/movie")[0], "video/mp4")

    def test_audio_class_differs_from_video(self):
        self.assertNotEqual(media_info("a.mp3")[2], media_info("a.mp4")[2])

    def test_image_is_not_seekable(self):
        self.assertFalse(MediaKind.IMAGE.seekable)
        self.assertTrue(MediaKind.VIDEO.seekable)
        self.assertTrue(MediaKind.AUDIO.seekable)


class TestDidl(unittest.TestCase):

    def test_ampersand_in_title_is_escaped(self):
        """"Tom & Jerry.mp4" 不转义就让整份 XML 非法，设备只回一句没头没脑的错。"""
        didl = build_didl("Tom & Jerry", "video/mp4", "http://h/a.mp4",
                          "object.item.videoItem")
        self.assertIn("Tom &amp; Jerry", didl)
        self.assertNotIn("Tom & Jerry", didl)

    def test_url_query_amp_is_escaped(self):
        didl = build_didl("t", "video/mp4", "http://h/a?x=1&y=2",
                          "object.item.videoItem")
        self.assertIn("x=1&amp;y=2", didl)

    def test_is_well_formed_xml(self):
        from xml.etree import ElementTree
        didl = build_didl('引号"与<尖括号>', "video/mp4", "http://h/a<b>.mp4",
                          "object.item.videoItem")
        ElementTree.fromstring(didl)      # 解析不了就直接抛

    def test_protocol_info_carries_mime(self):
        didl = build_didl("t", "application/vnd.apple.mpegurl", "http://h/i.m3u8",
                          "object.item.videoItem")
        self.assertIn("http-get:*:application/vnd.apple.mpegurl:DLNA.ORG_OP=", didl)

    def test_protocol_info_declares_byte_seek(self):
        """第四栏留 `*` 的话电视认定不可 seek，拖进度会退化成整档重拉。"""
        didl = build_didl("t", "video/mp4", "http://h/a.mp4",
                          "object.item.videoItem", MediaKind.VIDEO)
        self.assertIn("DLNA.ORG_OP=01", didl)
        self.assertNotIn("http-get:*:video/mp4:*", didl)

    def test_protocol_info_kind_defaults_from_mime(self):
        """不传 kind 时按 MIME 粗判，图片不该被当成可 seek 的视频。"""
        self.assertIn("DLNA.ORG_OP=00",
                      build_didl("t", "image/jpeg", "http://h/a.jpg",
                                 "object.item.imageItem.photo"))
        self.assertIn("DLNA.ORG_OP=01",
                      build_didl("t", "video/mp4", "http://h/a.mp4",
                                 "object.item.videoItem"))


class TestDlnaFeatures(unittest.TestCase):
    """protocolInfo 第四栏和 contentFeatures.dlna.org 头共用这一串。"""

    def test_video_and_audio_support_byte_seek(self):
        for mime, kind in (("video/mp4", MediaKind.VIDEO),
                           ("audio/mpeg", MediaKind.AUDIO)):
            self.assertIn("DLNA.ORG_OP=01", dlna_features(mime, kind))

    def test_time_seek_is_never_claimed(self):
        """TimeSeekRange.dlna.org 没实现；谎报支持会让电视直接判定播放失败。"""
        for mime, kind in (("video/mp4", MediaKind.VIDEO),
                           ("audio/mpeg", MediaKind.AUDIO),
                           ("image/jpeg", MediaKind.IMAGE)):
            op = dlna_features(mime, kind).split("DLNA.ORG_OP=", 1)[1][:2]
            self.assertEqual(op[0], "0", f"{mime} 的高位不该声称支持 time-seek")

    def test_hls_does_not_claim_byte_seek(self):
        """HLS 的进度靠 playlist，对 .m3u8 做字节 seek 没有意义。"""
        self.assertIn("DLNA.ORG_OP=00",
                      dlna_features("application/vnd.apple.mpegurl", MediaKind.VIDEO))

    def test_flags_are_32_hex_digits(self):
        """DLNA.ORG_FLAGS 位数不对，一部分机型会整条 protocolInfo 解析失败。"""
        for mime, kind in (("video/mp4", MediaKind.VIDEO),
                           ("image/jpeg", MediaKind.IMAGE)):
            flags = dlna_features(mime, kind).split("DLNA.ORG_FLAGS=", 1)[1]
            self.assertEqual(len(flags), 32)
            int(flags, 16)                    # 不是十六进制就直接抛

    def test_image_uses_interactive_transfer_mode(self):
        self.assertEqual(transfer_mode(MediaKind.IMAGE), "Interactive")
        self.assertEqual(transfer_mode(MediaKind.VIDEO), "Streaming")
        self.assertEqual(transfer_mode(MediaKind.AUDIO), "Streaming")


if __name__ == "__main__":
    unittest.main(verbosity=2)
