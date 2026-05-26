from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Any

from src.state.contracts import CommandStatus, StateCommand, StateCommandRecord
from src.state.errors import RETRYABLE_SQLSTATES, StatePlatformError


class IdempotencyConflictError(ValueError):
    pass


class InMemoryPostgresWriteQueue:
    """Deterministic queue kernel for contract tests; SQL-backed implementation uses the same invariants."""

    def __init__(self, *, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._records: dict[str, StateCommandRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._partition_next: dict[str, int] = {}
        self.dead_letters: list[StateCommandRecord] = []

    def snapshot(self) -> tuple[dict[str, StateCommandRecord], dict[tuple[str, str], str], dict[str, int], list[StateCommandRecord]]:
        return (dict(self._records), dict(self._idempotency), dict(self._partition_next), list(self.dead_letters))

    def restore(self, snapshot: tuple[dict[str, StateCommandRecord], dict[tuple[str, str], str], dict[str, int], list[StateCommandRecord]]) -> None:
        records, idempotency, partition_next, dead_letters = snapshot
        self._records = dict(records)
        self._idempotency = dict(idempotency)
        self._partition_next = dict(partition_next)
        self.dead_letters = list(dead_letters)

    @property
    def records(self) -> tuple[StateCommandRecord, ...]:
        return tuple(self._records.values())

    def _now(self) -> datetime:
        now = self._now_fn()
        return now.replace(tzinfo=None) if now.tzinfo is not None else now

    async def enqueue(self, command: StateCommand, *, max_attempts: int = 3) -> StateCommandRecord:
        key = (command.command_type, command.idempotency_key)
        existing_id = self._idempotency.get(key)
        if existing_id is not None:
            existing = self._records[existing_id]
            if existing.command.payload_fingerprint != command.payload_fingerprint:
                raise IdempotencyConflictError("idempotency key reused with different payload")
            return existing
        now = self._now()
        sequence = self._partition_next.get(command.partition_key, 0) + 1
        self._partition_next[command.partition_key] = sequence
        record = StateCommandRecord(
            command=command,
            status=CommandStatus.PENDING,
            partition_sequence=sequence,
            priority=command.priority,
            available_at=now,
            attempt_count=0,
            max_attempts=max_attempts,
            created_at=command.created_at or now,
            updated_at=now,
        )
        self._records[command.command_id] = record
        self._idempotency[key] = command.command_id
        return record

    async def claim_next(self, *, worker_id: str, lease_seconds: int = 30) -> StateCommandRecord | None:
        now = self._now()
        candidates = sorted(
            self._records.values(),
            key=lambda record: (-record.priority, record.created_at or now, record.command.partition_key, record.partition_sequence),
        )
        for record in candidates:
            if record.status not in {CommandStatus.PENDING, CommandStatus.RETRY_SCHEDULED, CommandStatus.LEASED}:
                continue
            if record.available_at and record.available_at > now:
                continue
            if record.status == CommandStatus.LEASED and record.lease_expires_at and record.lease_expires_at > now:
                continue
            if self._has_prior_unfinished(record):
                continue
            leased = replace(
                record,
                status=CommandStatus.LEASED,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=record.attempt_count + 1,
                updated_at=now,
            )
            self._records[record.command.command_id] = leased
            return leased
        return None

    def _has_prior_unfinished(self, record: StateCommandRecord) -> bool:
        for other in self._records.values():
            if other.command.partition_key != record.command.partition_key:
                continue
            if other.partition_sequence >= record.partition_sequence:
                continue
            if not other.status.terminal:
                return True
        return False

    async def complete(self, command_id: str, result: Mapping[str, Any] | None = None) -> StateCommandRecord:
        record = self._records[command_id]
        now = self._now()
        completed = replace(
            record,
            status=CommandStatus.SUCCEEDED,
            result=dict(result or {}),
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            completed_at=now,
        )
        self._records[command_id] = completed
        return completed

    async def fail(self, command_id: str, error: StatePlatformError) -> StateCommandRecord:
        record = self._records[command_id]
        now = self._now()
        if error.retryable and error.sqlstate in RETRYABLE_SQLSTATES and record.attempt_count < record.max_attempts:
            failed = replace(
                record,
                status=CommandStatus.RETRY_SCHEDULED,
                lease_owner=None,
                lease_expires_at=None,
                available_at=now + timedelta(seconds=60),
                last_error_code=error.code,
                last_error_message=error.message,
                updated_at=now,
            )
        else:
            failed = replace(
                record,
                status=CommandStatus.DEAD_LETTERED if error.retryable and error.sqlstate in RETRYABLE_SQLSTATES else CommandStatus.FAILED,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error.code,
                last_error_message=error.message,
                updated_at=now,
                completed_at=now,
            )
            if failed.status == CommandStatus.DEAD_LETTERED:
                self.dead_letters.append(failed)
        self._records[command_id] = failed
        return failed
