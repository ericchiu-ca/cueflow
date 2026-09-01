#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
import sys
import traceback
from pathlib import Path


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        audio_path = Path(str(request["audio_path"])).resolve()
        cache_dir = Path(str(request["cache_dir"])).resolve()
        language = str(request["language"])
        segments = request["segments"]
        if not audio_path.is_file() or audio_path.suffix.lower() != ".wav":
            raise ValueError("audio_path must be an existing WAV file")
        if language not in {"en", "fr"}:
            raise ValueError("language must be en or fr")
        if not isinstance(segments, list):
            raise ValueError("segments must be a list")
        cache_dir.mkdir(parents=True, exist_ok=True)

        with contextlib.redirect_stdout(sys.stderr):
            import whisperx

            audio = whisperx.load_audio(str(audio_path))
            model, metadata = whisperx.load_align_model(
                language_code=language,
                device="cpu",
                model_dir=str(cache_dir),
            )
            result = whisperx.align(
                segments,
                model,
                metadata,
                audio,
                "cpu",
                return_char_alignments=False,
            )
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
