# 阶段一：Plan Metadata 契约 PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **Status**：Ready for implementation
- **Date**：2026-06-25
- **Parent PRD**：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`
- **Phase Goal**：让 `capability_missing_fallback` 成为 Planner/Replanner 可输出、schema 可校验、repair prompt 可保留、parser 可传播的收敛 plan contract。

## 1. In Scope

1. 扩展 `PLANNER_OUTPUT_JSON_SCHEMA`：
   - 顶层允许 `metadata`；
   - node 允许 `metadata`；
   - `metadata.capability_missing_fallback` 使用收敛 schema；
   - `additionalProperties` 仍保持收敛。
2. 扩展 repair prompt：合法 fallback metadata 不得被删除；非法 metadata 应被修复为父 PRD 定义字段或按 planner 错误处理。
3. 扩展 `build_plan_from_llm_output()`：将顶层 metadata 和 node metadata 解析到 `WorkflowPlan` / `WorkflowNodePlan`。
4. 定义最小 sanitizer / validator：
   - required fields：`enabled`、`scope`、`reason_code`、`missing_capability_summary`、`fallback_content_scope`、`llm_fallback_allowed`、`artifact_generation_allowed`、`disclosure_required`；
   - partial additionally requires `attempted_capability_summary`；
   - 不接受 handler、runtime、source path、secret、raw prompt、完整历史等字段。

## 2. Out of Scope

- Planner 尚不必真的主动选择 fallback。
- Runtime 尚不发送 `capability.missing_fallback`。
- MainAgent prompt / 正文后处理 / history metadata 持久化留到阶段二。
- 前端 notice 留到阶段三。

## 3. Functional Requirements

| 编号 | 要求 | 验收 |
| --- | --- | --- |
| P1-R1 | schema 接受顶层 `metadata.capability_missing_fallback`。 | planner_contract 单测。 |
| P1-R2 | schema 接受 final node `metadata.capability_missing_fallback`。 | planner_contract 单测。 |
| P1-R3 | parser 将 metadata 写入 plan/node 模型。 | build_plan 单测。 |
| P1-R4 | repair prompt 保留合法 fallback metadata。 | repair prompt / provider 单测。 |
| P1-R5 | 未知字段和敏感字段被拒绝或净化。 | schema / sanitizer 单测。 |
| P1-R6 | 普通 `main_agent.respond` 不需要 fallback metadata。 | 回归测试。 |

## 4. Edge Cases

| 场景 | 期望 |
| --- | --- |
| plan metadata 有 fallback，node metadata 缺失 | parser 保留 plan metadata；阶段二 Runtime 会用更保守披露策略补传。 |
| node metadata 有 fallback，plan metadata 缺失 | parser 保留 node metadata；阶段二 Runtime 可提升到 task/event/history metadata。 |
| plan/node fallback 不一致 | 本阶段只保留事实；阶段二运行时选择更保守披露策略。 |
| `scope=partial` 但缺少 `attempted_capability_summary` | schema/validator 拒绝或 repair。 |
| LLM 输出不存在 capability id | 仍按既有 capability validation 拒绝，不用 metadata 掩盖。 |

## 5. 测试计划

```bash
python -m pytest tests/orchestration/ -k "planner_contract or build_plan or repair"
```

如测试文件尚不存在，应新增靠近 `src/orchestration/planner_contract.py` 和 `llm_workflow_provider` 的单元测试。

## 6. 完成标准

- fallback metadata 可以合法出现在 plan 和 final node 上。
- schema 和 parser 均不会丢弃合法 metadata。
- 仍不允许 LLM 任意扩展 metadata 或编造 capability id。
- 可以进入阶段二后端 full fallback 闭环。
