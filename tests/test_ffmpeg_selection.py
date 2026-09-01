import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import subflow.transcription as transcription


class TestFfmpegSelection(unittest.TestCase):
    def test_skips_broken_path_ffmpeg_and_uses_ffmpeg_full(self):
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            broken = temp / "ffmpeg"
            working = temp / "ffmpeg-full"
            broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            working.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            broken.chmod(0o755)
            working.chmod(0o755)

            with (
                patch.dict(os.environ, {"SUBFLOW_FFMPEG": str(broken)}),
                patch.object(transcription, "FFMPEG_FULL_PATH", working),
                patch.object(transcription.shutil, "which", return_value=str(broken)),
            ):
                selected = transcription._ensure_ffmpeg()

            self.assertEqual(selected, str(working.absolute()))

    def test_helper_environment_prepends_selected_ffmpeg(self):
        selected = Path("/opt/test/ffmpeg-full/bin/ffmpeg")
        with (
            patch.object(transcription, "_ensure_ffmpeg", return_value=str(selected)),
            patch.dict(os.environ, {"PATH": "/opt/homebrew/bin:/usr/bin"}),
        ):
            environment = transcription._ffmpeg_subprocess_env()

        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(selected.parent))
        self.assertEqual(environment["FFMPEG_BINARY"], str(selected))
        self.assertEqual(environment["IMAGEIO_FFMPEG_EXE"], str(selected))


if __name__ == "__main__":
    unittest.main()
