# CueFlow 版本与发布

本文件定义未来规则，并明确当前历史能证明和不能证明的内容。它不授权创建 commit、tag、GitHub Release、push、公开仓库或发布制品；这些动作必须由仓库所有者另行明确授权。

## 当前版本事实

- `pyproject.toml`：`version = "0.4.0"`。
- `subflow/__init__.py`：`__version__ = "0.4.0"`。
- `subflow/web.py`：HTTP `server_version = "CueFlow/0.4"`，只保留 major/minor。
- package classifier：`Development Status :: 3 - Alpha`。
- 当前公开历史从一个经过审查的初始快照开始，不包含此前私有归档的 Git 对象。
- 当前 Git tags：无。
- 自动发布、制品签名、PyPI 发布、GitHub Release workflow：无。

因此，`0.4.0` 只能称为**当前声明版本/Alpha 代码基线**。它是否曾正式发布、何时发布、发布了什么二进制，均无法从当前 Git 历史确认。

## 版本号规则

采用 Semantic Versioning 形式 `MAJOR.MINOR.PATCH`：

- `PATCH`：向后兼容的 bug fix、文档修正、测试增强，不改变 CLI 语义或持久化格式。
- `MINOR`：新增向后兼容功能。项目处于 `0.x` 时，必要的破坏性变更也只能进入新的 MINOR，并必须在 CHANGELOG 提供明确迁移说明。
- `MAJOR`：从 `1.0.0` 起，任何不兼容 CLI、`master.json`/review/translation schema、项目目录或自动化行为的变更。

以下变化至少需要 MINOR；在 `1.0.0` 后需要 MAJOR：

- 删除或重命名 CLI 子命令、参数、环境变量；
- 修改 stable ID、时间轴权威来源或 SRT/ASS 配对语义；
- 修改 `master.json`、`review.json`、translation schema 或 worker protocol 且旧项目不能直接读取；
- 改变默认数据外传边界、清理范围或本地服务暴露边界；
- 改变产物目录/文件名并影响脚本调用者。

只有在以下条件成立后才建议进入 `1.0.0`：持久化 schema 有显式版本与迁移策略、CLI 和配置命名稳定、依赖可复现策略明确、完整自动测试为绿色、真实媒体验收持续通过，并已建立至少一个可验证 tag/release 流程。

## 版本号的单一变更点

发布变更时必须同步核对：

1. `pyproject.toml` 的 `[project].version`：package canonical version；
2. `subflow/__init__.py` 的 `__version__`：运行时公开版本；
3. `subflow/web.py` 的 `server_version`：至少与 major/minor 一致；
4. `CHANGELOG.md` 的目标版本标题与真实发布日期；
5. Git tag `vMAJOR.MINOR.PATCH`。

当前仓库没有自动一致性测试；发布前必须人工或通过后续测试验证上述字段。

## Git tag 与 GitHub Release 规则

- tag 格式固定为 `vMAJOR.MINOR.PATCH`，例如 `v0.4.0`。
- 使用 annotated tag，记录版本与候选 commit；不要使用可移动的 branch 代替版本。
- tag 只能指向通过发布 gate 的干净 commit；不得给含未提交修改的工作区状态打 tag。
- tag 一旦被共享不得移动或复用；修复使用新 PATCH。
- GitHub Release 必须基于已经存在的不可变 tag，release notes 从 CHANGELOG 对应版本生成。
- wheel/source archive 应记录 SHA-256、构建 Python 与构建命令；不要把一次本地成功直接等同于跨机器可复现。
- prerelease 必须同时使用 PEP 440/SemVer 兼容版本，例如 package `0.4.1a1` 与 tag `v0.4.1-alpha.1`；不得只给 tag 加 `alpha` 而保持 package 正式版本不变。
- commit、tag、push、GitHub Release、PyPI 或仓库可见性更改都是独立授权动作。

授权后才可采用的 tag 命令形式：

```bash
git tag -a vX.Y.Z <full-commit-sha> -m "CueFlow vX.Y.Z"
git show --stat --decorate vX.Y.Z
```

push tag 和创建 GitHub Release 不属于上述命令的隐含后续步骤。

## 发布前验证

### 1. 仓库与历史边界

```bash
git status --short --branch --untracked-files=all
git worktree list --porcelain
git log -1 --format=fuller
git tag --list
git diff --check
```

- 检查所有 worktree，避免另一个 worktree 中的未提交功能被漏出 release notes。
- 候选 worktree 必须干净；不覆盖、不吸收无关修改。
- `CHANGELOG.md` 的 `Unreleased` 必须只包含候选 commit 可见的变化。

### 2. 版本与依赖

- 核对三个版本字段和目标 tag。
- 比较 `pyproject.toml`、`requirements.txt` 与 `requirements-whisperx.txt`。
- 对任何 Python、yt-dlp、faster-whisper、WhisperX、构建工具或系统工具最低版本变化增加 CHANGELOG 条目。
- 决定并记录 lockfile/constraint 策略；当前仓库不能复现完整依赖集合。

### 3. 自动验证

```bash
python3.11 -m unittest discover -s tests -v
python3.11 -m pip wheel . --no-deps --no-build-isolation -w dist
python3.11 -m subflow --help
git diff --check
```

还必须确认 GitHub Actions 的 Python 3.11 与 3.13 matrix 都成功。当前公开基线的两个 4:3 ASS 回归测试已在本地修复并通过，但本地结果不能替代目标 commit 的 GitHub Actions 结论。

### 4. 干净安装与人工验收

- 在全新 venv 安装候选 wheel，而不是只运行 source checkout。
- 按 [TESTING.md](TESTING.md) 验证真实英语、加拿大法语、mixed、WhisperX、Codex、ASS 和 VideoToolbox 路径。
- 人工检查安全区、音画同步、字体、播放器兼容和 SDR/HDR 边界。
- 检查产物、日志和 `master.json` 是否包含本机路径或私密内容。

### 5. 安全、隐私与公开检查

- 复核 [SECURITY.md](../SECURITY.md) 和历史 [CODE_AUDIT.md](CODE_AUDIT.md) 是否仍适用；历史审计不能替代当前 diff review。
- 扫描当前树与全部 reachable history 中的密钥、token、账户、绝对路径、客户/课程/商业素材和受限制数据。
- 核对字体二进制、OFL 文本、修改 notice 与代码 `LICENSE` 的分发边界。
- 为公开仓库人工核对 GitHub private vulnerability reporting、secret scanning、Dependabot、branch protection 与 least-privilege Actions permissions；这些设置不能由本地文件完整证明。
- 确认 README 没有依赖私人目录、未公开模型或只有维护者机器才有的隐式状态。

### 6. 发布记录

发布 gate 全部通过且得到授权后：

1. 将 `Unreleased` 条目移动到精确版本标题，并写入实际发布日；
2. 保留新的空 `Unreleased` 五分类；
3. 创建 release commit；
4. 用该 commit 的 full SHA 创建 annotated tag；
5. 构建、hash 并保存 release artifact；
6. 如另行授权，再 push commit/tag 和创建 GitHub Release；
7. read back tag、Release、artifact hash 和 CI 状态。

任何一步失败都保留证据，不得把部分完成描述成发布成功。

## 回滚

CueFlow 没有数据库或云端迁移，代码回滚应使用独立 worktree/venv，而不是在用户正在使用的目录中覆盖文件：

```bash
git worktree add ../cueflow-repro-vX.Y.Z vX.Y.Z
cd ../cueflow-repro-vX.Y.Z
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

然后以只读方式复制需要处理的项目输入，先运行 CLI/help 与兼容性检查。不要直接让旧版本修改唯一一份新版本项目。

回滚限制：

- 没有 `master.json` schema version，旧代码是否能读取新项目必须人工验证。
- 没有 lockfile，`pip install .` 不能还原当时精确依赖。
- 外部模型、WhisperX cache、yt-dlp、FFmpeg、Codex CLI 和账户模型可用性不由 Git tag 固定。
- 回滚代码不会恢复已被清理的媒体/产物，也不会撤销已发送的 Codex 请求或账户用量。

## 历史版本复现

当前没有 tag。初始公开基线可以由当前仓库自身解析，而无需在文档中硬编码可失效的 SHA：

```bash
initial_commit=$(git rev-list --max-parents=0 HEAD)
git worktree add ../cueflow-repro-initial "$initial_commit"
```

可可靠定位的历史从初始公开 commit 开始。该 commit 能证明当时整个文件快照，但不能证明其中各功能在更早时间的引入顺序。

无法从当前 Git 记录还原：

- 正式发布日期、历史 tag、GitHub Release 或已发布 artifact；
- 初始公开快照之前的 commit、作者信息、逐项变更顺序和分支关系；
- 各 commit 当时的完整测试结果、真实媒体验收与 CI 日志；
- 精确 Python dependency resolution 和系统工具版本；
- FrameLedger model/helper、WhisperX model cache 的内容 hash 与许可状态；
- YouTube 当时返回的字幕/音频、Codex 当时的模型行为和账户状态；
- 从未提交或已经丢失的 working-tree 内容。

## 当前初始版本建议

为保持与 manifest 一致，建议把通过全部 gate 后的第一个可追溯 tag 定义为 **`v0.4.0`（Alpha 基线）**，而不是另造 `v0.1.0` 或假定更早版本已发布。

初始公开基线的 4:3 布局、路径隐私和依赖下限修改已通过 Python 3.11/3.12 各 51 项测试，真实媒体链路也已由仓库所有者确认完成。当前迁移只建立可追溯的公开起点，不自动创建 tag 或 GitHub Release；首次打 tag 前仍应补齐可独立复核的人工验收记录并重新运行发布 gate。
