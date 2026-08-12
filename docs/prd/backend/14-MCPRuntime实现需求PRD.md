# MCP Runtime 实现需求 PRD

- **范围**：后端 / MCP client runtime / capability 接入 / 外部工具治理
- **文档状态**：Phase 1 已实现（2026-05-12）；长任务 / 完整流式 SSE 扩展见 `docs/prd/backend/17-MCP长任务流式SSEPRD.md`
- **日期**：2026-05-12
- **协议参考版本**：当前代码为 `2025-11-25` latest features + 四版本 client compatibility；目标增加 `2026-07-28` 第五版本（见 `docs/prd/MCP/user-scoped-on-demand/`）

## 1. 背景

当前系统已经具备主代理、数据查询 Skill、Skill 一等 capability、动态 Skill bundle、LLM Planner、runtime replanner、API/SSE 与状态存储基线。现阶段外部系统接入仍以单点 adapter 为主，例如 MySQL 只读适配器、LLM provider、Agent Skill 兼容层。

后续如果需要接入来自外部 MCP server 的工具，不能把 MCP tool 直接作为 orchestration 内核概念暴露给 Planner。MCP tool 本质上是外部 server 暴露的可调用原语；本项目的稳定编排单位仍应是 **capability**。因此需要新增 MCP Runtime，把 MCP 的连接、发现、调用、鉴权、输出治理和审计收口到外部适配层，再通过受控 capability 包装进入现有编排系统。

## 2. 官方协议要点摘要

本 PRD 仅摘取与本项目实现边界直接相关的 MCP 要点：

1. MCP 采用 Host / Client / Server 架构；一个 host 可以为多个 MCP server 建立各自的 MCP client 连接。
2. MCP 分为 data layer 与 transport layer；data layer 使用 JSON-RPC，包含 lifecycle、tools、resources、prompts、notifications 等原语；transport layer 负责连接、消息传输和鉴权。
3. Server 可暴露三类核心 server primitives：tools、resources、prompts；其中 tools 是可执行函数，resources 是上下文数据源，prompts 是可复用提示模板。
4. Tools 通过 `tools/list` 发现，通过 `tools/call` 调用；tool 定义包含 name、description、inputSchema、可选 outputSchema 与 annotations。
5. Tool annotations 不能天然信任，除非来自受信 server。
6. Streamable HTTP 是当前标准 transport 之一，支持 HTTP POST、可选 SSE、session id 与协议版本 header；远程 server 鉴权通常在 transport 层处理。
7. MCP tool 调用需要安全治理：输入校验、访问控制、限流、输出清洗、敏感操作的人类确认、超时与审计。

参考：
- MCP 架构概览：https://modelcontextprotocol.io/docs/learn/architecture
- MCP 2025-11-25 Base Protocol：https://modelcontextprotocol.io/specification/2025-11-25/basic
- MCP 2025-11-25 Lifecycle：https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
- MCP 2025-11-25 Transport：https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- MCP 2025-11-25 Tools：https://modelcontextprotocol.io/specification/2025-11-25/server/tools
- MCP 2025-11-25 Authorization：https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- MCP 2025-11-25 Key Changes：https://modelcontextprotocol.io/specification/2025-11-25/changelog

### 2.0A 当前四版本基线与五版本目标矩阵

本 PRD 的 v1 普通 MCP tools 链路不再只声明 single latest baseline。仓库内 MCP Client 兼容目标以 `docs/prd/MCP/compatibility/README.md` 和 `SUPPORTED_MCP_PROTOCOL_VERSIONS` 为准，四版本 client compatibility matrix 覆盖 `2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25`：

- `2024-11-05`：legacy HTTP+SSE transport family，仅用于普通 `initialize`、`tools/list`、`tools/call` 兼容。
- `2025-03-26`、`2025-06-18`、`2025-11-25`：Streamable HTTP transport family，后续 HTTP 请求使用 negotiated `MCP-Protocol-Version`。
- `2025-11-25 latest features`（Tasks、progress、cancellation、long-task Streamable HTTP/SSE）仍由 `docs/prd/backend/17-MCP长任务流式SSEPRD.md` 和 `docs/prd/MCP/` Phase 0-5 轨道治理，不等同于四版本普通 tools 首版兼容范围。
- `2026-07-28`：已批准的第五版本目标。它使用无协议 Session 的 Streamable HTTP、`server/discover`、每请求 metadata/header、MRTR 与 Tasks Extension，由用户级按需 MCP 三阶段 PRD 实施和验收；当前 `SUPPORTED_MCP_PROTOCOL_VERSIONS` 尚未包含该版本。


### 2.1 MCP 2025-11-25 session-era 通信基线

MCP Runtime 的通信协议层必须按照 latest spec 2025-11-25 的通用通信模型设计，不允许自创一套非标准 tool RPC。具体要求：

本节是现有 2025 实现基线，不适用于 `2026-07-28`。第五版本不执行 initialize、不使用 MCP Session/GET stream/Last-Event-ID，其独立 Adapter 契约见 `docs/prd/MCP/user-scoped-on-demand/01-用户级MCP配置凭据与按需GatewayPRD.md`。

1. **Base Protocol**：所有 client / server 消息必须是 JSON-RPC 2.0，UTF-8 编码；request id 必须是非空 string 或 integer；notification 不得包含 id；response 必须复用 request id。
2. **Lifecycle first**：每个 MCP client 与 server 建连后的第一阶段必须是 initialization，完成 protocol version negotiation 与 capability negotiation 后才能进入 operation。
3. **Protocol version**：默认请求最新支持版本 `2025-11-25`；如果 server 协商返回不受支持版本，client 应断开该 server；HTTP 后续请求必须携带 `MCP-Protocol-Version: 2025-11-25` 或协商后的版本。
4. **Standard transports**：transport 层必须抽象支持 MCP 标准 transports：`stdio` 与 `Streamable HTTP`。v1 远程 server 以 Streamable HTTP 为必选实现；stdio 是标准能力，但必须受显式配置、沙箱和进程生命周期治理约束。
5. **Streamable HTTP**：每条 JSON-RPC message 使用新的 HTTP POST；POST 必须声明可接受 `application/json` 与 `text/event-stream`；client 必须同时支持普通 JSON response 与 SSE response。
6. **SSE / reconnect**：如果 server 返回 SSE，client 不能把断连当作取消；应尊重 server SSE `retry`，支持基于 `Last-Event-ID` 的恢复请求，并用显式 MCP cancellation 通知表达取消意图。
7. **Session header**：如果 server 初始化响应返回 `MCP-Session-Id`，client 后续请求必须携带该 header；收到 session 失效响应时应重新 initialize。
8. **HTTP auth**：HTTP transport 的鉴权按 2025-11-25 Authorization 规范处理；token 只能走 `Authorization: Bearer` header，不允许放入 URI query；401/403 的 `WWW-Authenticate` 与 scope challenge 要映射为可审计错误或后续授权流程。
9. **stdio auth**：stdio transport 不走 HTTP OAuth；凭据来自环境变量或受控启动上下文，不得由 Planner 或用户消息生成。
10. **Tools**：tool discovery 使用 `tools/list` 并支持分页；tool invocation 使用 `tools/call`；tool result 的协议错误与执行错误要分开映射。
11. **2025-11-25 新元数据**：tool / resource / prompt 可能包含 icons；icons 不进入 Planner prompt，默认不主动拉取。如需展示，必须无凭据请求、限制来源、大小与 MIME。
12. **Tasks experimental**：2025-11-25 引入 MCP Tasks 实验特性。v1 不依赖 tasks 完成主链路，但 discovery 时应保留 `execution.taskSupport` metadata；后续如支持异步 MCP task polling，应单独设计状态映射。

### 2.2 本项目通信实现口径

为避免实现时把 MCP 做成普通 HTTP tool wrapper，v1 必须把“协议通信层”和“业务 capability 层”分开实现：

1. `MCPTransport` 抽象只负责标准消息传输：`initialize`、`send_request`、`send_notification`、`close`、可选 SSE 接收与重连。
2. session-era Adapter 的 `MCPClient` 负责 lifecycle 状态机：`new` → `initializing` → `initialized` → `closed` / `failed`；这些版本的 `tools/list`、`tools/call` 必须在收到 server `InitializeResult` 且发送 `notifications/initialized` 后执行。2026 Adapter 不复用该状态机。
3. v1 client capabilities 默认必须保持最小化：不声明 `roots`、`sampling`、`elicitation`、`tasks`，除非对应能力已经实现并有测试。
4. 如果 server 在 operation 阶段发起本项目未声明或未实现的 client feature request，client 必须返回标准 JSON-RPC method-not-found / unsupported error，并记录脱敏 audit；不得静默执行。
5. 每个 MCP session 内 request id 必须由 client 统一生成并保证不复用；response correlation、timeout、cancellation 与 audit 都以该 request id 为链路字段。
6. JSON Schema 默认按 2020-12 处理；显式 `$schema` 可以支持 draft-07。遇到不支持的 schema dialect 时，该 tool 不得公开，调用时必须 fail closed。
7. Streamable HTTP v1 应优先复用现有依赖 `httpx` / `httpx-sse`；inputSchema / outputSchema 校验优先复用现有 `jsonschema`。新增依赖必须另行说明必要性。
8. HTTP OAuth 完整交互授权不是 Phase 1 必交项；Phase 1 仅要求支持静态 bearer / API key / 预注册凭据注入，并能把 401 / 403 / insufficient_scope 映射为 `auth_required` / `scope_required` 类错误。交互式 OAuth、Client ID Metadata Documents、OpenID Connect Discovery 与 step-up authorization 放入 Phase 2。

## 3. 目标

### 3.1 产品目标

1. 支持把受信外部 MCP server 的部分 tools 接入为平台可编排能力。
2. 让 LLM Planner 只看到受控、脱敏、业务语义明确的 public capability，而不是直接看到全部原始 MCP tools。
3. 让外部 MCP tool 调用具备统一的超时、限流、错误映射、输出治理、审计和后续可观测性。
4. 保持与现有 capability registry、executor、macro provider、payload allowlist、API runtime 装配方式一致。
5. 为后续 resources / prompts / elicitation / sampling 等 MCP 更完整能力预留扩展点，但不在 v1 过度实现。

### 3.2 工程目标

1. MCP 协议与 transport 细节放入 `src/integrations/mcp/`，不污染 `src/orchestration/`。
2. MCP tool 通过 `src/capabilities/` 下的业务 capability 或受控 generic capability 进入执行链。
3. 测试默认使用 fake MCP server / fake MCP client，不访问真实外部 MCP server。
4. 所有外部 server、tool、auth、超时、限流、公开策略都由配置或 runtime 注入控制，禁止在代码里硬编码真实地址与密钥。
5. MCP Runtime 失败不能影响内置 `main_agent.respond` 与已注册 Skill capability（包括 `skill.data_lookup`）。

### 3.3 用户、干系人与受影响系统

| 对象 | 影响 | 本 PRD 对其承诺 |
|---|---|---|
| 内部业务用户 | 通过自然语言间接使用外部 MCP tools | 默认不需要理解 MCP；敏感操作必须可确认、可拒绝、可追溯 |
| 后端开发者 | 实现 MCP client runtime 与 capability 包装 | 有明确目录边界、配置契约、测试面与失败语义 |
| 运维 / 管理员 | 配置 MCP server、密钥、公开策略与限流 | 真实 endpoint / token 不入库内文档；server 失败可降级且可审计 |
| LLM Planner / Replanner | 只选择 public capability | 只看到本地审核后的能力描述和 payload allowlist |
| 外部 MCP server | 被本系统作为外部依赖调用 | 其 tool 描述、annotations、输出和 icons 都按不可信输入治理 |
| API / 前端 | 展示 capability、任务事件、interrupt / confirmation | v1 默认无 MCP 专属 UI；仅在确认、健康、授权管理需要时扩展 |

## 4. 非目标

v1 不做以下事项：

1. 不实现“把本平台能力反向暴露成 MCP server”。本 PRD 只覆盖 **MCP client runtime**。
2. 不实现任意未审核 MCP server 的动态接入市场。
3. 不默认把一个 server 的所有 tools 自动公开给 Planner。
4. 不让 LLM Planner 直接生成外部 server 地址、鉴权信息、账号、token、header 或任意 tool name。
5. 不支持未审核、未沙箱、未显式配置的本地 stdio server 自动启动；stdio 属于 MCP 标准 transport，但默认必须受配置、沙箱与权限约束。
6. 不在 v1 完整支持 MCP resources / prompts 作为独立 public capability。tool 返回的 resource link 可以作为受限 metadata / artifact 处理。
7. 本 v1 基线不支持 MCP sampling / elicitation 的完整双向协议；后续已批准仅为 `2026-07-28` 实现受控 MRTR elicitation，并映射到平台 Interrupt，不启用 Sampling/Roots/Logging。
8. 不在 Phase 1 实现完整交互式 OAuth 授权流；Phase 1 只支持静态 bearer / API key / 预注册凭据和授权错误识别。
9. 不引入 LangChain、LangGraph、AutoGen 等框架来承接 MCP。

## 5. 分层与代码边界

### 5.1 `src/integrations/mcp/`：MCP 外部适配层

负责：

- server 配置解析与归一化；
- MCP client lifecycle：initialize、`notifications/initialized`、capability negotiation、session 管理、close；
- transport 抽象：必须按 MCP 2025-11-25 标准通用通信模型实现 transport 抽象；v1 至少实现 Streamable HTTP，stdio 按同一接口保留受控实现位；
- client capabilities 生成：默认最小化声明，禁止声明未实现的 roots / sampling / elicitation / tasks；
- `tools/list` 分页发现与 tool 元数据缓存；
- `tools/call` 调用、超时、取消、错误归一化；
- HTTP auth header / bearer token / API key 等鉴权注入；
- 协议版本、`MCP-Session-Id`、request id 唯一性、response correlation、连接状态管理；
- tool result 内容解析：text、structuredContent、image/audio/resource link metadata；
- adapter 级审计 metadata 生成；
- fake client / fake server 测试 seam；
- unsupported server-to-client requests 的标准错误响应。

不负责：

- Planner 路由；
- business capability 描述；
- 用户可见最终回答；
- orchestration DAG 展开；
- 前端展示逻辑。

### 5.2 `src/capabilities/<domain>/` 或 `src/capabilities/mcp_tool/`：capability 包装层

MCP tool 进入 agent 系统必须先被包装成 capability。推荐两类包装方式：

#### A. 业务领域 capability（推荐默认）

适用于有明确业务语义、需要安全边界、需要多步编排或会产生副作用的 MCP tools。

示例：

```text
src/capabilities/customer_lookup/
  workflow.py
  executor.py
  mcp_mapping.py
```

对外只暴露：

```text
customer.lookup
```

内部再调用：

```text
server_id=crm
tool_name=get_customer_profile
```

#### B. 受控 generic MCP tool capability（仅限低风险场景）

适用于明确 allowlist、只读、幂等、输入输出结构简单的 MCP tools。可以由配置生成 public descriptor，例如：

```text
mcp.weather.get_current
mcp.docs.search
```

但必须满足：

- server 在受信 registry 中；
- tool 在本地 allowlist 中；
- public description 使用本地审核后的描述，不直接把 server 原始 description 全量暴露给 Planner；
- Planner payload 只允许 inputSchema 中经过本地 allowlist 的字段；
- tool annotations 仅作为辅助信号，不作为信任依据；
- destructive / non-idempotent / write 类 tool 不允许走 generic public 直达。

### 5.3 `src/orchestration/`：保持通用编排层

orchestration 只允许感知：

- `CapabilityDescriptor`；
- `CapabilityPayloadPolicy`；
- `WorkflowPlan` / `WorkflowNodePlan`；
- macro provider；
- validator / scheduler / expander。

不得新增：

- MCP server 连接逻辑；
- MCP tool schema 特判；
- `if capability_id.startswith("mcp.")` 之类的执行分支；
- LLM Planner 中针对某个 MCP tool 的 payload 特判。

### 5.4 `src/api/runtime.py`：集中装配层

runtime 负责：

- 读取已 bootstrap 的 MCP 配置；
- 构造 `MCPRuntimeState` / `MCPClientRegistry`；
- 构造 MCP capability descriptors 与 payload policies；
- 注册 execution instance；
- 把 MCP executor 加入 `CompositeExecutor`；
- 必要时注册 MCP macro provider；
- shutdown 时关闭 MCP client / HTTP session。

## 6. 配置需求

### 6.1 配置来源

MCP runtime 配置应遵循现有配置约定：

- API runtime 启动时一次性读取本地 `config.yaml` 或部署环境变量；
- 业务执行阶段不得重复读取 `config.yaml`；
- 真实 server 地址、token、API key 不得写入 tracked 文件；
- 测试必须支持显式 config dict / fake client 注入。

### 6.2 建议配置结构

示例仅表示结构，不代表必须原样实现：

```yaml
mcp:
  enabled: true
  default_timeout_seconds: 20
  servers:
    - server_id: crm
      enabled: true
      required: false
      transport: streamable_http
      endpoint_env: MAF_MCP_CRM_ENDPOINT
      protocol_version: "2025-11-25"
      allow_http_localhost: true
      client_capabilities:
        roots: false
        sampling: false
        elicitation: false
        tasks: false
      auth:
        type: bearer_env
        token_env: MAF_MCP_CRM_TOKEN
      trust_level: trusted_internal
      discovery:
        refresh_on_startup: true
        refresh_on_conversation_start: false
      limits:
        max_calls_per_task: 5
        max_output_bytes: 65536
      tools:
        - tool_name: get_customer_profile
          expose: false
          mode: internal
          risk_level: read_only
        - tool_name: search_customer
          expose: true
          capability_id: mcp.crm.search_customer
          public_name: Customer Search
          public_description: 通过 CRM MCP 服务查询客户基础信息。
          risk_level: read_only
          planner_allowed_fields: ["keyword"]
```

### 6.3 配置校验

启动时必须校验：

- `server_id` 稳定、唯一、只包含安全字符；
- `endpoint` 或 `endpoint_env` 解析后的值不能为空；
- 远程 HTTP server 默认要求 HTTPS，本地开发可显式允许 HTTP localhost；
- auth secret 只能来自环境变量或注入对象，配置文件不得直接保存 secret 值；
- public `capability_id` 不得与已有 capability 冲突；
- public capability 必须有本地审核后的 `public_description`；
- destructive / write 类 tool 不能配置为 generic public 直达；
- planner allowlist 字段必须是 `inputSchema` 的子集，或者在 discovery 不可用时由本地 schema 明确声明；
- `client_capabilities` 中任何设为 `true` 的能力都必须已有实现和测试，否则启动校验应拒绝该 server 配置；
- `protocol_version` 默认 `2025-11-25`，只允许配置到本 runtime 明确支持的版本；
- `auth.type=oauth` 在 Phase 1 必须被判定为 unsupported config，除非实现同时补齐授权流测试。

配置校验失败时，MCP runtime 应 fail closed：跳过该 server 或该 tool，并记录 audit diagnostic；不得让 API runtime 整体崩溃，除非配置显式要求 `required=true`。

## 7. MCP Runtime 状态模型

### 7.1 建议对象

- `MCPServerConfig`：静态 server 配置。
- `MCPToolDescriptor`：从 server 发现并归一化后的 tool 元数据。
- `MCPServerSnapshot`：某一 server 某次 discovery 的不可变快照。
- `MCPRuntimeBundle`：全部 enabled server 的快照、capability descriptors、tool 映射、revision。
- `MCPRuntimeState`：持有 active bundle，负责 refresh、rollback、retain revision。
- `MCPClientRegistry`：按 server_id 管理 client lifecycle。

### 7.2 revision 语义

如果 MCP tool capability 支持动态刷新，应采用与 Skill bundle 类似的 revision 保护：

- 新任务规划时记录 `mcp_bundle_revision`；
- 运行中任务继续使用规划时 revision 的 server/tool 映射；
- refresh 成功后只影响新任务；
- refresh 失败保留上一份 active bundle；
- terminal task 释放旧 revision 引用。

v1 可以先实现启动期 discovery + 手动 refresh seam；若要做到“新聊天刷新”，应与 Skill 动态加载保持一致，放在新 conversation 首次任务提交前。

### 7.3 原子激活与运行时同步

MCP bundle refresh 不只是更新 tool cache。一次成功激活必须同步完成：

1. active `MCPRuntimeBundle` 替换；
2. `CapabilityRegistry` 中旧 MCP descriptors 移除、新 descriptors 注册；
3. planner payload policies 与 descriptor 同步更新；
4. generic MCP executor 的 capability_id → server/tool 映射切换到同一 revision；
5. 如存在 MCP macro provider，`macro_provider_resolver` 能解析到同一 revision；
6. audit 记录 registered / skipped / failed 的 server 与 tool 摘要。

任一环节失败时必须保留旧 bundle，不能出现“Planner 可见但 executor 不支持”或“executor 支持但 registry 不可见”的半刷新状态。

## 8. Tool 发现与公开策略

### 8.1 发现流程

1. runtime 根据配置建立 MCP client；
2. client 完成 initialize 和 capability negotiation；
3. 对支持 tools 的 server 调用 `tools/list`，处理分页；
4. 将 tool name、title、description、inputSchema、outputSchema、annotations、icons、execution.taskSupport、server_id 归一化；
5. 校验 JSON Schema dialect；不支持的 schema dialect 进入 skipped diagnostics；
6. 与本地 allowlist 合并，生成内部映射；
7. 只有通过本地公开策略的 tool 才生成 public capability descriptor；
8. 如果 server 声明 `tools.listChanged=true`，v1 可先记录该能力但不自动热刷新；Phase 2 再决定是否接入 notification-driven refresh。

### 8.2 公开策略

默认策略：

- 未配置 allowlist 的 tool 不公开；
- description 以本地配置为准，server 原始 description 只作为 audit / debug metadata；
- 高风险 tool 只能被业务 macro capability 间接调用；
- public capability 数量应受预算控制，避免 Planner prompt 被外部工具池挤爆；
- 工具列表变化不能直接让运行中任务改路由。

### 8.3 Capability id 规范

generic MCP capability 建议使用：

```text
mcp.<server_id>.<tool_slug>
```

约束：

- 全局唯一；
- 只允许小写字母、数字、下划线、短横线、点；
- 不允许与 `main_agent.*`、`skill.*`、`mcp.*` 等保留 public capability namespace 冲突；
- 业务 wrapper capability 不必使用 `mcp.` 前缀，应使用业务语义命名。

## 9. Tool 调用执行需求

### 9.1 输入处理

执行前必须：

- 确认 MCP client 处于 `initialized` 状态；如 session 失效，应按 transport 规则重新 initialize 或返回 retriable error；
- 应用 `CapabilityPayloadPolicy`；
- 校验字段在 allowlist 内；
- 校验 JSON 可序列化；
- 对照 inputSchema 做基础类型与 required 校验；
- 追加系统可信 metadata，例如 account_id、conversation_id、task_id，但不得传递完整 prompt、API key、guard token、完整 conversation memory；
- 对敏感或副作用操作触发 Interrupt / confirmation，而不是直接调用。

### 9.2 调用控制

每次 tool 调用必须支持：

- per-call timeout；
- per-task 最大调用次数；
- per-server 并发限制；
- retry policy：仅对明确 transient 的 transport 错误重试；
- cancellation：任务取消时停止等待，并尽可能发送 MCP cancellation notification；若是 SSE 断连，不得把断连本身误判为取消；
- circuit breaker：连续失败的 server 临时降级，避免拖垮任务链。

### 9.3 输出映射

MCP tool result 应映射为 `CapabilityExecutionResult`：

- `structuredContent` → `output_payload["structured_content"]`，并按 outputSchema 校验；
- text content → `output_payload["text"]` 或摘要字段，超限时截断并标记；
- image/audio content → 优先进入 artifact，不直接塞入 prompt；
- resource_link / embedded resource → 作为受限 metadata / artifact 引用，不自动读取外部 URI；
- `isError=true` → `CapabilityExecutionError`，区分业务错误与协议错误；
- protocol JSON-RPC error → 按错误码映射 retriable / non-retriable。

输出进入主代理 prompt 前必须做：

- 字节数 / token 预算限制；
- secret pattern 粗过滤；
- 外部 URI 白名单或仅展示 metadata；
- 不把 server 原始隐藏指令、tool description、未经清洗的 resource 内容当系统指令注入。

## 10. 安全与治理需求

### 10.1 信任边界

- MCP server 是外部依赖，即使是内部部署，也不得被视为 orchestration 内核的一部分。
- tool description、annotations、resource content、icons、`_meta` 都可能携带 prompt injection 或 tool poisoning，不得作为系统指令。
- LLM Planner 只接收本地审核后的 capability 描述和字段 allowlist。
- server 原始 tool name 只能作为调用定位符，不能直接作为用户可见信任标识；展示名必须优先使用本地审核名。

### 10.2 权限与副作用

每个 tool 必须被分类：

| 风险级别 | 示例 | 默认策略 |
|---|---|---|
| `read_only` | 查询、搜索、计算 | 可配置 generic public，但仍需 allowlist |
| `idempotent_write` | 创建幂等草稿、生成临时资源 | 必须业务 capability 包装，通常需要确认 |
| `destructive` | 删除、付款、发送外部消息、改库 | v1 不允许自动调用，必须 Interrupt + 人类确认 |
| `credentialed_external` | 访问第三方账户数据 | 必须 account / server 权限隔离与审计 |

### 10.3 审计

必须记录 audit-only 事件：

- `mcp.server_discovery_started`
- `mcp.server_discovery_completed`
- `mcp.server_discovery_failed`
- `mcp.capability_registered`
- `mcp.tool_call_started`
- `mcp.tool_call_completed`
- `mcp.tool_call_failed`
- `mcp.tool_call_blocked`

审计 payload 允许包含：

- server_id；
- tool_name；
- capability_id；
- task_id / node_id；
- duration_ms；
- status；
- retriable；
- input field names；
- output size；
- error type / sanitized diagnostic。

审计 payload 禁止包含：

- access token / API key；
- Authorization header；
- 完整 tool arguments；
- 完整 output；
- 完整 prompt；
- 用户上传文件正文；
- 外部系统返回的高敏字段。

## 11. API 与前端影响

### 11.1 后端 API

v1 默认不新增用户手动选择 MCP tool 的 API。

已有 `/api/v1/capabilities` 可以展示被公开的 MCP capability，但必须只展示本地审核后的 name / description / source metadata。

只有以下情况才新增 API：

- 管理员需要手动 refresh MCP discovery；
- 需要展示 server 健康状态；
- 需要用户完成外部 OAuth 授权；
- 需要查看 tool 调用审计摘要。

### 11.2 前端

默认无需新增 MCP 专属 UI。用户仍通过自然语言发起任务，由 Planner 选择 public capability。

如果引入敏感 tool confirmation，应复用现有 interrupt / resume 产品语义。后续可在前端展示：

- “正在调用外部工具”进度；
- tool 名称的本地审核展示名；
- 需要确认的参数摘要；
- 调用失败的安全错误提示。

## 12. 与现有模块的关系

| 现有模块 | MCP Runtime 接入关系 |
|---|---|
| `src/core/` | 原则上不放 MCP 业务语义；只有确需复用的通用 contract 才进入 core。 |
| `src/integrations/` | 新增 `mcp` package，承接协议、transport、client、discovery、调用。 |
| `src/capabilities/` | 新增业务 wrapper capability，或受控 generic `mcp_tool` capability。 |
| `src/orchestration/` | 不加 MCP 特判；继续通过 registry、payload policy、validator、expander 工作。 |
| `src/api/runtime.py` | 负责装配 MCP runtime、注册 descriptor / executor / instance。 |
| `src/storage/` | v1 可不落库；如需持久 token、server registry、调用记录，再单独设计迁移。 |
| `frontend/` | v1 默认不改；confirmation / health / admin 后续按需求补。 |

## 13. 测试与验收

### 13.1 单元测试

建议新增：

- `tests/integrations/test_mcp_client.py`
  - initialize 成功 / 失败，并发送 `notifications/initialized`；
  - request id 在 session 内唯一且能关联 response；
  - `MCP-Protocol-Version` 与 `MCP-Session-Id` header 处理；
  - tools/list 分页；
  - tools/call 成功；
  - JSON response 与 SSE response；
  - SSE retry / Last-Event-ID reconnect；
  - protocol error；
  - tool execution error；
  - timeout / cancellation；
  - 401 / 403 / insufficient_scope 映射；
  - auth header 不进入 audit。

- `tests/integrations/test_mcp_runtime_state.py`
  - discovery bundle 构建；
  - allowlist 过滤；
  - duplicate capability id 阻断；
  - refresh 成功激活；
  - refresh 失败保留旧 bundle。

- `tests/capabilities/mcp_tool/test_executor.py`
  - payload allowlist；
  - inputSchema 校验；
  - result 映射；
  - output size 截断；
  - unsupported schema dialect fail closed；
  - icons / `_meta` 不进入 Planner prompt；
  - `isError=true` 映射为 capability error。

### 13.2 编排测试

建议新增：

- `tests/orchestration/test_mcp_capability_registry.py`
  - 只有 public MCP capability 进入 Planner 列表；
  - internal MCP capability 不可被 public plan 选择；
  - payload policy fail-closed；
  - macro wrapper 展开后 validator 通过。

### 13.3 API runtime 测试

建议新增：

- `tests/api/test_mcp_runtime_registration.py`
  - fake MCP config 注册 public capability；
  - discovery 失败不影响 main_agent / 数据查询 Skill；
  - `/api/v1/capabilities` 只返回脱敏 descriptor；
  - shutdown 会关闭 MCP client。

### 13.4 验收标准

MCP Runtime v1 完成时必须满足：

1. 可以通过 fake Streamable HTTP MCP server 完成 initialize、`notifications/initialized`、tools/list、tools/call。
2. HTTP 请求正确处理 `MCP-Protocol-Version`、`MCP-Session-Id`、JSON response、SSE response、SSE retry 与 Last-Event-ID reconnect。
3. Client 默认不声明 roots / sampling / elicitation / tasks；未实现 client feature request 会返回标准 unsupported error 并审计。
4. 可以把一个 allowlisted read-only tool 注册为 public capability，并由 Planner 看到。
5. 未 allowlist 的 tool 不会进入 public capability pool。
6. Planner 不能生成 server endpoint、auth token 或未允许的 arguments。
7. tool 调用结果能转换为 `CapabilityExecutionResult`，并被主代理汇总。
8. tool 调用失败、超时、授权错误、协议错误都有稳定错误映射，不导致 API runtime 崩溃。
9. audit 记录包含调用链路 metadata，但不泄露 token、完整参数、完整输出和 prompt。
10. MCP server discovery 失败时，内置能力和 Skill 能力仍可用。
11. 所有自动化测试均使用 fake client / fake server，不依赖真实外部 MCP 服务。

## 14. 分阶段建议

### Phase 0：PRD 与边界确认

- 明确是否只做 MCP client runtime；
- 明确 v1 需要启用哪些 MCP 2025-11-25 标准 transport，以及 stdio 是否仅在沙箱模式开放；
- 明确首批接入的 server 与 tool 风险级别；
- 明确是否需要用户级 OAuth。

### Phase 1：最小可用 MCP Runtime

- `src/integrations/mcp/`：MCP 2025-11-25 base protocol、lifecycle、minimal client capabilities、Streamable HTTP transport、discovery、call、fake client；
- runtime config 校验；
- read-only allowlisted tool → public capability；
- MCP executor；
- audit 与错误映射；
- targeted tests。

### Phase 2：安全增强与动态刷新

- MCP runtime bundle revision；
- 新 conversation 前可选 discovery refresh；
- server health / circuit breaker；
- 管理员 refresh API；
- 交互式 OAuth、Client ID Metadata Documents、OpenID Connect Discovery 与 incremental scope consent；
- 更完整 output artifact 化。

### Phase 3：更完整 MCP 原语

- resources 作为受控 context source；
- prompts 作为人工选择或 Skill-like 入口；
- elicitation 映射 Interrupt；
- stdio transport 的沙箱化、进程生命周期与 stderr 日志治理；
- 平台作为 MCP server 对外暴露内部 capability。

## 15. 关键决策

1. MCP Runtime 在本项目中首先是 **外部适配层**，不是新的编排核心。
2. capability 继续是 Planner 的唯一稳定选择单位。
3. MCP tool 默认不公开；公开必须显式 allowlist，并使用本地审核描述。
4. 高风险 tool 必须走业务 capability / interrupt / confirmation，不走 generic public 直达。
5. 通信协议按 MCP 2025-11-25 latest spec 实现；Streamable HTTP 是 v1 远程 server 的必选 transport，stdio 是标准 transport 但必须配置门控与沙箱化。
6. 自动化测试必须 fake 外部 server，真实 MCP server 只允许进入手工 smoke。

## 16. 风险、假设与开放问题

### 16.1 已记录假设

| 假设 | 对实现的影响 | 处理方式 |
|---|---|---|
| Phase 1 首批接入的是受信内部或明确审核过的外部 MCP server | 可以先做 allowlist + 静态凭据，不需要开放市场 | 若要接入任意第三方 server，必须新增 server trust registry / 管理后台 / 人工审核 PRD |
| Phase 1 不依赖交互式 OAuth 才能验收 | 可先支持 bearer / API key / 预注册凭据和 401 / 403 识别 | 需要用户授权时进入 Phase 2，不在 Phase 1 暗中半实现 |
| 现有依赖 `httpx`、`httpx-sse`、`jsonschema` 可用于首版实现 | 无需为 Streamable HTTP 和 JSON Schema 校验新增依赖 | 如果实现发现能力不足，必须同步更新 `requirements.txt`、README 与本 PRD |
| 用户仍通过自然语言发起任务 | 前端默认不增加 MCP tool 选择器 | 若产品改为管理员 / 用户手动选 tool，需要补 API / 前端 PRD |

### 16.2 主要风险与缓解

| 风险 | 影响 | 缓解要求 |
|---|---|---|
| MCP 规范继续演进 | 协议细节可能变化 | 版本常量集中管理；每次升级先更新本 PRD 与协议测试 |
| Tool poisoning / prompt injection | 外部 tool 描述或输出污染 Planner / 主代理 | Planner 只看本地审核描述；输出脱敏、截断、只作为 data context |
| stdio server 本地执行风险 | 可能引入任意进程执行与凭据泄露 | stdio 必须显式配置、沙箱化、限制 env、记录 stderr 审计摘要 |
| public capability 过多 | Planner prompt 预算被挤占、路由变差 | 只公开 allowlist tool；复用 public capability 列表预算与摘要策略 |
| bundle 半刷新 | Planner 可见与 executor 可执行不一致 | 必须按 7.3 原子激活；失败回滚旧 bundle |
| OAuth / scope 处理不足 | 用户误以为已支持完整授权 | Phase 1 只识别授权错误；完整授权流明确推迟到 Phase 2 |

### 16.3 开放问题

这些问题不阻塞 Phase 1 PRD 作为开发基线，但会影响 Phase 2 设计：

1. 首批真实 MCP server 的工具清单、风险级别、授权方式和 SLA 需要在开发前补一份接入清单。
2. 管理员是否需要在前端查看 MCP server health / discovery diagnostics，需要单独决定。
3. 是否需要平台反向作为 MCP server 暴露内部 capability，属于 Phase 3 之后的独立专题。

## 17. Phase 1 实现状态（2026-05-12）

Phase 1 后端基线已落地：

- `src/integrations/mcp/` 已实现 MCP 2025-11-25 JSON-RPC client、Streamable HTTP transport、initialize / initialized lifecycle、tools/list 分页、tools/call、协议 / session header、SSE JSON 响应解析、静态 bearer / API key 注入与稳定错误映射。
- `MCPRuntimeState` 已支持启动期 / 手动刷新 discovery、不可变 bundle、allowlist public capability 生成、payload policy 生成、失败保留旧 bundle 与 client shutdown。
- `src/capabilities/mcp_tool/` 已支持 read-only generic MCP tool capability 执行，包含 planner allowlist、JSON Schema 校验、输出截断 / 清洗、`CapabilityExecutionResult` 映射和 audit-only 事件。
- `src/api/runtime.py` 已支持显式 `mcp_config` / `mcp_client_factory` 注入，启动时注册 MCP descriptor / instance / executor，并在关闭 runtime 时关闭 MCP clients。
- 已补充 fake client / fake server seam 自动化测试；真实 MCP server 仍仅进入后续手工 smoke。
- 架构复核后补齐 Phase 1 安全边界：MCP 输出脱敏 / URL 屏蔽、`outputSchema` 执行期校验、空 planner allowlist fail-closed、pending bundle 注册成功后再 commit 的原子激活流程。

后续 Phase 2 仍保留：完整交互式 OAuth、健康检查 / circuit breaker、管理员 refresh API、stdio 沙箱进程生命周期、notification-driven refresh、resources / prompts / elicitation 等更完整 MCP 原语。
