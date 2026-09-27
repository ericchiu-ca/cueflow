from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .core import SubtitleSegment
from .proc import run_captured

YT_DLP = "yt-dlp"
FFMPEG_FULL_PATH = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
DEFAULT_COMMAND_TIMEOUT = 14400


def _is_runnable(candidate: Path) -> bool:
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [str(candidate), "-version" if "ffmpeg" in candidate.name else "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ensure_command(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is None or not _is_runnable(Path(resolved)):
        raise RuntimeError(
            f"`{command}` is required but missing or not runnable.\n"
            f"Install or repair it first. Example: `brew install {command}`."
        )
    return resolved


def find_working_ffmpeg() -> str:
    candidates: list[Path] = []
    configured = os.environ.get("SUBFLOW_FFMPEG")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(FFMPEG_FULL_PATH)
    for command in ("ffmpeg-full", "ffmpeg"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.absolute())
        if key in seen:
            continue
        seen.add(key)
        if _is_runnable(candidate):
            return key
    raise RuntimeError(
        "A runnable FFmpeg is required for YouTube audio extraction. "
        "Install `ffmpeg-full` with `brew install ffmpeg-full`, or set "
        "SUBFLOW_FFMPEG to a working executable."
    )


def run_command(
    cmd: list[str],
    cwd: Path | None = None,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT,
) -> str:
    try:
        proc = run_captured(cmd, cwd=cwd, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Command timed out after {timeout_seconds} seconds: {cmd[0]}"
        ) from error
    except OSError as error:
        raise RuntimeError(f"Could not run external command `{cmd[0]}`: {error}") from error
    if proc.returncode != 0:
        stderr = proc.stderr.strip()[-3000:]
        stdout = proc.stdout.strip()[-3000:]
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
        )
    return proc.stdout


def detect_english_subtitles(url: str) -> tuple[str | None, str | None]:
    """
    Returns (manual_en_track, auto_en_track). Priority is manual then auto.
    """
    yt_dlp = ensure_command(YT_DLP)
    output = run_command(
        [yt_dlp, "--dump-single-json", "--skip-download", "--no-playlist", url],
        timeout_seconds=180,
    )
    try:
        info = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("yt-dlp returned invalid video metadata JSON.") from error
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned unexpected video metadata.")
    return pick_english_tracks(
        list(info.get("subtitles") or {}),
        list(info.get("automatic_captions") or {}),
    )


def pick_english_tracks(manual: list[str], auto: list[str]) -> tuple[str | None, str | None]:
    manual_en = next((lang for lang in manual if lang.lower().startswith("en")), None)
    # YouTube lists machine translations of the original ASR track as ordinary
    # languages; only "<lang>-orig" is the speech itself. When the original is
    # not English, an automatic "en" track is a translation, not a transcript.
    originals = [lang for lang in auto if lang.lower().endswith("-orig")]
    candidates = originals if originals else auto
    auto_en = next((lang for lang in candidates if lang.lower().startswith("en")), None)
    return manual_en, auto_en


def _pick_subtitle_file(out_dir: Path, language: str) -> Path:
    candidates = sorted(p for p in out_dir.glob("*.srt"))
    if not candidates:
        raise RuntimeError(f"No subtitle file downloaded in {out_dir}")

    # Prefer exact match for language variant, then fallback to English-like files.
    for path in candidates:
        if f".{language}." in path.name.lower():
            return path
    for path in candidates:
        if ".en." in path.name.lower():
            return path
    return candidates[0]


def download_subtitles(url: str, out_dir: Path, language: str, use_auto: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    yt_dlp = ensure_command(YT_DLP)
    mode = "--write-auto-subs" if use_auto else "--write-subs"
    cmd = [
        yt_dlp,
        "--skip-download",
        mode,
        "--sub-langs",
        language,
        "--sub-format",
        "srt",
        "--convert-subs",
        "srt",
        "--output",
        str(out_dir / "%(id)s.%(ext)s"),
        "--no-playlist",
        url,
    ]
    run_command(cmd)
    return _pick_subtitle_file(out_dir, language)


def download_audio_m4a(url: str, output_path: Path) -> None:
    yt_dlp = ensure_command(YT_DLP)
    ffmpeg = find_working_ffmpeg()
    cmd = [
        yt_dlp,
        "--ffmpeg-location",
        str(Path(ffmpeg).parent),
        "-x",
        "--audio-format",
        "m4a",
        "--audio-quality",
        "0",
        "--output",
        str(output_path),
        "--no-playlist",
        url,
    ]
    run_command(cmd)


def transcribe_with_faster_whisper(audio_path: Path, model_name: str = "base") -> list[SubtitleSegment]:
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError(
            "faster-whisper is not installed, but required for audio transcription fallback.\n"
            "Install with: `pip install faster-whisper`."
        ) from exc

    model = WhisperModel(model_name, device="auto", compute_type="auto")
    transcribed = []

    result_segments, _ = model.transcribe(
        str(audio_path),
        language="en",
        beam_size=5,
        vad_filter=True,
    )
    for seg in result_segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        transcribed.append(
            SubtitleSegment(
                id="",
                start=round(seg.start, 3),
                end=round(seg.end, 3),
                text=text,
                words=[],
            )
        )
    return transcribed
