# 全仓业务代码渐进式架构清理 P1 实施计划

## 1. 状态、目标与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`complete`
- P1 start commit：`97d78c6986008e321580fcb1bb3cf21d1f2335ef`
- P1 start tree：`3a449edfa644146660b7827dd1127b3f128af264`
- 输入：P0 `complete` baseline、259-method literal signature authority与Cancellation characterization

P1只把`src.core.contracts.StoragePort`的单体声明拆成真实窄域Protocol，保留原aggregate作为兼容facade，并把Cancellation Sidecar writer从`Any`收窄为独立本地Protocol。P1不迁移生产consumer、不修改repository实现、schema/data、运行模式、错误、调用次数、顺序、公开`StoragePort` identity或259个方法签名，也不进入P2～P8。

成功条件：

1. 259个async method恰好属于一个窄Protocol，名称与`inspect.signature`保持P0 literal baseline；
2. `src.core.contracts.StoragePort`仍是原canonical aggregate，四条公开路径继续以`is`指向它；
3. `SQLiteStorage`、`PostgreSQLStorage`与现有test doubles继续满足aggregate；
4. Cancellation writer不进入aggregate，off/shadow/enforce/no-client与AgentRun/legacy admission trace不变；
5. 所有适用门禁通过，生产consumer与P2～P7 private helper零修改。

## 2. 审计结论

### 2.1 ai-slop-cleaner finding classification

| Finding | 分类 | 证据 | P1处置 |
|---|---|---|---|
| `P1-STORAGE-MONOLITH-001` | boundary violation | 单个runtime-checkable `StoragePort`约1460行、259个async method | 按现有连续业务块拆为19个窄Protocol；不复制method declaration |
| `P1-AGGREGATE-COMPAT-001` | grounded fallback | 四条公开路径、SQLite nominal subclass与大量既有consumer依赖aggregate | 保留薄多继承facade；只作兼容，不作为method owner |
| `P1-CANCELLATION-TYPING-001` | boundary violation | `CancellationService`用`Any`接收实际只需`write_cancellation_token`的Sidecar client | 定义单方法本地Protocol并收窄annotation；调用代码不变 |
| `P1-CANCELLATION-MODES-001` | grounded fallback | off/shadow/enforce/no-client及AgentRun/legacy分支已有exact trace | 原样保留；不得合并、默认或修复 |
| `P1-PORT-MEMBERSHIP-COVERAGE-001` | missing coverage | P0锁259签名，但尚未证明每个method只属于一个窄域 | 新增直接contract test，literal锁port membership、disjoint union与aggregate surface |

未发现需要在P1处理的masking fallback、dead code或实现复制；跨层consumer迁移全部留给其owner计划。

### 2.2 当前consumer集合

静态AST按259个method的attribute引用扫描到36个`src/**`文件；其中Storage adapter是implementer，其余是已知consumer。P1只登记handoff，不修改这些文件。动态`getattr`或外部调用不据此删除兼容facade。

## 3. 窄Protocol分区

成员以P1 start `src/core/contracts.py`中的连续method block为输入；`first..last`两端均包含。测试必须把每个完整member name写为literal，不能仅依赖行号或命名前缀。

| Protocol | methods | first..last | primary adoption owner | 已知生产consumer handoff |
|---|---:|---|---|---|
| `UserMCPConfigurationStoragePort` | 28 | `list_user_mcp_servers..invalidate_user_mcp_tool_grants` | P3 | User MCP routes/runtime、Gateway/health/config/credentials/selector/legacy migration |
| `MCPDispatchStoragePort` | 35 | `save_mcp_branch_record..commit_mcp_call_terminal` | P3 | API runtime、Dispatch Coordinator、selector、historical/result projection |
| `MCPResultLifecycleStoragePort` | 16 | `recover_mcp_terminal_candidate..release_mcp_durable_result_deletion` | P3 | API runtime、terminal/durable/artifact/historical projection |
| `MCPDispatchFinalizationStoragePort` | 13 | `finalize_mcp_dispatch..get_mcp_execution_terminal_projection` | P3 | API runtime、Coordinator、selector、artifact/historical projection |
| `MCPLegacyRetirementStoragePort` | 3 | `converge_legacy_runtime_retirement..list_mcp_legacy_retirement_task_ids` | P3 | API runtime |
| `MCPCP7StoragePort` | 5 | `append_mcp_cp7_safety_ledger_record..produce_mcp_cp7_safety_snapshot` | P3 | CP7 safety runtime |
| `MCPRemoteTaskStoragePort` | 39 | `save_mcp_remote_task_binding..expire_mcp_connection_leases` | P3 | API runtime、Coordinator/Gateway/recovery/credentials、Lifecycle presence/interrupt、P2 task projection |
| `MCPRolloutStoragePort` | 24 | `append_mcp_audit_event..list_mcp_rollout_instance_config_leases` | P3 | API runtime、MCP audit/observability |
| `AuthStoragePort` | 10 | `create_or_get_maf_master_key_validation..rotate_auth_user_token` | P5 | Auth services、MCP credentials |
| `ConversationStoragePort` | 29 | `save_conversation..delete_conversation_memory_summaries_for_conversation` | P5 | API/auth/files/conversations/runtime、MCP audit/coordinator/gateway、P2 task projection/memory |
| `PendingSkillContextStoragePort` | 6 | `save_pending_skill_context..mark_pending_skill_context_superseded` | P3 | API runtime |
| `MessageStoragePort` | 5 | `save_message..mark_file_upload_message_deleted` | P5 | API files/conversations/runtime、MCP selector、P2 memory/history |
| `TaskStoragePort` | 9 | `save_task..list_task_nodes_for_task` | P5 | API、Lifecycle、MCP、Main Agent与P2 Agent Loop/memory consumers |
| `ArtifactStoragePort` | 8 | `save_artifact..list_task_input_attachments_for_conversation` | P5 | API files/tasks/runtime、Main Agent、MCP result/selector、P2 memory |
| `EventStoragePort` | 4 | `append_event..list_event_page_for_task` | P5 | API task/runtime、Lifecycle cancellation/interrupt |
| `MailboxStoragePort` | 6 | `save_mailbox_message..list_mailbox_deliveries_for_message` | P5 | Lifecycle cancellation/mailbox |
| `InterruptStoragePort` | 7 | `save_interrupt..list_interrupt_answers` | P5 | API files/runtime、MCP Coordinator、Lifecycle、P2 task projection |
| `SlotStoragePort` | 8 | `save_slot_collection..get_slot_event_by_idempotency_key` | P5 | API runtime、P2 task projection |
| `CheckpointStoragePort` | 4 | `save_checkpoint..list_checkpoints_for_task` | P5 | Lifecycle cancellation/interrupt |
| **合计** | **259** | disjoint exact union | — | 任何未登记consumer由其source owner计划补录，不能默默改aggregate |

所有Protocol的canonical contract owner仍为P1/Core；表中owner只负责后续consumer adoption。P2/P4 consumer由其自身计划改annotation/injection，但不能复制或重新拥有port定义。P8退出时不得保留无解释的生产内部aggregate consumer。

## 4. 实现结构

### Checkpoint A：RED contract membership

新增`tests/core/test_persistence_port_contracts.py`：

- literal声明19个Protocol的完整method name集合；
- 证明每组非空、两两不交且union恰为P0的259 names；
- 证明每个method是async且signature与`EXPECTED_STORAGE_METHOD_SIGNATURES`相同；
- 证明aggregate inherited surface仍为259且四路径identity不变；
- 证明aggregate自身不再直接拥有async method，且不包含`write_cancellation_token`。

RED只允许因窄Protocol尚不存在而失败，不改旧测试期待。

### Checkpoint B：GREEN persistence contracts

在`src/core/contracts.py`原位按第3节边界拆分19个runtime-checkable Protocol；所有method body/signature逐字保留。文件末尾定义薄`StoragePort`多继承facade，继续保留`src.core.contracts` module identity。`src/core/__init__.py`、`src/storage/interfaces.py`、`src/storage/__init__.py`继续只公开原`StoragePort`，不扩张facade `__all__`。

`tests/core/test_public_contract_compatibility.py`只把aggregate surface读取从直接`__dict__`改为包含继承成员的`inspect.getmembers`；259项literal signature authority本身不得改动。新membership test复用该literal，不复制第二份signature manifest。

不得：

- 用动态class生成、method复制、catch-all或运行时manifest替代直接声明；
- 调整method顺序、参数、annotation/default或repository inheritance；
- 修改任何consumer annotation或实现。

Checkpoint提交：`refactor(core): split persistence port contracts`

### Checkpoint C：Cancellation独立边界

在`src/lifecycle/cancellation_service.py`定义本地`CancellationSidecarWriter` Protocol，其唯一方法返回同步或awaitable mapping；将构造参数和字段从`Any | None`收窄到该Protocol。保留现有`inspect.isawaitable`兼容、五个实际调用参数、error validation和所有mode分支。`tests/lifecycle/test_task_cancellation.py`直接锁其单方法signature和与aggregate的分离；不得把writer加入任一Storage Protocol或改真实gRPC client。

Checkpoint提交：`refactor(lifecycle): type cancellation sidecar boundary`

### Checkpoint D：Handoff与全量验证

把最终Protocol成员、consumer集合、owner/adoption与P8退出条件写回本计划终态账本；同步`docs/AGENTS.md`与`CHANGELOG.md`。P0 inventory保持历史冻结，P1单独记录start/final tracked set。生产源码允许差异只限`src/core/contracts.py`和`src/lifecycle/cancellation_service.py`。

Checkpoint提交：`docs(cleanup): close P1 persistence boundaries`

## 5. 行为锁与门禁

每个checkpoint先跑：

```bash
conda run -n multi_agent python -m unittest tests.core.test_public_contract_compatibility
conda run -n multi_agent python -m unittest tests.core.test_persistence_port_contracts
conda run -n multi_agent python -m unittest tests.lifecycle.test_task_cancellation
conda run -n multi_agent python -m unittest tests.storage.test_agent_repository_contract
conda run -n multi_agent python -m unittest tests.storage.test_runtime_sidecar_agent_repository
conda run -n multi_agent python -m unittest tests.api.test_runtime_public_contract
```

Cancellation变更额外运行：

```bash
conda run -n multi_agent python -m unittest tests.storage.test_rust_runtime_sidecar_contract
conda run -n multi_agent python -m unittest tests.lifecycle.test_agent_run_recovery
conda run -n multi_agent python -m unittest tests.api.test_task_cancel
```

P1终态运行P0 Backend canonical逐域门禁；Frontend与Rust业务均未触及，复用P0 complete证据并明确记为`N/A: production path not touched`，不重复制造无关门禁。另运行changed Python compile/Ruff、`git diff --check`、inventory exact equality、公开symbol existence与最终diff审查。

## 6. 回滚与停止条件

每个checkpoint独立commit；回滚只按逆序revert，不做schema/data恢复。出现以下任一情况立即停止当前checkpoint：

- 259 names/signatures、四路径identity或`StoragePort.__module__`漂移；
- SQLite/PostgreSQL/Sidecar nominal/structural兼容失败；
- Cancellation任一mode调用次数、顺序、错误或Task/AgentRun结果变化；
- 为通过测试需要修改P2～P7 consumer/private helper、repository实现、schema/data、Rust/Frontend或`prod`；
- 新Protocol形成catch-all、动态生成或第二份method declaration。

10项P0 deferred behavior全部只锁定、不修复；发现相邻缺陷仅登记到P2～P8对应owner，不扩大P1。

## 7. 实施终态

### 7.1 Checkpoints

| Checkpoint | Commit | 结果 |
|---|---|---|
| P1 plan/audit | `0d549be` | 259-method分区、36个生产引用文件、19个port与owner/consumer handoff冻结 |
| Persistence contracts | `9452999` | 19个runtime-checkable窄Protocol；薄`StoragePort` aggregate；literal membership/signature测试 |
| Cancellation boundary | `d1128d6` | 单方法`CancellationSidecarWriter`替代`Any` annotation；执行路径零修改 |
| Final ledger | 本文终态提交 | Backend canonical、final diff、索引与CHANGELOG闭合 |

### 7.2 Gate record

| Scope | ran/fail/skip | 结果 |
|---|---:|---|
| Python compileall `src scripts tests` | completed/0/0 | PASS |
| Core | 48/0/0 | PASS |
| Storage | 410/0/7 | PASS；7项真实PostgreSQL profile未配置，沿用P0逐项N/A |
| Lifecycle | 42/0/0 | PASS |
| Integrations | 707/0/2 | PASS；2项Linux Result Parser gate在macOS N/A |
| Agent Skills | 209/0/0 | PASS |
| Orchestration | 109/0/0 | PASS；既有`datetime.utcnow` warning不变 |
| Capabilities | 49/0/0 | PASS（16+15+15+3） |
| API | 446/0/0 | PASS；既有unclosed SQLite ResourceWarning不变 |
| E2E / Observability / Scripts / Deployment | 7+39+63+3 / 0 / 0 | PASS |
| Backend合计 | 2132/0/9 | PASS；9项均为未触及平台N/A，不是P1新增skip |
| Changed Python compile/Ruff | completed/0/0 | PASS |
| Ruff audit `src scripts` | 162 C901 + 7 F401 + 3 F841 | 与P0相同的172个finding信号；未运行`--fix` |
| Frontend / Rust | N/A | P1未触及对应生产或测试路径；不重复冒充新证据 |

### 7.3 Final invariants与handoff

- P1 final tracked set为1047，排序path SHA-256为`f4030c92bfae20319217ca6141e134aa3a7e0b58be2b5a8b318f8c902db88a73`；相对start只新增本计划与一份直接membership test。
- 四条`StoragePort`路径仍是同一`src.core.contracts.StoragePort`对象；aggregate直接async methods为0，19个窄Protocol的disjoint union恰为259，完整signature与P0 digest authority一致。
- 生产consumer和repository实现零修改；P2/P3/P5按第3节handoff逐文件采用最窄port，P4只改自己拥有的API annotation/injection。任何跨计划consumer变更都须在对应计划重新锁行为。
- Cancellation writer只有`write_cancellation_token`，不属于aggregate；off/shadow/enforce/no-client与AgentRun/legacy admission继续由现有测试锁定。
- P0的10项deferred behavior、schema/data、`prod`、Frontend、Rust、依赖与`docker_cmd.md`正文均未触及。
