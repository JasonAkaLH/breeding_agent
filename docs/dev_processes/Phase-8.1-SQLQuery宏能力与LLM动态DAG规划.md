# Phase 8.1：SQLQuery 宏能力与 LLM 动态 DAG 规划

> 状态：首轮实现完成（2026-04-24）  
> 定位：承接 Phase 8 的主代理 LLM 与 Skill 兼容层，进一步把数据库查询能力封装为对外单一的 SQLQuery 宏能力，并为后续 LLM Planner 生成受约束高层 DAG 打基础。

## 1. 背景

Phase 8 首轮已经完成：

- `main_agent.respond` 默认主代理入口；
- Codex Skill parser / catalog / matcher；
- 受控 Python script runner；
- 主代理非 thinking streaming 输出。

当前 SQL 查询能力已经在 Phase 5 / Phase 5.5 中沉淀为六个内部节点：

```text
sql_query.intent_route
sql_query.schema_context_prepare
sql_query.sql_generate
sql_query.sql_guard
sql_query.sql_execute_readonly
sql_query.result_summarize
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

> 根据用户自然语言问题，安全查询数据库并返回结果摘要。

内部固定展开为既有受控链路：

```text
intent_route
→ schema_context_prepare
→ sql_generate
→ sql_guard
→ sql_execute_readonly
→ result_summarize
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

后续实现 LLM Planner 前，必须先补：

1. public capability registry / allowlist；
2. `WorkflowPlanValidator`：校验 capability 是否 public、节点是否 acyclic、依赖是否存在、输入是否符合契约；
3. `WorkflowExpander`：将 `sql_query.query` 展开为内部 SQLQuery 固定子工作流；
4. Planner 输出 JSON schema 与 fake LLM 测试；
5. 对外能力目录默认隐藏 SQLQuery 内部节点。

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

## 7. 非目标

Phase 8.1 首版不做：

- 不在本阶段实现完整 LLM Planner；
- 不让 LLM 直接生成可执行低层 DAG；
- 不允许 LLM 调用 `sql_execute_readonly`、`sql_guard` 等内部节点；
- 不改变 SQL Guard 与只读执行安全边界。
