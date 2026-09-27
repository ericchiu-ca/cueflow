import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import subflow.yt_workflow as workflow


class ExternalCommandTests(unittest.TestCase):
    def test_ensure_command_rejects_broken_executable(self):
        with tempfile.TemporaryDirectory() as temp_value:
            broken = Path(temp_value) / "yt-dlp"
            broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            broken.chmod(0o755)
            with patch.object(workflow.shutil, "which", return_value=str(broken)):
                with self.assertRaisesRegex(RuntimeError, "not runnable"):
                    workflow.ensure_command("yt-dlp")

    def test_ffmpeg_full_fallback_skips_broken_path_binary(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            broken = root / "ffmpeg"
            working = root / "ffmpeg-full"
            broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            working.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            broken.chmod(0o755)
            working.chmod(0o755)
            with (
                patch.dict(workflow.os.environ, {"SUBFLOW_FFMPEG": str(broken)}),
                patch.object(workflow, "FFMPEG_FULL_PATH", working),
                patch.object(workflow.shutil, "which", return_value=str(broken)),
            ):
                self.assertEqual(workflow.find_working_ffmpeg(), str(working.absolute()))

    def test_external_command_timeout_is_reported(self):
        with patch.object(
            workflow,
            "run_captured",
            side_effect=subprocess.TimeoutExpired(["yt-dlp"], 1),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                workflow.run_command(["yt-dlp", "url"], timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
