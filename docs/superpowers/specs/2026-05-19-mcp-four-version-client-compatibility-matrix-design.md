# MCP Four-Version Client Compatibility Matrix Design

- **Date:** 2026-05-19
- **Status:** Approved design for implementation planning
- **Scope:** MCP Runtime as **client only**
- **Protocol revisions covered:** `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`

## 1. Goal and boundaries

Design a layered compatibility matrix for the repository MCP Runtime so it can reason about and test compatibility with four official MCP protocol revisions as a client connecting to external MCP servers.

The matrix is split into three layers:

1. **Core protocol layer** — JSON-RPC object shape, initialize, version negotiation, capability negotiation, request / response / notification handling.
2. **Transport layer** — stdio, legacy HTTP+SSE for `2024-11-05`, Streamable HTTP for `2025-03-26+`, session headers, and resume behavior.
3. **Feature / extension layer** — tools, resources, prompts, progress, cancellation, auth, structured output, elicitation, tasks, and metadata handling.

Out of scope:

- Implementing this repository as an MCP server.
- External server implementation internals.
- Non-official protocol extensions.
- Planner / LLM product strategy for selecting MCP tools.
- Production rollout, shadow, or enforce migration beyond identifying gates.

## 2. Compatibility status vocabulary

Every matrix cell uses one of these statuses:

| Status | Meaning |
|---|---|
| `supported` | Runtime should support this natively and have conformance / integration tests. |
| `compatible-degraded` | Runtime can connect or consume safely, but capability is reduced or metadata is ignored. |
| `config-gated` | Protocol capability exists, but this project enables it only through explicit safe configuration. |
| `not-supported` | Runtime explicitly does not support it and must fail closed or skip the server/tool. |
| `future` | Capability is acknowledged but reserved for later design / implementation. |
| `not-applicable` | Capability does not apply to that protocol version or layer. |

## 3. Core protocol layer

| Core capability | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | Client behavior |
|---|---|---|---|---|---|
| Protocol version enum | `supported` | `supported` | `supported` | `supported` | Expand `SUPPORTED_MCP_PROTOCOL_VERSIONS` to all four revisions. |
| Initialize `protocolVersion` | `supported` | `supported` | `supported` | `supported` | Mandatory. Client must send one candidate version in `initialize.params.protocolVersion`. |
| Initialize request version selection | `supported` | `supported` | `supported` | `supported` | If server config pins a version, send that version; otherwise send runtime default candidate `2025-11-25`. |
| Negotiated session version | `supported` | `supported` | `supported` | `supported` | Use `InitializeResult.protocolVersion` as the single session version; it must be in the supported set. |
| Post-initialize version stability | `supported` | `supported` | `supported` | `supported` | A session never switches versions after initialize. Transport and feature gates read session state. |
| Initialized notification | `supported` | `supported` | `supported` | `supported` | Always send `notifications/initialized` after a valid InitializeResult. |
| JSON-RPC request / response / notification object | `supported` | `supported` | `supported` | `supported` | Outer data-layer message must be a JSON-RPC 2.0 object. |
| JSON-RPC batch | `not-supported` | `compatible-degraded` | `not-supported` | `not-supported` | `2025-03-26` briefly supported batching, but this runtime remains object-only and rejects batch arrays. |
| Request id correlation | `supported` | `supported` | `supported` | `supported` | Client generates request IDs per session; responses must match the request ID. |
| Server-to-client request | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | Respond to `ping`; unsupported roots/sampling/elicitation/etc. return method-not-found / unsupported. |
| Capability negotiation | `supported` | `supported` | `supported` | `supported` | Operation uses only negotiated server capabilities and allowed tool metadata. |
| Client capabilities | `config-gated` | `config-gated` | `config-gated` | `config-gated` | Default `{}`; do not declare roots/sampling/elicitation/tasks unless separately implemented and tested. |
| Logging notification | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | May be received and sanitized for diagnostics; does not enter planner prompts. |
| Protocol mismatch handling | `supported` | `supported` | `supported` | `supported` | No shared supported version means fail closed; optional server is skipped, required server fails startup/refresh. |

Core principles:

- Version negotiation happens exactly once per session during initialize.
- `protocolVersion` is mandatory in the client initialize request.
- The server's InitializeResult determines the negotiated session version.
- Runtime default version affects only the initial candidate for unpinned server configs.
- JSON-RPC batch support is not required for tool business payloads. A tool may return many rows/items while the outer JSON-RPC response remains a single object.

## 4. Transport layer

| Transport capability | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | Client behavior |
|---|---|---|---|---|---|
| stdio | `config-gated` | `config-gated` | `config-gated` | `config-gated` | Standard transport, but this project requires sandbox / lifecycle / secret gates before enabling. |
| HTTP+SSE legacy transport | `supported` | `not-applicable` | `not-applicable` | `not-applicable` | Only for `2024-11-05`; implement as independent `LegacyHTTPSSETransport`. |
| Streamable HTTP transport | `not-applicable` | `supported` | `supported` | `supported` | Used for `2025-03-26+`; extend current `StreamableHTTPTransport`. |
| HTTP endpoint model | `supported` | `supported` | `supported` | `supported` | 2024 uses SSE endpoint plus server-provided POST endpoint; 2025+ uses a single MCP endpoint for POST/GET. |
| POST request body | `supported` | `supported` | `supported` | `supported` | Send one JSON-RPC object per request; never send batch arrays. |
| POST response JSON | `supported` | `supported` | `supported` | `supported` | Request may return a JSON-RPC response object. |
| POST response SSE stream | `not-applicable` | `supported` | `supported` | `supported` | Streamable HTTP may return an SSE stream from POST. |
| Dedicated GET stream | `not-applicable` | `supported` | `supported` | `supported` | 2025+ can open server-to-client stream by GET; 405 means unavailable, not fatal protocol corruption. |
| 2024 SSE endpoint messages | `supported` | `not-applicable` | `not-applicable` | `not-applicable` | Client connects to SSE endpoint, reads endpoint event, and POSTs future messages to that endpoint. |
| `MCP-Protocol-Version` header | `not-applicable` | `compatible-degraded` | `supported` | `supported` | Required for `2025-06-18+`; may be sent for `2025-03-26`; not used for `2024-11-05`. |
| `MCP-Session-Id` header | `not-applicable` | `supported` | `supported` | `supported` | If returned by server, carry it on later requests; 404 triggers controlled reinitialize. |
| `Last-Event-ID` resume | `compatible-degraded` | `supported` | `supported` | `supported` | Save legacy SSE IDs but do not promise full 2024 recovery; 2025+ resumes through GET. |
| DELETE session shutdown | `not-applicable` | `compatible-degraded` | `supported` | `supported` | Use for 2025+ where supported; 405 means server does not support active session termination. |
| Auth transport rule | `config-gated` | `config-gated` | `config-gated` | `config-gated` | Endpoint, token, headers, and auth never come from Planner, LLM, or user messages. |

Transport principles:

- Introduce `transport_family`:
  - `legacy_http_sse` for `2024-11-05` HTTP.
  - `streamable_http` for `2025-03-26+` HTTP.
  - `stdio` for future sandbox-gated local servers.
- HTTP transport family must be known from config before initialize because the 2024 and 2025+ connection models differ.
- Do not auto-detect HTTP transport in this design; a discovery probe would require its own design.
- Do not force 2024 HTTP+SSE into the existing Streamable HTTP adapter. Share validation and SSE parsing utilities, not transport state machines.

### Transport family compatibility gate

| Negotiated version | Allowed transport family |
|---|---|
| `2024-11-05` | `legacy_http_sse`, `stdio` |
| `2025-03-26` | `streamable_http`, `stdio` |
| `2025-06-18` | `streamable_http`, `stdio` |
| `2025-11-25` | `streamable_http`, `stdio` |

If negotiated version and transport family do not match: optional servers are skipped with diagnostics; required servers fail closed.

## 5. Feature / extension layer

| Feature capability | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | Client behavior |
|---|---|---|---|---|---|
| `tools/list` | `supported` | `supported` | `supported` | `supported` | Core cross-version discovery path. |
| Ordinary `tools/call` | `supported` | `supported` | `supported` | `supported` | Core cross-version invocation path; map to `CapabilityExecutionResult`. |
| Tool `inputSchema` | `supported` | `supported` | `supported` | `supported` | Validate before invocation; unsupported dialect means tool is not exposed. |
| Tool annotations | `not-applicable` | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | Treat as untrusted metadata; do not directly grant permissions. |
| Tool/resource/prompt icons | `not-applicable` | `not-applicable` | `not-applicable` | `compatible-degraded` | Ignore by default; do not pull remote icon data or include it in planner prompts. |
| Structured tool output / `outputSchema` | `not-applicable` | `not-applicable` | `supported` | `supported` | Validate output where schema exists. |
| Resource links in tool result | `not-applicable` | `not-applicable` | `compatible-degraded` | `compatible-degraded` | Preserve safe metadata only; do not auto-fetch resources. |
| Text/image/embedded resource content | `supported` | `supported` | `supported` | `supported` | Text supported; image/resource gated by size/MIME and artifact safety. |
| Audio content | `not-applicable` | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | Preserve as safe artifact metadata; do not send to Planner by default. |
| `resources/list` / `resources/read` | `future` | `future` | `future` | `future` | Do not expose as public capability in this design. |
| `prompts/list` / `prompts/get` | `future` | `future` | `future` | `future` | Do not expose as public capability in this design. |
| Progress notification | `supported` | `supported` | `supported` | `supported` | Accept only with active request/task correlation; sanitize diagnostics. |
| Cancellation notification | `supported` | `supported` | `supported` | `supported` | Use `notifications/cancelled` for ordinary in-flight requests. |
| OAuth / authorization framework | `not-applicable` | `config-gated` | `config-gated` | `config-gated` | Static bearer/API key first; interactive OAuth remains future. |
| Protected resource metadata / resource indicators | `not-applicable` | `future` | `future` | `future` | Map 401/403/scope challenge to stable auth errors. |
| Elicitation | `not-applicable` | `not-applicable` | `future` | `future` | Do not declare client capability; unsupported if server requests it. |
| Sampling | `config-gated` | `config-gated` | `config-gated` | `config-gated` | Default off and not declared. |
| Roots | `config-gated` | `config-gated` | `config-gated` | `config-gated` | Default off and not declared. |
| Completions | `not-applicable` | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | Ignore for public capability exposure. |
| Tasks | `not-applicable` | `not-applicable` | `not-applicable` | `future` | `2025-11-25` experimental; do not enable by default in this design. |
| Task-augmented `tools/call` | `not-applicable` | `not-applicable` | `not-applicable` | `future` | Requires server capability, tool `execution.taskSupport`, and config to all allow it. |
| Server instructions / richer implementation metadata | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | `compatible-degraded` | Keep out of Planner prompt; sanitize for diagnostics only. |

Feature principles:

- The first multi-version compatibility target is ordinary `tools/list` and ordinary `tools/call`.
- Newer metadata is safe to parse only as untrusted diagnostics unless explicitly allowlisted.
- Resources, prompts, tasks, interactive OAuth, roots, sampling, elicitation, and stdio sandbox remain outside this implementation slice.

## 6. Configuration and runtime data flow

Example configuration:

```yaml
mcp:
  servers:
    - server_id: legacy_crm
      enabled: true
      required: false
      protocol_version: "2024-11-05"
      transport: "legacy_http_sse"
      endpoint: "https://example.com/sse"

    - server_id: modern_crm
      enabled: true
      required: true
      protocol_version: "2025-11-25"
      transport: "streamable_http"
      endpoint: "https://example.com/mcp"
```

Configuration rules:

| Field | Design |
|---|---|
| `protocol_version` | Mandatory for non-default / legacy servers; if absent, use runtime default candidate `2025-11-25`. |
| `transport` | Mandatory; 2024 HTTP must use `legacy_http_sse`; 2025+ HTTP must use `streamable_http`. |
| `endpoint` | Interpreted by transport: 2024 is SSE endpoint; 2025+ is single MCP endpoint. |
| `required` | Required server failure fails startup/refresh; optional server failure skips with diagnostics. |
| `client_capabilities` | Default `{}`; only explicit allowlisted capabilities can be declared. |

Initialization flow:

1. Runtime reads server config.
2. Runtime creates the transport adapter from `transport`.
3. Client sends `initialize` with mandatory `params.protocolVersion`.
4. Server returns `InitializeResult.protocolVersion`.
5. Client verifies returned version is supported, compatible with transport family, and compatible with any explicit config pin.
6. Client stores `negotiated_protocol_version` in session state.
7. Client sends `notifications/initialized`.
8. Later `tools/list`, `tools/call`, progress, cancellation, and metadata parsing all use the session's negotiated version.

Session state shape:

```python
MCPNegotiatedSession(
    server_id: str,
    requested_protocol_version: str,
    negotiated_protocol_version: str,
    transport_family: str,
    server_capabilities: Mapping[str, Any],
    server_info: Mapping[str, Any],
    session_id: str | None,
    legacy_post_endpoint: str | None,
    last_event_id: str | None,
)
```

Constraints:

- No in-session version switching.
- No HTTP transport auto-detection in this design.
- No 2024 legacy HTTP+SSE emulation through Streamable HTTP.
- No protocol, endpoint, auth, transport, or tool identity control from LLM / Planner / user messages.

## 7. Error handling matrix

| Scenario | Behavior |
|---|---|
| Config protocol version outside four-version set | Config validation fails; server does not enter discovery. |
| Config transport incompatible with protocol version | Config validation fails. |
| Initialize returns unknown version | Fail closed; optional server skip, required server fails refresh/startup. |
| Initialize returns version different from explicit config pin | Fail closed to avoid transport/feature ambiguity. |
| Initialize result lacks `protocolVersion` | Protocol error; skip/fail server based on `required`. |
| Response ID mismatch | Protocol error; never auto-retry side-effecting requests. |
| Batch message | Protocol error; fail closed for every supported version. |
| 2024 SSE endpoint lacks POST endpoint event | Transport error; skip/fail server. |
| 2025+ session 404 | Reinitialize only for read-only/list flows; never automatically replay `tools/call`. |
| Progress/cancel lacks active correlation | Ignore user-visible effect and record sanitized diagnostic. |
| Unsupported server-to-client request | Return JSON-RPC method-not-found / unsupported response. |
| Tool result output validation fails | Return capability execution error; do not pass unvalidated output to Planner. |
| 401/403/scope challenge | Map to auth_required / scope_required; do not misclassify as protocol mismatch. |

## 8. Test and evidence plan

Test layers:

1. **Contract fixtures**
   - Versioned fixture roots such as `tests/fixtures/mcp/messages/<version>/...`.
   - Each version includes initialize, initialized, tools/list, tools/call, progress, cancellation, and error fixtures.
   - `2024-11-05` includes legacy HTTP+SSE endpoint event fixtures.
   - `2025-03-26+` includes Streamable HTTP POST/GET/session/header fixtures.

2. **Protocol unit tests**
   - Version candidate selection.
   - Negotiated session state.
   - Explicit config pin behavior.
   - Unsupported version fail-closed behavior.
   - Batch rejection.
   - Feature gate lookup by negotiated version.

3. **Transport integration tests**
   - `LegacyHTTPSSETransport`: connect SSE endpoint, parse endpoint event, POST to server-provided endpoint, read server message events, handle timeout/missing endpoint/malformed SSE.
   - `StreamableHTTPTransport`: POST JSON, POST SSE response, GET stream, session ID, 404 reinitialize, DELETE 405.

4. **Runtime discovery / capability tests**
   - Four-version fake server discovery.
   - Optional unsupported server skipped with diagnostic.
   - Required unsupported server fails refresh/startup.
   - Ordinary `tools/call` across all four versions.
   - Newer metadata does not enter Planner prompt by default.
   - `2025-11-25` tasks remain disabled by default.

Evidence/documentation gates:

- Change `tests/fixtures/mcp/contracts/conformance_matrix.json` from a single `mcp_spec_version` to `supported_mcp_spec_versions`.
- Change `src/integrations/mcp/mcp_runtime_gates.py` so conformance reports require coverage for all supported versions instead of only `2025-11-25`.
- Update MCP PRDs from a single `2025-11-25` baseline to multi-version client compatibility invariants.

## 9. Implementation boundary for the first plan

First implementation slice should include:

- Four-version enum and config validation.
- Mandatory initialize `protocolVersion` handling.
- Negotiated session version state.
- Ordinary `tools/list` and ordinary `tools/call` across four versions.
- `LegacyHTTPSSETransport` for `2024-11-05`.
- Existing / extended Streamable HTTP for `2025-03-26+`.
- Deterministic feature gates keyed by negotiated session version.
- Versioned conformance fixtures and tests.

First slice should not include:

- Public resources/prompts capabilities.
- Interactive OAuth.
- Tasks / durable task registry.
- Roots, sampling, or elicitation.
- stdio sandbox implementation.
- Production enforce rollout.

## 10. Official reference anchors

- MCP `2024-11-05` lifecycle and HTTP+SSE transport.
- MCP `2025-03-26` changelog: Streamable HTTP introduced and previous HTTP+SSE replaced.
- MCP `2025-06-18` changelog: JSON-RPC batching removed, structured output and elicitation added.
- MCP `2025-11-25` changelog: tasks, icons metadata, URL mode elicitation, and related enhancements added.
