from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.models import (
    MCPTerminalCandidateLifecycle,
    MCPTerminalCandidateLifecycleStatus,
    MCPValidatedTerminalResultCandidate,
    MCPTerminalState,
)
from src.integrations.mcp.cp7_artifacts import (
    canonical_sha256,
    mcp_terminal_candidate_id,
)
from src.integrations.mcp.cp7_terminal_lifecycle import (
    MCPTerminalCandidateLifecycleManager,
)
from src.integrations.mcp.cp7_terminal_results import (
    MCPTerminalCandidateSnapshotAuthority,
    enumerate_unconsumed_terminal_result_candidates,
    seal_terminal_result_candidate,
)


NOW = datetime(2026, 8, 19, 8, 0, 0)


class _LifecycleStorage:
    def __init__(self, row: MCPTerminalCandidateLifecycle) -> None:
        self.row = row

    async def list_incomplete_mcp_terminal_candidate_lifecycles(self, *, limit):
        del limit
        if self.row.status in {
            MCPTerminalCandidateLifecycleStatus.ARCHIVING,
            MCPTerminalCandidateLifecycleStatus.DELETING,
        }:
            return [self.row]
        return []

    async def claim_mcp_terminal_candidate_archives(self, now, *, limit):
        del limit
        if (
            self.row.status is MCPTerminalCandidateLifecycleStatus.RETAINED
            and self.row.eligible_at <= now
        ):
            self.row = replace(
                self.row,
                status=MCPTerminalCandidateLifecycleStatus.ARCHIVING,
                revision=self.row.revision + 1,
                archive_candidate_filename=self.row.active_candidate_filename,
                archive_task_index_filename=self.row.active_task_index_filename,
                archive_call_index_filename=self.row.active_call_index_filename,
                updated_at=now,
            )
            return [self.row]
        return []

    async def finish_mcp_terminal_candidate_archive(
        self, candidate_id, expected_revision, archived_at
    ):
        if (
            self.row.candidate_id != candidate_id
            or self.row.revision != expected_revision
            or self.row.status is not MCPTerminalCandidateLifecycleStatus.ARCHIVING
        ):
            return None
        self.row = replace(
            self.row,
            status=MCPTerminalCandidateLifecycleStatus.ARCHIVED,
            revision=self.row.revision + 1,
            eligible_at=archived_at + timedelta(days=30),
            updated_at=archived_at,
        )
        return self.row

    async def claim_mcp_terminal_candidate_deletions(self, now, *, limit):
        del limit
        if (
            self.row.status is MCPTerminalCandidateLifecycleStatus.ARCHIVED
            and self.row.eligible_at <= now
        ):
            self.row = replace(
                self.row,
                status=MCPTerminalCandidateLifecycleStatus.DELETING,
                revision=self.row.revision + 1,
                updated_at=now,
            )
            return [self.row]
        return []

    async def finish_mcp_terminal_candidate_deletion(
        self, candidate_id, expected_revision, deleted_at
    ):
        if (
            self.row.candidate_id != candidate_id
            or self.row.revision != expected_revision
            or self.row.status is not MCPTerminalCandidateLifecycleStatus.DELETING
        ):
            return None
        self.row = replace(
            self.row,
            status=MCPTerminalCandidateLifecycleStatus.DELETED,
            revision=self.row.revision + 1,
            eligible_at=None,
            updated_at=deleted_at,
        )
        return self.row


class CP7TerminalCandidateLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def _sealed(self, root: Path):
        payload_sha = "sha256:" + "a" * 64
        result_ref = "result-store://task-1/call-1"
        candidate = MCPValidatedTerminalResultCandidate(
            candidate_id=mcp_terminal_candidate_id("call-1", payload_sha),
            owner_user_id="alice",
            conversation_id="conversation-1",
            task_id="task-1",
            node_id="node-1",
            intent_id="intent-1",
            call_id="call-1",
            server_id="server-1",
            server_config_version=1,
            server_security_version=1,
            terminal_state=MCPTerminalState.COMPLETED,
            result_payload_sha256=payload_sha,
            safe_result_ref=result_ref,
            safe_result_ref_sha256=canonical_sha256(result_ref),
            safe_error_code=None,
            sealed_at=datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc),
            safe_result_content_sha256="sha256:" + "b" * 64,
            safe_result_size_bytes=12,
            safe_result_store_kind="durable_content_addressed",
        )
        sealed = seal_terminal_result_candidate(root, candidate).sealed
        snapshot = MCPTerminalCandidateSnapshotAuthority(root).snapshot(sealed)
        row = MCPTerminalCandidateLifecycle(
            candidate_id=candidate.candidate_id,
            call_id=candidate.call_id,
            task_id=candidate.task_id,
            candidate_schema=snapshot.candidate_schema,
            active_candidate_filename=snapshot.active_candidate_filename,
            active_task_index_filename=snapshot.active_task_index_filename,
            active_call_index_filename=snapshot.active_call_index_filename,
            candidate_file_sha256=snapshot.candidate_file_sha256,
            task_index_file_sha256=snapshot.task_index_file_sha256,
            call_index_file_sha256=snapshot.call_index_file_sha256,
            status=MCPTerminalCandidateLifecycleStatus.RETAINED,
            revision=0,
            created_at=NOW,
            updated_at=NOW,
            receipt_id="receipt-1",
            consumed_at=NOW,
            eligible_at=NOW,
        )
        return snapshot, row

    async def test_archive_lookup_and_thirty_day_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, row = self._sealed(root)
            storage = _LifecycleStorage(row)
            current = NOW
            manager = MCPTerminalCandidateLifecycleManager(
                storage, root, now_fn=lambda: current
            )
            authority = MCPTerminalCandidateSnapshotAuthority(
                root, archive_root=manager.archive_root
            )

            self.assertEqual(await manager.run_once(), (0, 1, 0))
            self.assertEqual(
                storage.row.status,
                MCPTerminalCandidateLifecycleStatus.ARCHIVED,
            )
            self.assertEqual(
                enumerate_unconsumed_terminal_result_candidates(root), ()
            )
            self.assertEqual(authority.revalidate(snapshot), snapshot)

            current = NOW + timedelta(days=30, seconds=1)
            self.assertEqual(await manager.run_once(), (0, 0, 1))
            self.assertEqual(
                storage.row.status,
                MCPTerminalCandidateLifecycleStatus.DELETED,
            )
            self.assertEqual(list(manager.archive_root.iterdir()), [])

    async def test_startup_repairs_partial_archive_before_strict_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _snapshot, row = self._sealed(root)
            storage = _LifecycleStorage(row)
            failed = False

            def inject(point, _row):
                nonlocal failed
                if point == "archive_unlink" and not failed:
                    failed = True
                    raise RuntimeError("crash_after_first_archive_unlink")

            manager = MCPTerminalCandidateLifecycleManager(
                storage, root, now_fn=lambda: NOW, fault_hook=inject
            )
            with self.assertRaisesRegex(RuntimeError, "crash_after_first"):
                await manager.run_once()
            self.assertEqual(
                storage.row.status,
                MCPTerminalCandidateLifecycleStatus.ARCHIVING,
            )

            restarted = MCPTerminalCandidateLifecycleManager(
                storage, root, now_fn=lambda: NOW
            )
            self.assertEqual(await restarted.repair_incomplete(), 1)
            self.assertEqual(
                storage.row.status,
                MCPTerminalCandidateLifecycleStatus.ARCHIVED,
            )
            self.assertEqual(
                enumerate_unconsumed_terminal_result_candidates(root), ()
            )

    async def test_startup_repairs_partial_archive_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _snapshot, row = self._sealed(root)
            storage = _LifecycleStorage(row)
            current = NOW
            manager = MCPTerminalCandidateLifecycleManager(
                storage, root, now_fn=lambda: current
            )
            await manager.run_once()
            current = NOW + timedelta(days=31)
            failed = False

            def inject(point, _row):
                nonlocal failed
                if point == "delete_unlink" and not failed:
                    failed = True
                    raise RuntimeError("crash_after_first_delete")

            crashing = MCPTerminalCandidateLifecycleManager(
                storage, root, now_fn=lambda: current, fault_hook=inject
            )
            with self.assertRaisesRegex(RuntimeError, "crash_after_first"):
                await crashing.run_once()
            self.assertEqual(
                storage.row.status,
                MCPTerminalCandidateLifecycleStatus.DELETING,
            )

            restarted = MCPTerminalCandidateLifecycleManager(
                storage, root, now_fn=lambda: current
            )
            self.assertEqual(await restarted.repair_incomplete(), 1)
            self.assertEqual(
                storage.row.status,
                MCPTerminalCandidateLifecycleStatus.DELETED,
            )


if __name__ == "__main__":
    unittest.main()
