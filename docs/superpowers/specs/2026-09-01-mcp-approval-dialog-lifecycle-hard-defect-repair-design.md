# MCP 工具授权框生命周期硬伤最小修复设计

- 日期：2026-09-01
- 状态：`approved`
- 目标分支：`main`
- 目标环境：main 开发环境；不涉及 `prod`

## 1. 背景与现场证据

开发环境使用 backend-dev `0.1.28`、frontend-dev `0.1.27` 时，用户级 OCR MCP
调用出现 `MCP 工具授权` 弹框：用户点击允许后按钮长时间转圈，Task 已完成后弹框仍不消失。

`biobin_dev` 只读现场检查确认同一 Task 的实际状态为：

- `mcp.tool_approval_required` 于 14:59:58 发布；
- Interrupt 于 15:00:00 进入 `answered`，唯一答案 `accepted=true`；
- pending action 随后进入 `consumed`；
- `start_parse_job` 于 15:00:01 开始、15:00:49 完成；
- Agent final output 于 15:01:09 发布，Task 与 AgentRun 均为 `completed`；
- `task.interrupt_continuation_completed` 于 15:01:09 发布；
- 无数据库锁、未完成事务或 `execution_crash`。

因此授权、MCP调用和Agent continuation均成功，故障位于授权请求与前端状态生命周期。

历史提交 `bb1cc550 fix(frontend): resume stream after MCP approval` 已包含在
frontend-dev `0.1.27` 的构建基线 `56187f85` 中。该修复假定授权 POST 很快返回，再重新订阅
Task SSE。后续 `fa4d19bb refactor(agent): switch all execution entries to agent loop` 将
Interrupt continuation切换为同步等待 `record_agent_continuation()`；当前授权 POST 因而要等完整
Agent continuation结束。本次现场等待约69秒，破坏了原前端修复的时序前提。

同时，当前成功链没有持久化 `mcp.tool_approval_decided`。等待态会主动关闭SSE；前端只有在长POST
返回后才重订阅。历史重放只有 `mcp.tool_approval_required`，而 `task.completed`与
`agent.run.completed` reducer不会清理 `mcp.approval`，所以已完成Task仍可恢复出永久pending弹框。

## 2. 目标与成功标准

本次只修复两个用户可见硬伤：

1. 用户点击MCP授权后，不再等待完整Tool/Agent执行才退出授权弹框；
2. Task已进入任一终态时，刷新、重连或历史重放不再恢复旧授权弹框。

成功标准：

- 授权状态首次成功转换后，立即产生durable frontend `mcp.tool_approval_decided`事件；
- authority转换成功但首次Event append失败时，exact answer replay必须补齐同一个决定事件且不得生成重复账本记录；
- 同一approval Interrupt的前端重试必须复用同一`client_message_id`，确保真实UI请求能够命中exact answer；
- 授权POST仍在执行时，前端已经重新订阅原Task SSE并能收到决定与后续执行事件；
- POST返回同一Task ID时，不在已经恢复的SSE之外再建立第三条冗余订阅；
- 旧POST返回不得覆盖较新的Interrupt、授权决定或Task终态；
- 授权提交在状态转换前失败时，弹框保持pending并允许重试；
- Event fold、HTTP状态对账和结果加载等任一终态收敛路径结束后，`mcp.approval`均为空；
- Grant、Tool参数、MCP调用、Agent lease/recovery/no-replay语义保持不变。

## 3. 方案决策

采用“首次决定事件 + 提交时立即重订阅 + 终态清理”三处最小组合修复。

不采用：

- **只在点击时乐观隐藏弹框**：授权失败、刷新和事件重放仍可能恢复错误状态；
- **把Agent continuation改为后台Task**：会扩张到lease、进程崩溃、exact replay、unknown side
  effect与no-replay边界，超出本次目标；
- **新增专用approval endpoint、数据库字段或状态表**：现有Interrupt提交、Event和Task终态足以闭环；
- **修改MCP Coordinator、协议或远端OCR服务**：现场证明它们已正确完成业务调用。

允许授权HTTP请求继续同步等待现有Agent continuation。前端不再依赖该请求返回来恢复事件流，
从而解决当前用户体验，同时不改变恢复authority。

## 4. 最小生产改动

### 4.1 后端：首次授权决定立即发布事件

修改 `src/api/runtime.py` 的MCP approval分支。

`accept_mcp_tool_approval()` 首次返回 `accepted` 或 `denied_finalized` 后、进入
`_resume_agent_interrupt()`或deny终态返回前，构造并写入：

```text
mcp.tool_approval_decided
```

payload只包含：

- `interrupt_id`；
- 由既有audit reference signer、approval fingerprint和现有
  `mcp-approval-call-reference-v1` context生成的`safe_call_ref`；
- `decision`。

前端已有required事件中的Server/Tool显示名称；decided reducer允许复用previous值，因此不新增
Server查询，也不把Tool参数、Endpoint、凭据或原始fingerprint写入Event。

Runtime决定事件必须使用由版本化event kind、`interrupt_id`和accepted answer ID派生的确定性
Event ID，并把accepted answer持久化的`created_at`作为稳定Event时间，再通过现有
`append_event_exact()`写入。Event ID、payload和`created_at`在首次请求与exact replay中必须完全一致，
避免exact append发生idempotency conflict。首次写入才发布给Event broker；exact answer replay必须再次
执行同一个exact append：记录已存在时不产生第二条账本记录，authority已成功但首次Event append失败时
则补齐并发布缺失记录。该补写只恢复UI投影，不再次创建Answer、Grant或调用Tool。

既有Coordinator可生成同名事件的路径保持不变。本次只补“approval已由持久化authority消费，
Coordinator随后直接命中Grant/approved action而没有返回decided Event”的缺口。若Coordinator在其他
既有路径稍后发布语义相同但Event ID不同的decided事件，前端fold仍幂等地保持同一decision；本次不为
消除这种无害重复扩大到Coordinator修改。

### 4.2 前端：授权提交开始时立即重订阅

修改 `frontend/src/App.tsx` 的 `handleMCPApprovalDecision()`。

固定顺序：

1. 读取当前approval、conversation、Task、assistant与generation快照；
2. 从版本化固定前缀和`interruptId`生成稳定的approval `clientMessageId`；同一Interrupt的同一决定重试
   必须复用该ID，不同Interrupt必须得到不同ID；
3. 设置现有`mcpApprovalSubmitting=true`；
4. 在调用`api.submitMessage()`前，对原Task调用现有`subscribeToTask()`；
5. 等待现有POST完成；
6. response返回后先检查generation、conversation、当前Task、当前phase和当前approval的
   `interruptId`；
7. 当前Task已被清除/替换、phase已进入`loading_artifacts`或取消/终态、或者当前waiting authority已
   不是步骤1捕获的同一pending approval时，旧response不得修改Task phase、approval、pending
   interrupt、assistant或订阅；后一种情况同时包含另一MCP approval和普通Interrupt；
8. response仍指向原Task时保留步骤4的订阅，不在POST返回后重复创建；只有Task ID实际变化且步骤1的
   提交快照仍是当前authority时才切换订阅；
9. 只有当前approval仍是步骤1捕获的同一pending Interrupt时，才允许用成功response作为decided
   Event尚未到达时的本地兜底，把该approval设为非pending；
10. `finally`继续清除submitting状态。

等待事件历史已由`seenEventIds`去重，重订阅回放原`approval_required`不会生成第二次业务授权。
durable `mcp.tool_approval_decided`到达后由现有reducer把approval置为非pending并关闭弹框，后续Tool和
Agent事件继续沿同一SSE展示。

若POST在authority转换前失败，不会产生decided事件；现有pending状态与错误提示保持，用户可以重试。
generation与conversation guard保持原样，切换对话后不得回写旧页面状态。

稳定`clientMessageId`不能包含decision。authority已经接受某个decision后，用户用同一Interrupt改交另一
decision必须继续触发现有message identity conflict，不能覆盖已经接受的Answer；重复提交原decision才是
合法exact replay。该规则只修复已有重试身份，不新增本地授权状态或API字段。

上述response guard同时覆盖两个直接竞争：continuation先产生下一次Tool/普通Interrupt，以及SSE先
产生Task终态并完成结果加载。旧POST在这两种情况下都只能结束自己的loading状态，不得清除新弹框或
把已完成Task重新设为running。

### 4.3 前端：终态清除陈旧approval

修改 `frontend/src/domain/taskEvents.ts`，并同步收紧已在本次范围内的`frontend/src/App.tsx`终态收敛
入口。

以下终态Event fold必须把 `mcp.approval`设为`null`：

- `task.completed`、`task.failed`、`task.cancelled`；
- `agent.run.completed`、`agent.run.failed`、`agent.run.cancelled`。

同一规则还必须用于不依赖终态Event的状态收敛入口：`markTaskCompleted()`、`markTaskFailed()`、
App内取消收敛，以及`reconcileTerminalTaskStatus()`的completed、failed、cancelled和既有MCP
terminal projection分支。终态是“不会再等待该授权”的更强authority。该兜底同时修复本次已完成但
缺少decided事件的历史Task；不修改数据库，也不回填旧Event。

## 5. 数据流

```text
frontend approval click
  -> subscribe existing Task SSE immediately
  -> POST interrupt answer
       -> accept approval atomically
       -> exact-persist/publish mcp.tool_approval_decided
       -> resume existing AgentRun synchronously
  -> SSE decided event closes dialog
  -> SSE Tool/Agent events continue
  -> POST eventually returns
       -> newer Interrupt or terminal state: no projection mutation
       -> same pending approval: response may provide local decided fallback
       -> same Task keeps current subscription
```

恢复路径：

```text
event replay
  -> approval_required
  -> decided event: clear pending approval
  -> or any Task/Agent terminal event: force approval=null
```

## 6. 错误与并发边界

- 决定事件只在storage authority成功转换后发布，不做先写UI事件再提交数据库；
- authority与Event之间发生部分失败时，exact answer replay只补写确定性Event，不重放业务副作用；
- POST失败且没有决定事件时不自动授权、不关闭可重试状态；
- 重订阅只读取同一Task的durable Event，不调用MCP、不重放Tool；
- 同一Task response不重复订阅，避免双SSE和重复事件处理；
- 旧response不覆盖较新的Interrupt、`loading_artifacts`或取消/终态状态；
- Event重复投递继续由`seenEventIds`去重；
- deny保持现有终态，不进入Agent resume；
- `always_allow`继续使用既有per-Tool Grant scope，不扩大Server级权限；
- Task终态清理只影响页面投影，不删除Interrupt、Answer、Grant或审计记录。

## 7. 测试与验收

### 7.1 后端聚焦测试

- 使用可控的阻塞Agent resume：断言resume尚未结束时，Interrupt已answered且
  `mcp.tool_approval_decided`已持久化；
- `allow_once`、`always_allow`与`deny`的decision正确；
- 注入authority成功后的首次Event append失败；exact answer replay补齐同一确定性Event，且不重复
  Answer、决定事件、Grant或Tool调用；
- 上述部分失败必须通过真实`submit_chat_message`入口以同一稳定`client_message_id`重试，不能只直接调用
  `answer_interrupt`模拟相同source message；
- 首次请求与exact replay构造的Event ID、payload和accepted-answer `created_at`完全一致，不触发
  idempotency conflict；
- decided与required Event使用相同`mcp-approval-call-reference-v1` context生成`safe_call_ref`；
- Event payload不含arguments、Endpoint、credential、authorization或原始fingerprint。

### 7.2 前端聚焦测试

- 将第二次`submitMessage`设为未完成Promise；点击授权后立即出现第二条Task订阅；
- 同一Interrupt失败重试的`clientMessageId`完全相同，不同Interrupt的ID不同；改变decision不能生成新
  消息身份覆盖既有authority；
- POST未返回时注入`mcp.tool_approval_decided`，授权框关闭并能继续消费Tool事件；
- response为同一Task时订阅数不再增加；
- POST未返回时注入下一次approval/Interrupt，再返回旧POST；新授权必须继续pending，且旧response不得
  清除新pending interrupt或修改waiting phase；
- POST未返回时注入Task/Agent终态并完成结果收敛，再返回旧POST；Task不得恢复为running、不得重建
  current Task或SSE；
- submission失败时授权框仍pending且按钮恢复可用；
- 六类Task/Agent终态Event fold后均为`approval=null`；
- Event stream失败且只靠`getTask()`对账为completed、failed或cancelled时均为`approval=null`；既有MCP
  terminal projection分支同样不得保留approval；
- restore/replay已完成Task时不显示授权框。

### 7.3 回归门禁

- 相关API/MCP approval/Agent continuation与recovery测试；
- `frontend/src/App.test.tsx`、`frontend/src/domain/taskEvents.test.ts`、
  `frontend/src/components/MCPApprovalDialog.test.tsx`；
- Frontend typecheck、build、Backend compileall、Ruff与`git diff --check`。

### 7.4 开发环境smoke

使用全新conversation调用需要审批的OCR Tool：

1. 点击`仅允许本次`或`始终允许`；
2. 决定提交后授权框及时关闭，不等待OCR执行结束；
3. 页面继续显示Tool运行和最终回答；
4. Task完成后刷新页面，授权框不再出现；
5. 数据库确认Interrupt answered、pending action consumed、唯一Grant语义和Task终态正确。

## 8. 发布与回滚

该修复同时修改backend与frontend，开发环境必须成对构建、验证和部署下一版本；Runtime Sidecar不需
重建。具体tag、镜像digest和部署步骤由后续实施计划决定。镜像构建前，实施commit必须以非强制方式推送
到现有两个源码远端并验证两个`main`都指向同一commit；任一远端失败则停止发布。候选backend还必须同时
断言`current_database()='biobin_dev'`和`current_user='biobin_user'`。`prod`不在范围。

回滚只恢复前一对backend/frontend镜像或回退对应代码提交。没有schema或数据迁移，不删除本次已
产生的合法Interrupt、Answer、MCP结果或Grant。

## 9. 明确不在范围

- 异步化或重构Agent continuation、lease、recovery或invocation；
- 新增数据库schema、migration、API DTO、Endpoint或配置项；
- 修改MCP协议协商、Selector、Gateway、Tool参数、OCR workflow或远端Server；
- 修改历史Task、Interrupt、Event或Grant数据；
- 通用Modal状态框架、SSE框架重写或无关Frontend清理；
- Runtime Sidecar、Rust、Skill、其他Docker容器或`prod`。

License Requirement：复用现有FastAPI、React、Task SSE、durable Event、Interrupt与unittest/
Vitest能力，无新增依赖或许可变化。
