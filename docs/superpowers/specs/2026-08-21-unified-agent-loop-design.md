# 统一同模型 Agent Loop 架构设计

- **日期**：2026-08-21
- **状态**：document-perfectization 第四次全量审计 100/100 通过；设计已确认，尚未实施
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
cutover后全部任务的唯一运行时控制面。显式 Skill、显式 MCP、普通请求、补充输入、审批和 remote continuation
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

### 3.3 用户、参与者与价值

| 参与者 | 当前问题 | 目标价值 |
|---|---|---|
| 最终用户 | 一次性计划无法根据中间结果持续调整；多 Skill/MCP 最终回答依赖静态 finalizer | Agent 根据真实结果继续选择能力，并只生成一次最终回答 |
| 显式 Skill/MCP 用户 | 显式命令进入独立 workflow，审批或补充输入后恢复静态节点 | 显式约束只限制首次调用，随后恢复同一 Agent Run |
| Skill 作者 | `delegated_main_agent`、`python_subprocess`、`platform_service` 依赖不同执行路径 | 三种模式都有明确、可测试的 Agent Tool 语义，不要求重写现有 Skill contract |
| MCP Server 集成方 | Outer planner 与 Server 内 Selector 是两层分离的模型决策 | 保留现有 discovery、Selector、审批、MRTR、remote Task 和 Result Parser 安全边界 |
| API/Frontend | 依赖 Task、TaskNode、SSE、interrupt、history 和 `/graph` 恢复读模型 | 执行控制面切换但用户可见任务、进度、审批和历史合同保持 |
| Runtime/Storage 维护者 | WorkflowPlan、Replanner、finalizer 与恢复路径交叉耦合 | 单一 AgentRun claim、append-only AgentItems 和原子 call outcome合同 |
| 运维与安全审查者 | 无界 Agent Loop 需要明确取消、观测、权限和 no-replay边界 | 保留既有安全配额，并新增低敏 Run/采样/调用/恢复指标 |

用户价值成功条件不是“代码中存在 while loop”，而是：模型能在一个 Task 内根据真实 tool result继续决策；
显式与自动路径共享相同安全执行内核；等待/恢复不丢失模型轨迹；最终回答由同一模型生成且只发布一次。

### 3.4 当前状态与仓库证据

| 当前事实 | 仓库证据 | 本设计要求 |
|---|---|---|
| 默认路径先生成完整 JSON WorkflowPlan | `src/orchestration/llm_workflow_provider.py::build_plan`、`src/orchestration/planner_contract.py::parse_planner_output` | 删除一次性 Planner 和 WorkflowPlan runtime |
| DAG executor 在内存保存 node outputs并按 plan遍历 | `src/orchestration/service.py::execute_request` | 改为 durable AgentItem/tool result循环 |
| `SharedLLMRuntime` 已复用一个逻辑模型 runtime，但只提供 text/stream text接口 | `src/integrations/llm_runtime.py::SharedLLMRuntime` | 增加 Run-bound native tool-call model port |
| `LLMClient` 会把 Provider 不支持的 message role降级 | `src/integrations/llm_client.py::_messages_payload` | Agent path禁止 tool/assistant role fallback并在启动时 fail closed |
| instruction-only Skill 默认 `delegated_main_agent` | `tests/integrations/agent_skills/test_execution.py::test_instruction_only_skill_defaults_to_delegated_main_agent_direct` | 增加可信 Delegated Skill activation路径 |
| SkillExecutor 明确拒绝 delegated mode | `src/capabilities/skill_tool/executor.py::execute` | delegated mode不得错误路由到 SkillExecutor |
| MCP Router/Selector 只调用 `text_generator(prompt)` | `src/capabilities/mcp_dispatch/server_router.py`、`selector.py` | 每次调用显式绑定当前 AgentRun model options |
| MCP remote recovery持久化 `continuation_plan` | `src/core/models.py::MCPRemoteTaskBinding`、`src/api/runtime.py::_run_execution` | 替换为 identity-bound Agent continuation locator |
| `/tasks/{id}/graph` 和 frontend恢复依赖 node/edge DTO | `src/api/routes/tasks.py::get_task_graph`、`frontend/src/App.tsx::pollTaskGraphFallback` | 保留只读兼容投影，但不保留 DAG执行/恢复 |
| Task 在 Node等待输入时仍保持 `running` | `src/lifecycle/task_state_machine.py`、Rust `TaskStatus` contract | AgentRun使用细粒度 waiting状态，Task继续投影为 running |

### 3.5 可访问性适用性

本设计不新增输入控件、视觉交互或导航模式；现有 interrupt、approval和下载卡片继续复用，因此没有新的
可访问性产品需求。若实施改变这些组件的 DOM、焦点或键盘行为，必须按对应 frontend组件的既有可访问性测试
执行，不能以本段“不新增交互”为由豁免。

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
| `status` | `running / waiting_for_input / waiting_for_dependency / completed / failed / cancelled` |
| `model_edition` | Run 内固定模型版本 |
| `reasoning_effort` / `thinking_enabled` | Run 内固定模型选项 |
| `next_item_sequence` | append-only item 序号分配权威 |
| `compacted_through_sequence` | 最近 context summary 覆盖边界 |
| `active_sample_item_id` | 当前尚未闭合 invocation batch 的 assistant sample |
| `waiting_call_item_ids` | 当前等待 continuation 的零到多个 tool call ID，按 call ordinal排序 |
| `next_batch_call_ordinal` | Waiting恢复后下一个尚未启动的 call ordinal |
| `claim_owner` / `claim_token` / `lease_expires_at` | 单一运行者 claim |
| `revision` | CAS 修订号 |
| `created_at` / `updated_at` / terminal time | 生命周期时间 |

一个 Task 同一时刻只能有一个有效 Agent Run claim。恢复 worker、API continuation 和本地执行线程必须通过同一
claim/CAS contract 竞争，不能依赖进程内锁作为唯一正确性边界。

Claim必须复用现有 Task lease语义，不能再建立一套与 Task lease并行竞争的 Agent专用租约：Runtime Sidecar路径以
`RuntimeLeaseFacade`/TaskLease为唯一 lease authority，SQLite和PostgreSQL实现等价的 acquire/renew/release CAS；
`AgentRun.claim_*`字段是该唯一租约在 Run contract中的权威投影。租约生命周期固定如下：

1. 执行 worker开始或恢复 Run前，以正数可配置 TTL获取租约；当前有效 lease token、未过期时间和 Run revision是
   所有后续 sample、outcome、waiting和 terminal commit的前置条件；生产 expiry比较使用 storage/Sidecar权威时钟，
   client传入时钟只允许用于可控测试；
2. 在模型采样、context compaction、active capability wave和 final publishing期间运行 heartbeat，成功续租间隔不得
   大于 TTL的三分之一；renew返回的新 token/revision必须原子替换旧 claim，旧 token立即失效。Heartbeat controller
   持有旋转后的 fencing token，commit在提交时读取当前 token/revision，不缓存调用启动时的旧值；
3. transient renew错误只能在当前 `lease_expires_at` 前重试；一旦无法证明租约仍有效即视为 lease lost，触发本地
   cancellation，禁止启动新调用，且所有 commit必须因 stale token/revision fail closed；
4. 已在执行中的 capability晚到结果不能绕过 claim写入。恢复者按第12节 authority/no-replay规则接纳已有权威结果，
   对不确定副作用调用补 `aborted`，不得自动重放；
5. 转入 waiting时先原子提交 waiting状态和全部 authority，再释放租约；continuation worker必须重新 acquire并校验
   waiting集合后才能写入结果或继续剩余 wave；waiting期间不运行 heartbeat；
6. completed/failed/cancelled原子提交同时清空 claim；进程异常退出则由 TTL到期允许其他 worker接管。Release失败
   不得回滚已提交终态，但必须记录可恢复的 lease cleanup结果。

实现不得把 TTL当作 Agent轮次或调用超时：只要 worker持有有效租约，长时间 Run可以持续续租。测试使用可控时钟，
不得依赖真实 sleep证明上述边界。

`AgentRun.status` 是内部执行权威，`Task.status` 是外部 API/SSE 的粗粒度生命周期投影。现有 TaskStatus 没有
waiting值，因此状态映射固定如下：

| AgentRun | Task | TaskNode/Interrupt |
|---|---|---|
| `running` | `running` | 当前调用为 pending/running/terminal |
| `waiting_for_input` | `running` | 至少一个 Node `waiting_for_input`，对应 open Interrupt |
| `waiting_for_dependency` | `running` | 至少一个 Node `waiting_for_dependency`，对应 durable remote authority |
| `completed` | `completed` | 全部 batch闭合，final publication receipt存在 |
| `failed` | `failed` | fatal error receipt存在 |
| `cancelled` | `cancelled` | cancellation terminal receipt存在 |

每次 waiting 或 terminal 转换必须在同一 storage transaction/Sidecar 原子操作中更新 AgentRun、Task投影及相关
Node/Interrupt authority。恢复时发现不符合上表属于 fatal consistency error，不得按较宽松状态猜测继续。

### 6.2 AgentItem

`AgentItem` 是 append-only 模型可见轨迹。支持以下闭合 kind：

| kind | 内容 |
|---|---|
| `user_message` | 当前 Task 用户输入的可信引用与模型可见内容 |
| `assistant_message` | 一次完整模型 assistant sample 的可见文本和 sample metadata |
| `tool_call` | call ID、capability ID、policy 过滤后的参数、所属 sample 和调用顺序 |
| `tool_result` | call ID、状态、安全 agent projection、artifact refs、错误和 continuation metadata |
| `skill_activation` | delegated Skill 的安全公开 activation profile、bundle revision和投影内容 digest |
| `context_summary` | 同模型生成的摘要、覆盖序号和源 items digest |
| `continuation` | 审批、补充输入或 remote completion 被接纳后的可信唤醒事实 |

每个 item 具有确定性 `item_id`、task-scoped 单调逻辑 `sequence` 和规范化 payload digest。单个持久化 payload
上限为 131,072 bytes，按从现有 CP7 canonical JSON算法提取到 core 的公共实现计量：严格 JSON value、UTF-8、
`ensure_ascii=false`、禁止 NaN/Infinity、key排序、无多余空白并包含一个结尾 LF。Python、PostgreSQL校验和 Rust
Sidecar必须使用相同 golden vectors。超过上限的 capability结果只在 item 中保存有界 agent projection、摘要和
artifact/result refs。

Tool-call sample提交时，Runtime在一个原子操作中持久化 assistant item、全部 tool_call items，并为每个 call
预留唯一 result item ID和逻辑 sequence；`next_item_sequence` 一次跨过整个预留区间。Result可以按完成顺序写入
预留位置，但写入后不可更新或删除。ContextBuilder按 parent sample和 call ordinal重建 Provider所需顺序，不把
数据库提交先后误当作模型消息顺序。

Hidden reasoning 不写入 AgentItem。Thinking delta 可以沿用 transient SSE，但不得进入 assistant history、
context summary 源文本或 durable frontend event payload。

### 6.3 Task 与 TaskNode

`Task` 继续作为用户任务的公共生命周期记录。`TaskNode` 改为真实 capability invocation和内部 final publication的
执行账本：

- 一个 tool call 对应一个稳定 TaskNode；
- node ID 由 task ID、AgentItem tool-call ID 确定性生成；
- 最终 assistant sample对应一个确定性 `agent.final_output` TaskNode，作为 final text Artifact和 final events的
  producer；它不进入 Tool catalog、不调用模型，也不是 DAG finalizer；
- 保存 assigned instance、status、input/output refs 和时间；
- 不再表达预计划依赖、criticality、dynamic node 或 finalizer 语义。

TaskEdge 不再参与新运行时。Clean-cutover migration 必须删除 TaskEdge 表、contract、写入路径和编排读取，
同时删除 TaskNode 的 `criticality`、`dependency_type`、`retry_policy`、`timeout_policy`、`resource_class` DAG字段和
Task 的 `root_node_id`。Instance assignment保留为真实调用属性；`agent.final_output` 不分配 capability instance。
现有 Skill sandbox、MCP call/remote Task和 Provider内部超时继续由各自 capability/runtime合同执行；当前
OrchestrationService没有执行 WorkflowNodePlan timeout，因此本设计不把该装饰性字段误称为安全边界，也不新增
猜测性的统一外层 timeout。

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
- 缺失、空白或不符合 provider-safe语法的 tool name；
- 无效 JSON arguments；
- 超过参数大小/深度限制；
- sample 结束后仍未闭合的 tool call；
- Provider 声称不支持 native tool calls。

`SharedLLMRuntime` 新增 agent sampling 方法，同时保留现有 `generate_text()` 和 `stream_events()` 给标题、内部窄
adapter 等非 Agent 场景。Run 绑定的 `model_edition` 在采样和 compaction 中不得切换。

具有合法 call ID、合法 JSON arguments和 provider-safe name，但 name不在当前 sample catalog中的调用不是 wire
损坏。Runtime持久化该 call并提交 `status=failed / code=unknown_tool` 的 tool result给模型；不得执行任何 capability。
缺 call ID、非法 name、损坏 arguments或未闭合 delta才属于 protocol violation并进入有界 adapter retry。

### 7.3 Model edition 启动门禁

每个可由用户选择的 `model_editions.options[]` 必须在有效配置中声明并通过以下能力门禁：

| 能力 | 要求 |
|---|---|
| messages | `supports_messages=true` |
| roles | 至少支持 `system / user / assistant / tool`，Agent path禁止 role fallback |
| native tools | `supports_native_tool_calls=true` |
| forced first call | `supports_required_tool_choice=true` |
| streamed tool deltas | adapter能够闭合 call ID/name/arguments；不支持时只能使用同模型非流式 Agent sample，不得改用文本 JSON |

默认 model edition或任一仍公开在 `/model-editions` 的 option未通过时，Runtime启动 fail closed；不得启动后等到用户
请求才发现不支持。测试注入从 `main_agent_stream_generator` 迁移为 provider-neutral `agent_model_port` fake；旧
stream generator只保留给非 Agent text用途。

删除 `enable_llm_planner`、`planner_llm_config/path/client_factory`、`planner_reasoning_effort` 和
`runtime_replanner` 装配参数。`main_agent_llm_config` 成为 Agent controller、compaction及受控内部模型调用的唯一
模型注册来源；platform service若显式配置独立业务模型，仍不取得 Agent决策权。

### 7.4 文本与 tool calls 并存

System contract 要求模型在需要工具时只输出 tool calls，在完成时输出最终 assistant text。Runtime 仍必须防御
文本和 tool calls 同时出现：

- 完整 sample 先缓冲；
- 只要存在 tool call，assistant text 作为非用户可见 observation 持久化，不发布为最终回答；
- 只有无 tool calls 的非空 assistant text 才进入 final publisher。

这会保留最终回答的 SSE/event 合同，但最终文本在 sample 完整闭合后发布，避免中间文字污染聊天正文。

最终 sample闭合后不得再发生模型/Provider调用。FinalOutputPublisher通过现有 EventSink发布确定性
`main_agent.output_delta` chunks，并以第15节的原子 final commit闭合 `main_agent.output_final`。验收以 fake
model记录“最后一个 sample后没有第二次 model call”，并用
`agent_final_publish_delay_seconds` histogram观察真实延迟，不设缺乏基线的任意毫秒阈值。

## 8. Tool Catalog 与输入策略

### 8.1 请求可见能力

`AgentToolCatalogBuilder` 必须只消费 `CapabilityRegistry.list_for_request(request, public_only=True)`，继续遵守：

- 用户 MCP execution path；
- 当前用户可用 Server Profiles；
- capability enabled/public 状态；
- Skill bundle revision；
- 安全来源和请求可信 metadata。

`main_agent.respond` 不再注册为模型工具。最终回答由无 tool calls 的 assistant sample自然产生。

Skill的 `AgentToolDescriptor` 不得只使用 240字符 CapabilityDescriptor description。CatalogBuilder必须从 pinned
Skill bundle复用现有安全 `PublicSkillProfile`，把 display name、description、routing triggers/examples、输入 schema
摘要和 file-selection摘要压缩为 routing descriptor；禁止暴露 runtime、script、handler、路径、配置或 secret。
Delegated Skill只在模型实际调用后把同一安全公开投影扩展为 `skill_activation`，不能为所有 Skill预注入，也不能
读取或注入原始 `SkillManifest.body`、resource正文、脚本内容、内部路径或未通过 public sanitizer的 metadata。

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
| `can_suspend` | 是否可能产生 Interrupt 或 durable remote waiting |

所有策略默认 fail closed：无 policy 的 capability 不进入 Agent catalog。执行前再次按 policy 过滤/验证，防止模型
输出、prompt injection 或 provider bug 绕过 authority。

初始安全声明如下：

- `mcp.dispatch`：`parallel_safe=false`、`can_suspend=true`；自动模式 schema中的 `server_id` 必须是当前安全
  Server Profile ID enum，显式 Server ID 由系统注入且不暴露为模型可改字段；
- `python_subprocess/platform_service skill.*`：`parallel_safe=false`、`can_suspend=true`；模型只可提供
  `subtask_label/parent_question`提示，`effective_user_message`、artifact context、Skill bundle revision和用户身份由
  系统注入；Skill真实输入继续由现有 input resolution/slot contract解析；
- `delegated_main_agent skill.*`：路由到 DelegatedSkillActivationService，不路由到 SkillExecutor；
- 只有 Skill contract 后续显式声明并通过隔离、无共享写和多 Interrupt回归后才可设 `parallel_safe=true`；
- 不存在“因为模型同时返回多个 call 就默认并发”的隐式规则。

一轮多个非并发调用仍被接受，并按模型 call 顺序串行执行。

### 8.4 Catalog 预算门禁

全部当前请求可见 native Tool schemas都计入 PromptEnvelope的 non-history token preflight，先压缩 conversation/Agent
history，不能裁剪 Tool name、input schema或 authority enum。若 stable rules、完整可见 Tool catalog、当前用户输入和
最小安全 suffix仍无法放入所选 model edition的输入预算，Run在任何模型采样前以 fatal
`agent_tool_catalog_too_large`失败并记录 tool count/schema bytes/token estimate；不得静默省略 capability或退回文本
Planner。

本次不新增 lazy capability discovery。Outer catalog仍只暴露每个 public Skill和单一 `mcp.dispatch`，不会展开
Server内 Tool list；显式 Skill/MCP首轮只有一个 Tool，因此不受普通全 catalog容量影响。测试必须覆盖恰好可容纳、
超出一项、Skill热重载后超限和错误事件不泄漏 schema正文。

### 8.5 Delegated Skill activation

Instruction-only Skill按现有 Rust Skill contract默认 `mode=delegated_main_agent / answer_mode=direct`。统一 Loop
必须增加 `DelegatedSkillActivationService`：

1. 按 AgentRun pinned Skill bundle revision解析 capability ID；
2. 调用现有 `build_public_skill_profile`并复用 prompt sanitizer，只从 `PublicSkillProfile.to_dict()`中的 display name、
   description、routing triggers/examples、public usage、schema/file-selection summary和 `main_agent` audience的 resource
   index构造安全 activation profile；resource index只含公开 ID/title/description/audience，不加载 resource正文；
3. 持久化 `skill_activation` AgentItem，绑定 capability ID、Skill name、bundle revision、安全投影 digest和来源；
4. 在后续 sample中由 AgentContextBuilder把 activation渲染到动态 trusted capability-instruction segment；
5. 返回 completed tool result，供 Provider tool-call pairing使用；
6. 即使 delegated manifest意外包含 scripts，也不得运行脚本，保持现有行为；
7. Activation只在当前 AgentRun有效，不写入普通 conversation memory，也不能由 tool result文本伪造；
8. `SkillManifest.body`、resource正文和私有 metadata不属于本次 activation合同。未来若需要执行式 instructions，必须
   另行完成权限、prompt-injection和内容审核设计，不能扩大本合同。

`python_subprocess` 和 `platform_service` 继续由 SkillExecutor执行。三种 `answer_mode` 在 Agent Loop中的含义固定：

| answer_mode | 新语义 |
|---|---|
| `direct` | Skill response是权威 tool result；不直接写 assistant正文，仍由同一 Agent模型决定最终措辞 |
| `requires_finalizer` | 与 `direct` 一样回填 tool result；不再创建独立 finalizer node |
| `none` | Tool result可供后续决策，但不得被 Runtime直接提升为用户回答 |

因此 `answer_mode` 只控制 output projection/用户回答资格提示，不再控制 DAG节点生成。该用户可见变化属于已确认的
“全部走同一 Agent Loop、最终回答由同一模型生成”决策。

### 8.6 Pinned catalog

AgentRun创建时固定 Skill bundle revision和当前可用 MCP Server Profile identity；同一 Run后续 sample从固定 Skill
revision构造 catalog，不能因热重载切换 Skill代码。MCP Server availability和授权在每次真实 invocation前仍按当前
authority复验；失效 Server转成模型可见 tool error，不能继续使用创建 Run时的过期权限。

## 9. 统一路由语义

### 9.1 普通请求

首轮 catalog 包含当前请求可见的全部 capability，模型自由选择调用、继续调用或直接回答。

### 9.2 显式 Skill

显式 Skill 不走独立 workflow。AgentRun 保存可信 initial constraint：

- 第一轮 catalog 只包含指定 Skill；
- model request 使用 required tool choice；Provider 不支持或不遵守 required contract 时进入有界 protocol retry，
  耗尽后 fatal；
- 模型必须返回且只返回一个目标 Skill tool call；零个、多个或不同 name都属于 protocol violation，不能用提前
  assistant text绕过；
- tool result 写回后 constraint 消费，后续恢复普通 catalog；
- Skill 缺输入时暂停同一 tool call，用户回答后继续该 call，再回到循环。

### 9.3 显式 MCP Server

显式 MCP 同样不走独立 workflow：

- 第一轮 catalog 只包含 `mcp.dispatch`；
- model request 使用 required tool choice，且必须只返回一个 `mcp.dispatch` call，不能用提前 assistant text或
  同轮重复 dispatch绕过；
- `server_id` 由 trusted request metadata 注入，模型不可选择或覆盖；
- MCP Coordinator 继续负责 Server discovery、tools/list、Selector、逐 Tool authorization、Tool chain、MRTR 和
  remote Tasks；
- dispatch 结果写回后 initial constraint 消费，模型可以继续调用其他 capability 或输出最终回答。

### 9.4 自动 MCP

普通请求可由模型选择 `mcp.dispatch` 和当前安全 Server Profile。Outer Agent Loop 不加载 Server 内完整 Tool list；
MCP Coordinator 在 Server 被选择后按现有设计发现 Tool，并在内部 Selector 循环组装 Tool chain。这样保留现有
MCP 注意力和安全边界。

### 9.5 MCP 内部模型绑定

当前 MCPServerRouter/MCPToolSelector在启动期持有 `text_generator(prompt)`，调用时没有 Request或 model edition。
统一 Loop必须把它们改为显式接收 `AgentModelBinding`：

- `run_id`、model edition、thinking/reasoning options的安全只读快照；
- 从当前 SharedLLMRuntime派生的窄 `generate_text` adapter；
- 当前 Task/Conversation/Node关联 ID仅供事件关联，不进入 prompt正文；
- MCP Selector仍使用严格 JSON action schema和现有 repair次数，不改用 native outer capability catalog；
- Router、Selector、missing-input question和 Agent compaction都不得选择另一个 model edition；
- 删除“用户 MCP routing requires LLM planner”的启动条件，替换为“requires valid Agent model binding”。

`CapabilityExecutionRequest.metadata` 只携带安全 model binding引用/选项，不携带 client、API key或 Provider对象。
Coordinator调用 Router/Selector时逐次解析该 binding；恢复后的原 tool call继续使用 AgentRun固定模型。

## 10. Agent Loop 状态机

```text
claim AgentRun
  -> start lease heartbeat while actively executing
  -> append current user/continuation item if required
  -> build bounded context and tool catalog
  -> sample same model
  -> persist assistant sample
       -> tool calls present
            -> atomically persist all tool_call items and reserve result slots
            -> execute deterministic invocation waves
            -> atomically commit each call outcome to its reserved result slot
            -> renew claim and continue; release only when entering waiting
       -> no tool calls + non-empty assistant text
            -> publish final output
            -> complete AgentRun and Task
       -> structurally invalid/empty sample
            -> adapter protocol retry
            -> retry exhausted => fail
```

主循环无 iteration counter 和 turn budget。每次循环前后都检查 Task cancellation、AgentRun claim 和 storage
revision；所有 active await边界由第6.1节 heartbeat覆盖，lease lost必须在下一次副作用或 commit前被观察并 fail
closed。异步用户 steer 不作为首阶段功能；新的用户消息通过现有 conversation/task guard 开启新 Task，waiting
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

- 一个原子 `commit_agent_sample` 先创建所有 tool_call items、TaskNodes和预留 result slots；任何 capability都不得在
  该操作成功前启动；
- 按 call ordinal把 batch切成确定性 waves：连续 `parallel_safe` calls组成一个并发 wave，每个
  non-parallel-safe call单独组成一个 exclusive wave；
- waves严格按顺序执行；一个 wave 完成前不得启动下一 wave，不依赖 Python中不存在的标准 RwLock或第三方锁；
- 并发 wave使用 `asyncio.gather`/TaskGroup等现有标准库机制，单个普通失败不取消同 wave其他调用；fatal才取消；
- 每个结果完成后立即写入预留 result slot；ContextBuilder只在整个 batch闭合后按 call ordinal渲染 tool results；
- 一个 call 的普通失败不取消同批其他 call；
- fatal 控制面错误取消未启动 call，并终止 Run。

如果任一 call进入 waiting：

- 当前 wave中已经启动的调用继续闭合或进入各自 waiting；
- 后续 wave不启动，`next_batch_call_ordinal`指向第一个未启动 call；
- AgentRun记录全部 `waiting_call_item_ids`；
- 每次 continuation只闭合对应 call，其他 waiting保持；
- waiting集合清空后从 `next_batch_call_ordinal`继续剩余 waves；
- 整个 batch终结前不得再次请求模型。

### 11.3 原子 outcome commit

新增 Storage/Sidecar原子操作 `commit_agent_call_outcome`，在一个事务中验证并写入：

1. AgentRun claim token/revision和 active sample；
2. tool call、reserved result ID、TaskNode和 capability identity；
3. 预先安全 staged的 Artifact metadata/refs；
4. TaskNode terminal状态或 waiting状态；
5. closed tool_result AgentItem，或 open Interrupt/remote waiting authority；
6. durable低敏 Event records；
7. AgentRun waiting集合、next call ordinal和revision；
8. terminal时的 Task粗粒度状态投影。

Artifact文件正文必须先按现有安全 staging合同写入；事务只提交已经可复验的 metadata/ref。事务失败不得留下
Node terminal但缺 tool result的可见状态。Recovery对已有 capability terminal authority执行同一幂等 commit，
不从进程内 `output_payload`猜测结果。

### 11.4 Tool result envelope

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

以下结果把 AgentRun置为 `waiting_for_input` 或 `waiting_for_dependency`，Task继续投影为 `running`，并把对应
call加入 `waiting_call_item_ids`：

- Skill missing input；
- MCP approval；
- MCP elicitation/MRTR；
- MCP remote Task；
- 其他现有 Interrupt contract。

Waiting 时没有 tool_result terminal item，因为对应 tool call 尚未结束。
同一 batch允许多个 open Interrupt；现有 chat API在未指定 `metadata.interrupt_id` 且存在多个 open Interrupt时继续
明确报错，Frontend按 waiting event的 interrupt/node ID一次展示一个。回答一个 Interrupt后，如果等待集合仍非空，
Task保持 running/waiting UI并展示下一个；不得提前进行模型采样。

### 12.2 Continuation

用户回答、approval acceptance 或 remote completion 到达后：

1. 通过 owner、conversation、task、node、call 和 resume digest 校验；
2. claim 同一个 AgentRun；
3. 调用现有 capability-specific continuation authority 完成原 tool call；
4. append continuation item，并通过 `commit_agent_call_outcome` 写入唯一 tool_result；
5. 从 waiting集合移除对应 call；
6. 集合仍非空时继续等待；集合清空时恢复未启动 batch waves；
7. batch全部闭合后继续同一 Agent Loop。

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

1. 创建确定性 `agent.final_output` TaskNode并进入 running；
2. 从已持久化 final assistant item生成确定性 delta event IDs，发布缓冲文本；
3. 调用 Storage/Sidecar原子 `commit_agent_final_output`；
4. 在一个事务中验证 final item/run claim并提交 final text Artifact、`main_agent.output_final` durable event、
   assistant history Message、final publication receipt、final Node completed、AgentRun completed和 Task completed；
5. 保留安全 `main_agent.llm_call`/Agent sample audit metadata；
6. 事务成功后通过 EventSink发布 committed final/terminal events；
7. 重试使用相同 node/artifact/message/event IDs和 receipt，不能生成第二份最终内容。

如果进程在 delta发布后、final commit前退出，恢复者从同一 final assistant item和确定性 IDs重放；Frontend按 event
ID去重。`agent.final_output` 只是 artifact/event归属与原子发布账本，不在 Tool catalog中，不执行 LLM，也不属于被
删除的 DAG finalizer。

现有 capability-missing fallback 披露合同继续生效：可信 fallback metadata 由 AgentContextBuilder 注入，最终文本
必须经过现有 disclosure guard，并发出既有 frontend/history notice。删除 `main_agent.respond` 不得删除这项用户
可见事实披露或文件下载声明校验。

Thinking 继续只通过 transient channel 展示。带 tool calls 的 sample 文本不进入用户聊天正文。Frontend 可以继续显示
TaskNode/Skill/MCP progress，但不渲染 tool result 为 assistant answer。

### 15.1 Durable 事件与 transient reasoning

新增事件使用闭合名称：

| 事件 | 可见性 | 最小 payload |
|---|---|---|
| `agent.run.started` | audit | run/model option digests、routing mode |
| `agent.sample.started/completed` | audit | sample ID、tool count、usage、duration、outcome |
| `agent.tool_call.accepted` | audit | call ID、capability kind、ordinal、argument digest |
| `agent.tool_result.committed` | audit | call ID、status、error code、artifact count、result digest |
| `agent.run.waiting/resumed` | frontend | waiting reason kind、interrupt ID、remaining count |
| `agent.run.lease_lost` | audit | execution phase、lease revision、closed reason code；不得包含 token |
| `agent.context.compacted` | audit | covered range/digest、token counts、duration、outcome |
| `agent.run.completed/failed/cancelled` | frontend/audit | terminal outcome、sample/tool/compaction counts、duration |
| `agent.reasoning_delta` | transient frontend | delta、ordinal、sample ID；不得 durable persist content |

现有 `node.*`、MCP、Skill、`main_agent.output_delta/final` 和 capability fallback事件继续生效。
`planner.reasoning_delta`、`soft_skill.reasoning_delta` 不再由新任务产生；Frontend改为消费
`agent.reasoning_delta`。事件、audit和日志不得保存完整 prompt、assistant observation、tool arguments/result或
Skill instructions，只保存 closed code、计数、大小和 digest。

### 15.2 指标

新增低基数指标：

- `agent_runs_active`；
- `agent_runs_total{outcome}`；
- `agent_time_to_final_seconds{outcome}`；
- `agent_samples_total{outcome}` / `agent_sample_duration_seconds`；
- `agent_tool_calls_total{capability_kind,outcome}` / `agent_tool_call_duration_seconds{capability_kind}`；
- `agent_run_tool_calls`、`agent_run_samples` histogram，用于观察无 maxTurns后的分布而非终止任务；
- `agent_waiting_total{reason_kind}` / `agent_resume_total{outcome}`；
- `agent_lease_acquire_total{outcome}` / `agent_lease_renew_total{outcome}` / `agent_lease_lost_total{phase}`；
- `agent_lease_remaining_seconds` histogram，用于验证 heartbeat裕量，不作为续租调度器；
- `agent_compactions_total{outcome}` / `agent_compaction_duration_seconds`；
- `agent_aborted_calls_total{reason_kind}`；
- `agent_final_publish_delay_seconds`。

禁止使用 task ID、conversation ID、call ID、capability ID、model输出或用户标识作为 metric label。现有
BackpressureGuard的 30 active Task边界保持；waiting Task不占用正在执行的 model/capability worker slot，但仍受
Task级资源和MCP lease配额约束。

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
- TaskEdge storage/proto/runtime dependency；
- Task/TaskNode中的 `root_node_id`、`criticality`、`dependency_type`、`retry_policy`、`timeout_policy`、
  `resource_class` DAG字段；
- `enable_llm_planner`、`planner_llm_*`、`planner_reasoning_effort` 和 runtime replanner注入参数；
- Prompt中“自动 DAG / 上游 DAG节点”等旧控制面措辞。

允许保留与“planner”同名但服务于其他独立产品功能的代码，前提是引用审查证明它不再参与 Task 编排；否则应
重命名或删除，避免形成隐式第二控制面。

允许保留 `/graph` response class和 `task.graph_created` 事件名作为第17节定义的当前客户端兼容投影；它们不得
import TaskEdge、读取 WorkflowPlan或参与恢复。

本设计不提供旧 DAG Task reader、resume adapter、feature flag 或 fallback。切换前存在的旧 Task 不属于迁移或
验收范围。

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

`GET /api/v1/tasks/{task_id}/graph` 保留为当前客户端的只读调用账本兼容视图，不作为 DAG兼容恢复：

- `nodes` 来自 TaskNode invocation ledger；
- 已从数据库删除的 `criticality/dependency_type` 在 DTO层固定投影为 `required/hard`；
- `edges` 固定为空数组，route不得调用 `list_task_edges`；
- `task.graph_created` 继续在 AgentRun初始化时发出，payload固定 `edge_count=0`，供现有 frontend进入 running状态；
- `task.graph_updated` 和 Replanner事件不再由新任务产生；
- API文档明确该 endpoint是兼容读模型，不能推断未来执行顺序。

该 facade不读取旧 WorkflowPlan、不恢复旧 Task，也不阻止删除 TaskEdge storage/proto。后续若发布 breaking API
version，可另行把它重命名为 `/calls`；本设计不扩大到该用户可见 API迁移。

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
5. reserved result ID/sequence与 parent sample/call ordinal唯一一致；
6. waiting call集合与 open Interrupt/remote authority逐项一致；
7. completed run 恰有一个 final assistant message/publication receipt；
8. 单一 Task lease authority、active heartbeat、waiting release、失租 fail-closed和 revision共同防止双执行；
9. `commit_agent_sample`、`commit_agent_call_outcome`、waiting/terminal transition在三种 backend语义一致；
10. `commit_agent_final_output` 原子闭合 final node/artifact/event/message/run/task且幂等；
11. payload size、digest 和 closed kind 在数据库与 Rust 两侧一致。

PostgreSQL role/permission 边界沿用现有最小权限设计；Agent storage 不获得读取 MCP credential/raw result 的能力。

### 18.1 依赖与配置矩阵

| 依赖 | 当前入口 | 改造要求 |
|---|---|---|
| OpenAI-compatible Provider | `LLMClient` / `SharedLLMRuntime` | native tools、required choice、tool role门禁和 AgentModelPort fake |
| Skill runtime | SkillRuntimeState、SkillExecutor、public profile/sanitizer | pin revision；delegated activation只使用安全公开投影；区分 executable Skill |
| MCP dispatch | Gateway、Coordinator、Router、Selector、recovery worker | 保留协议安全；注入 Run-bound model binding；替换 continuation plan |
| Lifecycle | Task/TaskNode/Interrupt/CancellationService | 新增 AgentRun细粒度状态映射、原子 outcome和 final commit |
| Storage | SQLite、PostgreSQL、Runtime Sidecar | AgentRun/AgentItem、claim、reserved result、final receipt和删除全部 DAG字段/TaskEdge |
| Prompt/Memory | PromptEnvelope、conversation memory、artifact/tool result sanitizer | 移除 DAG措辞；加入 tool transcript、Skill activation、summary |
| API/Frontend | Task/SSE/interrupt/history/graph | 保留外部合同；新增 Agent events；graph变为 empty-edge call ledger |
| Observability | Event/Audit/MCP metric基础设施 | 新增第15节低敏 Agent事件和指标 |

不得新增第三方 Agent、图执行或异步读写锁依赖；使用现有 OpenAI SDK、asyncio、SQLAlchemy、PostgreSQL和 Rust
workspace能力。

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

### 19.1 Main 分支 clean cutover 门禁

本设计只授权 `main` 仓库实施，不授权 `prod` 部署。仓库内按两个不可混淆的数据库边界推进：

1. **可回滚 additive boundary**：新增 AgentRun/AgentItem表、proto、repository和 model adapter；旧 DAG仍只用于
   开发基线测试，所有新增 schema可由旧 binary忽略；
2. **Agent proof boundary**：所有自动、显式、multi-call、waiting、remote、compaction和 final测试在三种 storage
   backend通过；此时尚未删除 DAG表/字段；
3. **clean-cutover boundary**：同一个受审 checkpoint切换全部 API入口并删除 DAG源码/wiring；禁止 feature flag或
   请求级 fallback；
4. **destructive schema boundary**：在 clean-cutover测试和仓库外备份完成后，删除 TaskEdge表及 DAG-only字段，
   更新 PostgreSQL manifest、SQLite bootstrap、Rust proto/contract和 frontend DTO投影；
5. **final proof boundary**：运行第22节全量门禁和静态删除证明，更新 AGENTS/CHANGELOG。

每个边界都需要独立 Git checkpoint、`git diff --check`、聚焦测试和明确 not-tested项。不得把 additive schema存在
误称为 dual runtime；在 clean-cutover boundary后生产源码只有 Agent Loop入口。

### 19.2 回滚与恢复

| 失败时点 | 允许动作 | 数据边界 |
|---|---|---|
| additive/Agent proof前 | 回退代码和新增未使用 schema | 没有 Agent Task用户数据 |
| clean-cutover后、destructive schema前 | 回退到上一个 DAG checkpoint | cutover后新 Agent Task不承诺由旧代码恢复，可在开发环境清理 |
| destructive schema后 | 只能恢复 destructive boundary前的数据库/Sidecar备份并回退对应代码，或继续向前修复 | 不提供反向 schema猜测，不把新 AgentItems转换成 WorkflowPlan |
| final proof后 | 新问题按 Agent Loop forward fix处理 | `prod`仍未获授权，不执行生产回滚 |

备份必须覆盖 SQLite/PostgreSQL schema/data和 Runtime Sidecar持久化文件，存放于仓库外并验证可读；不得读取、
移动、跟踪或删除根目录 `docker_cmd.md`。因为用户已明确不考虑旧 DAG Task，回滚目标是恢复开发数据库和代码
一致性，不是迁移旧 Task。

### 19.3 风险与缓解

| 风险 | 影响 | 缓解/验收 |
|---|---|---|
| 无 maxTurns导致长时间/高成本 Run | 资源占用和用户等待 | 保留30 active Task backpressure、用户取消、上下文压缩和第15节分布指标；不暗中新增轮次终止 |
| Provider声明兼容但 tool delta不合规 | Run无法继续或错误调用 | 每个 model edition启动门禁、wire golden tests、protocol retry耗尽后 fatal |
| Public Skill catalog超过模型预算 | 错选能力或请求超限 | 完整 routing profile/tool schema参与 preflight；采样前 `agent_tool_catalog_too_large` fail closed，不静默省略 |
| Delegated Skill instruction权限过高 | Prompt injection或跨 Run污染 | 只从 pinned bundle生成既有 `PublicSkillProfile`安全投影；禁止 manifest/resource正文；不接受 tool文本伪造，不写普通 memory |
| 长模型/Tool调用跨过 lease TTL | 双 worker执行、重复副作用或旧 owner覆盖新状态 | 单一 Task lease authority；active heartbeat不晚于 TTL/3；全部 commit校验 token/revision；失租取消且 no-replay；可控时钟接管测试 |
| 并行能力产生共享副作用 | 数据竞争或重复操作 | 默认全部非并发；显式 parallel-safe contract、deterministic waves、no-replay恢复 |
| 多 Interrupt未被用户逐个处理 | Task长期 waiting | waiting集合、interrupt ID选择、Frontend逐个呈现、waiting duration指标 |
| Compaction遗漏关键结果 | 后续决策错误 | 原始 items不删、covered digest、summary golden/恢复测试；失败 fail loud |
| Final answer缓冲增加感知延迟 | 用户较晚看到正文 | 不进行第二模型调用，发布延迟 histogram和 fake model无额外 roundtrip验收 |
| Final publisher crash导致重复正文或无 Artifact producer | 历史重复/Task假完成 | deterministic `agent.final_output` IDs和原子 final commit；恢复重放与去重测试 |
| 删除 DAG schema后代码回滚失败 | 开发环境不可启动 | destructive boundary前仓库外备份；之后只允许成对恢复或 forward fix |
| `/graph` 名称与新语义不一致 | API消费者误认为存在未来 DAG | 文档标为 call-ledger兼容视图、edges固定空；后续 breaking API另案处理 |
| 大规模删除误伤 MCP/Skill安全链 | 回归或权限退化 | Invocation Kernel先锁定行为；MCP/Skill全量测试和静态引用审查 |

### 19.4 已确认假设与开放问题

已确认假设：

- 用户接受 clean cutover，不迁移或恢复旧 DAG Task；
- 所有执行入口统一 Agent Loop，显式调用只约束首次 tool call；
- Agent controller、MCP Router/Selector、compaction和最终回答固定同一 model edition；
- Agent Loop没有 maxTurns；现有 capability内部安全额度继续保留；
- Outer Agent只看到 `mcp.dispatch`，不预加载 Server内完整 Tool list；
- 最终回答可在完整 sample闭合后缓冲发布；
- `main`实现不等于 `prod`部署。

开放问题：无。若实施发现需要改变上述产品决策、API支持义务或安全风险容忍度，必须回到用户确认，不能在
implementation plan中静默选择。

## 20. 功能需求

| ID | 需求 |
|---|---|
| FR-1 | Cutover后的全部 Task执行/恢复入口进入 AgentLoopOrchestrator，不构建 WorkflowPlan |
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
| FR-15 | delegated_main_agent Skill通过可信 activation进入模型上下文，且不得运行脚本 |
| FR-16 | executable Skill注入可信 user/artifact/revision上下文，三种 answer_mode不再生成 finalizer |
| FR-17 | Sample、tool calls和 reserved result slots先于副作用原子持久化；每个 outcome原子提交 |
| FR-18 | 一个 batch支持零到多个 waiting calls，全部闭合后才恢复剩余 waves或模型采样 |
| FR-19 | 每个可选 model edition在启动时通过 native tools/role/required-choice门禁 |
| FR-20 | MCP Router/Selector及恢复路径使用当前 AgentRun固定 model binding |
| FR-21 | `/graph` 仅返回 empty-edge invocation ledger兼容投影，不读取 TaskEdge |
| FR-22 | Agent durable事件、transient reasoning和低基数指标符合第15节合同 |
| FR-23 | `agent.final_output` internal node和原子 final commit提供唯一 Artifact producer与终态 |
| FR-24 | Skill Tool routing descriptor使用安全 public profile，完整 catalog执行 token preflight且不静默省略 |
| FR-25 | 删除未生效的 TaskNode timeout/resource DAG字段，只保留 capability内部真实超时 |
| FR-26 | AgentRun复用单一 Task lease authority，active阶段持续续租，waiting释放，失租后禁止提交并安全接管 |

## 21. 非功能需求

| 维度 | 要求 |
|---|---|
| 一致性 | 单一 Task lease authority、AgentRun claim、sample/result reservation、call/outcome pairing、waiting集合和 final publication满足第18节跨 backend不变量 |
| 安全 | 模型只见请求可见 capability；参数 fail closed；system authority覆盖模型字段；delegated activation只来自 pinned bundle的安全 PublicSkillProfile投影，不读取 manifest/resource正文 |
| 恢复 | durable capability continuation恢复；不确定副作用不自动重放；多 waiting逐项闭合；lease lost禁止旧 owner提交并允许 TTL后接管 |
| 可观测 | 第15节事件/指标全部实现；hidden内容不进入 event/audit/metric label |
| 上下文 | hidden reasoning/raw MCP/upload body不进入 AgentItems；canonical payload最多131,072 bytes；原始 items压缩后保留 |
| Tool catalog | Skill routing信息来自 pinned安全 public profile；完整 native schema计入 preflight；不适配时在采样前 fail closed |
| 性能 | 确定性 waves只并发明确安全能力；lease heartbeat间隔不晚于 TTL/3且无 busy polling；waiting释放lease且不占 model/capability worker；保留30 active Task backpressure |
| 最终输出 | Sample闭合后不进行第二模型调用，立即通过既有 delta/final合同发布并观察 publish delay |
| Provider | 每个公开 model edition启动期通过 messages/tool roles/native tools/required choice门禁，Run内不得换模型 |
| 兼容 | Task/SSE/interrupt/history保持；`/graph`按第17节固定兼容投影；只放弃已明确排除的旧 DAG Task恢复 |
| 可维护 | ApiRuntime不承载模型循环细节；invocation lifecycle和 atomic outcome commit只有一个实现；不新增第三方 Agent/图/锁框架 |
| 可访问性 | 不新增交互；若修改现有 approval/interrupt组件则必须保持对应焦点、键盘和语义测试 |

## 22. 测试与验收

### 22.1 Model adapter

- 单 tool call、多个 tool calls、分片 arguments；
- 最终 assistant text；
- text + tool calls 防御；
- malformed JSON、重复 ID、非法 name、缺失 ID进入 protocol retry；
- 结构合法但不在 catalog的 name生成 `unknown_tool` result且不执行 capability；
- Provider 不支持 tool calls；
- messages disabled、缺 assistant/tool role、required tool choice不支持时启动失败；
- 每个 model edition独立门禁且 `/model-editions` 不公开不合格 option；
- cancellation、transport retry 和 usage metadata；
- model edition 在 loop/compaction 中一致。

### 22.2 Agent Loop

- `tool_call -> tool_result -> next sample -> final`；
- 连续链式 capability 调用；
- 一轮多 call，串行与 parallel-safe gate；
- deterministic waves不越过 exclusive call；
- tool results 按 call 顺序回填；
- call outcome按完成顺序原子写入预留 result slot，重启后可重建原 Provider顺序；
- 普通失败后模型改用其他 capability；
- 超过旧 maxTurns/max replans 数值后仍继续并完成；
- 空/损坏 sample 不误完成；
- final answer 唯一且没有第二 LLM call；
- compaction 后继续并使用 summary + suffix。

### 22.3 Skill 模式

- instruction-only Skill默认 delegated mode并成功生成 `skill_activation`；
- delegated activation只包含 pinned revision、`PublicSkillProfile`安全投影及其 digest，不读取 manifest body/resource正文，
  不运行 manifest scripts；
- 用户/tool result不能伪造 privileged activation；
- python_subprocess/platform_service继续由 SkillExecutor执行；
- user message、artifact context、bundle revision由系统注入，模型不能覆盖；
- `direct/requires_finalizer/none` 均不创建第二 finalizer，只有同一 Agent模型发布最终回答；
- Skill热重载不改变运行中 AgentRun的 pinned revision。

### 22.4 显式路由

- 显式 Skill 第一轮只能调用目标 Skill；
- 显式 MCP 第一轮只能调用 pinned `mcp.dispatch` 且 server ID 不可覆盖；
- constraint 完成后 catalog 恢复；
- 普通请求 capability visibility 与当前 registry相同；
- 用户 MCP unavailable 时不回退 legacy MCP。

- required tool choice返回零个、多个或错误 tool name时协议重试，耗尽后 fatal；
- 自动 `mcp.dispatch.server_id` schema只包含当前安全 Server Profile IDs。

### 22.5 Continuation 与恢复

- Skill missing input 回到原 call；
- MCP approval/MRTR 回到原 call；
- remote Task completion 追加唯一 tool result 后继续；
- 同一 batch两个 waiting calls可分别回答；第一个回答后不采样，全部闭合后恢复剩余 wave；
- crash 后已有 authority result补写；
- unknown side-effect call补 aborted 且不重放；
- duplicate wakeup/approval/outbox 幂等；
- 双 worker claim/CAS竞态；
- 可控时钟覆盖 active model sample、compaction、capability wave和 final publish跨过初始 TTL且 heartbeat持续续租；
- renew在到期前持续失败时旧 worker取消本地工作，stale token不能提交，TTL后新 worker可接管；
- waiting transition先提交 authority再释放lease，waiting期间不续租，continuation重新 acquire后才恢复；
- lease lost时晚到 capability结果按 authority/no-replay规则处理，不被旧 worker写入或自动重放；
- terminal commit清空claim；异常退出依赖TTL接管；release cleanup失败不反转已提交终态；
- cancel 与 completion 线性化。

### 22.6 MCP 同模型与内部安全

- Router/Selector收到 AgentRun model binding并使用同一 model edition；
- approval或remote恢复后仍使用原 Run model binding；
- 删除 planner config后 user MCP wiring仍可启动；
- MCP discovery、failed/rejected fingerprint、内部 call budget、Result Parser和 no-replay测试保持；
- Outer Agent上下文不包含完整 Server Tool list或 raw result。

### 22.7 Storage

- SQLite/PostgreSQL/Rust Sidecar AgentRun/AgentItem parity；
- sequence、digest、payload上限、kind closure；
- CP7-derived canonical JSON Python/Rust golden vectors和 131,072-byte边界；
- `commit_agent_sample` 与 `commit_agent_call_outcome` crash/fault injection；
- AgentRun/Task/Node/Interrupt状态映射和多 waiting集合；
- SQLite/PostgreSQL/Rust Sidecar的单一 Task lease acquire/renew/release、token rotation、expiry takeover和stale commit parity；
- call/result foreign identity；
- terminal result唯一；
- final publication唯一；
- `agent.final_output` node、Artifact producer、Message/event/receipt和 Run/Task终态原子一致；
- migration、manifest、权限和 rollback安全。

### 22.8 API、Frontend、事件和指标

- `/graph` 返回 invocation nodes、固定 `required/hard`、`edges=[]`，且不调用 TaskEdge repository；
- `task.graph_created(edge_count=0)` 继续驱动现有 frontend running状态；
- `agent.reasoning_delta`替代 planner/soft-skill reasoning并保持 transient；
- main-agent final delta/final、history、fallback disclosure和下载声明约束保持；
- 第15节每个 event/metric有 contract测试，禁止高基数/敏感 label和 durable reasoning content；
- final sample后没有第二模型调用，并记录 publish delay。
- final publish crash后使用确定性 IDs重放且不重复 Artifact/Message；

### 22.9 现有回归

- Skill capability、input resolution、artifacts；
- MCP ordinary/approval/MRTR/remote/recovery/result parsing；
- cancel/interrupt/mailbox/task lifecycle；
- API/SSE/history/frontend；
- prompt envelope、memory、model reasoning options；
- Rust quality gates和contract tests。

### 22.10 删除证明

最终静态检查要求 `src/` 中不存在运行时引用：

- `WorkflowPlan` / `WorkflowNodePlan`；
- `RuntimeReplanner`；
- `max_replans` / `max_dynamic_nodes`；
- workflow provider/router/expander/validator；
- `main_agent.respond` 作为 finalizer；
- `mcp_remote_task_continuation_plan`；
- TaskEdge storage/proto/dependency scheduling；
- DAG-only Task/TaskNode字段；
- planner LLM config/wiring和 DAG Prompt措辞。

允许测试 fixture 或迁移文件引用已删除数据库列/表名时，必须有明确 migration-test 语义，不能被生产代码 import。

### 22.11 FR—验收—测试追踪

| 需求 | 验收标准 | 主要测试层 |
|---|---|---|
| FR-1、FR-14 | 所有入口不构建 Plan；删除证明为零生产引用 | API assembly、静态扫描、完整回归 |
| FR-2、FR-19、FR-20 | Run/Selector/compaction/final model edition相同；不合格 edition启动失败 | LLM integration、MCP wiring、API model-edition |
| FR-3、FR-10、FR-17、FR-18 | multi-call reserved result、deterministic waves、多 waiting和结果重建正确 | Agent Loop unit、storage fault injection、API interrupt E2E |
| FR-4、FR-9 | 普通失败/aborted回填模型；不自动重放 unknown side effect | invocation unit、restart recovery、MCP no-replay |
| FR-5、FR-6、FR-11、FR-12 | 无 tool call才完成；超过旧预算继续；compaction继续；final唯一 | Agent Loop unit、long trajectory、history/API |
| FR-7 | 显式 Skill/MCP恰好一次 forced first call，随后恢复 catalog | API explicit route、model protocol violation tests |
| FR-8 | approval、missing input、MRTR、remote结果回到原 call/Run | lifecycle、MCP integration、API E2E |
| FR-13 | MCP discovery/Selector/authorization/result parsing行为不退化 | 现有 MCP完整回归和真实隔离 smoke授权门禁 |
| FR-15、FR-16 | delegated activation与 executable Skill模式/answer mode闭合 | Skill unit、capability、API dynamic reload/slot tests |
| FR-21 | `/graph` 是 empty-edge ledger且不依赖 TaskEdge storage | API DTO、frontend restore、repository call spy |
| FR-22 | Agent事件/指标齐全且低敏，reasoning只 transient | event contract、observability、leak scan |
| FR-23 | final item产生唯一 internal node、Artifact、Message、final event和 terminal receipt | final publisher fault injection、history/API、Sidecar parity |
| FR-24 | Skill routing profile可选中正确能力；catalog超预算采样前失败且不省略 | tool catalog unit、PromptEnvelope preflight、dynamic reload |
| FR-25 | Task/TaskNode contract和三种 storage不再含装饰性 DAG timeout/resource字段 | schema/proto/static scan、capability内部 timeout回归 |
| FR-26 | active长调用持续续租；waiting释放；失租旧 owner不能提交；TTL后唯一新 owner接管 | fake-clock Agent Loop、storage CAS/Sidecar parity、fault injection |

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
10. delegated Skill activation、executable Skill和三种 answer mode回归通过；
11. multi-call原子 outcome、多 waiting、crash/no-replay和 deterministic waves证明通过；
12. 所有公开 model editions通过 native tool contract且 MCP内部模型绑定一致；
13. `/graph` empty-edge兼容投影、Agent events/metrics和 frontend恢复通过；
14. `agent.final_output` 原子发布、Artifact producer、crash重放和唯一 history通过；
15. Skill public routing catalog和全 catalog token preflight通过，超限不进行模型采样；
16. Task/TaskNode/storage/proto中全部 DAG-only timeout/resource字段删除，capability内部 timeout回归通过；
17. destructive schema boundary备份/恢复命令在受控开发环境验证；
18. 未把本地仓库实现误称为 `prod` 部署或外部环境验证；
19. 单一 Task lease在三种 backend完成active heartbeat、waiting release、失租fail-closed、TTL接管和terminal cleanup证明。

## 24. 已接受的取舍

- 接受 clean cutover，不为旧 DAG Task 保留恢复能力；
- 接受为正确暂停/恢复新增 AgentRun/AgentItem storage contract；
- 接受最终文本在完整 sample 闭合后再发布，以防 tool-call sample 的中间文字污染聊天；
- 初始所有现有 capability 默认非并发安全，先保证正确性；
- 接受 instruction-only delegated Skill作为可信 activation tool，且 direct Skill结果仍由同一 Agent模型生成最终措辞；
- Delegated activation只使用现有安全 `PublicSkillProfile`投影；不把 manifest body或resource正文提升为模型可信指令；
- 接受本阶段不实现 lazy capability discovery；完整 public catalog不适配所选模型时在采样前明确失败，不静默省略；
- 保留 MCP 内部 Selector Tool chain，而不是把完整 MCP Tool list一次性暴露给 outer Agent；
- 保留 `/graph` 名称作为当前客户端 empty-edge invocation-ledger兼容视图，不保留 DAG执行或恢复；
- 不通过固定轮次上限防无限循环，依赖模型自然终止、用户取消、fatal控制面和上下文压缩。

## 25. 后续实施计划要求

本设计批准后，实施计划必须：

- 按可独立验证的 checkpoint 拆分；
- 每个 checkpoint 指定测试、删除范围和回滚点；
- 先建立 storage/model contract，再切换恢复路径；
- 单独规划 delegated activation、Run-bound MCP model binding、atomic sample/outcome、多 waiting状态机和单一 Task lease heartbeat；
- 单独规划 `agent.final_output` 原子发布、Tool catalog preflight和 DAG-only TaskNode字段删除；
- 在删除旧 DAG 前完成所有 Agent Loop 路径证明；
- destructive schema checkpoint前生成并验证仓库外备份，计划中写明不可逆回滚边界；
- 最终不保留 dual-runtime feature flag；
- 对 `docker_cmd.md` 保持绝对保护，不读取、不跟踪、不删除；
- 大规模修改按仓库规则创建清晰 Git checkpoint。
