# CueFlow 代码与公开基线审计

- 初次安全审计记录日期：2026-08-25
- 公开基线复核日期：2026-09-01
- 范围：Python CLI、本地 HTTP server、浏览器 UI、外部命令、生成文件生命周期、Codex 翻译边界、package、字体、测试与 Git 公开边界

> 当前公开仓库从经过审查的文件快照重新初始化，没有导入此前仓库的 Git 对象。因此，本文件能够描述当前实现和保存下来的审计结论，但不能从当前公开历史独立还原修复前代码、旧 commit 或逐项引入顺序。当前验证见 [TESTING.md](TESTING.md)，剩余风险见 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [../SECURITY.md](../SECURITY.md)。

## 已确认的当前安全边界

| 边界 | 当前处理 |
| --- | --- |
| 本地 Web 暴露 | 只允许 loopback bind，并校验 Host header |
| 跨站写操作 | 每个 POST 和 raw upload 要求随机进程级 CSRF token |
| 清理范围 | 只删除 CueFlow 六个受管类别，保留 output root 中的无关文件 |
| 字幕 prompt injection | Codex 在空临时目录、read-only sandbox、忽略用户配置/rules、无 connector、schema-constrained output 下运行 |
| FFmpeg 输出 | stdout/stderr 合并为单一持续读取 stream，并只保留有界 diagnostic tail |
| 上传中断 | 上传使用临时文件后原子重命名；失败任务标为 failed |
| 模型路径 | 默认值从当前用户 home 推导，可由环境变量或 CLI 覆盖；生成元数据只保留 basename 并清理嵌套绝对路径 |
| 安装与资源 | package metadata、console entry point、静态资源、字体许可证和 CI 均在仓库中声明 |
| 外部命令 | 使用 argument array，不使用 `shell=True`；YouTube 命令有 timeout 和 runnable version check |

## 2026-09-01 文件快照复核

- Codex prompt 包含 `id`、`start`、`end`、`text`；请求还指定 source language 与 model。UI、Web 环境状态、README 和 SECURITY 已统一披露这些字段。
- 新生成的 `master.json` 只保存 `model_path` 和 `large_v3_model_path` 的目录 basename，并递归清理 warning、质量问题、置信度等嵌套元数据字符串中的绝对路径。
- 旧 `master.json` 经 WhisperX 重新对齐时会把遗留模型路径转成 basename，并清理其余嵌套绝对路径；原始旧文件不会被原地改写。
- Web 环境状态和转录 resolver 统一使用 `SUBFLOW_MLX_MODEL`。
- 4:3 ASS 底边距只在最终脚本坐标换算时舍入；两个回归用例均得到 `MarginV=86`。
- Python 3.11 和 3.12 在允许 loopback 的环境各有 51/51 项测试通过；Python 3.11 wheel 构建成功。
- tracked file 文本模式检查没有发现实际开发者 `/Users/...`、`/Volumes/...`、`/home/...` 路径或 `.local` author email。测试中的通用伪路径用于验证脱敏逻辑，不对应真实用户。
- Gitleaks 对准备提交的文件快照扫描未发现凭据；这不替代 GitHub secret scanning 或人工检查。
- 当前公开字体均有随附许可证或修改 notice；是否拥有最终分发权利仍需发布者负责确认。

## 全新公开 Git 边界

公开仓库采用以下迁移边界：

1. 只复制当前审查通过的 tracked 文件和未被忽略的新增文档；
2. 不复制旧 `.git`、refs、reflog、rollback checkpoint、缓存、模型或生成项目；
3. 使用 GitHub noreply email 创建单一初始 commit；
4. 旧仓库改名并设为 Private，只作为历史归档；
5. 新公开仓库使用原项目名，但拥有完全独立的 Git object graph。

该边界阻止新 clone 获取旧仓库对象，但不能撤回第三方此前已经取得的 clone、下载、截图或缓存。旧私有归档不得重新公开，也不得通过 `git push --mirror`、bundle 或复制 `.git` 的方式并入新仓库。

## 无法从当前公开历史确认

- 快照之前的 commit SHA、author/committer、分支关系和逐项变更顺序；
- 早期代码是否在每个阶段通过完整测试；
- 历史 tag、GitHub Release、正式发布日期或已发布制品；
- 旧 clone 或第三方缓存的实际分布和保留时间；
- 从未提交、已经删除或未保存的工作区内容。

## 持续审计 gate

```bash
python3.11 -m unittest discover -s tests -v
python3.11 -m pip wheel . --no-deps --no-build-isolation -w dist
gitleaks git --no-banner --redact
git diff --check
```

还必须检查当前 diff、全部 reachable history、真实生成产物、字体许可和外部服务/模型边界。发布规则见 [VERSIONING_AND_RELEASES.md](VERSIONING_AND_RELEASES.md)。
