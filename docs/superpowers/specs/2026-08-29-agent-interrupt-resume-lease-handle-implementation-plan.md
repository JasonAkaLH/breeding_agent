# Agent Interrupt 恢复 Lease Handle 最小实施计划

依据：`2026-08-29-agent-interrupt-resume-lease-handle-design.md`
设计提交：`a2dccdd8`
状态：`implemented_automated`；等待用户新Task真实授权smoke
目标分支：`main`

## 1. 完成声明

唯一目标是让Agent Interrupt恢复的Capability调用复用Recovery Coordinator当前
`AgentLeaseHandle`，使跨heartbeat调用在finish阶段使用最新claim token/revision。

完成时不放宽ownership/fencing，不改变heartbeat、MCP授权或执行语义；当前卡住Task不处理，
只用新Task验收。

## 2. Checkpoint A：聚焦红测与resolver合同

先修改直接受影响的测试：

- `tests/lifecycle/test_agent_run_recovery.py`
- `tests/orchestration/test_agent_invocation.py`
- 必要时扩展现有API Interrupt恢复定向测试，不新增独立测试框架。

红测必须证明旧代码存在两个缺口：

1. authority resolver无法接收Recovery Coordinator当前handle；
2. `AgentCapabilityInvoker.resume()`无法接收并转交handle。

测试使用确定性handle轮换，不依赖真实sleep：Capability执行期间把handle从旧token/revision更新为
新值，begin必须看到旧值，finish必须看到新值。现有普通调用长执行测试继续保留。

## 3. Checkpoint B：最小生产修改

只修改：

- `src/lifecycle/agent_run_recovery.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/api/runtime.py`

具体步骤：

1. `AuthorityResolver`改为接收`(locator, lease_handle)`。
2. `_close_call()`的现有`capability_wave` callback把同一handle交给`_resolve()`；
   `_resolve()`再传给resolver。
3. `converge_unknown_side_effect`和remote authority resolver机械接收未使用handle，行为不变。
4. `_resume_agent_interrupt()`的resolver把handle传给
   `_agent_capability_invoker.resume(locator, lease_handle=handle)`。
5. `AgentCapabilityInvoker.resume()`新增必填handle并传给现有`invoke()`。
6. 所有测试resolver fixture机械增加`_handle`参数，不改变原断言和返回值。

禁止增加兼容的一参数resolver fallback、signature introspection、新lease acquire、重试或宽松校验。

Implementation commit：`fix(agent): preserve lease ownership across interrupt resume`

## 4. Checkpoint C：自动回归

先运行精确红绿用例，再执行：

```bash
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest \
  tests.api.test_pending_skill_context \
  tests.api.test_mcp_server_explicit_agent_loop
conda run -n multi_agent python -m compileall -q \
  src/lifecycle src/orchestration/agent_loop src/api tests/lifecycle tests/orchestration
conda run -n multi_agent ruff check \
  src/lifecycle/agent_run_recovery.py \
  src/orchestration/agent_loop/capability_invoker.py \
  src/api/runtime.py \
  tests/lifecycle/test_agent_run_recovery.py \
  tests/orchestration/test_agent_invocation.py
git diff --check
```

最终diff必须证明没有修改Task数据、数据库schema、frontend、MCP Gateway/Selector、配置、Rust或
`docker_cmd.md`。

## 5. Checkpoint D：文档与本地新Task验收

1. 把设计状态更新为`implemented`、计划状态更新为`complete`，同步`docs/AGENTS.md`和
   `CHANGELOG.md`。
2. 按现有开发Compose只重建backend，frontend和Runtime Sidecar不重建。
3. 使用新Task触发同一OCR MCP授权；调用必须跨过至少一次10秒heartbeat。
4. 验收：授权HTTP成功、Grant有效、Interrupt answered、pending action consumed、MCP Call completed、
   Agent Tool result committed，Run继续或完成，日志无`agent_invocation_not_owned`。
5. 当前`task-19f493db9624`保持原状态，不重放、不修复、不删除。

Final commit：`docs(agent): close interrupt resume lease fix`

## 6. 回滚

回退Implementation commit并重建backend；无需回滚数据库、Grant、MCP配置、frontend或远端服务。
当前历史Task仍按原no-replay/人工取消边界处理。

License Requirement：复用既有Python、Agent lease/recovery/invocation和MCP approval能力；
无新增依赖或许可变化。

## 7. 当前完成证据（2026-08-29）

- `503a590f`完成三个生产文件与两个聚焦测试文件的最小修改。
- 两条红测在旧代码上分别以resolver缺handle和`resume()`不接受handle精确失败，修复后转绿。
- Lifecycle 48项、Orchestration 181项、受影响API 66项通过；compileall、Ruff与
  `git diff --check`通过。
- 只重建backend；backend healthy，capabilities API 200，frontend与Runtime Sidecar保持原
  容器。
- 运行镜像中`AgentCapabilityInvoker.resume`已要求`lease_handle`；旧
  `task-19f493db9624`仍为Task running、Run waiting、Tool result reserved，未被修改。
- Checkpoint D的新Task真实授权smoke等待用户执行；此前本计划不标记`complete`。
