# PRD-C：MCP 2025+ Streamable HTTP 多版本收敛

- **状态**：待评审
- **日期**：2026-05-19
- **范围**：`2025-03-26`、`2025-06-18`、`2025-11-25` 的 Streamable HTTP client behavior / fixtures / ordinary tools 链路
- **依赖**：PRD-A 协议版本与协商内核
- **非目标**：不实现 2024 legacy HTTP+SSE；不实现 tasks durable registry；不实现 interactive OAuth；不公开 resources/prompts

## 1. 问题陈述

当前 `StreamableHTTPTransport` 按 `2025-11-25` 单版本设计，默认发送 `MCP-Protocol-Version`、支持 POST JSON/SSE、GET stream、session id、DELETE session 等能力。要兼容 `2025-03-26` 与 `2025-06-18`，必须把这些行为改为基于 negotiated session version 的 gate，而不是继续假设所有 2025+ server 都是 latest。

## 2. 目标

1. 让现有 Streamable HTTP transport 支持 `2025-03-26`、`2025-06-18`、`2025-11-25` 三个 negotiated versions。
2. 保持 JSON-RPC object-only，不支持 batch。
3. 保留普通 `tools/list` / `tools/call` 作为首个跨版本能力。
4. 对 `MCP-Protocol-Version` header、session id、GET stream、DELETE 405、POST SSE response 等行为建立版本 gate 和 fixtures。
5. 对 structured output、icons、tasks 等新版本 feature 做 safe parse / ignore / future gate。

## 3. 非目标

1. 不实现 2024 HTTP+SSE。
2. 不实现 task-augmented `tools/call`、`tasks/get/result/list/cancel`。
3. 不实现 resources/prompts public capability。
4. 不实现 interactive OAuth、resource indicators、elicitation UI。
5. 不修改 Rust sidecar production enforce 门禁。

## 4. 当前证据

| 文件 | 当前事实 |
|---|---|
| `src/integrations/mcp/transport_http.py` | POST headers 始终包含 `Accept: application/json, text/event-stream`、`Content-Type`、`MCP-Protocol-Version`；GET/DELETE 使用 same endpoint。 |
| `tests/integrations/mcp/test_phase2_runtime_behavior.py` | 已有 GET stream、Last-Event-ID、session 404 reinitialize 测试。 |
| `tests/integrations/mcp/test_phase2_streamable_http_contract.py` | 已有 Streamable HTTP contract 方向测试。 |
| `docs/prd/MCP/03-Phase2-StreamableHTTP与SSE内核PRD.md` | 当前 Phase 2 仍以 `2025-11-25` 为标准一致性基线。 |

## 5. 功能需求

| ID | Requirement | Priority |
|---|---|---|
| MCP-C-FR-001 | `streamable_http` transport family 必须允许 `2025-03-26`、`2025-06-18`、`2025-11-25`。 | P0 |
| MCP-C-FR-002 | POST request body 必须始终为单个 JSON-RPC object。 | P0 |
| MCP-C-FR-003 | transport 必须支持 POST JSON response。 | P0 |
| MCP-C-FR-004 | transport 必须支持 POST `text/event-stream` response，并从 events 中选择 matching JSON-RPC response。 | P0 |
| MCP-C-FR-005 | `2025-03-26` 不得把 `MCP-Protocol-Version` header 作为 server 正确性的硬依赖。 | P0 |
| MCP-C-FR-006 | `2025-06-18` 与 `2025-11-25` 后续 HTTP 请求必须携带 negotiated `MCP-Protocol-Version` header。 | P0 |
| MCP-C-FR-007 | server 返回 `MCP-Session-Id` 后，后续 POST/GET/DELETE 必须携带 session id。 | P0 |
| MCP-C-FR-008 | session 404 对 discovery/read-only 请求可 reinitialize 后重试；对 `tools/call` 不自动重放。 | P0 |
| MCP-C-FR-009 | GET stream 405 必须表示 server 不支持 GET stream，不作为 protocol fatal。 | P1 |
| MCP-C-FR-010 | DELETE 405 必须表示 server 不支持主动 session shutdown，不作为 protocol fatal。 | P1 |
| MCP-C-FR-011 | `2025-06-18+` structured output / outputSchema 可校验；不存在时维持普通 result mapping。 | P1 |
| MCP-C-FR-012 | `2025-11-25` tasks、icons 等 metadata 默认不启用用户可见能力。 | P1 |

## 6. 非功能需求

| 类型 | Requirement |
|---|---|
| 兼容性 | 三个 2025+ 版本共享 transport adapter，但 version-specific 行为必须经 gate helper 控制。 |
| 安全 | Session id、Last-Event-ID、auth header、raw tool output 不得进入前端事件或未脱敏 audit。 |
| 可靠性 | 不自动 replay side-effecting `tools/call`；所有重试策略必须能区分 read-only discovery 与 invocation。 |
| 可测试性 | 每个 2025+ 版本必须有独立 fixtures，而不是只复用 latest fixture。 |
| 可维护性 | 新 metadata 只能作为 safe metadata / diagnostic；不得直接改变 planner 权限。 |

## 7. 数据流

1. PRD-A 初始化获得 negotiated version。
2. `StreamableHTTPTransport` 根据 session version 生成 headers。
3. client 执行 `tools/list` discovery。
4. runtime 按 negotiated version 解析 tool metadata：
   - `2025-03-26`：annotations/audio/completions 等 metadata safe degraded。
   - `2025-06-18`：structured output/outputSchema/resource links safe gate。
   - `2025-11-25`：icons/tasks metadata safe gate，tasks 默认 future。
5. executor 执行 ordinary `tools/call`。
6. output 经过 schema validation / sanitizer 后映射为 `CapabilityExecutionResult`。

## 8. 错误处理

| 场景 | 行为 |
|---|---|
| POST 返回 batch array | protocol error，fail closed。 |
| POST SSE event 非 JSON-RPC object | protocol error。 |
| response id mismatch | protocol error。 |
| `2025-06-18+` 后续请求缺 negotiated protocol header | client bug；测试应阻断。 |
| session 404 during `tools/list` | reinitialize 后重试一次。 |
| session 404 during `tools/call` | 不自动重发；返回 retriable transport/capability error。 |
| unsupported server-to-client request | 返回 method-not-found / unsupported。 |
| 401/403/scope challenge | 映射 auth_required / scope_required。 |

## 9. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-C-AC-001 | `streamable_http` 与三个 2025+ versions 配对合法。 | gate unit test |
| MCP-C-AC-002 | 三个版本均可完成 initialize + initialized。 | fake server integration test |
| MCP-C-AC-003 | 三个版本均可完成 `tools/list`。 | runtime discovery test |
| MCP-C-AC-004 | 三个版本均可完成 ordinary `tools/call`。 | capability execution test |
| MCP-C-AC-005 | `2025-03-26` fixture 覆盖不依赖 protocol version header 的 server。 | transport integration test |
| MCP-C-AC-006 | `2025-06-18` 与 `2025-11-25` 后续请求携带 negotiated `MCP-Protocol-Version`。 | header assertion test |
| MCP-C-AC-007 | POST SSE response 多事件中 response id 必须匹配 active request。 | SSE parser/client test |
| MCP-C-AC-008 | GET stream 405 和 DELETE 405 被视为 capability unavailable。 | transport integration test |
| MCP-C-AC-009 | session 404 不自动 replay `tools/call`。 | client integration test |
| MCP-C-AC-010 | tasks / resources / prompts / elicitation 不默认公开。 | feature gate test |

## 10. 测试计划

- `tests/fixtures/mcp/messages/2025-03-26/*`
- `tests/fixtures/mcp/messages/2025-06-18/*`
- `tests/fixtures/mcp/messages/2025-11-25/*`
- `tests/integrations/mcp/test_streamable_http_versions.py`
- `tests/integrations/mcp/test_streamable_http_session_recovery.py`
- `tests/integrations/mcp/test_mcp_feature_gates.py`

## 11. 风险与假设

| 类型 | 内容 | 处理 |
|---|---|---|
| 假设 | 2025+ 首版兼容只要求 ordinary tools。 | tasks/resources/prompts 标为 future。 |
| 风险 | 2025-03-26 server 对 header 行为实现不一致。 | 降级处理，不以 header 作为 hard dependency。 |
| 风险 | 新 metadata 被误送进 Planner prompt。 | 增加 prompt/context snapshot test。 |
| 风险 | session 404 重试误重放 tool side effect。 | 明确只对 list/read 重试，tools/call 不自动 replay。 |

## 12. 参考

- MCP `2025-03-26` changelog：https://modelcontextprotocol.io/specification/2025-03-26/changelog
- MCP `2025-06-18` changelog：https://modelcontextprotocol.io/specification/2025-06-18/changelog
- MCP `2025-11-25` changelog：https://modelcontextprotocol.io/specification/2025-11-25/changelog
