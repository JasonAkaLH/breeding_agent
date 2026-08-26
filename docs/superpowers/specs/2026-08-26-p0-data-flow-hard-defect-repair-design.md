# P0 数据流硬伤手术式修复设计

**状态：** 方案 A 已获用户批准；Checkpoint B、C 已实施，A 因 SQL/Sidecar 双 authority 假设不成立暂停

**设计基线：** `main` / `2ad43a818bb5148d8965c65d12bf7`

**范围：** 仅修复四个已复现的 P0 数据流/模块衔接硬伤

**实施原则：** 三个独立检查点、每个检查点先红测后修复、保持正常业务路径不变

## 1. 背景与结论

P0～P8 渐进式架构清理已经结束。本轮不是继续清理架构，也不接管当时明确延期的行为缺陷；它是一个新的、严格收敛的行为修复目标。

代码审查与本地复现确认四个 P0：

1. 外部 `client_message_id` 被直接当作全局 Message 主键，SQLite/PostgreSQL repository 的 merge/upsert 语义可把另一会话、另一用户的既有消息改绑到当前会话。
2. `ConversationSerialGuard` 的“无活跃 Task”读取，与后续 Conversation、Message、Task 三次独立保存之间存在 TOCTOU；并发请求可同时创建两个活跃 Task，`current_task_id` 只保留最后一个。
3. Agent lease heartbeat 会轮换 claim token，但 capability wave 冻结进入 wave 前的 `AgentRun.claim_token/revision`。执行时间跨过一次 heartbeat 后，外部能力已经产生结果，后置 ownership 校验却使用旧 token，导致结果不能提交并被上层记为 `execution_crash`。
4. MCP 2025 remote Task 在远端状态为 `failed` 时仍读取 `tasks/result`；缺少 `isError` 的普通结果会被解析为成功，generic recovery worker 又允许 parser 覆盖远端终态，最终把失败 Call/Branch 提升为 completed。

采用已批准的方案 A：不建设统一“Consistency Layer”，不依赖 API 进程内锁，也不改变 lease token 轮换规则。四个问题按三个可独立回滚的手术式检查点修复：

- Checkpoint A：一次原子会话准入，同时关闭 Message 主键越权覆盖和会话双活 Task。
- Checkpoint B：lease handle 内的短临界区，在每次 ownership-bound 持久化前取得当前 token/revision。
- Checkpoint C：remote status 为终态主权，只有 completed 才能进入成功结果解析。

## 2. 目标与非目标

### 2.1 目标

- 任意用户提供的 `client_message_id` 都不能修改、迁移或覆盖既有 Message。
- 同一会话在任意存储后端、任意 API worker 数量下最多只有一个活跃 Task 被准入。
- 完全相同的 `client_message_id` 请求重放返回首次准入的 Message/Task，不重复创建 Task、事件、标题任务或执行调度。
- heartbeat 继续按既有规则轮换 token；长 capability 执行跨越任意次数续租后，仍能用当前 lease 提交唯一结果。
- 远端任意非 `completed` 终态不能被本地 result parser 提升为 completed。
- 正常单请求、正常短 capability、2025/2026 completed remote Task 的业务结果保持不变。

### 2.2 非目标

本轮明确不处理：

- Skill v2 schema 加载失败仍可能执行/完成的问题。
- Rust Sidecar Agent enforce adapter 未完整实现的问题。
- MCP presence failure 丢失取消责任的问题。
- parallel wave 扩展、调度策略重写、通用幂等平台、全局 consistency/recovery 抽象。
- 新数据库表、唯一索引、数据迁移、协议版本、前端功能、部署或 `prod` 变更。
- P0～P8 清理范围内已经冻结的其他 deferred behavior。

上述三项 P1 只保留为独立后续候选，不进入本设计、实施计划或验收数字。

## 3. 不可变业务约束

本轮实现必须同时满足以下约束：

1. Conversation owner 与 `ACTIVE` 状态只能在持久化事务内作最终判定；API 预读不具有准入主权。
2. `client_message_id` 是不可信外部输入；全局主键相同但请求身份不同必须冲突，不能 merge。
3. 一次新消息准入的 Conversation 指针、Message 和 Task 要么全部提交，要么全部不提交。
4. 准入失败或幂等重放不能产生本地持久副作用；title、pending context supersede、upload/task binding、MCP initial intent、metric/event 与 execution schedule 只能发生在首次 `CREATED` 之后。
5. lease renew 必须继续轮换 token。不得通过固定 token、延长 TTL 或关闭 heartbeat 绕过竞态。
6. lease 同步锁只覆盖 ownership snapshot 与相邻数据库校验/提交，不覆盖模型采样、capability executor、MCP 网络或其他长 I/O。
7. remote status 是终态上界：parser 可以把 remote completed 降级为 failed，但不能把任何非 completed 终态升级为 completed。
8. 未知/丢失 authority 继续 fail closed；不得新增重放外部 capability 或 remote Tool 的兜底路径。

## 4. Checkpoint A：原子 Conversation/Message/Task 准入

### 4.1 新增窄合同

在 Core 增加一个独立的 `ConversationTaskAdmissionPort`，并由兼容 `StoragePort` facade 组合。它只暴露一个新方法；不把现有 Conversation、Message、Task CRUD 复制到新 Protocol。

输入为已经完成纯校验和只读解析的 authenticated username、目标 Conversation、USER Message、ACCEPTED Task 与规范化 submission fingerprint。输出是 closed result：

| 结果 | 含义 | API 行为 |
|---|---|---|
| `created` | 本事务首次写入 Conversation/Message/Task | 继续既有后续流程并返回 202 |
| `idempotent_replay` | 同一用户、同一会话、同一请求 fingerprint 已准入 | 返回首次 Message/Task 的 ID，不重复后续副作用 |
| `conversation_busy` | 会话已有活跃 Task | 复用既有 `ConversationBusyError` 与 HTTP 409 |
| `message_id_conflict` | 全局 Message ID 已存在但请求身份不同 | 新增稳定、无资源细节的 HTTP 409；不泄露既有 owner/conversation |

权限或会话状态错误继续走既有 404/不可用语义。存储合同返回 domain result，不返回 SQLAlchemy row、session 或后端异常。

### 4.2 Submission fingerprint

仅靠 Message 正文不能判断“完全相同的重试”：model edition、路由、附件选择或 MCP 绑定不同，都可能让同一文本产生不同执行。因此首次准入时计算一个 server-owned SHA-256 fingerprint，覆盖以下规范化字段：

- `conversation_id` 与 authenticated username；
- message content；
- resolved `routing_mode` 与 canonical requested capability；
- validated model edition；
- 去除 server-managed/禁止字段后的执行相关 request metadata，包括规范化 upload IDs 与 sheet selections；
- resolved MCP binding identity；
- Task 上冻结的 MCP execution/rollout assignment 字段。

编码复用仓库既有 canonical JSON 规则；Mapping key 顺序不影响结果。fingerprint 放入 Message 的 server-owned internal metadata key，不增加 schema。该 key：

- 用户输入不能覆盖；
- 不进入标题 prompt、Agent/LLM context、SSE、Message API 或前端 DTO；
- 只由原子准入 reader 用于精确重放判断；
- 不保存 credential、附件正文、MCP 参数或其他敏感原文。

如果既有历史 Message 没有 fingerprint，同 ID 请求一律视为 `message_id_conflict`，不能猜测为重放。

### 4.3 单事务算法

准入事务按以下固定次序执行：

1. 建立或锁定目标 Conversation，并在锁内验证 authenticated owner 与 `ACTIVE` 状态。
2. 在持有目标 Conversation 锁时读取全局 `message_id`。若存在，按 owner、conversation、USER role、Task root 关系与 fingerprint 判断精确重放；完全相同才返回 `idempotent_replay`，否则返回 `message_id_conflict`。同会话并发重放因此会在首个事务提交后重新读取 Message，而不会误报 busy。
3. 在同一事务查询该 Conversation 的活跃 Task。存在时返回 `conversation_busy`。
4. 插入 Message 与 Task，并把 Conversation `current_task_id` 更新为新 Task。
5. 一次 commit 后返回 `created`。任一步失败都 rollback 三个对象。

后端实现要求：

- SQLite：复用 `SQLiteStorage._run` 的 `BEGIN IMMEDIATE`，所有检查和写入必须位于同一个 callback/session 内；不得在 `_run` 外预查后再保存。
- PostgreSQL：现有 Conversation 使用 `SELECT ... FOR UPDATE`；Conversation 不存在时先用 conflict-safe insert 建立候选行，再 `FOR UPDATE` 读取和校验。并发插入由 Conversation/Message 主键约束串行化，唯一约束错误必须转换为上述 closed result，不能暴露数据库错误。
- Runtime Sidecar：当前 composition 已确认 Conversation/Message/Task 准入由 `SQLiteStorage` 或 `PostgreSQLStorage` 承担，Sidecar client 不接管该写路径；本轮不新增 Sidecar RPC 或 proto。

不同会话并发使用相同 Message ID 时，不要求全局粗锁：数据库 Message 主键决定唯一赢家，失败事务完整回滚并返回 `message_id_conflict`。

### 4.4 API 数据流调整

`ApiRuntime.submit_message` 保留现有校验、路由解析、upload 可用性检查和 MCP assignment 计算，但删除 `ConversationSerialGuard` 作为新 Task 的最终准入判断。它可以完全移除该调用；不能把它保留成第二套权威。

Runtime 在构造 Message/Task 后只调用一次原子准入：

- `created`：再执行 pending context supersede、title schedule、Task input binding、file selector/sheet interrupt、MCP initial no-server intent、route metric/event 和 Agent execution schedule，顺序沿用现有业务时序。
- `idempotent_replay`：立即返回存储中的 Message/Task，以上动作全部不执行。
- `conversation_busy/message_id_conflict`：立即返回对应错误，以上动作全部不执行。

`create_user_mcp_initial_intent` 继续更新已准入的同一个 Task，不再承担该 Task 的首次创建主权。它的 RETRY_ROUTE/terminal 行为不变。

## 5. Checkpoint B：heartbeat token 与 capability 提交线性化

### 5.1 根因边界

当前 heartbeat 每 `TTL / 3` 调用 `renew_task_lease`。renew 会返回新 token/revision 并更新 `AgentLeaseHandle.current`，这是统一 Agent Loop 的既有安全合同。问题不是 token 轮换，而是 `AgentCapabilityInvoker` 从进入 wave 前的 `AgentRun` 构造一次不可变 `InvocationRequest`，执行前后都复用其中的旧 token。

模型采样、compaction 与 final publish 在长操作结束、heartbeat 停止后读取最新 Run/handle 再提交；本轮不重写这些已正确路径。

### 5.2 Handle 内短临界区

`AgentLeaseHandle` 增加一个内部 `asyncio.Lock` 和一个最小 helper：在锁内读取 `current`，执行一个 ownership-bound 的短数据库操作，再释放锁。heartbeat 的 renew/read/update 也必须使用同一把锁：

```text
heartbeat: lock -> renew(old token) -> handle.current = renewed -> unlock
commit:    lock -> snapshot current token/revision -> DB assert/commit -> unlock
```

因此同一 worker 内不会发生“提交读到旧 token，同时 heartbeat 已把数据库 token 轮换为新值”的交叉。锁不替代数据库 CAS；另一个进程抢 lease 时，数据库仍按 owner/token/expiry 拒绝旧 worker。

### 5.3 Capability 调用接入点

`AgentRunner._execute_records` 把当前 `AgentLeaseHandle` 沿既有 call invoker 链传给 `AgentCapabilityInvoker`。Agent 专用 invocation request 保留 task/call/model 等不可变字段，但每个 ownership-bound seam 都在 handle 锁内以 `handle.current` 重建 `expected_claim_token` 和 `expected_revision`：

1. executor 前 ownership assert；
2. TaskNode start/route rejection commit；
3. executor 后 ownership assert；
4. completed/failed/waiting/late-result TaskNode commit。

Task/Node snapshot 读取和 executor 调用不持锁。executor 返回后如果 lease 已丢失，仍按现有 `AgentLeaseLost/AgentStorageConflict` fail closed，且绝不重放 executor。

wave 结束后的 `commit_agent_call_outcome` 继续在 `run_active_phase` 已停止 heartbeat 后使用最新 Run revision 与 `handle.current.token`；增加回归锁定即可，不另建 writer 抽象。

legacy DAG 调用 `CapabilityInvocationService` 时没有 Agent lease handle，继续使用现有 `InvocationRequest` 的固定 ownership 字段，行为不变。

## 6. Checkpoint C：remote terminal status 单向约束

### 6.1 协议 handler

`MCP2025RemoteTaskProtocolHandler.poll` 与 2026 handler 对齐：

- `status == completed`：读取 `tasks/result`，设置 `final_result` 与 `result_source=tasks_result`。
- `status == failed/cancelled`：不调用 `tasks/result`，`final_result/result_source` 都为 `None`。
- 非终态：沿用现有 poll interval/input 行为。

不为 failed Task 猜测或恢复业务 result，也不新增兼容 fallback。

### 6.2 Generic worker invariant

`MCPRemoteTaskRecoveryWorker` 先由 remote status 派生 terminal `call_status`。只有该状态为 completed 时才允许 final result 进入 result processor/persister：

- remote completed + parser succeeded：completed，保留 result ref；
- remote completed + `isError=true` 或 malformed：允许降级为 failed，丢弃成功 result ref；
- remote 非 completed 终态：保持由现有 `_terminal_statuses` 派生的状态，不调用成功 result processor/persister，不生成 completed continuation；
- 非 completed 却携带 `final_result`：视为 handler/authority 违反合同并 fail closed，不能由 parser 覆盖状态。

这条 invariant 放在 generic worker，而不是只依赖 2025 handler，防止未来 handler 再次把非 completed payload 送入成功解析链。它是远端状态主权检查，不是业务兜底。

## 7. 错误、事件与可观测性

- Message ID conflict 使用稳定低敏 code；不得返回既有 username、conversation ID、Task ID、Message 正文或 fingerprint。
- Conversation busy 保持既有 HTTP 409 和用户可见语义。
- 幂等重放保持 HTTP 202 与首次 `message_id/task_id`，不新增“已重放”前端分支。
- lease token、revision、fingerprint 不进入日志、audit payload、metric label 或 SSE。
- remote failed 的既有安全错误码继续保留；本轮不新建 metric family。
- 所有新异常必须在 API/worker 的现有错误边界内收敛，不能把 SQL 或原始 MCP payload 暴露给用户。

## 8. 回归与验收

### 8.1 Checkpoint A

先写能在旧代码失败的聚焦测试：

- 用户 B 用用户 A 的 `client_message_id` 向另一 Conversation 提交：返回 409，A 的 Message 每个字段保持不变，B 的 Conversation/Task/Message 无残留。
- 同用户跨 Conversation 重用 ID：返回 409，原 Message 不迁移。
- 同会话、同 ID、同规范化请求串行重试：两次响应 ID 完全相同，只有一个 Task/Message/title schedule/execution schedule。
- 同会话、同 ID 但正文、model edition、routing、upload selection 或 MCP binding 任一变化：返回 409。
- 两个并发请求使用不同 Message ID 提交同一 Conversation：恰好一个 202、一个 busy 409；数据库只有一个活跃 Task，Conversation 指向该 Task。
- 两个并发请求使用完全相同请求：恰好一次 `created`，另一次 `idempotent_replay`。
- 事务在 Conversation、Message、Task 三个写阶段逐点注入失败：全部 rollback。
- internal fingerprint 不出现在 API history、SSE、Agent context 与 title metadata。

SQLite 测试必须使用两个独立 session/并发任务；PostgreSQL 同义测试必须使用两个真实连接，不能用 mock 代替行锁证据。

### 8.2 Checkpoint B

- 使用短 TTL 和可控 sleep，让一个 capability executor 跨越至少两次 renew；外部 executor 只调用一次，TaskNode 与 Agent call outcome 正常提交。
- 在 executor 返回与 post-assert/terminal commit 之间强制 heartbeat，断言提交使用最新 token/revision。
- 在 ownership-bound DB 操作持锁时触发 heartbeat，断言 heartbeat 等待且不会发生旧 token 写。
- 模拟另一 owner 抢 lease：旧 worker 提交失败且 executor 不重放。
- 既有短 capability、waiting、cancel/late result 与 legacy DAG invocation 回归保持。

### 8.3 Checkpoint C

- 2025 `tasks/get=failed` 且服务端存在缺 `isError`/`isError=false` result：不得调用 `tasks/result`，Call/Branch 保持 failed，无成功 result ref、无 completed continuation。
- 2025 completed + 普通成功 result：继续 completed。
- 2025 completed + `isError=true`：继续降级 failed。
- 2026 completed/failed/cancelled 现有行为不变。
- 构造违反合同的 handler：非 completed + final result 必须 fail closed，不能调用 parser/persister。

### 8.4 每个检查点门禁

每个检查点独立执行：

1. 新红测在基线实现上证明原硬伤。
2. 最小实现使聚焦测试通过。
3. 运行对应模块完整测试：Checkpoint A 至少 API/Core/Storage/Lifecycle，B 至少 Orchestration/Agent Loop，C 至少 MCP Integrations/API result processor。
4. 运行 Backend canonical 全量回归与 `compileall`。
5. 审查 diff、`git diff --check`、公开合同与依赖变化。
6. 创建一个范围清晰的 Git commit 后才进入下一检查点。

涉及 PostgreSQL 的并发语义只有真实 PostgreSQL 零 skip 才能标记完成；环境不可用时必须如实记为验证缺口，不得用 SQLite 结果替代。

## 9. 预计修改边界与回滚

预计业务修改严格限制在：

- Core：准入 request/result/port 与低敏冲突错误；
- API Runtime/route：原子准入调用、后置副作用和 409 映射；
- SQLite/PostgreSQL storage：同义准入事务；
- Agent Loop lease/runner/capability invoker/task projection：当前 lease snapshot；
- MCP remote task recovery handler/worker：terminal 单向约束；
- 对应分层测试、文档索引与变更日志。

无 schema/proto/frontend/dependency 变更。每个检查点单独提交，可按 C → B → A 的逆序回滚；回滚某一检查点不要求回滚其他检查点。任何实现中发现必须改 schema、Sidecar proto、公开客户端 DTO 或外部 MCP 服务，均视为设计假设失效，停止该检查点并回到设计审查，不能顺手扩范围。

## 10. 自审结论

本文件已完成以下收敛审查：

- **占位符：** 无 TODO、TBD、待定实现分支或未选择方案。
- **范围：** 四个 P0 全部有唯一修复 owner；三项 P1 和其他 deferred behavior 明确排除。
- **矛盾：** token 轮换与提交稳定性通过短临界区同时成立；remote parser 只可降级、不允许升级；幂等重放与冲突拒绝边界明确。
- **后端等价：** SQLite `BEGIN IMMEDIATE` 与 PostgreSQL row/unique serialization 都要求在单事务内完成；未把进程锁当跨 worker 保证。
- **副作用：** 首次准入、重放、busy、conflict 四种结果的后置动作均已定义。
- **安全：** 不可信 ID、owner、fingerprint、lease token 和 remote payload 的暴露边界明确。
- **可验证性：** 每个硬伤都有旧代码失败的新回归，以及最小模块/全量门禁和真实 PostgreSQL 要求。
- **过度设计检查：** 只增加一个准入 port、一把 handle-local lock 和一条 worker invariant；无新平台、schema、RPC、重试或通用框架。

设计信心为 **96/100**。剩余 4 分来自实现时仍需用真实 PostgreSQL 验证“不存在 Conversation 行”的并发插入与唯一冲突转换细节；这是既定验收证据，不需要扩大设计。
