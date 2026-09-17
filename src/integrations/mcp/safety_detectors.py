from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .rollout_evidence import (
    MCPMetricExecutionPath,
    MCPMetricLabels,
    MCPMetricName,
    MCPMetricResultCategory,
    MCPMetricErrorCategory,
    MCPMetricRoutingMode,
    MCPSafetyRedLine,
)


AUTHORITATIVE_MCP_SAFETY_HOOKS: Mapping[MCPSafetyRedLine, str] = {
    MCPSafetyRedLine.CROSS_USER_ACCESS: "gateway.task_owner_boundary",
    MCPSafetyRedLine.SECRET_EXPOSURE: "audit.secret_payload_boundary",
    MCPSafetyRedLine.DUAL_TOOL_CALL: "dispatch.durable_call_idempotency_boundary",
    MCPSafetyRedLine.UNAUTHORIZED_TOOL_CALL: "dispatch.permission_boundary",
    MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS: "gateway.endpoint_policy_boundary",
    MCPSafetyRedLine.UNKNOWN_RESULT_REPLAY: "recovery.unknown_replay_boundary",
    MCPSafetyRedLine.SHADOW_TOOL_CALL: "gateway.persisted_assignment_boundary",
    MCPSafetyRedLine.PERSISTENT_RESOURCE_LEAK: "gateway.resource_cleanup_boundary",
}

MCP_SAFETY_VIOLATION_REASONS: Mapping[MCPSafetyRedLine, frozenset[str]] = {
    MCPSafetyRedLine.CROSS_USER_ACCESS: frozenset({"task_owner_mismatch"}),
    MCPSafetyRedLine.SECRET_EXPOSURE: frozenset({"secret_payload_rejected"}),
    MCPSafetyRedLine.DUAL_TOOL_CALL: frozenset({"call_idempotency_conflict"}),
    MCPSafetyRedLine.UNAUTHORIZED_TOOL_CALL: frozenset(
        {"permission_denied_boundary"}
    ),
    MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS: frozenset(
        {"endpoint_policy_rejected"}
    ),
    MCPSafetyRedLine.UNKNOWN_RESULT_REPLAY: frozenset({"unknown_replay_blocked"}),
    MCPSafetyRedLine.SHADOW_TOOL_CALL: frozenset({"shadow_call_blocked"}),
    MCPSafetyRedLine.PERSISTENT_RESOURCE_LEAK: frozenset({"cleanup_failed"}),
}

MCP_SAFETY_GAP_REASONS = frozenset(
    {
        "detector_unregistered",
        "detector_unhealthy",
        "interval_attestation_missing",
        "safety_metric_write_failed",
        "terminal_metric_write_failed",
        "producer_interval_missed",
        "zero_series_write_failed",
        "unplanned_process_exit",
        "maintenance_boundary_invalid",
    }
)

_MAX_ATTESTATION_WINDOW = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class MCPSafetyMetricGap:
    reason_code: str
    bucket_started_at: datetime
    bucket_ended_at: datetime
    red_line: MCPSafetyRedLine | None = None

    def __post_init__(self) -> None:
        if self.reason_code not in MCP_SAFETY_GAP_REASONS:
            raise ValueError("MCP safety metric gap reason is not supported")
        _validate_interval(self.bucket_started_at, self.bucket_ended_at)


SafetyMetricGapSink = Callable[[MCPSafetyMetricGap], Any | Awaitable[Any]]


class AuthoritativeMCPSafetyDetectorRegistry:
    """Closed registry proving that every red-line zero was actually observed."""

    def __init__(
        self,
        recorder: Any,
        *,
        gap_sink: SafetyMetricGapSink,
        routing_mode: MCPMetricRoutingMode,
    ) -> None:
        if not callable(gap_sink):
            raise ValueError("MCP safety detector durable gap sink is required")
        if not isinstance(routing_mode, MCPMetricRoutingMode):
            raise ValueError("MCP safety detector routing mode must be closed")
        self._recorder = recorder
        self._gap_sink = gap_sink
        self._routing_mode = routing_mode
        self._detectors: dict[MCPSafetyRedLine, AuthoritativeMCPSafetyDetector] = {}

    def register(
        self, red_line: MCPSafetyRedLine, hook_id: str
    ) -> "AuthoritativeMCPSafetyDetector":
        if not isinstance(red_line, MCPSafetyRedLine):
            raise ValueError("MCP safety red line must be closed")
        if AUTHORITATIVE_MCP_SAFETY_HOOKS[red_line] != hook_id:
            raise ValueError("MCP safety detector hook is not authoritative")
        if red_line in self._detectors:
            raise ValueError("MCP safety detector is already registered")
        detector = AuthoritativeMCPSafetyDetector(self, red_line, hook_id)
        self._detectors[red_line] = detector
        return detector

    def detector(self, red_line: MCPSafetyRedLine) -> "AuthoritativeMCPSafetyDetector":
        detector = self._detectors.get(red_line)
        if detector is None:
            raise RuntimeError("MCP safety detector is not registered")
        return detector

    @property
    def complete(self) -> bool:
        return set(self._detectors) == set(MCPSafetyRedLine)

    async def persist_gap(
        self,
        reason_code: str,
        *,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
        red_line: MCPSafetyRedLine | None = None,
    ) -> None:
        await self._persist_gap(
            MCPSafetyMetricGap(
                reason_code=reason_code,
                bucket_started_at=bucket_started_at,
                bucket_ended_at=bucket_ended_at,
                red_line=red_line,
            )
        )

    async def record_verified_zero_series(
        self, *, bucket_started_at: datetime, bucket_ended_at: datetime
    ) -> tuple[Any, ...]:
        _validate_bucket(bucket_started_at, bucket_ended_at)
        if not self.complete:
            await self._persist_gap(
                MCPSafetyMetricGap(
                    reason_code="detector_unregistered",
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                )
            )
            raise RuntimeError("MCP safety detector registry is incomplete")
        for red_line, detector in self._detectors.items():
            reason = detector._zero_blocker(bucket_started_at, bucket_ended_at)
            if reason is not None:
                await self._persist_gap(
                    MCPSafetyMetricGap(
                        reason_code=reason,
                        bucket_started_at=bucket_started_at,
                        bucket_ended_at=bucket_ended_at,
                        red_line=red_line,
                    )
                )
                raise RuntimeError("MCP safety detector interval is not healthy")

        records: list[Any] = []
        for red_line, detector in self._detectors.items():
            try:
                records.append(
                    await self._record_red_line(
                        red_line,
                        value=0,
                        bucket_started_at=bucket_started_at,
                        bucket_ended_at=bucket_ended_at,
                    )
                )
            except Exception:
                await self._persist_gap(
                    MCPSafetyMetricGap(
                        reason_code="zero_series_write_failed",
                        bucket_started_at=bucket_started_at,
                        bucket_ended_at=bucket_ended_at,
                        red_line=red_line,
                    )
                )
                raise
            detector._consume_attestation(bucket_started_at, bucket_ended_at)
        return tuple(records)

    async def _report_violation(
        self,
        red_line: MCPSafetyRedLine,
        *,
        reason_code: str,
        observed_at: datetime | None,
    ) -> Any:
        if reason_code not in MCP_SAFETY_VIOLATION_REASONS[red_line]:
            raise ValueError("MCP safety violation reason is not supported")
        started_at, ended_at = _minute_bucket(observed_at)
        try:
            durable_violation_writer = getattr(
                self._recorder, "record_safety_violation", None
            )
            if callable(durable_violation_writer):
                return await _await_maybe(
                    durable_violation_writer(
                        red_line=red_line,
                        reason_code=reason_code,
                        bucket_started_at=started_at,
                        bucket_ended_at=ended_at,
                    )
                )
            return await self._record_red_line(
                red_line,
                value=1,
                bucket_started_at=started_at,
                bucket_ended_at=ended_at,
            )
        except Exception:
            await self._persist_gap(
                MCPSafetyMetricGap(
                    reason_code="safety_metric_write_failed",
                    bucket_started_at=started_at,
                    bucket_ended_at=ended_at,
                    red_line=red_line,
                )
            )
            raise

    async def _record_red_line(
        self,
        red_line: MCPSafetyRedLine,
        *,
        value: int,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
    ) -> Any:
        return await self._recorder.record_count(
            MCPMetricName.SAFETY_RED_LINE_TOTAL,
            labels=MCPMetricLabels(
                execution_path=MCPMetricExecutionPath.USER_SCOPED,
                routing_mode=self._routing_mode,
                result_category=(
                    MCPMetricResultCategory.SUCCEEDED
                    if value == 0
                    else MCPMetricResultCategory.FAILED
                ),
                error_category=(
                    MCPMetricErrorCategory.NONE
                    if value == 0
                    else MCPMetricErrorCategory.VALIDATION
                ),
                red_line=red_line,
            ),
            bucket_started_at=bucket_started_at,
            bucket_ended_at=bucket_ended_at,
            value=value,
        )

    async def _persist_gap(self, gap: MCPSafetyMetricGap) -> None:
        await _await_maybe(self._gap_sink(gap))


class AuthoritativeMCPSafetyDetector:
    def __init__(
        self,
        registry: AuthoritativeMCPSafetyDetectorRegistry,
        red_line: MCPSafetyRedLine,
        hook_id: str,
    ) -> None:
        self._registry = registry
        self.red_line = red_line
        self.hook_id = hook_id
        self._healthy = True
        self._attestations: set[tuple[datetime, datetime]] = set()

    def mark_healthy(self) -> None:
        self._healthy = True

    def mark_unhealthy(self) -> None:
        self._healthy = False

    def attest_interval(self, started_at: datetime, ended_at: datetime) -> None:
        _validate_bucket(started_at, ended_at)
        if not self._healthy:
            raise RuntimeError("MCP safety detector is unhealthy")
        self._attestations.add((started_at, ended_at))

    async def report_violation(
        self, *, reason_code: str, observed_at: datetime | None = None
    ) -> Any:
        return await self._registry._report_violation(
            self.red_line,
            reason_code=reason_code,
            observed_at=observed_at,
        )

    def _zero_blocker(self, started_at: datetime, ended_at: datetime) -> str | None:
        if not self._healthy:
            return "detector_unhealthy"
        if (started_at, ended_at) not in self._attestations:
            return "interval_attestation_missing"
        return None

    def _consume_attestation(self, started_at: datetime, ended_at: datetime) -> None:
        self._attestations.remove((started_at, ended_at))


def register_authoritative_mcp_safety_detectors(
    registry: AuthoritativeMCPSafetyDetectorRegistry,
) -> Mapping[MCPSafetyRedLine, AuthoritativeMCPSafetyDetector]:
    return {
        red_line: registry.register(red_line, hook_id)
        for red_line, hook_id in AUTHORITATIVE_MCP_SAFETY_HOOKS.items()
    }


def _validate_interval(started_at: datetime, ended_at: datetime) -> None:
    if (
        not isinstance(started_at, datetime)
        or started_at.tzinfo is None
        or started_at.utcoffset() is None
        or not isinstance(ended_at, datetime)
        or ended_at.tzinfo is None
        or ended_at.utcoffset() is None
        or ended_at <= started_at
    ):
        raise ValueError("MCP safety detector interval is invalid")


def _validate_bucket(started_at: datetime, ended_at: datetime) -> None:
    _validate_interval(started_at, ended_at)
    utc_start = started_at.astimezone(timezone.utc)
    utc_end = ended_at.astimezone(timezone.utc)
    if (
        utc_end - utc_start != _MAX_ATTESTATION_WINDOW
        or utc_start.second != 0
        or utc_start.microsecond != 0
        or utc_end.second != 0
        or utc_end.microsecond != 0
    ):
        raise ValueError("MCP safety detector attestation must cover one full UTC minute")


def _minute_bucket(observed_at: datetime | None) -> tuple[datetime, datetime]:
    value = observed_at or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    started_at = value.replace(second=0, microsecond=0)
    return started_at, started_at + timedelta(minutes=1)


async def _await_maybe(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


__all__ = [
    "AUTHORITATIVE_MCP_SAFETY_HOOKS",
    "AuthoritativeMCPSafetyDetector",
    "AuthoritativeMCPSafetyDetectorRegistry",
    "MCP_SAFETY_GAP_REASONS",
    "MCP_SAFETY_VIOLATION_REASONS",
    "MCPSafetyMetricGap",
    "register_authoritative_mcp_safety_detectors",
]
