# PRD05 MCP Runtime release-gate evidence

This directory stores the PRD05 MCP Runtime Rust sidecar release-gate ledger:
`mcp_runtime_release_gates.json`.

`skill_runtime_release_gates.json` and `runtime_sidecar_release_gates.json` are
not reused here because MCP Runtime has additional conformance, task registry,
streaming, recovery, and legacy-decommission gates. The checked-in ledger is
allowed to remain pending for repo-local CI; strict validation must continue to
fail closed until real deployment evidence is recorded.
