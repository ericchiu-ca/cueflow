from __future__ import annotations

import contextlib
import json
import sys
import wave
from pathlib import Path


PROTOCOL = "cueflow-vad-v1"


def _load_pcm16(path: Path):
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    if sample_width != 2:
        raise RuntimeError("VAD input must be 16-bit PCM WAV")

    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np

    samples = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _chunk_spans(chunks) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for item in chunks:
        if isinstance(item, dict):
            start = float(item["start"])
            end = float(item["end"])
        else:
            start = float(item.start)
            end = float(item.end)
        if end > start:
            spans.append((start, end))
    return sorted(spans)


def _split_and_merge(
    spans: list[tuple[float, float]], duration: float, chunk_size: float, padding: float
) -> list[dict[str, float]]:
    split: list[tuple[float, float]] = []
    for start, end in spans:
        cursor = start
        while end - cursor > chunk_size:
            split.append((cursor, cursor + chunk_size))
            cursor += chunk_size
        if end > cursor:
            split.append((cursor, end))

    merged: list[tuple[float, float]] = []
    for start, end in split:
        if not merged or end - merged[-1][0] > chunk_size:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    windows: list[dict[str, float]] = []
    for index, (start, end) in enumerate(merged, start=1):
        padded_start = max(0.0, start - padding)
        padded_end = min(duration, end + padding)
        windows.append(
            {
                "index": index,
                "start": round(padded_start, 3),
                "end": round(padded_end, 3),
                "duration": round(padded_end - padded_start, 3),
            }
        )
    return windows


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if request.get("protocol") != PROTOCOL:
            raise RuntimeError("Unsupported VAD protocol")
        audio_path = Path(str(request.get("audio_path", ""))).expanduser().resolve()
        if not audio_path.is_file():
            raise RuntimeError(f"VAD audio does not exist: {audio_path}")

        onset = float(request.get("onset", 0.5))
        offset = float(request.get("offset", 0.363))
        chunk_size = float(request.get("chunk_size", 28.0))
        padding = float(request.get("padding", 0.2))
        samples, sample_rate = _load_pcm16(audio_path)
        duration = len(samples) / float(sample_rate)

        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from whisperx.vads import Pyannote

            detector = Pyannote(
                device=torch.device("cpu"),
                token=None,
                vad_onset=onset,
                vad_offset=offset,
                chunk_size=chunk_size,
            )
            annotation = detector(
                {
                    "waveform": torch.from_numpy(samples).unsqueeze(0),
                    "sample_rate": sample_rate,
                }
            )
            chunks = Pyannote.merge_chunks(
                annotation,
                chunk_size=chunk_size,
                onset=onset,
                offset=offset,
            )

        json.dump(
            {
                "protocol": PROTOCOL,
                "duration": round(duration, 3),
                "windows": _split_and_merge(
                    _chunk_spans(chunks), duration, chunk_size, padding
                ),
            },
            sys.stdout,
            ensure_ascii=False,
        )
        return 0
    except Exception as exc:
        json.dump(
            {"protocol": PROTOCOL, "error_type": type(exc).__name__, "error": str(exc)},
            sys.stderr,
            ensure_ascii=False,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
