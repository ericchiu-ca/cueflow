import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subflow.core import parse_srt_text
from subflow.translation import (
    CodexCLITranslationProvider,
    OpenAIResponsesTranslationProvider,
    TranslationError,
    TranslationProvider,
    TranslationRequest,
    build_translation_prompt,
    parse_translation_payload,
    run_translation_project,
)


SOURCE_SRT = """1
00:00:01,000 --> 00:00:03,500
The labor market remains strong.

2
00:00:04,000 --> 00:00:06,000
Inflation is coming down.
"""


class FakeProvider(TranslationProvider):
    name = "fake"

    def translate(self, request, output_path, progress=None):
        payload = {
            "segments": [
                {"id": request.segments[0].id, "text": "劳动力市场依然强劲。"},
                {"id": request.segments[1].id, "text": "通胀正在下降。"},
            ]
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return payload


class RecordingProvider(TranslationProvider):
    """Translates every requested ID; optionally breaks chosen attempts."""

    name = "recording"

    def __init__(self, broken_attempts=()):
        self.requests = []
        self.broken_attempts = set(broken_attempts)

    def translate(self, request, output_path, progress=None):
        self.requests.append(request)
        if progress:
            progress(50, "working")
        items = [{"id": s.id, "text": f"译{s.id}"} for s in request.segments]
        if len(self.requests) in self.broken_attempts:
            items = items[:-1]  # drop one ID, as a truncated response would
        return {"segments": items}


def numbered_srt(count):
    return "".join(
        f"{index}\n00:{index // 60:02d}:{index % 60:02d},000 --> 00:{index // 60:02d}:{index % 60:02d},500\nLine {index}\n\n"
        for index in range(1, count + 1)
    )


class TranslationTests(unittest.TestCase):
    def test_payload_requires_exact_unique_nonempty_ids(self):
        segments = parse_srt_text(SOURCE_SRT)
        valid = {
            "segments": [
                {"id": segments[0].id, "text": "第一条"},
                {"id": segments[1].id, "text": "第二条"},
            ]
        }
        parsed = parse_translation_payload(valid, segments)
        self.assertEqual(parsed[segments[0].id], "第一条")

        duplicate = {
            "segments": [
                {"id": segments[0].id, "text": "第一条"},
                {"id": segments[0].id, "text": "重复"},
            ]
        }
        with self.assertRaisesRegex(TranslationError, "Duplicate"):
            parse_translation_payload(duplicate, segments)

        missing = {"segments": [{"id": segments[0].id, "text": "第一条"}]}
        with self.assertRaisesRegex(TranslationError, "Missing"):
            parse_translation_payload(missing, segments)

        empty = {
            "segments": [
                {"id": segments[0].id, "text": ""},
                {"id": segments[1].id, "text": "第二条"},
            ]
        }
        with self.assertRaisesRegex(TranslationError, "empty"):
            parse_translation_payload(empty, segments)

    def test_project_preserves_timeline_and_writes_artifacts(self):
        segments = parse_srt_text(SOURCE_SRT)
        with tempfile.TemporaryDirectory() as directory:
            artifacts = run_translation_project(
                Path(directory),
                segments,
                source_language="en",
                model="gpt-5.6-terra",
                provider_name="fake",
                provider_instance=FakeProvider(),
            )
            translated = parse_srt_text(
                artifacts.zh_srt_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                [(item.start, item.end) for item in translated],
                [(item.start, item.end) for item in segments],
            )
            self.assertEqual(translated[0].text, "劳动力市场依然强劲。")
            self.assertIn(
                "[0001]",
                artifacts.translation_input_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "[0002]",
                artifacts.translation_text_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(artifacts.segment_count, 2)

    def test_long_input_is_translated_in_batches_with_context(self):
        segments = parse_srt_text(numbered_srt(250))
        provider = RecordingProvider()
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            artifacts = run_translation_project(
                Path(directory),
                segments,
                source_language="en",
                provider_instance=provider,
                batch_size=120,
                progress=lambda percent, message: messages.append((percent, message)),
            )
            result = json.loads(artifacts.result_json_path.read_text(encoding="utf-8"))
            translated = parse_srt_text(artifacts.zh_srt_path.read_text(encoding="utf-8"))

        self.assertEqual([len(r.segments) for r in provider.requests], [120, 120, 10])
        self.assertEqual(provider.requests[0].context_before, ())
        self.assertEqual([s.id for s in provider.requests[1].context_before], ["0118", "0119", "0120"])
        self.assertEqual([s.id for s in provider.requests[1].context_after], ["0241", "0242", "0243"])
        self.assertEqual(len(result["segments"]), 250)
        self.assertEqual(translated[249].text, "译0250")
        self.assertTrue(any("Batch 2/3 (0121-0240)" in message for _, message in messages))
        self.assertEqual([p for p, _ in messages], sorted(p for p, _ in messages))

        prompt = build_translation_prompt(provider.requests[1])
        source_part, context_part = prompt.split("Context only", 1)
        self.assertIn('"id":"0121"', source_part)
        self.assertNotIn('"id":"0119"', source_part)
        self.assertIn('"id":"0119"', context_part)

    def test_invalid_batch_is_retried_once_then_reported(self):
        segments = parse_srt_text(numbered_srt(5))
        with tempfile.TemporaryDirectory() as directory:
            provider = RecordingProvider(broken_attempts={2})
            artifacts = run_translation_project(
                Path(directory), segments, source_language="en", provider_instance=provider, batch_size=3
            )
            self.assertEqual(len(provider.requests), 3)  # batch 2 needed a retry
            self.assertIn("译0005", artifacts.zh_srt_path.read_text(encoding="utf-8"))

            provider = RecordingProvider(broken_attempts={2, 3})
            with self.assertRaisesRegex(TranslationError, r"Batch 2/2 \(0004-0005\) failed after 2 attempts.*Missing"):
                run_translation_project(
                    Path(directory), segments, source_language="en", provider_instance=provider, batch_size=3
                )

    def test_codex_command_is_read_only_and_model_selectable(self):
        segments = tuple(parse_srt_text(SOURCE_SRT))
        with tempfile.TemporaryDirectory() as directory:
            fake_codex = Path(directory) / "codex"
            fake_codex.write_text("#!/bin/sh\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            provider = CodexCLITranslationProvider(str(fake_codex))
            request = TranslationRequest(segments, "en", "gpt-5.6-luna")
            command = provider.build_command(request, Path(directory) / "result.json")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[-1], "-")

    def test_codex_ignores_stale_result_from_previous_run(self):
        segments = tuple(parse_srt_text(SOURCE_SRT))
        with tempfile.TemporaryDirectory() as directory:
            fake_codex = Path(directory) / "codex"
            # Exits successfully without writing --output-last-message.
            fake_codex.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            output_path = Path(directory) / "translation.result.json"
            output_path.write_text(
                json.dumps({"items": [{"id": "0001", "text": "旧"}, {"id": "0002", "text": "旧"}]}),
                encoding="utf-8",
            )
            provider = CodexCLITranslationProvider(str(fake_codex), timeout_seconds=30)
            request = TranslationRequest(segments, "en", "auto")
            with patch(
                "subflow.translation.codex_environment_status",
                return_value={"ready": True},
            ):
                with self.assertRaisesRegex(TranslationError, "without writing"):
                    provider.translate(request, output_path)
            self.assertFalse(output_path.exists())

    def test_codex_login_status_is_matched_loosely_but_requires_chatgpt(self):
        from subflow.translation import codex_environment_status

        cases = [
            (0, "Logged in using ChatGPT", True),
            (0, "Signed in with ChatGPT (plus plan)", True),
            (0, "logged in using chatgpt\n", True),
            (0, "Logged in using an API key - sk-...", False),
            (1, "Not logged in", False),
            (0, "Not logged in to ChatGPT", False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            fake_codex = Path(directory) / "codex"
            fake_codex.write_text("#!/bin/sh\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            for code, message, expected in cases:
                completed = subprocess.CompletedProcess([], code, stdout=message, stderr="")
                with self.subTest(message=message), patch(
                    "subflow.translation.subprocess.run", return_value=completed
                ):
                    self.assertEqual(codex_environment_status(str(fake_codex))["ready"], expected)

    def test_codex_prompt_contains_exact_disclosed_fields(self):
        segments = tuple(parse_srt_text(SOURCE_SRT))
        prompt = build_translation_prompt(
            TranslationRequest(segments, "en", "gpt-5.6-terra")
        )
        payload = json.loads(prompt.split("Source segments:\n", 1)[1])
        self.assertIn("from English", prompt)
        self.assertEqual(
            set(payload[0]),
            {"id", "start", "end", "text"},
        )
        self.assertEqual(payload[0]["start"], 1.0)
        self.assertEqual(payload[0]["end"], 3.5)

    def test_api_provider_is_reserved_without_request(self):
        segments = tuple(parse_srt_text(SOURCE_SRT))
        provider = OpenAIResponsesTranslationProvider()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TranslationError, "reserved but not enabled"):
                provider.translate(
                    TranslationRequest(segments, "en", "gpt-5.6-terra"),
                    Path(directory) / "result.json",
                )


if __name__ == "__main__":
    unittest.main()
