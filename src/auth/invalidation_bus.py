from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, cast

AuthGenerationReason = Literal["login", "refresh", "logout", "revoke", "reconcile"]
_ALLOWED_REASONS = {"login", "refresh", "logout", "revoke", "reconcile"}


@dataclass(frozen=True, slots=True)
class AuthGenerationChanged:
    username: str
    auth_generation: int
    changed_at: datetime
    reason: AuthGenerationReason

    def public_payload(self) -> dict[str, object]:
        return {
            "username": self.username,
            "auth_generation": self.auth_generation,
            "changed_at": self.changed_at.isoformat(),
            "reason": self.reason,
        }


def validate_auth_generation_changed(value: object) -> AuthGenerationChanged:
    if isinstance(value, AuthGenerationChanged):
        return value
    if not isinstance(value, dict):
        raise ValueError("auth generation payload must be an object")
    username = str(value.get("username") or "").strip()
    if not username:
        raise ValueError("username is required")
    generation = int(value.get("auth_generation"))
    if generation < 0:
        raise ValueError("auth_generation must be non-negative")
    reason = str(value.get("reason") or "").strip()
    if reason not in _ALLOWED_REASONS:
        raise ValueError("unsupported auth generation reason")
    changed_at_value = value.get("changed_at")
    if isinstance(changed_at_value, datetime):
        changed_at = changed_at_value
    elif isinstance(changed_at_value, str) and changed_at_value:
        changed_at = datetime.fromisoformat(changed_at_value.replace("Z", "+00:00"))
    else:
        raise ValueError("changed_at is required")
    return AuthGenerationChanged(
        username=username,
        auth_generation=generation,
        changed_at=changed_at,
        reason=cast(AuthGenerationReason, reason),
    )


class InMemoryAuthInvalidationBus:
    """Async in-process auth generation notification bus used by tests/dev.

    This bus is deliberately payload-only and never carries token material.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[AuthGenerationChanged]] = set()

    def subscribe(self) -> asyncio.Queue[AuthGenerationChanged]:
        queue: asyncio.Queue[AuthGenerationChanged] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[AuthGenerationChanged]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: AuthGenerationChanged) -> None:
        validate_auth_generation_changed(event.public_payload())
        for queue in tuple(self._subscribers):
            await queue.put(event)

    async def drain_all(self, queue: asyncio.Queue[AuthGenerationChanged]) -> list[AuthGenerationChanged]:
        events: list[AuthGenerationChanged] = []
        while True:
            try:
                events.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                return events

    @staticmethod
    def validate_no_token_material(payload: dict[str, object], sentinels: Iterable[str]) -> None:
        serialized = repr(payload)
        for sentinel in sentinels:
            if sentinel and sentinel in serialized:
                raise ValueError("auth generation payload leaked token material")
