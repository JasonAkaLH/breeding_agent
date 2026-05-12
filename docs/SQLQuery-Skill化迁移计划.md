# SQLQuery Skill 化迁移计划

- **状态**：2026-05-12 已按当前 Skill Executor infra 完成主链路迁移；后续仅保留 native SQLQuery capability 代码作为领域逻辑兼容层与渐进删除对象。
- **目标**：把 SQLQuery 从原生 `sql_query.query` capability 迁移为项目级 `skill.sql_query`，让业务 capability 来源统一收口为 Skill 与 MCP tools，并让 `src/orchestration/` 保持通用编排层。
- **范围**：SQLQuery 领域服务拆分、`skill.sql_query` platform-service handler、API runtime 装配、Planner / Router / Replanner 去 SQLQuery 特判、前端 SQLQuery 展示兼容、旧 capability alias 兼容。
- **当前前提**：`docs/prd/backend/15-SkillExecutor实现需求PRD.md` 对应的 generic Skill Executor infra 已落地，本文不再把“实现 Skill Executor”作为待办主任务，而是把它作为迁移前置能力使用。
- **非目标**：不引入 LangChain / LangGraph / AutoGen；不放开任意 shell / 任意本地文件访问；不把 MySQL、LLM secret 或完整环境变量暴露给普通 Skill；不改变用户现有 SQLQuery 能力与前端体验。

## 1. 复审结论

SQLQuery 仍然适合迁移为项目级 Skill，但迁移方式需要按现有 infra 调整：

1. **不能再按旧文档写 `execution.mode: script` 并绑定 MySQL / LLM service。**
   当前 Skill Executor 已明确：`python_subprocess` 不能绑定受控服务；service-bound Skill 必须使用 `platform_service`，由 runtime 预注册 handler 和 service allowlist。
2. **`skill.sql_query` 应走 `platform_service + answer_mode=requires_finalizer`。**
   SQLQuery 产出结构化查询结果、preview artifact 和诊断信息，最终自然语言回答仍应由内部 finalizer / `main_agent.respond` 汇总，避免 executor 自己变成回答生成器。
3. **SQLQuery 业务逻辑不应塞进 generic Skill Executor。**
   Skill Executor 只负责版本解析、输入校验、权限控制、handler 调用、输出归一化、artifact/event/audit 收口；SQLQuery 的路由、SQL 生成、SQL Guard、查询执行和结果筛选应进入 `src/sql_query/` domain engine。
4. **迁移必须先并行、再替换。**
   先让 native SQLQuery executor 和 `skill.sql_query` 共用同一个 domain engine，并做输出对比；确认 API、artifact、前端进度和安全边界兼容后，再移除 native capability。

推荐路线调整为：

```text
现有 Skill Executor infra 已完成
→ 固化 SQLQuery 现有行为基线
→ 抽出 SQLQuery domain engine，让旧 executor 先复用
→ 注册 skill.sql_query platform-service handler 并与 native 并行
→ Planner / Router / Runtime / Frontend 切到 skill.sql_query
→ API 边界保留 sql_query.query alias
→ 删除 native SQLQuery capability 与 orchestration 特判
```

最终目标不是“换目录”，而是：

```text
orchestration 只编排通用 capability：skill.* / mcp.* / 内部 finalizer
SQLQuery 只是一个项目级 Skill + domain service，不再是编排层内置业务知识
```

## 2. 当前代码事实

### 2.1 已落地的 Skill Executor infra

当前已经具备 SQLQuery Skill 化所需的通用执行壳：

- `src/capabilities/skill_tool/executor.py`
  - `SkillExecutor` 支持已注册 `skill.*` capability；
  - 支持 `delegated_main_agent`、`python_subprocess`、`platform_service`；
  - 支持 `answer_mode=direct | requires_finalizer | none`；
  - 对 missing revision、脚本超时、输出校验、service denied、handler failed 等错误做受控映射；
  - `python_subprocess` 明确禁止绑定受控 service。
- `src/integrations/codex_skills/execution.py`
  - `SkillExecutionConfig` / `resolve_skill_execution_config()`；
  - `SkillScriptExecutionService`；
  - `SkillServiceRegistry`；
  - `SkillPlatformHandlerRegistry`；
  - `SkillPlatformExecutionContext`；
  - runtime handler allowlist 和 service allowlist。
- `src/orchestration/skill_workflow_provider.py`
  - 会读取 Skill manifest execution config；
  - `delegated_main_agent` 仍展开为 forced `main_agent.respond`；
  - `python_subprocess` / `platform_service` 展开为真实 `skill.*` 执行节点；
  - `answer_mode=requires_finalizer` 时追加一个 `main_agent.respond` finalizer。
- `src/api/runtime.py`
  - 已接入 `SkillExecutor`；
  - 已在 `SkillServiceRegistry` 中预留 `mysql_readonly`、`llm.sql_query`、`artifact_writer`、`progress_events` 等 service seam；迁移后的 `skill.sql_query` 新实现中，`llm.sql_query` 必须是主代理 LLMProvider 的非流式适配器，而不是 SQLQuery 专属 LLM client；兼容期内现有 native SQLQuery runtime 可暂时保持现状；
  - 已提供 `skill_platform_handlers`、`trusted_skill_handlers`、`trusted_skill_services` 注入入口；
  - Skill bundle refresh 会同步 descriptor、payload policy 和 Skill executor instance。

因此，本文后续不再要求新增 generic Skill Executor；只要求使用这些能力承载 `skill.sql_query`。

### 2.2 SQLQuery 仍是 native capability

当前 SQLQuery 仍由原生 capability 组成：

- `src/capabilities/sql_query/workflow.py`
  - public capability：`sql_query.query`；
  - internal capability：`sql_query.intent_route`、`sql_query.schema_context_prepare`、`sql_query.sql_generate`、`sql_query.sql_guard`、`sql_query.sql_execute_readonly`、`sql_query.result_filtering`。
- `src/capabilities/sql_query/executor.py`
  - `SQLQueryExecutor` 直接持有各内部节点 capability 实例；
  - 注入 `MySQLReadonlyAdapter`、SQL generator、SQLQuery 内部 LLM、`trim_max_tokens`。
- `src/sql_query/`
  - 已有 schema context、route understanding、模型等复用资产；
  - 但还没有完整的、脱离 `CapabilityExecutionRequest` / `WorkflowNodePlan` 的 SQLQuery domain engine。

### 2.3 orchestration 仍有 SQLQuery 业务耦合

当前通用编排层仍知道 SQLQuery：

- `src/orchestration/auto_workflow_provider.py`
  - 硬编码 `sql_query.query` 自动 fallback 计划；
  - 仍导入 / 使用 SQLQuery route understanding。
- `src/orchestration/workflow_router.py`
  - 对 `sql_query` / `sql_query.query` 有专属分支。
- `src/orchestration/planner_contract.py`
  - prompt 直接写死“数据库 / 数据查询问题优先规划 `sql_query.query`”。
- 多个 orchestration 测试仍直接注册或断言 `sql_query.query`。

这正是本次迁移要消除的主要耦合。

### 2.4 API runtime 仍直接装配 SQLQuery

`src/api/runtime.py` 当前仍：

- import `src.capabilities.sql_query`；
- 注册 SQLQuery public / internal descriptors；
- 注册 native SQLQuery instance；
- 构造 `SQLQueryWorkflowProvider`、`SQLQueryExecutor`、legacy `SQLQueryRuntimeReplanner`；
- 保留 `macro_providers = {"sql_query.query": sql_query_workflow_provider}`；
- 在 `WorkflowRouter` 中传入 `sql_query_provider`。

这些在迁移后都应删除或降级为兼容 alias。

### 2.5 前端仍依赖 native SQLQuery 标识

当前前端仍有多处 SQLQuery capability id / internal node id 判断：

- `frontend/src/api/client.ts`：SQLQuery 模式提交 `sql_query.query`；
- `frontend/src/domain/taskEvents.ts`：根据 `sql_query.intent_route`、`sql_query.sql_execute_readonly` 等 internal capability id 展示进度；
- `frontend/src/domain/artifacts.ts`：通过 artifact id / producer node id 字符串识别 SQLQuery 结果。

前端迁移应优先改成识别 artifact metadata 与 Skill 阶段事件，同时短期保留老字符串识别 fallback。

## 3. 目标架构

### 3.1 统一业务 capability 来源

迁移完成后，业务 capability pool 只保留：

```text
business capability pool
├── skill.*
│   ├── skill.sql_query
│   └── skill.<project_skill>
└── mcp.*
    └── mcp.<server>.<tool>
```

不再公开 `sql_query.query`，也不公开任何 `sql_query.*` internal capability。

### 3.2 框架内核仍保留通用服务

以下能力仍属于框架内核，不作为业务 capability 表达：

```text
planner / validator / expander
scheduler / lifecycle / interrupt / cancel
storage / event / SSE / artifact manager
conversation memory
generic Skill executor
generic MCP executor
internal final answer synthesizer
```

短期 `main_agent.respond` 可以继续作为内部 finalizer 节点存在；长期不应把它当成业务能力来源。

### 3.3 `skill.sql_query` 目标执行链路

```text
Planner / explicit request selects skill.sql_query
→ SkillWorkflowProvider reads manifest execution config
→ Skill node executes through SkillExecutor
→ SkillExecutor validates platform_service handler allowlist
→ SkillExecutor binds mysql_readonly / llm.sql_query / artifact_writer / progress_events
→ registered sql_query.query platform handler calls SQLQuery domain engine
→ SkillExecutor returns structured output, artifacts, audit events
→ answer_mode=requires_finalizer appends main_agent.respond for final response
```

## 4. Manifest 与 runtime binding 设计

### 4.1 `SKILL.md` 建议骨架

`skill/sql-query/SKILL.md` 应使用 platform service，而不是 subprocess script：

```yaml
---
name: sql-query
capability_id: skill.sql_query
description: 安全回答品种、审定、基因型、表型和数据库类只读查询问题；适用于需要从受控 MySQL 只读库检索业务数据并返回表格预览的请求。
triggers:
  - 查询品种
  - 审定信息
  - 基因型
  - 表型数据
  - 数据库查询
  - 查一下
parameters:
  query:
    type: string
    required: true
outputs:
  required:
    - summary
    - filtered_query_result
execution:
  mode: platform_service
  trust_scope: project
  handler: sql_query.query
  answer_mode: requires_finalizer
  services:
    - mysql_readonly
    - llm.sql_query
    - artifact_writer
    - progress_events
---

# SQL Query

## Use when
- 用户需要查询品种、审定、基因型、表型或数据库中的只读业务数据。

## Workflow
1. 判断查询意图和目标数据域。
2. 准备 schema context。
3. 生成候选只读 SQL。
4. SQL 必须经过 guard 校验。
5. 只允许通过 readonly adapter 执行。
6. 对查询结果做 LLM / fallback 筛选；LLM 调用统一走主代理 LLMProvider 的非流式适配。
7. 返回业务摘要、原始 preview、筛选后 preview 和必要诊断。

## Boundaries
- 不执行写入、删除、更新、DDL、权限变更或多语句 SQL。
- 不暴露数据库连接信息、账号、密码、LLM key 或完整 prompt。
- 缺少关键查询实体时，应返回可控缺参 / 澄清结果，而不是编造 SQL。
```

### 4.2 runtime 预注册要求

`skill.sql_query` 不能通过 manifest 动态 import handler。API runtime 必须显式注册：

```python
skill_platform_handlers={
    "sql_query.query": sql_query_platform_handler,
}
trusted_skill_handlers={
    "skill.sql_query": "sql_query.query",
}
trusted_skill_services={
    "skill.sql_query": (
        "mysql_readonly",
        "llm.sql_query",
        "artifact_writer",
        "progress_events",
    ),
}
```

如果 handler 未注册、service 未注册、manifest 请求了未 allowlist 的 service，必须 fail closed，返回 `skill_service_denied` 或受控错误。

### 4.3 LLM Provider 复用规则

SQLQuery 迁移为 Skill 形态后，`skill.sql_query` 的新 platform handler / domain engine 内部 LLM 调用必须遵守以下规则；这些规则不要求在兼容期内立即改写现有 native SQLQuery runtime：

1. **不再维护 SQLQuery 专属 LLM client。**
   `skill.sql_query` platform handler 及其新 domain engine 不应自行创建、持有或读取独立的 SQLQuery LLM client / provider 配置；现有 native SQLQuery runtime 在并行兼容期内可保持原装配方式，直到 native capability 删除。
2. **沿用主代理 LLMProvider。**
   `llm.sql_query` 这个 service 名称可以作为权限边界保留，但其底层实现必须是从主代理 LLMProvider 派生出来的受控 text generator / adapter，而不是第二套 client。
3. **调用方式固定为非流式。**
   SQLQuery 的 intent routing、SQL generation、result filtering 等内部 LLM 调用只需要完整文本结果，必须使用非流式调用，不向前端透传 token delta。
4. **`thinking=false`。**
   SQLQuery 内部 LLM 调用必须显式关闭 thinking / 深度思考，不继承前端或主代理对话的 thinking 开关。
5. **不处理 `reasoning_content`。**
   为了兜底和契约稳定，SQLQuery 内部只读取普通文本内容；即使底层 provider 返回 `reasoning_content`，SQLQuery 也必须忽略，不做解析、不进入 prompt 拼接、不写入 artifact、不进入 audit。
6. **错误降级保持原有 fallback。**
   主代理 LLMProvider 不可用、返回非法 JSON、超时或输出不合规时，SQLQuery 继续使用现有规则 fallback，不因 LLM 异常导致任务崩溃。

验收时需要覆盖：

- `skill.sql_query` platform handler / 新 domain engine 不直接实例化独立 LLM client；
- `skill.sql_query` handler 收到的 `llm.sql_query` service 来自主代理 LLMProvider adapter；
- 内部调用参数包含 `stream=false` / 等价非流式语义与 `thinking=false`；
- fake provider 返回包含 `reasoning_content` 的响应时，SQLQuery 输出、artifact、audit 均不包含该字段内容。

### 4.4 输出 payload 与 artifact 要求

平台 handler 返回的 `output_payload` 至少包含：

```json
{
  "summary": "查询已完成，共返回 3 行结果。",
  "route_id": "approval_variety_db",
  "schema_profile_id": "approval_profile",
  "sql": "SELECT ...",
  "columns": ["variety_name", "approval_code"],
  "rows": [],
  "row_count": 3,
  "query_result_preview": {
    "columns": [],
    "rows": [],
    "row_count": 3,
    "truncated": false
  },
  "filtered_query_result": {
    "columns": [],
    "rows": [],
    "row_count": 3,
    "truncated": false
  },
  "diagnostics": []
}
```

SQLQuery 相关 artifact 必须带稳定 metadata，供前端不依赖 node id 字符串识别：

```json
{
  "domain_kind": "sql_query",
  "artifact_role": "filtered_query_result",
  "capability_id": "skill.sql_query",
  "route_id": "approval_variety_db"
}
```

兼容期可以继续生成旧 id 形态或保留旧识别 fallback，但新逻辑必须以 metadata 为主。

### 4.5 进度事件要求

`platform_service` handler 可通过 `progress_events` 发出用户可见进度事件，建议事件 payload 至少包含：

```json
{
  "domain_kind": "sql_query",
  "stage": "sql_execute_readonly",
  "label": "正在检索数据库"
}
```

建议阶段：

| stage | 用户可见文案 |
|---|---|
| `intent_route` | 正在理解查询意图 |
| `schema_context_prepare` | 正在准备数据库查询 |
| `sql_generate` | 正在生成安全查询语句 |
| `sql_guard` | 正在检查查询安全边界 |
| `sql_execute_readonly` | 正在检索数据库 |
| `result_filtering` | 正在筛选查询结果 |

## 5. 分阶段实施计划

### Phase 0：锁定迁移前行为基线

#### 目标

在改造前固定当前行为，避免迁移过程中用户体验或安全边界退化。

#### 必须覆盖

1. 自然语言数据库问题自动进入 SQLQuery。
2. 显式 `sql_query.query` 兼容入口可用。
3. SQLQuery 内部具体查哪个库仍由 SQLQuery 内部 LLM / fallback 决策。
4. 迁移后的 `skill.sql_query` 内部 LLM 沿用主代理 LLMProvider，通过非流式、`thinking=false` 的适配调用；不再维护 SQLQuery 专属 LLM client。
5. SQL Guard 对写操作、DDL、多语句、高风险 SQL 仍然阻断。
6. MySQL readonly 执行失败返回可控错误，不导致任务崩溃。
7. 结果筛选保留 filtered preview 和 raw preview。
8. 前端仍展示助手最终回答和 SQLQuery 结果卡片。
9. task cancel / interrupt / 补充信息路径不退化。

#### 验收标准

- 现有 SQLQuery 后端分层测试通过；
- 新增或确认 native 与 Skill 并行对比测试入口；
- 前端 SQLQuery artifact / progress 展示测试有迁移保护。

### Phase 1：抽出 SQLQuery domain engine

#### 目标

把 SQLQuery 从 orchestration/capability 节点形态拆成可被 native executor 和 Skill platform handler 共同调用的领域服务。

#### 建议目录

```text
src/sql_query/
  engine.py
  models.py
  routing.py
  schema_context.py
  sql_generation.py
  sql_guard.py
  readonly_execution.py
  result_filtering.py
  artifacts.py
  progress.py
```

#### 领域服务边界

domain engine 不应依赖：

- `CapabilityExecutionRequest`；
- `CapabilityExecutionResult`；
- `WorkflowNodePlan`；
- node id；
- scheduler / router / planner。

domain engine 可以依赖通过构造器注入的服务：

- `MySQLReadonlyAdapter`；
- 主代理 LLMProvider 的 SQLQuery 非流式适配器；
- config paths 或已解析 config；
- progress callback；
- artifact builder callback。

#### 建议模型

```python
@dataclass(frozen=True)
class SQLQueryEngineRequest:
    query: str
    subtask_label: str | None = None
    parent_question: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None

@dataclass(frozen=True)
class SQLQueryEngineResult:
    summary: str
    route_id: str
    schema_profile_id: str
    sql: str
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]
    row_count: int
    query_result_preview: Mapping[str, Any]
    filtered_query_result: Mapping[str, Any]
    diagnostics: tuple[Mapping[str, Any], ...]
```

#### 保留逻辑

必须完整保留：

1. LLM / fallback intent routing；
2. schema context builder；
3. SQL generation fallback；
4. SQL Guard；
5. MySQL readonly execution；
6. LLM / fallback result filtering；
7. token trimming；
8. 安全错误映射；
9. 当前 artifact 内容和 summary 语义。

#### 过渡要求

native `SQLQueryExecutor` 先改成调用 domain engine。此时对外仍是 `sql_query.query`，但内部业务逻辑已经脱离 capability 节点实现。

#### 验收标准

- domain engine 单测不需要 orchestration 即可运行；
- native SQLQuery executor 复用 domain engine 后既有 SQLQuery 测试通过；
- domain engine 输出能直接支持 `skill.sql_query` handler。

### Phase 2：新增 `skill.sql_query` platform-service handler

#### 目标

在不移除 native SQLQuery 的前提下，让 `skill.sql_query` 通过 Skill Executor 执行完整 SQLQuery 主链路。

#### 建议新增模块

```text
src/sql_query/platform_handler.py
skill/sql-query/SKILL.md
skill/sql-query/configs/...
```

也可以先复用 `configs/sql_query/`，避免迁移初期复制配置；若复制到 Skill 包，必须明确配置权威源和同步规则。

#### handler 职责

`sql_query.query` platform handler 负责：

1. 从 `SkillPlatformExecutionContext.input_payload` 读取 `query` / `user_message` / `subtask_label` / `parent_question`；
2. 从 `context.services` 获取 `mysql_readonly`、`llm.sql_query`、`artifact_writer`、`progress_events`；其中 `llm.sql_query` 是主代理 LLMProvider 的非流式适配器，不是独立 SQLQuery LLM client；
3. 调用 SQLQuery domain engine；
4. 把 domain result 转成 `SkillPlatformHandlerResult`；
5. 生成带 SQLQuery metadata 的 artifacts；
6. 只记录脱敏 audit / progress，不记录 secret、完整连接串或完整 prompt。

#### 验收标准

- `/api/v1/capabilities` 出现 `skill.sql_query`；
- 显式 `capability_id="skill.sql_query"` 可执行 SQLQuery 主路径；
- `skill.sql_query` 输出与 native SQLQuery 主路径结果等价；
- `answer_mode=requires_finalizer` 只追加一个 finalizer；
- 未注册 handler / 未授权 service / 缺失 service 均 fail closed。

### Phase 3：Planner / Router 切到 `skill.sql_query`

#### 目标

让自动规划和显式路由优先使用 `skill.sql_query`，但兼容旧 `sql_query.query` 请求。

#### 修改点

1. `src/orchestration/planner_contract.py`
   - 删除写死 `sql_query.query` 的提示；
   - 依赖 public capability descriptor 中 `skill.sql_query` 的 description / triggers；
   - 保留通用“根据 public capability 选择能力”的规则。
2. `src/orchestration/workflow_router.py`
   - 删除 SQLQuery 专属分支；
   - 保留通用分支：`skill.*` → Skill provider，`mcp.*` → MCP provider。
3. `src/orchestration/auto_workflow_provider.py`
   - 不再 import SQLQuery 业务模块；
   - fallback 只做通用 finalizer 或通用 Skill matcher，不做 SQLQuery 拆分计划。
4. runtime replanner
   - 不再保留 SQLQuery 专属 runtime replanner；
   - 继续使用 LLM Planner / generic Skill routing。

#### 兼容入口

旧请求：

```text
sql_query
sql_query.query
```

只允许在 API 请求边界转换为：

```text
skill.sql_query
```

禁止旧 id 继续进入 capability registry、Planner prompt 或 WorkflowRouter 专属分支。

#### 验收标准

- Planner prompt 不再出现 `sql_query.query`；
- 自动数据库问题规划到 `skill.sql_query`；
- 显式旧 id 请求在 API 边界转换后可用；
- `src/orchestration/` 不再 import `src.capabilities.sql_query` 或 `src.sql_query`。

### Phase 4：API runtime 移除 native SQLQuery 装配

#### 目标

让 API runtime 不再把 SQLQuery 当内置 capability 注册。

#### 删除项

`src/api/runtime.py` 应删除：

- `SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS` 注册；
- `SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS` 注册；
- `build_local_sql_query_instance()`；
- `SQLQueryWorkflowProvider()`；
- `SQLQueryExecutor(...)`；
- `SQLQueryRuntimeReplanner(...)`；
- `macro_providers = {"sql_query.query": ...}`；
- `WorkflowRouter(..., sql_query_provider=...)` 中的 SQLQuery provider 依赖。

#### 保留项

- SQLQuery domain engine；
- `skill.sql_query` manifest；
- `sql_query.query` platform handler 注册；
- API alias 转换逻辑；
- MySQL readonly config 解析；迁移后的 `skill.sql_query` 不再解析或维护独立 LLM client config，只接收由主代理 LLMProvider 派生的非流式 `thinking=false` LLM service。兼容期内现有 native SQLQuery runtime 可暂时保留原 LLM 装配，直到 native capability 删除。

#### 验收标准

- `/api/v1/capabilities` 不再返回 `sql_query.query`；
- `/api/v1/capabilities` 不再返回 `sql_query.*` internal capability；
- `SkillExecutor` 是唯一执行 `skill.sql_query` 的 capability executor；
- 真实 runtime 启动后 SQLQuery 仍可用。

### Phase 5：前端适配

#### 目标

用户体验不变，但前端不再依赖 native SQLQuery capability id / internal node id。

#### 修改点

1. `frontend/src/api/client.ts`
   - SQLQuery 手动模式 capability id 改为 `skill.sql_query`；
   - 或完全隐藏手动模式，仅使用自动规划。
2. `frontend/src/domain/taskEvents.ts`
   - 支持 `skill.sql_query`；
   - 支持 `domain_kind=sql_query` + `stage` 的 Skill progress 事件；
   - 旧 `sql_query.*` internal id 只作为兼容 fallback。
3. `frontend/src/domain/artifacts.ts`
   - SQLQuery artifact 识别改为 metadata 优先；
   - 旧 artifact id / producer node id 字符串识别保留为兼容 fallback。
4. 测试
   - 更新 `client.test.ts`、`taskEvents.test.ts`、`artifacts.test.ts`、`App.test.tsx`。

#### 验收标准

- 页面仍展示 SQLQuery 结果卡片；
- 任务进度仍显示 SQLQuery 阶段；
- 前端默认不再发送 `sql_query.query`；
- 老任务 / 老 artifact 的展示不退化。

### Phase 6：删除 native SQLQuery capability

#### 删除条件

只有满足以下条件后，才能删除 native SQLQuery capability：

1. `skill.sql_query` e2e 覆盖自然语言查询、显式调用、guard blocked、empty result、执行失败、结果筛选、final answer；
2. API alias 覆盖旧 `sql_query.query` 请求；
3. 前端不再主动发送旧 id；
4. `/api/v1/capabilities` 不再公开旧 id；
5. orchestration 无 SQLQuery import；
6. 手工真实 MySQL / LLM smoke 通过或明确记录未运行原因。

#### 删除范围

- `src/capabilities/sql_query/` 中只保留过渡期仍需要的兼容 adapter；最终可删除 capability 层文件；
- `tests/capabilities/sql_query/` 迁移为 `tests/sql_query/` domain tests 与 `tests/capabilities/skill_tool` / `tests/api` Skill SQLQuery tests；
- 删除 planner / router / runtime / frontend 中旧 id 的非兼容逻辑。

## 6. 验收清单

### 6.1 Capability 池

- [ ] `/api/v1/capabilities` 包含 `skill.sql_query`。
- [ ] `/api/v1/capabilities` 不包含 `sql_query.query`。
- [ ] `/api/v1/capabilities` 不包含任何 `sql_query.*` internal capability。
- [ ] MCP tool 仍以 `mcp.*` 形式出现。
- [ ] 其他项目 Skill 仍以 `skill.*` 形式出现。

### 6.2 orchestration 解耦

- [ ] `src/orchestration/` 不再 import `src.capabilities.sql_query`。
- [ ] `src/orchestration/` 不再 import `src.sql_query`。
- [ ] Planner prompt 不再写死 `sql_query.query`。
- [ ] Router 不再有 SQLQuery 专属分支。
- [ ] Auto fallback 不再硬编码 SQLQuery。

### 6.3 SQLQuery 功能

- [ ] 自然语言数据库问题仍能自动调用 `skill.sql_query`。
- [ ] SQLQuery 内部 LLM 路由仍可用。
- [ ] SQLQuery 内部 LLM 沿用主代理 LLMProvider，非流式、`thinking=false`，并忽略 / 不处理 `reasoning_content`。
- [ ] SQL 生成仍可用。
- [ ] SQL Guard 仍强制执行。
- [ ] MySQL readonly 执行仍可用。
- [ ] 结果筛选仍可用。
- [ ] raw preview 与 filtered preview artifact 仍可用。
- [ ] 最终助手回答仍可用。

### 6.4 安全

- [ ] 普通 `python_subprocess` Skill 不能访问 MySQL readonly。
- [ ] 普通 Skill 不能访问 SQLQuery 专用 LLM service binding；该 binding 只是主代理 LLMProvider 的受控非流式适配器。
- [ ] `skill.sql_query` 只能通过 runtime allowlist handler 使用受控 services。
- [ ] handler 未注册、service 未注册、service 未授权时 fail closed。
- [ ] audit 不记录数据库连接串、账号、密码、LLM key、完整 prompt。
- [ ] SQL Guard 失败不能被 Skill / handler 绕过。

### 6.5 兼容

- [ ] 兼容期内 `capability_id="sql_query.query"` 不崩，API 边界转换到 `skill.sql_query`。
- [ ] 前端默认发送 `skill.sql_query` 或完全交给自动规划。
- [ ] 老 SQLQuery 结果卡片展示不退化。
- [ ] 老任务事件 / 老 artifact 仍有 fallback 展示。

## 7. 测试策略

### 7.1 后端测试

建议按层分批执行：

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/sql_query -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
```

重点新增 / 调整测试：

- SQLQuery domain engine 单测；
- native SQLQuery executor 复用 domain engine 后的回归；
- `skill.sql_query` platform-service handler；
- service binding 权限与 fail-closed；
- `answer_mode=requires_finalizer` 只追加一个 finalizer；
- 旧 `sql_query.query` alias；
- Planner 通过 descriptor 选择 `skill.sql_query`；
- orchestration 无 SQLQuery import 的静态测试；
- artifact metadata 兼容测试。

### 7.2 前端测试

```bash
cd frontend
npm test -- --run
npm run build
```

重点覆盖：

- SQLQuery 模式不再发送 `sql_query.query`；
- `skill.sql_query` / `domain_kind=sql_query` 进度展示；
- SQLQuery 结果卡片通过 artifact metadata 识别；
- 旧 artifact 字符串识别 fallback；
- 老任务事件展示 fallback。

### 7.3 手工 smoke

真实 provider / 真实 MySQL smoke 不进入默认自动化回归，仍通过显式脚本或全栈人工验证：

```bash
conda run -n multi_agent python scripts/smoke_main_agent_llm.py --config config.yaml
python scripts/run_fullstack_dev.py
```

如果新增 SQLQuery Skill smoke，必须继续遵守：真实数据库连接信息只放本地 `config.yaml` 或部署环境变量，不写入 tracked 文件。

## 8. 风险与控制

| 风险 | 当前判断 | 控制方式 |
|---|---|---|
| 把 SQLQuery 业务塞进 Skill Executor | 中高风险，会破坏 generic executor 边界 | SQLQuery 逻辑只放 `src/sql_query/` domain engine 与 SQLQuery platform handler |
| 错用 `python_subprocess` 绑定 MySQL / LLM | 当前 infra 会拒绝，但文档若错误会误导实现 | `skill.sql_query` 必须声明 `platform_service`，runtime allowlist handler |
| Planner 选择率下降 | 删除 `sql_query.query` 硬编码后依赖 descriptor 质量 | 强化 `skill.sql_query` description / triggers；迁移期做 planner 选择回归 |
| 前端依赖 internal capability id | 当前事实存在 | 新增 Skill stage event + artifact metadata，旧字符串只做 fallback |
| 一次性删除 native SQLQuery 破坏链路 | 高风险 | 先 domain engine 复用，再 Skill 并行对比，最后删除 native |
| alias 长期存在造成概念混乱 | 中风险 | alias 只放 API 边界，标记 deprecated，并约定删除窗口 |
| service 使用审计不足 | 中风险 | `skill.service_bound`、SQLQuery domain progress、guard events 保持 audit-only 脱敏记录 |

## 9. 推荐落地顺序

已完成前置项：

1. ✅ generic Skill Executor infra；
2. ✅ Skill execution config / answer mode；
3. ✅ runtime handler / service allowlist seam；
4. ✅ Skill bundle refresh 与 executor instance 同步。

剩余建议按以下顺序提交：

1. **baseline PR**：补齐 SQLQuery native 行为与前端展示迁移保护测试。
2. **domain engine PR**：抽出 `src/sql_query/engine.py` 等领域服务，native executor 先复用。
3. **platform handler PR**：新增 `skill.sql_query` manifest 和 `sql_query.query` platform handler，和 native 并行。
4. **skill e2e PR**：显式 `skill.sql_query`、自动规划、guard blocked、empty result、finalizer、artifact metadata 全链路通过。
5. **routing cleanup PR**：Planner / Router / AutoWorkflowProvider / Replanner 去 SQLQuery 特判。
6. **runtime cleanup PR**：API runtime 不再注册 native SQLQuery capability，仅保留 service 和 alias。
7. **frontend PR**：前端切换 `skill.sql_query` 与 metadata / stage event 识别。
8. **native removal PR**：删除 native SQLQuery capability 与旧专项测试，保留 domain tests 和 Skill tests。
9. **alias deprecation PR**：按约定窗口删除 `sql_query.query` API alias。

## 10. 最终判断

按当前 infra，SQLQuery Skill 化已经具备关键前置条件，但迁移计划必须从“先实现 Skill Executor”更新为“使用现有 Skill Executor 的 platform-service 能力承载 SQLQuery”。

本计划的关键约束是：

1. **`skill.sql_query` 必须走 `platform_service`，不能走带 service binding 的 subprocess script。**
2. **LLM Provider 复用规则只约束迁移后的 `skill.sql_query` 新实现，不要求立即改写现有 native SQLQuery runtime。**
3. **SQLQuery 业务逻辑必须先抽成 domain engine，再由 native executor 与 Skill handler 复用。**
3. **orchestration 去耦合必须等 `skill.sql_query` 并行验证通过后再做。**
4. **前端必须从 capability id / node id 字符串识别逐步迁移到 metadata / stage event 识别。**

满足这些条件后，迁移可以在不改变现有能力和用户体验的前提下，把框架收口为更轻量的 Skill / MCP capability 统一管理模型。
