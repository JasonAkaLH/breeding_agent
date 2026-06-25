# Skill 运行闭环 Workbench 目录总纲 PRD

- **编号**：后端 PRD 22 Umbrella
- **日期**：2026-06-25
- **状态**：从 `docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md` 拆分；按 runtime replan 主线待实施
- **父兼容入口**：`docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md`
- **目标模块**：`src/orchestration/`、`src/capabilities/`、`src/api/runtime.py`、`src/api/routes/tasks.py`、`src/integrations/agent_skills/`

## 1. 目录目标

本目录把父 PRD 的平台层 Skill 运行闭环 Workbench 拆成可独立实施的阶段。目标不是重写父 PRD，而是让每个阶段都有清晰的开发边界、验收标准和测试入口。

总体目标保持不变：在每一版无环 `WorkflowPlan` 外层增加受控 Observe -> Verify -> Replan 机制，根据 Skill contract、运行策略、输入 / 输出契约和安全边界，由 deterministic `WorkbenchRuntimeReplanner` 自动追加内部 Workbench 节点和必要 finalizer，形成可验证的短 digest，再供最终回答或平台诊断使用。

## 2. 跨阶段不变量

| 不变量 | 要求 |
| --- | --- |
| DAG 不变 | 不把 `WorkflowPlan` 改成循环图、LangGraph 式 cyclic state graph 或新的 public plan schema。 |
| 内部能力 | `workbench.*` 必须注册为 `public=False`、`kind="workbench"`、`source="builtin"`，不得进入 public capability list 或 planner prompt。 |
| Runtime 主线 | Workbench 节点由 deterministic runtime replanner 追加；不让 LLM 规划 Workbench，不采用固定 DAG 作为主路径。 |
| 策略来源 | 启用规则只能来自 execution mode、answer mode、input schema、output contract、artifact policy、resource policy、quality policy 等通用属性。 |
| 预算与停止 | runtime replan 预算必须在 initial plan 阶段确定；revised plan 不得提高 `max_replans` 或 `max_dynamic_nodes`；loop 必须通过 terminal / wait state、stage 单调推进、progress marker、pending node gate、failure reason 去重和 input fingerprint 停止重复 replan；用户输入缺失复用已有 interrupt / resume。 |
| digest | Workbench output 必须短、结构化、脱敏；进入 finalizer 前再次经过 Workbench 专用 allowlist 和敏感字段过滤。 |
| answer mode | `direct` 不重复 finalizer；`requires_finalizer` 可消费 digest；`none` 只有显式策略要求时才新增 finalizer。 |
| 可见性 | 前端事件、graph API、history artifact、prompt 不能泄漏内部 capability、stage、handler、runtime、路径、SQL、schema DDL、storage ref、secret。 |
| Skill 边界 | Workbench 只做平台画像、校验、摘要和验证，不承载具体 Skill 业务算法；Skill output 仍走原有输出链路。 |

## 3. 术语

| 术语 | 含义 |
| --- | --- |
| Workbench stage | 平台内部验证阶段，例如 `schema_match`、`artifact_inspect`、`report_verify`。 |
| Workbench digest | 可进入审计或 finalizer 的短结构化安全摘要，不包含原始文件、完整 rows、路径和 secret。 |
| runtime loop | Skill 或 Workbench 节点完成后，由 deterministic replanner 追加下一批内部节点的主线形态。 |
| metadata-only preflight | 只检查 contract / schema / artifact metadata / policy 的前置检查，不验证最终 resolved inputs。 |
| opaque node id | 不包含 `workbench.*` 或真实 stage 名称的内部节点 ID，例如 `{task}:internal_validation:{index}`。 |

## 4. 目标 Workbench capability

目标 capability 继承父 PRD 列表：

| Capability | 作用 | 主要阶段 |
| --- | --- | --- |
| `workbench.data_profile` | 对输入、artifact metadata 或上游 output 摘要做轻量画像。 | 阶段一 / 二 |
| `workbench.schema_match` | 判断输入或输出是否匹配 selected input schema / output contract。 | 阶段一 / 二 |
| `workbench.preflight_validate` | 按通用 quality policy 做 metadata-only 执行前可用性检查。 | 阶段一 / 二 |
| `workbench.domain_validate` | 按 contract 暴露的 domain / quality policy 做边界校验。 | 阶段一 / 二 / 四 |
| `workbench.artifact_inspect` | 检查 Skill 产物是否存在、类型是否符合 contract、是否可下载。 | 阶段一 / 二 |
| `workbench.report_verify` | 验证报告或最终 digest 是否覆盖 contract 关键事实和风险。 | 阶段一 / 二 / 四 |

## 5. 阶段依赖

```text
01 Policy + budget + runtime state
  -> 02 capability + executor
  -> 03 deterministic runtime loop + finalizer digest
  -> 04 frontend/API/prompt 脱敏门禁
  -> 05 contract quality_workbench + diagnostics
```

阶段零和阶段一建立模型、注册和执行器，不追加 Workbench nodes。阶段二开始通过 runtime replan 改变 task graph。阶段三是生产部署前的安全门禁。阶段四依赖前面阶段的 metadata、policy、output contract 和 diagnostics。

## 6. 总体验收矩阵

| 类别 | 总体验收 |
| --- | --- |
| Capability 可见性 | `workbench.*` 不出现在 `/api/v1/capabilities` public 列表、planner prompt、LLM runtime replanner prompt。 |
| 策略 | policy 命中不按 Skill 名称分支；普通 Skill 默认不开 Workbench。 |
| Runtime DAG | runtime loop 追加节点后仍通过 `WorkflowPlanValidator` 的 capability、payload、依赖和无环校验。 |
| No mutation | revised plan 不改写已存在 node capability / dependencies。 |
| Answer mode | `direct` 不新增重复 finalizer；`requires_finalizer` digest 能进入 finalizer；`none` 只在显式策略下新增 finalizer。 |
| Digest | required 字段完整；禁止字段失败或剔除；进入 prompt 的字段只包含 safe digest。 |
| API / SSE | `node.started`、`task.graph_updated`、task graph response、history artifact 不暴露内部 capability id、stage 或敏感实现字段。 |
| Runtime budget / stop | 追加节点受 initial `max_replans/max_dynamic_nodes` 限制；预算耗尽或无法语义推进时进入 terminal state 并记录审计；用户输入缺失或 pending 节点进入 wait state，不追加重复节点。 |
| Interrupt / resume | resume 后不会重复执行已完成 Workbench stage。 |
| Artifact | Workbench 不创建前端可展示 artifact；Skill output file 展示与下载链路保持不变。 |
| Health | 非法 contract workbench stage 产生诊断，不破坏内置 capability 注册。 |

## 7. 部署口径

Workbench 先在开发环境完成 runtime loop、脱敏和测试验证。生产环境必须在以下证据齐备后再部署：

- consumer contract tests 通过；
- graph / event / prompt 泄漏回归通过；
- direct / requires_finalizer / none 三种 answer mode 回归通过；
- interrupt / resume 和 artifact 下载回归不退化；
- 至少一个真实 Skill smoke 验证 Workbench digest 对最终回答或诊断有正向价值。
