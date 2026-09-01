import tempfile
import unittest
from pathlib import Path

from subflow.web import JobManager


def make_manager(output_root: Path) -> JobManager:
    return JobManager(
        output_root=output_root,
        alignment_mode="auto",
        model_path=None,
        helper_path=None,
        whisperx_python=None,
    )


class TestLocalCleanup(unittest.TestCase):
    def test_cleanup_removes_outputs_and_stale_indexes(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value) / "projects"
            manager = make_manager(root)
            try:
                project = root / "transcriptions" / "example"
                output = project / "output"
                output.mkdir(parents=True)
                (project / "source.mp4").write_bytes(b"source-data")
                result_path = output / "result.srt"
                result_path.write_bytes(b"subtitle-data")
                manager.jobs["done"] = {"status": "complete", "project": str(project)}
                manager.ass_assets["ass"] = result_path
                manager.review_sessions["review"] = {"project": project}

                result = manager.cleanup_local_files()

                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["removed_files"], 2)
                self.assertGreaterEqual(result["freed_bytes"], 24)
                self.assertEqual(list(root.iterdir()), [])
                self.assertEqual(manager.jobs, {})
                self.assertEqual(manager.ass_assets, {})
                self.assertEqual(manager.review_sessions, {})
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_cleanup_refuses_while_a_job_is_active(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value) / "projects"
            manager = make_manager(root)
            try:
                project = root / "renders" / "active"
                project.mkdir(parents=True)
                source = project / "source.mp4"
                source.write_bytes(b"video")
                manager.jobs["active"] = {"status": "running", "project": str(project)}

                with self.assertRaisesRegex(ValueError, "active"):
                    manager.cleanup_local_files()

                self.assertTrue(source.is_file())
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
