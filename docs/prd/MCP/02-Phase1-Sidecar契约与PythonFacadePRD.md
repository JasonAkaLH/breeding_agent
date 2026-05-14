# Phase 1：Sidecar 契约与 Python Facade PRD

- **范围**：Rust MCP sidecar skeleton / Python sidecar client / protobuf / compatibility handshake / mode flag / facade
- **状态**：待实现
- **日期**：2026-05-14
- **前置依赖**：Phase 0

## 1. 目标

Phase 1 建立 Rust MCP sidecar 的内部生产接入骨架，使 Python runtime 可以通过稳定 facade 调用 sidecar，但不要求 sidecar 已完成完整 MCP 长任务协议能力。

## 2. 功能需求

1. 创建 `native/` Rust workspace 与 `maf_mcp_runtime` crate / sidecar binary。
2. 创建 `native/proto/maf/mcp/v1/` protobuf schema，并复用 `native/proto/maf/common/v1/`。
3. 实现 Python sidecar client：connect、health、readiness、version、compatibility handshake、deadline、typed error mapping。
4. 实现 `MAF_RUST_MCP_RUNTIME_MODE=off|shadow|enforce`：默认 `off`。
5. 保持 `MCPToolExecutor` 对外 contract 不变；Python 仍暴露 `mcp.*` capability。
6. sidecar readiness 必须在 compatibility handshake 通过后才为 ready。
7. sidecar inbound 只允许内部访问；endpoint 只能来自部署配置 / runtime allowlist。
8. `shadow` 下 Python legacy path 仍是用户可见结果来源，sidecar 只做旁路检查。
9. `enforce` 下 sidecar unavailable / incompatible / identity mismatch 默认 fail closed，除非对应 PRD 明确允许安全 fallback。
10. Python ↔ sidecar protobuf contract 必须显式区分内部 sidecar protocol version 与外部 MCP server `protocolVersion`；二者不得混用。
11. sidecar `supported_features` 必须区分 `streamable_http`, `sse_stream`, `server_to_client_get`, `mcp_tasks`, `task_augmented_tools_call`, `remote_cancel` 等能力；未声明的 feature 不得被 Python facade 调用。
12. Python facade 不得通过 sidecar contract 传入由用户、LLM、Planner 或 Skill 动态生成的 external MCP endpoint、header、token 或 sidecar endpoint。

## 3. 非目标

1. 不实现完整 Streamable HTTP SSE 内核。
2. 不实现 MCP Tasks durable registry。
3. 不切换生产用户请求到 Rust sidecar enforce。
4. 不下线 Python legacy MCP runtime。

## 4. Python facade 边界

Python 保留：

1. runtime config 注入；
2. capability descriptor 注册；
3. executor wrapper 与 `CapabilityExecutionResult` 映射；
4. API/SSE 事件桥接；
5. sidecar client、mode、shadow compare 接入点。

Python 不应长期保留：

1. MCP JSON-RPC 协议解析 canonical 语义；
2. output sanitizer canonical 语义；
3. bundle activation canonical 语义；
4. long-task durable registry canonical 语义。

## 5. Sidecar version response

version / readiness response 至少包含：

| 字段 | 说明 |
|---|---|
| `component` | 固定为 MCP runtime sidecar component id |
| `build_version` | binary / image build version |
| `protocol_version` | Python ↔ sidecar proto protocol version |
| `schema_hash` | proto / JSON schema hash |
| `error_code_table_hash` | typed error table hash |
| `supported_features` | 支持特性，如 `short_call`, `streamable_http`, `tasks` |
| `min_client_version` / `max_client_version` | Python client compatibility range |

Phase 1 的 proto 必须预留 MCP 标准字段承载能力，包括 external MCP `protocolVersion`、server capabilities、tool-level `execution.taskSupport`、session id fingerprint、last event id fingerprint、progress token fingerprint、task safe ref、remote error 与 JSON-RPC id correlation；raw secret 和 raw endpoint 不得作为普通 response 字段返回 Python。

## 6. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | health/version/readiness、typed error、config validation |
| Python unit | sidecar client deadline、connect failure、compatibility failure、typed error mapping |
| Integration | dev launcher / externally managed sidecar fixture、shadow mode no user-visible impact |
| Security | public bind denial、endpoint allowlist、identity mismatch fail closed |

## 7. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-P1-AC-001 | Python 可连接 sidecar 并完成 compatibility handshake | integration test |
| MCP-P1-AC-002 | `off|shadow|enforce` mode 生效且默认 off | config tests |
| MCP-P1-AC-003 | sidecar 不兼容时 readiness false，Python 不误用 | compatibility tests |
| MCP-P1-AC-004 | MCPToolExecutor 外部 API 行为不变 | existing regression tests |
| MCP-P1-AC-005 | shadow mode 不影响用户可见结果 | API/e2e regression |
| MCP-P1-AC-006 | 内部 sidecar protocol 与外部 MCP protocol 版本不会混淆 | compatibility / contract tests |
| MCP-P1-AC-007 | 未声明 supported feature 时 Python facade 不会调用对应能力 | feature gate tests |

## 8. 退出门禁

Phase 1 通过后，只能说明 sidecar 接入骨架可用。不得宣称 Rust MCP Runtime 已支持完整 MCP protocol 或长任务。
