from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from src.api.submission_admission import (
    DurableSubmissionHandoff,
    PreparedAgentRecoveryContext,
    SubmissionPreparedAgentRecoveryLoader,
    SubmissionAdmissionCoordinator,
    SubmissionRecoveryError,
    SubmissionRecoveryStatus,
    submission_interrupt_handoff_id,
    submission_memory_event_id,
    _validated_prepared_content,
)
from src.core.models import (
    SubmissionAdmissionDisposition,
    SubmissionAdmissionHandle,
    SubmissionAdmissionPhase,
    SubmissionAdmissionResult,
    SubmissionAdmissionState,
    SubmissionAuthorityState,
    SubmissionClaimResult,
    SubmissionHandoffState,
    SubmissionPreparationReceipt,
    SubmissionPreparationReceiptComponent,
    SubmissionPreparationRecord,
    SubmissionPreparationState,
    SubmissionProjectionState,
    SubmissionRecoveryRecord,
)
from src.integrations.mcp.cp7_artifacts import mcp_no_server_intent_id
from src.integrations.agent_skills.public_profile import PublicSkillProfile
from src.orchestration.agent_loop.skill_activation import (
    build_canonical_skill_activation,
)
from src.orchestration.conversation_memory import (
    COMPRESSION_POLICY_VERSION,
    SUMMARY_VERSION,
    _stable_memory_summary_id,
)


class SubmissionAdmissionRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = _Clock(datetime(2026, 8, 26, tzinfo=timezone.utc))

    async def test_projection_backlog_closes_before_any_pure_computation(self) -> None:
        admission = _FakeAdmission([_record("1"), _record("2")], self.clock)
        callbacks = _Callbacks(admission)
        coordinator = self._coordinator(admission, _FakeReceipts(), callbacks)

        result = await coordinator.recover_pending()

        self.assertEqual(result.status, SubmissionRecoveryStatus.COMPLETED)
        self.assertEqual(result.recovered_count, 2)
        self.assertEqual(callbacks.compute_order[:3], ["route:1", "memory:1", "selector:1"])
        self.assertTrue(callbacks.computed_after_projection_closed)
        self.assertEqual(admission.handoff_acks, ["1", "2"])
        self.assertEqual(callbacks.wakeups, ["agent-run:task-1", "agent-run:task-2"])

    async def test_projected_sidecar_missing_sql_repairs_after_restart_with_fresh_claim(
        self,
    ) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        admission.fail_sql_projection_once_after_authority_ack = True
        receipts = _FakeReceipts()
        first_callbacks = _Callbacks(admission)
        first = self._coordinator(admission, receipts, first_callbacks)

        with self.assertRaisesRegex(RuntimeError, "sql_projection_crash"):
            await first.recover_pending()
        self.assertEqual(
            admission.entries[0].record.phase.projection_state,
            SubmissionProjectionState.PROJECTED,
        )
        self.assertEqual(
            admission.projection_ack_states, [SubmissionProjectionState.PENDING]
        )
        self.assertEqual(admission.sql_projection_writes, 0)
        self.assertEqual(first_callbacks.compute_order, [])
        first_handle = admission.claimed_handles[-1]

        self.clock.advance(seconds=31)
        second_callbacks = _Callbacks(admission)
        second = self._coordinator(admission, receipts, second_callbacks)
        result = await second.recover_pending()

        self.assertEqual(result.status, SubmissionRecoveryStatus.COMPLETED)
        self.assertNotEqual(admission.claimed_handles[-1], first_handle)
        self.assertEqual(
            admission.projection_ack_states,
            [
                SubmissionProjectionState.PENDING,
                SubmissionProjectionState.PROJECTED,
            ],
        )
        self.assertEqual(admission.sql_projection_writes, 1)
        self.assertEqual(admission.handoff_acks, ["1"])

    async def test_created_continues_directly_without_pending_claim_scan(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        receipts = _FakeReceipts()
        result = admission.admission_result(
            disposition=SubmissionAdmissionDisposition.CREATED
        )

        recovered = await self._coordinator(
            admission, receipts, callbacks
        ).continue_admitted(result)

        self.assertEqual(recovered.status, SubmissionRecoveryStatus.COMPLETED)
        self.assertEqual(recovered.recovered_count, 1)
        self.assertEqual(admission.claim_calls, 0)
        self.assertEqual(
            admission.projection_ack_states, [SubmissionProjectionState.PENDING]
        )
        self.assertEqual(receipts.route_settle_count, 1)
        self.assertEqual(
            receipts.generic_write_components,
            [
                SubmissionPreparationReceiptComponent.MEMORY_CONTEXT,
                SubmissionPreparationReceiptComponent.SELECTOR_DECISION,
            ],
        )
        self.assertEqual(admission.handoff_acks, ["1"])

    async def test_sql_style_replay_handle_assists_projected_record(self) -> None:
        base = _record("1")
        projected = replace(
            base,
            phase=replace(
                base.phase,
                projection_state=SubmissionProjectionState.PROJECTED,
            ),
        )
        admission = _FakeAdmission([projected], self.clock)
        result = admission.admission_result(
            disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY
        )

        recovered = await self._coordinator(
            admission, _FakeReceipts(), _Callbacks(admission)
        ).continue_admitted(result)

        self.assertEqual(recovered.recovered_count, 1)
        self.assertEqual(admission.claim_calls, 0)
        self.assertEqual(admission.projection_ack_states, [])
        self.assertEqual(admission.sql_projection_writes, 0)
        self.assertEqual(admission.handoff_acks, ["1"])

    async def test_handed_off_replay_fast_returns_and_retries_agent_wakeup(self) -> None:
        base = _record("1")
        handed_off = replace(
            base,
            phase=replace(
                base.phase,
                projection_state=SubmissionProjectionState.PROJECTED,
                handoff_state=SubmissionHandoffState.HANDED_OFF,
            ),
        )
        admission = _FakeAdmission([handed_off], self.clock)
        callbacks = _Callbacks(admission)
        result = admission.admission_result(
            disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
            with_handle=True,
        )

        recovered = await self._coordinator(
            admission, _FakeReceipts(), callbacks
        ).continue_admitted(result)

        self.assertEqual(recovered.status, SubmissionRecoveryStatus.COMPLETED)
        self.assertEqual(recovered.recovered_count, 0)
        self.assertEqual(admission.claim_calls, 0)
        self.assertEqual(admission.projection_writes, 0)
        self.assertEqual(callbacks.compute_order, [])
        self.assertEqual(callbacks.materialized, [])
        self.assertEqual(callbacks.wakeups, ["agent-run:task-1"])

    async def test_pending_no_handle_replay_claims_and_continues_same_admission(
        self,
    ) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        result = admission.admission_result(
            disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
            with_handle=False,
        )

        async def reclaim_exact(_record):
            return admission.admission_result(
                disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY
            )

        recovery = asyncio.create_task(
            self._coordinator(
                admission, _FakeReceipts(), callbacks
            ).continue_admitted(result, reclaim_exact=reclaim_exact)
        )
        await asyncio.sleep(0)
        self.assertFalse(recovery.done())
        self.clock.advance(seconds=30)
        recovered = await recovery

        self.assertEqual(recovered.recovered_count, 1)
        self.assertEqual(admission.claim_calls, 0)
        self.assertEqual(admission.handoff_acks, ["1"])

    async def test_pending_no_handle_replay_waits_for_foreign_claim_expiry(
        self,
    ) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        admission.entries[0].claim_expires_at = self.clock.now() + timedelta(seconds=9)
        result = admission.admission_result(
            disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
            with_handle=False,
        )
        reclaim_calls = 0

        async def reclaim_exact(_record):
            nonlocal reclaim_calls
            reclaim_calls += 1
            if reclaim_calls == 1:
                return result
            return admission.admission_result(
                disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY
            )

        recovery = asyncio.create_task(
            self._coordinator(
                admission, _FakeReceipts(), _Callbacks(admission)
            ).continue_admitted(result, reclaim_exact=reclaim_exact)
        )

        await asyncio.sleep(0)
        self.assertFalse(recovery.done())
        self.clock.advance(seconds=30)
        await asyncio.sleep(0)
        self.assertFalse(recovery.done())
        self.clock.advance(seconds=30)
        recovered = await recovery

        self.assertEqual(recovered.recovered_count, 1)
        self.assertEqual(admission.claim_calls, 0)
        self.assertEqual(reclaim_calls, 2)
        self.assertEqual(admission.handoff_acks, ["1"])

    async def test_pending_no_handle_replay_never_range_claims_adjacent_admission(
        self,
    ) -> None:
        first = _record("1")
        target = replace(_record("2"), created_at=first.created_at)
        admission = _FakeAdmission([first, target], self.clock)
        callbacks = _Callbacks(admission)
        result = SubmissionAdmissionResult(
            disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
            conversation_id=target.conversation_id,
            message_id=target.message_id,
            task_id=target.task_id,
            message_created_at=target.created_at,
            task_created_at=target.created_at,
            phase=target.phase,
            record=target,
            handle=None,
        )

        async def reclaim_exact(_record):
            entry = admission.entries[1]
            handle = admission._rotate(  # noqa: SLF001
                entry, self.clock.now() + timedelta(seconds=30)
            )
            return replace(result, handle=handle)

        recovery = asyncio.create_task(
            self._coordinator(
                admission, _FakeReceipts(), callbacks
            ).continue_admitted(result, reclaim_exact=reclaim_exact)
        )
        await asyncio.sleep(0)
        self.clock.advance(seconds=30)
        recovered = await recovery

        self.assertEqual(recovered.recovered_count, 1)
        self.assertEqual(admission.claim_calls, 0)
        self.assertIsNone(admission.entries[0].active_handle)
        self.assertEqual(admission.handoff_acks, ["2"])

    async def test_pending_no_handle_replay_returns_only_after_concurrent_handoff(
        self,
    ) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        result = admission.admission_result(
            disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
            with_handle=False,
        )
        admission.entries[0].record = replace(
            admission.entries[0].record,
            phase=replace(
                admission.entries[0].record.phase,
                projection_state=SubmissionProjectionState.PROJECTED,
                preparation_state=SubmissionPreparationState.PREPARED,
                handoff_state=SubmissionHandoffState.HANDED_OFF,
            ),
        )

        async def reclaim_exact(_record):
            return admission.admission_result(
                disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
                with_handle=False,
            )

        recovery = asyncio.create_task(
            self._coordinator(
                admission, _FakeReceipts(), callbacks
            ).continue_admitted(result, reclaim_exact=reclaim_exact)
        )
        await asyncio.sleep(0)
        self.clock.advance(seconds=30)
        recovered = await recovery

        self.assertEqual(recovered.recovered_count, 0)
        self.assertEqual(admission.claim_calls, 0)
        self.assertEqual(callbacks.compute_order, [])
        self.assertEqual(callbacks.materialized, [])
        self.assertEqual(callbacks.wakeups, ["agent-run:task-1"])

    async def test_wakeup_failure_after_ack_is_retried_by_handed_off_replay(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        receipts = _FakeReceipts()
        callbacks = _Callbacks(admission)
        original_wakeup = callbacks.wakeup_agent
        wakeup_attempts = 0

        async def fail_once(record, identity):
            nonlocal wakeup_attempts
            wakeup_attempts += 1
            if wakeup_attempts == 1:
                raise RuntimeError("wakeup_failed")
            await original_wakeup(record, identity)

        callbacks.wakeup_agent = fail_once
        with self.assertRaisesRegex(RuntimeError, "wakeup_failed"):
            await self._coordinator(
                admission, receipts, callbacks
            ).continue_admitted(
                admission.admission_result(
                    disposition=SubmissionAdmissionDisposition.CREATED
                )
            )

        handed_off = admission.entries[0].record
        replay = SubmissionAdmissionResult(
            disposition=SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
            conversation_id=handed_off.conversation_id,
            message_id=handed_off.message_id,
            task_id=handed_off.task_id,
            message_created_at=handed_off.created_at,
            task_created_at=handed_off.created_at,
            phase=handed_off.phase,
            record=handed_off,
            handle=None,
        )
        recovered = await self._coordinator(
            admission, receipts, callbacks
        ).continue_admitted(replay)

        self.assertEqual(recovered.recovered_count, 0)
        self.assertEqual(wakeup_attempts, 2)
        self.assertEqual(callbacks.wakeups, ["agent-run:task-1"])

    async def test_live_durable_callback_failure_stops_keeper_and_remains_recoverable(
        self,
    ) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        receipts = _FakeReceipts()
        callbacks = _Callbacks(admission)
        callbacks.fail_handoff_once = True
        result = admission.admission_result(
            disposition=SubmissionAdmissionDisposition.CREATED
        )

        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(
                admission, receipts, callbacks
            ).continue_admitted(result)

        renews = admission.renew_count
        self.clock.advance(seconds=31)
        await asyncio.sleep(0)
        self.assertEqual(admission.renew_count, renews)
        replay_callbacks = _Callbacks(admission)
        recovered = await self._coordinator(
            admission, receipts, replay_callbacks
        ).recover_pending()

        self.assertEqual(recovered.recovered_count, 1)
        self.assertEqual(admission.handoff_acks, ["1"])
        self.assertEqual(replay_callbacks.compute_order, [])

    async def test_partial_receipt_resumes_without_recomputing_first_component(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        receipts = _FakeReceipts()
        callbacks = _Callbacks(admission)
        callbacks.fail_memory_once = True
        coordinator = self._coordinator(admission, receipts, callbacks)

        with self.assertRaisesRegex(RuntimeError, "memory_compute_crash"):
            await coordinator.recover_pending()
        self.assertIsNotNone(receipts.rows["task-1"].route_decision)
        self.clock.advance(seconds=31)
        await coordinator.recover_pending()

        self.assertEqual(callbacks.compute_order.count("route:1"), 1)
        self.assertEqual(callbacks.compute_order.count("memory:1"), 2)
        self.assertEqual(callbacks.compute_order.count("selector:1"), 1)
        self.assertEqual(receipts.route_settle_count, 1)
        self.assertNotIn(
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            receipts.generic_write_components,
        )

    async def test_route_settle_exact_conflict_stops_before_generic_writes(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        receipts = _FakeReceipts()
        receipts.route_conflict_on_settle = _canonical(_no_server_route())
        callbacks = _Callbacks(admission)

        with self.assertRaisesRegex(
            RuntimeError, "submission_preparation_receipt_conflict"
        ):
            await self._coordinator(admission, receipts, callbacks).recover_pending()

        self.assertEqual(receipts.route_settle_count, 1)
        self.assertEqual(receipts.generic_write_components, [])
        self.assertEqual(callbacks.compute_order, ["route:1"])

    async def test_prepare_winner_is_reused_without_recomputing_current_inputs(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        receipts = _FakeReceipts()
        first_callbacks = _Callbacks(admission)
        first_callbacks.fail_handoff_once = True
        first = self._coordinator(admission, receipts, first_callbacks)

        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await first.recover_pending()
        prepared = admission.entries[0].record.prepared_execution
        self.assertIsNotNone(prepared)

        self.clock.advance(seconds=31)
        second_callbacks = _Callbacks(admission)
        await self._coordinator(admission, receipts, second_callbacks).recover_pending()
        self.assertEqual(second_callbacks.compute_order, [])
        self.assertEqual(receipts.route_settle_count, 1)
        self.assertEqual(admission.entries[0].record.prepared_execution, prepared)
        self.assertEqual(second_callbacks.materialized, ["route:1", "memory:1", "selector:1"])

    async def test_claim_renew_rotates_handle_and_rejects_stale_owner(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        await self._coordinator(admission, _FakeReceipts(), callbacks).recover_pending()

        self.assertGreaterEqual(admission.renew_count, 3)
        self.assertEqual(admission.stale_handle_uses, 0)
        with self.assertRaisesRegex(RuntimeError, "stale_claim"):
            await admission.acknowledge_submission_handoff(admission.first_ack_request)

    async def test_held_head_returns_blocked_without_sleep_or_callbacks(self) -> None:
        admission = _FakeAdmission([_record("1"), _record("2")], self.clock)
        admission.entries[0].claim_expires_at = self.clock.now() + timedelta(seconds=9)
        callbacks = _Callbacks(admission)

        recovery = asyncio.create_task(
            self._coordinator(admission, _FakeReceipts(), callbacks).recover_pending()
        )
        await asyncio.sleep(0)
        self.assertFalse(recovery.done())
        self.clock.advance(seconds=9)
        result = await recovery

        self.assertEqual(result.status, SubmissionRecoveryStatus.COMPLETED)
        self.assertEqual(result.pending_count, 0)
        self.assertEqual(len(callbacks.compute_order), 6)

    async def test_stable_cursor_and_fixed_limit_fail_closed(self) -> None:
        admission = _FakeAdmission([_record("1"), _record("2")], self.clock)
        callbacks = _Callbacks(admission)
        coordinator = self._coordinator(
            admission, _FakeReceipts(), callbacks, recovery_limit=1
        )
        with self.assertRaisesRegex(SubmissionRecoveryError, "backlog_exceeded"):
            await coordinator.recover_pending()
        self.assertEqual(admission.projection_writes, 0)
        self.assertEqual(callbacks.compute_order, [])

    async def test_existing_exact_handoff_only_acks_and_identity_drift_fails(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        callbacks.existing_agent_identity = "agent-run:task-1"
        await self._coordinator(admission, _FakeReceipts(), callbacks).recover_pending()
        self.assertEqual(callbacks.agent_creates, 0)
        self.assertEqual(admission.handoff_acks, ["1"])

        drift_admission = _FakeAdmission([_record("2")], self.clock)
        drift_callbacks = _Callbacks(drift_admission)
        drift_callbacks.existing_agent_identity = "agent-run:wrong"
        with self.assertRaisesRegex(SubmissionRecoveryError, "identity_drift"):
            await self._coordinator(
                drift_admission, _FakeReceipts(), drift_callbacks
            ).recover_pending()
        self.assertEqual(drift_admission.handoff_acks, [])

    async def test_forbidden_continuation_and_oversize_prepared_fail_closed(self) -> None:
        forbidden = _record("1", execution_metadata_override={"api_key": "secret"})
        admission = _FakeAdmission([forbidden], self.clock)
        with self.assertRaises((RuntimeError, ValueError)):
            await self._coordinator(
                admission, _FakeReceipts(), _Callbacks(admission)
            ).recover_pending()

        oversize = _record("2")
        oversize_bytes = b"{" + b"x" * (128 * 1024) + b"}"
        oversize = replace(
            oversize,
            prepared_execution=oversize_bytes,
            prepared_execution_sha256=hashlib.sha256(
                b"maf.submission.prepared_execution.v1\0" + oversize_bytes
            ).hexdigest(),
            phase=replace(
                oversize.phase,
                projection_state=SubmissionProjectionState.PROJECTED,
                preparation_state=SubmissionPreparationState.PREPARED,
            ),
        )
        oversize_admission = _FakeAdmission([oversize], self.clock)
        with self.assertRaisesRegex(SubmissionRecoveryError, "oversize"):
            await self._coordinator(
                oversize_admission,
                _FakeReceipts(),
                _Callbacks(oversize_admission),
            ).recover_pending()

    async def test_zero_capability_replay_keeps_null_and_one_agent_identity(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        receipts = _FakeReceipts()
        callbacks = _Callbacks(admission)
        callbacks.fail_handoff_once = True
        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(admission, receipts, callbacks).recover_pending()
        prepared = json.loads(admission.entries[0].record.prepared_execution or b"{}")
        self.assertIsNone(prepared["requested_capability_id"])

        self.clock.advance(seconds=31)
        replay_callbacks = _Callbacks(admission)
        replay_callbacks.existing_agent_identity = "agent-run:task-1"
        await self._coordinator(admission, receipts, replay_callbacks).recover_pending()
        self.assertEqual(replay_callbacks.agent_creates, 0)
        self.assertEqual(admission.handoff_acks, ["1"])

    async def test_new_prepared_writer_uses_v2_domain_and_closed_auto_authority(self) -> None:
        admission = _FakeAdmission([_record("47")], self.clock)
        callbacks = _Callbacks(admission)
        callbacks.fail_handoff_once = True
        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(
                admission,
                _FakeReceipts(),
                callbacks,
            ).recover_pending()

        prepared = admission.entries[0].record.prepared_execution
        prepared_sha256 = admission.entries[0].record.prepared_execution_sha256
        assert prepared is not None
        value = json.loads(prepared)
        self.assertEqual(value["schema"], "maf.submission.prepared_execution.v2")
        self.assertEqual(value["routing_mode"], "auto")
        self.assertIsNone(value["skill_activation"])
        self.assertIsNone(value["initial_required_tool_name"])
        self.assertEqual(
            prepared_sha256,
            hashlib.sha256(
                b"maf.submission.prepared_execution.v2\0" + prepared
            ).hexdigest(),
        )

    async def test_v2_hint_requires_exact_canonical_activation_binding(self) -> None:
        admission = _FakeAdmission([_record("50")], self.clock)
        callbacks = _Callbacks(admission)
        callbacks.fail_handoff_once = True
        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(
                admission,
                _FakeReceipts(),
                callbacks,
            ).recover_pending()
        prepared = admission.entries[0].record.prepared_execution
        assert prepared is not None
        value = json.loads(prepared)
        value["routing_mode"] = "hint"
        value["requested_capability_id"] = "skill.report"
        value["bundle_revisions"]["skill_bundle_revision"] = "revision-1"
        activation = build_canonical_skill_activation(
            binding_mode="hint",
            profile=PublicSkillProfile(
                capability_id="skill.report",
                name="report",
                display_name="Report",
                description="safe",
                triggers=(),
            ),
            pinned_bundle_revision="revision-1",
            resolved_bundle_revision="revision-1",
        )
        value["skill_activation"] = {
            "payload": activation.payload_json,
            "payload_sha256": activation.payload_sha256,
        }
        hint_prepared = _canonical(value)

        validated = _validated_prepared_content(
            hint_prepared,
            conversation_id="conversation-50",
            message_id="50",
            task_id="task-50",
        )
        self.assertEqual(validated["routing_mode"], "hint")

        value["skill_activation"]["extra"] = True
        with self.assertRaisesRegex(SubmissionRecoveryError, "activation_invalid"):
            _validated_prepared_content(
                _canonical(value),
                conversation_id="conversation-50",
                message_id="50",
                task_id="task-50",
            )
    async def test_authority_receipt_is_pinned_on_found_and_empty_claims(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        admission.finalization_receipt = "e" * 64
        with self.assertRaisesRegex(SubmissionRecoveryError, "receipt_mismatch"):
            await self._coordinator(
                admission, _FakeReceipts(), _Callbacks(admission)
            ).recover_pending()

        empty = _FakeAdmission([], self.clock)
        empty.finalization_receipt = None
        with self.assertRaisesRegex(SubmissionRecoveryError, "receipt_mismatch"):
            await self._coordinator(
                empty, _FakeReceipts(), _Callbacks(empty)
            ).recover_pending()

    async def test_sql_authority_accepts_finalized_claim_without_sidecar_receipt(
        self,
    ) -> None:
        admission = _FakeAdmission([], self.clock)
        admission.finalization_receipt = None
        coordinator = SubmissionAdmissionCoordinator(
            admission=admission,
            receipts=_FakeReceipts(),
            callbacks=_Callbacks(admission),
            claim_owner="sql-worker",
            now=self.clock.now,
            wait_until=self.clock.wait_until,
            expected_finalization_receipt_sha256=None,
        )

        result = await coordinator.recover_pending()

        self.assertEqual(result.recovered_count, 0)
        self.assertEqual(result.pending_count, 0)

    async def test_claimed_handles_renew_while_later_head_is_held(self) -> None:
        admission = _FakeAdmission([_record("1"), _record("2")], self.clock)
        admission.entries[1].claim_expires_at = self.clock.now() + timedelta(seconds=25)
        callbacks = _Callbacks(admission)
        recovery = asyncio.create_task(
            self._coordinator(admission, _FakeReceipts(), callbacks).recover_pending()
        )
        for _ in range(10):
            if len(self.clock.waiters) >= 2:
                break
            await asyncio.sleep(0)
        self.assertGreaterEqual(len(self.clock.waiters), 2)
        self.clock.advance(seconds=11)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertGreaterEqual(admission.renew_count, 1)
        self.clock.advance(seconds=14)
        result = await recovery
        self.assertEqual(result.pending_count, 0)
        self.assertEqual(admission.handoff_acks, ["1", "2"])

    async def test_claim_renews_during_long_pure_compute(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        callbacks.block_memory = True
        recovery = asyncio.create_task(
            self._coordinator(admission, _FakeReceipts(), callbacks).recover_pending()
        )
        await callbacks.memory_started.wait()
        renews_before = admission.renew_count
        self.clock.advance(seconds=11)
        await asyncio.sleep(0)
        self.assertGreater(admission.renew_count, renews_before)
        callbacks.memory_release.set()
        await recovery

    async def test_root_memory_and_pending_execution_text_are_digest_bound(self) -> None:
        root_admission = _FakeAdmission([_record("1")], self.clock)
        root_callbacks = _Callbacks(root_admission)
        root_callbacks.fail_handoff_once = True
        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(
                root_admission, _FakeReceipts(), root_callbacks
            ).recover_pending()
        root_prepared = json.loads(root_admission.entries[0].record.prepared_execution)
        self.assertEqual(
            root_prepared["execution_text_sha256"],
            hashlib.sha256(b"hello-1").hexdigest(),
        )
        self.assertEqual(root_callbacks.agent_contexts[0].current_user_input, "hello-1")
        self.assertEqual(
            hashlib.sha256(
                root_callbacks.agent_contexts[0].current_user_input.encode()
            ).hexdigest(),
            root_prepared["execution_text_sha256"],
        )

        memory_admission = _FakeAdmission([_record("2")], self.clock)
        memory_callbacks = _Callbacks(memory_admission)
        nested_memory = _memory("resolved memory text")
        nested_memory["prompt_payload"]["recent_messages"] = [
            {
                "message_id": "history-1",
                "role": "user",
                "content": "history",
                "task_id": "history-task",
                "kind": "message",
                "created_at": None,
            }
        ]
        nested_memory["prompt_payload"]["memory_candidates"] = [
            {
                "candidate_id": "candidate-1",
                "kind": "message",
                "content": "history",
                "priority": 1,
                "trim_policy": "tail",
                "token_estimate": 2,
                "metadata": {},
            }
        ]
        memory_callbacks.memory_value = nested_memory
        memory_callbacks.fail_handoff_once = True
        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(
                memory_admission, _FakeReceipts(), memory_callbacks
            ).recover_pending()
        memory_prepared = json.loads(memory_admission.entries[0].record.prepared_execution)
        self.assertEqual(memory_prepared["execution_text_source"], "memory_context")
        self.assertEqual(
            memory_prepared["execution_text_sha256"],
            hashlib.sha256(b"resolved memory text").hexdigest(),
        )
        self.assertEqual(
            memory_callbacks.agent_contexts[0].current_user_input,
            "resolved memory text",
        )
        self.assertEqual(
            memory_callbacks.agent_contexts[0].memory_context,
            nested_memory["prompt_payload"],
        )

        pending_record = _with_continuation(
            _record("3"),
            pending_context={
                "context_id": "context-3",
                "capability_id": "skill.example",
                "original_user_message": "original request",
                "assistant_message": "need region",
                "missing_requirements": ["region"],
            },
        )
        pending_admission = _FakeAdmission([pending_record], self.clock)
        pending_callbacks = _Callbacks(pending_admission)
        pending_callbacks.fail_handoff_once = True
        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(
                pending_admission, _FakeReceipts(), pending_callbacks
            ).recover_pending()
        pending_prepared = json.loads(pending_admission.entries[0].record.prepared_execution)
        pending_text = "original request\n\n此前缺少的信息：region\n\n用户补充：hello-3"
        self.assertEqual(pending_prepared["execution_text_source"], "pending_context")
        self.assertEqual(
            pending_prepared["execution_text_sha256"],
            hashlib.sha256(pending_text.encode()).hexdigest(),
        )
        self.assertEqual(
            pending_callbacks.agent_contexts[0].current_user_input,
            pending_text,
        )

    async def test_requested_capability_uses_provider_safe_tool_name(self) -> None:
        record = _with_continuation(
            _record("1"),
            routing_mode="force_capability",
            requested_capability_id="skill.tool/unsafe",
        )
        admission = _FakeAdmission([record], self.clock)
        callbacks = _Callbacks(admission)
        callbacks.fail_handoff_once = True
        with self.assertRaisesRegex(RuntimeError, "handoff_crash"):
            await self._coordinator(admission, _FakeReceipts(), callbacks).recover_pending()
        prepared = json.loads(admission.entries[0].record.prepared_execution)
        self.assertRegex(prepared["initial_required_tool_name"], r"^[A-Za-z0-9_-]{1,64}$")
        self.assertNotEqual(prepared["initial_required_tool_name"], "resume")

    async def test_route_and_selector_cross_constraints_fail_closed(self) -> None:
        false_record = _record("1")
        false_admission = _FakeAdmission([false_record], self.clock)
        false_callbacks = _Callbacks(false_admission)
        false_callbacks.route_value = _no_server_route()
        with self.assertRaisesRegex(SubmissionRecoveryError, "route_decision_conflict"):
            await self._coordinator(
                false_admission, _FakeReceipts(), false_callbacks
            ).recover_pending()

        true_record = _with_continuation(
            _record("2"), initial_no_server_eligible=True
        )
        true_admission = _FakeAdmission([true_record], self.clock)
        true_callbacks = _Callbacks(true_admission)
        true_callbacks.route_value = _no_server_route()
        true_callbacks.selector_value = _selector(interrupt_kind="file_selection")
        await self._coordinator(
            true_admission, _FakeReceipts(), true_callbacks
        ).recover_pending()
        self.assertEqual(true_callbacks.compute_order, ["route:2"])
        self.assertEqual(true_callbacks.materialized, ["route:2"])

    async def test_interrupt_and_no_server_handoff_identities_are_exact(self) -> None:
        interrupt_admission = _FakeAdmission([_record("1")], self.clock)
        interrupt_callbacks = _Callbacks(interrupt_admission)
        interrupt_callbacks.selector_value = _selector(interrupt_kind="file_selection")
        await self._coordinator(
            interrupt_admission, _FakeReceipts(), interrupt_callbacks
        ).recover_pending()
        interrupt_identity = interrupt_callbacks.last_handoff_identity
        self.assertIn("task-1", interrupt_identity)
        selector_sha = interrupt_identity.rsplit(":", 1)[-1]
        self.assertNotEqual(
            submission_interrupt_handoff_id("task-1", selector_sha),
            submission_interrupt_handoff_id("task-other", selector_sha),
        )

        drift_admission = _FakeAdmission([_record("3")], self.clock)
        drift_callbacks = _Callbacks(drift_admission)
        drift_callbacks.selector_value = _selector(interrupt_kind="file_selection")
        drift_callbacks.forced_handoff_identity = submission_interrupt_handoff_id(
            "task-other", "c" * 64
        )
        with self.assertRaisesRegex(SubmissionRecoveryError, "identity_drift"):
            await self._coordinator(
                drift_admission, _FakeReceipts(), drift_callbacks
            ).recover_pending()

        no_server_record = _with_continuation(
            _record("2"), initial_no_server_eligible=True
        )
        no_server_admission = _FakeAdmission([no_server_record], self.clock)
        no_server_callbacks = _Callbacks(no_server_admission)
        no_server_callbacks.route_value = _no_server_route()
        await self._coordinator(
            no_server_admission, _FakeReceipts(), no_server_callbacks
        ).recover_pending()
        self.assertEqual(
            no_server_callbacks.last_handoff_identity,
            mcp_no_server_intent_id("task-2"),
        )

    async def test_materialization_replay_is_exact_across_coordinator_instances(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        admission.fail_handoff_ack_once = True
        receipts = _FakeReceipts()
        sink = _DurableSink()
        first_callbacks = _Callbacks(admission, sink=sink)
        with self.assertRaisesRegex(RuntimeError, "handoff_ack_crash"):
            await self._coordinator(admission, receipts, first_callbacks).recover_pending()
        created = sink.created_count

        self.clock.advance(seconds=31)
        second_callbacks = _Callbacks(admission, sink=sink)
        await self._coordinator(admission, receipts, second_callbacks).recover_pending()
        self.assertEqual(sink.created_count, created)
        self.assertEqual(admission.handoff_acks, ["1"])

        conflict_admission = _FakeAdmission([_record("2")], self.clock)
        conflict_sink = _DurableSink()
        conflict_sink.values["route:task-2"] = b"different"
        with self.assertRaisesRegex(RuntimeError, "durable_materialization_conflict"):
            await self._coordinator(
                conflict_admission,
                _FakeReceipts(),
                _Callbacks(conflict_admission, sink=conflict_sink),
            ).recover_pending()

    async def test_projection_and_handoff_are_two_private_nonreentrant_phases(self) -> None:
        admission = _FakeAdmission([_record("1"), _record("2")], self.clock)
        callbacks = _Callbacks(admission)
        coordinator = self._coordinator(admission, _FakeReceipts(), callbacks)

        projected = await coordinator.project_pending()
        self.assertEqual(projected.recovered_count, 2)
        self.assertEqual(callbacks.compute_order, [])
        self.assertEqual(callbacks.materialized, [])
        self.assertTrue(all(entry.active_handle is not None for entry in admission.entries))
        with self.assertRaisesRegex(SubmissionRecoveryError, "phase_conflict"):
            await coordinator.project_pending()

        recovered = await coordinator.recover_projected_handoffs()
        self.assertEqual(recovered.recovered_count, 2)
        self.assertTrue(all(entry.active_handle is None for entry in admission.entries))
        with self.assertRaisesRegex(SubmissionRecoveryError, "phase_conflict"):
            await coordinator.recover_projected_handoffs()

    async def test_compatibility_facade_runs_both_phases_end_to_end(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        result = await self._coordinator(
            admission, _FakeReceipts(), callbacks
        ).recover_pending()
        self.assertEqual(result.recovered_count, 1)
        self.assertEqual(admission.handoff_acks, ["1"])
        self.assertEqual(callbacks.wakeups, ["agent-run:task-1"])

    async def test_abort_during_project_waits_for_single_operation_owner(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        admission.block_projection_ack = True
        coordinator = self._coordinator(
            admission, _FakeReceipts(), _Callbacks(admission)
        )
        projecting = asyncio.create_task(coordinator.project_pending())
        await admission.projection_ack_started.wait()
        aborting = asyncio.create_task(coordinator.abort_pending())
        await asyncio.sleep(0)
        self.assertFalse(aborting.done())
        admission.projection_ack_release.set()
        await projecting
        await aborting
        renews = admission.renew_count
        self.clock.advance(seconds=31)
        await asyncio.sleep(0)
        self.assertEqual(admission.renew_count, renews)
        with self.assertRaisesRegex(SubmissionRecoveryError, "phase_conflict"):
            await coordinator.recover_projected_handoffs()

    async def test_abort_during_recover_cannot_clear_a_newer_batch(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        callbacks.block_memory = True
        coordinator = self._coordinator(admission, _FakeReceipts(), callbacks)
        await coordinator.project_pending()
        recovering = asyncio.create_task(coordinator.recover_projected_handoffs())
        await callbacks.memory_started.wait()
        aborting = asyncio.create_task(coordinator.abort_pending())
        await asyncio.sleep(0)
        self.assertFalse(aborting.done())
        callbacks.memory_release.set()
        await recovering
        await aborting
        self.assertEqual(admission.handoff_acks, ["1"])
        with self.assertRaisesRegex(SubmissionRecoveryError, "phase_conflict"):
            await coordinator.recover_projected_handoffs()

    async def test_abort_pending_stops_private_keepers_without_exposing_handles(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        callbacks = _Callbacks(admission)
        coordinator = self._coordinator(admission, _FakeReceipts(), callbacks)
        result = await coordinator.project_pending()
        self.assertFalse(hasattr(result, "handle"))
        await coordinator.abort_pending()
        renews = admission.renew_count
        self.clock.advance(seconds=31)
        await asyncio.sleep(0)
        self.assertEqual(admission.renew_count, renews)
        self.assertEqual(callbacks.compute_order, [])

    async def test_long_compute_renew_failure_prevents_component_sql_write(self) -> None:
        admission = _FakeAdmission([_record("1")], self.clock)
        receipts = _FakeReceipts()
        callbacks = _Callbacks(admission)
        callbacks.block_memory = True
        coordinator = self._coordinator(admission, receipts, callbacks)
        recovery = asyncio.create_task(coordinator.recover_pending())
        await callbacks.memory_started.wait()
        admission.fail_next_renew = True
        self.clock.advance(seconds=11)
        for _ in range(3):
            await asyncio.sleep(0)
        callbacks.memory_release.set()
        with self.assertRaisesRegex(SubmissionRecoveryError, "claim_renewal_failed"):
            await recovery
        self.assertIsNone(receipts.rows["task-1"].memory_context)
        self.assertEqual(callbacks.materialized, [])
        self.assertEqual(callbacks.sink.values, {})

    async def test_no_server_requires_canonical_null_memory_and_selector(self) -> None:
        record = _with_continuation(
            _record("1"), initial_no_server_eligible=True
        )
        admission = _FakeAdmission([record], self.clock)
        callbacks = _Callbacks(admission)
        callbacks.route_value = _no_server_route()
        callbacks.memory_value = _memory("must not compute")
        callbacks.selector_value = _selector(interrupt_kind="must_not_compute")
        receipts = _FakeReceipts()

        await self._coordinator(admission, receipts, callbacks).recover_pending()

        self.assertEqual(callbacks.compute_order, ["route:1"])
        self.assertEqual(callbacks.materialized, ["route:1"])
        self.assertEqual(
            set(callbacks.sink.values), {"route:task-1", "handoff:task-1"}
        )
        row = receipts.rows["task-1"]
        self.assertEqual(row.memory_context, b"null")
        self.assertEqual(row.selector_decision, b"null")

    async def test_memory_nested_contract_rejects_unknown_or_unbound_writes(self) -> None:
        invalid_values = []
        unknown_message = _memory("resolved")
        unknown_message["prompt_payload"]["recent_messages"] = [
            {
                "message_id": "m1",
                "role": "user",
                "content": "hello",
                "task_id": "task-1",
                "kind": "message",
                "created_at": None,
                "unknown": True,
            }
        ]
        invalid_values.append(unknown_message)
        unknown_candidate = _memory("resolved")
        unknown_candidate["prompt_payload"]["memory_candidates"] = [
            {
                "candidate_id": "candidate-1",
                "kind": "message",
                "content": "hello",
                "priority": 1,
                "trim_policy": "tail",
                "token_estimate": 2,
                "metadata": {},
                "unknown": True,
            }
        ]
        invalid_values.append(unknown_candidate)
        arbitrary_summary = _memory("resolved")
        arbitrary_summary["summary_write"] = {"summary_id": "random"}
        invalid_values.append(arbitrary_summary)
        arbitrary_event = _memory("resolved")
        arbitrary_event["event_write"] = {"event_id": "random"}
        invalid_values.append(arbitrary_event)

        for index, memory in enumerate(invalid_values, start=3):
            admission = _FakeAdmission([_record(str(index))], self.clock)
            callbacks = _Callbacks(admission)
            callbacks.memory_value = memory
            with self.assertRaisesRegex(SubmissionRecoveryError, "memory_"):
                await self._coordinator(
                    admission, _FakeReceipts(), callbacks
                ).recover_pending()
            self.assertEqual(callbacks.materialized, [])

    async def test_memory_summary_and_event_are_bound_to_current_admission(self) -> None:
        cases = (
            {"summary_conversation_id": "conversation-other"},
            {"summary_username": "owner-other"},
            {"event_conversation_id": "conversation-other"},
            {"event_task_id": "task-other"},
        )
        for index, changes in enumerate(cases, start=6):
            record = _record(str(index))
            admission = _FakeAdmission([record], self.clock)
            callbacks = _Callbacks(admission)
            callbacks.memory_value = _bound_memory(record, **changes)
            receipts = _FakeReceipts()
            with self.assertRaisesRegex(
                SubmissionRecoveryError, "memory_(summary|event)_binding_conflict"
            ):
                await self._coordinator(
                    admission, receipts, callbacks
                ).recover_pending()
            self.assertIsNone(receipts.rows[record.task_id].memory_context)
            self.assertEqual(callbacks.sink.values, {})

    def _coordinator(
        self,
        admission: "_FakeAdmission",
        receipts: "_FakeReceipts",
        callbacks: "_Callbacks",
        *,
        recovery_limit: int = 8,
    ) -> SubmissionAdmissionCoordinator:
        callbacks.receipts = receipts
        return SubmissionAdmissionCoordinator(
            admission=admission,
            receipts=receipts,
            callbacks=callbacks,
            claim_owner="recovery-worker",
            now=self.clock.now,
            wait_until=self.clock.wait_until,
            expected_finalization_receipt_sha256="f" * 64,
            claim_ttl=timedelta(seconds=30),
            recovery_limit=recovery_limit,
        )


class PreparedAgentRecoveryLoaderTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_found_returns_none_without_sql_read(self) -> None:
        admission = _ReadOnlyPreparationAdmission(None)
        receipts = _ReadOnlyPreparationReceipts(None)
        loader = SubmissionPreparedAgentRecoveryLoader(
            admission=admission,
            receipts=receipts,
        )

        loaded = await loader.load(
            username="owner",
            conversation_id="conversation-1",
            task_id="task-1",
            message_id="1",
            root_message_content="hello-1",
        )

        self.assertIsNone(loaded)
        self.assertEqual(admission.read_count, 1)
        self.assertEqual(receipts.read_count, 0)

    async def test_legacy_v1_prepared_record_remains_exactly_readable(self) -> None:
        record = _record("48")
        loader, admission, _receipts = await self._loader_for(record)
        assert admission.preparation is not None
        value = json.loads(admission.preparation.prepared_execution)
        value["schema"] = "maf.submission.prepared_execution.v1"
        value.pop("routing_mode")
        value.pop("skill_activation")
        content = _canonical(value)
        admission.preparation = replace(
            admission.preparation,
            prepared_execution=content,
            prepared_execution_sha256=hashlib.sha256(
                b"maf.submission.prepared_execution.v1\0" + content
            ).hexdigest(),
        )

        loaded = await loader.load(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            message_id=record.message_id,
            root_message_content="hello-48",
        )

        assert loaded is not None
        self.assertEqual(loaded.routing_mode, "auto")
        self.assertIsNone(loaded.skill_activation_payload_json)
        self.assertIsNone(loaded.skill_activation_payload_sha256)

    async def test_v2_content_with_v1_digest_is_rejected(self) -> None:
        record = _record("49")
        loader, admission, receipts = await self._loader_for(record)
        assert admission.preparation is not None
        content = admission.preparation.prepared_execution
        admission.preparation = replace(
            admission.preparation,
            prepared_execution_sha256=hashlib.sha256(
                b"maf.submission.prepared_execution.v1\0" + content
            ).hexdigest(),
        )

        with self.assertRaisesRegex(
            SubmissionRecoveryError,
            "prepared_digest_mismatch",
        ):
            await loader.load(
                username=record.username,
                conversation_id=record.conversation_id,
                task_id=record.task_id,
                message_id=record.message_id,
                root_message_content="hello-49",
            )
        self.assertEqual(receipts.read_count, 0)

    async def test_root_pending_and_large_memory_sources_recover_exactly(self) -> None:
        cases: list[tuple[str, SubmissionRecoveryRecord, object, str]] = [
            ("root", _record("31"), None, "hello-31"),
            (
                "pending",
                _with_continuation(
                    _record("32"),
                    pending_context={
                        "context_id": "context-loader",
                        "capability_id": "skill.example",
                        "original_user_message": "original request",
                        "assistant_message": "need region",
                        "missing_requirements": ["region"],
                    },
                ),
                None,
                "original request\n\n此前缺少的信息：region\n\n用户补充：hello-32",
            ),
        ]
        large_memory = _memory(None)
        large_memory["prompt_payload"]["current_user_message"] = "记忆" * 80_000
        memory_record = _record("33")
        memory_execution_metadata = json.loads(memory_record.continuation)[
            "execution_metadata"
        ]
        memory_execution_metadata.update(
            {
                "mcp_dispatch_server_id": "server-1",
                "mcp_binding_mode": "explicit",
                "mcp_command": "run",
                "mcp_execution_mode": "user_scoped",
                "mcp_rollout_config_version": "config-1",
                "mcp_route_reason_code": "assigned",
                "mcp_rollout_mode": "enforce",
                "mcp_shadow_enabled": False,
                "forced_by_mcp_command": True,
            }
        )
        cases.append(
            (
                "memory",
                _with_continuation(
                    memory_record,
                    routing_mode="force_capability",
                    requested_capability_id="mcp.dispatch",
                    bundle_revisions={
                        "skill_bundle_revision": "skill-r7",
                        "mcp_bundle_revision": "mcp-r9",
                    },
                    available_mcp_servers=[
                        {
                            "server_id": "server-1",
                            "display_name": "Server One",
                            "routing_description": "safe profile",
                            "transport": "streamable_http",
                        }
                    ],
                    mcp_binding={
                        "server_id": "server-1",
                        "display_name": "Server One",
                        "command": "run",
                        "binding_mode": "explicit",
                        "server_config_version": 1,
                        "server_security_version": 1,
                    },
                    mcp_assignment={
                        "execution_mode": "user_scoped",
                        "shadow_enabled": False,
                        "rollout_config_version": "config-1",
                        "route_reason_code": "assigned",
                        "rollout_mode": "enforce",
                    },
                    execution_metadata=memory_execution_metadata,
                ),
                large_memory,
                "记忆" * 80_000,
            )
        )

        for name, record, memory_value, expected_text in cases:
            with self.subTest(source=name):
                loader, admission, receipts = await self._loader_for(
                    record,
                    memory_value=memory_value,
                )
                loaded = await loader.load(
                    username=record.username,
                    conversation_id=record.conversation_id,
                    task_id=record.task_id,
                    message_id=record.message_id,
                    root_message_content=f"hello-{record.message_id}",
                )
                assert loaded is not None
                self.assertEqual(loaded.username, record.username)
                self.assertEqual(loaded.current_user_input, expected_text)
                self.assertEqual(admission.read_count, 1)
                self.assertEqual(receipts.read_count, 1)
                if name == "memory":
                    self.assertEqual(
                        loaded.memory_context,
                        large_memory["prompt_payload"],
                    )
                    self.assertEqual(
                        loaded.bundle_revisions,
                        {
                            "skill_bundle_revision": "skill-r7",
                            "mcp_bundle_revision": "mcp-r9",
                        },
                    )
                    self.assertRegex(
                        loaded.initial_required_tool_name or "",
                        r"^[A-Za-z0-9_-]{1,64}$",
                    )
                    self.assertEqual(
                        [profile.server_id for profile in loaded.available_mcp_servers],
                        ["server-1"],
                    )
                    self.assertEqual(
                        loaded.mcp_assignment,
                        {
                            "execution_mode": "user_scoped",
                            "shadow_enabled": False,
                            "rollout_config_version": "config-1",
                            "route_reason_code": "assigned",
                            "rollout_mode": "enforce",
                        },
                    )

    async def test_handoff_and_prepared_digest_drift_fail_before_sql_read(self) -> None:
        record = _record("34")
        loader, admission, receipts = await self._loader_for(record)
        assert admission.preparation is not None
        original = admission.preparation
        cases = (
            replace(original, handoff_identity="agent-run:wrong"),
            replace(original, prepared_execution_sha256="0" * 64),
        )
        for preparation in cases:
            with self.subTest(preparation=preparation):
                admission.preparation = preparation
                receipts.read_count = 0
                with self.assertRaises(SubmissionRecoveryError):
                    await loader.load(
                        username=record.username,
                        conversation_id=record.conversation_id,
                        task_id=record.task_id,
                        message_id=record.message_id,
                        root_message_content="hello-34",
                    )
                self.assertEqual(receipts.read_count, 0)

    async def test_rehashed_owner_drift_is_not_exposed_as_agent_scope(self) -> None:
        record = _record("37")
        loader, admission, receipts = await self._loader_for(record)
        assert admission.preparation is not None
        admission.preparation = _mutate_preparation(
            admission.preparation,
            lambda value: value.update({"owner_scope": "different-owner"}),
        )

        with self.assertRaisesRegex(
            SubmissionRecoveryError, "prepared_agent_identity_drift"
        ):
            await loader.load(
                username=record.username,
                conversation_id=record.conversation_id,
                task_id=record.task_id,
                message_id=record.message_id,
                root_message_content="hello-37",
            )

        self.assertEqual(receipts.read_count, 0)

    async def test_rehashed_mcp_binding_profile_and_metadata_drift_fail_closed(self) -> None:
        record = _record("38")
        loader, admission, _receipts = await self._loader_for(record)
        assert admission.preparation is not None
        original = admission.preparation

        def bind(value: dict[str, Any]) -> None:
            value["mcp_binding"] = {
                "server_id": "server-1",
                "display_name": "Server One",
                "command": "run",
                "binding_mode": "explicit",
                "server_config_version": 1,
                "server_security_version": 1,
            }
            value["available_mcp_servers"] = [
                {
                    "server_id": "server-1",
                    "display_name": "Server One",
                    "routing_description": "safe",
                    "transport": "streamable_http",
                }
            ]
            value["execution_metadata"].update(
                {
                    "mcp_dispatch_server_id": "server-1",
                    "mcp_binding_mode": "explicit",
                    "mcp_command": "run",
                    "forced_by_mcp_command": True,
                }
            )

        def metadata_drift(value: dict[str, Any]) -> None:
            bind(value)
            value["execution_metadata"]["mcp_command"] = "different"

        def profile_drift(value: dict[str, Any]) -> None:
            bind(value)
            value["available_mcp_servers"][0]["display_name"] = "Different"

        def missing_binding_metadata_drift(value: dict[str, Any]) -> None:
            value["execution_metadata"]["mcp_command"] = "run"

        for mutate in (
            metadata_drift,
            profile_drift,
            missing_binding_metadata_drift,
        ):
            with self.subTest(mutate=mutate.__name__):
                admission.preparation = _mutate_preparation(original, mutate)
                with self.assertRaisesRegex(
                    SubmissionRecoveryError, "prepared_mcp_binding_drift"
                ):
                    await loader.load(
                        username=record.username,
                        conversation_id=record.conversation_id,
                        task_id=record.task_id,
                        message_id=record.message_id,
                        root_message_content="hello-38",
                    )

    async def test_rehashed_mcp_assignment_metadata_drift_fails_closed(self) -> None:
        record = _record("39")
        loader, admission, _receipts = await self._loader_for(record)
        assert admission.preparation is not None

        def assignment_drift(value: dict[str, Any]) -> None:
            value["mcp_assignment"] = {
                "execution_mode": "user_scoped",
                "shadow_enabled": False,
                "rollout_config_version": "config-1",
                "route_reason_code": "assigned",
                "rollout_mode": "enforce",
            }
            value["execution_metadata"].update(
                {
                    "mcp_execution_mode": "legacy",
                    "mcp_shadow_enabled": False,
                    "mcp_rollout_config_version": "config-1",
                    "mcp_route_reason_code": "assigned",
                    "mcp_rollout_mode": "enforce",
                }
            )

        def missing_assignment_metadata_drift(value: dict[str, Any]) -> None:
            value["execution_metadata"]["mcp_execution_mode"] = "legacy"

        original = admission.preparation
        for mutate in (assignment_drift, missing_assignment_metadata_drift):
            with self.subTest(mutate=mutate.__name__):
                admission.preparation = _mutate_preparation(original, mutate)
                with self.assertRaisesRegex(
                    SubmissionRecoveryError, "prepared_mcp_assignment_drift"
                ):
                    await loader.load(
                        username=record.username,
                        conversation_id=record.conversation_id,
                        task_id=record.task_id,
                        message_id=record.message_id,
                        root_message_content="hello-39",
                    )

    async def test_component_overall_locator_and_execution_drift_fail_closed(self) -> None:
        record = _record("35")
        loader, admission, receipts = await self._loader_for(record)
        assert admission.preparation is not None
        assert receipts.receipt is not None
        original_preparation = admission.preparation
        original_receipt = receipts.receipt

        cases: list[tuple[SubmissionPreparationRecord, SubmissionPreparationReceipt, str]] = []
        cases.append(
            (
                original_preparation,
                replace(original_receipt, memory_context_sha256="0" * 64),
                "component_digest_drift",
            )
        )
        cases.append(
            (
                original_preparation,
                replace(original_receipt, receipt_sha256="0" * 64),
                "receipt_digest_drift",
            )
        )
        locator_drift = _mutate_preparation(
            original_preparation,
            lambda value: value["preparation_receipt"].update(
                {"receipt_sha256": "0" * 64}
            ),
        )
        cases.append((locator_drift, original_receipt, "receipt_drift"))
        execution_drift = _mutate_preparation(
            original_preparation,
            lambda value: value.update({"execution_text_sha256": "0" * 64}),
        )
        cases.append((execution_drift, original_receipt, "execution_text_digest_mismatch"))

        for preparation, receipt, error in cases:
            with self.subTest(error=error):
                admission.preparation = preparation
                receipts.receipt = receipt
                with self.assertRaisesRegex(SubmissionRecoveryError, error):
                    await loader.load(
                        username=record.username,
                        conversation_id=record.conversation_id,
                        task_id=record.task_id,
                        message_id=record.message_id,
                        root_message_content="hello-35",
                    )

    async def test_missing_closed_receipt_fails_after_exact_preparation_read(self) -> None:
        record = _record("36")
        loader, admission, receipts = await self._loader_for(record)
        receipts.receipt = None

        with self.assertRaisesRegex(
            SubmissionRecoveryError, "preparation_receipt_not_closed"
        ):
            await loader.load(
                username=record.username,
                conversation_id=record.conversation_id,
                task_id=record.task_id,
                message_id=record.message_id,
                root_message_content="hello-36",
            )

        self.assertEqual(admission.read_count, 1)
        self.assertEqual(receipts.read_count, 1)

    async def _loader_for(
        self,
        record: SubmissionRecoveryRecord,
        *,
        memory_value: object = None,
    ) -> tuple[
        SubmissionPreparedAgentRecoveryLoader,
        "_ReadOnlyPreparationAdmission",
        "_ReadOnlyPreparationReceipts",
    ]:
        clock = _Clock(datetime(2026, 8, 26, tzinfo=timezone.utc))
        admission = _FakeAdmission([record], clock)
        receipt_store = _FakeReceipts()
        callbacks = _Callbacks(admission)
        callbacks.receipts = receipt_store
        callbacks.memory_value = memory_value
        coordinator = SubmissionAdmissionCoordinator(
            admission=admission,
            receipts=receipt_store,
            callbacks=callbacks,
            claim_owner="loader-fixture",
            now=clock.now,
            wait_until=clock.wait_until,
            expected_finalization_receipt_sha256="f" * 64,
            claim_ttl=timedelta(seconds=30),
            recovery_limit=8,
        )
        await coordinator.recover_pending()
        recovered = admission.entries[0].record
        assert recovered.prepared_execution is not None
        assert recovered.prepared_execution_sha256 is not None
        preparation = SubmissionPreparationRecord(
            conversation_id=recovered.conversation_id,
            message_id=recovered.message_id,
            task_id=recovered.task_id,
            prepared_execution=recovered.prepared_execution,
            prepared_execution_sha256=recovered.prepared_execution_sha256,
            handoff_state=SubmissionHandoffState.HANDED_OFF,
            handoff_kind="agent_run",
            handoff_identity=f"agent-run:{recovered.task_id}",
        )
        read_admission = _ReadOnlyPreparationAdmission(preparation)
        read_receipts = _ReadOnlyPreparationReceipts(
            receipt_store.rows[recovered.task_id]
        )
        return (
            SubmissionPreparedAgentRecoveryLoader(
                admission=read_admission,
                receipts=read_receipts,
            ),
            read_admission,
            read_receipts,
        )


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)
        for deadline, future in tuple(self.waiters):
            if deadline <= self.value and not future.done():
                future.set_result(None)

    async def wait_until(self, deadline: datetime) -> None:
        if deadline <= self.value:
            return
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((deadline, future))
        try:
            await future
        finally:
            self.waiters.remove((deadline, future))


class _Entry:
    def __init__(self, record: SubmissionRecoveryRecord) -> None:
        self.record = record
        self.claim_revision = 0
        self.claim_expires_at: datetime | None = None
        self.active_handle: SubmissionAdmissionHandle | None = None


class _FakeAdmission:
    def __init__(self, records: list[SubmissionRecoveryRecord], clock: _Clock) -> None:
        self.entries = [_Entry(record) for record in records]
        self.clock = clock
        self.projection_writes = 0
        self.projection_ack_states: list[SubmissionProjectionState] = []
        self.sql_projection_writes = 0
        self.sql_projected_message_ids: set[str] = set()
        self.claimed_handles: list[SubmissionAdmissionHandle] = []
        self.claim_calls = 0
        self.handoff_acks: list[str] = []
        self.renew_count = 0
        self.stale_handle_uses = 0
        self.fail_sql_projection_once_after_authority_ack = False
        self.fail_handoff_ack_once = False
        self.fail_next_renew = False
        self.block_projection_ack = False
        self.projection_ack_started = asyncio.Event()
        self.projection_ack_release = asyncio.Event()
        self.finalization_receipt: str | None = "f" * 64
        self.first_ack_request = None
        self._handles: dict[SubmissionAdmissionHandle, _Entry] = {}

    async def claim_pending_submission(self, request) -> SubmissionClaimResult:
        self.claim_calls += 1
        candidates = [
            entry
            for entry in self.entries
            if entry.record.phase.handoff_state is SubmissionHandoffState.PENDING
            and (
                request.after_created_at is None
                or (entry.record.created_at, entry.record.message_id)
                > (request.after_created_at, request.after_message_id)
            )
        ]
        candidates.sort(key=lambda entry: (entry.record.created_at, entry.record.message_id))
        if not candidates:
            return SubmissionClaimResult(
                found=False,
                authority_state=SubmissionAuthorityState.FINALIZED,
                finalization_receipt_sha256=self.finalization_receipt,
            )
        entry = candidates[0]
        if entry.claim_expires_at is not None and entry.claim_expires_at > request.now:
            return SubmissionClaimResult(
                found=False,
                authority_state=SubmissionAuthorityState.FINALIZED,
                finalization_receipt_sha256=self.finalization_receipt,
                pending_count=len(candidates),
                earliest_claim_expires_at=entry.claim_expires_at,
            )
        handle = self._rotate(entry, request.claim_expires_at)
        self.claimed_handles.append(handle)
        return SubmissionClaimResult(
            found=True,
            authority_state=SubmissionAuthorityState.FINALIZED,
            finalization_receipt_sha256=self.finalization_receipt,
            pending_count=len(candidates),
            record=entry.record,
            handle=handle,
        )

    def admission_result(
        self,
        *,
        disposition: SubmissionAdmissionDisposition,
        with_handle: bool = True,
    ) -> SubmissionAdmissionResult:
        entry = self.entries[0]
        handle = (
            self._rotate(entry, self.clock.now() + timedelta(seconds=30))
            if with_handle
            else None
        )
        record = entry.record
        return SubmissionAdmissionResult(
            disposition=disposition,
            conversation_id=record.conversation_id,
            message_id=record.message_id,
            task_id=record.task_id,
            message_created_at=record.created_at,
            task_created_at=record.created_at,
            phase=record.phase,
            record=record,
            handle=handle,
        )

    async def renew_submission_claim(self, request) -> SubmissionAdmissionHandle:
        entry = self._binding(request.handle)
        if self.fail_next_renew:
            self.fail_next_renew = False
            raise RuntimeError("renew_failed")
        self.renew_count += 1
        return self._rotate(entry, request.claim_expires_at)

    async def get_submission_preparation(self, request) -> SubmissionPreparationRecord | None:
        entry = next(
            (
                candidate
                for candidate in self.entries
                if candidate.record.username == request.username
                and candidate.record.conversation_id == request.conversation_id
                and candidate.record.task_id == request.task_id
            ),
            None,
        )
        if entry is None:
            return None
        record = entry.record
        return SubmissionPreparationRecord(
            conversation_id=record.conversation_id,
            message_id=record.message_id,
            task_id=record.task_id,
            prepared_execution=record.prepared_execution or b"",
            prepared_execution_sha256=record.prepared_execution_sha256 or "",
            handoff_state=record.phase.handoff_state,
            handoff_kind=(
                "agent_run"
                if record.phase.handoff_state is SubmissionHandoffState.HANDED_OFF
                else None
            ),
            handoff_identity=(
                f"agent-run:{record.task_id}"
                if record.phase.handoff_state is SubmissionHandoffState.HANDED_OFF
                else None
            ),
        )

    async def acknowledge_submission_projection(self, request) -> SubmissionAdmissionPhase:
        entry = self._binding(request.handle)
        self.projection_writes += 1
        self.projection_ack_states.append(entry.record.phase.projection_state)
        if self.block_projection_ack:
            self.projection_ack_started.set()
            await self.projection_ack_release.wait()
        entry.record = replace(
            entry.record,
            phase=replace(
                entry.record.phase,
                projection_state=SubmissionProjectionState.PROJECTED,
            ),
        )
        if self.fail_sql_projection_once_after_authority_ack:
            self.fail_sql_projection_once_after_authority_ack = False
            raise RuntimeError("sql_projection_crash")
        if entry.record.message_id not in self.sql_projected_message_ids:
            self.sql_projected_message_ids.add(entry.record.message_id)
            self.sql_projection_writes += 1
        return entry.record.phase

    async def prepare_submission_handoff(self, request) -> SubmissionPreparationRecord:
        entry = self._binding(request.handle)
        existing = entry.record.prepared_execution
        if existing is not None and existing != request.prepared_execution:
            raise RuntimeError("submission_preparation_conflict")
        entry.record = replace(
            entry.record,
            prepared_execution=request.prepared_execution,
            prepared_execution_sha256=request.prepared_execution_sha256,
            phase=replace(
                entry.record.phase,
                preparation_state=SubmissionPreparationState.PREPARED,
            ),
        )
        return SubmissionPreparationRecord(
            conversation_id=entry.record.conversation_id,
            message_id=entry.record.message_id,
            task_id=entry.record.task_id,
            prepared_execution=request.prepared_execution,
            prepared_execution_sha256=request.prepared_execution_sha256,
            handoff_state=SubmissionHandoffState.PENDING,
        )

    async def acknowledge_submission_handoff(self, request) -> SubmissionAdmissionPhase:
        if self.first_ack_request is None:
            self.first_ack_request = request
        entry = self._binding(request.handle)
        if self.fail_handoff_ack_once:
            self.fail_handoff_ack_once = False
            raise RuntimeError("handoff_ack_crash")
        entry.record = replace(
            entry.record,
            phase=replace(
                entry.record.phase,
                handoff_state=SubmissionHandoffState.HANDED_OFF,
            ),
        )
        self.handoff_acks.append(entry.record.message_id)
        self._handles.pop(request.handle, None)
        entry.active_handle = None
        return entry.record.phase

    def all_projected(self) -> bool:
        return all(
            entry.record.phase.projection_state is SubmissionProjectionState.PROJECTED
            for entry in self.entries
        )

    def _rotate(self, entry: _Entry, expires_at: datetime) -> SubmissionAdmissionHandle:
        if entry.active_handle is not None:
            self._handles.pop(entry.active_handle, None)
        handle = SubmissionAdmissionHandle()
        entry.active_handle = handle
        entry.claim_revision += 1
        entry.claim_expires_at = expires_at
        self._handles[handle] = entry
        return handle

    def _binding(self, handle: SubmissionAdmissionHandle) -> _Entry:
        entry = self._handles.get(handle)
        if entry is None:
            self.stale_handle_uses += 1
            raise RuntimeError("stale_claim")
        return entry


class _FakeReceipts:
    def __init__(self) -> None:
        self.rows: dict[str, SubmissionPreparationReceipt] = {}
        self.route_settle_count = 0
        self.route_conflict_on_settle: bytes | None = None
        self.generic_write_components: list[
            SubmissionPreparationReceiptComponent
        ] = []

    async def get_submission_preparation_receipt(self, **request):
        return self.rows.get(request["task_id"])

    async def write_submission_preparation_component(self, **request):
        self.generic_write_components.append(request["component"])
        task_id = request["task_id"]
        now = request["written_at"]
        row = self.rows.get(task_id) or SubmissionPreparationReceipt(
            task_id=task_id,
            conversation_id=request["conversation_id"],
            route_decision=None,
            route_decision_sha256=None,
            memory_context=None,
            memory_context_sha256=None,
            selector_decision=None,
            selector_decision_sha256=None,
            receipt_sha256=None,
            created_at=now,
            updated_at=now,
        )
        name = request["component"].value
        current = getattr(row, name)
        if current is not None and current != request["canonical_json"]:
            raise RuntimeError("submission_preparation_receipt_conflict")
        row = replace(
            row,
            **{
                name: request["canonical_json"],
                f"{name}_sha256": request["component_sha256"],
                "updated_at": now,
            },
        )
        self.rows[task_id] = row
        return row

    async def settle_route_decision_exact(self, **request):
        self.route_settle_count += 1
        task_id = request["task_id"]
        now = request["written_at"]
        row = self.rows.get(task_id) or SubmissionPreparationReceipt(
            task_id=task_id,
            conversation_id=request["conversation_id"],
            route_decision=None,
            route_decision_sha256=None,
            memory_context=None,
            memory_context_sha256=None,
            selector_decision=None,
            selector_decision_sha256=None,
            receipt_sha256=None,
            created_at=now,
            updated_at=now,
        )
        existing = row.route_decision or self.route_conflict_on_settle
        if existing is not None and existing != request["canonical_json"]:
            raise RuntimeError("submission_preparation_receipt_conflict")
        row = replace(
            row,
            route_decision=request["canonical_json"],
            route_decision_sha256=request["component_sha256"],
            updated_at=now,
        )
        self.rows[task_id] = row
        return row

    async def close_submission_preparation_receipt(self, **request):
        row = self.rows[request["task_id"]]
        assert row.route_decision is not None
        assert row.memory_context is not None
        assert row.selector_decision is not None
        digest = hashlib.sha256(
            b"maf.submission.preparation_receipt.v1\0"
            + row.route_decision
            + b"\0"
            + row.memory_context
            + b"\0"
            + row.selector_decision
        ).hexdigest()
        if row.receipt_sha256 is not None and row.receipt_sha256 != digest:
            raise RuntimeError("submission_preparation_receipt_conflict")
        row = replace(row, receipt_sha256=digest, updated_at=request["closed_at"])
        self.rows[row.task_id] = row
        return row


class _ReadOnlyPreparationAdmission:
    def __init__(self, preparation: SubmissionPreparationRecord | None) -> None:
        self.preparation = preparation
        self.read_count = 0

    async def get_submission_preparation(self, request):
        self.read_count += 1
        if self.preparation is not None and (
            request.conversation_id != self.preparation.conversation_id
            or request.task_id != self.preparation.task_id
        ):
            raise AssertionError("unexpected preparation lookup identity")
        return self.preparation


class _ReadOnlyPreparationReceipts:
    def __init__(self, receipt: SubmissionPreparationReceipt | None) -> None:
        self.receipt = receipt
        self.read_count = 0

    async def get_submission_preparation_receipt(self, **request):
        self.read_count += 1
        if self.receipt is not None and (
            request["conversation_id"] != self.receipt.conversation_id
            or request["task_id"] != self.receipt.task_id
        ):
            raise AssertionError("unexpected receipt lookup identity")
        return self.receipt


class _Callbacks:
    def __init__(
        self, admission: _FakeAdmission, *, sink: "_DurableSink | None" = None
    ) -> None:
        self.admission = admission
        self.sink = sink or _DurableSink()
        self.compute_order: list[str] = []
        self.computed_after_projection_closed = True
        self.materialized: list[str] = []
        self.wakeups: list[str] = []
        self.fail_memory_once = False
        self.fail_handoff_once = False
        self.existing_agent_identity: str | None = None
        self.agent_creates = 0
        self.agent_contexts: list[PreparedAgentRecoveryContext] = []
        self.route_value: object = {
            "schema": "maf.submission.route_decision.v1",
            "decision": "not_applicable",
            "owner_server_set_fingerprint": None,
            "available_mcp_servers": [],
        }
        self.memory_value: object = None
        self.selector_value: object = None
        self.block_memory = False
        self.memory_started = asyncio.Event()
        self.memory_release = asyncio.Event()
        self.last_handoff_identity = ""
        self.forced_handoff_identity: str | None = None
        self.receipts: _FakeReceipts | None = None

    async def settle_route_decision_exact(self, record, continuation, written_at):
        self._computed("route", record)
        if self.receipts is None:
            raise AssertionError("route receipt store not bound")
        canonical = _canonical(self.route_value)
        return await self.receipts.settle_route_decision_exact(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            canonical_json=canonical,
            component_sha256=hashlib.sha256(canonical).hexdigest(),
            written_at=written_at,
        )

    async def compute_memory_context(self, record, continuation):
        self._computed("memory", record)
        if self.fail_memory_once:
            self.fail_memory_once = False
            raise RuntimeError("memory_compute_crash")
        if self.block_memory:
            self.memory_started.set()
            await self.memory_release.wait()
        return self.memory_value

    async def compute_selector_decision(self, record, continuation):
        self._computed("selector", record)
        return self.selector_value

    async def materialize_route_decision(self, record, canonical_component):
        self.sink.write(f"route:{record.task_id}", canonical_component)
        self.materialized.append(f"route:{record.message_id}")

    async def materialize_memory_context(self, record, canonical_component):
        self.sink.write(f"memory:{record.task_id}", canonical_component)
        self.materialized.append(f"memory:{record.message_id}")

    async def materialize_selector_decision(self, record, canonical_component):
        self.sink.write(f"selector:{record.task_id}", canonical_component)
        self.materialized.append(f"selector:{record.message_id}")

    async def initialize_agent_handoff(self, record, prepared, context):
        self.agent_contexts.append(context)
        if self.fail_handoff_once:
            self.fail_handoff_once = False
            raise RuntimeError("handoff_crash")
        if self.existing_agent_identity is None:
            self.agent_creates += 1
            identity = f"agent-run:{record.task_id}"
        else:
            identity = self.existing_agent_identity
        self.sink.write(f"handoff:{record.task_id}", identity.encode())
        self.last_handoff_identity = identity
        return DurableSubmissionHandoff("agent_run", identity)

    async def materialize_interrupt_handoff(self, record, prepared):
        identity = submission_interrupt_handoff_id(
            record.task_id,
            prepared["preparation_receipt"]["selector_decision_sha256"],
        )
        identity = self.forced_handoff_identity or identity
        self.sink.write(f"handoff:{record.task_id}", identity.encode())
        self.last_handoff_identity = identity
        return DurableSubmissionHandoff(
            "interrupt",
            identity,
        )

    async def materialize_no_server_intent_handoff(self, record, prepared):
        identity = mcp_no_server_intent_id(record.task_id)
        self.sink.write(f"handoff:{record.task_id}", identity.encode())
        self.last_handoff_identity = identity
        return DurableSubmissionHandoff("no_server_intent", identity)

    async def wakeup_agent(self, record, handoff_identity):
        self.wakeups.append(handoff_identity)

    def _computed(self, component: str, record: SubmissionRecoveryRecord) -> None:
        self.compute_order.append(f"{component}:{record.message_id}")
        self.computed_after_projection_closed &= self.admission.all_projected()


class _DurableSink:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.created_count = 0

    def write(self, key: str, value: bytes) -> None:
        existing = self.values.get(key)
        if existing is not None and existing != value:
            raise RuntimeError("durable_materialization_conflict")
        if existing is None:
            self.values[key] = value
            self.created_count += 1


def _record(
    suffix: str,
    *,
    execution_metadata_override: Mapping[str, Any] | None = None,
) -> SubmissionRecoveryRecord:
    created_at = datetime(2026, 8, 26, tzinfo=timezone.utc) + timedelta(seconds=int(suffix))
    content = f"hello-{suffix}"
    conversation_id = f"conversation-{suffix}"
    message_id = suffix
    task_id = f"task-{suffix}"
    conversation = _canonical(
        {
            "schema": "maf.submission.conversation_projection.v1",
            "conversation_id": conversation_id,
            "username": "owner",
            "status": "active",
            "current_task_id": task_id,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": created_at.isoformat().replace("+00:00", "Z"),
            "create_if_missing": True,
        }
    )
    message = _canonical(
        {
            "schema": "maf.submission.message_projection.v1",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "role": "user",
            "content": content,
            "task_id": task_id,
            "stream_status": "complete",
            "message_created_at": created_at.isoformat().replace("+00:00", "Z"),
            "message_type": "text",
            "metadata": {},
            "updated_at": created_at.isoformat().replace("+00:00", "Z"),
        }
    )
    execution_metadata = {
        "requested_capability_alias": None,
        "canonical_capability_id": None,
        "mcp_dispatch_server_id": None,
        "mcp_binding_mode": None,
        "mcp_command": None,
        "mcp_execution_mode": None,
        "mcp_rollout_config_version": None,
        "mcp_route_reason_code": None,
        "mcp_rollout_mode": None,
        "defer_task_completed_until_pending_skill_context_processed": None,
        "forced_by_mcp_command": None,
        "mcp_shadow_enabled": None,
    }
    if execution_metadata_override is not None:
        execution_metadata = dict(execution_metadata_override)
    continuation = _canonical(
        {
            "schema": "maf.submission.continuation.v1",
            "request_fingerprint": "a" * 64,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "task_id": task_id,
            "owner_scope": "owner",
            "message_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "routing_mode": "auto",
            "requested_capability_id": None,
            "model_options": {
                "model_edition": None,
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
            "bundle_revisions": {
                "skill_bundle_revision": None,
                "mcp_bundle_revision": None,
            },
            "execution_metadata": execution_metadata,
            "upload_refs": [],
            "sheet_selections": {},
            "mcp_binding": None,
            "mcp_assignment": None,
            "available_mcp_servers": [],
            "pending_context": None,
            "initial_no_server_eligible": False,
        }
    )
    return SubmissionRecoveryRecord(
        username="owner",
        conversation_id=conversation_id,
        message_id=message_id,
        task_id=task_id,
        conversation_projection=conversation,
        message_projection=message,
        projection_sha256=hashlib.sha256(
            b"maf.submission.projection.v1\0" + conversation + b"\0" + message
        ).hexdigest(),
        continuation=continuation,
        continuation_sha256=hashlib.sha256(
            b"maf.submission.continuation.v1\0" + continuation
        ).hexdigest(),
        prepared_execution=None,
        prepared_execution_sha256=None,
        phase=SubmissionAdmissionPhase(
            admission_state=SubmissionAdmissionState.OPEN,
            projection_state=SubmissionProjectionState.PENDING,
            preparation_state=SubmissionPreparationState.PENDING,
            handoff_state=SubmissionHandoffState.PENDING,
        ),
        created_at=created_at,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _mutate_preparation(
    preparation: SubmissionPreparationRecord,
    mutate,
) -> SubmissionPreparationRecord:
    value = json.loads(preparation.prepared_execution)
    mutate(value)
    content = _canonical(value)
    schema = value.get("schema")
    domain = (
        b"maf.submission.prepared_execution.v2\0"
        if schema == "maf.submission.prepared_execution.v2"
        else b"maf.submission.prepared_execution.v1\0"
    )
    return replace(
        preparation,
        prepared_execution=content,
        prepared_execution_sha256=hashlib.sha256(domain + content).hexdigest(),
    )


def _with_continuation(
    record: SubmissionRecoveryRecord, **changes: object
) -> SubmissionRecoveryRecord:
    value = json.loads(record.continuation)
    value.update(changes)
    continuation = _canonical(value)
    return replace(
        record,
        continuation=continuation,
        continuation_sha256=hashlib.sha256(
            b"maf.submission.continuation.v1\0" + continuation
        ).hexdigest(),
    )


def _memory(resolved: str | None) -> dict[str, object]:
    return {
        "schema": "maf.submission.memory_preparation.v1",
        "prompt_payload": {
            "current_user_message": "current memory text",
            "recent_messages": [],
            "clarification_messages": [],
            "capability_summaries": [],
            "memory_candidates": [],
            "compression_level": "none",
            "token_budget": 1000,
            "estimated_tokens_before": 10,
            "estimated_tokens_after": 10,
            "truncated": False,
            "resolution_metadata": {},
            **({"resolved_user_message": resolved} if resolved is not None else {}),
        },
        "summary_write": None,
        "event_write": None,
    }


def _no_server_route() -> dict[str, object]:
    return {
        "schema": "maf.submission.route_decision.v1",
        "decision": "no_server",
        "owner_server_set_fingerprint": "b" * 64,
        "available_mcp_servers": [],
    }


def _selector(*, interrupt_kind: str | None) -> dict[str, object]:
    return {
        "decision": "select",
        "reason_code": "candidate_selected",
        "candidate_digest": "c" * 64,
        "resume_action": "resume",
        "upload_ids": [],
        "interrupt_kind": interrupt_kind,
    }


def _bound_memory(
    record: SubmissionRecoveryRecord,
    *,
    summary_conversation_id: str | None = None,
    summary_username: str | None = None,
    event_conversation_id: str | None = None,
    event_task_id: str | None = None,
) -> dict[str, object]:
    conversation_id = summary_conversation_id or record.conversation_id
    username = summary_username or record.username
    summary_subject: dict[str, object] = {
        "schema": "maf.submission.memory_summary_write.v1",
        "summary_id": _stable_memory_summary_id(
            conversation_id=conversation_id,
            username=username,
            covered_until_turn_id="turn-1",
            covered_until_message_id="history-1",
        ),
        "conversation_id": conversation_id,
        "username": username,
        "covered_until_turn_id": "turn-1",
        "covered_until_message_id": "history-1",
        "covered_until_created_at": "2026-08-26T00:00:00Z",
        "summary_text": "summary",
        "source_message_count": 1,
        "source_message_ids_hash": "d" * 64,
        "estimated_tokens": 1,
        "summary_version": SUMMARY_VERSION,
        "compression_policy_version": COMPRESSION_POLICY_VERSION,
        "model_metadata_safe": {},
        "created_at": "2026-08-26T00:00:00Z",
        "updated_at": "2026-08-26T00:00:00Z",
    }
    summary_sha = hashlib.sha256(
        b"maf.submission.memory_summary_write.v1\0"
        + _canonical(summary_subject)
    ).hexdigest()
    summary = {**summary_subject, "summary_sha256": summary_sha}

    bound_event_task = event_task_id or record.task_id
    event_business: dict[str, object] = {
        "schema": "maf.submission.memory_event_write.v1",
        "memory_identity_sha256": summary_sha,
        "conversation_id": event_conversation_id or record.conversation_id,
        "task_id": bound_event_task,
        "node_id": None,
        "agent_id": None,
        "event_type": "conversation_memory_prepared",
        "payload": {"summary_id": summary["summary_id"]},
        "visibility": "internal",
        "created_at": "2026-08-26T00:00:00Z",
    }
    event_subject_sha = hashlib.sha256(
        b"maf.submission.memory_event.subject.v1\0"
        + _canonical(event_business)
    ).hexdigest()
    event_subject = {
        **event_business,
        "event_id": submission_memory_event_id(
            bound_event_task, "conversation_memory_prepared", event_subject_sha
        ),
        "event_subject_sha256": event_subject_sha,
    }
    event_sha = hashlib.sha256(
        b"maf.submission.memory_event_write.v1\0" + _canonical(event_subject)
    ).hexdigest()
    memory = _memory("resolved")
    memory["summary_write"] = {**summary, "summary_sha256": summary_sha}
    memory["event_write"] = {**event_subject, "event_sha256": event_sha}
    return memory


if __name__ == "__main__":
    unittest.main()
