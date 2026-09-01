from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Iterator, List, Dict

SRT_TIMEPOINT_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})[.,](\d{3})$")
ID_TAG_RE = re.compile(r"^\[(\d+)\]\s*$")


@dataclass
class SubtitleSegment:
    id: str
    start: float
    end: float
    text: str
    words: list


def _to_seconds(timecode: str) -> float:
    match = SRT_TIMEPOINT_RE.match(timecode.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {timecode}")
    h, m, s, ms = map(int, match.group(1, 2, 3, 4))
    return h * 3600 + m * 60 + s + ms / 1000.0


def _from_seconds(value: float) -> str:
    total_ms = max(0, int(round(float(value) * 1000)))
    td = timedelta(milliseconds=total_ms)
    total_seconds = int(td.total_seconds())
    ms = int(td.microseconds / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def stable_id(index: int) -> str:
    return f"{index:04d}"


def normalize_segments(segments: Iterable[SubtitleSegment]) -> List[SubtitleSegment]:
    return [
        SubtitleSegment(
            id=stable_id(i),
            start=round(float(seg.start), 2),
            end=round(float(seg.end), 2),
            text=(seg.text or "").strip(),
            words=list(seg.words),
        )
        for i, seg in enumerate(segments, start=1)
    ]


def parse_srt_text(raw: str) -> List[SubtitleSegment]:
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    blocks = [b.strip() for b in normalized.split("\n\n") if b.strip()]
    parsed: List[SubtitleSegment] = []

    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip() != ""]
        if len(lines) < 2:
            continue

        if lines[0].isdigit():
            if len(lines) < 3:
                continue
            time_line = lines[1]
            text_lines = lines[2:]
        else:
            time_line = lines[0]
            text_lines = lines[1:]

        if "-->" not in time_line:
            continue
        start_raw, end_raw = [x.strip() for x in time_line.split("-->")]
        start = _to_seconds(start_raw)
        end = _to_seconds(end_raw)
        if end <= start:
            continue

        text = "\n".join(line.strip() for line in text_lines).strip()
        if not text:
            continue

        parsed.append(SubtitleSegment(id="", start=round(start, 2), end=round(end, 2), text=text, words=[]))

    return normalize_segments(parsed)


def parse_srt_file(path: Path) -> List[SubtitleSegment]:
    return parse_srt_text(path.read_text(encoding="utf-8"))


def build_srt_text(segments: List[SubtitleSegment], translations: Dict[str, str] | None = None) -> str:
    lines: List[str] = []
    for i, segment in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_from_seconds(segment.start)} --> {_from_seconds(segment.end)}")
        text = segment.text.strip()
        if translations is not None:
            text = translations.get(segment.id, "").strip()
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def build_bilingual_srt_text(segments: List[SubtitleSegment], translations: Dict[str, str]) -> str:
    lines: List[str] = []
    for i, segment in enumerate(segments, start=1):
        zh = (translations.get(segment.id, "") or "").strip()
        en = segment.text.strip()
        lines.append(str(i))
        lines.append(f"{_from_seconds(segment.start)} --> {_from_seconds(segment.end)}")
        if zh:
            lines.append(f"{en}\n{zh}")
        else:
            lines.append(en)
        lines.append("")
    return "\n".join(lines)


def build_translation_input_text(segments: List[SubtitleSegment]) -> str:
    lines: List[str] = []
    for segment in segments:
        lines.append(f"[{segment.id}]")
        lines.append(segment.text.replace("\n", " "))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_master_json(
    path: Path,
    segments: List[SubtitleSegment],
    metadata: dict | None = None,
) -> None:
    payload = {
        "segments": [asdict(segment) for segment in segments],
    }
    if metadata:
        payload["metadata"] = metadata
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_master_json(path: Path) -> List[SubtitleSegment]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    segments_raw = raw.get("segments", [])
    return [
        SubtitleSegment(
            id=str(item.get("id", "")),
            start=float(item.get("start", 0.0)),
            end=float(item.get("end", 0.0)),
            text=str(item.get("text", "")),
            words=list(item.get("words", [])),
        )
        for item in segments_raw
    ]


def parse_translation_file(path: Path) -> Dict[str, str]:
    content = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    translations: Dict[str, str] = {}
    current_id: str | None = None
    chunk: List[str] = []

    def _flush():
        if current_id is None:
            return
        translations[current_id] = "\n".join(chunk).strip()

    for line in content.split("\n"):
        match = ID_TAG_RE.match(line.strip())
        if match:
            _flush()
            current_id = stable_id(int(match.group(1)))
            if current_id in translations:
                raise ValueError(f"Duplicated translation ID: {current_id}")
            chunk = []
            continue
        if current_id is not None:
            chunk.append(line.rstrip())

    _flush()
    return translations
