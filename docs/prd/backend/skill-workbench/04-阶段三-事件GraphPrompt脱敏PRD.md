# 阶段三：事件、Graph API 与 Prompt 脱敏 PRD

- **编号**：后端 PRD 22-Phase 3
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段二 Runtime Workbench Loop
- **下游阶段**：生产部署安全门禁、阶段四 contract diagnostics
- **目标模块**：`src/api/routes/tasks.py`、`src/api/runtime.py`、`src/orchestration/service.py`、`src/capabilities/main_agent/prompt_builder.py`、`tests/api/`

## 1. 阶段目标

阶段二会让内部 Workbench nodes 通过 runtime replan 进入实际 task graph。本阶段专门收口所有 public 或 prompt-facing 通道的泄漏风险，是生产部署前的安全门禁：

1. SSE / frontend event 不暴露 `workbench.*` capability id、内部 stage、handler、runtime、路径、SQL、schema DDL、storage ref。
2. `task.graph_updated` 和 `GET /api/v1/tasks/{task_id}/graph` 对内部节点隐藏、泛化或 opaque 映射。
3. task summary / history artifact 不展示 Workbench digest artifact。
4. finalizer prompt dependency context 同时包含 Skill output 与 Workbench safe digest；Workbench output 走专用 sanitizer。
5. audit 侧可保留真实内部 node / capability id / stage，但 audit payload 仍脱敏。

## 2. 范围

### In scope

- internal node frontend masking helper。
- `node.started` / `node.completed` / `node.failed` 等事件中的 capability 显示名泛化。
- `task.graph_updated.added_node_ids` 的 opaque 或泛化策略。
- graph API response 对 internal node 的隐藏 / 泛化 / opaque id 映射。
- prompt builder Workbench digest 专用 allowlist 与敏感字段过滤。
- history / artifact 展示边界测试。

### Out of scope

- 不新增 DTO 字段或 SSE schema。
- 不新增前端页面。
- 不改变 artifact 下载协议。
- 不把 audit 内部详情暴露给普通用户。

## 3. Internal node 识别规则

Graph API 和事件脱敏不能只依赖 `WorkflowNodePlan.metadata.internal_node=True`，因为持久化 `TaskNode` 当前不保存 plan metadata。实现必须支持以下识别方式：

1. **首选**：通过 `CapabilityRegistry` 查询 node capability descriptor，`public=False` 且 `kind="workbench"` 的节点必须视为内部 Workbench 节点。
2. **辅助**：plan 执行期间可继续使用 `metadata.internal_node=True`、`workbench_stage` 等内部 metadata。
3. **禁止**：不得要求普通 graph API 读取未持久化 metadata 才能完成脱敏。

若后续新增持久化 node metadata，也不能放宽 descriptor-based 判断；两者不一致时应 fail closed 到内部节点脱敏路径。

## 4. Frontend event 脱敏规则

对于内部 Workbench 节点：

1. `capability_id` 若出现在 frontend-facing payload 中，必须映射为泛化值，例如 `internal.validation`。
2. `node_id` 若出现在 frontend-facing payload 中，必须使用阶段二定义的 opaque id；不得包含真实 stage 或 `workbench.*`。
3. 用户可见文案只能是“结果校验中”“产物检查中”等泛化描述，不展示具体 Workbench stage 名称。
4. 不发送 handler、runtime、entrypoint、path、storage key、SQL、schema DDL、raw output。
5. audit-only sink 可记录真实 `node_id`、`capability_id`、stage 和 reason code，但也不能记录敏感 payload。

## 5. Graph API 脱敏规则

`GET /api/v1/tasks/{task_id}/graph` 必须满足：

| 情况 | 要求 |
| --- | --- |
| 普通前端视图 | 不直接返回 `workbench.*` capability id、真实 stage 或语义化内部 node id。 |
| 内部节点存在 | 可隐藏、泛化为 `internal.validation`，或使用稳定 opaque id。 |
| 依赖关系 | 不因隐藏内部节点而让 graph response 违反现有 schema；必要时以泛化节点占位。 |
| audit / debug | 可通过内部通道查看真实映射，但不进入 public graph response。 |

`task.graph_updated.added_node_ids` 如包含内部节点，应保持协议字段但避免语义化内部实现名称泄漏。

## 6. Prompt dependency context 规则

Finalizer 应综合 Skill output 与 Workbench 验证过程：

- 普通 Skill output 继续走现有通用 dependency sanitizer，保持业务能力输出的泛用性。
- Workbench output 作为独立节点 output，必须根据 producing node capability `workbench.*` 或 `schema_version=workbench.output.v1` 走 Workbench 专用 sanitizer。

Workbench 专用 sanitizer 只允许以下字段进入 finalizer：

- `summary`
- `highlights`
- `caveats`
- `structured_content.safe_digest`
- `structured_content.blocking`
- `structured_content.confidence`
- `satisfaction.satisfied`
- `satisfaction.reason_code`
- `satisfaction.replan_recommended`

禁止字段即使嵌套在 `safe_digest` 内也必须剔除或 fail closed。finalizer prompt 需明确：Workbench digest 是任务过程中的验证事实和风险提示，不是用户可见执行步骤；不得向用户暴露内部 stage 链路、capability id、handler、runtime、路径、SQL、schema DDL、storage ref 或 secret。

## 7. 测试计划

| 测试 | 断言 |
| --- | --- |
| descriptor-based masking | graph / event masking 可通过 `public=False && kind=workbench` 识别内部节点，不依赖未持久化 metadata。 |
| node event masking | internal node frontend event 不含 `workbench.*`、真实 stage、handler、runtime、path。 |
| graph response masking | task graph response 不直接暴露 Workbench capability id、真实 stage 或敏感字段。 |
| graph updated masking | `task.graph_updated` 不通过 added node id 泄露语义化内部实现。 |
| prompt allowlist | Workbench output 进入 finalizer 时只含 safe digest allowlist 字段。 |
| prompt forbidden nested | 禁止字段嵌套在 safe_digest 内也被剔除或 fail closed。 |
| skill output unaffected | 普通 Skill output 仍走现有通用 sanitizer，不被 Workbench 专用 sanitizer 误伤。 |
| history artifact boundary | Workbench 不创建 / 不展示 frontend artifact；Skill artifact 展示保持不变。 |
| audit safe detail | audit 可记录内部 stage，但不记录 raw payload / path / storage key。 |

推荐命令：

```bash
python -m pytest tests/api/ -k "graph or event or task"
python -m pytest tests/capabilities/main_agent/ -k "prompt or dependency"
python -m pytest tests/capabilities/workbench/
```

## 8. 阶段验收

- runtime loop 的所有 public-facing 通道均通过泄漏回归。
- graph / event masking 不依赖未持久化 metadata。
- prompt dependency context 中 Workbench digest 不含禁止字段。
- 普通 Skill output 泛用性不退化。
- Skill artifact 下载链路不退化。
- 本阶段通过后，runtime Workbench loop 才可进入生产部署。
