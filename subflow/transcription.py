from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .core import (
    SubtitleSegment,
    build_srt_text,
    load_master_json,
    normalize_segments,
    save_master_json,
)


FRAMELEDGER_ROOT = Path.home() / "Documents" / "FrameLedger"
DEFAULT_MLX_MODEL = (
    FRAMELEDGER_ROOT / ".cache" / "models" / "whisper-large-v3-turbo"
)
DEFAULT_MLX_LARGE_MODEL = (
    FRAMELEDGER_ROOT / ".cache" / "models" / "whisper-large-v3-mlx"
)
DEFAULT_MLX_HELPER = FRAMELEDGER_ROOT / "phase2" / "mlx_whisper_asr" / "run"
LANGUAGE_CODES = {"en": "en", "fr-CA": "fr", "mixed": "mixed"}
ALIGNMENT_MODES = {"auto", "native", "whisperx"}
SUPPORTED_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".avi",
    ".flac",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogg",
    ".wav",
    ".webm",
}
ProgressCallback = Callable[[str, int, str], None]
MAX_COMPRESSION_RATIO = 2.4
QUALITY_RETRY_PADDING_SECONDS = 0.25
MAX_AUTOMATIC_QUALITY_RETRIES = 12
QUOTED_FILE_URI_RE = re.compile(
    r'''(["'])file:/+[^"'\r\n]*\1''', re.IGNORECASE
)
FILE_URI_RE = re.compile(
    r'''(?<![\w/.-])file:/+[^"'\r\n<>]+''', re.IGNORECASE
)
QUOTED_ABSOLUTE_PATH_RE = re.compile(r'''(["'])/(?!/)[^"'\r\n]*\1''')
ABSOLUTE_PATH_RE = re.compile(r'''(?<![\w/.-])/(?!/)[^"'\r\n<>]+''')


class TranscriptionError(RuntimeError):
    pass


def _portable_model_reference(value: str | Path) -> str:
    """Keep model provenance without persisting a machine-specific path."""
    return Path(value).name


def _portable_metadata(value):
    """Remove absolute local paths from metadata that users may share."""
    if isinstance(value, dict):
        return {
            (
                _portable_metadata(key) if isinstance(key, str) else key
            ): _portable_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_portable_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_portable_metadata(item) for item in value]
    if isinstance(value, str):
        redacted = QUOTED_FILE_URI_RE.sub(
            lambda match: f"{match.group(1)}<local-path>{match.group(1)}",
            value,
        )
        redacted = FILE_URI_RE.sub("<local-path>", redacted)
        redacted = QUOTED_ABSOLUTE_PATH_RE.sub(
            lambda match: f"{match.group(1)}<local-path>{match.group(1)}",
            redacted,
        )
        return ABSOLUTE_PATH_RE.sub("<local-path>", redacted)
    return value


@dataclass
class TranscriptionResult:
    project: Path
    srt_path: Path
    master_path: Path
    segments: list[SubtitleSegment]
    alignment: str
    warnings: list[str]
    quality_issues: list[dict] = field(default_factory=list)
    confidence_windows: list[dict] = field(default_factory=list)
    confidence_summary: dict = field(default_factory=dict)


@dataclass
class MlxTranscription:
    segments: list[SubtitleSegment]
    quality_issues: list[dict]
    confidence_windows: list[dict] = field(default_factory=list)
    confidence_summary: dict = field(default_factory=dict)
    pipeline: str = "full-file"
    vad_window_count: int = 0
    large_v3_window_count: int = 0
    large_v3_selected_count: int = 0
    warnings: list[str] = field(default_factory=list)


def resolve_large_model_path(value: str | Path | None = None) -> Path:
    configured = value or os.environ.get("SUBFLOW_MLX_LARGE_MODEL") or DEFAULT_MLX_LARGE_MODEL
    return Path(configured).expanduser().resolve()


def resolve_mlx_paths(
    model_path: str | Path | None = None,
    helper_path: str | Path | None = None,
) -> tuple[Path, Path]:
    model = Path(
        model_path or os.environ.get("SUBFLOW_MLX_MODEL") or DEFAULT_MLX_MODEL
    ).expanduser().resolve()
    helper = Path(
        helper_path or os.environ.get("SUBFLOW_MLX_HELPER") or DEFAULT_MLX_HELPER
    ).expanduser().resolve()
    return model, helper


def resolve_language(value: str) -> str:
    try:
        return LANGUAGE_CODES[value]
    except KeyError as error:
        choices = ", ".join(LANGUAGE_CODES)
        raise TranscriptionError(f"Unsupported language: {value}. Choose: {choices}") from error


def validate_media_path(path: Path) -> Path:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise TranscriptionError(f"Media file not found: {source}")
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise TranscriptionError(
            f"Unsupported media type: {source.suffix or '(none)'}"
        )
    return source


def safe_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    if not name:
        raise TranscriptionError("Uploaded file has no filename")
    cleaned = "".join(
        character if character.isalnum() or character in "._- " else "_"
        for character in name
    ).strip(" .")
    if not cleaned:
        raise TranscriptionError("Uploaded filename is invalid")
    return cleaned[:180]


FFMPEG_FULL_PATH = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")


def _is_runnable_ffmpeg(candidate: Path) -> bool:
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False
    try:
        completed = subprocess.run(
            [str(candidate), "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _ensure_ffmpeg() -> str:
    candidates: list[Path] = []
    configured = os.environ.get("SUBFLOW_FFMPEG")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(FFMPEG_FULL_PATH)
    for command in ("ffmpeg-full", "ffmpeg"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.absolute())
        if key in seen:
            continue
        seen.add(key)
        if _is_runnable_ffmpeg(candidate):
            return key

    tried = ", ".join(seen) or "no candidates found"
    raise TranscriptionError(
        "A runnable FFmpeg is required for audio extraction. "
        f"CueFlow tried: {tried}. Install `ffmpeg-full` with "
        "`brew install ffmpeg-full`, or repair a broken Homebrew FFmpeg with "
        "`brew reinstall ffmpeg`."
    )


def _ffmpeg_subprocess_env() -> dict[str, str]:
    ffmpeg = Path(_ensure_ffmpeg())
    environment = os.environ.copy()
    current_path = environment.get("PATH", "")
    environment["PATH"] = str(ffmpeg.parent) + (os.pathsep + current_path if current_path else "")
    environment["FFMPEG_BINARY"] = str(ffmpeg)
    environment["IMAGEIO_FFMPEG_EXE"] = str(ffmpeg)
    return environment


def _run_ffmpeg(source: Path, wav_path: Path) -> None:
    completed = subprocess.run(
        [
            _ensure_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise TranscriptionError(f"ffmpeg could not extract audio: {detail}")


def _run_ffmpeg_clip(
    source: Path,
    wav_path: Path,
    *,
    start: float,
    end: float,
) -> None:
    completed = subprocess.run(
        [
            _ensure_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(source),
            "-t",
            f"{max(0.1, end - start):.3f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise TranscriptionError(f"ffmpeg could not extract retry clip: {detail}")


def _normalize_words(raw_words: object) -> list[dict]:
    if not isinstance(raw_words, list):
        return []
    words: list[dict] = []
    for item in raw_words:
        if not isinstance(item, Mapping):
            continue
        word = str(item.get("word", "")).strip()
        if not word:
            continue
        normalized: dict = {"word": word}
        for key in ("start", "end", "score", "probability"):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                normalized[key] = round(float(value), 3)
        words.append(normalized)
    return words


def _clock_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _quality_issues_from_result(result: Mapping[str, object]) -> list[dict]:
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        return []
    issues: list[dict] = []
    compression_groups: dict[str, dict] = {}
    previous_text = ""
    repeated_run: list[dict] = []

    def append_repeated_issue(run: list[dict]) -> None:
        if len(run) < 3:
            return
        timed = [
            item
            for item in run
            if isinstance(item.get("start"), (int, float))
            and isinstance(item.get("end"), (int, float))
            and float(item["end"]) > float(item["start"])
        ]
        if not timed:
            return
        raw_indices = [int(item["raw_index"]) for item in run]
        issues.append(
            {
                "raw_index": raw_indices[0],
                "raw_indices": raw_indices,
                "window_key": f"repeat:{raw_indices[0]}-{raw_indices[-1]}",
                "start": min(float(item["start"]) for item in timed),
                "end": max(float(item["end"]) for item in timed),
                "text": str(run[0].get("text", ""))[:500],
                "severity": "WARN",
                "code": "ASR_REPEATED_SEGMENTS",
                "message": f"Whisper 连续 {len(run)} 条字幕文本重复",
            }
        )

    for raw_index, item in enumerate(raw_segments):
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text", "")).strip()
        start_value = item.get("start")
        end_value = item.get("end")
        start = (
            round(float(start_value), 3)
            if isinstance(start_value, (int, float)) and not isinstance(start_value, bool)
            else None
        )
        end = (
            round(float(end_value), 3)
            if isinstance(end_value, (int, float)) and not isinstance(end_value, bool)
            else None
        )
        base = {
            "raw_index": raw_index,
            "start": start,
            "end": end,
            "text": text,
            "severity": "WARN",
        }
        compression_ratio = item.get("compression_ratio")
        if (
            isinstance(compression_ratio, (int, float))
            and not isinstance(compression_ratio, bool)
            and float(compression_ratio) > MAX_COMPRESSION_RATIO
            and start is not None
            and end is not None
            and end > start
        ):
            ratio = round(float(compression_ratio), 3)
            seek = item.get("seek")
            if isinstance(seek, (int, float)) and not isinstance(seek, bool):
                window_key = f"seek:{seek}"
            else:
                window_key = f"time:{int(start // 30)}"
            group = compression_groups.setdefault(
                window_key,
                {"ratio": ratio, "segments": []},
            )
            group["ratio"] = max(float(group["ratio"]), ratio)
            group["segments"].append(base)
        if "\ufffd" in text and start is not None and end is not None and end > start:
            issues.append(
                {
                    **base,
                    "window_key": f"unicode:{raw_index}",
                    "code": "ASR_INVALID_UNICODE",
                    "message": "Whisper 输出包含无效 Unicode 字符",
                }
            )
        comparable = " ".join(text.casefold().split())
        if comparable and comparable == previous_text:
            repeated_run.append(base)
        else:
            append_repeated_issue(repeated_run)
            previous_text = comparable
            repeated_run = [base] if comparable else []
    append_repeated_issue(repeated_run)

    for window_key, group in compression_groups.items():
        entries = group["segments"]
        raw_indices = [int(item["raw_index"]) for item in entries]
        ratio = float(group["ratio"])
        issues.append(
            {
                "raw_index": raw_indices[0],
                "raw_indices": raw_indices,
                "window_key": window_key,
                "start": min(float(item["start"]) for item in entries),
                "end": max(float(item["end"]) for item in entries),
                "text": " ".join(str(item.get("text", "")) for item in entries)[:500],
                "severity": "WARN",
                "code": "ASR_COMPRESSION_RATIO",
                "compression_ratio": round(ratio, 3),
                "message": (
                    f"Whisper 窗口压缩率 {ratio:.2f}，影响 {len(entries)} 条子字幕，"
                    "疑似重复或幻觉"
                ),
            }
        )
    issues.sort(
        key=lambda issue: (
            float(issue["start"])
            if isinstance(issue.get("start"), (int, float))
            else float("inf"),
            str(issue.get("code", "")),
        )
    )
    return issues


def _raise_first_quality_issue(issues: list[dict]) -> None:
    if not issues:
        return
    issue = issues[0]
    start = issue.get("start")
    end = issue.get("end")
    location = ""
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        location = f" at {_clock_time(float(start))}-{_clock_time(float(end))}"
    code = issue.get("code")
    if code == "ASR_COMPRESSION_RATIO":
        raise TranscriptionError(
            "Whisper quality gate rejected compression ratio "
            f"{float(issue['compression_ratio']):.2f}{location}"
        )
    if code == "ASR_INVALID_UNICODE":
        raise TranscriptionError(
            f"Whisper quality gate rejected invalid Unicode output{location}"
        )
    raise TranscriptionError(
        f"Whisper quality gate rejected three consecutive repeated segments{location}"
    )


def segments_from_result(
    result: Mapping[str, object],
    *,
    enforce_transcript_quality: bool = True,
) -> list[SubtitleSegment]:
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        raise TranscriptionError("Whisper result does not contain a segment list")
    if enforce_transcript_quality:
        _raise_first_quality_issue(_quality_issues_from_result(result))
    segments: list[SubtitleSegment] = []
    for item in raw_segments:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text", "")).strip()
        start = item.get("start")
        end = item.get("end")
        if (
            not text
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or float(end) <= float(start)
        ):
            continue
        segments.append(
            SubtitleSegment(
                id="",
                start=float(start),
                end=float(end),
                text=text,
                words=_normalize_words(item.get("words")),
            )
        )
    if not segments:
        raise TranscriptionError("Whisper returned no usable speech segments")
    return normalize_segments(segments)


def transcribe_with_mlx(
    wav_path: Path,
    *,
    language: str,
    model_path: Path,
    helper_path: Path,
    whisperx_python: str | Path | None = None,
    decoding_profile: str = "standard_fallback_v1",
    enforce_transcript_quality: bool = True,
    timeout_seconds: float = 14400.0,
) -> MlxTranscription:
    if not model_path.is_dir():
        raise TranscriptionError(
            f"FrameLedger MLX Whisper model was not found: {model_path}"
        )
    if not helper_path.is_file() or not os.access(helper_path, os.X_OK):
        raise TranscriptionError(
            f"FrameLedger MLX Whisper helper is unavailable: {helper_path}"
        )
    large_model = resolve_large_model_path()
    configured_whisperx = find_whisperx_python(whisperx_python)
    if decoding_profile == "standard_fallback_v1" and large_model.is_dir() and configured_whisperx:
        try:
            from .advanced_asr import transcribe_vad_cascade

            advanced = transcribe_vad_cascade(
                wav_path=wav_path,
                project_dir=wav_path.parent,
                language=language,
                turbo_model=model_path,
                large_model=large_model,
                helper_path=helper_path,
                whisperx_python=configured_whisperx,
                parse_result=segments_from_result,
                quality_analyzer=_quality_issues_from_result,
                environment_factory=_ffmpeg_subprocess_env,
            )
            return MlxTranscription(
                segments=advanced.segments,
                quality_issues=advanced.quality_issues,
                confidence_windows=advanced.confidence_windows,
                confidence_summary=advanced.confidence_summary,
                pipeline="vad-turbo-large-v3",
                vad_window_count=advanced.vad_window_count,
                large_v3_window_count=advanced.large_v3_window_count,
                large_v3_selected_count=advanced.large_v3_selected_count,
            )
        except Exception as error:
            if language == "mixed":
                raise TranscriptionError(f"Mixed-language VAD cascade failed: {error}") from error
            cascade_warning = (
                "VAD/large-v3 cascade failed; used full-file Turbo fallback"
            )
    else:
        missing: list[str] = []
        if not large_model.is_dir():
            missing.append("large-v3 model")
        if configured_whisperx is None:
            missing.append("WhisperX Python")
        if language == "mixed":
            raise TranscriptionError(
                "Mixed-language transcription requires " + " and ".join(missing)
            )
        cascade_warning = (
            "VAD/large-v3 cascade unavailable; used full-file Turbo fallback: "
            + ", ".join(missing)
        )
    request = {
        "protocol": "frameledger-asr-helper-v1",
        "audio_path": str(wav_path.resolve()),
        "model_path": str(model_path),
        "language": language,
        "task": "transcribe",
        "word_timestamps": True,
        "decoding_profile": decoding_profile,
    }
    try:
        completed = subprocess.run(
            [str(helper_path)],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=False,
            env=_ffmpeg_subprocess_env(),
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TranscriptionError(f"MLX Whisper helper could not run: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise TranscriptionError(
            f"MLX Whisper helper exited with {completed.returncode}: {detail}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise TranscriptionError("MLX Whisper helper returned invalid JSON") from error
    if not isinstance(response, dict) or response.get("protocol") != "frameledger-asr-helper-v1":
        raise TranscriptionError("MLX Whisper helper protocol mismatch")
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise TranscriptionError("MLX Whisper helper returned no result")
    quality_issues = _quality_issues_from_result(result)
    return MlxTranscription(
        segments=segments_from_result(
            result,
            enforce_transcript_quality=enforce_transcript_quality,
        ),
        quality_issues=quality_issues,
        warnings=[cascade_warning],
    )


def _replacements_from_retry(
    retry: MlxTranscription,
    *,
    clip_start: float,
    target_start: float,
    target_end: float,
) -> list[SubtitleSegment]:
    replacements: list[SubtitleSegment] = []
    for segment in retry.segments:
        shifted_start = segment.start + clip_start
        shifted_end = segment.end + clip_start
        if shifted_end <= target_start or shifted_start >= target_end:
            continue
        replacement_start = max(target_start, shifted_start)
        replacement_end = min(target_end, shifted_end)
        text = segment.text.strip()
        if not text or replacement_end <= replacement_start:
            continue
        words: list[dict] = []
        for word in segment.words:
            shifted = dict(word)
            for key in ("start", "end"):
                value = shifted.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    shifted[key] = round(float(value) + clip_start, 3)
            word_start = shifted.get("start")
            word_end = shifted.get("end")
            if (
                isinstance(word_start, (int, float))
                and isinstance(word_end, (int, float))
                and float(word_end) > replacement_start
                and float(word_start) < replacement_end
            ):
                words.append(shifted)
        replacements.append(
            SubtitleSegment(
                id="",
                start=replacement_start,
                end=replacement_end,
                text=text,
                words=words,
            )
        )
    return replacements


def _replace_segment_window(
    segments: list[SubtitleSegment],
    *,
    start: float,
    end: float,
    replacements: list[SubtitleSegment],
) -> list[SubtitleSegment]:
    updated: list[SubtitleSegment] = []
    inserted = False
    for segment in segments:
        overlaps = segment.end > start and segment.start < end
        if overlaps:
            if not inserted:
                updated.extend(replacements)
                inserted = True
            continue
        updated.append(segment)
    return normalize_segments(updated) if inserted else segments


def _retry_quality_issues(
    wav_path: Path,
    segments: list[SubtitleSegment],
    issues: list[dict],
    *,
    language: str,
    model_path: Path,
    helper_path: Path,
    progress: ProgressCallback | None = None,
) -> tuple[list[SubtitleSegment], list[dict], int]:
    ordered = sorted(
        issues,
        key=lambda issue: (
            float(issue["start"])
            if isinstance(issue.get("start"), (int, float))
            else float("inf")
        ),
    )
    grouped: list[dict] = []
    for issue in ordered:
        start = issue.get("start")
        end = issue.get("end")
        valid = (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and float(end) > float(start)
        )
        if (
            valid
            and grouped
            and grouped[-1]["valid"]
            and float(start) <= float(grouped[-1]["end"]) + 0.05
        ):
            grouped[-1]["issues"].append(issue)
            grouped[-1]["end"] = max(float(grouped[-1]["end"]), float(end))
        else:
            grouped.append(
                {
                    "issues": [issue],
                    "start": float(start) if valid else None,
                    "end": float(end) if valid else None,
                    "valid": valid,
                }
            )
    groups = grouped
    current = segments
    unresolved: list[dict] = []
    recovered = 0
    for index, grouped_window in enumerate(groups):
        group = grouped_window["issues"]
        start = grouped_window["start"]
        end = grouped_window["end"]
        if index >= MAX_AUTOMATIC_QUALITY_RETRIES:
            for issue in group:
                issue["retry_error"] = "automatic retry limit reached"
            unresolved.extend(group)
            continue
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or float(end) <= float(start)
        ):
            for issue in group:
                issue["retry_error"] = "invalid retry time range"
            unresolved.extend(group)
            continue
        target_start = float(start)
        target_end = float(end)
        clip_start = max(0.0, target_start - QUALITY_RETRY_PADDING_SECONDS)
        clip_end = target_end + QUALITY_RETRY_PADDING_SECONDS
        if progress:
            percent = 45 + round(22 * (index + 1) / max(1, min(len(groups), MAX_AUTOMATIC_QUALITY_RETRIES)))
            progress(
                "quality-retry",
                percent,
                f"检测到可疑片段，正在局部重试 {index + 1}/{min(len(groups), MAX_AUTOMATIC_QUALITY_RETRIES)}",
            )
        clip_path = wav_path.with_name(f".subflow-quality-retry-{index + 1:03d}.wav")
        try:
            _run_ffmpeg_clip(
                wav_path,
                clip_path,
                start=clip_start,
                end=clip_end,
            )
            retry = transcribe_with_mlx(
                clip_path,
                language=language,
                model_path=model_path,
                helper_path=helper_path,
                decoding_profile="fixed_zero_v1",
                enforce_transcript_quality=False,
            )
            replacements = _replacements_from_retry(
                retry,
                clip_start=clip_start,
                target_start=target_start,
                target_end=target_end,
            )
            if retry.quality_issues or not replacements:
                for issue in group:
                    issue["retry_error"] = (
                        "isolated retry still failed the quality gate"
                        if retry.quality_issues
                        else "isolated retry returned no replacement text"
                    )
                unresolved.extend(group)
                continue
            current = _replace_segment_window(
                current,
                start=target_start,
                end=target_end,
                replacements=replacements,
            )
            recovered += 1
        except TranscriptionError:
            for issue in group:
                issue["retry_error"] = "isolated retry failed"
            unresolved.extend(group)
        finally:
            clip_path.unlink(missing_ok=True)
    return current, unresolved, recovered


def _attach_quality_issue_ids(
    issues: list[dict],
    segments: list[SubtitleSegment],
) -> list[dict]:
    attached: list[dict] = []
    for issue in issues:
        value = dict(issue)
        start = value.get("start")
        end = value.get("end")
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and float(end) > float(start)
        ):
            matching = [
                segment
                for segment in segments
                if min(segment.end, float(end)) - max(segment.start, float(start)) > 0
            ]
            if matching:
                value["segment_ids"] = [segment.id for segment in matching]
                value["id"] = matching[0].id
        attached.append(value)
    return attached


def find_whisperx_python(value: str | Path | None = None) -> Path | None:
    configured = value or os.environ.get("SUBFLOW_WHISPERX_PYTHON")
    if configured:
        path = Path(configured).expanduser().absolute()
        return path if path.is_file() else None
    local = Path(__file__).resolve().parents[1] / ".venv-whisperx" / "bin" / "python"
    if local.is_file():
        return local
    if importlib.util.find_spec("whisperx") is not None:
        return Path(sys.executable).resolve()
    return None


def whisperx_version(python_path: Path | None) -> str | None:
    if python_path is None:
        return None
    try:
        completed = subprocess.run(
            [
                str(python_path),
                "-c",
                "import importlib.metadata, whisperx; print(importlib.metadata.version('whisperx'))",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    version = completed.stdout.strip()
    return version or None


def _run_whisperx_alignment(
    wav_path: Path,
    segments: list[SubtitleSegment],
    *,
    language: str,
    python_path: Path,
    cache_dir: Path,
    timeout_seconds: float = 14400.0,
) -> list[SubtitleSegment]:
    project_root = Path(__file__).resolve().parents[1]
    request = {
        "audio_path": str(wav_path.resolve()),
        "language": language,
        "cache_dir": str(cache_dir.resolve()),
        "segments": [
            {"start": item.start, "end": item.end, "text": item.text}
            for item in segments
        ],
    }
    try:
        completed = subprocess.run(
            [str(python_path), "-m", "subflow.whisperx_runner"],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=False,
            cwd=str(project_root),
            env=_ffmpeg_subprocess_env(),
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TranscriptionError(f"WhisperX alignment could not run: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-3000:]
        raise TranscriptionError(f"WhisperX alignment failed: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise TranscriptionError("WhisperX alignment returned invalid JSON") from error
    if not isinstance(result, Mapping):
        raise TranscriptionError("WhisperX alignment returned an invalid result")
    aligned = segments_from_result(result, enforce_transcript_quality=False)
    return _preserve_aligned_text(aligned, segments)


def _preserve_aligned_text(
    aligned: list[SubtitleSegment], originals: list[SubtitleSegment]
) -> list[SubtitleSegment]:
    if len(aligned) != len(originals):
        return aligned
    return normalize_segments(
        SubtitleSegment(
            id="",
            start=aligned_segment.start,
            end=aligned_segment.end,
            text=original_segment.text,
            words=aligned_segment.words,
        )
        for aligned_segment, original_segment in zip(aligned, originals)
    )


def _attach_confidence_window_ids(
    windows: list[dict], segments: list[SubtitleSegment]
) -> list[dict]:
    attached: list[dict] = []
    for source in windows:
        window = dict(source)
        start = window.get("start")
        end = window.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            window["segment_ids"] = [
                segment.id
                for segment in segments
                if min(float(end), segment.end) > max(float(start), segment.start)
            ]
        attached.append(window)
    return attached


def _run_mixed_whisperx_alignment(
    wav_path: Path,
    segments: list[SubtitleSegment],
    confidence_windows: list[dict],
    *,
    python_path: Path,
    cache_dir: Path,
) -> list[SubtitleSegment]:
    language_by_id: dict[str, str] = {}
    for window in confidence_windows:
        detected = str(window.get("language", ""))
        if detected not in {"en", "fr"}:
            continue
        for segment_id in window.get("segment_ids", []):
            language_by_id[str(segment_id)] = detected

    aligned: list[SubtitleSegment] = []
    assigned: set[str] = set()
    for detected in ("en", "fr"):
        subset = [item for item in segments if language_by_id.get(item.id) == detected]
        if subset:
            aligned.extend(
                _run_whisperx_alignment(
                    wav_path,
                    subset,
                    language=detected,
                    python_path=python_path,
                    cache_dir=cache_dir,
                )
            )
            assigned.update(item.id for item in subset)
    aligned.extend(item for item in segments if item.id not in assigned)
    return normalize_segments(sorted(aligned, key=lambda item: (item.start, item.end)))


def environment_status(
    *,
    model_path: str | Path | None = None,
    helper_path: str | Path | None = None,
    whisperx_python: str | Path | None = None,
) -> dict:
    model, helper = resolve_mlx_paths(model_path, helper_path)
    whisperx = find_whisperx_python(whisperx_python)
    installed_whisperx = whisperx_version(whisperx)
    try:
        ffmpeg = _ensure_ffmpeg()
    except TranscriptionError:
        ffmpeg = None
    return {
        "ffmpeg": ffmpeg is not None,
        "ffmpeg_path": ffmpeg,
        "mlx_model": str(model),
        "mlx_model_available": model.is_dir(),
        "mlx_large_model": str(resolve_large_model_path()),
        "mlx_large_model_available": resolve_large_model_path().is_dir(),
        "mlx_helper": str(helper),
        "mlx_helper_available": helper.is_file() and os.access(helper, os.X_OK),
        "whisperx_python": str(whisperx) if whisperx else None,
        "whisperx_configured": installed_whisperx is not None,
        "whisperx_version": installed_whisperx,
    }


def transcribe_media(
    source_path: str | Path,
    project_path: str | Path,
    *,
    language: str,
    alignment_mode: str = "auto",
    model_path: str | Path | None = None,
    helper_path: str | Path | None = None,
    whisperx_python: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> TranscriptionResult:
    source = validate_media_path(Path(source_path))
    whisper_language = resolve_language(language)
    if alignment_mode not in ALIGNMENT_MODES:
        raise TranscriptionError(f"Unsupported alignment mode: {alignment_mode}")
    model, helper = resolve_mlx_paths(model_path, helper_path)
    project = Path(project_path).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    output_dir = project / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = project / ".subflow-audio.wav"
    warnings: list[str] = []

    def update(stage: str, percent: int, message: str) -> None:
        if progress:
            progress(stage, percent, message)

    try:
        update("audio", 15, "正在提取 16 kHz 单声道音频")
        _run_ffmpeg(source, wav_path)
        update("transcribing", 32, "正在使用 FrameLedger MLX Whisper 转写")
        mlx_result = transcribe_with_mlx(
            wav_path,
            language=whisper_language,
            model_path=model,
            helper_path=helper,
            whisperx_python=whisperx_python,
            enforce_transcript_quality=False,
        )
        segments = mlx_result.segments
        quality_issues = mlx_result.quality_issues
        warnings.extend(mlx_result.warnings)
        recovered_quality_issues = 0
        if quality_issues and mlx_result.pipeline == "full-file":
            segments, quality_issues, recovered_quality_issues = _retry_quality_issues(
                wav_path,
                segments,
                quality_issues,
                language=whisper_language,
                model_path=model,
                helper_path=helper,
                progress=progress,
            )
        alignment = "mlx-word-timestamps"

        if alignment_mode != "native":
            python_path = find_whisperx_python(whisperx_python)
            if python_path is None:
                message = (
                    "WhisperX 环境未配置，已使用 MLX Whisper 词级时间戳。"
                )
                if alignment_mode == "whisperx":
                    raise TranscriptionError(
                        message
                        + " 请先创建 `.venv-whisperx`，或设置 SUBFLOW_WHISPERX_PYTHON。"
                    )
                warnings.append(message)
            else:
                update("aligning", 76, "正在使用 WhisperX 做词级强制对齐")
                try:
                    cache_dir = Path(__file__).resolve().parents[1] / ".cache" / "whisperx"
                    if whisper_language == "mixed":
                        segments = _run_mixed_whisperx_alignment(
                            wav_path,
                            segments,
                            mlx_result.confidence_windows,
                            python_path=python_path,
                            cache_dir=cache_dir,
                        )
                        alignment = "whisperx-mixed"
                    else:
                        segments = _run_whisperx_alignment(
                            wav_path,
                            segments,
                            language=whisper_language,
                            python_path=python_path,
                            cache_dir=cache_dir,
                        )
                        alignment = "whisperx"
                except TranscriptionError:
                    if alignment_mode == "whisperx":
                        raise
                    warnings.append(
                        "WhisperX 对齐失败，已回退到 MLX 词级时间戳"
                    )

        mlx_result.confidence_windows = _attach_confidence_window_ids(
            mlx_result.confidence_windows, segments
        )
        quality_issues = _attach_quality_issue_ids(quality_issues, segments)
        for issue in quality_issues:
            start = issue.get("start")
            end = issue.get("end")
            location = ""
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                location = f" {_clock_time(float(start))}-{_clock_time(float(end))}"
            identifier = f" [{issue['id']}]" if issue.get("id") else ""
            warnings.append(
                f"ASR 质量提醒{identifier}{location}: {issue.get('message', '需要人工审校')}"
            )

        update("writing", 92, "正在生成 master.json 与 SRT")
        master_path = project / "master.json"
        srt_path = output_dir / f"{source.stem}.srt"
        metadata = _portable_metadata(
            {
                "source": source.name,
                "language": language,
                "whisper_language": whisper_language,
                "engine": "mlx-whisper",
                "model_path": _portable_model_reference(model),
                "alignment": alignment,
                "warnings": warnings,
                "review_required": bool(quality_issues),
                "quality_issues": quality_issues,
                "quality_retries_recovered": recovered_quality_issues,
                "asr_pipeline": mlx_result.pipeline,
                "vad_windows": mlx_result.vad_window_count,
                "large_v3_windows": mlx_result.large_v3_window_count,
                "large_v3_selected": mlx_result.large_v3_selected_count,
                "large_v3_model_path": _portable_model_reference(
                    resolve_large_model_path()
                ),
                "confidence_summary": mlx_result.confidence_summary,
                "confidence_windows": mlx_result.confidence_windows,
            }
        )
        save_master_json(
            master_path,
            segments,
            metadata=metadata,
        )
        srt_path.write_text(build_srt_text(segments), encoding="utf-8")
        update("done", 100, "字幕已生成")
        return TranscriptionResult(
            project=project,
            srt_path=srt_path,
            master_path=master_path,
            segments=segments,
            alignment=alignment,
            warnings=warnings,
            quality_issues=quality_issues,
            confidence_windows=mlx_result.confidence_windows,
            confidence_summary=mlx_result.confidence_summary,
        )
    finally:
        wav_path.unlink(missing_ok=True)


def align_existing_project(
    project_path: str | Path,
    *,
    language: str | None = None,
    whisperx_python: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> TranscriptionResult:
    project = Path(project_path).expanduser().resolve()
    master_path = project / "master.json"
    if not master_path.is_file():
        raise TranscriptionError(f"master.json was not found: {master_path}")
    try:
        master_payload = json.loads(master_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TranscriptionError(f"master.json is invalid: {error}") from error
    metadata = master_payload.get("metadata")
    if not isinstance(metadata, dict):
        raise TranscriptionError("master.json does not contain source metadata")
    selected_language = language or str(metadata.get("language", ""))
    whisper_language = resolve_language(selected_language)
    source_name = metadata.get("source")
    if not isinstance(source_name, str) or not source_name:
        raise TranscriptionError("master.json does not identify the source media")
    source = (project / source_name).resolve()
    if source.parent != project:
        raise TranscriptionError("Source media must remain directly inside the project")
    source = validate_media_path(source)
    segments = load_master_json(master_path)
    if not segments:
        raise TranscriptionError("master.json contains no subtitle segments")
    python_path = find_whisperx_python(whisperx_python)
    version = whisperx_version(python_path)
    if python_path is None or version is None:
        raise TranscriptionError("WhisperX environment is not available")

    def update(stage: str, percent: int, message: str) -> None:
        if progress:
            progress(stage, percent, message)

    wav_path = project / ".subflow-realign.wav"
    try:
        update("audio", 20, "正在为现有字幕提取音频")
        _run_ffmpeg(source, wav_path)
        update("aligning", 55, "正在使用 WhisperX 重新对齐现有字幕")
        aligned = _run_whisperx_alignment(
            wav_path,
            segments,
            language=whisper_language,
            python_path=python_path,
            cache_dir=Path(__file__).resolve().parents[1] / ".cache" / "whisperx",
        )
        update("writing", 92, "正在写入独立的 WhisperX 产物")
        aligned_master = project / "master.whisperx.json"
        output_dir = project / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        aligned_srt = output_dir / f"{source.stem}.whisperx.srt"
        aligned_metadata = dict(metadata)
        for key in ("model_path", "large_v3_model_path"):
            if aligned_metadata.get(key):
                aligned_metadata[key] = _portable_model_reference(
                    str(aligned_metadata[key])
                )
        aligned_metadata.update(
            {
                "alignment": "whisperx",
                "whisperx_version": version,
                "derived_from": master_path.name,
                "warnings": [],
            }
        )
        save_master_json(
            aligned_master,
            aligned,
            metadata=_portable_metadata(aligned_metadata),
        )
        aligned_srt.write_text(build_srt_text(aligned), encoding="utf-8")
        update("done", 100, "现有字幕已完成 WhisperX 对齐")
        return TranscriptionResult(
            project=project,
            srt_path=aligned_srt,
            master_path=aligned_master,
            segments=aligned,
            alignment="whisperx",
            warnings=[],
        )
    finally:
        wav_path.unlink(missing_ok=True)
