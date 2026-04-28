# 高层 DAG 规划与 SQLQuery 宏能力边界

- **范围**：后端 / 编排层 / SQLQuery public capability
- **文档状态**：正式版（补齐 Phase 8.1 + 完整 LLM Planner 接入事实）
- **日期**：2026-04-27
- **对应开发过程**：`docs/dev_processes/backend/Phase-8.1-SQLQuery宏能力与LLM动态DAG规划.md`

## 1. 背景

一期 SQLQuery MVP 定义固定六节点内部 workflow；当前产品链路保留尾节点 `sql_query.result_filtering`，由 SQLQuery 内部先对 LIKE 召回的候选行做结果筛选，再向主代理提供筛选后的表格结果。但随着主代理进入二期，仍需要为后续 LLM Planner 预留“只生成高层 DAG”的边界：Planner 可以选择公开能力，但不能直接拼接 SQLQuery 内部节点，也不能绕过 SQL Guard 或固定 workflow。

当前实现已补齐 public capability、workflow validator、macro expander 与 planner 输出解析 seam，因此需要纳入正式 PRD。

## 2. 目标

本专题目标是：
- 对外只暴露 SQLQuery 宏能力 `sql_query.query`；
- 保持 `sql_query.*` 内部节点只由 SQLQuery workflow provider 生成；
- 让 LLM Planner 只面向 public capability 生成高层 DAG；
- 用 validator 拒绝未知 capability、非 public capability、重复节点、未知依赖、环形依赖与非 JSON input；
- 用 expander 将 `sql_query.query` 安全展开成固定 SQLQuery 子工作流；
- 默认入口先尝试 LLM Planner，输出非法或 provider 不可用时 fallback 到确定性自动规划，避免业务用户手动选择 capability。

## 3. Public / Internal Capability 边界

### 3.1 Public capability

SQLQuery 对外公开能力为：
- `sql_query.query`

展示名为：
- `SQLQuery`

入口别名：
- `sql_query`

### 3.2 Internal capability

以下 capability 是 SQLQuery 内部固定子工作流节点，不允许作为外部请求入口或 LLM 高层 DAG 节点：
- `sql_query.intent_route`
- `sql_query.schema_context_prepare`
- `sql_query.sql_generate`
- `sql_query.sql_guard`
- `sql_query.sql_execute_readonly`
- `sql_query.result_filtering`

这些节点只能由 `SQLQueryWorkflowProvider` 生成并交给 scheduler / executor 执行。

## 4. Workflow Router 契约

请求入口路由规则：
- `capability_id=None`：进入自动规划 workflow；
  - 普通问题：单节点 `main_agent.respond`；
  - 数据库 / 品种 / 审定 / 基因型类问题：高层 DAG 先选择 `sql_query.query`，再由 `main_agent.respond` 基于上游结果生成自然语言最终回答；
- `capability_id="sql_query"`：进入 SQLQuery fixed workflow；
- `capability_id="sql_query.query"`：进入 SQLQuery fixed workflow；
- `capability_id="main_agent"` / `main_agent.*`：进入主代理 workflow；
- 其他非 public 或未知 capability：拒绝。

该规则确保默认用户入口不再暴露手动 capability 选择；显式 SQLQuery / 主代理入口仅作为兼容或调试路径保留，且 SQLQuery 内部节点不能被外部绕过调用。

默认自动规划链路：
- `LLMWorkflowProvider` 先调用 planner text generator / LLM client 生成 public-only 高层 DAG；
- 高层 DAG 经 `WorkflowPlanValidator(public_only=True)` 校验后，通过 `WorkflowExpander` 展开宏能力；
- 展开后的内部 DAG 再经 internal validator 校验，随后进入 `OrchestrationService` 执行；
- planner 失败、输出非法、引用 internal capability、依赖非法或 provider 不可用时，fallback 到确定性 `AutoWorkflowProvider`；
- 确定性 fallback 仍按农业数据库线索走 `SQLQuery -> 主代理整合`，否则走单主代理。

## 5. LLM Planner 高层输出契约

LLM Planner 只能输出高层 DAG JSON，节点字段最小包含：
- `node_id`
- `capability_id`
- `depends_on`
- `input_payload`

Planner 输出要求：
- 只能使用 public capability，例如 `main_agent.respond`、`sql_query.query`；
- 不得引用 `sql_query.*` 内部节点；
- `input_payload` 必须 JSON-serializable；
- DAG 必须 acyclic；
- 依赖必须引用同一 plan 内已存在节点；
- 节点 ID 不得重复。

Runtime 已接入 planner prompt / output parser / fake LLM seam / 可选真实 LLM client。测试默认可关闭真实 planner，生产默认会尝试 planner 并在失败时安全回退。2026-04-28 起，默认生产路径中 planner、runtime replan advisor 与 main_agent finalizer 共享一个主代理 `SharedLLMRuntime`；SQLQuery 内部 text generator 使用独立 SQLQuery LLM runtime，非流式且 `thinking=disabled`，不复用主代理 LLM 实例。显式组件级 fake / override seam 仅作为测试和定制入口保留。
LLM-facing Prompt 使用中文表达；JSON key、capability_id、node_id、SQL 字段名等机器契约保持英文 / 原始标识，避免破坏解析和执行边界。

信任边界：
- Planner 的 `input_payload` 只作为结构输入通过 parser；执行前必须经过 per-capability payload allowlist。
- `CapabilityPayloadPolicy` / `PlannerPayloadPolicy` 默认 fail-closed：未配置 capability 不接收任何 planner-provided payload 字段；策略跟随 capability 注册到 `CapabilityRegistry`，不是由 LLM Planner 为 SQLQuery 写死判断。
- 每个 capability 可声明 `planner_allowed_fields`，仅白名单字段会从 LLM 输出进入执行图；`system_payload_factory` 负责从可信请求上下文生成系统字段，且系统字段始终覆盖 planner 字段；Planner prompt 会按 public capability 清单展示 allowlist，让后续 capability 在注册时即可获得同一套规划提示与执行保护。
- 对当前 public capability，系统会强制用真实用户问题覆盖 `main_agent.respond.user_message` 与 `sql_query.query.user_question`，不信任 LLM 改写后的查询/回答文本。
- 若 planner 同时输出 SQLQuery 和主代理但缺少依赖，系统会把主代理 finalizer 重连到数据能力叶子节点；若没有主代理 finalizer，则自动追加。

## 6. WorkflowPlanValidator 契约

`WorkflowPlanValidator` 必须支持两种使用方式：
- `public_only=True`：用于校验 LLM Planner 高层 DAG，只允许 public capability；
- `public_only=False`：用于校验系统内部展开后的 plan，可包含内部 capability。

校验失败必须显式拒绝，不允许 silently repair。

最小校验项：
- plan 至少有一个 node；
- node_id 非空且唯一；
- capability 已注册且 enabled；
- public-only 模式下 capability 必须 public；
- input payload 可 JSON 序列化；
- depends_on 不引用未知节点；
- DAG 不含环。

## 7. WorkflowExpander 契约

`WorkflowExpander` 负责把高层宏节点展开为内部固定 workflow。

对 `sql_query.query` 的展开要求：
- 使用 `SQLQueryWorkflowProvider` 生成内部六节点 workflow，尾节点为 `sql_query.result_filtering`；
- 宏节点上游依赖接到内部 root 节点；
- 宏节点下游依赖接到内部 tail 节点；
- 内部节点之间不得依赖 macro plan 外部节点；
- 展开后 metadata 标记 `expanded=true` 与 `macro_capabilities`。

这保证 LLM Planner 只做“能力选择与高层依赖规划”，不接触 SQLQuery 内部安全链路。

## 8. 安全与边界

- Planner 不得生成 SQL；SQL 生成仍属于 `sql_query.sql_generate`。
- Planner 不得跳过 `sql_query.sql_guard`；guard 仍是固定 workflow 必经节点。
- Planner 不得直接调用数据库执行节点。
- Planner 不得无限动态扩展；仍受 workflow plan 的 `max_replans` 与 `max_dynamic_nodes` 约束。
- Planner 输出非法时必须拒绝并 fallback 到确定性路由，不得执行不可信 DAG。
- 若 planner 输出只有数据能力而没有 `main_agent.respond`，系统会追加主代理 finalizer 节点，确保最终回答保持对话式整合。
- 若 planner 输出的 `main_agent.respond` 未依赖数据能力叶子节点，系统会重写依赖，确保主代理接收上游结果上下文。

## 9. 验收口径

本专题验收包括：
- public capability list 只向外展示 `sql_query.query`，不展示 `sql_query.*` 内部节点；
- `sql_query` 与 `sql_query.query` 都能进入 SQLQuery fixed workflow；
- 外部直接请求 `sql_query.*` 内部节点会被拒绝；
- 默认 `capability_id=None` 的数据库类问题会自动构造 `sql_query.query -> main_agent.respond` 高层 DAG，并展开成 SQLQuery 六节点 + 主代理一节点执行图；
- 默认普通问题仍为单 `main_agent.respond`；
- 主代理执行时能接收上游能力结果上下文，用 SQLQuery 筛选后的表格、路由、行数等安全字段生成最终回答；
- 注入 fake LLM planner 时可验证 planner 成功路径、单主代理路径、planner 输出 internal capability 后 fallback 路径；
- `workflow.plan_built` audit 事件记录 planner route、fallback 状态与原因；
- `WorkflowPlanValidator(public_only=True)` 能拒绝 internal capability；
- validator 能拒绝重复节点、未知依赖、环形依赖、非 JSON input；
- `WorkflowExpander` 能把 `sql_query.query` 展开为固定 SQLQuery 六节点 workflow 并正确改写上下游依赖；
- planner contract parser 能从 fake LLM 输出构造高层 plan，完整 LLM Planner 已作为默认自动规划首选路径接入 runtime，并保留确定性 fallback。
