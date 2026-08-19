from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from src.core.models import (
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

    async def claim_mcp_durable_result_deletions(self, now, *, limit):
        del limit
        if (
            self.row.status is MCPDurableResultLifecycleStatus.RETAINED
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
            status=MCPDurableResultLifecycleStatus.RETAINED,
            revision=self.row.revision + 1,
            eligible_at=retry_at,
            updated_at=retry_at,
        )
        return self.row


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
