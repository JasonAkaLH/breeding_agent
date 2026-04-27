# 高层 DAG 规划与 SQLQuery 宏能力边界

- **范围**：后端 / 编排层 / SQLQuery public capability
- **文档状态**：正式版（补齐 Phase 8.1 实现事实）
- **日期**：2026-04-27
- **对应开发过程**：`docs/dev_processes/Phase-8.1-SQLQuery宏能力与LLM动态DAG规划.md`

## 1. 背景

一期 SQLQuery MVP 定义了固定六节点内部 workflow。但随着主代理进入二期，需要为后续 LLM Planner 预留“只生成高层 DAG”的边界：Planner 可以选择公开能力，但不能直接拼接 SQLQuery 内部节点，也不能绕过 SQL Guard 或固定 workflow。

当前实现已补齐 public capability、workflow validator、macro expander 与 planner 输出解析 seam，因此需要纳入正式 PRD。

## 2. 目标

本专题目标是：
- 对外只暴露 SQLQuery 宏能力 `sql_query.query`；
- 保持 `sql_query.*` 内部节点只由 SQLQuery workflow provider 生成；
- 让后续 LLM Planner 只面向 public capability 生成高层 DAG；
- 用 validator 拒绝未知 capability、非 public capability、重复节点、未知依赖、环形依赖与非 JSON input；
- 用 expander 将 `sql_query.query` 安全展开成固定 SQLQuery 子工作流；
- 保持现阶段不实现完整 LLM Planner，只固化前置契约。

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
- `sql_query.result_summarize`

这些节点只能由 `SQLQueryWorkflowProvider` 生成并交给 scheduler / executor 执行。

## 4. Workflow Router 契约

请求入口路由规则：
- `capability_id=None`：进入主代理默认 workflow；
- `capability_id="sql_query"`：进入 SQLQuery fixed workflow；
- `capability_id="sql_query.query"`：进入 SQLQuery fixed workflow；
- 其他非 public 或未知 capability：拒绝。

该规则确保普通对话与显式 SQLQuery 查询可共存，且 SQLQuery 内部节点不能被外部绕过调用。

## 5. LLM Planner 高层输出契约

后续 LLM Planner 只能输出高层 DAG JSON，节点字段最小包含：
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

现阶段实现仅提供 planner prompt / output parser / fake LLM seam 与验证器，不负责让真实 LLM Planner 接管生产路由。

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
- 使用 `SQLQueryWorkflowProvider` 生成内部六节点 workflow；
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
- Planner 输出非法时必须拒绝或 fallback 到确定性路由，不得执行不可信 DAG。

## 9. 验收口径

本专题验收包括：
- public capability list 只向外展示 `sql_query.query`，不展示 `sql_query.*` 内部节点；
- `sql_query` 与 `sql_query.query` 都能进入 SQLQuery fixed workflow；
- 外部直接请求 `sql_query.*` 内部节点会被拒绝；
- `WorkflowPlanValidator(public_only=True)` 能拒绝 internal capability；
- validator 能拒绝重复节点、未知依赖、环形依赖、非 JSON input；
- `WorkflowExpander` 能把 `sql_query.query` 展开为固定 SQLQuery 六节点 workflow 并正确改写上下游依赖；
- planner contract parser 能从 fake LLM 输出构造高层 plan，但不代表完整 LLM Planner 已投入生产。
