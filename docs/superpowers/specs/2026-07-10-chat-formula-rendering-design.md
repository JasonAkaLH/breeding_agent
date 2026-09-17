# Chat Formula Rendering Design

**Date:** 2026-07-10
**Last reviewed:** 2026-07-14
**Status:** Approved for implementation planning

## Problem and Current State

The frontend uses a small custom `MarkdownText` renderer for chat content. It currently supports headings, lists, fenced code, tables, bold text, inline code, and safe links, but it displays TeX/LaTeX and MathML as plain source. Formula-heavy breeding, statistics, and experimental-design answers are therefore harder to read than the same content rendered as mathematical notation.

`MarkdownText` is already the shared rendering path for user messages, assistant replies, reasoning content, and interrupt questions. The change can remain frontend-only and does not require a new backend payload, persisted-message migration, or SSE event type.

## Goal

Render TeX/LaTeX and MathML safely, accessibly, and consistently everywhere the frontend uses `MarkdownText`, without weakening existing Markdown safety, making the initial chat bundle load MathJax eagerly, or disrupting streaming updates.

## Users and Affected Systems

- **Primary users:** authenticated SeedPilot users reading or writing mathematical chat content.
- **Affected content:** new messages and existing conversation history, because history is rendered through the same client component.
- **Affected frontend systems:** Markdown parsing, message presentation, frontend dependency packaging, Vite static assets, and frontend tests.
- **Unaffected systems:** backend message contracts, storage schemas, API routes, SSE event schemas, copy-to-clipboard payloads, and capability artifact rendering.

## Scope

The frontend must recognize these formula forms:

- Inline TeX: `$...$` and `\(...\)`
- Display TeX: `$$...$$` and `\[...\]`
- Display TeX fences: fenced blocks labelled `tex`, `latex`, or `math`
- Serialized Presentation MathML: `<math>...</math>`, including multi-line elements and `<math xmlns="http://www.w3.org/1998/Math/MathML">`

The implementation must support these forms inside every existing `MarkdownText` caller. Inline formula parsing must continue to work in headings, list items, table cells, and strong-text content when the surrounding Markdown construct already delegates to the inline renderer.

## Non-goals

- AsciiMath is not supported.
- Content MathML, prefixed forms such as `<m:math>`, and arbitrary XML are not supported.
- This change does not introduce a formula editor, equation numbering, cross-message references, or authoring toolbar.
- This change does not replace the custom Markdown parser with a general Markdown framework.
- This change does not alter backend messages, history records, SSE payloads, or model prompts.
- No custom model-output delimiter is required. The frontend accepts the standard TeX and MathML forms listed above.
- This change does not add a new copy action to user-authored messages.
- Formula analytics and product telemetry are not added in this iteration.

## Design Decisions and Evidence

1. **Use one renderer.** MathJax 4 supports TeX and MathML through a single internal model, avoiding separate error and security paths.
2. **Use SVG output.** SVG is visually consistent and avoids browser web-font rendering differences. Assistant-reply source remains available through the existing copy action because SVG glyphs themselves are not copyable.
3. **Self-host all runtime assets.** MathJax's browser loader can otherwise resolve components or dynamic font ranges through public CDN paths. The application must explicitly serve both the combined component and any dynamically requested data from its own base path.
4. **Keep formula detection in the existing parser.** Replacing the Markdown stack would expand scope and risk changing unrelated message rendering.
5. **Treat every formula as untrusted.** Both users and models can produce formula source, so unnecessary links, styles, HTML, dynamic packages, and IDs are denied rather than merely filtered permissively.

Relevant upstream references:

- MathJax local hosting: <https://docs.mathjax.org/en/latest/web/hosting.html>
- Combined TeX/MathML/SVG component: <https://docs.mathjax.org/en/v4.0/web/components/combined.html>
- Promise-based conversion: <https://docs.mathjax.org/en/v4.1/web/convert.html>
- Safe extension: <https://docs.mathjax.org/en/latest/options/safe.html>
- Dynamic font data: <https://docs.mathjax.org/en/v4.0/output/fonts.html>
- Accessibility components: <https://docs.mathjax.org/en/v4.1/options/accessibility.html>

## Architecture

### Formula tokenization

Formula detection remains part of `MarkdownText`, but parsing and rendering have separate responsibilities:

1. Block parsing first identifies ordinary fenced code so formula syntax inside it remains literal. Formula-labelled fences are the only exception.
2. Block parsing recognizes complete display TeX and complete block MathML before falling back to a paragraph.
3. Inline parsing protects inline code and Markdown link destinations before recognizing complete inline TeX or inline MathML.
4. A formula token contains `language`, `source`, `display`, and `fallbackSource` fields. TeX `source` excludes delimiters; MathML `source` contains the complete `<math>` element; `fallbackSource` preserves exactly what the user or model supplied.
5. A token longer than the formula length limit or beyond the per-message formula-count limit remains source text.

The parser must use a deterministic scanner rather than a single broad regular expression. Delimiter matching must account for escaping and incomplete streaming input.

### Formula component

An isolated `MathFormula` component owns asynchronous conversion:

1. It initially renders `fallbackSource`, so runtime loading never blanks the message.
2. It obtains the shared MathJax runtime promise.
3. It calls `tex2svgPromise()` or `mathml2svgPromise()` with the source string and the token's `display` option rather than asking MathJax to scan or mutate the whole chat DOM.
4. It mounts only the DOM node returned by MathJax. Application code must not put the original TeX or MathML string into `innerHTML` or `dangerouslySetInnerHTML`.
5. It discards a conversion result if the component unmounted or its source changed while the promise was pending.
6. It retains the source fallback when loading or conversion fails.

### Locally served MathJax runtime

Pin `mathjax` to exactly `4.1.3`. The lockfile must also pin its `@mathjax/mathjax-newcm-font` dependency. Both packages use the Apache-2.0 license.

Add a dependency-preparation script implemented with Node built-ins; no additional copy plugin is required. `predev`, `pretest`, and `prebuild` must run the script before Vite or formula integration tests. The script must recreate a generated, gitignored `frontend/public/vendor/` subtree containing only:

- `mathjax/tex-mml-svg.js`
- `mathjax/ui/safe.js`
- `mathjax/a11y/assistive-mml.js`
- `@mathjax/mathjax-newcm-font/svg/dynamic/**`

Vite copies this subtree into `dist/vendor/`. Runtime URLs and MathJax `loader.paths.mathjax` and `loader.paths.fonts` values must be derived from `import.meta.env.BASE_URL`; absolute root URLs must not be hard-coded. This preserves deployments that use `VITE_APP_BASE_PATH`.

The browser must not request MathJax code, fonts, speech maps, or extensions from a public CDN. The runtime is loaded once, on the first complete formula token. Pages without formulas must not request any MathJax asset.

### MathJax configuration

Configure MathJax before inserting the local combined-component script:

- Load `ui/safe` and `a11y/assistive-mml` from the local MathJax root.
- Set `startup.typeset` to `false`; the combined component must never scan or mutate the application document automatically.
- Remove the TeX `require` and `autoload` packages.
- Do not enable `html`, `texhtml`, or runtime package loading from formula source.
- Set `mathml.allowHtmlInTokenNodes` to `false`.
- Set safe input permissions for URLs, styles, classes, and CSS IDs to `none`.
- Disable the contextual menu, semantic enrichment, speech, and Braille so they cannot trigger Speech Rule Engine or math-map downloads.
- Enable assistive MathML for screen readers.
- Set the SVG font cache to `local` so each asynchronously mounted formula owns the paths it references and unmounting one formula cannot invalidate another.
- Use promise-based conversion for both TeX and MathML so local dynamic SVG font ranges can load safely when required.

If a required local runtime asset is absent, runtime initialization must reject and every affected formula must remain readable source text. It must not retry against another origin.

## Parsing Contract

### General precedence

The parser must apply these priorities:

1. Fenced-block recognition: formula-labelled fences route to formula parsing; every other fence routes to code
2. Inline code and Markdown link destinations
3. Complete MathML
4. Complete display TeX
5. Complete inline TeX
6. Existing Markdown tokens and plain text

Formula syntax inside ordinary code or a link destination remains literal. Existing recursive rendering may recognize formulas in strong-text content and link labels, but never in a link URL.

### Dollar delimiters

- Process `$$...$$` before `$...$`.
- A dollar delimiter is escaped when immediately preceded by an odd-length run of backslashes. Escaped delimiters lose one escape backslash and remain literal text.
- An opening single dollar must not be followed by whitespace or the end of the string.
- A closing single dollar must not be preceded by whitespace and must not be followed by a decimal digit.
- Single-dollar formulas do not span line breaks.
- Both delimiters must be complete. An unmatched dollar remains text.
- These rules intentionally keep text such as `$100 与 $200` literal while still accepting `$x^2$`.

### Backslash delimiters

- `\(...\)` is inline and may not span a line break.
- `\[...\]` is display math and may span lines.
- Delimiters preceded by an odd-length run of backslashes remain literal.
- Incomplete pairs remain source text.

### Display dollars and formula fences

- `$$...$$` is display math and may span lines.
- A `$$` or `\[` display opener is recognized only after optional whitespace at the start of a Markdown block, and its closing delimiter must be followed only by optional whitespace before that block ends. Display delimiters embedded in surrounding prose remain literal.
- A formula-labelled fence must have a closing fence. An incomplete fence uses the existing fenced-code fallback and must not invoke MathJax.
- The formula-fence language match is case-insensitive for `tex`, `latex`, and `math` only.
- Other fenced languages continue to render as code.

### MathML

- MathML recognition is case-sensitive and begins with `<math` followed by whitespace, `>`, or `/`.
- The token ends at the corresponding `</math>`; nested `<math>` elements are not supported and make the token fall back to source text.
- `<math display="block">` is display math. A complete `<math>` token that is the only non-whitespace content in its Markdown block is also display math; other complete MathML tokens are inline.
- Before conversion, `DOMParser` with `application/xml` must verify a single unprefixed `<math>` root and reject parser errors. The parsed document is used only for validation and is never mounted.
- Incomplete, syntactically malformed, oversized, nested, or prefixed MathML remains source text. Structurally unsupported but well-formed MathML may use MathJax's safe error output.
- The source is passed directly to MathJax's MathML conversion API, not parsed into application HTML.

## Streaming and Concurrency

- Incomplete TeX and MathML are plain source text and do not initialize MathJax.
- When a closing delimiter arrives, React replaces the source token with a keyed `MathFormula` component.
- MathJax initialization is represented by one module-level promise. Concurrent callers share it.
- Promise-based MathJax conversions are serialized by MathJax. Application code must not add a second global conversion queue unless testing proves it necessary.
- A stale conversion must not replace newer content after streaming updates.
- A conversion failure affects only its formula token; the rest of the message remains rendered.

## Security and Resource Limits

- Treat formula input as untrusted regardless of message role.
- Do not inject source with `innerHTML` or `dangerouslySetInnerHTML`.
- Deny formula-supplied URLs, CSS styles, classes, and IDs.
- Keep MathML HTML token content disabled.
- Disable formula-triggered TeX package loading.
- Do not log formula source, full messages, MathML attributes, or TeX commands on failure.
- Limit one formula source to **10,000 UTF-16 code units**.
- Limit one `MarkdownText` render to **100 complete formula tokens**.
- Tokens exceeding either limit remain source text and do not invoke MathJax.

These limits bound pathological model output while matching the current 10,000-character user composer limit.

## Accessibility and Copy Behavior

- Rendered formulas must expose assistive MathML and hide the visual SVG from screen readers as directed by MathJax's assistive-MathML component.
- Loading and fallback source must remain available to assistive technology as text.
- Formula rendering must not add an unexpected keyboard-focus target when the MathJax menu and explorer are disabled.
- The existing assistant-message copy button continues to copy the original assistant source, including TeX or MathML, rather than SVG markup. User-message copy behavior is unchanged and no new user-message action is added.
- User-message and assistant-message formulas must preserve readable foreground contrast.

## Error Handling and Diagnostics

- While the runtime loads, display the original formula source without a spinner or global blocking state.
- On conversion failure, add `data-formula-state="fallback"` to the local fallback element for testing and diagnosis.
- On success, add `data-formula-state="rendered"` and identify the input language without exposing source content.
- Log at most one runtime-initialization warning per page load. Conversion warnings may include the input language and error category, but must not include formula source.
- Do not show a global toast or fail the entire chat bubble for a formula error.
- A rejected runtime promise remains rejected for that page load; formulas stay in fallback state until reload rather than retrying repeatedly.

## Presentation

- Inline formulas use an inline container, align with the surrounding baseline, and must not increase the message width.
- Display formulas occupy their own row and are centered when they fit.
- Inline and display containers both have `max-width: 100%`; oversized output scrolls horizontally inside the formula container.
- Formula output uses `currentColor` so user-message formulas remain visible on the green bubble and assistant formulas follow normal text color.
- Formula containers must not override the existing `.message-body` width or conversation scrolling behavior.
- Source fallback uses `white-space: pre-wrap` and remains selectable.

## Performance and Packaging Requirements

- No MathJax network request may occur before the first complete formula is detected.
- Runtime initialization and conversion must not block rendering of surrounding Markdown or source fallback.
- The pinned `tex-mml-svg.js` startup asset must remain at or below **2.0 MB uncompressed**.
- The generated `dist/vendor/` MathJax subtree must remain at or below **15 MB uncompressed** for the pinned version.
- Runtime loading must be cached for the page lifetime.
- A production build must not add MathJax to the initial Vite application chunk.
- Real-browser verification must confirm that representative TeX and MathML formulas make only same-origin MathJax requests.

Any future MathJax upgrade that exceeds these bounds requires a separate design decision rather than silently increasing the limits.

## Compatibility, Rollout, and Rollback

- No database or API migration is required.
- The feature applies to existing conversation history after the new frontend is deployed.
- Conservative dollar rules protect ordinary currency-like historical text; escaped or unmatched delimiters remain readable.
- Roll out through the normal frontend build and deployment path without a feature flag.
- Before release, smoke-test a conversation containing plain Markdown, currency text, TeX, MathML, code, a long display formula, and an intentionally invalid formula.
- Roll back by deploying the preceding frontend bundle and removing the MathJax dependency/assets in a later cleanup commit. Persisted messages remain unchanged and require no repair.

## Dependencies and License

- Direct dependency: exact `mathjax@4.1.3`
- Transitive runtime dependency: lockfile-pinned `@mathjax/mathjax-newcm-font@4.1.3`
- License: Apache-2.0 for both packages
- No additional runtime, copy-plugin, Markdown, XML, or test dependency is required by this design.

`CHANGELOG.md` must record the dependency and Apache-2.0 license impact when implementation lands.

## Component and File Impact

Expected implementation surface:

- `frontend/src/components/MarkdownText.tsx`: integrate formula tokens with the existing Markdown parser.
- `frontend/src/components/MathFormula.tsx`: own asynchronous MathJax conversion, stale-result protection, accessibility output, and fallback rendering.
- `frontend/src/components/mathFormulaParser.ts`: keep deterministic formula recognition independent from React rendering.
- `frontend/src/components/mathJaxRuntime.ts`: configure and lazy-load the single local MathJax runtime.
- `frontend/scripts/prepare_mathjax_assets.mjs`: recreate the allowlisted generated vendor subtree.
- `frontend/src/components/MarkdownText.test.tsx` and focused formula tests: lock parsing, rendering, limits, safety, accessibility, and fallback behavior.
- `frontend/src/styles.css` and `frontend/src/styles.test.ts`: add inline/display formula layout and overflow rules.
- `frontend/package.json` and `frontend/package-lock.json`: pin MathJax and invoke asset preparation before dev, tests, and builds.
- `.gitignore`: ignore the generated `frontend/public/vendor/` subtree.
- `frontend/AGENTS.md`: list the new formula-rendering and asset-preparation entry points.
- `CHANGELOG.md`: record the user-visible capability and license impact.

Implementation may combine a small parser helper with its consumer only if the resulting file remains focused and independently testable. It must not add abstractions outside these responsibilities.

## Test Plan

### Parser unit tests

- Render `$x^2$` and `\(x^2\)` as inline TeX.
- Render `$$x^2$$`, `\[x^2\]`, and supported formula fences as display TeX.
- Recognize formula-fence labels case-insensitively.
- Render inline and multi-line `<math>...</math>` through the MathML path.
- Preserve formula-looking text inside inline code, ordinary code fences, and link destinations.
- Render formulas inside existing heading, list, table-cell, and strong-text paths.
- Preserve `$100 与 $200`, escaped delimiters, unmatched delimiters, and incomplete streaming source as text.
- Preserve incomplete formula fences as code.
- Reject nested, malformed, prefixed, and oversized MathML as formula tokens.
- Enforce the 10,000-code-unit and 100-token limits.
- Preserve existing Markdown heading, list, table, link, strong-text, and code tests.

### Component tests

- Show source while the runtime promise is pending.
- Mount the MathJax-returned DOM node on success.
- Fall back to source when runtime loading or conversion rejects.
- Prevent stale asynchronous results from replacing newer content.
- Share a single runtime initialization across multiple formula components.
- Preserve original message text for the existing copy action.
- Expose rendered/fallback diagnostic state without exposing source in attributes or warnings.

### Real-engine integration tests

At least one test must use the prepared local MathJax runtime rather than a mocked converter and verify:

- TeX and MathML both produce SVG output.
- Assistive MathML is present and visual SVG is hidden from screen readers.
- `javascript:`, `data:`, `file:`, formula-supplied styles, classes, and IDs do not survive into rendered output.
- HTML inside MathML token nodes is not rendered.
- `\require`, autoloaded extensions, and TeX HTML cannot trigger component loading.
- Rare dynamic font data resolves from the local font path.
- Failed component or font requests do not fall back to another origin.

### Styles and browser smoke tests

- Verify CSS rules for inline alignment, `max-width`, horizontal overflow, fallback selection, and `currentColor`.
- In the supported desktop browser used for release smoke testing, verify user and assistant bubbles, reasoning content, interrupt content, long formulas, invalid formulas, browser zoom, and narrow viewport behavior.
- Inspect the network log and confirm that pages without formulas request no MathJax assets and pages with formulas request MathJax only from the application origin.
- Confirm formulas do not create unexpected tab stops.

### Verification commands

Run from `frontend/` unless noted:

```bash
npm test -- --run
npm run typecheck
npm run build
npm ls mathjax @mathjax/mathjax-newcm-font
```

Also inspect generated asset sizes and execute the browser smoke checklist before release.

## Acceptance Criteria

| Area | Required outcome |
|---|---|
| Coverage | User messages, assistant replies, reasoning content, and interrupt questions render supported TeX and MathML through `MarkdownText`. |
| Markdown compatibility | Existing Markdown tests pass; code regions, link destinations, currency-like text, escapes, and incomplete delimiters retain readable source. |
| Streaming | Incomplete formulas remain source, complete formulas render, and stale conversions cannot replace newer content. |
| Safety | Untrusted formula input cannot create URLs, styles, classes, IDs, embedded HTML, or dynamic TeX component requests. |
| Accessibility | Successful output includes assistive MathML, fallback remains readable, no unexpected tab stop is added, and assistant-reply source remains copyable through the existing action. |
| Failure isolation | Loading or conversion failure affects only the local formula and produces no global chat failure. |
| Offline delivery | All MathJax code and dynamic font requests are same-origin and base-path aware; no public CDN is required. |
| Performance | MathJax is absent from initial requests, the startup asset is at most 2.0 MB uncompressed, generated vendor assets are at most 15 MB uncompressed, and parser resource limits are enforced. |
| Layout | Inline and display formulas remain inside the message bubble at normal and narrow widths. |
| Release | Focused tests, the full frontend test suite, type checking, production build, dependency inspection, asset-size check, and browser smoke test pass. |

## Risks and Assumptions

- **Historical reinterpretation:** existing messages containing valid formula delimiters will render as formulas after deployment. Conservative delimiter rules reduce false positives but cannot distinguish every intentional dollar expression.
- **Third-party size:** MathJax is materially larger than current frontend dependencies. Lazy loading and an allowlisted static subtree prevent it from increasing the initial chat payload.
- **SVG tradeoff:** SVG glyphs are not directly copyable. Existing assistant-message copy plus assistive MathML are the required compensating behaviors; adding a user-message copy action remains out of scope.
- **Dynamic font data:** uncommon glyphs can request additional font-range files. Those files must be packaged locally and tested with at least one non-basic range.
- **Browser assumption:** the supported browser set remains the latest stable Chromium, Firefox, and Safari releases capable of running the existing Vite ES2022 build. Release smoke testing covers Chromium and Safari; no legacy-browser support is introduced.
