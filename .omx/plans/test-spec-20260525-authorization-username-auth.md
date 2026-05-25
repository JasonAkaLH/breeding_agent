# Test Spec — Authorization-only username 登录态与 owner 字段统一

- **日期**：2026-05-25
- **关联计划**：`.omx/plans/prd-20260525-authorization-username-auth.md`
- **关联设计**：`docs/superpowers/specs/2026-05-25-authorization-username-auth-design.md`
- **状态**：待实施

## 1. 测试目标

证明一次性硬切换满足：

1. 登录只接收 username，并返回唯一当前 token。
2. 除登录外，所有受保护接口只通过 Authorization Bearer 定位 username。
3. 同一 username 新登录、refresh、logout 都会让旧 token 失效。
4. 业务 owner 字段从 account_id 统一为 username，历史数据迁移不丢失。
5. conversation/task/upload/artifact/长期记忆/SSE 隔离语义不变。
6. 前端使用 localStorage token，并对 REST、multipart、SSE 统一注入 Authorization。
7. API 文档不再暴露旧 Cookie/password/captcha/scoped token 管理口径。

## 2. Backend Auth API Tests

### 文件

- 新增 `tests/api/test_authorization_username_auth.py`
- 更新 `tests/api/test_auth_login_and_isolation.py`
- 更新或替换 `tests/api/test_auth_api_tokens.py`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| login creates username token | `POST /auth/login` body `{username: alice}` | 200，返回 `user.username=alice` 和 `access_token`，无 Set-Cookie。 |
| login existing user rotates token | alice 登录两次 | 第二个 token 可用，第一个 token 调 `/auth/me` 为 401。 |
| me requires bearer | 不带 Authorization 调 `/auth/me` | 401。 |
| me rejects malformed bearer | `Authorization: Bearer` / `Basic x` / 空 token | 401，不查出用户。 |
| logout clears token not user | alice 登录后 logout，再用旧 token 调业务 API | 401；再次 login 后历史数据仍可见。 |
| refresh rotates token | 旧 token 调 `/auth/refresh-token` | 返回新 token；旧 token 401；新 token 200。 |
| refresh rejects invalid token | 无效 token 调 refresh | 401。 |
| old captcha route down | 调 `/api/v1/auth/captcha` | 404 或 410。 |
| old register route down | 调 `/api/v1/auth/register` | 404 或 410。 |
| old api token routes down | create/list/delete `/api/v1/auth/api-tokens` | 404 或 410。 |
| no cookie auth fallback | 手动设置旧 `__Host-maf_session` / `maf_session` 但无 Bearer | 401。 |
| token plaintext not stored/logged | 检查 storage row/repr/audit payload | 不包含明文 access_token。 |
| last_used updated | 成功认证后查 token row | `token_last_used_at` 更新。 |

## 3. Storage and Migration Tests

### 文件

- 新增 `tests/storage/test_sqlite_username_auth_repository.py`
- 新增 `tests/storage/test_sqlite_username_migration.py`
- 更新 `tests/storage/test_sqlite_bootstrap.py`
- 更新 `tests/storage/test_sqlite_conversation_repository.py`
- 更新 `tests/storage/test_sqlite_conversation_memory_repository.py`
- 更新 `tests/storage/test_sqlite_pending_skill_context.py`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| save username token row | 保存 username-token hash | 可按 hash 查 username；hash unique。 |
| clear token keeps username | logout repository 操作 | row 仍存在，`api_token_hash is None`。 |
| rotate token atomic | 同 username 保存新 token | 旧 hash 查不到，新 hash 查到。 |
| conversation username column | 保存 Conversation(username=alice) | list by username 返回 alice 会话。 |
| memory summary username column | 保存 summary(username=alice) | latest summary 按 username 隔离。 |
| pending skill context username migration | 保存/查询 pending context | owner 字段为 username，跨用户不串。 |
| old account_id migration | 构造旧 schema/fixture 含 account_id | bootstrap/migration 后 username 值等于旧 account_id。 |
| migration preserves history | 旧 conversation/messages/tasks/artifacts/memory | 迁移后同 username 可全部读回。 |
| no external account_id in models | inspect dataclass/API model fields | 新 public model 不含 account_id。 |

## 4. Business Ownership and Isolation Tests

### 文件

- `tests/api/test_auth_login_and_isolation.py`
- `tests/api/test_task_cancel.py`
- `tests/api/test_task_events_sse.py`
- `tests/api/test_uploads.py`
- `tests/api/test_skill_output_artifacts.py`
- `tests/api/test_conversation_memory_runtime.py`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| submit uses token username | alice token submit body 不含 account_id | conversation.username=alice。 |
| malicious identity ignored | body 额外含 username/account_id=mallory | owner 仍为 token username 或请求被 schema 忽略/拒绝。 |
| list conversations scoped | alice/bob 各有会话 | 各自只看到自己会话。 |
| messages scoped | bob 访问 alice conversation messages | 404。 |
| task summary scoped | bob 访问 alice task | 404。 |
| task graph/artifacts scoped | bob 访问 alice task graph/artifacts | 404。 |
| cancel scoped | bob cancel alice task | 404。 |
| upload scoped | bob list/delete/use alice upload | 404。 |
| artifact download scoped | bob download alice artifact | 404。 |
| memory scoped | bob 请求 alice conversation 触发 memory build | 404 或 PermissionError，不构建 bob 可见 memory。 |
| login again preserves history | alice 登出/重新登录 | 仍能看到旧 conversation/messages/memory。 |

## 5. SSE Tests

### 文件

- `tests/api/test_task_events_sse.py`
- `frontend/src/api/taskEvents.test.ts`
- `frontend/src/domain/taskEvents.test.ts` 如 reducer 有 task filter

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| bearer sse works | alice token 订阅 alice task events | 200 text/event-stream，收到事件。 |
| no query token | URL query 带 token，无 Authorization | 401，不作为认证来源。 |
| cross-user sse denied | bob token 订阅 alice task | 404/401，无事件内容。 |
| token rotated during stream | 建连后 alice refresh/new login | 旧 stream 停止或下一次事件/heartbeat 失败。 |
| logout during stream | 建连后 logout | 旧 stream 停止或下一次事件/heartbeat 失败。 |
| frontend task filter | 收到非订阅 task_id 事件 | 前端丢弃，不更新当前 assistant 消息。 |

## 6. Frontend Tests

### 文件

- `frontend/src/api/client.test.ts`
- `frontend/src/api/taskEvents.test.ts`
- `frontend/src/App.test.tsx`
- `frontend/src/api/types.ts`

### 用例

| 用例 | 期望 |
| --- | --- |
| login sends username only | `/auth/login` body 只有 username。 |
| login stores token | App 登录成功后写入 `localStorage.maf_access_token` 或约定 key。 |
| me restores from token | 刷新时从 localStorage 取 token，`/auth/me` 带 Authorization。 |
| 401 clears token | 任意 API 返回 401 后清 localStorage 并回登录页。 |
| logout sends bearer and clears | logout 带 Authorization，成功后清 token、关闭 SSE。 |
| refresh replaces token | refresh 成功后 localStorage 使用新 token。 |
| JSON requests bearer | 所有 JSON API 带 Authorization。 |
| multipart upload bearer | 上传也带 Authorization。 |
| SSE fetch bearer | fetch stream 带 Authorization。 |
| submit no account_id | submitMessage body 不含 account_id。 |
| conversation type username | UI 使用 `conversation.username`，无 `account_id` 类型。 |
| no captcha/register UI | 登录页无验证码、密码、注册入口调用。 |

## 7. Documentation Tests

### 文件

- `tests/api/test_developer_docs.py`
- `docs/api/api-doc.html`
- `README.md`

### 断言

- 文档认证章节只描述 `Authorization: Bearer <api-token>`。
- login 请求只含 `username`。
- logout 和 refresh-token 文档存在。
- 不再把 Cookie/password/captcha/register/scoped api-token management 描述为正式认证方式。
- 参数明细不出现 `account_id`。
- Conversation response 字段为 `username`。
- SSE 文档说明 Bearer fetch stream 和 token currentness。
- README 说明内部弱认证边界与 localStorage 风险。

## 8. Static Sweep Tests

实施结束执行并人工确认 allowlist：

```bash
rg -n "account_id" src frontend/src docs/api tests
rg -n "__Host|maf_session|Cookie|cookie|captcha|register|api-tokens|required_scopes|AuthSource" src frontend/src docs/api tests
```

期望：

- `account_id` 只允许出现在旧迁移 fixture、历史 changelog、明确的 migration test 说明中。
- Cookie/captcha/register/api-tokens 不出现在运行时正式认证路径或 API 文档正式路径中。

## 9. Required Verification Commands

Targeted:

```bash
conda run -n multi_agent python -m unittest tests.api.test_authorization_username_auth
conda run -n multi_agent python -m unittest tests.storage.test_sqlite_username_auth_repository tests.storage.test_sqlite_username_migration
conda run -n multi_agent python -m unittest tests.api.test_auth_login_and_isolation tests.api.test_task_events_sse tests.api.test_developer_docs
cd frontend && npm test -- --run client.test.ts taskEvents.test.ts App.test.tsx
```

Regression:

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
git diff --check
```

## 10. License Requirement

本测试规格预期不新增依赖、不修改 Rust workspace 或 license policy。最终报告必须包含：

`License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。`

如果实现中触及 `native/`、`Cargo.lock`、`native/deny.toml` 或供应链策略，必须追加：

```bash
cd native && cargo deny check
```

## 11. Exit Criteria

- 所有 Required Verification Commands 通过，或明确记录不可运行原因与替代证据。
- 静态 sweep 无未解释的旧认证/`account_id` 残留。
- API docs、README、CHANGELOG 与运行时代码一致。
- 没有 Cookie fallback、password/captcha login、scoped token management 的运行时入口。
- 历史数据迁移测试证明旧 owner 数据可读。
