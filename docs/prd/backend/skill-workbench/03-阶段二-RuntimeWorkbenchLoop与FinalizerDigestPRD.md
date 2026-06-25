# 阶段二：Runtime Workbench Loop 与 Finalizer Digest PRD

- **编号**：后端 PRD 22-Phase 2
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段零 policy / runtime state、阶段一 internal capability / executor
- **下游阶段**：阶段三事件 / graph / prompt 脱敏门禁、阶段四 contract diagnostics
- **目标模块**：`src/orchestration/workbench_replanner.py`、`src/orchestration/runtime_replanner.py`、`src/orchestration/service.py`、`src/capabilities/main_agent/prompt_builder.py`、`tests/orchestration/`

## 1. 阶段目标

实现最终运行闭环主线：`WorkbenchRuntimeReplanner` 观察完成的 `skill.*` 或 `workbench.*` output，按策略追加下一批内部 Workbench nodes 和必要 finalizer。

1. `WorkbenchRuntimeReplanner` 放在 LLM `MainAgentRuntimeReplanner` 前。
2. Replanner 只读取安全 output、policy 和 `WorkbenchReplanState`，不调用 LLM。
3. 只追加 `workbench.*` 或必要 `main_agent.respond` finalizer，不改写已存在 node capability / dependencies。
4. `answer_mode=direct` 默认不新增第二个 finalizer。
5. `answer_mode=requires_finalizer` 的 finalizer 必须等待必要 Workbench verifier 完成后再消费 digest。
6. `answer_mode=none` 只有 `finalizer_digest_mode=required` 时才新增 task-level finalizer。
7. Finalizer 综合 Skill output 与 Workbench safe digest；Skill output 继续走原有输出链路。

## 2. 范围

### In scope

- 新增 deterministic `WorkbenchRuntimeReplanner`。
- `CompositeRuntimeReplanner` 顺序调整：Workbench deterministic replanner 先于 LLM replanner。
- 根据 `satisfaction.replan_recommended`、missing output、artifact policy、domain policy 追加 stage。
- pending finalizer 处理策略：不得原地修改已存在依赖；必要时新增 finalizer 或 orphan pending finalizer。
- Workbench node metadata：`internal_node`、`workbench_stage`、`target_skill_node_id`、`target_capability_id`。
- Workbench node id 使用稳定 opaque 命名，真实 stage 只进内部 metadata / audit。
- Finalizer digest dependency context 的 Workbench 专用 sanitizer 接入点。

### Out of scope

- 不采用 initial expansion 固定 DAG 作为主路径。
- 不改 public plan schema。
- 不新增 SSE event schema 或 graph DTO 字段。
- 不要求所有 Skill 修改 contract。
- 不允许 LLM replanner 生成 Workbench nodes。

## 3. Runtime DAG 规则

### 3.1 追加位置

典型链路：

```text
skill_execute
  -> WorkbenchRuntimeReplanner 追加 workbench.artifact_inspect / workbench.report_verify
  -> WorkbenchRuntimeReplanner 必要时追加 main_agent.respond finalizer
```

如果存在前置 preflight，阶段二只允许 metadata-only preflight：

```text
workbench.preflight_validate -> skill_execute
```

metadata-only preflight 只能检查 Skill contract 摘要、input schema / output contract 元信息、artifact metadata、resource policy 和 platform policy；不得读取完整文件、完整 rows、storage key、本地路径，也不得声称已经验证 Skill 最终 resolved inputs。基于已解析输入的验证必须等后续提供 safe input digest seam 后再做。

### 3.2 Node metadata 与 node_id

每个 Workbench node 必须包含内部 metadata：

```json
{
  "internal_node": true,
  "workbench_stage": "artifact_inspect",
  "target_skill_node_id": "task:skill_execute",
  "target_capability_id": "skill.<id>"
}
```

metadata 不得包含 handler、runtime、entrypoint、path、storage key、raw payload。

Workbench node id 必须使用稳定 opaque 命名，例如：

```text
{task_id}:internal_validation:{index}
```

不得在 node id 中包含 `workbench`、`schema_match`、`artifact_inspect`、`report_verify` 等内部 capability 或 stage 名称。真实 stage 只保留在内部 metadata / audit，不进入前端事件、graph API 或用户可见展示。

### 3.3 Budget

- Workbench 追加节点受 initial plan `max_replans` / `max_dynamic_nodes` 限制。
- 普通 Skill 默认 `max_replans=0`、`max_dynamic_nodes=0`。
- 后续 revised plan 不得提升预算。
- 预算不足时不追加节点，记录安全 reason code；不得声称验证通过或已完成补救。

## 4. Finalizer digest 规则

| answer mode / policy | 行为 |
| --- | --- |
| `direct` + 默认策略 | Workbench 可做 audit/health，但不新增 `main_agent.respond`，不改变 direct answer。 |
| `requires_finalizer` | finalizer 必须等待必要 Workbench verifier；dependency context 包含 Skill output 和 Workbench safe digest。 |
| `none` + `finalizer_digest_mode=none` | 不新增 finalizer。 |
| `none` + `finalizer_digest_mode=required` | 新增 task-level finalizer，依赖必要 Workbench verifier。 |

Workbench 是独立节点、独立 output；Skill output 仍按原有 Skill 输出链路处理。Finalizer 应综合任务过程中的 Skill output 与 Workbench 验证过程，但 Workbench 过程只以 safe digest 形态进入 prompt，不得把内部 stage、路径、raw output、SQL、schema、handler、runtime 或 secret 作为过程上下文传入。

## 5. Workbench 失败语义

| 类型 | 含义 | 处理原则 |
| --- | --- | --- |
| Workbench execution failure | executor 异常、digest schema 不合法、输出超限、sanitizer 发现禁止字段、artifact metadata 不可读 | 不得声称验证通过；按 answer mode 阻断或 audit/health 记录。 |
| Verification failure | Skill 输出不满足 output contract、required artifact 缺失、report/domain policy blocking | `requires_finalizer` 下必须阻断或进入 finalizer caveat；`direct` 默认 audit/health 非阻断。 |
| Replan unavailable | 建议 replan 但预算不足、validator 拒绝或动态节点不足 | 记录 audit；finalizer 如存在必须说明验证/补救不足，不得说已修复。 |

Criticality 规则：

| 场景 | Criticality / 失败语义 |
| --- | --- |
| `requires_finalizer` 且 Workbench verifier 是 finalizer 依赖 | REQUIRED；失败时 finalizer 不得声称验证通过。 |
| `direct` 默认 audit/health Workbench | 非阻断；失败只进 audit / health，不追加第二个回答，不改变 direct answer。 |
| `answer_mode=none` 且策略要求 finalizer | REQUIRED。 |
| 禁止字段 / digest 超限 / 敏感字段检测失败 | blocking / fail closed；不能把不安全 digest 交给 finalizer。 |
| required artifact / required output 缺失 | blocking。 |
| optional artifact / optional output 缺失 | caveat。 |

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| append next stage | Skill output 完成后可追加下一批 Workbench nodes。 |
| no mutation | revised plan 不改写已有 node dependencies / capability。 |
| opaque node id | Workbench node id 不包含 capability / stage 名称。 |
| budget respected | 超过 `max_replans` 或 `max_dynamic_nodes` 时 fail closed 并审计。 |
| replanner ordering | Workbench replanner 在 LLM replanner 前，LLM 仍只能输出 public DAG。 |
| direct no duplicate finalizer | `answer_mode=direct` 不新增第二个 `main_agent.respond`。 |
| requires finalizer digest | finalizer 等待 Workbench verifier，dependency context 包含 safe digest。 |
| finalizer safety | pending finalizer 不提前消费未验证 Skill output。 |
| resume dedupe | resume 后不会重复执行已完成 Workbench stage。 |
| workbench failure matrix | execution failure / verification failure / replan unavailable 按矩阵处理。 |

推荐命令：

```bash
python -m pytest tests/orchestration/test_workbench_replanner.py
python -m pytest tests/orchestration/ -k "runtime_replanner or dynamic_nodes or workbench"
python -m pytest tests/capabilities/main_agent/ -k "prompt or dependency"
```

## 7. 阶段验收

- runtime loop 只追加内部节点且严格受预算限制。
- LLM replanner 不知道也不能生成 `workbench.*`。
- Workbench node id 不泄漏内部 stage。
- finalizer 不提前消费未验证 output。
- answer mode、failure matrix 与总纲一致。
- 阶段三脱敏门禁未通过前，不得进入生产部署。
