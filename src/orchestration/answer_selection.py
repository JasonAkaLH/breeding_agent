from __future__ import annotations

from collections.abc import Iterable, Sequence

from src.core.enums import ArtifactType
from src.core.models import Artifact, EventRecord

from .answer_roles import RESPONSE_ROLE_FINAL, RESPONSE_ROLE_INTERMEDIATE, response_role_from_metadata


def select_final_text_artifact(
    artifacts: Sequence[Artifact],
    *,
    events: Iterable[EventRecord] = (),
) -> Artifact | None:
    """Select the task-level final answer without changing Artifact storage schema."""

    text_artifacts = [
        artifact
        for artifact in artifacts
        if str(artifact.artifact_type) == str(ArtifactType.TEXT) and artifact.storage_ref.strip()
    ]
    if not text_artifacts:
        return None

    for artifact in text_artifacts:
        if _response_role_from_artifact_id(artifact.artifact_id) == RESPONSE_ROLE_FINAL:
            return artifact
    final_node_ids = _final_node_ids_from_events(events)
    if final_node_ids:
        for artifact in text_artifacts:
            if artifact.producer_node_id in final_node_ids:
                return artifact
    return text_artifacts[0]


def _response_role_from_artifact_id(artifact_id: str) -> str | None:
    marker = ":main_agent_response:"
    if marker not in artifact_id:
        return None
    suffix = artifact_id.rsplit(marker, 1)[-1]
    first_part = suffix.split(":", 1)[0]
    if first_part in {RESPONSE_ROLE_FINAL, RESPONSE_ROLE_INTERMEDIATE}:
        return first_part
    return None


def _final_node_ids_from_events(events: Iterable[EventRecord]) -> set[str]:
    return {
        event.node_id
        for event in events
        if event.node_id
        and event.event_type in {"main_agent.output_final", "main_agent.llm_call"}
        and response_role_from_metadata(event.payload) == RESPONSE_ROLE_FINAL
    }
