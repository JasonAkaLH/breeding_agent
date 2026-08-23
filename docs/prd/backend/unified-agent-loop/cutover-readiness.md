# 统一 Agent Loop Cutover Readiness

- **日期**：2026-08-23
- **证据状态**：closed
- **适用分支**：main
- **tested DAG commit/tree**：`7bb8a05f8acdaf05349624dbcbc68027fa8f8f08` / `cfdb89bf3c5e083c7893f1ec60085a04ad1f9801`
- **tested Skill commit/tree**：`49b3aa0412438b55bbabc0c5ac6cad7fb14cf71f` / `06c8ff8924302a3163792ace214c4df1f9afdd14`
- **证据边界**：上述双仓检查点是P6-A已测试clean archive authority；本证据的后续docs-only commit不改变已测试runtime、
  frontend或Skill树。P6-B必须以正常commit推进，回滚时成对revert，不得用本文件替代代码、数据或pre-Phase7 schema证明。
- **schema inventory状态**：closed inventory；只登记Phase 7待删对象，本阶段不执行破坏性迁移。
- **remaining blocker结论**：P6-A门禁已闭合；P6-B/P6-C与Phase 7的计划工作仍是显式blocker，禁止提前部署、切换authority或删schema。

## 1. Phase 状态

| Phase | 状态 | 证据锚点 |
|---|---|---|
| Phase 0 | proof_complete | `5d59e85`、`5d3c82d`；active inventory和Agent Model合同闭合 |
| Phase 1 | proof_complete | `3581f13`～`1c3c71d`；SQLite、真实PostgreSQL、Runtime Sidecar/Rust状态与lease parity闭合 |
| Phase 2 | proof_complete | `4e1557c`～`f2299b6`；唯一Invocation Kernel、Catalog/Policy、Skill/MCP适配闭合 |
| Phase 3 | proof_complete | `8e21e01`～`066f1e6`；durable loop、compaction、atomic final闭合 |
| Phase 4 | proof_complete | `b982386`、`927e122`；multi-waiting、continuation、recovery、cancel/no-replay闭合 |
| Phase 5 | proof_complete | P5-A `768dd00`；P5-B tested commit `af5dfd8`；后端、Frontend、可访问性及本readiness闭合 |
| Phase 6 | in_progress | P6-A clean rollback authority已冻结；下一检查点P6-B，当前正式start仍为DAG |
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
| API discover | 502 tests passed |
| Storage discover | PostgreSQL 17七个隔离库441 tests passed，zero skip；临时库和role已精确清理 |
| Agent Skill discover | 209 tests passed，zero skip |
| Integrations discover | macOS 705 tests passed、2项Linux-only诊断skip；`linux/amd64`候选环境705 tests passed，zero skip |
| Core/Lifecycle/Orchestration/Main Agent | 46 / 36 / 209 / 65 tests passed |
| MCP Dispatch/MCP Tool/E2E | 15 / 15 / 2 tests passed |
| Observability discover | 39 tests passed |
| Scripts/Deployment | 51 / 3 tests passed |
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
| 最后DAG clean rollback archive | P6-A从双仓clean archive在隔离环境复验Phase 0～5 | resolved：双archive、只读Docker与Linux no-skip已闭合 |
| DAG runtime/config/event/test删除尚未执行 | P6-B单一受审bundle完成并通过零引用扫描 | blocks cutover_complete |
| 三backend备份恢复/operator未实现 | P7-A真实restore proof | blocks destructive schema deletion |
| 真实MCP最终smoke未执行 | P7-C受控授权smoke或用户明确书面waiver | blocks final complete |
| canonical storage discover存在环境skip，且共享单库会产生跨模块schema污染 | 为每类真实PG合同提供隔离库并使canonical storage全域命令零skip/零失败 | resolved：`10c0b9e`，441项零skip通过 |
| external Agent Skill suite曾有43项平台/外部bundle skip | 挂载与当前测试合同匹配的权威Skill bundle及所需runtime，令canonical Agent Skill命令零skip/零失败 | resolved：209项零skip、零失败 |

P6-A已达到freeze条件。最后DAG代码authority为`7bb8a05f8acdaf05349624dbcbc68027fa8f8f08`/tree
`cfdb89bf3c5e083c7893f1ec60085a04ad1f9801`，外部Skill authority为`49b3aa0412438b55bbabc0c5ac6cad7fb14cf71f`/tree
`06c8ff8924302a3163792ace214c4df1f9afdd14`。P6-B尚未改变任何route或数据；本文件不授权部署`prod`、迁移旧DAG Task、
跳过P6-B/P6-C或执行Phase 7删除。

### 6.1 P6-A历史预检证据（2026-08-22）

- 通过：Python compileall；core 46项、lifecycle 36项、integrations 705项、orchestration 209项、main_agent 65项、
  mcp_dispatch 15项、mcp_tool 15项、API 493项、E2E 2项、observability 39项、scripts 48项、deployment 3项；
  Rust `cargo_fmt`/`cargo_clippy`/`cargo_test`。integrations中2项Linux-only Result Parser在Ubuntu容器内单独2项通过。
- 真实PostgreSQL隔离库：Agent schema/transaction/lease/concurrency 4项、conversation delete 3项、legacy migration 15项、
  rollout 20项、permissions 15项、CP7 3项、MVCC 1项，合计61项零skip通过。
- storage no-skip已闭合：设置`MAF_POSTGRES_AGENT_TEST_DSN`、`MAF_POSTGRES_CONVERSATION_DELETE_TEST_DSN`、
  `MAF_POSTGRES_MVCC_TEST_DSN`、`MAF_POSTGRES_LEGACY_MIGRATION_TEST_DSN`、`MAF_POSTGRES_ROLLOUT_INTEGRATION_TEST_DSN`、
  `MAF_POSTGRES_ROLLOUT_PERMISSIONS_TEST_DSN`与`CP7_POSTGRES_VALIDATION_DSN`，并指向同一PostgreSQL 17实例的七个隔离数据库后，
  canonical storage discover实际运行441项，零skip、零失败。默认无外部环境口径仍为402项通过、7项显式skip。
- 仍未通过required no-skip口径：Agent Skill discover 200项通过但43项外部bundle/平台skip。
- 外部Skill调查：当前环境无权威部署挂载；本地其他workspace与历史Git snapshot分别只满足现行测试的部分互相冲突合同，
  未拷贝、未合成fixture、未放宽测试。
- 第三次外部环境审计：宿主`/data/peihai/vibe-breeding-dev/skills`不存在；当前backend的`/app/skill`挂载为只读空Skill卷且无
  `SKILL.md`；当前backend、本地`0.1.24`与已缓存开发镜像的`/app/skill`镜像层均无`SKILL.md`；Docker无其他Skill卷。
  因此不存在可直接复验的权威bundle，且不允许用本地漂移副本或合成fixture代替required证据。

### 6.2 P6-A冻结证据（2026-08-23）

- 双仓authority：主仓commit/tree为`7bb8a05f8acdaf05349624dbcbc68027fa8f8f08`/
  `cfdb89bf3c5e083c7893f1ec60085a04ad1f9801`；外部`vibe-breeding/dev` commit/tree为
  `49b3aa0412438b55bbabc0c5ac6cad7fb14cf71f`/`06c8ff8924302a3163792ace214c4df1f9afdd14`。
- clean archive：主仓archive SHA-256为`7746c19b1d77090581ad00f85ea9c59498615197d81b364f34f6ab704433c6db`；
  外部Skill仓archive SHA-256为`495fb0d145540b7403ed2bdc17e0f5a1ff530485cffbed3a3d1ed6583e106bcb`。
- exact bundle：两份clean archive均得到
  `sha256:38f4842d88a8d57a9df75b1bdd7284097c78429de0338ebff819358e312c4e86`，118文件、9,992,399字节、
  约25ms；两项未跟踪Finder metadata不属于Git authority，未进入archive或digest。
- clean archive回归：focused contract/route/Mini 11项、Agent Skill 209项、API 502项全部零skip、零失败；使用受跟踪的
  非敏感Agent-ready配置，未读取ignored本地配置。
- Docker候选：`linux/amd64`镜像ID
  `sha256:f4c62fad40b0d6df7d76c8050737665eeddbb3893597dea4e0fb45caaecccec4` readiness通过并注册12个合同合法Skill；
  `/app/skill`只读挂载的写入尝试被拒绝。篡改bundle容器在readiness前以
  `project_skill_bundle_digest_mismatch`退出，不存在catalog后校验窗口。
- Linux required证据：临时候选环境先构建并安装core lifecycle与safety kernels PyO3 wheel，再运行完整Integrations 705项，
  最终零skip、零失败；该结果替代macOS两项诊断skip，不改变现有运行时代码。
- 回滚边界：P6-B/P6-C失败时停止新Task，以正常`git revert`成对回退主仓cutover bundle和外部Skill提交；不使用
  reset/checkout移动分支，不恢复旧DAG Task，不回退单一仓或单一backend。

## 7. 验证命令

```bash
conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 5 --require-closed
```

预期结果：`unified_agent_loop_phase_5_evidence_closed`。任何入口、active文档、旧测试、必需字段或证据状态漂移都必须重新打开
本报告并阻断P6-A。
