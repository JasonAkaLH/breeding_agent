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
- 旧 main-agent auto-run 生产路径的删除验收归属阶段七。

## 2. 现有代码锚点

| 锚点 | 当前事实 | 本阶段约束 |
| --- | --- | --- |
| `src/capabilities/main_agent/executor.py` | 当前存在 `_resolve_skill_matches()` 与 `_run_auto_scripts()` 隐式执行路径。 | v2 Skill 命中后只能回答用法或发出 execute/replan signal，不能在 main_agent 内执行 entrypoint；旧 auto-run 不能用于 v2。 |
| `src/orchestration/soft_skill_replanner.py` | slash soft binding 已能将 execute signal replan 到 `skill.*`。 | v2 slash 与自然语言都要复用显式 `skill.*` 节点路径。 |
| `src/orchestration/skill_workflow_provider.py` | `skill.*` capability 可展开 Skill node + finalizer。 | public capability context 必须来自 contract registry，finalizer 禁止再次触发 Skill matching。 |

## 3. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C4-001 | public profile 不暴露内部实现。 | 无 script path、handler module、services、config、regex 全文。 |
| C4-002 | public profile 包含 resource index。 | 主代理可知道何时读取 usage/material/interval resource。 |
| C4-003 | public profile 包含 schema summaries。 | 显示 RCBD/Diagonal/Interval 摘要但不含 schema 原文。 |
| C4-004 | 用法问题按需读取 resource 并回答。 | `/field-design ck_spec 怎么填？` 不执行 skill node。 |
| C4-005 | 执行请求 replan 到 `skill.*`。 | `/field-design 用这个材料表做间比法设计` 生成 execute signal。 |
| C4-006 | 普通自然语言可触发 Skill。 | planner/replanner 规划 `skill.field_design`，无需 slash。 |
| C4-007 | finalizer 不再触发 Skill。 | finalizer metadata 禁止 Skill invocation/matching/resource unrelated reads。 |
| C4-008 | v2 Skill 不走 main-agent auto-run。 | main_agent 对 contract Skill 不调用 entrypoint。 |
| C4-009 | 明确区分自然语言规划与旧 auto-run。 | 普通自然语言可以规划 `skill.*` node；但规划前后都不能在 `main_agent.respond` 内部执行脚本。 |
| C4-010 | resource answer 分支可审计。 | 用法类问题读取 public resource 时产生 `skill.resource_read` audit event，最终回答不包含内部路径。 |

## 4. 测试计划

- `tests/api/test_soft_skill_binding.py`
- `tests/integrations/agent_skills/test_public_skill_profile.py`
- `tests/orchestration/test_llm_workflow_provider.py`
- `tests/api/test_capabilities_list.py`

## 5. 完成门禁

- soft binding answer/execute 分支均有回归。
- public profile 安全扫描通过。
- 普通自然语言规划 skill node 的测试通过。
- 无 contract Skill 不出现在 public profile、slash soft binding 候选或 planner public capability context 中。
- v2 Skill 的 answer/execute/finalizer 三分支均有负向测试，确认没有内部脚本执行。
