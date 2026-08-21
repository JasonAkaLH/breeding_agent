# Phase 3：核心 Agent Loop 与 Final Output PRD

- **日期**：2026-08-22
- **状态**：pending
- **文档审阅**：document-perfectization第二次全量审计100/100通过；实现尚未开始
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **上游**：Phase 0～2必须`proof_complete`
- **主责需求**：FR-4、FR-5、FR-6、FR-7、FR-10、FR-11、FR-12、FR-23
- **主责NFR**：上下文正确性、性能与资源、最终输出唯一性
- **直接参与者**：Agent Runtime维护者、最终用户、Prompt/Memory维护者、Skill/MCP调用方、Artifact/History与发布审查者
- **目标结果**：实现test-only的同模型Agent循环、multi-call、compaction和原子final publication；不切真实入口，不交付完整waiting resume。

## 1. 目标与价值

本阶段建立真正的在线控制循环：模型基于durable tool results持续选择下一步，普通错误可纠正，没有固定轮次上限，
无tool calls的非空assistant text自然结束。最终文本不再经第二模型/finalizer加工，并以唯一Artifact/Message/event/receipt
原子发布。

## 2. 进入条件

- AgentModelPort/native tool contract和model gate通过；
- AgentRun/AgentItem/atomic operations/lease parity通过；
- Invocation Kernel、Tool Catalog/Policy、Skill/MCP适配通过；
- 本阶段只使用test-only assembly，不接受真实API路由。

### 2.1 当前证据与受影响模块

| 锚点 | 当前事实 | 本阶段影响 |
|---|---|---|
| `src/orchestration/service.py`、`completion_policy.py` | 当前由DAG循环和CompletionPolicy决定执行/完成 | 新增AgentLoopOrchestrator test assembly，不切真实入口 |
| `src/capabilities/main_agent/` | `main_agent.respond`负责独立final回答与Artifact/Event | 提取可复用helper，新FinalPublisher不二次调用模型 |
| `src/orchestration/prompt_envelope.py`、`conversation_memory.py` | 已有安全prompt和memory候选 | AgentContextBuilder复用并加入durable items/summary |
| `src/integrations/llm_runtime.py` | Shared runtime管理模型调用 | 通过Phase 0 AgentModelPort固定edition采样/compaction |
| main-agent、prompt、memory、artifact/history tests | 锁定披露、下载、history和上下文安全 | 新Loop/final必须保留这些合同 |

## 3. 范围与非范围

### 3.1 范围内

- AgentLoopOrchestrator状态机；
- ContextBuilder、Tool catalog接入和模型采样；
- sample/calls/result reservations及deterministic waves；
- ordinary result/error回填和模型继续；
- 显式Skill/MCP首轮required constraint；
- 同模型compaction；
- `agent.final_output` node和FinalOutputPublisher；
- cancellation/fatal检查点；
- 无`maxTurns`的长轨迹proof。

### 3.2 非范围

- 不完成Interrupt/approval/MRTR/remote continuation resume；
- 不改真实API/SSE/Frontend入口；
- 不删除DAG runtime；
- 不实现异步用户steer；
- 不实现lazy Tool discovery或子Agent；
- 不引入模型切换、轮次预算或动态节点预算。

## 4. 状态机

```text
acquire AgentRun lease
  -> append trusted user/continuation fact when required
  -> build bounded context + complete visible catalog
  -> sample fixed model
  -> commit assistant sample
       -> tool calls present
            -> reserve all calls/results before side effects
            -> execute deterministic waves through Invocation Kernel
            -> commit each outcome into reserved slot
            -> if batch closed: renew lease and sample again
            -> if waiting authority: persist suspension and stop test runner
       -> no calls + non-empty text
            -> atomic final publication
       -> invalid/empty
            -> bounded protocol retry, exhausted => fatal
```

每个active await边界检查cancel、current lease token和Run revision。没有iteration counter或turn budget。

## 5. Multi-call与顺序

- 一个sample的calls构成batch；
- 连续`parallel_safe=true` calls形成并发wave；每个其他call是exclusive wave；
- waves严格按ordinal执行，不越过exclusive call；
- 并发使用标准`asyncio`机制，不新增RwLock/第三方锁；
- ordinary failure不取消同wave其他calls，fatal才取消；
- outcome完成即写预留slot；模型上下文只在batch闭合后按call ordinal渲染；
- waiting时后续wave不启动，Phase 4再恢复。

## 6. 显式路由

- 普通请求首轮catalog包含当前请求全部可见Capabilities；
- 显式Skill首轮只允许目标Skill并要求恰好一次call；
- 显式MCP首轮只允许pinned`mcp.dispatch`，server ID由system注入；
- 首轮constraint成功闭合后恢复普通catalog；
- required choice违规按Phase 0 protocol retry；
- Capability不可用返回普通tool result，不回退旧Workflow Provider。

## 7. Context与Compaction

ContextBuilder按固定顺序组装：stable rules、安全Tool rules、同模型summary、未覆盖AgentItems、current user/continuation
facts和final guard。Raw MCP result、上传正文、hidden reasoning和未净化Skill内容不得进入。

达到输入预算时：

- 先消费Phase 2 Catalog Preflight；仅`history_compaction_required`允许进入compaction；
- 使用同一model edition生成结构化summary；
- summary绑定covered sequence range和源items digest；
- append `context_summary`并CAS更新covered boundary；
- 原始items不删除；
- 下一轮使用summary+suffix；
- compaction不能调用业务Capability或改变Tool决策；
- compaction后必须重新运行完整Catalog Preflight；仍不适配或必保segments超限时fatal；
- 每次compaction必须推进`compacted_through_sequence`并产生新的covered digest；若没有推进或相同decision在无新eligible
  range时重复，必须fatal，不能busy-loop；
- retry耗尽fatal，不静默丢历史。

## 8. Final Output

无tool calls的非空assistant sample是唯一正常完成条件。FinalOutputPublisher不得再次调用模型：

1. 使用确定性`agent.final_output` TaskNode/Artifact/Message/event IDs；
2. 从已持久化final item生成确定性delta chunks；
3. 调用Phase 1原子final operation，提交Artifact、history、final event、receipt、Node/Run/Task终态和claim cleanup；
4. 事务成功后发布committed final/terminal events；
5. crash重放相同IDs，Frontend按event ID去重。

`agent.final_output`不进入Tool catalog、不调用模型、不分配Capability instance，也不是DAG finalizer。Fallback事实披露和
下载声明guard必须保留。

## 9. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P3-01 | Tool result后必须由同一model edition重新采样。 | Fake记录全轨迹binding identity。 |
| AL-P3-02 | 普通failed/unknown/aborted result不直接失败Task。 | 模型可改用其他Tool并完成。 |
| AL-P3-03 | 只有无calls的非空text正常完成。 | Empty、invalid、text+calls均不误完成。 |
| AL-P3-04 | Loop没有固定轮次/节点上限。 | 超过旧max_replans/max_dynamic_nodes数值仍完成。 |
| AL-P3-05 | Multi-call waves与结果顺序确定。 | Parallel/exclusive交错和乱序完成tests。 |
| AL-P3-06 | 显式约束只作用首轮。 | 首轮forced，result后普通catalog恢复。 |
| AL-P3-07 | Compaction只响应typed Preflight、使用同模型、可恢复且不删items。 | Decision、digest、suffix、re-preflight、crash/CAS和retry failure tests。 |
| AL-P3-08 | Final只调用一次模型并只发布一份。 | Fake无第二call；Artifact/Message/event/receipt唯一。 |
| AL-P3-09 | Final crash可幂等重放。 | Delta前/后、commit前/后fault injection无重复history。 |
| AL-P3-10 | waiting authority不会提前采样。 | Test runner返回suspended，后续model calls为0。 |

## 10. 失败、取消与停止

- ordinary capability错误：closed tool result并继续；
- storage/identity/authority损坏、provider retry耗尽、compaction失败：fatal；
- cancel：停止新call，取消in-flight，未闭合call按安全规则aborted，不采样/不final；
- late result：旧owner提交失败，不覆盖cancel/new owner；
- waiting：不是terminal；本阶段只证明持久化停止点，Phase 4交付resume；
- 正常停止只由final candidate触发。

### 10.1 跨阶段NFR协作

| NFR | 本阶段责任 | 后续复验 |
|---|---|---|
| 上下文正确性 | 主责：ContextBuilder、summary/digest/suffix和Preflight重试 | Phase 4恢复后、Phase 6/7全入口复验 |
| 性能与资源 | 主责：deterministic waves、no busy polling、backpressure | Phase 4 waiting资源释放、Phase 5 metrics复验 |
| 最终输出唯一性 | 主责：无第二LLM、atomic final和crash replay | Phase 5 history/SSE、Phase 6/7E2E复验 |
| Provider/一致性/安全/catalog | 消费Phase 0～2 binding、atomic APIs和safe projections | Fake binding、fault injection和leak scan必须随Loop测试运行 |
| 可观测性 | 产生Phase 5定义所需sample/call/compaction/final facts | Phase 5负责closed event/metric projection |

## 11. 测试计划

新增Agent Loop unit/integration/fault tests，并复用main-agent、PromptEnvelope、memory、Artifact/history回归。最低场景：

- `tool_call -> result -> sample -> final`；
- 3步以上链式Tool；
- multi-call safe/exclusive waves；
- ordinary failure纠错；
- long trajectory无旧预算终止；
- compaction+resume；
- final no-second-call和crash replay；
- waiting suspension不提前采样。

本阶段不得通过真实API请求启用新Loop。测试assembly必须是显式fixture，不能形成runtime feature flag。

实施必须创建并运行以下精确目标或等价同名模块；模块不存在、`Ran 0 tests`或non-zero exit均失败：

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_final_output \
  tests.api.test_main_agent_llm \
  tests.api.test_task_query \
  tests.capabilities.main_agent.test_conversation_memory_prompt
```

## 12. 风险、假设与开放问题

| 风险 | 缓解/阻断条件 |
|---|---|
| 无`maxTurns`造成长轨迹成本/等待 | 保留cancel/backpressure/compaction和分布metrics，不以隐式轮次终止 |
| Multi-call并发产生共享副作用 | 默认全部非并发；只有显式parallel-safe contract进入并发wave |
| Compaction遗漏关键Tool事实 | 原始items不删、covered digest、golden summary和re-preflight tests |
| Final delta先发布后crash产生重复正文 | 确定性IDs、Frontend去重和final fault matrix |
| Text+tool calls污染history | Tool sample text不进入assistant正文/history，adapter+history tests双重保护 |

已确认假设：所选model edition可在输入预算内完成结构化compaction；现有PromptEnvelope、memory和Artifact/Event helper
可复用。开放问题：无。

## 13. Git检查点与回滚

- 新Loop、ContextBuilder和Publisher只进入test assembly；
- 不删除旧DAG/finalizer，不改变用户路由；
- 回滚删除Agent-only orchestration并保留Phase 0～2稳定contract；
- 若发现需要maxTurns或模型切换，状态转`blocked`并回到产品决策，不自行增加。

## 14. 完成与交接

AL-P3-01～10、长轨迹、multi-call、compaction和final fault tests通过；真实入口不变；没有第二Agent控制面。

交付Phase 4：AgentLoopOrchestrator、ContextBuilder、FinalOutputPublisher和持久suspension点。Phase 4不得依赖旧finalizer、
API route或进程内未持久状态。
