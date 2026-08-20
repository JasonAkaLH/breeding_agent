from __future__ import annotations

import asyncio
import hashlib
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.integrations.mcp.temporary_results import (
    MCPAdmissionCancelledError,
    MCPDurableResultSnapshotAuthority,
    MCPCapacityUnavailableError,
    MCPResultTooLargeError,
    MCPTemporaryResultError,
    MCPTemporaryResultCapacity,
    MCPTemporaryResultCapacityConfig,
    MCPTemporaryResultJanitor,
    MCPTemporaryResultStore,
    MCPTemporaryStorageExhaustedError,
    MAX_DURABLE_MCP_RESULT_BYTES,
)


async def _record_async(target: list, value: object) -> None:
    target.append(value)


class MCPTemporaryResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_result_limit_accepts_boundary_and_rejects_next_byte(
        self,
    ) -> None:
        self.assertEqual(MAX_DURABLE_MCP_RESULT_BYTES, 64 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "src.integrations.mcp.temporary_results.MAX_DURABLE_MCP_RESULT_BYTES",
            8,
        ):
            store = MCPTemporaryResultStore(
                Path(temporary), memory_threshold_bytes=1
            )
            accepted = store.create_sink(
                "task-a",
                durable=True,
                owner_user_id="owner-a",
                node_id="node-a",
                call_ref="call-a",
            )
            await accepted.write(b"12345678")
            result = await accepted.finalize()
            self.assertEqual(result.size_bytes, 8)
            files_before_rejection = {
                path.relative_to(temporary)
                for path in Path(temporary).rglob("*")
                if path.is_file()
            }

            rejected = store.create_sink(
                "task-b",
                durable=True,
                owner_user_id="owner-b",
                node_id="node-b",
                call_ref="call-b",
            )
            with self.assertRaises(MCPResultTooLargeError) as raised:
                await rejected.write(b"123456789")
            self.assertEqual(raised.exception.mcp_error_code, "mcp_result_too_large")
            self.assertEqual(
                {
                    path.relative_to(temporary)
                    for path in Path(temporary).rglob("*")
                    if path.is_file()
                },
                files_before_rejection,
            )

    async def test_spills_without_using_task_id_as_path_and_preserves_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mcp-results"
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=8)
            sink = store.create_sink("../../another-user/task")
            payload = b'{"content":"streamed-result"}'

            await sink.write(payload[:5])
            await sink.write(payload[5:])
            result = await sink.finalize()
            rebuilt = b"".join([chunk async for chunk in store.iter_bytes(result, chunk_size=4)])

            self.assertEqual(rebuilt, payload)
            self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(result.storage, "file")
            self.assertNotIn(str(root), result.ref)
            self.assertNotIn("another-user", result.ref)
            task_dirs = list(root.iterdir())
            self.assertEqual(len(task_dirs), 1)
            self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(task_dirs[0]).st_mode & 0o777, 0o700)
            files = list(task_dirs[0].iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(os.stat(files[0]).st_mode & 0o777, 0o600)

    async def test_durable_result_is_retrievable_after_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mcp-results"
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1024)
            sink = store.create_sink(
                "task-restart",
                durable=True,
                owner_user_id="owner-restart",
                node_id="node-restart",
                call_ref="call-restart",
            )
            await sink.write(b'{"ok":true}')
            result = await sink.finalize()

            restarted = MCPTemporaryResultStore(root, memory_threshold_bytes=1024)
            rebuilt = b"".join(
                [chunk async for chunk in restarted.iter_bytes(result)]
            )

            self.assertEqual(rebuilt, b'{"ok":true}')
            self.assertEqual(result.storage, "file")
            self.assertEqual(len(restarted.active_task_keys()), 1)
            await restarted.cleanup_task("task-restart")
            janitor = MCPTemporaryResultJanitor(
                root, safe_age_seconds=0, clock=lambda: 10**12
            )
            self.assertEqual(
                await janitor.cleanup_orphans(
                    active_task_keys=restarted.active_task_keys()
                ),
                (),
            )
            self.assertEqual(
                b"".join([chunk async for chunk in restarted.iter_bytes(result)]),
                b'{"ok":true}',
            )

    async def test_durable_refs_are_task_owned_and_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mcp-results"
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1024)
            first_sink = store.create_sink(
                "task-a",
                durable=True,
                owner_user_id="owner-a",
                node_id="node-a",
                call_ref="call-a",
            )
            second_sink = store.create_sink(
                "task-b",
                durable=True,
                owner_user_id="owner-b",
                node_id="node-b",
                call_ref="call-b",
            )
            await first_sink.write(b'{"same":true}')
            await second_sink.write(b'{"same":true}')
            first = await first_sink.finalize()
            second = await second_sink.finalize()

            self.assertNotEqual(first.ref, second.ref)
            first_file = next(
                path for path in root.rglob(f"{first.ref}.json")
            )
            first_file.write_bytes(b"tampered")

            with self.assertRaises(MCPTemporaryResultError):
                MCPTemporaryResultStore(root, memory_threshold_bytes=1024)

    async def test_abort_and_task_cleanup_remove_partial_and_completed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mcp-results"
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            partial = store.create_sink("task-1")
            await partial.write(b"partial")
            await partial.abort()
            self.assertEqual([path for path in root.rglob("*") if path.is_file()], [])

            completed_sink = store.create_sink("task-1")
            await completed_sink.write(b"complete")
            result = await completed_sink.finalize()
            await store.cleanup_task("task-1")

            with self.assertRaises(KeyError):
                _ = [chunk async for chunk in store.iter_bytes(result)]
            self.assertEqual(list(root.iterdir()), [])

    async def test_promoted_result_is_retained_by_task_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=1)
            sink = store.create_sink("task-1")
            await sink.write(b"promote-me")
            result = await sink.finalize()
            store.mark_promoted(result)

            await store.cleanup_task("task-1")

            self.assertEqual(b"".join([chunk async for chunk in store.iter_bytes(result)]), b"promote-me")

    async def test_exact_discard_removes_staged_data_and_manifest_but_never_promoted_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            staged_sink = store.create_sink(
                "task-staged",
                durable=True,
                owner_user_id="owner",
                node_id="node",
                call_ref="call-staged",
            )
            await staged_sink.write(b'{"content":[]}')
            staged = await staged_sink.finalize()
            self.assertEqual(len(list(root.rglob("*.*"))), 2)

            await store.discard(staged)

            self.assertEqual([path for path in root.rglob("*") if path.is_file()], [])
            with self.assertRaises(KeyError):
                store.resolve_ref(staged.ref)

            published_sink = store.create_sink(
                "task-published",
                durable=True,
                owner_user_id="owner",
                node_id="node",
                call_ref="call-published",
            )
            await published_sink.write(b'{"content":[]}')
            published = await published_sink.finalize()
            store.mark_promoted(published)

            await store.discard(published)

            self.assertEqual(store.resolve_ref(published.ref), published)
            self.assertEqual(
                b"".join([chunk async for chunk in store.iter_bytes(published)]),
                b'{"content":[]}',
            )

    async def test_staged_orphan_janitor_uses_age_and_skips_published_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)

            async def make(call_ref: str):
                sink = store.create_sink(
                    f"task-{call_ref}",
                    durable=True,
                    owner_user_id="owner",
                    node_id="node",
                    call_ref=call_ref,
                )
                await sink.write(b'{"content":[]}')
                return await sink.finalize()

            old_staged = await make("old")
            fresh_staged = await make("fresh")
            published = await make("published")
            store.mark_promoted(published)
            for path in root.rglob(f"{old_staged.ref}*"):
                os.utime(path, (0, 0), follow_symlinks=False)
            for path in root.rglob(f"{published.ref}*"):
                os.utime(path, (0, 0), follow_symlinks=False)

            removed = await store.cleanup_staged_orphans(
                safe_age_seconds=10, now_seconds=100
            )

            self.assertEqual(removed, (old_staged.ref,))
            with self.assertRaises(KeyError):
                store.resolve_ref(old_staged.ref)
            self.assertEqual(store.resolve_ref(fresh_staged.ref), fresh_staged)
            self.assertEqual(store.resolve_ref(published.ref), published)

    async def test_exact_discard_never_unlinks_a_held_result_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=1)
            sink = store.create_sink(
                "task-held",
                durable=True,
                owner_user_id="owner",
                node_id="node",
                call_ref="call-held",
            )
            payload = b'{"content":[]}'
            await sink.write(payload)
            result = await sink.finalize()
            authority = MCPDurableResultSnapshotAuthority(store)

            async with authority.open_snapshot(
                result_ref=result.ref,
                owner_user_id="owner",
                task_id="task-held",
                node_id="node",
                call_id="call-held",
                expected_size_bytes=len(payload),
                expected_content_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
                expected_store_kind="durable_content_addressed",
            ):
                await store.discard(result)
                self.assertEqual(store.resolve_ref(result.ref), result)

            await store.discard(result)
            with self.assertRaises(KeyError):
                store.resolve_ref(result.ref)

    async def test_scope_cleanup_does_not_remove_another_server_result_in_same_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=1)
            first_sink = store.create_sink("shared-task", scope_id="scope-server-a")
            second_sink = store.create_sink("shared-task", scope_id="scope-server-b")
            await first_sink.write(b"server-a")
            await second_sink.write(b"server-b")
            first = await first_sink.finalize()
            second = await second_sink.finalize()

            await store.cleanup_scope("scope-server-a")

            with self.assertRaises(KeyError):
                _ = [chunk async for chunk in store.iter_bytes(first)]
            self.assertEqual(b"".join([chunk async for chunk in store.iter_bytes(second)]), b"server-b")

            await store.cleanup_task("shared-task")
            with self.assertRaises(KeyError):
                _ = [chunk async for chunk in store.iter_bytes(second)]

    async def test_scope_cleanup_preserves_another_scopes_in_progress_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=1)
            completed_sink = store.create_sink("shared-task", scope_id="scope-server-a")
            active_sink = store.create_sink("shared-task", scope_id="scope-server-b")
            await completed_sink.write(b"server-a")
            completed = await completed_sink.finalize()
            await active_sink.write(b"server-b")

            await store.cleanup_scope("scope-server-a")
            active = await active_sink.finalize()

            with self.assertRaises(KeyError):
                _ = [chunk async for chunk in store.iter_bytes(completed)]
            self.assertEqual(
                b"".join([chunk async for chunk in store.iter_bytes(active)]),
                b"server-b",
            )

    async def test_janitor_only_removes_old_inactive_task_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "task-old"
            active = root / "task-active"
            recent = root / "task-recent"
            unrelated = root / "other"
            for directory in (old, active, recent, unrelated):
                directory.mkdir()
            os.utime(old, (10, 10))
            os.utime(active, (10, 10))
            os.utime(recent, (95, 95))
            janitor = MCPTemporaryResultJanitor(root, safe_age_seconds=20, clock=lambda: 100)

            removed = await janitor.cleanup_orphans(active_task_keys={"task-active"})

            self.assertEqual(removed, ("task-old",))
            self.assertFalse(old.exists())
            self.assertTrue(active.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())

    async def test_capacity_configuration_and_fail_fast_admission(self) -> None:
        with self.assertRaises(ValueError):
            MCPTemporaryResultCapacityConfig(
                max_active_user_mcp_calls_per_instance=0,
                temporary_disk_low_watermark_bytes=1,
            )
        config = MCPTemporaryResultCapacityConfig(
            max_active_user_mcp_calls_per_instance=1,
            temporary_disk_low_watermark_bytes=10,
        )
        capacity = MCPTemporaryResultCapacity(config, storage_root=Path("/unused"), free_bytes=lambda _path: 100)
        async with capacity.admit():
            with self.assertRaises(MCPCapacityUnavailableError) as ctx:
                async with capacity.admit():
                    pass
            self.assertEqual(ctx.exception.mcp_error_code, "mcp_capacity_unavailable")
        self.assertEqual(capacity.active_calls, 0)

        low_disk = MCPTemporaryResultCapacity(config, storage_root=Path("/unused"), free_bytes=lambda _path: 9)
        with self.assertRaises(MCPCapacityUnavailableError):
            async with low_disk.admit():
                pass

    async def test_keyed_admission_is_round_robin_between_users_and_fifo_within_user(self) -> None:
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(1, 1),
            storage_root=Path("/unused"),
            free_bytes=lambda _path: 100,
        )
        holder = await capacity.acquire("holder", "holder-1")
        order: list[str] = []

        async def wait_for_slot(owner: str, request_ref: str):
            lease = await capacity.acquire(owner, request_ref)
            order.append(request_ref)
            return lease

        tasks = [
            asyncio.create_task(wait_for_slot("alice", "alice-1")),
            asyncio.create_task(wait_for_slot("alice", "alice-2")),
            asyncio.create_task(wait_for_slot("bob", "bob-1")),
            asyncio.create_task(wait_for_slot("bob", "bob-2")),
        ]
        while capacity.queued_calls != 4:
            await asyncio.sleep(0)

        await holder.release()
        for expected, task in zip(
            ("alice-1", "bob-1", "alice-2", "bob-2"), tasks[::2] + tasks[1::2]
        ):
            lease = await asyncio.wait_for(task, timeout=1)
            self.assertEqual(order[-1], expected)
            await lease.release()

        self.assertEqual(order, ["alice-1", "bob-1", "alice-2", "bob-2"])
        self.assertEqual(capacity.active_calls, 0)
        self.assertEqual(capacity.queued_calls, 0)

    async def test_keyed_admission_can_cancel_a_queued_request(self) -> None:
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(1, 1),
            storage_root=Path("/unused"),
            free_bytes=lambda _path: 100,
        )
        holder = await capacity.acquire("alice", "active")
        queued = asyncio.create_task(capacity.acquire("bob", "queued"))
        while capacity.queued_calls != 1:
            await asyncio.sleep(0)

        self.assertTrue(await capacity.cancel("queued"))
        with self.assertRaises(MCPAdmissionCancelledError):
            await queued
        self.assertFalse(await capacity.cancel("queued"))
        await holder.release()

    async def test_keyed_admission_reports_queue_entry_and_admission(self) -> None:
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(1, 1),
            storage_root=Path("/unused"),
            free_bytes=lambda _path: 100,
        )
        holder = await capacity.acquire("alice", "active")
        events: list[tuple[str, int | None]] = []

        queued = asyncio.create_task(
            capacity.acquire(
                "bob",
                "queued",
                on_queued=lambda position: _record_async(
                    events, ("queued", position)
                ),
                on_admitted=lambda: _record_async(events, ("admitted", None)),
            )
        )
        while capacity.queued_calls != 1:
            await asyncio.sleep(0)
        self.assertEqual(events, [("queued", 1)])

        await holder.release()
        lease = await queued
        self.assertEqual(events, [("queued", 1), ("admitted", None)])
        await lease.release()

    async def test_disk_exhaustion_returns_stable_error_and_removes_partial_file(self) -> None:
        class ExhaustedHandle:
            def write(self, _data):
                raise OSError(errno.ENOSPC, "no space")

            def flush(self):
                return None

            def close(self):
                return None

        def exhausted_fdopen(descriptor, _mode):
            os.close(descriptor)
            return ExhaustedHandle()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=0)
            sink = store.create_sink("task-1")
            with patch("src.integrations.mcp.temporary_results.os.fdopen", side_effect=exhausted_fdopen):
                await sink.write(b"payload")
                with self.assertRaises(MCPTemporaryStorageExhaustedError) as ctx:
                    await sink.finalize()

            self.assertEqual(ctx.exception.mcp_error_code, "temporary_storage_exhausted")
            self.assertEqual([path for path in root.rglob("*") if path.is_file()], [])


if __name__ == "__main__":
    unittest.main()
