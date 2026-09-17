from __future__ import annotations

import asyncio
import errno
import os
import resource
import stat
import struct
import tempfile
import threading
import time
import tracemalloc
import unittest
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import patch

from src.core.models import (
    MCPCallRecord,
    MCPExecutionTerminalProjection,
    MCPExecutionTerminalProjectionStatus,
    MCPExecutionTerminalReason,
    MCPPendingToolAction,
    MCPPendingToolActionStatus,
    MCPTerminalResultCompletionMode,
    MCPTerminalResultReceipt,
    MCPTerminalState,
)
from src.integrations.master_key import MasterKeyDeriver, MasterKeyDomain
from src.integrations.mcp.cp7_artifacts import canonical_json_bytes
from src.integrations.mcp.pending_action_payloads import (
    MAX_PENDING_ACTION_ARGUMENT_BYTES,
    MCPPendingActionPayloadCipher,
    MCPPendingActionPayloadDeletionEvidence,
    MCPPendingActionPayloadError,
    MCPPendingActionPayloadIdentity,
    MCPPendingActionPayloadStore,
    pending_action_payload_deletion_evidence,
    pending_action_payload_identity,
    pending_action_payload_ref,
)


class PendingActionPayloadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "pending-actions"
        self.cipher = MCPPendingActionPayloadCipher(
            MasterKeyDeriver.from_bytes(b"p" * 32).derive(
                MasterKeyDomain.MCP_RECOVERY
            )
        )
        self.store = MCPPendingActionPayloadStore(
            self.root,
            cipher=self.cipher,
            disk_available=lambda _path: True,
            gate_wait_interval_seconds=0.01,
        )
        self.identity = MCPPendingActionPayloadIdentity(
            action_id="action-1",
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            server_id="server-1",
            tool_name="lookup",
            server_config_version=3,
            server_security_version=4,
            input_schema_sha256="sha256:schema",
            arguments_sha256="sha256:arguments-fingerprint",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _path(self, identity=None) -> Path:
        payload_ref = pending_action_payload_ref(identity or self.identity)
        return self.root / f"{payload_ref}.bin"

    def test_terminal_evidence_follows_only_exact_direct_or_mrtr_binding(self) -> None:
        action = MCPPendingToolAction(
            action_id=self.identity.action_id,
            owner_user_id=self.identity.owner_user_id,
            conversation_id="conversation-1",
            task_id=self.identity.task_id,
            node_id=self.identity.node_id,
            server_id=self.identity.server_id,
            tool_name=self.identity.tool_name,
            arguments_sha256=self.identity.arguments_sha256,
            approval_fingerprint="sha256:approval",
            arguments_payload_ref=pending_action_payload_ref(self.identity),
            payload_file_sha256="sha256:file",
            payload_size_bytes=12,
            encryption_version=1,
            server_config_version=self.identity.server_config_version,
            server_security_version=self.identity.server_security_version,
            input_schema_sha256=self.identity.input_schema_sha256,
            status=MCPPendingToolActionStatus.CONSUMED,
            revision=2,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            approved_at=datetime.now(timezone.utc),
            consumed_at=datetime.now(timezone.utc),
        )
        original = MCPCallRecord(
            call_ref="call-1",
            branch_id="branch-1",
            owner_user_id=action.owner_user_id,
            task_id=action.task_id,
            node_id=action.node_id,
            server_id=action.server_id,
            tool_name=action.tool_name,
            status="input_required",
            call_sequence=1,
            arguments_sha256=action.arguments_sha256,
            server_security_version=action.server_security_version,
            input_schema_sha256=action.input_schema_sha256,
            server_config_version=action.server_config_version,
            pending_action_id=action.action_id,
        )
        continuation = replace(
            original,
            call_ref="call-2",
            status="failed",
            call_sequence=2,
            pending_action_id=None,
            continuation_of_call_ref=original.call_ref,
            safe_error_code="remote_failed",
        )
        receipt = MCPTerminalResultReceipt(
            result_receipt_id="receipt-2",
            candidate_id="candidate-2",
            owner_user_id=action.owner_user_id,
            conversation_id=action.conversation_id,
            task_id=action.task_id,
            node_id=action.node_id,
            intent_id="intent-1",
            call_id=continuation.call_ref,
            server_id=action.server_id,
            server_config_version=action.server_config_version,
            server_security_version=action.server_security_version,
            terminal_state=MCPTerminalState.FAILED,
            result_payload_sha256="sha256:" + "1" * 64,
            safe_result_ref=None,
            safe_result_ref_sha256=None,
            safe_error_code="remote_failed",
            completion_mode=MCPTerminalResultCompletionMode.NORMAL_TERMINAL_PROJECTION,
            committed_at=datetime.now(timezone.utc),
        )

        self.assertEqual(pending_action_payload_identity(action), self.identity)
        evidence = pending_action_payload_deletion_evidence(
            action=action,
            call=continuation,
            original_call=original,
            receipt=receipt,
            projection=None,
        )
        self.assertEqual(evidence.result_receipt_id, receipt.result_receipt_id)
        self.assertIsNone(
            pending_action_payload_deletion_evidence(
                action=action,
                call=replace(
                    continuation,
                    arguments_sha256="sha256:drift",
                ),
                original_call=original,
                receipt=receipt,
                projection=None,
            )
        )

        unknown_call = replace(
            original,
            status="unknown",
            safe_error_code="execution_status_unknown",
        )
        projection = MCPExecutionTerminalProjection(
            projection_id="projection-1",
            owner_user_id=action.owner_user_id,
            conversation_id=action.conversation_id,
            intent_id="intent-1",
            call_id=unknown_call.call_ref,
            task_id=action.task_id,
            node_id=action.node_id,
            status=MCPExecutionTerminalProjectionStatus.UNKNOWN,
            revision=0,
            no_replay=True,
            reason_code=MCPExecutionTerminalReason.TRUSTED_TERMINAL_RESULT_ABSENT,
            unknown_intent_revision=1,
            unknown_event_id="unknown-event-1",
            task_failed_event_id="task-failed-1",
            unknown_terminal_at=datetime.now(timezone.utc),
            task_terminal_status="failed",
            node_terminal_status="failed",
            result_receipt_id=None,
            result_payload_sha256=None,
            resolved_terminal_state=None,
            safe_result_ref=None,
            safe_result_ref_sha256=None,
            safe_error_code=None,
            resolved_intent_revision=None,
            resolution_event_id=None,
            correction_event_id=None,
            result_committed_at=None,
            resolved_at=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        evidence = pending_action_payload_deletion_evidence(
            action=action,
            call=unknown_call,
            original_call=None,
            receipt=None,
            projection=projection,
        )
        self.assertEqual(evidence.unknown_projection_id, projection.projection_id)

    async def test_round_trip_is_encrypted_no_clobber_and_descriptor_guarded(self) -> None:
        arguments = {"query": "secret-customer-value", "page": 2}
        first = await self.store.seal(self.identity, arguments)
        second = await self.store.seal(self.identity, arguments)

        self.assertEqual(first, second)
        self.assertEqual(stat_mode(self.root), 0o700)
        self.assertEqual(stat_mode(self._path()), 0o600)
        self.assertNotIn(b"secret-customer-value", self._path().read_bytes())
        async with self.store.open_validated(
            self.identity,
            first.arguments_payload_ref,
            expected_snapshot=first,
        ) as opened:
            self.assertEqual(opened.arguments, arguments)
            self.assertEqual(self.store.revalidate(opened.snapshot), first)
            self.assertNotIn("secret-customer-value", repr(opened))
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_descriptor_not_held",
        ):
            self.store.revalidate(first)

    async def test_publish_fsyncs_file_and_directory_and_rejects_symlink_root(self) -> None:
        calls: list[str] = []
        real_fsync = os.fsync

        def recording_fsync(descriptor: int) -> None:
            calls.append(
                "directory"
                if stat.S_ISDIR(os.fstat(descriptor).st_mode)
                else "file"
            )
            real_fsync(descriptor)

        with patch(
            "src.integrations.mcp.pending_action_payloads.os.fsync",
            recording_fsync,
        ):
            await self.store.seal(self.identity, {"query": "alice"})
        self.assertIn("file", calls)
        self.assertIn("directory", calls)

        target = Path(self.temporary.name) / "real-root"
        target.mkdir(mode=0o700)
        symlink = Path(self.temporary.name) / "symlink-root"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_root_unsafe",
        ):
            MCPPendingActionPayloadStore(symlink, cipher=self.cipher)

    async def test_exact_32_mib_boundary_and_rss_increment(self) -> None:
        empty_size = len(canonical_json_bytes({"blob": ""}))
        arguments = {"blob": "x" * (MAX_PENDING_ACTION_ARGUMENT_BYTES - empty_size)}
        self.assertEqual(
            len(canonical_json_bytes(arguments)),
            MAX_PENDING_ACTION_ARGUMENT_BYTES,
        )
        tracemalloc.start()
        baseline, _ = tracemalloc.get_traced_memory()
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        snapshot = await self.store.seal(self.identity, arguments)
        async with self.store.open_validated(
            self.identity, snapshot.arguments_payload_ref
        ) as opened:
            self.assertEqual(len(opened.arguments["blob"]), len(arguments["blob"]))
        _current, peak = tracemalloc.get_traced_memory()
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        tracemalloc.stop()

        self.assertLess(peak - baseline, 128 * 1024 * 1024)
        rss_scale = 1 if sys.platform == "darwin" else 1024
        self.assertLess(
            max(0, rss_after - rss_before) * rss_scale,
            128 * 1024 * 1024,
        )
        oversized = {"blob": arguments["blob"] + "x"}
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_too_large",
        ):
            await self.store.seal(replace(self.identity, action_id="action-2"), oversized)

    def test_binary_contract_rejects_header_length_trailing_and_ciphertext_drift(self) -> None:
        canonical = canonical_json_bytes({"query": "alice"})
        first = self.cipher.seal(self.identity, canonical)
        second = self.cipher.seal(self.identity, canonical)
        self.assertEqual(first[:8], b"MAFMPA1\0")
        self.assertEqual(struct.unpack(">H", first[8:10])[0], 1)
        self.assertEqual(len(first[10:22]), 12)
        self.assertNotEqual(first[10:22], second[10:22])
        self.assertEqual(self.cipher.unseal(self.identity, first)[1], {"query": "alice"})

        invalid_payloads = []
        invalid_payloads.append(b"X" + first[1:])
        invalid_payloads.append(first[:8] + b"\x00\x02" + first[10:])
        wrong_length = bytearray(first)
        wrong_length[22:30] = struct.pack(">Q", len(first))
        invalid_payloads.append(bytes(wrong_length))
        invalid_payloads.append(first + b"x")
        for payload in invalid_payloads:
            with self.subTest(payload_size=len(payload)):
                with self.assertRaisesRegex(
                    MCPPendingActionPayloadError,
                    "mcp_pending_action_payload_format_invalid",
                ):
                    self.cipher.unseal(self.identity, payload)

        tampered = bytearray(first)
        tampered[-1] ^= 1
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_decryption_failed",
        ):
            self.cipher.unseal(self.identity, bytes(tampered))

    def test_every_aad_identity_field_is_bound(self) -> None:
        canonical = canonical_json_bytes({"query": "alice"})
        payload = self.cipher.seal(self.identity, canonical)
        for item in fields(self.identity):
            original = getattr(self.identity, item.name)
            replacement = original + 1 if isinstance(original, int) else original + "-drift"
            with self.subTest(field=item.name):
                with self.assertRaisesRegex(
                    MCPPendingActionPayloadError,
                    "mcp_pending_action_payload_decryption_failed",
                ):
                    self.cipher.unseal(
                        replace(self.identity, **{item.name: replacement}),
                        payload,
                    )

    async def test_secure_read_rejects_symlink_hardlink_mode_owner_and_inode_drift(self) -> None:
        snapshot = await self.store.seal(self.identity, {"query": "alice"})
        path = self._path()

        os.chmod(path, 0o644)
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_file_unsafe",
        ):
            async with self.store.open_validated(
                self.identity, snapshot.arguments_payload_ref
            ):
                pass
        os.chmod(path, 0o600)

        hardlink = path.with_name("hardlink.bin")
        os.link(path, hardlink)
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_file_unsafe",
        ):
            async with self.store.open_validated(
                self.identity, snapshot.arguments_payload_ref
            ):
                pass
        hardlink.unlink()

        real_uid = os.getuid()
        with patch(
            "src.integrations.mcp.pending_action_payloads._validate_root_stat"
        ), patch(
            "src.integrations.mcp.pending_action_payloads.os.getuid",
            return_value=real_uid + 1,
        ):
            with self.assertRaisesRegex(
                MCPPendingActionPayloadError,
                "mcp_pending_action_payload_file_unsafe",
            ):
                async with self.store.open_validated(
                    self.identity, snapshot.arguments_payload_ref
                ):
                    pass

        async with self.store.open_validated(
            self.identity, snapshot.arguments_payload_ref
        ) as opened:
            replacement = path.with_name("replacement.bin")
            replacement.write_bytes(path.read_bytes())
            os.chmod(replacement, 0o600)
            os.replace(replacement, path)
            with self.assertRaisesRegex(
                MCPPendingActionPayloadError,
                "mcp_pending_action_payload_descriptor_conflict",
            ):
                self.store.revalidate(opened.snapshot)

        symlink_target = path.with_name("target.bin")
        os.replace(path, symlink_target)
        path.symlink_to(symlink_target)
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_file_unsafe",
        ):
            async with self.store.open_validated(
                self.identity, snapshot.arguments_payload_ref
            ):
                pass

    async def test_enospc_edquot_and_capacity_fail_before_authority_publish(self) -> None:
        for error_number in (os_error("ENOSPC"), os_error("EDQUOT")):
            identity = replace(self.identity, action_id=f"action-{error_number}")
            with self.subTest(errno=error_number), patch(
                "src.integrations.mcp.pending_action_payloads._publish_blob",
                side_effect=OSError(error_number, "full"),
            ):
                with self.assertRaisesRegex(
                    MCPPendingActionPayloadError,
                    "mcp_pending_action_payload_storage_exhausted",
                ):
                    await self.store.seal(identity, {"query": "alice"})
                self.assertFalse(self._path(identity).exists())

        unavailable = MCPPendingActionPayloadStore(
            Path(self.temporary.name) / "unavailable",
            cipher=self.cipher,
            disk_available=lambda _path: False,
        )
        with self.assertRaisesRegex(
            MCPPendingActionPayloadError,
            "mcp_pending_action_payload_capacity_unavailable",
        ):
            await unavailable.seal(
                replace(self.identity, action_id="capacity"), {"query": "alice"}
            )

    async def test_orphan_cleanup_requires_absence_from_action_authority_and_24_hours(self) -> None:
        referenced = await self.store.seal(self.identity, {"query": "keep"})
        orphan_identity = replace(self.identity, action_id="action-orphan")
        orphan = await self.store.seal(orphan_identity, {"query": "delete"})
        recent_identity = replace(self.identity, action_id="action-recent")
        await self.store.seal(recent_identity, {"query": "recent"})
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=25)).timestamp()
        os.utime(self._path(), (old, old), follow_symlinks=False)
        os.utime(self._path(orphan_identity), (old, old), follow_symlinks=False)

        removed = await self.store.cleanup_orphans(
            referenced_payload_refs={referenced.arguments_payload_ref},
            now=now,
        )

        self.assertEqual(removed, 1)
        self.assertTrue(self._path().exists())
        self.assertFalse(self._path(orphan_identity).exists())
        self.assertTrue(self._path(recent_identity).exists())
        self.assertNotEqual(
            referenced.arguments_payload_ref, orphan.arguments_payload_ref
        )

    async def test_consumed_payload_deletion_requires_terminal_evidence(self) -> None:
        snapshot = await self.store.seal(self.identity, {"query": "alice"})
        input_required = MCPPendingActionPayloadDeletionEvidence(
            action_id=self.identity.action_id,
            payload_ref=snapshot.arguments_payload_ref,
            action_status="consumed",
            call_status="input_required",
        )
        self.assertFalse(
            await self.store.delete_with_terminal_evidence(
                self.identity, snapshot, input_required
            )
        )
        self.assertTrue(self._path().exists())

        terminal = replace(
            input_required,
            call_status="completed",
            result_receipt_id="receipt-1",
        )
        self.assertTrue(
            await self.store.delete_with_terminal_evidence(
                self.identity, snapshot, terminal
            )
        )
        self.assertFalse(self._path().exists())

    async def test_crypto_gate_is_single_flight_and_wait_callback_runs(self) -> None:
        active = 0
        maximum = 0
        lock = threading.Lock()
        original = MCPPendingActionPayloadCipher._seal_validated

        def slow_seal(cipher, identity, canonical_arguments):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.06)
                return original(cipher, identity, canonical_arguments)
            finally:
                with lock:
                    active -= 1

        waits = 0

        async def on_wait() -> None:
            nonlocal waits
            waits += 1

        with patch.object(
            MCPPendingActionPayloadCipher, "_seal_validated", slow_seal
        ):
            await asyncio.gather(
                self.store.seal(
                    replace(self.identity, action_id="gate-1"),
                    {"query": "one"},
                    on_gate_wait=on_wait,
                ),
                self.store.seal(
                    replace(self.identity, action_id="gate-2"),
                    {"query": "two"},
                    on_gate_wait=on_wait,
                ),
            )

        self.assertEqual(maximum, 1)
        self.assertGreater(waits, 0)


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


def os_error(name: str) -> int:
    return int(getattr(errno, name))


if __name__ == "__main__":
    unittest.main()
