# Frontend Guide

## Structure

- `src/App.tsx`: authenticated chat shell, SSE lifecycle, message surfaces, interrupt flow, and copy actions.
- `src/components/MarkdownText.tsx`: shared safe Markdown rendering for user, assistant, reasoning, and interrupt content.
- `src/components/mathFormulaParser.ts`: deterministic TeX/MathML tokenization and per-render resource limits.
- `src/components/MathFormula.tsx`: React lifecycle boundary for asynchronous formula conversion and readable fallback.
- `src/components/mathJaxRuntime.ts`: lazy, same-origin, self-hosted MathJax runtime.
- `src/components/MCPSettingsPanel.tsx`: user-scoped MCP Server configuration and Grant management; new public HTTP endpoints require an explicit plaintext-risk confirmation, and save failures stay visible inside the active modal.
- `src/api/taskEvents.ts`, `src/domain/taskEvents.ts`: authenticated task-event transport and deterministic reducer; CP7 terminal events use closed payload schemas, predecessor-aware replay, conflict detection, and explicit resync state.
- `src/domain/artifacts.ts`: artifact display projection; `producer_node_id` is an opaque correlation key and must not be parsed for capability or product semantics.
- `src/components/MCPApprovalDialog.tsx`, `src/components/MCPRuntimeStatus.tsx`: accessible Tool approval and task execution status/control surfaces; unknown and recovered-late CP7 outcomes remain failed/no-replay and disable call controls.
- `scripts/prepare_mathjax_assets.mjs`: reproducible allowlisted MathJax asset preparation.

## Formula Rendering Constraints

- Keep formula parsing inside `MarkdownText`; do not replace the custom Markdown renderer or scan the application DOM.
- Treat TeX and MathML as untrusted source. Never mount source with `innerHTML` or `dangerouslySetInnerHTML`.
- Preserve ordinary code, links, currency-like text, incomplete streaming delimiters, and assistant copy-as-source behavior.
- Keep the MathJax runtime lazy and base-path aware. Runtime scripts and dynamic fonts must remain same-origin under `public/vendor/`; no CDN fallback is allowed.
- Preserve the 10,000 UTF-16-unit per-formula and 100-formula per-`MarkdownText` limits.
- Formula failures stay local and readable. Async results must not mount after unmount or replace newer streamed source.

## Verification

- Run focused component/parser/runtime tests before the full frontend suite.
- For CP7 terminal projection changes, run `src/api/taskEvents.test.ts`, `src/domain/taskEvents.test.ts`, `src/components/MCPRuntimeStatus.test.tsx`, and the restore/replay cases in `src/App.test.tsx`.
- Run `npm run typecheck` and `npm run build` for production changes.
- Verify both root and configured subpath builds when changing asset URLs or Vite base handling.
