# Dispatcher / Store / Event Rust Sidecar PRD

- **状态**：部分落地（`native/proto/maf/runtime/v1/`、`maf_runtime_store` / `maf_event_log` / `maf_task_dispatcher` contract/kernel 基线已落地，`maf_runtime_sidecar` service kernel 已开始承载 sidecar 写语义、health/readiness/drain 生命周期、RPC-shaped response envelope、tonic/prost gRPC binding 与 `maf-runtime-sidecar` 进程入口；`RuntimeSidecarSqliteAdapter` durable SQLite event cursor / idempotency / lease / task / node / cancellation / bundle revision 持久化已落地；Python event append/replay facade 已消费 Rust resource/page/deadline/backpressure limit，且 `SQLiteStorage(runtime_sidecar_client=...)` 在 enforce 模式下可将 task submit、node transition 与 event append 路由到已配置 sidecar client 并禁止 Python SQLite legacy 写入；`RuntimeSidecarGrpcClient` 已可通过内部 h2c gRPC 连接外部 Rust sidecar binary 并覆盖 version / compatibility、task/node/event、lease、cancellation token、bundle revision RPC；promotion、migration/DR、ops readiness 与 decommission evidence gates 已落地；production mTLS/Unix socket、shadow/enforce rollout 与 legacy 写路径最终下线仍待完成）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` RUST-P0-003、RUST-P0-004、8.2.1、9.3
- **影响范围**：`src/api/runtime.py` 内 dispatcher substrate、`src/storage/`、event append/replay、SSE cursor、task lease、bundle revision pinning

## 1. 问题陈述

当前 API runtime 中仍有任务运行态、事件分发、bundle revision pinning、取消 token 和 storage 调用等 runtime substrate。单进程内存态适合当前本地和一期闭环，但不适合作为长期多实例、crash recovery、durable event replay 的最终形态。

## 2. 目标

1. 将 dispatcher / durable store / event log 的长期目标形态定义为 Rust sidecar service。
2. Python `ApiRuntime` 保留 composition root 和 FastAPI dependency 职责，只作为 sidecar client/facade。
3. 支持 task lease、cancellation token、active task recovery、bundle revision pin/release、event cursor replay。
4. 本专题最终交付边界是 SQLite adapter 与 PostgreSQL-compatible contract；PostgreSQL production adapter 作为独立 PRD / 独立升级项推进。

## 3. 非目标

1. 不整体迁移 `ApiRuntime`。
2. 本专题不交付 PostgreSQL production adapter。
3. 不改变现有 API/SSE response schema。
4. 不把 LLM execution、Skill handler 或 MCP tool execution 放进 sidecar。

## 4. 目标架构

```text
FastAPI / ApiRuntime
  └─ Python RuntimeStoreClient / DispatcherClient
      └─ Rust sidecar service
          ├─ task dispatcher
          ├─ durable task/node store
          ├─ event append/replay log
          ├─ lease / idempotency / cancellation token
          └─ SQLite adapter first, PostgreSQL-compatible contract; production PostgreSQL later
```

## 5. 功能需求

- RUST-SIDE-FR-001：dispatcher / store / event sidecar 的正式协议必须使用 gRPC / tonic + protobuf；HTTP JSON 只允许作为本地开发或极早期 spike，不得作为生产协议，进入正式实现前必须迁移到 gRPC / tonic。
- RUST-SIDE-FR-002：任务提交、计划生成、节点执行状态、事件追加必须具备幂等键。
- RUST-SIDE-FR-003：dispatcher 必须支持 task lease、lease renew、lease expiry 和 active task recovery。
- RUST-SIDE-FR-004：cancel token 必须能阻止 late result 覆写 terminal state。
- RUST-SIDE-FR-005：bundle revision pin/release 必须与 task lifecycle 绑定；异常路径不得泄漏 retained revision。
- RUST-SIDE-FR-006：SSE initial replay 与 live event subscribe 必须基于同一 cursor 语义。
- RUST-SIDE-FR-007：sidecar 必须提供 health、readiness、liveness、version 与 shutdown drain。
- RUST-SIDE-FR-008：SQLite 与未来 PostgreSQL 必须保持逻辑同构；schema 变更必须有 migration policy。
- RUST-SIDE-FR-009：本专题不得实现 PostgreSQL production adapter；必须实现 SQLite adapter 与 PostgreSQL-compatible repository contract。
- RUST-SIDE-FR-010：生产环境 sidecar 生命周期必须由外部进程管理器 / 容器编排管理；Python `ApiRuntime` 不得在生产请求路径中 spawn / restart / kill sidecar。
- RUST-SIDE-FR-011：Dispatcher / Store / Event sidecar 进入 `enforce` 后，所有状态写入类操作失败必须 fail closed，不允许自动 fallback 到 Python legacy store。
- RUST-SIDE-FR-012：必须 fail-closed 的写入类操作包括 task submit / create、node state transition、event append、lease acquire / renew / release、cancellation token 写入、bundle revision pin / release。
- RUST-SIDE-FR-013：`enforce` 阶段只允许极少数只读降级，例如 health/status 查询、metrics 查询、已证明不会改变状态的 read-only snapshot；任何读降级不得产生状态写入、副作用或 cursor 推进。
- RUST-SIDE-FR-014：`enforce` 阶段 sidecar unhealthy、protocol version 不兼容或写入失败时，Python runtime 必须返回稳定 typed error，例如 `runtime_store_unavailable` / `dispatcher_unavailable`，由 API 层暴露为可重试失败，不得悄悄切回 Python 写路径。
- RUST-SIDE-FR-015：写路径自动重试只允许针对同一个 Rust sidecar 执行幂等重试，必须具备 idempotency key、max attempts、backoff / jitter、deadline 与 retry audit；重试耗尽后 fail closed。
- RUST-SIDE-FR-016：sidecar response、event append result、lease result、health/readiness/version response、structured audit / metrics / shadow diff / retry event 必须按 protobuf / contract artifact 校验；校验失败必须返回 typed error，不得消费未校验状态。
- RUST-SIDE-FR-017：结构化输出校验失败默认 `contract` 类 fail closed；仅当失败属于 transient transport / incomplete response 且原操作具备 idempotency key 时，才允许对同一个 Rust sidecar 自动重试。
- RUST-SIDE-FR-018：Python RuntimeStoreClient / DispatcherClient 必须在 connect、首次调用、reconnect 与 sidecar version 变化时执行 compatibility handshake，校验 component、protocol_version、schema_hash、error_code_table_hash、build_version、supported_features 与 client version range。
- RUST-SIDE-FR-019：Dispatcher / Store / Event sidecar 滚动升级必须支持旧 Python client / 新 sidecar 或新 Python client / 旧 sidecar 的兼容窗口；breaking change 必须进入 `maf.runtime.v2` 或 dual-stack，不得在 `enforce` 流量中混跑不兼容 v1/v2。
- RUST-SIDE-FR-020：Dispatcher / Store / Event sidecar 不得公网暴露，不得被前端、用户、普通 Skill 或外部系统直连；只允许 Python `ApiRuntime` / 受控内部组件通过 Unix domain socket、loopback、同 Pod / 内部网络、私有服务发现或 mTLS 内网访问。
- RUST-SIDE-FR-021：RuntimeStoreClient / DispatcherClient 必须校验 sidecar endpoint 来自部署配置 / runtime allowlist；`enforce` 下公网绑定、未授权 client、非 allowlist service discovery 或未配置 mTLS 的跨主机访问必须 fail closed。
- RUST-SIDE-FR-022：Dispatcher / Store / Event sidecar 必须执行本文档冻结的 max in-flight、queue、deadline、event payload、replay page、retry 与 shutdown drain 限制；禁止无界 event replay、无界 queue 或无 deadline 写入。
- RUST-SIDE-FR-023：Dispatcher / Store / Event sidecar 的 SQLite path、future PostgreSQL DSN、mTLS identity、service endpoint、storage root 与 lease owner identity 必须来自部署配置 / secret manager / runtime allowlist，不得来自用户输入、Skill manifest、LLM 输出或外部 tool output。
- RUST-SIDE-FR-024：`enforce` 下 runtime store / dispatcher identity mismatch、DB secret 缺失、DSN 泄露风险、证书过期或 client identity 未授权必须 fail closed，状态写入不得 fallback 到 Python legacy store。
- RUST-SIDE-FR-025：runtime sidecar binary / image 必须由 CI / 部署流水线预构建，携带 checksum、SBOM、Cargo.lock digest、proto hash、schema hash 与 provenance；Python client 只能连接 allowlist 中校验通过的 sidecar artifact。
- RUST-SIDE-FR-026：runtime sidecar 必须建立 task submit、state transition、event append、lease、event replay、SSE snapshot 的 Python baseline 与 Rust sidecar benchmark；P95 / P99、queue wait、CPU、memory、replay throughput 必须纳入 release gate。
- RUST-SIDE-FR-027：SQLite schema、event log、lease、cursor、bundle pin 与 future PostgreSQL-compatible contract 的任何状态变更必须具备 schema version、migration lock、preflight、dry-run、backup、restore、event replay 校验与 rollback / roll-forward runbook。
- RUST-SIDE-FR-028：runtime sidecar `enforce` 稳定后，Python storage / dispatcher 写路径必须下线；最终生产只允许 Python sidecar client / facade，不保留可隐式接管写入的 Python legacy store。
- RUST-SIDE-FR-029：runtime sidecar `enforce` 前必须具备 dashboard、alert、SLO、drain / restart / rollback / restore / replay runbook 与故障演练证据。

## 6. 数据与协议对象

正式协议冻结：gRPC / tonic + protobuf 是 production sidecar protocol；HTTP JSON 仅可用于本地开发或极早期 spike，不得进入正式生产路径。


| 对象 | 最小字段方向 |
|---|---|
| Task lease | `task_id`、`owner_id`、`revision`、`expires_at`、`renew_token` |
| Event cursor | `conversation_id`、`task_id`、`sequence`、`created_at` |
| Cancellation token | `task_id`、`requested_at`、`reason`、`terminal_policy` |
| Bundle pin | `task_id`、`bundle_kind`、`revision`、`released_at` |

具体字段以实现 PRD / protocol schema 为准，必须和 Core types 专题对齐。

## 7. PostgreSQL 延期冻结

冻结决策：Dispatcher / Store / Event sidecar 本专题最终交付边界是 SQLite adapter 与 PostgreSQL-compatible contract；不实现 PostgreSQL production adapter。PostgreSQL productionization 继续作为独立 PRD / 独立升级项推进。

当前 sidecar 仍必须保证 schema ownership、migration policy、repository contract 与错误码为未来 PostgreSQL adapter 预留兼容边界，不得因为 PostgreSQL productionization 独立推进而写死 SQLite-only 语义。

## 8. Sidecar 进程管理冻结

生产环境由外部进程管理器 / 容器编排管理 Dispatcher / Store / Event sidecar。Python `ApiRuntime` 只作为 sidecar client / facade，负责 connect、health/readiness/version check、shutdown drain 协调、protocol compatibility check 与 fail-closed / 受限只读降级；不得负责生产 sidecar 生命周期。

本地开发 / 测试环境必须提供一键 launcher 或 test fixture 拉起 sidecar，用于 integration、fault injection 与 shadow compare；该 launcher 不作为生产运行方式。

Sidecar network exposure 策略冻结：runtime sidecar 只允许内部可访问。生产推荐 Unix domain socket、loopback、同 Pod / 内部容器网络、私有服务发现或 mTLS 内网。health / readiness / metrics / debug endpoint 只能内网访问。`shadow` 阶段 endpoint 不安全时可回退 Python legacy path 并写 `rust.sidecar_exposure_denied`；`enforce` 阶段 endpoint 不安全必须 fail closed，状态写入不得 fallback 到 Python legacy store。

## 9. 最终交付门禁冻结

1. Build artifact provenance：sidecar binary / image 必须通过 CI / 部署流水线生成 checksum、SBOM、Cargo.lock digest、proto / schema hash 与 provenance；Python client connect 时必须校验 version / schema / artifact digest。
2. Performance SLO：task submit、state transition、event append、lease、event replay 与 SSE snapshot 必须有 Python baseline、Rust sidecar baseline、P50/P95/P99、queue wait、CPU、memory、throughput 指标；性能回归不得进入 `enforce`。
3. 迁移 / 容灾：SQLite schema、event log、lease、cursor、bundle pin 的 migration 必须执行 migration lock、preflight、dry-run、backup、restore、replay 校验；失败时不得接受新写入。
4. Python legacy 下线：Rust sidecar canonical 稳定后，Python storage / dispatcher 写路径必须删除；最终生产 rollback 通过 sidecar artifact / deployment rollback 或 restore 完成，不通过隐式 Python 写路径接管。
5. Ops runbook：`enforce` 前必须完成 unavailable、protocol mismatch、queue full、deadline spike、secret / identity mismatch、migration failure、crash recovery、restore / replay drill。

## 10. Rollout / rollback

1. 先以 `shadow` mode 旁路读取 / 双写对比 Python storage/event 行为；Python legacy path 始终作为用户可见结果来源，sidecar 差异只进入脱敏 audit / metrics。
2. 满足全局最低 promotion threshold 后，再按单 conversation / 单 task / 单实例灰度开启 `enforce` sidecar path。
3. `enforce` 阶段 sidecar health 失败、protocol version 不兼容、lease 异常或写入失败时，所有写路径 fail closed，并返回 `runtime_store_unavailable` / `dispatcher_unavailable` 等稳定 typed error；不得自动回退 Python legacy store 写路径。
4. `enforce` 阶段只允许 health/status、metrics、无副作用 read-only snapshot 等受限只读降级。
5. 旧 Python store/dispatcher 在稳定期前不得删除，但只能作为 `off` / `shadow` 主路径或显式人工 rollback 目标，不能在 `enforce` 写失败时自动接管。
6. `enforce` 稳定并通过 decommission gate 后，旧 Python store/dispatcher 写路径必须删除；最终生产 rollback 依赖 deployment / artifact rollback 与 restore / replay，而不是 Python 写路径 fallback。


Protobuf schema 必须归属 `native/proto/maf/runtime/v1/`，并复用 `native/proto/maf/common/v1/` 中的 shared message；breaking change 必须新建 `maf.runtime.v2`。

Protocol compatibility / rolling upgrade 策略冻结：runtime sidecar readiness 只有在 compatibility handshake 通过后才能为 ready。`shadow` 阶段不兼容可回退 Python legacy path，并记录 `rust.protocol_incompatible`；`enforce` 阶段不兼容必须返回 `runtime_store_unavailable` / `dispatcher_unavailable` 等稳定 typed error，状态写入不得 fallback 到 Python legacy store。滚动升级必须有 compatibility matrix 与 old/new client/server smoke。

Runtime config 必须遵守统一命名：`MAF_RUST_RUNTIME_STORE_MODE` / `MAF_RUST_EVENT_LOG_MODE` / `MAF_RUST_TASK_DISPATCHER_MODE`=off|shadow|enforce；默认 `off`，生产 `enforce` 前必须经过 `shadow`。

Shadow compare 差异处理策略冻结：`shadow` 模式下，Python legacy path 永远是用户可见结果来源；Rust kernel / sidecar 结果只用于旁路对比。差异必须写入 structured audit / metrics，至少包含 component、input fingerprint、legacy output fingerprint、rust output fingerprint、error code、duration；不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload。shadow 差异不得影响用户结果；只有差异率、错误率、性能指标达到对应专题 PRD 的 promotion threshold 后，才能进入 `enforce`。进入 `enforce` 前还必须满足全局最低 promotion threshold；本专题可更严格，不得更宽松。

Enforce 失败处理策略冻结：`enforce` 模式下 Rust kernel / sidecar 失败默认 fail closed。对 Dispatcher / Store / Event sidecar，本 PRD 明确禁止 task submit / create、node state transition、event append、lease acquire / renew / release、cancellation token 写入、bundle revision pin / release 等状态写入类操作自动 fallback 到 Python legacy store；只允许无状态或无副作用 read-only 查询按本文档受限降级。所有失败必须写 structured audit，并返回稳定 typed error。写路径可对同一个 Rust sidecar 做幂等自动重试，但必须由 `retriable=true`、idempotency key 与 retry policy 驱动。

Structured output validation 策略冻结：Python sidecar client 在消费 runtime sidecar response 之前，必须校验 protobuf message、typed error、event cursor、lease token、health/readiness/version 与 metrics payload。校验失败不得推进 cursor、不得提交状态、不得释放 lease；可重试时只允许针对同一个 sidecar 使用相同 idempotency key 重试，重试耗尽后 fail closed。

Resource limit / backpressure 策略冻结：

| 项 | 冻结值 |
|---|---|
| max in-flight | `min(64, cpu * 4)`，最低 8 |
| queue size | 1024 |
| queue 等待上限 | 2s |
| task submit deadline | 3s |
| state transition deadline | 2s |
| event append deadline | 2s |
| lease acquire / renew deadline | 1s |
| event replay deadline | 10s |
| event 单条 payload | 256KB |
| replay page | 1000 events 或 1MB，先到为准 |
| shutdown drain | 30s |
| enforce 写失败 fallback | 禁止，只能同 sidecar 幂等 retry |

queue full、queue wait timeout、deadline exceeded、event payload too large、replay page exceeded 必须返回 `runtime_store_` / `dispatcher_` / `event_log_` 前缀 typed error，并写 structured audit / metrics。`enforce` 下状态写入失败不得 fallback 到 Python legacy store。

Config / secrets / identity 策略冻结：runtime sidecar 的数据库连接信息、SQLite / storage path、mTLS key / cert、service identity、lease owner identity 与 endpoint 配置只允许来自部署配置、secret manager、只读配置文件或 runtime allowlist。audit / metrics / typed error 只能记录 secret fingerprint / version，不得记录 DSN、真实路径、证书私钥或 token。secret rotation 可通过受控 reload 或滚动重启完成；rotation 期间 sidecar 必须重新执行 readiness、compatibility handshake 与身份校验。

## 10.1 当前实现基线

- `src/storage/rust_contract.py` 读取 `src/storage/rust_contracts/runtime_sidecar_contract.json`，提供 operation policy、typed error 与 resource limit 访问器。
- `SQLiteCollaborationRepository.save_event_record()` 在事件落库前消费 Rust `event_append` policy、`event_payload_bytes` 限制与 `event_log_payload_too_large` typed error；超限 payload fail-closed，不进入 Python legacy 写入。
- `SQLiteCollaborationRepository.list_events_for_task()` 消费 Rust `event_replay` policy、`replay_page_events` / `replay_page_bytes` 限制与 `event_log_replay_page_exceeded` typed error；当前 Python facade 阶段拒绝无界 replay。
- `StoragePort.list_event_page_for_task()` / `SQLiteCollaborationRepository.list_event_page_for_task()` 提供按 Rust page limit 约束的分页 replay facade；API/SSE 历史回放已改为通过该分页 facade 读取，避免超过单页上限时退回无界读取。
- `src/storage/rust_contract.py` 通过 Rust contract `mode_env` 解析 runtime sidecar mode；`MAF_RUST_EVENT_LOG_MODE=enforce` 且当前尚未注入 Rust sidecar client 时，事件追加以 `event_log_unavailable` fail-closed，禁止继续写入 Python SQLite legacy path。
- `SQLiteStateRepository.save_task()` 与 `save_task_node()` 消费 Rust `task_submit` / `node_state_transition` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且当前尚未注入 Rust sidecar client 时，task / node 写入以 `runtime_store_unavailable` fail-closed，禁止继续写入 Python SQLite legacy path。
- `SQLiteStateRepository.save_task_edge()` 与 `save_artifact()` 也纳入 runtime store enforce guard：task graph edge 消费 Rust `task_submit` policy，artifact 消费 Rust `node_state_transition` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且当前尚未注入 Rust sidecar client 时，edge / artifact 写入以 `runtime_store_unavailable` fail-closed。
- `ApiRuntime` 的 Skill / MCP bundle revision retain / release 纳入 task dispatcher enforce guard：pin 消费 Rust `bundle_revision_pin` policy，release 消费 Rust `bundle_revision_release` policy；`MAF_RUST_TASK_DISPATCHER_MODE=enforce` 且当前尚未注入 Rust sidecar client 时，以 `dispatcher_unavailable` fail-closed，禁止继续写入进程内 revision pin 残留。
- `CancellationService.cancel_task_context()` 纳入 runtime store cancellation token enforce guard：取消 token 写入消费 Rust `cancellation_token_write` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且当前尚未注入 Rust sidecar client 时，以 `runtime_store_unavailable` fail-closed，禁止继续使用 Python task row 的 `cancel_requested_at` 作为 legacy cancellation token。
- `RuntimeLeaseFacade` 只提供 Rust sidecar lease facade，不持有 Python lease dict / repository；lease acquire / renew / release 分别消费 Rust `lease_acquire` / `lease_renew` / `lease_release` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且当前尚未注入 Rust sidecar client 时，以 `runtime_store_unavailable` fail-closed，禁止引入 Python legacy lease 状态。
- `ensure_sidecar_write_allowed()` 是当前 Python facade 阶段统一的 Rust contract 写路径 guard，集中校验 operation policy、component mode 与 typed error；API bundle pin/release、lifecycle cancellation token、storage event / runtime-store writes 与 lease facade 不再各自维护一套 enforce 判断。
- `validate_runtime_sidecar_handshake()` 按 Rust contract artifact 校验 sidecar compatibility handshake，包括 `component`、`protocol_version`、`schema_hash`、`error_code_table_hash` 与 `supported_features`；不兼容时以 `runtime_store_protocol_incompatible` fail-closed，为正式 RuntimeStoreClient / DispatcherClient 接入预置门禁。
- `validate_runtime_sidecar_endpoint()` 按 sidecar network exposure 冻结策略校验 endpoint，只允许 Unix socket、loopback / 内网 IP 或显式 runtime allowlist host；公网 / 非 allowlist service discovery endpoint 以 `runtime_store_unavailable` typed error fail-closed，为正式 sidecar client 接入预置连接安全门禁。
- `validate_runtime_sidecar_response()` 按 Rust sidecar contract 校验正式 client 将消费的 structured response envelope，包括 operation、event cursor、lease、task / node / cancellation / bundle response 与 typed error code / category / retriable / safe metadata；校验失败以 `runtime_store_response_invalid` fail-closed，不推进 cursor、不提交状态、不释放 lease。
- Runtime sidecar contract artifact 新增 Rust-owned retry policy，`build_sidecar_retry_plan()` 只在 retriable typed error、operation 具备 idempotency、同一 sidecar 且 idempotency key 存在时生成有界 retry plan；缺 key、跨 sidecar、达到 max attempts 或非幂等 read 操作均不自动重试。
- Runtime sidecar contract artifact 补齐 max-in-flight 公式参数与 task submit / state transition / event append / lease / event replay deadline hard caps；`runtime_sidecar_max_in_flight()` 只按 Rust artifact 计算 `min(64, cpu * 4)` 且最低 8，避免 Python 自带生产并发常量。
- Runtime sidecar contract artifact 新增 config / identity policy 与 `runtime_store_config_untrusted` typed error；`validate_runtime_sidecar_config_authority()` 只允许部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist 作为 sidecar 配置来源，拒绝用户输入、Skill manifest、LLM 输出、外部 tool output，且跨主机访问必须声明 mTLS identity 已配置。
- Runtime sidecar contract artifact 新增 artifact provenance policy 与 `runtime_store_artifact_untrusted` typed error；`validate_runtime_sidecar_artifact_provenance()` 在正式 client 连接前校验 sidecar binary / image 的 CI / deployment / allowlist 来源、checksum allowlist、Cargo.lock digest allowlist、proto hash、schema hash、SBOM digest 与 provenance attestation。
- Runtime sidecar contract artifact 新增 benchmark policy 与 `runtime_store_benchmark_invalid` typed error；`validate_runtime_sidecar_benchmark_report()` 要求 promotion 前同时提供 Python baseline 与 Rust sidecar baseline，并覆盖 task submit、node transition、event append、lease、event replay、SSE snapshot 的 P50/P95/P99、queue wait、CPU、memory 与 throughput 指标。
- Runtime sidecar contract artifact 新增 shadow → enforce promotion policy 与 `runtime_store_promotion_blocked` typed error；`validate_runtime_sidecar_promotion_readiness()` 要求进入 enforce 前满足连续 7 天、至少 1000 个 shadow 样本、contract mismatch / panic / crash 为 0、P95 不超过 Python legacy 110%、error rate 不高于 legacy，并具备 artifact、benchmark、audit/redaction、rollback、ops runbook、regression、cargo test/clippy/fmt 证据。
- Runtime sidecar contract artifact 新增 migration / DR policy 与 `runtime_store_migration_blocked` typed error；`validate_runtime_sidecar_migration_plan()` 要求 SQLite schema、event log、lease、cursor、bundle pin 状态变更前具备 schema version、migration lock、preflight、dry-run、backup、restore、event replay validation、rollback / roll-forward runbook 证据。
- Runtime sidecar contract artifact 新增 ops readiness policy 与 `runtime_store_ops_readiness_blocked` typed error；`validate_runtime_sidecar_ops_readiness()` 要求 enforce 前具备 dashboard、alert、SLO、drain / restart / rollback / restore / replay runbook，以及 unavailable、protocol mismatch、queue full、deadline spike、secret / identity mismatch、migration failure、crash recovery、restore / replay drill 证据。
- Runtime sidecar contract artifact 新增 legacy decommission policy 与 `runtime_store_decommission_blocked` typed error；`validate_runtime_sidecar_decommission_readiness()` 要求最终删除 Python storage / dispatcher 写路径前证明 canonical sidecar 稳定、legacy 写路径已移除、只保留 facade/client/DTO adapter、rollback 不走隐式 Python legacy fallback，并具备 promotion、ops、migration/DR、architecture guard 与 regression 证据。
- `native/crates/maf_runtime_sidecar` 新增 transport-independent Rust service kernel，组合 `maf_task_dispatcher`、`maf_event_log` 与 `maf_runtime_store::LeaseRegistry`，先由 Rust 持有 version / compatibility、task submit、node transition、event append/replay、lease、cancellation token 与 bundle revision pin/release 状态；后续 tonic/gRPC wrapper 与 SQLite adapter 必须委托该 kernel，不得在 Python client/facade 重新实现写语义。
- `maf_runtime_sidecar` service kernel 补齐 health / readiness / compatibility handshake / shutdown drain 生命周期语义：readiness 必须在 compatibility handshake 通过后才进入 ready，shutdown drain 后 readiness 转 not-ready、health 转 degraded，新的写路径以 `runtime_store_unavailable` fail-closed 且不推进 event cursor。
- `maf_runtime_sidecar` 新增 protobuf/RPC-shaped service adapter 与 `TypedErrorEnvelope`，先用 Rust request / response DTO 对齐 runtime proto 的 health/readiness、compatibility、append/replay、lease、cancellation token、bundle revision 等 response envelope；正式 tonic transport 接入时只应绑定该 adapter，不得在 Python facade 中重新拼装 typed error 或 cursor response。
- `maf_runtime_sidecar` 已通过 `tonic` / `prost` / `tonic-prost-build` 从 `native/proto/maf/runtime/v1/runtime.proto` 生成 Rust gRPC binding，并新增 `RuntimeSidecarGrpcService` 将 protobuf request 映射到 Rust adapter、将 Rust typed error envelope 映射回 protobuf `TypedError`；`RuntimeSidecarGrpcService::with_sqlite_adapter()` 与 `RuntimeSidecarServeConfig::build_service()` 已可把 append/replay 等 RPC 委托给 durable SQLite adapter 并通过 reopen 回放验证，`maf-runtime-sidecar --serve ... --sqlite <path>` 为外部进程配置 SQLite adapter 预留入口；当前仍未提供 production mTLS / Unix socket transport。
- `maf_runtime_sidecar` 新增 `maf-runtime-sidecar` 二进制入口，提供 `--version` 与 `--serve <addr>`，进程入口直接承载 Rust `RuntimeSidecarGrpcService`，并由 Rust `RuntimeSidecarServeConfig` 在 mTLS / 内网身份支持完成前拒绝非 loopback 监听地址；该入口为外部进程管理器 / 容器编排启动 sidecar 准备，Python 生产请求路径仍不得负责 spawn / restart / kill sidecar。
- `maf_runtime_sidecar` 新增 `RuntimeSidecarSqliteAdapter` durable SQLite adapter，覆盖 event append/replay cursor、event idempotency、task lease acquire/renew/release、task submit、node transition、cancellation token 与 bundle revision pin/release 的 reopen 后持久化，并保持重复 idempotency key 返回原始结果。
- `SQLiteStorage` 新增 `runtime_sidecar_client` 注入路径；当 runtime_store / event_log 处于 `enforce` 且 client 已配置时，task submit、node transition 与 event append 先调用 sidecar client 并通过 `validate_runtime_sidecar_response()` 校验 response envelope，成功后不写 Python SQLite legacy 表；未配置 client 时仍按 typed error fail-closed。
- `RuntimeSidecarGrpcClient` 新增无需额外 Python 依赖的内部 h2c gRPC unary client，连接前校验 endpoint allowlist 与 config source，运行时在每个 operation 前执行 compatibility handshake，并按 Rust response envelope 校验 version / compatibility、task submit、node transition、event append/replay、lease acquire/renew/release、cancellation token write、bundle revision pin/release；`tests/integrations/test_runtime_sidecar_grpc_client.py` 会拉起外部 `maf-runtime-sidecar --serve 127.0.0.1:<port> --sqlite <path>` 做真实 RPC 验证。
- `build_api_runtime()` 可通过 `MAF_RUNTIME_SIDECAR_ENDPOINT`（可选 `MAF_RUNTIME_SIDECAR_ALLOWED_HOSTS` / `MAF_RUNTIME_SIDECAR_MTLS_ENABLED`）装配 `RuntimeSidecarGrpcClient`；在已配置 sidecar client 的 enforce 模式下，lifecycle cancellation token、RuntimeLeaseFacade 与 ApiRuntime bundle revision pin/release 也会路由到 sidecar response envelope，未配置 client 时继续 fail-closed。
- 当前实现仍是 contract/kernel + Python facade / client 消费基线，不代表 shadow/enforce promotion、production mTLS / Unix socket、artifact provenance allowlist 或 Python 写路径最终下线已经完成。

## 11. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | lease、idempotency、event append/replay、cursor ordering、version / compatibility、health/readiness/drain、RPC-shaped response envelope、tonic/protobuf binding、sidecar binary smoke、cancellation token、bundle pin/release |
| Integration | externally managed sidecar start/health/shutdown、dev launcher、client protocol compatibility、rolling upgrade matrix、structured output validation、endpoint allowlist validation、artifact checksum / provenance validation |
| Python regression | `tests/storage`、`tests/api`、`tests/e2e`；当前覆盖 Rust contract accessors、sidecar handshake compatibility、sidecar endpoint allowlist fail-closed、structured response envelope fail-closed、same-sidecar idempotent retry gate、max-in-flight / deadline hard caps、config source / cross-host mTLS identity gate、artifact checksum / provenance gate、benchmark report completeness gate、shadow → enforce promotion threshold gate、migration / DR evidence gate、ops readiness evidence gate、decommission readiness evidence gate、事件 payload limit fail-closed、event replay page limit fail-closed、event log enforce no-legacy-fallback、runtime store task/node/edge/artifact enforce no-legacy-fallback、cancellation token enforce no-legacy-fallback、bundle revision pin/release enforce no-legacy-fallback、lease acquire/renew/release enforce no-legacy-fallback、分页 replay facade、跨页 API/SSE 历史回放与正常事件往返 |
| Fault injection | sidecar crash、lease expiry、duplicate submit、late result、enforce write failure no-fallback、idempotent retry exhausted、invalid structured response retry/fail-closed、protocol incompatible fail-closed、public bind / unauthorized client denied、queue full / deadline / payload too large、identity / secret mismatch、migration failure、restore failure |
| Performance | task submit + event replay smoke、large event stream、Python baseline vs Rust sidecar P50/P95/P99 / throughput / CPU / memory |
| Migration / DR | schema migration lock、backup、restore、event replay、rollback / roll-forward drill |
| Ops | dashboard / alert smoke、drain / restart / rollback / restore runbook drill |
| Decommission | Python legacy write path removal guard and regression |

## 12. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-SIDE-AC-001 | ApiRuntime 不再拥有最终 dispatcher 状态 | 架构审查 + sidecar client 代码 |
| RUST-SIDE-AC-002 | event replay 不依赖单进程内存 broker | replay integration test |
| RUST-SIDE-AC-003 | crash 后 task 状态可判定 | fault injection test |
| RUST-SIDE-AC-004 | `off` / `shadow` 可使用 Python legacy path，`enforce` 写路径失败不自动 fallback | rollback + failure injection test |
| RUST-SIDE-AC-005 | sidecar unavailable 返回稳定 typed error 而非静默切 Python 写路径 | API error contract test |
| RUST-SIDE-AC-006 | 写路径自动重试只对同一 sidecar 做幂等重试，重试耗尽 fail closed | retry audit + failure injection |
| RUST-SIDE-AC-007 | sidecar 结构化输出校验失败不会推进状态；可重试场景只对同一 sidecar 幂等重试 | schema validation + fault injection |
| RUST-SIDE-AC-008 | runtime sidecar compatibility handshake、rolling upgrade matrix 与不兼容 fail-closed 可验证 | compatibility matrix + readiness/failure injection |
| RUST-SIDE-AC-009 | runtime sidecar 仅内部可访问；公网绑定、未授权 client、非 allowlist discovery 在 `enforce` 下 fail closed | endpoint validation + security/failure injection |
| RUST-SIDE-AC-010 | runtime sidecar 并发、队列、deadline、event payload、replay page、shutdown drain 限制生效 | resource/backpressure tests + metrics evidence |
| RUST-SIDE-AC-011 | runtime sidecar config / DB secret / identity 只来自允许来源，secret 不泄露，rotation / mismatch fail-closed | config source tests + redaction snapshot + identity failure injection |
| RUST-SIDE-AC-012 | sidecar binary / image checksum、SBOM、proto / schema hash、provenance 与 client allowlist 校验可验证 | release artifact review + connect failure injection |
| RUST-SIDE-AC-013 | runtime sidecar benchmark 覆盖 task submit、state transition、event append、lease、event replay、SSE snapshot 与资源指标 | benchmark report + CI / release SLO gate |
| RUST-SIDE-AC-014 | SQLite schema / event log / cursor / lease / bundle pin migration 有 backup、restore、replay 与 rollback / roll-forward 演练 | migration tests + restore / replay drill |
| RUST-SIDE-AC-015 | sidecar canonical 稳定后 Python store / dispatcher 写路径下线，只保留 sidecar client / facade | decommission PR + architecture guard + regression tests |
| RUST-SIDE-AC-016 | `enforce` 前 runtime sidecar 具备 dashboard、alert、runbook 与 crash / overload / migration / restore 演练证据 | ops checklist + drill records |

## 13. 风险

| 风险 | 缓解 |
|---|---|
| sidecar 增加本地开发复杂度 | 生产由外部进程管理器 / 容器编排管理；本地提供一键 launcher 和 test fixture |
| 协议过早锁死 | versioned protocol，保留兼容窗口 |
| storage migration 风险 | 本专题交付 SQLite adapter 与 PostgreSQL-compatible contract；每次 schema 变更必须有 migration lock、backup、restore、replay 与 rollback / roll-forward drill；PostgreSQL productionization 独立 PRD 决策 |
| Python legacy store 残留导致双写漂移 | sidecar canonical 稳定后删除 Python 写路径，rollback 走 deployment / artifact rollback 与 restore |
