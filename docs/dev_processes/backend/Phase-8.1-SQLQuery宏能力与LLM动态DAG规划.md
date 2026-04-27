# Phase 8.1：SQLQuery 宏能力与 LLM 动态 DAG 规划

> 状态：文档计划项已完成（2026-04-27）；首轮 public SQLQuery 边界已完成（2026-04-24）；完整 LLM Planner 已接入默认自动规划（2026-04-27）
> 定位：承接 Phase 8 的主代理 LLM 与 Skill 兼容层，进一步把数据库查询能力封装为对外单一的 SQLQuery 宏能力，并为后续 LLM Planner 生成受约束高层 DAG 打基础。

## 1. 背景

Phase 8 首轮已经完成：

- `main_agent.respond` 默认主代理入口；
- Codex Skill parser / catalog / matcher；
- 受控 Python script runner；
- 主代理非 thinking streaming 输出。

当前 SQL 查询能力在 Phase 5 / Phase 5.5 中沉淀为六个内部运行节点：

```text
sql_query.intent_route
sql_query.schema_context_prepare
sql_query.sql_generate
sql_query.sql_guard
sql_query.sql_execute_readonly
sql_query.result_filtering
```

这对内部调试有价值，但不适合作为主代理或未来 LLM Planner 的公开规划面。主代理应把 SQL 查询视为一个整体业务能力，而不是理解 SQL 生成、Guard、执行这些内部步骤。

## 2. 命名规则

从 Phase 8.1 开始，数据库查询能力统一命名为 **SQLQuery**。

命名规则如下：

| 层级 | 规则 | 示例 |
|---|---|---|
| 产品 / 文档展示名 | `SQLQuery` | SQLQuery 能力 |
| public capability id | `sql_query.query` | 主代理 / LLM Planner 只能看到这个能力 |
| public alias | `sql_query` | API 可接受的查询能力简写 |
| Python 模块名 | `sql_query` | `src/capabilities/sql_query/` |
| Python 类名前缀 | `SQLQuery` | `SQLQueryWorkflowProvider` |
| 内部节点 capability id | `sql_query.*` | `sql_query.sql_generate` |
| 常量命名 | `SQL_QUERY_*` | `SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS` |

约束：

- 展示名、文档产品名、Python 类名前缀统一使用 `SQLQuery`。
- Python 包 / 目录 / 函数 / capability id 统一使用 `sql_query`。
- `sql_query.*` 只允许作为内部实现节点出现，能力目录接口默认不展示这些内部节点。
- 主代理与后续 LLM Planner 只应看到 `sql_query.query` 这个宏能力。
- 源码目录、配置目录、测试目录与开发文档同步完成命名迁移，不再保留旧内部命名规则。

## 3. SQLQuery 宏能力

对主代理 / LLM Planner 暴露：

```text
sql_query.query
```

语义：

> 根据用户自然语言问题，安全查询数据库并返回筛选后的表格与原始表格预览，供主代理做最终对话式整合。

内部固定展开为既有受控链路：

```text
intent_route
→ schema_context_prepare
→ sql_generate
→ sql_guard
→ sql_execute_readonly
→ result_filtering
```

这里 `sql_query.query` 是 **Macro Capability / Subworkflow Capability**：

- 对外是一个节点；
- 对内展开为固定子工作流；
- LLM Planner 不能直接调用内部节点；
- LLM Planner 不能跳过 `sql_guard`；
- 内部节点继续保留审计、产物、interrupt 与调试价值。

## 4. LLM 动态 DAG 规划边界

Phase 8.1 的 LLM Planner 后续只允许生成高层 DAG：

```json
[
  {"node_id": "query_data", "capability_id": "sql_query.query"},
  {"node_id": "answer_user", "capability_id": "main_agent.respond"}
]
```

禁止生成：

```json
[
  {"node_id": "generate_sql", "capability_id": "sql_query.sql_generate"},
  {"node_id": "execute_sql", "capability_id": "sql_query.sql_execute_readonly"}
]
```

后续实现 LLM Planner 前，必须先补的前置项当前已补齐：

1. [x] public capability registry / allowlist；
2. [x] `WorkflowPlanValidator`：校验 capability 是否 public、节点是否 acyclic、依赖是否存在、输入是否符合契约；
3. [x] `WorkflowExpander`：将 `sql_query.query` 展开为内部 SQLQuery 固定子工作流；
4. [x] Planner 输出 JSON schema 与 fake LLM 测试；
5. [x] 对外能力目录默认隐藏 SQLQuery 内部节点。

说明：以上前置契约已在 2026-04-27 用于接入完整 `LLMWorkflowProvider`。默认 `capability_id=None` 会先尝试 LLM Planner，校验 / 展开 / 再校验通过后执行；planner 不可用或输出非法时 fallback 到确定性 `AutoWorkflowProvider`。

## 5. Phase 8.1 首版验收

首版先完成 SQLQuery 公开命名与宏能力边界：

- [x] 注册 public capability `sql_query.query`，展示名为 `SQLQuery`；
- [x] 能力目录默认不展示 `sql_query.*` 内部节点；
- [x] `capability_id="sql_query"` / `"sql_query.query"` 进入现有 SQL 查询固定 workflow；
- [x] `capability_id="sql_query"` 作为顶层查询能力简写继续可用；
- [x] 现有 SQL 查询回归测试继续通过；
- [x] Phase 8 / LLM 接入文档同步使用 SQLQuery 作为新公开名称。

## 6. 首轮实现记录

首轮实现已完成 public SQLQuery 边界：

- `CapabilityDescriptor` 增加 `public` 字段，能力目录接口默认只返回 public capability。
- `SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS` 注册 `sql_query.query`，展示名为 `SQLQuery`。
- `SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS` 保持内部节点注册，但 `public=False`，继续服务固定 SQLQuery workflow。
- `SQLQueryWorkflowProvider` 承载 SQLQuery 固定 workflow。
- workflow router 在请求入口只支持 `sql_query` / `sql_query.query`；`sql_query.*` 内部节点只能由 `SQLQueryWorkflowProvider` 展开后进入 scheduler / executor，不接受外部直接指定。

## 6.1 前置规划契约补齐记录

2026-04-27 已补齐完整 Phase 8.1 文档计划中的 LLM Planner 前置契约：

- 新增 `WorkflowPlanValidator`，支持 public-only 校验，用于拒绝 LLM 高层 DAG 中的非 public capability，并校验重复节点、未知依赖、环形依赖与 JSON-serializable input payload。
- `OrchestrationService` 在执行内部 workflow 前运行非 public-only plan 校验，确保实际执行图仍满足 DAG 与 capability 注册约束。
- 新增 `WorkflowExpander`，可把高层 `sql_query.query` 宏节点展开为固定 SQLQuery 内部子工作流，并将上游 / 下游依赖改写到内部 root / tail 节点。
- 新增 Planner 输出 JSON schema 与 fake LLM 输出解析测试，只定义高层 DAG 输出契约，不开放低层 SQLQuery 内部节点给 Planner。

## 6.2 默认自动规划首轮接入记录

2026-04-27 已完成默认用户入口的自动 capability 选择首轮：

- 新增 `AutoWorkflowProvider`，在 `capability_id=None` 时根据用户问题自动构造高层 DAG。
- 普通问题仍回退到单节点 `main_agent.respond`，保持原主代理路径。
- 数据库 / 品种 / 审定 / 基因型类问题会先生成高层 `sql_query.query -> main_agent.respond` DAG，再通过 `WorkflowExpander` 展开成 SQLQuery 固定六节点 + 主代理整合节点。
- 主代理执行器会把上游能力输出中的摘要、路由、schema profile、行数、fallback 状态等安全字段注入 Prompt 的“上游能力结果上下文”，让最终回答基于已完成 capability 的事实。
- 前端移除“普通对话 / SQLQuery”手动模式选择，默认提交 `capability_id=null`，产品表现为“当前模式：自动规划”。

边界说明：

- 当前自动规划使用确定性规则，优先保障产品可用性和 SQLQuery 安全边界。
- 完整 LLM Planner 已在 public-only validator、planner output parser 和 fallback 策略之上接入。
- 显式 `sql_query` / `sql_query.query` 入口继续保留，用于兼容测试、调试与后续内部工具。

## 6.3 完整 LLM Planner 接入记录

2026-04-27 在 Ralph 执行中完成完整 LLM Planner 接入：

- 新增 `LLMWorkflowProvider`：默认入口先调用 planner text generator / LLM client 生成 public-only 高层 DAG。
- Planner prompt 会列出当前 public capability 的 id、name、description，并明确数据库类问题优先规划 `sql_query.query -> main_agent.respond`，普通问题规划单 `main_agent.respond`。
- Planner 输出经过 `parse_planner_output`、`WorkflowPlanValidator(public_only=True)`、`WorkflowExpander`、internal validator 四层处理后才执行。
- 新增 per-capability payload allowlist：`CapabilityPayloadPolicy` / `PlannerPayloadPolicy` 默认 fail-closed，策略随 capability 注册进入 `CapabilityRegistry`，只有 capability 明确声明的 `planner_allowed_fields` 会从 LLM planner payload 进入执行图，`system_payload_factory` 生成的系统字段始终覆盖 planner 字段。
- Planner 对当前 public capability 只被信任为拓扑规划者；`main_agent.respond` 与 `sql_query.query` 的 payload policy 由各自 capability 模块声明并强制用真实用户问题覆盖 `user_message` / `user_question`，后续新增 capability 通过同一注册 seam 声明自己的 planner payload allowlist。
- Planner 输出只有数据能力而没有主代理 final answer 时，系统会追加 `main_agent.respond` finalizer，依赖所有叶子节点；若已有主代理 finalizer 但未依赖数据能力叶子节点，系统会自动重连依赖。
- Planner 输出 internal capability、非法 JSON、非法依赖、provider 异常或无 planner generator 时，fallback 到确定性 `AutoWorkflowProvider`，并在 plan metadata 中记录 `planner_fallback_used` / `planner_fallback_reason`。
- `ApiRuntime` 支持异步 plan provider，并新增 `workflow.plan_built` audit-only 事件记录 node count 与 planner metadata。
- `build_api_runtime` 新增 planner 注入 seam：`planner_text_generator`、`planner_llm_config`、`planner_llm_config_path`、`planner_llm_client_factory`、`planner_reasoning_effort`、`enable_llm_planner`。测试可关闭真实 planner，生产默认可尝试真实 LLM 并安全 fallback。

新增验证覆盖：

- Orchestration 单测覆盖 LLM planner 成功展开、单主代理、SQL-only 自动 finalizer、payload 覆盖保护、registry 级 capability payload policy、自定义 capability payload allowlist、allowlist prompt 展示、未配置 capability payload fail-closed、未连线主代理重连、provider 异常 fallback、internal capability fallback、非法 JSON fallback。
- API 集成测试覆盖注入式 LLM planner 的数据库 DAG、普通单主代理 DAG、payload 覆盖保护、未连线主代理重连、internal capability fallback 和 factory seam。

## 7. 非目标

Phase 8.1 首版不做：

- 不让 LLM 直接生成可执行低层 DAG；
- 不允许 LLM 调用 `sql_execute_readonly`、`sql_guard` 等内部节点；
- 不改变 SQL Guard 与只读执行安全边界。
