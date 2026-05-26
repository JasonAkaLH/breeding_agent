# Phase 3 PRD — Runtime Integration、Fail-closed 与 Observability

- **日期**：2026-05-26
- **状态**：待实施
- **前置**：Phase 2 StateService 已通过
- **关联测试规格**：`test-spec-04-Phase3-RuntimeIntegrationFailClosedObservability.md`
- **范围**：API runtime integration seam、production PostgreSQL config gate、SQLite dev/test boundary、RuntimeSidecar writer boundary、health/readiness、runbook
- **非范围**：不执行 SQLite -> PostgreSQL 数据迁移；不做 production cutover；不提供真实远端 DSN

## 1. Goals

1. 在 API runtime 中引入 State Platform runtime factory。
2. production mode 下 PostgreSQL 缺 DSN、缺 driver、migration ledger not ready、writer boundary conflict 必须 fail closed。
3. dev/test mode 可继续 SQLite legacy path，但不得标记为 production-ready。
4. RuntimeSidecar 与 State Platform 不得同时成为同一业务表 canonical writer。
5. 暴露 state platform health/readiness 和 queue / worker / dead-letter observability。
6. 提供 PostgreSQL State Platform runbook。

## 2. Functional Requirements

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P3-FR-1 | 新增 backend state runtime factory。 | Runtime assembly tests 可选择 dev/test SQLite 或 production PostgreSQL。 |
| P3-FR-2 | Production PostgreSQL 缺配置必须 fail closed。 | 缺 DSN / driver / migration ready 负向测试通过。 |
| P3-FR-3 | Production 显式 SQLite backend 必须拒绝。 | Config matrix tests。 |
| P3-FR-4 | RuntimeSidecar enforce writer 与 State Platform writer 冲突必须拒绝或降为 shadow-only。 | Conflict tests。 |
| P3-FR-5 | health/readiness 必须包含 DB、migration、queue backlog、oldest pending、dead-letter、worker heartbeat。 | Observability tests。 |
| P3-FR-6 | Runbook 必须覆盖 dead-letter triage、worker drain、lease recovery、migration gate、backup/restore。 | Docs review check。 |

## 3. Config Rules

- `MAF_STATE_STORE_BACKEND=postgresql` 表示使用 PostgreSQL State Platform。
- `MAF_POSTGRES_STATE_DSN` 只允许来自部署环境变量或 git-ignored config bootstrap，不得写入 tracked 文件。
- production mode 不允许 SQLite canonical backend。
- migration ledger not ready 时 readiness=false，不能隐式运行 DDL。
- runtime startup 不得把 PostgreSQL unavailable fallback 到 SQLite。

## 4. Observability Requirements

| Signal | Required fields |
| --- | --- |
| readiness | db_status、migration_status、queue_backlog、oldest_pending_age、dead_letter_count、worker_heartbeat、degraded_reason。 |
| audit / metrics | operation、status、duration、error_code、partition category、attempt count；不得包含 secret/raw payload。 |
| runbook | drain、restart、lease reclaim、dead-letter replay/close、backup/restore、migration gate check。 |

## 5. Implementation Plan

1. 新增 `src/state/runtime_factory.py` 或 `src/api/state_runtime.py`。
2. 更新 `src/api/runtime.py` assembly seam，但不执行 cutover/migration。
3. 新增 health/readiness route 或扩展现有 route。
4. 新增 observability helpers。
5. 新增 `docs/runbooks/postgresql-state-platform.md`。
6. 更新 README / docs with config rules only; 不写真实 DSN。

## 6. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Production 缺 PostgreSQL 时偷偷回 SQLite。 | Fail-closed tests。 |
| Sidecar 与 State Platform 双写。 | Runtime assembly conflict test。 |
| Readiness 太宽松导致半迁移可服务。 | migration ledger not-ready tests。 |
| Health 泄漏 DSN。 | Redaction snapshot tests。 |

## 7. Exit Criteria

- Runtime assembly targeted tests 通过。
- Observability tests 通过。
- Runbook 存在且覆盖恢复路径。
- 未执行 SQLite -> PostgreSQL migration。
- 未提交真实生产 DSN。
