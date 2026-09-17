#!/usr/bin/env python
"""Validate canonical user-MCP phase-3 rollout evidence offline."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.integrations.mcp.rollout_evidence import (  # noqa: E402
    MCPCallKind,
    MCPCallKindObservation,
    MCPEvidenceKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
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
    MCPRedLineCount,
    MCPRolloutDrill,
    MCPRolloutEvidencePayload,
    MCPRolloutStage,
    MCPSafetyRedLine,
    MCPShadowScenario,
    MCPShadowScenarioObservation,
    MCPStageGateRequest,
    evaluate_mcp_stage_gate,
)


ARTIFACT_SCHEMA = "maf.user_mcp_phase3_evidence.v1"
ATTESTATION_KEYRING_SCHEMA = "maf.user_mcp_phase3_attestation_keyring.v1"
ATTESTATION_KEYRING_ENV = "MAF_MCP_ROLLOUT_ATTESTATION_KEYRING_PATH"


class Phase3EvidenceError(ValueError):
    """Fail-closed canonical evidence input error."""


_EnumT = TypeVar("_EnumT", bound=Enum)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def load_artifact(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
    )
    if not isinstance(value, dict):
        raise Phase3EvidenceError("evidence artifact must be a JSON object")
    return value


def load_attestation_keyring(path: Path) -> dict[str, bytes]:
    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
    )
    raw = _object(value, "attestation keyring")
    _exact_fields(raw, {"schema", "keys"}, "attestation keyring")
    if raw["schema"] != ATTESTATION_KEYRING_SCHEMA:
        raise Phase3EvidenceError("attestation keyring schema is unsupported")
    keys = _object(raw["keys"], "attestation keys")
    if not keys:
        raise Phase3EvidenceError("attestation keyring must contain at least one key")
    decoded: dict[str, bytes] = {}
    for key_id, encoded_key in keys.items():
        if not isinstance(key_id, str) or not _IDENTIFIER_RE.fullmatch(key_id):
            raise Phase3EvidenceError("attestation key id is invalid")
        encoded = _string(encoded_key, f"attestation key {key_id}")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Phase3EvidenceError(
                f"attestation key {key_id} must be canonical base64"
            ) from exc
        if not key or base64.b64encode(key).decode("ascii") != encoded:
            raise Phase3EvidenceError(
                f"attestation key {key_id} must be canonical base64"
            )
        decoded[key_id] = key
    return decoded


def parse_evidence_snapshot(value: Mapping[str, Any]) -> MCPEvidenceSnapshot:
    """Recreate an asserted snapshot without access to a production signing key."""

    raw = _object(value, "evidence snapshot")
    _exact_fields(
        raw,
        {
            "evidence_id",
            "environment_id",
            "git_sha",
            "deployment_id",
            "stage",
            "config_fingerprint",
            "window_started_at",
            "window_ended_at",
            "recorded_at",
            "producer",
            "source",
            "snapshot_id",
            "nonce",
            "payload",
            "payload_digest",
            "attestation_key_id",
            "attestation_signature",
        },
        "evidence snapshot",
    )
    return MCPEvidenceSnapshot(
        evidence_id=_string(raw["evidence_id"], "evidence_id"),
        environment_id=_string(raw["environment_id"], "environment_id"),
        git_sha=_string(raw["git_sha"], "git_sha"),
        deployment_id=_string(raw["deployment_id"], "deployment_id"),
        stage=_enum(MCPRolloutStage, raw["stage"], "stage"),
        config_fingerprint=_string(raw["config_fingerprint"], "config_fingerprint"),
        window_started_at=_datetime(raw["window_started_at"], "window_started_at"),
        window_ended_at=_datetime(raw["window_ended_at"], "window_ended_at"),
        recorded_at=_datetime(raw["recorded_at"], "recorded_at"),
        producer=_enum(MCPEvidenceProducer, raw["producer"], "producer"),
        source=_enum(MCPEvidenceSource, raw["source"], "source"),
        snapshot_id=_integer(raw["snapshot_id"], "snapshot_id"),
        nonce=_string(raw["nonce"], "nonce"),
        payload=_payload(_object(raw["payload"], "payload")),
        payload_digest=_string(raw["payload_digest"], "payload_digest"),
        attestation_key_id=_optional_string(
            raw["attestation_key_id"], "attestation_key_id"
        ),
        attestation_signature=_optional_string(
            raw["attestation_signature"], "attestation_signature"
        ),
    )


def evaluate_artifact(
    artifact: Mapping[str, Any],
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> tuple[MCPStageGateRequest, tuple[MCPEvidenceSnapshot, ...], Any]:
    raw = _object(artifact, "evidence artifact")
    _exact_fields(raw, {"schema", "request", "records"}, "evidence artifact")
    if raw["schema"] != ARTIFACT_SCHEMA:
        raise Phase3EvidenceError("evidence artifact schema is unsupported")
    request_raw = _object(raw["request"], "request")
    _exact_fields(
        request_raw,
        {
            "evidence_id",
            "environment_id",
            "evidence_deployment_id",
            "evidence_config_fingerprint",
            "current_stage",
            "target_stage",
        },
        "request",
    )
    records_raw = raw["records"]
    if not isinstance(records_raw, list) or not records_raw:
        raise Phase3EvidenceError("records must be a nonempty array")
    records = tuple(
        parse_evidence_snapshot(_object(item, "record")) for item in records_raw
    )
    request = MCPStageGateRequest(
        evidence_id=_string(request_raw["evidence_id"], "request.evidence_id"),
        environment_id=_string(request_raw["environment_id"], "request.environment_id"),
        evidence_deployment_id=_string(
            request_raw["evidence_deployment_id"], "request.evidence_deployment_id"
        ),
        evidence_config_fingerprint=_string(
            request_raw["evidence_config_fingerprint"],
            "request.evidence_config_fingerprint",
        ),
        current_stage=_enum(
            MCPRolloutStage, request_raw["current_stage"], "request.current_stage"
        ),
        target_stage=_enum(
            MCPRolloutStage, request_raw["target_stage"], "request.target_stage"
        ),
    )
    return (
        request,
        records,
        evaluate_mcp_stage_gate(
            request,
            records,
            trusted_attestation_keys=trusted_attestation_keys,
        ),
    )


def validate_artifact(
    artifact: Mapping[str, Any],
    *,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    _, _, evaluation = evaluate_artifact(
        artifact,
        trusted_attestation_keys=trusted_attestation_keys,
    )
    return {
        "status": evaluation.status.value,
        "allowed": evaluation.allowed,
        "blockers": [item.value for item in evaluation.blockers],
        "evidence_id": evaluation.evidence_id,
        "observed_stage": (
            evaluation.observed_stage.value
            if evaluation.observed_stage is not None
            else None
        ),
        "target_stage": evaluation.target_stage.value,
    }


def run(
    argv: list[str] | None = None,
    *,
    stdout: Any = sys.stdout,
    stderr: Any = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(
        description="Validate canonical user-MCP phase-3 rollout evidence offline."
    )
    parser.add_argument("artifact", help="Canonical evidence JSON artifact path.")
    parser.add_argument(
        "--attestation-keyring",
        default=os.environ.get(ATTESTATION_KEYRING_ENV),
        help=(
            "Trusted production-attestation keyring path; defaults to "
            f"${ATTESTATION_KEYRING_ENV}."
        ),
    )
    args = parser.parse_args(argv)
    try:
        trusted_keys = (
            None
            if args.attestation_keyring is None
            else load_attestation_keyring(Path(args.attestation_keyring))
        )
        result = validate_artifact(
            load_artifact(Path(args.artifact)),
            trusted_attestation_keys=trusted_keys,
        )
        print(json.dumps(result, sort_keys=True), file=stdout)
        return 0 if result["allowed"] else 2
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        Phase3EvidenceError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc) or type(exc).__name__},
                sort_keys=True,
            ),
            file=stderr,
        )
        return 2


def _payload(raw: Mapping[str, Any]) -> MCPRolloutEvidencePayload:
    allowed = {
        "kind",
        "metric_buckets",
        "call_kinds",
        "shadow_scenarios",
        "completed_drills",
        "red_line_counts",
        "continuous_window",
        "missing_bucket_count",
        "invalid_evidence_count",
        "unresolved_mismatch_count",
        "unapproved_not_comparable_count",
        "shadow_observation_count",
        "pre_dispatch_excluded_count",
        "ci_conformance_passed",
        "manifest_fingerprint",
        "fixture_fingerprint",
        "mapping_fingerprint",
    }
    if set(raw) - allowed or "kind" not in raw:
        raise Phase3EvidenceError("payload fields are invalid")
    return MCPRolloutEvidencePayload(
        kind=_enum(MCPEvidenceKind, raw["kind"], "payload.kind"),
        metric_buckets=tuple(
            _metric_bucket(_object(item, "metric bucket"))
            for item in _array(raw, "metric_buckets")
        ),
        call_kinds=tuple(
            _call_kind(_object(item, "call kind")) for item in _array(raw, "call_kinds")
        ),
        shadow_scenarios=tuple(
            _shadow_scenario(_object(item, "shadow scenario"))
            for item in _array(raw, "shadow_scenarios")
        ),
        completed_drills=frozenset(
            _enum(MCPRolloutDrill, item, "completed_drills")
            for item in _array(raw, "completed_drills")
        ),
        red_line_counts=tuple(
            _red_line(_object(item, "red line"))
            for item in _array(raw, "red_line_counts")
        ),
        continuous_window=_boolean(
            raw.get("continuous_window", False), "continuous_window"
        ),
        missing_bucket_count=_integer(
            raw.get("missing_bucket_count", 0), "missing_bucket_count"
        ),
        invalid_evidence_count=_integer(
            raw.get("invalid_evidence_count", 0), "invalid_evidence_count"
        ),
        unresolved_mismatch_count=_integer(
            raw.get("unresolved_mismatch_count", 0), "unresolved_mismatch_count"
        ),
        unapproved_not_comparable_count=_integer(
            raw.get("unapproved_not_comparable_count", 0),
            "unapproved_not_comparable_count",
        ),
        shadow_observation_count=_integer(
            raw.get("shadow_observation_count", 0), "shadow_observation_count"
        ),
        pre_dispatch_excluded_count=_integer(
            raw.get("pre_dispatch_excluded_count", 0), "pre_dispatch_excluded_count"
        ),
        ci_conformance_passed=_boolean(
            raw.get("ci_conformance_passed", False), "ci_conformance_passed"
        ),
        manifest_fingerprint=_optional_string(
            raw.get("manifest_fingerprint"), "manifest_fingerprint"
        ),
        fixture_fingerprint=_optional_string(
            raw.get("fixture_fingerprint"), "fixture_fingerprint"
        ),
        mapping_fingerprint=_optional_string(
            raw.get("mapping_fingerprint"), "mapping_fingerprint"
        ),
    )


def _metric_bucket(raw: Mapping[str, Any]) -> MCPMetricBucket:
    required = {
        "metric_name",
        "bucket_started_at",
        "bucket_ended_at",
        "labels",
        "value",
    }
    allowed = required | {"latency_bucket"}
    if set(raw) - allowed or not required.issubset(raw):
        raise Phase3EvidenceError("metric bucket fields are invalid")
    labels = _object(raw["labels"], "metric labels")
    label_fields = {
        "execution_path",
        "routing_mode",
        "transport",
        "protocol_version",
        "adapter",
        "result_category",
        "error_category",
        "call_kind",
        "red_line",
    }
    if set(labels) - label_fields:
        raise Phase3EvidenceError("metric label fields are invalid")
    call_kind = labels.get("call_kind")
    red_line = labels.get("red_line")
    return MCPMetricBucket(
        metric_name=_enum(MCPMetricName, raw["metric_name"], "metric_name"),
        bucket_started_at=_datetime(raw["bucket_started_at"], "bucket_started_at"),
        bucket_ended_at=_datetime(raw["bucket_ended_at"], "bucket_ended_at"),
        labels=MCPMetricLabels(
            execution_path=_enum(
                MCPMetricExecutionPath,
                labels.get("execution_path", "not_applicable"),
                "execution_path",
            ),
            routing_mode=_enum(
                MCPMetricRoutingMode,
                labels.get("routing_mode", "not_applicable"),
                "routing_mode",
            ),
            transport=_enum(
                MCPMetricTransport,
                labels.get("transport", "not_applicable"),
                "transport",
            ),
            protocol_version=_enum(
                MCPMetricProtocolVersion,
                labels.get("protocol_version", "not_applicable"),
                "protocol_version",
            ),
            adapter=_enum(
                MCPMetricAdapter, labels.get("adapter", "not_applicable"), "adapter"
            ),
            result_category=_enum(
                MCPMetricResultCategory,
                labels.get("result_category", "not_applicable"),
                "result_category",
            ),
            error_category=_enum(
                MCPMetricErrorCategory,
                labels.get("error_category", "not_applicable"),
                "error_category",
            ),
            call_kind=None
            if call_kind is None
            else _enum(MCPCallKind, call_kind, "call_kind"),
            red_line=None
            if red_line is None
            else _enum(MCPSafetyRedLine, red_line, "red_line"),
        ),
        value=_integer(raw["value"], "value"),
        latency_bucket=_enum(
            MCPLatencyBucket,
            raw.get("latency_bucket", "not_applicable"),
            "latency_bucket",
        ),
    )


def _call_kind(raw: Mapping[str, Any]) -> MCPCallKindObservation:
    required = {
        "call_kind",
        "terminal_success_count",
        "terminal_error_count",
        "cancellation_count",
        "p95_latency_ms",
        "baseline_success_count",
        "baseline_error_count",
        "baseline_p95_latency_ms",
    }
    _exact_fields(raw, required, "call kind")
    return MCPCallKindObservation(
        call_kind=_enum(MCPCallKind, raw["call_kind"], "call_kind"),
        terminal_success_count=_integer(
            raw["terminal_success_count"], "terminal_success_count"
        ),
        terminal_error_count=_integer(
            raw["terminal_error_count"], "terminal_error_count"
        ),
        cancellation_count=_integer(raw["cancellation_count"], "cancellation_count"),
        p95_latency_ms=_optional_number(raw["p95_latency_ms"], "p95_latency_ms"),
        baseline_success_count=_integer(
            raw["baseline_success_count"], "baseline_success_count"
        ),
        baseline_error_count=_integer(
            raw["baseline_error_count"], "baseline_error_count"
        ),
        baseline_p95_latency_ms=_optional_number(
            raw["baseline_p95_latency_ms"], "baseline_p95_latency_ms"
        ),
    )


def _shadow_scenario(raw: Mapping[str, Any]) -> MCPShadowScenarioObservation:
    allowed = {
        "scenario",
        "matched_count",
        "mismatched_count",
        "invalid_count",
        "not_comparable_count",
        "excluded_count",
    }
    if set(raw) - allowed or not {"scenario", "matched_count"}.issubset(raw):
        raise Phase3EvidenceError("shadow scenario fields are invalid")
    return MCPShadowScenarioObservation(
        scenario=_enum(MCPShadowScenario, raw["scenario"], "scenario"),
        matched_count=_integer(raw["matched_count"], "matched_count"),
        mismatched_count=_integer(raw.get("mismatched_count", 0), "mismatched_count"),
        invalid_count=_integer(raw.get("invalid_count", 0), "invalid_count"),
        not_comparable_count=_integer(
            raw.get("not_comparable_count", 0), "not_comparable_count"
        ),
        excluded_count=_integer(raw.get("excluded_count", 0), "excluded_count"),
    )


def _red_line(raw: Mapping[str, Any]) -> MCPRedLineCount:
    _exact_fields(raw, {"red_line", "count"}, "red line")
    return MCPRedLineCount(
        red_line=_enum(MCPSafetyRedLine, raw["red_line"], "red_line"),
        count=_integer(raw["count"], "count"),
    )


def _array(raw: Mapping[str, Any], key: str) -> list[Any]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise Phase3EvidenceError(f"{key} must be an array")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Phase3EvidenceError(f"{name} must be an object")
    return dict(value)


def _exact_fields(raw: Mapping[str, Any], fields: set[str], name: str) -> None:
    if set(raw) != fields:
        raise Phase3EvidenceError(f"{name} fields are invalid")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise Phase3EvidenceError(f"{name} must be a nonempty string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Phase3EvidenceError(f"{name} must be an integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise Phase3EvidenceError(f"{name} must be a boolean")
    return value


def _optional_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Phase3EvidenceError(f"{name} must be a number or null")
    return float(value)


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise Phase3EvidenceError(f"{name} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Phase3EvidenceError(f"{name} must be an RFC3339 string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Phase3EvidenceError(f"{name} must include a timezone")
    return parsed


def _enum(enum_type: type[_EnumT], value: Any, name: str) -> _EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise Phase3EvidenceError(f"{name} is invalid") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Phase3EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
