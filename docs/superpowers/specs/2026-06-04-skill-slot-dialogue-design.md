# Skill 缺参多轮对话补槽设计

## 背景与目标

当前 Skill 缺参存在两类用户体验不一致的问题：

1. 支持 interrupt 的路径会展示“需要补充信息”卡片，但问题文案主要来自硬编码字段标签与模板，部分字段缺少友好说明。
2. 非 interrupt / pending context 路径会写入普通 assistant 文本，例如“缺少 Skill 必需信息：xxx”，但不会持续带入完整补槽状态。

目标是把 Skill 缺参统一为 **持久化参数表驱动的多轮对话补槽**：后端继续使用 interrupt / resume 生命周期保证同一 task 暂停、恢复、取消与上传账本一致；用户可见层改为 LLM 生成的自然语言追问；每轮用户回答后由 LLM 抽取候选参数、后端校验并更新参数表，直到参数完整或用户主动取消。

## 非目标

- 不让 LLM 直接决定 Skill 是否可执行；最终参数有效性仍由后端 schema、类型、来源与 Skill runner 校验。
- 不从普通失败文本中猜测缺参；只有 typed `missing_input`、manifest required 参数或 allowlisted platform handler missing-input error 可进入补槽。
- 不改变上传文件内容安全边界；prompt 与审计只使用脱敏 artifact summary，执行阶段仍消费受控 artifact context。
- 不引入 LangChain、LangGraph、AutoGen 等外部 Agent 框架。

## 核心决策

采用 **interrupt 生命周期 + LLM 对话追问 + 持久化 slot table** 的混合方案。

- interrupt 是后端运行时协议：负责 task/node 进入 `waiting_for_input`、answer 记录、resume、取消与 SSE 事件。
- slot table 是补槽唯一事实源：记录参数 schema、已填值、缺失项、来源、轮次与校验结果。
- LLM 只做两个受限动作：
  - 根据 slot table 生成用户友好的追问文案。
  - 根据用户回答抽取候选字段值。
- 后端校验 LLM 候选值后更新 slot table；表完整才恢复 Skill 执行。

## 旧方式下线要求

本设计不是在旧逻辑旁边再加一条新分支，而是要清除旧的用户可见缺参方式：

1. **下线硬编码追问模板作为最终用户文案**
   - `_FIELD_LABELS` / `_FIELD_DESCRIPTIONS` 可临时作为 schema label fallback 或 migration seed。
   - 用户最终看到的问题必须来自新的 LLM question generator，或者在 LLM 不可用时来自统一 fallback generator。
   - 不再由 `_missing_input_question()` 直接拼接最终 interrupt question。

2. **下线普通 pending context 缺参文本路径**
   - `缺少 Skill 必需信息：xxx。请补充后继续。` 这类文本不再作为独立补槽体验。
   - pending context 如仍需保留，只作为兼容旧 slash/non-interrupt 缺参的迁移入口，进入同一 slot table 流程。
   - 新实现后不得存在“有些走卡片、有些写一句普通缺参文本”的双轨用户体验。

3. **下线前端强卡片式普通参数补槽**
   - 普通文本/数字参数缺失时，assistant 气泡显示 LLM 追问。
   - 输入框上方仅保留轻量状态提示：“当前任务等待补充信息，你的下一条消息会继续这个任务。”
   - 文件上传、Excel sheet 选择等结构化交互可保留控件，但其说明文案也应来自 slot table / question generator。

4. **统一事件与恢复语义**
   - 所有 Skill 缺参最终都应产生可追踪的 `skill.input_missing` 与 `node.waiting_for_input` / resume 相关事件。
   - 不保留无法被 SSE/graph/interrupt API 发现的隐式补槽状态。

## Slot table 数据模型

建议新增内部模型 `SkillSlotCollection`，可存储在 interrupt metadata / checkpoint snapshot / 后续专表中。首期可放入 `Interrupt.required_fields` 的保留 key，例如 `_slot_collection`，但前端只消费脱敏后的展示字段。

示例：

```json
{
  "collection_id": "slot-task-1-node-1-r1",
  "task_id": "task-1",
  "node_id": "node-1",
  "capability_id": "skill.field_design",
  "skill_name": "field-design",
  "round": 1,
  "status": "collecting",
  "slots": [
    {
      "name": "blocks",
      "label": "重复数",
      "type": "integer",
      "required": true,
      "status": "missing",
      "value": null,
      "source": null,
      "description": "随机区组重复数，例如 3。",
      "aliases": ["区组", "重复", "重复数"],
      "examples": ["3", "十个重复"],
      "validation": {
        "positive_integer": true
      }
    }
  ],
  "resolved": {},
  "missing": ["blocks"],
  "last_question": null,
  "last_answer": null
}
```

安全要求：

- 不保存 raw 上传内容、secret、DB URL、provider config。
- 只保存字段名、类型、脱敏 artifact id/filename/summary、用户回答文本和校验后的安全值。
- 审计事件不得包含 LLM evidence 原文，只记录字段名、来源类型、通过/拒绝原因。

## 后端流程

### 1. Skill 执行前解析

- 现有 `resolve_skill_inputs_with_llm()` 继续优先解析 payload、metadata、artifact summary 和安全文本上下文。
- 如果仍缺 required 参数，构造或更新 `SkillSlotCollection`。
- 对 typed script output `missing_input` 与 platform handler `skill_input_missing` 也进入同一构造流程。

### 2. 生成追问

新增 `SkillSlotQuestionGenerator`：输入 public Skill profile、slot table、用户原始问题、已解析字段；输出严格 JSON：

```json
{
  "question": "还差一个重复数。你希望随机区组设计做几次重复？例如可以回复：3次重复。",
  "ask_fields": ["blocks"],
  "answer_hint": "请给出一个正整数"
}
```

约束：

- 不暴露内部脚本路径、handler、provider、raw artifact content。
- 只问仍缺失字段；已填字段不得重复询问。
- 一轮可问多个强相关字段，但默认优先 1-3 个，避免一次性过载。
- LLM 失败时使用统一 deterministic fallback generator，仍基于 slot table 输出可读问题。

### 3. 打开 interrupt

- `build_missing_input_interrupt()` 改为接收 slot collection 与 question payload。
- interrupt `question` 使用 question generator 结果。
- `required_fields` 保留字段 schema、`accepts_upload`、sheet options 等结构化 UI 信息。
- event payload 加入 `slot_collection_id`、`round`、`missing`，但不包含敏感值。

### 4. 用户回答与抽取

前端调用现有 `answerInterrupt(task_id, interrupt_id, answer_payload)`。

后端新增 `SkillSlotAnswerExtractor`：输入 slot table + 当前用户回答 + safe upload summary，输出严格 JSON：

```json
{
  "resolved": {
    "blocks": {
      "value": 10,
      "source": "current_user_answer"
    }
  },
  "missing": []
}
```

后端逐字段校验：

- 类型合法才写入 `value`。
- artifact/file 字段只能来自受控上传账本，不能由 LLM 编造路径。
- 非法值写入 `last_validation_error`，字段保持 `missing`。

### 5. 多轮循环

校验后：

- 若 `missing` 非空：关闭/回答当前 interrupt，创建下一轮 slot collection version 与新 interrupt，重新生成追问。
- 若 `missing` 为空：恢复原 node，使用最终 payload 执行 Skill。
- 若用户主动取消：走现有 cancel lifecycle，slot collection 标记 `cancelled`。

为避免同一 interrupt id 冲突，每轮 interrupt id 必须包含 `round` 或 slot collection version digest。

## 前端体验

### 普通参数缺失

展示为 assistant 自然对话：

```text
还差一个重复数。你希望随机区组设计做几次重复？例如可以回复：3次重复。
```

输入框上方显示轻量 banner：

```text
当前任务等待补充信息，你的下一条消息会继续这个任务。
```

### 上传或 sheet 选择

保留必要控件：

- 缺文件：允许继续上传文件。
- Excel 多 sheet：保留 sheet selector。
- 控件周围说明由 slot table / question generator 提供，不再硬编码成独立旧卡片体验。

### 历史恢复

刷新或切换历史后：

- 通过 current task、task graph、interrupt API 找回 open interrupt。
- 恢复 assistant 追问气泡与轻量 banner。
- 不恢复成旧的硬卡片文案。

## 兼容与迁移

分阶段执行：

1. 新增 slot table builder / question generator / answer extractor，并用测试锁定行为。
2. 将 script missing-input、platform handler missing-input、pending context missing-input 都接入 slot table。
3. 调整前端普通参数缺失展示为 assistant 对话；上传/sheet 结构化控件保留。
4. 删除或降级旧 `_missing_input_question()` 直拼文案，仅保留 fallback label/description 工具。
5. 删除 pending context 普通缺参 assistant 文本写入路径，改为创建 slot-backed interrupt。
6. 回归确认不存在新旧双轨：同类缺参只出现一种用户体验。

## 测试计划

后端：

- `tests/integrations/agent_skills/test_input_resolution.py`
  - LLM extractor 只接受声明字段。
  - artifact 字段不能由 LLM 文本伪造。
  - 已填字段不会重复询问。
- `tests/integrations/agent_skills/test_missing_input_interrupt_contract.py`
  - 所有 required 参数生成 slot table。
  - interrupt question 来自 question generator/fallback，而非旧拼接模板。
- `tests/api/test_skill_slot_collection.py`（新增）
  - 单轮缺参 -> answer -> Skill 完成。
  - 多轮缺参：第一轮补 A 后仍缺 B，第二轮只问 B。
  - 用户回答无效时继续追问且不丢已填字段。
  - 用户取消后不继续执行 Skill。
  - pending context 缺参进入 slot-backed interrupt，不写旧普通缺参文本。
  - refresh/list interrupts 可恢复 slot-backed question。

前端：

- `frontend/src/App.test.tsx`
  - waiting input 普通参数显示 assistant 追问与轻量 banner。
  - 不显示旧的普通参数 interrupt 卡片。
  - 上传/sheet 仍显示必要控件。
  - 多轮追问时保留已填状态，不重复询问已填字段。

回归命令：

```bash
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_input_resolution
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_missing_input_interrupt_contract
conda run -n multi_agent python -m unittest tests.api.test_skill_input_resolution_runtime
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
cd frontend && npm test -- --run
```

## 风险与防护

- **LLM 编造参数**：后端 schema 校验、source allowlist 与 artifact ledger 防止写入非法值。
- **多轮死循环**：只要用户不取消就继续，但记录 no-progress rounds；连续无新字段时换更明确追问，不自动终止。
- **旧新双轨混乱**：迁移验收必须扫描/测试旧缺参文本与旧卡片路径，确保普通参数缺参统一为 slot-backed dialogue。
- **历史恢复丢状态**：slot table 必须持久化或可由 interrupt/checkpoint 还原，不能只存在内存。
- **隐私泄漏**：prompt profile 与 audit 只记录字段名、来源类型、预算与诊断码，不记录用户证据原文或 raw file content。

## 验收标准

1. 任意 typed Skill missing-input 都能说明缺什么，并给出自然语言追问。
2. 支持多轮追问：已填字段不丢失、不重复问；仍缺字段继续问。
3. 用户回答后由 LLM 抽取、后端校验、slot table 更新；完整后恢复原 Skill。
4. 普通参数缺失不再展示旧硬卡片；旧 pending context 普通文本路径被清理或迁移。
5. 上传/sheet 结构化交互不回退。
6. 取消、刷新恢复、SSE waiting/resume 事件、task terminal 状态保持现有 contract。
7. License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。
