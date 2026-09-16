"""ffmpeg 参数拼装与产物清理的回归测试。不会真的调起 ffmpeg。

    python3 -m unittest test_transcode -v

这里卡两件事：

* **参数顺序**。ffmpeg 的选项作用于紧随其后的输入或输出，``-c:v`` 落在
  ``-i`` 前面就变成了给输入解码用的参数，行为完全不同。
* **清理只删自己的**。``--work-dir`` 可能指到用户的 U 盘目录，那里还有别的
  文件；按通配删除会把人家的东西一起删掉，而且是不可撤销的。
"""

import os
import shutil
import tempfile
import unittest

from dlna_transcode import (
    FF_LOG, HLS_PLAYLIST, MP4_OUT, OutFormat, TS_OUT, Transcoder, XcodeConfig,
    count_segments, is_artifact, parse_format,
)


def argv_for(cfg, src="/tmp/a.mkv", outpath="/tmp/w/out", live=False):
    return Transcoder(cfg, log=lambda _m: None).build_argv(src, outpath, live)


class TestParseFormat(unittest.TestCase):

    def test_aliases(self):
        cases = {"raw": OutFormat.RAW, "none": OutFormat.RAW,
                 "direct": OutFormat.RAW, "原始": OutFormat.RAW,
                 "hls": OutFormat.HLS, "m3u8": OutFormat.HLS,
                 "ts": OutFormat.TS, "mpegts": OutFormat.TS,
                 "mp4": OutFormat.MP4}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertIs(parse_format(text), want)

    def test_case_and_space_insensitive(self):
        self.assertIs(parse_format("  M3U8 "), OutFormat.HLS)

    def test_unknown_raises_with_the_valid_list(self):
        with self.assertRaises(ValueError) as cm:
            parse_format("avi")
        self.assertIn("mpegts", str(cm.exception))

    def test_raw_is_disabled(self):
        self.assertFalse(XcodeConfig().enabled)
        self.assertTrue(XcodeConfig(fmt=OutFormat.TS).enabled)


class TestArgv(unittest.TestCase):

    def test_codec_options_come_after_input(self):
        """-c:v 落在 -i 前面就成了输入侧参数，语义完全变了。"""
        argv = argv_for(XcodeConfig(fmt=OutFormat.TS))
        self.assertLess(argv.index("-i"), argv.index("-c:v"))

    def test_output_path_is_last(self):
        argv = argv_for(XcodeConfig(fmt=OutFormat.TS), outpath="/tmp/w/out.ts")
        self.assertEqual(argv[-1], "/tmp/w/out.ts")

    def test_nostdin_is_present(self):
        """少了 -nostdin，ffmpeg 会和交互提示符抢标准输入，键盘输入直接乱掉。"""
        self.assertIn("-nostdin", argv_for(XcodeConfig(fmt=OutFormat.TS)))

    def test_subtitles_are_dropped(self):
        """mkv 的字幕流塞进 mpegts/mp4 会让 -c copy 直接失败。"""
        argv = argv_for(XcodeConfig(fmt=OutFormat.TS))
        self.assertIn("-sn", argv)
        self.assertIn("-dn", argv)
        self.assertIn("0:v:0?", argv)
        self.assertIn("0:a:0?", argv)

    def test_default_is_copy_both(self):
        argv = argv_for(XcodeConfig(fmt=OutFormat.TS))
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertEqual(argv[argv.index("-c:a") + 1], "copy")

    def test_ts_uses_mpegts_muxer(self):
        argv = argv_for(XcodeConfig(fmt=OutFormat.TS))
        self.assertEqual(argv[argv.index("-f") + 1], "mpegts")

    def test_mp4_has_faststart(self):
        """没有 +faststart，moov 在文件尾部，电视得下完整个文件才能起播。"""
        argv = argv_for(XcodeConfig(fmt=OutFormat.MP4))
        self.assertIn("+faststart", argv)

    def test_hls_playlist_type_follows_live(self):
        """转完再播是 vod（可随意拖），边转边播是 event（持续追加）。"""
        cfg = XcodeConfig(fmt=OutFormat.HLS)
        vod = argv_for(cfg, live=False)
        evt = argv_for(cfg, live=True)
        self.assertEqual(vod[vod.index("-hls_playlist_type") + 1], "vod")
        self.assertEqual(evt[evt.index("-hls_playlist_type") + 1], "event")

    def test_hls_keeps_every_segment_in_the_playlist(self):
        """-hls_list_size 0 才会留全部分片；默认值会把旧分片从列表里删掉。"""
        argv = argv_for(XcodeConfig(fmt=OutFormat.HLS))
        self.assertEqual(argv[argv.index("-hls_list_size") + 1], "0")

    def test_hls_segment_pattern_is_passed_through_verbatim(self):
        """seg%05d.ts 里的 %05d 是给 ffmpeg 的，不能被我们自己格式化掉。"""
        argv = argv_for(XcodeConfig(fmt=OutFormat.HLS),
                        outpath="/tmp/w/index.m3u8")
        seg = argv[argv.index("-hls_segment_filename") + 1]
        self.assertTrue(seg.endswith("seg%05d.ts"), seg)

    def test_hls_time_is_honored(self):
        argv = argv_for(XcodeConfig(fmt=OutFormat.HLS, hls_time=10))
        self.assertEqual(argv[argv.index("-hls_time") + 1], "10")

    def test_zero_hls_time_falls_back_to_six(self):
        argv = argv_for(XcodeConfig(fmt=OutFormat.HLS, hls_time=0))
        self.assertEqual(argv[argv.index("-hls_time") + 1], "6")

    def test_extra_args_go_before_the_output(self):
        """--ff-extra 必须落在输出文件之前，否则 ffmpeg 会当成又一个输出。"""
        argv = argv_for(XcodeConfig(fmt=OutFormat.TS,
                                    extra=["-bsf:a", "aac_adtstoasc"]),
                        outpath="/tmp/w/out.ts")
        self.assertEqual(argv[-3:], ["-bsf:a", "aac_adtstoasc", "/tmp/w/out.ts"])

    def test_custom_codecs(self):
        argv = argv_for(XcodeConfig(fmt=OutFormat.MP4, vcodec="h264_v4l2m2m",
                                    acodec="aac"))
        self.assertEqual(argv[argv.index("-c:v") + 1], "h264_v4l2m2m")
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")

    def test_custom_ffmpeg_path_is_argv0(self):
        argv = argv_for(XcodeConfig(fmt=OutFormat.TS, ffmpeg="/opt/bin/ffmpeg"))
        self.assertEqual(argv[0], "/opt/bin/ffmpeg")


class TestArtifacts(unittest.TestCase):

    def test_our_artifacts(self):
        for name in (HLS_PLAYLIST, TS_OUT, MP4_OUT, FF_LOG,
                     "seg00000.ts", "seg99999.TS"):
            with self.subTest(name=name):
                self.assertTrue(is_artifact(name))

    def test_user_files_are_not_touched(self):
        """--work-dir 可能是用户的 U 盘目录，里面还有别人的东西。"""
        for name in ("nature.mp4", "holiday.ts", "notes.txt", "out.mkv",
                     "index.m3u", "s.ts"):
            with self.subTest(name=name):
                self.assertFalse(is_artifact(name))


class TestWipe(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dlna_wipe_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def touch(self, *names):
        for n in names:
            with open(os.path.join(self.dir, n), "wb") as fp:
                fp.write(b"x")

    def test_wipe_keeps_foreign_files_and_the_directory(self):
        """目录不是我们建的（--work-dir 指进来的），清理后目录必须还在。"""
        self.touch("index.m3u8", "seg00001.ts", "ffmpeg.log", "家庭录像.mkv")
        tc = Transcoder(XcodeConfig(fmt=OutFormat.HLS), log=lambda _m: None)
        tc._dir, tc._dir_ours = self.dir, False
        tc._wipe()
        self.assertTrue(os.path.isdir(self.dir))
        self.assertEqual(os.listdir(self.dir), ["家庭录像.mkv"])

    def test_wipe_removes_our_own_directory(self):
        own = os.path.join(self.dir, "dlna_push_123")
        os.makedirs(own)
        tc = Transcoder(XcodeConfig(fmt=OutFormat.TS), log=lambda _m: None)
        tc._dir, tc._dir_ours = own, True
        with open(os.path.join(own, TS_OUT), "wb") as fp:
            fp.write(b"x")
        tc._wipe()
        self.assertFalse(os.path.exists(own))

    def test_wipe_is_idempotent(self):
        """stop() 会被 atexit、信号和换片各调一次，重复调用不能炸。"""
        tc = Transcoder(XcodeConfig(fmt=OutFormat.TS), log=lambda _m: None)
        tc._dir, tc._dir_ours = self.dir, False
        tc._wipe()
        tc._wipe()
        tc.stop()


class TestCountSegments(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dlna_pl_")
        self.path = os.path.join(self.dir, HLS_PLAYLIST)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write(text)

    def test_counts_only_segment_lines(self):
        self.write("#EXTM3U\n#EXT-X-VERSION:3\n"
                   "#EXTINF:6.0,\nseg00000.ts\n"
                   "#EXTINF:6.0,\nseg00001.ts\n")
        self.assertEqual(count_segments(self.path), 2)

    def test_missing_playlist_is_zero(self):
        """边转边播刚起来时播放列表还没被 ffmpeg 创建出来，不能当成错误。"""
        self.assertEqual(count_segments(self.path), 0)

    def test_header_only_is_zero(self):
        self.write("#EXTM3U\n#EXT-X-VERSION:3\n\n")
        self.assertEqual(count_segments(self.path), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
