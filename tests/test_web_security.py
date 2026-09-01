import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from subflow.bilingual import VideoBurnError
from subflow.web import (
    CueFlowHandler,
    JobManager,
    request_host_is_loopback,
    validate_bind_host,
)


def make_manager(root: Path) -> JobManager:
    return JobManager(
        output_root=root,
        alignment_mode="auto",
        model_path=None,
        helper_path=None,
        whisperx_python=None,
    )


class WebSecurityTests(unittest.TestCase):
    def test_server_rejects_non_loopback_bind(self):
        self.assertEqual(validate_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertTrue(request_host_is_loopback("localhost:8765"))
        self.assertTrue(request_host_is_loopback("[::1]:8765"))
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            validate_bind_host("0.0.0.0")
        self.assertFalse(request_host_is_loopback("example.com:8765"))

    def test_cleanup_preserves_unrelated_output_root_files(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value) / "output"
            manager = make_manager(root)
            try:
                unrelated = root / "keep-me.txt"
                unrelated.write_text("not managed by CueFlow", encoding="utf-8")
                project = root / "translations" / "job"
                project.mkdir(parents=True)
                (project / "zh.srt").write_text("subtitle", encoding="utf-8")

                result = manager.cleanup_local_files()

                self.assertTrue(unrelated.is_file())
                self.assertFalse((root / "translations").exists())
                self.assertEqual(result["removed_files"], 1)
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_environment_uses_transcription_model_variable(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            model = root / "whisper-large-v3-turbo"
            model.mkdir()
            manager = make_manager(root / "output")
            try:
                with (
                    patch.dict(os.environ, {"SUBFLOW_MLX_MODEL": str(model)}),
                    patch("subflow.web.find_whisperx_python", return_value=None),
                    patch("subflow.web.whisperx_version", return_value=None),
                    patch("subflow.web.find_ass_ffmpeg", return_value=root / "ffmpeg"),
                    patch("subflow.web.codex_environment_status", return_value={}),
                ):
                    environment = manager.environment()

                self.assertEqual(environment["model"]["path"], str(model))
                self.assertTrue(environment["model"]["ready"])
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_ass_manifest_uses_portable_paths_and_errors(self):
        source_srt = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"
        chinese_srt = "1\n00:00:00,000 --> 00:00:01,000\n你好\n"
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            manager = make_manager(root / "output")
            try:
                def write_preview(path, **_kwargs):
                    path.write_bytes(b"png")

                with patch(
                    "subflow.web.render_bilingual_preview", side_effect=write_preview
                ):
                    result = manager.create_ass(
                        "source.srt", source_srt, "zh.srt", chinese_srt
                    )
                project = Path(result["project"])
                manifest = json.loads(
                    (project / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["output"], "output/bilingual.ass")
                self.assertEqual(manifest["preview"], "output/bilingual.preview.png")
                self.assertNotIn(str(root), json.dumps(manifest))

                private_path = root / "private" / "preview.log"
                with patch(
                    "subflow.web.render_bilingual_preview",
                    side_effect=VideoBurnError(f"failed at {private_path}"),
                ):
                    failed = manager.create_ass(
                        "source.srt", source_srt, "zh.srt", chinese_srt
                    )
                self.assertNotIn(str(root), failed["preview_error"])
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_post_requires_process_csrf_token(self):
        with tempfile.TemporaryDirectory() as temp_value:
            manager = make_manager(Path(temp_value) / "output")
            server = ThreadingHTTPServer(("127.0.0.1", 0), CueFlowHandler)
            server.manager = manager
            server.max_upload_bytes = 1024 * 1024
            server.csrf_token = "test-token"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with patch.object(manager, "environment", return_value={}):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    connection.request("GET", "/api/environment")
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["csrf_token"], "test-token")
                    connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("POST", "/api/cleanup", body=b"")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "POST",
                    "/api/cleanup",
                    body=b"",
                    headers={"X-CueFlow-CSRF": "test-token"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                manager.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
