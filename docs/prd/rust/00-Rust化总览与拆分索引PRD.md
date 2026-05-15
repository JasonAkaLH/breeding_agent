# Rust 化总览与拆分索引 PRD

- **状态**：决策拆分（实施波次已冻结；MCP Phase 0 / Phase 1 基线已落地）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md`
- **目标读者**：后端维护者、Rust 实施负责人、测试负责人、运维负责人、Skill 平台维护者

## 1. 问题陈述

原 Rust 化 Runtime 模块评估 PRD 已冻结总体方向，但单个总文档不足以直接指导后续工程实施。Rust 化涉及工具链、跨语言 contract、sidecar、storage/event、Skill/MCP sandbox、artifact/file safety、auth/data access 与 rollout，各专题的风险、测试和验收方式不同，需要拆成独立 PRD。

截至 2026-05-15，MCP Runtime 已按 `docs/prd/MCP/` 进入联合 Phase 实施：Phase 0 / Phase 1 的 contract、proto、Rust sidecar skeleton、Python facade、mode gate 与 compatibility handshake 基线已落地；Phase 2-5 的 Rust Streamable HTTP / SSE、Tasks durable registry、API 事件桥接、shadow / enforce 与 Python legacy 下线仍未完成。该状态更新不改变 Rust 总体范围，也不代表其他 Wave 自动完成。

## 2. 总体目标

1. 保持 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md` 作为总决策基线。
2. 将 Rust 化拆成可独立评审、排期、开发、回滚和验收的专题 PRD。
3. 保证所有专题遵守长期交付级技术栈原则：typed protocol、health check、observability、feature flag、rollback、CI 构建、版本兼容与安全审计。
4. 保证 Rust 化不改变现有 API、SSE、artifact、Skill、MCP 与前端展示契约。

## 3. 非目标

1. 本目录不直接实现 Rust 代码；已经由 MCP Phase 1 创建的 `native/` workspace 与 `maf_mcp_runtime` 骨架只作为实施状态证据，不由本目录文档本身创建。
2. 本目录不创建新的 Cargo crate、PyO3 wheel、sidecar binary 或 CI 配置；新增 / 扩展 Rust artifact 必须进入对应专题实现 PRD 与测试计划。
3. 本目录不把 `ApiRuntime`、FastAPI route、DTO、LLM provider glue、prompt 产品语义或 UI 整体迁移为 Rust。
4. 本目录不为任何具体业务 Skill 设计专属 Rust 运行分支。

## 4. 专题拆分

| 专题 | 对应 PRD | 首要产出 | 依赖关系 |
|---|---|---|---|
| 工具链与质量门禁 | `01-Rust工具链构建发布与质量门禁PRD.md` | Rust workspace / CI / build / audit 标准 | 所有专题前置 |
| Core + Lifecycle kernel | `02-Core与LifecycleKernelPRD.md` | canonical types、状态机 transition table、Python facade | 工具链；canonical source 策略已冻结 |
| Dispatcher / Store / Event sidecar | `03-DispatcherStoreEventSidecarPRD.md` | durable runtime sidecar、event replay、lease、cursor | 工具链、Core types |
| Skill Runtime 与 Skill-owned Rust | `04-SkillRuntime与SkillOwnedRust接入PRD.md` | PyO3 policy kernel、Rust Skill Sandbox sidecar、Skill Rust adapter contract | 工具链、Core types |
| MCP Runtime sidecar | `05-MCPRuntimeRustSidecarPRD.md` + `docs/prd/MCP/` | Phase 0 / 1 sidecar 接入基线已落地；后续交付 Rust Streamable HTTP / SSE、Tasks、API bridge、shadow/enforce 与 legacy 下线 | 工具链、Core types |
| Artifact / Auth / DataAccess / Audit kernels | `06-ArtifactUploadAuthDataAccessKernelPRD.md` | path/hash/quota、auth primitives、readonly DB adapter、audit/event sanitizer | 工具链、Core types |
| Orchestration deterministic 与热点优化 | `07-OrchestrationDeterministicKernel与热点优化PRD.md` | DAG validator、scheduler policy、token/payload small kernels | 条件候选；不属于必做目标集 |

## 5. 已冻结实施波次

冻结决策：Rust 化实施顺序固定为 Wave 0 → Wave 1 → Wave 2 → Wave 3 → Wave 4；Orchestration deterministic kernel 最终归属为条件候选，不进入必做 Rust 化目标集。

| 波次 | 范围 | 成功标准 |
|---|---|---|
| Wave 0 | 工具链、workspace、CI、质量门禁 | fmt / clippy / test / nextest / audit / deny / llvm-cov 必跑门禁、maturin wheel、Cargo sidecar binary、平台基线明确；Python 不受影响 |
| Wave 1 | Core types + Lifecycle 状态机 | Python golden tests 与 Rust property tests 一致；错误码稳定 |
| Wave 2 | Dispatcher / Store / Event sidecar | event replay、lease、cancellation、bundle revision pinning 可 shadow compare |
| Wave 3 | Skill Runtime 与 MCP Runtime | untrusted input fail-closed；service binding 与 schema validation 保持双重授权 |
| Wave 4 | Artifact/Auth/DataAccess 与非 orchestration 热点优化 | path/auth/DB row shaping 等安全热点纳入 Rust；orchestration 仍为条件候选 |

MCP Runtime 因与长任务流式 SSE 共同构成最终生产能力，已经单独进入 `docs/prd/MCP/` 联合 Phase。Wave 表仍表示总体 Rust 化依赖顺序：MCP Phase 0 / 1 的提前落地不代表 Skill Runtime、Dispatcher / Store / Event、Core / Lifecycle 或 Artifact/Auth/DataAccess 已完成，也不允许跳过 MCP Phase 2-5 的退出门禁直接宣称 production enforce。

## 6. 跨专题统一要求

1. 每个 Rust kernel 必须有 Python facade 或 sidecar client，保持现有 Python API contract。
2. 每个专题必须先补行为锁定测试，再接 Rust 实现。
3. 每个专题必须使用统一 `MAF_RUST_<COMPONENT>_MODE=off|shadow|enforce` runtime config 支持回退到旧 Python path；默认 `off`，生产 `enforce` 前必须经过 `shadow`；`shadow` 差异不得影响用户结果，且进入 `enforce` 前必须满足全局最低 promotion threshold；`enforce` 失败默认 fail closed。
4. Rust panic 不得穿透 Python runtime；必须映射为稳定 typed error。
5. 所有外部输入必须 fail-closed，并有 size limit、redaction、audit。
6. 所有新增 Rust 依赖和工具必须同步记录到仓库级文档；替代冻结技术栈的依赖必须更新 PRD 并说明原因。
7. 生产 sidecar protocol 冻结为 gRPC / tonic + protobuf；HTTP JSON 只允许本地开发或极早期 spike。
8. Core/Lifecycle 接入方式冻结为 PyO3 extension；不得使用 sidecar 或 subprocess binary 作为主路径。
9. 主体 Rust workspace 目录冻结为 `native/`；Skill-owned Rust runtime 目录冻结为 `skill/<skill-name>/native/`；首批主体 crate 命名冻结为 `maf_core_types`、`maf_lifecycle`、`maf_runtime_store`、`maf_event_log`、`maf_task_dispatcher`、`maf_skill_runtime`、`maf_mcp_runtime`、`maf_artifact_store`、`maf_auth_core`、`maf_data_access`、`maf_audit_sanitizer`。`maf_orchestration_kernel` 仅作为条件候选 crate 名保留，不进入必做 Rust 化目标集创建。
10. Dispatcher / Store / Event sidecar 本专题最终交付边界是 SQLite adapter 与 PostgreSQL-compatible contract；不实现 PostgreSQL production adapter，PostgreSQL productionization 独立 PRD 推进。
11. MCP Runtime Rust 化最终接入方式冻结为独立 Rust sidecar；Python ↔ MCP sidecar 生产协议使用 gRPC / tonic + protobuf，Python 仅保留 sidecar client、config、capability descriptor 与 executor wrapper。
12. 通用 Skill Runtime 最终方案冻结为 Rust policy kernel + Rust sandbox sidecar 双层架构：policy kernel 通过 PyO3 提供 manifest / fingerprint / trust gate / allowlist 校验；不可信或进程型执行边界进入 Rust Skill Sandbox sidecar / isolated process manager。
13. Artifact/Auth/DataAccess/Audit 安全热点保持为一个聚合 PRD；内部四个子模块可分别实现、分别 feature flag、分别验收，但共享同一安全验收基线；Audit / Event sanitizer crate 归属冻结为 `maf_audit_sanitizer`。
14. 所有 Rust kernel / sidecar / 子模块统一使用 `MAF_RUST_<COMPONENT>_MODE=off|shadow|enforce` runtime config 命名规则；新增 Rust 模块不得自定义另一套 enable/disable 环境变量。
15. Sidecar 进程管理最终方案冻结为生产环境外部进程管理器 / 容器编排管理；Python runtime 不负责生产 sidecar 生命周期，只负责 client connect、health/readiness/version check、shutdown drain 协调与受限 fallback / fail-closed。dev/test 可提供 launcher。
16. shadow compare 差异处理策略冻结：`shadow` 模式下 Python legacy path 始终是用户可见结果来源；Rust 结果只用于旁路对比，差异写入脱敏 structured audit / metrics，不影响用户结果。
17. 进入 `enforce` 前必须满足全局最低 promotion threshold：至少连续 7 天且不少于 1000 次有效 shadow 样本，并同时满足 contract mismatch rate = 0、panic/crash = 0、P95 latency ≤ Python legacy 110%、error rate 不高于 legacy、audit/redaction/secret leak 测试 100% 通过、rollback 演练通过、对应 regression/cargo/clippy/fmt 全部通过。
18. `enforce` 失败处理策略冻结：Rust kernel / sidecar 失败默认 fail closed；只有对应 PRD 显式声明可 fallback，且 fallback 不会放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path。
19. protobuf schema 归属与版本策略冻结：所有生产 sidecar proto 统一位于 `native/proto/maf/<domain>/v1/`；`v1` / `v2` 是协议 schema 主版本 namespace，breaking change 必须新建 `v2` package；Rust server 与 Python client 必须从同一 proto source 生成或校验。
20. Rust dependency 技术栈冻结：`tokio`、`tonic`、`prost`、`serde`、`serde_json`、`schemars`、`thiserror`、`tracing`、`tracing-subscriber`、`pyo3`、`maturin`、`sqlx`、`uuid`、`time`、`sha2`、`hmac`、`pbkdf2`、`regex`、`jsonschema`、`proptest`、`insta`、`rstest`、`criterion`、`cargo-audit`、`cargo-deny`、`cargo-llvm-cov`、`cargo-nextest` 为长期技术栈；`axum` 仅可选用于本地 health/debug/admin endpoint；`cargo-fuzz` 对不可信输入边界强制启用。
21. Rust toolchain / edition / MSRV 策略冻结：`rust-toolchain.toml` 固定具体 stable 版本，不使用裸 `stable` channel；Cargo workspace 默认 Rust edition 2024；MSRV 等于 `rust-toolchain.toml` 固定版本；toolchain 升级必须单独 PR。
22. Orchestration Rust 化最终归属冻结：`maf_orchestration_kernel` 不属于必做 Rust 化目标集，只作为条件候选；LLM planner、router glue、provider fallback、prompt 和产品策略保留 Python；未来如需启动只能迁移 deterministic DAG / scheduler / payload policy 等可回放规则，并必须另开 PRD、提供性能或可靠性证据、通过 shadow compare。
23. CI / 发布产物矩阵冻结：任一 Rust 代码进入 `native/` 或 `skill/<skill-name>/native/` 后，必须启用 `cargo fmt`、`cargo clippy`、`cargo test`、`cargo nextest`、`cargo audit`、`cargo deny` 必跑门禁；PyO3 wheel 用 `maturin` 构建，sidecar binary 用 Cargo 构建，生产 sidecar 以 Linux container image / binary 为主；macOS arm64 是本地开发调试基线，Linux x86_64 是生产部署基线，Windows 暂不作为必需发布目标，Python 版本跟随 Python 3.13 系列。
24. Coverage / fuzz 门禁冻结：`cargo-llvm-cov` 对所有 Rust crate 必跑；普通 Rust crate 最低 line coverage 80%，安全敏感 crate 最低 line coverage 90%；`cargo-fuzz` 对 Skill manifest / sandbox policy、MCP JSON-RPC / sanitizer、artifact path / archive / filename、audit redaction / secret masking、DB readonly policy / row shaping 等不可信输入边界强制启用；fuzz 可用单独 pinned nightly toolchain，但只限 fuzz job，不改变生产 stable toolchain。
25. Dispatcher / Store / Event sidecar `enforce` 故障策略冻结：状态写入类操作失败必须 fail closed，不允许自动 fallback 到 Python legacy store；只允许 health/status、metrics、无副作用 read-only snapshot 等受限只读降级，sidecar unavailable 必须返回稳定 typed error。
26. Core / Lifecycle canonical source 策略冻结：`maf_core_types` 与 `maf_lifecycle` 是唯一 canonical source；Python `src/core` / `src/lifecycle` 只保留 facade / adapter，不得独立定义冲突 enum、默认值、状态转移规则或 error code 语义；`enforce` 后 Rust 判定为准。
27. Python facade 生成策略冻结：采用“生成 contract artifact + 手写薄 facade”的混合策略；Rust 生成 / 导出 JSON schema、error code table、enum/value snapshot、transition table snapshot 与 golden fixtures，Python facade 保持手写薄层，CI 校验二者一致。
28. Rust typed error / retry-correction policy 冻结：error code 使用 lowercase snake_case string；Rust error 必须包含 `code`、`message`、`retriable`、`category`、`safe_metadata`；自动重试必须满足 `retriable=true`、幂等、retry policy 与 audit，自动修正只允许系统生成结构化内容，安全 / 权限 / 一致性错误 fail closed。
29. Rust observability / audit / metrics / structured output validation 策略冻结：所有 Rust kernel / sidecar 的 response、audit event、metrics event、shadow diff、retry/correction event 必须按 schema / proto / contract artifact 校验；校验失败进入 typed error 与 retry / fail-closed 策略，只有 transient 且幂等场景允许自动重试；所有 tracing / metrics 必须脱敏并透传 Python trace context。
30. Rust sidecar / Python client 协议兼容与滚动升级策略冻结：启动、connect 和首次调用前必须校验 component、protocol / contract version、schema hash、error code table hash、build version 与 supported features；兼容 minor 变更允许滚动升级，breaking change 必须进入 v2 / contract major version 或 dual-stack；`enforce` 下不兼容 fail closed，`shadow` 下可回退 Python legacy path 并写 audit。
31. Rust sidecar network exposure / service discovery / security boundary 冻结：sidecar 不对公网、前端、用户、普通 Skill 或外部系统直连暴露；只允许 Python runtime / 受控内部组件经 Unix domain socket、loopback、同 Pod / 内部网络、私有服务发现或 mTLS 内网访问；endpoint 必须来自部署配置 / runtime allowlist，`enforce` 下不安全暴露或未授权访问 fail closed。
32. Rust resource limit / backpressure / deadline / cancellation 策略冻结：所有 sidecar / kernel 请求必须有 deadline；禁止无界队列、无界 stream、无界 stdout/stderr、无界 payload；各模块必须声明 max in-flight、queue、queue wait、payload size、retry、cancel、shutdown drain 与 overload typed error；默认生产基线可按模块收紧，突破 hard cap 必须单独 PRD。
33. Rust sidecar config / secrets / identity 管理策略冻结：配置只允许来自部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist；secret / token / mTLS key / 连接串不得进入 tracked 文件、audit、metrics、logs、error 或 safe metadata；跨主机访问必须 mTLS 或等价身份校验；secret rotation 通过受控 reload 或滚动重启；`enforce` 下配置缺失、identity mismatch、secret 缺失 / 过期 / 泄露风险必须 fail closed。
34. Rust build artifact provenance / SBOM / supply-chain 策略冻结：所有 PyO3 wheel、sidecar binary / image、native binary 与 Skill-owned Rust artifact 必须由 CI / 部署流水线预构建，产出 checksum、SBOM、Cargo.lock digest、toolchain / target / feature / build profile 元数据与 provenance record；runtime 只能加载 allowlist 且校验通过的 artifact，请求路径不得编译、下载或替换 Rust 产物。
35. Rust benchmark / performance regression / SLO 策略冻结：每个 Rust kernel / sidecar 必须建立 Python baseline、Rust implementation、FFI / sidecar overhead、P50/P95/P99 latency、throughput、CPU、memory 与 payload size 基线；进入 `enforce` 不得突破模块 SLO，默认 P95 不高于 Python legacy 110%，性能回归必须阻断发布。
36. Rust state migration / backup / restore / disaster recovery 策略冻结：任何 Rust-owned 持久状态、sidecar schema、event log、artifact metadata 或 bundle/runtime registry 变更必须有 schema version、migration lock、preflight、dry-run、备份、恢复、replay 校验与 rollback / roll-forward runbook；`enforce` 前必须完成 restore drill，破坏性迁移无备份一律禁止。
37. Python legacy path decommission 策略冻结：最终交付版不得长期保留双写语义；Rust 成为 canonical source 后，Python 只保留 facade / client / DTO adapter，不再保留重复状态机、store 写路径、安全策略或 sanitizer 语义。legacy 删除必须在 `enforce` 稳定窗口、回滚演练、contract drift=0 与回归通过后执行；删除后应通过 artifact / deployment rollback 恢复，不通过隐式 Python 语义 fallback。
38. Rust ops runbook / incident / rollback drill 策略冻结：任一 Rust sidecar / PyO3 kernel 进入 `enforce` 前，必须具备 dashboard、alert、SLO、health/readiness/version 诊断、drain / restart / rollback / restore 操作手册、on-call 分级与演练记录；无 runbook、无告警或无回滚演练的 Rust 模块不得进入最终生产路径。

## 7. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-SPLIT-AC-001 | 本目录 PRD 覆盖总评估 PRD 中 P0/P1/P2 Rust 化范围 | PRD 索引与范围矩阵交叉检查 |
| RUST-SPLIT-AC-002 | 每个专题都有目标、非目标、功能需求、非功能需求、验收和测试策略 | 文档审查 |
| RUST-SPLIT-AC-003 | 没有把具体业务 Skill 写入主体 Rust runtime 目标 | grep / 架构审查 |
| RUST-SPLIT-AC-004 | 后续实现待决项被归属到具体专题，而不是留在总 PRD 中阻塞 | 文档审查 |
| RUST-SPLIT-AC-005 | 38 项 Rust 化总体决策已冻结，剩余工作均进入实现 PRD / 测试计划层面 | 决策清单审查 |

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 拆分后专题互相重复 | 设计漂移 | `00` 维护边界矩阵；跨专题 contract 归属 Core types |
| 先实现后补 PRD | 失去 TDD 与评审基线 | 新增 Rust 代码前必须先更新对应专题 PRD |
| sidecar 设计过早耦合 Python runtime 细节 | 长期演进受限 | 以 typed protocol 和 compatibility policy 为边界 |
