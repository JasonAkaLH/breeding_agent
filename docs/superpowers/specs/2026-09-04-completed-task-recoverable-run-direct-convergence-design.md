# COMPLETED Task 与 Recoverable Run 直接收敛设计

状态：`published_pending_deploy`
日期：2026-09-04
目标分支：`main`

## 1. 背景与决定

Skill revision v2首次开发部署暴露了Task与AgentRun终态分裂：`e62c86db`已允许既有
`Task=FAILED/CANCELLED`把recoverable Run收敛到同名终态，但`Task=COMPLETED`仍因缺少完整
Agent final-output证据而fail closed。

用户现决定把`Task=COMPLETED`本身视为Run生命周期收敛的充分authority：只要Task/Run identity完整且
Run仍为recoverable，runtime就直接把Run收敛为`COMPLETED`。该决定不把本次操作解释为Agent重新产生了
成功业务结果。

## 2. 唯一允许的转换

仅允许以下输入：

- Task存在，`status=COMPLETED`；
- Run存在，`task_id`与`conversation_id`和Task精确一致；
- Run状态为`RUNNING`、`WAITING_FOR_INPUT`或`WAITING_FOR_DEPENDENCY`；
- writer使用当前Run的`revision`与`claim_token`执行CAS。

原子写入后的Run：

- `status=COMPLETED`；
- `waiting_call_item_ids=()`；
- `claim_owner`、`claim_token`、`lease_expires_at`清空；
- `revision += 1`；
- `updated_at`与`terminal_at`使用同一writer时间；
- `terminal_reason_code=agent_terminal_task_completed_run_convergence`。

Task保持`COMPLETED`。不改变其identity、时间、routing或assignment字段。

## 3. 不生成的成功证据

该操作不是`commit_final`，不得新增或改写：

- Assistant Message或AgentItem；
- `agent.final_output` TaskNode；
- Artifact；
- Event；
- AgentFinalReceipt或final projection；
- 已有普通TaskNode、Interrupt、Message、AgentItem或Artifact历史。

因此Run的`COMPLETED`只表示生命周期与已有Task authority一致，不声明存在新的Agent最终回复。

## 4. 实现边界

`AgentAtomicWriter`新增单一方法承载该转换。SQLite实现使用同一数据库事务锁定Run和Task；PostgreSQL继续
继承共享SQLAlchemy实现。Runtime Sidecar实现复用现有`CommitAgentState` wire和稳定幂等key，使用独立
operation name并携带原样`Task=COMPLETED`；现有非`commit_final`分支允许零Item/Node/Artifact/projection，
因此不修改Rust、proto或schema。

`ApiRuntime._recover_agent_run()`在任何Conversation、Message、prepared authority或Skill bundle读取前处理
终态Task：FAILED/CANCELLED保持`e62c86db`路径，COMPLETED调用新writer；其他未知组合继续fail closed。
写入后回读Run和Task，要求二者均为`COMPLETED`，再best-effort清理current-task指针。

## 5. Audit 与发布

受保护`docker_cmd.md`的只读pre/post audit增加
`terminal_completed_task_recoverable_run`计数：pre阶段把它计为可收敛项，post阶段要求归零。audit仍只输出
计数和active revision，不输出Task ID、DSN或业务内容，也不修改数据库。

backend-dev `0.1.32`不含本设计行为，不得部署为本设计版本。实现提交`5caac314`已发布为
backend-dev `0.1.33`；远端OCI index digest为
`sha256:97d9885c9684f2ff8eb57bf8e0c94bb5409ee9b9d18e5e0192b97b0ade3c191e`，包含`linux/amd64`
manifest `sha256:0f69e6a87f913a836093aa1e9f2d51916c425e1e27b59694d929e366ba6298ca`和attestation
manifest `sha256:a095c9643525944ec1f9fae73e9fdbdde44978282c5a3819245d62a904481ad2`。按远端digest
重拉后已确认镜像不含`/app/config.yaml`，且SQLite、Runtime Sidecar和Runtime收敛入口可导入。

## 6. 失败与幂等

- Task不是`COMPLETED`、identity不一致，或Run既不recoverable也不是本设计的精确稳定终态：拒绝本操作；
- CAS冲突：重新读取authority；若Run已是上述稳定收敛结果则视为完成，否则fail closed；
- Task/Run写入或回读不一致：startup失败，不恢复流量；
- 写入成功后重启：Run不再进入recoverable列表，不重复产生任何对象或副作用。

## 7. 验收标准

1. SQLite、PostgreSQL继承实现和Runtime Sidecar repository合同均支持同一直接收敛语义；
2. `Task=COMPLETED + recoverable Run`启动后得到`Run=COMPLETED`和稳定reason code；
3. Message、AgentItem、TaskNode、Artifact、Event、FinalReceipt和final projection数量及内容不变；
4. FAILED/CANCELLED既有收敛行为不回归，identity/CAS/未知状态继续fail closed；
5. audit pre接受并分项计数，post要求该计数为零；
6. 聚焦API/Storage/Runtime Sidecar测试、相关分层回归、compileall、Ruff、Rust门禁与`git diff --check`通过；
7. 不修改数据库schema、公开API、Frontend、MCP parser/projection、外部Skill或`prod`。

## 8. 实施结果

`5caac314`已完成`AgentAtomicWriter`、SQLite/PostgreSQL共享实现、Runtime Sidecar adapter、
AgentLoopOrchestrator和startup recovery接线。repository合同锁定Run唯一变更、Task原样、业务成功对象零新增/零改写、
精确幂等重放、错误authority拒绝和事务回滚；FAILED/CANCELLED路径保持不变。

聚焦85项、API 649项、Storage 573项（14 skip）、Orchestration 200项和E2E 12项通过；compileall、变更面
Ruff、Rust fmt/clippy/cargo test/nextest（198项）及cargo-audit通过。全仓Ruff仍有12个与本次无关的既存测试
unused-import错误，未纳入本次修改。受保护`docker_cmd.md`已加入COMPLETED分项、纳入pre/post terminalizable门禁并
统一切到`0.1.33`，bash、Python audit语法、镜像内imports/classifier及保护属性均通过。未执行开发部署或直接修改
数据库，真实hard cut和post-audit仍待执行。

License Requirement：复用现有Python、SQLAlchemy、Agent repository、Runtime Sidecar
`CommitAgentState`、unittest和Rust测试链；无新增依赖、第三方代码或许可变化。
