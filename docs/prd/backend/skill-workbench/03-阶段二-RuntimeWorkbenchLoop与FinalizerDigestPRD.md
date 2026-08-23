# 阶段二：Runtime Workbench Loop 与 Finalizer Digest PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **编号**：后端 PRD 22-Phase 2
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段零 policy / runtime state、阶段一 internal capability / executor
- **下游阶段**：阶段三事件 / graph / prompt 脱敏门禁、阶段四 contract diagnostics
- **目标模块**：`src/orchestration/workbench_replanner.py`、`src/orchestration/runtime_replanner.py`、`src/orchestration/service.py`、`src/capabilities/main_agent/prompt_builder.py`、`tests/orchestration/`

## 1. 阶段目标

实现最终运行闭环主线：`WorkbenchRuntimeReplanner` 观察完成的 `skill.*` 或 `workbench.*` output，按策略追加下一批后置内部 Workbench nodes 和必要 finalizer；同能力输入改写 retry 由独立 public-only `SkillRefinementRuntimeReplanner` 追加新的 `skill.*` 节点。

1. `CompositeRuntimeReplanner` 顺序必须是 `WorkbenchRuntimeReplanner` -> `SkillRefinementRuntimeReplanner` -> LLM `MainAgentRuntimeReplanner`。
2. `WorkbenchRuntimeReplanner` 只读取安全 output、policy 和 `WorkbenchReplanState`，不调用 LLM。
3. `WorkbenchRuntimeReplanner` 只追加 `post_skill_stages` 中的后置 `workbench.*` 或必要 `main_agent.respond` finalizer，不改写已存在 node capability / dependencies，不回插 preflight。
4. `SkillRefinementRuntimeReplanner` 只读取 Workbench safe digest 中的 `refinement_possible` hint 和 public Skill metadata，只追加 public `skill.*` retry 节点，不生成 `workbench.*`。
5. `answer_mode=direct` 默认不新增第二个 finalizer。
6. `answer_mode=requires_finalizer` 的 finalizer 必须等待必要 Workbench verifier 完成后再消费 digest。
7. `answer_mode=none` 只有 `finalizer_digest_mode=required` 时才新增 task-level finalizer。
8. Finalizer 综合 Skill output 与 Workbench safe digest；Skill output 继续走原有输出链路。

## 2. 范围

### In scope

- 新增 deterministic `WorkbenchRuntimeReplanner`。
- 新增 deterministic public-only `SkillRefinementRuntimeReplanner`。
- `CompositeRuntimeReplanner` 顺序调整：Workbench deterministic replanner 先处理后置验证 / finalizer；无 Workbench decision 时，Skill refinement replanner 才可基于 safe hint 追加 public `skill.*` retry；最后才进入 LLM replanner。
- 根据 `satisfaction.replan_recommended`、missing output、artifact policy、domain policy 追加 post-skill stage。
- pending finalizer 处理策略：不得原地修改已存在依赖；必要时新增 finalizer 或 orphan pending finalizer。
- Workbench node metadata：`internal_node`、`workbench_stage`、`target_skill_node_id`、`target_capability_id`。
- Workbench node id 使用稳定 opaque 命名，真实 stage 只进内部 metadata / audit。
- Finalizer digest dependency context 的 Workbench 专用 sanitizer 接入点。

### Out of scope

- 不采用 initial expansion 固定后置 DAG 作为主路径；但允许 initial expansion 按 policy 插入 metadata-only `preflight_validate` 前置节点。
- 不改 public plan schema。
- 不新增 SSE event schema 或 graph DTO 字段。
- 不要求所有 Skill 修改 contract。
- 不允许 LLM replanner 生成 Workbench nodes。

## 3. Runtime DAG 规则

### 3.1 追加位置

典型后置链路：

```text
skill_execute
  -> WorkbenchRuntimeReplanner 追加 workbench.artifact_inspect / workbench.report_verify
  -> WorkbenchRuntimeReplanner 必要时追加 main_agent.respond finalizer
```

如果存在前置 preflight，它必须已经由 initial expansion / policy expansion 在 Skill node 创建前插入：

```text
workbench.preflight_validate -> skill_execute
```

metadata-only preflight 只能检查 Skill contract 摘要、input schema / output contract 元信息、artifact metadata、resource policy 和 platform policy；不得读取完整文件、完整 rows、storage key、本地路径，也不得声称已经验证 Skill 最终 resolved inputs。基于已解析输入的验证必须等后续提供 safe input digest seam 后再做。

`WorkbenchRuntimeReplanner` 不得在 Skill node 已存在、已 pending 或已执行后回插 preflight；如果 initial expansion 未插入 preflight，runtime 阶段只能继续执行后置检查或记录 audit caveat。

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

- Workbench 后置追加节点和 public Skill refinement retry 都受 initial plan `max_replans` / `max_dynamic_nodes` 限制。
- 普通 Skill 默认 `max_replans=0`、`max_dynamic_nodes=0`。
- 后续 revised plan 不得提升预算。
- 预算不足时不追加节点，记录安全 reason code；不得声称验证通过或已完成补救。
- `max_same_capability_refinements` 单独限制同一 public capability 的输入改写 retry 次数；默认不超过 1。

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

## 6. Workbench Replan 停止条件与同能力输入改写

`WorkbenchRuntimeReplanner` 必须按有限状态机推进，不能把 `satisfaction.replan_recommended=true` 解释为无条件继续 replan。

### 6.1 Replan 前置停止门禁

出现任一情况时，WorkbenchRuntimeReplanner 必须返回 no decision，不得追加节点：

1. task 已经是 terminal / cancelling / cancelled，或 orchestration context 有 unresolved interrupt。
2. 当前已有未完成的 Workbench 内部节点，必须等待其完成后再判断下一步。
3. 当前已有 pending finalizer 且该 finalizer 等待的 verifier 未完成，不能再追加第二个同义 finalizer。
4. `replan_count >= max_replans` 或追加节点会超过 `max_dynamic_nodes`。
5. WorkbenchReplanState 已有 terminal state 或 wait state；wait state 必须等待现有 interrupt / resume 或 pending node 完成后再判断。
6. 没有未执行且对当前状态有意义的 stage，也不需要追加 finalizer。
7. 本次候选动作不会改变 progress marker：不减少 pending stage、不增加 completed / failed stage、不追加 finalizer、不改变 input fingerprint、不进入 terminal / wait state。
8. 候选同能力 retry 已达到 `max_same_capability_refinements`。

### 6.2 单次 replan 必须满足的条件

每次 replan 必须满足：

1. initial plan 仍有 `max_replans` / `max_dynamic_nodes` 预算。
2. 存在未执行且对当前状态有意义的 post-skill Workbench stage，或需要追加必要 finalizer，或允许一次同能力 refinement retry。
3. 本次动作会让 `WorkbenchReplanState` 单调前进：减少 pending post-skill stage、增加 completed / failed stage、追加 finalizer、改变 retry input fingerprint，或进入 terminal / wait state。
4. 不重复执行相同 stage + 相同 failure reason。
5. 不重复执行相同 capability + 相同 input fingerprint。
6. 不绕过必须由用户提供的信息。

### 6.3 Replan stop / wait states

`needs_user_input` 不表示 Workbench 最终失败；它必须复用平台已有 interrupt / clarification / resume 机制。Workbench 的职责是停止本轮 replan、记录安全缺失摘要并让编排层进入等待；用户补充信息后，runtime 按现有 resume 流程恢复，再重新评估是否需要新的 Skill / Workbench 节点。

| State | 类型 | 含义 | 处理 |
| --- | --- | --- |
| `verified` | terminal | Workbench 验证已满足 | 停止 replan；如需 finalizer，则追加 / 放行 finalizer。 |
| `unsatisfied_terminal` | terminal | 当前 Skill output 不满足，但没有可用补救 stage | 停止 replan；finalizer 输出 caveat 或任务失败。 |
| `unsupported_by_capability` | terminal | 当前 public capability 无法解决该任务 | 停止 replan；不得继续追加同能力检查。 |
| `needs_user_input` | wait | 需要用户补充必需信息 | 停止本轮 Workbench replan；触发或复用已有 interrupt / clarification；resume 后重新评估。 |
| `budget_exhausted` | terminal | replan 或 dynamic node 预算耗尽 | 停止；记录未完成验证。 |
| `unsafe_digest` | terminal | digest 超限或含禁止字段 | 停止；fail closed，不进 finalizer。 |
| `direct_audit_done` | terminal | direct Skill 的 audit / health 已完成 | 停止；不追加第二个回答。 |
| `no_progress` | terminal | 候选 replan 不会改变 stage / finalizer / fingerprint / terminal / wait state | 停止；记录 no-progress reason。 |
| `finalizer_pending` | wait | 已有 pending finalizer 等待 verifier 或最终输出 | 停止追加同义 finalizer；等待现有节点。 |
| `workbench_pending` | wait | 已有 Workbench 内部节点未完成 | 停止本轮 replan；等待内部节点完成。 |
| `refinement_exhausted` | terminal | 同能力输入改写 retry 达上限或 fingerprint 未变化 | 停止 retry；如缺少用户必填信息则转 `needs_user_input` wait，否则进入 `unsatisfied_terminal`。 |

Terminal / wait state 必须写入 safe audit / Workbench digest。Finalizer 如存在，只能陈述验证结论、未解决原因和下一步，不能声称已修复或已验证通过；`needs_user_input` 场景下 finalizer 不得抢在 interrupt/resume 之前输出最终结论。

### 6.4 Refinement possible：同一能力输入改写重试

存在一类通用情况：当前结果未满足，但不是 capability 不支持，也不是必须由用户补充信息；同一个 public capability 可能通过更清晰的输入、补全上下文、缩小范围或调整表达方式再次执行后得到结果。

Workbench 可输出 safe hint：

```json
{
  "satisfaction": {
    "satisfied": false,
    "reason_code": "input_underspecified",
    "replan_recommended": true
  },
  "structured_content": {
    "safe_digest": {
      "failure_kind": "refinement_possible",
      "retry_same_capability_allowed": true,
      "missing_constraints": ["时间范围", "对象名称"]
    }
  }
}
```

规则：

1. Workbench 不生成 refined query、不写业务 prompt、不针对具体 Skill 名称定制。
2. Workbench 只提供 `failure_kind=refinement_possible`、safe caveats、缺失约束摘要和是否允许同能力 retry。
3. 实际同能力 retry 必须由独立 `SkillRefinementRuntimeReplanner` 追加新的 `skill.*` 节点完成；该 replanner 仍只能输出 public DAG，不能生成 `workbench.*`。
4. 同一 capability 的 refinement retry 必须有独立上限 `max_same_capability_refinements`，并计入 `max_replans` / `max_dynamic_nodes`。
5. 每次 retry 必须改变输入 fingerprint；如果 refined input 与上次等价，禁止重试。fingerprint 必须基于 public `skill.*` input payload 的规范化安全摘要，不包含 raw file、path、storage_key、SQL 或 secret。
6. 连续出现相同 failure reason 或同一 missing constraint 集合时，停止 retry；若缺的是用户必填信息，进入 `needs_user_input` wait，否则进入 `unsatisfied_terminal`。
7. 如果缺失的是必须由用户提供的信息，必须停止本轮 Workbench replan 并交给已有 interrupt / clarification / resume 机制，不得由主代理猜测。
8. retry 后仍不满足，进入 terminal state 或 `needs_user_input` wait；不得无限尝试。
9. `SkillRefinementRuntimeReplanner` 可以使用用户原始请求、public Skill descriptor、上一轮 public Skill input、安全 failure kind / missing constraints 生成更清晰的 public input payload；不得读取 Workbench 内部 stage、raw output、完整文件内容、路径、storage key、SQL、schema DDL、handler、runtime 或 secret。

### 6.5 当前能力无法解决时的停止规则

如果 Workbench 判断 required output 缺失、required artifact 缺失、domain boundary blocking、Skill 明确返回 `is_error=true`、输出表明 capability 不支持用户目标，或连续两个 stage 得出同一类 blocking reason，则不得继续 replan 同一个 Skill 链路。

- `requires_finalizer`：追加或放行 finalizer，但 finalizer 必须说明当前能力未完成目标、已完成的验证、缺失内容和下一步。
- `direct`：不改变 direct answer，不追加第二个 finalizer；只记录 audit / health。
- `none + required finalizer`：按 required finalizer 处理，输出失败原因或 caveat。

## 7. 测试计划

| 测试 | 断言 |
| --- | --- |
| append next stage | Skill output 完成后可追加下一批 Workbench nodes。 |
| no mutation | revised plan 不改写已有 node dependencies / capability。 |
| opaque node id | Workbench node id 不包含 capability / stage 名称。 |
| budget respected | 超过 `max_replans` 或 `max_dynamic_nodes` 时 fail closed 并审计。 |
| replanner ordering | 顺序为 Workbench -> Skill refinement -> LLM；Workbench 先处理后置验证 / finalizer，Skill refinement 只追加 public `skill.*`，LLM 仍只能输出 public DAG。 |
| direct no duplicate finalizer | `answer_mode=direct` 不新增第二个 `main_agent.respond`。 |
| requires finalizer digest | finalizer 等待 Workbench verifier，dependency context 包含 safe digest。 |
| finalizer safety | pending finalizer 不提前消费未验证 Skill output。 |
| resume dedupe | resume 后不会重复执行已完成 Workbench stage。 |
| workbench failure matrix | execution failure / verification failure / replan unavailable 按矩阵处理。 |
| terminal / wait states | verified / unsupported / budget_exhausted / unsafe_digest 等 terminal state 停止 replan；needs_user_input / pending 类 wait state 暂停本轮 replan 并等待 interrupt / resume 或现有节点完成。 |
| refinement retry bounded | `refinement_possible` 只触发 `SkillRefinementRuntimeReplanner` 的有限同能力 retry，且输入 fingerprint 必须变化。 |
| repeated failure stops | 相同 stage + failure reason 或相同 missing constraints 不重复 retry。 |
| no progress stops | 候选 replan 不改变 progress marker 时进入 no-progress 停止。 |
| preflight not backfilled | preflight 只能由 initial expansion 前置插入；runtime replanner 不回插、不改写 Skill dependency。 |
| pending internal nodes stop | 已有 pending Workbench 或 pending finalizer 时不追加同义节点。 |
| terminal task stop | task terminal / cancelling / unresolved interrupt 时 Workbench replanner 不追加节点。 |

推荐命令：

```bash
python -m pytest tests/orchestration/test_workbench_replanner.py
python -m pytest tests/orchestration/ -k "runtime_replanner or dynamic_nodes or workbench"
python -m pytest tests/capabilities/main_agent/ -k "prompt or dependency"
```

## 8. 阶段验收

- runtime loop 只追加后置内部节点或 public Skill retry，且严格受预算限制。
- LLM replanner 和 Skill refinement replanner 都不能生成 `workbench.*`。
- Workbench node id 不泄漏内部 stage。
- finalizer 不提前消费未验证 output。
- answer mode、failure matrix 与总纲一致。
- 阶段三脱敏门禁未通过前，不得进入生产部署。
