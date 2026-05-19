# PRD-D：MCP Client 四版本 Conformance Gate、文档同步与 Sidecar 口径

- **状态**：待评审
- **日期**：2026-05-19
- **范围**：conformance evidence / fixtures contract / PRD 文档同步 / Rust sidecar external protocol 口径
- **依赖**：PRD-A、PRD-B、PRD-C 的验收定义；可并行准备文档与 schema
- **非目标**：不实现新的 transport；不做 production enforce rollout；不伪造外部生产 evidence

## 1. 问题陈述

仓库当前 MCP 文档、fixtures 和 evidence gate 多处仍把 `2025-11-25` 当作唯一协议基线。如果 runtime 已实现 client 四版本兼容，但 conformance gate 仍只接受 `mcp_spec_version == "2025-11-25"`，会导致证据账本、Rust sidecar 口径和 PRD 说明互相矛盾。

本 PRD 负责在实现 A/B/C 后，把文档和 evidence gate 收敛为 multi-version client compatibility，不让 production enforce 或 sidecar contract 提前宣称未完成能力。

## 2. 目标

1. 将 conformance matrix 从单版本字段升级为四版本覆盖字段。
2. 让 `src/integrations/mcp/mcp_runtime_gates.py` 验证所有 supported versions 的 conformance coverage。
3. 更新 `docs/prd/MCP/*`、`docs/prd/backend/14-MCPRuntime实现需求PRD.md` 和相关 README，明确从 single latest baseline 调整为 client multi-version compatibility。
4. 明确 Rust MCP sidecar 当前仍是 contract/handshake skeleton，除非后续实现，否则不得宣称 canonical multi-version transport。
5. 保留 PRD03-PRD05 external production evidence fail-closed 语义，不用本地合成证据替代生产 rollout。

## 3. 非目标

1. 不实现 legacy 或 streamable transport。
2. 不更新 production allowlist / artifact promotion 真实证据。
3. 不下线 Python legacy MCP protocol/sanitizer/activation 路径。
4. 不把本项目扩展为 MCP Server。
5. 不改变 non-MCP Rust runtime PRD gate。

## 4. 当前证据

| 文件 | 当前事实 |
|---|---|
| `tests/fixtures/mcp/contracts/conformance_matrix.json` | 使用 `mcp_spec_version: "2025-11-25"`。 |
| `src/integrations/mcp/mcp_runtime_gates.py` | `validate_mcp_runtime_conformance_report()` 只接受 `2025-11-25`。 |
| `docs/prd/MCP/00-MCPRuntime联合改造总览PRD.md` | 标题和 invariant 明确写 MCP `2025-11-25`。 |
| `docs/prd/MCP/README.md` | Phase 说明要求遵守 `2025-11-25` 标准一致性不变量。 |
| `docs/prd/backend/14-MCPRuntime实现需求PRD.md` | 协议参考版本为 latest spec `2025-11-25`。 |
| `tests/fixtures/mcp/contracts/sidecar_v1_contract.json` | `external_mcp_protocol_version` 为 `2025-11-25` 单值。 |
| `src/integrations/mcp/sidecar.py` | sidecar handshake 要求 external MCP protocol version 等于 current `MCP_PROTOCOL_VERSION`。 |

## 5. 功能需求

| ID | Requirement | Priority |
|---|---|---|
| MCP-D-FR-001 | conformance matrix 必须使用 `supported_mcp_spec_versions` 列出四版本。 | P0 |
| MCP-D-FR-002 | conformance report 必须包含每个 supported version 的 phase/result coverage。 | P0 |
| MCP-D-FR-003 | `validate_mcp_runtime_conformance_report()` 必须拒绝缺任一 supported version 的 report。 | P0 |
| MCP-D-FR-004 | conformance gate 必须继续检查 batch rejection、raw id redaction、safe diagnostics。 | P0 |
| MCP-D-FR-005 | MCP PRD 总览必须区分：legacy 2025-11-25 long-task Rust sidecar phase 与新 multi-version client compatibility PRD。 | P0 |
| MCP-D-FR-006 | backend MCP Runtime PRD 必须指向 compatibility matrix，而非只声明 latest spec。 | P1 |
| MCP-D-FR-007 | sidecar contract 必须明确当前 external protocol 单值状态和 future multi-version expansion path。 | P0 |
| MCP-D-FR-008 | CHANGELOG 必须记录文档和 gate 口径变更。 | P0 |

## 6. 非功能需求

| 类型 | Requirement |
|---|---|
| 证据完整性 | 本地 fixture/conformance 只能证明 repo-local compatibility，不能伪造成 production promotion / 7 天 shadow / ops drill。 |
| 可追溯 | 每个 supported version 的 conformance result 必须能追溯到 fixture、fake server 和测试命令。 |
| 兼容性 | 旧 PRD 中关于 `2025-11-25` long-task / tasks 的描述不能被删除，只能被重新定位为 latest-feature / Rust canonical target。 |
| 安全 | Gate schema 不得要求或保存 raw endpoint、session id、token、Last-Event-ID、progressToken。 |
| 可维护 | 所有 MCP compatibility PRD 入口必须从 `docs/prd/MCP/README.md` 可发现。 |

## 7. Conformance schema 草案

```json
{
  "schema_version": "maf.mcp.client_compatibility_conformance.v1",
  "supported_mcp_spec_versions": [
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25"
  ],
  "version_results": {
    "2024-11-05": {
      "initialize": true,
      "transport": true,
      "tools_list": true,
      "tools_call": true,
      "batch_rejected": true,
      "raw_id_redaction_passed": true
    }
  }
}
```

实际 schema 应覆盖四版本全部 key，并允许记录 safe diagnostic reason summary。

## 8. Sidecar 口径

首版建议采用保守口径：

1. Python MCP runtime 可以先实现 multi-version client compatibility。
2. Rust MCP sidecar contract 仍声明当前 sidecar protocol `maf.mcp.sidecar.v1` 与外部 MCP protocol 单值能力，不能宣称 canonical multi-version transport。
3. 如果 implementation plan 决定 sidecar contract 同步扩展，必须新增字段而不是复用单值字段：

```json
{
  "external_mcp_protocol_versions": [
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25"
  ]
}
```

4. enforce 模式下如果 required sidecar feature 未声明 multi-version support，必须 fail closed 或保持 Python legacy path 为 visible result。

## 9. 文档同步范围

| 文档 | 更新方向 |
|---|---|
| `docs/prd/MCP/README.md` | 新增 compatibility PRD 入口，说明其为 client multi-version 兼容轨道。 |
| `docs/prd/MCP/00-MCPRuntime联合改造总览PRD.md` | 将 single `2025-11-25` invariant 改为 latest-feature invariant + multi-version compatibility invariant。 |
| `docs/prd/backend/14-MCPRuntime实现需求PRD.md` | 增加四版本 client compatibility matrix 章节，保留 2025-11-25 latest features 说明。 |
| `docs/prd/backend/17-MCP长任务流式SSEPRD.md` | 明确长任务/tasks 仍是 `2025-11-25` feature，不是四版本普通 tools 兼容的首版目标。 |
| `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` | 标注 sidecar canonical multi-version transport 仍需后续 feature evidence。 |
| `CHANGELOG.md` | 记录文档和 gate 口径变更。 |

## 10. 错误处理

| 场景 | 行为 |
|---|---|
| conformance report 缺版本 | gate fail closed。 |
| 某版本 `tools_call` 未通过 | gate fail closed。 |
| batch rejection 未通过 | gate fail closed。 |
| raw id redaction 未通过 | gate fail closed。 |
| sidecar 宣称 multi-version 但 missing required feature | sidecar compatibility fail closed。 |
| 文档未同步 README / CHANGELOG | PR review blocker。 |

## 11. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-D-AC-001 | `conformance_matrix.json` 使用 `supported_mcp_spec_versions` 并包含四版本。 | fixture test |
| MCP-D-AC-002 | conformance gate 缺任一版本时失败。 | gate unit test |
| MCP-D-AC-003 | conformance gate 检查每版本 initialize、transport、tools/list、tools/call。 | gate unit test |
| MCP-D-AC-004 | batch rejection 与 redaction 仍为全版本硬门禁。 | gate unit test |
| MCP-D-AC-005 | MCP README 指向 compatibility PRD 目录。 | docs marker test 或 review checklist |
| MCP-D-AC-006 | backend MCP Runtime PRD 不再只声明 single latest baseline。 | docs review |
| MCP-D-AC-007 | sidecar external protocol 口径不再与 Python multi-version claim 冲突。 | sidecar contract/facade test or documented limitation |
| MCP-D-AC-008 | `CHANGELOG.md` 记录变更。 | git diff review |

## 12. 测试计划

- `tests/integrations/mcp/test_mcp_runtime_gates.py`
- `tests/integrations/mcp/test_prd05_mcp_runtime_evidence.py`
- `tests/integrations/mcp/test_phase0_contract_artifacts.py`
- 新增 docs marker check 脚本或 lightweight unittest，确认 README 入口和 supported versions 文档存在。

## 13. 风险与假设

| 类型 | 内容 | 处理 |
|---|---|---|
| 风险 | 文档从 `2025-11-25` 单基线调整到四版本时误删 long-task/latest-feature 约束。 | 保留原 long-task PRD，只新增 compatibility 轨道。 |
| 风险 | conformance gate 通过被误解为 production readiness。 | PRD 和 evidence ledger 明确本地 conformance 不等于 production shadow/promote。 |
| 风险 | sidecar contract 字段从单值到多值破坏兼容。 | 若改 contract，新增字段并保留旧字段过渡。 |
| 假设 | A/B/C 已经定义 runtime behavior 和测试边界。 | D 只收敛证据和文档口径。 |

## 14. 参考

- `docs/prd/MCP/compatibility/README.md`
- `docs/prd/rust/evidence/prd05/mcp_runtime_release_gates.json`
- `src/integrations/mcp/mcp_runtime_gates.py`
- `tests/fixtures/mcp/contracts/conformance_matrix.json`
