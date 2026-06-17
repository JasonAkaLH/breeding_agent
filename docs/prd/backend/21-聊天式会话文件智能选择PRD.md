# 聊天式会话文件智能选择 PRD

- **状态**：设计已确认，待实施
- **范围**：在不新增公开 API、不引入向量检索、不要求用户点选文件的前提下，让平台根据聊天内容、Skill 文件需求契约和会话文件元数据自动定位任务应使用的 conversation file resource。
- **关联 PRD**：`docs/prd/backend/20-对话文件本地资源文件系统PRD.md`、`docs/prd/backend/skill-contract-progressive-disclosure/README.md`、`docs/prd/backend/table-upload-normalization/README.md`。
- **非目标**：不是 RAG；不做语义向量索引；不读取文件正文给选择 LLM；不新增前端文件选择控件；不按现有 Skill 名称硬编码文件选择策略。

## 1. 背景与问题

对话文件本地资源系统已经让上传文件成为 conversation-scoped 本地资源，后端以 `upload_id` / `file_id` 管理文件，Skill 运行时通过 `resource_manifest.json` 和 `files[].mount_path` 读取真实文件副本。当前任务绑定文件主要依赖前端在提交消息时显式传入 `metadata.upload_ids`。

这会带来三个体验问题：

1. 用户经常用自然语言引用文件，例如“刚才那个表”“继续用上次的数据”“分析 materials.csv”，不希望再去文件面板点选。
2. 同一会话可能有多个同名文件，仅靠文件名无法可靠定位。
3. 未来新增 Skill 的文件需求形态不可预知，平台不能依赖当前已有 Skill 名称做硬编码。

本 PRD 设计一个平台级 `ConversationFileSelector`：只在检测到本轮可能需要文件时，将当前会话 active 文件的元数据、用户 query、上下文和 Skill 文件需求画像发送给 LLM，由 LLM 输出结构化文件选择决策。若多个文件均可用，则复用现有 interrupt，以自然语言候选列表让用户在聊天中消歧。

## 2. 产品目标

1. **聊天内智能定位文件**：用户无需点选 UI，也无需手动复制路径；可通过自然语言引用文件。
2. **保留安全边界**：LLM 只看元数据，不看文件正文、本地路径、`storage_key`、`content` 或 `content_base64`。
3. **兼容现有 API**：继续使用现有 chat message、interrupt answer、uploads 和 `metadata.upload_ids` 语义，不新增公开 API。
4. **面向未来 Skill**：新增/迁移 Skill 通过 contract/schema 声明文件需求，平台归一化为 `FileRequirementProfile`，selector 不依赖 Skill 名称。
5. **可审计可恢复**：每次自动选择、歧义中断、恢复选择都留下 audit-only 事件和结构化原因。
6. **低置信不猜测**：多个可用文件或置信不足时，转自然语言澄清，而不是强行选择。

## 3. 用户故事

### 3.1 单文件自动定位

用户上传 `materials.csv` 后没有点选文件，直接说：“用刚才上传的数据做 4 个区组的设计。”

平台检测到当前 Skill / query 需要表格文件，selector 看到会话中只有一个 active CSV，自动绑定该 `upload_id`，继续现有 Skill 执行流程。

### 3.2 同名文件聊天消歧

会话中有两个 `materials.csv`。用户说：“分析 materials.csv。”

selector 判断两个文件均可能可用，打开 `file_selection_ambiguous` interrupt。用户看到自然语言候选列表，其中每项包含文件名、`description_summary`、`upload_id`、上传时间。用户回复“用 120 行那个”或直接回复 `upload_id` 后，平台恢复原任务。

### 3.3 continuation 使用最近实际使用文件

用户先使用 `A.csv` 完成一次 RCBD 任务，随后又上传 `B.csv`。用户说：“继续用刚才那个数据，把区组改成 4。”

selector 根据 `recent_usage` 判断“刚才那个数据”更可能是最近实际用于任务的 `A.csv`，而不是最新上传的 `B.csv`。

### 3.4 未来 Skill 声明文件需求

后续新增 `plant-dis` 或其他业务 Skill，只要在 `schemas/*.input.yaml` 中声明 `type: data/file/artifact` 和 `file_selection` 元数据，平台即可自动生成文件需求画像并选择会话文件，无需在 selector 中写 Skill 名称分支。

## 4. 核心设计原则

1. **文件身份永远是 `upload_id`**：文件名、上传时间、摘要和预览只用于定位和解释，不作为权限或绑定事实源。
2. **LLM 直接做元数据筛选**：不实现规则预筛/打分；后端只做权限、状态和候选范围限定。
3. **不做向量检索**：第一版不引入 embedding、chunk、vector store 或语义检索。
4. **歧义走聊天 interrupt**：用户通过自然语言回答，不需要前端点选 UI。
5. **显式 `metadata.upload_ids` 优先**：如果前端/用户本轮已显式绑定文件，沿用现有流程，不额外调用 selector。
6. **未来 Skill 契约驱动**：Skill 文件需求应写入 machine-readable contract/schema，而不是只写在 `SKILL.md` 或 references prose。

## 5. 总体数据流

```text
POST /api/v1/conversations/chat-messages
  -> 保存 user message / task
  -> 若 request.metadata.upload_ids 非空：沿用现有 resolve_uploads_for_message()
  -> 若无显式 upload_ids：FileSelectionTriggerDetector 判断是否需要文件选择
  -> 读取当前 conversation active file metadata + recent_usage
  -> 构造 FileRequirementProfile + ConversationFileCandidate[]
  -> ConversationFileSelector 调 LLM 输出 FileSelectionDecision
  -> select_one / select_many：写入 effective_upload_ids
  -> ambiguous：打开 file_selection_ambiguous interrupt，返回 message/task
  -> no_usable_file：required 时打开缺文件/说明 interrupt；optional 时继续无文件流程
  -> no_file_needed：继续原流程
  -> resolve_uploads_for_message(effective_upload_ids)
  -> _bind_task_input_uploads()
  -> _task_input_attachment_metadata()
  -> schedule execution
```

## 6. 触发机制

新增轻量 `FileSelectionTriggerDetector`，只判断是否进入 LLM selector，不负责选择文件。

### 6.1 Skill / schema 触发

当当前或候选 Skill contract/schema 包含以下任一信号时触发：

- input `type` 为 `file`、`artifact` 或 `data`；
- `source.allowed` 包含 `task_attachment`、`upload_ledger` 或 `validated_artifact`；
- input 含新增 `file_selection` 元数据；
- contract 级 `file_intent.requires_file=true`。

### 6.2 用户 query 触发

用户文本含文件意图或指代时触发，例如：

- “这个文件”“刚才上传的”“上次那个表”“最新的 CSV”；
- “分析这个表”“总结 PDF”“看一下图片”“这个表有哪些字段”；
- “比较两个文件”“继续用刚才的数据”“换成 4 个区组再跑一次”。

### 6.3 Interrupt / slot collection 触发

当前 interrupt 正在等待文件类输入，用户回复没有显式 `upload_ids`，但说“用刚才那个表”等自然语言引用时触发。

### 6.4 不触发场景

- 当前会话没有 active 文件；
- 用户明确说“不用文件”；
- 当前 query 是普通问答且无文件指代；
- 本轮已有显式 `metadata.upload_ids`；
- 当前能力明确不支持或不需要文件。

## 7. 核心数据结构

### 7.1 FileRequirementProfile

`FileRequirementProfile` 归一化本轮“为什么可能需要文件”：

```json
{
  "source": "skill_schema | user_query | interrupt | continuation | platform",
  "needs_file": true,
  "required": true,
  "intent": "table_analysis | document_qa | image_understanding | skill_execution | file_summary | file_conversion | comparison | continuation | unknown",
  "accepted_file_types": ["csv", "spreadsheet"],
  "allow_multiple": false,
  "expected_inputs": [
    {
      "name": "material_data",
      "type": "data",
      "required": true,
      "description": "实验材料表"
    }
  ],
  "user_file_reference": "刚才上传的表",
  "context_notes": [
    "当前 Skill schema 有 required data 输入",
    "用户提到“刚才上传”"
  ]
}
```

生成来源：Skill contract/schema、当前 query、interrupt required fields、历史 continuation context。

### 7.2 ConversationFileCandidate

给 selector LLM 的每个候选只包含 prompt-safe 元数据：

```json
{
  "upload_id": "upl-xxx",
  "filename": "materials.csv",
  "uploaded_at": "2026-06-17T10:21:00Z",
  "file_type": "csv",
  "content_type": "text/csv",
  "size_bytes": 12345,
  "sha256_short": "a1b2c3d4",
  "description_summary": "CSV 表格，120 行，包含 ped_id、design_check、set 等字段。",
  "preview": {
    "columns": ["ped_id", "design_check", "set"],
    "row_count": 120,
    "column_count": 3,
    "selected_sheet": null,
    "requires_sheet_selection": false
  },
  "recent_usage": {
    "last_used_task_id": "task-abc",
    "last_used_at": "2026-06-17T10:30:00Z",
    "last_usage_summary": "用于 RCBD 设计",
    "last_capability_id": "main_agent.respond",
    "last_skill_name": "mini-breedstat-rcbd",
    "usage_count": 2
  }
}
```

禁止包含：`storage_key`、`mount_path`、`resource_manifest_path`、本地绝对路径、`content`、`content_base64`、数据库连接、secret、token。

### 7.3 recent_usage

`recent_usage` 是会话内文件使用历史摘要，用于解释“继续”“上次”“刚才那个数据”等指代。V1 从现有 `task_input_attachment` 聚合，不新增公开 API。

建议内部 storage 能力：

```python
list_task_input_attachments_for_conversation(conversation_id: str, limit: int | None = None)
```

聚合逻辑：按 `conversation_id + source_upload_id` 分组，计算 `usage_count`、最近一次 `task_id`、`created_at/updated_at`、`source_kind`、`selected_sheet`，并尽量从任务摘要或能力输出中生成 `last_usage_summary`。

## 8. LLM 输出契约

Selector 必须只返回 JSON object：

```json
{
  "needs_file": true,
  "decision": "select_one | select_many | ambiguous | no_file_needed | no_usable_file",
  "selected_upload_ids": ["upl-xxx"],
  "confidence": 0.91,
  "reason": "用户说继续用刚才的数据；该文件最近被用于 RCBD 设计，且类型和字段符合本轮请求。",
  "user_visible_basis": "我将继续使用 materials.csv（upl-xxx），该文件最近用于 RCBD 设计。",
  "ambiguous_candidates": [],
  "clarification_question": null
}
```

多候选歧义时：

```json
{
  "needs_file": true,
  "decision": "ambiguous",
  "selected_upload_ids": [],
  "confidence": 0.63,
  "reason": "有两个同名 CSV 都可能满足请求，且 recent_usage 不足以区分。",
  "ambiguous_candidates": [
    {
      "upload_id": "upl-a1b2",
      "filename": "materials.csv",
      "uploaded_at": "2026-06-17T10:21:00Z",
      "description_summary": "120 行，包含 ped_id/design_check/set。",
      "recent_usage_summary": "最近用于 RCBD 设计，2026-06-17 10:30"
    }
  ],
  "clarification_question": "我找到多个可能相关的文件，请告诉我要用哪一个。"
}
```

## 9. 服务端后处理与安全校验

LLM 输出后必须服务端验证：

1. `selected_upload_ids` 必须属于本次候选列表。
2. `ambiguous_candidates[*].upload_id` 必须属于本次候选列表。
3. `confidence` 必须在 0 到 1。
4. `decision=select_one` 时必须恰好一个合法 id。
5. `decision=select_many` 时必须多个合法 id，且 `FileRequirementProfile.allow_multiple=true` 或用户明确要求比较/合并多个文件；否则转 `ambiguous`。
6. 低于置信阈值（建议 `< 0.75`）转 `ambiguous`。
7. JSON parse 失败、schema invalid、选择不存在文件时降级为 `ambiguous` 或 `no_usable_file`。
8. 选中文件若 `requires_sheet_selection=true`，继续进入现有 `sheet_selection_required` interrupt。
9. 最终仍调用 `resolve_uploads_for_message()` 做 conversation/user/status 权限校验。

## 10. Interrupt 消歧与恢复

### 10.1 复用现有 interrupt

不新增公开 API，不新增前端点选流程。歧义时打开普通 interrupt：

```text
reason_code = file_selection_ambiguous
```

`question` 是自然语言候选列表。前端按现有 interrupt 文本展示和聊天回复处理，不渲染文件选择卡片。

### 10.2 用户可见候选格式

候选列表必须至少包含：文件名、`description_summary`、`upload_id`、上传时间。若有 `recent_usage`，也展示最近使用情况。

```text
我找到多个可能相关的文件，请在聊天里告诉我要用哪一个：

1. 文件名：materials.csv
   upload_id：upl-a1b2
   上传时间：2026-06-17 10:21
   摘要：120 行，包含 ped_id/design_check/set。
   最近使用：2026-06-17 10:30 用于 RCBD 设计。

2. 文件名：materials.csv
   upload_id：upl-c3d4
   上传时间：2026-06-16 18:04
   摘要：2000 行，包含 genotype/block/yield。
   最近使用：无。

你可以直接回复 upload_id，或说“用 120 行那个”。
```

### 10.3 required_fields 恢复上下文

把恢复所需的机器上下文放入 `Interrupt.required_fields` 保留字段：

```json
{
  "_file_selection": {
    "version": 1,
    "type": "conversation_file_selection",
    "original_user_message": "继续用刚才的数据跑一下",
    "requirement_profile": {},
    "candidate_files": [],
    "ambiguous_candidates": [],
    "selector_decision": {},
    "allow_multiple": false
  },
  "file_selection_answer": {
    "type": "text",
    "description": "请说明要使用哪个文件。"
  }
}
```

### 10.4 用户回复解析

用户可以回复：

- 精确 `upload_id`；
- “第一个”“第二个”；
- “最新的”“刚上传的”；
- “120 行那个”；
- “最近用于 RCBD 的那个”；
- “都用”；
- “不用文件”；
- 上传新文件并说“用这个”。

后端在 interrupt answer 路径中识别 `reason_code == file_selection_ambiguous`，读取 `_file_selection`，调用同一 selector 的 ambiguity-resolve 模式或受控解析器输出 `selected_upload_ids`。解析成功后调用 `_bind_or_update_resume_input_attachments()`、更新 `_task_input_attachment_metadata()` 并恢复原任务。

## 11. Skill 契约与 breeding-skill-builder 同步要求

本功能实施必须同步更新项目级 `breeding-skill-builder`，让未来 Skill 按新平台流程声明文件需求。

### 11.1 新增 schema 字段级 `file_selection`

文件类 input 应使用机器可读字段声明选择提示：

```yaml
inputs:
  material_data:
    type: data
    title: 材料数据表
    required: true
    source:
      allowed:
        - task_attachment
        - upload_ledger
        - validated_artifact
    file_selection:
      accepted_file_types:
        - csv
        - spreadsheet
      allow_multiple: false
      expected_content: 实验材料表，包含材料编号、区组或处理信息
      helpful_columns:
        - ped_id
        - genotype
        - design_check
        - set
      disambiguation_hint: 优先选择用户最近上传或最近用于本 Skill 的材料数据表
```

### 11.2 新增 contract 级 `file_intent`

如果一个 Skill 或多个 schema 均依赖文件，可在 `skill.contract.yaml` 声明默认文件意图：

```yaml
file_intent:
  requires_file: true
  default_allow_multiple: false
  supported_file_types:
    - csv
    - spreadsheet
  description: 本 Skill 通常需要用户上传表格数据作为输入。
```

字段级 `file_selection` 优先于 contract 级默认值。

### 11.3 builder 文档更新范围

实施时必须同步更新：

- `.codex/skills/breeding-skill-builder/SKILL.md`
- `.codex/skills/breeding-skill-builder/references/templates.md`
- `.codex/skills/breeding-skill-builder/references/checklist.md`
- `Skill构建指南.md`

更新要求：

1. Golden rules 增加：文件需求必须写入 contract/schema，不得只写在 `SKILL.md` 或 prose reference。
2. 模板增加 `file_selection` 和 `file_intent` 示例。
3. checklist 增加：文件类 input 是否可归一化为 `FileRequirementProfile`。
4. 明确脚本继续通过 `resource_manifest_path` / `files[].mount_path` 读取文件，`uploaded_artifacts[].content` / `content_base64` 只作 legacy fallback。
5. 旧 Skill 未声明 `file_selection` 时，平台仍可通过 `type: file/artifact/data` 做基础推断。

## 12. 审计事件

新增 audit-only 事件：

```text
conversation_file.selector_triggered
conversation_file.selector_prompt_rendered
conversation_file.selector_decision
conversation_file.selector_invalid_output
conversation_file.selector_ambiguous_interrupt_opened
conversation_file.selector_resumed_from_interrupt
conversation_file.selector_auto_bound
```

事件可保存：

- task_id / conversation_id / node_id；
- requirement_profile；
- candidate upload_ids 和安全元数据摘要；
- decision / confidence / reason；
- selected_upload_ids；
- prompt profile metadata；
- 降级原因。

事件不得保存文件正文、`content_base64`、`storage_key`、本地路径、secret 或 provider raw prompt。

## 13. 组件与接入点

### 13.1 建议文件组织

V1 可新增：

```text
src/api/file_selection.py
```

包含：

- `FileRequirementProfile`
- `ConversationFileCandidate`
- `FileSelectionDecision`
- `FileSelectionTriggerDetector`
- `ConversationFileSelector`
- `render_file_selection_question()`
- `parse_file_selection_answer()` 或 `FileSelectionAnswerResolver`

若后续膨胀，再迁移到 `src/lifecycle/file_selection/`。

### 13.2 Runtime 接入点

- `ApiRuntime.submit_message()`：在无显式 `upload_ids` 时触发 selector；`ambiguous` 时打开 interrupt 并返回。
- interrupt answer 路径：识别 `file_selection_ambiguous`，解析用户回复并恢复原任务。
- storage contract：新增或复用内部方法聚合 conversation task attachments 生成 `recent_usage`。
- main agent / Skill execution：后续仍消费 `_task_input_attachment_metadata()` 产出的 `uploaded_artifacts` / `skill_artifacts`。

## 14. 测试与验收

### 14.1 Unit tests

- trigger detector：Skill schema 文件输入触发；query 文件指代触发；普通问答不触发；显式 `upload_ids` 不触发。
- selector post-processing：非法 JSON、未知 `upload_id`、`select_many` + `allow_multiple=false`、低置信、`no_usable_file`。
- question rendering：同名文件候选包含文件名、`description_summary`、`upload_id`、上传时间、recent usage。
- `FileRequirementProfile`：从 schema `file_selection`、contract `file_intent`、旧 `type: data/file/artifact` 正确归一化。

### 14.2 API / integration tests

1. 单文件自动绑定：会话已有一个 CSV，用户说“分析刚才上传的数据”，无 `upload_ids`，selector 选中文件并保存 task attachment。
2. 同名多文件澄清：两个 `materials.csv`，用户说“分析 materials.csv”，打开 `file_selection_ambiguous` interrupt，question 包含两个 upload_id、上传时间、摘要。
3. 澄清恢复：用户回复 `upl-xxx`，原 task 恢复，选中文件成为 task attachment。
4. recent_usage continuation：先用 A 文件完成任务，再上传 B，用户说“继续用刚才那个数据”，selector 选 A。
5. 未来 Skill schema：构造测试 Skill，schema 含 `type: data` + `file_selection`，selector 不依赖 Skill 名称即可生成 requirement profile。
6. sheet selection 衔接：selector 选中多 sheet Excel 后继续进入现有 `sheet_selection_required`。
7. no file needed：会话有文件但用户普通问答，不调用 selector、不绑定文件。
8. 新上传优先：file-selection interrupt 回复中用户上传新文件并说“用这个”，恢复时绑定新上传文件。

### 14.3 文档 / builder 验证

```bash
python /Users/yinpeihai/.codex/skills/.system/skill-creator/scripts/quick_validate.py .codex/skills/breeding-skill-builder
python -m pytest tests/integrations/agent_skills/test_project_skill_manifest_contract.py
```

### 14.4 回归命令建议

```bash
python -m pytest tests/api/test_uploads.py
python -m pytest tests/integrations/agent_skills/test_artifact_context.py
python -m pytest tests/integrations/agent_skills/test_slot_state_machine.py
```

## 15. Rollout 与回滚

1. 默认可用 feature flag 控制，例如 `conversation_file_selector_enabled=false`。
2. Shadow 阶段只记录 selector 决策，不改变绑定行为。
3. Enforce 阶段仅对无显式 `upload_ids` 且高置信 `select_one` 生效。
4. Ambiguous interrupt 先在内部场景放量，确认前端现有 interrupt 体验足够自然。
5. 回滚时关闭 feature flag，恢复旧 `metadata.upload_ids` 显式绑定路径。

## 16. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| LLM 误选文件 | 低置信转 ambiguous；服务端校验 upload_id；最终仍走权限校验。 |
| 文件候选太多导致 prompt 过大 | 候选数量设上限；超过上限时让用户缩小描述或按最近 active 文件截断并审计。 |
| 同名同结构文件难区分 | 澄清中展示 upload_id、上传时间、摘要、recent_usage；用户自然语言消歧。 |
| 未来 Skill 没声明文件需求 | 通过旧 `type: file/artifact/data` 做基础推断；更新 breeding-skill-builder 模板和 checklist。 |
| 前端误渲染成卡片选择 | `file_selection_ambiguous` V1 只依赖 interrupt.question 文本，不要求新组件。 |
| 文件正文泄漏到 LLM | 候选 sanitizer 明确剔除 content/content_base64/storage_key/mount_path；测试锁定。 |

## 17. 验收标准

1. 用户无需点选文件，也可通过自然语言让平台选择会话文件。
2. 多个候选文件时，平台通过普通聊天 interrupt 提供候选，候选包含文件名、`description_summary`、`upload_id`、上传时间。
3. 自动选择只在合法、单一、高置信场景发生；歧义不强猜。
4. 所有最终绑定都复用现有 `resolve_uploads_for_message()` 和 task attachment 路径。
5. 不新增公开 API；现有上传、消息、interrupt answer 客户端兼容。
6. 新增或迁移 Skill 可通过 `file_selection` / `file_intent` 声明未来文件需求，selector 不硬编码 Skill 名。
7. `breeding-skill-builder` 和 `Skill构建指南.md` 同步更新，未来 Skill 模板默认符合新平台流程。
8. 审计事件能解释 selector 触发、候选、选择、歧义、恢复和降级原因，且不记录文件正文或敏感路径。
