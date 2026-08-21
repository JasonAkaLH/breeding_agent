# Phase 1：AgentRun、AgentItem 与 Task Lease 存储 PRD

- **日期**：2026-08-22
- **状态**：pending
- **文档审阅**：document-perfectization第二次全量审计100/100通过；实现尚未开始
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **上游**：Phase 0 Agent Model Contract必须`proof_complete`
- **主责需求**：FR-17、FR-26
- **协作需求**：为FR-23提供storage primitive，不取得端到端主责
- **主责NFR**：一致性与原子性
- **直接参与者**：Storage/Rust维护者、Agent Runtime维护者、数据库与权限维护者、恢复/发布审查者
- **目标结果**：以additive方式建立Agent durable state、原子sample/outcome/final操作和单一Task lease，在SQLite、PostgreSQL、Runtime Sidecar/Rust上语义一致。

## 1. 目标与价值

模型循环不能依赖进程内message list、Future或锁。每个sample、call、result reservation、waiting authority和final
publication都必须可在崩溃后恢复；副作用开始前必须存在durable call identity。单一Task lease必须防止两个worker
同时推进同一Run，且不能变成Agent轮次或调用超时。

## 2. 进入条件

- Phase 0规范化sample/tool-call类型已冻结；
- 当前Task/TaskNode/Interrupt/Artifact/Event repository与RuntimeLeaseFacade可复用；
- 三backend开发/测试入口可用；真实PostgreSQL测试DSN是完成门禁。

### 2.1 当前证据与受影响模块

| 锚点 | 当前事实 | 本阶段影响 |
|---|---|---|
| `src/core/models.py` | Task/TaskNode/TaskEdge是当前核心账本，没有AgentRun/AgentItem | Additive新增Agent types，不删除旧字段 |
| `src/storage/runtime_sidecar_facade.py::RuntimeLeaseFacade` | 已有Task lease acquire/renew/release facade | 复用为唯一lease authority，不建立Agent lease |
| `src/storage/sqlite/`、`src/storage/postgres/` | 保存Task/Node、Interrupt、Artifact/Event及MCP状态 | 新增Agent repository和原子事务，保持权限边界 |
| `native/proto/maf/runtime/v1/runtime.proto`、`maf_runtime_sidecar` | Sidecar持有Task/Node/lease合同 | Additive增加Agent schema/operations和Rust parity |
| `tests/storage/`、`tests/core/` | 已有三backend contract、permission和claim测试 | 增加Agent conformance/fault/fake-clock vectors |

## 3. 范围与非范围

### 3.1 范围内

- AgentRun/AgentItem核心model、repository和migration；
- closed item kinds、sequence、canonical payload和digest；
- sample/call/result slot reservation；
- outcome、waiting和final原子操作；
- Task/TaskNode/Interrupt状态投影写入；
- 单一Task lease acquire/renew/release、heartbeat fencing和expiry takeover；
- SQLite、PostgreSQL、Runtime Sidecar proto/Rust/Python facade parity；
- 权限、manifest、migration和fault injection。

### 3.2 非范围

- 不调用模型或Capability；
- 不实现Agent Loop、ContextBuilder或Catalog；
- 不切换API入口；
- 不删除TaskEdge或DAG-only字段；
- 不建立第二套Agent专用租约；
- 不迁移旧DAG Task。

## 4. 数据合同

### 4.1 AgentRun

至少包含：

```text
run_id, task_id, conversation_id
status: running | waiting_for_input | waiting_for_dependency | completed | failed | cancelled
model_edition, reasoning_effort, thinking_enabled
next_item_sequence, compacted_through_sequence
active_sample_item_id
waiting_call_item_ids[]
next_batch_call_ordinal
claim_owner, claim_token, lease_expires_at
revision, created_at, updated_at, terminal_at
```

Task与Run一对一。AgentRun是细粒度执行权威；Task继续使用现有粗粒度状态，waiting投影为Task `running`。

### 4.2 AgentItem

Closed kind：`user_message`、`assistant_message`、`tool_call`、`tool_result`、`skill_activation`、`context_summary`、
`continuation`。每项有确定性item ID、task-scoped逻辑sequence、canonical payload digest和created time。

Canonical JSON必须严格JSON、UTF-8、`ensure_ascii=false`、key排序、无多余空白、禁止NaN/Infinity并以一个LF结尾；单
payload上限131,072 bytes。Python/PostgreSQL/Rust使用同一golden vectors。

## 5. 原子操作

| 操作 | 必须在一个事务/CAS中验证和写入 |
|---|---|
| `commit_agent_sample` | 当前claim/revision、assistant item、全部tool_call items、TaskNodes、预留result IDs/sequences、active sample |
| `commit_agent_call_outcome` | claim/revision、call/result identity、staged Artifact refs、Node状态、tool_result或waiting authority、events、waiting集合、Run/Task投影 |
| waiting transition | 全部open authority与Run waiting状态先提交，再允许release lease |
| `commit_agent_final_output` | final assistant item、确定性final node/Artifact/Message/event/receipt、Run/Task completed、claim清理 |
| fatal/cancel terminal | closed reason、Run/Task/Node终态、未闭合calls处理、claim清理 |

Result slots在任何Capability启动前预留。并发结果可按完成顺序写入预留位置，但Context按parent sample和ordinal重建。

## 6. Task Lease合同

- Runtime Sidecar路径复用`RuntimeLeaseFacade`/TaskLease；SQLite/PostgreSQL实现等价CAS；
- positive configurable TTL，production expiry使用storage/Sidecar权威时钟；
- active model sample、compaction、capability wave和final publish期间heartbeat成功间隔不晚于TTL/3；
- renew旋转token/revision，commit读取当前fencing token，不缓存调用开始时token；
- renew在expiry前无法证明成功即lease lost：取消本地工作，不启动新调用，stale commit fail closed；
- waiting authority先提交再release；continuation重新acquire；waiting期间无heartbeat；
- terminal commit清claim；异常退出由TTL后新owner接管；
- lease不是Agent轮次或业务调用timeout。

## 7. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P1-01 | Task与AgentRun一对一，item sequence唯一单调。 | 三backend唯一约束/CAS tests。 |
| AL-P1-02 | Sample、calls和result reservations先于副作用持久化。 | Fault test证明commit前executor调用数为0。 |
| AL-P1-03 | 一个call最多一个terminal result，result必须引用现有call。 | Duplicate/orphan result被拒绝。 |
| AL-P1-04 | Outcome提交不能出现Node terminal但缺result的可见状态。 | Transaction failure all-or-zero。 |
| AL-P1-05 | 多waiting集合与open Interrupt/remote authority逐项一致。 | Mismatch恢复为fatal consistency error。 |
| AL-P1-06 | Final commit恰有一个Artifact/Message/event/receipt。 | Crash retry使用相同IDs且无重复。 |
| AL-P1-07 | Canonical payload上限和digest跨语言一致。 | Python/Rust boundary vectors覆盖131,071/131,072/131,073 bytes。 |
| AL-P1-08 | Active长调用跨TTL持续续租。 | Fake clock覆盖sample/compaction/wave/final。 |
| AL-P1-09 | Lease lost阻止旧owner提交并允许expiry接管。 | Token rotation、stale commit和takeover竞态tests。 |
| AL-P1-10 | Waiting释放、resume重领、terminal cleanup闭合。 | No-heartbeat waiting和release failure tests。 |

## 8. 失败与恢复

- canonical payload非法/超限：提交失败，不截断必保字段；Tool大结果只保存安全projection/ref；
- storage/CAS/identity损坏：fatal，不猜测继续；
- Artifact正文已stage但事务失败：保持不可见staged状态，由现有安全cleanup处理；
- heartbeat瞬时失败：仅在当前expiry前重试；
- lease lost后Capability晚到：旧owner不得写入，由后续authority/no-replay规则处理；
- terminal后release cleanup失败：不反转终态，记录可恢复cleanup结果。

## 9. 安全与权限

- Agent storage不获得读取MCP credential、raw MCP result或上传正文权限；
- claim token、raw payload和用户标识不得进入metric label或公开event；
- PostgreSQL沿用最小权限与角色分离；schema/permission drift fail closed；
- migration只additive，不读取或转换旧WorkflowPlan；
- hidden reasoning不进入AgentItem。

### 9.1 跨阶段NFR协作

| NFR | 本阶段责任 | 后续复验 |
|---|---|---|
| 一致性与原子性 | 主责：三backend reservation/outcome/final/lease parity | Phase 3、4、6、7用真实Loop/recovery重复fault tests |
| 安全与隐私 | Closed kinds、payload上限、权限和no-secret storage | Phase 2/3验证语义projection与Context不泄漏 |
| 性能与资源 | Lease heartbeat不busy-poll，waiting可释放资源 | Phase 3/4验证长调用和waiting worker占用 |
| Final唯一性/恢复/可观测性 | 提供atomic final、waiting和durable event primitives | Phase 3～5验证端到端语义和低敏event |

## 10. 测试与环境门禁

最低测试域：`tests/core/`、`tests/storage/`、Runtime Sidecar contract、native Runtime Sidecar tests。必须新增Agent storage
conformance vectors、atomic fault injection、lease fake clock和permission tests。

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest \
  tests.integrations.test_runtime_sidecar_grpc_client \
  tests.storage.test_rust_runtime_sidecar_contract
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run \
  --only cargo_fmt --only cargo_clippy --only cargo_test
```

真实PostgreSQL schema、transaction、permission和concurrency tests必须使用测试DSN。缺DSN、skip、缺cargo或required
Rust gate时本阶段为`blocked`，不能标记`proof_complete`。

## 11. 风险、假设与开放问题

| 风险 | 缓解/阻断条件 |
|---|---|
| 三backend对sequence/digest/transaction理解不同 | 单一conformance vectors和fault matrix；任一backend不通过即blocked |
| Caller时钟或token rotation造成双owner | Storage/Sidecar权威时钟、fencing token和fake-clock takeover tests |
| Additive schema影响旧binary | Migration/old-binary compatibility test；Phase 6前不得删除旧字段 |
| File staging与DB transaction留下orphan | No-clobber staging、不可见ref和可恢复cleanup tests |
| 真实PG/Sidecar环境缺失 | 不以SQLite/mock替代，状态保持blocked |

已确认假设：现有Task lease可扩展为AgentRun唯一lease authority；Agent schema可additive落地并被旧binary忽略。
开放问题：无。

## 12. Git检查点与回滚

- Schema/proto/repository为additive，旧binary必须可忽略；
- 不切换真实入口，不删除DAG字段；
- 回滚可删除未使用Agent schema和adapter代码；如果测试数据已写入，只清理开发数据，不做旧DAG转换；
- 任何备份/清理不得读取或移动`docker_cmd.md`。

## 13. 完成与交接

完成条件：AL-P1-01～10在SQLite、真实PostgreSQL和Runtime Sidecar/Rust通过；权限/migration/fault tests通过；无模型或
Capability执行路径变化。

交付Phase 2：AgentRun/AgentItem repository contract、原子sample/outcome/final API、LeaseController和跨backend
conformance vectors。Phase 2不得依赖SQL row、SQL语句或Rust内部表布局。
