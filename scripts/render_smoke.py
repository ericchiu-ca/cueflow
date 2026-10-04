"""Real FFmpeg/libass gate with generated media; no models or network calls."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from subflow.bilingual import (
    DEFAULT_FONTS_DIR,
    VIDEO_FILTER_SOURCE,
    adapt_bilingual_ass_for_render,
    build_bilingual_ass_text,
    burn_ass_into_video,
    find_ass_ffmpeg,
    render_bilingual_preview,
)
from subflow.core import parse_srt_text
from subflow.provenance import command_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--native-burn",
        action="store_true",
        help="Also exercise production HEVC VideoToolbox burn on Apple Silicon",
    )
    args = parser.parse_args()
    output = Path(args.output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ass_ffmpeg()
    source = parse_srt_text(
        "1\n00:00:00,000 --> 00:00:01,500\nReal libass {test} \\ path\n"
    )
    chinese = parse_srt_text(
        "1\n00:00:00,000 --> 00:00:01,500\n真实渲染测试：中文与原文双轨\n"
    )
    ass_text = build_bilingual_ass_text(source, chinese)
    results = []
    render_bilingual_preview(
        output / "preview.png", source_text=source[0].text, chinese_text=chinese[0].text
    )
    for width, height in ((960, 540), (720, 540)):
        case = output / f"case-{width}x{height} '[:;]"
        case.mkdir(exist_ok=True)
        video, ass, burned = (
            case / "source '[:;].mp4",
            case / "bilingual.ass",
            case / "rendered.mp4",
        )
        ass.write_text(ass_text, encoding="utf-8")
        subprocess.run(
            [
                str(ffmpeg),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=black:s={width}x{height}:d=1.6:r=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
            timeout=30,
        )
        with tempfile.TemporaryDirectory(prefix="cueflow-render-smoke-") as temp:
            work = Path(temp)
            (work / "subtitle.ass").write_text(
                adapt_bilingual_ass_for_render(
                    ass_text, frame_width=width, frame_height=height
                ),
                encoding="utf-8",
            )
            (work / "fonts").symlink_to(DEFAULT_FONTS_DIR, target_is_directory=True)
            subprocess.run(
                [
                    str(ffmpeg),
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(video),
                    "-vf",
                    VIDEO_FILTER_SOURCE,
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(burned),
                ],
                cwd=work,
                check=True,
                timeout=45,
            )
        frame = subprocess.run(
            [
                str(ffmpeg),
                "-v",
                "error",
                "-ss",
                "0.5",
                "-i",
                str(burned),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout
        assert len(frame) == width * height * 3, "Unexpected decoded dimensions"
        points = [
            (i % width, i // width)
            for i in range(width * height)
            if max(frame[i * 3 : i * 3 + 3]) > 100
        ]
        assert len(points) > 300, "No readable subtitle pixels rendered"
        bounds = [
            min(x for x, y in points),
            min(y for x, y in points),
            max(x for x, y in points),
            max(y for x, y in points),
        ]
        assert (
            bounds[0] > 0
            and bounds[2] < width - 1
            and bounds[1] > height / 2
            and bounds[3] < height - 1
        ), bounds
        subprocess.run(
            [
                str(ffmpeg),
                "-v",
                "error",
                "-y",
                "-ss",
                "0.5",
                "-i",
                str(burned),
                "-frames:v",
                "1",
                str(case / "frame.png"),
            ],
            check=True,
            timeout=15,
        )
        result = {
            "dimensions": [width, height],
            "subtitle_pixels": len(points),
            "bounds": bounds,
        }
        if args.native_burn:
            native = case / "production-hevc.mp4"
            burn_ass_into_video(video, ass, native)
            assert native.is_file() and native.stat().st_size > 1000
            assert native.with_suffix(".provenance.json").is_file()
            result["native_hevc"] = True
        results.append(result)
    report = {
        "ffmpeg": command_version(ffmpeg),
        "cases": results,
        "preview": "preview.png",
        "network_or_models": False,
    }
    (output / "render-smoke.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
