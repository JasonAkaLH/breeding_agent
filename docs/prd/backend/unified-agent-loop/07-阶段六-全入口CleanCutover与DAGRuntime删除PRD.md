# Phase 6：全入口 Clean Cutover 与 DAG Runtime 删除 PRD

- **日期**：2026-08-22
- **状态**：in_progress（P6-A proof_complete；下一检查点P6-B，尚未进入cutover）
- **文档审阅**：document-perfectization第二次全量审计100/100通过；P6-A双仓clean archive与Linux零skip证据已冻结
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **上游**：Phase 0～5必须`proof_complete`，cutover-readiness无未知入口
- **主责需求**：FR-1、FR-14
- **主责NFR**：可维护性与单控制面
- **直接参与者**：最终用户、API/Agent/Orchestration维护者、Skill/MCP/Lifecycle维护者、Frontend/Rust/文档与发布审查者
- **目标结果**：在同一受审commit序列中切换全部执行/恢复入口并删除DAG runtime源码与wiring；保留但不再读取DAG physical schema到Phase 7。

## 1. 目标与价值

Phase 0～5已经证明Model、Storage、Invocation、Loop、Recovery和API/Frontend合同。本阶段不再发明新功能，而是完成
不可分割的控制面切换：每个入口要么进入Agent Loop，要么被删除。任何feature flag、fallback或剩余DAG入口都会
形成双控制面，使Task恢复、Artifact、事件和终态不可预测。

## 2. 进入门禁

以下全部满足才能开始：

- Phase 0～5状态为`proof_complete`；
- SQLite、真实PostgreSQL、Runtime Sidecar/Rust Agent contract证据有效；
- Agent automatic/explicit/multi-call/final/waiting/recovery/cancel tests通过；
- API/Frontend/observability三门禁通过；
- `docs/prd/backend/unified-agent-loop/cutover-readiness.md`字段闭合，列出所有start/resume/cancel/recovery入口和旧runtime模块；
- 最后一个可回滚DAG代码检查点已提交；
- 当前分支确认是`main`，不涉及`prod`。

任一条件缺失，本阶段为`blocked`。

P6-A进入门禁已全部闭合：主仓DAG检查点`7bb8a05`/tree `cfdb89b`与外部Skill检查点`49b3aa0`/tree
`06c8ff8`的clean archive、相同bundle digest `sha256:38f4842d…c4e86`、只读`linux/amd64`候选启动、篡改前置拒绝及
Integrations 705项零skip证据均记录于`cutover-readiness.md`。该结论只解锁P6-B，不表示任何正式入口已切换。

### 2.1 当前证据与受影响模块

| 锚点 | 当前事实 | 本阶段影响 |
|---|---|---|
| `src/api/runtime.py::_run_execution`及startup recovery装配 | 当前构建/恢复WorkflowPlan并注入Planner/Replanner/finalizer | 全入口改为start/resume AgentRun并删除旧wiring |
| `src/orchestration/AGENTS.md`列出的workflow/planner/replanner模块 | 当前正式DAG控制面 | 本阶段删除生产源码/装配并同步索引 |
| `src/capabilities/main_agent/` | 当前包含finalizer、soft-skill reasoning和部分workflow逻辑 | 保留可复用prompt/disclosure/helper，删除控制面壳 |
| `tests/orchestration/`、`tests/api/test_runtime_replanner.py`等 | 混合行为合同与DAG实现断言 | 行为迁移，纯DAG断言按证据删除 |
| `docs/prd/README.md`、`backend/00`及旧专题PRD | 仍把DAG描述为当前基线 | Cutover同阶段更新active authority状态 |

## 3. 全入口切换 Inventory

至少覆盖：

| 入口 | Cutover后行为 |
|---|---|
| 普通message/task提交 | 创建/恢复唯一AgentRun并进入AgentLoopOrchestrator |
| 显式Skill命令 | 同一Loop，首轮required目标Skill，随后普通catalog |
| 显式MCP Server命令 | 同一Loop，首轮required pinned `mcp.dispatch`，server ID不可覆盖 |
| Skill missing-input answer | 通过continuation locator恢复原Run/call |
| MCP approval answer | 恢复原Run/call和pending authority |
| MCP MRTR/elicitation answer | 恢复原Run/call，不重跑Selector/Tool side effect |
| MCP remote Task completion/startup recovery | 唯一tool result写回原Run/call并继续 |
| Task cancel | 取消AgentRun/in-flight calls，late result discard |
| Crash/startup recovery | claim现有AgentRun，按authority/no-replay继续 |

静态route存在但仍构造WorkflowPlan、finalizer或continuation plan即视为未切换。

## 4. 必须删除的 Runtime

本阶段从业务源码和装配删除：

- `WorkflowPlan`/`WorkflowNodePlan`生产运行依赖；
- LLM/Auto/Skill/Main Agent/MCP Workflow Provider；
- Workflow Router、Expander、Validator；
- Planner contract、repair、node identity运行路径；
- DAG scheduler/execution loop和CompletionPolicy；
- Runtime Replanner、Soft Skill Replanner、Main Agent Replanner；
- `main_agent.respond`作为finalizer及其独立回答模型调用；
- `max_replans`、`max_dynamic_nodes`及对应config；
- `mcp_remote_task_continuation_plan`生产恢复路径；
- planner LLM config/factory/reasoning wiring；
- `planner.reasoning_delta`、`soft_skill.reasoning_delta`旧事件生产者及Frontend专属消费分支/实现断言；
- Prompt中的“自动DAG/上游DAG节点”等控制面措辞。

可以保留名字含planner但服务于独立非任务编排功能的代码，前提是静态证据证明不进入Task执行/恢复；否则重命名或
删除。历史migration/fixture引用必须明确，不得被业务源码import。

## 5. 必须保留到 Phase 7 的 Additive Physical Contract

为了保持Phase 6后仍可成对回退代码，本阶段暂时保留但新runtime不读取：

- TaskEdge表/storage/proto；
- Task.root_node_id；
- TaskNode criticality、dependency_type、retry_policy、timeout_policy、resource_class；
- API DTO所需固定兼容字段。

必须在`docs/prd/backend/unified-agent-loop/dag-runtime-deletion-report.md`记录明确inventory和“零生产读取”静态证明。
保留schema不等于保留DAG runtime或兼容恢复。

## 6. Assembly合同

- `ApiRuntime`只装配AgentLoopOrchestrator、Agent repositories/model port、InvocationService和projection services；
- 不保留enable flag、请求级selector、旧provider map或fallback factory；
- Agent Model、compaction、MCP Router/Selector共享Run binding；
- MainAgent可复用的PromptEnvelope、fallback disclosure、Artifact/Event helper应已提取，不能保留finalizer壳；
- MCP shadow observation从真实`mcp.dispatch` invocation hook开始，不预读WorkflowPlan；
- startup readiness必须验证所有公开model editions和Agent storage contracts。

## 7. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P6-01 | 所有start/resume/recovery入口只进入Agent Loop。 | Route/assembly spies和E2E覆盖inventory全部入口。 |
| AL-P6-02 | 生产源码不再构建或执行WorkflowPlan。 | 静态扫描和runtime spy为零。 |
| AL-P6-03 | Planner/Replanner/CompletionPolicy/finalizer不再装配。 | Dependency graph和startup tests无对应对象。 |
| AL-P6-04 | 不存在dual-runtime flag/fallback。 | Config/DTO/env/runtime搜索为零，非法旧配置不能启用旧路。 |
| AL-P6-05 | 普通/显式/continuation/cancel用户行为通过。 | 全入口API/E2E和history/Frontend回归。 |
| AL-P6-06 | MCP/Skill安全链保持。 | 现有ordinary/approval/MRTR/remote/result parser/slot/artifact回归。 |
| AL-P6-07 | 旧DAG physical schema无生产读取。 | Repository/proto call spies和`dag-runtime-deletion-report.md`静态inventory通过。 |
| AL-P6-08 | Active PRD不再把DAG标为当前基线。 | 文档inventory无active旧控制面口径。 |
| AL-P6-09 | 新Agent Task不承诺旧代码恢复。 | Run metadata/docs无兼容adapter或转换器。 |
| AL-P6-10 | Cutover可整体回滚。 | 回滚演练使用最后DAG代码检查点和pre-Phase7 schema。 |

## 8. 旧测试与文档处置

- 用户行为、安全、Skill/MCP、Interrupt/Cancel、Artifact/history/API测试迁移到Agent入口；
- 只断言DAG shape/edge/Replanner次数/finalizer存在的测试随源码删除；每组删除关联替代测试或无行为合同证据；
- `docs/prd/README.md`和`backend/00-主代理框架PRD.md`改为Agent当前编排入口；
- 旧MainAgent/memory/recovery/Workbench/fallback/Prompt/MCP/Frontend/Rust PRD按inventory preserve/rewrite/supersede/
  historical处理；
- 受影响`AGENTS.md`和CHANGELOG同步。

## 9. 失败模式

- 任一入口仍走DAG：阻断提交/回滚整个cutover；
- 全量回归出现无法归因红测：保持blocked，不以删除测试解决；
- Agent startup gate失败：Runtime fail closed，不回退DAG；
- 新Task写入后尝试旧代码回滚：允许清理开发Agent数据，但不构造旧WorkflowPlan；
- Phase 6过程中进程崩溃：部署/运行只允许完整pre-cutover或完整post-cutover binary，不支持混合版本；
- 文档或静态删除清单未闭合：不标记cutover_complete。

### 9.1 跨阶段NFR协作

| NFR | 本阶段责任 | 最终复验 |
|---|---|---|
| 可维护性与单控制面 | 主责：单一assembly、invocation/outcome实现、无DAG/flag/fallback | Phase 7 physical/static/docs再次证明 |
| Provider/一致性/安全/catalog/context | 消费Phase 0～3合同并在真实入口复验 | 全入口E2E、leak scan、same-binding和catalog overflow tests |
| 性能/final/recovery/observability/API/accessibility | 消费Phase 3～5闭环 | 完整Backend/Frontend/Rust与cutover readiness证据 |

## 10. 验证门禁

必须运行：

- README“验证口径”中的全部canonical后端命令，逐条要求非零测试；
- README中的Frontend Vitest、typecheck、build；
- 受影响Rust contract/tests；
- start/resume/cancel/recovery全入口E2E；
- Skill/MCP完整回归；
- runtime/config/docs静态删除扫描；
- pre/post cutover rollback rehearsal。

Rust最低命令：

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run \
  --only cargo_fmt --only cargo_clippy --only cargo_test
```

生产runtime零引用扫描必须覆盖`src/`、`frontend/src/`和`native/`，并把命令、commit与结果写入
`dag-runtime-deletion-report.md`。任何required命令不存在、零测试、skip或non-zero exit均失败。

本阶段不执行DAG physical migration，也不把真实MCP smoke推迟的缺口误记为最终complete。

## 11. 风险、假设与开放问题

| 风险 | 缓解/阻断条件 |
|---|---|
| 遗漏start/resume/recovery入口 | `cutover-readiness.md`双向route/worker inventory和全入口spy/E2E |
| 同一commit序列出现可运行半切换binary | 只允许完整pre/post assembly；中间检查点不得作为可运行交付物 |
| 删除旧测试掩盖行为回归 | 每组删除关联replacement test或纯实现断言证据 |
| 旧DAG schema被新代码继续读取 | Repository/proto spy和`dag-runtime-deletion-report.md`零读取扫描 |
| Active PRD仍指导Planner/Replanner | Phase 0 inventory闭合和主索引authority更新 |

已确认假设：Phase 0～5全部proof与真实环境门禁可在cutover前复验；Phase 7前旧physical schema仍允许完整Phase 6
binary运行。开放问题：无。

## 12. Git与回滚

- 使用单一受审commit序列；中间commit可构建测试，但任何可运行检查点只能是全旧或全新控制面；
- 最后DAG checkpoint必须可与尚未删除的schema一起启动；
- Phase 6后、Phase 7前回滚时成对回退代码和开发Agent数据；
- 不使用feature flag保持旧runtime；
- 不读取、移动、跟踪或删除`docker_cmd.md`。

## 13. 完成与交接

AL-P6-01～10、完整回归、`dag-runtime-deletion-report.md`和文档零漂移通过后，状态标记`cutover_complete`。生产源码
只剩Agent Loop控制面；DAG physical fields存在但无生产读取。

交付Phase 7：单一Agent runtime assembly、固定`dag-runtime-deletion-report.md`、待删除schema/proto inventory和最后
可恢复备份边界。
