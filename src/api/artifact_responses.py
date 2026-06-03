from __future__ import annotations

import json
from typing import Any

from src.core.enums import ArtifactType
from src.core.models import Artifact
from src.storage.artifact_files import is_active_skill_output_file, parse_file_storage_ref

from .dto import ArtifactResponse


_HISTORY_DATA_QUERY_ROLES = frozenset({"filtered_query_result", "query_result_preview"})
_HISTORY_DATA_QUERY_ROLE_MARKERS = tuple(sorted(_HISTORY_DATA_QUERY_ROLES))
_HISTORY_OCR_ROLE = "ocr_raw_text"


def should_return_task_artifact(artifact: Artifact) -> bool:
    if artifact.artifact_type != ArtifactType.FILE:
        return True
    return _is_active_file_artifact(artifact)


def should_return_history_display_artifact(artifact: Artifact) -> bool:
    if artifact.artifact_type == ArtifactType.FILE:
        return _is_active_file_artifact(artifact)
    if artifact.artifact_type != ArtifactType.JSON:
        return False

    payload = _json_object(artifact.storage_ref)
    if payload is None:
        return False

    role = _string_field(payload, "artifact_role")
    if role in _HISTORY_DATA_QUERY_ROLES:
        return True
    if any(marker in artifact.artifact_id for marker in _HISTORY_DATA_QUERY_ROLE_MARKERS):
        return True
    return role == _HISTORY_OCR_ROLE and _has_ocr_text(payload)


def artifact_response(artifact: Artifact) -> ArtifactResponse:
    if artifact.artifact_type != ArtifactType.FILE:
        return ArtifactResponse(
            artifact_id=artifact.artifact_id,
            producer_node_id=artifact.producer_node_id,
            artifact_type=str(artifact.artifact_type),
            storage_ref=artifact.storage_ref,
            summary=artifact.summary,
            is_complete=artifact.is_complete,
            created_at=artifact.created_at,
        )

    metadata = parse_file_storage_ref(artifact.storage_ref) or {}
    return ArtifactResponse(
        artifact_id=artifact.artifact_id,
        producer_node_id=artifact.producer_node_id,
        artifact_type=str(artifact.artifact_type),
        storage_ref="",
        summary=str(metadata.get("summary") or artifact.summary or ""),
        is_complete=artifact.is_complete,
        created_at=artifact.created_at,
        filename=_optional_string(metadata.get("filename")),
        mime_type=_optional_string(metadata.get("mime_type")),
        size_bytes=_optional_int(metadata.get("size_bytes")),
        sha256=_optional_string(metadata.get("sha256")),
        download_url=f"/api/v1/artifacts/{artifact.artifact_id}/download",
        source_file_count=_optional_int(metadata.get("source_file_count")),
        archive_format=_optional_string(metadata.get("archive_format")),
        retention_status=_optional_string(metadata.get("retention_status")),
    )


def _is_active_file_artifact(artifact: Artifact) -> bool:
    return is_active_skill_output_file(parse_file_storage_ref(artifact.storage_ref))


def _json_object(storage_ref: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(storage_ref)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_ocr_text(payload: dict[str, Any]) -> bool:
    return any(_string_field(payload, field) is not None for field in ("raw_text", "text", "markdown"))


def _string_field(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
