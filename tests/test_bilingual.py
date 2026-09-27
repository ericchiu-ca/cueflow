import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from subflow.bilingual import (
    VIDEO_FILTER_SOURCE,
    VIDEO_FILTER_1080P,
    DEFAULT_FONTS_DIR,
    MULISH_FONT,
    SOURCE_HAN_FONT,
    SubtitleBuildError,
    _VideoGeometry,
    _probe_video_info,
    burn_ass_into_video,
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
        self.assertEqual(
            ass_escape_text("Use {x}\\path\nnext"),
            "Use \\{x\\}\\\u2060path\\Nnext",
        )

    def test_ass_escape_keeps_literal_backslash_sequences_inert(self):
        # libass reads "\\N" as a backslash followed by a line break, so a
        # literal backslash must never be directly followed by an override letter.
        escaped = ass_escape_text(r"C:\new \N \h")
        self.assertNotRegex(escaped, r"\\[Nnh]")
        self.assertEqual(escaped.replace("\u2060", ""), r"C:\new \N \h")

    def test_render_layout_does_not_split_on_escaped_literal_backslash_n(self):
        source = parse_srt_text("1\n00:00:01,000 --> 00:00:02,000\nsee \\N here\n")
        chinese = parse_srt_text("1\n00:00:01,000 --> 00:00:02,000\n字面 \\N 保留\n")
        editable = build_bilingual_ass_text(source, chinese)
        output = adapt_bilingual_ass_for_render(editable, frame_width=1920, frame_height=1080)
        dialogue = [line for line in output.splitlines() if line.startswith("Dialogue:")]
        self.assertTrue(dialogue)
        self.assertTrue(all("\\\u2060N" in line for line in dialogue))

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


class TestPackagedFonts(unittest.TestCase):
    def test_fonts_ship_inside_the_package(self):
        import subflow

        self.assertEqual(DEFAULT_FONTS_DIR, Path(subflow.__file__).resolve().with_name("fonts"))
        for name in (SOURCE_HAN_FONT, MULISH_FONT):
            self.assertTrue((DEFAULT_FONTS_DIR / name).is_file(), name)


class TestVideoProbe(unittest.TestCase):
    def test_probe_reads_first_video_stream_when_audio_is_first(self):
        # Real ffprobe lists every stream unless -select_streams narrows it,
        # so an audio-first container used to fail geometry parsing.
        def fake_ffprobe(command, **_kwargs):
            if "-select_streams" in command:
                streams = [{"width": 1280, "height": 720}]
            else:
                streams = [{}, {"width": 1280, "height": 720}]
            payload = {"streams": streams, "format": {"duration": "12.5"}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

        with (
            patch("subflow.bilingual._ffprobe_path", return_value=Path("ffprobe")),
            patch("subflow.bilingual.subprocess.run", side_effect=fake_ffprobe) as run,
        ):
            duration, geometry = _probe_video_info(Path("ffmpeg"), Path("audio-first.mkv"))

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-select_streams") + 1], "v:0")
        self.assertEqual(duration, 12.5)
        self.assertEqual((geometry.width, geometry.height), (1280, 720))


    def test_probe_swaps_dimensions_for_quarter_turn_rotation(self):
        def probe_payload(stream):
            payload = {"streams": [stream], "format": {"duration": "3"}}

            def fake_ffprobe(command, **_kwargs):
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

            return fake_ffprobe

        cases = [
            ({"width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]}, (1080, 1920)),
            ({"width": 1920, "height": 1080, "tags": {"rotate": "270"}}, (1080, 1920)),
            ({"width": 1920, "height": 1080, "side_data_list": [{"rotation": 180}]}, (1920, 1080)),
            ({"width": 1920, "height": 1080}, (1920, 1080)),
        ]
        for stream, expected in cases:
            with (
                self.subTest(stream=stream),
                patch("subflow.bilingual._ffprobe_path", return_value=Path("ffprobe")),
                patch("subflow.bilingual.subprocess.run", side_effect=probe_payload(stream)),
            ):
                _duration, geometry = _probe_video_info(Path("ffmpeg"), Path("clip.mov"))
                self.assertEqual((geometry.width, geometry.height), expected)


class TestBurnProcessCleanup(unittest.TestCase):
    def test_failing_progress_callback_kills_ffmpeg_and_removes_partial_output(self):
        source = parse_srt_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        chinese = parse_srt_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n")
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            video = root / "input.mp4"
            video.write_bytes(b"not really a video")
            ass = root / "bilingual.ass"
            ass.write_text(build_bilingual_ass_text(source, chinese), encoding="utf-8")
            output = root / "out" / "result.mp4"
            pid_file = root / "ffmpeg.pid"
            fake_ffmpeg = root / "ffmpeg"
            fake_ffmpeg.write_text(
                "#!/bin/sh\n"
                f"echo $$ > {pid_file}\n"
                'for last; do :; done\n'
                'echo partial > "$last"\n'
                "echo out_time_us=5000000\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            fake_ffmpeg.chmod(0o755)
            geometry = _VideoGeometry(1920, 1080)

            def failing_progress(stage, percent, _message):
                if stage == "encoding" and percent > 3:
                    raise RuntimeError("progress sink failed")

            with (
                patch("subflow.bilingual._ensure_fonts"),
                patch("subflow.bilingual.find_ass_ffmpeg", return_value=fake_ffmpeg),
                patch("subflow.bilingual._probe_video_info", return_value=(10.0, geometry)),
                patch("subflow.bilingual._detect_active_picture", return_value=geometry),
            ):
                with self.assertRaisesRegex(RuntimeError, "progress sink failed"):
                    burn_ass_into_video(video, ass, output, progress=failing_progress)

            self.assertFalse(output.exists())
            pid = int(pid_file.read_text().strip())
            deadline = time.monotonic() + 3
            alive = True
            while alive and time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    alive = False
            self.assertFalse(alive)
