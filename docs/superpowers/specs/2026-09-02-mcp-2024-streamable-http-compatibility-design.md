# MCP 2024-11-05 + Streamable HTTP 最小兼容设计

状态：`implemented_verified`

目标分支：`main`

## 1. 目标

允许 MCP `2024-11-05` 使用现有 `streamable_http` direct POST transport，包括显式固定版本和
`auto` 降级协商。现有 `2024-11-05 + legacy_http_sse` 继续合法。

这是对非标准混合 Server 的兼容扩展，不把 MCP 2024 的 canonical remote transport 从
`legacy_http_sse` 改成 `streamable_http`。

## 2. 当前阻断

当前存在两层独立阻断：

1. 用户级 MCP 配置校验拒绝 `streamable_http + 2024-11-05`；
2. 共享协议矩阵认为 2024 与 `streamable_http` 不兼容，导致显式客户端或 `auto` 降级即使完成
   initialize 也会失败。

现有 factory 已能为 `streamable_http` 创建 `StreamableHTTPTransport`，并通过旧版
`MCPClient` 发送 2024 initialize、notification 和 Tool 请求，因此不新增 transport 或 adapter。

## 3. 最小修改

### 3.1 共享协议矩阵

`is_mcp_transport_family_allowed()` 对 2024 同时接受：

- `legacy_http_sse`；
- `streamable_http`；
- 既有 `stdio`。

2025/2026 的 transport 规则不变。`mcp_remote_transport_family_for_protocol_version()` 仍把 2024
映射到 canonical `legacy_http_sse`，避免改写既有 conformance、诊断和官方 SDK 证据语义。

### 3.2 配置校验

用户级 MCP 的 create/patch 允许 `streamable_http + 2024-11-05`。其他非法组合继续拒绝，尤其是
2025/2026 与 `legacy_http_sse`。

旧全局 MCP 配置复用共享协议矩阵，因此同步接受该兼容组合；不新增单独开关或重复规则。

### 3.3 显式与 auto 路径

- 显式 `2024-11-05 + streamable_http` 继续复用 `PythonLegacyMCPClientAdapter` 和
  `StreamableHTTPTransport`。
- `auto + streamable_http` 仍先尝试 2026；只有既有 initialize fallback 条件命中时才创建一次
  unpinned 2025 client。Server 若协商为 2024，共享协议矩阵允许继续使用当前 adapter。
- 只有协商为 `2025-11-25` 才包装 `MCP2025TasksAdapter`；2024 不获得 Tasks 能力。

## 4. 响应与错误边界

- direct POST 继续接受 JSON 或 request-scoped SSE JSON-RPC 响应；
- notification 的 HTTP 202/204 无正文继续视为成功；
- 已实现的单向响应 ID 兼容继续适用：整数请求 ID 可接受同值整数或规范十进制字符串响应 ID；
- 认证、网络、timeout、429、5xx、运行期 Tool 错误及非法响应不增加 fallback；
- 不根据响应正文猜测协议或 transport。

## 5. 测试

1. 共享矩阵证明 2024 同时允许 legacy HTTP+SSE、Streamable HTTP 和 stdio，其他版本规则不变；
2. 用户级 create/patch 接受 2024 + Streamable HTTP，并继续拒绝 2025/2026 + legacy HTTP+SSE；
3. 旧全局配置接受该组合；
4. 显式 2024 direct POST 覆盖 JSON、SSE、notification HTTP 202 和数字字符串响应 ID；
5. `auto + streamable_http` 可在既有 fallback 后协商到 2024，且不包装 Tasks adapter；
6. 相关 MCP integrations 与静态门禁通过。

## 6. 非目标

- 不修改 2024 canonical transport 映射；
- 不删除或弱化 legacy HTTP+SSE；
- 不为 2024 增加 Session、Tasks、elicitation、MRTR 或 2026 能力；
- 不修改 API DTO、数据库 schema、凭据、Header、Endpoint policy、Frontend、Rust 或部署配置；
- 不新增依赖、feature flag、transport 自动探测或兼容 fallback 次数。

## 7. 回滚

恢复配置层和共享矩阵的两处拒绝规则，并恢复相应测试即可。无需迁移数据库；已经保存的该组合配置
在回滚版本中会重新被运行时拒绝。

## 8. 实施证据

- 生产修改严格限定为共享协议矩阵和用户配置校验两处；factory、transport、adapter、DTO、schema、
  Frontend、Rust 与部署配置均未修改。
- 红测精确出现共享矩阵/全局配置、用户配置、显式 direct HTTP 和 auto 协商到 2024 的预期失败；
  最小修复后聚焦 41 项通过。
- 用户 MCP API 6 项通过；MCP integrations 569 项通过，其中 2 项为既有环境 skip。
- `compileall`、变更面 Ruff 和 `git diff --check` 通过。
