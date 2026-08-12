# 用户级 MCP 配置、凭据与按需 Gateway PRD

- **阶段**：三阶段改造第 1 阶段
- **范围**：后端 / 用户配置 / 凭据加密 / MCP Gateway / 远程 HTTP(S) 安全
- **状态**：设计已确认，待实施
- **日期**：2026-08-12
- **上游基线**：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`、`docs/prd/MCP/00-MCPRuntime联合改造总览PRD.md`
- **后续阶段**：`02-MCP两级路由授权与任务执行闭环PRD.md`

## 1. 一句话结论

将 MCP 从“后端进程启动时连接全部服务器并长期保存 Client/Tool Bundle”改造为“用户级最小配置持久化 + 凭据密文入库 + 任务级按需 MCP Gateway”。本阶段完成可被后续编排层调用的安全基础设施，但不让模型自动选择或执行用户 MCP 工具。

## 2. 背景与现状

当前仓库的 MCP 执行链路已具备远程 MCP Client、Streamable HTTP、Legacy HTTP+SSE、`tools/list`、`tools/call`、Schema 校验、输出清理、取消与 Rust Sidecar 适配基线，但其运行时状态仍是后端进程级：

- `src/integrations/mcp/runtime_state.py` 的 `MCPRuntimeState` 长期保存 `_clients`、`_bundles` 和 `_active_revision`。
- `src/api/runtime.py` 在启动时遍历已配置 MCP Server，执行 `tools/list` 并将工具注册到全局 `CapabilityRegistry`。
- 现有 MCP 配置来自服务器文件或环境变量，没有用户归属、用户配置 API 或用户级凭据存储。
- 任务、对话、文件和记忆已按认证用户隔离，MCP Runtime 却仍是进程全局。

新产品要求是：每个用户拥有自己的 MCP 服务器列表；配置在后端持久化，因此用户在不同端依次登录时可以使用同一配置；不支持同一用户多设备同时在线；Tool List、Schema 和连接不做长期持久化。

## 3. 阶段目标

### 3.1 产品目标

1. 用户可创建、编辑、删除和测试属于自己的远程 MCP 配置。
2. 用户配置的 MCP 在后端持久化，不依赖浏览器 `localStorage` 或某一设备的本地缓存。
3. 用户凭据以可解密密文存入数据库，加密主密钥以服务器挂载文件提供。
4. 后端仅在连接测试或任务执行时按需建立 MCP 运行时，结束后释放。
5. 支持远程 HTTPS Streamable HTTP 与受控的 HTTP Legacy HTTP+SSE，不支持本地 `stdio`；Client 目标兼容版本扩展为 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`、`2026-07-28`。

### 3.2 工程目标

1. 建立逻辑独立的 `MCPGateway` 边界，第一阶段允许与 FastAPI 后端同进程部署。
2. Gateway 只编排连接、发现、调用、取消与资源释放，继续复用现有 MCP Client/Transport/Rust Sidecar，不重写第二套协议栈。
3. 服务器不持久化 Tool List、`inputSchema`、`outputSchema`、MCP Client 或 HTTP/SSE 连接。
4. 用户身份始终来自后端认证上下文，不信任前端提交的 `user_id`/`username`。
5. Gateway 将来可以拆成独立服务，但本阶段不引入额外网络跳数和部署复杂度。

## 4. 非目标

1. 本阶段不实现主 Planner 的 MCP 服务器路由。
2. 本阶段不实现 MCP Tool Selector 或模型自动 `tools/call`。
3. 本阶段不将用户 Tool List 注册为全局 `mcp.*` Capability。
4. 不支持 `stdio`、本地进程、Unix Socket、`file://` 或用户指定本地文件的 MCP Server。
5. 不实现用户交互式 OAuth 授权流。首版沿用静态 Bearer/API Key/受控 Header 凭据注入能力。
6. 不实现加密密钥轮换、在线换钥或多 `key_id` Keyring。
7. 不实现已被 `2026-07-28` 废弃且本产品当前不使用的 Roots、Sampling、Logging，也不恢复 `stdio`。

## 5. 已确认的共享不变量

| 主题 | 决策 |
|---|---|
| 配置归属 | 每个 MCP Server 必须归属一个后端认证用户 |
| 配置同步 | 后端是权威数据源；用户在其他端依次登录后可直接使用 |
| 多设备 | 产品不支持同一用户多设备同时在线，但该约束由认证会话层实现 |
| 传输 | 仅支持远程 HTTP(S)：Streamable HTTP 和 Legacy HTTP+SSE |
| HTTP | 只允许管理员白名单中的企业域名或网段 |
| HTTPS | 允许受控自定义；私网、回环和特殊网段仍须管理员白名单 |
| Tool List/Schema | 不持久化，按任务、按服务器临时获取 |
| 连接 | 同一任务中复用临时 Client/连接池，任务之间不复用 |
| 输出 | 不设产品级大小上限；超出上下文容量时临时落盘并分块处理 |
| 密钥 | 数据库保存密文，单一固定主密钥以服务器文件提供，不支持轮换 |
| 前端持久化 | 前端不保存凭据、Tool List、Schema 或可信执行状态 |

## 6. 目标架构

```text
Authenticated API Request
        |
        v
UserMCPConfigService
  - ownership / CRUD / health
  - encrypted credential record
        |
        v
Task-scoped MCPGateway
  - endpoint policy
  - decrypt-on-use
  - connect / list_tools / call_tool / cancel / close
        |
        +--> existing Python MCP client / transports
        |
        +--> existing Rust MCP sidecar adapter
        |
        v
User-configured remote MCP Server
```

`MCPGateway` 的“无状态”指不保存跨任务的用户会话状态，而不是执行过程中完全没有内存对象。一个活跃任务可以临时持有 HTTP Client、Legacy SSE Reader、Tool Catalog、Schema Hash、调用计数和临时结果文件；任务终止后这些状态必须释放。

## 7. 数据模型

### 7.1 `user_mcp_server`

| 字段 | 语义 |
|---|---|
| `server_id` | 平台生成的不可猜测 ID |
| `owner_user_id` | 后端认证用户；当前实现可用 canonical `username` 承载，不从请求 body 接收 |
| `display_name` | 用户设置的 MCP 名称 |
| `routing_description` | 后续一级 Planner 路由时使用的服务器描述 |
| `endpoint_url` | 规范化后的 HTTP(S) Endpoint，不允许用户信息嵌入 URL |
| `transport` | `streamable_http` 或 `legacy_http_sse` |
| `protocol_preference` | `auto` 或显式版本；目标允许五个版本，默认 `auto`，显式版本不降级 |
| `auth_type` | `none`、`bearer`、`api_key_header` 或受控 `static_headers` |
| `auth_metadata` | 非敏感元数据，例如 Header 名称；不包含凭据值 |
| `enabled` | 用户是否启用该配置 |
| `health_status` | `untested`、`testing`、`available`、`unavailable` 或 `disabled` |
| `config_version` | 所有配置变更的单调版本 |
| `security_version` | Endpoint、transport、认证类型或凭据变更时递增，后续用于授权失效 |
| `last_tested_at` | 最后连接测试时间 |
| `last_test_error_code` | 脱敏错误类型，不保存堆栈、URL Query 或 Header |
| `created_at` / `updated_at` | 审计时间 |

数据库必须对 `(owner_user_id, server_id)` 和用户列表查询建立索引。`display_name` 可重复，执行不依赖名称唯一性。

### 7.2 凭据密文字段

凭据与服务器元数据逻辑分离，可存于同一表的独立密文列，也可由 Storage Port 映射为一对一内部表。对外不暴露存储形态。

| 字段 | 语义 |
|---|---|
| `credential_ciphertext` | AES-256-GCM 密文 |
| `credential_nonce` | 每次加密独立生成的 96-bit 随机 Nonce |
| `encryption_version` | 固定格式版本，首版为 `1`；不表示支持密钥轮换 |
| `credential_updated_at` | 凭据最后更新时间 |

密文明文容器是一个受控 JSON 对象，只保存当前 `auth_type` 需要的值。不接受 Planner 或用户消息动态生成的 Header。

### 7.3 本阶段预留的授权表

本阶段可创建 `user_mcp_tool_grant` 表及 Storage Port，但不开放用户授权流程。字段由第 2 阶段正式启用：

```text
grant_id
owner_user_id
server_id
tool_name
server_security_version
input_schema_sha256
granted_at
```

`(owner_user_id, server_id, tool_name, server_security_version, input_schema_sha256)` 必须唯一。

### 7.4 明确禁止持久化的内容

- Tool List 和远程 Tool Description。
- `inputSchema` 和 `outputSchema` 原文。
- MCP Client、HTTP Client、SSE Reader 和协议 Session 对象。
- MCP Session ID、Progress Token、`server/discover` 完整响应、远端能力快照、原始 JSON-RPC 报文和认证 Header。
- 连接测试的完整返回内容。

## 8. 凭据加密与密钥文件

### 8.1 加密算法

1. 使用 `AES-256-GCM`，复用仓库已锁定的 `cryptography` 依赖。
2. 每次凭据写入生成新 Nonce，不允许在同一主密钥下重用。
3. AAD 至少绑定 `owner_user_id`、`server_id` 和 `encryption_version`，防止密文在用户或 Server 之间被替换。
4. 凭据仅在连接测试或任务调用前解密，只存在于该次调用内存。
5. API 查询只返回 `credential_configured: true|false`，不返回明文、密文、Nonce 或可逆推的摘要。

### 8.2 密钥文件契约

- 路径由 `MCP_CREDENTIAL_KEY_FILE` 指定。
- 文件使用 UTF-8 文本保存一行标准 Base64；解码后必须恰好为 32-byte 主密钥，只允许末尾一个换行，不接受空白折叠、Hex 或自动补位。
- 文件不进 Git、不进 Docker 镜像、不进普通配置文件，生产权限为 `0400`。
- 所有 Gateway 实例必须挂载同一份主密钥，否则不得取得 Ready 状态。
- 启动时只校验可读性、权限和长度，不自动生成或覆盖密钥文件。
- 数据库备份与密钥文件分开备份。密钥丢失后已存凭据无法恢复，用户必须重新填写。
- 本阶段不提供密钥轮换。密钥泄露时的处置方式是废弃已存凭据，更换服务器密钥并要求用户重新配置。

## 9. URL、DNS 与 SSRF 安全边界

### 9.1 支持范围

| 目标 | 策略 |
|---|---|
| 公网 HTTPS | 通过 URL、DNS、IP 和重定向校验后允许 |
| 企业私网 HTTPS | 仅允许管理员域名/CIDR 白名单 |
| 企业 HTTP | 仅允许管理员域名/CIDR 白名单，记录 `plaintext_http` 安全标记 |
| `localhost`/回环/链路本地/云元数据 | 默认禁止，云元数据地址不可通过普通白名单放开 |
| `stdio`、`file://`、Unix Socket | 不支持 |

HTTP 白名单必须支持用户已给出的企业 MCP Endpoint 形式，例如 `http://breeding-dashboard-qa.biobin.com.cn/.../sse`，但具体域名不得硬编码到业务代码中。

### 9.2 校验规则

1. 解析 URL 后拒绝 user-info、空 Host、不受支持 Scheme 和非法端口。
2. 解析所有 A/AAAA 记录，每个目标 IP 都必须通过策略，不允许“部分安全、部分危险”。
3. 连接时防止 DNS Rebinding；实际连接目标必须与已校验解析结果一致。
4. 重定向默认不跟随。如为协议兼容必须跟随，仅允许同 Origin 的受控 307/308，并对每一跳重新执行全部检查。
5. 不允许 HTTPS 降级到 HTTP，不允许向新 Origin 转发认证 Header。
6. Header 名称必须规范化且黑名单保护 `Host`、`Content-Length`、`Connection`、`Cookie` 等连接字段；所有 `MCP-*` / `Mcp-*` 协议 Header（包括 `Mcp-Method`、`Mcp-Name`、`Mcp-Param-*`）只允许协议 Adapter 根据已校验的请求和 Schema 生成，用户静态配置不得覆盖。

URL 安全校验失败属于配置非法，不允许“保存为不可用”；只有通过安全校验但远程连接失败的配置才可以保存为 `unavailable`。

## 10. API 契约

所有端点必须调用 `require_authenticated_user`，并在 Storage Query 中同时约束 `owner_user_id`。未知 Server 和他人 Server 对调用者统一表现为 404，不泄露存在性。

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/v1/mcp/servers` | 列出当前用户 MCP 配置元数据 |
| `POST` | `/api/v1/mcp/servers` | 创建配置，加密凭据并异步发起连接测试 |
| `GET` | `/api/v1/mcp/servers/{server_id}` | 读取单个配置元数据 |
| `PATCH` | `/api/v1/mcp/servers/{server_id}` | 更新配置；关键连接/认证字段变更后自动重测 |
| `DELETE` | `/api/v1/mcp/servers/{server_id}` | 删除配置；预留级联删除授权记录 |
| `POST` | `/api/v1/mcp/servers/{server_id}/test` | 手动重新发起连接测试 |

### 10.1 响应约束

- 创建或关键编辑成功后返回配置元数据和 `health_status=testing`，不让 HTTP 请求阻塞最长 120 秒。
- 连接测试在后台执行，完成后更新为 `available` 或 `unavailable`。前端可在第 2 阶段通过轮询或专用事件刷新。
- 连接失败不回滚已通过安全校验的配置；配置保留但不可路由。
- 表示凭据的输入字段具有“未提交则保留旧凭据”语义；清空凭据必须使用显式操作，不能由空字符串隐式触发。
- API 不提供“返回密文”或“显示已存凭据”的后门。

## 11. `MCPGateway` 逻辑接口

Gateway 对业务层暴露稳定的任务作用域接口，不泄露具体 SDK 类型：

```text
open_scope(authenticated_user, platform_task_id, server_id) -> MCPTaskServerScope
list_tools(scope) -> ToolCatalogSnapshot
call_tool(scope, tool_name, arguments, callbacks) -> MCPCallOutcome
cancel_call(scope, call_ref, reason) -> CancelOutcome
close_scope(scope, reason) -> None
close_task(platform_task_id, reason) -> None
```

### 11.1 Scope 不变量

1. `open_scope` 再次校验 Server 归属、`enabled` 和 `health_status=available`，不信任上游传递的 Server DTO。
2. 凭据只在创建底层 Client 前解密，不进入任务 metadata、Planner payload、Tool Catalog 或异常字符串。
3. `(platform_task_id, server_id)` 在一个 Gateway 实例中只有一个活跃 Scope；同一任务重复访问同一 Server 复用它。
4. 不同任务不共享 Tool Catalog、协议 Session、Client、Discovery 结果或远端能力快照。
5. Scope 关闭时必须取消未完成请求、关闭 SSE/HTTP 资源并删除未提升为 Artifact 的临时文件。

### 11.2 Tool Catalog

- 当前任务首次访问某 Server 时调用一次 `tools/list`，并处理协议分页。
- Catalog 在该任务和 Server Scope 内复用，不发回前端作为权威执行源，不持久化到数据库。
- Catalog 是当前任务的不可变快照；运行中收到 `tools/list_changed` 只记录安全状态，不在本任务二次发现，新任务会重新获取。
- 保留 Tool Name、Description、`inputSchema`、可选 `outputSchema` 和必要 annotations，所有远程 metadata 都按不可信输入处理。
- 为每个 `inputSchema` 计算 canonical SHA-256，但本阶段只保存在 Scope 内存中。
- MCP `2026-07-28` 返回的 `ttlMs/cacheScope` 只作为当前任务 Scope 内的缓存提示；不跨任务、不写数据库，也不发往前端作为权威执行源。

### 11.3 `MCPCallOutcome`

Gateway 对上层返回归一化联合类型，不把不同协议版本的 Wire Contract 泄露给 Planner：

```text
completed(result_ref)
input_required(requests, sealed_request_state_ref)
task_created(safe_remote_task_ref, status, next_poll_at)
```

- 前四个版本继续映射已有普通结果；仅在对应版本 Adapter 已实现异步任务语义时返回 `task_created`。
- `2026-07-28` 的 `InputRequiredResult` 和 Tasks Extension 在第 1 阶段完成协议解析、能力门控与安全引用封装；用户交互、重试、轮询与取消闭环由第 2 阶段实现。
- `requestState` 和远端 Task ID 都是协议内部值，不进入模型、前端、普通日志或审计原文。

## 12. 五版本协议 Adapter 与协商

### 12.1 目标兼容矩阵

| 协议版本 | Transport | 生命周期 | 本阶段目标 |
|---|---|---|---|
| `2024-11-05` | Legacy HTTP+SSE | `initialize` / `initialized`，session-era | 保留现有普通 tools 兼容 |
| `2025-03-26` | Streamable HTTP | `initialize` / `initialized`，session-era | 保留现有普通 tools 兼容 |
| `2025-06-18` | Streamable HTTP | `initialize` / `initialized`，session-era | 保留现有普通 tools 兼容 |
| `2025-11-25` | Streamable HTTP | `initialize` / `initialized`，session-era | 保留现有普通 tools 与既有 Tasks 语义 |
| `2026-07-28` | Streamable HTTP | 无协议 Session；每个请求独立且自描述 | 新增 `server/discover`、ordinary tools、List Cache Hint、MRTR 与 Tasks Extension 适配 |

当前仓库的 `SUPPORTED_MCP_PROTOCOL_VERSIONS` 仍只有前四个版本；只有实现、fixtures 和 conformance gate 全部通过后，才能宣称运行时支持第五个版本。

`legacy_http_sse` 只能选择 `2024-11-05`。`streamable_http` 可选择四个 Streamable HTTP 版本。已有系统配置未填写版本时继续沿用当前 `2025-11-25` 默认语义，避免部署升级后静默改变；新建用户配置默认使用 `auto`。

### 12.2 `auto` 协商

Streamable HTTP 的自动协商必须遵循以下顺序：

1. 先发送无业务副作用的 `server/discover`，请求按 `2026-07-28` 的每请求 metadata/header 契约构造。
2. Discovery 成功且 `supportedVersions` 包含 `2026-07-28` 时，选择 `2026-07-28`；显式 pin 到该版本时，每个任务 Scope 仍先调用一次 Discovery 做能力门控，但失败不降级。
3. 只有 Discovery 成功但明确只列出旧版，或收到结构正确的“不支持该协议版本”/`server/discover` method-not-found，`auto` 才回退到双方支持的最高 2025 版本，并执行现有 `initialize` / `notifications/initialized` 协商。
4. TLS、认证、SSRF、网络、5xx、非法 JSON-RPC 或畸形 Discovery 响应不得触发版本回退。
5. 显式固定版本必须 fail closed，不自动降级。
6. 一旦发出 `tools/call` 或任何可能产生副作用的请求，不得换版本重放。

只把最终 `effective_protocol_version` 和本任务必要能力放入 Scope。`supportedVersions`、Discovery 原文和 Tool Catalog 都不持久化；`serverInfo` 是远端自报信息，只用于展示/诊断，不能作为身份、所有权或授权依据。

### 12.3 `2026-07-28` 每请求契约

1. 不发送 `initialize`、`notifications/initialized`、`MCP-Session-Id`、独立 GET stream 或 `Last-Event-ID`。
2. 每个 JSON-RPC 请求都是独立 HTTP POST；响应可以是 JSON，也可以是只服务于该请求的 SSE。
3. Body `_meta` 必须包含 `protocolVersion`、`clientInfo`、`clientCapabilities`；`MCP-Protocol-Version` Header 必须与 Body 一致。
4. 所有请求发送 `Mcp-Method`；`tools/call` 等命名请求发送 `Mcp-Name`。用户静态 Header 不能覆盖任何 MCP 协议 Header。
5. 如 Tool Schema 使用 `x-mcp-header` 声明参数镜像，Adapter 只能从已通过 Schema 校验的参数生成安全 Header；认证凭据和受保护 Header 永远不能由工具参数覆盖。
6. 变更通知只在活跃任务确有需要且 Server 支持 `subscriptions/listen` 时订阅；默认依赖当前任务内 `ttlMs/cacheScope`，不维护跨任务长连接。
7. Client 按请求只声明已启用且完成测试的 `elicitation` 和 Tasks Extension 能力；不声明 Roots、Sampling、Logging。

## 13. 连接、发现和重试策略

### 13.1 连接/发现

每次尝试的 60 秒预算覆盖：

- DNS 和网络连接。
- 旧协议所需的 initialize/initialized 协商，或 `2026-07-28` 的 `server/discover` 与每请求 metadata 校验。
- `tools/list` 全分页读取。
- Legacy HTTP+SSE 的 Endpoint 发现。

首次尝试遇到连接超时、连接中断或可明确识别的暂时性 5xx 时，可在短退避后重试一次。第二次拥有独立的 60 秒预算，不计入第一次。最坏发现等待约 120 秒加一次短退避。

以下错误不重试：

- 401/403 或明确凭据/权限错误。
- URL/SSRF/Header 策略错误。
- 协议版本不兼容、非法 JSON-RPC 或非法 Tool Schema。
- 服务器明确的业务拒绝。

### 13.2 `tools/call`

- 不设置产品级最长执行时间。
- 不设置 MCP 子流程总时长。
- 默认不自动重试，不依赖远程 annotations 判断写操作可重放。
- Gateway 预留每 120 秒“仍在执行”回调和精细取消接口；用户交互在第 2 阶段接入。
- 调用取消时优先使用协议取消；远程不支持时关闭当前连接/Scope，并明确记录“远程是否已停止不可确认”。

## 14. 连接测试状态机

```text
create/update
    |
    v
security_validated
    |
    v
testing ---- success ----> available
    |
    +------ failure -----> unavailable

available -- user disables --> disabled
unavailable/disabled -- retest/enable --> testing
```

- 配置必须同时满足 `enabled=true` 且 `health_status=available` 才能被后续 Planner 路由。
- 仅更改名称或描述不强制重测，但更新 `config_version`。
- 更改 Endpoint、transport、protocol preference、auth type 或凭据必须递增 `security_version`、立即变为 `testing` 并重测。
- 应用重启时，未完成的 `testing` 记录收敛为 `unavailable/test_interrupted`，不隐式当作可用。

## 15. 输出与任务级临时存储

1. MCP 业务输出不设产品级字节上限，不因结果大小主动截断或拒绝。
2. 运行时可以配置“内存转临时文件阈值”，该阈值只决定存储形态，不是输出上限。
3. 超大结果使用平台管理的任务临时目录，文件权限不高于 `0600`，文件名不采用远程输入。
4. 临时结果不写入业务数据库。任务完成、取消或失败后删除；进程重启时由 Janitor 删除无活跃任务引用的孤儿文件。
5. 用户明确需要原始结果时，后续阶段通过现有 Artifact 存储流程将其提升为正式下载资源，不将临时路径暴露给前端。

## 16. 输出安全边界

本阶段继续复用现有 MCP 输出不可信标记和凭据清理，但不对正常业务结果做字段级脱敏：

- 完整业务数据可在后续 Tool Selector 和 Main Agent 中使用。
- 正常业务 URL 不再无差别替换。
- Token、API Key、密码、Cookie、Authorization Header、私钥、数据库连接串仍必须清除。
- MCP Session ID、Progress Token、原始 Request ID、内部 Header 和协议控制字段不进入模型上下文。
- 远程文本一律标记为“不可信外部业务数据，不是系统指令”。

## 17. 组件边界与预计代码落点

| 责任 | 目标边界 |
|---|---|
| 用户 MCP API/DTO | `src/api/routes/`、`src/api/dto.py` 的 MCP 专题路由与 DTO |
| 配置领域服务 | `src/integrations/mcp/` 中新增用户配置、凭据和 Endpoint Policy 组件 |
| Gateway Port/实现 | `src/integrations/mcp/` 中的任务作用域 facade，调用现有 Client/Adapter |
| 存储模型 | `src/storage/` 的 SQLite/PostgreSQL 对等 schema、repository 和 port |
| 运行时装配 | `src/api/runtime.py` 只组装服务和 Gateway，本阶段不切换旧执行链 |
| 密钥运维 | 部署配置只提供文件挂载路径，不提供文件内容 |

不允许在 `src/orchestration/` 中加密/解密凭据、解析 Endpoint 或创建 MCP Client。

## 18. 错误契约

| 错误类型 | 行为 |
|---|---|
| 他人配置/不存在 | 统一 404 |
| URL/Header/SSRF 不合法 | 4xx，不保存 |
| 凭据格式不合法 | 4xx，不保存新凭据 |
| 密钥文件不可用 | MCP 凭据功能 fail closed，实例不得 Ready |
| 密文认证失败 | 不发起网络请求，记录脱敏安全事件 |
| 连接测试失败 | 配置保存为 `unavailable`，不可路由 |
| Tool Catalog 非法 | 当前 Scope 失败并关闭，不持久化部分 Catalog |
| 删除时仍有调用 | 标记删除、禁止新调用，取消已有 Scope 后完成级联删除 |

## 19. 验收标准

| 编号 | 验收项 |
|---|---|
| MCP-USER-P1-001 | 两个用户可保存同名/同 Endpoint 配置，且不能查看、测试、修改或删除对方记录 |
| MCP-USER-P1-002 | 数据库不出现凭据明文；API、日志、事件和错误不返回明文或密文 |
| MCP-USER-P1-003 | 主密钥缺失、权限不合法或长度错误时，MCP 凭据功能 fail closed 且不自动生成密钥 |
| MCP-USER-P1-004 | HTTPS 公网 Endpoint 通过安全校验后可测试；HTTP/私网目标仅在管理员白名单内可访问 |
| MCP-USER-P1-005 | `localhost`、链路本地、云元数据、DNS Rebinding 和跨 Origin 凭据重定向均被阻断 |
| MCP-USER-P1-006 | 连接失败的安全配置可保存为 `unavailable`，但后续不可被路由 |
| MCP-USER-P1-007 | Gateway 在同一任务内对同一 Server 只执行一次 Tool Discovery，并复用任务级 Scope |
| MCP-USER-P1-008 | 任务结束/取消后 Client、SSE、Tool Catalog 和临时文件均被释放 |
| MCP-USER-P1-009 | Tool List、Schema 和完整工具结果不写入用户 MCP 配置表 |
| MCP-USER-P1-010 | 连接/发现每次 60 秒，可重试一次且拥有独立 60 秒预算；非暂时错误不重试 |
| MCP-USER-P1-011 | `tools/call` 不自动重试，不设最长执行时间，并提供 120 秒运行中回调 seam |
| MCP-USER-P1-012 | 超大输出不被截断，可切换为任务级临时文件，且未提升文件会在任务后清理 |
| MCP-USER-P1-013 | 前四个版本行为无回归；`2026-07-28` 不发送 initialize/session/GET stream，并正确发送每请求 metadata 与协议 Header |
| MCP-USER-P1-014 | `auto` 只在明确版本不支持时安全回退；认证、网络、5xx、畸形响应和已发业务请求均不触发降级重放 |
| MCP-USER-P1-015 | `server/discover`、List Cache Hint、MRTR 与 Tasks Extension 均经版本门控；Discovery/能力/Tool List 不跨任务持久化 |

## 20. 测试要求

### 20.1 单元测试

- 配置 DTO 和 owner 强制绑定。
- AES-GCM round trip、AAD 替换失败、Nonce 唯一性和错误密钥 fail closed。
- 凭据更新/保留/显式清空语义。
- URL 正规化、IPv4/IPv6 分类、域名/CIDR 白名单、DNS Rebinding 和重定向策略。
- 任务 Scope 去重、Tool Catalog 只读快照和 Close 幂等。
- 发现重试分类，验证每次独立 60 秒预算。
- 临时结果文件权限、路径隔离和 Janitor。
- 五版本配置矩阵、`auto`/pin 协商、2026 Body/Header 一致性和静态 Header 不可覆盖。
- `x-mcp-header` 只从已验证参数生成，禁止认证与连接级 Header 注入。

### 20.2 存储/API 测试

- SQLite 与 PostgreSQL 模型/repository 语义对等。
- 列表、详情、编辑、删除、测试的跨用户隔离。
- API 响应全字段扫描，确保无凭据明文、密文、Nonce 和认证 Header。
- 配置删除与当前活跃 Scope 的竞态测试。

### 20.3 集成测试

- Fake HTTPS Streamable HTTP Server。
- Fake HTTP Legacy HTTP+SSE Server，分别在白名单内/外验证。
- 分页 `tools/list`、慢发现、首次失败第二次成功、两次失败。
- 大输出落盘不截断、任务后清理和 Artifact 提升 seam。
- 不访问真实外部 MCP Server 的默认 CI 测试。
- `2026-07-28` Fake Server 覆盖 `server/discover`、JSON/SSE 响应、List Cache Hint、`InputRequiredResult`、`CreateTaskResult` 与 method-not-found 安全回退。

## 21. 与第 2 阶段的交付边界

第 1 阶段只在以下条件全部满足后交付第 2 阶段：

1. 用户 MCP 配置、加密凭据和健康状态可被认证 API 管理。
2. Gateway 可在 service-level integration test 中完成按需 connect/list/call/cancel/close，不需要新增对普通用户公开的“任意工具调用 API”。
3. 任务结束后没有用户 Client、Tool List、Schema 或临时文件残留。
4. 旧的全局 MCP Runtime 执行链未被切换，因此本阶段可以独立回滚。

## 22. 参考

- [MCP 2026-07-28 Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP 2026-07-28 发布说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/)（无协议 Session、List Cache Hint、Header Routing）
- [MCP 2026-07-28 Server Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [MCP 2026-07-28 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [MCP 2026-07-28 Multi-Round-Trip Requests](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr)
- [MCP Tasks Extension](https://modelcontextprotocol.io/extensions/tasks/overview)
- [OpenAI Codex Configuration Reference](https://developers.openai.com/codex/config-reference/)（MCP `startup_timeout_sec` / `tool_timeout_sec` 对照）

本 PRD 的 60 秒发现尝试、单次重试和工具无硬超时是本产品已确认策略，不声称为 Codex 或 MCP 协议的默认值。
