# DateTimeText 跨后端时间合同修复设计

## 状态

- 日期：2026-08-31
- 分支与环境：`main` 开发环境；不适用于 `prod`
- 状态：`implemented_verified`；document-perfectization 只读硬伤审查 `100/100 Pass`；
  仓库实现、相关全量回归与隔离 PostgreSQL 零跳过回归已通过
- 目标：修复 SQLAlchemy `DateTimeText` 在 SQLite 与 PostgreSQL 返回不同
  awareness 的问题，同时保留 rollout、CP7 和密钥证据现有的 aware-UTC 合同

## 背景与已确认根因

开发环境一次 conversation submission 已在 PostgreSQL 中原子写入 Conversation、Task 和
Message，但未写入 accepted Event、AgentRun 或 TaskNode。运行日志只显示
`submission_admission_unavailable` 503。

排查确认：

- API Runtime 创建的 Task 时间为 UTC-naive；
- `DateTimeText` 在 SQLite 使用 `TEXT`，读取原 naive 字符串后返回 naive；
- 同一类型在 PostgreSQL 使用 `DateTime(timezone=True)`，读取 `TIMESTAMPTZ` 后返回 aware UTC；
- `ApiRuntime.materialize_route_decision()` 对已持久化 Task 与待 materialize Task 执行严格
  `created_at` 比较；
- 两者表示同一瞬间，但 Python 严格相等为 `False`，触发
  `submission_task_materialization_conflict`，随后被上层映射为 503；
- PostgreSQL 表结构、权限、RLS、触发器和 EventRecord 写入路径均已单独验证正常。

当前失败任务已放弃。本设计不恢复、删除或修改该任务。

## 目标

1. 让普通 SQLAlchemy 持久化时间在 SQLite 和 PostgreSQL 中统一返回 UTC-naive。
2. PostgreSQL 写入 UTC-naive 时显式按 UTC 解释，不受 session `TimeZone` 影响。
3. 保留已有安全证据字段的 aware-UTC 语义。
4. 用一个真实 PostgreSQL submission 回归测试覆盖原失败比较与后续 Event 写入。
5. 不修改数据库 schema 或现有业务协议。

## 非目标

- 不修改 PostgreSQL 表、列、数据、session 时区或部署配置。
- 不修改 API DTO、SSE 或 Frontend，不新增公开时间格式化逻辑；现有序列化器接收普通字段的
  UTC-naive 结果，是存储合同修复的直接表现。
- 不批量替换 `isoformat()`，不修改 submission、rollout、CP7 或 sidecar 的序列化代码与
  版本协议。
- 不增加历史数据扫描器、迁移脚本、启动预检或自动修复。
- 不重构通用 datetime 比较、时钟或现有兼容 helper。
- 不为每个时间字段分别增加行为测试。
- 不处理 `prod` 或当前已放弃任务。

## 方案选择

评估过三种方案：

1. 只在 submission 比较点归一化。改动最小，但其他 Task、Message、Agent、lease 和 outbox
   路径仍继续承受跨后端类型差异。
2. 单一 `DateTimeText` 接受 naive/aware 并静默归一化。落库结果可以统一，但 SQLAlchemy
   对象在 flush 前仍保留调用方传入的 awareness，Python 内存比较仍不稳定。
3. 两个严格类型：普通状态只接受 UTC-naive，安全证据只接受 aware，并在数据库边界规范化。

采用方案 3。它直接约束写入对象与读取对象，且不需要修改物理 schema。

## 类型合同

### `DateTimeText`

保留现有类名和物理类型映射：

- PostgreSQL：`TIMESTAMPTZ`
- SQLite：`TEXT`

绑定规则：

- `None` 原样通过；
- aware datetime 拒绝写入；
- naive datetime 在 PostgreSQL bind 时临时附加 `timezone.utc`；
- naive datetime 在 SQLite 中继续保存为无偏移 ISO 文本。

读取规则：

- PostgreSQL aware datetime 先转换为 UTC，再移除 `tzinfo`；
- SQLite naive ISO 文本解析后保持 naive；
- SQLite 旧的 aware ISO 文本先转换为 UTC，再移除 `tzinfo`。

最后一条是必要历史兼容：旧 `DateTimeText` 会把调用方传入的 aware datetime 原样写成带
偏移的 SQLite 文本。本设计不增加扫描或数据改写。

### `AwareUTCDateTimeText`

在 `src/storage/sqlalchemy_base.py` 新增内部类型，物理映射仍为 PostgreSQL
`TIMESTAMPTZ` / SQLite `TEXT`。

绑定规则：

- `None` 原样通过；
- naive datetime 拒绝写入；
- aware datetime 转换为 UTC；
- PostgreSQL 接收 aware UTC datetime，SQLite 保存带 UTC 偏移的 ISO 文本。

读取规则：

- datetime 或 ISO 文本必须带有效时区；
- 返回值统一为 aware UTC；
- naive 历史值直接失败，不增加修复或兜底路径。

新类型只由共享 SQLAlchemy model 声明使用，不增加 legacy SQLite re-export。

## 字段边界

AST 盘点覆盖 `src/storage/sqlalchemy_models.py` 的全部 61 个 Row、169 个时间字段：

- 46 个 Row、138 个字段继续使用 `DateTimeText`；
- 15 个 Row、31 个字段改用 `AwareUTCDateTimeText`。

### aware-UTC：15 个 Row / 31 个字段

| Row | 字段 |
|---|---|
| `MCPLegacyMigrationRecordRow` | `occurred_at`, `evidence_expires_at` |
| `MCPRolloutGateScopeRow` | `created_at` |
| `MCPRolloutDrillObservationRow` | `observed_at`, `recorded_at`, `expires_at` |
| `MCPRolloutMetricBucketRow` | `bucket_started_at`, `bucket_ended_at`, `created_at`, `updated_at` |
| `MCPRolloutEvidenceSnapshotRow` | `window_started_at`, `window_ended_at`, `recorded_at` |
| `MCPShadowAuditSampleRow` | `observed_at`, `recorded_at`, `expires_at` |
| `MCPRolloutStageApprovalRow` | `created_at` |
| `MCPRolloutDeploymentActivationRow` | `created_at` |
| `MCPRolloutPromotionBlockRow` | `created_at` |
| `MCPRolloutBlockResolutionRow` | `created_at` |
| `MCPRolloutInstanceConfigRow` | `lease_expires_at`, `created_at`, `updated_at` |
| `MAFMasterKeyValidationRow` | `created_at` |
| `MCPCP7SafetyLedgerRow` | `bucket_started_at`, `bucket_ended_at`, `recorded_at` |
| `MCPCP7ReadyEpochEventRow` | `boundary_at` |
| `MCPCP7CandidateGuardRow` | `first_invalid_at`, `created_at`, `updated_at` |

这些字段的现有 writer 已产生 aware 时间，并参与 evidence window、canonical digest、主密钥
校验或 CP7 连续性检查。将其改为普通 UTC-naive 会直接破坏现有域合同。

### UTC-naive：46 个 Row / 138 个字段

| Row | 字段 |
|---|---|
| `UserMCPServerRow` | `last_tested_at`, `credential_updated_at`, `deleted_at`, `created_at`, `updated_at` |
| `UserMCPToolGrantRow` | `granted_at`, `invalidated_at` |
| `MCPBranchRecordRow` | `created_at`, `updated_at`, `terminal_at` |
| `MCPCallRecordRow` | `created_at`, `updated_at`, `terminal_at` |
| `MCPRemoteTaskBindingRow` | `next_poll_at`, `published_at`, `created_at`, `updated_at`, `terminal_at`, `lease_expires_at` |
| `MCPRemoteTaskOutboxRow` | `lease_expires_at`, `created_at`, `updated_at`, `continuation_admitted_at`, `continuation_dispatched_at`, `continuation_lease_expires_at`, `completed_at` |
| `MCPSealedStateRow` | `created_at`, `updated_at` |
| `MCPConnectionLeaseRow` | `lease_expires_at`, `disconnected_at`, `created_at`, `updated_at` |
| `MCPAuditEventRow` | `occurred_at`, `expires_at` |
| `UserMCPHealthAttemptRow` | `lease_expires_at`, `created_at`, `updated_at` |
| `UserMCPScopeLeaseRow` | `lease_expires_at`, `created_at`, `updated_at` |
| `ConversationRow` | `created_at`, `updated_at`, `delete_requested_at`, `delete_started_at`, `delete_finished_at`, `delete_failed_at` |
| `SubmissionPreparationReceiptRow` | `created_at`, `updated_at` |
| `ConversationFileResourceRow` | `created_at`, `updated_at` |
| `ConversationFileIndexRepairMarkerRow` | `next_retry_at`, `created_at`, `updated_at`, `resolved_at` |
| `ConversationMemorySummaryRow` | `covered_until_created_at`, `created_at`, `updated_at` |
| `PendingSkillContextRow` | `created_at`, `updated_at` |
| `AuthUserTokenRow` | `token_issued_at`, `token_last_used_at`, `auth_generation_updated_at`, `created_at`, `updated_at` |
| `MessageRow` | `created_at`, `updated_at` |
| `UserMCPOwnerMutationGuardRow` | `created_at`, `updated_at` |
| `MCPNoServerIntentRow` | `created_at`, `updated_at`, `terminal_at` |
| `MCPDispatchResumeOutboxRow` | `lease_expires_at`, `created_at`, `updated_at`, `completed_at` |
| `MCPPendingToolActionRow` | `created_at`, `updated_at`, `approved_at`, `consumed_at`, `invalidated_at` |
| `MCPTerminalCandidateLifecycleRow` | `consumed_at`, `eligible_at`, `created_at`, `updated_at` |
| `MCPDurableResultLifecycleRow` | `eligible_at`, `deleted_at`, `created_at`, `updated_at` |
| `MCPDispatchAggregateMigrationRow` | `created_at`, `updated_at` |
| `MCPNoServerConvergenceReceiptRow` | `committed_at` |
| `MCPLegacyRetirementEvidenceRow` | `created_at` |
| `MCPLegacyRetirementReceiptRow` | `committed_at` |
| `MCPTerminalResultReceiptRow` | `committed_at` |
| `MCPExecutionTerminalProjectionRow` | `unknown_terminal_at`, `result_committed_at`, `resolved_at`, `created_at`, `updated_at` |
| `TaskRow` | `cancel_requested_at`, `created_at`, `updated_at` |
| `AgentRunRow` | `lease_expires_at`, `created_at`, `updated_at`, `terminal_at` |
| `AgentItemRow` | `created_at`, `committed_at` |
| `AgentFinalReceiptRow` | `created_at` |
| `TaskNodeRow` | `started_at`, `finished_at` |
| `ArtifactRow` | `created_at` |
| `TaskInputAttachmentRow` | `created_at`, `updated_at` |
| `EventRecordRow` | `created_at` |
| `MailboxMessageRow` | `created_at`, `resolved_at` |
| `MailboxDeliveryRow` | `expires_at`, `delivered_at`, `acknowledged_at`, `resolved_at`, `next_retry_at`, `created_at`, `updated_at` |
| `InterruptRow` | `expires_at`, `created_at`, `answered_at`, `cancelled_at` |
| `InterruptAnswerRow` | `created_at`, `accepted_at` |
| `SlotCollectionRow` | `created_at`, `updated_at`, `completed_at`, `cancelled_at`, `failed_at` |
| `SlotEventRow` | `created_at` |
| `CheckpointRow` | `created_at`, `invalidated_at` |

`MCPTerminalResultReceiptRow` 和 `MCPExecutionTerminalProjectionRow` 保存的是 Task/SQL 生命周期
时间，继续使用 UTC-naive。terminal candidate 的严格 `sealed_at` 属于独立文件/域证据，不是
这里的 `DateTimeText` 列。

## 直接代码修改

批准时确认的三个生产修改点：

1. `src/storage/sqlalchemy_base.py`
   - 收紧 `DateTimeText`；
   - 新增 `AwareUTCDateTimeText`。
2. `src/storage/sqlalchemy_models.py`
   - 只替换上表 31 个字段的类型声明。
3. `src/storage/postgres/repositories.py`
   - 将 owner mutation guard 已确认的 aware `updated_at` 写入改为项目现有的
     UTC-naive 时钟形式。

实施回归又直接复现出三个同合同 writer 边界，并做了最小修复：

4. `src/storage/sqlite/repositories.py`
   - 将 message identity sidecar 的 UTC 毫秒结果解码为普通 Message 所需的 UTC-naive。
5. `src/storage/sqlite/agent_repository.py`
   - 将 PostgreSQL `CURRENT_TIMESTAMP` 存储时钟转为 UTC-naive 后再写普通 Agent/lease 字段。
6. `src/integrations/mcp/legacy_migration_apply.py`
   - 普通 server 时钟继续 naive，仅在生成 legacy migration 证据时间时附加 UTC。

已盘点的 Python 时间比较、排序、算术和序列化主要分布在 API Runtime、submission
admission、conversation memory、Agent orchestrator、task state machine、SQLite repository
和 Agent repository。除上述由真实回归复现的三个 writer 边界外，其余 consumer 无需修改；
普通字段在写入前与读取后统一为 naive，严格字段始终为 aware UTC。

## 数据库与兼容性

- 两个类型在 PostgreSQL 中都映射到现有 `TIMESTAMPTZ`，无 schema diff。
- 两个类型在 SQLite 中都继续使用现有 `TEXT`，无 schema migration。
- PostgreSQL 已有数据表示的瞬间不变；普通字段读取时只改变 Python awareness。
- 普通 SQLite 历史 aware 文本由 `DateTimeText` 必要兼容读取。
- 严格类型遇到 naive 历史值直接失败，不提供扫描或修复工具。
- `AuthUserTokenRow.auth_generation_updated_at` 的 `CURRENT_TIMESTAMP` 默认值继续有效；ORM
  读取时经过 `DateTimeText` result processor 返回 naive。

## 错误处理

- awareness 合同不匹配时直接由 SQLAlchemy 写入或读取失败。
- 不新增业务错误码、日志指标、异常映射或降级路径。
- 不修改现有 `submission_admission_unavailable` 上层映射。

## 测试

### 新增聚焦测试

1. 时间类型单元测试：
   - `DateTimeText` naive bind、PostgreSQL aware result、SQLite naive/旧 aware result、aware
     bind 拒绝；
   - `AwareUTCDateTimeText` aware bind/result UTC 规范化、naive bind/result 拒绝。
2. 声明归类测试：
   - 精确断言 31 个字段使用 aware 类型；
   - 精确断言其余 138 个字段使用普通类型。
3. PostgreSQL submission 回归：
   - 用 naive Task 时间完成 admission；
   - round-trip 后 Task 时间仍为 naive 且与 materialized Task 严格相等；
   - 调用真实 `ApiRuntime.materialize_route_decision()`；
   - 成功写入 `task.accepted` 和 route Event，不再触发
     `submission_task_materialization_conflict`。

### 现有回归

运行 shared SQLAlchemy declaration、submission admission SQLite/PostgreSQL、submission
preparation callback、rollout ledger、shadow evidence、CP7 safety ledger、master-key/user MCP
repository，以及相关 Storage/API 测试集。因严格输入合同实际失败的旧测试 fixture 才改为
对应 naive 或 aware 时间，不提前批量重写。

最终结果：类型/声明聚焦 9 项、Storage 560 项、Lifecycle 48 项、Orchestration 195 项、
Integrations 764 项、API 615 项、PostgreSQL schema 24 项全部通过；四个模块在四个隔离
PostgreSQL 17 测试库中 19/19 通过且零 skip，其中 submission materialize 成功写入两类 Event。

## 验收标准

1. 169 个字段全部且仅落入上述一个合同。
2. 原 PostgreSQL submission 比较路径通过真实 materialize 与 Event 写入。
3. PostgreSQL runtime schema manifest 无变化。
4. rollout、CP7、master-key 和 legacy migration round-trip 后继续得到 aware UTC。
5. 普通 Task、Message、Agent 和 lease round-trip 后得到 UTC-naive。
6. 最终业务 diff 不包含 API、SSE、Frontend、schema、migration 或无关重构。

## 回滚

本设计不修改数据库结构或数据。若代码回归，回滚上述六个生产边界修改即可，不需要数据库
回滚。
