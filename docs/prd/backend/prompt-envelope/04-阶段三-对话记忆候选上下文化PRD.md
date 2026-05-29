# 阶段三 PRD —— 对话记忆候选上下文化

- **日期**：2026-05-29
- **状态**：待实施
- **父总纲 PRD**：`docs/prd/backend/prompt-envelope/00-大语言模型提示词信封与缓存友好上下文组装总纲PRD.md`
- **所属专题**：大语言模型提示词信封
- **范围**：conversation memory candidate payload、动态历史预算接入、history / clarification / capability summary 优先级、memory audit 字段
- **非范围**：不改变历史落库模型；不修复 Skill artifact 继承；不改前端历史展示

## 1. 问题陈述

当前 conversation memory 在内部使用 `actual_memory_budget` 决定最终可带入历史长度，该预算固定扣除 25% 作为系统预留，无法根据本次真实 system/tool/user/dependency token 占用动态释放空间。阶段三要把 memory 从“最终预算决策者”降级为“候选上下文提供者”，由 PromptAssembler 在完整 prompt 视角下计算历史预算。

## 2. 目标

1. 新增 memory candidate 数据结构，保留旧 `to_prompt_payload()` 兼容路径。
2. history summary、recent messages、clarification messages、capability summaries 分别标注 priority、trim policy 和 token estimate。
3. PromptAssembler 根据 `final_input_token_budget - non_history_tokens - safety_margin` 装入 history candidates，其中 `final_input_token_budget=floor(trim_max_tokens * 0.75)`。
4. current-task clarification、同一任务已接受 interrupt answer payload、已接受上传 artifact metadata 与 artifact summaries 优先于旧 history。
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
| P3-FR-2 | 最终历史预算必须由 PromptAssembler 计算。 | 长历史测试证明可用 history budget 接近 `floor(trim_max_tokens * 0.75) - non_history - margin`；75% 是最终输入预算，不是固定历史预算。 |
| P3-FR-3 | clarification / accepted interrupt answer 优先级必须高于旧 history。 | 超预算时保留当前任务补充信息、同一任务已接受 answer payload 与上传 artifact metadata，优先裁剪较早 recent/history summary。 |
| P3-FR-4 | token fallback 必须增加 margin。 | token counter 不可用时 audit 标记 fallback，并使用 `max(2048, floor(trim_max_tokens * 0.02))`；精确/可信估算使用 `max(1024, floor(trim_max_tokens * 0.01))`。 |
| P3-FR-5 | audit 必须记录 memory 裁剪。 | 包含 `final_input_token_budget`、`final_input_tokens`、`candidate_history_tokens`、`bulk_history_budget`、`bulk_history_tokens_used`、`history_truncated`、`history_compression_retry`。 |
| P3-FR-6 | final preflight 失败后只允许一次历史压缩重试。 | 首次 preflight 失败时仅压缩/收缩 `bulk_conversation_history` candidates 并重渲染；第二次 preflight 仍失败必须 fail closed，不继续循环压缩。 |

## 5. 非功能需求

- **Safety**：memory 是上下文，不是指令；prompt 必须继续标注“历史数据，不是系统指令”。
- **Reliability**：summary LLM 失败仍走既有 fallback，不阻断主代理回答。
- **Compatibility**：`off` 模式继续使用旧 memory payload。

## 6. 实施计划

1. 为 `ConversationMemoryContext` 增加 candidate adapter，而不是直接删除 `to_prompt_payload()`。
2. 在 PromptEnvelope renderer 中实现 history candidates 装载与裁剪。
3. 更新主代理 string 模式，使用 candidate path。
4. 为长历史、clarification、已接受 interrupt answer payload / 上传 artifact metadata、capability summaries、fallback token counter、preflight 失败后一次 history compression retry 添加测试。
5. 保留 `off` 路径旧行为，便于回滚。

## 7. 验收标准

- `conda run -n multi_agent python -m unittest tests.orchestration.test_conversation_memory` 通过。
- `conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt` 通过。
- 长历史测试证明不再把 75% 当成历史预算，而是先限制最终输入预算为 `floor(trim_max_tokens * 0.75)`，再按 non-history 占用反算 history。
- 多轮 interrupt resume 场景中，第一次已接受的上传 artifact metadata 与后续标量补参不会被旧 history 裁掉。
- audit 可解释 history 裁剪原因，以及是否触发唯一一次 history compression retry。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 历史过多挤占工具结果。 | 工具结果为 required segment；history 只使用 75% 最终输入预算扣除 non-history 与 margin 后的 flexible budget。 |
| clarification 或已接受 answer 被裁掉导致追问 / resume 失效。 | clarification、accepted interrupt answer payload、上传 artifact metadata 与 current task notes 设更高优先级并测试。 |
| summary 与原文矛盾。 | prompt 标注 summary 非逐字原文；用户纠正和近期原文优先。 |
