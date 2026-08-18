# MCP Dispatch 聚合状态机与恢复加固设计

## 状态

- 日期：2026-08-18
- 结论：设计已确认，等待形成实施计划并进入代码实现
- 适用范围：当前 `main` 分支 user-scoped MCP 的 SQL authority、Coordinator、Gateway、
  Tool approval、MRTR、remote Task 和启动恢复路径
- 前置设计：
  `2026-08-18-mcp-dispatch-reference-resume-envelope-design.md`

## 背景与证据

最新本地测试任务在 Tool approval 后已经建立 `may_have_dispatched=true` 的 Call，远端
响应返回后创建 CP7 terminal candidate 时，运行时传入 UTC-naive 时间，而 candidate
合同要求 timezone-aware UTC 整秒。candidate 未能封存，通用异常处理随后只把 Task
标记为 `failed`，留下 MCP Node=`running`、Call=`active`、intent=`dispatched`、
outbox=`claimed`。服务重启后又因 Task 已终态而跳过 MCP 聚合收敛，因此不一致状态被
永久保留。

审查同时确认，这不是单一时间戳缺陷。当前 Tool approval、普通连续调用、MRTR、remote
Task、terminal candidate、dispatch finalizer 和 startup recovery 分别修改 Task、Node、
branch、Call、intent、outbox、Interrupt、Answer、sealed state 与 remote binding，缺少
一个共同的原子状态合同。局部修补无法保证其他崩溃边界安全。

现有相关测试可以通过，但 Coordinator 单测普遍不启用真正的 CP7 terminal store，
MRTR 使用 fake storage，remote Task 测试也没有覆盖完整 `runtime.start()` 恢复顺序，
所以没有验证生产 authority 的组合路径。

## 目标

1. 修复当前 terminal candidate UTC 时间错误，并安全收敛已经产生的不一致聚合。
2. 建立一套覆盖普通 Tool、approval、MRTR 和 remote Task 的持久化状态机。
3. 所有跨 Task/Node/branch/Call/intent/outbox 的转换由 Repository 聚合事务完成。
4. 任何崩溃点只能恢复为安全续跑、保持等待、可信终结或 unknown/no-replay。
5. 审批后执行原先被批准的精确 Tool action，不因重新运行 Selector 产生重复审批。
6. 保持 CP7 “candidate 先封存、数据库 receipt 后提交”的证据顺序。
7. 保持 MCP dispatch v2 引用式信封和 64 KiB 上限，不把实际 I/O、Tool 参数、附件正文
   或 Base64 放回信封。

## 非目标

- 不扩展 Sidecar enforce authority 或设计跨 SQL/Sidecar 的新原子协议。
- 不改变 Tool Grant 的 owner、Server、Tool、schema、安全版本边界。
- 不把附件正文自动桥接为 MCP Tool 参数，不新增 OCR 文件传输协议。
- 不自动重放任何 `may_have_dispatched=true` 且缺少可信终态证据的调用。
- 不复活旧失败任务；需要重新执行业务时创建新任务。
- 不重写 legacy v1 resume envelope 或迁移历史 intent 内容。
- 不修改普通非 MCP Interrupt、Storage mutation 或全局任务生命周期语义，除非它们直接
  承载 MCP approval 的原子提交。

## 方案选择

评估过三种方案：

1. 局部修补时间和异常分支。改动小，但会继续保留 MRTR、连续调用、重复审批和启动
   恢复中的不同状态合同。
2. 保留现有 CP7/SQL authority，新增 MCP 聚合状态机和少量持久化字段，将转换集中到
   Repository 事务。该方案可分阶段交付，且不需要重写整个运行时。
3. 将 MCP 生命周期整体改为事件溯源。边界最统一，但迁移、回滚和验证范围远超本次
   修复。

本设计采用方案 2。

## 架构边界

- Coordinator 负责发现、Selector 决策、审批编排和调用 Gateway，但不得自行拼接多次
  Storage mutation 来表达一个业务状态转换。
- Repository 是 MCP 聚合状态转换的单一 writer。SQLite 和 PostgreSQL 必须实现相同
  事务合同和 CAS 语义。
- Gateway 只在 Repository admission 成功并持有有效 dispatch claim 后允许网络调用。
- CP7 candidate file 仍先于数据库事务封存；数据库 terminal commit 消费 candidate，
  原子写 receipt 并更新 Call/branch/outbox。
- Tool arguments 不进入 resume envelope。待审批参数由现有 MCP recovery 加密领域密封，
  pending action 只保存密封引用和摘要。
- startup recovery 以持久化证据优先级驱动，不从内存推断执行状态。

## 聚合状态合同

### Intent

Intent 保留主生命周期：

```text
armed -> available -> dispatched -> resolved
                              \-> unknown
```

- `available` 表示尚无 Tool 可能发送。
- 第一个 Tool admission 原子转换为 `dispatched`。
- `dispatched` 可以跨多个 ordinary Call、MRTR round 或 remote Task 存活。
- `resolved` 表示整个 dispatch 已结束，不只是某一个 Call 已完成。
- `unknown` 表示至少一个 Call 可能已发送但缺少可信终态，永不自动重放。

### Dispatch outbox

Outbox 从一次性 resume claim 扩展为 dispatch 监督状态：

```text
pending -> claimed -> active
                    |-> waiting_approval -> claimed
                    |-> waiting_input    -> claimed
                    |-> remote_pending   -> active
                    \-> completed | aborted
```

规则：

- `pending`、`waiting_approval` 和 `waiting_input` 只有满足相应恢复前置时才可 claim。
- `claimed` 必须具有 owner、token、lease；admission 必须提交精确 token。
- Coordinator 处于 Selector 或网络调用阶段时为 `active`，并定期续租。
- 进入等待用户或 remote Task 状态时释放 dispatch claim；remote worker 使用其现有独立
  binding claim。
- 单次 Tool terminal receipt 不再提前把 outbox 标为 `completed`。
- 只有 dispatch finalizer 或 unknown convergence 可以进入 terminal outbox 状态。

### Call 与 branch

```text
reserved -> active
            |-> completed
            |-> failed
            |-> cancelled
            |-> input_required
            |-> remote_pending
            \-> unknown
```

- `completed`、`failed`、`cancelled`、`input_required` 和 `unknown` 都结束当前网络 round，
  必须在同一事务中清除 `branch.active_call_ref`。
- `remote_pending` 保留 logical Call 非终态，并由 remote binding 证明恢复 authority；
  branch 不允许并发第二个 Tool。
- terminal candidate commit 必须同时更新 Call、receipt、branch 和 outbox 的 last receipt，
  但 outbox 保持 `active`，允许 Selector决定继续下一 Tool或结束 dispatch。

### Pending Tool action

新增 `MCPPendingToolAction`：

```text
proposed -> waiting_approval -> approved -> consumed
                            \-> denied | invalidated
```

字段闭集：

- `action_id`、owner、conversation、Task、Node、Server、Tool；
- canonical arguments SHA、现有 approval fingerprint、`sealed_arguments_ref`；
- Server config/security version、Tool input schema SHA；
- status、revision，以及 created/approved/consumed/invalidated 时间；
- approval Interrupt ID 和 accepted Answer ID。

实际 arguments 使用现有 MCP recovery cipher 和 sealed-state authority，
`state_kind=pending_tool_action`。pending action row、resume envelope、Interrupt public payload、
audit 和日志都不得包含参数正文。附件正文不能成为 sealed action 的隐式复制；如 Tool
需要文件，只能使用后续明确设计的持久化文件引用合同。

`always_allow` 必须在接受 Answer 的同一事务中保存或精确复用 Grant，然后批准当前精确
action。Grant 不能替代 action 本身：恢复仍执行被批准的参数，不重新运行 Selector。

## Repository 聚合事务

### `suspend_mcp_for_approval`

输入 Selector action、closed Server/Tool版本、密封参数引用和 expected revisions。事务内：

1. 再验证 Task、Node、intent、outbox、Server 和 branch；
2. 写 pending action；
3. 写唯一 open approval Interrupt；
4. Node 进入 `waiting_for_input`；
5. outbox 进入 `waiting_approval` 并释放 claim。

精确重试返回同一 action/Interrupt；内容漂移返回 conflict，不创建第二份审批。

### `accept_mcp_tool_approval`

使用 Interrupt、action 和 expected revisions 做行锁/CAS。事务内只允许 open Interrupt 和
waiting action 接受一个 Answer，并同时更新 Interrupt、Answer、Node、action，以及
`always_allow` Grant。并发第二次提交返回 already-answered 或 conflict，不得产生第二个
accepted Answer。

`deny` 将 action 标为 denied，并调用统一 no-call dispatch finalizer；`allow_once` 和
`always_allow` 将 action 标为 approved，Node 进入 `ready_to_resume`。

### `claim_mcp_dispatch`

从满足前置的 `pending`、`waiting_approval` 或 `waiting_input` CAS 到 `claimed`，写 owner、
token、lease 和 revision。已过期 claim 可回收；未过期的其他 owner claim 不可接管。
Coordinator 在 Selector 和普通 Call 执行期间续租。claim 丢失后不得继续新的网络发送；
若发送状态已经不确定，则进入 unknown/no-replay。

### `admit_approved_mcp_action`

事务内验证 Task running、Node可执行、intent/outbox、claim token、pending action状态、
Grant/Answer、Server config/security、Tool schema、branch预算及无 active Call。随后创建
Call、设置 branch active、消费action，并在首个Call时把intent转换为`dispatched`、
outbox转换为`active`。只有该事务成功后Gateway才可释放网络调用门。

对于无需审批的有效Grant，Coordinator仍先创建精确pending action，但可以在同一聚合
事务中直接批准并admit，避免形成另一条admission状态机。

### `commit_mcp_call_terminal`

读取已经封存的 candidate，验证 owner/Task/Node/intent/Call/Server版本和payload摘要。
事务内幂等写 terminal receipt，更新Call终态，清除branch active ref，记录outbox last
receipt并保持outbox=`active`。相同candidate重试返回already-committed；同Call不同
candidate为authority corruption。

### `suspend_mcp_for_input`

事务内保存或精确比较 MRTR sealed state，创建唯一 input Interrupt，将当前Call标为
`input_required`，清除branch active，Node和outbox进入waiting状态并释放claim。
accepted Answer必须由durable Answer重建，不能从resume envelope恢复。

### `publish_mcp_remote_task`

事务内发布 remote binding，将Call/branch/outbox转换为`remote_pending`，Node进入
`waiting_for_dependency`并释放dispatch claim。remote worker只通过binding claim轮询，
不得重新发送原 `tools/call`。

remote terminal candidate提交后，Call和branch终结，outbox回到`active`，再由durable
continuation恢复Selector/finalizer。

### `finalize_mcp_dispatch`

FINISH、STOP、deny、selector invalid、step limit、明确失败、取消都通过同一接口。它根据
是否曾有Call和last receipt选择closed completion mode：

```text
completed
stopped_no_call
stopped_after_call
failed_no_call
failed_after_call
cancelled_no_call
cancelled_after_call
unknown_no_replay
```

事务原子终结intent、outbox、branch和MCP Node；required Node失败时同时失败Task并阻断
未执行下游。completed/stopped Node按现有completion policy允许下游finalizer继续。

### `converge_mcp_unknown_no_replay`

锁定整个聚合，验证至少一个Call具有`may_have_dispatched=true`且缺少可信receipt、
candidate、remote binding或MRTR等待证据。事务内把相关Call标为unknown，清除branch，
终结intent/outbox/Node/Task，写唯一projection和no-replay事件。Task已经failed不能提前
返回；只有聚合全部满足终态不变量时才返回already-converged。

## 时间合同

新增唯一 CP7 封存辅助函数：

```text
canonical_utc_second(now) -> timezone-aware UTC, microsecond=0
```

普通、失败和remote Task三条candidate创建路径都必须使用该函数。SQLAlchemy当前
DateTimeText和Task生命周期字段继续使用UTC-naive时间；本轮不全局切换数据库时间类型。
测试必须拒绝naive candidate时间，并覆盖非UTC aware输入被规范到UTC整秒。

## 启动恢复

对每个 active 或不一致聚合执行固定顺序：

1. 先读取数据库中未完成的GC marker并完成或回滚受控删除，再安全枚举 terminal
   candidate，按receipt识别已消费项；
2. candidate存在、receipt缺失时幂等提交receipt和Call终态；
3. active remote binding存在时保持`remote_pending`，交给worker claim；
4. MRTR sealed state存在时，open Interrupt保持等待，accepted Answer恢复精确continuation；
5. pending action存在时，open审批保持等待，approved action恢复精确调用；
6. `available`且没有Call时验证v1/v2信封并回收过期claim；
7. `may_have_dispatched=true`且以上可信证据均不存在时unknown/no-replay；
8. 验证聚合不变量，全部满足后才允许Ready。

startup reconciliation 必须先分类remote binding、MRTR和pending action，再启动 ordinary
unknown convergence。不能先把全部dispatched intent收敛为unknown后再启动remote worker。

以上第3至第6步只适用于Task仍running且未请求取消的聚合。Task已经终态或已取消时，
不得新发 `tools/call`、MRTR continuation或remote polling；有可信candidate/receipt时提交
证据，否则把残留的nonterminal authority收敛为unknown/no-replay。completed receipt可以
恢复Selector决定下一步，failed/cancelled receipt只能进入dispatch finalizer，不能再次
调用Selector。

### Ready策略

- schema、digest、identity、owner、candidate fork或receipt冲突属于authority corruption，
  阻断Ready且不改写证据。
- 附件正常失效、dependency不可恢复、审批因版本漂移失效等只收敛对应Task，不阻断服务。
- remote Server暂时不可达由worker backoff处理，不阻断Ready。
- Task已终态但MCP聚合未终态属于可收敛不一致；完成安全收敛后可以Ready。

## 错误语义

| 边界 | 结果 |
|---|---|
| admission前失败 | no-call finalizer，不创建`may_have_dispatched` Call |
| 远端明确失败 | seal failed candidate并提交可信终态 |
| 发送后超时、断连或结果不确定 | unknown/no-replay |
| 远端成功但本地candidate无法封存 | unknown/no-replay |
| candidate已封存、DB提交失败 | startup从candidate幂等提交 |
| receipt已提交、Coordinator崩溃 | 从receipt继续Selector/finalizer，不再调用已完成Tool |
| 等待approval或MRTR时重启 | 保持等待或按durable Answer精确恢复 |
| remote Task轮询中重启 | 从binding恢复，绝不重发原Tool |
| 用户在发送前取消 | cancelled_no_call |
| 用户在发送后取消且无可信cancel receipt | unknown/no-replay |

预期业务错误映射为closed safe code；`execution_crash`不得把任意`str(exc)`发送给前端。
完整异常只允许进入现有脱敏日志，且不得包含Tool arguments、用户输入、credential、
endpoint或结果正文。

## 强制不变量

1. 终态Task不得关联非终态MCP Node、Call、intent或outbox。
2. `branch.active_call_ref`只能引用同一聚合的非终态Call。
3. `waiting_approval`必须且只能有一个open approval Interrupt和一个waiting action。
4. `waiting_input`必须有sealed state和一个open MRTR Interrupt。
5. `remote_pending`必须有active remote binding。
6. `may_have_dispatched=true`且没有可信终态时只能unknown/no-replay。
7. 没有有效dispatch claim token不得admit或发起网络调用。
8. 一个Interrupt最多一个accepted Answer。
9. terminal outbox不得再次admit Tool。
10. consumed pending action不得重新执行。

## 现有不一致任务

升级后的首次startup恢复必须识别以下形态：Task已failed、MCP Node仍running、Call active且
`may_have_dispatched=true`、intent dispatched、outbox claimed、无candidate/receipt。
它必须调用unknown聚合收敛：Task保持failed，Node failed，Call unknown，branch清除，
intent unknown，outbox completed/unknown_no_replay，并写唯一projection。不得重新调用
OCR Tool。业务需要重试时创建新Task。

## Candidate消费与GC

数据库terminal receipt是candidate已消费的权威证据。startup枚举时必须把receipt绑定的
candidate ID传给unconsumed reader，避免每次重放所有已提交candidate。

GC采用两阶段、可恢复流程：

1. 在数据库记录candidate已消费及`gc_eligible_at`；默认至少保留7天；
2. janitor只删除同时具有精确receipt、已过保留期且不涉及late-result未决projection的
   candidate、task index和call index；
3. 删除前写`deleting` GC marker，三个文件都不存在后才写`deleted`；删除中崩溃时，
   startup必须先根据receipt和GC marker完成这次受控删除，再运行严格目录枚举，不能把
   预期的部分删除识别为corruption；
4. unexpected缺文件、额外文件、binding漂移仍阻断Ready。

目录项数在硬上限前增加容量告警。硬上限不提高，也不以无界扫描替代GC。

## Task级预算与执行单航班

- branch继续持久化20次Tool Call预算。
- 新增Task/Node级累计Selector step和approval round计数，跨Interrupt和重启不重置。
- 现有每次调用64步限制改为聚合生命周期上限；达到上限统一failed_after/no-call finalize。
- `_schedule_execution`必须以Task generation/token做CAS式单航班。旧handle结束时只能删除
  自己的generation，不能按`task_id`无条件移除新handle。
- `_await_existing_execution`增加有界等待；超时后不并发启动第二执行，而是交给startup/
  claim恢复或安全失败。

## Observability

新增audit-only状态转换只记录closed字段：

```text
operation
from_state
to_state
result
reason_code
call_sequence
selector_step_total
approval_round_total
recovery_outcome
```

不得记录Tool arguments、用户输入、附件信息、原始业务ID、SHA、credential、endpoint或
结果正文。现有安全引用只有在对应事件合同已经允许时才可继续使用。

新增低基数指标：

- `mcp_aggregate_inconsistency_total`
- `mcp_recovery_outcomes_total`
- `mcp_pending_approval_seconds`
- `mcp_pending_input_seconds`
- `mcp_duplicate_approval_prevented_total`
- `mcp_terminal_candidate_inventory`
- `mcp_terminal_candidate_gc_total`
- `mcp_claim_conflict_total`

指标写入失败遵守现有metric-gap合同，不得改变已经确定的业务终态。

## 实施阶段

### Phase 0：P0止血

- 接入统一UTC整秒封存时钟；
- 修复post-admission异常收敛；
- 终态Task不再跳过active MCP聚合；
- 增加现有坏任务形态回归并在启动时安全收敛。

### Phase 1：聚合事务

- 迁移SQLite/PostgreSQL outbox状态、completion mode和revision约束；
- 实现claim、admission、terminal commit、dispatch finalize和unknown convergence；
- terminal commit清除branch active；单次Call不再提前完成outbox；
- 所有Coordinator退出分支接入统一finalizer。

### Phase 2：持久化审批action

- 新增pending action与sealed arguments引用；
- approval open/answer/action/Node使用原子CAS；
- approval恢复执行精确action；
- claim token进入admission，版本漂移使旧action失效。

### Phase 3：MRTR与remote Task

- 接入`waiting_input`和`remote_pending`状态；
- 修复MRTR continuation admission；
- 调整startup恢复顺序；
- remote terminal统一UTC时钟和聚合commit；
- sealed state在continuation取得明确结果后才删除。

### Phase 4：长期加固

- 持久化Selector/approval预算；
- candidate消费和GC；
- 安全错误映射；
- 修复执行handle竞态；
- startup聚合不变量扫描与指标。

每个Phase必须形成独立、可回滚checkpoint。Phase 0可以先发布，但在Phase 1至Phase 3
完成前不得宣称整个MCP恢复闭环已加固。

## 测试与故障注入

必须在以下持久化边界模拟进程崩溃：

1. intent armed后、outbox创建前；
2. pending action保存后、Interrupt提交前；
3. Answer原子提交期间及并发双提交；
4. Call保留后、网络门释放前；
5. `may_have_dispatched`后、远端响应前；
6. 远端响应后、candidate封存前；
7. candidate封存后、receipt提交前；
8. receipt提交后、branch清理前；
9. Call完成后、dispatch finalizer前；
10. MRTR sealed state保存前后；
11. remote binding发布前后；
12. candidate GC marker与文件删除之间。

每个场景断言网络调用为零或精确一次、聚合满足不变量、有可信candidate时恢复结果、
无可信结果且可能已发送时unknown/no-replay，并且只有authority corruption阻断Ready。

功能和兼容回归必须覆盖：

- 当前2.3 MB图片对应的v2信封仍小于4 KiB且不含实际I/O或Tool参数；
- 两次连续ordinary Tool调用；
- 首次OCR不同Tool可以分别审批，同一action不重复审批；
- `always_allow` Grant持久化后相同Tool不再审批；
- approval Answer并发双击只接受一个；
- MRTR等待、Answer提交、continuation执行三个阶段跨重启；
- remote Task创建、轮询、input required和terminal阶段跨重启；
- STOP、selector invalid和step limit发生在已有成功Call之后；
- Task已failed但Call仍active的启动收敛；
- active legacy v1 intent；
- SQLite和真实PostgreSQL的事务回滚、CAS并发和schema约束；
- 前端approval后SSE继续接收后续状态；
- candidate GC保留期、late result、部分删除恢复和目录容量告警；
- MCP、storage、orchestration、API、frontend及Rust相关质量门禁。

## 迁移与回滚

- schema migration为additive：新增pending action表、必要sealed-state kind、outbox状态、
  completion mode、计数/revision和candidate consumption/GC字段。
- 迁移完成后先运行只读聚合分类：`available + pending/claimed`保持原状态；存在open
  approval和匹配action时映射`waiting_approval`；存在sealed MRTR/open Interrupt时映射
  `waiting_input`；存在active remote binding时映射`remote_pending`；已存在receipt但
  Node/intent未终结时映射`active`并恢复finalizer/Selector；Task已终态且Call仍active的
  形态交给unknown convergence。分类不能只依据旧outbox=`completed`，因为旧实现可能在
  单次Call receipt后提前完成outbox。authority corruption继续阻断Ready。
- 不改写legacy v1 envelope正文，不自动复活旧失败任务。
- 新版本产生`active`、`waiting_approval`、`waiting_input`或`remote_pending` outbox后，旧版
  二进制不认识这些状态，禁止直接回滚启动。
- 回滚前必须停止新提交，并证明不存在上述active状态、pending action、未完成MRTR或
  remote binding；否则继续用新版本完成收敛。
- 本设计只面向`main`开发环境，不构成`prod`部署或CP7-B退役授权。

## 完成标准

只有以下条件全部满足，才能声明本设计实施完成：

1. 最新故障形态能够在零网络重放下收敛；
2. 普通多Call、approval、MRTR和remote Task共用已定义的聚合状态机；
3. 所有Repository转换在SQLite/PostgreSQL具有等价原子/CAS测试；
4. 全部12个崩溃边界通过故障注入；
5. 启动Ready前不变量扫描通过；
6. v2引用式信封、64 KiB上限和no-replay原则保持；
7. candidate消费/GC不会把预期删除误判为corruption；
8. 相关回归与`git diff --check`通过，且没有把业务I/O或敏感内容加入审计。

## 已记录假设

- 当前本地和本轮主要验收环境以SQL authority为准。
- 现有MCP recovery cipher可以安全承载`pending_tool_action` sealed state；若实现时发现其
  大小合同不足，只允许新增独立加密blob引用，不得把参数退回resume envelope或数据库
  明文字段。
- remote binding worker现有claim/lease机制继续保留；本设计只调整它与dispatch聚合的
  先后关系和终态提交。
- 现有completion policy继续决定completed/stopped MCP Node后的下游执行；聚合事务负责
  保证MCP自身authority一致，并在required失败时同步失败Task。
