from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Callable, Iterable

from .core import SubtitleSegment, parse_srt_file, track_pairing
from .ffmpeg_tools import find_ffmpeg, has_ass_filter
from .proc import kill_tree, popen, release


SOURCE_HAN_FONT = "CueFlowHanSansSC-SemiBold.otf"
INTER_FONT = "Inter-Medium.ttf"
MULISH_FONT = "Mulish-SemiBold.ttf"
# Shipped as package data so every install scheme (editable, wheel, --user,
# venv) finds them next to this module rather than under sys.prefix.
DEFAULT_FONTS_DIR = Path(__file__).resolve().with_name("fonts")

ASS_PLAY_RES_X = 1920
ASS_PLAY_RES_Y = 1080
CHINESE_FONT_NAME = "CueFlow Han Sans SC"
CHINESE_FONT_SIZE = 94
CHINESE_OUTLINE = 4.6
CHINESE_MARGIN_V = 166
SOURCE_FONT_NAME = "Mulish SemiBold"
SOURCE_FONT_SIZE = 64
SOURCE_OUTLINE = 4.4
SOURCE_MARGIN_V = 94
ASS_MARGIN_V = SOURCE_MARGIN_V
ASS_PREVIEW_WIDTH = 816
ASS_PREVIEW_HEIGHT = 251
ASS_PREVIEW_CROP_Y = 490
ASS_PREVIEW_CROP_HEIGHT = 590
ASS_SAFE_HORIZONTAL_RATIO = 0.05
ASS_SAFE_BOTTOM_RATIO = 0.08
ASS_LANGUAGE_GAP = 14
ASS_CJK_ADVANCE_RATIO = 0.68
VIDEO_FILTER_SOURCE = "ass=subtitle.ass:fontsdir=fonts"
VIDEO_FILTER_1080P = (
    "scale=w='min(1920,iw)':h='min(1080,ih)':"
    "force_original_aspect_ratio=decrease:force_divisible_by=2,"
    "ass=subtitle.ass:fontsdir=fonts"
)

ProgressCallback = Callable[[str, int, str], None]


class SubtitleBuildError(ValueError):
    """Raised when two subtitle tracks cannot form a bilingual subtitle."""


class VideoBurnError(RuntimeError):
    """Raised when ASS rendering or video encoding cannot be completed."""


@dataclass(frozen=True)
class _VideoGeometry:
    width: int
    height: int
    active_x: int = 0
    active_y: int = 0
    active_width: int | None = None
    active_height: int | None = None

    @property
    def content_width(self) -> int:
        return self.active_width if self.active_width is not None else self.width

    @property
    def content_height(self) -> int:
        return self.active_height if self.active_height is not None else self.height


def _centiseconds(seconds: float) -> int:
    # Go through whole milliseconds and round half up, so 1.005 s is 1.01 and
    # not 1.00 from binary float error in round(x * 100).
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    return (milliseconds + 5) // 10


def _ass_timing(start: float, end: float) -> str:
    """ASS only has centiseconds; keep sub-10 ms cues at least 1 cs long."""
    start_cs = _centiseconds(start)
    end_cs = max(_centiseconds(end), start_cs + 1)
    return f"{_ass_time_cs(start_cs)},{_ass_time_cs(end_cs)}"


def _ass_time(seconds: float) -> str:
    return _ass_time_cs(_centiseconds(seconds))


def _ass_time_cs(total_cs: int) -> str:
    hours, remainder = divmod(total_cs, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _plain_subtitle_text(value: str) -> str:
    value = re.sub(
        r"</?(?:i|b|u|font)(?:\s+[^>]*)?>",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return html.unescape(value).strip()


# libass has no "\\" escape; a following word joiner keeps a literal backslash
# from being read as the start of an override such as \N or \h.
ASS_LITERAL_BACKSLASH = "\\\u2060"


def _srt_clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def ass_escape_text(value: str) -> str:
    escaped_lines: list[str] = []
    normalized = _plain_subtitle_text(value).replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        escaped = (
            line.replace("\\", ASS_LITERAL_BACKSLASH)
            .replace("{", r"\{")
            .replace("}", r"\}")
        )
        escaped_lines.append(escaped.strip())
    return r"\N".join(escaped_lines)


def _validate_tracks(
    source_segments: Iterable[SubtitleSegment],
    chinese_segments: Iterable[SubtitleSegment],
) -> tuple[list[SubtitleSegment], list[SubtitleSegment]]:
    source = list(source_segments)
    chinese = list(chinese_segments)
    if not source:
        raise SubtitleBuildError("The source-language SRT contains no usable subtitle segments.")
    if not chinese:
        raise SubtitleBuildError("The Chinese SRT contains no usable subtitle segments.")
    pairing = track_pairing(source, chinese)
    divergence = pairing["first_divergence"]
    where = (
        f" The tracks first diverge at source cue {divergence['id']} ({_srt_clock(divergence['start'])})."
        if divergence
        else ""
    )
    if len(source) != len(chinese):
        raise SubtitleBuildError(
            "Subtitle count mismatch: "
            f"source has {len(source)} segments, Chinese has {len(chinese)}."
            + where
        )
    if pairing["drifted_ids"]:
        drifted = pairing["drifted_ids"]
        raise SubtitleBuildError(
            "Chinese subtitles are out of step with the source timeline at "
            f"{len(drifted)} cue(s): {', '.join(drifted[:8])}."
            + where
            + " A cue was probably removed in one place and added in another."
        )
    empty = [str(index) for index, segment in enumerate(chinese, start=1) if not segment.text.strip()]
    if empty:
        raise SubtitleBuildError(f"Chinese subtitle has empty segments: {', '.join(empty[:8])}")
    return source, chinese


def build_bilingual_ass_text(
    source_segments: Iterable[SubtitleSegment],
    chinese_segments: Iterable[SubtitleSegment],
    *,
    title: str = "CueFlow Bilingual Subtitles",
) -> str:
    source, chinese = _validate_tracks(source_segments, chinese_segments)
    safe_title = " ".join(title.replace("\r", " ").replace("\n", " ").split())
    header = f"""[Script Info]
; Generated by CueFlow. Source-language timing is authoritative.
Title: {safe_title}
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
Collisions: Normal
YCbCr Matrix: TV.709
PlayResX: {ASS_PLAY_RES_X}
PlayResY: {ASS_PLAY_RES_Y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chinese,{CHINESE_FONT_NAME},{CHINESE_FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{CHINESE_OUTLINE},0,2,70,70,{CHINESE_MARGIN_V},1
Style: Source,{SOURCE_FONT_NAME},{SOURCE_FONT_SIZE},&H00F0F0F0,&H00F0F0F0,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{SOURCE_OUTLINE},0,2,70,70,{SOURCE_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for source_segment, chinese_segment in zip(source, chinese):
        timing = _ass_timing(source_segment.start, source_segment.end)
        events.append(
            "Dialogue: 1,"
            f"{timing},Chinese,,0,0,0,,{ass_escape_text(chinese_segment.text)}"
        )
        events.append(
            "Dialogue: 0,"
            f"{timing},Source,,0,0,0,,{ass_escape_text(source_segment.text)}"
        )
    return header + "\n".join(events) + "\n"


def _round_positive(value: float) -> int:
    return int(value + 0.5)


def _dialogue_fields(line: str) -> list[str] | None:
    if not line.startswith("Dialogue:"):
        return None
    fields = line[len("Dialogue:") :].lstrip().split(",", 9)
    return fields if len(fields) == 10 else None


def _ass_text_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "{" and "}" in value[index + 1 :]:
            end = value.index("}", index + 1) + 1
            tokens.append(value[index:end])
            index = end
            continue
        if value[index] == "\\" and index + 1 < len(value):
            tokens.append(value[index : index + 2])
            index += 2
            continue
        tokens.append(value[index])
        index += 1
    return tokens


def _ass_token_character(token: str) -> str:
    if token.startswith("{"):
        return ""
    if token == ASS_LITERAL_BACKSLASH:
        return "\\"
    if token.startswith("\\") and len(token) == 2:
        return " " if token == r"\h" else token[1]
    return token


def _ass_token_columns(token: str) -> int:
    character = _ass_token_character(token)
    if not character or unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _wrap_ass_line_for_columns(value: str, max_columns: int) -> list[str]:
    break_after = frozenset("，。！？；：、,.!?;:")
    closing_punctuation = frozenset("，。！？；：、）》】”’）〕］｝,.!?;:")
    wrapped: list[str] = []
    current: list[str] = []
    current_columns = 0

    for token in _ass_text_tokens(value):
        columns = _ass_token_columns(token)
        character = _ass_token_character(token)
        if current and columns and current_columns + columns > max_columns:
            if character in closing_punctuation and current_columns + columns <= max_columns + 2:
                current.append(token)
                current_columns += columns
                continue

            running_columns = 0
            break_index: int | None = None
            minimum_break = max_columns * 0.55
            for candidate_index, candidate in enumerate(current):
                running_columns += _ass_token_columns(candidate)
                candidate_character = _ass_token_character(candidate)
                if (
                    running_columns >= minimum_break
                    and (candidate_character.isspace() or candidate_character in break_after)
                ):
                    break_index = candidate_index + 1

            if break_index is None:
                wrapped.append("".join(current).rstrip())
                current = []
            else:
                wrapped.append("".join(current[:break_index]).rstrip())
                current = current[break_index:]
                while current and _ass_token_character(current[0]).isspace():
                    current.pop(0)
            current_columns = sum(_ass_token_columns(item) for item in current)

        if not current and character.isspace():
            continue
        current.append(token)
        current_columns += columns

    if current or not wrapped:
        wrapped.append("".join(current).rstrip())
    return wrapped


def _wrap_ass_cjk_text(value: str, max_columns: int) -> str:
    rendered_lines: list[str] = []
    for manual_line in re.split(r"(?<!\\)\\N", value):
        rendered_lines.extend(_wrap_ass_line_for_columns(manual_line, max_columns))
    return r"\N".join(rendered_lines)


def adapt_bilingual_ass_for_render(
    ass_text: str,
    *,
    frame_width: int,
    frame_height: int,
    active_x: int = 0,
    active_y: int = 0,
    active_width: int | None = None,
    active_height: int | None = None,
) -> str:
    """Create a target-aware render copy without changing the downloadable ASS.

    CueFlow's editable ASS intentionally keeps Chinese and source tracks separate.
    Rendering them as independent bottom-anchored events can overlap after either
    track wraps. This render-only form pairs matching CueFlow events into one
    libass text block so line stacking is calculated by the renderer itself.
    """
    if frame_width <= 0 or frame_height <= 0:
        raise VideoBurnError("Video dimensions must be positive before adapting ASS layout.")

    content_width = active_width if active_width is not None else frame_width
    content_height = active_height if active_height is not None else frame_height
    if (
        active_x < 0
        or active_y < 0
        or content_width <= 0
        or content_height <= 0
        or active_x + content_width > frame_width
        or active_y + content_height > frame_height
    ):
        raise VideoBurnError("Detected active-picture bounds are outside the video frame.")

    lines = ass_text.splitlines()
    grouped: dict[tuple[str, str], dict[str, list[tuple[int, list[str]]]]] = {}
    for index, line in enumerate(lines):
        fields = _dialogue_fields(line)
        if fields is None:
            continue
        style = fields[3].strip()
        if style not in {"Chinese", "Source"}:
            continue
        key = (fields[1].strip(), fields[2].strip())
        tracks = grouped.setdefault(key, {"Chinese": [], "Source": []})
        tracks[style].append((index, fields))

    if not grouped:
        return ass_text
    if any(
        not tracks["Chinese"] or len(tracks["Chinese"]) != len(tracks["Source"])
        for tracks in grouped.values()
    ):
        return ass_text

    play_res_x = max(2, _round_positive(ASS_PLAY_RES_Y * frame_width / frame_height))
    horizontal_inset = _round_positive(content_width * ASS_SAFE_HORIZONTAL_RATIO)
    margin_left_px = active_x + horizontal_inset
    margin_right_px = frame_width - (active_x + content_width) + horizontal_inset
    margin_bottom_px = (
        frame_height
        - (active_y + content_height)
        + content_height * ASS_SAFE_BOTTOM_RATIO
    )
    margin_left = _round_positive(margin_left_px * play_res_x / frame_width)
    margin_right = _round_positive(margin_right_px * play_res_x / frame_width)
    margin_bottom = _round_positive(margin_bottom_px * ASS_PLAY_RES_Y / frame_height)
    available_width = max(1, play_res_x - margin_left - margin_right)
    chinese_max_columns = max(
        16,
        _round_positive(
            available_width / CHINESE_FONT_SIZE * 2 / ASS_CJK_ADVANCE_RATIO
        ),
    )

    source_transition = (
        rf"\N{{\fs{ASS_LANGUAGE_GAP}\bord0\shad0\alpha&HFF&}}\h"
        rf"\N{{\alpha&H00&\fn{SOURCE_FONT_NAME}\fs{SOURCE_FONT_SIZE}"
        rf"\1c&HF0F0F0&\3c&H000000&\bord{SOURCE_OUTLINE}\shad0}}"
    )
    replacements: dict[int, str] = {}
    skipped: set[int] = set()
    for tracks in grouped.values():
        for chinese_event, source_event in zip(tracks["Chinese"], tracks["Source"]):
            chinese_index, chinese_fields = chinese_event
            source_index, source_fields = source_event
            merged = source_fields.copy()
            merged[0] = "0"
            merged[3] = "Bilingual"
            merged[5:8] = ["0", "0", "0"]
            merged[8] = ""
            wrapped_chinese = _wrap_ass_cjk_text(chinese_fields[9], chinese_max_columns)
            merged[9] = wrapped_chinese + source_transition + source_fields[9]
            replacement_index = min(chinese_index, source_index)
            replacements[replacement_index] = "Dialogue: " + ",".join(merged)
            skipped.update({chinese_index, source_index})

    rebuilt: list[str] = []
    for index, line in enumerate(lines):
        if index in replacements:
            rebuilt.append(replacements[index])
        elif index not in skipped:
            rebuilt.append(line)

    bilingual_style = (
        f"Style: Bilingual,{CHINESE_FONT_NAME},{CHINESE_FONT_SIZE},"
        "&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{CHINESE_OUTLINE},0,2,"
        f"{margin_left},{margin_right},{margin_bottom},1"
    )
    adapted: list[str] = []
    style_inserted = False
    for line in rebuilt:
        stripped = line.strip()
        if stripped.startswith("PlayResX:"):
            adapted.append(f"PlayResX: {play_res_x}")
            continue
        if stripped.startswith("PlayResY:"):
            adapted.append(f"PlayResY: {ASS_PLAY_RES_Y}")
            continue
        if stripped.startswith("Style: Bilingual,"):
            continue
        if stripped == "[Events]" and not style_inserted:
            adapted.extend([bilingual_style, ""])
            style_inserted = True
        adapted.append(line)

    if not style_inserted:
        raise VideoBurnError("ASS subtitle is missing an [Events] section.")
    return "\n".join(adapted) + ("\n" if ass_text.endswith(("\n", "\r")) else "")


def generate_bilingual_ass(
    source_srt: str | Path,
    chinese_srt: str | Path,
    output_path: str | Path,
    *,
    title: str | None = None,
) -> int:
    source_path = Path(source_srt).expanduser()
    chinese_path = Path(chinese_srt).expanduser()
    if not source_path.is_file():
        raise SubtitleBuildError(f"Source-language SRT was not found: {source_path}")
    if not chinese_path.is_file():
        raise SubtitleBuildError(f"Chinese SRT was not found: {chinese_path}")
    source = parse_srt_file(source_path)
    chinese = parse_srt_file(chinese_path)
    ass_text = build_bilingual_ass_text(
        source,
        chinese,
        title=title or f"{source_path.stem} + {chinese_path.stem}",
    )
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(ass_text, encoding="utf-8")
    return len(source)


def find_ass_ffmpeg(configured: str | Path | None = None) -> Path:
    ffmpeg, _tried = find_ffmpeg(configured, accept=has_ass_filter)
    if ffmpeg is None:
        raise VideoBurnError(
            "An FFmpeg build with the libass `ass` filter is required. "
            "On Apple Silicon macOS install it with `brew install ffmpeg-full`. "
            "CueFlow uses /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg without changing your PATH."
        )
    return ffmpeg


def _ffprobe_path(ffmpeg: Path) -> Path:
    ffprobe = ffmpeg.with_name("ffprobe")
    if not ffprobe.is_file():
        found = shutil.which("ffprobe")
        if not found:
            raise VideoBurnError(
                "FFprobe is required to inspect video dimensions before subtitle rendering."
            )
        ffprobe = Path(found)
    return ffprobe


def _stream_rotation(stream: dict) -> int:
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return round(float(side_data["rotation"])) % 360
            except (TypeError, ValueError):
                return 0
    try:
        return round(float((stream.get("tags") or {}).get("rotate", 0))) % 360
    except (TypeError, ValueError):
        return 0


def _probe_video_info(ffmpeg: Path, video: Path) -> tuple[float | None, _VideoGeometry]:
    ffprobe = _ffprobe_path(ffmpeg)
    try:
        completed = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration:stream=width,height:stream_side_data=rotation:stream_tags=rotate",
                "-of",
                "json",
                str(video),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VideoBurnError(f"FFprobe could not inspect the video: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise VideoBurnError(f"FFprobe could not inspect the video: {detail[-2000:]}")
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration_value = float(payload.get("format", {}).get("duration", 0))
        rotation = _stream_rotation(stream)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise VideoBurnError("FFprobe returned incomplete video geometry.") from error
    if width <= 0 or height <= 0:
        raise VideoBurnError("FFprobe returned invalid video dimensions.")
    if rotation % 180 == 90:
        # FFmpeg autorotates before the filter graph, so libass and cropdetect
        # see the displayed (portrait) frame, not the stored one.
        width, height = height, width
    duration = duration_value if duration_value > 0 else None
    return duration, _VideoGeometry(width, height)


def _detect_active_picture(
    ffmpeg: Path,
    video: Path,
    duration: float | None,
    geometry: _VideoGeometry,
) -> _VideoGeometry:
    sample_times = [0.0]
    if duration and duration > 3:
        sample_times = sorted(
            {
                round(min(max(duration * ratio, 0.0), max(0.0, duration - 1.0)), 3)
                for ratio in (0.1, 0.5, 0.9)
            }
        )

    candidates: list[tuple[int, int, int, int]] = []
    pattern = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
    for sample_time in sample_times:
        try:
            completed = subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-ss",
                    f"{sample_time:.3f}",
                    "-i",
                    str(video),
                    "-an",
                    "-sn",
                    "-vf",
                    "cropdetect=limit=24:round=2:reset=0",
                    "-frames:v",
                    "24",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        matches = pattern.findall(f"{completed.stdout}\n{completed.stderr}")
        if not matches:
            continue
        width, height, x, y = (int(value) for value in matches[-1])
        if (
            width > 0
            and height > 0
            and x >= 0
            and y >= 0
            and x + width <= geometry.width
            and y + height <= geometry.height
            and width * height >= geometry.width * geometry.height * 0.45
        ):
            candidates.append((width, height, x, y))

    if not candidates:
        return geometry
    width = _round_positive(median(item[0] for item in candidates))
    height = _round_positive(median(item[1] for item in candidates))
    x = _round_positive(median(item[2] for item in candidates))
    y = _round_positive(median(item[3] for item in candidates))
    if width / geometry.width >= 0.96 and height / geometry.height >= 0.96:
        return geometry
    return _VideoGeometry(geometry.width, geometry.height, x, y, width, height)


def _geometry_for_encoding_profile(
    geometry: _VideoGeometry,
    profile: str,
) -> _VideoGeometry:
    if profile == "hevc-source":
        return geometry
    scale = min(1.0, 1920 / geometry.width, 1080 / geometry.height)
    width = max(2, int(geometry.width * scale) // 2 * 2)
    height = max(2, int(geometry.height * scale) // 2 * 2)
    scale_x = width / geometry.width
    scale_y = height / geometry.height
    return _VideoGeometry(
        width,
        height,
        _round_positive(geometry.active_x * scale_x),
        _round_positive(geometry.active_y * scale_y),
        _round_positive(geometry.content_width * scale_x),
        _round_positive(geometry.content_height * scale_y),
    )


def _ensure_fonts(fonts_dir: Path) -> None:
    missing = [name for name in (SOURCE_HAN_FONT, MULISH_FONT) if not (fonts_dir / name).is_file()]
    if missing:
        raise VideoBurnError(
            f"Bundled subtitle fonts are missing from {fonts_dir}: {', '.join(missing)}"
        )


def render_bilingual_preview(
    output_path: str | Path,
    *,
    source_text: str = "How Quebec became North America's urban outlier",
    chinese_text: str = "魁北克为何成为北美的城市异类",
    ffmpeg_path: str | Path | None = None,
    fonts_dir: str | Path = DEFAULT_FONTS_DIR,
) -> Path:
    output = Path(output_path).expanduser().absolute()
    fonts = Path(fonts_dir).expanduser().absolute()
    _ensure_fonts(fonts)
    ffmpeg = find_ass_ffmpeg(ffmpeg_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    ass_text = build_bilingual_ass_text(
        [SubtitleSegment("0001", 0.0, 5.0, source_text, [])],
        [SubtitleSegment("0001", 0.0, 5.0, chinese_text, [])],
        title="CueFlow libass preview",
    )
    with tempfile.TemporaryDirectory(prefix="cueflow-preview-") as temp_value:
        temp = Path(temp_value)
        (temp / "subtitle.ass").write_text(ass_text, encoding="utf-8")
        (temp / "fonts").symlink_to(fonts, target_is_directory=True)
        preview_filter = (
            "ass=subtitle.ass:fontsdir=fonts,"
            f"crop={ASS_PLAY_RES_X}:{ASS_PREVIEW_CROP_HEIGHT}:0:{ASS_PREVIEW_CROP_Y},"
            f"scale={ASS_PREVIEW_WIDTH}:{ASS_PREVIEW_HEIGHT}"
        )
        try:
            completed = subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=0x10252a:s={ASS_PLAY_RES_X}x{ASS_PLAY_RES_Y}:d=1",
                    "-vf",
                    preview_filter,
                    "-frames:v",
                    "1",
                    str(output),
                ],
                cwd=temp,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            output.unlink(missing_ok=True)
            raise VideoBurnError(f"FFmpeg could not render the ASS preview: {error}") from error
    if completed.returncode != 0:
        output.unlink(missing_ok=True)
        detail = (completed.stderr or completed.stdout).strip()
        raise VideoBurnError(f"FFmpeg could not render the ASS preview: {detail[-2000:]}")
    return output


def video_encoding_options(profile: str) -> tuple[str, list[str]]:
    aliases = {"quality": "hevc", "fast": "h264"}
    canonical = aliases.get(profile, profile)
    if canonical in {"hevc-source", "hevc"}:
        return canonical, [
            "-c:v",
            "hevc_videotoolbox",
            "-q:v",
            "65",
            "-tag:v",
            "hvc1",
        ]
    if canonical == "h264":
        return canonical, [
            "-c:v",
            "h264_videotoolbox",
            "-q:v",
            "65",
        ]
    raise VideoBurnError("Encoding profile must be `hevc-source`, `hevc`, or `h264`.")


def burn_ass_into_video(
    video_path: str | Path,
    ass_path: str | Path,
    output_path: str | Path,
    *,
    profile: str = "hevc-source",
    ffmpeg_path: str | Path | None = None,
    fonts_dir: str | Path = DEFAULT_FONTS_DIR,
    progress: ProgressCallback | None = None,
) -> Path:
    video = Path(video_path).expanduser().absolute()
    ass = Path(ass_path).expanduser().absolute()
    output = Path(output_path).expanduser().absolute()
    fonts = Path(fonts_dir).expanduser().absolute()
    if not video.is_file():
        raise VideoBurnError(f"Video file was not found: {video}")
    if not ass.is_file():
        raise VideoBurnError(f"ASS subtitle file was not found: {ass}")
    canonical_profile, video_options = video_encoding_options(profile)
    video_filter = VIDEO_FILTER_SOURCE if canonical_profile == "hevc-source" else VIDEO_FILTER_1080P
    _ensure_fonts(fonts)
    ffmpeg = find_ass_ffmpeg(ffmpeg_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = progress or (lambda _stage, _percent, _message: None)
    report("layout", 2, "Detecting the video's active picture and subtitle safe area")
    duration, source_geometry = _probe_video_info(ffmpeg, video)
    detected_geometry = _detect_active_picture(ffmpeg, video, duration, source_geometry)
    render_geometry = _geometry_for_encoding_profile(detected_geometry, canonical_profile)
    report(
        "encoding",
        3,
        (
            "Preparing target-aware libass layout "
            f"({render_geometry.content_width}x{render_geometry.content_height} active picture)"
        ),
    )

    with tempfile.TemporaryDirectory(prefix="cueflow-burn-") as temp_value:
        temp = Path(temp_value)
        try:
            ass_text = ass.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                ass_text = ass.read_text(encoding="utf-16")
            except UnicodeError as error:
                raise VideoBurnError("ASS subtitle must be encoded as UTF-8 or UTF-16.") from error
        render_ass = adapt_bilingual_ass_for_render(
            ass_text,
            frame_width=render_geometry.width,
            frame_height=render_geometry.height,
            active_x=render_geometry.active_x,
            active_y=render_geometry.active_y,
            active_width=render_geometry.content_width,
            active_height=render_geometry.content_height,
        )
        (temp / "subtitle.ass").write_text(render_ass, encoding="utf-8")
        (temp / "fonts").symlink_to(fonts, target_is_directory=True)
        command = [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            video_filter,
            *video_options,
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-map_metadata",
            "0",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output),
        ]
        try:
            process = popen(
                command,
                cwd=temp,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise VideoBurnError(f"FFmpeg could not start: {error}") from error

        assert process.stdout is not None
        last_percent = 3
        output_tail: deque[str] = deque(maxlen=200)
        try:
            for raw_line in process.stdout:
                output_tail.append(raw_line)
                key, separator, value = raw_line.strip().partition("=")
                if separator and key == "out_time_us" and duration:
                    try:
                        elapsed = int(value) / 1_000_000
                    except ValueError:
                        continue
                    percent = min(98, max(3, int(elapsed / duration * 95) + 3))
                    if percent >= last_percent + 2:
                        last_percent = percent
                        report("encoding", percent, f"Burning subtitles into video ({percent}%)")
            return_code = process.wait()
        except BaseException:
            # A failing progress callback or interrupt must not leave FFmpeg
            # running with a full pipe and a half-written MP4.
            kill_tree(process)
            output.unlink(missing_ok=True)
            raise
        finally:
            process.stdout.close()
            release(process)
        if return_code != 0:
            output.unlink(missing_ok=True)
            detail = "".join(output_tail).strip()
            raise VideoBurnError(
                f"FFmpeg subtitle burn failed with exit code {return_code}: {detail[-3000:]}"
            )

    report(
        "complete",
        100,
        (
            "Bilingual subtitles burned into original-resolution HEVC video"
            if canonical_profile == "hevc-source"
            else f"Bilingual subtitles burned into 1080p {canonical_profile.upper()} video"
        ),
    )
    return output
