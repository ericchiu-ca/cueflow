import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import subflow.ffmpeg_tools as ffmpeg_tools
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
                patch.object(ffmpeg_tools, "FFMPEG_FULL_PATH", working),
                patch.object(ffmpeg_tools.shutil, "which", return_value=str(broken)),
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


    def test_probe_results_are_cached_until_the_binary_changes(self):
        with tempfile.TemporaryDirectory() as temp_value:
            counter = Path(temp_value) / "count"
            ffmpeg = Path(temp_value) / "ffmpeg"
            ffmpeg.write_text(f"#!/bin/sh\necho x >> {counter}\nexit 0\n", encoding="utf-8")
            ffmpeg.chmod(0o755)
            for _ in range(3):
                self.assertTrue(ffmpeg_tools.is_runnable(ffmpeg))
            self.assertEqual(counter.read_text().count("x"), 1)

            ffmpeg.write_text("#!/bin/sh\nexit 1\n# replaced binary\n", encoding="utf-8")
            self.assertFalse(ffmpeg_tools.is_runnable(ffmpeg))

    def test_all_callers_use_the_shared_finder(self):
        import subflow.bilingual as bilingual
        import subflow.yt_workflow as workflow

        with (
            patch.object(transcription, "find_ffmpeg", return_value=(None, [])),
            patch.object(workflow, "find_ffmpeg", return_value=(None, [])),
            patch.object(bilingual, "find_ffmpeg", return_value=(None, [])),
        ):
            with self.assertRaises(transcription.TranscriptionError):
                transcription._ensure_ffmpeg()
            with self.assertRaises(RuntimeError):
                workflow.find_working_ffmpeg()
            with self.assertRaises(bilingual.VideoBurnError):
                bilingual.find_ass_ffmpeg()


if __name__ == "__main__":
    unittest.main()
