# Agent Reasoning 实时展示与 SeedPilot 身份恢复设计

状态：用户已批准设计；awaiting written spec review
日期：2026-08-27
分支：`main`

## 1. 背景与事实

2026-08-27 本地最新真实 Task 已确认请求使用 DeepSeek V4 Pro、`thinking_enabled=true`、
`reasoning_effort=high`，但 Task 没有产生 `agent.reasoning_delta`。统一 Agent 当前通过
`OpenAIAgentModelAdapter` 解析 Provider stream；该适配器只读取 `delta.content` 与
`delta.tool_calls`，没有读取 `delta.reasoning_content`，也没有调用
`AgentModelRequest.reasoning_delta_sink`。独立文本生成路径虽然已支持 reasoning stream，但不承担完整 Agent
Tool Call 协议，不能替代统一 Agent adapter。

同一次排查还确认，2026-08-23 的 `fa4d19b` 在切换统一 Agent Loop 时，把实际运行的
`AgentContextRules.stable_rules` 写成“你是统一同模型Agent”。已有
`MAIN_AGENT_SYSTEM_CONTRACT_LINES` 仍声明“育种助手（SeedPilot）”，但统一 Agent 路径不再使用它，导致内部
架构名泄漏为用户可见身份。

会话上下文没有发现同会话丢失：连续三轮 Task 分别纳入 0、2、4 条历史消息且未截断。最新一条提问属于新
conversation，因此按现有隔离规则不继承前一个 conversation。该边界保持不变。

## 2. 已确认决策

1. SeedPilot 是唯一用户可见身份；“统一 Agent”只作为内部架构名称。
2. thinking 开启时，Provider reasoning 通过当前 Task SSE 实时展示在前端。
3. reasoning 不进入历史消息、AgentItem、会话记忆、artifact、最终回答或审计正文。
4. 页面刷新或历史恢复后不重放 reasoning，只保留最终答案。
5. 同一会话继续使用既有历史；新会话不跨 conversation 继承上下文。

## 3. 目标与非目标

### 3.1 目标

- 完成 `reasoning_content → reasoning_delta_sink → agent.reasoning_delta → SSE → ReasoningBox` 链路。
- 保持正文、Tool Call、usage、finish reason 与协议重试语义不变。
- 恢复 SeedPilot 身份与既有行为合同，同时保留统一 Agent Tool 安全规则。
- 用自动化和本地真实 Task 证明实时展示、无持久化、身份与上下文边界。

### 3.2 非目标

- 不持久化或历史重放 reasoning。
- 不把 reasoning 混入最终答案、Tool 参数或会话摘要。
- 不重写 Agent Loop、Tool Calling、reasoning effort 配置或前端 ReasoningBox 设计。
- 不增加跨 conversation 记忆。
- 不修改数据库 schema、Rust sidecar、部署合同或 `prod`。

## 4. 方案选择

采用最小适配层修复：扩展现有 `OpenAIAgentModelAdapter`，不改用独立
`generate_text_with_thinking` 路径，也不新增 reasoning 持久化模型。

该方案复用现有 Agent stream、reasoning sink、SSE 与前端状态机，避免重新实现 Tool Call 增量解析及协议重试。

## 5. Reasoning 数据流

```text
Provider delta.reasoning_content
  -> OpenAIAgentModelAdapter
  -> AgentModelRequest.reasoning_delta_sink
  -> AgentLoopRunner publish_reasoning
  -> agent.reasoning_delta transient event
  -> Task SSE
  -> frontend task-event reducer
  -> ReasoningBox
```

### 5.1 流式 Agent sample

- 只有 `request.binding.thinking_enabled=true` 且 sink 存在时才转发 reasoning。
- 每个 attempt 按 Provider 顺序暂存非空 reasoning fragment；只有该 attempt 已得到完整 finish reason、通过 Tool
  Call/required-choice 协议校验并形成合法 `AgentSample` 后，才在返回 sample 前依次 `await` sink。这样前端仍会在
  最终回答提交前通过 SSE 收到 reasoning，同时失败 attempt 的不完整推理不会混入展示。
- 同一 chunk 可同时包含 reasoning、正文和 Tool Call；三者必须分别处理，不能互斥。
- reasoning 不加入 `text_parts`、Tool Call buffer 或 `AgentSample.visible_text`。
- sink 抛错时当前 sample 失败并沿用既有安全失败路径；不得静默吞错后展示不完整 reasoning。
- 协议重试仍由 adapter 管理；失败 attempt 丢弃自己的暂存 reasoning，只有最终协议合法的 attempt 可以发布。
  前端已有 event ID、sample identity 与 ordinal 去重边界，不把 reasoning 转为 durable model state。

### 5.2 非流式 fallback

若未来模型 edition 使用已支持的 non-stream Agent fallback，并且 response message 明确包含
`reasoning_content`，可在 sample 完成前把非空内容作为一次 reasoning delta 发布。它不伪装成逐 token 流，也不改变
最终答案或 Tool Call 解析。当前五个目标模型仍以 stream 路径为主。

### 5.3 不返回 reasoning

Provider 没有返回 `reasoning_content` 时，最终答案照常完成。前端沿用现有文案“本次模型未返回
reasoning_content”，不把空 reasoning 当成 Task 失败。

thinking 关闭时，即使 Provider 异常返回 reasoning，也不得转发。

## 6. 身份 Prompt

统一 Agent 的 `stable_rules` 复用 `MAIN_AGENT_SYSTEM_CONTRACT_LINES`，以现有 SeedPilot 身份和行为合同作为单一
authority；不得在 `src/api/runtime.py` 再复制第二套产品身份。

Agent Loop 仍独立保留以下内部机制规则：

- 根据当前公开 Tool catalog 选择 Tool，观察结果后继续；不需要 Tool 时直接回答。
- 只能调用当前 catalog 中的 Tool，不伪造结果、凭据、隐藏路径或内部状态。
- 最终回答面向用户，不包含隐藏推理或原始敏感结果。

对“你是谁”“你叫什么”等问题，模型应称自己为“育种助手”或“SeedPilot”，不得把“统一 Agent”“统一同模型
Agent”作为用户可见名称。

## 7. 会话上下文边界

本次不改变 Conversation Memory：

- 同一 conversation 按既有 token budget 纳入历史摘要、最近原文和澄清消息。
- 新 conversation 的历史计数从零开始，不读取其他 conversation 内容。
- reasoning 不作为 memory candidate，也不参与 effective question 补全。

增加回归断言锁定上述边界，防止修复身份或 reasoning 时误改上下文。

## 8. 安全、隐私与错误处理

- reasoning event 仅为当前 Task 的 frontend transient event，不进入 durable EventRecord、Message、AgentItem、artifact
  或日志正文。
- SSE 继续执行现有 owner/task 鉴权、event payload 校验和事件 ID 去重。
- 不记录 Provider request ID、reasoning 原文、密钥、base URL 或 header 到测试证据和变更记录。
- cancellation 或 stream 协议失败时关闭 stream，reasoning 不改变既有 Task 终态规则。
- 身份合同只来自可信 system rules，用户输入不能覆盖产品身份或 Tool authority。

## 9. 验收与测试

### 9.1 Adapter

- thinking 开启时按序转发多个 reasoning fragment。
- 同一 chunk 的 reasoning、answer、Tool Call 全部保留。
- thinking 关闭或 sink 缺失时不发布 reasoning。
- sink 失败、cancellation、retry 与 incomplete stream 沿用既有失败语义。
- non-stream response 的 reasoning 可单次发布且不进入 visible text。

### 9.2 Agent Loop、SSE 与持久化

- `agent.reasoning_delta` 带正确 task、sample、ordinal，并只对 frontend 可见。
- ReasoningBox 实时累积 delta，Task 完成后标记 complete。
- Message、AgentItem、Conversation Memory、最终回答和历史 API 均不含 reasoning。
- 历史加载不恢复 ReasoningBox 内容。

### 9.3 身份与上下文

- 实际 Agent system prompt 包含 SeedPilot 身份合同及 Tool 安全规则。
- 生产路径不再包含“你是统一同模型Agent”。
- 同一 conversation 历史继续进入 memory；新 conversation 仍从零开始。

### 9.4 本地真实验收

使用新的本地会话和 thinking-enabled DeepSeek Task：

1. 页面在答案完成前或同时显示非空 reasoning。
2. 最终答案正常完成，Tool Calling 行为不回归。
3. 数据库和历史 API 只包含最终答案，不包含 reasoning。
4. 询问身份时回答“育种助手”或“SeedPilot”。
5. 不部署 `prod`，不复活或重放旧终态 Task。

## 10. 回滚

- Adapter reasoning 解析可独立回滚；回滚后只失去实时 reasoning 展示，不影响最终答案和历史数据。
- SeedPilot stable rules 可独立回滚，但会恢复已确认的产品身份回归，因此只作为紧急代码回退手段。
- 本功能没有持久化迁移，回滚无需清理数据库。
