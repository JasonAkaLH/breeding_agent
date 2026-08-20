from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.core.enums import EventVisibility
from src.core.models import Artifact, EventRecord, MCPDurableResultLifecycle
from src.integrations.mcp.cp7_artifacts import mcp_durable_result_artifact_id
from src.integrations.mcp.result_parsing.projection_store import (
    MCPProjectionStagingHandle,
    MCPPublishedProjection,
    PROJECTION_SCHEMA,
)
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    build_file_storage_ref,
    parse_file_storage_ref,
    sanitize_download_filename,
)


MCP_RESULT_ARTIFACT_PROJECTION_EVENT = "mcp.result_artifact_projection"
MCP_RESULT_ARTIFACT_PROJECTION_SCHEMA = (
    "maf.user_mcp.result_artifact_projection.v1"
)
MCP_RESULT_ARTIFACT_PROJECTION_MAX_CALLS = 20
MCP_RESULT_ARTIFACT_PROJECTION_MAX_EVENTS = 120
_SAFE_CALL_REF_PATTERN = re.compile(r"[0-9a-f]{64}")
_SAFE_TOOL_COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class MCPResultArtifactProjectionStatus(StrEnum):
    READY = "ready"
    DEFERRED = "deferred"
    PERMANENT_FAILURE = "permanent_failure"


class MCPResultArtifactProjectionReason(StrEnum):
    PROMOTED = "promoted"
    ALREADY_PROMOTED = "already_promoted"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    PROJECTION_FAILED = "projection_failed"
    SOURCE_EXPIRED = "source_expired"


_VALID_REASON_BY_STATUS = {
    MCPResultArtifactProjectionStatus.READY: frozenset(
        {
            MCPResultArtifactProjectionReason.PROMOTED,
            MCPResultArtifactProjectionReason.ALREADY_PROMOTED,
        }
    ),
    MCPResultArtifactProjectionStatus.DEFERRED: frozenset(
        {
            MCPResultArtifactProjectionReason.CAPACITY_UNAVAILABLE,
            MCPResultArtifactProjectionReason.PROJECTION_FAILED,
        }
    ),
    MCPResultArtifactProjectionStatus.PERMANENT_FAILURE: frozenset(
        {
            MCPResultArtifactProjectionReason.PROJECTION_FAILED,
            MCPResultArtifactProjectionReason.SOURCE_EXPIRED,
        }
    ),
}
_REASON_PRIORITY = {
    MCPResultArtifactProjectionStatus.READY: {
        MCPResultArtifactProjectionReason.ALREADY_PROMOTED: 0,
        MCPResultArtifactProjectionReason.PROMOTED: 1,
    },
    MCPResultArtifactProjectionStatus.DEFERRED: {
        MCPResultArtifactProjectionReason.CAPACITY_UNAVAILABLE: 0,
        MCPResultArtifactProjectionReason.PROJECTION_FAILED: 1,
    },
    MCPResultArtifactProjectionStatus.PERMANENT_FAILURE: {
        MCPResultArtifactProjectionReason.PROJECTION_FAILED: 0,
        MCPResultArtifactProjectionReason.SOURCE_EXPIRED: 1,
    },
}


@dataclass(frozen=True, slots=True)
class MCPResultArtifactProjection:
    safe_call_ref: str
    status: MCPResultArtifactProjectionStatus
    reason_code: MCPResultArtifactProjectionReason
    artifact_count: int

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": MCP_RESULT_ARTIFACT_PROJECTION_SCHEMA,
            "safe_call_ref": self.safe_call_ref,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "artifact_count": self.artifact_count,
        }


@dataclass(frozen=True, slots=True)
class MCPResultArtifactProjectionResult:
    status: MCPResultArtifactProjectionStatus
    reason_code: MCPResultArtifactProjectionReason
    artifact: Artifact | None = None
    safe_call_ref: str | None = None

    @property
    def should_delete_expired_source(self) -> bool:
        return self.status is MCPResultArtifactProjectionStatus.PERMANENT_FAILURE


@dataclass(frozen=True, slots=True)
class MCPResultArtifactProjectionObservation:
    status: MCPResultArtifactProjectionStatus
    reason_code: MCPResultArtifactProjectionReason
    source: str
    elapsed_ms: int


def parse_mcp_result_artifact_projection_payload(
    value: Mapping[str, Any],
) -> MCPResultArtifactProjection:
    if set(value) != {
        "schema",
        "safe_call_ref",
        "status",
        "reason_code",
        "artifact_count",
    }:
        raise ValueError("mcp_result_artifact_projection_payload_shape_invalid")
    if value.get("schema") != MCP_RESULT_ARTIFACT_PROJECTION_SCHEMA:
        raise ValueError("mcp_result_artifact_projection_schema_invalid")
    safe_call_ref = value.get("safe_call_ref")
    if not isinstance(safe_call_ref, str) or _SAFE_CALL_REF_PATTERN.fullmatch(
        safe_call_ref
    ) is None:
        raise ValueError("mcp_result_artifact_projection_safe_call_ref_invalid")
    try:
        status = MCPResultArtifactProjectionStatus(str(value.get("status")))
        reason = MCPResultArtifactProjectionReason(str(value.get("reason_code")))
    except ValueError as exc:
        raise ValueError("mcp_result_artifact_projection_state_invalid") from exc
    artifact_count = value.get("artifact_count")
    if isinstance(artifact_count, bool) or artifact_count not in {0, 1}:
        raise ValueError("mcp_result_artifact_projection_count_invalid")
    if reason not in _VALID_REASON_BY_STATUS[status]:
        raise ValueError("mcp_result_artifact_projection_reason_invalid")
    if (status is MCPResultArtifactProjectionStatus.READY) != (
        artifact_count == 1
    ):
        raise ValueError("mcp_result_artifact_projection_count_invalid")
    return MCPResultArtifactProjection(
        safe_call_ref=safe_call_ref,
        status=status,
        reason_code=reason,
        artifact_count=int(artifact_count),
    )


def fold_mcp_result_artifact_projection_payloads(
    payloads: Iterable[Mapping[str, Any]],
) -> tuple[MCPResultArtifactProjection, ...]:
    grouped: dict[
        str, dict[MCPResultArtifactProjectionStatus, MCPResultArtifactProjection]
    ] = {}
    event_count = 0
    for payload in payloads:
        event_count += 1
        if event_count > MCP_RESULT_ARTIFACT_PROJECTION_MAX_EVENTS:
            raise ValueError("mcp_result_artifact_projection_event_limit_exceeded")
        item = parse_mcp_result_artifact_projection_payload(payload)
        per_call = grouped.setdefault(item.safe_call_ref, {})
        if len(grouped) > MCP_RESULT_ARTIFACT_PROJECTION_MAX_CALLS:
            raise ValueError("mcp_result_artifact_projection_call_limit_exceeded")
        current = per_call.get(item.status)
        if current is None or _REASON_PRIORITY[item.status][
            item.reason_code
        ] > _REASON_PRIORITY[item.status][current.reason_code]:
            per_call[item.status] = item
    folded: list[MCPResultArtifactProjection] = []
    for safe_call_ref in sorted(grouped):
        per_call = grouped[safe_call_ref]
        if (
            MCPResultArtifactProjectionStatus.READY in per_call
            and MCPResultArtifactProjectionStatus.PERMANENT_FAILURE in per_call
        ):
            raise ValueError("mcp_result_artifact_projection_terminal_fork")
        selected = (
            per_call.get(MCPResultArtifactProjectionStatus.READY)
            or per_call.get(MCPResultArtifactProjectionStatus.PERMANENT_FAILURE)
            or per_call.get(MCPResultArtifactProjectionStatus.DEFERRED)
        )
        if selected is not None:
            folded.append(selected)
    return tuple(folded)


def mcp_result_artifact_projection_event_id(
    artifact_id: str,
    status: MCPResultArtifactProjectionStatus,
    reason_code: MCPResultArtifactProjectionReason,
) -> str:
    return (
        "mcp-result-artifact-projection:v1:"
        f"{artifact_id}:{status.value}:{reason_code.value}"
    )


class MCPResultArtifactProjector:
    def __init__(
        self,
        *,
        storage: Any,
        lifecycle_manager: Any,
        artifact_file_store: LocalArtifactFileStore,
        audit_reference_signer: Any,
        artifact_disk_low_watermark_bytes: int,
        event_sink: Callable[[EventRecord], Awaitable[None] | None] | None = None,
        observer: Callable[
            [MCPResultArtifactProjectionObservation], Awaitable[None] | None
        ]
        | None = None,
        free_bytes: Callable[[Path], int] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(artifact_disk_low_watermark_bytes, bool)
            or not isinstance(artifact_disk_low_watermark_bytes, int)
            or artifact_disk_low_watermark_bytes <= 0
        ):
            raise ValueError("artifact_disk_low_watermark_bytes_invalid")
        self._storage = storage
        self._lifecycle_manager = lifecycle_manager
        self._artifact_file_store = artifact_file_store
        self._audit_reference_signer = audit_reference_signer
        self._artifact_disk_low_watermark_bytes = (
            artifact_disk_low_watermark_bytes
        )
        self._event_sink = event_sink
        self._observer = observer
        self._free_bytes = free_bytes or _disk_free_bytes
        self._now = now_fn or (
            lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        )

    async def project_completed_result(
        self, result_ref: str, *, source: str
    ) -> MCPResultArtifactProjectionResult:
        if source not in {"immediate", "reconciler"}:
            raise ValueError("mcp_result_artifact_projection_source_invalid")
        started = time.monotonic()
        lifecycle = await self._storage.get_mcp_durable_result_lifecycle(
            str(result_ref)
        )
        if lifecycle is None:
            result = MCPResultArtifactProjectionResult(
                MCPResultArtifactProjectionStatus.DEFERRED,
                MCPResultArtifactProjectionReason.PROJECTION_FAILED,
            )
            await self._observe(result, source, started)
            return result
        call = await self._storage.get_mcp_call_record(
            lifecycle.owner_user_id,
            lifecycle.task_id,
            lifecycle.call_id,
        )
        receipt = await self._storage.get_mcp_terminal_result_receipt_for_call(
            lifecycle.call_id
        )
        if not _matches_completed_authority(lifecycle, call, receipt):
            result = self._failure_result(
                lifecycle,
                reason=MCPResultArtifactProjectionReason.PROJECTION_FAILED,
            )
            await self._observe(result, source, started)
            return result
        safe_call_ref = self._audit_reference_signer.safe_reference(
            lifecycle.call_id,
            context="mcp-call-reference-v1",
        )
        artifact_id = mcp_durable_result_artifact_id(lifecycle.result_ref)
        existing = await self._storage.get_artifact(artifact_id)
        if (
            str(lifecycle.status) == "deleted"
            and existing is None
        ):
            result = MCPResultArtifactProjectionResult(
                MCPResultArtifactProjectionStatus.PERMANENT_FAILURE,
                MCPResultArtifactProjectionReason.SOURCE_EXPIRED,
                safe_call_ref=safe_call_ref,
            )
            await self._emit(lifecycle, receipt, result, artifact_id)
            await self._observe(result, source, started)
            return result
        if existing is None and self._free_bytes(
            self._artifact_file_store.root_dir
        ) < lifecycle.size_bytes + self._artifact_disk_low_watermark_bytes:
            failure_reason = (
                MCPResultArtifactProjectionReason.CAPACITY_UNAVAILABLE
                if not _is_expired(lifecycle, self._now())
                else MCPResultArtifactProjectionReason.PROJECTION_FAILED
            )
            result = self._failure_result(
                lifecycle,
                reason=failure_reason,
                safe_call_ref=safe_call_ref,
            )
            await self._emit(lifecycle, receipt, result, artifact_id)
            await self._observe(result, source, started)
            return result
        try:
            artifact = await self._lifecycle_manager.promote_to_artifact(
                result_ref=lifecycle.result_ref,
                filename=_result_filename(call.call_sequence, call.tool_name),
                summary=_result_summary(call.tool_name),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            result = self._failure_result(
                lifecycle,
                reason=MCPResultArtifactProjectionReason.PROJECTION_FAILED,
                safe_call_ref=safe_call_ref,
            )
            await self._emit(lifecycle, receipt, result, artifact_id)
            await self._observe(result, source, started)
            return result
        result = MCPResultArtifactProjectionResult(
            MCPResultArtifactProjectionStatus.READY,
            (
                MCPResultArtifactProjectionReason.ALREADY_PROMOTED
                if existing is not None
                else MCPResultArtifactProjectionReason.PROMOTED
            ),
            artifact=artifact,
            safe_call_ref=safe_call_ref,
        )
        await self._emit(lifecycle, receipt, result, artifact_id)
        await self._observe(result, source, started)
        return result

    async def attach_published_projection(
        self,
        artifact: Artifact,
        *,
        published: MCPPublishedProjection,
        staging_handle: MCPProjectionStagingHandle,
    ) -> Artifact:
        metadata = parse_file_storage_ref(artifact.storage_ref) or {}
        binding = staging_handle.binding
        if (
            metadata.get("source_kind") != "mcp_result"
            or artifact.task_id != binding.task_id
            or artifact.producer_node_id != binding.node_id
            or str(metadata.get("result_ref") or "") == ""
            or str(metadata.get("sha256") or "")
            != binding.raw_sha256.removeprefix("sha256:")
        ):
            raise ValueError("mcp_result_projection_artifact_authority_invalid")
        replacement_metadata = {
            **metadata,
            "visibility": "internal_raw",
            "protocol_version": (
                await self._storage.get_mcp_call_record(
                    binding.owner_user_id, binding.task_id, binding.call_ref
                )
            ).protocol_version,
            "terminal_result_source": binding.source,
            "output_schema_sha256": binding.output_schema_sha256,
            "parser_revision": binding.parser_revision,
            "projection_schema": PROJECTION_SCHEMA,
            "projection_ref": published.projection_ref,
            "projection_sha256": published.projection_sha256,
            "owner_user_id": binding.owner_user_id,
            "call_ref": binding.call_ref,
        }
        replacement_ref = build_file_storage_ref(replacement_metadata)
        if len(replacement_ref.encode("utf-8")) > 16 * 1024:
            raise ValueError("mcp_result_projection_artifact_metadata_too_large")
        updated = await self._storage.compare_and_set_artifact_storage_ref(
            artifact.artifact_id,
            artifact.storage_ref,
            replacement_ref,
        )
        current = await self._storage.get_artifact(artifact.artifact_id)
        if current is None or (
            not updated and current.storage_ref != replacement_ref
        ):
            raise ValueError("mcp_result_projection_artifact_cas_conflict")
        return current

    def _failure_result(
        self,
        lifecycle: MCPDurableResultLifecycle,
        *,
        reason: MCPResultArtifactProjectionReason,
        safe_call_ref: str | None = None,
    ) -> MCPResultArtifactProjectionResult:
        status = (
            MCPResultArtifactProjectionStatus.PERMANENT_FAILURE
            if _is_expired(lifecycle, self._now())
            else MCPResultArtifactProjectionStatus.DEFERRED
        )
        if (
            status is MCPResultArtifactProjectionStatus.PERMANENT_FAILURE
            and reason is MCPResultArtifactProjectionReason.CAPACITY_UNAVAILABLE
        ):
            reason = MCPResultArtifactProjectionReason.PROJECTION_FAILED
        return MCPResultArtifactProjectionResult(
            status,
            reason,
            safe_call_ref=safe_call_ref,
        )

    async def _emit(
        self,
        lifecycle: MCPDurableResultLifecycle,
        receipt: Any,
        result: MCPResultArtifactProjectionResult,
        artifact_id: str,
    ) -> None:
        if self._event_sink is None or result.safe_call_ref is None:
            return
        projection = MCPResultArtifactProjection(
            safe_call_ref=result.safe_call_ref,
            status=result.status,
            reason_code=result.reason_code,
            artifact_count=(
                1
                if result.status is MCPResultArtifactProjectionStatus.READY
                else 0
            ),
        )
        event = EventRecord(
            event_id=mcp_result_artifact_projection_event_id(
                artifact_id, result.status, result.reason_code
            ),
            conversation_id=receipt.conversation_id,
            task_id=lifecycle.task_id,
            node_id=lifecycle.node_id,
            event_type=MCP_RESULT_ARTIFACT_PROJECTION_EVENT,
            payload=projection.as_payload(),
            visibility=EventVisibility.FRONTEND,
            created_at=_stable_event_time(lifecycle, receipt, result),
        )
        try:
            await _await_maybe(self._event_sink(event))
        except Exception:
            return

    async def _observe(
        self,
        result: MCPResultArtifactProjectionResult,
        source: str,
        started: float,
    ) -> None:
        if self._observer is None:
            return
        observation = MCPResultArtifactProjectionObservation(
            status=result.status,
            reason_code=result.reason_code,
            source=source,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        try:
            await _await_maybe(self._observer(observation))
        except Exception:
            return


def _matches_completed_authority(lifecycle, call, receipt) -> bool:
    return bool(
        call is not None
        and receipt is not None
        and str(call.status) == "completed"
        and call.owner_user_id == lifecycle.owner_user_id
        and call.task_id == lifecycle.task_id
        and call.node_id == lifecycle.node_id
        and call.call_ref == lifecycle.call_id
        and call.result_ref == lifecycle.result_ref
        and receipt.terminal_state is not None
        and str(receipt.terminal_state) == "completed"
        and receipt.owner_user_id == lifecycle.owner_user_id
        and receipt.task_id == lifecycle.task_id
        and receipt.node_id == lifecycle.node_id
        and receipt.call_id == lifecycle.call_id
        and receipt.safe_result_ref == lifecycle.result_ref
        and receipt.safe_result_size_bytes == lifecycle.size_bytes
        and receipt.safe_result_content_sha256 == lifecycle.content_sha256
        and receipt.safe_result_store_kind == lifecycle.store_kind
    )


def _result_filename(sequence: int, tool_name: str) -> str:
    component = _SAFE_TOOL_COMPONENT_PATTERN.sub("_", str(tool_name)).strip(
        "._"
    )
    component = (component or "tool")[:140]
    return sanitize_download_filename(f"{int(sequence):02d}-{component}-result.json")


def _result_summary(tool_name: str) -> str:
    return f"MCP Tool原始返回：{str(tool_name).strip()}"[:200]


def _is_expired(lifecycle: MCPDurableResultLifecycle, now: datetime) -> bool:
    return lifecycle.eligible_at is not None and lifecycle.eligible_at <= now


def _stable_event_time(lifecycle, receipt, result) -> datetime:
    if result.status is MCPResultArtifactProjectionStatus.READY:
        if result.artifact is not None and result.artifact.created_at is not None:
            return result.artifact.created_at
        return receipt.committed_at
    if result.status is MCPResultArtifactProjectionStatus.DEFERRED:
        return receipt.committed_at or lifecycle.created_at
    if (
        result.reason_code is MCPResultArtifactProjectionReason.SOURCE_EXPIRED
        and lifecycle.deleted_at is not None
    ):
        return lifecycle.deleted_at
    return lifecycle.eligible_at or lifecycle.updated_at


async def _await_maybe(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


def _disk_free_bytes(path: Path) -> int:
    values = os.statvfs(path)
    return int(values.f_bavail * values.f_frsize)


__all__ = [
    "MCP_RESULT_ARTIFACT_PROJECTION_EVENT",
    "MCP_RESULT_ARTIFACT_PROJECTION_MAX_CALLS",
    "MCP_RESULT_ARTIFACT_PROJECTION_MAX_EVENTS",
    "MCP_RESULT_ARTIFACT_PROJECTION_SCHEMA",
    "MCPResultArtifactProjection",
    "MCPResultArtifactProjectionObservation",
    "MCPResultArtifactProjectionReason",
    "MCPResultArtifactProjectionResult",
    "MCPResultArtifactProjectionStatus",
    "MCPResultArtifactProjector",
    "fold_mcp_result_artifact_projection_payloads",
    "mcp_result_artifact_projection_event_id",
    "parse_mcp_result_artifact_projection_payload",
]
