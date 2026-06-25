# Skill 运行闭环 Workbench 分步 PRD 索引

- **日期**：2026-06-25
- **状态**：已从总纲拆分，按最终 runtime replan 方案待实施
- **父兼容入口**：`docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md`
- **关联 PRD**：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`、`docs/prd/backend/15-SkillExecutor实现需求PRD.md`、`docs/prd/backend/skill-contract-progressive-disclosure/README.md`、`docs/prd/backend/prompt-envelope/README.md`
- **总目标**：在不改变 public plan schema、API/SSE schema 和 Skill 业务归属的前提下，让 deterministic `WorkbenchRuntimeReplanner` 观察 `skill.*` 输出并追加后端内部 Workbench 验证节点、必要 finalizer、短 digest、受控预算和安全脱敏。

## 目录级置信标准

本目录是一组可逐阶段实施、验证和回滚的 PRD。每个阶段必须同时满足：

1. **DAG 不变量**：每一版 `WorkflowPlan` 仍是无环 DAG；运行闭环通过新 DAG 版本、预算和 replan decision 表达，不引入 cyclic graph。
2. **Public/Internal 分层**：`workbench.*` 必须是 `public=False` 的内部 capability；Planner、用户、public capability API 和 LLM replanner 不得看到或生成它。
3. **Runtime replan 主线**：Workbench 不采用固定 DAG 过渡方案 主路径；核心方案是 deterministic runtime replanner 在 Skill output 后追加 Workbench nodes / finalizer。
4. **Contract / Policy 驱动**：Workbench 启用和 stage 选择必须来自平台策略与 Skill contract 的通用字段，不得按具体 Skill 名称定制。
5. **Answer mode 守恒**：`answer_mode=direct` 默认不得追加第二个 `main_agent.respond`；`requires_finalizer` 可让 finalizer 消费安全 digest；`none` 只有显式策略要求时才新增 finalizer。
6. **Digest 安全**：Workbench output 必须短、结构化、脱敏，并禁止 raw rows、完整文件内容、路径、storage ref、SQL、schema DDL、handler、runtime、entrypoint、token、secret 等字段。
7. **预算受控与语义停止**：普通 Skill 默认保持现有一次性行为；只有策略允许的 Skill plan 才在 initial plan 阶段写入有限 runtime replan 预算，后续 revised plan 不得提升预算；runtime loop 必须通过 terminal / wait state、stage 单调推进、progress marker、pending node gate 和 input fingerprint 防止重复 replan；用户输入缺失复用已有 interrupt / resume。
8. **事件和 graph 不泄漏**：SSE、task graph API、history artifact、prompt dependency context 不得暴露 `workbench.*` capability id、内部 stage、内部 node id 语义或实现细节。
9. **测试先行**：每个阶段先补平台 consumer contract tests，再实现生产路径；具体 Skill 的业务测试仍由 Skill 维护者负责。

## 拆分原则

1. 父兼容入口保留完整背景、总体设计、跨阶段风险和总体验收矩阵；本目录负责把实施拆成可计划、开发、验收的阶段 PRD。
2. 阶段 PRD 默认继承总纲的不变量、禁止字段、安全边界、answer mode 规则、预算规则和脱敏规则；不得放宽总纲约束。
3. 先建立 policy / budget / state 基座，再实现内部 capability / executor；随后接入 runtime loop 与 finalizer；最后收口事件 / graph / prompt 脱敏和 contract diagnostics。
4. 不新增前端页面、前端 API、artifact 下载协议、interrupt/resume 协议或 SSE event schema。

## 父总纲 Phase 与本目录阶段映射

| 父总纲范围 | 本目录落地阶段 | 说明 |
| --- | --- | --- |
| MVP：固定 Workbench DAG 与安全 digest | 被本目录替换为阶段零至阶段三的 runtime replan 主线 | 固定 DAG 不作为实施主路径；安全 digest、内部 capability、finalizer 和脱敏要求保留。 |
| Phase 2：确定性 Runtime Workbench Loop | 阶段零、阶段二、阶段三 | runtime budget / state、deterministic replanner、finalizer digest 与 graph 更新脱敏共同完成。 |
| Phase 3：Skill Contract 准入与健康诊断 | 阶段四 | `quality_workbench` optional contract、diagnostics 和 Skill 维护者文档。 |

## 阶段文件

| 阶段 | PRD | 目标 | 实施优先级 |
| --- | --- | --- | --- |
| 总纲 | `00-Skill运行闭环Workbench总纲PRD.md` | 目录内总纲摘录：统一 runtime replan 主线、不变量、术语、阶段依赖、总体验收矩阵与禁止项。 | Umbrella |
| 阶段零 | `01-阶段零-Workbench基座Policy与RuntimeStatePRD.md` | 建立 `WorkbenchPolicy`、`WorkbenchStage`、`WorkbenchOutputContractV1`、runtime budget、replan state 和 policy decision；不执行 Workbench。 | P0 |
| 阶段一 | `02-阶段一-内部Capability与ExecutorPRD.md` | 注册 `workbench.*` 内部 capability，提供本地 executor、输出契约校验、敏感字段剔除和基础 consumer contract tests。 | P1 |
| 阶段二 | `03-阶段二-RuntimeWorkbenchLoop与FinalizerDigestPRD.md` | 接入 deterministic `WorkbenchRuntimeReplanner`，根据 Skill / Workbench output 追加验证节点和必要 finalizer，并定义停止状态机与有限同能力 refinement retry。 | P2 |
| 阶段三 | `04-阶段三-事件GraphPrompt脱敏PRD.md` | 收口 SSE、`task.graph_updated`、task graph API、history artifact 与 finalizer prompt dependency context 的内部节点脱敏。 | P2 / Gate |
| 阶段四 | `05-阶段四-Contract质量策略与健康诊断PRD.md` | 支持 optional `quality_workbench` contract、diagnostics、health payload 和 Skill builder 文档。 | P3 |

## 推荐阶段执行顺序

```text
阶段零 Policy / budget / runtime state
  -> 阶段一 internal capability / executor / output validation
  -> 阶段二 deterministic runtime loop / finalizer digest
  -> 阶段三 event + graph + prompt 脱敏发布门禁
  -> 阶段四 contract quality_workbench / diagnostics / Skill builder docs
```

阶段三是 runtime loop 开发完成后的安全门禁：如果事件、graph 或 prompt 脱敏无法通过，不得进入生产部署。

## 总体验证命令入口

各阶段 PRD 给出更细命令。完整 release gate 至少覆盖：

```bash
python -m pytest tests/orchestration/
python -m pytest tests/capabilities/workbench/
python -m pytest tests/api/test_route_contract.py
python -m pytest tests/api/ -k "task or graph or event or capability"
```

若本阶段触及 prompt dependency context 或前端展示语义，还应补充对应前端 / API 文档回归；若只新增文档，则以链接、目录和索引检查为验收。
