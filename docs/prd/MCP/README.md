# MCP Runtime 联合改造 Phase PRD 索引

本目录承接两个已冻结基线的联合实施拆分：

1. `docs/prd/backend/17-MCP长任务流式SSEPRD.md`：定义 MCP 长任务、完整流式 SSE、断线恢复、progress、task status、取消、最终结果获取与 API/SSE 事件桥接。
2. `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md`：定义 MCP Runtime 最终生产承载方式为独立 Rust sidecar，Python 只保留 facade / sidecar client / capability wrapper。

## 1. 总原则

Phase 是工程落地顺序和验收门禁，不是产品版本、降级目标或临时路线。所有 Phase 全部完成并通过门禁后，才可以宣称本项目具备生产级 MCP 长任务流式 SSE Runtime。

本目录内 PRD 不替代上述两个冻结基线；如果出现冲突，以后续明确更新过的冻结基线和本目录总览 PRD 为准，并必须同步更新 `CHANGELOG.md`。

所有 Phase 必须遵守 `00-MCPRuntime联合改造总览PRD.md` 第 8 节的 MCP 2025-11-25 标准一致性不变量；任一 Phase PRD 没有重复书写某条标准规则，不代表该 Phase 可以不遵守。

## 2. Phase 列表

| Phase | PRD | 目标 | 完成后允许宣称 |
|---|---|---|---|
| Phase 0 | `01-Phase0-协议契约夹具与验收基线PRD.md` | 固定 MCP 2025-11-25 fixtures、proto / error / event contract、fake server 与失败测试 | 具备可执行验收基线，不具备运行时能力 |
| Phase 1 | `02-Phase1-Sidecar契约与PythonFacadePRD.md` | 建立 Python ↔ Rust MCP sidecar gRPC / protobuf 契约、facade、mode 与 compatibility handshake | 具备 sidecar 接入骨架，不具备完整 MCP 长任务能力 |
| Phase 2 | `03-Phase2-StreamableHTTP与SSE内核PRD.md` | 在 Rust sidecar 中实现 Streamable HTTP、多事件 SSE、router、tracker、GET stream、reconnect | 具备流式协议内核，不具备完整 task 状态治理 |
| Phase 3 | `04-Phase3-Tasks长任务状态与DurableRegistryPRD.md` | 实现 task-augmented tools/call、tasks/get/result/list/cancel、durable registry 与恢复 | 具备 MCP 长任务状态治理内核，不代表前端已完整可见 |
| Phase 4 | `05-Phase4-API事件桥接取消与Executor集成PRD.md` | 接入 MCP executor、live event bridge、API/SSE、取消传播、CapabilityExecutionResult 映射 | 具备用户可见长任务闭环，可进入 shadow |
| Phase 5 | `06-Phase5-ShadowEnforce生产门禁与Legacy下线PRD.md` | shadow compare、SLO、安全、运维、rollback drill、enforce 与 Python legacy 下线 | 具备最终生产级 MCP Runtime 能力 |

## 3. 总览入口

联合实施总览见：`00-MCPRuntime联合改造总览PRD.md`。

## 4. 维护要求

新增或修改本目录 PRD 时，应同步检查：

1. `docs/prd/backend/17-MCP长任务流式SSEPRD.md` 是否仍与 Phase 划分一致；
2. `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` 是否仍与 Phase 划分一致；
3. `docs/prd/README.md` 是否仍能指向本目录；
4. `CHANGELOG.md` 是否记录当天文档变更。
