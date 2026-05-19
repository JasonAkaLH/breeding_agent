# PRD05 MCP Runtime release-gate evidence

This directory stores the PRD05 MCP Runtime Rust sidecar release-gate ledger:
`mcp_runtime_release_gates.json`.

`skill_runtime_release_gates.json` and `runtime_sidecar_release_gates.json` are
not reused here because MCP Runtime has additional conformance, task registry,
streaming, recovery, and legacy-decommission gates. The checked-in ledger is
allowed to remain pending for repo-local CI; strict validation must continue to
fail closed until real deployment evidence is recorded.

The `conformance_report` gate may include nested repo-local MCP client
compatibility evidence for the four supported MCP spec versions. That nested
evidence documents Python client behavior only and does not satisfy the PRD05
Rust sidecar production conformance gate while `conformance_report.status`
remains `pending`.

The nested client compatibility evidence may also record the official Rust SDK
adapter lane (`rmcp` client-only dependency, 2025+ Streamable HTTP shadow
compare, 2024 legacy HTTP+SSE skipped gap, and adapter enforce allowlist).
Those fields are traceability for PRD03/PRD04 only: they must not be interpreted
as production artifact promotion, 7-day shadow, benchmark, ops/recovery drill,
rollback drill, or legacy decommission evidence.
