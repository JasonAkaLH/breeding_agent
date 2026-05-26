# Test Spec — Phase 1 PostgreSQL Schema 与 Write Queue Kernel

- **日期**：2026-05-26
- **状态**：待实施
- **关联 PRD**：`02-Phase1-PostgreSQLSchema与WriteQueueKernelPRD.md`

## 1. Test Goals

证明 schema 和 queue kernel 在 contract/fake 层可测，并在真实 PostgreSQL 中验证 `FOR UPDATE SKIP LOCKED`、partition ordering、lease reclaim 和 timeout 基础行为。

## 2. Target Tests

| Test file | Coverage |
| --- | --- |
| `tests/storage/test_postgres_state_schema_contract.py` | schema descriptors、字段、索引、约束、status enum。 |
| `tests/storage/test_postgres_write_queue_contract.py` | enqueue、idempotency、payload fingerprint、complete/fail/retry/dead-letter。 |
| `tests/storage/test_postgres_worker_claim_ordering.py` | claim SQL、same partition ordering、cross partition parallelism、lease reclaim。 |

## 3. Required Cases

| Case | Expected |
| --- | --- |
| command table fields | 所有 durable command 字段存在。 |
| idempotency unique | 重复 idempotency key 不重复写 command。 |
| payload fingerprint mismatch | 同 key 不同 payload 返回 conflict。 |
| claim uses skip locked | 多 worker 不 claim 同一 command。 |
| no partition skip | seq1 未 terminal 时 seq2 不被 claim。 |
| cross partition parallel | partition A/B 可被不同 worker 并行 claim。 |
| lease recovery | lease 过期后 command 可被其他 worker 接管。 |
| retry exhausted | 超过 max attempts 进入 dead-letter。 |
| redacted dead-letter | dead-letter 不包含 DSN、token、secret、raw user payload。 |

## 4. Verification Commands

```bash
conda run -n multi_agent python -m unittest tests.storage.test_postgres_state_schema_contract tests.storage.test_postgres_write_queue_contract tests.storage.test_postgres_worker_claim_ordering
git diff --check
```

## 5. Real PostgreSQL Gate

真实 PostgreSQL integration 至少覆盖：

- `FOR UPDATE SKIP LOCKED` 并发 claim。
- same partition prior guard。
- lease expiry reclaim。
- statement timeout 设置入口。

无 PostgreSQL 测试实例时可 skip，但 skip reason 必须为 `postgres_test_dsn_not_configured`，且 release gate 保持 pending。
