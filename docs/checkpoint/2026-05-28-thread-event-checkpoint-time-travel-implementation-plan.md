# Thread Event Log + Execution Checkpoint + Time Travel 实施计划

日期：2026-05-28

状态：实施计划草案（基于已复审设计文档）

输入设计：`docs/checkpoint/2026-05-28-thread-event-checkpoint-time-travel-design.md`

分支：`dev`

## 1. 目标与成功定义

本计划把已确认的 Thread Event Log + Execution Checkpoint + Time Travel 设计拆成可执行、可验证的实施阶段。计划只定义执行顺序、改动边界、验收标准与验证命令；本文件不包含源码实现。

成功状态：

1. 新 runtime 使用 `/api/v2` thread / branch / run / checkpoint 命名，且新接口不暴露 `conversation_id` / `task_id` 作为 v2 contract 字段。
2. `thread_event_log` 成为 v2 事实源；`execution_checkpoint` 成为执行状态快照；projection 成为读模型。
3. 普通用户可以从节点级 checkpoint 重新执行，系统创建新 branch、新 run，并自动切换 active branch。
4. Interrupt resume 必须绑定 checkpoint；checkpoint 缺失、失效、引用缺失时 fail closed。
5. Branch / thread / user 删除均物理清理，删除后不能通过 API、checkpoint、time travel 或 artifact download 找回历史。
6. v2 使用新 PostgreSQL 开发 DB fresh start，不迁移 v1 数据，不做 v1/v2 双写。

## 2. 计划依据与当前证据

| 证据 | 当前事实 | 计划影响 |
| --- | --- | --- |
| `docs/checkpoint/2026-05-28-thread-event-checkpoint-time-travel-design.md:42-45` | 设计明确 `thread_event_log` 是事实源、`execution_checkpoint` 是恢复源、projection 是查询层。 | 实施顺序必须先建立 event/checkpoint/projection 内核，再接 API/前端。 |
| `docs/checkpoint/2026-05-28-thread-event-checkpoint-time-travel-design.md:59-75` | 范围包含 v2 DB、API v2、event log、checkpoint、projection、普通用户 branch/time travel、物理删除；非目标包括不迁移旧数据、不 v1/v2 双写。 | 计划采用 fresh start 和破坏性 v2 命名，不做旧 API 兼容层。 |
| `docs/checkpoint/2026-05-28-thread-event-checkpoint-time-travel-design.md:1144-1153` | FR-1 至 FR-10 定义 v2 DB、event log、checkpoint、time travel、branch UI、删除和 API 命名验收。 | 每个实施阶段必须绑定 FR 验收与自动化测试。 |
| `src/core/models.py:80-170` | 当前 Message/Task/TaskNode/Artifact/EventRecord 仍以 conversation/task 为中心。 | v2 需要新增 thread/run/branch 模型，不应在旧模型上做命名折中。 |
| `src/core/models.py:244-255` | 当前 Checkpoint 只有 `snapshot_ref`、`snapshot_kind`、`resume_token`、`invalidated_at` 等薄字段。 | PRD-4 必须重建 checkpoint schema，保存执行态快照。 |
| `src/core/contracts.py:180-266` | StoragePort 现有接口围绕 message/task/node/event/artifact/interrupt/checkpoint 分散读写。 | PRD-1/2 应新增 v2 storage/event-store contract，避免把 v2 语义塞进旧 StoragePort。 |
| `src/storage/sqlite/models.py:191-207` | 旧 event_record 表按 task/conversation 记录事件。 | v2 event log 需要 branch 内 `event_seq`、idempotency、projection 状态与 actor/causation 字段。 |
| `src/storage/sqlite/models.py:305-321` | 旧 checkpoint row 没有 state snapshot、event cursor、branch lineage、state hash。 | PRD-4 的 schema/test 必须覆盖完整 snapshot 字段和敏感字段约束。 |
| `src/api/runtime.py:340-402` | 当前提交流程创建 conversation current_task、message、task 与 `task.accepted` event。 | PRD-3 需要改为 thread active branch + message/run events + projection 原子写。 |
| `src/api/runtime.py:648-680` | 当前执行调度以 `request.task_id` 管理 `_running_tasks`。 | PRD-3 需要用 run_id 和 thread 单 active run 约束替代 task-centric 调度。 |
| `src/api/runtime.py:760-792` | 当前 assistant 历史由 final answer event/artifact 选择并写回 Message。 | PRD-3/4 需决定 v2 assistant finalized event、message_projection 与 artifact projection 的同事务边界。 |
| `src/orchestration/service.py:548-566` | 当前 `node_outputs` 是执行期内存字典。 | PRD-4 必须把 node_outputs/dependency_outputs/next_node_ids 写入 checkpoint，保证 resume/time travel。 |
| `src/orchestration/service.py:604-612` | 节点执行后 output_payload 留在 orchestration 内存流程。 | PRD-3/4 需要在 node 完成/失败/waiting 边界创建 checkpoint。 |
| `frontend/src/App.tsx`、`frontend/src/api/client.ts`、`frontend/src/api/types.ts` | 前端当前以 conversation/task/events 类型组织业务台。 | PRD-5/8 需要迁移前端 Thread/Branch/Run/Checkpoint 类型和 branch switcher。 |

## 3. 总体实施顺序

实施应按“底座 -> 读模型 -> 执行链路 -> checkpoint -> time travel -> 删除 -> 前端 -> 文档/发布门禁”顺序推进，避免在缺少事实源和 projection 的情况下先改 UI 或执行链路。

```text
P0 准备与保护门禁
P1 v2 Thread Event Store 内核
P2 Projection 与 v2 storage/API 读写基础
P3 Run / Node / Artifact 执行链路
P4 Execution Checkpoint 与 Interrupt Resume
P5 Time Travel 与 Branch 版本行为
P6 物理删除与全局清理
P7 前端 Thread/Branch/Run UI 接入
P8 API 文档、回归矩阵、部署/DB runbook
P9 总体验证、性能/安全扫描、旧命名防回归
```

## 4. 实施阶段详情

### P0：准备与保护门禁

目标：确保 v2 工作只在 `dev` 分支和新 PostgreSQL DB 中发生，不污染 v1 runtime。

改动范围：

- `docs/checkpoint/`：落地 PRD/test-spec 子文档目录结构。
- `config` / runtime config seam：新增 v2 backend 配置读取约定，但不得提交真实密码。
- `tests/`：新增 v2 feature gate / schema target 的最小失败测试。

任务：

1. 建立 `docs/checkpoint/prd/` 与 `docs/checkpoint/test-spec/`，把设计中的 PRD-1 至 PRD-6 拆成可实施子 PRD。
2. 定义 v2 显式开关，例如 `state_platform.backend=postgresql_v2`。
3. 定义 v2 DB 允许名单和 fail-closed 规则：目标 DB 不应等于当前 v1 远端库名。
4. 补静态测试：v2 docs/API/DTO 中不得出现新 contract 字段 `conversation_id` / `task_id`。

验收：

- 未配置 v2 backend 时，现有 v1 测试仍按原路径运行。
- 配错 v2 DB 名称时，bootstrap 测试应失败并返回稳定错误。
- 文档和测试中没有真实 DB 密码/API key。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

### P1：v2 Thread Event Store 内核

目标：建立 v2 source of truth：thread、branch、event log、branch event cursor、idempotency。

主要文件/模块：

- 新增 `src/thread/` 或 `src/state/thread/`：v2 domain models 与 event store contract。
- 新增 `src/storage/postgres/thread_schema.py` 或等价 schema descriptor。
- 新增 `tests/storage/test_thread_event_store_*.py`。

任务：

1. 新增 v2 dataclasses / DTO：`Thread`、`ThreadBranch`、`ThreadEvent`、`BranchEventCursor`。
2. 建立 PostgreSQL DDL descriptor：`thread`、`thread_branch`、`thread_event_log`、`branch_event_cursor`、schema ledger。
3. 实现 event append contract：同 branch `event_seq` 单调、不同 branch 可并发、idempotency key 重放返回原事件。
4. 实现 branch 创建和 active branch 初始 main branch。
5. 实现 fail-closed bootstrap：schema 缺失、hash 不匹配、权限不足、DB 名称不在允许范围时不得启动。

验收：

- FR-1、FR-2 通过。
- `unique(thread_id, branch_id, event_seq)` 与 `idempotency_key` 约束有测试。
- 并发写同 branch 不乱序；并发写不同 branch 不互相阻塞。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_thread_event_store*.py'
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_thread*.py'
```

### P2：Projection 与 v2 Storage/API 基础

目标：建立 projection 读模型和 `/api/v2/threads` 基础 API。

主要文件/模块：

- 新增/调整 `src/api/routes/threads.py`、`src/api/dto.py`。
- 新增 v2 runtime assembly seam，避免污染 `src/api/routes/conversations.py`。
- 新增 `thread_projection`、`branch_projection`、`message_projection` 表。
- 前端 API 类型先增量定义，但 UI 可暂不切换。

任务：

1. 实现 thread 创建/list/get/delete 的 v2 DTO 与 route skeleton。
2. 实现 branch list/activate 的基础 projection 更新。
3. 实现 message_projection 写入与读取，GET messages 默认 active branch，可指定 branch。
4. 同事务写 event log + projection；投影失败则 event 回滚。
5. 增加 API schema/static 测试：v2 response 字段使用 `thread_id`、`branch_id`，不使用新 `conversation_id`。

验收：

- FR-5、FR-6、FR-9、FR-10 的 API 基础路径通过。
- 非 active branch 发送消息 fail closed。
- Projection 可从测试事件重建或至少可验证与 event 同事务一致。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_v2_thread*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_thread_projection*.py'
```

### P3：Run / Node / Artifact 执行链路

目标：把一次用户消息执行从 task-centric 链路迁移到 run-centric 链路。

主要文件/模块：

- `src/api/runtime.py`：新增 v2 submit/run orchestration path，逐步隔离旧 conversation/task path。
- `src/orchestration/service.py`：新增 run-aware execution adapter 或 v2 orchestration service。
- `src/core/models.py` 或新 v2 models：`RunProjection`、`NodeProjection`、`ArtifactProjection`。
- `src/api/routes/runs.py`、`src/api/routes/artifacts.py`。

任务：

1. 新增 `run_projection`、`node_projection`、`artifact_projection` schema。
2. 将 v2 message submit 写 `message.user_created` + `run.accepted` + projections。
3. Planner 生成 plan 后写 `plan.created` event。
4. Node started/completed/failed/waiting 写事件，并更新 node/run/artifact projection。
5. Finalizer 写 `assistant.finalized` event 与 assistant message projection。
6. SSE v2 `GET /api/v2/runs/{run_id}/events` 只输出该 run/branch 事件。

验收：

- 单条 v2 用户消息能完整执行并生成 run/node/artifact/message projections。
- run graph API 能返回 node status 和 checkpoint placeholder 字段。
- SSE 事件体包含 `thread_id`、`branch_id`、`run_id`。
- 同一 thread active run 冲突返回 `run_conflict`。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_v2_run*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_v2*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_v2*.py'
```

### P4：Execution Checkpoint 与 Interrupt Resume

目标：实现 execution checkpoint 作为执行状态快照，并接入 interrupt resume。

主要文件/模块：

- 新增 `src/checkpoint/` 或 `src/lifecycle/checkpoint_service.py`。
- `src/lifecycle/interrupt_service.py`：新增 v2 interrupt-checkpoint 绑定路径。
- `src/orchestration/service.py`：在稳定边界生成 checkpoint。
- `tests/lifecycle/test_v2_interrupt_resume.py`、`tests/storage/test_execution_checkpoint*.py`。

任务：

1. 建立 `execution_checkpoint` schema：state snapshot、state hash、ledger fingerprint、event_seq、branch lineage、resume_token。
2. 定义 snapshot schema version 和敏感字段 reject/scan helper。
3. 在 `plan.created`、`node.completed`、`node.failed`、`node.waiting_for_input`、`interrupt.opened`、`interrupt.answered`、`assistant.finalized`、`run.completed/failed` 边界写 checkpoint。
4. 将 `node_outputs`、`dependency_outputs`、`next_node_ids`、artifact refs、message refs、memory/context refs 写入 checkpoint。
5. Interrupt opened 必须引用 checkpoint；answer 后按 checkpoint 恢复上下文。
6. checkpoint 失效、branch 删除、artifact/message/payload refs 缺失时 resume/time travel fail closed。

验收：

- FR-3、FR-7 通过。
- checkpoint payload 不包含历史消息原文、大 SQL 结果、artifact 内容、secret。
- v2 waiting_for_input 能通过 checkpoint 恢复执行。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_execution_checkpoint*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_v2_interrupt*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_v2_checkpoint*.py'
```

### P5：Time Travel 与 Branch 版本行为

目标：实现普通用户可用的节点级重新执行。

主要文件/模块：

- `src/api/routes/checkpoints.py`：`POST /api/v2/checkpoints/{checkpoint_id}/time-travel`。
- v2 branch service：fork、activate、lineage、idempotency。
- `frontend/src/api/types.ts`、`frontend/src/api/client.ts`、`frontend/src/domain/*`：branch/run/checkpoint 类型。

任务：

1. 实现 checkpoint 可用性校验：thread/branch/checkpoint/ref/schema/hash。
2. 实现 time travel idempotency：`user_id + checkpoint_id + client_request_id`。
3. 创建新 branch，写 `branch.forked` event。
4. 创建新 run，写 `run.replay_started` event。
5. 默认复用 checkpoint 前 node outputs，仅执行 checkpoint 后 next nodes。
6. 写 `branch.activated` event 并更新 active branch。
7. API 返回新 `branch_id`、`run_id`、`active_branch_id`、SSE URL。

验收：

- FR-4、FR-5、FR-6 通过。
- 双击 time travel 不创建重复 branch。
- 原 branch 不被修改；新 branch 显示 fork 点前历史 + fork 后新结果。
- 非 active branch 只读，发送消息返回 `branch_not_active`。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_v2_time_travel*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_v2_time_travel*.py'
```

### P6：物理删除与全局清理

目标：确保 branch/thread/user 删除是物理清理，并不能被 checkpoint/time travel/artifact 绕过。

主要文件/模块：

- v2 deletion service / runner。
- `src/api/routes/threads.py`、branch delete API、user delete API 所在模块。
- artifact storage cleanup helpers。
- `tests/storage/test_v2_physical_delete*.py`、`tests/api/test_v2_delete*.py`。

任务：

1. 实现 branch 删除：级联子 branch，取消 active run/SSE，删除 projections、events、checkpoints、payload/context refs、artifact metadata、physical files。
2. 实现 thread 删除：删除所有 branches 和 thread 级历史。
3. 实现 user 删除：删除该用户所有 threads/branches/history/artifacts/uploads/memory。
4. 删除中对象对业务 API 隐身；删除失败标记 `deleting_failed`，不得恢复为正常可见。
5. 脱敏 audit 只记录 delete requested/completed/failed，不含正文/snapshot/artifact 内容。
6. 删除后 checkpoint/time travel/resume/artifact download 均返回稳定错误。

验收：

- FR-8 通过。
- 删除 active branch 必须提供 replacement 或删除整个 thread。
- 删除 parent branch 默认删除子 branch。
- 删除后文件系统/object storage 没有 branch-scoped artifact 残留。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_v2_physical_delete*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_v2_delete*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_v2_delete*.py'
```

### P7：前端 Thread / Branch / Run UI 接入

目标：前端业务对话台迁移到 v2 命名和 branch/time travel 交互。

主要文件：

- `frontend/src/api/types.ts`
- `frontend/src/api/client.ts`
- `frontend/src/api/taskEvents.ts` 或新增 `runEvents.ts`
- `frontend/src/domain/taskEvents.ts` 或新增 `runEvents.ts`
- `frontend/src/App.tsx`
- `frontend/src/styles.css`

任务：

1. 新增 v2 API client 类型：Thread、Branch、Run、Checkpoint、Artifact。
2. 将前端状态模型从 conversation/task 迁移到 thread/run；如需阶段过渡，限制在 v2 adapter 内，不扩散到 UI 业务层。
3. 实现 branch switcher：显示当前版本、历史版本、fork 来源、状态。
4. 非 active branch 输入框禁用，并提供“设为当前版本”。
5. run graph / node 列表展示“重新从这一步执行”。
6. time travel 成功后自动切换到新 branch，订阅新 run SSE。
7. 删除 branch/thread/user 后 UI 移除对应历史并清理本地状态。

验收：

- 普通用户可从节点 checkpoint 触发重新执行。
- 旧 branch 可查看但只读。
- active branch 切换后新消息写入新 active branch。
- 删除提示清楚说明永久删除。

验证：

```bash
cd frontend
npm test -- --run
npm run build
```

### P8：API 文档、运行手册与部署配置

目标：让 v2 API、DB、删除和 time travel 操作可交付、可诊断。

任务：

1. 新增或替换 `docs/api` v2 API 文档，明确 `/api/v2/threads`、`runs`、`checkpoints`。
2. 新增 `docs/checkpoint/runbook-*.md`，记录 v2 DB 创建、schema bootstrap、删除 retry、time travel diagnostics。
3. 更新 Docker/部署文档中 v2 DB 配置示例，禁止写真实密码。
4. 增加静态文档测试：v2 docs 不出现旧 `/api/v1/conversations` 作为新 contract。
5. 记录 rollback：停 v2 runtime，切回旧 runtime/DB；不做 v2 数据回写 v1。

验收：

- 文档能让开发者在新 PostgreSQL DB 上启动 v2。
- API docs 覆盖 v2 path、request、response、错误码。
- Runbook 包含最小判别命令和脱敏原则。

验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_api_doc*.py'
```

### P9：总体验证与质量门禁

目标：在进入执行收口前，确保 v2 全链路可靠。

总体验证命令：

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
cd frontend && npm test -- --run && npm run build
```

必须新增的专项检查：

1. v2 API schema 不暴露新 `conversation_id` / `task_id` 字段。
2. checkpoint snapshot 敏感字段扫描。
3. 删除后 artifact 文件系统残留扫描。
4. event/projection transaction rollback 测试。
5. time travel idempotency 双击测试。
6. branch activation 与发送消息竞态测试。

## 5. 依赖关系与不可并行边界

| 依赖 | 说明 |
| --- | --- |
| P1 -> P2 | Projection 必须依赖 thread_event_log 和 branch cursor。 |
| P2 -> P3 | Run API 和 orchestration 需要 projection 基础。 |
| P3 -> P4 | Checkpoint 需要 run/node/artifact execution states。 |
| P4 -> P5 | Time travel 必须依赖可恢复 checkpoint。 |
| P5 -> P7 | 前端 branch UI 需要 time travel/branch API contract。 |
| P6 可与 P7 部分并行 | 删除服务可在 API contract 稳定后与前端删除 UI 并行，但删除验收必须等后端先通过。 |
| P8 可贯穿执行 | API docs/runbook 可随每个 PRD 更新，但最终必须统一校验。 |

可并行建议：

- P1 schema contract 与 P2 API DTO 初稿可并行，但 P2 不得合并到可运行路径前通过 P1。
- P4 checkpoint schema 与 P7 前端 checkpoint affordance 原型可并行，但 UI 只能接 mock contract，不能绕过 P4。
- P6 删除测试矩阵可提前编写，后端删除实现完成后再接入。

## 6. 风险与缓解

| 风险 | 等级 | 缓解 |
| --- | --- | --- |
| source of truth 切换导致 v1 runtime 被污染 | 高 | v2 显式 backend、新 DB 允许名单、v2 schema hash、禁止 v1/v2 双写。 |
| event/projection 半成功 | 高 | 第一版强制同事务写 event + projection，失败整体回滚。 |
| checkpoint 泄露消息正文或大结果 | 高 | checkpoint schema tests + 敏感字段扫描 + payload refs 替代大内容。 |
| 删除后仍可从 checkpoint/artifact 找回 | 高 | 删除级联覆盖 event/checkpoint/projection/files；删除后访问专项测试。 |
| time travel 重复点击创建多个 branch | 中 | idempotency key 强约束并返回同一 branch/run。 |
| branch activation 与发送消息竞态 | 中 | thread row lock / active branch conditional write。 |
| 前端旧 conversation/task 命名残留 | 中 | API/types/static scan，v2 UI state 统一 Thread/Run 命名。 |
| 新 DB 配置误连旧库 | 高 | DB name allowlist + schema marker + startup fail closed。 |

## 7. 执行交付建议

### 推荐执行模式

该计划跨 storage、API、orchestration、checkpoint/lifecycle、frontend、docs 和 deletion，建议后续用 **Team + Ultragoal** 执行：

- Ultragoal：持有总目标、验收矩阵、阶段 checkpoint 和最终证据。
- Team：并行推进 schema/storage、API/runtime、frontend、deletion/tests 文档等工作流。

### 建议 agent roster

| Lane | 推荐角色 | Reasoning | 责任 |
| --- | --- | --- | --- |
| Storage/Event Store | architect + executor | high/medium | v2 schema、event store、projection transaction。 |
| Runtime/Orchestration | architect + executor | high/medium | run execution、node event、checkpoint hook。 |
| Lifecycle/Delete | debugger + executor | high/medium | interrupt resume、物理删除、fail-closed。 |
| Frontend | executor + designer | medium/high | Thread/Branch/Run UI、branch switcher、time travel。 |
| Tests/Verification | test-engineer + verifier | medium/high | acceptance matrix、e2e、delete residue scans。 |
| Docs/Runbook | writer | medium | API docs、DB runbook、rollback。 |

### Team launch hint

后续执行时可使用：

```text
$ultragoal docs/checkpoint/2026-05-28-thread-event-checkpoint-time-travel-implementation-plan.md
```

如果并行推进：

```text
$team docs/checkpoint/2026-05-28-thread-event-checkpoint-time-travel-implementation-plan.md
```

Team 关闭前必须证明：

1. 每个 PRD phase 对应 tests 通过。
2. FR-1 至 FR-10 均有验证证据。
3. 删除后残留扫描通过。
4. checkpoint 敏感字段扫描通过。
5. `/api/v2` 命名静态扫描通过。
6. 前端 build/test 通过。

## 8. Stop 条件

计划阶段完成条件：

- 本实施计划写入 `docs/checkpoint/`。
- 计划覆盖设计文档 FR/NFR、风险、验收、验证命令。
- 工作区只包含文档/计划变更，无源码实现。
- 用户确认后再进入 `$ultragoal` / `$team` 执行，不在 planning agent 内直接实现。
