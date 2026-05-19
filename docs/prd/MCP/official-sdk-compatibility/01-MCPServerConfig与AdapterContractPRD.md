# PRD-1：MCP Server Config 与 Adapter Contract

- **状态**：已实现（仓库内，待提交）
- **日期**：2026-05-19
- **范围**：MCP server 注册配置、config-driven headers、HTTP plaintext mode、统一 client adapter contract、Python legacy adapter contract 化
- **依赖**：`docs/superpowers/specs/2026-05-19-mcp-official-sdk-client-compatibility-design.md`
- **后续依赖方**：PRD-2 2024 Legacy HTTP+SSE、PRD-3 Official Rust SDK Adapter、PRD-4 Conformance Gate

## 1. 问题陈述

当前 MCP server 配置、header/auth、transport 选择与 runtime client 构造边界仍不够稳定，难以安全引入官方 SDK。真实联调还暴露出两个通用需求：一是 server 需要配置多个业务 header；二是内部 MCP server 可能使用 HTTP 而不是 HTTPS。为避免在 SDK 接入时把协议执行层、业务 runtime、安全策略和 server-specific 联调逻辑混在一起，本 PRD 先建立独立配置文件与统一 adapter contract。


## 1.1 当前状态与证据

| 证据 | 当前事实 | 对本 PRD 的影响 |
|---|---|---|
| `src/integrations/mcp/config.py` | 现有 `MCPRuntimeConfig` / `MCPServerConfig` 已支持 `server_id`、`endpoint`、`protocol_version`、`transport`、`auth`、`limits` 与 `tools`，但字段是内部 snake_case。 | `mcp_server_config.json` 必须定义外部 JSON schema，并显式归一化到内部 config。 |
| `src/integrations/mcp/config.py` | `MCPAuthConfig.headers()` 当前只生成 bearer/API key header，不支持多个普通业务 header。 | 必须新增 config-driven static/request headers，并与 auth header 做冲突校验。 |
| `src/integrations/mcp/config.py::_validate_endpoint` | 当前 remote HTTP 默认被拒绝，只允许 localhost HTTP 例外。 | 本 PRD 必须把 HTTP 改为一等支持路径，并以 `plaintext_http` guard 替代默认拒绝。 |
| `src/integrations/mcp/runtime_state.py` | runtime 已以 `MCPRuntimeConfig` 构造 discovery/registration bundle。 | 新 JSON loader 不应绕过 runtime_state；应归一化为现有 runtime config 后再注册。 |
| `docs/prd/MCP/compatibility/` | 已有四版本兼容 PRD 与 tests 口径。 | 本 PRD 是 SDK 引入前置边界，不替代既有 compatibility PRD。 |


## 1.2 当前实现证据

| 文件 / 命令 | 证据 |
|---|---|
| `src/integrations/mcp/config.py` | 已新增 `load_mcp_server_config()`、外部 `mcpServers` JSON 归一化、camelCase/snake_case 冲突校验、重复 server id fail-closed、config-driven `request_headers`、`transport_security` 与 header/auth 冲突校验。 |
| `src/integrations/mcp/transport_http.py` / `src/integrations/mcp/transport_legacy_http_sse.py` | Streamable HTTP 与 legacy HTTP+SSE 均已接收 `request_headers` 并与 auth headers 合并发送；remote HTTP 不再因 scheme 被默认拒绝，legacy POST endpoint 仍保留同源校验。 |
| `src/integrations/mcp/adapter.py` | 已新增 `MCPClientAdapter` protocol、`MCPAdapterDiagnostic` 与 `PythonLegacyMCPClientAdapter`，用于在官方 SDK 接入前把现有 Python client 包到稳定 contract 后。 |
| `src/integrations/mcp/runtime_state.py` | 默认 client factory 已返回 `PythonLegacyMCPClientAdapter`；`MCPRuntimeDiagnostic` 已包含 `transport_security` 与脱敏 `header_names`。 |
| `src/api/runtime.py` | MCP runtime config 解析已合并 `mcp_server_config.json` / `MAF_MCP_SERVER_CONFIG_PATH` 入口，并保留既有 config 注入路径。 |
| `.gitignore` / `mcp_server_config.example.json` | 真实 `mcp_server_config.json` 已排除入库；示例文件不包含真实 secret、租户或用户值。 |
| `tests/integrations/mcp/test_mcp_server_config.py` | 覆盖 loader、字段映射、alias 冲突、重复 server id、危险 header、auth/header 冲突、example hygiene。 |
| `tests/integrations/mcp/test_mcp_plaintext_http_security.py` | 覆盖 remote HTTP `plaintext_http`、server/tool diagnostic header name redaction、POST/DELETE request header 注入、stdio fail-closed。 |
| `tests/integrations/mcp/test_mcp_adapter_contract.py` | 覆盖 Python legacy adapter contract、初始化/list/call/async+sync close 与错误 diagnostic redaction。 |
| `conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'` | 206 个 integration tests 通过。 |
| `conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'` | 113 个 API tests 通过，含 `mcp.server_discovery_completed` / `mcp.capability_registered` 安全审计字段回归。 |

## 2. 目标

1. 新增独立 `mcp_server_config.json` 注册配置入口，并提供 `mcp_server_config.example.json`。
2. 支持多 server 注册、`enabled` 开关、显式/推断 transport、可选 protocol version pin。
3. 支持 config-driven headers，不在代码中写死 `X-Tenant-Id`、`X-User-Id` 或任何业务 header 名。
4. 将普通 header 与 auth header 分离建模，并定义合并与冲突 fail-closed 规则。
5. 将 HTTP 作为一等支持 transport；`http://` 自动进入 `plaintext_http` 安全模式。
6. 定义并接入统一 `MCPClientAdapter` contract，让现有 Python legacy client 先实现该 contract。
7. 建立 adapter 输出对象、错误分类、diagnostics redaction 与 contract tests。
8. 明确 `mcp_server_config.json` 与既有内部 `MCPRuntimeConfig` 的字段映射、冲突处理与兼容策略。

## 3. 非目标

1. 不在本 PRD 引入官方 Rust SDK 主路径。
2. 不修复 2024 legacy HTTP+SSE 持久 SSE response 缺口；该工作属于 PRD-2。
3. 不实现 resources、prompts、sampling、roots、elicitation 等 feature surface 的业务接入。
4. 不允许 LLM、Planner、用户 prompt 或 tool result 动态决定 MCP server URL、auth、header value、transport 或 protocol version。
5. 不以 `mcp_test.json` 为规范目标，不允许任何 server-specific 特判。
6. 不在本 PRD 实现 `stdio` sandbox；当前仓库仍要求 stdio transport fail-closed，直到单独 sandbox PRD 完成。

## 4. 用户、系统与影响面

| Actor / system | 影响 |
|---|---|
| 运维 / 部署配置者 | 通过 `mcp_server_config.json` 注册一个或多个 MCP server。 |
| MCP runtime | 从独立配置加载 server，构造 adapter，并把 tools 暴露进 CapabilityRegistry。 |
| Security / audit | 需要记录 HTTP 明文风险与 header name，但不得记录 header value。 |
| Planner / CapabilityRegistry | 只消费归一化后的 tool descriptors，不感知底层 SDK 或 transport 细节。 |
| Tests | 新增 config schema、header/auth 合并、plaintext diagnostics、adapter contract 回归。 |

## 5. 配置规范

### 5.1 文件路径

支持两个入口：

```text
默认路径：./mcp_server_config.json
环境变量覆盖：MAF_MCP_SERVER_CONFIG_PATH=/path/to/mcp_server_config.json
```

真实 `mcp_server_config.json` 可能包含业务 header value，默认不入库；仓库提交示例文件：

```text
mcp_server_config.example.json
```

### 5.2 示例

```json
{
  "mcpServers": {
    "example-server": {
      "url": "http://example.internal/gateway/mcp/sse",
      "transport": "legacy_http_sse",
      "protocolVersion": "2024-11-05",
      "headers": {
        "X-Example-Tenant": "example-tenant",
        "X-Example-User": "example-user"
      },
      "auth": {
        "type": "none"
      },
      "enabled": true
    }
  }
}
```

### 5.3 字段要求

| 字段 | 必填 | 规则 |
|---|---:|---|
| `mcpServers` | 是 | object；key 为 server id。 |
| `url` | 是 | 静态配置 URL；支持 `http://` 与 `https://`。 |
| `transport` | 否 | `legacy_http_sse`、`streamable_http`、`stdio`；缺省允许 auto detect，但必须写 diagnostics；`stdio` 在当前 PRD 中只能被识别并 fail closed，不得启用。 |
| `protocolVersion` | 否 | 四版本之一；填了即 pinned，不填则 initialize negotiation。 |
| `headers` | 否 | object；允许多个普通 header；value 不落日志。 |
| `auth` | 否 | `none`、`bearer_env`、`api_key_env`、`preconfigured` 等既有/扩展类型。 |
| `enabled` | 否 | 缺省 true；false 时不注册。 |


### 5.4 外部 JSON 到内部配置的映射

`mcp_server_config.json` 采用 MCP ecosystem 常见的 `mcpServers` 外部形态；runtime 内部仍使用现有 `MCPRuntimeConfig` / `MCPServerConfig`。实现必须在 loader 边界完成归一化，不得让业务 runtime 同时理解两套字段。

| 外部 JSON 字段 | 内部字段 / 行为 | 规则 |
|---|---|---|
| `mcpServers.<key>` | `server_id` | `<key>` 即 server id；如对象内另有 `server_id` / `serverId` 且不一致，必须 fail closed。 |
| `url` | `endpoint` | 必须是静态 URL；不允许从 prompt/tool result 动态生成。 |
| `transport` | `transport` | 缺省允许推断，但推断结果必须进入 diagnostics；正式部署建议显式配置。 |
| `protocolVersion` | `protocol_version` | camelCase 外部字段归一化为 snake_case；也可兼容 `protocol_version`，二者冲突时 fail closed。 |
| `headers` | `request_headers`（新增内部字段） | 普通业务 header；不得与 auth header 冲突；value 永不进入日志。 |
| `auth.tokenEnv` | `auth.token_env` | 支持 camelCase 到 snake_case 归一化；冲突时 fail closed。 |
| `auth.apiKeyEnv` | `auth.api_key_env` | 同上。 |
| `auth.headerName` | `auth.header_name` | 同上。 |
| `enabled` | `enabled` | 缺省 true。 |

### 5.5 配置源与冲突处理

1. `MAF_MCP_SERVER_CONFIG_PATH` 设置时，优先读取该路径。
2. 未设置环境变量时，如仓库运行目录存在 `./mcp_server_config.json`，则读取该文件。
3. 现有通过 `config.yaml` / 显式 `config` dict 注入的 MCP config 仍保留，用于兼容测试和部署；所有来源最终必须归一化为一个 `MCPRuntimeConfig`。
4. 不同来源出现相同 `server_id` 时必须 fail closed，除非调用方显式传入覆盖策略；默认不得静默覆盖。
5. `mcp_test.json` 不作为默认读取路径；如需真实联调，应通过 `--config` 参数显式传给 smoke 脚本或复制为 `mcp_server_config.json`。

## 6. Header / Auth 规则

| ID | Requirement | Priority |
|---|---|---|
| MCP-SDK-1-FR-001 | `headers` 必须完全来自 config，不得写死业务 header 名。 | P0 |
| MCP-SDK-1-FR-002 | header name 必须符合 HTTP token 规则。 | P0 |
| MCP-SDK-1-FR-003 | 必须拒绝危险 header：`Host`、`Content-Length`、`Transfer-Encoding`、`Connection`、`Upgrade` 等。 | P0 |
| MCP-SDK-1-FR-004 | diagnostics/audit 只能记录 header name 列表，不得记录 value。 | P0 |
| MCP-SDK-1-FR-005 | auth header 与普通 header 合并前必须检测冲突；冲突时 fail closed。 | P0 |
| MCP-SDK-1-FR-006 | bearer/API key secret 只能来自环境变量或 secret store，不得写入 tracked config。 | P0 |
| MCP-SDK-1-FR-007 | `mcp_server_config.json` loader 必须把 camelCase/snake_case alias 归一化为内部字段，并拒绝冲突字段。 | P0 |
| MCP-SDK-1-FR-008 | 多配置源出现重复 server id 时必须 fail closed，默认不得静默覆盖。 | P0 |

## 7. HTTP Plaintext Mode

HTTP 默认可用，不需要 `allowInsecureHttp` 开关。runtime 根据 scheme 自动分类：

```text
https:// -> tls_http
http://  -> plaintext_http + mandatory guards
```

强制 guard：

1. URL 只能来自静态 MCP server config。
2. 禁止跨 host redirect。
3. legacy endpoint event 不能跳到不同 origin。
4. audit 记录 `transport_security=plaintext_http`。
5. bearer/API key over HTTP 记录 `credential_over_plaintext_http=true`。
6. header value、token、完整 query、raw request/response body 不进入日志。
7. 请求体、响应体、tool result 均受 size limit。
8. SSE/read/connect/close 均受 timeout 限制。

## 8. Adapter Contract

统一 adapter contract：

```text
MCPClientAdapter
- initialize()
- list_tools()
- call_tool()
- close()
- diagnostics()
```

adapter 输出归一化对象：

```text
NegotiatedSession
MCPToolDescriptor[]
SanitizedToolResult
MCPDiagnostics
MCPError
```

错误分类：

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

## 9. 数据流

```text
读取 mcp_server_config.json
  ↓
schema validate + header/auth validate
  ↓
标记 transport_security
  ↓
构造 MCPServerConfig
  ↓
创建 PythonLegacyAdapter（本 PRD）
  ↓
initialize / tools/list
  ↓
归一化 descriptors
  ↓
CapabilityRegistry 注册 public MCP tools
```

## 10. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-SDK-1-AC-001 | 缺省路径和 `MAF_MCP_SERVER_CONFIG_PATH` 都能加载配置。 | unit / integration test |
| MCP-SDK-1-AC-002 | `headers` 支持多个普通 header 且不写死字段名。 | config test |
| MCP-SDK-1-AC-003 | 危险 header 被拒绝。 | config negative test |
| MCP-SDK-1-AC-004 | auth header 与普通 header 冲突时 fail closed。 | config negative test |
| MCP-SDK-1-AC-005 | HTTP server 不因 scheme 被默认拒绝，diagnostics 标记 `plaintext_http`。 | runtime integration test |
| MCP-SDK-1-AC-006 | header value 不出现在 audit / diagnostics。 | redaction snapshot test |
| MCP-SDK-1-AC-007 | Python legacy client 通过 `MCPClientAdapter` contract 暴露 initialize/list/call/close。 | adapter contract test |
| MCP-SDK-1-AC-008 | disabled server 不注册任何 capability。 | runtime_state test |
| MCP-SDK-1-AC-009 | `mcp_server_config.example.json` 不包含真实 secret 或真实内网 header value。 | repository hygiene check |
| MCP-SDK-1-AC-010 | `mcp_server_config.json` 被 `.gitignore` 排除，真实配置不进入 tracked 文件。 | repository hygiene check |
| MCP-SDK-1-AC-011 | 外部 camelCase config 与内部 snake_case config 的归一化、冲突字段、重复 server id 均有测试。 | config loader tests |
| MCP-SDK-1-AC-012 | stdio 仍按当前 sandbox gate fail closed，不被本 PRD 隐式启用。 | config negative test |

## 11. 测试计划

- `tests/integrations/mcp/test_mcp_server_config.py`
- `tests/integrations/mcp/test_mcp_adapter_contract.py`
- `tests/integrations/mcp/test_mcp_plaintext_http_security.py`
- `tests/integrations/test_mcp_runtime_state.py`
- redaction snapshot / assertion tests

## 12. 风险与处理

| 风险 | 处理 |
|---|---|
| HTTP 默认可用增加明文传输风险。 | 自动进入 `plaintext_http`，强制 audit、redirect/origin、redaction、timeout 与 size limit。 |
| config 文件包含业务 header value 被误提交。 | `.gitignore` 真实文件，只提交 example，并在文档中明确。 |
| adapter contract 过早抽象导致实现冗余。 | 只抽 initialize/list/call/close/diagnostics 五个必要方法，避免提前泛化 feature surface。 |
| SDK 后续接入绕过 Python policy。 | contract 明确 SDK 只能在 adapter 内，输出必须归一化。 |

## 13. 参考

- 设计文档：`docs/superpowers/specs/2026-05-19-mcp-official-sdk-client-compatibility-design.md`
- MCP 官方 SDK 文档：https://modelcontextprotocol.io/docs/sdk
- 官方 Rust SDK：https://github.com/modelcontextprotocol/rust-sdk
