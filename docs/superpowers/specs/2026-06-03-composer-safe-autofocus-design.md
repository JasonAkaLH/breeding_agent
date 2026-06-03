# Composer Safe Autofocus Design

Status: Draft reviewed for implementation planning
Date: 2026-06-03
Owner: frontend v1 business conversation console

## 1. Problem Statement

Users currently need to click the message composer before typing after entering the page or after a task returns to an idle state. This adds avoidable friction to the main chat workflow, especially when the user repeatedly submits follow-up questions, answers interrupts, or tests task cancellation/retry flows.

The desired behavior is not a naive `autoFocus`. The composer must become ready for immediate typing when it is safe, without stealing focus from deliberate user interactions such as history navigation, upload controls, menus, model selectors, rename/delete actions, or task cancellation controls.

## 2. Goals

- Let the user type immediately when the composer first becomes safely available.
- Restore composer focus after task terminal states so follow-up input can begin without mouse interaction.
- Focus the composer when a supplemental-input interrupt prompt is ready, so the user can answer without clicking the box first.
- Avoid stealing focus from active controls, popovers, menus, select/dropdown options, dialogs, upload controls, or the history sidebar.
- Keep autofocus as a frontend-only progressive enhancement; it must not affect task, interrupt, upload, SSE, cancel, or backend behavior.
- Add deterministic frontend tests for both focus and no-steal cases.

## 3. Non-goals

- No backend, database, API, SSE contract, or storage changes.
- No global shortcut system, command palette, or app-wide focus event bus.
- No change to Enter-to-send, Shift+Enter newline, IME confirmation handling, slash command behavior, upload behavior, pending interrupt submission, or cancellation semantics.
- No automatic focus while the task is actively running, waiting for SSE, loading artifacts, or cancelling.
- No new dependency.

## 4. Users, Stakeholders, and Affected Systems

| Category | Details |
| --- | --- |
| Primary user | Internal business user using the frontend chat console to submit questions, upload files, answer interrupts, and continue conversations. |
| Affected system | `frontend/src/App.tsx` composer interaction around the Ant Design `Input.TextArea`. |
| Test surface | `frontend/src/App.test.tsx` with React Testing Library focus assertions. |
| Unaffected systems | Backend API, runtime, storage, SSE event schema, skill execution, uploads ledger, and database schemas. |

## 5. Current State and Evidence

Repo evidence inspected on 2026-06-03:

- `frontend/src/App.tsx` contains the composer `Input.TextArea` at lines around 1820-1846 with `aria-label="请输入问题"`, controlled `value`, IME handlers, Enter handling, placeholder, `autoSize`, and `disabled={active && taskState.phase !== 'cancelling'}`.
- `frontend/src/App.tsx` computes `active = isTaskActive(taskState.phase)` around line 371.
- `frontend/src/App.tsx` already tracks IME composition through `composingInputRef` and `isComposerImeConfirming` around lines 1030-1034.
- `frontend/src/App.tsx` centralizes task terminal transitions in `handleTaskEvent`, `reconcileTerminalTaskStatus`, and `settleCancelledTask`.
- `frontend/src/App.test.tsx` already has broad composer state tests around disabled/enabled behavior, history switching, task terminal events, cancellation, and interrupt flows, but no focus assertions for the composer.

The current implementation has no explicit composer focus manager and no `autoFocus` attribute on the composer.

## 6. Proposed Solution

Implement a small local composer autofocus helper in the frontend, scoped to `App.tsx` unless implementation shows a clear readability need to extract it to a tiny local module. The helper observes composer availability and schedules a safe focus attempt after React/Ant Design DOM updates settle.

The design should remain intentionally small:

- Add a ref to the existing `Input.TextArea`.
- Derive a conservative `composerAutoFocusEnabled` state from existing UI state.
- Use a local hook/helper such as `useComposerAutoFocus(...)` to schedule focus with `requestAnimationFrame`.
- Before focusing, call a `canStealFocusForComposer(...)` guard that permits focus only when the current active element is safe.
- Cancel pending animation-frame focus attempts when state changes or the component unmounts.

The focus manager is a UI enhancement only. If the ref is unavailable, the browser does not support an option, or focusing fails, the app should continue normally without user-visible error.

## 7. Functional Requirements

| ID | Requirement |
| --- | --- |
| FR-1 | When the user enters or restores an idle conversation and the composer is enabled, the composer must receive focus if no unsafe active element is currently focused. |
| FR-2 | After a task reaches `completed`, `failed`, or `cancelled` and the composer returns to idle enabled state, the composer must receive focus if safe. |
| FR-3 | Switching to a historical conversation with no active current task must focus the composer if safe after messages and task restoration complete. |
| FR-4 | Creating or entering a new empty conversation must focus the composer if safe. |
| FR-5 | When a `node.waiting_for_input` event has resolved to a visible `pendingInterrupt` prompt and the composer is enabled for the user's supplemental answer, the composer must receive focus if safe. |
| FR-6 | The focus manager must not focus during active task phases, including submitting, accepted/running/streaming, waiting-input prompt loading before `pendingInterrupt` is available, loading artifacts, or cancelling. |
| FR-7 | The focus manager must not steal focus when the current active element is an interactive control or is inside an interactive popup/menu/dialog/sidebar action. |
| FR-8 | The focus manager must not interfere with IME composition. |
| FR-9 | The existing composer input value, slash command menu behavior, upload controls, selected skill command, interrupt answer handling, and cancellation behavior must remain unchanged. |

## 8. Conservative Focus Eligibility

Autofocus eligibility should be stricter than the textarea `disabled` prop.

A focus attempt may be scheduled only when all are true:

1. The user is authenticated and the chat workspace is initialized enough to render the composer.
2. The textarea is not disabled.
3. The current state is one of the user-input-ready states:
   - idle or terminal conversation state; or
   - `taskState.phase === 'waiting_for_input'` with a visible `pendingInterrupt` prompt ready for the user's answer.
4. The state must not be `submitting`, `accepted`, `running`, `streaming`, waiting-input prompt loading before `pendingInterrupt` is available, `loading_artifacts`, or `cancelling`.
5. The component is not in IME composition.
6. The composer ref resolves to a focusable textarea element.

Implementation may reuse existing `active`, `taskState.phase`, `pendingInterrupt`, `currentTaskId`, and `conversationId` state, but it must keep focus eligibility separate from business state transitions. `waiting_for_input` must be treated as user-input-ready only after the interrupt prompt is loaded, because the current reducer intentionally excludes `waiting_for_input` from `isTaskActive`.

Conversation restore readiness is a required gate: page entry, cross-conversation switch, and same-conversation history reload must clear the ready marker before async restore begins, and must set it only after the target conversation has been restored or explicitly confirmed by a local submit path. Initial reducer `idle` must not be interpreted as confirmed idle before this gate passes.

## 9. No-steal Guard

Before calling `focus`, inspect `document.activeElement`.

Autofocus is allowed when:

- There is no active element.
- The active element is `document.body`.
- The active element is already the composer textarea.
- The active element is the composer send button from the just-submitted message flow; after the task reaches terminal state this is treated as safe so the user can type the next message immediately.

Autofocus must be skipped when the active element is or is inside:

- `button`, `input`, `textarea`, `select`, `a[href]`, or `[contenteditable="true"]`.
- Elements with `role="button"`, `role="menuitem"`, `role="option"`, `role="combobox"`, `role="dialog"`, `role="listbox"`, or `role="textbox"` outside the composer.
- Ant Design popup/dropdown/popover/select/modal/menu containers known by stable classes or roles available in rendered DOM.
- History sidebar rows/actions and account/history controls.
- File upload controls or controls inside the composer action popover.
- Composer controls other than the send button, such as the input-menu/plus button or stop button.

This guard should prefer skipping focus over stealing focus when uncertain.

## 10. Scheduling and Cleanup

- Schedule focus with `requestAnimationFrame` so it runs after the current render.
- Use `focus({ preventScroll: true })` when supported; gracefully fall back to plain `focus()` if needed.
- Re-check all focus eligibility and no-steal rules inside the scheduled callback.
- Cancel the pending animation frame when dependencies change or the component unmounts.
- The effect must be idempotent: repeated idle renders must not cause visible flicker or scroll movement.

## 11. UX Flows

### 11.1 Page Entry / Restore

1. User opens the app and authenticates or existing auth is restored.
2. Current conversation loads.
3. If there is no active task and composer is enabled, focus moves to the composer.
4. User can type immediately.

### 11.2 Task Terminal Follow-up

1. User submits a message.
2. Composer is unavailable while task is active.
3. Task reaches `completed`, `failed`, or `cancelled` through SSE, cancel API response reconciliation, or task polling reconciliation.
4. Composer returns to idle enabled state.
5. Focus moves to the composer if no unsafe element is focused.

### 11.3 User Operates Another Control

1. User clicks a history item, refresh button, upload/menu button, select, popover option, rename/delete action, or cancel control.
2. A state change occurs that would otherwise make the composer eligible.
3. The no-steal guard sees the active interactive element.
4. Composer focus is skipped.

### 11.4 Interrupt Flow

1. A task asks for supplemental input through `node.waiting_for_input`.
2. While the frontend is still loading the prompt from `/interrupts`, autofocus does not run.
3. Once `pendingInterrupt` is visible and the composer is enabled for the supplemental answer, focus moves to the composer if the no-steal guard passes.
4. The autofocus implementation must not alter interrupt answer semantics.
5. If the task resumes after answer submission, no autofocus occurs during resumed running state.
6. If cancellation fails and the app restores an interrupt answer state, focus may return only if the composer is enabled and the no-steal guard passes.

## 12. Non-functional Requirements

| Area | Requirement |
| --- | --- |
| Reliability | Focus failures must be no-op and must not throw user-visible errors. |
| Accessibility | Existing `aria-label="请输入问题"` remains. Autofocus must not steal focus from deliberate keyboard navigation or interactive controls. |
| Performance | The effect must schedule at most one pending animation-frame focus attempt per relevant state transition. |
| Compatibility | Must work in the current React + Ant Design + Vite test/runtime environment. |
| Privacy/security | No data or permission implications; no backend or storage changes. |
| Observability | No runtime audit event required. Tests are the verification surface. |

## 13. Acceptance Criteria

| AC | Verification |
| --- | --- |
| AC-1 | Frontend test proves initial idle page renders with composer focused. |
| AC-2 | Frontend test proves `task.completed` or equivalent terminal transition returns focus to the composer. |
| AC-3 | Frontend test proves `task.cancelled` or cancel reconciliation returns focus to the composer after terminal state. |
| AC-4 | Frontend test proves a visible interrupt prompt focuses the composer for the supplemental answer when safe. |
| AC-5 | Frontend test proves waiting-input prompt loading before `pendingInterrupt` does not focus prematurely. |
| AC-6 | Frontend test proves running/active task state does not focus the composer and keeps existing disabled behavior. |
| AC-7 | Frontend test proves an active history/sidebar button or other interactive control is not displaced by composer autofocus. |
| AC-8 | Frontend test proves cancelling state does not trigger composer autofocus. |
| AC-9 | Frontend test proves autofocus waits for workspace restore before focusing an apparently idle composer, including same-conversation reload. |
| AC-10 | Existing frontend tests for submit, slash commands, upload, interrupt, cancellation, and history still pass. |
| AC-11 | `npm run build` passes. |

## 14. Test Plan

Targeted tests should be added to `frontend/src/App.test.tsx`.

Recommended cases:

1. `focuses the composer when the restored workspace is idle`.
2. `focuses the composer after task completion returns the workspace to idle`.
3. `focuses the composer after task cancellation reaches terminal state`.
4. `focuses the composer when an interrupt prompt is visible and ready for supplemental input`.
5. `does not autofocus while waiting-input prompt details are still loading`.
6. `does not autofocus while a task is active or cancelling`.
7. `does not steal focus from a focused history or sidebar control`.
8. `does not change slash command / upload / interrupt existing behavior` through existing regression suite.

Validation commands:

```bash
cd frontend
npm test -- --run App.test.tsx -t "composer"
npm test -- --run
npm run build
```

## 15. Rollout and Migration

- No schema migration.
- No backend deployment dependency.
- No feature flag required for the first implementation because the change is local, reversible, and test-covered.
- If manual QA discovers focus behavior is disruptive in real browser use, the change can be reverted or narrowed to page-entry-only without data migration.

## 16. Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Focus steals keyboard navigation from buttons or menus | User loses context or activates wrong input | Conservative no-steal guard; tests for focused interactive controls. |
| Ant Design TextArea ref shape differs from native textarea | Focus call fails or ref points to wrapper | Implementation must resolve the native textarea via documented/ref-observed shape and fail safely. |
| Repeated renders cause focus loops | Annoying UX and flaky tests | Schedule only on meaningful eligibility key changes; idempotent focus check. |
| Cancelling state currently leaves textarea not disabled | Autofocus might distract during cancel | Eligibility explicitly excludes `cancelling` even if textarea is technically enabled. |
| Existing tests assume focus elsewhere implicitly | Test flake | Add explicit focus assertions and avoid brittle global side effects. |

## 17. Assumptions

- Safe autofocus is desired only for the primary chat composer, not for upload controls or history actions.
- Skipping autofocus when any interactive element is focused is preferable to stealing focus.
- The current single-composer `App.tsx` architecture remains in place for this change; extraction is optional only if it improves readability without broad refactor.

## 18. Open Questions

No blocking open questions remain. The product decision is confirmed: use the safe-focus approach, not aggressive or page-entry-only autofocus.
