from __future__ import annotations

import json
import re
import shutil
import tempfile
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean
from typing import Callable

from .core import SubtitleSegment, normalize_segments
from .proc import run_captured


VAD_PROTOCOL = "cueflow-vad-v1"
MLX_PROTOCOL = "cueflow-mlx-batch-v1"
LANGUAGE_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ'’]+")
LONG_ALPHA_RUN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]{22,}")
FRENCH_MARKERS = {
    "alors", "au", "aux", "avec", "avait", "ce", "ces", "cette", "comme",
    "dans", "de", "des", "donc", "du", "elle", "elles", "en", "entre", "est",
    "et", "être", "était", "fait", "il", "ils", "la", "le", "les", "leur",
    "leurs", "mais", "même", "ne", "nous", "ont", "ou", "par", "pas", "plus",
    "pour", "que", "qui", "sa", "se", "ses", "son", "sont", "sur", "tout",
    "tous", "un", "une", "vous",
}
ENGLISH_MARKERS = {
    "about", "all", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "for", "from", "had", "has", "have", "he", "in", "into", "is", "it",
    "its", "more", "not", "of", "or", "she", "that", "the", "their", "them",
    "these", "they", "this", "those", "to", "was", "we", "were", "with", "would",
    "you",
}


def _text_language_evidence(text: str) -> tuple[str | None, float]:
    tokens: list[str] = []
    for match in LANGUAGE_TOKEN_RE.finditer(text.casefold().replace("’", "'")):
        tokens.extend(part for part in match.group().split("'") if part)
    french = float(sum(token in FRENCH_MARKERS for token in tokens))
    english = float(sum(token in ENGLISH_MARKERS for token in tokens))
    accented = sum(character in "àâçéèêëîïôùûüÿœ" for character in text.casefold())
    french += min(3.0, accented * 0.5)
    winner = max(french, english)
    loser = min(french, english)
    confidence = (winner - loser) / winner if winner else 0.0
    if winner < 3.0 or winner - loser < 2.0:
        return None, round(confidence, 3)
    return ("fr" if french > english else "en"), round(confidence, 3)


def _text_anomalies(text: str) -> list[str]:
    long_runs = LONG_ALPHA_RUN_RE.findall(text)
    if any(len(run) >= 32 for run in long_runs) or sum(len(run) >= 22 for run in long_runs) >= 2:
        return ["unspaced_text"]
    compact = "".join(text.split())
    if len(compact) >= 80 and len(text.split()) <= 3:
        return ["unspaced_text"]
    return []


@dataclass
class AdvancedAsrResult:
    segments: list[SubtitleSegment]
    quality_issues: list[dict]
    confidence_windows: list[dict]
    confidence_summary: dict
    vad_window_count: int
    large_v3_window_count: int
    large_v3_selected_count: int


def _offline_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    return env


def _run_json_worker(
    python: Path, script: Path, request: dict, environment: dict[str, str]
) -> dict:
    completed = run_captured(
        [str(python), str(script)],
        input=json.dumps(request, ensure_ascii=False),
        timeout=28800,
        env=environment,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"{script.name} exited with {completed.returncode}: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{script.name} returned invalid JSON") from exc


def _helper_python(helper_path: Path) -> Path:
    python = helper_path.parent / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"FrameLedger MLX Python was not found: {python}")
    return python


def _write_clips(source: Path, windows: list[dict], directory: Path) -> list[Path]:
    clips: list[Path] = []
    with wave.open(str(source), "rb") as audio:
        sample_rate = audio.getframerate()
        params = audio.getparams()
        for window in windows:
            start_frame = max(0, int(float(window["start"]) * sample_rate))
            end_frame = min(audio.getnframes(), int(float(window["end"]) * sample_rate))
            audio.setpos(start_frame)
            frames = audio.readframes(max(0, end_frame - start_frame))
            clip = directory / f"window-{int(window['index']):05d}.wav"
            with wave.open(str(clip), "wb") as output:
                output.setparams(params)
                output.writeframes(frames)
            clips.append(clip)
    return clips


def _batch_results(
    python: Path, model: Path, items: list[dict], environment: dict[str, str]
) -> dict[str, dict]:
    response = _run_json_worker(
        python,
        Path(__file__).with_name("mlx_batch_runner.py"),
        {"protocol": MLX_PROTOCOL, "model_path": str(model), "items": items},
        environment,
    )
    return {str(item.get("id", "")): item for item in response.get("items", [])}


def _float_values(items: list[dict], key: str) -> list[float]:
    return [float(item[key]) for item in items if isinstance(item.get(key), (int, float))]


def _metrics(result: dict, duration: float) -> dict:
    raw_segments = [item for item in result.get("segments", []) if isinstance(item, dict)]
    logprobs = _float_values(raw_segments, "avg_logprob")
    ratios = _float_values(raw_segments, "compression_ratio")
    no_speech = _float_values(raw_segments, "no_speech_prob")
    word_scores: list[float] = []
    speech_duration = 0.0
    for segment in raw_segments:
        start = segment.get("start")
        end = segment.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            speech_duration += max(0.0, float(end) - float(start))
        for word in segment.get("words", []) or []:
            if isinstance(word, dict):
                value = word.get("probability", word.get("score"))
                if isinstance(value, (int, float)):
                    word_scores.append(float(value))
    return {
        "avg_logprob": mean(logprobs) if logprobs else None,
        "max_compression_ratio": max(ratios) if ratios else None,
        "avg_no_speech_prob": mean(no_speech) if no_speech else None,
        "avg_word_probability": mean(word_scores) if word_scores else None,
        "speech_coverage": min(1.0, speech_duration / max(duration, 0.001)),
    }


def _score(text: str, metrics: dict, has_quality_issue: bool) -> int:
    if not text.strip():
        return 0
    score = 100.0
    logprob = metrics.get("avg_logprob")
    if logprob is None:
        score -= 8
    elif logprob < -0.25:
        score -= min(32.0, (-0.25 - logprob) * 38.0)
    ratio = metrics.get("max_compression_ratio")
    if ratio is not None and ratio > 2.4:
        score -= min(40.0, (ratio - 2.4) * 12.0 + 12.0)
    no_speech = metrics.get("avg_no_speech_prob")
    if no_speech is not None and no_speech > 0.45:
        score -= min(20.0, (no_speech - 0.45) * 36.0)
    word_probability = metrics.get("avg_word_probability")
    if word_probability is not None and word_probability < 0.8:
        score -= min(25.0, (0.8 - word_probability) * 90.0)
    if metrics.get("speech_coverage", 0.0) < 0.08:
        score -= 18.0
    if has_quality_issue:
        score -= 15.0
    return max(0, min(100, int(round(score))))


def _tier(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def _candidate(
    response: dict | None,
    duration: float,
    requested_language: str,
    parse_result: Callable,
    quality_analyzer: Callable,
) -> dict:
    if not response or response.get("error"):
        return {
            "error": str((response or {}).get("error", "missing worker result")),
            "segments": [],
            "quality_issues": [],
            "metrics": {},
            "score": 0,
            "text": "",
            "language": requested_language if requested_language != "auto" else "unknown",
            "text_language": None,
            "text_language_confidence": 0.0,
            "language_conflict": False,
            "text_anomalies": [],
        }
    result = response.get("result") or {}
    issues = quality_analyzer(result)
    try:
        segments = parse_result(result, enforce_transcript_quality=False)
    except Exception as exc:
        return {
            "error": str(exc),
            "segments": [],
            "quality_issues": issues,
            "metrics": _metrics(result, duration),
            "score": 0,
            "text": "",
            "language": str(result.get("language") or requested_language or "unknown"),
            "text_language": None,
            "text_language_confidence": 0.0,
            "language_conflict": False,
            "text_anomalies": [],
        }
    text = " ".join(segment.text for segment in segments).strip()
    metrics = _metrics(result, duration)
    detected_language = str(result.get("language") or requested_language or "unknown")
    text_language, text_language_confidence = _text_language_evidence(text)
    language_conflict = (
        detected_language in {"en", "fr"}
        and text_language in {"en", "fr"}
        and detected_language != text_language
        and text_language_confidence >= 0.35
    )
    text_anomalies = _text_anomalies(text)
    score = _score(text, metrics, bool(issues))
    if language_conflict:
        score = max(0, score - 22)
    if text_anomalies:
        score = max(0, score - 30)
    return {
        "error": "",
        "segments": segments,
        "quality_issues": issues,
        "metrics": metrics,
        "score": score,
        "text": text,
        "language": detected_language,
        "text_language": text_language,
        "text_language_confidence": text_language_confidence,
        "language_conflict": language_conflict,
        "text_anomalies": text_anomalies,
    }


def _offset_segments(segments: list[SubtitleSegment], offset: float) -> list[SubtitleSegment]:
    output: list[SubtitleSegment] = []
    for segment in segments:
        words: list[dict] = []
        for source_word in segment.words:
            word = dict(source_word)
            for key in ("start", "end"):
                if isinstance(word.get(key), (int, float)):
                    word[key] = round(float(word[key]) + offset, 3)
            words.append(word)
        output.append(
            SubtitleSegment(
                id="",
                start=float(segment.start) + offset,
                end=float(segment.end) + offset,
                text=segment.text,
                words=words,
            )
        )
    return output


def _words_to_text(words: list[dict]) -> str:
    pieces = [str(word.get("word", word.get("text", ""))) for word in words]
    if any(piece[:1].isspace() for piece in pieces[1:]):
        return "".join(pieces).strip()
    text = " ".join(piece.strip() for piece in pieces if piece.strip())
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    text = re.sub(r"\s+(['’])", r"\1", text)
    text = re.sub(r"(['’])\s+", r"\1", text)
    text = re.sub(r"([([{])\s+", r"\1", text)
    return text.strip()


def _clip_segments_to_ownership(
    segments: list[SubtitleSegment],
    *,
    start: float | None = None,
    end: float | None = None,
) -> list[SubtitleSegment]:
    clipped: list[SubtitleSegment] = []
    for segment in segments:
        timed_words: list[tuple[dict, float]] = []
        for source_word in segment.words:
            word_start = source_word.get("start")
            word_end = source_word.get("end")
            if isinstance(word_start, (int, float)) and isinstance(word_end, (int, float)):
                timed_words.append(
                    (dict(source_word), (float(word_start) + float(word_end)) / 2.0)
                )

        if timed_words:
            kept_words = [
                word
                for word, midpoint in timed_words
                if (start is None or midpoint >= start) and (end is None or midpoint < end)
            ]
            if not kept_words:
                continue
            text = (
                segment.text
                if len(kept_words) == len(timed_words)
                else _words_to_text(kept_words)
            )
            word_starts = [
                float(word["start"])
                for word in kept_words
                if isinstance(word.get("start"), (int, float))
            ]
            word_ends = [
                float(word["end"])
                for word in kept_words
                if isinstance(word.get("end"), (int, float))
            ]
            clipped.append(
                SubtitleSegment(
                    id="",
                    start=min(word_starts) if word_starts else segment.start,
                    end=max(word_ends) if word_ends else segment.end,
                    text=text or segment.text,
                    words=kept_words,
                )
            )
            continue

        midpoint = (segment.start + segment.end) / 2.0
        if (start is not None and midpoint < start) or (end is not None and midpoint >= end):
            continue
        clipped.append(segment)
    return clipped


def _apply_ownership_boundaries(
    window_segments: list[list[SubtitleSegment]], windows: list[dict]
) -> list[list[SubtitleSegment]]:
    owned = [list(segments) for segments in window_segments]
    for index in range(len(owned) - 1):
        left_end = float(windows[index]["end"])
        right_start = float(windows[index + 1]["start"])
        if left_end <= right_start:
            continue
        boundary = (left_end + right_start) / 2.0
        owned[index] = _clip_segments_to_ownership(owned[index], end=boundary)
        owned[index + 1] = _clip_segments_to_ownership(
            owned[index + 1], start=boundary
        )
    return owned


def _shift_issue(issue: dict, offset: float, window_index: int) -> dict:
    shifted = dict(issue)
    for key in ("start", "end", "seek"):
        if isinstance(shifted.get(key), (int, float)):
            shifted[key] = round(float(shifted[key]) + offset, 3)
    shifted["window_key"] = f"vad:{window_index}:{shifted.get('window_key', 'quality')}"
    return shifted


def _similarity(left: str, right: str) -> float | None:
    if not left.strip() or not right.strip():
        return None
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _candidate_problem_count(candidate: dict) -> int:
    return (
        len(candidate["quality_issues"])
        + len(candidate["text_anomalies"])
        + int(candidate["language_conflict"])
        + int(bool(candidate["error"]))
    )


def transcribe_vad_cascade(
    *,
    wav_path: Path,
    project_dir: Path,
    language: str,
    turbo_model: Path,
    large_model: Path,
    helper_path: Path,
    whisperx_python: Path,
    parse_result: Callable,
    quality_analyzer: Callable,
    environment_factory: Callable[[], dict[str, str]],
) -> AdvancedAsrResult:
    if language not in {"en", "fr", "mixed"}:
        raise RuntimeError(f"Unsupported advanced ASR language: {language}")
    worker_python = _helper_python(helper_path)
    environment = _offline_env(environment_factory())
    requested_language = {"en": "en", "fr": "fr", "mixed": "auto"}[language]
    vad = _run_json_worker(
        whisperx_python,
        Path(__file__).with_name("vad_runner.py"),
        {
            "protocol": VAD_PROTOCOL,
            "audio_path": str(wav_path),
            "onset": 0.5,
            "offset": 0.363,
            "chunk_size": 28.0,
            "padding": 0.2,
        },
        environment,
    )
    windows = [
        window for window in vad.get("windows", []) if float(window.get("duration", 0)) > 0
    ]
    if not windows:
        raise RuntimeError("Voice activity detection found no speech")

    project_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".subflow-vad-", dir=project_dir))
    try:
        clips = _write_clips(wav_path, windows, temp_dir)
        turbo_items = [
            {
                "id": str(window["index"]),
                "audio_path": str(clip),
                "language": requested_language,
            }
            for window, clip in zip(windows, clips)
        ]
        turbo_output = _batch_results(worker_python, turbo_model, turbo_items, environment)
        turbo_candidates = [
            _candidate(
                turbo_output.get(str(window["index"])),
                float(window["duration"]),
                requested_language,
                parse_result,
                quality_analyzer,
            )
            for window in windows
        ]
        retry_reasons: dict[int, list[str]] = {}
        neighbor_consensus: dict[int, str] = {}
        for index, candidate in enumerate(turbo_candidates):
            reasons: list[str] = []
            if candidate["error"]:
                reasons.append("worker_error")
            if candidate["score"] < 80:
                reasons.append("low_confidence")
            if candidate["quality_issues"]:
                reasons.append("quality_gate")
            if candidate["text_anomalies"]:
                reasons.extend(candidate["text_anomalies"])
            if language == "mixed":
                if candidate["language"] not in {"en", "fr"}:
                    reasons.append("unsupported_language")
                if candidate["language_conflict"]:
                    reasons.append("language_conflict")
                previous = turbo_candidates[index - 1]["language"] if index > 0 else None
                following = (
                    turbo_candidates[index + 1]["language"]
                    if index + 1 < len(turbo_candidates)
                    else None
                )
                if (
                    previous in {"en", "fr"}
                    and previous == following
                    and candidate["language"] != previous
                ):
                    reasons.append("isolated_language")
                    neighbor_consensus[index] = previous
            if reasons:
                retry_reasons[index] = list(dict.fromkeys(reasons))

        retry_indexes = sorted(retry_reasons)
        retry_languages: dict[int, list[str]] = {}
        large_items: list[dict] = []
        for index in retry_indexes:
            candidate = turbo_candidates[index]
            languages = [requested_language]
            if language == "mixed":
                primary = (
                    candidate["text_language"]
                    if candidate["text_language"] in {"en", "fr"}
                    else candidate["language"]
                    if candidate["language"] in {"en", "fr"}
                    else "auto"
                )
                languages = [primary]
                if "isolated_language" in retry_reasons[index]:
                    languages.append(neighbor_consensus[index])
                elif index in neighbor_consensus and (
                    candidate["text_anomalies"]
                    or candidate["language"] not in {"en", "fr"}
                ):
                    languages = [neighbor_consensus[index]]
            languages = list(dict.fromkeys(languages))
            retry_languages[index] = languages
            for retry_language in languages:
                large_items.append(
                    {
                        "id": f"{windows[index]['index']}:{retry_language}",
                        "audio_path": str(clips[index]),
                        "language": retry_language,
                    }
                )
        large_output = (
            _batch_results(worker_python, large_model, large_items, environment)
            if large_items
            else {}
        )

        selected_window_segments: list[list[SubtitleSegment]] = []
        quality_issues: list[dict] = []
        confidence_windows: list[dict] = []
        large_selected = 0
        language_counts: dict[str, int] = {}
        for index, (window, turbo) in enumerate(zip(windows, turbo_candidates)):
            large = None
            if index in retry_indexes:
                large_candidates: list[dict] = []
                for retry_language in retry_languages[index]:
                    candidate = _candidate(
                        large_output.get(f"{window['index']}:{retry_language}"),
                        float(window["duration"]),
                        retry_language,
                        parse_result,
                        quality_analyzer,
                    )
                    candidate["retry_language"] = retry_language
                    large_candidates.append(candidate)
                large = max(
                    large_candidates,
                    key=lambda candidate: (
                        candidate["score"],
                        -_candidate_problem_count(candidate),
                    ),
                )
            selected = turbo
            selected_model = "turbo"
            if large and not large["error"]:
                if turbo["error"] or large["score"] > turbo["score"] or (
                    large["score"] == turbo["score"]
                    and _candidate_problem_count(large) < _candidate_problem_count(turbo)
                ):
                    selected = large
                    selected_model = "large-v3"
                    large_selected += 1

            similarity = _similarity(turbo["text"], large["text"]) if large else None
            start = float(window["start"])
            end = float(window["end"])
            selected_window_segments.append(_offset_segments(selected["segments"], start))
            quality_issues.extend(
                _shift_issue(issue, start, int(window["index"]))
                for issue in selected["quality_issues"]
            )
            score = int(selected["score"])
            tier = _tier(score)
            detected_language = selected["language"]
            language_counts[detected_language] = language_counts.get(detected_language, 0) + 1

            if selected["error"]:
                quality_issues.append(
                    {
                        "severity": "ERROR",
                        "code": "ASR_WINDOW_FAILED",
                        "message": f"语音窗识别失败：{selected['error']}",
                        "start": start,
                        "end": end,
                        "window_key": f"vad:{window['index']}:failed",
                    }
                )
            elif tier != "high":
                quality_issues.append(
                    {
                        "severity": "ERROR" if tier == "low" else "WARN",
                        "code": "ASR_LOW_CONFIDENCE" if tier == "low" else "ASR_MEDIUM_CONFIDENCE",
                        "message": f"该语音窗转录置信度为 {score}/100（{tier}），建议人工复核。",
                        "start": start,
                        "end": end,
                        "text": selected["text"],
                        "window_key": f"vad:{window['index']}:confidence",
                    }
                )
            if selected["language_conflict"]:
                quality_issues.append(
                    {
                        "severity": "WARN",
                        "code": "ASR_LANGUAGE_CONFLICT",
                        "message": (
                            f"Whisper 检测为 {detected_language}，但文本证据支持 "
                            f"{selected['text_language']}，建议人工复核。"
                        ),
                        "start": start,
                        "end": end,
                        "text": selected["text"],
                        "window_key": f"vad:{window['index']}:language-conflict",
                    }
                )
            if "unspaced_text" in selected["text_anomalies"]:
                quality_issues.append(
                    {
                        "severity": "WARN",
                        "code": "ASR_UNSPACED_TEXT",
                        "message": "检测到异常长的无空格文本，建议人工复核。",
                        "start": start,
                        "end": end,
                        "text": selected["text"],
                        "window_key": f"vad:{window['index']}:unspaced-text",
                    }
                )
            if similarity is not None and similarity < 0.6:
                quality_issues.append(
                    {
                        "severity": "WARN",
                        "code": "ASR_MODEL_DISAGREEMENT",
                        "message": f"Turbo 与 large-v3 文本差异较大（相似度 {similarity:.2f}），建议人工复核。",
                        "start": start,
                        "end": end,
                        "text": selected["text"],
                        "window_key": f"vad:{window['index']}:model-disagreement",
                    }
                )
            if language == "mixed" and detected_language not in {"en", "fr"}:
                quality_issues.append(
                    {
                        "severity": "WARN",
                        "code": "ASR_UNSUPPORTED_LANGUAGE",
                        "message": f"检测到非英语/法语语音：{detected_language}。",
                        "start": start,
                        "end": end,
                        "text": selected["text"],
                        "window_key": f"vad:{window['index']}:language",
                    }
                )
            confidence_windows.append(
                {
                    "window": int(window["index"]),
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "language": detected_language,
                    "text_language": selected["text_language"],
                    "text_language_confidence": selected["text_language_confidence"],
                    "language_conflict": selected["language_conflict"],
                    "text_anomalies": selected["text_anomalies"],
                    "model": selected_model,
                    "score": score,
                    "tier": tier,
                    "turbo_score": int(turbo["score"]),
                    "large_v3_score": int(large["score"]) if large else None,
                    "model_similarity": round(similarity, 3) if similarity is not None else None,
                    "retry_reasons": retry_reasons.get(index, []),
                    "retry_languages": retry_languages.get(index, []),
                    "retry_language": (
                        large.get("retry_language")
                        if large and selected_model == "large-v3"
                        else retry_languages.get(index, [None])[0]
                        if len(retry_languages.get(index, [])) == 1
                        else None
                    ),
                    "metrics": {
                        key: round(value, 4) if isinstance(value, float) else value
                        for key, value in selected["metrics"].items()
                    },
                }
            )

        owned_windows = _apply_ownership_boundaries(selected_window_segments, windows)
        selected_segments = [segment for group in owned_windows for segment in group]
        segments = normalize_segments(sorted(selected_segments, key=lambda item: (item.start, item.end)))
        if not segments:
            # Every window failing (e.g. unloadable weights) must not look like
            # a successful, empty transcription.
            failures = [
                str(issue["message"])
                for issue in quality_issues
                if issue.get("code") == "ASR_WINDOW_FAILED"
            ]
            raise RuntimeError(
                "VAD cascade produced no subtitle segments"
                + (f" ({len(failures)} window(s) failed; first: {failures[0][:300]})" if failures else "")
            )
        for window in confidence_windows:
            window["segment_ids"] = [
                segment.id
                for segment in segments
                if min(float(window["end"]), segment.end)
                > max(float(window["start"]), segment.start)
            ]
        tiers = {"high": 0, "medium": 0, "low": 0}
        for window in confidence_windows:
            tiers[window["tier"]] += 1
        summary = {
            "total_windows": len(confidence_windows),
            "high": tiers["high"],
            "medium": tiers["medium"],
            "low": tiers["low"],
            "large_v3_escalated": len(retry_indexes),
            "large_v3_attempts": len(large_items),
            "large_v3_selected": large_selected,
            "languages": language_counts,
            "language_rechecks": sum(
                bool({"language_conflict", "isolated_language"} & set(reasons))
                for reasons in retry_reasons.values()
            ),
            "unspaced_rechecks": sum(
                "unspaced_text" in reasons for reasons in retry_reasons.values()
            ),
        }
        return AdvancedAsrResult(
            segments=segments,
            quality_issues=quality_issues,
            confidence_windows=confidence_windows,
            confidence_summary=summary,
            vad_window_count=len(windows),
            large_v3_window_count=len(retry_indexes),
            large_v3_selected_count=large_selected,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
