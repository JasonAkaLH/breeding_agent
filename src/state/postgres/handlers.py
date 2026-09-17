from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any

PRODUCTION_COMMAND_TYPES = frozenset(
    {
        "conversation.create",
        "conversation.update",
        "message.append",
        "pending_skill_context.save",
        "task.create",
        "task.update",
        "task_node.save",
        "event.append",
        "artifact.save",
        "interrupt.save",
        "cancel.request",
        "mailbox.deliver",
        "auth.login",
        "auth.rotate_token",
        "auth.logout",
        "migration.cutover",
        "migration.rollback",
    }
)


@dataclass(frozen=True, slots=True)
class CommandHandlerSpec:
    command_type: str
    partition_scope: str
    idempotency_fields: tuple[str, ...]
    lock_order: tuple[str, ...]
    payload_schema: Mapping[str, str]
    result_schema: Mapping[str, str]
    allows_external_io: bool = False
    retryable_sqlstates: tuple[str, ...] = ("40P01", "40001", "55P03", "57014")

    def partition_key_for(self, payload: Mapping[str, Any]) -> str:
        if self.partition_scope == "conversation":
            return f"conversation:{payload['conversation_id']}"
        if self.partition_scope == "task":
            return f"task:{payload['task_id']}"
        if self.partition_scope == "auth":
            return f"auth:{payload['username']}"
        return "system:migration"


class CommandHandlerRegistry:
    def __init__(self, specs: tuple[CommandHandlerSpec, ...]) -> None:
        self._specs = {spec.command_type: spec for spec in specs}

    def get(self, command_type: str) -> CommandHandlerSpec:
        return self._specs[command_type]

    def command_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def handlers_allowing_external_io(self) -> list[str]:
        return sorted(command_type for command_type, spec in self._specs.items() if spec.allows_external_io)


def _spec(command_type: str, scope: str, idempotency: tuple[str, ...], locks: tuple[str, ...]) -> CommandHandlerSpec:
    return CommandHandlerSpec(
        command_type=command_type,
        partition_scope=scope,
        idempotency_fields=idempotency,
        lock_order=locks,
        payload_schema={field: "required" for field in idempotency},
        result_schema={"updated": "boolean"},
    )

DEFAULT_HANDLER_REGISTRY = CommandHandlerRegistry(
    (
        _spec("conversation.create", "conversation", ("conversation_id",), ("conversation",)),
        _spec("conversation.update", "conversation", ("conversation_id",), ("conversation",)),
        _spec("message.append", "conversation", ("conversation_id", "message_id"), ("conversation", "message")),
        _spec("pending_skill_context.save", "conversation", ("conversation_id", "context_id"), ("conversation", "pending_skill_context")),
        _spec("task.create", "task", ("task_id",), ("task",)),
        _spec("task.update", "task", ("task_id",), ("task",)),
        _spec("task_node.save", "task", ("task_id", "node_id"), ("task", "task_node")),
        _spec("event.append", "task", ("task_id", "event_id"), ("task", "event")),
        _spec("artifact.save", "task", ("task_id", "artifact_id"), ("task", "artifact")),
        _spec("interrupt.save", "task", ("task_id", "interrupt_id"), ("task", "interrupt")),
        _spec("cancel.request", "task", ("task_id",), ("task", "cancel")),
        _spec("mailbox.deliver", "task", ("task_id", "message_id"), ("task", "mailbox")),
        _spec("auth.login", "auth", ("username",), ("auth_user_token",)),
        _spec("auth.rotate_token", "auth", ("username",), ("auth_user_token",)),
        _spec("auth.logout", "auth", ("username",), ("auth_user_token",)),
        _spec("migration.cutover", "system", ("migration_id",), ("migration_ledger",)),
        _spec("migration.rollback", "system", ("migration_id",), ("migration_ledger",)),
    )
)
