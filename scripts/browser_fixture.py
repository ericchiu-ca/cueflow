"""Isolated HTTP fixture using real UI/routes/storage and a synthetic translation provider."""

import json
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subflow import proc
from subflow.web import CueFlowHandler, JobManager

SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello browser\n"


class QuietHandler(CueFlowHandler):
    def log_message(self, *args):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix="cueflow-browser-") as temp:
        root = Path(temp)

        def manager():
            return JobManager(
                output_root=root / "projects",
                alignment_mode="auto",
                model_path=None,
                helper_path=None,
                whisperx_python=None,
                config_path=root / "config.json",
            )

        initial = manager()
        for identifier in ("retry", "cancel"):
            project = initial._project_dir(
                "translations", f"{identifier}.srt", identifier
            )
            (project / "source.srt").write_text(SRT, encoding="utf-8")
            initial._create_job(
                identifier,
                "translation",
                project,
                source_name=f"{identifier}.srt",
                source_language="en",
                model="auto",
                provider="codex",
            )
            initial._update(
                identifier, status="running" if identifier == "retry" else "failed"
            )
        initial.executor.shutdown(wait=True)
        m = manager()  # real restart -> interrupted, never auto-executed
        model = root / "model"
        model.mkdir()

        def fake_translation(project, segments, **kwargs):
            if project.name.endswith("cancel"):
                for _ in range(600):
                    proc.check_cancelled()
                    time.sleep(0.05)
            output, ids = project / "zh.srt", project / "translation_zh.txt"
            output.write_text(
                SRT.replace("Hello browser", "浏览器验证"), encoding="utf-8"
            )
            ids.write_text("[0001]\n浏览器验证\n", encoding="utf-8")
            return SimpleNamespace(
                zh_srt_path=output,
                translation_text_path=ids,
                warnings=[],
                segment_count=1,
                provider="fixture",
                model="synthetic",
            )

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        server.manager, server.max_upload_bytes, server.csrf_token = (
            m,
            1024 * 1024,
            "browser-fixture-token",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with (
            patch("subflow.web.run_translation_project", side_effect=fake_translation),
            patch("subflow.web.codex_environment_status", return_value={"ready": True}),
            patch("subflow.web.find_whisperx_python", return_value=None),
            patch("subflow.web.whisperx_version", return_value=None),
        ):
            thread.start()
            print(
                json.dumps(
                    {
                        "url": f"http://127.0.0.1:{server.server_address[1]}",
                        "model": str(model),
                    }
                ),
                flush=True,
            )
            try:
                input()  # closing stdin ends the isolated fixture
            except EOFError:
                pass
            finally:
                server.shutdown()
                server.server_close()
                m.close()
                m.executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
