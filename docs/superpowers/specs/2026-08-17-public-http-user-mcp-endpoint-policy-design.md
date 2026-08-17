# 用户级 MCP 公网 HTTP/HTTPS Endpoint Policy 设计

日期：2026-08-17

状态：设计已确认，待实施

适用范围：用户级按需 MCP Server 配置、健康检查、任务级 Gateway、前端 MCP 设置弹窗及相关运行手册

## 1. 目标

用户可以添加任意公网 HTTP 或 HTTPS MCP Server，不再要求管理员预先维护域名或 CIDR 白名单。系统继续在服务端执行 URL、DNS、IP、重定向和连接目标校验，阻止用户配置访问私网、回环、链路本地、云元数据及其他特殊地址。

本设计只取消“公网 HTTP 必须命中管理员白名单”这一条，不取消 Endpoint Policy、SSRF 防护、DNS rebinding 防护、重定向防护或具体 MCP Tool 的授权流程。

## 2. 已确认决策

1. 任意公网 HTTP/HTTPS Endpoint 均可配置，端口允许 `1-65535` 的合法值。
2. 公网 HTTPS 直接保存，不增加风险确认。
3. 公网 HTTP 在前端新建时显示一次风险确认；Endpoint 从 HTTPS 改为 HTTP 时再次显示。
4. 公网 HTTP 可以携带 Bearer Token、API Key 或受控静态 Header。
5. 风险确认只存在于前端交互，不增加 API 请求确认字段，不写数据库确认记录。
6. 直接调用后端 API 创建公网 HTTP MCP 不要求额外确认字段。
7. 保存 MCP 代表允许系统立即使用配置和凭据执行连接、协议协商及 `tools/list` 健康检查。
8. 保存 MCP 不代表授权其全部 Tool；真正执行 `tools/call` 时继续使用现有逐工具授权和长期 Grant 机制。
9. 私网、回环、链路本地、云元数据、多播、保留、未指定等地址继续由后端强制拒绝，用户不能确认放开。
10. DNS rebinding、跨 Origin 重定向及 HTTPS 降级到 HTTP 继续拒绝；只允许现有同源 `307/308` 重定向规则。
11. 管理员 MCP Endpoint 域名/CIDR 白名单从配置、运行时逻辑和文档中直接删除。
12. 现有 `plaintext_http` 诊断标记继续保留，但不阻止调用，也不记录凭据。

## 3. 当前问题与证据

当前 `EndpointPolicy` 对所有 HTTP Endpoint 设置 `allowlist_required`，未命中 `MAF_USER_MCP_ALLOWLIST_DOMAINS` 或 `MAF_USER_MCP_ALLOWLIST_CIDRS` 时返回 `mcp_endpoint_http_not_allowlisted`。因此合法公网 HTTP MCP 也需要管理员介入，与用户自助添加 MCP 的产品目标冲突。

前端保存失败时只调用外层全局 toast。该 toast 的层级低于 Drawer 内的 Modal，且 API client 未把后端 MCP 安全原因码映射成用户可读消息，用户会看到“点击 OK 没有反应”。

本设计取代用户级 MCP 阶段一 PRD 中以下旧口径：

- 公网或企业 HTTP 必须进入管理员域名/CIDR 白名单；
- 企业私网 HTTPS 可以通过管理员白名单放开；
- 验收要求 HTTP/私网目标只能访问管理员白名单范围。

新的权威口径是：公网 HTTP/HTTPS 允许，所有私网和特殊地址始终拒绝，不再存在用户级 MCP Endpoint 管理员白名单例外。

## 4. 目标 Endpoint Policy

| Endpoint 分类 | 保存 | 健康检查与调用 | 用户确认 |
|---|---|---|---|
| 公网 HTTPS | 允许 | 允许 | 不需要 |
| 公网 HTTP | 允许 | 允许并标记 `plaintext_http` | 前端新建或 HTTPS 改 HTTP 时确认一次 |
| 私网 HTTP/HTTPS | 拒绝 | 拒绝 | 不能放开 |
| localhost/回环 | 拒绝 | 拒绝 | 不能放开 |
| 链路本地/云元数据 | 拒绝 | 拒绝 | 不能放开 |
| 多播/保留/未指定地址 | 拒绝 | 拒绝 | 不能放开 |
| 不支持的 Scheme、URL user-info、非法端口 | 拒绝 | 拒绝 | 不能放开 |

Endpoint 在保存、健康检查和任务连接时继续解析并校验。连接只能使用校验时确定的公网 IP 集合；解析结果变化或实际连接地址不在集合内时按 DNS rebinding 拒绝。

## 5. 前端交互

### 5.1 公网 HTTP 风险确认

用户新建 HTTP MCP，或把 Endpoint 从 HTTPS 改为 HTTP 时，首次点击保存不立即提交，而是显示确认弹窗。提示必须明确说明：

- HTTP 连接未使用 TLS；
- MCP 请求、响应及 Bearer Token/API Key 可能被网络链路观察或篡改；
- 继续代表用户接受当前 Endpoint 的风险。

用户取消后保留原表单内容。用户确认后立即提交原请求，不向 API 增加确认字段，也不持久化确认状态。编辑名称、路由描述、启停状态、认证方式或凭据时，只要 Endpoint 仍是原 HTTP Endpoint，就不重复提示。

### 5.2 保存错误

保存失败时：

- MCP 设置 Modal 保持打开；
- 保留全部已填写内容；
- OK 按钮恢复可点击；
- 在 Modal 内显示明确、可见的错误 Alert；
- 不依赖全局 toast 作为唯一错误反馈。

安全错误映射至少覆盖：

| 后端原因 | 用户提示口径 |
|---|---|
| `mcp_endpoint_private_forbidden` | 该地址解析到不允许访问的私网地址 |
| `mcp_endpoint_ip_forbidden` | 该地址属于回环、链路本地、云元数据或其他禁止网段 |
| `mcp_endpoint_dns_failed` | 无法解析 MCP Server 地址 |
| `mcp_endpoint_dns_rebinding` | MCP Server 的 DNS 解析结果发生不安全变化 |
| `mcp_endpoint_redirect_cross_origin` | MCP Server 返回了不允许的跨域重定向 |
| `mcp_endpoint_redirect_downgrade` | MCP Server 尝试从 HTTPS 降级到 HTTP |
| 未知错误 | MCP Server 配置未保存，请稍后重试 |

提示不得包含凭据、完整响应体、内部堆栈或敏感 Header。

## 6. 后端与配置变更

`EndpointPolicy` 保留公网/私网及特殊地址分类，但改为：

- 公网 HTTP 不再查询 allowlist，校验通过后返回 `plaintext_http=true`；
- 公网 HTTPS 行为不变；
- 私网地址无论 HTTP/HTTPS 均以稳定原因码 `mcp_endpoint_private_forbidden` 拒绝；
- 特殊地址、DNS rebinding 与重定向规则不变。

删除用户级 MCP Endpoint 准入对以下环境变量的读取、装配、运行手册说明和测试：

```text
MAF_USER_MCP_ALLOWLIST_DOMAINS
MAF_USER_MCP_ALLOWLIST_CIDRS
```

部署环境如仍残留这些变量，应在本次部署配置更新中直接移除。运行时不再使用它们，也不为它们保留兼容语义。

API DTO 不增加风险确认字段。现有 `CreateUserMCPServerRequest` 和 `PatchUserMCPServerRequest` 请求形状保持不变。

## 7. 已有配置的处理

升级后不得删除任何已有 MCP Server 或加密凭据。

- 已有公网 HTTP/HTTPS 配置按新策略重新测试；公网目标可恢复为 `available`。
- 启动后的已有配置安全重检负责把私网配置持久化标记为 `unavailable` 并停止自动路由；即使该重检尚未完成，Gateway 在连接前的强制重检也必须拒绝私网目标。
- 前端展示明确的私网 Endpoint 不再支持原因。
- 不自动把私网 Endpoint 改写为其他地址，不自动删除凭据。

## 8. Tool 授权边界

本设计不修改现有 MCP Tool 授权模型：

- Server 保存和 `tools/list` 不要求 Tool 授权；
- `tools/call` 仍检查具体 Tool 的一次允许、始终允许或拒绝决策；
- Server 后续新增 Tool 不继承其他 Tool 的长期授权；
- 现有 Grant 撤销、失效和安全版本绑定保持不变。

## 9. 审计与诊断

继续记录现有低敏感度 `plaintext_http` 分类，用于运行诊断、审计统计和故障定位。不得记录：

- Bearer Token、API Key 或静态 Header 值；
- 完整 Authorization Header；
- MCP 请求或响应正文；
- 用户在前端看到的风险确认正文。

本设计不新增风险确认数据库表、确认版本、确认时间或专用审计事件。

## 10. 测试与验收

### 10.1 Endpoint Policy

- 公网 HTTPS 默认通过。
- 公网 HTTP 默认通过并返回 `plaintext_http=true`。
- 公网 HTTP 自定义端口通过。
- 私网 IPv4/IPv6 无法通过任何配置放开。
- 回环、链路本地、云元数据、多播、保留和未指定地址继续拒绝。
- DNS rebinding、连接 IP 偏离、跨域重定向和 HTTPS 降级继续拒绝。

### 10.2 API 与运行时

- API 可直接创建公网 HTTP MCP，不需要额外确认字段。
- 公网 HTTP 可使用 `none`、Bearer、API Key Header 和受控静态 Header 认证。
- 创建后立即进入现有健康检查并可完成协议协商及 `tools/list`。
- 已有私网配置保留数据但变为 `unavailable`。
- `plaintext_http` 诊断标记继续存在且不泄漏凭据。

### 10.3 前端

- 新建 HTTP MCP 时先显示风险确认，取消后不发请求并保留表单。
- 确认后只发送一次创建请求。
- HTTPS 改 HTTP 时重新确认；其他 HTTP 配置编辑不重复确认。
- 保存 4xx/5xx 时 Modal 内显示错误、保留内容并恢复按钮。
- 私网、DNS、重定向等安全错误显示具体中文原因。
- 错误反馈不被 Drawer/Modal 遮挡。

### 10.4 授权回归

- 保存 Server 不创建 wildcard Tool Grant。
- 未授权 Tool 仍触发现有授权流程。
- 远端新增 Tool 不自动继承其他 Tool 的 Grant。

## 11. 明确不做

- 不允许私网、localhost 或云元数据 MCP。
- 不提供用户级“放开私网”开关。
- 不增加 API 风险确认字段或数据库确认记录。
- 不取消具体 Tool 的执行授权。
- 不修改 MCP 协议版本、Transport、凭据加密或任务恢复机制。
- 不自动迁移、改写或删除已有 Endpoint 和凭据。

## 12. 完成条件

只有第 10 节全部定向测试通过，相关旧 PRD、运行手册、环境配置说明、`docs/AGENTS.md` 和 `CHANGELOG.md` 同步完成，且公网 HTTP 的真实本地健康检查能进入 `testing/available`，才可宣称本设计实施完成。
