"""Private local resource settings shared by CLI and Web workers."""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

FIELDS = {
    "model_path": "SUBFLOW_MLX_MODEL",
    "large_model_path": "SUBFLOW_MLX_LARGE_MODEL",
    "helper_path": "SUBFLOW_MLX_HELPER",
    "whisperx_python": "SUBFLOW_WHISPERX_PYTHON",
    "ffmpeg_path": "SUBFLOW_FFMPEG",
}
_context = threading.local()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with partial.open("x", encoding="utf-8") as handle:
            os.chmod(partial, 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def default_config_path() -> Path:
    override = os.environ.get("CUEFLOW_CONFIG")
    return (
        Path(override).expanduser()
        if override
        else Path.home() / "Library/Application Support/CueFlow/config.json"
    )


def candidates(field: str) -> list[Path]:
    roots = [
        Path.home() / "Documents/ChatGPT/FrameLedger",
        Path.home() / "Documents/FrameLedger",
    ]
    if field == "model_path":
        return [r / ".cache/models/whisper-large-v3-turbo" for r in roots]
    if field == "large_model_path":
        return [r / ".cache/models/whisper-large-v3-mlx" for r in roots]
    if field == "helper_path":
        return [r / "phase2/mlx_whisper_asr/run" for r in roots]
    if field == "whisperx_python":
        return [
            Path.cwd() / ".venv-whisperx/bin/python",
            Path.home() / "Documents/ChatGPT/cueflow-public/.venv-whisperx/bin/python",
        ]
    return []


class Settings:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_config_path()
        self.lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(
                f"Cannot read local CueFlow configuration: {error}"
            ) from error
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("Unsupported local CueFlow configuration format.")
        values = payload.get("resources")
        if not isinstance(values, dict) or any(
            k not in FIELDS or not isinstance(v, str) for k, v in values.items()
        ):
            raise ValueError("Invalid local resource settings.")
        return values

    def save(self, values: object) -> dict:
        if not isinstance(values, dict) or any(k not in FIELDS for k in values):
            raise ValueError("Unknown or invalid resource settings.")
        clean = {}
        for key, value in values.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"{key} must be a path or an empty string for automatic detection."
                )
            if not value.strip():
                continue
            path = Path(value.strip()).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{key} must be an absolute path.")
            valid = (
                path.is_dir()
                if key in {"model_path", "large_model_path"}
                else path.is_file() and os.access(path, os.X_OK)
            )
            if not valid:
                raise ValueError(
                    f"{key}: choose an existing model directory or executable."
                )
            clean[key] = str(path.absolute())
        with self.lock:
            atomic_json(self.path, {"schema_version": 1, "resources": clean})
        return clean


def resolve_resource(field: str, explicit: str | Path | None = None) -> Path | None:
    if field not in FIELDS:
        raise ValueError(f"Unknown resource: {field}")
    saved = getattr(_context, "values", None)
    if saved is None:
        saved = Settings().load()
    value = explicit or os.environ.get(FIELDS[field]) or saved.get(field)
    if value:
        return Path(value).expanduser().absolute()
    choices = candidates(field)
    return next((p for p in choices if p.exists()), choices[-1] if choices else None)


@contextmanager
def resource_context(values: dict):
    previous = getattr(_context, "values", None)
    _context.values = values
    try:
        yield
    finally:
        _context.values = previous


def describe_settings(store: Settings, explicit: dict | None = None) -> dict:
    error = None
    try:
        saved = store.load()
    except ValueError as problem:
        saved, error = {}, str(problem)
    result = {}
    with resource_context(saved):
        for field, variable in FIELDS.items():
            value = (explicit or {}).get(field)
            path = resolve_resource(field, value)
            requested = path
            if field == "ffmpeg_path":
                from .ffmpeg_tools import find_ffmpeg
                from .bilingual import has_ass_filter

                path, _ = find_ffmpeg(value, accept=has_ass_filter)
            elif field == "whisperx_python":
                from .transcription import find_whisperx_python

                path = find_whisperx_python(value)
            source = (
                "command"
                if value
                else "environment"
                if os.environ.get(variable)
                else "saved"
                if saved.get(field)
                else "detected"
            )
            if (
                field == "ffmpeg_path"
                and requested
                and path
                and path.resolve() != requested.resolve()
            ):
                source = "fallback"
            ready = bool(
                path
                and (
                    path.is_dir()
                    if field in {"model_path", "large_model_path"}
                    else path.is_file() and os.access(path, os.X_OK)
                )
            )
            result[field] = {
                "path": str(path) if path else "",
                "ready": ready,
                "source": source,
                "candidates": [str(p) for p in candidates(field) if p.exists()],
            }
    return {"resources": saved, "effective": result, "error": error}
