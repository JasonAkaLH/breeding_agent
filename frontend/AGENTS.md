# Frontend Guide

## Structure

- `src/App.tsx`: authenticated chat shell, SSE lifecycle, message surfaces, interrupt flow, and copy actions.
- `src/components/MarkdownText.tsx`: shared safe Markdown rendering for user, assistant, reasoning, and interrupt content.
- `src/components/mathFormulaParser.ts`: deterministic TeX/MathML tokenization and per-render resource limits.
- `src/components/MathFormula.tsx`: React lifecycle boundary for asynchronous formula conversion and readable fallback.
- `src/components/mathJaxRuntime.ts`: lazy, same-origin, self-hosted MathJax runtime.
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
- Run `npm run typecheck` and `npm run build` for production changes.
- Verify both root and configured subpath builds when changing asset URLs or Vite base handling.
