# Assistant History Final Answer Event Filter Design

日期：2026-05-26
状态：Draft for user review

## 背景

深度思考开启后，主代理和 planner 会产生大量 `main_agent.reasoning_delta` 事件。当前 assistant 历史消息同步会读取任务的全量事件，再调用 `select_final_text_artifact()` 选择最终回答 artifact。当事件数量超过 RuntimeSidecar 的 event replay page limit 时，同步阶段会抛出 `event_log_replay_page_exceeded`。

实测问题表现是：任务已生成最终回答 text artifact，并出现 `main_agent.output_final` 与 `task.completed`，但随后 assistant 历史消息同步失败，导致 `task_id:assistant` 没有写入 `message` 表。刷新网页或重新登录后，前端只读取历史 messages，因此最终回答不显示。

## 用户确认的边界

本设计采用 B 方案：保留现有 message / artifact 模型，增加受控事件过滤读取。明确不采用新增 `assistant_response` 表的 C 方案。

本轮只修复最终 answer content 的历史恢复。

### 明确要做

1. 深度思考任务完成后，刷新网页 / 重新登录仍能看到最终 answer content。
2. assistant 历史消息同步只读取选择最终 answer 所需的最小事件集合。
3. 大量 `main_agent.reasoning_delta` 不能触发历史同步失败。
4. 已完成任务不能因为完成后的 assistant 历史同步失败而被反转成 failed。

### 明确不做

1. 不持久化深度思考内容。
2. 不在历史消息 API 返回 reasoning 内容。
3. 不从历史恢复“思考内容”卡片。
4. 不新增 `assistant_response` 表。
5. 不改变实时 SSE 展示：实时阶段仍可显示 reasoning 气泡与 answer。

## 当前证据

- `src/capabilities/main_agent/executor.py` 会把最终回答写成 text artifact，并发出 `main_agent.output_final`。
- `src/api/runtime.py` 的 `_persist_assistant_history_message()` 当前通过 `storage.list_events_for_task(task_id)` 全量读取事件，再调用 `select_final_text_artifact(artifacts, events=events)`。
- `src/api/routes/conversations.py` 的历史消息接口会调用 `sync_assistant_history_messages()`，然后返回 message 表内容。
- `frontend/src/App.tsx` 的历史恢复只通过 `listConversationMessages()` 得到 `MessageResponse`，再用 `messageFromHistory()` 恢复 assistant 消息。
- 本地运行数据中，失败任务已有 final text artifact 和 `main_agent.output_final`，但缺少 `task_id:assistant` message，且 task 后续被记录为 `execution_crash: event_log_replay_page_exceeded`。

## 设计

### 1. Storage 增加过滤事件读取接口

在 storage contract 增加只读接口，语义为数据库侧过滤，不允许先全量读取再 Python 过滤：

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

SQLite 实现使用 SQL `WHERE` 条件过滤：

- `task_id = :task_id`
- `event_type IN (...)` when provided
- `node_id = :node_id` when provided
- `visibility = :visibility` when provided
- `ORDER BY created_at, event_id`
- `LIMIT :limit` when provided

该接口用于历史同步等 bounded 查询。原 `list_events_for_task()` 保持兼容，不扩大使用面。

### 2. Assistant 历史同步只读取 final marker

将 `_persist_assistant_history_message()` 从全量事件读取：

```python
events = await self.storage.list_events_for_task(task_id)
```

改为只读取 final marker：

```python
final_events = await self.storage.list_events_for_task_filtered(
    task_id,
    event_types={"main_agent.output_final"},
    visibility=EventVisibility.FRONTEND,
)
```

然后继续复用：

```python
select_final_text_artifact(artifacts, events=final_events)
```

这样仍保留现有 answer selection 规则，但不再扫描 reasoning delta。

### 3. 完成后历史同步失败隔离

`_run_execution()` 中主编排执行完成后，assistant 历史同步属于完成后 bookkeeping。它失败时不能调用 `_mark_task_failed()` 反转已完成任务。

建议结构：

```python
result = await execute_request(...)
await handle_pending_skill_context(...)
if result.completion_status == "completed":
    await _sync_assistant_history_after_completion(...)
```

其中 `_sync_assistant_history_after_completion()` 捕获异常并记录 audit-only event，例如：

```text
assistant_history_sync.failed
```

payload 只包含脱敏字段：

- `task_id`
- `conversation_id`
- `code`
- `diagnostic`
- `history_message_id`

不得包含 prompt、answer 正文、API key、base_url、raw event payload。

### 4. 前端行为保持不变

实时阶段：

```text
SSE reasoning_delta -> 前端内存 reasoningContent -> 显示“思考内容”气泡
SSE output_delta -> 前端内存 content -> 显示 answer
```

刷新 / 重新登录后：

```text
GET /conversations/{id}/messages -> message.content -> 显示最终 answer
```

不恢复 reasoning 气泡，符合用户确认的“不持久化深度思考内容”。

## 数据流

### 修复前

```text
task completed
  -> list all events for task
  -> too many reasoning_delta events
  -> event_log_replay_page_exceeded
  -> assistant message not written
  -> task may become failed
  -> refresh shows no answer
```

### 修复后

```text
task completed
  -> list text artifacts
  -> list only main_agent.output_final events
  -> select final text artifact
  -> write task_id:assistant message
  -> refresh shows answer
```

If history sync still fails unexpectedly:

```text
task remains completed
  -> assistant_history_sync.failed audit event
  -> no user-visible task failure regression
```

## Error handling

1. Filtered event query failure during post-completion sync must not mark task failed.
2. Missing final text artifact means no assistant history message is written; this remains a no-op, matching current behavior.
3. Duplicate assistant message race remains idempotent: if `task_id:assistant` already exists, sync returns successfully.
4. `assistant_history_sync.failed` must be audit-only and sanitized.

## Testing plan

### Storage tests

- `list_events_for_task_filtered()` returns only requested event types.
- Filtering by visibility and node_id works.
- Limit and ordering are deterministic.
- Test proves implementation does not call all-event replay helper when event_types filter is provided, where practical.

### Runtime / API tests

- Create a completed task with:
  - many `main_agent.reasoning_delta` events,
  - a final text artifact,
  - a `main_agent.output_final` event.
- Configure or fake all-event replay to raise `event_log_replay_page_exceeded` if called.
- Call `sync_assistant_history_message_for_task()` or `GET /conversations/{id}/messages`.
- Assert:
  - `task_id:assistant` message exists,
  - message content equals final answer,
  - task remains completed,
  - no full event replay was required.

### Failure isolation tests

- Force `_persist_assistant_history_message()` to raise after task completion.
- Assert:
  - task remains completed,
  - audit-only `assistant_history_sync.failed` event exists,
  - no frontend `task.failed` event is emitted for that completed task.

### Frontend tests

Existing history restoration tests remain valid because API still returns `message.content`. No new reasoning restoration tests should be added because reasoning is intentionally not persisted.

## Acceptance criteria

1. Deep-thinking tasks with large reasoning streams can complete and remain completed.
2. Refreshing after completion shows final answer content.
3. Re-login after completion shows final answer content.
4. History sync never reads all task events solely to recover final answer.
5. Reasoning content is not persisted into message history or returned by history API.
6. License Requirement remains unaffected: no dependency or license policy changes.

## Out of scope

- Persisting reasoning content.
- Returning reasoning in historical messages.
- Adding `assistant_response` durable store.
- Reworking SSE event storage architecture.
- PostgreSQL migration of this path.
