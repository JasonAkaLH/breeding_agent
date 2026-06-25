# 阶段三：事件、Graph API 与 Prompt 脱敏 PRD

- **编号**：后端 PRD 22-Phase 3
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段二固定 DAG 插入
- **下游阶段**：fixed DAG 放量门禁、阶段四 runtime replanner
- **目标模块**：`src/api/routes/tasks.py`、`src/api/runtime.py`、`src/orchestration/service.py`、`src/capabilities/main_agent/prompt_builder.py`、`tests/api/`

## 1. 阶段目标

阶段二会让内部 Workbench nodes 进入实际 task graph。本阶段专门收口所有 public 或 prompt-facing 通道的泄漏风险，是 `fixed_dag` 放量前的安全门禁：

1. SSE / frontend event 不暴露 `workbench.*` capability id、handler、runtime、路径、SQL、schema DDL、storage ref。
2. `task.graph_updated` 和 `GET /api/v1/tasks/{task_id}/graph` 对内部节点隐藏、泛化或 opaque 映射。
3. task summary / history artifact 不展示 Workbench digest artifact。
4. finalizer prompt dependency context 只包含 allowlist safe digest。
5. audit 侧可保留真实内部 node / capability id，但 audit payload 仍脱敏。

## 2. 范围

### In scope

- internal node frontend masking helper。
- `node.started` / `node.completed` / `node.failed` 等事件中的 capability 显示名泛化。
- `task.graph_updated.added_node_ids` 的 opaque 或泛化策略。
- graph API response 对 internal node 的隐藏 / 泛化 / opaque id 映射。
- prompt builder Workbench digest allowlist 与敏感字段过滤。
- history / artifact 展示边界测试。

### Out of scope

- 不新增 DTO 字段或 SSE schema。
- 不新增前端页面。
- 不改变 artifact 下载协议。
- 不把 audit 内部详情暴露给普通用户。

## 3. Frontend event 脱敏规则

对于 `metadata.internal_node=True` 的节点：

1. `capability_id` 若出现在 frontend-facing payload 中，必须映射为泛化值，例如 `internal.validation`。
2. 用户可见文案只能是“结果校验中”“产物检查中”等泛化描述，不展示具体 Workbench stage 名称，除非该 stage 名称已被产品确认可公开。
3. 不发送 handler、runtime、entrypoint、path、storage key、SQL、schema DDL、raw output。
4. audit-only sink 可记录真实 `node_id`、`capability_id`、stage 和 reason code，但也不能记录敏感 payload。

## 4. Graph API 脱敏规则

`GET /api/v1/tasks/{task_id}/graph` 必须满足：

| 情况 | 要求 |
| --- | --- |
| 普通前端视图 | 不直接返回 `workbench.*` capability id。 |
| 内部节点存在 | 可隐藏、泛化为 `internal.validation`，或使用稳定 opaque id。 |
| 依赖关系 | 不因隐藏内部节点而让 graph response 违反现有 schema；必要时以泛化节点占位。 |
| audit / debug | 可通过内部通道查看真实映射，但不进入 public graph response。 |

`task.graph_updated.added_node_ids` 如包含内部节点，应保持协议字段但避免语义化内部实现名称泄漏。

## 5. Prompt dependency context 规则

Prompt builder 对 Workbench output 只允许以下字段进入 finalizer：

- `summary`
- `highlights`
- `caveats`
- `structured_content.safe_digest`
- `structured_content.blocking`
- `structured_content.confidence`
- `satisfaction.satisfied`
- `satisfaction.reason_code`
- `satisfaction.replan_recommended`

禁止字段即使嵌套在 `safe_digest` 内也必须剔除或 fail closed。finalizer prompt 需明确：Workbench digest 是验证事实和风险提示，不是用户可见执行步骤，不得向用户暴露内部 stage 链路。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| node event masking | internal node frontend event 不含 `workbench.*`、handler、runtime、path。 |
| graph response masking | task graph response 不直接暴露 Workbench capability id 或敏感字段。 |
| graph updated masking | `task.graph_updated` 不通过 added node id 泄露语义化内部实现。 |
| prompt allowlist | finalizer dependency context 只含 safe digest allowlist 字段。 |
| prompt forbidden nested | 禁止字段嵌套在 safe_digest 内也被剔除或 fail closed。 |
| history artifact boundary | Workbench 不创建 / 不展示 frontend artifact；Skill artifact 展示保持不变。 |
| audit safe detail | audit 可记录内部 stage，但不记录 raw payload / path / storage key。 |

推荐命令：

```bash
python -m pytest tests/api/ -k "graph or event or task"
python -m pytest tests/capabilities/main_agent/ -k "prompt or dependency"
python -m pytest tests/capabilities/workbench/
```

## 7. 阶段验收

- fixed DAG 的所有 public-facing 通道均通过泄漏回归。
- prompt dependency context 不含禁止字段。
- Skill artifact 下载链路不退化。
- 本阶段通过后，`fixed_dag` 才可从本地测试推进到受控灰度。
