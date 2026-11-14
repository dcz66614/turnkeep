# Turnkeep（工作名）

**本地优先的多智能体项目工作记忆与变更恢复。**
下一位 agent 接手时，知道现在什么仍有效、什么需要重做。

> **状态：OFFLINE_RC——Linux 离线发布候选，版本 0.1.0（契约 v0.1）。**
> 这是单受信操作系统用户使用的本地 SQLite + Python 产品，**不是**
> 跨平台完成版。依赖它做其他用途之前，请先读
> [已知限制](#已知限制)与
> [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)。

Turnkeep 是同一项目上轮流工作的 agent 的共享记忆：一个 agent
留下任务、决定与依据；下一个 agent 接手；用户中途改了要求，
系统指出哪些已完成的工作已经过时，并组织一次受控重做——而不是
悄悄覆盖、悄悄复用旧结果，或把变更弄丢。

- **[已验证]** 不悄悄覆盖：同一 key 写不同值会成为显式冲突，
  冲突双方在裁决前都离开默认视图。
- **[已验证]** 变更恢复：要求一变，所有依赖它的成果都会被污染
  （AND 父节点语义）；基于旧快照的 finish 会被
  `STALE_SNAPSHOT` 拒绝；`resume` 给出差异，`rebase` 重新计算
  （最多自动 2 次，之后候选成果进入隔离区）。
- **[已验证]** 原子 finish 与幂等回执：一次 run 只提交一次；
  用同一幂等键重试会返回原回执。回执只证明本地 SQLite 数据库
  接受了这次提交（`guarantee: "local_sqlite_commit_only"`）。
- **[已验证]** 交接简报带记录 ID、来源与预算上限；简报中会脱敏。
- **[已验证]** 导出 / 导入（v1 JSONL + manifest）、Markdown 导入
  （只解析结构化条目）、过期、擦除（含残留报告）、在线备份 /
  恢复 / 诊断。
- **[实验]** 本地 HTTP 工作台（`turnkeep serve`，仅 loopback，
  每进程会话 token）与浏览器 UI（见 `docs/UX_SPEC.md`）。
- **[已验证——有限制，已披露]** 真实双客户端接力（T026）与真实模型
  评测（T042）已运行，但有限制（见下；T026：agy 一段已验证，Codex
  正向一段未运行，严格门禁仍为 NOT_RUN；T042：两次干净的真实运行，
  结论 MIXED，不作笼统胜出断言）。Windows / macOS 安装未运行。

## 60 秒演示——无需 key、无需联网、无需模型

```sh
python -m turnkeep demo --scenario export-format --no-model
python -m turnkeep demo --scenario research-correction --no-model
```

两次演示都在临时本地 SQLite 数据库上跑固定 fixture。每次演示
输出都标注 `SCRIPTED_SIMULATION_NO_MODEL_CALLS`：它只展示本地
SQLite 的正确性，不代表 MCP 集成、模型质量或外部动作安全。
见 [docs/demo.md](docs/demo.md)。更长的双 agent SDK 接力：

```sh
python examples/python/handoff.py --temp-db
```

## 安装（Linux 已验证；macOS / Windows 未运行）

需要 Python ≥ 3.11。核心只用标准库（SQLite）；不需要模型 API key、
数据库服务或 Docker。

```sh
bash scripts/install.sh
.venv/bin/python -m turnkeep doctor --json   # 诊断
.venv/bin/python -m turnkeep demo --no-model
```

`install.sh` 会在 checkout 内创建 `.venv`（设置
`TURNKEEP_WITH_MCP=1` 可同时安装 MCP 额外依赖）。不需要管理员
权限，不改系统 Python。数据在 Linux 上位于
`~/.local/share/turnkeep`（可用 `TURNKEEP_DATA_DIR` 覆盖）。
完整安装、启动 / 停止与卸载说明见
[docs/install.md](docs/install.md)。快速开始见
[docs/quickstart.md](docs/quickstart.md)。

Windows（PowerShell）与 macOS 安装脚本已存在
（`scripts/install.ps1`、`bash scripts/install.sh`），但**未运行**：
没有任何 Windows/macOS runner 执行过它们。在真实运行记录进
`docs/COMPATIBILITY.md` 之前，不要把它们当作已支持。

## 接力循环（CLI）

```sh
# 一个项目 = 一个 scope。先创建，再记一条要求。
python -m turnkeep --db /tmp/demo.sqlite3 init --scope demo
python -m turnkeep --db /tmp/demo.sqlite3 --json remember --scope demo \
  --key build.python_version --value 3.11 --kind requirement

# Agent A 开始一次 run（服务端快照，绑定当前 epoch）。
python -m turnkeep --db /tmp/demo.sqlite3 --json begin --scope demo \
  --task "prepare release"
# → {"run_id": "…", "read_epoch": 1, "records": […], "status": "open"}

# Agent B 用 CAS 纠正这条要求（需要当前 revision）。
python -m turnkeep --db /tmp/demo.sqlite3 --json correct --scope demo \
  --id <record-id> --expected-revision 1 --value 3.12

# Agent A 基于旧快照的 finish 会被拒绝，而不是悄悄写入。
python -m turnkeep --db /tmp/demo.sqlite3 --json finish --scope demo \
  --run <run-id> --key release --value "Python 3.11"
# → exit 2，{"error": {"code": "STALE_SNAPSHOT",
#      "details": {"changed_keys": ["build.python_version"], …}}}

# Agent A resume 看差异，重新计算，再 finish。
python -m turnkeep --db /tmp/demo.sqlite3 --json resume --scope demo --run <run-id>
python -m turnkeep --db /tmp/demo.sqlite3 --json begin --scope demo \
  --task "recompute release"
python -m turnkeep --db /tmp/demo.sqlite3 --json finish --scope demo \
  --run <new-run-id> --key release --value "Python 3.12" \
  --idempotency-key demo-1
# → 回执：{"record_id": "…", "commit_epoch": 3,
#         "guarantee": "local_sqlite_commit_only"}
```

上面每条命令都会由
`scripts/test_documented_commands.py --offline` 在干净临时
fixture 上真实执行——文档里的命令失败，测试就失败：

```sh
.venv/bin/python scripts/test_documented_commands.py --offline
.venv/bin/python scripts/check_docs.py --profile release
```

更多旅程（J01–J05）、recall / brief、冲突、影响与恢复见
[docs/quickstart.md](docs/quickstart.md)、
[docs/UX_SPEC.md](docs/UX_SPEC.md)。

## MCP 客户端与工作台

Turnkeep 通过 stdio 提供 9 个 MCP 工具（`begin`、`recall`、
`remember`、`correct`、`retract`、`resolve_conflict`、`resume`、
`finish`、`brief`）：

```sh
python -m turnkeep mcp --scope demo --db /path/to/turnkeep.sqlite3
```

适配器模板见
[docs/integrations/claude-code.md](docs/integrations/claude-code.md)、
[docs/integrations/codex.md](docs/integrations/codex.md)。
**模板 + 一段真实接力（T026）**：模板能解析并通过
`scripts/check_adapter.py --mode config` 检查；agy 反向一段经由
MCP 真实跑完了接力（6 次调用，finish 回执）。Codex 一段用的是
CLI 包装器而非 MCP 配置（Codex exec 不暴露 MCP 工具），且其正向
一段未运行（后端故障）——因此严格的双客户端门禁仍为 NOT_RUN。
两个 SDK 测试进程只是协议证据，绝不是两个宿主产品的替代品。

本地工作台 API（仅 loopback，每进程会话 token）：

```sh
python -m turnkeep serve --scope demo   # 输出 {"url": "http://127.0.0.1:…", "token": "…"}
```

每次请求都要带 token（`Authorization: Bearer …` 或
`X-Turnkeep-Token`）。`GET /api/v1/health` 返回
`{"status": "ok", "scope": …, "epoch": …}`。OpenAPI 快照：
`specs/local-app-api.json`。

## 数据：导出、导入、擦除、备份

```sh
# 导出一个 scope（JSONL + 每个文件带 sha256 的 manifest）。
python -m turnkeep --db /tmp/demo.sqlite3 export --scope demo --out /tmp/demo-export

# 先 dry-run，再导入全新数据库。
python -m turnkeep --db /tmp/fresh.sqlite3 --json import --format turnkeep --dry-run /tmp/demo-export
python -m turnkeep --db /tmp/fresh.sqlite3 --json import --format turnkeep /tmp/demo-export

# Markdown 导入：只解析结构化条目，普通散文绝不会被升级为事实。
python -m turnkeep import --format markdown --dry-run notes.md
```

细节见 [docs/migration.md](docs/migration.md)（Markdown 与导出
导入）、[docs/recovery.md](docs/recovery.md)（备份 / 恢复 / 崩溃
诊断）、[docs/retention.md](docs/retention.md)（retract、expire、
erase 三者的区别——erase 会把内容换成 tombstone，并报告备份、
导出等不受控残留）。

回滚：没有安装器回滚——没有任何东西装到系统里。回滚数据请用
变更前做的备份恢复（见 `docs/recovery.md`）；用擦除前的备份恢复
会让已擦除内容重现，这是符合预期的时间点行为。卸载时删除
checkout；如果连数据也不要，再删除数据目录（Linux 上是
`~/.local/share/turnkeep`）。

## 每项能力的真实状态

| 能力 | 状态 | 证据 |
|---|---|---|
| 核心：remember / 冲突 / CAS correct / retract / resolve | [已验证] | R1 结束时核心测试 110 个；`run_gate --profile core` PASS |
| 快照 begin / 原子 finish / resume / rebase | [已验证] | 含双进程竞态：1 个胜出；旧快照 finish 被拒绝 |
| 词法 recall、预算、简报 | [已验证] | 先按 scope 过滤；计数字符数，不是 token 数 |
| CLI、Python SDK、MCP（9 个工具，官方 SDK） | [已验证] | 仅协议层：SDK 客户端测试 12/12 + `mcp_smoke.py` PASS |
| 本地 HTTP 工作台 + 浏览器 UI | [已验证] | 开发版与生产构建 e2e 各 11/11 |
| 零 key 脚本演示 + 回放 | [已验证] | 8 + 7 个链式摘要事件；篡改会断链 |
| 导出 / 导入 v1、Markdown 导入 | [已验证] | 确定性重导入是 no-op；篡改 manifest 会被拒绝 |
| 过期 / 擦除 / 备份 / 恢复 / 诊断 | [已验证] | 擦除后扫描全部文本列验证 |
| 确定性评测 harness（Gate C smoke） | [已验证] | 12 次脚本尝试：none 1/3、markdown 3/3、log 3/3、turnkeep 3/3 |
| 变更回放，25 个冻结场景 | [已验证] | 真实核心 25/25；no-taint 变异 21/25、silent-overwrite 变异 22/25 |
| 真实双客户端接力（Claude Code ↔ Codex） | [已验证——有限制，已披露] | T026 混合替代方案经 Sam 于 2026-11-10 接受；agy 反向一段已在数据库中验证（6 次 MCP 调用，finish 回执）；Codex 正向一段未运行，严格门禁 NOT_RUN——见下 |
| 真实模型评测（Gate C） | [已验证——结论 MIXED] | T042 两次干净的真实运行（codex-api 354/432，agy 352/432）；turnkeep 臂零过期事实，但完成率低于文件臂——见下 |
| Windows / macOS 安装 + 旅程 | [未运行] | T043——见下 |
| 10k / 100k 记录下的性能 | [已验证——FAIL] | 门禁 FAIL，3 个阈值——见下 |

## 已知限制

完整清单见
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)。以下几条
会改变你对它的期待：

- **真实双客户端接力——已验证，有已披露的限制（T026）。** Sam 于
  2026-11-10 ~09:08 GMT 接受了混合替代方案：Codex CLI
  0.164.0-alpha.9（经桥接，GPT-5.6 Sol）+ agy 1.5.3
  （gemini-3.8-flash-low）。agy 反向一段已在数据库中验证（smoke
  通过，S3+S3c 完成，6 次真实 MCP 调用，finish 回执，3 条记录；
  见 `evidence/live-clients/run-20261109-01/SUMMARY.json`）。已披露：
  Codex 正向一段（S1/S2/S3b/S4）未运行（M365 空回答故障，自
  ~18:31Z 起），严格的 live-clients 门禁仍为 NOT_RUN。不要声称
  严格门禁已跑过，也不要声称跨工具接力已被完全证明。
- **真实模型评测——已验证，结论 MIXED（T042）。** 两次独立干净的
  真实运行：codex-api（GPT-6 Luna low，官方 OpenAI API）pilot
  43/48、confirmation 354/432（81.9%）；agy pilot 43/48、
  confirmation 352/432（81.5%）。480 次尝试总花费 $1.90。结论
  MIXED：两次运行中 turnkeep 臂的过期事实均为 0%，但完成率低于
  文件臂（codex-api 90.7% 对 97.2%；agy 88.0% 对 99.1%/97.2%）——
  按此定级，不作笼统胜出断言。受污染的桥接 Codex 路线永不引用。
  上面确定性 smoke 的数字是脚本模拟，不是模型结果；本条中的真实
  数字才是模型结果。证据打包：发布 zip 只带一份脱敏的证据摘要
  （`evidence-summary.json`）——仅凭 zip 无法独立确认每次 T042
  运行的结果。原始 trace 在源码 checkout
  （`evidence/live-evals/`）中。
- **Windows / macOS——NOT_RUN（T043）。** 安装脚本与 CI workflow
  存在，但从未在这些平台上运行过。
- **性能门禁——FAIL（T043）。** 在 Linux 7.0.0-43-generic、4 CPU、
  7.9 GB 内存、Python 3.12.3 / SQLite 3.45.1 上测得：finish p95
  在 10k 记录时 **7,760 ms**、100k 记录时 **12,288 ms**，recall
  p95 在 100k 记录时 **3,183 ms**（阈值：finish 500 ms、recall
  1,000 ms）。冷启动 159 ms 通过；1k 记录时 finish p95 442 ms、
  recall p95 76 ms 通过。原因是架构性的（`begin()` 会快照全部
  active 记录；`recall()` 在 Python 里全表扫描）。阈值没有调低，
  数据集没有缩小以求通过。如果你的 scope 很大又要求交互延迟，
  不要用这个版本。
- **外部动作原子性——NOT_SUPPORTED。** 回执只覆盖本地 SQLite
  提交。发邮件、部署、交易不会因此变成原子操作。
- **名称 / 商标——NOT_RUN。** “Turnkeep”只是工作名。2026-11-08
  查 PyPI / npm / GitHub 用户均返回 404，但
  [turnkeep.app](https://turnkeep.app/)
  是同名在营的租赁换手产品（不同市场）。商标 clearance 未跑；
  未经复查与决策，不要买域名、不要申请商标。
- **单受信操作系统用户。** scope 是正确性边界，不是防本地用户
  直接读数据库文件的安全边界。没有多租户；除 loopback 工作台
  token 外没有认证。

## 最近邻

Turnkeep 不是第一个 agent 共享记忆工具，也不声称是。它加的是
组合——显式 key 冲突、AND 父节点污染、服务端快照 + 原子 finish +
幂等回执、影响说明与可回放评测——做成一个本地优先产品；而这个
组合仍是待真实试验（T042）证明的假设，不是已证明的领先。细节、
各家强项与仍待验证的差异见
[docs/comparison.md](docs/comparison.md)、
[docs/COMPETITORS.md](docs/COMPETITORS.md)。

## 文档

- [docs/quickstart.md](docs/quickstart.md)——安装 → 第一次接力
- [docs/demo.md](docs/demo.md)——脚本演示与回放文件
- [docs/install.md](docs/install.md)——安装 / doctor / 卸载
- [docs/migration.md](docs/migration.md)——Markdown 与导出导入
- [docs/recovery.md](docs/recovery.md)——备份 / 恢复 / 诊断
- [docs/retention.md](docs/retention.md)——retract / expire / erase
- [docs/troubleshooting.md](docs/troubleshooting.md)——doctor 输出、
  常见故障、诊断导出
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)——诚实清单
- [docs/comparison.md](docs/comparison.md)——最近邻
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)——客户端 / 平台 /
  性能矩阵与证据
- [docs/EVALUATION_RESULTS.md](docs/EVALUATION_RESULTS.md)——Gate C 状态
- [specs/CORE_CONTRACT.md](specs/CORE_CONTRACT.md)——冻结契约（v0.1）

## 贡献与支持

- 许可证：MIT——见 [LICENSE](LICENSE)。（依赖 / SBOM / 安全边界
  检查另行跟踪；随发布检查补齐 `SECURITY.md`。）
- 提 issue 时请附 `turnkeep doctor --json` 的输出和失败的确切
  命令。不要粘贴真实用户数据、token 或内部日志。常见问题的
  回复草稿在 `release/announcement-drafts/issue-replies.md`——只是
  草稿，没有发到任何地方。
- 发布草稿（GitHub release、Hacker News、论坛帖子）只是
  `release/announcement-drafts/` 下的本地文件，**没有发布**。
  这个仓库不会替你发帖、私信或上传。

## 版本

0.1.0——契约 v0.1，OFFLINE_RC 范围（Linux 离线）。Python ≥ 3.11；
验证机上 SQLite 3.45.1（kit 报告写 3.12.14 / 3.53.1；本机是
3.12.3 / 3.45.1——测试仍通过）。测试套件：R1 rework 结束时
276/276 pytest（2026-11-10，仅本机开发 checkout——不是对解压后
zip 在其他机器上的断言）。
