# MCP 长任务与流式 SSE PRD

- **范围**：后端 / MCP Runtime / Streamable HTTP SSE / 长任务状态映射 / API 事件桥接
- **文档状态**：已复审收口，待实现
- **日期**：2026-05-14
- **前置基线**：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`
- **联合实施 Phase**：`docs/prd/MCP/README.md`
- **协议参考版本**：Model Context Protocol 2025-11-25

## 1. 一句话结论

本项目需要把 MCP 通信升级到完整的 **长任务流式 SSE** 能力：不再只解析单条 SSE 响应，而是要支持 MCP Streamable HTTP 的多事件流、断线恢复、server-to-client notification / request、progress、task status、取消、最终结果拉取与本项目 API/SSE 事件桥接。

这不是“多等一会儿 HTTP response”的小改动，而是 MCP Runtime 的一项正式能力扩展。

本项目是长期交付级产品，本 PRD 不接受“临时长连接可跑”的过渡方案作为最终验收；生产可用能力必须包含协议兼容、状态恢复、取消传播、资源上限、脱敏审计、API/SSE 可见性和回归测试闭环。

## 2. 当前事实

当前实现已经具备：

1. `StreamableHTTPTransport` 会发送 `Accept: application/json, text/event-stream`。
2. 如果 server 返回 `Content-Type: text/event-stream`，当前代码可以解析第一段 SSE `data:` 中的 JSON-RPC response。
3. 当前会记录 `MCP-Session-Id`、`Last-Event-ID`、SSE `retry`。
4. 当前测试已经覆盖“单条 SSE event 里返回一个 JSON-RPC response”。

当前缺口：

1. 不是边读边处理 SSE stream，而是等完整 HTTP response 后再解析。
2. 只解析第一段 event，不处理一个 stream 内多条 JSON-RPC message。
3. 没有 request id → pending future / task 的持续映射。
4. 没有 server-to-client request / notification 分发。
5. 没有基于 `retry` 与 `Last-Event-ID` 的自动断线恢复。
6. 没有把 MCP progress / task status 映射到本项目 task event / API SSE。
7. 没有长任务取消、恢复、最终结果查询、资源限制和审计闭环。

因此，当前只能算 **单条 SSE 响应兼容**，不能算完整长任务流式 SSE 支持。

## 3. 术语定义

### 3.1 MCP 长任务

本项目将 MCP 长任务定义为：

> 一个 MCP 工具调用不能在普通同步 `tools/call` 响应时间内稳定完成，需要持续运行、进度更新、取消、断线恢复或异步取结果的执行过程。

满足任一条件，即按 MCP 长任务治理：

1. 预期执行时间超过 30 秒。
2. 可能超过 HTTP client、gateway 或 server timeout。
3. 需要向用户展示进度。
4. 需要用户或系统取消。
5. 需要断线后恢复监听或继续查询状态。
6. 执行过程有多阶段状态，例如 working、input_required、completed、failed、cancelled。
7. 最终结果太大，不能稳定一次性返回。
8. 有副作用，不能简单自动重试。

### 3.2 流式 SSE

本 PRD 的“流式 SSE”不是“HTTP body 里只有一段 `data:`”。它指：

1. client 以 HTTP POST 发送 JSON-RPC request；
2. server 返回 `Content-Type: text/event-stream`；
3. client 持续读取 SSE events；
4. 每个 SSE `data:` 内是一条 JSON-RPC message；
5. server 可以在最终 response 前发送 progress、task status、logging、request 或 notification；
6. 断线后 client 可以用 `Last-Event-ID` 恢复；
7. client 不把断线视为取消；普通 in-flight request 取消必须显式发送 `notifications/cancelled`，task-augmented request 取消必须发送 `tasks/cancel`。

### 3.3 Task-augmented `tools/call`

MCP 2025-11-25 支持 task-augmented request。对 `tools/call` 来说，请求参数可携带：

```json
{
  "task": {
    "ttl": 1800000
  },
  "_meta": {
    "progressToken": "mcp-progress-xxx"
  },
  "name": "generate_report",
  "arguments": {
    "reportId": "rpt_001"
  }
}
```

如果 server 支持 task-augmented tools/call，可以先返回 `CreateTaskResult`，后续通过 `notifications/tasks/status`、`tasks/get`、`tasks/result`、`tasks/cancel` 完成长任务状态与结果治理。

除 server capabilities 外，`tools/list` 返回的 tool metadata 还可能通过 `execution.taskSupport` 表达单个 tool 的任务支持策略：

| `execution.taskSupport` | 本项目行为 |
|---|---|
| `required` | 调用该 tool 必须使用 task-augmented request；如果本项目未启用长任务或 negotiation 不通过，不得公开该 capability |
| `optional` | 可以按配置选择 task-augmented 或普通 `tools/call` |
| `forbidden` | 禁止对该 tool 发送 task-augmented request；配置为 long task required 时必须 fail closed |
| 未声明 | 按 MCP 2025-11-25 规范不得 task-augment；本项目按 `forbidden` 处理 |

## 4. 目标、用户与影响面

### 4.1 目标

1. 完整支持 MCP 2025-11-25 Streamable HTTP 的 SSE 多事件读取。
2. 支持 POST request 返回 SSE stream，并在 stream 中处理最终 JSON-RPC response。
3. 支持 GET 打开 server-to-client SSE stream，如果 server 返回 405 则按“不提供独立监听流”处理，而不是系统错误。
4. 支持 `retry`、`id`、`Last-Event-ID` 和断线恢复。
5. 支持 `notifications/progress` 映射为本项目 task progress event。
6. 支持 `notifications/tasks/status` 映射为本项目 MCP long-task status event。
7. 支持 task-augmented `tools/call`、`tasks/get`、`tasks/result`、`tasks/list`、`tasks/cancel`。
8. 支持 non-task request 的 `notifications/cancelled`。
9. 支持 server 在 SSE stream 中发起 `ping`；client 必须按 JSON-RPC 响应。
10. 对未声明或未实现的 server-to-client request 返回标准 method-not-found / unsupported error，并写脱敏审计。
11. 将 MCP 长任务事件桥接到本项目 API/SSE，前端可看到“正在执行、进度、取消、失败、完成”。
12. 不改变现有 capability 编排入口：MCP tool 仍必须先被包装为受控 `mcp.*` capability。

### 4.2 用户、干系人与影响面

| 对象 | 关注点 | 本 PRD 承诺 |
|---|---|---|
| 终端业务用户 | 长任务不能“卡死”，取消与进度要可见 | API/SSE 必须持续输出可见状态，最终完成 / 失败 / 取消必须有明确事件 |
| 前端业务对话台 | 事件语义稳定、payload 安全 | 只消费本项目事件，不直面原始 MCP session / task / event id |
| 后端 Runtime | 不破坏现有 capability / task / node 生命周期 | MCP 长任务仍通过 `mcp.*` capability 进入编排，最终产出标准 `CapabilityExecutionResult` |
| MCP Server 开发者 | 明确 task、progress、SSE、取消协议 | 按 MCP 2025-11-25 Streamable HTTP、Progress、Tasks、Cancellation 语义对接 |
| 平台运维 / 安全 | 资源、鉴权、审计、脱敏、恢复 | 必须有 deadline、重连上限、状态持久化策略、脱敏审计和可观测指标 |

### 4.3 成功判定

本 PRD 的成功判定不是“能收到最终结果”，而是：

1. 长任务执行期间，平台 task event 可以在最终结果之前实时出现；
2. stream 断开不会被误判为取消；
3. 用户取消可以传播到 MCP server；
4. 有 MCP task id 时，断线 / 进程恢复后可以判定任务最终状态；
5. 任何非法 JSON、非法 JSON-RPC、越权 request、超限 event 或敏感信息泄露风险均 fail closed。

## 5. 非目标

1. 不把本项目反向实现为 MCP Server。
2. 不允许 LLM / Planner 动态指定 MCP endpoint、token、header 或 tool name。
3. 不默认公开外部 server 的所有 tools。
4. 不把所有 MCP 长任务都视为可自动重试；有副作用的任务默认不可自动重试。
5. 不在本 PRD 里实现完整交互式 OAuth。
6. 不让未经 allowlist 的 server-to-client request 操作本项目内部能力。
7. 不把单条 SSE 响应解析作为最终验收；最终验收必须覆盖多事件、重连、取消和任务结果。

## 6. 目标架构

```text
MCPToolExecutor
  └─ MCPRuntimeState
      ├─ MCPClient
      │   ├─ MCPStreamableHTTPTransport
      │   ├─ SSEEventParser
      │   ├─ MCPMessageRouter
      │   ├─ MCPRequestTracker
      │   └─ MCPTaskClient
      ├─ MCPLongTaskRegistry
      ├─ MCPBundle / ToolBinding
      └─ EventBridge -> 本项目 task events / API SSE
```

### 6.1 MCPStreamableHTTPTransport

负责：

1. 发送 HTTP POST JSON-RPC message。
2. 使用 streaming HTTP client 增量读取 `text/event-stream`。
3. 解析多个 SSE event。
4. 保存和更新 `MCP-Session-Id`、SSE event id、retry。
5. 在断线后按 `retry` 等待，并按 Streamable HTTP 规范用 HTTP GET + `Last-Event-ID` 发起恢复；不得重新 POST 原始 `tools/call` 来“重连”。
6. 支持 HTTP GET 打开独立 server-to-client SSE stream。
7. 区分正常结束、断线、超时、取消和 server session 失效。
8. GET stream 只能承载 server-to-client request / notification，或恢复此前断开的 stream；不得把无关 JSON-RPC response 当成当前 request 的完成信号。

### 6.2 SSEEventParser

负责把原始 SSE 文本转成结构化 event：

```python
SSEEvent(
    id="evt-123",
    event="message",
    retry_ms=1000,
    data='{"jsonrpc":"2.0", ...}'
)
```

要求：

1. 支持多行 `data:` 合并。
2. 支持 comment / heartbeat 行。
3. 支持 `id`、`event`、`retry`。
4. `data` 必须解析为 JSON object。
5. 单条 event 超限必须 fail closed。
6. 非法 JSON、非法 JSON-RPC、超大 event 必须返回稳定错误码。

### 6.3 MCPMessageRouter

负责处理 stream 中的 JSON-RPC message：

| message 类型 | 行为 |
|---|---|
| response | 按 `id` 找到 pending request，完成对应 future / task |
| notification | 分发到 progress、task status、logging、list changed 等 handler |
| request | 支持 `ping`；未实现方法返回 method-not-found / unsupported error |
| invalid | 记录协议错误，按 request / stream 策略 fail closed |

router 不能假设某个 task 的后续消息一定只出现在原始 POST stream。长任务相关 progress / status / request 可能出现在 POST response stream、`tasks/result` stream 或独立 GET stream，必须通过 response id、progress token、`io.modelcontextprotocol/related-task` 和 registry 关联。

### 6.4 MCPRequestTracker

负责 request id 与状态：

1. 每个 MCP session 内 request id 不复用。
2. 每个 in-flight request 有 deadline、cancel token、progress token、last_event_id、retry state。
3. response id 必须匹配 pending request。
4. request 完成、失败、取消后必须清理 tracker，避免泄漏。
5. stream 断开不等于 request 取消。

### 6.5 MCPTaskClient

新增 task 相关调用：

| 方法 | 用途 |
|---|---|
| `tasks/get` | 查询任务当前状态 |
| `tasks/result` | 获取最终结果；如果 task 尚未 terminal，允许按协议阻塞 / 返回 SSE stream，但必须受 timeout、cancel 和 polling 策略约束 |
| `tasks/list` | 按分页列出 server 侧任务，主要用于诊断 / 恢复 |
| `tasks/cancel` | 取消 task-augmented 长任务 |

只有 server capability 声明支持 tasks 时，才允许使用 task-augmented 模式。

### 6.6 MCPLongTaskRegistry

负责本项目内部映射：

| 字段 | 说明 |
|---|---|
| `conversation_id` | 本项目会话 |
| `task_id` | 本项目 task |
| `node_id` | 本项目节点 |
| `capability_id` | `mcp.*` capability |
| `server_id` | MCP server |
| `tool_name` | MCP tool |
| `mcp_request_id` | JSON-RPC request id |
| `mcp_task_id` | MCP task id，可为空 |
| `progress_token` | MCP progress token |
| `session_id` | MCP session id |
| `last_event_id` | 最近已处理 SSE event id |
| `status` | normalized long-task status |
| `started_at` / `updated_at` / `finished_at` | 时间字段 |

生产级长任务必须具备可恢复状态。要求如下：

1. raw `mcp_task_id`、`session_id`、`last_event_id`、`progress_token` 等只允许进入受控 runtime state / storage，不得进入前端事件 payload。
2. in-memory registry 只允许用于 unit test、local dev 或 shadow 验证，不得作为生产 `long_task.enabled=true` 的最终实现。
3. 生产 `enforce` 模式必须把 registry 写入可恢复存储，或由后续 Rust sidecar runtime 作为 durable registry 承担；否则只能声明支持“网络重连”，不能声明支持“进程重启恢复”。
4. registry 必须支持 terminal 状态不可逆：`completed`、`failed`、`cancelled` 后不得被 replay 的 `working` 覆盖。
5. registry 清理必须遵守 MCP task `ttl`、本项目 retention 策略和审计要求。

### 6.7 EventBridge / live event emission

当前代码已有 `EventSink`、`InMemoryEventBroker`、`ApiRuntime.iter_frontend_events()` 与 `record_live_event()`，主代理执行器已经可以通过 `live_event_recorder` 在最终结果前写入事件；但 MCP executor 当前主要通过 `CapabilityExecutionResult.events` 在执行结束后统一返回事件。

MCP 长任务要满足 API/SSE 实时可见，必须补齐 MCP executor 的 live event bridge：

1. `MCPToolExecutor` 必须接入受控 `EventSink` / `record_live_event` 等价能力，在执行期间实时写 `mcp.long_task_*` 事件。
2. live event 写入必须同时完成 storage append 与 event broker publish，避免“前端看到但历史查不到”或“历史有但 SSE 不推送”。
3. 如果现有 `ExecutorPort` 不足以传入 live event recorder，应扩展 runtime 装配或 executor 构造参数，不得把 progress 缓存在内存中等最终结果后再一次性输出。
4. live event 失败必须按安全策略处理：审计失败不应泄露敏感信息；事件存储失败时不能假装长任务正常可观测。
5. event bridge 必须有背压与 rate limit，不能让外部 MCP server 通过 progress flooding 压垮本项目 API/SSE。

## 7. 协议行为要求

### 7.1 初始化与 capability negotiation

1. 初始化仍使用 `initialize`。
2. client 默认仍不声明未实现的 client capabilities，例如 roots、sampling、elicitation。
3. 本项目作为 client，必须读取 server `InitializeResult.capabilities.tasks`。只有 server 声明 `tasks.requests.tools.call` 且本项目实现已启用长任务时，才允许发送 task-augmented `tools/call`。
4. 本项目还必须读取 `tools/list` 的 `execution.taskSupport`。server capability 是粗粒度开关，tool-level `execution.taskSupport` 是细粒度约束，两者都通过才允许启用 task-augmented `tools/call`。
5. 只有 server 声明 `tasks.cancel` 时，才允许用 `tasks/cancel` 取消 MCP task；否则只能按普通 in-flight request cancellation 处理。
6. 只有 server 声明 `tasks.list` 时，才允许用 `tasks/list` 做诊断或恢复。
7. server 未声明 task support 时，不得发送 task-augmented `tools/call`。
8. server 声明不完整时，按 tool 配置 fail closed 或降级为普通 streamable `tools/call`，但仍可接收与当前 request 关联的 `notifications/progress`。

### 7.2 普通短调用

短调用仍允许：

1. `tools/call` 返回普通 `application/json` response；
2. `tools/call` 返回 SSE stream，但 stream 中最终只有一个 response。

短调用不应强制进入长任务 registry。

### 7.3 长任务调用

对配置为 long-task 的 MCP tool：

1. 必须生成 progress token。
2. 如果 server 支持 task-augmented `tools/call`，请求参数必须携带 `task`。
3. 如果 tool-level `execution.taskSupport=required`，但本项目无法发送 task-augmented request，不得公开该 capability。
4. 如果 tool-level `execution.taskSupport=forbidden`，不得发送 task-augmented request；配置要求长任务时必须 fail closed。
5. 如果 server 不支持 tasks，但支持 SSE progress，只有在配置为 `task_augmented_mode=preferred` 时才允许以 streaming `tools/call` 承载长任务，且必须有明确 timeout、cancel 和 final response。
6. 长任务开始后必须写本项目 task event。
7. 长任务完成后必须产出标准 `CapabilityExecutionResult`。

### 7.4 Progress

MCP `notifications/progress` 映射为本项目事件：

```json
{
  "event_type": "mcp.long_task_progress",
  "payload": {
    "server_id": "reporting",
    "tool_name": "generate_report",
    "progress": 40,
    "total": 100,
    "message": "正在生成第 4 个章节"
  }
}
```

要求：

1. progress 必须按 progress token 关联到 request / task。
2. task-augmented request 的 progress token 在 task 生命周期内持续有效，直到 task 进入 terminal 状态。
3. progress 数值必须按 MCP progress 语义单调递增；出现倒退时不得覆盖已记录最大进度，应写协议诊断。
4. progress message 必须做长度限制和脱敏。
5. progress 不得被 Planner 当成最终结果。
6. progress 乱序时按 event id / received_at 做幂等处理。

### 7.5 Task status

MCP `notifications/tasks/status` 映射为本项目事件：

```json
{
  "event_type": "mcp.long_task_status",
  "payload": {
    "mcp_task_ref": "mcp-task-fp-001",
    "status": "working",
    "status_message": "正在处理"
  }
}
```

状态映射：

| MCP status | 本项目语义 |
|---|---|
| `working` | running / in_progress |
| `input_required` | 首版不默认支持；必须映射为 `mcp_task_input_required_unsupported` 并安全失败，除非同一实现 PR 同步完成 server-to-client elicitation / Interrupt bridge 与安全测试 |
| `completed` | 可以调用 `tasks/result` 获取最终结果，或等待 final response |
| `failed` | capability error |
| `cancelled` | capability cancelled |

`notifications/tasks/status` 是可选通知，不能作为唯一状态来源。只要 registry 已有 `mcp_task_id`，本项目必须按 server `pollInterval` 或本项目上限调用 `tasks/get` 轮询，直到 task 进入 terminal 状态或超时 / 取消。

### 7.6 最终结果

最终结果来源优先级：

1. 如果当前 SSE stream 收到原始 request 对应的 JSON-RPC response，直接消费 response。
2. 如果 response 是 `CreateTaskResult`，写入 registry，并按 `notifications/tasks/status` + `tasks/get` 监控状态；task terminal 后调用 `tasks/result` 获取最终 `CallToolResult`。
3. 如果 stream 断开但 task id 已知，按 `tasks/get` / `tasks/result` 恢复。
4. 如果既没有 response 也没有 task id，按 stream failure 处理。

最终结果仍必须走现有 MCP output schema validation、output sanitizer、size limit 和 `CapabilityExecutionResult` 映射。

### 7.7 取消

本项目 task cancellation 必须向 MCP server 传播：

| 场景 | MCP 行为 |
|---|---|
| 普通 in-flight request，没有 MCP task id | 发送 `notifications/cancelled`，携带 `requestId` 和 reason |
| task-augmented request，已有 MCP task id | 发送 `tasks/cancel`，携带 `taskId` |
| 本地 stream 断开 | 不视为取消；尝试恢复或按超时失败处理 |
| server 已完成但本地取消晚到 | 必须幂等处理，不得把 completed 改回 running |

取消后：

1. 本项目不再消费迟到的成功结果作为用户可见结果。
2. 迟到 response / event 只能记录脱敏 audit。
3. cancel event 必须进入 API/SSE。
4. API `cancel_task` 不能只把本地 task 标记为 cancelled；对正在执行的 MCP node，runtime 必须向 MCP executor 传递 cancellation signal，executor 必须在有界 deadline 内发送 `notifications/cancelled` 或 `tasks/cancel`。
5. 如果本地 asyncio task 被取消，MCP executor 必须在 `CancelledError` 路径中执行同样的 remote cancel / cleanup 逻辑，避免只取消本地协程而让远端继续运行。

### 7.8 Server-to-client request

MCP server 可能在 SSE stream 中发起 request。

首批支持：

| server request | 本项目行为 |
|---|---|
| `ping` | 返回成功 response |
| 未实现方法 | 返回 JSON-RPC method-not-found / unsupported error |
| sampling / elicitation / roots 等未启用能力 | 默认拒绝并审计 |

server-to-client request 的 response 必须按 Streamable HTTP 规则通过新的 HTTP POST 发回 MCP endpoint；不得试图在接收的 SSE stream 上写回数据。

后续如果要支持 elicitation，应映射到本项目 Interrupt 机制，必须单独补测试和安全策略。

## 8. 配置要求

MCP tool 配置需要新增长任务开关和限制：

```yaml
mcp_runtime:
  servers:
    - server_id: reporting
      endpoint: https://mcp.example.com/mcp
      protocol_version: "2025-11-25"
      transport: streamable_http
      tools:
        - tool_name: generate_report
          expose: true
          public_name: 生成报告
          public_description: 根据报表编号生成完整分析报告
          risk_level: read_only
          planner_allowed_fields:
            - report_id
          long_task:
            enabled: true
            task_augmented_mode: required
            ttl_ms: 1800000
            progress_events: true
            max_duration_seconds: 1800
            stream_idle_timeout_seconds: 90
            reconnect_max_attempts: 20
```

默认规则：

1. 未显式启用 `long_task.enabled` 的 tool 按短调用处理。
2. 长任务 tool 必须是 allowlist tool。
3. 长任务 tool 必须声明 risk level；非 read-only 工具必须单独审批。
4. `task_augmented_mode` 取值为 `required`、`preferred`、`disabled`：
   - `required`：server capability 与 tool-level `execution.taskSupport` 必须允许 task-augmented request，否则 fail closed 且不公开 capability。
   - `preferred`：优先使用 task-augmented request；如果 server capability 或 tool-level `execution.taskSupport` 不允许 task augmentation，则降级为普通 streaming `tools/call` 并记录诊断。
   - `disabled`：即使 server 支持 tasks，也不发送 task-augmented request，只使用普通 `tools/call`。
5. 超过 `max_duration_seconds` 必须取消或失败，不得无限运行。
6. `execution.taskSupport=required` 的 server tool 不允许配置为 `disabled`；`execution.taskSupport=forbidden` 或未声明的 server tool 不允许配置为 `required`，`preferred` 只能降级普通 `tools/call`，不得 task-augment。

本文中的 `enforce` 指生产环境正式启用并对用户承诺该能力，不特指 Rust feature flag；如果后续 Rust MCP sidecar 接管 durable registry，其自身仍需遵守 Rust PRD 中的 `off|shadow|enforce` 模式。

## 9. 资源限制与默认值

| 项 | 默认值 | 说明 |
|---|---|---|
| long task threshold | 30s | 超过即按长任务治理 |
| default max duration | 30min | 可按 tool 收紧 |
| hard max duration | 2h | 突破需单独 PRD / 配置评审 |
| stream idle timeout | 90s | 无 event / heartbeat 超过该值触发恢复 |
| reconnect max attempts | 20 | 指数退避，受 server `retry` 影响 |
| reconnect max interval | 30s | 防止无限快速重连 |
| SSE event max size | 256KB | 单 event 上限 |
| stream total message cap | 32MB | 单次工具调用累计消息上限 |
| progress event rate | 最多 2 条 / 秒 | 超出合并或采样 |
| task status event rate | 最多 1 条 / 秒 | 同状态重复事件应去重 |
| final result size | 沿用 MCP tool output limit | 超限截断或转 artifact，按现有策略 |

超限行为必须返回稳定错误码，例如：

- `mcp_stream_idle_timeout`
- `mcp_stream_reconnect_exhausted`
- `mcp_long_task_timeout`
- `mcp_long_task_cancelled`
- `mcp_sse_event_too_large`
- `mcp_task_result_unavailable`
- `mcp_task_input_required_unsupported`

## 10. 状态、事件与审计

### 10.1 API/SSE 事件

新增事件建议：

| event_type | 用途 |
|---|---|
| `mcp.long_task_started` | 长任务开始 |
| `mcp.long_task_progress` | 进度更新 |
| `mcp.long_task_status` | MCP task 状态变化 |
| `mcp.long_task_reconnected` | SSE 断线恢复成功 |
| `mcp.long_task_cancel_requested` | 用户 / 系统请求取消 |
| `mcp.long_task_cancelled` | 取消完成 |
| `mcp.long_task_failed` | 长任务失败 |
| `mcp.long_task_completed` | 长任务完成 |

事件投递要求：

1. `mcp.long_task_started`、`mcp.long_task_progress`、`mcp.long_task_status`、`mcp.long_task_cancel_requested` 必须能在最终 `CapabilityExecutionResult` 返回前进入 API/SSE。
2. 前端可见事件 `visibility` 使用 `FRONTEND`，审计专用细节使用 `AUDIT_ONLY`。
3. 前端事件只允许暴露 `server_id`、`tool_name`、`capability_id`、normalized status、progress、message、duration、safe reference 等脱敏字段。
4. raw `mcp_task_id`、`MCP-Session-Id`、`Last-Event-ID`、`progressToken`、request id 不得进入前端事件；如需关联，只能使用不可逆 fingerprint / short ref。
5. 事件写入必须先通过 schema / payload sanitizer 校验，失败时不得把外部原文直接透传到前端。

事件 payload 必须脱敏，禁止写入：

- token；
- Authorization header；
- 完整工具参数；
- 完整工具输出；
- 内部真实 URL；
- secret reference value；
- 原始外部异常堆栈。

### 10.2 Audit

必须记录：

1. server_id、tool_name、capability_id；
2. conversation_id、task_id、node_id；
3. MCP request id fingerprint、MCP task id fingerprint、progress token fingerprint；
4. stream open / close / reconnect；
5. cancel request / cancel result；
6. final status；
7. error code；
8. duration、event count、bytes count。

不得记录敏感原文。raw MCP task / session / event id 只允许进入受控 durable registry；普通 audit 只记录 fingerprint。

### 10.3 幂等与去重

1. SSE event id 已处理后不得重复消费。
2. 同一个 final response 只能完成一次 capability result。
3. 重连后 replay 的 progress / status event 必须去重。
4. task completed / failed / cancelled 为 terminal 状态，迟到 working 不得覆盖 terminal。

## 11. 错误处理

| 错误 | 行为 |
|---|---|
| server 返回普通 JSON-RPC error | 映射为 MCP remote error |
| SSE event JSON 非法 | stream fail closed，尝试恢复；恢复失败则 capability error |
| response id 未匹配 | protocol error，fail closed |
| stream 断开 | 不等于取消；按 retry / Last-Event-ID 恢复 |
| session 404 | 重新 initialize；如有 task id，尝试 tasks/get / tasks/result 恢复 |
| reconnect 耗尽 | capability error，可重试性按 tool risk 与 task 状态判断 |
| cancellation 失败 | 本地 task 仍按 cancel requested 处理，并写审计 |
| task result 不可用 | capability error，不输出半成品为最终结果 |
| task 进入 `input_required` 但未启用 elicitation / Interrupt bridge | 返回 `mcp_task_input_required_unsupported`，不得无限等待或让外部 server 直接驱动用户输入 |

## 12. 安全要求

1. SSE stream 是外部输入，所有 message 必须 schema 校验。
2. server-to-client request 默认不可信；只支持明确 allowlist 方法。
3. progress / logging / status message 不得进入 system prompt。
4. 长任务中间内容只作为外部工具状态，不得被当成用户指令。
5. 对有副作用的 MCP tools，自动重连只能恢复监听或查询状态，不得重复发起 `tools/call`。
6. `Last-Event-ID`、session id、task id 不得暴露给前端原文；前端只看平台 task event。
7. 鉴权 token 不得进入 log / audit / event / error metadata。

## 13. 与现有代码的影响范围

预计需要改动：

| 路径 | 改动 |
|---|---|
| `src/integrations/mcp/protocol.py` | 增加 SSE event、progress、task、cancel、message router 相关模型 |
| `src/integrations/mcp/transport_http.py` | 从单次 response 解析升级为 streaming read、GET stream、reconnect、Last-Event-ID |
| `src/integrations/mcp/client.py` | 增加 request tracker、message router、task API、cancel API、server request handling |
| `src/integrations/mcp/runtime_state.py` | 增加 long-task tool binding、task registry、bundle capability negotiation |
| `src/capabilities/mcp_tool/executor.py` | 将长任务进度 / 状态桥接为 task events，最终结果仍映射为 CapabilityExecutionResult |
| `src/core/contracts.py` / executor 装配 | 如现有 `ExecutorPort` 无法传 live event recorder / cancellation signal，补受控扩展点 |
| `src/api/runtime.py` | MCP executor 装配 live event recorder，取消任务时把 cancellation signal 传播到 MCP executor |
| `src/api/` / lifecycle event 层 | 如现有 event schema 不足，补 MCP long-task event 类型与 API/SSE payload sanitizer |
| `src/storage/` | 生产 `enforce` 模式需要 durable long-task registry；至少覆盖 task id、session、last event、terminal status 与 TTL |
| `tests/integrations/` | fake streaming MCP server、multi-event、reconnect、task result、cancel 测试 |
| `tests/capabilities/mcp_tool/` | executor 级长任务、progress、cancel、失败映射测试 |
| `tests/api/` / `tests/e2e/` | API/SSE 可见事件与取消联动测试 |

## 14. 测试策略

### 14.1 TDD 要求

实现前必须先补失败测试：

1. 单个 POST 返回多条 SSE event，最终 response 正确完成。
2. stream 中包含 progress notification，能映射为 task event。
3. stream 中包含 task status notification，能映射为 task event。
4. server 发起 `ping` request，client 返回 response。
5. server 发起未支持 request，client 返回 unsupported error。
6. stream 断开，client 按 `retry` + `Last-Event-ID` 恢复。
7. replay event 不重复消费。
8. task-augmented `tools/call` 返回 `CreateTaskResult`，随后 `tasks/result` 获取最终结果。
9. 用户取消普通 in-flight request，发送 `notifications/cancelled`。
10. 用户取消 task-augmented request，发送 `tasks/cancel`。
11. task terminal 后迟到 event 不覆盖 terminal 状态。
12. SSE event 超大、JSON 非法、response id 不匹配均 fail closed。
13. API/SSE 订阅端在最终结果前收到 `mcp.long_task_progress` 或 `mcp.long_task_status`。
14. server capability 与 tool-level `execution.taskSupport` 组合覆盖 `required`、`optional`、`forbidden`、未声明四种情况。
15. durable registry 恢复测试：stream 断开或 runtime 重启后，能按 `Last-Event-ID` / `tasks/get` / `tasks/result` 判定最终状态；若未启用 durable registry，不得通过生产验收。
16. task 进入 `input_required` 且未启用 elicitation / Interrupt bridge 时，返回稳定错误 `mcp_task_input_required_unsupported`，不得卡住等待。

### 14.2 最小测试命令

沿用现有分层 unittest：

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
```

## 15. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-LONG-AC-001 | POST SSE stream 可以增量处理多条 JSON-RPC message | fake MCP server integration test |
| MCP-LONG-AC-002 | `notifications/progress` 映射为本项目 task progress event | executor / API SSE test |
| MCP-LONG-AC-003 | `notifications/tasks/status` 映射为本项目 long-task status event | integration / API test |
| MCP-LONG-AC-004 | 支持 task-augmented `tools/call`、`tasks/get`、`tasks/result`、`tasks/cancel` | protocol tests |
| MCP-LONG-AC-005 | SSE 断线后按 `retry` 与 `Last-Event-ID` 恢复，且 replay 去重 | reconnect test |
| MCP-LONG-AC-006 | 本项目取消能传播到 MCP server，普通 request 用 `notifications/cancelled`，task 用 `tasks/cancel` | cancellation tests |
| MCP-LONG-AC-007 | server `ping` 可响应，未实现 server request 被标准拒绝并审计 | server-to-client request tests |
| MCP-LONG-AC-008 | 长任务最终仍产出标准 `CapabilityExecutionResult`，并通过 output schema / sanitizer | capability executor tests |
| MCP-LONG-AC-009 | 资源限制、idle timeout、event size、duration、reconnect attempts 生效 | fault injection tests |
| MCP-LONG-AC-010 | secret、token、完整参数、完整输出不进入 event / audit / error | redaction snapshot tests |
| MCP-LONG-AC-011 | 旧短调用行为不回归，普通 JSON response 与单条 SSE response 仍可用 | existing MCP regression tests |
| MCP-LONG-AC-012 | API/SSE 可在最终 `CapabilityExecutionResult` 前看到长任务 progress / status | API streaming timing test |
| MCP-LONG-AC-013 | server capability 与 `execution.taskSupport` negotiation 正确控制公开、降级或 fail closed | negotiation matrix tests |
| MCP-LONG-AC-014 | 生产 `long_task.enabled=true` 具备 durable registry 或等价恢复能力；in-memory registry 不得通过 enforce 验收 | storage / restart recovery tests |
| MCP-LONG-AC-015 | 本地 task cancellation 能真正触发 MCP remote cancel，而不是只标记本地状态 | cancellation propagation integration test |
| MCP-LONG-AC-016 | `input_required` 在未实现 elicitation / Interrupt bridge 时稳定失败，不造成挂起或越权输入 | input-required unsupported test |

## 16. Rollout / rollback

1. 先实现 streaming parser 与 message router，但默认不启用 long-task tool。
2. durable registry 未落地前，只允许 local dev / unit test / shadow 验证长任务，不得对生产 tool 开启 `long_task.enabled=true` enforce。
3. 对已配置 `long_task.enabled=true` 且通过 capability / `execution.taskSupport` negotiation 的 MCP tool 开启长任务模式。
4. 若 long-task negotiation 失败，required tool fail closed；preferred tool 可降级但必须记录诊断；forbidden tool 不得 task-augment。
5. 旧短调用路径必须保留并通过回归测试。
6. 若出现长任务 stream 故障，可按 tool 关闭 `long_task.enabled`，退回短调用或不公开该 capability。
7. 不允许在失败时绕过 schema validation、输出清洗或 allowlist。

## 17. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| SSE stream 长连接泄漏 | 资源耗尽 | deadline、idle timeout、max duration、shutdown drain、tracker cleanup |
| 断线重连重复消费事件 | 重复进度或重复最终结果 | event id 去重、terminal 状态不可覆盖、final result exactly-once |
| 有副作用 tool 被重复执行 | 外部生产副作用 | 重连只恢复 stream / 查询 task，不重复发起 `tools/call` |
| server-to-client request 越权 | 外部 server 影响内部系统 | 默认只支持 ping；其他 request method-not-found / unsupported |
| progress 被当成指令 | prompt 注入风险 | progress 只进入状态事件，不进入 system prompt |
| 长任务状态丢失 | 用户看不到结果或无法取消 | task registry、MCP task id、Last-Event-ID、tasks/get/result 恢复 |
| in-memory registry 被误当生产恢复能力 | 进程重启后无法找回远端任务 | enforce 必须 durable registry；in-memory 只能用于测试 / shadow |
| MCP Tasks 仍是实验特性 | 后续协议变更导致兼容风险 | pin `2025-11-25` contract fixtures，升级协议需单独 PRD / migration |
| 本地取消未传播到远端 | 远端任务继续消耗资源或产生副作用 | cancellation signal 必须进入 MCP executor，`CancelledError` 路径也必须 remote cancel / cleanup |
| 前端暴露 raw MCP id | 任务枚举或敏感关联泄露 | 前端事件只暴露 safe ref / fingerprint，raw id 仅受控 registry 可见 |

## 18. 已冻结决策与显式假设

1. **协议版本冻结**：本 PRD 按 MCP `2025-11-25` 设计；Tasks 是该版本引入且仍标注为 experimental 的能力，后续协议升级必须单独评审。
2. **生产恢复冻结**：生产 `long_task.enabled=true` / enforce 必须有 durable registry 或等价 sidecar 状态能力；in-memory registry 不是最终交付能力。
3. **依赖策略冻结**：默认优先使用现有 `httpx` streaming 与内部 `SSEEventParser`；如实现阶段决定引入 `httpx-sse` 或其他依赖，必须同步更新 `requirements.txt`、README / 本 PRD，并补供应链风险说明。
4. **前端契约冻结**：前端只消费本项目 API/SSE 事件，不直接处理 MCP 原始 SSE、session id、task id、event id 或 progress token。
5. **server-to-client 能力冻结**：首版只支持 `ping`；sampling、elicitation、roots、其它 server request 默认拒绝并审计；`input_required` 在未实现 elicitation / Interrupt bridge 前稳定失败，后续启用必须另开 PRD。
6. **tool negotiation 冻结**：是否 task-augment 由 server `capabilities.tasks`、tool-level `execution.taskSupport` 与本项目 tool config 三者共同决定；tool 未声明 `execution.taskSupport` 时按 `forbidden` 处理；`required` 配置遇到任一方禁止必须 fail closed，`preferred` 只允许降级普通 `tools/call`。
7. **安全边界冻结**：LLM / Planner / Skill 不能动态指定 MCP endpoint、token、header、server id、tool name 或 long-task 策略。

## 19. 参考资料

- MCP 2025-11-25 Transport：https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- MCP 2025-11-25 Tasks：https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks
- MCP 2025-11-25 Progress：https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress
- MCP 2025-11-25 Cancellation：https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation
- MCP 2025-11-25 Schema Reference：https://modelcontextprotocol.io/specification/2025-11-25/schema
- 既有 MCP Runtime PRD：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`
