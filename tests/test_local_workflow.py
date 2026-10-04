"""Regression coverage for resource settings, restart, retry and cancellation."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from subflow import proc
from subflow.cli import main as cli_main
from subflow.core import load_master_json, parse_srt_text, save_master_json
from subflow.job_store import JobStore
from subflow.provenance import write_provenance
from subflow.settings import (
    FIELDS,
    Settings,
    describe_settings,
    resolve_resource,
    resource_context,
)
from subflow.web import JobManager

SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"


def manager(root):
    return JobManager(
        output_root=root / "projects",
        alignment_mode="auto",
        model_path=None,
        helper_path=None,
        whisperx_python=None,
        config_path=root / "config.json",
    )


def failed_translation(m):
    project = m._project_dir("translations", "source.srt", "retry")
    (project / "source.srt").write_text(SRT)
    m._create_job(
        "retry",
        "translation",
        project,
        source_name="source.srt",
        source_language="en",
        model="auto",
        provider="codex",
    )
    m._update("retry", status="failed")
    return project


class SettingsTests(unittest.TestCase):
    def test_config_is_private_atomic_and_shared(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / "model"
            model.mkdir()
            store = Settings(root / "settings.json")
            store.save({"model_path": str(model), "helper_path": ""})
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(root.glob("*.tmp")), [])
            with patch.dict(os.environ, {"CUEFLOW_CONFIG": str(store.path)}):
                self.assertEqual(resolve_resource("model_path"), model)
                with resource_context({"model_path": str(root / "saved")}):
                    self.assertEqual(resolve_resource("model_path"), root / "saved")
                    with patch.dict(
                        os.environ, {FIELDS["model_path"]: str(root / "environment")}
                    ):
                        self.assertEqual(
                            resolve_resource("model_path"), root / "environment"
                        )
                        self.assertEqual(
                            resolve_resource("model_path", root / "command"),
                            root / "command",
                        )

            # A Web --config override must also reach synchronous ASS previews,
            # not only the background transcription/burn worker.
            store.save({"ffmpeg_path": sys.executable})
            m = JobManager(
                output_root=root / "preview-projects",
                alignment_mode="auto",
                model_path=None,
                helper_path=None,
                whisperx_python=None,
                config_path=store.path,
            )
            observed = []

            def preview(path, **kwargs):
                observed.append(resolve_resource("ffmpeg_path"))
                path.write_bytes(b"fixture-png")

            try:
                with patch("subflow.web.render_bilingual_preview", side_effect=preview):
                    m.default_ass_preview()
                    m.create_ass(
                        "source.srt", SRT, "zh.srt", SRT.replace("Hello", "你好")
                    )
                self.assertEqual(observed, [Path(sys.executable).absolute()] * 2)
            finally:
                m.close()

    def test_cli_and_web_select_the_same_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text("broken")
            with patch("subflow.web.serve") as serve:
                self.assertEqual(
                    cli_main(["--config", str(path), "web", "--no-open"]), 0
                )
                self.assertEqual(serve.call_args.kwargs["config_path"], str(path))
            Settings(path).save({})
            with (
                patch("subflow.cli.cmd_qc", return_value=0) as qc,
                patch("subflow.cli.build_parser") as parser,
            ):
                from argparse import Namespace

                parser.return_value.parse_args.return_value = Namespace(
                    settings_file=str(path),
                    command="qc",
                    func=lambda _: qc(resolve_resource("model_path")),
                )
                self.assertEqual(cli_main([]), 0)
                qc.assert_called_once()

    def test_discovery_uses_existing_moved_frameledger(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            model = (
                home
                / "Documents/ChatGPT/FrameLedger/.cache/models/whisper-large-v3-turbo"
            )
            model.mkdir(parents=True)
            with (
                patch("subflow.settings.Path.home", return_value=home),
                resource_context({}),
                patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(resolve_resource("model_path"), model)

    def test_invalid_save_leaves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Settings(Path(temp) / "settings.json")
            store.save({})
            original = store.path.read_bytes()
            for values in (
                {"secret": "no"},
                {"model_path": "relative"},
                {"helper_path": str(Path(temp) / "missing")},
                {"model_path": 123},
            ):
                with self.assertRaises(ValueError):
                    store.save(values)
                self.assertEqual(store.path.read_bytes(), original)

    def test_malformed_configuration_is_visible_and_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Settings(Path(temp) / "settings.json")
            store.path.write_text("broken")
            with self.assertRaisesRegex(ValueError, "Cannot read"):
                store.load()
            with (
                patch("subflow.ffmpeg_tools.find_ffmpeg", return_value=(None, [])),
                patch("subflow.transcription.find_whisperx_python", return_value=None),
            ):
                self.assertTrue(describe_settings(store)["error"])
            self.assertEqual(store.path.read_text(), "broken")
            requested, fallback = (
                Path(temp) / "requested-ffmpeg",
                Path(temp) / "working-ffmpeg",
            )
            for command in (requested, fallback):
                command.write_text("#!/bin/sh\nexit 0\n")
                command.chmod(0o700)
            store.save({"ffmpeg_path": str(requested)})
            with (
                patch("subflow.ffmpeg_tools.find_ffmpeg", return_value=(fallback, [])),
                patch("subflow.transcription.find_whisperx_python", return_value=None),
            ):
                description = describe_settings(store)["effective"]["ffmpeg_path"]
                self.assertEqual(description["path"], str(fallback))
                self.assertEqual(description["source"], "fallback")


class JobLifecycleTests(unittest.TestCase):
    def test_two_concurrent_retries_submit_once(self):
        with tempfile.TemporaryDirectory() as temp:
            m = manager(Path(temp))
            failed_translation(m)
            entered, release = threading.Event(), threading.Event()

            def submit(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))

            try:
                with (
                    patch.object(
                        m, "_submit_translation", side_effect=submit
                    ) as mocked,
                    ThreadPoolExecutor(2) as requests,
                ):
                    first = requests.submit(m.retry_translation, "retry")
                    self.assertTrue(entered.wait(5))
                    second = requests.submit(m.retry_translation, "retry")
                    self.assertEqual(second.result(5)["status"], "resuming")
                    release.set()
                    self.assertEqual(first.result(5)["id"], "retry")
                    self.assertEqual(mocked.call_count, 1)
            finally:
                release.set()
                m.close()
                m.executor.shutdown(wait=True)

    def test_restart_recovers_running_task_without_executing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = manager(root)
            project = failed_translation(first)
            first._update("retry", status="running")
            record = json.loads((project / "job.state.json").read_text())
            self.assertNotIn(str(root), json.dumps(record))
            first.executor.shutdown(wait=True)
            with patch("subflow.web.run_translation_project") as run:
                second = manager(root)
                try:
                    self.assertEqual(second.snapshot("retry")["status"], "interrupted")
                    run.assert_not_called()
                    with patch.object(second, "_submit_translation") as submit:
                        second.resume_job("retry")
                        self.assertEqual(submit.call_count, 1)
                        self.assertEqual(submit.call_args.args[0], "retry")
                finally:
                    second.close()

    def test_completed_task_and_output_survive_relocation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            m = manager(root)
            project = failed_translation(m)
            output = project / "zh.srt"
            output.write_text(SRT)
            m._update(
                "retry",
                status="complete",
                output_path=str(output),
                download_url="/api/translate/retry/download",
            )
            m.close()
            moved = root / "moved"
            m.output_root.rename(moved)
            jobs, warnings = JobStore(moved).load()
            self.assertFalse(warnings)
            self.assertEqual(jobs["retry"]["status"], "complete")
            self.assertEqual(Path(jobs["retry"]["output_path"]).read_text(), SRT)

    def test_malformed_and_escaped_records_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            m = manager(root)
            project = failed_translation(m)
            m.close()
            record = project / "job.state.json"
            payload = json.loads(record.read_text())
            for value in ("../outside", str(root / "outside")):
                payload["job"]["output_path"] = value
                record.write_text(json.dumps(payload))
                jobs, warnings = JobStore(m.output_root).load()
                self.assertFalse(jobs)
                self.assertEqual(len(warnings), 1)
            record.write_text("{")
            jobs, warnings = JobStore(m.output_root).load()
            self.assertFalse(jobs)
            self.assertEqual(len(warnings), 1)

    def test_cancel_running_job_preserves_checkpoint_and_blocks_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            m = manager(Path(temp))
            project = failed_translation(m)
            started, release = threading.Event(), threading.Event()
            checkpoint = project / "checkpoint.json"

            def work(report, project):
                checkpoint.write_text("saved")
                started.set()
                self.assertTrue(release.wait(5))
                return {"message": "should not complete"}

            try:
                m._submit(
                    "retry", work, queued_message="queued", failure_message="failed"
                )
                self.assertTrue(started.wait(5))
                with self.assertRaisesRegex(ValueError, "active tasks"):
                    m.update_configuration({})
                self.assertEqual(m.cancel_job("retry")["status"], "cancelling")
                release.set()
                m.executor.shutdown(wait=True)
                self.assertEqual(m.snapshot("retry")["status"], "cancelled")
                self.assertTrue(checkpoint.exists())
                self.assertTrue((project / "source.srt").exists())
                self.assertFalse(m._events)
                self.assertFalse(m._futures)
            finally:
                release.set()
                m.close()

    def test_queued_cancel_never_executes_work(self):
        with tempfile.TemporaryDirectory() as temp:
            m = manager(Path(temp))
            failed_translation(m)
            blocker_started, unblock = threading.Event(), threading.Event()

            def block():
                blocker_started.set()
                unblock.wait(5)

            blocker = m.executor.submit(block)
            self.assertTrue(blocker_started.wait(5))
            try:
                with patch("subflow.web.run_translation_project") as run:
                    m.resume_job("retry")
                    m.cancel_job("retry")
                    unblock.set()
                    blocker.result(5)
                    m.executor.shutdown(wait=True)
                    run.assert_not_called()
                    self.assertEqual(m.snapshot("retry")["status"], "cancelled")
            finally:
                unblock.set()
                m.close()

    def test_stale_resume_cannot_replace_a_new_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            m = manager(Path(temp))
            failed_translation(m)
            m._update("retry", status="resuming", attempt_id="new")
            try:
                m._submit(
                    "retry",
                    lambda *_: {},
                    queued_message="queued",
                    failure_message="failed",
                    expected_attempt="old",
                )
                self.assertEqual(m.snapshot("retry")["status"], "resuming")
                self.assertFalse(m._events)
            finally:
                m.close()

    def test_original_source_is_saved_before_queue_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            m = manager(Path(temp))
            try:
                with patch.object(m, "_submit_translation"):
                    job = m.create_translation_job("a.srt", SRT, "en", "auto")
                self.assertEqual((Path(job["project"]) / "source.srt").read_text(), SRT)
            finally:
                m.close()


class ProcessAndEvidenceTests(unittest.TestCase):
    def test_cancellation_kills_only_processes_owned_by_task(self):
        events = [threading.Event(), threading.Event()]
        processes = []
        try:
            for event in events:
                with proc.cancellation_scope(event):
                    processes.append(
                        proc.popen(
                            [sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    )
            proc.cancel(events[0])
            self.assertIsNotNone(processes[0].poll())
            self.assertIsNone(processes[1].poll())
            with (
                self.assertRaises(proc.CancelledError),
                proc.cancellation_scope(events[0]),
            ):
                pass
        finally:
            for p in processes:
                proc.kill_tree(p)
                proc.release(p)

    def test_kill_tree_reaches_descendants_after_parent_exits(self):
        import time

        with tempfile.TemporaryDirectory() as temp:
            heartbeat = Path(temp) / "heartbeat"
            worker = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); i=0\nwhile True:\n i+=1; p.write_text(str(i)); time.sleep(.02)"
            parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])"
            process = proc.popen(
                [sys.executable, "-c", parent, worker, str(heartbeat)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process.wait(timeout=5)
            try:
                for _ in range(100):
                    if heartbeat.exists():
                        break
                    time.sleep(0.01)
                self.assertTrue(heartbeat.exists(), "Grandchild never started")
                proc.kill_tree(process)
                time.sleep(0.05)
                stopped = heartbeat.read_text()
                time.sleep(0.10)
                self.assertEqual(
                    heartbeat.read_text(),
                    stopped,
                    "Grandchild survived after its parent exited",
                )
            finally:
                proc.kill_tree(process)
                proc.release(process)

    def test_provenance_hashes_contents_and_excludes_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.srt"
            source.write_text(SRT)
            model = root / "model"
            model.mkdir()
            (model / "weights.bin").write_bytes(b"weights")
            payload = write_provenance(
                root / "provenance.json",
                operation="fixture",
                inputs={"source": source},
                outputs={},
                models={"asr": model},
                tools={"external": None},
            )
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(
                payload["inputs"]["source"]["sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            self.assertTrue(payload["models"]["asr"]["inference_fingerprint"])
            self.assertNotIn(str(root), json.dumps(payload))
            self.assertIsNone(payload["tools"]["external"])

    def test_master_accepts_legacy_and_rejects_unknown_version(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "master.json"
            save_master_json(path, parse_srt_text(SRT))
            payload = json.loads(path.read_text())
            self.assertEqual(payload["schema_version"], 1)
            payload.pop("schema_version")
            path.write_text(json.dumps(payload))
            self.assertEqual(load_master_json(path)[0].text, "Hello")
            payload["schema_version"] = 99
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "schema version"):
                load_master_json(path)
