# PRD04 Skill Runtime evidence ledger

This directory tracks the release-gate evidence for `docs/prd/rust/04-SkillRuntime与SkillOwnedRust接入PRD.md`.

`skill_runtime_release_gates.json` is intentionally fail-closed: strict validation must fail until real CI/deployment artifact allowlists, benchmark reports, shadow promotion data, ops drills, and decommission evidence are present. CI may run `scripts/validate_prd04_skill_runtime_evidence.py --allow-pending` only to prove the pending gates are explicit and machine-readable.

Do not replace production-only evidence with local smoke tests or synthetic JSON. Synthetic complete evidence is allowed only inside tests that validate the gate shape.
