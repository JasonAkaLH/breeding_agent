# 阶段五：SkillExecutor v2 与 SlotCollection v2 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一、阶段二、阶段三
- **目标模块**：SkillExecutor、InputResolver v2、slot_collection v2、output contract validation、interrupt/resume
- **目标结果**：显式 `skill.*` node 使用 selected input schema 解析/校验参数，缺参或 invalid 进入 slot_collection v2，参数完整后执行 contract-bound entrypoint 并校验 output contract。

## 1. 范围

### 1.1 In scope

- SkillExecutor v2 读取 `SkillContract` 与 selected `SkillInputSchema`。
- InputResolver v2 以 schema inputs 为事实源。
- LLM 参数抽取使用 selected schema missing/invalid 作为 required_now。
- SlotCollection v2 持久化 `selected_schema_id`、`selected_entrypoint`、missing、invalid、resolved。
- resume 恢复 selected schema，不默认重选。
- 执行 python_subprocess 与 platform_service entrypoint。
- output contract 校验 required keys 与文件约束。
- 记录 schema/input/slot/entrypoint/output 事件。

### 1.2 Out of scope

- 不迁移具体 Skill bundle。
- 不改前端视觉体验，沿用现有 interrupt/waiting-input 能力。
- 不支持 v1 manifest execution/scripts/parameters 路径。

## 2. 现有代码锚点

| 锚点 | 当前事实 | 本阶段约束 |
| --- | --- | --- |
| `src/capabilities/skill_tool/executor.py` | 当前显式 SkillExecutor 读取旧 manifest execution/scripts/parameters。 | v2 必须只读取 contract + selected schema；旧 manifest execution/scripts/parameters 路径不得执行项目 Skill。 |
| `src/integrations/agent_skills/missing_input_interrupt.py` | 当前 `_slot_collection` 已作为 waiting-input 的结构化字段存在。 | slot_collection v2 必须沿用前端可识别 envelope，同时扩展 selected schema、entrypoint、invalid、resource_hints。 |
| `src/api/runtime.py` | resume 会把 previous slot_collection 放进 metadata。 | resume 必须恢复 v2 slot_collection，不重选 schema、不丢 resolved/invalid。 |
| `src/orchestration/service.py` | `node.waiting_for_input` 会携带 slot_collection event payload。 | v2 事件必须复用现有 waiting-input envelope，并补充 schema/entrypoint 元数据。 |

## 3. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C5-001 | schema 未选中时不得执行 entrypoint。 | ambiguous selector 只打开 interrupt。 |
| C5-002 | InputResolver v2 只看 selected schema。 | Interval 缺 `ck_spec`，RCBD 不缺。 |
| C5-003 | artifact 字段只能来自 artifact ledger。 | 文本/LLM 伪造 artifact 被拒。 |
| C5-004 | invalid 字段进入 slot_collection。 | regex/enum 不合法下轮继续要求修正。 |
| C5-005 | resume 恢复 selected schema/entrypoint。 | 用户回答后继续同一 node，不重猜 schema。 |
| C5-006 | 参数完整后执行 bound entrypoint。 | selected schema 对应 entrypoint 被调用。 |
| C5-007 | output contract 生效。 | 缺 required key fail closed；非法文件扩展拒绝 artifact。 |
| C5-008 | finalizer dependency context 安全。 | 只包含 summary/download_url/schema id，不含内部路径。 |
| C5-009 | slot_collection v2 复用现有 waiting-input envelope。 | `Interrupt.required_fields._slot_collection` 仍存在；新增字段不得破坏当前前端字段读取。 |
| C5-010 | running task 绑定 bundle revision。 | 已开始的 Skill node 使用 request metadata 中的 `skill_bundle_revision` 查 contract/schema，不被热更新影响。 |

## 4. 事件要求

必须记录：

```text
skill.input_schema_selected
skill.input_resolved
skill.slot_collection_opened
skill.entrypoint_started
skill.output_contract_validated
```


## 5. SlotCollection v2 最小形态

`_slot_collection` 必须至少包含：

```text
schema_version = 2
collection_id
round
selected_schema_id
selected_entrypoint
missing[]
invalid[]
resolved{}
slots[]
resource_hints[]
last_question
no_progress_rounds
```

前端仍通过 `_slot_collection` envelope 展示 waiting-input；新增字段主要供 runtime/checkpoint/time travel 和补槽 LLM 使用。

## 6. 测试计划

- `tests/api/test_skill_executor_runtime.py`
- `tests/api/test_skill_slot_collection.py`
- `tests/integrations/agent_skills/test_input_resolution_v2.py`
- `tests/integrations/agent_skills/test_output_contract.py`

## 7. 完成门禁

- v2 python_subprocess 与 platform_service fake Skill 均通过。
- slot_collection v2 resume 测试通过。
- output contract 测试通过。
- 无 contract Skill 或 v1 manifest execution 请求必须 fail closed，不执行。
- 当前 `_slot_collection` 展示回归与 v2 resume 恢复测试均通过。
