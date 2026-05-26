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

## 7. PostgreSQL 数据模型

生产库分为业务状态表、写队列表、运维迁移表。

### 7.1 业务状态表

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

### 7.2 写命令表

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

### 7.3 Partition cursor 表

表：`state_partition_cursor`

```sql
partition_key text primary key,
next_sequence bigint not null,
blocked_command_id text,
updated_at timestamptz not null
```

Enqueue 时在同一 transaction 内锁定 cursor row，分配单调 `partition_sequence`。Worker 只能执行该 partition 当前最小未完成 sequence，避免后序 command 越过前序 command。

### 7.4 Dead-letter / archive

失败命令不直接删除：

- 近期保留在 `state_write_command`，状态为 `dead_letter`。
- 长期可归档到 `state_write_command_archive`。
- 归档 payload 需要遵守脱敏策略，避免泄露 prompt、secret、token、DSN、本地路径或完整用户内容。

### 7.5 Migration / cutover 表

未来迁移阶段使用，不在今日执行：

- `state_schema_migration`
- `state_migration_run`
- `state_migration_validation`
- `state_cutover_gate`

用途：记录 schema version、迁移批次、校验结果、cutover gate 状态。

## 8. 防死锁与超时策略

### 8.1 短事务规则

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

### 8.2 固定锁顺序

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

### 8.3 PostgreSQL 超时

Writer transaction 必须设置：

- `lock_timeout`
- `statement_timeout`
- `idle_in_transaction_session_timeout`

Read transaction 设置较短 `statement_timeout`，避免慢查询拖垮连接池。

### 8.4 可重试错误

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

### 8.5 不可重试错误

以下错误不重试：

- payload schema validation failed；
- handler 不存在；
- ownership / permission denied；
- idempotency key 冲突但 payload fingerprint 不一致；
- schema contract mismatch；
- safety / Rust contract enforce fail-closed；
- 业务状态机非法迁移。

## 9. Worker 调度与恢复

### 9.1 抢占 SQL

Worker 使用 PostgreSQL row lock 抢 command：

```sql
SELECT command_id
FROM state_write_command
WHERE status IN ('pending', 'retrying')
  AND available_at <= now()
  AND lease_expires_at IS NULL
ORDER BY priority DESC, created_at ASC
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

抢到后在同一 transaction 标记 `leased`，设置 `lease_owner` 和 `lease_expires_at`。

### 9.2 Partition 保序

Worker 执行前检查：

- 该 command 是其 partition 最小未完成 sequence；或
- 前序 command 均已 terminal。

如果前序 command 未完成，当前 command 不执行，释放或延后。

### 9.3 Lease 恢复

Worker crash 后：

- `lease_expires_at < now()` 的 command 可被其他 worker reclaim。
- reclaim 写入 attempt metadata。
- 如果 handler 已提交业务表但未更新 command，idempotency handler 必须能识别已提交结果并补写 command result。

### 9.4 Backpressure

当 queue backlog 或 oldest pending age 超阈值：

- readiness degraded；
- 可拒绝低优先级新写入；
- 保留关键系统写入；
- 输出 audit-safe 指标，不泄露 payload。

## 10. 配置边界

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

## 11. 迁移策略（未来阶段，不在今日实施）

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

## 12. 测试与验收

### 12.1 单元测试

覆盖：

- command payload schema validation；
- idempotency key 重复提交；
- partition sequence 分配；
- command status transition；
- retry/backoff 计算；
- dead-letter 规则；
- 不可重试错误 fail closed；
- handler 锁顺序声明完整性。

### 12.2 PostgreSQL 集成测试

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

### 12.3 业务回归测试

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

### 12.4 压测与故障注入

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

### 12.5 验收门槛

- Reader 不等待 pending queue。
- Deadlock / lock timeout 均转为 bounded retry 或 dead-letter。
- Command 无重复执行。
- 同一 partition 无乱序。
- Worker crash 后 leased command 可恢复。
- Queue 指标进入 health/readiness。
- Production 缺 PostgreSQL 配置 fail closed。
- SQLite 不作为 production fallback。

## 13. 开放事项

以下事项不阻塞本设计落文档，但进入实施计划前需要拆解：

1. PostgreSQL Python driver 选择：需结合现有依赖快照和异步运行模型评估。
2. StateService API 细化：哪些上层调用需要 `submit_command`，哪些需要 `execute_command_and_wait`。
3. Command handler 拆分顺序：建议从 event append / task transition / message append 等高频状态写入开始。
4. 迁移 PRD：未来单独设计 SQLite -> PostgreSQL 数据迁移、shadow、cutover、rollback。
5. RuntimeSidecar 关系：短期作为独立可靠性通道保留，长期可评估是否承载部分 StateCommandHandlers。

## 14. 今日停止条件

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
