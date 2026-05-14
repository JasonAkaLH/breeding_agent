# Phase 4：API 事件桥接、取消传播与 Executor 集成 PRD

- **范围**：MCPToolExecutor facade / ApiRuntime / EventBridge / API SSE / cancellation / final result mapping
- **状态**：待实现
- **日期**：2026-05-14
- **前置依赖**：Phase 3

## 1. 目标

Phase 4 把 Rust sidecar 的 MCP 长任务能力接入本项目现有 task / node / API/SSE 体系，让业务用户能在前端看到长任务开始、进度、状态、重连、取消、失败和完成。

## 2. 功能需求

1. `MCPToolExecutor` 通过 Python sidecar client 调用 Rust sidecar，保留 `ExecutorPort` 与 `CapabilityExecutionResult` 对外契约。
2. MCP executor 必须接入 live event recorder，执行期间实时写入 `mcp.long_task_*` 事件。
3. live event 写入必须同时完成 storage append 与 event broker publish。
4. API/SSE 订阅端必须在最终结果前收到 progress / status / cancellation event。
5. final result 必须继续走 output schema validation、sanitizer、size limit、artifact / text payload 映射。
6. 本地 `cancel_task` 必须向正在运行的 MCP node 传播 cancellation signal。
7. 普通 in-flight request 无 MCP task id 时发送 `notifications/cancelled`。
8. task-augmented request 已有 MCP task id 时发送 `tasks/cancel`。
9. `initialize` request 不进入用户任务取消传播；初始化失败按 lifecycle / transport error 处理，不能发送无意义 cancellation。
10. `notifications/cancelled` 是 notification，不应期待 JSON-RPC response；如果 notification POST 返回非 202 / transport error，只能写脱敏 audit 并按本地 cancel 继续收口。
11. `tasks/cancel` 是 request，必须等待 response 或 deadline；deadline 耗尽后本地任务仍进入 cancel requested / cancelled 收口，并写 remote cancel uncertain audit。
12. 本地 asyncio task 被 cancel 时，executor 的 `CancelledError` 路径也必须 remote cancel / cleanup。
13. 迟到 success response 不得覆盖本地 cancelled / failed terminal result，只能进脱敏 audit。
14. 前端事件 payload 不得包含 raw `mcp_task_id`、session id、Last-Event-ID、progressToken、request id、完整工具参数或完整输出。
15. `input_required` 未启用前必须作为稳定失败事件进入 API/SSE，不得无限等待。
16. API/SSE event payload 的 `safe_ref` 必须由 Rust sidecar registry 生成或校验，Python 不得从 raw MCP id 可逆拼接。

## 3. API/SSE 事件契约

| event_type | 前端可见字段 |
|---|---|
| `mcp.long_task_started` | `server_id`、`tool_name`、`capability_id`、`safe_ref` |
| `mcp.long_task_progress` | `server_id`、`tool_name`、`progress`、`total`、`message`、`safe_ref` |
| `mcp.long_task_status` | `status`、`status_message`、`safe_ref` |
| `mcp.long_task_reconnected` | `safe_ref`、`attempt`、`duration_ms` |
| `mcp.long_task_cancel_requested` | `safe_ref`、`reason` |
| `mcp.long_task_cancelled` | `safe_ref`、`status` |
| `mcp.long_task_failed` | `safe_ref`、`error_code`、`retriable` |
| `mcp.long_task_completed` | `safe_ref`、`duration_ms`、`output_size_bytes`、`truncated` |

所有前端可见 MCP event 必须满足：`event_type` 使用本项目命名空间，`payload` 只含 sanitized / safe fields，`created_at` 由平台 runtime 生成，raw MCP `_meta` 不直接透传。

## 4. Python / Rust 分工

| 责任 | Python | Rust sidecar |
|---|---|---|
| capability descriptor | 负责 | 提供 tool binding metadata |
| MCP protocol / transport | 不做 canonical | 负责 |
| task registry | 只读 facade / safe ref | 负责 durable state |
| live event 发布 | 负责写入平台 event store / broker | 输出 sanitized event envelope |
| final result sanitizer | 不绕过 Rust 结果，可做 facade 校验 | 负责 canonical sanitizer |
| cancellation | 发起并映射平台 task 状态 | 负责 remote cancel protocol |

## 5. 测试策略

| 层级 | 测试 |
|---|---|
| Python unit | MCPToolExecutor sidecar client success / error / cancel mapping |
| API test | `/api/v1/tasks/{task_id}/events` 在 final result 前收到 MCP event |
| E2E | user task 启动 MCP long task、进度、完成、取消 |
| Fault injection | sidecar unavailable、event storage failure、late response、cancel race |
| Redaction | raw id / secret / full args / full output 不进入 frontend event |

## 6. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-P4-AC-001 | MCPToolExecutor 使用 sidecar 后对外 contract 不变 | capability executor tests |
| MCP-P4-AC-002 | progress / status 在 final result 前进入 API/SSE | API timing test |
| MCP-P4-AC-003 | 本地取消传播为 `notifications/cancelled` 或 `tasks/cancel` | cancellation integration test |
| MCP-P4-AC-004 | late response 不覆盖 terminal 本地状态 | race tests |
| MCP-P4-AC-005 | final result 通过 schema / sanitizer / size limit | executor tests |
| MCP-P4-AC-006 | 前端事件无敏感原文 | redaction snapshot |
| MCP-P4-AC-007 | cancellation notification 与 tasks/cancel 分支符合 MCP 标准响应语义 | cancellation protocol tests |
| MCP-P4-AC-008 | `safe_ref` 不可逆且由受控 registry 生成 / 校验 | security tests |

## 7. 退出门禁

Phase 4 通过后，可以进入 shadow 验证。仍不得直接进入生产 enforce；enforce 必须通过 Phase 5。
