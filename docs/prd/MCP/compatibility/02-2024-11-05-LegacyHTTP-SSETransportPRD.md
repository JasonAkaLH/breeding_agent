# PRD-B：MCP 2024-11-05 Legacy HTTP+SSE Transport

- **状态**：待评审
- **日期**：2026-05-19
- **范围**：`2024-11-05` HTTP+SSE client transport / fixtures / fake server / ordinary tools integration
- **依赖**：PRD-A 协议版本与协商内核
- **非目标**：不改 2025+ Streamable HTTP；不实现 stdio sandbox；不实现 resources/prompts/tasks/interactive OAuth

## 1. 问题陈述

`2024-11-05` 的 HTTP transport 与 `2025-03-26+` Streamable HTTP 不是同一个连接模型。2024 legacy HTTP+SSE 需要 client 先连接 SSE endpoint，从 server-sent `endpoint` event 获得 POST endpoint，然后把 JSON-RPC object POST 到该 endpoint。当前仓库只有 Streamable HTTP 风格的 `StreamableHTTPTransport`，不能把 2024 legacy 行为硬塞进去。

## 2. 目标

1. 新增 `legacy_http_sse` transport family，仅允许 `2024-11-05` 使用。
2. 实现 `LegacyHTTPSSETransport`，支持 SSE endpoint connect、endpoint event parsing、POST endpoint 保存与 JSON-RPC object 发送。
3. 复用现有 JSON-RPC validation、SSE parser、timeout、auth header 与 redaction 规则。
4. 支持 `initialize`、`notifications/initialized`、`tools/list`、ordinary `tools/call` 的 2024 fake server 链路。
5. 建立 2024 contract fixtures 和 transport integration tests。

## 3. 非目标

1. 不支持 JSON-RPC batch。
2. 不承诺 2024 SSE `Last-Event-ID` 完整恢复；仅保存 safe metadata。
3. 不自动从 Streamable HTTP endpoint 探测 legacy endpoint。
4. 不允许 LLM、Planner 或用户消息指定 SSE endpoint、POST endpoint 或 auth。
5. 不实现 server-to-client roots/sampling/elicitation/tasks。

## 4. 用户、系统与影响面

| Actor / system | 影响 |
|---|---|
| MCP server config 作者 | 2024 HTTP server 必须配置 `protocol_version: "2024-11-05"` 和 `transport: "legacy_http_sse"`。 |
| MCP client runtime | 通过新 transport family 连接 legacy server。 |
| Security / audit | 必须避免记录 raw endpoint query、auth header、session/event id。 |
| Tests / fixtures | 新增 2024 legacy HTTP+SSE fixture 和 fake server。 |

## 5. 功能需求

| ID | Requirement | Priority |
|---|---|---|
| MCP-B-FR-001 | config validation 必须允许 `transport: legacy_http_sse` 仅与 `2024-11-05` 配对。 | P0 |
| MCP-B-FR-002 | `LegacyHTTPSSETransport` 必须连接配置中的 SSE endpoint。 | P0 |
| MCP-B-FR-003 | transport 必须从 SSE stream 的 `endpoint` event 中解析 server-provided POST endpoint。 | P0 |
| MCP-B-FR-004 | 在 POST endpoint 未建立前发送 JSON-RPC request 必须 fail closed。 | P0 |
| MCP-B-FR-005 | transport 必须把后续 JSON-RPC object POST 到 server-provided endpoint。 | P0 |
| MCP-B-FR-006 | transport 必须拒绝 JSON-RPC batch arrays。 | P0 |
| MCP-B-FR-007 | transport 必须支持普通 JSON-RPC response 与 SSE message event 响应路径。 | P0 |
| MCP-B-FR-008 | endpoint、auth、event id 诊断必须脱敏。 | P0 |
| MCP-B-FR-009 | fake 2024 server 必须覆盖 initialize、initialized、tools/list、tools/call、malformed endpoint event、missing endpoint event。 | P0 |
| MCP-B-FR-010 | 2024 server discovery 成功后应能注册 read-only public MCP tool descriptor。 | P1 |

## 6. 非功能需求

| 类型 | Requirement |
|---|---|
| 安全 | POST endpoint 来自 server event，必须经过 endpoint allowlist / scheme / host 校验，不得盲目信任。 |
| 稳定性 | SSE connect、endpoint event 等待与 POST 请求必须有独立 timeout。 |
| 兼容性 | 2024 legacy transport 不发送 `MCP-Protocol-Version` / `MCP-Session-Id` 作为协议必需 header。 |
| 可维护性 | legacy transport 只能复用通用 parser/helper，不继承 Streamable HTTP 状态机。 |
| 可观测 | 失败 diagnostic 使用 reason code：`legacy_sse_connect_failed`、`legacy_endpoint_missing`、`legacy_endpoint_invalid`、`legacy_post_failed`。 |

## 7. 配置示例

```yaml
mcp:
  servers:
    - server_id: legacy_crm
      enabled: true
      required: false
      protocol_version: "2024-11-05"
      transport: "legacy_http_sse"
      endpoint: "https://legacy.example.com/sse"
      auth:
        type: "bearer"
        token_env: "LEGACY_MCP_TOKEN"
```

## 8. 数据流

1. Runtime 读取 server config 并验证 `2024-11-05` + `legacy_http_sse` 配对。
2. `LegacyHTTPSSETransport` 连接 SSE endpoint。
3. transport 等待并解析 `endpoint` event。
4. POST endpoint 通过 endpoint allowlist、scheme 与 secret redaction 检查。
5. MCP client 发送 `initialize.params.protocolVersion = "2024-11-05"`。
6. server 返回 `InitializeResult.protocolVersion = "2024-11-05"`。
7. client 保存 negotiated session 并发送 `notifications/initialized`。
8. runtime 执行 `tools/list` discovery。
9. executor 执行 ordinary `tools/call`。

## 9. 错误处理

| 场景 | 行为 |
|---|---|
| SSE endpoint 连接失败 | optional skip / required fail，reason `legacy_sse_connect_failed`。 |
| endpoint event 缺失或超时 | optional skip / required fail，reason `legacy_endpoint_missing`。 |
| endpoint URL 不合法或不在 allowlist | fail closed，reason `legacy_endpoint_invalid`。 |
| POST endpoint 返回非 JSON-RPC object | protocol error。 |
| SSE data 非 JSON-RPC object | protocol error。 |
| server 返回非 `2024-11-05` protocolVersion | 按 PRD-A pin mismatch fail closed。 |
| `tools/call` POST 失败 | 不自动重放；按 capability execution error 返回。 |

## 10. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-B-AC-001 | `legacy_http_sse` 只允许与 `2024-11-05` 配置组合。 | config/gate unit test |
| MCP-B-AC-002 | transport 能从 SSE endpoint 解析 POST endpoint。 | transport integration test |
| MCP-B-AC-003 | 缺 endpoint event 时 fail closed。 | transport integration test |
| MCP-B-AC-004 | endpoint event 中 raw URL 不进入 diagnostic。 | redaction snapshot test |
| MCP-B-AC-005 | 2024 fake server 可完成 initialize + initialized。 | fake server integration test |
| MCP-B-AC-006 | 2024 fake server 可完成 `tools/list`。 | runtime discovery test |
| MCP-B-AC-007 | 2024 fake server 可完成 ordinary `tools/call`。 | capability execution test |
| MCP-B-AC-008 | JSON-RPC batch 被拒绝。 | protocol unit test |
| MCP-B-AC-009 | optional legacy server 失败只记录 diagnostic；required legacy server 失败阻断 refresh/startup。 | runtime_state integration test |

## 11. 测试计划

- `tests/fixtures/mcp/messages/2024-11-05/*`
- `tests/fixtures/mcp/transports/2024-11-05/legacy_http_sse_*`
- `tests/integrations/mcp/test_legacy_http_sse_transport.py`
- `tests/integrations/mcp/test_2024_legacy_runtime_discovery.py`

## 12. 风险与假设

| 类型 | 内容 | 处理 |
|---|---|---|
| 假设 | 首版只需 HTTP+SSE，不做 2024 stdio。 | stdio 保持 config-gated future。 |
| 风险 | server-provided POST endpoint 可能包含 query secret。 | endpoint diagnostic 必须 fingerprint/redact。 |
| 风险 | 2024 SSE reconnect 语义弱于 2025+ GET resume。 | 明确标为 compatible-degraded，不承诺完整恢复。 |
| 风险 | fake server 与真实 legacy server 行为不一致。 | 后续可增加手工 smoke，但不作为默认回归。 |

## 13. 参考

- MCP `2024-11-05` transport：https://modelcontextprotocol.io/specification/2024-11-05/basic/transports
- MCP `2024-11-05` lifecycle：https://modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle
