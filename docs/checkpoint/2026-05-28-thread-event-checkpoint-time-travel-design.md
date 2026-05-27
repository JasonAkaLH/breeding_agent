# Thread Event Log + Execution Checkpoint + Time Travel 设计

日期：2026-05-28

状态：设计确认稿（document-perfectization 复审通过）

范围：新 `dev` 分支上的后端状态底座、API v2、前端分支/时间旅行交互、PostgreSQL 新开发库

## 1. 背景与目标

当前系统已经有 task / node / message / event / artifact / interrupt / checkpoint 等分散账本结构。现有 checkpoint 只保存 `snapshot_ref`、`resume_token` 等薄引用，无法承担完整的执行状态快照、节点级时间旅行和 thread 状态保存序列。

本设计选择直接演进为：

```text
Thread Event Log + Execution Checkpoint + Branch + Projection
```

目标：

1. 用 `thread_event_log` 作为事实账本 source of truth。
2. 用 `execution_checkpoint` 保存执行状态快照，而不是保存历史消息原文或事实账本本体。
3. 支持普通用户可见的节点级 time travel。
4. 支持同一个 thread 内的 branch/version 切换。
5. 支持 interrupt resume、失败重试、checkpoint 恢复。
6. 使用新的 PostgreSQL 开发 DB，fresh start，不迁移旧数据。
7. API、后端模型、前端状态命名统一改为 `thread / branch / run / checkpoint`，不再沿用 `conversation / task` 作为新接口命名。


### 1.1 问题陈述

当前系统的事实记录、前端进度、执行恢复和 artifact 归属分散在 task / message / event / artifact / interrupt / checkpoint 多套表与接口中。现有 checkpoint 只提供薄恢复引用，不能独立表达“某一时刻可继续执行的完整状态”。因此系统难以支持节点级重新执行、长期 thread 状态序列、分支版本查看和可靠中断恢复。

本设计要解决的问题是：把事实账本、执行状态快照和查询视图明确分层，使普通用户可以从任意稳定节点边界重新执行，同时保证历史不可被隐式改写，删除后没有业务残留。

### 1.2 用户、干系人与受影响系统

| 类别 | 影响 |
| --- | --- |
| 普通用户 | 可以在对话内查看分支版本，并从节点级重新执行点创建新版本。 |
| 前端业务对话台 | 需要从 conversation/task 状态模型迁移到 thread/branch/run/checkpoint 状态模型，并实现 branch switcher。 |
| 后端 API runtime | 需要提供 `/api/v2` thread/run/checkpoint 接口，并禁止 v2 runtime 回写 v1 表。 |
| 编排服务 | 需要把 plan、node outputs、next nodes 和 runtime replan 状态写入 checkpoint。 |
| Storage / PostgreSQL runtime | 需要以新 PostgreSQL DB 承载 event log、checkpoint、projection 和物理删除。 |
| Skill / SQL / LLM capability | time travel 默认复用 checkpoint 前输出，checkpoint 后的节点可能重新执行并产生新 artifact。 |
| 运维/开发者 | 需要新的 DB、schema bootstrap、删除清理验证、typed error 与审计证据。 |

### 1.3 当前仓库证据

| 证据 | 当前状态 | 对本设计的影响 |
| --- | --- | --- |
| `src/core/models.py` | `Task`、`TaskNode`、`EventRecord`、`Artifact` 与 `Checkpoint` 是分散模型；`Checkpoint` 仅包含 `snapshot_ref` / `resume_token` / `invalidated_at` 等薄字段。 | 需要把 checkpoint 升级为独立执行状态快照，而非继续依赖薄引用。 |
| `src/core/contracts.py` | StoragePort 分别提供 task、node、artifact、event、interrupt、checkpoint 读写接口。 | v2 应新增 thread event store / checkpoint store contract，而不是只扩展旧 StoragePort 命名。 |
| `src/orchestration/service.py` | `node_outputs` 是执行中的内存字典，节点完成/失败只写旧 task/node/event。 | checkpoint 必须持久化 node outputs / dependency outputs / next nodes，否则无法可靠 resume/time travel。 |
| `src/api/runtime.py` | 提交消息会创建 conversation current_task、message、task 和 `task.accepted` event；assistant 历史由 final event/artifact 派生。 | v2 需要用 thread active branch + run projection 替代 current_task 语义。 |
| `docs/prd/backend/postgresql-state-platform/` | 已有 PostgreSQL State Platform、write queue、fail-closed 和 fresh cutover 设计经验。 | v2 DB/schema bootstrap 可复用 PostgreSQL fail-closed 和 no silent fallback 原则。 |
| `CHANGELOG.md` | 当前仓库已多次选择 fresh cutover / 不迁移旧 SQLite 历史。 | 本设计的 fresh start 与既有迁移策略一致。 |

### 1.4 范围与非目标

**范围内：**

1. 新 PostgreSQL v2 DB schema。
2. `/api/v2` thread / branch / run / checkpoint / artifact / interrupt 接口契约。
3. Thread event log 事实账本。
4. Execution checkpoint 执行状态快照。
5. Projection 读模型。
6. 普通用户可见 branch switcher 和节点级重新执行。
7. Branch / thread / user 物理删除与清理验证。
8. Fresh start 开发，不迁移旧数据。

**明确非目标：**

1. 不兼容 `/api/v1/conversations` / `/api/v1/tasks` 旧 URL。
2. 不迁移旧 SQLite/PostgreSQL 历史数据。
3. 不做 v1/v2 双写。
4. 不把历史消息原文、artifact 内容或 provider secret 存进 checkpoint。
5. 不支持删除后的恢复。
6. 不在第一版支持同一 thread 多 active run 并发。

## 2. 总体架构原则

### 2.1 核心定义

```text
thread      = 一个长期对话上下文
branch      = thread 下的一条历史线 / 版本
run         = branch 上的一次用户请求执行或一次 replay/fork 执行
event       = 真实发生过的事实账本记录
checkpoint  = 某个 branch 在某个 event sequence 上的执行状态快照
projection  = 从 event/checkpoint 派生出的 API/前端查询视图
```

### 2.2 Source of truth

- `thread_event_log` 是事实源。
- `execution_checkpoint` 是恢复源。
- projection 是读模型和查询优化层，可以重建，不是最终事实源。

### 2.3 Fresh start

本次在新的 git branch `dev` 与新的 PostgreSQL 开发 DB 中推进。

- 不迁移旧 SQLite/PostgreSQL 数据。
- 不做 v1/v2 双写。
- 不做旧 task/message/event/artifact 到 thread event log 的 backfill。
- 旧架构数据保留在旧分支 / 旧 DB。

### 2.4 时间旅行原则

时间旅行不是回滚，也不是覆盖历史。

```text
基于旧 checkpoint 创建新 branch
在当前真实时间写入 branch.forked / run.replay_started 等新事件
后续执行追加到新 branch
旧 branch 保持不可变
```

### 2.5 Checkpoint 原则

Checkpoint 是执行状态快照，不是事实账本本体。

保存：

- plan / DAG snapshot；
- node 状态矩阵；
- node output refs；
- dependency outputs；
- next runnable nodes；
- interrupt/resume 上下文；
- event cursor；
- branch lineage；
- state hash；
- snapshot schema version。

不保存：

- 历史消息原文；
- 完整 event log；
- artifact 内容；
- provider API key / DB 密码 / request headers；
- 大 SQL 结果；
- reasoning 原文。

历史消息原文存放在事实账本 / message projection 中，checkpoint 只保存 message refs、context refs、event cursor 与 hash。

## 3. 核心数据模型

### 3.1 `thread`

表示长期对话上下文。

关键字段：

```text
thread_id
owner_username
title
active_branch_id
status
created_at
updated_at
```

约束：

- `active_branch_id` 指向当前可写 branch。
- 用户发送新消息只能写入 active branch。
- 删除 thread 必须级联删除所有 branch、events、checkpoints、projections、artifacts 和物理文件。

### 3.2 `thread_branch`

表示 thread 内的一条历史线。

关键字段：

```text
branch_id
thread_id
name
status
parent_branch_id
forked_from_checkpoint_id
forked_from_event_seq
forked_from_run_id
created_by
created_at
activated_at
```

语义：

- 初始 thread 创建 `main` branch。
- time travel 创建新 branch。
- branch 激活要写 `branch.activated` 事件。
- 切换旧 branch 只是查看；只有 active branch 可继续发送消息。

### 3.3 `thread_event_log`

事实账本 source of truth。

关键字段：

```text
event_id
thread_id
branch_id
run_id
event_seq
event_type
event_time
recorded_at
actor_type
actor_id
payload
payload_schema_version
causation_event_id
correlation_id
idempotency_key
visibility
projection_status
created_at
```

关键约束：

```text
unique(thread_id, branch_id, event_seq)
unique(idempotency_key) where idempotency_key is not null
index(thread_id, branch_id, event_seq)
index(run_id, event_seq)
index(event_type, recorded_at)
```

时间语义：

- `recorded_at` 是真实写入时间，审计用，不能伪装成过去。
- `event_seq` 是 branch 内逻辑顺序。
- `event_time` 是业务事件发生时间，一般等于 `recorded_at`。

主要事件类型：

```text
thread.created
branch.created
branch.forked
branch.activated
message.user_created
run.accepted
run.replay_started
plan.created
node.started
node.completed
node.failed
node.waiting_for_input
interrupt.opened
interrupt.answered
checkpoint.created
artifact.created
assistant.finalized
run.completed
run.failed
branch.delete_requested
thread.delete_requested
```

删除类事件只作为删除流程中的短暂事实记录；物理删除完成后，业务账本随目标对象一起清理。长期只保留脱敏 audit。

### 3.4 `execution_checkpoint`

执行状态快照。

关键字段：

```text
checkpoint_id
thread_id
branch_id
run_id
node_id
parent_checkpoint_id
event_seq
checkpoint_kind
snapshot_schema_version
state_snapshot
state_hash
ledger_fingerprint
resume_token
created_at
invalidated_at
```

关键约束：

```text
unique(resume_token) where resume_token is not null
index(thread_id, branch_id, event_seq)
index(run_id, node_id)
index(parent_checkpoint_id)
```

`state_snapshot` 示例：

```json
{
  "plan": {
    "plan_id": "plan-xxx",
    "nodes": [],
    "edges": [],
    "metadata": {}
  },
  "node_statuses": {
    "node-1": "completed",
    "node-2": "ready"
  },
  "node_outputs": {
    "node-1": {
      "artifact_refs": ["artifact-1"],
      "output_payload_ref": "payload-ref-1",
      "summary": "SQL 查询结果"
    }
  },
  "dependency_outputs": {},
  "next_node_ids": ["node-2"],
  "message_refs": ["msg-1", "msg-2"],
  "memory_context_ref": "memory-ref-1",
  "context_snapshot_ref": "context-ref-1",
  "artifact_refs": ["artifact-1"],
  "interrupt_ref": null,
  "model_config_ref": {
    "model_edition": "default"
  },
  "replay_policy": {
    "reuse_prior_outputs": true
  }
}
```

### 3.5 Projection 表

Projection 服务 API 和前端读取。

建议表：

```text
thread_projection
branch_projection
message_projection
run_projection
node_projection
artifact_projection
interrupt_projection
```

字段方向：

```text
thread_projection:
  thread_id, owner_username, title, active_branch_id, latest_run_id, status, updated_at, projection_version

branch_projection:
  branch_id, thread_id, name, parent_branch_id, latest_run_id, latest_checkpoint_id, message_count, run_count, status, updated_at

message_projection:
  message_id, thread_id, branch_id, run_id, role, content_ref, content_text, stream_status, created_at, event_seq

run_projection:
  run_id, thread_id, branch_id, root_message_id, status, routing_mode, requested_capability_id, current_checkpoint_id, started_at, finished_at, error_summary, updated_at

node_projection:
  node_id, run_id, thread_id, branch_id, capability_id, status, input_refs, output_refs, started_at, finished_at, checkpoint_id

artifact_projection:
  artifact_id, thread_id, branch_id, run_id, producer_node_id, artifact_type, storage_ref, summary, is_complete, created_at
```

第一版采用同事务写 event log + projection，后续再评估异步 projector。

## 4. 普通执行流

用户在 active branch 发消息：

```text
1. 写 message.user_created event
2. 写 run.accepted event
3. 更新 message_projection / run_projection
4. planner 生成 plan
5. 写 plan.created event
6. 生成 after_plan_created checkpoint
7. scheduler 执行 ready nodes
8. node started/completed/failed/waiting 写 event
9. 每个稳定节点边界生成 checkpoint
10. finalizer 写 assistant.finalized event
11. 写 run.completed 或 run.failed event
12. 生成 final checkpoint
```

稳定 checkpoint 边界：

```text
run.accepted
plan.created
node.completed
node.failed
node.waiting_for_input
interrupt.opened
interrupt.answered
assistant.finalized
run.completed
run.failed
branch.forked
branch.activated
```

不按 token/chunk 生成 checkpoint。

## 5. 中断恢复流程

当节点需要用户补充信息：

```text
1. 写 node.waiting_for_input event
2. 创建 checkpoint: node.waiting_for_input
3. 写 interrupt.opened event
4. projection 标记 run/node waiting_for_input
5. 前端展示问题
```

用户回答：

```text
1. 写 interrupt.answered event
2. 读取 interrupt 绑定的 checkpoint
3. 校验 checkpoint 未删除、未失效、branch 仍存在
4. 合并 answer_payload 到执行上下文
5. 写 checkpoint: interrupt.answered
6. node 进入 ready_to_resume / resuming
7. 从 checkpoint.next_node_ids 或 node_id 继续执行
8. 后续继续生成 events / checkpoints
```

分工：

- checkpoint 负责“从哪里、以什么执行状态继续”。
- interrupt 负责“为什么停、等什么、用户答了什么”。
- 任何可恢复 interrupt 都必须绑定 checkpoint。

## 6. 时间旅行与分支行为

### 6.1 用户入口

普通用户可从节点级 checkpoint 重新执行。UI 文案使用：

```text
重新从这一步执行
```

而不是暴露底层 checkpoint/fork 术语。

### 6.2 Time travel 流程

```text
1. 后端校验 checkpoint 可用
2. 创建新 branch
3. new_branch.parent_branch_id = source_branch
4. new_branch.forked_from_checkpoint_id = checkpoint_id
5. new_branch.forked_from_event_seq = checkpoint.event_seq
6. 写 branch.forked event
7. 创建新 run
8. 写 run.replay_started event
9. 默认复用 checkpoint 前已完成 node outputs
10. 从 checkpoint 后的 next nodes 继续执行
11. 写 branch.activated event
12. thread.active_branch_id = new_branch
13. 前端自动切到新 branch
```

原则：

- 不修改原 branch。
- 不回滚原账本。
- 新 branch 事件的 `recorded_at` 是当前真实时间。
- 新 branch 使用自己的 `event_seq`。
- 新 artifact 必须新建。
- 复用旧 artifact 时只引用；如果旧 artifact 已因删除不可用，则 time travel fail closed。

### 6.3 新分支消息呈现

从历史 checkpoint fork 后，新 branch 消息流为：

```text
fork 点之前的历史消息 + fork 后新执行产生的消息/结果
```

fork 点之后原 branch 的旧消息/任务仍留在原 branch，不出现在新 branch 默认消息流。

### 6.4 Branch switcher

同一个 thread 内有 branch/version 切换器。

- 切换旧 branch：只读查看。
- 只有 active branch 可继续发送。
- 要在旧 branch 继续，必须显式点击“设为当前版本”。
- 设为当前版本写 `branch.activated` event，并更新 `thread.active_branch_id`。

## 7. 删除语义与清理范围

删除行为都是历史删除 / 物理清理语义。除脱敏 audit 外，不保留可通过业务 API、branch、checkpoint、artifact、projection 找回的历史数据。

### 7.1 Branch 删除

删除 branch 时必须物理删除该 branch 及其子 branch：

```text
thread_branch 子树
thread_event_log
execution_checkpoint
message_projection
run_projection
node_projection
artifact_projection
interrupt/wait state
branch-scoped memory summary
payload/context refs
artifact metadata
physical artifact files
```

规则：

1. active branch 不能直接删除，除非同时指定新的 active branch。
2. 删除 thread 最后一条 branch 等同删除整个 thread。
3. branch 删除默认级联删除子 branch。
4. 删除后 branch switcher 不显示该 branch。
5. 删除后不能查询消息、run、checkpoint、artifact。
6. 删除后不能从该 branch checkpoint time travel 或 resume。

### 7.2 Thread / Conversation 删除

删除 thread 等同删除该对话下所有历史：

```text
all branches
all events
all checkpoints
all runs/messages/nodes/artifacts projections
all interrupt/wait state
all memory/context refs
all physical artifact files
```

删除后：

- thread 列表消失；
- 任意 branch 不可访问；
- 任意 checkpoint 不可恢复；
- 任意 artifact 不可下载。

“删除对话历史”在第一版中语义等同 thread 删除，不提供“清空某 branch 但保留 thread shell”的能力。

### 7.3 User 删除

删除用户时必须删除该用户名下全部业务历史：

```text
user owned threads
branches
events
checkpoints
messages
runs
nodes
artifacts
uploads / attachment refs
memory/context refs
physical files
SSE/current run state
```

删除后该用户不能通过 API 找回任何旧 thread、branch、checkpoint 或 artifact。

### 7.4 审计例外

唯一允许长期保留的是脱敏 audit：

```text
user.delete_requested / completed
thread.delete_requested / completed
branch.delete_requested / completed
```

审计不得包含：

- 原始消息正文；
- artifact 内容；
- checkpoint snapshot；
- provider payload；
- DB 查询结果；
- 文件路径中的敏感内容。

## 8. API v2 契约

新架构统一使用 `/api/v2`，URL 和字段命名都使用 `thread / branch / run / checkpoint`。

### 8.1 Thread

```text
POST   /api/v2/threads
GET    /api/v2/threads
GET    /api/v2/threads/{thread_id}
DELETE /api/v2/threads/{thread_id}
```

### 8.2 Branch

```text
GET    /api/v2/threads/{thread_id}/branches
POST   /api/v2/threads/{thread_id}/branches/{branch_id}/activate
DELETE /api/v2/threads/{thread_id}/branches/{branch_id}
```

激活 branch：

```json
{
  "branch_id": "branch-2"
}
```

删除 active branch 时必须提供 replacement：

```json
{
  "replacement_active_branch_id": "branch-main"
}
```

### 8.3 Messages

```text
GET  /api/v2/threads/{thread_id}/messages?branch_id=...
POST /api/v2/threads/{thread_id}/messages
```

发送消息只能写 active branch。如果请求显式带 `branch_id`，必须等于 active branch，否则 fail closed。

### 8.4 Runs

```text
GET /api/v2/runs/{run_id}
GET /api/v2/runs/{run_id}/graph
GET /api/v2/runs/{run_id}/events
```

SSE 事件体：

```json
{
  "event_id": "evt-1",
  "thread_id": "thread-1",
  "branch_id": "branch-main",
  "run_id": "run-1",
  "event_type": "node.completed",
  "payload": {}
}
```

### 8.5 Checkpoints

```text
POST /api/v2/checkpoints/{checkpoint_id}/time-travel
```

请求：

```json
{
  "branch_name": "从 SQL 查询完成后重新执行",
  "client_request_id": "..."
}
```

返回：

```json
{
  "thread_id": "thread-1",
  "branch_id": "branch-new",
  "run_id": "run-new",
  "active_branch_id": "branch-new",
  "sse_url": "/api/v2/runs/run-new/events"
}
```

### 8.6 Interrupts

```text
POST /api/v2/interrupts/{interrupt_id}/answers
```

Interrupt resume 必须通过绑定 checkpoint 恢复执行状态。

### 8.7 Artifacts

```text
GET    /api/v2/artifacts/{artifact_id}
DELETE /api/v2/artifacts/{artifact_id}
```

Artifact 查询和下载必须校验 thread/branch/user 权限，且删除后不可通过旧 checkpoint 引用绕过访问。

## 9. 前端契约

### 9.1 命名

代码状态模型使用：

```text
Thread
Branch
Run
Checkpoint
Artifact
```

UI 中文可显示：

```text
对话
版本
执行任务
重新执行点
文件/结果
```

### 9.2 Branch switcher

会话标题附近展示版本切换器：

```text
当前版本：main
```

每个 branch 展示：

- 名称；
- 状态；
- 创建时间；
- fork 来源描述；
- 是否当前版本。

### 9.3 历史 branch 只读

查看非 active branch 时：

- 输入框禁用；
- 提示“你正在查看历史版本。要继续发送消息，请先设为当前版本。”；
- 提供“设为当前版本”按钮。

### 9.4 节点级重新执行

任务图或消息附近展示：

```text
重新从这一步执行
```

点击后：

- 调用 checkpoint time travel API；
- 创建新 branch；
- 自动切换；
- 新 assistant 运行气泡出现；
- 旧 branch 不混入当前视图。

## 10. 并发、一致性与幂等

### 10.1 Event sequence

每个 branch 的 event sequence 必须单调递增。

建议使用 cursor 表：

```text
branch_event_cursor:
  thread_id
  branch_id
  next_event_seq
  updated_at
```

写事件时：

```text
SELECT branch_event_cursor FOR UPDATE
取 next_event_seq
写 event
next_event_seq += 1
更新 cursor
```

### 10.2 Active branch 并发

发送消息：

```text
1. SELECT thread FOR UPDATE
2. 读取 active_branch_id
3. 校验请求 branch
4. 写 message/run events 到 active branch
5. 更新 projection
```

激活 branch：

```text
1. SELECT thread FOR UPDATE
2. 校验 branch 未删除
3. 写 branch.activated event
4. 更新 thread.active_branch_id
```

### 10.3 Run 并发

第一版约束：

- 同一 thread 同一时间只允许一个 active run。
- 不同 thread 可并发。
- 同一 branch 不允许两个 run 同时写入。
- time travel 创建新 branch + 新 run，并自动成为 active branch。

### 10.4 Time travel 幂等

`POST /api/v2/checkpoints/{checkpoint_id}/time-travel` 必须支持 idempotency：

```text
idempotency_key = user_id + checkpoint_id + client_request_id
```

重复请求返回同一个新 branch/run，不重复 fork。

### 10.5 事务边界

这些操作必须原子：

```text
message.user_created + run.accepted + projection 更新
node.completed + artifact projection + checkpoint
interrupt.answered + checkpoint + node resume 状态
branch.forked + new run + branch.activated + active_branch_id
branch.activated + active_branch_id
```

第一版采用同事务写 event log + projection；如果不能同事务完成，则 fail closed。

## 11. 错误模型

稳定错误码：

```text
thread_not_found
branch_not_found
branch_not_active
branch_deleting
run_conflict
checkpoint_not_found
checkpoint_unavailable
checkpoint_refs_missing
checkpoint_schema_unsupported
time_travel_conflict
artifact_deleted
deletion_in_progress
```

前端映射：

- `branch_not_active`：你正在查看历史版本，请先设为当前版本。
- `checkpoint_unavailable`：这个重新执行点已不可用。
- `run_conflict`：当前对话已有任务在执行，请等待完成。
- `deletion_in_progress`：该内容正在删除，暂不可操作。

## 12. 新 PostgreSQL 开发 DB

建议新建独立开发库：

```text
breeding_agent_v2_dev
```

配置示例不包含密码：

```yaml
state_platform:
  backend: postgresql_v2
  postgres:
    host: postgres
    port: 5432
    database: breeding_agent_v2_dev
    username: biobin_user
```

原则：

- 不污染现有生产/远端测试库。
- 允许破坏性 schema 迭代。
- schema bootstrap 只创建 v2 表。
- v2 runtime 不写 v1 表。
- schema 不完整时 fail closed。

启动检查：

```text
1. 检查 PostgreSQL 连接
2. 检查目标 DB 名称
3. 检查 v2 schema_version
4. 创建/升级 thread/event/checkpoint/projection 表
5. 校验索引、唯一约束、FK/删除策略
6. 启动 API runtime
```

## 13. 实施切分建议

### PRD-1：Thread Event Store 内核

范围：

- `thread`
- `thread_branch`
- `thread_event_log`
- branch event cursor
- event append contract
- idempotency
- schema bootstrap

验收：

- 同 branch 事件顺序稳定。
- 不同 branch 可并发。
- event log 是事实源。

### PRD-2：Projection 与 `/api/v2/threads`

范围：

- projection 表；
- thread 列表；
- branch 列表；
- message projection；
- active branch；
- `/api/v2/threads/*` 基础 API。

验收：

- 前端能展示 thread/branch/message。
- 历史 branch 只读。
- active branch 可写。

### PRD-3：Run / Node / Artifact 执行链路

范围：

- run projection；
- node projection；
- artifact projection；
- orchestration 写入 event log；
- run graph API；
- SSE v2。

验收：

- 一次普通用户消息能完整执行。
- node/run/artifact/event 都带 branch_id。
- SSE 只污染对应 branch/run。

### PRD-4：Execution Checkpoint 与中断恢复

范围：

- `execution_checkpoint`；
- checkpoint schema version；
- checkpoint 生成边界；
- interrupt 绑定 checkpoint；
- resume 校验。

验收：

- waiting_for_input 可从 checkpoint 恢复。
- checkpoint 不保存历史消息原文。
- 删除后 checkpoint 不可恢复。

### PRD-5：Time Travel / Branch UI

范围：

- checkpoint time travel API；
- branch fork；
- branch activated；
- 前端 branch switcher；
- 节点级“重新从这一步执行”。

验收：

- 用户可从节点 checkpoint 创建新 branch。
- 新 branch 自动 active。
- 旧 branch 保留且只读。
- 默认复用 checkpoint 前节点输出。

### PRD-6：物理删除与全局清理

范围：

- branch 删除；
- thread 删除；
- 用户删除；
- artifact 文件物理清理；
- SSE/run/interrupt 终止；
- 脱敏 audit。

验收：

- 删除后业务 API 无残留。
- checkpoint/time travel 不可绕过删除。
- branch 删除级联子 branch。
- 用户删除清理全部 thread/branch/history/artifact。

## 14. 测试策略

### 14.1 单元测试

覆盖：

- event seq 分配；
- idempotency；
- checkpoint state hash；
- branch lineage；
- projection 更新；
- 删除级联计划。

### 14.2 集成测试

覆盖：

- PostgreSQL schema bootstrap；
- 普通 run 执行；
- branch activation；
- time travel；
- interrupt resume；
- artifact download；
- branch/thread/user 删除后不可访问。

### 14.3 前端测试

覆盖：

- branch switcher；
- 历史 branch 只读；
- active branch 发送消息；
- time travel 自动切新 branch；
- 删除提示与删除后 UI 消失。

### 14.4 E2E smoke

最小链路：

```text
创建 thread
发送消息
生成 run/checkpoint
从 node checkpoint time travel
新 branch 自动 active
继续发送消息
删除旧 branch
确认旧 checkpoint/artifact/message 不可访问
```

## 15. 风险与边界

### 15.1 最大风险

- 大爆炸重构风险：source of truth 直接切换，必须只在 `dev` 分支和新 DB 推进。
- Projection 一致性风险：第一版必须同事务写 event + projection。
- 删除遗漏风险：删除后不得通过 checkpoint/time travel/artifact 找回。
- Time travel 副作用风险：LLM、SQL、Skill 重跑结果可能不同，必须明确新 branch 是当前时间的新执行结果。

### 15.2 明确不做事项

第一版不做：

1. 旧数据迁移。
2. v1/v2 双写。
3. checkpoint-only 存储。
4. 异步 projector。
5. 多 active run 并发。
6. 同一 thread 多 branch 同时写。
7. 删除后可恢复。
8. branch 删除保留子 branch。
9. checkpoint 内保存完整消息原文。
10. 时间旅行原地覆盖旧 branch。
11. 普通用户看到底层 event/checkpoint 技术细节。
12. 只删除 UI 记录、不删物理 artifact 的软删除。

## 16. 成功标准

完成后系统应能做到：

```text
创建 thread
main branch 发消息
run 执行并生成 node checkpoints
用户从任意节点 checkpoint 重新执行
系统创建新 branch
新 branch 自动 active
旧 branch 可切换查看但只读
用户可设旧 branch 为 active
删除 branch/thread/user 后无业务残留
checkpoint 可用于 interrupt resume
所有历史事实来自 thread_event_log
所有恢复态来自 execution_checkpoint
```

## 17. 最终设计决策

- 直接走 Event-sourcing + Checkpoint，不做轻量兼容阶段。
- 新 git branch `dev` 开发。
- 新 PostgreSQL DB，fresh start。
- API 改为 `/api/v2/threads / runs / checkpoints`。
- `thread_event_log` 是事实源。
- `execution_checkpoint` 是执行状态快照。
- 历史消息原文在 event log / message projection，不在 checkpoint。
- 普通用户可用节点级 time travel。
- 时间旅行创建新 branch，不改旧 branch。
- 同一 thread 内有 branch switcher。
- 只有 active branch 可继续发送消息。
- branch activation 写账本事件。
- branch/thread/user 删除都是物理清理。
- 删除后 checkpoint/time travel/artifact 不得绕过恢复。


## 18. 功能需求与验收矩阵

| ID | 需求 | 验收标准 | 验证方式 |
| --- | --- | --- | --- |
| FR-1 | v2 runtime 必须使用新 PostgreSQL DB 和新 schema。 | 连接到 v1 DB、缺少 v2 schema 或 schema hash 不匹配时启动 fail closed。 | PostgreSQL bootstrap integration test；错误配置启动测试。 |
| FR-2 | 所有新事实必须先进入 `thread_event_log`。 | message、run、node、artifact、interrupt、branch activation 都有对应 event，且同 branch `event_seq` 单调。 | Event store unit/integration tests。 |
| FR-3 | checkpoint 必须保存执行状态快照。 | checkpoint 包含 plan、node_statuses、node_outputs、dependency_outputs、next_node_ids、refs、schema_version、state_hash；不包含历史消息原文。 | Checkpoint schema tests；敏感字段扫描测试。 |
| FR-4 | 普通用户可以从节点级 checkpoint 重新执行。 | time travel 创建新 branch/run，自动设为 active branch，默认复用 checkpoint 前节点输出。 | API integration + frontend E2E。 |
| FR-5 | Branch switcher 必须支持查看历史版本。 | 非 active branch 只读，不能发送消息；显式 activate 后才可写。 | Frontend state/reducer/App tests。 |
| FR-6 | branch activation 必须入账。 | 每次设为当前版本都写 `branch.activated` event 并更新 active branch projection。 | API integration test。 |
| FR-7 | interrupt resume 必须绑定 checkpoint。 | interrupt 缺 checkpoint、checkpoint 失效或引用缺失时 fail closed；正常回答后从 checkpoint 恢复。 | Lifecycle integration test。 |
| FR-8 | branch/thread/user 删除必须物理清理。 | 删除后业务 API、artifact download、checkpoint time travel、resume 均不可访问对应历史。 | Deletion integration + artifact filesystem test。 |
| FR-9 | Projection 必须与 event 同事务更新。 | event 写入和 projection 更新不能半成功；任一失败事务回滚。 | Transaction rollback integration test。 |
| FR-10 | `/api/v2` 命名必须保持一致。 | 新接口和 DTO 使用 `thread_id`、`branch_id`、`run_id`、`checkpoint_id`，不暴露新 `conversation_id` / `task_id` 字段。 | API schema/static contract test。 |

## 19. 非功能需求

| 类别 | 要求 |
| --- | --- |
| 可靠性 | v2 写路径必须 fail closed；event/projection/checkpoint 不允许半成功；删除失败对象不得恢复为正常可见。 |
| 一致性 | 同一 branch 内 event 顺序必须严格单调；active branch 切换与发送消息必须串行化。 |
| 安全与隐私 | checkpoint、audit、错误响应不得包含 API key、DB 密码、provider header、历史消息全文、artifact 内容、大 SQL 结果或 reasoning 原文。 |
| 可删除性 | branch/thread/user 删除后，业务 API、checkpoint、time travel、artifact download、projection 和物理文件均不得留可访问残留。 |
| 可观测性 | branch fork、branch activation、checkpoint create、time travel fail、delete requested/completed/failed 必须有脱敏审计或结构化事件。 |
| 兼容性 | v2 使用 fresh start；不保证 v1 数据可读；v1/v2 不双写。 |
| 性能边界 | 第一版不得在 API 热路径全量 replay thread event log；必须通过 projection 读取消息、branch、run、artifact 列表。 |
| 测试性 | 每个 PRD checkpoint 必须有单元/集成/E2E 验收，且删除、checkpoint 敏感字段、event ordering 必须纳入自动化测试。 |

## 20. Rollout、Rollback 与开发门禁

1. `dev` 分支使用新 PostgreSQL 开发 DB，例如 `breeding_agent_v2_dev`。
2. v2 runtime 必须由显式配置启用，例如 `state_platform.backend=postgresql_v2` 或等价开关。
3. 启动时必须校验目标 DB 名称、schema version、schema hash、关键索引和约束。
4. 发现连接到旧 DB、schema 缺失或权限不足时必须 fail closed。
5. Rollback 语义是停止 v2 runtime 并切回旧代码/旧 DB；不做 v2 数据回写 v1。
6. 开发阶段允许破坏性重建 v2 DB；进入共享测试环境后，schema 变更必须走显式 migration ledger。
7. 不得把真实生产敏感配置写入 tracked 文档或配置；文档只能保留脱敏示例。

## 21. 已确认业务决策、假设与开放问题

### 已确认业务决策

| 决策 | 结果 |
| --- | --- |
| 架构路线 | 直接采用 Event-sourcing + Checkpoint，而不是轻量兼容阶段。 |
| Git 分支 | 在新 `dev` 分支推进。 |
| 数据策略 | 新 PostgreSQL DB fresh start，不迁移旧数据。 |
| API 命名 | 使用 `/api/v2/threads`、`runs`、`checkpoints`，不沿用 conversation/task URL。 |
| 用户入口 | 普通用户可用节点级重新执行。 |
| Time travel 语义 | 创建新 branch，新 branch 自动 active，不覆盖旧 branch。 |
| Branch 发送消息 | 只有 active branch 可写；历史 branch 只读。 |
| 删除语义 | branch/thread/user 删除都是物理清理，默认级联删除子 branch。 |
| 消息原文位置 | 存 event log / message projection，不存 checkpoint。 |

### 必要假设

| 假设 | 风险控制 |
| --- | --- |
| v2 可以使用独立开发 DB，不需要保留旧会话。 | PRD 明确 fresh start；rollback 只切回旧 runtime/DB。 |
| 第一版同一 thread 只允许一个 active run。 | 保持与当前单任务串行会话假设一致，避免 branch/time travel 与多 run 并发叠加。 |
| Projection 可以同事务维护。 | 第一版不做异步 projector；后续如需异步化必须另起 PRD。 |

### 开放问题

当前无阻塞型开放问题。后续进入实施计划时，仍需在 PRD-1 中确定具体 PostgreSQL schema DDL、schema hash 生成方式和 v2 DB 命名。
