# 用户级 MCP 公网 HTTP/HTTPS Endpoint Policy 设计

日期：2026-08-17

状态：`main` 仓库实现完成并通过自动回归与真实 OCR MCP 隔离 smoke；`prod` 未变更，开发环境容器人工验收待部署时执行

适用范围：`main` 分支开发环境的用户级按需 MCP Server 配置、健康检查、任务级 Gateway、Phase 3 evidence、legacy migration CLI、前端 MCP 设置弹窗及相关运行手册

不适用范围：`prod` 分支和生产环境。生产策略保持不变，后续变更必须单独设计、审查和批准。

## 1. 目标

用户可以添加任意公网 HTTP 或 HTTPS MCP Server，不再要求管理员预先维护域名或 CIDR 白名单。系统继续在服务端执行 URL、DNS、IP、重定向和连接目标校验，阻止用户配置访问私网、回环、链路本地、云元数据及其他特殊地址。

本设计只取消“公网 HTTP 必须命中管理员白名单”这一条，不取消 Endpoint Policy、SSRF 防护、DNS rebinding 防护、重定向防护或具体 MCP Tool 的授权流程。

## 1.1 用户、角色与受影响系统

| 角色/系统 | 影响 |
|---|---|
| 已认证用户 | 可自行添加任意公网 HTTP/HTTPS MCP；公网 HTTP 在前端确认明文传输风险 |
| API 调用方 | 可直接创建公网 HTTP MCP；API 不增加风险确认字段 |
| backend | 继续承担 URL/DNS/IP/重定向校验、健康检查、连接前重检和错误码持久化 |
| MCP Gateway | 只连接校验后的公网地址；具体 `tools/call` 继续执行现有授权检查 |
| 运维/发布人员 | 删除开发环境管理员 Endpoint 白名单配置；按 `main` 开发环境验证，不修改 `prod` |
| Phase 3 evidence/legacy migration | 更新 public HTTP 场景和 CLI，保留历史 evidence 的只读兼容 |

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
13. HTTP Endpoint 改成另一个 HTTP Endpoint 时不重复提示；只有新建 HTTP 或 HTTPS 改 HTTP 时提示。
14. 本设计只在 `main` 开发环境实施；`prod` 不变。

## 3. 当前问题与证据

当前 `EndpointPolicy` 对所有 HTTP Endpoint 设置 `allowlist_required`，未命中 `MAF_USER_MCP_ALLOWLIST_DOMAINS` 或 `MAF_USER_MCP_ALLOWLIST_CIDRS` 时返回 `mcp_endpoint_http_not_allowlisted`。因此合法公网 HTTP MCP 也需要管理员介入，与用户自助添加 MCP 的产品目标冲突。

前端保存失败时只调用外层全局 toast。该 toast 的层级低于 Drawer 内的 Modal，且 API client 未把后端 MCP 安全原因码映射成用户可读消息，用户会看到“点击 OK 没有反应”。

当前实现还存在四个与本设计直接相关的事实：

- Phase 3 shadow/evidence 把 public HTTP 场景固定命名为 `allowlisted_http_legacy_sse_success`，并要求 `allowed_by_enterprise_allowlist` provenance；新策略若不更新闭合值会使 evidence 校验失败。
- legacy migration CLI 仍暴露 `--allowlist-domain` 和 `--allowlist-cidr`，并独立构造 `EndpointAllowlist`。
- `ValidatedEndpoint` 已包含 `plaintext_http`，但用户级 health/Gateway/audit 尚未完整传播该布尔值；不能把 legacy global MCP 的 audit 覆盖误认为用户级链路已完成。
- `MCPHealthRunner.start()` 只恢复过期 health attempt，`UserMCPConfigService.start()` 只协调删除；当前没有跨用户扫描全部 Server 的启动重检能力。

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

域名返回多个 A/AAAA 结果时，只有全部地址都属于允许的公网分类才通过；任一结果属于私网或特殊地址时整次校验失败。

## 5. 前端交互

### 5.1 公网 HTTP 风险确认

前端必须使用标准 URL parser 对去除首尾空白后的 Endpoint 进行 scheme 判断，不允许用大小写敏感的字符串前缀判断。用户新建 HTTP MCP，或把 Endpoint 从 HTTPS 改为 HTTP 时，首次点击保存不立即提交，而是显示确认弹窗。HTTP 改为另一个 HTTP Endpoint 不重复提示。提示必须明确说明：

- HTTP 连接未使用 TLS；
- MCP 请求、响应及 Bearer Token/API Key 可能被网络链路观察或篡改；
- 继续代表用户接受当前 Endpoint 的风险。

用户取消后保留原表单内容。用户确认后立即提交原请求，不向 API 增加确认字段，也不持久化确认状态。编辑名称、路由描述、启停状态、认证方式、凭据或 HTTP Endpoint 时不重复提示。

确认流程必须防重入：同一次表单提交最多存在一个风险确认弹窗，用户确认后最多发送一个 API 请求。确认弹窗必须有可访问名称、明确的继续/取消按钮、键盘操作和可预测的焦点返回；取消不得触发网络请求。

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
| `mcp_endpoint_private_not_allowlisted` | 历史兼容：该地址解析到不允许访问的私网地址 |
| `mcp_endpoint_ip_forbidden` | 该地址属于回环、链路本地、云元数据或其他禁止网段 |
| `mcp_endpoint_dns_failed` | 无法解析 MCP Server 地址 |
| `mcp_endpoint_dns_rebinding` | MCP Server 的 DNS 解析结果发生不安全变化 |
| `mcp_endpoint_redirect_cross_origin` | MCP Server 返回了不允许的跨域重定向 |
| `mcp_endpoint_redirect_downgrade` | MCP Server 尝试从 HTTPS 降级到 HTTP |
| 未知错误 | MCP Server 配置未保存，请稍后重试 |

提示不得包含凭据、完整响应体、内部堆栈或敏感 Header。

前端必须从 `ApiError.detail.detail.code` 的安全结构中提取后端原因码；缺少该结构时使用未知错误提示。不得直接渲染任意后端响应正文。

## 6. 后端与配置变更

`EndpointPolicy` 保留公网/私网及特殊地址分类，但改为：

- 公网 HTTP 不再查询 allowlist，校验通过后返回 `plaintext_http=true`；
- 公网 HTTPS 行为不变；
- 私网地址无论 HTTP/HTTPS 均以稳定原因码 `mcp_endpoint_private_forbidden` 拒绝；
- 特殊地址、DNS rebinding 与重定向规则不变。

`EndpointPolicy` 不再接收 `EndpointAllowlist`。`EndpointAllowlist` 及其用户级装配从运行路径删除；历史 evidence 的闭合字符串兼容不等于继续保留 Endpoint 白名单能力。

删除用户级 MCP Endpoint 准入对以下环境变量的读取、装配、运行手册说明和测试：

```text
MAF_USER_MCP_ALLOWLIST_DOMAINS
MAF_USER_MCP_ALLOWLIST_CIDRS
```

部署环境如仍残留这些变量，应在本次部署配置更新中直接移除。运行时不再使用它们，也不为它们保留兼容语义。

API DTO 不增加风险确认字段。现有 `CreateUserMCPServerRequest` 和 `PatchUserMCPServerRequest` 请求形状保持不变。

### 6.1 健康检查与错误码

`EndpointPolicyError` 必须在 health 和 Gateway 边界保留稳定原因码，不得统一收敛为 `tool_discovery_failed`。发生 Endpoint 拒绝时：

- 不解密或发送凭据；
- 不建立远端连接；
- health attempt 或 Gateway 使用当前 `config_version/security_version` 做 CAS；
- 成功 CAS 后写入 `health_status=unavailable`、精确 `last_test_error_code` 和 `last_tested_at`；
- CAS 失败代表配置已变化，不覆盖新版本状态。

health、Gateway enforce/shadow 和远端 Task recovery 必须统一使用 validated-endpoint-first 合同：

```text
Endpoint validate/revalidate
→ 得到不可变 ValidatedEndpoint
→ 解密当前 Server 凭据
→ client factory 消费该 ValidatedEndpoint，不再次解析 DNS
→ policy-bound connection 只能连接 ValidatedEndpoint.allowed_ips
```

Endpoint 校验失败时不得调用 credential loader。一次 client/session 建立流程只能产生一个权威 `ValidatedEndpoint`；不得在解密凭据后再次解析 Endpoint。连接前的实际 IP 仍必须命中该对象固定的 `allowed_ips`。

远端 Task recovery 只调整上述安全连接顺序，不修改允许的方法集合、协议版本绑定、密封恢复状态、claim/lease/CAS、取消、结果提交或禁止重放语义。

### 6.2 Phase 3 evidence 兼容

新策略的 canonical public HTTP shadow 场景为：

```text
scenario=public_http_legacy_sse_success
transport=legacy_http_sse
endpoint_policy=runtime_enforced
```

实现必须同步更新：

- `ShadowScenario` 与 `MCPShadowScenario`；
- `SHADOW_SCENARIO_EXPECTATIONS` 和 closed-value validators；
- PostgreSQL SECURITY DEFINER evidence 校验函数及其 expectation matrix；
- shadow manifest、fixture、Python/SQL 集成测试和 rollout runbook。

历史 `allowlisted_http_legacy_sse_success` 与 `allowed_by_enterprise_allowlist` 只作为已持久化 evidence 的读取兼容值保留，在其现有 retention/ledger 生命周期内继续可验证；新用户级运行路径不得再生成这两个值。实现不得通过删除旧 closed value 使历史 evidence 失效，也不得把旧样本重写成新场景。

实现必须把“可接受的历史值”和“当前 deployment 必需值”拆成两个集合，禁止继续用完整 enum 自动派生当前 Gate 必需场景：

- historical accepted scenarios/policies：允许读取、校验摘要和查询旧 `allowlisted_http_legacy_sse_success + allowed_by_enterprise_allowlist` sample；
- current required scenarios：只包含当前合同要求的场景，其中 HTTP 场景必须是 `public_http_legacy_sse_success + runtime_enforced`；
- current producer：只为 current required scenarios 生成 observation，不为历史场景生成零计数占位；
- current Gate：只检查 current required scenarios 的样本门槛，不要求历史场景产生新样本；
- 历史 sample/snapshot 不得用于满足新 deployment/config fingerprint 的 Gate，只保证原始记录、摘要和历史查询仍可验证。

Python snapshot producer、Gate validator 与 PostgreSQL snapshot producer/validator 必须使用相同的 current required 顺序和集合，并通过 schema-contract 测试防止再次漂移。

### 6.3 Legacy migration CLI

删除 `migrate_legacy_mcp_config.py` 的 `--allowlist-domain`、`--allowlist-cidr` 参数及 `EndpointAllowlist` 装配。迁移后的公网 HTTP/HTTPS Endpoint 使用与 API 相同的 policy；私网和特殊地址迁移继续失败关闭。同步更新 CLI 测试和 `user-mcp-phase3-rollout.md`。

## 7. 已有配置的处理

升级后不得删除任何已有 MCP Server 或加密凭据。本设计不增加启动时跨用户全库 Endpoint 扫描，也不让配置列表 GET 产生 DNS 或写库副作用。

- 已有公网 HTTP/HTTPS 配置按新策略重新测试；公网目标可恢复为 `available`。
- 已有私网配置在下一次显式健康检查或 Gateway 连接前重检时被拒绝，并以第 6.1 节的 CAS 规则持久化为 `unavailable`；在此之前列表可能仍显示历史 health 状态，但任何真实连接都必须失败关闭。
- 前端展示明确的私网 Endpoint 不再支持原因。
- 不自动把私网 Endpoint 改写为其他地址，不自动删除凭据。

### 7.1 开发发布预检与安全红线

Gateway 运行期的 Endpoint Policy 拒绝继续报告现有 authoritative `ENDPOINT_POLICY_BYPASS / endpoint_policy_rejected` 红线；本设计不降低、改名或自动清零该门禁。API 保存阶段的正常拒绝和发布前显式 health re-test 的拒绝不属于 Gateway bypass 红线。

在 `main` 开发环境启用新路由前，发布人员必须枚举该环境全部 enabled 用户 MCP Server，并逐一触发现有显式 health re-test：

- 公网 HTTP/HTTPS 可按新策略进入 `available`；
- 私网或特殊地址必须先以精确原因码 CAS 标记为 `unavailable`；
- 预检完成前不得把旧 `available` 状态视为新策略已验证；
- 预检或后续运行出现非零 Endpoint Policy 红线时立即停止验收，不得自动改写、抑制或清零 evidence；
- 预检不删除 Server、凭据或 Grant。

实现计划必须选择一个现有 owner-scoped API/运维入口完成该开发环境预检；如果缺少跨用户只读枚举入口，应提供受限的开发运维命令，而不是在 API Runtime 启动路径加入无界全库扫描。

## 8. Tool 授权边界

本设计不修改现有 MCP Tool 授权模型：

- Server 保存和 `tools/list` 不要求 Tool 授权；
- `tools/call` 仍检查具体 Tool 的一次允许、始终允许或拒绝决策；
- Server 后续新增 Tool 不继承其他 Tool 的长期授权；
- 现有 Grant 撤销、失效和安全版本绑定保持不变。

## 9. 审计与诊断

保留 `ValidatedEndpoint.plaintext_http` 字段，并把它从用户级 health/Gateway 的校验结果传播到安全诊断、低基数指标与字段 allowlist 审计。用户级链路还必须派生 `credential_over_plaintext_http` 布尔值；该值只表示 HTTP Server 配置了非 `none` 认证，不包含凭据内容。不得记录：

- Bearer Token、API Key 或静态 Header 值；
- 完整 Authorization Header；
- MCP 请求或响应正文；
- 用户在前端看到的风险确认正文。

上述两个布尔值必须进入用户级链路的定向测试和敏感字段扫描。本设计不新增风险确认数据库表、确认版本、确认时间或专用审计事件；legacy global MCP 的既有 `plaintext_http` audit 行为保持不变。

## 10. 测试与验收

### 10.1 Endpoint Policy

- 公网 HTTPS 默认通过。
- 公网 HTTP 默认通过并返回 `plaintext_http=true`。
- 公网 HTTP 自定义端口通过。
- 公网域名同时解析出公网和私网/特殊地址时整次拒绝。
- 私网 IPv4/IPv6 无法通过任何配置放开。
- 回环、链路本地、云元数据、多播、保留和未指定地址继续拒绝。
- DNS rebinding、连接 IP 偏离、跨域重定向和 HTTPS 降级继续拒绝。

### 10.2 API 与运行时

- API 可直接创建公网 HTTP MCP，不需要额外确认字段。
- 公网 HTTP 可使用 `none`、Bearer、API Key Header 和受控静态 Header 认证。
- 创建后立即进入现有健康检查并可完成协议协商及 `tools/list`。
- 已有私网配置保留数据但变为 `unavailable`。
- health 和 Gateway Endpoint 拒绝保留精确错误码，并使用版本 CAS 更新 `unavailable`。
- health、Gateway enforce/shadow 和 remote recovery 在 Endpoint 校验成功前不调用 credential loader，且每次 client/session 建立只绑定一个 `ValidatedEndpoint`。
- 用户级 `plaintext_http` 与 `credential_over_plaintext_http` 诊断存在且不泄漏凭据。

### 10.3 前端

- 新建 HTTP MCP 时先显示风险确认，取消后不发请求并保留表单。
- 确认后只发送一次创建请求。
- HTTPS 改 HTTP 时重新确认；HTTP 改另一个 HTTP 或其他 HTTP 配置编辑不重复确认。
- 大小写、首尾空白和无效 URL 不得绕过或错误触发 HTTP 风险判断。
- 风险确认防重复弹窗和重复 API 请求，并满足键盘与焦点可访问性。
- 保存 4xx/5xx 时 Modal 内显示错误、保留内容并恢复按钮。
- 私网、DNS、重定向等安全错误显示具体中文原因。
- 新旧私网错误码 `mcp_endpoint_private_forbidden` / `mcp_endpoint_private_not_allowlisted` 显示同一安全提示。
- 错误反馈不被 Drawer/Modal 遮挡。

### 10.4 授权回归

- 保存 Server 不创建 wildcard Tool Grant。
- 未授权 Tool 仍触发现有授权流程。
- 远端新增 Tool 不自动继承其他 Tool 的 Grant。

### 10.5 Evidence、CLI 与兼容回归

- 新 `public_http_legacy_sse_success + runtime_enforced` shadow sample 通过 Python 和 PostgreSQL validator。
- 历史 `allowlisted_http_legacy_sse_success + allowed_by_enterprise_allowlist` 样本仍可读取和验证，但新运行路径不再产生。
- Python/PostgreSQL current producer 只输出 current required scenarios；Gate 不要求历史场景产生新样本，历史样本不能满足新 deployment/config fingerprint 的门槛。
- Python 与 PostgreSQL 的 current required scenario 集合、顺序和 expectation matrix 通过 schema-contract 测试保持一致。
- legacy migration CLI 不再接受 allowlist 参数；公网 HTTP 可迁移，私网 Endpoint 拒绝且不写 Server/credential/migration record。
- 旧环境变量、CLI 参数、PRD 和 runbook 引用完成仓库级检索清理；历史变更日志和历史 evidence 字段不做破坏性重写。
- fake credential loader 证明 health、Gateway enforce/shadow 和 remote recovery 的 Endpoint 拒绝路径调用次数为零；fake resolver 证明每次 client/session 建立只产生一个权威 `ValidatedEndpoint`。

### 10.6 验证分层

- 默认 CI 只使用 fake resolver、fake server 和本地 fixture，不依赖真实外部 MCP。
- `main` 开发环境构建镜像后执行一次人工 QA：使用真实公网 OCR MCP 完成保存、health、`tools/list` 和状态展示；Token 只从受控凭据输入进入，不写命令、日志或测试 artifact。
- 人工 QA 前完成全部 enabled Server 的显式 health 预检，并验证 Endpoint Policy authoritative red line 保持为零；发现非零时验收失败。
- `prod` 不运行本设计的部署或验收步骤。

## 11. 明确不做

- 不允许私网、localhost 或云元数据 MCP。
- 不提供用户级“放开私网”开关。
- 不增加 API 风险确认字段或数据库确认记录。
- 不取消具体 Tool 的执行授权。
- 不修改 MCP 协议版本、Transport、凭据加密算法或远端 Task recovery 的协议/状态机语义；仅允许调整建立 client 前的 Endpoint 校验与凭据加载顺序。
- 不自动迁移、改写或删除已有 Endpoint 和凭据。
- 不修改 `prod` 分支或生产部署配置。

## 12. 非功能要求

| 维度 | 要求 |
|---|---|
| 安全 | 每次保存、健康检查、连接与重定向都执行 Endpoint Policy；任何非公网解析结果失败关闭 |
| 隐私 | 凭据、Header 值、Endpoint 响应正文不进入前端错误、日志、指标或审计 |
| 可靠性 | 保存/健康/Gateway 的 Endpoint 原因码稳定；状态更新使用 config/security version CAS |
| 兼容性 | 历史 evidence 可读；现有 Server/凭据不删除；API DTO 不变 |
| 可访问性 | HTTP 风险确认和保存错误支持键盘、焦点、可访问名称与实时状态反馈 |
| 可观测性 | `plaintext_http` 和 `credential_over_plaintext_http` 仅以低敏感度布尔值记录 |

## 13. 风险、假设与回滚

### 13.1 已接受风险

- 公网 HTTP 没有 TLS，MCP 请求、响应和认证凭据可能被链路观察或篡改。
- 前端会提示该风险，但直接 API 调用方不会收到交互式提示；这是已确认的产品决策，API 调用方被假设为理解其提交的 URL scheme。
- 公网 MCP 本身可能不可信；保存只允许连接/发现，不授予其当前或未来 Tool 的自动执行权限。

### 13.2 假设

- “公网”以服务端当前 IP 分类器为权威，所有解析结果都必须为 global/public。
- 本轮只面向 `main` 开发环境；生产风险接受、合规与发布窗口尚未批准。
- 现有历史 evidence 必须保持可验证，不能通过改写历史数据完成 schema 迁移。

### 13.3 回滚

- 回滚到旧代码后，新保存的公网 HTTP MCP 会再次受管理员白名单策略限制并可能变为 `unavailable`。
- 回滚不得删除 Server、凭据、Grant、health history 或 evidence。
- 回滚开发部署时恢复旧运行时代码即可；不恢复已删除的开发环境白名单变量，除非单独执行并记录旧版本兼容操作。
- `prod` 未进入本轮，因此不存在本设计导致的生产回滚动作。

## 14. 实施依赖与同步范围

| 范围 | 主要文件/合同 |
|---|---|
| Endpoint Policy/runtime 装配 | `src/integrations/mcp/endpoint_policy.py`、`src/api/runtime.py` |
| health/Gateway/recovery 状态与错误 | `src/integrations/mcp/health.py`、`gateway.py`、`user_client.py`、`recovery_worker.py`、`src/api/runtime.py` 的 recovery client factory |
| API/前端错误与确认 | `src/api/routes/user_mcp.py`、`frontend/src/api/client.ts`、`MCPSettingsPanel.tsx` |
| Phase 3 evidence | `shadow_compare.py`、`rollout_evidence.py`、`shadow_evidence.py`、PostgreSQL permissions/validator SQL |
| legacy migration | `scripts/migrate_legacy_mcp_config.py` 及 CLI/集成测试 |
| 开发发布预检 | owner-scoped MCP Server/test API；如不足则增加受限开发运维命令及其 owner/凭据脱敏测试 |
| 文档 | 三阶段用户级 MCP PRD、gateway/phase3 runbook、`docs/AGENTS.md`、`CHANGELOG.md` |

## 15. 开发环境发布顺序

1. 先更新 Endpoint/evidence/CLI 合同和全部定向测试，锁定 historical accepted 与 current required evidence 集合。
2. 更新 backend policy、health/Gateway 原因码与诊断传播。
3. 更新前端风险确认、Modal 内错误和前端测试。
4. 更新旧 PRD、runbook、环境变量与 CLI 文档。
5. 在 `main` 构建开发镜像并执行默认 CI/定向回归。
6. 枚举开发环境全部 enabled MCP Server 并逐一显式 health re-test；私网/特殊地址先收敛为 `unavailable`，确认 Endpoint Policy 红线为零。
7. 使用真实 OCR MCP 完成开发环境人工 QA，确认公网 HTTP、凭据、health、Tool discovery 和错误显示。
8. 不发布或修改 `prod`；生产变更另行审批。

## 16. 完成条件

只有第 10 节全部定向测试、第 12 节非功能要求和第 15 节开发环境发布顺序全部满足，相关旧 PRD、运行手册、环境配置说明、CLI、evidence closed contract、`docs/AGENTS.md` 和 `CHANGELOG.md` 同步完成，且真实 OCR MCP 的 `main` 开发环境人工 QA 能进入 `testing/available`，才可宣称本设计在开发环境实施完成。不得据此宣称 `prod` 已变更或生产准入完成。
