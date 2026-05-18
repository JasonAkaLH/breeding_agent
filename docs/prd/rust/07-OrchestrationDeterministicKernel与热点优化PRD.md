# Orchestration Deterministic Kernel 与热点优化 PRD

- **状态**：条件候选（非必做 Rust 化目标集）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` RUST-P1-002、RUST-P2-001、RUST-P2-002、RUST-P2-003、RUST-P2-004
- **影响范围**：`src/orchestration/` deterministic kernel、token counter、sanitizer、大 payload 处理、可选 frontend WASM

## 1. 问题陈述

orchestration 中既有 deterministic DAG / scheduler / validator，也有 LLM planner、router glue、provider fallback 和 prompt 语义。后者变化快，不应整体 Rust 化；前者只在出现明确性能或可靠性瓶颈时作为条件候选。已冻结决策：orchestration 不属于必做 Rust 化目标集，本 PRD 仅沉淀边界，避免后续误把 orchestration 整包迁移。

## 2. 目标

1. 明确 orchestration 可 Rust 化的仅限 deterministic kernel：DAG validator、scheduler policy、completion policy、backpressure、payload policy。
2. 明确不 Rust 化 LLM planner prompt、provider fallback、router glue 与产品策略。
3. 记录 token budget、message trimming、artifact/dependency sanitizer、大 payload 解析等热点小 kernel 候选。
4. 将本专题标记为条件候选；启动前必须另开 PRD，且 Core/Lifecycle、Store/Event、Skill/MCP 等基础 runtime 已稳定。

## 3. 非目标

1. 本专题不属于必做 Rust 化目标集，当前不实施 orchestration Rust 化。
2. 不引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架。
3. 不把 prompt、planner reasoning、回答策略固化为 Rust。
4. 不把 React / Ant Design UI 整体 Rust 化或 WASM 化。

## 4. 候选范围

| 模块 | 可 Rust 化部分 | 保留 Python 部分 |
|---|---|---|
| `src/orchestration/` | DAG validator、scheduler、completion policy、backpressure、payload policy | LLM planner、router glue、provider fallback、prompt |
| `src/integrations/token_counter.py` | token budget accounting、message trimming、缓存 | provider-specific tokenizer 选择 |
| `src/capabilities/main_agent/` | artifact/dependency sanitizer、Skill match index、输出 schema 校验 | prompt 文案、主代理回答策略 |
| frontend data-heavy logic | 大表格 preview、artifact JSON/CSV 解析 WASM | React UI、Ant Design 组件 |
| config bootstrap | typed config schema、secret redaction | YAML 读取入口、部署环境整合 |

## 5. 功能需求（仅在条件候选升级后适用）

- RUST-HOT-FR-001：DAG validator 必须保证 cycle、missing dependency、invalid node reference fail closed。
- RUST-HOT-FR-002：scheduler policy 必须有 deterministic priority 与 backpressure rule。
- RUST-HOT-FR-003：payload policy 必须限制 input/output size、schema、truncation metadata。
- RUST-HOT-FR-004：token budget kernel 必须支持 Python baseline compare，避免 provider tokenizer 语义漂移。
- RUST-HOT-FR-005：main-agent sanitizer 必须不改变用户可见回答语义，只处理结构化安全边界。
- RUST-HOT-FR-006：WASM 只在前端大数据解析成为实际热点后采用，且不得替代 React UI。
- RUST-HOT-FR-007：若未来启动 `maf_orchestration_kernel` 或热点小 kernel，必须遵守全局 Rust artifact provenance / SBOM / supply-chain 门禁，所有 wheel / binary / WASM artifact 均由 CI / 部署流水线预构建并通过 allowlist 校验。
- RUST-HOT-FR-008：若未来启动，必须先建立 Python / JS baseline、Rust / WASM candidate baseline、FFI / WASM bridge overhead、P50/P95/P99、CPU、memory 与 payload size 性能基线；无性能或可靠性证据不得升级为实施项。
- RUST-HOT-FR-009：若 deterministic kernel 产生 Rust-owned cache、index、artifact parse cache 或 schema snapshot，必须具备 migration lock、backup、restore 与 rollback / roll-forward runbook。
- RUST-HOT-FR-010：若 Rust kernel 成为 canonical source，重复 Python / JS deterministic 语义必须在稳定后下线；最终生产只保留 facade / adapter，不保留隐式 fallback 双语义。
- RUST-HOT-FR-011：若未来启动 sidecar / WASM / PyO3 热点 kernel，进入 `enforce` 或生产启用前必须具备 dashboard、alert、SLO、rollback / restore / disable runbook 与演练证据。

## 6. 启动条件

本专题只有在以下条件全部满足时，才允许从条件候选升级为实际实施 PRD：

1. 单独创建并评审 `maf_orchestration_kernel` 实施 PRD，不复用本文档直接开工。
2. `02-Core与LifecycleKernelPRD.md` 已完成或 contract 足够稳定。
3. `03-DispatcherStoreEventSidecarPRD.md` 已完成 shadow compare 或明确不依赖 orchestration 改造。
4. `04` / `05` 中 Skill/MCP untrusted boundary 已稳定。
5. 有性能或可靠性证据证明 deterministic DAG / scheduler / payload policy 存在真实热点，而不是凭直觉迁移。
6. 能建立 Python baseline 与 Rust candidate 的 shadow compare，且 shadow 差异不得影响用户可见结果。
7. 已补齐 artifact provenance、benchmark / SLO、state migration / DR、Python legacy decommission 与 ops runbook 五类最终交付门禁。

## 7. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | DAG validation、scheduler policy、payload policy |
| Property | random DAG、priority ordering、size/truncation invariants |
| Python golden | existing orchestration tests behavior compare |
| Performance | token trimming、payload sanitizer、大 artifact parse benchmark；Python / JS baseline vs Rust / WASM P50/P95/P99、CPU、memory、payload size |
| Supply chain | wheel / binary / WASM checksum、SBOM、provenance、allowlist denial（未来启动时） |
| Migration / DR | Rust-owned cache / index / schema snapshot backup、restore、migration lock（存在持久状态时） |
| Ops | dashboard / alert / rollback / disable drill（进入生产前） |
| Decommission | Python / JS duplicate deterministic semantics removal guard（Rust canonical 稳定后） |
| Frontend optional | WASM parse 与 JS baseline 输出一致 |

## 8. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-HOT-AC-001 | orchestration 不进入必做 Rust 化目标集 | 架构审查 / PRD 索引检查 |
| RUST-HOT-AC-002 | 若未来启动，只迁移 deterministic kernel | 架构审查 |
| RUST-HOT-AC-003 | LLM planner/prompt 不被 Rust 固化 | grep / code review |
| RUST-HOT-AC-004 | Rust kernel 与 Python baseline 行为一致 | golden tests |
| RUST-HOT-AC-005 | 性能收益有 benchmark 证据 | criterion / smoke benchmark |
| RUST-HOT-AC-006 | 若未来启动，供应链、SLO、迁移容灾、legacy 下线、运维演练门禁均已补齐 | 实施 PRD 审查 + release / ops evidence |

## 9. 风险

| 风险 | 缓解 |
|---|---|
| 过早 Rust 化 orchestration 造成迭代变慢 | 标记条件候选，必须有性能或可靠性证据且另开实施 PRD |
| prompt 语义被错误固化 | 明确 prompt/LLM glue 非目标 |
| WASM 引入前端复杂度 | 仅数据解析热点启用，UI 不迁移 |
| 条件候选绕过最终交付门禁 | 未来启动前必须另开实施 PRD，并显式继承供应链、SLO、迁移容灾、legacy 下线与运维演练要求 |

## 10. 仓库内 guard 证据

本 PRD 当前不启动 `maf_orchestration_kernel` / WASM 实现。仓库内完成项是将条件候选状态机器可读化，防止后续绕过启动条件直接 Rust 化 orchestration：

- evidence ledger：`docs/prd/rust/evidence/prd07/orchestration_hotspot_release_gates.json`
- validator：`scripts/validate_prd07_orchestration_hotspot_evidence.py --json`
- CI guard：Rust quality workflow 执行 PRD07 validator，确认当前状态为 `guarded`

若未来要启动候选，需要先另开 implementation PRD，并把 ledger 从 `conditional_candidate_not_started` 升级为 `candidate_ready_to_start`；严格模式必须证明独立实施 PRD、性能/可靠性证据、Python/JS baseline、Rust/WASM candidate baseline、shadow compare、供应链、benchmark/SLO、migration/DR、ops runbook 与 legacy decommission 计划均已就绪。
