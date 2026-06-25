# Skill 运行闭环 Workbench 分步 PRD 索引

- **日期**：2026-06-25
- **状态**：已从总纲拆分，待实施
- **父兼容入口**：`docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md`
- **关联 PRD**：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`、`docs/prd/backend/15-SkillExecutor实现需求PRD.md`、`docs/prd/backend/skill-contract-progressive-disclosure/README.md`、`docs/prd/backend/prompt-envelope/README.md`
- **总目标**：在不改变 public plan schema、API/SSE schema 和 Skill 业务归属的前提下，为 `skill.*` 执行链增加后端内部 Observe -> Verify -> Replan 闭环、短 digest、受控预算和安全脱敏。

## 目录级置信标准

本目录是一组可逐阶段实施、验证和回滚的 PRD。每个阶段必须同时满足：

1. **DAG 不变量**：每一版 `WorkflowPlan` 仍是无环 DAG；运行闭环通过新 DAG 版本、预算和 replan decision 表达，不引入 cyclic graph。
2. **Public/Internal 分层**：`workbench.*` 必须是 `public=False` 的内部 capability；Planner、用户、public capability API 和 LLM replanner 不得看到或生成它。
3. **Contract / Policy 驱动**：Workbench 启用和 stage 选择必须来自平台策略与 Skill contract 的通用字段，不得按具体 Skill 名称定制。
4. **Answer mode 守恒**：`answer_mode=direct` 默认不得追加第二个 `main_agent.respond`；`requires_finalizer` 只消费安全 digest；`none` 只有显式策略要求时才新增 finalizer。
5. **Digest 安全**：Workbench output 必须短、结构化、脱敏，并禁止 raw rows、完整文件内容、路径、storage ref、SQL、schema DDL、handler、runtime、entrypoint、token、secret 等字段。
6. **预算受控**：普通 Skill 默认保持一次性行为；runtime replan 预算只能在 initial plan 阶段确定，后续 revised plan 不得提升预算。
7. **事件和 graph 不泄漏**：SSE、task graph API、history artifact、prompt dependency context 不得暴露 `workbench.*` capability id 或内部实现细节。
8. **测试先行**：每个阶段先补平台 consumer contract tests，再实现生产路径；具体 Skill 的业务测试仍由 Skill 维护者负责。
9. **可灰度回滚**：所有行为必须受 `workbench.rollout_scope` 控制，关闭后回到现有 Skill 一次性执行链。

## 拆分原则

1. 父兼容入口保留完整背景、总体设计、跨阶段风险和总体验收矩阵；本目录负责把实施拆成可计划、开发、验收、回滚的阶段 PRD。
2. 阶段 PRD 默认继承总纲的不变量、禁止字段、安全边界、answer mode 规则、预算规则和脱敏规则；不得放宽总纲约束。
3. 先做 audit-only 和数据契约，再实现 executor；先固定 DAG，再做 runtime replanner；先后端内部健康诊断，后 contract 可选字段。
4. 不新增前端页面、前端 API、artifact 下载协议、interrupt/resume 协议或 SSE event schema。

## 阶段文件

| 阶段 | PRD | 目标 | 实施优先级 |
| --- | --- | --- | --- |
| 总纲 | `00-Skill运行闭环Workbench总纲PRD.md` | 目录内总纲摘录：统一不变量、术语、阶段依赖、总体验收矩阵与禁止项。 | Umbrella |
| 阶段零 | `01-阶段零-Workbench基座Policy与AuditOnlyPRD.md` | 建立 `WorkbenchPolicy`、`WorkbenchStage`、`WorkbenchOutputContractV1`、feature flag 和 audit-only policy decision；不改 DAG。 | P0 |
| 阶段一 | `02-阶段一-内部Capability与ExecutorPRD.md` | 注册 `workbench.*` 内部 capability，提供本地 executor、输出契约校验、敏感字段剔除和基础 consumer contract tests。 | P1 |
| 阶段二 | `03-阶段二-固定DAG插入与FinalizerDigestPRD.md` | 在 Skill plan 初始展开时插入固定 Workbench DAG，处理 finalizer 依赖和 answer mode。 | P2 |
| 阶段三 | `04-阶段三-事件GraphPrompt脱敏PRD.md` | 收口 SSE、`task.graph_updated`、task graph API、history artifact 与 finalizer prompt dependency context 的内部节点脱敏。 | P2 / Gate |
| 阶段四 | `05-阶段四-RuntimeReplanner与Contract健康诊断PRD.md` | 引入 deterministic `WorkbenchRuntimeReplanner`、预算闭环、`quality_workbench` contract 可选字段和 health diagnostics。 | P3 |

## 推荐阶段执行顺序

```text
阶段零 Policy / output contract / audit-only
  -> 阶段一 internal capability / executor / output validation
  -> 阶段二 fixed DAG / finalizer digest / answer mode
  -> 阶段三 event + graph + prompt 脱敏发布门禁
  -> 阶段四 runtime replan / contract quality_workbench / diagnostics
```

阶段三是 `fixed_dag` 放量前的安全门禁：如果事件、graph 或 prompt 脱敏无法通过，阶段二只能停留在本地测试或 audit-only，不得面向普通前端视图放量。

## 总体验证命令入口

各阶段 PRD 给出更细命令。完整 release gate 至少覆盖：

```bash
python -m pytest tests/orchestration/
python -m pytest tests/capabilities/workbench/
python -m pytest tests/api/test_route_contract.py
python -m pytest tests/api/ -k "task or graph or event or capability"
```

若本阶段触及 prompt dependency context 或前端展示语义，还应补充对应前端 / API 文档回归；若只新增文档，则以链接、目录和索引检查为验收。
