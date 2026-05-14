# Phase 0：协议契约、夹具与验收基线 PRD

- **范围**：MCP 2025-11-25 fixtures / fake MCP server / Python-Rust contract / typed error / event schema / TDD baseline
- **状态**：待实现
- **日期**：2026-05-14
- **前置依赖**：PRD 14、PRD 17、Rust MCP sidecar PRD 已冻结

## 1. 目标

Phase 0 的目标是先把联合改造的测试与契约基线固定下来，再进入运行时代码实现。没有 Phase 0，不允许开始实现 Rust sidecar 主链路。

## 2. 功能范围

1. 固定 MCP 2025-11-25 相关 JSON fixtures：initialize、notifications/initialized、tools/list、tools/call、progress、tasks、cancel、ping、JSON-RPC error、SSE event。
2. fixtures 必须覆盖 Streamable HTTP POST 的三种合法 body：JSON-RPC request、notification、response；必须覆盖 notification / response POST 被 server 接收后的 HTTP 202 no body。
3. fixtures 必须覆盖非法 JSON-RPC batch array；本项目按 MCP Streamable HTTP 规则 fail closed。
4. 建立 fake MCP server：支持普通 JSON response、单条 SSE、多事件 SSE、空 data priming event、断线重连、GET stream、GET 405、session 404、DELETE session 405、tasks/get、tasks/result、tasks/list、tasks/cancel、progress flooding、malformed event。
5. fake server 必须校验 `MCP-Protocol-Version`、`MCP-Session-Id`、`Last-Event-ID`、POST / GET `Accept` header 与 POST `Content-Type`。
6. 固定 Python ↔ Rust MCP sidecar protobuf 初版 contract 草案：health、readiness、version、list_tools、call_tool、cancel、stream event、task state、typed error。
7. 固定 `mcp_runtime_` typed error 前缀、错误码表与 Python `CapabilityExecutionError` 映射表。
8. 固定 API/SSE 前端可见事件 payload schema：`mcp.long_task_started`、`mcp.long_task_progress`、`mcp.long_task_status`、`mcp.long_task_reconnected`、`mcp.long_task_cancel_requested`、`mcp.long_task_cancelled`、`mcp.long_task_failed`、`mcp.long_task_completed`。
9. 固定 redaction snapshot：token、Authorization、raw endpoint、raw task id、session id、Last-Event-ID、progressToken、完整参数、完整输出不得泄露。

## 3. 非目标

1. 不实现生产 sidecar。
2. 不实现完整 streaming transport。
3. 不公开任何新 MCP capability 给 Planner。
4. 不改变现有 Python MCP 短调用行为。

## 4. 测试先行要求

Phase 0 结束时，以下测试可以先失败，但必须存在且语义明确：

| 测试组 | 必须覆盖 |
|---|---|
| protocol fixtures | JSON-RPC 2.0 request / response / notification / error、MCP protocol version、session header |
| Streamable HTTP fixtures | POST request / notification / response、HTTP 202 no body、GET 405、session 404 reinitialize、DELETE session 405 |
| SSE fixtures | multi-line data、empty data priming event、comment、heartbeat、id、retry、Last-Event-ID resume、malformed JSON、oversized event |
| tasks fixtures | CreateTaskResult、Task status lifecycle、tasks/get/result/list/cancel、related-task metadata、tool 未声明 taskSupport 按 forbidden、terminal state replay |
| server-to-client | ping success、unsupported request fail closed |
| redaction | raw id / secret / endpoint / token 不进入 frontend event 和普通 audit |
| compatibility | proto schema hash、error table hash、supported_features、client version range |

## 5. 交付物

1. `tests/fixtures/mcp/` 或等价测试夹具目录。
2. fake MCP streaming server 测试工具。
3. Python-Rust proto / contract 草案文档或 schema artifact 草案。
4. typed error code table 草案。
5. API/SSE event payload schema 草案。
6. Phase 1-5 共用验收矩阵。

## 6. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-P0-AC-001 | fixtures 覆盖 PRD17 与 Rust MCP PRD 的关键协议行为 | fixture review + tests listed |
| MCP-P0-AC-002 | fake MCP server 可表达 multi-event、reconnect、tasks、cancel、malformed 场景 | fake server smoke tests |
| MCP-P0-AC-003 | Python ↔ Rust sidecar contract 字段足以承载 PRD17 long task 行为 | contract review |
| MCP-P0-AC-004 | redaction snapshot 覆盖所有敏感字段 | snapshot tests |
| MCP-P0-AC-005 | 后续 Phase 的失败测试入口已建立 | test discovery output |
| MCP-P0-AC-006 | MCP 标准一致性 fixtures 覆盖 POST / GET / session / resume / task metadata | conformance fixture review |

## 7. 退出门禁

Phase 0 通过后，只能说明“验收基线已具备”。不得宣称 MCP 长任务运行时可用。
