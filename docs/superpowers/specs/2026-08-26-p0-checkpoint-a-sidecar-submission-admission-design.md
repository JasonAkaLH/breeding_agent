# P0 Checkpoint A：Sidecar Submission Admission 设计

**状态：** 方案 1 已获用户方向批准；本书面规格已完成反向自审，待用户确认后再生成实施计划

**设计基线：** `main` / `4a5689f`

**替代范围：** 仅替代 `2026-08-26-p0-data-flow-hard-defect-repair-design.md` 中因双 authority 暂停的 Checkpoint A

**边界：** 允许为 Checkpoint A 增加必要的 Rust Sidecar schema、proto、Python adapter 和迁移证据；不纳入三个 P1，不改 Frontend，不执行部署或 `prod` 操作

## 1. 决策摘要

采用方案 1：Rust Runtime Sidecar 新增一个最小的 **Submission Admission Aggregate**，成为 `runtime_store=enforce` 下新消息提交准入的唯一 canonical authority。一次 Sidecar SQLite transaction 原子完成：

1. 验证或建立 Conversation admission guard；
2. 占用外部 Message identity；
3. 判断精确重放、Message ID 冲突和 Conversation busy；
4. 创建 ACCEPTED Task；
5. 保存可重放的 SQL projection 与安全 continuation envelope；
6. 把 Conversation 的 `active_task_id` 指向该 Task。

Python SQLite/PostgreSQL 中的 Conversation 与 Message 保持现有读模型和普通 CRUD authority，但新提交产生的两行是 Sidecar canonical admission 的**可恢复投影**。Sidecar commit 后即使 API 进程退出，也不会形成不可判定的半提交：startup/recovery worker 可从 Sidecar 找回 pending admission，精确重投影后继续原工作流。Task 在投影确认前不得进入 Agent execution。

本设计不把完整 Conversation/Message 生命周期迁移到 Rust，不建设通用 saga、outbox、2PC 或 consistency framework。

## 2. 现状与必须关闭的硬伤

Checkpoint A 原设计假设 Conversation、Message、Task 可在一个 SQL transaction 中提交。实施期确认该假设只对 `runtime_store=off/shadow` 成立：

- Conversation/Message 写 Python SQLite 或 PostgreSQL；
- enforce 模式的 Task 只写独立 Rust Sidecar SQLite；
- 现有 `SubmitTask` 立即提交，且没有 Conversation/Message、prepare、abort 或 delete RPC；
- 两个存储没有共享 transaction、2PC 或共同 WAL。

所以无论先写 SQL 还是先写 Sidecar，进程退出都可能留下半提交。SQL row lock 内调用 Sidecar 只能串行化，并不能共同 commit。失败补偿也会因进程退出而跳过。

方案 1 通过“一个 canonical commit + 可证明幂等的 projection/recovery”关闭该窗口，而不是伪造跨库原子性。

## 3. 目标、非目标与成功定义

### 3.1 目标

- 任意外部 `client_message_id` 都不能覆盖、迁移或改绑既有 Message。
- 同一 Conversation 在任意 API worker 数量下最多准入一个 active Task。
- 同一用户、Conversation、Message ID 与 submission fingerprint 的重试返回首次 Message/Task，不重复 durable handoff 或执行调度。
- Sidecar commit 后任何 crash point 都可收敛为同一个 SQL projection 和同一个后续工作流，不猜测、不新建替代 Task。
- enforce 模式的新 Task、Conversation admission guard 与 Message identity 在一个 Sidecar transaction 中形成 canonical admission。
- off/shadow 保持当前 SQL authority；shadow 对 Sidecar 的比较写不影响 SQL 成败，也不能成为隐含第二 authority。
- 现有 Task terminal transition 释放同一 Conversation 的 active guard，旧 Task 的迟到终态不能清除后继 Task。
- 正常单请求的 HTTP 202、Message/Task ID、路由、附件选择、MCP intent、Interrupt 与 Agent 输出行为保持。

### 3.2 非目标

本轮明确不做：

- 完整 Conversation/Message CRUD 或历史查询迁移到 Rust；
- 通用跨存储事务、通用 outbox/saga、通用工作流引擎；
- 多 active Task、并行会话执行或调度策略改造；
- 三项已排除 P1：Skill v2 schema fail-open、Sidecar Agent adapter 完整化、MCP presence cancel responsibility；
- Frontend、公开 API DTO 形态、部署编排、数据卷实迁、`prod` 切换；
- 与四项原始 P0 无关的 cleanup、命名、错误兜底或性能优化。

### 3.3 完成定义

Checkpoint A 只有在以下证据同时成立时才能完成：

1. Rust kernel、Sidecar SQLite 与 gRPC 三层均通过 created/replay/conflict/busy/rollback/concurrency 状态机测试；
2. Python SQLite、真实 PostgreSQL 与 enforce Sidecar 集成测试证明 canonical admission、投影和 recovery；
3. 每个规定 crash point 重启后只产生一个 Message、一个 Task、一个 durable handoff；
4. Message identity 的跨用户、跨 Conversation 与 Interrupt 冲突回归通过；
5. migration/backfill 对歧义数据 fail closed，证据与 schema/proto hash 闭合；
6. 原 Checkpoint B/C 与完整后端/Rust 门禁继续通过。

## 4. Authority 模型

### 4.1 按 runtime-store mode 选择唯一 authority

| 模式 | 新提交 canonical authority | Sidecar 行为 | SQL 行为 |
|---|---|---|---|
| `off` | Python SQL admission transaction | 不参与 | Conversation/Message/Task 一次 transaction |
| `shadow` | Python SQL admission transaction | 只做无副作用比较；失败只记录既有 closed observation | 与 off 相同，一次 transaction |
| `enforce` | Sidecar Submission Admission Aggregate | 一次 transaction 创建 guard/identity/admission/Task | 只投影 Conversation/Message，不创建 SQL Task |

一个请求只能有一个 canonical writer。不得出现“SQL 成功后再把 Sidecar 当 authority”或“Sidecar 失败时静默降级 SQL”的路径。

### 4.2 canonical 与 projection 的边界

enforce 下 canonical admission 包含：

- Conversation ID、authenticated username、admission availability 与 active Task pointer；
- 所有 Message ID 的不可变 identity；外部 submission/Interrupt 另含 request fingerprint 和所属 Task；
- ACCEPTED TaskRecord；
- 首次 Conversation/USER Message 的 canonical projection bytes 与 digest；
- 恢复后续提交工作流所需的有界 continuation envelope 与 digest；
- projection、workflow handoff 与 recovery claim 状态。

SQL 仍是 Conversation history/API 的读取来源。SQL projection 丢失或未完成时，Task 保持不可执行；这不是双 authority，而是 canonical 数据尚未发布到现有读模型。

### 4.3 线性化点

enforce 的线性化点是 `AdmitSubmission` 的 Sidecar SQLite commit：

- commit 前：Conversation/Message/Task 均未被准入；
- commit 后：Message identity 与唯一 active Task 已确定，所有相同重试只能得到同一 admission；
- SQL projection、title、binding、Interrupt/intent 与 execution 均不是准入线性化点。

任何 Sidecar RPC 都不得在 Python SQL write transaction 或 PostgreSQL row lock 内调用。

## 5. Core 合同与闭合结果

Core 保留一个窄 `ConversationTaskAdmissionPort`。API 只调用该 port，不按 backend 自行拼装步骤。输入是已通过纯校验/只读解析的 `SubmissionAdmissionRequest`，输出只能是以下 disposition：

| disposition | 含义 | API 语义 |
|---|---|---|
| `created` | 首次 canonical admission | 继续 projection 与首次 handoff，返回 202 |
| `idempotent_replay` | 同一 canonical 请求已存在 | 返回首次 Message/Task；协助恢复未完成阶段，但不重复已完成 handoff |
| `conversation_busy` | Conversation 已有 active Task | 复用既有 409 busy |
| `message_id_conflict` | Message ID 已被不同 identity/fingerprint 占用 | 稳定低敏 409 |
| `conversation_not_available` | owner 不匹配或状态不可提交 | 保持既有低敏 404/不可用语义 |

adapter 不返回 SQL row、Sidecar 内部 revision、fingerprint 或原始错误正文。需要继续 projection/handoff 时，只向 API workflow 返回一个 opaque admission handle；claim token 封装在 handle 内，不能进入 DTO、日志或业务 metadata。

Message identity 必须先于 busy 判定。否则同一请求在首次 Task 仍 active 时重放会被错误返回 busy，而不是 `idempotent_replay`。

## 6. Submission fingerprint 与外部 Message ID

### 6.1 fingerprint

server 使用仓库既有 canonical JSON 规则计算 SHA-256，覆盖：

- authenticated username、Conversation ID、Message ID、USER role 与正文；
- resolved routing mode、requested capability、model edition；
- 去除 server-managed/禁止字段后的 execution metadata；
- 规范化 upload IDs、sheet selections；
- resolved MCP binding identity 与冻结的 rollout/assignment 字段。

fingerprint 不包含随机 Task ID、server timestamp、credential、文件正文/Base64、MCP Tool 参数或 provider 原始 payload。相同业务请求因此可精确重放，业务字段变化必然冲突。

### 6.2 外部 ID 兼容边界

本轮不新增 `client_message_id` 字符集、前缀或长度规则；既有可接受输入继续可接受。外部 ID 即使形似 `msg-`、`file_upload:`、`agent-message:` 或其他 server-owned 形式，也只能通过全局 immutable-identity 检查占用一个尚不存在的 ID，不能覆盖既有记录。未来 server-generated ID 若命中已占用 identity，应重新生成 server ID，而不是改写既有 Message。第 13 节要求 enforce 下所有新 Message 首次插入前都经过同一个 identity authority，因此不存在“Sidecar 未见过新 assistant Message，外部请求却先占用其 SQL 主键”的缺口。

这样直接关闭已复现覆盖漏洞，同时避免把未证明必要的公开输入限制混入行为保持修复。历史 Message 不改名。

### 6.3 immutable identity 与 mutable content

Message 的不可变 identity 是 `(message_id, conversation_id, role, task_id, message_type, created_at)`。通用 repository 的更新路径不得改变这些字段：

- admission/Interrupt 使用专用的 external-message insert-or-exact 方法；
- server-owned assistant streaming 仍可通过现有路径更新 content、stream status、metadata 与 updated time，但 immutable identity 必须相同；
- immutable identity 不同一律 conflict，禁止 merge/upsert 改绑。

## 7. Sidecar proto 合同

在 `maf.runtime.v1.RuntimeSidecar` 增加以下窄 RPC；不引入通用 CRUD 或任意状态 patch：

1. `AdmitSubmission`：原子判定并创建 canonical admission；created 时返回 canonical Message/Task projection 和一个短期 workflow claim，replay 返回同一 admission 当前阶段。
2. `ClaimPendingSubmission`：以 CAS claim 一个待 projection 或待 handoff admission，返回 claim token、expiry 和 canonical envelope；用于 startup 与多 worker recovery。
3. `RenewSubmissionClaim`：只允许当前 owner/token 延长同一个短 claim；不改变 admission phase，也不替代 Task lease。
4. `AcknowledgeSubmissionProjection`：校验 claim、projection digest 与期望 phase，把 `pending_projection` 推进为 `projected`。
5. `AcknowledgeSubmissionHandoff`：校验 claim、handoff kind/identity 与期望 phase，把 `projected` 推进为 `handed_off`。
6. `CloseConversationAdmission`：以 owner 与 operation id 幂等地把 active guard 置为 unavailable；Sidecar 在自身 transaction 内推进 revision，仅供删除流程。
7. `ReserveMessageIdentity`：为 enforce 下除 submission admission 已原子登记之外的所有新 Message identity 原子占位并返回 created/exact/conflict；只管理不可变 identity，不接管 Message content 或 lifecycle。

`ClaimPendingSubmission` 是恢复工作所需的专用 claim，不扩展为通用 job queue。claim token 只在 Python↔Sidecar 内部流转，不进日志、audit、SSE 或 API。

所有 response 使用现有 `maf.common.v1.TypedError`；新增字段遵循 proto additive 编号，更新 protocol/schema/error-code hashes 和 compatibility required feature。旧 client 与新 server 可以继续使用旧 RPC，但 enforce readiness 在 Python 未声明新 feature 时必须 fail closed。

## 8. Sidecar 持久模型

### 8.1 `submission_conversations`

| 字段 | 约束/含义 |
|---|---|
| `conversation_id` | PRIMARY KEY |
| `username` | owner；创建后不可变 |
| `status` | `active` / `unavailable`，闭合枚举 |
| `revision` | CAS revision |
| `active_task_id` | nullable；同一 Conversation 只存一个 active Task |
| `close_operation_id` | nullable；删除重试幂等 identity |
| `updated_at_ms` | server time |

`active_task_id` 加唯一约束，避免同一 Task 被错误挂到多个 Conversation；TaskRecord 的 conversation identity 必须完全一致。

### 8.2 `submission_message_identities`

| 字段 | 约束/含义 |
|---|---|
| `message_id` | PRIMARY KEY，全局 identity |
| `conversation_id` | 不可变 |
| `username` | 不可变 owner |
| `identity_kind` | `submission` / `interrupt` / `server_internal` / `legacy_conflict_only` |
| `role` / `message_type` / `created_at_ms` | canonical immutable identity；legacy 缺失值使用显式 legacy 形态而非猜测 |
| `task_id` | submission/interrupt/server internal 对应 Task；legacy 可空 |
| `request_fingerprint` | submission/interrupt 精确重放；server internal/legacy 为空 |
| `created_at_ms` | server time |

历史记录以 `legacy_conflict_only` 导入：相同 ID 的新请求只可 conflict，不能猜测为 replay。

### 8.3 `submission_admissions`

| 字段 | 约束/含义 |
|---|---|
| `message_id` | PRIMARY KEY，FK identity |
| `task_id` | UNIQUE，FK canonical Task |
| `conversation_id` / `username` | 与 guard/identity/Task 一致 |
| `idempotency_key` | UNIQUE，server-owned |
| `request_fingerprint` | 64 hex，必须等于 identity |
| `conversation_projection_json` | canonical UTF-8 JSON bytes |
| `message_projection_json` | canonical UTF-8 JSON bytes |
| `projection_sha256` | 两份 projection envelope 的 digest |
| `continuation_json` | 有界安全恢复 envelope |
| `continuation_sha256` | envelope digest |
| `projection_state` | `pending` / `projected` |
| `handoff_state` | `pending` / `handed_off` |
| `handoff_kind` / `handoff_identity` | `agent_run`、`interrupt` 或既有 durable intent 的 identity |
| `claim_owner` / `claim_token` / `claim_expires_at_ms` | nullable recovery claim |
| `created_at_ms` / `updated_at_ms` | server time |

JSON bytes 在 Rust 入库前执行 closed schema、size 与 digest 校验。每个 projection 最大 64 KiB；continuation 最大 64 KiB。禁止 credential、raw file、Base64、MCP arguments、LLM prompt 或 provider response。

### 8.4 现有 Task 表

`AdmitSubmission` 复用现有 TaskRecord validator 与 Task insert helper，但与上述三张表位于同一个 Sidecar SQLite transaction。不得在 transaction 内调用一次会自行 commit 的旧 `SubmitTask`。

enforce authority 激活后，旧 `SubmitTask`：

- 可以更新已经存在且 identity 一致的 Task；
- 不得为没有 admission/import evidence 的新 ACCEPTED Task 建立 canonical authority；
- migration/import 专用路径必须与在线 SubmitTask 分离，不能通过 magic idempotency key 绕过。

内存 kernel 使用同构 maps、枚举与状态转换，保持与 SQLite adapter 相同结果和检查顺序。

## 9. Sidecar 原子准入算法

`AdmitSubmission` 在一个 transaction 中固定执行：

1. 校验 request/projection/continuation schema、size、digest，以及 Task/Conversation/Message identity 一致性。
2. 读取 `submission_conversations`。不存在则按 authenticated owner 与 active status 建立；存在则验证 owner 与 `active`。
3. 读取全局 `submission_message_identities`：
   - legacy、不同 owner/Conversation/task/kind/fingerprint：`message_id_conflict`；
   - 完全一致的 submission identity：读取 admission 并返回 `idempotent_replay`；
   - 不存在：继续。
4. 检查 `active_task_id`。非空时返回 `conversation_busy`。
5. 验证 Task status 为 `accepted`，root Message/Conversation 与 request 一致，Task ID 尚未被其他 authority 使用。
6. 插入 Message identity、admission、Task/idempotency receipt，并把 guard `active_task_id` 设为该 Task。
7. 建立 created 请求的短期 recovery claim，commit 后返回 `created`。

任何 unique/CAS/validator 失败都 rollback 整个 transaction，并映射到闭合 disposition 或 TypedError。不得把 SQLite 错误、已存在 owner/Conversation/Task 信息返回给 caller。

## 10. SQL projection

### 10.1 单一方法

enforce adapter 使用专用 `project_submission_admission`，一次 SQL transaction 投影 Conversation 与 USER Message：

- SQLite：一个 `_run` callback 内使用现有 `BEGIN IMMEDIATE`；
- PostgreSQL：一个 transaction；已有 Conversation 用 `FOR UPDATE`，缺失时 conflict-safe insert 后锁定；
- Conversation owner/status/Message immutable identity 与 canonical bytes 完全一致才可 exact replay；
- 任何字段不一致都 fail closed 为 projection conflict；不得 merge、补猜或覆盖；
- enforce 模式不得写 SQL Task row。

投影成功后，caller 用相同 claim token 与 `projection_sha256` 调用 `AcknowledgeSubmissionProjection`。如果 crash 发生在 SQL commit 后、ack 前，recovery 重新执行 exact projection，再 ack；不会产生第二行或第二 Task。

### 10.2 Conversation projection 更新范围

新 Conversation projection 可以创建现有 Conversation 行。既有 Conversation 只允许本次准入需要的 closed mutable set：现有 `current_task_id` 与对应更新时间；owner、创建时间和其他业务字段必须相同。title 等普通字段仍由既有 SQL authority 后续更新，不回写 Sidecar。

### 10.3 projection failure

SQL unavailable、timeout 或 conflict 时：

- API 返回现有安全边界内的 503；
- Sidecar admission 保持 `pending`，Task 不执行；
- 相同客户端重试进入 `idempotent_replay` 并协助同一 admission 恢复；
- startup recovery 也会继续投影；
- 不创建新 Message/Task，不删除 canonical admission，不降级到 SQL Task。

## 11. 后续工作流与 crash recovery

### 11.1 continuation envelope

Sidecar 保存首次准入后继续原流程所需的 server-normalized facts：

- Conversation/Message/Task IDs 与 projection digest；
- resolved routing/capability/model options；
- upload IDs、sheet selections等安全引用；
- MCP binding/profile/version/assignment identity；
- 已验证的非敏感 request metadata；
- 预定 handoff 类型所需的确定性 identity seed。

它不保存外部 credential、附件正文、Base64、Tool arguments、raw MCP/LLM result 或完整 prompt。recovery 只按 envelope 恢复，不重新根据可变 catalog 猜路由。

### 11.2 固定顺序

首次 created 或 replay recovery 在持有 admission claim 时按以下顺序推进：

1. exact SQL projection；
2. ack projection；
3. 执行既有 post-admission durable mutation：pending context supersede、Task input binding、file/sheet selection、MCP initial intent；每个写入必须以 admission/task identity 幂等；
4. 建立且复验一个 durable handoff：AgentRun、Interrupt 或既有 durable intent/outbox；
5. `AcknowledgeSubmissionHandoff` 保存 handoff kind/identity；
6. 只有 handoff 为 AgentRun 时才启动/唤醒 execution；title 仍按现有 best-effort 规则处理，只在 SQL title 尚未设置时由当前 claim owner 触发，最终 title update 继续使用现有 Conversation identity/CAS，不成为 canonical 完成条件。

submission-owned metric/event 只能在首次 durable handoff 创建成功时发出；事件 ID 必须由 admission/message/task 与 closed event kind 确定性生成并使用现有 exact append，避免 crash-after-append-before-ack 产生重复。下游 Agent/file-selector 生命周期事件继续由各自现有 owner 管理，不复制一套事件框架。

### 11.3 claim 与多 worker

created response 可携带 Sidecar 已建立的短 claim。进程退出后，`ClaimPendingSubmission` 只允许在 claim 到期后以 CAS 转移给一个 worker；当前 worker 在执行可能超过一个 claim 周期的既有 selector/数据库流程时使用 `RenewSubmissionClaim`。claim TTL 只限制 recovery owner，不改变 Task lease。

所有 ack 都要求 claim owner/token、expected state 与 digest。旧 owner 的迟到 ack 必须 conflict。外部网络/LLM 不在 Sidecar transaction 内执行。

### 11.4 startup 顺序

API readiness 前：

1. 完成现有 Sidecar compatibility/migration gate；
2. 分页 claim 并收敛所有 `projection_state=pending` 的 SQL projection；
3. 只有 projection backlog 闭合后才运行需要 Conversation/Message 的既有 startup validator。

runtime components ready 后、AgentRun recovery 前：

4. claim `projected + handoff pending` admission，按 continuation envelope 恢复 durable post-admission mutation；
5. 若对应 AgentRun/Interrupt/intent 已存在且 identity 完全一致，直接 ack，不重建；不一致 fail closed；
6. 完成 handoff recovery 后再进入既有 AgentRun recovery。

分页使用稳定 `(created_at_ms, message_id)` cursor 与固定上限；容量超限使 readiness fail closed，不静默截断。

### 11.5 execution singleflight

`_schedule_execution` 不再是 durability authority。A4 需要把当前 `AgentLoopOrchestrator.start_or_resume` 的“建立 Task RUNNING + AgentRun + 首条 user item”窄初始化阶段，与后续模型循环分开：前一阶段保持现有校验/事件/绑定行为并可幂等调用，只有它返回 canonical AgentRun 后，admission 才能 ack `handoff_identity=agent-run:{task_id}`；后一阶段继续使用现有 runner/lease。该拆分只暴露内部 seam，不改变公开 API 或 Agent 业务状态机。

真正的“可执行”条件是：

- Sidecar admission `projection_state=projected`；
- `handoff_state=handed_off` 且 handoff identity 对应同一 Task；
- AgentRun 初始化 seam 已用 Task identity 幂等建立/读取唯一 Run，并提交同一 user item。

client replay 只协助 pending recovery，不能创建第二 AgentRun。进程重启可以再次投递同一 Task 的本地 wakeup，但 AgentRun identity、run lease 和现有 `_running_tasks` singleflight 必须保证只有一个 logical execution owner；不得用“本地 schedule 调用次数”冒充 durable exactly-once 证明。

## 12. Task terminal 与 Conversation guard 释放

active Task status 集合沿用现有 closed status：`accepted/planning/running/cancelling`；terminal 为 `completed/failed/cancelled`。

Sidecar 提取一个唯一内部 helper，在所有可写 TaskRecord 的 canonical 路径中执行：

- `submit_task_record` / gRPC `SubmitTask` update；
- `CommitAgentState(request.task = Some(...))`。

当 Task 从 active 进入 terminal 时，仅在 `submission_conversations.active_task_id == task.task_id` 时清空 pointer 并推进 revision。旧 Task 的重复或迟到 terminal 不得清除不同的后继 Task。Task 从 terminal reopen 继续由现有状态机拒绝。

若 terminal transaction 同时提交 Agent state/Task，则 guard 释放必须处于同一个 Sidecar SQLite transaction；不能随后补写。

## 13. 全局 Message identity 与 Interrupt

原始 P0 是全局 Message 主键覆盖，不只发生在新 Task admission。若只登记外部 submission，后续新建的 assistant/system Message 仍可能先存在于 SQL、却不在 Sidecar identity registry；用户可复用该已知 ID，使 Sidecar admission commit 后才在 SQL projection 冲突。因此 enforce 下**所有 Message 首次插入**都必须共享一个 identity authority：

- submission USER Message 在 `AdmitSubmission` transaction 内登记；
- Interrupt USER Message 和所有 server-generated assistant/system/file-visible Message 在首次 SQL insert 前调用 `ReserveMessageIdentity`；
- `ReserveMessageIdentity` 保存并返回 canonical immutable tuple；server-generated ID conflict 时调用方重新生成 ID，外部指定 ID conflict 时返回低敏 409；
- server-owned streaming/update 用同一 immutable tuple exact replay reservation，或在已证明 identity 存在后只更新 SQL mutable fields；不把 content、stream chunk 或 metadata 写入 Sidecar；
- Sidecar reservation 成功而 SQL insert 前 crash 只留下一个不可复用 identity tombstone，不形成 Message/Task/Conversation 业务事实；不得清理或猜测复用该 ID。

Interrupt answer 还接受外部 Message ID，需在相同边界内关闭：

- off/shadow：SQL external-message insert-or-exact 在现有 Interrupt transaction/authority 内检查全局 ID；
- enforce：保存 Interrupt Message 前调用 `ReserveMessageIdentity`，以 `(username, conversation_id, task_id, role, message_type, kind=interrupt, fingerprint)` 原子占位；response 返回首次 reservation 的 canonical `created_at`，使 reservation 后 crash 的 retry 能重建同一 immutable projection；
- exact replay 返回已有 Interrupt Message/answer，不重复 continuation；
- identity/fingerprint 不同返回同一个低敏 `message_id_conflict` 409；
- Sidecar unavailable/timeout 时不写 SQL Message，也不继续 Agent Run。

若 RPC timeout 结果未知，caller 只能用同一 Message ID/fingerprint 重试 reservation；不得换 ID 或猜测未写入。

## 14. Conversation 删除协调

enforce 下删除先调用 `CloseConversationAdmission`，再执行现有 SQL 删除状态机：

1. Sidecar 校验 owner、当前 `active` 状态与 operation id，在自身 transaction 内把 guard 置 `unavailable` 并推进 revision；caller 不传入从 SQL 猜测的 Sidecar revision；
2. 相同 operation id 重试返回 exact success；不同 owner/operation 的 reopen/close conflict fail closed；
3. SQL 再进入既有 deleting/physical cleanup；
4. 如果 SQL 阶段失败，Sidecar 保持 unavailable，Conversation 不能再接收新 Task；相同删除 operation 重试继续 SQL cleanup。

这是有意的 fail-closed 顺序，避免 SQL 已删除后 Sidecar 仍准入 Task。rename/title/history read 不需要 Sidecar RPC。off/shadow 删除保持现有 SQL authority；shadow observation 不阻止删除。

## 15. Migration、backfill 与 cutover evidence

### 15.1 additive schema

Sidecar SQLite 在现有 schema bootstrap 中增加三张表、索引与闭合 CHECK。禁止静默重建或丢弃已有 Task 表。fresh schema 与 upgrade schema 必须得到相同 manifest/hash。

### 15.2 backfill 输入

enforce 激活前的离线/启动门禁需在旧 writer 已停止或被既有数据库 writer fence 排除后，从一个一致的 canonical SQL snapshot 导入：

- 每个 Conversation 的 ID、owner 与可提交/不可提交状态；
- 所有历史 Message ID，统一作为 `legacy_conflict_only` identity；
- 每个 Conversation 的 active Task pointer，以 Sidecar canonical Task 为准交叉校验；
- SQL Conversation `current_task_id` 与 Sidecar active Task 的一致性报告。

历史 Message 不生成 replay fingerprint；缺失信息不得猜测。

report 与 apply 必须绑定同一个 snapshot boundary、writer fence identity 和源 PK digest。未证明旧 writer 已退出、fence 已持有或 snapshot 已固定时，operator 只能 report blocked，不能 apply。代码仓库只实现和验证该门禁；如何在目标环境停 writer 属于部署步骤，不在本轮执行。

### 15.3 阻断条件

任一情况阻断 enforce readiness：

- 同一 Conversation 存在两个以上 active Sidecar Task；
- Task、Conversation、root Message identity 不一致；
- SQL owner/status/current_task 与 Sidecar guard 无法唯一对应；
- Message ID 重复且历史身份无法唯一化；
- backfill 行数、主键集合、digest 或 schema hash 不一致；
- 存在没有显式 finalize-empty evidence 的空数据源。

### 15.4 evidence

现有 runtime-sidecar migration evidence 升级为 versioned v2，保留原 Task authority cutover 证明并新增 `submission_authority_cutover`：

- source backend/identity 与 snapshot boundary；
- Conversation、Message identity、active Task 的 count/PK digest；
- ambiguity count 必须为 0；
- Sidecar destination count/digest；
- proto/schema/error-code/feature hash；
- report/apply/finalize receipt 与生成时间；
- authenticated evidence digest。

Python startup validator 必须逐字段闭合验证 v2；未知字段、旧 v1、缺块、hash drift 一律 fail closed。此次仓库实施只提供 schema/operator/validator 与测试，不执行真实数据卷迁移、部署或 `prod` cutover。

## 16. 错误、隐私与可观测性

- `message_id_conflict`：HTTP 409，响应不包含既有 owner、Conversation、Task、Message、fingerprint。
- `conversation_busy`：保持既有 409。
- owner/status unavailable：保持既有低敏 404/不可用语义。
- Sidecar unavailable、uncertain timeout、projection/recovery unavailable：现有安全边界内 503；canonical admission 若已 commit，重试恢复同一 identity。
- digest/schema/claim/handoff mismatch：fail closed，不能当作普通重试新建 Task。

日志/metric/audit 只允许 disposition、phase、低敏 error code 和计数；不得记录 username、Message 正文、fingerprint、claim token、continuation bytes 或 projection bytes。复用现有 metric family，除非实施发现没有表达 admission disposition/phase 的现有维度；不为本设计先建通用 observability framework。

## 17. 测试与故障注入矩阵

### 17.1 Rust kernel / SQLite / gRPC

- created、exact replay、replay-before-busy、cross-user/cross-Conversation conflict、busy；
- projection/continuation schema、size、digest、identity mismatch；
- 每个 insert/update 点故障导致全 transaction rollback；
- 两连接并发同 Conversation：不同 ID 恰好一 created/一 busy；相同请求一 created/一 replay；
- pending claim、expiry takeover、stale token/ack conflict、digest mismatch；
- terminal guard release覆盖 SubmitTask 与 CommitAgentState；旧 terminal 不清后继；
- in-memory kernel 与 SQLite disposition/state parity；
- gRPC typed error、wire round-trip、feature/hash compatibility。

### 17.2 SQL off/shadow

- SQLite 两 session 与真实 PostgreSQL 两 connection 的原子 Conversation/Message/Task admission；
- Message ID 跨用户/跨 Conversation 不改任何既有字段；
- exact replay 不重复 Task/title/event/schedule；
- shadow Sidecar unavailable 不改变 canonical SQL success；
- PostgreSQL row lock/unique conflict 映射闭合结果，不暴露 DB error。

### 17.3 enforce projection/recovery

逐点注入 crash/restart：

1. Sidecar commit 前；
2. Sidecar commit 后、SQL insert 前；
3. SQL Conversation 后、Message/transaction commit 前；
4. SQL commit 后、projection ack 前；
5. ack 后、post-admission durable mutation 前/中/后；
6. durable handoff commit 后、handoff ack 前；
7. handoff ack 后、`_schedule_execution` 前/后。

每个点最终必须只有：一个 canonical Task、一个 SQL USER Message、一个 Conversation pointer、一个 AgentRun/Interrupt/intent durable handoff、一个 logical execution owner。允许重启后对同一 handoff 发出幂等 wakeup，不得产生第二 AgentRun、第二 capability 调用或靠清理孤儿通过。

### 17.4 Message/Interrupt

- 历史与新 external ID 不重命名、不增加语法限制；
- external USER 与 Interrupt ID 撞既有 server-owned/另一用户/另一 Conversation identity 均 conflict，空闲 ID 仍按既有输入兼容规则准入；
- enforce 下 submission、Interrupt、assistant/system/file-visible Message 首次 insert 均有 Sidecar identity reservation；server-owned 随机 ID conflict 只重新生成，不覆盖；
- exact Interrupt retry 不重复 answer/continuation；
- reservation timeout 后同 fingerprint retry 返回 exact；
- assistant streaming mutable update继续成功，immutable identity 变更被拒绝。

### 17.5 Migration/startup

- fresh/upgrade schema 等价；已有业务行不丢失；
- Conversation/Message/active Task backfill count/PK/digest；
- 双 active、owner/root mismatch、历史 duplicate、旧 evidence、hash drift、缺 finalize-empty 均阻断；
- pending projection 在 AgentRun recovery 前收敛；pending handoff 在 AgentRun recovery 前建立或复验；
- backlog 超限不截断，readiness fail closed。

### 17.6 全量门禁

- Python compileall、Ruff；
- Core、Storage、Lifecycle、Integrations、Orchestration、Capabilities、API、E2E、Observability；
- Rust fmt、clippy/check、unit/integration/nextest、proto/contract/public-surface、audit/deny；
- 真实 PostgreSQL admission/projection/concurrency；
- 原 Checkpoint B heartbeat 与 Checkpoint C remote terminal 回归。

Frontend、Docker deployment、真实数据卷 apply 与 `prod` 不计为本 Checkpoint 的仓库完成证据。

## 18. 实施检查点与回滚

书面规格批准后，实施计划按以下独立 green checkpoints 展开；每个检查点都先红测、后最小实现、再提交：

1. **A1：Core/proto/state machine** —— closed types、RPC、Rust kernel；不接业务流。
2. **A2：Sidecar SQLite authority** —— additive schema、atomic admit、claim/ack、terminal release。
3. **A3：Python client 与 SQL projection** —— adapter、insert-or-exact、off/shadow transaction。
4. **A4：API submission/handoff recovery** —— 唯一 mode router、submit_message、startup recovery、singleflight。
5. **A5：Message identity 边界** —— external ID validation、repository immutable guard、Interrupt reservation。
6. **A6：Conversation delete 与 migration gate** —— close guard、backfill/evidence/compatibility。
7. **A7：故障注入与全量证明** —— SQLite/PostgreSQL/Sidecar crash matrix、全量回归与最终 diff 审计。

回滚必须按 checkpoint commit 回退。A4 接通 enforce 后，不允许单独回退 A1～A3 的 schema/proto/client；安全运行回滚只能把 runtime-store 切回既有 off authority，并保留 Sidecar additive 表。不得删除 admission 表或清理 pending records。部署/配置切换不在本轮授权范围，本段只定义代码兼容边界。

## 19. 反向自审

### 19.1 已排除的替代方案

- **SQL→Sidecar 或 Sidecar→SQL 顺序写：** crash 留半提交，拒绝。
- **SQL lock 内 Sidecar RPC：** 只能串行化，不能共同 rollback，拒绝。
- **失败补偿/启动猜孤儿：** 无 durable intent 时不可判定且 crash 会跳过，拒绝。
- **把 SQL Task shadow 当 enforce canonical：** 违反现有 authority cutover，拒绝。
- **迁移完整 Conversation/Message：** 超出修复所需边界，扩大回归面，拒绝。
- **通用 outbox/saga framework：** 过度抽象；本轮只做 Submission Admission Aggregate，拒绝。

### 19.2 主要风险与设计闭合

| 风险 | 闭合方式 |
|---|---|
| Sidecar commit 后 SQL 不可用 | durable pending projection；Task 不执行；exact retry/startup recovery |
| 多 worker 重复 recovery | Sidecar claim CAS + expiry + stale token rejection |
| 重放在 active Task 时误报 busy | identity/replay 检查先于 active guard |
| Task terminal 未释放 busy | 两条 Task writer path 同 transaction 调唯一 helper |
| SQL projection 覆盖旧 Message | insert-or-exact + immutable identity guard |
| 新 assistant/system Message 不在 Sidecar 导致后续外部 ID 先准入 | enforce 下所有首次 Message insert 共用 identity reservation；Sidecar 只持不可变 tuple |
| crash 重复启动 Agent | durable handoff identity + AgentRun 初始化 seam + run lease/execution singleflight |
| 删除与新准入竞态 | Sidecar close 先于 SQL delete，guard unavailable fail closed |
| 历史 Message 被误判 replay | legacy_conflict_only，绝不猜 fingerprint |
| recovery envelope 泄露 | closed schema、64 KiB、digest、敏感字段禁入与测试扫描 |
| schema/proto 漂移 | additive schema、feature negotiation、v2 authenticated evidence |

### 19.3 信心结论

书面设计信心为 **97/100**，结论为 `Pass pending user review`：

- 0 Blocking：canonical linearization、projection/recovery、Task guard release、Interrupt identity 与删除竞态均有唯一 owner；
- 0 Major：原 Checkpoint A 的两个 P0 和 enforce 双 authority 半提交均被同一 admission aggregate 关闭；
- 2 Minor：真实 PostgreSQL 锁行为和现有 post-admission 各 mutation 的精确幂等 seam 仍需在 A3/A4 红测中用代码证据确认；若发现某个 seam 不可幂等，只允许在该 seam 增加窄 receipt/CAS，不得升级为通用 workflow framework。

未到 100 分的原因是上述两项必须由实施期故障注入证明，不能用文档推断冒充。它们是已记录门禁，不改变方案 1 的 authority 决策。
