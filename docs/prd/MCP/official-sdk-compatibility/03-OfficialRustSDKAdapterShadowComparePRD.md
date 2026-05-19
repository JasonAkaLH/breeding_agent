# PRD-3：Official Rust SDK Adapter 与 Shadow Compare

- **状态**：已实现（仓库内，待提交；production enforce 仍受 PRD-4 / PRD05 外部 evidence gate 约束）
- **日期**：2026-05-19
- **范围**：Rust sidecar 官方 SDK 依赖引入、`OfficialRustSdkAdapter`、adapter contract bridge、shadow compare evidence、供应链门禁
- **依赖**：PRD-1 MCP Server Config 与 Adapter Contract；PRD-2 2024 Legacy HTTP+SSE 完整协议支持
- **后续依赖方**：PRD-4 四版本 Conformance Gate 与 Enforce Rollout

## 1. 问题陈述

我们决定中长期引入官方 MCP SDK，降低自研协议栈维护成本。但 SDK 不能直接替换业务 runtime，也不能绕过现有安全、审计、配置、planner 权限和 release gate。本 PRD 将官方 Rust SDK / `rmcp` 接入 Rust sidecar，形成 `OfficialRustSdkAdapter`，并先以 shadow compare 方式验证它与现有 Python legacy visible path 的行为一致性。


## 1.1 当前状态与证据

| 证据 | 当前事实 | 对本 PRD 的影响 |
|---|---|---|
| `native/crates/maf_mcp_runtime/` | 已存在 `maf-mcp-runtime-sidecar` crate / binary、contract artifact 与 typed error table。 | 官方 SDK adapter 应落在该 crate 或其明确子模块内，不新建平行 runtime。 |
| `native/crates/maf_mcp_runtime/src/lib.rs` | 当前侧重 contract、compatibility handshake、JSON-RPC validation、sanitization、task registry 等确定性 kernel。 | SDK adapter 必须复用现有 error code / sanitizer / compatibility guard，不绕过 kernel。 |
| `native/Cargo.toml` | Rust workspace 已纳入 `maf_mcp_runtime`，并由 Rust quality gates 管理。 | `rmcp` 依赖必须进入 workspace lock、deny、SBOM/provenance 证据链。 |
| MCP 官方 SDK 文档 | 官方 SDK 列出 Rust SDK 为 Tier 2，SDK 可用于构建 MCP clients/servers 并支持本地/远程 transport。 | 引入 SDK 合理，但 Tier 2 意味着必须用本项目 conformance gate 验证，不以 SDK 声明替代验收。 |

## 1.2 当前实现证据

| 文件 / 命令 | 证据 |
|---|---|
| `native/Cargo.toml` / `native/Cargo.lock` | `rmcp = 1.7.0` 已进入 workspace dependency 与 lockfile，`default-features = false`，仅启用 `client`、`transport-streamable-http-client-reqwest`、`reqwest`；lockfile drift guard 要求不得引入 `rmcp-macros`。 |
| `native/crates/maf_mcp_runtime/Cargo.toml` | `maf_mcp_runtime` 通过 workspace 引用 `rmcp`，并保留 `official-rust-sdk-adapter` feature marker；该 marker 不能扩展产品批准的 MCP 协议版本集合。 |
| `native/crates/maf_mcp_runtime/src/lib.rs` | 已新增 `OfficialRustSdkAdapter` metadata、`rmcp` compile-time API marker、Streamable HTTP SDK-backed `initialize / list_tools / call_tool / close / diagnostics` session、shadow compare evidence、matched / mismatched / skipped 状态、脱敏 server fingerprint 与 SDK error 到 `McpRuntimeErrorCode` 的稳定映射。 |
| `native/crates/maf_mcp_runtime/src/lib.rs` tests | Rust unit tests 覆盖 metadata traceability、compile-time marker、SDK-backed 2025+ Streamable HTTP fake server initialize/list/call/close、shadow compare matched / mismatched / skipped、redaction 与 error mapping。 |
| `tests/integrations/mcp/test_official_rust_sdk_shadow_compare.py` | Python validator 覆盖 `rmcp` dependency / lockfile / feature drift guard、四版本 shadow evidence、matched / mismatched / skipped、bad transport-version 与 raw payload fail-closed。 |
| `tests/integrations/mcp/test_mcp_enforce_allowlist.py` | 组合级 enforce allowlist 已证明 shadow mismatch 阻止 official SDK enforce。 |

边界说明：当前 `rmcp 1.7.0` 以本项目批准的 client-only Streamable HTTP feature set 接入，因此 SDK-backed operational adapter 覆盖 `2025-03-26 / 2025-06-18 / 2025-11-25` Streamable HTTP；`2024-11-05` legacy HTTP+SSE 仍由 Python legacy visible path 覆盖，official Rust SDK lane 在该组合中记录 `skipped / unsupported_transport`，不得误报为 SDK operational pass。

## 2. 目标

1. 在 `maf-mcp-runtime-sidecar` 内引入官方 Rust SDK / `rmcp`。
2. 实现 `OfficialRustSdkAdapter`，对齐 PRD-1 定义的 `MCPClientAdapter` contract。
3. Python runtime 通过 sidecar/facade 调用 SDK adapter，不直接依赖 SDK。
4. 在 shadow 模式下对比 Python legacy visible path 与 Rust SDK shadow path。
5. 记录脱敏 shadow compare evidence。
6. 建立 SDK dependency、license、SBOM/provenance、feature compatibility 与 failure isolation 门禁。
7. 本 PRD 不直接启用 production enforce。
8. 记录官方 SDK crate 版本、feature flags、license、SBOM/provenance 与 conformance gap，不硬编码过期版本号。

## 3. 非目标

1. 不让 SDK 直接注册 CapabilityRegistry。
2. 不让 SDK 决定 server URL、headers、auth、HTTP security 或 planner 权限。
3. 不在 Python 主 runtime 引入 Python SDK 作为第二主路径。
4. 不承诺 SDK 支持的额外历史版本自动进入产品支持范围。
5. 不因为 shadow 成功就下线 Python legacy path。
6. 不用真实 server smoke 替代 repo-local fixture / conformance tests。
7. 不把 `rmcp` 示例中的 server-side macros 或 server handler 能力引入业务 runtime；本 PRD 只使用 client/transport 所需特性。

## 4. 架构边界

```text
Python runtime
  - config loader
  - policy / audit / sanitizer
  - CapabilityRegistry
  - PythonLegacyAdapter visible path
        |
        | sidecar request / shadow compare request
        v
maf-mcp-runtime-sidecar
  - OfficialRustSdkAdapter
  - rmcp client / transport
  - normalized response bridge
        |
        v
MCP server
```

SDK adapter 必须只返回归一化对象：

```text
NegotiatedSession
MCPToolDescriptor[]
SanitizedToolResult
MCPDiagnostics
MCPError
```

## 5. 功能需求

| ID | Requirement | Priority |
|---|---|---|
| MCP-SDK-3-FR-001 | Rust sidecar 必须把官方 SDK 包在 `OfficialRustSdkAdapter` 内。 | P0 |
| MCP-SDK-3-FR-002 | SDK adapter 必须实现 initialize/list_tools/call_tool/close/diagnostics 语义。 | P0 |
| MCP-SDK-3-FR-003 | SDK adapter 输入只能来自已验证的 `MCPServerConfig`，不能接受动态 URL。 | P0 |
| MCP-SDK-3-FR-004 | SDK adapter 输出必须归一化，不泄露 SDK 原始异常/结构。 | P0 |
| MCP-SDK-3-FR-005 | shadow compare 必须对比 negotiated version、serverInfo、capabilities、tool descriptors、safe call result shape、error category。 | P0 |
| MCP-SDK-3-FR-006 | shadow mismatch 不改变 visible path 用户结果。 | P0 |
| MCP-SDK-3-FR-007 | evidence 必须脱敏，不包含 header value、token、raw request/response body。 | P0 |
| MCP-SDK-3-FR-008 | SDK dependency 必须进入 cargo lock、license allowlist、SBOM/provenance 检查。 | P0 |
| MCP-SDK-3-FR-009 | SDK 不支持或行为不一致的合法协议情况必须在 adapter shim 或 conformance gap 中显式记录。 | P1 |
| MCP-SDK-3-FR-010 | SDK 支持的额外版本不得自动扩展 `SUPPORTED_MCP_PROTOCOL_VERSIONS`。 | P0 |
| MCP-SDK-3-FR-011 | `rmcp` crate version 与 feature flags 必须在 PRD evidence / lockfile / SBOM 中可追溯；不得仅依赖 README 示例版本。 | P0 |
| MCP-SDK-3-FR-012 | SDK adapter error 必须映射到 `McpRuntimeErrorCode` / Python `MCPClientError` 稳定错误码，不得泄露 SDK 原始错误字符串中的 secret。 | P0 |

## 6. Shadow Compare Evidence

示例：

```json
{
  "server": "example-server",
  "visible_adapter": "python_legacy",
  "shadow_adapter": "official_rust_sdk",
  "protocol_version": "2025-11-25",
  "transport": "streamable_http",
  "status": "matched",
  "compared_fields": [
    "negotiated_protocol_version",
    "server_info",
    "capabilities",
    "tools_descriptor_shape",
    "safe_tool_call_result_shape",
    "error_category"
  ],
  "redaction": {
    "header_values": "redacted",
    "raw_payload": "omitted"
  }
}
```

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| SDK 初始化失败 | shadow evidence 记录 `sdk_initialize_failed`；visible path 不受影响。 |
| SDK 返回 unsupported transport/version | evidence 记录 `sdk_unsupported_combination`；不得自动 fallback 成 false pass。 |
| SDK 原始异常 | 映射为稳定 `MCPError` 分类并脱敏。 |
| shadow mismatch | 记录 mismatch category、fingerprint 与 adapter versions；不改变用户可见结果。 |
| sidecar 不可用 | shadow skip 记录 reason；visible path 继续按 Python legacy 执行。 |
| dependency/license gate 失败 | build/release gate fail closed。 |

## 8. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-SDK-3-AC-001 | Rust sidecar 编译并包含官方 SDK adapter。 | cargo check/test |
| MCP-SDK-3-AC-002 | SDK adapter 通过 PRD-1 adapter contract tests。 | adapter contract tests |
| MCP-SDK-3-AC-003 | shadow compare 对四版本 fixture 至少产生 matched / mismatched / skipped 三类稳定 evidence。 | integration tests |
| MCP-SDK-3-AC-004 | shadow mismatch 不影响 Python visible path。 | runtime integration test |
| MCP-SDK-3-AC-005 | SDK 原始异常不会泄露到 planner/frontend。 | error mapping test |
| MCP-SDK-3-AC-006 | header value、token、raw payload 不进入 evidence。 | redaction test |
| MCP-SDK-3-AC-007 | SDK dependency 通过 license / SBOM / provenance gate。 | cargo deny / provenance scripts |
| MCP-SDK-3-AC-008 | 未经 PRD-4 allowlist，不允许 production enforce SDK adapter。 | release gate test |
| MCP-SDK-3-AC-009 | `Cargo.lock`、license allowlist、SBOM/provenance 均能追溯 `rmcp` 及其传递依赖。 | cargo deny / provenance test |
| MCP-SDK-3-AC-010 | SDK adapter 不引入 server-side handler/macro 作为 runtime 必需路径。 | dependency/features review |

## 9. 测试计划

- Rust unit tests for `OfficialRustSdkAdapter`
- Python facade integration tests for sidecar shadow compare
- `tests/integrations/mcp/test_mcp_adapter_contract.py`
- `tests/integrations/mcp/test_official_rust_sdk_shadow_compare.py`
- `cargo test --workspace --all-features`
- `cargo check --workspace --all-targets --all-features`
- license / SBOM / provenance gate tests

## 10. 风险与处理

| 风险 | 处理 |
|---|---|
| 官方 SDK 对某些 legacy 行为覆盖不足。 | adapter shim 内补齐，或在 conformance gap 中 fail closed，不把 gap 推给业务 runtime。 |
| SDK 版本升级改变行为。 | pin dependency、记录 adapter/sdk version、shadow compare 作为升级门禁。 |
| Rust sidecar 与 Python runtime contract 漂移。 | 使用 shared contract fixtures 与 adapter contract tests。 |
| SDK 成功诱导过早 enforce。 | PRD-4 allowlist 前只允许 shadow。 |

## 11. 参考

- MCP 官方 SDK 文档：https://modelcontextprotocol.io/docs/sdk
- 官方 Rust SDK：https://github.com/modelcontextprotocol/rust-sdk
- 设计文档：`docs/superpowers/specs/2026-05-19-mcp-official-sdk-client-compatibility-design.md`
