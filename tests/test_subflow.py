import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from subflow.core import (
    SubtitleSegment,
    build_bilingual_srt_text,
    build_srt_text,
    parse_srt_file,
    parse_srt_text,
    parse_translation_file,
    save_master_json,
)
from subflow.qc import run_qc
from subflow.transcription import (
    MlxTranscription,
    TranscriptionError,
    align_existing_project,
    find_whisperx_python,
    resolve_language,
    safe_filename,
    segments_from_result,
    transcribe_media,
)


class TestCoreParsing(unittest.TestCase):
    def test_parse_srt_text_and_ids(self):
        sample = """
1
00:00:10,000 --> 00:00:13,500
The Federal Reserve is trying to bring inflation down.

2
00:00:14,200 --> 00:00:16,700
But the labor market remains surprisingly strong.
"""
        segments = parse_srt_text(sample)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].id, "0001")
        self.assertEqual(segments[0].start, 10.0)
        self.assertEqual(segments[0].end, 13.5)
        self.assertEqual(segments[1].id, "0002")

    def test_parse_srt_keeps_first_cue_after_utf8_bom(self):
        sample = "﻿1\n00:00:01,000 --> 00:00:02,000\nFirst\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond\n"
        segments = parse_srt_text(sample)
        self.assertEqual([segment.text for segment in segments], ["First", "Second"])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bom.srt"
            path.write_bytes(sample.encode("utf-8"))
            self.assertEqual(len(parse_srt_file(path)), 2)

    def test_parse_srt_accepts_cue_settings_and_names_bad_blocks(self):
        segments = parse_srt_text(
            "1\n00:00:01,000 --> 00:00:02,000 X1:10 X2:20 Y1:5 Y2:9\nPositioned\n\n"
            "2\n00:00:03,000-->00:00:04,000\nTight arrow\n"
        )
        self.assertEqual([(s.start, s.end, s.text) for s in segments], [(1.0, 2.0, "Positioned"), (3.0, 4.0, "Tight arrow")])
        with self.assertRaisesRegex(ValueError, "block 2"):
            parse_srt_text("1\n00:00:01,000 --> 00:00:02,000\nA\n\n2\n00:00:03,000 --> 00:00:04,000 --> 00:00:05,000\nB\n")
        with self.assertRaisesRegex(ValueError, "block 1"):
            parse_srt_text("1\n00:00:01,000 --> 4\nA\n")

    def test_parse_srt_preserves_millisecond_timing_round_trip(self):
        sample = "1\n00:00:01,234 --> 00:00:02,567\nPrecise\n\n2\n00:00:03,001 --> 00:00:03,004\nShort\n"
        segments = parse_srt_text(sample)
        self.assertEqual([(s.start, s.end) for s in segments], [(1.234, 2.567), (3.001, 3.004)])
        self.assertIn("00:00:01,234 --> 00:00:02,567", build_srt_text(segments))
        self.assertIn("00:00:03,001 --> 00:00:03,004", build_srt_text(segments))


class TestSrtOutput(unittest.TestCase):
    def test_build_srt_outputs(self):
        segments = parse_srt_text(
            """
1
00:00:00,000 --> 00:00:01,500
Hello world

2
00:00:01,800 --> 00:00:03,000
How are you
"""
        )
        zh = {"0001": "你好世界", "0002": "你好吗"}
        english = build_srt_text(segments)
        translated = build_srt_text(segments, zh)
        bilingual = build_bilingual_srt_text(segments, zh)
        self.assertIn("Hello world", english)
        self.assertIn("你好世界", translated)
        self.assertIn("Hello world\n你好世界", bilingual)
        self.assertIn("-->", english)


class TestQCMetrics(unittest.TestCase):
    def setUp(self):
        self.segments = [
            SubtitleSegment("0001", 10.0, 11.0, "first", []),
            SubtitleSegment("0002", 10.80, 12.0, "second", []),
            SubtitleSegment("0004", 13.0, 13.50, "short", []),
        ]

    def test_qc_detects_overlap_and_count_issues(self):
        issues = run_qc(self.segments, translations={"0001": "第一", "0003": "第三"})
        codes = {issue.code for issue in issues}
        self.assertIn("OVERLAP", codes)
        self.assertIn("MISSING_ID", codes)
        self.assertIn("COUNT_MISMATCH", codes)
        self.assertIn("MISSING_TRANSLATION", codes)

    def test_qc_detects_short_and_empty_translation(self):
        issues = run_qc(
            self.segments,
            translations={"0001": "", "0002": "非常长" * 30, "0004": "短"},
            min_duration=1.2,
            max_chinese_chars=10,
        )
        codes = {issue.code for issue in issues}
        self.assertIn("SHORT_SEGMENT", codes)
        self.assertIn("EMPTY_TRANSLATION", codes)
        self.assertIn("LONG_CHINESE_SEGMENT", codes)


    def test_qc_reports_every_duplicate_and_counted_length(self):
        segments = [
            SubtitleSegment(seg_id, index * 2.0, index * 2.0 + 1.5, "line", [])
            for index, seg_id in enumerate(["0001", "0001", "0002", "0002", "0003"])
        ]
        issues = run_qc(segments, translations={"0001": "中 文 " * 20}, max_chinese_chars=10)
        duplicate = next(issue for issue in issues if issue.code == "DUPLICATE_ID")
        self.assertIn("(2): 0001, 0002", duplicate.message)
        long_text = next(issue for issue in issues if issue.code == "LONG_CHINESE_SEGMENT")
        self.assertIn("40 chars (limit 10)", long_text.message)

    def test_qc_accepts_ids_beyond_9999_segments(self):
        segments = [
            SubtitleSegment(f"{index:04d}", index * 2.0, index * 2.0 + 1.5, "line", [])
            for index in range(1, 10002)
        ]
        codes = {issue.code for issue in run_qc(segments)}
        self.assertNotIn("INVALID_ID", codes)


class TestTranslationParser(unittest.TestCase):
    def test_parse_translation_file(self):
        content = """
[0001]
美联储正试图压低通胀。

[0002]
但劳动力市场依然出乎意料地强劲。
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "translation_zh.txt"
            path.write_text(content, encoding="utf-8")
            parsed = parse_translation_file(path)
            self.assertEqual(parsed["0001"], "美联储正试图压低通胀。")
            self.assertEqual(parsed["0002"], "但劳动力市场依然出乎意料地强劲。")

        dup = "[0001]\nA\n\n[0001]\nB"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "translation_zh.txt"
            path.write_text(dup, encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_translation_file(path)


class TestLocalTranscription(unittest.TestCase):
    def test_language_mapping_and_filename_safety(self):
        self.assertEqual(resolve_language("en"), "en")
        self.assertEqual(resolve_language("fr-CA"), "fr")
        self.assertEqual(safe_filename("../../Interview: Montreal.mp4"), "Interview_ Montreal.mp4")
        with self.assertRaises(TranscriptionError):
            resolve_language("fr-FR")

    def test_whisperx_python_preserves_virtualenv_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "python"
            link.symlink_to(Path(sys.executable))
            self.assertEqual(find_whisperx_python(link), link.absolute())

    def test_mlx_result_to_master_segments(self):
        segments = segments_from_result(
            {
                "segments": [
                    {
                        "start": 1.234,
                        "end": 3.456,
                        "text": " Bonjour Montreal.",
                        "words": [
                            {"word": "Bonjour", "start": 1.234, "end": 2.1, "probability": 0.98},
                            {"word": "Montreal.", "start": 2.2, "end": 3.456, "probability": 0.95},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(segments[0].id, "0001")
        self.assertEqual(segments[0].start, 1.234)
        self.assertEqual(segments[0].text, "Bonjour Montreal.")
        self.assertEqual(segments[0].words[0]["probability"], 0.98)

    def test_aligned_text_is_restored_only_for_matching_cues(self):
        from subflow.transcription import _preserve_aligned_text

        originals = [
            SubtitleSegment("0001", 0.0, 1.0, "Hello,  world!", []),
            SubtitleSegment("0002", 1.0, 2.0, "Second line.", []),
            SubtitleSegment("0003", 2.0, 3.0, "Third line.", []),
        ]
        # Same count, but WhisperX split the first cue and dropped the second.
        aligned = [
            SubtitleSegment("", 0.0, 0.5, "hello world", []),
            SubtitleSegment("", 0.5, 1.0, "extra split", []),
            SubtitleSegment("", 2.0, 3.0, "third line", []),
        ]
        restored = _preserve_aligned_text(aligned, originals)
        self.assertEqual(
            [s.text for s in restored], ["Hello,  world!", "extra split", "Third line."]
        )

    def test_non_finite_timings_are_dropped(self):
        nan, inf = float("nan"), float("inf")
        segments = segments_from_result(
            {
                "segments": [
                    {"start": nan, "end": 2.0, "text": "nan start"},
                    {"start": 1.0, "end": inf, "text": "inf end"},
                    {
                        "start": 3.0,
                        "end": 4.0,
                        "text": "kept",
                        "words": [{"word": "kept", "start": nan, "end": 4.0, "probability": 0.9}],
                    },
                ]
            },
            enforce_transcript_quality=False,
        )
        self.assertEqual([s.text for s in segments], ["kept"])
        self.assertEqual(segments[0].words, [{"word": "kept", "end": 4.0, "probability": 0.9}])

    def test_mlx_quality_gate_rejects_repetition_and_high_compression(self):
        repeated = {
            "segments": [
                {"start": index, "end": index + 0.8, "text": "Looping text"}
                for index in range(3)
            ]
        }
        with self.assertRaisesRegex(TranscriptionError, "repeated segments"):
            segments_from_result(repeated)
        self.assertEqual(
            len(
                segments_from_result(
                    repeated,
                    enforce_transcript_quality=False,
                )
            ),
            3,
        )
        with self.assertRaisesRegex(TranscriptionError, "compression ratio"):
            segments_from_result(
                {
                    "segments": [
                        {
                            "start": 0,
                            "end": 1,
                            "text": "Bad output",
                            "compression_ratio": 3.1,
                        }
                    ]
                }
            )

    def test_transcription_master_uses_portable_model_references(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            legacy_home = Path("/").joinpath("Users", "private-user")
            external_path = Path("/").joinpath(
                "Volumes", "Client-Alpha", "private", "helper"
            )
            quoted_external_path = Path("/").joinpath(
                "Volumes", "Client Alpha", "private", "helper"
            )
            source = root / "interview.wav"
            source.write_bytes(b"source")
            model = root / "private" / "whisper-large-v3-turbo"
            large_model = root / "private" / "whisper-large-v3-mlx"
            result = MlxTranscription(
                segments=[SubtitleSegment("0001", 0.0, 1.0, "Hello", [])],
                quality_issues=[
                    {
                        "message": "retry failed",
                        "retry_error": str(legacy_home / "models" / "error.log"),
                        str(legacy_home / "cache" / "secret.log"): "path key",
                    }
                ],
                pipeline="vad-turbo-large-v3",
                warnings=[
                    f"worker failed at {external_path}",
                    f"worker failed at {quoted_external_path}",
                    f"worker failed at '{quoted_external_path}'",
                    f"worker:{legacy_home}/models/error.log",
                    f"worker returned file://{legacy_home}/models/error.log",
                    f"worker returned file://{quoted_external_path}",
                    f'worker returned "file://{quoted_external_path}"',
                    f"worker:file:{legacy_home}/models/error.log",
                    f"worker FILE://{legacy_home}/models/error.log",
                ],
            )
            with (
                patch("subflow.transcription._run_ffmpeg"),
                patch(
                    "subflow.transcription.transcribe_with_mlx",
                    return_value=result,
                ),
                patch(
                    "subflow.transcription.resolve_large_model_path",
                    return_value=large_model,
                ),
            ):
                artifact = transcribe_media(
                    source,
                    root / "project",
                    language="en",
                    alignment_mode="native",
                    model_path=model,
                    helper_path=root / "helper",
                )

            master_text = artifact.master_path.read_text(encoding="utf-8")
            metadata = json.loads(master_text)["metadata"]
            self.assertEqual(metadata["model_path"], model.name)
            self.assertEqual(metadata["large_v3_model_path"], large_model.name)
            self.assertNotIn(str(root), master_text)
            self.assertNotIn(str(legacy_home), master_text)
            self.assertNotIn("Client-Alpha", master_text)
            self.assertNotIn("Client Alpha", master_text)
            self.assertNotIn("file:/", master_text.lower())
            self.assertNotIn("worker:/", master_text)

    def test_alignment_scrubs_legacy_model_paths(self):
        with tempfile.TemporaryDirectory() as temp_value:
            project = Path(temp_value) / "project"
            legacy_home = Path("/").joinpath("Users", "private-user")
            project.mkdir()
            source = project / "interview.mp4"
            source.write_bytes(b"source")
            segments = [SubtitleSegment("0001", 0.0, 1.0, "Hello", [])]
            save_master_json(
                project / "master.json",
                segments,
                metadata={
                    "source": source.name,
                    "language": "en",
                    "model_path": str(legacy_home / "models" / "turbo"),
                    "large_v3_model_path": str(
                        legacy_home / "models" / "large-v3"
                    ),
                    "quality_issues": [
                        {
                            "retry_error": str(
                                legacy_home / "cache" / "error.log"
                            )
                        }
                    ],
                },
            )
            with (
                patch("subflow.transcription._run_ffmpeg"),
                patch(
                    "subflow.transcription._run_whisperx_alignment",
                    return_value=segments,
                ),
                patch(
                    "subflow.transcription.find_whisperx_python",
                    return_value=Path("/mock/whisperx-python"),
                ),
                patch(
                    "subflow.transcription.whisperx_version",
                    return_value="3.8.6",
                ),
            ):
                artifact = align_existing_project(project)

            aligned_text = artifact.master_path.read_text(encoding="utf-8")
            metadata = json.loads(aligned_text)["metadata"]
            self.assertEqual(metadata["model_path"], "turbo")
            self.assertEqual(metadata["large_v3_model_path"], "large-v3")
            self.assertNotIn(str(legacy_home), aligned_text)

    def test_mixed_language_project_realigns_per_window_language(self):
        with tempfile.TemporaryDirectory() as temp_value:
            project = Path(temp_value) / "project"
            project.mkdir()
            (project / "talk.mp4").write_bytes(b"source")
            segments = [
                SubtitleSegment("0001", 0.0, 2.0, "Hello there", []),
                SubtitleSegment("0002", 2.0, 4.0, "Bonjour à tous", []),
            ]
            save_master_json(
                project / "master.json",
                segments,
                metadata={
                    "source": "talk.mp4",
                    "language": "mixed",
                    "confidence_windows": [
                        {"start": 0.0, "end": 2.0, "language": "en", "segment_ids": ["0001"]},
                        {"start": 2.0, "end": 4.0, "language": "fr", "segment_ids": ["0002"]},
                    ],
                    "quality_issues": [
                        {"start": 2.5, "end": 3.0, "id": "0009", "segment_ids": ["0009"]}
                    ],
                },
            )
            # WhisperX splits the French cue in two, shifting later IDs.
            def fake_alignment(_wav, subset, *, language, **_kwargs):
                if language == "fr":
                    return [
                        SubtitleSegment("", 2.0, 2.8, "Bonjour", []),
                        SubtitleSegment("", 2.8, 4.0, "à tous", []),
                    ]
                return list(subset)

            with (
                patch("subflow.transcription._run_ffmpeg"),
                patch(
                    "subflow.transcription._run_whisperx_alignment",
                    side_effect=fake_alignment,
                ) as alignment,
                patch("subflow.transcription.find_whisperx_python", return_value=Path("/mock/python")),
                patch("subflow.transcription.whisperx_version", return_value="3.8.6"),
            ):
                artifact = align_existing_project(project)

            self.assertEqual(
                sorted(call.kwargs["language"] for call in alignment.call_args_list),
                ["en", "fr"],
            )
            metadata = json.loads(artifact.master_path.read_text(encoding="utf-8"))["metadata"]
            self.assertEqual(metadata["alignment"], "whisperx-mixed")
            self.assertEqual(metadata["confidence_windows"][1]["segment_ids"], ["0002", "0003"])
            self.assertEqual(metadata["quality_issues"][0]["segment_ids"], ["0002", "0003"])

    def test_realign_accepts_media_outside_the_project_via_source(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            project = root / "project"
            project.mkdir()
            media = root / "videos" / "talk.mp4"
            media.parent.mkdir()
            media.write_bytes(b"source")
            segments = [SubtitleSegment("0001", 0.0, 1.0, "Hello", [])]
            save_master_json(project / "master.json", segments, metadata={"source": "talk.mp4", "language": "en"})
            with self.assertRaisesRegex(TranscriptionError, "--source"):
                align_existing_project(project)
            other = root / "videos" / "other.mp4"
            other.write_bytes(b"x")
            with self.assertRaisesRegex(TranscriptionError, "talk.mp4"):
                align_existing_project(project, source_path=other)
            with (
                patch("subflow.transcription._run_ffmpeg") as ffmpeg,
                patch("subflow.transcription._run_whisperx_alignment", return_value=segments),
                patch("subflow.transcription.find_whisperx_python", return_value=Path("/mock/python")),
                patch("subflow.transcription.whisperx_version", return_value="3.8.6"),
            ):
                artifact = align_existing_project(project, source_path=media)
            self.assertEqual(ffmpeg.call_args.args[0], media.resolve())
            self.assertTrue(artifact.srt_path.is_file())

    def test_mixed_language_realign_without_windows_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temp_value:
            project = Path(temp_value) / "project"
            project.mkdir()
            (project / "talk.mp4").write_bytes(b"source")
            save_master_json(
                project / "master.json",
                [SubtitleSegment("0001", 0.0, 1.0, "Hello", [])],
                metadata={"source": "talk.mp4", "language": "mixed"},
            )
            with self.assertRaisesRegex(TranscriptionError, "per-window language"):
                align_existing_project(project)
