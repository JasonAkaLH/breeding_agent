# MCP Client 官方 SDK 引入与远程 HTTP 普通 tools 四版本兼容 PRD 索引

本目录承接 `docs/superpowers/specs/2026-05-19-mcp-official-sdk-client-compatibility-design.md`，把“引入官方 Rust SDK、保持 client-only runtime 边界、在远程 HTTP/HTTPS ordinary tools 范围内兼容四个 MCP 协议版本”的工作拆成 4 个可独立评审、实施和验收的 PRD。

覆盖协议版本：

- `2024-11-05`
- `2025-03-26`
- `2025-06-18`
- `2025-11-25`

本目录记录的是当前已实现的四版本 SDK/adapter 基线，不代表官方 SDK 当前已经适配 `2026-07-28`。第五版本的产品目标、无状态 lifecycle、MRTR、Tasks Extension 与放量门禁由 `../user-scoped-on-demand/` 三阶段 PRD 管理；实施时必须先验证所选官方 SDK 版本是否具备对应 Wire Contract，缺口由受控 Adapter 补齐，不得靠版本字符串冒充支持。

## 总原则

1. 本项目在该轨道中仍然只是 **MCP Client**，不扩展为 MCP Server。
2. 官方 SDK 只作为协议 / transport 执行层，不能接管 CapabilityRegistry、Planner 权限、租户/用户上下文、审计、脱敏、release gate 或 rollout 策略。
3. `mcp_test.json` 只是非规范真实 smoke 样本；实现不得包含 server-specific 特调。
4. HTTP 是一等支持 transport；`http://` 默认可用，但必须进入 `plaintext_http` 受控明文安全模式。
5. 真实 `mcp_server_config.json` 可能包含业务 header value，不应提交；仓库只提交 `mcp_server_config.example.json`。
6. 本目录首批交付聚焦远程 HTTP/HTTPS MCP client ordinary tools 链路；`stdio` 仍按当前仓库约束保留为 sandbox-gated 后续轨道，未完成 stdio sandbox 前不得宣称 all-transport conformance。

## PRD 列表

| 顺序 | PRD | 目标 | 完成后允许宣称 |
|---|---|---|---|
| 1 | `01-MCPServerConfig与AdapterContractPRD.md` | 建立独立 MCP server 注册配置、config-driven headers、HTTP plaintext mode 与统一 `MCPClientAdapter` contract | Runtime 具备 SDK 接入前的稳定配置/adapter 边界 |
| 2 | `02-2024LegacyHTTPSSE完整协议PRD.md` | 补齐 `2024-11-05` legacy HTTP+SSE 持久 SSE reader、POST endpoint 与 request id correlation | 2024 legacy 普通 tools 链路具备完整协议形态覆盖 |
| 3 | `03-OfficialRustSDKAdapterShadowComparePRD.md` | 已在 Rust sidecar 中引入官方 Rust SDK / `rmcp 1.7.0`，实现 metadata / compile-time marker / 2025+ Streamable HTTP SDK-backed initialize/list/call/close/diagnostics / shadow compare，不直接 enforce | 官方 SDK 已进入正确边界，可与 Python legacy path 对照验证；2024 legacy HTTP+SSE 仍走 Python legacy visible path；production enforce 仍由 PRD-4 / PRD05 gate 控制 |
| 4 | `04-四版本ConformanceGate与EnforceRolloutPRD.md` | 已建立四版本 conformance evidence、adapter shadow/enforce allowlist、PRD05 ledger 与非规范 smoke 脚本，并显式标注 official SDK 2024 legacy 组合 skipped | 可按 `version + transport + adapter` 组合逐步推进，但当前 production gates 仍 pending/fail-closed |

## 依赖关系

```text
PRD 1：MCP Server Config + Adapter Contract
  ↓
PRD 2：2024 Legacy HTTP+SSE 完整协议
  ↓
PRD 3：Official Rust SDK Adapter shadow
  ↓
PRD 4：四版本 Conformance Gate + Enforce Rollout
```

PRD 3 的 SDK 依赖评估可以提前准备，但不得在 PRD 1 的 adapter contract 与配置安全边界完成前接入业务 runtime。

## 与既有 MCP PRD 的关系

- `docs/prd/MCP/compatibility/`：现有 client 四版本兼容轨道，重点是已落地/待收敛的协议协商、2024 legacy、2025+ streamable 与 conformance 口径。
- `docs/prd/MCP/user-scoped-on-demand/`：已批准的第五版本目标轨道，新增 `2026-07-28`，当前待实施。
- 本目录：新的中长期官方 SDK 引入轨道，重点是 `mcp_server_config.json`、统一 adapter contract、官方 Rust SDK shadow/enforce rollout。
- `docs/prd/MCP/` Phase 0-5：仍是 MCP 长任务 / Rust sidecar canonical runtime 的阶段性交付，不被本目录取代。

## 维护要求

修改本目录任一 PRD 时，同步检查：

1. `docs/prd/MCP/README.md` 是否仍准确指向本目录；
2. `docs/prd/README.md` 是否仍准确描述 MCP PRD 入口；
3. `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` 和 PRD05 evidence 是否仍与 SDK sidecar rollout 口径一致；
4. `CHANGELOG.md` 是否记录当天文档变更。
