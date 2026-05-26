# PostgreSQL State Platform 防死锁与写队列 Phase PRD 索引

- **日期**：2026-05-26
- **状态**：repo-local foundation 已实施；真实 PostgreSQL integration / migration / cutover 仍为生产门禁 pending
- **父计划**：`.omx/plans/prd-20260526-postgresql-state-platform.md`
- **父测试规格**：`.omx/plans/test-spec-20260526-postgresql-state-platform.md`
- **设计来源**：`docs/superpowers/specs/2026-05-26-postgresql-state-platform-deadlock-design.md`
- **总目标**：把当前 SQLite-centered 状态存储演进为生产级 PostgreSQL State Platform，实现读取不阻塞、写入排队、partition 保序、deadlock/lock timeout bounded retry、production fail-closed 与可观测运维门禁。

## Phase 拆分原则

1. 保留父计划作为 umbrella plan，不在子 PRD 里重新解释核心决策。
2. 每个 Phase 都必须有独立 PRD 与 test spec，可以单独实施、验收、回滚。
3. Phase 0-3 不执行 SQLite -> PostgreSQL 数据迁移，也不需要用户提供远端生产库地址。
4. Phase 4 单独处理 migration / cutover / rollback，不与平台内核实现混在一起。
5. 任何阶段都不得把 production PostgreSQL 不可用时 fallback 到 SQLite。

## Phase 文件

| Phase | PRD | Test Spec | 目标 |
| --- | --- | --- | --- |
| Phase 0 | `01-Phase0-Driver与StatePlatformContractPRD.md` | `test-spec-01-Phase0-Driver与StatePlatformContract.md` | PostgreSQL driver ADR、State Platform contract、error policy、command DTO、health/readiness model、red tests。 |
| Phase 1 | `02-Phase1-PostgreSQLSchema与WriteQueueKernelPRD.md` | `test-spec-02-Phase1-PostgreSQLSchema与WriteQueueKernel.md` | PostgreSQL schema descriptors、`state_write_command`、partition cursor、enqueue/idempotency/claim/lease/retry/dead-letter。 |
| Phase 2 | `03-Phase2-CommandHandlersReadStoreStateServicePRD.md` | `test-spec-03-Phase2-CommandHandlersReadStoreStateService.md` | command handler framework、ReadStore、read-not-blocked、StateService submit / execute-and-wait / command group。 |
| Phase 3 | `04-Phase3-RuntimeIntegrationFailClosedObservabilityPRD.md` | `test-spec-04-Phase3-RuntimeIntegrationFailClosedObservability.md` | API runtime integration seam、production fail-closed、RuntimeSidecar writer boundary、health/readiness、runbook。 |
| Phase 4 | `05-Phase4-SQLiteToPostgreSQLMigrationCutoverPRD.md` | `test-spec-05-Phase4-SQLiteToPostgreSQLMigrationCutover.md` | SQLite -> PostgreSQL migration、校验、shadow、cutover、rollback、backup/restore drill。 |

## 跨 Phase 不变量

- Reads must not wait for pending write commands; readers see last committed PostgreSQL state.
- Writes must enter durable typed command queue before business table mutation.
- Same partition commands must execute in `partition_sequence` order.
- Retry is allowlisted; unknown or business/security/contract errors fail closed.
- Queue, dead-letter, audit, readiness and runbook payloads must not expose DSN, token, secret or raw user payload.
- Real PostgreSQL integration evidence is required before any production-ready claim.
