import unittest

from subflow.bilingual import (
    VIDEO_FILTER_SOURCE,
    VIDEO_FILTER_1080P,
    SubtitleBuildError,
    adapt_bilingual_ass_for_render,
    ass_escape_text,
    build_bilingual_ass_text,
    video_encoding_options,
)
from subflow.core import parse_srt_text


class TestBilingualAss(unittest.TestCase):
    def setUp(self):
        self.source = parse_srt_text(
            """1
00:00:01,000 --> 00:00:03,500
How Quebec became North America's urban outlier

2
00:00:04,000 --> 00:00:06,250
Public transit shaped the city.
"""
        )
        self.chinese = parse_srt_text(
            """1
00:00:10,000 --> 00:00:11,000
魁北克为何成为北美的城市异类

2
00:00:12,000 --> 00:00:13,000
公共交通塑造了这座城市。
"""
        )

    def test_ass_uses_source_timeline_and_requested_styles(self):
        output = build_bilingual_ass_text(self.source, self.chinese)
        self.assertIn("PlayResX: 1920", output)
        self.assertIn("MarginV, Encoding", output)
        self.assertIn("CueFlow Han Sans SC,94", output)
        self.assertIn("Style: Source,Mulish SemiBold,64", output)
        self.assertIn(",4.6,0,2,70,70,166,1", output)
        self.assertIn(",4.4,0,2,70,70,94,1", output)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.50", output)
        self.assertNotIn("Dialogue: 0,0:00:10.00", output)
        self.assertLess(output.index("魁北克为何"), output.index("How Quebec"))

    def test_ass_requires_equal_segment_counts(self):
        with self.assertRaisesRegex(SubtitleBuildError, "count mismatch"):
            build_bilingual_ass_text(self.source, self.chinese[:1])

    def test_ass_escapes_override_characters_and_line_breaks(self):
        self.assertEqual(ass_escape_text("Use {x}\\path\nnext"), r"Use \{x\}\\path\Nnext")

    def test_render_layout_stacks_tracks_inside_detected_4_3_picture(self):
        editable = build_bilingual_ass_text(self.source, self.chinese)
        output = adapt_bilingual_ass_for_render(
            editable,
            frame_width=1280,
            frame_height=720,
            active_x=172,
            active_y=0,
            active_width=944,
            active_height=720,
        )
        self.assertIn("PlayResX: 1920", output)
        self.assertIn("Style: Bilingual,CueFlow Han Sans SC,94", output)
        self.assertIn(",329,317,86,1", output)
        self.assertEqual(output.count("Dialogue: "), 2)
        self.assertNotIn(",Chinese,,", output)
        self.assertNotIn(",Source,,", output)
        self.assertIn(r"\N{\fs14\bord0\shad0\alpha&HFF&}\h", output)
        self.assertIn(r"\fnMulish SemiBold\fs64", output)
        self.assertIn(r"\1c&HF0F0F0&", output)
        self.assertNotIn(r"\1c&H00F0F0&", output)
        self.assertLess(output.index("魁北克为何"), output.index("How Quebec"))

    def test_render_layout_uses_a_4_3_script_canvas_for_true_4_3_video(self):
        editable = build_bilingual_ass_text(self.source, self.chinese)
        output = adapt_bilingual_ass_for_render(
            editable,
            frame_width=960,
            frame_height=720,
        )
        self.assertIn("PlayResX: 1440", output)
        self.assertIn(",72,72,86,1", output)

    def test_render_layout_wraps_unspaced_chinese_inside_safe_width(self):
        chinese_text = "总理莫里斯迪普莱西在巡视魁北克北部地区期间因脑溢血去世"
        chinese = parse_srt_text(
            f"""1
00:00:01,000 --> 00:00:03,500
{chinese_text}
"""
        )
        editable = build_bilingual_ass_text(self.source[:1], chinese)
        output = adapt_bilingual_ass_for_render(
            editable,
            frame_width=1280,
            frame_height=720,
            active_x=172,
            active_y=0,
            active_width=944,
            active_height=720,
        )
        dialogue = next(line for line in output.splitlines() if line.startswith("Dialogue: "))
        chinese_render = dialogue.split(r"\N{\fs14", 1)[0].split(",", 9)[9]
        self.assertIn(r"\N", chinese_render)
        self.assertEqual(chinese_render.replace(r"\N", ""), chinese_text)

    def test_1080p_hardware_encoding_profiles(self):
        profile, options = video_encoding_options("hevc")
        self.assertEqual(profile, "hevc")
        self.assertIn("hevc_videotoolbox", options)
        self.assertIn("hvc1", options)
        self.assertIn("min(1920,iw)", VIDEO_FILTER_1080P)
        self.assertIn("min(1080,ih)", VIDEO_FILTER_1080P)
        self.assertTrue(VIDEO_FILTER_1080P.index("scale=") < VIDEO_FILTER_1080P.index("ass="))

    def test_source_resolution_hevc_profile_does_not_scale(self):
        profile, options = video_encoding_options("hevc-source")
        self.assertEqual(profile, "hevc-source")
        self.assertIn("hevc_videotoolbox", options)
        self.assertNotIn("scale=", VIDEO_FILTER_SOURCE)
        self.assertTrue(VIDEO_FILTER_SOURCE.startswith("ass="))


if __name__ == "__main__":
    unittest.main()
