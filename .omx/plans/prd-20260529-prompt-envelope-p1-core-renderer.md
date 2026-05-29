# Implementation Plan — Prompt Envelope P1 Core Model and Renderer

- **Date**: 2026-05-29
- **Mode**: `$plan` direct mode followed by `$ralph` execution.
- **Source PRD**: `docs/prd/backend/prompt-envelope/02-阶段一-提示词信封核心模型与渲染器PRD.md`
- **Context snapshot**: `.omx/context/prompt-envelope-p1-core-renderer-<timestamp>.md`
- **Objective**: implement the standalone PromptEnvelope core renderer without connecting it to production prompt/runtime paths.

## Requirements Summary

1. P1 scope is the standalone core: `PromptEnvelope`, `PromptSegment`, render audit, deterministic ordering, dynamic budget, trim policies, prefix hash, and string renderer (`02-阶段一...PRD.md:7-21`).
2. P1 non-scope forbids main-agent production-path changes, conversation-memory generation changes, provider tokenization network calls, and messages-native runtime support (`02-阶段一...PRD.md:23-28`).
3. Functional requirements require importable data models, deterministic ordering, `floor(trim_max_tokens * 0.75)` final input budget, dynamic `bulk_history_budget`, fail-closed required overflow, flexible trimming, prefix hash, no-raw audit, and final preflight with exactly one history compression retry (`02-阶段一...PRD.md:32-41`).
4. Parent PRD requires safety margins: trusted estimator `max(1024, floor(trim_max_tokens * 0.01))`; fallback estimator `max(2048, floor(trim_max_tokens * 0.02))`; default final input budget for 1024000 is 768000 (`00-...总纲PRD.md:67-68`, `:220-231`).
5. Core module must not depend on FastAPI, provider, Skill executor, or storage repository (`02-阶段一...PRD.md:43-48`, `:61-66`).

## Acceptance Criteria

1. `PromptSegment`, `PromptEnvelope`, `PromptSegmentAudit`, `PromptRenderAudit`, `RenderedPrompt`, and `RenderedMessages` are importable from `src.orchestration.prompt_envelope`.
2. Rendering the same segment set in different input orders produces identical prompt text, identical segment audit order, and identical prefix hash.
3. `trim_max_tokens=1024000` yields `final_input_token_budget=768000`; trusted safety margin is `10240`; fallback safety margin is `20480`.
4. `bulk_history_budget = final_input_token_budget - required_non_history_tokens - safety_margin_tokens`, floored at 0 for allocation/audit when negative.
5. Required non-history segments are never truncated; if required content alone exceeds final input budget, renderer raises a fail-closed error.
6. `compressible`, `drop_oldest`, and `drop_if_needed` segments are trimmed/dropped according to available budget and record `tokens_before`, `tokens_after`, `trimmed`, and `trim_reason`.
7. `cacheable_prefix_hash` only changes when `cache_affinity='prefix'` and `mutability='stable'` segment content changes; dynamic or non-prefix segment changes do not change it.
8. Audit serialization contains no raw segment content, raw prompt, artifact body, internal fake path, secret, token, or DSN from segment content.
9. Final preflight recounts the full rendered string; first over-budget preflight triggers one history compression retry and re-render; second failure raises fail-closed.
10. Existing P0 baseline tests remain green.
11. `git diff --check` passes and no dependencies/license policies change.

## Implementation Steps

1. **TDD tests in `tests/orchestration/test_prompt_envelope.py`**
   - Keep existing P0 baseline tests.
   - Add imports for the new PromptEnvelope module.
   - Add tests for importability/order determinism, 75% final input budget, trusted/fallback safety margins, dynamic history budget, required overflow fail-closed, trim policies, prefix hash semantics, no-raw audit recursion, and final preflight retry/fail-closed.

2. **Core module in `src/orchestration/prompt_envelope.py`**
   - Define frozen/slots dataclasses for core models and audits.
   - Define `PromptEnvelopeRenderError` with machine-readable reason.
   - Define an injectable token estimator seam; default to deterministic local char estimator to avoid provider network calls.
   - Implement deterministic segment sorting by semantic/security role, then priority, then name.

3. **Budget and trimming implementation**
   - Compute `final_input_token_budget=floor(trim_max_tokens * 0.75)`.
   - Compute `safety_margin_tokens` from estimator reliability.
   - Compute required non-history tokens and `bulk_history_budget`.
   - Preserve required segments; trim or drop flexible/history segments with audit entries.

4. **Rendering, final preflight, audit**
   - Render deterministic string from included segment content.
   - Recount the final prompt before returning.
   - On first preflight failure, shrink `bulk_conversation_history` / `history` budget once, re-render, and recount.
   - On second failure, raise `PromptEnvelopeRenderError` without truncating required segments.
   - Compute content hashes and prefix hash; audit must not include raw content or metadata.

5. **Completion records and verification**
   - Update `CHANGELOG.md` with P1 implementation summary.
   - Run targeted and relevant layer tests.
   - Run `git diff --check`.
   - Confirm no production main-agent/runtime path changed beyond adding the standalone core module.

## Verification Commands

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.integrations.test_llm_client tests.integrations.test_llm_runtime
git diff --check
```

If exports or broader orchestration behavior are touched, also run:

```bash
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
```

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Renderer becomes coupled to current main-agent prompt too early. | Keep P1 standalone; do not import from `src.capabilities.main_agent` or mutate `build_main_agent_prompt`. |
| Token estimator behavior differs from provider tokenization. | Use injectable estimator seam, final preflight, and larger fallback safety margin; provider optimization remains later phase. |
| Audit accidentally leaks content. | Provide audit dataclasses without `content`/`metadata`; add recursive no-raw tests. |
| Trimming policy becomes ambiguous. | Implement minimal deterministic semantics: required keep/fail; compressible keep prefix; drop_oldest keep suffix/newest; drop_if_needed all-or-nothing. |
| Preflight retry loops indefinitely. | Store retry count and hard-code at most one history compression retry. |

## Staffing / Team Decision

- Recommended execution lane: solo `$ralph`; scope is one standalone module plus tests.
- `$team` is not launched initially because parallel tmux workers would add coordination overhead for a tightly coupled core renderer.
- Use architect subagent verification after implementation per Ralph requirements.
