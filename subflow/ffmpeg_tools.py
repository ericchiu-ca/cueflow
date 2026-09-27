"""Single place to locate a working FFmpeg.

Candidates, in order: an explicit path or SUBFLOW_FFMPEG, Homebrew's
ffmpeg-full (which ships libass), then `ffmpeg-full`/`ffmpeg` on PATH. The
system PATH is never modified. Probe results are cached per path and
modification time, so repeated extraction/retry calls do not respawn
`ffmpeg -version` for every candidate.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable

FFMPEG_FULL_PATH = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")


def ffmpeg_candidates(configured: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    configured_value = configured or os.environ.get("SUBFLOW_FFMPEG")
    if configured_value:
        candidates.append(Path(configured_value).expanduser())
    candidates.append(FFMPEG_FULL_PATH)
    for command in ("ffmpeg-full", "ffmpeg"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        absolute = candidate.absolute()
        if absolute not in seen:
            seen.add(absolute)
            unique.append(absolute)
    return unique


def _executable_signature(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path), stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=64)
def _run_probe(signature: tuple[str, int, int], args: tuple[str, ...]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [signature[0], *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}"


def is_runnable(path: Path, version_flag: str = "-version") -> bool:
    signature = _executable_signature(path)
    return signature is not None and _run_probe(signature, (version_flag,))[0] == 0


def has_ass_filter(path: Path) -> bool:
    signature = _executable_signature(path)
    if signature is None:
        return False
    code, output = _run_probe(signature, ("-hide_banner", "-filters"))
    return code == 0 and bool(re.search(r"\bass\s+V->V\b", output))


def find_ffmpeg(
    configured: str | Path | None = None,
    *,
    accept: Callable[[Path], bool] = is_runnable,
) -> tuple[Path | None, list[Path]]:
    """Return the first accepted candidate and every candidate that was tried."""
    candidates = ffmpeg_candidates(configured)
    for candidate in candidates:
        if accept(candidate):
            return candidate, candidates
    return None, candidates
