# PRD-A：MCP Client 协议版本与协商内核

- **状态**：已实现（仓库内，待提交；后续 PRD-B 已基于本内核接入）
- **日期**：2026-05-19
- **范围**：Python MCP client / config / runtime session state / feature gate 基础接口
- **上游设计**：`docs/superpowers/specs/2026-05-19-mcp-four-version-client-compatibility-matrix-design.md`
- **依赖**：当前 `src/integrations/mcp/` 单版本实现与 MCP Phase 0/1 基线
- **非目标**：不实现 2024 legacy HTTP+SSE；不改 Streamable HTTP 行为；不启用 resources/prompts/tasks/stdio/OAuth

## 1. 问题陈述

立项时 MCP Runtime 使用单一 `MCP_PROTOCOL_VERSION = "2025-11-25"`，server config 只接受该版本，client initialize 后也要求 server 返回同一版本。这使原实现确定但无法安全支持 `2024-11-05`、`2025-03-26` 与 `2025-06-18`；本 PRD 的当前实现证据见第 5 节。

要支持四版本兼容，第一步必须把协议版本从全局常量变成 **session negotiated state**。否则后续 transport 和 feature gate 会继续读取全局版本，导致 2024 legacy transport、2025+ Streamable HTTP 和 future sidecar enforce 口径互相污染。

## 2. 目标

1. 定义四版本支持集合：`2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`。
2. 保留 runtime default candidate 为 `2025-11-25`，但只用于未显式 pin 的 initialize 请求。
3. `initialize.params.protocolVersion` 必须始终发送。
4. `InitializeResult.protocolVersion` 必须写入 session state，作为后续 transport / feature gate 的唯一版本来源。
5. 明确 config pin 规则：显式 pin 的 server 如果返回不同版本，fail closed。
6. 提供集中式 protocol / transport / feature gate helper，避免 executor 或 planner 中散落版本判断。

## 3. 非目标

1. 不实现 `legacy_http_sse` transport。
2. 不修改 HTTP POST/GET 具体发送逻辑。
3. 不实现 stdio sandbox。
4. 不公开 resources、prompts、tasks、roots、sampling、elicitation。
5. 不调整 production enforce 或 Rust sidecar artifact trust gate。

## 4. 用户、系统与影响面

| Actor / system | 影响 |
|---|---|
| MCP Runtime config 作者 | 可以为 server 显式配置 protocol version；错误配置会 fail closed。 |
| MCP Client runtime | 从全局版本常量迁移到 session negotiated version。 |
| MCPToolExecutor | 后续只能消费已协商 session 与 feature gate 结果，不自行判断版本。 |
| API runtime / capability registry | discovery 成功后注册 capability；失败按 optional/required server 策略处理。 |
| Rust MCP sidecar | 本 PRD 不要求 sidecar 支持多版本 transport，但要避免 Python-side 设计与 sidecar contract 冲突。 |

## 5. 当前证据

| 文件 | 当前事实 |
|---|---|
| `src/integrations/mcp/protocol.py` | 已定义四版本 supported set、`DEFAULT_MCP_PROTOCOL_VERSION`、transport family gate、feature gate 与 `MCPNegotiatedSession`。 |
| `src/integrations/mcp/config.py` | `MCPServerConfig.from_mapping()` 已区分显式 pin 与默认候选；validation 已拒绝未知版本和不兼容 transport/version 组合。 |
| `src/integrations/mcp/client.py` | initialize 始终发送 requested `protocolVersion`；初始化后保存 negotiated version，并在后续 request/notification/response/GET stream 使用 negotiated version。 |
| `src/integrations/mcp/runtime_state.py` | refresh diagnostic 已携带 safe version/transport/required 字段；required 失败 fail closed，optional 失败保留 diagnostic。 |
| `tests/fixtures/mcp/messages/versions/<version>/` | 已补齐四版本 initialize request/result fixtures。 |
| `tests/integrations/mcp/test_protocol_version_negotiation.py`、`tests/integrations/test_mcp_client.py`、`tests/integrations/test_mcp_runtime_state.py` | 已覆盖四版本集合、config gate、pinned/unpinned negotiation、transport gate、feature gate 与 optional/required 失败路径。 |

## 6. 功能需求

| ID | Requirement | Priority |
|---|---|---|
| MCP-A-FR-001 | `SUPPORTED_MCP_PROTOCOL_VERSIONS` 必须包含四个官方版本。 | P0 |
| MCP-A-FR-002 | `DEFAULT_MCP_PROTOCOL_VERSION` 必须保留为 `2025-11-25`，并只表示 initialize 默认候选。 | P0 |
| MCP-A-FR-003 | `MCPServerConfig.protocol_version` 为空时使用默认候选；显式配置时必须在四版本集合内。 | P0 |
| MCP-A-FR-004 | `initialize.params.protocolVersion` 必须始终存在。 | P0 |
| MCP-A-FR-005 | client 必须保存 `requested_protocol_version` 与 `negotiated_protocol_version`。 | P0 |
| MCP-A-FR-006 | 显式 pin server 的 negotiated version 如果不同于 requested version，必须 fail closed。 | P0 |
| MCP-A-FR-007 | 未显式 pin server 可接受任一 supported negotiated version，但该 session 后续不得切换版本。 | P1 |
| MCP-A-FR-008 | transport family compatibility gate 必须先以 helper 形式存在：2024 仅允许 `legacy_http_sse` / `stdio`，2025+ 仅允许 `streamable_http` / `stdio`。 | P0 |
| MCP-A-FR-009 | feature gate helper 必须能回答普通 tools、batch、resources/prompts/tasks、server-to-client request 的版本状态。 | P0 |
| MCP-A-FR-010 | optional server 协商失败时跳过并记录 safe diagnostic；required server 失败时使 refresh/startup fail closed。 | P0 |

## 7. 非功能需求

| 类型 | Requirement |
|---|---|
| 安全 | protocol version、transport、endpoint、auth 不得来自 LLM、Planner 或用户消息。 |
| 可维护性 | 所有版本判断集中在 protocol/session/gate 模块，不散落到 planner prompt、executor 或 API route。 |
| 可观测 | 诊断必须包含 server_id、requested version、negotiated version、transport family、required/optional outcome 与 reason code；不得包含 raw endpoint secret。 |
| 兼容性 | 旧的 `MCP_PROTOCOL_VERSION` import 可作为 default alias 保留，但新增代码必须读 session negotiated version。 |
| 可测试性 | 每条功能需求必须有 unittest 或 integration test 覆盖。 |

## 8. 数据模型与接口

新增或等价扩展：

```python
@dataclass(slots=True, frozen=True)
class MCPNegotiatedSession:
    server_id: str
    requested_protocol_version: str
    negotiated_protocol_version: str
    transport_family: str
    server_capabilities: Mapping[str, Any]
    server_info: Mapping[str, Any]
    pinned_protocol_version: bool
    session_id: str | None = None
    legacy_post_endpoint: str | None = None
    last_event_id: str | None = None
```

建议 helper：

```text
validate_mcp_protocol_version(value: str) -> str
is_transport_family_allowed(version: str, family: str) -> bool
mcp_feature_status(version: str, feature: str) -> CompatibilityStatus
```

## 9. 错误处理

| 场景 | 行为 |
|---|---|
| config version 未知 | config validation error；server 不进入 discovery。 |
| initialize result 缺 `protocolVersion` | protocol error；optional skip / required fail。 |
| initialize 返回 unsupported version | protocol error；optional skip / required fail。 |
| pinned version 与 negotiated version 不同 | protocol error；optional skip / required fail。 |
| negotiated version 与 transport family 不兼容 | protocol error；optional skip / required fail。 |
| batch message | protocol error；本 PRD 只要求 gate 明确拒绝，不要求 transport 新实现。 |

## 10. 验收标准

| AC | 验收项 | 验证 |
|---|---|---|
| MCP-A-AC-001 | 四版本常量和 default candidate 被明确定义。 | protocol unit test |
| MCP-A-AC-002 | config 接受四版本并拒绝未知版本。 | config unit test |
| MCP-A-AC-003 | initialize 始终发送 `protocolVersion`。 | client unit test |
| MCP-A-AC-004 | client 保存 requested 与 negotiated version。 | client/session unit test |
| MCP-A-AC-005 | pinned server 返回不同版本时 fail closed。 | client integration test |
| MCP-A-AC-006 | unpinned server 可协商到任一 supported version。 | client integration test |
| MCP-A-AC-007 | transport family gate 拒绝 2024 + streamable_http、2025+ + legacy_http_sse。 | gate unit test |
| MCP-A-AC-008 | feature gate 对 batch 返回 not-supported。 | gate unit test |
| MCP-A-AC-009 | optional/required server 失败路径分别跳过或失败。 | runtime_state integration test |

## 11. 测试计划

- `tests/integrations/mcp/test_protocol_version_negotiation.py`
- `tests/integrations/test_mcp_client.py`
- `tests/integrations/test_mcp_runtime_state.py`
- versioned initialize fixtures：`tests/fixtures/mcp/messages/versions/<version>/initialize_request.json`、`initialize_result.json`。

## 12. 风险与假设

| 类型 | 内容 | 处理 |
|---|---|---|
| 假设 | 本 PRD 只建立协商内核，不保证任一新增版本 transport 已可用。 | README 和验收声明中明确。 |
| 风险 | 保留 `MCP_PROTOCOL_VERSION` alias 可能让新代码继续误用全局版本。 | 新测试应检查关键 runtime 路径读取 negotiated session version。 |
| 风险 | Rust sidecar external protocol 仍是单值。 | PRD-D 处理 sidecar 口径；本 PRD 不宣称 sidecar canonical multi-version。 |

## 13. 参考

- `docs/superpowers/specs/2026-05-19-mcp-four-version-client-compatibility-matrix-design.md`
- MCP lifecycle `2024-11-05`：https://modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle
- MCP changelog `2025-03-26`：https://modelcontextprotocol.io/specification/2025-03-26/changelog
