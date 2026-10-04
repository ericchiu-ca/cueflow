"""Versioned per-project task records; no automatic execution on restore."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .settings import atomic_json

PATH_FIELDS = {
    "project",
    "media_path",
    "output_path",
    "master_path",
    "translation_text_path",
}
CATEGORIES = {
    "transcriptions": "transcription",
    "translations": "translation",
    "renders": "burn",
}
ACTIVE = {"uploading", "resuming", "queued", "running", "cancelling"}
STATUSES = ACTIVE | {"complete", "failed", "interrupted", "cancelled"}


class JobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _relative(self, value: str) -> str:
        return str(Path(value).resolve().relative_to(self.root))

    def save(self, job: dict) -> None:
        project = Path(job["project"])
        if not project.is_dir():
            return
        stored = dict(job)
        for key in PATH_FIELDS:
            if stored.get(key):
                stored[key] = self._relative(str(stored[key]))
        atomic_json(project / "job.state.json", {"schema_version": 1, "job": stored})

    def load(self) -> tuple[dict[str, dict], list[str]]:
        jobs, warnings = {}, []
        for category, kind in CATEGORIES.items():
            for record in sorted((self.root / category).glob("*/job.state.json")):
                try:
                    if (
                        record.is_symlink()
                        or record.parent.resolve().parent != self.root / category
                    ):
                        raise ValueError("Task record is outside its managed category.")
                    payload = json.loads(record.read_text(encoding="utf-8"))
                    if (
                        not isinstance(payload, dict)
                        or payload.get("schema_version") != 1
                    ):
                        raise ValueError("Unsupported task record version.")
                    job = payload.get("job")
                    if (
                        not isinstance(job, dict)
                        or job.get("kind") != kind
                        or job.get("status") not in STATUSES
                    ):
                        raise ValueError("Invalid task record.")
                    if (
                        not re.fullmatch(
                            r"[A-Za-z0-9_-]{1,128}", str(job.get("id", ""))
                        )
                        or job["id"] in jobs
                    ):
                        raise ValueError("Invalid or duplicate task ID.")
                    for key in PATH_FIELDS:
                        if job.get(key):
                            relative = Path(job[key])
                            if relative.is_absolute() or ".." in relative.parts:
                                raise ValueError("Invalid task path.")
                            absolute = (self.root / relative).resolve()
                            absolute.relative_to(self.root)
                            job[key] = str(absolute)
                    if (
                        Path(job.get("project", "")).resolve()
                        != record.parent.resolve()
                    ):
                        raise ValueError("Task project does not match its record.")
                    if job["status"] in ACTIVE:
                        job.update(
                            status="interrupted",
                            stage="interrupted",
                            message="Service stopped; resume this task when ready.",
                        )
                        self.save(job)
                    jobs[job["id"]] = job
                except (OSError, ValueError, TypeError, KeyError) as error:
                    warnings.append(f"{category}/{record.parent.name}: {error}")
        return jobs, warnings
