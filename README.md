# CueFlow

[English summary](README.en.md) | **简体中文（主文档）**

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-1f6feb)
![Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-111111)
![License](https://img.shields.io/badge/License-MIT-2f855a)
![Status](https://img.shields.io/badge/status-alpha-d97706)

CueFlow 把视频或音频变成可追溯的字幕项目：本地转录、可选词级对齐、稳定 ID 翻译、双轨人工审校、双语 ASS 排版和 HEVC VideoToolbox 压制。`master.json` 保存字幕段、时间轴和转录元数据；SRT、ASS 与 MP4 是交换或交付产物。

## 当前状态与边界

- **规模**：中型单机应用。仓库是一个 Python 包，但同时包含 CLI、本地 Web UI、ASR 编排、翻译、审校、排版、视频编码和测试。
- **声明版本**：`0.4.0`，`pyproject.toml` 将成熟度标为 Alpha。当前公开 Git 历史从经过审查的代码快照开始，且没有 tag；更早的版本演进、正式发布日期和发布边界无法从本仓库确认。
- **平台**：主要面向 Apple Silicon macOS。`pyproject.toml` 声明 Python `>=3.11`；CI 覆盖 Python 3.11、3.12 和 3.13，建议本地优先使用 3.11。
- **运行方式**：本地 CLI 或仅绑定回环地址的本地网页；不是多用户服务，不得通过反向代理或公网隧道暴露。
- **数据边界**：视频、音频、ASR、WhisperX、ASS 和 FFmpeg 编码留在本机。主动使用 Codex 翻译时，请求包含字幕文本、稳定 ID、起止时间、源语言和所选模型；不会上传视频或音频。
- **验证状态**：本地 Python 3.11/3.12 各 128 项测试通过；合成视频的真实 libass/HEVC 烧录与 Chromium 桌面、手机尺寸交互通过。CI 定义包含 Python 3.11/3.12/3.13、真实软件渲染和浏览器 smoke；本地新提交尚未在 GitHub 执行。真实模型字幕质量与 Codex 翻译仍需独立验收。详见 [测试指南](docs/TESTING.md)。

CueFlow 不保证 ASR 或翻译语义完全正确，最终字幕和成片必须人工审校。

## 工作流

```mermaid
flowchart LR
    A[本地媒体或 YouTube] --> B[转录与质量评分]
    B --> C[master.json + 原文 SRT]
    C --> D[可选 Codex 中文翻译]
    D --> E[双轨人工审校]
    E --> F[双语 ASS]
    F --> G[HEVC MP4]
```

本地媒体默认使用外部 FrameLedger MLX Whisper helper；高级路径可先用 VAD 生成语音窗，以 Turbo 转录，并对低置信度窗口尝试 full large-v3。`auto` 对齐模式在 WhisperX 可用时执行词级对齐，否则保留 MLX 词时间并给出警告。

## 安装

### 1. 系统依赖

```bash
brew install python@3.11 ffmpeg-full
```

`ffmpeg-full` 必须包含 libass 的 `ass` filter；视频压制还需要同目录或 `PATH` 中可用的 `ffprobe`，Apple 硬件编码需要 `hevc_videotoolbox`。`yt-dlp` 会随 CueFlow 的 Python 基础依赖安装。

### 2. Python 环境

```bash
git clone https://github.com/ericchiu-ca/cueflow.git
cd cueflow
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[asr]"
```

`.[asr]` 包含 YouTube 无字幕时使用的可选 `faster-whisper` fallback。Python 依赖没有统一 lockfile，安装会按约束解析当时可用版本，不能视为字节级可复现环境。浏览器测试依赖单独由 `package-lock.json` 固定。

### 3. 可选 WhisperX 环境

```bash
python3.11 -m venv .venv-whisperx
.venv-whisperx/bin/python -m pip install -r requirements-whisperx.txt
```

`requirements-whisperx.txt` 当前固定为 `whisperx==3.8.6`。它与主环境分开，避免 ML 依赖冲突。

## 配置

最稳妥的方式是在命令行显式传入本地资源：

```bash
cueflow web \
  --host 127.0.0.1 \
  --port 8765 \
  --output projects/local \
  --model-path /path/to/whisper-large-v3-turbo \
  --mlx-helper /path/to/frameledger-asr-helper \
  --whisperx-python /path/to/whisperx/python \
  --no-open
```

也可以使用以下环境变量：

| 变量 | 实际用途 |
| --- | --- |
| `SUBFLOW_MLX_MODEL` | Turbo MLX 模型目录 |
| `SUBFLOW_MLX_LARGE_MODEL` | full large-v3 MLX 模型目录 |
| `SUBFLOW_MLX_HELPER` | FrameLedger MLX helper 可执行文件 |
| `SUBFLOW_WHISPERX_PYTHON` | 安装了 WhisperX 的 Python 可执行文件 |
| `SUBFLOW_FFMPEG` | 可运行且具备所需 filter/encoder 的 FFmpeg |
| `SUBFLOW_CODEX` | Codex CLI 可执行文件 |

网页的“环境配置”可选择 Turbo/full large-v3 模型目录、MLX helper、WhisperX Python 和 FFmpeg。留空恢复自动发现；输入路径必须存在，helper/Python/FFmpeg 必须可执行。路径可用不代表模型格式或 WhisperX 依赖完整，最终以环境检查和任务结果为准。

CLI 与网页使用同一规则：**命令参数 > 环境变量 > 已保存配置 > 自动发现**。配置默认保存在 `~/Library/Application Support/CueFlow/config.json`（仅当前用户可读写）；`CUEFLOW_CONFIG` 或 `cueflow --config /path/to/config.json <command>` 可选择其他文件，Web 也接受 `cueflow web --config ...`。正在运行或排队的任务完成前不能保存配置。自动发现优先检查 `~/Documents/ChatGPT/FrameLedger`，再兼容原来的 `~/Documents/FrameLedger`；FFmpeg 使用可运行且包含 libass 的候选；指定的 FFmpeg 不满足要求时，页面明确显示“自动回退”及实际使用路径。

配置文件损坏时，页面显示错误并允许显式重新保存；后台任务在修复前拒绝执行，不会悄悄使用另一份配置。

使用 Codex 翻译前：

```bash
codex login
codex login status
```

Codex CLI 的登录方式、模型可用性和账户用量由 OpenAI 当前产品与账户策略决定；参见官方 [Authentication](https://learn.chatgpt.com/docs/auth) 和 [Models](https://learn.chatgpt.com/docs/models) 文档。

## 任务记录、恢复与取消

网页“任务记录与恢复”显示最近 30 个任务；完整记录仍保存在各项目的 `job.state.json`，API 返回全部。服务重启后，上传中、排队或运行中的任务标记为“已中断”，不会自动调用模型或 Codex。点击“恢复任务”沿用原项目和输入，复用匹配的 ASR/翻译检查点；重复点击或并发重试只排队一次。输入丢失或上传未完成时，应重新上传。旧版本未保存任务记录的项目不会自动变成可恢复任务。

“取消”终止该任务的媒体处理子进程，保留输入和已完成检查点；取消后可再次恢复。已经完成的产物仍可下载。短小的工具版本检查受原有超时限制，取消在这些检查结束后生效。审校会话与未保存编辑仍只在浏览器/当前服务内，应先保存和下载。

新 `master.json` 使用 `schema_version: 1`；旧版没有该字段时仍可读取，未知版本会报错。转录/翻译项目和 ASS/MP4 旁的 `provenance.json` 或 `*.provenance.json` 记录输入/输出 SHA-256、模型内容与 worker 指纹、生成时间、CueFlow/Python 及可取得的工具版本。缺失版本记为 `null`，Codex 远端模型 revision 仍未知；这些记录不证明识别或翻译质量。

## 最小运行示例

先确认安装和命令入口：

```bash
cueflow --help
cueflow web --host 127.0.0.1 --port 8765 --no-open
```

然后打开 <http://127.0.0.1:8765/>。服务会为每个进程生成 CSRF token，所有改变状态的请求必须携带该 token。

常用 CLI：

```bash
# YouTube：优先人工英文字幕，其次自动字幕，最后可选 faster-whisper
cueflow prepare "YOUTUBE_URL" --output projects/example

# 本地媒体转录
cueflow transcribe-file input.mp4 \
  --output projects/local/example \
  --language en \
  --alignment auto \
  --model-path /path/to/whisper-large-v3-turbo \
  --mlx-helper /path/to/frameledger-asr-helper

# 将已有英文或加拿大法语 SRT 翻译为简体中文
cueflow translate-srt en.srt \
  --output projects/local/translation \
  --language en \
  --model gpt-5.6-terra

# 稳定 ID 手工翻译工作流
cueflow import-translation projects/example/translation_zh.txt
cueflow build projects/example
cueflow qc projects/example

# 双语 ASS 与视频压制
cueflow build-ass en.srt zh.srt --output output/bilingual.ass
cueflow burn input.mp4 output/bilingual.ass \
  --output output/input.bilingual.mp4 \
  --profile hevc-source
```

完整子命令以 `cueflow --help` 和 `cueflow <子命令> --help` 为准。

## 目录结构

```text
.
├── subflow/                    # Python 包
│   ├── cli.py                  # CLI 入口与子命令
│   ├── core.py                 # SRT、稳定 ID、master.json 基础模型
│   ├── transcription.py        # 本地转录与 WhisperX 对齐编排
│   ├── advanced_asr.py         # VAD、双模型重试与置信度评分
│   ├── translation.py          # Codex 翻译边界与结果校验
│   ├── review.py               # 人工审校数据校验
│   ├── bilingual.py            # ASS、预览与视频压制
│   ├── web.py                  # 回环 HTTP 服务与任务生命周期
│   ├── proc.py                 # 子进程生命周期（进程组、超时、退出清理）
│   ├── ffmpeg_tools.py         # FFmpeg 查找与探测缓存
│   ├── settings.py / job_store.py / provenance.py # 配置、任务记录、产物证据
│   ├── static/                 # index.html、app.js、styles.css
│   ├── schemas/                # 翻译结果 JSON Schema
│   └── fonts/                  # 内置字体、许可证和修改说明（随包安装）
├── tests/                      # unittest 测试
├── docs/                       # 架构、测试、发布和历史审计
├── pyproject.toml              # 包元数据与主要依赖声明
├── requirements.txt            # 兼容性安装清单
└── requirements-whisperx.txt   # 独立 WhisperX 环境
```

网页产物默认写入 `projects/local/` 下的六个受管类别：`transcriptions/`、`translations/`、`reviews/`、`bilingual/`、`ass-assets/` 和 `renders/`。清理操作只删除这些类别，但删除后不可由应用恢复，应先下载所需结果。

## 测试

```bash
python3.11 -m unittest discover -s tests -v
python3.11 -m pip wheel . --no-deps --no-build-isolation -w dist
git diff --check
```

新增浏览器和渲染验证命令：

```bash
npm ci --ignore-scripts
npx --no-install playwright install chromium
npm run check:ui
npm run test:browser
python3.11 -m scripts.render_smoke --output /tmp/cueflow-render-check
```

Node.js 22 只用于开发/CI 浏览器测试；运行 CueFlow 不需要 Node。发布前仍应按 [docs/TESTING.md](docs/TESTING.md) 补齐可审计的版本、输入 hash、命令和产物记录。

## 已知限制

- 产物记录了格式版本与内容 hash，但没有跨版本自动迁移机制，也不冻结外部 MLX/Python 环境或远端模型 revision。
- 新生成的转录 `master.json` 只保存模型目录 basename，并递归清理元数据字符串中的绝对本机路径；它仍会保存源媒体 basename、字幕和质量信息，分享前仍需检查内容敏感性。
- `pyproject.toml` 与 `requirements.txt` 已统一 `yt-dlp>=2024.1`，但仓库仍没有统一 lockfile。
- 任务记录可恢复；审校会话、未保存编辑和旧版无记录项目仍不能从服务重启中自动恢复。
- 大型 SRT 按默认 120 条分批，可复用匹配检查点；单条超长字幕仍可能超过模型上下文限制。
- WhisperX 只改善时间边界，不保证修复识别文本。
- 双语轨按顺序配对，不执行语义对齐；段落数必须一致。
- 竖屏视频没有独立版式；输出固定为 SDR `yuv420p`，不保留 HDR 元数据或色调映射。
- CI 执行合成视频的 FFmpeg/libass 软件渲染；真实 YouTube、MLX、WhisperX、Codex 和原生 VideoToolbox 仍需本机验收。

## 文档入口

- [CHANGELOG.md](CHANGELOG.md)：按可验证 commit 回填的变化记录与 `Unreleased` 区域。
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：模块边界、数据流、外部依赖、环境差异与技术债务。
- [docs/TESTING.md](docs/TESTING.md)：测试类型、命令、CI 范围、当前结果与人工验证。
- [docs/VERSIONING_AND_RELEASES.md](docs/VERSIONING_AND_RELEASES.md)：版本、tag、发布、复现与回滚规则。
- [docs/CODE_AUDIT.md](docs/CODE_AUDIT.md)：2026-08-25 的历史代码审计，不代表当前持续审计结果。
- [SECURITY.md](SECURITY.md)：安全支持范围、数据边界和漏洞报告方式。
- [CONTRIBUTING.md](CONTRIBUTING.md)：开发环境、修改原则、测试与提交审查清单。

## 许可证

仓库当前包含 [MIT License](LICENSE)。内置 CueFlow Han Sans SC、Mulish 和 Inter 字体的许可证及修改说明位于 `subflow/fonts/`；发布前仍应由仓库所有者确认代码、字体二进制和所有拟发布素材的权利链。
