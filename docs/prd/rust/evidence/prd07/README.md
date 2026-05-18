# PRD07 Orchestration Hotspot Evidence

This directory records the machine-readable guard for `07-OrchestrationDeterministicKernel与热点优化PRD.md`.

PRD07 is intentionally a **conditional candidate**:

- it is not part of the mandatory Rust migration target set;
- it must not create `maf_orchestration_kernel` or WASM artifacts from this PRD alone;
- any future implementation must first create a separate implementation PRD with accepted performance or reliability evidence;
- LLM planner prompts, provider fallback, router glue, product answer strategy, React UI, and Ant Design components remain outside Rust/WASM scope.

Validation command:

```bash
python scripts/validate_prd07_orchestration_hotspot_evidence.py --json
```

The default ledger validates as `guarded`: the current repository is intentionally not starting the candidate. If a future implementation PRD changes the ledger to `candidate_ready_to_start`, strict validation will require all startup gates to be ready; `--allow-pending` is available only to surface pending gates in non-release CI.
