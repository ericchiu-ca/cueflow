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
        self.assertEqual(segments[0].start, 1.23)
        self.assertEqual(segments[0].text, "Bonjour Montreal.")
        self.assertEqual(segments[0].words[0]["probability"], 0.98)

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
