# MCP Runtime 联合改造 Phase PRD 索引

本目录承接两个已冻结基线的联合实施拆分：

1. `docs/prd/backend/17-MCP长任务流式SSEPRD.md`：定义 MCP 长任务、完整流式 SSE、断线恢复、progress、task status、取消、最终结果获取与 API/SSE 事件桥接。
2. `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md`：定义 MCP Runtime 最终生产承载方式为独立 Rust sidecar，Python 只保留 facade / sidecar client / capability wrapper。

## 1. 总原则

Phase 是工程落地顺序和验收门禁，不是产品版本、降级目标或临时路线。所有 Phase 全部完成并通过门禁后，才可以宣称本项目具备生产级 MCP 长任务流式 SSE Runtime。

本目录内 PRD 不替代上述两个冻结基线；如果出现冲突，以后续明确更新过的冻结基线和本目录总览 PRD 为准，并必须同步更新 `CHANGELOG.md`。

所有 Phase 必须遵守 `00-MCPRuntime联合改造总览PRD.md` 第 8 节的版本化 invariant。当前 Python Client/Gateway 已实现 `2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25 / 2026-07-28` 五版本普通 tools 基线；Rust Sidecar 仍只声明已验证的 `2025-11-25` 路径。

## 2. Phase 列表

| Phase | PRD | 目标 | 完成后允许宣称 |
|---|---|---|---|
| Phase 0 | `01-Phase0-协议契约夹具与验收基线PRD.md` | 固定 MCP 2025-11-25 fixtures、proto / error / event contract、fake server 与失败测试 | 已落地：具备可执行验收基线，不具备运行时能力 |
| Phase 1 | `02-Phase1-Sidecar契约与PythonFacadePRD.md` | 建立 Python ↔ Rust MCP sidecar gRPC / protobuf 契约、facade、mode 与 compatibility handshake | 已落地：具备 sidecar 接入骨架，不具备完整 MCP 长任务能力 |
| Phase 2 | `03-Phase2-StreamableHTTP与SSE内核PRD.md` | 在 Rust sidecar 中实现 Streamable HTTP、多事件 SSE、router、tracker、GET stream、reconnect | 具备流式协议内核，不具备完整 task 状态治理 |
| Phase 3 | `04-Phase3-Tasks长任务状态与DurableRegistryPRD.md` | 实现 task-augmented tools/call、tasks/get/result/list/cancel、durable registry 与恢复 | 具备 MCP 长任务状态治理内核，不代表前端已完整可见 |
| Phase 4 | `05-Phase4-API事件桥接取消与Executor集成PRD.md` | 接入 MCP executor、live event bridge、API/SSE、取消传播、CapabilityExecutionResult 映射 | 具备用户可见长任务闭环，可进入 shadow |
| Phase 5 | `06-Phase5-ShadowEnforce生产门禁与Legacy下线PRD.md` | shadow compare、SLO、安全、运维、rollback drill、enforce 与 Python legacy 下线 | 具备最终生产级 MCP Runtime 能力 |


## 3. Client 四版本兼容 PRD 轨道

`docs/prd/MCP/compatibility/` 是新增的 MCP Client 四版本兼容实施轨道，覆盖 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`。该轨道只处理本项目作为 MCP Client 连接外部 MCP Server 的兼容矩阵，不把本项目扩展为 MCP Server。

该轨道与上方 Phase 0-5 的关系：

- Phase 0-5 仍描述 MCP 长任务 / Rust sidecar canonical runtime 的阶段性交付。
- compatibility PRD 轨道补充普通 `tools/list` / `tools/call` 的多版本 client 兼容前置工作。
- compatibility PRD-D 已把 conformance gate、fixtures evidence 与 sidecar 口径同步为 client multi-version compatibility：`2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25`。

入口：`docs/prd/MCP/compatibility/README.md`。


## 4. 官方 SDK 引入与四版本完整兼容 PRD 轨道

`docs/prd/MCP/official-sdk-compatibility/` 是新增的 MCP Client 官方 SDK 中长期引入轨道，承接 `docs/superpowers/specs/2026-05-19-mcp-official-sdk-client-compatibility-design.md`。该轨道将官方 Rust SDK / `rmcp` 限定在 Rust sidecar adapter 内，保持 CapabilityRegistry、Planner 权限、租户/用户 header policy、审计脱敏、release gate 与 rollback 仍由本项目 runtime 控制。首批交付聚焦远程 HTTP/HTTPS ordinary tools；当前 `stdio` 仍按 sandbox-gated 后续轨道处理，未完成前不得宣称 all-transport conformance。

当前 PRD-3 / PRD-4 的仓库内实现已经落地：`rmcp 1.7.0` 作为 client-only Streamable HTTP 依赖进入 Rust workspace / lockfile，Rust sidecar 已具备 official SDK-backed 2025+ Streamable HTTP initialize/list/call/close/diagnostics shadow adapter；四版本 `version + transport + adapter` conformance、shadow compare、enforce allowlist 与非规范真实 server smoke 口径已同步到 PRD05 evidence ledger。`2024-11-05 + legacy_http_sse` 组合仍由 Python legacy visible path 覆盖，official SDK lane 记录为 skipped/unsupported transport，不得误报为 SDK operational pass。该 repo-local evidence 不满足 Phase 5 production gate；真实 artifact provenance、生产 shadow、benchmark、ops/recovery drill、rollback drill 与 legacy decommission 仍保持 pending/fail-closed。

该轨道分为 4 个 PRD：

1. `01-MCPServerConfig与AdapterContractPRD.md`：独立 `mcp_server_config.json`、config-driven headers、HTTP `plaintext_http` mode 与统一 adapter contract。
2. `02-2024LegacyHTTPSSE完整协议PRD.md`：补齐 `2024-11-05` legacy HTTP+SSE 持久 SSE reader、POST endpoint 与 request id correlation。
3. `03-OfficialRustSDKAdapterShadowComparePRD.md`：在 Rust sidecar 引入官方 Rust SDK adapter，并只做 shadow compare。
4. `04-四版本ConformanceGate与EnforceRolloutPRD.md`：建立 `version + transport + adapter` conformance / shadow / enforce gate。

入口：`docs/prd/MCP/official-sdk-compatibility/README.md`。

## 5. 用户级按需 MCP 三阶段改造

`docs/prd/MCP/user-scoped-on-demand/` 是用户专属 MCP 配置与运行时状态模型改造轨道。它不替代本目录已有的协议、长任务、Rust Sidecar 或版本兼容 PRD，而是在这些能力之上解决用户所有权、凭据存储、按需发现、两级路由、调用授权、低常驻资源与旧全局 Runtime 下线。

该轨道按可独立交付的三个阶段实施：

1. `user-scoped-on-demand/01-用户级MCP配置凭据与按需GatewayPRD.md`：用户级最小配置、凭据密文入库、服务器密钥文件、远程 HTTP(S) 安全和任务级按需 Gateway。
2. `user-scoped-on-demand/02-MCP两级路由授权与任务执行闭环PRD.md`：Server 级主路由、按需 Tool List、专用 Tool Selector、所有工具授权、长调用提示、任务生命周期和前端闭环。
3. `user-scoped-on-demand/03-按需MCP灰度切换与旧Runtime下线PRD.md`：shadow/enforce 灰度、单路径执行、旧配置迁移、回滚门禁和进程级 MCP Runtime 下线。

当前状态：阶段一用户隔离、凭据加密、Endpoint Policy、五版本任务级 Gateway 与流式临时结果已实现；阶段二、三待实施。不得绕过后续授权门禁直接开放模型自动调用。

该轨道同时承接第五版本升级：阶段 1 实现 `2026-07-28` 无状态 Adapter、`server/discover`、每请求 metadata/header、ordinary tools 与结果联合类型；阶段 2 接入 MRTR elicitation 和 Tasks Extension 用户闭环；阶段 3 完成五版本 conformance、灰度与旧 Runtime 下线。现有四版本继续保留。

## 6. 总览入口

联合实施总览见：`00-MCPRuntime联合改造总览PRD.md`。

## 7. 维护要求

新增或修改本目录 PRD 时，应同步检查：

1. `docs/prd/backend/17-MCP长任务流式SSEPRD.md` 是否仍与 Phase 划分一致；
2. `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` 是否仍与 Phase 划分一致；
3. `docs/prd/README.md` 是否仍能指向本目录；
4. `docs/prd/MCP/user-scoped-on-demand/` 是否仍与用户级配置、路由、授权和 Runtime 状态模型一致；
5. `CHANGELOG.md` 是否记录当天文档变更。
