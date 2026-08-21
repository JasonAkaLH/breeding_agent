# Phase 4：Waiting、Continuation 与 Recovery PRD

- **日期**：2026-08-22
- **状态**：pending
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **上游**：Phase 0～3必须`proof_complete`
- **主责需求**：FR-8、FR-9、FR-18
- **主责NFR**：恢复与no-replay
- **目标结果**：让零到多个waiting calls、Interrupt、approval、MRTR、remote Task、crash和cancel都恢复或终止原AgentRun/原tool call；仍不切真实API入口。

## 1. 目标与价值

工具链中的补充输入、审批和远端任务可能等待数秒到数天。Runtime必须释放执行资源和Task lease，同时保留足够的
durable authority，在用户或远端结果到达后继续同一模型轨迹。任何恢复都不能构造新WorkflowPlan、新AgentRun或
重发不确定副作用。

## 2. 进入条件

- AgentRun waiting状态、waiting_call_item_ids、next_batch_call_ordinal和lease primitive通过；
- Core Loop能在持久suspension点停止且不提前采样；
- Skill missing-input和MCP approval/MRTR/remote已有authority状态机；
- Interrupt/Cancel/Task生命周期基线已锁定。

### 2.1 当前证据与受影响模块

| 锚点 | 当前事实 | 本阶段影响 |
|---|---|---|
| `src/lifecycle/interrupt_service.py`、`cancellation_service.py` | 已有Interrupt/Cancel与Task线性化合同 | 复用到AgentRun/call identity，不创建新公开状态 |
| `src/api/runtime.py` | 当前负责approval、remote outbox和旧continuation恢复装配 | 提取Agent recovery coordinator，真实route留到Phase 6 |
| `src/integrations/mcp/` aggregate/recovery modules | 已有approval、MRTR、remote durable authority和claim/no-replay | Locator绑定原call并复用authority，不重发Tool side effect |
| `tests/lifecycle/`、MCP recovery/API/e2e tests | 已覆盖duplicate、restart、cancel和late result边界 | 迁移到同Run/multi-waiting/fencing proof |

## 3. 范围与非范围

### 3.1 范围内

- 一个batch内零到多个waiting calls；
- Skill missing input、MCP approval、elicitation/MRTR、remote Task；
- continuation locator和identity/digest校验；
- duplicate answer/wakeup/outbox幂等；
- waiting release/reacquire lease；
- crash recovery、authority补写、unknown side effect aborted；
- cancel/completion/late-result线性化；
- recovery worker与start/resume统一内部入口。

### 3.2 非范围

- 不修改公开API DTO、SSE或Frontend；
- 不把真实用户请求切到Agent Loop；
- 不迁移旧DAG continuation plan；
- 不保证任意Capability exactly-once；
- 不自动重放可能已产生副作用的调用；
- 不增加steer、子Agent或轮次上限。

## 4. Waiting合同

以下结果把AgentRun置为`waiting_for_input`或`waiting_for_dependency`，Task继续投影为`running`：

- Skill missing input；
- MCP Tool approval；
- MCP elicitation/MRTR；
- MCP remote Task；
- 其他已注册`can_suspend=true`的现有Interrupt合同。

Waiting call尚无terminal tool_result。当前wave已启动calls可闭合或各自进入waiting；后续wave不启动。Run持久化全部
waiting call IDs和第一个未启动ordinal。Waiting transition必须先原子提交全部authority，再release lease；waiting期间
不heartbeat、不占model/capability worker。

## 5. Continuation Locator

每个authority至少绑定：

```text
run_id
active_sample_item_id
tool_call_item_id
task_id / node_id / provider call identity
owner / conversation identity
resume kind
safe authority reference
payload/result digest
pinned skill bundle / model binding references
```

Locator不得包含实际MCP Tool参数、附件正文、raw result、credential或未净化用户内容。旧MCP
`continuation_plan`替换为该identity-bound locator；不提供WorkflowPlan reader或adapter。

## 6. 恢复流程

```text
receive answer / approval / remote completion / recovery signal
  -> authenticate owner + task + node + call + digest
  -> acquire same AgentRun lease via CAS
  -> reload waiting set and current authority
  -> append trusted continuation item
  -> continue capability-specific authority
  -> atomically commit the original call outcome
  -> remove only that waiting call
       -> waiting set non-empty: release and remain waiting
       -> empty: resume remaining waves
  -> batch closed: sample same model edition
```

回答一个Interrupt时若仍有其他open waiting，不能采样。现有chat API在多个Interrupt且未指定ID时继续明确报错，不能
按顺序猜测用户回答目标。

## 7. Crash Recovery与no-replay

| 发现状态 | 行为 |
|---|---|
| Capability已有权威terminal result但缺AgentItem | 通过原子outcome API幂等补写唯一tool_result |
| MCP有durable continuation authority | 恢复现有MCP状态机，完成后补写原call result |
| Call可能有副作用但无可证明结果 | 写`aborted` result，不自动重放，由模型决定其他方案 |
| Call明确未开始且无start authority | 可写`aborted`，不得假装业务失败原因 |
| Orphan result或call/result identity不一致 | fatal consistency error |
| Lease过期且旧worker仍返回 | 新owner可接管；旧token提交失败，late result不覆盖 |

Runtime承诺no automatic replay，不承诺任意外部系统exactly-once。

## 8. Cancel合同

- Cancel与completion按Task/Run revision线性化；
- cancel后不启动新call、不恢复remaining wave、不采样、不final；
- 通过现有CancellationService取消in-flight Capability；
- 未闭合call按authority写aborted或保持受控远端cancel状态；
- late result继续discard；
- Run/Task最终`cancelled`，claim清理；
- duplicate cancel幂等。

## 9. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P4-01 | Batch可持久化多个waiting calls。 | 两个同wave waiting及后续exclusive wave fixture。 |
| AL-P4-02 | 回答一个waiting不提前采样。 | Remaining count>0时model calls保持0。 |
| AL-P4-03 | 全部waiting闭合后恢复remaining wave。 | Ordinal/结果顺序与原sample一致。 |
| AL-P4-04 | Skill missing input恢复原Run/call。 | Bundle/model/call identity保持。 |
| AL-P4-05 | MCP approval/MRTR/remote恢复原Run/call。 | Existing authority/no-replay回归通过。 |
| AL-P4-06 | Duplicate answer/wakeup/outbox幂等。 | 一个call最多一个terminal result。 |
| AL-P4-07 | Crash后authority结果可补写。 | Result exists/item missing fault test。 |
| AL-P4-08 | 不确定副作用补aborted且不重放。 | Executor invocation count不增加。 |
| AL-P4-09 | Waiting释放lease，resume重新acquire。 | Fake clock/no-heartbeat/takeover tests。 |
| AL-P4-10 | Cancel/completion/late result线性化。 | 竞态最终只有一个closed terminal。 |

## 10. 失败模式

- owner/conversation/task/node/call不匹配：拒绝，不修改waiting集合；
- locator digest不匹配或authority损坏：fatal，不请求模型猜测；
- continuation到达但Run已terminal：幂等返回terminal，不复活；
- 多Interrupt未指定ID：明确ambiguous错误；
- reacquire失败：保持waiting，由持有者处理；
- capability continuation普通失败：提交failed tool result，batch闭合后模型可纠正；
- storage不可用：不ack外部authority成功，按现有outbox恢复。

## 11. 测试计划

最低域：

```bash
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
```

实现时先跑聚焦Interrupt/MCP recovery/cancel模块；退出时必须覆盖multi-waiting、restart fault、duplicate delivery、lease
takeover和cancel race。仍只通过test-only assembly调用Agent resume入口。

## 12. Git检查点与回滚

- 新locator/Agent recovery为pre-cutover proof，不注册真实route；
- 不删除旧DAG continuation代码；
- 回滚删除Agent-only locator/coordinator，保留Phase 1 durable contract；
- 若必须重放不确定副作用才能完成，阶段转`blocked`，不能弱化no-replay。

## 13. 完成与交接

AL-P4-01～10及所有Skill/MCP/cancel恢复回归通过；所有恢复回到原Run/call；无真实API切换。

交付Phase 5：start/resume统一内部入口、waiting集合、continuation locator和recovery/cancel coordinator。Phase 5不得消费
raw MCP result、旧continuation plan或进程内Future。
