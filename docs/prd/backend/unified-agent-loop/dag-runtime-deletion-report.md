# Phase 6 DAG Runtime 删除报告

- **日期**：2026-08-23
- **证据状态**：closed
- **适用分支**：`main`
- **P6 cutover bundle**：`fa4d19b`、`b21cba7`、`c3ceb32`
- **rollback authority**：主仓`7bb8a05f8acdaf05349624dbcbc68027fa8f8f08` / tree `cfdb89bf3c5e083c7893f1ec60085a04ad1f9801`；外部Skill仓`49b3aa0412438b55bbabc0c5ac6cad7fb14cf71f` / tree `06c8ff8924302a3163792ace214c4df1f9afdd14`
- **Phase 7边界**：本报告只关闭DAG runtime、wiring、config、events与测试；physical schema/proto删除尚未执行。
- **合同字段**：deleted runtime inventory、replacement tests、zero runtime reference、Phase 7 remaining inventory、rollback authority。

## 1. Cutover结果

普通submit、显式Skill、显式MCP、Skill missing-input answer、MCP approval/MRTR、remote completion、cancel和startup recovery均只进入或恢复`AgentLoopOrchestrator`管理的同一`AgentRun`。`ApiRuntime`只装配Agent repositories/model port、`CapabilityInvocationService`、continuation/recovery和公开投影；不存在请求级旧runtime selector、feature flag或fallback factory。

显式Skill直接作为首轮required Tool。显式MCP只把已通过owner/health预检的单一Server安全profile放入catalog，并由system metadata固定`server_id`；模型不能改写Server authority。MCP shadow observation从真实`mcp.dispatch` invocation hook开始，不预构造执行计划。

## 2. Deleted runtime、wiring与config

已删除：

- `src/orchestration/service.py`、`scheduler.py`及旧DAG execution loop；
- workflow providers、router、expander、validator；
- planner contract、repair、node identity和planner payload adapter；
- Runtime/Soft Skill/Main Agent重规划器与CompletionPolicy；
- `src/capabilities/main_agent/executor.py`、旧workflow壳和独立回答finalizer；
- `agent_loop/legacy_dag_adapter.py`；
- `max_replans`、`max_dynamic_nodes`、旧planner LLM/reasoning wiring与旧continuation reader；
- misleading capability `workflow.py` registration文件名，统一改为`registration.py`。

正式生产事件不再生产或消费旧Planner/soft-Skill/replan/main-agent-output事件。前端以`agent.reasoning_delta`和`agent.run.completed|failed|cancelled`作为统一Agent实时/终态合同；`agent.final_output`是唯一持久最终输出authority。

## 3. Replacement tests

删除的纯DAG shape、重规划次数和独立finalizer测试由以下行为证据替代：

- `tests/e2e/test_agent_loop_cutover.py`：普通、显式Skill、continuation、cancel、startup recovery全入口；
- `tests/e2e/test_mcp_server_explicit_agent_loop.py`：owner-pinned显式MCP、真实dispatch invocation与唯一Agent final；
- `tests/orchestration/test_agent_loop.py`、`test_agent_invocation.py`、`test_agent_final_output.py`：multi-call、普通Tool失败回模、唯一Invocation lifecycle、真实MCP hook和原子final；
- `tests/lifecycle/test_agent_run_recovery.py`：waiting/recovery/no-replay；
- API 任务、Interrupt、Skill、MCP、SSE/history/graph回归；
- Frontend event transport/reducer/App replay与终态回归；
- SQLite/PostgreSQL/Runtime Sidecar Agent repository与Rust contract回归。

## 4. Zero runtime reference evidence

以下扫描覆盖`src/`、非测试`frontend/src/`和`native/`生产源码；结果为零：

```bash
rg -n "WorkflowPlan|WorkflowNodePlan|OrchestrationRequest|WorkflowProvider|WorkflowRouter|WorkflowExpander|WorkflowPlanValidator|CompletionPolicy|RuntimeReplanner|SoftSkillReplanner|main_agent\.respond|max_replans|max_dynamic_nodes|planner\.reasoning_delta|soft_skill\.reasoning_delta|main_agent\.output_(delta|final)|mcp_remote_task_continuation_plan|legacy_dag" src frontend/src native --glob '!frontend/src/**/*.test.*' --glob '!native/**/tests/**'
```

旧配置不能复活已删除控制面；`CapabilityRegistry.list_for_visibility`只向Outer Agent暴露public Skill与安全条件满足时的单一`mcp.dispatch`。

## 5. Phase 7 remaining physical inventory

下列合同只为Phase 6整体rollback保留，当前生产执行/恢复不读取其调度语义：

- TaskEdge表、SQLite/PostgreSQL repository方法、Runtime Sidecar RPC/proto字段；
- `Task.root_node_id`；
- `TaskNode.criticality`、`dependency_type`、`retry_policy`、`timeout_policy`、`resource_class`；
- Planner replan claim表/model/repository/RPC；
- remote continuation物理列`continuation_plan`中当前仅保存Agent continuation locator的兼容映射；
- `/graph` DTO固定字段，返回Agent活动ledger且`edges=[]`。

这些项目由P7-A备份/恢复、P7-B destructive migration和P7-C最终证明删除；不得在P6提前删除。

## 6. Verification summary

- Backend：compileall通过；Core 46、Storage 403（本机7项外部环境skip）、Lifecycle 37、Integrations 705（本机2项平台skip）、Agent Skills 209、Orchestration 102、Main Agent 16、MCP dispatch 14、MCP Tool 15、API 436、E2E 7、Observability 39、Deployment 3通过。
- Frontend：21 files / 307 tests、typecheck、production build通过。
- Rust：统一脚本`cargo_fmt`、`cargo_clippy -D warnings`、workspace all-features tests通过。
- P6-A冻结证据继续提供真实PostgreSQL、Linux Integrations零skip、clean archive和只读Skill bundle候选验证；本报告不把本机skip改写成通过。
- Clean archive rehearsal：在仓库外`/private/tmp/unified-agent-p6-rehearsal.VCNyP9`分别展开P6-A commit与post-cutover `c3ceb32`/tree `756edc96ce305eaece45abb2f61a015196363ca7`；两侧compileall通过，pre-cutover assembly 4项和post-cutover全入口E2E 5项通过。演练未移动分支、未连接原数据库、未读取或打包本地忽略文件。

## 7. Rollback

Phase 7前若需回滚，停止接收新Task，使用P6-A两份clean archive和pre-Phase7 schema在隔离数据库副本演练，然后以正常`git revert`成对回退整个P6 cutover bundle与外部Skill提交。不得移动分支指针、恢复半切换binary、迁移新Agent Task到旧控制面，或触碰受保护的本地部署文件。

结论：AL-P6-01～10的代码、替代测试、zero runtime reference、文档authority和rollback边界已闭合；Phase 6状态为`cutover_complete`，下一步只能进入P7-A备份与restore-all演练。
