# MCP Dispatch 聚合状态机与恢复加固设计

## 状态

- 日期：2026-08-18
- 结论：**96% — Pass with recorded assumptions**；已通过95%信心门，Blocking=0、Major=0
- 实施状态：2026-08-19 已在`main`完成Phase 0～4仓库实现与本地SQLite v6 cutover；
  实施后审计提交`96f7c65`进一步闭合十阶段真实consumer、keyset分页、post-ready网络边界、
  candidate容量告警及durable result backfill/orphan/Artifact接管；未执行`prod`部署，真实
  PostgreSQL validation DSN与用户OCR人工smoke仍是外部完成证据。
- 适用范围：当前 `main` 分支 user-scoped MCP 的 SQL authority、Coordinator、Gateway、
  Tool approval、MRTR、remote Task 和启动恢复路径
- 前置设计：
  `2026-08-18-mcp-dispatch-reference-resume-envelope-design.md`

## 复审记录

用户已授权在95%信心门内自主修改和循环复审。本次按五个闭环轮次执行：范围/权威收缩、
consumer与状态转换核对、跨重启I/O与并发安全、迁移/兼容/验收追踪、跨文档最终一致性。

| 维度 | 置信度 | 最终结论 |
|---|---:|---|
| 目标、范围与非目标 | 98% | SQL authority和非目标边界明确 |
| 状态机、事务与consumer | 96% | Blocking/Major为0，resume cursor与单一writer闭合 |
| 安全、隐私与容量 | 96% | 32 MiB加密参数、64 MiB结果、64 KiB信封互不混用 |
| 崩溃恢复、并发与GC | 96% | 17个边界、no-replay、candidate/result marker闭合 |
| 迁移、兼容与回滚 | 95% | v1/v2双读、受控cutover和旧binary门禁明确 |
| 可观测性与验收证据 | 96% | 18项FR、8项NFR全部映射自动化证据 |

最终文档置信度为96%。保留的假设在文末列明；该分数评价设计本身。实现状态与验证缺口以
对应implementation plan第16节为准，不把本地SQLite/CI证据等同于真实PostgreSQL或生产部署。

## 设计优先级

- `2026-08-18-mcp-dispatch-reference-resume-envelope-design.md` 的 v2 引用式信封、64 KiB
  上限、legacy v1 reader和禁止实际I/O进入信封的合同保持不变。
- 本设计取代 `2026-08-13-user-mcp-cp7-manual-retirement-design.md` 中“任一普通Call
  terminal receipt立即完成整个dispatch outbox/intent/Task/Node”的单Call口径；terminal
  receipt现在只终结当前Call，整个dispatch只由统一finalizer终结。CP7 safety ledger、
  candidate先于receipt、late-result、no-replay和人工退役门禁不变。
- 若其他旧文档与本设计的多Call、approval、MRTR、remote Task或outbox状态冲突，以本设计
  为准；实施时必须同步旧文档的交叉引用，不得保留两个都声称权威的合同。

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

## 参与者、依赖与受影响系统

| 参与者/系统 | 本设计中的职责或影响 |
|---|---|
| 最终用户 | 发起MCP任务、批准/拒绝精确Tool action、提交MRTR输入并看到安全状态 |
| API Runtime / Orchestration | 调度单航班、打开/恢复Node、投递frontend事件，不再拼接MCP聚合写入 |
| Coordinator / Selector | 生成下一精确action；审批恢复时消费持久action，不重新选择 |
| Gateway / Adapter | 在admission barrier后发送网络请求；预封存MRTR/remote evidence和durable result |
| SQL Repository | SQLite/PostgreSQL的MCP聚合单一writer、CAS、锁序、恢复分类和projection |
| CP7 terminal store | 保存严格封闭的terminal candidate active/archive工件，不保存业务正文 |
| MCP recovery cipher | 复用现有MCP recovery密钥领域，加密pending-action payload、MRTR和remote ID |
| MCP result store | 保存跨重启可验证的content-addressed Tool结果，受容量和保留策略约束 |
| remote Task worker | 使用binding claim轮询；不能重发原`tools/call`，终态通过聚合writer提交 |
| 前端SSE/Interrupt UI | 保持现有审批字段和重订阅行为，不读取内部action/payload引用 |

实现依赖现有 `StoragePort`、owner mutation guard、Task/Node生命周期、CP7 candidate/receipt、
MCP recovery cipher、temporary-result容量门禁、remote binding/outbox和frontend closed event
parser。不得引入新的外部依赖或密钥领域。

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
- 所有authority Call的normalized Tool结果必须先进入跨重启、content-addressed durable result
  store，再封存CP7 candidate；数据库 terminal commit同时验证结果manifest、内容SHA和
  字节数，消费candidate并原子写receipt、Call、branch和outbox。
- Tool arguments 不进入 resume envelope。待审批参数使用现有MCP recovery密钥领域加密，
  但保存在独立的immutable pending-action payload store；action row只保存引用和摘要。
- startup recovery 以持久化证据优先级驱动，不从内存推断执行状态。

### 大payload authority

pending-action payload store与terminal candidate store相互独立：

- 单个canonical arguments JSON明文最大32 MiB，足以覆盖当前20 MiB上传上限经Base64和
  JSON编码后的开销；超过限制在approval Interrupt前以
  `mcp_pending_action_payload_too_large`失败，不截断、不写resume envelope。
- payload使用AES-GCM和现有MCP recovery派生密钥，AAD精确绑定action、owner、Task、Node、
  Server、Tool、config/security version、schema SHA和arguments fingerprint。
- 新`MCPPendingActionPayloadCipher`直接接受已canonicalize的UTF-8 bytes，不提高现有
  `MCPRecoveryCipher.MAX_TASK_PRIVATE_JSON_BYTES`。binary file v1固定为8-byte magic
  `MAFMPA1\0`、2-byte big-endian encryption version、12-byte随机nonce、8-byte big-endian
  ciphertext length及ciphertext+16-byte tag；长度、magic、version和trailing bytes必须
  exact。AAD使用独立`pending_action_payload\0v1`前缀，96-bit nonce每次由OS CSPRNG生成。
- 文件发布采用`0600`、no-clobber、file+directory fsync、secure read和内容摘要复验；目录
  为`0700`，禁止symlink、hardlink、owner/mode或inode漂移。
- payload文件必须在action事务前封存；事务失败留下的无引用文件在24小时安全期后清理。
  waiting/approved action的payload不得清理；action consumed且对应Call已有可信终态或
  unknown projection后可以删除。
- 写入前后继续执行现有临时磁盘low-watermark容量检查；ENOSPC/EDQUOT在当前Call admission
  前不创建新Call，并由历史Call ledger选择`failed_no_call|failed_after_call`；在可能发送后
  按unknown/no-replay处理。
- 每个backend进程的payload seal/unseal使用容量1的专用async gate，等待期间继续续租dispatch
  claim并响应取消；这接受AES-GCM one-shot实现，不留下“实现时再决定分块格式”的分支。
  32 MiB边界压测必须证明单次seal/unseal峰值RSS增量不超过128 MiB且不会阻塞claim续租。

本设计不把上传文件额外复制到pending payload。Selector arguments若包含文件内容，只有
该内容本来就是精确Tool参数时才进入加密payload；未来文件引用桥接仍属于非目标。

### Durable Tool result authority

- authority路径不得使用进程内memory ref或会被`close_task`删除的non-promoted ref。
- 单个normalized Tool result硬上限为64 MiB，与现有CP7 Artifact secure-read上限对齐；所有
  ordinary、MRTR terminal和remote Task结果在流式写入时计数。第64 MiB后的首个字节使sink
  abort并生成closed `mcp_result_too_large` failed candidate；若failed candidate也无法封存，
  则按可能已发送的unknown/no-replay处理，绝不以截断结果继续Selector。
- result ref必须是owner/task/call-owned、content-addressed durable ref；manifest绑定owner、
  Task、Node、Call、ref、content SHA、字节数和store kind，读取时必须逐字节复验。
- result bytes与manifest均使用no-clobber发布、file+directory fsync和secure read；相同
  task-owned ref已存在时只能逐字段/逐字节exact-idempotent，同ref不同内容、manifest或scope
  属于authority corruption，禁止当前`os.replace`式覆盖。
- 新writer生成`maf.user_mcp.cp7.terminal_result_candidate.v2`，在v1字段之外增加
  `safe_result_content_sha256`、`safe_result_size_bytes`和
  `safe_result_store_kind=durable_content_addressed`。failed/cancelled时三项必须为null。
- terminal receipt同步升级为v2字段合同：新增nullable result content SHA、size和store kind；
  新v2 completed candidate提交时三项必填且必须与candidate/manifest/正文完全一致，v1历史
  receipt允许为null。receipt schema迁移只新增nullable列，new writer不得写不完整v2 receipt。
- reader继续接受已存在的exact v1 candidate；v1 completed candidate只有在其result ref仍
  可解析且实际内容/manifest通过复验时才可信。旧v1 ref正常缺失时只失败对应Task并进入
  no-replay，不把不可恢复的业务结果伪装为成功；candidate fork、摘要或身份漂移仍阻断
  Ready。
- active dispatch、未完成continuation和未决late-result引用的result不得删除。dispatch
  resolved后保留至少24小时恢复宽限；已提升为Artifact的结果由Artifact保留策略接管，
  未提升结果在宽限结束后清理。terminal candidate archive只保留摘要和引用，不延长业务
  结果正文的保留期。

### Durable result retention与GC

新增`mcp_durable_result_lifecycle`可变SQL行，append-only terminal receipt保持不变。字段
闭集为owner/Task/Node/Call/ref、content SHA/size、data/manifest精确相对文件名及各自文件SHA、
`retained|artifact_owned|deleting|deleted`、reason=`dispatch_resolved|artifact_promoted|orphan`、
eligible/updated/deleted时间和revision：

- terminal commit在同一事务insert-or-compare `retained`行；dispatch仍active时
  `eligible_at=NULL`，finalizer按`resolved_at+24h`设置eligible。
- Artifact promotion只有在Artifact已独立持久化并逐字节复验后，才能CAS为`artifact_owned`
  并立即eligible；candidate archive本身不能触发result删除。
- result已落盘但candidate/receipt/lifecycle都不存在时，startup先按manifest绑定的Call
  分类；仍可能恢复的Call保留，已unknown/terminal且安全期满后insert `reason=orphan`行。
- janitor只按SQL keyset每批最多1000行选择eligible记录，再重验没有nonterminal dispatch、
  candidate提交或Artifact复制缺口，CAS到`deleting`，按marker精确删除data+manifest、fsync
  目录，最后写`deleted`。startup在验证active result前先完成`deleting` marker。
- 没有marker的半删除、同ref不同内容或active result缺失按Ready策略处理；已`deleted`且没有
  active consumer是预期状态，不参与result store strict load。

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
                    |-> waiting_approval -> pending
                    |-> waiting_input    -> pending
                    |-> remote_pending   -> pending
                    \-> completed | aborted
```

规则：

- 只有`pending`可claim；approval/MRTR的accepted Answer或remote terminal聚合提交必须先把
  waiting状态CAS回`pending`，然后由唯一consumer claim。
- `claimed`和`active`必须具有owner、token、lease；其他状态必须清空三者。admission、
  ordinary terminal commit和finalizer必须提交精确token与expected revision。
- Coordinator claim成功后为`claimed`，admission或开始恢复Selector时转`active`并保留
  同一claim。claim TTL固定30秒，独立续租间隔不超过10秒，不依赖120秒用户可见heartbeat。
- 进入等待用户或 remote Task 状态时释放 dispatch claim；remote worker 使用其现有独立
  binding claim。
- `active` claim过期后，startup先按Call/candidate/receipt/pending evidence分类：没有
  `may_have_dispatched`可回`pending`；有可信terminal evidence可恢复；可能已发送但无可信
  evidence只能unknown/no-replay。
- 单次 Tool terminal receipt 不再提前把 outbox 标为 `completed`。
- 只有 dispatch finalizer 或 unknown convergence 可以进入 terminal outbox 状态。

Outbox新增closed resume cursor，避免另建含糊的continuation command：

```text
resume_reason = initial | ordinary_terminal | approval_accepted | mrtr_answer | remote_terminal
resume_receipt_id = terminal reasons时必填，否则null
resume_answer_id = user-answer reasons时必填，否则null
```

三者与outbox revision同一CAS更新。`initial`两类引用都必须null；
`ordinary_terminal|remote_terminal`只允许receipt；`approval_accepted|mrtr_answer`只允许accepted
Answer。claim/retry不得由caller改写cursor，Repository从锁内receipt/Answer推导；相同revision
同cursor幂等，不同cursor冲突。首次创建outbox写`initial`；ordinary completed commit即使
保持`active`也写`ordinary_terminal`和receipt；approval/MRTR Answer分别写
`approval_accepted|mrtr_answer`；remote completed写`remote_terminal`。

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
- ordinary completed candidate commit必须同时更新Call、receipt、branch和outbox的last
  receipt，并在有效dispatch claim下保持outbox=`active`，允许Selector继续；ordinary
  failed/cancelled candidate直接进入统一finalizer。
- remote worker以binding claim提交completed candidate时，把outbox从`remote_pending`
  变为`pending`并把resume cursor设为`remote_terminal`；failed/cancelled remote candidate在
  同一聚合事务中finalize。remote binding claim不能用于普通Tool admission。

### Pending Tool action

新增 `MCPPendingToolAction`：

```text
proposed -> waiting_approval -> approved -> consumed
                            \-> denied | invalidated
proposed ---------------------> approved -> consumed
```

字段闭集：

- `action_id`、owner、conversation、Task、Node、Server、Tool；
- canonical arguments SHA、现有 approval fingerprint、`arguments_payload_ref`、payload file
  SHA/字节数/encryption version；
- Server config/security version、Tool input schema SHA；
- status、revision，以及 created/approved/consumed/invalidated 时间；
- approval Interrupt ID 和 accepted Answer ID。

actual arguments只存在于前述加密pending-action payload store。现有`MCPSealedState`
继续只承载已有Call绑定的MRTR request state，不以虚构`call_ref`复用来保存审批前action。
pending action row、resume envelope、Interrupt public payload、audit和日志都不得包含参数
正文。

`always_allow` 必须在接受 Answer 的同一事务中保存或精确复用 Grant，然后批准当前精确
action。Grant 不能替代 action 本身：恢复仍执行被批准的参数，不重新运行 Selector。

### Approval API与前端兼容

- 现有`mcp.tool_approval_required`事件名、`interrupt_id`、Server/Tool安全显示名、参数摘要和
  `safe_call_ref`字段保持兼容；审批前尚无Call时，该字段承载由action派生的opaque
  `safe_action_ref`，名称仅为前端兼容，不能被后端当作Call authority。
- Interrupt public `required_fields`保持现有`mcp_tool_approval` closed options、
  `approval_ref`、`server_id`和`tool_name`兼容形态，不新增action ID或payload ref；这些字段
  只用于展示/旧客户端匹配，后端通过Interrupt ID反查pending action并视客户端字段为不可信。
- approval提交DTO和HTTP状态保持现有合同；重复提交返回现有already-answered语义。SSE重订阅
  从durable EventRecord/Interrupt恢复，不要求前端持有payload ref，也不新增客户端重试协议。
- UI只能展示安全摘要并发送`deny|allow_once|always_allow`；后端必须以Interrupt绑定的action
  为authority，拒绝客户端替换Server、Tool、arguments SHA或版本。

## Repository 聚合事务

### `suspend_mcp_for_approval`

输入Selector action、closed Server/Tool版本、已预封存的加密payload引用和expected
revisions。Repository取得owner/Server/intent/outbox锁后、写action前secure-read并验证payload
文件SHA/大小/AAD和canonical arguments SHA。事务内：

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

`deny`在同一聚合事务中将action标为denied，并以`stopped`调用统一dispatch finalizer；
finalizer按此前是否已有Call选择`stopped_no_call|stopped_after_call`。不能先提交Answer再从
事务外调用finalizer。`allow_once`和
`always_allow` 将 action 标为 approved，Node 进入`ready_to_resume`，outbox从
`waiting_approval`回到`pending`并写`approval_accepted` cursor。API Runtime必须在识别
`reason_code=mcp_tool_approval_required`后直接调用该聚合接口，不能先走通用
`InterruptService.record_answer`分步写入。

### `claim_mcp_dispatch`

只从满足前置的`pending` CAS到`claimed`，写owner、token、30秒lease和revision。已过期
claim可回收；未过期的其他owner claim不可接管。
Coordinator 在 Selector 和普通 Call 执行期间续租。claim 丢失后不得继续新的网络发送；
若发送状态已经不确定，则进入 unknown/no-replay。

### 取消与admission线性化

MCP取消入口与admission/finalizer使用同一owner mutation guard。线性化点是
`admit_approved_mcp_action|admit_mrtr_continuation`事务提交：

- 取消先取得guard时，事务原子写cancel request并走`cancelled_no_call|cancelled_after_call`
  finalizer；后续admission必须因Task/Node/outbox终态失败，Gateway不得发送新请求。
- admission先提交时，Call已经是`may_have_dispatched=true`，随后取消只能走现有best-effort
  transport/remote Task cancel；没有可信cancel receipt时按unknown/no-replay，不能声称未调用。
- Gateway在真正transport write前再次验证本地claim仍有效并检查durable cancel flag；该检查
  缩小竞态但不改变上述线性化点。commit后即使实际尚未写socket，也按may-have-dispatched
  保护，绝不因猜测“可能没发”而重放。

### `admit_approved_mcp_action`

事务内验证 Task running、Node可执行、intent/outbox、claim token、pending action状态、
Grant/Answer、Server config/security、Tool schema、branch预算及无 active Call。随后创建
Call、设置 branch active、消费action，并在首个Call时把intent转换为`dispatched`、
outbox转换为`active`。只有该事务成功后Gateway才可释放网络调用门。

该接口必须保留现有CP7 admission参数和安全门禁：candidate/epoch成对出现、candidate guard
未invalid、exact epoch已有Ready且未closed/invalidated、Task为user-scoped enforce、shadow
关闭、八条detector已配置，以及Gateway `authorization_verified=true`。任一门禁缺失均不得
创建Call或释放网络门。

对于无需审批的有效Grant，Coordinator仍先创建精确pending action，但可以在同一聚合
事务中直接批准并admit，避免形成另一条admission状态机。

### `commit_mcp_call_terminal`

读取已经封存的candidate，验证owner/Task/Node/intent/Call/Server版本、candidate schema、
payload摘要，以及completed结果的durable manifest、内容SHA和字节数。事务内幂等写
terminal receipt，更新Call终态，清除branch active ref并记录outbox last receipt。

- ordinary completed通过dispatch claim提交并保持outbox=`active`；
- ordinary failed/cancelled在同一事务中finalize；
- remote completed通过exact binding claim提交并把outbox转`pending`、写`remote_terminal`
  resume cursor；
- remote failed/cancelled通过binding claim提交并finalize。

相同candidate重试返回already-committed；同Call不同candidate、同ref不同manifest或结果
内容漂移均为authority corruption。

### `suspend_mcp_for_input`

Adapter在解析完整、合法的`input_required`响应后，先用现有recovery cipher预封存
request state、validated input requests、Tool、原action/arguments payload ref及Call身份，
不复制arguments正文，并保持现有64 KiB sealed-state上限；它尚不改变Node或
outbox。`suspend_mcp_for_input`事务secure-read并精确采用这份prepublished evidence，创建
唯一input Interrupt，将当前Call标为`input_required`，清除branch active，Node/outbox进入
waiting状态并释放claim。
accepted Answer必须由durable Answer重建，不能从resume envelope恢复。

崩溃发生在prepublication后、aggregate adoption前时，startup可从完整evidence创建相同
Interrupt并进入`waiting_input`；evidence缺少input requests或身份漂移时不得猜测，进入
unknown/no-replay。Answer原子提交后outbox回`pending`并写`mrtr_answer` cursor，continuation
取得claim后才能发送。

### `admit_mrtr_continuation`

MRTR continuation不是新的Selector action，不能伪造pending action或重复请求Tool审批。
Repository在事务内验证原Call=`input_required`、唯一accepted Answer、sealed evidence、原始
approval/Grant、Server config/security和Tool schema未漂移、dispatch claim token及预算，
然后创建带`continuation_of_call_ref`的新Call、设置branch active并把outbox转`active`。
Gateway只在该admission成功后发送原Tool、原arguments、request state和durable Answer重建的
input responses；原arguments必须从evidence绑定的pending payload secure-read，不能从resume
envelope、Interrupt或客户端重建。版本漂移发生在网络门前时使continuation失效并安全finalize；网络门后结果
不确定仍按unknown/no-replay。sealed state只有在continuation取得可信terminal evidence或
unknown projection后删除。

### `publish_mcp_remote_task`

Adapter验证CreateTask响应后先保存`published_at=NULL`的加密remote binding。聚合事务
secure-read并采用该prepublished binding，原子设置`published_at`、Call/branch/outbox
`remote_pending`和Node=`waiting_for_dependency`，然后释放dispatch claim。remote worker
只通过binding claim轮询，不得重新发送原`tools/call`。

崩溃发生在binding prepublication后、aggregate adoption前时，startup只有在Call、Task、
Node和binding身份完整一致时才采用；否则终结binding并把可能已发送的Call收敛为unknown。
remote completed terminal提交后，Call/branch终结，outbox回`pending`，再由durable resume
cursor和dispatch claim恢复Selector；failed/cancelled直接finalize。

remote Task自己的`input_required`继续由remote binding/outbox、durable Interrupt/Answer和
`tasks/update`合同处理；dispatch outbox在此期间保持`remote_pending`，不得误用MRTR的
`waiting_input`或重发原`tools/call`。Task/Node取消后remote worker不得继续poll/update。

### `finalize_mcp_dispatch`

FINISH、STOP、deny、selector invalid、step limit、明确失败、取消都通过同一接口。它根据
是否曾有Call和last receipt选择closed **dispatch outbox completion mode**：

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

这些值不替代terminal receipt的
`normal_terminal_projection|late_result_no_continuation`；两个completion mode字段必须使用
不同Enum和validator，禁止交叉写入。

事务原子终结intent、outbox、branch和MCP Node；required Node失败时同时失败Task并阻断
未执行下游。completed/stopped Node按现有completion policy允许下游finalizer继续。

### `converge_mcp_unknown_no_replay`

锁定整个聚合，验证至少一个Call具有`may_have_dispatched=true`且缺少可信receipt、
candidate、remote binding或MRTR等待证据。事务内把相关Call标为unknown，清除branch，
终结intent/outbox/Node/Task，写唯一projection和no-replay事件。Task已经failed不能提前
返回；只有聚合全部满足终态不变量时才返回already-converged。

### Selector恢复投影

普通completed receipt提交后即使Coordinator崩溃，恢复也不得依赖旧进程内的Selector
context。初次执行与恢复都通过同一builder重建：

- 用户请求、binding、附件摘要和dependency projection按v2引用式信封设计从Task、root
  Message、TaskInputAttachment及Artifact authority读取并复验快照；
- completed result refs按`call_sequence`升序从Call+v2 receipt重建，逐个验证durable result
  manifest；Selector只接收opaque refs，不把Tool结果正文注入prompt；
- failed/rejected arguments fingerprint、剩余Call预算、累计Selector/approval计数从SQL
  authority读取；
- 当前Server依次取approved pending action、最新Call、branch initial Server；无持久副作用的
  route decision可以在恢复后重新选择，但已批准action或已admit Call的Server绝不能漂移；
- MRTR continuation使用专用admission，不进入普通Selector builder；remote completed后只有
  terminal commit已写`remote_terminal` resume cursor时才恢复Selector。

任一必需result ref缺失按Ready策略失败对应Task；顺序、Task/Call归属、receipt或manifest
冲突属于authority corruption。builder输出必须canonical可比较，测试要求同一持久化快照在
崩溃前后逐字段相同。

### SQL锁序与单一writer

所有上述接口、MCP approval API和remote worker聚合提交必须先进入同一owner mutation
guard，并遵守固定顺序：

```text
owner guard -> target Server -> intent -> dispatch outbox -> pending action
-> branch -> Call(s) -> terminal candidate secure-read -> terminal receipt/projection
-> remote binding -> remote task outbox -> dispatch resume cursor -> Task -> Node
-> Interrupt -> Answer -> Grant
```

已有action的pending payload在action行锁后secure-read；创建action时则在锁定outbox后、插入
action前secure-read预封存payload。它不是SQL锁。SQLite使用同一`BEGIN IMMEDIATE`
事务，PostgreSQL扩展现有`_run_cp7_authority_sync`按上述顺序`FOR UPDATE`。remote worker和
generic Interrupt路径不得绕过owner guard写同一MCP聚合。锁序必须用并发测试证明，没有
仅靠调用方约定的旁路writer。

## 时间合同

新增独立、可注入的CP7 terminal clock：

```text
terminal_now_fn() -> timezone-aware UTC, microsecond=0
```

默认实现直接使用`datetime.now(timezone.utc).replace(microsecond=0)`；普通、失败和remote
Task三条candidate创建路径都必须调用该clock。不得给任意naive datetime静默附加UTC，
candidate validator继续拒绝naive或非整秒输入。SQLAlchemy当前DateTimeText和Task生命周期
字段继续使用独立的UTC-naive SQL clock；本轮不全局切换数据库时间类型。测试必须覆盖
aware非UTC输入由测试clock调用方先规范到UTC、naive拒绝和三条writer的一致结果。

## 启动恢复

对每个 active 或不一致聚合执行固定顺序：

1. 先读取数据库中未完成的candidate archive/GC及durable result lifecycle marker，并完成或
   回滚受控文件移动/删除；
2. 严格枚举active terminal candidate root，并按receipt与archive marker分类；
3. candidate存在、receipt缺失时，取得对应dispatch或binding recovery claim后幂等提交；
4. active remote binding存在时保持`remote_pending`，启动完成后交给worker claim；
5. MRTR prepublished evidence存在时，open Interrupt保持等待，accepted Answer把outbox转
   `pending`并恢复精确continuation；
6. pending action存在时，open审批保持等待，approved action把outbox转`pending`并恢复
   精确调用；
7. `available`且没有Call时验证v1/v2信封并回收过期claim；
8. `active`且claim过期时，按Call、candidate、receipt和prepublished evidence决定回
   `pending`、可信恢复或unknown；
9. `may_have_dispatched=true`且以上可信证据均不存在时unknown/no-replay；
10. 验证聚合不变量，全部满足后才允许Ready。

startup reconciliation 必须先分类remote binding、MRTR和pending action，再启动 ordinary
unknown convergence。不能先把全部dispatched intent收敛为unknown后再启动remote worker。
startup只做SQL/本地工件恢复，不访问远端MCP Server。active聚合使用status、updated_at和
stable ID的keyset pagination，每批最多1000条；不得把全部历史terminal outbox加载到
内存，也不得用固定10,000条总扫描上限把正常历史增长误判为corruption。

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
- completed receipt引用的durable result必须可解析；业务结果正常缺失只失败对应Task，
  内容/manifest或candidate身份冲突才属于authority corruption。

## 错误语义

| 边界 | 结果 |
|---|---|
| 当前Call admission前失败 | 不创建新`may_have_dispatched` Call；按历史Call选择`failed_no_call|failed_after_call` |
| 远端明确失败 | seal failed candidate并提交可信终态 |
| 发送后超时、断连或结果不确定 | unknown/no-replay |
| 远端成功但本地candidate无法封存 | unknown/no-replay |
| candidate已封存、DB提交失败 | startup从candidate幂等提交 |
| receipt已提交、Coordinator崩溃 | 从receipt继续Selector/finalizer，不再调用已完成Tool |
| 等待approval或MRTR时重启 | 保持等待或按durable Answer精确恢复 |
| remote Task轮询中重启 | 从binding恢复，绝不重发原Tool |
| 用户在当前Call admission前取消 | 按历史Call选择`cancelled_no_call|cancelled_after_call` |
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
6. `may_have_dispatched=true`且既没有可信终态、也没有MRTR sealed evidence或active remote
   binding等可信非终态恢复证据时，只能unknown/no-replay。
7. 没有有效dispatch claim token不得admit或发起网络调用。
8. 一个Interrupt最多一个accepted Answer。
9. terminal outbox不得再次admit Tool。
10. consumed pending action不得重新执行。
11. `claimed|active` outbox必须持有未过期claim，其他outbox状态不得保留claim字段。
12. completed candidate的durable result ref、manifest、内容SHA和字节数必须一致。
13. waiting/approved pending action必须有唯一可secure-read的加密payload，payload不能被
    resume envelope、event、日志或数据库明文字段复制。

## Functional requirements

| ID | 必须满足的行为 | 权威章节 |
|---|---|---|
| FR-001 | v2 resume envelope保持引用式、64 KiB和legacy v1 reader | 目标、设计优先级 |
| FR-002 | 三条terminal writer只使用aware UTC整秒clock | 时间合同 |
| FR-003 | authority Tool结果在candidate前durable化，candidate v2绑定内容SHA/大小 | Durable Tool result authority |
| FR-004 | outbox状态、claim shape、TTL/renew和过期恢复精确满足合同 | Dispatch outbox |
| FR-005 | approval前精确action及最大32 MiB arguments由独立加密payload authority持久化 | Pending Tool action、大payload authority |
| FR-006 | approval Interrupt、唯一Answer、action、Node、outbox和always-allow Grant原子CAS | `accept_mcp_tool_approval` |
| FR-007 | admission验证claim、action、Server/schema/security、预算和全部CP7安全门禁 | `admit_approved_mcp_action` |
| FR-008 | terminal commit原子更新receipt/Call/branch/outbox；整个dispatch只由finalizer终结 | terminal commit、finalizer |
| FR-009 | MRTR prepublication/adoption、waiting、Answer和continuation跨重启且不重发已知Call | `suspend_mcp_for_input` |
| FR-010 | remote binding prepublication/adoption、poll、terminal和continuation不重发原Tool | `publish_mcp_remote_task` |
| FR-011 | startup按固定evidence优先级、本地分页恢复并在不变量通过后Ready | 启动恢复 |
| FR-012 | 可能已发送且无可信证据时只允许unknown/no-replay，包括已有terminal Task | unknown convergence |
| FR-013 | candidate active/archive/30天删除流程可恢复且不破坏strict enumeration | Candidate消费与GC |
| FR-014 | 20 Call、64 Selector step、20 approval round和Task执行单航班跨重启生效 | Task级预算与执行单航班 |
| FR-015 | approval事件/DTO/SSE保持兼容，客户端不能替换action authority或看到实际参数 | Approval API与前端兼容 |
| FR-016 | ordinary completed后从SQL及持久ref生成与崩溃前逐字段相同的Selector context | Selector恢复投影 |
| FR-017 | cancel与admission按owner guard单赢家，取消后不产生新网络调用 | 取消与admission线性化 |
| FR-018 | durable result保留、Artifact接管、orphan和两文件GC均由可恢复marker驱动 | Durable result retention与GC |

## Non-functional requirements

| ID | 可验证要求 |
|---|---|
| NFR-001 Security | 参数/结果正文不得进入resume envelope、event、audit、metric或明文DB；payload/candidate/result secure-read必须拒绝link、mode、owner、inode或摘要漂移 |
| NFR-002 Reliability | 17个持久化边界崩溃后网络调用为零或精确一次，且聚合收敛到可信终态、等待或unknown/no-replay |
| NFR-003 Concurrency | SQLite使用`BEGIN IMMEDIATE`，PostgreSQL遵守统一owner锁序；双Answer、双claim、双Call和双candidate均单赢家 |
| NFR-004 Capacity | pending action单项32 MiB、crypto gate=1、单次RSS增量≤128 MiB；normalized result单项64 MiB；active candidate 8,000告警/10,000硬上限；startup每批最多1000条且不全量读取terminal历史 |
| NFR-005 Compatibility | 新writer candidate v2、reader v1/v2；resume envelope v1/v2不变；forward cutover无旧新writer混跑，旧binary不读新Enum |
| NFR-006 Privacy/retention | pending orphan安全期24小时；业务result在active/continuation期间保留，resolved后至少24小时；candidate archive保留30天；Artifact使用自身策略 |
| NFR-007 Observability | 所有event/metric使用closed schema和低基数labels；metric失败不改变业务终态 |
| NFR-008 Operability | authority corruption阻断Ready；正常单Task不可恢复只失败该Task；startup不访问远端Server |

## 现有不一致任务

升级后的首次startup恢复必须识别以下形态：Task已failed、MCP Node仍running、Call active且
`may_have_dispatched=true`、intent dispatched、outbox claimed、无candidate/receipt。
它必须调用unknown聚合收敛：Task保持failed，Node failed，Call unknown，branch清除，
intent unknown，outbox completed/unknown_no_replay，并写唯一projection。不得重新调用
OCR Tool。聚合收敛复用已有`task.failed`事件作为projection的Task失败证据，并新增
audit-only `mcp.aggregate_reconciled_after_task_failure`；不得发送第二条用户可见
`task.failed`。如果已有失败事件缺失或与Task身份不一致，才按authority corruption阻断
Ready。业务需要重试时创建新Task。

## Candidate消费与GC

数据库terminal receipt是candidate已消费的权威证据。现有
`enumerate_unconsumed_terminal_result_candidates`即使传入consumed ID也仍会读取并验证整个
目录，因此本设计不把该参数误当成容量或启动性能优化。

新增独立、可变的`mcp_terminal_candidate_lifecycle`表；append-only receipt保持不变。行
字段至少包括candidate/call/Task、candidate schema、active/archive三个精确相对文件名及
文件SHA、receipt ID、`retained|archiving|archived|deleting|deleted`、revision、
consumed/eligible/updated时间。marker只能由精确receipt派生，不能由caller提供文件名。

采用active/archive两层流程：

1. active root只保存unconsumed candidate和已消费但尚未归档的短暂工件；仍执行当前完整
   relationship graph严格枚举。
2. receipt及`retained` marker在同一数据库事务提交后，candidate即可归档；后续幂等提交和
   late-result lookup必须同时支持active/archive，不能以“调用方已收到响应”作为不可证明的
   归档前置。janitor CAS到`archiving`，以no-clobber方式把candidate、task
   index和call index三件套移动到同文件系统的`0700` archive root，fsync两个目录，再写
   `archived`。
3. archive工件保留30天；secure lookup先查active marker再查archive marker，因此幂等
   commit重试和审计读取不需要全量扫描archive。
4. 30天后CAS到`deleting`，安全删除archive三件套并fsync目录，全部不存在后写`deleted`。
5. startup在严格枚举active root之前先完成`archiving|deleting` marker。marker记录的精确
   文件集合允许恢复部分移动/删除；没有marker的缺文件、额外文件、symlink/hardlink、
   摘要或binding漂移仍阻断Ready。

active root目录项达到8,000时触发容量告警和立即归档扫描；10,000硬上限保持。archive按
marker做每批最多1000条keyset分页，不进入Ready全量扫描。candidate归档/删除不改变前述
业务result正文的独立保留策略。

## Task级预算与执行单航班

- branch继续持久化20次Tool Call预算。
- 新增Task/Node级累计Selector step和approval round计数，跨Interrupt和重启不重置。
- Selector生命周期硬上限为64步；approval round硬上限为20轮。因Server/schema/security漂移
  invalidated的审批也计入round，防止外部配置抖动形成无界Interrupt。达到上限统一走
  `failed_no_call|failed_after_call` finalizer。
- `_schedule_execution`必须以Task generation/token做CAS式单航班。旧handle结束时只能删除
  自己的generation，不能按`task_id`无条件移除新handle。
- `_await_existing_execution`最多等待30秒；超时后不并发启动第二执行，也不发网络调用，
  而是释放当前API请求并由过期dispatch claim/startup recovery接管。

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

事件schema固定为`maf.user_mcp.aggregate_transition.v1`。`operation`、state、result、
reason_code和recovery_outcome必须来自版本化Enum；未知值拒绝，不以异常文本生成label。
Task/Node关联只使用EventRecord外层现有字段，不复制到payload。

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

指标label闭集：

| 指标 | 允许label |
|---|---|
| aggregate inconsistency | `reason_code` |
| recovery outcomes | `recovery_outcome`、`evidence_kind` |
| pending approval/input seconds | `outcome` |
| duplicate approval prevented | `reason_code` |
| candidate inventory | `tier=active|archive` |
| candidate GC | `operation=archive|delete`、`result` |
| claim conflict | `phase`、`reason_code` |

所有指标继续附加现有closed execution path/routing/protocol维度；禁止owner、Task、Node、
Server、Tool、action、candidate或任意安全引用成为metric label。

指标写入失败遵守现有metric-gap合同，不得改变已经确定的业务终态。

## 实施阶段

### Phase 0：P0止血

- 接入独立aware UTC整秒terminal clock；
- authority普通结果改为durable content-addressed store并新增terminal candidate v2双读；
- 修复post-admission异常收敛；
- 终态Task不再跳过active MCP聚合；
- startup在ordinary unknown前先识别receipt/candidate、remote binding、MRTR evidence和open
  approval，保证本Phase不会误杀已有等待任务；
- 增加现有坏任务形态回归并在启动时安全收敛。

### Phase 1：聚合事务

- 迁移SQLite/PostgreSQL outbox状态、completion mode和revision约束；
- 实现claim、admission、terminal commit、dispatch finalize和unknown convergence；
- terminal commit清除branch active；单次Call不再提前完成outbox；
- 所有Coordinator退出分支接入统一finalizer；
- 新接口继承CP7 candidate/epoch safety admission和统一owner锁序。

### Phase 2：持久化审批action

- 新增加密pending-action payload store与pending action引用；
- approval open/answer/action/Node使用原子CAS；
- approval恢复执行精确action；
- claim token进入admission，版本漂移使旧action失效。

### Phase 3：MRTR与remote Task

- 接入`waiting_input`和`remote_pending`状态；
- 修复MRTR continuation admission；
- 把Phase 0的最小evidence分类升级为prepublication/adoption完整恢复；
- remote terminal统一UTC时钟和聚合commit；
- sealed state在continuation取得明确结果后才删除。

### Phase 4：长期加固

- 持久化Selector/approval预算；
- candidate消费/GC与durable result lifecycle/GC；
- 安全错误映射；
- 修复执行handle竞态；
- startup聚合不变量扫描与指标。

每个Phase必须形成独立代码checkpoint，但兼容性改变后的Phase不得由旧新backend混合运行。
Phase 0只有在启动preflight证明不存在active remote binding、MRTR/open approval或旧v1
不可恢复result ref时才可单独用于本地止血；否则必须连续完成至Phase 3后再重启服务。
在Phase 1至Phase 3全部完成前不得宣称整个MCP恢复闭环已加固，也不得部署`prod`。

## 测试与故障注入

必须在以下持久化边界模拟进程崩溃：

1. intent armed后、outbox创建前；
2. pending payload封存后、action/Interrupt聚合提交前；
3. action/Interrupt聚合提交响应丢失；
4. Answer原子提交期间及并发双提交；
5. Call保留后、网络门释放前；
6. `may_have_dispatched`后、远端响应前；
7. durable result落盘后、candidate封存前；
8. candidate封存后、receipt提交前；
9. receipt提交后、branch/outbox更新响应丢失；
10. Call完成后、dispatch finalizer前；
11. MRTR evidence prepublication后、aggregate adoption前；
12. MRTR Answer后、continuation admission前；
13. remote binding prepublication后、aggregate adoption前；
14. remote terminal candidate后、dispatch outbox resume cursor提交前；
15. candidate active-to-archive marker与三文件移动之间；
16. archive delete marker与三文件删除之间；
17. durable result delete marker与data/manifest删除之间。

每个场景断言网络调用为零或精确一次、聚合满足不变量、有可信candidate时恢复结果、
无可信结果且可能已发送时unknown/no-replay，并且只有authority corruption阻断Ready。

功能和兼容回归必须覆盖：

- 当前2.3 MB图片对应的v2信封仍小于4 KiB且不含实际I/O或Tool参数；
- 32 MiB pending payload边界、超限不创建当前Call、按历史Call选择failure mode、密文/AAD
  篡改、orphan清理和磁盘不足；
- ordinary result跨`close_task`及进程重启仍可逐字节解析，v2 candidate绑定内容SHA/大小；
- 64 MiB normalized result成功，首个超限字节生成可信失败且不截断续跑；
- legacy v1 candidate双读，以及v1 ref正常缺失时单Task失败而非虚假成功；
- 两次连续ordinary Tool调用；
- 首次OCR不同Tool可以分别审批，同一action不重复审批；
- `always_allow` Grant持久化后相同Tool不再审批；
- approval Answer并发双击只接受一个；
- MRTR等待、Answer提交、continuation执行三个阶段跨重启；
- remote Task创建、轮询、input required和terminal阶段跨重启；
- STOP、selector invalid和step limit发生在已有成功Call之后；
- Task已failed但Call仍active的启动收敛；
- active legacy v1 intent；
- CP7 candidate/epoch guard、invalidated epoch和authorization detector不能被新admission绕过；
- SQLite表重建和真实PostgreSQL constraint replacement的事务回滚、CAS并发、旧行分类、
  forward quiescence及旧新实例混跑拒绝；
- 前端approval后SSE继续接收后续状态；
- candidate active/archive、30天保留、late result、部分移动/删除恢复、8,000告警和10,000
  active硬上限；
- MCP、storage、orchestration、API、frontend及Rust相关质量门禁。

### 验收追踪矩阵

| 需求 | 主要验证位置 | 必须证明的证据 |
|---|---|---|
| FR-001 | `test_resume_envelope.py`、`test_mcp_dispatch_resume_v2.py` | v1/v2双读、64 KiB、2.3 MB图片不进入信封 |
| FR-002 | `test_cp7_terminal_results.py`、remote adapter测试 | 三条writer均为aware UTC整秒，naive拒绝 |
| FR-003、NFR-001/006 | temporary results、gateway、CP7 terminal result测试 | close/restart后逐字节可读；篡改、link/mode/owner漂移拒绝；保留期生效 |
| FR-004、FR-007、NFR-003 | SQLite/PostgreSQL repository集成测试 | claim shape/TTL、锁序、CP7门禁、双claim/双Call单赢家 |
| FR-005、NFR-004 | 新pending-action payload单测与repository测试 | 32 MiB边界、AAD、orphan、ENOSPC、无明文泄漏 |
| FR-006、FR-015 | API grant/call-control、Interrupt和frontend测试 | 双Answer单赢家、DTO/event兼容、精确action不可替换 |
| FR-008 | CP7 projection与Coordinator测试 | 单Call不提前终结dispatch；receipt/Call/branch/outbox原子 |
| FR-009 | 2026 adapter、MRTR recovery和Coordinator测试 | prepublish/adopt、Answer、continuation重启不重发旧Call |
| FR-010 | phase3 task recovery/runtime测试 | binding adopt、remote input、terminal continuation不重发原Tool |
| FR-011、FR-012、NFR-002/008 | startup recovery API与17点故障注入 | evidence顺序、分页、Ready门禁、零或一次调用、unknown/no-replay |
| FR-013 | CP7 terminal result/lifecycle repository测试 | active/archive/删除的部分操作可恢复，30天及容量门禁 |
| FR-014 | Coordinator预算与runtime scheduling测试 | 20/64/20跨重启，旧handle不删除新generation，30秒不并发 |
| FR-016 | Selector context builder与Coordinator restart测试 | Call顺序、ref、fingerprint、预算和Server恢复逐字段一致 |
| FR-017 | cancel/admission SQLite/PostgreSQL并发测试与Gateway spy | 两种锁赢家都满足线性化合同，取消赢时网络调用为0 |
| FR-018 | durable result lifecycle repository/janitor/startup测试 | active不删、24小时、Artifact接管、orphan及部分删除可恢复 |
| NFR-005 | SQLite/PostgreSQL迁移、schema contract和rollback测试 | 分类、约束替换、旧新writer拒绝混跑、受控回滚 |
| NFR-007 | audit/metrics/frontend event schema测试 | closed enum/labels、无敏感字段、metric失败不改变终态 |

每个FR/NFR必须至少由矩阵中的自动化测试覆盖；17点故障注入需记录边界、预期authority状态、
实际网络调用次数和最终projection。只验证HTTP结果而没有读取SQL及本地authority工件，不算
恢复验收证据。

## 迁移与回滚

- 这是受控cutover，不是纯additive migration：新增pending action、candidate/result
  lifecycle表、payload与
  archive目录、result/candidate v2字段、预算字段；同时扩展outbox Enum、claim shape和
  completion mode约束。
- forward cutover必须停止全部旧backend writer，不允许滚动混跑。SQLite先创建`0600`本地
  备份，再在独占事务中按当前metadata重建`mcp_dispatch_resume_outbox`并校验行数、主键、
  唯一索引和每行分类。PostgreSQL升级fresh schema manifest版本，add column/table后按
  operator reconciliation替换CP7 CHECK为`NOT VALID`、完成分类、再`VALIDATE CONSTRAINT`。
- schema mutation前生成只读分类报告，并要求以下处理全部闭合：
  - `available + pending/claimed`保留；过期claim回`pending`；
  - 旧open Tool approval没有持久化arguments，无法构造可信pending action。cutover必须先
    证明不存在该状态；否则按历史Call ledger以`failed_no_call|failed_after_call`失败对应
    Task并要求用户新建任务，不能重新运行
    Selector冒充原审批；
  - sealed MRTR/open Interrupt映射`waiting_input`；
  - active remote binding映射`remote_pending`；
  - receipt存在但Node/intent未终结时，completed映射`pending`等待Selector，failed/
    cancelled直接finalize；
  - 不能只依据旧outbox=`completed`分类，因为旧实现可能在单次Call receipt后提前完成；
  - Task已终态且Call仍active的形态交给unknown convergence；
  - candidate/result/identity冲突继续阻断Ready。
- 不改写legacy v1 envelope正文，不自动复活旧失败任务。
- 新版本产生`active`、`waiting_approval`、`waiting_input`或`remote_pending` outbox后，旧版
  二进制不认识这些状态，禁止直接回滚启动。
- 回滚前必须停止新提交，并证明不存在上述active状态、pending action、未完成MRTR或
  remote binding；否则继续用新版本完成收敛。
- rollback只能回退代码checkpoint且保留新schema，或在零新状态证明后使用受控逆迁移；
  不允许旧binary直接读取新Enum。SQLite恢复备份会丢弃cutover后的合法新数据，因此仅在
  明确放弃该时间窗数据的开发环境人工决定后使用。
- 本设计只面向`main`开发环境，不构成`prod`部署或CP7-B退役授权。

## 完成标准

只有以下条件全部满足，才能声明本设计实施完成：

1. 最新故障形态能够在零网络重放下收敛；
2. 普通多Call、approval、MRTR和remote Task共用已定义的聚合状态机；
3. 所有Repository转换在SQLite/PostgreSQL具有等价原子/CAS测试；
4. 全部17个崩溃边界通过故障注入；
5. 启动Ready前不变量扫描通过；
6. v2引用式信封、64 KiB上限和no-replay原则保持；
7. candidate消费/GC不会把预期删除误判为corruption；
8. FR/NFR验收追踪矩阵全部有自动化证据，相关回归与`git diff --check`通过，且没有把
   业务I/O或敏感内容加入审计。

## 决策、风险与未决项

### 已确认决策

- 设计信心门为95%；通过还要求Blocking=0、Major=0，不能用平均分掩盖关键缺口。
- resume envelope继续保持64 KiB，不把大文件或Tool实际I/O装入信封。
- pending-action canonical arguments明文硬上限为32 MiB，使用独立加密payload authority。
- normalized Tool result硬上限为64 MiB，超限失败且不把截断输出交给Selector。
- terminal candidate archive保留30天；业务result正文使用独立24小时恢复宽限或Artifact策略。
- 本设计实施后，统一dispatch finalizer取代CP7旧的单Call完成整个dispatch口径。

### 风险与缓解

| 风险 | 影响 | 缓解与验收 |
|---|---|---|
| 非additive outbox约束迁移分类错误 | 活跃任务丢失或旧binary误写 | 停旧writer、只读报告、SQLite备份/重建、PostgreSQL validate、零混跑证明 |
| 32 MiB AES-GCM一体式读写造成内存峰值 | 并发审批时内存压力 | 单项硬限、crypto gate=1、claim续租和≤128 MiB RSS增量压测；本设计不引入未定义的分块格式 |
| candidate/result/payload三类本地工件GC竞态 | 恢复证据或业务结果提前删除 | 独立lifecycle marker、24小时宽限、secure lookup双层、边界故障注入 |
| aggregate writer改动跨SQLite/PostgreSQL漂移 | 一端通过而另一端丢原子性 | 同一合同、固定锁序、双后端CAS/rollback测试；无真实PostgreSQL证据不得完成 |
| frontend仍把`safe_call_ref`理解为真实Call | 审批前引用语义混淆 | 保持opaque兼容字段，后端不信任该值，fixture与frontend测试锁定行为 |
| Phase 0和Phase 1-3之间短暂语义不全 | startup误收敛等待中的任务 | preflight硬门；存在remote/MRTR/open approval/v1坏ref时必须连续完成Phase 3 |

当前没有需要产品方决定的开放问题。实现若触发32 MiB上限调整、保留期调整、新密钥领域、
生产部署或Sidecar authority扩展，属于范围变化，必须另行设计和批准。

## 已记录假设

- 当前本地和本轮主要验收环境以SQL authority为准。
- `MasterKeyDeriver.derive(MasterKeyDomain.MCP_RECOVERY)`可重复生成相同领域key；pending cipher复用该领域但
  使用独立AAD前缀，不能复用MRTR sealed-state row或虚构Call身份。该行为必须锁入cipher
  contract回归，key/version不匹配时fail closed。
- remote binding worker现有claim/lease机制继续保留；本设计只调整它与dispatch聚合的
  先后关系和终态提交。
- 现有completion policy继续决定completed/stopped MCP Node后的下游执行；聚合事务负责
  保证MCP自身authority一致，并在required失败时同步失败Task。
