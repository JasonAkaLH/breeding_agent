# PRD 02: Pending Skill Context Continuation

日期：2026-05-20  
状态：待实施  
依赖：`01-frontend-slash-skill-command-mvp.md`  
范围：slash 强制调用 Skill 后，信息不足时的后端持久化待补全上下文与下一轮续接。

## 1. 背景

Slash command 表示用户明确指定某个 Skill。若用户给出的输入不足以满足该 Skill 的信息需求，并且该 Skill 没有自身多轮补全能力，系统不能只失败或丢失上下文。它应该把这次调用作为正式对话历史保存，并在用户下一轮补充信息时继续优先调用同一个 Skill。

该问题主要是后端状态、存储、生命周期与路由优先级问题，不应由前端内存状态承载。

当前相关代码锚点：

- Message model 无 metadata：`src/core/models.py:90-99`
- Task model 有 routing / requested capability 但无 arbitrary metadata：`src/core/models.py:101-113`
- SQLite message/task schema：`src/storage/sqlite/models.py:100-126`
- Runtime submit flow：`src/api/runtime.py:277-377`
- Existing task routing：`src/orchestration/workflow_router.py:14-21`
- Existing Skill provider：`src/orchestration/skill_workflow_provider.py:31-78`

## 2. 目标

当 slash 强制调用的信息不足且不能通过现有 interrupt / waiting_for_input 机制补全时，后端持久化 pending Skill context。用户下一轮普通输入补充信息时，后端优先复用该 context 并继续调用同一个 Skill。

## 3. 非目标

- 不改变 PRD 01 的前端 picker / badge / submit 交互。
- 不要求前端判断 Skill 参数是否完整。
- 不做复杂自然语言取消意图识别；MVP 只要求新 slash 覆盖、成功清理、显式取消可选。
- 不伪造 Skill 已支持多轮；支持 interrupt 的 Skill 继续走现有 interrupt 流程。

## 4. 核心语义

### 4.1 路由优先级

1. 新 slash command：最高优先级，覆盖任何旧 pending Skill context。
2. Existing pending Skill context：当本轮无 slash 且 context 仍 pending 时，优先继续原 Skill。
3. 无 slash 且无 pending：走现有 LLM 自动路由。

### 4.2 信息不足处理

当强制 Skill 因信息不足无法执行：

- 如果 Skill 支持 `waiting_for_input` / interrupt：
  - 不创建 pending Skill context。
  - 继续使用现有 interrupt lifecycle。

- 如果 Skill 不支持 interrupt：
  - 写入 assistant 正式消息，说明缺少的信息。
  - 持久化 pending Skill context。
  - 任务应以可理解的完成/等待状态收尾，避免 conversation busy 卡住下一轮输入。

### 4.3 下一轮续接

当用户下一轮普通输入补充信息：

- 后端查找 conversation 最近 active pending Skill context。
- 构造合并后的 Skill input：原始问题 + 缺失提示 + 用户补充。
- `requested_capability_id` 使用原 pending `capability_id`。
- 调用完成后清理 context。

## 5. 数据模型建议

当前 Message / Task 不适合直接承载 arbitrary pending context，因此建议新增一个小型持久化模型，而不是藏在前端或非结构化 message content 中。

候选方案：

### 方案 A：新增 SQLite table（推荐）

新增 `conversation_pending_skill_context`：

- `context_id` TEXT primary key
- `conversation_id` TEXT indexed
- `account_id` TEXT nullable / optional
- `capability_id` TEXT not null
- `skill_name` TEXT not null
- `source_task_id` TEXT not null
- `source_message_id` TEXT not null
- `original_user_message` TEXT not null
- `missing_requirements` JSON/TEXT not null
- `status` TEXT not null：`pending_user_input | consumed | cancelled | superseded`
- `created_at` DateTime
- `updated_at` DateTime

优点：查询和生命周期清晰；不会污染 Message / Task contract。  
缺点：需要 storage schema / repository / tests。

### 方案 B：基于事件回放

用 task event 记录 pending context，下一轮 submit 时扫描最近事件。

优点：少建表。  
缺点：查询复杂，状态更新/消费语义不清晰，不适合长期维护。

### 方案 C：扩展 Conversation metadata

若后续已有 conversation-level metadata，可存当前 pending context。

优点：模型直观。  
缺点：当前锚点未显示已有适配字段，可能引入更大 schema 改动。

推荐采用方案 A。

## 6. 后端实现要求

### 6.1 Repository / storage

新增 repository 能力：

- `save_pending_skill_context(context)`
- `get_active_pending_skill_context(conversation_id)`
- `mark_pending_skill_context_consumed(context_id)`
- `mark_pending_skill_context_cancelled(context_id)`
- `mark_pending_skill_context_superseded(conversation_id)`

### 6.2 Runtime submit flow

修改 `ApiRuntime.submit_message()`：

1. 先识别本轮是否 explicit slash / force capability。
2. 若 explicit force capability：
   - 如果同 conversation 有 pending context，标记旧 context 为 `superseded`。
   - 按本轮 explicit capability 执行。
3. 若无 explicit capability：
   - 查询 active pending context。
   - 若存在，构造续接 request：
     - `requested_capability_id=context.capability_id`
     - metadata 加入 `continued_from_pending_skill_context`。
     - user message 可保持用户补充原文；Skill input payload / orchestration metadata 应包含 original +补充。
   - 若不存在，保持 auto routing。

### 6.3 信息不足写入点

需要在 Skill 执行边界定义一个明确的“信息不足且无法 interrupt”的错误/结果 contract。MVP 可先支持 allowlisted code / output shape，例如：

```json
{
  "status": "needs_input",
  "missing_requirements": ["查询对象", "数据库范围"],
  "message": "请补充查询对象和数据库范围。",
  "supports_interrupt": false
}
```

当 runtime 观察到该结果时：

- 保存 pending context。
- 写 assistant message：`message`。
- 结束当前 task。

如果现有 Skill executor 已有等价错误码，应复用现有 typed result，不新增平行协议。

## 7. 验收标准

1. 强制 Skill 信息不足且不支持 interrupt 时，assistant 正式回复缺失信息。
2. 该回复进入对话历史，刷新后可见。
3. 后端持久化 pending context，不依赖前端内存。
4. 下一轮普通输入优先复用 pending capability，而不是 LLM 自动选其他 Skill。
5. 新 slash command 覆盖旧 pending context，并将旧 context 标记为 `superseded`。
6. Skill 成功执行后，pending context 标记为 `consumed`。
7. 已支持 interrupt 的 Skill 不重复创建 pending context。
8. conversation 不应因 pending context 保持 busy；用户必须能继续输入补充信息。

## 8. 测试计划

### 8.1 Storage tests

新增/扩展 `tests/storage`：

- 保存 pending context。
- 查询 conversation active pending context。
- consumed / cancelled / superseded 状态转换。
- 同 conversation 仅一个 active pending context。

### 8.2 API / runtime tests

新增 `tests/api/test_pending_skill_context.py`：

- force Skill needs_input -> assistant 缺失提示 + pending context。
- 下一轮普通消息复用 pending `capability_id`。
- 新 slash force request supersedes old pending context。
- successful continuation consumes context。
- interrupt-capable path 不创建 pending context。

### 8.3 Orchestration tests

- pending continuation 的 `requested_capability_id` 进入 `SkillWorkflowProvider`。
- metadata 包含 `continued_from_pending_skill_context`，但用户 metadata 不能单独伪造强制 Skill。

## 9. 验证命令

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
```

如只新增窄测试文件，可先运行：

```bash
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
```

## 10. 风险与缓解

- **状态模型过重**：限制为单表、单 active context，不做复杂 pending 队列。
- **误续接错误 Skill**：新 slash 优先覆盖；成功后消费；可设置 context 过期策略作为后续增强。
- **conversation busy 卡住补充输入**：needs_input 无 interrupt path 必须结束当前 task，让下一轮可提交。
- **与现有 interrupt 重叠**：支持 interrupt 的 Skill 继续走 interrupt，不创建 pending context。
- **schema 改动影响 Rust migration**：新增 storage tests，若涉及 Rust sidecar contract，必须另行更新对应 contract / gate；本 PRD 不宣称 Rust canonical path 完成。

## 11. 与 PRD 01 的关系

PRD 01 完成后，即使没有本 PRD，用户仍可通过 slash 强制调用 Skill。本 PRD 只补齐信息不足后的跨轮续接语义，不能阻塞 PRD 01 上线。
