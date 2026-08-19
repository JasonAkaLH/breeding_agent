from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from src.core.contracts import StoragePort
from src.core.enums import ArtifactType
from src.core.models import Artifact, MCPDurableResultLifecycle
from src.integrations.mcp.temporary_results import (
    MCPDurableResultSnapshotAuthority,
)
from src.integrations.mcp.cp7_artifacts import mcp_durable_result_artifact_id
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    build_file_storage_ref,
    parse_file_storage_ref,
)


class MCPDurableResultLifecycleError(RuntimeError):
    pass


class MCPDurableResultLifecycleManager:
    def __init__(
        self,
        storage: StoragePort,
        snapshot_authority: MCPDurableResultSnapshotAuthority,
        *,
        artifact_file_store: LocalArtifactFileStore | None = None,
        now_fn: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str, MCPDurableResultLifecycle], None] | None = None,
    ) -> None:
        self._storage = storage
        self._snapshot_authority = snapshot_authority
        self._artifact_file_store = artifact_file_store
        self._now = now_fn or (
            lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        )
        self._fault_hook = fault_hook

    async def repair_incomplete(self, *, limit: int = 1000) -> int:
        rows = await self._storage.list_incomplete_mcp_durable_result_lifecycles(
            limit=limit
        )
        for row in rows:
            await self._delete(row)
        return len(rows)

    async def run_once(self, *, limit: int = 1000) -> tuple[int, int]:
        repaired = await self.repair_incomplete(limit=limit)
        _reconciled, deferred = await self.reconcile_untracked(limit=limit)
        if deferred:
            return repaired, 0
        deleting = await self._storage.claim_mcp_durable_result_deletions(
            self._now(), limit=limit
        )
        for row in deleting:
            await self._delete(row)
        return repaired, len(deleting)

    async def reconcile_untracked(
        self, *, limit: int = 1000
    ) -> tuple[int, int]:
        reconciled = 0
        deferred = 0
        after_result_ref: str | None = None
        while True:
            identities = self._snapshot_authority.list_result_identities(
                after_result_ref=after_result_ref,
                limit=limit,
            )
            for identity in identities:
                existing = await self._storage.get_mcp_durable_result_lifecycle(
                    identity.result_ref
                )
                if existing is not None:
                    continue
                if (
                    identity.owner_user_id is None
                    or identity.node_id is None
                    or identity.call_id is None
                ):
                    deferred += 1
                    continue
                async with self._snapshot_authority.open_snapshot(
                    result_ref=identity.result_ref,
                    owner_user_id=identity.owner_user_id,
                    task_id=identity.task_id,
                    node_id=identity.node_id,
                    call_id=identity.call_id,
                    expected_size_bytes=identity.size_bytes,
                    expected_content_sha256=identity.content_sha256,
                    expected_store_kind=identity.store_kind,
                ) as snapshot:
                    saved = await self._storage.reconcile_mcp_durable_result_lifecycle(
                        snapshot,
                        self._now(),
                    )
                if saved is None:
                    deferred += 1
                else:
                    reconciled += 1
            if len(identities) < limit:
                return reconciled, deferred
            after_result_ref = identities[-1].result_ref

    async def _delete(self, row: MCPDurableResultLifecycle) -> None:
        deleted = await self._snapshot_authority.delete_lifecycle_files(
            row,
            fault_hook=self._fault_hook,
        )
        if not deleted:
            released = await self._storage.release_mcp_durable_result_deletion(
                row.result_ref,
                row.revision,
                self._now() + timedelta(minutes=5),
            )
            if released is None:
                raise MCPDurableResultLifecycleError(
                    "mcp_durable_result_release_cas_conflict"
                )
            return
        saved = await self._storage.finish_mcp_durable_result_deletion(
            row.result_ref,
            row.revision,
            self._now(),
        )
        if saved is None:
            raise MCPDurableResultLifecycleError(
                "mcp_durable_result_delete_cas_conflict"
            )

    async def promote_to_artifact(
        self,
        *,
        result_ref: str,
        filename: str = "mcp-result.json",
        summary: str = "MCP Tool result",
    ) -> Artifact:
        if self._artifact_file_store is None:
            raise MCPDurableResultLifecycleError(
                "mcp_durable_result_artifact_store_unavailable"
            )
        lifecycle = await self._storage.get_mcp_durable_result_lifecycle(
            result_ref
        )
        if lifecycle is None:
            raise MCPDurableResultLifecycleError(
                "mcp_durable_result_lifecycle_missing"
            )
        artifact_id = mcp_durable_result_artifact_id(lifecycle.result_ref)
        existing = await self._storage.get_artifact(artifact_id)
        if existing is not None:
            self._validate_promoted_artifact(
                existing,
                lifecycle,
                self._artifact_file_store,
            )
            artifact = existing
            if str(lifecycle.status) == "deleted":
                return artifact
        else:
            if str(lifecycle.status) != "retained":
                raise MCPDurableResultLifecycleError(
                    "mcp_durable_result_artifact_missing"
                )
            _snapshot, stored = (
                await self._snapshot_authority.copy_to_artifact_file(
                    result_ref=lifecycle.result_ref,
                    owner_user_id=lifecycle.owner_user_id,
                    task_id=lifecycle.task_id,
                    node_id=lifecycle.node_id,
                    call_id=lifecycle.call_id,
                    expected_size_bytes=lifecycle.size_bytes,
                    expected_content_sha256=lifecycle.content_sha256,
                    expected_store_kind=lifecycle.store_kind,
                    file_store=self._artifact_file_store,
                    artifact_id=artifact_id,
                    filename=filename,
                )
            )
            artifact = Artifact(
                artifact_id=artifact_id,
                task_id=lifecycle.task_id,
                producer_node_id=lifecycle.node_id,
                artifact_type=ArtifactType.FILE,
                storage_ref=build_file_storage_ref(
                    {
                        "version": 1,
                        "source_kind": "mcp_result",
                        "storage_key": stored.storage_key,
                        "filename": stored.filename,
                        "mime_type": "application/json",
                        "size_bytes": stored.size_bytes,
                        "sha256": stored.sha256,
                        "summary": summary,
                        "result_ref": lifecycle.result_ref,
                        "retention_status": "active",
                    }
                ),
                summary=summary,
                is_complete=True,
                created_at=self._now(),
            )
            artifact = await self._storage.save_artifact(artifact)
        promoted = await self._storage.mark_mcp_durable_result_artifact_owned(
            lifecycle.result_ref,
            lifecycle.revision,
            artifact.artifact_id,
            lifecycle.size_bytes,
            lifecycle.content_sha256,
            self._now(),
        )
        if promoted is None:
            current = await self._storage.get_mcp_durable_result_lifecycle(
                lifecycle.result_ref
            )
            if current is None or str(current.status) != "artifact_owned":
                raise MCPDurableResultLifecycleError(
                    "mcp_durable_result_artifact_promotion_cas_conflict"
                )
        return artifact

    @staticmethod
    def _validate_promoted_artifact(
        artifact: Artifact,
        lifecycle: MCPDurableResultLifecycle,
        file_store: LocalArtifactFileStore,
    ) -> None:
        metadata = parse_file_storage_ref(artifact.storage_ref) or {}
        if (
            artifact.task_id != lifecycle.task_id
            or artifact.producer_node_id != lifecycle.node_id
            or artifact.artifact_type is not ArtifactType.FILE
            or not artifact.is_complete
            or metadata.get("source_kind") != "mcp_result"
            or metadata.get("result_ref") != lifecycle.result_ref
            or metadata.get("size_bytes") != lifecycle.size_bytes
            or metadata.get("sha256")
            != lifecycle.content_sha256.removeprefix("sha256:")
            or metadata.get("retention_status") != "active"
        ):
            raise MCPDurableResultLifecycleError(
                "mcp_durable_result_artifact_identity_conflict"
            )
        try:
            path = file_store.open_path(str(metadata["storage_key"]))
            before = path.stat(follow_symlinks=False)
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                after = os.fstat(handle.fileno())
            after_path = path.stat(follow_symlinks=False)
        except (KeyError, OSError, ValueError) as exc:
            raise MCPDurableResultLifecycleError(
                "mcp_durable_result_artifact_file_invalid"
            ) from exc
        def identity(item):
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
            )
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or identity(before) != identity(opened)
            or identity(opened) != identity(after)
            or identity(after) != identity(after_path)
            or size != lifecycle.size_bytes
            or "sha256:" + digest.hexdigest() != lifecycle.content_sha256
        ):
            raise MCPDurableResultLifecycleError(
                "mcp_durable_result_artifact_file_invalid"
            )


__all__ = [
    "MCPDurableResultLifecycleError",
    "MCPDurableResultLifecycleManager",
]
