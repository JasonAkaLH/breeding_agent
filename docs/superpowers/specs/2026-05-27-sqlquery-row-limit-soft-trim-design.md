# SQLQuery Row Limit Soft Trim and Failure Bubble PRD

Date: 2026-05-27
Status: Reviewed and hardened with repo evidence; awaiting user review before implementation planning.

## Problem Statement

Broad SQLQuery requests can currently fail before the system has a chance to trim data for LLM summarization. The immediate failure mode is `data_access_row_limit_exceeded`: `MySQLReadonlyAdapter` fetches rows, validates the full row count against the safety contract, and SQLQuery stops before `result_filtering` applies token-budget trimming.

Separately, when a task reaches a terminal failure state, the frontend can still present a generic assistant waiting/failure placeholder instead of a useful failure reason. Users should not see an assistant bubble that appears to keep waiting after the backend has already failed the task.

## Goals

1. Row-count overflow must be recoverable for SQLQuery: keep the newest/tail rows within the configured row safety cap and continue the SQLQuery pipeline.
2. Token-budget overflow must remain strict: only rows that fit the configured LLM context budget may be sent to LLM filtering or summarization.
3. Terminal task failures must stop the waiting assistant bubble and show a concise, user-safe failure reason.
4. User-facing truncation/failure messages must be natural Chinese and must not expose technical limit names by default.

## Non-goals

- Do not weaken read-only SQL enforcement, SQL guard rejection, DB timeout handling, column-count limits, or retained-result byte-size limits.
- Do not add SQL `LIMIT` as the primary solution; trimming should happen after data access according to platform policy.
- Do not expose `db_row_limit`, `trim_max_tokens`, raw SQL diagnostics, DSNs, provider config, or stack traces in user-facing bubbles/artifacts.
- Do not guarantee semantic “newest by timestamp” unless the generated SQL has an explicit ordering. This PRD defines newest as the tail of the DB result order available to the adapter.
- Do not introduce new dependencies.

## Users, Stakeholders, and Affected Systems

| Area | Impact |
| --- | --- |
| End users | Broad SQLQuery requests continue with a clear “截取后分析” notice instead of failing on row count alone. |
| Main agent / final answer | Receives bounded SQLQuery context that can fit the LLM summarization path. |
| SQLQuery Skill | Continues after row-count overflow and still performs result filtering/token trimming. |
| Data access safety layer | Keeps hard safety boundaries except row-count overflow becomes a soft trim condition for retained rows. |
| Frontend conversation UI | Terminal failures clear pending/waiting state and display a concise failure reason. |
| Observability/debugging | Internal counters and typed failure metadata remain available for audit/tests without leaking to users. |

## Current State and Evidence

| Evidence | Observation |
| --- | --- |
| `src/integrations/mysql_readonly.py` | `execute_readonly()` calls `_ensure_result_limits(result)` after execution. `_ensure_result_limits()` sends `row_count`, `column_count`, and `result_bytes` to the safety contract. |
| `src/integrations/rust_safety_contract.py` | `_validate_data_access_shape_py()` raises `DataAccessContractError(code="data_access_row_limit_exceeded")` when `row_count > resource_limit("db_row_limit")`. |
| `skill/sql-query/runtime/sql_query_skill/sql_execute_readonly.py` | Converts `DataAccessContractError` into `CapabilityExecutionError`, causing the SQLQuery stage to fail. |
| `skill/sql-query/runtime/sql_query_skill/result_filtering.py` | `_trim_rows_for_llm()` already keeps rows bottom-first by token budget, but it only runs after SQL execution succeeds. |
| `skill/sql-query/tests/test_sql_execute_readonly.py` | Current test expects `data_access_row_limit_exceeded` for 501 rows, so tests must be updated for the new row-limit soft-trim behavior. |
| `frontend/src/domain/taskEvents.ts` | Reducer records `errorMessage` for `node.failed` / `task.failed`, including mappings for guard and transient DB errors. |
| `frontend/src/App.tsx` | `assistantActivityText()` and event handling render progress/status text; the assistant bubble currently uses generic status text rather than guaranteed detailed `errorMessage`. |
| `frontend/src/App.test.tsx` | Existing failed-task test checks red icon and no spinner, but only asserts generic `本次任务未完成`; it does not verify that a failure reason is displayed. |

## Proposed Solution

### A. Row-limit overflow becomes a soft trim condition

For SQLQuery read-only results only, row-count overflow must not terminate the task. The data access path must retain the tail rows within the configured row cap and return success with truncation metadata.

Required retained-row policy:

```text
retained_rows = original_rows[-row_limit:]
```

For implementation safety, the preferred adapter shape is a bounded tail buffer rather than materializing an unbounded full result only to trim later. The adapter should count total rows while retaining only the last `row_limit` rows whenever row soft trim is enabled.

### B. Hard safety checks still apply after row soft trim

After retaining the tail rows, the adapter must still validate:

- SQL is read-only and has a guard pass token;
- deadline/timeout;
- column-count limit;
- retained-result byte-size limit;
- transient DB retry budget.

Only row-count overflow changes from hard failure to recoverable soft trim.

### C. Token trimming remains the LLM-context gate

SQLQuery `result_filtering` must continue bottom-first token trimming before constructing LLM prompts:

- Compute per-row token cost using the existing token counter path.
- Iterate from bottom/newest row to top/older row.
- Keep only rows that fit within `trim_max_tokens`.
- If the newest single row does not fit, keep zero rows and return a user-safe “请缩小查询范围” message.
- Never allow LLM prompt input to exceed the token budget just to include at least one row.

### D. User-facing messages are concise and non-technical

When row soft trim and/or token trim happened:

> 查询结果较多，系统已自动截取可处理的最近结果用于分析。以下结论基于截取后的数据。

When the newest single row is too large for LLM context:

> 查询结果内容过长，当前无法整理成可靠总结。请缩小查询范围后重试。

When a task still fails for another reason, the assistant bubble should use a concise mapped reason, for example:

> 查询没有完成：数据库查询超时了，请稍后重试或缩小查询范围。

> 查询没有完成：当前查询不符合只读查询安全边界，请改用查询类问题。

### E. Frontend terminal failure visibility

When the backend emits or reports a terminal failure:

1. The assistant bubble must stop showing waiting/pending UI such as “正在等待回答...”.
2. The bubble must display a failure notice with a red failure state.
3. The notice text must prefer `TaskEventState.errorMessage` from `node.failed` / `task.failed` mappings.
4. A later generic `task.failed` event must not overwrite a more specific node failure message.
5. If the SSE stream breaks and the task status is recovered from `/tasks/{task_id}` without detailed failure metadata, a generic user-safe failure message is acceptable.

## Functional Requirements

| ID | Requirement |
| --- | --- |
| FR-1 | SQLQuery read-only execution must continue when row count exceeds the configured row cap by retaining the tail rows. |
| FR-2 | Row soft trim must set internal truncation metadata: original/source row count, retained row count, removed row count, row soft-trim flag, and `truncated=true`. |
| FR-3 | SQLQuery output/artifacts must carry enough internal metadata for final-answer disclaimer and debugging, while user-facing summaries stay non-technical. |
| FR-4 | Token trimming must run after row soft trim and must enforce `trim_max_tokens` strictly. |
| FR-5 | If newest single row exceeds the token budget, SQLQuery must not send it to the LLM and must produce a user-safe narrow-query message. |
| FR-6 | Non-row-limit safety violations must remain hard failures. |
| FR-7 | Frontend failed task bubbles must render the mapped failure reason rather than a spinner or indefinite waiting placeholder. |
| FR-8 | Frontend must preserve the more specific node failure reason when a later generic task failure event arrives. |

## Non-functional Requirements

| Area | Requirement |
| --- | --- |
| Reliability | Row soft trim must be deterministic and idempotent for the same DB result order. |
| Safety | SQL write denial, guard failures, timeout, column overflow, and retained-result byte overflow must remain fail-closed. |
| Performance | The adapter should avoid unbounded in-memory accumulation for oversized result sets when retaining only tail rows is sufficient. |
| Privacy/Security | User-facing messages must not include raw SQL, DSN/provider config, stack traces, or hidden diagnostics. |
| Observability | Internal audit/test metadata must preserve enough detail to explain whether row soft trim or token trim happened. |
| Compatibility | Existing SQLQuery progress events, artifact rendering, and task lifecycle events should keep their current contracts unless explicitly extended. |

## Data Flow

```text
SQL guard passed
  -> MySQLReadonlyAdapter executes read-only query
  -> row-count overflow? retain tail rows and mark internal truncation
  -> validate column count and retained-result byte size
  -> SQLQuery sql_execute_readonly returns retained rows + truncation metadata
  -> result_filtering applies strict token trim from newest rows backward
  -> LLM receives only rows that fit context
  -> final answer/artifact uses concise non-technical truncation notice when needed
```

Failure UI flow:

```text
node.failed/task.failed event or recovered failed task status
  -> reducer enters failed phase and computes user-safe errorMessage
  -> App updates assistant bubble activityText from errorMessage when available
  -> bubble shows red failed notice, no spinner, no waiting placeholder
```

## Edge Cases and Failure Modes

| Case | Expected behavior |
| --- | --- |
| Row count exceeds cap | Retain tail rows, continue, mark truncated internally. |
| Result has exactly row cap rows | No row soft trim; continue normally. |
| Zero rows | Continue normally; final answer states no matching data if applicable. |
| Newest row alone exceeds token budget | Do not call LLM with that row; return user-safe narrow-query message. |
| Column count exceeds limit | Hard fail. |
| Retained result bytes exceed limit | Hard fail. |
| DB timeout | Hard fail with mapped user-facing timeout reason. |
| SQL guard/write pattern failure | Hard fail with read-only safety boundary message. |
| SSE fails after backend already failed | Poll task status; show generic failure if no detailed event payload is available. |
| `task.failed` follows `node.failed` | Keep the more specific node error message. |

## Acceptance Criteria

| ID | Verification |
| --- | --- |
| AC-1 | `MySQLReadonlyAdapter` test with `row_limit + 1` rows returns success, retained rows equal the last `row_limit` rows, and truncation metadata is set. |
| AC-2 | Existing write denial, timeout, column limit, and retained-result byte-limit tests still fail closed. |
| AC-3 | `SQLQuerySQLExecuteReadonlyCapability` no longer returns `data_access_row_limit_exceeded` for row overflow; it returns retained rows and truncation metadata. |
| AC-4 | `SQLQueryResultFilteringCapability` test proves token trim still keeps newest rows first after row soft trim. |
| AC-5 | Token test proves newest-row-too-large produces zero LLM candidate rows and the natural narrow-query message. |
| AC-6 | Artifact/final-answer tests prove user-visible truncation copy is natural Chinese and does not include `db_row_limit`, `trim_max_tokens`, raw counters, DSN, or stack traces. |
| AC-7 | Frontend domain reducer tests prove failure codes map to user-safe Chinese messages and later generic `task.failed` does not overwrite a specific node failure. |
| AC-8 | Frontend component test proves a failed task bubble shows the failure reason with red failed state and no spinner / no “正在等待回答...”. |
| AC-9 | Integration regression for the broad rice-for-Henan query shape completes SQLQuery with truncated data instead of failing at `sql_execute_readonly`. |

## Test and Rollout Plan

1. Write failing tests first for adapter row soft trim, SQLQuery execution behavior, token overflow strictness, and frontend failure bubble copy.
2. Implement backend row soft trim and SQLQuery metadata propagation.
3. Implement frontend failure bubble rendering from `errorMessage`.
4. Run targeted tests:
   - `conda run -n multi_agent python -m unittest tests/integrations/test_mysql_readonly_adapter.py`
   - `cd skill/sql-query && conda run -n multi_agent python -m unittest discover -s tests -p 'test_*.py'`
   - `cd frontend && npm test -- --run`
5. Run broader affected suites if targeted tests pass:
   - `conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'`
   - `conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'`

No feature flag is required for local development, but rollback is straightforward: restore row-limit overflow to the previous `DataAccessContractError` hard failure and revert frontend rendering changes.

## Dependencies and Integration Points

- `src/integrations/mysql_readonly.py`
- `src/integrations/rust_safety_contract.py`
- `src/integrations/rust_contracts/safety_contract.json`
- `skill/sql-query/runtime/sql_query_skill/sql_execute_readonly.py`
- `skill/sql-query/runtime/sql_query_skill/result_filtering.py`
- `frontend/src/domain/taskEvents.ts`
- `frontend/src/App.tsx`
- Existing token counter integration used by SQLQuery result filtering

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Tail rows are not semantically newest without `ORDER BY` | Document assumption; future SQL generation can add explicit ordering when schema supports a business timestamp. |
| Unbounded row materialization creates memory pressure | Prefer streaming/tail-buffer retention in adapter implementation. |
| User mistakes truncated result for complete result | Always include concise truncation disclaimer when row or token trim happened. |
| Failure bubble leaks technical diagnostics | Use mapped user-safe messages; keep diagnostics in audit/logs. |
| Softening row limit accidentally weakens other safety checks | Tests must prove non-row-limit violations still fail closed. |

## Assumptions and Open Questions

Assumptions:

- “最新/底部数据” means the tail of the DB result order available to the adapter.
- SQLQuery is the affected user-facing path; other data-access callers should only receive row soft trim if they explicitly use the same adapter behavior and accept the truncation metadata contract.
- Generic failure copy is acceptable when a recovered failed task status has no detailed failure payload.

Open questions: none currently blocking implementation planning.
