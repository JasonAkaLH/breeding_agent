# PRD-2：2024 Legacy HTTP+SSE 完整协议支持

- **状态**：已实现（仓库内，待提交）
- **日期**：2026-05-19
- **范围**：`2024-11-05` legacy HTTP+SSE persistent reader、POST endpoint、request id correlation、fixtures、conformance
- **依赖**：PRD-1 MCP Server Config 与 Adapter Contract
- **后续依赖方**：PRD-3 Official Rust SDK Adapter shadow、PRD-4 四版本 Conformance Gate

## 1. 问题陈述

`2024-11-05` legacy HTTP+SSE 的合法 client 行为包括：client 持续连接 SSE endpoint，server 通过 `endpoint` event 告知 POST message endpoint，client 把 JSON-RPC request POST 到 message endpoint，而 JSON-RPC response 可以通过原 SSE stream 的 `message` event 返回。当前实现如果关闭原 SSE stream 并只期待 POST body 返回 JSON-RPC response，就无法覆盖该合法协议形态。

本 PRD 补齐该协议缺口，但不得为任何真实测试 server 写特调逻辑。


## 1.1 实施前状态与证据

| 证据 | 当前事实 | 对本 PRD 的影响 |
|---|---|---|
| `src/integrations/mcp/transport_legacy_http_sse.py` | 已有基础 `LegacyHTTPSSETransport`，能 GET SSE endpoint、解析 `endpoint` event、POST JSON-RPC object。 | 本 PRD 不是从零新增 transport，而是升级为持久 SSE reader + response correlation。 |
| `src/integrations/mcp/transport_legacy_http_sse.py` | 当前 `_ensure_post_endpoint()` 读取 endpoint 后退出 SSE stream；`send()` 仍主要期待 POST response body。 | 必须修正为 session 生命周期内保持原 SSE stream，并能从该 stream 接收 response。 |
| `src/integrations/mcp/transport_legacy_http_sse.py::_validate_post_endpoint` | 当前 remote HTTP POST endpoint 仍被 localhost gate 拒绝。 | 必须与 PRD-1 的 `plaintext_http` 安全模式保持一致。 |
| `src/integrations/mcp/client.py` | `send_request()` 依赖 request id 匹配；`send_notification()` 不要求 response。 | legacy transport 必须区分 request 与 notification：request 等待 id response，notification 允许 202/204/empty response 或只完成 POST。 |
| `tests/integrations/mcp/test_legacy_http_sse_transport.py` | 已有 endpoint/POST body/SSE body 等基础回归。 | 新测试必须新增 original persistent SSE stream response path，不删除既有回归。 |

## 1.2 当前实现证据

| 文件 / 命令 | 证据 |
|---|---|
| `src/integrations/mcp/transport_legacy_http_sse.py` | 已升级为 session 生命周期内持久 SSE reader：首次 GET 读取并保持 SSE stream，`endpoint` event 建立 POST endpoint，request 通过 pending map 按 JSON-RPC id correlation 等待原 SSE `message` response；POST body JSON/SSE response 兼容路径保留。 |
| `src/integrations/mcp/transport_legacy_http_sse.py` | notification / client response POST 在 202/204/empty response 下成功返回，不要求 JSON-RPC response；request timeout 返回稳定 `legacy_response_timeout` 并清理 pending map；`close()` 取消 reader task、关闭 pending futures 与 owned HTTP client。 |
| `src/integrations/mcp/transport_legacy_http_sse.py` | same-origin / scheme / default-port validation 保留；diagnostics 只暴露 `endpoint_fingerprint` / status 等安全字段，不记录 raw endpoint query、header value 或 JSON-RPC body。 |
| `tests/integrations/mcp/test_legacy_http_sse_persistent_reader.py` | 新增持久 SSE fake server，覆盖 initialize、initialized notification 204、tools/list、tools/call、并发 request id correlation、unknown id ignore、timeout cleanup、close cleanup。 |
| `tests/integrations/mcp/test_legacy_http_sse_transport.py` | 既有 endpoint/POST body/SSE body/invalid endpoint/batch/default port 回归保留，并纳入 persistent response fixture。 |
| `tests/integrations/mcp/test_2024_legacy_runtime_discovery.py` | runtime discovery fake server 已改为 original SSE stream response 路径，证明 public tool 注册与 `state.call_tool()` 在完整 legacy 形态下可用。 |
| `tests/fixtures/mcp/transports/2024-11-05/legacy_http_sse_persistent_response.json` | 新增 2024 legacy POST 202 + original SSE `message` response fixture。 |
| `conda run -n multi_agent python -m unittest tests.integrations.mcp.test_legacy_http_sse_persistent_reader tests.integrations.mcp.test_legacy_http_sse_transport tests.integrations.mcp.test_2024_legacy_runtime_discovery` | 17 个 targeted legacy tests 通过。 |
| `conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'` | 108 个 MCP integration tests 通过。 |
| `conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'` | 212 个 integration tests 通过。 |

## 2. 目标

1. 实现 session 生命周期内持久 SSE reader。
2. 解析 `endpoint` event 并保存 POST message endpoint。
3. JSON-RPC request 通过 POST endpoint 发送。
4. JSON-RPC response 支持从原 SSE stream 的 `message` event 返回；POST body response 兼容路径仍保留。
5. 通过 JSON-RPC request id correlation 匹配并发/串行 request response。
6. 覆盖 initialize、initialized、tools/list、ordinary tools/call。
7. 保持 same-origin、redirect、scheme、timeout、redaction 与 plaintext_http guard。
8. 建立 repo-local fake server / fixtures，证明 2024 legacy 完整协议形态。

## 3. 非目标

1. 不实现 MCP server。
2. 不实现 server-specific 逻辑；`mcp_test.json` 仅作为非规范 smoke sample。
3. 不支持 JSON-RPC batch 作为 client request 形态。
4. 不承诺 2024 SSE `Last-Event-ID` 完整恢复；如需恢复，只能作为后续 enhancement。
5. 不实现 resources/prompts/sampling/roots/elicitation 的业务接入。
6. 不把 2024 legacy transport 逻辑混入 2025+ Streamable HTTP 状态机。

## 4. 协议数据流

```text
1. GET configured SSE endpoint
2. 保持 SSE stream 打开
3. 读取 event: endpoint
4. validate POST endpoint same-origin / scheme / redirect policy
5. POST JSON-RPC request 到 message endpoint
6. 原 SSE stream 收 event: message
7. 按 JSON-RPC id 唤醒 pending request
8. 返回归一化 response 给 MCPClientAdapter
```

## 5. 功能需求

| ID | Requirement | Priority |
|---|---|---|
| MCP-SDK-2-FR-001 | `legacy_http_sse` 仅允许与 `2024-11-05` 配对。 | P0 |
| MCP-SDK-2-FR-002 | transport 必须在 session 生命周期内保持 SSE reader。 | P0 |
| MCP-SDK-2-FR-003 | transport 必须解析 `endpoint` event，建立 POST message endpoint。 | P0 |
| MCP-SDK-2-FR-004 | POST endpoint 必须通过 same-origin / scheme / redirect / plaintext guard。 | P0 |
| MCP-SDK-2-FR-005 | request 必须以 JSON-RPC object POST 到 message endpoint。 | P0 |
| MCP-SDK-2-FR-006 | response 可以来自 POST body 或原 SSE stream；原 SSE stream response 为必测路径。 | P0 |
| MCP-SDK-2-FR-007 | transport 必须用 JSON-RPC id correlation 匹配 response。 | P0 |
| MCP-SDK-2-FR-008 | pending request 超时必须清理，不得泄露 future/task。 | P0 |
| MCP-SDK-2-FR-009 | close 必须关闭 SSE reader、HTTP session 与 pending requests。 | P0 |
| MCP-SDK-2-FR-010 | diagnostics 不得记录 raw endpoint query、header value、request/response body。 | P0 |
| MCP-SDK-2-FR-011 | fake server 必须覆盖 initialize、initialized、tools/list、tools/call。 | P0 |
| MCP-SDK-2-FR-012 | batch request 必须拒绝并给稳定 error code。 | P1 |
| MCP-SDK-2-FR-013 | notification POST 成功后不得要求 JSON-RPC response；若 stream 同时返回 server request/notification，仍按 client 现有 unsupported request 策略处理。 | P0 |
| MCP-SDK-2-FR-014 | persistent SSE reader 必须具备 ready/endpoint-established 状态，避免每个 request 重复打开 SSE stream。 | P0 |

## 6. 非功能需求

| 类型 | Requirement |
|---|---|
| 安全 | endpoint event 不能让 client 跳到不同 origin；HTTP 下继续标记 `plaintext_http`。 |
| 稳定性 | SSE reader、POST request、response wait、close 均受 timeout 管理。 |
| 并发 | 至少支持多个 pending request 的 id correlation；若首版串行化，也必须在 contract 中明确互斥与错误行为。 |
| 可维护性 | legacy transport 内聚，不复用/污染 Streamable HTTP session header 状态机。 |
| 可观测 | error reason 必须稳定：`legacy_endpoint_missing`、`legacy_endpoint_invalid`、`legacy_sse_read_failed`、`legacy_response_timeout` 等。 |

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| SSE connect 失败 | optional server skip / required server fail closed；reason `legacy_sse_connect_failed`。 |
| endpoint event 缺失或超时 | fail closed；reason `legacy_endpoint_missing`。 |
| endpoint URL 跨 origin、非法或违反 plaintext guard | fail closed；reason `legacy_endpoint_invalid`。 |
| POST message endpoint 返回 HTTP error | fail closed；reason `legacy_post_failed`。 |
| SSE message event 非 JSON-RPC object | protocol error；脱敏记录 reason。 |
| response id 未匹配 pending request | protocol diagnostic；不得唤醒错误 request。 |
| pending request 超时 | timeout error，清理 pending map。 |
| close 时仍有 pending request | 全部以 cancellation/close error 完成。 |

## 8. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-SDK-2-AC-001 | 2024 fake server 可完成 initialize + initialized。 | integration test |
| MCP-SDK-2-AC-002 | 2024 fake server 可完成 tools/list。 | integration test |
| MCP-SDK-2-AC-003 | 2024 fake server 可完成 ordinary tools/call。 | integration test |
| MCP-SDK-2-AC-004 | response 从原 SSE stream 返回时可按 id 匹配。 | transport test |
| MCP-SDK-2-AC-005 | POST body response 兼容路径仍可用。 | regression test |
| MCP-SDK-2-AC-006 | endpoint event 缺失、非法、跨 origin 均 fail closed。 | negative tests |
| MCP-SDK-2-AC-007 | HTTP 明文路径记录 `plaintext_http` 且不拒绝合法配置。 | security test |
| MCP-SDK-2-AC-008 | header value、raw endpoint query 不进入 diagnostics。 | redaction test |
| MCP-SDK-2-AC-009 | request timeout 后 pending map 清理。 | lifecycle test |
| MCP-SDK-2-AC-010 | `mcp_test.json` 类真实 server 仅作为 external smoke，不作为默认 CI 或规范 gate。 | smoke script / docs |
| MCP-SDK-2-AC-011 | initialized notification 在 POST 204/empty response 下成功，不触发 `expected JSON-RPC response` 错误。 | transport/client test |
| MCP-SDK-2-AC-012 | close/cancel 后 SSE reader task、pending futures 与 HTTP client 全部清理。 | lifecycle test |

## 9. 测试计划

- `tests/integrations/mcp/test_legacy_http_sse_persistent_reader.py`
- `tests/integrations/mcp/test_2024_legacy_runtime_discovery.py`
- `tests/integrations/mcp/test_mcp_plaintext_http_security.py`
- `tests/fixtures/mcp/transports/2024-11-05/legacy_http_sse_persistent_response.json`
- fake server 覆盖 original SSE stream response 与 POST body response 两种路径

真实 server smoke 可由后续脚本执行：

```bash
python scripts/smoke_mcp_server_config.py --config mcp_server_config.json
```

## 10. 风险与处理

| 风险 | 处理 |
|---|---|
| 持久 SSE reader 引入 task lifecycle 泄漏。 | close/cancel/timeout 必须覆盖 pending request 与 reader task 清理。 |
| 并发 request id correlation 复杂度增加。 | 先以 pending map + per-request future 实现，测试超时、未知 id、重复 id。 |
| fake server 与真实 server 行为差异。 | fake server 覆盖协议合法形态；真实 server 仅作为非规范 smoke sample 补充。 |
| endpoint query 可能含敏感信息。 | diagnostics 只记录 fingerprint，不记录 raw URL/query。 |

## 11. 参考

- MCP `2024-11-05` 规范：https://modelcontextprotocol.io/specification/2024-11-05/basic/index
- 设计文档：`docs/superpowers/specs/2026-05-19-mcp-official-sdk-client-compatibility-design.md`
