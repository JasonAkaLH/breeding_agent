# PRD — PostgreSQL State Platform 防死锁与写队列实施计划

- **日期**：2026-05-26
- **设计来源**：`docs/superpowers/specs/2026-05-26-postgresql-state-platform-deadlock-design.md`
- **状态**：待实施
- **范围**：生产级 PostgreSQL State Platform、读写隔离、写队列、writer workers、防死锁、health/readiness、可观测性、回归测试与上线门禁
- **非范围**：本计划不执行 SQLite -> PostgreSQL 数据迁移；不配置或连接远端 PostgreSQL；不切换生产启动方式；不删除 SQLite；不做生产 cutover / rollback 操作
- **关键决策**：不以现有 `StoragePort` 作为长期生产架构核心；允许迁移期 adapter，但新能力必须围绕 State Platform 设计


## 0. Document Perfectization Confidence Standard

本次复审把这份 PRD 判定为可进入实现的标准如下：

| 维度 | 必须满足 |
| --- | --- |
| 目标一致性 | 继承用户已确认的生产 PostgreSQL、读取已提交快照不阻塞、写入排队、不要以 `StoragePort` 为长期核心、今日不迁移。 |
| 范围边界 | 实施计划必须覆盖生产 State Platform 基础能力，但不得隐式执行 SQLite 数据迁移、远端 DB 配置、生产 cutover 或 RuntimeSidecar canonical writer 切换。 |
| 可实施性 | 每个 checkpoint 都能落到明确模块、测试文件和退出门禁；CP-0 必须先处理 PostgreSQL driver 依赖决策。 |
| 可验证性 | 每条关键读写、队列、retry、fail-closed、readiness 和安全要求都必须能被单元、集成、故障注入或静态扫描验证。 |
| 生产安全 | 任何缺配置、缺 driver、migration 未 ready、sidecar writer 冲突、权限/安全/contract 错误都必须 fail closed。 |
| 证据边界 | 本文引用当前仓库事实；无法从仓库验证的内容以假设或后续门禁记录，不伪造生产 PostgreSQL 证据。 |

## 1. Requirements Summary

实施已审定的生产级状态平台方向：

1. PostgreSQL 是未来生产 canonical state store；SQLite 仅保留本地开发 / 测试兼容和未来迁移来源。
2. 读取不进入写队列，不等待 pending command，不使用业务写锁；读默认返回 PostgreSQL 最后已提交快照。
3. 所有生产业务写入必须转为 typed command，并进入 PostgreSQL-backed `state_write_command` 队列。
4. Writer workers 是生产业务写入唯一执行者；同一 partition 严格保序，不同 partition 可并行。
5. Deadlock、lock timeout、serialization failure、连接类 transient failure 必须 bounded retry；不可恢复错误 fail closed 并进入 failed / dead-letter。
6. State Platform 必须提供 `StateService`、`StateReadStore`、`StateWriteQueue`、`StateCommandHandlers`、`StateWriterWorkers`、`StateMigrationService`、`StateHealth/Readiness`。
7. API / orchestration / lifecycle / auth / capability 层不得在 DB transaction 内调用 LLM、HTTP、Skill、MCP 或文件 IO。
8. 本次实施以可分阶段门禁推进；迁移和远端生产配置作为后续独立 PRD / 运维动作。

## 2. Baseline Evidence

| 证据 | 当前状态 | 实施影响 |
| --- | --- | --- |
| `src/api/runtime.py:128-153` | `ApiRuntime` 当前接收 `Engine` 与 `SQLiteStorage`，并把 `storage` 作为核心状态入口。 | 需要新增 `StatePlatformRuntime` / `StateService` 装配路径；迁移期可桥接旧调用，但不能继续把 `SQLiteStorage` 作为生产核心。 |
| `src/api/runtime.py:211-214` | 事件写入直接 `storage.append_event()` 后 publish。 | 未来生产事件写入必须通过 command handler 或明确的 append-event command，避免上层绕过写队列。 |
| `src/core/contracts.py:63-180` | `StoragePort` 是大量 repository-style 方法集合，缺 queue、lease、partition、readiness、migration gate 语义。 | `StoragePort` 只能作为兼容 adapter；长期接口应由 State Platform contract 表达读、写、健康、迁移、worker 语义。 |
| `src/storage/sqlite/session.py:9-17` | SQLite engine 只设置 `check_same_thread=False`。 | 当前 SQLite 主路径不能证明具备生产级锁等待治理；新生产路径必须用 PostgreSQL connection/pool/timeout/read-write split 明确治理。 |
| `src/storage/sqlite/bootstrap.py:16-20` | API 启动期 bootstrap 会执行 SQLite migration 与 `create_all()`。 | 生产 PostgreSQL schema migration 不得在普通 API 启动路径隐式执行；必须独立 migration gate / readiness。 |
| `requirements.txt:71` | 已有 SQLAlchemy 2.x。 | 可复用 ORM / SQL builder 能力，但 PostgreSQL async driver 仍需依赖决策。 |
| `requirements.txt:54` | 有 PyMySQL；无 PostgreSQL driver。 | CP-0 必须先做 PostgreSQL driver dependency decision，不得假设环境已有 driver。 |
| `native/crates/maf_runtime_sidecar/src/sqlite_adapter.rs:12-24` | RuntimeSidecar SQLite adapter 以进程内 `Mutex<Connection>` 串行化。 | Sidecar 不能与新 State Platform 同时作为 canonical writer；后续必须明确 shadow/enforce 边界，避免双写源。 |
| `docs/superpowers/specs/2026-05-26-postgresql-state-platform-deadlock-design.md` | 已记录用户确认的读旧 committed snapshot、写排队、生产 PostgreSQL、今日不迁移等决策。 | 本 PRD 必须继承这些约束，不重新回到最小侵入或 SQLite 防锁补丁方向。 |


## 2.5 Users, Stakeholders, and Affected Systems

| 对象 | 目标 / 关注点 | 计划影响 |
| --- | --- | --- |
| 内部业务用户 | 对话、任务、上传、Skill 结果读取不能被状态写锁长期阻塞；允许读到最后已提交状态。 | ReadStore 不等待 queue；需要同步确认的写调用走 `execute_command_and_wait()`。 |
| 后端维护者 | 状态写入要有 durable intent、幂等、可恢复、可审计和明确失败语义。 | 新增 `src/state/` contract、queue、handler、worker 和 error policy。 |
| 运维 / 部署维护者 | 生产 PostgreSQL、writer worker、queue backlog、migration gate、dead-letter 要可观测可演练。 | 新增 health/readiness、runbook、fail-closed 配置和真实 PostgreSQL 集成门禁。 |
| 安全 / 审计维护者 | secret、DSN、token、raw payload 不得进入 queue/audit/dead-letter；权限错误不能被 retry 成成功。 | 错误策略、脱敏测试、non-retry fail-closed 和 audit-safe metrics。 |
| Skill / MCP / 主代理开发者 | 能力层不得在 DB transaction 内做外部调用；状态变更统一经 command handler。 | handler contract 和静态扫描限制 transaction 内外部 IO。 |
| 受影响系统 | API runtime、orchestration、lifecycle、auth、artifact/upload、RuntimeSidecar、observability。 | CP-6 统一 runtime assembly；RuntimeSidecar 只能 shadow-only 或互斥，避免双 canonical writer。 |

## 3. Acceptance Criteria

| ID | Criterion | Validation |
| --- | --- | --- |
| AC-1 | 代码中存在新的 State Platform contract，显式区分 read query、async command submit、sync execute-and-wait、command group、health/readiness。 | Contract/unit tests；接口文档检查。 |
| AC-2 | 生产写路径不能直接调用旧 repository 写方法；所有业务写入经过 typed command 与 writer handler。 | Static sweep + integration tests + reviewer checklist。 |
| AC-3 | Reader 在 writer 持有未提交事务或 pending command 时仍能读取最后已提交状态，不等待写队列清空。 | PostgreSQL integration/fault test。 |
| AC-4 | 同一 partition 的 commands 按 `partition_sequence` 严格顺序执行；不同 partition 可并行执行。 | Worker concurrency tests。 |
| AC-5 | Worker claim 使用 `FOR UPDATE SKIP LOCKED`，且不会跳过同 partition 更早未完成 command。 | SQL contract test + concurrent worker integration test。 |
| AC-6 | `40P01`、`40001`、`55P03`、`57014`、连接 transient failure 按 bounded retry + jitter 处理；超过上限进入 dead-letter。 | Fault injection tests。 |
| AC-7 | schema/payload/handler/ownership/idempotency/safety/state-machine 错误不 retry，直接 fail closed。 | Unit + API tests。 |
| AC-8 | 每个 command 有 idempotency key、partition key、partition sequence、status、attempt、lease、trace/audit metadata。 | Schema tests + repository tests。 |
| AC-9 | Writer transaction 设置 `lock_timeout`、`statement_timeout`、`idle_in_transaction_session_timeout`，并禁止 transaction 内外部 IO。 | Handler tests + code review checklist。 |
| AC-10 | health/readiness 暴露 DB connectivity、migration state、queue backlog、oldest pending age、dead-letter count、worker heartbeat。 | Observability tests。 |
| AC-11 | PostgreSQL 配置缺失或 migration 未通过时，生产 backend fail closed；不得 fallback 到 SQLite。 | Config/readiness tests。 |
| AC-12 | SQLite 仍可服务本地开发 / 测试，但任何 production-mode canonical state store 不能指向 SQLite。 | Config matrix tests。 |
| AC-13 | RuntimeSidecar 与 State Platform canonical writer 边界明确，不能出现两个 production writer 同时对同一业务表写入。 | Runtime assembly tests + docs check。 |
| AC-14 | 本实施不执行 SQLite 数据迁移，不要求本地远端 DB 地址。 | Plan scope review + no migration/cutover scripts executed。 |
| AC-15 | 如果新增 PostgreSQL driver 依赖，必须更新 `requirements.txt`，记录 license / supply-chain decision，并提供验证证据。 | Dependency decision artifact + final verification report。 |
| AC-16 | State Platform 必须记录 NFR：可靠性、性能、安全、隐私、兼容、可观测性、运维和可测试性。 | NFR matrix + tests / runbook review。 |
| AC-17 | 真实 PostgreSQL 行为不得只用 fake tests 代替；进入生产 ready 前必须有真实 PostgreSQL integration evidence。 | Integration gate report；无测试实例时最终报告必须 Not-tested。 |
| AC-18 | 所有 queue/dead-letter/audit/health payload 必须脱敏，不得包含 DSN、token、secret、raw user payload。 | Redaction tests + audit snapshot tests。 |


## 3.5 Non-functional Requirements

| 维度 | 生产要求 | 验收 / 测试 |
| --- | --- | --- |
| Reliability | Durable enqueue 后 command 不能静默丢失；worker crash、lease expiry、DB transient failure 必须可恢复。 | worker crash / lease recovery / retry exhausted tests。 |
| Performance | 读路径不等待 write queue；queue backlog 增长不能让普通 read query 等待 pending command。 | read-not-blocked integration test + mixed read/write smoke。 |
| Security | 缺 DSN、缺 driver、migration 未 ready、sidecar writer 冲突、contract mismatch 必须 fail closed。 | production fail-closed API/runtime tests。 |
| Privacy | DSN、token、secret、raw prompt/user payload 不进入 command metadata、dead-letter、audit 或 health。 | redaction snapshot tests。 |
| Compatibility | 本地 dev/test 可继续 SQLite；production canonical backend 不能是 SQLite。 | config matrix tests。 |
| Observability | readiness 必须暴露 queue backlog、oldest pending age、dead-letter、worker heartbeat、migration state、DB connectivity。 | observability tests + runbook check。 |
| Operability | 必须支持 worker drain、stale lease recovery、dead-letter triage、migration gate 和 backup/restore 演练。 | runbook acceptance + ops drill evidence gate。 |
| Testability | fake/contract tests 可用于本地 TDD，但生产 ready 需要真实 PostgreSQL integration evidence。 | test spec integration gate。 |

## 4. Implementation Strategy

采用“先 contract / schema / fake tests，再 PostgreSQL queue / worker，再 runtime integration / observability”的分阶段 TDD 路线。核心原则：

- 先补失败测试和 contract，再写实现。
- 先证明 queue ordering、deadlock retry、read-not-blocked，再接入上层业务。
- 新 State Platform 独立成清晰模块，不把所有语义塞回 `StoragePort`。
- SQLite 迁移、远端 PostgreSQL 配置、生产 cutover 不在本计划执行。
- 依赖选择进入单独 checkpoint；未完成 dependency gate 前不修改 runtime 依赖。

### 4.1 Proposed Module Boundaries

建议新增模块边界：

```text
src/state/
  contracts.py          # StateService / ReadStore / WriteQueue / CommandHandler protocols + DTOs
  errors.py             # typed transient/non-retryable errors and error policy
  commands.py           # command type registry, schemas, partition rules
  service.py            # StateService orchestration, execute-and-wait, command group
  health.py             # health/readiness model
  postgres/
    config.py           # DSN/env parsing, production fail-closed rules
    engine.py           # async engine/pool construction, statement timeouts
    schema.py           # SQL DDL metadata or migration descriptors
    read_store.py       # read-only query implementation
    write_queue.py      # command enqueue/claim/result repository
    worker.py           # writer worker lifecycle
    handlers.py         # business command handlers
    observability.py    # metrics/audit payload helpers
  testing/
    fake_state_service.py
    fake_postgres.py or fixtures.py
```

保留 `src/storage/sqlite/` 作为开发 / 测试兼容路径；迁移期可增加 `src/state/adapters/legacy_storage_port.py`，但 adapter 不得新增长期业务语义。

### 4.2 Checkpoint Gate Matrix

| Checkpoint | 目标 | 退出门禁 | 可并行性 |
| --- | --- | --- | --- |
| CP-0 Dependency + contract red tests | 明确 PostgreSQL driver 方案、写 State Platform contract、先补失败测试。 | dependency decision 记录完成；contract tests 指向缺失实现失败；未接入生产 runtime。 | 不并行；所有后续依赖。 |
| CP-1 PostgreSQL schema descriptors | 定义业务表 PostgreSQL 等价 schema、`state_write_command`、partition cursor、dead-letter/archive、migration ledger。 | schema tests 校验字段、索引、约束、status enum、idempotency unique、partition sequence。 | CP-0 后可独立。 |
| CP-2 Queue repository + worker claim | 实现 enqueue、claim、lease、heartbeat、complete/fail/dead-letter、retry schedule。 | 并发 claim tests 证明 `SKIP LOCKED` 与同 partition 不跳序。 | 可与 CP-3 handler contract 并行，但共享 command DTO 需冻结。 |
| CP-3 Command handler framework | 建立 handler registry、payload validation、partition rule、idempotency、error policy、no-external-IO rule。 | handler unit tests 覆盖 retry/non-retry 分类与 short transaction contract。 | 可与 CP-2 并行。 |
| CP-4 Read store + read-not-blocked integration | 实现 read pool / read-only query，并证明 pending/locked writer 不阻塞 reader。 | PostgreSQL integration tests 通过；读不读取 pending queue。 | 依赖 CP-1；可与 CP-2/3 后半并行。 |
| CP-5 StateService + execute-and-wait | 串起 submit、execute-and-wait、command group、result polling、deadline/cancel。 | service tests 覆盖 success/fail/timeout/cancel/idempotent replay。 | 依赖 CP-2/3。 |
| CP-6 Runtime integration seam | API runtime 装配 StateService；生产 mode fail-closed；旧 StoragePort 只作 migration/dev adapter。 | runtime assembly tests 证明 production 不落 SQLite fallback，dev/test 可继续 SQLite。 | 依赖 CP-5。 |
| CP-7 Observability + readiness | 暴露 queue backlog、oldest age、dead-letter、worker heartbeat、migration state、DB connectivity。 | health/readiness API tests + audit/metrics payload tests。 | 可与 CP-6 并行。 |
| CP-8 Full regression + docs sweep | 更新 README/API/PRD evidence，执行后端分层回归和静态检查。 | Required Verification Commands + `git diff --check` + dependency/license report。 | 最后执行。 |


## 4.3 Dependencies and Integration Points

| 依赖 / 集成点 | 当前证据 | 实施要求 |
| --- | --- | --- |
| PostgreSQL driver | `requirements.txt` 有 SQLAlchemy / PyMySQL，但无 PostgreSQL driver。 | CP-0 必须完成 driver ADR，确认 Python 3.13、SQLAlchemy 2.x async、license、error code、timeout/cancel 支持后才能新增依赖。 |
| API runtime | `src/api/runtime.py` 当前创建 SQLite engine 并装配 `SQLiteStorage`。 | CP-6 新增 State Platform runtime factory；production 缺 PostgreSQL readiness fail closed。 |
| Legacy storage | `src/core/contracts.py` 仍是 `StoragePort` repository-style 方法集合。 | 只允许作为迁移/dev adapter；新 queue/worker/health 语义不回填进 `StoragePort`。 |
| SQLite bootstrap | `src/storage/sqlite/bootstrap.py` 会在启动期执行 SQLite migration / `create_all()`。 | PostgreSQL migration 必须独立于 API 普通启动；readiness 读取 migration ledger。 |
| RuntimeSidecar | Python runtime 已有 sidecar shadow/enforce gate；Rust sidecar SQLite adapter 不是生产 State Platform。 | State Platform writer 与 RuntimeSidecar writer 必须互斥或 shadow-only，避免同一状态双 canonical writer。 |
| Audit / observability | 现有 audit sink 和 sidecar shadow diff 已有脱敏模式。 | 复用 audit-safe payload 思路，新增 queue/worker/dead-letter/readiness 指标。 |
| Remote production PostgreSQL | 用户后续部署并提供地址；当前本地不配置。 | 不把 DSN 写入 tracked 文件；配置通过 env / git-ignored config 注入。 |

## 4.4 Requirement Traceability Matrix

| Requirement | PRD section | Test spec coverage |
| --- | --- | --- |
| 读不阻塞 / last committed snapshot | §1、AC-3、CP-4 | Test Spec §7、§13 targeted read-not-blocked。 |
| 写入排队 / durable command | §1、AC-2、CP-2 | Test Spec §5、§9。 |
| Partition 保序 / 跨 partition 并行 | AC-4、AC-5、CP-2 | Test Spec §5 worker claim ordering。 |
| Deadlock / lock timeout retry | AC-6、CP-3 | Test Spec §6 fault injection。 |
| Non-retry fail closed | AC-7、NFR Security | Test Spec §3、§6、§10。 |
| Production PostgreSQL fail-closed | AC-11、AC-12、CP-6 | Test Spec §10。 |
| RuntimeSidecar 边界 | AC-13、Dependencies | Test Spec §10 sidecar conflict。 |
| 脱敏与隐私 | AC-18、NFR Privacy | Test Spec §11、§14。 |
| 真实 PostgreSQL 证据 | AC-17、NFR Testability | Test Spec §13 integration gate。 |
| 今日不迁移 / 不配置远端库 | AC-14、Out of Scope | Test Spec §1、§13。 |

## 5. Implementation Steps

### Step 1 — CP-0 dependency decision 与 contract red tests

**目标**：先把生产 PostgreSQL State Platform 的 contract 和失败测试锁住，不直接写 runtime 实现。

**文件**：
- 新增 `.omx/plans/` 之外的 dependency decision artifact（建议 `docs/superpowers/specs/2026-05-26-postgresql-driver-decision.md` 或 ADR）
- 新增 `src/state/contracts.py`
- 新增 `src/state/errors.py`
- 新增 `tests/storage/test_state_platform_contract.py`
- 新增 `tests/storage/test_state_platform_error_policy.py`

**实现要点**：
- 评估 PostgreSQL driver：必须基于官方 / 上游文档确认 Python 3.13、SQLAlchemy 2.x async 支持、license、pool、timeout、cancel、server-side errors 暴露方式。
- Dependency ADR 必须至少比较 `psycopg` / `asyncpg` / SQLAlchemy async dialect 组合，记录选择、拒绝项、license、维护状态、错误码映射、connection cancellation、statement timeout 设置方式。
- Contract 定义：`StateService`、`StateReadStore`、`StateWriteQueue`、`StateCommandHandler`、`StateHealthProvider`。
- Error policy 定义 retryable vs non-retryable，至少覆盖 `40P01`、`40001`、`55P03`、`57014`。
- 测试先红：当前仓库无 `src/state`，目标测试应稳定失败。

**完成标准**：dependency decision 可审阅；contract/error-policy tests 写好且能准确指向缺失实现；生产代码尚未接入。

### Step 2 — CP-1 PostgreSQL schema descriptors

**目标**：定义可测试的 PostgreSQL schema，不执行远端迁移。

**文件**：
- 新增 `src/state/postgres/schema.py`
- 新增 `tests/storage/test_postgres_state_schema_contract.py`
- 后续可新增 `migrations/postgres/` 或 `src/state/postgres/migrations/`（必须有命名与 rollback 策略）

**实现要点**：
- 定义 `state_write_command`：`command_id`、`command_type`、`idempotency_key`、`partition_key`、`partition_sequence`、`payload jsonb`、`status`、`priority`、`available_at`、`attempt_count`、`max_attempts`、`lease_owner`、`lease_expires_at`、`last_error_code`、`last_error_message`、`result jsonb`、`created_at`、`updated_at`、`completed_at`。
- 定义唯一约束：`idempotency_key` unique；`(partition_key, partition_sequence)` unique。
- 定义索引：claim index 覆盖 status / available_at / priority / created_at；partition outstanding index 覆盖 ordering check。
- 定义 `state_partition_cursor`、`state_dead_letter_command`、`state_command_archive`、`state_migration_ledger`。
- 业务表 PostgreSQL 等价 schema 使用 `jsonb`、`timestamptz`、明确 FK / index / cascade，不直接复刻 SQLite 宽松类型。

**完成标准**：schema contract tests 证明约束、索引、字段、状态枚举和 migration ledger 存在；不需要连接远端 DB。

### Step 3 — CP-2 queue repository 与 worker claim

**目标**：实现 durable enqueue / claim / lease / complete / retry / dead-letter 的 repository。

**文件**：
- 新增 `src/state/postgres/write_queue.py`
- 新增 `src/state/postgres/worker.py`
- 新增 `tests/storage/test_postgres_write_queue_contract.py`
- 新增 `tests/storage/test_postgres_worker_claim_ordering.py`

**核心 claim SQL**：

```sql
SELECT c.command_id
FROM state_write_command c
WHERE c.status IN ('pending', 'retrying')
  AND c.available_at <= now()
  AND (c.lease_expires_at IS NULL OR c.lease_expires_at < now())
  AND NOT EXISTS (
    SELECT 1
    FROM state_write_command prior
    WHERE prior.partition_key = c.partition_key
      AND prior.partition_sequence < c.partition_sequence
      AND prior.status NOT IN ('succeeded', 'failed', 'dead_letter', 'cancelled')
  )
ORDER BY c.priority DESC, c.created_at ASC
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

**实现要点**：
- enqueue 必须分配 partition sequence，并用 idempotency key 去重。
- claim 必须设置 lease owner / lease expiry。
- worker heartbeat 延长 lease；worker crash 后 lease 到期可被其他 worker 接管。
- complete/fail/dead-letter 必须在短事务内更新 command 状态和 result/error。
- retry schedule 使用 exponential backoff + jitter，max attempts 默认 5，可按 command type 覆盖。

**完成标准**：并发 worker 测试证明同一 partition 不跳序、不同 partition 可并行、lease expiry 后可恢复。

### Step 4 — CP-3 command handler framework

**目标**：建立业务写入 handler 的生产边界。

**文件**：
- 新增 `src/state/commands.py`
- 新增 `src/state/postgres/handlers.py`
- 新增 `tests/storage/test_state_command_handlers.py`
- 新增 `tests/storage/test_state_command_error_policy.py`

**实现要点**：
- 每个 handler 声明 command type、payload schema、partition key rule、idempotency rule、锁顺序、可重试错误集合、结果 schema。
- Handler transaction 只允许 DB 操作，不允许 LLM/HTTP/Skill/MCP/file IO。
- 对状态机错误、权限错误、payload validation、handler bug、安全 contract mismatch 直接 non-retry fail closed。
- 先实现基础 commands：conversation/message/task/event/artifact/auth token/interrupt/cancel/mailbox/pending skill context。

**完成标准**：handler registry 可验证所有 command type 均有 schema 与 partition rule；错误分类稳定可测。

### Step 5 — CP-4 read store 与 read-not-blocked 集成证明

**目标**：实现 PostgreSQL read store，并证明读取不被 pending command 或 writer 未提交事务阻塞。

**文件**：
- 新增 `src/state/postgres/read_store.py`
- 新增 `tests/storage/test_postgres_read_store_contract.py`
- 新增 `tests/storage/test_postgres_read_not_blocked_by_writer.py`

**实现要点**：
- 默认 `READ COMMITTED`，可显式 read-only transaction。
- 禁用 read query 中的 `FOR UPDATE` / write lock。
- 强制 statement timeout、分页、慢查询观察字段。
- Read store 只读业务表，不读 pending command queue。

**完成标准**：测试构造 writer 未提交变更时，reader 能返回旧 committed row；pending command 不影响读返回。

### Step 6 — CP-5 StateService orchestration

**目标**：串起 read/write/service API，让上层获得明确语义。

**文件**：
- 新增 `src/state/service.py`
- 新增 `tests/storage/test_state_service_execute_and_wait.py`
- 新增 `tests/storage/test_state_service_command_group.py`

**实现要点**：
- `submit_command()` 返回 durable command id。
- `execute_command_and_wait()` 等待 committed / failed / timeout，并支持 caller deadline/cancel。
- `transactional_command_group()` 只用于确需原子的多写；不得跨外部 IO。
- command result 可按 id 查询，错误以 typed error 返回。

**完成标准**：service tests 覆盖 success、non-retry fail、retry success、retry exhausted、timeout、cancel、idempotent replay。

### Step 7 — CP-6 runtime integration seam

**目标**：把 State Platform 引入 runtime 装配，但不做生产切换 / 数据迁移。

**文件**：
- `src/api/runtime.py`
- 新增 `src/api/state_runtime.py` 或 `src/state/runtime_factory.py`
- 更新 `src/core/contracts.py`（只保留兼容 adapter 需要的最小桥接；不把新语义塞进 `StoragePort`）
- 新增 `tests/api/test_state_platform_runtime_assembly.py`
- 新增 `tests/api/test_state_platform_production_fail_closed.py`

**实现要点**：
- 新增 `MAF_STATE_STORE_BACKEND` / `MAF_POSTGRES_STATE_DSN` 等配置入口，但本计划不写真实远端地址。
- production mode 选择 PostgreSQL 且缺 DSN / driver / migration ready 时 fail closed。
- dev/test mode 允许 SQLite legacy storage，但不得标记为 production-ready。
- RuntimeSidecar shadow/enforce 与 State Platform writer 必须互斥或明确 shadow-only，避免双 canonical writer。

**完成标准**：runtime assembly tests 证明 production 不 fallback SQLite；dev/test 旧路径仍可跑；缺生产配置时 readiness 失败而非隐式成功。

### Step 8 — CP-7 observability、health/readiness 与 runbook

**目标**：让生产运维能看见 DB、queue、worker、migration 状态。

**文件**：
- 新增 `src/state/health.py`
- 新增 `src/state/postgres/observability.py`
- 更新 `src/api/routes/health.py`（如存在；否则新增 health/readiness route）
- 新增 `tests/observability/test_state_platform_health.py`
- 新增 `docs/runbooks/postgresql-state-platform.md`

**实现要点**：
- health：process alive、optional DB ping。
- readiness：DB connectivity、migration ledger ready、writer workers heartbeat、queue backlog below threshold、dead-letter threshold、oldest pending age。
- audit/metrics 不记录 DSN、raw payload、access token、secret。
- runbook 包含 dead-letter triage、worker drain、lease recovery、schema migration gate、backup/restore 演练口径。

**完成标准**：observability tests 和 runbook review 通过。

### Step 9 — CP-8 docs sweep 与 full regression

**目标**：收口文档、静态扫描、全量回归和 license/supply-chain 说明。

**文件**：
- `README.md`
- `AGENTS.md`（仅当新增标准命令/工具时）
- `CHANGELOG.md`
- 相关 PRD / runbook / evidence docs

**完成标准**：Required Verification Commands 通过；新增依赖有 license 说明；无生产 fallback 到 SQLite；无未解释的 direct write path。

## 6. Risks and Mitigations

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| PostgreSQL driver 选择不当 | async cancel、timeouts、error code 映射不稳定。 | CP-0 单独做 dependency decision，基于官方文档和小型 spike；未通过不进入实现。 |
| 新旧写路径并存导致双写或绕过 queue | 破坏 ordering / idempotency。 | runtime assembly tests + static sweep + code review checklist；生产只允许一个 canonical writer。 |
| `SKIP LOCKED` claim 跳过同 partition 早期 command | 同一会话/任务状态乱序。 | 使用 `NOT EXISTS prior` guard；并发集成测试覆盖。 |
| Retry 错误分类过宽 | 把业务 bug / 权限错误伪装成暂态。 | error policy 白名单；未知错误默认 non-retry fail closed。 |
| 读旧 committed snapshot 被误解为 read-your-write | 用户刚写入后立即读到旧值。 | API contract / docs 明确；需要同步语义的调用使用 `execute_command_and_wait()`。 |
| Migration 与 API 启动耦合 | 生产启动时 DDL 争锁或半迁移。 | migration ledger 独立；readiness gate；普通 API 启动不隐式迁移。 |
| RuntimeSidecar 与 State Platform 职责冲突 | 两套状态内核同时写同一表。 | CP-6 明确互斥 / shadow-only；未来 Rust canonicalization 另开 PRD。 |
| 本地无法跑真实 PostgreSQL | 测试覆盖不足。 | 单元/contract tests 可本地跑；PostgreSQL integration tests 允许服务容器或显式 env gate，生产地址由后续提供。 |

## 7. Out of Scope / Deferred Work

- SQLite -> PostgreSQL 数据迁移、校验、cutover、rollback：后续单独 PRD。
- 远端 PostgreSQL DSN、账号、TLS、网络策略：用户部署后再接入，不写入 tracked 文件。
- RuntimeSidecar 直接成为 State Platform canonical writer：未来 Rust 化专题另行评审。
- Redis/Kafka/RabbitMQ 外部队列：第一阶段不引入；如 PostgreSQL queue 无法满足吞吐再评估。
- 多区域 active-active：不在当前内部业务交付范围。


## 7.5 Assumptions and Open Questions

| 类型 | 内容 | 处理方式 |
| --- | --- | --- |
| Assumption | 用户会在后续阶段提供远端生产 PostgreSQL 地址和访问策略。 | 本计划只定义配置入口和 fail-closed 行为，不写入真实 DSN。 |
| Assumption | 第一阶段 PostgreSQL internal queue 足以覆盖当前内部业务吞吐；暂不引入外部消息队列。 | CP-8 若压测不满足，再追加外部队列评估 PRD。 |
| Assumption | SQLite 仍需要保留给本地开发 / 单元测试，但不能作为 production canonical store。 | Config matrix tests 固化 dev/test vs production 行为。 |
| Open gate | PostgreSQL driver 尚未选择。 | CP-0 dependency ADR 解决；未通过不得进入实现。 |
| Open gate | 真实 PostgreSQL integration 环境尚未配置。 | 可先用 fake/contract TDD；进入 production ready 前必须补真实 PostgreSQL evidence。 |
| Open gate | SQLite -> PostgreSQL 数据迁移方案未设计。 | 后续独立 migration PRD；本计划不得隐式执行迁移。 |
| Open gate | RuntimeSidecar 长期是否承载部分 StateCommandHandlers 尚未决定。 | 当前只要求互斥 / shadow-only；长期 Rust canonicalization 另开 PRD。 |

## 8. Verification Steps

Targeted during implementation:

```bash
conda run -n multi_agent python -m unittest tests.storage.test_state_platform_contract tests.storage.test_state_platform_error_policy
conda run -n multi_agent python -m unittest tests.storage.test_postgres_state_schema_contract tests.storage.test_postgres_write_queue_contract tests.storage.test_postgres_worker_claim_ordering
conda run -n multi_agent python -m unittest tests.storage.test_state_command_handlers tests.storage.test_postgres_read_store_contract tests.storage.test_state_service_execute_and_wait
conda run -n multi_agent python -m unittest tests.storage.test_postgres_deadlock_retry tests.storage.test_postgres_lock_timeout_retry tests.storage.test_postgres_serialization_retry
conda run -n multi_agent python -m unittest tests.api.test_state_platform_runtime_assembly tests.api.test_state_platform_production_fail_closed
conda run -n multi_agent python -m unittest tests.observability.test_state_platform_health
```

Regression before claiming implementation complete:

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

Static sweeps before production handoff:

```bash
rg -n "append_event\(|save_task\(|save_message\(|save_artifact\(|save_conversation\(" src tests
rg -n "MAF_STATE_STORE_BACKEND|MAF_POSTGRES_STATE_DSN|sqlite" src tests docs README.md
rg -n "FOR UPDATE|SKIP LOCKED|lock_timeout|statement_timeout|idle_in_transaction" src/state tests/storage
```

## 9. Handoff Guidance

Recommended follow-up execution lane:

- **Default**：`$ultragoal` over this PRD + test spec for durable checkpoint tracking.
- **Parallel execution**：use `$team` only after CP-0 freezes dependency and contract; split CP-1/CP-2/CP-3/CP-4 across backend/storage/test lanes.
- **Sequential fallback**：use `$ralph` only if a single owner must verify each checkpoint before moving on.

Suggested roles if using team:

| Lane | Role | Scope |
| --- | --- | --- |
| Architecture / contract | architect + executor | `src/state/contracts.py`, error policy, dependency decision. |
| Storage / PostgreSQL | executor + test-engineer | schema, queue, worker claim, integration fixtures. |
| Runtime integration | executor | API runtime assembly, production fail-closed, sidecar boundary. |
| Verification | verifier / code-reviewer | ordering, retry, no direct write bypass, docs/runbook. |

Stop condition for planning phase: this PRD and its test spec exist, pass static document checks, and are committed without runtime changes.
