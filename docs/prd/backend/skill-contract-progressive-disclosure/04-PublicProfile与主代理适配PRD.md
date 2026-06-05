# 阶段四：PublicProfile 与主代理适配 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一、阶段二、阶段三
- **目标模块**：public profile builder、main_agent soft binding、planner/replanner public capability context
- **目标结果**：主代理只消费 public profile、resource index 和 schema summaries；用法类问题可按需读取 public resource；执行类请求通过 slash/planner/replanner 进入显式 `skill.*` node，不在 main_agent 内部执行脚本。

## 1. 范围

### 1.1 In scope

- 重构 `build_public_skill_profile()` 以 contract/resource/schema summary 为输入。
- public profile 包含 resource index、schema summaries、routing triggers/examples。
- soft binding answer 分支可调用 `SkillResourceService` 读取 public resource。
- soft binding execute 分支保持 replan 到 `skill.*`。
- planner/replanner public capability context 可看到新 contract 注册的 Skill。
- 新格式 Skill 禁止 main-agent 内部 `_run_auto_scripts` 执行。
- `match_skills` 可保留为候选召回，但不得执行新格式脚本。

### 1.2 Out of scope

- 不实现 SkillExecutor v2。
- 不迁移项目级 Skill 文件。
- 不下线 legacy auto-run。

## 2. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C4-001 | public profile 不暴露内部实现。 | 无 script path、handler module、services、config、regex 全文。 |
| C4-002 | public profile 包含 resource index。 | 主代理可知道何时读取 usage/material/interval resource。 |
| C4-003 | public profile 包含 schema summaries。 | 显示 RCBD/Diagonal/Interval 摘要但不含 schema 原文。 |
| C4-004 | 用法问题按需读取 resource 并回答。 | `/field-design ck_spec 怎么填？` 不执行 skill node。 |
| C4-005 | 执行请求 replan 到 `skill.*`。 | `/field-design 用这个材料表做间比法设计` 生成 execute signal。 |
| C4-006 | 普通自然语言可触发 Skill。 | planner/replanner 规划 `skill.field_design`，无需 slash。 |
| C4-007 | finalizer 不再触发 Skill。 | finalizer metadata 禁止 Skill invocation/matching/resource unrelated reads。 |
| C4-008 | 新格式 Skill 不走 main-agent auto-run。 | main_agent 对新 contract Skill 不调用 entrypoint。 |

## 3. 测试计划

- `tests/capabilities/main_agent/test_soft_skill_binding.py`
- `tests/capabilities/main_agent/test_public_skill_profile.py`
- `tests/orchestration/test_llm_workflow_provider.py`
- `tests/api/test_capabilities.py`

## 4. 完成门禁

- soft binding answer/execute 分支均有回归。
- public profile 安全扫描通过。
- 普通自然语言规划 skill node 的测试通过。
- legacy Skill 行为保持兼容。
