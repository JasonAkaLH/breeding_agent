from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import ArtifactType, EventVisibility
from src.core.models import Artifact, EventRecord


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
    prompt: str,
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
    prompt: str,
    *,
    reasoning_effort: str | None = None,
    thinking: bool | None = None,
    model_edition: str | None = None,
    stage: str | None = None,
) -> AsyncIterator[dict[str, str | None]]:
    stream_options = _accepted_stream_options(
        generator,
        {
            "reasoning_effort": reasoning_effort,
            "thinking": thinking,
            "model_edition": model_edition,
            "stage": stage,
        },
    )
    produced = generator(prompt, **stream_options) if stream_options else generator(prompt)
    if hasattr(produced, "__aiter__"):
        async for chunk in produced:  # type: ignore[union-attr]
            event = _coerce_stream_event(chunk)
            if event:
                yield event
        return
    if hasattr(produced, "__await__"):
        value = await produced  # type: ignore[misc]
        event = _coerce_stream_event(value)
        if event:
            yield event
        return
    if isinstance(produced, str | Mapping):
        event = _coerce_stream_event(produced)
        if event:
            yield event
        return
    for chunk in produced:  # type: ignore[union-attr]
        event = _coerce_stream_event(chunk)
        if event:
            yield event


def _accepted_stream_options(generator: StreamGenerator, options: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(generator)
    except (TypeError, ValueError):
        return {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    return {
        key: value
        for key, value in options.items()
        if value is not None and (accepts_kwargs or key in signature.parameters)
    }


def _coerce_stream_event(value: Any) -> dict[str, str | None] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        answer = _optional_string(value.get("answer") if "answer" in value else value.get("delta"))
        reasoning = _optional_string(value.get("reasoning") if "reasoning" in value else value.get("reasoning_content"))
        if answer is None and reasoning is None:
            return None
        return {"answer": answer, "reasoning": reasoning}
    text = str(value)
    if not text:
        return None
    return {"answer": text, "reasoning": None}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
