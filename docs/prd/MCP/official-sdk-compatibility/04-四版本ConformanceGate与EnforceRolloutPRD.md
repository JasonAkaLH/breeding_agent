# PRD-4：四版本 Conformance Gate 与 Enforce Rollout

- **状态**：已实现（仓库内，待提交；真实 production artifact / shadow / benchmark / ops / decommission evidence 仍按 PRD05 pending/fail-closed）
- **日期**：2026-05-19
- **范围**：四版本 conformance evidence、adapter shadow/enforce allowlist、release gate、真实 server smoke、文档与 PRD05 evidence 对齐
- **依赖**：PRD-1 MCP Server Config 与 Adapter Contract；PRD-2 2024 Legacy HTTP+SSE；PRD-3 Official Rust SDK Adapter Shadow Compare

## 1. 问题陈述

引入官方 SDK 后，不能用“SDK 能跑”或“某个真实 server 能调通”作为生产支持声明。我们需要把四个声明支持的 MCP 协议版本拆成 `version + transport + adapter` 组合，分别用 repo-local fixtures、fake server、adapter contract、security diagnostics、shadow compare evidence 与 release gate 证明。只有组合级 evidence 通过后，才允许进入 enforce。


## 1.1 当前状态与证据

| 证据 | 当前事实 | 对本 PRD 的影响 |
|---|---|---|
| `tests/fixtures/mcp/contracts/conformance_matrix.json` | 已存在 MCP conformance matrix，并已被前序 compatibility 轨道扩展为四版本口径。 | 本 PRD 应扩展 adapter/shadow/enforce 维度，而不是另建不兼容 schema。 |
| `src/integrations/mcp/mcp_runtime_gates.py` | 已校验 `SUPPORTED_MCP_PROTOCOL_VERSION_ORDER` 与 transport family evidence。 | 官方 SDK enforce gate 必须复用/扩展现有 gate，不绕过 PRD05 fail-closed 机制。 |
| `docs/prd/rust/evidence/prd05/mcp_runtime_release_gates.json` | PRD05 仍保持真实 production artifact、shadow、benchmark、ops/recovery、legacy decommission pending gate。 | 本 PRD 的 repo-local conformance 不能伪造 production enforce 证据。 |
| `tests/integrations/mcp/test_mcp_runtime_gates.py` / `test_prd05_mcp_runtime_evidence.py` | 已有 evidence validator 回归。 | 新 SDK matrix 字段必须配套 validator tests。 |

## 1.2 当前实现证据

| 文件 / 命令 | 证据 |
|---|---|
| `tests/fixtures/mcp/contracts/conformance_matrix.json` | 已声明四个且仅四个 supported versions，并为 `version + transport + adapter` 记录 `python_legacy` 与 `official_rust_sdk` conformance / shadow / enforce evidence；scope 明确为 `remote_http_only_until_stdio_sandbox_passes`。其中 official SDK 在 `2024-11-05 + legacy_http_sse` 组合为 `unsupported_transport / skipped`，在 2025+ Streamable HTTP 组合为 `partial_shadow_verified / matched`：repo-local SDK 证据只声明 JSON object response，SSE response 仍由 Python visible path 覆盖，直到补齐 rmcp-backed SSE fixture。 |
| `src/integrations/mcp/mcp_runtime_gates.py` | `validate_mcp_official_sdk_conformance_matrix()` 校验 exact four versions、transport family、adapter dimension、stdio 未通过、组合级 evidence 与 repo-local `evidence_refs` 文件存在性；并拒绝 official SDK 在 unsupported/pending 组合上把 operation 字段伪装为 passed，且禁止 official SDK 在缺少 SSE fixture 时把 `sse_response` 写成 true；`validate_mcp_enforce_allowlist()` 要求 enforce 组合 shadow status 为 matched。 |
| `docs/prd/rust/evidence/prd05/mcp_runtime_release_gates.json` | 已纳入 repo-local client compatibility evidence、`rmcp 1.7.0` client-only dependency 证据、official SDK 2025+ Streamable HTTP shadow compare、2024 legacy skipped gap、adapter enforce allowlist 与非规范 smoke 标记；production release gate 仍为 `pending_external_production_evidence`。 |
| `scripts/smoke_mcp_server_config.py` | 真实 server smoke 为非默认脚本，输出 redacted JSON report，并标注 `external_smoke_sample.is_normative=false`、`server_specific_logic_allowed=false`。 |
| `tests/integrations/mcp/test_official_sdk_conformance_matrix.py` / `test_prd05_mcp_runtime_evidence.py` / `test_mcp_smoke_server_config_script.py` | 覆盖 matrix schema、stdio/all-transport fail-closed、shadow mismatch fail-closed、PRD05 allow-pending vs strict fail-closed、非规范 dry-run smoke redaction。 |

## 2. 目标

1. 建立四版本 conformance matrix：`2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`。
2. 对每个版本覆盖 initialize、initialized、tools/list、tools/call、transport response shape、error、timeout、redaction、安全策略。
3. 建立 `version + transport + adapter` shadow/enforce allowlist。
4. 把官方 Rust SDK adapter 从 shadow 推进到组合级 enforce，而不是全局切换。
5. 建立 `scripts/smoke_mcp_server_config.py` 作为真实 server 非默认 smoke。
6. 同步 MCP PRD、Rust PRD05 evidence、README 与开发对接指南口径。
7. 明确本轨道首批 conformance 覆盖 remote HTTP/HTTPS ordinary tools；`stdio` 必须等 sandbox-gated PRD 完成后才能加入 all-transport 声明。

## 3. 非目标

1. 不把真实 server smoke 作为 normative conformance gate。
2. 不允许 server-specific 特调。
3. 不自动支持 SDK 暴露的额外历史版本。
4. 不在所有组合通过前下线 Python legacy rollback path。
5. 不在本 PRD 扩展为 MCP server。
6. 不把 resources/prompts/sampling/roots/elicitation 全量业务接入作为首批 enforce 前置条件；但 unsupported feature gating 必须可诊断。
7. 不把 `stdio` 纳入本轨道首批 enforce；当前仓库仍要求 stdio sandbox 未完成前 fail closed。

## 4. Conformance Matrix

每个 version/transport 至少覆盖：

| 维度 | 必测项 |
|---|---|
| lifecycle | initialize negotiation、initialized notification、close |
| tools | tools/list、ordinary tools/call、tool error result |
| transport | object response、SSE response、legacy persistent SSE response、session header、404 session 行为 |
| config | protocolVersion pinned/auto、transport family gate、multi-header config |
| safety | HTTP plaintext audit、redirect/origin guard、header redaction、raw id redaction、payload size limit |
| policy | batch request policy、unsupported feature gating、metadata 不进入 planner 权限 |
| errors | stable error code、SDK/Python adapter error mapping consistency |

示意 schema：

```json
{
  "supported_mcp_spec_versions": {
    "2024-11-05": {
      "legacy_http_sse": {
        "python_legacy": {
          "initialize": "passed",
          "initialized": "passed",
          "tools_list": "passed",
          "tools_call": "passed",
          "persistent_sse_response": "passed",
          "request_id_correlation": "passed",
          "plaintext_http_audit": "passed",
          "redaction": "passed"
        },
        "official_rust_sdk": {
          "operational_status": "unsupported_transport",
          "shadow_compare": "skipped",
          "enforce_allowed": false,
          "gap_reason": "official Rust SDK Streamable HTTP adapter does not cover legacy HTTP+SSE"
        }
      }
    }
  }
}
```

## 5. Enforce Rollout

状态机：

```text
unsupported
  ↓ conformance fixtures pass
shadow_allowed
  ↓ shadow compare matched + release evidence pass
enforce_allowed
  ↓ production gate + rollback drill
enforced
```

要求：

1. enforce allowlist 以组合为单位：`server? + version + transport + adapter`。
2. 未在 allowlist 的组合必须 fail closed 或回到 visible Python legacy path。
3. enforce 后仍保留 rollback path，直到后续明确 legacy decommission gate 通过。
4. production gate 不得用本地合成 evidence 伪造真实 shadow / ops / benchmark / allowlist promotion。

## 6. 真实 Server Smoke

新增非默认脚本：

```bash
python scripts/smoke_mcp_server_config.py --config mcp_server_config.json
```

输出必须包含：

- server name
- requested / negotiated protocol version
- transport
- adapter
- serverInfo
- capabilities
- tools list
- safe no-arg tool call 摘要
- diagnostics redaction evidence
- `external_smoke_sample.is_normative=false`

真实 server smoke 不进入默认 CI，不作为 conformance 必要条件。

## 7. 功能需求

| ID | Requirement | Priority |
|---|---|---|
| MCP-SDK-4-FR-001 | conformance matrix 必须声明四个支持版本，且不得静默扩展。 | P0 |
| MCP-SDK-4-FR-002 | 每个版本必须覆盖 initialize、initialized、tools/list、tools/call。 | P0 |
| MCP-SDK-4-FR-003 | `2024-11-05` 必须覆盖 legacy persistent SSE response 与 request id correlation。 | P0 |
| MCP-SDK-4-FR-004 | 2025+ 必须覆盖 Streamable HTTP object response 与 SSE response。 | P0 |
| MCP-SDK-4-FR-005 | 每个组合必须有 redaction、plaintext_http、redirect/origin safety evidence。 | P0 |
| MCP-SDK-4-FR-006 | SDK adapter enforce 必须以 allowlist 控制，不能全局开关直接切换。 | P0 |
| MCP-SDK-4-FR-007 | shadow mismatch 必须阻止该组合进入 enforce。 | P0 |
| MCP-SDK-4-FR-008 | 真实 server smoke evidence 必须标注非规范且禁止特调。 | P0 |
| MCP-SDK-4-FR-009 | 文档、PRD05 evidence、开发对接指南必须同步四版本和 SDK adapter 口径。 | P1 |
| MCP-SDK-4-FR-010 | conformance evidence 必须显式标注 transport scope：`remote_http` / `legacy_http_sse` / `streamable_http`；不得暗示 stdio 已通过。 | P0 |
| MCP-SDK-4-FR-011 | all-transport 支持声明必须等待 stdio sandbox PRD 与 evidence 通过。 | P0 |

## 8. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-SDK-4-AC-001 | conformance matrix 包含四个且仅四个声明支持版本。 | schema test |
| MCP-SDK-4-AC-002 | 四版本 fixtures 均通过 Python legacy adapter conformance。 | integration tests |
| MCP-SDK-4-AC-003 | 官方 Rust SDK adapter 对四版本生成 shadow compare evidence。 | sidecar integration tests |
| MCP-SDK-4-AC-004 | shadow mismatch 阻止组合进入 enforce allowlist。 | release gate test |
| MCP-SDK-4-AC-005 | HTTP 明文、安全脱敏、redirect/origin、payload size limit 都有组合级 evidence。 | security tests |
| MCP-SDK-4-AC-006 | `scripts/smoke_mcp_server_config.py` 可读取 config 并输出脱敏报告。 | script smoke / unit test with fake server |
| MCP-SDK-4-AC-007 | `mcp_test.json` 类真实配置可作为人工 smoke，但代码中无 server-specific 特判。 | static/assertion + smoke |
| MCP-SDK-4-AC-008 | PRD05 evidence ledger 仍对真实 production allowlist、shadow、ops、benchmark fail-closed。 | validator test |
| MCP-SDK-4-AC-009 | docs 索引和开发对接指南均指向新 SDK/四版本口径。 | documentation check |
| MCP-SDK-4-AC-010 | conformance/evidence 明确 remote HTTP scope，且 stdio 未完成时不会出现 all-transport passed 声明。 | schema / documentation check |
| MCP-SDK-4-AC-011 | PRD05 validator 保持 production external gates pending/fail-closed，不被 SDK repo-local conformance 覆盖。 | validator test |

## 9. 测试计划

- `tests/integrations/mcp/test_official_sdk_conformance_matrix.py`
- `tests/integrations/mcp/test_official_rust_sdk_shadow_compare.py`
- `tests/integrations/mcp/test_mcp_enforce_allowlist.py`
- `tests/integrations/mcp/test_mcp_smoke_server_config_script.py`
- `tests/integrations/mcp/test_mcp_runtime_gates.py`
- `tests/integrations/mcp/test_prd05_mcp_runtime_evidence.py`
- Rust sidecar workspace tests and quality gates

## 10. Release Gate Evidence

PRD04 完成时，应在 evidence 中至少记录：

```json
{
  "client_compatibility": {
    "supported_versions": [
      "2024-11-05",
      "2025-03-26",
      "2025-06-18",
      "2025-11-25"
    ],
    "adapters": ["python_legacy", "official_rust_sdk"],
    "conformance_status": "passed",
    "transport_scope": "remote_http_only_until_stdio_sandbox_passes",
    "shadow_status": "matched_or_documented_gap",
    "external_smoke_samples": {
      "is_normative": false,
      "server_specific_logic_allowed": false
    }
  }
}
```

Production enforce 仍必须等待真实外部证据：deployment allowlist promotion、生产 shadow、benchmark、ops/recovery drill、rollback drill 与 legacy decommission gate。

## 11. 风险与处理

| 风险 | 处理 |
|---|---|
| conformance matrix 与 PRD05 evidence 口径分裂。 | PRD04 必须同步 evidence ledger 和 validator tests。 |
| 真实 smoke 被误当作规范 gate。 | evidence schema 强制 `is_normative=false`，文档重复声明。 |
| SDK adapter 在某版本 shadow mismatch。 | 组合级阻止 enforce，允许 adapter shim 或记录 unsupported gap。 |
| allowlist 过粗导致风险扩散。 | allowlist 粒度至少包含 version、transport、adapter，必要时包含 server class/fingerprint。 |

## 12. 参考

- 设计文档：`docs/superpowers/specs/2026-05-19-mcp-official-sdk-client-compatibility-design.md`
- MCP 官方 SDK 文档：https://modelcontextprotocol.io/docs/sdk
- 官方 Rust SDK：https://github.com/modelcontextprotocol/rust-sdk
- MCP `2024-11-05` 规范：https://modelcontextprotocol.io/specification/2024-11-05/basic/index
- MCP `2025-03-26` 规范：https://modelcontextprotocol.io/specification/2025-03-26/basic/index
- MCP `2025-06-18` 规范：https://modelcontextprotocol.io/specification/2025-06-18/basic/index
- MCP `2025-11-25` 规范：https://modelcontextprotocol.io/specification/2025-11-25/basic/index
