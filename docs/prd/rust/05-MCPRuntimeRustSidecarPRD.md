# MCP Runtime Rust Sidecar PRD

- **状态**：实施中（接入方式已冻结为独立 Rust sidecar；Phase 0 / Phase 1 基线已落地；Phase 2-5 与 production enforce 待完成）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` RUST-P0-007、9.5；`docs/prd/backend/14-MCPRuntime实现需求PRD.md`；`docs/prd/backend/17-MCP长任务流式SSEPRD.md`
- **联合实施 Phase**：`docs/prd/MCP/README.md`
- **影响范围**：`src/integrations/mcp/`、`src/capabilities/mcp_tool/`、MCP sidecar client、MCP bundle activation、tool input/output validation

## 1. 问题陈述

MCP Runtime 需要处理外部 server / tools 的协议输入输出。外部 tool output 不可信，transport、JSON-RPC、schema validation、authorization error、pagination、streaming response 与 bundle activation 都适合 Rust 化为更强边界的 protocol/runtime kernel。

本专题 PRD 不再单独承载 MCP Rust 化的全部实施细节。MCP 长任务流式 SSE 与 Rust sidecar 的最终交付范围已拆入 `docs/prd/MCP/`：该目录的 Phase 0-5 是具体工程顺序、退出门禁与验收矩阵；本文保留 Rust sidecar 架构边界、Python facade 分工、非目标、安全要求与生产门禁。

## 1.1 当前实施状态口径（2026-05-15）

已落地基线：

1. `native/` Rust workspace 与 `maf_mcp_runtime` crate / sidecar binary 骨架。
2. `native/proto/maf/common/v1/` 与 `native/proto/maf/mcp/v1/` proto contract 草案。
3. health、readiness、version、compatibility handshake、typed error 与 supported features 的 Phase 1 contract skeleton。
4. Python sidecar facade、`MAF_RUST_MCP_RUNTIME_MODE=off|shadow|enforce` mode gate、endpoint allowlist、compatibility 校验、shadow fallback 与 enforce fail-closed gate。
5. Phase 0 contract artifacts / conformance matrix 与 Phase 1 facade 回归测试入口。

未完成范围：

1. Rust sidecar 内 canonical Streamable HTTP、多事件 SSE、router、request tracker、GET stream 与 reconnect。
2. Rust sidecar 内 MCP Tasks、durable long-task registry、task recovery、remote cancellation 与 final result retrieval。
3. `MCPToolExecutor` 对 Rust sidecar canonical runtime operations 的完整调用、API/SSE live event bridge 与 cancel propagation。
4. shadow 样本、promotion report、production enforce、ops runbook / rollback drill 与 Python legacy MCP duplicate semantics 下线。

因此，当前只能宣称“Rust MCP sidecar 接入骨架 / Phase 0-1 基线已落地”；不得宣称完整 Rust MCP Runtime、完整长任务流式 SSE、production enforce 或 Python legacy 下线已完成。

## 2. 目标

1. 用 Rust protocol layer 校验 MCP JSON-RPC request / response / error。
2. 固化 tool input planner allowlist、JSON Schema fail-closed 校验与 output sanitization。
3. 支持 MCP bundle 原子激活：新 bundle 准备成功后切换，失败保留旧 bundle。
4. 保持 Python capability wrapper 与 `MCPToolExecutor` 对外契约不变。
5. MCP Runtime Rust 化最终接入方式冻结为独立 Rust sidecar；Python 只保留 config、capability descriptor、executor wrapper 与 sidecar client。

## 3. 非目标

1. 不把外部 MCP tool 原始能力直接暴露给 orchestration。
2. 不把主代理 final answer 逻辑迁移为 Rust。
3. 不在本 PRD 中扩展 OAuth 或新增未冻结 MCP 功能；协议边界以 MCP Runtime PRD 已冻结范围为准。
4. 不绕过现有 capability 包装、planner allowlist 和 audit redaction。
5. 不把 MCP Runtime 做成 PyO3 主路径或普通 subprocess native binary。

## 4. 接入方式冻结

MCP Runtime Rust 化最终接入方式冻结为 **独立 Rust sidecar service**。Python 层通过 sidecar client 调用 MCP Runtime；sidecar 内部负责 MCP lifecycle、transport state、tools/list、tools/call、schema validation、output sanitization 与 bundle activation。

内部 Python ↔ MCP sidecar 的正式生产协议沿用全局 sidecar 冻结决策：**gRPC / tonic + protobuf**。MCP sidecar 对外连接 MCP server 时仍遵循 MCP 标准 transport；两者不得混淆。HTTP JSON 仅允许本地开发或极早期 spike，不得作为生产 sidecar 协议。

选择理由：MCP Runtime 面向外部 server / tools，是不可信输入和进程/transport 生命周期边界；独立 sidecar 能提供更强隔离、health/readiness、crash containment、统一 resource limit 与后续 stdio/OAuth/session 管理空间。

## 5. 功能需求

- RUST-MCP-FR-001：JSON-RPC request id、method、params、result、error 必须由 Rust protocol layer 解析和校验。
- RUST-MCP-FR-002：transport state 必须区分 initialize、initialized、list tools、call tool、shutdown 等生命周期阶段。
- RUST-MCP-FR-003：tools/list pagination 与 tool binding 必须保持 deterministic bundle 结果。
- RUST-MCP-FR-004：tool input 必须同时满足 planner allowlist 与 tool input schema；任何不匹配 fail closed。
- RUST-MCP-FR-005：tool output 必须执行 size limit、schema validation、secret redaction、URL / external content notice policy。
- RUST-MCP-FR-006：bundle activation 必须 pending -> validate -> commit，失败时保留旧 active bundle。
- RUST-MCP-FR-007：Rust error 必须映射为 Python `CapabilityExecutionResult` 可消费的稳定错误码。
- RUST-MCP-FR-008：MCP Runtime Rust 化必须以独立 sidecar service 作为最终主路径；不得以 PyO3 extension 或普通 native binary 作为主路径。
- RUST-MCP-FR-009：Python ↔ MCP sidecar 生产协议必须使用 gRPC / tonic + protobuf；HTTP JSON 不得进入生产路径。
- RUST-MCP-FR-010：MCP sidecar 必须提供 health、readiness、liveness、version、shutdown drain、structured logs 与 metrics。
- RUST-MCP-FR-011：生产环境 MCP sidecar 生命周期必须由外部进程管理器 / 容器编排管理；Python runtime 不得在生产请求路径中 spawn / restart / kill MCP sidecar。
- RUST-MCP-FR-012：MCP Runtime `enforce` failure 默认 fail closed；tool input/output schema validation、sanitization、allowlist、bundle activation 失败不得 fallback 到未校验路径。
- RUST-MCP-FR-013：`maf_mcp_runtime` 属于安全敏感 crate，line coverage 必须不低于 90%；MCP JSON-RPC、schema validation 与 output sanitizer 必须启用 `cargo-fuzz`。
- RUST-MCP-FR-014：MCP Runtime typed error 必须使用 `mcp_runtime_` 前缀；schema validation、output sanitizer、allowlist、bundle activation error 不得自动修正或重试到未校验路径。
- RUST-MCP-FR-015：MCP sidecar response、tool list、tool call result、sanitized output、bundle activation result、structured audit / metrics / retry event 必须按 protobuf / JSON schema / contract artifact 校验；校验失败必须 fail closed。
- RUST-MCP-FR-016：MCP tool 自动重试只允许在 tool 被声明为 read-only / idempotent、输入未越权、schema 与 sanitizer 均已通过、且 retry policy 明确允许时发生；side-effecting tool call 默认不得自动重试。
- RUST-MCP-FR-017：MCP sidecar 必须提供 compatibility handshake；Python executor client 必须校验 component、protocol_version、schema_hash、error_code_table_hash、build_version、supported_features 与 client version range。
- RUST-MCP-FR-018：MCP Runtime breaking change 必须进入 `maf.mcp.v2` 或 dual-stack；不得把 Python ↔ sidecar 协议版本升级与外部 MCP server transport/spec 版本混为一谈。
- RUST-MCP-FR-019：MCP sidecar inbound 只允许 Python runtime / 受控内部组件通过内部通道访问；不得被前端、用户、普通 Skill、外部 MCP server 或外部系统直连。
- RUST-MCP-FR-020：MCP sidecar endpoint 必须来自部署配置 / runtime allowlist；外部 MCP server transport 配置不得影响 Python ↔ sidecar 内部 endpoint。
- RUST-MCP-FR-021：MCP sidecar 必须执行本文档冻结的 tool call 并发、per-server 并发、queue、deadline、raw/sanitized output、stream idle timeout 与 retry 限制；禁止无界 stream 和无界 tool output。
- RUST-MCP-FR-022：MCP sidecar 的内部 endpoint、mTLS identity、外部 MCP server credentials、tool secret binding 与 bundle secret reference 必须来自部署配置 / secret manager / runtime allowlist；MCP bundle 不得携带 secret value。
- RUST-MCP-FR-023：`enforce` 下 MCP sidecar identity mismatch、外部 server credential 缺失、secret reference 未授权、证书过期或 secret 泄露风险必须 fail closed，并保留旧 active bundle。
- RUST-MCP-FR-024：MCP sidecar binary / image 必须由 CI / 部署流水线预构建，携带 checksum、SBOM、Cargo.lock digest、proto / schema hash、bundle contract hash 与 provenance；Python executor 只能连接 allowlist 中校验通过的 artifact。
- RUST-MCP-FR-025：MCP sidecar 必须建立 initialize、list_tools、call_tool、output sanitizer、bundle activation 的 Python baseline 与 Rust benchmark；P95/P99、stream idle、CPU、memory、raw/sanitized output size 必须纳入 SLO。
- RUST-MCP-FR-026：MCP bundle activation registry、tool binding cache、secret reference snapshot 或 sidecar-managed state 变更必须具备 migration lock、backup、restore 与 rollback / roll-forward runbook。
- RUST-MCP-FR-027：MCP sidecar canonical 稳定后，旧 Python MCP protocol / sanitizer / bundle activation 重复语义必须下线；最终生产只保留 Python executor facade / sidecar client。
- RUST-MCP-FR-028：MCP sidecar `enforce` 前必须具备 dashboard、alert、SLO、drain / restart / rollback、bundle quarantine、output sanitizer failure、secret / identity failure 与 external server failure 演练证据。
- RUST-MCP-FR-029：MCP sidecar 必须兼容 `docs/prd/backend/17-MCP长任务流式SSEPRD.md` 冻结的长任务 / 完整流式 SSE 行为；普通短调用 hard cap 不得阻断显式配置的长任务 stream、reconnect、task status 与 cancellation。

## 6. Python facade / executor 边界

1. Python 继续负责 runtime config 注入、capability descriptor 注册、sidecar client 初始化和最终主代理汇总。
2. Rust sidecar 负责 MCP 协议解析、transport state、schema 校验、sanitization、bundle activation 与 tool call 执行边界。
3. `MCPToolExecutor` 不应接触未经 MCP sidecar 校验和清洗的原始外部 output。
4. sidecar health 失败、protocol version 不兼容或 bundle activation 失败时，Python runtime 必须保留旧 MCP bundle 或回退旧 Python implementation。


MCP sidecar protobuf schema 必须归属 `native/proto/maf/mcp/v1/`，并复用 `native/proto/maf/common/v1/`；breaking change 必须新建 `maf.mcp.v2`。

Protocol compatibility / rolling upgrade 策略冻结：MCP sidecar readiness 必须在 Python executor client compatibility handshake 通过后才为 ready。`shadow` 阶段不兼容可保留旧 MCP bundle 或回退旧 Python implementation，并记录 `rust.protocol_incompatible`；`enforce` 阶段不兼容 fail closed。外部 MCP server 协议演进必须先由 MCP sidecar 适配并通过 bundle activation，不得绕过 Python ↔ sidecar 的内部 proto compatibility。

Runtime config 必须遵守统一命名：`MAF_RUST_MCP_RUNTIME_MODE`=off|shadow|enforce；默认 `off`，生产 `enforce` 前必须经过 `shadow`。

Shadow compare 差异处理策略冻结：`shadow` 模式下，Python legacy path 永远是用户可见结果来源；Rust kernel / sidecar 结果只用于旁路对比。差异必须写入 structured audit / metrics，至少包含 component、input fingerprint、legacy output fingerprint、rust output fingerprint、error code、duration；不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload。shadow 差异不得影响用户结果；只有差异率、错误率、性能指标达到对应专题 PRD 的 promotion threshold 后，才能进入 `enforce`。进入 `enforce` 前还必须满足全局最低 promotion threshold；本专题可更严格，不得更宽松。

Enforce 失败处理策略冻结：`enforce` 模式下 Rust kernel / sidecar 失败默认 fail closed；只有对应 PRD 显式声明可 fallback，且 fallback 不会放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path。fallback 事件必须写 structured audit。

Structured output validation 策略冻结：MCP sidecar 的 JSON-RPC 解析结果、tool schema、tool output sanitizer 结果、bundle activation 结果与 Python executor 可消费 facade result 必须先校验再使用。validation failure 返回 `mcp_runtime_` 前缀 typed error；不得把未经校验或 sanitizer 失败的 output 通过 retry / correction 送入主代理。

## 7. Sidecar 进程管理冻结

生产环境由外部进程管理器 / 容器编排管理 MCP sidecar。Python runtime 只负责 sidecar client 初始化、connect、health/readiness/version check、protocol compatibility check、shutdown drain 协调，以及 bundle activation 失败时保留旧 bundle 或 fallback。

本地开发 / 测试环境可以提供 Python launcher、脚本或 fixture 一键拉起 MCP sidecar；该 launcher 不作为生产运行方式。

Sidecar network exposure 策略冻结：MCP sidecar 的 inbound API 只允许内部访问，推荐 Unix domain socket、loopback、同 Pod / 内部网络、私有服务发现或 mTLS 内网。外部 MCP server 连接属于 sidecar 对外 MCP transport，不得反向暴露 Python ↔ MCP sidecar 内部 gRPC 端口。`enforce` 下公网绑定、未授权 client、endpoint 不在 allowlist 或跨主机未配置 mTLS 时必须 fail closed。

Resource limit / backpressure 策略冻结：

下表是普通短调用默认限制。长任务 / 完整流式 SSE 的 max duration、idle timeout、reconnect、event size、progress rate 与 cancellation 以 `docs/prd/backend/17-MCP长任务流式SSEPRD.md` 为准。

| 项 | 冻结值 |
|---|---|
| max concurrent tool calls | 16 |
| per MCP server concurrent | 4 |
| queue size | 128 |
| list_tools deadline | 10s |
| call_tool 默认 deadline | 60s |
| call_tool hard cap | 300s |
| raw tool output cap | 8MB，之后截断或拒绝，按 tool / sanitizer policy 决定 |
| sanitized output cap | 4MB |
| stream idle timeout | 30s |
| shutdown drain | 30s |
| side-effecting tool retry | 默认禁止 |

queue full、per-server concurrency exceeded、list_tools / call_tool deadline exceeded、raw output too large、sanitized output too large、stream idle timeout 必须返回 `mcp_runtime_` 前缀 typed error，并写 structured audit / metrics。side-effecting tool call 默认不得自动重试。

Config / secrets / identity 策略冻结：MCP sidecar 内部 gRPC endpoint、mTLS identity、外部 MCP server credentials、tool secret binding、OAuth/session token（如后续支持）只能来自部署配置、secret manager、只读配置或 runtime allowlist。MCP bundle 可引用 secret id / scope，但不得携带 secret value。bundle activation 必须校验 secret reference 授权；失败时保留旧 active bundle。rotation 必须重新执行 bundle validation / readiness，不得把 secret value 写入 tool output、audit、metrics、typed error 或 shadow diff。


## 8. 最终交付门禁冻结

1. 供应链：MCP sidecar binary / image 必须有 checksum、SBOM、Cargo.lock digest、proto / schema hash、bundle contract hash 与 provenance；Python executor connect 时必须校验 allowlist。
2. 性能：initialize、list_tools、call_tool、output sanitizer、bundle activation、stream idle 场景必须有 Python baseline、Rust sidecar baseline 与 P50/P95/P99 / CPU / memory / output size 指标。
3. 状态 / 容灾：bundle activation registry、tool binding cache、secret reference snapshot 如由 Rust sidecar 管理，必须有 backup、restore、migration lock 与 rollback / roll-forward runbook。
4. Python legacy 下线：Rust MCP sidecar canonical 稳定后，Python 只保留 executor facade / sidecar client，旧 protocol / sanitizer / activation 语义不得继续作为隐式 fallback。
5. 运维：sidecar unavailable、external MCP server unavailable、bundle activation failure、sanitizer failure、queue full、stream idle、secret / identity mismatch 必须有告警与演练。

## 9. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | JSON-RPC parse/error、transport state、pagination、bundle activation |
| Rust fuzz | malformed JSON、large payload、schema edge cases、output sanitizer；PR bounded smoke + nightly / release 长跑 |
| Rust coverage | `cargo-llvm-cov` line coverage ≥90% |
| Integration | externally managed MCP sidecar start/health/shutdown、dev launcher、mock MCP server lifecycle、tools/list、tools/call、gRPC client compatibility、rolling upgrade matrix、endpoint allowlist、artifact checksum / provenance validation |
| Python regression | `tests/integrations/mcp`、`tests/capabilities/mcp_tool`、`tests/api` |
| Security | output redaction、schema mismatch、allowlist denial、side-effecting tool no auto-retry、public bind / external direct-call denial、queue/deadline/output cap denial、secret reference / identity mismatch denial |
| Structured output | sidecar response / sanitized output / audit event schema validation |
| Performance | initialize / list_tools / call_tool / sanitizer / bundle activation Python baseline vs Rust P50/P95/P99、CPU、memory、output size |
| Migration / DR | bundle registry / tool binding / secret reference backup、restore、migration lock（如由 Rust 持久化） |
| Ops | dashboard / alert smoke、bundle quarantine、rollback、external server / sanitizer / identity failure drill |
| Decommission | Python MCP protocol / sanitizer / activation duplicate semantics removal guard |

## 10. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-MCP-AC-001 | MCP malformed protocol input fail closed | Rust / Python tests |
| RUST-MCP-AC-002 | bundle activation 失败不污染 active bundle | integration test |
| RUST-MCP-AC-003 | tool output 进入主代理前已 size limit + redaction | security regression |
| RUST-MCP-AC-004 | 现有 MCP executor API 行为不变 | API / e2e regression |
| RUST-MCP-AC-005 | MCP Runtime 以独立 sidecar 运行，Python 只通过 client 调用 | sidecar integration + architecture review |
| RUST-MCP-AC-006 | MCP Runtime coverage / fuzz 达到安全敏感门禁 | `cargo-llvm-cov` report + fuzz logs |
| RUST-MCP-AC-007 | MCP 结构化输出校验失败 fail closed；仅 read-only / idempotent tool 允许受控重试 | schema validation + retry/fault injection |
| RUST-MCP-AC-008 | MCP sidecar 内部 proto compatibility handshake、rolling upgrade 与不兼容 fail-closed 可验证 | compatibility matrix + bundle activation tests |
| RUST-MCP-AC-009 | MCP sidecar inbound 仅内部可访问，外部 MCP transport 不得暴露内部 gRPC | endpoint allowlist + security/failure injection |
| RUST-MCP-AC-010 | MCP sidecar 并发、per-server 队列、deadline、output cap、stream idle timeout 限制生效 | resource/backpressure tests + metrics evidence |
| RUST-MCP-AC-011 | MCP sidecar config、external server credentials、secret reference、identity 只来自允许来源，bundle secret value 被拒绝 | bundle validation tests + redaction snapshot + identity failure injection |
| RUST-MCP-AC-012 | MCP sidecar artifact checksum、SBOM、proto / schema hash、provenance 与 executor allowlist 校验可验证 | release artifact review + connect failure injection |
| RUST-MCP-AC-013 | MCP benchmark 覆盖 initialize、list_tools、call_tool、sanitizer、bundle activation 与资源指标 | benchmark report + SLO gate |
| RUST-MCP-AC-014 | Rust-owned MCP bundle registry / binding cache / secret reference state 有 backup、restore、migration lock 与 rollback / roll-forward 证据 | migration / restore tests（存在持久状态时） |
| RUST-MCP-AC-015 | MCP sidecar canonical 稳定后旧 Python protocol / sanitizer / activation 重复语义下线 | decommission PR + architecture guard |
| RUST-MCP-AC-016 | `enforce` 前具备 dashboard、alert、runbook 与 external server / sanitizer / bundle / identity failure 演练证据 | ops checklist + drill records |
| RUST-MCP-AC-017 | Rust MCP sidecar 兼容长任务 / 完整流式 SSE PRD，不用短调用 hard cap 阻断显式长任务流 | PRD 17 compatibility tests + sidecar stream tests |

## 11. 风险

| 风险 | 缓解 |
|---|---|
| MCP spec 演进导致协议层落后 | protocol version policy，MCP PRD 单独更新 |
| MCP sidecar 增加本地开发复杂度 | 生产由外部进程管理器 / 容器编排管理；本地提供一键 launcher、test fixture、health smoke 与 Python fallback |
| 外部 output 绕过 sanitization | executor 只接受 sanitized facade result |
| MCP sidecar 产物供应链不可追溯 | binary / image 必须有 checksum、SBOM、proto hash、provenance 与 allowlist 校验 |
| 旧 Python MCP 语义残留 | sidecar canonical 稳定后删除重复 protocol / sanitizer / activation 逻辑，Python 仅保留 executor facade |
