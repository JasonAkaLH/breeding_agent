# Agent Interrupt 恢复 Lease Handle 最小修复设计

状态：`approved`；待实施计划
日期：2026-08-29
目标分支：`main`

## 1. 问题与目标

真实MCP授权Task `task-19f493db9624`在用户选择“始终允许”后，Grant、Interrupt answer、
pending action和`start_parse_job`均成功提交，但授权HTTP请求最终返回500：

```text
AgentStorageConflict: agent_invocation_not_owned
```

该OCR恢复调用运行约44秒。Recovery Coordinator在`capability_wave`期间每10秒续租一次，
续租会轮换claim token并增加AgentRun revision；Interrupt authority resolver调用
`AgentCapabilityInvoker.resume()`时没有传递Coordinator已持有的`AgentLeaseHandle`，导致
Invocation finish仍用调用开始时的旧token/revision校验并被正确拒绝。结果是Tool已执行，
但Agent Tool result仍为reserved、Run仍为waiting。

本设计只修复这一点：Interrupt恢复的Capability调用必须与普通Agent调用一样，在同一个
lease handle的ownership lock内读取最新token/revision并提交。

## 2. 最小方案

1. `AuthorityResolver`从只接收`locator`改为接收`locator + AgentLeaseHandle`。
2. `AgentRunRecoveryCoordinator._close_call()`在现有`run_active_phase("capability_wave")`
   callback中，把同一个handle传给resolver。
3. `ApiRuntime._resume_agent_interrupt()`的resolver把handle传给
   `AgentCapabilityInvoker.resume()`。
4. `AgentCapabilityInvoker.resume()`新增必填`lease_handle`参数，并继续传给现有
   `invoke(..., lease_handle=lease_handle)`。
5. 远端MCP authority projection等不执行Capability的resolver只接收并忽略handle，不改变
   结果或ack语义。

普通`invoke()`已有正确实现：ownership boundary通过
`AgentLeaseHandle.run_ownership_bound()`读取当前handle，再用最新revision/token执行begin和
finish校验。本修复只让resume路径复用它，不新增第二套续租或校验逻辑。

## 3. 安全与错误边界

- 不放宽`agent_invocation_not_owned`、claim token、revision、Task running或cancel校验。
- 不暂停heartbeat、不延长TTL、不允许旧worker用陈旧lease提交。
- Resolver必须使用Recovery Coordinator本次claim的精确handle，不能自行重新acquire或从
  数据库猜测ownership。
- Tool副作用、no-replay、authority digest、outcome CAS和ack顺序保持不变。
- Resolver失败继续沿现有异常路径传播；不增加吞错、重试或fallback。

## 4. 明确不修改

- 不修复、重放、删除或迁移当前卡住的`task-19f493db9624`。
- 不修改MCP协议、Server/Tool选择、Tool Grant语义、pending action、Gateway、Selector、
  Result Parser或Artifact。
- 不修改前端授权弹窗、SSE重订阅、数据库schema、Rust/Sidecar、配置或`prod`。
- 不处理与lease handle缺失无关的历史Task、错误文案或UI体验。

## 5. 测试与验收

聚焦测试必须证明：

1. Recovery Coordinator把当前`AgentLeaseHandle`交给authority resolver。
2. `AgentCapabilityInvoker.resume()`把同一handle传入现有invocation ownership boundary。
3. 恢复Capability执行跨越至少一次heartbeat、token/revision发生轮换后，finish使用最新值并
   成功提交，不再抛`agent_invocation_not_owned`。
4. 旧token/revision仍被拒绝，heartbeat和fencing测试保持通过。
5. 普通调用、remote authority projection、duplicate continuation、deny和no-replay回归不变。

自动验证覆盖受影响的Lifecycle recovery、Agent invocation和MCP approval/API定向套件，随后
运行相关Lifecycle、Orchestration与API回归、compileall、Ruff和`git diff --check`。

本地验收使用新Task：授权后Tool执行时间跨过至少一次heartbeat，HTTP提交成功，Interrupt关闭，
Agent Tool result committed，Run继续或完成。现有卡住Task只作历史证据，不参与验收。

## 6. 实施范围、回滚与完成声明

预期生产修改限定为：

- `src/lifecycle/agent_run_recovery.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/api/runtime.py`

`interrupt_service.py`继续透传同一`AuthorityResolver`类型，无新业务逻辑。测试只修改直接受影响
文件；机械的一参数resolver fixture调整不得改变断言语义。

回滚恢复旧resolver/resume签名及对应测试即可，不涉及数据回滚。只有跨heartbeat恢复回归和
新Task本地验收均通过，且ownership/fencing规则零放宽，才可声明完成。

License Requirement：复用既有Python、Agent lease/recovery/invocation和MCP approval能力；
无新增依赖或许可变化。
