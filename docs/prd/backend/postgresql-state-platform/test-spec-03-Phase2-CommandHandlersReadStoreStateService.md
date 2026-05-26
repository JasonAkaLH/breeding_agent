# Test Spec — Phase 2 Command Handlers、ReadStore 与 StateService

- **日期**：2026-05-26
- **状态**：待实施
- **关联 PRD**：`03-Phase2-CommandHandlersReadStoreStateServicePRD.md`

## 1. Test Goals

证明 command handler、ReadStore 和 StateService 形成可用状态平台语义，并保持读不阻塞、写入短事务、错误 fail-closed。

## 2. Target Tests

| Test file | Coverage |
| --- | --- |
| `tests/storage/test_state_command_handlers.py` | handler registry、payload schema、partition rule、lock order、no external IO。 |
| `tests/storage/test_state_command_error_policy.py` | handler retry / non-retry 分类。 |
| `tests/storage/test_postgres_read_store_contract.py` | ReadStore query contract、no pending queue、no write lock、pagination。 |
| `tests/storage/test_postgres_read_not_blocked_by_writer.py` | Real PostgreSQL MVCC read-not-blocked。 |
| `tests/storage/test_state_service_execute_and_wait.py` | submit / execute-and-wait success/fail/timeout/cancel/replay。 |
| `tests/storage/test_state_service_command_group.py` | command group atomic success / rollback。 |

## 3. Required Cases

| Case | Expected |
| --- | --- |
| all command types registered | 每个 production write command 有 handler。 |
| no external io marker | handler transaction 内禁止 LLM/HTTP/Skill/MCP/file IO。 |
| read committed snapshot | writer 未提交 v2 时 reader 返回 v1。 |
| pending command invisible | enqueue 未执行时 query 返回旧业务表状态。 |
| no read lock clause | read SQL 不含 `FOR UPDATE`。 |
| execute wait success | command committed 后返回 result。 |
| execute wait timeout | caller timeout 不伪装成功。 |
| command group rollback | 任一写失败时 group 无半提交。 |

## 4. Verification Commands

```bash
conda run -n multi_agent python -m unittest tests.storage.test_state_command_handlers tests.storage.test_state_command_error_policy
conda run -n multi_agent python -m unittest tests.storage.test_postgres_read_store_contract tests.storage.test_postgres_read_not_blocked_by_writer
conda run -n multi_agent python -m unittest tests.storage.test_state_service_execute_and_wait tests.storage.test_state_service_command_group
git diff --check
```

## 5. Real PostgreSQL Gate

`tests.storage.test_postgres_read_not_blocked_by_writer` 只有在真实 PostgreSQL 或可信 service container 下通过，才能宣称 read-not-blocked 生产语义已验证。
