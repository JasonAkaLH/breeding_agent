# API与核心数据模型

> 来源：从 `docs/prd/backend/00-主代理框架PRD.md` 拆分而来，承载 API 设计与核心数据模型定义。

## 11. API 设计
### 11.1 会话消息入口
#### `POST /api/v1/conversations/{conversation_id}/messages`
用于提交用户消息并触发新的异步任务。

请求体建议：
```json
{
  "account_id": "acc_123",
  "content": "帮我统计最近30天按地区分组的订单数",
  "routing_mode": "auto",
  "capability_id": null,
  "client_message_id": "client_msg_001",
  "metadata": {}
}
```

返回体建议：
```json
{
  "conversation_id": "conv_123",
  "message_id": "msg_123",
  "task_id": "task_123",
  "status": "accepted"
}
```

串行约束：若同一会话已有活跃任务，返回冲突错误（如 `409 Conflict`）。

### 11.2 查询任务
#### `GET /api/v1/tasks/{task_id}`
返回任务当前状态、根节点、执行摘要、取消状态、时间戳等。

建议响应字段：
- `task_id`
- `conversation_id`
- `status`
- `root_node_id`
- `active_node_count`
- `completed_node_count`
- `failed_node_count`
- `cancel_requested`
- `created_at`
- `updated_at`

### 11.3 任务事件流
#### `GET /api/v1/tasks/{task_id}/events`
用于前端订阅该任务的实时事件流，建议采用 SSE。

建议事件类型：
- `task.accepted`
- `task.graph_created`
- `task.status_changed`
- `node.started`
- `node.completed`
- `node.failed`
- `node.cancelled`
- `agent.started`
- `agent.finished`
- `message.delta`
- `task.completed`
- `task.cancelled`
- `task.cancellation_partial`

### 11.4 取消任务
#### `POST /api/v1/tasks/{task_id}/cancel`
用于用户主动停止当前任务。

建议返回：
```json
{
  "task_id": "task_123",
  "status": "cancelling",
  "accepted": true
}
```

### 11.5 查询任务图
#### `GET /api/v1/tasks/{task_id}/graph`
用于查看任务 DAG、节点状态、依赖关系、关键性标记。

### 11.6 查询任务产物
#### `GET /api/v1/tasks/{task_id}/artifacts`
用于查看已完成节点的关键产物摘要和可下载/可引用信息。

### 11.7 查询能力目录
#### `GET /api/v1/capabilities`
用于前端或后台查看系统当前支持的 capability 列表、版本与状态。

## 12. 核心数据模型
### 12.1 Conversation
| 字段 | 说明 |
|---|---|
| `conversation_id` | 会话标识 |
| `account_id` | 用户账户标识 |
| `status` | `active / archived / locked` |
| `current_task_id` | 当前活跃任务 |
| `title` | 会话标题或摘要 |
| `created_at` / `updated_at` | 时间戳 |

### 12.2 Message
| 字段 | 说明 |
|---|---|
| `message_id` | 消息标识 |
| `conversation_id` | 所属会话 |
| `role` | `user / assistant / system` |
| `content` | 消息正文 |
| `task_id` | 关联任务 |
| `stream_status` | 流式输出状态 |
| `created_at` | 时间戳 |

### 12.3 Task
| 字段 | 说明 |
|---|---|
| `task_id` | 任务标识 |
| `conversation_id` | 所属会话 |
| `root_message_id` | 触发消息 |
| `status` | `accepted / planning / running / cancelling / cancelled / completed / failed` |
| `routing_mode` | `auto / hint / force_capability` |
| `requested_capability_id` | 用户强制或提示的能力 |
| `root_node_id` | 根节点 |
| `summary` | 任务摘要 |
| `cancel_requested_at` | 取消请求时间 |
| `created_at` / `updated_at` | 时间戳 |

### 12.4 TaskNode
| 字段 | 说明 |
|---|---|
| `node_id` | 节点标识 |
| `task_id` | 所属任务 |
| `capability_id` | 节点目标能力 |
| `assigned_instance_id` | 被调度实例 |
| `status` | `pending / ready / running / completed / failed / cancelled / blocked_by_cancellation / orphaned` |
| `criticality` | `required / optional / fallback` |
| `dependency_type` | `hard / soft` |
| `retry_policy` | 重试策略 |
| `timeout_policy` | 超时策略 |
| `resource_class` | 资源类型 |
| `input_refs` | 输入引用 |
| `output_refs` | 输出引用 |
| `started_at` / `finished_at` | 时间戳 |

### 12.5 TaskEdge
| 字段 | 说明 |
|---|---|
| `from_node_id` | 上游节点 |
| `to_node_id` | 下游节点 |
| `edge_type` | `data / control / fallback` |
| `condition` | 可选触发条件 |

### 12.6 Artifact
| 字段 | 说明 |
|---|---|
| `artifact_id` | 产物标识 |
| `task_id` | 所属任务 |
| `producer_node_id` | 生产节点 |
| `artifact_type` | `text / json / file / dataset / summary` |
| `storage_ref` | 存储引用 |
| `summary` | 摘要 |
| `is_complete` | 是否完整 |
| `created_at` | 时间戳 |

### 12.7 CapabilityDefinition
| 字段 | 说明 |
|---|---|
| `capability_id` | 能力标识 |
| `name` | 能力名称 |
| `description` | 说明 |
| `input_contract` | 输入契约 |
| `output_contract` | 输出契约 |
| `tool_profile_id` | 允许的工具范围 |
| `policy_template_id` | 节点策略模板 |
| `version` | 版本 |
| `status` | `active / deprecated / disabled` |

### 12.8 AgentInstance
| 字段 | 说明 |
|---|---|
| `instance_id` | 实例标识 |
| `agent_type` | 实例类型 |
| `supported_capabilities` | 支持的能力列表 |
| `endpoint` | 本地或远程执行地址 |
| `state` | `online / busy / draining / offline` |
| `load_score` | 当前负载分值 |
| `resource_snapshot` | 资源快照 |
| `last_heartbeat_at` | 最近心跳时间 |

### 12.9 EventRecord
| 字段 | 说明 |
|---|---|
| `event_id` | 事件标识 |
| `conversation_id` | 会话标识 |
| `task_id` | 任务标识 |
| `node_id` | 节点标识（可选） |
| `agent_id` | Agent 标识（可选） |
| `event_type` | 事件类型 |
| `payload` | 结构化内容 |
| `visibility` | `frontend / internal / audit_only` |
| `created_at` | 时间戳 |

### 12.10 MailboxMessage
建议将 mailbox 逻辑拆成“消息主表 + 投递状态表”，避免把消息语义和单个接收方状态混在一起。

| 字段 | 说明 |
|---|---|
| `message_id` | mailbox 消息主键 |
| `conversation_id` | 所属会话 |
| `task_id` | 所属任务 |
| `node_id` | 所属节点（可选） |
| `parent_message_id` | 父消息，用于 request/response 串联 |
| `correlation_id` | 协作链路关联 ID |
| `from_agent` | 发送方 agent |
| `to_agent` | 接收方 agent（可选） |
| `to_role` | 接收方角色（可选） |
| `channel` | `orchestrator_control / peer_collaboration / interrupt_resume` |
| `message_type` | 结构化消息类型 |
| `ack_policy` | `strong / light` |
| `priority` | 优先级 |
| `payload` | 结构化 JSON payload |
| `payload_schema_version` | payload 版本号 |
| `created_at` | 创建时间 |
| `resolved_at` | 消息整体 resolved 时间 |

建议索引：
- `(task_id, created_at)`
- `(node_id, created_at)`
- `(channel, message_type, created_at)`
- `(correlation_id)`

### 12.11 MailboxDelivery
同一条 mailbox message 可能面向单个 agent，也可能面向某个 role 扇出到多个实例，因此建议单独建投递状态表。

| 字段 | 说明 |
|---|---|
| `delivery_id` | 投递记录主键 |
| `message_id` | 关联 `MailboxMessage` |
| `recipient_agent` | 实际接收实例 |
| `recipient_role` | 接收角色快照 |
| `status` | `pending / delivered / acknowledged / resolved / expired / cancelled` |
| `attempt_count` | 已投递尝试次数 |
| `max_attempts` | 最大尝试次数 |
| `ttl_seconds` | TTL 秒数 |
| `expires_at` | 过期时间 |
| `delivered_at` | 实际投递时间 |
| `acknowledged_at` | ACK 时间（强 ACK 时重点使用） |
| `resolved_at` | 该投递记录结束时间 |
| `next_retry_at` | 下次重试时间 |
| `last_error_code` | 最近一次投递错误码 |
| `last_error_message` | 最近一次投递错误说明 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

建议约束与索引：
- `UNIQUE(message_id, recipient_agent)`
- `(status, expires_at)` 便于扫描过期消息
- `(recipient_agent, status, priority, created_at)` 便于接收方拉取待处理消息
- `(next_retry_at)` 便于重试调度

### 12.12 Interrupt
Interrupt 不建议仅作为 mailbox payload 的一部分存在，应有独立对象，便于前端、会话和恢复流程引用。

| 字段 | 说明 |
|---|---|
| `interrupt_id` | interrupt 主键 |
| `conversation_id` | 所属会话 |
| `task_id` | 所属任务 |
| `node_id` | 触发节点 |
| `source_agent` | 发起 interrupt 的 agent |
| `source_message_id` | 对应的 `clarification_request` mailbox message |
| `question` | 给用户展示的问题 |
| `reason_code` | 中断原因码 |
| `required_fields` | 结构化缺失字段定义 |
| `status` | `open / answered / expired / cancelled` |
| `expires_at` | 用户回复截止时间 |
| `created_at` | 创建时间 |
| `answered_at` | 回复时间 |
| `cancelled_at` | 取消时间 |

建议索引：
- `(conversation_id, status, created_at)`
- `(task_id, node_id)`
- `(expires_at)`

### 12.13 InterruptAnswer
若希望保留用户补充历史和多次回答痕迹，建议单独保留 answer 记录表。

| 字段 | 说明 |
|---|---|
| `interrupt_answer_id` | 回答记录主键 |
| `interrupt_id` | 关联 interrupt |
| `answer_payload` | 用户补充的结构化内容 |
| `source_message_id` | 关联前端消息 ID（可选） |
| `accepted` | 是否被系统接受为有效回答 |
| `created_at` | 创建时间 |
| `accepted_at` | 被接受时间 |

### 12.14 Checkpoint
为支持 `resume_notice` 和节点恢复，checkpoint 也建议独立持久化。

| 字段 | 说明 |
|---|---|
| `checkpoint_id` | checkpoint 主键 |
| `task_id` | 所属任务 |
| `node_id` | 所属节点 |
| `agent_id` | 创建 checkpoint 的 agent |
| `snapshot_ref` | 快照引用（对象存储、文件路径或序列化键） |
| `snapshot_kind` | 快照类型 |
| `resume_token` | 恢复令牌 |
| `source_message_id` | 关联 mailbox message（可选） |
| `created_at` | 创建时间 |
| `invalidated_at` | 失效时间 |
