# 统一同模型 Agent Loop PRD 拆分设计

- **日期**：2026-08-21
- **状态**：拆分方案已确认；待用户复核书面版本后生成 PRD
- **适用分支**：`main`
- **架构来源**：`docs/superpowers/specs/2026-08-21-unified-agent-loop-design.md`
- **目标产物**：`docs/prd/backend/unified-agent-loop/` 下的 README、总纲 PRD 与 8 份阶段 PRD
- **实施状态**：尚未生成阶段 PRD，尚未修改业务代码

## 1. 结论

统一 Agent Loop 架构不适合作为单一实施 PRD。它同时涉及模型协议、Agent durable state、跨 backend 原子存储、
Capability 调用内核、Skill/MCP 适配、模型循环、暂停恢复、API/Frontend、控制面切换和破坏性 DAG schema 删除。
若直接生成一份实施计划，会把 additive proof、clean cutover 和 destructive migration 混成一个不可独立验收的变更。

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
- TaskNode invocation ledger、instance selection、artifact/event、interrupt、错误映射和 late result discard；
- `CapabilityInvocationPolicy`、model allowlist、system payload、parallel-safe和 can-suspend声明；
- 完整 Tool catalog token preflight和 provider-safe name反查；
- delegated Skill的安全 `PublicSkillProfile` activation；
- executable Skill可信 user/artifact/revision注入与 answer mode适配；
- MCP discovery、authorization、Selector、Result Parser保持原设计，并注入 Run-bound model binding。

不决定下一次模型动作，不拥有最终回答。

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
- 为所有入口切换准备 assembly contract和 API E2E，但不启用请求级双运行模式。

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
- 记录受控真实 MCP smoke结果或精确的外部证据缺口。

不迁移或恢复旧 DAG Task，不部署 `prod`。

退出门禁：三种 backend destructive migration与恢复演练通过，静态删除清单为零生产引用，全部自动门禁通过，
外部未验证项没有被误报为通过。

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

## 11. 测试与阶段状态

每个阶段采用 test-first：先添加能描述本阶段目标的失败测试，再实现并通过聚焦回归。阶段 README 状态至少使用：

- `pending`：上游门禁未满足或尚未开始；
- `in_progress`：测试/实现正在当前检查点进行；
- `proof_complete`：本阶段退出门禁通过，但尚未进入最终 cutover；
- `cutover_complete`：Phase 6全部入口切换和旧 runtime删除完成；
- `complete`：Phase 7最终证明完成。

不得把文档完成、测试 fixture存在或单 backend通过标记为阶段实现完成。

## 12. 回滚边界

| 边界 | 回滚合同 |
|---|---|
| Phase 0～Phase 5 | additive/proof阶段；可回滚本阶段代码和未使用schema，不切换真实入口 |
| Phase 6前 | 保留最后一个DAG代码检查点；切换必须在单一受审commit序列完成 |
| Phase 6后、Phase 7前 | 可以成对回退到DAG代码检查点；cutover后Agent Task不承诺由旧代码恢复，可清理开发数据 |
| Phase 7后 | 只能同时恢复Phase 7前数据库/Sidecar备份和对应代码，或继续forward fix |

Phase 7备份必须位于仓库外并验证可读。不得以读取或移动 `docker_cmd.md` 作为任何备份、清理或回滚步骤。

## 13. 文档生成与复核流程

本文经用户复核后，下一步只生成 README、总纲和8份阶段 PRD，不实施代码：

1. 先写 README和总纲，建立全局不变量、目录状态和FR追踪；
2. 按 Phase 0～Phase 7依次写阶段 PRD，后文可以引用前文，禁止反向依赖；
3. 对整组 PRD执行 placeholder、冲突、FR唯一归属、范围和链接扫描；
4. 同步 `docs/AGENTS.md`、`docs/prd/README.md`和`CHANGELOG.md`；
5. 创建文档检查点并请求用户复核；
6. 用户批准整组 PRD后，才能生成详细实施计划；
7. 实施计划批准前不修改业务代码。

## 14. 本拆分设计完成标准

本拆分设计只有在以下条件满足时才完成：

1. 用户确认8阶段依赖、目录、命名和职责；
2. FR-1～FR-26全部且唯一分配主责阶段；
3. Phase 6与Phase 7的runtime删除和physical schema删除边界不混淆；
4. 每阶段都有明确非范围和退出门禁；
5. 全局约束没有改变原统一Agent Loop设计；
6. 文档索引和CHANGELOG同步；
7. `git diff --check`通过并创建独立文档提交；
8. 用户复核书面版本后再生成阶段 PRD。
