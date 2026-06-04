# Skill 缺参多轮对话补槽 PRD

- **状态**：document-perfectization 审查加固版，待实施计划拆解
- **日期**：2026-06-04
- **目标模块**：Skill runtime、main-agent Skill delegation、interrupt/resume lifecycle、前端业务对话台 waiting-input UX
- **目标结果**：用一个 slot-table-backed clarification path 替换当前分散的 Skill 缺参提示方式，支持自然语言多轮追问，直到用户补齐信息或主动取消。

## 1. 问题陈述

当前 Skill 缺参用户体验和运行时状态存在双轨：

1. 支持 interrupt 的路径会展示“需要补充信息”卡片，但问题文案主要来自硬编码字段标签与模板，字段未覆盖时不能稳定说明缺少什么。
2. pending context / 非 interrupt 路径会写入普通 assistant 文本，例如“缺少 Skill 必需信息：xxx。请补充后继续。”，但不会持续带入完整补槽状态。
3. 用户回答后的参数提取已有 LLM-first 能力，但缺少一个跨轮持久化的 slot table 来记录已填、仍缺、校验失败和追问轮次。

需要把 Skill 缺参统一为 **持久化参数表驱动的多轮对话补槽**：后端继续使用 interrupt/resume 生命周期保证同一 task 暂停、恢复、取消、SSE 和上传账本一致；用户可见层改为 LLM 生成的自然语言追问；每轮用户回答后由 LLM 抽取候选参数、后端校验并更新 slot table，直到参数完整或用户主动取消。

## 2. 目标与非目标

### 2.1 目标

- **G1 统一补槽路径**：typed Skill missing-input 必须进入同一 slot table + interrupt/resume 补槽流程。
- **G2 多轮追问**：用户一次只补充部分信息或补充无效时，系统必须保留已确认字段，只追问仍缺字段。
- **G3 自然语言追问**：普通文本/数字参数缺失时，用户看到的是 assistant 对话式问题，而不是硬编码缺参卡片。
- **G4 后端事实源**：slot table 是补槽状态唯一事实源；LLM 只生成追问和抽取候选值，不能直接决定 Skill 可执行。
- **G5 清理旧体验**：清除旧硬编码最终追问、pending context 普通缺参文本、普通参数强卡片 UI，避免新旧双轨。
- **G6 现有安全边界不回退**：上传内容、LLM prompt、审计、取消、SSE waiting/resume、历史恢复保持现有安全与生命周期 contract。

### 2.2 非目标

- 不从普通失败文本中猜测缺参；只有 typed `missing_input`、manifest required 参数缺失或 allowlisted platform handler `skill_input_missing` 可进入补槽。
- 不让 LLM 编造文件、路径、artifact、未声明参数或执行内部细节。
- 不引入 LangChain、LangGraph、AutoGen 等外部 Agent 框架。
- 不在本 PRD 中重写整个 task/node lifecycle；只在现有 interrupt/resume 上统一 Skill 缺参补槽。

## 3. 用户、干系人与受影响系统

| 类别 | 对象 | 关注点 |
| --- | --- | --- |
| 终端用户 | 业务对话台用户 | 缺什么说清楚；能像正常对话一样补充；补一半不会丢；不主动停止就继续收集。 |
| Skill 作者 | `skill/*/SKILL.md` 与脚本维护者 | 只需声明参数和 typed missing-input；不需要手写每种追问文案。 |
| 后端 runtime | `src/integrations/agent_skills/`、`src/capabilities/*`、`src/api/runtime.py`、`src/orchestration/service.py` | 统一缺参状态、resume、事件、上传账本、审计与安全校验。 |
| 前端 | `frontend/src/App.tsx`、task event reducer/client | 用自然 assistant 追问替代普通参数硬卡片；保留文件/sheet 控件。 |
| 测试与运维 | API/frontend/integration 回归、SSE/event log | 能通过事件与 storage 追踪缺参轮次、取消、恢复和失败。 |

## 4. 当前状态与证据

| 证据 | 当前行为 |
| --- | --- |
| `src/integrations/agent_skills/missing_input_interrupt.py` | `build_missing_input_interrupt()` 通过 `_missing_input_question()` 拼接最终用户问题，依赖 `_FIELD_LABELS` / `_FIELD_DESCRIPTIONS`。 |
| `src/integrations/agent_skills/input_resolution.py` | 已有 `resolve_skill_inputs_with_llm()`，可对自然语言标量参数做 LLM-first 抽取，并在 LLM 失败后 deterministic fallback。 |
| `src/integrations/agent_skills/execution.py` | manifest required 参数缺失时返回 `SkillScriptExecutionResult(status="missing_input")`。 |
| `src/capabilities/skill_tool/executor.py` | script/platform missing-input 会调用 `build_missing_input_interrupt()`，或产生 `CapabilityExecutionError(code="skill_input_missing")`。 |
| `src/capabilities/main_agent/executor.py` | delegated Skill script missing-input 也会记录 `skill.input_missing` 并构造 interrupt。 |
| `src/api/runtime.py` | `list_interrupts()` 向前端暴露 `question` / `required_fields`；`answer_interrupt()` 会合并历史 answer payload、绑定上传账本、记录 `task.interrupt_answered` 并调度原 task resume。 |
| `src/orchestration/service.py` | capability result 带 `interrupt` 时进入 `node.waiting_for_input` 并发送 FRONTEND 事件；`skill_input_missing` error 也会进入 waiting 状态。 |
| `frontend/src/App.tsx` | `InterruptPromptCard` 把 interrupt question 展示为“需要补充信息”卡片，并对所有 required fields 生成标签。 |
| `tests/api/test_pending_skill_context.py` | 已覆盖 interrupt 与 pending context 的分离行为；新方案必须反向锁定 pending context 不再作为用户可见普通缺参路径。 |
| `tests/api/test_skill_input_resolution_runtime.py` | 已覆盖 `blocks=十个重复` interrupt answer 可解析并恢复执行；新方案应扩展为多轮 slot table。 |

## 5. 核心方案

采用 **interrupt 生命周期 + LLM 对话追问 + 持久化 slot table**。

- **interrupt 是运行时协议**：负责 task/node `waiting_for_input`、answer 记录、resume、取消、SSE 与历史恢复。
- **slot table 是事实源**：记录参数 schema、已填值、缺失项、来源、轮次、校验结果和 no-progress 计数。
- **LLM 是受限 helper**：
  - question generator：根据 slot table 生成自然语言追问。
  - answer extractor：根据用户当前回答抽取候选值。
- **后端是裁决者**：所有候选值必须通过 manifest parameter spec、source allowlist、artifact ledger 和 Skill runner contract 校验；完整后才恢复执行。

### 5.1 多轮状态流

```text
Skill input resolution
  -> build/update slot table
  -> missing? generate question
  -> open slot-backed interrupt
  -> user answerInterrupt
  -> record answer + resume same task
  -> answer extractor + backend validation
  -> update slot table
  -> if still missing: open next interrupt round
  -> if complete: execute Skill
  -> if user cancels: cancel lifecycle
```

多轮不是靠 LLM 记忆实现；每轮都从后端持久化 slot table、历史 interrupt answers 和 task input attachments 重建补槽状态。

## 6. 数据与状态设计

### 6.1 Phase 1 持久化决策

Phase 1 **必须复用现有 interrupt 持久化能力**，不新增数据库表：

- 在 `Interrupt.required_fields` 中增加保留 key：`_slot_collection`。
- `_slot_collection` 只保存 prompt-safe / frontend-safe 的 slot table，不保存 raw 文件内容、provider 配置、secret 或 LLM evidence 原文。
- 已有 `InterruptAnswer.answer_payload` 继续作为用户每轮回答的持久记录；slot table 不重复保存 raw answer 文本，只保存校验后的安全值、来源类型和失败原因码。
- 每一轮 still-missing 都创建新的 interrupt，`interrupt_id` 必须包含 round 或 collection version digest，避免与旧 answered interrupt 冲突。
- 前端展示普通参数时必须忽略以下划线开头的 reserved required field，例如 `_slot_collection`。

选择该持久化方式的原因：现有 `src/api/runtime.py:list_interrupts()` 已向前端返回 `required_fields`，现有 storage 已持久化 interrupt JSON；Phase 1 不新增 schema，以降低迁移风险并满足刷新恢复需求。

### 6.2 Slot collection schema

```json
{
  "schema_version": 1,
  "collection_id": "slot-task-1-node-1-r2",
  "task_id": "task-1",
  "node_id": "node-1",
  "capability_id": "skill.field_design",
  "skill_name": "field-design",
  "round": 2,
  "status": "collecting",
  "slots": [
    {
      "name": "blocks",
      "label": "重复数",
      "type": "integer",
      "required": true,
      "status": "resolved",
      "value": 10,
      "source": "current_user_answer",
      "description": "随机区组重复数，例如 3。",
      "aliases": ["区组", "重复", "重复数"],
      "examples": ["3", "十个重复"],
      "validation": {"positive_integer": true},
      "last_validation_error": null
    },
    {
      "name": "ncols",
      "label": "田块列数",
      "type": "integer",
      "required": true,
      "status": "missing",
      "value": null,
      "source": null,
      "description": "田块列数，例如 10。",
      "aliases": ["ncols", "列数", "田块列数"],
      "examples": ["10列"],
      "validation": {"positive_integer": true},
      "last_validation_error": "missing"
    }
  ],
  "resolved": {"blocks": 10},
  "missing": ["ncols"],
  "no_progress_rounds": 0,
  "last_question": "重复数已收到。还差田块列数，你希望每行/每列按多少列排布？例如回复：10列。"
}
```

### 6.3 安全与隐私规则

- slot table 允许保存校验后的 scalar value；不得保存 raw artifact content、`content_base64`、DB URL、token、provider `base_url`、secret、cookie。
- artifact/file slot 的 value 只能是受控上传账本中的 `upload_id` / `artifact_id` / filename / hash / selected_sheet / summary，不能是 LLM 生成路径。
- prompt profile 和 audit event 只能记录字段名、来源类型、校验状态、诊断码、token budget；不得记录 LLM evidence 原文或 raw 用户历史全文。
- `task.interrupt_answered` 当前会携带 answer payload；本 PRD 不扩大该 payload 内容，后续若做脱敏应另开审计/事件安全 PRD。

## 7. 功能需求

| ID | Requirement | 验收要点 |
| --- | --- | --- |
| FR-001 | 所有 typed Skill missing-input 必须构造 `SkillSlotCollection`。 | script required 缺失、script output `error.type=missing_input`、platform `skill_input_missing` 都进入 slot-backed interrupt。 |
| FR-002 | 普通参数追问必须由 `SkillSlotQuestionGenerator` 生成。 | `Interrupt.question` 不再由 `_missing_input_question()` 直拼最终文案；LLM 失败时走统一 fallback generator。 |
| FR-003 | LLM question generator 只能看到 public Skill profile、safe context、slot table 和已解析字段。 | prompt 中不出现脚本路径、handler、raw artifact content、secret。 |
| FR-004 | 用户回答后必须用 `SkillSlotAnswerExtractor` 抽取候选值。 | extractor 只返回声明字段；unknown field 被拒绝并审计诊断。 |
| FR-005 | 后端必须校验候选值后更新 slot table。 | 类型不合法、source 不允许、artifact 非账本来源时保持 missing 并记录 reason code。 |
| FR-006 | 多轮追问必须保留已确认字段。 | 第一轮补 A 后第二轮只问 B；A 的 value/source 不丢失。 |
| FR-007 | 多轮追问不得自动终止。 | 用户不取消时任务保持 waiting/resumable；无新字段时换更明确问法，不把 task 标记 failed/completed。 |
| FR-008 | 每轮仍缺信息必须创建新的 interrupt round。 | 新 interrupt id 包含 round/digest；旧 interrupt 已 answered，不复用同一 id。 |
| FR-009 | 参数完整后必须恢复原 Skill 执行。 | 最终 payload 使用 slot table resolved values + task input attachments；生成正常 Skill result/artifact。 |
| FR-010 | 用户取消必须走现有 cancel lifecycle。 | cancel 后不再生成下一轮追问，不执行 Skill late result。 |
| FR-011 | pending context 普通缺参文本路径必须下线。 | 新 typed missing-input 不保存“缺少 Skill 必需信息：xxx”正式 assistant 消息；如保留 pending table，只用于迁移旧 active context。 |
| FR-012 | 前端普通参数缺参必须展示为 assistant 对话。 | 不显示普通参数的旧 `InterruptPromptCard` 标签卡；显示追问气泡 + 轻量 waiting banner。 |
| FR-013 | 文件上传和 sheet selection 结构化控件必须保留。 | `accepts_upload` 和 `sheet_selection` 继续可操作；控件说明来自 slot table/question。 |
| FR-014 | 刷新恢复必须可找回当前 open slot interrupt。 | current task + graph + interrupts API 可恢复追问气泡、banner、上传/sheet 控件。 |
| FR-015 | 普通失败文本不得被猜测为缺参。 | 没有 typed missing-input 的 Skill failure 不创建 slot collection。 |

## 8. 非功能需求

| 类别 | Requirement |
| --- | --- |
| 可靠性 | slot table 必须随 interrupt 持久化；进程重启后能通过 storage 恢复 open interrupt 的补槽状态。 |
| 安全 | LLM 输出一律不可信，必须 JSON parse + allowlist + schema validation；artifact/file 不能由文本伪造。 |
| 隐私 | prompt/audit 不包含 raw artifact content、secret、provider config、DB URL；slot table 不重复保存 raw answer。 |
| 性能 | 每轮缺参最多新增一次 question LLM call 和一次 answer extractor LLM call；若 LLM 不可用，fallback generator/extractor 不阻塞 resume。 |
| 可观测 | 记录 AUDIT_ONLY 事件：question generated/fallback、answer extracted、candidate rejected、slot updated、no-progress round。 |
| 兼容 | 不新增 DB schema；不改变现有 `/tasks/{task_id}/interrupts` 与 `answerInterrupt` 的基本 API 形态。 |
| 可访问性 | 前端 waiting banner 使用 `role="status"`；assistant 追问在聊天正文中可被屏幕阅读器读取。 |
| 可测试性 | 每个 FR 必须有 API、integration 或 frontend test 覆盖；旧路径下线必须有负向断言。 |

## 9. 后端行为设计

### 9.1 Skill 执行前解析

- `resolve_skill_inputs_with_llm()` 保留现有职责：结构化事实优先、自然语言标量 LLM-first、deterministic fallback。
- 新增 slot table builder，将 manifest parameters、script input contract、safe artifact summary、resolved sources 归一为 `SkillSlotCollection`。
- 当 required 参数仍缺失时，builder 生成 `status=collecting` 的 slot table。

### 9.2 追问生成

新增 `SkillSlotQuestionGenerator`，输出严格 JSON：

```json
{
  "question": "重复数已收到。还差田块列数，你希望按多少列排布？例如回复：10列。",
  "ask_fields": ["ncols"],
  "answer_hint": "请给出一个正整数",
  "style": "assistant_dialogue"
}
```

要求：

- `ask_fields` 必须是当前 slot table 的 missing 子集。
- 默认一轮追问 1-3 个字段；只有强相关字段允许同问，例如 material file + sheet selection。
- LLM 输出 invalid/empty/unsafe 时，fallback generator 使用 slot label/description/examples 生成确定性问题。

### 9.3 打开 slot-backed interrupt

- `build_missing_input_interrupt()` 改为接收 `slot_collection` 和 `question_payload`。
- `Interrupt.question` 使用 question payload 的 `question`。
- `Interrupt.required_fields` 包含：
  - 普通字段的 type/description/aliases/examples/validation summary。
  - 文件字段的 `accepts_upload=true`。
  - sheet 字段的 options。
  - reserved `_slot_collection`。
- `node.waiting_for_input` event payload 增加 `slot_collection_id`、`round`、`missing`、`question_source=llm|fallback`。

### 9.4 answerInterrupt 与 resume

现有 `ApiRuntime.answer_interrupt()` 已做以下事情：合并历史 answer payload、绑定/更新 task input attachments、记录 `InterruptAnswer`、写入用户消息、发送 `task.interrupt_answered`、调度同 task resume。新方案必须复用该链路。

resume 后，Skill input resolution 读取：

- 当前 open/answered interrupt 的 `_slot_collection`。
- 当前 task 的所有 interrupt answers。
- task input attachment prompt-safe / execution artifact context。
- 当前用户补充消息与合并后的 resume message。

然后执行 answer extraction、后端校验、slot table update。若仍缺字段，capability result 再返回下一轮 interrupt。

### 9.5 pending context 迁移

- 新实现完成后，typed Skill missing-input 不得调用 `_format_pending_skill_missing_message()` 写普通 assistant 缺参文本。
- `conversation_pending_skill_context` table 在 Phase 1 保留，仅用于：
  - 标记旧 active pending context 为 superseded/cancelled/consumed。
  - 兼容旧数据迁移到 slot-backed interrupt。
- 新测试必须断言 typed missing-input 不创建新的用户可见 pending context assistant 文本。

## 10. 前端 UX 设计

### 10.1 普通参数缺失

前端必须把普通参数缺参渲染为 assistant 追问气泡：

```text
重复数已收到。还差田块列数，你希望按多少列排布？例如回复：10列。
```

输入框上方显示轻量 banner：

```text
当前任务等待补充信息，你的下一条消息会继续这个任务。
```

### 10.2 文件上传 / sheet selection

- 如果 missing slot 含 `accepts_upload=true`，保留上传能力和上传提示。
- 如果 required field 为 `sheet_selection`，保留现有 sheet selector 控件。
- 控件说明文案必须来自 interrupt question / slot metadata，不再使用旧普通参数卡片标题作为主要说明。

### 10.3 旧卡片清理

- `InterruptPromptCard` 可保留为文件/sheet 等结构化控件容器，或拆分为 `ClarificationPrompt` + `StructuredInterruptControls`。
- 普通文本/数字字段不得显示“需要补充：Tag 列表”的旧硬卡片。
- 前端必须过滤 `_slot_collection` 等 reserved required fields，不能把内部 key 渲染成用户字段。

## 11. 迁移与清理清单

| 清理项 | 要求 | 验收 |
| --- | --- | --- |
| `_missing_input_question()` | 不再生成最终用户文案；降级为 fallback helper 或删除。 | 单测断言 question generator/fallback 被调用。 |
| `_FIELD_LABELS` / `_FIELD_DESCRIPTIONS` | 只作为 label/description seed；不得作为唯一追问系统。 | 字段未覆盖时仍能用 manifest spec 生成自然问题。 |
| `_format_pending_skill_missing_message()` | 新 typed missing-input 不再写该文本作为 assistant final message。 | API 测试断言历史消息不包含旧文本。 |
| pending context 创建 | 新 slot-backed typed missing-input 不创建 active pending context。 | `get_active_pending_skill_context()` 为 None。 |
| `InterruptPromptCard` 普通参数展示 | 普通 scalar missing 不再显示旧卡片 tag UI。 | 前端测试 `queryByText('需要补充：')` 对 scalar path 为 null。 |
| event payload | waiting event 加入 slot collection metadata。 | API/SSE 测试覆盖 `slot_collection_id`、`round`、`missing`。 |

## 12. 边界与失败模式

| 场景 | 预期行为 |
| --- | --- |
| 用户只回答部分字段 | 更新已回答字段，下一轮只问剩余字段。 |
| 用户回答无效值 | 字段保持 missing，记录 `last_validation_error`，下一轮换更明确问法。 |
| 用户上传文件但缺 sheet | 文件 slot resolved，sheet slot missing，展示 sheet selector。 |
| 用户重复上传同一文件 | 复用/更新 task input attachment，不丢原始 message upload provenance。 |
| LLM question 失败 | fallback generator 生成问题，仍打开 interrupt。 |
| LLM extractor 失败 | deterministic text fallback 尝试解析；仍失败则保持 missing 并追问。 |
| LLM 返回未知字段 | 拒绝 unknown，审计 `slot_candidate_rejected_unknown_field`。 |
| LLM 返回 artifact 路径 | 拒绝，artifact/file 只能来自上传账本。 |
| 用户说取消/停止 | 调用现有 cancel lifecycle；slot status 标记 `cancelled`。 |
| 用户长期不回复 | task 保持 waiting，不产生后台 busy loop。 |
| 连续多轮无新信息 | 增加 `no_progress_rounds`，换更明确/更短问题；不自动失败。 |
| 普通 Skill failure | 不创建 slot table；按现有失败处理。 |
| 刷新页面 | current task + graph + interrupts API 恢复追问和控件。 |

## 13. 验收标准与测试矩阵

| AC | 覆盖需求 | 测试路径 |
| --- | --- | --- |
| AC-001 | FR-001/002 | `test_missing_input_interrupt_contract`：typed missing-input 生成 slot collection，question source 为 llm/fallback。 |
| AC-002 | FR-004/005 | `test_skill_slot_answer_extractor`：unknown/invalid/artifact text path 被拒绝。 |
| AC-003 | FR-006/008 | `tests/api/test_skill_slot_collection.py`：A/B 两字段多轮补槽，第二轮只问 B，新 interrupt id 不同。 |
| AC-004 | FR-007 | API 测试：连续无效回答后 task 仍 waiting，未 failed/completed。 |
| AC-005 | FR-009 | runtime 测试：slot table 完整后 Skill runner 收到最终 payload 并完成。 |
| AC-006 | FR-010 | cancel 测试：waiting slot task 取消后不再 resume，不写下一轮 interrupt。 |
| AC-007 | FR-011 | pending context 测试：新 typed missing-input 不保存旧普通缺参 assistant 文本。 |
| AC-008 | FR-012/013 | `frontend/src/App.test.tsx`：scalar path 显示追问气泡 + banner；file/sheet 控件仍显示。 |
| AC-009 | FR-014 | refresh restore 测试：open slot interrupt 恢复 question、banner 和控件。 |
| AC-010 | FR-015 | plain failure 测试：普通失败文本不创建 slot collection。 |
| AC-011 | NFR 安全 | prompt/audit 测试：不泄漏 raw artifact content、secret、LLM evidence。 |
| AC-012 | 旧路径清理 | grep/单测：旧硬编码文本不是新 typed missing-input 用户可见输出。 |

推荐回归命令：

```bash
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_input_resolution
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_missing_input_interrupt_contract
conda run -n multi_agent python -m unittest tests.api.test_skill_input_resolution_runtime
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
conda run -n multi_agent python -m unittest tests.api.test_skill_slot_collection
cd frontend && npm test -- --run
```

## 14. Rollout 计划

1. **测试先行**：新增 slot collection builder/question/extractor 单测，新增 API 多轮补槽失败测试。
2. **后端接入**：改造 `missing_input_interrupt.py`、Skill executor/main-agent missing-input 分支、runtime resume 补槽合并。
3. **前端切换**：普通参数缺参改为 assistant 追问气泡，文件/sheet 控件保留。
4. **旧路径清理**：删除/降级旧 question 拼接、pending context 用户可见文本、普通参数硬卡片 UI。
5. **回归与手工 smoke**：至少覆盖 field-design scalar、多字段、上传文件、sheet selection、取消、刷新恢复。
6. **发布观察**：观察 AUDIT_ONLY slot events、waiting/resume terminal 状态、用户取消率和 no-progress rounds。

## 15. 依赖

- 现有 LLM runtime / main-agent LLM client，用于 question generator 和 answer extractor。
- 现有 `Interrupt.required_fields` JSON 持久化能力。
- 现有 task input attachment ledger，用于上传文件跨 resume 恢复。
- 现有 frontend SSE task event client 与 interrupt API client。
- 现有 manifest parameter spec、script input contract 和 Skill output typed missing-input contract。

## 16. 风险、假设与开放问题

### 16.1 风险

| 风险 | 缓解 |
| --- | --- |
| LLM 编造参数 | 后端 schema/source/artifact ledger 校验；unknown/invalid 全部拒绝。 |
| 新旧双轨残留 | 清理清单 + 负向测试锁定旧文本/旧卡片不再出现。 |
| required_fields 变大 | `_slot_collection` 仅保存 prompt-safe compact schema/value；必要时后续独立 storage PRD。 |
| 历史恢复显示内部字段 | 前端过滤 reserved key；API 测试覆盖 `_slot_collection` 不渲染为用户字段。 |
| no-progress 多轮消耗 LLM | 每次用户输入最多一次 extractor + question；无后台循环；LLM 失败走 fallback。 |

### 16.2 假设

- 现有 interrupt storage 能稳定持久化 `required_fields` JSON；Phase 1 不需要新增 DB schema。
- 用户的“只要不主动停止就继续收集”是指系统在用户每次回复后继续尝试，不代表后台自动轮询或无限 LLM 调用。
- 普通参数自然语言追问优先于表单式 UI；文件上传和 sheet selection 仍允许结构化控件。

### 16.3 开放问题

无阻断性开放问题。若 Phase 1 实施中发现 `_slot_collection` 体积或审计合规不满足生产要求，应另开存储/脱敏 PRD，而不是在本 PRD 内静默扩大 schema 变更。

## 17. 完成定义

本 PRD 对应实现完成时必须同时满足：

1. 所有 typed Skill missing-input 进入 slot-backed interrupt。
2. 普通参数缺失的用户可见问题来自 LLM/fallback question generator。
3. 用户多轮补充时 slot table 持久保留已填字段，仍缺字段继续追问。
4. 参数完整后恢复原 Skill 执行并生成正常结果。
5. 旧 pending context 普通缺参文本和普通参数硬卡片体验被清理或迁移。
6. 上传、sheet selection、取消、刷新恢复、SSE waiting/resume 不回退。
7. API/integration/frontend 回归覆盖通过。
8. License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。
