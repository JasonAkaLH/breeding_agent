from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.integrations.mcp.temporary_results import (
    MCPTemporaryResultError,
    MCPTemporaryResultJanitor,
    MCPTemporaryResultStore,
)


class MCPDurableResultTests(unittest.IsolatedAsyncioTestCase):
    def _sink(
        self,
        store: MCPTemporaryResultStore,
        *,
        owner_user_id: str = "owner-1",
    ):
        return store.create_sink(
            "task-1",
            durable=True,
            owner_user_id=owner_user_id,
            node_id="node-1",
            call_ref="call-1",
        )

    async def test_exact_retry_is_idempotent_without_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            first_sink = self._sink(store)
            await first_sink.write(b'{"ok":true}')
            first = await first_sink.finalize()
            data_path = next(root.rglob(f"{first.ref}.json"))
            manifest_path = next(root.rglob(f"{first.ref}.manifest.json"))
            first_data_inode = data_path.stat().st_ino
            first_manifest_inode = manifest_path.stat().st_ino

            retry_sink = self._sink(store)
            await retry_sink.write(b'{"ok":true}')
            retry = await retry_sink.finalize()

            self.assertEqual(retry, first)
            self.assertEqual(data_path.stat().st_ino, first_data_inode)
            self.assertEqual(manifest_path.stat().st_ino, first_manifest_inode)
            self.assertEqual(data_path.stat().st_nlink, 1)
            self.assertEqual(manifest_path.stat().st_nlink, 1)

    async def test_result_root_symlink_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir(mode=0o700)
            linked_root = base / "linked-root"
            linked_root.symlink_to(target, target_is_directory=True)

            with self.assertRaises(MCPTemporaryResultError):
                MCPTemporaryResultStore(linked_root, memory_threshold_bytes=1)

    async def test_same_ref_with_different_authority_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            first_sink = self._sink(store, owner_user_id="owner-1")
            await first_sink.write(b'{"ok":true}')
            first = await first_sink.finalize()
            manifest_path = next(root.rglob(f"{first.ref}.manifest.json"))
            manifest_before = manifest_path.read_bytes()

            conflicting_sink = self._sink(store, owner_user_id="owner-2")
            await conflicting_sink.write(b'{"ok":true}')
            with self.assertRaises(MCPTemporaryResultError):
                await conflicting_sink.finalize()

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(
                b"".join([chunk async for chunk in store.iter_bytes(first)]),
                b'{"ok":true}',
            )

    async def test_v2_manifest_unknown_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            sink = self._sink(store)
            await sink.write(b'{"ok":true}')
            result = await sink.finalize()
            manifest_path = next(root.rglob(f"{result.ref}.manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["unexpected"] = True
            manifest_path.write_text(
                json.dumps(manifest, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)

            with self.assertRaises(MCPTemporaryResultError):
                MCPTemporaryResultStore(root, memory_threshold_bytes=1)

    async def test_candidate_boundary_revalidates_manifest_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            sink = store.create_sink(
                "task-1",
                durable=True,
                owner_user_id="owner-1",
                node_id="node-1",
                call_ref="call-1",
                scope_id="scope-1",
            )
            await sink.write(b'{"ok":true}')
            result = await sink.finalize()

            verified = await store.verify_durable_ref(
                result.ref,
                owner_user_id="owner-1",
                task_id="task-1",
                node_id="node-1",
                call_ref="call-1",
                scope_id="scope-1",
                expected_size_bytes=result.size_bytes,
                expected_sha256=result.sha256,
                expected_store_kind="durable_content_addressed",
            )
            self.assertEqual(verified, result)

            manifest_path = next(root.rglob(f"{result.ref}.manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["owner_user_id"] = "owner-2"
            manifest_path.write_text(
                json.dumps(manifest, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)

            with self.assertRaises(MCPTemporaryResultError):
                await store.verify_durable_ref(
                    result.ref,
                    owner_user_id="owner-1",
                    task_id="task-1",
                    node_id="node-1",
                    call_ref="call-1",
                    scope_id="scope-1",
                    expected_size_bytes=result.size_bytes,
                    expected_sha256=result.sha256,
                    expected_store_kind="durable_content_addressed",
                )

    async def test_data_link_or_private_mode_drift_fails_closed(self) -> None:
        for mutation in ("hardlink", "mode"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
                sink = self._sink(store)
                await sink.write(b'{"ok":true}')
                result = await sink.finalize()
                data_path = next(root.rglob(f"{result.ref}.json"))
                if mutation == "hardlink":
                    os.link(data_path, root / "unexpected-link")
                else:
                    os.chmod(data_path, 0o640)

                with self.assertRaises(MCPTemporaryResultError):
                    MCPTemporaryResultStore(root, memory_threshold_bytes=1)

    async def test_janitor_does_not_delete_inactive_durable_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            sink = self._sink(store)
            await sink.write(b'{"ok":true}')
            result = await sink.finalize()
            task_dir = next(path for path in root.iterdir() if path.is_dir())
            os.utime(task_dir, (10, 10))

            janitor = MCPTemporaryResultJanitor(
                root, safe_age_seconds=0, clock=lambda: 100
            )
            self.assertEqual(await janitor.cleanup_orphans(), ())
            self.assertEqual(
                b"".join([chunk async for chunk in store.iter_bytes(result)]),
                b'{"ok":true}',
            )
