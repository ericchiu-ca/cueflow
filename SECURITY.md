# 安全政策

## 支持范围

CueFlow 是本地桌面工作流。Web 服务只支持 `127.0.0.1`、`::1` 或 `localhost`；它没有远程用户认证，不得通过反向代理、公网隧道或局域网地址暴露。

仓库当前没有 Git tag，也没有可验证的发布支持矩阵。只有当前维护分支可获得 best-effort 修复；无法从现有历史承诺任何历史版本的安全支持期限。

## 数据边界

- 视频、音频、MLX Whisper、WhisperX、ASS 生成和 FFmpeg 编码留在本机。
- Codex 翻译不会上传视频或音频。主动翻译时，请求包含字幕文本、稳定 ID、起止时间、源语言和所选模型。
- Codex 作为独立、临时的 non-interactive run 执行：空临时工作目录、`read-only` sandbox、`--ephemeral`、忽略用户配置和 exec rules，并使用 JSON Schema 限制输出。
- 保留的 `openai-api` provider 当前不会读取 API key，也不会发出 API 请求。
- 新生成的转录 `master.json` 只保存模型目录 basename，并递归把嵌套元数据字符串中的绝对本机路径替换为 `<local-path>`；旧项目经 WhisperX 重新对齐时也会清理遗留模型路径。
- ASS 项目的 `manifest.json` 只保存项目内相对输出路径，并清理预览错误中的绝对本机路径。
- `master.json` 仍包含源媒体 basename、字幕、时间、语言、质量问题和置信度信息。这些内容可能属于个人、客户或受限制数据，分享前仍必须检查。
- Web job、review session 与 CSRF token 只存在于当前进程；项目文件写入用户指定的本地 output root。

## 本地 Web 控制

- 服务端拒绝非 loopback bind，并校验请求 Host。
- 每个进程生成随机 CSRF token；所有改变状态的请求和原始上传都必须携带 token。
- 上传文件名会收敛为安全 basename，上传使用临时文件后再重命名。
- 清理只允许删除 `transcriptions`、`translations`、`reviews`、`bilingual`、`ass-assets` 和 `renders` 六个受管类别，并在活动任务存在时拒绝执行。
- 清理是递归且不可由应用恢复的本地删除；执行前必须另存需要保留的结果。

## 报告漏洞

不要在公开 issue 中提交私密媒体、字幕、凭据、绝对本机路径或可直接利用的 exploit。

优先使用 GitHub Private Vulnerability Reporting（如果仓库已启用）；否则通过仓库所有者明确提供的私密渠道联系。报告应包含：受影响 commit/版本、影响、最小复现步骤、经过脱敏的样例、环境信息和预期安全属性。

本地文件无法证明 GitHub Private Vulnerability Reporting 已启用。公开前必须由仓库所有者在 GitHub settings 中人工确认；在此之前不要把公开 issue 当作默认安全报告渠道。

## 操作要求

- 始终保持 loopback bind，不给 CueFlow 增加公网入口。
- 字幕含有机密、个人、客户或受限内容时，在发送 Codex 前完成人工授权与最小化检查。
- 不要提交 `.env`、Codex auth cache、API key、token、模型 cache、生成项目、媒体或客户字幕。
- 不把 CLI stderr、warning、`master.json` 或 manifest 未经检查地附到公开 issue。
- 只处理操作者拥有或获授权使用的媒体、字幕和模型。
- 发布前按 [docs/TESTING.md](docs/TESTING.md) 完成安全测试和隐私产物检查，并复核历史 [docs/CODE_AUDIT.md](docs/CODE_AUDIT.md) 的适用性。

## 已知剩余风险

- 当前公开仓库由经过审查的文件快照重新初始化，没有复制旧仓库的 `.git`、refs、reflog 或未引用对象。更早的仓库只保留为私有归档；已经被第三方取得的旧 clone、下载或缓存无法由本仓库撤回，私有归档不得再次公开。详见 [docs/CODE_AUDIT.md](docs/CODE_AUDIT.md)。
- 依赖没有统一 lockfile，外部命令和模型版本未固定。
- 本地服务没有持久化 audit log、远程认证或多用户隔离；这些能力也不在支持范围。
- 模型输出可能错误，prompt/schema 控制不能替代人工字幕审校。
- 自动测试不执行真实模型、YouTube、Codex 或 VideoToolbox；绿色 CI 不能证明完整生产链路安全。
