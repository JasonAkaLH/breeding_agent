from __future__ import annotations

import os

CLAIM_NEXT_COMMAND_SQL = """
WITH candidate AS (
    SELECT command_id
    FROM state_write_command candidate
    WHERE (
          candidate.status IN ('pending', 'retry_scheduled')
          OR (candidate.status = 'leased' AND candidate.lease_expires_at <= now())
      )
      AND candidate.available_at <= now()
      AND NOT EXISTS (
          SELECT 1
          FROM state_write_command prior
          WHERE prior.partition_key = candidate.partition_key
            AND prior.partition_sequence < candidate.partition_sequence
            AND prior.status NOT IN ('succeeded', 'failed', 'dead_lettered', 'cancelled')
      )
    ORDER BY candidate.priority DESC, candidate.created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE state_write_command command
SET status = 'leased',
    lease_owner = :worker_id,
    lease_expires_at = now() + (:lease_seconds || ' seconds')::interval,
    attempt_count = command.attempt_count + 1,
    updated_at = now()
FROM candidate
WHERE command.command_id = candidate.command_id
RETURNING command.*;
""".strip()


def postgres_test_dsn_or_skip_reason() -> tuple[str | None, str | None]:
    dsn = os.environ.get("MAF_POSTGRES_TEST_DSN", "").strip()
    if not dsn:
        return None, "postgres_test_dsn_not_configured"
    return dsn, None
