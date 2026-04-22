# 主代理框架 PRD（草案 v1）

- **项目**：multi_agent_framework
- **范围**：后端主代理框架
- **文档状态**：草案第二版
- **日期**：2026-04-22
- **本版目标**：在 v0 基础上补充 MVP 验收闭环、核心 API 草案、核心数据模型草案与逻辑组件边界

## 1. 项目背景
本项目面向内部付费用户，目标是构建一个办公助手后端。当前阶段不优先做具体功能 Agent，而是先完成主代理框架设计，确保后续文档 RAG、NL2SQL、数据分析、农业生物信息分析等能力可以稳定接入。

本框架不依赖 LangChain、LangGraph、AutoGen 等现成 Agent 框架，采用 Python 为主、异步优先的服务端架构；性能热点未来可下沉到 C++，但不作为一期前提。

## 2. 一期目标与非目标
### 2.1 一期目标
一期需交付一个可支撑业务扩展的主代理内核，至少覆盖：
- 任务拆分
- Agent 注册与发现
- 资源调度与执行
- 上下文传递
- 会话状态
- 任务队列
- 观测日志
- 记忆系统
- 实时事件流
- 用户主动中断任务

### 2.2 一期不做但必须预留接口
- 人工审批
- 权限控制
- 通用工具调用平台
- 多实例生产化部署能力
- 完整长期记忆系统

## 3. 产品形态与部署策略
- 产品形态为**前后端分离**，本 PRD 仅覆盖后端。
- 对外形态为 **HTTP / API 服务**。
- 服务模型为**异步任务驱动的对话式 Agent 服务**。
- 用户提交消息后，后端立即返回 `task_id`，后台异步执行。
- 支持**实时流式事件回传**。
- 架构按**分布式可扩展**设计，但**首期支持单服务器部署**。
- 当前 LLM 与生信分析能力均通过外部 API 提供，本机主要负责异步编排、状态管理、记忆、事件流与审计。

## 4. 核心术语
- **Capability**：主代理进行任务拆分、能力匹配与资源调度时使用的稳定能力契约。
- **Agent**：一个或多个 capability 的具体执行实体。
- **Tool**：Agent 执行过程中调用的底层操作接口。
- **Instance**：Agent 在运行时的具体实例，可本地也可远程。
- **Task**：一次用户消息触发的异步执行单元。
- **Node**：Task DAG 中的执行节点。
- **Artifact**：节点产出的中间结果或最终结果引用。
- **Event**：对前端和日志系统输出的状态变化或执行事件。

主代理优先面向 capability 编排，而不是直接面向 tool。

## 5. 逻辑架构草案
一期建议至少包含以下逻辑组件：
- **API 接入层**：接收消息、查询任务、取消任务、输出事件流。
- **会话服务**：维护 `conversation_id`、消息顺序、串行约束。
- **主代理编排器**：负责任务拆分、DAG 生成、节点策略装配。
- **Capability Registry**：维护稳定能力目录。
- **Instance Registry**：维护运行态实例、心跳、负载与健康状态。
- **调度器**：在资源约束下选择节点执行实例。
- **执行适配层**：统一封装本地执行器与外部 RESTful API。
- **状态存储**：持久化会话、任务、节点、事件、产物引用。
- **记忆服务**：维护用户账户与会话连续性记忆。
- **审计日志服务**：输出 JSONL 结构化日志。
- **事件流服务**：将任务状态和节点事件推送给前端。

## 6. 编排模型
### 6.1 任务结构
- 一期采用**混合型 DAG**：先生成主干 DAG，执行中允许受控动态扩展。
- 支持中间结果被多个下游节点复用。
- 设计参考成熟工作流 / DAG 编排逻辑，不自造完全脱离实践的模型。

### 6.2 任务拆分指导方针
- 主代理在**预定义任务拆分指导方针**下工作。
- 拆分依据以**结构化规则**为主，prompt / 推理为辅。
- 结构化规则先由人定义业务拆分原则，后续逐步沉淀为配置。
- 主代理不可完全依赖自由 prompt 推理决定任务图。

### 6.3 子代理执行范式
- 一期采用**混合模式**：优先专用任务型 Agent，必要时允许受限 ReAct Worker。
- 受限 ReAct Worker 只可在当前 capability、节点策略、资源约束允许的范围内选择 tool。
- 不开放全局大 tool pool，不允许无边界自由扩展执行路径。

## 7. 注册、发现与资源调度
### 7.1 注册与发现
采用混合模式：
- **Capability Registry**：静态定义系统“会什么”。
- **Instance Registry**：动态反映“现在谁在线、谁可用、谁负载高”。
- 调度时先按 capability 匹配，再按实例状态和资源状态选择执行者。

### 7.2 统一执行协议
子代理统一视为“可执行实例”，一期物理部署可单机，但逻辑上统一支持：
- 本地执行逻辑
- 外部 RESTful API 适配器
- 未来远程 worker / 独立服务

### 7.3 资源调度策略
资源调度采用**混合策略**：
1. 先用硬性规则过滤可执行 Agent / 节点 / 资源。
2. 再在可行集合内基于实时负载、资源占用、能力匹配等动态评分选择。

一期资源维度至少覆盖：
- 执行并发
- Agent 能力资源
- 外部资源（模型额度、数据库连接、检索资源、CPU/内存/GPU 预算）

## 8. 协作共享、会话与记忆
### 8.1 协作共享模型
采用**分层共享**：
- 主代理/调度层持有全局任务图状态、共享状态摘要、资源视图。
- 子代理默认只拿当前子任务所需上下文、共享状态摘要、相关依赖产物。
- 不默认向所有 Agent 全量复制所有信息。

协作机制可借鉴 mailbox 风格设计：共享状态底座 + 定向上下文分发 + 短消息/长指令分层。

### 8.2 会话模型
- 同一 `conversation_id` 内任务**串行执行**。
- 前端在活跃任务未完成时不再发送新消息，后端也需做串行兜底校验。
- 记忆按**用户账户**组织，通过数据库关联 `account_id/user_id` 与 `conversation_id`。

### 8.3 会话记忆持久化
同一 `conversation_id` 下，一期至少持久化：
- 消息历史
- 任务状态（DAG、节点状态、执行结果摘要）
- 关键中间产物（可复用节点产物、关键分析结论、检索结果摘要）

账户体系采用混合模式：一期先接收上游系统传入的用户标识，后续预留内置认证/权限能力。

## 9. 节点策略、失败处理与取消
### 9.1 节点策略来源
节点关键性、依赖类型、失败策略、重试策略、timeout 策略采用：
- **规则模板自动推导 + 人工可覆盖**

执行 Agent 不临场发明策略，只按节点元数据执行。

### 9.2 节点策略建议字段
每个节点建议至少具备以下策略元数据：
- `criticality`: `required | optional | fallback`
- `dependency_type`: `hard | soft`
- `on_failure`: `fail_task | retry | skip | fallback`
- `retry_policy`: 次数、间隔、适用错误类型
- `timeout_policy`: queue / start / execution / heartbeat / deadline
- `resource_class`: 对资源级别的要求

### 9.3 取消机制
一期采用**硬停止语义**：
- 收到“停止处理 / 停止思考”请求后，立即冻结 DAG 调度、阻止新节点启动。
- 向运行中节点与外部执行单元发起 best-effort cancel。
- 未开始节点标记为取消阻断；运行中节点进入取消跟踪。
- 需要可审计的取消状态机、取消传播、资源释放、orphan 追踪、后台清扫机制。
- 对外部 API 不承诺物理层一定立刻停机，只承诺系统语义上立即取消并尽最大能力中断。

被取消任务视为**彻底作废**，后续同一会话不复用该任务的中间产物。

## 10. 实时事件流与观测日志
### 10.1 实时事件流粒度
对前端开放**执行可见粒度**事件，至少包括：
- 用户消息与助手输出
- 任务整体状态变化
- DAG 节点状态变化
- 子代理开始 / 结束
- 阶段性进度信息

### 10.2 一期事件流传输建议
一期建议优先使用 **SSE**：
- 当前主要是后端向前端单向推送执行状态
- SSE 更适合浏览器接入、实现简单、维护成本低
- 后续若存在双向协商型实时控制需求，再评估 WebSocket

### 10.3 日志与审计
一期观测能力包括：
- 运行日志
- 任务链路追踪
- 结构化事件审计

日志落地为**JSONL 结构化日志文件**，至少支持按以下字段检索：
- `conversation_id`
- `task_id`
- `agent_id`
- `node_id`

需记录关键决策、任务拆分结果、Agent 选择原因、状态迁移、错误异常、取消过程与资源释放结果。

## 11. API 草案
### 11.1 会话消息入口
#### `POST /api/v1/conversations/{conversation_id}/messages`
用于提交用户消息并触发新的异步任务。

请求体建议：
```json
{
  "account_id": "acc_123",
  "content": "请帮我分析这个任务",
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

## 12. 核心数据模型草案
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

## 13. 结构化规则配置草案（建议）
一期不强制先实现完整 DSL，但建议预留如下配置形态：

```yaml
routing_rules:
  - when:
      intent: document_retrieval
    then:
      root_capability: document.retrieve

  - when:
      intent: mysql_query
    then:
      root_capability: mysql.nl2sql_query

node_templates:
  bioinfo.external_job:
    criticality: required
    dependency_type: hard
    retry_policy:
      max_attempts: 1
    timeout_policy:
      execution_seconds: 1800
    resource_class: external_api
```

## 14. MVP 验收闭环（建议稿）
### 14.1 MVP 核心目标
证明主代理框架已经具备“接收对话消息 → 生成任务 DAG → 调度多个 capability → 调用外部执行单元 → 持久化状态与记忆 → 流式回传 → 支持硬停止”的完整闭环。

### 14.2 MVP 验收场景一：标准异步执行闭环
一个用户在已有 `conversation_id` 下发起一条消息，主代理需要：
1. 接收消息并在短时间内返回 `task_id`
2. 读取会话历史记忆
3. 生成一个最小可运行 DAG
4. 至少调度 3 个节点：
   - 任务理解 / 路由节点
   - 外部能力执行节点
   - 汇总输出节点
5. 通过统一执行协议调用至少一个外部 API 型执行单元
6. 将节点状态、任务状态、关键产物、最终消息持久化
7. 通过实时事件流向前端回传执行进度
8. 最终返回助手结果，并将任务置为 `completed`

### 14.3 MVP 验收场景二：硬停止闭环
在任务运行过程中，用户发起“停止处理”请求，系统需要：
1. 立即接受取消请求并返回 `cancelling`
2. 阻止新节点启动
3. 向运行中节点发起 best-effort cancel
4. 将未启动节点标记为取消阻断
5. 释放本地调度与资源占位
6. 输出结构化取消事件与 JSONL 审计日志
7. 将任务收敛到 `cancelled` 或 `cancellation_partial`
8. 后续同一会话可继续发起新任务，且不复用被取消任务产物

### 14.4 MVP 最低验收标准
- 消息提交接口可稳定返回 `task_id`
- 同一会话串行约束生效
- 任务图可查询
- 至少 1 个外部 API 型实例可被主代理成功调度
- 事件流可稳定输出关键状态变化
- JSONL 日志可按 `conversation_id / task_id` 检索
- 硬停止链路可成功跑通

## 15. 当前仍待定事项
以下内容仍需在后续版本继续细化：
- MVP 业务示例是否要绑定到具体业务能力（如 RAG / NL2SQL / 生信）
- 数据库、队列、状态存储的具体技术选型
- 外部 API cancel 契约的统一规范
- 任务优先级、配额与背压策略
- DAG 动态扩展的具体约束边界
- mailbox 风格协作机制的数据结构细节
