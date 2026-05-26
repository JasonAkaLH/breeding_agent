from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable

from .contracts import CommandStatus, StateCommand, StateCommandRecord, StateCommandResult, StateHealthSnapshot
from .errors import RETRYABLE_SQLSTATES, StatePlatformError
from .postgres.read_store import InMemoryPostgresReadStore
from .postgres.write_queue import InMemoryPostgresWriteQueue


def should_retry_command_error(error: StatePlatformError) -> bool:
    return error.retryable and error.sqlstate in RETRYABLE_SQLSTATES


class InMemoryStateService:
    def __init__(self, *, queue: InMemoryPostgresWriteQueue, read_store: InMemoryPostgresReadStore | None = None) -> None:
        self._queue = queue
        self._read_store = read_store or InMemoryPostgresReadStore()

    async def query(self) -> InMemoryPostgresReadStore:
        return self._read_store

    async def submit_command(self, command: StateCommand) -> StateCommandRecord:
        return await self._queue.enqueue(command)

    async def execute_command_and_wait(self, command: StateCommand, *, timeout_seconds: float) -> StateCommandResult:
        record = await self._queue.enqueue(command)
        if record.status.terminal:
            return StateCommandResult(
                record.command.command_id,
                record.status,
                record.result,
                completed_at=record.completed_at,
            )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            current = self._record(record.command.command_id)
            if current.status.terminal:
                return StateCommandResult(
                    current.command.command_id,
                    current.status,
                    current.result,
                    completed_at=current.completed_at,
                )
            await asyncio.sleep(min(0.01, max(0.0, deadline - asyncio.get_running_loop().time())))
        return StateCommandResult(
            record.command.command_id,
            CommandStatus.FAILED,
            error=StatePlatformError("state_command_timeout", "State command timed out waiting for terminal status", retryable=False),
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

    async def transactional_command_group(self, commands: Iterable[StateCommand]) -> tuple[StateCommandRecord, ...]:
        snapshot = self._queue.snapshot()
        try:
            records = []
            for command in commands:
                records.append(await self._queue.enqueue(command))
            return tuple(records)
        except Exception:
            self._queue.restore(snapshot)
            raise

    async def health(self) -> StateHealthSnapshot:
        return await self.readiness()

    async def readiness(self) -> StateHealthSnapshot:
        pending = [record for record in self._queue.records if not record.status.terminal]
        dead = [record for record in self._queue.records if record.status == CommandStatus.DEAD_LETTERED]
        return StateHealthSnapshot(
            db_status="ok",
            migration_status="ready",
            queue_backlog=len(pending),
            oldest_pending_age_seconds=None,
            dead_letter_count=len(dead),
            worker_heartbeat_status="unknown",
            ready=True,
        )

    def _record(self, command_id: str) -> StateCommandRecord:
        for record in self._queue.records:
            if record.command.command_id == command_id:
                return record
        raise KeyError(command_id)
