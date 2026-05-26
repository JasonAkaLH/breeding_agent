# PostgreSQL State Platform Implementation Status

- **Date**: 2026-05-26
- **Current decision**: PostgreSQL is a fresh canonical start. SQLite history is abandoned; no SQLite row migration, row-count/checksum validation, or shadow-read parity is required.
- **Scope completed in this implementation pass**: Phase 4 fresh cutover foundation on top of Phase 0-3 State Platform work: PostgreSQL driver dependency, runtime schema manifest/DDL, no-drop bootstrap, runtime assembly, cutover/readiness tooling, SQLite cleanup guard, and regression evidence.
- **Production cutover status**: Not executed by Codex. The user has provided remote PostgreSQL connection details in local `config.yaml`, but real serving cutover still requires operator-side schema/bootstrap smoke and readiness gate in the deployment environment.

## Completed

1. Phase 0 driver ADR, State Platform contracts, command DTOs, typed error policy, health/readiness model, and redaction tests.
2. Phase 1 PostgreSQL schema descriptors, claim SQL with `FOR UPDATE SKIP LOCKED` plus same-partition prior guard, deterministic queue kernel test double, idempotency, lease reclaim, retry/dead-letter behavior.
3. Phase 2 handler registry/specs, read store contract that avoids pending queue/write locks, StateService submit / execute-and-wait / command group semantics.
4. Phase 3 runtime config factory with production fail-closed rules and health/telemetry redaction; runbook added.
5. Phase 4 fresh cutover primitives: runtime table manifest/DDL, guarded schema reconciler, PostgreSQL bootstrap with advisory lock and bounded timeouts, PostgreSQLStorage facade, cutover CLI, migration-disabled shim, SQLite cleanup dry-run/archive/delete guard.
6. PostgreSQL driver dependency added: `psycopg[binary]==3.3.4`.
7. Runtime assembly now supports PostgreSQL backend and still keeps SQLite as dev/test default unless explicitly selected.

## Still gated for production readiness

- No Codex-executed remote DDL or destructive remote operation has been performed in this pass.
- Deployment must set PostgreSQL backend/DSN; `cutover_ready` / `MAF_STATE_PLATFORM_CUTOVER_READY` is removed from runtime gating and must not block startup.
- Real read-not-blocked/write-queue evidence against the remote PostgreSQL instance remains an operator/integration gate.
- Local SQLite files should only be archived/deleted through explicit operator cleanup after PostgreSQL cutover is accepted.

## Review fixes applied

- Expired `leased` commands are explicitly eligible for reclaim in the PostgreSQL claim SQL contract.
- `build_schema_ddl()` now emits schema DDL only; worker claim SQL remains a separate runtime SQL contract.
- `state_write_archive` descriptor and DDL both include archived command columns.
- Runtime and cutover scripts reject raw DSN command-line arguments and require DSN lookup through environment variables.
- SQLite migration script is now a compatibility shim that rejects import and points to fresh cutover.
- Public metadata redaction is recursive for nested mappings/lists, and queue retry uses the allowlisted SQLSTATE policy.
