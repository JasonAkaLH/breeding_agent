# Chat Formula Rendering Design

**Date:** 2026-07-10
**Status:** Approved for implementation planning

## Goal

Render TeX/LaTeX and MathML safely and consistently anywhere the frontend uses the shared chat Markdown renderer. Formula rendering must work for user messages, assistant replies, reasoning content, and interrupt questions without weakening the existing Markdown safety behavior or disrupting streaming updates.

## Scope

The frontend will recognize these formula forms:

- Inline TeX: `$...$` and `\(...\)`
- Display TeX: `$$...$$` and `\[...\]`
- Display TeX fences: fenced code blocks labelled `tex`, `latex`, or `math`
- Serialized MathML: `<math>...</math>`, including multi-line elements

The shared `MarkdownText` path already renders all four target message surfaces, so the feature belongs in that component and its focused helpers rather than in each caller.

## Non-goals

- AsciiMath is not supported.
- This change does not introduce a formula editor, equation numbering, cross-message references, or authoring toolbar.
- This change does not replace the existing custom Markdown parser with a general Markdown framework.
- This change does not alter backend message, history, or SSE payload schemas.
- No custom model-output delimiter is required. The accepted TeX and MathML forms are standard enough that the frontend can tolerate normal model output without a backend prompt change.

## Architecture

Use MathJax 4 as the single TeX and MathML rendering engine. Install and serve it with the frontend application rather than loading code or fonts from a public CDN. Use SVG output so formula rendering does not depend on remotely loaded MathJax font assets.

Formula detection remains part of the Markdown rendering pipeline, but MathJax execution is isolated in a dedicated `MathFormula` component:

1. `MarkdownText` tokenizes Markdown blocks as it does today.
2. Block parsing recognizes complete display TeX, supported formula fences, and complete MathML before falling back to a paragraph.
3. Inline parsing recognizes complete inline TeX and inline MathML alongside the existing strong text, inline code, and link tokens.
4. A formula token carries its input language, source text, display mode, and original source for fallback display.
5. `MathFormula` lazy-loads a shared MathJax runtime and calls the promise-based TeX-to-SVG or MathML-to-SVG conversion API.
6. The returned DOM node is mounted inside the component without injecting the untrusted source string as HTML.

The MathJax runtime is initialized once. Concurrent conversions use MathJax's promise-based API so dynamic streaming updates do not start conflicting synchronous render operations.

## Parsing Rules

- Formula parsing never runs inside ordinary fenced code blocks.
- Inline backticks continue to mean Markdown code. Formula markers inside them remain literal.
- `\$` is rendered as a literal dollar sign.
- A single-dollar inline formula must have both opening and closing delimiters. Unmatched dollar signs remain text.
- `\(...\)`, `\[...\]`, and `$$...$$` also require complete closing delimiters.
- A MathML token starts at `<math` and ends at the matching `</math>`. Incomplete MathML remains source text while streaming.
- Formula fences labelled `tex`, `latex`, or `math` are display formulas. Other language fences remain code blocks.
- A `<math display="block">` element is rendered as a display formula; otherwise MathML follows its surrounding inline or block position.
- Existing Markdown headings, lists, tables, links, bold text, and code rendering keep their current behavior.

## Streaming Behavior

Assistant and reasoning text can change repeatedly while an SSE response is in progress. Formula rendering therefore follows these rules:

- An incomplete formula is plain source text and does not invoke MathJax.
- When the closing delimiter arrives, React replaces the source token with a keyed `MathFormula` component.
- Each component ignores a conversion result if it has unmounted or its source has changed, preventing stale asynchronous output from replacing newer content.
- A formula conversion failure affects only that formula; the rest of the message remains rendered.

## Security

Both users and models can supply formula source, so all formula input is untrusted.

- Enable MathJax's `ui/safe` extension for both TeX and MathML.
- Keep safe filtering enabled for URLs, CSS classes, IDs, and styles.
- Keep MathML `allowHtmlInTokenNodes` disabled.
- Do not use `dangerouslySetInnerHTML` with TeX or MathML source.
- Mount only the DOM node returned by MathJax's conversion API.
- Preserve the existing safe-link protocol allowlist for ordinary Markdown links.

## Error Handling

- While the runtime loads, show the original formula source without blocking the message.
- If MathJax loading or conversion fails, retain readable source text in a formula fallback element.
- Do not surface a global toast or fail the entire chat bubble for a local formula error.
- Invalid or unsupported TeX commands follow MathJax's configured non-throwing error presentation where possible; rejected conversions use the source fallback.

## Presentation

- Inline formulas participate in the surrounding text flow and align with the text baseline.
- Display formulas occupy their own row and are centered when they fit.
- Oversized display formulas use horizontal scrolling within the message bubble instead of widening the conversation layout.
- Formula output inherits the current message text color where MathJax permits it.
- User-message contrast remains readable against the existing green bubble background.

## Component and File Impact

Expected implementation surface:

- `frontend/src/components/MarkdownText.tsx`: integrate formula tokens with the existing Markdown parser.
- `frontend/src/components/MathFormula.tsx`: own asynchronous MathJax conversion and fallback rendering.
- `frontend/src/components/mathFormulaParser.ts`: keep formula recognition independent from React rendering if extraction makes the parser easier to test.
- `frontend/src/components/mathJaxRuntime.ts`: configure and lazy-load the single safe MathJax runtime.
- `frontend/src/components/MarkdownText.test.tsx` and focused formula tests: lock parsing, rendering, safety, and fallback behavior.
- `frontend/src/styles.css` and `frontend/src/styles.test.ts`: add inline/display formula layout and overflow rules.
- `frontend/package.json` and `frontend/package-lock.json`: add the MathJax 4 runtime dependency.
- `frontend/AGENTS.md`: list new formula-rendering entry points if new files are added.
- `CHANGELOG.md`: record the user-visible formula-rendering capability after implementation.

Implementation may combine a small helper with its consumer when that produces a smaller and clearer change. New abstractions beyond the listed responsibilities are not required.

## Test Plan

### Parser and component tests

- Render `$x^2$` and `\(x^2\)` as inline TeX.
- Render `$$x^2$$`, `\[x^2\]`, and supported formula fences as display TeX.
- Render inline and multi-line `<math>...</math>` input through the MathML conversion path.
- Preserve formula-looking text inside inline code and ordinary code fences.
- Preserve escaped and unmatched dollar signs as text.
- Keep incomplete TeX and MathML readable during streaming and render them after completion.
- Preserve existing Markdown heading, list, table, link, strong-text, and code tests.
- Fall back to source text when MathJax loading or conversion rejects.
- Prevent stale asynchronous conversion results from replacing newer content.

### Security tests

- Verify dangerous TeX links do not produce executable URLs.
- Verify dangerous MathML URL, style, class, and ID attributes are filtered.
- Verify embedded HTML inside MathML token nodes is not accepted.
- Verify formula source is never directly inserted as HTML.

### Verification commands

Run from `frontend/`:

```bash
npm test -- --run
npm run typecheck
npm run build
```

## Acceptance Criteria

- User messages, assistant replies, reasoning content, and interrupt questions render supported TeX and MathML through the shared Markdown component.
- Existing Markdown syntax and safe-link behavior remain unchanged.
- Formula-like content in code regions remains code.
- Incomplete or invalid formulas never crash or blank a chat bubble.
- Untrusted formula input cannot create executable links or unrestricted styles through MathJax.
- Formula rendering requires no public CDN at runtime.
- Display formulas cannot expand the conversation layout beyond the message bubble.
- Focused tests, the complete frontend test suite, type checking, and the production build pass.
