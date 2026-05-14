# Phase 2：Streamable HTTP 与 SSE 内核 PRD

- **范围**：Rust MCP Streamable HTTP / SSE parser / message router / request tracker / GET stream / reconnect
- **状态**：待实现
- **日期**：2026-05-14
- **前置依赖**：Phase 1

## 1. 目标

Phase 2 在 Rust MCP sidecar 内实现完整 Streamable HTTP 与 SSE 协议内核，替代当前 Python 只解析首个 SSE data 的能力缺口。

## 2. 功能需求

1. HTTP POST JSON-RPC request 必须声明 `Accept: application/json, text/event-stream` 与 `Content-Type: application/json`。
2. HTTP POST JSON-RPC notification 或 response 必须按 MCP Streamable HTTP 发送；server 正常接收时应返回 HTTP 202 no body，client 不得期待 JSON body。
3. 支持普通 `application/json` response。
4. 支持 `text/event-stream` 增量读取，不等待完整 response body。
5. SSE parser 支持多行 `data:`、comment / heartbeat、`id`、`event`、`retry`、空 data priming event。
6. 每个非空 SSE data 内 JSON 必须先通过 JSON-RPC schema / contract 校验；空 data priming event 只更新 stream 状态，不进入 message router。
7. message router 支持 response、notification、request、invalid 四类消息。
8. request tracker 维护 request id、deadline、cancel token、progress token、last_event_id、retry state、pending future。
9. request id 在同一 MCP session 内不得复用；JSON-RPC batch array 必须 fail closed。
10. 支持 HTTP GET 打开 server-to-client SSE stream，GET 必须声明 `Accept: text/event-stream`；server 返回 405 时视为“不提供独立监听流”，不是系统错误。
11. 支持断线后按 `retry` 与 HTTP GET + `Last-Event-ID` 恢复；不得重新 POST 原始 `tools/call`。
12. server replay 只能恢复原 stream 未送达 message；client 必须按 stream id / event id 去重，不得把其它 stream 的 response 错配给当前 request。
13. 支持 server-to-client `ping` request，并通过新的 HTTP POST 发回 JSON-RPC response；response POST 正常应收到 202 no body。
14. 未实现 server-to-client request 返回 method-not-found / unsupported，并写脱敏 audit。
15. 初始化后如果 server 返回 `MCP-Session-Id`，后续 POST / GET / DELETE 必须携带；session 请求收到 404 时必须重新 initialize。
16. 支持可选 HTTP DELETE session shutdown；server 返回 405 时视为不支持客户端主动终止 session。
17. 所有 stream、event、queue、deadline、bytes 必须有上限。

## 3. 非目标

1. 不实现 MCP Tasks durable registry。
2. 不实现 API/SSE 用户可见事件桥接。
3. 不启用 `tasks` client capability，也不因 Phase 2 支持 SSE 而声明未实现的 roots、sampling、elicitation。
4. 不支持 sampling、roots、elicitation。

## 4. 资源限制

| 项 | 默认值 | 说明 |
|---|---|---|
| SSE event max size | 256KB | 单 event 超限 fail closed |
| stream total cap | 32MB | 单次调用累计 message 上限 |
| stream idle timeout | 90s | 长任务配置可覆盖，但必须有 hard cap |
| reconnect max attempts | 20 | 受 server retry 与本地上限共同约束 |
| reconnect max interval | 30s | 防止无限慢性挂起 |
| pending request max | 按 server / runtime 限流 | 不允许无界 pending future |

## 5. 错误码

至少输出以下稳定 typed error：

- `mcp_runtime_sse_event_too_large`
- `mcp_runtime_sse_invalid_json`
- `mcp_runtime_jsonrpc_invalid_message`
- `mcp_runtime_response_id_mismatch`
- `mcp_runtime_stream_idle_timeout`
- `mcp_runtime_reconnect_exhausted`
- `mcp_runtime_server_request_unsupported`
- `mcp_runtime_jsonrpc_batch_unsupported`
- `mcp_runtime_session_expired`

## 6. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | SSE parser、JSON-RPC router、request tracker、retry state |
| Rust fuzz | malformed SSE、large event、invalid JSON、random JSON-RPC fields |
| Integration | fake MCP server multi-event POST、GET stream、ping、unsupported request、reconnect |
| Python regression | existing MCP short call JSON response 与单条 SSE response 不回归 |
| Conformance | POST notification / response 202、GET 405、DELETE 405、session 404 reinitialize、batch array rejection |

## 7. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-P2-AC-001 | POST SSE stream 可增量处理多条 JSON-RPC message | integration test |
| MCP-P2-AC-002 | GET stream 可接收 server-to-client request / notification | integration test |
| MCP-P2-AC-003 | 断线后使用 Last-Event-ID 恢复且不重复 POST tool call | reconnect test |
| MCP-P2-AC-004 | ping 可响应，unsupported request 被标准拒绝 | server-to-client tests |
| MCP-P2-AC-005 | malformed / oversized event fail closed | fuzz + fault injection |
| MCP-P2-AC-006 | 短调用行为不回归 | Python regression |
| MCP-P2-AC-007 | POST notification / response、GET、DELETE 与 session 行为符合 MCP Streamable HTTP | conformance tests |
| MCP-P2-AC-008 | JSON-RPC batch array 被拒绝且不进入业务 router | protocol negative tests |

## 8. 退出门禁

Phase 2 通过后，可以宣称 Rust sidecar 具备 Streamable HTTP / SSE 协议内核，但不得宣称完整 MCP 长任务可用。
