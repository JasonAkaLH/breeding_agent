# SQLQuery Row Limit Soft Trim Design

Date: 2026-05-27
Status: Approved design direction; awaiting user review before implementation planning.

## Goal

SQLQuery should not fail a user task merely because a read-only query returns more rows than the configured database row safety limit. For row-count overflow only, the system should keep the newest allowed rows and continue the SQLQuery flow. The data passed into LLM summarization must still fit the configured token budget.

## Non-goals

- Do not remove the read-only SQL safety model.
- Do not soften write-operation denial, SQL guard failures, query timeout, column-count overflow, or result-size overflow.
- Do not expose technical limit names such as `db_row_limit` or `trim_max_tokens` in user-facing artifact text.
- Do not add SQL `LIMIT` as the primary solution; the user wants raw result cardinality preserved until the platform trims for processing.

## Current Behavior

The current SQLQuery path is:

1. SQLQuery generates SQL and passes SQL guard.
2. `SQLQuerySQLExecuteReadonlyCapability` calls `MySQLReadonlyAdapter.execute_readonly`.
3. The adapter fetches all rows and calls the data-access safety contract.
4. If `row_count > db_row_limit`, the safety layer raises `data_access_row_limit_exceeded`.
5. SQLQuery fails before `result_filtering` can apply token trimming.

This blocks broad but legitimate user requests such as finding varieties suitable for a region.

## Desired Behavior

For row-limit overflow only:

1. Execute the read-only query.
2. If the result has more rows than the configured row limit, keep the newest rows by taking the tail of the DB result: `rows[-row_limit:]`.
3. Continue SQLQuery with the trimmed rows.
4. Mark the payload internally as truncated.
5. Then apply the existing token-budget trim before LLM filtering/summarization.
6. If token trimming cannot include even the newest single row, return no candidate rows and a user-safe message asking the user to narrow the query.

All other safety failures remain hard failures.

## User-Facing Message Policy

User-facing text should be short and non-technical.

When row-limit soft trim and/or token trim happened:

> 查询结果较多，系统已自动截取可处理的最近结果用于分析。以下结论基于截取后的数据。

When the newest single row is too large to fit the LLM context:

> 查询结果内容过长，当前无法整理成可靠总结。请缩小查询范围后重试。

Technical details stay in internal metadata, audit events, and test assertions. They should not appear in artifact summaries or final answers unless explicitly requested for debugging.

## Recommended Architecture

### 1. Keep the global safety contract strict except row-limit overflow

The existing safety contract remains the source of configured limits. The design changes the adapter behavior for row-count overflow from hard fail to recoverable soft trim.

The adapter should still enforce:

- read-only SQL checks before execution;
- query deadline;
- column-count limit;
- result byte-size limit after any safe row trim;
- typed errors for non-row-limit violations.

### 2. Add row-limit trim metadata to read-only query results

Extend the read-only query result shape with internal metadata such as:

- `source_row_count`: original fetched row count;
- `row_limit_trimmed`: whether row soft trim happened;
- `row_limit`: configured retained-row cap;
- `row_limit_removed_row_count`: number of rows dropped from the head;
- `truncated`: true when any row soft trim happened.

The existing public SQLQuery output can expose only the safe fields needed by downstream code, while audit/debug metadata can retain the detailed counters.

### 3. Preserve newest rows by tail selection

The row soft trim policy is deterministic:

```text
if original_row_count > row_limit:
    rows = rows[-row_limit:]
else:
    rows = rows
```

This matches the user's requirement that bottom/latest data is prioritized.

### 4. Reuse SQLQuery token trim as the LLM-context gate

`SQLQueryResultFilteringCapability` already trims rows from the bottom by per-row token cost before building the LLM filtering prompt. Keep that responsibility there, but ensure it receives row-soft-trimmed data rather than failing earlier.

The token trim policy remains strict:

- Sum row token counts in bottom-to-top order.
- Keep only rows that fit under `trim_max_tokens`.
- If the newest row alone exceeds the budget, keep zero rows and return the user-safe narrow-query message.
- Do not allow token-budget overflow just to include at least one row.

### 5. Propagate truncation without technical user wording

SQLQuery payloads and artifacts should carry an internal `truncated` state so the final answer can add the short user-facing disclaimer. The final answer should not list raw counters by default.


## Frontend Failure Bubble Behavior

When a task still fails after the backend has produced a terminal failure state, the frontend must not leave the assistant placeholder bubble in a waiting state such as “正在等待回答...”.

Required behavior:

1. Stop the pending/waiting visual state as soon as the task reaches a terminal failure status.
2. Replace the waiting placeholder with a concise failure explanation.
3. Prefer user-safe backend failure messages when available.
4. If only a technical error code is available, map it to a natural Chinese message before showing it.
5. Keep detailed technical diagnostics in logs/state, not in the user-facing bubble by default.

For the SQLQuery row-overflow case, the preferred fix is still soft trimming so the task should complete. This frontend requirement covers remaining hard failures such as SQL guard rejection, DB timeout, non-row-limit safety errors, or unexpected execution errors.

Example user-facing failure copy:

> 查询没有完成：数据库查询超时了，请稍后重试或缩小查询范围。

> 查询没有完成：当前查询条件返回的内容过大，请缩小范围后重试。

## Data Flow

```text
SQL guard passed
  -> MySQLReadonlyAdapter executes read-only query
  -> row-count overflow? keep rows[-row_limit:] and mark internal truncation
  -> validate column count and byte size on retained result
  -> SQLQuery sql_execute_readonly returns retained rows + truncation state
  -> result_filtering applies token trim from newest rows backward
  -> LLM summarizes only retained rows that fit context
  -> final answer includes short non-technical truncation disclaimer when needed
```

## Error Handling

Hard failures remain:

- missing guard pass token;
- non-read-only SQL or write-like clauses;
- SQL guard rejection;
- DB timeout;
- transient DB failure after retry budget;
- column-count overflow;
- byte-size overflow after row soft trim;
- newest single row exceeds token budget and no safe LLM candidate can be built.

Recoverable condition:

- row-count overflow only, handled by tail-row soft trim.

## Testing Plan

Add or update tests before implementation:

1. `MySQLReadonlyAdapter` returns success when runner returns `db_row_limit + 1` rows, retaining the last `db_row_limit` rows and marking truncation metadata.
2. Existing tests for write denial, timeout, column limit, and byte-size limit still fail closed.
3. `SQLQuerySQLExecuteReadonlyCapability` no longer returns `data_access_row_limit_exceeded` for row overflow; it emits trimmed rows and truncation state.
4. `SQLQueryResultFilteringCapability` continues bottom-first token trim after row soft trim.
5. A newest-row-too-large token test returns zero candidate rows and the natural-language narrow-query message.
6. Artifact/user-facing summary tests assert natural wording and absence of technical names such as `db_row_limit` and `trim_max_tokens`.
7. Frontend reducer/component tests assert terminal task failures remove the waiting placeholder and show a concise failure reason.
8. Integration regression for the previously failing broad rice-for-Henan style query shape: SQLQuery should complete with truncated data instead of failing at `sql_execute_readonly`.

## Implementation Notes

- Keep changes small and localized around `src/integrations/mysql_readonly.py`, `skill/sql-query/runtime/sql_query_skill/sql_execute_readonly.py`, and `skill/sql-query/runtime/sql_query_skill/result_filtering.py`.
- Avoid changing the safety contract file unless needed to represent row-limit overflow as metadata instead of an exception.
- Preserve audit/debug detail but avoid leaking sensitive DB config or raw hidden diagnostics to user-facing messages.
- Update frontend task-event handling and assistant bubble rendering so terminal failures are visible instead of leaving a waiting placeholder.
- No new dependency is needed.

## Open Decisions

None. The user confirmed:

- row-limit overflow should be soft-trimmed;
- soft trim applies only to row-limit overflow;
- token overflow remains strict because data must fit the LLM context;
- user-facing artifact wording should be short and non-technical;
- terminal task failures should stop the waiting assistant bubble and show a concise failure reason.
