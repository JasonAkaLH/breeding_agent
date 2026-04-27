# 前端业务对话台 PRD（v1）

- **项目**：multi_agent_framework
- **范围**：前端 / 业务用户对话台
- **文档状态**：草案（基于当前后端 Phase 8.2 实现事实）
- **日期**：2026-04-27
- **上游依据**：`docs/prd/backend/00-主代理框架PRD.md`、`docs/prd/backend/05-API与核心数据模型.md`、`docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`、`docs/prd/backend/09-高层DAG规划与SQLQuery宏能力边界.md`
- **访谈记录**：`.omx/interviews/frontend-prd-20260427T072200Z.md`

## 1. 背景

当前后端已经具备主代理普通对话、SQLQuery 只读查询、FastAPI 接入、SSE 事件流、任务查询、取消、任务图、产物查询和 capability 目录等基础能力。前端尚未开始实现，因此第一版前端 PRD 应先贴合当前后端真实能力，避免把尚未开放的后端能力误写成 v1 必须项。

前端 v1 的产品定位是 **内部业务用户对话台**：让用户以对话方式向主代理或 SQLQuery 提问，并获得清晰、顺畅、可理解的回答。它不是研发/运维调试控制台。

## 2. 目标与非目标

### 2.1 目标

1. 提供一个业务用户可理解的对话入口。
2. 支持普通主代理对话的 streaming 回复展示。
3. 支持 SQLQuery 查询模式，并展示“自然语言摘要 + 简表预览”。
4. 基于 SSE 展示任务执行状态，但默认不暴露内部 DAG / 节点 / SQL / 审计日志细节。
5. 支持取消当前任务。
6. 基于当前已实现 API 完成 v1，不要求后端新增接口作为 v1 前置条件。

### 2.2 非目标

1. 不做研发/运维调试台。
2. 不做用户登录、权限、RBAC、账号管理系统。
3. 不做通用文件上传、文件管理、文件预览闭环。
4. 不默认展示 SQL、schema、SQL Guard、LLM fallback、审计日志等技术细节。
5. 不做服务端长历史中心、跨会话搜索、收藏、归档；如需要最近记录，v1 只能做浏览器本地缓存级体验。
6. 不实现前端侧任意工具调用平台。

## 3. 用户与核心场景

### 3.1 目标用户

- 内部业务用户：希望用自然语言查询信息、让主代理生成普通回答，重点关注“问了什么、答案是什么、是否可信”。
- 非 v1 主用户：研发、运维、框架调试人员。研发调试能力可通过后端日志、测试和后续调试台补充，不作为当前前端主体验。

### 3.2 核心场景

1. **普通对话**：用户输入一般问题，前端提交到主代理，实时展示 streaming 回复。
2. **数据库查询**：用户选择 SQLQuery 查询模式，输入品种、基因型、审定信息等查询问题，前端展示任务进度，完成后显示摘要和结果表预览。
3. **任务执行中**：用户看到“已提交 / 正在分析 / 正在生成答案 / 已完成”等业务化状态。
4. **任务取消**：用户对当前长任务点击取消，前端展示取消中和最终取消结果。
5. **失败或不支持**：前端展示可理解的失败说明，引导用户调整问题或切换模式。

## 4. 当前后端能力基线

### 4.1 已实现 API

| 能力 | 方法与路径 | 前端用途 |
|---|---|---|
| 提交消息 | `POST /api/v1/conversations/{conversation_id}/messages` | 创建用户消息和异步任务。 |
| 查询任务 | `GET /api/v1/tasks/{task_id}` | 获取任务状态、节点计数、取消状态。 |
| 订阅事件 | `GET /api/v1/tasks/{task_id}/events` | SSE replay + live 事件流。 |
| 取消任务 | `POST /api/v1/tasks/{task_id}/cancel` | 请求取消当前任务。 |
| 查询任务图 | `GET /api/v1/tasks/{task_id}/graph` | v1 默认不用作主展示，可保留为隐藏诊断数据。 |
| 查询产物 | `GET /api/v1/tasks/{task_id}/artifacts` | 获取主代理最终文本、SQLQuery 摘要、结果预览。 |
| 查询能力目录 | `GET /api/v1/capabilities` | 确认当前 public capability：主代理、SQLQuery。 |

### 4.2 提交消息契约

请求体字段来自 `src/api/dto.py`：

```json
{
  "account_id": "acc-1",
  "content": "查询品种龙粳33的基因型信息",
  "routing_mode": "auto",
  "capability_id": "sql_query.query",
  "client_message_id": "optional-client-id",
  "metadata": {}
}
```

响应：

```json
{
  "conversation_id": "conv-1",
  "message_id": "msg-...",
  "task_id": "task-...",
  "status": "accepted"
}
```

前端 v1 的路由策略：

- 普通对话模式：`capability_id = null`，由后端进入 `main_agent.respond`。
- 数据库查询模式：`capability_id = "sql_query.query"`。
- `sql_query.*` 内部 capability 不允许出现在前端可选项中。
- `account_id` v1 可用配置值或本地默认值传入，不做登录/账号管理。

### 4.3 SSE 事件契约

SSE event 的 `data` JSON 包含：

```json
{
  "event_id": "evt-...",
  "conversation_id": "conv-1",
  "task_id": "task-...",
  "node_id": "task-...:node",
  "event_type": "main_agent.output_delta",
  "payload": {},
  "created_at": "2026-04-27T..."
}
```

前端 v1 关注的 frontend-visible 事件：

| event_type | v1 展示策略 |
|---|---|
| `task.accepted` | 展示“任务已提交”。 |
| `task.graph_created` | 展示“正在规划/准备执行”，不显示节点图。 |
| `node.started` | 映射为“正在处理”。可根据 capability 做轻量文案映射。 |
| `node.completed` | 更新进度，不显示节点详情。 |
| `node.failed` | 展示当前任务失败提示。 |
| `task.completed` | 标记回答完成，触发 artifacts 拉取。 |
| `task.failed` | 展示失败说明和重试建议。 |
| `task.cancelled` | 展示任务已取消。 |
| `task.cancellation_requested` | 展示取消中。 |
| `main_agent.output_delta` | 追加到当前 assistant 气泡。payload：`delta`、`ordinal`。 |
| `main_agent.output_final` | 结束主代理 streaming 气泡。 |
| `sql_query.sql_guard_passed` | 默认不单独展示，可用于内部状态推进。 |
| `sql_query.sql_guard_blocked` | 展示“当前查询不符合只读/安全边界”，不展示原始 SQL。 |
| `task.interrupt_answered` | 当前无前端答复入口，仅作为已有任务恢复事件展示。 |

Audit-only 事件（如 `main_agent.llm_call`、`sql_query.llm_call`、`skill.*`、fallback 审计）不进入 v1 默认 UI。

## 5. 页面与信息架构

### 5.1 页面结构

v1 采用单页业务对话台：

1. **顶部栏**
   - 产品名 / 当前环境标识。
   - 当前能力状态：主代理、SQLQuery 是否可用。
2. **对话区**
   - 用户消息气泡。
   - 主代理 streaming 气泡。
   - SQLQuery 结果卡片。
   - 任务状态提示条。
3. **输入区**
   - 多行文本输入。
   - 模式选择：`普通对话` / `数据库查询（SQLQuery）`。
   - 提交按钮。
   - 当前任务运行时显示取消按钮。
4. **轻量结果/产物区**
   - 默认嵌入在回答卡片内，不做独立调试面板。
   - 可显示本次任务的最终摘要和结果表预览。

### 5.2 模式选择

由于当前后端不会把 `capability_id=None` 的消息自动路由到 SQLQuery，前端 v1 必须提供明确模式选择：

- 默认模式：普通对话。
- 用户选择“数据库查询（SQLQuery）”后，提交时传 `capability_id="sql_query.query"`。
- 前端可做轻量提示：当输入包含“查询、品种、基因型、审定”等词时，提示“这可能适合使用数据库查询模式”。该提示不改变后端契约。

## 6. 核心交互流程

### 6.1 普通主代理对话

1. 用户在普通对话模式输入问题。
2. 前端生成或复用 `conversation_id`。
3. 调用消息提交 API，`capability_id=null`。
4. 收到 `task_id` 后立即订阅 SSE。
5. 收到 `main_agent.output_delta` 时追加到 assistant 气泡。
6. 收到 `main_agent.output_final` 或 `task.completed` 后结束 loading。
7. 如 SSE 中断，前端可用 `GET /api/v1/tasks/{task_id}` 查询最终状态；已完成时可拉取 artifacts 兜底展示。

### 6.2 SQLQuery 查询

1. 用户选择数据库查询模式。
2. 前端提交消息，`capability_id="sql_query.query"`。
3. SSE 展示业务化进度：
   - 已提交；
   - 正在理解查询意图；
   - 正在检索数据库；
   - 正在整理结果。
4. 收到 `task.completed` 后调用 `GET /api/v1/tasks/{task_id}/artifacts`。
5. 前端从 artifacts 中优先提取：
   - `result_summary`：用于自然语言摘要；
   - `query_result_preview`：用于表格预览；
   - 若无法解析结构化内容，则使用 artifact `summary` 字段降级展示。
6. 默认不展示 `generated_sql`、`guard_report` 等技术 artifact。

### 6.3 取消任务

1. 当前任务运行中时，输入区显示“取消任务”。
2. 用户点击后调用 `POST /api/v1/tasks/{task_id}/cancel`。
3. 前端立即显示“取消请求已发送”。
4. 后续根据 SSE / 任务查询更新为“已取消”或“任务已在取消前完成”。

### 6.4 失败和不支持

- `400 Unsupported capability_id`：前端不应产生；若出现，提示“当前模式不可用，请刷新能力目录后重试”。
- `409 ConversationBusyError`：提示“当前会话已有任务运行中，请等待完成或取消后再继续”。
- `task.failed` / `node.failed`：显示“本次任务未完成”，并根据 payload code 映射业务提示。
- `sql_query.sql_guard_blocked`：提示“该查询不符合当前只读查询安全边界，请改用查询类问题”。
- interrupt 相关等待：当前无 API 可供前端读取 interrupt 详情或提交答复；v1 可在任务长时间未完成时调用任务图接口，若发现节点状态为 `waiting_for_input`，展示“任务需要补充信息；当前前端版本暂不支持继续该任务，请重新提交更完整的问题”。

## 7. SQLQuery 结果卡片

### 7.1 默认内容

1. 查询摘要：来自 `result_summary` artifact 的 `summary`。
2. 简表预览：来自 `query_result_preview` artifact 的 `columns` 和 `rows`。
3. 结果规模：如可解析 `row_count`，展示“共 N 行”。
4. 截断提示：如可解析到截断信息，展示“仅展示预览”。当前 query_result_preview 未必包含截断字段，前端不得假定一定存在。

### 7.2 默认隐藏内容

- SQL 文本；
- schema profile；
- SQL Guard 报告；
- LLM generation/fallback 细节；
- 内部 capability/node id；
- 审计事件。

### 7.3 降级策略

- 找不到 `result_summary`：显示“查询已完成，但摘要不可用”，并尝试展示其他 summary artifact。
- 找不到可解析表格：只展示自然语言摘要。
- artifacts API 失败：展示任务完成状态和“结果加载失败，可重试加载结果”。

## 8. 状态模型（前端视角）

| 前端状态 | 来源 | 展示 |
|---|---|---|
| idle | 初始/任务结束 | 可输入。 |
| submitting | POST messages 未返回 | 提交中。 |
| accepted | `task.accepted` / 202 response | 已提交。 |
| streaming | `main_agent.output_delta` | 追加回答。 |
| running | `task.graph_created`、`node.started` | 正在处理。 |
| loading_artifacts | `task.completed` 后 | 正在整理结果。 |
| completed | `task.completed` + 展示完成 | 可继续提问。 |
| cancelling | cancel response / cancellation event | 取消中。 |
| cancelled | `task.cancelled` | 已取消。 |
| failed | `task.failed` / `node.failed` | 失败提示。 |
| waiting_input_unsupported | 任务长期运行且任务图存在 `waiting_for_input` 节点 | 提示当前版本暂不支持继续。 |

## 9. 数据与本地持久化

v1 后端没有服务端会话列表和历史检索 API，因此前端只做最小本地状态：

- `conversation_id`：首次进入页面生成，保存在浏览器本地。
- `account_id`：使用配置默认值或本地输入，不做账号系统。
- `current_task_id`：运行中任务用于恢复状态和取消。
- 最近任务列表：可选，仅保存在浏览器本地，不承诺跨设备或服务端历史能力。

## 10. 验收标准

### 10.1 功能验收

1. 用户能在普通对话模式提交消息，并看到 streaming 回复。
2. 用户能在数据库查询模式提交 SQLQuery 问题，并在任务完成后看到摘要和简表预览。
3. 用户能看到任务执行中的业务化状态变化。
4. 用户能取消当前任务，并看到取消状态。
5. 用户连续提交同一会话任务时，如后端返回 409，前端能给出明确提示。
6. 前端不会向后端提交 internal SQLQuery capability id。
7. 前端默认不展示 DAG、SQL、schema、审计日志。
8. artifacts 不可解析时，前端能降级展示 summary 或错误提示。

### 10.2 体验验收

1. 首要体验是“问答闭环顺畅”：输入、提交、等待、回答完成的路径清晰。
2. 错误提示面向业务用户，不出现堆栈、内部异常文本或 provider secret。
3. SQLQuery 卡片让用户看到“答案 + 结果预览”，但不把用户带入调试视角。
4. 当前版本不支持继续 interrupt 时，提示应明确且不伪装成可继续。

## 11. 后续增强（非 v1 阻塞）

以下能力不属于 v1 必须项，但建议在后端补齐后进入前端 P1/P2：

1. Interrupt API：
   - `GET /api/v1/tasks/{task_id}/interrupts`
   - `POST /api/v1/tasks/{task_id}/interrupts/{interrupt_id}/answer`
2. SQLQuery 专用 frontend result event，减少前端解析 artifact `storage_ref` 的耦合。
3. typed artifact download/preview API。
4. 服务端 conversation list / message history / task history API。
5. CORS、部署配置、鉴权与账号体系。
6. 面向研发/运维的独立调试台，而不是混入业务对话台默认体验。

## 12. 代码依据

- API routes：`src/api/routes/conversations.py`、`src/api/routes/tasks.py`、`src/api/routes/capabilities.py`
- DTO：`src/api/dto.py`
- SSE：`src/api/sse.py`
- runtime 装配与路由：`src/api/runtime.py`
- 主代理 capability：`src/capabilities/main_agent/`
- SQLQuery workflow：`src/capabilities/sql_query/workflow.py`
- SQLQuery 结果与产物：`src/capabilities/sql_query/result_summarize.py`、`src/capabilities/sql_query/sql_execute_readonly.py`
- 后端 API PRD：`docs/prd/backend/05-API与核心数据模型.md`
