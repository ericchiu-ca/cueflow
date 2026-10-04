# CueFlow 缓存修复与开发交接

记录日期：2026-10-03（America/Toronto）。本文件交接的是本地修复，不是发布或真实媒体全链路验收。

## 从哪里继续

- 当前开发仓库是 `cueflow-public`，远端为 `ericchiu-ca/cueflow`。
- `cueflow` 目录是旧的私有归档，本次检查开始时有 11 个已修改 tracked 文件；不要覆盖、重置或把旧 `.git` 合入公开仓库。
- 本次分支：`codex/cache-input-fingerprints`；基线：`895476a09b20f72340ad4659c1e29ac782c8bdbe`（`feat/resumable-jobs` / PR #4）。
- 原开发 checkout 继续保持 `feat/resumable-jobs`，本次修改在独立本地 worktree。使用 `git worktree list --porcelain` 找到路径；临时 worktree 即使日后移除，已提交的本地分支仍在公开仓库 Git 中。
- 下一位接手者先检查 `git status --short --branch`、`git log -3 --oneline` 和 worktree 列表。先阅读本文，再查看 `git diff 895476a..codex/cache-input-fingerprints`；不要从旧归档开始开发。

## Claude 已有工作的状态

以下是本次通过 GitHub CLI 只读查询的状态快照，合并前必须重新查询。四个 PR 均为 OPEN、MERGEABLE/CLEAN，各自 head 的 Python 3.11/3.12/3.13 检查全部 SUCCESS：

| PR | head / 当前提交 | base | 内容 |
| --- | --- | --- | --- |
| [#1](https://github.com/ericchiu-ca/cueflow/pull/1) | `fix/review-findings` / `c755560` | `main` | 字幕、排版、转录、Web 与进程修复 |
| [#2](https://github.com/ericchiu-ca/cueflow/pull/2) | `refactor/followups` / `09349a9` | `fix/review-findings` | 配对、字体打包、分批翻译、审校统一与重构 |
| [#3](https://github.com/ericchiu-ca/cueflow/pull/3) | `fix/remaining-findings` / `27c5a2e` | `refactor/followups` | 剩余解析、ASR、ASS 和 UI 修复 |
| [#4](https://github.com/ericchiu-ca/cueflow/pull/4) | `feat/resumable-jobs` / `895476a` | `fix/remaining-findings` | ASR/翻译续跑与统一任务生命周期 |

原 CI 失败由上传中断测试的单次 `recv()` 引起，`09349a9` 改为读取完整响应并向后续分支合入；重新运行后的检查已成功。CI 卡点已经解除。上述绿色检查不覆盖本交接分支的新修改。

依赖顺序为 #1 → #2 → #3 → #4，再集成本分支。合并策略（尤其 squash/rebase）会改变后续 PR 的 ancestry；每次合并后人工核对下一 PR 的 base、实际 diff 和当前 head 的 CI，不要只依赖自动重定向。此次没有推送、创建 PR、合并、修改仓库设置或发布。

## 本次修复

1. `subflow/translation.py`：翻译缓存匹配完整实际 prompt、输出 Schema、模型和 provider。时间、邻近上下文或翻译规则修改时，受影响批次重算；未受影响批次仍复用。
2. `subflow/advanced_asr.py`：ASR 缓存匹配模型文件内容、worker 实现、语言、initial prompt 和音频内容。权重/配置原地替换、同名不同模型都不会复用旧结果；同内容迁移目录可以复用。
3. `tests/test_translation.py`、`tests/test_advanced_asr.py`：添加行为回归，覆盖修复前失败、修复后通过及旧检查点失效；更新相邻上下文改变后的既有测试预期。
4. `CHANGELOG.md`、`docs/ARCHITECTURE.md`、`docs/TESTING.md`：记录行为、验证及适用边界。

旧检查点自动失效，不会手动删除用户项目文件；首次重跑可能需要重新处理。模型目录每批扫描一次，完整读取非隐藏文件，重试不重复扫描。运行期间保持权重不变。外部 Python/MLX 版本与云端模型的具体 revision 尚未冻结，`auto` 只记录选择本身；这些属于后续可复现性工作。

## 验证证据

- Python 3.11.15 / 3.12.15：各 111 项通过。
- 修改涉及的四份 Python 文件：`ruff check` 通过；`git diff --check` 通过。
- wheel：构建成功；在独立环境、仓库外确认 Schema、静态页、字体与 CLI 可用。安装 smoke 使用 `--no-deps --no-index`，不证明运行时依赖解析成功。
- 真实 Turbo MLX worker：macOS 合成短语音 → 两段结果 → 用不可用 Python 从检查点复用成功；约 7.49 s / 0.63 s。完整边界及输入 hash 见 [TESTING.md](TESTING.md)。合成语音 smoke 不是字幕质量验收。
- 未执行：本分支 Python 3.13、本分支 GitHub CI、真实 VAD/full large-v3/WhisperX 全链路、真实 Codex 分批与失败恢复、浏览器和成片人工验收。

复核命令（在本分支 worktree 内）：

```bash
python3.11 -m unittest discover -s tests -v
python3.12 -m unittest discover -s tests -v
ruff check subflow/translation.py subflow/advanced_asr.py tests/test_translation.py tests/test_advanced_asr.py
python3.11 -m pip wheel . --no-deps --no-build-isolation -w dist
git diff --check
```

## 接下来的步骤

1. 审查本分支差异，核对当前四个 PR 状态；推送前确认用户授权。若作为新的叠加 PR，其 base 应为 `feat/resumable-jobs`，前提是该分支尚未合并；否则依据真实 main 重新选择基线并运行检查。
2. 为本机配置正确模型/helper 路径。公开版本默认 `~/Documents/FrameLedger`，检查时本机资源在 `~/Documents/ChatGPT/FrameLedger`。本次只在测试中指定路径，未更改用户持久环境或应用 launcher：

   ```bash
   export SUBFLOW_MLX_MODEL="$HOME/Documents/ChatGPT/FrameLedger/.cache/models/whisper-large-v3-turbo"
   export SUBFLOW_MLX_LARGE_MODEL="$HOME/Documents/ChatGPT/FrameLedger/.cache/models/whisper-large-v3-mlx"
   export SUBFLOW_MLX_HELPER="$HOME/Documents/ChatGPT/FrameLedger/phase2/mlx_whisper_asr/run"
   export SUBFLOW_WHISPERX_PYTHON="$HOME/Documents/ChatGPT/cueflow-public/.venv-whisperx/bin/python"
   ```

3. 使用获授权的短视频做真实 VAD → Turbo/large-v3 → WhisperX → 审校 → ASS → 压制验收，保存工具版本、输入/输出 hash、命令和人工结论。真实 Codex 翻译会发送字幕数据；完成必要的数据授权后再验证分批与恢复。
4. 合并前重查当前 head 的 CI，按依赖顺序处理 PR。发布版本、tag、Release、仓库设置和部署仍需要单独授权。
5. 不清理原有用户媒体或检查点；临时验证产物留在忽略目录。交接包保留 patch、验证摘要和 CI 状态快照，临时 worktree 不作为唯一恢复来源。
