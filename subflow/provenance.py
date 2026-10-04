"""Portable artifact evidence; credentials and absolute paths are excluded."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .settings import atomic_json


def file_evidence(path: Path) -> dict:
    evidence = {"name": path.name, "sha256": None}
    if path.is_file():
        with path.open("rb") as handle:
            evidence["sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
    return evidence


def command_version(command: str | Path | None, flag: str = "-version") -> str | None:
    if not command:
        return None
    try:
        result = subprocess.run(
            [str(command), flag], capture_output=True, text=True, timeout=5, check=False
        )
        if result.returncode == 0:
            return (result.stdout or result.stderr).splitlines()[0][:200]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return None


def write_provenance(
    path: Path,
    *,
    operation: str,
    inputs: dict[str, Path],
    outputs: dict[str, Path],
    models: dict[str, Path] | None = None,
    tools: dict[str, str | None] | None = None,
    options: dict | None = None,
) -> dict:
    identities = {}
    if models:
        from .advanced_asr import _inference_fingerprint

        for label, model in models.items():
            try:
                fingerprint = _inference_fingerprint(model) if model.is_dir() else None
            except OSError:
                fingerprint = None
            identities[label] = {
                "name": model.name,
                "inference_fingerprint": fingerprint,
            }
    try:
        version = importlib.metadata.version("cueflow-subtitles")
    except importlib.metadata.PackageNotFoundError:
        version = "0.4.0"
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "inputs": {label: file_evidence(p) for label, p in inputs.items()},
        "outputs": {label: file_evidence(p) for label, p in outputs.items()},
        "models": identities,
        "runtime": {"cueflow": version, "python": platform.python_version()},
        "tools": tools or {},
        "options": options or {},
    }
    atomic_json(path, payload)
    return payload
