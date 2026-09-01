# CueFlow

**English summary** | [简体中文（主文档）](README.md)

> The Chinese README and `docs/` files are the canonical, maintained technical documentation. This English file is a concise usage summary.

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-1f6feb)
![Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-111111)
![License](https://img.shields.io/badge/License-MIT-2f855a)
![Local first](https://img.shields.io/badge/media-local--first-d97706)

**Turn video into a reviewable, translatable, production-ready bilingual subtitle workflow.**

CueFlow is a Python CLI and local web app for Apple Silicon macOS. It uses WhisperX/Pyannote VAD to create speech windows, transcribes them with local MLX Whisper large-v3-turbo, and selectively retries low-confidence windows with full large-v3. English, Canadian French, and per-window mixed English/French detection are supported before WhisperX word-level alignment. CueFlow also translates stable-ID subtitles through Codex, provides a dual-track review desk, generates styled bilingual ASS, and hard-burns it with HEVC VideoToolbox.

Each speech window receives a `0-100` score, detected language, selected model, and Turbo/large-v3 disagreement record in `master.json`; medium and low confidence windows automatically become review warnings. The full model defaults to `~/Documents/FrameLedger/.cache/models/whisper-large-v3-mlx` and can be overridden with `SUBFLOW_MLX_LARGE_MODEL`.

> Local-first media: video, audio, ASR, alignment, ASS generation, and encoding stay on the Mac. When Codex translation is explicitly used, the request includes subtitle text, stable IDs, start/end times, source language, and the selected model; video and audio are not uploaded.

## Workflow

```mermaid
flowchart LR
    A[Video / YouTube] --> B[MLX Whisper]
    B --> C[WhisperX alignment]
    C --> D[Codex Chinese translation]
    D --> E[Dual-track review]
    E --> F[Bilingual ASS]
    F --> G[HEVC VideoToolbox MP4]
```

1. **Transcribe**: prefer human YouTube English subtitles, then automatic subtitles; local media uses FrameLedger MLX Whisper with a faster-whisper fallback.
2. **Align**: WhisperX refines English and Canadian French word timing.
3. **Translate**: Codex returns schema-constrained `{id, text}` records; missing, duplicate, extra, and empty IDs are rejected.
4. **Review**: switch tracks, search, edit timing, delete, add, split, merge, and inspect QC warnings.
5. **Typeset**: generate bilingual ASS with Chinese above the source track, using CueFlow Han Sans SC and Mulish.
6. **Encode**: use `hevc_videotoolbox` at source resolution or scale down proportionally to 1080p.

`master.json` remains the authoritative data source. SRT and ASS are exchange formats.

## Quick start

### 1. Install system dependencies

```bash
brew install python@3.11 yt-dlp ffmpeg-full
```

`ffmpeg-full` includes the ASS/libass filter. CueFlow validates executables and prefers `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` without modifying the system `PATH`.

### 2. Install CueFlow

```bash
git clone https://github.com/ericchiu-ca/cueflow.git
cd cueflow
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[asr]"
```

### 3. Configure optional local models

CueFlow looks for the default FrameLedger model at:

```text
~/Documents/FrameLedger/.cache/models/whisper-large-v3-turbo
```

Override local tools explicitly when needed:

```bash
export SUBFLOW_MLX_MODEL="/path/to/whisper-large-v3-turbo"
export SUBFLOW_MLX_LARGE_MODEL="/path/to/whisper-large-v3-mlx"
export SUBFLOW_MLX_HELPER="/path/to/frameledger-asr-helper"
export SUBFLOW_WHISPERX_PYTHON="/path/to/whisperx/python"
export SUBFLOW_FFMPEG="/path/to/ffmpeg"
export SUBFLOW_CODEX="/path/to/codex"
```

The Web environment status and transcription path both read `SUBFLOW_MLX_MODEL`; an explicit `--model-path` takes priority.

WhisperX can live in a separate environment to avoid ML dependency conflicts:

```bash
python3.11 -m venv .venv-whisperx
.venv-whisperx/bin/python -m pip install -r requirements-whisperx.txt
```

### 4. Sign in to Codex and launch the UI

```bash
codex login
cueflow web
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). The server enforces a loopback bind and requires a per-process request token for every state-changing operation.

## Translation models

| Option | Intended use |
| --- | --- |
| `auto` | Let the active Codex configuration choose |
| `gpt-5.6-luna` | Fast, lower-cost first pass |
| `gpt-5.6-terra` | Default balance of quality and speed |
| `gpt-5.6-sol` | Heavier high-quality translation |

Each translation is an independent, ephemeral, read-only Codex task. It runs from an empty directory, cannot inspect the repository, and does not inherit this conversation. Model availability still depends on the signed-in account. The `openai-api` provider boundary is reserved, but it does not read an API key or make API requests.

## CLI examples

```bash
# YouTube subtitle-first workflow
cueflow prepare "YOUTUBE_URL" --output projects/example

# Local transcription with optional WhisperX alignment
cueflow transcribe-file input.mp4 \
  --output projects/local/example \
  --language en \
  --alignment auto

# Translate an existing SRT through Codex
cueflow translate-srt en.srt \
  --output projects/local/translation \
  --language en \
  --model gpt-5.6-terra

# Manual stable-ID translation workflow
cueflow import-translation projects/example/translation_zh.txt
cueflow build projects/example
cueflow qc projects/example

# Bilingual ASS and hard-burn
cueflow build-ass en.srt zh.srt --output output/bilingual.ass
cueflow burn input.mp4 output/bilingual.ass \
  --output output/input.bilingual.mp4 \
  --profile hevc-source
```

## Translation artifacts

```text
projects/local/translations/.../
├── source.srt
├── master.json
├── translation_input.txt
├── translation.result.json
├── translation_zh.txt
└── output/
    └── zh.srt
```

Other tasks live under `transcriptions/`, `reviews/`, `bilingual/`, `ass-assets/`, and `renders/`. The cleanup action removes only these managed categories and preserves all unrelated files in the configured output root.

## Editable bilingual ASS baseline

| Setting | Chinese | English / French |
| --- | --- | --- |
| Font | CueFlow Han Sans SC | Mulish SemiBold |
| Size | 94 | 64 |
| Color | `#FFFFFF` | `#F0F0F0` |
| Outline | Black 4.6 | Black 4.4 |
| Shadow | 0 | 0 |
| Order | Top | Bottom |
| Alignment | Bottom center | Bottom center |
| Style margin | 166 | 94 |

ASS styles start from a `1920x1080` script canvas. The burn path adapts the script canvas, safe margins, and line wrapping to the detected active picture.

## Tests and audit

```bash
python3.11 -m unittest discover -s tests -v
python3.11 -m pip wheel . --no-deps --no-build-isolation -w dist
```

The current public baseline passes all 51 unit tests on Python 3.11 and 3.12 when loopback binding is permitted. The repository owner has confirmed completing real-media ASR, translation, burn, cleanup, and Apple VideoToolbox checks; the exact tool versions, input/output hashes, and acceptance record have not been committed. See [docs/TESTING.md](docs/TESTING.md) for exact evidence, [docs/CODE_AUDIT.md](docs/CODE_AUDIT.md) for the audit boundary, and [SECURITY.md](SECURITY.md) for current data boundaries.

## Current limitations

- Large SRT files are not chunked or resumable and can exceed the model context window.
- Job state is in memory. Completed files remain after restart, but old jobs cannot be polled.
- WhisperX improves timing boundaries but cannot guarantee transcription text accuracy.
- Vertical video does not yet have a dedicated ASS layout.
- Output is SDR `yuv420p`; HDR metadata and tone mapping are not preserved.
- New transcription `master.json` files retain model basenames rather than absolute paths and scrub absolute paths from nested metadata. Source filenames, subtitles, and quality metadata can still be sensitive.
- The repository has no dependency lockfile; `pyproject.toml` and `requirements.txt` now agree on `yt-dlp>=2024.1`.

## Documentation

- [Chinese README](README.md)
- [Changelog](CHANGELOG.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Testing](docs/TESTING.md)
- [Versioning and releases](docs/VERSIONING_AND_RELEASES.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## License

The repository currently contains an [MIT License](LICENSE). Bundled CueFlow Han Sans SC, Mulish, and Inter fonts retain the separate OFL texts and modification notice included in `assets/fonts/`.
