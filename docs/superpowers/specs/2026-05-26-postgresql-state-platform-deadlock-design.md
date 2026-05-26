# PostgreSQL State Platform 防死锁与写队列设计

- **项目**：breeding_agent
- **日期**：2026-05-26
- **状态**：设计稿（brainstorming 审批后落文档；今日不实施迁移）
- **范围**：生产级状态存储架构、读写隔离、写队列、防死锁、失败恢复、测试验收
- **非范围**：今日不做 SQLite -> PostgreSQL 数据迁移；不配置远端 PostgreSQL；不改生产启动方式；不删除 SQLite；不做 cutover / shadow / rollback 操作

## 1. 背景

当前仓库的状态存储主路径集中在 `src/storage/sqlite/`，API runtime 在启动时创建 SQLite engine 并装配 `SQLiteStorage`。此前审计发现：

- Python SQLite 主状态库缺少统一的 busy timeout、WAL、operation deadline、locked retry 与写入排队设计。
- SQLite bootstrap / DDL 迁移缺少 migration lock、busy retry 与 bounded timeout。
- MySQL readonly 已有 SQL Guard、deadline 和部分锁语句禁止，但不承担主状态库职责。
- Rust RuntimeSidecar SQLite 有进程内单连接串行化和短事务，但仍不是完整生产状态平台。

用户明确要求：

- 读取不阻塞，读取最后已提交快照即可。
- 写入可以排队。
- 生产必须使用 PostgreSQL，部署在远端服务器；本地当前无需配置生产库。
- 不追求最小改动；优先成熟稳定的生产级交付设计。
- 不以现有 `StoragePort` 作为长期架构核心。
- 今天只落设计，不做迁移实施。

### 1.1 用户、干系人与受影响系统

| 对象 | 关注点 | 受影响范围 |
| --- | --- | --- |
| 内部业务用户 | 对话、任务、上传、Skill 结果不能因状态库锁等待长期卡住；读取历史和任务状态应稳定返回最后已提交结果。 | API、SSE、前端业务对话台 |
| 后端维护者 | 状态写入必须可排队、可恢复、可审计；deadlock / lock timeout 必须有稳定处理路径。 | `src/api/`、`src/orchestration/`、`src/lifecycle/`、`src/storage/` |
| 运维 / 部署维护者 | 生产 PostgreSQL、writer workers、queue backlog、health/readiness、备份与恢复必须可观测、可演练。 | 部署配置、监控、runbook、CI/CD |
| 安全与审计维护者 | 不得把权限、安全、schema、contract 失败通过 retry 或 fallback 伪装成成功；敏感配置和 payload 必须脱敏。 | auth、audit、Rust safety/runtime contract |
| Skill / MCP / 主代理能力开发者 | 能力层不应在 DB transaction 内执行外部调用；状态变更通过明确 command handler 进入队列。 | capability executor、Skill runtime、MCP runtime |

### 1.2 当前仓库证据

| 证据 | 仓库位置 | 对设计的约束 |
| --- | --- | --- |
| API runtime 当前直接创建 SQLite engine 并装配 `SQLiteStorage`。 | `src/api/runtime.py` | 生产 State Platform 需要替代 runtime storage 装配，而不是只改单个 repository 方法。 |
| `StoragePort` 当前是大量 `save_*` / `get_*` 方法集合。 | `src/core/contracts.py` | 它可作为迁移期 adapter 边界，但缺 lifecycle、queue、migration、health/readiness 语义，不作为长期生产核心。 |
| SQLite engine 仅设置 `check_same_thread=False`。 | `src/storage/sqlite/session.py` | 当前 SQLite 主路径不能证明具备生产级读写锁治理。 |
| SQLite bootstrap 会执行 DDL/DML。 | `src/storage/sqlite/bootstrap.py` | 迁移、schema 变更和 production cutover 必须独立于 API 启动路径。 |
| 基础设施建议已把 PostgreSQL 状态存储、durable dispatcher、事件 replay、health/readiness 列为生产基座。 | `docs/Agent基础设施优化建议.md` | 本设计应覆盖状态平台、持久队列、事件/任务恢复和可观测性，不只替换数据库方言。 |
| requirements 当前有 SQLAlchemy / PyMySQL，但没有 PostgreSQL driver。 | `requirements.txt` | PostgreSQL driver 选择必须进入实施计划；本设计不假设当前环境已能连接 PostgreSQL。 |
| RuntimeSidecar 已有 health/readiness、sidecar shadow/enforce 和 release gate 文档。 | `docs/prd/rust/03-DispatcherStoreEventSidecarPRD.md`、`native/crates/maf_runtime_sidecar/` | State Platform 需要明确与 RuntimeSidecar 的边界，避免两个 canonical writer 冲突。 |

### 1.3 本次完备性标准

本设计被视为可进入后续实施计划，必须同时满足：

- 目标、非目标和今日停止边界清晰。
- 用户价值、干系人、受影响系统明确。
- 读写语义、partition 保序、command lifecycle、retry/dead-letter 规则可测试。
- 可靠性、安全、隐私、兼容、性能、观测和运维要求可验收。
- PostgreSQL 依赖、配置、测试环境和生产 fail-closed 规则明确。
- 迁移、cutover、rollback 被标记为未来独立阶段，不被今日设计隐式执行。
- 与当前仓库证据不矛盾；缺失信息以显式开放事项或假设记录。

## 2. 目标

设计一套生产级 **State Platform**，使系统满足：

1. PostgreSQL 是生产 canonical state store。
2. 读请求不等待写队列，直接读取 PostgreSQL 已提交快照。
3. 所有业务状态写入统一进入 PostgreSQL-backed write queue，由 writer workers 执行。
4. 同一业务分区内写入严格保序，不同分区可并行。
5. Deadlock、lock timeout、serialization failure 等可恢复错误进入 bounded retry。
6. 不可恢复错误 fail closed，并进入稳定 dead-letter / failed 状态。
7. Queue、worker、migration、health、readiness 是一等生产能力。
8. SQLite 仅保留本地开发 / 测试兼容和未来迁移来源，不作为生产可靠性边界。

## 3. 非目标

本设计不做：

- 不在今日连接或配置远端 PostgreSQL。
- 不在今日执行 SQLite -> PostgreSQL 迁移。
- 不引入 Redis / Kafka / RabbitMQ 作为第一阶段必需依赖。
- 不把 production PostgreSQL 不可用时 fallback 到 SQLite。
- 不把权限失败、安全失败、schema/contract mismatch 包装成成功。
- 不在 DB transaction 内调用 LLM、HTTP、Skill、MCP 或文件 IO。
- 不要求读请求看到已排队但尚未提交的写入。
- 不继续把现有 `StoragePort` 视为长期生产状态平台接口。

## 4. 推荐方案

采用 **PostgreSQL canonical store + PostgreSQL 内部 write command queue / outbox + writer workers**。

未选择方案：

- **外部消息队列**：吞吐和解耦更强，但 exactly-once、幂等、replay 与运维复杂度更高；当前不是第一阶段必要依赖。
- **RuntimeSidecar 直接作为 canonical writer**：长期可演进，但当前 sidecar 仍在 shadow/enforce 迁移阶段，直接作为生产写入核心风险和周期都更高。

选择 PostgreSQL 内队列的原因：

- PostgreSQL MVCC 天然支持读写隔离，读可看旧 committed snapshot。
- `FOR UPDATE SKIP LOCKED` 是成熟的多 worker 抢占模式。
- command queue、业务写入、结果状态可在同一个数据库内保持一致。
- 少引入基础设施，便于生产运维和灾备。

## 5. 总体架构

生产状态平台由以下组件组成：

```text
API / Orchestration / Lifecycle / Auth
        |
        v
StateService
  ├── StateReadStore
  ├── StateWriteQueue
  ├── StateCommandHandlers
  ├── StateWriterWorkers
  ├── StateMigrationService
  └── StateHealth / Readiness
        |
        v
PostgreSQL
```

### 5.1 StateService

StateService 是长期的上层状态入口，逐步替代旧 repository / StoragePort 思维。它提供明确读写语义：

- `query_*` / `get_*`：只读，直接读已提交业务表。
- `submit_command()`：提交异步写命令。
- `execute_command_and_wait()`：提交写命令并等待该命令 committed / failed。
- `transactional_command_group()`：多个写命令需要原子执行时使用。
- `health()` / `readiness()`：暴露 DB、queue、worker、migration gate 状态。

迁移期可以存在 `LegacyStoragePortAdapter -> StateService`，但它只是兼容桥，不是长期架构核心。

### 5.2 StateReadStore

只负责读取：

- 使用 PostgreSQL read pool。
- 默认 `READ COMMITTED`。
- 可使用显式 read-only transaction。
- 不读 pending queue。
- 不使用 `FOR UPDATE`。
- 强制分页、statement timeout、慢查询观测。

读请求看到最后已提交状态。若写命令已排队但未提交，读请求不等待它。

### 5.3 StateWriteQueue

所有业务写入统一转为 typed command。Command 是写入系统的 durable intent，也是 retry / idempotency / audit / recovery 的基础单位。

### 5.4 StateCommandHandlers

每类写操作一个 handler。Handler 声明：

- command type；
- payload schema；
- 读写表集合；
- partition key 规则；
- 幂等规则；
- 是否可重试；
- 锁顺序；
- 结果 schema；
- 错误码。

### 5.5 StateWriterWorkers

Writer worker 是唯一生产业务写入执行者：

- 从 `state_write_command` 抢命令；
- 按 partition 保序；
- 在短事务内调用 handler；
- 对可重试错误执行 bounded retry；
- 对不可恢复错误进入 failed / dead-letter；
- shutdown 时 drain 或释放 leased commands。

## 6. 读写语义

### 6.1 读取语义

读取不阻塞：

- 读直接访问 PostgreSQL 业务表。
- 读不检查写队列是否清空。
- 读不等待 pending command。
- 读不承诺 read-your-queued-write。
- 读只承诺最后已提交状态。

这与用户确认的语义一致：读请求可读旧 committed snapshot。

### 6.2 写入语义

写入必须排队：

1. 上层把状态修改提交为 command。
2. Command durable 写入 `state_write_command`。
3. Writer worker 抢占 command。
4. Handler 在短事务内写业务表。
5. Command 标记为 `succeeded` / `failed` / `dead_letter`。
6. 需要同步确认的调用等待自己的 command 结果；其他读请求不等待。

### 6.3 Partition 顺序

同一业务分区严格保序，不同分区并行。

建议 partition 规则：

- `conversation:{conversation_id}`：conversation、message、pending skill context 等会话级写入。
- `task:{task_id}`：task、node、edge、artifact、event、interrupt、checkpoint、mailbox 等任务级写入。
- `auth:{username}`：用户 token 当前性、登录态相关写入。
- `system:migration`：schema / migration / cutover gate。

同一 command 如果同时影响多个 scope，应由 handler 明确声明主 partition，并在 transaction 内按全局锁顺序访问其他表。

## 7. 功能需求

| ID | 需求 | 验收方式 |
| --- | --- | --- |
| FR-1 | 生产 StateService 必须区分 read API、异步 command submit、同步 execute-and-wait 和 command group。 | API/单元测试覆盖接口语义；调用方不能绕过 writer 直接写业务表。 |
| FR-2 | StateReadStore 必须直接读取 PostgreSQL 已提交业务表，不读取 pending queue，不使用业务写锁。 | 集成测试证明 writer 未提交时 reader 可读取旧 committed snapshot。 |
| FR-3 | 所有生产业务写入必须持久化为 typed command，并通过 writer worker 执行。 | 静态/架构测试禁止 production 业务写路径直接绕过 command handler。 |
| FR-4 | 同一 `partition_key` 内 command 必须按 `partition_sequence` 严格提交。 | 并发集成测试验证同 partition 不乱序。 |
| FR-5 | 不同 partition 的 command 可以并行执行，但不能违反各自 partition 顺序。 | 多 worker 集成测试验证不同 partition 并行吞吐。 |
| FR-6 | Command 必须支持 idempotency；同一 `(command_type, idempotency_key)` 重复提交返回同一逻辑结果或稳定冲突。 | 单元/集成测试覆盖重复提交、payload fingerprint mismatch。 |
| FR-7 | Worker crash 后，leased command 必须能在 lease 过期后被安全 reclaim。 | 故障注入测试 kill worker 后 command 最终 terminal。 |
| FR-8 | Deadlock、lock timeout、serialization failure 等可恢复错误必须 bounded retry；达到上限进入 dead-letter。 | PostgreSQL 故障注入测试覆盖错误码、attempt、backoff、dead-letter。 |
| FR-9 | 权限、安全、schema、contract、非法状态机错误必须 fail closed，不得 retry 成成功。 | 负向测试覆盖不可重试错误。 |
| FR-10 | Health/readiness 必须暴露 PostgreSQL 连接、queue backlog、oldest pending age、worker 存活、dead-letter 和 migration gate。 | API/运维测试验证健康状态和 degraded/not-ready 条件。 |

## 8. 非功能需求

| 维度 | 要求 | 验收方式 |
| --- | --- | --- |
| 可靠性 | 写入必须 durable enqueue；worker crash、DB transient error、进程重启不得导致 command 静默丢失。 | crash/restart/reclaim 集成测试。 |
| 性能 | 读路径不等待 pending queue；写 backlog 增长时读 P95 不应因 queue wait 线性上升。 | 读写混合压测。 |
| 安全 | Production PostgreSQL 缺配置、schema/cutover gate 未通过、contract mismatch 必须 fail closed。 | 启动/配置负向测试。 |
| 隐私 | Queue、audit、dead-letter、migration report 不得记录 DSN、token、secret、本地路径或未脱敏大 payload。 | 脱敏快照测试。 |
| 兼容 | SQLite 只能作为 dev/test 和未来迁移来源；production 不允许 fallback 到 SQLite。 | production env 启动测试。 |
| 可观测性 | queue depth、oldest age、attempts、dead-letter、worker lease、retry code、read/write latency 必须有 metrics 或 audit-safe 事件。 | 指标/审计契约测试。 |
| 可运维性 | 必须提供 worker drain、lease recovery、dead-letter inspection、backpressure、backup/restore 与 migration gate runbook。 | 运维演练验收。 |
| 可测试性 | PostgreSQL 行为必须在真实 PostgreSQL 集成测试中验证；本地 skip 不能替代生产证据。 | CI service container 或远端测试库报告。 |

## 9. PostgreSQL 数据模型

生产库分为业务状态表、写队列表、运维迁移表。

### 9.1 业务状态表

从现有 SQLite 状态表升级为 PostgreSQL schema：

- ID 字段继续使用 `text`，保持现有业务 ID 兼容。
- JSON 字段使用 `jsonb`。
- 时间字段使用 `timestamptz`。
- boolean / integer 使用原生类型。
- 索引按读取路径重建。
- 外键可分阶段启用，避免第一阶段被历史脏数据阻断。

核心业务表：

- `conversation`
- `conversation_memory_summary`
- `conversation_pending_skill_context`
- `auth_user_token`
- `message`
- `task`
- `task_node`
- `task_edge`
- `artifact`
- `event_record`
- `mailbox_message`
- `mailbox_delivery`
- `interrupt`
- `interrupt_answer`
- `checkpoint`

### 9.2 写命令表

核心表：`state_write_command`

建议字段：

```sql
command_id text primary key,
command_type text not null,
idempotency_key text not null,
partition_key text not null,
partition_sequence bigint not null,
causality_key text,
payload jsonb not null,
status text not null,
priority integer not null default 0,
available_at timestamptz not null,
deadline_at timestamptz,
attempt_count integer not null default 0,
max_attempts integer not null default 5,
lease_owner text,
lease_expires_at timestamptz,
result jsonb,
last_error_code text,
last_error_message text,
created_at timestamptz not null,
updated_at timestamptz not null
```

关键约束 / 索引：

- unique `(command_type, idempotency_key)`。
- index `(status, available_at, priority, created_at)`。
- index `(partition_key, status, partition_sequence)`。
- index `(lease_expires_at)`。
- check `status in ('pending', 'leased', 'succeeded', 'retrying', 'failed', 'dead_letter', 'cancelled')`。

### 9.3 Partition cursor 表

表：`state_partition_cursor`

```sql
partition_key text primary key,
next_sequence bigint not null,
blocked_command_id text,
updated_at timestamptz not null
```

Enqueue 时在同一 transaction 内锁定 cursor row，分配单调 `partition_sequence`。Worker 只能执行该 partition 当前最小未完成 sequence，避免后序 command 越过前序 command。

### 9.4 Dead-letter / archive

失败命令不直接删除：

- 近期保留在 `state_write_command`，状态为 `dead_letter`。
- 长期可归档到 `state_write_command_archive`。
- 归档 payload 需要遵守脱敏策略，避免泄露 prompt、secret、token、DSN、本地路径或完整用户内容。

### 9.5 Migration / cutover 表

未来迁移阶段使用，不在今日执行：

- `state_schema_migration`
- `state_migration_run`
- `state_migration_validation`
- `state_cutover_gate`

用途：记录 schema version、迁移批次、校验结果、cutover gate 状态。

## 10. 防死锁与超时策略

### 10.1 短事务规则

DB transaction 内只能做：

- command 状态更新；
- 当前状态读取和校验；
- 业务表写入；
- 结果写回；
- 必要的 audit-safe metadata。

禁止在 transaction 内做：

- LLM 调用；
- HTTP / MCP / Skill 调用；
- 文件 IO；
- 长时间 CPU 计算；
- 等待其他异步任务。

### 10.2 固定锁顺序

队列抢占事务与业务写事务分离：

- **抢占事务** 只锁 `state_write_command` / `state_partition_cursor`，负责 claim、lease、recover，不访问业务表。
- **业务写事务** 只在 command 已 leased 后执行，按固定顺序访问业务表，并在最后更新 command result / terminal status。

业务写事务涉及多表写入时必须按统一顺序访问：

```text
conversation
-> conversation_memory_summary
-> conversation_pending_skill_context
-> auth_user_token
-> message
-> task
-> task_node
-> task_edge
-> artifact
-> event_record
-> mailbox_message
-> mailbox_delivery
-> interrupt
-> interrupt_answer
-> checkpoint
-> state_write_command(result/status only)
```

实际 handler 可声明更小子集，但不能反向访问。`state_partition_cursor` 只在 enqueue / recovery 类队列事务中更新，不应混入业务写事务。

### 10.3 PostgreSQL 超时

Writer transaction 必须设置：

- `lock_timeout`
- `statement_timeout`
- `idle_in_transaction_session_timeout`

Read transaction 设置较短 `statement_timeout`，避免慢查询拖垮连接池。

### 10.4 可重试错误

以下错误可 bounded retry：

- `40P01` deadlock detected；
- `40001` serialization failure；
- `55P03` lock not available；
- `57014` statement timeout / cancelled；
- transient connection error；
- pool checkout timeout；
- PostgreSQL restart 后的可恢复连接错误。

重试约束：

- 只重试具备 idempotency key 的 command。
- 指数退避 + jitter。
- 默认最大 5 次。
- 每次 retry 写 audit-safe metadata。
- 超过上限进入 `dead_letter`。

### 10.5 不可重试错误

以下错误不重试：

- payload schema validation failed；
- handler 不存在；
- ownership / permission denied；
- idempotency key 冲突但 payload fingerprint 不一致；
- schema contract mismatch；
- safety / Rust contract enforce fail-closed；
- 业务状态机非法迁移。

## 11. Worker 调度与恢复

### 11.1 抢占 SQL

Worker 使用 PostgreSQL row lock 抢 command：

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

抢到后在同一 transaction 标记 `leased`，设置 `lease_owner` 和 `lease_expires_at`。`NOT EXISTS` 是 partition 保序的 SQL 级保护；handler 层仍需二次校验，避免实现错误导致越序提交。

### 11.2 Partition 保序

Worker 执行前检查：

- 该 command 是其 partition 最小未完成 sequence；或
- 前序 command 均已 terminal。

如果前序 command 未完成，当前 command 不执行，释放或延后。

### 11.3 Lease 恢复

Worker crash 后：

- `lease_expires_at < now()` 的 command 可被其他 worker reclaim。
- reclaim 写入 attempt metadata。
- 如果 handler 已提交业务表但未更新 command，idempotency handler 必须能识别已提交结果并补写 command result。

### 11.4 Backpressure

当 queue backlog 或 oldest pending age 超阈值：

- readiness degraded；
- 可拒绝低优先级新写入；
- 保留关键系统写入；
- 输出 audit-safe 指标，不泄露 payload。

## 12. 配置边界

生产必须显式配置 PostgreSQL：

- `MAF_STATE_STORE_BACKEND=postgresql`
- `MAF_POSTGRES_DSN` 或等价 secret manager 引用
- `MAF_POSTGRES_POOL_SIZE`
- `MAF_POSTGRES_MAX_OVERFLOW`
- `MAF_POSTGRES_STATEMENT_TIMEOUT_MS`
- `MAF_POSTGRES_LOCK_TIMEOUT_MS`
- `MAF_WRITE_QUEUE_WORKERS`
- `MAF_WRITE_QUEUE_BATCH_SIZE`

规则：

- `MAF_API_ENV=production` 时，缺 PostgreSQL 配置必须 fail closed。
- Production 不允许自动 fallback 到 SQLite。
- DSN、密码、证书、token 不写入 tracked 文件。
- 本地当前不需要配置远端 PostgreSQL；用户后续提供远端地址后再进入实施/联调阶段。

## 13. 依赖与集成点

| 类别 | 设计要求 | 当前证据 / 约束 |
| --- | --- | --- |
| PostgreSQL server | 生产必须由远端 PostgreSQL 承载 canonical state store。 | 用户确认后续提供远端地址；今日不配置。 |
| Python PostgreSQL driver | 实施计划必须评估并选择 SQLAlchemy-compatible PostgreSQL driver。 | `requirements.txt` 当前没有 PostgreSQL driver；新增依赖需同步依赖快照和 License Requirement。 |
| API runtime | 生产启动需要从 SQLite bootstrap 改为 State Platform bootstrap。 | `src/api/runtime.py` 当前硬编码 SQLiteStorage 装配。 |
| Orchestration / lifecycle / auth | 长期应依赖 StateService 语义，不直接依赖旧 StoragePort 作为生产核心。 | 当前大量模块通过 StoragePort 访问状态，需分阶段替换或 adapter 过渡。 |
| RuntimeSidecar | 短期保持独立可靠性通道；不得与 State Platform 同时成为同一业务表的 canonical writer。 | Rust PRD 已定义 sidecar shadow/enforce 和 fail-closed gate。 |
| Audit / observability | Queue、worker、retry、dead-letter、backpressure 必须输出 audit-safe metrics/events。 | 现有 audit sink 与 Rust sidecar shadow 审计提供模式参考。 |
| CI / test env | 需要真实 PostgreSQL service container 或远端测试库。 | 本地无 PostgreSQL 时可 skip，但 skip 不是生产证据。 |

## 14. 迁移策略（未来阶段，不在今日实施）

迁移保留为未来独立阶段 / 独立 PRD：

1. Schema 建立。
2. SQLite 离线导出。
3. PostgreSQL 导入。
4. Row count / primary key / relationship / event replay 校验。
5. Shadow compare。
6. Read cutover。
7. Write queue enforce。
8. SQLite production decommission。

今日只记录设计，不执行任何迁移命令，不连接生产库，不改生产配置。

## 15. 测试与验收

### 15.1 单元测试

覆盖：

- command payload schema validation；
- idempotency key 重复提交；
- partition sequence 分配；
- command status transition；
- retry/backoff 计算；
- dead-letter 规则；
- 不可重试错误 fail closed；
- handler 锁顺序声明完整性。

### 15.2 PostgreSQL 集成测试

需要真实 PostgreSQL 测试环境。CI 可用 service container；本地没有 PostgreSQL 时允许显式 skip，但不能把 skip 当作生产证据。

场景：

- 多 worker 并发 `FOR UPDATE SKIP LOCKED`，同一 command 只执行一次。
- 同一 partition 严格按 sequence 执行。
- 不同 partition 并行执行。
- Reader 在 writer transaction 未提交时仍读旧 committed snapshot。
- Writer lock timeout 后 command retry。
- Deadlock detected 后 bounded retry。
- Worker crash 后 lease 过期可恢复。
- Queue backlog 超阈值 readiness degraded。
- Dead-letter 后不会无限重试。

### 15.3 业务回归测试

覆盖现有行为：

- conversation 创建、列表、重命名、删除；
- message append / history；
- task submit / node transition / graph edges；
- event append / replay / SSE；
- artifact metadata save / list / download；
- interrupt answer / resume；
- cancellation late result ignored；
- mailbox retry / expire；
- auth token rotate / logout / refresh 当前性；
- pending skill context continuation。

### 15.4 压测与故障注入

生产级验收必须有：

- 读写混合压力：读请求 P95 不因写 backlog 明显升高；
- 写队列 backlog 压力：worker 按 partition 保序，系统不死锁；
- PostgreSQL deadlock 注入；
- lock timeout 注入；
- worker kill -9 / restart；
- PostgreSQL restart / connection reset；
- long transaction 检测；
- slow query timeout；
- queue full / backpressure；
- graceful shutdown drain。

### 15.5 验收门槛

- Reader 不等待 pending queue。
- Deadlock / lock timeout 均转为 bounded retry 或 dead-letter。
- Command 无重复执行。
- 同一 partition 无乱序。
- Worker crash 后 leased command 可恢复。
- Queue 指标进入 health/readiness。
- Production 缺 PostgreSQL 配置 fail closed。
- SQLite 不作为 production fallback。

## 16. 需求追踪与验收矩阵

| 目标 / 需求 | 关键验收证据 |
| --- | --- |
| 读不阻塞 | PostgreSQL 集成测试：writer 持有未提交事务时，reader 读取旧 committed snapshot 且不等待 pending queue。 |
| 写入排队 | 所有 production 写路径静态/架构测试经过 `state_write_command`，writer worker 是唯一业务写执行者。 |
| Partition 保序 | 多 worker 并发测试证明同一 partition 按 `partition_sequence` 提交，不同 partition 可并行。 |
| Deadlock 防护 | 注入 `40P01` / `55P03` / `40001` 后 bounded retry，超过上限进入 dead-letter。 |
| 幂等 | 重复 `(command_type, idempotency_key)` 不重复写；payload fingerprint mismatch 返回稳定冲突。 |
| Worker 恢复 | kill worker 后 lease 过期、command 被 reclaim，并最终进入 terminal status。 |
| 生产配置 fail-closed | `MAF_API_ENV=production` 且缺 PostgreSQL 配置或 schema gate 未通过时启动失败。 |
| 敏感信息保护 | queue/dead-letter/audit/migration report 脱敏快照不含 DSN、token、secret、本地路径或完整大 payload。 |
| 今日边界 | 无迁移命令、无远端 DB 配置、无 runtime 代码变更被本设计文档要求立即执行。 |

## 17. 风险、假设与开放事项

### 17.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| StateService 替换范围大 | 上层 API / orchestration / lifecycle / auth 需要分阶段适配，短期复杂度上升。 | 先落 command contract 和 adapter，按高频写路径分批替换；每批都有回归测试。 |
| Queue 和业务表同库导致数据库压力集中 | PostgreSQL 同时承载读、queue、写事务和观测查询。 | 分离 read/write pool、限制 batch、控制 worker 并发、索引 queue 查询、设置 statement/lock timeout。 |
| Partition 规则设计错误 | 可能导致同一资源乱序或不必要串行。 | 每个 command handler 必须声明 partition，架构测试校验；并发测试覆盖关键业务流。 |
| Handler 幂等不足 | Worker crash 或 retry 可能重复业务写。 | 所有 handler 必须使用 idempotency key、结果 fingerprint 和业务唯一约束。 |
| 与 RuntimeSidecar canonical 边界冲突 | 两个系统可能同时写同一状态，造成漂移。 | 实施计划必须定义单一 canonical writer；sidecar 仅作为独立可靠性通道或明确 adapter，不双写同一业务表。 |
| PostgreSQL driver / 依赖选择不当 | 异步模型、SQLAlchemy 兼容、license 或部署 wheel 可能受影响。 | 独立 dependency-expert 评估；新增依赖同步 requirements 并执行 License Requirement。 |

### 17.2 已记录假设

- 生产 PostgreSQL 由用户后续提供地址、账号、TLS/网络策略和运维权限。
- 第一阶段不引入外部消息队列；PostgreSQL 内队列足以支撑当前生产目标。
- 读请求允许读取最后已提交快照，不要求 read-your-queued-write。
- SQLite 未来只保留 dev/test 和迁移来源，不承担 production fallback。

### 17.3 开放事项

以下事项不阻塞本设计落文档，但进入实施计划前需要拆解：

1. PostgreSQL Python driver 选择：需结合现有依赖快照、async/await 运行模型、SQLAlchemy 兼容性、wheel/Ubuntu 22.04/Python 3.13 与 license 评估。
2. StateService API 细化：哪些上层调用使用 `submit_command`，哪些使用 `execute_command_and_wait`，哪些需要 `transactional_command_group`。
3. Command handler 拆分顺序：建议从 event append / task transition / message append 等高频状态写入开始。
4. 迁移 PRD：未来单独设计 SQLite -> PostgreSQL 数据迁移、shadow、cutover、rollback。
5. RuntimeSidecar 关系：短期作为独立可靠性通道保留，长期可评估是否承载部分 StateCommandHandlers；任何阶段都必须避免同一状态双 canonical writer。

## 18. 今日停止条件

今天完成：

- 本设计文档落库；
- 自审通过；
- 等待用户 review。

今天不进入：

- 代码实现；
- PostgreSQL 配置；
- SQLite 数据迁移；
- 生产 cutover；
- writer worker 实作。
