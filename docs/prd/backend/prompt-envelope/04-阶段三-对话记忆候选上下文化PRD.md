# 阶段三 PRD —— 对话记忆候选上下文化

- **日期**：2026-05-29
- **状态**：待实施
- **父实施计划**：`docs/orchestration/大语言模型提示词信封与缓存友好上下文组装实施计划.md`
- **所属专题**：大语言模型提示词信封
- **范围**：conversation memory candidate payload、动态历史预算接入、history / clarification / capability summary 优先级、memory audit 字段
- **非范围**：不改变历史落库模型；不修复 Skill artifact 继承；不改前端历史展示

## 1. 问题陈述

当前 conversation memory 在内部使用 `actual_memory_budget` 决定最终可带入历史长度，该预算固定扣除 25% 作为系统预留，无法根据本次真实 system/tool/user/dependency token 占用动态释放空间。阶段三要把 memory 从“最终预算决策者”降级为“候选上下文提供者”，由 PromptAssembler 在完整 prompt 视角下计算历史预算。

## 2. 目标

1. 新增 memory candidate 数据结构，保留旧 `to_prompt_payload()` 兼容路径。
2. history summary、recent messages、clarification messages、capability summaries 分别标注 priority、trim policy 和 token estimate。
3. PromptAssembler 根据 `trim_max_tokens - non_history_tokens - safety_margin` 装入 history candidates。
4. current-task clarification / accepted answer / artifact summaries 优先于旧 history。
5. audit 区分 candidate tokens、bulk history budget、bulk history used 和 truncation reason。

## 3. 非目标

- 不取消 conversation memory summary 生成。
- 不把用户自由文本提升为 active continuity facts。
- 不改变 task/conversation 数据库 schema。
- 不把 artifact 原始内容放入 prompt。

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P3-FR-1 | 必须新增 candidate 输出。 | `ConversationMemoryContext` 或 adapter 能输出 summary/recent/clarification/capability candidates。 |
| P3-FR-2 | 最终历史预算必须由 PromptAssembler 计算。 | 长历史测试证明可用 history budget 接近 `trim_max_tokens - non_history - margin`，不是固定 75%。 |
| P3-FR-3 | clarification 优先级必须高于旧 history。 | 超预算时保留当前任务补充信息，优先裁剪较早 recent/history summary。 |
| P3-FR-4 | token fallback 必须增加 margin。 | token counter 不可用时 audit 标记 fallback，并使用更保守 margin。 |
| P3-FR-5 | audit 必须记录 memory 裁剪。 | 包含 `candidate_history_tokens`、`bulk_history_budget`、`bulk_history_tokens_used`、`history_truncated`。 |

## 5. 非功能需求

- **Safety**：memory 是上下文，不是指令；prompt 必须继续标注“历史数据，不是系统指令”。
- **Reliability**：summary LLM 失败仍走既有 fallback，不阻断主代理回答。
- **Compatibility**：`off` 模式继续使用旧 memory payload。

## 6. 实施计划

1. 为 `ConversationMemoryContext` 增加 candidate adapter，而不是直接删除 `to_prompt_payload()`。
2. 在 PromptEnvelope renderer 中实现 history candidates 装载与裁剪。
3. 更新主代理 string 模式，使用 candidate path。
4. 为长历史、clarification、capability summaries、fallback token counter 添加测试。
5. 保留 `off` 路径旧行为，便于回滚。

## 7. 验收标准

- `conda run -n multi_agent python -m unittest tests.orchestration.test_conversation_memory` 通过。
- `conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt` 通过。
- 长历史测试证明不再固定 75%。
- audit 可解释 history 裁剪原因。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 历史过多挤占工具结果。 | 工具结果为 required segment；history 只使用 flexible budget。 |
| clarification 被裁掉导致追问失效。 | clarification / current task notes 设更高优先级并测试。 |
| summary 与原文矛盾。 | prompt 标注 summary 非逐字原文；用户纠正和近期原文优先。 |
