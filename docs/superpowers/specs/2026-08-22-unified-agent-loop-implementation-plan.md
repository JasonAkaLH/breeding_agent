# 统一同模型 Agent Loop 实施计划

## 状态与依据

- 日期：2026-08-22
- 分支：`main`
- 状态：document-perfectization三轮自主审查99/100通过；按检查点实施中
- 执行状态：Phase 0、Phase 1均`proof_complete`；P1-A/P1-B/P1-C green（真实隔离PG 32项零skip、真实Rust
  Sidecar进程完成AgentAtomicWriter全链路）；下一检查点P2-A
- 总纲：`docs/prd/backend/unified-agent-loop/00-统一同模型AgentLoop总纲PRD.md`
- 阶段依据：`docs/prd/backend/unified-agent-loop/README.md`及Phase 0～7八份阶段PRD
- 架构依据：`docs/superpowers/specs/2026-08-21-unified-agent-loop-design.md`
- 拆分依据：`docs/superpowers/specs/2026-08-21-unified-agent-loop-prd-decomposition-design.md`
- 业务代码基线：`f707235`；计划审查起点：`7d929b8`。两者之间只有本计划、索引和CHANGELOG变更；正式运行时仍为
  DAG；计划起点只有Phase 0 `AgentModelPort`，当前已完成Phase 1 additive Agent storage，仍无正式route可达的
  `AgentLoopOrchestrator`
- 计划边界：本计划只安排`main`开发仓库实现，不部署`prod`，不迁移或恢复旧DAG Task

本计划把已批准PRD转换为逐文件、逐测试、逐回滚点的green checkpoint。计划不改变同模型、无固定轮次上限、
完整Tool catalog、no-replay、单一Task lease、clean cutover和Phase 7破坏性删除等产品与安全决策。

## 1. 目标、非目标、参与者与代码边界

用户价值保持父PRD不变：长链任务必须能基于真实Tool结果继续选择下一步，等待或审批后恢复原Run/call，普通错误可由
同一模型纠正，最终用户只看到一份由同一模型生成的回答。实施计划的直接目标是把这个结果拆成可独立验证、可回滚且
不会形成双控制面的仓库检查点。

非目标：不新增子Agent、第三方Agent/图/异步锁框架、lazy Tool discovery、异步steer、旧DAG Task reader或迁移器、
统一外层业务timeout、`prod`部署、模型切换或固定轮次上限；不改变MCP transport/discovery/Endpoint Policy/Result Parser和
Skill manifest业务input/output合同。

| 参与者 | 主责 | 必需复核 |
|---|---|---|
| Agent/Orchestration维护者 | P0、P2～P4、P6的model、Kernel、Loop、continuation与cutover | Storage原子边界、Skill/MCP安全、删除inventory |
| Storage/PostgreSQL/Rust维护者 | P1、P7的schema、事务、lease、Sidecar与恢复 | 真实PG权限/并发、proto parity、备份实际可恢复 |
| Skill/MCP/Lifecycle维护者 | P2、P4的适配与authority恢复 | raw/secret不泄漏、no-replay、cancel/late-result |
| API/Frontend/Observability维护者 | P5及P6/7兼容投影 | SSE/history/graph、低基数指标、可访问性 |
| 发布/安全审查者 | P6 clean cutover、P7 destructive boundary | 当前分支、证据commit、回滚工件、required gate无skip |
| 最终用户 | 批准改变产品方向、风险容忍或真实MCP waiver | 不负责补齐自动门禁或替代真实PG/Rust证据 |

当前仓库证据：

| 锚点 | 审查事实 | 计划约束 |
|---|---|---|
| `src/api/runtime.py` | 约14213行，`_run_execution`、Interrupt、MCP continuation/startup recovery和装配集中 | P0先做入口inventory；P6只做装配切换，不把Loop细节搬入该文件 |
| `src/orchestration/service.py` | 约1509行，`_execute_node`混合DAG控制与单次调用生命周期 | P2先提取公共Kernel；P6再删除DAG controller |
| `src/core/contracts.py`、SQLite/PostgreSQL repositories | 现有共享StoragePort与repository体积大 | 新Agent repository使用窄port；不得把新事务继续堆入单一巨型接口 |
| `native/proto/maf/runtime/v1/runtime.proto` | 当前含Task/Node/Edge/lease，无Agent合同 | P1 additive新增；P7才删除Edge/DAG-only字段 |
| `frontend/src/App.tsx`、`domain/taskEvents.ts` | 当前同时依赖Task/SSE/graph和Planner/Skill事件 | P5 additive消费Agent fixture；P6删除旧事件生产/专属消费 |
| `rg` Agent symbols | 审查起点无正式AgentRun/AgentItem/AgentLoop实现 | Phase 0～5必须保持真实route不可达 |

权威设计固定在`src/orchestration/agent_loop/`维持以下七个不可合并职责；实施不得改回扁平
`src/orchestration/agent_*.py`或把它们合进`ApiRuntime`：

| 文件 | 单一职责 |
|---|---|
| `models.py` | AgentRun、AgentItem、AgentSample、AgentToolCall/Result及closed枚举 |
| `model_port.py` | provider-neutral采样、compaction入口和测试fake |
| `tool_catalog.py` | 可见性、provider-safe name、Policy与完整catalog preflight |
| `context.py` | PromptEnvelope、summary、suffix和安全上下文组装 |
| `invocation.py` | 唯一单次调用生命周期、Agent outcome writer和deterministic waves |
| `runner.py` | 唯一Agent Loop状态机、lease/cancel检查点和start/resume入口 |
| `final_output.py` | 唯一final Artifact/Message/event/receipt发布 |

允许增加`repository.py`、`lease.py`、`continuation.py`、`skill_activation.py`和`observability.py`等窄support文件，但不得
复制上述七项职责。`src/core`只保留真正跨模块/Rust共享的常量或codec，不得维护第二套Agent dataclass。

## 2. 完成声明

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

## 3. 执行策略与顺序

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

每个阶段状态变化必须同步本目录README、对应Phase PRD的实际证据/未验证项、`docs/prd/README.md`、
`docs/prd/backend/00-主代理框架PRD.md`、`docs/AGENTS.md`、受影响模块AGENTS和CHANGELOG。所有checkpoint必须记录
License Requirement；本计划默认不新增依赖，若实现证明必须改变依赖或许可，阶段转`blocked`并回到用户确认。

P0-A至P5-B的业务删除范围默认为“无”：只允许移除当前checkpoint自己造成的孤儿import、fixture或未使用窄helper；
不得顺手删除既有DAG源码、测试或schema。P6-B、P6-C和P7-B的删除范围以各节显式inventory为准，超出清单必须先
回到计划审查。

Phase 0～5只允许additive schema、行为保持Kernel抽取和test-only Agent assembly。Phase 6以前不得注册真实Agent route、
请求级feature flag或shadow副作用执行。Phase 6必须在同一受审commit序列完成全入口切换与DAG runtime删除，Phase 7前
不得删除DAG physical schema。

## 4. Checkpoint 总览

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

## 5. Phase 0：基线与 Agent Model Contract

### P0-A：锁定现状和处置清单

目标：建立可比较基线，不写Agent storage，不改变任何用户入口。

文件与任务：

- 新建`docs/prd/backend/unified-agent-loop/active-prd-inventory.md`，双向登记所有命中`WorkflowPlan`、
  `RuntimeReplanner`、`main_agent.respond`、`CompletionPolicy`、`max_replans`、`max_dynamic_nodes`的active文档；
- 新建`scripts/validate_unified_agent_loop_evidence.py`和`tests/scripts/test_unified_agent_loop_evidence_contract.py`，分类
  行为/安全测试、Phase 6迁移测试和纯DAG shape删除候选，并验证README定义的四份handoff文档closed字段、阶段到期规则、
  生产源码/允许historical匹配和tested commit/tree；
- 记录`src/api/runtime.py::_run_execution`、Interrupt answer、MCP continuation worker、startup recovery、cancel、普通/显式
  start route清单；
- 运行Phase 0 PRD列出的现有LLM/model-edition基线测试，并记录任何既有失败。

Green gate：inventory与`rg`发现集双向一致；现有LLM text/title/helper行为有可重放基线。

回滚：仅删除inventory和基线fixture；业务运行时不变。

建议commit：`test(agent): lock unified loop baseline inventory`。

### P0-B：Agent Model contract、adapter与edition gate

优先新增文件：

- `src/orchestration/agent_loop/models.py`：`AgentModelBinding`、`AgentToolDescriptor`、`AgentModelRequest`、`AgentSample`、
  `AgentToolCall`、usage/finish/protocol错误闭合类型；
- `src/orchestration/agent_loop/model_port.py`：provider-neutral sampling port和测试fake；
- `src/integrations/openai_agent_model_adapter.py`：native tools、stream delta组装、provider-safe name和required choice；
- `src/integrations/agent_model_gate.py`：逐公开edition启动能力门禁；
- 修改`llm_client.py`、`llm_runtime.py`、`model_editions.py`，只新增Agent-only入口，保留`generate_text()`兼容路径；
- 新建`tests/integrations/test_agent_model_adapter.py`、`test_agent_model_gate.py`，扩展现有LLM和model edition测试。

每个`ModelEditionOption`必须声明closed Agent capability profile：messages、system/user/assistant/tool roles、native tools、
required choice及streamed tool delta；不支持stream delta但支持同edition非流式Agent sample时可显式声明non-stream fallback。
缺声明或声明不闭合的非默认edition不进入公开列表，默认edition不合格则Runtime readiness fail closed。Gate依靠配置合同和逐edition
wire golden，不在Phase 0增加外部Provider smoke门禁。`AgentModelBinding`只保存edition、reasoning/thinking选项和safe digest，
序列化测试必须拒绝client、base URL、key或provider实例。

协议重试使用单独的`AgentProtocolRetryPolicy`：新增启动期校验的非负配置`agent_protocol_max_retries`，默认1，总尝试上限
为2；每次重试保持同一edition/options且只针对provider contract violation。它与现有transport `max_retries`分开，不作为
Agent turn/Tool调用上限。Run只持久化policy digest、closed attempt count/outcome，不保存raw delta；修改默认值属于运行配置
变更，必须更新配置文档和startup tests，但不允许按请求覆盖。

先写红测：0/1/N calls、交错delta、缺ID、非法/重复name、损坏JSON、text+calls、unknown tool、required choice违规、
cancellation、usage缺失、同edition identity和role/text fallback拒绝。

Green gate：Phase 0 AL-P0-01～10全部通过；不合格edition不公开，默认edition失败时Runtime fail closed；非Agent text路径
无行为回归。

回滚：删除Agent-only contract/adapter/gate，恢复model edition新增声明；不涉及数据。

建议commit：`feat(agent): add native model sampling contract`。

## 6. Phase 1：Agent durable state 与单一 Task lease

### P1-A：Core contract、canonical codec与SQLite

优先新增文件：

- 扩展`src/orchestration/agent_loop/models.py`：`AgentRun`、`AgentItem`、closed status/kind和原子操作输入/结果类型；
- `src/orchestration/agent_loop/repository.py`：窄`AgentRunRepository`与`AgentAtomicWriter`协议，避免继续扩大已有
  `StoragePort`和巨型repository职责；
- `src/storage/agent_payload.py`：严格canonical JSON、131072-byte上限和digest；
- `src/storage/sqlite/agent_repository.py`：Agent表、CAS、sample/result reservation、outcome、waiting、fatal/cancel、final；
- 修改SQLite models/bootstrap/session导出与schema测试，但旧DAG表/字段保持additive不变；
- 新建`tests/orchestration/test_agent_models.py`、`tests/storage/test_agent_storage_conformance.py`、
  `tests/storage/test_agent_storage_sqlite.py`和fault fixtures。

P1-A先冻结精确contract：Task与Run一对一；Run保存fixed model options、sequence/compaction boundary、active sample、
waiting call IDs、next ordinal、claim/revision和终态时间；Item kind只允许`user_message|assistant_message|tool_call|tool_result|
skill_activation|context_summary|continuation`。Canonical JSON必须UTF-8、sorted keys、`ensure_ascii=false`、无多余空白、拒绝
NaN/Infinity并以单个LF结束。`commit_agent_sample`在一个事务中写assistant/calls/TaskNodes/result reservations；
`commit_agent_call_outcome`、waiting、fatal/cancel和`commit_agent_final_output`各自具有唯一CAS入口，调用方不得拼接多次普通save。

先写红测：sequence唯一、result引用、sample前零executor调用、事务all-or-zero、staged Artifact不可见、多个waiting一致性、
deterministic final IDs、canonical 131071/131072/131073 bytes和NaN/Infinity拒绝。

Green gate：SQLite完成AL-P1-01～07；旧binary/schema bootstrap可忽略Agent additive表；没有模型或Capability调用。

回滚：回退additive migration和未接流量repository；开发Agent测试数据可按精确表清理，不转换旧Task。

建议commit：`feat(storage): persist agent run state in sqlite`。

### P1-B：PostgreSQL、Task lease与fencing

文件与任务：

- 新建`src/storage/postgres/agent_repository.py`，同步schema manifest、reconciler和最小权限；
- 新建`src/orchestration/agent_loop/lease.py`，统一SQLite/PostgreSQL/Sidecar `LeaseController`语义；
- 扩展现有`RuntimeLeaseFacade`，复用Task lease，不创建Agent专用lease；
- 新建`tests/storage/test_agent_storage_postgres_integration.py`、`test_agent_task_lease.py`和fake-clock/fault matrix。

Lease TTL必须为启动期校验的正值；active sample、compaction、capability wave和final publish的成功heartbeat间隔不得晚于
TTL/3。生产expiry只使用storage/Sidecar权威时钟；renew旋转token/revision，outcome commit读取最新fencing token；waiting
authority先提交再release，waiting期间不heartbeat，resume重新acquire。

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

实施证据：P1-C新增实现`AgentAtomicWriter`业务合同的Python Sidecar repository；Rust内存核与SQLite adapter在同一CAS事务
写AgentRun/AgentItem、Task、TaskNode、Artifact及final Message/Event/receipt投影，reserved result可按immutable identity转为
waiting或terminal，orphan/重复result fail closed。真实Rust Sidecar进程完成sample、双call、waiting staged Artifact不可见、
resume outcome、唯一final、投影读取及final retry；P1-C Python canonical 57项、统一Rust fmt/clippy/test gate、Phase 1聚焦44项、
core 46项和storage 398项通过。storage discover的7项skip均为未注入外部PostgreSQL DSN的既有真实环境套件；Agent必需PG
证据仍引用P1-B同一代码基线上的隔离PG 32项零skip，不把本次skip记为通过。Phase 1据此`proof_complete`，正式入口仍保持DAG，
下一检查点P2-A。

## 7. Phase 2：Invocation Kernel、Catalog、Skill 与 MCP

### P2-A：行为保持地提取唯一 Invocation Kernel

优先新增`src/orchestration/agent_loop/invocation.py`和窄`InvocationCommitPort`；从`service.py::_execute_node`抽取route authority、
instance selection、Node start CAS、`CapabilityExecutionRequest`、CompositeExecutor调用、Artifact/Event/Interrupt处理、结果分类和
Phase 1 atomic outcome调用。

Kernel不得按DAG/Agent分支。Agent fixture注入唯一`AgentAtomicWriter`；旧DAG在pre-cutover期间通过临时
`src/orchestration/agent_loop/legacy_dag_adapter.py`实现同一commit port，只保存现有Task/Node/Artifact/Event语义。该adapter
不是第二份调用生命周期，不得被Agent入口引用，并在P6-B强制删除。

新建`tests/orchestration/test_agent_invocation.py`并先写spy/golden红测，固定旧DAG在Task/Node/Artifact/Event/Interrupt/
cancel/late-result上的可见行为。旧DAG改为调用公共Kernel，
但scheduler、Planner和入口仍不变。不得复制一份Agent专用executor lifecycle。

Green gate：旧DAG behavior-preservation回归通过；静态/spy证明只有一个invocation lifecycle。

回滚：独立回退Kernel抽取，恢复原`service.py`私有生命周期；Agent storage保留未接流量。

建议commit：`refactor(orchestration): extract capability invocation kernel`。

### P2-B：Tool Catalog、Policy与完整预算preflight

优先新增：

- `src/orchestration/agent_loop/tool_catalog.py`；
- `tests/orchestration/test_agent_tool_catalog.py`、`test_agent_catalog_preflight.py`。

新增不依赖`WorkflowPlan`或`OrchestrationRequest`的`CapabilityVisibilityContext`；`CapabilityRegistry.list_for_request()`在
pre-cutover只作为legacy adapter委托给该安全视图，P6-B删除legacy签名。把当前planner payload policy中仍有效的字段
allowlist/system override迁入唯一`CapabilityInvocationPolicy`；所有Capability默认
`parallel_safe=false`，无policy、private、disabled、跨owner项不进入catalog。Outer Agent只看到public Skills与一个
`mcp.dispatch`。

Visibility context只包含已认证owner scope、execution path、pinned Skill bundle、当前安全MCP Server Profiles和请求级公开
capability allowlist，不包含用户可写metadata。Policy精确包含`model_allowed_fields`、`input_schema`、
`system_payload_factory`、`parallel_safe`和`can_suspend`；执行前必须再次过滤/schema校验并由system值覆盖同名模型字段。

Preflight只返回`fits|history_compaction_required|fatal_required_segments_too_large`，计入完整schema和必保规则，不做summary、
不裁剪schema、不实现lazy discovery。

Green gate：可见性、prompt injection、热重载、完整schema预算、no-schema-leak和三种closed decision通过。

回滚：删除Agent catalog/preflight，旧DAG继续使用公共Kernel。

建议commit：`feat(agent): build policy-filtered tool catalog`。

### P2-C：delegated activation与executable Skill

优先新增`src/orchestration/agent_loop/skill_activation.py`、`tests/orchestration/test_agent_skill_activation.py`和
`tests/capabilities/skill_tool/test_executor.py`；只消费pinned revision的
`PublicSkillProfile.to_dict()`安全投影，resource index只保留公开元数据，写`skill_activation` item和digest，永不执行脚本。

修改`src/capabilities/skill_tool/executor.py`及Skill wiring，使`python_subprocess/platform_service`继续走现有Executor，模型字段
经过allowlist，用户/附件/revision由system覆盖；三种answer mode都只返回tool result，不创建Agent finalizer。

Green gate：manifest body/resource正文/path/config/secret leak scan、pinned revision、脚本零调用、三answer mode和可信上下文
覆盖通过。

回滚：删除Agent activation adapter，旧DAG Skill行为保持。

建议commit：`feat(agent): adapt delegated and executable skills`。

### P2-D：Run-bound MCP model binding与安全链

修改`src/capabilities/mcp_dispatch/server_router.py`、`selector.py`和窄runtime装配，并新建
`tests/orchestration/test_agent_mcp_binding.py`，使Router/Selector从Run解析Phase 0 binding；
不得读取请求中的替代edition。复用现有discovery、authorization、approval、MRTR/Tasks、Result Parser、durable result、
call budget和no-replay实现，不改变transport或协议版本。

先写ordinary、approval、remote/restart的binding identity测试，以及显式server system override、当前Profile allowlist、raw result/
Server Tool list不进入AgentItems测试。

Green gate：AL-P2-01～09、Skill/MCP完整安全回归和旧DAG Kernel行为保持通过；Agent adapter仍仅由test fixture调用。

回滚：回退Run binding注入和Agent MCP adapter，不触碰现有MCP authority数据。

建议commit：`feat(agent): bind mcp routing to agent runs`。

## 8. Phase 3：核心 Loop、Compaction 与 Final Output

### P3-A：ContextBuilder、Loop状态机与deterministic waves

优先新增：

- `src/orchestration/agent_loop/context.py`；
- `src/orchestration/agent_loop/runner.py`；
- 扩展`src/orchestration/agent_loop/invocation.py`实现deterministic waves，不建立第二个调度控制面；
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

扩展`src/orchestration/agent_loop/context.py`并新建`tests/orchestration/test_agent_compaction.py`；Context顺序固定为
stable rules、safe Tool rules、summary、未覆盖items、当前trusted facts和final guard。

只有P2 typed preflight允许compaction；summary绑定covered range和源digest，原始items不删除，CAS推进boundary后重新运行
完整catalog preflight。结构化compaction重试复用P0-B的同binding协议重试policy；无推进、相同decision无eligible range、
retry耗尽或必保segment仍超限均fatal，禁止busy loop。

Green gate：同binding、digest/suffix、crash/CAS、re-preflight、raw/hidden/secret/upload正文排除和失败收敛通过。

回滚：回退compaction模块；未接流量summary测试数据可保留或精确清理。

建议commit：`feat(agent): compact context with fixed model binding`。

### P3-C：唯一`agent.final_output`

优先新增`src/orchestration/agent_loop/final_output.py`和`tests/orchestration/test_agent_final_output.py`；从
`src/capabilities/main_agent/`提取可复用PromptEnvelope、fallback disclosure、download guard、Artifact/Event/history helper，
但此阶段不删除旧finalizer。

Final publisher只消费已持久化、无calls的非空assistant item，使用确定性Node/Artifact/Message/event/receipt ID和Phase 1原子
final操作；不再次调用模型，不进入catalog，不分配Capability instance。

Green gate：final前/后、delta前/后、commit前/后fault matrix无重复；text+calls不入history；超过旧replan/dynamic node阈值的
长轨迹仍完成；Phase 3精确测试目标与main-agent/history回归通过。

回滚：删除test-only publisher与提取的Agent adapter，旧`main_agent.respond`仍为正式入口。

建议commit：`feat(agent): publish atomic final output`。

## 9. Phase 4：Waiting、Continuation、Recovery 与 Cancel

### P4-A：multi-waiting与Continuation Locator

优先新增`src/orchestration/agent_loop/continuation.py`；locator绑定Run/sample/call/Task/Node/
owner/conversation/resume kind/authority digest/pinned bundle/model binding，不保存Tool参数、raw result、credential、附件正文或
未净化用户内容。

把Skill missing input、MCP approval、MRTR/elicitation、remote Task映射为`can_suspend=true` waiting authority；一个wave可有多个
waiting，后续wave不启动。waiting集合与authority同事务提交后才release lease。

Green gate：两个同wave waiting、回答一个不采样、全部闭合后恢复remaining wave、ambiguous Interrupt明确拒绝和waiting期间
零heartbeat/零worker占用通过。

回滚：删除Agent locator/test coordinator；旧DAG continuation保持正式路径。

建议commit：`feat(agent): persist multi-waiting continuations`。

### P4-B：恢复、no-replay与cancel线性化

优先新增`src/lifecycle/agent_run_recovery.py`、`tests/lifecycle/test_agent_run_recovery.py`和
`tests/api/test_agent_continuation.py`，修改`interrupt_service.py`、`cancellation_service.py`和MCP窄恢复适配；
Agent resume只由`tests/orchestration/support.py`或API测试support中的显式fixture装配，`src/api/runtime.py`真实route保持不变。

恢复规则：已有权威结果缺Item则幂等补写；有durable MCP authority则继续原状态机；可能已有副作用但结果不确定则写
`aborted`且executor调用数不增加；identity损坏fatal；旧owner晚结果由fencing拒绝。cancel后不启动call、不恢复wave、不采样、
不final，和completion按revision线性化。

owner/conversation/task/node/call/digest任一不匹配时不得修改waiting集合；Run已terminal时duplicate continuation只返回同一终态；
lease reacquire失败时保持waiting并由当前owner处理。Storage/outcome commit未成功前不得ack外部answer/outbox/remote completion，
避免外部authority已消费但Agent result缺失。

Green gate：Skill/MCP approval/MRTR/remote/restart使用原Run/call/binding；duplicate answer/wakeup/outbox幂等；lease takeover、
cancel vs remote completion、late result和startup recovery fault tests通过。

回滚：回退Agent recovery coordinator；保留Phase 1 durable数据，不生成旧WorkflowPlan adapter。

建议commit：`feat(agent): recover waiting runs without replay`。

## 10. Phase 5：API、Frontend 与 Observability 预适配

### P5-A：后端投影、事件与指标

优先新增`src/api/agent_projection.py`和`src/orchestration/agent_loop/observability.py`，修改`dto.py`、`sse.py`、
`routes/tasks.py`及history helper。

- AgentRun到Task/TaskNode/Interrupt保持现有公开状态；waiting仍投影Task `running`；
- `/graph`对Agent fixture返回invocation nodes、固定`required/hard`和`edges=[]`，repository spy证明不读TaskEdge；
- durable Agent事件使用closed低敏字段，`agent.reasoning_delta`只transient；
- metrics只允许closed outcome/kind/reason/phase标签，不含ID、用户、文本或credential；
- final/fallback/download/history live与replay一致。

指标名称和含义必须逐项实现源设计第15.2节的active/total/time-to-final/sample/tool-call/run distribution/waiting/resume/
lease/compaction/aborted/final-publish集合；保留`BackpressureGuard`的30 active Task边界，waiting不占model/capability worker。

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

## 11. Phase 6：全入口 Clean Cutover 与 DAG Runtime 删除

### P6-A：冻结最后DAG回滚点

开始条件：Phase 0～5全部`proof_complete`；真实PG/Rust/Frontend证据有效；readiness无未知入口；当前分支为`main`。
工作树与index必须无未归属修改；若存在用户变更，必须在原位保留并把cutover标记`blocked`，不得stash、reset或清理绕过。

任务：

- 运行Phase 0～5完整复验并在`cutover-readiness.md`记录tested DAG commit/tree和命令摘要；
- 使用`git archive`生成最后DAG与Agent cutover候选的clean archive，在仓库外临时目录和隔离数据库副本验证
  pre-Phase7 schema；不得切换、reset、stash或清理当前工作树；
- 冻结start/resume/cancel/recovery route和待删除module/test/config/event inventory；
- 审查Phase 6提交序列，明确中间commit不是可运行交付物。

Green gate：具备可整体回滚的DAG checkpoint；任一缺口使Phase 6保持`blocked`。

回滚：本checkpoint只冻结证据和inventory；预检失败时继续运行原DAG，不进入P6-B，也不改变数据或route。

建议commit：`docs(agent): freeze pre-cutover rollback checkpoint`；该docs-only commit记录已测试的DAG代码commit/tree，
自身仍是可运行DAG检查点。

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
- `agent_loop/legacy_dag_adapter.py`、接收`OrchestrationRequest`的`CapabilityRegistry.list_for_request`兼容签名和
  legacy planner payload policy adapter；
- planner LLM factory/reasoning wiring、旧reasoning事件生产者/Frontend专属消费分支；
- prompt中的DAG控制面措辞。

保留到Phase 7但零生产读取：TaskEdge physical表/storage/proto、Task.root_node_id、TaskNode DAG-only字段以及DTO固定兼容字段。

旧测试按P0 inventory处置：行为/安全迁移到Agent入口；纯DAG shape测试只在绑定replacement test或无行为合同证据后删除。
新增`tests/e2e/test_agent_loop_cutover.py`覆盖P6入口inventory的普通/显式/continuation/cancel/recovery全部分支。

Green gate：全部入口spies/E2E只命中Agent；生产runtime/config搜索无DAG/flag/fallback；旧配置不能复活DAG；Skill/MCP/
Interrupt/cancel/history行为回归通过。

回滚：若任一入口或门禁失败，停止新Task并整体恢复P6-A DAG checkpoint和pre-Phase7 schema；已提交bundle只能用正常
`git revert`生成可审查回滚commit，不移动分支指针、不使用reset/checkout覆盖工作树。不得保留半切换binary；新Agent开发
Task不承诺由旧代码恢复。

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
回滚演练只使用P6-A clean archive和隔离数据库副本，不移动当前分支、不复活旧Task、不触碰`docker_cmd.md`。

Green gate：AL-P6-01～10全部通过，状态标记`cutover_complete`；DAG physical字段仍存在但生产读取为零。

回滚：Phase 7前仍可整体恢复P6-A代码和开发Agent数据边界；不承诺旧代码恢复新Agent Task。

建议commit：`docs(agent): record clean cutover evidence`。

## 12. Phase 7：备份恢复、破坏性删除与最终证明

### P7-A：仓库外备份与隔离恢复硬门禁

先新增`src/storage/agent_schema_migration.py`、`scripts/migrate_unified_agent_loop_schema.py`和
`tests/scripts/test_migrate_unified_agent_loop_schema.py`。operator使用closed
`report -> backup -> restore-check -> apply`状态机；每一步绑定canonical report/backup-set SHA，禁止绕过前置步骤。

Operator在仓库外state root使用单实例文件锁和immutable no-clobber receipts；PostgreSQL阶段同时持有专用advisory lock，
SQLite/Sidecar writer必须已quiesced。Closed前缀只允许
`reported -> backed_up -> restore_verified -> applying_sqlite -> sqlite_applied -> applying_postgres -> postgres_applied ->
applying_sidecar -> sidecar_applied -> verified -> completed`。
相同input digest的duplicate invocation幂等返回当前receipt；缺失前缀、非法跳转、不同digest、锁丢失或已存在冲突receipt均
fail closed。每个receipt包含前驱file/payload SHA、tested commit/tree、schema versions、backup-set SHA和UTC时间，使用
O_EXCL/no-clobber及file+directory fsync；不包含业务正文、credential、DSN或绝对敏感路径。

在任何migration前：

- 确认根`docker_cmd.md`只做存在/ignored/untracked验证，不读取内容；
- drain当前开发环境的新Task并停止SQLite/Sidecar writer；PostgreSQL使用一致性snapshot；不能证明quiesced/snapshot时fail closed；
- `report`只读枚举三个backend的schema version、待删对象、新Agent数据计数/digest和blocker，输出canonical report SHA；
- `backup --expected-report-sha`在明确的仓库外持久backup root生成SQLite online backup、PostgreSQL custom-format dump和
  Runtime Sidecar一致性备份；backup root不高于0700、普通文件不高于0600，使用O_EXCL/no-clobber、拒绝symlink/
  多hard-link/owner或mode漂移并执行file+directory fsync；
- backup manifest记录tested commit/tree、schema version、file digest/size/mode、created time和脱敏restore reference；不在
  argv、stdout、manifest或文档输出DSN/credential/绝对敏感路径；PostgreSQL DSN只从既有测试环境读取；
- `restore-check --expected-backup-set-sha`在全新隔离SQLite/PG/Sidecar目标实际恢复，运行readiness、Agent storage、Task history、
  Artifact/Event和old-DAG-not-readable smoke；不得连接或覆盖原数据；
- 备份集至少保留到P7-C全部通过和用户明确结束rollback窗口；`mktemp -d`只允许承载可删除的restore临时目标，不得作为唯一
  rollback备份位置；
- 创建`docs/prd/backend/unified-agent-loop/destructive-migration-evidence.md`，不写DSN、credential或绝对敏感路径。

先写红测：report drift、expected SHA不匹配、已存在文件、symlink/hard-link/mode/owner漂移、fsync失败、PG/Sidecar部分失败、
restore指向原数据、输出泄密和duplicate exact retry。

Green gate：三类备份均实际恢复成功；只存在文件、dry-run或打印命令都不算通过。

回滚：restore失败时禁止开始P7-B，保留Phase 6代码/schema并修复备份流程。

建议commit序列：`feat(storage): add agent schema migration operator`，随后
`docs(agent): record pre-migration restore proof`。两个commit共同构成P7-A，未取得restore proof时不得进入P7-B。

### P7-B：DAG physical schema/proto删除

删除范围：

- `Task.root_node_id`；
- `TaskNode.criticality/dependency_type/retry_policy/timeout_policy/resource_class`持久字段；
- `TaskEdge` model/table/repository/StoragePort/proto/Rust type和全部read/write/list path；
- 任何残留dependency scheduling、planner config和DAG prompt/runtime引用。

同步修改Python core、SQLite migration/bootstrap/repository、PostgreSQL manifest/reconciler/permissions、Runtime Sidecar proto/Rust/
SQLite adapter/gRPC/Python facade、contract golden、API DTO固定投影和Frontend types/tests。Capability内部Skill/MCP/Provider timeout
必须保持，不新增统一外层timeout。

破坏性执行只能调用P7-A operator的`apply --expected-report-sha --expected-backup-set-sha`；operator先复验当前commit/tree、
schema/data inventory、backup文件digest和restore receipt，任一漂移都拒绝。SQLite、PostgreSQL和Sidecar按版本绑定顺序切换；
任一backend失败时不得启动post-migration binary，必须按P7-A备份成对恢复全部backend和Phase 6代码，不能继续使用混合schema。

固定apply顺序为SQLite、PostgreSQL、Runtime Sidecar；这只是离线迁移顺序，不表示三者可混合运行。每个backend开始前发布
`applying_*`receipt，事务提交并复验目标schema/data digest后发布对应`*_applied`receipt，再进入下一backend。若进程在
`applying_*`后退出，恢复者只允许检查精确target digest：完全匹配则补发同一确定性`*_applied`receipt；不匹配或无法证明时
必须`restore-all`，不得重跑未知migration。其他任一失败也只报告精确前缀并要求restore-all，禁止从中间backend继续apply。
`restore-all --expected-backup-set-sha`固定恢复Sidecar、PostgreSQL、SQLite，随后
复验三者均为pre-migration schema/data digest并发布`restored`receipt；只有全部恢复成功才允许重新生成report/backup/restore
proof后再次apply。全部migration完成后必须先运行post-migration contract/data digest和新binary readiness，再发布
`verified -> completed`；失败同样执行restore-all或保持停机forward fix，不能启动混合binary。

先写migration/restore/parity红测：旧DAG字段确实消失，新AgentRun/Item/Artifact/Event保留，`/graph`固定字段来自DTO且
`edges=[]`，route不读TaskEdge，混合binary/schema拒绝启动；另覆盖每个receipt前后的崩溃、锁竞争/丢失、非法前缀、
partial apply禁止续跑、restore-all三backend部分失败和完整恢复后重新report。

Green gate：AL-P7-02～06通过；SQLite、真实PG、Sidecar/Rust migration与conformance通过；业务源码零TaskEdge/DAG-only字段。

回滚：Phase 7后只能成对恢复P7-A三backend备份并以正常revert commit恢复Phase 6代码，或保持新schema forward fix；禁止只
回退代码、移动分支指针或反向猜测DAG。

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

## 13. FR、阶段验收与 Checkpoint 追踪

下表中的范围均为闭区间；例如`AL-P0-01～10`精确包含01至10，不允许省略中间ID。P7-C再次复验全部FR/NFR，
不改变原主责归属。

| Checkpoint | 主责FR/交接 | 阶段验收ID |
|---|---|---|
| P0-A | active PRD/test/entry inventory，Phase 0进入证据 | inventory双向集合合同 |
| P0-B | FR-2、FR-3、FR-19 | AL-P0-01～10 |
| P1-A | FR-17的model/SQLite/canonical部分 | AL-P1-01～07 |
| P1-B | FR-17、FR-26的PG/lease/fencing部分 | AL-P1-01～10在真实PG |
| P1-C | FR-17、FR-26三backend parity | AL-P1-01～10在Sidecar/Rust并跨backend复验 |
| P2-A | 唯一invocation lifecycle与旧DAG行为保持 | AL-P2-01、AL-P2-09 |
| P2-B | FR-24、catalog/policy/preflight | AL-P2-02～04 |
| P2-C | FR-15、FR-16 | AL-P2-05～06 |
| P2-D | FR-13、FR-20 | AL-P2-07～08，并复验AL-P2-01～09 |
| P3-A | FR-4～7、FR-10 | AL-P3-01～06、AL-P3-10 |
| P3-B | FR-11 | AL-P3-07 |
| P3-C | FR-12、FR-23 | AL-P3-08～09，并复验AL-P3-01～10 |
| P4-A | FR-18 | AL-P4-01～03、AL-P4-09 |
| P4-B | FR-8、FR-9 | AL-P4-04～10，并复验AL-P4-01～10 |
| P5-A | FR-21、FR-22的后端投影 | AL-P5-01～06、AL-P5-09 |
| P5-B | FR-21、FR-22的Frontend/a11y/readiness | AL-P5-07～08、AL-P5-10，并复验AL-P5-01～10 |
| P6-A | Phase 6全部进入条件和最后DAG rollback authority | readiness closed合同 |
| P6-B | FR-1、FR-14的入口/源码/wiring切换 | AL-P6-01～07、AL-P6-09 |
| P6-C | FR-1、FR-14的文档/回滚/完整证明 | AL-P6-08、AL-P6-10，并复验AL-P6-01～10 |
| P7-A | 三backend实际备份恢复 | AL-P7-01 |
| P7-B | FR-25和physical schema/proto删除 | AL-P7-02～06 |
| P7-C | FR-1～26与12类NFR最终集成证明 | AL-P7-07～10，并复验AL-P7-01～10 |

## 14. 每个 Checkpoint 的最小 Green 命令

每个checkpoint除下表命令外，都必须运行
`conda run -n multi_agent python -m compileall -q src tests`和`git diff --check`。Phase退出还必须运行对应阶段PRD列出的
discover域；P6/P7必须逐条运行目录README的全部canonical命令。未来目标模块不存在、`Ran 0 tests`、required skip、缺工具
或non-zero exit均为失败。

| Checkpoint | 最小聚焦命令 |
|---|---|
| P0-A | `conda run -n multi_agent python -m unittest tests.scripts.test_unified_agent_loop_evidence_contract tests.integrations.test_llm_client tests.integrations.test_llm_runtime tests.api.test_model_edition_selection` |
| P0-B | `conda run -n multi_agent python -m unittest tests.integrations.test_agent_model_adapter tests.integrations.test_agent_model_gate tests.integrations.test_llm_client tests.integrations.test_llm_runtime tests.integrations.test_llm_request_options tests.api.test_model_edition_selection` |
| P1-A | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_models tests.storage.test_agent_storage_sqlite tests.storage.test_agent_storage_conformance` |
| P1-B | `conda run -n multi_agent python -m unittest tests.storage.test_agent_storage_postgres_integration tests.storage.test_agent_task_lease tests.storage.test_postgres_runtime_schema_manifest tests.storage.test_postgres_schema_reconciler`；必须配置`MAF_POSTGRES_TEST_DSN`且测试不得skip |
| P1-C | `conda run -n multi_agent python -m unittest tests.integrations.test_runtime_sidecar_grpc_client tests.storage.test_rust_runtime_sidecar_contract tests.storage.test_agent_storage_conformance tests.storage.test_runtime_sidecar_agent_repository`；另运行`conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt --only cargo_clippy --only cargo_test` |
| P2-A | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_invocation tests.orchestration.test_fake_capability_flow tests.orchestration.test_mcp_route_handoff_service tests.lifecycle.test_task_cancellation` |
| P2-B | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_tool_catalog tests.orchestration.test_agent_catalog_preflight tests.orchestration.test_registry_scheduler tests.orchestration.test_prompt_envelope` |
| P2-C | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_skill_activation tests.capabilities.skill_tool.test_executor tests.integrations.agent_skills.test_public_skill_profile tests.integrations.agent_skills.test_execution` |
| P2-D | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_mcp_binding tests.capabilities.mcp_dispatch.test_selector_router_executor tests.orchestration.test_mcp_dispatch_resume_v2`，随后`conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'` |
| P3-A | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_loop tests.orchestration.test_agent_context_builder` |
| P3-B | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_context_builder tests.orchestration.test_agent_compaction` |
| P3-C | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_final_output tests.api.test_main_agent_llm tests.api.test_task_query tests.capabilities.main_agent.test_conversation_memory_prompt` |
| P4-A | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_continuation tests.lifecycle.test_agent_run_recovery` |
| P4-B | `conda run -n multi_agent python -m unittest tests.orchestration.test_agent_continuation tests.lifecycle.test_agent_run_recovery tests.api.test_agent_continuation tests.lifecycle.test_task_cancellation`，随后运行`conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'` |
| P5-A | `conda run -n multi_agent python -m unittest tests.api.test_agent_task_projection tests.api.test_task_query tests.api.test_task_events_sse tests.api.test_main_agent_llm tests.observability.test_agent_metrics` |
| P5-B | 在`frontend/`工作目录逐条运行`npm test -- --run`、`npm run typecheck`、`npm run build`；后端运行`conda run -n multi_agent python -m unittest tests.scripts.test_unified_agent_loop_evidence_contract` |
| P6-A | README canonical Backend/Frontend/Rust全集，加`conda run -n multi_agent python -m unittest tests.scripts.test_unified_agent_loop_evidence_contract`；在clean archive执行DAG与Agent候选startup smoke |
| P6-B | `conda run -n multi_agent python -m unittest tests.e2e.test_agent_loop_cutover tests.api.test_agent_continuation tests.lifecycle.test_agent_run_recovery`，随后运行README canonical全集和`conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| P6-C | README canonical全集、Frontend三门禁、required Rust gates、全入口E2E、rollback clean-archive rehearsal，以及`conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| P7-A | `conda run -n multi_agent python -m unittest tests.scripts.test_migrate_unified_agent_loop_schema tests.storage.test_agent_storage_postgres_integration tests.storage.test_rust_runtime_sidecar_contract`；随后依次运行`conda run -n multi_agent python scripts/migrate_unified_agent_loop_schema.py report --state-root "$AGENT_SCHEMA_STATE_ROOT" --output "$AGENT_SCHEMA_REPORT_PATH"`、`conda run -n multi_agent python scripts/migrate_unified_agent_loop_schema.py backup --state-root "$AGENT_SCHEMA_STATE_ROOT" --report "$AGENT_SCHEMA_REPORT_PATH" --expected-report-sha "$AGENT_SCHEMA_REPORT_SHA" --backup-root "$AGENT_SCHEMA_BACKUP_ROOT"`和`conda run -n multi_agent python scripts/migrate_unified_agent_loop_schema.py restore-check --state-root "$AGENT_SCHEMA_STATE_ROOT" --backup-manifest "$AGENT_SCHEMA_BACKUP_MANIFEST_PATH" --expected-backup-set-sha "$AGENT_SCHEMA_BACKUP_SET_SHA" --restore-root "$AGENT_SCHEMA_RESTORE_ROOT"` |
| P7-B | `conda run -n multi_agent python -m unittest tests.storage.test_agent_schema_destructive_migration tests.storage.test_agent_storage_postgres_integration tests.storage.test_postgres_runtime_schema_manifest tests.storage.test_postgres_schema_reconciler tests.storage.test_rust_runtime_sidecar_contract`；只有P7-A proof有效时运行`conda run -n multi_agent python scripts/migrate_unified_agent_loop_schema.py apply --state-root "$AGENT_SCHEMA_STATE_ROOT" --report "$AGENT_SCHEMA_REPORT_PATH" --expected-report-sha "$AGENT_SCHEMA_REPORT_SHA" --backup-manifest "$AGENT_SCHEMA_BACKUP_MANIFEST_PATH" --expected-backup-set-sha "$AGENT_SCHEMA_BACKUP_SET_SHA" --restore-receipt "$AGENT_SCHEMA_RESTORE_RECEIPT_PATH"`；失败后只允许运行`conda run -n multi_agent python scripts/migrate_unified_agent_loop_schema.py restore-all --state-root "$AGENT_SCHEMA_STATE_ROOT" --backup-manifest "$AGENT_SCHEMA_BACKUP_MANIFEST_PATH" --expected-backup-set-sha "$AGENT_SCHEMA_BACKUP_SET_SHA"` |
| P7-C | README canonical全集；真实PG目标；`conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt --only cargo_clippy --only cargo_test --only cargo_deny`；Frontend三门禁；受控真实MCP smoke；`conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 7 --require-closed` |

表中的Frontend命令按列出顺序逐条执行并分别记录exit code，不能因末条成功掩盖前序失败。
`AGENT_SCHEMA_*`变量是任务专用执行变量，运行前必须解析为非空、精确、已复验的仓库外路径或SHA；不得使用未解析变量，
不得把变量值写入仓库文档或命令日志。Operator输出只记录脱敏basename/digest和closed状态。

## 15. 固定 Handoff 证据合同

| 产物 | 首次生成/闭合 | 实施计划验证责任 | 下游阻断规则 |
|---|---|---|---|
| `active-prd-inventory.md` | P0-A；P6-C最终处置 | evidence contract test双向比较`rg`发现集、disposition、owner phase、status和命令 | 漏项、重复项、active旧DAG authority或命令不可执行即阻断 |
| `cutover-readiness.md` | P5-B；P6-A冻结 | 校验tested commit/tree、Phase 0～5、全部入口、测试、docs、blockers和physical inventory | 当前code tree或入口inventory漂移即失效 |
| `dag-runtime-deletion-report.md` | P6-C | 校验删除项、replacement tests、允许的historical匹配、零生产引用、rollback checkpoint和P7 inventory | 任一生产引用或未绑定替代测试即不得`cutover_complete` |
| `destructive-migration-evidence.md` | P7-A开始；P7-C闭合 | 校验backup-set/restore receipts、三backend migration、full gates、static scan、MCP smoke/waiver和NFR/FR映射 | 缺真实PG、restore、required gate或MCP证据/waiver即保持`blocked` |

四份Markdown必须包含README规定字段和closed status，且不得记录credential、DSN、raw result、用户正文、绝对敏感路径或
`docker_cmd.md`内容。`tests/scripts/test_unified_agent_loop_evidence_contract.py`从P0-A起逐阶段扩展；允许未来产物尚不存在时仅在
其producer阶段之前报告`not_due`，到达producer阶段后缺失必须失败，不能以`pending`通过下游门禁。

## 16. NFR 测试与证据矩阵

| NFR | 主责checkpoint | 后续强制复验与最终证据 |
|---|---|---|
| Provider与同模型 | P0-B | P2-D/P3/P4 binding spy；P6/7公开edition startup gate |
| 一致性/原子性 | P1-A～C | P3/P4 fault matrix；P6/7三backend真实路径 |
| 安全/隐私 | P2-B～D | P3 context、P4 locator、P5 event/history/metric leak scan；P7真实MCP安全摘要 |
| Tool catalog | P2-B | P3 compaction/re-preflight；P6/7全入口overflow-before-sample |
| 上下文 | P3-B | 原始items保留、digest/suffix/CAS/restart；P4 resume、P6/7真实入口 |
| 性能/资源 | P3-A、P4-A | deterministic wave、无busy polling、waiting零worker、30 Task backpressure和P5低基数分布指标 |
| Final唯一性 | P3-C | P5 live/replay/history；P6/7 crash与唯一Artifact/Message/event/receipt |
| 恢复/no-replay | P4-A/B | P5投影、P6全入口、P7 real MCP approval/waiting恢复 |
| 可观测性 | P5-A | durable/transient事件区分、完整指标名、低基数label；P6/7全入口复验 |
| API/Frontend兼容 | P5-A/B | P6/7 Task/SSE/Interrupt/history和empty-edge graph全量 |
| 可访问性 | P5-B | P6/7 focus、keyboard、semantics、refresh announcement测试 |
| 可维护性/单控制面 | P2-A、P6-B/C | P7 package职责、单Kernel/outcome、无DAG/flag/fallback静态证明 |

## 17. 工期、资源和重估规则

以下是低置信度初始工程量，不是交付承诺。假设1名Agent/Backend主责、1名Storage/Rust主责、1名Frontend/测试主责可在
阶段内部并行，外部Provider、真实PG、Rust runner和MCP授权可按门禁获得；不含外部等待、`prod`部署或新增许可审查。

| 阶段 | 初始工程日 | 主要不确定性 |
|---|---:|---|
| Phase 0 | 3～5 | Provider native delta与公开edition能力差异 |
| Phase 1 | 10～15 | 三backend事务、真实PG权限、Sidecar proto/Rust parity |
| Phase 2 | 9～13 | Kernel行为保持、visibility/policy去DAG依赖、MCP/Skill安全链 |
| Phase 3 | 8～12 | multi-call wave、compaction与final fault matrix |
| Phase 4 | 6～9 | 多waiting、remote authority、cancel/lease竞态 |
| Phase 5 | 5～8 | 前端恢复、事件冲突、可访问性与readiness inventory |
| Phase 6 | 7～11 | ApiRuntime装配收敛、旧测试处置和clean-archive回滚证明 |
| Phase 7 | 8～13 | operator、真实PG/Sidecar备份恢复、破坏性migration与真实MCP |
| 合计 | 56～86 | 只表示当前已知仓库工作量 |

每个Phase进入前用已完成checkpoint的实际用时和新增blocker重新估算剩余范围；估算变化不改变阶段顺序或质量门禁。
Phase 1、6、7的事务/删除/恢复checkpoint必须由至少两名对应领域维护者review；缺review只影响排期，不允许降低门禁。

## 18. 假设、风险、开放问题与停止条件

已确认假设：权威设计和PRD组继续有效；现有Capability、Skill、MCP、Lifecycle、Artifact、Event和Prompt安全合同可经公共
接口复用；旧binary可以忽略Phase 1 additive schema；用户接受旧DAG Task不迁移/不恢复；所有实现只面向`main`开发仓库；
不新增第三方Agent/图/异步锁依赖。开放问题：无。

记录的实现假设：PRD只要求protocol retry有界而未固定次数；本计划选择`agent_protocol_max_retries=1`作为默认值，确保至少
一次同edition修复机会，同时避免把协议错误放大为隐式长循环。该值是启动配置而非请求选项，任何默认值调整都必须更新
配置文档、wire/fault测试和本计划证据，不得作为解除Provider不兼容门禁的手段。

| 风险/停止条件 | 影响 | 必需动作 |
|---|---|---|
| Provider不能闭合roles/native tools/required choice | Run中途协议失败 | P0阻断该edition；不得降级text JSON或换edition |
| Agent schema不能被旧binary安全忽略 | pre-cutover回滚失效 | P1停止，不得提前切入口 |
| Kernel抽取造成旧DAG行为漂移或出现第二生命周期 | 安全/终态回归 | 回滚P2-A；只修公共Kernel/adapter，不复制实现 |
| Registry/Policy仍依赖WorkflowPlan/OrchestrationRequest | P6删除后Agent catalog不可用 | P2-B未去依赖不得`proof_complete` |
| Catalog必保segments超过模型预算 | 模型看见不完整authority | 采样前closed fatal；不得裁剪schema或换模型 |
| 不确定副作用只有重放才能继续 | 可能重复外部副作用 | P4保持`blocked`；提交aborted并让模型选择其他方案 |
| Phase 6未知入口、半切换binary或旧DAG静态引用 | 双控制面 | 整体回滚P6 bundle到最后DAG checkpoint |
| 备份no-clobber/restore/三backend任一失败 | destructive删除不可恢复 | 禁止P7-B；修复operator并重新生成整套proof |
| 真实PG、required Rust、真实MCP授权缺失 | mock不能证明真实边界 | 状态`blocked`；仅MCP可由用户明确书面waiver |
| 需要改变产品方向、风险容忍、API义务、依赖许可或waiver | 超出本计划授权 | 回到用户确认，不在实施中静默选择 |
| 任意操作可能影响`docker_cmd.md` | 本地敏感部署信息丢失/泄漏 | 立即停止；不得读取内容；按根AGENTS仓库外0600备份与恢复规则处理 |

## 19. Document-perfectization 审阅结论

本计划完成三轮完整审计/修订：

1. 第一轮关闭6个Major：权威`agent_loop/`包边界、FR/AL/NFR追踪、每checkpoint精确命令、Kernel persistence adapter、
   Registry/Policy去DAG依赖、P7 no-clobber operator与隔离rollback；同时修复owner、基线和估算重评三个Minor；
2. 第二轮关闭1个新增Major：P7跨backend互斥锁、immutable crash-prefix receipts、固定apply/restore顺序、未知提交判定与
   partial migration强制restore-all；
3. 第三轮复验22个checkpoint各有green gate/rollback/commit，22行FR/AL追踪、22行最小命令、12类NFR和四份handoff合同
   闭合，无Blocking或Major。

完整评分：

| 类别 | 得分 | 结论 |
|---|---:|---|
| 目标、范围、用户与stakeholder价值 | 15/15 | 用户价值、非目标、参与者和代码边界明确 |
| 功能需求 | 20/20 | FR-1～26及AL-P0～P7全部映射到checkpoint |
| 非功能需求 | 10/10 | 12类NFR均有主责与复验证据 |
| 验收标准与可测试性 | 15/15 | 22个green gate、最小命令、零测试/skip失败语义闭合 |
| 边界情况与失败模式 | 10/10 | protocol、lease、waiting、cancel、no-replay、cutover和migration crash闭合 |
| 依赖与实现可行性 | 10/10 | 对齐当前代码锚点、固定七职责包和三backend边界 |
| 测试、rollout、migration与rollback | 10/10 | pre-cutover proof、clean cutover、backup/restore和destructive边界独立 |
| 风险、假设、追踪与一致性 | 9/10 | 仅保留一个显式、风险受限的实现假设 |
| **总分** | **99/100** | **Pass with recorded assumptions** |

唯一扣分（Minor）：PRD只规定protocol retry有界，没有真实Provider证据支持精确默认次数；本计划选择
`agent_protocol_max_retries=1`。证据是父PRD的bounded retry要求和Phase 0明确“不新增外部真实Provider smoke门禁”；影响是个别
Provider可能在实施后需要受审配置调整，但不会破坏同edition、fail-closed或无`maxTurns`合同。后续在P0-B通过wire/fault tests、
attempt metrics和配置文档验证；不得用增大重试掩盖不兼容edition。

最终结论：**Pass with recorded assumptions**。无开放问题、无Blocking、无Major；业务实现仍需明确实施指令。

## 20. 实施启动边界

本次document-perfectization授权只覆盖计划审查和文档修订，不授权业务实现。收到明确实施指令后只启动P0-A：创建active
PRD/test/entry inventory并运行基线测试。P0-A绿灯前不写Agent Model代码；Phase 0退出前不开始Agent storage；Phase 5
readiness闭合前不进入cutover。每阶段回报实际commit、通过测试、未验证项和下一阶段进入条件，不把计划命令写成已通过。
