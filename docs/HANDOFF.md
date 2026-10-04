# CueFlow 开发交接

记录日期：2026-10-03。本次为本地开发交接；没有推送、PR 创建/合并、发布、launcher 切换或用户模型配置写入。

## 从哪里继续

- 当前公开开发仓库是 `cueflow-public`，remote 为 `ericchiu-ca/cueflow`。`cueflow` 是保留的旧私有归档，检查开始时有 11 个已修改 tracked 文件；不要覆盖、重置或复用旧 `.git`。
- 本次分支：`codex/local-workflow-reliability`；父提交为 `b2021638214cae5918ca584495d918221b612535`（`codex/cache-input-fingerprints`），再往前是 `895476a09b20f72340ad4659c1e29ac782c8bdbe`（`feat/resumable-jobs` / PR #4）。
- 修改位于独立本地 worktree。用 `git worktree list --porcelain` 查询路径；临时 worktree 不作为唯一恢复来源，本地分支与交接 patch 都保留。
- 原公开 checkout 继续保持 `feat/resumable-jobs`。下一位接手先看 `git status --short --branch`、`git log -3 --oneline`，确认没有进入旧归档或 main。

```bash
git diff b202163..codex/local-workflow-reliability  # 本轮六项修改
git diff 895476a..codex/local-workflow-reliability # 包含前轮缓存修复
```

## 本轮完成内容

| 范围 | 行为与入口 |
| --- | --- |
| 重试 | `JobManager.resume_job` 在锁内预占尝试；重复或并发请求只执行一次，过期尝试不能覆盖新尝试。原翻译 retry API 保持兼容。 |
| 配置 | `settings.py` 共用 CLI/Web 路径解析；参数 > 环境 > 保存设置 > 自动发现，FrameLedger 新旧目录均支持。网页配置可保存/检查，活动任务期间拒绝修改；未知字段/无效路径不落盘。 |
| 持久化/取消 | `job_store.py` 原子写入项目内 `job.state.json`，相对 output root、版本化、恢复时校验路径。重启未完成任务等待人工恢复；取消保留输入/检查点，只终止关联进程。直接父进程退出后仍清理孙进程。 |
| 真实 smoke / CI | `scripts/render_smoke.py` 实际 FFmpeg/libass 烧录合成视频；`scripts/browser_smoke.mjs` 使用真实 HTTP/前端与合成 provider，验证设置、恢复/取消、刷新历史、审校、未保存保护。新增 Ubuntu job；原 Python 3.11/3.12/3.13 matrix 保留。 |
| 前端拆分 | `static/index.html`、`app.js`、`styles.css` 分离并格式化；静态路由白名单，wheel 全部打包；新增环境配置与任务记录面板。 |
| 格式/文档 | master/manifest/config/job/provenance 带 v1；legacy master 可读，未知版本拒绝。provenance 含输入/输出 SHA-256、模型/worker 指纹、时间与可取得工具版本，缺失为 null。README、架构、测试、安全与发布文档同步。 |

前轮 `b202163` 同时保留：翻译缓存匹配完整实际 prompt、Schema、模型和 provider；ASR 缓存匹配音频、语言、initial prompt、模型文件内容和 worker，实现同名/原地模型更新失效、内容迁移仍复用。旧键自动失效，可能首次重算。不要删除用户检查点来模拟修复。

## 验证与证据边界

- Python 3.11.15 / 3.12.15：各 **128/128**。新增 17 项生命周期、配置与格式回归；前轮 4 项缓存回归仍通过。
- `ruff check subflow tests scripts`、`npm run check:ui`、`git diff --check`：通过。
- wheel：重新构建后，在独立 venv、仓库外验证 CLI、Schema、HTML/JS/CSS、字体和新模块；安装 smoke 为 `--no-index --no-deps`，不代表完整依赖解析成功。
- Chromium / Playwright 1.62.1：桌面 1440×1000、手机 390×844，无 console/HTTP 错误或横向溢出；配置、恢复/取消、下载、刷新、审校保存、未保存保护、ASS 生成/预览通过。Browser 插件不可用，使用 Playwright。
- FFmpeg 9.0.2：真实合成 960×540 和 720×540 视频，字幕像素/安全边界与特殊字符路径通过；两段生产 `hevc_videotoolbox` 烧录通过，生成 provenance。
- macOS 沙箱最初拒绝 Chromium Mach port 与 VideoToolbox；宿主权限重跑通过。这些是合成 smoke，不代表真实字幕语义/成片质量验收。
- 前轮真实 Turbo MLX 合成语音检查点复用通过，完整记录在 [TESTING.md](TESTING.md)。本轮没有调用 MLX/full large-v3/WhisperX 模型、Codex 翻译或 YouTube。
- **未验证**：本地 Python 3.13、当前新提交的 GitHub CI、Ubuntu render-browser 实跑、真实模型/翻译质量与长视频端到端。CI 定义完成不等于已在 GitHub 成功。

复核命令见 [TESTING.md](TESTING.md)。Node.js 22 和 `package-lock.json` 只服务开发/CI；正常使用不需要 Node。

## 持久状态注意事项

- 默认 config 在用户 Application Support 目录，`CUEFLOW_CONFIG` 或 `--config` 可覆盖。此次测试只用了临时配置，没有改变用户正式配置或 FrameLedger。
- 原生模型目录/工具路径不包含在 Git；截图与任务错误可能显示本机路径，交接证据属于本地私有材料。
- 重启时不会自动执行任何已恢复任务，也不会自动补写旧项目记录。旧版没有 `job.state.json` 的项目需要重新导入/CLI 重跑；输入丢失或上传不完整应重新上传。
- 取消是协作式停止；媒体处理和探测进程被跟踪，少数短小版本/环境检查仍受原有超时限制。保留已完成检查点，半写输出不能当完成结果。
- 审校 session 与未保存编辑仍不能跨服务重启恢复；先保存/下载。已保存 reviewed SRT 仍可人工导入。
- 未知 remote 模型 revision 保留 null；provenance 不冻结外部 ML 依赖，也不证明文本正确。模型运行期间需保持文件不变。
- 清理仍是永久删除六类受管项目（包含任务记录）；本轮没有执行用户目录清理。

## Claude PR 状态快照与集成顺序

以下是此前 2026-10-03 通过 GitHub CLI 只读取得的快照，**合并前必须刷新**；本轮没有重新查询远端。四个 PR 当时都是 OPEN、MERGEABLE/CLEAN，各自 head 的 Python 3.11/3.12/3.13 CI 为 SUCCESS：

| PR | head / 提交 | base |
| --- | --- | --- |
| [#1](https://github.com/ericchiu-ca/cueflow/pull/1) | `fix/review-findings` / `c755560` | `main` |
| [#2](https://github.com/ericchiu-ca/cueflow/pull/2) | `refactor/followups` / `09349a9` | `fix/review-findings` |
| [#3](https://github.com/ericchiu-ca/cueflow/pull/3) | `fix/remaining-findings` / `27c5a2e` | `refactor/followups` |
| [#4](https://github.com/ericchiu-ca/cueflow/pull/4) | `feat/resumable-jobs` / `895476a` | `fix/remaining-findings` |

旧 CI 卡点是中断上传测试依赖单次 `recv()`，`09349a9` 已改为完整读取响应并向下合入。这些绿色检查只覆盖旧 heads。

建议集成顺序 #1 → #2 → #3 → #4 → `b202163` 缓存修复 → 本轮分支。若直接为两轮本地提交建叠加 PR，未合并时 base 可用 `feat/resumable-jobs`；若祖先已经 squash/rebase 合入，依据真实 main 重新整理 diff。每次合并后复查下一 PR 的 base、head、实际 diff 与当前 CI，防止重复提交。推送、合并、tag、Release、launcher 切换或部署仍需用户授权。
