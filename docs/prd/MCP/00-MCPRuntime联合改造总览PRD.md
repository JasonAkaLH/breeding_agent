# MCP Runtime 联合改造总览 PRD

- **范围**：MCP Runtime / Rust sidecar / Python facade / 长任务流式 SSE / API 事件桥接 / 生产门禁
- **状态**：Phase 拆分已冻结；Phase 0 / Phase 1 基线已落地，Phase 2-5 待完成
- **日期**：2026-05-14
- **基线 PRD**：
  - `docs/prd/backend/14-MCPRuntime实现需求PRD.md`
  - `docs/prd/backend/17-MCP长任务流式SSEPRD.md`
  - `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md`

## 1. 一句话结论

MCP 长任务流式 SSE 与 MCP Runtime Rust sidecar 必须作为同一个最终交付目标联合实现：`17-MCP长任务流式SSEPRD.md` 定义“必须支持什么 MCP 行为”，`05-MCPRuntimeRustSidecarPRD.md` 定义“最终生产边界由 Rust sidecar 承担”。

Phase 只表达工程顺序，不允许把任一中间 Phase 包装成完整 MCP 长任务能力。

截至 2026-05-15，Phase 0 / Phase 1 的契约夹具、proto 草案、Rust sidecar skeleton、Python facade、mode gate 与 compatibility handshake 基线已落地；Phase 2-5 仍是未完成范围。当前不得宣称完整 MCP 长任务流式 SSE Runtime 或 production enforce 已完成。

## 2. 问题陈述

当前 Python MCP Runtime 已具备基础 Streamable HTTP 请求、单条 SSE data 兼容、MCP session / Last-Event-ID / retry 元数据记录、tools/list 与 tools/call 的最小链路。但它尚不具备完整多事件 stream、server-to-client request / notification、request tracker、long task registry、durable recovery、remote cancel propagation 和 API/SSE live event bridge。

同时，Rust 化基线已经冻结 MCP Runtime 最终接入方式为独立 Rust sidecar。若先在 Python 中完整复制一套生产级长任务 Runtime，再迁移到 Rust sidecar，会造成协议内核、task registry、sanitizer、resource limit、typed error 与审计语义重复实现，不符合最终交付版的长期维护目标。

## 3. 总目标

1. 以 MCP 2025-11-25 latest-feature invariant 为长任务 / Tasks / Streamable HTTP 完整能力基线，完整支持 Streamable HTTP、多事件 SSE、GET server-to-client stream、reconnect、progress、tasks、cancel 与 result retrieval；普通 tools client 兼容另受 multi-version client compatibility invariant 约束。
2. 以独立 Rust sidecar 作为最终生产 MCP Runtime 承载层，Python 只保留 config、sidecar client、capability descriptor、executor wrapper、API/SSE bridge 与 DTO adapter。
3. 保持现有用户行为、API/SSE、capability、Skill、artifact 与前端事件契约兼容。
4. 所有外部 MCP 输入、output、status、progress、server-to-client request 均按不可信输入治理，fail closed、脱敏审计、限流和可观测。
5. 生产 `enforce` 前必须通过 shadow compare、durable recovery、resource limit、redaction、compatibility、rollback drill 与 ops runbook 门禁。

## 4. 非目标

1. 不把本项目实现为 MCP Server。
2. 不允许 LLM / Planner / Skill 动态指定 MCP endpoint、token、header、tool name、sidecar endpoint 或 long-task 策略。
3. 不在本联合改造中启用完整 OAuth、sampling、roots、elicitation；`input_required` 在未完成独立安全 PRD 前稳定失败。
4. 不用 Python 生产实现替代最终 Rust sidecar 目标。
5. 不绕过现有 capability 包装、planner allowlist、schema validation、output sanitizer、audit redaction 或 resource limit。

## 5. 最终架构

```text
FastAPI / ApiRuntime
  ├─ Python MCP sidecar client
  ├─ MCPToolExecutor facade
  ├─ API/SSE EventBridge
  └─ existing task / node / conversation lifecycle

Rust MCP sidecar
  ├─ gRPC / protobuf internal API
  ├─ compatibility handshake / version / readiness
  ├─ MCP Streamable HTTP transport
  ├─ SSE parser / message router / request tracker
  ├─ Task client / durable long-task registry
  ├─ schema validation / sanitizer / redaction
  ├─ bundle activation / tool binding cache
  └─ audit / metrics / health / shutdown drain

External MCP servers
  ├─ MCP 2025-11-25 JSON-RPC over Streamable HTTP（long-task / Tasks latest-feature target）
  └─ MCP 2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25 ordinary tools client compatibility
```

## 6. Phase 顺序与依赖

| Phase | 必须依赖 | 产物 |
|---|---|---|
| Phase 0 | 已冻结 PRD 14 / 17 / Rust MCP sidecar PRD | fixtures、contract、fake server、失败测试、验收矩阵 |
| Phase 1 | Phase 0 | proto、Python facade、sidecar mode、health/readiness/version、compatibility handshake |
| Phase 2 | Phase 1 | Rust Streamable HTTP、multi-event SSE、router、tracker、GET stream、reconnect |
| Phase 3 | Phase 2 | MCP Tasks、durable registry、task recovery、remote cancellation 内核 |
| Phase 4 | Phase 3 | MCPToolExecutor facade 集成、API/SSE live event、cancel propagation、final result mapping |
| Phase 5 | Phase 4 | shadow / enforce、ops、SLO、security hardening、legacy decommission |

Phase 之间可以并行准备测试、fixtures、proto 草案和 fake server，但生产能力必须按依赖门禁顺序晋级。

## 7. 跨 Phase 不变量

1. Python-visible API / SSE event schema 不因 Rust sidecar 引入而破坏。
2. Rust sidecar 不对公网、前端、用户、普通 Skill 或外部 MCP server 暴露内部 gRPC endpoint。
3. 外部 MCP server 永远只通过 MCP 标准 transport 与 sidecar 通信，不得直连 Python ↔ sidecar 内部协议。
4. 所有 raw MCP task id、session id、Last-Event-ID、progressToken 只进入受控 registry / audit fingerprint，不进入前端事件原文。
5. side-effecting tool call 不允许自动重复发起；重连只恢复 stream、查询 task 或拉取 result。
6. `input_required` 首版稳定失败；不得让外部 MCP server 直接驱动用户输入或内部能力。
7. `enforce` 模式下安全、权限、schema、sanitizer、sidecar identity、secret、contract mismatch 默认 fail closed。

## 8. MCP latest-feature invariant 与 multi-version client compatibility invariant

以下规则跨所有 Phase 生效，任何实现或测试不得放宽：

- **latest-feature invariant**：MCP `2025-11-25` 是长任务、Tasks、progress、cancellation、完整 Streamable HTTP/SSE 与 Rust sidecar canonical runtime 的 latest-feature invariant。`2025-11-25 long-task / Tasks` 约束不得因四版本普通 tools 兼容而删除或降级。
- **multi-version client compatibility invariant**：本项目作为 MCP Client 的普通 `tools/list` / `tools/call` 首版兼容范围是 `2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25`；2024 使用 legacy HTTP+SSE，2025+ 使用 Streamable HTTP，conformance gate 必须逐版本验证。

1. **JSON-RPC 形态**：所有 MCP data layer message 必须是 JSON-RPC 2.0 object；Streamable HTTP POST body 必须是单个 JSON-RPC request、notification 或 response，不支持 batch array。
2. **Lifecycle first**：每个 MCP session 的第一阶段必须是 `initialize`；client 收到 `InitializeResult` 后必须发送 `notifications/initialized`，随后才能进入 operation。初始化完成前除 `ping` 外不得发送普通 operation request。
3. **Protocol version header**：HTTP 后续请求必须携带协商后的 `MCP-Protocol-Version`；如果 server 返回不支持协议版本，client 必须断开或 fail closed。
4. **Streamable HTTP POST**：client POST 必须声明 `Accept: application/json, text/event-stream`；如果 POST 内容是 JSON-RPC notification 或 response，server 正常接收时应返回 HTTP 202 且无 body。
5. **Streamable HTTP GET**：client GET 独立 stream 必须声明 `Accept: text/event-stream`；server 返回 405 表示不提供 GET stream，不是协议崩溃。
6. **SSE resume**：断线不等于取消；恢复必须使用 HTTP GET + `Last-Event-ID`，不得重新 POST 原始 `tools/call`；server replay 不得跨 stream 混发。
7. **Session management**：server 返回 `MCP-Session-Id` 后，client 后续请求必须携带；带 session 的请求收到 404 时必须重新 initialize；可选 session shutdown 使用 HTTP DELETE，405 表示 server 不支持 client 主动终止 session。
8. **Capabilities**：operation 阶段只能使用 negotiation 成功的 capability；本项目不得声明未实现的 roots、sampling、elicitation 或 client-side tasks capability。
9. **Tasks**：对 server-side `tools/call` 使用 task augmentation 时，必须以 server `capabilities.tasks.requests.tools.call` 和 tool-level `execution.taskSupport` 为准；tool 未声明 `execution.taskSupport` 时按 `forbidden` 处理；不因本项目作为 requestor 就声明 client `capabilities.tasks`。
10. **Progress**：`progressToken` 必须来自 request `_meta`，为 string 或 integer，并在 active request / task 生命周期内唯一；progress 数值必须单调递增。
11. **Cancellation**：普通 in-flight request 使用 `notifications/cancelled`；task-augmented request 必须使用 `tasks/cancel`，不得用 cancellation notification 代替。
12. **Task related metadata**：task 相关消息必须按 MCP 规则携带或省略 `_meta["io.modelcontextprotocol/related-task"]`：普通 task 相关请求 / 响应 / 通知需要关联 metadata；`tasks/get`、`tasks/result`、`tasks/cancel` 请求以 `taskId` 参数为准；`tasks/get`、`tasks/list`、`tasks/cancel` 结果和 `notifications/tasks/status` 不应携带该 metadata；`tasks/result` 结果必须携带该 metadata。前端只看 safe ref，不看 raw metadata。

## 9. 总体验收

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-JOINT-AC-001 | Rust MCP sidecar 支持 PRD17 全部长任务 / 流式 SSE 行为 | Phase 2 / 3 / 4 integration tests |
| MCP-JOINT-AC-002 | Python facade 对外保持 `mcp.*` capability 与 `CapabilityExecutionResult` 兼容 | API / e2e regression |
| MCP-JOINT-AC-003 | API/SSE 能在最终结果前实时输出 progress / status / cancellation | API streaming timing tests |
| MCP-JOINT-AC-004 | durable registry 支持断线和进程恢复后的状态判定 | restart / recovery tests |
| MCP-JOINT-AC-005 | shadow compare 达标后才允许 enforce | shadow metrics + promotion report |
| MCP-JOINT-AC-006 | Rust canonical 后 Python legacy MCP protocol / sanitizer / activation 重复语义下线 | decommission PR + architecture guard |
| MCP-JOINT-AC-007 | MCP 标准一致性不变量均被 contract / integration / fault injection 覆盖 | conformance test matrix |

## 10. 关联文档

- `docs/prd/MCP/01-Phase0-协议契约夹具与验收基线PRD.md`
- `docs/prd/MCP/02-Phase1-Sidecar契约与PythonFacadePRD.md`
- `docs/prd/MCP/03-Phase2-StreamableHTTP与SSE内核PRD.md`
- `docs/prd/MCP/04-Phase3-Tasks长任务状态与DurableRegistryPRD.md`
- `docs/prd/MCP/05-Phase4-API事件桥接取消与Executor集成PRD.md`
- `docs/prd/MCP/06-Phase5-ShadowEnforce生产门禁与Legacy下线PRD.md`
- `docs/prd/MCP/compatibility/README.md`

## 11. 标准参考

- MCP 2025-11-25 Base Protocol：https://modelcontextprotocol.io/specification/2025-11-25/basic
- MCP 2025-11-25 Lifecycle：https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
- MCP 2025-11-25 Transport：https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- MCP 2025-11-25 Tasks：https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks
- MCP 2025-11-25 Progress：https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress
- MCP 2025-11-25 Cancellation：https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation
