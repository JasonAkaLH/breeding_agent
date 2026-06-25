# 阶段零：Workbench 基座、Policy 与 Runtime State PRD

- **编号**：后端 PRD 22-Phase 0
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：`docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md`
- **下游阶段**：阶段一内部 capability 与 executor、阶段二 Runtime Workbench Loop
- **目标模块**：`src/orchestration/`、`src/api/runtime.py`、`tests/orchestration/`

## 1. 阶段目标

建立 Workbench 的最小平台基座，但不执行 Workbench：

1. 定义 `WorkbenchPolicy`、`WorkbenchStage`、`WorkbenchOutputContractV1` 的平台模型。
2. 定义 runtime loop 需要的 `WorkbenchReplanState`、`WorkbenchStageDecision`、`WorkbenchBudget`。
3. 实现按通用 contract / descriptor / runtime 属性计算 policy decision 的 helper。
4. 在 initial plan 阶段为命中策略的 Skill 写入有限 `max_replans` / `max_dynamic_nodes` 预算；普通 Skill 继续保持预算为 0。
5. 用测试锁定“不得按具体 Skill 名称命中策略”。

## 2. 范围

### In scope

- policy 输入：`execution_mode`、`answer_mode`、`input_schema_count`、`schema_selector`、`output_required_fields`、`output_artifact_policy`、`resource_policy`、未来 `quality_workbench`。
- policy 输出：`enabled`、`stages`、`finalizer_digest_mode`、`max_replans`、`max_dynamic_nodes`、`event_visibility`、`decision_reason`。
- runtime state：已执行 stage、目标 Skill node、目标 capability、是否已有 finalizer、预算消耗摘要。
- initial plan 预算写入：仅策略明确允许的 Skill 可提升预算；后续 revised plan 不得提升预算。
- safe policy decision 记录：只记录安全枚举和原因，不记录 payload、path、storage key 或 raw output。
- consumer contract tests 使用 fake Skill descriptor / contract，不依赖真实业务 Skill。

### Out of scope

- 不注册 `workbench.*` capability。
- 不新增 executor。
- 不追加 Workbench nodes。
- 不改 SSE、graph API、prompt dependency context。
- 不要求 Skill contract 新增 `quality_workbench` 字段；本阶段只预留解析目标。

## 3. 数据结构要求

### 3.1 WorkbenchStage

阶段枚举必须至少覆盖：

```text
data_profile
schema_match
preflight_validate
domain_validate
artifact_inspect
report_verify
```

未知 stage 必须 fail closed，不能 silently ignore。

### 3.2 WorkbenchPolicy

建议字段：

```python
class WorkbenchPolicy:
    enabled: bool
    stages: list[WorkbenchStage]
    finalizer_digest_mode: Literal["none", "when_finalizer_exists", "required"]
    max_replans: int
    max_dynamic_nodes: int
    event_visibility: Literal["masked_frontend", "audit_only"]
    decision_reason: str
```

约束：

1. `enabled=False` 时 `stages=[]`、预算为 0。
2. 未命中平台策略的 Skill 默认 `enabled=False`。
3. `answer_mode=direct` 默认 `finalizer_digest_mode=none`。
4. `answer_mode=requires_finalizer` 默认只允许 `when_finalizer_exists`。
5. `answer_mode=none` 只有显式策略才允许 `required`。
6. `max_replans/max_dynamic_nodes` 不能为负数。
7. policy helper 不得读取完整用户 payload、原始文件、完整 rows、storage key 或本地路径。

### 3.3 WorkbenchOutputContractV1

本阶段只定义 schema 和验证 helper，不执行真实 Workbench。required 字段：

```json
{
  "schema_version": "workbench.output.v1",
  "workbench_kind": "schema_match",
  "target_capability_id": "skill.<id>",
  "target_node_id": "task:skill_execute",
  "summary": "短摘要",
  "satisfaction": {
    "satisfied": true,
    "reason_code": "verified",
    "replan_recommended": false
  }
}
```

禁止字段继承总纲：`sql`、`schema_ddl`、`raw_output`、完整 `rows`、完整文件内容、`storage_ref`、`storage_key`、`path`、`handler`、`runtime`、`entrypoint`、`token`、`secret`、`password`、`api_key`、`authorization` 等。

### 3.4 WorkbenchReplanState

Runtime loop 必须能从 plan metadata / node outputs / current nodes 中恢复状态，避免 resume 或重复 replan 时重复执行同一 stage。建议字段：

```python
class WorkbenchReplanState:
    target_skill_node_id: str
    target_capability_id: str
    completed_stages: tuple[WorkbenchStage, ...]
    pending_stages: tuple[WorkbenchStage, ...]
    finalizer_node_id: str | None
    budget: WorkbenchBudget
```

该状态不得包含 raw output、文件路径、storage key、SQL、schema DDL 或 secret。

## 4. Policy decision 行为

在 Skill plan initial 阶段：

1. 解析候选 Skill 的通用属性。
2. 计算 `WorkbenchPolicy`。
3. 将 safe policy decision 写入 plan metadata，便于 runtime replanner 后续读取。
4. 若 `enabled=True`，只在 initial plan 写入受控预算；不追加 Workbench nodes。
5. 若 `enabled=False`，保持现有 Skill plan 行为和预算。

Decision payload 不得包含：用户原文、Skill payload、上传文件路径、storage key、原始输出、SQL、schema DDL、handler、runtime、secret。

## 5. 测试计划

| 测试 | 断言 |
| --- | --- |
| default disabled | 未命中策略时 policy disabled，DAG 不变，预算为 0。 |
| budget only in initial plan | 命中策略时只在 initial plan 写入预算；revised plan 不得提高预算。 |
| no skill-name matching | 两个不同 Skill 名称但相同 contract 属性得到同一 decision；名称变化不影响 policy。 |
| answer mode defaults | `direct` 不需要 finalizer；`requires_finalizer` 只接已有 finalizer；`none` 默认不新增 finalizer。 |
| output contract schema | required 字段缺失失败；禁止字段失败或被 sanitizer 剔除。 |
| decision safe payload | policy decision 不包含 path、storage key、SQL、schema DDL、handler、runtime、secret。 |
| replan state safe | Workbench state 不包含 raw output、路径、storage key 或 secret。 |

推荐命令：

```bash
python -m pytest tests/orchestration/ -k "workbench or skill_workflow_provider or runtime_replanner"
```

## 6. 阶段验收

- Policy helper、budget 写入和 runtime state 有测试覆盖。
- 未命中策略的 Skill 行为不变。
- policy 决策来源只依赖通用属性。
- output contract、禁止字段和 state 安全规则已被测试锁定。
- 本阶段完成后可以安全进入阶段一，但不能声称 Workbench 已执行。
