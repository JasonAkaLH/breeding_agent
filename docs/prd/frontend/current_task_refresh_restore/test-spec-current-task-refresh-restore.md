# Test Spec: Current Task Refresh Restore

日期：2026-05-20  
状态：已固化，长期交付级测试规格，已完成 document-perfectization 审查  
适用 PRD：`docs/prd/frontend/current_task_refresh_restore/01-current-task-refresh-restore.md`

## 1. Test Objectives

验证前端只基于 `conversation.current_task_id` 恢复当前 conversation 的 active task assistant 临时气泡运行态，并复用既有 SSE replay、task graph、interrupts 和 artifact fallback。测试不得把其它 unfinished tasks 当作恢复入口，也不得把刷新恢复功能扩大成“运行中自由切换 conversation”的新 UX。本测试规格按长期交付质量执行，不接受只覆盖 happy path 的验证。

## 2. Test Data Contract

### 2.1 Task status sets

- Active statuses：`accepted`、`planning`、`running`、`cancelling`。
- Terminal statuses：`completed`、`failed`、`cancelled`。
- Unknown status：按 stale / unsupported 处理，释放输入并提示。

### 2.2 Current task source

- Restore 入口只读取 `ConversationSummaryResponse.current_task_id`。
- `listConversationTasks(scope='unfinished')` 只允许出现在 cancel 兜底路径，不允许出现在 restore 判断路径。

## 3. Unit / Reducer Tests

文件：`frontend/src/domain/taskEvents.test.ts`

### 3.1 createRestoringTaskState

Given 调用 `createRestoringTaskState()`  
Then：

- `phase === 'running'`
- `statusText` 包含“恢复任务状态”
- `currentActivityText` 包含“同步任务输出”
- `assistantText === ''`
- `reasoningText === ''`
- `seenEventIds === []`

### 3.2 replay output delta from restoring state

Given `createRestoringTaskState()`  
When apply `main_agent.output_delta` with visible payload and event id `evt-1`  
Then `assistantText` 累积 delta，phase 进入 `streaming`。

### 3.3 replay reasoning delta from restoring state

Given `createRestoringTaskState()`  
When apply `main_agent.reasoning_delta`  
Then `reasoningText` 累积 delta。

### 3.4 replay skill progress from restoring state

Given `createRestoringTaskState()`  
When apply `node.started` / `skill.progress` for `capability_id='skill.example'`  
Then `skillStatuses` 出现对应 Skill 状态行，`currentCapabilityId` 指向该 Skill。

### 3.5 duplicate event id is idempotent

Given state 已处理 `evt-1` output delta  
When 再次 apply 同一个 event id  
Then `assistantText` 不重复追加，`seenEventIds` 不重复膨胀。

### 3.6 non-final / intermediate main-agent deltas remain hidden

Given restore state  
When apply `main_agent.output_delta` with `response_role='intermediate'`  
Then visible `assistantText` 不追加该 delta，保持现有 response_role 过滤语义。

## 4. App Integration Tests

文件：`frontend/src/App.test.tsx`

### 4.1 自动登录后恢复 active current task

Given：

- localStorage 中 `maf.frontend.conversation_id.alice = 'conv-history'`。
- `api.me()` 成功返回 `{ user: { username: 'alice' } }`。
- `api.listConversations()` 返回 `conv-history.current_task_id = 'task-running'`。
- `api.listConversationMessages('conv-history')` 返回一条历史 user message。
- `api.getTask('task-running')` 返回 `status='running'`。
- fake EventSource 对 `/api/v1/tasks/task-running/events` replay：
  - `task.accepted`
  - `task.graph_created`
  - `node.started`
  - `main_agent.output_delta`，delta 为 `已生成内容`

Then：

- 历史 user message 可见。
- 页面出现一个恢复 assistant 气泡，初始有“正在恢复任务状态”或等价 activity。
- replay 后同一个气泡展示 `已生成内容`。
- 输入框 disabled。
- 停止按钮可见且可点击。
- 即使 React effect 或 history refresh 重跑，也只保留一个恢复气泡和一个 open subscription。

### 4.2 显式登录后恢复 active current task

Given：

- `api.me()` 先失败进入登录页。
- 用户通过登录表单成功登录 `alice`。
- localStorage / `loadOrCreateConversationId()` 指向 `conv-history`。
- `listConversations()`、`listConversationMessages()`、`getTask()`、EventSource replay 与 4.1 相同。

Then：

- 登录成功进入工作台后行为与 4.1 相同。
- 自动登录与显式登录不得出现一个恢复、另一个不恢复的分叉。

### 4.3 点击历史 conversation 恢复 active current task

Given 当前页面已经登录并展示 conversation list  
When 用户在 idle 状态点击 `conv-history`，其 summary 有 `current_task_id='task-running'`  
Then：

- `listConversationMessages('conv-history')` 被调用。
- `getTask('task-running')` 被调用。
- 加载历史 messages 后追加恢复气泡并订阅 task events。
- replay 后展示已生成内容。

### 4.4 completed current task 自愈为正式历史

Given：

- conversation summary 有 `current_task_id='task-completed'`。
- 第一次 `listConversationMessages()` 返回历史 user message。
- `getTask('task-completed')` 返回 `status='completed'`。
- 第二次 `listConversationMessages()` 返回包含正式 assistant reply。

Then：

- 页面不保留“正在恢复任务状态”的临时气泡。
- 正式 assistant reply 可见。
- 输入框可用。
- UI active 状态被释放。

### 4.5 failed / cancelled current task 释放输入

Given conversation summary 有 `current_task_id`  
When `getTask()` 返回 `failed` 或 `cancelled`  
Then：

- 不打开 EventSource。
- 不保留 active 恢复气泡。
- 显示任务已结束 / 未完成的 transient notice。
- 输入框可用。

### 4.6 unknown current task status fail-safe

Given conversation summary 有 `current_task_id`  
When `getTask()` 返回未识别 status，例如 `paused`  
Then：

- 不打开 EventSource。
- 不保留 active task state。
- 显示状态暂不支持 / 已失效类提示。
- 输入框可用。

### 4.7 current_task_id missing / forbidden 释放输入

Given conversation summary 有 `current_task_id`  
When `getTask()` rejected / throws `ApiError`  
Then：

- 不打开 EventSource。
- 不保留 active task state。
- 输入框可用。
- transient notice 显示任务状态失效类提示。
- 正式历史 messages 保留。

### 4.8 waiting-for-input 恢复

Given：

- `getTask('task-running')` 返回 `running`。
- EventSource opened 后没有 terminal event。
- `getTaskGraph('task-running')` 返回至少一个 `status='waiting_for_input'` node。
- `listInterrupts('task-running')` 返回 open interrupt，带 question 和 required_fields。

Then：

- assistant 气泡显示 interrupt prompt。
- 页面出现 `需要补充信息` 区域。
- 输入框 placeholder 对应 required field。
- 用户输入并提交后调用 `answerInterrupt('task-running', interruptId, payload)`。
- 补充提交后重新订阅 same task events。

### 4.9 current_task_id null 不恢复残留 unfinished tasks

Given：

- conversation summary `current_task_id === null`。
- mock `listConversationTasks()` 即使配置了 unfinished tasks 也不应被 restore 流程调用。

Then：

- 不调用 `getTask()`。
- 不打开 EventSource。
- 不出现恢复气泡。
- 输入框可用。
- `listConversationTasks()` 未被调用，除非用户点击停止按钮。

### 4.10 active task 时保持既有 conversation switch guard

Given：

- 当前页面已有 active task（初次提交或 restore 后均可）。
- history list 中存在另一个 conversation。

When 用户点击另一个 history conversation  
Then：

- 目标 conversation 的 `listConversationMessages()` 不被调用。
- 当前 running assistant 气泡仍保留。
- 当前 EventSource 不因点击 history 被关闭。
- 这证明本 PRD 没有扩大为多 conversation active switching。

### 4.11 idle 切换 conversation 关闭 stale subscription

Given：

- 当前页面处于 idle，存在一个旧 subscription ref（例如上一个 completed / cancelled task 后未清理干净的测试注入或 terminal cleanup 路径）。
- 用户点击另一个 conversation。

Then：

- 旧 EventSource `close()` 被调用。
- 旧 assistant 恢复气泡不出现在新 conversation。
- 新 conversation 按自己的 messages/current_task_id 恢复或保持 idle。

### 4.12 task.completed 后以正式 history 为最终展示且不重复消息

Given 恢复 task SSE replay 包含 `task.completed`  
When `loadArtifacts()` 完成，并 history refresh / messages reload 返回正式 assistant message  
Then：

- 页面最终以正式 conversation history 中的 assistant reply 为准。
- 页面最终只显示一个最终 assistant 回答语义，不同时残留恢复临时气泡和正式 reply 的重复答案。
- `currentTaskId` 清空，输入框可用。

### 4.13 登出 / 删除 / 新建 conversation 清理 runtime

Given 当前页面存在 restored active task 或 stale subscription  
When 用户登出、删除当前 conversation 或新建 conversation  
Then：

- subscription `close()` 被调用。
- `currentTaskId`、`currentAssistantId`、`pendingInterrupt` 清空。
- selected slash command、pending uploads、pending assistant patches 不泄漏到新 workspace。

### 4.14 过期异步结果不得污染当前 conversation

Given：

- 用户触发 conversation A restore，请求尚未返回。
- 用户在 idle 状态切换到 conversation B。
- conversation A 的 `listConversationMessages()` / `getTask()` / EventSource replay 随后返回。

Then：

- conversation A 的 messages、恢复气泡、task state 不写入 conversation B。
- conversation A 的 EventSource 被关闭或其 callback 被 generation guard 忽略。
- conversation B 只显示自己的 messages/current task 状态。

### 4.15 status 判断集中定义

Given 实现新增 active/terminal/stale status 判断  
Then：

- `accepted`、`planning`、`running`、`cancelling` active set 只在一个 helper / 常量位置定义。
- `completed`、`failed`、`cancelled` terminal set 只在一个 helper / 常量位置定义。
- 测试覆盖未知 status fail-safe。

### 4.16 初次提交路径不回归

Given 用户在 idle conversation 输入普通消息  
When submit message accepted  
Then：

- 仍立即追加 user message + 临时 assistant 气泡。
- `submitMessage()` accepted 后才设置 `currentTaskId` 并订阅 SSE。
- Slash command forced capability / upload metadata 既有测试仍通过。

## 5. Regression Matrix

必须保持以下既有行为：

1. Slash command picker / direct slash submit / forced capability metadata 不变。
2. 上传文件 metadata merge 不变。
3. 初次 waiting-for-input 路径仍能进入 interrupt answer。
4. busy conversation 409 仍显示友好错误。
5. stop button 仍调用 `listConversationTasks(scope='unfinished')` 作为取消兜底，但该 API 不参与恢复入口。
6. 历史 completed assistant reply 复制按钮、artifact 渲染、Skill 状态行渲染不受影响。
7. 当前 active task 时 history click guard 不被本 PRD 放宽。

## 6. Verification Commands

前端必跑：

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

如果实现触及后端 DTO / route / lifecycle，再追加：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 7. Manual Smoke

```bash
python scripts/run_fullstack_dev.py
```

Smoke 步骤：

1. 打开前端并登录。
2. 发送一条能产生可观察 streaming / Skill progress 的消息。
3. 在任务运行中刷新浏览器。
4. 重新进入同一 conversation。
5. 确认：历史消息可见、恢复 assistant 气泡出现、已生成文本/状态继续同步、输入框锁定、停止按钮可用。
6. 如任务进入等待补充，刷新后仍能看到补充提示并继续回答。
7. 在任务 active 时尝试点击其它 history conversation，确认仍保持既有 active guard。
