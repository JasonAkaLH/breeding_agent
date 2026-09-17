from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.models import MCPCP7SafetySnapshot
from src.integrations.mcp.cp7_safety import (
    CP7BoundaryEvidence,
    CP7LocalSafetyFacade,
    CP7RuntimeIdentity,
    CP7SafetyFatalPersistenceError,
    CP7SafetyStateError,
)
from src.api.runtime import ApiRuntime
from src.integrations.mcp.rollout_evidence import MCPSafetyRedLine


AT = datetime(2026, 8, 13, 2, 0, 10, tzinfo=timezone.utc)
MINUTE = datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc)


class _Storage:
    def __init__(self) -> None:
        self.records = []
        self.events = []
        self.invalid_latched = False
        self.fail_records = False
        self.fail_canary = False
        self.snapshot_end = MINUTE + timedelta(minutes=1)

    async def append_mcp_cp7_safety_ledger_record(self, record):
        if self.fail_records:
            raise RuntimeError("writer unavailable")
        existing = next(
            (item for item in self.records if item.record_id == record.record_id), None
        )
        if existing is not None:
            if existing != record:
                raise RuntimeError("ledger conflict")
            return existing
        self.records.append(record)
        if str(record.record_kind) in {"violation", "gap"}:
            self.invalid_latched = True
        return record

    async def append_mcp_cp7_ready_epoch_event(self, event):
        competing = next(
            (
                item
                for item in self.events
                if item.candidate_id == event.candidate_id
                and item.epoch_id == event.epoch_id
                and item.event_kind == event.event_kind
            ),
            None,
        )
        if competing is not None:
            if competing != event:
                raise RuntimeError("epoch fork")
            return competing
        self.events.append(event)
        return event

    async def get_mcp_cp7_ready_epoch_event(
        self, candidate_id, epoch_id, event_kind
    ):
        return next(
            (
                item
                for item in self.events
                if item.candidate_id == candidate_id
                and item.epoch_id == epoch_id
                and item.event_kind == event_kind
            ),
            None,
        )

    async def get_mcp_cp7_candidate_guard(self, candidate_id):
        if self.fail_canary:
            raise RuntimeError("canary read unavailable")
        if not any(item.candidate_id == candidate_id for item in self.records):
            return None
        return SimpleNamespace(invalid_latched=self.invalid_latched)

    async def produce_mcp_cp7_safety_snapshot(self, candidate_id):
        counts = {red_line.value: 0 for red_line in MCPSafetyRedLine}
        registrations = {red_line.value: 1 for red_line in MCPSafetyRedLine}
        attestations = {red_line.value: 1 for red_line in MCPSafetyRedLine}
        return MCPCP7SafetySnapshot(
            schema="maf.user_mcp.cp7_safety_snapshot.v1",
            candidate_id=candidate_id,
            config_fingerprint="config-1",
            registry_definition_sha256="sha256:definition",
            epoch_chain_sha256="sha256:epoch-chain",
            ready_epochs=("epoch-1",),
            maintenance_boundary_count=0,
            observation_started_at=MINUTE,
            observation_ended_at=self.snapshot_end,
            registration_count_by_red_line=registrations,
            attestation_interval_count_by_red_line=attestations,
            violation_count_by_red_line=counts,
            gap_count=0,
            invalid_latched=False,
            record_count=len(self.records),
            ordered_record_payload_sha256s=tuple(
                item.payload_sha256 for item in self.records
            ),
            snapshot_sha256="sha256:snapshot",
        )


def _identity(
    *, epoch_id: str = "epoch-1", predecessor_epoch_id: str | None = None
) -> CP7RuntimeIdentity:
    return CP7RuntimeIdentity(
        candidate_id="candidate-1",
        epoch_id=epoch_id,
        predecessor_epoch_id=predecessor_epoch_id,
        container_id="container-1",
        image_id="image-1",
        config_fingerprint="config-1",
    )


def _boundary(at: datetime, count: int = 0) -> CP7BoundaryEvidence:
    return CP7BoundaryEvidence(
        boundary_at=at,
        audit_device="device-1",
        audit_inode=1,
        audit_offset=count,
        ledger_record_count=count,
        inflight_state_sha256="sha256:inflight",
    )


def _attest_all(
    facade: CP7LocalSafetyFacade, started_at: datetime, ended_at: datetime
) -> None:
    for detector in facade.detectors.values():
        detector.attest_interval(started_at, ended_at)


class CP7LocalSafetyFacadeTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_persists_opened_exact_registrations_and_canary(self) -> None:
        storage = _Storage()
        facade = CP7LocalSafetyFacade(storage, _identity())

        await facade.open_epoch(_boundary(AT))

        self.assertEqual([str(item.event_kind) for item in storage.events], ["opened"])
        self.assertEqual(len(storage.records), 8)
        self.assertEqual(
            {item.red_line for item in storage.records},
            {red_line.value for red_line in MCPSafetyRedLine},
        )
        self.assertTrue(all(str(item.record_kind) == "registration" for item in storage.records))
        self.assertFalse(facade.ready)

    async def test_full_minute_is_required_before_ready_and_continuous_after_ready(self) -> None:
        storage = _Storage()
        facade = CP7LocalSafetyFacade(storage, _identity())
        await facade.open_epoch(_boundary(AT))

        with self.assertRaisesRegex(ValueError, "full UTC minute"):
            await facade.complete_minute(MINUTE, MINUTE + timedelta(seconds=59))
        _attest_all(facade, MINUTE, MINUTE + timedelta(minutes=1))
        await facade.complete_minute(MINUTE, MINUTE + timedelta(minutes=1))
        await facade.mark_ready(_boundary(MINUTE + timedelta(minutes=1), 16))
        self.assertTrue(facade.ready)

        later = MINUTE + timedelta(minutes=2)
        _attest_all(facade, later, later + timedelta(minutes=1))
        with self.assertRaisesRegex(CP7SafetyStateError, "producer_interval_missed"):
            await facade.complete_minute(later, later + timedelta(minutes=1))
        self.assertTrue(storage.invalid_latched)

    async def test_missing_or_unhealthy_detector_writes_gap_and_latches(self) -> None:
        for unhealthy in (False, True):
            with self.subTest(unhealthy=unhealthy):
                storage = _Storage()
                facade = CP7LocalSafetyFacade(storage, _identity())
                await facade.open_epoch(_boundary(AT))
                if unhealthy:
                    for detector in facade.detectors.values():
                        detector.attest_interval(MINUTE, MINUTE + timedelta(minutes=1))
                    facade.detectors[MCPSafetyRedLine.SECRET_EXPOSURE].mark_unhealthy()
                with self.assertRaisesRegex(CP7SafetyStateError, "could not be attested"):
                    await facade.complete_minute(MINUTE, MINUTE + timedelta(minutes=1))
                self.assertTrue(storage.invalid_latched)
                self.assertEqual(
                    str(storage.records[-1].record_kind),
                    "gap",
                )
                self.assertFalse(facade.ready)

    async def test_violation_latch_permanently_blocks_ready(self) -> None:
        storage = _Storage()
        facade = CP7LocalSafetyFacade(storage, _identity())
        await facade.open_epoch(_boundary(AT))
        await facade.detectors[MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS].report_violation(
            reason_code="endpoint_policy_rejected",
            observed_at=MINUTE,
        )
        self.assertFalse(facade.ready)
        with self.assertRaises(CP7SafetyFatalPersistenceError):
            await facade.mark_ready(_boundary(MINUTE + timedelta(minutes=1), 17))
        self.assertTrue(storage.invalid_latched)

    async def test_writer_or_startup_canary_failure_is_typed_fatal(self) -> None:
        storage = _Storage()
        storage.fail_records = True
        facade = CP7LocalSafetyFacade(storage, _identity())
        with self.assertRaises(CP7SafetyFatalPersistenceError):
            await facade.open_epoch(_boundary(AT))

    async def test_ready_guard_is_reread_for_each_admission(self) -> None:
        storage = _Storage()
        facade = CP7LocalSafetyFacade(storage, _identity())
        await facade.open_epoch(_boundary(AT))
        _attest_all(facade, MINUTE, MINUTE + timedelta(minutes=1))
        await facade.complete_minute(MINUTE, MINUTE + timedelta(minutes=1))
        await facade.mark_ready(_boundary(MINUTE + timedelta(minutes=1), 16))
        self.assertTrue(await facade.ensure_ready())
        storage.invalid_latched = True
        self.assertFalse(await facade.ensure_ready())

    async def test_unexpected_minute_failure_records_exit_and_invokes_fatal_boundary(self) -> None:
        facade = SimpleNamespace(
            ready=True,
            complete_minute=AsyncMock(side_effect=RuntimeError("probe failed")),
            record_unplanned_process_exit=AsyncMock(),
        )
        exits: list[int] = []
        runtime = ApiRuntime.__new__(ApiRuntime)
        runtime._mcp_cp7_safety_facade = facade
        runtime._mcp_cp7_boundary_provider = lambda: _boundary(
            MINUTE + timedelta(minutes=1), 16
        )
        runtime._mcp_cp7_open_boundary = _boundary(AT)
        runtime._mcp_cp7_safety_probes = ()
        runtime._mcp_cp7_fatal_exit = exits.append
        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            await runtime._run_cp7_safety_minutes()
        self.assertEqual(exits, [70])
        facade.record_unplanned_process_exit.assert_awaited_once()

    async def test_shutdown_requires_explicit_verifier_authorization(self) -> None:
        evidence = _boundary(MINUTE + timedelta(minutes=1), 16)
        for authorized in (False, True):
            with self.subTest(authorized=authorized):
                facade = SimpleNamespace(
                    ready=True,
                    begin_verifier_maintenance=AsyncMock(),
                    record_unplanned_process_exit=AsyncMock(),
                )
                runtime = ApiRuntime.__new__(ApiRuntime)
                runtime._mcp_cp7_safety_facade = facade
                runtime._mcp_cp7_boundary_provider = lambda: evidence
                runtime._mcp_cp7_maintenance_authorization = (
                    "verifier-token" if authorized else None
                )
                runtime._mcp_cp7_maintenance_authorizer = (
                    lambda token: token == "verifier-token"
                )
                runtime._mcp_cp7_requests_stopped = True
                runtime._mcp_cp7_fatal_exit = lambda _code: None
                await runtime._close_cp7_safety()
                if authorized:
                    facade.begin_verifier_maintenance.assert_awaited_once_with(
                        evidence,
                        verifier_authorized=True,
                        requests_stopped=True,
                    )
                    facade.record_unplanned_process_exit.assert_not_awaited()
                else:
                    facade.begin_verifier_maintenance.assert_not_awaited()
                    facade.record_unplanned_process_exit.assert_awaited_once_with(
                        evidence.boundary_at
                    )

    async def test_authorized_maintenance_waits_for_next_full_minute_before_quiesce(self) -> None:
        runtime = ApiRuntime.__new__(ApiRuntime)
        runtime._mcp_cp7_safety_facade = SimpleNamespace(ready=True)
        runtime._mcp_cp7_maintenance_authorization = "verifier-token"
        runtime._mcp_cp7_maintenance_authorizer = lambda token: token == "verifier-token"
        runtime._mcp_cp7_requests_stopped = False
        runtime._mcp_cp7_clock = lambda: datetime(
            2026, 8, 13, 2, 0, 15, tzinfo=timezone.utc
        )
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            self.assertFalse(runtime._mcp_cp7_requests_stopped)
            sleeps.append(delay)

        runtime._mcp_cp7_sleep = sleep
        await runtime._quiesce_cp7_for_shutdown()
        self.assertEqual(sleeps, [45.0])
        self.assertTrue(runtime._mcp_cp7_requests_stopped)

        storage = _Storage()
        storage.fail_canary = True
        facade = CP7LocalSafetyFacade(storage, _identity())
        with self.assertRaisesRegex(CP7SafetyFatalPersistenceError, "startup canary"):
            await facade.open_epoch(_boundary(AT))

    async def test_verifier_maintenance_requires_closed_minute_and_no_recreate(self) -> None:
        storage = _Storage()
        first = CP7LocalSafetyFacade(storage, _identity())
        await first.open_epoch(_boundary(AT))
        _attest_all(first, MINUTE, MINUTE + timedelta(minutes=1))
        await first.complete_minute(MINUTE, MINUTE + timedelta(minutes=1))
        closed_at = MINUTE + timedelta(minutes=1)
        await first.mark_ready(_boundary(closed_at, 16))
        close = await first.begin_verifier_maintenance(
            _boundary(closed_at, 16),
            verifier_authorized=True,
            requests_stopped=True,
        )

        successor = CP7LocalSafetyFacade(
            storage, _identity(epoch_id="epoch-2", predecessor_epoch_id="epoch-1")
        )
        await successor.open_epoch(
            _boundary(closed_at, 16),
            predecessor=close,
            verifier_authorized=True,
        )
        self.assertFalse(successor.ready)

        drifted = CP7LocalSafetyFacade(
            storage,
            CP7RuntimeIdentity(
                candidate_id="candidate-1",
                epoch_id="epoch-3",
                predecessor_epoch_id="epoch-1",
                container_id="recreated-container",
                image_id="image-1",
                config_fingerprint="config-1",
            ),
        )
        with self.assertRaisesRegex(CP7SafetyStateError, "maintenance_boundary_invalid"):
            await drifted.open_epoch(
                _boundary(closed_at, 16),
                predecessor=close,
                verifier_authorized=True,
            )
        self.assertTrue(storage.invalid_latched)

    async def test_epoch_fork_is_fatal_and_approval_snapshot_ends_at_close(self) -> None:
        storage = _Storage()
        first = CP7LocalSafetyFacade(storage, _identity())
        await first.open_epoch(_boundary(AT))
        fork = CP7LocalSafetyFacade(storage, _identity())
        with self.assertRaisesRegex(CP7SafetyFatalPersistenceError, "epoch append"):
            await fork.open_epoch(_boundary(AT + timedelta(seconds=1)))

        storage = _Storage()
        facade = CP7LocalSafetyFacade(storage, _identity())
        await facade.open_epoch(_boundary(AT))
        _attest_all(facade, MINUTE, MINUTE + timedelta(minutes=1))
        await facade.complete_minute(MINUTE, MINUTE + timedelta(minutes=1))
        close_at = MINUTE + timedelta(minutes=1)
        await facade.mark_ready(_boundary(close_at, 16))
        snapshot = await facade.close_for_approval(
            _boundary(close_at, 16), verifier_authorized=True
        )
        self.assertEqual(snapshot.observation_ended_at, close_at)
        self.assertFalse(facade.ready)

    async def test_unplanned_exit_writes_closed_gap(self) -> None:
        storage = _Storage()
        facade = CP7LocalSafetyFacade(storage, _identity())
        await facade.open_epoch(_boundary(AT))
        with self.assertRaisesRegex(CP7SafetyStateError, "unplanned_process_exit"):
            await facade.record_unplanned_process_exit(MINUTE)
        self.assertEqual(storage.records[-1].reason_code, "unplanned_process_exit")
        self.assertTrue(storage.invalid_latched)


if __name__ == "__main__":
    unittest.main()
