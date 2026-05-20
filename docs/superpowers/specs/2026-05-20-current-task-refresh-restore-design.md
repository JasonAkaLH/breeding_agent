# Current Task Refresh Restore Design

日期：2026-05-20  
状态：设计已确认  
范围：前端业务对话台在浏览器刷新或重新进入历史对话后，恢复当前 conversation 正在运行的单个任务展示。

## 1. 背景

当前业务对话台在用户首次提交消息后，会在聊天流中追加 user message 和一个临时 assistant 气泡，通过 task SSE 事件展示运行进度、流式回答、reasoning、Skill 状态、interrupt 提示和最终结果。

问题是：如果用户在任务运行中刷新浏览器，或者重新点进这个 conversation，前端目前主要恢复正式历史消息；运行中的临时 assistant 气泡、事件订阅和等待补充状态不会按刷新前体验还原。用户会误以为任务丢了。

本设计的业务目标是：**初次聊天流程是什么样，刷新后仍尽量恢复成同样的交互形态**。恢复对象是同一 conversation 的当前任务，而不是引入新的多任务面板。

## 2. 当前代码事实

- 同一 conversation 正常运行模型是单任务串行：
  - `src/lifecycle/conversation_guard.py` 的 `ConversationSerialGuard.ensure_conversation_available()` 会在已有 active task 时抛 `ConversationBusyError`。
  - `src/api/runtime.py` 的 `submit_message()` 在创建新 task 前调用该 guard。
  - `Conversation.current_task_id` 是单值。
  - `_clear_conversation_current_task()` 只在当前 task 终态后清空该字段。
- 前端当前也是单任务运行态：
  - `frontend/src/App.tsx` 使用单个 `currentTaskId`、`taskState`、`currentAssistantId`、`pendingInterrupt` 和 `subscriptionRef`。
- 后端已有恢复所需事实来源：
  - `GET /api/v1/conversations` 返回 conversation summary，其中包含 `current_task_id`。
  - `GET /api/v1/conversations/{conversation_id}/messages` 返回正式历史消息。
  - `GET /api/v1/tasks/{task_id}` 返回最新 task summary。
  - `GET /api/v1/tasks/{task_id}/graph` 返回节点状态。
  - `GET /api/v1/tasks/{task_id}/interrupts` 返回 open interrupts。
  - `GET /api/v1/tasks/{task_id}/events` 支持已落库事件 replay + 实时 SSE。

## 3. 目标

当用户刷新浏览器或重新点进一个 conversation 时：

1. 如果该 conversation 的 `current_task_id` 仍指向 active task，前端恢复一个临时 assistant 气泡。
2. 该气泡尽量恢复刷新前已经生成的 streaming text、reasoning、Skill 进度和当前活动状态。
3. 前端继续订阅同一个 task 的 SSE 实时事件。
4. 如果 task 已完成，临时恢复态消失，正式 assistant 历史消息进入聊天流。
5. 如果 task 等待补充，输入框恢复为 interrupt answer 模式，用户可以继续回答。

## 4. 非目标

- 不设计同一 conversation 多任务并行 UI。
- 不引入 composer 上方运行中任务面板。
- 不把 `listConversationTasks(scope=unfinished)` 作为刷新恢复的主入口。
- 不新增后端 snapshot API。
- 不要求 100% 恢复刷新前的 UI 展开状态、滚动位置或浏览器本地临时状态。

## 5. 产品行为

### 5.1 初次提交

初次发送消息的体验保持当前模式：

- 聊天流立即追加 user message。
- 聊天流追加一个临时 assistant 气泡。
- 临时 assistant 气泡显示提交、运行、Skill 进度、流式回答、reasoning、interrupt 和终态。
- task 完成后加载 artifacts / 最终内容，正式历史刷新后仍保持 assistant 回答可见。

### 5.2 刷新 / 重新进入 conversation

前端加载 conversation 后：

1. 先加载正式历史 messages。
2. 查找当前 conversation summary 的 `current_task_id`。
3. 若为空：不恢复运行态，输入框可用。
4. 若存在：
   - 调 `getTask(current_task_id)` 校验最新状态。
   - 若仍 active：在聊天流末尾重建一个临时 assistant 气泡。
   - 通过 task SSE replay 恢复已生成内容和进度。
   - 继续实时订阅 task events。

恢复中的 assistant 气泡应显示明确的同步文案，例如“正在恢复任务状态 / 正在同步输出”。当 replay 事件到达后，文案被真实状态替换。

### 5.3 等待补充

恢复后继续复用现有 `detectPendingInterrupt(taskId)` 机制：

- graph 中存在 `waiting_for_input` node 且 `listInterrupts()` 返回 open interrupt：
  - assistant 气泡展示 interrupt prompt；
  - 设置 `pendingInterrupt`；
  - 输入框 placeholder 切换为补充信息提示；
  - 用户下一条输入通过 `answerInterrupt()` 继续原 task。
- graph 显示 waiting 但 open interrupt 尚未出现：
  - 保持输入锁定；
  - assistant 气泡显示“正在等待任务给出补充信息”；
  - 继续轮询，避免用户误发新消息。

### 5.4 终态处理

- `completed`：
  - 移除或完成临时恢复态；
  - 刷新正式 conversation messages；
  - 最终 assistant 消息以正式历史展示。
- `failed`：
  - 临时 assistant 气泡显示失败态和友好错误；
  - 输入框恢复可用。
- `cancelled`：
  - 临时 assistant 气泡显示取消态；
  - 输入框恢复可用。

## 6. 数据流设计

### 6.1 恢复入口

新增或抽取一个前端编排函数：

```ts
restoreCurrentConversationTask(conversation: ConversationSummaryResponse): Promise<void>
```

触发点：

1. 登录后恢复本地保存的 conversation。
2. 用户点击历史 conversation。
3. 当前 conversation summary 刷新后发现 `current_task_id` 变化。

### 6.2 恢复步骤

1. 关闭旧 `subscriptionRef`。
2. 清理旧 `currentTaskId`、`currentAssistantId`、`pendingInterrupt`、`taskState`。
3. 加载 conversation messages 并渲染正式历史。
4. 如果 `conversation.current_task_id` 为空，结束恢复。
5. 调 `api.getTask(current_task_id)`：
   - 若 task 不存在或无权限：显示轻量提示，结束恢复。
   - 若 task 已终态：按终态处理，必要时刷新 messages。
   - 若 task active：继续。
6. 追加恢复用 assistant 临时气泡：
   - `id = restored-assistant-${taskId}`。
   - `activityText = 正在恢复任务状态`。
   - `reasoningRequested` 可从历史 task metadata 不可得时默认为 false；后续 replay 有 reasoning event 再显示。
7. 设置：
   - `currentTaskId = taskId`
   - `currentAssistantId = restoredAssistantId`
   - `taskState = restoring/running 状态`
8. 调 `subscribeToTask(taskId, restoredAssistantId)`。
9. 依赖 SSE replay 调用现有 `applyTaskEvent()` 重建状态。
10. 启动现有 waiting input 检测。

### 6.3 为什么不使用 unfinished tasks 作为恢复主入口

`listConversationTasks(scope=unfinished)` 能返回多个 unfinished tasks，但这些更多用于取消、删除和残留清理。产品主路径中，同一 conversation 是单任务串行，`current_task_id` 是当前交互任务的唯一可信来源。

如果 `current_task_id` 为空但 unfinished tasks 仍存在，MVP 不主动恢复这些残留任务。删除 conversation、取消当前对话任务等兜底路径仍可继续清理所有 unfinished tasks。

## 7. 前端组件与状态边界

### 7.1 保持单任务状态模型

不引入 `runningTasksById`，继续使用：

- `currentTaskId`
- `currentAssistantId`
- `taskState`
- `pendingInterrupt`
- `subscriptionRef`

这与后端单 conversation 串行任务模型一致，避免为异常残留状态引入多任务 UI。

### 7.2 复用 task event reducer

继续使用 `frontend/src/domain/taskEvents.ts`：

- `applyTaskEvent()` 处理 replay 和实时事件。
- `seenEventIds` 保证 replay / reconnect 不重复追加。
- 必要时补充恢复态初始状态 helper，例如 `createRestoringTaskState()`。

### 7.3 临时 assistant 气泡

恢复气泡和初次提交气泡使用同一 `ConversationMessage` 结构。差异只在创建来源：

- 初次提交：用户点击发送后立即创建。
- 刷新恢复：确认 `current_task_id` active 后创建。

## 8. 错误处理

### 8.1 `current_task_id` 失效

`getTask()` 返回 404、权限错误或其它不可恢复错误时：

- 不创建恢复气泡，或移除已创建的恢复气泡。
- 输入框恢复可用。
- 显示 toast：`任务状态已失效，请刷新历史或重新提交。`

### 8.2 SSE 失败

沿用现有 `handleEventStreamError()` 思路：

- 显示“事件流暂时中断，正在查询任务状态”。
- 查询 `getTask()`：
  - completed：加载 artifacts / 刷新 messages。
  - failed：标记失败。
  - cancelled：标记取消。
  - active：保留恢复气泡，显示等待同步提示。

### 8.3 切换 conversation

切换 conversation 前必须：

- 关闭旧 SSE subscription。
- 清空旧 task state。
- 清空旧 pending interrupt。
- 清空 pending assistant patches。

新 conversation 再按 `current_task_id` 恢复。

## 9. 测试计划

### 9.1 `frontend/src/App.test.tsx`

新增/扩展以下测试：

1. **刷新后恢复 current task**
   - localStorage 指向历史 conversation。
   - `listConversations()` 返回该 conversation 且 `current_task_id=task-1`。
   - `listConversationMessages()` 返回正式历史 user message。
   - `getTask(task-1)` 返回 running。
   - EventSource replay 返回 `task.accepted`、`task.graph_created`、`node.started`、`main_agent.output_delta`。
   - 断言恢复的 assistant 气泡显示 replay 出来的文本和运行状态。

2. **current task completed 自愈**
   - `getTask(task-1)` 返回 completed。
   - 前端刷新 messages。
   - 不保留恢复气泡。
   - 输入框可用。

3. **恢复 waiting_for_input**
   - `getTask()` 返回 running。
   - `getTaskGraph()` 返回 `waiting_for_input` node。
   - `listInterrupts()` 返回 open interrupt。
   - 断言 assistant 气泡显示补充 prompt，输入框进入 interrupt answer 模式。

4. **current_task_id 失效**
   - `getTask()` 抛 404 / ApiError。
   - 断言不恢复运行态，显示友好提示，输入框可用。

5. **切换 conversation 清理旧订阅**
   - 从有 running task 的 conversation 切到另一个 conversation。
   - 断言旧 EventSource `close()` 被调用。
   - 断言旧恢复气泡不残留。

### 9.2 `frontend/src/domain/taskEvents.test.ts`

补充 replay 场景：

- 多个 replay event 能恢复 streaming text、reasoning 和 Skill 状态。
- 重复 replay event 不重复追加文本或状态行。

### 9.3 API 测试

MVP 不要求新增 API。若现有覆盖不足，可补窄测试确认：

- conversation list response 包含 `current_task_id`。
- task events endpoint 对已落库事件支持 replay。

## 10. 验收标准

1. 用户提交任务后刷新页面，再进入同一 conversation，能看到 assistant 气泡仍处于运行/同步状态。
2. SSE replay 能尽量恢复已生成文本、reasoning 和 Skill 进度。
3. task 完成后，正式 assistant 历史消息展示，临时恢复态不残留。
4. waiting_for_input 任务刷新后仍能继续补充。
5. 只恢复 `conversation.current_task_id`，不展示多任务面板。
6. `current_task_id` 失效、终态、SSE 中断都有友好降级。
7. 切换 conversation 不串流，不泄漏旧订阅。

## 11. 后续可选增强

- 如果生产中频繁出现 `current_task_id` 为空但 unfinished tasks 残留，可另行设计残留任务诊断和清理提示。
- 如果恢复流程中的前端请求过多，再考虑新增后端 `conversation runtime snapshot` 聚合 API。
- 如果未来产品明确支持同一 conversation 多任务并行，再重新设计多任务面板；当前不提前实现。
