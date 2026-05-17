# 全局变更日志

本文件是 **multi_agent_framework 仓库的总变更记录**，面向人类开发者与 AI 编码助手，用于快速理解当前工程状态、最近进展与后续入口。

> 语言：全部条目使用中文。当前记录以“最新状态优先”为准；已经解决的中间报错、重复验证和过期阻断只保留归并摘要。

---

## [Unreleased]

### 当前开发焦点：Rust Runtime 迁移按 `docs/prd/rust` 顺序推进

- 当前恢复锚点仍是 `docs/prd/rust/03-DispatcherStoreEventSidecarPRD.md`；后续应继续 PRD03，不要跳到 PRD04。
- PRD03 的 **edge / artifact sidecar coverage posture / implementation** 已明确为补独立 RuntimeSidecar RPC，并已在本地落地 task edge save/list 与 artifact metadata save/get/list：Rust proto / service kernel / SQLite adapter、Python `RuntimeSidecarGrpcClient`、`SQLiteStorage` enforce routing / shadow audit、contract artifact 与 storage/integration 回归均已接入。
- PRD03 剩余待收口项转为 production enforce rollout / 7 天 shadow promotion 证据、ops / migration / rollback drill 实际执行证据、部署 allowlist promotion 与 Python legacy 写路径最终下线；RuntimeSidecar binary 的 CI SBOM / provenance / manifest 上传口径、PRD03 evidence ledger 与 fail-closed 校验脚本已进入当前收口分支，完成真实生产证据前不要进入 PRD04。
- 最新远端绿灯证据：GitHub Actions `Rust quality gates` push run `25948082624`（commit `ed44653a9fb591da5be82366cf5f87e4458030ad`）已通过；该 run 覆盖 Ubuntu 22.04 x86_64 / Python 3.13 的 Rust quality gates、bounded fuzz smoke 与 PyO3 wheel smoke。
- 当前 Ralph goal 已按本节恢复锚点继续推进；后续若再次中断，请先读取 `CHANGELOG.md`、OMX/Ralph 状态与 `docs/prd/rust`，并以本节恢复锚点覆盖历史状态中混杂的 PRD04 残留字段。

### Rust Runtime 迁移最新进展

- **PRD01 / Rust quality gate 已形成 Ubuntu 生产基线**：CI 在 Ubuntu 22.04 x86_64 / Python 3.13 跑通 fmt、Rust 1.95 clippy、workspace test / nextest、audit、deny、coverage threshold、bounded fuzz、PyO3 wheel smoke、SBOM / provenance / manifest 等门禁。期间暴露的 PyO3 Python 版本、clippy、cargo-fuzz、cargo-deny、coverage 等阻断已逐项修复并归并为质量门禁基线，不再作为当前阻断。
- **PRD02 / Core + Lifecycle PyO3 facade 已落地**：新增 `maf_core_lifecycle_pyo3`，Python facade 支持 `off|shadow|enforce` 模式、contract / feature handshake、typed error 映射与 lifecycle transition JSON bridge；Ubuntu CI wheel smoke 已验证 Core/Lifecycle wheel build、SBOM、provenance、manifest 与 installed-module contract smoke。
- **PRD03 / RuntimeSidecar transport 已推进**：Rust sidecar 支持 loopback TCP、Unix domain socket 与 mTLS gRPC 入口；Python `RuntimeSidecarGrpcClient` 支持 loopback、`unix://` 与 `https://` mTLS endpoint，生产跨主机访问继续要求 mTLS / allowlist / fail-closed。
- **PRD03 / RuntimeSidecar shadow compare 已扩展**：`SQLiteStorage` 与 API runtime 在 shadow 模式下保留 Python legacy 用户可见结果，同时旁路调用 Rust sidecar 并写入 `runtime.sidecar_shadow_diff`；审计 payload 只保留 component、operation、fingerprint、duration、状态与 allowlisted error code，避免 secret / raw payload 泄漏。
- **PRD03 / 新增 shadow 覆盖**：cancellation token 与 Skill / MCP bundle revision pin/release 已接入 RuntimeSidecar shadow helper；sidecar 或 audit sink 失败不阻断 legacy 可见结果，enforce 路径仍保持 fail-closed。
- **仍未完成**：PRD03 enforce rollout、生产 shadow promotion 证据、ops / migration / rollback drill、远端 artifact/provenance 证据，以及 Rust canonical 稳定后的 Python legacy 写路径下线。完成这些之前不要进入 PRD04。

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
