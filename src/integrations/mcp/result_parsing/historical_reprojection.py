from __future__ import annotations

import asyncio
import os
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Protocol

from src.core.contracts import (
    ArtifactStoragePort,
    MCPDispatchFinalizationStoragePort,
    MCPDispatchStoragePort,
    MCPResultLifecycleStoragePort,
)
from src.core.models import Artifact, MCPCallRecord, MCPTerminalResultReceipt
from src.integrations.mcp.cp7_artifacts import mcp_durable_result_artifact_id
from src.integrations.mcp.temporary_results import MCPTemporaryResultError
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    build_file_storage_ref,
    parse_file_storage_ref,
)

from .models import MCPRawResultDescriptor, MCPResultDecodeRequest, MCPResultSource
from .projection_store import MCPProjectionBinding, MCPProjectionStore
from .service import MCPIsolatedResultService
from .worker import PARSER_REVISION


MCPHistoricalUnavailableReason = Literal[
    "projection_missing", "historical_authority_invalid", "projection_invalid"
]


class MCPHistoricalReprojectionStoragePort(
    ArtifactStoragePort,
    MCPDispatchStoragePort,
    MCPDispatchFinalizationStoragePort,
    MCPResultLifecycleStoragePort,
    Protocol,
):
    pass


class MCPRawResultAuthorityError(RuntimeError):
    def __init__(self, reason: MCPHistoricalUnavailableReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class MCPHistoricalReprojectionSummary:
    scanned: int = 0
    ready: int = 0
    already_ready: int = 0
    projection_missing: int = 0
    historical_authority_invalid: int = 0
    projection_invalid: int = 0
    revision_retired: int = 0


class MCPRawResultAuthorityResolver:
    """Resolve historical raw bytes without any MCP or other network access."""

    def __init__(
        self,
        *,
        storage: MCPHistoricalReprojectionStoragePort,
        snapshot_authority: Any,
        artifact_file_store: LocalArtifactFileStore,
    ) -> None:
        self._storage = storage
        self._snapshot_authority = snapshot_authority
        self._artifact_file_store = artifact_file_store

    @asynccontextmanager
    async def resolve(
        self,
        *,
        call: MCPCallRecord,
        receipt: MCPTerminalResultReceipt,
    ) -> AsyncIterator[MCPRawResultDescriptor]:
        _validate_raw_authority(call, receipt)
        lifecycle = await self._storage.get_mcp_durable_result_lifecycle(
            str(receipt.safe_result_ref)
        )
        if lifecycle is not None and str(lifecycle.status) in {
            "retained",
            "artifact_owned",
        }:
            if not _matches_lifecycle(call, receipt, lifecycle):
                raise MCPRawResultAuthorityError("historical_authority_invalid")
            try:
                async with self._snapshot_authority.open_result_parser_descriptor(
                    result_ref=str(receipt.safe_result_ref),
                    owner_user_id=call.owner_user_id,
                    task_id=call.task_id,
                    node_id=call.node_id,
                    call_id=call.call_ref,
                    expected_size_bytes=int(receipt.safe_result_size_bytes),
                    expected_content_sha256=str(
                        receipt.safe_result_content_sha256
                    ),
                    expected_store_kind=str(receipt.safe_result_store_kind),
                ) as descriptor:
                    yield descriptor
                    return
            except MCPTemporaryResultError as exc:
                raise MCPRawResultAuthorityError(
                    "historical_authority_invalid"
                ) from exc

        artifact = await self._storage.get_artifact(
            mcp_durable_result_artifact_id(str(receipt.safe_result_ref))
        )
        descriptor = self._managed_copy_descriptor(call, receipt, artifact)
        yield descriptor

    def _managed_copy_descriptor(
        self,
        call: MCPCallRecord,
        receipt: MCPTerminalResultReceipt,
        artifact: Artifact | None,
    ) -> MCPRawResultDescriptor:
        if artifact is None:
            raise MCPRawResultAuthorityError("projection_missing")
        metadata = parse_file_storage_ref(artifact.storage_ref) or {}
        expected_sha = str(receipt.safe_result_content_sha256)
        expected_size = int(receipt.safe_result_size_bytes or -1)
        if (
            artifact.task_id != call.task_id
            or artifact.producer_node_id != call.node_id
            or not artifact.is_complete
            or metadata.get("source_kind") != "mcp_result"
            or metadata.get("result_ref") != receipt.safe_result_ref
            or metadata.get("sha256") != expected_sha.removeprefix("sha256:")
            or metadata.get("size_bytes") != expected_size
            or not isinstance(metadata.get("storage_key"), str)
        ):
            raise MCPRawResultAuthorityError("historical_authority_invalid")
        try:
            path = self._artifact_file_store.open_path(metadata["storage_key"])
            details = os.stat(path, follow_symlinks=False)
        except (OSError, ValueError) as exc:
            raise MCPRawResultAuthorityError("projection_missing") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != os.getuid()
            or details.st_nlink != 1
            or details.st_size != expected_size
        ):
            raise MCPRawResultAuthorityError(
                "historical_authority_invalid"
            )
        return MCPRawResultDescriptor(
            path=str(path),
            size_bytes=expected_size,
            sha256=expected_sha,
            device=details.st_dev,
            inode=details.st_ino,
            owner_uid=details.st_uid,
        )


class MCPHistoricalResultReprojector:
    def __init__(
        self,
        *,
        storage: MCPHistoricalReprojectionStoragePort,
        authority_resolver: MCPRawResultAuthorityResolver,
        result_service: MCPIsolatedResultService,
        projection_store: MCPProjectionStore,
        projection_attacher: Any,
    ) -> None:
        self._storage = storage
        self._authority_resolver = authority_resolver
        self._result_service = result_service
        self._projection_store = projection_store
        self._projection_attacher = projection_attacher

    async def run_once(self, *, limit: int = 1000) -> MCPHistoricalReprojectionSummary:
        if isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise ValueError("mcp_result_reprojection_limit_invalid")
        counts = {
            "scanned": 0,
            "ready": 0,
            "already_ready": 0,
            "projection_missing": 0,
            "historical_authority_invalid": 0,
            "projection_invalid": 0,
            "revision_retired": 0,
        }
        after_call_ref: str | None = None
        while True:
            calls = await self._storage.list_completed_mcp_calls_for_result_reprojection(
                after_call_ref=after_call_ref,
                limit=limit,
            )
            for call in calls:
                counts["scanned"] += 1
                outcome = await self._reproject_call(call)
                counts[outcome] += 1
            if calls:
                await asyncio.sleep(0)
            if len(calls) < limit:
                break
            after_call_ref = calls[-1].call_ref
        return MCPHistoricalReprojectionSummary(**counts)

    async def _reproject_call(
        self, call: MCPCallRecord
    ) -> Literal[
        "ready",
        "already_ready",
        "projection_missing",
        "historical_authority_invalid",
        "projection_invalid",
        "revision_retired",
    ]:
        receipt = await self._storage.get_mcp_terminal_result_receipt_for_call(
            call.call_ref
        )
        if receipt is not None and receipt.result_parser_revision in {
            None,
            "mcp-result-parser.v1",
        }:
            return "revision_retired"
        if (
            receipt is not None
            and receipt.result_parser_revision != PARSER_REVISION
        ):
            return "projection_invalid"
        artifact = (
            None
            if call.result_ref is None
            else await self._storage.get_artifact(
                mcp_durable_result_artifact_id(call.result_ref)
            )
        )
        if artifact is None:
            return "projection_missing"
        if not _has_reprojection_authority(call, receipt):
            await self._mark_unavailable(
                artifact, "historical_authority_invalid"
            )
            return "historical_authority_invalid"
        assert receipt is not None
        if self._published_projection_is_valid(artifact, call, receipt):
            return "already_ready"
        try:
            async with self._authority_resolver.resolve(
                call=call, receipt=receipt
            ) as descriptor:
                parsed = await self._result_service.parse(
                    owner_user_id=call.owner_user_id,
                    task_id=call.task_id,
                    node_id=call.node_id,
                    call_ref=call.call_ref,
                    request=MCPResultDecodeRequest(
                        protocol_version=str(call.protocol_version),
                        source=MCPResultSource(str(call.terminal_result_source)),
                        payload=descriptor,
                        output_schema=call.output_schema,
                        output_schema_sha256=call.output_schema_sha256,
                        historical_compatibility=False,
                    ),
                )
        except asyncio.CancelledError:
            raise
        except MCPRawResultAuthorityError as exc:
            await self._mark_unavailable(artifact, exc.reason)
            return exc.reason
        except Exception:
            await self._mark_unavailable(artifact, "projection_invalid")
            return "projection_invalid"
        if (
            parsed.checkpoint.outcome != "succeeded"
            or parsed.checkpoint.raw_sha256
            != receipt.safe_result_content_sha256
            or parsed.projection_staging_handle is None
        ):
            if parsed.projection_staging_handle is not None:
                self._result_service.discard_projection(
                    parsed.projection_staging_handle
                )
            reason: MCPHistoricalUnavailableReason = (
                "projection_missing"
                if parsed.checkpoint.outcome == "succeeded"
                else "projection_invalid"
            )
            await self._mark_unavailable(artifact, reason)
            return reason
        published = self._result_service.publish_projection(
            parsed.projection_staging_handle
        )
        try:
            await self._projection_attacher(
                artifact,
                published=published,
                staging_handle=parsed.projection_staging_handle,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._mark_unavailable(artifact, "projection_invalid")
            return "projection_invalid"
        return "ready"

    def _published_projection_is_valid(
        self,
        artifact: Artifact,
        call: MCPCallRecord,
        receipt: MCPTerminalResultReceipt,
    ) -> bool:
        metadata = parse_file_storage_ref(artifact.storage_ref) or {}
        projection_ref = metadata.get("projection_ref")
        projection_sha256 = metadata.get("projection_sha256")
        if not isinstance(projection_ref, str) or not isinstance(
            projection_sha256, str
        ) or metadata.get("parser_revision") != PARSER_REVISION:
            return False
        try:
            self._projection_store.load(
                projection_ref,
                binding=MCPProjectionBinding(
                    owner_user_id=call.owner_user_id,
                    task_id=call.task_id,
                    node_id=call.node_id,
                    call_ref=call.call_ref,
                    raw_sha256=str(receipt.safe_result_content_sha256),
                    output_schema_sha256=call.output_schema_sha256,
                    source=str(call.terminal_result_source),
                    parser_revision=str(metadata.get("parser_revision") or ""),
                ),
                expected_projection_sha256=projection_sha256,
            )
        except Exception:
            return False
        return True

    async def _mark_unavailable(
        self,
        artifact: Artifact,
        reason: MCPHistoricalUnavailableReason,
    ) -> None:
        metadata = parse_file_storage_ref(artifact.storage_ref) or {}
        if metadata.get("source_kind") != "mcp_result":
            return
        for field in (
            "projection_schema",
            "projection_ref",
            "projection_sha256",
            "parser_revision",
        ):
            metadata.pop(field, None)
        metadata["mcp_projection_unavailable_reason"] = reason
        replacement = build_file_storage_ref(metadata)
        if replacement == artifact.storage_ref:
            return
        updated = await self._storage.compare_and_set_artifact_storage_ref(
            artifact.artifact_id,
            artifact.storage_ref,
            replacement,
        )
        if updated:
            return
        current = await self._storage.get_artifact(artifact.artifact_id)
        current_metadata = (
            {}
            if current is None
            else parse_file_storage_ref(current.storage_ref) or {}
        )
        if (
            current_metadata.get("mcp_projection_unavailable_reason") == reason
            or (
                isinstance(current_metadata.get("projection_ref"), str)
                and isinstance(
                    current_metadata.get("projection_sha256"), str
                )
            )
        ):
            return
        raise RuntimeError("mcp_result_reprojection_artifact_cas_conflict")


def _validate_raw_authority(
    call: MCPCallRecord, receipt: MCPTerminalResultReceipt
) -> None:
    if not _matches_call_receipt(call, receipt):
        raise MCPRawResultAuthorityError("historical_authority_invalid")


def _has_reprojection_authority(
    call: MCPCallRecord, receipt: MCPTerminalResultReceipt | None
) -> bool:
    return bool(
        receipt is not None
        and call.protocol_version
        and call.terminal_result_source in {source.value for source in MCPResultSource}
        and call.output_schema is not None
        and call.output_schema_sha256 is not None
        and _matches_call_receipt(call, receipt)
    )


def _matches_call_receipt(
    call: MCPCallRecord, receipt: MCPTerminalResultReceipt
) -> bool:
    return bool(
        call.status == "completed"
        and call.result_ref
        and receipt.terminal_state == "completed"
        and receipt.owner_user_id == call.owner_user_id
        and receipt.task_id == call.task_id
        and receipt.node_id == call.node_id
        and receipt.call_id == call.call_ref
        and receipt.safe_result_ref == call.result_ref
        and receipt.safe_result_size_bytes is not None
        and receipt.safe_result_size_bytes == call.output_size_bytes
        and isinstance(receipt.safe_result_content_sha256, str)
        and receipt.safe_result_content_sha256.startswith("sha256:")
        and receipt.safe_result_store_kind == "durable_content_addressed"
    )


def _matches_lifecycle(call, receipt, lifecycle) -> bool:
    return bool(
        lifecycle.result_ref == receipt.safe_result_ref
        and lifecycle.owner_user_id == call.owner_user_id
        and lifecycle.task_id == call.task_id
        and lifecycle.node_id == call.node_id
        and lifecycle.call_id == call.call_ref
        and lifecycle.size_bytes == receipt.safe_result_size_bytes
        and lifecycle.content_sha256 == receipt.safe_result_content_sha256
        and lifecycle.store_kind == receipt.safe_result_store_kind
    )


__all__ = [
    "MCPHistoricalReprojectionSummary",
    "MCPHistoricalResultReprojector",
    "MCPRawResultAuthorityError",
    "MCPRawResultAuthorityResolver",
]
