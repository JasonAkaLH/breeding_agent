# 统一同模型 Agent Loop 实施计划

## 状态与依据

- 日期：2026-08-22
- 分支：`main`
- 状态：待用户批准；业务实现尚未开始
- 总纲：`docs/prd/backend/unified-agent-loop/00-统一同模型AgentLoop总纲PRD.md`
- 阶段依据：`docs/prd/backend/unified-agent-loop/README.md`及Phase 0～7八份阶段PRD
- 架构依据：`docs/superpowers/specs/2026-08-21-unified-agent-loop-design.md`
- 拆分依据：`docs/superpowers/specs/2026-08-21-unified-agent-loop-prd-decomposition-design.md`
- 当前代码基线：`main@f707235`；正式运行时仍为DAG，仓库中尚无`AgentRun`、`AgentItem`、
  `AgentModelPort`或`AgentLoopOrchestrator`
- 计划边界：本计划只安排`main`开发仓库实现，不部署`prod`，不迁移或恢复旧DAG Task

本计划把已批准PRD转换为逐文件、逐测试、逐回滚点的green checkpoint。计划不改变同模型、无固定轮次上限、
完整Tool catalog、no-replay、单一Task lease、clean cutover和Phase 7破坏性删除等产品与安全决策。

## 1. 完成声明

只有同时满足以下条件，才能宣称统一Agent Loop开发完成：

1. FR-1～FR-26及总纲12类NFR都有绑定当前commit的自动或真实环境证据；
2. 普通、显式Skill、显式MCP、missing-input、approval、MRTR、remote completion、cancel和startup recovery全部进入或
   恢复同一`AgentRun`；
3. Run内采样、Tool选择、compaction、MCP Router/Selector和final固定同一`AgentModelBinding`；
4. native sample的全部call/result slot先于副作用持久化，outcome、waiting、fatal/cancel和final均原子提交；
5. 一个sample支持零到多个call、安全并发wave、确定性结果顺序和多个waiting call；
6. 普通failed/unknown/aborted结果回填模型，只有无call的非空assistant text可正常完成；
7. `agent.final_output`只发布一次Artifact、Message、event和receipt，且没有第二次LLM finalizer调用；
8. SQLite、真实PostgreSQL和Runtime Sidecar/Rust在canonical payload、事务、lease、恢复和最终发布上语义一致；
9. Phase 6后生产源码无DAG runtime、Planner/Replanner、CompletionPolicy、独立finalizer或dual-runtime开关；
10. Phase 7后TaskEdge和DAG-only Task/TaskNode storage/proto合同物理删除，`/graph`仍返回empty-edge ledger；
11. Backend、Frontend、Rust、静态扫描、文档、仓库外备份恢复和受控真实MCP门禁通过；
12. `docker_cmd.md`始终存在、ignored、untracked，且全程不读取、不移动、不跟踪、不删除。

缺真实PostgreSQL DSN、required Rust工具、备份恢复证据或真实MCP授权时，阶段状态必须是`blocked`，不能以skip、mock或
书面记录冒充通过；只有真实MCP smoke可以由用户明确书面waiver。

## 2. 执行策略与顺序

严格关键路径不可并行越过阶段门禁：

```text
Phase 0 Model Contract
  -> Phase 1 Agent Storage / Lease
  -> Phase 2 Invocation / Skill / MCP
  -> Phase 3 Core Loop / Final
  -> Phase 4 Waiting / Recovery
  -> Phase 5 API / Frontend / Observability
  -> Phase 6 Clean Cutover + DAG Runtime Delete
  -> Phase 7 Backup/Restore + Physical Schema Delete + Final Proof
```

每个checkpoint执行固定循环：

1. 记录`git status --short`、当前commit和阶段进入证据；
2. 先新增能证明目标合同的失败测试，不单独提交红测；
3. 实现最小代码使聚焦测试转绿；
4. 运行受影响域回归、`compileall`和`git diff --check`；
5. 审查diff、更新阶段证据/AGENTS/CHANGELOG，并创建范围清晰的Git commit；
6. 只有当前阶段全部checkpoint和真实环境门禁通过，才把阶段标为`proof_complete`或相应终态。

P0-A至P5-B的业务删除范围默认为“无”：只允许移除当前checkpoint自己造成的孤儿import、fixture或未使用窄helper；
不得顺手删除既有DAG源码、测试或schema。P6-B、P6-C和P7-B的删除范围以各节显式inventory为准，超出清单必须先
回到计划审查。

Phase 0～5只允许additive schema、行为保持Kernel抽取和test-only Agent assembly。Phase 6以前不得注册真实Agent route、
请求级feature flag或shadow副作用执行。Phase 6必须在同一受审commit序列完成全入口切换与DAG runtime删除，Phase 7前
不得删除DAG physical schema。

## 3. Checkpoint 总览

| 阶段 | Checkpoint | 主要交付 | 阶段状态出口 |
|---|---|---|---|
| 0 | P0-A | 现状、入口、旧测试与active PRD inventory | `in_progress` |
| 0 | P0-B | provider-neutral Agent Model contract、native adapter、edition gate | `proof_complete` |
| 1 | P1-A | AgentRun/AgentItem/canonical codec/SQLite原子操作 | `in_progress` |
| 1 | P1-B | PostgreSQL原子操作、单一Task lease与fencing | `in_progress` |
| 1 | P1-C | Runtime Sidecar proto/Rust/Python parity | `proof_complete` |
| 2 | P2-A | 行为保持地提取唯一Invocation Kernel | `in_progress` |
| 2 | P2-B | Tool Catalog、Policy与完整预算preflight | `in_progress` |
| 2 | P2-C | delegated activation与executable Skill适配 | `in_progress` |
| 2 | P2-D | Run-bound MCP binding与现有安全链回归 | `proof_complete` |
| 3 | P3-A | Agent Context与核心Loop、multi-call waves | `in_progress` |
| 3 | P3-B | 同模型compaction与有界上下文 | `in_progress` |
| 3 | P3-C | 唯一final output与长轨迹/fault proof | `proof_complete` |
| 4 | P4-A | multi-waiting与identity-bound locator | `in_progress` |
| 4 | P4-B | continuation、crash recovery、cancel/no-replay | `proof_complete` |
| 5 | P5-A | API/SSE/history/graph/events/metrics投影 | `in_progress` |
| 5 | P5-B | Frontend恢复、多waiting、可访问性与readiness报告 | `proof_complete` |
| 6 | P6-A | 最后DAG回滚检查点与cutover预检 | `in_progress` |
| 6 | P6-B | 全入口切换、DAG runtime/wiring/config删除 | `in_progress` |
| 6 | P6-C | 全量证明、删除报告与文档authority切换 | `cutover_complete` |
| 7 | P7-A | 三backend仓库外备份及隔离恢复演练 | `in_progress` |
| 7 | P7-B | TaskEdge/DAG-only schema/proto破坏性删除 | `in_progress` |
| 7 | P7-C | 全量、真实环境、静态与文档最终证明 | `complete` |

## 4. Phase 0：基线与 Agent Model Contract

### P0-A：锁定现状和处置清单

目标：建立可比较基线，不写Agent storage，不改变任何用户入口。

文件与任务：

- 新建`docs/prd/backend/unified-agent-loop/active-prd-inventory.md`，双向登记所有命中`WorkflowPlan`、
  `RuntimeReplanner`、`main_agent.respond`、`CompletionPolicy`、`max_replans`、`max_dynamic_nodes`的active文档；
- 新建测试inventory或脚本fixture，分类行为/安全测试、Phase 6迁移测试和纯DAG shape删除候选；
- 记录`src/api/runtime.py::_run_execution`、Interrupt answer、MCP continuation worker、startup recovery、cancel、普通/显式
  start route清单；
- 运行Phase 0 PRD列出的现有LLM/model-edition基线测试，并记录任何既有失败。

Green gate：inventory与`rg`发现集双向一致；现有LLM text/title/helper行为有可重放基线。

回滚：仅删除inventory和基线fixture；业务运行时不变。

建议commit：`test(agent): lock unified loop baseline inventory`。

### P0-B：Agent Model contract、adapter与edition gate

优先新增文件：

- `src/core/agent_model.py`：`AgentModelBinding`、`AgentToolDescriptor`、`AgentModelRequest`、`AgentSample`、
  `AgentToolCall`、usage/finish/protocol错误闭合类型；
- `src/integrations/agent_model_port.py`：provider-neutral sampling port和测试fake；
- `src/integrations/openai_agent_model_adapter.py`：native tools、stream delta组装、provider-safe name和required choice；
- `src/integrations/agent_model_gate.py`：逐公开edition启动能力门禁；
- 修改`llm_client.py`、`llm_runtime.py`、`model_editions.py`，只新增Agent-only入口，保留`generate_text()`兼容路径；
- 新建`tests/integrations/test_agent_model_adapter.py`、`test_agent_model_gate.py`，扩展现有LLM和model edition测试。

先写红测：0/1/N calls、交错delta、缺ID、非法/重复name、损坏JSON、text+calls、unknown tool、required choice违规、
cancellation、usage缺失、同edition identity和role/text fallback拒绝。

Green gate：Phase 0 AL-P0-01～10全部通过；不合格edition不公开，默认edition失败时Runtime fail closed；非Agent text路径
无行为回归。

回滚：删除Agent-only contract/adapter/gate，恢复model edition新增声明；不涉及数据。

建议commit：`feat(agent): add native model sampling contract`。

## 5. Phase 1：Agent durable state 与单一 Task lease

### P1-A：Core contract、canonical codec与SQLite

优先新增文件：

- `src/core/agent_state.py`：`AgentRun`、`AgentItem`、closed status/kind和原子操作输入/结果类型；
- `src/core/agent_contracts.py`：窄`AgentRunRepository`与`AgentAtomicWriter`协议，避免继续扩大已有
  `StoragePort`和巨型repository职责；
- `src/storage/agent_payload.py`：严格canonical JSON、131072-byte上限和digest；
- `src/storage/sqlite/agent_repository.py`：Agent表、CAS、sample/result reservation、outcome、waiting、fatal/cancel、final；
- 修改SQLite models/bootstrap/session导出与schema测试，但旧DAG表/字段保持additive不变；
- 新建`tests/core/test_agent_state.py`、`tests/storage/test_agent_storage_conformance.py`、
  `tests/storage/test_agent_storage_sqlite.py`和fault fixtures。

先写红测：sequence唯一、result引用、sample前零executor调用、事务all-or-zero、staged Artifact不可见、多个waiting一致性、
deterministic final IDs、canonical 131071/131072/131073 bytes和NaN/Infinity拒绝。

Green gate：SQLite完成AL-P1-01～07；旧binary/schema bootstrap可忽略Agent additive表；没有模型或Capability调用。

回滚：回退additive migration和未接流量repository；开发Agent测试数据可按精确表清理，不转换旧Task。

建议commit：`feat(storage): persist agent run state in sqlite`。

### P1-B：PostgreSQL、Task lease与fencing

文件与任务：

- 新建`src/storage/postgres/agent_repository.py`，同步schema manifest、reconciler和最小权限；
- 新建`src/storage/agent_lease.py`，统一SQLite/PostgreSQL/Sidecar `LeaseController`语义；
- 扩展现有`RuntimeLeaseFacade`，复用Task lease，不创建Agent专用lease；
- 新建`tests/storage/test_agent_storage_postgres_integration.py`、`test_agent_task_lease.py`和fake-clock/fault matrix。

先写红测：storage权威时钟、TTL/3 heartbeat、token rotation、renew临界失败、stale commit、expiry takeover、waiting先提交后
release、resume reacquire、terminal cleanup和permission drift。

Green gate：真实PostgreSQL schema/transaction/permission/concurrency通过；active sample、compaction、wave、final全程续租；
waiting不heartbeat。缺测试DSN时Phase 1保持`blocked`。

回滚：回退additive PostgreSQL Agent schema和LeaseController；不修改旧Task lease数据。

建议commit：`feat(storage): fence agent runs with task leases`。

### P1-C：Runtime Sidecar / Rust parity

文件与任务：

- additive修改`native/proto/maf/runtime/v1/runtime.proto`，新增Agent records和原子RPC；TaskEdge和DAG字段暂时保留；
- 在`maf_core_types`、`maf_runtime_store`、`maf_runtime_sidecar`新增Agent model、SQLite adapter、事务和lease parity；
- 扩展`src/storage/runtime_sidecar_grpc_client.py`、`runtime_sidecar_facade.py`及Rust contract JSON；
- 新建Rust/Python共享canonical vectors和`tests/storage/test_rust_runtime_sidecar_contract.py`中的Agent用例。

Green gate：Python/SQLite、真实PG、Rust Sidecar三backend共享conformance/fault vectors；Rust fmt/clippy/test通过；旧Sidecar
contract additive兼容验证通过。

回滚：成对回退proto、Rust、Python facade和additive schema；禁止运行混合contract binary。

建议commit：`feat(runtime): add sidecar agent state contract`。

Phase 1退出：AL-P1-01～10在三backend全部通过，状态才可标记`proof_complete`。

## 6. Phase 2：Invocation Kernel、Catalog、Skill 与 MCP

### P2-A：行为保持地提取唯一 Invocation Kernel

优先新增`src/orchestration/invocation_service.py`和窄输入/结果contract；从`service.py::_execute_node`抽取route authority、
instance selection、Node start CAS、`CapabilityExecutionRequest`、CompositeExecutor调用、Artifact/Event/Interrupt处理、结果分类和
Phase 1 atomic outcome调用。

先写spy/golden红测，固定旧DAG在Task/Node/Artifact/Event/Interrupt/cancel/late-result上的可见行为。旧DAG改为调用公共Kernel，
但scheduler、Planner和入口仍不变。不得复制一份Agent专用executor lifecycle。

Green gate：旧DAG behavior-preservation回归通过；静态/spy证明只有一个invocation lifecycle。

回滚：独立回退Kernel抽取，恢复原`service.py`私有生命周期；Agent storage保留未接流量。

建议commit：`refactor(orchestration): extract capability invocation kernel`。

### P2-B：Tool Catalog、Policy与完整预算preflight

优先新增：

- `src/orchestration/agent_tool_catalog.py`；
- `src/orchestration/capability_invocation_policy.py`；
- `tests/orchestration/test_agent_tool_catalog.py`、`test_agent_catalog_preflight.py`。

把当前planner payload policy中仍有效的字段allowlist/system override迁入或适配到唯一invocation policy；所有Capability默认
`parallel_safe=false`，无policy、private、disabled、跨owner项不进入catalog。Outer Agent只看到public Skills与一个
`mcp.dispatch`。

Preflight只返回`fits|history_compaction_required|fatal_required_segments_too_large`，计入完整schema和必保规则，不做summary、
不裁剪schema、不实现lazy discovery。

Green gate：可见性、prompt injection、热重载、完整schema预算、no-schema-leak和三种closed decision通过。

回滚：删除Agent catalog/preflight，旧DAG继续使用公共Kernel。

建议commit：`feat(agent): build policy-filtered tool catalog`。

### P2-C：delegated activation与executable Skill

优先新增`src/orchestration/delegated_skill_activation.py`和对应测试；只消费pinned revision的
`PublicSkillProfile.to_dict()`安全投影，resource index只保留公开元数据，写`skill_activation` item和digest，永不执行脚本。

修改`src/capabilities/skill_tool/executor.py`及Skill wiring，使`python_subprocess/platform_service`继续走现有Executor，模型字段
经过allowlist，用户/附件/revision由system覆盖；三种answer mode都只返回tool result，不创建Agent finalizer。

Green gate：manifest body/resource正文/path/config/secret leak scan、pinned revision、脚本零调用、三answer mode和可信上下文
覆盖通过。

回滚：删除Agent activation adapter，旧DAG Skill行为保持。

建议commit：`feat(agent): adapt delegated and executable skills`。

### P2-D：Run-bound MCP model binding与安全链

修改`src/capabilities/mcp_dispatch/server_router.py`、`selector.py`和窄runtime装配，使Router/Selector从Run解析Phase 0 binding；
不得读取请求中的替代edition。复用现有discovery、authorization、approval、MRTR/Tasks、Result Parser、durable result、
call budget和no-replay实现，不改变transport或协议版本。

先写ordinary、approval、remote/restart的binding identity测试，以及显式server system override、当前Profile allowlist、raw result/
Server Tool list不进入AgentItems测试。

Green gate：AL-P2-01～09、Skill/MCP完整安全回归和旧DAG Kernel行为保持通过；Agent adapter仍仅由test fixture调用。

回滚：回退Run binding注入和Agent MCP adapter，不触碰现有MCP authority数据。

建议commit：`feat(agent): bind mcp routing to agent runs`。

## 7. Phase 3：核心 Loop、Compaction 与 Final Output

### P3-A：ContextBuilder、Loop状态机与deterministic waves

优先新增：

- `src/orchestration/agent_context.py`；
- `src/orchestration/agent_loop.py`；
- `src/orchestration/agent_wave_scheduler.py`；
- `tests/orchestration/test_agent_loop.py`、`test_agent_context_builder.py`。

实现test-only assembly：acquire lease、构建上下文/catalog、同binding采样、原子commit sample/reservation、通过Kernel执行wave、
batch闭合后重新采样。连续`parallel_safe=true`形成并发wave，其余call独占；结果按sample ordinal渲染。普通failed/unknown/
aborted不终止Task；waiting停止runner；每个await检查cancel/lease/revision。

显式Skill/MCP只在首轮使用required choice，首轮成功后恢复普通catalog。不得出现`maxTurns`、迭代预算、模型切换、第三方锁或
真实route。

Green gate：tool-result-to-next-sample、3步以上链路、safe/exclusive交错、乱序完成、ordinary error纠错、显式首轮约束、
waiting不提前采样通过。

回滚：删除test-only Loop assembly；Kernel和Agent storage保留。

建议commit：`feat(agent): execute durable multi-call loop`。

### P3-B：同模型compaction与有界上下文

优先新增`src/orchestration/agent_compaction.py`；Context顺序固定为stable rules、safe Tool rules、summary、未覆盖items、当前
trusted facts和final guard。

只有P2 typed preflight允许compaction；summary绑定covered range和源digest，原始items不删除，CAS推进boundary后重新运行
完整catalog preflight。无推进、相同decision无eligible range、retry耗尽或必保segment仍超限均fatal，禁止busy loop。

Green gate：同binding、digest/suffix、crash/CAS、re-preflight、raw/hidden/secret/upload正文排除和失败收敛通过。

回滚：回退compaction模块；未接流量summary测试数据可保留或精确清理。

建议commit：`feat(agent): compact context with fixed model binding`。

### P3-C：唯一`agent.final_output`

优先新增`src/orchestration/agent_final_output.py`和`tests/orchestration/test_agent_final_output.py`；从
`src/capabilities/main_agent/`提取可复用PromptEnvelope、fallback disclosure、download guard、Artifact/Event/history helper，
但此阶段不删除旧finalizer。

Final publisher只消费已持久化、无calls的非空assistant item，使用确定性Node/Artifact/Message/event/receipt ID和Phase 1原子
final操作；不再次调用模型，不进入catalog，不分配Capability instance。

Green gate：final前/后、delta前/后、commit前/后fault matrix无重复；text+calls不入history；超过旧replan/dynamic node阈值的
长轨迹仍完成；Phase 3精确测试目标与main-agent/history回归通过。

回滚：删除test-only publisher与提取的Agent adapter，旧`main_agent.respond`仍为正式入口。

建议commit：`feat(agent): publish atomic final output`。

## 8. Phase 4：Waiting、Continuation、Recovery 与 Cancel

### P4-A：multi-waiting与Continuation Locator

优先新增`src/orchestration/agent_continuation.py`和`src/core/agent_continuation.py`；locator绑定Run/sample/call/Task/Node/
owner/conversation/resume kind/authority digest/pinned bundle/model binding，不保存Tool参数、raw result、credential、附件正文或
未净化用户内容。

把Skill missing input、MCP approval、MRTR/elicitation、remote Task映射为`can_suspend=true` waiting authority；一个wave可有多个
waiting，后续wave不启动。waiting集合与authority同事务提交后才release lease。

Green gate：两个同wave waiting、回答一个不采样、全部闭合后恢复remaining wave、ambiguous Interrupt明确拒绝和waiting期间
零heartbeat/零worker占用通过。

回滚：删除Agent locator/test coordinator；旧DAG continuation保持正式路径。

建议commit：`feat(agent): persist multi-waiting continuations`。

### P4-B：恢复、no-replay与cancel线性化

优先新增`src/lifecycle/agent_run_recovery.py`，修改`interrupt_service.py`、`cancellation_service.py`和MCP窄恢复适配；
Agent resume只由`tests/orchestration/support.py`或API测试support中的显式fixture装配，`src/api/runtime.py`真实route保持不变。

恢复规则：已有权威结果缺Item则幂等补写；有durable MCP authority则继续原状态机；可能已有副作用但结果不确定则写
`aborted`且executor调用数不增加；identity损坏fatal；旧owner晚结果由fencing拒绝。cancel后不启动call、不恢复wave、不采样、
不final，和completion按revision线性化。

Green gate：Skill/MCP approval/MRTR/remote/restart使用原Run/call/binding；duplicate answer/wakeup/outbox幂等；lease takeover、
cancel vs remote completion、late result和startup recovery fault tests通过。

回滚：回退Agent recovery coordinator；保留Phase 1 durable数据，不生成旧WorkflowPlan adapter。

建议commit：`feat(agent): recover waiting runs without replay`。

## 9. Phase 5：API、Frontend 与 Observability 预适配

### P5-A：后端投影、事件与指标

优先新增`src/api/agent_projection.py`和`src/orchestration/agent_observability.py`，修改`dto.py`、`sse.py`、
`routes/tasks.py`及history helper。

- AgentRun到Task/TaskNode/Interrupt保持现有公开状态；waiting仍投影Task `running`；
- `/graph`对Agent fixture返回invocation nodes、固定`required/hard`和`edges=[]`，repository spy证明不读TaskEdge；
- durable Agent事件使用closed低敏字段，`agent.reasoning_delta`只transient；
- metrics只允许closed outcome/kind/reason/phase标签，不含ID、用户、文本或credential；
- final/fallback/download/history live与replay一致。

新建PRD指定的`tests/api/test_agent_task_projection.py`、`tests/observability/test_agent_metrics.py`并扩展SSE/history测试。

Green gate：旧DAG fixture和Agent fixture均通过；真实请求仍无法选择Agent assembly。

回滚：删除Agent-only投影/event/metric分支；旧API语义不变。

建议commit：`feat(api): project agent runs through task contracts`。

### P5-B：Frontend、多waiting与cutover readiness

修改`frontend/src/api/taskEvents.ts`、`domain/taskEvents.ts`、`App.tsx`、approval/progress组件及测试：

- 消费Agent events并安全忽略unknown audit events；
- reasoning只显示transient Agent delta；tool result不当作assistant answer；
- 多waiting按interrupt/node ID逐项展示，回答一个后其余仍在；
- refresh/reconnect从Task graph/history/open Interrupt恢复，不依赖edges；
- event ID去重final replay，payload conflict触发resync；
- 保持focus trap、键盘、语义label、合理焦点恢复和无重复announcement。

创建`docs/prd/backend/unified-agent-loop/cutover-readiness.md`，绑定当前commit并闭合Phase 0～5状态、全部start/resume/
cancel/recovery入口、测试证据、active docs、remaining blockers及Phase 7 schema inventory。

Green gate：Frontend Vitest、typecheck、build和可访问性测试通过；AL-P5-01～10通过；readiness无未知入口。

回滚：删除Agent-only reducer/fixture分支和readiness候选；不改变用户数据。

建议commit：`feat(frontend): consume agent run projections`；readiness可使用独立`docs(agent): record cutover readiness`。

## 10. Phase 6：全入口 Clean Cutover 与 DAG Runtime 删除

### P6-A：冻结最后DAG回滚点

开始条件：Phase 0～5全部`proof_complete`；真实PG/Rust/Frontend证据有效；readiness无未知入口；当前分支为`main`。

任务：

- 运行Phase 0～5完整复验并记录最后DAG commit；
- 验证pre-Phase7 schema能启动最后DAG binary与Agent cutover binary；
- 冻结start/resume/cancel/recovery route和待删除module/test/config/event inventory；
- 审查Phase 6提交序列，明确中间commit不是可运行交付物。

Green gate：具备可整体回滚的DAG checkpoint；任一缺口使Phase 6保持`blocked`。

回滚：本checkpoint只冻结证据和inventory；预检失败时继续运行原DAG，不进入P6-B，也不改变数据或route。

建议commit：不额外制造空commit；将最后通过全量门禁的现有DAG commit记录为rollback checkpoint。

### P6-B：单一受审commit序列完成cutover与删除

切换：

- `src/api/runtime.py`、普通/显式submit、Interrupt answer、MCP continuation worker、remote completion、cancel和startup recovery
  全部调用唯一Agent start/resume/cancel/recover入口；
- `build_api_runtime`只装配AgentLoopOrchestrator、Agent repositories/model port、InvocationService和projection services；
- startup readiness验证全部公开edition与Agent storage contract；
- MCP shadow observation改从真实`mcp.dispatch` invocation hook开始。

删除业务源码与wiring：

- `WorkflowPlan/WorkflowNodePlan`生产依赖、workflow providers/router/expander/validator；
- Planner contract/repair/node identity运行路径、scheduler/DAG loop、CompletionPolicy；
- Runtime/Soft Skill/Main Agent Replanner；
- `main_agent.respond` finalizer壳和第二回答模型调用；
- `max_replans`、`max_dynamic_nodes`配置/DTO/env；
- `mcp_remote_task_continuation_plan`和旧恢复reader；
- planner LLM factory/reasoning wiring、旧reasoning事件生产者/Frontend专属消费分支；
- prompt中的DAG控制面措辞。

保留到Phase 7但零生产读取：TaskEdge physical表/storage/proto、Task.root_node_id、TaskNode DAG-only字段以及DTO固定兼容字段。

旧测试按P0 inventory处置：行为/安全迁移到Agent入口；纯DAG shape测试只在绑定replacement test或无行为合同证据后删除。

Green gate：全部入口spies/E2E只命中Agent；生产runtime/config搜索无DAG/flag/fallback；旧配置不能复活DAG；Skill/MCP/
Interrupt/cancel/history行为回归通过。

回滚：若任一入口或门禁失败，整体回退到P6-A DAG checkpoint和pre-Phase7 schema；不得保留半切换binary。

建议commit序列：

1. `refactor(agent): switch all execution entries to agent loop`
2. `refactor(agent): remove dag runtime control plane`
3. `test(agent): migrate cutover behavior coverage`

上述序列只作为一个受审cutover bundle交付；前两个中间状态不得部署或作为可运行checkpoint。

### P6-C：全量证明、删除报告与文档authority

创建`docs/prd/backend/unified-agent-loop/dag-runtime-deletion-report.md`，记录删除源码/wiring/config/events/tests、替代测试、
零runtime引用扫描、Phase 7待删schema/proto和rollback checkpoint。

更新`docs/prd/README.md`、`backend/00-主代理框架PRD.md`、active PRD inventory、受影响AGENTS和CHANGELOG，使Agent Loop成为
当前基线，旧DAG文档转rewrite/superseded/historical。

运行README全部canonical Backend、Frontend和Rust门禁、全入口E2E、Skill/MCP完整回归、静态扫描及pre/post rollback rehearsal。

Green gate：AL-P6-01～10全部通过，状态标记`cutover_complete`；DAG physical字段仍存在但生产读取为零。

回滚：Phase 7前仍可整体恢复P6-A代码和开发Agent数据边界；不承诺旧代码恢复新Agent Task。

建议commit：`docs(agent): record clean cutover evidence`。

## 11. Phase 7：备份恢复、破坏性删除与最终证明

### P7-A：仓库外备份与隔离恢复硬门禁

在任何migration前：

- 确认根`docker_cmd.md`只做存在/ignored/untracked验证，不读取内容；
- 使用`mktemp -d`或明确仓库外目录生成SQLite、真实PG、Runtime Sidecar备份；目录权限不高于0700，文件不高于0600；
- 记录commit、schema version、digest、created time和脱敏restore reference；
- 在隔离开发目标实际restore并运行readiness/Agent storage/Task history/Artifact/Event smoke；
- 创建`docs/prd/backend/unified-agent-loop/destructive-migration-evidence.md`，不写DSN、credential或绝对敏感路径。

Green gate：三类备份均实际恢复成功；只存在文件、dry-run或打印命令都不算通过。

回滚：restore失败时禁止开始P7-B，保留Phase 6代码/schema并修复备份流程。

建议commit：`docs(agent): record pre-migration restore proof`。

### P7-B：DAG physical schema/proto删除

删除范围：

- `Task.root_node_id`；
- `TaskNode.criticality/dependency_type/retry_policy/timeout_policy/resource_class`持久字段；
- `TaskEdge` model/table/repository/StoragePort/proto/Rust type和全部read/write/list path；
- 任何残留dependency scheduling、planner config和DAG prompt/runtime引用。

同步修改Python core、SQLite migration/bootstrap/repository、PostgreSQL manifest/reconciler/permissions、Runtime Sidecar proto/Rust/
SQLite adapter/gRPC/Python facade、contract golden、API DTO固定投影和Frontend types/tests。Capability内部Skill/MCP/Provider timeout
必须保持，不新增统一外层timeout。

先写migration/restore/parity红测：旧DAG字段确实消失，新AgentRun/Item/Artifact/Event保留，`/graph`固定字段来自DTO且
`edges=[]`，route不读TaskEdge，混合binary/schema拒绝启动。

Green gate：AL-P7-02～06通过；SQLite、真实PG、Sidecar/Rust migration与conformance通过；业务源码零TaskEdge/DAG-only字段。

回滚：Phase 7后只能成对恢复P7-A三backend备份和Phase 6代码，或保持新schema forward fix；禁止只回退代码或反向猜测DAG。

建议commit：`refactor(storage): remove dag physical contracts`。

### P7-C：最终全量、真实MCP与文档收口

运行并记录：

- unified-agent-loop README中的每条canonical Backend命令，且每条discover实际发现非零测试；
- 真实PostgreSQL schema/permission/migration/transaction/lease/concurrency/restore目标；
- Rust `cargo_fmt/cargo_clippy/cargo_test/cargo_deny` required gates；
- Frontend Vitest/typecheck/build；
- 全入口automatic/explicit/waiting/recovery/cancel/final E2E；
- 受控真实MCP discovery、Selector、ordinary Tool、approval或waiting恢复、Result Parser、Artifact和final answer；
- runtime/config/proto/docs静态零引用扫描；
- 总纲12类NFR和FR-1～FR-26的最终证据映射。

真实MCP缺授权时保持`blocked`，除非用户明确书面waiver。证据只记录closed outcome/digest，不含credential/raw result。

更新本目录README和Phase 0～7状态、`docs/AGENTS.md`、`docs/prd/README.md`、`backend/00`、受影响src/tests/frontend/native
AGENTS及CHANGELOG；所有详细证据只引用`destructive-migration-evidence.md`，避免多份正文漂移。

Green gate：AL-P7-01～10、FR-1～26和全部NFR无未批准缺口，目录才能标记`complete`。

回滚：任一最终门禁失败时状态保持`blocked`；若失败涉及post-migration schema兼容，选择forward fix或成对恢复P7-A备份与
Phase 6代码，禁止只回退单一backend或只修改完成状态文档。

建议commit：`docs(agent): close unified loop proof`。

## 12. 测试与证据矩阵

| 证据域 | 首次主责 | 后续强制复验 |
|---|---|---|
| Native tools、required choice、same binding | P0-B | P2-D、P3、P4、P6、P7 |
| Sample/outcome/final原子性 | P1-A/B/C | P3、P4、P6、P7 |
| Task lease heartbeat/fencing/takeover | P1-B/C | P3、P4、P6、P7 |
| Invocation唯一实现 | P2-A | P3、P4、P6、P7 |
| Catalog/Policy/预算 | P2-B | P3、P6、P7 |
| Delegated/executable Skill安全 | P2-C | P3、P4、P6、P7 |
| MCP discovery/auth/result/no-replay | P2-D | P4、P6、P7真实smoke |
| Loop/multi-call/compaction/final | P3 | P4、P5、P6、P7 |
| Multi-waiting/recovery/cancel | P4 | P5、P6、P7 |
| API/SSE/history/graph/events/metrics/a11y | P5 | P6、P7 |
| 单控制面与DAG runtime零引用 | P6 | P7 |
| 备份恢复和physical schema删除 | P7 | 最终完成证据 |

Phase 6/7必须运行PRD README定义的完整canonical命令；任何`Ran 0 tests`、required skip、缺工具或non-zero exit均为失败。
阶段聚焦测试不能替代最终全量门禁。

## 13. 工期与资源建议

以下仅用于排期，假设1名后端主责、1名Storage/Rust主责、1名前端/测试主责可在阶段内部并行，但阶段门禁严格串行：

| 阶段 | 建议工程日 | 主要不确定性 |
|---|---:|---|
| Phase 0 | 3～5 | Provider native delta与公开edition能力差异 |
| Phase 1 | 10～15 | 三backend事务、真实PG权限、Sidecar proto/Rust parity |
| Phase 2 | 9～13 | 从现有DAG抽取Kernel时的行为保持、MCP/Skill安全链 |
| Phase 3 | 8～12 | multi-call wave、compaction与final fault matrix |
| Phase 4 | 6～9 | 多waiting、remote authority、cancel/lease竞态 |
| Phase 5 | 5～8 | 前端恢复、事件冲突、可访问性与readiness inventory |
| Phase 6 | 7～11 | 1.4万行ApiRuntime装配收敛和旧测试迁移/删除 |
| Phase 7 | 6～10 | 真实PG、Sidecar备份恢复、破坏性migration与真实MCP |
| 合计 | 54～83 | 不含外部授权/环境等待和`prod`部署 |

建议按阶段而非日历承诺推进；任何真实环境门禁缺失会停止关键路径。Phase 1、Phase 6和Phase 7应预留双人review窗口。

## 14. 主要风险与停止条件

- Provider无法完整支持roles/native tools/required choice：Phase 0阻断该edition，不能降级text JSON；
- Agent schema不能被旧binary安全忽略：Phase 1停止，不能提前切入口；
- Invocation Kernel抽取造成旧DAG用户行为漂移：回滚P2-A，不能复制第二实现；
- Catalog必保segments超过模型预算：采样前closed fatal，不能裁剪schema或偷偷换模型；
- 不确定副作用恢复需要重放才能继续：Phase 4保持`blocked`，不能弱化no-replay；
- Phase 6发现未知入口或半切换binary：整体回滚到最后DAG checkpoint；
- 备份无法实际恢复：禁止执行Phase 7 destructive migration；
- 实现需要改变产品方向、风险容忍、API支持义务、依赖许可或真实环境waiver：回到用户确认；
- 任意仓库操作可能影响`docker_cmd.md`：停止并先建立/验证仓库外安全备份，不得读取其内容。

## 15. 计划批准后的第一步

用户批准本计划后，只启动P0-A：创建active PRD/test/entry inventory并运行基线测试。P0-A绿灯前不写Agent Model代码；
Phase 0退出前不开始Agent storage；Phase 5 readiness闭合前不进入cutover。每个阶段完成后回报实际commit、通过测试、
未验证项和下一阶段进入条件，不把计划中的预期命令写成已通过。
