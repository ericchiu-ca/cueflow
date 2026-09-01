import unittest

from subflow.cli import build_parser


class ReleaseCLITests(unittest.TestCase):
    def test_translate_command_exposes_model_and_provider(self):
        args = build_parser().parse_args(
            [
                "translate-srt",
                "source.srt",
                "--output",
                "project",
                "--language",
                "fr-CA",
                "--model",
                "gpt-5.6-sol",
            ]
        )
        self.assertEqual(args.language, "fr-CA")
        self.assertEqual(args.model, "gpt-5.6-sol")
        self.assertEqual(args.provider, "codex")


if __name__ == "__main__":
    unittest.main()
