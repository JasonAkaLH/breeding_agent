# 前端业务对话台 PRD（v1）

- **项目**：multi_agent_framework
- **范围**：前端 / 业务用户对话台
- **文档状态**：草案（基于当前后端 Phase 8.2 与自动规划首轮实现事实）
- **日期**：2026-04-27
- **上游依据**：`docs/prd/backend/00-主代理框架PRD.md`、`docs/prd/backend/05-API与核心数据模型.md`、`docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`、对应可移除 Skill bundle 自带的边界文档
- **访谈记录**：`.omx/interviews/frontend-prd-20260427T072200Z.md`

## 1. 背景

当前后端已经具备主代理普通对话、可移除数据查询 Skill、FastAPI 接入、SSE 事件流、任务查询、取消、任务图、产物查询、capability 目录和默认自动规划等基础能力。前端 v1 应贴合当前后端真实能力，避免把 capability 选择责任交给业务用户。

前端 v1 的产品定位是 **内部业务用户对话台**：让用户以自然语言提问，由主代理自动判断是否需要调用数据查询能力 等能力，并获得清晰、顺畅、可理解的回答。它不是研发/运维调试控制台。

## 2. 目标与非目标

### 2.1 目标

1. 提供一个业务用户可理解的对话入口。
2. 支持普通主代理对话的 streaming 回复展示。
3. 支持主代理自动调用 数据查询能力，并展示“主代理回答 + 表格预览”。
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
2. **数据库查询**：用户直接输入品种、基因型、审定信息等查询问题，后端自动规划 数据查询 Skill + 主代理整合路径，前端展示任务进度，完成后显示最终回答与结果预览。
3. **任务执行中**：用户看到“已提交 / 正在分析 / 正在生成答案 / 已完成”等业务化状态。
4. **任务取消**：用户对当前长任务点击取消，前端展示取消中和最终取消结果。
5. **失败或不支持**：前端展示可理解的失败说明，引导用户调整问题或停止/重试任务。

## 4. 当前后端能力基线

### 4.1 已实现 API

| 能力 | 方法与路径 | 前端用途 |
|---|---|---|
| 提交消息 | `POST /api/v1/conversations/{conversation_id}/messages` | 创建用户消息和异步任务。 |
| 查询任务 | `GET /api/v1/tasks/{task_id}` | 获取任务状态、节点计数、取消状态。 |
| 订阅事件 | `GET /api/v1/tasks/{task_id}/events` | SSE replay + live 事件流。 |
| 取消任务 | `POST /api/v1/tasks/{task_id}/cancel` | 请求取消当前任务。 |
| 查询任务图 | `GET /api/v1/tasks/{task_id}/graph` | v1 默认不用作主展示，可保留为隐藏诊断数据。 |
| 查询产物 | `GET /api/v1/tasks/{task_id}/artifacts` | 获取主代理最终文本与 结构化表格结果预览。 |
| 查询能力目录 | `GET /api/v1/capabilities` | 确认当前 public capability：主代理与已安装 public Skill。 |

### 4.2 提交消息契约

请求体字段来自 `src/api/dto.py`：

```json
{
  "account_id": "acc-1",
  "content": "查询品种龙粳33的基因型信息",
  "routing_mode": "auto",
  "capability_id": null,
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

- 默认自动规划：`capability_id = null`，由后端自动判断单主代理或已安装数据查询 Skill -> `main_agent.respond` 数据整合。
- 前端 v1 不提供 数据查询 Skill 专属显式入口；始终交给自动规划与 capability registry。
- Skill 内部 domain stage 不允许出现在前端可选项中；前端只显示 generic progress 与 artifact。
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
| `skill.progress` | 展示 Skill 提供的 generic `label`，没有 label 时按通用数据查询阶段文案降级。 |
| `task.failed` / `node.failed` | 对 SQL Guard 阻断等失败展示“当前查询不符合只读/安全边界”，不展示原始 SQL。 |
| `task.interrupt_answered` | 展示补充信息已提交，并继续订阅原任务。 |

Audit-only 事件（如 `main_agent.llm_call`、`skill.llm_call`、`skill.llm_fallback`、fallback 审计）不进入 v1 默认 UI。

## 5. 页面与信息架构

### 5.1 页面结构

v1 采用单页业务对话台：

1. **顶部栏**
   - 产品名 / 当前环境标识。
   - 当前能力状态：主代理与已安装 public Skill 是否可用。
2. **对话区**
   - 用户消息气泡。
   - 主代理 streaming 气泡。
   - 数据查询结果卡片。
   - 任务状态提示条。
3. **输入区**
   - 多行文本输入。
   - 当前模式展示：`自动规划`。
   - 提交按钮。
   - 当前任务运行时显示取消按钮。
4. **轻量结果/产物区**
   - 默认嵌入在回答卡片内，不做独立调试面板。
   - 可显示本次任务的主代理最终回答和结果表预览。

### 5.2 自动规划

当前后端会把 `capability_id=None` 的消息交给自动规划入口，前端 v1 不再提供手动模式选择：

- 默认显示：`当前模式：自动规划`。
- 提交时始终传 `capability_id=null`。
- 前端可做轻量提示：当输入包含“查询、品种、基因型、审定”等词时，提示“主代理会自动判断是否需要调用数据查询能力”。该提示不改变后端契约。
- 后端若自动选择已安装数据查询 Skill，执行图由 Skill bundle 内部决定并由主代理整合；Skill 末端可返回筛选后的表格并保留原始表格预览，前端只展示业务化状态与最终结果，不展示 DAG 细节。

## 6. 核心交互流程

### 6.1 普通主代理对话

1. 用户直接输入问题。
2. 前端生成或复用 `conversation_id`。
3. 调用消息提交 API，`capability_id=null`。
4. 收到 `task_id` 后立即订阅 SSE。
5. 收到 `main_agent.output_delta` 时追加到 assistant 气泡。
6. 收到 `main_agent.output_final` 或 `task.completed` 后结束 loading。
7. 如 SSE 中断，前端可用 `GET /api/v1/tasks/{task_id}` 查询最终状态；已完成时可拉取 artifacts 兜底展示。

### 6.2 自动数据查询

1. 用户直接输入数据库/品种/审定/基因型类问题。
2. 前端提交消息，`capability_id=null`。
3. 后端自动规划为已安装数据查询 Skill -> `main_agent.respond`。
4. SSE 展示业务化进度：
   - 已提交；
   - 正在理解查询意图；
   - 正在检索数据库；
   - 正在筛选查询结果；
   - 正在整理回答。
5. 收到 `task.completed` 后调用 `GET /api/v1/tasks/{task_id}/artifacts`。
6. 前端从 artifacts 中优先提取：
   - 主代理最终文本：作为 assistant 最终回答；
   - `filtered_query_result`：优先用于筛选后的表格预览；
   - `query_result_preview`：仅在没有筛选结果时作为原始表格预览降级；
   - 主代理最终文本负责最终自然语言整合；
   - 若无法解析结构化内容，则使用 artifact `summary` 字段降级展示。
7. 前端不得根据任务图中出现了某个 capability 节点就改变 assistant 气泡的最终展示模式；主代理 text artifact 始终是最终自然语言回答，数据查询或后续其他 capability 的结构化结果只能作为补充卡片追加展示。
8. 默认不展示 `generated_sql`、`guard_report` 等技术 artifact。

### 6.3 取消任务

1. 当前任务运行中时，输入区显示“取消任务”。
2. 用户点击后调用 `POST /api/v1/tasks/{task_id}/cancel`。
3. 前端立即显示“取消请求已发送”。
4. 后续根据 SSE / 任务查询更新为“已取消”或“任务已在取消前完成”。

### 6.4 失败和不支持

- `400 Unsupported capability_id`：前端不应产生；若出现，提示“当前模式不可用，请刷新能力目录后重试”。
- `409 ConversationBusyError`：提示“当前会话已有任务运行中，请等待完成或取消后再继续”。
- `task.failed` / `node.failed`：显示“本次任务未完成”，并根据 payload code 映射业务提示。
- SQL Guard 阻断通过 `task.failed` / `node.failed` 的安全错误 payload 映射提示“该查询不符合当前只读查询安全边界，请改用查询类问题”。
- interrupt 相关等待：前端通过任务图与 interrupt API 发现 `waiting_for_input` 后展示补充信息卡片；用户下一条输入应作为 interrupt answer 继续原任务，而不是提交新任务或要求重新提问。

## 7. 数据查询结果卡片

### 7.1 默认内容

1. 查询完成提示：优先基于筛选后表格的 `row_count` 生成中性提示；主代理最终文本负责最终自然语言回答。
2. 简表预览：优先来自 `filtered_query_result` artifact 的 `columns` 和 `rows`；找不到时降级使用 `query_result_preview`。
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

- 找不到 `filtered_query_result` 但可解析原始表格：按原始表格 `row_count` 显示“查询已完成，共返回 N 行结果”。
- 找不到可解析表格：只展示主代理最终回答或 artifact summary 降级文本。
- artifacts API 失败：展示任务完成状态和“结果加载失败，可重试加载结果”。
- 当前端已经加载到最终 text artifact 或补充结果 artifact 后，后到达或批处理乱序的 streaming delta 不得覆盖最终回答；无最终 artifact 时则保留 streaming 内容作为降级回答。

### 7.4 多 capability 展示边界

- 数据查询 Skill 表格卡片是首个结构化 artifact renderer，不代表 assistant 消息模式只能属于 数据查询 Skill。
- 后续新增 capability 时，应在“artifact 解析 / renderer 注册”层增加补充展示模型，而不是在任务恢复、消息气泡或 text artifact 展示逻辑中写 capability 特判。
- 即使某个 capability 暂无专用 renderer，前端也应至少保留主代理最终 text artifact，避免结构化结果解析失败导致最终回答消失。

## 8. 状态模型（前端视角）

| 前端状态 | 来源 | 展示 |
|---|---|---|
| idle | 初始/任务结束 | 可输入。 |
| submitting | POST messages 未返回 | 提交中。 |
| accepted | `task.accepted` / 202 response | 已提交。 |
| streaming | `main_agent.output_delta` | 追加回答。 |
| running | `task.graph_created`、`node.started` | 正在处理。 |
| loading_artifacts | `task.completed` 后 | 正在整理回答。 |
| completed | `task.completed` + 展示完成 | 可继续提问。 |
| cancelling | cancel response / cancellation event | 取消中。 |
| cancelled | `task.cancelled` | 已取消。 |
| failed | `task.failed` / `node.failed` | 失败提示。 |
| waiting_for_input | 任务图存在 `waiting_for_input` 节点且发现 open interrupt | 展示补充信息卡片，下一条输入继续原任务。 |

## 9. 数据与本地持久化

v1 后端没有服务端会话列表和历史检索 API，因此前端只做最小本地状态：

- `conversation_id`：首次进入页面生成，保存在浏览器本地。
- `account_id`：使用配置默认值或本地输入，不做账号系统。
- `current_task_id`：运行中任务用于恢复状态和取消。
- 最近任务列表：可选，仅保存在浏览器本地，不承诺跨设备或服务端历史能力。

## 10. 验收标准

### 10.1 功能验收

1. 用户能在自动规划模式提交普通消息，并看到 streaming 回复。
2. 用户能不切换模式直接提交数据库类问题，并在任务完成后看到主代理整合后的回答；必要时可看到摘要和简表预览。
3. 用户能看到任务执行中的业务化状态变化。
4. 用户能取消当前任务，并看到取消状态。
5. 用户连续提交同一会话任务时，如后端返回 409，前端能给出明确提示。
6. 前端不会向后端提交 internal 数据查询 Skill capability id。
7. 前端默认不展示 DAG、SQL、schema、审计日志。
8. artifacts 不可解析时，前端能降级展示主代理回答、artifact summary 或错误提示。

### 10.2 体验验收

1. 首要体验是“问答闭环顺畅”：输入、提交、等待、回答完成的路径清晰。
2. 错误提示面向业务用户，不出现堆栈、内部异常文本或 provider secret。
3. 数据查询卡片让用户看到“答案 + 结果预览”，但不把用户带入调试视角。
4. 当前任务需要补充信息时，前端应把用户下一条输入作为 interrupt answer 继续原任务，而不是要求重新提交更完整的问题。

## 11. 后续增强（非 v1 阻塞）

以下能力不属于 v1 必须项，但建议在后端补齐后进入前端 P1/P2：

1. Interrupt API：
   - `GET /api/v1/tasks/{task_id}/interrupts`
   - `POST /api/v1/tasks/{task_id}/interrupts/{interrupt_id}/answer`
2. 通用 data-query frontend result event，减少前端解析 artifact `storage_ref` 的耦合。
3. typed artifact download/preview API。
4. 服务端 conversation list / message history / task history API。
5. CORS、部署配置、鉴权与账号体系。
6. 面向研发/运维的独立调试台，而不是混入业务对话台默认体验。

## 12. 技术选型建议与约束（草案）

本节用于约束 v1 前端实施方向，避免在未开始实现前过早引入重型框架或与当前后端事件模型不匹配的对话 SDK。技术选型后续如需调整，应直接补充到前端 PRD 或新增前端专题 PRD；本文先给出 PRD 级推荐口径。

### 12.1 行业常见路线判断

截至 2026-04-27，公开生态与主流工程实践中，前端对话类产品常见技术路线大致分为三类：

1. **企业内部系统路线**：React + TypeScript + 企业级组件库（Ant Design / Arco Design / MUI / Fluent UI），适合管理台、业务查询、表格预览、状态提示和权限体系逐步增强。
2. **AI 原生产品路线**：React + TypeScript + Tailwind CSS + shadcn/ui / Radix UI，适合高度定制的 ChatGPT / Claude 类对话体验和更强视觉品牌化。
3. **全栈 React 路线**：Next.js + React + TypeScript，适合需要 SSR、BFF、复杂路由、边缘部署或与 Vercel AI SDK 深度绑定的产品。

当前项目 v1 是内部业务对话台，核心是对接已有 FastAPI + SSE + artifacts 后端能力，而不是构建 SEO 页面或全栈 Web 应用。因此 v1 应优先选择轻量、清晰、与后端解耦的 SPA 路线。

### 12.2 v1 推荐技术栈

| 层级 | 推荐选择 | 说明 |
|---|---|---|
| 语言 | TypeScript | 前端 API DTO、SSE event、artifact 解析都需要明确类型；避免纯 JavaScript 带来的隐式契约漂移。 |
| 前端框架 | React | 当前主流生态最稳妥；对话组件、表格、企业组件库和测试工具链成熟。 |
| 构建工具 | Vite | v1 不需要 SSR / BFF；Vite SPA 足够轻量，便于独立部署或由后端静态托管。 |
| UI 组件库 | Ant Design | 更贴近内部业务系统；表格、提示、按钮、表单、状态反馈能力成熟，适合结构化简表预览。 |
| 流式通信 | 浏览器 `EventSource` / SSE | 当前后端已经提供 `GET /api/v1/tasks/{task_id}/events`；v1 不新增 WebSocket 协议。 |
| HTTP 请求 | `fetch` 封装 | v1 API 数量有限，先不引入重型 request 层。 |
| 状态管理 | React 本地状态 + 自定义 hooks | 对话流、当前任务、SSE 生命周期先用组件状态和 hooks 承载；避免过早引入 Redux。 |
| 服务端状态缓存 | 暂不强制；复杂化后再评估 TanStack Query | v1 没有服务端历史列表和复杂缓存一致性需求。 |
| 单元 / 组件测试 | Vitest + React Testing Library | 覆盖自动规划输入区、SSE event reducer、artifact 解析和错误状态展示。 |
| E2E | Playwright | 覆盖普通对话、数据查询、取消、失败降级等浏览器级主路径。 |

推荐组合：

```text
React + TypeScript + Vite + Ant Design + EventSource/SSE
```

### 12.3 备选方案与不选原因

| 方案 | v1 结论 | 原因 |
|---|---|---|
| Next.js App Router | 暂不作为 v1 默认 | 当前没有 SSR、SEO、BFF 或全栈部署需求；引入会增加工程复杂度。 |
| Tailwind CSS + shadcn/ui | 可作为 UI 风格备选 | 更适合高度定制 AI 产品；但 v1 有表格、状态、表单等内部系统需求，Ant Design 更省实现成本。 |
| WebSocket | 暂不引入 | 后端已实现 SSE；v1 是服务端单向推送任务事件和文本增量，不需要双向实时协议。 |
| Vercel AI SDK UI | 暂不作为核心依赖 | 当前后端不是 AI SDK 默认 stream protocol，而是自定义 task event + artifacts 模型；直接接入会产生协议适配成本。 |
| AG-UI | 作为后续协议参考 | 方向上适合 Agent UI，但 v1 已有后端 SSE event schema，不应为 v1 重写协议层。 |
| Redux / Zustand | 暂不强制 | v1 状态边界较小；只有在出现跨页面状态、复杂历史、全局偏好或调试台时再评估。 |

### 12.4 前端实现边界

1. v1 不因为选型引入新的后端前置要求；必须基于现有 API/SSE/artifacts 完成。
2. 前端不得为了适配 UI SDK 而要求后端改变 `event_type`、artifact 类型或 Skill public capability 边界。
3. 如果未来选择 Next.js，应明确其职责是前端应用框架还是 BFF；不得把后端 orchestration 逻辑迁入前端服务层。
4. 如果未来引入 AI 对话 SDK，应先定义兼容层，而不是让 UI 直接依赖第三方 stream protocol。
5. 技术选型发生重大变化前，应在前端 PRD 中补充：
   - 工程目录结构；
   - API client 与 SSE client 设计；
   - event reducer / 状态机设计；
   - artifact 解析与降级策略；
   - 测试计划与验收命令。

### 12.5 公开依据

- Stack Overflow Developer Survey 2025：React、Next.js、TypeScript 等 Web 技术使用情况。<https://survey.stackoverflow.co/2025/technology/>
- State of JS 2024：React / Vue / Angular / Svelte 等前端框架使用趋势。<https://2024.stateofjs.com/en-US/libraries/front-end-frameworks/>
- React 官方文档：新 React 应用推荐使用社区主流 React 框架；不需要 SSR/BFF 时可采用 Vite 等工具路线。<https://react.dev/learn/start-a-new-react-project>
- Vite 官方文档：现代前端构建工具与 SPA 工程入口参考。<https://vite.dev/guide/>
- MDN Server-Sent Events 文档：SSE 适合服务端向浏览器单向持续推送事件。<https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>
- Ant Design 官方文档：企业级 React UI 组件库参考。<https://ant.design/docs/react/introduce/>
- shadcn/ui 官方文档：现代可定制 UI 组件路线参考。<https://ui.shadcn.com/docs>
- Vercel AI SDK UI 文档：AI stream UI SDK 参考。<https://ai-sdk.dev/docs/ai-sdk-ui/overview>
- AG-UI 官方文档：Agent UI 事件协议参考。<https://docs.ag-ui.com/>

## 13. 代码依据

- API routes：`src/api/routes/conversations.py`、`src/api/routes/tasks.py`、`src/api/routes/capabilities.py`
- DTO：`src/api/dto.py`
- SSE：`src/api/sse.py`
- runtime 装配与路由：`src/api/runtime.py`
- 主代理 capability：`src/capabilities/main_agent/`
- 数据查询 Skill：`skill/<domain-query>/SKILL.md`
- 数据查询 Skill 结果与产物：`skill/<domain-query>/runtime/`（表格 preview 与结果筛选，随 bundle 可移除）
- 后端 API PRD：`docs/prd/backend/05-API与核心数据模型.md`
