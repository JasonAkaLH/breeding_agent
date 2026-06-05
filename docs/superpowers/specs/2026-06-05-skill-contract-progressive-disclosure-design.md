# Skill Contract 渐进式披露与显式执行 PRD

- **状态**：设计确认版，待实施计划拆解
- **日期**：2026-06-05
- **目标模块**：Skill runtime、Skill capability 注册、main-agent soft binding / planner / replanner、SkillExecutor、slot_collection、Skill 资源读取、项目级 `skill/*` bundle
- **目标结果**：把当前膨胀的 `SKILL.md` frontmatter 拆分为轻量 `SKILL.md`、平台执行契约 `skill.contract.yaml`、机器输入 schema `schemas/*.input.yaml` 与按需资源读取；所有新格式 Skill 通过显式 `skill.*` 节点执行，不再依赖 main-agent 内部隐式 `auto_run`。

## 1. 问题陈述

当前项目级 Skill 把多类职责塞进 `SKILL.md` 顶部 YAML：

1. **发现/展示信息**：`name`、`display_name`、`description`、`triggers`、`capability_id`。
2. **公开说明信息**：`public_usage`。
3. **执行契约信息**：`execution`、`scripts`、`outputs`、`parameters`。
4. **参数 schema**：所有方法的参数平铺在一个全局 `parameters` 下。

这导致几个长期问题：

- `SKILL.md` 既像 Codex Skill 说明，又像平台 runtime manifest，职责混杂。
- 参数 schema 与公开说明重复，例如 `public_usage.parameters` 与顶层 `parameters` 同时描述同一业务概念。
- 全局 `parameters.required` 不能表达“按方法动态必填”，例如 field-design 的 `ck_spec` 对 Interval 必填、对 RCBD 不必填。
- main-agent 仍存在旧的隐式路径：进入 `main_agent.respond` 后内部 `match_skills -> scripts[].auto_run -> run script`，导致任务图看不到显式 `skill.*` 节点，不利于 checkpoint/time travel 和 v2 契约。
- 主代理目前不会按 `SKILL.md` 正文中的资源索引自动读取 `references/`；如果要使用渐进式披露，需要平台提供受控资源读取机制。

需要把 Skill bundle 重新设计成：

```text
SKILL.md                  # 轻量 agent-facing 入口
skill.contract.yaml       # 平台 capability / runtime / entrypoint / output 契约
schemas/*.input.yaml      # 机器可读输入 schema，按场景/方法拆分
references/*              # 主代理/用户帮助/补槽可按需读取的说明资源
scripts/ 或 runtime/       # 实际执行实现
```

## 2. 目标与非目标

### 2.1 目标

- **G1 轻量 `SKILL.md`**：新格式 `SKILL.md` frontmatter 只保留 `name` 与 `description`；正文只保留用途摘要、资源索引和主代理边界。
- **G2 平台 contract 独立**：capability 注册、执行模式、entrypoint、output contract、schema selector、资源策略由 `skill.contract.yaml` 承载。
- **G3 输入 schema 渐进披露**：参数 schema 从 `SKILL.md` 移到 `schemas/*.input.yaml`，按业务场景/方法拆分，例如 RCBD / Diagonal / Interval 各自独立。
- **G4 动态必填语义正确**：`required` 作用域是“当前已选 input schema”，不是全局 Skill manifest。
- **G5 主代理按需读取资源**：主代理不直接打开任意路径，而通过 `SkillResourceService` 按 `capability_id + resource_id/path + audience` 读取 bundle 内资源；不逐文件白名单，采用 bundle 内默认可读 + 黑名单 + 硬安全规则。
- **G6 显式执行**：新格式 Skill 只能在被规划为显式 `skill.*` node 后执行；main-agent 不再内部执行 Skill scripts。
- **G7 移除新格式 `auto_run`**：`auto_run` / `run_by_default` 不属于新 contract；entrypoint 由 selected input schema 显式绑定。
- **G8 兼容旧格式**：未迁移旧 Skill 通过 legacy adapter 继续运行；存在新 contract 时优先新 contract，禁止新旧执行契约混用。
- **G9 v2/checkpoint 友好**：schema selection、resource read、slot_collection、entrypoint execution、output contract validation 都可作为显式事件记录。

### 2.2 非目标

- 不实现通用本地 Codex Skill runtime；本 PRD 只约束本项目后端平台 Skill。
- 不允许主代理读取脚本、handler、secret、config 或任意越界路径。
- 不让 LLM 决定最终可执行性；LLM 只参与意图判断、schema 选择辅助、补槽问题生成和候选参数抽取，后端负责校验与裁决。
- 不把 SQL guard、数据库权限、OCR 远端配置等内部业务安全规则塞进 input schema；input schema 只描述进入 Skill 前的用户输入契约。
- 不要求用户必须 slash command；自然语言仍可由 planner/router/replanner 触发显式 `skill.*` node。

## 3. 当前状态与证据

| 证据 | 当前行为 |
| --- | --- |
| `Skill构建指南.md` | 当前把 `capability_id`、`public_usage`、`parameters`、`scripts`、`execution` 等都列为 `SKILL.md` frontmatter 字段。 |
| `/Users/yinpeihai/.codex/skills/.system/skill-creator/SKILL.md` | 标准 Codex Skill 只要求 `name` / `description` frontmatter；正文用于 instructions/resources，强调 progressive disclosure。 |
| `src/integrations/agent_skills/parser.py` | 只解析 `SKILL.md` frontmatter；`parameters` / `scripts` / `outputs` 来自顶部 YAML；正文路径索引不会被 runtime 自动读取。 |
| `src/integrations/agent_skills/parameters.py` | `SkillParameterSpec.required` 当前是全局参数 required，不能表达按 schema 的 required_now。 |
| `src/capabilities/main_agent/executor.py` | 存在 `_resolve_skill_matches()` + `_run_auto_scripts()` 隐式路径；匹配到 Skill 后可在 `main_agent.respond` 内部执行 `scripts[].auto_run`。 |
| `src/capabilities/skill_tool/executor.py` | 显式 `SkillExecutor` 已作为 `skill.*` node executor 存在；当前仍依赖旧 manifest execution/scripts/parameters。 |
| `src/orchestration/skill_workflow_provider.py` | `skill.*` public capability 可展开为 Skill node + finalizer；这是新设计应保留并强化的主路径。 |
| `src/orchestration/soft_skill_replanner.py` | slash soft binding 的 execute signal 已能 replan 到 `skill.*`。 |
| `skill/field-design/SKILL.md` | RCBD / Diagonal / Interval 参数平铺在一个 `parameters` 下，`ck_spec` 只能设为非 required，导致 Interval 动态必填需要脚本/slot_collection 补救。 |
| `skill/sql-query/SKILL.md` | platform_service contract 与 public_usage 也放在 `SKILL.md` frontmatter 中。 |

## 4. 设计原则

1. **职责分离**：`SKILL.md` 面向 agent/human，`skill.contract.yaml` 面向平台，`schemas/*.input.yaml` 面向参数解析/补槽/校验，`references/*` 面向按需说明。
2. **显式节点**：所有新格式 Skill 执行必须出现在任务图中，不能藏在 `main_agent.respond` 内部。
3. **schema-first 补槽**：slot_collection 的 `missing/invalid` 来自 selected input schema；resume 时恢复 `selected_schema_id`，不重新猜、不看旧 `manifest.required`。
4. **受控按需读取**：主代理可以按需读取 Skill 公开资源，但读取必须通过平台服务、受 bundle 边界和黑名单控制、可审计。
5. **默认可读但不裸奔**：Skill bundle 内资源读取不做逐文件白名单；默认允许读取 bundle 内文件，但强制应用路径越界保护、硬黑名单、audience 策略、大小限制、脱敏和审计。
6. **旧格式只兼容**：legacy adapter 只为现有未迁移 Skill 服务；新规范和新项目级 Skill 不支持 `auto_run`。

## 5. 目标文件结构

以 `field-design` 为例：

```text
skill/field-design/
  SKILL.md
  skill.contract.yaml

  schemas/
    rcbd.input.yaml
    diagonal.input.yaml
    interval.input.yaml

  references/
    usage.md
    material-data.md
    rcbd.md
    diagonal.md
    interval.md

  scripts/
    run_field_design.py
```

`SKILL.md` 示例：

```markdown
---
name: field-design
description: 基于材料清单生成田间试验设计；适用于随机区组、对角线增广、间比法、fieldbook、小区排布和田间布局预览。
---

# Field Design

## 什么时候使用
- 用户要求生成田间试验设计、fieldbook、小区排布或田间布局预览。
- 支持 RCBD、Diagonal、Interval 三类设计。

## 资源索引
- 平台执行契约：`skill.contract.yaml`
- 输入 schema：`schemas/`
- 用户用法说明：`references/usage.md`
- 材料表格式说明：`references/material-data.md`

## 主代理边界
- 如果用户询问字段含义、材料格式、参数解释，只回答用法，不执行。
- 如果用户明确要求生成设计，交给平台按 contract 和 input schema 执行。
- 不要暴露脚本路径、handler、服务、secret、config 或本地路径。
```

`SKILL.md` 的资源索引是人类和 agent 导航，不是 runtime 事实源。runtime 事实源是 `skill.contract.yaml`。

## 6. `skill.contract.yaml` 契约

### 6.1 顶层结构

```yaml
contract_version: 1

capability:
  id: skill.field_design
  display_name: 试验设计智能体
  visibility: public

routing:
  triggers:
    - 试验设计
    - 田间试验设计
    - 随机区组设计
    - 对角线增广设计
    - 间比法设计
  examples:
    - 用这个材料表做 RCBD，3 个重复
    - 生成间比法设计

runtime:
  execution_mode: python_subprocess
  answer_mode: requires_finalizer
  trust_scope: project

entrypoints:
  run_field_design:
    kind: python_subprocess
    path: scripts/run_field_design.py
    timeout_seconds: 300
    output_contract: field_design_output

input_schemas:
  - schema_id: field-design.rcbd
    path: schemas/rcbd.input.yaml
    entrypoint: run_field_design
    selector: default
  - schema_id: field-design.diagonal
    path: schemas/diagonal.input.yaml
    entrypoint: run_field_design
    selector: default
  - schema_id: field-design.interval
    path: schemas/interval.input.yaml
    entrypoint: run_field_design
    selector: default

schema_selectors:
  default:
    strategy: deterministic_then_llm
    candidates:
      rcbd:
        schema_id: field-design.rcbd
        aliases: [rcbd, RCBD, 随机区组, 随机完全区组]
      diagonal:
        schema_id: field-design.diagonal
        aliases: [diagonal, 对角线, 对角线增广]
      interval:
        schema_id: field-design.interval
        aliases: [interval, 间比法, 间比法设计]
    when_ambiguous:
      ask_for: design
      clarification_hint: 需要确认试验设计类型：随机区组、对角线增广或间比法。

output_contracts:
  field_design_output:
    required: [answer]
    files:
      - role: fieldbook
        extensions: [.csv]
      - role: layout_preview
        extensions: [.html]

resources:
  public:
    usage:
      path: references/usage.md
      kind: usage
      audience: [main_agent, user_help]
      max_tokens: 1200
    material_data:
      path: references/material-data.md
      kind: input_reference
      audience: [main_agent, slot_question]
      max_tokens: 1200
    interval_help:
      path: references/interval.md
      kind: method_reference
      audience: [main_agent, slot_question]
      max_tokens: 1000

resource_policy:
  main_agent:
    default: allow
    deny:
      - config.yaml
      - .env
      - .env.*
      - "**/scripts/**"
      - "**/runtime/**"
      - "**/native/**"
      - "**/outputs/**"
      - "**/.git/**"
      - "**/__pycache__/**"
      - "**/*secret*"
      - "**/*token*"
      - "**/*credential*"
  runtime:
    default: allow
    deny:
      - .env
      - .env.*
      - "**/.git/**"
      - "**/__pycache__/**"
```

### 6.2 Capability 注册

能力注册事实源从旧的 `SKILL.md frontmatter.capability_id` 改为：

```text
skill.contract.yaml capability.id
  -> SkillCapabilityRegistry
  -> global CapabilityRegistry
  -> /api/v1/capabilities
  -> frontend slash command
  -> main_agent public capability context
```

注册出的 public descriptor 至少包含：

```json
{
  "capability_id": "skill.field_design",
  "display_name": "试验设计智能体",
  "description": "来自 SKILL.md description",
  "kind": "skill",
  "public": true,
  "source": "skill"
}
```

### 6.3 Entrypoint

新 contract 不支持：

```yaml
auto_run: true
run_by_default: true
```

入口由 selected input schema 明确绑定：

```text
selected_schema_id -> input_schemas[].entrypoint -> entrypoints[entrypoint]
```

如果无法选择 schema，则不执行 entrypoint，生成 selector 补槽。

### 6.4 Platform service

`platform_service` 使用同一 contract：

```yaml
entrypoints:
  sql_query:
    kind: platform_service
    handler: skill.sql_query.platform_handler
    handler_module: runtime/sql_query_skill/platform_handler.py
    handler_factory: build_handler
    services:
      - mysql_readonly
      - llm.non_stream
      - artifact_writer
      - progress_events
    output_contract: sql_query_output
```

服务绑定仍必须由 runtime allowlist 和 trust scope fail-closed 管理。

## 7. Input Schema Contract

### 7.1 通用结构

```yaml
schema_version: 1
schema_id: field-design.interval
title: 间比法试验设计输入
description: 生成间比法田间试验设计所需输入参数。

applies_when:
  intent_aliases: [间比法, interval, 间比法设计]
  discriminator:
    field: design
    values: [interval]

inputs:
  material_data:
    type: artifact
    required: true
    source:
      allowed: [artifact]
    accepted_file_types: [csv, xlsx, xls]
    description: 试验材料清单。
    reference_resource: material_data

  design:
    type: string
    required: true
    const: interval
    aliases: [design, design_type, 设计类型, 试验设计]
    description: 设计类型，本 schema 固定为 interval。

  ncols:
    type: integer
    required: true
    source:
      allowed: [payload, text, metadata]
    aliases: [ncols, 列数, 田块列数]
    patterns:
      - '(?:ncols|列数|田块列数)\s*[:：=]?\s*(\d+)'
      - '(\d+)\s*(?:列|columns?)'
    description: 田块布局列数。
    clarification:
      hint: 提醒用户提供一个正整数列数。
      examples: [10, 12]
    validation:
      min: 1
      message: 田块列数必须是正整数。

  ck_spec:
    type: string
    required: true
    source:
      allowed: [payload, text, metadata]
    aliases: [ck_spec, CK参数, CK间隔]
    description: 间比法 CK 参数，格式为 ck_no,start_pos,interval；多个 CK 用分号分隔。
    reference_resource: interval_help
    clarification:
      hint: 提醒用户按 ck_no,start_pos,interval 提供；多个 CK 用分号分隔。
      examples:
        - "1,2,8"
        - "1,2,8; 2,6,11; 3,1,9"
    validation:
      regex: '^\s*\d+\s*,\s*\d+\s*,\s*\d+(\s*;\s*\d+\s*,\s*\d+\s*,\s*\d+)*\s*$'
      message: CK 参数格式应为 ck_no,start_pos,interval；多个 CK 用分号分隔。

constraints:
  any_of: []
  one_of: []
  mutually_exclusive: []
  dependencies: []

slot_policy:
  ask_strategy: llm_generated
  max_rounds: 5
  group_hints:
    - group_id: interval_core
      fields: [ncols, ck_spec]
      clarification_hint: 可以一次性询问田块列数和 CK 参数；CK 参数需说明格式。

entrypoint_mapping:
  material_data: material_data
  design: design
  ncols: ncols
  ck_spec: ck_spec
```

### 7.2 `required` 语义

`required` 表示：

> 当前 input schema 被选中后，该字段是本次执行的 required_now。

它不表示全局 Skill 必填。例：

- `field-design.interval` 中 `ck_spec.required=true`。
- `field-design.rcbd` 中不存在 `ck_spec` 或 `ck_spec.required=false`。
- resume 时 `slot_collection.missing/invalid` 是本轮补槽权威。

### 7.3 字段类型

可交付版本必须支持：

```text
string, integer, number, boolean, object, array, artifact
```

字段能力包括：

- `required`
- `required_when`
- `source.allowed`
- `aliases`
- `patterns`
- `default`
- `enum`
- `const`
- `description`
- `reference_resource`
- `clarification.hint/examples`
- `validation.regex/min/max/min_length/max_length/message`
- `expose.to_llm/to_user/to_entrypoint`

### 7.4 Artifact 安全

artifact 参数只能来自 artifact context / task input attachment ledger：

- LLM 不得填 artifact 参数。
- 文本路径不得伪造成上传 artifact。
- artifact value 只能保存 upload/artifact descriptor，不保存 raw content。
- 缺 artifact 时生成上传型 interrupt 或普通追问，具体 UI 由现有上传控件承载。

## 8. SkillResourceService 按需读取

### 8.1 服务定位

资源按需读取不放在 `SkillExecutor` 内。新增独立服务：

```text
SkillResourceService / SkillResourceReader
```

调用方：

- `MainAgent`：回答用法、字段解释、示例时读取 public resources。
- `SlotQuestionGenerator`：生成专业补槽问题时读取相关 public resources。
- `SchemaSelector/InputResolver`：读取 machine schemas 或 schema references。
- `SkillExecutor`：执行侧如需读取 runtime 资源，也通过该服务但 audience 为 `runtime`。

### 8.2 接口概念

```python
@dataclass(frozen=True)
class SkillResourceReadRequest:
    capability_id: str
    resource_id: str | None = None
    path: str | None = None
    audience: str  # main_agent | slot_question | runtime | schema_selector
    max_tokens: int | None = None
```

```python
@dataclass(frozen=True)
class SkillResourceReadResult:
    capability_id: str
    resource_id: str | None
    path: str
    content: str
    content_type: str
    token_count: int
    truncated: bool
    redactions: tuple[str, ...]
```

### 8.3 读取策略

- 不逐文件白名单；bundle 内默认允许读取。
- 所有读取必须限制在 Skill bundle 根目录内。
- 禁止 `../` 越界、绝对路径越界、symlink 越界。
- 全局硬黑名单永远生效，包括 `.git/`、`.env*`、secret/token/credential 文件、缓存目录。
- `main_agent` / `slot_question` audience 读取到 prompt-facing 内容前必须拒绝脚本、runtime、native、config、outputs 等内部实现目录。
- 非文本或超大资源不得原样进入 prompt；返回 metadata 或裁剪内容，并标记 `truncated=true`。
- 返回前做 secret/token/password/base_url 等脱敏扫描。
- 每次读取记录 `skill.resource_read` audit event，包含 capability、resource/path、audience、truncated、redaction count，不记录敏感原文。

### 8.4 Public profile 中的资源索引

主代理初始只看到资源目录，不看到所有全文：

```json
{
  "resource_index": [
    {
      "resource_id": "usage",
      "kind": "usage",
      "summary": "整体用法、输入输出、常见示例",
      "when_to_read": "用户询问这个 Skill 怎么用、支持什么输出时读取"
    },
    {
      "resource_id": "interval_help",
      "kind": "method_reference",
      "summary": "间比法参数说明和 CK 参数格式",
      "when_to_read": "用户询问间比法、CK 参数或 interval 设计时读取"
    }
  ]
}
```

主代理按需请求：

```text
SkillResourceService.read(capability_id="skill.field_design", resource_id="interval_help", audience="main_agent")
```

主代理不直接解析路径，也不读取 machine schema 原文。

## 9. 主代理与调用流程

### 9.1 Capability 注册到主代理

```text
skill.contract.yaml capability
  -> SkillRuntimeState active bundle
  -> SkillCapabilityRegistry
  -> global CapabilityRegistry
  -> public capability context
  -> MainAgent public skill profile
```

`build_public_skill_profile()` 输入：

- `SKILL.md` name/description/body 摘要
- `skill.contract.yaml` capability/routing/resources
- input schema summaries
- resource index

输出不包含：

- script path
- handler module
- services
- validation regex 全文
- config 路径
- secret 或内部 runtime 细节

### 9.2 Slash soft binding

用户：

```text
/field-design ck_spec 怎么填？
```

流程：

```text
main_agent.respond + metadata.soft_skill_binding
  -> main_agent 读取 public profile/resource index
  -> 必要时 SkillResourceService 读取 public resource
  -> 判断 answer
  -> 直接回答用法，不执行 skill node
```

用户：

```text
/field-design 用这个材料表做间比法设计
```

流程：

```text
main_agent.respond soft binding
  -> decision=execute
  -> SoftSkillBindingReplanner
  -> 显式 skill.field_design node
  -> SkillExecutor
```

### 9.3 自然语言触发

用户不写 slash：

```text
帮我用这个材料表生成间比法田间设计
```

允许路径：

```text
planner/router/replanner 根据 public capability profile 规划 skill.field_design
```

禁止路径：

```text
main_agent.respond 内部 match_skills 后直接执行 scripts
```

### 9.4 Finalizer

当 `answer_mode=requires_finalizer`：

```text
skill.field_design -> main_agent.respond finalizer
```

finalizer 必须：

- 禁用 Skill invocation / matching。
- 只总结上游 Skill 安全输出。
- 只根据平台 artifact `download_url` 声称文件可下载。
- 不按需读取无关 Skill resources。
- 不触发第二个 skill node。

## 10. SkillExecutor v2 流程

显式 `skill.*` node 执行：

```text
resolve skill by capability_id
  -> load SkillContract
  -> select input schema
  -> resolve inputs
  -> validate inputs and constraints
  -> if missing/invalid: build slot_collection v2 interrupt
  -> map payload to entrypoint input
  -> execute python_subprocess / platform_service
  -> validate output_contract
  -> return result / artifacts / events
```

### 10.1 Schema selection

规则：

1. resume metadata 有 `selected_schema_id` 时优先恢复。
2. `single_schema` 直接选择。
3. deterministic alias/pattern/explicit payload 命中唯一候选时选择。
4. 多候选/弱命中时可调用 LLM selector。
5. 仍不确定时生成 selector slot_collection，例如缺 `design`。
6. schema 未选中前不得执行 entrypoint。

事件：

```json
{
  "event_type": "skill.input_schema_selected",
  "payload": {
    "schema_id": "field-design.interval",
    "source": "deterministic",
    "confidence": "high"
  }
}
```

### 10.2 Input resolution

新 resolver 以 `SkillInputSchema.inputs` 为事实源：

```python
resolve_skill_inputs_v2(schema, base_payload, context)
```

来源顺序：

1. payload
2. metadata
3. artifact
4. text/current_user_message/resolved_user_message
5. LLM scalar/object extraction
6. default

规则：

- LLM 输出只作为候选。
- 所有候选必须 type coerce + validation。
- artifact 只能来自 artifact context。
- default 只在字段允许 default source 时生效。
- constraints 失败进入 invalid 或 missing。

输出：

```json
{
  "selected_schema_id": "field-design.interval",
  "payload": {...},
  "missing": ["ck_spec"],
  "invalid": {},
  "sources": {...}
}
```

### 10.3 SlotCollection v2

```json
{
  "schema_version": 2,
  "collection_id": "...",
  "capability_id": "skill.field_design",
  "skill_name": "field-design",
  "selected_schema_id": "field-design.interval",
  "selected_entrypoint": "run_field_design",
  "round": 1,
  "status": "collecting",
  "missing": ["ck_spec"],
  "invalid": {},
  "resolved": {
    "design": "interval",
    "ncols": 10
  },
  "resource_hints": ["interval_help"],
  "last_question": "..."
}
```

resume 规则：

- 恢复 `selected_schema_id` 和 `selected_entrypoint`。
- 默认不重选 schema。
- `missing + invalid` 是本轮 required_now 权威。
- 用户伪造 `_slot_collection` 被拒绝；只能使用后端保存的 interrupt metadata。
- 如果用户明确要求切换方法，按 contract 策略处理：允许切换则清理不兼容字段并重建 slot_collection；不允许则提示新开任务。

## 11. 现有 Skill 目标迁移形态

### 11.1 field-design

```text
schemas/rcbd.input.yaml
schemas/diagonal.input.yaml
schemas/interval.input.yaml
references/usage.md
references/material-data.md
references/rcbd.md
references/diagonal.md
references/interval.md
```

验收：

- RCBD schema 需要 `material_data/design/blocks`，不要求 `ck_spec`。
- Diagonal schema 按业务规则要求 `material_data/design/ncols` 等。
- Interval schema 要求 `material_data/design/ncols/ck_spec`。
- 缺 design 时先追问设计类型。
- output contract 校验 CSV/HTML artifact。

### 11.2 field-analysis

```text
schemas/rcbd-analysis.input.yaml
schemas/diagonal-analysis.input.yaml
references/field-data.md
```

验收：

- RCBD / Diagonal 两 schema 可选。
- 缺 `field_data` 或 `design` 进入 slot_collection。
- JSON report output contract 生效。

### 11.3 rice-genie

```text
schemas/qtn-check.input.yaml
schemas/report-from-gene-check.input.yaml
references/vcf-input.md
references/gene-check-json.md
```

验收：

- VCF/VCF.GZ 选择 qtn-check。
- gene_check JSON 选择 report-from-gene-check。
- `sample/samples/run_id` 可选参数解析。
- Markdown report output contract 生效。

### 11.4 OCR

```text
schemas/document-ocr.input.yaml
references/supported-files.md
references/output-formats.md
```

`document-ocr.input.yaml` 必须表达：

```yaml
constraints:
  any_of:
    - [document]
    - [file_path]
```

验收：

- 上传图片/PDF 或 file_path 二选一。
- `output_format` enum 校验。
- 缺两者时补槽。
- OCR 错误码保留。

### 11.5 SQLQuery

```text
schemas/readonly-query.input.yaml
references/query-examples.md
references/data-boundaries.md
runtime/sql_query_skill/
```

验收：

- platform_service contract 注册。
- `query` schema 生效。
- 服务 allowlist 生效。
- SQL guard / readonly adapter 仍在 handler 内部。

## 12. 后端模型清单

### 12.1 新增/重构模型

```text
AgentSkillManifest
SkillContract
SkillCapabilityContract
SkillRoutingContract
SkillRuntimeContract
SkillEntrypointContract
SkillInputSchemaRef
SkillSchemaSelectorContract
SkillOutputContract
SkillInputSchema
SkillInputField
SkillInputSourcePolicy
SkillValidationRules
SkillResourcePolicy
SkillResourceReadRequest
SkillResourceReadResult
SkillSchemaSelectionResult
SkillInputResolutionResultV2
```

### 12.2 Legacy adapter

```text
legacy SKILL.md frontmatter
  -> LegacySkillContractAdapter
  -> SkillContract-like object
```

规则：

- `skill.contract.yaml` 存在时优先使用新 contract。
- 新旧执行契约同时存在且冲突时 fail closed 或 startup diagnostic。
- 旧 `auto_run` 只存在 legacy adapter；新 contract parser 遇到 `auto_run` fail closed。

## 13. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| FR-001 | 新格式 Skill 使用 `skill.contract.yaml` 注册 capability。 | `/api/v1/capabilities` 返回 contract capability id/display_name。 |
| FR-002 | 新格式 `SKILL.md` frontmatter 只保留 `name/description`。 | parser 不再要求新 Skill 在 frontmatter 写 execution/parameters/scripts。 |
| FR-003 | 新 contract 不支持 `auto_run/run_by_default`。 | contract parser 遇到该字段 fail closed。 |
| FR-004 | 主代理不再内部执行新格式 Skill scripts。 | `main_agent.respond` 不调用新格式 Skill entrypoint。 |
| FR-005 | 普通自然语言仍可规划到显式 `skill.*` node。 | 无 slash 的 field-design 请求出现 skill node。 |
| FR-006 | SchemaSelector 选择 input schema 后才执行 entrypoint。 | ambiguous 不执行，生成 selector slot_collection。 |
| FR-007 | InputResolver v2 使用 selected schema required。 | Interval 要 `ck_spec`，RCBD 不要。 |
| FR-008 | SlotCollection v2 持久化 `selected_schema_id`。 | resume 使用同一 schema。 |
| FR-009 | LLM 追问由 schema/resource/runtime context 生成。 | 问题非硬编码，ask_fields 是 missing/invalid 子集。 |
| FR-010 | SkillResourceService 支持主代理按需读取。 | 用法问题读取相关 reference 并回答。 |
| FR-011 | ResourceService 默认 bundle 内可读但应用黑名单和安全边界。 | scripts/runtime/config/secret/越界读取被拒。 |
| FR-012 | OutputContract 校验 required keys 和文件约束。 | 缺 required 或扩展名不允许 fail closed/拒绝 artifact。 |
| FR-013 | finalizer 不触发二次 Skill。 | finalizer metadata 禁止 skill invocation。 |
| FR-014 | 旧格式 Skill 继续兼容。 | 未迁移 legacy 测试通过。 |

## 14. 非功能需求

| 类别 | Requirement |
| --- | --- |
| 安全 | 所有资源读取限制在 bundle 根目录内；禁止越界、硬黑名单、脱敏、大小限制。 |
| 隐私 | 主代理 prompt 不包含 secret/config/raw artifact content/provider 配置。 |
| 可观测 | 记录 schema selection、resource read、input resolved/rejected、slot updated、output contract validation 事件。 |
| 可恢复 | slot_collection v2 可从 interrupt/resume 恢复 selected schema 与已解析字段。 |
| 性能 | public profile 初始只放 resource index/schema summary，不加载全部 references。 |
| 兼容 | legacy adapter 保留旧格式运行能力，但新 contract 优先。 |
| 可测试 | 每个 FR 至少有 parser/integration/API/e2e/frontend 或 contract 测试覆盖。 |

## 15. 测试矩阵

### 15.1 Contract parser

- 解析合法 `skill.contract.yaml`。
- 缺 `capability.id` fail closed。
- `capability.id` 非 `skill.*` fail closed。
- public capability 缺 `display_name` fail closed。
- entrypoint 引用不存在 output contract fail closed。
- input schema path 越界 fail closed。
- `auto_run` / `run_by_default` 出现在新 contract fail closed。
- platform_service 缺 handler fail closed。
- python_subprocess 声明 services fail closed。

### 15.2 Input schema parser / validator

- 支持 string/integer/number/boolean/object/array/artifact。
- `schema_id` 与 contract ref 不一致 fail closed。
- required/default/enum/const 生效。
- validation regex/min/max/min_length/max_length 生效。
- constraints any_of/one_of/mutually_exclusive/dependencies 生效。
- artifact source policy 拒绝 LLM/text 伪造。

### 15.3 Capability 注册 / public profile

- 新格式 Skill 注册到 `/api/v1/capabilities`。
- capability id/display_name 来自 contract。
- public profile 包含 resource index/schema summaries。
- public profile 不包含 script path/handler module/services/config/regex 全文。
- 新旧 capability id 冲突 fail closed 或 diagnostic。

### 15.4 MainAgent

- `/field-design ck_spec 怎么填？`：读取相关 resource，回答用法，不执行 skill node。
- `/field-design 用这个材料表做间比法设计`：soft binding execute，replan 到 `skill.field_design`。
- 普通自然语言“帮我用这个材料表生成间比法田间设计”：规划显式 skill node，无需 slash。
- finalizer 不再次触发 Skill，不读取无关 resource。

### 15.5 Schema selection

- RCBD 输入选 `field-design.rcbd`。
- Diagonal 输入选 `field-design.diagonal`。
- Interval 输入选 `field-design.interval`。
- “帮我做田间试验设计” ambiguous：缺 `design`，不执行 entrypoint。
- resume 恢复 selected schema，不重新猜。

### 15.6 ResourceService

- 读取 `.md/.txt/.yaml/.json` 资源成功。
- 读取 `scripts/run_field_design.py` 作为 main_agent audience 被拒。
- 读取 `runtime/sql_query_skill/platform_handler.py` 作为 main_agent audience 被拒。
- 读取 `config.yaml` 被拒。
- `../` / symlink 越界被拒。
- 大文件裁剪并标记 `truncated=true`。
- secret/token/password 脱敏。
- 每次读取记录 `skill.resource_read`。

### 15.7 SkillExecutor

- 新格式 python_subprocess Skill 执行成功。
- 新格式 platform_service Skill 执行成功。
- ambiguous/input invalid 不执行 entrypoint。
- output contract 缺 required key fail closed。
- output file extension 不允许时拒绝 artifact。
- `requires_finalizer` 添加 finalizer node。
- 执行不依赖 `auto_run`。

## 16. 文档与迁移要求

必须更新：

- `Skill构建指南.md`
- 项目级 `skill/*/SKILL.md`
- 每个项目级 Skill 新增 `skill.contract.yaml`
- 每个项目级 Skill 新增 `schemas/*.input.yaml`
- 必要的 `references/*.md`
- API 文档 Skill 调用说明
- `CHANGELOG.md`

迁移后的文档必须明确：

- 新 `SKILL.md` frontmatter 只保留 `name/description`。
- 新 Skill 不支持 `auto_run`。
- 参数 schema 必须在 `schemas/*.input.yaml`。
- 执行契约必须在 `skill.contract.yaml`。
- 主代理按需读取资源必须走 `SkillResourceService`。
- 旧格式仅兼容，不作为新写法。

## 17. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 新旧 Skill 双轨导致行为不一致 | 新 contract 优先；legacy adapter 明确隔离；新格式禁止 `auto_run`。 |
| 主代理资源读取泄漏内部实现 | audience policy、硬黑名单、bundle 边界、脱敏、audit。 |
| schema selector 误选方法 | deterministic 优先、LLM 低置信追问、ambiguous 不执行。 |
| 参数 schema 过度复杂 | 通用字段能力有限集；复杂业务校验留给 entrypoint，但 schema 层保留基础 validation。 |
| 自然语言触发能力下降 | planner/router/replanner 必须消费 public capability profile；保留 match_skills 作为候选召回但不得执行。 |
| checkpoint/time travel 状态不足 | schema selection/resource read/slot/output validation 全部事件化。 |

## 18. 已确认决策

- 采用分层方案：`SKILL.md` + `skill.contract.yaml` + `schemas/*.input.yaml` + `references/*`。
- 参数 schema 保留机器可读 YAML/JSON，不放在 `SKILL.md` frontmatter。
- `required` 作用域为 selected input schema。
- 补参问题由 LLM 生成；schema/resource 提供业务约束、示例和 fallback 材料。
- 主代理按需读取资源走独立 `SkillResourceService`，不放在 `SkillExecutor` 内。
- Skill bundle 内资源读取不逐文件白名单，采用默认可读 + 黑名单 + 硬安全规则。
- 新格式移除 `auto_run`；所有 Skill 执行必须是显式 `skill.*` node。
- 用户不必须 slash；普通自然语言仍可触发 Skill，但要进入显式节点。

## 19. Stop condition

本 PRD 的实施完成条件：

1. 文档和代码均采用新 Skill contract 结构。
2. field-design、field-analysis、rice-genie、OCR、SQLQuery 至少完成目标形态迁移并通过测试矩阵。
3. 新格式 Skill 不依赖旧 `SKILL.md` frontmatter execution/parameters/scripts/outputs。
4. main-agent 不再对新格式 Skill 执行隐式 `auto_run`。
5. 所有新格式 Skill 执行、补槽、资源读取、输出校验都有可恢复/可审计事件证据。
