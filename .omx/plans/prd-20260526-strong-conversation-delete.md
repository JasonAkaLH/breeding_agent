# PRD — Strong Conversation Delete

日期：2026-05-26  
状态：Ready for implementation planning handoff  
设计来源：`docs/superpowers/specs/2026-05-26-strong-conversation-delete-design.md`

## 1. Requirements Summary

目标是在远端 PostgreSQL 生产模式下，把“删除历史会话”改造成强删除意图 + 后端托管物理删除：用户确认删除后，该 conversation 立即进入普通用户不可见的 `deleting` 状态；前端对应历史条目显示 spinner；后端 deletion runner 即使在浏览器刷新、网络断开或 HTTP 请求取消后仍继续执行任务取消、artifact 文件清理和多表物理删除；成功返回时必须代表物理删除完成；失败时普通用户列表不复活该会话，后台保留 `deleting_failed` 供诊断和重试。

## 2. Evidence Anchors

| Area | Evidence | Planning implication |
| --- | --- | --- |
| Design decisions | `docs/superpowers/specs/2026-05-26-strong-conversation-delete-design.md:24-31` | 强物理删除、条目 spinner、断线继续、失败不复活均已确认。 |
| Current frontend | `frontend/src/App.tsx:629-659` | 当前等待 DELETE 返回后才移除条目，需要条目级 deleting 状态。 |
| Current route | `src/api/routes/conversations.py:117-134` | route 已做 owner check，但 runtime 再读 conversation；计划中要消除重复读并扩展响应字段。 |
| Current runtime | `src/api/runtime.py:1048-1081` | 当前同步 delete 被请求生命周期承载，需要拆 runner、shield 和启动恢复。 |
| Current storage | `src/storage/sqlite/repositories.py:705-777` | 当前 Python 搬运 task/mailbox/interrupt id；PostgreSQL 生产路径要 set-based delete。 |
| Current list behavior | `src/storage/sqlite/repositories.py:539-545` | 当前按 username 返回所有状态；需要 active-only 普通列表。 |
| Current enum | `src/core/enums.py:17` plus runtime check | 当前 ConversationStatus 为 `active/archived/locked`；需新增 `deleting/deleting_failed` 并同步 Rust/core contract。 |
| Test expectations | `tests/api/test_auth_login_and_isolation.py:139-202` | 现有 delete 测试要求 owner scoped、purge history、auto-cancel running task；新实现必须保持并扩展。 |

## 3. In Scope

1. Conversation 状态与 deletion 元数据 schema。
2. Storage contract / repositories / PostgreSQL set-based physical delete。
3. Deletion runner、跨进程互斥、客户端断开保护、启动恢复。
4. 普通用户 API 对 `deleting` / `deleting_failed` 的不可见和不可写规则。
5. 前端历史条目 spinner、目标条目禁用、当前会话删除切换。
6. 最小运维诊断/重试脚本或内部入口。
7. API 文档更新。
8. Unit / API / frontend / PostgreSQL integration/smoke 验证。

## 4. Out of Scope

- 普通用户批量删除 API。
- 普通用户删除失败恢复 UI。
- 外部任务队列系统。
- 历史 SQLite 数据迁移。
- 完整管理后台。

## 5. Acceptance Criteria

| ID | Criterion | Verification |
| --- | --- | --- |
| AC-1 | DELETE 启动后 conversation 进入 `deleting`，普通 list/messages/submit/rename/SSE 对该会话不可见或不可操作。 | API/storage tests. |
| AC-2 | DELETE 成功响应不早于物理删除完成。 | Storage/API tests assert related rows gone after response. |
| AC-3 | 客户端断开或取消等待 DELETE 不取消 runner。 | API async cancellation test observes runner completion. |
| AC-4 | 运行中 task 会被同步取消并记录 cancelled ids。 | Existing running delete test extended. |
| AC-5 | Artifact refs 一次性读取；文件删除幂等；文件缺失视为成功。 | Runtime/storage tests with fake file store. |
| AC-6 | PostgreSQL delete uses set-based SQL / dialect path, not Python large ID list production path. | SQL/repository tests inspect compiled statements or repository behavior. |
| AC-7 | 删除失败进入 `deleting_failed`，普通用户列表不显示，运维诊断可见且脱敏。 | Failure injection tests + ops script test. |
| AC-8 | 应用启动扫描 `deleting` 并恢复 runner。 | Runtime assembly/startup test. |
| AC-9 | 删除目标历史条目显示 spinner，只禁用该条目，其他会话仍可用。 | Frontend App tests. |
| AC-10 | API docs 说明强删除、断线继续、失败不复活和新增响应字段。 | `tests/api/test_developer_docs.py`. |
| AC-11 | 无依赖/许可策略变更；若触及 Rust/native contract only, cargo-deny not required unless dependencies change. | Final report License Requirement. |

## 6. Implementation Steps

### CP-0 — Regression lock before behavior changes

- Add failing tests first for active-only conversation list, deleting-state invisibility, frontend spinner, and runner cancellation behavior.
- Extend existing delete tests rather than replacing them:
  - `tests/api/test_auth_login_and_isolation.py`
  - `tests/storage/test_sqlite_conversation_delete.py`
  - `frontend/src/App.test.tsx`
- Stop condition: tests fail for the intended missing behavior before implementation.

### CP-1 — Conversation status and deletion metadata

Files:
- `src/core/rust_contracts/core_contract.json`
- `native/crates/maf_core_types/src/lib.rs`
- `src/core/models.py`
- `src/storage/sqlite/models.py`
- `src/state/postgres/runtime_schema.py`
- `src/state/postgres/schema_reconciler.py`
- storage schema tests under `tests/storage/`

Work:
1. Add `deleting` and `deleting_failed` to `ConversationStatus` contract.
2. Add deletion metadata columns or an equivalent tracking table with runner id, requested/started/finished/failed timestamps, phase, error code, and sanitized error summary.
3. Update schema reconciliation so fresh PostgreSQL creates the columns and existing PostgreSQL adds them without drop privileges.
4. Keep successful physical delete as row removal; no persistent `deleted` status.

Stop condition: schema/model tests pass and schema reconciler can add missing deletion columns without DROP.

### CP-2 — Storage contract and active-only visibility

Files:
- `src/core/contracts.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/`
- `src/api/auth.py`
- API route files using conversation ownership / visibility.

Work:
1. Add storage APIs:
   - `mark_conversation_deleting(...)`
   - `update_conversation_delete_phase(...)`
   - `mark_conversation_delete_failed(...)`
   - `list_deleting_conversations(...)`
   - `list_artifacts_for_conversation(...)`
   - PostgreSQL optimized `delete_conversation_physical(...)` or dialect-specific implementation.
2. Make ordinary `list_conversations_for_username` return active only.
3. Add explicit internal methods for runner/admin access to deleting/deleting_failed rows so ordinary user APIs do not accidentally expose them.
4. Update owner helpers so user-facing read/write APIs reject `deleting` / `deleting_failed` with 404.

Stop condition: user-facing API/storage tests prove deleting/deleting_failed are invisible and active rows still work.

### CP-3 — PostgreSQL set-based physical delete

Files:
- `src/storage/postgres/repositories.py`
- `src/storage/sqlite/repositories.py`
- `tests/storage/test_postgres_*`

Work:
1. Implement PostgreSQL physical delete using `DELETE ... USING` or subquery deletes.
2. Avoid Python large ID materialization for production PostgreSQL delete path.
3. Verify or add indexes for mailbox delivery/interrupt answer/mailbox conversation paths identified in the design.
4. Keep SQLite tests compatible; SQLite may keep simpler implementation but should match behavior.
5. Return `deleted_counts` compatible with existing API.

Stop condition: repository tests prove full purge and compiled/observed PostgreSQL path is set-based.

### CP-4 — Deletion runner and disconnect-safe execution

Files:
- `src/api/runtime.py`
- `src/api/routes/conversations.py`
- `src/api/dto.py`
- `tests/api/`

Work:
1. Introduce runtime deletion task registry keyed by conversation id.
2. Use PostgreSQL row/advisory lock or atomic status transition as cross-process guard; process-local registry is only local dedupe.
3. Route awaits runner via `asyncio.shield` or equivalent so client/request cancellation does not cancel runner.
4. Runner phases:
   - mark/deleting metadata
   - cancel unfinished tasks
   - cancel local execution handles
   - bulk read artifact refs
   - idempotent file deletion
   - DB physical delete in short transaction
   - failure to `deleting_failed`
5. Preserve existing response core fields and add `delete_status`, `runner_id`, `started_at`, `finished_at`, `error_code`.

Stop condition: API tests prove success, failure, duplicate DELETE, client disconnect, and running-task cancellation semantics.

### CP-5 — Startup recovery and ops entrypoints

Files:
- `src/api/app.py` or runtime assembly lifecycle location
- `scripts/` for ops command if selected
- `docs/runbooks/postgresql-state-platform.md`
- tests under `tests/api/` / `tests/observability/`

Work:
1. On startup, scan `deleting` conversations and start runners.
2. Do not auto-expose or auto-restore `deleting_failed`.
3. Add minimum safe ops tooling:
   - list deleting/deleting_failed with sanitized phase/error metadata;
   - retry deleting_failed by re-entering runner.
4. Ensure tooling never prints DB password, API tokens, provider base_url, or full stack traces.

Stop condition: recovery tests and ops script tests pass.

### CP-6 — Frontend item-level deletion UX

Files:
- `frontend/src/App.tsx`
- `frontend/src/api/types.ts`
- `frontend/src/api/client.ts`
- `frontend/src/styles.css`
- `frontend/src/App.test.tsx`
- `frontend/src/api/client.test.ts`

Work:
1. Track deletion state per conversation id.
2. Show spinner on the target history item while DELETE is pending.
3. Disable only target item select/rename/delete.
4. Allow other conversations to remain usable.
5. If deleting current conversation, close subscription and move workspace to a new blank conversation while target item remains deleting until completion/history refresh.
6. Do not add frontend auto timeout.
7. Handle failures without resurrecting conversations that disappear from refreshed history.

Stop condition: frontend tests pass for target spinner, isolation, current conversation deletion, success, and failure behavior.

### CP-7 — API docs and runbook

Files:
- `docs/api/api-doc.html`
- `docs/runbooks/postgresql-state-platform.md`
- possibly `CHANGELOG.md` at end-of-day update.

Work:
1. Document DELETE response fields and semantics.
2. Explain that deletion continues after client disconnect.
3. Explain ordinary user invisibility for deleting/deleting_failed.
4. Document ops diagnosis/retry commands.

Stop condition: API docs tests pass and runbook has operational guidance.

### CP-8 — Verification and production smoke

Run targeted gates first:

```bash
conda run -n multi_agent python -m unittest tests.storage.test_sqlite_conversation_delete
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_postgres*.py'
conda run -n multi_agent python -m unittest tests.api.test_auth_login_and_isolation
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*conversation*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
```

Then run broader impacted gates if time permits:

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
```

Manual / smoke:

- Start fullstack.
- Create long history conversation.
- Delete it while using another conversation.
- Refresh during delete and confirm target is no longer ordinary-user visible.
- Confirm runner completion in logs and rows physically gone.

## 7. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Multi-worker double runner | Use PostgreSQL row/advisory lock and conditional status transitions; local task map only dedupes one process. |
| Long delete blocks other users | Do not use global runtime lock; keep DB transaction short; file I/O outside transaction. |
| File deleted but DB delete fails | Mark `deleting_failed`; retry treats missing files as idempotent success. |
| Client times out before response | Runner continues; ordinary list hides deleting/deleting_failed; ops metadata tracks progress. |
| Schema drift on live PostgreSQL | Add schema reconciler tests for additive columns/indexes without DROP. |
| Sensitive error leakage | Sanitize error summary and ops script output. |

## 8. ADR

### Decision

Implement strong conversation delete as a deletion-state + runtime deletion runner architecture with PostgreSQL set-based physical delete and active-only ordinary user visibility.

### Drivers

1. User selected physical delete completion semantics.
2. Long history deletes must survive client disconnects.
3. Deleted-intent failures must not resurrect ordinary user history.
4. Remote PostgreSQL latency requires set-based delete and minimal application round trips.

### Alternatives considered

- Pure long HTTP synchronous delete: rejected because client disconnect cannot reliably keep deletion running or track recovery.
- Immediate logical delete with background cleanup: rejected because it weakens the selected “success means physical delete complete” response semantics.
- In-memory-only deletion runner lock: rejected because production may run multiple workers/processes.

### Consequences

- Requires new statuses, schema metadata, recovery scan, and ops tooling.
- Ordinary user APIs become active-only by default.
- Implementation is larger than a DELETE SQL optimization but matches production reliability requirements.

### Follow-ups

- After implementation, consider whether a full admin UI is needed for deletion_failed diagnostics.
- After production observation, evaluate PostgreSQL delete duration and indexes with real long-history data.

## 9. Follow-up Staffing Guidance

Recommended execution path: **Team + Ultragoal**.

- Ultragoal owns durable checkpoints and final quality evidence.
- Team is useful because backend schema/storage, runtime runner, frontend UX, and docs/tests are parallelizable after CP-0/CP-1.

Suggested lanes:

1. Backend storage/schema lane — executor/test-engineer, medium/high reasoning.
2. Runtime/API runner lane — executor/debugger, high reasoning.
3. Frontend UX lane — executor/test-engineer, medium reasoning.
4. Verification/docs lane — verifier/writer, medium/high reasoning.

Ralph fallback: use only if the user wants a single persistent owner after the plan is approved rather than coordinated parallel work.
