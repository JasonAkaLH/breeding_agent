# 阶段一：Skill Contract 解析与注册 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **目标模块**：`src/integrations/agent_skills/` parser/catalog/runtime state、capability registry、contract diagnostics
- **目标结果**：新增 `skill.contract.yaml` 解析、校验、bundle 归档和 capability 注册事实源；系统只注册 v2 Skill，没有 contract 的 Skill 不注册、不执行、不进入 capability 列表。

## 1. 范围

### 1.1 In scope

- 新增 `SkillContract` 数据模型和 parser。
- 在 Skill bundle 加载时发现并解析同目录 `skill.contract.yaml`。
- 将 contract capability 注册进 `SkillCapabilityRegistry`。
- 为 active bundle 保存 contract registry、diagnostics、bundle revision/fingerprint。
- 没有 `skill.contract.yaml` 的 Skill 记录 diagnostic 并跳过注册。
- v2 Skill 中出现 `auto_run`、`run_by_default`、顶层 `parameters`、`scripts`、`execution`、`public_usage` 等 v1 平台字段必须 fail closed。

### 1.2 Out of scope

- 不实现 input schema 解析。
- 不实现 ResourceService。
- 不改 main-agent soft binding prompt。
- 不改 SkillExecutor 执行流程。
- 不迁移任何项目级 Skill。
- 不提供 v1 manifest adapter 或 fallback。

## 2. 现有代码锚点

| 锚点 | 当前事实 | 本阶段约束 |
| --- | --- | --- |
| `src/integrations/agent_skills/parser.py` | 当前只解析 `SKILL.md` frontmatter，并把未知字段放入 `metadata`。 | v2 contract parser 必须独立读取 `skill.contract.yaml`；旧平台字段不得进入 v2 执行事实源。 |
| `src/integrations/agent_skills/catalog.py` | 当前从 roots 查找 `SKILL.md` 并跳过单个解析失败 Skill。 | contract 缺失或解析失败只能影响当前 Skill，不能阻断整个 catalog。 |
| `src/integrations/agent_skills/skill_capabilities.py` | capability id 可来自旧 metadata 或 skill name 派生。 | v2 capability id 只能来自 contract；不得从旧 metadata 或 name 派生公开 Skill capability。 |
| `src/integrations/agent_skills/skill_runtime_state.py` | `SkillRuntimeBundle` 保存 catalog、capability registry、fingerprint 与 revision。 | bundle 必须保存 contract registry/diagnostics；fingerprint 已覆盖 Skill 目录文件，contract/schema/resource 变更必须影响 revision。 |

## 3. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C1-001 | 解析合法 `skill.contract.yaml` 为 `SkillContract`。 | 单测覆盖 python_subprocess 与 platform_service 两类 contract。 |
| C1-002 | contract capability 是 public Skill 注册事实源。 | `/api/v1/capabilities` 和 registry test 中 descriptor id/display_name 来自 contract。 |
| C1-003 | 缺必填字段 fail closed。 | 缺 `contract_version`、`capability.id`、`capability.display_name`、entrypoint/output 引用时报 diagnostic 且不注册。 |
| C1-004 | capability id 必须以 `skill.` 开头且不冲突。 | 非法 id、reserved id、重复 id 均不注册。 |
| C1-005 | v1 执行字段 fail closed。 | v2 Skill 出现 `auto_run`、`run_by_default`、顶层 `parameters`、`scripts`、`execution`、`public_usage` 任一平台字段时拒绝注册。 |
| C1-006 | 无 contract Skill 不注册。 | 只有 `SKILL.md`、没有 `skill.contract.yaml` 的 Skill 产生 diagnostic，不进入 capability 列表。 |
| C1-007 | bundle revision 包含 contract 文件指纹。 | 修改 `skill.contract.yaml` 能触发 runtime refresh revision 变化。 |
| C1-008 | contract 解析失败的隔离粒度是单个 Skill。 | 一个坏 contract 只产生 diagnostic 并跳过该 Skill，不影响其他 Skill 注册。 |

## 4. 数据模型

新增：

```text
SkillContract
SkillCapabilityContract
SkillRoutingContract
SkillRuntimeContract
SkillEntrypointContract
SkillInputSchemaRef
SkillSchemaSelectorContract
SkillOutputContract
SkillResourcePolicy
SkillContractDiagnostic
```

`SkillRuntimeBundle` 增加：

```text
contracts_by_skill_name
contract_by_capability_id
contract_diagnostics
```

## 5. 失败模式

- YAML 不是 mapping：fail closed。
- `skill.contract.yaml` 缺失：跳过该 Skill 注册，记录 `contract_missing` diagnostic。
- v1 平台字段出现在 v2 Skill：fail closed，记录 `v1_field_forbidden` diagnostic。
- contract path 越界：fail closed。
- input schema ref path 越界：fail closed，但 schema 内容解析留到阶段二。
- platform_service 缺 handler：fail closed。
- python_subprocess 带 services：fail closed。
- output_contract 引用不存在：fail closed。

## 6. 测试计划

- `tests/integrations/agent_skills/test_skill_contract_parser.py`
- `tests/integrations/agent_skills/test_skill_capabilities.py`
- `tests/integrations/agent_skills/test_skill_runtime_state.py`
- `tests/api/test_capabilities_list.py`
- `tests/api/test_skill_capability_pool.py`

测试必须覆盖：合法 contract、非法 contract、contract missing、v1 forbidden fields、reserved id、duplicate id、refresh fingerprint、坏 contract 隔离。

## 7. 完成门禁

- 所有 contract parser / registry / runtime state 测试通过。
- 无 contract Skill 不注册的负向测试通过。
- v1 平台字段 fail-closed 测试通过。
- 坏 contract 隔离测试通过，确认 catalog 仍加载其他合法 v2 Skill。
- 无生产执行行为变化；本阶段只改变注册事实源，不执行 Skill。
- CHANGELOG 记录 License Requirement；本阶段无依赖变更。
