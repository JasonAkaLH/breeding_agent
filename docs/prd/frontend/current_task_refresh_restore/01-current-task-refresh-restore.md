# PRD: Current Task Refresh Restore

日期：2026-05-20  
状态：已固化，长期交付级计划，已完成 document-perfectization 审查  
设计来源：`docs/superpowers/specs/2026-05-20-current-task-refresh-restore-design.md`

## 1. Problem Statement

当前前端初次发送消息时会立即创建 user message + 临时 assistant 气泡，并通过 SSE 持续展示任务进度。但用户刷新浏览器或重新进入历史 conversation 后，前端只加载正式 history messages，不会根据 `conversation.current_task_id` 恢复正在运行的临时 assistant 气泡。用户会误以为任务丢失，且无法直观看到任务仍在运行、是否需要补充信息或是否已经完成。

本 PRD 的目标是：刷新 / 重新进入 conversation 后，只要该 conversation 的 `current_task_id` 指向 active task，前端必须恢复与初次发送消息时一致的单任务 assistant 临时气泡运行态，并继续消费既有 SSE replay + realtime events。该交付按长期可维护产品能力建设，不能以临时验证或只覆盖 happy path 的方式降级；实现必须形成状态机、错误处理、测试和回归验证闭环。

## 2. Users, Stakeholders, and Affected Systems

| Actor / System | Impact |
| --- | --- |
| 业务用户 | 刷新页面或重新进入历史会话后，能继续看到当前任务的输出、进度、等待补充状态和停止入口。 |
| 前端业务对话台 | 需要把 history loading 与 current task runtime restore 串接起来，同时保持单任务 UI 状态模型。 |
| FastAPI task / conversation API | 不新增 API；继续提供 `ConversationSummaryResponse.current_task_id`、task summary、task events、graph、interrupts、messages。 |
| SSE event replay | 继续作为恢复生成文本、reasoning、Skill progress 的唯一实时 / replay 来源。 |

## 3. Current State and Evidence

### 3.1 Frontend evidence

- `frontend/src/App.tsx:140-150`：当前聊天页已经是单任务运行态结构：`messages`、`taskState`、`currentTaskId`、`currentAssistantId`、`pendingInterrupt`、`subscriptionRef`。
- `frontend/src/App.tsx:169-183`：自动登录恢复只设置 `authUser` 与 localStorage conversation id，没有加载该 conversation messages / current task。
- `frontend/src/App.tsx:218-229`、`frontend/src/App.tsx:238-241`：登录后会拉取 history list，但 `refreshConversationHistory()` 当前只更新侧栏，不驱动当前 conversation 的运行态恢复。
- `frontend/src/App.tsx:282-360`：只要存在 active `currentTaskId`，前端会轮询 `getTaskGraph()` + `listInterrupts()`，并能恢复 open interrupt prompt。
- `frontend/src/App.tsx:363-378`：显式登录后只加载 history list，不加载当前 conversation messages / current task。
- `frontend/src/App.tsx:420-437`：`handleSelectConversation()` 只加载历史 messages 并清空 runtime；这是点击历史 conversation 时的恢复缺口。
- `frontend/src/App.tsx:579-628`：初次提交路径已经创建 user + 临时 assistant 气泡、设置 submitting state，并在 accepted 后订阅 SSE。
- `frontend/src/App.tsx:755-797`：`subscribeToTask()` + `handleTaskEvent()` 已把 SSE event replay 到 assistant 气泡文本、reasoning、Skill status 和 activity。
- `frontend/src/App.tsx:799-835`：SSE 错误时已有 task summary / artifact fallback，但 restore 场景仍需补齐 terminal 后刷新正式 messages 的语义。
- `frontend/src/domain/taskEvents.ts:38-55`：已有 initial / submitting task state，可新增 restoring state 而不改 UI 状态机枚举。
- `frontend/src/domain/taskEvents.ts:87-210`：`applyTaskEvent()` 已用 `seenEventIds` 去重并支持 output、reasoning、completed、failed、cancelled、skill progress replay。
- `frontend/src/api/client.ts:56-65`、`frontend/src/api/client.ts:179-202`：已有 list conversations/messages、task、graph、interrupts、artifacts API client，无需新增后端 endpoint。
- `frontend/src/App.test.tsx:86-108`：已有 fake EventSource factory，可直接扩展 restore replay 测试。
- `frontend/src/domain/taskEvents.test.ts:129-160`：已有 output/reasoning 去重测试，可补 restoring state 测试而不重写 reducer 测试体系。

### 3.2 Backend evidence

- `src/api/routes/tasks.py:33-38`、`src/api/runtime.py:105-110`：unfinished / active task statuses 是 `accepted`、`planning`、`running`、`cancelling`。
- `src/lifecycle/conversation_guard.py:12-17`：同一 conversation 存在 active task 时会抛 `ConversationBusyError`，正常业务路径是单任务串行。
- `src/api/runtime.py:294-310`：submit message 前调用 conversation guard。
- `src/api/runtime.py:337-350`：创建 / 更新 conversation 时写入 `current_task_id=task_id`。
- `src/api/runtime.py:637-643`：任务执行完成 / 失败后会尝试同步 assistant history 并清理 current task。
- `src/api/runtime.py:956-967`：只有当 task terminal 且仍匹配 conversation current task 时才清空 `current_task_id`。
- `src/api/routes/conversations.py:28-37`：conversation summary 已返回 `current_task_id`。
- `src/api/routes/conversations.py:45-55`：busy conversation 返回 HTTP 409。
- `src/api/routes/tasks.py:116-124`：task event stream 已基于 `runtime.iter_frontend_events(task_id)` 提供 replay / live 事件。

## 4. Delivery Principles

1. **长期交付级能力**：本能力必须作为业务对话台的稳定恢复机制交付，不能以“临时可跑”为目标。
2. **单一事实源**：恢复入口只以后端 `conversation.current_task_id` 为事实源；前端不得引入第二套 unfinished task 推断。
3. **状态机闭环**：active、terminal、waiting-for-input、stale/error 都必须有明确 UI 状态、清理动作和测试。
4. **复用现有协议**：优先复用 messages / task summary / SSE replay / graph / interrupts；除非后续证据证明不足，否则不新增后端 snapshot API。
5. **回归安全**：实现不得破坏初次提交、slash command、upload、artifact、cancel、history copy 等既有路径。

## 5. Goals and Non-goals

### 5.1 Goals

1. 刷新或重新进入历史 conversation 后，如果 `current_task_id` 是 active task，恢复 assistant 临时气泡运行态。
2. 恢复过程必须尽量复用初次提交后的 UI 行为：输入锁定、停止按钮可用、SSE 更新同一个 assistant 气泡。
3. 恢复内容必须来自既有 backend API / SSE replay，不新增 snapshot API。
4. waiting-for-input 状态必须能恢复为可继续补充的 interrupt prompt。
5. terminal / stale current task 必须自愈，避免输入框永久锁定。

### 5.2 Non-goals

1. 不做多任务运行面板。
2. 不以 `listConversationTasks(scope='unfinished')` 作为恢复入口；该 API 只保留给取消兜底。
3. 不恢复非 `current_task_id` 的残留 unfinished tasks。
4. 不新增后端 snapshot / resume API。
5. 不保证恢复刷新前本地 UI 展开状态、精确滚动位置或未落库 draft。
6. 不改变后端 conversation 串行任务模型。
7. 不改变 slash command、上传文件、artifact rendering 的既有业务契约。

## 6. Functional Requirements

| ID | Requirement |
| --- | --- |
| FR-1 | 前端在自动登录恢复和显式登录后，必须尝试加载 localStorage 指向 conversation 的正式 messages；若 history summary 中存在同 conversation 且 `current_task_id` 非空，必须进入 current task restore 流程。 |
| FR-2 | 用户点击历史 conversation 时，前端必须先加载该 conversation messages，再基于该 conversation summary 的 `current_task_id` 决定是否 restore。 |
| FR-3 | Restore 入口必须只接受 `current_task_id`；`listConversationTasks(scope='unfinished')` 不得用于恢复 UI。 |
| FR-4 | Active task statuses 必须按后端 unfinished contract 判定：`accepted`、`planning`、`running`、`cancelling`。Terminal statuses 必须是 `completed`、`failed`、`cancelled`。未知 status 必须 fail-safe：提示并释放输入，不保持 active lock。 |
| FR-5 | Active restore 必须追加一个恢复用 assistant 临时气泡，初始 activity 文案明确表示正在恢复 / 同步。 |
| FR-6 | Active restore 必须设置 `currentTaskId`、`currentAssistantId`、`taskState`，然后通过现有 `subscribeToTask(taskId, assistantId)` 消费 task events。 |
| FR-7 | SSE replay 的 output、reasoning、Skill status、activity 必须通过现有 `applyTaskEvent()` 更新恢复气泡；重复 event id 不得重复追加。 |
| FR-8 | Restore 后若 graph / interrupts 显示 node `waiting_for_input` 且存在 open interrupt，必须显示 interrupt prompt，并允许下一条输入调用 `answerInterrupt()` 继续同一 task。 |
| FR-9 | Restore 后 task completed 时，必须刷新正式 conversation messages 并移除 / 替换恢复临时气泡，避免临时气泡与正式 assistant history 重复展示。 |
| FR-10 | Restore 时 `getTask()` 失败、task missing / forbidden、failed、cancelled 或未知 status，必须友好提示并释放 input lock。 |
| FR-11 | 必须保持现有 active task 交互语义：当当前页面已经处于 active task 运行态时，不扩大为可随意切换 conversation 的新模式；若既有 UI 阻止 active 时切换 conversation，本 PRD 必须继续保持。 |
| FR-12 | 登出、删除当前 conversation、新建 conversation 或切换非 active conversation 时，必须关闭旧 subscription 并清理本地 runtime，避免事件串流到错误 conversation。 |
| FR-13 | 自动登录、显式登录、history refresh、conversation switch 的异步结果必须只作用于其发起时的目标 conversation；过期结果必须丢弃。 |
| FR-14 | Restore 相关 active/terminal/stale status 判断必须集中定义，禁止散落字符串判断。 |

## 7. Non-functional Requirements

| Category | Requirement |
| --- | --- |
| Reliability | stale `current_task_id` 不得导致永久输入锁定；任意 restore 失败路径都必须释放 runtime 并提示。 |
| Compatibility | 不新增后端 endpoint，不改变现有 DTO 字段含义，不破坏 slash command / upload metadata / artifact rendering。 |
| Privacy / Security | 前端不得从 event payload 展示 debug / audit / non-final main-agent response；继续依赖 `applyTaskEvent()` 的 visible response_role 过滤。 |
| Accessibility | 恢复后的状态文案、停止按钮、interrupt prompt 必须沿用现有可访问控件和 role，不新增不可达的自定义控件。 |
| Performance | Restore 不得轮询 conversation task list；每次 restore 最多一次 `getTask()`，active 后交给 SSE + 既有 interrupt polling。 |
| Observability / Debuggability | 失败提示必须能区分 history load 失败、task stale / forbidden、SSE 中断等用户可理解状态；不要求新增 telemetry。 |
| Maintainability | Restore 流程必须集中在明确 helper / 小型状态机边界内，禁止在多个 effect / handler 中复制 active/terminal/stale 判断。 |
| Race Safety | 自动登录、history refresh、conversation switch、SSE replay 必须有 generation / stable id / cleanup 保护，避免过期 async 结果写入当前 conversation。 |

## 8. UX / Flow Requirements

### 8.1 自动登录 / 刷新恢复

1. `api.me()` 成功后得到用户。
2. 从 localStorage 读取或创建 conversation id。
3. 拉取 conversation history list。
4. 若 history list 包含当前 conversation：
   - 先加载该 conversation messages；
   - 如果 `current_task_id` active，append 恢复 assistant 气泡并订阅 SSE；
   - 如果 `current_task_id` terminal / stale，按 terminal 自愈；
   - 如果 `current_task_id` null，仅展示 messages / idle 状态。
5. 若 history list 不包含当前 conversation，保持空 conversation workspace。

### 8.2 点击历史 conversation

1. 若当前页面已有 active task，保持现有 active guard，不允许通过本 PRD 引入“运行中自由切换 conversation”的新行为。
2. 若当前页面 idle，保存 selected conversation id。
3. 清理旧 idle runtime / stale subscription。
4. 加载目标 conversation messages。
5. 基于目标 conversation summary 的 `current_task_id` restore 或保持 idle。

### 8.3 Active task restore

1. `api.getTask(taskId)` 返回 active status。
2. 创建 `restored-assistant-${taskId}` 或等价稳定 id 的 assistant message。
3. 气泡初始 `activityText` 必须是“正在恢复任务状态 / 正在同步任务输出”一类明确文案。
4. 设置 task runtime state 后订阅 task events。
5. replay event 到达后，逐步替换 activity、输出文本、reasoning、Skill status。

### 8.4 Terminal / stale self-heal

1. `completed`：同步正式 messages；不保留恢复气泡；释放 input。
2. `failed` / `cancelled`：提示任务已结束或未完成；不保留恢复气泡；释放 input。
3. `getTask()` 404 / 403 / network failure：提示任务状态暂时无法恢复；释放 input；保留正式 history messages。
4. 未知 status：按 stale / unsupported 状态处理，释放 input。

## 9. Implementation Plan

### Step 1 — 先补失败测试锁定恢复入口

文件：`frontend/src/App.test.tsx`

新增失败测试覆盖：

- 自动登录 / localStorage conversation restore：`listConversations()` 返回 `current_task_id`，`listConversationMessages()` 返回历史 user message，`getTask()` 返回 running，fake EventSource replay 输出文本；断言恢复气泡与输入锁定。
- 点击历史 conversation restore：目标 conversation 有 `current_task_id` 时触发同样恢复流程。
- current_task_id null：不调用 `getTask()` / 不打开 EventSource。
- active task 时点击另一个 conversation 仍保持既有阻断语义，不引入自由切换。

### Step 2 — 抽取本地 helper，降低 `App.tsx` 重复

文件：`frontend/src/App.tsx`

建议增加轻量内部 helper：

```ts
async function loadConversationMessages(targetConversationId: string): Promise<ConversationMessage[]>;
async function loadConversationHistory(): Promise<ConversationSummaryResponse[]>;
function clearCurrentTaskRuntime(options?: { closeSubscription?: boolean }): void;
function isTerminalTaskStatus(status: string): boolean;
function isActiveTaskStatus(status: string): boolean;
```

要求：

- `loadConversationMessages()` 只负责 `api.listConversationMessages()` + `messageFromHistory()` mapping。
- `loadConversationHistory()` 返回 conversations 并更新 `conversationHistory`，避免 `refreshConversationHistory()` 只产生副作用导致 startup restore 难串接。
- `clearCurrentTaskRuntime()` 统一清理 `currentTaskId`、`currentAssistantId`、`pendingInterrupt`、`taskState`、pending assistant patches，并按参数关闭 subscription。
- 不引入新依赖，不新增全局状态库。

### Step 3 — 增加恢复初始 task state

文件：`frontend/src/domain/taskEvents.ts`

新增：

```ts
export function createRestoringTaskState(): TaskEventState
```

语义必须满足：

- `phase: 'running'`
- `statusText: '正在恢复任务状态'`
- `currentActivityText: '正在同步任务输出'`
- 其它字段继承 `createInitialTaskEventState()`。

不新增 `TaskPhase='restoring'`，避免扩大 UI active 判断分支；恢复态本质上是 running task 的本地展示状态。

### Step 4 — 实现 `restoreCurrentConversationTask()`

文件：`frontend/src/App.tsx`

建议签名：

```ts
async function restoreCurrentConversationTask(conversation: ConversationSummaryResponse): Promise<void>;
```

流程：

1. 读取 `conversation.current_task_id`。
2. 为空：只执行 runtime cleanup，返回。
3. 调 `api.getTask(taskId)` 获取最新状态。
4. terminal：
   - completed：重新 `loadConversationMessages(conversation.conversation_id)` 并替换 `messages`；
   - failed / cancelled：提示任务已结束或未完成；
   - 所有 terminal 均清理 runtime，不保留恢复气泡。
5. active：
   - 创建稳定恢复气泡 id：`restored-assistant-${taskId}`；
   - append 一个空 assistant 临时气泡，`activityText` 来自 `createRestoringTaskState()`；
   - 设置 `currentTaskId`、`currentAssistantId`、`taskState(createRestoringTaskState())`；
   - 写 `taskPresentationModesRef.current.set(taskId, mode)`；
   - 调 `subscribeToTask(taskId, restoredAssistantId)`。
6. `getTask()` 失败或未知 status：提示“任务状态已失效，请刷新历史消息”，释放 input。

### Step 5 — 串接自动登录、显式登录与历史切换恢复

文件：`frontend/src/App.tsx`

- 自动登录 (`api.me()` 成功后)：
  - 设置 `authUser` 和 conversation id 后，必须由后续 effect 或 helper 加载 history + messages + restore；避免只显示空欢迎页。
  - 必须防止 React effect 双调用导致重复 append 恢复气泡，可用 `restoredTaskIdsRef` / `restoreGenerationRef` / stable assistant id 去重。
- 显式 `handleLogin()`：
  - 复用同一 initialization helper，避免自动登录和显式登录行为分叉。
- `handleSelectConversation()`：
  - 保持 active guard；当前页 active 时不允许切换。
  - idle 时 close stale subscription / cleanup，然后加载 messages 并 restore 目标 conversation。
- 异步竞态防护：
  - 每次 initialization / restore / conversation switch 生成递增 generation token；
  - `listConversationMessages()`、`getTask()`、EventSource callbacks 写 UI 前必须确认 generation 与 conversation id 仍匹配；
  - cleanup 时关闭旧 subscription 并使旧 generation 失效。

### Step 6 — 保持 SSE replay 和 completed 自愈闭环

文件：`frontend/src/App.tsx`、`frontend/src/domain/taskEvents.ts`

- 恢复态复用现有 `subscribeToTask()`，不新增事件协议。
- `applyTaskEvent()` 继续使用 `seenEventIds` 去重，避免 replay + live 重复追加文本。
- `task.completed` 后仍走 `loadArtifacts()`；若 assistant id 属于 restored task，则还必须刷新正式 messages 或用正式 history 替换临时气泡，避免重复最终答案。
- SSE error 下沿用 `handleEventStreamError()`；若 task terminal，释放 runtime；若仍 active，保留恢复气泡并提示事件流暂时中断。

### Step 7 — waiting-for-input restore

文件：`frontend/src/App.tsx`

- 恢复后只要设置了 `currentTaskId` 且 `taskState.phase` active，现有 `useEffect` 会调用 `detectPendingInterrupt()`。
- 补充测试确认 graph waiting + open interrupt 能显示 interrupt prompt，并且提交补充输入后调用 `answerInterrupt()`。
- 当进入 `waiting_for_input` 时关闭 subscription，符合现有初次运行路径。

### Step 8 — 收敛清理和回归

文件：`frontend/src/App.tsx`、`frontend/src/App.test.tsx`、`frontend/src/domain/taskEvents.test.ts`

- 登出、删除当前 conversation、新建 conversation、idle 切换 conversation 时统一通过 cleanup helper 关闭 subscription。
- 确认 pending uploads、selected slash command、pending interrupt state 不跨 conversation 泄漏。
- 保持 App 变更局部化；如果 helper 超过合理复杂度，后续单独 PRD 抽 `useCurrentTaskRuntime` hook，本 PRD 不做大重构。

## 10. Acceptance Criteria

| ID | Criteria |
| --- | --- |
| AC-1 | 刷新后自动登录进入有 active `current_task_id` 的 conversation，页面先显示历史 messages，再显示一个恢复 assistant 临时气泡。 |
| AC-2 | 点击 idle 状态下的历史 conversation，若目标 summary 有 active `current_task_id`，同样恢复 assistant 临时气泡。 |
| AC-3 | 恢复气泡通过 SSE replay 显示 output_delta；reasoning_delta 能进入 reasoning 区；skill.progress / node.started 能恢复 Skill 状态行。 |
| AC-4 | 恢复期间输入框锁定，停止按钮可用，且停止仍能取消当前 conversation unfinished tasks。 |
| AC-5 | Active task completed 后，前端刷新正式 conversation messages，不留下重复临时 assistant 气泡。 |
| AC-6 | Active task waiting-for-input 后刷新，前端能恢复 interrupt prompt，用户下一条输入调用 `answerInterrupt(taskId, interruptId, payload)`。 |
| AC-7 | `current_task_id` 为 null 时，即使 conversation 有其它 unfinished 残留任务，也不恢复运行态，且恢复流程不调用 `listConversationTasks()`。 |
| AC-8 | `current_task_id` 指向 missing / forbidden / failed / cancelled / unknown task status 时，前端友好提示并释放输入框。 |
| AC-9 | 当前页已有 active task 时，点击其它 conversation 仍保持既有 active guard，不引入运行中自由切换。 |
| AC-10 | 登出、删除当前 conversation、新建 conversation 或 idle 切换 conversation 时，旧 EventSource `close()` 被调用，不串流到新 conversation。 |
| AC-11 | 初次发送消息路径保持不变：仍立即追加 user + 临时 assistant 气泡，accepted 后订阅 SSE。 |
| AC-12 | 自动登录、显式登录、history refresh、conversation switch 的过期异步结果不得写入当前 conversation。 |
| AC-13 | 实现通过 `npm test -- --run`、`npm run typecheck`、`npm run build`，且新增逻辑无重复 status 字符串判断和一次性 workaround。 |

## 11. Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| 自动登录 / 显式登录 restore 逻辑分叉 | 刷新恢复与手动登录表现不一致 | 抽共享 initialization helper；测试覆盖 auto-auth 和 explicit login。 |
| React effect / history refresh 重复触发 restore | 重复 assistant 气泡或重复订阅 | 使用 stable restored assistant id、generation guard 或 restored task ref；测试断言单气泡。 |
| completed 后临时气泡与正式 history 重复 | 用户看到重复回答 | restored task completed 后刷新 messages 并清 runtime；测试覆盖不重复。 |
| replay/live 重复事件 | 文本重复追加 | 继续依赖 `seenEventIds`，补 reducer 去重测试。 |
| stale `current_task_id` | 输入框永久锁定 | `getTask()` 失败、terminal 或未知 status 均释放 runtime 并提示。 |
| 错误扩大 UX 范围为多 conversation active 切换 | 违背“初次聊天流程什么样，刷新后还是什么样” | 保持现有 active guard；只在 idle / restore initialization 时切换 conversation。 |
| App.tsx 继续膨胀 | 可维护性下降 | 本轮只抽局部 helper；复杂度升高时后续拆 hook。 |

## 12. Assumptions and Resolved Decisions

| Type | Item | Handling |
| --- | --- | --- |
| Assumption | `current_task_id` active statuses 与后端 unfinished statuses 保持一致：`accepted`、`planning`、`running`、`cancelling`。 | 已由 `src/api/routes/tasks.py:33-38`、`src/api/runtime.py:105-110` 证明；实现中应集中定义，避免散落字符串。 |
| Assumption | `runtime.iter_frontend_events(task_id)` 对 task events 提供足够 replay，以恢复 output / reasoning / Skill progress。 | 复用现有 SSE reducer；若未来发现 broker 不持久 replay，应另开后端事件持久化 PRD，本 PRD 不新增 snapshot API。 |
| Decision | Completed restore 的最终展示以正式 conversation history messages 为准。 | 允许短暂 artifact fallback，但最终必须 reload messages 并移除 / 替换恢复临时气泡；测试约束最终 UI 不重复。 |

## 13. Verification Steps

最小验证：

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

若实现中触及 API DTO 或后端 conversation / task 生命周期，再追加：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

人工 smoke：

1. 启动全栈：`python scripts/run_fullstack_dev.py`。
2. 发送一条触发较慢 Skill / LLM 的消息。
3. 在任务运行中刷新浏览器。
4. 重新进入该 conversation，确认恢复 assistant 气泡、已生成内容、Skill 状态、停止按钮和 waiting input 行为一致。

## 14. Follow-up Staffing Guidance

### `$ralph` sequential implementation

推荐用于本 PRD：单 owner 更适合在 `App.tsx` 状态机中小步 TDD 修改并持续回归。

建议 handoff：

```text
$ralph implement .omx/plans/prd-20260520-current-task-refresh-restore.md with .omx/plans/test-spec-20260520-current-task-refresh-restore.md
```

### `$team` parallel implementation

若选择并行，建议拆成 2 lane：

1. frontend runtime lane：`executor`，负责 `frontend/src/App.tsx` 恢复流程与 cleanup helper。
2. frontend test lane：`test-engineer`，负责 `frontend/src/App.test.tsx` 与 `frontend/src/domain/taskEvents.test.ts` 的失败测试和回归补强。

Team verification path：test lane 必须先证明新增测试失败；runtime lane 实现后共同跑 `cd frontend && npm test -- --run && npm run build`，再由 leader 检查是否需要后端 API 回归。

## 15. Stop Condition

当 `.omx/plans/test-spec-20260520-current-task-refresh-restore.md` 中列出的必测场景全部有测试覆盖，`npm test -- --run`、`npm run typecheck` 与 `npm run build` 通过，人工 smoke 能证明刷新后恢复当前 task assistant 气泡运行态，并且实现没有引入一次性特判、重复状态源或不可维护的临时 workaround，即可结束实现。
