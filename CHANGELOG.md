# 变更记录

本文件采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的分类方式，版本规则见 [docs/VERSIONING_AND_RELEASES.md](docs/VERSIONING_AND_RELEASES.md)。只记录能够由当前公开 Git 历史、tracked file 或保存的测试记录验证的内容。

当前公开历史从一个经过审查的 `0.4.0` Alpha 文件快照开始，且没有 Git tag。更早仓库的 commit、变更顺序、发布日期和发布状态未导入，因此无法从本仓库确认。

## [Unreleased]

### Added

- 建立中文主文档体系：架构、测试、版本与发布指南，以及本变更记录。
- 为 README 增加项目规模、当前验证状态、目录结构、文档入口和可追溯限制。
- 增加 `CONTRIBUTING.md`，定义开发环境、隐私边界、测试 gate 和提交审查清单。
- 增加 VAD、批量 MLX worker、Turbo/full large-v3 候选比较、低置信度重试、英法混合识别和置信度评分。
- 增加 Codex CLI 翻译 provider、严格 JSON Schema、稳定 ID 结果校验和保留但禁用的 `openai-api` provider 边界。
- 增加 CueFlow Han Sans SC、Mulish 与 Inter 字体及其许可证和修改说明。
- 增加模型元数据可移植性、旧项目重新对齐脱敏、Codex prompt 披露和模型环境变量回归测试。

### Changed

- Web 处理器改为路由表（每个接口一个方法，统一 Host/CSRF 校验与错误处理，POST 路由声明是否写文件）；`transcribe_vad_cascade` 拆分为升级原因、重试语言、候选选择、问题生成和汇总等小函数，行为不变。
- Codex 翻译按每批 120 条分批进行，附带前后各 3 条只读上下文；每批单独校验、失败重试一次，进度按批次显示。长视频不再依赖单次输出覆盖全部 ID。Codex 登录状态每个任务只检查一次。
- 审校规则只在后端实现：审校台通过新的只读接口 `POST /api/review/audit` 获取问题和双轨配对状态，不再在页面里复制一份规则。此前两份实现已出现差异（重复文本是否忽略空白、非有限时间是否报错、提示文案）。
- 字体从 `assets/fonts/` 移到 `subflow/fonts/`，作为 package data 随包安装；`pip install --user` 等非 `sys.prefix` 安装方式不再找不到字体。License 表达式改为 `MIT AND OFL-1.1`，字体许可证与修改说明一并写入 wheel 元数据。
- 三份 FFmpeg 查找逻辑合并为 `subflow/ffmpeg_tools.py`，探测结果按路径、修改时间和大小缓存。
- CI matrix 增加 Python 3.12，并在仓库外用独立 venv 安装 wheel 做 smoke test，以发现打包遗漏的资源文件。
- 将安装、模型配置、Codex 数据边界、测试命令和已知限制整理为当前公开基线的实际行为。
- 将 `requirements.txt` 的 `yt-dlp` 下限统一为 manifest 使用的 `>=2024.1`。
- 将本地转录扩展为高级 ASR pipeline，并调整双语 ASS 字体、字号、几何适配、预览与压制布局。
- 记录仓库所有者已完成真实媒体 ASR、翻译、烧录、清理和 Apple VideoToolbox 人工验收；未提供的 hash、工具版本和逐项结果继续标为不可独立复核。

### Fixed

- Web 环境状态和实际转录统一读取 `SUBFLOW_MLX_MODEL`；显式 `--model-path` 仍具有优先级。
- Codex 数据披露明确包含字幕文本、稳定 ID、起止时间、源语言和所选模型。
- 修复 4:3 活动画面和原生 4:3 视频的 ASS 安全底边距二次舍入；两个回归测试均得到 `MarginV=86`。
- ASS 文本中的反斜杠改用 `\` + U+2060 转义；libass 不识别 `\\`，原先字面 `\N`、`\h` 会被当作换行或硬空格。
- SRT 解析容忍 UTF-8 BOM，不再丢弃首条字幕；解析与规范化统一保留毫秒精度，不再四舍五入到 10ms。
- FFprobe 只读取第一个视频流（`-select_streams v:0`），音轨在前的容器不再烧录失败。
- Codex 翻译运行前删除旧的结果文件，避免 Codex 未写出结果时误用上一次的翻译。
- Web 下载路由校验任务类型；GET 请求出错时返回 JSON 错误，而不是断开连接不响应。
- 审校保存先校验全部轨道再原子写入，并按会话加锁；中文轨校验失败不再留下半更新的原文轨。
- 本地清理会等待进行中的上传、ASS/审校创建和保存完成后再执行；删除在锁外进行，不再阻塞状态轮询。默认样式预览渲染加锁并原子替换。
- 新增 `subflow/proc.py`：外部命令在独立进程组中运行，超时或出错时连同子进程（如 yt-dlp 调起的 FFmpeg）一起终止；Web 服务退出时终止所有子进程，Ctrl+C 不再等待当前任务跑完。
- FFmpeg 音频提取和片段提取增加 `-nostdin` 与超时；压制进度回调异常时终止 FFmpeg 并删除不完整的输出。
- 带旋转元数据的视频（如 iPhone 竖屏）按显示方向计算 ASS 布局，不再按存储方向把字幕挤压变形。
- VAD 级联所有语音窗都失败时报错并回退，不再输出空字幕却显示完成；回退警告保留失败原因。
- mixed 语言项目可以重新对齐（按语音窗语言分别对齐）；重新对齐后刷新置信度窗口和质量问题中的字幕 ID。
- YouTube 字幕检测改用 yt-dlp 的结构化 JSON；原先按 `--list-subs` 文本匹配 "automatic subtitles"，而 yt-dlp 实际输出 "automatic captions"，导致自动字幕被当成人工字幕。自动字幕只接受原始语音轨（`en-orig`），不再把非英语视频的机器翻译当作英文原文。
- Web 响应增加 `X-Frame-Options: DENY`、`frame-ancestors 'none'`、`nosniff` 和 `no-referrer`，防止页面被第三方网站嵌入做点击劫持。
- QC 与审校共用稳定 ID 规则（4 位以上数字）；超过 9999 条字幕时 QC 不再误报 `INVALID_ID`。
- SRT 时间行允许携带位置等 cue 设置（如 `X1:10 X2:20`），不再整份文件被拒；格式错误时报告出错的块号。
- WhisperX/MLX 输出中的 NaN、Infinity 时间不再进入 `master.json` 与 SRT。
- 压制上传只接受视频扩展名、转录上传只接受支持的音视频扩展名，并在创建项目目录前校验；上传文件不再可能与项目内固定的 `bilingual.ass`、`output` 冲突。编码 profile 错误提示列出全部可选值。
- ASS 时间改为按毫秒四舍五入到厘秒（1.005 s → 1.01），不足 1 厘秒的字幕保留最短 1 厘秒而不是变成 0 时长。
- 样式预览的 FFmpeg 超时或无法启动时作为预览错误处理，不再让 ASS 生成请求返回 500。
- Codex 登录检测改为宽松匹配（仍要求 ChatGPT 登录），不再依赖一句固定英文文案。JSON 请求体超限提示显示正确的 MB 数值。
- ASS 生成和审校创建先在内存中校验，通过后才创建项目目录；创建后的步骤失败、上传中断时删除对应项目目录，不再留下空目录。删除只作用于输出目录下受管类别中的单个项目目录。
- 中英轨在同一时间轴上时按时间校验配对：条数相同但中间删一条、别处补一条的情况会被拒绝，并指出从哪一条开始错开；时间轴无关的两轨仍按顺序配对。审校台同步显示错位位置。
- 合并并持续读取 FFmpeg 输出，避免管道阻塞；为 YouTube 外部命令增加可运行性检查与超时。
- 上传先写入临时文件再原子重命名，失败任务会进入失败状态。
- wheel 包含 Web 静态文件、JSON Schema 和字体资源。

### Removed

- 当前公开历史没有可验证的版本间移除记录。

### Security

- Web 服务限制到 loopback，校验 Host，并为写操作和原始上传要求进程级 CSRF token。
- 清理范围限制为 CueFlow 管理的六类目录，活动任务存在时拒绝清理。
- Codex 翻译在空临时目录中，以 read-only、ephemeral、忽略本机配置和规则、schema-constrained output 的方式运行。
- 新生成的 `master.json` 仅保存模型 basename，并递归清理 warning、质量问题、置信度等嵌套元数据中的绝对路径；重新对齐旧项目时同步清理遗留路径。
- ASS `manifest.json` 使用项目内相对路径，并清理预览错误中的绝对路径。
- 当前公开仓库由经过审查的文件快照重新初始化，没有复制此前仓库的 `.git`、refs、reflog 或未引用对象。

## 历史边界

`pyproject.toml` 与 `subflow/__init__.py` 当前声明 `0.4.0`，但仓库没有 `v0.4.0` tag 或 GitHub Release 记录。因此 `0.4.0` 只能称为当前 Alpha 代码基线，不能称为已经验证发布的历史版本。

快照之前的提交、作者、逐项引入顺序、测试结果和发布日期均标记为：**无法从当前公开历史确认**。
