# Test Spec — PostgreSQL State Platform 防死锁、读写隔离与写队列

- **日期**：2026-05-26
- **关联计划**：`.omx/plans/prd-20260526-postgresql-state-platform.md`
- **关联设计**：`docs/superpowers/specs/2026-05-26-postgresql-state-platform-deadlock-design.md`
- **状态**：待实施
- **测试范围**：State Platform contract、PostgreSQL schema、write queue、writer worker、deadlock retry、read-not-blocked、runtime assembly、health/readiness、static sweep
- **测试非范围**：不执行 SQLite -> PostgreSQL 数据迁移；不连接用户未来远端生产库；不做生产 cutover / rollback

## 1. 测试目标

证明未来实现满足：

1. 读路径直接读已提交业务表，不等待 pending queue，不使用业务写锁。
2. 写路径全部进入 durable command queue，由 writer workers 执行。
3. 同一 partition 保序，不同 partition 可并行。
4. Deadlock / lock timeout / serialization failure / transient connection 按 bounded retry 处理。
5. 不可恢复错误 fail closed，不 retry，不伪装成功。
6. PostgreSQL production mode 缺配置、缺 driver、migration 未 ready 时 fail closed；不 fallback SQLite。
7. health/readiness 能暴露 DB、queue、worker、migration 和 dead-letter 状态。
8. 本轮实施不执行真实生产迁移，也不要求用户提供远端 PostgreSQL 地址。

## 2. Checkpoint Gate Matrix

| Checkpoint | 必跑 targeted tests / checks | 进入下一阶段条件 |
| --- | --- | --- |
| CP-0 Dependency + contract red tests | `tests.storage.test_state_platform_contract`、`tests.storage.test_state_platform_error_policy`、dependency decision document review。 | Tests 初始可红且指向缺失 contract；dependency decision 记录 driver/license/support 证据；未改 runtime。 |
| CP-1 PostgreSQL schema descriptors | `tests.storage.test_postgres_state_schema_contract`。 | `state_write_command`、partition cursor、dead-letter/archive、migration ledger 字段/索引/约束全部可测。 |
| CP-2 Queue repository + worker claim | `tests.storage.test_postgres_write_queue_contract`、`tests.storage.test_postgres_worker_claim_ordering`。 | enqueue/claim/lease/complete/retry/dead-letter 通过；同 partition 不跳序。 |
| CP-3 Command handler framework | `tests.storage.test_state_command_handlers`、`tests.storage.test_state_command_error_policy`。 | command type、payload schema、partition rule、idempotency、retry policy 全部声明并可测。 |
| CP-4 Read store | `tests.storage.test_postgres_read_store_contract`、`tests.storage.test_postgres_read_not_blocked_by_writer`。 | writer 未提交 / pending command 场景下 reader 返回旧 committed snapshot。 |
| CP-5 StateService orchestration | `tests.storage.test_state_service_execute_and_wait`、`tests.storage.test_state_service_command_group`。 | submit、execute-and-wait、timeout、cancel、idempotent replay、command group 通过。 |
| CP-6 Runtime integration seam | `tests.api.test_state_platform_runtime_assembly`、`tests.api.test_state_platform_production_fail_closed`。 | production PostgreSQL 缺配置 fail closed；dev/test SQLite 兼容；无双 canonical writer。 |
| CP-7 Observability + readiness | `tests.observability.test_state_platform_health`。 | readiness 覆盖 DB、migration、queue backlog、oldest pending、dead-letter、worker heartbeat。 |
| CP-8 Full regression | 后端分层 discover、前端 test/build、static sweep、`git diff --check`。 | 无 direct write bypass、无未解释 SQLite production fallback、license report 完整。 |

Gate rule：任何 checkpoint 失败时，只允许修复该 checkpoint 或上游依赖；不得用 fallback 到 SQLite、跳过 queue、放宽 retry 分类或删除测试来变绿。

## 3. Contract and Error Policy Tests

### 文件

- 新增 `tests/storage/test_state_platform_contract.py`
- 新增 `tests/storage/test_state_platform_error_policy.py`

### 用例

| 用例 | 期望 |
| --- | --- |
| contract exposes read/write split | `StateService` 明确包含 read query、submit command、execute-and-wait、command group、health/readiness。 |
| write command DTO durable fields | command DTO 包含 command_id、type、idempotency_key、partition_key、partition_sequence、status、attempt、lease、payload、result/error、timestamps。 |
| health model fields | health/readiness model 包含 DB connectivity、migration state、queue backlog、oldest pending age、dead-letter count、worker heartbeat。 |
| retryable pg error codes | `40P01`、`40001`、`55P03`、`57014` 被分类为 retryable/transient。 |
| non-retryable business errors | schema/payload/ownership/idempotency/safety/state-machine/handler bug 默认 non-retry。 |
| unknown error default | 未知错误默认 fail closed，不 retry。 |
| idempotency required | 所有 write command 必须有 idempotency key，缺失时报 contract error。 |

## 4. PostgreSQL Schema Contract Tests

### 文件

- 新增 `tests/storage/test_postgres_state_schema_contract.py`

### 用例

| 用例 | 期望 |
| --- | --- |
| command table fields | `state_write_command` 包含计划要求的字段和类型；payload/result/error metadata 使用 JSONB 等 PostgreSQL 原生类型。 |
| command status enum | status 仅允许 pending/running/retrying/succeeded/failed/dead_letter/cancelled。 |
| idempotency unique | `idempotency_key` 有唯一约束。 |
| partition sequence unique | `(partition_key, partition_sequence)` 有唯一约束。 |
| claim index exists | claim 所需 status/available_at/priority/created_at 索引存在。 |
| partition outstanding index exists | 同 partition prior command 检查所需索引存在。 |
| partition cursor table | `state_partition_cursor` 支持按 partition 分配 sequence。 |
| dead letter/archive tables | dead-letter 与 archive 表可存储原 command、错误和审计 metadata。 |
| migration ledger | migration ledger 可记录 version、checksum、started/completed、status、operator。 |
| business tables pg types | conversation/task/message/event/artifact/auth 等业务表使用 timestamptz/jsonb/明确 FK 与索引。 |

## 5. Write Queue and Worker Claim Tests

### 文件

- 新增 `tests/storage/test_postgres_write_queue_contract.py`
- 新增 `tests/storage/test_postgres_worker_claim_ordering.py`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| enqueue stores durable command | enqueue 一个 command | status=pending，partition_sequence 分配，idempotency key 可查。 |
| idempotent enqueue returns existing | 相同 idempotency key enqueue 两次 | 返回同一 command，不创建重复写入。 |
| claim uses skip locked | 两个 worker 并发 claim | 不 claim 同一 command；SQL 包含 `FOR UPDATE SKIP LOCKED`。 |
| no partition skip | partition A seq1 未完成，seq2 pending | worker 不得 claim seq2。 |
| cross partition parallel | partition A/B 各有 pending | 多 worker 可同时 claim A/B。 |
| lease recovery | worker claim 后 crash，lease 到期 | 其他 worker 可重新 claim。 |
| heartbeat extends lease | worker heartbeat | lease_expires_at 后移。 |
| complete records result | handler 成功 | status=succeeded，result 写入，completed_at 存在。 |
| retry schedules backoff | handler 返回 retryable error | attempt+1，status=retrying，available_at 带 jitter 后移。 |
| retry exhausted dead letter | 超过 max attempts | status=dead_letter 或复制到 dead-letter 表。 |
| non retry fails closed | handler 返回 non-retry error | status=failed，不进入 retrying。 |

## 6. Deadlock / Timeout / Serialization Fault Injection Tests

### 文件

- 新增 `tests/storage/test_postgres_deadlock_retry.py`
- 新增 `tests/storage/test_postgres_lock_timeout_retry.py`
- 新增 `tests/storage/test_postgres_serialization_retry.py`

### 用例

| 用例 | 模拟方式 | 期望 |
| --- | --- | --- |
| deadlock retry | fake driver 或 PostgreSQL fixture 抛 `40P01` | command retry，attempt 增加，最终成功或 dead-letter。 |
| serialization retry | 抛 `40001` | command retry，保留 idempotency。 |
| lock not available retry | 抛 `55P03` | command retry，不把业务状态标为成功。 |
| statement timeout retry | 抛 `57014` 且 source=statement_timeout | command retry 或按 command policy retry。 |
| caller cancel not retried as success | caller deadline/cancel | command 状态按 policy 处理，API 不伪装提交成功。 |
| connection transient retry | pool timeout/server restart/transient disconnect | bounded retry，不泄漏 DSN。 |
| invalid payload no retry | payload schema invalid | failed，attempt 不重复增长。 |
| permission/ownership no retry | owner mismatch | failed，no retry。 |
| handler bug no retry | handler 抛未分类异常 | failed，审计脱敏。 |

## 7. Read Store and Read-not-blocked Tests

### 文件

- 新增 `tests/storage/test_postgres_read_store_contract.py`
- 新增 `tests/storage/test_postgres_read_not_blocked_by_writer.py`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| read uses committed snapshot | 已有 row=v1，writer 未提交 v2 | reader 返回 v1，不等待 writer commit。 |
| pending command invisible | enqueue 更新 command 但未执行 | reader 返回旧业务表状态，不读取 pending queue。 |
| no read lock clause | inspect generated SQL / query wrapper | read query 不含 `FOR UPDATE` 或业务写锁。 |
| read statement timeout set | 执行 read query | session / transaction 设置 statement timeout。 |
| pagination enforced | list 大量 rows | query 有 limit 或分页 contract。 |
| slow read observed | fake slow query | metrics/audit 记录脱敏 duration 与 query category。 |

## 8. Command Handler Tests

### 文件

- 新增 `tests/storage/test_state_command_handlers.py`
- 新增 `tests/storage/test_state_command_error_policy.py`

### 用例

| 用例 | 期望 |
| --- | --- |
| all registered command types declare schema | 每个 command type 有 payload/result schema。 |
| partition key required | 每个 write command 有稳定 partition key rule。 |
| global lock order declared | 同时写多表的 handler 声明锁顺序。 |
| no external io marker | handler contract / tests 阻止在 transaction 内调用 LLM/HTTP/Skill/MCP/file IO。 |
| conversation commands partitioned | conversation/message/pending skill context 使用 `conversation:{id}`。 |
| task commands partitioned | task/node/edge/event/artifact/interrupt/mailbox 使用 `task:{id}`。 |
| auth commands partitioned | token/login/logout 使用 `auth:{username}`。 |
| system commands partitioned | migration/cutover 使用 `system:migration`。 |
| idempotent replay | 重复 command 返回已有 result，不重复写业务表。 |

## 9. StateService Tests

### 文件

- 新增 `tests/storage/test_state_service_execute_and_wait.py`
- 新增 `tests/storage/test_state_service_command_group.py`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| submit async returns command id | submit command | 立即返回 durable command id，不等待业务表更新。 |
| execute and wait success | submit 并等待成功 | 返回 handler result，业务表已提交。 |
| execute and wait failure | handler non-retry fail | 返回 typed failed error。 |
| execute and wait timeout | worker 未完成，caller deadline 到 | caller 得到 timeout；command 仍按 queue policy 继续/取消。 |
| command group atomic success | 两个相关写入同 group | 全部提交。 |
| command group atomic rollback | 第二个写入失败 | 全部回滚或 group 标记 failed，无半提交。 |
| idempotent execute replay | 同 idempotency key 再次 execute | 返回已有 result。 |

## 10. Runtime Assembly and Production Fail-closed Tests

### 文件

- 新增 `tests/api/test_state_platform_runtime_assembly.py`
- 新增 `tests/api/test_state_platform_production_fail_closed.py`

### 用例

| 用例 | 环境 | 期望 |
| --- | --- | --- |
| dev default keeps sqlite | 未设置 production backend | 现有测试/dev 可继续启动 SQLite legacy path。 |
| production requires postgres backend | production mode + backend missing | 启动或 readiness fail closed。 |
| production missing dsn fails | `MAF_STATE_STORE_BACKEND=postgresql` 且无 DSN | fail closed，不 fallback SQLite。 |
| missing driver fails clearly | PostgreSQL backend 但 driver import 失败 | typed config error，不隐式跳过。 |
| migration not ready fails readiness | migration ledger 未 ready | readiness=false。 |
| sidecar canonical writer conflict | State Platform production writer + RuntimeSidecar enforce writer 同时启用 | fail closed 或强制 sidecar shadow-only。 |
| old StoragePort adapter not production core | production code path 检查 | 不能直接暴露 `SQLiteStorage` 为 canonical state store。 |

## 11. Observability and Runbook Tests

### 文件

- 新增 `tests/observability/test_state_platform_health.py`
- 新增/更新 docs runbook review checks

### 用例

| 用例 | 期望 |
| --- | --- |
| health alive | process alive 可返回 health。 |
| readiness includes db | readiness 输出 DB connectivity。 |
| readiness includes migration | readiness 输出 migration ledger status。 |
| readiness includes queue backlog | 输出 pending/retrying/running/dead-letter counts。 |
| readiness includes oldest pending | 输出 oldest pending age。 |
| readiness includes workers | 输出 worker heartbeat/stale worker count。 |
| audit redacts payload | audit/metrics 不包含 DSN、raw command payload、token、secret。 |
| runbook has recovery paths | runbook 覆盖 dead-letter triage、worker drain、lease recovery、migration gate、backup/restore。 |

## 12. Static Sweep Tests

实施结束执行并人工确认 allowlist：

```bash
rg -n "append_event\(|save_task\(|save_message\(|save_artifact\(|save_conversation\(" src tests
rg -n "StateService|StateReadStore|StateWriteQueue|StateWriter|state_write_command" src tests docs README.md
rg -n "MAF_STATE_STORE_BACKEND|MAF_POSTGRES_STATE_DSN|sqlite" src tests docs README.md
rg -n "FOR UPDATE|SKIP LOCKED|lock_timeout|statement_timeout|idle_in_transaction" src/state tests/storage
python - <<'PY'
from pathlib import Path
markers = ["TO" + "DO", "TB" + "D", "占" + "位", "待" + "定", "?" * 3]
for file_name in [
    ".omx/plans/prd-20260526-postgresql-state-platform.md",
    ".omx/plans/test-spec-20260526-postgresql-state-platform.md",
    "docs/superpowers/specs/2026-05-26-postgresql-state-platform-deadlock-design.md",
]:
    text = Path(file_name).read_text()
    for marker in markers:
        if marker in text:
            raise SystemExit(f"unresolved marker {marker!r} in {file_name}")
PY
```

期望：

- Direct writes 只允许出现在 handler implementation、legacy adapter、tests 或明确 migration/dev-only path。
- Production config 不允许 SQLite canonical fallback。
- PostgreSQL timeout / locking SQL 只出现在 State Platform implementation 和 tests。
- 文档无 unresolved placeholder。

## 13. Required Verification Commands

Targeted:

```bash
conda run -n multi_agent python -m unittest tests.storage.test_state_platform_contract tests.storage.test_state_platform_error_policy
conda run -n multi_agent python -m unittest tests.storage.test_postgres_state_schema_contract tests.storage.test_postgres_write_queue_contract tests.storage.test_postgres_worker_claim_ordering
conda run -n multi_agent python -m unittest tests.storage.test_postgres_deadlock_retry tests.storage.test_postgres_lock_timeout_retry tests.storage.test_postgres_serialization_retry
conda run -n multi_agent python -m unittest tests.storage.test_postgres_read_store_contract tests.storage.test_postgres_read_not_blocked_by_writer
conda run -n multi_agent python -m unittest tests.storage.test_state_command_handlers tests.storage.test_state_service_execute_and_wait tests.storage.test_state_service_command_group
conda run -n multi_agent python -m unittest tests.api.test_state_platform_runtime_assembly tests.api.test_state_platform_production_fail_closed
conda run -n multi_agent python -m unittest tests.observability.test_state_platform_health
```

Regression:

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
git diff --check
```

PostgreSQL integration gate：

- 如果实现阶段提供本地 service container 或显式测试 DSN，可运行真实 PostgreSQL integration tests。
- 如果没有 PostgreSQL 测试实例，必须至少通过 contract/fake tests，并在最终报告中把真实 PostgreSQL integration 记为 Not-tested，不得宣称生产 ready。
- 用户提供远端生产地址之前，不得把真实生产 connectivity / cutover / migration 伪造成已验证。

## 14. License and Dependency Verification

- 若新增 Python PostgreSQL driver：更新 `requirements.txt`，记录 license、版本、官方支持证据和 Python 3.13 / SQLAlchemy 2.x 兼容性验证。
- 若不涉及 Rust / `native/` / `Cargo.lock` / `native/deny.toml`，最终报告明确：License Requirement：无 Rust 依赖/许可策略变更，未触发 cargo-deny 风险。
- 若涉及 Rust workspace 或供应链策略，则必须运行 `cd native && cargo deny check` 并读取结果。
