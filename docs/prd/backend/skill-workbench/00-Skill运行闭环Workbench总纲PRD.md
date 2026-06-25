# Skill 运行闭环 Workbench 目录总纲 PRD

- **编号**：后端 PRD 22 Umbrella
- **日期**：2026-06-25
- **状态**：从 `docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md` 拆分；待实施
- **父兼容入口**：`docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md`
- **目标模块**：`src/orchestration/`、`src/capabilities/`、`src/api/runtime.py`、`src/api/routes/tasks.py`、`src/integrations/agent_skills/`

## 1. 目录目标

本目录把父 PRD 的平台层 Skill 运行闭环 Workbench 拆成可独立实施的阶段。目标不是重写父 PRD，而是让每个阶段都有清晰的开发边界、验收标准、回滚策略和测试入口。

总体目标保持不变：在每一版无环 `WorkflowPlan` 外层增加受控 Observe -> Verify -> Replan 机制，根据 Skill contract、运行策略、输入 / 输出契约和安全边界自动插入或追加内部 Workbench 节点，形成可验证的短 digest，再供最终回答或平台诊断使用。

## 2. 跨阶段不变量

| 不变量 | 要求 |
| --- | --- |
| DAG 不变 | 不把 `WorkflowPlan` 改成循环图、LangGraph 式 cyclic state graph 或新的 public plan schema。 |
| 内部能力 | `workbench.*` 必须注册为 `public=False`、`kind="workbench"`、`source="builtin"`，不得进入 public capability list 或 planner prompt。 |
| 策略来源 | 启用规则只能来自 execution mode、answer mode、input schema、output contract、artifact policy、resource policy、quality policy 等通用属性。 |
| 预算 | runtime replan 预算必须在 initial plan 阶段确定；revised plan 不得提高 `max_replans` 或 `max_dynamic_nodes`。 |
| digest | Workbench output 必须短、结构化、脱敏；进入 finalizer 前再次经过 allowlist 和敏感字段过滤。 |
| answer mode | `direct` 不重复 finalizer；`requires_finalizer` 可消费 digest；`none` 只有显式策略要求时才新增 finalizer。 |
| 可见性 | 前端事件、graph API、history artifact、prompt 不能泄漏内部 capability、handler、runtime、路径、SQL、schema DDL、storage ref、secret。 |
| Skill 边界 | Workbench 只做平台画像、校验、摘要和验证，不承载具体 Skill 业务算法。 |

## 3. 术语

| 术语 | 含义 |
| --- | --- |
| Workbench stage | 平台内部验证阶段，例如 `schema_match`、`artifact_inspect`、`report_verify`。 |
| Workbench digest | 可进入审计或 finalizer 的短结构化安全摘要，不包含原始文件、完整 rows、路径和 secret。 |
| fixed DAG | 初始 plan 展开时一次性插入 Workbench node 的 MVP 形态。 |
| runtime loop | Skill 或 Workbench 节点完成后，由 deterministic replanner 追加下一批内部节点的 Phase 4 形态。 |
| audit-only | 只记录策略命中和 would-run stages，不改变 DAG、不执行 Workbench node。 |

## 4. 目标 Workbench capability

目标 capability 继承父 PRD 列表：

| Capability | 作用 | 主要阶段 |
| --- | --- | --- |
| `workbench.data_profile` | 对输入、artifact metadata 或上游 output 摘要做轻量画像。 | 阶段一 / 二 |
| `workbench.schema_match` | 判断输入或输出是否匹配 selected input schema / output contract。 | 阶段一 / 二 |
| `workbench.preflight_validate` | 按通用 quality policy 做执行前可用性检查。 | 阶段一 / 二 |
| `workbench.domain_validate` | 按 contract 暴露的 domain / quality policy 做边界校验。 | 阶段一 / 四 |
| `workbench.artifact_inspect` | 检查 Skill 产物是否存在、类型是否符合 contract、是否可下载。 | 阶段一 / 二 |
| `workbench.report_verify` | 验证报告或最终 digest 是否覆盖 contract 关键事实和风险。 | 阶段一 / 二 / 四 |

## 5. 阶段依赖

```text
01 Policy + audit-only
  -> 02 capability + executor
  -> 03 fixed DAG + finalizer digest
  -> 04 frontend/API/prompt 脱敏门禁
  -> 05 deterministic runtime loop + contract diagnostics
```

阶段零和阶段一可以在 feature flag 关闭或 audit-only 下完成，不影响现有 Skill 执行。阶段二开始会改变 DAG，必须依赖阶段三定义的脱敏门禁才能放量到普通前端视图。阶段四依赖阶段二的 metadata、policy 和 output contract。

## 6. 总体验收矩阵

| 类别 | 总体验收 |
| --- | --- |
| Capability 可见性 | `workbench.*` 不出现在 `/api/v1/capabilities` public 列表、planner prompt、LLM runtime replanner prompt。 |
| 策略 | policy 命中不按 Skill 名称分支；普通 Skill 默认不开 Workbench。 |
| DAG | fixed DAG 插入后仍通过 `WorkflowPlanValidator` 的 capability、payload、依赖和无环校验。 |
| Answer mode | `direct` 不新增重复 finalizer；`requires_finalizer` digest 能进入既有 finalizer；`none` 只在显式策略下新增 finalizer。 |
| Digest | required 字段完整；禁止字段失败或剔除；进入 prompt 的字段只包含 safe digest。 |
| API / SSE | `node.started`、`task.graph_updated`、task graph response、history artifact 不暴露内部 capability id 或敏感实现字段。 |
| Runtime budget | Phase 4 追加节点受 initial `max_replans/max_dynamic_nodes` 限制；预算耗尽 fail closed 并记录审计。 |
| Interrupt / resume | resume 后不会重复执行已完成 Workbench stage。 |
| Artifact | Workbench 不创建前端可展示 artifact；Skill output file 展示与下载链路保持不变。 |
| Health | 非法 contract workbench stage 产生诊断，不破坏内置 capability 注册。 |

## 7. 回滚口径

Workbench 必须受配置控制：

```yaml
workbench:
  enabled: false
  rollout_scope: disabled | audit_only | fixed_dag | runtime_replan
```

- `disabled`：回到现有 Skill 一次性执行链。
- `audit_only`：只记录 policy decision 和 would-run stages，不改 DAG。
- `fixed_dag`：只执行初始插入的 Workbench DAG。
- `runtime_replan`：允许 deterministic Workbench replanner 追加内部节点。

任何阶段若发现内部节点泄漏、prompt 泄漏或重复 finalizer，必须先回滚到 `audit_only` 或 `disabled`，再修复。
