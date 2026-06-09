# Interrupt 状态下开放性追问设计

## 背景

当前用户在任务进入 `waiting_for_input` / interrupt 后，前端会把下一条输入直接提交到 `/api/v1/tasks/interrupts/answer`。后端随后把 interrupt 从 `open` 置为 `answered` 并调度 resume。这个语义无法支持用户在补槽前先问开放性问题，例如“这个数据要什么格式？”、“几种设计方法区别和利弊是什么？”。

## 目标

- 复用现有 `/api/v1/tasks/interrupts/answer` API，不新增 endpoint。
- interrupt 状态下，用户可以自然输入问题或答案，由 LLM 判断本轮意图。
- 只有高置信判断为正式补槽答案时，才关闭 interrupt 并恢复任务。
- 解释性、比较性、格式性、利弊性、低置信或模糊输入均保持 interrupt 为 `open`。
- 不以成本为约束，优先理解准确率和状态安全。

## 非目标

- 不增加前端“提问/提交”手动模式切换。
- 不改变 task cancellation、sheet selection 的既有确定性流程。
- 不改变已有 v2 slot collection 状态机的终态语义。

## 设计

### 1. API 兼容扩展

继续使用 `POST /api/v1/tasks/interrupts/answer`。请求格式保持兼容。响应保留旧字段，并增加可选字段：

```json
{
  "interrupt_id": "...",
  "status": "open|answered",
  "node_id": "...",
  "answer_payload": {},
  "action": "clarification_answer|resumed",
  "assistant_message": "..."
}
```

旧前端只看旧字段仍可工作；新前端根据 `action` 分支。

### 2. 高准确理解器

当 interrupt 是 v2 slot collection，且 payload 是自然语言 answer 时，后端在关闭 interrupt 前运行 interrupt turn 理解器：

- 输入：当前用户文本、interrupt question、slot collection ref、schema snapshot、slots、missing、invalid、resolved、skill resources 摘要、已接受答案摘要、上传文件摘要。
- 输出严格 JSON：
  - `intent`: `slot_answer` / `clarification_question` / `mixed` / `ambiguous`
  - `confidence`: 0-1
  - `reason`
  - `clarification_answer`
  - `extracted_answer`

状态安全规则：

- `slot_answer` 且高置信才允许走既有 `_answer_v2_slot_interrupt` resume。
- `clarification_question` / `ambiguous` 保持 interrupt open，写 assistant clarification message。
- `mixed` 可先解释再尝试抽取；若 verifier 不确认，保持 open。
- LLM 输出无效、异常、低置信时保持 open。

### 3. 状态保持

clarification turn 不调用 `InterruptService.record_answer()`，因此：

- `Interrupt.status` 仍为 `open`。
- `TaskNode.status` 仍为 `waiting_for_input`。
- 不发布 `node.ready_to_resume`。
- 不调度 `_schedule_v2_slot_resume()`。

为可追踪性，clarification turn 保存为普通 assistant message，并发布前端事件 `task.interrupt_clarification_answered`。

### 4. 前端行为

`handleInterruptAnswer()` 仍调用 `api.answerInterrupt()`。收到响应后：

- `action === 'clarification_answer'`：追加 assistant 解释消息；保留 `pendingInterrupt`；task phase 仍为 `waiting_for_input`；不订阅 resume stream。
- 其他或 `action === 'resumed'`：沿用既有 resume 行为。

## 测试

后端：

- v2 interrupt 中“这个数据格式是什么？”返回 clarification，interrupt 仍 open，node 仍 waiting，不调度 resume。
- v2 interrupt 中“12列”返回 resumed，interrupt answered，任务完成。
- LLM invalid JSON / low confidence 默认 clarification / 保持 open。

前端：

- pending interrupt 下发送追问，显示用户问题和 assistant 解释，banner 保留，未清空 pendingInterrupt，未订阅 resume。
- pending interrupt 下发送正式答案，保持既有 resume 流。

## 风险与缓解

- 误判导致过早 resume：采用高置信阈值和保守默认 open。
- 解释内容幻觉：prompt 限定只基于 schema/resources/current interrupt，上报不确定性。
- 响应兼容性：只新增可选字段，不删除旧字段。
