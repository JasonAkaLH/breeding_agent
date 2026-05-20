# Implementation Plan: Frontend Slash Skill Command MVP

Date: 2026-05-20  
Source spec: `docs/superpowers/specs/2026-05-20-frontend-slash-skill-command-design.md`  
Mode: planning only; no implementation in this step.

## Requirements Summary

Build an MVP slash command flow in the frontend composer so users can explicitly force-call a public `skill.*` capability by typing `/` or `/skill-name args`. Existing plain chat must keep automatic LLM routing. Slash force-calls must preserve upload metadata and must be represented as structured API fields (`routing_mode=force_capability`, `capability_id=skill.xxx`) rather than prompt prefixes.

Current anchors:

- Composer state and submission are centralized in `frontend/src/App.tsx`; current input state lives at `App.tsx:121-150`, and `handleSubmit()` currently derives `content` from `input.trim()` and sends upload metadata at `App.tsx:472-502`.
- The composer TextArea and Enter handler are in `frontend/src/App.tsx:969-990`; the send button is in `App.tsx:1027-1034`.
- The frontend API client currently lacks capability listing and always sends `routing_mode: 'auto'` with mode-derived `capability_id` in `frontend/src/api/client.ts:31-40`, `client.ts:42-64`, and `client.ts:151-171`.
- Frontend request types already model `routing_mode`, `capability_id`, and metadata in `frontend/src/api/types.ts:22-29`.
- Backend capability listing already exists at `src/api/routes/capabilities.py:15-31`, returning fields defined in `src/api/dto.py:230-239`.
- Backend message submission already accepts `routing_mode`, `capability_id`, and metadata in `src/api/dto.py:9-15`, canonicalizes/validates capability at `src/api/runtime.py:291-292`, stores `requested_capability_id` at `runtime.py:330-340`, merges upload context at `runtime.py:351-367`, and schedules orchestration with metadata at `runtime.py:369-377`.
- `WorkflowRouter` already sends top-level `skill.*` requests to `SkillWorkflowProvider` at `src/orchestration/workflow_router.py:14-21`; `SkillWorkflowProvider` resolves skill name and produces forced skill execution metadata at `src/orchestration/skill_workflow_provider.py:31-78`.
- Existing tests cover composer layout and send behavior in `frontend/src/App.test.tsx:182-230` and `App.test.tsx:754-861`, upload metadata in `App.test.tsx:891-908`, API submit body behavior in `frontend/src/api/client.test.ts:44-79`, and backend skill routing in `tests/orchestration/test_workflow_router.py:11-30` plus `tests/api/test_skill_capability_pool.py:8-58`.

## Acceptance Criteria

1. When authenticated users type `/` at the start of the composer, the UI shows an inline Skill picker sourced from `GET /api/v1/capabilities`; only active public `skill.*` capabilities are shown.
2. Picker supports mouse selection, ArrowUp/ArrowDown active item movement, Enter selection, and Esc close without breaking the existing IME Enter guard in `App.tsx:980-985`.
3. Selecting a Skill displays an independent Skill badge in the composer and leaves the TextArea containing only the user’s natural-language problem.
4. Clicking the badge remove control clears only the current composer selection and returns the next submit to `routing_mode=auto`.
5. Direct input `/skill-name args` submits with `content=args`, `routing_mode=force_capability`, `capability_id=<matched skill.*>`, and metadata containing `forced_by_slash_command=true` and `slash_command=/skill-name`.
6. Direct input `/unknown args` does not call `submitMessage`; the user sees a recoverable “未找到 Skill” message in or near the picker.
7. Upload + slash forced submit includes both `capability_id=skill.xxx` and existing `metadata.upload_ids`; the current upload metadata behavior at `App.tsx:501` must not be overwritten.
8. Plain input without slash keeps current behavior: API body has `routing_mode=auto`, `capability_id=null`, deep-thinking metadata, and existing SSE/task rendering remains unchanged.
9. Backend rejects or fails closed for `routing_mode=force_capability` without a valid supported `capability_id`; supported `skill.*` force routes continue through `SkillWorkflowProvider`.
10. Pending Skill context for insufficient information is either implemented with durable tests or explicitly split into a follow-up PR with backend acceptance tests before claiming full spec completion. If split, frontend slash MVP must still be complete and not pretend pending continuation exists.

## Implementation Steps

### Phase 1 — Frontend API contract and pure slash parsing (TDD first)

1. Add capability response types to `frontend/src/api/types.ts` near existing API DTOs (`types.ts:22-29` for submit request shape). Include `CapabilityResponse` and `CapabilityListResponse` matching backend fields from `src/api/dto.py:230-239`.
2. Extend `ApiClient` in `frontend/src/api/client.ts:42-64` with `listCapabilities(): Promise<CapabilityListResponse>` and implement it via existing `request<T>()` helper at `client.ts:93-106`.
3. Extend `SubmitMessageInput` in `frontend/src/api/client.ts:31-40` with optional `capabilityId?: string | null` and optional slash metadata remains passed through `metadata`.
4. Update `submitMessage()` at `frontend/src/api/client.ts:151-171` so `input.capabilityId` overrides mode-derived capability. If non-null, send `routing_mode: 'force_capability'`; otherwise preserve existing `routing_mode: 'auto'` and `capability_id: null`.
5. Add `frontend/src/domain/slashCommands.ts` and tests before implementation:
   - derive commands from capabilities, filtering `capability_id.startsWith('skill.')` and active status;
   - command name should prefer capability id suffix normalized for display (for example `skill.mini_breedstat_rcbd` -> `/mini-breedstat-rcbd`) while preserving exact capability id;
   - parse `/skill-name args`, exact-match command names, and return blocked/no-match for `/unknown`.
6. Update `frontend/src/api/client.test.ts` around existing submit tests (`client.test.ts:44-79`) to assert capability listing and forced routing body.

### Phase 2 — Frontend composer UI and submission integration (TDD first)

1. Add `frontend/src/components/SlashCommandMenu.tsx` plus focused component tests if useful, or cover through `App.test.tsx` if component is intentionally simple.
2. In `frontend/src/App.tsx`, add state near current composer state (`App.tsx:121-150`) for:
   - loaded `skillCommands`;
   - loading/error state for capability list;
   - slash menu open/query/active index;
   - `selectedSkillCommand`.
3. Load capabilities after authentication/conversation initialization. Use the existing `api.me()` effect pattern at `App.tsx:163-177`; do not block normal chat when capability loading fails.
4. Replace the TextArea `onChange={(event) => setInput(event.target.value)}` at `App.tsx:970-974` with a handler that updates input plus slash menu state when input starts with `/`.
5. Extend the TextArea `onPressEnter` block at `App.tsx:980-985`:
   - preserve IME guard first;
   - when slash menu is open and a selectable candidate exists, prevent default and select it;
   - when input starts with unknown slash, prevent submit and show menu/no-match;
   - otherwise call `handleSubmit()` as today.
6. Render `SlashCommandMenu` adjacent to the composer, inside the `Space` / composer card near `App.tsx:946-969`, so it visually anchors to the input row and does not enter message history.
7. Render the selected Skill badge before the send row or inside composer attachments area near `App.tsx:946-968`. The badge remove action clears only `selectedSkillCommand`.
8. Update `handleSubmit()` at `App.tsx:472-502` to derive a submit intent:
   - selected badge takes precedence;
   - direct slash exact match is parsed on submit;
   - unknown slash blocks submit;
   - cleaned content is used for the user message and API body;
   - metadata merges upload ids with slash fields, never replaces either.
9. Update send button disabled logic at `App.tsx:1027-1034` so `/skill-name` with empty args can still submit if it matches a Skill, but `/unknown` cannot.
10. Add/extend `frontend/src/App.test.tsx` cases near existing composer tests (`App.test.tsx:754-861`, upload test at `App.test.tsx:891-908`): slash picker opens, keyboard selects, badge renders/removes, direct slash submits forced capability, unknown slash blocks, and upload + slash includes both capability and upload ids.

### Phase 3 — Backend force-capability semantic hardening (TDD first)

1. Add API/runtime tests extending `tests/api/test_skill_capability_pool.py` and/or a new targeted `tests/api/test_slash_force_capability.py`:
   - `routing_mode=force_capability` with valid `skill.*` stores task `requested_capability_id` and routes through Skill provider;
   - `force_capability` without `capability_id` returns 400;
   - `force_capability` with unsupported capability returns 400;
   - metadata `forced_by_slash_command` and `slash_command` is passed into the orchestration request/audit-safe path where existing tests can observe it.
2. Update `src/api/runtime.py` in `submit_message()` before `_canonical_capability_id()` (`runtime.py:291-292`) to validate force mode explicitly:
   - if `request.routing_mode == 'force_capability'` and no `request.capability_id`, raise `ValueError`;
   - keep `_ensure_supported_capability()` as the authoritative supported-capability gate.
3. Ensure `Task.routing_mode` is set from request routing mode instead of always defaulting to `RoutingMode.AUTO`. Current `Task` construction at `runtime.py:330-340` does not pass `routing_mode`; set it using the existing enum contract so task lists can reflect forced routing.
4. Do not add a separate route for skills; reuse `GET /api/v1/capabilities` (`routes/capabilities.py:15-31`).
5. Keep security boundary from `tests/api/test_skill_capability_pool.py:59-104`: user metadata alone must not force a Skill unless the top-level requested capability route is `skill.*`.

### Phase 4 — Pending Skill context continuation (separate backend slice unless trivial)

This is semantically important but broader than the composer MVP because current `Message` and `Task` models do not expose arbitrary metadata fields (`src/core/models.py:90-113`; `src/storage/sqlite/models.py:100-126`). Implement only after a narrow backend design/test slice confirms where durable pending state lives.

1. Add a small durable model/repository surface for conversation pending Skill context rather than hiding it in frontend state. Candidate implementation options:
   - dedicated SQLite table keyed by conversation id and status;
   - existing conversation summary safe metadata if an appropriate field already exists for conversation-level runtime metadata;
   - task/event-derived context if it can be queried deterministically without schema churn.
2. Define a typed context payload: `capability_id`, `skill_name`, `original_user_message`, `missing_requirements`, `source_task_id`, `created_at`, `status`.
3. Add tests for lifecycle:
   - forced Skill insufficient info writes assistant message and pending context;
   - next ordinary user message reuses pending capability before auto planner;
   - new slash command overrides pending context;
   - successful completion clears pending context.
4. Only then wire `ApiRuntime.submit_message()` to check pending context before auto routing, without overriding explicit slash force requests.
5. If this phase is deferred, document the gap in the implementation report and do not claim full pending-continuation completion.

### Phase 5 — Verification and polish

1. Run targeted frontend tests:
   - `cd frontend && npm test -- --run frontend/src/domain/slashCommands.test.ts frontend/src/api/client.test.ts frontend/src/App.test.tsx` if Vitest path selection works;
   - otherwise `cd frontend && npm test -- --run`.
2. Run frontend build: `cd frontend && npm run build`.
3. Run targeted backend tests for changed runtime/router behavior:
   - `conda run -n multi_agent python -m unittest tests.api.test_skill_capability_pool`
   - any new `tests.api.test_slash_force_capability`
   - `conda run -n multi_agent python -m unittest tests.orchestration.test_workflow_router`
4. If backend pending context touches storage/schema, also run:
   - `conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'`
   - `conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'`
5. Run full frontend smoke manually with Browser only after implementation changes, because Browser is required after significant local frontend changes when target URL is known/obvious.

## Risks and Mitigations

- **Risk: slash command metadata overwrites upload ids.** Mitigate with App test asserting one submit carries both `capabilityId` and `metadata.upload_ids`, anchored to existing upload behavior at `App.test.tsx:891-908` and `App.tsx:501`.
- **Risk: Enter selection breaks IME composition.** Mitigate by preserving `isComposerImeConfirming()` before slash handling and extending the existing IME test at `App.test.tsx:847-861`.
- **Risk: frontend tries to validate Skill-specific required fields.** Mitigate by keeping `slashCommands.ts` limited to command parsing/matching; backend/Skill owns parameter sufficiency.
- **Risk: force routing can be spoofed by metadata.** Mitigate by preserving the existing guard tested in `tests/api/test_skill_capability_pool.py:59-104`; only top-level `capability_id` may force routing.
- **Risk: pending Skill context becomes hidden ephemeral UI state.** Mitigate by implementing it only as backend durable state or explicitly deferring it as incomplete.
- **Risk: broad schema changes for pending context collide with Rust migration gates.** Mitigate by treating Phase 4 as a separate backend slice with storage tests and no Rust runtime claims unless contract updates are intentionally included.

## Verification Checklist

- [ ] `frontend/src/domain/slashCommands.test.ts` covers command derivation, exact slash parsing, no-match blocking, and cleaned content.
- [ ] `frontend/src/api/client.test.ts` proves forced submit sends `routing_mode=force_capability` and normal submit still sends `auto`.
- [ ] `frontend/src/App.test.tsx` proves picker, badge, direct slash submit, unknown slash block, upload merge, and IME safety.
- [ ] Backend API tests prove valid `skill.*` force route and invalid force request fail closed.
- [ ] Existing no-slash auto-routing tests still pass.
- [ ] Frontend build passes.
- [ ] Implementation report distinguishes completed Phase 1-3 MVP from Phase 4 pending-continuation if Phase 4 is not implemented in the same branch.

## Execution Handoff Guidance

Recommended execution path: `$ralph` or solo executor for Phases 1-3; consider `$team` only if implementing Phase 4 pending context in parallel with frontend work.

Suggested lanes if using team:

1. Frontend lane: `slashCommands.ts`, `SlashCommandMenu.tsx`, `App.tsx`, frontend tests.
2. API client lane: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `client.test.ts`.
3. Backend force route lane: `src/api/runtime.py`, API/orchestration tests.
4. Backend pending context lane: storage/runtime design and tests; only run in parallel if write scope is isolated and schema decision is locked.

Stop condition: all chosen phases have passing targeted tests and the final report states exactly which acceptance criteria are complete versus deferred.
