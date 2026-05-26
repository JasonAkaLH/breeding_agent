# ADR — PostgreSQL Driver for State Platform

- **Date**: 2026-05-26
- **Status**: Accepted for Phase 0 implementation
- **Decision**: Use SQLAlchemy 2.x async engine with the `postgresql+psycopg` dialect, backed by Psycopg 3, as the production PostgreSQL driver stack for the State Platform.

## Context

The repository already uses SQLAlchemy 2.x for SQLite storage. The State Platform requires PostgreSQL MVCC semantics, `FOR UPDATE SKIP LOCKED` queue claiming, SQLSTATE-aware retry, async runtime integration, bounded statement execution, and clear production fail-closed behavior.

## Options considered

| Option | Decision | Rationale |
| --- | --- | --- |
| SQLAlchemy 2.x + Psycopg 3 (`postgresql+psycopg`) | **Accepted** | Keeps one SQLAlchemy Core/engine abstraction, supports sync/async dialect selection, and Psycopg exposes SQLSTATE-oriented errors. |
| SQLAlchemy 2.x + asyncpg | Rejected for now | Strong async driver, but would add a second driver-specific error/cancel surface while the repo already standardizes on SQLAlchemy. |
| psycopg direct without SQLAlchemy | Rejected for now | Useful for low-level spikes, but would duplicate SQL construction/migration conventions and reduce reuse of existing SQLAlchemy skills. |
| Continue SQLite as production state backend | Rejected | Does not satisfy production read/write concurrency, deadlock retry, or remote deployment target. |

## Evidence and constraints

- SQLAlchemy PostgreSQL dialect docs state the Psycopg dialect provides sync and async implementations under the same dialect name.
- Psycopg 3 docs describe `AsyncConnection` / `AsyncCursor` support for asyncio.
- Psycopg error docs state it exposes classes for SQLSTATE values, allowing code to handle database conditions by SQLSTATE.
- PostgreSQL docs define row locking behavior for `SELECT FOR UPDATE`; the queue kernel must use `SKIP LOCKED` only for worker claim rows, never read paths.
- PyPI metadata for `psycopg` confirms Python 3.13 classifier and LGPL-3.0-only license expression as of 2026-05-26 lookup.

## Pool and timeout behavior

- The runtime must use bounded SQLAlchemy connection pool settings sized by deployment capacity, not unbounded per-request connections.
- Statement timeout is configured at connection/session setup for write workers and migration operations.
- Connection cancellation is used only for bounded shutdown/caller timeout paths; cancelled transactions must be rolled back before reuse in the pool.

## Runtime rules

1. Production backend selection is explicit via `MAF_STATE_STORE_BACKEND=postgresql`.
2. Production startup requires `MAF_POSTGRES_STATE_DSN`; missing DSN fails closed.
3. The DSN is read only from deployment environment or git-ignored bootstrap config; it must never be written to tracked files or health/audit output.
4. Missing driver fails closed when PostgreSQL backend is required.
5. PostgreSQL migration readiness must be explicit; startup must not run implicit DDL.
6. RuntimeSidecar enforce writer and State Platform canonical writer cannot both own the same business state path.

## Retry policy

The State Platform retries only allowlisted transient PostgreSQL SQLSTATEs:

| SQLSTATE | Meaning used by platform | Handling |
| --- | --- | --- |
| `40P01` | deadlock detected | bounded retry |
| `40001` | serialization failure | bounded retry |
| `55P03` | lock not available | bounded retry |
| `57014` | query canceled / statement timeout | bounded retry only when operation policy allows |

Unknown driver, business, validation, security, or handler errors are non-retry fail-closed by default.

## Follow-up before production ready claim

- Add pinned `psycopg` dependency only when the runtime connects to real PostgreSQL in deployment/CI.
- Run real PostgreSQL integration tests for `FOR UPDATE SKIP LOCKED`, same-partition prior guard, lease reclaim, and read-not-blocked MVCC.
- Record license and supply-chain evidence in the PR that introduces the dependency.
