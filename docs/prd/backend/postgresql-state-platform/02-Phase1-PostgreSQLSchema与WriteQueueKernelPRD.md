# Phase 1 PRD — PostgreSQL Schema 与 Write Queue Kernel

- **日期**：2026-05-26
- **状态**：待实施
- **前置**：Phase 0 driver ADR 与 State Platform contract 已通过
- **关联测试规格**：`test-spec-02-Phase1-PostgreSQLSchema与WriteQueueKernel.md`
- **范围**：PostgreSQL schema descriptors、write command queue、partition cursor、enqueue/idempotency、claim/lease、retry/dead-letter kernel
- **非范围**：不实现业务 command handlers；不接 API runtime；不执行 SQLite 数据迁移；不做生产 cutover

## 1. Goals

1. 定义 PostgreSQL 业务表与 queue 表 schema descriptors。
2. 实现 `state_write_command`、`state_partition_cursor`、dead-letter/archive、migration ledger。
3. 实现 enqueue、idempotency payload fingerprint、claim、lease、heartbeat、complete/fail/retry/dead-letter。
4. 使用 `FOR UPDATE SKIP LOCKED` 并保证同 partition 不跳序。
5. 为真实 PostgreSQL integration tests 留出独立测试库 / schema 入口。

## 2. Functional Requirements

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P1-FR-1 | Schema descriptors 必须包含 command queue、partition cursor、dead-letter/archive、migration ledger。 | Schema contract tests 验证字段、索引、约束、status enum。 |
| P1-FR-2 | Enqueue 必须 durable 且 idempotent。 | 相同 idempotency key + 相同 payload 返回同一 command；payload mismatch 返回 conflict。 |
| P1-FR-3 | Claim 必须使用 `FOR UPDATE SKIP LOCKED`。 | SQL contract 和真实 PG 并发测试验证不重复 claim。 |
| P1-FR-4 | 同 partition 不得跳过未完成早期 command。 | `NOT EXISTS prior` guard 或等价机制通过并发测试。 |
| P1-FR-5 | Lease 到期后可 reclaim。 | worker crash / stale lease 测试最终 terminal。 |
| P1-FR-6 | Retry schedule 必须 bounded。 | retryable error 增加 attempt、设置 available_at；超过上限进入 dead-letter。 |

## 3. Schema Requirements

核心 queue 字段至少包括：

- `command_id`
- `command_type`
- `idempotency_key`
- `payload_fingerprint`
- `partition_key`
- `partition_sequence`
- `payload jsonb`
- `status`
- `priority`
- `available_at`
- `attempt_count`
- `max_attempts`
- `lease_owner`
- `lease_expires_at`
- `last_error_code`
- `last_error_message`
- `result jsonb`
- `created_at`
- `updated_at`
- `completed_at`

必须有：

- `idempotency_key` unique 或 `(command_type, idempotency_key)` unique。
- `(partition_key, partition_sequence)` unique。
- claim index 覆盖 status / available_at / priority / created_at。
- partition outstanding index 支撑 same-partition prior check。

## 4. Non-functional Requirements

- Queue/dead-letter payload 必须脱敏或只存允许字段。
- Schema migration descriptors 不在普通 API 启动期执行。
- 本 phase 的真实 PostgreSQL tests 不得连接生产库。
- Fake tests 不能替代 `SKIP LOCKED` / MVCC 真实集成证据。

## 5. Implementation Plan

1. 新增 `src/state/postgres/schema.py`。
2. 新增 `src/state/postgres/write_queue.py`。
3. 新增 `src/state/postgres/worker.py` 中的 claim/lease kernel，不包含业务 handler。
4. 新增 storage tests 和可选真实 PG integration fixtures。
5. 保持 runtime assembly 不变。

## 6. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| `SKIP LOCKED` claim 跳过同 partition 早期 command。 | SQL 必须包含 prior unfinished guard，真实并发测试覆盖。 |
| Idempotency key 被不同 payload 复用。 | payload fingerprint mismatch 返回 conflict。 |
| Dead-letter 泄漏 raw payload。 | redaction tests 和 metadata allowlist。 |

## 7. Exit Criteria

- Schema contract tests 通过。
- Queue kernel tests 通过。
- 真实 PostgreSQL integration tests 有证据；若缺测试实例，最终报告必须 Not-tested 且不能宣称 production ready。
- `src/api/runtime.py` production assembly 未接入新 queue。
