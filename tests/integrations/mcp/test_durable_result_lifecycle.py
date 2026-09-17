from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.core.models import (
    Artifact,
    MCPDurableResultLifecycle,
    MCPDurableResultLifecycleReason,
    MCPDurableResultLifecycleStatus,
)
from src.integrations.mcp.durable_result_lifecycle import (
    MCPDurableResultLifecycleManager,
)
from src.integrations.mcp.temporary_results import (
    MCPDurableResultSnapshotAuthority,
    MCPTemporaryResultStore,
)
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    parse_file_storage_ref,
)


NOW = datetime(2026, 8, 19, 10, 0, 0)


class _LifecycleStorage:
    def __init__(self, row: MCPDurableResultLifecycle) -> None:
        self.row = row

    async def list_incomplete_mcp_durable_result_lifecycles(self, *, limit):
        del limit
        return (
            [self.row]
            if self.row.status is MCPDurableResultLifecycleStatus.DELETING
            else []
        )

    async def get_mcp_durable_result_lifecycle(self, result_ref):
        return self.row if self.row.result_ref == result_ref else None

    async def claim_mcp_durable_result_deletions(self, now, *, limit):
        del limit
        if (
            self.row.status
            in {
                MCPDurableResultLifecycleStatus.RETAINED,
                MCPDurableResultLifecycleStatus.ARTIFACT_OWNED,
            }
            and self.row.eligible_at is not None
            and self.row.eligible_at <= now
        ):
            self.row = replace(
                self.row,
                status=MCPDurableResultLifecycleStatus.DELETING,
                revision=self.row.revision + 1,
                updated_at=now,
            )
            return [self.row]
        return []

    async def finish_mcp_durable_result_deletion(
        self, result_ref, expected_revision, deleted_at
    ):
        if (
            self.row.result_ref != result_ref
            or self.row.revision != expected_revision
            or self.row.status is not MCPDurableResultLifecycleStatus.DELETING
        ):
            return None
        self.row = replace(
            self.row,
            status=MCPDurableResultLifecycleStatus.DELETED,
            revision=self.row.revision + 1,
            eligible_at=None,
            deleted_at=deleted_at,
            updated_at=deleted_at,
        )
        return self.row

    async def release_mcp_durable_result_deletion(
        self, result_ref, expected_revision, retry_at
    ):
        if (
            self.row.result_ref != result_ref
            or self.row.revision != expected_revision
            or self.row.status is not MCPDurableResultLifecycleStatus.DELETING
        ):
            return None
        self.row = replace(
            self.row,
            status=(
                MCPDurableResultLifecycleStatus.ARTIFACT_OWNED
                if self.row.reason
                is MCPDurableResultLifecycleReason.ARTIFACT_PROMOTED
                else MCPDurableResultLifecycleStatus.RETAINED
            ),
            revision=self.row.revision + 1,
            eligible_at=retry_at,
            updated_at=retry_at,
        )
        return self.row


class _PromotionStorage(_LifecycleStorage):
    def __init__(self, row: MCPDurableResultLifecycle) -> None:
        super().__init__(row)
        self.artifacts: dict[str, Artifact] = {}

    async def get_mcp_durable_result_lifecycle(self, result_ref):
        return self.row if self.row.result_ref == result_ref else None

    async def get_artifact(self, artifact_id):
        return self.artifacts.get(artifact_id)

    async def save_artifact(self, artifact):
        existing = self.artifacts.get(artifact.artifact_id)
        if existing is not None and existing != artifact:
            raise RuntimeError("artifact_conflict")
        self.artifacts[artifact.artifact_id] = artifact
        return artifact

    async def mark_mcp_durable_result_artifact_owned(
        self,
        result_ref,
        expected_revision,
        artifact_id,
        expected_size_bytes,
        expected_content_sha256,
        occurred_at,
    ):
        if (
            self.row.result_ref != result_ref
            or self.row.revision != expected_revision
            or artifact_id not in self.artifacts
            or self.row.size_bytes != expected_size_bytes
            or self.row.content_sha256 != expected_content_sha256
        ):
            return None
        self.row = replace(
            self.row,
            status=MCPDurableResultLifecycleStatus.ARTIFACT_OWNED,
            reason=MCPDurableResultLifecycleReason.ARTIFACT_PROMOTED,
            revision=self.row.revision + 1,
            eligible_at=occurred_at,
            updated_at=occurred_at,
        )
        return self.row


class _ProjectionLifecycleStorage(_LifecycleStorage):
    async def list_projectable_mcp_durable_result_lifecycles(
        self,
        *,
        after_updated_at=None,
        after_result_ref=None,
        limit=1000,
    ):
        del after_updated_at, after_result_ref, limit
        if (
            self.row.status is MCPDurableResultLifecycleStatus.RETAINED
            and self.row.reason
            is MCPDurableResultLifecycleReason.DISPATCH_RESOLVED
        ):
            return [self.row]
        return []

    async def claim_mcp_dispatch_result_deletion(
        self, result_ref, expected_revision, now
    ):
        if (
            self.row.result_ref != result_ref
            or self.row.revision != expected_revision
            or self.row.status is not MCPDurableResultLifecycleStatus.RETAINED
            or self.row.reason
            is not MCPDurableResultLifecycleReason.DISPATCH_RESOLVED
            or self.row.eligible_at is None
            or self.row.eligible_at > now
        ):
            return None
        self.row = replace(
            self.row,
            status=MCPDurableResultLifecycleStatus.DELETING,
            revision=self.row.revision + 1,
            updated_at=now,
        )
        return self.row

    async def claim_mcp_durable_result_deletions(self, now, *, limit):
        del now, limit
        return []


class _PermanentProjector:
    def __init__(self):
        self.calls = []

    async def project_completed_result(self, result_ref, *, source):
        self.calls.append((result_ref, source))
        return SimpleNamespace(status="permanent_failure")


class _BusinessReprojector:
    async def run_once(self, *, limit):
        del limit
        return SimpleNamespace(
            ready=1,
            already_ready=2,
            projection_missing=3,
            historical_authority_invalid=4,
            projection_invalid=5,
            revision_retired=6,
        )

class DurableResultLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def _result(self, root: Path):
        store = MCPTemporaryResultStore(root, memory_threshold_bytes=0)
        sink = store.create_sink(
            "task-1",
            scope_id="scope-1",
            durable=True,
            owner_user_id="alice",
            node_id="node-1",
            call_ref="call-1",
        )
        await sink.write(b'{"ok":true}')
        result = await sink.finalize()
        authority = MCPDurableResultSnapshotAuthority(store)
        manager = authority.open_snapshot(
            result_ref=result.ref,
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_id="call-1",
            expected_size_bytes=result.size_bytes,
            expected_content_sha256="sha256:" + result.sha256,
            expected_store_kind="durable_content_addressed",
        )
        async with manager as snapshot:
            row = MCPDurableResultLifecycle(
                result_ref=snapshot.result_ref,
                owner_user_id=snapshot.owner_user_id,
                task_id=snapshot.task_id,
                node_id=snapshot.node_id,
                call_id=snapshot.call_id,
                content_sha256=snapshot.content_sha256,
                size_bytes=snapshot.size_bytes,
                data_filename=snapshot.data_filename,
                manifest_filename=snapshot.manifest_filename,
                data_file_sha256=snapshot.data_file_sha256,
                manifest_file_sha256=snapshot.manifest_file_sha256,
                store_kind=snapshot.store_kind,
                status=MCPDurableResultLifecycleStatus.RETAINED,
                reason=MCPDurableResultLifecycleReason.DISPATCH_RESOLVED,
                revision=0,
                created_at=NOW,
                updated_at=NOW,
                eligible_at=NOW,
            )
        return store, authority, result, row

    async def test_eligible_result_deletes_data_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, authority, result, row = await self._result(root)
            storage = _LifecycleStorage(row)
            manager = MCPDurableResultLifecycleManager(
                storage, authority, now_fn=lambda: NOW
            )

            self.assertEqual(await manager.run_once(), (0, 1))
            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.DELETED,
            )
            with self.assertRaises(KeyError):
                store.resolve_ref(result.ref)
            self.assertEqual(list(root.glob("task-*/*")), [])

    async def test_active_snapshot_prevents_deletion_and_releases_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, authority, result, row = await self._result(root)
            storage = _LifecycleStorage(row)
            manager = MCPDurableResultLifecycleManager(
                storage, authority, now_fn=lambda: NOW
            )

            async with authority.open_snapshot(
                result_ref=result.ref,
                owner_user_id="alice",
                task_id="task-1",
                node_id="node-1",
                call_id="call-1",
                expected_size_bytes=result.size_bytes,
                expected_content_sha256="sha256:" + result.sha256,
                expected_store_kind="durable_content_addressed",
            ):
                self.assertEqual(await manager.run_once(), (0, 1))

            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.RETAINED,
            )
            self.assertEqual(storage.row.eligible_at, NOW + timedelta(minutes=5))
            self.assertEqual(store.resolve_ref(result.ref).ref, result.ref)

    async def test_artifact_owned_snapshot_release_preserves_artifact_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, authority, result, retained = await self._result(root)
            row = replace(
                retained,
                status=MCPDurableResultLifecycleStatus.ARTIFACT_OWNED,
                reason=MCPDurableResultLifecycleReason.ARTIFACT_PROMOTED,
            )
            storage = _LifecycleStorage(row)
            manager = MCPDurableResultLifecycleManager(
                storage, authority, now_fn=lambda: NOW
            )

            async with authority.open_snapshot(
                result_ref=result.ref,
                owner_user_id="alice",
                task_id="task-1",
                node_id="node-1",
                call_id="call-1",
                expected_size_bytes=result.size_bytes,
                expected_content_sha256="sha256:" + result.sha256,
                expected_store_kind="durable_content_addressed",
            ):
                self.assertEqual(await manager.run_once(), (0, 1))

            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.ARTIFACT_OWNED,
            )
            self.assertEqual(
                storage.row.reason,
                MCPDurableResultLifecycleReason.ARTIFACT_PROMOTED,
            )

    async def test_verified_artifact_copy_takes_over_result_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_root = root / "results"
            artifact_root = root / "artifacts"
            store, authority, result, row = await self._result(result_root)
            storage = _PromotionStorage(row)
            artifact_store = LocalArtifactFileStore(artifact_root)
            manager = MCPDurableResultLifecycleManager(
                storage,
                authority,
                artifact_file_store=artifact_store,
                now_fn=lambda: NOW,
            )

            artifact = await manager.promote_to_artifact(
                result_ref=result.ref,
            )

            metadata = parse_file_storage_ref(artifact.storage_ref)
            self.assertEqual(metadata["source_kind"], "mcp_result")
            self.assertEqual(metadata["sha256"], result.sha256)
            artifact_path = artifact_store.open_path(metadata["storage_key"])
            self.assertEqual(artifact_path.read_bytes(), b'{"ok":true}')
            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.ARTIFACT_OWNED,
            )

            self.assertEqual(await manager.run_once(), (0, 1))
            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.DELETED,
            )
            self.assertEqual(artifact_path.read_bytes(), b'{"ok":true}')
            self.assertEqual(
                await manager.promote_to_artifact(result_ref=result.ref),
                artifact,
            )

    async def test_reconciler_exactly_deletes_expired_dispatch_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _store, authority, result, row = await self._result(root)
            storage = _ProjectionLifecycleStorage(
                replace(row, eligible_at=NOW)
            )
            projector = _PermanentProjector()
            manager = MCPDurableResultLifecycleManager(
                storage,
                authority,
                now_fn=lambda: NOW,
            )
            manager.configure_result_projector(projector)

            summary = await manager.reconcile_artifacts_and_gc_once()

            self.assertEqual(
                projector.calls,
                [(result.ref, "reconciler")],
            )
            self.assertEqual(summary.permanent_failure, 1)
            self.assertEqual(summary.exact_deleted, 1)
            self.assertEqual(summary.bulk_deleted, 0)
            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.DELETED,
            )

    async def test_reconciler_reports_retired_business_projection_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _store, authority, _result, row = await self._result(root)
            storage = _ProjectionLifecycleStorage(
                replace(
                    row,
                    status=MCPDurableResultLifecycleStatus.DELETED,
                    eligible_at=None,
                    deleted_at=NOW,
                )
            )
            manager = MCPDurableResultLifecycleManager(
                storage,
                authority,
                now_fn=lambda: NOW,
            )
            manager.configure_result_projector(_PermanentProjector())
            manager.configure_business_reprojector(_BusinessReprojector())

            summary = await manager.reconcile_artifacts_and_gc_once()

            self.assertEqual(summary.business_ready, 1)
            self.assertEqual(summary.business_projection_invalid, 5)
            self.assertEqual(summary.business_revision_retired, 6)

    async def test_startup_repairs_manifest_first_partial_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _store, authority, _result, row = await self._result(root)
            storage = _LifecycleStorage(row)
            failed = False

            def inject(point, _row):
                nonlocal failed
                if point == "manifest_unlink" and not failed:
                    failed = True
                    raise RuntimeError("crash_after_manifest_unlink")

            manager = MCPDurableResultLifecycleManager(
                storage,
                authority,
                now_fn=lambda: NOW,
                fault_hook=inject,
            )
            with self.assertRaisesRegex(RuntimeError, "crash_after_manifest"):
                await manager.run_once()
            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.DELETING,
            )

            restarted_store = MCPTemporaryResultStore(
                root, memory_threshold_bytes=0
            )
            restarted = MCPDurableResultLifecycleManager(
                storage,
                MCPDurableResultSnapshotAuthority(restarted_store),
                now_fn=lambda: NOW,
            )
            self.assertEqual(await restarted.repair_incomplete(), 1)
            self.assertEqual(
                storage.row.status,
                MCPDurableResultLifecycleStatus.DELETED,
            )
            self.assertEqual(list(root.glob("task-*/*")), [])


if __name__ == "__main__":
    unittest.main()
