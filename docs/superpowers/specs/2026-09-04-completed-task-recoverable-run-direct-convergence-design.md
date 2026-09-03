# COMPLETED Task 与 Recoverable Run 直接收敛设计

状态：`approved_pending_written_review`
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

当前backend-dev `0.1.32`不含本设计行为，不得把它记为本设计完成版本。若后续发布新镜像，必须使用新tag，
更新受保护部署命令并验证远端`linux/amd64` manifest、镜像无`/app/config.yaml`及收敛入口。

## 6. 失败与幂等

- Task不是`COMPLETED`、identity不一致或Run不再recoverable：拒绝本操作；
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

License Requirement：复用现有Python、SQLAlchemy、Agent repository、Runtime Sidecar
`CommitAgentState`、unittest和Rust测试链；无新增依赖、第三方代码或许可变化。
