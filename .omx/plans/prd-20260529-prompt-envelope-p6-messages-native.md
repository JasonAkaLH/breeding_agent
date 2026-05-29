# Prompt Envelope P6 消息原生运行时扩展实施计划

- **日期**：2026-05-29
- **模式**：`$plan` direct planning for `$ralph` execution
- **目标 PRD**：`docs/prd/backend/prompt-envelope/07-阶段六-消息原生运行时扩展PRD.md`
- **Ralph context snapshot**：`.omx/context/prompt-envelope-p6-messages-native-20260529T043450Z.md`
- **配套测试规格**：`.omx/plans/test-spec-20260529-prompt-envelope-p6-messages-native.md`

## 1. Requirements Summary

本阶段要在不破坏现有 string LLM runtime 的前提下，为 PromptEnvelope 增加 messages-native 能力：

1. 定义 `LLMMessage` 或等价消息模型。
2. `SharedLLMRuntime.generate_text/stream_events` 支持 `str | PromptEnvelope | Sequence[LLMMessage]`。
3. `LLMClient.generate_text/generate_text_with_thinking/stream_text` 支持 messages，并继续兼容字符串。
4. `MAF_PROMPT_ENVELOPE_MODE=messages` 才改变实际发送形态；默认 `off` 不变，`shadow` 只审计，`string` 仍发送 envelope-to-string。
5. Provider role capability 必须来自启动期 / runtime / 测试 config；未知 provider 默认只支持 basic role，扩展 role deterministic fallback。
6. role fallback audit 必须记录发生了哪些 role 折叠或上下文块 fallback，且不得含 raw content。
7. messages 最终输入同样继承 75% final input budget 与 final token preflight；一次历史压缩 retry 后仍超预算必须 fail closed。
8. thinking / reasoning_delta streaming、main_agent.output_delta / output_final 行为不回归。

## 2. Brownfield Evidence

- `src/orchestration/prompt_envelope.py:40-109` 已有 `PromptEnvelope` / `PromptSegment` / `PromptRenderAudit` / `RenderedMessages` 类型，但 `RenderedMessages` 仅是占位式可导入模型，还没有 messages renderer 或 role fallback audit。
- `src/orchestration/prompt_envelope.py:112-166` 已实现 75% final input budget、final token preflight、一次 history compression retry 与 fail-closed，可复用为 messages renderer 的预算内核。
- `src/integrations/llm_runtime.py:103-165` 当前只接收 `prompt: str`，并将 prompt 直接传给 client 或 stream generator。
- `src/integrations/llm_client.py:153-203` 当前总是发送 `[{'role': 'user', 'content': prompt}]`，没有 messages 入参、role capability config 或 fallback audit。
- `src/capabilities/main_agent/prompt_envelope_builder.py:34-56` 当前 `MAF_PROMPT_ENVELOPE_MODE=messages` 会被当作未知值 fallback 到 `off`。
- `src/capabilities/main_agent/prompt_envelope_builder.py:106-253` 主代理 PromptEnvelope segment 已分层，适合作为 messages-native golden 测试来源。
- `tests/integrations/test_llm_runtime.py` 与 `tests/integrations/test_llm_client.py` 是 runtime/client 最小回归入口；当前覆盖 string、thinking stream、model edition cache。

## 3. RALPLAN-DR Short Summary

### Principles

1. **兼容第一**：所有旧 string tests 必须不改语义通过；默认不启用 `messages`。
2. **单一预算内核**：messages 渲染必须复用 PromptEnvelope 75% final input budget 与 preflight，不复制另一套预算公式。
3. **保守 role 策略**：未知 provider 只允许 basic roles；`developer` / `tool` / `context` 等扩展 role 必须有 config 才能原生发送。
4. **no-raw audit**：fallback audit 只记 role、segment、reason、hash/token，不记正文。
5. **streaming 不回归**：messages 只影响输入形态，不改变 reasoning / answer event contract。

### Decision Drivers

1. P6 是 runtime/client 级扩展，风险集中在 provider 兼容与 stream generator 兼容。
2. P1-P5 已有 PromptEnvelope/Profile 预算与 audit 基线，应复用而不是重写。
3. `messages` 模式属于显式灰度候选，必须可通过 `off|string` 快速回滚。

### Viable Options

| Option | Pros | Cons | Decision |
| --- | --- | --- | --- |
| A. 在 PromptEnvelope core 增加 `LLMMessage` + messages renderer，runtime/client 接受 union input | 预算/audit 与 segment 排序一处维护；P5 profiles 可自然继承 messages mode | 需要适度调整类型与测试 | **选择** |
| B. 只在 LLMClient 层把字符串拆成 messages | 改动少 | 无法保留 segment role、role fallback audit 与 messages preflight | 拒绝 |
| C. 新建独立 messages builder，不复用 PromptEnvelope renderer | 局部实现快 | 预算/裁剪/audit 逻辑分叉，后续维护风险高 | 拒绝 |

## 4. ADR

### Decision

采用 Option A：在 `src/orchestration/prompt_envelope.py` 中补齐 `LLMMessage`、`PromptRoleFallbackAudit` 与 `render_prompt_envelope_messages(...)`；让 `src/integrations/llm_runtime.py` 和 `src/integrations/llm_client.py` 接受 `str | PromptEnvelope | Sequence[LLMMessage]`；在 main-agent / prompt-profile mode resolver 中显式支持 `messages`。

### Drivers

- 同一个 PromptEnvelope 必须能输出 string 或 messages，并共享 75% final input preflight。
- Provider role 支持不确定，必须默认 conservative fallback。
- 现有 fake clients/generators 大量假设 `str`，因此只有 `messages` flag 下才发送 messages-native。

### Alternatives considered

- **Client-only split**：无法解释 segment-level role fallback，且无法证明 message wrapper 后仍在 75% 输入预算内。
- **单 provider 定制**：过早绑定某个 provider 行为，不符合 PRD “capability 来自 config / 测试显式 config”。
- **生产默认 messages**：违反 P6 非目标与父总纲回滚约束。

### Why chosen

该方案让 P6 成为 P1-P5 的自然扩展：核心 renderer 负责结构、预算和 no-raw audit；runtime/client 只负责把规范化 messages 传给 OpenAI-compatible provider；调用点通过 `MAF_PROMPT_ENVELOPE_MODE=messages` 显式启用。

### Consequences

- 需要给 `PromptRenderAudit` 增加 role fallback 安全字段，并更新 audit payload 投影。
- 需要让 `iter_stream_events` 与 runtime/client 类型宽化，但保持旧 string 调用兼容。
- 若真实 provider 未配置 extended role，messages-native 仍会 deterministic fallback 到 `system/user` 或单 user string，audit 记录 fallback。

### Follow-ups

- P7 可在此基础上加入 provider-specific cache hint 与更细 cache observability。
- 后续真实 provider smoke 应验证配置中启用 `developer/tool` role 时的 provider 接受度；本阶段只做 fake provider contract。

## 5. Acceptance Criteria

1. `LLMMessage` 可导入；`RenderedMessages` 返回 message 序列与 no-raw audit。
2. `render_prompt_envelope_messages` 对同一 envelope 保留关键 system rules、current user、tool result、final guard，并通过 `final_input_tokens <= final_input_token_budget`。
3. role fallback audit 覆盖 `developer -> system`、`context/tool -> user context block` 等确定性折叠；audit 不含 raw prompt/secret。
4. `SharedLLMRuntime.generate_text/stream_events` 可接收 `Sequence[LLMMessage]` 与 `PromptEnvelope`，仍可收集 reasoning delta。
5. `LLMClient` 对 messages 入参发送多 role messages；未知 provider 默认不发送 unsupported extended role；显式 config 支持 extended role 时才发送。
6. `MAF_PROMPT_ENVELOPE_MODE=messages` 时主代理使用 messages-native；`off` 默认仍是旧 string。
7. `main_agent.prompt_envelope_rendered` / `main_agent.llm_call` audit 记录 `mode=messages`、`effective_mode=messages`、`role_fallbacks` 与 budget 字段，不进入前端可见 SSE。
8. 现有 thinking / stream tests 继续通过。

## 6. Implementation Steps

1. **TDD — core messages renderer**
   - 更新 `tests/orchestration/test_prompt_envelope.py`：新增 messages 渲染、role fallback、message-wrapper preflight retry/no-raw audit 用例。
   - 实现 `LLMMessage`、`PromptRoleFallbackAudit`、`render_prompt_envelope_messages`，复用 `_render_once` 预算路径。

2. **TDD — LLMClient messages**
   - 更新 `tests/integrations/test_llm_client.py`：覆盖 string 兼容、messages 多 role payload、未知 provider fallback、显式 role config、stream thinking messages。
   - 实现 prompt normalization、role capability config 解析、OpenAI-compatible messages payload 构造。

3. **TDD — SharedLLMRuntime union input**
   - 更新 `tests/integrations/test_llm_runtime.py`：覆盖 Sequence[LLMMessage]、PromptEnvelope direct render、thinking + on_reasoning_delta 保持。
   - 扩展 runtime 入参类型、client option forwarding 与 fallback client 兼容。

4. **TDD — main-agent messages mode**
   - 更新 `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py` / `tests/api/test_main_agent_llm.py`：覆盖 `MAF_PROMPT_ENVELOPE_MODE=messages` 发送 messages、audit-only event、前端不可见、默认 off 不变。
   - 扩展 `resolve_main_agent_prompt_for_mode` 支持 messages；executor/stream helper 接受 messages union。

5. **Profile helper messages mode**
   - 扩展 `src/orchestration/prompt_profiles.py` 的 mode 解析与 `PromptProfileResolution`，让 P5 多调用 profile 在 `messages` 下不退回 legacy off。
   - 补充 `tests/orchestration/test_prompt_profiles.py` messages mode 回归。

6. **Audit + changelog**
   - 确保 prompt/audit payload 不含 raw content；新增 role fallback 字段只含 safe scalar/list。
   - 更新 `CHANGELOG.md` 记录 P6 计划与实现。

7. **Verification / Architect / Deslop**
   - 运行 targeted tests、affected discover、`py_compile`、`git diff --check`。
   - Ralph architect 复核；通过后对 changed files 运行 deslop 扫描和 post-deslop 回归。

## 7. Verification Steps

最小 targeted gate：

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope tests.orchestration.test_prompt_profiles
conda run -n multi_agent python -m unittest tests.integrations.test_llm_client tests.integrations.test_llm_runtime
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor tests.api.test_main_agent_llm
conda run -n multi_agent python -m py_compile src/orchestration/prompt_envelope.py src/orchestration/prompt_profiles.py src/integrations/llm_client.py src/integrations/llm_runtime.py src/capabilities/main_agent/prompt_envelope_builder.py src/capabilities/main_agent/helpers.py src/capabilities/main_agent/executor.py src/api/runtime.py
git diff --check
```

分层回归建议：

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 8. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Provider 不支持 developer/tool/context role | 默认 basic role fallback；只有 config 显式支持才原生发送；fallback audit 覆盖。 |
| messages wrapper token overhead 让输入超预算 | messages preflight 使用带 role wrapper 的估算文本；复用一次 history compression retry。 |
| fake generator 不支持 messages | 默认 `off` / `string` 不变；messages tests 使用支持 messages 的 fake；runtime 仍支持 str。 |
| streaming / thinking 回归 | 保留现有 `generate_text_with_thinking` 事件解析，只替换 input normalization；新增 stream tests。 |
| audit 泄露 raw prompt | role fallback audit 只记录 segment name/source/target/reason；no-raw 扫描测试。 |

## 9. Available-Agent-Types Roster / Staffing Guidance

Known useful roles: `executor`, `test-engineer`, `architect`, `verifier`, `code-reviewer`, `code-simplifier`, `explore`.

- **Solo Ralph default**：本阶段改动集中在 runtime/client/core/main-agent，单 owner 可控；先不启动 tmux `$team`。
- **If `$team` needed**：建议 `omx team 3:executor "implement Prompt Envelope P6 messages-native runtime from .omx/plans/prd-20260529-prompt-envelope-p6-messages-native.md"`。
  - Worker 1 executor: core renderer + prompt profile helper。
  - Worker 2 executor: LLMClient/SharedRuntime + tests。
  - Worker 3 test-engineer/verifier: main-agent API tests + regression evidence。
- **Ralph fallback**：当前已由 `$ralph` 作为持久单 owner verification/fix lane 执行。
