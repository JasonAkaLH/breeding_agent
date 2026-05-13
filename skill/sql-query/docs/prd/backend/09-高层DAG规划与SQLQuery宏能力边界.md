# 高层 DAG 规划与 SQLQuery Skill 边界

- **范围**：后端 / 编排层 / SQLQuery Skill public capability
- **文档状态**：当前版（以 `skill.sql_query` 为唯一公开 SQLQuery 入口）
- **日期**：2026-04-27；更新：2026-05-13

## 1. 背景

SQLQuery 当前公开能力已收口为项目级 Skill：`skill.sql_query`。Planner 只能选择公开能力，不能直接拼接 SQLQuery 领域阶段，不能绕过 SQL Guard，也不能把 SQL / schema / guard 细节泄入编排层。SQLQuery 由通用 `SkillWorkflowProvider` + `SkillExecutor` 根据 `skill/sql-query/SKILL.md` 的 `platform_service` 配置执行。

## 2. 当前目标

本专题当前目标是：
- 对外只暴露 SQLQuery Skill 能力 `skill.sql_query`；
- SQLQuery 的 intent route、schema context、SQL generation、SQL guard、readonly execute、result filtering 均作为 Skill bundle `runtime/sql_query_skill/` 领域阶段存在，不再作为 public/internal capability 注册；
- LLM Planner 只面向 public capability 生成高层 DAG；
- validator 拒绝未知 capability、非 public capability、重复节点、未知依赖、环形依赖与非 JSON input；
- `SkillWorkflowProvider` 将 `skill.sql_query` 展开为通用 Skill executor 节点，并通过受信任 handler project Skill bundle handler 调用 SQLQuery domain engine；
- 默认入口先尝试 LLM Planner，输出非法或 provider 不可用时 fallback 到确定性自动规划；确定性路径如需数据库查询也只能选择 `skill.sql_query`。

## 3. Public / Domain Stage 边界

### 3.1 Public capability

SQLQuery 对外公开能力为：
- `skill.sql_query`

展示名为：
- `SQLQuery`

不在 capability registry 中的外部显式请求应在 API 边界返回 unsupported capability。

### 3.2 Domain stages

以下阶段是 SQLQuery Skill handler 内部的领域步骤，不允许作为外部请求入口或 LLM 高层 DAG 节点：
- intent route
- schema context prepare
- SQL generation
- SQL guard
- readonly execute
- result filtering

这些阶段由 `skill/sql-query/runtime/sql_query_skill/engine.py` 串联，并通过通用 project platform-service loader 从 `SkillExecutor` 受控调用。编排层只看到 `skill.sql_query` 及后续 finalizer 所需的脱敏 artifact / metadata。

## 4. Workflow Router 契约

请求入口路由规则：
- `capability_id=None`：进入自动规划 workflow；
  - 普通问题：单节点 `main_agent.respond`；
  - 数据库 / 品种 / 审定 / 基因型类问题：高层 DAG 先选择 `skill.sql_query`，再由 `main_agent.respond` 基于上游结果生成自然语言最终回答；
- `capability_id="skill.sql_query"`：进入 SQLQuery Skill platform-service workflow；
- `capability_id="main_agent"` / `main_agent.*`：进入主代理 workflow；
- 其他非 public 或未知 capability：拒绝。

默认自动规划链路：
- `LLMWorkflowProvider` 调用 planner text generator / LLM client 生成 public-only 高层 DAG；
- 高层 DAG 经 `WorkflowPlanValidator(public_only=True)` 校验后，通过 `WorkflowExpander` 展开 Skill 能力；
- 展开后的 DAG 再经 internal validator 校验，随后进入 `OrchestrationService` 执行；
- planner 失败、输出非法、引用 domain/internal 细节、依赖非法或 provider 不可用时，fallback 到确定性 `AutoWorkflowProvider`；
- 确定性 fallback 仍按农业数据库线索走 `skill.sql_query -> main_agent.respond`，否则走单主代理。

## 5. LLM Planner 高层输出契约

LLM Planner 只能输出高层 DAG JSON，节点字段最小包含：
- `node_id`
- `capability_id`
- `depends_on`
- `input_payload`

Planner 输出要求：
- 只能使用 public capability，例如 `main_agent.respond`、`skill.sql_query` 与已公开 `skill.*` / `mcp.*`；
- 不得引用 SQLQuery domain stages 或 handler key；
- `input_payload` 必须 JSON-serializable；
- DAG 必须 acyclic；
- 依赖必须引用同一 plan 内已存在节点；
- 节点 ID 不得重复。

Runtime 已接入 planner prompt / output parser / fake LLM seam / 可选真实 LLM client。2026-05-13 起，SQLQuery Skill 内部 LLM 调用通过受控 service binding 复用主代理 `SharedLLMRuntime` 的非流式 adapter，并在 handler 内固定 `thinking=false`；不再维护独立 SQLQuery LLM runtime。

信任边界：
- Planner 的 `input_payload` 只作为结构输入通过 parser；执行前必须经过 per-capability payload allowlist。
- `CapabilityPayloadPolicy` / `PlannerPayloadPolicy` 默认 fail-closed：未配置 capability 不接收任何 planner-provided payload 字段。
- `skill.sql_query` 的可信用户问题由系统 payload 填充，系统字段始终覆盖 planner 字段。
- Planner 只能决定是否调用 `skill.sql_query`；具体数据库 route、schema profile、SQL 生成和结果筛选都由 SQLQuery domain engine 决定。
- 若 planner 同时输出 SQLQuery 和主代理但缺少依赖，系统会把主代理 finalizer 重连到数据能力叶子节点；若没有主代理 finalizer，则根据 `answer_mode=requires_finalizer` 自动追加。

## 6. WorkflowPlanValidator 契约

`WorkflowPlanValidator` 必须支持两种使用方式：
- `public_only=True`：用于校验 LLM Planner 高层 DAG，只允许 public capability；
- `public_only=False`：用于校验系统内部展开后的 plan。

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

`WorkflowExpander` 负责把高层 Skill / macro 节点展开为系统可执行节点。

对 `skill.sql_query` 的展开要求：
- 使用 `SkillWorkflowProvider` 基于 `skill/sql-query/SKILL.md` 生成 `SkillExecutor` 节点；
- `execution.mode` 必须为 `platform_service`；
- handler key 与 handler module 必须由 `skill/sql-query/SKILL.md` 声明，并由通用 project platform-service loader fail-closed 加载；
- `answer_mode` 必须为 `requires_finalizer`，确保 SQLQuery 结构化结果由主代理最终汇总；
- Skill node 上游依赖和下游 finalizer 依赖按通用 Skill workflow 规则重连。

这保证 LLM Planner 只做“能力选择与高层依赖规划”，不接触 SQLQuery 内部安全链路。

## 8. 安全与边界

- Planner 不得生成 SQL；SQL 生成只发生在 SQLQuery domain engine 内。
- Planner 不得跳过 SQL Guard；guard 是 handler 内固定必经阶段。
- Planner 不得直接调用数据库执行阶段。
- Planner 不得无限动态扩展；仍受 workflow plan 的 `max_replans` 与 `max_dynamic_nodes` 约束。
- Planner 输出非法时必须拒绝并 fallback 到确定性路由，不得执行不可信 DAG。
- 若 planner 输出只有数据能力而没有 `main_agent.respond`，系统会基于 Skill `answer_mode` 追加主代理 finalizer 节点，确保最终回答保持对话式整合。
- 若 planner 输出的 `main_agent.respond` 未依赖数据能力叶子节点，系统会重写依赖，确保主代理接收上游结果上下文。

## 9. 当前验收口径

本专题验收包括：
- public capability list 只向外展示 `skill.sql_query`，不展示 SQLQuery domain stages；
- 外部显式请求 SQLQuery domain stage 或其他未知 capability 会被拒绝；
- 默认 `capability_id=None` 的数据库类问题会自动构造 `skill.sql_query -> main_agent.respond` 高层 DAG，并展开成 Skill executor + 主代理 finalizer 执行图；
- 默认普通问题仍为单 `main_agent.respond`；
- 主代理执行时能接收上游 Skill 结果上下文，用 SQLQuery 筛选后的表格、路由、行数等安全字段生成最终回答；
- 注入 fake LLM planner 时可验证 planner 成功路径、单主代理路径、planner 输出非 public capability 后 fallback 路径；
- `workflow.plan_built` audit 事件记录 planner route、fallback 状态与原因；
- `WorkflowPlanValidator(public_only=True)` 能拒绝非 public capability；
- validator 能拒绝重复节点、未知依赖、环形依赖、非 JSON input；
- `WorkflowExpander` 能把 `skill.sql_query` 展开为通用 Skill workflow 并正确改写上下游依赖；
- planner contract parser 能从 fake LLM 输出构造高层 plan，完整 LLM Planner 已作为默认自动规划首选路径接入 runtime，并保留确定性 fallback。
