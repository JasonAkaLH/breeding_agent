# Write-after-completion Streaming Path 设计

状态：已完成 document-perfectization 审查；可进入实施计划。

## 1. 背景与问题

当前主代理流式输出的热路径是：LLM 每返回一个 `answer` / `reasoning` chunk，后端先把对应 `main_agent.output_delta` / `main_agent.reasoning_delta` 事件写入数据库，再通过内存事件 broker 推给 SSE。切换到远端 PostgreSQL 后，每个 chunk 都承担一次远端数据库事务往返，导致用户看到的流式输出被数据库时延线性拖慢。

本设计目标是恢复接近 LLM 原始流速的前端流式体验，同时保持最终回答、任务状态、错误与取消记录的生产级可追溯性。

### 1.1 当前代码证据

- `src/capabilities/main_agent/executor.py` 当前在每个 reasoning / answer chunk 上构造 `main_agent.reasoning_delta` / `main_agent.output_delta` 并调用 `_record_or_collect`，因此 live recorder 存在时会进入持久化热路径。
- `src/api/runtime.py` 的 `_record_event` 与 runtime assembly 的 `record_live_event` 都按 `storage.append_event -> event_broker.publish` 顺序执行；这意味着事件必须先入库成功，前端才看到 SSE。
- `src/orchestration/service.py` 当前在 capability 返回后先 `save_artifact`，再逐条 `_record_event(result.events)`，随后才更新 node / task terminal 状态；最终回答历史通过 `main_agent.output_final` + text artifact 同步为 assistant message。
- `src/api/routes/tasks.py` 当前 SSE loop 每次拿到事件后都会执行 `require_current_bearer_for_user`；`src/auth/services.py` 的 current-token 校验会 `touch_auth_user_token_last_used`，形成每事件认证写库。
- `src/lifecycle/cancellation_service.py` 当前可以把 task / node / interrupt 等状态改为 cancelled，但 running LLM stream 需要实施时显式感知取消，避免用户停止后继续向前端推 transient deltas。

## 2. 核心决策

采用 **write-after-completion streaming path**：

> LLM 流式 chunk 到达后立即进入内存 buffer 并 publish 到 SSE；数据库只在任务状态边界和最终完成阶段写入。只有完整生成成功的最终 `answer content` 才进入正式会话历史；失败、取消、中断的 partial output 不进入消息历史，只保留 task/event 状态与脱敏诊断。

## 3. 范围

### In scope

- 主代理流式 answer delta 不再逐 chunk 入库，也不写入 audit JSONL 正文。
- 深度思考 reasoning delta 仍只前端实时展示，不持久化。
- 正常完成后一次性持久化最终 answer content、assistant message、task completed 状态。
- LLM API 中断、provider 报错、内部错误时，丢弃 partial output，只持久化失败状态和脱敏诊断。
- 用户中途停止时，丢弃 partial output，只持久化取消状态和取消元信息。
- SSE 认证 revalidation 不能在每个流式事件上触发数据库写入。
- 用户取消必须能阻止后续 transient delta 继续推送；若 LLM provider 无法立即中止，也必须丢弃 late chunks。
- 刷新 / 重新登录后只恢复成功完成的最终 answer，不恢复 partial output。

### Out of scope

- 不持久化 token 级 output delta。
- 不持久化 reasoning 内容。
- 不做 SQLite 历史迁移。
- 不把前端断开 SSE 连接视作用户取消。
- 不引入新的 Agent 框架。
- 不承诺重连后 replay 未持久化的 transient deltas；重连恢复只依赖已持久化 final / terminal 状态。

## 4. 持久化规则

### 4.1 内容持久化边界

本设计区分三类内容：

1. **transient delta**：`main_agent.output_delta`、`main_agent.reasoning_delta`，只用于当前 SSE 实时展示，不进入数据库和 audit 正文。
2. **final answer artifact / assistant message**：只有 LLM stream 正常结束并完成 finalize 后才持久化；当前项目可继续用 text artifact + `main_agent.output_final` 触发 assistant history sync，但 artifact 必须只在成功完成后创建。
3. **terminal / diagnostic event**：`task.completed`、`task.failed`、`task.cancelled`、`node.*`、脱敏诊断事件必须持久化，用于刷新、重新登录、审计与问题追踪。


| 场景 | answer content 是否入库 | 必须持久化的状态 / 事件 | partial output |
| --- | --- | --- | --- |
| 正常完成 | 是 | text artifact 或等价 final answer record、`main_agent.output_final`、assistant message、`task.completed` | 内存中聚合后作为最终 answer 保存 |
| LLM API 流中断 | 否 | `task.failed`、错误阶段、错误码、脱敏诊断、`partial_output_discarded=true` | 丢弃 |
| provider 报错 | 否 | `task.failed`、provider 诊断、model metadata、是否可重试 | 丢弃 |
| 后端内部错误 | 否 | `task.failed`、内部阶段、脱敏错误类型 | 丢弃 |
| 用户点击停止 | 否 | `task.cancelled`、`cancel_source=user`、`cancel_requested_at`、`partial_output_discarded=true` | 丢弃 |
| 前端 SSE 断开 / 刷新 | 视任务最终结果而定 | 不直接写失败；后端继续生成 | 连接断开期间前端不再看到实时 partial，完成后历史只显示最终 answer |
| 后端进程崩溃 / 重启恢复悬挂任务 | 否，除非崩溃前已完成 final write | `task.failed` 或 `task.interrupted` 等价状态、恢复诊断 | 丢弃 |

## 5. 运行时状态流

推荐状态语义：

```text
accepted
  -> running
  -> streaming
  -> finalizing
  -> completed

streaming
  -> failed       # LLM/API/网络/内部错误
  -> cancelling
  -> cancelled    # 用户主动停止

running/streaming/finalizing
  -> interrupted/failed_on_recovery  # 进程崩溃后启动恢复发现悬挂任务
```

当前实现已经有 `ACCEPTED` / `PLANNING` / `RUNNING` / `CANCELLING` / `CANCELLED` / `COMPLETED` / `FAILED` 等状态。实施本设计时不要求立即新增数据库枚举；`streaming` / `finalizing` / `interrupted` 可先作为 persisted event payload 的 `stage` / `terminal_reason` 表达，避免为了性能修复引入额外状态迁移风险。

## 6. 数据流设计

### 6.1 提交消息阶段

提交用户消息时仍同步持久化：

- conversation / `current_task_id`
- user message
- task / node 初始状态
- busy guard 所需状态

原因：这些是任务存在性、刷新恢复、并发控制与权限校验的基础，不能延迟到回答结束。

### 6.2 流式生成阶段

LLM 每个 chunk 到达后：

1. 追加到内存 `answer_buffer` 或 `reasoning_buffer` 统计器。
2. 构造 transient frontend event。
3. 直接 `event_broker.publish(event)`。
4. 不调用 `storage.append_event`。
5. 不写 audit JSONL 大正文；如需观测，只记录计数、字节数、阶段耗时。
6. 每轮循环检查 task 是否已取消；若已取消，停止 publish 后续 transient deltas，并进入取消收尾。

内存 buffer 至少保存：

- `answer_chunks: list[str]`
- `received_chunk_count`
- `received_answer_char_count`
- `received_reasoning_char_count`，仅计数，不保存正文
- `started_at`
- `model_edition`
- `thinking_enabled`
- `reasoning_effort`

### 6.3 正常完成阶段

LLM stream 正常结束后进入 `finalizing`：

1. 合并 `answer_chunks` 得到 final answer。
2. 先重新读取 task 状态；若 task 已 `CANCELLING` / `CANCELLED`，不得保存 final answer，必须转取消 / late-result discard 流程。
3. 在一个可靠 finalize 流程中持久化：
   - text artifact 或等价 final answer record
   - `main_agent.output_final` 或等价 final event；payload 不保存正文，只保存长度、scope、role、计数、耗时等元信息
   - assistant message / conversation history
   - node completed
   - task completed
4. publish terminal frontend event。
5. 清理内存 buffer。

最终 answer 入库必须是幂等的：同一个 task 重试 finalize 不应产生重复 assistant message。当前项目已有 `message_id=f"{task_id}:assistant"` 的幂等方向，实施时应保留或强化该约束。

### 6.4 失败阶段

LLM stream 抛错、provider 返回错误、内部处理失败时：

1. 丢弃内存 partial output 正文。
2. 写入 `task.failed` / node failed 状态。
3. 返回 no-artifact / no-output-final 的失败结果，避免 orchestration 层保存 partial text artifact。
4. 写入脱敏错误事件，例如：

```json
{
  "stage": "llm_streaming",
  "error_code": "provider_stream_interrupted",
  "error_type": "timeout",
  "model_edition": "deepseek-v4-flash-260425",
  "thinking_enabled": false,
  "reasoning_effort": "minimal",
  "partial_output_discarded": true,
  "received_chunk_count": 37,
  "received_answer_char_count": 1280,
  "elapsed_ms": 18432,
  "retriable": true
}
```

不得保存 partial answer 正文或 reasoning 正文；也不得把 partial output 放入 `CapabilityExecutionResult.output_payload`、artifact、audit payload 或 exception message。

### 6.5 用户取消阶段

用户点击停止时：

1. 设置 task cancel requested。
2. 运行中的 LLM stream 应尽快被取消；如果底层 provider 不能取消，也必须在本地停止 publish 并丢弃 late chunks。
3. 丢弃内存 partial output。
4. 写入 `task.cancelled` / node cancelled 状态。
5. 如果 stream 之后返回 late result，必须沿用现有 `task.late_result_discarded` 语义或等价诊断，且不得保存 answer artifact。
6. 写入取消事件，例如：

```json
{
  "stage": "llm_streaming",
  "cancel_source": "user",
  "partial_output_discarded": true,
  "received_chunk_count": 12,
  "received_answer_char_count": 420,
  "elapsed_ms": 5210
}
```

取消结果不得生成 assistant message。

### 6.6 SSE 断开阶段

前端刷新、网络波动或 SSE 连接断开不等于用户取消：

- 后端任务继续运行。
- 若任务最终完成，历史 API 返回最终 answer。
- 若任务最终失败 / 取消，历史 API 不返回 partial answer，只能看到任务状态。
- 如果前端重新连接同一 running task，只能接收重新连接之后的 transient events；除非未来实现专门的短期内存 replay，本设计不承诺恢复断开期间的 partial output。
- 如果任务已完成，重连 / 刷新通过消息历史读取 final answer；如果任务失败或取消，只读取 terminal 状态和脱敏诊断。

## 7. SSE 认证 revalidation 规则

### 7.1 认证读写分离

SSE 热路径必须使用“只读当前性校验”。`last_used` 这类使用痕迹写入必须与 event delivery 解耦：

- REST 登录、普通 REST API、token refresh / logout 可以继续写 token 使用状态。
- SSE 建连时可以做一次正常认证。
- SSE 事件循环内的定期 revalidation 只判断 token hash 是否仍是当前 token，不更新 `token_last_used_at`。
- 如果产品仍要求长连接期间刷新 last-used，应由连接级节流任务异步写入，频率不得高于配置窗口，且不得阻塞 event yield。


- token 被刷新 / 登出后，下一次 revalidation 必须关闭旧连接。
- 测试必须证明 100 个 transient events 不会触发 100 次 token touch。

## 8. 错误处理与可追溯性

所有失败、取消、中断状态必须包含：

- `stage`
- `status`
- `partial_output_discarded=true`
- `received_chunk_count`
- `received_answer_char_count`
- `received_reasoning_char_count`，只计数
- `elapsed_ms`
- `model_edition`
- `thinking_enabled`
- `reasoning_effort`
- 脱敏 `error_code` / `error_type` / `retriable`，如适用

禁止包含：

- partial answer 文本
- reasoning 文本
- API key、token、数据库地址、provider base_url、密码
- 原始 exception 中可能含敏感配置的完整字符串

## 9. 幂等与恢复

### 9.1 Replay 语义变化

当前 `iter_frontend_events` 会先 replay 已持久化 frontend events，再订阅 live broker。实施本设计后：

- 历史 replay 不再包含 token 级 `main_agent.output_delta` / `main_agent.reasoning_delta`。
- running task 的新 SSE 连接只保证收到连接后产生的 transient events，以及后续持久化 terminal events。
- completed task 的刷新恢复必须依赖 assistant message / final event / artifact，而不是 replay output deltas。
- failed / cancelled task 的刷新恢复必须依赖 terminal event 与 task summary，不得尝试重建 partial answer。

- final answer persistence 必须以 `task_id` 或 final message id 幂等。
- 如果 finalize 成功但 terminal publish 失败，刷新后仍应看到最终 answer。
- 如果 finalize 前进程崩溃，启动恢复应将悬挂 running task 标记为 failed/interrupted，并保留 `partial_output_discarded=true`。
- 如果失败状态已写入，后续重复失败处理不能覆盖成 completed。
- 如果用户取消与 LLM 正常结束竞态发生，取消优先；finalize 前必须重新读取 task 状态并拒绝保存 late answer。

## 10. 依赖与集成点

实施计划必须显式覆盖以下集成点：

| 集成点 | 当前角色 | 本设计要求 |
| --- | --- | --- |
| `MainAgentExecutor` | 生成 chunk、final event、artifact | 支持 transient publish 与 completion-only persistence；失败/取消不返回 partial artifact |
| live event recorder / `record_live_event` | 当前先入库再 publish | 拆成 transient publisher 与 persistent recorder，或为 event 标记 persistence policy |
| `OrchestrationService` | 保存 artifacts/events，更新 node/task | finalize 前检查取消状态；失败/取消不得保存 answer artifact；terminal events 继续持久化 |
| `ApiRuntime.iter_frontend_events` | replay persisted events + subscribe broker | 明确 persisted replay 不含 transient deltas；running task 只订阅新 live deltas |
| `CancellationService` | 写取消状态与事件 | 通知 running stream 停止 publish / 丢弃 late chunks |
| SSE auth helper | current-token check 可能写 last_used | 增加只读 current-token 校验或节流写入 |
| assistant history sync | 从 final event + artifact 生成 assistant message | 只处理 completed task 的 final answer；失败/取消不生成 assistant message |

## 11. 观测指标

建议新增或复用以下脱敏指标 / audit 字段：

- `llm_first_chunk_ms`
- `llm_stream_elapsed_ms`
- `sse_publish_latency_ms`
- `finalize_db_elapsed_ms`
- `stream_chunk_count`
- `stream_answer_char_count`
- `stream_reasoning_char_count`
- `partial_output_discarded`
- `stream_terminal_status`

这些指标只记录数字和状态，不记录正文。

## 12. 测试策略

### 12.1 单元 / API 回归

- LLM 返回多个 chunk 时，前端收到多个 output delta，但 storage 不保存逐 chunk `main_agent.output_delta`，audit JSONL 也不保存 delta 正文。
- 正常完成后，messages history 只包含最终 assistant answer。
- thinking enabled 时，reasoning delta 可实时到前端，但完成后不进入 messages history。
- provider stream 抛错时，不保存 assistant answer、不保存 answer artifact、不保存 `main_agent.output_final`；task failed；错误事件包含 `partial_output_discarded=true` 与 chunk 计数。
- 用户 cancel 时，停止继续 publish transient deltas；不保存 assistant answer；task cancelled；取消事件包含 `cancel_source=user`。
- SSE 断开不取消任务；任务完成后刷新可看到最终 answer。
- token refresh 后旧 SSE 在下一次 revalidation 关闭，但不会每个 event 写 `last_used`。
- 取消与 LLM 正常结束竞态下，取消优先，late final answer 被丢弃。

### 12.2 性能 / 集成验证

- mock LLM 100 个 chunk：后端不应产生 100 次 DB append_event，也不应产生 100 次 token touch。
- 远端 PostgreSQL 下 chunk 到 SSE 的 p50 延迟应显著低于逐 chunk 入库旧路径。
- finalize 阶段 DB 慢只影响完成收尾，不影响每个 chunk 展示。

## 13. 验收标准

- 普通回答流式速度接近 LLM API 原始流速。
- 正常完成后刷新 / 重新登录能看到最终 answer。
- 失败 / 取消 / 中断后刷新 / 重新登录看不到 partial answer，数据库与 audit 中也没有 partial answer 正文。
- 数据库保留足够状态和脱敏诊断，可追溯失败阶段与原因。
- reasoning 内容不持久化。
- SSE 认证不再对每个 chunk 写库。
- 用户点击停止后不再继续向该任务 SSE 推送新的 answer / reasoning delta。
- 无敏感信息写入 tracked 文件或 audit payload。

## 14. 风险、假设与决策

| 类型 | 内容 | 处理 |
| --- | --- | --- |
| 已确认决策 | 未完成、失败、取消、中断的 partial output 不入库 | 作为硬性验收标准 |
| 已确认决策 | reasoning 内容不持久化 | 继续沿用现有产品约束 |
| 风险 | SSE 断开期间的 transient output 无法 replay | 文档明确不承诺；完成后依赖 final answer 恢复 |
| 风险 | 取消与 provider stream 结束存在竞态 | finalize 前重读 task 状态，取消优先 |
| 风险 | 当前代码多个 `_record_event` 入口可能遗漏 | 实施计划必须统一 transient / persistent event policy |
| 假设 | 生产可接受中途失败/取消时丢弃 partial answer | 用户已确认该方向；文档作为记录 |

## 15. 自审结果

- Placeholder scan：无 TBD / TODO。
- 一致性：成功路径只保存 final answer；失败/取消/中断路径只保存状态与诊断，不保存 partial answer。
- 范围：聚焦主代理流式输出、SSE 认证 revalidation、最终持久化、取消竞态和失败追溯；不包含 SQLite 迁移或新框架。
- 歧义处理：明确 SSE 断开不等于用户取消；明确 reasoning 与 partial answer 都不持久化。
