# Rust 化专题 PRD 目录

- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md`
- **目录状态**：实施专题拆分入口
- **更新时间**：2026-05-15

本目录把后端 Rust 化 Runtime 模块评估 PRD 拆成可独立评审、实施和验收的专题 PRD。`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` 仍是总决策基线；本目录文档负责把该基线拆成后续工程落地单元。

## 拆分原则

1. 以长期交付级 Agent 系统为目标，不按临时 PoC 标准设计 Rust 边界。
2. 主体框架只 Rust 化 deterministic、并发敏感、安全敏感、可重放、可类型约束的 runtime substrate。
3. `ApiRuntime`、FastAPI routes、DTO、LLM provider glue、prompt 产品语义和 UI 不整体迁移。
4. dispatcher / store / event log 的长期目标形态是 Rust sidecar service。
5. Skill-owned Rust runtime 必须由 Skill 适配框架 contract；框架不为具体 Skill 提供专属 runtime 分支。
6. 本目录作为 PRD / 状态索引；Rust 实现位于仓库 `native/` workspace，新增或扩展 crate 必须按对应专题 PRD 与评审推进。
7. 生产 sidecar 由外部进程管理器 / 容器编排管理；Python runtime 不负责生产 sidecar 生命周期。
8. Rust toolchain 固定具体 stable 版本，不使用裸 `stable` channel；Cargo workspace 默认 edition 2024。
9. Orchestration Rust 化最终归属为条件候选，不属于必做 Rust 化目标集；未来如需启动必须另开实施 PRD 并提供性能或可靠性证据。
10. Rust CI / 发布产物矩阵已冻结：fmt、clippy、test、nextest、audit、deny 必跑；PyO3 wheel 走 `maturin`，sidecar binary 走 Cargo；macOS arm64 用于本地开发，Ubuntu 22.04.5 LTS / Linux x86_64 作为生产部署基线，Windows 非必需目标；当前 PyO3 生产 wheel gate 目标为 Python 3.13 + `manylinux_2_35` + `auditwheel check`。
11. Coverage / fuzz 门禁已冻结：所有 Rust crate 跑 `cargo-llvm-cov`；普通 crate line coverage ≥80%，安全敏感 crate ≥90%；不可信输入边界强制启用 `cargo-fuzz`。
12. Dispatcher / Store / Event sidecar `enforce` 后，状态写入类操作失败必须 fail closed，不允许自动 fallback 到 Python legacy store。
13. Core / Lifecycle canonical source 策略已冻结：`maf_core_types` / `maf_lifecycle` 是唯一来源，Python 只保留兼容 facade / adapter。
14. Python facade 生成策略已冻结：Rust 生成 contract artifact，Python 保持手写薄 facade，CI 校验一致。
15. Rust typed error / retry-correction policy 已冻结：error code 用 lowercase snake_case，自动重试受 `retriable` + 幂等 + retry policy 约束，自动修正只处理系统生成结构化内容。
16. Rust observability / structured output validation 已冻结：response、audit、metrics、shadow diff、retry/correction event 必须先 schema 校验，出错后按 typed error + retry / fail-closed 策略处理。
17. Rust protocol compatibility / rolling upgrade 已冻结：Python client / PyO3 facade 必须校验 version、schema hash、error table hash 与 feature flags；breaking change 走 v2 / contract major version 或 dual-stack。
18. Rust sidecar network exposure 已冻结：sidecar 仅内部可访问，不向公网、前端、用户、普通 Skill 或外部系统直连暴露；endpoint 必须来自部署配置 / runtime allowlist。
19. Rust resource limit / backpressure 已冻结：所有请求必须有 deadline，禁止无界队列 / stream / stdout-stderr / payload；模块必须声明并测试并发、队列、payload、retry、cancel、shutdown drain 和 overload typed error。
20. Rust config / secrets / identity 已冻结：配置只来自部署配置 / 环境变量 / secret manager / 只读配置 / runtime allowlist；secret 不进 tracked 文件、日志、audit、metrics、error；跨主机 sidecar 必须 mTLS 或等价身份校验。
21. Rust build artifact provenance / SBOM / supply-chain 已冻结：所有 Rust 产物由 CI / 部署流水线预构建，必须有 checksum、SBOM、Cargo.lock digest 与 provenance；runtime 只加载 allowlist 产物，请求路径不得编译或下载。
22. Rust benchmark / performance regression / SLO 已冻结：每个 Rust 模块必须有 Python baseline、Rust baseline、FFI / sidecar overhead 与 P50/P95/P99、CPU、memory、throughput 指标；性能回归阻断发布。
23. Rust state migration / backup / restore / DR 已冻结：任何 Rust-owned 持久状态或 schema 变更必须有 migration lock、preflight、dry-run、备份、恢复、replay 校验与 rollback / roll-forward runbook。
24. Python legacy path decommission 已冻结：最终交付版不得长期保留重复 Python 语义；Rust canonical 稳定后 Python 只保留 facade / client / DTO adapter，旧状态机 / 写路径 / 安全策略必须下线。
25. Rust ops runbook / incident / rollback drill 已冻结：进入 `enforce` 前必须具备 dashboard、alert、SLO、诊断、drain / restart / rollback / restore 操作手册和演练证据。

## PRD 索引

| 编号 | 文档 | 范围 | 状态 |
|---|---|---|---|
| 00 | `00-Rust化总览与拆分索引PRD.md` | 总体拆分、实施波次、跨专题验收 | 实施波次已冻结 |
| 01 | `01-Rust工具链构建发布与质量门禁PRD.md` | rustup / Cargo / PyO3 / sidecar 构建、CI、质量门禁 | Rust workspace 多 crate contract/kernel 基线、PRD01 Rust quality workflow、本地 gate runner、PyO3 wheel smoke、Ubuntu 22.04 x86_64 `manylinux_2_35` wheel CI 目标、artifact provenance self-check、SBOM / provenance / manifest 生成命令面、CI wheel artifact 上传配置、80% / 90% `cargo-llvm-cov` threshold runner 与 fuzz harness compile smoke 已落地；真实 CI 运行结果、coverage 实际报告 / 长时 fuzz / 真实 SBOM-provenance artifact allowlist 门禁待完成 |
| 02 | `02-Core与LifecycleKernelPRD.md` | `src/core/` contract 与 `src/lifecycle/` 状态机 Rust kernel | `maf_core_types` / `maf_lifecycle` contract artifact 与 Python facade 校验基线已落地，Core/Lifecycle 状态、取消、终态取消 no-op、超时、active task 与 interrupt reopen guard 改为 artifact 驱动；`maf_core_lifecycle_pyo3` 预构建 wheel source、Core/Lifecycle PyO3 contract handshake、`MAF_RUST_CORE_MODE` / `MAF_RUST_LIFECYCLE_MODE` enforce fail-closed、lifecycle transition / target / cancel / late-result JSON bridge 与 Ubuntu wheel smoke 配置已落地；真实 CI / 部署 provenance、benchmark、ops 与 legacy 下线仍待完成 |
| 03 | `03-DispatcherStoreEventSidecarPRD.md` | dispatcher / durable store / event log Rust sidecar | `maf_runtime_store` / `maf_event_log` / `maf_task_dispatcher` contract kernel、`maf_runtime_sidecar` service kernel / RPC-shaped adapter / tonic-prost binding / `maf-runtime-sidecar` binary 与 runtime proto 基线已落地，`RuntimeSidecarSqliteAdapter` durable SQLite event cursor / idempotency / lease / task / node / cancellation / bundle revision 持久化已落地；Python event append / replay facade 与 API/SSE replay 已消费 Rust payload/page/deadline/backpressure limits，`SQLiteStorage(runtime_sidecar_client=...)` 在 enforce 下可将 task submit、node transition 与 event append 路由到已配置 sidecar client 并禁止 Python SQLite legacy 写入；`RuntimeSidecarGrpcClient` 已可通过内部 h2c gRPC loopback、mTLS TCP 或 Unix domain socket 连接外部 Rust sidecar binary，并覆盖 version / compatibility、task/node/event、lease、cancellation token、bundle revision RPC，`build_api_runtime()` 可从 `MAF_RUNTIME_SIDECAR_ENDPOINT` 装配该 client；event log、runtime store task/node/edge/artifact、cancellation token、bundle revision pin/release、lease acquire/renew/release enforce 无 sidecar 时已 fail-closed 禁止 legacy fallback；sidecar handshake compatibility、health/readiness/drain lifecycle、endpoint allowlist、structured response envelope、idempotent retry、config source / mTLS identity、artifact provenance、benchmark report、promotion threshold、migration / DR、ops readiness 与 decommission readiness gates 已落地；task/node/event、cancellation token 与 Skill/MCP bundle revision pin/release shadow 旁路 compare 及 `runtime.sidecar_shadow_diff` 脱敏 audit 已落地；task edge / artifact metadata 独立 RuntimeSidecar RPC、SQLite adapter、Python client/storage enforce routing 与 shadow audit 已落地；RuntimeSidecar binary SBOM/provenance/manifest CI 口径与 PRD03 evidence ledger / fail-closed 校验已补齐；剩余为 production enforce rollout、7 天 production shadow promotion、真实 benchmark、ops / migration / rollback drill、deployment allowlist promotion 与 Python legacy 写路径最终下线 |
| 04 | `04-SkillRuntime与SkillOwnedRust接入PRD.md` | Rust policy kernel + Skill Sandbox sidecar + Skill-owned Rust 接入规范 | `maf_skill_runtime` policy contract/kernel 与 skill sandbox proto 基线已落地，manifest/execution facade 已消费 Rust policy artifact（含默认执行模式与 answer_mode）；`SkillSandboxService` Rust service kernel、`SkillSandboxGrpcService` tonic/prost binding 与 `maf-skill-sandbox` 二进制入口已覆盖 version / compatibility / readiness、client version range 校验、handler allowlist policy、loopback-only serve config、sandbox root 配置、相对 argv 执行、timeout、stdin 上限、stdout/stderr 并发有界 drain、`env_clear` 最小环境、Unix process-group cleanup、lingering descendant stdio bounded wait 与 path / symlink escape fail-closed；Python `SkillSandboxGrpcClient` 已可连接 Rust sandbox binary 并调用 `ValidatePolicy` / `ExecuteSandboxed` RPC，并已校验 h2c payload 精确长度、缺失/短头拒绝与 server client version range；`SkillPlatformHandlerRegistry` 在 `shadow` 下记录安全字段 / fingerprint / duration 组成的 Rust policy diff、在 `enforce` 下要求 Rust policy client 并 fail-closed 禁止 Python trust gate 放行，`SkillScriptRunner` enforce 下要求 Rust sandbox client 并禁止 Python subprocess legacy fallback；Rust policy JSON bridge、Python 预构建 PyO3 module facade / contract gate、`maf_skill_runtime_pyo3` PyO3 crate、`maturin` wheel build 与本地 import smoke、Ubuntu 22.04 x86_64 / Python 3.13 `manylinux_2_35` wheel CI 目标已落地，artifact provenance / benchmark / promotion / ops / decommission gate contract 与 Python fail-closed validator 已落地；Skill Sandbox binary SBOM/provenance/manifest CI 口径、PRD04 evidence ledger / fail-closed 校验与 enforce artifact allowlist 门禁已补齐；剩余为真实 deployment allowlist promotion、7 天 production shadow promotion、真实 benchmark、ops drill、跨平台或容器级进程树清理强化与 legacy 下线 |
| 05 | `05-MCPRuntimeRustSidecarPRD.md` | MCP protocol/runtime 独立 Rust sidecar | Phase 0 / 1 基线已落地，并补充 JSON-RPC / sanitizer / bundle / task registry kernels 与 MCP task status contract artifact；MCP Runtime sidecar binary SBOM/provenance/manifest CI 口径、PRD05 evidence ledger / fail-closed 校验与 enforce artifact allowlist 门禁已补齐；Phase 2-5 canonical runtime operations、production enforce、真实 shadow/benchmark/ops/recovery/decommission evidence 仍待完成 |
| 06 | `06-ArtifactUploadAuthDataAccessKernelPRD.md` | artifact/upload/file safety、auth primitives、readonly DB access、audit/event sanitizer 聚合 PRD | `maf_artifact_store` / `maf_auth_core` / `maf_data_access` / `maf_audit_sanitizer` safety kernel、contract metadata、`maf_safety_kernels_pyo3` PyO3 facade、Python safety facade consumption、auth fuzz target、PRD06 evidence ledger / fail-closed 校验与 CI wheel 口径已落地；真实 production shadow / benchmark / ops drill / deployment allowlist promotion 与 Python legacy 下线仍待完成 |
| 07 | `07-OrchestrationDeterministicKernel与热点优化PRD.md` | orchestration deterministic kernel 与热点小 kernel；条件候选专题 | 非必做目标集 |

## 使用规则

- 做 Rust 总体决策时，先读 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md` 与本目录 `00`。
- 做 MCP Runtime Rust sidecar 实现时，先读 `docs/prd/MCP/README.md`；MCP 长任务流式 SSE 与 Rust sidecar 必须按联合 Phase PRD 协同实现。当前只允许宣称 Phase 0 / Phase 1 接入基线已落地，完整生产级 MCP Runtime 必须等 Phase 5 enforce / legacy 下线门禁通过后才能宣称。
- 做具体 Rust 实现前，必须先把对应专题 PRD 从“待实现”细化为可开发测试计划。
- 引入任何 Rust 工具、依赖、构建脚本或 `native/` 目录时，必须同步更新 `README.md`、`AGENTS.md`、`CHANGELOG.md` 与本目录相关 PRD。
