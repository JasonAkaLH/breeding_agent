from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import (
    MCPCP7ReadyEpochEvent,
    MCPCP7ReadyEpochEventKind,
    MCPCP7SafetyLedgerRecord,
    MCPCP7SafetyRecordKind,
)
from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.storage.sqlite.bootstrap import bootstrap_sqlite_database
from src.storage.sqlite.repositories import SQLiteStorage


RED_LINES = (
    ("cross_user_access", "gateway.task_owner_boundary"),
    ("secret_exposure", "audit.secret_payload_boundary"),
    ("dual_tool_call", "dispatch.durable_call_idempotency_boundary"),
    ("unauthorized_tool_call", "dispatch.permission_boundary"),
    ("endpoint_policy_bypass", "gateway.endpoint_policy_boundary"),
    ("unknown_result_replay", "recovery.unknown_replay_boundary"),
    ("shadow_tool_call", "gateway.persisted_assignment_boundary"),
    ("persistent_resource_leak", "gateway.resource_cleanup_boundary"),
)


class MCPCP7SafetyLedgerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(self.temp_dir.name) / 'state.db'}"
        )
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(sessionmaker(bind=self.engine, expire_on_commit=False))
        self.at = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def _seed_candidate(
        self,
        candidate_id: str,
        *,
        closed_minutes: int,
        attestation_minutes: int,
        fork: bool = False,
    ) -> None:
        epoch_id = f"epoch-{candidate_id}"
        for index, (red_line, hook_id) in enumerate(RED_LINES):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                MCPCP7SafetyLedgerRecord(
                    record_id=f"{candidate_id}-registration-{index}",
                    candidate_id=candidate_id,
                    epoch_id=epoch_id,
                    config_fingerprint="config-1",
                    record_kind=MCPCP7SafetyRecordKind.REGISTRATION,
                    red_line=red_line,
                    hook_id=hook_id,
                    bucket_started_at=None,
                    bucket_ended_at=None,
                    reason_code="registered",
                    value=0,
                    boundary_source_sha256=None,
                    payload_sha256=canonical_sha256(
                        {"candidate": candidate_id, "registration": index}
                    ),
                    recorded_at=self.at + timedelta(microseconds=index),
                )
            )
        for minute in range(attestation_minutes):
            for index, (red_line, hook_id) in enumerate(RED_LINES):
                await self.storage.append_mcp_cp7_safety_ledger_record(
                    MCPCP7SafetyLedgerRecord(
                        record_id=f"{candidate_id}-attestation-{minute}-{index}",
                        candidate_id=candidate_id,
                        epoch_id=epoch_id,
                        config_fingerprint="config-1",
                        record_kind=MCPCP7SafetyRecordKind.ATTESTATION,
                        red_line=red_line,
                        hook_id=hook_id,
                        bucket_started_at=self.at + timedelta(minutes=1 + minute),
                        bucket_ended_at=self.at + timedelta(minutes=2 + minute),
                        reason_code="observed_zero",
                        value=0,
                        boundary_source_sha256=None,
                        payload_sha256=canonical_sha256(
                            {
                                "candidate": candidate_id,
                                "attestation": index,
                                "minute": minute,
                            }
                        ),
                        recorded_at=self.at
                        + timedelta(minutes=2 + minute, microseconds=index),
                    )
                )
        boundaries = (
            (MCPCP7ReadyEpochEventKind.OPENED, 0),
            (MCPCP7ReadyEpochEventKind.READY, 1),
            (MCPCP7ReadyEpochEventKind.CLOSED, 1 + closed_minutes),
        )
        for kind, minute in boundaries:
            await self.storage.append_mcp_cp7_ready_epoch_event(
                MCPCP7ReadyEpochEvent(
                    event_id=f"{candidate_id}-{kind.value}",
                    candidate_id=candidate_id,
                    epoch_id=epoch_id,
                    predecessor_epoch_id=None,
                    event_kind=kind,
                    container_id="container-1",
                    image_id="image-1",
                    config_fingerprint="config-1",
                    boundary_at=self.at + timedelta(minutes=minute),
                    audit_device="device-1",
                    audit_inode=1,
                    audit_offset=minute,
                    ledger_record_count=8 + 8 * attestation_minutes,
                    inflight_state_sha256=canonical_sha256(
                        {"candidate": candidate_id, "inflight": minute}
                    ),
                    payload_sha256=canonical_sha256(
                        {"candidate": candidate_id, "event": kind.value}
                    ),
                )
            )
        if fork:
            fork_epoch = f"{epoch_id}-fork"
            for kind, minute in boundaries:
                await self.storage.append_mcp_cp7_ready_epoch_event(
                    MCPCP7ReadyEpochEvent(
                        event_id=f"{candidate_id}-fork-{kind.value}",
                        candidate_id=candidate_id,
                        epoch_id=fork_epoch,
                        predecessor_epoch_id=None,
                        event_kind=kind,
                        container_id="container-1",
                        image_id="image-1",
                        config_fingerprint="config-1",
                        boundary_at=self.at + timedelta(minutes=10 + minute),
                        audit_device="device-1",
                        audit_inode=1,
                        audit_offset=10 + minute,
                        ledger_record_count=8 + 8 * attestation_minutes,
                        inflight_state_sha256=canonical_sha256(
                            {"candidate": candidate_id, "fork": minute}
                        ),
                        payload_sha256=canonical_sha256(
                            {"candidate": candidate_id, "fork-event": kind.value}
                        ),
                    )
                )

    async def test_snapshot_is_deterministic_and_positive_record_latches_guard(self) -> None:
        for index, (red_line, hook_id) in enumerate(RED_LINES):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                MCPCP7SafetyLedgerRecord(
                    record_id=f"registration-{index}",
                    candidate_id="candidate-1",
                    epoch_id="epoch-1",
                    config_fingerprint="config-1",
                    record_kind=MCPCP7SafetyRecordKind.REGISTRATION,
                    red_line=red_line,
                    hook_id=hook_id,
                    bucket_started_at=None,
                    bucket_ended_at=None,
                    reason_code="registered",
                    value=0,
                    boundary_source_sha256=None,
                    payload_sha256=canonical_sha256({"registration": index}),
                    recorded_at=self.at + timedelta(microseconds=index),
                )
            )
        for index, (red_line, hook_id) in enumerate(RED_LINES):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                MCPCP7SafetyLedgerRecord(
                    record_id=f"attestation-{index}",
                    candidate_id="candidate-1",
                    epoch_id="epoch-1",
                    config_fingerprint="config-1",
                    record_kind=MCPCP7SafetyRecordKind.ATTESTATION,
                    red_line=red_line,
                    hook_id=hook_id,
                    bucket_started_at=self.at + timedelta(minutes=1),
                    bucket_ended_at=self.at + timedelta(minutes=2),
                    reason_code="observed_zero",
                    value=0,
                    boundary_source_sha256=None,
                    payload_sha256=canonical_sha256({"attestation": index}),
                    recorded_at=self.at + timedelta(minutes=2, microseconds=index),
                )
            )
        for offset, kind in enumerate(
            (
                MCPCP7ReadyEpochEventKind.OPENED,
                MCPCP7ReadyEpochEventKind.READY,
                MCPCP7ReadyEpochEventKind.CLOSED,
            )
        ):
            await self.storage.append_mcp_cp7_ready_epoch_event(
                MCPCP7ReadyEpochEvent(
                    event_id=f"epoch-{kind.value}",
                    candidate_id="candidate-1",
                    epoch_id="epoch-1",
                    predecessor_epoch_id=None,
                    event_kind=kind,
                    container_id="container-1",
                    image_id="image-1",
                    config_fingerprint="config-1",
                    boundary_at=self.at + timedelta(minutes=offset),
                    audit_device="device-1",
                    audit_inode=1,
                    audit_offset=offset,
                    ledger_record_count=8,
                    inflight_state_sha256=canonical_sha256({"inflight": offset}),
                    payload_sha256=canonical_sha256({"event": kind.value}),
                )
            )
        first = await self.storage.produce_mcp_cp7_safety_snapshot("candidate-1")
        second = await self.storage.produce_mcp_cp7_safety_snapshot("candidate-1")
        self.assertEqual(first.snapshot_sha256, second.snapshot_sha256)
        self.assertFalse(first.invalid_latched)
        gap = MCPCP7SafetyLedgerRecord(
            record_id="gap-1",
            candidate_id="candidate-1",
            epoch_id="epoch-1",
            config_fingerprint="config-1",
            record_kind=MCPCP7SafetyRecordKind.GAP,
            red_line=None,
            hook_id=None,
            bucket_started_at=None,
            bucket_ended_at=None,
            reason_code="producer_interval_missed",
            value=1,
            boundary_source_sha256=canonical_sha256({"boundary": 1}),
            payload_sha256=canonical_sha256({"gap": 1}),
            recorded_at=self.at + timedelta(minutes=3),
        )
        await self.storage.append_mcp_cp7_safety_ledger_record(gap)
        await self.storage.append_mcp_cp7_safety_ledger_record(gap)
        guard = await self.storage.get_mcp_cp7_candidate_guard("candidate-1")
        self.assertTrue(guard.invalid_latched)
        self.assertEqual(guard.first_invalid_record_id, "gap-1")

    async def test_snapshot_rejects_missing_complete_minute_hook(self) -> None:
        for index, (red_line, hook_id) in enumerate(RED_LINES):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                MCPCP7SafetyLedgerRecord(
                    record_id=f"registration-missing-{index}",
                    candidate_id="candidate-missing",
                    epoch_id="epoch-missing",
                    config_fingerprint="config-1",
                    record_kind=MCPCP7SafetyRecordKind.REGISTRATION,
                    red_line=red_line,
                    hook_id=hook_id,
                    bucket_started_at=None,
                    bucket_ended_at=None,
                    reason_code="registered",
                    value=0,
                    boundary_source_sha256=None,
                    payload_sha256=canonical_sha256({"registration-missing": index}),
                    recorded_at=self.at + timedelta(microseconds=index),
                )
            )
        for index, (red_line, hook_id) in enumerate(RED_LINES[:-1]):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                MCPCP7SafetyLedgerRecord(
                    record_id=f"attestation-missing-{index}",
                    candidate_id="candidate-missing",
                    epoch_id="epoch-missing",
                    config_fingerprint="config-1",
                    record_kind=MCPCP7SafetyRecordKind.ATTESTATION,
                    red_line=red_line,
                    hook_id=hook_id,
                    bucket_started_at=self.at + timedelta(minutes=1),
                    bucket_ended_at=self.at + timedelta(minutes=2),
                    reason_code="observed_zero",
                    value=0,
                    boundary_source_sha256=None,
                    payload_sha256=canonical_sha256({"attestation-missing": index}),
                    recorded_at=self.at + timedelta(minutes=2, microseconds=index),
                )
            )
        for offset, kind in enumerate(
            (
                MCPCP7ReadyEpochEventKind.OPENED,
                MCPCP7ReadyEpochEventKind.READY,
                MCPCP7ReadyEpochEventKind.CLOSED,
            )
        ):
            await self.storage.append_mcp_cp7_ready_epoch_event(
                MCPCP7ReadyEpochEvent(
                    event_id=f"missing-{kind.value}",
                    candidate_id="candidate-missing",
                    epoch_id="epoch-missing",
                    predecessor_epoch_id=None,
                    event_kind=kind,
                    container_id="container-1",
                    image_id="image-1",
                    config_fingerprint="config-1",
                    boundary_at=self.at + timedelta(minutes=offset),
                    audit_device="device-1",
                    audit_inode=1,
                    audit_offset=offset,
                    ledger_record_count=15,
                    inflight_state_sha256=canonical_sha256({"missing-inflight": offset}),
                    payload_sha256=canonical_sha256({"missing-event": kind.value}),
                )
            )
        with self.assertRaisesRegex(RuntimeError, "attestation_coverage_invalid"):
            await self.storage.produce_mcp_cp7_safety_snapshot("candidate-missing")

    async def test_snapshot_rejects_zero_attestations_missing_minute_and_epoch_fork(self) -> None:
        cases = (
            ("candidate-zero", 1, 0, False, "attestation_coverage_invalid"),
            ("candidate-minute-gap", 2, 1, False, "attestation_coverage_invalid"),
            ("candidate-fork", 1, 1, True, "epoch_chain_invalid"),
        )
        for candidate_id, closed_minutes, attestation_minutes, fork, error in cases:
            with self.subTest(candidate_id=candidate_id):
                await self._seed_candidate(
                    candidate_id,
                    closed_minutes=closed_minutes,
                    attestation_minutes=attestation_minutes,
                    fork=fork,
                )
                with self.assertRaisesRegex(RuntimeError, error):
                    await self.storage.produce_mcp_cp7_safety_snapshot(candidate_id)


if __name__ == "__main__":
    unittest.main()
