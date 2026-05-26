# Test Spec — Strong Conversation Delete

日期：2026-05-26
对应 PRD：`.omx/plans/prd-20260526-strong-conversation-delete.md`
设计来源：`docs/superpowers/specs/2026-05-26-strong-conversation-delete-design.md`

## 1. Test Objectives

Validate that conversation deletion is physically complete on successful response, continues after client disconnect, hides deleting/deleting_failed conversations from ordinary users, remains isolated to the target conversation, uses PostgreSQL-friendly set-based deletion for long histories, and gives front-end users item-level deletion feedback.

## 2. Unit / Storage Tests

### ST-1 Conversation status contract

Files:
- `tests/core/test_rust_contract_artifact.py`
- `tests/core/test_contracts.py`
- storage schema tests

Assertions:
- `ConversationStatus` includes `active`, `archived`, `locked`, `deleting`, `deleting_failed`.
- Python core contract and Rust core type contract agree.
- Conversation dataclass/row mapping preserves deletion metadata or tracking table state.

### ST-2 Schema reconciler additive migration

Assertions:
- Fresh PostgreSQL schema creates deletion metadata and required indexes.
- Existing schema gets additive columns/indexes without DROP TABLE/DROP DATABASE privileges.
- Drift detection accepts expected timestamp/index forms.

### ST-3 Active-only ordinary conversation list

Assertions:
- `list_conversations_for_username` returns active conversations only.
- Internal/admin storage method can still retrieve deleting/deleting_failed rows for runner/ops.
- Owner helpers used by ordinary routes treat deleting/deleting_failed as not found, including task-derived owner checks.

### ST-4 Mark deleting / failed transitions

Assertions:
- `mark_conversation_deleting` succeeds only for active owned conversation.
- Repeated mark for deleting conversation is idempotent or returns existing runner metadata.
- `mark_conversation_delete_failed` records phase, error code, sanitized summary, and timestamps.

### ST-5 Bulk artifact lookup

Assertions:
- `list_artifacts_for_conversation` returns all artifacts across tasks in one storage call.
- Artifact rows from other conversations are excluded.

### ST-6 PostgreSQL set-based physical delete

Assertions:
- PostgreSQL repository uses `DELETE ... USING` or subquery statements for child rows.
- Production PostgreSQL path does not materialize unbounded Python task/mailbox/interrupt ID lists.
- `deleted_counts` remains compatible with existing API response.
- After physical delete, all related business rows are gone while auth token rows remain.

### ST-7 Idempotent file deletion retry

Assertions:
- Missing artifact file is treated as success on retry.
- Partial file delete followed by DB failure can be retried to physical completion.

## 3. API / Runtime Tests

### API-1 Successful delete still purges history

Extend `tests/api/test_auth_login_and_isolation.py`:
- Submit message.
- Wait terminal.
- DELETE conversation.
- Assert success response contains old fields plus new status fields.
- Assert conversation/messages/task/events/artifacts are not visible and storage rows are physically gone.

### API-2 Running task is cancelled before purge

Assertions:
- Start blocking task.
- DELETE conversation.
- Release blocker if needed.
- Response includes cancelled task id.
- Task rows/events/messages are gone after success.

### API-3 Deleting state hides ordinary conversation routes

Assertions during controlled paused runner:
- `GET /api/v1/conversations` excludes target.
- `GET /api/v1/conversations/{id}/messages` returns 404.
- submit chat message returns 404 or equivalent ordinary not-found.
- rename route returns 404.

### API-3b Deleting state hides task-derived routes

Assertions during controlled paused runner for an existing task under the deleting conversation:
- `GET /api/v1/tasks/{task_id}` returns 404.
- `GET /api/v1/tasks/{task_id}/events` refuses subscription before streaming.
- `POST /api/v1/tasks/cancel` returns 404 for ordinary user.
- interrupts list/answer, graph, task artifacts, and artifact download routes return 404.

### API-3c Deleting state hides upload routes

Assertions during controlled paused runner:
- list conversation uploads returns 404.
- upload to existing deleting conversation returns 404.
- delete upload for deleting conversation returns 404 or equivalent not-found.
- upload behavior for a new not-yet-created local conversation id remains compatible with current UX if no existing conversation row is present.

### API-4 Client disconnect does not cancel runner

Setup:
- Inject runner pause after marking deleting.
- Start DELETE request task and cancel the client-side await.

Assertions:
- Runtime runner remains alive or resumes.
- After releasing pause, physical delete completes.
- Conversation never becomes visible again.

### API-5 Duplicate DELETE does not duplicate runner

Assertions:
- Two DELETE calls for same conversation while first is in progress share one runner or second waits.
- Only one physical delete path executes.
- Both callers either get compatible completion result or one receives stable not-found after completion.

### API-6 Failure goes to deleting_failed and stays hidden

Scenarios:
- Artifact file deletion raises non-idempotent error.
- PostgreSQL physical delete raises injected error.

Assertions:
- Status becomes `deleting_failed`.
- Ordinary list/messages return hidden/not found.
- Error summary is sanitized.
- Ops diagnostic can see phase/error code.

### API-7 Startup recovery

Setup:
- Seed a `deleting` conversation with metadata.
- Build runtime/app.

Assertions:
- Startup scan starts runner.
- Runner completes physical delete or marks deleting_failed on injected failure.

### API-7b Shutdown after marking deleting is recoverable

Setup:
- Mark conversation deleting and pause runner before DB delete.
- Simulate runtime shutdown / task cancellation.
- Build a new runtime with the same storage.

Assertions:
- Startup recovery re-enters runner.
- Missing files from any previous partial file cleanup are accepted as idempotent success.
- Final state is physical delete or deleting_failed, never ordinary active visibility.

### API-8 Other conversation remains usable

During paused long delete:
- List messages for another conversation succeeds.
- Submit message to another conversation succeeds.
- SSE events for another task continue.
- Existing SSE for the deleting conversation terminates through normal task terminal event when deletion cancels a running task; new SSE subscribe is rejected by API-3b.

## 4. Frontend Tests

### FE-1 Target item spinner

File: `frontend/src/App.test.tsx`

Assertions:
- Click delete on one history item.
- Confirm dialog accepted.
- Target item shows spinner or accessible pending indicator.
- Target rename/delete/select are disabled.

### FE-2 Other items remain usable

Assertions:
- While target delete promise is pending, another history item can be selected.
- Sending in another active conversation remains enabled when no task is active.

### FE-3 Current conversation delete

Assertions:
- Deleting current conversation closes subscription.
- Workspace moves to new blank conversation.
- Target item remains pending until DELETE success or history refresh removes it.

### FE-4 Success removes item

Assertions:
- Resolve DELETE promise with success.
- Target item disappears.
- Success notice uses existing cancelled/non-cancelled copy.

### FE-5 Failure and history refresh behavior

Assertions:
- If DELETE rejects and refreshed history still includes item, pending state clears and error appears.
- If refreshed history no longer includes item, frontend does not resurrect the item.

### FE-6 No frontend auto timeout

Assertions:
- With fake timers advanced far beyond 60 seconds, pending item remains pending if DELETE promise has not settled.

## 5. Ops / Observability Tests

### OPS-1 Diagnostic command is sanitized

Assertions:
- Lists deleting/deleting_failed rows with runner id, phase, error code, timestamps.
- Does not print DB password, bearer token, provider base_url, or raw stack traces.

### OPS-2 Retry command re-enters runner

Assertions:
- Seed deleting_failed.
- Run retry command with conversation id.
- Conversation transitions to deleting and runner completes or records new failure.

## 6. Docs Tests

Update `tests/api/test_developer_docs.py` or equivalent to assert `docs/api/api-doc.html` contains:

- DELETE `/api/v1/conversations` response fields.
- Deletion continues after client disconnect.
- `deleting` / `deleting_failed` ordinary-user invisibility.
- No frontend/user-side timeout promise.
- Ops/runbook reference for failed deletion recovery.

## 7. Verification Commands

Targeted commands:

```bash
conda run -n multi_agent python -m unittest tests.storage.test_sqlite_conversation_delete
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_postgres*.py'
conda run -n multi_agent python -m unittest tests.api.test_auth_login_and_isolation
conda run -n multi_agent python -m unittest tests.api.test_task_cancel
conda run -n multi_agent python -m unittest tests.api.test_task_events_sse
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*conversation*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
```

Broader impacted commands:

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

Manual smoke:

1. Start fullstack against configured PostgreSQL.
2. Create a long-history conversation.
3. Delete it and confirm target item spinner.
4. Use another conversation during deletion.
5. Refresh during deletion and confirm target is absent from ordinary list.
6. Confirm backend logs/ops diagnostic show runner phase/completion.
7. Confirm rows are physically gone after completion.

## 8. Exit Criteria

- All targeted tests pass.
- No ordinary conversation, task-derived, artifact, upload, cancel, interrupt, or SSE route exposes deleting/deleting_failed conversation.
- Runner survives client disconnect and startup recovery.
- PostgreSQL physical delete path is set-based.
- Frontend item-level pending UX is verified.
- Docs/runbook describe production behavior.
- License Requirement documented in final implementation report.
