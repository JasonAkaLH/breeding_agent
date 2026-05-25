# PRD — Authorization-only username 登录态一次性切换实施计划

- **日期**：2026-05-25
- **设计来源**：`docs/superpowers/specs/2026-05-25-authorization-username-auth-design.md`
- **状态**：待实施
- **范围**：后端 Bearer-only 认证、username owner 字段统一、SQLite 迁移、API/SSE 资源隔离、前端 localStorage 登录态、API 文档与回归测试
- **取代计划**：本计划取代 `.omx/plans/prd-20260521-secure-session-cookie-auth.md` 的 Cookie + scoped token 双通道方向；本轮不保留 Cookie fallback 或 scoped API token 管理。

## 1. Requirements Summary

实施已审定的内部弱认证模型：

1. 登录接口只接收 `username` 并返回当前唯一 `access_token`；不再使用密码、验证码、注册或 Cookie。
2. 除登录接口外，所有受保护接口只通过 `Authorization: Bearer <api-token>` 定位当前 `username`。
3. 同一 `username` 同时最多一个有效 token；登录和 refresh 覆盖旧 token；logout 清空当前 token 但保留 username 与历史业务数据。
4. 代表用户归属的字段从 `account_id` 统一为 `username`，包括 core model、SQLite row、storage contract、API DTO、前端类型、文档和测试 fixture。
5. conversation/task/upload/artifact/长期记忆的业务行为保持不变：只替换身份来源和 owner 字段名，不重写业务算法。
6. SSE 建连前和连接期间都要保证 token 当前性与 task owner 一致，避免旧 token 或跨用户 task 泄漏事件。
7. 一次性硬切换：旧 Cookie session、captcha/register、scoped API token create/list/revoke 不作为正式 API 保留。

## 2. Baseline Evidence

| 证据 | 当前状态 | 实施影响 |
| --- | --- | --- |
| `src/api/auth.py:12-16` | 定义 Cookie 名与 `AuthSource = cookie|bearer`。 | 删除 Cookie/source 分支，收敛为 Bearer-only。 |
| `src/api/auth.py:36-53` | 同时支持 Cookie 解析与 Bearer token 解析。 | 保留 Authorization 解析，删除 Cookie 解析入口。 |
| `src/api/auth.py:56-84` | `require_authenticated_user()` 先尝试 Bearer，再 fallback Cookie，并支持 scope。 | 改为必须 Bearer；删除 `required_scopes` 和 `require_cookie_session` 语义。 |
| `src/api/auth.py:87-108` | owner guard 通过 `conversation.account_id != user.username` 隔离。 | 改为 `conversation.username != user.username`，隔离语义不变。 |
| `src/api/routes/auth.py:37-84` | captcha/login/register 使用密码验证码并写 Cookie。 | login 改为 username-only；captcha/register 下线。 |
| `src/api/routes/auth.py:93-143` | logout 清 Cookie；API token create/list/revoke 依赖 Cookie session。 | logout 清当前 token；旧 token 管理接口删除或 404/410。 |
| `src/api/dto.py:9-16` | `SubmitMessageRequest` 要求 `account_id`。 | 删除该字段，owner 来自 Authorization。 |
| `src/api/dto.py:146-149` | `ConversationSummaryResponse.account_id` 对外暴露。 | 改为 `username`。 |
| `src/core/models.py:28-43` | `Conversation` / `ConversationMemorySummary` 使用 `account_id`。 | 改为 `username` 并同步 repository mapping。 |
| `src/core/models.py:60-100` | 现有 `AuthUser` 含 password 字段，`AuthSession` / `AuthApiToken` 含旧认证语义。 | 简化为 username-token 映射模型；或兼容内部实现但运行时只暴露单 token 语义。 |
| `src/storage/sqlite/models.py:9-34` | SQLite `conversation` / `conversation_memory_summary` 列名为 `account_id`。 | 需要 schema/backfill migration。 |
| `src/storage/sqlite/models.py:72-131` | 认证表为 password user、session、多 token。 | 改造或新增 `auth_user_token` 单 token 表。 |
| `src/storage/sqlite/bootstrap.py:8-9` | 仅 `create_all`，没有旧 schema migration。 | 必须补显式迁移路径和旧库 fixture 测试。 |
| `src/core/contracts.py:68-108` | StoragePort 暴露 auth/session/token/account 方法。 | 改为 username-token 与 username owner 方法。 |
| `src/api/routes/conversations.py:46-71` | submit/list 通过 `user.username` 传给业务层，但 storage 方法仍是 account 语义。 | 删除 body account_id，重命名业务方法。 |
| `src/api/routes/tasks.py:117-127` | SSE 建连前 owner 校验，但流内不重验 token 当前性。 | 增加长连接期间 token 当前性检查策略。 |
| `src/api/routes/uploads.py:72-85` | multipart 保留 `conversation_id`，上传保存 `account_id=user.username`。 | multipart 位置不变，字段名改为 `username`。 |
| `frontend/src/api/client.ts:49-56` | 前端有 captcha/register/token 管理方法。 | 删除旧认证 client 方法，新增 refreshToken 和 localStorage token provider。 |
| `frontend/src/api/client.ts:104-107` | 已支持 Authorization header provider。 | 可复用为全局登录态注入。 |
| `frontend/src/api/client.ts:192-205` | submitMessage 发送 `account_id` fallback。 | 删除 `account_id`。 |
| `frontend/src/api/taskEvents.ts:83-92` | fetch stream 可带 Authorization。 | 改为默认 Bearer SSE，并加入 task_id 防御性过滤。 |
| `frontend/src/api/types.ts:45-48`、`:125-128` | 前端请求/响应类型暴露 account_id。 | 改为 username。 |
| `docs/superpowers/specs/2026-05-25-authorization-username-auth-design.md:52-63` | 核心决策已确认：无 TTL、localStorage、单 token、一次性切换。 | 实现不得引入未确认的 fallback 或 TTL。 |

## 3. Acceptance Criteria

| ID | Criterion | Validation |
| --- | --- | --- |
| AC-1 | `POST /api/v1/auth/login` 只接受 `username`，返回 `user.username` 和 `access_token`，不返回 Set-Cookie。 | API test + docs schema check。 |
| AC-2 | `GET /api/v1/auth/me`、logout、refresh 和所有业务 API 缺少/无效 Bearer 均 401。 | API auth tests。 |
| AC-3 | 同一 username 新登录或 refresh 后旧 token 立即失效。 | API token lifecycle tests。 |
| AC-4 | logout 只清空当前 token，保留 username 和历史 conversation/memory/upload。 | API + storage tests。 |
| AC-5 | 所有请求 DTO 和前端 submitMessage 不再包含 `account_id`。 | DTO/type tests + `rg account_id` allowlist review。 |
| AC-6 | API response 中 conversation owner 字段为 `username`，不再暴露 `account_id`。 | API route tests + docs tests。 |
| AC-7 | SQLite 旧 `account_id` 数据迁移到 `username` 后历史 conversation、memory summary、pending skill context 可读。 | Migration fixture tests。 |
| AC-8 | A 用户不能访问 B 用户 conversation/task/SSE/upload/artifact/memory。 | API isolation tests。 |
| AC-9 | SSE 建连后 token 被覆盖/登出时，旧连接停止或下一次事件/heartbeat 失败。 | SSE integration test。 |
| AC-10 | 前端登录页只输入 username，token 保存到 localStorage，REST/upload/SSE 均注入 Authorization。 | Vitest tests + build。 |
| AC-11 | API 文档不再描述 Cookie/password/captcha/scoped token 管理为正式认证方式。 | `tests/api/test_developer_docs.py`。 |
| AC-12 | 不新增依赖，不触发 license gate。 | Final report License Requirement。 |

## 4. Implementation Strategy

采用 TDD 分层硬切换，避免半迁移状态长期存在：

1. 先写后端 contract/storage/auth API 失败测试，锁定目标外部行为。
2. 完成 storage schema + repository + migration，使 username owner 和 token 映射可落库。
3. 改后端认证入口和业务 owner 字段，保持业务层只接收从 token 解析出的 username。
4. 改前端 localStorage 登录态和请求注入。
5. 改 API 文档和历史测试 fixture。
6. 全量验证后再清理旧 Cookie/session/scope 路径残留。

### 4.1 Split Decision — 父计划 + 可验证检查点

这份 PRD 与关联 Test Spec 保持为唯一父计划，不再把产品决策拆成多份互相独立的 PRD。实际实施必须拆成下列可验证检查点推进；每个检查点有独立退出标准，下游写代码不得越过未通过的上游门禁。

| Checkpoint | 覆盖步骤 | 目标 | 退出门禁 | 可并行性 |
| --- | --- | --- | --- | --- |
| CP-0 Contract red tests | Step 1 | 先把目标认证/API/迁移行为写成失败测试。 | 新增/更新测试能稳定指向旧行为失败；未改生产代码。 | 不并行；所有后续实现依赖它。 |
| CP-1 Storage + token foundation | Step 2-3 | 落地 username owner、旧 account_id backfill、单 token hash 映射与 auth service。 | storage/auth service targeted tests 通过；旧库 fixture 可迁移；明文 token 不落库。 | 可由一个后端 lane 独立完成。 |
| CP-2 API contract hard switch | Step 4 | FastAPI 改为 Bearer-only，删除 Cookie/scope 入口，DTO/response 改 username。 | auth API/isolation/docs schema targeted tests 通过；public endpoints 未误伤。 | 依赖 CP-1 contract；可与 CP-3 只读准备并行，不能并行改同一 route。 |
| CP-3 Business owner + SSE currentness | Step 5-6 | runtime、memory、upload、task/SSE 继续按 owner 隔离，只改身份来源和字段名。 | memory/upload/task/SSE targeted tests 通过；旧 token stream 不再收业务事件。 | CP-2 DTO 稳定后可与 CP-4 并行。 |
| CP-4 Frontend login/token flow | Step 7 | 前端 username-only login、localStorage token、REST/upload/SSE Authorization 注入。 | frontend targeted tests 与 build 通过；401 清 token 并关闭 SSE。 | CP-2 API contract 稳定后可与 CP-3 并行。 |
| CP-5 Docs + sweep + full regression | Step 8-10 | 文档、README、静态残留、全量回归与最终证据。 | Required Verification Commands、`git diff --check` 与 `rg` allowlist 通过。 | 必须最后执行。 |

实施口径：如果使用 `$team` 或 `$ultragoal`，可以把 CP-1、CP-3、CP-4 分配给不同 owner；但 CP-0、CP-2 的接口契约和 CP-5 的最终验收必须由一个主 owner 收口，防止不同 lane 重新解释 Authorization-only / username-only 决策。

## 5. Implementation Steps

### Step 1 — 后端认证契约测试先行

**目标**：让旧 Cookie/password/captcha/scoped token 行为的测试红掉，同时新增目标认证行为测试。

**文件**：
- `tests/api/test_auth_login_and_isolation.py`
- `tests/api/test_auth_api_tokens.py`（可重命名或改为 token-only lifecycle 测试）
- 新增 `tests/api/test_authorization_username_auth.py`
- `tests/api/test_developer_docs.py`

**测试点**：
- login body 只需要 `username`。
- login 不设置 Cookie。
- `/auth/me` 必须 Bearer。
- logout 置空 token 后旧 token 401。
- refresh 替换 token 后旧 token 401。
- `/auth/captcha`、`/auth/register`、`/auth/api-tokens` 系列删除或 404/410。
- 缺 Authorization、非 Bearer、malformed token 均 401。

**完成标准**：目标测试能准确失败，且没有开始改生产代码。

### Step 2 — Storage model 与 SQLite migration

**目标**：建立 `username` owner 与单 token 映射的持久层。

**文件**：
- `src/core/models.py`
- `src/core/contracts.py`
- `src/storage/sqlite/models.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/sqlite/bootstrap.py`
- `tests/storage/test_sqlite_bootstrap.py`
- 新增 `tests/storage/test_sqlite_username_auth_repository.py`
- 新增 `tests/storage/test_sqlite_username_migration.py`

**实现要点**：
- `Conversation.account_id` -> `Conversation.username`。
- `ConversationMemorySummary.account_id` -> `ConversationMemorySummary.username`。
- `PendingSkillContextRow.account_id` -> `username`。
- `UploadedFileRecord.account_id` 后续在 API 层同步改为 `username`。
- 新增或改造 `auth_user_token` 语义：`username` primary key、`api_token_hash` unique nullable、issued/last_used/created/updated。
- bootstrap 必须支持旧库迁移：检测旧 `account_id` 列，将值 backfill 到 `username` 列；索引改为 username。
- 如果 SQLite 不能安全 drop old columns，运行态可以保留物理旧列一段时间，但 Python model/API contract 必须只暴露 `username`；计划内要记录该技术债并有测试防止外部泄漏。

**完成标准**：storage tests 通过，旧库 fixture 能迁移并读出 username-owned conversation/memory/pending context。

### Step 3 — Auth service 与 runtime façade

**目标**：用单 token 映射替换 password/session/scoped token runtime 入口。

**文件**：
- `src/auth/services.py`
- `src/api/runtime.py`
- `src/core/contracts.py`
- `tests/api/test_authorization_username_auth.py`

**实现要点**：
- 保留/复用 `validate_username()`，但不要求 password/captcha。
- 新服务提供：`login_username(username) -> access_token`、`get_user_for_bearer(raw_token)`、`logout_bearer(raw_token)`、`refresh_bearer(raw_token)`。
- token hash 使用现有 `hmac_sha256_hex` / secret 机制；不得落明文。
- `token_last_used_at` 在成功认证时更新。
- 并发登录/refresh 以最后写入 token 为唯一有效 token。
- 删除 runtime 对 `SessionService`、captcha、scoped `ApiTokenService` 的可见认证依赖；如保留类用于迁移，不得被 API runtime 调用。

**完成标准**：service/API tests 证明 login/logout/refresh/me 与旧 token 失效语义正确。

### Step 4 — FastAPI 认证入口与路由改造

**目标**：所有受保护 API 走 Bearer-only username resolver，owner guard 改为 username。

**文件**：
- `src/api/auth.py`
- `src/api/routes/auth.py`
- `src/api/routes/conversations.py`
- `src/api/routes/tasks.py`
- `src/api/routes/uploads.py`
- `src/api/routes/capabilities.py`（确认 public 不误伤）
- `src/api/dto.py`

**实现要点**：
- `AuthenticatedUser` 保留 `username`，删除 `auth_source/scopes` 或不再对外使用。
- `require_authenticated_user(request)` 必须要求 Bearer；删除 `required_scopes` 参数在各 route 的使用。
- owner helper 改为 `conversation.username`。
- auth routes：login/me/logout/refresh-token 为正式接口；旧 captcha/register/api-tokens 删除或稳定 404/410。
- `SubmitMessageRequest` 删除 `account_id`。
- `ConversationSummaryResponse` 暴露 `username`。
- `UploadFileResponse` 可不新增 username；upload 内部 owner 改为 username。
- `GET /api/v1/capabilities` 和 `/api-doc` 继续 public。

**完成标准**：API auth/isolation tests 通过；`rg "account_id" src/api src/core src/storage frontend/src docs/api` 仅剩迁移兼容/历史注释 allowlist。

### Step 5 — Runtime、orchestration、memory 与 upload owner 改名

**目标**：业务层继续按 owner 隔离，但统一变量名。

**文件**：
- `src/api/runtime.py`
- `src/api/upload_store.py`
- `src/orchestration/conversation_memory.py`
- 相关 tests under `tests/api`, `tests/orchestration`, `tests/capabilities`

**实现要点**：
- `submit_message(..., authenticated_account_id=...)` 改为 `authenticated_username`。
- conversation 创建/更新使用 `username`。
- `resolve_uploads_for_message`、`save_upload`、`list_uploads`、`delete_upload` 参数改为 username。
- conversation memory `build(..., account_id=...)` 改为 `username`，算法和 prompt payload 不变。
- 所有旧 `account_id` 参数不作为身份来源。

**完成标准**：conversation memory、upload、main_agent、pending skill context 回归通过。

### Step 6 — SSE token 当前性与事件隔离

**目标**：防止旧 token 长连接继续接收事件。

**文件**：
- `src/api/routes/tasks.py`
- `src/api/runtime.py`（如需 token current checker）
- `frontend/src/api/taskEvents.ts`
- `frontend/src/domain/taskEvents.ts`（如事件 reducer 存在）
- `tests/api/test_task_events_sse.py`
- `frontend/src/api/taskEvents.test.ts`

**实现要点**：
- 建连前继续 `task -> conversation -> username` owner 校验。
- `_event_stream()` 每次 yield 前或定期 heartbeat 前重新验证当前 Authorization token 仍映射到同一 username。
- token 被 refresh/logout/覆盖后，旧 stream 应停止；可用 401 前置错误、关闭连接，或发送安全的 auth-expired terminal event，但不得继续推业务事件。
- 前端解析 SSE 后校验 `event.task_id` 与当前订阅 taskId 一致，不一致丢弃。

**完成标准**：SSE tests 覆盖跨用户订阅拒绝和 token 覆盖后旧连接停止。

### Step 7 — Frontend localStorage 登录态

**目标**：前端从 Cookie/captcha/password flow 切为 username-only + localStorage Bearer flow。

**文件**：
- `frontend/src/api/client.ts`
- `frontend/src/api/types.ts`
- `frontend/src/App.tsx`
- `frontend/src/App.test.tsx`
- `frontend/src/api/client.test.ts`
- `frontend/src/api/taskEvents.test.ts`

**实现要点**：
- `AuthUserResponse` 或新增响应类型包含 `access_token`。
- `login({ username })` body 只发 username。
- 删除 `createCaptcha` / `register` / token management client methods，或不再从 UI 调用。
- localStorage key 使用稳定命名，例如 `maf_access_token`；可另存 `maf_username` 用于展示，但认证以 token 为准。
- createApiClient 通过 `authHeaderProvider` 从 localStorage 取 token。
- JSON、multipart upload、fetch stream SSE 都带 Authorization。
- 401 统一清理 localStorage、关闭 SSE、回登录页。
- `submitMessage` 不再发送 account_id。
- `ConversationSummaryResponse.username` 替换前端 `account_id`。

**完成标准**：Vitest 与 build 通过；浏览器刷新恢复由 `/auth/me` 验证 token。

### Step 8 — API 文档、README、CHANGELOG 同步

**目标**：交付文档不再保留旧认证口径。

**文件**：
- `docs/api/api-doc.html`
- `README.md`
- `CHANGELOG.md`
- 如有相关 PRD 索引或 API docs tests 同步更新

**实现要点**：
- 删除 Cookie、password、captcha、register、scoped token 管理作为正式接口说明。
- 参数表删除 `account_id`。
- 认证章节说明 Authorization-only、username-only login、logout/refresh。
- SSE 章节说明 Bearer fetch stream 和 token currentness。
- README 当前认证部署说明改为内部弱认证边界，避免继续要求 Cookie/HTTPS 作为正式登录入口。

**完成标准**：developer docs tests 通过；文档搜索无旧正式认证口径残留。

### Step 9 — Cleanup 与 compatibility sweep

**目标**：删除死代码与测试残留，避免隐藏 fallback。

**文件/命令**：
- `rg "cookie|Cookie|__Host|maf_session|captcha|register|api-tokens|account_id|required_scopes|AuthSource" src frontend docs tests`

**实现要点**：
- 保留 only allowlisted 历史迁移测试和说明。
- 删除或隔离旧 test helpers 中 password login 逻辑。
- 确保 `config.yaml`/env 中旧 token TTL/scope 口径不再影响 runtime。
- 确保 CORS 仍允许 Authorization header，且不引入 credentialed wildcard。

**完成标准**：sweep 输出经人工/测试确认，无运行时旧认证路径。

### Step 10 — Full verification and release evidence

**目标**：验证计划完成，记录证据。

**命令**：见测试规格的 Required Verification Commands。

**完成标准**：后端分层测试、前端测试/build、docs tests、diff check 均通过；最终报告包含 License Requirement。

## 6. Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| 旧 `account_id` 数据迁移遗漏 | 历史会话/记忆不可见 | 旧 schema fixture migration test；迁移后查询 username-owned resources。 |
| 物理 DB 列无法一次性安全 drop | 代码/API 已改但 DB 残留旧列 | 允许物理旧列短期存在但运行时/API 不暴露；记录 migration debt；测试外部无 `account_id`。 |
| 弱认证被误用于公网 | 任意知道 username 可登录 | README/API docs 明确内部系统边界；未来强认证另开 PRD。 |
| localStorage token 被 XSS 读取 | token 泄露 | 内部风险接受；继续不渲染不可信 HTML；token 可被新登录/登出覆盖失效。 |
| SSE 建连后旧 token 继续收事件 | 跨设备旧连接泄漏结果 | 流内 token currentness check + 前端 task_id 过滤。 |
| 删除 scoped token 打破外部脚本 | 旧集成失败 | 一次性硬切换为已确认边界；API docs 清楚标注新 login/refresh。 |
| 大范围重命名引发回归 | 编译/测试失败 | 分层 TDD；先 storage/model，再 API/runtime，再 frontend/docs；每层跑 targeted tests。 |
| Rust contracts 或 sidecar schema 文档含 account_id | 误改非本轮范围 | 只改 Python/API/frontend runtime 相关 owner；Rust runtime contracts 若只是历史 contract，不在无 PRD 情况下扩范围。 |

## 7. Verification Steps

Targeted first:

```bash
conda run -n multi_agent python -m unittest tests.api.test_authorization_username_auth
conda run -n multi_agent python -m unittest tests.storage.test_sqlite_username_auth_repository tests.storage.test_sqlite_username_migration
conda run -n multi_agent python -m unittest tests.api.test_auth_login_and_isolation tests.api.test_task_events_sse tests.api.test_developer_docs
cd frontend && npm test -- --run client.test.ts taskEvents.test.ts App.test.tsx
```

Layered regression:

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

License:

- Expected: no dependency/license change.
- If `native/`, `Cargo.lock`, or `native/deny.toml` changes unexpectedly, run `cd native && cargo deny check`.

## 8. Follow-up Staffing Guidance

### Recommended default: `$ultragoal`

Use `$ultragoal` as durable owner because this is a broad, multi-file migration with explicit acceptance criteria and many verification gates.

Suggested goal lanes:

1. Backend auth/storage migration lane — high reasoning.
2. API route/runtime owner rename lane — high reasoning.
3. Frontend login/localStorage/SSE lane — medium reasoning.
4. Docs/tests verification lane — medium/high reasoning.

### Parallel option: Team + Ultragoal

This work is parallelizable after Step 1/2 contracts are clear.

Suggested `$team` lanes:

- `test-engineer`: write failing backend/storage/frontend tests and migration fixtures.
- `executor`: storage/auth service/runtime implementation.
- `executor`: API routes/DTO/docs update.
- `executor`: frontend client/App/SSE update.
- `verifier`: run regression gates and inspect `rg` sweeps.

Launch hint:

```bash
$team --plan .omx/plans/prd-20260525-authorization-username-auth.md
```

Team verification path:

- Team proves targeted tests pass per lane.
- Team returns `rg` sweep evidence for old Cookie/account_id/scoped-token leftovers.
- Ultragoal checkpoints full regression evidence and remaining risks.

### Ralph fallback

Use `$ralph` only if the user explicitly wants a persistent single-owner sequential implementation/verification loop instead of parallel team execution.

## 9. ADR

### Decision

Implement a one-shot Authorization-only internal username login model with one active token per username and owner fields unified to `username`.

### Drivers

1. Remove ambiguity between Cookie/session/scoped token identity sources.
2. Ensure every non-login request identifies the user only through Authorization.
3. Make long-term memory and resource ownership consistently keyed by username.
4. Preserve business behavior while eliminating old auth complexity.

### Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Keep Cookie + Bearer dual mode | Conflicts with explicit requirement that Authorization be the only auth carrier. |
| Keep `account_id` as business owner name | User ultimately chose unified `username`; retaining account_id would keep conceptual ambiguity. |
| Multi-token scoped API token model | User requested one current token per username, no account management or token management UI. |
| Add password/SSO/shared secret | Out of scope for internal weak-auth model confirmed by user. |

### Why chosen

The chosen model is the simplest contract matching the confirmed product direction: username identifies internal users; Authorization token proves current login; resource ownership remains server-side and testable.

### Consequences

- Existing external clients using Cookie/password/scoped API tokens break and must switch to username login + Bearer token.
- Database migration is mandatory for owner field naming.
- Frontend must own token persistence and 401 handling.
- Security posture is intentionally internal-only.

### Follow-ups

- If this system becomes public-facing, open a new PRD for strong authentication/SSO.
- If multiple devices per user become required, open a new PRD for multi-token sessions.
- If token TTL becomes required, open a new PRD for expiry/refresh-token families.

## 10. Plan Review Checklist

- Testable acceptance criteria: yes, AC-1 through AC-12.
- File references: yes, baseline evidence maps to current files/lines.
- Risks mitigated: yes.
- Vague terms: bounded or tied to tests.
- Execution split: yes, CP-0 through CP-5 define checkpoint gates and parallelization boundaries.
- Saved under `.omx/plans/`: yes.
