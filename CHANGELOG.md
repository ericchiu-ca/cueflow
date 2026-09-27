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
