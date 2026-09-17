from __future__ import annotations

import json
from typing import Any

from src.core.enums import ArtifactType
from src.core.models import Artifact
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    is_active_managed_output_file,
    parse_file_storage_ref,
)
from src.integrations.mcp.result_parsing.projection_store import (
    MCPProjectionBinding,
    MCPProjectionStore,
)

from .dto import ArtifactResponse, MCPBusinessResultView


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


async def artifact_response(
    artifact: Artifact,
    *,
    artifact_file_store: LocalArtifactFileStore,
    projection_store: MCPProjectionStore | None = None,
) -> ArtifactResponse:
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
    if metadata.get("source_kind") == "mcp_result":
        recorded_reason = metadata.get("mcp_projection_unavailable_reason")
        business_result = _unavailable_mcp_result(
            recorded_reason
            if recorded_reason in {
                "projection_missing",
                "historical_authority_invalid",
                "projection_invalid",
            }
            else "safe_hide"
        )
        projection_ref = _optional_string(metadata.get("projection_ref"))
        projection_sha256 = _optional_string(metadata.get("projection_sha256"))
        if projection_ref is not None and projection_sha256 is not None:
            if projection_store is None:
                business_result = _unavailable_mcp_result(
                    "projection_missing"
                )
            else:
                try:
                    envelope = projection_store.load(
                        projection_ref,
                        binding=MCPProjectionBinding(
                            owner_user_id=str(metadata["owner_user_id"]),
                            task_id=artifact.task_id,
                            node_id=artifact.producer_node_id,
                            call_ref=str(metadata["call_ref"]),
                            raw_sha256=(
                                "sha256:" + str(metadata["sha256"])
                            ),
                            output_schema_sha256=(
                                _optional_string(
                                    metadata.get("output_schema_sha256")
                                )
                            ),
                            source=str(metadata["terminal_result_source"]),
                            parser_revision=str(metadata["parser_revision"]),
                        ),
                        expected_projection_sha256=projection_sha256,
                    )
                    business_result = MCPBusinessResultView.model_validate(
                        envelope["user_view"]
                    )
                except Exception:
                    business_result = _unavailable_mcp_result(
                        "projection_invalid"
                    )
        return ArtifactResponse(
            artifact_id=artifact.artifact_id,
            producer_node_id=artifact.producer_node_id,
            artifact_type="mcp_result",
            storage_ref="",
            summary=str(metadata.get("summary") or artifact.summary or ""),
            is_complete=artifact.is_complete,
            created_at=artifact.created_at,
            mcp_business_result=business_result,
        )
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
    return is_active_managed_output_file(
        parse_file_storage_ref(artifact.storage_ref)
    )


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


def _unavailable_mcp_result(reason: str) -> MCPBusinessResultView:
    return MCPBusinessResultView(
        schema="maf.mcp.business_result_view.v1",
        availability="unavailable",
        outcome="succeeded",
        unavailable_reason=reason,
        projection_truncated=False,
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
