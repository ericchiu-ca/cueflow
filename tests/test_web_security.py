import http.client
import json
import os
import shutil
import socket
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


class ServerFixture:
    def __init__(self, root: Path) -> None:
        self.manager = make_manager(root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CueFlowHandler)
        self.server.manager = self.manager
        self.server.max_upload_bytes = 1024 * 1024
        self.server.csrf_token = "test-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.manager.executor.shutdown(wait=True, cancel_futures=True)


class WebRobustnessTests(unittest.TestCase):
    def test_download_route_requires_matching_job_kind(self):
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = ServerFixture(Path(temp_value) / "output")
            try:
                srt = Path(temp_value) / "result.srt"
                srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
                fixture.manager.jobs["abc"] = {
                    "id": "abc",
                    "kind": "transcription",
                    "status": "complete",
                    "output_path": str(srt),
                    "download_name": "result.srt",
                }
                # Previously raised KeyError and closed the socket without a response.
                status, _ = fixture.get("/api/translate/abc/download?format=ids")
                self.assertEqual(status, 404)
                status, _ = fixture.get("/api/burn/abc/download")
                self.assertEqual(status, 404)
                status, body = fixture.get("/api/jobs/abc/download")
                self.assertEqual(status, 200)
                self.assertIn(b"Hi", body)
            finally:
                fixture.close()

    def test_responses_forbid_framing(self):
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = ServerFixture(Path(temp_value) / "output")
            try:
                for path in ("/", "/api/does-not-exist"):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", fixture.server.server_address[1], timeout=5
                    )
                    connection.request("GET", path)
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
                    self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
                    self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                    connection.close()
            finally:
                fixture.close()

    def test_review_audit_endpoint_returns_rules_and_pairing(self):
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = ServerFixture(Path(temp_value) / "output")
            try:
                body = json.dumps(
                    {
                        "tracks": {
                            "source": [
                                {"id": "0001", "start": 1, "end": 2, "text": "A"},
                                {"id": "0002", "start": 3, "end": 4, "text": ""},
                            ],
                            "chinese": [
                                {"id": "0001", "start": 1, "end": 2, "text": "甲"},
                                {"id": "0002", "start": 9, "end": 10, "text": "乙"},
                            ],
                        }
                    }
                ).encode("utf-8")
                connection = http.client.HTTPConnection("127.0.0.1", fixture.server.server_address[1], timeout=5)
                connection.request(
                    "POST",
                    "/api/review/audit",
                    body=body,
                    headers={"X-CueFlow-CSRF": "test-token", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual([issue["code"] for issue in payload["tracks"]["source"][1]], ["EMPTY_TEXT"])
                self.assertEqual(payload["pairing"]["drifted_ids"], ["0002"])
                # Audit writes nothing, so it must not create project folders.
                self.assertEqual(list((Path(temp_value) / "output").iterdir()), [])
            finally:
                fixture.close()

    def test_route_tables_resolve_each_path_to_one_existing_handler(self):
        for routes in (CueFlowHandler.GET_ROUTES, CueFlowHandler.POST_ROUTES):
            for route in routes:
                self.assertTrue(callable(getattr(CueFlowHandler, route[0], None)), route[0])
        expectations = {
            ("GET", "/"): "_get_index",
            ("GET", "/api/jobs/abc/"): "_get_job",
            ("GET", "/api/ass/abc/download"): "_get_ass_download",
            ("GET", "/api/burn/abc/download"): "_get_job_download",
            ("POST", "/api/ass"): "_post_ass",
            ("POST", "/api/ass-assets"): "_post_ass_asset",
            ("POST", "/api/review/audit"): "_post_review_audit",
            ("POST", "/api/review/abc/save"): "_post_review_save",
        }
        for (method, path), handler in expectations.items():
            routes = CueFlowHandler.GET_ROUTES if method == "GET" else CueFlowHandler.POST_ROUTES
            matches = [r[0] for r in routes if r[1].fullmatch(path.rstrip("/") or "/")]
            self.assertEqual(matches, [handler], (method, path))
        writes = {r[0]: r[2] for r in CueFlowHandler.POST_ROUTES}
        self.assertFalse(writes["_post_cleanup"])
        self.assertFalse(writes["_post_review_audit"])
        self.assertTrue(all(v for k, v in writes.items() if k not in {"_post_cleanup", "_post_review_audit"}))

    def test_unexpected_get_error_returns_json_500(self):
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = ServerFixture(Path(temp_value) / "output")
            try:
                with patch.object(fixture.manager, "environment", side_effect=KeyError("boom")):
                    status, body = fixture.get("/api/environment")
                self.assertEqual(status, 500)
                self.assertIn("error", json.loads(body))
            finally:
                fixture.close()

    def test_save_review_validates_every_track_before_writing(self):
        source_srt = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"
        chinese_srt = "1\n00:00:00,000 --> 00:00:01,000\n你好\n"
        with tempfile.TemporaryDirectory() as temp_value:
            manager = make_manager(Path(temp_value) / "output")
            try:
                review = manager.create_review("source.srt", source_srt, "zh.srt", chinese_srt)
                project = Path(review["project"])
                tracks = {
                    "source": review["tracks"]["source"]["segments"],
                    "chinese": "not a segment list",
                }
                with self.assertRaisesRegex(ValueError, "segment list"):
                    manager.save_review(review["id"], tracks)
                self.assertFalse((project / "output" / "reviewed.source.srt").exists())
                self.assertFalse((project / "master.reviewed.source.json").exists())

                tracks["chinese"] = review["tracks"]["chinese"]["segments"]
                saved = manager.save_review(review["id"], tracks)
                self.assertTrue((project / "output" / "reviewed.source.srt").is_file())
                self.assertTrue((project / "output" / "reviewed.zh.srt").is_file())
                self.assertEqual(saved["pairing"]["matched"], True)
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_review_pairing_reports_time_drift_with_equal_counts(self):
        source_srt = "1\n00:00:01,000 --> 00:00:02,000\nA\n\n2\n00:00:03,000 --> 00:00:04,000\nB\n\n3\n00:00:05,000 --> 00:00:06,000\nC\n"
        chinese_srt = "1\n00:00:01,000 --> 00:00:02,000\n甲\n\n2\n00:00:05,000 --> 00:00:06,000\n丙\n\n3\n00:00:07,000 --> 00:00:08,000\n丁\n"
        with tempfile.TemporaryDirectory() as temp_value:
            manager = make_manager(Path(temp_value) / "output")
            try:
                review = manager.create_review("a.srt", source_srt, "zh.srt", chinese_srt)
                self.assertFalse(review["pairing"]["matched"])
                self.assertEqual(review["pairing"]["drifted_ids"], ["0002", "0003"])
                self.assertEqual(review["pairing"]["first_divergence"]["id"], "0002")
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_rejected_requests_leave_no_project_directories(self):
        source_srt = "1\n00:00:01,000 --> 00:00:02,000\nA\n\n2\n00:00:03,000 --> 00:00:04,000\nB\n"
        drifted_srt = "1\n00:00:01,000 --> 00:00:02,000\n甲\n\n2\n00:00:09,000 --> 00:00:10,000\n乙\n"
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value) / "output"
            manager = make_manager(root)
            try:
                with patch("subflow.web.render_bilingual_preview") as preview:
                    with self.assertRaisesRegex(ValueError, "out of step"):
                        manager.create_ass("a.srt", source_srt, "zh.srt", drifted_srt)
                    with self.assertRaisesRegex(ValueError, "count mismatch"):
                        manager.create_ass("a.srt", source_srt, "zh.srt", drifted_srt.split("\n\n")[0])
                    preview.assert_not_called()
                with self.assertRaisesRegex(ValueError, "Chinese SRT contains no usable"):
                    manager.create_review("a.srt", source_srt, "zh.srt", "not an srt")
                with (
                    patch("subflow.web.render_bilingual_preview"),
                    patch.object(Path, "write_text", side_effect=OSError("disk full")),
                ):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        manager.create_ass("a.srt", source_srt, "zh.srt", source_srt)
                leftovers = [p for p in root.rglob("*")] if root.exists() else []
                self.assertEqual([p for p in leftovers if p.is_dir() and p.parent != root], [])
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_interrupted_uploads_leave_no_project_directories(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value) / "output"
            fixture = ServerFixture(root)
            try:
                for path in ("/api/ass-assets?filename=a.ass", "/api/jobs?filename=talk.mp4&language=en"):
                    with socket.create_connection(("127.0.0.1", fixture.server.server_address[1]), timeout=5) as raw:
                        raw.sendall(
                            (
                                f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                "X-CueFlow-CSRF: test-token\r\nContent-Length: 1000\r\n\r\npartial"
                            ).encode()
                        )
                        raw.shutdown(socket.SHUT_WR)
                        chunks = []
                        while chunk := raw.recv(4096):
                            chunks.append(chunk)
                        response = b"".join(chunks).decode(errors="replace")
                    self.assertIn(" 400 ", response.splitlines()[0])
                    self.assertIn("Upload ended before", response)
                project_dirs = [p for p in root.glob("*/*") if p.is_dir()]
                self.assertEqual(project_dirs, [])
                failed = [job for job in fixture.manager.jobs.values() if job["status"] == "failed"]
                self.assertEqual(len(failed), 1)  # the transcription job is still reported
            finally:
                fixture.close()

    def test_discard_project_only_removes_managed_project_directories(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value) / "output"
            manager = make_manager(root)
            try:
                outside = Path(temp_value) / "keep"
                outside.mkdir()
                unmanaged = root / "mine" / "project"
                unmanaged.mkdir(parents=True)
                category = root / "reviews"
                category.mkdir()
                for candidate in (outside, unmanaged, category, root):
                    manager.discard_project(candidate)
                self.assertTrue(outside.is_dir())
                self.assertTrue(unmanaged.is_dir())
                self.assertTrue(category.is_dir())
                managed = root / "reviews" / "20260926-a-1234"
                managed.mkdir()
                manager.discard_project(managed)
                self.assertFalse(managed.exists())
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)

    def test_cleanup_and_in_flight_writes_exclude_each_other(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value) / "output"
            manager = make_manager(root)
            try:
                (root / "reviews" / "job").mkdir(parents=True)
                with manager.operation():
                    with self.assertRaisesRegex(ValueError, "still writing"):
                        manager.cleanup_local_files()
                self.assertTrue((root / "reviews" / "job").is_dir())

                started = threading.Event()
                release = threading.Event()
                original_rmtree = shutil.rmtree

                def slow_rmtree(path, *args, **kwargs):
                    started.set()
                    release.wait(5)
                    return original_rmtree(path, *args, **kwargs)

                with patch("subflow.web.shutil.rmtree", side_effect=slow_rmtree):
                    worker = threading.Thread(target=manager.cleanup_local_files)
                    worker.start()
                    self.assertTrue(started.wait(5))
                    # Status polling must not block on the deletion...
                    self.assertIsNone(manager.snapshot("missing"))
                    # ...and new writers are refused until it finishes.
                    with self.assertRaisesRegex(ValueError, "being cleaned up"):
                        with manager.operation():
                            pass
                    release.set()
                    worker.join(5)

                self.assertFalse((root / "reviews").exists())
                with manager.operation():
                    pass
            finally:
                manager.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
