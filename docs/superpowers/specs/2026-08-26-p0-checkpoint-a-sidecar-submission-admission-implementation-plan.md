# P0 Checkpoint A：Sidecar Submission Admission 实施计划

**状态：** implementation in progress；A1、A2已完成并通过独立终审，下一步为A3 Python client与SQL off/shadow/enforce projection

**设计 authority：** `docs/superpowers/specs/2026-08-26-p0-checkpoint-a-sidecar-submission-admission-design.md`

**计划基线：** `main` / `3d6fdad`

**已完成且不得回写：** Checkpoint B=`dfc0cd0` + `0626684`；Checkpoint C=`2b39c25`

**范围例外：** 用户已在原 `a22a972` 停止条件触发后明确授权 Checkpoint A 所必需的 Sidecar schema/proto/migration evidence；批准方案中的crash-exact preparation还需要一张submission-specific SQL additive `submission_preparation_receipts`表，只含immutable route/memory/selector receipt，不含job/status/lease/retry/schedule。这是本计划唯一新增SQL业务表；该后续授权不覆盖其他schema、三个 P1、Frontend、部署或 `prod`。

## 1. 计划目标与行为锁

本计划只完成原四项 P0 中尚未闭合的两项：

1. 外部 `client_message_id` 不得覆盖、迁移或改绑任何既有 Message；相同 submission/Interrupt 才能精确重放。
2. 同一 Conversation 在多 API worker 下最多准入一个 active Task，并在 enforce 的 Sidecar Task authority 下不存在不可恢复的 SQL/Sidecar 半提交。

必须保持的现有业务行为：

- 正常提交仍返回 HTTP 202 和同一 Message/Task DTO；不增加前端分支。
- off/shadow 仍由 SQL canonical admission；enforce 才由 Sidecar canonical admission。
- title、pending Skill context、upload binding、file/sheet selection、MCP initial intent、route assignment、Interrupt 与 Agent Loop 的业务顺序保持，只把持久副作用移到首次 canonical admission 之后。
- Task/Agent/MCP 的既有状态枚举、lease/token/no-replay、cancel、waiting 与 recovery 语义不变。
- 既有任意格式 `client_message_id` 继续可用；不新增保留前缀或字符集规则。
- Message content/history/streaming lifecycle 继续以 SQL 为 authority；Sidecar 只持 immutable identity 与 submission recovery envelope。

以下内容无论实施中多么方便都禁止加入：三个 P1、通用 saga/outbox/workflow framework、完整 Conversation/Message Rust CRUD、Frontend、公开 DTO 扩展、新依赖、部署配置修改、真实数据卷 apply 或 `prod` 操作。

## 2. 完成证据

Checkpoint A 只有在 A1～A7 全部 green、逐项 commit 且最终审计同时证明以下事实时完成：

| 要求 | authoritative evidence |
|---|---|
| Sidecar 一次 transaction 创建 guard/identity/admission/Task | Rust kernel + SQLite fault injection + 两连接并发测试 |
| replay 先于 busy；不同 identity 冲突 | kernel/SQLite/gRPC/Python contract 四层同义测试 |
| SQL off/shadow 原子准入 | SQLite 双 session、PostgreSQL 双 connection 与逐写点 rollback |
| enforce 只投影 Conversation/Message，不写 SQL Task | storage integration 与 runtime contract negative assertion |
| crash 后 pending projection/preparation/handoff 可恢复 | API startup fault matrix 与 Sidecar claim/renew/prepare/get/ack tests |
| durable Agent handoff 先于本地 wakeup | AgentRun 初始化 seam、restart/no-second-Run/capability-once tests |
| 所有 Message identity 不可改绑 | repository immutable guard、Sidecar reservation inventory、Interrupt/API tests |
| terminal Task 释放正确 guard | SubmitTask 与 CommitAgentState 两路径 Rust tests |
| delete 不与新 admission 竞态且可自动恢复 | SQL DELETING intent→Sidecar close→physical delete并发/crash tests |
| migration 不是人工声明 | writer-fenced report/apply、source/destination count+PK digest、authenticated v2 evidence tests |
| B/C 与完整系统未回归 | Backend canonical、Rust quality gates、B/C focused suites、final diff audit |

真实 PostgreSQL 环境不可用时，A3/A7 必须记录为验证缺口，Checkpoint A 不得标记 complete；SQLite 结果不能替代真实行锁证据。

## 3. 生产接线原则

A1～A5 先构建并验证独立能力，但不让现有 production-mode request 自动进入新 authority。生产接线只在 A6 完成以下条件后一次切入：

1. 所有 Message 首次 insert 的 identity reservation 已覆盖；
2. Conversation delete 已使用SQL durable DELETING intent→Sidecar close/fence→physical delete；
3. v2 migration evidence validator 已要求 Conversation/Message/active Task inventory；
4. Sidecar compatibility feature/schema hash 已更新并通过；
5. startup 可在 AgentRun recovery 前收敛 pending projection/preparation/handoff并绑定当前Sidecar finalization receipt。

这样任何中间 commit 都可以运行既有业务或在测试注入下运行新能力，不会暴露一个“只修 submission、未登记 assistant Message”或“已准入但无 recovery”的半成品 enforce 路径。

每个检查点：先红测、再最小实现、运行 focused gate、审查 diff、更新必要 `AGENTS.md`/`CHANGELOG.md`，然后创建单一范围 commit。不得把下一检查点顺手并入。

## 4. A1：Core、proto 与内存状态机

### 4.1 修改边界

- `src/core/models.py`：新增 closed admission request/result/opaque handle、Message identity 与 recovery phase value objects；不放 backend row 或 gRPC 字段。
- `src/core/contracts.py`：在既有persistence protocol分块中增加唯一窄`ConversationTaskAdmissionPort`，组合admit、claim/renew、projection/preparation/handoff、owner-bound preparation read、close与identity reservation九个动作，并由`StoragePort`兼容facade组合；不复制已有CRUD signature。
- `src/core/errors.py`：新增低敏 `MessageIdentityConflictError`；busy 继续复用 `ConversationBusyError`，不在实施时再选择owner。
- `native/proto/maf/runtime/v1/runtime.proto`：additive 增加设计批准的9个RPC与closed messages/enums；不改旧 field number，不复活 DAG 字段。
- `native/crates/maf_runtime_store/src/lib.rs`：A1只更新proto hash/operation declarations/resource-limit contract；SQLite authority尚未完成前不宣称supported feature或新schema hash，migration evidence v2留A6；只在现有error code无法表达协议/写冲突时增加最小error code。
- `native/crates/maf_runtime_sidecar/src/lib.rs`：新增root public request/response/record declarations、内存kernel maps/transition及9个in-memory gRPC handlers；SQLite-backed service在A1对新写RPC显式`migration_blocked`，A2替换，保证A1独立编译green；protobuf转换仍留`codec.rs`。
- `native/crates/maf_runtime_sidecar/src/codec.rs`：新增 protobuf↔domain closed conversion。
- `src/storage/rust_contracts/runtime_sidecar_contract.json`：只通过现有 Rust exporter生成并与 Rust artifact byte-equal。

### 4.2 红测

- 新建 `tests/core/test_submission_admission_contract.py`：closed disposition、immutable record、opaque handle 不泄露 token、StoragePort method union/signature。
- 扩展 `native/crates/maf_runtime_sidecar/tests/runtime_sidecar_kernel.rs`：
  - created；
  - exact replay；
  - replay-before-busy；
  - same ID/different owner/conversation/task/fingerprint conflict；
  - different ID/same active Conversation busy；
  - unavailable Conversation；
  - reserve server_internal/interrupt exact/conflict；
  - claim/renew/stale token/phase CAS；
  - close exact/conflict；
  - envelope size/digest/identity mismatch。
- 扩展 contract/public-surface tests：proto method/field additive、feature/schema hash、checked-in JSON 与 Rust exporter一致。
- 新建 `native/crates/maf_runtime_sidecar/tests/public_surface.rs`，锁定只增加批准的admission public symbols；当前crate没有同类门禁，不能用workspace test间接代替。
- shared boundary vectors锁定Conversation projection 64 KiB、Message projection/continuation 64 MiB、prepared execution 128 KiB以及完整gRPC message至少140 MiB；用实际HTTP/canonical/protobuf serializer构造接近50 MiB、包含高转义/多字节内容的合法request，验证Message projection、Admit request/response与SQLite-backed binary整包roundtrip，证明当前50m入口行为未缩小。

### 4.3 实施顺序

1. 定义closed domain data和设计6.2 exact nested/offline shared vectors，不接API。
2. 添加proto message/RPC、Rust codec和完整in-memory handlers；SQLite backend显式拒绝新能力。
3. 在kernel以`BTreeMap`同构实现Conversation guard、Message identity、Admission、claim/prepare/get/ack；提供非RPC finalized constructor/offline method供kernel test，不暴露在线finalize。
4. 固定检查顺序：validate → Conversation owner/status → Message replay/conflict → busy → Task identity → atomic in-memory mutation。
5. 更新 contract artifact/hash；禁止手工只改 JSON。

A1 同时把设计6.2的三类domain-separated digest、projection/continuation exact key集合固化为Rust/Python同义contract tests。replay identity明确不比较当前尝试新生成的候选Task ID或timestamp；它们只在首次created分支验证，replay必须返回首次canonical对象。

### 4.4 focused gate

```bash
conda run -n multi_agent python -m unittest tests.core.test_submission_admission_contract tests.core.test_persistence_port_contracts tests.storage.test_rust_runtime_sidecar_contract
cd native && cargo test -p maf_runtime_sidecar --test runtime_sidecar_kernel
cd native && cargo test -p maf_runtime_sidecar --test runtime_sidecar_grpc
cd native && cargo test -p maf_runtime_sidecar --test public_surface
cd native && cargo test -p maf_runtime_store
cd native && cargo fmt --all -- --check
```

**停止条件：** proto 必须删除旧字段或需要通用 patch RPC；Core port 必须暴露 raw token/backend row；kernel 无法在一个方法内保持 closed disposition。

**提交：** `feat(admission): define sidecar submission contract`

## 5. A2：Sidecar SQLite canonical authority

### 5.1 修改边界

- `native/crates/maf_runtime_sidecar/src/sqlite_adapter.rs`：additive三张业务表+一个`submission_authority_meta` singleton、索引/CHECK、atomic admit、claim/renew/prepare/get/ack、identity reserve、close、offline import/finalize primitive，以及terminal guard release helper。
- `native/crates/maf_runtime_sidecar/src/lib.rs`：gRPC service handlers；kernel parity。
- `native/crates/maf_runtime_sidecar/src/codec.rs`：仅新增 A2 response mapping 所需 conversion。
- `native/crates/maf_runtime_sidecar/tests/runtime_sidecar_sqlite.rs`、`runtime_sidecar_grpc.rs`、`runtime_sidecar_binary.rs`：持久化、wire 与重启证据。

表结构严格使用修正后批准设计中的：

- `submission_authority_meta`
- `submission_conversations`
- `submission_message_identities`
- `submission_admissions`

不新增通用 receipt/job 表；singleton只记录`uninitialized/finalized`、finalization receipt digest/首次时间及strict canonical首次receipt blob，后者仅用于同digest crash exact replay；claim/phase/投影 bytes 都属于 admission row。

### 5.2 红测

- fresh/reopen schema 具有 exact columns、indexes、CHECK；已有 Task 行升级不丢失。
- uninitialized meta拒绝online admission；isolated finalize-empty后才允许created；same digest finalize返回并复验首次stored receipt/time、different digest conflict；finalized后旧SubmitTask不能创建无admission/import evidence的新ACCEPTED Task。
- 两独立 connection 同 Conversation 不同 ID：恰好一 created、一 busy。
- 两独立 connection 同一请求：恰好一 created、一 replay。
- fault injector 在 guard、identity、admission、Task、active pointer、claim 每个写点失败：四类 canonical state 全 rollback。
- SQL commit 后重开 adapter，pending admission/claim/bytes/digest 完整。
- ack after projection exact；digest mismatch/stale token/wrong phase conflict。
- prepare first-write exact；different prepared snapshot/expired owner conflict；owner-bound get不越权；prepared digest参与handoff ack。
- close与pending recovery并发：close同transaction使admission closed、fence claim、取消未handoff accepted Task；后续scan/旧ack不能复活。
- Reserve在guard缺失时按owner创建active/null-task guard；close后file/server reserve拒绝；异owner拒绝。
- `submit_task_record` terminal 与 `commit_agent_state(task=Some(...))` terminal 都在同 transaction 清 guard。
- 旧 Task 迟到 terminal 不清新 `active_task_id`；terminal reopen 仍被既有状态机拒绝。
- `SubmitTask` 不得创建没有 admission/import evidence 的新 ACCEPTED Task；既有 Task update 保持。
- Task route assignment继续write-once，唯一例外是accepted/open/prepared/pending-handoff且prepared kind为no_server_intent时，允许一次exact user_scoped→unavailable/no_user_scoped_server并同时accepted→failed的canonical update；其他字段/状态/重复漂移拒绝。
- gRPC 9 RPC wire roundtrip、TypedError安全、binary restart/Unix/TCP兼容回归；Claim found=false仍返回authority finalization receipt。
- 64 MiB Message projection/continuation边界与140 MiB完整gRPC message配置在SQLite-backed binary roundtrip保持，不因A1→A2接线退回默认4 MiB或64 KiB。

### 5.3 实施顺序

1. 在 bootstrap additive 建表并验证 exact shape；meta默认uninitialized，不重建 `submitted_tasks`；SQLite connection配置现有风格的bounded busy timeout，双adapter指向同一文件时不能把`database is locked`冒充closed disposition。
2. 提取 transaction-local Task insert/update helper，供 SubmitTask/Admit/CommitAgentState 复用，禁止嵌套自提交。
3. 实现 atomic admission 固定检查顺序。
4. 实现identity reservation；所有online kind同transaction create/validate active Conversation guard，file_visible允许null task且不保存content，interrupt保存fingerprint与canonical Message timestamp。
5. 实现claim/renew/prepare/get/ack与稳定`(admission.created_at_ms,message_id)`pending scan；prepared first-write-wins，closed admission永不返回；replay只向同workflow owner返回未过期token。
6. close在同transaction关闭pending admission/fence claim；accepted Task用既有accepted→cancelling→cancelled两步validator但单commit；相同owner closed retry exact，不依赖随机SQL runner ID。
7. 提供adapter offline import/finalize/finalize-empty primitive；输入含strict 12-key canonical subject，按批准domain公式在write transaction前复验subject digest，并在同一IMMEDIATE transaction内重算三类PK/canonical inventory后才finalize；确定性receipt exact replay，A6只包装stdin binary/operator/evidence。
8. 在Task update validator加入上述initial-no-server窄transition，并用prepared admission row校验资格；不增加通用assignment patch。
9. 将 guard release 插入两条 Task canonical writer transaction。
10. 替换A1 SQLite-backed migration-blocked handlers并复验kernel/SQLite parity；此时才更新SCHEMA_HASH、supported feature和checked contract，error table未变则hash不变。

### 5.4 focused gate

```bash
cd native && cargo test -p maf_runtime_sidecar --test runtime_sidecar_sqlite
cd native && cargo test -p maf_runtime_sidecar --test runtime_sidecar_grpc
cd native && cargo test -p maf_runtime_sidecar --test runtime_sidecar_binary
cd native && cargo test -p maf_runtime_sidecar --test runtime_sidecar_kernel
cd native && cargo test -p maf_runtime_sidecar --test public_surface
cd native && cargo fmt --all -- --check
cd native && cargo check --workspace --all-targets --all-features
```

**停止条件：** 任意canonical准入写需要第二次commit；terminal release只能事后补写；SQLite重开后无法区分pending/projected/prepared/handed_off/closed；需要清理孤儿才能通过。

**提交：** `feat(admission): persist atomic sidecar admission`

## 6. A3：Python gRPC client 与 SQL admission/projection

### 6.1 修改边界

- `src/storage/runtime_sidecar_grpc_client.py`：手写protobuf unary client的9个窄方法和strict decoder；不引入grpcio。
- `src/storage/runtime_sidecar_facade.py`：closed response/record/digest validator；不承担业务编排。
- `src/storage/sqlite/repositories.py`：
  - off/shadow `admit_submission` 的单 `_run` / `BEGIN IMMEDIATE` transaction；
  - enforce `project_submission_admission` 的 Conversation+Message insert-or-exact transaction；
  - Message immutable identity guard，替代危险 `session.merge` 改绑语义。
- `src/storage/postgres/repositories.py`：PostgreSQL-specific admission/projection transaction，使用 conflict-safe Conversation insert、`FOR UPDATE`、Message unique conflict mapping。
- `src/storage/interfaces.py` / exports：只同步既有模块索引需要的 type exposure。

### 6.2 红测

- 扩展`tests/integrations/test_runtime_sidecar_grpc_client.py`：9 RPC request/response field、unknown/missing/oversize/digest/identity error、authority receipt、compatibility feature。
- 升级 `tests/api/support.py` 的Sidecar fake为strict admission/identity fake；禁止测试fake继续用无条件Task overwrite把冲突/重放测成假绿。
- 新建 `tests/storage/test_submission_admission_sqlite.py`：
  - cross-user/cross-Conversation same ID conflict且原行 byte-equivalent；
  - exact replay；不同正文/model/routing/upload/MCP binding conflict；
  - same Conversation并发 one created/one busy；same request one created/one replay；
  - Conversation/Message/Task逐点fault rollback；
  - enforce projection never inserts SQL Task；projection replay exact；immutable mismatch fail closed。
- 新建 `tests/storage/test_submission_admission_postgres_integration.py`：使用 `tests/postgres_test_support.py` 模块专用 DSN 和两真实 connection 复现同义并发/row lock/unique conflict/rollback。
- 扩展 `tests/storage/test_sqlite_conversation_repository.py` 与 PostgreSQL contract：assistant streaming mutable update通过，immutable owner tuple变化拒绝。

### 6.3 实施顺序

1. client先做 wire roundtrip，不接 storage。
2. SQLiteStateRepository 增加 transaction-local insert-or-exact helper；`save_message` 只允许相同 immutable identity 的 mutable update。
3. SQLiteStorage 一次 `_run` 实现 off/shadow canonical admission。
4. enforce projection只写Conversation/Message，使用Sidecar canonical bytes/digest；绝不调用`save_task`。existing Conversation固定`create_if_missing=false`并锁行要求ACTIVE；只有准入时SQL确实不存在的新Conversation可固定true，缺失existing row不得由recovery重建。
5. PostgreSQL override在单 session transaction内锁定/插入/复验；不得在锁内调用 Sidecar。
6. shadow只比较纯结果/记录 closed observation，不调用 canonical `AdmitSubmission`。

### 6.4 focused gate

```bash
conda run -n multi_agent python -m unittest tests.integrations.test_runtime_sidecar_grpc_client tests.storage.test_submission_admission_sqlite tests.storage.test_sqlite_conversation_repository tests.storage.test_rust_runtime_sidecar_contract
conda run -n multi_agent python -m unittest tests.storage.test_submission_admission_postgres_integration
conda run -n multi_agent python -m compileall -q src tests
python -m ruff check src/core src/storage tests/core/test_submission_admission_contract.py tests/storage/test_submission_admission_sqlite.py tests/storage/test_submission_admission_postgres_integration.py tests/integrations/test_runtime_sidecar_grpc_client.py
```

**停止条件：** PostgreSQL first-Conversation race 需要进程锁；Sidecar RPC 必须进入 SQL transaction；projection 需要 SQL Task shadow；immutable guard破坏合法 streaming update。

**提交：** `feat(admission): add SQL admission projection`

## 7. A4：Durable Agent handoff 与 admission recovery primitive

### 7.1 修改边界

- `src/orchestration/agent_loop/orchestrator.py`：把现有 `start_or_resume` 拆为：
  - 幂等 `initialize_run(request)`：Task RUNNING CAS、唯一 AgentRun、首条 user item、binding check、使用Task/Run确定性event ID补齐现有 created events；不能只靠本地`created`布尔值；
  - `run_initialized(...)`：注册context、取得run lease并进入现有 runner/finalizer。
  - `start_or_resume` 保留兼容 facade，顺序调用两者，现有 consumer 不变。
- `src/orchestration/agent_loop/models.py`：仅在需要时增加内部 initialized handoff record；不公开到 API DTO。
- `src/api/runtime.py`：把upload scrub与conversation memory preparation收敛为一次`_prepare_execution_request`；memory先走无summary/event写入的pure seam，receipt与Sidecar prepared胜出后再按确定性identity exact物化既有summary/event。`_schedule_execution`只接收已初始化的同一prepared request并创建本地wakeup，本地 singleflight仍由 `_running_tasks` 管理。
- `src/storage/sqlalchemy_models.py`、`src/storage/sqlite/repositories.py`、`src/storage/postgres/repositories.py`：新增submission-specific `submission_preparation_receipts`与first-write-exact repository seam；只保存route decision、完整memory context、selector decision及各自/整体digest，不含status、lease、retry或调度字段。
- `src/lifecycle/agent_run_recovery.py`：仅补齐从 durable initialized Run 恢复所需的 existing seam；不改 waiting/capability result no-replay。
- 新建 `src/api/submission_admission.py`：只拥有 approved submission canonical JSON/fingerprint、safe continuation envelope、claim驱动的 projection/handoff coordinator；不做通用 workflow。
- `src/api/runtime.py` startup：增加 pending projection/handoff recovery hook，位置严格早于 `_recover_agent_runs`，且外部网络/LLM 不进 pre-ready projection 阶段。

### 7.2 红测

- 新建 `tests/orchestration/test_agent_submission_handoff.py`：
  - initialize 创建唯一 Run/user item，重复调用 exact；binding/user text变化 conflict；
  - crash after Run/user item before local wakeup，restart只运行同一Run；
  - 两 worker initialize同Task恰好一个 canonical Run；
  - `start_or_resume`调用序列与现有事件、Task status、runner次数不变；
  - B的current lease/no-replay测试继续通过。
- 新建 `tests/api/test_submission_admission_recovery.py`（先用 fake port，不接生产 mode）：
  - pending projection startup先于AgentRun recovery；
  - SQL commit/ack之间 crash exact reproject；
  - durable AgentRun/Interrupt/intent已存在时只ack；identity drift fail closed；
  - claim expiry takeover、stale owner不能ack；
  - backlog cursor稳定且超限阻断readiness；
  - continuation envelope拒绝credential/Base64/raw arguments/oversize。
- preparation receipt/prepared snapshot：route decision、完整memory preparation（prompt payload+summary/event intent）、selector decision先以SQL canonical bytes first-write-exact，再由Sidecar snapshot保存receipt locator/digest、initial required tool、execution text source/digest、model/bundle revisions与upload/MCP refs；SQL NULL只表示unset，合法none保存canonical `null`+digest，三组件全部settled后overall receipt才immutable；prepared后不再调用memory LLM/current catalog，component或overall digest drift fail closed。
- selector/sheet：claim expiry前后两个不同decision竞争时只有first prepared snapshot可materialize一个attachment set/Interrupt；Interrupt required_fields持locator/digest，restart answer恢复exact metadata。
- route decision：exact `maf.submission.route_decision.v1` schema锁定`decision/fingerprint/model-safe profiles`三者约束；initial-no-server首次观察的`retry_route|no_server`写入即冻结，非适用路径写`not_applicable`；decision前Server出现可选retry，decision后Server出现不得漂移。
- full memory recovery：当前builder最大fixture的完整prompt payload逐字roundtrip且不施加AgentItem 131072-byte限制；prepared后新增历史/file-visible Message不进入本次执行。claim takeover可重复pure memory计算，但summary/event各只按winner的确定性identity物化一次。
- 扩展 `tests/api/test_execution_singleflight.py`：允许restart幂等wakeup但只有一个 logical execution owner/capability call。
- initial-no-server：SQL owner guard重验有Server时保持RETRY_ROUTE；无Server时只materialize exact intent，随后Sidecar一个canonical update完成allowed assignment+Task failed并释放guard，最后SQL enforce专用no-Task materializer推进intent/receipt/events；三段crash可恢复且SQL永不读写Task。
- duplicate wakeup/recovery：live第二runtime精确遇到`agent_task_lease_held`只退出且不调用`_mark_task_failed`；startup面对dead owner未过期lease时按authoritative expiry注册同Run窄重试，到期后恢复；live owner heartbeat推进时第二worker持续不执行/不fail。其他异常仍按既有failure boundary。

### 7.3 实施顺序

1. 先拆 Agent initialize/run并补确定性初始化事件；让全部现有 Agent tests green。
2. 提取memory pure preparation与winner exact materialization；三组件first-write并settle overall receipt，Sidecar prepared胜出后才写summary/event。`_schedule_execution` 改为 handoff ack 后对已初始化request做纯wakeup。legacy/Interrupt resume通过兼容helper先initialize再wakeup。
3. 实现submission pure canonicalizer、continuation/prepared validator与domain-separated digest，使用现有canonical JSON/hash helper，不复制serializer。
4. coordinator按project→ack→闭合SQL preparation receipt→CAS Sidecar prepared→durable mutations→initialize/interrupt/intent→handoff ack固定序列；first receipt/snapshot共同约束route/memory/selector/binding；每个admission-owned SQL mutation先锁Conversation并要求ACTIVE；initial-no-server使用上述SQL intent→Sidecar terminal→SQL no-Task materializer恢复链。
5. 增加 pre-AgentRun recovery hook与仅针对held AgentRun的lease-expiry retry；不改lease算法、不抢未过期lease。只在测试注入新 port，production mode仍未切入。

### 7.4 focused gate

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_agent_submission_handoff tests.orchestration.test_agent_loop tests.orchestration.test_agent_invocation tests.storage.test_agent_task_lease tests.api.test_execution_singleflight tests.api.test_submission_admission_recovery tests.lifecycle.test_agent_run_recovery
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m compileall -q src tests
python -m ruff check src/api/submission_admission.py src/api/runtime.py src/orchestration/agent_loop src/lifecycle/agent_run_recovery.py tests/orchestration/test_agent_submission_handoff.py tests/api/test_submission_admission_recovery.py
```

**停止条件：** durable handoff仍依赖`asyncio.Task`先运行；prepared snapshot无法精确恢复memory/required tool/pinned revisions；memory在prepared前仍写summary/event；selector可在prepared后写不同attachment/Interrupt；initial-no-server仍暗写SQL Task；duplicate lease-held会mark failed或dead owner expiry后无自动进展；拆分改变普通/continuation/waiting Agent状态机；需要重放 capability/remote Tool 才能恢复。

**提交：** `refactor(agent): separate durable admission handoff`

## 8. A5：全局 Message identity 与 API/Interrupt 行为

### 8.1 修改边界

- `src/storage/sqlite/repositories.py` / `src/storage/postgres/repositories.py`：enforce下所有 Message首次insert在SQL transaction外先经Sidecar reservation；已存在 identity 的mutable update只复验 immutable tuple。
- `src/api/runtime.py`：
  - `answer_interrupt` 在记录 answer/Message/continuation前完成 external identity reservation/exact replay。
- `src/api/file_selection_runtime.py`、`src/orchestration/visible_message_history.py`、`src/api/upload_runtime.py`：只在实际存在绕过 `save_message` 的首次 insert时接同一 reservation seam；不改业务分支。
- `src/api/routes/conversations.py`：映射低敏 `message_id_conflict` 409；不暴露既有资源字段。

必须先用静态/运行时 inventory 找全所有 `MessageRow` insert/upsert入口。允许的首次写 owner最终只剩：submission projection、统一 `save_message` facade、file-upload专用 upsert；每一条在enforce均有Sidecar identity evidence。

Identity schema使用`message_created_at_ms`和`reserved_at_ms`两个字段；`file_visible`允许task_id为null，submission/interrupt/server_internal必须有task_id。随机server ID conflict可重生；upload-derived/Task-derived确定性ID只允许exact或fail closed。

### 8.2 红测

- storage/API identity聚焦：用户B/同用户跨Conversation复用既有assistant/file-upload/internal ID时原Message每字段不变；arbitrary legacy格式ID仍接受；完整new-submission HTTP 202/replay行为留到A6正式单路径接线后验收。
- 扩展 `tests/api/test_auth_login_and_isolation.py`：owner/status低敏语义保持。
- 扩展 `tests/lifecycle/test_interrupt_resume.py`、`tests/api/test_skill_slot_collection_v2.py`、`test_conversation_file_selection.py`：Interrupt exact replay不重复answer/continuation；changed content/cross-task conflict；reservation timeout同fingerprint重试使用首次created_at。
- generic、MCP approval、MRTR、remote、file selection、slot/v2等所有Interrupt分支都在任何answer/continuation mutation前reserve；现有`already_accepted`提前return必须先通过共同`ensure exact answer Message` seam，crash-after-answer-before-Message重试能补Message且不二次resume。本轮不重写通用Interrupt状态机。
- 新建/扩展 storage identity inventory test：生产 `MessageRow` first-insert paths全部经过admission/reservation owner；assistant streaming content update通过，identity字段变化拒绝。
- title/pending context/upload/MCP/file selector现有测试锁定正常时序与调用次数。

### 8.3 实施顺序

1. 为 `save_message` facade增加mode-aware reservation；Sidecar unavailable时enforce不写SQL。
2. 覆盖file-upload和任何直接first-insert owner；禁止在每个call site复制RPC逻辑。
3. 所有Interrupt分支在首个lifecycle mutation前reserve；共同ensure-message seam覆盖already-accepted提前return，exact replay复用首次canonical created_at和answer identity。
4. identity conflict domain映射与泄漏扫描；new-submission route mapping不在A6前接production。

### 8.4 focused gate

```bash
conda run -n multi_agent python -m unittest tests.api.test_message_submission tests.api.test_auth_login_and_isolation tests.api.test_conversation_titles tests.api.test_pending_skill_context tests.api.test_conversation_file_selection tests.api.test_uploads tests.api.test_skill_slot_collection_v2 tests.lifecycle.test_interrupt_resume tests.storage.test_sqlite_conversation_repository
conda run -n multi_agent python -m compileall -q src tests
python -m ruff check src/api src/storage src/orchestration/visible_message_history.py tests/api/test_message_submission.py tests/lifecycle/test_interrupt_resume.py
```

**停止条件：** 必须限制合法 `client_message_id` 才能正确；存在未登记的Message first-insert；exact replay会重新selector/LLM/capability；修改公开DTO。

**提交：** `fix(message): enforce immutable message identity`

## 9. A6：Delete coordination、migration gate 与 production mode接线

### 9.1 修改边界

- `src/api/runtime.py`：Conversation delete先用现有SQL row lock持久化DELETING intent，再调用Sidecar close，最后才physical delete；operation id由owner+Conversation确定性派生，独立于现有随机SQL runner id。close同transaction关闭pending admissions/fence claims并按既有accepted→cancelling→cancelled两步/单commit取消未handoff Task。startup从DELETING精确重试close/cleanup。
- `src/storage/runtime_sidecar_facade.py`：migration evidence升级为closed v2，新增 `submission_authority_cutover`，保留原Task cutover并严格拒绝v1/unknown/drift。
- `native/crates/maf_runtime_store/src/lib.rs` 与 checked-in contract：migration policy v2字段/hash。
- 新建 `src/storage/runtime_sidecar_submission_migration.py` 与 `scripts/migrate_runtime_sidecar_submission_authority.py`：专用离线 report/apply/finalize/evidence core/CLI；不得复用或改写已冻结的Phase7 destructive operator。
- Rust Sidecar增加离线 import adapter/二进制入口：只从stdin接收closed canonical inventory，在一个`BEGIN IMMEDIATE`内导入并finalize singleton；不暴露在线import RPC，不加入server service。
- `src/api/runtime.py` factory/startup/`submit_message`：enforce要求v2 authenticated evidence、新Sidecar feature，并在任何request/recovery前用一次found=false也带authority meta的Claim response逐字匹配v2 finalization receipt；随后接pending recovery。在此checkpoint一次性用coordinator替换原三次save链，删除`ConversationSerialGuard`准入主权，让off/shadow/enforce都走唯一mode router；pending supersede/title/binding/selector/intent/events/schedule只在created/recovery owner后执行，replay返回首次对象且不重复已ack handoff。
- `tests/api/test_runtime_sidecar_contract.py`、`test_user_mcp_runtime_wiring.py`、`tests/storage/test_rust_runtime_sidecar_contract.py`、`tests/storage/test_runtime_sidecar_submission_migration.py`、`tests/scripts/test_migrate_runtime_sidecar_submission_authority.py`。

### 9.2 migration operator closed contract

`report` 必须：

- 验证 source SQL snapshot boundary 与 writer quiesced/fence identity；
- 枚举Conversation owner/status、全部Message ID、Sidecar Task/active status；
- 检测双active、root identity、owner/status/current_task drift、duplicate/unknown rows；
- 输出脱敏 count、sorted PK digest、canonical row digest、blockers、tested commit/tree/schema hash；
- 空库也必须有 finalize-empty evidence。

`apply` 必须：

- 绑定 exact report SHA、tested commit/tree、source snapshot/fence；
- 先做 `0600` no-clobber Sidecar backup；
- 通过Rust离线adapter在一个独占transaction导入guards与`legacy_conflict_only` identities，并从canonical Task计算active pointer；写finalized meta/receipt digest；
- 逐inventory复验destination count/digest；
- 生成HMAC authenticated v2 evidence与append-only receipt；
- importer commit前失败完整rollback；commit后evidence写前crash以相同finalization subject精确重跑，返回首次stored receipt/timestamp并补发HMAC evidence；不同digest冲突。不得选择一个双活Task或修补source业务行。

本轮只实现operator与隔离fixture验证，不对本地真实运行卷、部署或`prod`执行report/apply。

### 9.3 红测

- delete与admit并发：SQL mark、projection/mutation共享Conversation row serialization；mark后admission即使暂时Sidecar commit也无法SQL投影并会被close取消；close后任何新admit unavailable。
- SQL mark后close前crash由startup继续close；close后physical delete前crash继续cleanup；其他owner失败且无信息泄露。
- Admit commit/pending→close→SQL physical delete→startup：closed admission不被claim，Conversation/Message不复活，未handoff Task cancelled；旧claim ack conflict。
- v1/缺submission块/unknown field/hash drift/HMAC mismatch/弱权限/symlink evidence阻断startup。
- v2 evidence finalization receipt与当前Sidecar authority meta不一致时，即使pending为空也阻断startup。
- report在writer未quiesced、双active、identity drift、digest mismatch、缺empty finalize时blocked。
- apply在同一受锁snapshot重新生成inventory；Sidecar finalize commit前的report SHA/tree/fence漂移、备份失败、逐表fault、destination drift全部rollback。finalize commit后外部evidence写前crash用deterministic receipt exact resume补发evidence，不虚称rollback。
- offline importer strict shared vectors覆盖exact nested schema、1,000-row pages、64KiB record/1GiB total/nonnegative-u32 count，以及request→exact finalization subject canonical bytes→digest→stdout receipt；subject只能含批准的12个top-level字段和三组exact inventory，不含arrays、时间或receipt字段；unknown/oversize/计数漂移在write transaction前拒绝。
- valid SQLite fixture与真实PostgreSQL snapshot fixture得到相同canonical inventory语义。
- enforce生产 wiring：无v2 evidence/feature/client fail closed；valid evidence后submit/recovery启用；SQL Task仍不存在。
- 正式HTTP submission：cross-user/cross-Conversation ID 409且原行不变；exact replay 202返回首次IDs，Task/Message/title/event/binding/selector/intent/schedule各一次；正文/model/routing/upload/MCP任一fingerprint漂移409；same Conversation并发closed结果正确。

### 9.4 focused gate

```bash
conda run -n multi_agent python -m unittest tests.scripts.test_migrate_runtime_sidecar_submission_authority tests.storage.test_runtime_sidecar_submission_migration tests.storage.test_rust_runtime_sidecar_contract tests.api.test_runtime_sidecar_contract tests.api.test_user_mcp_runtime_wiring tests.api.test_message_submission tests.api.test_submission_admission_recovery tests.api.test_auth_login_and_isolation tests.storage.test_sqlite_conversation_delete tests.storage.test_postgres_conversation_delete
cd native && cargo test -p maf_runtime_sidecar
cd native && cargo test -p maf_runtime_store
conda run -n multi_agent python -m compileall -q src tests scripts
python -m ruff check src/storage/runtime_sidecar_submission_migration.py scripts/migrate_runtime_sidecar_submission_authority.py src/api/runtime.py tests/storage/test_runtime_sidecar_submission_migration.py tests/scripts/test_migrate_runtime_sidecar_submission_authority.py
```

**停止条件：** apply可在writer活动时运行；evidence可由caller自报counts而无source/destination复验；需要在线import bypass RPC；delete必须reopen guard；生产接线发生在全局identity/migration之前。

**提交：** `feat(admission): activate submission authority cutover`

## 10. A7：故障注入、全量证明与终态审计

### 10.1 crash矩阵

在 `tests/api/test_submission_admission_recovery.py` 与Sidecar/SQL fixture逐点注入：

1. Sidecar commit前；
2. Sidecar commit后、SQL insert前；
3. SQL Conversation/Message transaction commit前；
4. SQL commit后、projection ack前；
5. ack后、pending context/binding/selector/intent前中后；
6. durable AgentRun/Interrupt/intent commit后、handoff ack前；
7. handoff ack后、本地wakeup前后；
8. terminal Task transaction与guard release之间；
9. delete close与SQL mark/delete之间；
10. Interrupt identity reservation与SQL Message/answer之间。

每个restart终态只允许：一个canonical Task、一个SQL USER Message、一个Conversation pointer、一个durable handoff、一个logical execution owner；允许同handoff幂等wakeup，不允许第二AgentRun、第二capability/remote调用或孤儿清理猜测。

### 10.2 最终模块门禁

使用仓库既有 canonical 分层入口，先逐域、再全量：

```bash
conda run -n multi_agent python -m compileall -q src tests scripts
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

另执行：

- 真实PostgreSQL `test_submission_admission_postgres_integration` 两connection零skip；
- `scripts/run_rust_quality_gates.py` 的 fmt/check/clippy/test/nextest/contract/proto/public-surface/audit/deny相关现有门禁；
- Checkpoint B focused：heartbeat/current lease/Agent invocation/long executor；
- Checkpoint C focused：2025 failed不取result、generic non-completed final result fail closed；
- `git diff --check`、dependency/Cargo.lock、tracked schema/proto、file mode、`docker_cmd.md`存在/ignored/未跟踪保护；
- `skill/sql-query` 若本地ignored compatibility checkout不存在，明确N/A而非PASS。

Frontend没有修改，故不以Frontend测试冒充本轮证据；若backend公开DTO意外变化则属于停止条件而不是补Frontend。

### 10.3 completion audit

最终逐条把设计3.3、测试17、计划2节要求映射到：具体测试名、命令输出、commit、文件diff与N/A原因。审计还必须证明：

- 三个P1相关生产文件零diff；
- `frontend/**`、deploy/docker/prod配置零diff；
- 没有通用outbox/saga/workflow、第二套serializer、第二个Message identity owner；
- Sidecar RPC不在SQL transaction/lock内；
- 所有新增依赖为0，Cargo.lock/package files无意外变化；
- `docs/AGENTS.md`、受影响源码AGENTS索引和`CHANGELOG.md`已同步；
- 工作树clean。

**提交：** `docs(admission): close checkpoint A proof`

## 11. 回滚与停止规则

- A1～A6每个commit可独立revert；A6 production接线后，运行时安全回滚只能回到既有off SQL authority并保留additive Sidecar表/identity tombstone，不删除pending records。
- SQL `submission_preparation_receipts`是additive空表/运行期receipt载体：Conversation physical delete同步清理；代码回滚保留表，不做破坏性drop，也不对历史数据执行backfill。
- 本任务不实际执行mode切换；上述运行时回滚只是代码合同。
- 任何测试发现规格必须新增公开DTO、完整Message CRUD、通用job表、网络补偿重放、Frontend或部署修改时，停止当前checkpoint，回到已批准设计的最小authority边界；不得“先实现再解释”。
- 发现无关死代码/C901/重复只能记录，不删除。
- 既有测试失败必须先用当前checkpoint前commit复现；不能通过放宽assertion、增加fallback或扩大重试掩盖。

## 12. 计划自审门

计划提交前执行：

1. placeholder扫描：无TODO/TBD/未选方案；
2. 文件边界核对：每个production文件只属于一个首要checkpoint；
3. authority审计：off/shadow SQL、enforce Sidecar、SQL projection三者无双写主权；
4. crash-prefix审计：每个跨库点都有canonical commit、exact retry或fail-closed状态；
5. behavior审计：客户端ID、DTO、Agent/Interrupt/title/file/MCP正常路径不变；
6. scope审计：三个P1、Frontend、deploy/prod、通用平台化为0；
7. verification审计：真实PostgreSQL与Rust contract/proto不可由mock替代。

自审结论：**98/100，0 Blocking / 0 Major / 2 Minor，Ready**。两个Minor仅为必须由A3真实PostgreSQL两连接测试与A4逐故障点证明的实施证据，不授权新增抽象或改变行为。

计划通过后直接从A1开始，用户已授权自主完成，不再设置逐checkpoint人工批准门。
