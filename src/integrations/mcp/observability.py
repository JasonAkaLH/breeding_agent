from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from src.core.contracts import StoragePort
from src.core.models import (
    MCPRolloutEvidenceSnapshot as MCPRolloutEvidenceSnapshotRecord,
)
from src.core.models import MCPRolloutMetricBucket as MCPRolloutMetricBucketRecord

from .rollout_evidence import (
    MCP_ROLLOUT_PROGRAM,
    MCPCallKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPGateBlocker,
    MCPLatencyBucket,
    MCPMetricAdapter,
    MCPMetricBucket,
    MCPMetricErrorCategory,
    MCPMetricExecutionPath,
    MCPMetricLabels,
    MCPMetricName,
    MCPMetricProtocolVersion,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
    MCPMetricTransport,
    MCPRolloutStage,
    MCPSafetyRedLine,
    canonical_evidence_attestation_signature,
    canonical_evidence_content_digest,
    is_exact_mcp_metric_bucket_window,
    validate_evidence_snapshot,
)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_TERMINAL_RESULTS = frozenset(
    {
        MCPMetricResultCategory.SUCCEEDED,
        MCPMetricResultCategory.FAILED,
        MCPMetricResultCategory.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class MCPRolloutMetricContext:
    environment_id: str
    deployment_id: str
    stage: MCPRolloutStage
    config_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("environment_id", "deployment_id", "config_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
                raise ValueError(f"MCP rollout metric {name} is invalid")
        if not isinstance(self.stage, MCPRolloutStage):
            raise ValueError(
                "MCP rollout metric stage must be a closed MCPRolloutStage"
            )


class MCPRolloutMetricRecorder:
    """Persist closed-label MCP rollout counters, histograms, and gauges."""

    def __init__(
        self,
        storage: StoragePort,
        context: MCPRolloutMetricContext,
        *,
        trusted_attestation_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        self._storage = storage
        self._context = context
        self._trusted_attestation_keys = trusted_attestation_keys
        self._safety_detector_registry: Any | None = None
        self._safety_interval_probes: tuple[Any, ...] = ()

    def configure_safety_detector_registry(self, registry: Any) -> None:
        if registry is None or not callable(
            getattr(registry, "record_verified_zero_series", None)
        ):
            raise ValueError("MCP safety detector registry is invalid")
        self._safety_detector_registry = registry

    def configure_safety_interval_probes(self, *probes: Any) -> None:
        if not probes or any(not callable(probe) for probe in probes):
            raise ValueError("MCP safety interval probes are invalid")
        self._safety_interval_probes = tuple(probes)

    async def record_count(
        self,
        metric_name: MCPMetricName,
        *,
        labels: MCPMetricLabels,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
        value: int = 1,
    ) -> MCPRolloutMetricBucketRecord:
        bucket = MCPMetricBucket(
            metric_name=metric_name,
            bucket_started_at=bucket_started_at,
            bucket_ended_at=bucket_ended_at,
            labels=labels,
            value=value,
        )
        return await self.record_bucket(bucket)

    async def record_latency(
        self,
        metric_name: MCPMetricName,
        *,
        duration_seconds: float,
        labels: MCPMetricLabels,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
    ) -> MCPRolloutMetricBucketRecord:
        bucket = MCPMetricBucket(
            metric_name=metric_name,
            bucket_started_at=bucket_started_at,
            bucket_ended_at=bucket_ended_at,
            labels=labels,
            latency_bucket=mcp_latency_bucket(duration_seconds),
            value=1,
        )
        return await self.record_bucket(bucket)

    async def record_gauge(
        self,
        metric_name: MCPMetricName,
        *,
        labels: MCPMetricLabels,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
        value: int,
    ) -> MCPRolloutMetricBucketRecord:
        bucket = MCPMetricBucket(
            metric_name=metric_name,
            bucket_started_at=bucket_started_at,
            bucket_ended_at=bucket_ended_at,
            labels=labels,
            value=value,
        )
        record = mcp_metric_bucket_to_record(bucket, context=self._context)
        return await self._storage.set_mcp_rollout_metric_bucket(record)

    async def record_bucket(
        self, bucket: MCPMetricBucket
    ) -> MCPRolloutMetricBucketRecord:
        record = mcp_metric_bucket_to_record(bucket, context=self._context)
        return await self._storage.upsert_mcp_rollout_metric_bucket(record)

    async def record_evidence_snapshot(
        self, snapshot: MCPEvidenceSnapshot
    ) -> MCPRolloutEvidenceSnapshotRecord:
        if (
            snapshot.environment_id != self._context.environment_id
            or snapshot.deployment_id != self._context.deployment_id
            or snapshot.stage is not self._context.stage
            or snapshot.config_fingerprint != self._context.config_fingerprint
        ):
            raise ValueError("MCP evidence snapshot does not match recorder context")
        record = mcp_evidence_snapshot_to_record(
            snapshot,
            trusted_attestation_keys=self._trusted_attestation_keys,
        )
        return await self._storage.append_mcp_rollout_evidence_snapshot(record)

    async def record_shadow_mismatch(
        self,
        *,
        observed_at: datetime | None = None,
    ) -> MCPRolloutMetricBucketRecord:
        """Record one closed-label shadow route mismatch.

        The shadow comparator decides whether an observation is a mismatch. This
        method only gives that boundary a narrow, typo-proof metric family.
        """

        started_at, ended_at = _minute_bucket(observed_at)
        return await self.record_count(
            MCPMetricName.ROUTE_SHADOW_MISMATCH_TOTAL,
            labels=MCPMetricLabels(
                execution_path=MCPMetricExecutionPath.USER_SCOPED,
                routing_mode=MCPMetricRoutingMode.SHADOW,
                result_category=MCPMetricResultCategory.FAILED,
                error_category=MCPMetricErrorCategory.VALIDATION,
            ),
            bucket_started_at=started_at,
            bucket_ended_at=ended_at,
        )

    async def record_required_zero_series(
        self,
        *,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
    ) -> tuple[MCPRolloutMetricBucketRecord, ...]:
        """Persist mandatory zero-valued terminal series.

        Counter upserts are additive, so inserting zero makes an absent series
        explicit without replacing real events already recorded in the bucket.
        Safety red lines are deliberately not synthesized: until an
        authoritative positive detector is wired for a red line, the missing
        series must keep the promotion gate fail-closed.
        """

        _validate_window(bucket_started_at, bucket_ended_at)
        routing_mode = _routing_mode_for_stage(self._context.stage)
        records: list[MCPRolloutMetricBucketRecord] = []
        for call_kind in MCPCallKind:
            for result_category, error_category in (
                (
                    MCPMetricResultCategory.SUCCEEDED,
                    MCPMetricErrorCategory.NONE,
                ),
                (
                    MCPMetricResultCategory.FAILED,
                    MCPMetricErrorCategory.UNKNOWN,
                ),
                (
                    MCPMetricResultCategory.UNKNOWN,
                    MCPMetricErrorCategory.UNKNOWN,
                ),
                (
                    MCPMetricResultCategory.CANCELLED,
                    MCPMetricErrorCategory.NONE,
                ),
            ):
                records.append(
                    await self.record_count(
                        MCPMetricName.TOOL_CALLS_TOTAL,
                        labels=MCPMetricLabels(
                            execution_path=MCPMetricExecutionPath.USER_SCOPED,
                            routing_mode=routing_mode,
                            result_category=result_category,
                            error_category=error_category,
                            call_kind=call_kind,
                        ),
                        bucket_started_at=bucket_started_at,
                        bucket_ended_at=bucket_ended_at,
                        value=0,
                    )
                )
        registry = self._safety_detector_registry
        if registry is None:
            raise RuntimeError("MCP safety detector registry is not configured")
        if not self._safety_interval_probes:
            raise RuntimeError("MCP safety interval probes are not configured")
        for probe in self._safety_interval_probes:
            await _await_maybe(probe(bucket_started_at, bucket_ended_at))
        records.extend(
            await registry.record_verified_zero_series(
                bucket_started_at=bucket_started_at,
                bucket_ended_at=bucket_ended_at,
            )
        )
        return tuple(records)

    async def run_continuous_zero_series(self) -> None:
        """Write only fully observed one-minute zero-series buckets forever."""

        now = datetime.now(timezone.utc)
        next_boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        await asyncio.sleep(max(0.0, (next_boundary - now).total_seconds()))
        bucket_started_at = next_boundary
        while True:
            bucket_ended_at = bucket_started_at + timedelta(minutes=1)
            delay = (bucket_ended_at - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            observed_at = datetime.now(timezone.utc)
            if observed_at > bucket_ended_at + timedelta(seconds=5):
                # Never backfill an interval that the process could not observe
                # (suspend, long event-loop stall, or storage outage). Leaving
                # the bucket absent makes the promotion gate fail closed.
                registry = self._safety_detector_registry
                if registry is None:
                    raise RuntimeError("MCP safety detector registry is not configured")
                await registry.persist_gap(
                    "producer_interval_missed",
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                )
                bucket_started_at = observed_at.replace(second=0, microsecond=0)
                continue
            try:
                await self.record_required_zero_series(
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                )
            except Exception:
                registry = self._safety_detector_registry
                if registry is None:
                    raise
                await registry.persist_gap(
                    "zero_series_write_failed",
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                )
                raise
            bucket_started_at = bucket_ended_at


def mcp_latency_bucket(duration_seconds: float) -> MCPLatencyBucket:
    if isinstance(duration_seconds, bool) or not isinstance(
        duration_seconds, (int, float)
    ):
        raise ValueError("MCP latency must be a finite non-negative number")
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError("MCP latency must be a finite non-negative number")
    for upper_bound, bucket in (
        (0.1, MCPLatencyBucket.LE_100_MS),
        (0.5, MCPLatencyBucket.LE_500_MS),
        (1.0, MCPLatencyBucket.LE_1_S),
        (5.0, MCPLatencyBucket.LE_5_S),
        (30.0, MCPLatencyBucket.LE_30_S),
        (120.0, MCPLatencyBucket.LE_120_S),
    ):
        if duration_seconds <= upper_bound:
            return bucket
    return MCPLatencyBucket.GT_120_S


def _minute_bucket(
    observed_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    value = observed_at or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    started_at = value.replace(second=0, microsecond=0)
    return started_at, started_at + timedelta(minutes=1)


def _routing_mode_for_stage(stage: MCPRolloutStage) -> MCPMetricRoutingMode:
    if stage is MCPRolloutStage.OFF:
        return MCPMetricRoutingMode.OFF
    if stage is MCPRolloutStage.INTERNAL_SHADOW:
        return MCPMetricRoutingMode.SHADOW
    return MCPMetricRoutingMode.ENFORCE


def is_mcp_terminal_call_sample(labels: MCPMetricLabels) -> bool:
    """Return whether an outcome belongs in the real terminal-call denominator."""

    _validate_labels(labels)
    return labels.call_kind is not None and labels.result_category in _TERMINAL_RESULTS


def mcp_terminal_call_sample_count(
    buckets: tuple[MCPMetricBucket, ...] | list[MCPMetricBucket],
    *,
    call_kind: MCPCallKind | None = None,
) -> int:
    if call_kind is not None and not isinstance(call_kind, MCPCallKind):
        raise ValueError("MCP call kind must be a closed MCPCallKind")
    count = 0
    for bucket in buckets:
        if not isinstance(bucket, MCPMetricBucket):
            raise TypeError("terminal denominator accepts only MCPMetricBucket")
        if (
            isinstance(bucket.value, bool)
            or not isinstance(bucket.value, int)
            or bucket.value < 0
        ):
            raise ValueError("MCP rollout metric value must be a non-negative integer")
        if (
            bucket.metric_name is MCPMetricName.TOOL_CALL_DURATION_SECONDS
            and (call_kind is None or bucket.labels.call_kind is call_kind)
            and is_mcp_terminal_call_sample(bucket.labels)
        ):
            count += bucket.value
    return count


def mcp_metric_bucket_to_record(
    bucket: MCPMetricBucket,
    *,
    context: MCPRolloutMetricContext,
) -> MCPRolloutMetricBucketRecord:
    if not isinstance(bucket, MCPMetricBucket):
        raise TypeError("MCP rollout metric recorder accepts only MCPMetricBucket")
    if not isinstance(bucket.metric_name, MCPMetricName):
        raise ValueError("MCP metric name must be a closed MCPMetricName")
    _validate_labels(bucket.labels)
    if not isinstance(bucket.latency_bucket, MCPLatencyBucket):
        raise ValueError("MCP latency bucket must be a closed MCPLatencyBucket")
    _validate_window(bucket.bucket_started_at, bucket.bucket_ended_at)
    if (
        isinstance(bucket.value, bool)
        or not isinstance(bucket.value, int)
        or bucket.value < 0
    ):
        raise ValueError("MCP rollout metric value must be a non-negative integer")

    identity = {
        "environment_id": context.environment_id,
        "deployment_id": context.deployment_id,
        "stage": context.stage.value,
        "config_fingerprint": context.config_fingerprint,
        "metric_name": bucket.metric_name.value,
        "bucket_started_at": _canonical_timestamp(bucket.bucket_started_at),
        "bucket_ended_at": _canonical_timestamp(bucket.bucket_ended_at),
        "execution_path": bucket.labels.execution_path.value,
        "routing_mode": bucket.labels.routing_mode.value,
        "transport": bucket.labels.transport.value,
        "protocol_version": bucket.labels.protocol_version.value,
        "adapter": bucket.labels.adapter.value,
        "result_category": bucket.labels.result_category.value,
        "error_category": bucket.labels.error_category.value,
        "call_kind": (
            "not_applicable"
            if bucket.labels.call_kind is None
            else bucket.labels.call_kind.value
        ),
        "red_line": (
            "not_applicable"
            if bucket.labels.red_line is None
            else bucket.labels.red_line.value
        ),
        "latency_bucket": bucket.latency_bucket.value,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    metric_bucket_id = f"mcp-metric-{hashlib.sha256(encoded).hexdigest()}"
    now = datetime.now(timezone.utc)
    return MCPRolloutMetricBucketRecord(
        metric_bucket_id=metric_bucket_id,
        environment_id=context.environment_id,
        rollout_program=MCP_ROLLOUT_PROGRAM,
        deployment_id=context.deployment_id,
        stage=context.stage.value,
        config_fingerprint=context.config_fingerprint,
        metric_name=bucket.metric_name.value,
        bucket_started_at=bucket.bucket_started_at,
        bucket_ended_at=bucket.bucket_ended_at,
        execution_path=bucket.labels.execution_path.value,
        routing_mode=bucket.labels.routing_mode.value,
        transport=bucket.labels.transport.value,
        protocol_version=bucket.labels.protocol_version.value,
        adapter=bucket.labels.adapter.value,
        result_category=bucket.labels.result_category.value,
        error_category=bucket.labels.error_category.value,
        call_kind=None
        if bucket.labels.call_kind is None
        else bucket.labels.call_kind.value,
        red_line=None
        if bucket.labels.red_line is None
        else bucket.labels.red_line.value,
        latency_bucket=bucket.latency_bucket.value,
        value=bucket.value,
        created_at=now,
        updated_at=now,
    )


def mcp_evidence_snapshot_to_record(
    snapshot: MCPEvidenceSnapshot,
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPRolloutEvidenceSnapshotRecord:
    blockers = validate_evidence_snapshot(
        snapshot,
        trusted_attestation_keys=trusted_attestation_keys,
    )
    if blockers:
        rendered = ", ".join(blocker.value for blocker in blockers)
        raise ValueError(f"MCP evidence snapshot is not sealed and valid: {rendered}")
    return MCPRolloutEvidenceSnapshotRecord(
        evidence_id=snapshot.evidence_id,
        environment_id=snapshot.environment_id,
        rollout_program=MCP_ROLLOUT_PROGRAM,
        git_sha=snapshot.git_sha,
        deployment_id=snapshot.deployment_id,
        stage=snapshot.stage.value,
        config_fingerprint=snapshot.config_fingerprint,
        window_started_at=snapshot.window_started_at,
        window_ended_at=snapshot.window_ended_at,
        recorded_at=snapshot.recorded_at,
        producer=snapshot.producer.value,
        source=snapshot.source.value,
        snapshot_id=snapshot.snapshot_id,
        nonce=snapshot.nonce,
        evidence_kind=snapshot.payload.kind.value,
        payload=_json_value(snapshot.payload),
        payload_digest=snapshot.payload_digest,
        attestation_key_id=snapshot.attestation_key_id,
        attestation_signature=snapshot.attestation_signature,
    )


def validate_mcp_evidence_snapshot_record(
    record: MCPRolloutEvidenceSnapshotRecord,
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> tuple[MCPGateBlocker, ...]:
    """Independently revalidate a snapshot after canonical-storage reload."""

    blockers: list[MCPGateBlocker] = []
    if record.rollout_program != MCP_ROLLOUT_PROGRAM:
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    if (record.source, record.producer) not in {
        (MCPEvidenceSource.CI.value, MCPEvidenceProducer.CI_PIPELINE.value),
        (
            MCPEvidenceSource.PRODUCTION.value,
            MCPEvidenceProducer.PRODUCTION_SNAPSHOT.value,
        ),
    }:
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    if record.evidence_kind != record.payload.get("kind"):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    content = _record_evidence_content(record)
    expected_digest = canonical_evidence_content_digest(content)
    if not hmac.compare_digest(record.payload_digest, expected_digest):
        blockers.append(MCPGateBlocker.DIGEST_INVALID)

    key_id = record.attestation_key_id
    signature = record.attestation_signature
    if record.source == MCPEvidenceSource.CI.value:
        if key_id is not None or signature is not None:
            blockers.append(MCPGateBlocker.ATTESTATION_INVALID)
    elif record.source == MCPEvidenceSource.PRODUCTION.value:
        if not key_id or not signature:
            blockers.append(MCPGateBlocker.ATTESTATION_MISSING)
        elif trusted_attestation_keys is None:
            blockers.append(MCPGateBlocker.ATTESTATION_MISSING)
        else:
            key = trusted_attestation_keys.get(key_id)
            if not isinstance(key, bytes) or not key:
                blockers.append(MCPGateBlocker.ATTESTATION_INVALID)
            else:
                expected_signature = canonical_evidence_attestation_signature(
                    _record_attestation_identity(record),
                    key_id=key_id,
                    key=key,
                )
                if not hmac.compare_digest(signature, expected_signature):
                    blockers.append(MCPGateBlocker.ATTESTATION_INVALID)
    return tuple(dict.fromkeys(blockers))


def mcp_evidence_snapshot_matches_record(
    snapshot: MCPEvidenceSnapshot,
    record: MCPRolloutEvidenceSnapshotRecord,
) -> bool:
    """Bind every persisted signed field to the caller's asserted snapshot."""

    return (
        record.evidence_id == snapshot.evidence_id
        and _record_evidence_content(record)
        == _snapshot_evidence_content(snapshot)
        and record.payload_digest == snapshot.payload_digest
        and record.attestation_key_id == snapshot.attestation_key_id
        and record.attestation_signature == snapshot.attestation_signature
        and record.evidence_kind == snapshot.payload.kind.value
    )


def _record_evidence_content(
    record: MCPRolloutEvidenceSnapshotRecord,
) -> dict[str, object]:
    return {
        "evidence_id": record.evidence_id,
        "environment_id": record.environment_id,
        "git_sha": record.git_sha,
        "deployment_id": record.deployment_id,
        "stage": record.stage,
        "config_fingerprint": record.config_fingerprint,
        "window_started_at": record.window_started_at,
        "window_ended_at": record.window_ended_at,
        "recorded_at": record.recorded_at,
        "producer": record.producer,
        "source": record.source,
        "snapshot_id": record.snapshot_id,
        "nonce": record.nonce,
        "payload": dict(record.payload),
    }


def _snapshot_evidence_content(snapshot: MCPEvidenceSnapshot) -> dict[str, object]:
    return {
        "evidence_id": snapshot.evidence_id,
        "environment_id": snapshot.environment_id,
        "git_sha": snapshot.git_sha,
        "deployment_id": snapshot.deployment_id,
        "stage": snapshot.stage.value,
        "config_fingerprint": snapshot.config_fingerprint,
        "window_started_at": snapshot.window_started_at,
        "window_ended_at": snapshot.window_ended_at,
        "recorded_at": snapshot.recorded_at,
        "producer": snapshot.producer.value,
        "source": snapshot.source.value,
        "snapshot_id": snapshot.snapshot_id,
        "nonce": snapshot.nonce,
        "payload": _json_value(snapshot.payload),
    }


def _record_attestation_identity(
    record: MCPRolloutEvidenceSnapshotRecord,
) -> dict[str, object]:
    return {
        "payload_digest": record.payload_digest,
        "evidence_id": record.evidence_id,
        "environment_id": record.environment_id,
        "git_sha": record.git_sha,
        "deployment_id": record.deployment_id,
        "stage": record.stage,
        "config_fingerprint": record.config_fingerprint,
        "snapshot_id": record.snapshot_id,
        "nonce": record.nonce,
    }


def _validate_labels(labels: MCPMetricLabels) -> None:
    if not isinstance(labels, MCPMetricLabels):
        raise TypeError("MCP rollout metric recorder accepts only MCPMetricLabels")
    expected_types = {
        "execution_path": MCPMetricExecutionPath,
        "routing_mode": MCPMetricRoutingMode,
        "transport": MCPMetricTransport,
        "protocol_version": MCPMetricProtocolVersion,
        "adapter": MCPMetricAdapter,
        "result_category": MCPMetricResultCategory,
        "error_category": MCPMetricErrorCategory,
        "call_kind": MCPCallKind,
        "red_line": MCPSafetyRedLine,
    }
    for name, expected_type in expected_types.items():
        value = getattr(labels, name)
        if name in {"call_kind", "red_line"} and value is None:
            continue
        if not isinstance(value, expected_type):
            raise ValueError(
                f"MCP metric label {name} must be a closed {expected_type.__name__}"
            )


def _validate_window(started_at: datetime, ended_at: datetime) -> None:
    if not is_exact_mcp_metric_bucket_window(started_at, ended_at):
        raise ValueError(
            "MCP rollout metric bucket must be one complete UTC-aligned minute"
        )


def _canonical_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


async def _await_maybe(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (set, frozenset)):
        return sorted(_json_value(item) for item in value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported MCP evidence payload value: {type(value).__name__}")


__all__ = [
    "MCPRolloutMetricContext",
    "MCPRolloutMetricRecorder",
    "is_mcp_terminal_call_sample",
    "mcp_evidence_snapshot_to_record",
    "mcp_evidence_snapshot_matches_record",
    "mcp_latency_bucket",
    "mcp_metric_bucket_to_record",
    "mcp_terminal_call_sample_count",
    "validate_mcp_evidence_snapshot_record",
]
