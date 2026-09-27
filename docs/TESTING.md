# CueFlow 测试指南

本文区分三类证据：自动测试、构建/静态检查、真实媒体人工验收。任何一类通过都不能替代另外两类。

## 实际存在的测试

测试框架是 Python 标准库 `unittest`。仓库没有 pytest、tox、nox、coverage threshold、snapshot test 或端到端浏览器测试配置。

| 文件 | 主要覆盖 |
| --- | --- |
| `tests/test_subflow.py` | SRT/稳定 ID、QC、MLX 结果转换、语言映射、文件名与 WhisperX Python 路径 |
| `tests/test_advanced_asr.py` | VAD window、语言证据、Turbo/large-v3 选择、置信度、时间边界和词重建 |
| `tests/test_bilingual.py` | ASS 配对、转义、样式、4:3/安全区布局、换行与编码 profile |
| `tests/test_review.py` | 审校数据往返、问题检测、结构修改与校验 |
| `tests/test_translation.py` | Codex command 参数、provider 边界、ID 完整性、翻译产物 |
| `tests/test_cleanup.py` | 受管目录清理、活动任务保护和内存索引清理 |
| `tests/test_web_security.py` | loopback、Host、CSRF 和非受管文件保护；其中一项会绑定临时本机端口 |
| `tests/test_external_commands.py` | 外部命令可运行性、超时和 FFmpeg fallback |
| `tests/test_ffmpeg_selection.py` | FFmpeg 选择与 helper 环境 |
| `tests/test_cli_release.py` | 翻译子命令、默认 model/provider 与可选项 |

这些测试主要是单元测试和轻量集成测试，使用 mock、临时目录、假外部命令和内存 HTTP server。没有真实 YouTube、模型下载、MLX Metal、WhisperX model、Codex 翻译或真实视频编码测试。

## 可复制命令

### 完整单元测试

```bash
python3.11 -m unittest discover -s tests -v
```

CI 使用 `python`，因为 `actions/setup-python` 已选择 matrix 版本；本地文档使用 `python3.11`，避免误用未在 CI 中覆盖的系统 Python。

### 定向测试

```bash
python3.11 -m unittest tests.test_subflow -v
python3.11 -m unittest tests.test_advanced_asr -v
python3.11 -m unittest tests.test_bilingual -v
python3.11 -m unittest tests.test_translation -v
python3.11 -m unittest tests.test_web_security -v
```

`tests.test_web_security.WebSecurityTests.test_post_requires_process_csrf_token` 需要绑定 `127.0.0.1` 的临时端口。若受限沙箱返回 `PermissionError: [Errno 1] Operation not permitted`，先在允许本机 loopback 的环境重跑该测试，再判断产品是否失败；不要把权限错误改写成通过。

### CLI 与构建

```bash
python3.11 -m subflow --help
python3.11 -m pip wheel . --no-deps --no-build-isolation -w dist
git diff --check
```

发布前还应在全新 venv 中安装刚生成的 wheel，再运行：

```bash
cueflow --help
cueflow web --help
cueflow transcribe-file --help
cueflow translate-srt --help
cueflow burn --help
```

`--no-build-isolation` 要求当前环境已有满足 `pyproject.toml` 的 `setuptools>=77` 和 `wheel`。CI 的 `python -m pip wheel . --no-deps -w dist` 会创建隔离构建环境，可能从网络解析 build dependency；两种构建路径都应在发布前明确记录。

## 当前验证基线

以下结果记录于 2026-09-01（America/Montreal），对象是创建当前公开仓库时使用的完整文件快照。它是本地验证证据，不是 Git tag、GitHub Release 或跨机器可复现性证明。

| 环境/命令 | 结果 |
| --- | --- |
| Python 3.11.15，完整 unittest，允许本机 loopback | 51/51 通过 |
| Python 3.12.14，完整 unittest，允许本机 loopback | 51/51 通过 |
| Python 3.11/3.12，完整 unittest，受限沙箱 | 各 49 项通过；唯一错误是沙箱拒绝测试绑定 `127.0.0.1` 临时端口 |
| Python 3.11.15，布局、转录隐私、翻译披露和环境变量针对性测试 | 25/25 通过 |
| Python 3.11.15，wheel build，`--no-deps --no-build-isolation` | 成功生成 `cueflow_subtitles-0.4.0-py3-none-any.whl` |
| Python 3.11.15，`python3.11 -m subflow --help` 及 10 个子命令 help | 全部成功加载 |

本轮修复前，以下两个 `tests/test_bilingual.py` 用例都期望 ASS `MarginV` 为 `86`，而实现输出 `87`：

- `test_render_layout_stacks_tracks_inside_detected_4_3_picture`
- `test_render_layout_uses_a_4_3_script_canvas_for_true_4_3_video`

原因是实现先把 active-picture 安全底边距舍入为像素，再换算到 ASS script canvas，产生二次舍入偏差。现在保留浮点底边距，只在最终脚本坐标换算时舍入；两个用例以及完整 Python 3.11/3.12 suite 均已通过。

`test_post_requires_process_csrf_token` 必须真实绑定 loopback 端口。受限沙箱中的 `PermissionError: [Errno 1] Operation not permitted` 是执行环境限制；只有在允许 loopback 的环境重跑并通过后，才能把该测试计为通过。

2026-09-26 在 `fix/review-findings` 分支修复代码审查问题后的本地复核：

| 环境/命令 | 结果 |
| --- | --- |
| Python 3.11.15，完整 unittest，允许本机 loopback | 73/73 通过 |
| Python 3.12.14，完整 unittest，允许本机 loopback | 73/73 通过 |
| Python 3.11.15，wheel build 后装入独立 venv，在仓库外导入 schema/静态页并运行 `cueflow --help` | 成功 |
| `ffmpeg-full`（libass）真实渲染反斜杠转义文本、音轨在前的 MKV 探测、90° 旋转视频端到端烧录 | 结果符合预期 |
| 真实启动 `cueflow web`，经浏览器调用审校创建/保存/下载、ASS 生成、预览和清理 API | 结果符合预期 |

新增的每个回归测试都在修复前的代码上失败、修复后通过。

`refactor/followups` 分支（同日）：Python 3.11.15 与 3.12.14 各 84/84 通过；wheel 在独立 venv 中确认字体、schema 与静态页随包安装；`pip install --user` 场景下字体可被找到（旧版本找不到）；`transcribe_vad_cascade` 拆分前后对 7 个语音窗的 en/mixed 黄金用例输出逐字节一致；真实启动 `cueflow web` 验证审校台的后端审计、按时间配对提示与各路由。

仓库中没有 JUnit artifact、coverage artifact 或 release verification record。公开历史从当前快照开始；更早代码状态及其测试结果**无法从本仓库确认**。

## CI 覆盖范围

`.github/workflows/tests.yml` 在以下事件运行：

- push 到 `main`；
- 任意 pull request。

单个 `unit` job 使用 `macos-14`、15 分钟 timeout 和 Python 3.11/3.12/3.13 matrix，步骤为：

1. `actions/checkout@v4`；
2. `actions/setup-python@v5`，启用 pip cache；
3. `python -m pip install -e .`；
4. `python -m unittest discover -s tests -v`；
5. `python -m pip wheel . --no-deps -w dist`。
6. 把 wheel 安装进独立 venv，在仓库目录之外确认 translation schema 和静态页面随包安装，并运行 `cueflow --help`。

CI workflow 只授予 `contents: read`。它不发布 wheel、不创建 tag/Release、不上传测试报告，也不修改云端环境。

CI 安装的是基础 package，不是 `.[asr]`，也不安装 `requirements-whisperx.txt`。因此 CI 主要验证纯 Python 逻辑、mock 边界和 package build，不能证明外部 ML/媒体链路可用。

## 自动测试不能证明什么

即使完整 unittest 与 CI 全绿，也不能证明：

- FrameLedger 模型/helper 在目标 Mac 存在、版本兼容或使用 Metal 成功；
- WhisperX 3.8.6 能下载/加载对齐模型并正确处理英语、加拿大法语和混合语音；
- `yt-dlp` 对目标 URL 有权限、能适应站点变化或符合内容使用条款；
- 当前 Codex 登录账户可使用所选模型，或翻译准确、费用/用量可接受；
- 私密字幕适合发送到 OpenAI；
- FFmpeg 具备 `ass` filter、`ffprobe` 和 `hevc_videotoolbox`，且真实视频音画同步；
- 16:9、4:3、letterbox、竖屏、长字幕、特殊字符、HDR 输入和长视频的视觉质量；
- 进程终止、磁盘写满、超大上传、并发操作或长任务后的恢复能力；
- wheel 在一台全新的受支持 Mac 上可以无额外隐式环境完成安装与运行；
- GitHub repository settings、branch protection、private vulnerability reporting、secret scanning 或 Release artifact 状态。

## 发布前人工验收

每次候选版本至少保存以下人工记录，记录候选 commit、Python/FFmpeg/Codex/模型版本、输入的非敏感 hash、命令、结果和产物 hash：

> 2026-09-01，仓库所有者确认已经使用真实媒体完成人工验证，包括 ASR、翻译、烧录、清理和 Apple VideoToolbox 路径。当前仓库没有收到对应媒体 hash、工具/模型版本、命令、产物 hash 或逐项验收记录，因此该 gate 记为“操作者确认完成”，但尚不能由第三方从 Git 独立复核。以下清单继续作为未来候选版本的留档格式。

1. 在干净 Python 3.11 环境安装 wheel，验证所有 CLI help。
2. 使用可公开或已获授权的短媒体分别验证英语、加拿大法语和 mixed 模式。
3. 分别验证 `native`、`auto`，以及已配置时的显式 `whisperx` 对齐。
4. 验证高/中/低置信度记录、large-v3 重试、质量提醒和人工审校流程。
5. 用不含秘密的 SRT 验证 Codex 翻译，检查实际发送字段、ID 完整性和人工翻译质量。
6. 验证 16:9、4:3/letterbox 和至少一个长行字幕的 ASS；自动化 4:3 回归测试已恢复为绿色，但仍需肉眼确认真实画面安全区。
7. 用真实视频执行 `hevc-source` 与 `hevc`，人工检查分辨率、帧率、音轨、字幕安全区、播放器兼容性和 SDR/HDR 结果。
8. 检查 `master.json`、warning、manifest 和日志是否包含本机路径、账户、客户名或私密字幕。
9. 验证清理只删除六个受管类别，并确认重要结果已另行保存。

## 数据与实验复现说明

CueFlow 不是数据分析或回测项目，没有市场数据、训练集、回测时间边界或随机种子配置。它处理用户媒体与字幕；仓库不包含测试媒体或真实模型输出。

当前可复现性边界：

- **数据来源**：本地用户媒体，或运行当时由 `yt-dlp` 获取的 YouTube 字幕/音频；仓库不记录抓取时间和远端内容版本。
- **时间边界**：由媒体/SRT 自带时间码决定；没有分析样本期。
- **随机种子**：代码和测试没有可配置 seed；外部模型/硬件推理的确定性未在仓库声明。
- **主要输出**：`master.json`、SRT、`translation.result.json`、`translation_zh.txt`、`review.json`、`master.reviewed.*.json`、ASS、预览 PNG 和 MP4。
- **缺失 provenance**：没有源媒体 SHA-256、模型 SHA-256、完整工具版本、lockfile 或标准 run manifest，因而不能从当前仓库做字节级实验复现。

版本化复现与回滚方法见 [VERSIONING_AND_RELEASES.md](VERSIONING_AND_RELEASES.md)。
