# 阶段二：Input Schema 与 SchemaSelector PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一 contract registry
- **目标模块**：input schema parser、schema registry、schema selector、基础 validation
- **目标结果**：支持 `schemas/*.input.yaml` 作为机器输入契约，并能按用户请求、payload、artifact summary 与 selector contract 选出 selected schema；schema 未确定时不得执行 entrypoint。

## 1. 范围

### 1.1 In scope

- 解析 `schemas/*.input.yaml` 为 `SkillInputSchema`。
- 校验 schema id/path 与 contract `input_schemas` 引用一致。
- 支持字段类型、source policy、required/default/enum/const、基础 validation、constraints。
- 新增 `SkillInputSchemaSelector`。
- 支持 selector strategy：`single_schema`、`deterministic_then_llm`。
- LLM selector 仅返回候选 schema id/confidence/reason，后端负责 allowlist 校验。
- ambiguous 时输出 missing selector fields，不执行 entrypoint。

### 1.2 Out of scope

- 不执行 Skill entrypoint。
- 不生成 final slot_collection interrupt UI。
- 不迁移现有 Skill 文件。
- 不实现主代理按需 resource 读取。

## 2. 现有代码锚点

| 锚点 | 当前事实 | 本阶段约束 |
| --- | --- | --- |
| `src/integrations/agent_skills/parameters.py` | 当前 `SkillParameterSpec.required` 是全局字段属性。 | 新 schema 的 `required` 只能在 selected schema 内生效，不能回退到 manifest-level required。 |
| `src/integrations/agent_skills/input_resolution.py` | 当前补槽时已支持用 active slot_collection.missing 作为 `required_now`。 | schema selector 必须在执行前确定 `selected_schema_id`，后续 resolver 只看 selected schema。 |
| `skill/field-design/SKILL.md` | RCBD/Diagonal/Interval 参数平铺，Interval 动态必填依赖脚本/补槽补救。 | 三种方法必须拆成独立 input schema，消除全局 required 歧义。 |

## 3. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C2-001 | schema parser 支持通用字段模型。 | string/integer/number/boolean/object/array/artifact 均有单测。 |
| C2-002 | `required` 作用域为 selected schema。 | field-design interval 要 `ck_spec`，rcbd 不要。 |
| C2-003 | artifact source policy 禁止 LLM/text 伪造。 | LLM candidate 填 artifact 被拒。 |
| C2-004 | validation 生效。 | regex/min/max/min_length/max_length 失败进入 invalid。 |
| C2-005 | constraints 生效。 | any_of/one_of/mutually_exclusive/dependencies 覆盖 OCR document/file_path。 |
| C2-006 | deterministic selector 唯一命中时选 schema。 | RCBD/Diagonal/Interval aliases 命中对应 schema。 |
| C2-007 | ambiguous 时不执行。 | “做田间试验设计” 输出 missing selector field `design`。 |
| C2-008 | resume 可固定 selected schema。 | context 带 selected_schema_id 时跳过重选。 |
| C2-009 | LLM selector 输出必须经 allowlist 校验。 | LLM 返回 contract 未声明的 schema id、低 confidence 或非 JSON 时进入 selector 补槽/错误，不执行。 |
| C2-010 | schema id 是稳定状态键。 | `selected_schema_id` 可序列化进 task metadata/checkpoint，并能在 bundle revision 内重新定位 schema。 |

## 4. Schema 支持字段

可交付版本必须支持：

```text
schema_version, schema_id, title, description, applies_when, inputs, constraints, slot_policy, entrypoint_mapping
```

字段级支持：

```text
type, required, required_when, source.allowed, aliases, patterns, default, enum, const,
description, reference_resource, clarification.hint/examples,
validation.regex/min/max/min_length/max_length/message, expose
```

## 5. 测试计划

- `tests/integrations/agent_skills/test_input_schema_parser.py`
- `tests/integrations/agent_skills/test_input_schema_selector.py`
- `tests/integrations/agent_skills/test_input_schema_validation.py`

必须包含 field-design 三 schema fixture 和 OCR any_of fixture。

## 6. 完成门禁

- schema parser / selector / validation 测试全绿。
- schema 原文不得进入 public profile。
- ambiguous selector 不会生成 entrypoint execution request。
- 非 allowlist schema id、低置信 LLM selector、坏 JSON selector 均有负向测试。
- CHANGELOG 记录 License Requirement；无依赖变更。
