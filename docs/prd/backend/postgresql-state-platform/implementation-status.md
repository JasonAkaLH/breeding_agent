# PostgreSQL State Platform Implementation Status

- **Date**: 2026-05-26
- **Scope completed in this implementation pass**: Phase 0 through Phase 4 repo-local contract, SQL descriptor, fake-kernel, fail-closed, observability and safe migration planning foundations.
- **Production cutover status**: Not executed. User has not provided production PostgreSQL address, and Phase 4 requires external operator evidence.

## Completed

1. Phase 0 driver ADR, State Platform contracts, command DTOs, typed error policy, health/readiness model, and redaction tests.
2. Phase 1 PostgreSQL schema descriptors, claim SQL with `FOR UPDATE SKIP LOCKED` plus same-partition prior guard, deterministic queue kernel test double, idempotency, lease reclaim, retry/dead-letter behavior.
3. Phase 2 handler registry/specs, read store contract that avoids pending queue/write locks, StateService submit / execute-and-wait / command group semantics.
4. Phase 3 runtime config factory with production fail-closed rules and health/telemetry redaction; runbook added.
5. Phase 4 safe migration/cutover planning primitives: dry-run plan, cutover readiness evaluator, migration evidence redaction.

## Still gated for production readiness

- No real PostgreSQL DSN has been provided; integration tests that require a live PostgreSQL instance remain skipped with `postgres_test_dsn_not_configured`.
- No production migration, shadow compare, backup/restore drill, cutover, or rollback drill has been executed.
- No PostgreSQL driver dependency has been added to `requirements.txt` in this pass; the ADR records the accepted stack and the runtime factory fails closed if PostgreSQL is required and the driver is missing.


## Review fixes applied

- Expired `leased` commands are explicitly eligible for reclaim in the PostgreSQL claim SQL contract.
- `build_schema_ddl()` now emits schema DDL only; worker claim SQL remains a separate runtime SQL contract.
- `state_write_archive` descriptor and DDL both include archived command columns.
- Runtime and migration scripts reject raw DSN command-line arguments and require DSN lookup through environment variables.
- Public metadata redaction is recursive for nested mappings/lists, and queue retry uses the allowlisted SQLSTATE policy.
