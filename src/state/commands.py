from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Mapping

from .contracts import StateCommand

CONVERSATION_COMMANDS = frozenset({"conversation.create", "conversation.update", "message.append", "pending_skill_context.save"})
TASK_COMMANDS = frozenset({"task.create", "task.update", "task_node.save", "task_edge.save", "event.append", "artifact.save", "interrupt.save", "cancel.request", "mailbox.deliver"})
AUTH_COMMANDS = frozenset({"auth.login", "auth.rotate_token", "auth.logout"})
MIGRATION_COMMANDS = frozenset({"migration.cutover", "migration.rollback"})


def payload_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def command_partition_key(command_type: str, payload: Mapping[str, Any]) -> str:
    if command_type in CONVERSATION_COMMANDS or "conversation_id" in payload:
        value = payload.get("conversation_id")
        if not value:
            raise ValueError("conversation command requires conversation_id")
        return f"conversation:{value}"
    if command_type in TASK_COMMANDS or "task_id" in payload:
        value = payload.get("task_id")
        if not value:
            raise ValueError("task command requires task_id")
        return f"task:{value}"
    if command_type in AUTH_COMMANDS or "username" in payload:
        value = payload.get("username")
        if not value:
            raise ValueError("auth command requires username")
        return f"auth:{value}"
    if command_type in MIGRATION_COMMANDS:
        return "system:migration"
    raise ValueError(f"Unknown command partition rule for {command_type}")


def build_command(
    *,
    command_type: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
    command_id: str | None = None,
    partition_key: str | None = None,
    priority: int = 0,
    created_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> StateCommand:
    return StateCommand(
        command_id=command_id or f"cmd-{uuid4().hex}",
        command_type=command_type,
        idempotency_key=idempotency_key,
        payload_fingerprint=payload_fingerprint(payload),
        partition_key=partition_key or command_partition_key(command_type, payload),
        payload=dict(payload),
        priority=priority,
        created_at=created_at or datetime.now(timezone.utc).replace(tzinfo=None),
        metadata=dict(metadata or {}),
    )
