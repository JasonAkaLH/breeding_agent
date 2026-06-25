# 阶段四：Runtime Replanner 与 Contract 健康诊断 PRD

- **编号**：后端 PRD 22-Phase 4
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段零至阶段三全部完成
- **目标模块**：`src/orchestration/workbench_replanner.py`、`src/orchestration/runtime_replanner.py`、`src/orchestration/service.py`、`src/integrations/agent_skills/skill_capabilities.py`、Skill runtime diagnostics、`tests/orchestration/`

## 1. 阶段目标

在 fixed DAG 稳定后，引入真正的确定性运行闭环和 contract 健康诊断：

1. 新增 `WorkbenchRuntimeReplanner`，放在 LLM `MainAgentRuntimeReplanner` 前。
2. 根据已完成 `skill.*` 或 `workbench.*` output、policy 和已执行 stage 追加下一批内部节点。
3. 严格遵守 initial plan 的 `max_replans` / `max_dynamic_nodes`，预算不足 fail closed 并记录审计。
4. 不改写已存在 node capability 或 dependencies；只新增 Workbench nodes、必要 finalizer，或 orphan pending finalizer。
5. Skill contract 可选增加 `quality_workbench`，并提供 health / diagnostics。

## 2. 范围

### In scope

- `WorkbenchReplanState`、`WorkbenchStageDecision`、`WorkbenchBudget`。
- `CompositeRuntimeReplanner` 顺序调整：Workbench deterministic replanner 先于 LLM replanner。
- 根据 `satisfaction.replan_recommended`、missing output、artifact policy、domain policy 追加 stage。
- pending finalizer 处理策略：不得原地修改已存在依赖；必要时新增 finalizer 或 orphan pending finalizer。
- contract 可选字段 `quality_workbench` 解析和诊断。
- capability health payload 的 internal / audit-only 诊断。

### Out of scope

- 不让 LLM replanner 规划 `workbench.*`。
- 不把 diagnostics 变成新的 public API，除非后续 PRD 明确要求。
- 不强制所有 Skill 立即声明 `quality_workbench`。
- 不把 Workbench 变成具体业务质量算法。

## 3. Runtime replanner 规则

`WorkbenchRuntimeReplanner` 必须：

1. 只读取 `RuntimeReplanContext` 中的安全 output。
2. 只追加 `workbench.*` 或必要 `main_agent.respond` finalizer。
3. 不调用 LLM。
4. 不改写已存在节点的 capability / dependencies。
5. 不提高 initial plan 预算。
6. 对已执行 stage 去重，resume 后不重复执行。
7. 预算耗尽时 fail closed，记录审计，并按 answer mode 决定是否允许继续 finalizer。

## 4. Budget 规则

| 预算 | 要求 |
| --- | --- |
| `max_replans` | 只能来自 initial plan。Workbench 每次 revised plan 计入预算。 |
| `max_dynamic_nodes` | 所有追加 Workbench / finalizer nodes 都计入动态节点预算。 |
| 预算不足 | 不追加节点；记录 reason code；不得声称验证通过。 |
| 普通 Skill | 未显式允许 runtime loop 时预算保持 0。 |

## 5. Contract 扩展

Phase 4 可选支持：

```yaml
quality_workbench:
  enabled: true
  domain_kind: generic
  stages: [schema_match, artifact_inspect, report_verify]
  finalizer_digest_mode: when_finalizer_exists
  max_replans: 1
  max_dynamic_nodes: 3
```

规则：

1. Contract 策略优先，平台静态表作为兼容 fallback。
2. 非法 stage、非法预算、非法 finalizer mode 产生 `SkillCapabilityDiagnostic`。
3. 默认不因非法 `quality_workbench` 破坏内置 Workbench capability 注册。
4. 对该 Skill 的行为按策略 fail closed 或降级到 audit-only，由平台策略决定。
5. 文档必须告诉 Skill 维护者如何声明 stage、digest 需求和预算。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| append next stage | Skill output 完成后可追加下一批 Workbench nodes。 |
| no mutation | revised plan 不改写已有 node dependencies / capability。 |
| budget respected | 超过 `max_replans` 或 `max_dynamic_nodes` 时 fail closed 并审计。 |
| replanner ordering | Workbench replanner 在 LLM replanner 前，LLM 仍只能输出 public DAG。 |
| finalizer safety | pending finalizer 不提前消费未验证 Skill output。 |
| resume dedupe | resume 后不会重复执行已完成 Workbench stage。 |
| contract diagnostics | 非法 `quality_workbench` stage / budget 产生诊断。 |
| fallback policy | 无 contract 字段时仍可走平台静态策略 fallback。 |

推荐命令：

```bash
python -m pytest tests/orchestration/test_workbench_replanner.py
python -m pytest tests/orchestration/ -k "runtime_replanner or dynamic_nodes or workbench"
python -m pytest tests/integrations/agent_skills/ -k "contract or capability"
```

## 7. 阶段验收

- runtime loop 只追加内部节点且严格受预算限制。
- LLM replanner 不知道也不能生成 `workbench.*`。
- finalizer 不提前消费未验证 output。
- `quality_workbench` 为 optional，非法配置有 diagnostics，不强制破坏现有 Skill 注册。
- `runtime_replan` rollout_scope 可灰度开启并可回滚到 `fixed_dag` 或 `audit_only`。
