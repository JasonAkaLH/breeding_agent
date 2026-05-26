# PostgreSQL State Platform Runbook

## Readiness checklist

- `MAF_STATE_STORE_BACKEND=postgresql` is set only in environments where PostgreSQL is intended canonical state.
- `MAF_POSTGRES_STATE_DSN` is provided through deployment environment or git-ignored config bootstrap only.
- Migration ledger reports ready before serving production traffic.
- Queue backlog, oldest pending age, dead-letter count, and worker heartbeat are visible.
- RuntimeSidecar writer is not in enforce mode for the same canonical state tables.

## Dead-letter triage

1. Inspect redacted dead-letter metadata: command type, partition category, SQLSTATE/error code, attempt count, timestamps.
2. Do not replay commands containing unknown security/contract errors without manual remediation.
3. For transient SQLSTATEs (`40P01`, `40001`, `55P03`, `57014`), verify the root cause has cleared before replay.
4. Replay through the State Platform command API, not by mutating business tables directly.

## Worker drain and lease recovery

1. Set worker drain mode in deployment orchestration.
2. Wait for in-flight leased commands to finish or lease expiry to pass.
3. Confirm stale leases are reclaimable and same-partition ordering is preserved.
4. Restart workers gradually and watch duplicate-claim / dead-letter metrics.

## Migration and cutover gate

- Phase 4 must complete dry-run, import, validation, shadow compare, backup/restore drill, and operator confirmation before cutover.
- Cutover is blocked if queue backlog is non-zero, dead-letter is non-empty, shadow compare has critical diffs, or readiness is false.
- SQLite is not a production fallback. Rollback means restoring a known cutover-pre snapshot, not running long-term dual primary writes.

## Backup / restore drill

1. Snapshot PostgreSQL and legacy SQLite before import/cutover.
2. Restore into staging and run row count/checksum validation.
3. Record operator, timestamp, artifact references, and pending gates.
4. Keep reports redacted: no DSN, token, password, raw prompt, or user payload.
