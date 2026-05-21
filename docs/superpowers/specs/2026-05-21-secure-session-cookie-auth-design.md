# 安全 Session Cookie 与多客户端认证设计

日期：2026-05-21  
状态：已按用户确认的推荐方案落档，等待实现计划  
范围：浏览器同站同域前端、跨站/第三方浏览器前端、非浏览器 REST API 客户端的认证边界与 Cookie 设计

## 1. 目标

本设计的目标是让认证机制满足长期交付质量：

1. Cookie 中只保存不透明随机 `session_id`，不得包含用户名、账号、角色、邮箱、租户等可识别用户信息。
2. 同站同域浏览器前端继续使用安全的服务端 Session Cookie，保持浏览器体验与后端集中撤销能力。
3. 跨站/第三方浏览器前端与非浏览器客户端不依赖默认 Session Cookie，优先使用 `Authorization: Bearer <opaque-token>`。
4. 只有在确实需要跨站浏览器 Cookie 时，才启用单独的 allowlisted cross-site cookie profile，并配套 Origin / CORS / CSRF 防护。
5. 不把 JWT 中携带用户信息作为默认方案；默认 token 都是服务端可撤销的不透明随机值。

## 2. 当前仓库事实

当前后端认证实现位于：

- `src/api/auth.py`
- `src/api/routes/auth.py`
- `src/auth/services.py`
- `src/core/models.py`
- `src/storage/sqlite/repositories.py`

当前实现已经具备以下基础：

- Cookie 名称为 `maf_session`。
- Cookie 值只写入服务端生成的 `session_id`。
- `SessionService.create_session()` 使用 `secrets.token_urlsafe(32)` 生成随机不透明 session ID，形如 `sess-<random>`。
- 服务端通过 `runtime.get_session_user(session_id)` 查 storage 中的 `AuthSession`，再恢复 `username`。
- Cookie 已设置 `HttpOnly`、`SameSite=Lax`、`Path=/`，并在 HTTPS 或 `MAF_AUTH_COOKIE_SECURE` 开启时设置 `Secure`。
- 登录 / 注册响应 body 中会返回用户信息，但 Cookie 自身不含用户信息。

因此，实现计划应优先强化现有边界，而不是重写认证系统。

## 3. 外部安全依据

设计参考以下公开安全建议：

- OWASP Session Management Cheat Sheet：Session ID 内容应无意义，避免可解码出用户或系统信息；推荐 Cookie 使用 `Secure`、`HttpOnly`、`SameSite` 等属性。  
  https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- MDN Set-Cookie / SameSite 文档：`SameSite=None` 必须配合 `Secure`；`HttpOnly` 可阻止 JavaScript 通过 `document.cookie` 读取 Cookie；`Secure` 限制 Cookie 只在 HTTPS 等安全上下文发送。  
  https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie/SameSite

## 4. 推荐架构

采用“双通道、默认安全”的认证模型：

### 4.1 同站同域浏览器前端：Session Cookie

同站同域前端使用服务端 Session Cookie：

```http
Set-Cookie: __Host-maf_session=<opaque-session-id>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=28800
```

约束：

- Cookie 名升级为 `__Host-maf_session`。
- Cookie 值仍然只保存随机不透明 session ID。
- 必须设置 `Secure`、`HttpOnly`、`SameSite=Lax`、`Path=/`。
- 不设置 `Domain`，让 Cookie 绑定当前 host，降低子域注入和跨子域泄漏风险。
- 保留服务端 Session 存储与撤销能力。
- 登录成功后可以返回用户信息到响应 body，供前端展示；但 Cookie 中不得包含该信息。

### 4.2 跨站/第三方浏览器前端：Authorization Bearer

跨站或第三方浏览器前端优先使用 REST API token：

```http
Authorization: Bearer <opaque-access-token>
```

约束：

- Bearer token 是不透明随机值，不是默认携带用户信息的 JWT。
- 服务端存储 token 摘要或 token id 到用户 / client / scope / 过期时间 / 撤销状态的映射。
- CORS 只允许登记过的前端 origin。
- 不使用 `Access-Control-Allow-Origin: *` 搭配凭证。
- 对第三方前端按 client 维度记录：名称、allowed origins、允许的 API scope、token TTL、撤销状态。

### 4.3 非浏览器客户端：Authorization Bearer

CLI、脚本、后端服务、移动端等非浏览器客户端也使用：

```http
Authorization: Bearer <opaque-access-token>
```

约束：

- 不依赖 Cookie jar。
- token 可以通过登录换取、后台创建、或未来的 OAuth / PAT 流程发放。
- 服务端鉴权入口统一支持从 Authorization 头解析 Bearer token，并复用用户恢复与权限检查逻辑。

### 4.4 例外场景：跨站 Cookie Profile

如果某个跨站浏览器前端必须使用 Cookie，而不是 Bearer token，则只允许按显式 allowlist 开启单独 profile：

```http
Set-Cookie: maf_cross_site_session=<opaque-session-id>; HttpOnly; Secure; SameSite=None; Path=/; Max-Age=<short-ttl>
```

约束：

- 只对被 allowlist 的 origin 生效。
- 必须 `SameSite=None; Secure; HttpOnly`。
- 必须启用严格 Origin 校验。
- 必须配置 CORS credentials allowlist，禁止 wildcard origin。
- 必须有 CSRF 防护策略，例如 Origin 校验 + 双提交 CSRF token，或仅允许带显式 CSRF header 的状态变更请求。
- 该 profile 不是默认路径，只作为兼容例外。

## 5. 数据流

### 5.1 同站 Cookie 登录流

1. 浏览器调用 `/api/v1/auth/login` 或 `/api/v1/auth/register`。
2. 后端校验用户名、密码、验证码。
3. 后端创建服务端 `AuthSession`。
4. 后端写入 `__Host-maf_session=<session_id>`。
5. 后续同站请求自动携带 Cookie。
6. 后端只从 Cookie 取 `session_id`，再从 storage 查用户。
7. 退出登录时 revoke session 并清除 Cookie。

### 5.2 Bearer Token API 调用流

1. 客户端通过受控入口获取不透明 access token。
2. 客户端调用 REST API 时发送 `Authorization: Bearer <token>`。
3. 后端对 token 做格式校验、摘要查表、过期 / 撤销 / scope 校验。
4. 后端恢复用户身份，并复用当前 conversation/task ownership guard。
5. token 过期或撤销时返回 `401`，权限不足返回 `403` 或现有 API 约定错误。

## 6. 错误处理与安全策略

- 缺少 Cookie / Bearer token：返回 `401 Authentication required`。
- Cookie session 不存在、过期、已撤销：返回 `401`。
- Bearer token 不存在、过期、已撤销、摘要不匹配：返回 `401`。
- Bearer token scope 不允许访问目标 API：返回 `403`，或若当前 API 以资源隐藏为安全策略，则返回 `404`。
- 跨用户访问 conversation/task：保持当前 owner guard 行为，返回 `404` 隐藏资源存在性。
- Origin 不在 allowlist：拒绝 CORS 或返回 `403`。
- 跨站 Cookie profile 缺 CSRF 证据的状态变更请求：返回 `403`。
- 日志不得记录原始 Cookie、原始 Bearer token、密码、验证码或其他 secret；只记录脱敏 fingerprint。

## 7. 测试计划

实现时应先补回归测试，再改代码：

1. Cookie 内容测试
   - 登录后 `Set-Cookie` 只包含不透明 session ID。
   - Cookie 值不包含用户名、`username`、邮箱、角色等信息。
   - Cookie 属性包含 `HttpOnly`、`Secure`、`SameSite=Lax`、`Path=/`。
   - 默认 Cookie 不设置 `Domain`。

2. Cookie 名称迁移测试
   - 新 Cookie 名为 `__Host-maf_session`。
   - 旧 `maf_session` 如需兼容，只允许短期读取，不再写入。
   - logout 同时清理新旧 Cookie，避免灰度期间残留。

3. 身份恢复测试
   - `GET /api/v1/auth/me` 能通过新 Cookie 恢复用户。
   - 无 Cookie / 无效 Cookie 返回 `401`。
   - Cookie 中不包含用户信息时仍能从服务端 session 恢复用户。

4. Bearer token 测试
   - Authorization Bearer 可访问受保护 API。
   - 无效 / 过期 / revoked token 返回 `401`。
   - scope 不足返回权限错误。
   - Bearer token 与 Cookie 同时存在时，优先级必须明确并有测试覆盖。建议优先使用 Bearer token，因为显式 header 比浏览器自动 Cookie 更适合 API 客户端。

5. CORS / 跨站测试
   - allowlisted origin 可通过 Bearer token 调用 API。
   - 非 allowlisted origin 被拒绝。
   - 默认同站 Cookie 不为跨站请求放宽到 `SameSite=None`。

6. 文档测试
   - `docs/api/api-doc.html` 说明同站 Cookie、Bearer token、跨站 Cookie 例外策略。
   - API client / smoke 脚本同步新的认证入口。

## 8. 非目标

本设计不在第一阶段引入以下内容：

- 默认 JWT 认证。
- 把用户信息、角色、租户信息编码进 Cookie 或默认 token。
- 全站统一 `SameSite=None` Cookie。
- 未登记 origin 的第三方浏览器访问。
- OAuth 完整授权服务器；未来如果需要可作为独立 PRD 设计。

## 9. 实施切分建议

第一阶段建议按以下顺序实施：

1. 先强化 Cookie：`__Host-maf_session`、Secure 默认策略、logout 清理、测试与文档。
2. 再抽象认证入口：统一 `require_authenticated_user` 支持 Cookie 与 Bearer 来源，但保持 owner guard 不变。
3. 增加不透明 Bearer token 的 storage model、service、API 发放 / revoke / scope 校验。
4. 增加 CORS allowlist 配置与文档。
5. 如确有必要，再设计 cross-site cookie profile；不与第一阶段强绑定。

## 10. 验收标准

- Cookie 中没有用户名或任何可识别用户信息。
- 同站前端登录、刷新、调用现有 API、退出登录均正常。
- 跨站/第三方 REST 客户端可用 Bearer token 调用 allowlisted API。
- 非浏览器客户端不需要 Cookie 即可认证。
- 未授权、过期、撤销、跨用户访问、非 allowlisted origin 均 fail closed。
- 自动化测试覆盖 Cookie 属性、身份恢复、Bearer token、CORS / origin 策略和 API 文档。
- 无新增第三方依赖，除非后续实现计划明确说明并经过 License Requirement 检查。
