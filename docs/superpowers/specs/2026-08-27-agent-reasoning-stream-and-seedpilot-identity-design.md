# Agent Reasoning 实时展示与 SeedPilot 身份恢复设计

状态：`implemented`；用户已批准；document-perfectization 2 cycles；100/100 Pass
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

### 3.3 用户、参与者与受影响系统

- 直接用户：在当前 Task 开启 thinking、需要查看思考过程的 SeedPilot 用户。
- 产品身份参与者：所有收到统一 Agent 最终回答的用户；无论是否开启 thinking，用户可见身份都必须是
  “育种助手”或“SeedPilot”。
- 受影响系统：OpenAI-compatible Agent stream adapter、Agent model request/runner、transient event broker、Task
  SSE、前端 Task event reducer/ReasoningBox、统一 Agent system prompt 装配及对应测试。
- 不受影响系统：历史消息 API、Conversation Memory authority、持久化 EventRecord、AgentItem schema、Artifact、
  Rust sidecar、数据库和 `prod` 部署。

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
- Adapter 读取到非空、非纯空白 `reasoning_content` 后必须立即按 Provider 顺序 `await` delta sink；不得等到
  sample 完成后再批量发布。
- 同一 chunk 可同时包含 reasoning、正文和 Tool Call；三者必须分别处理，不能互斥。
- reasoning 不加入 `text_parts`、Tool Call buffer 或 `AgentSample.visible_text`。
- delta sink 抛错时停止本次 model sample 的后续 reasoning 发布，最终答案、Tool Call 解析和 sample 协议校验必须
  继续；只记录不含 reasoning、用户内容、Task/Conversation ID 或 Provider 标识的闭合失败计数和阶段。
- 每次 `AgentLoopRunner` 执行最多发布 524,288 UTF-8 bytes reasoning。实现必须为截断提示预留字节，并在达到
  上限时只发布一次固定文案“思考内容过长，已截断”；之后继续消费 Provider stream，但不再发布 reasoning。
- 前端对当前活动 Task 的 reasoning 同样执行 524,288 UTF-8 bytes 防御性上限，超限后只保留一次相同截断提示。

### 5.2 Attempt reset

为保持真正逐段展示且不混入失败 attempt，增加最小控制面：

- `AgentModelRequest` 新增一个可选 `reasoning_reset_sink`，不改变既有 delta sink 的字符串签名。
- 新增 `agent.reasoning_reset` transient frontend event，payload 精确为 `{sample_id}`；不得进入 storage、audit、history
  或 replay。
- 当前 model sample 的 attempt 因 Agent protocol violation、cancellation 或 Provider stream 异常而失败时，如果该
  attempt 已发布过 reasoning，Adapter 必须 best-effort 调用 reset sink；reset sink 失败不得改变原 Task 终态。
- 同一逻辑 model sample 的 retry 复用 sample ID，reasoning delta ordinal 继续单调递增，event ID 不得因 retry
  重复。
- 前端记录当前 sample 首个 delta 到来前的 reasoning 字符串边界。收到匹配 sample ID 的 reset 时，只回退到该
  边界，保留此前已成功 model sample 的 reasoning；重复 reset 必须幂等。
- reset 后新 attempt 只能在当前 Runner 执行的剩余 byte budget 内继续逐段展示。失败 attempt 已发布的字节不返还，
  防止 retry 绕过容量边界；剩余 budget 为零时不再展示新 reasoning，但答案和 Tool Call 必须继续。
- 截断状态属于当前 Runner 执行，不属于某个 sample。若失败 attempt 已触发截断，reset 只清除该 sample 的 Provider
  reasoning，固定截断提示必须保留且仍只出现一次。前端用独立的 `reasoningTruncated` 状态维护该提示，不依赖对
  sample 文本的重复追加。

### 5.3 非流式 fallback

若未来模型 edition 使用已支持的 non-stream Agent fallback，并且 response message 明确包含
`reasoning_content`，Adapter 必须先完成 Tool Call/required-choice 协议校验，再在返回合法 sample 前把有界非空内容
作为一次 reasoning delta 发布。它不得伪装成逐 token 流，也不得改变最终答案或 Tool Call 解析。当前五个目标模型
仍以 stream 路径为主。

### 5.4 不返回 reasoning

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
- Reasoning delta/reset 继续使用已有 Task owner 鉴权；reset 不携带失败原因、Provider response ID 或正文。
- 524,288-byte 上限按发布前 UTF-8 bytes 计量，截断必须停在合法 UTF-8 边界；后端和前端都不得通过反复追加截断
  文案突破上限。

## 9. 功能与非功能要求

| ID | 类型 | 要求 | 验收证据 |
|---|---|---|---|
| FR-01 | Functional | thinking enabled 时 Provider reasoning 按 chunk 顺序立即进入当前 Task SSE | Adapter + Runner + SSE 测试 |
| FR-02 | Functional | 失败 attempt 触发 transient reset，只清除当前 sample 内容 | retry/cancel/stream failure + reducer 测试 |
| FR-03 | Functional | thinking disabled、无 sink 或 Provider 无 reasoning 时不发布 delta/reset | 负向 adapter 测试 |
| FR-04 | Functional | transient sink 失败不影响答案、Tool Call 或 Task 正常终态 | sink fault injection |
| FR-05 | Functional | reasoning/reset 不进入 durable storage、history、memory 或 final answer | storage/history/leak scan |
| FR-06 | Functional | 用户可见身份固定为育种助手 SeedPilot | prompt contract + 真实身份问答 |
| FR-07 | Functional | 同会话历史与跨会话隔离语义不变 | Conversation Memory 回归 |
| NFR-01 | Reliability | delta 顺序、ordinal、event ID、sample reset 在 retry 下确定且幂等 | deterministic retry vectors |
| NFR-02 | Capacity | Runner 与前端各自强制 524,288 UTF-8 bytes 上限和唯一截断提示 | exact boundary tests |
| NFR-03 | Privacy | reasoning 只存在于活动进程/SSE/页面内存，不进入日志正文、audit 或持久层 | audit/storage/log assertions |
| NFR-04 | Compatibility | 正文、Tool Call、usage、finish reason、protocol retry 和历史 API 合同不变 | 既有 adapter/API/frontend 回归 |
| NFR-05 | Accessibility | ReasoningBox 保留现有语义标签、展开/收起按钮和键盘可操作性 | frontend DOM/a11y 回归 |

## 10. 验收与测试

### 10.1 Adapter

- thinking 开启时按序转发多个 reasoning fragment。
- 同一 chunk 的 reasoning、answer、Tool Call 全部保留。
- thinking 关闭或 sink 缺失时不发布 reasoning。
- protocol retry、cancellation、incomplete stream 与 transport failure 在已发布 reasoning 后触发一次 reset；未发布时不
  发送无意义 reset。
- delta/reset sink fault injection 证明最终合法 sample 仍完成，安全诊断不含正文或身份。
- 524,287、524,288、524,289-byte 边界及多 attempt 累计上限精确闭合。
- 失败 attempt 在边界前后触发 reset 时，byte budget 不返还、截断提示保留一次、retry 无剩余额度但最终答案仍完成。
- non-stream response 的 reasoning 在合法 sample 完成后单次发布且不进入 visible text。

### 10.2 Agent Loop、SSE 与持久化

- `agent.reasoning_delta` 带正确 task、sample、ordinal，并只对 frontend 可见。
- `agent.reasoning_reset` 只有 `sample_id`，只走 transient broker；durable projector 必须拒绝 delta/reset。
- ReasoningBox 实时累积 delta，Task 完成后标记 complete。
- Message、AgentItem、Conversation Memory、最终回答和历史 API 均不含 reasoning。
- 历史加载不恢复 ReasoningBox 内容。

### 10.3 身份与上下文

- 实际 Agent system prompt 包含 SeedPilot 身份合同及 Tool 安全规则。
- 生产路径不再包含“你是统一同模型Agent”。
- 同一 conversation 历史继续进入 memory；新 conversation 仍从零开始。

### 10.4 本地真实验收

使用新的本地会话和 thinking-enabled DeepSeek Task：

1. 页面在答案完成前或同时显示非空 reasoning。
2. 最终答案正常完成，Tool Calling 行为不回归。
3. 数据库和历史 API 只包含最终答案，不包含 reasoning。
4. 询问身份时回答“育种助手”或“SeedPilot”。
5. 不部署 `prod`，不复活或重放旧终态 Task。

## 11. 依赖与精确修改面

不新增第三方依赖。预期修改面：

- `src/orchestration/agent_loop/models.py`：可选 reset sink contract。
- `src/integrations/openai_agent_model_adapter.py`：reasoning 解析、即时 delta、attempt reset、sink fail-open。
- `src/orchestration/agent_loop/runner.py`：sample 边界、单调 ordinal、Runner reasoning byte budget。
- `src/api/runtime.py`：reset transient publisher、SeedPilot stable rules 复用。
- `src/api/agent_projection.py`、`src/api/sse.py`：reset closed payload 与 durable rejection。
- `frontend/src/domain/taskEvents.ts`：sample 起点、reset 幂等、UTF-8 byte cap。
- `frontend/src/App.tsx`：仅在现有 ReasoningBox 需要截断状态或无障碍断言时做最小调整。
- 对应 `tests/integrations/test_agent_model_adapter.py`、Agent Loop/API/SSE/Conversation Memory 测试及
  `frontend/src/App.test.tsx`、`frontend/src/api/taskEvents.test.ts`、`frontend/src/domain/taskEvents.test.ts`。

## 12. 本地发布与验证

1. 先运行 adapter、Agent Loop、API/SSE、Conversation Memory 和前端定向测试，再运行受影响全域回归、Ruff、
   typecheck 与 build。
2. 使用当前本地敏感配置重建 backend/frontend 镜像；不得输出、暂存或提交 `config.yaml`、master-key 路径或
   `docker_cmd.md`。
3. 只重建 backend/frontend，保留 runtime sidecar 和数据卷；若 Skill host bind 不可用，只能显式记录本地 Skill
   能力缺口，不得把临时空 Skill 卷当成完整能力验收。
4. 健康检查必须覆盖 backend `/api-doc`、frontend `/seedpilot/` 和受认证模型配置 API。
5. 浏览器必须强制刷新或重新打开页面，确认加载新 hashed asset，避免旧 JS 与新 API schema 混用。
6. 使用新的本地 conversation/Task 完成 thinking、retry/reset、截断、历史无 reasoning 和 SeedPilot 身份 smoke；
   不重放旧终态 Task。
7. `prod` 与外部部署不在本轮发布范围。

## 13. 风险与缓解

| 风险 | 缓解 |
|---|---|
| retry 混入失败 reasoning | sample 起点 + transient reset；retry deterministic tests |
| reasoning 造成内存/SSE/DOM 压力 | Runner/Frontend 双 524,288-byte 上限，唯一截断提示 |
| transient 展示故障拖垮答案 | delta/reset sink fail-open，正文和 Tool Call 继续 |
| reasoning 泄漏进历史/audit | durable projector rejection、storage/history/log leak scan |
| 身份合同再次漂移 | stable rules 只复用 `MAIN_AGENT_SYSTEM_CONTRACT_LINES`，生产字符串零重复 |
| 浏览器继续运行旧 bundle | hashed asset 检查与强制刷新门禁 |

不存在未决产品问题；已确认真正逐段展示、失败 attempt reset、reasoning 不持久化、SeedPilot 唯一用户身份和
conversation 隔离边界。

## 14. 回滚

- Adapter reasoning 解析可独立回滚；回滚后只失去实时 reasoning 展示，不影响最终答案和历史数据。
- SeedPilot stable rules 可独立回滚，但会恢复已确认的产品身份回归，因此只作为紧急代码回退手段。
- 本功能没有持久化迁移，回滚无需清理数据库。
