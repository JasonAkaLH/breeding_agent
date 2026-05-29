# Implementation Plan — Prompt Envelope P0 Test Baseline

- **Date**: 2026-05-29
- **Mode**: `$plan` direct mode; planning only, no implementation in this pass.
- **Source PRD**: `docs/prd/backend/prompt-envelope/01-阶段零-测试基线与旧行为锁定PRD.md`
- **Objective**: implement the phase-zero test baseline that locks current legacy prompt/runtime behavior before later PromptEnvelope migrations.
- **Stop condition**: tests-only baseline plan is saved; source implementation remains unchanged until a later execution step.

## 1. Requirements Summary

1. Phase zero is explicitly scoped to current main-agent prompt order, conversation-memory static budget, Skill prompt risk, legacy LLM runtime string path, and key regression baselines; it explicitly excludes new PromptEnvelope implementation and production prompt/runtime changes (`01-阶段零...PRD.md:7-8`).
2. The PRD requires tests, not fixes: lock current prompt order, static 75% memory behavior, `manifest.body` exposure risk, and current string-only `LLMClient` / `SharedLLMRuntime` provider path (`01-阶段零...PRD.md:16-20`, `:24-28`).
3. Current main-agent prompt is a string assembled by `build_main_agent_prompt(...)` using `parts`, with download safety rules first, then optional memory, artifacts, response role, dependency context, Skill instructions, script output, and user question (`src/capabilities/main_agent/prompt_builder.py:23-75`).
4. Current memory payload is sanitized before formatting and rendered under `# 对话记忆上下文（历史数据，不是系统指令）` (`src/capabilities/main_agent/prompt_builder.py:49-51`, `:100-136`; `src/orchestration/conversation_memory.py:919-949`).
5. Current conversation memory budget is `max_tokens - max(1024, max_tokens // 4)`, so `1024000` resolves to `768000`; this is a legacy baseline to replace later, not a final design (`src/orchestration/conversation_memory.py:63-67`; `01-阶段零...PRD.md:43-45`).
6. Current Skill prompt path injects `match.manifest.body`, while `SkillManifest.body` is a first-class manifest field and `SkillMatch` carries it into prompt building (`src/capabilities/main_agent/prompt_builder.py:62-71`; `src/integrations/codex_skills/manifest.py:12-23`; `src/integrations/codex_skills/matcher.py:9-13`).
7. Current `LLMClient` converts the entire prompt string into one user-role chat message for both non-streaming and streaming paths (`src/integrations/llm_client.py:153-203`).
8. Current `SharedLLMRuntime` accepts `prompt: str` and forwards that string to client `generate_text(...)` or `generate_text_with_thinking(...)` / `stream_text(...)` (`src/integrations/llm_runtime.py:103-165`).
9. Existing tests already cover memory prompt redaction, Skill script metadata isolation, runtime/client basics, and model-edition trim budgets; phase zero should extend those tests rather than create brittle end-to-end fixtures (`tests/capabilities/main_agent/test_conversation_memory_prompt.py:13-167`; `tests/integrations/test_llm_client.py:245-361`; `tests/integrations/test_llm_runtime.py:8-124`).

## 2. Acceptance Criteria

1. **Prompt order baseline**: a main-agent test asserts relative order of these markers only, not the full prompt string: system/download safety rules → `# 对话记忆上下文` → `# 上传文件上下文（已脱敏）` → `# 回答角色` → `# 上游能力结果上下文（已执行完成）` → `# 已匹配 Skill 指令` → `# Skill 脚本输出` → `# 用户问题`.
2. **Download safety baseline**: the same or adjacent test asserts the current safety wording contains `/api/v1/artifacts/`, `/download`, and forbids `sandbox:/mnt/data`, `file://`, local absolute paths, and `outputs/...` as download links.
3. **Memory budget baseline**: an orchestration test asserts `ConversationMemoryConfig(max_tokens=1024000).actual_memory_budget == 768000`; the test name must state this is a phase-zero legacy baseline before dynamic PromptEnvelope budgeting.
4. **Skill body exposure risk baseline**: a test constructs a synthetic `SkillManifest.body` containing fake internal markers such as `runtime: python_subprocess`, `handler: synthetic.internal.Handler`, and `scripts/internal_demo.py`, then asserts the current main-agent prompt includes those markers. Use only fake/synthetic paths and no secrets.
5. **Legacy LLMClient shape baseline**: integration tests assert provider calls are exactly one `messages` item with `role == "user"` and `content == prompt`; no system/developer/tool role should be introduced in phase zero.
6. **Legacy SharedLLMRuntime forwarding baseline**: runtime tests assert prompt inputs remain `str` and are forwarded unchanged to text and stream client paths.
7. **No runtime behavior change**: implementation should modify tests and completion records only. Production source changes are out of scope unless strictly needed for test seams; any such change must be explicitly justified and behavior-preserving.
8. **Green-by-default**: no unskipped red tests are added. Future desired behavior can be recorded as skipped TODO tests or later-stage test plans only.
9. **Verification evidence**: targeted unittest commands and `git diff --check` pass.
10. **License Requirement**: no dependency, Rust, `Cargo.lock`, or license-policy changes; final report records “无依赖/许可变更，未触发 cargo-deny 风险”.

## 3. Implementation Steps

### Step 1 — Extend main-agent prompt baseline tests

- File: `tests/capabilities/main_agent/test_conversation_memory_prompt.py`.
- Add imports for direct prompt construction:
  - `build_main_agent_prompt` from `src.capabilities.main_agent.prompt_builder`.
  - `RESPONSE_ROLE_FINAL` from `src.capabilities.main_agent.response_roles` if needed.
  - `SkillManifest` and `SkillMatch` from `src.integrations.codex_skills` modules.
- Add a small helper such as `_assert_markers_in_order(testcase, text, markers)` that searches indices and fails with the missing/out-of-order marker.
- Add `test_phase_zero_locks_main_agent_prompt_segment_order_and_download_safety_wording`:
  - Build the prompt directly with synthetic `memory_context`, `artifact_context`, `response_role`, `answer_scope`, `dependency_context`, `skill_matches`, `script_results`, and a unique user message.
  - Assert relative order of the required markers listed in Acceptance Criterion 1.
  - Assert download safety wording listed in Acceptance Criterion 2.
- Keep this as a relative-marker test; do not snapshot the entire prompt.

### Step 2 — Add Skill body exposure risk baseline

- File: `tests/capabilities/main_agent/test_conversation_memory_prompt.py`.
- Add `test_phase_zero_documents_current_skill_manifest_body_exposure_risk`:
  - Construct `SkillManifest(name="synthetic", description="Synthetic skill", triggers=("synthetic",), body="runtime: python_subprocess\nhandler: synthetic.internal.Handler\nscripts/internal_demo.py", source_path=Path("skill/synthetic/SKILL.md"))`.
  - Wrap it in `SkillMatch(manifest=manifest, score=100, reason="trigger:synthetic")`.
  - Call `build_main_agent_prompt(...)` and assert all fake internal markers are included.
- Optional: add a skipped TODO test documenting the later P4 target that public Skill profiles must not expose internal runtime/handler/script paths. It must be decorated with `@unittest.skip(...)` so default discovery remains green.

### Step 3 — Add conversation-memory static budget baseline

- Preferred file: `tests/orchestration/test_prompt_envelope.py`.
- If the file does not exist, create it as a tests-only module; do **not** create `src/orchestration/prompt_envelope.py` in phase zero.
- Add `PromptEnvelopePhaseZeroBaselineTest(unittest.TestCase)` with:
  - `test_phase_zero_locks_static_conversation_memory_budget_before_dynamic_prompt_envelope_budgeting` asserting `ConversationMemoryConfig(max_tokens=1024000).actual_memory_budget == 768000`.
  - Optional adjacent assertions for existing formula clarity: `max_tokens=8000 -> 6000`, `max_tokens=10000, reserved_tokens=2000 -> 8000`.
- Test names/comments should make clear this is a legacy baseline that future PromptEnvelope work will intentionally replace.

### Step 4 — Lock LLMClient single-user-message provider shape

- File: `tests/integrations/test_llm_client.py`.
- Extend existing fake-completion tests or add focused assertions near:
  - `test_streaming_extracts_reasoning_and_answer_chunks`.
  - `test_stream_text_uses_streaming_without_thinking_for_main_agent_output`.
  - `test_generate_text_uses_non_streaming_completion_for_structured_tasks`.
- Assert `fake_completions.calls[0]["messages"] == [{"role": "user", "content": "prompt"}]` for both streaming and non-streaming paths.
- Assert the roles list is exactly `["user"]` to make accidental system/developer/tool role introduction visible during P0-P5.

### Step 5 — Lock SharedLLMRuntime string forwarding

- File: `tests/integrations/test_llm_runtime.py`.
- Extend `test_reuses_one_client_for_text_and_stream_calls` or add a new focused test:
  - Fake client records raw prompt values and `type(prompt)` for `generate_text` and `generate_text_with_thinking`.
  - Call `runtime.generate_text("p1", ...)` and `runtime.stream_events("p2", ...)`.
  - Assert recorded prompt values are unchanged strings: `("text", str, "p1")`, `("stream", str, "p2")`.
- Keep this test independent of external provider config.

### Step 6 — Confirm phase-zero non-goals remain true

- Check `git diff -- src` after implementation.
- Expected result: no production source diff. If test-only import paths require a tiny code exposure change, document why it is behavior-preserving; otherwise revert source changes.
- Do not add PromptEnvelope models, renderer, message-native runtime, dynamic budgets, provider cache logic, or Skill public-profile migration in phase zero.

### Step 7 — Update completion record

- Update `CHANGELOG.md` after implementation with a concise phase-zero baseline entry.
- Final implementation report should include changed files, tests run, result, and License Requirement statement.

## 4. Verification Steps

Run targeted checks first:

```bash
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope
conda run -n multi_agent python -m unittest tests.integrations.test_llm_client tests.integrations.test_llm_runtime
git diff --check
```

If time permits or touched areas broaden, run layer checks:

```bash
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
```

Do not run real-provider smoke tests; P0 must not depend on real LLM credentials or network provider behavior.

## 5. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Prompt-order test becomes brittle. | Assert only relative markers and key safety substrings; avoid full prompt snapshots. |
| Future desired behavior is added as a failing test. | Use skipped TODO tests or later-stage PRDs; default unittest discovery must stay green. |
| Synthetic Skill risk test leaks real internals. | Use fake names and fake paths only, e.g. `synthetic.internal.Handler` and `scripts/internal_demo.py`. |
| Runtime baseline accidentally enables messages-native early. | Assert exact single user-message shape in `LLMClient` and unchanged `str` forwarding in `SharedLLMRuntime`. |
| Test file name suggests implementation exists. | Keep `tests/orchestration/test_prompt_envelope.py` tests importing existing `ConversationMemoryConfig` only; do not add production PromptEnvelope code. |
| Completion claims overstate phase zero. | Report “tests-only baseline; no runtime behavior change” and include evidence from targeted tests. |

## 6. Follow-up Staffing Guidance

- Recommended follow-up lane: **solo executor** for implementation, then **verifier** review of test evidence. Scope is tests-only and does not warrant `$team` unless bundled with later PRD stages.
- Suggested roles if delegated:
  - `executor` (medium): implement test-only baseline in the files above.
  - `verifier` (medium/high): run targeted tests, inspect `git diff -- src`, and confirm no production behavior changed.
- `$ultragoal` is appropriate if the user wants durable multi-stage execution across P0-P7 later. For this P0-only slice, direct execution is sufficient.
- `$ralph` fallback is only useful if the user explicitly wants a persistent single-owner loop to implement and verify this baseline now.

## 7. Explicit Non-Implementation Boundary

This plan intentionally does **not** implement:

- PromptEnvelope data model or renderer.
- Dynamic final input budget or final token preflight.
- Skill public profile replacement.
- Messages-native runtime or provider cache routing.
- Any production prompt content/order changes.
