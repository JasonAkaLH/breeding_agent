# Prompt Envelope P3 — 对话记忆候选上下文化实施计划

## Requirements Summary

目标是按 `docs/prd/backend/prompt-envelope/04-阶段三-对话记忆候选上下文化PRD.md` 将 conversation memory 从“单体最终历史 payload/预算决策者”降级为“候选上下文提供者”，由 PromptEnvelope renderer 在完整 prompt 视角下计算 history budget，并保证当前任务补充信息、已接受 interrupt answer 与上传 artifact metadata 不被旧 history 挤掉。

当前事实：
- `ConversationMemoryContext.to_prompt_payload()` 当前只输出旧兼容字段：`history_summary`、`recent_messages`、`clarification_messages`、`capability_summaries`、当前/补全用户消息与压缩 audit，见 `src/orchestration/conversation_memory.py:109-165`。
- `ConversationMemoryConfig.actual_memory_budget` 仍是 P0 legacy 静态预算，见 `src/orchestration/conversation_memory.py:65-76`，并由 `tests/orchestration/test_prompt_envelope.py:80-87` 锁定；P3 不删除该兼容行为。
- API runtime 已在 `_attach_conversation_memory()` 中调用 `context.to_prompt_payload()` 注入 `request.memory_context` / metadata，见 `src/api/runtime.py:793-831`，因此 P3 新字段必须通过同一 payload 兼容传递。
- 当前主代理 PromptEnvelope builder 把整个 memory payload 格式化为一个 `bulk_conversation_history` segment，见 `src/capabilities/main_agent/prompt_envelope_builder.py:126-139`。
- P1 renderer 已支持 `final_input_token_budget=floor(trim_max_tokens*0.75)`、trusted/fallback safety margin、`bulk_history_budget=final_input_token_budget-required_non_history-safety_margin`、一次 history compression retry 与第二次 fail-closed，见 `src/orchestration/prompt_envelope.py:109-163` 和 `src/orchestration/prompt_envelope.py:201-292`。
- interrupt answer 会保存为同 task 的 user message；当前 task 的非 root user message 会进入 clarification，见 `src/api/runtime.py:1441-1449` 与 `src/orchestration/conversation_memory.py:355-363`。resume 的 uploaded_artifacts 已由 memory builder 安全投影为 capability summary，见 `src/api/runtime.py:1480-1488` 与 `src/orchestration/conversation_memory.py:412-423`。

## Acceptance Criteria

1. **Candidate 输出兼容**：`ConversationMemoryContext.to_prompt_payload()` 保留旧字段，同时新增 `memory_candidates`；每个 candidate 带 `candidate_id`、`kind`、`content`、`priority`、`trim_policy`、`token_estimate` 与 safe metadata。
2. **Candidate 类型完整**：history summary、recent messages、clarification messages、capability summaries 都能生成 candidates；accepted interrupt answer payload 通过 clarification message，uploaded artifact metadata 通过 capability/upload summary 进入高优先级 candidates。
3. **动态历史预算**：主代理 string/shadow rendered path 使用 candidate path 组装 `bulk_conversation_history`，实际历史可用空间由 renderer 的 `floor(trim_max_tokens*0.75) - non_history_tokens - safety_margin` 决定；测试证明 75% 是最终输入预算，不是固定 history 预算。
4. **关键上下文优先保留**：超预算时低优先级 old history / older recent 先裁剪，高优先级 clarification / accepted answer / upload metadata / capability summary 保留。
5. **Fallback margin 口径不回退**：renderer 继续在可信 estimator 下使用 `max(1024, floor(trim_max_tokens*0.01))`，fallback 下使用 `max(2048, floor(trim_max_tokens*0.02))`，audit 标记 `token_estimator=fallback|trusted`。
6. **Audit 可解释**：render/audit payload 包含 `final_input_token_budget`、`final_input_tokens`、`candidate_history_tokens`、`memory_candidate_count`、`bulk_history_budget`、`bulk_history_tokens_used`、`history_truncated`、`history_compression_retry` 与 segment trim reason；audit 不含 raw prompt/candidate content。
7. **Final preflight 规则保持**：第一次 final preflight 失败只压缩/收缩 history，第二次仍失败则 fail closed；P1 既有测试继续通过。
8. **Off 兼容**：`MAF_PROMPT_ENVELOPE_MODE=off` / legacy `build_main_agent_prompt()` 继续使用旧 memory formatting，不要求消费 candidate path。
9. **License Requirement**：无依赖/许可变更，不触发 cargo-deny 风险。

## Implementation Steps

1. **TDD：新增候选与 audit 测试**
   - 在 `tests/orchestration/test_conversation_memory.py` 添加 candidate adapter 测试，断言 `to_prompt_payload()` 新增 `memory_candidates` 且不破坏旧字段；断言 summary/recent/clarification/capability priority、trim policy、token estimate。
   - 在同文件添加 interrupt/upload resume 场景测试：当前 task 多轮补参 + uploaded artifact metadata 生成高优先级 candidates，raw upload content 不进入 payload。
   - 在 `tests/orchestration/test_prompt_envelope.py` 添加 segment metadata / top-level candidate audit 测试，确保 audit 只有统计/hash/name而无 raw content。
   - 在 `tests/capabilities/main_agent/test_conversation_memory_prompt.py` 添加 rendered prompt candidate trim 测试：低优先级 old history 被裁，clarification/upload/capability 保留；并断言 `bulk_history_budget` 约等于最终输入预算扣除 non-history 和 margin，而不是固定 75% history。

2. **实现 `ConversationMemoryCandidate` 与 adapter**
   - 在 `src/orchestration/conversation_memory.py` 新增 frozen dataclass：`candidate_id`、`kind`、`content`、`priority`、`trim_policy`、`token_estimate`、`metadata`。
   - `ConversationMemoryContext.to_prompt_candidates(token_estimator=None)` 生成低→高优先级候选：history summary、recent messages、capability summaries、clarification messages；clarification / upload / accepted answer 类候选的 priority 高于 old history。
   - `to_prompt_payload()` 增加 `memory_candidates`，旧字段保持不变；`to_audit_payload()` 增加非内容 candidate 统计。
   - `sanitize_memory_prompt_payload()` 增加 candidate 白名单清洗，保留 safe content 与统计字段，继续剔除 summary_id、username、source hash、raw upload content、SQL 等敏感字段。

3. **接入主代理 PromptEnvelope candidate path**
   - 在 `src/capabilities/main_agent/prompt_envelope_builder.py` 新增 candidate-aware formatter：优先消费 `memory_candidates`，按 priority/sequence 低→高排序，组装为一个 `bulk_conversation_history` segment，使 `drop_oldest` suffix trim 优先保留末尾高优先级补充信息。
   - 保留旧 `_format_memory_context(memory_payload)` fallback，保证 P0/P2 old payload 仍可渲染。
   - 为 history segment 填充 safe metadata：candidate count、candidate token sum、kind/priority 分布等，不包含 raw content。

4. **扩展 PromptEnvelope audit**
   - 在 `src/orchestration/prompt_envelope.py` 为 `PromptSegmentAudit` 增加 safe metadata 字段，或等价地在 `PromptRenderAudit` 汇总 `candidate_history_tokens` / `memory_candidate_count`。
   - `_render_once()` 从 history segment metadata 汇总 candidate 统计，`prompt_envelope_audit_payload()` 将字段输出到 audit-only payload；segment audit metadata 必须经过 primitive/list/dict safe projection。

5. **验证与 Ralph gates**
   - 先运行新增 targeted tests，确认红灯来自缺失 P3 功能。
   - 实现后运行：
     - `conda run -n multi_agent python -m unittest tests.orchestration.test_conversation_memory`
     - `conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope`
     - `conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt`
   - 运行必要层级回归：`conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'` 与 `conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'`。
   - 运行 `python -m compileall` 覆盖修改的 Python 文件、`git diff --check`。
   - Architect verification（STANDARD tier minimum）通过后，对 Ralph changed files 做 deslop pass 并重跑 targeted tests。
   - 写 Ralph completion audit、读回确认，再完成 goal；按“同样规则”提交本阶段 git commit。

## Risks and Mitigations

- **candidate content 被 audit 泄漏**：candidate payload 可进入 prompt，但 audit 仅输出 hash/statistics/metadata allowlist；新增测试递归扫描 audit 文本。
- **高优先级补充仍被 suffix trim 裁掉**：candidate formatter 将低优先级旧 history 放前、高优先级 clarification/upload/capability 放后；测试使用超预算历史证明关键 marker 保留。
- **legacy/off 行为意外变化**：旧 `build_main_agent_prompt()` 继续使用原 `_format_memory_context()`；P0/P2 tests 保持。
- **预算被误解为固定 history 75%**：测试断言 `bulk_history_budget = floor(trim_max_tokens*0.75) - non_history_tokens - safety_margin`，且工具/用户/guard 等 required segment 增大会缩小 history，而不是让 history 固定占 75%。
- **多轮 interrupt answer 覆盖/丢失**：基于现有 persisted answer message 与 uploaded_artifacts metadata，不改 DB schema；测试覆盖第一次上传 metadata + 后续 scalar 补参。

## Team Decision

本阶段核心触点集中在 conversation memory adapter、renderer audit、主代理 PromptEnvelope builder 与对应 tests，文件数中等且接口耦合强；先不拉 `$team`。若全层回归暴露 API runtime / Skill resume 跨域问题，再拆分 Team lanes：executor（runtime 接入）、test-engineer（interrupt resume e2e）、verifier（audit/泄漏复核）。

## Stop Condition

P3 acceptance criteria 均被测试或审计证据覆盖；targeted + 必要层级回归通过；architect approval 通过；deslop 后回归仍绿；Ralph completion audit 读回通过；goal mode 标记 complete；本阶段变更已按 Lore commit protocol 提交。
