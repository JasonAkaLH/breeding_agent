# MCP 连续 Tool 审批事件流恢复设计

## 状态

- 日期：2026-08-18
- 决策：自审批通过
- 范围：前端 MCP Tool 审批提交后的当前 Task SSE 恢复

## 问题

前端收到 `node.waiting_for_input` 后加载 durable Interrupt，并主动关闭当前 Task
的 SSE。普通 Interrupt 回答成功后会重新调用 `subscribeToTask`，但
`handleMCPApprovalDecision` 只提交回答并把本地 approval 标记为已处理，没有重新订阅。

当一次 MCP 任务按顺序调用多个 Tool 时，第一次审批可以被后端接受并恢复执行，随后
产生的第二个 `mcp.tool_approval_required` 和 `node.waiting_for_input` 已经持久化，但前端
既收不到 SSE，也不会重新请求 `/interrupts`，用户看到的结果是任务一直 running。

## 约束

- 保持逐 Tool 授权；`always_allow` 仍只适用于当前 Tool 和当前安全版本。
- 不改变后端 Interrupt、Grant、intent/outbox 或 MCP 调用状态机。
- 不保持 waiting 状态下的长连接，不新增轮询器。
- 继续以 durable Task event replay 和现有 event ID 去重为恢复 authority。
- 审批失败时保持当前审批可重试，不提前清理本地状态。

## 方案比较

### A. 审批成功后重新订阅（采用）

审批提交成功后，以响应中的 Task ID（缺失时使用当前 Task ID）恢复当前 Task 引用，清理
旧 `pendingInterrupt` 和 assistant interrupt prompt，并调用现有 `subscribeToTask`。

优点是与普通 Interrupt 回答路径一致，改动集中，且 SSE replay 可以补回订阅建立前已经
产生的事件。缺点是会重放旧事件，但现有 reducer 和 waiting-event ID 集合已经负责去重。

### B. waiting 时保持 SSE

不在 `loadPendingInterruptFromWaitingEvent` 关闭连接。该方案会改变所有 Skill、文件选择、
MRTR 与 MCP Interrupt 的连接和重复消费语义，超出当前缺陷范围。

### C. 审批后轮询

审批成功后周期性查询 Task、Graph 和 Interrupt。该方案建立第二套恢复 authority，增加
延迟、请求量和竞态处理，不采用。

## 数据流

1. 审批点击时固定当前 conversation、Task、assistant 和 restore generation。
2. 调用现有 `submitMessage`，提交 `interrupt_id` 与 `mcp_tool_approval`。
3. 只有请求成功且 generation/conversation 仍匹配时才推进本地 UI。
4. 使用响应 Task ID 或原 Task ID更新当前 Task 与 presentation mode。
5. 清除旧 `pendingInterrupt`、assistant `interruptPrompt`，将旧 approval 标为非 pending。
6. 调用 `subscribeToTask`。
7. durable replay 交给现有 reducer；若后端已经产生下一次审批，新
   `mcp.tool_approval_required` 打开 Dialog，新 `node.waiting_for_input` 重新加载对应
   Interrupt 并按既有规则关闭 SSE。

拒绝审批也走相同重订阅流程，以便接收 Node/Task 的停止或失败终态。

## 错误与竞态

- `submitMessage` 失败：保留原 pending approval，显示现有错误提示，不重新订阅。
- 用户在请求期间切换 conversation 或触发新的 restore generation：忽略过期响应，不覆盖
  新会话状态。
- Task ID 变化：以后端响应 Task ID 为准，并同步 assistant task binding。
- 下一次审批早于重订阅产生：依赖 durable SSE replay 补回，不额外轮询。
- 缺少当前 Task/assistant 引用：不应出现在可见 approval Dialog；handler fail closed，不
  构造新的 Task 或 assistant。

## 验收

- 单次审批成功后建立新的 Task event subscription。
- 第一 Tool 选择 `always_allow` 后，第二个不同 Tool 的审批能够自动显示，无需刷新页面。
- 第二次 `/interrupts` 加载使用新的 interrupt ID。
- 审批请求携带原 interrupt ID 和原 decision。
- stale pending Interrupt 与 assistant prompt 被清除。
- submit 失败时不关闭审批 Dialog、不创建新订阅。
- frontend 定向测试、完整测试、typecheck 和 production build 通过。

## 非目标

- Server-wide 授权。
- 自动批准同一 Server 的其他 Tool。
- 修改后端 Tool admission 或恢复信封。
- 改写所有 Interrupt 的 SSE 生命周期。
