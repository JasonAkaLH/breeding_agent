from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from .rollout import MCPRolloutConfig, is_strict_mcp_exposure_decrease


MCP_ROLLOUT_PROGRAM = "user_mcp_phase3"

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class MCPRolloutStage(StrEnum):
    OFF = "off"
    INTERNAL_SHADOW = "internal_shadow"
    INTERNAL_ENFORCE = "internal_enforce"
    COHORT_ENFORCE = "cohort_enforce"
    FULL_ENFORCE = "full_enforce"
    LEGACY_ASSEMBLY_OFF = "legacy_assembly_off"


class MCPEvidenceSource(StrEnum):
    CI = "ci"
    PRODUCTION = "production"


class MCPEvidenceProducer(StrEnum):
    CI_PIPELINE = "ci_pipeline"
    PRODUCTION_SNAPSHOT = "production_snapshot_producer"


class MCPEvidenceKind(StrEnum):
    CI_CONFORMANCE = "ci_conformance"
    INTERNAL_SHADOW = "internal_shadow"
    INTERNAL_ENFORCE = "internal_enforce"
    COHORT_ENFORCE = "cohort_enforce"
    FULL_ENFORCE = "full_enforce"
    LEGACY_ASSEMBLY_OFF = "legacy_assembly_off"
    ROLLBACK_DRILL = "rollback_drill"
    RESOURCE_BASELINE = "resource_baseline"
    RELEASE_TAG = "release_tag"


class MCPMetricName(StrEnum):
    ROUTE_REQUESTS_TOTAL = "mcp_route_requests_total"
    ROUTE_SHADOW_MISMATCH_TOTAL = "mcp_route_shadow_mismatch_total"
    GATEWAY_ACTIVE_SCOPES = "mcp_gateway_active_scopes"
    GATEWAY_CONNECT_DURATION_SECONDS = "mcp_gateway_connect_duration_seconds"
    TOOLS_LIST_DURATION_SECONDS = "mcp_tools_list_duration_seconds"
    TOOLS_LIST_ATTEMPTS_TOTAL = "mcp_tools_list_attempts_total"
    TOOL_CALLS_ACTIVE = "mcp_tool_calls_active"
    TOOL_CALLS_TOTAL = "mcp_tool_calls_total"
    TOOL_CALL_DURATION_SECONDS = "mcp_tool_call_duration_seconds"
    TOOL_CALL_UNKNOWN_TOTAL = "mcp_tool_call_unknown_total"
    PERMISSION_DECISIONS_TOTAL = "mcp_permission_decisions_total"
    DISCONNECT_LEASE_EXPIRED_TOTAL = "mcp_disconnect_lease_expired_total"
    TEMP_SPILL_BYTES = "mcp_temp_spill_bytes"
    RESOURCE_CLEANUP_FAILURES_TOTAL = "mcp_resource_cleanup_failures_total"
    PROTOCOL_NEGOTIATION_TOTAL = "mcp_protocol_negotiation_total"
    SERVER_DISCOVER_DURATION_SECONDS = "mcp_server_discover_duration_seconds"
    MRTR_ROUNDS_TOTAL = "mcp_mrtr_rounds_total"
    REMOTE_TASKS_ACTIVE = "mcp_remote_tasks_active"
    SAFETY_RED_LINE_TOTAL = "mcp_safety_red_line_total"
    RESULT_PARSER_OUTCOMES_TOTAL = "mcp_result_parser_outcomes_total"
    RESULT_PARSER_DURATION_SECONDS = "mcp_result_parser_duration_seconds"


class MCPMetricExecutionPath(StrEnum):
    LEGACY = "legacy"
    USER_SCOPED = "user_scoped"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class MCPMetricRoutingMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"
    NOT_APPLICABLE = "not_applicable"


class MCPMetricTransport(StrEnum):
    STREAMABLE_HTTP = "streamable_http"
    LEGACY_HTTP_SSE = "legacy_http_sse"
    NOT_APPLICABLE = "not_applicable"


class MCPMetricProtocolVersion(StrEnum):
    V2024_11_05 = "2024-11-05"
    V2025_03_26 = "2025-03-26"
    V2025_06_18 = "2025-06-18"
    V2025_11_25 = "2025-11-25"
    V2026_07_28 = "2026-07-28"
    NOT_APPLICABLE = "not_applicable"


class MCPMetricAdapter(StrEnum):
    PYTHON_LEGACY = "python_legacy"
    PYTHON_2026 = "python_2026"
    RUST_SIDECAR = "rust_sidecar"
    LEGACY_GLOBAL_RUNTIME = "legacy_global_runtime"
    NOT_APPLICABLE = "not_applicable"


class MCPMetricResultCategory(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    INPUT_REQUIRED = "input_required"
    TASK_CREATED = "task_created"
    PERMISSION_DENIED = "permission_denied"
    NOT_COMPARABLE = "not_comparable"
    NOT_APPLICABLE = "not_applicable"


class MCPMetricErrorCategory(StrEnum):
    NONE = "none"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    ENDPOINT_POLICY = "endpoint_policy"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    SERVER = "server"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
    VALIDATION = "validation"
    CLEANUP = "cleanup"
    NOT_APPLICABLE = "not_applicable"


class MCPLatencyBucket(StrEnum):
    LE_100_MS = "le_100_ms"
    LE_500_MS = "le_500_ms"
    LE_1_S = "le_1_s"
    LE_5_S = "le_5_s"
    LE_30_S = "le_30_s"
    LE_120_S = "le_120_s"
    GT_120_S = "gt_120_s"
    NOT_APPLICABLE = "not_applicable"


class MCPCallKind(StrEnum):
    ORDINARY = "ordinary"
    REMOTE_TASK = "remote_task"


class MCPShadowScenario(StrEnum):
    HTTPS_STREAMABLE_SUCCESS = "https_streamable_success"
    HTTPS_LEGACY_SSE_SUCCESS = "https_legacy_sse_success"
    PUBLIC_HTTP_LEGACY_SSE_SUCCESS = "public_http_legacy_sse_success"
    ALLOWLISTED_HTTP_LEGACY_SSE_SUCCESS = "allowlisted_http_legacy_sse_success"
    AUTHENTICATION_FAILURE = "authentication_failure"
    TIMEOUT = "timeout"
    PERMISSION_DENIAL = "permission_denial"
    LARGE_OUTPUT = "large_output"


class MCPRolloutDrill(StrEnum):
    CANCELLATION = "cancellation"
    LONG_CALL_120_SECONDS = "long_call_120_seconds"
    DISCONNECT_FIVE_MINUTES = "disconnect_five_minutes"
    RESTART_UNKNOWN = "restart_unknown"
    MRTR_RECOVERY = "mrtr_recovery"
    TASKS_RECOVERY = "tasks_recovery"
    FAIR_QUEUEING = "fair_queueing"
    FLAG_ROLLBACK = "flag_rollback"


class MCPSafetyRedLine(StrEnum):
    CROSS_USER_ACCESS = "cross_user_access"
    SECRET_EXPOSURE = "secret_exposure"
    DUAL_TOOL_CALL = "dual_tool_call"
    UNAUTHORIZED_TOOL_CALL = "unauthorized_tool_call"
    ENDPOINT_POLICY_BYPASS = "endpoint_policy_bypass"
    UNKNOWN_RESULT_REPLAY = "unknown_result_replay"
    SHADOW_TOOL_CALL = "shadow_tool_call"
    PERSISTENT_RESOURCE_LEAK = "persistent_resource_leak"


class MCPGateBlocker(StrEnum):
    NO_EVIDENCE = "no_evidence"
    INVALID_TRANSITION = "invalid_transition"
    EVIDENCE_ID_REPLAY = "evidence_id_replay"
    NONCE_REPLAY = "nonce_replay"
    SNAPSHOT_REPLAY = "snapshot_replay"
    SNAPSHOT_NON_MONOTONIC = "snapshot_non_monotonic"
    PROVENANCE_INVALID = "provenance_invalid"
    DIGEST_INVALID = "digest_invalid"
    ATTESTATION_MISSING = "attestation_missing"
    ATTESTATION_INVALID = "attestation_invalid"
    EVIDENCE_SCOPE_MISMATCH = "evidence_scope_mismatch"
    EVIDENCE_STAGE_MISMATCH = "evidence_stage_mismatch"
    EVIDENCE_KIND_MISMATCH = "evidence_kind_mismatch"
    SOURCE_POLICY_VIOLATION = "source_policy_violation"
    PAYLOAD_INVALID = "payload_invalid"
    WINDOW_TOO_SHORT = "window_too_short"
    WINDOW_INCOMPLETE = "window_incomplete"
    METRIC_SERIES_MISSING = "metric_series_missing"
    METRIC_SUMMARY_MISMATCH = "metric_summary_mismatch"
    ZERO_DENOMINATOR = "zero_denominator"
    SAMPLE_INSUFFICIENT = "sample_insufficient"
    SCENARIO_SAMPLE_INSUFFICIENT = "scenario_sample_insufficient"
    UNRESOLVED_MISMATCH = "unresolved_mismatch"
    INVALID_SAMPLE = "invalid_sample"
    UNAPPROVED_NOT_COMPARABLE = "unapproved_not_comparable"
    REQUIRED_DRILL_MISSING = "required_drill_missing"
    RED_LINE_DATA_MISSING = "red_line_data_missing"
    SAFETY_RED_LINE = "safety_red_line"
    SAFETY_RED_LINE_NONZERO = "safety_red_line_nonzero"
    BASELINE_MISSING = "baseline_missing"
    P95_LATENCY_REGRESSED = "p95_latency_regressed"
    ERROR_RATE_REGRESSED = "error_rate_regressed"
    CI_CONFORMANCE_MISSING = "ci_conformance_missing"


class MCPGateStatus(StrEnum):
    PASSED = "passed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class MCPMetricLabels:
    execution_path: MCPMetricExecutionPath = MCPMetricExecutionPath.NOT_APPLICABLE
    routing_mode: MCPMetricRoutingMode = MCPMetricRoutingMode.NOT_APPLICABLE
    transport: MCPMetricTransport = MCPMetricTransport.NOT_APPLICABLE
    protocol_version: MCPMetricProtocolVersion = MCPMetricProtocolVersion.NOT_APPLICABLE
    adapter: MCPMetricAdapter = MCPMetricAdapter.NOT_APPLICABLE
    result_category: MCPMetricResultCategory = MCPMetricResultCategory.NOT_APPLICABLE
    error_category: MCPMetricErrorCategory = MCPMetricErrorCategory.NOT_APPLICABLE
    call_kind: MCPCallKind | None = None
    red_line: MCPSafetyRedLine | None = None


@dataclass(frozen=True, slots=True)
class MCPMetricBucket:
    metric_name: MCPMetricName
    bucket_started_at: datetime
    bucket_ended_at: datetime
    labels: MCPMetricLabels
    value: int
    latency_bucket: MCPLatencyBucket = MCPLatencyBucket.NOT_APPLICABLE


@dataclass(frozen=True, slots=True)
class MCPCallKindObservation:
    call_kind: MCPCallKind
    terminal_success_count: int
    terminal_error_count: int
    cancellation_count: int
    p95_latency_ms: float | None
    baseline_success_count: int
    baseline_error_count: int
    baseline_p95_latency_ms: float | None

    @property
    def terminal_sample_count(self) -> int:
        return self.terminal_success_count + self.terminal_error_count

    @property
    def baseline_sample_count(self) -> int:
        return self.baseline_success_count + self.baseline_error_count

    @property
    def error_rate(self) -> float | None:
        if self.terminal_sample_count <= 0:
            return None
        return self.terminal_error_count / self.terminal_sample_count

    @property
    def baseline_error_rate(self) -> float | None:
        if self.baseline_sample_count <= 0:
            return None
        return self.baseline_error_count / self.baseline_sample_count


@dataclass(frozen=True, slots=True)
class MCPShadowScenarioObservation:
    scenario: MCPShadowScenario
    matched_count: int
    mismatched_count: int = 0
    invalid_count: int = 0
    not_comparable_count: int = 0
    excluded_count: int = 0


@dataclass(frozen=True, slots=True)
class MCPRedLineCount:
    red_line: MCPSafetyRedLine
    count: int


@dataclass(frozen=True, slots=True)
class MCPRolloutEvidencePayload:
    kind: MCPEvidenceKind
    metric_buckets: tuple[MCPMetricBucket, ...] = ()
    call_kinds: tuple[MCPCallKindObservation, ...] = ()
    shadow_scenarios: tuple[MCPShadowScenarioObservation, ...] = ()
    completed_drills: frozenset[MCPRolloutDrill] = frozenset()
    red_line_counts: tuple[MCPRedLineCount, ...] = ()
    continuous_window: bool = False
    missing_bucket_count: int = 0
    invalid_evidence_count: int = 0
    unresolved_mismatch_count: int = 0
    unapproved_not_comparable_count: int = 0
    shadow_observation_count: int = 0
    pre_dispatch_excluded_count: int = 0
    ci_conformance_passed: bool = False
    manifest_fingerprint: str | None = None
    fixture_fingerprint: str | None = None
    mapping_fingerprint: str | None = None

    @property
    def terminal_sample_count(self) -> int:
        # Shadow observations, pre-dispatch exclusions, and cancellations are
        # deliberately represented outside this real terminal-call denominator.
        return sum(item.terminal_sample_count for item in self.call_kinds)


@dataclass(frozen=True, slots=True)
class MCPEvidenceSnapshot:
    evidence_id: str
    environment_id: str
    git_sha: str
    deployment_id: str
    stage: MCPRolloutStage
    config_fingerprint: str
    window_started_at: datetime
    window_ended_at: datetime
    recorded_at: datetime
    producer: MCPEvidenceProducer
    source: MCPEvidenceSource
    snapshot_id: int
    nonce: str
    payload: MCPRolloutEvidencePayload
    payload_digest: str
    attestation_key_id: str | None = None
    attestation_signature: str | None = None

    @classmethod
    def seal(
        cls,
        *,
        evidence_id: str,
        environment_id: str,
        git_sha: str,
        deployment_id: str,
        stage: MCPRolloutStage,
        config_fingerprint: str,
        window_started_at: datetime,
        window_ended_at: datetime,
        recorded_at: datetime,
        producer: MCPEvidenceProducer,
        source: MCPEvidenceSource,
        snapshot_id: int,
        nonce: str,
        payload: MCPRolloutEvidencePayload,
        attestation_key_id: str | None = None,
        attestation_key: bytes | None = None,
    ) -> MCPEvidenceSnapshot:
        if source is MCPEvidenceSource.PRODUCTION:
            if (
                not attestation_key_id
                or not isinstance(attestation_key, bytes)
                or not attestation_key
            ):
                raise ValueError(
                    "production evidence requires an attestation key id and key"
                )
        elif attestation_key_id is not None or attestation_key is not None:
            raise ValueError("CI evidence must not carry a production attestation")
        draft = cls(
            evidence_id=evidence_id,
            environment_id=environment_id,
            git_sha=git_sha,
            deployment_id=deployment_id,
            stage=stage,
            config_fingerprint=config_fingerprint,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            recorded_at=recorded_at,
            producer=producer,
            source=source,
            snapshot_id=snapshot_id,
            nonce=nonce,
            payload=payload,
            payload_digest="",
            attestation_key_id=attestation_key_id,
        )
        sealed = replace(draft, payload_digest=canonical_evidence_digest(draft))
        if source is MCPEvidenceSource.PRODUCTION:
            assert attestation_key_id is not None
            assert attestation_key is not None
            sealed = replace(
                sealed,
                attestation_signature=_evidence_attestation_signature(
                    sealed,
                    key_id=attestation_key_id,
                    key=attestation_key,
                ),
            )
        return sealed


@dataclass(frozen=True, slots=True)
class MCPRolloutStageApproval:
    approval_id: str
    environment_id: str
    deployment_id: str
    stage: MCPRolloutStage
    config_fingerprint: str
    evidence_id: str
    reason: str
    approver: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MCPRolloutDeploymentActivation:
    activation_id: str
    environment_id: str
    deployment_id: str
    stage: MCPRolloutStage
    config_fingerprint: str
    approval_id: str
    previous_activation_id: str | None
    operator_reason: str
    is_rollback: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MCPRolloutPromotionBlock:
    block_id: str
    environment_id: str
    deployment_id: str
    stage: MCPRolloutStage
    config_fingerprint: str
    evidence_id: str
    reason_code: MCPGateBlocker
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MCPRolloutBlockResolution:
    resolution_id: str
    block_id: str
    approval_id: str
    evidence_id: str
    reason: str
    approver: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MCPEvidenceRecordValidation:
    valid: bool
    blockers: tuple[MCPGateBlocker, ...]
    invalid_evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MCPStageGateRequest:
    evidence_id: str
    environment_id: str
    evidence_deployment_id: str
    evidence_config_fingerprint: str
    current_stage: MCPRolloutStage
    target_stage: MCPRolloutStage


@dataclass(frozen=True, slots=True)
class MCPStageGateEvaluation:
    status: MCPGateStatus
    blockers: tuple[MCPGateBlocker, ...]
    evidence_id: str | None
    observed_stage: MCPRolloutStage | None
    target_stage: MCPRolloutStage

    @property
    def allowed(self) -> bool:
        return self.status is MCPGateStatus.PASSED


CURRENT_MCP_SHADOW_SCENARIOS = (
    MCPShadowScenario.HTTPS_STREAMABLE_SUCCESS,
    MCPShadowScenario.HTTPS_LEGACY_SSE_SUCCESS,
    MCPShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS,
    MCPShadowScenario.AUTHENTICATION_FAILURE,
    MCPShadowScenario.TIMEOUT,
    MCPShadowScenario.PERMISSION_DENIAL,
    MCPShadowScenario.LARGE_OUTPUT,
)
_REQUIRED_SHADOW_SCENARIOS = frozenset(CURRENT_MCP_SHADOW_SCENARIOS)
_REQUIRED_INTERNAL_ENFORCE_DRILLS = frozenset(MCPRolloutDrill)
_REQUIRED_PERFORMANCE_CALL_KINDS = frozenset(MCPCallKind)
_EXPECTED_PRODUCER = {
    MCPEvidenceSource.CI: MCPEvidenceProducer.CI_PIPELINE,
    MCPEvidenceSource.PRODUCTION: MCPEvidenceProducer.PRODUCTION_SNAPSHOT,
}
_EXPECTED_KIND_BY_STAGE = {
    MCPRolloutStage.INTERNAL_SHADOW: MCPEvidenceKind.INTERNAL_SHADOW,
    MCPRolloutStage.INTERNAL_ENFORCE: MCPEvidenceKind.INTERNAL_ENFORCE,
    MCPRolloutStage.COHORT_ENFORCE: MCPEvidenceKind.COHORT_ENFORCE,
    MCPRolloutStage.FULL_ENFORCE: MCPEvidenceKind.FULL_ENFORCE,
    MCPRolloutStage.LEGACY_ASSEMBLY_OFF: MCPEvidenceKind.LEGACY_ASSEMBLY_OFF,
}
_VALID_TRANSITIONS = frozenset(
    {
        (MCPRolloutStage.OFF, MCPRolloutStage.INTERNAL_SHADOW),
        (MCPRolloutStage.INTERNAL_SHADOW, MCPRolloutStage.INTERNAL_ENFORCE),
        (MCPRolloutStage.INTERNAL_ENFORCE, MCPRolloutStage.COHORT_ENFORCE),
        (MCPRolloutStage.COHORT_ENFORCE, MCPRolloutStage.COHORT_ENFORCE),
        (MCPRolloutStage.COHORT_ENFORCE, MCPRolloutStage.FULL_ENFORCE),
        (MCPRolloutStage.FULL_ENFORCE, MCPRolloutStage.LEGACY_ASSEMBLY_OFF),
    }
)


def canonical_evidence_digest(snapshot: MCPEvidenceSnapshot) -> str:
    payload = {
        field.name: getattr(snapshot, field.name)
        for field in fields(snapshot)
        if field.name
        not in {"payload_digest", "attestation_key_id", "attestation_signature"}
    }
    return canonical_evidence_content_digest(payload)


def canonical_evidence_content_digest(content: Mapping[str, object]) -> str:
    """Digest canonical evidence content without requiring a domain reconstruction."""

    encoded = json.dumps(
        _canonical_value(content),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_evidence_snapshot(
    snapshot: MCPEvidenceSnapshot,
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> tuple[MCPGateBlocker, ...]:
    blockers: list[MCPGateBlocker] = []
    if not isinstance(snapshot.stage, MCPRolloutStage):
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    if not isinstance(snapshot.source, MCPEvidenceSource) or not isinstance(
        snapshot.producer, MCPEvidenceProducer
    ):
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    elif _EXPECTED_PRODUCER.get(snapshot.source) is not snapshot.producer:
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    for value in (
        snapshot.evidence_id,
        snapshot.environment_id,
        snapshot.deployment_id,
        snapshot.config_fingerprint,
        snapshot.nonce,
    ):
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
            break
    if not isinstance(snapshot.git_sha, str) or not _GIT_SHA_RE.fullmatch(
        snapshot.git_sha
    ):
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    if not _is_positive_int(snapshot.snapshot_id):
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    if not _valid_window(snapshot.window_started_at, snapshot.window_ended_at):
        blockers.append(MCPGateBlocker.WINDOW_INCOMPLETE)
    if not _is_aware(snapshot.recorded_at) or (
        _is_aware(snapshot.window_ended_at)
        and snapshot.recorded_at < snapshot.window_ended_at
    ):
        blockers.append(MCPGateBlocker.PROVENANCE_INVALID)
    if not isinstance(snapshot.payload, MCPRolloutEvidencePayload):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    else:
        blockers.extend(_validate_payload(snapshot))
        if snapshot.source is MCPEvidenceSource.PRODUCTION:
            blockers.extend(_production_metric_blockers(snapshot))
    if not isinstance(snapshot.payload_digest, str) or not _DIGEST_RE.fullmatch(
        snapshot.payload_digest
    ):
        blockers.append(MCPGateBlocker.DIGEST_INVALID)
    else:
        try:
            digest = canonical_evidence_digest(snapshot)
        except (TypeError, ValueError):
            blockers.append(MCPGateBlocker.DIGEST_INVALID)
        else:
            if not _constant_time_equal(snapshot.payload_digest, digest):
                blockers.append(MCPGateBlocker.DIGEST_INVALID)
    blockers.extend(_attestation_blockers(snapshot, trusted_attestation_keys))
    return _ordered_unique(blockers)


def validate_evidence_records(
    records: Sequence[MCPEvidenceSnapshot],
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPEvidenceRecordValidation:
    blockers: list[MCPGateBlocker] = []
    invalid_ids: list[str] = []
    evidence_ids: set[str] = set()
    nonces: set[str] = set()
    snapshot_keys: set[tuple[str, MCPRolloutStage, int]] = set()
    groups: dict[tuple[str, str, MCPRolloutStage], list[MCPEvidenceSnapshot]] = {}

    for record in records:
        record_blockers = validate_evidence_snapshot(
            record,
            trusted_attestation_keys=trusted_attestation_keys,
        )
        blockers.extend(record_blockers)
        if record_blockers:
            invalid_ids.append(record.evidence_id)
        if record.evidence_id in evidence_ids:
            blockers.append(MCPGateBlocker.EVIDENCE_ID_REPLAY)
            invalid_ids.append(record.evidence_id)
        evidence_ids.add(record.evidence_id)
        if record.nonce in nonces:
            blockers.append(MCPGateBlocker.NONCE_REPLAY)
            invalid_ids.append(record.evidence_id)
        nonces.add(record.nonce)
        snapshot_key = (record.deployment_id, record.stage, record.snapshot_id)
        if snapshot_key in snapshot_keys:
            blockers.append(MCPGateBlocker.SNAPSHOT_REPLAY)
            invalid_ids.append(record.evidence_id)
        snapshot_keys.add(snapshot_key)
        groups.setdefault(
            (record.environment_id, record.deployment_id, record.stage), []
        ).append(record)

    for group in groups.values():
        chronological = sorted(
            group, key=lambda item: _datetime_sort_key(item.recorded_at)
        )
        previous: MCPEvidenceSnapshot | None = None
        for record in chronological:
            if previous is not None:
                if record.snapshot_id <= previous.snapshot_id:
                    blockers.append(MCPGateBlocker.SNAPSHOT_NON_MONOTONIC)
                    invalid_ids.append(record.evidence_id)
                if (
                    _is_aware(record.window_ended_at)
                    and _is_aware(previous.window_ended_at)
                    and record.window_ended_at <= previous.window_ended_at
                ):
                    blockers.append(MCPGateBlocker.SNAPSHOT_NON_MONOTONIC)
                    invalid_ids.append(record.evidence_id)
                if (
                    _is_aware(record.window_started_at)
                    and _is_aware(previous.window_ended_at)
                    and record.window_started_at > previous.window_ended_at
                ):
                    blockers.append(MCPGateBlocker.WINDOW_INCOMPLETE)
                    invalid_ids.append(record.evidence_id)
                if record.config_fingerprint != previous.config_fingerprint:
                    blockers.append(MCPGateBlocker.EVIDENCE_SCOPE_MISMATCH)
                    invalid_ids.append(record.evidence_id)
            previous = record

    normalized_blockers = _ordered_unique(blockers)
    return MCPEvidenceRecordValidation(
        valid=not normalized_blockers,
        blockers=normalized_blockers,
        invalid_evidence_ids=tuple(dict.fromkeys(invalid_ids)),
    )


def evaluate_mcp_stage_observation(
    snapshot: MCPEvidenceSnapshot,
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> tuple[MCPGateBlocker, ...]:
    blockers = list(
        validate_evidence_snapshot(
            snapshot,
            trusted_attestation_keys=trusted_attestation_keys,
        )
    )
    payload = snapshot.payload
    if not isinstance(payload, MCPRolloutEvidencePayload):
        return _ordered_unique(blockers)

    blockers.extend(_common_observation_blockers(payload))
    if snapshot.stage is MCPRolloutStage.OFF:
        if payload.kind is not MCPEvidenceKind.CI_CONFORMANCE:
            blockers.append(MCPGateBlocker.EVIDENCE_KIND_MISMATCH)
        if not payload.ci_conformance_passed:
            blockers.append(MCPGateBlocker.CI_CONFORMANCE_MISSING)
        return _ordered_unique(blockers)

    expected_kind = _EXPECTED_KIND_BY_STAGE.get(snapshot.stage)
    if expected_kind is not None and payload.kind is not expected_kind:
        blockers.append(MCPGateBlocker.EVIDENCE_KIND_MISMATCH)

    if not payload.continuous_window or payload.missing_bucket_count != 0:
        blockers.append(MCPGateBlocker.WINDOW_INCOMPLETE)

    if snapshot.stage is MCPRolloutStage.INTERNAL_SHADOW:
        _require_window_hours(snapshot, minimum=24, blockers=blockers)
        blockers.extend(_shadow_scenario_blockers(payload))
    elif snapshot.stage is MCPRolloutStage.INTERNAL_ENFORCE:
        _require_window_hours(snapshot, minimum=48, blockers=blockers)
        if payload.terminal_sample_count <= 0:
            blockers.append(MCPGateBlocker.ZERO_DENOMINATOR)
        if not _REQUIRED_INTERNAL_ENFORCE_DRILLS.issubset(payload.completed_drills):
            blockers.append(MCPGateBlocker.REQUIRED_DRILL_MISSING)
    elif snapshot.stage in {
        MCPRolloutStage.COHORT_ENFORCE,
        MCPRolloutStage.FULL_ENFORCE,
        MCPRolloutStage.LEGACY_ASSEMBLY_OFF,
    }:
        _require_window_hours(snapshot, minimum=24 * 7, blockers=blockers)
        if payload.terminal_sample_count <= 0:
            blockers.append(MCPGateBlocker.ZERO_DENOMINATOR)
        elif payload.terminal_sample_count < 1000:
            blockers.append(MCPGateBlocker.SAMPLE_INSUFFICIENT)
        blockers.extend(_performance_blockers(payload))
    return _ordered_unique(blockers)


def evaluate_mcp_stage_gate(
    request: MCPStageGateRequest,
    records: Sequence[MCPEvidenceSnapshot],
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPStageGateEvaluation:
    blockers: list[MCPGateBlocker] = []
    if (request.current_stage, request.target_stage) not in _VALID_TRANSITIONS:
        blockers.append(MCPGateBlocker.INVALID_TRANSITION)

    record_validation = validate_evidence_records(
        records,
        trusted_attestation_keys=trusted_attestation_keys,
    )
    blockers.extend(record_validation.blockers)
    matching = [
        record for record in records if record.evidence_id == request.evidence_id
    ]
    if not matching:
        blockers.append(MCPGateBlocker.NO_EVIDENCE)
        return _gate_evaluation(request, None, blockers)
    if len(matching) != 1:
        blockers.append(MCPGateBlocker.EVIDENCE_ID_REPLAY)
    snapshot = matching[0]

    if (
        snapshot.environment_id != request.environment_id
        or snapshot.deployment_id != request.evidence_deployment_id
        or snapshot.config_fingerprint != request.evidence_config_fingerprint
    ):
        blockers.append(MCPGateBlocker.EVIDENCE_SCOPE_MISMATCH)
    if snapshot.stage is not request.current_stage:
        blockers.append(MCPGateBlocker.EVIDENCE_STAGE_MISMATCH)

    if (
        request.current_stage is MCPRolloutStage.OFF
        and request.target_stage is MCPRolloutStage.INTERNAL_SHADOW
    ):
        if snapshot.source is not MCPEvidenceSource.CI:
            blockers.append(MCPGateBlocker.SOURCE_POLICY_VIOLATION)
        if snapshot.payload.kind is not MCPEvidenceKind.CI_CONFORMANCE:
            blockers.append(MCPGateBlocker.EVIDENCE_KIND_MISMATCH)
    elif snapshot.source is not MCPEvidenceSource.PRODUCTION:
        blockers.append(MCPGateBlocker.SOURCE_POLICY_VIOLATION)

    blockers.extend(
        evaluate_mcp_stage_observation(
            snapshot,
            trusted_attestation_keys=trusted_attestation_keys,
        )
    )
    return _gate_evaluation(request, snapshot, blockers)


def is_provable_mcp_exposure_decrease(
    current: MCPRolloutConfig,
    candidate: MCPRolloutConfig,
) -> bool:
    return is_strict_mcp_exposure_decrease(current, candidate)


def active_mcp_promotion_blocks(
    blocks: Iterable[MCPRolloutPromotionBlock],
    resolutions: Iterable[MCPRolloutBlockResolution],
) -> tuple[MCPRolloutPromotionBlock, ...]:
    resolved_block_ids = {resolution.block_id for resolution in resolutions}
    return tuple(block for block in blocks if block.block_id not in resolved_block_ids)


def _validate_payload(snapshot: MCPEvidenceSnapshot) -> tuple[MCPGateBlocker, ...]:
    payload = snapshot.payload
    blockers: list[MCPGateBlocker] = []
    if not isinstance(payload.kind, MCPEvidenceKind):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    count_values = (
        payload.missing_bucket_count,
        payload.invalid_evidence_count,
        payload.unresolved_mismatch_count,
        payload.unapproved_not_comparable_count,
        payload.shadow_observation_count,
        payload.pre_dispatch_excluded_count,
    )
    if any(not _is_nonnegative_int(value) for value in count_values):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    if not isinstance(payload.continuous_window, bool) or not isinstance(
        payload.ci_conformance_passed, bool
    ):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    fingerprints = (
        payload.manifest_fingerprint,
        payload.fixture_fingerprint,
        payload.mapping_fingerprint,
    )
    if payload.kind is MCPEvidenceKind.INTERNAL_SHADOW:
        if any(
            not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None
            for value in fingerprints
        ):
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    elif any(value is not None for value in fingerprints):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    if any(
        not isinstance(drill, MCPRolloutDrill) for drill in payload.completed_drills
    ):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)

    scenario_keys: set[MCPShadowScenario] = set()
    for scenario_observation in payload.shadow_scenarios:
        counts = (
            scenario_observation.matched_count,
            scenario_observation.mismatched_count,
            scenario_observation.invalid_count,
            scenario_observation.not_comparable_count,
            scenario_observation.excluded_count,
        )
        if not isinstance(scenario_observation.scenario, MCPShadowScenario) or any(
            not _is_nonnegative_int(value) for value in counts
        ):
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        if scenario_observation.scenario in scenario_keys:
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        scenario_keys.add(scenario_observation.scenario)

    call_kind_keys: set[MCPCallKind] = set()
    for call_kind_observation in payload.call_kinds:
        counts = (
            call_kind_observation.terminal_success_count,
            call_kind_observation.terminal_error_count,
            call_kind_observation.cancellation_count,
            call_kind_observation.baseline_success_count,
            call_kind_observation.baseline_error_count,
        )
        if not isinstance(call_kind_observation.call_kind, MCPCallKind) or any(
            not _is_nonnegative_int(value) for value in counts
        ):
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        if not _is_optional_nonnegative_finite(
            call_kind_observation.p95_latency_ms
        ) or not _is_optional_nonnegative_finite(
            call_kind_observation.baseline_p95_latency_ms
        ):
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        if call_kind_observation.call_kind in call_kind_keys:
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        call_kind_keys.add(call_kind_observation.call_kind)

    red_line_keys: set[MCPSafetyRedLine] = set()
    for red_line_count in payload.red_line_counts:
        if not isinstance(
            red_line_count.red_line, MCPSafetyRedLine
        ) or not _is_nonnegative_int(red_line_count.count):
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        if red_line_count.red_line in red_line_keys:
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        red_line_keys.add(red_line_count.red_line)

    valid_metric_buckets: list[MCPMetricBucket] = []
    for metric_bucket in payload.metric_buckets:
        if not _valid_metric_bucket(metric_bucket, snapshot):
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
        else:
            valid_metric_buckets.append(metric_bucket)
    if _has_overlapping_metric_bucket_identity(valid_metric_buckets):
        blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
    return _ordered_unique(blockers)


def _valid_metric_bucket(item: MCPMetricBucket, snapshot: MCPEvidenceSnapshot) -> bool:
    if not isinstance(item, MCPMetricBucket):
        return False
    if not isinstance(item.metric_name, MCPMetricName) or not isinstance(
        item.labels, MCPMetricLabels
    ):
        return False
    if not isinstance(item.latency_bucket, MCPLatencyBucket) or not _is_nonnegative_int(
        item.value
    ):
        return False
    if not is_exact_mcp_metric_bucket_window(
        item.bucket_started_at, item.bucket_ended_at
    ):
        return False
    if (
        item.bucket_started_at < snapshot.window_started_at
        or item.bucket_ended_at > snapshot.window_ended_at
    ):
        return False
    enum_fields = (
        (item.labels.execution_path, MCPMetricExecutionPath),
        (item.labels.routing_mode, MCPMetricRoutingMode),
        (item.labels.transport, MCPMetricTransport),
        (item.labels.protocol_version, MCPMetricProtocolVersion),
        (item.labels.adapter, MCPMetricAdapter),
        (item.labels.result_category, MCPMetricResultCategory),
        (item.labels.error_category, MCPMetricErrorCategory),
    )
    if any(not isinstance(value, enum_type) for value, enum_type in enum_fields):
        return False
    if item.labels.call_kind is not None and not isinstance(
        item.labels.call_kind, MCPCallKind
    ):
        return False
    return item.labels.red_line is None or isinstance(
        item.labels.red_line, MCPSafetyRedLine
    )


def _has_overlapping_metric_bucket_identity(
    buckets: Sequence[MCPMetricBucket],
) -> bool:
    # Exact, aligned one-minute windows overlap only when they share a start.
    seen: set[
        tuple[MCPMetricName, MCPMetricLabels, MCPLatencyBucket, datetime]
    ] = set()
    for bucket in buckets:
        identity = (
            bucket.metric_name,
            bucket.labels,
            bucket.latency_bucket,
            bucket.bucket_started_at.astimezone(timezone.utc),
        )
        if identity in seen:
            return True
        seen.add(identity)
    return False


def _attestation_blockers(
    snapshot: MCPEvidenceSnapshot,
    trusted_attestation_keys: Mapping[str, bytes] | None,
) -> tuple[MCPGateBlocker, ...]:
    key_id = snapshot.attestation_key_id
    signature = snapshot.attestation_signature
    if snapshot.source is not MCPEvidenceSource.PRODUCTION:
        if key_id is not None or signature is not None:
            return (MCPGateBlocker.ATTESTATION_INVALID,)
        return ()
    if (
        not isinstance(key_id, str)
        or not _IDENTIFIER_RE.fullmatch(key_id)
        or not isinstance(signature, str)
        or not _DIGEST_RE.fullmatch(signature)
    ):
        return (MCPGateBlocker.ATTESTATION_MISSING,)
    if trusted_attestation_keys is None:
        return (MCPGateBlocker.ATTESTATION_MISSING,)
    key = trusted_attestation_keys.get(key_id)
    if not isinstance(key, bytes) or not key:
        return (MCPGateBlocker.ATTESTATION_INVALID,)
    expected = _evidence_attestation_signature(snapshot, key_id=key_id, key=key)
    if not _constant_time_equal(signature, expected):
        return (MCPGateBlocker.ATTESTATION_INVALID,)
    return ()


def _evidence_attestation_signature(
    snapshot: MCPEvidenceSnapshot,
    *,
    key_id: str,
    key: bytes,
) -> str:
    return canonical_evidence_attestation_signature(
        {
            "payload_digest": snapshot.payload_digest,
            "evidence_id": snapshot.evidence_id,
            "environment_id": snapshot.environment_id,
            "git_sha": snapshot.git_sha,
            "deployment_id": snapshot.deployment_id,
            "stage": snapshot.stage,
            "config_fingerprint": snapshot.config_fingerprint,
            "snapshot_id": snapshot.snapshot_id,
            "nonce": snapshot.nonce,
        },
        key_id=key_id,
        key=key,
    )


def canonical_evidence_attestation_signature(
    content: Mapping[str, object],
    *,
    key_id: str,
    key: bytes,
) -> str:
    """Sign the established v1 evidence identity from canonical field values."""

    identity = {
        "domain": "user-mcp-rollout-evidence-v1",
        "key_id": key_id,
        **content,
    }
    encoded = json.dumps(
        _canonical_value(identity),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _production_metric_blockers(
    snapshot: MCPEvidenceSnapshot,
) -> tuple[MCPGateBlocker, ...]:
    payload = snapshot.payload
    blockers: list[MCPGateBlocker] = []
    if not payload.metric_buckets:
        return (
            MCPGateBlocker.METRIC_SERIES_MISSING,
            MCPGateBlocker.WINDOW_INCOMPLETE,
        )

    red_line_buckets: dict[MCPSafetyRedLine, list[MCPMetricBucket]] = {}
    terminal_buckets: dict[
        tuple[MCPCallKind, MCPMetricResultCategory], list[MCPMetricBucket]
    ] = {}
    for bucket in payload.metric_buckets:
        if not _valid_metric_bucket(bucket, snapshot):
            continue
        if bucket.metric_name is MCPMetricName.SAFETY_RED_LINE_TOTAL:
            if bucket.labels.red_line is None:
                blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
            else:
                red_line_buckets.setdefault(bucket.labels.red_line, []).append(bucket)
        elif bucket.metric_name is MCPMetricName.TOOL_CALLS_TOTAL:
            if bucket.labels.call_kind is None or bucket.labels.red_line is not None:
                blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
            elif bucket.labels.execution_path is not MCPMetricExecutionPath.LEGACY:
                # Legacy-path terminal buckets are the durable performance
                # baseline. They must not be folded into the user-scoped
                # terminal summaries or their required-series coverage.
                terminal_buckets.setdefault(
                    (bucket.labels.call_kind, bucket.labels.result_category),
                    [],
                ).append(bucket)

    missing_series = 0
    derived_red_lines: dict[MCPSafetyRedLine, int] = {}
    for red_line in MCPSafetyRedLine:
        series = red_line_buckets.get(red_line, [])
        if not series or not _series_covers_snapshot(series, snapshot):
            missing_series += 1
            continue
        derived_red_lines[red_line] = sum(bucket.value for bucket in series)

    reported_red_lines = {item.red_line: item.count for item in payload.red_line_counts}
    if derived_red_lines != reported_red_lines:
        blockers.append(MCPGateBlocker.METRIC_SUMMARY_MISMATCH)

    reported_call_kinds = {item.call_kind: item for item in payload.call_kinds}
    required_call_kinds = set(reported_call_kinds)
    if snapshot.stage in {
        MCPRolloutStage.COHORT_ENFORCE,
        MCPRolloutStage.FULL_ENFORCE,
        MCPRolloutStage.LEGACY_ASSEMBLY_OFF,
    }:
        required_call_kinds.update(_REQUIRED_PERFORMANCE_CALL_KINDS)

    for call_kind in required_call_kinds:
        counts: dict[MCPMetricResultCategory, int] = {}
        for category in (
            MCPMetricResultCategory.SUCCEEDED,
            MCPMetricResultCategory.FAILED,
            MCPMetricResultCategory.UNKNOWN,
            MCPMetricResultCategory.CANCELLED,
        ):
            series = terminal_buckets.get((call_kind, category), [])
            if not series or not _series_covers_snapshot(series, snapshot):
                missing_series += 1
                continue
            counts[category] = sum(bucket.value for bucket in series)
        reported = reported_call_kinds.get(call_kind)
        if reported is None:
            continue
        if (
            counts.get(MCPMetricResultCategory.SUCCEEDED)
            != reported.terminal_success_count
            or (
                counts.get(MCPMetricResultCategory.FAILED, 0)
                + counts.get(MCPMetricResultCategory.UNKNOWN, 0)
            )
            != reported.terminal_error_count
            or counts.get(MCPMetricResultCategory.CANCELLED)
            != reported.cancellation_count
        ):
            blockers.append(MCPGateBlocker.METRIC_SUMMARY_MISMATCH)

    if missing_series:
        blockers.append(MCPGateBlocker.METRIC_SERIES_MISSING)
        blockers.append(MCPGateBlocker.WINDOW_INCOMPLETE)
    derived_continuous_window = missing_series == 0
    if (
        payload.continuous_window is not derived_continuous_window
        or payload.missing_bucket_count != missing_series
    ):
        blockers.append(MCPGateBlocker.METRIC_SUMMARY_MISMATCH)
    return _ordered_unique(blockers)


def _series_covers_snapshot(
    buckets: Sequence[MCPMetricBucket],
    snapshot: MCPEvidenceSnapshot,
) -> bool:
    ordered = sorted(buckets, key=lambda item: item.bucket_started_at)
    if not ordered or ordered[0].bucket_started_at != snapshot.window_started_at:
        return False
    covered_until = ordered[0].bucket_ended_at
    for current in ordered[1:]:
        # Multiple closed-label series may overlap the zero-fill series in the
        # same minute. Treat their interval union as coverage rather than
        # rejecting a real event merely because the zero bucket also exists.
        if current.bucket_started_at > covered_until:
            return False
        if current.bucket_ended_at > covered_until:
            covered_until = current.bucket_ended_at
    return covered_until == snapshot.window_ended_at


def _common_observation_blockers(
    payload: MCPRolloutEvidencePayload,
) -> tuple[MCPGateBlocker, ...]:
    blockers: list[MCPGateBlocker] = []
    red_line_map = {item.red_line: item.count for item in payload.red_line_counts}
    if set(red_line_map) != set(MCPSafetyRedLine):
        blockers.append(MCPGateBlocker.RED_LINE_DATA_MISSING)
    if any(count > 0 for count in red_line_map.values()):
        blockers.append(MCPGateBlocker.SAFETY_RED_LINE)
    if payload.invalid_evidence_count > 0:
        blockers.append(MCPGateBlocker.INVALID_SAMPLE)
    if payload.unresolved_mismatch_count > 0:
        blockers.append(MCPGateBlocker.UNRESOLVED_MISMATCH)
    if payload.unapproved_not_comparable_count > 0:
        blockers.append(MCPGateBlocker.UNAPPROVED_NOT_COMPARABLE)
    return _ordered_unique(blockers)


def _shadow_scenario_blockers(
    payload: MCPRolloutEvidencePayload,
) -> tuple[MCPGateBlocker, ...]:
    blockers: list[MCPGateBlocker] = []
    by_scenario = {item.scenario: item for item in payload.shadow_scenarios}
    if not by_scenario:
        blockers.append(MCPGateBlocker.ZERO_DENOMINATOR)
    for scenario in _REQUIRED_SHADOW_SCENARIOS:
        item = by_scenario.get(scenario)
        if item is None or item.matched_count < 3:
            blockers.append(MCPGateBlocker.SCENARIO_SAMPLE_INSUFFICIENT)
            continue
        if item.mismatched_count > 0:
            blockers.append(MCPGateBlocker.UNRESOLVED_MISMATCH)
        if item.invalid_count > 0:
            blockers.append(MCPGateBlocker.INVALID_SAMPLE)
        if item.not_comparable_count > 0:
            blockers.append(MCPGateBlocker.UNAPPROVED_NOT_COMPARABLE)
    return _ordered_unique(blockers)


def _performance_blockers(
    payload: MCPRolloutEvidencePayload,
) -> tuple[MCPGateBlocker, ...]:
    blockers: list[MCPGateBlocker] = []
    by_kind = {item.call_kind: item for item in payload.call_kinds}
    for call_kind in _REQUIRED_PERFORMANCE_CALL_KINDS:
        item = by_kind.get(call_kind)
        if item is None or item.terminal_sample_count <= 0:
            blockers.append(MCPGateBlocker.ZERO_DENOMINATOR)
            continue
        if item.baseline_sample_count <= 0 or item.baseline_p95_latency_ms is None:
            blockers.append(MCPGateBlocker.BASELINE_MISSING)
            continue
        if item.p95_latency_ms is None:
            blockers.append(MCPGateBlocker.PAYLOAD_INVALID)
            continue
        error_rate = item.error_rate
        baseline_error_rate = item.baseline_error_rate
        if error_rate is None or baseline_error_rate is None:
            blockers.append(MCPGateBlocker.BASELINE_MISSING)
        elif error_rate > baseline_error_rate:
            blockers.append(MCPGateBlocker.ERROR_RATE_REGRESSED)
        if item.p95_latency_ms > item.baseline_p95_latency_ms * 1.10:
            blockers.append(MCPGateBlocker.P95_LATENCY_REGRESSED)
    return _ordered_unique(blockers)


def _require_window_hours(
    snapshot: MCPEvidenceSnapshot,
    *,
    minimum: int,
    blockers: list[MCPGateBlocker],
) -> None:
    if not _valid_window(snapshot.window_started_at, snapshot.window_ended_at):
        blockers.append(MCPGateBlocker.WINDOW_INCOMPLETE)
        return
    seconds = (snapshot.window_ended_at - snapshot.window_started_at).total_seconds()
    if seconds < minimum * 60 * 60:
        blockers.append(MCPGateBlocker.WINDOW_TOO_SHORT)


def _gate_evaluation(
    request: MCPStageGateRequest,
    snapshot: MCPEvidenceSnapshot | None,
    blockers: Iterable[MCPGateBlocker],
) -> MCPStageGateEvaluation:
    normalized = _ordered_unique(blockers)
    return MCPStageGateEvaluation(
        status=MCPGateStatus.BLOCKED if normalized else MCPGateStatus.PASSED,
        blockers=normalized,
        evidence_id=snapshot.evidence_id if snapshot is not None else None,
        observed_stage=snapshot.stage if snapshot is not None else None,
        target_stage=request.target_stage,
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        if not _is_aware(value):
            raise ValueError("canonical evidence timestamps must be timezone-aware")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        rendered = [_canonical_value(item) for item in value]
        return sorted(
            rendered,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical evidence numbers must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical evidence type: {type(value).__name__}")


def _ordered_unique(values: Iterable[MCPGateBlocker]) -> tuple[MCPGateBlocker, ...]:
    return tuple(dict.fromkeys(values))


def _valid_window(started_at: datetime, ended_at: datetime) -> bool:
    return _is_aware(started_at) and _is_aware(ended_at) and ended_at > started_at


def is_exact_mcp_metric_bucket_window(
    started_at: datetime, ended_at: datetime
) -> bool:
    """Return whether a metric bucket is one complete UTC minute."""

    if not _is_aware(started_at) or not _is_aware(ended_at):
        return False
    utc_started_at = started_at.astimezone(timezone.utc)
    utc_ended_at = ended_at.astimezone(timezone.utc)
    return (
        utc_started_at.second == 0
        and utc_started_at.microsecond == 0
        and utc_ended_at.second == 0
        and utc_ended_at.microsecond == 0
        and utc_ended_at - utc_started_at == timedelta(minutes=1)
    )


def _is_aware(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _datetime_sort_key(value: Any) -> tuple[int, datetime]:
    if _is_aware(value):
        return (0, value.astimezone(timezone.utc))
    return (1, datetime.max.replace(tzinfo=timezone.utc))


def _is_positive_int(value: Any) -> bool:
    return _is_nonnegative_int(value) and value > 0


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_optional_nonnegative_finite(value: Any) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
