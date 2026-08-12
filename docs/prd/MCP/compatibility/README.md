# MCP Client 多版本兼容 PRD 索引

本目录承接 `docs/superpowers/specs/2026-05-19-mcp-four-version-client-compatibility-matrix-design.md`。A-D 是已经落地的四版本基线；用户级按需 MCP 轨道已批准把 `2026-07-28` 增量加入目标矩阵，但当前代码尚未实现第五个版本。

当前已实现版本：

- `2024-11-05`
- `2025-03-26`
- `2025-06-18`
- `2025-11-25`

新增目标版本：

- `2026-07-28`（无协议 Session 的 Streamable HTTP；见用户级按需 MCP 第 1-3 阶段 PRD）

## 拆分原则

1. 先建立协议版本与 session 协商内核，再接入具体 transport。
2. `2024-11-05` legacy HTTP+SSE 与 `2025-03-26+` Streamable HTTP 分开实现和测试。
3. 普通 `tools/list` / `tools/call` 是首个兼容目标；resources、prompts、tasks、interactive OAuth、roots、sampling、elicitation 与 stdio sandbox 保持 future / config-gated。
4. Conformance gate 与现有 MCP PRD 文档口径必须最后统一收敛，避免 runtime 已多版本但证据账本仍只认 `2025-11-25`。

## PRD 列表

| 顺序 | PRD | 目标 | 完成后允许宣称 |
|---|---|---|---|
| A | `01-协议版本与协商内核PRD.md` | 建立四版本常量、config 校验、mandatory initialize `protocolVersion`、negotiated session state 与 feature gate 基础接口 | 已实现（仓库内，待提交）；Runtime 具备多版本协商内核 |
| B | `02-2024-11-05-LegacyHTTP-SSETransportPRD.md` | 实现 `2024-11-05` legacy HTTP+SSE transport family、fixtures 与 fake server 测试 | 已实现（仓库内，待提交）；可通过 legacy HTTP+SSE 对 2024 server 完成普通 tools 链路 |
| C | `03-2025Plus-StreamableHTTP多版本收敛PRD.md` | 将现有 Streamable HTTP 收敛为 `2025-03-26` / `2025-06-18` / `2025-11-25` 多版本行为 | 已实现（仓库内，待提交）；2025+ 三个版本的普通 tools 链路均受 negotiated version gate 管理 |
| D | `04-ConformanceGate文档同步与Sidecar口径PRD.md` | 更新 conformance gate、fixtures evidence、MCP PRD 口径与 Rust sidecar external protocol 说明 | 已实现（仓库内，待提交）；文档、测试证据与 sidecar 口径不再隐含单一 `2025-11-25` |
| E | `../user-scoped-on-demand/01-用户级MCP配置凭据与按需GatewayPRD.md`（并由阶段 2/3 完成交互与放量） | 新增 `2026-07-28` 无状态 Adapter、`server/discover`、ordinary tools、MRTR、Tasks Extension 与五版本 conformance | 已批准、待实施；完成前不得宣称第五版本运行时支持 |

## 依赖关系

```text
A 协商内核
├── B 2024 legacy HTTP+SSE
├── C 2025+ Streamable HTTP
└── D Conformance / 文档 / sidecar 口径（依赖 A/B/C 的验收定义，可并行准备）
```

## 维护要求

- 修改本目录 PRD 时，同步检查 `docs/prd/MCP/README.md`、`docs/prd/README.md` 与 `CHANGELOG.md`。
- 任一 PRD 不得把本项目扩展为 MCP Server；本目录只覆盖 MCP Client runtime。
- 任一 PRD 不得绕过现有 endpoint/auth/config 安全边界；LLM、Planner 或用户消息不得决定 MCP endpoint、token、transport、protocol version 或 tool identity。
- 当前仓库 `SUPPORTED_MCP_PROTOCOL_VERSIONS` 仍是四版本已实现事实；五版本是已批准的目标态。实现 E 后必须同步常量、fixtures、逐版本 conformance、Sidecar evidence 和本文状态。
- 除已批准的 `2026-07-28` 外，若上游 SDK 暴露其他版本（例如 `2024-10-07`），不得静默扩大范围，必须先补充决策记录、PRD、fixtures 与 conformance gate。
