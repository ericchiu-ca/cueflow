from __future__ import annotations

import contextlib
import json
import math
import sys
from pathlib import Path
from typing import Any


PROTOCOL = "cueflow-mlx-batch-v1"
SUPPORTED_LANGUAGES = {"en", "fr", "auto"}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        return _json_value(value.item())
    if hasattr(value, "tolist"):
        return _json_value(value.tolist())
    return str(value)


def _transcribe(mlx_whisper, model_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    audio_path = Path(str(item.get("audio_path", ""))).expanduser().resolve()
    if not audio_path.is_file():
        raise RuntimeError(f"Audio clip does not exist: {audio_path}")
    language = str(item.get("language", "auto"))
    if language not in SUPPORTED_LANGUAGES:
        raise RuntimeError(f"Unsupported language: {language}")

    kwargs: dict[str, Any] = {
        "path_or_hf_repo": str(model_path),
        "language": None if language == "auto" else language,
        "word_timestamps": True,
        "condition_on_previous_text": False,
        "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "no_speech_threshold": 0.6,
        "fp16": True,
        "verbose": None,
    }
    prompt = str(item.get("initial_prompt", "")).strip()
    if prompt:
        kwargs["initial_prompt"] = prompt
    with contextlib.redirect_stdout(sys.stderr):
        result = mlx_whisper.transcribe(str(audio_path), **kwargs)
    return _json_value(result)


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if request.get("protocol") != PROTOCOL:
            raise RuntimeError("Unsupported MLX batch protocol")
        model_path = Path(str(request.get("model_path", ""))).expanduser().resolve()
        if not model_path.is_dir():
            raise RuntimeError(f"MLX model directory does not exist: {model_path}")
        items = request.get("items")
        if not isinstance(items, list) or not items:
            raise RuntimeError("MLX batch requires at least one item")
        if len(items) > 2048:
            raise RuntimeError("MLX batch is limited to 2048 items")

        with contextlib.redirect_stdout(sys.stderr):
            import mlx_whisper

        output: list[dict[str, Any]] = []
        for item in items:
            item_id = str(item.get("id", ""))
            try:
                output.append(
                    {"id": item_id, "result": _transcribe(mlx_whisper, model_path, item)}
                )
            except Exception as exc:
                output.append(
                    {"id": item_id, "error_type": type(exc).__name__, "error": str(exc)}
                )
        json.dump({"protocol": PROTOCOL, "items": output}, sys.stdout, ensure_ascii=False)
        return 0
    except Exception as exc:
        json.dump(
            {"protocol": PROTOCOL, "error_type": type(exc).__name__, "error": str(exc)},
            sys.stderr,
            ensure_ascii=False,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
