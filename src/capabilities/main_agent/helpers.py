from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, Mapping

from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import ArtifactType, EventVisibility
from src.core.models import Artifact, EventRecord


StreamGenerator = Callable[[str], AsyncIterator[str] | Awaitable[str] | Iterable[str] | str]
LiveEventRecorder = Callable[[EventRecord], Awaitable[None]]


def make_event(
    request: CapabilityExecutionRequest,
    *,
    event_type: str,
    payload: Mapping[str, Any],
    visibility: EventVisibility,
    ordinal: int | None = None,
) -> EventRecord:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{request.node_id}:{event_type}:{ordinal}:{serialized}".encode("utf-8")).hexdigest()[:12]
    return EventRecord(
        event_id=f"{request.node_id}:{event_type}:{ordinal or 0}:{digest}",
        conversation_id=request.conversation_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=event_type,
        payload=dict(payload),
        visibility=visibility,
    )


def make_text_artifact(*, task_id: str, node_id: str, text: str) -> Artifact:
    digest = hashlib.sha256(f"{node_id}:main_agent_response:{text}".encode("utf-8")).hexdigest()[:12]
    return Artifact(
        artifact_id=f"{node_id}:main_agent_response:{digest}",
        task_id=task_id,
        producer_node_id=node_id,
        artifact_type=ArtifactType.TEXT,
        storage_ref=text,
        summary=text[:200],
        is_complete=True,
    )


async def iter_stream(generator: StreamGenerator, prompt: str) -> AsyncIterator[str]:
    produced = generator(prompt)
    if hasattr(produced, "__aiter__"):
        async for chunk in produced:  # type: ignore[union-attr]
            if chunk:
                yield str(chunk)
        return
    if hasattr(produced, "__await__"):
        value = await produced  # type: ignore[misc]
        if value:
            yield str(value)
        return
    if isinstance(produced, str):
        if produced:
            yield produced
        return
    for chunk in produced:  # type: ignore[union-attr]
        if chunk:
            yield str(chunk)
