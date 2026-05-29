# Prompt Envelope P2 — 主代理信封字符串迁移实施计划

## Requirements Summary

目标是按 `docs/prd/backend/prompt-envelope/03-阶段二-主代理信封字符串迁移PRD.md` 将主代理从直接字符串拼接迁移到 PromptEnvelope rendered seam，同时保持 runtime 仍向 LLM 发送字符串。

当前事实：
- 旧主代理 prompt 拼接集中在 `src/capabilities/main_agent/prompt_builder.py:23-75`，顺序为系统规则/下载硬约束、memory、artifact、回答角色、dependency、Skill body、script result、用户问题。
- 主代理 executor 目前在调用 LLM 前直接调用旧 `build_main_agent_prompt(...)`，见 `src/capabilities/main_agent/executor.py:153-162`；成功后写 `main_agent.llm_call` audit-only event，见 `src/capabilities/main_agent/executor.py:332-348`。
- 阶段一 renderer 已提供 `PromptEnvelope` / `PromptSegment` / `RenderedPrompt` 和 75% final input budget、preflight retry、no-raw audit，见 `src/orchestration/prompt_envelope.py:109-163` 与 audit 字段 `src/orchestration/prompt_envelope.py:75-94`。
- P0 baseline 已锁定旧 prompt 顺序和 `manifest.body` 暴露风险，见 `tests/capabilities/main_agent/test_conversation_memory_prompt.py:35-95`。
- PRD 要求 P2 默认不切 string，shadow 不改变实际 prompt，string 才发送 envelope-to-string prompt；含 Skill match 的生产 string 必须等待 P4 安全门禁。

## Acceptance Criteria

1. **Rendered seam 可测**：新增 `build_main_agent_rendered_prompt(...) -> RenderedPrompt` 或等价 seam，测试可断言 segment names、audit、75% final input budget。
2. **Off 兼容**：默认 `off` 下 `build_main_agent_prompt(...)` 和 executor LLM prompt 保持旧行为；P0/P2 旧顺序测试通过。
3. **Shadow 不改 prompt**：`MAF_PROMPT_ENVELOPE_MODE=shadow` 时 fake stream generator 收到旧 prompt，但结果事件中包含 audit-only prompt envelope audit 摘要。
4. **String 发送新 prompt**：`MAF_PROMPT_ENVELOPE_MODE=string` 且无 Skill match 时 fake stream generator 收到 envelope-to-string prompt；`current_user_request` 与 `final_recency_guard` 位于尾部，文件下载硬约束仍存在。
5. **Final preflight 生效**：string/seam 使用阶段一 renderer，`final_input_tokens <= floor(trim_max_tokens * 0.75)`；超预算按 renderer 规则先压缩历史再 fail closed。
6. **Skill guard**：P4 前含 Skill match / forced Skill 的 string mode 不把 `manifest.body` 通过 string prompt 发送给 LLM；实际 LLM prompt 回退旧 prompt 或 guard 阻止，并记录 audit-only guard reason。
7. **Audit 不泄漏 raw**：prompt envelope audit 事件不含 raw prompt、raw artifact、manifest body、用户问题原文等敏感正文。
8. **SSE 不变**：frontend 可见 `main_agent.output_delta` / `main_agent.output_final` 语义不变；audit-only event 不进入 transient SSE。
9. **License Requirement**：无依赖/许可变更，不触发 cargo-deny 风险。

## Implementation Steps

1. **新增主代理 PromptEnvelope builder**
   - 新增 `src/capabilities/main_agent/prompt_envelope_builder.py`。
   - 将旧 prompt 的稳定系统规则拆到 `stable_system_contract`，文件下载硬约束拆到 `stable_tool_rules`，尾部新增 `final_recency_guard`。
   - 将 memory 放入 `bulk_conversation_history`，artifact/dependency/script result 放入 `required_tool_results_and_artifacts`，当前用户请求放入 `current_user_request`。
   - builder 复用旧 `prompt_builder` 的 sanitization/formatting helper，避免复制安全逻辑。

2. **补 mode 解析与兼容包装**
   - 支持 `MAF_PROMPT_ENVELOPE_MODE=off|shadow|string`，默认 `off`；非法值保守按 `off`。
   - 保留旧 `build_main_agent_prompt(...) -> str` 作为兼容入口；新增 `build_main_agent_rendered_prompt(...)` 供 tests/executor 使用。
   - string mode 的 trim budget 从显式参数、request metadata / stream metadata 或 `load_config()` 派生；不可用时由 renderer default fail-safe 处理。

3. **接入 MainAgentRespondCapability**
   - 在 `src/capabilities/main_agent/executor.py:153-162` 替换为 mode-aware prompt resolution：
     - off：构建旧 prompt，不写 envelope audit（或写最小 mode audit）。
     - shadow：构建旧 prompt发送，同时生成 envelope audit 摘要并加入 audit-only event / `main_agent.llm_call` payload。
     - string：无 Skill match 时发送 rendered.prompt；含 Skill match 时回退旧 prompt并记录 `skill_string_guard`。
   - 在 `main_agent.llm_call` payload 中加入脱敏 audit summary：mode、effective_mode、guard reason、template id/version、token budget、final input tokens、history truncated、preflight retry count、segment token stats（不含 raw content）。

4. **先写 P2 回归测试，再实现**
   - `tests/capabilities/main_agent/test_conversation_memory_prompt.py`：测试 rendered seam 的 segment 顺序、文件下载硬约束、string 75% budget / preflight。
   - `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py`：测试 off 默认旧 prompt、shadow prompt 不变但 audit-only event 存在、string prompt 发送新顺序、Skill match string guard 回退旧 prompt且不泄露 audit。
   - `tests/api/test_main_agent_llm.py`：补 shadow audit 不进入 frontend SSE / persisted frontend output semantics 不变的 API 级证据（如现有 API 测试足够，可增加最小 targeted test）。

5. **验证与 Ralph gates**
   - 运行 targeted：
     - `conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt`
     - `conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor`
     - `conda run -n multi_agent python -m unittest tests.api.test_main_agent_llm`
   - 运行必要层级 smoke：`conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'`；如 P2 改 orchestration renderer，补跑 orchestration prompt envelope tests。
   - 用 architect subagent 复核；对 Ralph-owned changed files 做 deslop review（本上下文执行、必要时微调）；再次跑 targeted tests。
   - 写 Ralph completion audit 并读回；提交 git。

## Risks and Mitigations

- **新旧 prompt 分叉**：builder 复用旧 formatting helpers，off 默认不变；string 仅显式启用。
- **含 Skill body 的 string 生产泄漏**：P2 设置 Skill guard；含 Skill match 时 string 回退旧 path 并写 audit-only guard，P4 完成前不放量。
- **audit 泄漏正文**：audit summary 只取 renderer audit 的数字/hash/name，不写 raw prompt / segment content。
- **token 口径不准**：P2 使用 renderer seam 和可注入 estimator；真实 provider token counter 不在本阶段强行接入，避免扩大 LLM runtime 入参变更。
- **SSE 回归**：audit-only event 不 publish transient；API 测试验证 frontend event 仍只有 output/reasoning/final 等可见事件。

## Verification Steps

1. 先跑新增测试，确认红灯来自缺失 P2 seam/mode/audit/guard。
2. 实现后跑 PRD 验收 targeted 命令。
3. 运行 `python -m compileall` 覆盖新增/修改 Python 文件。
4. Architect verification：要求 verdict=APPROVED 或按反馈修复后重跑。
5. Post-deslop regression：重跑 targeted tests。
6. License Requirement：确认无 `native/`、`Cargo.lock`、`native/deny.toml`、依赖文件变更；最终说明未触发 cargo-deny 风险。

## Team Decision

本阶段触点集中在主代理 prompt builder/executor 与对应测试，依赖关系强且文件数中等；先不拉 `$team`。若后续出现 API runtime、Skill public profile 或 provider token counter 大范围联动，再拆出 Team lanes（executor / test-engineer / verifier）。

## Stop Condition

所有 P2 acceptance criteria 有测试或明确证据覆盖，targeted tests 通过，architect approval 通过，Ralph completion audit 读回通过，git commit 完成。
