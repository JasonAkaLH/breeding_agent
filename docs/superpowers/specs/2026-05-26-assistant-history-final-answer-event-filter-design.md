# Assistant History Final Answer Event Filter Design

日期：2026-05-26
状态：Reviewed / implementation-ready

## 1. Problem statement

Deep-thinking requests can produce hundreds of `main_agent.reasoning_delta` events from the planner and the final answer node. The current assistant history sync path reads the full task event log before choosing the final text artifact. In the observed local failure, the task had already produced a final text artifact plus `main_agent.output_final`, but post-completion sync hit `event_log_replay_page_exceeded`, did not write `task_id:assistant` to `message`, and then marked the task failed. After refresh or re-login, the frontend loads only historical messages, so the final answer disappears.

## 2. Goals and non-goals

### Goals

1. After a completed deep-thinking task, refresh / re-login must show the final answer content.
2. Assistant history sync must not read all task events solely to recover the final answer.
3. Large volumes of `main_agent.reasoning_delta` must not affect final answer history recovery.
4. Post-completion assistant history sync failure must not reverse a completed task to failed.
5. The fix must preserve the current message / artifact storage model.

### Non-goals

1. Do not persist deep-thinking reasoning content.
2. Do not return reasoning content from historical message APIs.
3. Do not restore the “思考内容” card after refresh / re-login.
4. Do not add an `assistant_response` table.
5. Do not change realtime SSE behavior; realtime reasoning can still display from in-memory `reasoning_delta` events.
6. Do not start PostgreSQL migration work in this fix.

## 3. Users, stakeholders, and affected systems

| Actor / system | Impact |
| --- | --- |
| End user | Sees final answers after refresh / re-login even when deep thinking was enabled. Does not see historical reasoning. |
| Frontend chat UI | Continues to render `MessageResponse.content`; no reasoning restore contract is added. |
| API runtime | Must isolate post-completion history sync failure from task terminal status. |
| Storage layer | Must provide bounded event filtering without full event replay. |
| Conversation memory | Should reuse bounded final-marker selection when falling back from messages to artifacts, so old broken tasks do not re-trigger full event replay. |
| RuntimeSidecar contract | Existing event replay page limits remain respected; the fix avoids unnecessary full replay. |

## 4. Current state and evidence

| Evidence | Current behavior |
| --- | --- |
| `src/capabilities/main_agent/executor.py` | Final answers are emitted as text artifacts and `main_agent.output_final`; reasoning is emitted as `main_agent.reasoning_delta`. |
| `src/api/runtime.py::_persist_assistant_history_message()` | Reads `storage.list_events_for_task(task_id)` before selecting final text artifact. |
| `src/api/runtime.py::_run_execution()` | Wraps execution and post-completion history sync in one `try`; a sync exception reaches `_mark_task_failed()`. |
| `src/storage/sqlite/repositories.py::list_events_for_task()` | Applies RuntimeSidecar replay limits to full event replay and raises `event_log_replay_page_exceeded` when too many events are returned. |
| `src/api/routes/conversations.py::list_conversation_messages()` | Calls `sync_assistant_history_messages()` then returns rows from `message`. |
| `frontend/src/App.tsx::messageFromHistory()` | Restores only historical `message.content` and completion flags. |
| Local SQLite observation | Failed deep-thinking task had final text artifact and `main_agent.output_final`, but no `task_id:assistant` message; task later recorded `execution_crash: event_log_replay_page_exceeded`. |

## 5. Proposed solution

### 5.1 Add bounded filtered event reads

Add a read-only storage API that filters at the database query level rather than loading all task events and filtering in Python.

```python
async def list_events_for_task_filtered(
    task_id: str,
    *,
    event_types: set[str] | None = None,
    node_id: str | None = None,
    visibility: EventVisibility | None = None,
    limit: int | None = None,
) -> list[EventRecord]: ...
```

Implementation requirements:

- Add the method to `StoragePort` and concrete SQLite storage classes.
- Import / reference `EventVisibility` in the contract where needed.
- SQLite must translate filters to SQL `WHERE` clauses:
  - `task_id = :task_id`
  - `event_type IN (...)` when provided
  - `node_id = :node_id` when provided
  - `visibility = :visibility` when provided
  - `ORDER BY created_at, event_id`
  - `LIMIT :limit` when provided
- The method must remain bounded by RuntimeSidecar replay policy. If no explicit limit is provided, use the contract page limit. If a caller asks for more than the page limit, fail with the existing replay limit error.
- The method must validate the filtered result against event count / payload byte limits after filtering.
- The method must not call `list_events_for_task()` internally.

### 5.2 Use final-marker reads for assistant history sync

Change assistant history sync from full event replay:

```python
events = await self.storage.list_events_for_task(task_id)
```

to bounded final-marker reads:

```python
final_events = await self.storage.list_events_for_task_filtered(
    task_id,
    event_types={"main_agent.output_final"},
    visibility=EventVisibility.FRONTEND,
    limit=32,
)
```

Then keep the existing artifact selection helper:

```python
text_artifact = select_final_text_artifact(artifacts, events=final_events)
```

Rationale:

- `select_final_text_artifact()` already prefers artifact IDs containing `:main_agent_response:final`.
- For roleless historical artifacts, the final event’s node ID is enough to select the final answer.
- `main_agent.reasoning_delta` events are irrelevant to final answer selection and must not be read by this path.

### 5.3 Apply the same bounded final-marker fallback to conversation memory

`src/orchestration/conversation_memory.py::_final_text_artifact()` also calls `list_events_for_task()` before `select_final_text_artifact()`. That path can re-trigger the same failure for old tasks that lack assistant messages.

Requirement:

- If storage provides `list_events_for_task_filtered()`, conversation memory must use it with `event_types={"main_agent.output_final"}` and `visibility=EventVisibility.FRONTEND`.
- If the filtered method is unavailable in a test fake, it may fall back to `events=()` rather than full event replay. It must not use full event replay solely to select final answer artifacts.

### 5.4 Isolate post-completion history sync failures

Move assistant history sync into a post-completion guard. Once `execute_request()` reports `completion_status == completed`, history sync failure must not call `_mark_task_failed()`.

Required behavior:

```text
execute_request completed
  -> handle pending skill context
  -> try sync assistant history message
      success: no extra event
      failure: audit-only assistant_history_sync.failed
  -> task remains completed
```

`assistant_history_sync.failed` must be audit-only and sanitized. Payload may include:

- `task_id`
- `conversation_id`
- `history_message_id`
- stable `code`
- short `diagnostic`

Payload must not include answer text, reasoning text, prompt, raw event payloads, API keys, provider base URLs, or database credentials.

## 6. Functional requirements

| ID | Requirement |
| --- | --- |
| FR-1 | Storage must expose a bounded filtered event read API. |
| FR-2 | Assistant history sync must use filtered final-marker reads, not full task event replay. |
| FR-3 | Conversation memory final-text fallback must not full-replay task events solely to select final text artifacts. |
| FR-4 | Completed tasks must remain completed if assistant history sync fails after execution completion. |
| FR-5 | Historical message API must continue returning final answer via `message.content`. |
| FR-6 | Historical message API must not return reasoning content. |
| FR-7 | Existing realtime SSE reasoning and answer streaming must keep working. |

## 7. Non-functional requirements

| Area | Requirement |
| --- | --- |
| Reliability | Deep-thinking event volume must not prevent final answer history recovery. |
| Performance | Final answer history sync must read O(number of final markers), not O(total task events). |
| Security / privacy | No reasoning text, prompts, secrets, raw provider URLs, or raw event payloads may be added to historical message payloads or sync failure diagnostics. |
| Compatibility | Existing `message`, `artifact`, event schemas and frontend `MessageResponse` contract remain compatible. |
| Observability | Sync failures after task completion must be visible as sanitized audit-only events. |
| Accessibility / UX | No frontend visual behavior changes are required for historical reasoning; final answer visibility after refresh is the user-facing success criterion. |

## 8. Edge cases and failure modes

| Case | Required behavior |
| --- | --- |
| Many reasoning events, one final answer | Assistant message is written from final text artifact; task remains completed. |
| Multiple text artifacts | Prefer artifact ID role marker `final`; otherwise use filtered `main_agent.output_final` node ID; otherwise first non-empty text artifact as current helper does. |
| No final text artifact | No assistant message is written; task status is not changed by history sync. |
| Duplicate history sync race | Existing idempotency remains: if `task_id:assistant` exists after a save conflict, treat as success. |
| Filtered event read itself fails | Record sanitized audit-only sync failure; do not emit frontend `task.failed`; do not change completed task status. |
| Old tasks already marked failed by the old bug | This fix does not rewrite old task status automatically. If the user opens a conversation containing an already-written assistant message, it displays as before. |
| Test fakes missing new method | Production paths must implement the method; tests may use explicit fakes or fall back to no events where final artifact ID role markers are sufficient. |

## 9. Acceptance criteria

| ID | Acceptance criterion | Verification |
| --- | --- | --- |
| AC-1 | A completed deep-thinking task with large reasoning event volume writes `task_id:assistant`. | API/runtime test with many reasoning events. |
| AC-2 | `/api/v1/conversations/{conversation_id}/messages` returns the final answer content after sync. | API test. |
| AC-3 | No historical API response contains reasoning content introduced by this fix. | DTO/API assertions. |
| AC-4 | If post-completion history sync raises, task remains completed and no frontend `task.failed` event is added. | Runtime failure-isolation test. |
| AC-5 | Filtered storage reads return only matching event types and do not call full replay. | Storage unit test / fake sentinel. |
| AC-6 | Conversation memory final-text fallback no longer requires full event replay. | Conversation memory or storage fake test. |
| AC-7 | Existing frontend history rendering still works with `message.content`. | Existing App history tests continue passing. |

## 10. Test plan

### Storage tests

1. `list_events_for_task_filtered(event_types={"main_agent.output_final"})` returns only final events for the task.
2. Filtering by `visibility` and `node_id` works.
3. Ordering is deterministic by `created_at, event_id`.
4. `limit` is honored and validated against replay policy.
5. A sentinel test proves the filtered method does not delegate to `list_events_for_task()`.

### Runtime / API tests

1. Build a completed task with:
   - many `main_agent.reasoning_delta` events,
   - a final text artifact,
   - a `main_agent.output_final` event.
2. Configure all-event replay to fail if used.
3. Call `sync_assistant_history_message_for_task()` and/or `GET /conversations/{id}/messages`.
4. Assert:
   - `task_id:assistant` exists,
   - content equals final answer,
   - task remains completed,
   - full event replay was not used.

### Failure isolation tests

1. Force `_persist_assistant_history_message()` or the filtered event read to raise after task completion.
2. Assert:
   - task remains completed,
   - `assistant_history_sync.failed` audit-only event exists,
   - no frontend `task.failed` event is emitted due to post-completion sync failure.

### Conversation memory tests

1. Create a task with many reasoning events and a final artifact.
2. Make full event replay fail.
3. Assert conversation memory can select the final artifact or safely omit the fallback without crashing.

### Frontend tests

No new reasoning-history test is required. Existing history rendering tests should continue to pass because `MessageResponse.content` remains the only historical answer source.

## 11. Rollout and migration

- No schema migration is required.
- No frontend API contract migration is required.
- Existing completed tasks with already-written assistant messages remain readable.
- Existing tasks already marked failed by the old post-completion bug are not automatically repaired by this change; any backfill / repair tool would be a separate operational task.

## 12. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| A roleless final artifact relies on final event node ID. | Read bounded `main_agent.output_final` events and keep existing selector behavior. |
| A future task emits more than 32 final events. | `limit=32` is intentionally above expected cardinality; exceeding it should fail sync audit-only rather than task status. |
| Another subsystem still uses full event replay for final artifact selection. | Include conversation memory in scope and search for remaining `select_final_text_artifact(... events=list_events_for_task(...))` call sites during implementation. |
| Sync failure diagnostics could leak data. | Require sanitized audit-only payload and explicitly forbid answer/reasoning/prompt/raw event payloads. |
| Implementers accidentally restore reasoning history. | Non-goals, FR-6, AC-3, and frontend test guidance explicitly prohibit reasoning persistence / historical return. |

## 13. Dependencies

- `src.core.contracts.StoragePort`
- `src.storage.sqlite.repositories.SQLiteStorage` and sync state repository implementation
- `src.api.runtime` assistant history sync and post-completion execution handling
- `src.orchestration.answer_selection.select_final_text_artifact()`
- `src.orchestration.conversation_memory` final text artifact fallback
- Existing `EventVisibility` and RuntimeSidecar event replay policy helpers

## 14. Open questions

None. The user explicitly selected B and explicitly rejected reasoning persistence for this fix.

## 15. License requirement

No dependency, Rust crate, Cargo lockfile, or license policy change is expected. Final implementation notes must still state: “License Requirement：无依赖/许可变更，未触发 cargo-deny 风险” unless implementation scope changes.
