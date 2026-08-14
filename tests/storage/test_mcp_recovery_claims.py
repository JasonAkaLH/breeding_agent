from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

from src.core.enums import NodeStatus, TaskStatus
from src.core.models import (
    Conversation,
    Interrupt,
    MCPBranchRecord,
    MCPCallRecord,
    MCPRemoteTaskBinding,
    MCPSealedState,
    Task,
    TaskNode,
)
from src.integrations.mcp.credentials import MCPRecoveryCallContext, MCPRecoveryService
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase
from tests.master_key_support import recovery_cipher


class MCPRecoveryClaimsTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        self.now = datetime(2026, 8, 12, 12, 0, 0)

    def _binding(self, ref: str = "remote-a", *, call_ref: str = "call-a") -> MCPRemoteTaskBinding:
        return MCPRemoteTaskBinding(
            safe_remote_task_ref=ref,
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            call_ref=call_ref,
            server_id="server-a",
            protocol_version="2026-07-28",
            remote_task_ciphertext=b"ciphertext",
            remote_task_nonce=b"nonce",
            encryption_version=1,
            last_status="working",
            next_poll_at=self.now,
            created_at=self.now,
            updated_at=self.now,
        )

    def test_remote_binding_is_write_once_and_claim_updates_use_revision_cas(self) -> None:
        original = self._binding()
        self.assertEqual(asyncio.run(self.storage.save_mcp_remote_task_binding(original)), original)

        replay = replace(original, last_status="completed", terminal_at=self.now)
        self.assertEqual(asyncio.run(self.storage.save_mcp_remote_task_binding(replay)), original)
        with self.assertRaisesRegex(ValueError, "immutable"):
            asyncio.run(
                self.storage.save_mcp_remote_task_binding(
                    replace(original, remote_task_ciphertext=b"different")
                )
            )

        lease_one = self.now + timedelta(seconds=30)
        claimed = asyncio.run(
            self.storage.claim_due_mcp_remote_task_bindings(
                claim_owner="worker-a",
                claim_token="token-a",
                now=self.now,
                lease_expires_at=lease_one,
                limit=10,
            )
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(
            (claimed[0].claim_owner, claimed[0].claim_token, claimed[0].lease_expires_at, claimed[0].revision),
            ("worker-a", "token-a", lease_one, 1),
        )
        self.assertEqual(
            asyncio.run(
                self.storage.claim_due_mcp_remote_task_bindings(
                    claim_owner="worker-b",
                    claim_token="token-b",
                    now=self.now,
                    lease_expires_at=lease_one,
                )
            ),
            [],
        )

        lease_two = self.now + timedelta(seconds=60)
        self.assertIsNone(
            asyncio.run(
                self.storage.renew_mcp_remote_task_binding_claim(
                    "alice",
                    "task-a",
                    "remote-a",
                    claim_owner="worker-a",
                    claim_token="wrong-token",
                    expected_revision=1,
                    lease_expires_at=lease_two,
                    updated_at=self.now + timedelta(seconds=1),
                )
            )
        )
        renewed = asyncio.run(
            self.storage.renew_mcp_remote_task_binding_claim(
                "alice",
                "task-a",
                "remote-a",
                claim_owner="worker-a",
                claim_token="token-a",
                expected_revision=1,
                lease_expires_at=lease_two,
                updated_at=self.now + timedelta(seconds=1),
            )
        )
        self.assertEqual((renewed.lease_expires_at, renewed.revision), (lease_two, 2))

        self.assertIsNone(
            asyncio.run(
                self.storage.update_mcp_remote_task_binding_status(
                    "alice",
                    "task-a",
                    "remote-a",
                    claim_owner="worker-a",
                    claim_token="token-a",
                    expected_revision=1,
                    last_status="input_required",
                    next_poll_at=None,
                    updated_at=self.now + timedelta(seconds=2),
                )
            )
        )
        updated = asyncio.run(
            self.storage.update_mcp_remote_task_binding_status(
                "alice",
                "task-a",
                "remote-a",
                claim_owner="worker-a",
                claim_token="token-a",
                expected_revision=2,
                last_status="input_required",
                next_poll_at=None,
                updated_at=self.now + timedelta(seconds=2),
            )
        )
        self.assertEqual((updated.last_status, updated.revision), ("input_required", 3))

        released = asyncio.run(
            self.storage.release_mcp_remote_task_binding_claim(
                "alice",
                "task-a",
                "remote-a",
                claim_owner="worker-a",
                claim_token="token-a",
                expected_revision=3,
                updated_at=self.now + timedelta(seconds=3),
            )
        )
        self.assertEqual(
            (released.claim_owner, released.claim_token, released.lease_expires_at, released.revision),
            (None, None, None, 4),
        )

    def test_claim_is_exclusive_and_expired_lease_can_be_taken_over(self) -> None:
        asyncio.run(self.storage.save_mcp_remote_task_binding(self._binding()))

        async def _claim(owner: str, token: str) -> list[MCPRemoteTaskBinding]:
            return await self.storage.claim_due_mcp_remote_task_bindings(
                claim_owner=owner,
                claim_token=token,
                now=self.now,
                lease_expires_at=self.now + timedelta(seconds=5),
                limit=1,
            )

        async def _race() -> tuple[list[MCPRemoteTaskBinding], list[MCPRemoteTaskBinding]]:
            return await asyncio.gather(_claim("worker-a", "token-a"), _claim("worker-b", "token-b"))

        first, second = asyncio.run(_race())
        self.assertEqual(sorted((len(first), len(second))), [0, 1])

        takeover = asyncio.run(
            self.storage.claim_due_mcp_remote_task_bindings(
                claim_owner="worker-c",
                claim_token="token-c",
                now=self.now + timedelta(seconds=6),
                lease_expires_at=self.now + timedelta(seconds=30),
                limit=1,
            )
        )
        self.assertEqual(len(takeover), 1)
        self.assertEqual((takeover[0].claim_owner, takeover[0].claim_token), ("worker-c", "token-c"))

        terminal = asyncio.run(
            self.storage.update_mcp_remote_task_binding_status(
                "alice",
                "task-a",
                "remote-a",
                claim_owner="worker-c",
                claim_token="token-c",
                expected_revision=takeover[0].revision,
                last_status="completed",
                next_poll_at=None,
                updated_at=self.now + timedelta(seconds=7),
                terminal_at=self.now + timedelta(seconds=7),
            )
        )
        self.assertEqual(terminal.last_status, "completed")
        self.assertIsNotNone(terminal.terminal_at)
        self.assertIsNone(terminal.claim_owner)
        self.assertEqual(
            asyncio.run(
                self.storage.claim_due_mcp_remote_task_bindings(
                    claim_owner="worker-d",
                    claim_token="token-d",
                    now=self.now + timedelta(minutes=1),
                    lease_expires_at=self.now + timedelta(minutes=2),
                )
            ),
            [],
        )

    def test_terminal_binding_update_atomically_finishes_matching_call_and_branch(self) -> None:
        call = self._reserve_dispatched_call("remote-terminal")
        binding = replace(
            self._binding("remote-terminal", call_ref=call.call_ref),
            task_id=call.task_id,
            node_id=call.node_id,
        )
        asyncio.run(self.storage.save_mcp_remote_task_binding(binding))
        claimed = asyncio.run(
            self.storage.claim_due_mcp_remote_task_bindings(
                claim_owner="worker-a",
                claim_token="token-a",
                now=self.now,
                lease_expires_at=self.now + timedelta(seconds=30),
            )
        )[0]

        finished = asyncio.run(
            self.storage.finish_mcp_remote_task_binding(
                binding.owner_user_id,
                binding.task_id,
                binding.safe_remote_task_ref,
                claim_owner="worker-a",
                claim_token="token-a",
                expected_revision=claimed.revision,
                remote_status="unknown",
                call_status="unknown",
                terminal_at=self.now + timedelta(seconds=1),
                safe_error_code="execution_status_unknown",
            )
        )

        self.assertEqual(finished.last_status, "unknown")
        self.assertIsNotNone(finished.terminal_at)
        self.assertIsNone(finished.claim_owner)
        stored_call = asyncio.run(
            self.storage.get_mcp_call_record(call.owner_user_id, call.task_id, call.call_ref)
        )
        self.assertEqual(
            (stored_call.status, stored_call.safe_error_code),
            ("unknown", "execution_status_unknown"),
        )
        self.assertIsNotNone(stored_call.terminal_at)
        branch = asyncio.run(
            self.storage.get_mcp_branch_record(call.owner_user_id, call.task_id, call.branch_id)
        )
        self.assertIsNone(branch.active_call_ref)

    def test_running_continuation_abandonment_remains_retryable_until_converged(self) -> None:
        call = self._reserve_dispatched_call("continuation-abandon")
        binding = replace(
            self._binding("remote-continuation-abandon", call_ref=call.call_ref),
            task_id=call.task_id,
            node_id=call.node_id,
        )
        asyncio.run(self.storage.save_mcp_remote_task_binding(binding))
        binding_claim = asyncio.run(
            self.storage.claim_due_mcp_remote_task_bindings(
                claim_owner="poller",
                claim_token="poll-token",
                now=self.now,
                lease_expires_at=self.now + timedelta(seconds=30),
            )
        )[0]
        asyncio.run(
            self.storage.finish_mcp_remote_task_binding(
                binding.owner_user_id,
                binding.task_id,
                binding.safe_remote_task_ref,
                claim_owner="poller",
                claim_token="poll-token",
                expected_revision=binding_claim.revision,
                remote_status="completed",
                call_status="completed",
                terminal_at=self.now + timedelta(seconds=1),
                result_ref="result-ref",
            )
        )
        delivery = asyncio.run(
            self.storage.claim_mcp_remote_task_outbox(
                claim_owner="recovery",
                claim_token="delivery-token",
                now=self.now + timedelta(seconds=1),
                lease_expires_at=self.now + timedelta(seconds=31),
            )
        )[0]
        asyncio.run(
            self.storage.save_conversation(
                Conversation(f"conv-{call.task_id}", call.owner_user_id)
            )
        )
        asyncio.run(
            self.storage.save_task(
                Task(
                    call.task_id,
                    f"conv-{call.task_id}",
                    f"message-{call.task_id}",
                    status=TaskStatus.RUNNING,
                )
            )
        )
        asyncio.run(
            self.storage.save_task_node(
                TaskNode(
                    call.node_id,
                    call.task_id,
                    "mcp.dispatch",
                    status=NodeStatus.WAITING_FOR_DEPENDENCY,
                )
            )
        )
        applied = asyncio.run(
            self.storage.apply_mcp_remote_task_continuation(
                delivery.outbox_id,
                claim_owner="recovery",
                claim_token="delivery-token",
                expected_revision=delivery.revision,
                updated_at=self.now + timedelta(seconds=1),
            )
        )
        admitted = asyncio.run(
            self.storage.admit_mcp_remote_task_continuation(
                applied.outbox_id,
                claim_owner="recovery",
                claim_token="delivery-token",
                expected_revision=applied.revision,
                admitted_at=self.now + timedelta(seconds=2),
            )
        )
        command = asyncio.run(
            self.storage.claim_mcp_remote_task_continuations(
                claim_owner="runtime",
                claim_token="command-token",
                now=self.now + timedelta(seconds=2),
                lease_expires_at=self.now + timedelta(seconds=3),
            )
        )[0]
        running = asyncio.run(
            self.storage.begin_mcp_remote_task_continuation(
                admitted.outbox_id,
                claim_owner="runtime",
                claim_token="command-token",
                expected_revision=command.continuation_revision,
                started_at=self.now + timedelta(seconds=2),
            )
        )
        first = asyncio.run(
            self.storage.abandon_expired_mcp_remote_task_continuations(
                now=self.now + timedelta(seconds=4)
            )
        )[0]
        self.assertEqual(first.continuation_status, "abandoning")
        self.assertIsNone(first.continuation_dispatched_at)

        retry = asyncio.run(
            self.storage.abandon_expired_mcp_remote_task_continuations(
                now=self.now + timedelta(seconds=5)
            )
        )[0]
        self.assertEqual(retry.outbox_id, running.outbox_id)
        completed = asyncio.run(
            self.storage.complete_abandoned_mcp_remote_task_continuation(
                retry.outbox_id,
                expected_revision=retry.continuation_revision,
                completed_at=self.now + timedelta(seconds=5),
            )
        )
        self.assertEqual(completed.continuation_status, "failed")
        self.assertIsNotNone(completed.continuation_dispatched_at)

    def test_initial_terminal_create_is_due_only_after_waiting_publication(self) -> None:
        recovery = MCPRecoveryService(
            self.storage,
            recovery_cipher(b"i" * 32),
            now_fn=lambda: self.now,
        )

        safe_refs: list[str] = []
        for status in ("completed", "failed", "cancelled", "unknown"):
            with self.subTest(status=status):
                call = self._reserve_dispatched_call(f"immediate-{status}")
                safe_ref = f"remote-immediate-{status}"
                safe_refs.append(safe_ref)
                asyncio.run(
                    recovery.save_remote_task(
                        MCPRecoveryCallContext(
                            owner_user_id=call.owner_user_id,
                            task_id=call.task_id,
                            node_id=call.node_id,
                            call_ref=call.call_ref,
                        ),
                        server_id=call.server_id,
                        protocol_version="2026-07-28",
                        safe_remote_task_ref=safe_ref,
                        remote_task_id=f"raw-{status}",
                        status=status,
                        poll_interval_ms=0,
                    )
                )

                binding = asyncio.run(
                    self.storage.get_mcp_remote_task_binding(
                        call.owner_user_id, call.task_id, safe_ref
                    )
                )
                self.assertEqual(binding.last_status, status)
                self.assertIsNone(binding.terminal_at)
                self.assertIsNone(binding.next_poll_at)
                stored_call = asyncio.run(
                    self.storage.get_mcp_call_record(
                        call.owner_user_id, call.task_id, call.call_ref
                    )
                )
                self.assertEqual(stored_call.status, "active")
                self.assertIsNone(stored_call.safe_error_code)
                self.assertIsNone(stored_call.result_ref)
                self.assertIsNone(stored_call.terminal_at)
                branch = asyncio.run(
                    self.storage.get_mcp_branch_record(
                        call.owner_user_id, call.task_id, call.branch_id
                    )
                )
                self.assertEqual(branch.active_call_ref, call.call_ref)

                asyncio.run(
                    self.storage.save_conversation(
                        Conversation(f"conv-{call.task_id}", call.owner_user_id)
                    )
                )
                asyncio.run(
                    self.storage.save_task(
                        Task(call.task_id, f"conv-{call.task_id}", f"message-{call.task_id}")
                    )
                )
                asyncio.run(
                    self.storage.save_task_node(
                        TaskNode(
                            node_id=call.node_id,
                            task_id=call.task_id,
                            capability_id="mcp.dispatch",
                            status=NodeStatus.WAITING_FOR_DEPENDENCY,
                        )
                    )
                )
                published = asyncio.run(
                    self.storage.publish_mcp_remote_task_binding(
                        call.owner_user_id,
                        call.task_id,
                        safe_ref,
                        published_at=self.now,
                    )
                )
                self.assertEqual(published.next_poll_at, self.now)

        claimed = asyncio.run(
            self.storage.claim_due_mcp_remote_task_bindings(
                claim_owner="worker-after-restart",
                claim_token="token-after-restart",
                now=self.now + timedelta(seconds=1),
                lease_expires_at=self.now + timedelta(seconds=31),
            )
        )
        self.assertEqual(
            {binding.safe_remote_task_ref for binding in claimed}, set(safe_refs)
        )

    def _reserve_dispatched_call(self, suffix: str) -> MCPCallRecord:
        branch = MCPBranchRecord(
            branch_id=f"branch-{suffix}",
            owner_user_id="alice",
            task_id=f"task-{suffix}",
            node_id=f"node-{suffix}",
            status="ready",
            created_at=self.now,
            updated_at=self.now,
        )
        asyncio.run(self.storage.save_mcp_branch_record(branch))
        call = MCPCallRecord(
            call_ref=f"call-{suffix}",
            branch_id=branch.branch_id,
            owner_user_id=branch.owner_user_id,
            task_id=branch.task_id,
            node_id=branch.node_id,
            server_id="server-a",
            tool_name="lookup",
            status="active",
            call_sequence=1,
            arguments_sha256="args",
            server_security_version=1,
            input_schema_sha256="schema",
            protocol_version="2026-07-28",
            may_have_dispatched=True,
            created_at=self.now,
            updated_at=self.now,
        )
        self.assertTrue(asyncio.run(self.storage.reserve_mcp_call(call)))
        return call

    def test_startup_converges_orphan_sealed_and_ordinary_calls_to_unknown(self) -> None:
        ordinary = self._reserve_dispatched_call("ordinary")
        remote = self._reserve_dispatched_call("remote")
        sealed = self._reserve_dispatched_call("sealed")
        finished = self._reserve_dispatched_call("finished")
        asyncio.run(
            self.storage.save_mcp_remote_task_binding(
                replace(
                    self._binding("remote-task", call_ref=remote.call_ref),
                    task_id=remote.task_id,
                    node_id=remote.node_id,
                )
            )
        )
        asyncio.run(
            self.storage.save_mcp_sealed_state(
                MCPSealedState(
                    sealed_state_ref="sealed-state",
                    owner_user_id="alice",
                    task_id=sealed.task_id,
                    node_id=sealed.node_id,
                    call_ref=sealed.call_ref,
                    state_kind="mrtr_request_state",
                    ciphertext=b"sealed-ciphertext",
                    nonce=b"sealed-nonce",
                    encryption_version=1,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        )
        asyncio.run(self.storage.save_conversation(Conversation("conv-sealed", "alice")))
        asyncio.run(self.storage.save_task(Task(sealed.task_id, "conv-sealed", "msg-sealed")))
        asyncio.run(
            self.storage.save_task_node(
                TaskNode(
                    node_id=sealed.node_id,
                    task_id=sealed.task_id,
                    capability_id="mcp.dispatch",
                    status=NodeStatus.WAITING_FOR_INPUT,
                )
            )
        )
        asyncio.run(
            self.storage.save_interrupt(
                Interrupt(
                    interrupt_id="interrupt-sealed-open",
                    conversation_id="conv-sealed",
                    task_id=sealed.task_id,
                    node_id=sealed.node_id,
                    source_agent="mcp.dispatch",
                    source_message_id="msg-sealed",
                    question="input required",
                    reason_code="mcp_input_required",
                    required_fields={"sealed_request_state_ref": "sealed-state"},
                    created_at=self.now,
                )
            )
        )
        asyncio.run(
            self.storage.save_mcp_sealed_state(
                MCPSealedState(
                    sealed_state_ref="sealed-finished-state",
                    owner_user_id="alice",
                    task_id=finished.task_id,
                    node_id=finished.node_id,
                    call_ref=finished.call_ref,
                    state_kind="mrtr_request_state",
                    ciphertext=b"sealed-finished-ciphertext",
                    nonce=b"sealed-finished-nonce",
                    encryption_version=1,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        )
        asyncio.run(
            self.storage.finish_mcp_call(
                "alice",
                finished.task_id,
                finished.call_ref,
                status="completed",
                terminal_at=self.now + timedelta(seconds=1),
            )
        )

        converged = asyncio.run(
            self.storage.converge_dispatched_mcp_calls_to_unknown(
                now=self.now + timedelta(seconds=2), limit=100
            )
        )
        self.assertEqual(
            [call.call_ref for call in converged],
            [ordinary.call_ref, sealed.call_ref],
        )
        self.assertTrue(
            all(
                (call.status, call.safe_error_code)
                == ("unknown", "execution_status_unknown")
                for call in converged
            )
        )
        self.assertTrue(all(call.terminal_at is not None for call in converged))
        branch = asyncio.run(
            self.storage.get_mcp_branch_record("alice", ordinary.task_id, ordinary.branch_id)
        )
        self.assertIsNone(branch.active_call_ref)
        self.assertIsNone(
            asyncio.run(
                self.storage.get_mcp_call_record("alice", remote.task_id, remote.call_ref)
            ).terminal_at
        )
        self.assertIsNotNone(
            asyncio.run(
                self.storage.get_mcp_call_record("alice", sealed.task_id, sealed.call_ref)
            ).terminal_at
        )
        self.assertIsNotNone(
            asyncio.run(
                self.storage.get_mcp_sealed_state(
                    "alice", sealed.task_id, "sealed-state"
                )
            )
        )
        self.assertIsNone(
            asyncio.run(
                self.storage.get_mcp_sealed_state(
                    "alice", finished.task_id, "sealed-finished-state"
                )
            )
        )
        self.assertEqual(
            asyncio.run(
                self.storage.converge_dispatched_mcp_calls_to_unknown(
                    now=self.now + timedelta(seconds=3), limit=100
                )
            ),
            [],
        )

    def test_sealed_state_scope_and_ciphertext_are_write_once(self) -> None:
        original = MCPSealedState(
            sealed_state_ref="sealed-a",
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            call_ref="call-a",
            state_kind="mrtr_request_state",
            ciphertext=b"ciphertext",
            nonce=b"nonce",
            encryption_version=1,
            created_at=self.now,
            updated_at=self.now,
        )
        self.assertEqual(asyncio.run(self.storage.save_mcp_sealed_state(original)), original)
        self.assertEqual(asyncio.run(self.storage.save_mcp_sealed_state(original)), original)
        self.assertIsNone(asyncio.run(self.storage.get_mcp_sealed_state("bob", "task-a", "sealed-a")))
        self.assertEqual(
            asyncio.run(self.storage.get_mcp_sealed_state("alice", "task-a", "sealed-a")), original
        )
        for changed in (
            replace(original, task_id="task-b"),
            replace(original, ciphertext=b"different"),
            replace(original, nonce=b"different"),
            replace(original, encryption_version=2),
        ):
            with self.assertRaisesRegex(ValueError, "immutable"):
                asyncio.run(self.storage.save_mcp_sealed_state(changed))
