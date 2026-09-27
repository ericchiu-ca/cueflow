from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from .core import STABLE_ID_RE, SubtitleSegment


@dataclass(frozen=True)
class ReviewIssue:
    severity: str
    code: str
    message: str


# Character budgets per track; the web UI shows exactly these rules because
# it asks /api/review/audit instead of re-implementing them.
MAX_CHARS_BY_TRACK = {"source": 90, "chinese": 45}
MIN_DURATION_SECONDS = 0.8


def _audit_ordered(
    ordered: list[SubtitleSegment],
    *,
    min_duration: float,
    max_source_chars: int,
) -> list[list[ReviewIssue]]:
    results: list[list[ReviewIssue]] = []
    for index, segment in enumerate(ordered):
        current: list[ReviewIssue] = []
        results.append(current)
        text = segment.text.strip()
        if not text:
            current.append(ReviewIssue("ERROR", "EMPTY_TEXT", "字幕文本为空"))
        duration = segment.end - segment.start
        if (
            not math.isfinite(segment.start)
            or not math.isfinite(segment.end)
            or segment.start < 0
            or duration <= 0
        ):
            current.append(ReviewIssue("ERROR", "INVALID_TIME", "结束时间必须晚于开始时间"))
        elif duration < min_duration:
            current.append(
                ReviewIssue("WARN", "SHORT_DURATION", f"持续时间仅 {duration:.2f} 秒")
            )
        visible_length = len(re.sub(r"\s+", "", text))
        if visible_length > max_source_chars:
            current.append(
                ReviewIssue(
                    "WARN",
                    "LONG_TEXT",
                    f"单条字幕共 {visible_length} 个字符，建议精简或拆分",
                )
            )
        if index:
            previous = ordered[index - 1]
            if segment.start < previous.end:
                current.append(
                    ReviewIssue(
                        "WARN",
                        "OVERLAP",
                        f"与上一条 {previous.id} 重叠 {previous.end - segment.start:.2f} 秒",
                    )
                )
            if text and " ".join(text.casefold().split()) == " ".join(previous.text.casefold().split()):
                current.append(
                    ReviewIssue("WARN", "REPEATED_TEXT", f"文本与上一条 {previous.id} 相同")
                )
    return results


def audit_review_segments(
    segments: Iterable[SubtitleSegment],
    *,
    min_duration: float = MIN_DURATION_SECONDS,
    max_source_chars: int = 90,
) -> dict[str, list[ReviewIssue]]:
    ordered = list(segments)
    results = _audit_ordered(ordered, min_duration=min_duration, max_source_chars=max_source_chars)
    return {segment.id: issues for segment, issues in zip(ordered, results)}


def _lenient_number(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def lenient_segments(raw_segments: object) -> list[SubtitleSegment]:
    """Editor segments as-is; invalid numbers become NaN rather than errors."""
    if not isinstance(raw_segments, list):
        raise ValueError("Review audit request must contain a segment list.")
    return [
        SubtitleSegment(
            id=str(item.get("id", "")) if isinstance(item, Mapping) else "",
            start=_lenient_number(item.get("start")) if isinstance(item, Mapping) else math.nan,
            end=_lenient_number(item.get("end")) if isinstance(item, Mapping) else math.nan,
            text=str(item.get("text") or "") if isinstance(item, Mapping) else "",
            words=[],
        )
        for item in raw_segments
    ]


def audit_review_track(raw_segments: object, track: str) -> list[list[dict]]:
    """Audit an in-progress editor track, returning issues by position.

    Unlike saving, this never rejects input: half-edited values (blank times,
    duplicate IDs) must still produce the same issues the save would show.
    """
    if track not in MAX_CHARS_BY_TRACK:
        raise ValueError("Review track must be source or chinese.")
    results = _audit_ordered(
        lenient_segments(raw_segments),
        min_duration=MIN_DURATION_SECONDS,
        max_source_chars=MAX_CHARS_BY_TRACK[track],
    )
    return [[asdict(issue) for issue in issues] for issues in results]


def review_payload(
    segments: Iterable[SubtitleSegment],
    reviewed: Mapping[str, bool] | None = None,
    *,
    max_source_chars: int = 90,
) -> list[dict]:
    values = list(segments)
    issue_map = audit_review_segments(values, max_source_chars=max_source_chars)
    reviewed = reviewed or {}
    return [
        {
            "id": segment.id,
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "reviewed": bool(reviewed.get(segment.id, False)),
            "issues": [asdict(issue) for issue in issue_map[segment.id]],
        }
        for segment in values
    ]


def review_summary(payload: Iterable[Mapping[str, object]]) -> dict[str, int]:
    values = list(payload)
    errors = 0
    warnings = 0
    issue_segments = 0
    reviewed = 0
    for segment in values:
        if segment.get("reviewed") is True:
            reviewed += 1
        raw_issues = segment.get("issues")
        segment_issues = raw_issues if isinstance(raw_issues, list) else []
        if segment_issues:
            issue_segments += 1
        for issue in segment_issues:
            if not isinstance(issue, Mapping):
                continue
            if issue.get("severity") == "ERROR":
                errors += 1
            else:
                warnings += 1
    return {
        "segments": len(values),
        "reviewed": reviewed,
        "issue_segments": issue_segments,
        "errors": errors,
        "warnings": warnings,
    }


def segments_from_review_payload(
    raw_segments: object,
    expected_ids: list[str],
    *,
    allow_structure_changes: bool = False,
) -> tuple[list[SubtitleSegment], dict[str, bool]]:
    if not isinstance(raw_segments, list):
        raise ValueError("Review save request must contain a segment list.")
    if not raw_segments:
        raise ValueError("A subtitle track cannot be empty.")
    if not allow_structure_changes and len(raw_segments) != len(expected_ids):
        raise ValueError(
            f"Review segment count changed: expected {len(expected_ids)}, received {len(raw_segments)}."
        )
    segments: list[SubtitleSegment] = []
    reviewed: dict[str, bool] = {}
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_segments, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"Review segment {index} must be an object.")
        segment_id = str(item.get("id", ""))
        expected_id = expected_ids[index - 1] if index <= len(expected_ids) else None
        if not allow_structure_changes and segment_id != expected_id:
            raise ValueError(
                f"Review segment ID/order changed at position {index}: expected {expected_id}, received {segment_id}."
            )
        if not STABLE_ID_RE.fullmatch(segment_id):
            raise ValueError(f"Review segment {index} has an invalid stable ID: {segment_id}.")
        if segment_id in seen_ids:
            raise ValueError(f"Review segment ID is duplicated: {segment_id}.")
        seen_ids.add(segment_id)
        start = item.get("start")
        end = item.get("end")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(start) < 0
            or float(end) <= float(start)
        ):
            raise ValueError(f"Segment {segment_id} has an invalid time range.")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Segment {segment_id} has empty subtitle text.")
        segments.append(
            SubtitleSegment(
                id=segment_id,
                start=round(float(start), 3),
                end=round(float(end), 3),
                text=text.strip(),
                words=[],
            )
        )
        reviewed[segment_id] = item.get("reviewed") is True
    return segments, reviewed
