# 阶段一：Skill Contract 解析与 Legacy Adapter PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **目标模块**：`src/integrations/agent_skills/` parser/catalog/runtime state、capability registry、legacy manifest adapter
- **目标结果**：新增 `skill.contract.yaml` 解析、校验、bundle 归档和 capability 注册事实源；未迁移 Skill 继续通过 legacy adapter 工作；本阶段不改变 Skill 执行路径。

## 1. 范围

### 1.1 In scope

- 新增 `SkillContract` 数据模型和 parser。
- 在 Skill bundle 加载时发现并解析同目录 `skill.contract.yaml`。
- 将 contract capability 注册进 `SkillCapabilityRegistry`。
- 建立 `LegacySkillContractAdapter`，把旧 `SKILL.md` frontmatter 映射为 contract-like 对象供兼容路径使用。
- 为 active bundle 保存 contract registry、diagnostics、bundle revision/fingerprint。
- 新 contract 中出现 `auto_run` / `run_by_default` 必须 fail closed。

### 1.2 Out of scope

- 不实现 input schema 解析。
- 不实现 ResourceService。
- 不改 main-agent soft binding prompt。
- 不改 SkillExecutor 执行流程。
- 不迁移任何项目级 Skill。

## 2. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C1-001 | 解析合法 `skill.contract.yaml` 为 `SkillContract`。 | 单测覆盖 python_subprocess 与 platform_service 两类 contract。 |
| C1-002 | contract capability 是 public Skill 注册事实源。 | `/api/v1/capabilities` 或 registry test 中 descriptor id/display_name 来自 contract。 |
| C1-003 | 缺必填字段 fail closed。 | 缺 `contract_version`、`capability.id`、`capability.display_name`、entrypoint/output 引用时报 diagnostic。 |
| C1-004 | capability id 必须以 `skill.` 开头且不冲突。 | 非法 id、reserved id、重复 id 均不注册。 |
| C1-005 | 新 contract 禁止 `auto_run` / `run_by_default`。 | parser 发现后拒绝该 Skill。 |
| C1-006 | 新旧执行契约不得混用。 | 同一 bundle 同时存在 contract 与旧 execution/parameters/scripts 时，contract 优先并记录 diagnostic；冲突字段不参与执行事实源。 |
| C1-007 | legacy adapter 保持旧 Skill 可注册。 | 未迁移 Skill 的现有 registry tests 继续通过。 |
| C1-008 | bundle revision 包含 contract 文件指纹。 | 修改 `skill.contract.yaml` 能触发 runtime refresh revision 变化。 |

## 3. 数据模型

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

## 4. 失败模式

- YAML 不是 mapping：fail closed。
- contract path 越界：fail closed。
- input schema ref path 越界：fail closed，但 schema 内容解析留到阶段二。
- platform_service 缺 handler：fail closed。
- python_subprocess 带 services：fail closed。
- output_contract 引用不存在：fail closed。

## 5. 测试计划

- `tests/integrations/agent_skills/test_skill_contract_parser.py`
- `tests/integrations/agent_skills/test_skill_capabilities.py`
- `tests/integrations/agent_skills/test_skill_runtime_state.py`
- `tests/api/test_capabilities.py` 或现有 capabilities API 回归

测试必须覆盖：合法 contract、非法 contract、legacy fallback、新旧冲突、reserved id、duplicate id、refresh fingerprint。

## 6. 完成门禁

- 所有 contract parser / registry / runtime state 测试通过。
- 现有未迁移 Skill 不因本阶段失败。
- 无生产执行行为变化；SkillExecutor 仍可通过 legacy adapter 工作。
- CHANGELOG 记录 License Requirement；本阶段无依赖变更。
