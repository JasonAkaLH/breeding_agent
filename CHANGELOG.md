# 全局变更日志

本文件是 **multi_agent_framework 仓库的总变更记录**，面向人类开发者与 AI 编码助手，用于快速理解当前工程状态、最近进展与后续入口。

> 语言：全部条目使用中文。当前记录以“最新状态优先”为准；已经解决的中间报错、重复验证和过期阻断只保留归并摘要。

---

## [Unreleased]

- 前端临时 Skill 状态行：多 / 单 Skill 进度现在以当前页灰色轻量行显示在 assistant 气泡外，不写入聊天正文、历史或记忆；刷新 / 历史会话不恢复，最终回答仍只在 assistant 气泡内展示。
- 多 Skill final-only DAG 收口：保留显式单 Skill `requires_finalizer` 的最终回答节点，同时抑制 LLM Planner 多 Skill 宏展开中的 per-skill 中间 finalizer；全局 finalizer 会汇总所有 answer-producing skill，assistant history 同步对并发重复写入保持幂等。
- 本地全栈开发会话收尾：按需重启并最终清理 `maf-fullstack-dev` tmux 会话、uvicorn / Vite 后台进程，确认 `8000` / `5173` 端口已释放。
- Skill finalizer 输出契约修复：`answer` / `summary` 现在在 Skill executor 边界归一化为 `response_text`，覆盖 `python_subprocess` 与 `platform_service`，使 `requires_finalizer` 的主代理 dependency context 能读取 answer-only Skill 中间结果；同步更新《Codex-Skill构建指南.md》并补充 helper / executor / API 多 Skill DAG 回归测试。
- Skill 脚本上传文件边界修复：public `skill.*` 的 `python_subprocess` 执行路径现在优先消费 `skill_artifacts` 中的完整上传文件内容，LLM / finalizer prompt 仍只接收脱敏 `uploaded_artifacts` 摘要；补充 script-only artifact helper、SkillExecutor / API 回归，并验证 RCBD sample CSV 可生成 3 重复 30 行 fieldbook。

### 当前开发焦点：Rust Runtime 迁移按 `docs/prd/rust` 顺序推进

- 多 Skill DAG 回答编排修复：新增 `response_role=intermediate|final` 轻量合约、全局 finalizer 自动追加、finalizer 阶段 skill auto-match 抑制，以及 assistant history / conversation memory 优先选择全局最终回答的回归测试；保持 Artifact / SQLite schema 不变。
- PRD01-PRD07 cleanup：新增共享 `scripts/prd_evidence.py`，把 PRD03-PRD07 evidence validator 中重复的 JSON object 读取、pending gate 收集、required mapping、allowlist digest、CLI 输出与轻量模块加载逻辑收敛为 stdlib-only helper；CI path trigger 已纳入该 helper，PRD07 `provider_fallback` 仍仅作为范围排除标签，不是 runtime fallback。
- PRD07 条件候选 guard 已落地：新增 `docs/prd/rust/evidence/prd07/orchestration_hotspot_release_gates.json`、`scripts/validate_prd07_orchestration_hotspot_evidence.py`、PRD07 evidence README、CI wiring 与 integration tests；当前状态为 `guarded`，明确不创建 `maf_orchestration_kernel` / WASM artifact，未来启动必须另开 implementation PRD 并补齐性能/可靠性、baseline/shadow compare、供应链、SLO、migration/DR、ops 与 legacy decommission gate。
- 安全配置口径澄清：敏感信息不得写入、提交或推送 tracked 文件；开发 / 手工 smoke 使用本地 `config.yaml` 或部署环境变量，并由启动 bootstrap 注入 `MAF_CONFIG_*` / 专用环境变量供 runtime 消费。
- PRD06 仓库内收口：新增 `maf_safety_kernels_pyo3` PyO3 facade、PRD06 safety contract metadata、Python `rust_safety_contract` enforce/shadow facade、upload/artifact/auth/readonly SQL/DB deadline/audit sink safety facade consumption、auth fuzz target、`docs/prd/rust/evidence/prd06/safety_kernel_release_gates.json` 与 `scripts/validate_prd06_safety_kernel_evidence.py`；远端 GitHub Actions `Rust quality gates` push run `25995183561` 已通过并验证 wheel / binary artifact 生成上传链路可跑通；真实 deployment allowlist promotion、7 天 production shadow、benchmark / long-run fuzz、ops drill 与 Python legacy 下线仍作为外部证据 pending gate，不得伪造为本地完成。
- PRD03 的仓库内实现与远端 CI 收口已完成：最新远端绿灯证据为 GitHub Actions `Rust quality gates` push run `25987197322`（commit `865339e73cd1e947d2e9d0ab997f5301c74a812a`），覆盖 Ubuntu 22.04 x86_64 / Python 3.13 的 Rust quality gates、bounded fuzz smoke、PyO3 wheel smoke 与 RuntimeSidecar binary artifact 上传。PRD03 剩余 production enforce / 7 天 shadow / benchmark / ops / deployment allowlist / legacy 下线仍作为真实外部证据 pending gate，不阻塞进入 PRD04 repo-local 实施。
- 当前恢复锚点切换为 `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md`；本轮 PRD05 目标是完成仓库内 MCP Runtime sidecar release evidence、enforce artifact allowlist、fail-closed evidence ledger、测试与文档闭环，同时明确 Phase 2-5 canonical runtime operations 仍待真实实现。
- PRD04 已新增 Skill Sandbox binary CI 产物口径、`docs/prd/rust/evidence/prd04/skill_runtime_release_gates.json`、`scripts/validate_prd04_skill_runtime_evidence.py` 与 `MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH` / `MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH` enforce artifact trust gate；真实 deployment allowlist promotion、7 天 shadow、benchmark、ops drill、跨平台/容器级 process cleanup 强化与 Python legacy trust/subprocess 下线仍不得用本地合成证据替代。
- PRD04 CI 收口追加修复：`scripts/validate_prd04_skill_runtime_evidence.py` 改为轻量加载 stdlib-only 的 Skill Runtime gate helper，避免 Rust quality workflow 在未安装 PyYAML 等 Python app 依赖时因 `src.integrations.__init__` 副作用失败；新增 `python -S` 回归覆盖该场景。
- PRD01-PRD04 cleanup：`src/api/runtime.py` 中 RuntimeSidecar 与 Skill Sandbox artifact trust 的 JSON 加载、allowlist digest 收集与 exact manifest 匹配已收敛为共享 helper，并新增 API 层 allowlist helper 回归；PRD03/PRD04 的 pending external evidence fail-safe 语义保持不变。
- PRD05 已新增 MCP Runtime sidecar binary CI 产物口径、`docs/prd/rust/evidence/prd05/mcp_runtime_release_gates.json`、`scripts/validate_prd05_mcp_runtime_evidence.py`、`src/integrations/mcp/mcp_runtime_gates.py` 与 `MAF_RUST_MCP_RUNTIME_ARTIFACT_MANIFEST_PATH` / `MAF_RUST_MCP_RUNTIME_ARTIFACT_ALLOWLIST_PATH` enforce artifact trust gate；真实 Phase 2-5 Rust canonical operations、7 天 shadow、benchmark、ops / recovery drill、deployment allowlist promotion 与 Python legacy MCP protocol/sanitizer/activation 下线仍不得用本地合成证据替代。
- PRD05 远端 CI 收口中发现 `maf_runtime_sidecar` SQLite gRPC 测试在 coverage 并发执行下临时文件名存在纳秒时间戳碰撞风险；RuntimeSidecar gRPC / SQLite 测试 helper 已追加进程内 atomic counter，避免并发测试互相删除 SQLite 文件。
- 当前 Ralph context：`.omx/context/prd05-mcp-runtime-20260517T123946Z.md`；计划与测试规格为 `.omx/plans/prd-20260517-prd05-mcp-runtime-wrapup.md` 与 `.omx/plans/test-spec-20260517-prd05-mcp-runtime-wrapup.md`。

### Rust Runtime 迁移最新进展

- **PRD01 / Rust quality gate 已形成 Ubuntu 生产基线**：CI 在 Ubuntu 22.04 x86_64 / Python 3.13 跑通 fmt、Rust 1.95 clippy、workspace test / nextest、audit、deny、coverage threshold、bounded fuzz、PyO3 wheel smoke、SBOM / provenance / manifest 等门禁。期间暴露的 PyO3 Python 版本、clippy、cargo-fuzz、cargo-deny、coverage 等阻断已逐项修复并归并为质量门禁基线，不再作为当前阻断。
- **PRD02 / Core + Lifecycle PyO3 facade 已落地**：新增 `maf_core_lifecycle_pyo3`，Python facade 支持 `off|shadow|enforce` 模式、contract / feature handshake、typed error 映射与 lifecycle transition JSON bridge；Ubuntu CI wheel smoke 已验证 Core/Lifecycle wheel build、SBOM、provenance、manifest 与 installed-module contract smoke。
- **PRD03 / RuntimeSidecar transport 已推进**：Rust sidecar 支持 loopback TCP、Unix domain socket 与 mTLS gRPC 入口；Python `RuntimeSidecarGrpcClient` 支持 loopback、`unix://` 与 `https://` mTLS endpoint，生产跨主机访问继续要求 mTLS / allowlist / fail-closed。
- **PRD03 / RuntimeSidecar shadow compare 已扩展**：`SQLiteStorage` 与 API runtime 在 shadow 模式下保留 Python legacy 用户可见结果，同时旁路调用 Rust sidecar 并写入 `runtime.sidecar_shadow_diff`；审计 payload 只保留 component、operation、fingerprint、duration、状态与 allowlisted error code，避免 secret / raw payload 泄漏。
- **PRD03 / 新增 shadow 覆盖**：cancellation token 与 Skill / MCP bundle revision pin/release 已接入 RuntimeSidecar shadow helper；sidecar 或 audit sink 失败不阻断 legacy 可见结果，enforce 路径仍保持 fail-closed。
- **仍未完成**：PRD03 production enforce rollout、生产 shadow promotion 证据、ops / migration / rollback drill、deployment allowlist promotion，以及 Rust canonical 稳定后的 Python legacy 写路径下线；这些都作为真实外部证据 pending gate 管理，不应伪造为本地完成。

### Rust / MCP / Skill 架构口径已冻结

- Rust 化总体 PRD、`docs/prd/rust` 专题文档与 backend Rust Runtime 评估 PRD 已冻结核心边界：Rust 只迁移确定性 kernel、状态写路径、sandbox / sanitizer / sidecar 等适合强约束的模块；LLM planner、prompt、provider glue 与产品策略继续留在 Python。
- Rust workspace / crate 命名、protobuf 归属、PyO3 facade 策略、sidecar 进程管理、config / secrets / identity、network exposure、resource limit、backpressure、typed error、retry/correction、observability、shadow/enforce promotion、coverage/fuzz、SBOM/provenance/allowlist 等规则已收口，当前实现应直接遵守，不再重复讨论。
- MCP Runtime 已从 Rust 化候选进入 `docs/prd/MCP/` 联合 Phase 实施：Phase 0/1 基线与 Rust sidecar compatibility handshake 已落地，Phase 2-5、长任务 Streamable HTTP/SSE、durable task registry、production enforce 与 Python legacy 下线仍需继续推进。

### 数据查询 Skill 与业务对话台近期状态

- SQLQuery 已迁移为可移除 Skill platform-service 能力，保留 `approval_variety_db` 与 `genotype_db` 两个数据库 route；泛查询触发词、品种综合概览专属逻辑、前端 SQLQuery 专属展示等已清理。
- SQLQuery 支持内部 LLM 语义路由、实时 `skill.progress` 事件、MySQL 只读配置从本地 `config.yaml` / 环境变量读取，并补充 Skill ownership、intent route、progress live recorder 等回归测试。
- 前端业务对话台已支持当前任务停止按钮、Skill 名称展示、历史会话交互、流式滚动跟随、文件/数据查询通用 artifact 渲染与全栈 dev 脚本；后端 SSE / API 已补齐关键事件时间戳与 conversation/task 生命周期保护。

### 历史基线摘要

- 主代理一期 Phase 0~8 已完成：核心 contract、SQLite 状态存储、task/node/mailbox/interrupt/cancel 生命周期、orchestration scheduler / planner / router / validator / expander、API/SSE、main_agent capability、LLM runtime、audit 与前端 v1 均已形成可测试基线。
- Skill 一等 Capability、动态加载 / 热部署、Codex Skill 兼容层、Capability 接入指南、对话上下文记忆与压缩、上传文件暂存、登录权限与用户隔离历史等能力已落地；后续修改应复用既有边界，不要回到硬编码 capability 或前端专属渲染。
- MCP 服务器开发对接指南、MCP 长任务流式 SSE PRD、MCP Phase 0-5 PRD、Rust Runtime 专题 PRD、SQLQuery Skill 化迁移计划等文档已建立；新增重大运行时或跨模块边界变更前，应先更新对应 PRD 与测试计划。

---

## 归档说明

- 旧版 CHANGELOG 曾按天记录大量中间 CI 失败、clippy 报错、PRD 冻结小步提交与重复验证命令；这些信息已归并到上面的“当前状态 / 最新进展 / 历史基线摘要”。
- 如需追溯某次具体修复，请使用 git 历史与提交信息；当前文档优先服务后续开发入口和最新状态判断。
