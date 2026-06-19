# 阶段三：文件需求画像与 Selector Shadow PRD

- **编号**：后端 PRD 21-Phase 3
- **日期**：2026-06-19
- **状态**：待实施
- **前置阶段**：阶段二会话文件上下文与 memory 安全
- **目标模块**：`src/api/file_selection.py`、`src/api/file_selection_runtime.py`、Skill contract / input schema parser、`breeding-skill-builder` 文档模板

## 1. 阶段目标

在不改变实际执行路径的 shadow 模式下，建立文件需求识别、`FileRequirementProfile` 归一化、selector 触发判定、candidate 摘要和 would-select 审计，为后续 enforce 阶段提供可观测证据。

## 2. 范围

### In scope

- 定义并实现 `FileRequirementProfile` 归一化结构。
- 从 interrupt/resume、metadata、pending Skill、Skill contract/schema、旧 schema type 和用户 query 推断文件需求。
- 支持 Skill contract / schema 中的 `file_selection` / `file_intent` 字段解析。
- 建立 selector trigger detector，但阶段三只 shadow 记录，不写 binding、不打开新 interrupt。
- 为 active candidates 构造 prompt-safe 摘要和 recent usage 摘要，但不让 LLM 结果改变执行。
- 同步 builder 模板、checklist、指南的文件需求声明要求。

### Out of scope

- 不自动绑定文件。
- 不打开 `file_selection_ambiguous`。
- 不把 LLM selector 结果用于实际执行。
- 不开启 guarded multi-select。

## 3. FileRequirementProfile

`FileRequirementProfile` 归一化本轮为什么可能需要文件：

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
  "context_notes": ["当前 Skill schema 有 required data 输入", "用户提到刚才上传"]
}
```

归一化来源优先级：

1. interrupt / resume 上下文中的文件需求；
2. 显式 `metadata.file_requirement_profile` / `metadata.file_selection` / `metadata.file_intent`；
3. soft / pending Skill binding 中的 file profile；
4. Skill contract / input schema 的 `file_selection` / `file_intent`；
5. 旧 schema `type: file | artifact | data` 的基础推断；
6. 用户 query 中的文件指代、continuation 词和比较 / 合并意图。

显式 `metadata.upload_ids` 存在时直接退出 selector，不生成 profile-driven selection。

## 4. Trigger detector

必须触发 shadow 记录或进入等价缺文件观察分支：

- `FileRequirementProfile.required=true` 且本轮没有显式 `metadata.upload_ids`。
- 用户 query 明确要求“用这个文件 / 刚才那个表 / materials.csv / 第一份文件”等文件指代，或直接包含 `upload_id`，且当前文件池需要缩窄或写入 task-level provenance。
- 多个 active 文件同名、同类型或都满足当前 Skill 文件需求，且下游只接受单文件。
- continuation 明确要求“继续用刚才那个数据”，需要基于 recent usage 判断。
- interrupt answer 恢复时需要把自然语言选择解析为 upload_id。
- future Skill schema / contract 声明 required file input。

不应触发 selector：

- 本轮已有显式 `metadata.upload_ids`。
- 用户明确说“不需要文件 / 不用上传的文件”。
- 普通问答且无文件指代、无 required file profile。
- 只有 conversation file context 注入即可满足普通 summarization / exploratory query，且平台策略不要求 task-level binding。

若 query、Skill contract/schema 或 interrupt 明确需要文件，但当前会话没有 active 文件，平台在 shadow 阶段只记录 would-no-usable-file，不改变执行路径；enforce 阶段再打开缺文件澄清。

## 5. Skill contract 与 builder 同步

Skill 文件需求必须进入 machine-readable contract/schema，不得只写在 `SKILL.md` 或 prose reference。

建议 schema 字段：

```yaml
properties:
  material_file:
    type: data
    description: 实验材料表
    file_selection:
      required: true
      accepted_file_types: [csv, spreadsheet]
      intent: table_analysis
      allow_multiple: false
      helpful_columns: [ped_id, variety, block]
      disambiguation_hint: 优先选择最近实际用于本会话设计任务的材料表
```

实施时必须同步更新 `breeding-skill-builder`：

- `SKILL.md`
- `references/templates.md`
- `references/checklist.md`
- `references/Skill构建指南.md`

更新要求：

1. Golden rules 增加：文件需求必须写入 contract/schema，不得只写在 prose 中。
2. 模板增加 `file_selection` 和 `file_intent` 示例。
3. checklist 增加：文件类 input 是否可归一化为 `FileRequirementProfile`。
4. 明确脚本继续优先通过 `resource_manifest_path` / `files[].mount_path` 读取文件，`uploaded_artifacts[].content` / `content_base64` 只作 legacy fallback。
5. 旧 Skill 未声明 `file_selection` 时，平台仍可通过 `type: file/artifact/data` 做基础推断。

## 6. Shadow 审计

阶段三只记录 audit-only 事件，不改变任务执行：

```text
conversation_file.file_selector_invoked
conversation_file.file_selector_decision_recorded
conversation_file.file_selector_invalid_output
```

事件只保存结构化摘要：

- task_id / conversation_id / node_id；
- selector 触发原因和 `requirement_profile` 摘要；
- candidate 数量、candidate upload_id 列表或 hash、安全元数据摘要；
- shadow decision / confidence / reason_code；
- 是否 would-clarify 或 would-auto-bind。

事件不得保存完整 LLM prompt、文件正文、`content_base64`、`storage_key`、本地路径、secret 或 provider raw prompt。

## 7. 测试计划

| 测试 | 断言 |
| --- | --- |
| profile 来源优先级 | interrupt > metadata > pending skill > schema > legacy type > user query。 |
| 显式 upload_ids | 不触发 profile-driven selector。 |
| required file schema | 归一化为 `required=true`、accepted types、intent、allow_multiple。 |
| legacy type | `type: data/file/artifact` 可基础推断。 |
| 普通问答 | 不触发 selector shadow。 |
| continuation | “继续用刚才那个数据”触发 recent usage 需求。 |
| shadow only | 不写 task attachment、不打开 interrupt、不改变 execution metadata。 |
| audit safety | 不记录 raw prompt、正文、路径、storage_key、secret。 |

推荐命令：

```bash
python -m pytest tests/integrations/agent_skills/test_input_schema_parser.py
python -m pytest tests/api/test_conversation_file_selection.py -k "profile or shadow or trigger"
python -m pytest tests/api/test_pending_skill_context.py
```

## 8. 阶段验收

- 文件需求可从 machine-readable schema / contract 进入平台画像。
- selector 触发条件在 shadow 模式下可观测且不改变当前产品行为。
- builder 文档和模板要求新 Skill 显式声明文件需求。
- 后续 enforce 阶段可直接复用 profile、trigger 和 audit payload。
