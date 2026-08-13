from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.models import (
    MCPCP7ReadyEpochEvent,
    MCPCP7ReadyEpochEventKind,
    MCPCP7SafetyLedgerRecord,
    MCPCP7SafetyRecordKind,
)
from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.integrations.mcp.cp7_safety import (
    CP7BoundaryEvidence,
    CP7LocalSafetyFacade,
    CP7RuntimeIdentity,
    CP7SafetyStateError,
)
from src.integrations.mcp.rollout_evidence import MCPSafetyRedLine
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


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _signed_record(record: MCPCP7SafetyLedgerRecord) -> MCPCP7SafetyLedgerRecord:
    payload = {
        "candidate_id": record.candidate_id,
        "epoch_id": record.epoch_id,
        "config_fingerprint": record.config_fingerprint,
        "record_kind": record.record_kind.value,
        "red_line": record.red_line,
        "hook_id": record.hook_id,
        "bucket_started_at": None if record.bucket_started_at is None else _utc_text(record.bucket_started_at),
        "bucket_ended_at": None if record.bucket_ended_at is None else _utc_text(record.bucket_ended_at),
        "reason_code": record.reason_code,
        "value": record.value,
        "boundary_source_sha256": record.boundary_source_sha256,
        "recorded_at": _utc_text(record.recorded_at),
    }
    return replace(record, payload_sha256=canonical_sha256(payload))


def _signed_event(event: MCPCP7ReadyEpochEvent) -> MCPCP7ReadyEpochEvent:
    payload = {
        "candidate_id": event.candidate_id,
        "epoch_id": event.epoch_id,
        "predecessor_epoch_id": event.predecessor_epoch_id,
        "event_kind": event.event_kind.value,
        "container_id": event.container_id,
        "image_id": event.image_id,
        "config_fingerprint": event.config_fingerprint,
        "boundary_at": _utc_text(event.boundary_at),
        "audit_device": event.audit_device,
        "audit_inode": event.audit_inode,
        "audit_offset": event.audit_offset,
        "ledger_record_count": event.ledger_record_count,
        "inflight_state_sha256": event.inflight_state_sha256,
    }
    return replace(event, payload_sha256=canonical_sha256(payload))


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
                _signed_record(MCPCP7SafetyLedgerRecord(
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
                ))
            )
        for minute in range(attestation_minutes):
            for index, (red_line, hook_id) in enumerate(RED_LINES):
                await self.storage.append_mcp_cp7_safety_ledger_record(
                    _signed_record(MCPCP7SafetyLedgerRecord(
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
                    ))
                )
        boundaries = (
            (MCPCP7ReadyEpochEventKind.OPENED, 0),
            (MCPCP7ReadyEpochEventKind.READY, 1),
            (MCPCP7ReadyEpochEventKind.CLOSED, 1 + closed_minutes),
        )
        for kind, minute in boundaries:
            await self.storage.append_mcp_cp7_ready_epoch_event(
                _signed_event(MCPCP7ReadyEpochEvent(
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
                ))
            )
        if fork:
            fork_epoch = f"{epoch_id}-fork"
            for kind, minute in boundaries:
                await self.storage.append_mcp_cp7_ready_epoch_event(
                    _signed_event(MCPCP7ReadyEpochEvent(
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
                    ))
                )

    async def test_snapshot_is_deterministic_and_positive_record_latches_guard(self) -> None:
        for index, (red_line, hook_id) in enumerate(RED_LINES):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                _signed_record(MCPCP7SafetyLedgerRecord(
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
                ))
            )
        for minute in range(2):
            for index, (red_line, hook_id) in enumerate(RED_LINES):
                await self.storage.append_mcp_cp7_safety_ledger_record(
                    _signed_record(MCPCP7SafetyLedgerRecord(
                    record_id=f"attestation-{minute}-{index}",
                    candidate_id="candidate-1",
                    epoch_id="epoch-1",
                    config_fingerprint="config-1",
                    record_kind=MCPCP7SafetyRecordKind.ATTESTATION,
                    red_line=red_line,
                    hook_id=hook_id,
                    bucket_started_at=self.at + timedelta(minutes=minute),
                    bucket_ended_at=self.at + timedelta(minutes=minute + 1),
                    reason_code="observed_zero",
                    value=0,
                    boundary_source_sha256=None,
                    payload_sha256=canonical_sha256(
                        {"attestation": index, "minute": minute}
                    ),
                    recorded_at=self.at
                    + timedelta(minutes=minute + 1, microseconds=index),
                    ))
                )
        for offset, kind in enumerate(
            (
                MCPCP7ReadyEpochEventKind.OPENED,
                MCPCP7ReadyEpochEventKind.READY,
                MCPCP7ReadyEpochEventKind.CLOSED,
            )
        ):
            await self.storage.append_mcp_cp7_ready_epoch_event(
                _signed_event(MCPCP7ReadyEpochEvent(
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
                ))
            )
        first = await self.storage.produce_mcp_cp7_safety_snapshot("candidate-1")
        second = await self.storage.produce_mcp_cp7_safety_snapshot("candidate-1")
        self.assertEqual(first.snapshot_sha256, second.snapshot_sha256)
        self.assertFalse(first.invalid_latched)
        gap = _signed_record(MCPCP7SafetyLedgerRecord(
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
        ))
        await self.storage.append_mcp_cp7_safety_ledger_record(gap)
        await self.storage.append_mcp_cp7_safety_ledger_record(gap)
        guard = await self.storage.get_mcp_cp7_candidate_guard("candidate-1")
        self.assertTrue(guard.invalid_latched)
        self.assertEqual(guard.first_invalid_record_id, "gap-1")

    async def test_snapshot_accepts_durable_two_epoch_restart_chain(self) -> None:
        def identity(epoch_id: str, predecessor: str | None = None):
            return CP7RuntimeIdentity(
                candidate_id="candidate-restart",
                epoch_id=epoch_id,
                predecessor_epoch_id=predecessor,
                container_id="container-1",
                image_id="image-1",
                config_fingerprint="config-1",
            )

        def boundary(at: datetime, count: int):
            return CP7BoundaryEvidence(
                boundary_at=at,
                audit_device="device-1",
                audit_inode=1,
                audit_offset=count,
                ledger_record_count=count,
                inflight_state_sha256=canonical_sha256({"at": at.isoformat()}),
            )

        first = CP7LocalSafetyFacade(self.storage, identity("epoch-1"))
        await first.open_epoch(boundary(self.at, 0))
        first_start = self.at
        first_end = first_start + timedelta(minutes=1)
        for detector in first.detectors.values():
            detector.attest_interval(first_start, first_end)
        await first.complete_minute(first_start, first_end)
        await first.mark_ready(boundary(first_end, 16))
        predecessor = await first.begin_verifier_maintenance(
            boundary(first_end, 16),
            verifier_authorized=True,
            requests_stopped=True,
        )

        second = CP7LocalSafetyFacade(
            self.storage, identity("epoch-2", "epoch-1")
        )
        await second.open_epoch(
            boundary(first_end, 16),
            predecessor=predecessor,
            verifier_authorized=True,
        )
        second_start = first_end
        second_end = second_start + timedelta(minutes=1)
        for detector in second.detectors.values():
            detector.attest_interval(second_start, second_end)
        await second.complete_minute(second_start, second_end)
        await second.mark_ready(boundary(second_end, 32))
        snapshot = await second.close_for_approval(
            boundary(second_end, 32), verifier_authorized=True
        )

        self.assertEqual(snapshot.ready_epochs, ("epoch-1", "epoch-2"))
        self.assertEqual(snapshot.observation_started_at, self.at)
        self.assertEqual(snapshot.observation_ended_at, second_end)
        self.assertTrue(
            all(value == 2 for value in snapshot.registration_count_by_red_line.values())
        )
        self.assertEqual(set(snapshot.registration_count_by_red_line), {
            red_line.value for red_line in MCPSafetyRedLine
        })

    async def test_snapshot_rejects_missing_complete_minute_hook(self) -> None:
        for index, (red_line, hook_id) in enumerate(RED_LINES):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                _signed_record(MCPCP7SafetyLedgerRecord(
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
                ))
            )
        for index, (red_line, hook_id) in enumerate(RED_LINES[:-1]):
            await self.storage.append_mcp_cp7_safety_ledger_record(
                _signed_record(MCPCP7SafetyLedgerRecord(
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
                ))
            )
        for offset, kind in enumerate(
            (
                MCPCP7ReadyEpochEventKind.OPENED,
                MCPCP7ReadyEpochEventKind.READY,
                MCPCP7ReadyEpochEventKind.CLOSED,
            )
        ):
            await self.storage.append_mcp_cp7_ready_epoch_event(
                _signed_event(MCPCP7ReadyEpochEvent(
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
                ))
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

    async def test_snapshot_rejects_tampered_payload_and_authoritative_hook(self) -> None:
        await self._seed_candidate(
            "candidate-tamper", closed_minutes=1, attestation_minutes=1
        )
        with self.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER IF EXISTS trg_mcp_cp7_safety_ledger_reject_update"))
            connection.execute(
                text("UPDATE mcp_cp7_safety_ledger SET payload_sha256 = :sha WHERE record_id = :id"),
                {"sha": canonical_sha256({"tampered": True}), "id": "candidate-tamper-registration-0"},
            )
        with self.assertRaisesRegex(RuntimeError, "ledger_payload_tampered"):
            await self.storage.produce_mcp_cp7_safety_snapshot("candidate-tamper")

        await self._seed_candidate(
            "candidate-hook", closed_minutes=1, attestation_minutes=1
        )
        with self.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER IF EXISTS trg_mcp_cp7_safety_ledger_reject_update"))
            connection.execute(text("PRAGMA ignore_check_constraints = ON"))
            connection.execute(
                text("UPDATE mcp_cp7_safety_ledger SET hook_id = :hook WHERE record_id = :id"),
                {"hook": "gateway.endpoint_policy_boundary", "id": "candidate-hook-registration-0"},
            )
            connection.execute(text("PRAGMA ignore_check_constraints = OFF"))
        with self.assertRaisesRegex(RuntimeError, "ledger_payload_tampered|hook_mismatch"):
            await self.storage.produce_mcp_cp7_safety_snapshot("candidate-hook")

    async def test_snapshot_rejects_successor_boundary_identity_drift(self) -> None:
        def identity(epoch_id: str, predecessor: str | None = None):
            return CP7RuntimeIdentity(
                candidate_id="candidate-drift", epoch_id=epoch_id,
                predecessor_epoch_id=predecessor, container_id="container-1",
                image_id="image-1", config_fingerprint="config-1",
            )
        def boundary(at: datetime, count: int):
            return CP7BoundaryEvidence(
                boundary_at=at, audit_device="device-1", audit_inode=1,
                audit_offset=count, ledger_record_count=count,
                inflight_state_sha256=canonical_sha256({"at": at.isoformat()}),
            )
        first = CP7LocalSafetyFacade(self.storage, identity("epoch-1"))
        await first.open_epoch(boundary(self.at, 0))
        end = self.at + timedelta(minutes=1)
        for detector in first.detectors.values():
            detector.attest_interval(self.at, end)
        await first.complete_minute(self.at, end)
        await first.mark_ready(boundary(end, 16))
        predecessor = await first.begin_verifier_maintenance(
            boundary(end, 16), verifier_authorized=True, requests_stopped=True
        )
        drifted = replace(predecessor, container_id="container-recreated")
        second = CP7LocalSafetyFacade(self.storage, identity("epoch-2", "epoch-1"))
        with self.assertRaisesRegex(CP7SafetyStateError, "maintenance_boundary_invalid"):
            await second.open_epoch(
                boundary(end, 16), predecessor=drifted, verifier_authorized=True
            )


if __name__ == "__main__":
    unittest.main()
