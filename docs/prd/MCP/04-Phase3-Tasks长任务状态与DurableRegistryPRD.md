# Phase 3：Tasks 长任务状态与 Durable Registry PRD

- **范围**：MCP Tasks / task-augmented tools/call / tasks/get/result/list/cancel / durable registry / recovery
- **状态**：待实现
- **日期**：2026-05-14
- **前置依赖**：Phase 2

## 1. 目标

Phase 3 在 Rust MCP sidecar 内实现 MCP Tasks 与生产级 durable long-task registry，使长任务可以跨 stream 断线和 runtime 重启恢复可判定状态。

## 2. 功能需求

1. 初始化时读取 server `capabilities.tasks`。
2. `tools/list` 时读取 tool-level `execution.taskSupport`，支持 `required`、`optional`、`forbidden`；未声明时按 `forbidden` 处理，不得推断为 optional。
3. 按 server `capabilities.tasks.requests.tools.call`、tool-level support 与本项目 config 三者共同决定是否发送 task-augmented `tools/call`。
4. 支持 `task_augmented_mode=required|preferred|disabled`。
5. task-augmented `tools/call` 必须携带 `task.ttl` 与 `_meta.progressToken`。
6. 支持 `CreateTaskResult` 解析、registry 写入、status polling、result retrieval。
7. 支持 `tasks/get`，按 server `pollInterval` 与本项目上限轮询直到 terminal 或超时 / 取消。
8. 支持 `tasks/result`；非 terminal 时允许阻塞 / SSE stream，但必须受 deadline、cancel 和 polling 策略约束。
9. 支持 `tasks/list` 作为诊断 / 恢复能力，必须使用 cursor-based pagination。
10. 支持 `tasks/cancel`，terminal task cancellation 被拒绝时要按 MCP error 映射。
11. `notifications/progress` 必须按 progress token 关联 task，progress 数值单调递增。
12. `notifications/tasks/status` 是可选通知，不能作为唯一状态来源。
13. `input_required` 首版映射为稳定错误 `mcp_task_input_required_unsupported`，不得挂起或直接驱动用户输入。
14. terminal 状态不可逆：`completed`、`failed`、`cancelled` 后不得被 replay 的 `working` 覆盖。
15. 本项目作为 client 调用 server-side `tools/call` 时，不需要也不得声明 client-side `capabilities.tasks`；只有接收 server task-augmented sampling / elicitation / roots 时才涉及 client tasks capability，本联合改造首版不启用。
16. task 相关请求、通知、响应必须按 MCP 规则携带或省略 `_meta["io.modelcontextprotocol/related-task"]`；普通 task 相关消息需要关联 metadata，但 `tasks/get`、`tasks/result`、`tasks/cancel` 请求以 `taskId` 参数为准，不应额外依赖 related-task metadata。
17. `tasks/get`、`tasks/list`、`tasks/cancel` 的 result 不应携带 related-task metadata；`notifications/tasks/status` 不应携带 related-task metadata；`tasks/result` 的 result 必须携带 related-task metadata。
18. progress token 必须为 string 或 integer，并在 active task / request 生命周期内唯一；token 不得进入前端原文。

## 3. Task negotiation 矩阵

| server `tasks.requests.tools.call` | tool `execution.taskSupport` | 本项目 `task_augmented_mode` | 行为 |
|---|---|---|---|
| absent | any | `required` | 不公开 capability / fail closed |
| absent | any | `preferred` / `disabled` | 普通 `tools/call`，记录不支持 task augmentation 诊断 |
| present | `required` | `disabled` | 不公开 capability / fail closed |
| present | `required` | `required` / `preferred` | 必须 task-augmented |
| present | `optional` | `required` | task-augmented；失败时 fail closed |
| present | `optional` | `preferred` | 优先 task-augmented；未发起前 negotiation 不满足时允许降级并审计 |
| present | `optional` | `disabled` | 普通 `tools/call` |
| present | `forbidden` / 未声明 | `required` | 不公开 capability / fail closed |
| present | `forbidden` / 未声明 | `preferred` / `disabled` | 普通 `tools/call`；不得 task-augment |

## 4. Durable registry 字段

| 字段 | 说明 |
|---|---|
| `conversation_id` | 本项目会话 id |
| `task_id` | 本项目 task id |
| `node_id` | 本项目 node id |
| `capability_id` | `mcp.*` capability id |
| `server_id` | MCP server id |
| `tool_name` | MCP tool name |
| `mcp_request_id` | JSON-RPC request id |
| `mcp_task_id` | MCP task id |
| `progress_token` | MCP progress token |
| `session_id` | MCP session id |
| `last_event_id` | 最近处理的 SSE event id |
| `status` | normalized long-task status |
| `created_at` / `updated_at` / `finished_at` | 时间字段 |
| `ttl_ms` / `expires_at` | retention / cleanup |
| `safe_ref` | 前端可见关联引用，不含 raw id |

## 5. 持久化要求

1. in-memory registry 只允许 unit test / local dev / shadow 验证，不得通过生产 enforce 验收。
2. registry 必须可 migration、backup、restore、replay 校验。
3. raw MCP task id、session id、event id、progress token 不得进入前端事件。
4. cleanup 必须遵守 MCP task ttl、本项目 retention 与 audit 要求。
5. registry 写失败时，生产长任务必须 fail closed；不得假装支持恢复。

## 6. 错误码

至少输出以下稳定 typed error：

- `mcp_runtime_task_unsupported`
- `mcp_runtime_task_support_forbidden`
- `mcp_runtime_task_required_unavailable`
- `mcp_runtime_task_result_unavailable`
- `mcp_runtime_task_input_required_unsupported`
- `mcp_runtime_task_registry_unavailable`
- `mcp_runtime_task_cancel_failed`
- `mcp_runtime_task_terminal_cannot_cancel`
- `mcp_runtime_task_related_metadata_invalid`
- `mcp_runtime_progress_token_invalid`

## 7. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | capability negotiation、task status transition、registry idempotency |
| Integration | CreateTaskResult、tasks/get polling、tasks/result、tasks/cancel、tasks/list pagination |
| Recovery | stream disconnect、sidecar restart、registry restore 后继续 get/result |
| Security | raw id 不进 frontend event / normal audit、side-effecting tool 不重复 call |
| Fault injection | registry unavailable、task expired、terminal replay、input_required unsupported |
| Conformance | related-task metadata、task support matrix、progress token type / monotonicity |

## 8. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-P3-AC-001 | capability + tool-level negotiation 正确控制 task-augmented call | negotiation matrix tests |
| MCP-P3-AC-002 | `tools/call` 可返回 CreateTaskResult 并进入 registry | integration test |
| MCP-P3-AC-003 | `tasks/get` / `tasks/result` 可获取 terminal final result | integration test |
| MCP-P3-AC-004 | `tasks/cancel` 可取消 task 并保持 terminal 不可逆 | cancellation tests |
| MCP-P3-AC-005 | sidecar restart 后可恢复 task 状态判定 | restart recovery test |
| MCP-P3-AC-006 | registry unavailable 时生产长任务 fail closed | fault injection |
| MCP-P3-AC-007 | `input_required` 稳定失败且不越权 | unsupported input test |
| MCP-P3-AC-008 | related-task metadata 与 tasks/get/result/cancel 参数规则符合 MCP 规范 | conformance tests |
| MCP-P3-AC-009 | 本项目不错误声明 client-side `capabilities.tasks` | initialize capability snapshot |
| MCP-P3-AC-010 | task negotiation 矩阵全部覆盖 | matrix tests |

## 9. 退出门禁

Phase 3 通过后，可以宣称 Rust sidecar 具备 MCP long-task 状态治理内核。仍不得宣称用户前端已完整可见，除非 Phase 4 通过。
