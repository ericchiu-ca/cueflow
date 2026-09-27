from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from .core import STABLE_ID_RE, SubtitleSegment


@dataclass
class QCIssue:
    level: str
    code: str
    message: str


def _chinese_char_length(value: str) -> int:
    stripped = re.sub(r"\s+", "", value or "")
    return len(stripped)


def run_qc(
    segments: List[SubtitleSegment],
    translations: Dict[str, str] | None = None,
    min_duration: float = 1.0,
    max_chinese_chars: int = 45,
) -> List[QCIssue]:
    issues: List[QCIssue] = []
    if not segments:
        issues.append(QCIssue("ERROR", "NO_SEGMENTS", "master.json missing or empty"))
        return issues

    ids = [seg.id for seg in segments]

    # Duplicate / malformed ID checks.
    unique_ids = set(ids)
    if len(ids) != len(unique_ids):
        seen = set()
        for seg_id in ids:
            if seg_id in seen:
                issues.append(QCIssue("ERROR", "DUPLICATE_ID", f"Duplicate segment ID in master: {seg_id}"))
                break
            seen.add(seg_id)

    invalid_ids = [seg_id for seg_id in ids if not STABLE_ID_RE.fullmatch(seg_id)]
    if invalid_ids:
        issues.append(QCIssue("ERROR", "INVALID_ID", f"Invalid segment IDs: {', '.join(invalid_ids[:5])}"))

    expected_ids = [f"{i:04d}" for i in range(1, len(ids) + 1)]
    missing_ids = [item for item in expected_ids if item not in unique_ids]
    if missing_ids:
        issues.append(QCIssue("ERROR", "MISSING_ID", f"Missing IDs in master: {', '.join(missing_ids[:5])}"))

    # Time checks.
    ordered = sorted(segments, key=lambda s: s.start)
    for index in range(1, len(ordered)):
        prev = ordered[index - 1]
        current = ordered[index]
        if current.start < prev.end:
            issues.append(
                QCIssue(
                    "ERROR",
                    "OVERLAP",
                    f"Overlap detected: {prev.id} ({prev.start:.2f}-{prev.end:.2f}) overlaps {current.id} ({current.start:.2f}-{current.end:.2f})",
                )
            )

    for segment in segments:
        duration = segment.end - segment.start
        if duration < min_duration:
            issues.append(
                QCIssue(
                    "WARN",
                    "SHORT_SEGMENT",
                    f"Short segment {segment.id}: {duration:.2f}s",
                )
            )

    if translations is not None:
        translation_ids = set(translations.keys())
        if len(translations) != len(ids):
            issues.append(
                QCIssue(
                    "ERROR",
                    "COUNT_MISMATCH",
                    f"Translation segments ({len(translations)}) != master segments ({len(ids)})",
                )
            )
        missing_in_translation = [seg_id for seg_id in ids if seg_id not in translation_ids]
        if missing_in_translation:
            issues.append(
                QCIssue(
                    "ERROR",
                    "MISSING_TRANSLATION",
                    f"Master IDs missing in translation: {', '.join(missing_in_translation[:5])}",
                )
            )
        extra_translation_ids = [seg_id for seg_id in translation_ids if seg_id not in unique_ids]
        if extra_translation_ids:
            issues.append(
                QCIssue(
                    "WARN",
                    "EXTRA_TRANSLATION_ID",
                    f"Translation IDs not in master: {', '.join(extra_translation_ids[:5])}",
                )
            )

        for seg_id, text in translations.items():
            if not text.strip():
                issues.append(QCIssue("ERROR", "EMPTY_TRANSLATION", f"Empty translation for ID {seg_id}"))
            elif _chinese_char_length(text) > max_chinese_chars:
                issues.append(
                    QCIssue(
                        "WARN",
                        "LONG_CHINESE_SEGMENT",
                        f"Long Chinese text at {seg_id}: {len(text)} chars",
                    )
                )

    return issues
