# Legacy Auth Context Removal Design

- **Date**: 2026-05-25
- **Status**: Reviewed and hardened for implementation planning
- **Decision owner**: internal Authorization-only username auth migration
- **Related current design**: `docs/superpowers/specs/2026-05-25-authorization-username-auth-design.md`
- **Related current plan**: `.omx/plans/prd-20260525-authorization-username-auth.md`

## 1. Purpose

The Authorization-only username auth migration is already the active runtime direction. The remaining risk is stale legacy auth context: password users, captcha challenges, Cookie sessions, scoped API tokens, their storage contracts, and historical design documents still exist in the repository even though the API runtime no longer uses them.

This cleanup removes those old concepts from code, SQLite schema, tests, Rust core contract snapshots, and historical auth planning context so future agents and developers cannot accidentally revive the old Cookie/password/captcha/scoped-token model.

## 2. User-approved decisions

1. **Removal mode**: hard delete legacy code and old SQLite tables.
2. **Database behavior**: `bootstrap_sqlite_database()` must drop legacy auth tables with idempotent `DROP TABLE IF EXISTS` statements.
3. **Historical context behavior**: delete old Cookie/password/captcha/scoped-token design and plan documents instead of archiving or marking them superseded.
4. **Preserved behavior**: current Authorization-only username token runtime behavior must not change.

### 2.1 Confidence standard for this cleanup

This design is implementation-ready only if it satisfies all of these gates:

- **Goal fit**: it removes stale Cookie/password/captcha/scoped-token context that can revive the replaced auth model.
- **Scope precision**: it names the exact legacy code, tables, tests, and known historical documents to delete.
- **Behavior preservation**: current `Authorization: Bearer` username token behavior, owner guards, uploads, SSE, conversation history, memory, task, artifact, and pending skill behavior stay unchanged.
- **Data boundary**: only legacy auth tables are destructively dropped; business tables and current `auth_user_token` remain intact.
- **False-positive control**: current identifiers that contain the words `AuthUser` or `token` are explicitly allowed when they belong to the Authorization-only contract.
- **Testability**: every destructive change has a targeted storage/bootstrap/API/contract/static-sweep verification path.
- **Risk visibility**: assumptions about changelog history, document deletion, and Rust contract drift are recorded instead of hidden.

## 3. Scope

### 3.1 Delete from active code and contracts

Remove these legacy core concepts:

- `AuthUser`
- `CaptchaChallenge`
- `AuthSession`
- `AuthApiToken`

Do **not** remove current Authorization-only identifiers merely because their names include `AuthUser` or `token`. These are current and must remain unless a separate behavior-neutral rename is explicitly planned with API JSON kept stable:

- `AuthUserToken`
- `AuthUserTokenRow`
- `AuthUserResponse`
- `AuthTokenResponse`
- `UserResponse`

Remove their `StoragePort` methods:

- `save_auth_user` / `get_auth_user`
- `save_captcha_challenge` / `get_captcha_challenge`
- `save_auth_session` / `get_auth_session`
- `save_auth_api_token` / `get_auth_api_token` / `get_auth_api_token_by_hash`
- `list_auth_api_tokens_for_user`
- `touch_auth_api_token_last_used`
- `revoke_auth_api_token_for_user`

Remove their SQLite implementation surface:

- `AuthUserRow`
- `CaptchaChallengeRow`
- `AuthSessionRow`
- `AuthApiTokenRow`
- `_row_to_auth_user`
- `_row_to_captcha_challenge`
- `_row_to_auth_session`
- `_row_to_auth_api_token`
- sync and async repository methods for those models

Update the Rust core contract snapshot so it no longer declares legacy auth models. Current repo evidence shows `AuthUser`, `AuthSession`, and `CaptchaChallenge` in the Rust contract snapshot; remove those entries and assert `AuthApiToken` is absent there as well. Update the schema hash after the snapshot changes. Keep `AuthUserToken` in both Python and Rust contract surfaces.

### 3.2 Drop legacy SQLite tables

`bootstrap_sqlite_database()` must drop these old tables before or during startup migration:

- `auth_user`
- `auth_captcha_challenge`
- `auth_session`
- `auth_api_token`

The drop is destructive by design. Old password, captcha, Cookie session, and scoped API token data is no longer a supported source of truth. Users re-enter the system by calling the current username-only login endpoint, which creates or replaces the row in `auth_user_token`.

### 3.3 Delete old historical auth context

Delete these known repository documents because their main subject is the replaced auth model:

- `.omx/plans/prd-20260504-auth-user-memory.md`
- `.omx/plans/test-spec-20260504-auth-user-memory.md`
- `.omx/plans/prd-20260504-auth-user-registration.md`
- `.omx/plans/test-spec-20260504-auth-user-registration.md`
- `.omx/plans/prd-20260521-secure-session-cookie-auth.md`
- `.omx/plans/test-spec-20260521-secure-session-cookie-auth.md`
- `docs/superpowers/specs/2026-05-21-secure-session-cookie-auth-design.md`

Also delete any newly discovered document whose main subject is one of these replaced paths:

- Cookie session auth designs, PRDs, and test specs.
- Password + captcha login designs, PRDs, and test specs.
- Registration/account-management designs, PRDs, and test specs that describe the removed auth model.
- Scoped API token create/list/revoke management designs, PRDs, and test specs.
- Implementation plans that present those old auth paths as current or future work.

Keep current Authorization-only username auth context:

- `docs/superpowers/specs/2026-05-25-authorization-username-auth-design.md`
- `.omx/plans/prd-20260525-authorization-username-auth.md`
- `.omx/plans/test-spec-20260525-authorization-username-auth.md`
- `docs/superpowers/specs/2026-05-25-legacy-auth-context-removal-design.md`
- README, API docs, and CHANGELOG entries that state the current Authorization-only behavior.

Do not delete unrelated security, audit, Rust, MCP, or redaction documents merely because they mention words such as token, password, Authorization, or cookie as generic sensitive-data examples.

Assumption: `CHANGELOG.md` is an audit log, not active product/design context. Do not rewrite unrelated chronological changelog history solely to erase old decisions. The latest unreleased changelog section must, however, state that old Cookie/password/captcha/scoped-token context was superseded and removed.

## 4. Non-goals

- Do not change the current login, logout, refresh, or `/auth/me` API behavior.
- Do not add user account management, passwords, captcha, TTL, scopes, role management, or multi-device token support.
- Do not migrate legacy password/session/token rows into `auth_user_token`.
- Do not delete conversation, message, task, event, artifact, upload, memory, or pending skill context data.
- Do not remove generic sensitive-data redaction logic that protects tokens/passwords in unrelated integrations.

## 5. Target architecture after cleanup

The auth model has one active persistence concept:

```text
username -> auth_user_token(username primary key, nullable api_token_hash, issued/last-used timestamps)
```

The runtime path remains:

```text
POST /api/v1/auth/login {username}
  -> UsernameTokenService.login_username()
  -> save_auth_user_token(username, hash(raw token))
  -> return raw token once

Protected request
  -> Authorization: Bearer <raw token>
  -> UsernameTokenService.get_current_token()
  -> get_auth_user_token_by_hash(hash(raw token))
  -> route owner guard uses AuthenticatedUser.username
```

Logout clears `api_token_hash` on the current `username` row. Refresh conditionally replaces the old hash with a new hash. No old auth model participates in this path.

## 6. Execution design

### 6.1 Contract-first deletion

Start by deleting legacy model definitions and storage protocol methods. This forces all remaining references to fail fast during type/import/test execution. Then remove each implementation reference until only `AuthUserToken` remains in the auth storage contract.

### 6.2 SQLite model and repository deletion

Remove old SQLAlchemy row classes and repository methods after the public contract is narrowed. This prevents orphaned implementation code that cannot be reached from `StoragePort`.

### 6.3 Bootstrap table deletion

Add a small bootstrap helper that executes idempotent table drops for the legacy auth tables. It should run in the same startup bootstrap transaction style as existing migrations. If a drop fails, startup should fail fast; the runtime must not continue with a partially cleaned auth schema.

Required startup order:

1. Preserve the existing `account_id -> username` owner migration behavior.
2. Drop the legacy auth tables with `DROP TABLE IF EXISTS`.
3. Run `SQLiteBase.metadata.create_all(engine)` or its current equivalent.

Because the old SQLAlchemy row classes are removed from metadata, `create_all` must not recreate `auth_user`, `auth_captcha_challenge`, `auth_session`, or `auth_api_token`.

### 6.4 Historical context deletion

Delete replaced auth design/plan/test-spec files after the code and tests establish the new contract. A static sweep should confirm no remaining active design or plan file presents Cookie/password/captcha/scoped token auth as a supported or pending path.

## 7. Data flow and behavior preservation

Business data remains keyed by `username` through current owner fields:

- conversations
- conversation memory summaries
- pending skill context
- uploads
- task and artifact access through conversation owner guards

Dropping old auth tables does not affect these tables. A user whose old password/session data is removed simply has no valid Authorization token until they log in again with `username`.

## 8. Error handling

- Missing or invalid Bearer token still returns 401.
- Removed legacy auth routes still return 404 or 410 because no route should be reintroduced.
- Bootstrap old-table cleanup is idempotent for missing tables.
- Bootstrap old-table cleanup fails fast if SQLite cannot drop the old table.
- No fallback may recreate old tables or read old session/password/token rows.

## 9. Tests and verification

### 9.1 Storage and bootstrap

Add or update tests to prove:

- A legacy SQLite database containing `auth_user`, `auth_captcha_challenge`, `auth_session`, and `auth_api_token` loses those tables after bootstrap.
- `auth_user_token` is still created and works after cleanup.
- Existing `account_id -> username` owner migration for conversation, memory summary, and pending skill context still works.
- New username-owned writes still work after bootstrapping a legacy database.

Delete old positive tests for password/captcha/session/scoped-token storage:

- `tests/storage/test_sqlite_auth_repository.py`
- `tests/storage/test_sqlite_api_token_repository.py`

Rewrite, rather than blindly delete, any mixed-purpose test that currently seeds old auth rows but also protects non-auth behavior. In the current repo, `tests/storage/test_sqlite_conversation_delete.py` must keep its conversation-deletion assertions and replace legacy auth fixture rows with current username/auth-user-token or non-auth fixtures.

### 9.2 Core and Rust contract

Update tests to prove:

- `AuthUserToken` remains in the Python and Rust contract model snapshot.
- `AuthUser`, `CaptchaChallenge`, and `AuthSession` are removed from the Python and Rust contract snapshots.
- `AuthApiToken` is absent from the Python contract and either removed from, or confirmed absent in, the Rust contract snapshot.
- The checked-in Rust contract JSON matches Python model fields.

### 9.3 API behavior

Keep Authorization-only API tests proving:

- login accepts only `username` and returns `access_token`.
- `/auth/me`, logout, refresh, and protected APIs require Bearer.
- re-login and refresh invalidate old tokens.
- logout clears the token but preserves the username row.
- old captcha/register/api-token management routes remain unavailable.
- Cookie-only requests remain unauthorized.

### 9.4 Static sweeps

Run a static sweep over active code, tests, docs, specs, and plans. Remaining matches for legacy terms must be limited to:

- negative tests asserting old routes/fields are unavailable;
- DTO reserved-key rejection lists;
- generic sensitive-data redaction unrelated to this auth model;
- changelog summaries that state the old model was removed or replaced.

Suggested sweep:

```bash
rg -n "\\b(AuthUser|CaptchaChallenge|AuthSession|AuthApiToken|AuthUserRow|CaptchaChallengeRow|AuthSessionRow|AuthApiTokenRow)\\b|__Host|maf_session|Cookie|cookie|captcha|register|api-tokens|required_scopes|AuthSource" src frontend/src docs/api docs/superpowers .omx/plans tests native
```

The word-boundary form is required so the sweep does not fail on current identifiers such as `AuthUserToken`, `AuthUserTokenRow`, `AuthUserResponse`, `AuthTokenResponse`, and `UserResponse`. If a remaining hit is current behavior, the implementation report must explain it explicitly instead of deleting behavior to satisfy a broad substring search.

### 9.5 Required command surface

At minimum, run targeted suites for core, storage, API auth/docs/SSE/upload, and frontend auth client/task events. Because Rust contract files change, run Rust contract checks and license gate:

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
npm --prefix frontend test -- --run
npm --prefix frontend run build
cd native && cargo fmt --check && cargo test -p maf_core_types && cargo check --workspace --all-targets --all-features && cargo deny check
git diff --check
```

## 10. Completion criteria

The cleanup is complete only when all of these are true:

1. Active code has no legacy auth model, storage method, SQLite row, or repository method.
2. `auth_user_token` and `UsernameTokenService` remain the only auth persistence/service path.
3. Bootstrap drops old auth tables and keeps business data intact.
4. Old auth design/plan/test-spec context is deleted from active repository context.
5. Current docs describe only Authorization-only username auth.
6. Tests and static sweeps show no unexplained legacy auth residue.
7. License requirement is satisfied because native/Rust contract files change.
8. Static sweep allowlists current Authorization-only identifiers instead of forcing behavior-changing renames.
9. The exact historical context delete list in section 3.3 is gone from active repository context.

## 11. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Dropping old auth tables deletes data someone expected to keep. | This is an explicit user-approved hard-delete decision; current username login recreates the active token mapping. |
| Over-broad document deletion removes unrelated security context. | Delete only documents whose main subject is old auth. Keep generic sensitive-data docs. |
| Rust contract/PyO3 contract drift. | Update schema hash, JSON artifact, Rust model snapshot, and contract tests together. |
| Hidden runtime reference remains. | Delete public contract first, then run import/compile/tests/static sweep. |
| Business owner migration regresses. | Keep existing username migration tests and add old-auth-table drop tests alongside them. |
| Static sweep false positives push developers to remove current API/DTO names. | Use word-boundary matching and explicitly allow current `AuthUserToken` / `AuthUserResponse` / token response identifiers. |
| Mixed-purpose tests lose non-auth coverage during cleanup. | Rewrite mixed tests such as `test_sqlite_conversation_delete.py` instead of deleting their conversation behavior assertions. |
| Changelog history is mistaken for active design context. | Treat changelog as audit history; only latest entries and active docs must reflect the current Authorization-only model. |
