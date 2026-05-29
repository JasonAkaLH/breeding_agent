from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import ArtifactType, EventVisibility
from src.core.models import Artifact, EventRecord
from src.integrations.llm_stream_events import accepted_options, coerce_stream_event, iter_stream_like


StreamGenerator = Callable[..., AsyncIterator[str] | Awaitable[str] | Iterable[str] | str]
LiveEventRecorder = Callable[[EventRecord], Awaitable[None]]
TransientEventPublisher = Callable[[EventRecord], Awaitable[None]]


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


def make_text_artifact(*, task_id: str, node_id: str, text: str, response_role: str | None = None) -> Artifact:
    digest = hashlib.sha256(f"{node_id}:main_agent_response:{text}".encode("utf-8")).hexdigest()[:12]
    role_part = f":{response_role}" if response_role else ""
    return Artifact(
        artifact_id=f"{node_id}:main_agent_response{role_part}:{digest}",
        task_id=task_id,
        producer_node_id=node_id,
        artifact_type=ArtifactType.TEXT,
        storage_ref=text,
        summary=text[:200],
        is_complete=True,
    )


async def iter_stream(
    generator: StreamGenerator,
    prompt: Any,
    *,
    reasoning_effort: str | None = None,
    thinking: bool | None = None,
    stage: str | None = None,
) -> AsyncIterator[str]:
    async for event in iter_stream_events(
        generator,
        prompt,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        stage=stage,
    ):
        answer = event.get("answer")
        if answer:
            yield answer


async def iter_stream_events(
    generator: StreamGenerator,
    prompt: Any,
    *,
    reasoning_effort: str | None = None,
    thinking: bool | None = None,
    model_edition: str | None = None,
    stage: str | None = None,
    prompt_profile: Mapping[str, Any] | None = None,
) -> AsyncIterator[dict[str, str | None]]:
    stream_options = accepted_options(
        generator,
        {
            "reasoning_effort": reasoning_effort,
            "thinking": thinking,
            "model_edition": model_edition,
            "stage": stage,
            "prompt_profile": prompt_profile,
        },
    )
    produced = generator(prompt, **stream_options) if stream_options else generator(prompt)
    async for chunk in iter_stream_like(produced):
        event = coerce_stream_event(chunk)
        if event:
            yield event
