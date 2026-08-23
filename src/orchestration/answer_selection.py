from __future__ import annotations

from collections.abc import Iterable, Sequence

from src.core.enums import ArtifactType
from src.core.models import Artifact, EventRecord

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
        if artifact.artifact_id.startswith("agent-artifact:") and artifact.artifact_id.endswith(":final"):
            return artifact
    final_node_ids = _final_node_ids_from_events(events)
    if final_node_ids:
        for artifact in text_artifacts:
            if artifact.producer_node_id in final_node_ids:
                return artifact
    return text_artifacts[0]

def _final_node_ids_from_events(events: Iterable[EventRecord]) -> set[str]:
    return {
        event.node_id
        for event in events
        if event.node_id
        and event.event_type == "agent.final_output"
    }
