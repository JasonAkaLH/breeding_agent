# PRD — 旧认证上下文残留硬删除实施计划

- **日期**：2026-05-25
- **设计来源**：`docs/superpowers/specs/2026-05-25-legacy-auth-context-removal-design.md`
- **关联当前认证计划**：`.omx/plans/prd-20260525-authorization-username-auth.md`
- **状态**：待实施
- **范围**：删除旧 Cookie/password/captcha/scoped API token core/storage/Rust contract 残留，启动时 drop 旧 SQLite auth 表，删除旧认证历史计划/设计上下文，并保持当前 Authorization-only username token 业务行为不变。

## 1. Requirements Summary

本计划把已审定的旧认证上下文清理设计拆成可执行 checkpoint：

1. 硬删除旧认证 core model：`AuthUser`、`CaptchaChallenge`、`AuthSession`、`AuthApiToken`。
2. 硬删除旧 `StoragePort` 方法、SQLite row class、mapper、sync/async repository 方法。
3. `bootstrap_sqlite_database()` 必须在启动迁移中幂等 drop `auth_user`、`auth_captcha_challenge`、`auth_session`、`auth_api_token`。
4. 保留并验证当前唯一认证持久路径：`AuthUserToken` / `auth_user_token` / `UsernameTokenService`。
5. 保持当前登录、登出、刷新、`/auth/me`、业务 owner guard、上传、SSE、conversation history、memory、artifact、pending skill context 行为不变。
6. 删除旧 Cookie/password/captcha/register/scoped-token 设计、PRD、test-spec 上下文，避免未来误引用。
7. 保留 `CHANGELOG.md` 作为审计历史；只要求最新条目说明旧上下文已删除。
8. 通过静态扫描和回归测试证明没有未解释旧认证残留，也没有误删当前 Authorization-only 标识。

## 2. Baseline Evidence

| 证据 | 当前状态 | 实施影响 |
| --- | --- | --- |
| `src/core/models.py:60-100` | 仍定义 `AuthUser`、`CaptchaChallenge`、`AuthSession`、`AuthApiToken`。 | 删除这些 dataclass；保留 `src/core/models.py:104-110` 的 `AuthUserToken`。 |
| `src/core/contracts.py:8-17`、`:67-122` | `StoragePort` 仍 import 并暴露旧 auth/session/scoped-token 方法，同时也有当前 `AuthUserToken` 方法。 | 删除旧 imports/methods；保留 `save/get/touch/clear/rotate_auth_user_token`。 |
| `src/storage/sqlite/models.py:72-131` | 旧 `auth_user`、`auth_captcha_challenge`、`auth_session`、`auth_api_token` row classes 仍在 metadata。 | 删除旧 row classes，避免 `create_all` 重建旧表。 |
| `src/storage/sqlite/models.py:134-146` | 当前 `AuthUserTokenRow` 已是 `auth_user_token` 单 token 表。 | 保留并作为唯一 auth persistence row。 |
| `src/storage/sqlite/repositories.py:128-173` | 旧 row-to-model mapper 仍存在。 | 删除旧 mapper；保留 `_row_to_auth_user_token`。 |
| `src/storage/sqlite/repositories.py:412-531`、`:1339-1375` | sync/async 旧 auth repository 方法仍存在。 | 删除这些方法；保留当前 token 方法。 |
| `src/storage/sqlite/bootstrap.py:8-10` | 当前 bootstrap 顺序为 `_migrate_username_owner_columns(engine)` 后 `SQLiteBase.metadata.create_all(engine)`。 | 插入 `_drop_legacy_auth_tables(engine)`，顺序必须保留 owner migration，再 drop 旧 auth 表，再 create_all。 |
| `src/core/rust_contracts/core_contract.json:294-346` | Rust contract JSON 仍含 `AuthSession`、`AuthUser`、`CaptchaChallenge`，并保留 `AuthUserToken`。 | 删除旧 contract entries，保留 `AuthUserToken`，更新 schema hash。 |
| `native/crates/maf_core_types/src/lib.rs:265-299` | native snapshot map 仍含 `AuthSession`、`AuthUser`、`CaptchaChallenge`，并保留 `AuthUserToken`。 | 同步删除旧 entries，保留 `AuthUserToken`。 |
| `tests/storage/test_sqlite_auth_repository.py:5-47` | 旧 password/captcha/session 正向 storage 测试。 | 删除整文件或删除旧 auth 用例；其中 conversation list 用例若仍有价值需迁移到现有 conversation repository tests。 |
| `tests/storage/test_sqlite_api_token_repository.py:5-77` | 旧 scoped API token 正向 storage 测试。 | 删除整文件；当前 token lifecycle 由 username-token tests/API tests 覆盖。 |
| `tests/storage/test_sqlite_conversation_delete.py:8-43`、`:53-77`、`:214-216` | 混合业务删除测试用旧 auth rows 验证“保留 auth records”。 | 重写 fixture：保留 conversation deletion 覆盖，改为当前 `AuthUserTokenRow` 或非 auth fixture，不能整文件删除。 |
| `tests/api/test_auth_cookie_security.py:14-40` | 当前是负向 cookie 测试：login 不写 Cookie、cookie-only 401。 | 保留或改名，不得删除该行为保护。 |
| `tests/api/test_authorization_username_auth.py:25-72` | 当前已有 Authorization-only 行为测试和旧 route 下线测试。 | 继续作为 API 行为回归门禁。 |
| `.omx/plans/prd-20260504-auth-user-memory.md` 等旧文件 | 旧 password/captcha/Cookie/scoped-token 计划仍作为活跃上下文存在。 | 按设计精确删除。 |

## 3. Acceptance Criteria

| ID | Criterion | Validation |
| --- | --- | --- |
| AC-1 | Python core model 不再暴露 `AuthUser`、`CaptchaChallenge`、`AuthSession`、`AuthApiToken`。 | `tests/core/test_rust_contract_artifact.py` + static sweep。 |
| AC-2 | `StoragePort` 不再暴露旧 auth/session/scoped-token 方法。 | core/import tests + static sweep。 |
| AC-3 | SQLite metadata 不再包含 `auth_user`、`auth_captcha_challenge`、`auth_session`、`auth_api_token` row classes。 | storage bootstrap tests + metadata inspection。 |
| AC-4 | 旧 SQLite auth 表在 bootstrap 后被 drop；缺表时 bootstrap 仍幂等成功。 | legacy DB fixture test。 |
| AC-5 | `auth_user_token` 仍创建、读写、清空、旋转 token 正常。 | `tests/storage/test_sqlite_username_auth_repository.py`。 |
| AC-6 | `test_sqlite_conversation_delete.py` 仍验证删除 conversation 会清理 business rows，但不会依赖旧 auth rows。 | storage test。 |
| AC-7 | 当前 API 行为不变：login/me/logout/refresh、旧 token 失效、Cookie-only 401、旧 route 404/410。 | API auth tests。 |
| AC-8 | Rust contract JSON 和 native snapshot 不再含旧 auth model，保留 `AuthUserToken`，schema hash 更新。 | core contract tests + `cargo test -p maf_core_types`。 |
| AC-9 | 指定旧历史 auth design/PRD/test-spec 文件从 active repository context 删除。 | `test -e` negative checks + static sweep。 |
| AC-10 | README/API docs 只把 Authorization-only username token 描述为正式认证方式。 | `tests/api/test_developer_docs.py` + static sweep allowlist。 |
| AC-11 | 静态扫描无未解释 legacy residue，且不会误伤 `AuthUserToken/AuthUserResponse/AuthTokenResponse/UserResponse`。 | word-boundary `rg` + implementation report allowlist。 |
| AC-12 | License Requirement 满足：因 native/Rust contract 变更，必须运行 `cargo deny check`。 | final verification log。 |

## 4. Checkpoint Plan

| Checkpoint | 目标 | 主要文件 | 退出门禁 | 可并行性 |
| --- | --- | --- | --- | --- |
| CP-0 Red tests and scope lock | 先补/调整失败测试，锁定 drop 表、contract 删除、混合测试改写、历史文档删除。 | `tests/storage/test_sqlite_bootstrap.py` 或新增 `test_sqlite_legacy_auth_cleanup.py`、`tests/core/test_rust_contract_artifact.py`、相关 static sweep notes。 | 目标测试能在旧实现上失败；未改生产代码。 | 不并行；所有后续依赖。 |
| CP-1 Python core/storage deletion | 删除 Python 旧 model/contract/SQLite row/repository surface，保留当前 username token。 | `src/core/models.py`、`src/core/contracts.py`、`src/storage/sqlite/models.py`、`src/storage/sqlite/repositories.py`。 | core/storage targeted tests 通过；current `AuthUserToken` tests 通过。 | 可由一个后端执行 lane 完成。 |
| CP-2 Bootstrap destructive table cleanup | 实现 idempotent drop helper，重写混合 storage test，证明 business rows 不受影响。 | `src/storage/sqlite/bootstrap.py`、`tests/storage/test_sqlite_bootstrap.py`、`tests/storage/test_sqlite_conversation_delete.py`。 | legacy DB bootstrap 后旧表不存在，`auth_user_token` 仍存在，owner migration 仍通过。 | 依赖 CP-1 metadata 删除；可与 CP-3 之后并行验证。 |
| CP-3 Rust contract snapshot sync | 同步 core contract JSON/native snapshot/hash。 | `src/core/rust_contracts/core_contract.json`、`native/crates/maf_core_types/src/lib.rs`、`tests/core/test_rust_contract_artifact.py`。 | Python/Rust model snapshot 一致；cargo targeted tests 通过。 | 依赖 CP-1 Python model 形态。 |
| CP-4 Historical context deletion and docs hardening | 删除指定旧计划/设计文档，确保当前 docs/API/README 不回退。 | `.omx/plans/*auth*` 旧文件、`docs/superpowers/specs/2026-05-21-secure-session-cookie-auth-design.md`、`docs/api/api-doc.html`、`README.md`。 | 精确 delete list 不存在；developer docs tests 与 static sweep allowlist 通过。 | 可在 CP-1/2/3 之后并行收尾。 |
| CP-5 Full regression, license, and final sweep | 全面验证无行为回退和无未解释残留。 | 全仓库测试/命令。 | Required Verification Commands 全部通过或明确环境缺口；`git diff --check` 通过；License Requirement 记录。 | 最后执行。 |

Gate rule：任何 checkpoint 失败时，只能修复该 checkpoint 或上游依赖；不得通过恢复旧 Cookie/password/captcha/scoped-token fallback 让测试变绿。

## 5. Implementation Steps

### Step 1 — TDD：锁定旧 auth 删除与当前行为保留

**目标**：先写或更新测试，证明实现必须删除旧 auth surface，但不能影响当前 Authorization-only 行为。

**文件**：

- `tests/storage/test_sqlite_bootstrap.py` 或新增 `tests/storage/test_sqlite_legacy_auth_cleanup.py`
- `tests/storage/test_sqlite_conversation_delete.py`
- `tests/core/test_rust_contract_artifact.py`
- `tests/api/test_authorization_username_auth.py`
- `tests/api/test_auth_cookie_security.py`
- `tests/api/test_auth_api_tokens.py`

**测试点**：

- 构造 legacy SQLite DB，包含四张旧 auth 表和数据；bootstrap 后四张表消失。
- bootstrap 缺旧 auth 表时也成功。
- bootstrap 后 `auth_user_token` 可正常创建和读写。
- `test_sqlite_conversation_delete.py` 不 import 旧 auth model/row，仍验证 business rows 被删除。
- contract tests 断言旧 model name absent，`AuthUserToken` present。
- API 负向 cookie/旧 route tests 继续存在并通过。

**完成标准**：目标测试能指向当前旧实现失败；不引入生产代码变更。

### Step 2 — 删除 Python core/storage 旧认证 surface

**目标**：删除旧模型与旧 repository 能力，迫使所有残留引用显性失败。

**文件**：

- `src/core/models.py`
- `src/core/contracts.py`
- `src/storage/sqlite/models.py`
- `src/storage/sqlite/repositories.py`
- `tests/storage/test_sqlite_auth_repository.py`
- `tests/storage/test_sqlite_api_token_repository.py`

**实现要点**：

- 删除 `AuthUser`、`CaptchaChallenge`、`AuthSession`、`AuthApiToken` dataclass。
- 删除 `StoragePort` 中旧 save/get/list/touch/revoke 方法与 imports。
- 删除 `AuthUserRow`、`CaptchaChallengeRow`、`AuthSessionRow`、`AuthApiTokenRow`。
- 删除 `_row_to_auth_user`、`_row_to_captcha_challenge`、`_row_to_auth_session`、`_row_to_auth_api_token`。
- 删除 sync/async 旧 repository 方法。
- 删除旧正向 storage test 文件；把非 auth conversation list 覆盖迁移到现有 conversation repository test（如缺覆盖）。
- 保留 `AuthUserToken`、`AuthUserTokenRow`、`_row_to_auth_user_token` 与所有 username-token repository 方法。

**完成标准**：core/storage import 不再依赖旧 auth types，username-token repository tests 通过。

### Step 3 — 实现 bootstrap drop legacy auth tables

**目标**：旧 SQLite auth 表在启动时被幂等删除，同时不影响 username owner migration 和 business data。

**文件**：

- `src/storage/sqlite/bootstrap.py`
- `tests/storage/test_sqlite_bootstrap.py`
- `tests/storage/test_sqlite_conversation_delete.py`

**实现要点**：

- 新增 `_drop_legacy_auth_tables(engine)` 或等价 helper。
- 启动顺序必须是：`_migrate_username_owner_columns(engine)` → `_drop_legacy_auth_tables(engine)` → `SQLiteBase.metadata.create_all(engine)`。
- drop SQL 必须是 `DROP TABLE IF EXISTS`，表名固定 allowlist，不接收外部输入。
- 如果 drop 出错，bootstrap fail fast。
- 因旧 row classes 从 metadata 删除，`create_all` 不得重建旧表。
- 重写 `test_sqlite_conversation_delete.py`：如果要验证 auth 数据保留，改为当前 `AuthUserTokenRow`；否则只验证 business rows purge。

**完成标准**：legacy DB fixture bootstrap 后旧表不存在，业务表和 `auth_user_token` 正常。

### Step 4 — 同步 Rust core contract snapshot

**目标**：Python/Rust contract 对旧 auth deletion 达成一致。

**文件**：

- `src/core/rust_contracts/core_contract.json`
- `native/crates/maf_core_types/src/lib.rs`
- `tests/core/test_rust_contract_artifact.py`

**实现要点**：

- 删除 contract JSON 中 `AuthSession`、`AuthUser`、`CaptchaChallenge` entries。
- 确认 `AuthApiToken` 不存在；若出现则删除。
- 保留 `AuthUserToken` fields 与 Python dataclass 一致。
- 更新 `schema_hash`，建议命名体现 legacy auth removal，例如 `maf_core_types_core_v1_schema_20260525_username_token_legacy_auth_removed`。
- 同步 native Rust snapshot map 与 schema hash 常量。

**完成标准**：`tests/core/test_rust_contract_artifact.py`、`cargo test -p maf_core_types`、`cargo check` 相关命令通过。

### Step 5 — 删除旧历史 auth context 并保护当前文档

**目标**：清理会误导未来实现的旧计划/设计文档。

**必须删除**：

- `.omx/plans/prd-20260504-auth-user-memory.md`
- `.omx/plans/test-spec-20260504-auth-user-memory.md`
- `.omx/plans/prd-20260504-auth-user-registration.md`
- `.omx/plans/test-spec-20260504-auth-user-registration.md`
- `.omx/plans/prd-20260521-secure-session-cookie-auth.md`
- `.omx/plans/test-spec-20260521-secure-session-cookie-auth.md`
- `docs/superpowers/specs/2026-05-21-secure-session-cookie-auth-design.md`

**必须保留**：

- `docs/superpowers/specs/2026-05-25-authorization-username-auth-design.md`
- `.omx/plans/prd-20260525-authorization-username-auth.md`
- `.omx/plans/test-spec-20260525-authorization-username-auth.md`
- `docs/superpowers/specs/2026-05-25-legacy-auth-context-removal-design.md`
- 本计划与关联 test-spec。
- `CHANGELOG.md` 历史审计条目。

**完成标准**：精确 delete list 不存在；当前 docs/API/README 只把 Authorization-only username token 作为正式认证方式。

### Step 6 — Static sweep、回归验证与收口报告

**目标**：证明没有未解释旧认证残留，也没有为了扫描误删当前行为。

**实现要点**：

- 使用 word-boundary 扫描旧 class/model names。
- 对 Cookie/captcha/register/api-token route 词汇做语义 allowlist：负向测试、reserved-key rejection、changelog、当前 cleanup docs 可以保留。
- 明确 allowlist 当前标识：`AuthUserToken`、`AuthUserTokenRow`、`AuthUserResponse`、`AuthTokenResponse`、`UserResponse`。
- 在最终报告记录 License Requirement：native/Rust contract 变更触发 `cargo deny check`。

**完成标准**：测试规格中的 Required Verification Commands 全部完成或记录真实环境缺口。

## 6. Team + Ultragoal Follow-up Guidance

本任务适合 **Team + Ultragoal**：Team 并行执行文件面，Ultragoal 作为 leader-owned durable ledger 记录 checkpoint 完成证据。不要用 Team worker 自行改全局计划。

### Available-agent-types roster

| Role | Recommended effort | Lane |
| --- | --- | --- |
| `executor` | medium | Python core/storage/bootstrap 删除与测试实现。 |
| `test-engineer` | medium | TDD red tests、legacy DB fixture、API behavior preservation、static sweep。 |
| `architect` | high | Rust contract/schema hash 与 destructive cleanup 顺序复核。 |
| `verifier` | high | 最终 regression、license gate、allowlist 证据收集。 |
| `code-reviewer` | high | 审查是否误删当前 Authorization-only behavior。 |
| `git-master` | medium | 分 checkpoint 提交/最终提交整理，如用户要求。 |

### Suggested Team lanes

1. **Lane A — Python core/storage cleanup**：`executor`，负责 CP-1/CP-2 代码和 storage tests。
2. **Lane B — Contract/Rust snapshot**：`executor` + `architect` review，负责 CP-3。
3. **Lane C — Docs/context/static sweep**：`test-engineer` 或 `writer`，负责 CP-4/静态扫描 allowlist。
4. **Lane D — Verification**：`verifier`，按 CP-5 跑 targeted/regression/license gate 并输出证据。

### Launch hints

```bash
# Team pipeline（建议 4 workers，上述 lanes）
omx team start --name legacy-auth-context-removal --workers 4 --goal "Implement .omx/plans/prd-20260525-legacy-auth-context-removal.md and .omx/plans/test-spec-20260525-legacy-auth-context-removal.md without changing current Authorization-only behavior"

# Ultragoal ledger（leader 负责 checkpoint 证据）
# 使用本计划 + test spec 作为 durable goal context，按 CP-0..CP-5 checkpoint 记录证据。
```

Team shutdown 前必须证明：CP-0..CP-5 均完成、required commands 已读输出、static sweep allowlist 已解释、License Requirement 已记录。Ultragoal checkpoint 应保存每个 checkpoint 的测试输出摘要、残留风险和最终 commit/dirty tree 状态。

## 7. Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Drop 旧 auth 表删除仍有人期望保留的数据。 | 旧 password/session/scoped token 数据不可恢复。 | 这是用户批准的 hard-delete；计划明确用户重新 login 会创建当前 `auth_user_token`。 |
| `create_all` 重新创建旧表。 | 清理失败且静态扫描不明显。 | 先删旧 SQLAlchemy row classes，再加 bootstrap drop test 验证表不存在。 |
| 当前 `AuthUserToken` / `AuthUserResponse` 被误删。 | 破坏现有 API/前端类型。 | word-boundary sweep + allowlist；code review 必查。 |
| `test_sqlite_conversation_delete.py` 被整文件删除导致 business purge 失去覆盖。 | conversation deletion regression 漏测。 | 明确 rewrite-only；保留 business row assertions。 |
| Rust contract hash/snapshot 漂移。 | PyO3/core contract enforce/shadow 风险。 | JSON/native/hash/tests 同步改；跑 `cargo test -p maf_core_types` 和 `cargo check`。 |
| 历史 changelog 被误删。 | 丢失审计历史。 | 保留 `CHANGELOG.md`，只更新 Unreleased 当前状态。 |
| 静态扫描中 generic security/token/password 词汇误报。 | 过度删除安全文档或 redaction 逻辑。 | 只删除主语义为旧 auth model 的文档；generic sensitive-data docs allowlist。 |

## 8. ADR

- **Decision**：按硬删除方式清理旧认证上下文；旧 SQLite auth 表启动时 drop；当前 Authorization-only username token 行为不变。
- **Drivers**：避免旧设计误导未来 agent；减少无用 storage/contract surface；用户已确认不再管理账户/password/captcha/Cookie/scoped token。
- **Alternatives considered**：
  - Archive/supersede old docs instead of delete：拒绝，因为用户明确选择删除旧历史上下文。
  - Keep old tables but unused：拒绝，因为会让未来实现误判为可迁移数据源。
  - Rename current `AuthUserResponse` immediately：不纳入本计划，因为会触碰当前 API/type naming，且不是旧行为删除所必需。
- **Consequences**：旧 auth 数据不可用；当前用户需通过 username login 重新生成 token；Rust/native contract 必须同步更新并跑 license gate。
- **Follow-ups**：实施后如仍想清理 current DTO 命名，可单独设计 behavior-neutral rename 任务，不能混入本次 hard-delete。

## 9. Plan Changelog

- 2026-05-25：初版实施计划，来源于 document-perfectization 加固后的 legacy auth context removal design。
