from __future__ import annotations

import ipaddress
import json
import mimetypes
import re
import os
import secrets
import shutil
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .bilingual import (
    DEFAULT_FONTS_DIR,
    MULISH_FONT,
    SOURCE_HAN_FONT,
    VideoBurnError,
    build_bilingual_ass_text,
    burn_ass_into_video,
    find_ass_ffmpeg,
    render_bilingual_preview,
)
from . import proc
from .core import build_srt_text, parse_srt_text, save_master_json, track_pairing
from .review import (
    MAX_CHARS_BY_TRACK,
    audit_review_track,
    lenient_segments,
    review_payload,
    review_summary,
    segments_from_review_payload,
)
from .transcription import (
    SUPPORTED_EXTENSIONS,
    _portable_metadata,
    find_whisperx_python,
    resolve_large_model_path,
    safe_filename,
    transcribe_media,
    whisperx_version,
)
from .translation import (
    CODEX_MODEL_CHOICES,
    DEFAULT_CODEX_MODEL,
    SOURCE_LANGUAGE_NAMES,
    codex_environment_status,
    run_translation_project,
)


STATIC_FILE = Path(__file__).with_name("static") / "index.html"
DEFAULT_FRAMELEDGER_MODEL = (
    Path.home()
    / "Documents"
    / "FrameLedger"
    / ".cache"
    / "models"
    / "whisper-large-v3-turbo"
)
VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"})
ENCODING_PROFILES = ("hevc-source", "hevc", "h264", "quality", "fast")
MANAGED_PROJECT_CATEGORIES = frozenset(
    {"transcriptions", "translations", "bilingual", "reviews", "ass-assets", "renders"}
)


def is_loopback_host(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def request_host_is_loopback(value: str) -> bool:
    host = value.strip()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return is_loopback_host(host)


def _write_text_atomic(path: Path, text: str) -> None:
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        partial.write_text(text, encoding="utf-8")
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def validate_bind_host(host: str) -> str:
    if not is_loopback_host(host):
        raise ValueError(
            "CueFlow has no remote-user authentication and can only bind to "
            "127.0.0.1, ::1, or localhost."
        )
    return host


class JobManager:
    def __init__(
        self,
        *,
        output_root: str | Path,
        alignment_mode: str,
        model_path: str | Path | None,
        helper_path: str | Path | None,
        whisperx_python: str | Path | None,
    ) -> None:
        self.output_root = Path(output_root).expanduser().absolute()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.alignment_mode = alignment_mode
        self.model_path = model_path
        self.helper_path = helper_path
        self.whisperx_python = whisperx_python
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cueflow")
        self.jobs: dict[str, dict] = {}
        self.ass_assets: dict[str, Path] = {}
        self.review_sessions: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._preview_lock = threading.Lock()
        # Requests that write under output_root without being a queued job
        # (uploads, ASS/review creation, review saves); cleanup waits for none.
        self._active_operations = 0
        self._cleaning = False

    @contextmanager
    def operation(self):
        with self.lock:
            if self._cleaning:
                raise ValueError("Local files are being cleaned up. Try again in a moment.")
            self._active_operations += 1
        try:
            yield
        finally:
            with self.lock:
                self._active_operations -= 1

    def _project_dir(self, category: str, filename: str, identifier: str) -> Path:
        if category not in MANAGED_PROJECT_CATEGORIES:
            raise ValueError(f"Unsupported CueFlow project category: {category}")
        stem = Path(filename).stem or category
        cleaned = safe_filename(stem)[:72].strip(" ._-") or category
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        project = self.output_root / category / f"{stamp}-{cleaned}-{identifier[:8]}"
        project.mkdir(parents=True, exist_ok=False)
        return project

    @contextmanager
    def _new_project(self, category: str, filename: str, identifier: str):
        """Create a project directory that is removed again if setup fails."""
        project = self._project_dir(category, filename, identifier)
        try:
            yield project
        except BaseException:
            self.discard_project(project)
            raise

    def discard_project(self, project: Path) -> None:
        """Remove one CueFlow project directory, and never anything else."""
        resolved = Path(project).absolute()
        if (
            resolved.parent.parent == self.output_root
            and resolved.parent.name in MANAGED_PROJECT_CATEGORIES
            and resolved.is_dir()
            and not resolved.is_symlink()
        ):
            shutil.rmtree(resolved, ignore_errors=True)

    @staticmethod
    def _path_usage(path: Path) -> tuple[int, int]:
        files = 0
        total_bytes = 0
        candidates = [path] if path.is_file() or path.is_symlink() else path.rglob("*")
        for candidate in candidates:
            if not (candidate.is_file() or candidate.is_symlink()):
                continue
            try:
                total_bytes += candidate.lstat().st_size
                files += 1
            except OSError:
                continue
        return files, total_bytes

    def cleanup_local_files(self) -> dict:
        active_statuses = {"uploading", "queued", "running"}
        with self.lock:
            if self._cleaning:
                raise ValueError("Local file cleanup is already running.")
            active_jobs = [
                job for job in self.jobs.values() if job.get("status") in active_statuses
            ]
            if active_jobs:
                raise ValueError(
                    f"Cannot clean local files while {len(active_jobs)} job(s) are active."
                )
            if self._active_operations:
                raise ValueError(
                    f"Cannot clean local files while {self._active_operations} request(s) "
                    "are still writing files."
                )
            # Block new writers, then delete without holding the lock so status
            # polling stays responsive during a large rmtree.
            self._cleaning = True
            self.jobs.clear()
            self.ass_assets.clear()
            self.review_sessions.clear()

        try:
            entries = [
                self.output_root / category
                for category in sorted(MANAGED_PROJECT_CATEGORIES)
                if (self.output_root / category).exists()
                or (self.output_root / category).is_symlink()
            ]
            removed_files = 0
            freed_bytes = 0
            for entry in entries:
                file_count, byte_count = self._path_usage(entry)
                if entry.is_symlink() or entry.is_file():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry)
                removed_files += file_count
                freed_bytes += byte_count
        finally:
            with self.lock:
                self._cleaning = False

        return {
            "status": "complete",
            "removed_files": removed_files,
            "removed_entries": len(entries),
            "freed_bytes": freed_bytes,
        }

    def _create_job(self, identifier: str, kind: str, project: Path, **metadata: object) -> dict:
        job = {
            "id": identifier,
            "kind": kind,
            "status": "uploading",
            "stage": "upload",
            "percent": 0,
            "message": "Receiving local file",
            "project": str(project),
            "error": None,
            "download_url": None,
            "download_name": None,
            **metadata,
        }
        with self.lock:
            self.jobs[identifier] = job
        return dict(job)

    def _update(self, identifier: str, **changes: object) -> None:
        with self.lock:
            if identifier in self.jobs:
                self.jobs[identifier].update(changes)

    def mark_upload_failed(self, identifier: str) -> None:
        self._update(identifier, status="failed", stage="failed", message="Upload failed")
        snapshot = self.snapshot(identifier)
        if snapshot:
            self.discard_project(Path(snapshot["project"]))

    def snapshot(self, identifier: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(identifier)
            return dict(job) if job else None

    def prepare_transcription_upload(self, filename: str, language: str, alignment: str) -> tuple[dict, Path]:
        identifier = uuid.uuid4().hex
        cleaned = safe_filename(filename)
        if Path(cleaned).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported media type {Path(cleaned).suffix or '(none)'}; use one of "
                + ", ".join(sorted(SUPPORTED_EXTENSIONS))
            )
        project = self._project_dir("transcriptions", cleaned, identifier)
        media = project / cleaned
        job = self._create_job(
            identifier,
            "transcription",
            project,
            language=language,
            alignment_requested=alignment,
        )
        return job, media

    def _submit(
        self,
        identifier: str,
        work,
        *,
        queued_message: str,
        failure_message: str,
        running: dict | None = None,
    ) -> None:
        """Queue `work(report, project) -> completion fields` with shared bookkeeping."""
        self._update(identifier, status="queued", stage="queued", percent=1, message=queued_message)

        def run() -> None:
            self._update(identifier, status="running", **(running or {}))

            def report(stage: str, percent: int, message: str) -> None:
                self._update(identifier, stage=stage, percent=percent, message=message)

            try:
                snapshot = self.snapshot(identifier)
                if snapshot is None:
                    raise RuntimeError("Job disappeared from the local queue.")
                completed = work(report, Path(snapshot["project"]))
                self._update(identifier, status="complete", stage="complete", percent=100, **completed)
            except Exception as error:
                self._update(
                    identifier,
                    status="failed",
                    stage="failed",
                    message=failure_message,
                    error=str(error),
                )

        self.executor.submit(run)

    def start_transcription(self, identifier: str, media: Path, language: str, alignment: str) -> None:
        def work(report, project: Path) -> dict:
            result = transcribe_media(
                media,
                project,
                language=language,
                alignment_mode=alignment,
                model_path=self.model_path,
                helper_path=self.helper_path,
                whisperx_python=self.whisperx_python,
                progress=report,
            )
            summary = result.confidence_summary
            return {
                "message": (
                    f"SRT generated · confidence H{summary.get('high', 0)}"
                    f"/M{summary.get('medium', 0)}/L{summary.get('low', 0)}"
                    + (
                        f" · {len(result.quality_issues)} item(s) need review"
                        if result.quality_issues
                        else ""
                    )
                ),
                "alignment": result.alignment,
                "warnings": result.warnings,
                "quality_issues": result.quality_issues,
                "confidence_summary": summary,
                "review_required": bool(result.quality_issues),
                "download_url": f"/api/jobs/{identifier}/download",
                "download_name": result.srt_path.name,
                "output_path": str(result.srt_path),
                "master_path": str(result.master_path),
            }

        self._submit(
            identifier,
            work,
            queued_message="Queued for local transcription",
            failure_message="Transcription failed",
        )

    def create_translation_job(
        self,
        source_filename: str,
        source_srt: str,
        source_language: str,
        model: str,
        provider: str = "codex",
    ) -> dict:
        if source_language not in SOURCE_LANGUAGE_NAMES:
            raise ValueError(f"Unsupported source language: {source_language}")
        if model not in CODEX_MODEL_CHOICES:
            raise ValueError(f"Unsupported translation model: {model}")
        if provider not in {"codex", "openai-api"}:
            raise ValueError(f"Unsupported translation provider: {provider}")
        segments = parse_srt_text(source_srt)
        if not segments:
            raise ValueError("The uploaded SRT contains no subtitle segments.")

        identifier = uuid.uuid4().hex
        project = self._project_dir("translations", source_filename, identifier)
        self._create_job(
            identifier,
            "translation",
            project,
            source_name=Path(source_filename).name or "source.srt",
            source_language=source_language,
            provider=provider,
            model=model,
        )
        self._submit_translation(identifier, segments, source_filename, source_language, model, provider)
        snapshot = self.snapshot(identifier)
        if snapshot is None:
            raise RuntimeError("Could not create the translation job.")
        return snapshot

    def retry_translation(self, identifier: str) -> dict:
        """Re-run a failed translation in its project, reusing validated batches."""
        job = self.snapshot(identifier)
        if job is None or job.get("kind") != "translation":
            raise ValueError("Translation job not found.")
        if job.get("status") != "failed":
            raise ValueError("Only a failed translation can be retried.")
        source = Path(job["project"]) / "source.srt"
        if not source.is_file():
            raise ValueError("The original subtitles are no longer available; start a new translation.")
        segments = parse_srt_text(source.read_text(encoding="utf-8"))
        self._update(identifier, error=None, warnings=[])
        self._submit_translation(
            identifier,
            segments,
            str(job.get("source_name") or "source.srt"),
            str(job["source_language"]),
            str(job["model"]),
            str(job["provider"]),
        )
        snapshot = self.snapshot(identifier)
        if snapshot is None:
            raise RuntimeError("Could not requeue the translation job.")
        return snapshot

    def _submit_translation(
        self,
        identifier: str,
        segments: list,
        source_filename: str,
        source_language: str,
        model: str,
        provider: str,
    ) -> None:
        def work(report, project: Path) -> dict:
            artifacts = run_translation_project(
                project,
                segments,
                source_language=source_language,
                model=model,
                provider_name=provider,
                source_filename=source_filename,
                progress=report,
            )
            return {
                "message": "Chinese subtitles generated",
                "warnings": list(artifacts.warnings),
                "segment_count": artifacts.segment_count,
                "provider": artifacts.provider,
                "model": artifacts.model,
                "output_path": str(artifacts.zh_srt_path),
                "download_name": "zh.srt",
                "download_url": f"/api/translate/{identifier}/download",
                "translation_text_path": str(artifacts.translation_text_path),
                "translation_text_name": "translation_zh.txt",
                "translation_text_url": f"/api/translate/{identifier}/download?format=ids",
            }

        self._submit(
            identifier,
            work,
            queued_message="Queued for subtitle translation",
            failure_message="Translation failed",
            running={"stage": "translation", "percent": 8, "message": "Preparing stable subtitle IDs"},
        )

    def create_ass(self, source_name: str, source_text: str, chinese_name: str, chinese_text: str) -> dict:
        # Validate and build in memory first: a rejected pair (count mismatch,
        # timeline drift) must not leave an empty project directory behind.
        source_segments = parse_srt_text(source_text)
        chinese_segments = parse_srt_text(chinese_text)
        ass_text = build_bilingual_ass_text(
            source_segments,
            chinese_segments,
            title=f"{Path(source_name).stem} / {Path(chinese_name).stem}",
        )
        count = len(source_segments)
        identifier = uuid.uuid4().hex
        with self._new_project("bilingual", source_name, identifier) as project:
            output_dir = project / "output"
            output_dir.mkdir()
            ass_path = output_dir / "bilingual.ass"
            (project / "source.srt").write_text(source_text, encoding="utf-8")
            (project / "zh.srt").write_text(chinese_text, encoding="utf-8")
            ass_path.write_text(ass_text, encoding="utf-8")
            preview_path = output_dir / "bilingual.preview.png"
            preview_url = None
            preview_error = None
            try:
                render_bilingual_preview(
                    preview_path,
                    source_text=source_segments[0].text,
                    chinese_text=chinese_segments[0].text,
                )
                preview_url = f"/api/ass/{identifier}/preview"
            except VideoBurnError as error:
                preview_error = str(_portable_metadata(str(error)))
            manifest = {
                "source_file": source_name,
                "chinese_file": chinese_name,
                "segment_count": count,
                "timeline": "source",
                "output": str(ass_path.relative_to(project)),
                "preview": (
                    str(preview_path.relative_to(project)) if preview_url else None
                ),
                "preview_error": preview_error,
            }
            (project / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        with self.lock:
            self.ass_assets[identifier] = ass_path
        return {
            "id": identifier,
            "status": "complete",
            "segment_count": count,
            "project": str(project),
            "download_name": ass_path.name,
            "download_url": f"/api/ass/{identifier}/download",
            "preview_url": preview_url,
            "preview_error": preview_error,
        }

    def create_review(
        self,
        source_name: str,
        source_text: str,
        chinese_name: str | None = None,
        chinese_text: str | None = None,
    ) -> dict:
        source_segments = parse_srt_text(source_text)
        if not source_segments:
            raise ValueError("The SRT contains no usable subtitle segments.")
        tracks: dict[str, dict] = {
            "source": {
                "name": source_name,
                "expected_ids": [segment.id for segment in source_segments],
                "segments": review_payload(source_segments, max_source_chars=MAX_CHARS_BY_TRACK["source"]),
            }
        }
        chinese_segments = None
        if chinese_text is not None:
            chinese_segments = parse_srt_text(chinese_text)
            if not chinese_segments:
                raise ValueError("The Chinese SRT contains no usable subtitle segments.")
            tracks["chinese"] = {
                "name": chinese_name or "zh.srt",
                "expected_ids": [segment.id for segment in chinese_segments],
                "segments": review_payload(chinese_segments, max_source_chars=MAX_CHARS_BY_TRACK["chinese"]),
            }
        summaries = {key: review_summary(value["segments"]) for key, value in tracks.items()}
        pairing = self._pairing(source_segments, chinese_segments)

        identifier = uuid.uuid4().hex
        with self._new_project("reviews", source_name, identifier) as project:
            (project / "source.srt").write_text(source_text, encoding="utf-8")
            if chinese_text is not None:
                (project / "zh.srt").write_text(chinese_text, encoding="utf-8")
            (project / "review.json").write_text(
                json.dumps(
                    {
                        "tracks": {key: value["segments"] for key, value in tracks.items()},
                        "summaries": summaries,
                        "pairing": pairing,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        session = {
            "project": project,
            "lock": threading.Lock(),
            "tracks": {
                key: {
                    "name": value["name"],
                    "expected_ids": value["expected_ids"],
                    "output_path": None,
                }
                for key, value in tracks.items()
            },
        }
        with self.lock:
            self.review_sessions[identifier] = session
        return {
            "id": identifier,
            "tracks": {
                key: {"name": value["name"], "segments": value["segments"], "summary": summaries[key]}
                for key, value in tracks.items()
            },
            "pairing": pairing,
            "project": str(project),
        }

    @staticmethod
    def _pairing(source: list, chinese: list | None) -> dict:
        if chinese is None:
            return {"source_count": len(source), "chinese_count": 0, "matched": True}
        return track_pairing(source, chinese)

    def save_review(self, identifier: str, raw_tracks: object) -> dict:
        with self.lock:
            session = self.review_sessions.get(identifier)
        if session is None:
            raise ValueError("The review session is no longer available. Load the SRT again.")
        if not isinstance(raw_tracks, dict):
            raise ValueError("Review save request must contain subtitle tracks.")
        project = Path(session["project"])
        output_dir = project / "output"
        with session["lock"]:
            # Validate every track before writing any file so a rejected
            # Chinese track cannot leave a freshly overwritten source track.
            prepared = []
            for track, track_session in session["tracks"].items():
                segments, reviewed = segments_from_review_payload(
                    raw_tracks.get(track),
                    track_session["expected_ids"],
                    allow_structure_changes=True,
                )
                payload = review_payload(
                    segments,
                    reviewed,
                    max_source_chars=MAX_CHARS_BY_TRACK[track],
                )
                prepared.append((track, track_session, segments, payload))

            output_dir.mkdir(exist_ok=True)
            results: dict[str, dict] = {}
            persisted_tracks: dict[str, list[dict]] = {}
            for track, track_session, segments, payload in prepared:
                summary = review_summary(payload)
                filename = "reviewed.zh.srt" if track == "chinese" else "reviewed.source.srt"
                output_path = output_dir / filename
                srt_text = build_srt_text(segments)
                _write_text_atomic(output_path, srt_text)
                save_master_json(
                    project / f"master.reviewed.{track}.json",
                    segments,
                    metadata={
                        "source_file": track_session["name"],
                        "reviewed_segments": summary["reviewed"],
                        "issue_segments": summary["issue_segments"],
                    },
                )
                results[track] = {
                    "name": track_session["name"],
                    "segments": payload,
                    "summary": summary,
                    "srt": srt_text,
                    "download_name": filename,
                    "download_url": f"/api/review/{identifier}/download?track={track}",
                }
                persisted_tracks[track] = payload
                with self.lock:
                    track_session["output_path"] = output_path
            segments_by_track = {track: segments for track, _session, segments, _payload in prepared}
            pairing = self._pairing(segments_by_track["source"], segments_by_track.get("chinese"))
            _write_text_atomic(
                project / "review.json",
                json.dumps(
                    {
                        "tracks": persisted_tracks,
                        "summaries": {key: value["summary"] for key, value in results.items()},
                        "pairing": pairing,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return {
            "id": identifier,
            "tracks": results,
            "pairing": pairing,
        }

    def review_path(self, identifier: str, track: str) -> Path | None:
        with self.lock:
            session = self.review_sessions.get(identifier)
            track_session = session.get("tracks", {}).get(track) if session else None
            path = track_session.get("output_path") if track_session else None
        return Path(path) if path and Path(path).is_file() else None

    def prepare_ass_upload(self, filename: str) -> tuple[str, Path]:
        identifier = uuid.uuid4().hex
        project = self._project_dir("ass-assets", filename, identifier)
        return identifier, project / "bilingual.ass"

    def register_ass_upload(self, identifier: str, path: Path) -> dict:
        with self.lock:
            self.ass_assets[identifier] = path
        return {"id": identifier, "status": "complete", "filename": path.name}

    def ass_path(self, identifier: str) -> Path | None:
        with self.lock:
            path = self.ass_assets.get(identifier)
        return path if path and path.is_file() else None

    def default_ass_preview(self) -> Path:
        preview = self.output_root / "ass-assets" / "style-preview.png"
        with self.operation(), self._preview_lock:
            if not preview.is_file():
                preview.parent.mkdir(parents=True, exist_ok=True)
                partial = preview.with_name(f".style-preview.{uuid.uuid4().hex}.png")
                try:
                    render_bilingual_preview(partial)
                    partial.replace(preview)
                finally:
                    partial.unlink(missing_ok=True)
        return preview

    def ass_preview_path(self, identifier: str) -> Path | None:
        ass = self.ass_path(identifier)
        if ass is None:
            return None
        preview = ass.with_name("bilingual.preview.png")
        return preview if preview.is_file() else None

    def prepare_burn_upload(self, filename: str, ass_identifier: str, profile: str) -> tuple[dict, Path]:
        ass_path = self.ass_path(ass_identifier)
        if ass_path is None:
            raise ValueError("The selected ASS subtitle is no longer available. Upload or generate it again.")
        if profile not in ENCODING_PROFILES:
            raise ValueError("Encoding profile must be one of: " + ", ".join(ENCODING_PROFILES))
        identifier = uuid.uuid4().hex
        cleaned = safe_filename(filename)
        # Also keeps the upload from colliding with the project's fixed
        # bilingual.ass / output entries.
        if Path(cleaned).suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(
                f"Unsupported video type {Path(cleaned).suffix or '(none)'}; use one of "
                + ", ".join(sorted(VIDEO_EXTENSIONS))
            )
        with self._new_project("renders", cleaned, identifier) as project:
            shutil.copy2(ass_path, project / "bilingual.ass")
        video = project / cleaned
        job = self._create_job(
            identifier,
            "burn",
            project,
            profile=profile,
            ass_id=ass_identifier,
        )
        return job, video

    def start_burn(self, identifier: str, video: Path, profile: str) -> None:
        def work(report, project: Path) -> dict:
            output_dir = project / "output"
            output_dir.mkdir(exist_ok=True)
            output = output_dir / f"{video.stem}.bilingual.mp4"
            warnings: list[str] = []
            burn_ass_into_video(
                video,
                project / "bilingual.ass",
                output,
                profile=profile,
                progress=report,
                warnings=warnings,
            )
            return {
                "message": "Bilingual MP4 generated locally",
                "warnings": warnings,
                "download_url": f"/api/burn/{identifier}/download",
                "download_name": output.name,
                "output_path": str(output),
            }

        self._submit(
            identifier,
            work,
            queued_message="Queued for video encoding",
            failure_message="Video encoding failed",
        )

    def environment(self) -> dict:
        configured_model = self.model_path or os.environ.get("SUBFLOW_MLX_MODEL")
        model = Path(configured_model).expanduser() if configured_model else DEFAULT_FRAMELEDGER_MODEL
        whisperx_python = find_whisperx_python(self.whisperx_python)
        version = whisperx_version(whisperx_python)
        try:
            ffmpeg = find_ass_ffmpeg()
            ffmpeg_status = {"ready": True, "path": str(ffmpeg)}
        except VideoBurnError as error:
            ffmpeg_status = {"ready": False, "error": str(error)}
        fonts = {
            "ready": all((DEFAULT_FONTS_DIR / name).is_file() for name in (SOURCE_HAN_FONT, MULISH_FONT)),
            "path": str(DEFAULT_FONTS_DIR),
        }
        return {
            "model": {"ready": model.is_dir(), "path": str(model)},
            "large_model": {
                "ready": resolve_large_model_path().is_dir(),
                "path": str(resolve_large_model_path()),
            },
            "whisperx": {
                "ready": whisperx_python is not None and version is not None,
                "python": str(whisperx_python) if whisperx_python else None,
                "version": version,
            },
            "ffmpeg": ffmpeg_status,
            "fonts": fonts,
            "codex": codex_environment_status(),
            "translation": {
                "default_provider": "codex",
                "default_model": DEFAULT_CODEX_MODEL,
                "models": list(CODEX_MODEL_CHOICES),
                "openai_api": {"reserved": True, "enabled": False},
            },
            "privacy": (
                "Media processing stays local. The Codex request includes subtitle "
                "text, stable IDs, start/end times, source language, and the selected "
                "model; video and audio are not uploaded."
            ),
        }


class CueFlowHandler(BaseHTTPRequestHandler):
    server_version = "CueFlow/0.4"

    @property
    def manager(self) -> JobManager:
        return self.server.manager  # type: ignore[attr-defined]

    @property
    def max_upload_bytes(self) -> int:
        return self.server.max_upload_bytes  # type: ignore[attr-defined]

    @property
    def csrf_token(self) -> str:
        return self.server.csrf_token  # type: ignore[attr-defined]

    def end_headers(self) -> None:
        # Loopback Host checks do not stop another site from framing the UI
        # (e.g. clickjacking the cleanup button), so forbid framing outright.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[web] {self.address_string()} {format % args}")

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)

    def _content_length(self, *, maximum: int | None = None) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length header.") from error
        limit = maximum if maximum is not None else self.max_upload_bytes
        if length <= 0:
            raise ValueError("The request body is empty.")
        if length > limit:
            readable = f"{limit / 1024**3:.1f} GB" if limit >= 1024**3 else f"{limit / 1024**2:.0f} MB"
            raise ValueError(f"Request body exceeds the configured limit of {readable}.")
        return length

    def _receive_file(self, destination: Path) -> None:
        remaining = self._content_length()
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.upload")
        try:
            with partial.open("wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Upload ended before the declared Content-Length.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def _read_json(self) -> dict:
        length = self._content_length(maximum=24 * 1024 * 1024)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must be valid UTF-8 JSON.") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON request must be an object.")
        return payload

    def _send_file(self, path: Path, download_name: str, *, attachment: bool = True) -> None:
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Output file is no longer available.")
            return
        content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        if attachment:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)

    # Routes are (method-name, pattern). GET and POST each share one guard
    # and error handler; POST routes flagged `writes` run inside
    # manager.operation() so cleanup cannot delete their files mid-write.
    GET_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("_get_index", re.compile(r"/")),
        ("_get_environment", re.compile(r"/api/environment")),
        ("_get_style_preview", re.compile(r"/api/ass-preview")),
        ("_get_job", re.compile(r"/api/(?P<route>jobs|translate|burn)/(?P<id>[^/]+)")),
        ("_get_ass_preview", re.compile(r"/api/ass/(?P<id>[^/]+)/preview")),
        ("_get_review_download", re.compile(r"/api/review/(?P<id>[^/]+)/download")),
        ("_get_ass_download", re.compile(r"/api/ass/(?P<id>[^/]+)/download")),
        (
            "_get_job_download",
            re.compile(r"/api/(?P<route>jobs|translate|burn)/(?P<id>[^/]+)/download"),
        ),
    )
    POST_ROUTES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
        ("_post_cleanup", re.compile(r"/api/cleanup"), False),
        ("_post_review_audit", re.compile(r"/api/review/audit"), False),
        ("_post_transcription", re.compile(r"/api/jobs"), True),
        ("_post_translation", re.compile(r"/api/translate"), True),
        ("_post_translation_retry", re.compile(r"/api/translate/(?P<id>[^/]+)/retry"), True),
        ("_post_ass", re.compile(r"/api/ass"), True),
        ("_post_review", re.compile(r"/api/review"), True),
        ("_post_review_save", re.compile(r"/api/review/(?P<id>[^/]+)/save"), True),
        ("_post_ass_asset", re.compile(r"/api/ass-assets"), True),
        ("_post_burn", re.compile(r"/api/burn"), True),
    )
    JOB_KINDS = {"jobs": "transcription", "translate": "translation", "burn": "burn"}

    @staticmethod
    def _match(routes, path: str):
        normalized = path.rstrip("/") or "/"
        for route in routes:
            match = route[1].fullmatch(normalized)
            if match:
                return route, match.groupdict()
        return None, {}

    def do_GET(self) -> None:
        if not request_host_is_loopback(self.headers.get("Host", "")):
            self._error(HTTPStatus.FORBIDDEN, "CueFlow accepts loopback Host headers only.")
            return
        parsed = urlparse(self.path)
        route, params = self._match(self.GET_ROUTES, parsed.path)
        try:
            if route is None:
                self._error(HTTPStatus.NOT_FOUND, "Route not found.")
                return
            getattr(self, route[0])(parse_qs(parsed.query), **params)
        except ConnectionError:
            return
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unexpected local server error: {error}")

    def do_POST(self) -> None:
        if not request_host_is_loopback(self.headers.get("Host", "")):
            self._error(HTTPStatus.FORBIDDEN, "CueFlow accepts loopback Host headers only.")
            return
        supplied_token = self.headers.get("X-CueFlow-CSRF", "")
        if not supplied_token or not secrets.compare_digest(supplied_token, self.csrf_token):
            self._error(HTTPStatus.FORBIDDEN, "Missing or invalid CueFlow request token.")
            return
        parsed = urlparse(self.path)
        route, params = self._match(self.POST_ROUTES, parsed.path)
        try:
            if route is None:
                self._error(HTTPStatus.NOT_FOUND, "Route not found.")
                return
            handler = getattr(self, route[0])
            if route[2]:
                with self.manager.operation():
                    handler(parse_qs(parsed.query), **params)
            else:
                handler(parse_qs(parsed.query), **params)
        except (ValueError, RuntimeError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unexpected local server error: {error}")

    # -- GET handlers -------------------------------------------------------

    def _get_index(self, _query: dict) -> None:
        body = STATIC_FILE.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _get_environment(self, _query: dict) -> None:
        payload = self.manager.environment()
        payload["csrf_token"] = self.csrf_token
        self._send_json(payload)

    def _get_style_preview(self, _query: dict) -> None:
        try:
            preview = self.manager.default_ass_preview()
        except VideoBurnError as error:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
            return
        self._send_file(preview, "cueflow-ass-preview.png", attachment=False)

    def _get_job(self, _query: dict, *, route: str, id: str) -> None:
        job = self.manager.snapshot(id)
        if job is None or job.get("kind") != self.JOB_KINDS[route]:
            self._error(HTTPStatus.NOT_FOUND, "Job not found.")
        else:
            self._send_json(job)

    def _get_ass_preview(self, _query: dict, *, id: str) -> None:
        path = self.manager.ass_preview_path(id)
        if path is None:
            self._error(HTTPStatus.NOT_FOUND, "ASS preview not found.")
        else:
            self._send_file(path, "bilingual.preview.png", attachment=False)

    def _get_review_download(self, query: dict, *, id: str) -> None:
        track = query.get("track", ["source"])[0]
        if track not in {"source", "chinese"}:
            raise ValueError("Review track must be source or chinese.")
        path = self.manager.review_path(id, track)
        if path is None:
            self._error(HTTPStatus.NOT_FOUND, "Reviewed SRT output not found.")
        else:
            self._send_file(path, "reviewed.zh.srt" if track == "chinese" else "reviewed.source.srt")

    def _get_ass_download(self, _query: dict, *, id: str) -> None:
        path = self.manager.ass_path(id)
        if path is None:
            self._error(HTTPStatus.NOT_FOUND, "ASS output not found.")
        else:
            self._send_file(path, "bilingual.ass")

    def _get_job_download(self, query: dict, *, route: str, id: str) -> None:
        job = self.manager.snapshot(id)
        if not job or job.get("kind") != self.JOB_KINDS[route] or job.get("status") != "complete":
            self._error(HTTPStatus.NOT_FOUND, "Completed output not found.")
            return
        if route == "translate" and query.get("format") == ["ids"]:
            path_key, name_key = "translation_text_path", "translation_text_name"
        else:
            path_key, name_key = "output_path", "download_name"
        if not job.get(path_key) or not job.get(name_key):
            self._error(HTTPStatus.NOT_FOUND, "Completed output not found.")
        else:
            self._send_file(Path(job[path_key]), str(job[name_key]))

    # -- POST handlers ------------------------------------------------------

    def _post_cleanup(self, _query: dict) -> None:
        self._send_json(self.manager.cleanup_local_files(), HTTPStatus.OK)

    def _post_review_audit(self, _query: dict) -> None:
        # Read-only: the editor calls this after edits so the page never
        # carries its own copy of the review rules.
        payload = self._read_json()
        raw_tracks = payload.get("tracks")
        if not isinstance(raw_tracks, dict):
            raise ValueError("Review audit request must contain subtitle tracks.")
        result: dict = {
            "tracks": {
                track: audit_review_track(segments, track)
                for track, segments in raw_tracks.items()
            }
        }
        if raw_tracks.get("source") and raw_tracks.get("chinese"):
            result["pairing"] = track_pairing(
                lenient_segments(raw_tracks["source"]),
                lenient_segments(raw_tracks["chinese"]),
            )
        self._send_json(result)

    def _receive_job_upload(self, job: dict, destination: Path) -> None:
        try:
            self._receive_file(destination)
        except Exception:
            self.manager.mark_upload_failed(job["id"])
            raise

    def _send_job_snapshot(self, identifier: str, missing_message: str) -> None:
        snapshot = self.manager.snapshot(identifier)
        if snapshot is None:
            raise RuntimeError(missing_message)
        self._send_json(snapshot, HTTPStatus.ACCEPTED)

    def _post_transcription(self, query: dict) -> None:
        filename = query.get("filename", [""])[0]
        language = query.get("language", ["en"])[0]
        alignment = query.get("alignment", [self.manager.alignment_mode])[0]
        if language not in {"en", "fr-CA", "mixed"}:
            raise ValueError("Language must be en, fr-CA, or mixed.")
        if alignment not in {"auto", "native", "whisperx"}:
            raise ValueError("Alignment must be auto, native, or whisperx.")
        job, destination = self.manager.prepare_transcription_upload(filename, language, alignment)
        self._receive_job_upload(job, destination)
        self.manager.start_transcription(job["id"], destination, language, alignment)
        self._send_job_snapshot(job["id"], "Could not create the local transcription job.")

    def _post_translation(self, _query: dict) -> None:
        payload = self._read_json()
        source_filename = payload.get("source_filename")
        source_srt = payload.get("source_srt")
        if not isinstance(source_filename, str) or not source_filename.strip():
            raise ValueError("A source SRT filename is required.")
        if not isinstance(source_srt, str) or not source_srt.strip():
            raise ValueError("A source SRT is required for translation.")
        result = self.manager.create_translation_job(
            source_filename,
            source_srt,
            str(payload.get("source_language") or "en"),
            str(payload.get("model") or DEFAULT_CODEX_MODEL),
            str(payload.get("provider") or "codex"),
        )
        self._send_json(result, HTTPStatus.ACCEPTED)

    def _post_translation_retry(self, _query: dict, *, id: str) -> None:
        self._send_json(self.manager.retry_translation(id), HTTPStatus.ACCEPTED)

    def _post_ass(self, _query: dict) -> None:
        payload = self._read_json()
        required = ("source_filename", "source_srt", "translation_filename", "translation_srt")
        if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
            raise ValueError("Both source-language and Chinese SRT files are required.")
        result = self.manager.create_ass(
            payload["source_filename"],
            payload["source_srt"],
            payload["translation_filename"],
            payload["translation_srt"],
        )
        self._send_json(result, HTTPStatus.CREATED)

    def _post_review(self, _query: dict) -> None:
        payload = self._read_json()
        source_filename = payload.get("source_filename", payload.get("filename"))
        source_srt = payload.get("source_srt", payload.get("srt"))
        if not isinstance(source_filename, str) or not source_filename.strip():
            raise ValueError("A source SRT filename is required.")
        if not isinstance(source_srt, str) or not source_srt.strip():
            raise ValueError("A source SRT is required for review.")
        chinese_filename = payload.get("chinese_filename")
        chinese_srt = payload.get("chinese_srt")
        if (chinese_filename is None) != (chinese_srt is None):
            raise ValueError("Chinese SRT filename and content must be provided together.")
        if chinese_srt is not None and (
            not isinstance(chinese_filename, str)
            or not chinese_filename.strip()
            or not isinstance(chinese_srt, str)
            or not chinese_srt.strip()
        ):
            raise ValueError("The optional Chinese SRT is invalid.")
        self._send_json(
            self.manager.create_review(source_filename, source_srt, chinese_filename, chinese_srt),
            HTTPStatus.CREATED,
        )

    def _post_review_save(self, _query: dict, *, id: str) -> None:
        payload = self._read_json()
        tracks = payload.get("tracks")
        if tracks is None and "segments" in payload:
            tracks = {"source": payload.get("segments")}
        self._send_json(self.manager.save_review(id, tracks), HTTPStatus.OK)

    def _post_ass_asset(self, query: dict) -> None:
        filename = query.get("filename", [""])[0]
        if Path(filename).suffix.lower() != ".ass":
            raise ValueError("Please upload an .ass subtitle file.")
        identifier, destination = self.manager.prepare_ass_upload(filename)
        try:
            self._receive_file(destination)
        except Exception:
            self.manager.discard_project(destination.parent)
            raise
        self._send_json(self.manager.register_ass_upload(identifier, destination), HTTPStatus.CREATED)

    def _post_burn(self, query: dict) -> None:
        filename = query.get("filename", [""])[0]
        ass_identifier = query.get("ass_id", [""])[0]
        profile = query.get("profile", ["hevc-source"])[0]
        job, destination = self.manager.prepare_burn_upload(filename, ass_identifier, profile)
        self._receive_job_upload(job, destination)
        self.manager.start_burn(job["id"], destination, profile)
        self._send_job_snapshot(job["id"], "Could not create the local video job.")


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    output_root: str | Path = "projects/local",
    alignment_mode: str = "auto",
    model_path: str | Path | None = None,
    helper_path: str | Path | None = None,
    whisperx_python: str | Path | None = None,
    open_browser: bool = True,
    max_upload_gb: float = 20.0,
) -> None:
    host = validate_bind_host(host)
    manager = JobManager(
        output_root=output_root,
        alignment_mode=alignment_mode,
        model_path=model_path,
        helper_path=helper_path,
        whisperx_python=whisperx_python,
    )
    server = ThreadingHTTPServer((host, port), CueFlowHandler)
    server.manager = manager  # type: ignore[attr-defined]
    server.max_upload_bytes = int(max_upload_gb * 1024**3)  # type: ignore[attr-defined]
    server.csrf_token = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/"
    print(f"CueFlow local web UI: {url}")
    print(f"Projects: {manager.output_root}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping CueFlow web UI.")
    finally:
        manager.executor.shutdown(wait=False, cancel_futures=True)
        # Kill running FFmpeg/ASR children so the worker thread can finish
        # instead of keeping the interpreter alive until the job completes.
        proc.terminate_all()
        server.server_close()
