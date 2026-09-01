# 参与 CueFlow 开发

感谢参与 CueFlow。项目目前是面向 Apple Silicon macOS 的 Alpha 阶段本地字幕工具。贡献应保持本地优先、产物可追溯、数据边界清楚，并避免把真实媒体或私密字幕带入 Git 历史。

## 开始前

1. 阅读 [README.md](README.md)、[架构说明](docs/ARCHITECTURE.md)、[测试指南](docs/TESTING.md)和[安全政策](SECURITY.md)。
2. 执行 `git status --short --branch`，确认当前分支以及已有未提交修改；不要覆盖或整理与本次贡献无关的工作。
3. 如使用多个 worktree，执行 `git worktree list --porcelain` 并分别检查状态，避免把另一工作树的未提交修改误当成当前基线。
4. 不要提交媒体、字幕、模型、生成项目、凭据、账户缓存、客户数据或未经确认可再分发的素材。

仓库尚未规定强制 commit message 格式。请使用简短、可核验的描述，并让每个 commit 保持单一目的。

## 开发环境

建议使用 Python 3.11：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[asr]"
```

WhisperX 使用独立环境：

```bash
python3.11 -m venv .venv-whisperx
.venv-whisperx/bin/python -m pip install -r requirements-whisperx.txt
```

系统媒体依赖、模型路径和 Codex 登录配置见 [README.md](README.md)。仓库没有 lockfile，安装记录不能等同于精确环境快照。

## 修改原则

- 保持 `master.json` 中稳定字幕 ID 和时间轴的语义；更改结构时必须同时说明兼容性和迁移方式。
- 生成元数据不得写入本机绝对路径、凭据或无必要的原始异常详情。
- Web 服务必须继续限制在 loopback，并保持 Host 与 CSRF 控制。
- Codex 数据边界或 prompt 字段变化时，同步修改 UI、README、SECURITY、测试和 CHANGELOG。
- 依赖最低版本变化时，同步检查 `pyproject.toml`、`requirements.txt`、`requirements-whisperx.txt` 和 CI。
- 不把真实模型、网络服务或硬件测试的结果伪装成单元测试能够证明的结论。

## 测试

至少运行与修改直接相关的测试。提交前运行完整基础 gate：

```bash
python3.11 -m unittest discover -s tests -v
python3.11 -m pip wheel . --no-deps --no-build-isolation -w dist
git diff --check
```

HTTP 安全测试会绑定 `127.0.0.1` 的临时端口；受限执行环境可能阻止该操作。若出现 `PermissionError: [Errno 1] Operation not permitted`，应在允许本机 loopback 的环境重跑，不能把它记录成通过。

ASR、WhisperX、Codex、FFmpeg/libass 和 VideoToolbox 修改还需要按 [测试指南](docs/TESTING.md) 的人工清单使用获授权的测试媒体验证，并记录工具版本、输入 hash、命令、输出和人工结论。

## 文档和变更记录

- 面向使用者的变化写入 [CHANGELOG.md](CHANGELOG.md) 的 `Unreleased` 区域。
- 命令、环境变量、依赖、数据流或安全边界变化时，同步更新对应文档。
- 只陈述本次实际执行的测试结果；未执行的检查应明确写成未验证。
- 检查新增 Markdown 的本地链接，并避免把开发者本机绝对路径写进示例。

## 提交审查清单

- [ ] 差异只包含本次贡献范围。
- [ ] 没有密钥、token、账户、绝对本机路径、真实媒体或私密字幕。
- [ ] 相关测试和完整基础 gate 已运行，结果如实记录。
- [ ] 依赖声明、README、SECURITY 和 CHANGELOG 已按需同步。
- [ ] 生成产物未加入 Git；wheel 或临时文件已留在忽略目录或从工作树移除。
- [ ] 真实媒体/外部工具路径的人工验证已完成，或明确列为待人工验证。

版本 tag、GitHub Release 和回滚规则见 [版本与发布指南](docs/VERSIONING_AND_RELEASES.md)。安全问题请遵循 [SECURITY.md](SECURITY.md)，不要在公开 issue 中包含可利用细节或私密数据。
