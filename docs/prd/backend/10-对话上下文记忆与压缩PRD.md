# 对话上下文记忆与压缩 PRD

- **范围**：后端 / 对话记忆 / LLM 上下文工程 / 压缩策略
- **文档状态**：草案（用于后续实现规划）
- **日期**：2026-05-07
- **关联模块**：`src/api/`、`src/orchestration/`、`src/capabilities/main_agent/`、`src/storage/`、`src/integrations/token_counter.py`

## 1. 背景

当前系统已经具备会话、消息、任务、事件、artifact 与历史会话列表等基础能力。用户每轮消息会持久化为 `Message`，任务完成后也会把主代理最终文本回复持久化为 assistant history message。

但当前主代理 LLM prompt 主要使用：
- 当前用户问题；
- 上传 artifact 的脱敏 metadata；
- 上游 capability 已完成结果的 allowlist 上下文；
- Skill 匹配与脚本输出。

系统尚未把同一 conversation 的历史对话稳定注入 LLM Planner 与 `main_agent.respond`。因此，多轮追问、指代、省略主语、纠错和延续式对话容易退化为“单轮问答”，也缺少统一的上下文预算与压缩策略。

本 PRD 定义 v1 的**会话延续型记忆**与两级 compression engineering 目标，作为后续实现与验收依据。

## 2. 目标

1. **有记忆的自然对话**：同一 `conversation_id` 内，智能体在规划与回答时可读取历史上下文，使用户可以自然追问，例如“那它的基因型呢”“继续查这个品种的审定信息”“换成龙粳18”。
2. **规划阶段可用记忆**：LLM Planner / 自动规划前应获得受控历史上下文，避免省略主语的追问无法正确路由到 SQLQuery 或主代理。
3. **最终回答可用记忆**：`main_agent.respond` 应获得同一份对话记忆上下文，用于生成连贯、可承接前文的回答。
4. **上下文预算可控**：当历史消息、artifact 摘要或 capability 结果过长时，按分级压缩策略控制进入 LLM 的 token 规模。
5. **安全与隔离**：记忆仅在当前 account / conversation 授权范围内使用，不跨用户、跨 conversation 泄漏；不把 SQL、guard token、完整 rows、完整 prompt 等敏感或高成本内容注入通用记忆。

## 3. 非目标

- 不实现跨 conversation 的长期用户画像、长期偏好记忆或知识沉淀。
- 不引入向量数据库、RAG 召回或跨任务知识库。
- 不让 SQLQuery 内部 `sql_generate` / `result_filtering` 等专用节点直接消费完整对话记忆；SQLQuery 内部仍以明确问题与 schema context 为主。
- 不把 capability 原始中间产物、SQL、schema DDL、guard pass token、完整数据库 rows 或 provider prompt 记录到 audit。
- 不改变现有前端历史会话 API 契约；v1 默认是后端内部上下文工程能力。

## 4. 用户场景

### 4.1 多轮业务追问

用户：查一下龙粳33的品种信息。

助手：返回审定信息与基因型概要。

用户：那它的基因型数据库里有什么？

系统应能从同一 conversation 的历史中识别“它”指代“龙粳33”，并在规划阶段路由到 SQLQuery 基因型相关查询，最终回答时承接上一轮结果。

### 4.2 纠错与替换

用户：查龙粳33。

用户：不是这个，换成龙粳18。

系统应把“换成”理解为对上一轮查询对象的替换，而不是孤立问题。

### 4.3 长会话压缩

同一 conversation 经过多轮 SQLQuery、主代理总结与用户追问后，历史消息与能力结果超过 LLM 上下文预算。系统应先删除 capability 业务中间产物，再对更早对话进行摘要压缩，同时保留最近若干轮原文消息。

## 5. 范围与注入位置

v1 记忆上下文注入范围为：

1. **LLM Planner / 自动规划阶段**
   - 用于理解追问、省略、纠错与上下文依赖。
   - Planner 仍只能输出 public capability DAG，不因记忆上下文获得 internal capability 权限。
   - 记忆上下文 / effective question 的构建必须发生在 workflow provider 构建 plan 之前；不仅 LLM Planner prompt 可读取该上下文，LLM Planner 失败、禁用或输出非法时的 deterministic `AutoWorkflowProvider` fallback 也必须使用同一份系统侧补全后的 `resolved_user_message`，避免 fallback 路径重新退化为单轮问题判断。

2. **`main_agent.respond` 最终回答阶段**
   - 用于自然语言回答的承接、消歧与上下文一致性。
   - 与现有上传 artifact context、Skill context、上游 dependency context 合并，但必须保持边界标注。

v1 暂不把完整 conversation memory 直接注入 SQLQuery 内部 LLM 节点。若 SQLQuery 需要上下文补全，不应依赖 LLM Planner 在 `input_payload` 中自由改写 `user_question`；当前 planner payload policy 会 fail-closed，且 public `sql_query.query` 的可信 `user_question` 由系统 payload 填充。后续实现应由系统侧 memory builder / orchestration 层基于 `ConversationMemoryContext` 生成受控的 `resolved_user_message` / effective question，同时保留原始当前用户问题；Planner prompt 可读取 memory context 辅助选择 public capability，public capability 的 `user_question` 则应由系统可信字段填充，确保“那它的基因型呢”这类追问在进入 SQLQuery 内部 workflow 前已经被系统补全为明确问题。

Conversation memory 默认不得通过 `request.metadata` 原样透传给 Skill 自动脚本。若运行时需要在 metadata 中携带 memory 相关信息，也必须在执行 Skill script 前剥离或替换为脚本专用 allowlist；v1 Skill script 只接收当前轮 query、显式上传 artifact 和既有脚本输入契约，不读取完整 conversation memory。

## 6. 记忆上下文模型

后续实现应引入内部模型 `ConversationMemoryContext`，作为 prompt-safe、可审计的运行时上下文对象。建议字段：

| 字段 | 含义 |
|---|---|
| `conversation_id` | 当前会话 ID |
| `root_message_id` | 当前任务根用户消息 ID，用于从历史 memory 中排除当前问题 |
| `source_message_count` | 参与构建的原始消息数量 |
| `current_user_message` | 当前用户原始问题，仅作为独立 current 区块注入，不进入历史摘要 |
| `resolved_user_message` | 系统基于 memory 补全后的 effective question，可为空；用于 Planner / public capability 的可信系统输入 |
| `recent_messages` | 保留原文的最近用户 / 助手消息，按时间升序 |
| `clarification_messages` | 当前任务或历史业务轮次中的 interrupt answer / resume 补充消息，需保留与 task / interrupt 的关联 |
| `history_summary` | Level 2 生成的较早历史摘要，可为空 |
| `capability_summaries` | 已完成 capability 的安全摘要，不包含原始中间产物 |
| `compression_level` | `none` / `level_1` / `level_2` / `fallback` |
| `token_budget` | 当前记忆上下文预算 |
| `estimated_tokens_before` | 压缩前估算 token 数 |
| `estimated_tokens_after` | 压缩后估算 token 数 |
| `truncated` | 是否仍有内容被舍弃 |
| `fallback_reason` | 压缩或摘要失败原因，可为空 |
| `resolution_metadata` | 记录是否发生补全、补全依据类别、置信度 / fallback 原因等安全 metadata，不直接作为业务事实注入 LLM |

该模型属于运行时 / orchestration / capability 之间的内部契约，不要求直接暴露给前端。

`resolved_user_message` 不得覆盖 `current_user_message`；prompt 中必须显式标注“原始当前问题”和“系统根据历史补全后的问题”，避免模型把补全文本误认为用户逐字原话。

## 7. 上下文构建规则

### 7.1 数据来源

允许读取：
- 当前 `conversation_id` 下的 user / assistant `Message`；
- 已完成任务的主代理最终文本 artifact / assistant history message；
- capability 输出中已经过 allowlist 的摘要字段，例如 `summary`、`response_text`、`route_id`、`row_count`、`highlights`、`caveats`。

读取主代理最终回答时必须按 `task_id` 去重：若同一任务已经存在 assistant history message，则优先使用该 message 作为对话主线，不再重复读取同一 `task_id` 的最终 TEXT artifact；只有历史 assistant message 缺失时，才允许回退读取主代理最终 TEXT artifact。

Conversation memory 必须使用独立的 `ConversationMemorySafeAllowlist`，不得直接复用 `main_agent.respond` 的 dependency context allowlist。memory allowlist 默认禁止 `rows`、`candidate_rows`、`storage_ref`、`sql`、`schema_ddl`、`guard_token` 等高成本或敏感字段；如确需保留表格信息，只能保留 capped preview 的统计性摘要，例如 `row_count`、`preview_row_count`、`columns` 的受限列表、`highlights`、`caveats`，且必须记录 `truncated=true`。

v1 conversation memory 只记住历史轮次中“用户曾上传过文件及其脱敏摘要 / `upload_id` / 文件名 / preview 等安全 metadata”，不保证跨轮可重新读取原始文件内容。若用户后续追问“继续用刚才那个文件”，系统可基于记忆提示曾有相关上传摘要；只有当上传记录仍在 `InMemoryUploadStore` 中且当前用户 / conversation 校验通过时，才可继续通过显式 upload reference 使用原始内容；若上传已过期或被删除，应要求用户重新上传。完整文件内容不得写入 conversation memory 或摘要快照。

禁止读取或注入：
- SQLQuery 原始 SQL、guard token、schema DDL、完整 rows、完整 candidate rows；
- 本地文件路径、`storage_ref` 中的大对象正文、API key、base_url、provider 原始 prompt；
- 其他 account 或其他 conversation 的消息、任务、artifact。

### 7.2 顺序与角色

- 记忆上下文必须保持消息时间顺序。
- Prompt 中必须显式区分：历史摘要、最近原文消息、当前用户问题、上游能力结果。
- 历史摘要必须标注“这是系统生成的较早对话摘要，不是逐字原文”。
- 当前用户问题始终保留最高新鲜度，不得被摘要覆盖或改写掉。
- 构建 `ConversationMemoryContext` 时必须排除当前任务的 `root_message_id` 对应 user message；当前用户问题只能出现在独立的“当前用户问题 / current_user_message”区块中，不得同时作为 `recent_messages` 或 `history_summary` 的一部分重复注入。若 interrupt answer / resume 场景生成了补充消息，应按澄清消息规则单独标注，避免覆盖原始 root message。

### 7.3 澄清 / interrupt answer 消息规则

当用户通过 interrupt answer / resume 机制补充信息时，系统可能会持久化额外的 user message。此类消息不应被当作新的独立业务轮次处理，而应在 `ConversationMemoryContext` 中标注为当前任务的 clarification message：

- 保留其与原始 `root_message_id` / `task_id` / `interrupt_id` 的关联；
- 在 prompt 中标注为“用户对上一问题的补充信息”，而不是新的当前问题；
- resume 执行当前任务时，可把 clarification 与 root message 合成为本轮 effective question；
- 后续新任务构建 memory 时，可把已完成任务的 root message、clarification 和 assistant answer 作为同一业务轮次摘要；
- 摘要压缩时不得让 clarification 覆盖原始 root message，也不得把它提升为新的当前用户问题。

### 7.4 与现有上下文的合并顺序

推荐 prompt 结构顺序：
1. system / 主代理行为约束；
2. 对话记忆上下文；
3. 上传 artifact 脱敏上下文；
4. 上游能力结果上下文；
5. Skill 指令与脚本输出；
6. 当前用户问题区块：必须包含 `current_user_message` 用户原文；若存在 `resolved_user_message`，则以“系统根据历史补全后的 effective question”单独标注展示。Planner / public capability 路由可优先使用 `resolved_user_message` 作为可信系统输入，但最终回答应保留用户原文边界，避免把补全文本说成用户逐字表达。

Planner prompt 可使用更短模板，但必须保留 `current_user_message` 与 `resolved_user_message` 的边界；deterministic `AutoWorkflowProvider` fallback 可直接消费系统侧 `resolved_user_message`。

## 8. 两级压缩策略

### 8.1 触发条件

系统应基于 token 估算判断是否压缩。token 估算优先复用 `src/integrations/token_counter.py`，配置来源遵守现有 runtime 约定：启动期读取配置并写入环境，业务节点执行阶段不得重复读取 `config.yaml`。

建议配置项：
- `conversation_memory_max_tokens`：本轮上下文工程的总 token 上限来源；该逻辑值不新增独立 YAML 配置，默认取启动配置 `config.yaml` 中的 `trim_max_tokens`。API runtime / smoke 初始化期间已通过 `bootstrap_config_env()` 将该字段写入进程环境变量 `MAF_CONFIG_TRIM_MAX_TOKENS`，因此运行期应从环境中的 `trim_max_tokens` 配置值读取，不得在业务节点执行阶段重复读取 `config.yaml`。实际可用于对话记忆的预算不是直接用满该值，而应先为 system prompt、当前用户问题、上传摘要、上游能力结果、Skill 上下文和模型输出预留空间，再使用剩余预算构建 / 压缩 `ConversationMemoryContext`；
- `conversation_memory_recent_turns`：始终保留原文的最近业务轮数；v1 代码默认保留 6 轮，一个业务轮次由 root user message、clarification messages 和对应 assistant answer 组成。可通过显式 runtime 参数或已 bootstrap 的环境变量 `MAF_CONFIG_CONVERSATION_MEMORY_RECENT_TURNS` 覆盖，缺省不要求写入 `config.yaml`；
- `conversation_memory_summary_max_tokens`：Level 2 摘要目标预算；v1 不要求单独配置，默认按实际 memory 可用预算派生：`min(4096, max(1024, actual_memory_budget // 4))`，且不得超过实际 memory 可用预算；如后续通过显式 runtime 参数或已 bootstrap 环境变量覆盖，仍必须 clamp 到实际 memory 可用预算并记录 metadata；
- `conversation_memory_enable_summary_llm`：是否启用 LLM 摘要压缩；v1 默认启用，前提是主代理 LLM runtime 可用。可通过显式 runtime 参数或已 bootstrap 的环境变量 `MAF_CONFIG_CONVERSATION_MEMORY_ENABLE_SUMMARY_LLM=false` 禁用；自动化测试仍必须使用 fake / injected LLM，不访问真实 provider。

除 `conversation_memory_max_tokens` 固定取启动期已写入环境的 `trim_max_tokens` 外，其余可选配置的优先级为：显式 runtime 参数 / config dict > 已 bootstrap 的环境变量 > 代码内受测试覆盖的默认值；业务节点执行阶段不得为这些配置重复读取 `config.yaml`。若 `conversation_memory_enable_summary_llm=false` 且 Level 1 后仍超预算，系统不得调用 LLM 摘要，应 fallback 到“最近消息 + Level 1 安全摘要 + 保守截断”，记录 `compression_level=fallback` 与 `fallback_reason=summary_llm_disabled`，不得阻塞用户请求。

默认预算应小于 provider 上下文窗口，给 system prompt、当前问题、上游结果和模型输出预留空间。

### 8.2 Level 0：无需压缩

当完整安全记忆上下文低于预算时：
- 保留最近消息原文；
- 保留已存在的历史摘要；
- 保留 capability 安全摘要；
- `compression_level = none`。

### 8.3 Level 1：删除 capability 业务中间产物

当 Level 0 超过预算时，先执行 Level 1：
- 删除 capability 内部业务中间产物；
- 删除重复、低价值、过长的 capability payload；
- 仅保留最终回答、用户消息、必要的 capability 结果摘要与少量可解释 metadata；
- 不调用 LLM，不产生新的摘要。

Level 1 的核心目标是先移除“机器过程噪声”，保留人与助手的语义对话主线。

### 8.4 Level 2：历史对话摘要压缩

当 Level 1 后仍超过预算时，执行 Level 2：
- 保留最近 `conversation_memory_recent_turns` 轮原文消息；
- 将更早的用户 / 助手历史压缩为 `history_summary`；
- 摘要应保留：用户目标、已确认实体、关键约束、已给出的结论、未完成事项、用户纠正过的信息；
- 摘要不得引入未出现过的新事实；
- 摘要失败时 fallback 到“最近消息 + Level 1 安全摘要”，不得阻塞用户请求。

Level 2 可通过主代理 LLM runtime 的非流式调用完成，建议固定低成本 reasoning / thinking 设置；摘要调用必须记录安全 audit metadata，但不得记录完整 prompt。

## 9. 摘要持久化

为避免每轮重复压缩全量历史，后续实现应支持 conversation 级摘要快照：

- 摘要与 `conversation_id`、覆盖到的最后一个 `message_id` / `created_at` 绑定；
- 新消息到来后，只对“上次摘要之后且不属于 recent window 的较早消息”做增量合并；
- 用户手动删除 conversation 时同步删除摘要；
- SQLite 与未来 PostgreSQL 逻辑同构，可先以独立表或受控 JSON 字段落地；
- 摘要属于 conversation 私有状态，不跨 account 复用。

建议摘要快照字段：
- `summary_id`
- `conversation_id`
- `account_id`
- `covered_until_message_id`
- `covered_until_created_at`
- `summary_text`
- `source_message_count`
- `source_message_ids_hash`
- `estimated_tokens`
- `summary_version`
- `compression_policy_version`
- `model_metadata_safe`
- `last_error`
- `created_at`
- `updated_at`

`model_metadata_safe` 只能保存 provider、model、reasoning_effort、thinking_enabled、duration_ms、fallback 状态等非敏感信息，不得保存 API key、base_url、完整 prompt 或原始消息正文。摘要快照字段中的 `summary_id`、`account_id`、`source_message_ids_hash`、`summary_version`、`compression_policy_version`、`model_metadata_safe`、`last_error`、`created_at`、`updated_at` 等仅用于后端持久化、失效判断、审计与排障；正常 Planner / `main_agent.respond` prompt 只允许注入 `summary_text` 经过边界标注后的 `history_summary`，不得把这些存储 metadata 传入 LLM。

## 10. 安全、权限与审计

- 构建记忆前必须确认 conversation 归属当前登录用户。
- 记忆构建不得绕过 `require_conversation_owner` / runtime owner 校验。
- audit 事件可记录：压缩等级、token 估算、是否 fallback、摘要调用是否成功、摘要模型 metadata。
- audit 事件不得记录：完整 prompt、API key、SQL、guard token、完整 rows、base_url。
- 记忆上下文在 prompt 中应作为“不高于系统指令的历史数据”注入，避免历史消息或上传内容覆盖系统约束。

建议新增 audit-only 事件：
- `conversation.memory_built`
- `conversation.memory_compressed`
- `conversation.memory_summary_updated`
- `conversation.memory_fallback`

## 11. API 与前端影响

v1 不新增前端必需 API。现有：
- `POST /api/v1/conversations/{conversation_id}/messages`
- `GET /api/v1/conversations/{conversation_id}/messages`
- `DELETE /api/v1/conversations/{conversation_id}`

仍保持不变。

可选后续增强：
- 在调试视图展示本轮是否使用记忆、压缩等级与 token 预算；
- 为管理员或研发调试提供 memory summary 查看接口；
- 前端无需依赖 audit-only 事件作为用户主流程。

## 12. 验收标准

验收必须覆盖正向能力、降级路径、权限隔离、prompt 注入边界和审计安全；以下每一项都应至少有一个自动化测试或明确的组合测试覆盖：

1. 同一 conversation 连续两轮对话，第二轮省略主语时，系统可基于记忆补全当前问题，并选择正确 public capability。
2. 追问补全产生的 `resolved_user_message` / effective question 由系统侧 memory builder / orchestration 生成；LLM Planner 不能通过自由 `input_payload` 覆盖可信 `user_question`。
3. 记忆上下文 / effective question 在 workflow provider 构建 plan 前生成；LLM Planner 禁用、失败或输出非法时，deterministic `AutoWorkflowProvider` fallback 仍使用同一份补全结果。
4. `main_agent.respond` 的 prompt 中包含受控历史上下文，回答能承接前文；prompt 只注入带边界标注的 `history_summary`、最近原文消息和安全 capability 摘要，不注入摘要快照存储 metadata。
5. 构建 `ConversationMemoryContext` 时排除当前任务 `root_message_id` 对应 user message，当前问题不会重复出现在 `recent_messages` 或 `history_summary` 中。
6. interrupt answer / resume 补充消息会被标注为 clarification，并与原始 root message、assistant answer 归为同一业务轮次；不会被当作新的当前问题或独立业务轮次。
7. 不同 account / conversation 的消息、artifact、upload metadata 与 memory summary 不会进入彼此的 memory context。
8. 同一 `task_id` 的 assistant history message 与最终 TEXT artifact 不会重复进入 memory context。
9. Conversation memory 使用独立 `ConversationMemorySafeAllowlist`，不会把 `rows`、`candidate_rows`、`storage_ref`、SQL、schema DDL、guard token、完整 prompt、API key 或 base_url 注入长期记忆。
10. Conversation memory 不会通过 `request.metadata` 原样透传给 Skill 自动脚本；Skill script 只接收当前轮 query、显式上传 artifact 和既有脚本输入契约。
11. 跨轮上传文件引用只保留脱敏摘要 / `upload_id` / 文件名 / preview 等安全 metadata；完整文件内容不进入 conversation memory 或摘要快照。上传过期或删除后，系统不会从记忆恢复原始内容，而应要求用户重新上传。
12. `trim_max_tokens` 只作为本轮上下文工程总 token 上限来源，实际 memory 可用预算会扣除 system prompt、当前问题、上传摘要、上游能力结果、Skill 上下文和模型输出预留空间。
13. Level 1 压缩会移除 capability 业务中间产物，仅保留安全摘要，不泄漏 SQL、guard token、schema DDL、完整 rows 或完整 candidate rows。
14. Level 2 压缩会保留最近原文消息，并把更早历史生成摘要；摘要不得引入未出现过的新事实。
15. 摘要 LLM 失败时，本轮任务仍可继续执行，并记录 fallback metadata；fallback metadata 不进入正常 LLM prompt。
16. 摘要快照包含 account、覆盖边界、source hash、版本与 safe model metadata 等持久化 / 审计字段，但正常 Planner / `main_agent.respond` prompt 不注入这些存储 metadata。
17. 删除 conversation 后，对应 messages / tasks / artifacts / events / interrupts / checkpoints / memory summary 被清理；append-only audit 日志仍不记录敏感正文。
18. 所有自动化测试默认使用 fake / injected LLM，不访问真实 provider。

## 13. 测试计划

### 13.1 单元测试

- memory context builder 按时间顺序读取 user / assistant 消息，并按 `task_id` 把 root message、clarification message 与 assistant answer 归为同一业务轮次。
- builder 排除当前任务 `root_message_id`，当前用户问题只出现在 current_user_message 区块。
- builder 只读取当前 conversation，且按 account owner 校验入口使用；跨 account / conversation 数据构造应返回空或拒绝。
- assistant history message 与同任务最终 TEXT artifact 去重：已有 assistant message 时不重复读取 TEXT artifact；assistant message 缺失时才 fallback 到 TEXT artifact。
- `ConversationMemorySafeAllowlist` 明确拒绝 `rows`、`candidate_rows`、`storage_ref`、`sql`、`schema_ddl`、`guard_token`、完整 prompt、API key、base_url 等字段，只允许 capped 安全摘要。
- 上传文件 memory 只保留脱敏摘要 / `upload_id` / 文件名 / preview 等 metadata，不保留 `content` 或完整文件正文。
- memory budget resolver 从已 bootstrap 的环境配置读取 `trim_max_tokens`，并在扣除 system / current / upload / dependency / Skill / output reserve 后得到实际 memory 可用预算。
- Level 0 未超预算时不压缩。
- Level 1 超预算时删除 capability 中间产物，仅保留安全摘要。
- Level 2 超预算时生成历史摘要并保留最近 N 轮原文。
- 摘要失败时 fallback 到最近消息，不抛出到用户请求链路。
- 摘要快照失效判断覆盖 `source_message_ids_hash`、`summary_version`、`compression_policy_version` 变化。
- prompt builder 明确区分历史摘要、最近消息、clarification、当前问题和上游能力结果。
- prompt builder 不把 `summary_id`、`account_id`、`source_message_ids_hash`、版本、safe model metadata、`last_error`、时间戳等摘要快照存储 metadata 注入 LLM。

### 13.2 集成测试

- API 连续提交多轮消息，第二轮追问正确携带 memory context / `resolved_user_message` 进入 planner fake prompt，并保留原始当前用户问题用于审计和最终回答边界。
- SQLQuery 后续追问可基于上一轮品种名 / route 生成正确 public workflow；LLM Planner 不能通过 planner payload 覆盖可信 `user_question`。
- LLM Planner 禁用、provider 抛错、非法 JSON、输出 internal capability 四类 fallback 场景下，`AutoWorkflowProvider` 仍使用同一份系统侧 effective question。
- `main_agent.respond` fake prompt 包含带边界标注的历史摘要和最近消息，但不包含摘要快照存储 metadata、SQL、完整 rows、guard token、base_url 或 API key。
- Skill 自动脚本测试：即使 runtime metadata 内部携带 memory 相关字段，脚本 payload 也不会收到完整 conversation memory。
- interrupt / resume 测试：补充答案被标注为 clarification，resume 当前任务时可合成 effective question；后续新任务 memory 把 root、clarification、assistant answer 视为同一业务轮次。
- 上传跨轮测试：同 conversation 后续追问可看到上传安全摘要；上传过期或删除后不会恢复原始内容，并产生要求重新上传的可解释路径。
- 删除 conversation 后，messages / tasks / artifacts / events / interrupts / checkpoints / memory summary 一并清理。
- 多用户隔离：Bob 无法读取 Alice conversation 的 memory summary、upload metadata 或由其构建出的 memory context。

### 13.3 安全与审计测试

- `conversation.memory_built` / `conversation.memory_compressed` / `conversation.memory_summary_updated` / `conversation.memory_fallback` 只记录压缩等级、token 估算、fallback、safe model metadata 等审计字段，不记录完整 prompt、原始消息正文、完整 rows、SQL、guard token、API key 或 base_url。
- 摘要 LLM 调用失败、超时或输出空摘要时，任务仍继续执行，audit-only 事件记录安全 fallback metadata。
- memory context 作为“不高于系统指令的历史数据”注入；历史消息中包含类似系统指令的内容时，不会覆盖 system prompt 或 capability 安全约束。

### 13.4 回归测试

按实际改动范围运行对应分层 unittest：

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/sql_query -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

如仅修改 PRD 文档，至少校验文档路径、索引链接与 Markdown 基本可读性。

## 14. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 记忆摘要引入错误事实 | 后续回答沿用错误上下文 | 摘要明确标注为系统生成；保留最近原文；用户纠错优先级高于摘要 |
| 历史消息 prompt injection | 历史内容诱导模型违反系统约束 | 历史上下文低于 system 指令；模板明确历史是数据不是指令 |
| token 预算估算不准 | 仍可能超 provider 窗口 | 预算预留 buffer；压缩失败后保守保留最近消息 |
| capability 中间产物泄漏 | SQL / guard token / rows 暴露给通用 LLM | fail-closed allowlist；复用现有 dependency output sanitizer 思路 |
| 摘要调用增加延迟 | 长会话首轮压缩变慢 | 增量摘要持久化；后台或低成本非流式调用；失败可降级 |

## 15. 后续演进

- 支持跨 conversation 的用户可控长期记忆，需单独 PRD。
- 支持可解释的 memory debug 面板。
- 支持不同 capability 声明自己的 memory projection policy。
- 支持更多压缩级别，例如 topic clustering、结构化 facts / preferences 分离、外部摘要评估器。
- 支持 memory quality eval，用固定多轮对话集评估指代解析、纠错承接与摘要忠实度。
