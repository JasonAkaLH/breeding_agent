from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta
import re
from typing import Any, Iterable

from src.core.enums import UserMCPTransport
from src.core.models import MCPRolloutMetricBucket as MCPRolloutMetricBucketRecord
from src.core.models import MCPShadowAuditSample

from .rollout_evidence import (
    MCPEvidenceKind,
    MCPRolloutEvidencePayload,
    MCPCallKind,
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
    MCPSafetyRedLine,
    MCPShadowScenario,
    MCPShadowScenarioObservation,
    canonical_evidence_content_digest,
)
from .shadow_compare import (
    SHADOW_SCENARIO_EXPECTATIONS,
    ShadowComparison,
    ShadowOutcome,
)


MCP_SHADOW_SAMPLE_RETENTION = timedelta(days=30)
APPROVED_VERIFIED_RETIRE_BLOCKER = "approved_verified_retire"
MCP_SHADOW_SAMPLE_SCENARIOS = frozenset(item.value for item in MCPShadowScenario)
MCP_SHADOW_SAMPLE_OUTCOMES = frozenset(item.value for item in ShadowOutcome)
MCP_SHADOW_SAMPLE_TRANSPORTS = frozenset(item.value for item in UserMCPTransport)
MCP_SHADOW_SAMPLE_ENDPOINT_POLICIES = frozenset(
    {"allowed", "allowed_by_enterprise_allowlist", "runtime_enforced"}
)
MCP_SHADOW_SAMPLE_COMPARISONS = frozenset(item.value for item in ShadowComparison)
MCP_SHADOW_SAMPLE_BLOCKERS = frozenset(
    {
        APPROVED_VERIFIED_RETIRE_BLOCKER,
        "audit_incomplete",
        "catalog_count_mismatch",
        "catalog_names_hmac_mismatch",
        "cleanup_incomplete",
        "config_fingerprint_mismatch",
        "digest_invalid",
        "endpoint_policy_allowed_mismatch",
        "endpoint_policy_mismatch",
        "fixture_fingerprint_mismatch",
        "grant_check_mismatch",
        "legacy_cleanup_incomplete",
        "legacy_outcome_mismatch",
        "legacy_route_mapping_mismatch",
        "manifest_fingerprint_mismatch",
        "mapping_config_fingerprint_mismatch",
        "mapping_set_fingerprint_mismatch",
        "ownership_verified_mismatch",
        "sample_nonce_missing",
        "sample_not_terminal",
        "sample_outside_window",
        "schema_fingerprints_mismatch",
        "schema_valid_mismatch",
        "selected_tool_hmac_mismatch",
        "shadow_outcome_mismatch",
        "shadow_outcome_not_ready",
        "shadow_route_mapping_mismatch",
        "timeout_checkpoint_mismatch",
        "transport_mismatch",
        "verified_mapping_ambiguous",
        "verified_mapping_input_invalid",
        "verified_mapping_invalid",
        "verified_mapping_missing",
        "verified_mapping_not_in_approved_set",
    }
)
MCP_SHADOW_SAMPLE_EXPECTATIONS = {
    scenario.value: (
        legacy_outcome.value,
        shadow_outcome.value,
        transport,
        endpoint_policy,
    )
    for scenario, (
        legacy_outcome,
        shadow_outcome,
        transport,
        endpoint_policy,
    ) in SHADOW_SCENARIO_EXPECTATIONS.items()
}
MCP_SHADOW_SAMPLE_CLOSED_VALUES = {
    "scenarios": MCP_SHADOW_SAMPLE_SCENARIOS,
    "outcomes": MCP_SHADOW_SAMPLE_OUTCOMES,
    "transports": MCP_SHADOW_SAMPLE_TRANSPORTS,
    "endpoint_policies": MCP_SHADOW_SAMPLE_ENDPOINT_POLICIES,
    "comparisons": MCP_SHADOW_SAMPLE_COMPARISONS,
    "blockers": MCP_SHADOW_SAMPLE_BLOCKERS,
}
_SAFE_REF_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_EXCLUDED_BLOCKERS = frozenset({"sample_not_terminal", "sample_outside_window"})


def canonical_shadow_sample_digest(sample: MCPShadowAuditSample) -> str:
    return canonical_evidence_content_digest(
        {
            field.name: getattr(sample, field.name)
            for field in fields(sample)
            if field.name != "payload_digest"
        }
    )


def seal_shadow_audit_sample(sample: MCPShadowAuditSample) -> MCPShadowAuditSample:
    sealed = replace(sample, payload_digest="")
    return replace(sealed, payload_digest=canonical_shadow_sample_digest(sealed))


def validate_shadow_audit_sample(sample: MCPShadowAuditSample) -> tuple[str, ...]:
    blockers: list[str] = []
    required = (
        sample.sample_id,
        sample.environment_id,
        sample.deployment_id,
        sample.config_fingerprint,
        sample.manifest_fingerprint,
        sample.fixture_fingerprint,
        sample.mapping_fingerprint,
        sample.scenario,
        sample.nonce,
        sample.legacy_outcome,
        sample.shadow_outcome,
        sample.transport,
        sample.endpoint_policy,
    )
    if any(not isinstance(value, str) or not value.strip() for value in required):
        blockers.append("required_field_invalid")
    if sample.rollout_program != "user_mcp_phase3" or sample.stage != "internal_shadow":
        blockers.append("scope_invalid")
    if sample.scenario not in MCP_SHADOW_SAMPLE_SCENARIOS:
        blockers.append("scenario_invalid")
    if (
        sample.legacy_outcome not in MCP_SHADOW_SAMPLE_OUTCOMES
        or sample.shadow_outcome not in MCP_SHADOW_SAMPLE_OUTCOMES
    ):
        blockers.append("outcome_invalid")
    if sample.transport not in MCP_SHADOW_SAMPLE_TRANSPORTS:
        blockers.append("transport_invalid")
    if sample.endpoint_policy not in MCP_SHADOW_SAMPLE_ENDPOINT_POLICIES:
        blockers.append("endpoint_policy_invalid")
    if sample.comparison not in MCP_SHADOW_SAMPLE_COMPARISONS:
        blockers.append("comparison_invalid")
    expected = MCP_SHADOW_SAMPLE_EXPECTATIONS.get(sample.scenario)
    if (
        sample.comparison == ShadowComparison.MATCHED.value
        and expected is not None
        and (
            sample.legacy_outcome,
            sample.shadow_outcome,
            sample.transport,
            sample.endpoint_policy,
        )
        != expected
    ):
        blockers.append("matched_expectation_invalid")
    if any(
        value is not None
        and (not isinstance(value, str) or _SAFE_REF_RE.fullmatch(value) is None)
        for value in (
            sample.safe_owner_ref,
            sample.safe_task_ref,
            sample.safe_call_ref,
        )
    ):
        blockers.append("safe_ref_invalid")
    sample_blockers = sample.blockers
    if (
        not isinstance(sample_blockers, tuple)
        or len(sample_blockers) != len(set(sample_blockers))
        or any(value not in MCP_SHADOW_SAMPLE_BLOCKERS for value in sample_blockers)
    ):
        blockers.append("blockers_invalid")
    elif not _comparison_blockers_are_valid(sample.comparison, sample_blockers):
        blockers.append("blockers_invalid")
    if not all(_aware(value) for value in (sample.observed_at, sample.recorded_at, sample.expires_at)):
        blockers.append("timestamp_invalid")
    elif (
        sample.recorded_at < sample.observed_at
        or sample.expires_at != sample.recorded_at + MCP_SHADOW_SAMPLE_RETENTION
    ):
        blockers.append("retention_invalid")
    try:
        digest = canonical_shadow_sample_digest(sample)
    except (TypeError, ValueError):
        blockers.append("digest_invalid")
    else:
        if sample.payload_digest != digest:
            blockers.append("digest_invalid")
    return tuple(dict.fromkeys(blockers))


def _comparison_blockers_are_valid(
    comparison: str,
    blockers: tuple[str, ...],
) -> bool:
    if comparison == ShadowComparison.MATCHED.value:
        return not blockers
    if comparison == ShadowComparison.MISMATCHED.value:
        return bool(blockers) and not any(
            blocker in _EXCLUDED_BLOCKERS
            or blocker == APPROVED_VERIFIED_RETIRE_BLOCKER
            for blocker in blockers
        ) and blockers != ("verified_mapping_missing",)
    if comparison == ShadowComparison.NOT_COMPARABLE.value:
        return blockers in (
            (APPROVED_VERIFIED_RETIRE_BLOCKER,),
            ("verified_mapping_missing",),
        )
    if comparison == ShadowComparison.EXCLUDED.value:
        return bool(blockers) and set(blockers) <= _EXCLUDED_BLOCKERS
    return False


def build_internal_shadow_evidence_payload(
    samples: Iterable[MCPShadowAuditSample],
    *,
    environment_id: str,
    deployment_id: str,
    config_fingerprint: str,
    manifest_fingerprint: str,
    fixture_fingerprint: str,
    mapping_fingerprint: str,
    window_started_at: datetime,
    window_ended_at: datetime,
    metric_buckets: tuple[Any, ...] = (),
    continuous_window: bool = True,
    missing_bucket_count: int = 0,
) -> MCPRolloutEvidencePayload:
    counts = {
        scenario: {"matched": 0, "mismatched": 0, "invalid": 0, "not_comparable": 0, "excluded": 0}
        for scenario in MCPShadowScenario
    }
    invalid_count = 0
    unresolved_mismatch_count = 0
    unapproved_not_comparable_count = 0
    observation_count = 0
    excluded_count = 0
    seen_nonces: set[str] = set()
    seen_ids: dict[str, str] = {}

    expected_scope = (
        environment_id,
        deployment_id,
        "internal_shadow",
        config_fingerprint,
        manifest_fingerprint,
        fixture_fingerprint,
        mapping_fingerprint,
    )
    for sample in samples:
        scenario = _scenario(sample.scenario)
        scope = (
            sample.environment_id,
            sample.deployment_id,
            sample.stage,
            sample.config_fingerprint,
            sample.manifest_fingerprint,
            sample.fixture_fingerprint,
            sample.mapping_fingerprint,
        )
        invalid = bool(validate_shadow_audit_sample(sample)) or scope != expected_scope
        invalid = invalid or not (window_started_at <= sample.observed_at < window_ended_at)
        previous_digest = seen_ids.get(sample.sample_id)
        invalid = invalid or sample.nonce in seen_nonces or (
            previous_digest is not None and previous_digest != sample.payload_digest
        )
        seen_nonces.add(sample.nonce)
        seen_ids[sample.sample_id] = sample.payload_digest
        if invalid or scenario is None:
            invalid_count += 1
            if scenario is not None:
                counts[scenario]["invalid"] += 1
            continue
        observation_count += 1
        if sample.comparison == "matched":
            counts[scenario]["matched"] += 1
        elif sample.comparison == "mismatched":
            counts[scenario]["mismatched"] += 1
            unresolved_mismatch_count += 1
        elif sample.comparison == "excluded":
            counts[scenario]["excluded"] += 1
            excluded_count += 1
        else:
            counts[scenario]["not_comparable"] += 1
            if tuple(sample.blockers) != (APPROVED_VERIFIED_RETIRE_BLOCKER,):
                unapproved_not_comparable_count += 1

    scenarios = tuple(
        MCPShadowScenarioObservation(
            scenario=scenario,
            matched_count=counts[scenario]["matched"],
            mismatched_count=counts[scenario]["mismatched"],
            invalid_count=counts[scenario]["invalid"],
            not_comparable_count=counts[scenario]["not_comparable"],
            excluded_count=counts[scenario]["excluded"],
        )
        for scenario in MCPShadowScenario
    )
    return MCPRolloutEvidencePayload(
        kind=MCPEvidenceKind.INTERNAL_SHADOW,
        metric_buckets=metric_buckets,
        shadow_scenarios=scenarios,
        continuous_window=continuous_window,
        missing_bucket_count=missing_bucket_count,
        invalid_evidence_count=invalid_count,
        unresolved_mismatch_count=unresolved_mismatch_count,
        unapproved_not_comparable_count=unapproved_not_comparable_count,
        shadow_observation_count=observation_count,
        pre_dispatch_excluded_count=excluded_count,
        manifest_fingerprint=manifest_fingerprint,
        fixture_fingerprint=fixture_fingerprint,
        mapping_fingerprint=mapping_fingerprint,
    )


def metric_records_to_domain(
    records: Iterable[MCPRolloutMetricBucketRecord],
) -> tuple[MCPMetricBucket, ...]:
    buckets: list[MCPMetricBucket] = []
    for record in records:
        buckets.append(
            MCPMetricBucket(
                metric_name=MCPMetricName(record.metric_name),
                bucket_started_at=record.bucket_started_at,
                bucket_ended_at=record.bucket_ended_at,
                labels=MCPMetricLabels(
                    execution_path=MCPMetricExecutionPath(record.execution_path),
                    routing_mode=MCPMetricRoutingMode(record.routing_mode),
                    transport=MCPMetricTransport(record.transport),
                    protocol_version=MCPMetricProtocolVersion(record.protocol_version),
                    adapter=MCPMetricAdapter(record.adapter),
                    result_category=MCPMetricResultCategory(record.result_category),
                    error_category=MCPMetricErrorCategory(record.error_category),
                    call_kind=None if record.call_kind is None else MCPCallKind(record.call_kind),
                    red_line=None if record.red_line is None else MCPSafetyRedLine(record.red_line),
                ),
                latency_bucket=MCPLatencyBucket(record.latency_bucket),
                value=record.value,
            )
        )
    return tuple(buckets)


def _scenario(value: str) -> MCPShadowScenario | None:
    try:
        return MCPShadowScenario(value)
    except ValueError:
        return None


def _aware(value: datetime) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
