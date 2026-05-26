from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core.contracts import AuditSink, EventSink
from src.core.enums import EventVisibility
from src.core.models import EventRecord


@dataclass(slots=True)
class EventSubscription:
    task_id: str
    queue: asyncio.Queue[EventRecord]
    _broker: "InMemoryEventBroker"

    async def get(self) -> EventRecord:
        return await self.queue.get()

    def close(self) -> None:
        self._broker.unsubscribe(self.task_id, self.queue)


class InMemoryEventBroker(EventSink):
    def __init__(self, *, audit_sink: AuditSink | None = None) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[EventRecord]]] = {}
        self._audit_sink = audit_sink

    def subscribe(self, task_id: str) -> EventSubscription:
        queue: asyncio.Queue[EventRecord] = asyncio.Queue()
        self._subscribers.setdefault(task_id, set()).add(queue)
        return EventSubscription(task_id=task_id, queue=queue, _broker=self)

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[EventRecord]) -> None:
        queues = self._subscribers.get(task_id)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(task_id, None)

    async def publish(self, event: EventRecord) -> None:
        if self._audit_sink is not None:
            await self._audit_sink.record(
                event.event_type,
                {
                    "event_id": event.event_id,
                    "visibility": str(event.visibility),
                    "created_at": _to_isoformat(event.created_at),
                    **dict(event.payload),
                },
                conversation_id=event.conversation_id,
                task_id=event.task_id,
                node_id=event.node_id,
            )

        for queue in tuple(self._subscribers.get(event.task_id, ())):
            await queue.put(event)

    async def publish_transient(self, event: EventRecord) -> None:
        """Fan out an in-memory-only event without storage or full audit writes.

        Transient stream deltas can contain user-visible answer/reasoning text.
        They are allowed to exist only in the active process/SSE response and
        therefore intentionally bypass the audit sink used by persistent events.
        """

        for queue in tuple(self._subscribers.get(event.task_id, ())):
            await queue.put(event)


def is_frontend_event(event: EventRecord) -> bool:
    return event.visibility == EventVisibility.FRONTEND


def encode_sse_event(event: EventRecord) -> dict[str, str]:
    return {
        "id": event.event_id,
        "event": event.event_type,
        "data": json.dumps(
            {
                "event_id": event.event_id,
                "conversation_id": event.conversation_id,
                "task_id": event.task_id,
                "node_id": event.node_id,
                "event_type": event.event_type,
                "payload": dict(event.payload),
                "created_at": _to_isoformat(event.created_at),
            },
            ensure_ascii=False,
        ),
    }


def _to_isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
