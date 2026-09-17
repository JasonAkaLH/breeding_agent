# 统一 Agent Loop Skill Soft Binding 实施证据

日期：2026-08-28

状态：`complete_local`；仓库实现和本地自动门禁已闭合，发布级成对 UI/API 与真实租户 MCP smoke 未执行，因此不宣称 `release_complete`

## 1. Commit / branch

- 分支：`main`
- 设计基线：`81a8ec1`
- Checkpoint H 实现 HEAD：`07de2f140462f58b9d480e13eaba0d64c3aa1174`
- Checkpoint H 实现 tree：`f0611db4b2c5c44f5217e852a51d5f7c6a0932b3`
- 最终证据提交：包含本文档的 `docs(agent): close skill soft binding rollout evidence` 提交；其 identity 以 Git 历史为准，避免文档自引用 commit hash
- 环境：`main` 本地开发环境；未构建、推送或部署生产镜像
- `prod_untouched=true`

## 2. Checkpoint commits

| Checkpoint | Commit | 已闭合范围 |
|---|---|---|
| A | `8f83b81c` | pinned contract-v2 PublicSkillProfile 与唯一 canonical activation builder |
| B | `5466a5d2` | prepared execution v2-only writer、v1/v2 exact reader、Python/SQLite/Runtime Sidecar contract |
| C-D | `fd3a8220` | hint admission、原子 user+activation、auto Tool choice、pending transition、delegated instruction |
| E | `9b510880` | repository 前唯一 result projector、三层预算与 typed failure |
| F | `76193c42` | deterministic `skill_result.json` staging、outcome CAS、owner-only Artifact、recovery/janitor |
| G | `eb8e49dc` | Frontend 显式 routing intent 与一次性 Skill hint |
| H | `07de2f14` | result projection audit/metric、API 文档、deterministic E2E 与全量门禁修复 |

计划审查与收敛提交为 `8ffad3a8`，不是业务 checkpoint。

## 3. 定向与全量测试计数

### 3.1 Backend

| 门禁 | 结果 |
|---|---|
| `compileall src tests` | PASS |
| Core | 54 / 54 |
| Storage | 548 / 548；13 项环境条件 skip |
| Lifecycle | 46 / 46 |
| Integrations | 761 / 761；2 项平台条件 skip |
| Orchestration | 152 / 152 |
| Capabilities / main agent | 17 / 17 |
| Capabilities / MCP Tool | 15 / 15 |
| Capabilities / Skill Tool | 3 / 3 |
| API | 610 / 610 |
| E2E | 8 / 8 |
| Observability | 40 / 40 |
| H 最终定向回归 | 30 / 30 |
| `bioinfo-daily` 外部 Skill checkout 自测 | 6 / 6 |
| `germplasm-mcp` 主仓契约测试 | 2 / 2 |

本地 Git-ignored `skill/sql-query` 兼容 checkout 不存在，状态记录为 `not_present`，未伪报通过。

### 3.2 PostgreSQL

- Checkpoint C-D：隔离真实 PostgreSQL 15 / 15，零 skip。
- Checkpoint F：隔离真实 PostgreSQL Agent outcome CAS 5 / 5，零 skip。
- 覆盖 user+activation 原子初始化、pending transition、result/Node/Artifact/Run 同事务 CAS、exact replay 与 rollback。

### 3.3 Frontend

- focused：172 / 172。
- full：24 files / 334 tests。
- TypeScript typecheck：PASS。
- production build：PASS；只有既有的大 chunk 警告。

### 3.4 Rust / Runtime Sidecar

统一 `scripts/run_rust_quality_gates.py --run` 退出码为 0，覆盖 fmt、Clippy、workspace tests、nextest 198 / 198、cargo-audit、cargo-deny、LLVM coverage thresholds、两个 30 秒 fuzz smoke、provenance/SBOM self-check 与三个 macOS PyO3 wheel。CycloneDX 对既有 `Proprietary` SPDX 表达式的提示未导致门禁失败。

本机为 macOS，未证明 Ubuntu 22.04 / `manylinux_2_35` wheel 兼容；该项保留为发布证据缺口。

### 3.5 静态与仓库卫生

- 修改过的 Python 文件 Ruff：PASS。
- `git diff --check`：PASS。
- `dict(result.output_payload)`：生产源码零命中。
- `soft_skill_binding.decision` / `soft_skill.reasoning_delta`：生产源码零命中。
- Frontend `forced_by_slash_command` / `slash_command` Skill authority：零命中。
- 根 `docker_cmd.md`：存在、被 Git ignore、未被跟踪；未读取、暂存或修改。
- 未新增依赖、数据库表/列或 protobuf 字段。

## 4. Fault matrix

| 边界 | 已验证故障 | 预期收敛 |
|---|---|---|
| Profile / activation | hidden/source/runtime 字段、digest drift、超 envelope | fail closed，不把内部正文写入首轮上下文 |
| Prepared v2 | v1 exact read、unknown/mixed version、cross-domain digest、partial handoff | 新写仅 v2；不完整 authority 不返回成功 |
| Hint admission | 非 public/disabled/revision drift、附件或 submission 副作用前失败 | HTTP 409 `skill_hint_unavailable`，零 submission 副作用 |
| Agent initialization | user/activation 任一写点失败、CAS replay、waiting/startup recovery | all-or-zero；hint 始终为 auto Tool choice |
| Pending context | auto consume、hint/force supersede、事务故障与重放 | transition 与 Task/prepared receipt 同事务且只发生一次 |
| Result projector | 非 JSON、non-finite、surrogate、复杂度、敏感 authority、超预算 | closed typed failed outcome，无 raw 直通 |
| Result staging | 写入/manifest/envelope 失败、CAS loser、response loss | CAS 前不可发现；winner/replay identity 与 bytes 不变 |
| Terminal state | result/Node/Run CAS fault、terminal event 写失败、startup recovery | 无 terminal Node + reserved result；event 可确定性补齐 |
| Janitor | registered、reserved、recoverable、nonterminal、未满 24h、identity drift | fail-safe 保留；只清理 closed orphan/manifest |
| Artifact access | foreign owner、猜 storage key、non-regular file、size/SHA drift | owner-bound 404/拒绝；正常 `skill_output` / `skill_result` 可下载 |
| Audit / metric | audit writer失败、五种 projection mode、payload leak scan | 业务路径 fail-open；审计只含七个低敏字段 |
| Frontend | 非法 routing/capability/MCP 组合、submit 成功/失败、键盘与 Interrupt gate | 发 HTTP 前拒绝非法组合；一次性 hint 清除且交互不回归 |

## 5. 核心业务验收

Deterministic E2E 使用固定 fixture，不依赖实时网络数量：

- `conv-bioinfo-informational`：Task completed；Tool call、Skill Node、fake network call 均为 0；回答只依赖 pinned profile。
- `conv-bioinfo-execution`：模型只调用一次 `skill.bioinfo_daily`；fixture 固定 28 篇并含重复的顶层/`structured_content` 数组；完整 28 篇只保存为一份 `skill_result.json`，safe result 为 `artifact_backed` 且不超过 80,000 bytes；Task、Node、Tool result、final answer 和 Artifact 均完成，无 reserved 或 `execution_crash`。
- 最终回答明确提示完整结果在 Artifact，不声称已分析未进入 model view 的记录。
- `germplasm-mcp` informational / execution 语义由 Agent/Skill/MCP 自动回归覆盖；informational 零 MCP 调用，execution 复用现有 approval/dispatch 与单一 activation。

上述 conversation ID 是测试 fixture identity，不是真实部署 Task ID。

## 6. 真实 smoke 摘要

在本地外部 Skill checkout 直接执行 `bioinfo-daily`，查询 2026-08-21 至 2026-08-28、`max_results=1`：

- 产物：`pubmed_results.json`
- top-level keys：`articles`、`search_summary`
- 实际 article count：1
- 文件大小：9,226 bytes
- SHA-256：`38ce2ae0f4404f21551fba1dbe2d1d952212b77c0aaccb4968f0278074ad8ad2`
- 未把文章正文、绝对路径、查询响应或凭据写入本证据

该命令是外部 Skill executor 直连 smoke，不经过成对启动的 Web UI/API/Runtime Sidecar，因此没有 Task ID，也不替代 H6 的发布级 smoke。

## 7. 发布 / 回滚检查

- 新 Frontend、backend 与 Runtime Sidecar 必须成对发布；本次没有执行镜像重建、推送或部署。
- 发布前仍须确认 prepared-only、partial Interrupt、recoverable v2 与 orphan reserved result 的两侧脱敏计数均为 0。
- startup 顺序已由自动回归锁定为 Agent run/result recovery 先于 staged-result janitor。
- 回滚不得让旧 reader 接触在途 v2；不得删除 v2 bytes、AgentItem、Task、Message、Artifact、audit 或 raw result。
- projector 回滚只能使用 bounded inline / typed failure safe mode，禁止恢复 raw `dict(output_payload)` 直通。
- `skill_result` 不参与 `skill_output` supersede，MCP raw result 不进入该下载通道。
- 当前分支是 `main`；未修改或部署 `prod`。

## 8. 已知 gap

1. 未成对启动本地 backend、frontend 与 Runtime Sidecar 做浏览器/API Task smoke；因此没有 picker/Slash/Interrupt/restart 的真实 Task ID。
2. 未配置真实租户 MCP authority，未执行 `germplasm-mcp` 的真实网络/approval/dispatch smoke；已有 deterministic 与完整 MCP regression，不将其写成真实 smoke。
3. 没有第二真实用户会话做 Artifact 404 手工 smoke；owner、猜 key、regular file、size/SHA 边界由 HTTP 自动回归覆盖。
4. macOS 完整 Rust gate 通过，但 Ubuntu 22.04 / `manylinux_2_35` wheel smoke 未执行。
5. `skill/sql-query` 兼容 checkout 不存在，未执行其独立测试。

这些 gap 不阻断本次“仓库实现、本地自动门禁、文档与检查点提交”的授权目标，但阻止宣称发布级真实验收完成。

## 9. Design 完成条件复审

| 条件 | 结论 |
|---|---|
| 1～14：hint、authority/recovery、profile、projector、Artifact、typed failure、delegated 与 MCP 不变 | 自动化、真实 PostgreSQL、真实 Runtime Sidecar 和外部 Skill直连证据已闭合 |
| 15：前后端/后端/Rust门禁与实际本地 smoke | 自动门禁闭合；发布级成对 UI/API 与真实租户 MCP smoke 未闭合，不能宣称完整通过 |
| 16：文档、索引、CHANGELOG一致 | 本最终证据提交闭合 |
| 17：`prod` 未修改或部署 | PASS；`prod_untouched=true` |

最终判定：`repository_implementation=complete`，`release_acceptance=not_claimed`，`deployment_performed=false`，`prod_untouched=true`。

License Requirement：复用现有 Python、Rust/Runtime Sidecar、FastAPI/Pydantic、React/TypeScript、Agent Loop、PublicSkillProfile、MCP projection budget、managed Artifact store 与 Skill runtime；无新增第三方依赖或许可类型。
