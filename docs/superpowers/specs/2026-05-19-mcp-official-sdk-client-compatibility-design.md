# MCP Client 官方 SDK 引入与四版本完整兼容设计

日期：2026-05-19

## 1. 背景与目标

我们当前是 **MCP client**，不是 MCP server。目标是在不改变现有业务 runtime 边界的前提下，引入官方 MCP SDK，形成面向 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25` 四个协议版本的完整 client 兼容能力。

本设计的核心目标：

1. 引入官方 Rust SDK 作为中长期 MCP 协议/transport 执行层。
2. 保留我们的业务 runtime 边界：CapabilityRegistry、planner 权限、租户/用户上下文、审计、脱敏、release gate、sidecar rollout 都不交给 SDK。
3. 四个协议版本都作为一等 conformance target，而不是只围绕 latest spec 或单个真实测试 server 适配。
4. 支持独立 `mcp_server_config.json` 注册配置，headers、transport、protocol version、enabled 状态都由配置声明。
5. HTTP 作为一等支持 transport；默认可用，但进入受控明文安全模式。

官方参考：

- MCP 官方 SDK 文档：https://modelcontextprotocol.io/docs/sdk
- 官方 Rust SDK 仓库：https://github.com/modelcontextprotocol/rust-sdk

## 2. 非目标与约束

### 2.1 只做 client runtime

本设计不要求我们实现 MCP server。所有能力围绕 client-side initialize、capability discovery、tools/list、tools/call、transport lifecycle、错误处理与 conformance evidence 展开。

### 2.2 不以 `mcp_test.json` 为规范目标

`mcp_test.json` 只是一个真实联调样本 server。它可以用于证明通用协议能力能在真实网络环境中工作，但不是协议标准，也不是特调目标。

实现中禁止出现以下逻辑：

- server name 特判
- host 特判
- path 特判
- header name 写死
- tool name 写死
- 返回 payload shape 特判

正确口径是：我们兼容它暴露出的合法 legacy HTTP+SSE 行为，但不为它做 server-specific 适配。

### 2.3 SDK 不接管业务 runtime

官方 SDK 只负责协议与 transport 执行。以下能力仍由我们控制：

- server 配置来源与 schema validation
- header/auth 合并与冲突处理
- URL 来源限制、HTTP 明文安全模式、redirect/origin 检查
- planner tool allowlist 与 capability 注册
- audit、diagnostics、redaction
- payload sanitizer 与 size limit
- release gate、shadow compare、enforce allowlist
- Python fallback / rollback path

## 3. 总体架构

```text
业务 runtime
- CapabilityRegistry
- tenant/user/header policy
- planner allowlist
- audit / diagnostics / redaction
- task / node / lifecycle
- release gate / evidence
        |
        v
MCPClientAdapter 抽象层
        |
        +-- PythonLegacyAdapter
        |   现有 Python 实现，迁移期可见路径与 rollback 兜底
        |
        +-- OfficialRustSdkAdapter
            Rust sidecar 内接入官方 Rust SDK / rmcp，先 shadow 后 enforce
```

### 3.1 Adapter contract

所有 adapter 对业务层暴露同一种语义合同：

```text
MCPClientAdapter
- initialize()
- list_tools()
- call_tool()
- close()
- diagnostics()
```

adapter 输出必须归一化为我们的内部对象，而不是泄露 SDK 原始结构：

```text
NegotiatedSession
MCPToolDescriptor[]
SanitizedToolResult
MCPDiagnostics
MCPError
```

### 3.2 SDK 引入位置

官方 Rust SDK 进入 `maf-mcp-runtime-sidecar`，作为 `OfficialRustSdkAdapter` 的实现依赖。

Python 主 runtime 不直接依赖官方 SDK；它通过 sidecar / adapter contract 使用能力。这样可以避免 Python SDK 与 Rust SDK 双主路径造成协议栈分裂。

### 3.3 迁移策略

迁移分三段：

1. **contract first**：先让现有 Python client 接入统一 adapter contract。
2. **SDK shadow**：Rust SDK adapter 只做 shadow compare，不改变用户可见结果。
3. **逐组合 enforce**：按 `version + transport` 组合通过 conformance 后，进入 enforce allowlist。

## 4. 四版本与 transport 兼容矩阵

| MCP 协议版本 | 支持目标 | 主 transport |
| --- | --- | --- |
| `2024-11-05` | 完整普通 client 支持 | Legacy HTTP+SSE / stdio |
| `2025-03-26` | 完整普通 client 支持 | Streamable HTTP / stdio |
| `2025-06-18` | 完整普通 client 支持 | Streamable HTTP / stdio |
| `2025-11-25` | latest baseline 完整普通 client 支持 | Streamable HTTP / stdio |

“完整普通 client 支持”至少包含：

- initialize negotiation
- initialized notification
- tools/list
- tools/call
- 基础错误处理
- capability gating
- connection close
- timeout / reconnect 语义
- header/auth 发送
- diagnostics redaction
- HTTP plaintext audit
- conformance evidence

本设计不要求立即把 resources、prompts、sampling、elicitation、roots 等所有 MCP feature surface 接入业务层；但协议层与 adapter contract 不得把这些能力设计死。

## 5. `2024-11-05` Legacy HTTP+SSE 设计

`2024-11-05` 必须支持真正的 legacy HTTP+SSE 形态：

```text
GET /sse
  <- event: endpoint
POST endpoint(message)
  -> JSON-RPC request
GET /sse 持续接收
  <- event: message, JSON-RPC response
```

关键要求：

1. SSE reader 必须在 session 生命周期内保持打开。
2. `endpoint` event 只用于发现 POST message endpoint。
3. JSON-RPC request 通过 POST endpoint 发送。
4. JSON-RPC response 可以来自原 SSE stream，而不要求 POST body 返回。
5. response 必须按 JSON-RPC request id correlation 匹配。
6. 支持 timeout、close、reconnect、malformed event diagnostics。
7. endpoint URL 必须通过 same-origin / redirect / scheme safety 检查。

这不是为某个真实 server 特调，而是 legacy HTTP+SSE client 应支持的合法协议形态。

## 6. `2025+` Streamable HTTP 设计

`2025-03-26`、`2025-06-18`、`2025-11-25` 以 Streamable HTTP 为主路径：

```text
POST /mcp initialize
POST /mcp initialized
POST /mcp tools/list
POST /mcp tools/call
```

必须覆盖：

- object-only JSON-RPC response
- SSE response stream
- session id header
- protocol version negotiation
- missing / expired session diagnostics
- 404 session 行为
- GET / DELETE 行为
- batch request policy
- unsupported feature gating
- metadata 不进入 planner 权限

## 7. `mcp_server_config.json` 注册配置

### 7.1 文件职责

新增独立 MCP server 注册配置文件：

```text
mcp_server_config.json
```

该文件声明可注册的 MCP server。示例：

```json
{
  "mcpServers": {
    "example-server": {
      "url": "http://example.internal/mcp/sse",
      "transport": "legacy_http_sse",
      "protocolVersion": "2024-11-05",
      "headers": {
        "X-Tenant-Id": "118",
        "X-User-Id": "363"
      },
      "enabled": true
    }
  }
}
```

### 7.2 文件入口

建议支持两个入口：

```text
默认路径：./mcp_server_config.json
环境变量覆盖：MAF_MCP_SERVER_CONFIG_PATH=/path/to/mcp_server_config.json
```

真实 `mcp_server_config.json` 可能包含业务 header value，默认不入库；仓库只提交：

```text
mcp_server_config.example.json
```

### 7.3 注册流程

```text
读取 mcp_server_config.json
    ↓
schema validate
    ↓
构造 MCPServerConfig
    ↓
按 server 注册 MCP client adapter
    ↓
initialize / tools/list
    ↓
把 tools 暴露进 CapabilityRegistry
```

### 7.4 配置规则

- `headers` 完全来自 JSON，不写死业务字段。
- 支持任意多个 header。
- `transport` 建议显式配置；缺省时允许 auto detect，但 diagnostics 必须记录推断结果。
- `protocolVersion` 可选：填了就是 pinned，不填则 initialize negotiation。
- `enabled=false` 的 server 不注册。
- 文件中允许多个 server。
- header value 不进入日志、不进入 LLM、不进入 planner。

## 8. Header / Auth 策略

### 8.1 普通 header

普通 header 由 server-level config 声明：

```json
{
  "headers": {
    "X-Tenant-Id": "118",
    "X-User-Id": "363"
  }
}
```

规则：

- header name 必须符合 HTTP header name 格式。
- 禁止危险 header，例如 `Host`、`Content-Length`、`Transfer-Encoding`、`Connection`、`Upgrade`。
- diagnostics/audit 最多记录 header name 列表。
- header value 只用于发送，不能写入日志、prompt、planner 或 artifact。

### 8.2 Auth header

认证 header 继续独立建模：

```json
{
  "auth": {
    "type": "bearer_env",
    "tokenEnv": "MAF_MCP_EXAMPLE_TOKEN"
  }
}
```

最终发送前合并：

```text
final_headers = configured_headers + auth_headers
```

如果普通 header 与 auth header 冲突，例如都声明 `Authorization`，则 fail closed。

## 9. HTTP 明文安全模式

HTTP 是一等支持 transport，不需要 `allowInsecureHttp` 开关。

runtime 根据 scheme 自动分类：

```text
https:// -> tls_http
http://  -> plaintext_http + mandatory guards
```

HTTP 下强制执行：

1. URL 只能来自静态 MCP server config。
2. LLM、用户 prompt、tool result 不能动态拼 MCP server URL。
3. 禁止跨 host redirect。
4. legacy endpoint event 不能跳到不同 origin。
5. header value 永不落日志。
6. audit 记录 `transport_security=plaintext_http`。
7. bearer/API key over HTTP 记录 `credential_over_plaintext_http=true`。
8. 请求体、响应体、tool result 都有 size limit。
9. SSE 有 read timeout、heartbeat timeout、close timeout。
10. diagnostics 只包含脱敏 URL fingerprint、scheme、host、transport、version、header names。

## 10. 错误处理模型

所有 adapter 统一返回我们的错误分类：

```text
MCPConfigError
MCPTransportError
MCPProtocolError
MCPNegotiationError
MCPAuthError
MCPToolError
MCPTimeoutError
MCPConformanceError
```

要求：

- SDK 原始异常不得直接抛给业务层。
- planner / frontend 只看到稳定 error code。
- diagnostics 可定位问题但不泄露 secret。
- Python adapter 与 Rust SDK adapter 返回同一种错误模型。

## 11. Conformance Gate

四版本支持必须由 repo-local fake server / fixtures 证明。每个版本至少覆盖：

- initialize negotiation
- initialized notification
- tools/list
- tools/call
- transport-specific response shape
- timeout / reconnect / close
- invalid server diagnostics
- header redaction
- HTTP plaintext audit
- batch request policy
- unsupported feature gating

示意 evidence：

```json
{
  "supported_mcp_spec_versions": {
    "2024-11-05": {
      "legacy_http_sse": {
        "initialize": "passed",
        "initialized": "passed",
        "tools_list": "passed",
        "tools_call": "passed",
        "persistent_sse_response": "passed",
        "request_id_correlation": "passed"
      }
    },
    "2025-03-26": {
      "streamable_http": {
        "initialize": "passed",
        "initialized": "passed",
        "tools_list": "passed",
        "tools_call": "passed",
        "session_header": "passed",
        "sse_response": "passed"
      }
    }
  }
}
```

## 12. SDK Shadow Compare

官方 Rust SDK adapter 先以 shadow 模式运行：

```text
visible path: PythonLegacyAdapter
shadow path: OfficialRustSdkAdapter
```

对比字段：

- negotiated protocol version
- serverInfo
- capabilities
- tool descriptors
- safe tools/call result shape
- error category

shadow compare 只写 evidence，不改变用户可见结果。只有某个 `version + transport` 组合同时通过 conformance 与 shadow compare，才能进入 enforce allowlist。

示意 evidence：

```json
{
  "server": "example-server",
  "visible_adapter": "python_legacy",
  "shadow_adapter": "official_rust_sdk",
  "protocol_version": "2024-11-05",
  "transport": "legacy_http_sse",
  "status": "matched"
}
```

## 13. 真实 Server Smoke

真实 server smoke 是非规范补充证据：

```json
{
  "kind": "external_smoke_sample",
  "is_normative": false,
  "server_specific_logic_allowed": false
}
```

建议脚本：

```bash
python scripts/smoke_mcp_server_config.py --config mcp_server_config.json
```

输出：

- server name
- requested / negotiated protocol version
- transport
- serverInfo
- capabilities
- tools list
- safe no-arg tool call 摘要
- diagnostics redaction evidence

该脚本访问真实网络服务，不进入默认 CI。

## 14. 分步 PRD / 交付拆分

### PRD 1：MCP Server Config 与 Adapter Contract

交付：

- `mcp_server_config.json` 读取入口。
- `mcp_server_config.example.json`。
- server config schema validation。
- 多 server 注册。
- 多 header 支持。
- auth header 与普通 header 合并规则。
- HTTP plaintext mode。
- 统一 `MCPClientAdapter` contract。
- Python legacy adapter 接入该 contract。

验收：

- config-driven headers 不写死。
- HTTP 可连，但 diagnostics 标记 plaintext。
- header value 不落日志。
- adapter contract tests 通过。

### PRD 2：2024 Legacy HTTP+SSE 完整协议支持

交付：

- 持久 SSE reader。
- `endpoint` event 解析。
- POST message endpoint。
- 原 SSE stream `message` response 接收。
- JSON-RPC request id correlation。
- reconnect / timeout / close。
- same-origin / redirect safety。
- 2024 fixture fake server。
- `tools/list` / `tools/call` conformance。

验收：

- repo-local fixture 覆盖 POST body response 与 original SSE response 两种形态。
- external smoke sample 可选通过，但不作为 normative gate。

### PRD 3：官方 Rust SDK Adapter + Shadow Compare

交付：

- Rust sidecar 中新增 `OfficialRustSdkAdapter`。
- 使用官方 Rust SDK / `rmcp` 处理标准 MCP client 协议。
- Python runtime 通过 sidecar 调 adapter。
- shadow compare evidence。
- SDK dependency / license / provenance gate。
- 不直接 enforce。

验收：

- SDK adapter 与 Python adapter contract 对齐。
- 四版本 fixture 至少能跑 shadow compare。
- shadow mismatch 可诊断、可脱敏、不会影响 visible path。

### PRD 4：四版本 Conformance Gate 与 Enforce Rollout

交付：

- `2024-11-05`
- `2025-03-26`
- `2025-06-18`
- `2025-11-25`

四版本矩阵化 evidence：

```text
version × transport × initialize/tools/list/tools/call/error/safety
```

同时交付：

- enforce allowlist。
- release gate。
- docs 更新。
- smoke 脚本。
- PRD05 evidence 对齐。

验收：

- 每个 `version + transport` 组合都有 fixture/conformance evidence。
- Rust SDK adapter 可按组合切 enforce。
- Python legacy 保留 rollback path。
- 真实 server smoke 可选通过。
- 无 server-specific 特调。

## 15. 实施顺序

```text
PRD 1：配置 + Adapter Contract
  ↓
PRD 2：2024 Legacy 完整补齐
  ↓
PRD 3：官方 Rust SDK Adapter shadow
  ↓
PRD 4：四版本 Conformance + enforce
```

该顺序先解决配置和 header/HTTP 通用问题，再修协议缺口，再引官方 SDK，最后做正式发布门禁。

## 16. 验收总标准

完成整个设计时必须满足：

1. 四个协议版本都有 repo-local conformance evidence。
2. HTTP/HTTPS 都是正式支持路径。
3. HTTP 明文连接有 audit 与 mandatory guards。
4. headers 完全 config-driven，无业务字段写死。
5. 官方 SDK 位于 Rust sidecar adapter 内，不绕过业务 runtime。
6. Python legacy path 可用于迁移期 visible path 与 rollback。
7. 真实 server smoke 不产生任何 server-specific 代码。
8. release gate 能按 `version + transport + adapter` 组合决定 shadow/enforce。
