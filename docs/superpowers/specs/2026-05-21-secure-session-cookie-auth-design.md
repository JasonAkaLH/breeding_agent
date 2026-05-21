# 安全 Session Cookie 与多客户端认证设计

日期：2026-05-21
状态：已通过 document-perfectization 审查并加固，等待实现计划
范围：同站同域浏览器前端、跨站/第三方浏览器前端、非浏览器 REST API 客户端的认证边界、Cookie 策略、Bearer Token 策略、CORS / CSRF / SSE 兼容约束

## 1. 问题陈述

当前系统已经支持基于服务端 session 的登录态：后端在登录 / 注册后写入 `maf_session` Cookie，后续接口通过 Cookie 中的 session ID 恢复用户。用户明确要求 Cookie 里只能有 `session_id`，不能暴露用户名，并补充未来会同时存在：

- 和后端同站同域部署的浏览器前端。
- 两三个跨站或第三方浏览器前端调用 REST API。
- 非浏览器客户端调用 REST API。

如果所有场景都统一使用一个跨站 Cookie，就会扩大 CSRF 与第三方 Cookie 风险；如果所有场景都只用 Cookie，也不适合 CLI、脚本、后端服务等非浏览器客户端。因此认证必须按客户端类型拆分默认路径，同时保持身份恢复、撤销、权限隔离和审计边界一致。

## 2. 目标

1. Cookie 中只保存不透明随机 `session_id`，不得包含用户名、账号、角色、邮箱、租户、scope 等可识别用户或授权语义的信息。
2. 同站同域浏览器前端继续使用服务端 Session Cookie，保持浏览器自动携带 Cookie、刷新恢复和服务端集中撤销能力。
3. 跨站 / 第三方浏览器前端默认使用 `Authorization: Bearer <opaque-token>`，不依赖默认 Session Cookie。
4. 非浏览器客户端默认使用 `Authorization: Bearer <opaque-token>`，不依赖 Cookie jar。
5. 只有在明确登记且确实需要的跨站浏览器场景中，才允许单独启用 cross-site cookie profile，并强制 Origin / CORS / CSRF 防护。
6. 所有认证凭据默认都是服务端可撤销的不透明随机值；不把 JWT 或其他自包含用户信息 token 作为第一阶段方案。
7. 认证改造不得破坏现有 conversation / task ownership guard：跨用户访问仍必须 fail closed，并按当前策略隐藏资源存在性。

## 3. 非目标

本设计第一阶段不做以下事情：

- 不默认引入 JWT、自包含 access token 或把用户信息编码进 Cookie / token。
- 不把全站默认 Cookie 改为 `SameSite=None`。
- 不支持未登记 origin 的第三方浏览器访问。
- 不建设完整 OAuth 授权服务器。
- 不在 URL query 中传递 session、Bearer token、refresh token 或长期 stream token。
- 不改变现有业务 API 的 owner guard 语义。
- 不为跨站 Cookie profile 做默认启用；它只是明确记录的兼容例外。

## 4. 用户、客户端与受影响系统

| 类型 | 示例 | 默认认证方式 | 关键风险 | 设计要求 |
| --- | --- | --- | --- | --- |
| 同站同域浏览器前端 | 内部业务对话台 | `__Host-maf_session` Cookie | XSS 读取、CSRF、session fixation | `HttpOnly; Secure; SameSite=Lax; Path=/`，无 `Domain`，只放 opaque session ID |
| 跨站 / 第三方浏览器前端 | 独立部署的业务前端、伙伴前端 | `Authorization: Bearer <opaque-token>` | XSS 暴露 token、CORS 配置错误、SSE header 限制 | origin allowlist、短 TTL、scope、撤销、禁止 wildcard credentials、SSE 使用可带 header 的 fetch stream |
| 非浏览器客户端 | CLI、脚本、服务端集成、移动端原生层 | `Authorization: Bearer <opaque-token>` | token 泄露、日志泄露、长期凭据失控 | token 摘要存储、scope、TTL、revoke、脱敏日志 |
| 例外跨站 Cookie 客户端 | 被明确批准的浏览器集成 | `maf_cross_site_session` Cookie | 第三方 Cookie 拦截、CSRF、配置复杂 | 显式 allowlist、`SameSite=None; Secure; HttpOnly`、Origin 校验、CSRF 证据、短 TTL |

## 5. 当前仓库事实与证据

当前后端认证实现位于：

- `src/api/auth.py`
- `src/api/routes/auth.py`
- `src/auth/services.py`
- `src/api/runtime.py`
- `src/core/models.py`
- `src/storage/sqlite/repositories.py`
- `tests/api/test_auth_login_and_isolation.py`

当前实现事实：

- `src/api/auth.py` 定义 `SESSION_COOKIE_NAME = "maf_session"`，`set_session_cookie()` 写入 Cookie。
- Cookie 值只写入服务端生成的 `session_id`，不写用户名。
- `SessionService.create_session()` 使用 `secrets.token_urlsafe(32)` 生成随机不透明 session ID，当前格式为 `sess-<random>`。
- `require_authenticated_user()` 从 `request.cookies` 读取 session ID，并通过 `runtime.get_session_user(session_id)` 恢复用户。
- `ApiRuntime.get_session_user()` 委托 `SessionService.get_active_user()` 检查 session 是否存在、过期、撤销，以及用户是否 active。
- 登录 / 注册响应 body 返回 `AuthUserResponse(user.username)`，这是响应数据，不是 Cookie 内容。
- `tests/api/test_auth_login_and_isolation.py` 已覆盖登录后 Cookie 不包含 `alice` 或 `username` 的回归断言。
- `src/api/app.py` 当前未配置 CORS middleware；跨站 REST API 调用必须在实现计划中新增配置与测试。
- `frontend/src/api/client.ts` 当前使用 `credentials: 'same-origin'`，适合同站 Cookie 前端；跨站 Bearer 客户端需要单独 client 配置或调用方式。
- `docs/api/api-doc.html` 当前仍以 `maf_session` Cookie 为主要认证说明；实现时必须同步更新文档。

## 6. 外部安全依据

设计参考以下公开安全建议：

- OWASP Session Management Cheat Sheet：session ID 应无业务意义，避免能推导用户或系统信息；应使用 `Secure`、`HttpOnly`、`SameSite` 等 Cookie 属性，并可使用 `__Host-` 前缀强化 Cookie 边界。
  https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- OWASP CSRF Prevention Cheat Sheet：依赖 Cookie 的状态变更请求需要 CSRF 防护，`SameSite` 可降低风险但不应作为所有场景的唯一防线。
  https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- MDN Set-Cookie / SameSite：`SameSite=None` 必须配合 `Secure`；`__Host-` Cookie 必须 `Secure`、`Path=/` 且不能设置 `Domain`。
  https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie/SameSite
- MDN Secure Cookie Configuration：优先使用安全 Cookie 前缀、`Secure`、`HttpOnly` 和合适的 `SameSite`。
  https://developer.mozilla.org/en-US/docs/Web/Security/Practical_implementation_guides/Cookies

## 7. 推荐方案

采用“双通道、默认安全”的认证模型：

1. 同站同域浏览器前端：使用 `__Host-maf_session` 服务端 Session Cookie。
2. 跨站 / 第三方浏览器前端：默认使用 opaque Bearer Token。
3. 非浏览器客户端：默认使用 opaque Bearer Token。
4. 跨站 Cookie：只作为显式 allowlisted 兼容例外。

该方案保留当前服务端 session 架构，并为跨站与非浏览器客户端新增更合适的 API 凭据模型，避免把所有场景强行塞进一个 Cookie 策略。

## 8. 功能需求

| 编号 | 需求 | 验收方式 |
| --- | --- | --- |
| FR-1 | 登录 / 注册成功后，同站 Cookie 中只能写不透明 session ID。 | API 测试断言 Set-Cookie 值不含用户名、`username`、邮箱、角色、scope。 |
| FR-2 | 新默认 Cookie 名必须为 `__Host-maf_session`。 | API 测试断言响应头包含新 Cookie 名。 |
| FR-3 | `__Host_maf_session` 形式不得使用；必须使用带连字符的标准前缀 `__Host-maf_session`。 | 常量测试或文档测试覆盖准确名称。 |
| FR-4 | 新 Cookie 必须设置 `HttpOnly; Secure; SameSite=Lax; Path=/` 且不得设置 `Domain`。 | API 测试解析 Set-Cookie 属性。 |
| FR-5 | `__Host-maf_session` 必须始终带 `Secure`；本地 HTTP 不得降级写出无效 Host Cookie。 | API 测试解析 `Set-Cookie`，实现中固定 `Secure=True`。 |
| FR-6 | 灰度迁移期可以短期读取旧 `maf_session`，但不得继续写入旧 Cookie。 | 迁移测试覆盖新旧读取；登录只写新 Cookie。 |
| FR-7 | logout 必须撤销当前 session，并同时清理新旧 Cookie，避免迁移期残留。 | API 测试覆盖 logout Set-Cookie 清理。 |
| FR-8 | `require_authenticated_user()` 必须支持 Cookie 与 Bearer 两种认证来源。 | API 测试分别使用 Cookie / Bearer 调用受保护接口。 |
| FR-9 | 当 Bearer 与 Cookie 同时存在时，必须优先使用 Bearer。 | API 测试构造不同用户的 Bearer 与 Cookie，断言按 Bearer 用户授权。 |
| FR-10 | Bearer Token 必须是不透明随机值，服务端只保存摘要或不可逆校验材料，不保存可直接使用的明文 token。 | storage / service 测试断言 raw token 不落库。 |
| FR-11 | Bearer Token 必须支持 TTL、revoke、scope、client 标识和最后使用时间。 | storage / API 测试覆盖生命周期字段。 |
| FR-12 | Bearer Token scope 不足时必须 fail closed。 | API 测试覆盖无权访问状态变更接口。 |
| FR-13 | CORS 必须只允许配置中的 origin；不得对凭证请求使用 wildcard origin。 | CORS middleware 测试覆盖 allowlisted / denied origin。 |
| FR-14 | 跨站浏览器使用 Bearer 调用 SSE / event stream 时，必须使用可设置 Authorization header 的 fetch streaming 客户端；不得把 token 放入 URL。 | 文档与前端/客户端测试覆盖跨站 SSE 调用方式。 |
| FR-15 | cross-site cookie profile 不得默认启用；启用时必须同时满足 origin allowlist、`SameSite=None; Secure; HttpOnly`、CSRF 证据和短 TTL。 | 配置测试覆盖默认关闭与显式开启条件。 |
| FR-16 | 日志、审计、错误信息不得输出原始 Cookie、Bearer token、验证码或密码。 | 脱敏测试覆盖认证失败、CORS 拒绝、token revoke 等路径。 |

## 9. 非功能需求

### 9.1 安全与隐私

- Cookie / token 都必须是不透明随机凭据，不承载用户信息或业务权限信息。
- Browser JavaScript 不得读取同站 session Cookie；必须保持 `HttpOnly`。
- 跨站 Bearer token 因需要由第三方前端主动设置 header，存在 XSS 泄露面；因此必须使用短 TTL、最小 scope、可撤销、可轮换策略，并在文档中禁止长期 token 存入不受控页面的 `localStorage` 作为默认建议。
- 所有认证凭据在日志中只能出现脱敏 fingerprint。

### 9.2 可靠性与兼容性

- 同站前端的现有登录、刷新恢复、会话列表、消息提交、SSE 读取、上传和退出登录必须保持可用。
- 旧 `maf_session` 兼容读取只能作为迁移期行为，并应在文档中标注下线条件。
- 跨站前端不得依赖浏览器第三方 Cookie 是否可用；默认 Bearer 路径必须在现代浏览器限制第三方 Cookie 时仍可工作。
- 非浏览器客户端必须能不使用 Cookie 完成 REST API 调用。

### 9.3 可观测性

- 认证失败应记录原因类别，例如 missing、expired、revoked、scope_denied、origin_denied，但不得记录原始凭据。
- Bearer token 使用应记录 client id、scope、脱敏 token fingerprint 和最后使用时间，便于撤销与排障。

### 9.4 性能

- 每次认证检查最多进行一次 session / token 主查表和必要的用户状态查询。
- Bearer token 摘要查询应有唯一索引；不得引入线性扫描。

## 10. Cookie 设计

同站同域前端使用：

```http
Set-Cookie: __Host-maf_session=<opaque-session-id>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=28800
```

约束：

- Cookie 名：`__Host-maf_session`。
- Cookie 值：当前可以继续使用 `sess-<secrets.token_urlsafe(32)>`，但实现应把它视为不透明字符串，不解析业务语义。
- `Secure`：始终启用。开发 / 测试必须使用 HTTPS test harness、localhost 安全例外或显式非浏览器 Bearer 流程；不得写出不满足 `__Host-` 要求的 Cookie。
- `HttpOnly`：必须启用。
- `SameSite`：默认 `Lax`，兼顾同站前端与基础 CSRF 降低。
- `Path`：必须为 `/`。
- `Domain`：不得设置。
- `Max-Age`：第一阶段沿用当前 28,800 秒；未来调整 TTL 需单独评估用户体验与安全。

## 11. Bearer Token 设计

Bearer Token 是跨站 / 第三方前端和非浏览器客户端的默认认证方式：

```http
Authorization: Bearer <opaque-access-token>
```

### 11.1 Token 形态

- token 必须由服务端生成，使用足够熵的随机值。
- 返回给调用方的明文 token 只在创建时出现一次。
- 服务端 storage 不保存明文 token，只保存摘要、token id、client id、username、scope、expires_at、revoked_at、created_at、last_used_at。
- 摘要建议复用项目已有安全 kernel / HMAC 能力；如实现选择其他摘要方式，必须说明 secret 来源与轮换策略。

### 11.2 Token scope

第一阶段最小 scope 集合建议：

| Scope | 含义 |
| --- | --- |
| `conversation:read` | 读取会话列表、消息历史、任务状态、artifact 元数据。 |
| `conversation:write` | 提交消息、重命名 / 删除会话。 |
| `task:control` | 取消任务、回答 interrupt。 |
| `upload:write` | 上传和删除附件。 |
| `capability:read` | 读取公开 capability 列表。 |

实现计划可以先按现有 API 分组细化 scope，但不得把所有 token 默认授予无限权限，除非是受控的内部开发 token 且明确标注。

### 11.3 Token 发放入口

实现计划应新增受保护的 token 管理 API，路径保持当前“非 GET 不在 URL 携带业务 ID”的规则。建议形态：

- `POST /api/v1/auth/api-tokens`：登录用户创建新 token，body 指定 client name、scopes、ttl。
- `GET /api/v1/auth/api-tokens`：列出当前用户的 token 元数据，不返回明文 token。
- `DELETE /api/v1/auth/api-tokens`：body 携带 token id，撤销 token。

这些路径是实施建议；最终实现计划如调整命名，必须同步 API 文档和测试。

## 12. CORS 与 CSRF 策略

### 12.1 CORS

当前 `src/api/app.py` 未配置 CORS middleware。实现跨站 Bearer 客户端前必须新增 CORS 配置：

- allowed origins 来自部署环境变量或显式配置，不写死真实域名到 tracked 文件。
- 对 Bearer 请求允许 `Authorization`、`Content-Type`、必要的 SSE / stream headers。
- 对 credentialed cookie 请求禁止 wildcard origin。
- 未配置跨站 origin 时，默认仅服务同站 / same-origin 调用。

### 12.2 CSRF

- 默认同站 Cookie 使用 `SameSite=Lax` 降低 CSRF 风险。
- 所有依赖 Cookie 的跨站状态变更请求必须有额外 CSRF 防护。
- Bearer token 由客户端显式放入 Authorization header，不会像 Cookie 一样被浏览器自动附加，因此默认不走 Cookie CSRF 模型；但仍必须做好 CORS allowlist 和 XSS 风险提示。
- cross-site cookie profile 若启用，必须至少满足 Origin 校验；如存在复杂嵌入或跳转场景，需增加双提交 CSRF token 或显式 CSRF header。

## 13. SSE / 流式接口约束

当前系统存在 SSE / event stream 使用场景。跨站浏览器如果使用原生 `EventSource`，通常无法自定义 `Authorization` header，因此 Bearer 方案必须明确客户端策略：

- 同站浏览器前端可以继续使用 Cookie 驱动的 SSE。
- 跨站浏览器前端必须使用 `fetch` + `ReadableStream` 或等价可设置 header 的流式客户端来携带 `Authorization: Bearer ...`。
- 不允许把 Bearer token 放进 SSE URL query。
- 如未来必须兼容原生 `EventSource` 跨站场景，应另行设计一次性、短 TTL、服务端绑定的 stream lease，并单独审查其 URL 暴露风险；这不是第一阶段目标。

## 14. 数据、迁移与回滚

### 14.1 Cookie 名迁移

- 新登录 / 注册只写 `__Host-maf_session`。
- `require_authenticated_user()` 可在迁移期读取旧 `maf_session`，但读取优先级必须低于新 Cookie。
- logout 必须同时清理 `__Host-maf_session` 和旧 `maf_session`。
- 迁移期结束后删除旧 Cookie 读取逻辑；删除时间或版本应在实现计划中记录。

### 14.2 Bearer Token 数据模型

新增 token 存储应与 SQLite 现有 repository 模式一致，并为未来 PostgreSQL 迁移保持逻辑同构。建议字段：

- `token_id`
- `token_hash`
- `username`
- `client_name`
- `scopes`
- `expires_at`
- `revoked_at`
- `created_at`
- `last_used_at`

必须建立 `token_hash` 唯一索引，并避免保存原始 token。

### 14.3 回滚

- Cookie 名迁移回滚：保留短期读取旧 Cookie 能降低回滚风险；但回滚不得重新把用户名写入 Cookie。
- Bearer token 回滚：新增 token 表和 API 可以在功能开关关闭时停止发放，但已有 token 必须能被撤销或自然过期。
- CORS 配置回滚：移除或清空 allowed origins 后，跨站调用应 fail closed。

## 15. 错误处理

| 场景 | 响应 |
| --- | --- |
| 缺少 Cookie / Bearer token | `401 Authentication required` |
| session 不存在、过期、已撤销 | `401 Authentication required` |
| token 摘要不存在、过期、已撤销 | `401 Authentication required` |
| token scope 不足 | `403`；若暴露资源存在性有风险，则按现有 owner guard 返回 `404` |
| conversation / task 不属于当前用户 | 保持当前 `404` 隐藏资源存在性 |
| Origin 不在 allowlist | CORS 拒绝或 `403` |
| cross-site cookie 状态变更缺 CSRF 证据 | `403` |
| 认证服务未配置 | fail closed，不得静默降级为匿名访问 |

## 16. 验收标准

| 编号 | 验收项 |
| --- | --- |
| AC-1 | Cookie 值不包含用户名或任何可识别用户信息。 |
| AC-2 | 同站登录响应写入 `__Host-maf_session`，属性满足 `HttpOnly; Secure; SameSite=Lax; Path=/` 且无 `Domain`。 |
| AC-3 | `/api/v1/auth/me` 能通过新 Cookie 恢复用户；旧 Cookie 只在迁移期可读。 |
| AC-4 | logout 撤销 session 并清理新旧 Cookie。 |
| AC-5 | Bearer token 可调用受保护 API，且不需要 Cookie。 |
| AC-6 | Bearer token 不落明文库；过期、撤销、scope 不足均 fail closed。 |
| AC-7 | Bearer 与 Cookie 同时存在时优先 Bearer，并有跨用户冲突测试。 |
| AC-8 | allowlisted cross-site origin 可用 Bearer 调用 API；未登记 origin 被拒绝。 |
| AC-9 | 跨站 SSE 不通过 URL query 传 token，必须使用可设置 Authorization header 的流式客户端。 |
| AC-10 | API 文档说明 Cookie、Bearer、CORS、SSE 和 cross-site cookie 例外策略。 |
| AC-11 | 无新增依赖；若后续实现必须新增依赖，则同 PR 更新依赖快照并执行 License Requirement 检查。 |

## 17. 测试计划

实现必须先补测试再改代码，至少覆盖：

1. `tests/api/test_auth_login_and_isolation.py`
   - Cookie 不含用户信息。
   - 新 Cookie 名和属性。
   - logout 清理新旧 Cookie。
   - `/auth/me` 新旧迁移读取。

2. 新增 API token 测试文件，例如 `tests/api/test_auth_api_tokens.py`
   - 创建 token 只返回一次明文。
   - storage 不保存明文 token。
   - Bearer 可访问受保护 API。
   - expired / revoked / insufficient scope fail closed。
   - Bearer 优先级高于 Cookie。

3. CORS 测试
   - allowlisted origin 允许 Authorization header。
   - denied origin 被拒绝。
   - wildcard origin 不得与 credentials 组合。

4. SSE / 流式测试
   - 同站 Cookie SSE 保持可用。
   - Bearer stream 客户端可设置 Authorization header。
   - URL query 中出现 token 的路径被拒绝或文档禁止并由测试覆盖相关 helper。

5. 文档测试
   - `docs/api/api-doc.html` 包含 `__Host-maf_session`、Bearer token、跨站调用和 SSE 说明。

6. 回归测试
   - `conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'`
   - `conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'`
   - `cd frontend && npm test -- --run`
   - `cd frontend && npm run build`

## 18. 依赖与集成点

- FastAPI `Request` / `Response` Cookie API。
- Starlette / FastAPI CORS middleware（已有 FastAPI 依赖链，不应新增第三方包）。
- `src.auth.services.SessionService`。
- `src.api.auth.require_authenticated_user` 与 owner guard helpers。
- `src.storage.sqlite` repository / models。
- `docs/api/api-doc.html`。
- `frontend/src/api/client.ts` 的 same-origin Cookie client。
- 跨站 / 非浏览器客户端文档或后续 SDK。

## 19. 风险、假设与开放问题

| 类型 | 内容 | 处理 |
| --- | --- | --- |
| 假设 | 生产环境通过 HTTPS 暴露 API。 | Cookie 使用 `__Host-` 与 `Secure` 的必要前提；若不存在 HTTPS，不得进入生产。 |
| 假设 | 跨站前端数量较少且可登记 origin。 | 使用 allowlist 配置；不支持任意第三方 origin。 |
| 风险 | 第三方浏览器前端持有 Bearer token，XSS 会导致 token 泄露。 | 短 TTL、最小 scope、撤销、文档禁止默认长期 localStorage；高风险前端建议使用 BFF。 |
| 风险 | 原生 EventSource 不能设置 Authorization header，影响跨站 SSE。 | 第一阶段要求跨站前端使用 fetch stream；原生 EventSource 兼容另开设计。 |
| 风险 | 旧 `maf_session` 迁移期过长会扩大兼容面。 | 实现计划必须记录下线条件，并在文档标注临时兼容。 |
| 开放问题 | API token 的默认 TTL 与最大 TTL 具体数值。 | 实现计划中给出保守默认值；如需要业务侧确认，再单独确认。 |
| 开放问题 | 第三方前端是否需要用户自助创建 token，还是管理员预置 token。 | 第一阶段可以从“登录用户自助创建个人 token”开始；管理员模型另行设计。 |

## 20. 实施切分建议

1. Cookie hardening：新 Cookie 名、属性、旧 Cookie 迁移读取、logout 清理、API 文档更新。
2. 认证入口抽象：`require_authenticated_user()` 同时支持 Cookie 与 Bearer 来源，并定义优先级。
3. Bearer token storage / service：摘要存储、TTL、revoke、scope、last_used_at。
4. Bearer token API：创建、列表、撤销，保持非 GET 业务 ID 在 body 的现有 API 规则。
5. CORS 配置：allowlist、Authorization header、credentials 约束和测试。
6. SSE / 客户端文档：同站 Cookie SSE 与跨站 Bearer fetch stream 的分流说明。
7. cross-site cookie profile：仅在明确需要时另行实现，不阻塞前六步。
