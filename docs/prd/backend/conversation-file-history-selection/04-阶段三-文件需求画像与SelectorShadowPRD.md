# 阶段三：文件需求画像与 Selector Shadow PRD

- **编号**：后端 PRD 21-Phase 3
- **日期**：2026-06-19
- **状态**：已实施
- **前置阶段**：阶段二会话文件上下文与 memory 安全
- **目标模块**：`src/api/file_selection.py`、`src/api/file_selection_runtime.py`、Skill contract / input schema parser、`breeding-skill-builder` 文档模板

## 1. 阶段目标

在不改变实际执行路径的 shadow 模式下，建立文件需求识别、`FileRequirementProfile` 归一化、selector 触发判定、candidate 摘要和 would-select 审计，为后续 `enforce_narrow` 阶段提供可观测证据。

## 2. 范围

### In scope

- 定义并实现 `FileRequirementProfile` 归一化结构。
- 从 interrupt/resume、metadata、pending Skill、Skill contract/schema 和用户 query 推断文件需求。
- 支持 Skill contract / schema 中的 `file_selection` 最终字段解析；不接受 `file_intent`、旧 schema type 或别名字段作为交付契约。
- 建立 selector trigger detector，但阶段三只 shadow 记录，不写 binding、不打开新 interrupt。
- 为 active candidates 构造 prompt-safe 摘要和 recent usage 摘要，但不让 LLM 结果改变执行。
- 同步 builder 模板、checklist、指南的文件需求声明要求。

### Out of scope

- 不自动绑定文件。
- 不打开 `file_selection_ambiguous`。
- 不把 LLM selector 结果用于实际执行。
- 不开启 guarded multi-select。

## 3. FileRequirementProfile

`FileRequirementProfile` 归一化本轮为什么可能需要文件。该结构是交付级 closed schema，只接受以下最终字段；不得用 alias、legacy 字段或版本化 fallback 兜底：

```json
{
  "source": "metadata | soft_skill_binding | skill_contract | input_schema | user_query | interrupt",
  "required": true,
  "allow_multiple": false,
  "expected_content": ["材料表"],
  "supported_file_types": ["csv", "xlsx"],
  "helpful_columns": ["ped_id", "variety"],
  "disambiguation_hint": "优先选择最近实际用于本会话设计任务的材料表",
  "user_file_reference": "刚才上传的表",
  "context_notes": ["当前 Skill schema 有 required data 输入", "用户提到刚才上传"]
}
```

字段约束：

| 字段 | 要求 |
| --- | --- |
| `source` | 枚举：`metadata`、`soft_skill_binding`、`skill_contract`、`input_schema`、`user_query`、`interrupt`。 |
| `required` | boolean；是否必须选择文件才能继续。 |
| `allow_multiple` | boolean；是否允许多文件作为同一需求的有效输入。 |
| `expected_content` | string array；描述期望文件内容、业务语义或数据对象。 |
| `supported_file_types` | string array；允许的文件类型或归一化类型，例如 `csv`、`xlsx`、`txt`、`image`。 |
| `helpful_columns` | string array；用于表格类文件消歧的关键列名提示。 |
| `disambiguation_hint` | string；候选多个时的业务消歧提示。 |
| `user_file_reference` | string；用户原话中的文件指代片段。 |
| `context_notes` | string array；解释 profile 来源和推断依据，只能包含安全摘要。 |

以下旧字段不得出现在交付契约中：`needs_file`、`intent`、`accepted_file_types`、`expected_inputs`、`requires_file`、`required_file`、`default_allow_multiple`。若这些字段出现在 metadata、contract 或 schema 中，应作为契约错误处理，不得静默映射到最终字段。

归一化来源优先级：

1. interrupt / resume 上下文中的文件需求；
2. 显式 `metadata.file_requirement_profile` / `metadata.file_selection`；
3. soft / pending Skill binding 中的 file profile；
4. Skill contract / input schema 的 `file_selection`；
5. 用户 query 中的文件指代、continuation 词和比较 / 合并意图。

显式 `metadata.upload_ids` 存在时直接退出 selector，不生成 profile-driven selection。

## 4. Trigger detector

必须触发 shadow 记录或进入等价缺文件观察分支：

- `FileRequirementProfile.required=true` 且本轮没有显式 `metadata.upload_ids`。
- 用户 query 明确要求“用这个文件 / 刚才那个表 / materials.csv / 第一份文件”等文件指代，或直接包含 `upload_id`，且当前文件池需要缩窄或写入 task-level provenance。
- 多个 active 文件同名、同类型或都满足当前 Skill 文件需求，且下游只接受单文件。
- continuation 明确要求“继续用刚才那个数据”，需要基于 recent usage 判断。
- interrupt answer 恢复时需要把自然语言选择解析为 upload_id。
- future Skill schema / contract 声明 required file input。

触发判定必须在“已有 active conversation files 已注入上下文”之后仍可运行：active context 只表示文件池可用，不代表本轮已经选择文件，也不得让 required / narrowing 场景跳过 shadow audit。

正文 `upload_id` 识别必须使用当前上传 ID 生成规则的完整 token：`upl-` + 12 位十六进制字符，正则为 `(?<![A-Za-z0-9_-])upl-[0-9a-fA-F]{12}(?![A-Za-z0-9_-])`。substring 命中、编辑距离、前缀补全或把 `upl_` / `upload_` 当作等价格式都不得触发精准选择。

不应触发 selector：

- 本轮已有显式 `metadata.upload_ids`。
- 用户明确说“不需要文件 / 不用上传的文件”。
- 普通问答且无文件指代、无 required file profile。
- 只有 conversation file context 注入即可满足普通 summarization / exploratory query，且平台策略不要求 task-level binding。

若 query、Skill contract/schema 或 interrupt 明确需要文件，但当前会话没有 active 文件，平台在 shadow 阶段只记录 would-no-usable-file，不改变执行路径；`enforce_narrow` 阶段再打开缺文件澄清。

## 5. Skill contract 与 builder 同步

Skill 文件需求必须进入 machine-readable contract/schema，不得只写在 `SKILL.md` 或 prose reference。

标准 schema 字段：

```yaml
properties:
  material_file:
    type: data
    description: 实验材料表
    file_selection:
      required: true
      allow_multiple: false
      expected_content: [材料表]
      supported_file_types: [csv, xlsx]
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
2. 模板增加 `file_selection` 最终字段示例。
3. checklist 增加：文件类 input 是否可归一化为 `FileRequirementProfile`。
4. 明确脚本继续通过 `resource_manifest_path` / `files[].mount_path` 读取文件；文件需求声明不得依赖 `uploaded_artifacts[].content` / `content_base64` 或 prose 描述。
5. `file_selection` 必须使用最终字段；builder 应拒绝 `file_intent`、`accepted_file_types`、`intent`、`expected_inputs`、`needs_file` 等旧字段。

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
| profile 来源优先级 | interrupt > metadata > pending skill > schema > user query。 |
| 显式 upload_ids | 不触发 profile-driven selector。 |
| required file schema | 归一化为 `required=true`、`supported_file_types`、`expected_content`、`allow_multiple`。 |
| 非标准字段 | `file_intent`、`accepted_file_types`、`intent`、`expected_inputs`、`needs_file` 等旧字段触发契约错误，不被 alias 映射。 |
| 普通问答 | 不触发 selector shadow。 |
| continuation | “继续用刚才那个数据”触发 recent usage 需求。 |
| upload_id token | 只完整匹配 `upl-[0-9a-fA-F]{12}`；嵌在更长 token 中不命中。 |
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
- 后续 `enforce_narrow` 阶段可直接复用 profile、trigger 和 audit payload。


## 9. 实施记录（2026-06-19）

- **实现范围**：`FileRequirementProfile` 已收敛为 closed schema，仅接受最终字段和 `metadata/soft_skill_binding/skill_contract/input_schema/user_query/interrupt` 来源；metadata、Skill contract、input schema 与 soft binding 中的 `file_intent` / legacy alias 字段会 fail-closed，不再静默映射。
- **Skill contract/schema**：`skill.contract.yaml` 支持顶层最终 `file_selection`；schema 字段级 `file_selection` 只接受 `required/allow_multiple/expected_content/supported_file_types/helpful_columns/disambiguation_hint`，不再从 `type: file/artifact/data`、field `required`、标题/描述或 `validation.file_extensions` 推断文件需求画像。
- **Shadow selector**：`shadow` 模式会在 active conversation files 已注入的情况下继续记录 `conversation_file.file_selector_invoked`、`conversation_file.file_selector_decision_recorded` 和 malformed LLM 输出的 `conversation_file.file_selector_invalid_output`，但不绑定 task attachment、不打开 `file_selection_ambiguous` interrupt、不让 LLM selector 结果改变执行 metadata。
- **安全审计**：audit payload 只包含 profile 摘要、candidate 安全元数据/hash、decision/confidence/reason 与 would flags；不记录 raw prompt、raw selector output、文件正文、`content_base64`、`storage_key`、本地路径、secret。
- **builder 文档**：已更新本地 installed `.codex/skills/breeding-skill-builder/` 的 `SKILL.md`、`references/templates.md`、`references/checklist.md` 与 `references/Skill构建指南.md`，改为最终 `file_selection` 字段、legacy 字段拒绝、manifest / `mount_path` 主读取路径。该 `.codex/` 目录按仓库策略被 `.gitignore` 忽略，本阶段不 force-add；tracked `CHANGELOG.md` 记录该边界，独立 builder 仓库如需发布应另行同步。
- **关键验证**：`tests/integrations/agent_skills/test_input_schema_parser.py`、`tests/integrations/agent_skills/test_skill_contract_parser.py`、`tests/integrations/agent_skills/test_public_skill_profile.py`、`tests/api/test_conversation_file_selection.py`、`tests/api/test_pending_skill_context.py`，以及 builder `quick_validate.py`。
