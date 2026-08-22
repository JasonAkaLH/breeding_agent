# 统一 Agent Loop Cutover Readiness

- **日期**：2026-08-22
- **证据状态**：closed
- **适用分支**：main
- **tested commit**：af5dfd8d52a2993eac76eb2314de425306f1fe0a
- **tested tree**：c1c4656fb78dccf6b6ae5cbdab196dbd0e8f34de
- **证据边界**：该commit是P5-B已测试代码检查点；本证据的后续docs-only commit不改变已测试runtime/frontend树。P6-A必须绑定
  当时HEAD重新复验，不得沿用本文件替代clean-archive rehearsal。
- **schema inventory状态**：closed inventory；只登记Phase 7待删对象，本阶段不执行破坏性迁移。
- **remaining blocker结论**：Phase 5无未知入口或未闭合门禁；Phase 6/7的计划工作仍是显式blocker，禁止提前部署或删schema。

## 1. Phase 状态

| Phase | 状态 | 证据锚点 |
|---|---|---|
| Phase 0 | proof_complete | `5d59e85`、`5d3c82d`；active inventory和Agent Model合同闭合 |
| Phase 1 | proof_complete | `3581f13`～`1c3c71d`；SQLite、真实PostgreSQL、Runtime Sidecar/Rust状态与lease parity闭合 |
| Phase 2 | proof_complete | `4e1557c`～`f2299b6`；唯一Invocation Kernel、Catalog/Policy、Skill/MCP适配闭合 |
| Phase 3 | proof_complete | `8e21e01`～`066f1e6`；durable loop、compaction、atomic final闭合 |
| Phase 4 | proof_complete | `b982386`、`927e122`；multi-waiting、continuation、recovery、cancel/no-replay闭合 |
| Phase 5 | proof_complete | P5-A `768dd00`；P5-B tested commit `af5dfd8`；后端、Frontend、可访问性及本readiness闭合 |
| Phase 6 | pending | 只允许从P6-A clean rollback freeze开始；当前正式start仍为DAG |
| Phase 7 | pending | backup/restore operator、三backend破坏性schema删除和最终真实MCP证明尚未开始 |

## 2. Start、resume、cancel 与 recovery 入口

以下集合与`active-prd-inventory.md`的9行closed inventory双向一致，无未知入口。当前控制面保持DAG；P6-B必须在同一受审
bundle内全部切换到replacement authority，不得保留请求flag、fallback或双runtime。

| entry_id | 类别 | 当前code anchor/control | Phase 6 replacement | 当前状态 |
|---|---|---|---|---|
| ordinary_submit | start | `src/api/runtime.py::submit_message` → WorkflowProvider/WorkflowPlan | `AgentLoopOrchestrator.start_or_resume` | registered |
| explicit_skill_submit | start | 同一submit入口 → SkillWorkflowProvider/forced finalizer | required first Skill call进入同一Run | registered |
| explicit_mcp_submit | start | 同一submit入口 → fixed `mcp.dispatch` plan/finalizer | required pinned `mcp.dispatch`进入同一Run | registered |
| skill_missing_input_answer | resume | `src/api/runtime.py::answer_interrupt` → rebuilt DAG resume | 原Run/call continuation | registered |
| mcp_approval_answer | resume | 同一answer入口 → MCP pending action/DAG continuation | 原Run/call durable authority | registered |
| mcp_mrtr_answer | resume | 同一answer入口 → MRTR/DAG resume | 原Run/call durable authority | registered |
| mcp_remote_completion | resume/recovery | `src/api/runtime.py::_consume_mcp_continuation_command` → persisted DAG node | 单一result恢复原Run/call | registered |
| task_cancel | cancel | `src/api/runtime.py::cancel_task` → CancellationService/DAG handle | AgentRun cancel与late-result fencing | registered |
| crash_startup_recovery | recovery | `src/api/runtime.py::_recover_user_mcp_calls` → MCP/DAG startup recovery | AgentRun claim与no-replay recovery | registered |

## 3. Test evidence

| 证据域 | 当前tested commit结果 |
|---|---|
| P5-A canonical backend | 41 tests passed |
| API discover | 493 tests passed |
| Storage discover | 398 tests passed；7项既有外部PG环境skip，真实PG门禁已在Phase 1独立闭合 |
| Observability discover | 39 tests passed |
| Frontend focused Agent reducer/SSE | 55 tests passed |
| Frontend App multi-waiting | 1 test passed；完整suite同时覆盖该用例 |
| Frontend Vitest | 21 files、313 tests passed |
| Frontend typecheck | `npm run typecheck` passed |
| Frontend production build | `npm run build` passed；仅保留既有large chunk warning |
| 静态与保护 | `git diff --check`通过；`docker_cmd.md`只完成exists/ignored/untracked检查，未读取内容 |

Agent fixture已证明：waiting保持Task `running`；graph为fixed `required/hard`与`edges=[]`且零TaskEdge读取；Agent reasoning只
transient；同event ID同payload幂等、不同payload触发resync；tool result不成为assistant answer；两个waiting按interrupt/node
逐项呈现，回答一个后第二个继续waiting并恢复composer focus。既有approval Dialog focus/keyboard/semantic tests和完整App
可访问性回归均通过；本阶段未修改Dialog或新增DOM控件。

## 4. Active docs 与旧测试

- `active-prd-inventory.md`证据状态closed，当前validator发现26份active PRD、55个legacy test和9个执行/恢复入口；均有唯一
  disposition、replacement authority、owner phase和evidence command。
- Phase 6前所有行保持`registered`是预期状态；P6-C必须更新为rewritten/superseded/historical/removed并重新扫描，未知新增项
  会被validator拒绝。
- 当前权威仍为DAG源码与`docs/prd/backend/00-主代理框架PRD.md`；Phase 6完成前本目录不得被解释为已切正式route。

## 5. DAG physical schema inventory

| 对象 | Python/SQLite/PostgreSQL | Runtime Sidecar/Rust/proto | Phase 7处置 |
|---|---|---|---|
| Task DAG字段 | `Task.root_node_id`；SQLAlchemy `submitted_tasks.root_node_id`，PostgreSQL复用同metadata/bootstrap | proto `TaskRecord.root_node_id`及Rust/Python facade字段 | 删除物理字段；Task DTO其余兼容字段保留 |
| TaskNode DAG-only字段 | `criticality`、`dependency_type`、`retry_policy`、`timeout_policy`、`resource_class`及对应SQL列 | proto `TaskNodeRecord`字段、Rust record/sqlite adapter/facade | 删除持久字段；graph DTO继续固定投影`required/hard` |
| TaskEdge | core model、StoragePort、SQLite/PostgreSQL table/repository/read/write/list path | Save/List RPC、`TaskEdgeRecord`、Rust kernel/sqlite adapter及idempotency表 | 全部物理删除；`GET /graph`继续返回`edges=[]`且不读repository |

主要锚点：`src/core/models.py`、`src/storage/sqlite/models.py`、`src/storage/sqlite/repositories.py`、
`src/storage/postgres/bootstrap.py`、`src/storage/runtime_sidecar_*`、`native/proto/maf/runtime/v1/runtime.proto`及
`native/crates/maf_runtime_sidecar/`。P6只允许删除运行时读取/wiring；上述physical schema/proto必须保留到P7 operator的
backup→restore-check→apply门禁。

## 6. Remaining blockers 与 P6-A 进入条件

| blocker | 解除条件 | 当前结论 |
|---|---|---|
| 当前9个入口仍命中DAG | P6-B全入口spies/E2E只命中Agent | expected pending work |
| 尚无最后DAG clean rollback archive | P6-A从clean archive在隔离库复验Phase 0～5 | blocks P6-B |
| DAG runtime/config/event/test删除尚未执行 | P6-B单一受审bundle完成并通过零引用扫描 | blocks cutover_complete |
| 三backend备份恢复/operator未实现 | P7-A真实restore proof | blocks destructive schema deletion |
| 真实MCP最终smoke未执行 | P7-C受控授权smoke或用户明确书面waiver | blocks final complete |

P6-A进入条件已满足：分支为`main`、Phase 0～5为`proof_complete`、入口/文档/schema inventory无unknown、Frontend三门禁与
当前P5后端证据闭合。进入P6-A后必须先复验工作树归属、当前commit/tree和全部Phase 0～5门禁；本文件不授权部署`prod`、
不授权迁移旧DAG Task，也不允许跳过P6-A直接切换或删除。

## 7. 验证命令

```bash
conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 5 --require-closed
```

预期结果：`unified_agent_loop_phase_5_evidence_closed`。任何入口、active文档、旧测试、必需字段或证据状态漂移都必须重新打开
本报告并阻断P6-A。
