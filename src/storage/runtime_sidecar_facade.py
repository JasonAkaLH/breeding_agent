from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.core.enums import DependencyType, NodeCriticality, NodeStatus, RoutingMode, TaskStatus
from src.core.evidence import (
    is_number as _is_number,
    number_at_least as _number_at_least,
    number_at_most as _number_at_most,
    require_boolean_evidence as _require_boolean_evidence,
)
from src.storage.rust_contract import (
    artifact_policy,
    benchmark_policy,
    config_policy,
    decommission_policy,
    error_policy,
    load_runtime_sidecar_contract,
    migration_policy,
    mode_for_component,
    operation_policy,
    ops_policy,
    promotion_policy,
    resource_limit,
    retry_policy,
)


def validate_runtime_sidecar_handshake(handshake: Mapping[str, Any]) -> dict[str, Any]:
    contract = load_runtime_sidecar_contract()
    expected = {
        "component": contract["component"],
        "protocol_version": contract["protocol_version"],
        "schema_hash": contract["schema_hash"],
        "error_code_table_hash": contract["error_code_table_hash"],
    }
    for key, value in expected.items():
        if handshake.get(key) != value:
            _raise_protocol_incompatible()

    supported_features = {str(feature) for feature in handshake.get("supported_features", ())}
    required_features = {str(feature) for feature in contract["supported_features"]}
    if not required_features.issubset(supported_features):
        _raise_protocol_incompatible()

    return dict(handshake)


def _raise_protocol_incompatible() -> None:
    error_code = error_policy("runtime_store_protocol_incompatible")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar handshake is incompatible")


def validate_runtime_sidecar_response(operation_name: str, response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a Rust runtime sidecar response envelope before consuming state."""

    operation_policy(operation_name)
    if not isinstance(response, Mapping) or response.get("operation") != operation_name:
        _raise_response_invalid()
    error = response.get("error")
    if error is not None:
        _validate_typed_error(error)
        return dict(response)
    _validate_success_response(operation_name, response)
    return dict(response)


def validate_runtime_sidecar_task_record(task: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete canonical TaskRecord outside a response envelope."""

    _validate_task_record(task)
    return dict(task)


def _validate_typed_error(error: Any) -> None:
    if not isinstance(error, Mapping):
        _raise_response_invalid()
    code = error.get("code")
    if not isinstance(code, str) or not code:
        _raise_response_invalid()
    try:
        policy = error_policy(code)
    except KeyError:
        _raise_response_invalid()
    if not isinstance(error.get("message"), str):
        _raise_response_invalid()
    if error.get("retriable") is not policy["retriable"]:
        _raise_response_invalid()
    if str(error.get("category")) != policy["category"]:
        _raise_response_invalid()
    safe_metadata = error.get("safe_metadata", {})
    if not isinstance(safe_metadata, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in safe_metadata.items()
    ):
        _raise_response_invalid()


def _validate_success_response(operation_name: str, response: Mapping[str, Any]) -> None:
    if operation_name == "event_append":
        _validate_event_cursor(response.get("cursor"))
        return
    if operation_name == "event_replay":
        cursors = response.get("cursors")
        if not isinstance(cursors, list) or not isinstance(response.get("truncated"), bool):
            _raise_response_invalid()
        for cursor in cursors:
            _validate_event_cursor(cursor)
        return
    if operation_name in {"lease_acquire", "lease_renew"}:
        _validate_lease_response(response)
        return
    if operation_name == "lease_release":
        if not isinstance(response.get("released"), bool):
            _raise_response_invalid()
        return
    if operation_name == "task_submit":
        if not _non_empty_string(response.get("task_id")) or not isinstance(response.get("duplicate"), bool):
            _raise_response_invalid()
        task = response.get("task")
        if task is not None:
            _validate_task_record(task)
        return
    if operation_name == "task_get":
        found = response.get("found")
        task = response.get("task")
        if not isinstance(found, bool) or found != (task is not None):
            _raise_response_invalid()
        if task is not None:
            _validate_task_record(task)
        return
    if operation_name == "task_list_for_conversation":
        tasks = response.get("tasks")
        if not isinstance(tasks, list):
            _raise_response_invalid()
        for task in tasks:
            _validate_task_record(task)
        return
    if operation_name == "task_get_active_for_conversation":
        found = response.get("found")
        task = response.get("task")
        if not isinstance(found, bool) or found != (task is not None):
            _raise_response_invalid()
        if task is not None:
            _validate_task_record(task)
        return
    if operation_name in {
        "planner_replan_claim",
        "planner_replan_claim_get",
        "planner_replan_claim_mark",
    }:
        found = response.get("found")
        claim = response.get("claim")
        if not isinstance(found, bool) or found != (claim is not None):
            _raise_response_invalid()
        if operation_name != "planner_replan_claim_get" and not found:
            _raise_response_invalid()
        if claim is not None:
            _validate_planner_replan_claim_record(claim)
        return
    if operation_name == "node_state_transition":
        if not _non_empty_string(response.get("node_id")) or not _non_empty_string(response.get("status")):
            _raise_response_invalid()
        if response.get("node") is not None:
            _validate_task_node_record(response["node"])
        return
    if operation_name == "task_node_get":
        found = response.get("found")
        node = response.get("node")
        if not isinstance(found, bool) or found != (node is not None):
            _raise_response_invalid()
        if node is not None:
            _validate_task_node_record(node)
        return
    if operation_name == "task_node_list":
        nodes = response.get("nodes")
        if not isinstance(nodes, list):
            _raise_response_invalid()
        for node in nodes:
            _validate_task_node_record(node)
        return
    if operation_name == "agent_state_commit":
        _validate_agent_run_record(response.get("run"))
        items = response.get("items")
        if not isinstance(items, list) or not isinstance(response.get("duplicate"), bool):
            _raise_response_invalid()
        for item in items:
            _validate_agent_item_record(item)
        return
    if operation_name == "agent_run_get":
        found = response.get("found")
        run = response.get("run")
        if not isinstance(found, bool) or found != (run is not None):
            _raise_response_invalid()
        if run is not None:
            _validate_agent_run_record(run)
        return
    if operation_name == "agent_item_list":
        items = response.get("items")
        if not isinstance(items, list):
            _raise_response_invalid()
        for item in items:
            _validate_agent_item_record(item)
        return
    if operation_name == "agent_final_projection_get":
        found = response.get("found")
        projection = response.get("projection_json")
        if not isinstance(found, bool):
            _raise_response_invalid()
        if found:
            if not isinstance(projection, bytes) or not projection:
                _raise_response_invalid()
            try:
                value = json.loads(projection)
            except (UnicodeDecodeError, json.JSONDecodeError):
                _raise_response_invalid()
            if not isinstance(value, dict):
                _raise_response_invalid()
        elif projection is not None:
            _raise_response_invalid()
        return
    if operation_name == "task_edge_save":
        _validate_task_edge_record(response.get("edge"))
        return
    if operation_name == "task_edge_list":
        edges = response.get("edges")
        if not isinstance(edges, list):
            _raise_response_invalid()
        for edge in edges:
            _validate_task_edge_record(edge)
        return
    if operation_name == "artifact_save":
        _validate_artifact_record(response.get("artifact"))
        return
    if operation_name == "artifact_get":
        found = response.get("found")
        if not isinstance(found, bool):
            _raise_response_invalid()
        artifact = response.get("artifact")
        if found:
            _validate_artifact_record(artifact)
        elif artifact is not None:
            _raise_response_invalid()
        return
    if operation_name == "artifact_list":
        artifacts = response.get("artifacts")
        if not isinstance(artifacts, list):
            _raise_response_invalid()
        for artifact in artifacts:
            _validate_artifact_record(artifact)
        return
    if operation_name == "cancellation_token_write":
        if not isinstance(response.get("written"), bool):
            _raise_response_invalid()
        return
    if operation_name in {"bundle_revision_pin", "bundle_revision_release"}:
        if not (
            _non_empty_string(response.get("task_id"))
            and _non_empty_string(response.get("bundle_kind"))
            and _non_empty_string(response.get("revision"))
        ):
            _raise_response_invalid()
        return
    _raise_response_invalid()


def _validate_event_cursor(cursor: Any) -> None:
    if not isinstance(cursor, Mapping):
        _raise_response_invalid()
    if not (
        _non_empty_string(cursor.get("conversation_id"))
        and _non_empty_string(cursor.get("task_id"))
        and isinstance(cursor.get("sequence"), int)
        and cursor["sequence"] > 0
        and isinstance(cursor.get("created_at_ms"), int)
    ):
        _raise_response_invalid()


def _validate_agent_run_record(run: Any) -> None:
    if not isinstance(run, Mapping):
        _raise_response_invalid()
    if not all(_non_empty_string(run.get(name)) for name in ("run_id", "task_id", "conversation_id", "status", "model_edition", "reasoning_effort")):
        _raise_response_invalid()
    if not (
        isinstance(run.get("thinking_enabled"), bool)
        and isinstance(run.get("next_item_sequence"), int)
        and run["next_item_sequence"] > 0
        and isinstance(run.get("compacted_through_sequence"), int)
        and isinstance(run.get("waiting_call_item_ids"), list)
        and isinstance(run.get("revision"), int)
    ):
        _raise_response_invalid()


def _validate_agent_item_record(item: Any) -> None:
    if not isinstance(item, Mapping):
        _raise_response_invalid()
    payload = item.get("payload_json")
    if not (
        all(_non_empty_string(item.get(name)) for name in ("item_id", "run_id", "task_id", "kind", "state", "payload_sha256"))
        and isinstance(item.get("sequence"), int)
        and item["sequence"] > 0
        and isinstance(payload, bytes)
        and isinstance(item.get("payload_size_bytes"), int)
        and item["payload_size_bytes"] == len(payload)
    ):
        _raise_response_invalid()


def _validate_planner_replan_claim_record(claim: Any) -> None:
    if not isinstance(claim, Mapping):
        _raise_response_invalid()
    decision_digest = claim.get("decision_digest")
    planning_revision = claim.get("planning_revision")
    if not (
        _non_empty_string(claim.get("task_id"))
        and isinstance(decision_digest, str)
        and len(decision_digest) == 64
        and all(character in "0123456789abcdef" for character in decision_digest)
        and isinstance(planning_revision, int)
        and not isinstance(planning_revision, bool)
        and planning_revision >= 1
        and claim.get("planning_epoch") == f"r{planning_revision}"
        and claim.get("status") in {"claimed", "applied", "rejected"}
        and _non_empty_string(claim.get("created_at"))
        and _non_empty_string(claim.get("updated_at"))
    ):
        _raise_response_invalid()


def _validate_lease_response(response: Mapping[str, Any]) -> None:
    if not (
        _non_empty_string(response.get("task_id"))
        and _non_empty_string(response.get("owner_id"))
        and isinstance(response.get("revision"), int)
        and response["revision"] > 0
        and isinstance(response.get("expires_at_ms"), int)
        and _non_empty_string(response.get("renew_token"))
    ):
        _raise_response_invalid()


def _validate_task_edge_record(edge: Any) -> None:
    if not isinstance(edge, Mapping):
        _raise_response_invalid()
    if not (
        _non_empty_string(edge.get("task_id"))
        and _non_empty_string(edge.get("from_node_id"))
        and _non_empty_string(edge.get("to_node_id"))
        and _non_empty_string(edge.get("edge_type"))
        and isinstance(edge.get("condition"), str)
    ):
        _raise_response_invalid()


def _validate_task_record(task: Any) -> None:
    if not isinstance(task, Mapping):
        _raise_response_invalid()
    if not all(
        _non_empty_string(task.get(name))
        for name in ("task_id", "conversation_id", "root_message_id", "status", "routing_mode")
    ):
        _raise_response_invalid()
    if task.get("status") not in {str(value) for value in TaskStatus}:
        _raise_response_invalid()
    if task.get("routing_mode") not in {str(value) for value in RoutingMode}:
        _raise_response_invalid()
    for name in (
        "requested_capability_id",
        "root_node_id",
        "summary",
        "cancel_requested_at",
        "created_at",
        "updated_at",
    ):
        if task.get(name) is not None and not isinstance(task.get(name), str):
            _raise_response_invalid()
    assignment = task.get("assignment")
    if assignment is None:
        return
    if not isinstance(assignment, Mapping):
        _raise_response_invalid()
    if (
        assignment.get("route_mode") not in {"off", "shadow", "enforce"}
        or assignment.get("real_path") not in {"legacy", "user_scoped", "unavailable"}
        or assignment.get("shadow_path") not in {"none", "user_scoped"}
        or not _non_empty_string(assignment.get("config_version"))
        or assignment.get("reason_code")
        not in {
            "routing_off",
            "shadow_enabled",
            "enforce_selected",
            "cohort_not_selected",
            "percent_not_selected",
            "explicit_legacy_capability",
            "user_server_rollout_unavailable",
            "no_execution_path",
        }
    ):
        _raise_response_invalid()
    if (assignment["route_mode"] == "shadow") != (assignment["shadow_path"] == "user_scoped"):
        _raise_response_invalid()
    for name in ("cohort_id", "assignment_key_hash", "assigned_at"):
        if assignment.get(name) is not None and not isinstance(assignment.get(name), str):
            _raise_response_invalid()


def _validate_task_node_record(node: Any) -> None:
    if not isinstance(node, Mapping):
        _raise_response_invalid()
    if not all(
        _non_empty_string(node.get(name))
        for name in ("node_id", "task_id", "capability_id", "status", "criticality", "dependency_type")
    ):
        _raise_response_invalid()
    if (
        node.get("status") not in {str(value) for value in NodeStatus}
        or node.get("criticality") not in {str(value) for value in NodeCriticality}
        or node.get("dependency_type") not in {str(value) for value in DependencyType}
    ):
        _raise_response_invalid()
    if not isinstance(node.get("retry_policy"), Mapping) or not isinstance(node.get("timeout_policy"), Mapping):
        _raise_response_invalid()
    if not isinstance(node.get("input_refs"), list) or not all(isinstance(value, str) for value in node["input_refs"]):
        _raise_response_invalid()
    if not isinstance(node.get("output_refs"), list) or not all(isinstance(value, str) for value in node["output_refs"]):
        _raise_response_invalid()
    for name in ("assigned_instance_id", "resource_class", "started_at", "finished_at"):
        if node.get(name) is not None and not isinstance(node.get(name), str):
            _raise_response_invalid()


def _validate_artifact_record(artifact: Any) -> None:
    if not isinstance(artifact, Mapping):
        _raise_response_invalid()
    if not (
        _non_empty_string(artifact.get("artifact_id"))
        and _non_empty_string(artifact.get("task_id"))
        and _non_empty_string(artifact.get("producer_node_id"))
        and _non_empty_string(artifact.get("artifact_type"))
        and isinstance(artifact.get("storage_ref"), str)
        and isinstance(artifact.get("summary"), str)
        and isinstance(artifact.get("is_complete"), bool)
        and isinstance(artifact.get("created_at"), str)
    ):
        _raise_response_invalid()


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _raise_response_invalid() -> None:
    error_code = error_policy("runtime_store_response_invalid")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar response failed contract validation")


def build_sidecar_retry_plan(
    operation_name: str,
    *,
    error: Mapping[str, Any],
    failed_attempt: int,
    idempotency_key: str | None,
    same_sidecar: bool = True,
) -> dict[str, Any] | None:
    """Build a bounded retry plan only when the Rust contract allows it."""

    operation = operation_policy(operation_name)
    _validate_typed_error(error)
    policy = error_policy(str(error["code"]))
    retry = retry_policy()
    if (
        not policy["retriable"]
        or operation.get("idempotency_required") is not True
        or not _non_empty_string(idempotency_key)
        or retry.get("requires_idempotency_key") is not True
        or retry.get("same_sidecar_only") is not True
        or not same_sidecar
        or failed_attempt >= int(retry["max_attempts"])
    ):
        return None
    backoff_ms = min(
        int(retry["initial_backoff_ms"]) * (2 ** max(failed_attempt - 1, 0)),
        int(retry["max_backoff_ms"]),
    )
    return {
        "operation": operation_name,
        "idempotency_key": idempotency_key,
        "next_attempt": failed_attempt + 1,
        "max_attempts": int(retry["max_attempts"]),
        "backoff_ms": backoff_ms,
        "jitter_percent": int(retry["jitter_percent"]),
        "same_sidecar_only": True,
    }


def runtime_sidecar_max_in_flight(*, cpu_count: int) -> int:
    computed = cpu_count * resource_limit("max_in_flight_cpu_multiplier")
    return max(resource_limit("max_in_flight_min"), min(resource_limit("max_in_flight_cap"), computed))


def validate_runtime_sidecar_artifact_provenance(
    metadata: Mapping[str, Any],
    *,
    allowed_checksums: set[str] | frozenset[str] | tuple[str, ...],
    allowed_cargo_lock_digests: set[str] | frozenset[str] | tuple[str, ...],
) -> dict[str, str]:
    """Validate sidecar artifact provenance before a client can connect to it."""

    policy = artifact_policy()
    if not isinstance(metadata, Mapping):
        _raise_artifact_untrusted()
    required_fields = {str(field) for field in policy["required_fields"]}
    if any(not _non_empty_string(metadata.get(field)) for field in required_fields):
        _raise_artifact_untrusted()

    source = str(metadata["source"]).strip().lower()
    if source not in set(policy["allowed_sources"]):
        _raise_artifact_untrusted()
    checksum = str(metadata["checksum_sha256"])
    cargo_lock_digest = str(metadata["cargo_lock_digest"])
    if policy.get("require_checksum_allowlist") is True and checksum not in set(allowed_checksums):
        _raise_artifact_untrusted()
    if (
        policy.get("require_cargo_lock_digest_allowlist") is True
        and cargo_lock_digest not in set(allowed_cargo_lock_digests)
    ):
        _raise_artifact_untrusted()
    if str(metadata["proto_hash"]) != policy["expected_proto_hash"]:
        _raise_artifact_untrusted()
    expected_schema_hash = load_runtime_sidecar_contract()["schema_hash"]
    if policy.get("require_schema_hash_match") is True and str(metadata["schema_hash"]) != expected_schema_hash:
        _raise_artifact_untrusted()
    return {
        "source": source,
        "artifact_kind": str(metadata["artifact_kind"]),
        "checksum_sha256": checksum,
        "cargo_lock_digest": cargo_lock_digest,
        "proto_hash": str(metadata["proto_hash"]),
        "schema_hash": str(metadata["schema_hash"]),
        "provenance_attestation": "configured",
        "sbom": "configured",
    }


def validate_runtime_sidecar_benchmark_report(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate the benchmark evidence required before runtime sidecar promotion."""

    policy = benchmark_policy()
    required_baselines = [str(baseline) for baseline in policy["required_baselines"]]
    required_operations = [str(operation) for operation in policy["required_operations"]]
    required_metrics = [str(metric) for metric in policy["required_metrics"]]
    if not isinstance(report, Mapping):
        _raise_benchmark_invalid()
    for baseline in required_baselines:
        baseline_report = report.get(baseline)
        if not isinstance(baseline_report, Mapping):
            _raise_benchmark_invalid()
        for operation in required_operations:
            metrics = baseline_report.get(operation)
            if not isinstance(metrics, Mapping):
                _raise_benchmark_invalid()
            for metric in required_metrics:
                value = metrics.get(metric)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    _raise_benchmark_invalid()
    return {
        "baselines": ",".join(required_baselines),
        "operations": ",".join(required_operations),
        "metrics": ",".join(required_metrics),
    }


def validate_runtime_sidecar_promotion_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate the shadow-to-enforce promotion threshold evidence."""

    policy = promotion_policy()
    if not isinstance(report, Mapping):
        _raise_promotion_blocked()
    if report.get("scope") not in set(policy["allowed_scopes"]):
        _raise_promotion_blocked()
    if not _number_at_least(report.get("shadow_days"), policy["min_shadow_days"]):
        _raise_promotion_blocked()
    if not _number_at_least(report.get("shadow_samples"), policy["min_shadow_samples"]):
        _raise_promotion_blocked()
    if not _number_at_most(
        report.get("contract_mismatch_rate_ppm"),
        policy["max_contract_mismatch_rate_ppm"],
    ):
        _raise_promotion_blocked()
    if not _number_at_most(report.get("panic_count"), policy["max_panic_count"]):
        _raise_promotion_blocked()
    if not _number_at_most(report.get("crash_count"), policy["max_crash_count"]):
        _raise_promotion_blocked()
    python_p95_ms = report.get("python_legacy_p95_ms")
    rust_p95_ms = report.get("rust_p95_ms")
    if not (
        _is_number(python_p95_ms)
        and python_p95_ms > 0
        and _is_number(rust_p95_ms)
        and rust_p95_ms <= python_p95_ms * (policy["max_p95_latency_ratio_percent"] / 100)
    ):
        _raise_promotion_blocked()
    if policy.get("error_rate_must_not_exceed_legacy") is True and not _number_at_most(
        report.get("rust_error_rate_ppm"),
        report.get("python_legacy_error_rate_ppm"),
    ):
        _raise_promotion_blocked()
    evidence = report.get("evidence")
    if not isinstance(evidence, Mapping) or any(evidence.get(item) is not True for item in policy["required_evidence"]):
        _raise_promotion_blocked()
    return {
        "promotion": "ready",
        "scope": str(report["scope"]),
        "shadow_days": str(report["shadow_days"]),
        "shadow_samples": str(report["shadow_samples"]),
    }


def validate_runtime_sidecar_migration_plan(plan: Mapping[str, Any]) -> dict[str, str]:
    """Validate state migration / backup / restore / replay evidence."""

    policy = migration_policy()
    if not isinstance(plan, Mapping) or set(plan) != {
        "target_schema_version",
        "components",
        "task_authority_cutover",
    }:
        _raise_migration_blocked()
    if (
        policy.get("require_target_schema_version") is True
        and plan.get("target_schema_version")
        != load_runtime_sidecar_contract()["schema_hash"]
    ):
        _raise_migration_blocked()
    components = plan.get("components")
    if not isinstance(components, Mapping):
        _raise_migration_blocked()
    required_components = [str(component) for component in policy["required_components"]]
    required_evidence = [str(evidence) for evidence in policy["required_evidence"]]
    if set(components) != set(required_components):
        _raise_migration_blocked()
    for component in required_components:
        evidence = components.get(component)
        if (
            not isinstance(evidence, Mapping)
            or set(evidence) != set(required_evidence)
            or any(evidence.get(item) is not True for item in required_evidence)
        ):
            _raise_migration_blocked()
    cutover = plan.get("task_authority_cutover")
    _validate_task_authority_cutover(cutover)
    return {
        "migration": "ready",
        "target_schema_version": str(plan["target_schema_version"]),
        "components": ",".join(required_components),
    }


def validate_runtime_sidecar_migration_evidence_artifact(
    artifact: Mapping[str, Any],
    *,
    authentication_key: bytes,
) -> dict[str, str]:
    """Authenticate and validate the enforce-only Task authority cutover artifact."""

    contract = load_runtime_sidecar_contract()
    policy = migration_policy()
    expected_fields = {
        "schema",
        "component",
        "protocol_version",
        "schema_hash",
        "error_code_table_hash",
        "key_id",
        "migration_plan",
        "hmac_sha256",
    }
    if not isinstance(artifact, Mapping) or set(artifact) != expected_fields:
        _raise_migration_blocked()
    for field in ("component", "protocol_version", "schema_hash", "error_code_table_hash"):
        if artifact.get(field) != contract[field]:
            _raise_migration_blocked()
    if artifact.get("schema") != policy["task_authority_evidence_schema"]:
        _raise_migration_blocked()
    if not _non_empty_string(artifact.get("key_id")) or len(authentication_key) < 32:
        _raise_migration_blocked()
    signature = artifact.get("hmac_sha256")
    if not isinstance(signature, str) or len(signature) != 64:
        _raise_migration_blocked()
    signed_payload = {key: value for key, value in artifact.items() if key != "hmac_sha256"}
    expected_signature = hmac.new(
        authentication_key,
        _canonical_json_bytes(signed_payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        _raise_migration_blocked()
    return validate_runtime_sidecar_migration_plan(artifact["migration_plan"])


def load_runtime_sidecar_migration_evidence_artifact(
    evidence_path: Path,
    *,
    authentication_key_path: Path,
) -> dict[str, str]:
    """Load a configured evidence artifact and authenticate it before enforce cutover."""

    try:
        if (
            evidence_path.is_symlink()
            or authentication_key_path.is_symlink()
            or not evidence_path.is_file()
            or not authentication_key_path.is_file()
            or authentication_key_path.stat().st_mode & 0o077
        ):
            _raise_migration_blocked()
        artifact = json.loads(evidence_path.read_text(encoding="utf-8"))
        authentication_key = authentication_key_path.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError):
        _raise_migration_blocked()
    return validate_runtime_sidecar_migration_evidence_artifact(
        artifact,
        authentication_key=authentication_key,
    )


def _validate_task_authority_cutover(cutover: Any) -> None:
    if not isinstance(cutover, Mapping) or set(cutover) != {
        "backfill_import_complete",
        "task_inventory",
        "task_node_inventory",
        "legacy_null_assignment_resolution",
    }:
        _raise_migration_blocked()
    if cutover.get("backfill_import_complete") is not True:
        _raise_migration_blocked()
    _validate_matching_inventory(cutover.get("task_inventory"))
    _validate_matching_inventory(cutover.get("task_node_inventory"))
    null_resolution = cutover.get("legacy_null_assignment_resolution")
    if not isinstance(null_resolution, Mapping) or set(null_resolution) != {
        "resolution_complete",
        "active_count",
        "active_canonical_digest",
        "terminal_historical_count",
        "terminal_historical_canonical_digest",
        "terminal_historical_remains_unassigned",
    }:
        _raise_migration_blocked()
    if (
        null_resolution.get("resolution_complete") is not True
        or null_resolution.get("active_count") != 0
        or null_resolution.get("active_canonical_digest")
        != hashlib.sha256(b"[]").hexdigest()
        or null_resolution.get("terminal_historical_remains_unassigned") is not True
        or not _non_negative_int(null_resolution.get("terminal_historical_count"))
        or not _sha256_digest(null_resolution.get("terminal_historical_canonical_digest"))
    ):
        _raise_migration_blocked()


def _validate_matching_inventory(inventory: Any) -> None:
    if not isinstance(inventory, Mapping) or set(inventory) != {
        "legacy_count",
        "sidecar_count",
        "legacy_canonical_digest",
        "sidecar_canonical_digest",
    }:
        _raise_migration_blocked()
    if (
        not _non_negative_int(inventory.get("legacy_count"))
        or inventory.get("sidecar_count") != inventory.get("legacy_count")
        or not _sha256_digest(inventory.get("legacy_canonical_digest"))
        or inventory.get("sidecar_canonical_digest") != inventory.get("legacy_canonical_digest")
    ):
        _raise_migration_blocked()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_runtime_sidecar_ops_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate ops observability, runbook, and fault-drill evidence."""

    policy = ops_policy()
    if not isinstance(report, Mapping):
        _raise_ops_readiness_blocked()
    required_observability = [str(item) for item in policy["required_observability"]]
    required_runbooks = [str(item) for item in policy["required_runbooks"]]
    required_drills = [str(item) for item in policy["required_drills"]]
    _require_boolean_evidence(report.get("observability"), required_observability, _raise_ops_readiness_blocked)
    _require_boolean_evidence(report.get("runbooks"), required_runbooks, _raise_ops_readiness_blocked)
    _require_boolean_evidence(report.get("drills"), required_drills, _raise_ops_readiness_blocked)
    return {
        "ops": "ready",
        "observability": ",".join(required_observability),
        "runbooks": ",".join(required_runbooks),
        "drills": ",".join(required_drills),
    }


def validate_runtime_sidecar_decommission_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate legacy Python write-path decommission evidence before final cutoff."""

    policy = decommission_policy()
    if not isinstance(report, Mapping):
        _raise_decommission_blocked()
    if report.get("canonical_sidecar_stable") is not True:
        _raise_decommission_blocked()
    rollback_path = report.get("rollback_path")
    if rollback_path not in set(policy["allowed_rollback_paths"]):
        _raise_decommission_blocked()
    required_removed_legacy_paths = [str(item) for item in policy["required_removed_legacy_paths"]]
    required_facade_only_paths = [str(item) for item in policy["required_facade_only_paths"]]
    required_evidence = [str(item) for item in policy["required_evidence"]]
    _require_boolean_evidence(
        report.get("legacy_write_paths_removed"),
        required_removed_legacy_paths,
        _raise_decommission_blocked,
    )
    _require_boolean_evidence(
        report.get("facade_only_paths"),
        required_facade_only_paths,
        _raise_decommission_blocked,
    )
    _require_boolean_evidence(report.get("evidence"), required_evidence, _raise_decommission_blocked)
    return {
        "decommission": "ready",
        "rollback_path": str(rollback_path),
        "removed_legacy_paths": ",".join(required_removed_legacy_paths),
        "facade_only_paths": ",".join(required_facade_only_paths),
        "evidence": ",".join(required_evidence),
    }


def _raise_decommission_blocked() -> None:
    error_code = error_policy("runtime_store_decommission_blocked")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar legacy decommission evidence is incomplete")


def _raise_ops_readiness_blocked() -> None:
    error_code = error_policy("runtime_store_ops_readiness_blocked")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar ops readiness evidence is incomplete")


def _raise_migration_blocked() -> None:
    error_code = error_policy("runtime_store_migration_blocked")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar migration plan is incomplete")


def _raise_promotion_blocked() -> None:
    error_code = error_policy("runtime_store_promotion_blocked")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar promotion threshold is not satisfied")


def _raise_benchmark_invalid() -> None:
    error_code = error_policy("runtime_store_benchmark_invalid")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar benchmark report is incomplete")


def _raise_artifact_untrusted() -> None:
    error_code = error_policy("runtime_store_artifact_untrusted")["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar artifact provenance is not trusted")


def validate_runtime_sidecar_config_authority(
    config_name: str,
    source: str,
    *,
    component: str,
    cross_host: bool = False,
    mtls_enabled: bool = False,
) -> dict[str, str]:
    """Validate sidecar config source/identity authority without exposing secret values."""

    mode_for_component(component)
    policy = config_policy()
    normalized_source = source.strip().lower()
    if normalized_source not in set(policy["allowed_sources"]) or normalized_source in set(policy["forbidden_sources"]):
        _raise_config_untrusted("Rust runtime sidecar config source is not trusted")
    if cross_host and policy.get("cross_host_requires_mtls") is True and not mtls_enabled:
        _raise_config_untrusted("Rust runtime sidecar cross-host access requires mTLS identity")
    return {
        "config_name": config_name,
        "source": normalized_source,
        "cross_host": str(bool(cross_host)).lower(),
        "mtls": "configured" if mtls_enabled else "not_required",
    }


def _raise_config_untrusted(message: str) -> None:
    error_code = error_policy("runtime_store_config_untrusted")["code"]
    raise RuntimeError(f"{error_code}: {message}")


def validate_runtime_sidecar_endpoint(
    endpoint: str,
    *,
    component: str,
    unavailable_error_code: str,
    allowed_hosts: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> str:
    """Validate that a sidecar endpoint is internally allowlisted before connecting."""

    mode_for_component(component)
    normalized_endpoint = endpoint.strip()
    parsed = urlparse(normalized_endpoint)
    if parsed.scheme == "unix" and parsed.path.startswith("/"):
        return normalized_endpoint
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        hostname = parsed.hostname.lower()
        normalized_allowed_hosts = {host.lower() for host in allowed_hosts}
        if (
            hostname == "localhost"
            or hostname in normalized_allowed_hosts
            or _is_internal_ip_address(hostname)
        ):
            return normalized_endpoint
    error_code = error_policy(unavailable_error_code)["code"]
    raise RuntimeError(f"{error_code}: Rust runtime sidecar endpoint is not internally allowlisted")


def _is_internal_ip_address(hostname: str) -> bool:
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def ensure_sidecar_write_allowed(
    *,
    component: str,
    operation_name: str,
    unavailable_error_code: str,
) -> None:
    policy = operation_policy(operation_name)
    if policy.get("kind") != "write" or policy.get("enforce_failure") != "fail_closed":
        raise RuntimeError(f"Rust runtime sidecar {operation_name} policy is incompatible")
    if policy.get("python_legacy_write_fallback") is not False:
        raise RuntimeError(f"Rust runtime sidecar {operation_name} legacy fallback policy is incompatible")
    if mode_for_component(component) == "enforce":
        error_code = error_policy(unavailable_error_code)["code"]
        raise RuntimeError(
            f"{error_code}: Rust runtime sidecar enforce mode is active but no Rust runtime sidecar client is configured"
        )


class RuntimeLeaseFacade:
    """Rust sidecar lease facade without Python-owned lease state."""

    def __init__(self, *, runtime_sidecar_client: Any | None = None) -> None:
        self._runtime_sidecar_client = runtime_sidecar_client

    def acquire(
        self,
        *,
        task_id: str,
        owner_id: str,
        now_ms: int = 0,
        ttl_ms: int = 0,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        sidecar_client = self._sidecar_client_for("lease_acquire")
        if sidecar_client is not None:
            response = sidecar_client.acquire_lease(
                task_id=task_id,
                owner_id=owner_id,
                now_ms=now_ms,
                ttl_ms=ttl_ms,
                idempotency_key=idempotency_key or f"{task_id}:{owner_id}:lease_acquire",
            )
            self._consume_response("lease_acquire", response)
            return response
        self._ensure_lease_write_allowed("lease_acquire", task_id=task_id, owner_id=owner_id)
        return None

    def renew(
        self,
        *,
        task_id: str,
        owner_id: str,
        lease_token: str,
        now_ms: int = 0,
        ttl_ms: int = 0,
    ) -> dict[str, Any] | None:
        sidecar_client = self._sidecar_client_for("lease_renew")
        if sidecar_client is not None:
            response = sidecar_client.renew_lease(
                task_id=task_id,
                renew_token=lease_token,
                now_ms=now_ms,
                ttl_ms=ttl_ms,
            )
            self._consume_response("lease_renew", response)
            return response
        self._ensure_lease_write_allowed("lease_renew", task_id=task_id, owner_id=owner_id, lease_token=lease_token)
        return None

    def release(self, *, task_id: str, owner_id: str, lease_token: str) -> dict[str, Any] | None:
        sidecar_client = self._sidecar_client_for("lease_release")
        if sidecar_client is not None:
            response = sidecar_client.release_lease(task_id=task_id, renew_token=lease_token)
            self._consume_response("lease_release", response)
            return response
        self._ensure_lease_write_allowed("lease_release", task_id=task_id, owner_id=owner_id, lease_token=lease_token)
        return None

    def _sidecar_client_for(self, operation_name: str) -> Any | None:
        if mode_for_component("runtime_store") != "enforce":
            return None
        if self._runtime_sidecar_client is None:
            self._ensure_lease_write_allowed(operation_name)
            return None
        return self._runtime_sidecar_client

    @staticmethod
    def _ensure_lease_write_allowed(operation_name: str, **_: str) -> None:
        ensure_sidecar_write_allowed(
            component="runtime_store",
            operation_name=operation_name,
            unavailable_error_code="runtime_store_unavailable",
        )

    @staticmethod
    def _consume_response(operation_name: str, response: Mapping[str, Any]) -> None:
        envelope = validate_runtime_sidecar_response(operation_name, response)
        error = envelope.get("error")
        if isinstance(error, Mapping):
            raise RuntimeError(f"{error['code']}: {error['message']}")


__all__ = [
    "RuntimeLeaseFacade",
    "build_sidecar_retry_plan",
    "ensure_sidecar_write_allowed",
    "load_runtime_sidecar_migration_evidence_artifact",
    "runtime_sidecar_max_in_flight",
    "validate_runtime_sidecar_artifact_provenance",
    "validate_runtime_sidecar_benchmark_report",
    "validate_runtime_sidecar_config_authority",
    "validate_runtime_sidecar_decommission_readiness",
    "validate_runtime_sidecar_endpoint",
    "validate_runtime_sidecar_handshake",
    "validate_runtime_sidecar_migration_plan",
    "validate_runtime_sidecar_migration_evidence_artifact",
    "validate_runtime_sidecar_ops_readiness",
    "validate_runtime_sidecar_promotion_readiness",
    "validate_runtime_sidecar_response",
    "validate_runtime_sidecar_task_record",
]
