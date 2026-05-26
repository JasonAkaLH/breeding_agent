# Phase 0 PRD — PostgreSQL Driver 与 State Platform Contract

- **日期**：2026-05-26
- **状态**：待实施
- **父计划**：`.omx/plans/prd-20260526-postgresql-state-platform.md`
- **关联测试规格**：`test-spec-01-Phase0-Driver与StatePlatformContract.md`
- **范围**：PostgreSQL driver ADR、State Platform contract、typed error policy、command DTO、health/readiness model、contract red tests
- **非范围**：不新增 runtime production path；不连接远端 PostgreSQL；不执行 schema migration；不修改 SQLite 数据

## 1. Problem Statement

当前仓库已经有 SQLAlchemy 2.x 与 SQLite legacy storage，但没有 PostgreSQL driver，也没有表达 queue、partition、lease、health/readiness 的长期 State Platform contract。若直接进入 schema 或 runtime 实现，会把 driver 行为、timeout、SQLSTATE 提取、async/cancel 能力和接口语义混在一起，造成后续重工。

## 2. Goals

1. 形成 PostgreSQL driver ADR，明确选择、拒绝项、license、Python 3.13 / SQLAlchemy 2.x async 兼容、SQLSTATE/error code 提取、statement timeout、connection cancellation 和 pool 行为。
2. 新增 `src/state/` contract 边界，不把新语义塞回 `StoragePort`。
3. 定义 durable command DTO、command status、idempotency payload fingerprint、partition key / sequence、lease、retry/dead-letter metadata。
4. 定义 retryable / non-retryable typed error policy。
5. 定义 health/readiness DTO，覆盖 DB、migration、queue、worker、dead-letter。
6. 先写 red tests，证明后续实现入口明确。

## 3. Non-goals

- 不实现 PostgreSQL schema。
- 不实现 write queue / worker。
- 不接入 API runtime。
- 不切换任何生产状态路径。
- 不把 SQLite 改造成生产 fallback。

## 4. Baseline Evidence

| 证据 | 约束 |
| --- | --- |
| `requirements.txt` 当前有 SQLAlchemy / PyMySQL，无 PostgreSQL driver。 | 必须先做 driver ADR。 |
| `src/core/contracts.py` 的 `StoragePort` 是 repository-style 方法集合。 | 新 queue/worker/health 语义放入 `src/state/`，旧 port 只可做 adapter。 |
| `src/api/runtime.py` 当前直接装配 `SQLiteStorage`。 | Phase 0 不接 runtime，只冻结 contract。 |
| 父计划 AC-15 / AC-17 | 依赖和真实 PostgreSQL evidence 不能省略。 |

## 5. Functional Requirements

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P0-FR-1 | 必须新增 driver ADR。 | ADR 比较 `psycopg`、`asyncpg`、SQLAlchemy async dialect 组合，并记录 license / error / timeout / cancel 证据。 |
| P0-FR-2 | 必须新增 `StateService` / `StateReadStore` / `StateWriteQueue` / `StateCommandHandler` / `StateHealthProvider` Protocol 或等价 contract。 | Contract tests 可 import 并验证方法签名与读写语义。 |
| P0-FR-3 | Command DTO 必须包含 durable write 所需字段。 | Tests 验证 command id、type、idempotency key、payload fingerprint、partition、status、attempt、lease、timestamps、result/error metadata。 |
| P0-FR-4 | Error policy 必须白名单 retryable PostgreSQL SQLSTATE。 | `40P01`、`40001`、`55P03`、`57014` 可分类；未知错误默认 non-retry fail closed。 |
| P0-FR-5 | Health/readiness model 必须覆盖后续 phase 所需字段。 | Tests 验证 DB、migration、queue backlog、oldest pending age、dead-letter、worker heartbeat。 |

## 6. Non-functional Requirements

- **Security**：ADR 和 tests 不得记录 DSN、账号、密码、token。
- **Compatibility**：不破坏现有 SQLite dev/test 路径。
- **Testability**：本 phase 只要求 contract/fake tests，不宣称真实 PostgreSQL ready。
- **Maintainability**：contract 命名必须独立于 SQLite / SQLAlchemy repository 旧模型。

## 7. Implementation Plan

1. 新增 driver ADR：建议路径 `docs/prd/backend/postgresql-state-platform/adr-postgresql-driver.md` 或同级明确文件。
2. 新增 `src/state/contracts.py`、`src/state/errors.py`、`src/state/commands.py`、`src/state/health.py` 的最小 contract。
3. 新增 tests：`tests/storage/test_state_platform_contract.py`、`tests/storage/test_state_platform_error_policy.py`。
4. 保持所有 runtime assembly、schema 和 queue implementation 不进入 Phase 0。

## 8. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Driver ADR 只看包名不验证 SQLSTATE/timeout/cancel。 | ADR 必须列出官方/上游证据和小型 spike 验证计划。 |
| Contract 过度贴合旧 `StoragePort`。 | 测试禁止把 queue/lease/health 语义追加进 `StoragePort` 作为唯一接口。 |
| Phase 0 被顺手接入 runtime。 | Acceptance 明确本 phase 不改 API runtime production path。 |

## 9. Exit Criteria

- Driver ADR 完成并可审阅。
- Contract / error policy tests 已存在并通过或在红绿循环中指向明确实现缺口。
- 无 runtime production path 变更。
- `git diff --check` 通过。
- License Requirement 报告包含 Python dependency 评估；如未新增依赖则说明未触发。
