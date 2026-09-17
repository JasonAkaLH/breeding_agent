# MCP Result Text Artifact Design

> 公共展示决定已由 `2026-08-20-mcp-versioned-result-parsing-design.md` 替代。
> 受校验的内部 managed-file 副本和生命周期保证继续有效，但公共 API 不再公开完整 MCP raw JSON 正文。

## Decision

MCP Tool JSON results are public text artifacts, not downloadable files. The public task and conversation-history APIs must return each MCP result as `artifact_type=text`, with the original UTF-8 JSON body in `storage_ref` and no `download_url`.

The verified managed-file copy remains an internal persistence detail. Keeping it preserves the existing byte-count, SHA-256, lifecycle CAS, recovery, and historical backfill guarantees without copying result bodies into the artifact database row.

## Implementation Status

Implemented on `main` on 2026-08-20. Backend task/history APIs, the download boundary, secure UTF-8 reads, frontend parsing/rendering, regression tests, API documentation, and repository indexes all follow this design. No database migration or external dependency was added.

## Scope

- Apply the text projection to both newly created and existing MCP result artifacts whose managed-file metadata has `source_kind=mcp_result`.
- Preserve the original result bytes as text; do not parse, pretty-print, summarize, or rewrite the JSON body.
- Reject direct download requests for MCP result artifacts, including previously issued artifact URLs.
- Keep Skill output files and their download behavior unchanged.
- Render each MCP text artifact as a supplemental, expandable raw-result card. Never select it as the assistant's message text.

## Data Flow

1. The existing MCP projector verifies and promotes the durable result into the private artifact file store.
2. Task and conversation-history response builders recognize `source_kind=mcp_result`, read the managed UTF-8 body, and return a public text artifact DTO.
3. The frontend recognizes the deterministic `mcp-result-artifact:v1:` identifier and renders the exact text in a collapsed raw-result card.
4. The artifact download route accepts active Skill outputs only; MCP result artifacts return 404.

This adapter boundary also converts historical MCP artifacts on read, so no database migration or source-result replay is required.

## Failure Behavior

- A missing, inactive, undecodable, or unreadable internal MCP result body is not downgraded to a downloadable file.
- The API fails closed rather than exposing a stale download URL or silently changing the original content.
- Ordinary non-MCP text artifacts and Skill file artifacts retain their current behavior.

## Acceptance Criteria

1. Task artifact and conversation-history responses expose MCP result artifacts as text with exact original content and no download metadata.
2. The MCP artifact download endpoint returns 404, while Skill file downloads remain available.
3. The frontend displays every MCP result text artifact as an expandable raw-return card.
4. MCP result text cannot replace or become the assistant answer.
5. Existing projection, lifecycle, API, and frontend regression tests remain green.

## Implementation Plan

1. Add a secure UTF-8 read operation to the managed artifact store and a public MCP-text projection branch in the artifact response builder.
2. Pass the artifact store into task/history response construction and narrow the download route to Skill output files.
3. Add the MCP text display model, parser, card, and assistant-text exclusion in the frontend.
4. Add focused backend and frontend regression tests, update API documentation and repository indexes, then run targeted and relevant suite checks.
