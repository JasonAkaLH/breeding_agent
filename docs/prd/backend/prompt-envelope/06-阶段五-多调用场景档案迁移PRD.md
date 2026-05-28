# 阶段五 PRD —— 多调用场景档案迁移

- **日期**：2026-05-29
- **状态**：待实施
- **父实施计划**：`docs/orchestration/大语言模型提示词信封与缓存友好上下文组装实施计划.md`
- **所属专题**：大语言模型提示词信封
- **范围**：Soft Skill decision / answer、Planner / repair、Runtime Replanner、Skill input resolver、conversation memory resolver / summary 的 PromptEnvelope profile
- **非范围**：不启用 messages-native；不改变 public capability validation；不改变 conversation memory 存储模型

## 1. 问题陈述

阶段二至阶段四优先解决主代理回答，但仓库中仍存在多处手写 LLM prompt：Soft Skill 判断/答疑、Planner、Planner repair、Runtime Replanner、Skill 参数补槽、conversation memory 实体补全和摘要。如果这些路径不纳入 profile 管理，系统仍然存在散落 prompt、审计盲区和安全口径不一致问题。

## 2. 目标

1. 建立 profile registry 或等价工厂，按调用场景选择稳定规则、上下文 segment 和输出 guard。
2. Soft Skill decision 使用 decision profile，answer 使用 public-answer profile，并保持流式答疑。
3. Planner / repair 使用 planner profile，继续保证 JSON-only、public capability-only 和 repair 校验。
4. Runtime Replanner 使用 replan profile，禁止内部 capability / handler / Skill 阶段输出。
5. Skill input resolver 使用 resolver profile，只接收 schema、current request、active notes、artifact summaries、answer payload 和少量 clarification。
6. Conversation memory resolver / summary 使用 memory profiles 或明确记录旧路径 fallback audit。

## 3. 非目标

- 不让 Soft Skill slash command 变回硬执行接口。
- 不放宽 planner validator / workflow expander 的 fail-closed 规则。
- 不把完整 conversation memory 都交给 Skill resolver。
- 不将用户原文自动提升为事实性 active notes。

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P5-FR-1 | Soft Skill decision 必须 profile 化。 | prompt/audit 有 `soft_skill_decision` template；answer/execute 判断保持现有 API 语义。 |
| P5-FR-2 | Soft Skill answer 必须继续流式。 | 答疑路径仍产生 `main_agent.output_delta`，追问能使用历史上下文。 |
| P5-FR-3 | Planner 与 repair 必须 profile 化或有 fallback audit。 | JSON plan 仍可 parse/validate；repair prompt 限制上一轮 raw output 长度。 |
| P5-FR-4 | Runtime Replanner 不得成盲区。 | replan prompt 生成 audit；输出仍只允许 public DAG。 |
| P5-FR-5 | Skill input resolver 必须限制上下文。 | 不接收完整 memory；artifact 只使用 summaries；无法确定的字段进入 missing。 |
| P5-FR-6 | Memory resolver/summary 必须受控。 | resolver 仍只做高置信实体补全，不回答用户问题、不选择 capability；summary 只做忠实摘要。 |

## 5. 非功能需求

- **Consistency**：所有 profile 使用统一 audit schema 和 mode 开关。
- **Safety**：JSON guard 不替代后端 validator；LLM 输出仍必须被解析/校验。
- **Compatibility**：`off` 模式保留所有旧 prompt 路径。

## 6. 实施计划

1. 在 PromptEnvelope 层增加 `template_id` / `template_version` 命名约定。
2. 迁移 Soft Skill decision / answer prompt builder，并更新 API 回归。
3. 迁移 `src/orchestration/planner_contract.py` 的 planner / repair prompt。
4. 迁移 `src/capabilities/main_agent/runtime_replanner.py` 的 replan prompt。
5. 迁移 `src/integrations/codex_skills/input_resolution.py` 的 LLM slot prompt。
6. 为 `src/orchestration/conversation_memory.py` 的 resolver / summary prompt 加 profile 或 fallback audit。
7. 每条路径均新增 audit no-raw-content 测试。

## 7. 验收标准

- `conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding tests.api.test_skill_input_resolution_runtime tests.api.test_runtime_replanner` 通过。
- `conda run -n multi_agent python -m unittest tests.orchestration.test_planner_contract tests.orchestration.test_llm_workflow_provider` 通过。
- `/skill` 追问、流式答疑、interrupt 缺参 API 回归通过。
- Runtime Replanner、memory resolver / summary 不再是未审计 prompt 盲区。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 一次性迁移路径过多。 | 按 profile 子路径分小提交；每个子路径保留 `off` fallback。 |
| Planner 输出格式受 prompt 重排影响。 | 保留 JSON schema guard，并继续使用 parse/validate/repair。 |
| Skill resolver 从历史误抽参数。 | resolver profile 只带少量 clarification 和 active notes，缺置信时 missing。 |
