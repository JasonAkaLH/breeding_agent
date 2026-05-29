# Prompt Envelope P6 消息原生运行时扩展测试规格

- **日期**：2026-05-29
- **目标 PRD**：`docs/prd/backend/prompt-envelope/07-阶段六-消息原生运行时扩展PRD.md`
- **实施计划**：`.omx/plans/prd-20260529-prompt-envelope-p6-messages-native.md`

## 1. Test Strategy

采用 TDD：先补失败测试，再实现。默认 mode 必须保持 `off`；所有 messages-native 行为必须显式设置 `MAF_PROMPT_ENVELOPE_MODE=messages` 或传入 `PromptEnvelope` / `LLMMessage`。

## 2. Unit Tests

### UT-1 PromptEnvelope messages renderer

File: `tests/orchestration/test_prompt_envelope.py`

Cases:
1. `LLMMessage` 可导入；`render_prompt_envelope_messages` 返回 `RenderedMessages`。
2. messages 按 segment 顺序保留 system/tool rules、tool result/context、current user、final guard。
3. 未配置 extended roles 时，`context/tool` fallback 到 user context block，audit 记录 role fallback。
4. 配置支持 developer role 时 developer 可原生保留；不支持时 deterministic fold into system。
5. messages final preflight 使用 message wrapper 后的 token count；首次超预算只 retry 一次 history compression；二次仍超预算 fail closed。
6. audit 不含 raw segment content、secret、DSN、内部路径。

### UT-2 PromptProfile messages mode

File: `tests/orchestration/test_prompt_profiles.py`

Cases:
1. `MAF_PROMPT_ENVELOPE_MODE=messages` 时返回 LLMMessage sequence，不再 fallback 到 off。
2. `llm_call_payload` 包含 `mode=messages`、`effective_mode=messages`、budget 字段与 safe `role_fallbacks`。
3. over-budget messages path fail closed；shadow 仍只审计不改发送 prompt。

### UT-3 LLMClient messages support

File: `tests/integrations/test_llm_client.py`

Cases:
1. 旧 `generate_text("prompt")` / `stream_text("prompt")` 仍发送单 user message。
2. `generate_text([LLMMessage(system,...), LLMMessage(user,...)])` 发送多条 messages。
3. 未知 provider 对 developer/tool/context 执行 deterministic fallback，不发送 unsupported role。
4. 显式 config role capability 支持 developer 时，developer 原生发送。
5. `generate_text_with_thinking(messages, thinking=True)` 保持 reasoning/answer chunk 解析。

### UT-4 SharedLLMRuntime union input

File: `tests/integrations/test_llm_runtime.py`

Cases:
1. `generate_text(Sequence[LLMMessage])` 转交给 client，不强制转 string。
2. `stream_events(Sequence[LLMMessage])` 保持 answer/reasoning event coercion。
3. `generate_text(PromptEnvelope, on_reasoning_delta=...)` 可通过 stream path 收集 reasoning。
4. model edition cache 与 trim budget 不回归。

## 3. API / Capability Tests

### IT-1 Main agent messages mode

Files: `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py`, `tests/api/test_main_agent_llm.py`

Cases:
1. 默认未设置 mode 时 stream generator 仍收到 `str`。
2. `MAF_PROMPT_ENVELOPE_MODE=messages` 时 stream generator / fake client 收到 messages sequence。
3. messages prompt 中关键规则、用户请求、tool result、final guard 均存在。
4. `main_agent.prompt_envelope_rendered` audit-only event 包含 mode/effective_mode/messages budget/role fallback；前端 SSE 不可见。
5. `main_agent.llm_call.prompt_envelope` 保留 `final_input_token_budget` / `final_input_tokens` 与 role fallback summary。
6. thinking enabled 时仍产生 `main_agent.reasoning_delta` transient event，最终 completion-only 持久化不变。

## 4. Regression Gates

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope tests.orchestration.test_prompt_profiles
conda run -n multi_agent python -m unittest tests.integrations.test_llm_client tests.integrations.test_llm_runtime
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor tests.api.test_main_agent_llm
conda run -n multi_agent python -m py_compile src/orchestration/prompt_envelope.py src/orchestration/prompt_profiles.py src/integrations/llm_client.py src/integrations/llm_runtime.py src/capabilities/main_agent/prompt_envelope_builder.py src/capabilities/main_agent/helpers.py src/capabilities/main_agent/executor.py src/api/runtime.py
git diff --check
```

Post-deslop / final affected discover:

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 5. Non-test Checks

- `git diff --check` must pass.
- Search changed audit payloads for raw prompt / secret examples.
- License Requirement: no dependency/native/Cargo change expected; final report must state no cargo-deny risk triggered.
