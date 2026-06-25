# 阶段二：固定 DAG 插入与 Finalizer Digest PRD

- **编号**：后端 PRD 22-Phase 2
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段零 policy、阶段一 internal capability / executor
- **下游阶段**：阶段三事件 / graph / prompt 脱敏门禁、阶段四 runtime replanner
- **目标模块**：`src/orchestration/skill_workflow_provider.py`、`src/orchestration/workflow_expander.py`、`src/capabilities/main_agent/prompt_builder.py`、`tests/orchestration/`

## 1. 阶段目标

实现 MVP 形态：在 initial plan 展开时插入固定 Workbench DAG。此阶段不做 runtime 动态追加，但必须正确处理 answer mode 和 finalizer digest：

1. `skill.*` macro 或 direct Skill plan 根据 `WorkbenchPolicy` 插入固定 Workbench nodes。
2. Workbench nodes 使用 `metadata.internal_node=True`、`workbench_stage`、`target_skill_node_id`、`target_capability_id` 标记。
3. `answer_mode=direct` 不新增第二个 finalizer。
4. `answer_mode=requires_finalizer` 的 finalizer 依赖最后一个 verifier，而不是直接依赖 Skill node。
5. `answer_mode=none` 只有 `finalizer_digest_mode=required` 时才新增 task-level finalizer。
6. finalizer dependency context 只消费 Workbench safe digest。

## 2. 范围

### In scope

- `SkillWorkflowProvider` 或相邻 policy helper 负责初始 Workbench node 插入。
- `WorkflowExpander` 保持 public macro 展开后仍是内部 DAG。
- 多 Skill finalizer 不重复生成。
- finalizer dependency 改接最后一个 Workbench verifier。
- plan metadata 记录 Workbench strategy，便于测试和审计。
- fixed DAG 不消耗 runtime replan budget，除非策略明确为未来 Phase 4 预留。

### Out of scope

- 不实现 `WorkbenchRuntimeReplanner`。
- 不改 public plan schema。
- 不新增 SSE event schema 或 graph DTO 字段。
- 不要求所有 Skill 修改 contract。
- 不允许 LLM replanner 生成 Workbench nodes。

## 3. DAG 规则

### 3.1 插入位置

按 policy stage 构造线性或轻量分支 DAG：

```text
skill_execute
  -> workbench.artifact_inspect / workbench.schema_match / workbench.report_verify
  -> main_agent.respond（仅当 answer mode / policy 需要）
```

若存在 preflight stage，可在 skill node 前执行：

```text
workbench.data_profile -> workbench.schema_match -> workbench.preflight_validate -> skill_execute
```

阶段二优先选择确定、可测试、低复杂度的固定链路；复杂条件选择留给阶段四 runtime loop。

### 3.2 Node metadata

每个 Workbench node 必须包含：

```json
{
  "internal_node": true,
  "workbench_stage": "artifact_inspect",
  "target_skill_node_id": "task:skill_execute",
  "target_capability_id": "skill.<id>"
}
```

metadata 不得包含 handler、runtime、entrypoint、path、storage key、raw payload。

### 3.3 Budget

- fixed DAG 插入不依赖 runtime replan。
- 普通 Skill 默认 `max_replans=0`、`max_dynamic_nodes=0`。
- 若 policy 为阶段四预留预算，预算必须写入 initial plan metadata 且可审计。
- 后续 revised plan 不得提升预算。

## 4. Finalizer digest 规则

| answer mode / policy | 行为 |
| --- | --- |
| `direct` + 默认策略 | Workbench 可做 audit/health，但不新增 `main_agent.respond`。 |
| `requires_finalizer` | finalizer 依赖最后一个 Workbench verifier；dependency context 可包含 safe digest。 |
| `none` + `finalizer_digest_mode=none` | 不新增 finalizer。 |
| `none` + `finalizer_digest_mode=required` | 新增 task-level finalizer，依赖最后一个 verifier。 |

进入 finalizer 的 Workbench payload 只允许：`summary`、`highlights`、`caveats`、`structured_content.safe_digest`、`satisfaction`。禁止字段必须在 prompt builder 层再次过滤。

## 5. 测试计划

| 测试 | 断言 |
| --- | --- |
| fixed DAG validity | 插入 Workbench 后 plan 仍无环、依赖存在、capability 已注册。 |
| direct no duplicate finalizer | `answer_mode=direct` 不新增第二个 `main_agent.respond`。 |
| requires finalizer digest | finalizer 依赖 verifier，dependency context 包含 safe digest。 |
| none required finalizer | 只有显式 `finalizer_digest_mode=required` 才新增 finalizer。 |
| multi skill finalizer | 多 Skill 汇总不重复 finalizer，不漏 Workbench verifier 依赖。 |
| no LLM workbench | LLM planner / replanner 不产生 `workbench.*`。 |
| plan metadata safe | Workbench strategy metadata 不含敏感字段。 |

推荐命令：

```bash
python -m pytest tests/orchestration/ -k "skill_workflow_provider or workflow_expander or workbench"
python -m pytest tests/capabilities/main_agent/ -k "prompt or dependency"
```

## 6. 阶段验收

- fixed DAG 可在 feature flag `fixed_dag` 下生成并通过 validator。
- answer mode 行为与总纲一致。
- finalizer 只消费 safe digest。
- 阶段三脱敏门禁未通过前，不得面向普通前端视图放量。
