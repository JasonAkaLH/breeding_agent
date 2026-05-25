# Test Spec — 旧认证上下文残留硬删除

- **日期**：2026-05-25
- **关联计划**：`.omx/plans/prd-20260525-legacy-auth-context-removal.md`
- **关联设计**：`docs/superpowers/specs/2026-05-25-legacy-auth-context-removal-design.md`
- **状态**：待实施

## 1. 测试目标

证明 legacy auth cleanup 满足：

1. 旧 `AuthUser` / `CaptchaChallenge` / `AuthSession` / `AuthApiToken` Python model、StoragePort、SQLite row/repository、Rust contract snapshot 全部移除。
2. 旧 SQLite auth 表启动时被幂等 drop，且不会被 `create_all` 重建。
3. 当前 `AuthUserToken` / `auth_user_token` / `UsernameTokenService` 行为不变。
4. conversation/task/upload/artifact/memory/pending skill context 等业务 owner 行为不变。
5. 旧 Cookie/password/captcha/register/scoped-token docs/plans 从 active context 删除。
6. 静态扫描不误伤当前 Authorization-only 标识。
7. Rust/native 变更满足 license gate。

## 2. Checkpoint Gate Matrix

| Checkpoint | 必跑 targeted tests / checks | 进入下一阶段条件 |
| --- | --- | --- |
| CP-0 Red tests | 新增/更新 legacy cleanup storage/bootstrap/core tests；运行相关 targeted tests，预期旧实现失败。 | 失败原因对应旧 auth surface/drop 表缺失；生产代码未改。 |
| CP-1 Python core/storage deletion | `conda run -n multi_agent python -m unittest tests.core.test_rust_contract_artifact tests.storage.test_sqlite_username_auth_repository` + affected storage tests。 | 旧 Python imports/methods 消失；当前 token repository 正常。 |
| CP-2 Bootstrap cleanup | `conda run -n multi_agent python -m unittest tests.storage.test_sqlite_bootstrap tests.storage.test_sqlite_conversation_delete tests.storage.test_sqlite_username_migration`。 | 旧 auth 表 drop、`auth_user_token` 存活、owner migration 和 conversation deletion 通过。 |
| CP-3 Rust contract | `conda run -n multi_agent python -m unittest tests.core.test_rust_contract_artifact`；`cd native && cargo test -p maf_core_types`。 | JSON/native snapshot 无旧 auth model，保留 `AuthUserToken`。 |
| CP-4 Historical docs/docs tests | `test ! -e <deleted-file>` checks；`conda run -n multi_agent python -m unittest tests.api.test_developer_docs`。 | 精确旧文档清单不存在；当前 API docs 仍为 Authorization-only。 |
| CP-5 Regression/license/sweep | Required Verification Commands + static sweep + `cargo deny check` + `git diff --check`。 | 无未解释 legacy residue，所有输出已读。 |

Gate rule：不得通过恢复 Cookie/password/captcha/scoped-token fallback 让测试变绿。

## 3. Core and Contract Tests

### 文件

- `tests/core/test_rust_contract_artifact.py`
- `src/core/models.py`
- `src/core/contracts.py`
- `src/core/rust_contracts/core_contract.json`
- `native/crates/maf_core_types/src/lib.rs`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| legacy models absent from Python | `from src.core import models` 后检查属性 | `AuthUser`、`CaptchaChallenge`、`AuthSession`、`AuthApiToken` 不存在。 |
| current token model present | 检查 `AuthUserToken` dataclass fields | fields 为 `username/api_token_hash/token_issued_at/token_last_used_at/created_at/updated_at`。 |
| StoragePort legacy methods absent | inspect `StoragePort` methods | 旧 save/get/list/touch/revoke auth API 不存在；current token methods 存在。 |
| contract legacy models absent | `load_core_contract()` | `models` 不含旧 auth names，含 `AuthUserToken`。 |
| rust snapshot legacy models absent | `cargo test -p maf_core_types` | Rust snapshot 与 JSON 一致。 |
| schema hash updated | 读取 JSON/native hash | hash 与旧 `maf_core_types_core_v1_schema_20260525_username_token` 不同，并表达 legacy auth removed。 |

## 4. Storage and Bootstrap Tests

### 文件

- `tests/storage/test_sqlite_bootstrap.py` 或新增 `tests/storage/test_sqlite_legacy_auth_cleanup.py`
- `tests/storage/test_sqlite_username_auth_repository.py`
- `tests/storage/test_sqlite_username_migration.py`
- `tests/storage/test_sqlite_conversation_delete.py`

### 用例

| 用例 | 步骤 | 期望 |
| --- | --- | --- |
| drops all legacy auth tables | 手工创建四张旧表和数据后调用 `bootstrap_sqlite_database(engine)` | `inspect(engine).get_table_names()` 不含四张旧表。 |
| missing old tables idempotent | 新空库调用 bootstrap | 成功，`auth_user_token` 存在。 |
| create_all does not recreate old tables | bootstrap 后再 inspect metadata/table names | 旧表仍不存在。 |
| current token table works after cleanup | bootstrap 后用 repository 保存/查询/clear/rotate token | current token lifecycle 正常。 |
| owner migration still works | 构造旧 `account_id` schema fixture | bootstrap 后 username owner 数据可读。 |
| conversation delete keeps current auth row | seed `AuthUserTokenRow` + business rows 后 delete conversation | business rows 为 0；`AuthUserTokenRow` 仍为 1；无旧 auth row imports。 |
| old storage tests removed | 检查旧 test modules | `tests/storage/test_sqlite_auth_repository.py` 和 `tests/storage/test_sqlite_api_token_repository.py` 不再包含旧正向 auth storage 测试；若文件删除，unittest discover 正常。 |

## 5. API Behavior Preservation Tests

### 文件

- `tests/api/test_authorization_username_auth.py`
- `tests/api/test_auth_cookie_security.py`
- `tests/api/test_auth_api_tokens.py`
- `tests/api/test_auth_login_and_isolation.py`
- `tests/api/test_task_events_sse.py`
- `tests/api/test_uploads.py`
- `tests/api/test_developer_docs.py`

### 用例

| 用例 | 期望 |
| --- | --- |
| login username-only | `{username}` 返回 `user.username` + `access_token`，无 Set-Cookie。 |
| me requires bearer | 无 Authorization / malformed Authorization / cookie-only 均 401。 |
| token rotation unchanged | 新 login 或 refresh 后旧 token 401，新 token 200。 |
| logout unchanged | logout 清 token 但保留 username row。 |
| old routes unavailable | captcha/register/api-tokens create/list/delete 为 404 或 410。 |
| public docs/capabilities unaffected | `/api-doc`、public capabilities 保持既有访问语义。 |
| business owner isolation unchanged | A/B conversation/task/upload/artifact/SSE 隔离测试继续通过。 |
| current DTO names tolerated | `AuthUserResponse/AuthTokenResponse/UserResponse` 作为当前 response types 不因 cleanup 被删除。 |

## 6. Historical Context Deletion Checks

### 必须不存在

```bash
test ! -e .omx/plans/prd-20260504-auth-user-memory.md
test ! -e .omx/plans/test-spec-20260504-auth-user-memory.md
test ! -e .omx/plans/prd-20260504-auth-user-registration.md
test ! -e .omx/plans/test-spec-20260504-auth-user-registration.md
test ! -e .omx/plans/prd-20260521-secure-session-cookie-auth.md
test ! -e .omx/plans/test-spec-20260521-secure-session-cookie-auth.md
test ! -e docs/superpowers/specs/2026-05-21-secure-session-cookie-auth-design.md
```

### 必须存在

```bash
test -e docs/superpowers/specs/2026-05-25-authorization-username-auth-design.md
test -e .omx/plans/prd-20260525-authorization-username-auth.md
test -e .omx/plans/test-spec-20260525-authorization-username-auth.md
test -e docs/superpowers/specs/2026-05-25-legacy-auth-context-removal-design.md
test -e .omx/plans/prd-20260525-legacy-auth-context-removal.md
test -e .omx/plans/test-spec-20260525-legacy-auth-context-removal.md
```

`CHANGELOG.md` 作为审计历史必须保留，但最新 Unreleased 条目需要说明旧认证上下文已删除。

## 7. Static Sweep and Allowlist

### Required sweeps

```bash
rg -n "\\b(AuthUser|CaptchaChallenge|AuthSession|AuthApiToken|AuthUserRow|CaptchaChallengeRow|AuthSessionRow|AuthApiTokenRow)\\b" src frontend/src docs/api docs/superpowers .omx/plans tests native
rg -n "__Host|maf_session|Cookie|cookie|captcha|register|api-tokens|required_scopes|AuthSource" src frontend/src docs/api docs/superpowers .omx/plans tests native
```

### Allowed current identifiers

These must not be treated as cleanup failures:

- `AuthUserToken`
- `AuthUserTokenRow`
- `AuthUserResponse`
- `AuthTokenResponse`
- `UserResponse`

### Allowed legacy-word contexts

Remaining hits are allowed only when they are:

- negative tests proving old routes/cookies are rejected;
- DTO reserved-key rejection lists preventing identity spoofing;
- generic sensitive-data redaction not tied to the removed auth model;
- `CHANGELOG.md` audit history;
- current cleanup design/plan/test-spec explaining what was removed.

Any other hit must be removed or explicitly justified in the implementation report.

## 8. Required Verification Commands

### Targeted

```bash
conda run -n multi_agent python -m unittest tests.core.test_rust_contract_artifact
conda run -n multi_agent python -m unittest tests.storage.test_sqlite_bootstrap tests.storage.test_sqlite_username_auth_repository tests.storage.test_sqlite_username_migration tests.storage.test_sqlite_conversation_delete
conda run -n multi_agent python -m unittest tests.api.test_authorization_username_auth tests.api.test_auth_cookie_security tests.api.test_auth_api_tokens tests.api.test_auth_login_and_isolation tests.api.test_developer_docs
conda run -n multi_agent python -m unittest tests.api.test_task_events_sse tests.api.test_uploads
cd native && cargo fmt --check && cargo test -p maf_core_types && cargo check --workspace --all-targets --all-features && cargo deny check
```

### Regression

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

## 9. Completion Evidence Template

Implementation is complete only when the final report records:

1. Changed files by checkpoint.
2. Deleted legacy docs/tests/code surfaces.
3. Static sweep command outputs and allowlist explanation.
4. Targeted and regression command outputs.
5. License Requirement result, including `cargo deny check` because native/Rust files change.
6. Confirmation that current Authorization-only behavior did not change.
7. Known gaps, if any, with exact reason and next action.
