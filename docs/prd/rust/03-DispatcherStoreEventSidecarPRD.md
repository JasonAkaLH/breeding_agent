# Dispatcher / Store / Event Rust Sidecar PRD

- **状态**：待实现（sidecar 正式协议、PostgreSQL 延期策略、enforce 写路径 fail-closed 已冻结）
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

## 11. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | lease、idempotency、event append/replay、cursor ordering |
| Integration | externally managed sidecar start/health/shutdown、dev launcher、client protocol compatibility、rolling upgrade matrix、structured output validation、endpoint allowlist validation、artifact checksum / provenance validation |
| Python regression | `tests/storage`、`tests/api`、`tests/e2e` |
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
