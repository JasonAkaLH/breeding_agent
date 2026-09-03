# Skill Bundle Revision v2 与缺失 Revision 启动隔离设计

状态：`published_pending_deploy`
信心门：`98/100 Pass（0 Blocking / 0 Major / 2 Minor）`
日期：2026-09-03
目标分支：`main`

## 1. 问题与目标

开发环境切换到backend-dev `0.1.30`后，FastAPI在startup recovery阶段读取到一个仍可恢复的
Agent Run。该Run的prepared authority固定了旧进程内的Skill revision
`skillrev-000002-f84bc49b3ad8`，而新进程的`SkillRuntimeState`不含这个key，
`bundle_for_revision()`抛出`KeyError`，异常继续穿透lifespan并导致整个服务退出。

旧revision由进程内递增序号和12位fingerprint摘要组成：

```text
skillrev-<process-local counter>-<12 hex fingerprint prefix>
```

序号不能跨重启重建。即使当前挂载的Skill文件与旧任务使用的内容相同，新进程生成的序号也可能不同。
当前系统又只持久化revision字符串，不持久化旧Skill bundle或script package snapshot，因此缺失的旧内容
没有可恢复authority。

本设计现已完成仓库实现和自动门禁，并基于`main@414afa2d`发布backend-dev `0.1.31`；镜像尚未部署，
开发库旧Task终态化和hard cut仍待执行。

本设计完成三个目标：

1. 新Skill bundle改用跨重启稳定的revision v2；只要当前扫描到的catalog/manifest fingerprint完全一致，
   新任务就能在重启后重新取得同一个revision。
2. 旧对话、消息、已持久化结果和Artifact保持可读；历史读取不得解析或加载v1 Skill bundle。
3. 新对话和旧对话的后续新消息都创建使用当前v2 revision的新Task/AgentRun。非终态v1 Task不恢复、不迁移、
   不重放；无论它已有AgentRun、仍处于pending handoff，还是已handoff为pre-AgentRun Interrupt，都必须安全
   终态化并继续应用startup。`ACCEPTED`/`PLANNING`/`RUNNING`收敛为`FAILED`；已经处于
   `CANCELLING`的Task沿现有状态机完成为`CANCELLED`，不得为强制FAILED而扩大Rust状态合同。

## 2. 已确认决策

采用以下方案：

- 新写格式固定为`skillrev-v2-<64 lowercase hex sha256>`；
- v2摘要沿用当前Skill fingerprint的canonical输入，但使用完整SHA-256，不再拼接进程内计数器；
- v2 runtime全面停止执行v1 revision；即使进程内字典存在同名v1 key，执行解析也必须先判定为retired；
- current active v2只允许在新submission preparation冻结authority时被选择；一旦Task/prepared/slot authority
  已存在，所有retain、恢复、manifest解析和continuation都必须使用其显式revision，不得以active兜底；
- v1只可作为历史opaque metadata随既有记录读取，历史投影不得调用`bundle_for_revision()`；
- 非终态v1、null或空revision Task使用`agent_skill_bundle_revision_retired`安全终止；合法但不可用的v2使用
  `agent_skill_bundle_revision_unavailable`，其他格式使用`agent_skill_bundle_revision_invalid`。终态目标
  默认为FAILED；Task已经CANCELLING时使用现有cancel writer收敛Run/Task为CANCELLED；
- startup在任何MCP aggregate/recovery之前执行Skill revision eligibility prepass；它同时覆盖recoverable
  AgentRun和已handoff但尚无AgentRun的非终态submission Task。所有已经形成执行或handed-off authority的
  legacy Task先failed，只有exact可用的v2 authority才能留给后续MCP和Agent recovery；仍为pending且没有
  执行authority的submission由handoff阶段处理；
- 尚未创建AgentRun且prepared snapshot已完整校验的pending submission，在任何handoff物化回调之前执行同一
  eligibility检查；legacy submission不得进入正常handoff，而要按原planned handoff kind物化确定性的terminal
  对象、按Task当前状态收敛为FAILED或CANCELLED并正常acknowledge为`handed_off`，避免重启重复处理；prepared snapshot本身缺失或损坏时
  沿用现有fail-closed错误，不猜测handoff kind、不acknowledge；
- 已handoff且尚无AgentRun的非终态submission Task由prepass按冻结handoff authority处理；legacy initial
  Interrupt在handoff identity校验后只要求通过现有权威Task CAS将Task置为FAILED或CANCELLED，既有Interrupt/Node作为
  不可操作的历史证据保留；异常的非终态no-server intent复用既有exact convergence，已终态记录不改写；
- 任一失败分支均不得降级到active bundle，不得改写prepared authority，不得执行Skill、MCP Tool或模型采样；
- 旧对话的新消息与新对话一样固定当前active v2并创建新Task/AgentRun；legacy Run遗留的补充输入或审批
  作废，用户只能通过新消息重新发起；
- Task/Run terminal是所有Interrupt、补充输入和MCP approval入口的统一liveness gate；所有入口必须在保存
  Answer、Grant、pending-action状态或触发continuation之前拒绝terminal Task；
- 已有AgentRun分支以Run/Task/适用Node的既有原子terminal writer为durable authority；无AgentRun的
  pre-AgentRun分支以Task terminal为唯一必需authority。`conversation.current_task_id`是可幂等修复的
  指针投影，不与任何terminal writer虚称跨表或跨authority原子；
- v2只声明扫描时的catalog/manifest/package fingerprint身份；`script_package_snapshot=false`继续明确，
  不宣称执行脚本字节严格可复现；
- 本轮不改变MCP bundle recovery、MCP parser/projection revision或`MCPRuntimeState.mcprev-*`格式。

### 2.1 未采用方案

1. **用v1的12位后缀匹配当前bundle**：可以快速恢复，但12位摘要不是完整内容authority，而且会把
   已经不存在的旧revision静默替换成当前对象，违反“不加载相应内容”的决定。
2. **把旧v1 Run原地改写为v2后继续**：会改变prepared authority、Skill activation和审批/输入上下文，
   还可能重放Tool副作用，不符合全面v2 clean cutover。
3. **立即持久化完整Skill/script package snapshot**：可提供严格执行复现能力，但涉及新的持久化格式、
   生命周期、容量、完整性和清理策略；当前`script_package_snapshot=false`阶段不扩大到该范围。

## 3. Revision v2 与历史读取合同

### 3.1 Canonical摘要

继续复用`SkillRuntimeState._fingerprint_roots()`产生的有序fingerprint。其每一项包含当前规范化文件路径、
文件大小和文件内容SHA-256；忽略规则继续排除`__pycache__`和`.pyc`。将现有canonical文本：

```text
<resolved path>\t<size>:<file sha256>\n...
```

编码为UTF-8后计算完整SHA-256，并生成：

```text
skillrev-v2-<64 lowercase hex>
```

同一部署环境中，只要挂载根路径和扫描时全部纳入fingerprint的文件完全相同，跨进程实例必须得到同一个
v2 revision。任一纳入文件的路径、大小或内容在下一次扫描时改变，都必须得到不同revision。内容改回原
状态时允许回到原revision，因为该revision表示扫描时的package fingerprint，不再表示进程内刷新次数。

该revision不替代package snapshot。当前脚本执行仍从只读挂载目录解析路径，因此设计只保证
catalog、capability映射、manifest和扫描时package fingerprint身份，不保证扫描后宿主机文件被替换时仍执行
原字节。严格可复现执行继续属于既有Phase 3 package snapshot范围。

### 3.2 内存快照与保留计数

`_bundles`、`retain_revision()`、`release_revision()`和inactive eviction继续按revision key工作：

- 同fingerprint的force refresh可以用同一个v2 key替换等价active bundle；
- 被运行中任务retain的不同内容revision继续保留在当前进程内；
- 如果内容已经从当前进程和挂载目录消失，重启后不能仅凭revision恢复其bundle；
- 不新增磁盘快照、数据库字段或隐藏fallback。

### 3.3 v1退役与历史可读

旧格式`skillrev-<6 digit counter>-<12 hex>`以及null/空revision在v2 runtime中全面退役：

- 任何规划、执行、恢复、Skill activation、补充输入或审批resume路径都必须显式携带合法v2；不得解析或
  执行v1/null/空revision；
- 执行解析必须在active bundle fallback或字典lookup前识别legacy revision并返回
  `agent_skill_bundle_revision_retired`；即使测试或注入状态中存在同名v1 key也不例外；
- 禁止按12位后缀建立alias，禁止改写prepared authority，禁止降级到active v2；
- 已完成、失败或取消的旧Task及其Message、AgentItem、Artifact和安全结果投影继续按现有持久化内容读取；
- 历史读取把revision视为opaque metadata，不调用Skill runtime，不读取Skill正文、manifest、脚本或资源；
- terminal Task的`list_interrupts()`读取不得触发Slot修复、resume调度或Skill执行；既有的安全历史消息投影
  行为不在本设计中改变；
- 若某个历史展示字段只能通过已退役bundle重建，该字段安全显示为unavailable，但不得让conversation、message
  或artifact列表接口整体失败。

### 3.4 对话继续执行边界

| 用户动作或状态 | v2 runtime行为 |
|---|---|
| 打开含v1记录的旧对话 | 读取既有持久化信息，不加载Skill bundle |
| 在旧对话发送新消息 | 创建全新Task/AgentRun并固定当前active v2 |
| 创建新对话并发送消息 | 创建全新Task/AgentRun并固定当前active v2 |
| 回答legacy Run遗留的补充输入 | Task terminal门在保存Answer前拒绝；用户通过新消息重发 |
| 审批legacy Run遗留的Tool调用 | Task terminal门在写Grant/pending action前拒绝且不得重放Tool；用户通过新消息重发 |
| 恢复exact可用的v2 Run | 按prepared authority正常retain并恢复 |
| 恢复不存在的合法v2 revision | 不加载active bundle，按Task当前状态终态化对应Run |

## 4. Startup recovery 数据流

```text
submission prepared projection
  → Skill recovery eligibility prepass
       → list recoverable Agent Runs through existing repository contract
       → page nonterminal submission Tasks with handed_off authority and no AgentRun
       → load and validate durable prepared authority only
       → classify Skill revision before active fallback/bundle lookup
            ├─ Run prepared missing / v1 / null / blank → terminalize Run/Task as retired
            ├─ valid v2 missing → terminalize owning Run/Task as unavailable
            ├─ unknown format → terminalize owning Run/Task as invalid
            └─ exact v2 exists → leave owning Run/Task unchanged
       → branch-specific terminal authority + best-effort current-task cleanup
  → existing MCP aggregate/recovery
       → terminal legacy Tasks are skipped by existing Task/Node status gates
  → recover projected submission handoffs
       ├─ exact v2 → existing handoff path
       ├─ validated prepared legacy / invalid / unavailable
            → materialize deterministic terminal object for original handoff kind
            → fail Task without loading Skill or executing work
            → acknowledge submission as handed_off
       └─ prepared snapshot missing / corrupt → block startup; no handoff or ack
  → normal Agent recovery
       → only exact-v2 Runs remain recoverable
  → startup completes only when no authority blocker remains
```

eligibility prepass必须放在submission coordinator完成prepared projection之后、
`MCPAggregateStartupReconciler`和`_reconcile_mcp_dispatch_recovery()`之前。候选集合来自既有
`list_recoverable_runs()`合同，以及`ACCEPTED`/`PLANNING`/`RUNNING`/`CANCELLING`、submission handoff已为`handed_off`且没有
AgentRun的Task。第二条通过现有SQL Task/Conversation/AgentRun投影增加private、有界、稳定游标分页reader取得
候选。SQL结果只作candidate index；每个候选在分类或写入前必须通过当前`TaskStoragePort`和已选择的
AgentRun repository重新读取并复验Task仍非终态、身份一致且仍无AgentRun，再按Task identity调用既有submission
preparation lookup验证handoff authority。`pending`记录留给4.1，不得被重复处理。不得为第一条强行增加
Runtime Sidecar分页协议；其现有backlog行为和合同不在本设计中改变。prepass只读取Run/Task/Conversation、
root Message和durable prepared authority，不读取当前用户输入、不刷新Server Profile、不执行Agent或任何Tool。

Skill revision classifier必须区分retired v1/null/blank、合法v2和unknown格式。已有AgentRun的prepared
authority完全缺失归入retired legacy；已`handed_off`但无AgentRun的Task若preparation/snapshot缺失、digest
不匹配、schema/relationship无效，无法安全证明原handoff kind或revision，必须保留原Task并阻断startup，
不得猜测或终态化。只有合法v2才能进入`bundle_for_revision()`。合法v2 lookup的`KeyError`转换为只在Skill
startup recovery边界消费的typed/private unavailable信号，不得携带或向前端输出完整revision、文件路径或
bundle内容。

prepass对retired/unavailable/invalid AgentRun通过现有Agent Loop terminal writer将Run、Task和适用open Node
收口：Task为ACCEPTED/PLANNING/RUNNING时使用fail writer并写入对应stable safe error，Task为CANCELLING时
使用现有cancel writer完成CANCELLED；已handoff但无AgentRun的Task按4.2收口。exact可用的v2 Run不得在
prepass中retain、初始化或恢复，继续由既有`_recover_agent_runs()`在MCP authority完成收敛后处理；exact
可用的v2 pre-AgentRun Task保持原waiting/terminal语义。正常Agent recovery不得保留“prepared缺失时绑定
active v2”的旧fallback；所有recoverable Run都必须具有显式、有效且exact可用的prepared v2 authority。

prepass完成后，已经形成AgentRun或handed-off authority的legacy Task不再是`RUNNING`，现有MCP recovery必须
沿既有Task/Node terminal状态门跳过，不claim resume outbox、不调用`_recover_agent_mcp_dispatch()`。仍为
`pending`的submission尚无AgentRun、MCP Call、resume outbox或可执行handoff authority，在MCP recovery期间
不得产生任何执行；随后只由4.1的claimed handoff路径处理。MCP revision仍沿现有逻辑处理，不得因本设计改变
其bundle失败或恢复语义。

### 4.1 Pending prepared submission 退役

`SubmissionAdmissionCoordinator`必须先完成prepared snapshot存在性、digest和schema/relationship
校验。snapshot本身缺失或损坏时，继续返回现有`submission_prepared_snapshot_missing`或对应的
prepared-validation错误，释放claim并让startup fail closed；不得构造terminal handoff、猜测
`planned_handoff_kind`/identity或写入acknowledgement。

仅对已完整校验的prepared snapshot，coordinator对`skill_bundle_revision`执行与AgentRun prepass相同的
分类。该门必须紧跟`_validated_prepared_snapshot()`，且位于`materialize_route_decision()`、
`materialize_memory_context()`、`materialize_selector_decision()`以及正常durable handoff callback之前；
被退役的submission不得先写route/memory/selector投影或任何执行副作用。该门只影响仍为`pending`的
handoff；已经`handed_off`的AgentRun由Run prepass处理，无AgentRun的非终态Task由4.2处理。

对已校验prepared中的v1、null、空、unknown或不可用v2，coordinator不得调用正常执行型handoff。它必须通过
新增的窄terminal-handoff callback，按prepared中已经冻结的`planned_handoff_kind`形成与现有
`_validate_handoff()`相同的kind和确定性identity：

| planned handoff kind | terminal materialization |
|---|---|
| `agent_run` | 创建或读取确定性`agent-run:<task_id>`，不写Skill activation、不采样；Task非CANCELLING时Run/Task为FAILED，Task已CANCELLING时Run/Task为CANCELLED |
| `interrupt` | 创建或读取原确定性Interrupt identity并直接保存为CANCELLED；Task非CANCELLING时FAILED，已CANCELLING时CANCELLED |
| `no_server_intent` | 创建或读取原确定性intent identity并收敛到terminal；Task非CANCELLING时FAILED，已CANCELLING时CANCELLED |

`agent_run`分支新增窄repository operation：SQL backend在一个现有事务内、Runtime Sidecar backend通过既有
`CommitAgentState` wire一次写入匹配Task当前状态的terminal AgentRun和Task；不新增Rust/proto字段或operation。
写入携带prepared中现有model binding facts和safe error，但不创建user/activation AgentItem。三种backend必须
对同一Run/Task/binding/error/terminal status exact replay，且不得先创建RUNNING Run再二次终态化。

`interrupt`和`no_server_intent`分支不宣称跨对象原子。它们在submission claim保护下按确定性identity执行
幂等物化，以Task terminal作为阻止执行的liveness authority，并把acknowledgement作为最后一步。任一步骤后
崩溃时，下次claim必须读取exact已有对象、补齐尚未完成的Task终态后再ack；identity或既有内容冲突则fail
closed。

terminal materialization返回原kind和原identity，coordinator继续使用现有
`acknowledge_submission_handoff()`写入`handed_off`。不新增handoff state、kind、数据库字段或Sidecar schema。
如果进程在terminal对象写入后、acknowledgement前崩溃，下次恢复必须exact replay同一terminal对象，再完成
acknowledgement；不得创建第二个Run/Interrupt/intent，也不得重复Event或副作用。

`agent_run` terminal handoff完成acknowledgement后，既有`wakeup_agent`即使被调用也必须识别Run已终态并
no-op；不得重新排队。任一terminal materialization或acknowledgement失败，submission claim按现有规则释放，
startup fail closed并在下次启动重试。

### 4.2 已 handed-off 的 pre-AgentRun Task 退役

prepass的第二条分页只处理同时满足以下条件的Task：

- Task状态为`ACCEPTED`、`PLANNING`、`RUNNING`或`CANCELLING`，且不存在AgentRun；
- submission preparation为`handed_off`，prepared snapshot及其digest、schema和relationship完整有效；
- handoff kind为`interrupt`或`no_server_intent`，handoff identity与prepared中冻结事实及现有确定性公式
  exact一致。

`handoff_kind=agent_run`但AgentRun不存在属于已ack authority损坏，必须阻断startup，不得伪造Run或当作legacy
退役；preparation/snapshot缺失或损坏也按4.1相同原则fail closed。已经terminal的Task不进入分页，且不得为了
更新错误文案而改写历史。

对`interrupt`候选，classifier在任何Skill lookup前判断revision。retired/invalid/unavailable时，新增的窄
retirement operation必须复验Task、prepared handoff identity与既有Interrupt identity，然后通过当前
Task-authority writer以CAS把`ACCEPTED`/`PLANNING`/`RUNNING` Task更新为`FAILED`；`CANCELLING` Task使用
现有允许的transition更新为`CANCELLED`。既有Interrupt和Node保持原状态，
作为历史证据保留；它们因Task terminal liveness gate不再可回答、resume或执行。重跑读取terminal Task即视为
收敛，不新增Interrupt、Node、Event或其他记录；任一authority不匹配或CAS冲突无法收敛时阻断startup。

异常的非终态`no_server_intent`候选先验证原确定性intent identity。Task非CANCELLING时复用既有exact
no-server convergence；Task已CANCELLING时只沿现有Task cancellation transition收敛，并保留intent作为历史。
不得改变MCP revision、恢复顺序或业务执行语义。该分支只修复“handoff已完成但Task仍非终态”的不一致，
已终态intent/Task保持不变。

这些记录已是`handed_off`，因此终态化后不再次acknowledge，也不调用`wakeup_agent`。Task终态提交成功后执行
与AgentRun分支相同的best-effort current-task CAS；即使CAS失败，后续新消息仍通过现有admission自愈清除指向
terminal Task的指针并创建当前v2 Task。

### 4.3 统一终态与执行入口门

已有AgentRun的Run/Task/适用Node原子terminal commit，以及pre-AgentRun分支的Task terminal CAS，是各自唯一
必需的失败authority。commit失败时startup继续fail closed；并发冲突只允许重新读取，若对应Task/Run已由其他
owner进入相同终态可继续，否则传播现有storage conflict。

`conversation.current_task_id`不在该terminal事务中。terminal commit成功后对它执行幂等best-effort CAS；
即使清理失败也不得把已经terminal的Run重新视为recoverable。后续新消息提交沿用现有admission自愈：如果
current pointer指向同conversation的terminal Task，先清空指针再创建新的v2 Task。不得把跨表两步虚称原子。

整个prepass退役分支不执行Agent sampling、Skill、MCP Tool、补充输入continuation或approval continuation，
不重放任何业务副作用。一个Task安全终态化后继续检查剩余候选；任一terminal authority写入失败仍阻断startup。

terminal commit后的OPEN Interrupt和pending approval记录可以作为历史证据保留，但不再具有actionability。
`answer_interrupt()`以及显式interrupt replay入口必须先读取Task/Run terminal状态，并在任何Answer、Grant、
MCP pending-action mutation或continuation之前通过现有终态/不再等待错误通道拒绝；不新增公开DTO或路由。

`list_interrupts()`调用的`_recover_missing_v2_slot_interrupts()`属于隐式恢复入口：读取到terminal Task时必须
在读取/修改SlotCollection、保存Interrupt/Message/SlotEvent或调度resume之前立即返回，随后列表接口只返回
已持久化Interrupt。该历史读取分支不得调用Skill runtime。

所有非新建执行的Skill解析必须使用exact revision。`_manifest_for_slot_collection()`不得捕获
catalog lookup失败后改用`active_bundle.catalog`；`_retain_task_skill_revision()`不得在既有Task的metadata
缺失revision时改用`active_revision`；`_recover_agent_run()`也不得在prepared authority缺失时绑定active。
普通`SkillToolExecutor._resolve_skill()`和`activate_delegated_skill()`必须在调用bundle resolver前拒绝
null/blank/v1/invalid revision，并把retired/invalid/unavailable映射为本设计对应的stable safe error；不得继续
沿用`skill_bundle_revision_missing`把不同状态混为一类，也不得把null转换成`None`后取得active bundle。
active v2只由新submission的prepared builder显式写入，随后同一revision沿prepared/Agent/Slot authority传递。
retired/invalid/unavailable在各自边界转为前述safe failure，不得以广义`except Exception`吞掉并fallback。

## 5. 错误与可见性

| 场景 | 行为 |
|---|---|
| 已校验pending prepared submission含legacy/invalid/unavailable Skill revision | 按原handoff kind物化确定性终态、Task按当前状态FAILED或CANCELLED并ack handed_off |
| pending submission的prepared snapshot本身缺失或损坏 | 维持现有fail-closed；不handoff、不ack、不猜测kind/identity |
| 已handoff、无AgentRun的非终态Interrupt Task含legacy/invalid/unavailable revision | exact校验handoff后以权威Task CAS完成FAILED或CANCELLED；Interrupt/Node保留为不可操作历史；不重复ack |
| 已handoff、无AgentRun的非终态Task缺失/损坏prepared authority | 保留Task并阻断startup；不猜测revision或handoff authority |
| 已handoff的`agent_run`不存在对应AgentRun | 视为authority损坏并阻断startup，不伪造Run |
| recoverable Run缺失prepared authority | 不绑定active v2；在prepass以retired终态化 |
| v1/null/空revision出现在任何执行/恢复路径 | 不fallback、不lookup、不执行；以retired终态化该Run/Task |
| legacy Task已经处于CANCELLING | 不恢复或执行；沿既有transition收敛Run/Task为CANCELLED，而非扩张Rust合同强制FAILED |
| v1/null/空revision出现在历史读取路径 | 作为opaque metadata读取；不访问Skill runtime |
| terminal legacy Task通过`list_interrupts()`读取 | 返回既有持久化Interrupt；不修复Slot、不调度resume、不写Slot相关记录 |
| v2 revision与当前bundle精确一致 | 正常retain并恢复 |
| 合法v2 revision不存在 | 不加载active bundle；以unavailable失败该Run |
| revision格式未知 | 不加载active bundle；以invalid失败该Run |
| bundle lookup内部出现非“revision不存在”错误 | 不降级，传播并阻断startup |
| 失败终态无法持久化 | 不跳过Run，startup fail closed |
| current task指针CAS失败 | Run保持terminal；后续submission按既有合同自愈 |
| terminal Task仍留有OPEN Interrupt/approval记录 | 保留历史记录但所有回答/授权入口在mutation前拒绝 |

面向用户的任务失败只暴露稳定safe error和通用安全文案。审计允许记录bundle kind、Run/Task现有安全标识
和错误类型，不记录Skill正文、文件列表、绝对路径或完整历史revision。

## 6. 修改边界

预计生产代码只涉及：

- `src/integrations/agent_skills/skill_runtime_state.py`：生成稳定v2 revision并拒绝执行v1；
- `src/capabilities/skill_tool/executor.py`：普通Skill执行要求显式exact v2并区分
  retired/invalid/unavailable，不允许null/blank取得active bundle；
- `src/api/submission_admission.py`：handoff前Skill eligibility gate和窄terminal-handoff callback；
- `src/api/runtime.py`：MCP recovery前的Skill eligibility prepass、prepared-missing fallback删除、Skill
  retired/unavailable/invalid信号、单Run终态隔离、指针best-effort清理、Interrupt/approval terminal
  liveness gate、Slot隐式恢复的terminal门、Slot/retain exact revision解析、三种确定性terminal handoff
  materialization和startup继续；
- `src/core/contracts.py`及现有SQLite/PostgreSQL storage repository：private分页读取非终态pre-AgentRun
  submission候选，并通过既有权威Task CAS失败已handoff legacy Task；不新增表或字段；
- SQL Agent repository、`RuntimeSidecarAgentRepository`及其窄repository contract：提供匹配Task当前状态的
  terminal AgentRun+Task一次性创建；Sidecar实现复用现有`CommitAgentState` wire，不修改Rust/proto；
- 必要时在现有Agent Loop私有模块中放置窄异常类型，但不新增公共API。

测试集中在：

- `tests/integrations/agent_skills/test_skill_runtime_state.py`；
- `tests/capabilities/skill_tool/`中的普通与delegated Skill revision分类/零active-fallback回归；
- `tests/api/test_submission_admission_recovery.py`；
- `tests/api/test_submission_admission_runtime_startup.py`；
- 相关storage repository的pre-AgentRun Task CAS、分页，以及三Agent backend terminal-create合同回归；
- 必要的现有Skill dynamic reload、conversation history、message submission、Interrupt/approval和runtime
  startup回归。

明确不新增或迁移数据库schema，不执行离线批量data migration，不删除或改写历史Message、AgentItem、
Artifact和prepared snapshot。runtime只能按分支写入本设计规定的幂等终态：既有Run分支写AgentRun、Task和
适用Node；pending handoff分支创建确定性的terminal AgentRun或Interrupt/no-server intent并终态化Task；
已handoff pre-AgentRun Interrupt分支只终态化Task；目标通常为FAILED，已经CANCELLING时为CANCELLED。
最后可best-effort更新current-task指针。不得为本设计新增
terminal Event或改写已handoff Interrupt/Node。
除此之外不修改MCP bundle recovery、MCP Result Parser/Projection Store、`MCPRuntimeState` revision格式、
Gateway/Selector、Frontend、Rust Sidecar协议、外部Skill内容、外部MCP Server、公开API DTO/路由和`prod`。

## 7. 验证要求

### 7.1 Revision单元测试

1. 两个独立`SkillRuntimeState`实例读取同一路径和扫描内容，得到完全相同的64位v2 revision；
2. 任一纳入fingerprint的文件在下一次扫描前变化后revision变化；内容恢复后revision恢复；
3. refresh后的旧bundle在retain期间仍可按exact revision读取，release后按既有规则淘汰；
4. v1即使作为字典exact key存在，也不得被任何执行resolver返回；null/空revision不得fallback到active；
5. 缺失v2或未知revision均不得返回active bundle；
6. 新写revision格式严格拒绝计数器、短摘要和非小写十六进制。

### 7.2 Startup recovery测试

1. prepared Run引用v1/null/空revision时，在active fallback或bundle lookup前终态化Run/Task/open Node；
   Task为ACCEPTED/PLANNING/RUNNING时以retired失败，Task为CANCELLING时完成取消；
2. prepared authority缺失不得绑定active v2，必须在prepass以retired失败；
3. 缺失合法v2以unavailable失败，未知格式以invalid失败，所有分支均不读取active bundle；
4. prepass严格发生在MCP aggregate/recovery之前；legacy Task终态后MCP recovery不得claim outbox或调用
   `_recover_agent_mcp_dispatch()`；
5. 第一条Run安全失败后，第二条有效v2 Run保持不变并在正常Agent recovery恢复，证明单Run不会退出应用；
6. terminal writer失败或bundle lookup内部非missing错误仍阻断startup；
7. current task CAS失败不撤销terminal authority；下一次submission清理terminal pointer并创建v2 Run；
8. terminal legacy Task即使仍有OPEN Interrupt或pending approval，直接answer和显式chat replay都必须在保存
   Answer、Grant或pending-action mutation前拒绝，不得执行或重放；
9. MCP missing revision行为保持现有基线，证明本设计没有扩张MCP bundle恢复语义；
10. `_manifest_for_slot_collection()`对retired/invalid/unavailable revision不得读取active catalog，
    `_retain_task_skill_revision()`对既有Task缺失revision不得绑定active；对应Task按本设计终态化；
11. 普通Skill executor和delegated activator对null/blank/v1/unknown/missing-v2分别返回retired、invalid或
    unavailable，且在注入可命中的v1 key时仍不调用Skill、不选择active bundle；
12. 原有prepared exact facts、waiting、lease retry、terminal release和动态Skill刷新测试保持通过。

### 7.3 Pending handoff测试

1. `agent_run`、`interrupt`和`no_server_intent`三类已完整校验的pending prepared legacy submission都不得调用
   route/memory/selector物化或正常handoff callback；
2. 三类均使用原planned kind和确定性identity形成terminal对象、将Task按当前状态收敛为FAILED或CANCELLED并
   ack `handed_off`；
3. 已校验snapshot内的v1/null/空、unknown和不可用v2覆盖相同terminal path，exact v2保持现有handoff；
4. prepared snapshot本身缺失、digest不匹配或schema/relationship无效时，保持现有fail-closed错误，
   不调用物化/terminal/normal handoff callback，不ack且不猜测kind/identity；
5. terminal materialization后、ack前故障重跑不产生第二对象、重复Event、Skill load、Agent sampling或Tool调用；
6. terminal `agent_run`被既有wakeup callback观察时no-op，不进入执行队列；
7. terminal materialization或ack失败保持claim/retry合同并阻断startup，不把pending记录静默丢弃；
8. pending `agent_run`在SQL和Runtime Sidecar三backend均以一次权威写入创建匹配Task状态的terminal Run+Task，
   不先创建RUNNING Run，不写user/activation AgentItem；Sidecar只使用现有`CommitAgentState` wire；
9. pending Interrupt/no-server在每个确定性步骤后的故障均可exact replay并以ack-last收口，不宣称跨对象原子；
10. Sidecar handoff state/kind、prepared snapshot schema、Rust/proto/codec和数据库schema保持零diff。

### 7.4 已 handed-off 的 pre-AgentRun Task 测试

1. private稳定游标分页覆盖所有`ACCEPTED`/`PLANNING`/`RUNNING`/`CANCELLING`且无AgentRun的Task，只对`handed_off` preparation执行
   revision分类；SQL候选逐条通过当前Task authority和Agent repository复验。既有`list_recoverable_runs()`
   合同不变，`pending`、已终态Task和已有AgentRun的Task不得重复处理；
2. 已handoff initial Interrupt的v1/null/空、unknown和不可用v2分别按retired、invalid和unavailable终态化；
   仅Task通过当前authority CAS转为FAILED或CANCELLED，Interrupt/Node保持历史状态且完整v2保持可回答；
3. legacy Interrupt为OPEN/ANSWERED/EXPIRED/CANCELLED中的任一状态但Task仍非终态时，都保留Interrupt/Node
   历史状态，只按Task当前状态完成FAILED或CANCELLED；重跑不新增记录或产生状态回退；
4. Task、Interrupt或handoff identity不匹配，prepared snapshot缺失、digest不匹配或schema/relationship
   无效，以及`handoff_kind=agent_run`但AgentRun不存在时均阻断startup且不写部分终态；
5. 异常非终态`no_server_intent`在非CANCELLING时复用既有exact convergence，在CANCELLING时只完成Task取消
   并保留intent历史；MCP revision和正常MCP recovery基线不变；
6. CANCELLING legacy候选通过现有允许的transition收敛为CANCELLED；其他三种非终态收敛为FAILED，且均不
   修改Rust生命周期合同；
7. legacy pre-AgentRun Task终态后，旧Conversation仍可读且新消息清理terminal current pointer并创建v2
   Task/AgentRun。

### 7.5 历史读取与对话继续测试

1. 含v1/null metadata的completed/failed/cancelled旧对话可读取conversation、message、Task和已有Artifact；
2. 历史读取期间Skill runtime lookup调用次数为0；只能依赖已持久化安全投影的字段不可重建时，局部显示
   unavailable而非接口失败；
3. 在旧对话提交新消息时使用当前active v2，prepared authority、Agent visibility和Skill activation均不含v1；
4. 新对话提交保持同一v2行为；
5. terminal legacy Task存在ready/waiting SlotCollection时，`list_interrupts()`只返回已有Interrupt，
   `_recover_missing_v2_slot_interrupts()`不保存Interrupt/Message/SlotEvent、不调度resume且Skill lookup为0；
6. 不删除、不迁移、不改写任何历史legacy Message、AgentItem、Artifact或prepared snapshot。

### 7.6 相关门禁

运行聚焦Skill/API startup测试，然后运行Integrations、Orchestration、API和E2E相关回归、compileall、Ruff、
package import与`git diff --check`。如果实施后发布新backend镜像，还需独立验证远端OCI `linux/amd64`、
镜像不含`/app/config.yaml`。开发环境部署前只读分别统计：非终态prepared-missing/v1/null/空revision
Agent Run，已handoff且无AgentRun的非终态submission Task，已校验snapshot含非v2 revision的pending
submission，以及snapshot本身缺失或损坏的pending/handed-off submission startup blocker。部署后证明已有
Run和pre-AgentRun legacy Task都在MCP recovery前被终态化，已校验的legacy pending submission按原handoff
kind terminalize并ack，且三者均无业务执行副作用；已handoff initial Interrupt只终态化Task，原Interrupt/Node
保持历史状态且不可操作；snapshot缺失/损坏记录仍fail closed且不新增ack。同时证明API
成功启动、旧对话可读、旧/新对话的新消息均创建v2
Run，并验证一个v2 Run在fingerprint未变化时完成重启恢复。构建、推送、部署和远端数据终态化仍需实施
阶段的明确授权。

## 8. Rollout 与回退

首次部署revision v2 binary前必须只读分别统计非终态prepared-missing/v1/null/空revision Agent Run、
已handoff且无AgentRun的非终态submission Task、已校验snapshot含非v2 revision的pending submission，以及
snapshot本身缺失/损坏的pending/handed-off startup blocker。部署后v2 runtime在任何MCP recovery前终态化
已有Run和已handoff pre-AgentRun legacy Task，并在submission handoff边界终态化已校验且尚未handoff的
legacy记录，不恢复、改写或重放；snapshot缺失/损坏的pending记录继续fail closed且不ack，已handed-off记录
不新增ack。
已持久化历史继续可读。新对话和旧对话的新消息统一写入v2 revision。

任一pending/handed-off authority blocker计数非零时不得启动v2或恢复流量；本设计不自动修复、删除或猜测这些
记录。必须中止本次发布，并在尚未写入任何v2 authority时回到原binary，或另行批准精确的数据处置方案。

该发布必须是writer hard cut：先停止并确认全部v1 backend writer退出，再启动任何v2实例；禁止v1/v2混跑或
rolling overlap。若无法证明旧writer已经退出，停止发布。v2 readiness成功且上述legacy收口证据完成后才允许
恢复流量，避免旧实例在prepass之后再次创建v1 prepared authority。

一旦任何环境写入v2 prepared authority，不得回退到只能理解旧计数器revision的binary。安全cutback下限
必须保留v2生成和读取；可以向前修复startup隔离，但不能把v2 revision截短或改回v1。回退业务部署前需
先证明没有非终态v2 Run，否则停止。

## 9. 完成定义

只有以下条件全部满足，才可声明仓库实施完成：

- 新写Skill revision稳定为`skillrev-v2-<64 hex>`且同一扫描fingerprint跨独立进程状态可复现；
- v2 runtime不执行任何v1/null/空revision；历史legacy对话可读且读取路径不访问Skill runtime；
- active v2只在新submission preparation时选择；prepared/Agent/Slot后续解析全部要求exact revision，
  retention、slot manifest、普通Skill executor、delegated activator和Agent recovery均无active fallback，
  且统一区分retired/invalid/unavailable；
- 旧对话和新对话的新消息统一创建v2 Task/AgentRun，legacy pending input/approval不迁移或重放；
- retired legacy、缺失v2和未知revision均不加载active/current内容、不改写prepared authority；
- 所有四种非终态Task均被覆盖：ACCEPTED/PLANNING/RUNNING legacy收敛为FAILED，CANCELLING legacy沿现有
  状态机收敛为CANCELLED；没有legacy Task继续可执行；
- terminal legacy Task的所有Interrupt/approval入口在任何authority mutation前拒绝；
- terminal Task的`list_interrupts()`读取不触发Slot修复、resume调度、Skill lookup或Slot相关持久化写入；
  既有安全历史消息投影保持不变；
- Skill eligibility prepass在任何MCP recovery前终态化所有非v2 recoverable Run；prepared缺失不得绑定active v2；
- prepass同时终态化已handoff、无AgentRun且prepared有效的非v2 submission Task；initial Interrupt和Node
  保持历史状态且只由权威Task CAS将Task收敛为FAILED或CANCELLED，authority损坏则不写部分状态并阻断startup；
- 已完整校验的pending prepared non-v2 submission按原handoff kind形成幂等terminal对象并ack handed_off，
  不重复恢复；snapshot本身缺失/损坏时保持fail closed且不ack；
- v2发布前全部v1 writer已经停止，发布过程无v1/v2 overlap，readiness成功并完成legacy收口后才恢复流量；
- 单个revision失败Run安全终态化且不阻断其他Run与应用startup；
- 失败持久化异常继续fail closed；
- terminal current pointer可由best-effort CAS和既有submission admission确定性收敛；
- MCP bundle recovery保持零行为变化；
- 所有聚焦与相关自动门禁通过；
- 无数据库schema迁移或无关data mutation；持久化data变化严格限于本设计逐分支列出的幂等终态与best-effort
  current-task指针，不修改Frontend、Rust、MCP parser/projection或`prod`。

设计经12轮审查/修订循环，以98/100、0 Blocking、0 Major、2 Minor通过完整信心门。实施计划见
`2026-09-04-skill-bundle-revision-v2-restart-recovery-implementation-plan.md`；当前尚未修改生产代码、
处理远端失败Run、构建镜像或部署。

License Requirement：复用现有Python、SHA-256 fingerprint、Agent Run terminal writer、prepared authority与
unittest；不新增依赖、第三方代码或许可变化。
