# Frontend Guide

## Structure

- `src/App.tsx`: authenticated chat shell and the sole owner of conversation/message state, SSE lifecycle, attachment upload/delete/rollback effects, and interrupt answer submission.
- `src/domain/collections.ts`: pure immutable collection updates shared by React state setters.
- `src/domain/attachments.ts`, `src/components/AttachmentCards.tsx`: attachment types, pure labels/summary/merge helpers, and stateless draft/saved-file cards; API effects and attachment state remain in `App.tsx`.
- `src/domain/interrupts.ts`, `src/components/InterruptPresentation.tsx`: interrupt metadata/sheet/slot pure helpers and stateless question/status presentation; answer submission, optimistic messages, pending state, and task continuation remain in `App.tsx`.
- `src/components/MarkdownText.tsx`: shared safe Markdown rendering for user, assistant, reasoning, and interrupt content.
- `src/components/mathFormulaParser.ts`: deterministic TeX/MathML tokenization and per-render resource limits.
- `src/components/MathFormula.tsx`: React lifecycle boundary for asynchronous formula conversion and readable fallback.
- `src/components/mathJaxRuntime.ts`: lazy, same-origin, self-hosted MathJax runtime.
- `src/components/MCPSettingsPanel.tsx`: user-scoped MCP Server configuration and Grant management; new public HTTP endpoints require an explicit plaintext-risk confirmation, and save failures stay visible inside the active modal.
- `src/api/taskEvents.ts`, `src/domain/taskEvents.ts`: authenticated task-event transport and deterministic reducer；以closed Agent frontend events作为正式Run终态，按interrupt/node跟踪multi-waiting，仅渲染transient Agent reasoning；不消费旧Planner/soft-Skill/replan事件；CP7 terminal events和MCP result-Artifact projections使用closed payload、canonical per-Call folding、冲突检测和显式resync。
- `src/domain/artifacts.ts`: artifact display projection; `producer_node_id` and Artifact ID are opaque correlation keys and must not be parsed for capability or product semantics. MCP结果只接受`artifact_type=mcp_result`与strict `maf.mcp.business_result_view.v1`，非法/超预算DTO安全降级，不读取raw `storage_ref`，并从assistant-answer selection排除。
- `src/components/MCPApprovalDialog.tsx`, `src/components/MCPRuntimeStatus.tsx`: accessible Tool approval and task execution status/control surfaces; unknown and recovered-late CP7 outcomes remain failed/no-replay and disable call controls; deferred/permanent result Artifact states use aggregate Alerts and are copied into completed assistant history without extending SSE lifetime。
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
- For CP7 terminal or MCP result-Artifact projection changes, run `src/api/taskEvents.test.ts`, `src/domain/taskEvents.test.ts`, `src/components/MCPRuntimeStatus.test.tsx`, and the restore/replay cases in `src/App.test.tsx`.
- Run `npm run typecheck` and `npm run build` for production changes.
- Verify both root and configured subpath builds when changing asset URLs or Vite base handling.
