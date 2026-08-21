# 统一同模型 Agent Loop 架构设计

- **日期**：2026-08-21
- **状态**：设计已确认，尚未实施
- **适用分支**：`main`
- **目标系统**：breeding-agent 后端编排、LLM runtime、Task 生命周期、Skill/MCP capability 调用与恢复
- **替代架构**：一次性 LLM Planner、WorkflowPlan/DAG、Runtime Replanner、独立 finalizer
- **不兼容决策**：不迁移、不恢复旧 DAG 任务；切换后的全部任务只走新的 Agent Loop

## 1. 背景与结论

当前默认请求先由 LLM Planner 生成完整 `WorkflowPlan`，再由 DAG executor 按依赖执行 capability，必要时通过
Runtime Replanner 修改计划，最后由独立 `main_agent.respond` 节点生成最终回答。这种架构适合可提前确定的工作流，
但不适合以下目标场景：

1. 一个 MCP Server 内部包含需要按结果连续选择的链式 Tool；
2. 同一用户目标可能因每次 Tool result 改变下一步动作；
3. 模型需要在执行中判断是否补充调用 Skill、MCP 或直接回答；
4. 一次模型响应可能产生多个独立 capability calls；
5. 规划、观察、纠错和最终回答必须由同一模型连续完成。

目标架构采用与 cc-agent、Codex 同类的在线循环：

```text
模型采样
  -> assistant tool calls
  -> capability invocation batch
  -> structured tool results
  -> 下一次模型采样
  -> ...
  -> 无 tool calls 的 assistant message
  -> 最终回答
```

本设计不在现有 DAG 外再包一层循环，也不把每次模型采样伪装成 Runtime Replan。`AgentLoopOrchestrator` 是
所有新任务的唯一运行时控制面。显式 Skill、显式 MCP、普通请求、补充输入、审批和 remote continuation
全部进入或恢复同一个 Agent Loop。

## 2. 设计原则

### 2.1 单一决策者

一个 Agent Run 从首次模型采样到最终回答固定使用同一 `model_edition` 和同一逻辑
`SharedLLMRuntime` session。模型同时负责：

- 判断是否需要调用 capability；
- 选择 capability 和受允许的参数；
- 观察 capability 结果；
- 纠正普通工具错误；
- 判断是否继续；
- 生成唯一最终回答。

不得重新引入独立 planner model、replanner model 或 finalizer model。Capability 内部已有的受控 LLM helper
可以继续复用同一 SharedLLMRuntime，但它们不是 Agent Run 的决策控制面。
Provider 不得在 Run 内回退到不同 model edition；固定模型在既有重试后仍不可用时，Run 按 fatal 失败。

### 2.2 无 Agent 轮次上限

Agent Loop 不设置 `maxTurns`、`max_replans` 或 `max_dynamic_nodes`。上下文接近模型预算时执行同模型压缩并
继续。以下现有边界不属于 Agent 轮次上限，继续保留：

- Provider transport/protocol retry；
- capability timeout；
- MCP Coordinator 内部的调用额度；
- backpressure、权限、审批和资源配额；
- 用户取消和安全控制面终止。

### 2.3 能力实现复用

保留现有 CapabilityRegistry、实例选择、CompositeExecutor、Skill executor、MCP Gateway/Coordinator、授权、
artifact、事件、interrupt、cancel 和存储安全合同。重构只替换它们之上的 Agent 编排控制面，并提取各路径
共用的 capability invocation 内核。

### 2.4 原生结构化调用

Agent model adapter 使用 OpenAI-compatible 原生 tool-call contract，不使用自由文本 JSON 伪协议。
Provider-neutral 类型隔离供应商 wire format，业务层只处理规范化 assistant message、tool call 和 tool result。

### 2.5 持久化先于副作用

每个 assistant sample 和 tool call 在 capability 执行前持久化。Tool result、interrupt 或 terminal 状态在执行后
追加。恢复时不自动重放可能已经产生副作用但没有权威结果的调用。

## 3. 范围

### 3.1 范围内

- 统一 Agent Loop runtime；
- 原生 LLM tool-call adapter；
- AgentRun/AgentItem 持久化与恢复；
- capability catalog、输入策略和并发门控；
- 普通、显式 Skill、显式 MCP 的统一入口；
- Skill missing-input、MCP approval、MRTR、remote Task continuation 恢复；
- 同模型上下文压缩；
- 唯一最终回答发布；
- 删除 DAG planner/executor/replanner/finalizer 运行时；
- SQLite、PostgreSQL、Runtime Sidecar/Rust contract 一致实现；
- API、SSE、history、frontend 适配和回归。

### 3.2 非范围

- 不改变 MCP Server discovery 和 Server 内 Tool discovery 时机；
- 不改变 MCP transport、协议版本、认证、Endpoint Policy 或 Result Parser；
- 不改变 Skill manifest 的业务输入/输出合同；
- 不引入第三方 Agent 框架；
- 不实现子 Agent；
- 不处理旧 DAG Task 的迁移、恢复或兼容读取；
- 不部署 `prod`；
- 不把 hidden reasoning 持久化为模型上下文或用户历史。

本设计只替换当前服务端 breeding-agent 的任务编排控制面，不替代
`docs/个人桌面长任务Agent总体设计总纲.md` 中尚未实施的个人桌面 Rust daemon、子 Agent 和本地文件安全架构。

## 4. 已选择方案

### 4.1 未采用：DAG 外层增加循环

该方案保留 planner、validator、expander、replanner、completion policy 和 finalizer，再在外层判断是否继续。
它形成两个相互竞争的控制面，模型在线决策仍受静态 DAG 预算和节点语义限制。

### 4.2 未采用：把每次采样建模为 Runtime Replan

该方案可以复用部分代码，但必须把每轮 assistant sample、tool batch 和下一轮 sample 编译成动态 DAG 节点，
继续依赖 `max_replans/max_dynamic_nodes`，也难以表达无界循环、同批并发与自然 assistant 终止。

### 4.3 未采用：把循环封装进 `main_agent.respond`

该方案代码表面最少，但内部 capability call 无法自然获得独立 TaskNode、interrupt、取消、artifact 和恢复边界，
并会让主 Agent capability 反向依赖所有 executor。

### 4.4 采用：单一 Agent Loop + 公共 Invocation Kernel

所有请求进入 `AgentLoopOrchestrator`。循环通过 `CapabilityInvocationService` 调用现有 executor；后者负责真实
调用的生命周期和持久化。旧 DAG runtime 在切换完成后删除，不保留兼容执行分支或 feature flag。

## 5. 总体架构

```text
API request / continuation wakeup
  -> AgentLoopOrchestrator
       -> AgentRunRepository
       -> AgentContextBuilder
       -> AgentToolCatalogBuilder
       -> AgentModelPort (SharedLLMRuntime)
            -> OpenAI-compatible native tool calls
       -> CapabilityInvocationService
            -> CapabilityRegistry
            -> Instance Scheduler
            -> CompositeExecutor
                 -> SkillExecutor
                 -> MCPDispatchExecutor
            -> TaskNode / Artifact / Event / Interrupt persistence
       -> AgentFinalOutputPublisher
            -> main_agent.output_final
            -> text Artifact
            -> assistant history
            -> SSE
```

在 `src/orchestration/agent_loop/` 建立以下固定边界：

| 文件 | 单一职责 |
|---|---|
| `models.py` | AgentRun、AgentItem、AgentSample、AgentToolCall、AgentToolResult 规范化模型 |
| `model_port.py` | Provider-neutral 模型采样接口和流事件 |
| `tool_catalog.py` | 当前请求可见 capability 到模型 Tool 的安全映射 |
| `context.py` | PromptEnvelope、AgentItem suffix 和 context summary 组装 |
| `invocation.py` | 单个或一批 capability call 的节点、结果、interrupt 和取消生命周期 |
| `runner.py` | 唯一 Agent Loop 状态机 |
| `final_output.py` | 最终回答 artifact、事件、history 和 Task terminal 发布 |

实现计划可以把纯常量或窄 helper 留在现有公共模块，但上述七项职责必须保持独立，不得把 model sampling、
invocation lifecycle 和 final publication 重新合并进 `ApiRuntime` 或单个巨型 executor。

## 6. 核心持久化模型

### 6.1 AgentRun

`AgentRun` 是一个 Task 的循环权威状态，至少包含：

| 字段 | 语义 |
|---|---|
| `run_id` | 与 Task 一对一的稳定 ID |
| `task_id` / `conversation_id` | 所属 Task 与 Conversation |
| `status` | `running / waiting / completed / failed / cancelled` |
| `model_edition` | Run 内固定模型版本 |
| `reasoning_effort` / `thinking_enabled` | Run 内固定模型选项 |
| `next_item_sequence` | append-only item 序号分配权威 |
| `compacted_through_sequence` | 最近 context summary 覆盖边界 |
| `waiting_item_id` | 当前等待完成的 tool call 或 continuation |
| `claim_owner` / `claim_token` / `lease_expires_at` | 单一运行者 claim |
| `revision` | CAS 修订号 |
| `created_at` / `updated_at` / terminal time | 生命周期时间 |

一个 Task 同一时刻只能有一个有效 Agent Run claim。恢复 worker、API continuation 和本地执行线程必须通过同一
claim/CAS contract 竞争，不能依赖进程内锁作为唯一正确性边界。

`AgentRun.status` 是内部执行权威，`Task.status` 是外部 API/SSE 生命周期投影。每次 waiting 或 terminal 转换必须在
同一 storage transaction/Sidecar 原子操作中同时更新两者；恢复时发现二者不一致属于 fatal consistency error，
不得按较宽松状态猜测继续。

### 6.2 AgentItem

`AgentItem` 是 append-only 模型可见轨迹。支持以下闭合 kind：

| kind | 内容 |
|---|---|
| `user_message` | 当前 Task 用户输入的可信引用与模型可见内容 |
| `assistant_message` | 一次完整模型 assistant sample 的可见文本和 sample metadata |
| `tool_call` | call ID、capability ID、policy 过滤后的参数、所属 sample 和调用顺序 |
| `tool_result` | call ID、状态、安全 agent projection、artifact refs、错误和 continuation metadata |
| `context_summary` | 同模型生成的摘要、覆盖序号和源 items digest |
| `continuation` | 审批、补充输入或 remote completion 被接纳后的可信唤醒事实 |

每个 item 具有确定性 `item_id`、task-scoped 单调 `sequence` 和规范化 payload digest。单个持久化 payload 上限为
128 KiB；超过上限的 capability 结果只在 item 中保存有界 agent projection、摘要和 artifact/result refs。

Hidden reasoning 不写入 AgentItem。Thinking delta 可以沿用 transient SSE，但不得进入 assistant history、
context summary 源文本或 durable frontend event payload。

### 6.3 Task 与 TaskNode

`Task` 继续作为用户任务的公共生命周期记录。`TaskNode` 改为一次真实 capability invocation 的执行账本：

- 一个 tool call 对应一个稳定 TaskNode；
- node ID 由 task ID、AgentItem tool-call ID 确定性生成；
- 保存 assigned instance、status、input/output refs 和时间；
- 不再表达预计划依赖、criticality、dynamic node 或 finalizer 语义。

TaskEdge 不再参与新运行时。Clean-cutover migration 必须删除 TaskEdge 表、contract、写入路径和编排读取，
同时删除 TaskNode 的 `criticality`、`dependency_type`、`retry_policy` DAG 字段和 Task 的 `root_node_id`。
Capability timeout、resource class 和 instance assignment 保留为调用执行属性，不再从 WorkflowNodePlan 派生。

## 7. 模型采样合同

### 7.1 Provider-neutral 类型

`AgentModelPort` 接受：

- 固定 system/tool rules；
- 有界 model-visible messages；
- 当前 `AgentToolDescriptor` 列表；
- Run 固定的 model/reasoning options；
- cancellation token。

返回流式规范化事件，并最终闭合为一个 `AgentSample`：

- `assistant_text`；
- 有序 `tool_calls`；
- finish reason；
- usage 和安全 provider metadata；
- 不包含需要业务层理解的供应商对象。

### 7.2 OpenAI-compatible adapter

`LLMClient` 使用 Chat Completions 原生 `tools` 和 assistant `tool_calls`/tool result message contract。流式 adapter
必须正确组装分片的 tool call ID、name 和 arguments，并拒绝：

- 重复 call ID；
- 缺失或未知 tool name；
- 无效 JSON arguments；
- 超过参数大小/深度限制；
- sample 结束后仍未闭合的 tool call；
- Provider 声称不支持 native tool calls。

`SharedLLMRuntime` 新增 agent sampling 方法，同时保留现有 `generate_text()` 和 `stream_events()` 给标题、内部窄
adapter 等非 Agent 场景。Run 绑定的 `model_edition` 在采样和 compaction 中不得切换。

### 7.3 文本与 tool calls 并存

System contract 要求模型在需要工具时只输出 tool calls，在完成时输出最终 assistant text。Runtime 仍必须防御
文本和 tool calls 同时出现：

- 完整 sample 先缓冲；
- 只要存在 tool call，assistant text 作为非用户可见 observation 持久化，不发布为最终回答；
- 只有无 tool calls 的非空 assistant text 才进入 final publisher。

这会保留最终回答的 SSE/event 合同，但最终文本在 sample 完整闭合后发布，避免中间文字污染聊天正文。

## 8. Tool Catalog 与输入策略

### 8.1 请求可见能力

`AgentToolCatalogBuilder` 必须只消费 `CapabilityRegistry.list_for_request(request, public_only=True)`，继续遵守：

- 用户 MCP execution path；
- 当前用户可用 Server Profiles；
- capability enabled/public 状态；
- Skill bundle revision；
- 安全来源和请求可信 metadata。

`main_agent.respond` 不再注册为模型工具。最终回答由无 tool calls 的 assistant sample自然产生。

### 8.2 Provider-safe 名称

Capability ID 与 provider function name 使用稳定双向映射。Provider-safe 名称仅允许 ASCII 字母、数字、下划线
和连字符；映射冲突在启动或 catalog 构造时 fail closed。模型返回的 name 必须先通过当前 sample catalog 反查，
不能直接当 capability ID 执行。

### 8.3 CapabilityInvocationPolicy

现有 `CapabilityPayloadPolicy` 重命名并扩展为 `CapabilityInvocationPolicy`：

| 字段 | 作用 |
|---|---|
| `model_allowed_fields` | 模型可以提供的字段白名单 |
| `input_schema` | 提供给模型并用于 Runtime 校验的 JSON Schema |
| `system_payload_factory` | 从可信 request/run context 生成的字段，覆盖模型同名值 |
| `parallel_safe` | 是否允许与同批其他 parallel-safe call 并发 |

所有策略默认 fail closed：无 policy 的 capability 不进入 Agent catalog。执行前再次按 policy 过滤/验证，防止模型
输出、prompt injection 或 provider bug 绕过 authority。

初始安全声明如下：

- `mcp.dispatch`：`parallel_safe=false`；模型只可提供自动模式允许的字段，显式 Server ID 由系统注入；
- 现有 `skill.*`：默认 `parallel_safe=false`；只有 Skill contract 后续明确声明并通过隔离回归后才可并发；
- 不存在“因为模型同时返回多个 call 就默认并发”的隐式规则。

一轮多个非并发调用仍被接受，并按模型 call 顺序串行执行。

## 9. 统一路由语义

### 9.1 普通请求

首轮 catalog 包含当前请求可见的全部 capability，模型自由选择调用、继续调用或直接回答。

### 9.2 显式 Skill

显式 Skill 不走独立 workflow。AgentRun 保存可信 initial constraint：

- 第一轮 catalog 只包含指定 Skill；
- model request 使用 required tool choice；Provider 不支持或不遵守 required contract 时进入有界 protocol retry，
  耗尽后 fatal；
- 模型必须完成该 tool call，不能用提前 assistant text 绕过；
- tool result 写回后 constraint 消费，后续恢复普通 catalog；
- Skill 缺输入时暂停同一 tool call，用户回答后继续该 call，再回到循环。

### 9.3 显式 MCP Server

显式 MCP 同样不走独立 workflow：

- 第一轮 catalog 只包含 `mcp.dispatch`；
- model request 使用 required tool choice，不能用提前 assistant text 绕过；
- `server_id` 由 trusted request metadata 注入，模型不可选择或覆盖；
- MCP Coordinator 继续负责 Server discovery、tools/list、Selector、逐 Tool authorization、Tool chain、MRTR 和
  remote Tasks；
- dispatch 结果写回后 initial constraint 消费，模型可以继续调用其他 capability 或输出最终回答。

### 9.4 自动 MCP

普通请求可由模型选择 `mcp.dispatch` 和当前安全 Server Profile。Outer Agent Loop 不加载 Server 内完整 Tool list；
MCP Coordinator 在 Server 被选择后按现有设计发现 Tool，并在内部 Selector 循环组装 Tool chain。这样保留现有
MCP 注意力和安全边界。

## 10. Agent Loop 状态机

```text
claim AgentRun
  -> append current user/continuation item if required
  -> build bounded context and tool catalog
  -> sample same model
  -> persist assistant sample
       -> tool calls present
            -> persist all tool_call items before execution
            -> execute invocation batch
            -> persist ordered tool_result items
            -> release/renew claim and continue
       -> no tool calls + non-empty assistant text
            -> publish final output
            -> complete AgentRun and Task
       -> structurally invalid/empty sample
            -> adapter protocol retry
            -> retry exhausted => fail
```

主循环无 iteration counter 和 turn budget。每次循环前后都检查 Task cancellation、AgentRun claim 和 storage
revision。异步用户 steer 不作为首阶段功能；新的用户消息通过现有 conversation/task guard 开启新 Task，waiting
Task 的回答通过 continuation contract 精确路由。

## 11. Capability Invocation

### 11.1 公共执行内核

从现有 `OrchestrationService._execute_node()` 提取 `CapabilityInvocationService`，统一负责：

- selected MCP route authority 校验；
- instance selection；
- TaskNode start CAS；
- CapabilityExecutionRequest 构造；
- CompositeExecutor 调用；
- artifact 和 event 持久化；
- interrupt、Skill missing input、MCP remote pending；
- capability error 到 node/tool result 的映射；
- cancellation 和 late result discard；
- TaskNode terminal 状态。

`AgentLoopOrchestrator` 不复制上述逻辑，也不直接调用具体 Skill/MCP executor。

### 11.2 批次与顺序

同一 assistant sample 的 tool calls 构成 invocation batch：

- 每个 call 先独立创建 tool_call item 和 TaskNode；
- parallel-safe calls 通过共享读锁执行；
- non-parallel-safe call 通过写锁独占；
- 执行可以重叠，但 tool_result 按原 call 顺序追加并回填模型；
- 一个 call 的普通失败不取消同批其他 call；
- fatal 控制面错误取消未启动 call，并终止 Run。

### 11.3 Tool result envelope

每个结果向模型提供闭合结构：

- `call_id` / `capability_id`；
- `status=completed|failed|aborted`；
- bounded safe output projection；
- artifact/download refs；
- closed error code、message、retriable；
- waiting/continuation 不作为 terminal tool result，直到原 call 真正恢复或终止。

Tool result 明确标记为外部数据而非用户指令，继续复用 PromptEnvelope 的 security role 和裁剪规则。

## 12. 暂停、恢复与重启

### 12.1 Waiting

以下结果把 AgentRun 和 Task 置为 waiting，并保存 `waiting_item_id`：

- Skill missing input；
- MCP approval；
- MCP elicitation/MRTR；
- MCP remote Task；
- 其他现有 Interrupt contract。

Waiting 时没有 tool_result terminal item，因为对应 tool call 尚未结束。

### 12.2 Continuation

用户回答、approval acceptance 或 remote completion 到达后：

1. 通过 owner、conversation、task、node、call 和 resume digest 校验；
2. claim 同一个 AgentRun；
3. 调用现有 capability-specific continuation authority 完成原 tool call；
4. append continuation item 和唯一 tool_result；
5. 清除 waiting 状态；
6. 继续同一 Agent Loop。

不得构造 `WorkflowPlan`、恢复静态 finalizer 或创建新的 Agent Run。

MCP remote binding/outbox 中现有 `continuation_plan` authority 替换为闭合的 Agent continuation locator，至少绑定
`run_id`、`tool_call_item_id`、task/node/call identity 和 digest。Envelope 继续遵守引用式、无实际 Tool 参数、无
附件正文、无 raw result 的安全原则。

### 12.3 Crash recovery

恢复规则：

- tool call 已有权威 terminal result但缺 AgentItem：幂等补写 tool_result；
- MCP 有权威 continuation state：恢复现有 MCP 状态机，完成后补写 tool_result；
- tool call 可能已产生副作用但没有可证明结果：写入 `aborted` tool result，不自动重放；
- 未开始执行且 start authority 明确不存在：可以写入 `aborted`，由模型决定是否重新调用；
- orphan tool_result 或 call/result identity 不一致：fatal storage consistency error。

Runtime 不承诺任意 capability exactly-once；它承诺不自动重放不确定副作用，并为支持 durable continuation 的
capability 保持现有权威恢复语义。

## 13. 错误、取消与停止

### 13.1 模型可恢复错误

以下错误转换为 `status=failed` tool result，并继续交给模型：

- 未知或当前不可见 tool；
- 参数 schema/allowlist 失败；
- capability unavailable；
- 普通权限拒绝；
- retriable 或 non-retriable 业务错误；
- 恢复时补写的 aborted；
- capability 返回的安全错误。

`retriable=false` 仅禁止 Runtime 自动重放，不禁止模型选择其他方法。

### 13.2 Fatal

以下情况终止 AgentRun 并使 Task failed：

- storage/CAS/identity 一致性损坏；
- authority 或安全状态损坏；
- Provider transport/protocol retry 耗尽；
- Provider 不支持必须的 native tool-call contract；
- context compaction 无法完成；
- Runtime 内部不可恢复错误。

### 13.3 取消

取消后：

- 不启动新 tool call；
- 通过现有 CancellationService 取消 in-flight capability；
- 未闭合 call 追加 aborted result；
- 迟到结果继续执行 late-result discard；
- AgentRun 和 Task 最终为 cancelled；
- 不再进行模型采样或 final publication。

### 13.4 正常停止

唯一正常完成条件是：模型返回无 tool calls 的非空 assistant message。Waiting 不是停止，compaction 不是停止，
普通 tool failure 不是停止。Agent Loop 不通过轮数、动态节点数或 planner budget 完成任务。

## 14. 上下文与压缩

### 14.1 ContextBuilder

每次采样由 `AgentContextBuilder` 生成：

1. stable system contract；
2. stable tool/capability rules；
3. conversation memory；
4. 最近 context summary；
5. summary 后的 AgentItems；
6. current continuation/user facts；
7. final guard。

Tool results 使用现有 safe projection、artifact references 和 PromptEnvelope security role，不把 raw MCP result、上传
正文或 hidden reasoning注入模型。

### 14.2 Compaction

达到 Run 固定模型的输入预算时：

- 使用同一 `model_edition` 和同一 SharedLLMRuntime生成结构化 summary；
- summary 绑定 covered sequence range 和源 items digest；
- append `context_summary`，CAS 更新 `compacted_through_sequence`；
- 原始 items 不删除；
- 下一轮使用 summary 加未覆盖 suffix；
- compaction 不能调用业务 capability，也不能改变 AgentRun 的工具决策。

Compaction Provider 重试耗尽后 fail loud，不静默丢历史或伪造最终回答。

## 15. 最终回答与用户可见事件

`AgentFinalOutputPublisher` 接收已经闭合的最终 assistant message，不再次调用模型。它必须：

- 生成现有 final text Artifact；
- 发出 `main_agent.output_final(response_role=final)`；
- 保留安全 `main_agent.llm_call`/Agent sample audit metadata；
- 同步 conversation assistant history；
- 完成 AgentRun 和 Task；
- 确保重复调用幂等，不生成第二个 final artifact/message。

现有 capability-missing fallback 披露合同继续生效：可信 fallback metadata 由 AgentContextBuilder 注入，最终文本
必须经过现有 disclosure guard，并发出既有 frontend/history notice。删除 `main_agent.respond` 不得删除这项用户
可见事实披露或文件下载声明校验。

Thinking 继续只通过 transient channel 展示。带 tool calls 的 sample 文本不进入用户聊天正文。Frontend 可以继续显示
TaskNode/Skill/MCP progress，但不渲染 tool result 为 assistant answer。

## 16. DAG 退役范围

最终源码不再包含新任务运行时依赖：

- `WorkflowPlan` / `WorkflowNodePlan`；
- LLM、Auto、Skill、Main Agent、MCP Workflow Provider；
- Workflow Router、Expander、Validator；
- Planner contract、Planner repair 和 Planner node identity；
- Runtime Replanner、Soft Skill Replanner、Main Agent Replanner；
- CompletionPolicy 和 DAG execution loop；
- `main_agent.respond` finalizer capability；
- `MainAgentRespondCapability` 内部的 auto Skill matching、script orchestration 和第二次回答模型调用；可复用的
  PromptEnvelope、fallback disclosure 与 final artifact/event helper 提取到 Agent Loop边界；
- `max_replans` / `max_dynamic_nodes`；
- `mcp_remote_task_continuation_plan`；
- TaskEdge runtime dependency。

允许保留与“planner”同名但服务于其他独立产品功能的代码，前提是引用审查证明它不再参与 Task 编排；否则应
重命名或删除，避免形成隐式第二控制面。

本设计不提供旧 DAG Task reader、resume adapter、feature flag 或 fallback。切换前存在的旧 Task 不属于迁移或验收
范围。

## 17. 装配与 API 切换

`ApiRuntime._run_execution()` 不再 build plan，而是：

```text
scrub/attach trusted request context
  -> acquire backpressure
  -> AgentLoopOrchestrator.start_or_resume(request)
  -> handle completed/waiting/failed/cancelled
  -> assistant history / gateway cleanup / revision release
```

现有 `/tasks`、SSE、interrupt answer、approval 和 cancel API 路径保持外部 DTO 兼容，但内部统一唤醒 AgentRun。
Runtime assembly 删除 workflow providers/replanners/finalizer，注入 AgentLoop、Agent repositories、model port 和
CapabilityInvocationService。

当前依赖 WorkflowPlan 的 MCP shadow observation 改为 capability invocation hook：只在真实 `mcp.dispatch` call
进入执行内核时开始对应 observation，不再预读未来计划。

## 18. 数据库与 Rust Sidecar

AgentRun/AgentItem storage contract 必须在切换前同时落地：

- SQLite SQLAlchemy model/repository/migration；
- PostgreSQL model/repository/manifest/reconciler/权限测试；
- Runtime Sidecar proto、Rust model、transaction/CAS 和 Python client；
- StoragePort facade；
- SQLite/PostgreSQL/Rust contract conformance tests。

关键不变量：

1. task 与 run 一对一；
2. item sequence 在 task 内唯一且单调；
3. tool result 必须引用已有 tool call；
4. 一个 tool call 最多一个 terminal result；
5. waiting item 与 AgentRun waiting authority一致；
6. completed run 恰有一个 final assistant message/publication receipt；
7. claim/lease/revision 防止双执行；
8. payload size、digest 和 closed kind 在数据库与 Rust 两侧一致。

PostgreSQL role/permission 边界沿用现有最小权限设计；Agent storage 不获得读取 MCP credential/raw result 的能力。

## 19. 实施策略

开发可以使用中间 checkpoint，但最终提交序列完成后只存在一个运行时控制面：

1. **基线**：增加当前 capability、MCP、Skill、interrupt、cancel、history 行为锁定测试；
2. **模型与存储**：实现 Agent types、native tool-call adapter、Agent repositories 和 Sidecar contract；
3. **Invocation Kernel**：从旧 service 提取并让旧测试先通过公共内核，证明行为保持；
4. **Loop**：实现自动、显式、multi-call、error、final、compaction；
5. **Continuation**：切换 Skill input、approval、MRTR、remote Task 到 AgentRun resume；
6. **入口切换**：所有 API execution 路由到 Agent Loop；
7. **DAG 删除**：删除 planner/workflow/replanner/finalizer 及对应 wiring/tests；
8. **收口**：清理 storage contract、frontend、文档、AGENTS、CHANGELOG，运行全量门禁。

阶段 3 可以在开发分支短暂让旧 service 调用提取后的 kernel，但不得作为最终兼容模式、配置开关或发布路径。

## 20. 功能需求

| ID | 需求 |
|---|---|
| FR-1 | 所有新 Task 进入 AgentLoopOrchestrator，不构建 WorkflowPlan |
| FR-2 | 一个 AgentRun 的决策、观察、compaction 和最终回答固定同一 model edition |
| FR-3 | 模型原生返回一个或多个 tool calls，并接收有序结构化 tool results |
| FR-4 | 普通 capability 错误写回模型，不直接失败 Task |
| FR-5 | 只有无 tool calls 的非空 assistant message 正常完成 Task |
| FR-6 | Agent Loop 不实现 maxTurns/max replans/max dynamic nodes |
| FR-7 | 显式 Skill/MCP 通过可信首轮 constraint 强制，但随后恢复普通循环 |
| FR-8 | approval、missing input、MRTR、remote Task 恢复原 AgentRun和原 tool call |
| FR-9 | 不确定副作用调用不自动重放，缺结果时补 aborted |
| FR-10 | 一轮多个 call 支持安全并发门控和确定结果顺序 |
| FR-11 | 上下文不足时同模型 compaction，原始 AgentItems 保留 |
| FR-12 | 最终回答只发布一次，不执行独立 finalizer model call |
| FR-13 | MCP discovery、selector、authorization 和 result parsing 保持现有安全设计 |
| FR-14 | 最终源码删除 DAG runtime 和旧任务兼容恢复入口 |

## 21. 非功能需求

| 维度 | 要求 |
|---|---|
| 一致性 | AgentRun claim、item sequence、call/result pairing、final publication 使用数据库/Rust 可验证不变量 |
| 安全 | 模型只见请求可见 capability；参数 fail closed；system authority覆盖模型字段 |
| 恢复 | durable capability continuation恢复；不确定副作用不自动重放 |
| 可观测 | sample、call、result、waiting、compaction、terminal 有低敏事件和稳定关联 ID |
| 上下文 | hidden reasoning/raw MCP/upload body不进入 AgentItems；tool result有界 |
| 性能 | 同批安全调用可并发；非并发 capability 串行；无 busy polling |
| 兼容 | 对外 API/SSE/history 尽量保持；不兼容仅限明确删除的旧 DAG Task runtime |
| 可维护 | ApiRuntime不承载模型循环细节；invocation lifecycle只有一个实现 |

## 22. 测试与验收

### 22.1 Model adapter

- 单 tool call、多个 tool calls、分片 arguments；
- 最终 assistant text；
- text + tool calls 防御；
- malformed JSON、重复 ID、未知 name、缺失 ID；
- Provider 不支持 tool calls；
- cancellation、transport retry 和 usage metadata；
- model edition 在 loop/compaction 中一致。

### 22.2 Agent Loop

- `tool_call -> tool_result -> next sample -> final`；
- 连续链式 capability 调用；
- 一轮多 call，串行与 parallel-safe gate；
- tool results 按 call 顺序回填；
- 普通失败后模型改用其他 capability；
- 超过旧 maxTurns/max replans 数值后仍继续并完成；
- 空/损坏 sample 不误完成；
- final answer 唯一且没有第二 LLM call；
- compaction 后继续并使用 summary + suffix。

### 22.3 显式路由

- 显式 Skill 第一轮只能调用目标 Skill；
- 显式 MCP 第一轮只能调用 pinned `mcp.dispatch` 且 server ID 不可覆盖；
- constraint 完成后 catalog 恢复；
- 普通请求 capability visibility 与当前 registry相同；
- 用户 MCP unavailable 时不回退 legacy MCP。

### 22.4 Continuation 与恢复

- Skill missing input 回到原 call；
- MCP approval/MRTR 回到原 call；
- remote Task completion 追加唯一 tool result 后继续；
- crash 后已有 authority result补写；
- unknown side-effect call补 aborted 且不重放；
- duplicate wakeup/approval/outbox 幂等；
- 双 worker claim/CAS竞态；
- cancel 与 completion 线性化。

### 22.5 Storage

- SQLite/PostgreSQL/Rust Sidecar AgentRun/AgentItem parity；
- sequence、digest、payload上限、kind closure；
- call/result foreign identity；
- terminal result唯一；
- final publication唯一；
- migration、manifest、权限和 rollback安全。

### 22.6 现有回归

- Skill capability、input resolution、artifacts；
- MCP ordinary/approval/MRTR/remote/recovery/result parsing；
- cancel/interrupt/mailbox/task lifecycle；
- API/SSE/history/frontend；
- prompt envelope、memory、model reasoning options；
- Rust quality gates和contract tests。

### 22.7 删除证明

最终静态检查要求 `src/` 中不存在运行时引用：

- `WorkflowPlan` / `WorkflowNodePlan`；
- `RuntimeReplanner`；
- `max_replans` / `max_dynamic_nodes`；
- workflow provider/router/expander/validator；
- `main_agent.respond` 作为 finalizer；
- `mcp_remote_task_continuation_plan`；
- TaskEdge dependency scheduling。

允许测试 fixture 或迁移文件引用已删除数据库列/表名时，必须有明确 migration-test 语义，不能被生产代码 import。

## 23. 完成标准

只有满足以下条件才能声明改造完成：

1. 所有 API 执行入口使用 Agent Loop；
2. 全部显式和 continuation 场景恢复同一 AgentRun；
3. 普通 tool failure 可由模型继续处理；
4. 无 Agent 轮次上限；
5. 同一模型完成所有 Agent 决策和最终回答；
6. DAG runtime、replanner、finalizer 已从生产源码和装配删除；
7. SQLite/PostgreSQL/Rust Sidecar Agent storage parity通过；
8. 聚焦测试、完整后端回归、frontend测试/build、Rust quality gates通过；
9. 最终 diff 无无关重构，AGENTS/CHANGELOG/文档索引同步；
10. 未把本地仓库实现误称为 `prod` 部署或外部环境验证。

## 24. 已接受的取舍

- 接受 clean cutover，不为旧 DAG Task 保留恢复能力；
- 接受为正确暂停/恢复新增 AgentRun/AgentItem storage contract；
- 接受最终文本在完整 sample 闭合后再发布，以防 tool-call sample 的中间文字污染聊天；
- 初始所有现有 capability 默认非并发安全，先保证正确性；
- 保留 MCP 内部 Selector Tool chain，而不是把完整 MCP Tool list一次性暴露给 outer Agent；
- 不通过固定轮次上限防无限循环，依赖模型自然终止、用户取消、fatal控制面和上下文压缩。

## 25. 后续实施计划要求

本设计批准后，实施计划必须：

- 按可独立验证的 checkpoint 拆分；
- 每个 checkpoint 指定测试、删除范围和回滚点；
- 先建立 storage/model contract，再切换恢复路径；
- 在删除旧 DAG 前完成所有 Agent Loop 路径证明；
- 最终不保留 dual-runtime feature flag；
- 对 `docker_cmd.md` 保持绝对保护，不读取、不跟踪、不删除；
- 大规模修改按仓库规则创建清晰 Git checkpoint。
