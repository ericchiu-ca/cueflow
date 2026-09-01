from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .bilingual import DEFAULT_FONTS_DIR, burn_ass_into_video, generate_bilingual_ass
from .core import (
    SubtitleSegment,
    build_bilingual_srt_text,
    build_srt_text,
    build_translation_input_text,
    load_master_json,
    normalize_segments,
    parse_srt_file,
    parse_translation_file,
    save_master_json,
)
from .qc import QCIssue, run_qc
from .translation import CODEX_MODEL_CHOICES, run_translation_project
from .yt_workflow import (
    download_audio_m4a,
    download_subtitles,
    detect_english_subtitles,
    ensure_command,
    transcribe_with_faster_whisper,
)


def _ensure_project(project_path: Path) -> Path:
    project_path.mkdir(parents=True, exist_ok=True)
    return project_path


def _write_outputs(project: Path, segments: list[SubtitleSegment]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    save_master_json(project / "master.json", segments)
    (project / "en.srt").write_text(build_srt_text(segments), encoding="utf-8")
    (project / "translation_input.txt").write_text(build_translation_input_text(segments), encoding="utf-8")


def _build_from_translation(project: Path, translation_file: Path) -> None:
    translations = parse_translation_file(translation_file)
    project_master = project / "master.json"
    if not project_master.exists():
        raise SystemExit(f"master.json not found in {project}")

    segments = load_master_json(project_master)
    output_dir = project / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "zh.srt").write_text(build_srt_text(segments, translations), encoding="utf-8")
    (output_dir / "bilingual.srt").write_text(build_bilingual_srt_text(segments, translations), encoding="utf-8")


def cmd_prepare(args: argparse.Namespace) -> int:
    project = _ensure_project(Path(args.output).expanduser())
    ensure_command("yt-dlp")

    manual, auto = detect_english_subtitles(args.url)
    segments: list[SubtitleSegment] = []

    if manual:
        with tempfile.TemporaryDirectory(prefix="subflow-yt-") as tmpdir:
            tmp = Path(tmpdir)
            subtitle_file = download_subtitles(args.url, tmp, manual, use_auto=False)
            segments = parse_srt_file(subtitle_file)
    elif auto:
        with tempfile.TemporaryDirectory(prefix="subflow-yt-") as tmpdir:
            tmp = Path(tmpdir)
            subtitle_file = download_subtitles(args.url, tmp, auto, use_auto=True)
            segments = parse_srt_file(subtitle_file)
    else:
        audio_path = project / "source.m4a"
        download_audio_m4a(args.url, audio_path)
        segments = transcribe_with_faster_whisper(audio_path, model_name=args.whisper_model)

    segments = normalize_segments(segments)
    if not segments:
        raise SystemExit("No usable subtitle segments found.")

    _write_outputs(project, segments)

    print(f"Prepared project at: {project}")
    print("Generated:")
    print(f"- master.json")
    print(f"- en.srt")
    print(f"- translation_input.txt")
    print(f"- source.m4a (only for ASR fallback)")
    return 0


def cmd_import_translation(args: argparse.Namespace) -> int:
    translation_path = Path(args.translation_file).expanduser()
    if not translation_path.exists():
        raise SystemExit(f"Translation file not found: {translation_path}")

    project = translation_path.parent
    _build_from_translation(project, translation_path)

    # Keep a canonical translation filename for later `build`/`qc` commands.
    canonical = project / "translation_zh.txt"
    if translation_path.name != canonical.name:
        canonical.write_text(translation_path.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Generated output subtitles in {project / 'output'}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser()
    translation_path = project / "translation_zh.txt"
    if not translation_path.exists():
        raise SystemExit(
            f"Missing translation file: {translation_path}. "
            "Run `import-translation` first or place translation_zh.txt in the project."
        )

    _build_from_translation(project, translation_path)
    print(f"Built zh.srt and bilingual.srt in {project / 'output'}")
    return 0


def _issue_to_line(issue: QCIssue) -> str:
    return f"[{issue.level}] {issue.code}: {issue.message}"


def cmd_qc(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser()
    master_path = project / "master.json"
    if not master_path.exists():
        raise SystemExit(f"master.json not found: {master_path}")

    segments = load_master_json(master_path)
    translation_path = project / "translation_zh.txt"
    translations = parse_translation_file(translation_path) if translation_path.exists() else None

    issues = run_qc(
        segments=segments,
        translations=translations,
        min_duration=args.min_duration,
        max_chinese_chars=args.max_chinese_chars,
    )
    if not issues:
        print("QC: PASS")
        return 0

    print("QC: FOUND ISSUES")
    for issue in issues:
        print(_issue_to_line(issue))

    if any(issue.level == "ERROR" for issue in issues):
        return 1
    return 0


def cmd_transcribe_file(args: argparse.Namespace) -> int:
    from .transcription import transcribe_media

    result = transcribe_media(
        args.media_file,
        args.output,
        language=args.language,
        alignment_mode=args.alignment,
        model_path=args.model_path,
        helper_path=args.mlx_helper,
        whisperx_python=args.whisperx_python,
        progress=lambda _stage, percent, message: print(f"[{percent:3d}%] {message}"),
    )
    print(f"Generated SRT: {result.srt_path}")
    print(f"Master data: {result.master_path}")
    print(f"Alignment: {result.alignment}")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    return 0


def cmd_align_project(args: argparse.Namespace) -> int:
    from .transcription import align_existing_project

    result = align_existing_project(
        args.project,
        language=args.language,
        whisperx_python=args.whisperx_python,
        progress=lambda _stage, percent, message: print(f"[{percent:3d}%] {message}"),
    )
    print(f"Generated aligned SRT: {result.srt_path}")
    print(f"Aligned master data: {result.master_path}")
    return 0


def cmd_translate_srt(args: argparse.Namespace) -> int:
    source = Path(args.source_srt).expanduser()
    if not source.is_file():
        raise ValueError(f"Source SRT was not found: {source}")
    segments = parse_srt_file(source)
    artifacts = run_translation_project(
        Path(args.output).expanduser(),
        segments,
        source_language=args.language,
        model=args.model,
        provider_name=args.provider,
        source_filename=source.name,
        progress=lambda percent, message: print(f"[{percent:3d}%] {message}"),
    )
    print(f"Generated Chinese SRT: {artifacts.zh_srt_path}")
    print(f"Stable-ID translation: {artifacts.translation_text_path}")
    print(f"Provider: {artifacts.provider}; model: {artifacts.model}")
    for warning in artifacts.warnings:
        print(f"Warning: {warning}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    serve(
        host=args.host,
        port=args.port,
        output_root=args.output,
        alignment_mode=args.alignment,
        model_path=args.model_path,
        helper_path=args.mlx_helper,
        whisperx_python=args.whisperx_python,
        open_browser=not args.no_open,
        max_upload_gb=args.max_upload_gb,
    )
    return 0


def cmd_build_ass(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser()
    count = generate_bilingual_ass(
        args.source_srt,
        args.chinese_srt,
        output,
        title=args.title,
    )
    print(f"Generated bilingual ASS: {output}")
    print(f"Paired segments: {count}")
    return 0


def cmd_burn(args: argparse.Namespace) -> int:
    video = Path(args.video).expanduser()
    output = Path(args.output).expanduser() if args.output else video.with_name(f"{video.stem}.bilingual.mp4")
    result = burn_ass_into_video(
        video,
        args.ass,
        output,
        profile=args.profile,
        ffmpeg_path=args.ffmpeg,
        fonts_dir=args.fonts_dir,
        progress=lambda _stage, percent, message: print(f"[{percent:3d}%] {message}"),
    )
    print(f"Generated bilingual video: {result}")
    return 0


def _add_local_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-path",
        help="Local MLX Whisper model (defaults to FrameLedger's model)",
    )
    parser.add_argument(
        "--mlx-helper",
        help="FrameLedger MLX Whisper helper executable",
    )
    parser.add_argument(
        "--whisperx-python",
        help="Python executable from an environment containing WhisperX",
    )
    parser.add_argument(
        "--alignment",
        choices=("auto", "native", "whisperx"),
        default="auto",
        help="auto uses WhisperX when configured, otherwise MLX word timestamps",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subflow",
        description="Local transcription, translation, review, ASS, and video workflow",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Download subtitles / transcribe and generate master + en.srt")
    prepare.add_argument("url", help="YouTube URL")
    prepare.add_argument("--output", required=True, help="Output project directory")
    prepare.add_argument("--whisper-model", default="base", help="faster-whisper model size")
    prepare.set_defaults(func=cmd_prepare)

    imp = sub.add_parser("import-translation", help="Import translated text by ID and generate translated SRTs")
    imp.add_argument("translation_file", help="translation_zh.txt like file with [0001] blocks")
    imp.set_defaults(func=cmd_import_translation)

    build = sub.add_parser("build", help="Build zh.srt and bilingual.srt from translation_zh.txt")
    build.add_argument("project", help="Project directory")
    build.set_defaults(func=cmd_build)

    qc = sub.add_parser("qc", help="Run subtitle quality checks")
    qc.add_argument("project", help="Project directory")
    qc.add_argument("--min-duration", type=float, default=1.0, help="Min segment duration seconds")
    qc.add_argument("--max-chinese-chars", type=int, default=45, help="Max Chinese subtitle characters")
    qc.set_defaults(func=cmd_qc)

    transcribe = sub.add_parser(
        "transcribe-file",
        help="Transcribe a local video/audio file with FrameLedger's MLX Whisper model",
    )
    transcribe.add_argument("media_file", help="Local video or audio file")
    transcribe.add_argument("--output", required=True, help="Output project directory")
    transcribe.add_argument(
        "--language",
        choices=("en", "fr-CA", "mixed"),
        default="en",
        help="Spoken language",
    )
    _add_local_model_options(transcribe)
    transcribe.set_defaults(func=cmd_transcribe_file)

    align = sub.add_parser(
        "align-project",
        help="Apply WhisperX to an existing local-video master.json without retranscribing",
    )
    align.add_argument("project", help="Existing Subflow project directory")
    align.add_argument(
        "--language",
        choices=("en", "fr-CA"),
        help="Override the language stored in master.json",
    )
    align.add_argument(
        "--whisperx-python",
        help="Python executable from an environment containing WhisperX",
    )
    align.set_defaults(func=cmd_align_project)

    translate = sub.add_parser(
        "translate-srt",
        help="Translate an English or Canadian French SRT to Chinese with Codex",
    )
    translate.add_argument("source_srt", help="English or Canadian French SRT")
    translate.add_argument("--output", required=True, help="Translation project directory")
    translate.add_argument(
        "--language",
        choices=("en", "fr-CA"),
        default="en",
        help="Source subtitle language",
    )
    translate.add_argument(
        "--model",
        choices=CODEX_MODEL_CHOICES,
        default="gpt-5.6-terra",
        help="Codex translation model",
    )
    translate.add_argument(
        "--provider",
        choices=("codex", "openai-api"),
        default="codex",
        help="Translation provider; openai-api is reserved but not enabled",
    )
    translate.set_defaults(func=cmd_translate_srt)

    web = sub.add_parser("web", help="Launch the local drag-and-drop subtitle UI")
    web.add_argument("--host", default="127.0.0.1", help="Loopback bind address only")
    web.add_argument("--port", type=int, default=8765, help="Local port")
    web.add_argument("--output", default="projects/local", help="Project output root")
    web.add_argument("--max-upload-gb", type=float, default=20.0, help="Upload size limit")
    web.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    _add_local_model_options(web)
    web.set_defaults(func=cmd_web)

    ass = sub.add_parser("build-ass", help="Pair source-language and Chinese SRT files into a styled ASS")
    ass.add_argument("source_srt", help="English or French source SRT; its timeline is authoritative")
    ass.add_argument("chinese_srt", help="Chinese SRT with the same number of segments")
    ass.add_argument("--output", required=True, help="Output .ass path")
    ass.add_argument("--title", help="Optional ASS title")
    ass.set_defaults(func=cmd_build_ass)

    burn = sub.add_parser("burn", help="Hard-burn an ASS subtitle into a new MP4")
    burn.add_argument("video", help="Input video path")
    burn.add_argument("ass", help="Input ASS subtitle path")
    burn.add_argument("--output", help="Output MP4 path (defaults beside the source video)")
    burn.add_argument(
        "--profile",
        choices=("hevc-source", "hevc"),
        default="hevc-source",
        help="HEVC VideoToolbox output: preserve source resolution or scale down to 1080p",
    )
    burn.add_argument("--ffmpeg", help="FFmpeg executable containing the libass filter")
    burn.add_argument(
        "--fonts-dir",
        default=str(DEFAULT_FONTS_DIR),
        help="Directory containing the bundled subtitle fonts",
    )
    burn.set_defaults(func=cmd_burn)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
