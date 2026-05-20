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
- Existing script missing-input output：`src/capabilities/main_agent/executor.py:398-408`、`src/capabilities/main_agent/executor.py:548-595`
- Direct Skill executor missing-input error：`src/capabilities/skill_tool/executor.py:275-313`
- Direct Skill executor interrupt branch：`src/capabilities/skill_tool/executor.py:556-608`
- Completion policy waiting input：`src/orchestration/completion_policy.py:18-29`
- Existing API interrupt resume test：`tests/api/test_message_submission.py:35-68`

## 2. 目标

当 slash 强制调用的信息不足且不能通过现有 interrupt / waiting_for_input 机制补全时，后端持久化 pending Skill context。用户下一轮普通输入补充信息时，后端优先复用该 context 并继续调用同一个 Skill。

本 PRD 的成功状态是：用户不需要重新输入 slash command，也不会被 LLM 自动路由到其他 Skill；系统能基于持久化 pending context 完成同一 Skill 的续接。

## 3. 用户、利益相关方与受影响系统

| 类别 | 说明 | 本 PRD 影响 |
|---|---|---|
| 内部业务用户 | 第一轮强制 Skill 信息不足，第二轮补充信息 | 第二轮普通输入应继续原 Skill。 |
| Skill 作者 / 维护者 | 定义 Skill 必需输入、missing input 结果、interrupt 能力 | 需要使用统一 missing-input / interrupt contract。 |
| API runtime | `src/api/runtime.py` submit / route 入口 | 需要在 auto routing 前检查 pending context。 |
| Storage | SQLite repositories / schema | 需要持久化单 conversation active pending context。 |
| Orchestration / lifecycle | Skill execution result、task completion、interrupt | 需要区分 interrupt path 与 non-interrupt pending context path。 |
| 前端 | 对话历史和普通输入 | 不保存 pending 状态，只展示后端消息并正常提交下一轮输入。 |

## 4. 非目标

- 不改变 PRD 01 的前端 picker / badge / submit 交互。
- 不要求前端判断 Skill 参数是否完整。
- 不做复杂自然语言取消意图识别；MVP 只要求新 slash 覆盖、成功清理、显式取消可选。
- 不伪造 Skill 已支持多轮；支持 interrupt 的 Skill 继续走现有 interrupt 流程。

## 5. 核心语义

### 5.1 路由优先级

1. 新 slash command：最高优先级，覆盖任何旧 pending Skill context。
2. Existing pending Skill context：当本轮无 slash 且 context 仍 pending 时，优先继续原 Skill。
3. 无 slash 且无 pending：走现有 LLM 自动路由。

### 5.2 信息不足处理

当强制 Skill 因信息不足无法执行：

- 如果 Skill 支持 `waiting_for_input` / interrupt：
  - 不创建 pending Skill context。
  - 继续使用现有 interrupt lifecycle。

- 如果 Skill 不支持 interrupt：
  - 写入 assistant 正式消息，说明缺少的信息。
  - 持久化 pending Skill context。
  - 任务应以可理解的完成/等待状态收尾，避免 conversation busy 卡住下一轮输入。

### 5.3 下一轮续接

当用户下一轮普通输入补充信息：

- 后端查找 conversation 最近 active pending Skill context。
- 构造合并后的 Skill input：原始问题 + 缺失提示 + 用户补充。
- `requested_capability_id` 使用原 pending `capability_id`。
- 调用完成后清理 context。

## 6. 数据模型建议

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

## 7. 后端实现要求

### 7.1 Repository / storage

新增 repository 能力：

- `save_pending_skill_context(context)`
- `get_active_pending_skill_context(conversation_id)`
- `mark_pending_skill_context_consumed(context_id)`
- `mark_pending_skill_context_cancelled(context_id)`
- `mark_pending_skill_context_superseded(conversation_id)`

### 7.2 Runtime submit flow

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

### 7.3 信息不足写入点

本 PRD 不允许新增与现有结果平行、无法被测试识别的模糊协议。实现必须先复用或收敛现有 missing-input 信号：

- delegated main-agent Skill script path 已产生 `skill.input_missing` 事件和 output payload：`ok=false`、`error.type=missing_input`、`missing=[...]`、`answer=...`。
- direct Skill executor path 已产生 `CapabilityExecutionError(code=skill_input_missing, metadata={missing:[...]})`。
- interrupt-capable path 已通过 `handler_result.interrupt` 进入 `waiting_for_input`，该路径不得再创建 pending Skill context。

实现必须定义一个内部 helper，例如 `extract_pending_skill_missing_input(result/events/artifacts) -> PendingSkillMissingInput | None`，只接受上述 allowlisted 信号。该 helper 的输出字段必须至少包含：

- `skill_name`
- `capability_id`
- `missing_requirements`
- `assistant_message`
- `source_node_id` 或 `source_task_id`

当 runtime 观察到 helper 返回值且本任务没有 open interrupt 时：

- 保存 pending context。
- 写 assistant message：`assistant_message`。
- 结束当前 task，确保 conversation 不保持 busy。

如果某类 Skill 只能返回普通失败文本而没有 typed missing-input 信号，本 PRD 不要求推断；该 Skill 应先补齐 typed result 或 interrupt 能力。

## 8. 非功能要求

| 维度 | 要求 | 验证方式 |
|---|---|---|
| 持久性 | pending context 必须存储在后端持久化层；刷新前端或重启前端不丢失。 | storage + API 测试。 |
| 一致性 | 同一 conversation 同时最多一个 active pending context。 | repository 测试。 |
| 安全 | 用户 metadata 不能伪造 `continued_from_pending_skill_context` 来强制 Skill；续接必须来自后端已保存 context。 | API / orchestration 测试。 |
| 隐私 | pending context 不保存原始上传文件内容、secret、DB URL 或 provider config；只保存 message text、missing field names 和 safe ids。 | storage 测试 / code review。 |
| 可恢复性 | task 在 non-interrupt missing-input path 中必须终止为允许下一轮提交的状态，不能保持 conversation busy。 | API test。 |
| 可观测性 | 创建、消费、覆盖、取消 pending context 必须产生 audit-safe 事件或可查询状态，便于排查。 | API / storage 测试。 |

## 9. Rollout / Migration

- 本 PRD 涉及存储 schema；实施前必须补 storage migration / bootstrap 兼容测试。
- 如果当前 Rust sidecar / contract 覆盖相关存储路径，必须同步更新 Rust contract 或明确 pending context 仍走 Python-only path，不能宣称 Rust canonical support。
- 上线顺序：先发布 PRD 01；再发布本 PRD 的 storage + runtime + tests。
- 回滚策略：禁用 pending context 检查后，slash 强制调用仍可用；信息不足会退化为普通缺失提示，但不得破坏普通 auto routing。

## 10. 验收标准

1. 强制 Skill 信息不足且不支持 interrupt 时，assistant 正式回复缺失信息。
2. 该回复进入对话历史，刷新后可见。
3. 后端持久化 pending context，不依赖前端内存。
4. 下一轮普通输入优先复用 pending capability，而不是 LLM 自动选其他 Skill。
5. 新 slash command 覆盖旧 pending context，并将旧 context 标记为 `superseded`。
6. Skill 成功执行后，pending context 标记为 `consumed`。
7. 已支持 interrupt 的 Skill 不重复创建 pending context。
8. conversation 不应因 pending context 保持 busy；用户必须能继续输入补充信息。
9. 用户 metadata 不能伪造 pending continuation；只有后端 active context 可触发续接。
10. 只有 typed missing-input 信号会创建 pending context；普通失败文本不会被猜测为待补全。

## 11. 测试计划

### 11.1 Storage tests

新增/扩展 `tests/storage`：

- 保存 pending context。
- 查询 conversation active pending context。
- consumed / cancelled / superseded 状态转换。
- 同 conversation 仅一个 active pending context。

### 11.2 API / runtime tests

新增 `tests/api/test_pending_skill_context.py`：

- force Skill needs_input -> assistant 缺失提示 + pending context。
- 下一轮普通消息复用 pending `capability_id`。
- 新 slash force request supersedes old pending context。
- successful continuation consumes context。
- interrupt-capable path 不创建 pending context。

### 11.3 Orchestration tests

- pending continuation 的 `requested_capability_id` 进入 `SkillWorkflowProvider`。
- metadata 包含 `continued_from_pending_skill_context`，但用户 metadata 不能单独伪造强制 Skill。

## 12. 验证命令

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
```

如只新增窄测试文件，可先运行：

```bash
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
```

## 13. 风险与缓解

- **状态模型过重**：限制为单表、单 active context，不做复杂 pending 队列。
- **误续接错误 Skill**：新 slash 优先覆盖；成功后消费；可设置 context 过期策略作为后续增强。
- **conversation busy 卡住补充输入**：needs_input 无 interrupt path 必须结束当前 task，让下一轮可提交。
- **与现有 interrupt 重叠**：支持 interrupt 的 Skill 继续走 interrupt，不创建 pending context。
- **schema 改动影响 Rust migration**：新增 storage tests，若涉及 Rust sidecar contract，必须另行更新对应 contract / gate；本 PRD 不宣称 Rust canonical path 完成。

## 14. 与 PRD 01 的关系

PRD 01 完成后，即使没有本 PRD，用户仍可通过 slash 强制调用 Skill。本 PRD 只补齐信息不足后的跨轮续接语义，不能阻塞 PRD 01 上线。
