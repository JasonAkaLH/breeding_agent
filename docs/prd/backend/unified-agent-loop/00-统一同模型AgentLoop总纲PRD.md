# 统一同模型 Agent Loop 总纲 PRD

- **日期**：2026-08-22
- **状态**：已批准未来架构；PRD组待整组复核，实现待开始
- **适用分支**：`main`
- **架构来源**：`docs/superpowers/specs/2026-08-21-unified-agent-loop-design.md`
- **拆分来源**：`docs/superpowers/specs/2026-08-21-unified-agent-loop-prd-decomposition-design.md`
- **目录入口**：`docs/prd/backend/unified-agent-loop/README.md`

## 1. 问题与目标

当前默认路径先生成完整`WorkflowPlan`，再由DAG executor按依赖执行，必要时由Runtime Replanner修改计划，最后由
独立`main_agent.respond`节点生成回答。这种方式无法自然支持根据每次Tool result持续选择下一步、同一sample多调用、
多waiting恢复和模型自然结束。

目标是把全部普通、显式Skill、显式MCP、补充输入、审批和remote continuation统一为在线循环：

```text
same model sample
  -> zero or more native tool calls
  -> durable capability outcomes
  -> same model sample
  -> ...
  -> no tool calls + non-empty assistant text
  -> one atomic final publication
```

成功不是“存在while循环”，而是模型能依据真实结果继续决策，暂停后恢复同一Run/call，普通错误可自纠，最终回答
由同一模型生成且只发布一次。

## 2. 用户与参与者

| 参与者 | 价值 |
|---|---|
| 最终用户 | 长链任务可依据中间结果继续，等待/审批后不丢上下文，只看到唯一最终回答 |
| Skill作者 | delegated/executable模式进入统一Tool合同，不重写现有业务input/output contract |
| MCP集成方 | 保留discovery、逐Tool授权、MRTR/Tasks、Result Parser和no-replay安全边界 |
| Runtime/Storage维护者 | AgentRun/AgentItem、原子outcome和单一Task lease替代跨DAG/进程内状态 |
| API/Frontend维护者 | Task/SSE/history/interrupt保持，graph变为empty-edge invocation ledger |
| 运维/安全 | 无轮次上限仍受取消、backpressure、context budget、权限和低敏指标控制 |

## 3. 范围

### 3.1 范围内

- provider-neutral原生Agent Model contract；
- AgentRun/AgentItem持久化、原子sample/outcome/final和Task lease；
- Invocation Kernel、Tool Catalog/Policy、Skill/MCP适配；
- multi-call Agent Loop、compaction、final publisher；
- Interrupt/approval/MRTR/remote recovery、cancel和no-replay；
- API、SSE、history、Frontend、事件和指标；
- 全入口clean cutover、DAG runtime删除和DAG physical schema删除；
- SQLite、PostgreSQL、Runtime Sidecar/Rust一致性。

### 3.2 非范围

- 不改变MCP discovery、transport、协议版本、Endpoint Policy或Result Parser；
- 不展开MCP Server完整Tool list给Outer Agent；
- 不改变Skill manifest业务input/output合同；
- 不引入第三方Agent框架、子Agent、图执行或异步锁框架；
- 不保存hidden reasoning；
- 不迁移、读取或恢复旧DAG Task；
- 不部署`prod`。

## 4. 总体架构

```text
API request / continuation
  -> AgentLoopOrchestrator
       -> AgentRunRepository / LeaseController
       -> AgentContextBuilder / AgentToolCatalogBuilder
       -> AgentModelPort (fixed model edition)
       -> CapabilityInvocationService
            -> CompositeExecutor
                 -> SkillExecutor / DelegatedActivation / MCP Dispatch
       -> AgentFinalOutputPublisher

SQLite / PostgreSQL / Runtime Sidecar
  -> AgentRun / AgentItem / Task / TaskNode / Interrupt / Artifact / Event
```

Model决定是否继续；Runtime只校验协议、权限、原子性、恢复和安全边界。Capability内部受控helper可以继续使用，但
不能取得Agent决策控制权或切换model edition。

## 5. 全局功能需求

| ID | 需求 | 主责阶段 |
|---|---|---|
| FR-1 | Cutover后全部执行/恢复入口进入AgentLoopOrchestrator | Phase 6 |
| FR-2 | Run内决策、观察、compaction和final固定同一model edition | Phase 0 |
| FR-3 | 原生返回零到多个tool calls并接收有序tool results | Phase 0 |
| FR-4 | 普通capability错误回填模型，不直接失败Task | Phase 3 |
| FR-5 | 仅无tool calls的非空assistant message正常完成Task | Phase 3 |
| FR-6 | 不实现maxTurns/max replans/max dynamic nodes | Phase 3 |
| FR-7 | 显式Skill/MCP只强制首次调用，随后恢复普通循环 | Phase 3 |
| FR-8 | approval、missing input、MRTR、remote恢复原Run/call | Phase 4 |
| FR-9 | 不确定副作用不自动重放，缺结果补aborted | Phase 4 |
| FR-10 | 多call支持安全并发门控和确定结果顺序 | Phase 3 |
| FR-11 | 上下文不足时同模型compaction，原始items保留 | Phase 3 |
| FR-12 | 最终回答只发布一次，不执行第二finalizer模型调用 | Phase 3 |
| FR-13 | MCP discovery、authorization、Selector和Result Parser不退化 | Phase 2 |
| FR-14 | 最终删除DAG runtime和旧任务兼容恢复入口 | Phase 6 |
| FR-15 | delegated Skill通过安全activation进入上下文且不运行脚本 | Phase 2 |
| FR-16 | executable Skill注入可信上下文，answer mode不再生成finalizer | Phase 2 |
| FR-17 | Sample/calls/result slots先于副作用原子持久化，每个outcome原子提交 | Phase 1 |
| FR-18 | Batch支持多个waiting calls，全部闭合后再继续 | Phase 4 |
| FR-19 | 每个公开model edition通过native tools/role/required choice门禁 | Phase 0 |
| FR-20 | MCP Router/Selector和恢复路径使用Run固定model binding | Phase 2 |
| FR-21 | `/graph`只返回empty-edge invocation ledger | Phase 5 |
| FR-22 | Durable Agent事件、transient reasoning和低基数指标闭合 | Phase 5 |
| FR-23 | `agent.final_output`提供唯一Artifact producer与原子终态 | Phase 3 |
| FR-24 | Skill routing使用安全public profile，完整catalog执行preflight | Phase 2 |
| FR-25 | 删除装饰性DAG timeout/resource字段，保留真实capability内部超时 | Phase 7 |
| FR-26 | 单一Task lease持续续租、waiting释放、失租fencing和接管 | Phase 1 |

## 6. 非功能需求

| 维度 | 主责阶段 | 全局门禁 |
|---|---|---|
| Provider与同模型 | Phase 0 | 所有公开edition启动门禁通过，Run内model binding固定 |
| 一致性/原子性 | Phase 1 | 三backend的reservation、waiting、final和lease语义一致 |
| 安全/隐私 | Phase 2 | capability可见性/参数fail closed，raw/hidden/secret不泄漏 |
| Tool catalog | Phase 2 | 完整schema计入预算，超限采样前明确失败 |
| 上下文 | Phase 3 | canonical payload有界，summary有digest且原始items保留 |
| 性能/资源 | Phase 3 | 仅安全并发，无busy polling，waiting不占worker，保留backpressure |
| Final唯一性 | Phase 3 | 无第二LLM call，Artifact/Message/event/receipt原子幂等 |
| 恢复/no-replay | Phase 4 | 多waiting、失租和不确定副作用恢复闭合 |
| 可观测性 | Phase 5 | 事件指标完整、低基数、无durable reasoning |
| API/Frontend兼容 | Phase 5 | Task/SSE/interrupt/history保持，graph固定兼容投影 |
| 可访问性 | Phase 5 | approval/interrupt/progress变化必须保持焦点、键盘和语义 |
| 可维护性 | Phase 6 | 单一控制面、单一invocation/outcome实现，ApiRuntime不承载Loop细节 |

## 7. 阶段门禁

| 边界 | 要求 |
|---|---|
| Phase 0～5 | pre-cutover proof；Phase 1 schema additive，Phase 2只做行为保持Kernel抽取，其余使用test-only assembly |
| Phase 6 | 同一受审commit序列切换全部入口并删除DAG runtime/wiring；禁止feature flag、fallback或dual runtime |
| Phase 7 | 仓库外备份恢复后删除TaskEdge和DAG-only storage/proto；完成全量与真实环境证明 |

## 8. 失败与恢复总原则

- malformed provider delta、缺call ID、非法name或损坏arguments走有界protocol retry，耗尽后fatal；
- 结构合法但catalog未知的tool name写`unknown_tool` result，不执行capability；
- 普通业务错误写failed tool result，模型可选择其他方法；
- storage/authority/identity损坏、provider contract耗尽、compaction失败为fatal；
- waiting不是terminal，不提前采样；
- 取消后不启动新call，late result丢弃，不发布final；
- crash后只恢复可证明authority，不自动重放不确定副作用。

## 9. 测试、真实环境与完成

- 后端使用当前`unittest`分层套件和新Agent contract/fault tests；
- Phase 1/7必须有真实PostgreSQL schema/transaction/permission/concurrency证据；
- Rust从`scripts/run_rust_quality_gates.py`运行相关门禁；
- Phase 5～7必须通过Frontend Vitest、typecheck和build；
- Phase 7必须完成仓库外备份恢复演练与受控真实MCP smoke，或对MCP smoke取得用户书面waiver；
- skip、缺DSN、缺工具、缺授权不是pass。

最终只有在FR-1～FR-26、全部NFR、三backend、全入口、静态删除、完整回归、文档和真实环境门禁全部闭合后，
目录状态才可标记`complete`。

## 10. 既有文档与测试

Phase 0生成active PRD inventory，分类`preserve/rewrite/supersede_at_phase6/historical`。Phase 6后active文档不得继续
把WorkflowPlan、Planner/Replanner、DAG finalizer或旧恢复路径称为当前运行时。

用户行为、安全、Skill/MCP、Interrupt/Cancel、Artifact/history和API兼容测试必须迁移；只断言DAG实现形状的测试在
Phase 6随源码删除，但每组删除都必须关联替代测试或证明其无行为合同。Migration历史测试可保留已删除表/列名，
不得被业务源码import。

## 11. 回滚

- Phase 0～5可回滚当前阶段代码和未使用additive schema；
- Phase 6后、Phase 7前可成对回退到最后DAG检查点，但新Agent Task不承诺由旧代码恢复；
- Phase 7后只能同时恢复Phase 7前代码和数据库/Sidecar备份，或forward fix；
- 任何操作不得读取、移动、跟踪或删除`docker_cmd.md`。

## 12. 开放问题

无。发现需要改变产品方向、风险容忍度、API支持义务或真实环境waiver时，必须回到用户确认。
