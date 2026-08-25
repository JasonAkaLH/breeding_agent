from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
import os

from src.core.contracts import MCPCP7StoragePort
from src.core.models import (
    MCPCP7ReadyEpochEvent,
    MCPCP7ReadyEpochEventKind,
    MCPCP7SafetyLedgerRecord,
    MCPCP7SafetyRecordKind,
    MCPCP7SafetySnapshot,
)

from .cp7_artifacts import canonical_sha256
from .rollout_evidence import MCPMetricRoutingMode, MCPSafetyRedLine
from .safety_detectors import (
    AUTHORITATIVE_MCP_SAFETY_HOOKS,
    AuthoritativeMCPSafetyDetector,
    AuthoritativeMCPSafetyDetectorRegistry,
    MCPSafetyMetricGap,
    register_authoritative_mcp_safety_detectors,
)


class CP7SafetyError(RuntimeError):
    """Base error for the fail-closed CP7-local safety lifecycle."""


class CP7SafetyFatalPersistenceError(CP7SafetyError):
    """Durable safety evidence could not be written or read back."""


class CP7SafetyStateError(CP7SafetyError):
    """A caller attempted an invalid or unsafe epoch transition."""


@dataclass(frozen=True, slots=True)
class CP7RuntimeIdentity:
    candidate_id: str
    epoch_id: str
    predecessor_epoch_id: str | None
    container_id: str
    image_id: str
    config_fingerprint: str


@dataclass(frozen=True, slots=True)
class CP7BoundaryEvidence:
    boundary_at: datetime
    audit_device: str
    audit_inode: int
    audit_offset: int
    ledger_record_count: int
    inflight_state_sha256: str


@dataclass(frozen=True, slots=True)
class CP7PredecessorClose:
    candidate_id: str
    epoch_id: str
    container_id: str
    image_id: str
    config_fingerprint: str
    closed_boundary: CP7BoundaryEvidence


class _CP7LedgerRecorder:
    def __init__(self, facade: "CP7LocalSafetyFacade") -> None:
        self._facade = facade

    async def record_count(self, _metric_name: Any, **kwargs: Any) -> MCPCP7SafetyLedgerRecord:
        labels = kwargs["labels"]
        red_line = labels.red_line
        if not isinstance(red_line, MCPSafetyRedLine):
            raise CP7SafetyStateError("CP7 safety ledger red line is not closed")
        value = kwargs["value"]
        kind = (
            MCPCP7SafetyRecordKind.ATTESTATION
            if value == 0
            else MCPCP7SafetyRecordKind.VIOLATION
        )
        reason_code = (
            "observed_zero"
            if value == 0
            else None
        )
        if reason_code is None:
            raise CP7SafetyStateError("CP7 violation has no closed reason")
        started_at = kwargs["bucket_started_at"]
        ended_at = kwargs["bucket_ended_at"]
        return await self._facade._append_record(
            kind=kind,
            red_line=red_line,
            reason_code=reason_code,
            bucket_started_at=started_at,
            bucket_ended_at=ended_at,
            boundary_source_sha256=None,
            recorded_at=ended_at,
        )

    async def record_safety_violation(
        self,
        *,
        red_line: MCPSafetyRedLine,
        reason_code: str,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
    ) -> MCPCP7SafetyLedgerRecord:
        boundary_source_sha256 = canonical_sha256(
            {
                "schema": "maf.user_mcp.cp7_safety_boundary.v1",
                "candidate_id": self._facade.identity.candidate_id,
                "epoch_id": self._facade.identity.epoch_id,
                "red_line": red_line.value,
                "hook_id": AUTHORITATIVE_MCP_SAFETY_HOOKS[red_line],
                "reason_code": reason_code,
                "bucket_started_at": _utc_text(bucket_started_at),
                "bucket_ended_at": _utc_text(bucket_ended_at),
            }
        )
        return await self._facade._append_record(
            kind=MCPCP7SafetyRecordKind.VIOLATION,
            red_line=red_line,
            reason_code=reason_code,
            bucket_started_at=bucket_started_at,
            bucket_ended_at=bucket_ended_at,
            boundary_source_sha256=boundary_source_sha256,
            recorded_at=bucket_ended_at,
        )


class CP7LocalSafetyFacade:
    """Candidate-local safety and Ready lifecycle independent of rollout admission."""

    def __init__(
        self,
        storage: MCPCP7StoragePort,
        identity: CP7RuntimeIdentity,
        *,
        fatal_exit: Callable[[int], None] = lambda _code: None,
    ) -> None:
        _validate_identity(identity)
        self._storage = storage
        self.identity = identity
        self._opened_at: datetime | None = None
        self._ready_at: datetime | None = None
        self._closed_at: datetime | None = None
        self._last_attested_end: datetime | None = None
        self._fatal = False
        self._fatal_exit = fatal_exit
        self._fatal_exit_invoked = False
        self._registry = AuthoritativeMCPSafetyDetectorRegistry(
            _CP7LedgerRecorder(self),
            gap_sink=self._persist_gap,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
        )
        self.detectors = register_authoritative_mcp_safety_detectors(self._registry)

    @property
    def registry(self) -> AuthoritativeMCPSafetyDetectorRegistry:
        return self._registry

    @property
    def ready(self) -> bool:
        return self._ready_at is not None and self._closed_at is None and not self._fatal

    async def ensure_ready(self) -> bool:
        if not self.ready:
            return False
        guard = await self._durable_call(
            "request admission guard",
            self._storage.get_mcp_cp7_candidate_guard,
            self.identity.candidate_id,
        )
        if guard is None or guard.invalid_latched:
            self._fatal = True
            return False
        return True

    @property
    def opened(self) -> bool:
        return self._opened_at is not None and self._closed_at is None and not self._fatal

    async def open_epoch(
        self,
        evidence: CP7BoundaryEvidence,
        *,
        predecessor: CP7PredecessorClose | None = None,
        verifier_authorized: bool = False,
    ) -> None:
        self._require_live()
        if self._opened_at is not None:
            raise CP7SafetyStateError("CP7 safety epoch is already opened")
        _validate_boundary(evidence)
        if self.identity.predecessor_epoch_id is None:
            if predecessor is not None:
                raise CP7SafetyStateError("first CP7 epoch cannot have a predecessor")
        else:
            durable = await self._durable_call(
                "predecessor close read",
                self._storage.get_mcp_cp7_ready_epoch_event,
                self.identity.candidate_id,
                self.identity.predecessor_epoch_id,
                MCPCP7ReadyEpochEventKind.CLOSED,
            )
            if (
                not verifier_authorized
                or not _predecessor_matches(self.identity, predecessor)
                or predecessor is None
                or durable is None
                or not _durable_predecessor_matches(predecessor, durable)
                or evidence.boundary_at != durable.boundary_at
            ):
                await self._fatal_gap(
                    "maintenance_boundary_invalid",
                    evidence.boundary_at,
                    evidence.boundary_at,
                )
        await self._append_epoch_event(MCPCP7ReadyEpochEventKind.OPENED, evidence)
        self._opened_at = evidence.boundary_at
        for red_line in MCPSafetyRedLine:
            await self._append_record(
                kind=MCPCP7SafetyRecordKind.REGISTRATION,
                red_line=red_line,
                reason_code="registered",
                bucket_started_at=None,
                bucket_ended_at=None,
                boundary_source_sha256=None,
                recorded_at=evidence.boundary_at,
            )
        guard = await self._durable_call(
            "startup canary", self._storage.get_mcp_cp7_candidate_guard,
            self.identity.candidate_id,
        )
        if guard is None or guard.invalid_latched:
            raise CP7SafetyStateError("CP7 candidate guard rejects startup")

    async def complete_minute(self, started_at: datetime, ended_at: datetime) -> None:
        self._require_open()
        _validate_full_minute(started_at, ended_at)
        first_allowed = _next_minute(self._opened_at)
        expected_start = self._last_attested_end or first_allowed
        if started_at != expected_start:
            await self._fatal_gap("producer_interval_missed", expected_start, ended_at)
        try:
            await self._registry.record_verified_zero_series(
                bucket_started_at=started_at, bucket_ended_at=ended_at
            )
        except CP7SafetyFatalPersistenceError:
            raise
        except Exception as exc:
            raise CP7SafetyStateError("CP7 safety minute could not be attested") from exc
        self._last_attested_end = ended_at

    async def mark_ready(self, evidence: CP7BoundaryEvidence) -> None:
        self._require_open()
        _validate_boundary(evidence)
        if self._ready_at is not None:
            raise CP7SafetyStateError("CP7 safety epoch is already Ready")
        if self._last_attested_end is None or evidence.boundary_at != self._last_attested_end:
            await self._fatal_gap(
                "maintenance_boundary_invalid", evidence.boundary_at, evidence.boundary_at
            )
        guard = await self._durable_call(
            "Ready guard", self._storage.get_mcp_cp7_candidate_guard,
            self.identity.candidate_id,
        )
        if guard is None or guard.invalid_latched:
            raise CP7SafetyStateError("CP7 candidate guard rejects Ready")
        await self._append_epoch_event(MCPCP7ReadyEpochEventKind.READY, evidence)
        self._ready_at = evidence.boundary_at

    async def begin_verifier_maintenance(
        self,
        evidence: CP7BoundaryEvidence,
        *,
        verifier_authorized: bool,
        requests_stopped: bool,
    ) -> CP7PredecessorClose:
        if not verifier_authorized or not requests_stopped:
            await self._fatal_gap(
                "maintenance_boundary_invalid", evidence.boundary_at, evidence.boundary_at
            )
        self._require_ready_boundary(evidence)
        await self._append_epoch_event(
            MCPCP7ReadyEpochEventKind.MAINTENANCE_STARTED, evidence
        )
        await self._append_epoch_event(MCPCP7ReadyEpochEventKind.CLOSED, evidence)
        self._closed_at = evidence.boundary_at
        return CP7PredecessorClose(
            candidate_id=self.identity.candidate_id,
            epoch_id=self.identity.epoch_id,
            container_id=self.identity.container_id,
            image_id=self.identity.image_id,
            config_fingerprint=self.identity.config_fingerprint,
            closed_boundary=evidence,
        )

    async def close_for_approval(
        self, evidence: CP7BoundaryEvidence, *, verifier_authorized: bool
    ) -> MCPCP7SafetySnapshot:
        if not verifier_authorized:
            await self._fatal_gap(
                "maintenance_boundary_invalid", evidence.boundary_at, evidence.boundary_at
            )
        self._require_ready_boundary(evidence)
        await self._append_epoch_event(MCPCP7ReadyEpochEventKind.CLOSED, evidence)
        self._closed_at = evidence.boundary_at
        snapshot = await self._durable_call(
            "approval snapshot", self._storage.produce_mcp_cp7_safety_snapshot,
            self.identity.candidate_id,
        )
        if (
            snapshot.candidate_id != self.identity.candidate_id
            or snapshot.config_fingerprint != self.identity.config_fingerprint
            or snapshot.observation_ended_at != evidence.boundary_at
            or snapshot.invalid_latched
            or snapshot.gap_count != 0
            or any(snapshot.violation_count_by_red_line.values())
        ):
            raise CP7SafetyStateError("CP7 approval snapshot is not continuous")
        return snapshot

    async def record_unplanned_process_exit(self, observed_at: datetime) -> None:
        self._require_open()
        bucket_start = observed_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        await self._fatal_gap(
            "unplanned_process_exit", bucket_start, bucket_start + timedelta(minutes=1)
        )

    async def report_violation(
        self,
        red_line: MCPSafetyRedLine,
        *,
        reason_code: str,
        observed_at: datetime,
    ) -> Any:
        self._require_open()
        return await self.detectors[red_line].report_violation(
            reason_code=reason_code, observed_at=observed_at
        )

    def _require_live(self) -> None:
        if self._fatal:
            raise CP7SafetyFatalPersistenceError("CP7 safety lifecycle is fatal")

    def _require_open(self) -> None:
        self._require_live()
        if self._opened_at is None or self._closed_at is not None:
            raise CP7SafetyStateError("CP7 safety epoch is not open")

    def _require_ready_boundary(self, evidence: CP7BoundaryEvidence) -> None:
        self._require_open()
        _validate_boundary(evidence)
        if not self.ready or evidence.boundary_at != self._last_attested_end:
            raise CP7SafetyStateError("CP7 maintenance requires the last full minute")

    async def _persist_gap(self, gap: MCPSafetyMetricGap) -> None:
        red_line = gap.red_line
        boundary_source = canonical_sha256(
            {
                "schema": "maf.user_mcp.cp7_safety_gap_boundary.v1",
                "candidate_id": self.identity.candidate_id,
                "epoch_id": self.identity.epoch_id,
                "reason_code": gap.reason_code,
                "red_line": None if red_line is None else red_line.value,
                "bucket_started_at": _utc_text(gap.bucket_started_at),
                "bucket_ended_at": _utc_text(gap.bucket_ended_at),
            }
        )
        await self._append_record(
            kind=MCPCP7SafetyRecordKind.GAP,
            red_line=red_line,
            reason_code=gap.reason_code,
            bucket_started_at=gap.bucket_started_at,
            bucket_ended_at=gap.bucket_ended_at,
            boundary_source_sha256=boundary_source,
            recorded_at=gap.bucket_ended_at,
        )

    async def _fatal_gap(
        self, reason_code: str, started_at: datetime, ended_at: datetime
    ) -> None:
        if ended_at <= started_at:
            ended_at = started_at + timedelta(minutes=1)
        await self._persist_gap(
            MCPSafetyMetricGap(
                reason_code=reason_code,
                bucket_started_at=started_at,
                bucket_ended_at=ended_at,
            )
        )
        raise CP7SafetyStateError(f"CP7 safety gap: {reason_code}")

    async def _append_record(
        self,
        *,
        kind: MCPCP7SafetyRecordKind,
        red_line: MCPSafetyRedLine | None,
        reason_code: str,
        bucket_started_at: datetime | None,
        bucket_ended_at: datetime | None,
        boundary_source_sha256: str | None,
        recorded_at: datetime,
    ) -> MCPCP7SafetyLedgerRecord:
        payload = {
            "candidate_id": self.identity.candidate_id,
            "epoch_id": self.identity.epoch_id,
            "config_fingerprint": self.identity.config_fingerprint,
            "record_kind": kind.value,
            "red_line": None if red_line is None else red_line.value,
            "hook_id": None if red_line is None else AUTHORITATIVE_MCP_SAFETY_HOOKS[red_line],
            "bucket_started_at": None if bucket_started_at is None else _utc_text(bucket_started_at),
            "bucket_ended_at": None if bucket_ended_at is None else _utc_text(bucket_ended_at),
            "reason_code": reason_code,
            "value": 0 if kind in {MCPCP7SafetyRecordKind.REGISTRATION, MCPCP7SafetyRecordKind.ATTESTATION} else 1,
            "boundary_source_sha256": boundary_source_sha256,
            "recorded_at": _utc_text(recorded_at),
        }
        payload_sha = canonical_sha256(payload)
        record = MCPCP7SafetyLedgerRecord(
            record_id=f"mcp-cp7-safety:v1:{payload_sha.removeprefix('sha256:')}",
            payload_sha256=payload_sha,
            candidate_id=self.identity.candidate_id,
            epoch_id=self.identity.epoch_id,
            config_fingerprint=self.identity.config_fingerprint,
            record_kind=kind,
            red_line=payload["red_line"],
            hook_id=payload["hook_id"],
            bucket_started_at=bucket_started_at,
            bucket_ended_at=bucket_ended_at,
            reason_code=reason_code,
            value=payload["value"],
            boundary_source_sha256=boundary_source_sha256,
            recorded_at=recorded_at,
        )
        persisted = await self._durable_call(
            "ledger append", self._storage.append_mcp_cp7_safety_ledger_record, record
        )
        if kind in {MCPCP7SafetyRecordKind.VIOLATION, MCPCP7SafetyRecordKind.GAP}:
            self._fatal = True
        return persisted

    async def _append_epoch_event(
        self, kind: MCPCP7ReadyEpochEventKind, evidence: CP7BoundaryEvidence
    ) -> MCPCP7ReadyEpochEvent:
        payload = {
            "candidate_id": self.identity.candidate_id,
            "epoch_id": self.identity.epoch_id,
            "predecessor_epoch_id": self.identity.predecessor_epoch_id,
            "event_kind": kind.value,
            "container_id": self.identity.container_id,
            "image_id": self.identity.image_id,
            "config_fingerprint": self.identity.config_fingerprint,
            "boundary_at": _utc_text(evidence.boundary_at),
            "audit_device": evidence.audit_device,
            "audit_inode": evidence.audit_inode,
            "audit_offset": evidence.audit_offset,
            "ledger_record_count": evidence.ledger_record_count,
            "inflight_state_sha256": evidence.inflight_state_sha256,
        }
        payload_sha = canonical_sha256(payload)
        event = MCPCP7ReadyEpochEvent(
            event_id=f"mcp-cp7-epoch:v1:{payload_sha.removeprefix('sha256:')}",
            payload_sha256=payload_sha,
            candidate_id=self.identity.candidate_id,
            epoch_id=self.identity.epoch_id,
            predecessor_epoch_id=self.identity.predecessor_epoch_id,
            event_kind=kind,
            container_id=self.identity.container_id,
            image_id=self.identity.image_id,
            config_fingerprint=self.identity.config_fingerprint,
            boundary_at=evidence.boundary_at,
            audit_device=evidence.audit_device,
            audit_inode=evidence.audit_inode,
            audit_offset=evidence.audit_offset,
            ledger_record_count=evidence.ledger_record_count,
            inflight_state_sha256=evidence.inflight_state_sha256,
        )
        return await self._durable_call(
            "epoch append", self._storage.append_mcp_cp7_ready_epoch_event, event
        )

    async def _durable_call(self, label: str, function: Any, *args: Any) -> Any:
        try:
            return await function(*args)
        except Exception as exc:
            self._fatal = True
            if not self._fatal_exit_invoked:
                self._fatal_exit_invoked = True
                self._fatal_exit(70)
            raise CP7SafetyFatalPersistenceError(
                f"CP7 safety {label} failed"
            ) from exc


def _predecessor_matches(
    identity: CP7RuntimeIdentity, predecessor: CP7PredecessorClose | None
) -> bool:
    return bool(
        predecessor
        and predecessor.candidate_id == identity.candidate_id
        and predecessor.epoch_id == identity.predecessor_epoch_id
        and predecessor.container_id == identity.container_id
        and predecessor.image_id == identity.image_id
        and predecessor.config_fingerprint == identity.config_fingerprint
    )


def _durable_predecessor_matches(
    predecessor: CP7PredecessorClose, event: MCPCP7ReadyEpochEvent
) -> bool:
    evidence = predecessor.closed_boundary
    payload = {
        "candidate_id": event.candidate_id,
        "epoch_id": event.epoch_id,
        "predecessor_epoch_id": event.predecessor_epoch_id,
        "event_kind": str(event.event_kind),
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
    return bool(
        event.event_kind is MCPCP7ReadyEpochEventKind.CLOSED
        and canonical_sha256(payload) == event.payload_sha256
        and predecessor.candidate_id == event.candidate_id
        and predecessor.epoch_id == event.epoch_id
        and predecessor.container_id == event.container_id
        and predecessor.image_id == event.image_id
        and predecessor.config_fingerprint == event.config_fingerprint
        and evidence.boundary_at == event.boundary_at
        and evidence.audit_device == event.audit_device
        and evidence.audit_inode == event.audit_inode
        and evidence.audit_offset == event.audit_offset
        and evidence.ledger_record_count == event.ledger_record_count
        and evidence.inflight_state_sha256 == event.inflight_state_sha256
    )


def _validate_identity(identity: CP7RuntimeIdentity) -> None:
    values = (
        identity.candidate_id,
        identity.epoch_id,
        identity.container_id,
        identity.image_id,
        identity.config_fingerprint,
    )
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("CP7 runtime identity is incomplete")


def _validate_boundary(evidence: CP7BoundaryEvidence) -> None:
    if (
        evidence.boundary_at.tzinfo is None
        or evidence.boundary_at.utcoffset() is None
        or not evidence.audit_device
        or evidence.audit_inode < 0
        or evidence.audit_offset < 0
        or evidence.ledger_record_count < 0
        or not evidence.inflight_state_sha256.startswith("sha256:")
    ):
        raise ValueError("CP7 safety boundary evidence is invalid")


def _validate_full_minute(started_at: datetime, ended_at: datetime) -> None:
    utc_start = started_at.astimezone(timezone.utc)
    utc_end = ended_at.astimezone(timezone.utc)
    if (
        utc_end - utc_start != timedelta(minutes=1)
        or utc_start.second != 0
        or utc_start.microsecond != 0
        or utc_end.second != 0
        or utc_end.microsecond != 0
    ):
        raise ValueError("CP7 safety attestation must cover one full UTC minute")


def _next_minute(value: datetime | None) -> datetime:
    if value is None:
        raise CP7SafetyStateError("CP7 safety epoch has no opened boundary")
    result = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if result < value:
        result += timedelta(minutes=1)
    return result


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def cp7_runtime_safety_wiring(
    storage: MCPCP7StoragePort,
    identity: CP7RuntimeIdentity,
    *,
    fatal_exit: Callable[[int], None] = os._exit,
) -> tuple[CP7LocalSafetyFacade, Mapping[MCPSafetyRedLine, AuthoritativeMCPSafetyDetector]]:
    """Runtime assembly API; inject the returned exact detector map into hooks."""

    facade = CP7LocalSafetyFacade(storage, identity, fatal_exit=fatal_exit)
    return facade, facade.detectors


__all__ = [
    "CP7BoundaryEvidence",
    "CP7LocalSafetyFacade",
    "CP7PredecessorClose",
    "CP7RuntimeIdentity",
    "CP7SafetyError",
    "CP7SafetyFatalPersistenceError",
    "CP7SafetyStateError",
    "cp7_runtime_safety_wiring",
]
