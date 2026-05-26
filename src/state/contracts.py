from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from src.core.models import Conversation, Message, Task
from .errors import StatePlatformError, redact_text, redact_value

JsonMapping = Mapping[str, Any]


class CommandStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.DEAD_LETTERED, self.CANCELLED}


@dataclass(slots=True, frozen=True)
class StateCommand:
    command_id: str
    command_type: str
    idempotency_key: str
    payload_fingerprint: str
    partition_key: str
    payload: JsonMapping = field(default_factory=dict, repr=False, compare=False)
    priority: int = 0
    created_at: datetime | None = None
    metadata: JsonMapping = field(default_factory=dict, repr=False, compare=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type,
            "idempotency_key": self.idempotency_key,
            "payload_fingerprint": self.payload_fingerprint,
            "partition_key": self.partition_key,
            "priority": self.priority,
            "created_at": self.created_at,
            "metadata": _redact_mapping(self.metadata),
            "payload_redacted": True,
        }


@dataclass(slots=True, frozen=True)
class StateCommandResult:
    command_id: str
    status: CommandStatus
    result: JsonMapping = field(default_factory=dict, repr=False)
    error: StatePlatformError | None = None
    completed_at: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status.value,
            "result": _redact_mapping(self.result),
            "error": None if self.error is None else self.error.public_dict(),
            "completed_at": self.completed_at,
        }


@dataclass(slots=True, frozen=True)
class StateCommandRecord:
    command: StateCommand
    status: CommandStatus = CommandStatus.PENDING
    partition_sequence: int = 0
    priority: int = 0
    available_at: datetime | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = field(default=None, repr=False)
    result: JsonMapping = field(default_factory=dict, repr=False)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            **self.command.public_dict(),
            "status": self.status.value,
            "partition_sequence": self.partition_sequence,
            "available_at": self.available_at,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "last_error_code": self.last_error_code,
            "last_error_message": redact_text(self.last_error_message),
            "result": _redact_mapping(self.result),
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


@dataclass(slots=True, frozen=True)
class StateHealthSnapshot:
    db_status: str
    migration_status: str
    queue_backlog: int
    oldest_pending_age_seconds: float | None
    dead_letter_count: int
    worker_heartbeat_status: str
    ready: bool
    degraded_reason: str | None = field(default=None, repr=False)
    checked_at: datetime | None = None
    metadata: JsonMapping = field(default_factory=dict, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "db_status": self.db_status,
            "migration_status": self.migration_status,
            "queue_backlog": self.queue_backlog,
            "oldest_pending_age_seconds": self.oldest_pending_age_seconds,
            "dead_letter_count": self.dead_letter_count,
            "worker_heartbeat_status": self.worker_heartbeat_status,
            "ready": self.ready,
            "degraded_reason": redact_text(self.degraded_reason),
            "checked_at": self.checked_at,
            "metadata": _redact_mapping(self.metadata),
        }


@runtime_checkable
class StateReadStore(Protocol):
    async def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    async def list_messages_for_conversation(self, conversation_id: str) -> list[Message]: ...

    async def get_task(self, task_id: str) -> Task | None: ...


@runtime_checkable
class StateWriteQueue(Protocol):
    async def enqueue(self, command: StateCommand, *, max_attempts: int = 3) -> StateCommandRecord: ...

    async def claim_next(self, *, worker_id: str, lease_seconds: int = 30) -> StateCommandRecord | None: ...

    async def complete(self, command_id: str, result: JsonMapping | None = None) -> StateCommandRecord: ...

    async def fail(self, command_id: str, error: StatePlatformError) -> StateCommandRecord: ...


@runtime_checkable
class StateCommandHandler(Protocol):
    command_type: str

    def partition_key_for(self, payload: JsonMapping) -> str: ...

    async def execute(self, payload: JsonMapping) -> JsonMapping: ...


@runtime_checkable
class StateHealthProvider(Protocol):
    async def health(self) -> StateHealthSnapshot: ...

    async def readiness(self) -> StateHealthSnapshot: ...


@runtime_checkable
class StateService(Protocol):
    async def query(self) -> StateReadStore: ...

    async def submit_command(self, command: StateCommand) -> StateCommandRecord: ...

    async def execute_command_and_wait(self, command: StateCommand, *, timeout_seconds: float) -> StateCommandResult: ...

    async def transactional_command_group(self, commands: Iterable[StateCommand]) -> tuple[StateCommandRecord, ...]: ...

    async def health(self) -> StateHealthSnapshot: ...

    async def readiness(self) -> StateHealthSnapshot: ...


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return redact_value(value)
