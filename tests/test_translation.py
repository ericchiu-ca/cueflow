import json
import tempfile
import unittest
from pathlib import Path

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
