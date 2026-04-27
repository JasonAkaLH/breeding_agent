# Phase 2：SQLite 状态存储表结构草案

- 状态：草案 / 可进入实现
- 适用阶段：Phase 2
- 主要依据：
  - `docs/prd/backend/04-状态存储与迁移策略.md`
  - `docs/prd/backend/05-API与核心数据模型.md`
  - `docs/prd/backend/03-协作协议与任务生命周期.md`
  - `docs/dev_processes/Phase-2-落地SQLite状态存储与仓储抽象.md`

---

## 1. 目的

本草案把已经冻结的规则翻译成可实现的 SQLite 表结构设计，重点解决三件事：

1. 一期状态对象到底拆成哪些表；
2. 哪些字段必须是独立列；
3. 哪些字段一期先落成 JSON / refs / summary 即可。

本草案服务于 **Phase 2 的 SQLite 实现**，不是最终 PostgreSQL 物理 DDL；但要求与 PostgreSQL 保持**逻辑同构**。

---

## 2. 设计总原则

### 2.1 一期 memory 不单独建库/建专用表
会话延续型记忆直接复用主框架状态对象：
- `conversation`
- `message`
- `task`
- `task_node`
- `interrupt`
- `artifact`

### 2.2 恢复判断字段优先独立列
凡是会影响以下判断的字段，都优先做成独立列：
- 当前会话是谁
- 当前任务是不是还活着
- 当前节点跑到哪一步
- 当前是否等待用户输入
- 当前结果是否完整

### 2.3 补充上下文字段可先 JSON 化
以下类型字段，一期可优先落为 JSON 文本或引用：
- refs
- policy
- required_fields
- answer_payload
- summary
- storage_ref

### 2.4 SQLite 只是一期本地状态库
- 一期先保证 SQLite 可稳定实现与回归
- 所有逻辑字段命名、状态语义、主外键关系，都要为后续 PostgreSQL 同构迁移服务

---

## 3. 一期建议实现范围

### 3.1 第一批核心表（必须）
1. `conversation`
2. `message`
3. `task`
4. `task_node`
5. `task_edge`
6. `artifact`
7. `event_record`
8. `mailbox_message`
9. `mailbox_delivery`
10. `interrupt`
11. `interrupt_answer`
12. `checkpoint`

### 3.2 第二批可简化表（建议有最小版）
1. `capability_definition`
2. `agent_instance`

---

## 4. 表结构草案

## 4.1 conversation

**用途**：会话定位、账户归属、当前活跃任务定位。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `conversation_id` | `TEXT PRIMARY KEY` | 是 | 会话主键 |
| `account_id` | `TEXT NOT NULL` | 是 | 账户主键/外部用户标识 |
| `status` | `TEXT NOT NULL` | 是 | `active / archived / locked` |
| `current_task_id` | `TEXT NULL` | 是 | 当前活跃任务 |
| `title` | `TEXT NULL` | 否 | 会话标题/摘要 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `updated_at` | `TEXT NOT NULL` | 是 | ISO8601 |

**索引建议**
- `INDEX idx_conversation_account_updated(account_id, updated_at)`
- `INDEX idx_conversation_current_task(current_task_id)`

---

## 4.2 message

**用途**：会话连续性恢复的主表。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `message_id` | `TEXT PRIMARY KEY` | 是 | 消息主键 |
| `conversation_id` | `TEXT NOT NULL` | 是 | 所属会话 |
| `role` | `TEXT NOT NULL` | 是 | `user / assistant / system` |
| `content` | `TEXT NOT NULL` | 是 | 消息正文 |
| `task_id` | `TEXT NULL` | 是 | 关联任务 |
| `stream_status` | `TEXT NULL` | 否 | 流式输出状态 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |

**索引建议**
- `INDEX idx_message_conversation_created(conversation_id, created_at)`
- `INDEX idx_message_task_created(task_id, created_at)`

---

## 4.3 task

**用途**：任务级恢复与取消语义的核心对象。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `task_id` | `TEXT PRIMARY KEY` | 是 | 任务主键 |
| `conversation_id` | `TEXT NOT NULL` | 是 | 所属会话 |
| `root_message_id` | `TEXT NOT NULL` | 是 | 触发消息 |
| `status` | `TEXT NOT NULL` | 是 | `accepted / planning / running / cancelling / cancelled / completed / failed` |
| `routing_mode` | `TEXT NOT NULL` | 是 | `auto / hint / force_capability` |
| `requested_capability_id` | `TEXT NULL` | 否 | 用户指定能力 |
| `root_node_id` | `TEXT NULL` | 是 | 根节点 |
| `summary` | `TEXT NULL` | 否 | 任务摘要（允许先做文本） |
| `cancel_requested_at` | `TEXT NULL` | 否 | ISO8601 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `updated_at` | `TEXT NOT NULL` | 是 | ISO8601 |

**索引建议**
- `INDEX idx_task_conversation_created(conversation_id, created_at)`
- `INDEX idx_task_status_updated(status, updated_at)`

---

## 4.4 task_node

**用途**：节点执行进度恢复、依赖恢复、取消传播判断。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `node_id` | `TEXT PRIMARY KEY` | 是 | 节点主键 |
| `task_id` | `TEXT NOT NULL` | 是 | 所属任务 |
| `capability_id` | `TEXT NOT NULL` | 是 | 节点目标 capability |
| `assigned_instance_id` | `TEXT NULL` | 否 | 当前执行实例 |
| `status` | `TEXT NOT NULL` | 是 | 节点状态 |
| `criticality` | `TEXT NOT NULL` | 是 | `required / optional / fallback` |
| `dependency_type` | `TEXT NOT NULL` | 是 | `hard / soft` |
| `retry_policy` | `TEXT NULL` | 否 | JSON 文本 |
| `timeout_policy` | `TEXT NULL` | 否 | JSON 文本 |
| `resource_class` | `TEXT NULL` | 否 | 资源类型 |
| `input_refs` | `TEXT NULL` | 否 | JSON 文本 / refs |
| `output_refs` | `TEXT NULL` | 否 | JSON 文本 / refs |
| `started_at` | `TEXT NULL` | 否 | ISO8601 |
| `finished_at` | `TEXT NULL` | 否 | ISO8601 |

**索引建议**
- `INDEX idx_task_node_task_status(task_id, status)`
- `INDEX idx_task_node_capability_status(capability_id, status)`
- `INDEX idx_task_node_started(started_at)`

---

## 4.5 task_edge

**用途**：DAG 依赖边存储。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `edge_id` | `TEXT PRIMARY KEY` | 是 | 建议显式主键，避免复合主键复杂度 |
| `task_id` | `TEXT NOT NULL` | 是 | 所属任务 |
| `from_node_id` | `TEXT NOT NULL` | 是 | 上游节点 |
| `to_node_id` | `TEXT NOT NULL` | 是 | 下游节点 |
| `edge_type` | `TEXT NOT NULL` | 是 | `data / control / fallback` |
| `condition` | `TEXT NULL` | 否 | 条件说明 |

**约束/索引建议**
- `UNIQUE(task_id, from_node_id, to_node_id)`
- `INDEX idx_task_edge_to_node(task_id, to_node_id)`

---

## 4.6 artifact

**用途**：当前会话继续执行所需的最小结果摘要与引用。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `artifact_id` | `TEXT PRIMARY KEY` | 是 | 产物主键 |
| `task_id` | `TEXT NOT NULL` | 是 | 所属任务 |
| `producer_node_id` | `TEXT NOT NULL` | 是 | 生产节点 |
| `artifact_type` | `TEXT NOT NULL` | 是 | `text / json / file / dataset / summary` |
| `storage_ref` | `TEXT NULL` | 否 | 引用/路径/对象键 |
| `summary` | `TEXT NULL` | 否 | 摘要 |
| `is_complete` | `INTEGER NOT NULL` | 是 | 0/1 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |

**索引建议**
- `INDEX idx_artifact_task_created(task_id, created_at)`
- `INDEX idx_artifact_node_created(producer_node_id, created_at)`

---

## 4.7 event_record

**用途**：前端观察与审计链路的统一事件表。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `event_id` | `TEXT PRIMARY KEY` | 是 | 事件主键 |
| `conversation_id` | `TEXT NOT NULL` | 是 | 所属会话 |
| `task_id` | `TEXT NOT NULL` | 是 | 所属任务 |
| `node_id` | `TEXT NULL` | 否 | 节点 |
| `agent_id` | `TEXT NULL` | 否 | agent |
| `event_type` | `TEXT NOT NULL` | 是 | 事件类型 |
| `payload` | `TEXT NULL` | 否 | JSON 文本 |
| `visibility` | `TEXT NOT NULL` | 是 | `frontend / internal / audit_only` |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |

**索引建议**
- `INDEX idx_event_task_created(task_id, created_at)`
- `INDEX idx_event_conversation_created(conversation_id, created_at)`
- `INDEX idx_event_type_created(event_type, created_at)`

---

## 4.8 mailbox_message

**用途**：协作语义主表。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `message_id` | `TEXT PRIMARY KEY` | 是 | mailbox 主键 |
| `conversation_id` | `TEXT NOT NULL` | 是 | 所属会话 |
| `task_id` | `TEXT NOT NULL` | 是 | 所属任务 |
| `node_id` | `TEXT NULL` | 否 | 所属节点 |
| `parent_message_id` | `TEXT NULL` | 否 | 父消息 |
| `correlation_id` | `TEXT NOT NULL` | 是 | 关联链路 ID |
| `from_agent` | `TEXT NOT NULL` | 是 | 发送方 |
| `to_agent` | `TEXT NULL` | 否 | 目标 agent |
| `to_role` | `TEXT NULL` | 否 | 目标角色 |
| `channel` | `TEXT NOT NULL` | 是 | channel |
| `message_type` | `TEXT NOT NULL` | 是 | 消息类型 |
| `ack_policy` | `TEXT NOT NULL` | 是 | `strong / light` |
| `priority` | `INTEGER NOT NULL` | 是 | 优先级 |
| `payload` | `TEXT NULL` | 否 | JSON 文本 |
| `payload_schema_version` | `INTEGER NOT NULL` | 是 | 版本 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `resolved_at` | `TEXT NULL` | 否 | ISO8601 |

**索引建议**
- `INDEX idx_mailbox_message_task_created(task_id, created_at)`
- `INDEX idx_mailbox_message_node_created(node_id, created_at)`
- `INDEX idx_mailbox_message_channel_type_created(channel, message_type, created_at)`
- `INDEX idx_mailbox_message_correlation(correlation_id)`

---

## 4.9 mailbox_delivery

**用途**：协作消息投递状态表。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `delivery_id` | `TEXT PRIMARY KEY` | 是 | 投递主键 |
| `message_id` | `TEXT NOT NULL` | 是 | 关联 mailbox_message |
| `recipient_agent` | `TEXT NOT NULL` | 是 | 实际接收实例 |
| `recipient_role` | `TEXT NULL` | 否 | 角色快照 |
| `status` | `TEXT NOT NULL` | 是 | 投递状态 |
| `attempt_count` | `INTEGER NOT NULL` | 是 | 已尝试次数 |
| `max_attempts` | `INTEGER NOT NULL` | 是 | 最大次数 |
| `ttl_seconds` | `INTEGER NOT NULL` | 是 | TTL |
| `expires_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `delivered_at` | `TEXT NULL` | 否 | ISO8601 |
| `acknowledged_at` | `TEXT NULL` | 否 | ISO8601 |
| `resolved_at` | `TEXT NULL` | 否 | ISO8601 |
| `next_retry_at` | `TEXT NULL` | 否 | ISO8601 |
| `last_error_code` | `TEXT NULL` | 否 | 错误码 |
| `last_error_message` | `TEXT NULL` | 否 | 错误说明 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `updated_at` | `TEXT NOT NULL` | 是 | ISO8601 |

**约束/索引建议**
- `UNIQUE(message_id, recipient_agent)`
- `INDEX idx_mailbox_delivery_status_expires(status, expires_at)`
- `INDEX idx_mailbox_delivery_recipient_status(recipient_agent, status, created_at)`
- `INDEX idx_mailbox_delivery_retry(next_retry_at)`

---

## 4.10 interrupt

**用途**：等待用户输入的恢复对象，不只作为 mailbox payload 存在。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `interrupt_id` | `TEXT PRIMARY KEY` | 是 | interrupt 主键 |
| `conversation_id` | `TEXT NOT NULL` | 是 | 所属会话 |
| `task_id` | `TEXT NOT NULL` | 是 | 所属任务 |
| `node_id` | `TEXT NOT NULL` | 是 | 触发节点 |
| `source_agent` | `TEXT NOT NULL` | 是 | 发起 agent |
| `source_message_id` | `TEXT NULL` | 否 | 对应 mailbox message |
| `question` | `TEXT NOT NULL` | 是 | 用户问题 |
| `reason_code` | `TEXT NOT NULL` | 是 | 原因码 |
| `required_fields` | `TEXT NULL` | 否 | JSON 文本 |
| `status` | `TEXT NOT NULL` | 是 | `open / answered / expired / cancelled` |
| `expires_at` | `TEXT NULL` | 否 | ISO8601 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `answered_at` | `TEXT NULL` | 否 | ISO8601 |
| `cancelled_at` | `TEXT NULL` | 否 | ISO8601 |

**索引建议**
- `INDEX idx_interrupt_conversation_status(conversation_id, status, created_at)`
- `INDEX idx_interrupt_task_node(task_id, node_id)`
- `INDEX idx_interrupt_expires(expires_at)`

---

## 4.11 interrupt_answer

**用途**：保留补充答案历史与接受状态。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `interrupt_answer_id` | `TEXT PRIMARY KEY` | 是 | 回答主键 |
| `interrupt_id` | `TEXT NOT NULL` | 是 | 关联 interrupt |
| `answer_payload` | `TEXT NOT NULL` | 是 | JSON 文本 |
| `source_message_id` | `TEXT NULL` | 否 | 关联前端消息 |
| `accepted` | `INTEGER NOT NULL` | 是 | 0/1 |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `accepted_at` | `TEXT NULL` | 否 | ISO8601 |

**索引建议**
- `INDEX idx_interrupt_answer_interrupt_created(interrupt_id, created_at)`

---

## 4.12 checkpoint

**用途**：resume 时恢复节点执行上下文。

| 列名 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `checkpoint_id` | `TEXT PRIMARY KEY` | 是 | checkpoint 主键 |
| `task_id` | `TEXT NOT NULL` | 是 | 所属任务 |
| `node_id` | `TEXT NOT NULL` | 是 | 所属节点 |
| `agent_id` | `TEXT NOT NULL` | 是 | 生成者 |
| `snapshot_ref` | `TEXT NOT NULL` | 是 | 快照引用 |
| `snapshot_kind` | `TEXT NOT NULL` | 是 | 快照类型 |
| `resume_token` | `TEXT NULL` | 否 | 恢复令牌 |
| `source_message_id` | `TEXT NULL` | 否 | 关联 mailbox message |
| `created_at` | `TEXT NOT NULL` | 是 | ISO8601 |
| `invalidated_at` | `TEXT NULL` | 否 | ISO8601 |

**索引建议**
- `INDEX idx_checkpoint_task_node(task_id, node_id, created_at)`
- `INDEX idx_checkpoint_resume_token(resume_token)`

---

## 4.13 capability_definition（最小版）

**用途**：最小能力目录持久化。

建议列：
- `capability_id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `description TEXT NOT NULL`
- `input_contract TEXT NULL`（JSON）
- `output_contract TEXT NULL`（JSON）
- `tool_profile_id TEXT NULL`
- `policy_template_id TEXT NULL`
- `version TEXT NOT NULL`
- `status TEXT NOT NULL`

---

## 4.14 agent_instance（最小版）

**用途**：实例注册与心跳持久化。

建议列：
- `instance_id TEXT PRIMARY KEY`
- `agent_type TEXT NOT NULL`
- `supported_capabilities TEXT NOT NULL`（JSON）
- `endpoint TEXT NOT NULL`
- `state TEXT NOT NULL`
- `load_score INTEGER NOT NULL`
- `resource_snapshot TEXT NULL`（JSON）
- `last_heartbeat_at TEXT NOT NULL`

---

## 5. 一期独立列 / JSON / refs / summary 映射速查

### 必须独立列
- 会话归属与状态：`conversation_id`, `account_id`, `status`, `current_task_id`
- 消息连续性：`message_id`, `conversation_id`, `role`, `content`, `task_id`, `created_at`
- 任务恢复：`task_id`, `conversation_id`, `root_message_id`, `status`, `root_node_id`, `cancel_requested_at`
- 节点恢复：`node_id`, `task_id`, `capability_id`, `status`, `started_at`, `finished_at`
- interrupt 恢复：`interrupt_id`, `task_id`, `node_id`, `question`, `reason_code`, `status`
- artifact 元数据：`artifact_id`, `task_id`, `producer_node_id`, `artifact_type`, `is_complete`

### 可先 JSON / refs / summary
- `Task.summary`
- `TaskNode.retry_policy`
- `TaskNode.timeout_policy`
- `TaskNode.input_refs`
- `TaskNode.output_refs`
- `Interrupt.required_fields`
- `Interrupt.answer_payload`
- `Artifact.storage_ref`
- `Artifact.summary`
- `MailboxMessage.payload`
- `EventRecord.payload`

---

## 6. 从 SQLite 迁到 PostgreSQL 时，这份草案里最可能动的地方

1. 所有 `TEXT(JSON)` 字段改为 `JSONB` 对应 ORM 类型
2. 所有时间字符串列改为 `TIMESTAMPTZ` 对应 ORM 类型
3. `mailbox_delivery`、`interrupt`、`task_node`、`event_record` 的索引策略要增强
4. 若 SQLite 下使用了简化 upsert / 查询写法，PostgreSQL implementation 需要单独适配
5. 表名、列名、主外键语义不要改，优先做物理类型增强而不是逻辑重构

---

## 7. 实现阶段的最小检查表

- [ ] 所有核心表已定义主键
- [ ] 所有恢复判断必需字段已落独立列
- [ ] JSON / refs / summary 字段已和独立列边界分清
- [ ] mailbox 主表与 delivery 表职责分清
- [ ] interrupt / interrupt_answer / checkpoint 已独立建表
- [ ] SQLite 下可完成会话连续性最小回归
- [ ] PostgreSQL 迁移时需要改的代码/配置/测试点已预留说明
