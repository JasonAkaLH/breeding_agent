# 统一同模型 Agent Loop PRD 拆分设计

- **日期**：2026-08-21
- **状态**：document-perfectization 第四次全量审计100/100通过；可生成阶段PRD，尚未实施
- **适用分支**：`main`
- **架构来源**：`docs/superpowers/specs/2026-08-21-unified-agent-loop-design.md`
- **目标产物**：`docs/prd/backend/unified-agent-loop/` 下的 README、总纲 PRD 与 8 份阶段 PRD
- **实施状态**：尚未生成阶段 PRD，尚未修改业务代码

## 1. 结论

统一 Agent Loop 架构不适合作为单一实施 PRD。它同时涉及模型协议、Agent durable state、跨 backend 原子存储、
Capability 调用内核、Skill/MCP 适配、模型循环、暂停恢复、API/Frontend、控制面切换和破坏性 DAG schema 删除。
若直接生成一份实施计划，会把 pre-cutover proof、clean cutover 和 destructive migration 混成一个不可独立验收的变更。

采用以下结构：

- 1 个目录 README，维护阶段状态、执行顺序和验证入口；
- 1 个总纲 PRD，继承架构决策并维护全局不变量与追踪矩阵；
- 8 个按实施依赖排序的阶段 PRD，编号 Phase 0～Phase 7；
- 每个功能需求只有一个主责阶段，其他阶段只能作为依赖方或消费方验证；
- Phase 6 在同一受审检查点完成全部入口切换和 DAG runtime/wiring 删除；
- Phase 7 才执行 DAG storage/proto 的破坏性物理删除和最终证明。

## 2. 拆分目标

拆分后的每份阶段 PRD 必须满足：

1. 有单一、可解释的交付目标；
2. 上游输入和下游输出明确；
3. 可以在不宣称后续阶段完成的前提下独立测试；
4. 有明确的进入条件、退出门禁和 Git 检查点；
5. 回滚边界不跨越未声明的数据兼容边界；
6. 不重新讨论已批准的产品决策；
7. 能直接转换为一份有序、可执行的实施计划。

### 2.1 用户、参与者与阶段价值

| 参与者 | 关切 | 拆分后的价值 |
|---|---|---|
| 最终用户 | Agent能否根据真实Tool结果继续决策，并在等待/恢复后给出唯一最终回答 | 用户可见行为由Phase 3～6逐层证明，不以底层schema存在冒充功能完成 |
| Agent/Orchestration维护者 | Model、Storage、Invocation、Loop职责是否解耦 | 每个控制面只有一个主责阶段和一个交接合同，避免在`ApiRuntime`或Repository复制循环 |
| Skill作者与MCP集成方 | 既有contract、安全路由、审批和恢复是否退化 | Phase 2锁定能力适配，Phase 4锁定continuation；MCP discovery和Server内Tool list策略不改变 |
| Storage与Rust维护者 | 三种backend是否同义，租约与原子提交是否可恢复 | Phase 1先建立跨backend proof，Phase 7才物理删除DAG schema/proto |
| API与Frontend维护者 | Task/SSE/history/interrupt/graph兼容与可访问性是否保持 | Phase 5在入口切换前完成投影、恢复、焦点、键盘和语义测试 |
| 运维、安全与发布审查者 | 无`maxTurns`成本、敏感信息、no-replay、回滚和外部证据是否受控 | 每阶段有阻断条件；Phase 6/7分别守住控制面与破坏性数据边界 |

本拆分不以代码行数、文档数量或提交数量作为成功指标。成功标准是每一阶段都能用其退出门禁证明明确的用户或
平台能力，并且任何未满足的真实环境门禁都阻止阶段状态升级。

## 3. 未采用方案

### 3.1 六阶段粗粒度拆分

把 Model、Storage、Lease 和 Loop 合并，可减少文档数量，但会让单份 PRD 同时包含 provider wire contract、数据库
事务和业务状态机。失败时难以判断应回滚模型层、存储层还是控制层，不采用。

### 3.2 十至十一阶段细粒度拆分

把 Provider、Lease、Skill、MCP、Final Publisher 分别拆成 PRD，局部边界更细，但同一个 Agent sample 的合同会被
分散到过多文档，跨 PRD 更新和验收成本过高，不采用。

### 3.3 按技术团队或目录拆分

分别建立 Model、Storage、Skill、MCP、Frontend PRD，会产生相互等待的横向工作流，无法表达 clean cutover 的
严格顺序，不采用。

## 4. 文档权威与冲突处理

文档优先级固定如下：

1. 用户最新明确确认的产品决策；
2. `2026-08-21-unified-agent-loop-design.md` 的产品、架构和安全合同；
3. 本文的阶段顺序、FR 主责和切换边界；
4. 总纲 PRD 的跨阶段追踪与验收矩阵；
5. 各阶段 PRD 的本阶段交付细节；
6. 后续实施计划的文件级任务。

阶段 PRD 不得修改同模型、无 `maxTurns`、不恢复旧 DAG Task、保留当前 MCP discovery、单一 Agent 控制面等已确认
决策。若实施发现必须改变上层合同，应回到用户确认并更新权威设计，不能在阶段 PRD 或实施计划中静默覆盖。

### 4.1 当前状态与仓库证据

| 阶段 | 当前事实与证据 | 拆分要求 |
|---|---|---|
| Phase 0 | `src/integrations/llm_client.py`和`llm_runtime.py`只有text/messages路径，`_messages_payload`允许role fallback；`model_editions.py`管理公开模型 | 单独建立native Agent Model contract和启动能力门禁，不污染非Agent text用途 |
| Phase 1 | `src/storage/runtime_sidecar_facade.py::RuntimeLeaseFacade`已有Task lease；SQLite/PostgreSQL/Rust保存Task/TaskNode，但没有AgentRun/AgentItem | 复用单一Task lease语义并新增三backend等价Agent storage，不建立第二套租约 |
| Phase 2 | `src/orchestration/service.py`同时承载DAG执行；`registry.py`/`composite_executor.py`可复用；Skill已有`PublicSkillProfile`且`SkillExecutor`拒绝delegated mode；MCP Router/Selector只接收text generator | 提取唯一Invocation Kernel并分别闭合Tool catalog、delegated activation和Run-bound MCP model binding |
| Phase 3 | 当前最终回答由`main_agent.respond` capability/finalizer产生；PromptEnvelope、conversation memory、artifact/event helper可复用 | 新Loop拥有模型继续/停止权和唯一final publisher，旧finalizer在cutover前不被新入口调用 |
| Phase 4 | `src/lifecycle/`已有Interrupt/Cancel；MCP aggregate/outbox已有approval、MRTR、remote recovery authority | continuation必须回到原AgentRun/tool call并保留现有no-replay安全状态机 |
| Phase 5 | `src/api/runtime.py::_run_execution`装配DAG；`routes/tasks.py::get_task_graph`读取graph；Frontend消费`task.graph_created`和planner/soft-skill reasoning事件 | 先准备Agent投影与前端兼容，再切换真实入口；不得用请求级dual-runtime flag测试新路径 |
| Phase 6 | `src/orchestration/AGENTS.md`列出的Planner、Workflow、Replanner、CompletionPolicy仍是当前控制面 | 同一受审检查点切换所有入口并删除runtime源码/wiring，保留未读取的additive旧schema到Phase 7 |
| Phase 7 | Task/TaskNode/TaskEdge字段仍存在于Python model、SQLite/PostgreSQL、Runtime Sidecar、Rust core与Proto | 备份验证后成对删除物理合同，并保留`/graph`固定兼容投影 |

当前测试按`tests/AGENTS.md`分布在API、core、storage、lifecycle、orchestration、capabilities、integrations、e2e和
observability；Frontend使用Vitest/typecheck/build；Rust门禁统一从`scripts/run_rust_quality_gates.py`进入。这些是
阶段PRD必须引用的现有验证入口，不能自创一套与仓库工具不一致的测试体系。

## 5. 目标目录

```text
docs/prd/backend/unified-agent-loop/
  README.md
  00-统一同模型AgentLoop总纲PRD.md
  01-阶段零-现状基线与AgentModelContractPRD.md
  02-阶段一-AgentRunAgentItem与TaskLease存储PRD.md
  03-阶段二-InvocationKernel与SkillMCP适配PRD.md
  04-阶段三-核心AgentLoop与FinalOutputPRD.md
  05-阶段四-WaitingContinuation与RecoveryPRD.md
  06-阶段五-APISSEFrontend与Observability适配PRD.md
  07-阶段六-全入口CleanCutover与DAGRuntime删除PRD.md
  08-阶段七-破坏性Schema删除与最终门禁PRD.md
```

README 只维护索引、状态和验证入口。`00` 负责总纲，不作为额外实施阶段。`01`～`08` 分别对应 Phase 0～Phase 7。

## 6. 阶段依赖

```text
Phase 0 现状基线与 Agent Model Contract
  -> Phase 1 AgentRun / AgentItem / Task Lease / 原子存储
  -> Phase 2 Invocation Kernel / Tool Catalog / Skill / MCP
  -> Phase 3 核心 Agent Loop / Multi-call / Compaction / Final Output
  -> Phase 4 Waiting / Continuation / Recovery / Cancel
  -> Phase 5 API / SSE / Frontend / Observability 兼容适配
  -> Phase 6 全入口 Clean Cutover + DAG runtime/wiring 删除
  -> Phase 7 破坏性 Schema/Proto 删除 + 最终证明
```

依赖是严格有向链。允许在同一开发窗口连续实施相邻阶段，但不得合并退出门禁、跳过前置证明或把多个阶段记为
一个不可审查的提交。

## 7. 阶段定义

### 7.1 Phase 0：现状基线与 Agent Model Contract

**主责 FR**：FR-2、FR-3、FR-19。

负责：

- 锁定现有 Skill、MCP、Interrupt、Task、history 和 Provider 行为基线；
- 定义 provider-neutral `AgentModelPort`、`AgentSample`、`AgentToolCall` 与 streamed delta 合同；
- 实现原生单/多 tool calls、分片 arguments 和最终 assistant text 解析；
- 定义 provider-safe name 映射和 unknown tool 与 protocol violation 的边界；
- 为每个公开 model edition增加 messages、roles、native tools 和 required tool choice 启动门禁；
- 证明 Agent sample、compaction 和内部 MCP model binding使用同一 model edition所需的底层能力。

不负责 AgentRun、业务循环或入口切换。

退出门禁：model fake/wire golden tests覆盖单调用、多调用、损坏 delta、unknown tool、role/tool能力缺失、required choice
违规、取消和 usage metadata；现有非 Agent text用途不退化。

### 7.2 Phase 1：AgentRun、AgentItem 与 Task Lease 存储

**主责 FR**：FR-17、FR-26。为 FR-23 提供 storage primitive，但不取得其端到端完成权。

负责：

- AgentRun、AgentItem、状态映射、item kind、sequence、digest 和 131,072-byte canonical payload；
- `commit_agent_sample`、reserved result slots、`commit_agent_call_outcome` 和 `commit_agent_final_output` 原子合同；
- 单一 Task lease authority、acquire/renew/release、heartbeat fencing、stale commit拒绝和 TTL接管；
- SQLite、PostgreSQL、Runtime Sidecar/Rust contract、migration、manifest、权限和 conformance tests；
- additive schema与旧 binary可忽略边界。

不调用模型或 Capability，不实现 Agent 控制流。

退出门禁：三种 backend parity、canonical JSON golden vectors、fault injection、token rotation、长调用续租、失租接管、
waiting release primitive、terminal cleanup和 stale commit测试通过。

### 7.3 Phase 2：Invocation Kernel 与 Skill/MCP 适配

**主责 FR**：FR-13、FR-15、FR-16、FR-20、FR-24。

负责：

- 从旧 Orchestration 提取唯一 `CapabilityInvocationService`；
- 允许旧DAG service在开发检查点改为调用该公共Kernel，但必须是行为保持重构并通过旧路径回归；这不是Agent入口、
  dual runtime或新路由模式；
- TaskNode invocation ledger、instance selection、artifact/event、interrupt、错误映射和 late result discard；
- `CapabilityInvocationPolicy`、model allowlist、system payload、parallel-safe和 can-suspend声明；
- 完整 Tool catalog token preflight和 provider-safe name反查；
- delegated Skill的安全 `PublicSkillProfile` activation；
- executable Skill可信 user/artifact/revision注入与 answer mode适配；
- MCP discovery、authorization、Selector、Result Parser保持原设计，并注入 Run-bound model binding。

不决定下一次模型动作，不拥有最终回答，也不允许用户请求进入尚未闭合的Agent Loop。

退出门禁：旧执行路径可在开发检查点调用公共 Kernel；Skill/MCP ordinary、approval、MRTR、remote、result parsing、
no-replay和敏感信息扫描回归通过；不存在第二份 capability lifecycle实现。

### 7.4 Phase 3：核心 Agent Loop 与 Final Output

**主责 FR**：FR-4、FR-5、FR-6、FR-7、FR-10、FR-11、FR-12、FR-23。

负责：

- `AgentLoopOrchestrator`、ContextBuilder和模型自然停止；
- 自动请求和显式 Skill/MCP first-call constraint；
- multi-call deterministic waves、parallel-safe gate、reserved result顺序重建；
- 普通 tool failure回填、unknown tool结果、fatal边界和无轮次上限；
- 同模型 compaction、covered digest和原始 AgentItems保留；
- `agent.final_output`内部节点、Artifact producer、history message、durable event和唯一 publication receipt。

不交付长期 waiting continuation，不切换真实 API 执行入口。

退出门禁：自动/显式请求、连续 Tool chain、多调用顺序、超过旧预算仍完成、错误自纠、compaction、唯一 final、
final crash重放和无第二模型调用测试通过。

### 7.5 Phase 4：Waiting、Continuation 与 Recovery

**主责 FR**：FR-8、FR-9、FR-18。消费 FR-26 的 lease contract，不重新定义租约。

负责：

- 一个 batch的零到多个 waiting calls和剩余 wave位置；
- Skill missing input、MCP approval、MRTR和 remote Task恢复原 AgentRun/tool call；
- continuation locator、identity/digest校验、重复唤醒幂等；
- waiting前提交 authority并释放 lease，恢复时重新 acquire；
- crash recovery、late result、unknown side effect `aborted`和 no-replay；
- cancel/completion线性化。

不改变外部 API DTO 或切换全部入口。

退出门禁：多 Interrupt逐项回答、首个回答不提前采样、remote恢复、重复 outbox、失租接管、迟到结果、取消竞态和
restart fault injection通过；所有 continuation恢复同一 Run和原 call。

### 7.6 Phase 5：API、SSE、Frontend 与 Observability 适配

**主责 FR**：FR-21、FR-22。

负责：

- AgentRun到 Task/TaskNode/Interrupt的外部状态投影；
- `/graph` empty-edge invocation ledger兼容视图；
- Agent durable events、transient reasoning、低基数指标和 final publish delay；
- SSE、history、fallback disclosure、download声明和 frontend progress/restore适配；
- 为所有入口切换准备 assembly contract和 API E2E；Agent投影通过test-only assembly、fake events和fixture验证，不启用
  请求级双运行模式或可被真实请求选择的新入口。

不切换真实执行入口，不保留 runtime feature flag作为过渡交付物。

退出门禁：API DTO、SSE replay、history、frontend refresh、approval/interrupt交互、graph repository spy、事件/指标合同、
敏感 label扫描、frontend test/typecheck/build通过。

### 7.7 Phase 6：全入口 Clean Cutover 与 DAG Runtime 删除

**主责 FR**：FR-1、FR-14。

负责在同一个受审 Git 检查点：

- 把普通、显式 Skill、显式 MCP、Interrupt answer、approval和 remote recovery全部切到 Agent Loop；
- 删除 WorkflowPlan构建、Workflow Provider/Router/Expander/Validator；
- 删除 DAG executor、Runtime/Soft Skill Replanner、CompletionPolicy和独立 `main_agent.respond` finalizer；
- 删除 planner LLM config/wiring、DAG Prompt措辞和旧任务 resume/fallback入口；
- 保留尚未物理删除但不再读取的 additive DAG storage字段，供 Phase 7安全删除。

退出门禁：生产源码只有一个 Agent控制面；不存在请求级 fallback、dual-runtime flag或旧 DAG恢复入口；全入口E2E、
完整后端回归、frontend回归和Rust非破坏性合同通过。

### 7.8 Phase 7：破坏性 Schema 删除与最终门禁

**主责 FR**：FR-25，并负责 FR-1～FR-26的最终集成证明。

负责：

- 在仓库外生成并验证 SQLite、PostgreSQL和Runtime Sidecar持久化备份；
- 删除 TaskEdge表、storage/proto/runtime dependency和 DAG-only Task/TaskNode字段；
- 更新 SQLite bootstrap、PostgreSQL manifest/reconciler、Rust contract和DTO固定兼容投影；
- 静态证明生产源码不存在旧 DAG控制面或 storage读取；
- 完成完整后端、frontend、Rust、文档、AGENTS和CHANGELOG门禁；
- 记录受控真实MCP smoke结果；若没有授权或配置，必须保持`blocked`，除非用户明确批准书面waiver。

不迁移或恢复旧 DAG Task，不部署 `prod`。

退出门禁：三种 backend destructive migration与恢复演练通过，静态删除清单为零生产引用，全部自动门禁通过，
且受控真实MCP smoke证据或用户明确批准的书面waiver存在；否则Phase 7保持`blocked`。

## 8. FR 主责矩阵

| 主责阶段 | FR |
|---|---|
| Phase 0 | FR-2、FR-3、FR-19 |
| Phase 1 | FR-17、FR-26 |
| Phase 2 | FR-13、FR-15、FR-16、FR-20、FR-24 |
| Phase 3 | FR-4、FR-5、FR-6、FR-7、FR-10、FR-11、FR-12、FR-23 |
| Phase 4 | FR-8、FR-9、FR-18 |
| Phase 5 | FR-21、FR-22 |
| Phase 6 | FR-1、FR-14 |
| Phase 7 | FR-25；全部需求最终集成证明（不改变主责归属） |

每个 FR 恰有一个实现主责阶段。Phase 7 的最终证明不改变主责归属。

### 8.1 阶段交接产物

| 阶段 | 必须交付给下一阶段的稳定产物 | 下游不得依赖的内部细节 |
|---|---|---|
| Phase 0 | `AgentModelPort`、规范化sample/tool-call类型、provider capability gate、测试fake | OpenAI wire对象、client实例、API key、text fallback实现 |
| Phase 1 | AgentRun/AgentItem repository contract、原子sample/outcome/final API、LeaseController与三backend conformance vectors | SQLAlchemy row、SQL语句、Rust SQLite内部表布局 |
| Phase 2 | `CapabilityInvocationService`、Tool Catalog/Policy、Delegated activation、Run-bound MCP binding | 旧WorkflowNodePlan、具体Skill/MCP executor内部对象 |
| Phase 3 | `AgentLoopOrchestrator`、ContextBuilder、FinalOutputPublisher及非waiting测试assembly | API route、旧finalizer、进程内未持久状态 |
| Phase 4 | start/resume统一入口、waiting集合、continuation locator、recovery/cancel coordinator | MCP raw result、旧continuation plan、进程内Future |
| Phase 5 | Task/API投影、SSE/history/event/metric合同、Frontend reducer和cutover-readiness report | DAG edge、Planner/Replanner事件作为新路径事实源 |
| Phase 6 | 单一Agent runtime assembly、DAG runtime删除清单、仍待Phase 7删除的schema inventory | 可执行旧DAG入口、feature flag或请求级fallback |
| Phase 7 | destructive migration/restore evidence、静态删除报告、最终测试证据和文档状态 | 旧DAG任务reader、反向schema猜测或未验证的prod声明 |

每个阶段的交接产物必须由contract测试或schema golden vector保护。下游只能消费表中稳定产物；若必须读取被禁止的
内部细节，说明阶段边界错误，必须回到PRD修订而不是增加临时adapter。

### 8.2 NFR 主责矩阵

| NFR维度 | 主责阶段 | 必须协作/复验的阶段 | 退出要求 |
|---|---|---|---|
| Provider兼容与同模型 | Phase 0 | Phase 2～7 | 所有公开edition通过messages/tool roles/native tools/required choice门禁，Run内model binding不变 |
| 一致性与原子性 | Phase 1 | Phase 3、4、6、7 | 三backend满足sample/result reservation、waiting、final publication和lease fencing不变量 |
| 安全与隐私 | Phase 2 | Phase 0、1、3～7 | capability可见性和参数fail closed；Skill/MCP/raw/hidden内容不泄漏到AgentItems、event或metric label |
| Tool catalog容量 | Phase 2 | Phase 3、6、7 | 完整可见schema计入preflight；超限采样前fail closed，不静默省略 |
| 上下文正确性 | Phase 3 | Phase 4、6、7 | canonical payload有界；compaction保留digest和原始items；hidden/raw正文不进入summary |
| 性能与资源 | Phase 3 | Phase 1、4、5、6、7 | 只并发明确parallel-safe调用；无busy polling；waiting不占worker；保留30 active Task backpressure |
| 最终输出唯一性 | Phase 3 | Phase 1、5、6、7 | final sample后无第二模型调用；Artifact/Message/event/receipt原子且幂等 |
| 恢复与no-replay | Phase 4 | Phase 1、2、5、6、7 | 多waiting逐项闭合；失租旧owner不能提交；不确定副作用不自动重放 |
| 可观测性 | Phase 5 | Phase 1、3、4、6、7 | 源架构设计第15节事件/指标齐全、低基数且无durable reasoning content |
| API与Frontend兼容 | Phase 5 | Phase 6、7 | Task/SSE/interrupt/history保持；`/graph`固定empty-edge；旧客户端安全退化 |
| 可访问性 | Phase 5 | Phase 6、7 | 若approval/interrupt/progress DOM变化，焦点、键盘、语义和恢复测试必须通过；无变化也要记录证据 |
| 可维护性与单控制面 | Phase 6 | Phase 2、3、4、7 | `ApiRuntime`不承载Loop细节；invocation/outcome只有一个实现；无第三方Agent/图/锁框架和双runtime |

NFR与FR一样只有一个主责阶段。协作阶段必须复验与自身路径相关的不变量，不能因为主责阶段已经通过而省略集成
验证。

## 9. 每份阶段 PRD 的固定结构

每份 PRD 必须包含以下章节：

1. 文档状态、日期、来源和阶段编号；
2. 目标、用户价值和可观察结果；
3. 上游依赖与进入条件；
4. 本阶段范围与明确非范围；
5. 当前代码证据和受影响模块；
6. 数据、接口、状态和安全合同；
7. 正常流程、失败流程、恢复与取消；
8. FR/NFR主责与跨阶段消费关系；
9. 测试先行清单、验收矩阵和最小命令入口；
10. Git检查点、回滚方式和不可逆边界；
11. 完成标准、未验证项和下一阶段交接。

阶段 PRD 应描述交付级合同，不展开成逐文件编码步骤；逐文件操作属于后续实施计划。

## 10. 跨阶段不变量

所有阶段继承以下约束：

- Agent controller、Tool选择、context compaction、MCP Router/Selector和最终回答绑定同一 model edition；
- Agent Loop没有 `maxTurns`、`max_replans`或`max_dynamic_nodes`终止条件；
- 不迁移、不读取、不恢复旧 DAG Task；
- MCP discovery、Server内 Tool discovery、授权、Result Parser和内部 call budget保持现有设计；
- Outer Agent不展开完整 MCP Server Tool list；
- Tool call/sample先持久化，副作用后原子提交 outcome；
- unknown side effect不自动重放；
- delegated Skill只使用安全 `PublicSkillProfile`投影，不读取 manifest/resource正文；
- hidden reasoning、raw MCP result、上传正文和敏感内部信息不进入 durable Agent上下文；
- Phase 6最终不保留双控制面、fallback或feature flag；
- 所有工作只授权 `main`仓库，不等于`prod`部署；
- 根目录 `docker_cmd.md`不读取、不移动、不跟踪、不删除。

### 10.1 既有 PRD 与文档处置

新PRD目录在生成时是“已批准未来架构”，当前`docs/prd/backend/00-主代理框架PRD.md`仍描述已实现DAG基线。文档
状态必须随代码阶段推进，不能提前把未来设计写成当前实现，也不能在Phase 6后继续把旧DAG标为正式基线。

| 文档类别 | 最低已知范围 | 处置责任 |
|---|---|---|
| 主入口与编排基线 | `docs/prd/README.md`、`backend/00-主代理框架PRD.md`、`backend/02-编排模型与资源调度.md` | 生成PRD时登记future authority；Phase 6切换后把Agent Loop改为当前编排基线并移除DAG现行口径 |
| Main Agent、memory与恢复 | `backend/08-主代理Skill兼容与真实LLM运行时.md`、`10-对话上下文记忆与压缩PRD.md`、`18-失败自检恢复与Fallback控制层PRD.md` | 保留仍有效产品行为；Phase 3～6替换Planner/Replanner/finalizer控制面描述 |
| Skill与Workbench | `backend/12-*`、`13-*`、`15-*`、`22-*`、`backend/skill-workbench/`、`skill-contract-progressive-disclosure/` | 保留Skill contract/executor/runtime安全；重写或标记被Agent Loop取代的runtime replan/finalizer章节，不整份误删 |
| 能力缺失fallback | `backend/23-*`、`backend/capability-missing-fallback/` | Phase 3定义Agent-native披露事实源；Phase 6废止Plan metadata和Replanner路径，同时保留正文、event、history和artifact禁止合同 |
| Prompt与Provider | `backend/prompt-envelope/` | 保留PromptEnvelope、安全profile、messages-native和缓存边界；删除Planner/Replanner消费者并改由Agent ContextBuilder消费 |
| MCP、Frontend与Rust | `backend/14-*`、`MCP/user-scoped-on-demand/02-*`、`frontend/00-*`、`rust/07-*` | 保留MCP协议/授权/恢复、Frontend交互和Rust安全kernel；只更新DAG/Planner/TaskEdge引用及新Agent投影 |
| 索引与维护规则 | `docs/AGENTS.md`、各受影响目录`AGENTS.md`、`CHANGELOG.md` | 每阶段职责或入口变化时同步；Phase 7做最终零漂移审查 |

Phase 0必须生成精确的active PRD inventory，至少覆盖仓库中包含`WorkflowPlan`、`RuntimeReplanner`、
`main_agent.respond`、`CompletionPolicy`、`max_replans`或`max_dynamic_nodes`的PRD。每项标记为：

- `preserve`：产品合同仍有效，只更新实现引用；
- `rewrite`：合同继续有效，但控制面改为Agent Loop；
- `supersede_at_phase6`：只描述被删除的DAG行为，Phase 6后不得继续标为active；
- `historical`：只保留历史证据，必须明确不再指导实现。

Phase 6退出时，active PRD和索引中不得继续把Planner、Replanner、WorkflowPlan、DAG finalizer或旧恢复路径称为当前
运行时。历史设计可以保留这些词，但必须有显式historical/superseded状态，不能被主索引列为实现入口。

## 11. 测试与阶段状态

每个阶段采用 test-first：先添加能描述本阶段目标的失败测试，再实现并通过聚焦回归。阶段 README 状态至少使用：

- `pending`：上游门禁未满足或尚未开始；
- `blocked`：必需代码、环境、权限或外部证据缺失，必须记录精确原因和解除条件；
- `in_progress`：测试/实现正在当前检查点进行；
- `proof_complete`：本阶段退出门禁通过，但尚未进入最终 cutover；
- `cutover_complete`：Phase 6全部入口切换和旧 runtime删除完成；
- `complete`：Phase 7最终证明完成。

不得把文档完成、测试 fixture存在或单 backend通过标记为阶段实现完成。

### 11.1 分阶段验证矩阵

| 阶段 | 最低当前测试域 | 新增证明重点 |
|---|---|---|
| Phase 0 | `tests/integrations/test_llm_client.py`、`test_llm_runtime.py`、`tests/api/test_model_edition_selection.py` | native tool wire golden、required choice、unknown tool、同edition binding |
| Phase 1 | `tests/core/`、`tests/storage/`、Runtime Sidecar contract/Rust tests | Agent schema parity、canonical vectors、atomic fault injection、lease fake clock/fencing |
| Phase 2 | `tests/orchestration/`、`tests/capabilities/`、`tests/integrations/agent_skills/`、`tests/integrations/mcp/` | 单一Invocation生命周期、PublicSkillProfile activation、catalog preflight、MCP binding/no-replay |
| Phase 3 | 新Agent Loop unit/integration、main-agent、memory、prompt、artifact/history tests | long trajectory、multi-call waves、compaction、final atomicity、无第二LLM call |
| Phase 4 | `tests/lifecycle/`、MCP aggregate/recovery、API interrupt/cancel、`tests/e2e/` | 多waiting、resume identity、duplicate wakeup、restart、late result、cancel linearization |
| Phase 5 | API DTO/SSE/history/graph tests与Frontend Vitest | empty-edge投影、event/metric leak scan、refresh restore、approval/interrupt可访问性 |
| Phase 6 | 全部后端分层回归、Frontend全量、Rust非破坏性合同、静态runtime删除扫描 | 所有入口唯一进入Agent Loop，旧DAG源码/wiring和测试装配不再可执行 |
| Phase 7 | storage migration/permission/real PostgreSQL、Rust quality gates、Frontend build、全量后端及静态schema扫描 | destructive migration/restore、三backend最终parity、文档与源码零漂移 |

阶段PRD必须把上表展开为精确命令。命令遵守仓库当前工具：后端使用`conda run -n multi_agent python -m unittest`
及`compileall`；Frontend至少运行`npm test -- --run`、`npm run typecheck`和`npm run build`；Rust从
`scripts/run_rust_quality_gates.py`选择并运行本阶段要求的gate。`--skip-unavailable`只允许诊断工具缺失，不能作为
required gate通过证据；非默认PyO3 wheel smoke只有在本阶段修改对应wheel/ABI或发布合同时才进入必需门禁。

### 11.2 环境证据与 skip 语义

| 证据 | 完成要求 |
|---|---|
| SQLite | 本地/CI自动测试必须通过，不能由mock替代 |
| PostgreSQL | Phase 1和Phase 7的schema、transaction、permission与并发测试必须使用真实测试DSN；未配置DSN或skip时阶段保持`blocked` |
| Runtime Sidecar/Rust | contract、proto和Rust tests必须通过；缺cargo或必需质量工具时保持`blocked`，不得以Python facade测试替代 |
| Frontend | Phase 5～7的Vitest、typecheck、build必须全部通过；只跑reducer test不构成完成 |
| 受控真实MCP smoke | 自动fake/隔离integration始终必需；源设计要求的受控真实MCP smoke缺少明确授权或配置时保持外部证据缺口，Phase 7不得标记`complete`，除非用户明确批准书面waiver |
| Phase 7备份恢复 | 必须在受控开发环境生成仓库外备份并实际验证可读/可恢复；只写命令或检查文件存在不构成通过 |

任何skip、not-run或环境缺失都必须出现在阶段README和最终证据报告中。只有明确被阶段PRD判定为不适用且说明原因的
门禁可以记为N/A；缺工具、缺DSN、缺凭据和缺授权不是N/A。

### 11.3 旧测试处置

- 证明用户行为、安全边界、Skill/MCP contract、Interrupt/Cancel、Artifact/history或API兼容的测试必须迁移到新
  Agent入口，不得因其依赖旧fixture而删除；
- 只断言WorkflowPlan JSON形状、DAG edge排序、Replanner次数、CompletionPolicy或finalizer节点存在的实现专属测试，
  在Phase 6随对应源码删除；删除前必须确认其用户可见/安全断言已由新测试覆盖；
- Phase 7 migration tests可以保留已删除表/列名，但文件名和断言必须明确其migration/history用途，且不能被业务源码
  import；
- 不允许通过批量删除红测试使阶段变绿。每个删除的旧测试集合必须在cutover checklist中关联替代测试或记录“纯旧
  实现断言、无行为合同”的证据。

## 12. 回滚边界

| 边界 | 回滚合同 |
|---|---|
| Phase 0～Phase 5 | pre-cutover proof阶段；Phase 1 schema为additive，Phase 2允许行为保持的Kernel抽取，其余新Agent能力只通过test assembly证明；可回滚本阶段代码和未使用schema，不切换真实入口 |
| Phase 6前 | 保留最后一个DAG代码检查点；切换必须在单一受审commit序列完成 |
| Phase 6后、Phase 7前 | 可以成对回退到DAG代码检查点；cutover后Agent Task不承诺由旧代码恢复，可清理开发数据 |
| Phase 7后 | 只能同时恢复Phase 7前数据库/Sidecar备份和对应代码，或继续forward fix |

Phase 7备份必须位于仓库外并验证可读。不得以读取或移动 `docker_cmd.md` 作为任何备份、清理或回滚步骤。

### 12.1 风险、缓解与假设

| 风险 | 影响 | 缓解与阻断条件 |
|---|---|---|
| 阶段交接contract漂移 | 下游绑定上游内部实现，后续重构反复返工 | 使用第8.1节稳定产物和contract tests；出现反向依赖时阻断下一阶段 |
| Phase 0～5 pre-cutover代码长期不接流量 | 新旧路径行为漂移或形成事实上的双runtime | 除Phase 2行为保持Kernel抽取外只通过test assembly验证；Phase 6前执行cutover-readiness全回归；禁止请求级开关和影子副作用 |
| 三backend语义分叉 | 本地通过、PostgreSQL或Sidecar恢复失败 | Phase 1建立conformance vectors，Phase 4/7重复fault injection；真实PG/Sidecar缺失即blocked |
| Phase 6半切换 | 不同入口进入不同控制面，恢复和终态不一致 | 单一受审commit序列、入口inventory、静态删除扫描和全入口E2E；任一入口仍指向DAG则回滚整个checkpoint |
| Phase 7误删或无法代码回退 | 开发数据丢失或schema与binary不匹配 | 仓库外备份和恢复演练先于migration；删除后只允许成对恢复或forward fix |
| 既有PRD继续宣称DAG为当前基线 | 后续实现重新引入Planner/Replanner | Phase 0 inventory、Phase 6 active-doc零漂移门禁和第10.1节处置分类 |
| 无`maxTurns`导致成本/等待增长 | 长任务占用资源、用户误以为卡死 | 保留backpressure、取消、compaction和低基数分布指标；不得暗中增加轮次终止 |
| 真实MCP、PG或必需Rust证据不可用 | 仓库自动测试无法证明对应真实集成 | 状态保持blocked或在仅允许waiver的MCP smoke项记录用户明确waiver；不得把skip写成pass |
| PRD数量增加导致复制漂移 | 同一FR/NFR在多文档出现不同定义 | 总纲保留唯一矩阵；阶段PRD引用上层合同，只写本阶段增量和消费验证 |

已确认假设：

- 源架构设计继续是产品/安全权威，本拆分不重新选择产品方向；
- 当前分支`main`只用于开发仓库实现，`prod`部署不在任何阶段范围；
- 现有Capability、Skill、MCP、Lifecycle、Artifact、Event和Prompt安全实现可通过公共接口复用；
- 新增Agent storage可以先additive落地，旧binary在Phase 6前能够忽略；
- 用户接受不迁移、不恢复cutover前旧DAG Task；
- 不新增第三方Agent、图执行或异步锁框架，依赖许可变化必须另行审查。

开放问题：无。若真实实现推翻上述任一假设，阶段状态转为`blocked`并回到用户确认，不能由实施计划自行改变。

## 13. 文档生成与复核流程

本文通过document-perfectization信心门后，下一步只生成 README、总纲和8份阶段 PRD，不实施代码：

1. 先写 README和总纲，建立全局不变量、目录状态和FR追踪；
2. 按 Phase 0～Phase 7依次写阶段 PRD，后文可以引用前文，禁止反向依赖；
3. 对整组 PRD执行 placeholder、冲突、FR唯一归属、范围和链接扫描；
4. 同步 `docs/AGENTS.md`、`docs/prd/README.md`和`CHANGELOG.md`；
5. 创建文档检查点并请求用户复核整组PRD；
6. 用户批准整组 PRD后，才能生成详细实施计划；
7. 实施计划批准前不修改业务代码。

## 14. 本拆分设计完成标准

本拆分设计只有在以下条件满足时才完成：

1. 用户确认8阶段依赖、目录、命名和职责；
2. FR-1～FR-26全部且唯一分配主责阶段；
3. 源设计全部NFR有唯一主责阶段和跨阶段复验责任；
4. 每阶段有稳定交接产物、明确非范围和退出门禁；
5. Phase 6与Phase 7的runtime删除和physical schema删除边界不混淆；
6. 真实PostgreSQL、Rust、Frontend、MCP smoke和备份恢复的skip/blocked语义明确；
7. 既有active PRD、旧测试和目录索引有可验证的处置规则；
8. 风险、假设、失败路径和回滚边界没有隐藏决策；
9. 全局约束没有改变原统一Agent Loop设计；
10. 文档索引和CHANGELOG同步；
11. `git diff --check`通过并创建独立文档提交；
12. document-perfectization完整评分达到至少95/100且无Blocking或Major后再生成阶段PRD。
