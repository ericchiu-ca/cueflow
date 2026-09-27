from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from subflow.advanced_asr import (
    _apply_ownership_boundaries,
    _score,
    _text_anomalies,
    _text_language_evidence,
    _tier,
    _words_to_text,
    transcribe_vad_cascade,
)
from subflow.core import SubtitleSegment
from subflow.transcription import (
    _attach_confidence_window_ids,
    _preserve_aligned_text,
    resolve_language,
    segments_from_result,
)
from subflow.vad_runner import _chunk_spans


class TestAdvancedAsr(unittest.TestCase):
    def test_confidence_window_ids_follow_final_aligned_timeline(self):
        windows = [
            {
                "start": 10.0,
                "end": 20.0,
                "segment_ids": ["0001"],
            }
        ]
        final_segments = [
            SubtitleSegment("0042", 11.0, 13.0, "final text", []),
            SubtitleSegment("0043", 21.0, 22.0, "outside", []),
        ]
        attached = _attach_confidence_window_ids(windows, final_segments)
        self.assertEqual(attached[0]["segment_ids"], ["0042"])

    def test_word_reconstruction_and_alignment_preserve_spaces(self):
        self.assertEqual(
            _words_to_text(
                [
                    {"word": "Les"},
                    {"word": "discours"},
                    {"word": "de"},
                    {"word": "son"},
                    {"word": "président"},
                    {"word": "."},
                ]
            ),
            "Les discours de son président.",
        )
        original = SubtitleSegment("0001", 1.0, 2.0, "Les discours de son président.", [])
        aligned = SubtitleSegment(
            "0001",
            1.1,
            2.1,
            "Lesdiscoursdesonprésident.",
            [{"word": "Les", "start": 1.1, "end": 1.2}],
        )
        restored = _preserve_aligned_text([aligned], [original])
        self.assertEqual(restored[0].text, original.text)
        self.assertEqual(restored[0].start, 1.1)

    def test_french_text_evidence_and_unspaced_text_detection(self):
        language, confidence = _text_language_evidence(
            "Le nouveau gouvernement est dans la rue et les citoyens sont avec lui."
        )
        self.assertEqual(language, "fr")
        self.assertGreaterEqual(confidence, 0.5)
        self.assertEqual(
            _text_anomalies(
                "LesdiscoursdesonprésidentPierreBourgaudenflammentlajeunesseetlestroupes"
            ),
            ["unspaced_text"],
        )

    def test_isolated_language_conflict_forces_large_v3_in_text_language(self):
        def raw(language, text, tag):
            return {
                "tag": tag,
                "language": language,
                "segments": [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": text,
                        "avg_logprob": -0.1,
                        "compression_ratio": 1.1,
                        "no_speech_prob": 0.02,
                        "words": [
                            {
                                "word": f" {text}",
                                "start": 0.0,
                                "end": 2.0,
                                "probability": 0.95,
                            }
                        ],
                    }
                ],
            }

        turbo = {
            "1": {"id": "1", "result": raw("fr", "Le gouvernement est dans la rue.", "turbo")},
            "2": {
                "id": "2",
                "result": raw(
                    "en",
                    "Le nouveau parti est dans la rue et les citoyens sont avec lui. "
                    "Lesdiscoursdesonprésidentenflammentlajeunesseetlestroupes",
                    "turbo",
                ),
            },
            "3": {"id": "3", "result": raw("fr", "Les citoyens sont avec le parti.", "turbo")},
        }
        large = {
            "2:fr": {
                "id": "2",
                "result": raw(
                    "fr",
                    "Le nouveau parti est dans la rue et les citoyens sont avec lui. "
                    "Les discours de son président enflamment la jeunesse et les troupes.",
                    "large",
                ),
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("subflow.advanced_asr._helper_python", return_value=Path("/fake/python")),
                patch(
                    "subflow.advanced_asr._run_json_worker",
                    return_value={
                        "windows": [
                            {"index": 1, "start": 0.0, "end": 2.0, "duration": 2.0},
                            {"index": 2, "start": 3.0, "end": 5.0, "duration": 2.0},
                            {"index": 3, "start": 6.0, "end": 8.0, "duration": 2.0},
                        ]
                    },
                ),
                patch(
                    "subflow.advanced_asr._write_clips",
                    return_value=[Path("/fake/1.wav"), Path("/fake/2.wav"), Path("/fake/3.wav")],
                ),
                patch(
                    "subflow.advanced_asr._batch_results",
                    side_effect=[turbo, large],
                ) as batches,
            ):
                result = transcribe_vad_cascade(
                    wav_path=Path("/fake/audio.wav"),
                    project_dir=Path(directory),
                    language="mixed",
                    turbo_model=Path("/fake/turbo"),
                    large_model=Path("/fake/large"),
                    helper_path=Path("/fake/run"),
                    whisperx_python=Path("/fake/whisperx"),
                    parse_result=segments_from_result,
                    quality_analyzer=lambda _: [],
                    environment_factory=dict,
                )

        retry_items = batches.call_args_list[1].args[2]
        self.assertEqual(
            retry_items,
            [{"id": "2:fr", "audio_path": "/fake/2.wav", "language": "fr"}],
        )
        self.assertEqual(result.large_v3_window_count, 1)
        self.assertEqual(result.large_v3_selected_count, 1)
        self.assertEqual(result.confidence_windows[1]["language"], "fr")
        self.assertEqual(result.confidence_windows[1]["model"], "large-v3")
        self.assertEqual(result.confidence_windows[1]["retry_language"], "fr")

    def test_low_confidence_window_escalates_and_selects_large_v3(self):
        turbo_result = {
            "tag": "turbo",
            "language": "fr",
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "bad transcript",
                    "avg_logprob": -1.5,
                    "compression_ratio": 5.0,
                    "no_speech_prob": 0.1,
                    "words": [
                        {
                            "word": " bad",
                            "start": 0.0,
                            "end": 1.0,
                            "probability": 0.2,
                        },
                        {
                            "word": " transcript",
                            "start": 1.0,
                            "end": 2.0,
                            "probability": 0.2,
                        },
                    ],
                }
            ],
        }
        large_result = {
            "tag": "large",
            "language": "fr",
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "correct transcript",
                    "avg_logprob": -0.1,
                    "compression_ratio": 1.1,
                    "no_speech_prob": 0.02,
                    "words": [
                        {
                            "word": " correct",
                            "start": 0.0,
                            "end": 1.0,
                            "probability": 0.95,
                        },
                        {
                            "word": " transcript",
                            "start": 1.0,
                            "end": 2.0,
                            "probability": 0.95,
                        },
                    ],
                }
            ],
        }

        def quality(result):
            if result.get("tag") == "turbo":
                return [
                    {
                        "severity": "WARN",
                        "code": "WHISPER_COMPRESSION_RATIO",
                        "message": "compression ratio is high",
                        "start": 0.0,
                        "end": 2.0,
                    }
                ]
            return []

        reports = []
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("subflow.advanced_asr._helper_python", return_value=Path("/fake/python")),
                patch(
                    "subflow.advanced_asr._run_json_worker",
                    return_value={
                        "windows": [
                            {"index": 1, "start": 0.0, "end": 2.0, "duration": 2.0}
                        ]
                    },
                ),
                patch(
                    "subflow.advanced_asr._write_clips",
                    return_value=[Path("/fake/window.wav")],
                ),
                patch(
                    "subflow.advanced_asr._batch_results",
                    side_effect=[
                        {"1": {"id": "1", "result": turbo_result}},
                        {"1:fr": {"id": "1:fr", "result": large_result}},
                    ],
                ) as batches,
            ):
                result = transcribe_vad_cascade(
                    wav_path=Path("/fake/audio.wav"),
                    project_dir=Path(directory),
                    language="fr",
                    turbo_model=Path("/fake/turbo"),
                    large_model=Path("/fake/large"),
                    helper_path=Path("/fake/run"),
                    whisperx_python=Path("/fake/whisperx"),
                    parse_result=segments_from_result,
                    quality_analyzer=quality,
                    environment_factory=dict,
                    progress=lambda percent, message: reports.append((percent, message)),
                )

        self.assertEqual([percent for percent, _ in reports], [34, 40, 58, 72])
        self.assertEqual(batches.call_count, 2)
        self.assertEqual(result.large_v3_window_count, 1)
        self.assertEqual(result.large_v3_selected_count, 1)
        self.assertEqual(result.confidence_windows[0]["model"], "large-v3")
        self.assertEqual(result.segments[0].text, "correct transcript")

    def test_cascade_raises_when_every_window_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("subflow.advanced_asr._helper_python", return_value=Path("/fake/python")),
                patch(
                    "subflow.advanced_asr._run_json_worker",
                    return_value={
                        "windows": [
                            {"index": 1, "start": 0.0, "end": 2.0, "duration": 2.0}
                        ]
                    },
                ),
                patch(
                    "subflow.advanced_asr._write_clips",
                    return_value=[Path("/fake/window.wav")],
                ),
                patch(
                    "subflow.advanced_asr._batch_results",
                    side_effect=lambda *args, **kwargs: {
                        key: {"id": key, "error": "model weights could not be loaded"}
                        for key in ("1", "1:en", "1:fr")
                    },
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "no subtitle segments.*weights"):
                    transcribe_vad_cascade(
                        wav_path=Path("/fake/audio.wav"),
                        project_dir=Path(directory),
                        language="en",
                        turbo_model=Path("/fake/turbo"),
                        large_model=Path("/fake/large"),
                        helper_path=Path("/fake/run"),
                        whisperx_python=Path("/fake/whisperx"),
                        parse_result=segments_from_result,
                        quality_analyzer=lambda _result: [],
                        environment_factory=dict,
                    )

    def test_metrics_ignore_non_finite_values(self):
        from subflow.advanced_asr import _metrics

        metrics = _metrics(
            {
                "segments": [
                    {"start": 0.0, "end": float("nan"), "avg_logprob": float("nan"), "words": [{"probability": float("nan")}]},
                    {"start": 0.0, "end": 1.0, "avg_logprob": -0.2, "words": [{"probability": 0.9}]},
                ]
            },
            2.0,
        )
        for value in metrics.values():
            if isinstance(value, float):
                self.assertTrue(value == value, metrics)  # no NaN leaks into scoring
        self.assertEqual(metrics["avg_logprob"], -0.2)

    def test_worker_errors_are_structured_truncated_and_protocol_checked(self):
        import sys

        from subflow.advanced_asr import _run_json_worker

        with tempfile.TemporaryDirectory() as directory:
            failing = Path(directory) / "failing_worker.py"
            failing.write_text(
                "import json, sys\n"
                "sys.stderr.write('torch warning\\n' * 5000)\n"
                "json.dump({'protocol': 'p1', 'error_type': 'ValueError', 'error': 'bad weights'}, sys.stderr)\n"
                "sys.exit(2)\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as caught:
                _run_json_worker(Path(sys.executable), failing, {"protocol": "p1"}, {}, timeout=30)
            self.assertTrue(str(caught.exception).endswith("ValueError: bad weights"))
            self.assertNotIn("torch warning", str(caught.exception))

            wrong = Path(directory) / "wrong_worker.py"
            wrong.write_text("import json, sys\njson.dump({'protocol': 'other'}, sys.stdout)\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "expected 'p1'"):
                _run_json_worker(Path(sys.executable), wrong, {"protocol": "p1"}, {}, timeout=30)

            listing = Path(directory) / "list_worker.py"
            listing.write_text("import json, sys\njson.dump([1, 2], sys.stdout)\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "non-object"):
                _run_json_worker(Path(sys.executable), listing, {"protocol": "p1"}, {}, timeout=30)

    def test_crashed_batch_worker_resumes_from_its_checkpoint(self):
        import os
        import sys
        import wave

        from subflow.advanced_asr import _batch_results

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model").mkdir()
            fake = root / "fake_modules"
            fake.mkdir()
            log = root / "calls.log"
            crashed = root / "crashed.marker"
            (fake / "mlx_whisper.py").write_text(
                "import os\n"
                f"LOG = {str(log)!r}\nMARK = {str(crashed)!r}\n"
                "def transcribe(path, **kwargs):\n"
                "    open(LOG, 'a').write(os.path.basename(path) + '\\n')\n"
                "    if path.endswith('clip-3.wav') and not os.path.exists(MARK):\n"
                "        open(MARK, 'w').close()\n"
                "        os._exit(9)  # hard crash mid-batch\n"
                "    return {'text': os.path.basename(path), 'segments': []}\n",
                encoding="utf-8",
            )
            items = []
            for index in range(1, 5):
                clip = root / f"clip-{index}.wav"
                with wave.open(str(clip), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(bytes([index]) * 3200)  # distinct content per clip
                items.append({"id": str(index), "audio_path": str(clip), "language": "en"})

            environment = dict(os.environ, PYTHONPATH=str(fake))
            checkpoint = root / "ckpt" / "turbo.jsonl"
            results = _batch_results(
                Path(sys.executable), root / "model", items, environment, checkpoint=checkpoint
            )

            self.assertEqual({key: value["result"]["text"] for key, value in results.items()},
                             {str(i): f"clip-{i}.wav" for i in range(1, 5)})
            calls = log.read_text().split()
            # clips 1-2 finished before the crash and were not transcribed again
            self.assertEqual(calls, ["clip-1.wav", "clip-2.wav", "clip-3.wav", "clip-3.wav", "clip-4.wav"])

            # A later run over the same audio reuses everything without a worker.
            log.unlink()
            again = _batch_results(Path("/nonexistent/python"), root / "model", items, environment, checkpoint=checkpoint)
            self.assertEqual(len(again), 4)
            self.assertFalse(log.exists())

    def test_worker_timeout_is_reported_as_runtime_error(self):
        import sys

        from subflow.advanced_asr import _run_json_worker

        with tempfile.TemporaryDirectory() as directory:
            slow = Path(directory) / "slow_worker.py"
            slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "timed out after 1 s"):
                _run_json_worker(Path(sys.executable), slow, {}, {}, timeout=1)

    def test_whisperx_chunk_dicts_are_converted_to_spans(self):
        self.assertEqual(
            _chunk_spans(
                [
                    {"start": 4.5, "end": 8.0, "segments": [1]},
                    {"start": 1.0, "end": 2.25, "segments": [0]},
                ]
            ),
            [(1.0, 2.25), (4.5, 8.0)],
        )

    def test_overlap_assigns_each_boundary_word_to_one_window(self):
        left = SubtitleSegment(
            id="",
            start=9.5,
            end=10.1,
            text="hello boundary",
            words=[
                {"word": " hello", "start": 9.5, "end": 9.9},
                {"word": " boundary", "start": 9.9, "end": 10.1},
            ],
        )
        right = SubtitleSegment(
            id="",
            start=9.9,
            end=10.4,
            text="boundary again",
            words=[
                {"word": " boundary", "start": 9.9, "end": 10.1},
                {"word": " again", "start": 10.1, "end": 10.4},
            ],
        )
        windows = [
            {"start": 0.0, "end": 10.2},
            {"start": 9.8, "end": 20.0},
        ]

        owned = _apply_ownership_boundaries([[left], [right]], windows)

        self.assertEqual(owned[0][0].text, "hello")
        self.assertEqual(owned[1][0].text, "boundary again")
        self.assertLessEqual(owned[0][0].end, owned[1][0].start)

    def test_non_overlapping_windows_are_unchanged(self):
        segment = SubtitleSegment("", 1.0, 2.0, "hello", [])
        owned = _apply_ownership_boundaries(
            [[segment], []],
            [{"start": 0.0, "end": 5.0}, {"start": 6.0, "end": 10.0}],
        )
        self.assertIs(owned[0][0], segment)

    def test_mixed_language_and_confidence_tiers(self):
        self.assertEqual(resolve_language("mixed"), "mixed")
        strong = _score(
            "clear speech",
            {
                "avg_logprob": -0.1,
                "max_compression_ratio": 1.2,
                "avg_no_speech_prob": 0.05,
                "avg_word_probability": 0.95,
                "speech_coverage": 0.8,
            },
            False,
        )
        weak = _score(
            "repeated repeated repeated",
            {
                "avg_logprob": -1.2,
                "max_compression_ratio": 9.0,
                "avg_no_speech_prob": 0.8,
                "avg_word_probability": 0.4,
                "speech_coverage": 0.03,
            },
            True,
        )
        self.assertEqual(_tier(strong), "high")
        self.assertEqual(_tier(weak), "low")

    def test_frontend_recognizes_mixed_whisperx_alignment(self):
        html = (
            Path(__file__).resolve().parents[1] / "subflow" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("(job.alignment||'').startsWith('whisperx')", html)
        self.assertNotIn("job.alignment==='whisperx'", html)


if __name__ == "__main__":
    unittest.main()
