from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn, cast

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from src.auth.invalidation_bus import AuthGenerationChanged, AuthGenerationReason
from src.auth.postgres_invalidation_bus import auth_generation_notify_sql
from src.core.contracts import StoragePort
from src.core.enums import (
    ArtifactType,
    ConversationStatus,
    EdgeType,
    EventVisibility,
    MessageRole,
    DependencyType,
    NodeCriticality,
    NodeStatus,
    RoutingMode,
    TaskStatus,
    UserMCPAuthType,
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import (
    Artifact,
    AuthUserToken,
    Checkpoint,
    Conversation,
    ConversationMemorySummary,
    ConversationFileIndexRepairMarker,
    ConversationFileResource,
    EventRecord,
    FileUploadMessageProjection,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    MCPAuditEvent,
    MCPBranchRecord,
    MCPCallRecord,
    MCPConnectionLease,
    MCPLegacyMigrationBatchResult,
    MCPLegacyMigrationRecord,
    MCPRemoteTaskBinding,
    MCPRemoteTaskOutbox,
    MCPRolloutBlockResolution,
    MCPRolloutDeploymentActivation,
    MCPRolloutDrillObservation,
    MCPRolloutEvidenceSnapshot,
    MCPRolloutGateScope,
    MCPRolloutInstanceConfigLease,
    MCPRolloutMetricBucket,
    MCPRolloutPromotionBlock,
    MCPRolloutStageApproval,
    MCPShadowAuditSample,
    MCPSealedState,
    Message,
    PendingSkillContext,
    SlotCollection,
    SlotEvent,
    Task,
    TaskEdge,
    TaskInputAttachment,
    TaskNode,
    UserMCPCredentialRecord,
    UserMCPHealthAttempt,
    UserMCPScopeLease,
    UserMCPServer,
    UserMCPToolGrant,
    MCPCredentialKeyValidation,
    validate_mcp_rollout_drill_observation,
)
from src.storage.conversation_files import (
    FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
    FILE_UPLOAD_MESSAGE_TYPE,
    FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
    file_upload_message_audit_payload,
    file_upload_message_id,
    render_file_upload_message,
    safe_file_upload_message_metadata,
)
from src.lifecycle.rust_contract import contract_value as lifecycle_contract_value
from src.lifecycle.rust_contract import status_list as lifecycle_status_list
from src.integrations.mcp.rollout_evidence import is_exact_mcp_metric_bucket_window
from src.storage.rust_contract import error_policy as runtime_error_policy
from src.storage.rust_contract import mode_for_component as runtime_mode_for_component
from src.storage.rust_contract import operation_policy as runtime_operation_policy
from src.storage.rust_contract import resource_limit as runtime_resource_limit
from src.storage.runtime_sidecar_facade import ensure_sidecar_write_allowed, validate_runtime_sidecar_response
from src.storage.runtime_sidecar_shadow import (
    RuntimeSidecarShadowSink,
    normalize_runtime_sidecar_response,
    record_runtime_sidecar_shadow_write,
)

from .base import build_task_edge_id
from .models import (
    ArtifactRow,
    AuthUserTokenRow,
    CheckpointRow,
    ConversationRow,
    ConversationMemorySummaryRow,
    ConversationFileIndexRepairMarkerRow,
    ConversationFileResourceRow,
    EventRecordRow,
    InterruptAnswerRow,
    InterruptRow,
    MailboxDeliveryRow,
    MailboxMessageRow,
    MCPAuditEventRow,
    MCPBranchRecordRow,
    MCPCallRecordRow,
    MCPConnectionLeaseRow,
    MCPLegacyMigrationRecordRow,
    MCPRemoteTaskBindingRow,
    MCPRemoteTaskOutboxRow,
    MCPRolloutBlockResolutionRow,
    MCPRolloutDeploymentActivationRow,
    MCPRolloutDrillObservationRow,
    MCPRolloutEvidenceSnapshotRow,
    MCPRolloutGateScopeRow,
    MCPRolloutInstanceConfigRow,
    MCPRolloutMetricBucketRow,
    MCPRolloutPromotionBlockRow,
    MCPRolloutStageApprovalRow,
    MCPShadowAuditSampleRow,
    MCPSealedStateRow,
    MessageRow,
    PendingSkillContextRow,
    SlotCollectionRow,
    SlotEventRow,
    TaskInputAttachmentRow,
    TaskEdgeRow,
    TaskNodeRow,
    TaskRow,
    UserMCPHealthAttemptRow,
    UserMCPScopeLeaseRow,
    UserMCPServerRow,
    UserMCPToolGrantRow,
    MCPCredentialKeyValidationRow,
)


CONVERSATION_FILE_INDEX_REPAIR_KIND = "conversation_file_index"
MCP_ROLLOUT_PROGRAM = "user_mcp_phase3"
MCP_ROLLOUT_ATTESTATION_KEY_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
)
MCP_ROLLOUT_ATTESTATION_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
MCP_ROLLOUT_STAGES = frozenset(
    {
        "off",
        "internal_shadow",
        "internal_enforce",
        "cohort_enforce",
        "full_enforce",
        "legacy_assembly_off",
    }
)
MCP_ROLLOUT_METRIC_NAMES = frozenset(
    {
        "mcp_route_requests_total",
        "mcp_route_shadow_mismatch_total",
        "mcp_gateway_active_scopes",
        "mcp_gateway_connect_duration_seconds",
        "mcp_tools_list_duration_seconds",
        "mcp_tools_list_attempts_total",
        "mcp_tool_calls_active",
        "mcp_tool_calls_total",
        "mcp_tool_call_duration_seconds",
        "mcp_tool_call_unknown_total",
        "mcp_permission_decisions_total",
        "mcp_disconnect_lease_expired_total",
        "mcp_temp_spill_bytes",
        "mcp_resource_cleanup_failures_total",
        "mcp_protocol_negotiation_total",
        "mcp_server_discover_duration_seconds",
        "mcp_mrtr_rounds_total",
        "mcp_remote_tasks_active",
        "mcp_safety_red_line_total",
    }
)
MCP_ROLLOUT_LABEL_VALUES = {
    "execution_path": frozenset({"legacy", "user_scoped", "unavailable", "not_applicable"}),
    "routing_mode": frozenset({"off", "shadow", "enforce", "not_applicable"}),
    "transport": frozenset({"streamable_http", "legacy_http_sse", "not_applicable"}),
    "protocol_version": frozenset(
        {
            "2024-11-05",
            "2025-03-26",
            "2025-06-18",
            "2025-11-25",
            "2026-07-28",
            "not_applicable",
        }
    ),
    "adapter": frozenset(
        {
            "python_legacy",
            "python_2026",
            "rust_sidecar",
            "legacy_global_runtime",
            "not_applicable",
        }
    ),
    "result_category": frozenset(
        {
            "succeeded",
            "failed",
            "unknown",
            "cancelled",
            "input_required",
            "task_created",
            "permission_denied",
            "not_comparable",
            "not_applicable",
        }
    ),
    "error_category": frozenset(
        {
            "none",
            "authentication",
            "authorization",
            "endpoint_policy",
            "transport",
            "protocol",
            "server",
            "timeout",
            "unknown",
            "validation",
            "cleanup",
            "not_applicable",
        }
    ),
    "call_kind": frozenset({"ordinary", "remote_task", "not_applicable"}),
    "red_line": frozenset(
        {
            "cross_user_access",
            "secret_exposure",
            "dual_tool_call",
            "unauthorized_tool_call",
            "endpoint_policy_bypass",
            "unknown_result_replay",
            "shadow_tool_call",
            "persistent_resource_leak",
            "not_applicable",
        }
    ),
    "latency_bucket": frozenset(
        {
            "le_100_ms",
            "le_500_ms",
            "le_1_s",
            "le_5_s",
            "le_30_s",
            "le_120_s",
            "gt_120_s",
            "not_applicable",
        }
    ),
}
MCP_ROLLOUT_EVIDENCE_SOURCES = frozenset({"ci", "production"})
MCP_ROLLOUT_EVIDENCE_PRODUCERS = frozenset(
    {"ci_pipeline", "production_snapshot_producer"}
)
MCP_ROLLOUT_EVIDENCE_KINDS = frozenset(
    {
        "ci_conformance",
        "internal_shadow",
        "internal_enforce",
        "cohort_enforce",
        "full_enforce",
        "legacy_assembly_off",
        "rollback_drill",
        "resource_baseline",
        "release_tag",
    }
)
MCP_ROLLOUT_BLOCK_REASONS = frozenset(
    {
        "no_evidence",
        "invalid_transition",
        "evidence_id_replay",
        "nonce_replay",
        "snapshot_replay",
        "snapshot_non_monotonic",
        "provenance_invalid",
        "digest_invalid",
        "attestation_missing",
        "attestation_invalid",
        "evidence_scope_mismatch",
        "evidence_stage_mismatch",
        "evidence_kind_mismatch",
        "source_policy_violation",
        "payload_invalid",
        "window_too_short",
        "window_incomplete",
        "metric_series_missing",
        "metric_summary_mismatch",
        "zero_denominator",
        "sample_insufficient",
        "scenario_sample_insufficient",
        "unresolved_mismatch",
        "invalid_sample",
        "unapproved_not_comparable",
        "required_drill_missing",
        "red_line_data_missing",
        "safety_red_line",
        "safety_red_line_nonzero",
        "baseline_missing",
        "p95_latency_regressed",
        "error_rate_regressed",
        "ci_conformance_missing",
    }
)


def _rollout_value(value: object) -> str:
    return str(value)


def _validate_rollout_scope(environment_id: str, rollout_program: str, stage: str) -> None:
    if not environment_id:
        raise ValueError("MCP rollout environment ID is required")
    if rollout_program != MCP_ROLLOUT_PROGRAM:
        raise ValueError("MCP rollout program is not supported")
    if stage not in MCP_ROLLOUT_STAGES:
        raise ValueError("MCP rollout stage is not supported")


def _row_to_user_mcp_tool_grant(row: UserMCPToolGrantRow) -> UserMCPToolGrant:
    return UserMCPToolGrant(
        grant_id=row.grant_id,
        owner_user_id=row.owner_user_id,
        server_id=row.server_id,
        tool_name=row.tool_name,
        server_security_version=int(row.server_security_version),
        input_schema_sha256=row.input_schema_sha256,
        granted_at=row.granted_at,
        invalidated_at=row.invalidated_at,
        invalid_reason=row.invalid_reason,
    )


def _row_to_mcp_branch(row: MCPBranchRecordRow) -> MCPBranchRecord:
    return MCPBranchRecord(
        branch_id=row.branch_id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        node_id=row.node_id,
        status=row.status,
        initial_server_id=row.initial_server_id,
        tool_call_count=int(row.tool_call_count),
        max_tool_calls=int(row.max_tool_calls),
        active_call_ref=row.active_call_ref,
        result_ref=row.result_ref,
        safe_summary=row.safe_summary,
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
    )


def _row_to_mcp_call(row: MCPCallRecordRow) -> MCPCallRecord:
    return MCPCallRecord(
        call_ref=row.call_ref,
        branch_id=row.branch_id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        node_id=row.node_id,
        server_id=row.server_id,
        tool_name=row.tool_name,
        status=row.status,
        call_sequence=int(row.call_sequence),
        arguments_sha256=row.arguments_sha256,
        server_security_version=int(row.server_security_version),
        input_schema_sha256=row.input_schema_sha256,
        protocol_version=row.protocol_version,
        input_field_names=tuple(row.input_field_names or ()),
        may_have_dispatched=bool(row.may_have_dispatched),
        result_ref=row.result_ref,
        output_size_bytes=None if row.output_size_bytes is None else int(row.output_size_bytes),
        safe_error_code=row.safe_error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
    )


def _row_to_mcp_remote_task(row: MCPRemoteTaskBindingRow) -> MCPRemoteTaskBinding:
    return MCPRemoteTaskBinding(
        safe_remote_task_ref=row.safe_remote_task_ref,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        node_id=row.node_id,
        call_ref=row.call_ref,
        server_id=row.server_id,
        protocol_version=row.protocol_version,
        remote_task_ciphertext=row.remote_task_ciphertext,
        remote_task_nonce=row.remote_task_nonce,
        encryption_version=int(row.encryption_version),
        last_status=row.last_status,
        next_poll_at=row.next_poll_at,
        published_at=row.published_at,
        continuation_plan=dict(row.continuation_plan or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
        claim_owner=row.claim_owner,
        claim_token=row.claim_token,
        lease_expires_at=row.lease_expires_at,
        revision=0 if row.revision is None else int(row.revision),
    )


def _row_to_mcp_remote_task_outbox(
    row: MCPRemoteTaskOutboxRow,
) -> MCPRemoteTaskOutbox:
    return MCPRemoteTaskOutbox(
        outbox_id=row.outbox_id,
        kind=row.kind,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        node_id=row.node_id,
        call_ref=row.call_ref,
        safe_remote_task_ref=row.safe_remote_task_ref,
        payload=dict(row.payload or {}),
        status=row.status,
        claim_owner=row.claim_owner,
        claim_token=row.claim_token,
        lease_expires_at=row.lease_expires_at,
        revision=int(row.revision or 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
        continuation_admitted_at=row.continuation_admitted_at,
        continuation_dispatched_at=row.continuation_dispatched_at,
        continuation_status=row.continuation_status,
        continuation_claim_owner=row.continuation_claim_owner,
        continuation_claim_token=row.continuation_claim_token,
        continuation_lease_expires_at=row.continuation_lease_expires_at,
        continuation_revision=int(row.continuation_revision or 0),
        continuation_node_ids=tuple(row.continuation_node_ids or ()),
        continuation_safe_error_code=row.continuation_safe_error_code,
        completed_at=row.completed_at,
    )


def _row_to_mcp_sealed_state(row: MCPSealedStateRow) -> MCPSealedState:
    return MCPSealedState(
        sealed_state_ref=row.sealed_state_ref,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        node_id=row.node_id,
        call_ref=row.call_ref,
        state_kind=row.state_kind,
        ciphertext=row.ciphertext,
        nonce=row.nonce,
        encryption_version=int(row.encryption_version),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_mcp_connection_lease(row: MCPConnectionLeaseRow) -> MCPConnectionLease:
    return MCPConnectionLease(
        connection_id=row.connection_id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        instance_id=row.instance_id,
        lease_expires_at=row.lease_expires_at,
        disconnected_at=row.disconnected_at,
        auth_generation=None if row.auth_generation is None else int(row.auth_generation),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_mcp_audit_event(row: MCPAuditEventRow) -> MCPAuditEvent:
    return MCPAuditEvent(
        audit_event_id=row.audit_event_id,
        owner_user_id=row.owner_user_id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        expires_at=row.expires_at,
        task_id=row.task_id,
        node_id=row.node_id,
        server_id=row.server_id,
        call_ref=row.call_ref,
        safe_payload=dict(row.safe_payload or {}),
    )


def _row_to_mcp_legacy_migration_record(
    row: MCPLegacyMigrationRecordRow,
) -> MCPLegacyMigrationRecord:
    return MCPLegacyMigrationRecord(
        migration_id=row.migration_id,
        event_type=row.event_type,
        plan_fingerprint=row.plan_fingerprint,
        source_server_id=row.source_server_id,
        source_fingerprint=row.source_fingerprint,
        owner_consumer_ref=row.owner_consumer_ref,
        target_server_id=row.target_server_id,
        target_consumer_set_digest=row.target_consumer_set_digest,
        capability_obligations_fingerprint=row.capability_obligations_fingerprint,
        catalog_fingerprint=row.catalog_fingerprint,
        capability_fingerprint=row.capability_fingerprint,
        validator_provenance_fingerprint=row.validator_provenance_fingerprint,
        credential_digest=row.credential_digest,
        disposition=row.disposition,
        occurred_at=row.occurred_at,
        evidence_expires_at=row.evidence_expires_at,
    )


def _row_to_mcp_rollout_gate_scope(row: MCPRolloutGateScopeRow) -> MCPRolloutGateScope:
    return MCPRolloutGateScope(
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        created_at=row.created_at,
    )


def _row_to_mcp_rollout_drill_observation(
    row: MCPRolloutDrillObservationRow,
) -> MCPRolloutDrillObservation:
    return MCPRolloutDrillObservation(
        drill_observation_id=row.drill_observation_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        deployment_id=row.deployment_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        drill=row.drill,
        outcome=row.outcome,
        observed_at=row.observed_at,
        recorded_at=row.recorded_at,
        expires_at=row.expires_at,
        payload_digest=row.payload_digest,
    )


def _row_to_mcp_rollout_metric_bucket(
    row: MCPRolloutMetricBucketRow,
) -> MCPRolloutMetricBucket:
    return MCPRolloutMetricBucket(
        metric_bucket_id=row.metric_bucket_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        deployment_id=row.deployment_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        metric_name=row.metric_name,
        bucket_started_at=row.bucket_started_at,
        bucket_ended_at=row.bucket_ended_at,
        execution_path=row.execution_path,
        routing_mode=row.routing_mode,
        transport=row.transport,
        protocol_version=row.protocol_version,
        adapter=row.adapter,
        result_category=row.result_category,
        error_category=row.error_category,
        call_kind=None if row.call_kind == "not_applicable" else row.call_kind,
        red_line=None if row.red_line == "not_applicable" else row.red_line,
        latency_bucket=row.latency_bucket,
        value=int(row.value),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_mcp_rollout_evidence_snapshot(
    row: MCPRolloutEvidenceSnapshotRow,
) -> MCPRolloutEvidenceSnapshot:
    return MCPRolloutEvidenceSnapshot(
        evidence_id=row.evidence_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        git_sha=row.git_sha,
        deployment_id=row.deployment_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        window_started_at=row.window_started_at,
        window_ended_at=row.window_ended_at,
        recorded_at=row.recorded_at,
        producer=row.producer,
        source=row.source,
        snapshot_id=int(row.snapshot_id),
        nonce=row.nonce,
        evidence_kind=row.evidence_kind,
        payload=dict(row.payload),
        payload_digest=row.payload_digest,
        attestation_key_id=row.attestation_key_id,
        attestation_signature=row.attestation_signature,
    )


def _row_to_mcp_shadow_audit_sample(
    row: MCPShadowAuditSampleRow,
) -> MCPShadowAuditSample:
    return MCPShadowAuditSample(
        sample_id=row.sample_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        deployment_id=row.deployment_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        manifest_fingerprint=row.manifest_fingerprint,
        fixture_fingerprint=row.fixture_fingerprint,
        mapping_fingerprint=row.mapping_fingerprint,
        scenario=row.scenario,
        nonce=row.nonce,
        safe_owner_ref=row.safe_owner_ref,
        safe_task_ref=row.safe_task_ref,
        safe_call_ref=row.safe_call_ref,
        legacy_outcome=row.legacy_outcome,
        shadow_outcome=row.shadow_outcome,
        transport=row.transport,
        endpoint_policy=row.endpoint_policy,
        comparison=row.comparison,
        blockers=tuple(str(item) for item in row.blockers),
        payload_digest=row.payload_digest,
        observed_at=row.observed_at,
        recorded_at=row.recorded_at,
        expires_at=row.expires_at,
    )


def _row_to_mcp_rollout_stage_approval(
    row: MCPRolloutStageApprovalRow,
) -> MCPRolloutStageApproval:
    return MCPRolloutStageApproval(
        approval_id=row.approval_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        deployment_id=row.deployment_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        evidence_id=row.evidence_id,
        reason=row.reason,
        approver=row.approver,
        created_at=row.created_at,
    )


def _row_to_mcp_rollout_deployment_activation(
    row: MCPRolloutDeploymentActivationRow,
) -> MCPRolloutDeploymentActivation:
    return MCPRolloutDeploymentActivation(
        activation_id=row.activation_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        deployment_id=row.deployment_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        approval_id=row.approval_id,
        evidence_id=row.evidence_id,
        previous_activation_id=row.previous_activation_id,
        operator_reason=row.operator_reason,
        is_rollback=bool(row.is_rollback),
        created_at=row.created_at,
    )


def _row_to_mcp_rollout_promotion_block(
    row: MCPRolloutPromotionBlockRow,
) -> MCPRolloutPromotionBlock:
    return MCPRolloutPromotionBlock(
        block_id=row.block_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        deployment_id=row.deployment_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        evidence_id=row.evidence_id,
        reason_code=row.reason_code,
        created_at=row.created_at,
    )


def _row_to_mcp_rollout_block_resolution(
    row: MCPRolloutBlockResolutionRow,
) -> MCPRolloutBlockResolution:
    return MCPRolloutBlockResolution(
        resolution_id=row.resolution_id,
        block_id=row.block_id,
        approval_id=row.approval_id,
        evidence_id=row.evidence_id,
        reason=row.reason,
        approver=row.approver,
        created_at=row.created_at,
    )


def _row_to_mcp_rollout_instance_config(
    row: MCPRolloutInstanceConfigRow,
) -> MCPRolloutInstanceConfigLease:
    return MCPRolloutInstanceConfigLease(
        instance_config_id=row.instance_config_id,
        environment_id=row.environment_id,
        rollout_program=row.rollout_program,
        deployment_id=row.deployment_id,
        instance_id=row.instance_id,
        stage=row.stage,
        config_fingerprint=row.config_fingerprint,
        activation_id=row.activation_id,
        lease_expires_at=row.lease_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_user_mcp_server(row: UserMCPServerRow) -> UserMCPServer:
    return UserMCPServer(
        server_id=row.server_id,
        owner_user_id=row.owner_user_id,
        display_name=row.display_name,
        routing_description=row.routing_description,
        endpoint_url=row.endpoint_url,
        transport=UserMCPTransport(row.transport),
        protocol_preference=UserMCPProtocolPreference(row.protocol_preference),
        auth_type=UserMCPAuthType(row.auth_type),
        auth_metadata=dict(row.auth_metadata or {}),
        enabled=bool(row.enabled),
        health_status=UserMCPHealthStatus(row.health_status),
        config_version=int(row.config_version),
        security_version=int(row.security_version),
        credential_configured=row.credential_ciphertext is not None,
        last_tested_at=row.last_tested_at,
        last_test_error_code=row.last_test_error_code,
        deletion_pending=bool(row.deletion_pending),
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_user_mcp_credential(row: UserMCPServerRow) -> UserMCPCredentialRecord | None:
    if row.credential_ciphertext is None or row.credential_nonce is None or row.encryption_version is None:
        return None
    return UserMCPCredentialRecord(
        owner_user_id=row.owner_user_id,
        server_id=row.server_id,
        credential_ciphertext=bytes(row.credential_ciphertext),
        credential_nonce=bytes(row.credential_nonce),
        encryption_version=int(row.encryption_version),
        credential_updated_at=row.credential_updated_at,
    )


def _row_to_user_mcp_health_attempt(row: UserMCPHealthAttemptRow) -> UserMCPHealthAttempt:
    return UserMCPHealthAttempt(
        attempt_id=row.attempt_id,
        owner_user_id=row.owner_user_id,
        server_id=row.server_id,
        config_version=int(row.config_version),
        security_version=int(row.security_version),
        runner_instance_id=row.runner_instance_id,
        lease_expires_at=cast(datetime, row.lease_expires_at),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_user_mcp_scope_lease(row: UserMCPScopeLeaseRow) -> UserMCPScopeLease:
    return UserMCPScopeLease(
        scope_id=row.scope_id,
        owner_user_id=row.owner_user_id,
        server_id=row.server_id,
        security_version=int(row.security_version),
        gateway_instance_id=row.gateway_instance_id,
        lease_expires_at=cast(datetime, row.lease_expires_at),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        conversation_id=row.conversation_id,
        username=row.username,
        status=row.status,
        current_task_id=row.current_task_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        delete_runner_id=row.delete_runner_id,
        delete_requested_at=row.delete_requested_at,
        delete_started_at=row.delete_started_at,
        delete_finished_at=row.delete_finished_at,
        delete_failed_at=row.delete_failed_at,
        delete_error_code=row.delete_error_code,
        delete_error_summary=row.delete_error_summary,
        delete_phase=row.delete_phase,
    )


def _row_to_conversation_file_resource(row: ConversationFileResourceRow) -> ConversationFileResource:
    return ConversationFileResource(
        file_id=row.file_id,
        conversation_id=row.conversation_id,
        username=row.username,
        original_filename=row.original_filename,
        content_type=row.content_type,
        file_type=row.file_type,
        size_bytes=int(row.size_bytes or 0),
        sha256=row.sha256,
        storage_key=row.storage_key,
        preview=dict(row.preview or {}),
        description_status=row.description_status,
        description_summary=row.description_summary,
        description_ref=row.description_ref,
        status=row.status,
        normalized_filename=row.normalized_filename,
        normalized_content_type=row.normalized_content_type,
        requires_sheet_selection=bool(row.requires_sheet_selection),
        selected_sheet=row.selected_sheet,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _normalize_repair_upload_ids(value: object) -> tuple[str, ...]:
    if value is None or isinstance(value, str):
        values = () if value is None else (value,)
    elif isinstance(value, Iterable):
        values = tuple(value)
    else:
        values = ()
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        upload_id = str(item or "").strip()
        if not upload_id or upload_id in seen:
            continue
        normalized.append(upload_id)
        seen.add(upload_id)
    return tuple(normalized)


def _merge_repair_upload_ids(existing: object, incoming: Iterable[str]) -> list[str]:
    return list(_normalize_repair_upload_ids((*_normalize_repair_upload_ids(existing), *tuple(incoming))))


def _repair_next_retry_at(now: datetime, attempt_count: int) -> datetime:
    if attempt_count <= 1:
        return now + timedelta(seconds=5)
    if attempt_count == 2:
        return now + timedelta(seconds=30)
    return now + timedelta(seconds=120)


def _row_to_conversation_file_index_repair_marker(
    row: ConversationFileIndexRepairMarkerRow,
) -> ConversationFileIndexRepairMarker:
    return ConversationFileIndexRepairMarker(
        conversation_id=row.conversation_id,
        repair_kind=row.repair_kind,
        status=row.status,
        reason_code=row.reason_code,
        affected_upload_ids=_normalize_repair_upload_ids(row.affected_upload_ids),
        attempt_count=int(row.attempt_count or 0),
        next_retry_at=row.next_retry_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        resolved_at=row.resolved_at,
    )


def _row_to_conversation_memory_summary(row: ConversationMemorySummaryRow) -> ConversationMemorySummary:
    return ConversationMemorySummary(
        summary_id=row.summary_id,
        conversation_id=row.conversation_id,
        username=row.username,
        covered_until_turn_id=row.covered_until_turn_id,
        covered_until_message_id=row.covered_until_message_id,
        covered_until_created_at=row.covered_until_created_at,
        summary_text=row.summary_text,
        source_message_count=row.source_message_count,
        source_message_ids_hash=row.source_message_ids_hash,
        estimated_tokens=row.estimated_tokens,
        summary_version=row.summary_version,
        compression_policy_version=row.compression_policy_version,
        model_metadata_safe=dict(row.model_metadata_safe or {}),
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_pending_skill_context(row: PendingSkillContextRow) -> PendingSkillContext:
    return PendingSkillContext(
        context_id=row.context_id,
        conversation_id=row.conversation_id,
        username=row.username,
        capability_id=row.capability_id,
        skill_name=row.skill_name,
        source_task_id=row.source_task_id,
        source_message_id=row.source_message_id,
        original_user_message=row.original_user_message,
        missing_requirements=tuple(str(item) for item in (row.missing_requirements or ())),
        assistant_message=row.assistant_message,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row_to_auth_user_token(row: AuthUserTokenRow) -> AuthUserToken:
    return AuthUserToken(
        username=row.username,
        api_token_hash=row.api_token_hash,
        token_issued_at=row.token_issued_at,
        token_last_used_at=row.token_last_used_at,
        auth_generation=int(row.auth_generation or 0),
        auth_generation_updated_at=row.auth_generation_updated_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_metadata_object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _message_type_value(value: object) -> str:
    text = str(value or "").strip()
    return text or "chat"


def _file_upload_audit_event_id(
    *,
    event_type: str,
    conversation_id: str,
    upload_id: str,
    outcome: str,
    reason_code: str | None,
    at: datetime,
) -> str:
    serialized = json.dumps(
        {
            "at": at.isoformat(),
            "conversation_id": conversation_id,
            "event_type": event_type,
            "outcome": outcome,
            "reason_code": reason_code,
            "upload_id": upload_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f"file_upload_audit:{upload_id}:{event_type.rsplit('.', 1)[-1]}:{digest}"


def _file_upload_message_error_reason(message: str) -> str:
    if "another conversation" in message:
        return "conversation_mismatch"
    if "non-file_upload" in message:
        return "message_type_conflict"
    if "resurrected" in message:
        return "deleted_no_resurrection"
    return "repository_error"


def _row_to_message(row: MessageRow) -> Message:
    return Message(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        task_id=row.task_id,
        stream_status=row.stream_status,
        created_at=row.created_at,
        message_type=_message_type_value(getattr(row, "message_type", None)),
        metadata=_message_metadata_object(getattr(row, "message_metadata", None)),
        updated_at=getattr(row, "updated_at", None),
    )


_MCP_EXECUTION_MODES = frozenset({"legacy", "user_scoped", "unavailable"})
_MCP_ROLLOUT_MODES = frozenset({"off", "shadow", "enforce"})
_MCP_ROUTE_REASON_CODES = frozenset(
    {
        "routing_off",
        "shadow_enabled",
        "enforce_selected",
        "cohort_not_selected",
        "percent_not_selected",
        "explicit_legacy_capability",
        "user_server_rollout_unavailable",
        "no_execution_path",
    }
)
_TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED}
)
_TERMINAL_NODE_STATUSES = frozenset(
    {
        NodeStatus.COMPLETED,
        NodeStatus.FAILED,
        NodeStatus.CANCELLED,
        NodeStatus.BLOCKED_BY_CANCELLATION,
        NodeStatus.ORPHANED,
    }
)


def _validated_mcp_task_assignment(
    *,
    execution_mode: Any,
    shadow_enabled: Any,
    config_version: Any,
    reason_code: Any,
    rollout_mode: Any,
) -> dict[str, Any]:
    values = (execution_mode, shadow_enabled, config_version, reason_code, rollout_mode)
    if all(value is None for value in values):
        return {
            "mcp_execution_mode": None,
            "mcp_shadow_enabled": None,
            "mcp_rollout_config_version": None,
            "mcp_route_reason_code": None,
            "mcp_rollout_mode": None,
        }
    if any(value is None for value in values):
        raise ValueError("mcp_task_route_assignment_corrupt: task assignment must be all null or all non-null")
    if execution_mode not in _MCP_EXECUTION_MODES:
        raise ValueError("mcp_task_route_assignment_invalid: unsupported execution mode")
    if type(shadow_enabled) is not bool:
        raise ValueError("mcp_task_route_assignment_invalid: shadow flag must be boolean")
    if not isinstance(config_version, str) or not config_version:
        raise ValueError("mcp_task_route_assignment_invalid: config version must be non-empty")
    if reason_code not in _MCP_ROUTE_REASON_CODES:
        raise ValueError("mcp_task_route_assignment_invalid: unsupported reason code")
    if rollout_mode not in _MCP_ROLLOUT_MODES:
        raise ValueError("mcp_task_route_assignment_invalid: unsupported rollout mode")
    return {
        "mcp_execution_mode": execution_mode,
        "mcp_shadow_enabled": shadow_enabled,
        "mcp_rollout_config_version": config_version,
        "mcp_route_reason_code": reason_code,
        "mcp_rollout_mode": rollout_mode,
    }


def _task_mcp_assignment(task: Task) -> dict[str, Any]:
    return _validated_mcp_task_assignment(
        execution_mode=task.mcp_execution_mode,
        shadow_enabled=task.mcp_shadow_enabled,
        config_version=task.mcp_rollout_config_version,
        reason_code=task.mcp_route_reason_code,
        rollout_mode=task.mcp_rollout_mode,
    )


def _row_to_task(row: TaskRow) -> Task:
    assignment = _validated_mcp_task_assignment(
        execution_mode=row.mcp_execution_mode,
        shadow_enabled=row.mcp_shadow_enabled,
        config_version=row.mcp_rollout_config_version,
        reason_code=row.mcp_route_reason_code,
        rollout_mode=row.mcp_rollout_mode,
    )
    return Task(
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        root_message_id=row.root_message_id,
        status=row.status,
        routing_mode=row.routing_mode,
        requested_capability_id=row.requested_capability_id,
        root_node_id=row.root_node_id,
        summary=row.summary,
        cancel_requested_at=row.cancel_requested_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        **assignment,
    )


def _row_to_task_node(row: TaskNodeRow) -> TaskNode:
    return TaskNode(
        node_id=row.node_id,
        task_id=row.task_id,
        capability_id=row.capability_id,
        assigned_instance_id=row.assigned_instance_id,
        status=row.status,
        criticality=row.criticality,
        dependency_type=row.dependency_type,
        retry_policy=row.retry_policy or {},
        timeout_policy=row.timeout_policy or {},
        resource_class=row.resource_class,
        input_refs=tuple(row.input_refs or ()),
        output_refs=tuple(row.output_refs or ()),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _row_to_task_edge(row: TaskEdgeRow) -> TaskEdge:
    return TaskEdge(from_node_id=row.from_node_id, to_node_id=row.to_node_id, edge_type=row.edge_type, condition=row.condition)


def _row_to_artifact(row: ArtifactRow) -> Artifact:
    return Artifact(
        artifact_id=row.artifact_id,
        task_id=row.task_id,
        producer_node_id=row.producer_node_id,
        artifact_type=row.artifact_type,
        storage_ref=row.storage_ref,
        summary=row.summary,
        is_complete=bool(row.is_complete),
        created_at=row.created_at,
    )


def _row_to_task_input_attachment(row: TaskInputAttachmentRow) -> TaskInputAttachment:
    return TaskInputAttachment(
        attachment_id=row.attachment_id,
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        source_kind=row.source_kind,
        source_upload_id=row.source_upload_id,
        source_message_id=row.source_message_id,
        interrupt_answer_id=row.interrupt_answer_id,
        filename=row.filename,
        content_type=row.content_type,
        file_type=row.file_type,
        size_bytes=int(row.size_bytes or 0),
        sha256=row.sha256,
        prompt_artifact=dict(row.prompt_artifact or {}),
        skill_artifact=dict(row.skill_artifact or {}),
        source_payload=dict(row.source_payload or {}),
        selected_sheet=row.selected_sheet,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_event_record(row: EventRecordRow) -> EventRecord:
    return EventRecord(
        event_id=row.event_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        node_id=row.node_id,
        agent_id=row.agent_id,
        event_type=row.event_type,
        payload=row.payload or {},
        visibility=row.visibility,
        created_at=row.created_at,
    )


def _row_to_mailbox_message(row: MailboxMessageRow) -> MailboxMessage:
    return MailboxMessage(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        node_id=row.node_id,
        parent_message_id=row.parent_message_id,
        correlation_id=row.correlation_id,
        from_agent=row.from_agent,
        to_agent=row.to_agent,
        to_role=row.to_role,
        channel=row.channel,
        message_type=row.message_type,
        ack_policy=row.ack_policy,
        priority=row.priority,
        payload=row.payload or {},
        payload_schema_version=row.payload_schema_version,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


def _row_to_mailbox_delivery(row: MailboxDeliveryRow) -> MailboxDelivery:
    return MailboxDelivery(
        delivery_id=row.delivery_id,
        message_id=row.message_id,
        recipient_agent=row.recipient_agent,
        recipient_role=row.recipient_role,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        ttl_seconds=row.ttl_seconds,
        expires_at=row.expires_at,
        delivered_at=row.delivered_at,
        acknowledged_at=row.acknowledged_at,
        resolved_at=row.resolved_at,
        next_retry_at=row.next_retry_at,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_interrupt(row: InterruptRow) -> Interrupt:
    return Interrupt(
        interrupt_id=row.interrupt_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        node_id=row.node_id,
        source_agent=row.source_agent,
        source_message_id=row.source_message_id,
        question=row.question,
        reason_code=row.reason_code,
        required_fields=row.required_fields or {},
        status=row.status,
        expires_at=row.expires_at,
        created_at=row.created_at,
        answered_at=row.answered_at,
        cancelled_at=row.cancelled_at,
    )


def _row_to_interrupt_answer(row: InterruptAnswerRow) -> InterruptAnswer:
    return InterruptAnswer(
        interrupt_answer_id=row.interrupt_answer_id,
        interrupt_id=row.interrupt_id,
        answer_payload=row.answer_payload or {},
        source_message_id=row.source_message_id,
        accepted=bool(row.accepted),
        created_at=row.created_at,
        accepted_at=row.accepted_at,
    )


def _slot_collection_row_values(collection: SlotCollection) -> dict[str, object]:
    return {
        "task_id": collection.task_id,
        "node_id": collection.node_id,
        "conversation_id": collection.conversation_id,
        "capability_id": collection.capability_id,
        "skill_name": collection.skill_name,
        "kind": collection.kind,
        "status": collection.status,
        "round": collection.round,
        "revision": collection.revision,
        "selected_schema_id": collection.selected_schema_id,
        "selected_entrypoint": collection.selected_entrypoint,
        "skill_bundle_revision": collection.skill_bundle_revision,
        "contract_revision": collection.contract_revision,
        "schema_digest": collection.schema_digest,
        "schema_snapshot_json": dict(collection.schema_snapshot),
        "slots_json": dict(collection.slots),
        "resolved_json": dict(collection.resolved),
        "missing_json": list(collection.missing),
        "invalid_json": [dict(item) for item in collection.invalid],
        "last_question": collection.last_question,
        "created_at": collection.created_at,
        "updated_at": collection.updated_at,
        "completed_at": collection.completed_at,
        "cancelled_at": collection.cancelled_at,
        "failed_at": collection.failed_at,
    }


def _row_to_slot_collection(row: SlotCollectionRow) -> SlotCollection:
    return SlotCollection(
        collection_id=row.collection_id,
        task_id=row.task_id,
        node_id=row.node_id,
        conversation_id=row.conversation_id,
        capability_id=row.capability_id,
        skill_name=row.skill_name,
        kind=row.kind,
        status=row.status,
        round=int(row.round or 1),
        revision=int(row.revision or 0),
        selected_schema_id=row.selected_schema_id,
        selected_entrypoint=row.selected_entrypoint,
        skill_bundle_revision=row.skill_bundle_revision,
        contract_revision=row.contract_revision,
        schema_digest=row.schema_digest,
        schema_snapshot=dict(row.schema_snapshot_json or {}),
        slots=dict(row.slots_json or {}),
        resolved=dict(row.resolved_json or {}),
        missing=tuple(str(item) for item in (row.missing_json or ())),
        invalid=tuple(dict(item) for item in (row.invalid_json or ()) if isinstance(item, Mapping)),
        last_question=row.last_question,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        cancelled_at=row.cancelled_at,
        failed_at=row.failed_at,
    )


def _row_to_slot_event(row: SlotEventRow) -> SlotEvent:
    return SlotEvent(
        slot_event_id=row.slot_event_id,
        collection_id=row.collection_id,
        task_id=row.task_id,
        node_id=row.node_id,
        conversation_id=row.conversation_id,
        event_type=row.event_type,
        round=int(row.round or 1),
        revision=int(row.revision or 0),
        idempotency_key=row.idempotency_key,
        payload=dict(row.payload_json or {}),
        created_at=row.created_at,
    )


def _row_to_checkpoint(row: CheckpointRow) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=row.checkpoint_id,
        task_id=row.task_id,
        node_id=row.node_id,
        agent_id=row.agent_id,
        snapshot_ref=row.snapshot_ref,
        snapshot_kind=row.snapshot_kind,
        resume_token=row.resume_token,
        source_message_id=row.source_message_id,
        created_at=row.created_at,
        invalidated_at=row.invalidated_at,
    )


def _ensure_event_append_payload_within_rust_contract(event: EventRecord) -> None:
    ensure_sidecar_write_allowed(
        component="event_log",
        operation_name="event_append",
        unavailable_error_code="event_log_unavailable",
    )
    payload_size = len(json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"))
    limit = runtime_resource_limit("event_payload_bytes")
    if payload_size > limit:
        error_code = runtime_error_policy("event_log_payload_too_large")["code"]
        raise ValueError(f"{error_code}: event payload exceeds Rust runtime sidecar limit of {limit} bytes")


def _ensure_event_replay_policy_compatible_with_rust_contract() -> tuple[int, int]:
    policy = runtime_operation_policy("event_replay")
    if policy.get("kind") != "read" or policy.get("python_legacy_write_fallback") is not False:
        raise RuntimeError("Rust runtime sidecar event_replay policy is incompatible")
    return (
        runtime_resource_limit("replay_page_events"),
        runtime_resource_limit("replay_page_bytes"),
    )


def _resolve_event_replay_page_limit(requested_limit: int | None, event_limit: int) -> int:
    if requested_limit is None:
        return event_limit
    if requested_limit < 1 or requested_limit > event_limit:
        error_code = runtime_error_policy("event_log_replay_page_exceeded")["code"]
        raise ValueError(f"{error_code}: requested event replay page exceeds Rust runtime sidecar limit")
    return requested_limit


def _ensure_event_replay_page_within_rust_contract(events: list[EventRecord], event_limit: int, byte_limit: int) -> None:
    if len(events) > event_limit:
        error_code = runtime_error_policy("event_log_replay_page_exceeded")["code"]
        raise ValueError(f"{error_code}: event replay exceeds Rust runtime sidecar page limit")
    payload_bytes = sum(
        len(json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"))
        for event in events
    )
    if payload_bytes > byte_limit:
        error_code = runtime_error_policy("event_log_replay_page_exceeded")["code"]
        raise ValueError(f"{error_code}: event replay exceeds Rust runtime sidecar page limit")


def _ensure_runtime_store_write_allowed_by_rust_contract(
    operation_name: str,
    *,
    task_authority_mode: str | None = None,
) -> None:
    if task_authority_mode in {"off", "shadow"}:
        return
    if task_authority_mode == "enforce":
        raise RuntimeError(
            "runtime_store_unavailable: MCP Task enforce authority is active "
            "but the Python store write path was reached"
        )
    ensure_sidecar_write_allowed(
        component="runtime_store",
        operation_name=operation_name,
        unavailable_error_code="runtime_store_unavailable",
    )


class SQLiteStateRepository:
    def __init__(
        self,
        session: Session,
        *,
        task_authority_mode: str | None = None,
    ) -> None:
        self._session = session
        self._task_authority_mode = task_authority_mode

    def save_auth_user_token(self, token: AuthUserToken, *, auth_generation_reason: str | None = None) -> AuthUserToken:
        at = token.updated_at or token.auth_generation_updated_at or _utcnow_naive()
        existing = self._session.execute(
            select(AuthUserTokenRow).where(AuthUserTokenRow.username == token.username).with_for_update()
        ).scalar_one_or_none()
        if existing is None:
            row = AuthUserTokenRow(
                username=token.username,
                api_token_hash=token.api_token_hash,
                token_issued_at=token.token_issued_at,
                token_last_used_at=token.token_last_used_at,
                auth_generation=int(token.auth_generation or 1),
                auth_generation_updated_at=at,
                created_at=token.created_at or at,
                updated_at=at,
            )
            self._session.add(row)
            self._session.flush()
            saved = _row_to_auth_user_token(row)
        else:
            existing.api_token_hash = token.api_token_hash
            existing.token_issued_at = token.token_issued_at
            existing.token_last_used_at = token.token_last_used_at
            existing.auth_generation = int(existing.auth_generation or 0) + 1
            existing.auth_generation_updated_at = at
            existing.updated_at = at
            if existing.created_at is None:
                existing.created_at = token.created_at or at
            self._session.flush()
            saved = _row_to_auth_user_token(existing)
        self._notify_auth_generation_change(saved, auth_generation_reason, changed_at=at)
        return saved

    def get_auth_user_token(self, username: str) -> AuthUserToken | None:
        row = self._session.get(AuthUserTokenRow, username)
        return None if row is None else _row_to_auth_user_token(row)

    def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None:
        row = self._session.execute(
            select(AuthUserTokenRow).where(AuthUserTokenRow.api_token_hash == api_token_hash)
        ).scalar_one_or_none()
        return None if row is None else _row_to_auth_user_token(row)

    def get_auth_user_generation(self, username: str) -> AuthUserToken | None:
        return self.get_auth_user_token(username)

    def list_auth_user_generations(self) -> list[AuthUserToken]:
        rows = self._session.scalars(select(AuthUserTokenRow).order_by(AuthUserTokenRow.username)).all()
        return [_row_to_auth_user_token(row) for row in rows]

    def touch_auth_user_token_last_used(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        result = self._session.execute(
            update(AuthUserTokenRow)
            .where(
                AuthUserTokenRow.username == username,
                AuthUserTokenRow.api_token_hash == api_token_hash,
            )
            .values(token_last_used_at=at, updated_at=at)
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        return None if row is None else _row_to_auth_user_token(row)

    def clear_auth_user_token(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None:
        result = self._session.execute(
            update(AuthUserTokenRow)
            .where(
                AuthUserTokenRow.username == username,
                AuthUserTokenRow.api_token_hash == api_token_hash,
            )
            .values(
                api_token_hash=None,
                token_issued_at=None,
                token_last_used_at=None,
                auth_generation=AuthUserTokenRow.auth_generation + 1,
                auth_generation_updated_at=at,
                updated_at=at,
            )
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        saved = None if row is None else _row_to_auth_user_token(row)
        if saved is not None:
            self._notify_auth_generation_change(saved, auth_generation_reason, changed_at=at)
        return saved

    def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None:
        result = self._session.execute(
            update(AuthUserTokenRow)
            .where(
                AuthUserTokenRow.username == username,
                AuthUserTokenRow.api_token_hash == old_api_token_hash,
            )
            .values(
                api_token_hash=new_api_token_hash,
                token_issued_at=at,
                token_last_used_at=None,
                auth_generation=AuthUserTokenRow.auth_generation + 1,
                auth_generation_updated_at=at,
                updated_at=at,
            )
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        saved = None if row is None else _row_to_auth_user_token(row)
        if saved is not None:
            self._notify_auth_generation_change(saved, auth_generation_reason, changed_at=at)
        return saved

    def _notify_auth_generation_change(
        self,
        token: AuthUserToken,
        reason: str | None,
        *,
        changed_at: datetime,
    ) -> None:
        if not reason:
            return
        bind = self._session.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return
        sql, params = auth_generation_notify_sql(
            AuthGenerationChanged(
                username=token.username,
                auth_generation=token.auth_generation,
                changed_at=changed_at,
                reason=cast(AuthGenerationReason, reason),
            )
        )
        self._session.execute(text(sql), params)

    def save_conversation(self, conversation: Conversation) -> Conversation:
        existing = self._session.get(ConversationRow, conversation.conversation_id)
        if (
            existing is not None
            and existing.status in {str(ConversationStatus.DELETING), str(ConversationStatus.DELETING_FAILED)}
            and str(conversation.status) == str(ConversationStatus.ACTIVE)
        ):
            raise ValueError(f"Conversation is not available: {conversation.conversation_id}")
        row = ConversationRow(
            conversation_id=conversation.conversation_id,
            username=conversation.username,
            status=conversation.status,
            current_task_id=conversation.current_task_id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            delete_runner_id=conversation.delete_runner_id,
            delete_requested_at=conversation.delete_requested_at,
            delete_started_at=conversation.delete_started_at,
            delete_finished_at=conversation.delete_finished_at,
            delete_failed_at=conversation.delete_failed_at,
            delete_error_code=conversation.delete_error_code,
            delete_error_summary=conversation.delete_error_summary,
            delete_phase=conversation.delete_phase,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_conversation(merged)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        return None if row is None else _row_to_conversation(row)

    def list_conversations_for_username(self, username: str) -> list[Conversation]:
        rows = self._session.scalars(
            select(ConversationRow)
            .where(ConversationRow.username == username, ConversationRow.status == str(ConversationStatus.ACTIVE))
            .order_by(ConversationRow.updated_at.desc(), ConversationRow.conversation_id.desc())
        ).all()
        return [_row_to_conversation(row) for row in rows]

    def list_deleting_conversations(self) -> list[Conversation]:
        rows = self._session.scalars(
            select(ConversationRow)
            .where(ConversationRow.status.in_([str(ConversationStatus.DELETING), str(ConversationStatus.DELETING_FAILED)]))
            .order_by(ConversationRow.updated_at.asc(), ConversationRow.conversation_id.asc())
        ).all()
        return [_row_to_conversation(row) for row in rows]

    def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None:
            return None
        if row.status == str(ConversationStatus.DELETING_FAILED):
            return _row_to_conversation(row)
        if row.status != str(ConversationStatus.DELETING):
            row.status = str(ConversationStatus.DELETING)
            row.delete_requested_at = requested_at
        row.delete_runner_id = row.delete_runner_id or runner_id
        if started_at is not None:
            row.delete_started_at = started_at
        row.delete_phase = phase
        row.delete_error_code = None
        row.delete_error_summary = None
        row.updated_at = requested_at
        self._session.flush()
        return _row_to_conversation(row)

    def update_conversation_delete_phase(
        self,
        conversation_id: str,
        *,
        phase: str,
        updated_at: datetime,
        runner_id: str | None = None,
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None:
            return None
        row.delete_phase = phase
        row.updated_at = updated_at
        if phase != "marking" and row.delete_started_at is None:
            row.delete_started_at = updated_at
        if runner_id is not None:
            row.delete_runner_id = runner_id
        self._session.flush()
        return _row_to_conversation(row)

    def mark_conversation_delete_failed(
        self,
        conversation_id: str,
        *,
        failed_at: datetime,
        phase: str,
        error_code: str,
        error_summary: str,
        runner_id: str | None = None,
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None:
            return None
        row.status = str(ConversationStatus.DELETING_FAILED)
        row.delete_failed_at = failed_at
        row.delete_finished_at = failed_at
        row.delete_phase = phase
        row.delete_error_code = error_code[:120]
        row.delete_error_summary = error_summary[:500]
        if runner_id is not None:
            row.delete_runner_id = runner_id
        row.updated_at = failed_at
        self._session.flush()
        return _row_to_conversation(row)

    def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None or row.status != str(ConversationStatus.DELETING_FAILED):
            return None
        row.status = str(ConversationStatus.DELETING)
        row.delete_runner_id = runner_id
        row.delete_requested_at = requested_at
        row.delete_started_at = started_at
        row.delete_finished_at = None
        row.delete_failed_at = None
        row.delete_error_code = None
        row.delete_error_summary = None
        row.delete_phase = phase
        row.updated_at = requested_at
        self._session.flush()
        return _row_to_conversation(row)

    def save_conversation_memory_summary(self, summary: ConversationMemorySummary) -> ConversationMemorySummary:
        row = ConversationMemorySummaryRow(
            summary_id=summary.summary_id,
            conversation_id=summary.conversation_id,
            username=summary.username,
            covered_until_turn_id=summary.covered_until_turn_id,
            covered_until_message_id=summary.covered_until_message_id,
            covered_until_created_at=summary.covered_until_created_at,
            summary_text=summary.summary_text,
            source_message_count=summary.source_message_count,
            source_message_ids_hash=summary.source_message_ids_hash,
            estimated_tokens=summary.estimated_tokens,
            summary_version=summary.summary_version,
            compression_policy_version=summary.compression_policy_version,
            model_metadata_safe=dict(summary.model_metadata_safe),
            last_error=summary.last_error,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_conversation_memory_summary(merged)

    def get_conversation_memory_summary(self, summary_id: str) -> ConversationMemorySummary | None:
        row = self._session.get(ConversationMemorySummaryRow, summary_id)
        return None if row is None else _row_to_conversation_memory_summary(row)

    def get_latest_conversation_memory_summary(
        self,
        conversation_id: str,
        *,
        username: str | None = None,
    ) -> ConversationMemorySummary | None:
        statement = select(ConversationMemorySummaryRow).where(
            ConversationMemorySummaryRow.conversation_id == conversation_id
        )
        if username is not None:
            statement = statement.where(ConversationMemorySummaryRow.username == username)
        row = self._session.scalar(
            statement.order_by(
                ConversationMemorySummaryRow.covered_until_created_at.desc(),
                ConversationMemorySummaryRow.covered_until_turn_id.desc(),
                ConversationMemorySummaryRow.covered_until_message_id.desc(),
                ConversationMemorySummaryRow.updated_at.desc(),
                ConversationMemorySummaryRow.created_at.desc(),
                ConversationMemorySummaryRow.summary_id.desc(),
            )
        )
        return None if row is None else _row_to_conversation_memory_summary(row)

    def list_conversation_memory_summaries(self, conversation_id: str) -> list[ConversationMemorySummary]:
        rows = self._session.scalars(
            select(ConversationMemorySummaryRow)
            .where(ConversationMemorySummaryRow.conversation_id == conversation_id)
            .order_by(
                ConversationMemorySummaryRow.covered_until_created_at.desc(),
                ConversationMemorySummaryRow.covered_until_turn_id.desc(),
                ConversationMemorySummaryRow.covered_until_message_id.desc(),
                ConversationMemorySummaryRow.updated_at.desc(),
                ConversationMemorySummaryRow.created_at.desc(),
                ConversationMemorySummaryRow.summary_id.desc(),
            )
        ).all()
        return [_row_to_conversation_memory_summary(row) for row in rows]

    def delete_conversation_memory_summaries_for_conversation(self, conversation_id: str) -> int:
        result = self._session.execute(
            delete(ConversationMemorySummaryRow).where(ConversationMemorySummaryRow.conversation_id == conversation_id)
        )
        self._session.flush()
        return int(result.rowcount if result.rowcount is not None and result.rowcount > 0 else 0)

    def save_pending_skill_context(self, context: PendingSkillContext) -> PendingSkillContext:
        if context.status == "pending_user_input":
            self.mark_pending_skill_context_superseded(
                context.conversation_id,
                exclude_context_id=context.context_id,
                updated_at=context.updated_at or context.created_at,
            )
        row = PendingSkillContextRow(
            context_id=context.context_id,
            conversation_id=context.conversation_id,
            username=context.username,
            capability_id=context.capability_id,
            skill_name=context.skill_name,
            source_task_id=context.source_task_id,
            source_message_id=context.source_message_id,
            original_user_message=context.original_user_message,
            missing_requirements=list(context.missing_requirements),
            assistant_message=context.assistant_message,
            status=context.status,
            created_at=context.created_at,
            updated_at=context.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_pending_skill_context(merged)

    def get_pending_skill_context(self, context_id: str) -> PendingSkillContext | None:
        row = self._session.get(PendingSkillContextRow, context_id)
        return None if row is None else _row_to_pending_skill_context(row)

    def get_active_pending_skill_context(self, conversation_id: str) -> PendingSkillContext | None:
        row = self._session.scalar(
            select(PendingSkillContextRow)
            .where(
                PendingSkillContextRow.conversation_id == conversation_id,
                PendingSkillContextRow.status == "pending_user_input",
            )
            .order_by(PendingSkillContextRow.updated_at.desc(), PendingSkillContextRow.created_at.desc(), PendingSkillContextRow.context_id.desc())
        )
        return None if row is None else _row_to_pending_skill_context(row)

    def mark_pending_skill_context_consumed(self, context_id: str, *, updated_at: datetime | None = None) -> PendingSkillContext | None:
        return self._mark_pending_skill_context_status(context_id, "consumed", updated_at=updated_at)

    def mark_pending_skill_context_cancelled(self, context_id: str, *, updated_at: datetime | None = None) -> PendingSkillContext | None:
        return self._mark_pending_skill_context_status(context_id, "cancelled", updated_at=updated_at)

    def mark_pending_skill_context_superseded(
        self,
        conversation_id: str,
        *,
        exclude_context_id: str | None = None,
        updated_at: datetime | None = None,
    ) -> int:
        status_updated_at = updated_at or _utcnow_naive()
        rows = self._session.scalars(
            select(PendingSkillContextRow).where(
                PendingSkillContextRow.conversation_id == conversation_id,
                PendingSkillContextRow.status == "pending_user_input",
            )
        ).all()
        count = 0
        for row in rows:
            if exclude_context_id is not None and row.context_id == exclude_context_id:
                continue
            row.status = "superseded"
            row.updated_at = status_updated_at
            count += 1
        self._session.flush()
        return count

    def _mark_pending_skill_context_status(
        self,
        context_id: str,
        status: str,
        *,
        updated_at: datetime | None = None,
    ) -> PendingSkillContext | None:
        row = self._session.get(PendingSkillContextRow, context_id)
        if row is None:
            return None
        row.status = status
        row.updated_at = updated_at or _utcnow_naive()
        self._session.flush()
        return _row_to_pending_skill_context(row)

    def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        task_ids = list(
            self._session.scalars(select(TaskRow.task_id).where(TaskRow.conversation_id == conversation_id)).all()
        )
        mailbox_conditions = [MailboxMessageRow.conversation_id == conversation_id]
        event_conditions = [EventRecordRow.conversation_id == conversation_id]
        interrupt_conditions = [InterruptRow.conversation_id == conversation_id]
        message_conditions = [MessageRow.conversation_id == conversation_id]
        slot_collection_conditions = [SlotCollectionRow.conversation_id == conversation_id]
        slot_event_conditions = [SlotEventRow.conversation_id == conversation_id]
        if task_ids:
            mailbox_conditions.append(MailboxMessageRow.task_id.in_(task_ids))
            event_conditions.append(EventRecordRow.task_id.in_(task_ids))
            interrupt_conditions.append(InterruptRow.task_id.in_(task_ids))
            message_conditions.append(MessageRow.task_id.in_(task_ids))
            slot_collection_conditions.append(SlotCollectionRow.task_id.in_(task_ids))
            slot_event_conditions.append(SlotEventRow.task_id.in_(task_ids))

        mailbox_message_ids = list(
            self._session.scalars(
                select(MailboxMessageRow.message_id).where(or_(*mailbox_conditions))
            ).all()
        )
        interrupt_ids = list(
            self._session.scalars(
                select(InterruptRow.interrupt_id).where(or_(*interrupt_conditions))
            ).all()
        )
        slot_collection_ids = list(
            self._session.scalars(
                select(SlotCollectionRow.collection_id).where(or_(*slot_collection_conditions))
            ).all()
        )
        if slot_collection_ids:
            slot_event_conditions.append(SlotEventRow.collection_id.in_(slot_collection_ids))

        deleted_counts: dict[str, int] = {
            "conversation_file_resource": 0,
            "conversation_memory_summary": 0,
            "conversation_pending_skill_context": 0,
            "mailbox_delivery": 0,
            "interrupt_answer": 0,
            "slot_event": 0,
            "slot_collection": 0,
            "checkpoint": 0,
            "interrupt": 0,
            "mailbox_message": 0,
            "event_record": 0,
            "artifact": 0,
            "task_input_attachment": 0,
            "mcp_remote_task_outbox": 0,
            "mcp_remote_task_binding": 0,
            "mcp_sealed_state": 0,
            "mcp_call_record": 0,
            "mcp_branch_record": 0,
            "mcp_connection_lease": 0,
            "mcp_audit_event": 0,
            "task_edge": 0,
            "task_node": 0,
            "message": 0,
            "task": 0,
            "conversation": 0,
        }

        def _delete(name: str, statement) -> None:
            result = self._session.execute(statement)
            rowcount = result.rowcount if result.rowcount is not None and result.rowcount > 0 else 0
            deleted_counts[name] = int(rowcount)

        if mailbox_message_ids:
            _delete("mailbox_delivery", delete(MailboxDeliveryRow).where(MailboxDeliveryRow.message_id.in_(mailbox_message_ids)))
        if interrupt_ids:
            _delete("interrupt_answer", delete(InterruptAnswerRow).where(InterruptAnswerRow.interrupt_id.in_(interrupt_ids)))
        _delete("slot_event", delete(SlotEventRow).where(or_(*slot_event_conditions)))
        _delete("slot_collection", delete(SlotCollectionRow).where(or_(*slot_collection_conditions)))
        if task_ids:
            _delete("checkpoint", delete(CheckpointRow).where(CheckpointRow.task_id.in_(task_ids)))
        _delete("interrupt", delete(InterruptRow).where(or_(*interrupt_conditions)))
        _delete("mailbox_message", delete(MailboxMessageRow).where(or_(*mailbox_conditions)))
        _delete("event_record", delete(EventRecordRow).where(or_(*event_conditions)))
        if task_ids:
            _delete("artifact", delete(ArtifactRow).where(ArtifactRow.task_id.in_(task_ids)))
            _delete(
                "task_input_attachment",
                delete(TaskInputAttachmentRow).where(TaskInputAttachmentRow.task_id.in_(task_ids)),
            )
            _delete(
                "mcp_remote_task_outbox",
                delete(MCPRemoteTaskOutboxRow).where(
                    MCPRemoteTaskOutboxRow.task_id.in_(task_ids)
                ),
            )
            _delete(
                "mcp_remote_task_binding",
                delete(MCPRemoteTaskBindingRow).where(MCPRemoteTaskBindingRow.task_id.in_(task_ids)),
            )
            _delete(
                "mcp_sealed_state",
                delete(MCPSealedStateRow).where(MCPSealedStateRow.task_id.in_(task_ids)),
            )
            _delete("mcp_call_record", delete(MCPCallRecordRow).where(MCPCallRecordRow.task_id.in_(task_ids)))
            _delete(
                "mcp_branch_record", delete(MCPBranchRecordRow).where(MCPBranchRecordRow.task_id.in_(task_ids))
            )
            _delete(
                "mcp_connection_lease",
                delete(MCPConnectionLeaseRow).where(MCPConnectionLeaseRow.task_id.in_(task_ids)),
            )
            _delete("mcp_audit_event", delete(MCPAuditEventRow).where(MCPAuditEventRow.task_id.in_(task_ids)))
            _delete("task_edge", delete(TaskEdgeRow).where(TaskEdgeRow.task_id.in_(task_ids)))
            _delete("task_node", delete(TaskNodeRow).where(TaskNodeRow.task_id.in_(task_ids)))
        _delete(
            "conversation_file_resource",
            delete(ConversationFileResourceRow).where(ConversationFileResourceRow.conversation_id == conversation_id),
        )
        _delete(
            "conversation_memory_summary",
            delete(ConversationMemorySummaryRow).where(ConversationMemorySummaryRow.conversation_id == conversation_id),
        )
        _delete(
            "conversation_pending_skill_context",
            delete(PendingSkillContextRow).where(PendingSkillContextRow.conversation_id == conversation_id),
        )
        _delete(
            "conversation_file_index_repair_marker",
            delete(ConversationFileIndexRepairMarkerRow).where(
                ConversationFileIndexRepairMarkerRow.conversation_id == conversation_id
            ),
        )
        _delete("message", delete(MessageRow).where(or_(*message_conditions)))
        _delete("task", delete(TaskRow).where(TaskRow.conversation_id == conversation_id))
        _delete("conversation", delete(ConversationRow).where(ConversationRow.conversation_id == conversation_id))
        self._session.flush()
        return deleted_counts

    def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]:
        return self.delete_conversation(conversation_id)

    def save_conversation_file_resource(self, resource: ConversationFileResource) -> ConversationFileResource:
        row = ConversationFileResourceRow(
            file_id=resource.file_id,
            conversation_id=resource.conversation_id,
            username=resource.username,
            original_filename=resource.original_filename,
            content_type=resource.content_type,
            file_type=resource.file_type,
            size_bytes=resource.size_bytes,
            sha256=resource.sha256,
            storage_key=resource.storage_key,
            preview=dict(resource.preview),
            description_status=resource.description_status,
            description_summary=resource.description_summary,
            description_ref=resource.description_ref,
            status=resource.status,
            normalized_filename=resource.normalized_filename,
            normalized_content_type=resource.normalized_content_type,
            requires_sheet_selection=resource.requires_sheet_selection,
            selected_sheet=resource.selected_sheet,
            created_at=resource.created_at,
            updated_at=resource.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_conversation_file_resource(merged)

    def get_conversation_file_resource(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
    ) -> ConversationFileResource | None:
        row = self._session.get(ConversationFileResourceRow, file_id)
        if row is None or row.conversation_id != conversation_id or row.username != username:
            return None
        return _row_to_conversation_file_resource(row)

    def get_conversation_file_resource_by_id(self, file_id: str) -> ConversationFileResource | None:
        row = self._session.get(ConversationFileResourceRow, file_id)
        return None if row is None else _row_to_conversation_file_resource(row)

    def list_conversation_file_resources(
        self,
        conversation_id: str,
        username: str | None = None,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[ConversationFileResource]:
        statement = select(ConversationFileResourceRow).where(ConversationFileResourceRow.conversation_id == conversation_id)
        if username is not None:
            statement = statement.where(ConversationFileResourceRow.username == username)
        if not include_deleted:
            statement = statement.where(ConversationFileResourceRow.status != "deleted")
        if cursor:
            cursor_statement = select(ConversationFileResourceRow).where(
                ConversationFileResourceRow.conversation_id == conversation_id,
                ConversationFileResourceRow.file_id == cursor,
            )
            if username is not None:
                cursor_statement = cursor_statement.where(ConversationFileResourceRow.username == username)
            cursor_row = self._session.scalars(cursor_statement).first()
            if cursor_row is None:
                return []
            statement = statement.where(
                or_(
                    ConversationFileResourceRow.created_at > cursor_row.created_at,
                    and_(
                        ConversationFileResourceRow.created_at == cursor_row.created_at,
                        ConversationFileResourceRow.file_id > cursor_row.file_id,
                    ),
                )
            )
        statement = statement.order_by(ConversationFileResourceRow.created_at, ConversationFileResourceRow.file_id)
        if limit is not None and limit > 0:
            statement = statement.limit(limit)
        rows = self._session.scalars(statement).all()
        return [_row_to_conversation_file_resource(row) for row in rows]

    def mark_conversation_file_resource_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        row = self._session.get(ConversationFileResourceRow, file_id)
        if row is None or row.conversation_id != conversation_id or row.username != username:
            return None
        row.status = "deleted"
        row.updated_at = updated_at
        self._session.flush()
        return _row_to_conversation_file_resource(row)

    def save_conversation_file_resource_with_upload_message(
        self,
        resource: ConversationFileResource,
        projection: FileUploadMessageProjection,
        *,
        now: datetime,
    ) -> ConversationFileResource:
        saved = self.save_conversation_file_resource(resource)
        self.upsert_file_upload_message(projection, now=now)
        return saved

    def mark_conversation_file_resource_and_upload_message_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        deleted = self.mark_conversation_file_resource_deleted(
            conversation_id,
            username,
            file_id,
            updated_at=updated_at,
        )
        if deleted is None:
            return None
        self.mark_file_upload_message_deleted(conversation_id, file_id, deleted_at=updated_at)
        return deleted

    def compensate_failed_conversation_file_upload(
        self,
        conversation_id: str,
        username: str,
        upload_id: str,
        *,
        reason_code: str,
        now: datetime,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "upload_id": upload_id,
            "reason_code": reason_code,
            "resource_deleted": 0,
            "message_deleted": 0,
            "status": "noop",
        }
        resource_row = self._session.get(ConversationFileResourceRow, upload_id)
        if (
            resource_row is not None
            and resource_row.conversation_id == conversation_id
            and resource_row.username == username
        ):
            self._session.delete(resource_row)
            result["resource_deleted"] = 1
        message_row = self._session.get(MessageRow, file_upload_message_id(upload_id))
        if (
            message_row is not None
            and message_row.conversation_id == conversation_id
            and _message_type_value(message_row.message_type) == FILE_UPLOAD_MESSAGE_TYPE
        ):
            self._session.delete(message_row)
            result["message_deleted"] = 1
        if result["resource_deleted"] or result["message_deleted"]:
            result["status"] = "removed"
        result["compensated_at"] = now.isoformat()
        self._session.flush()
        return result

    def record_conversation_file_index_repair_required(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        affected_upload_ids: Iterable[str] = (),
        now: datetime,
    ) -> ConversationFileIndexRepairMarker:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            attempt_count = 1
            row = ConversationFileIndexRepairMarkerRow(
                conversation_id=conversation_id,
                repair_kind=CONVERSATION_FILE_INDEX_REPAIR_KIND,
                status="pending",
                reason_code=str(reason_code or "index_write_failed"),
                affected_upload_ids=_merge_repair_upload_ids((), affected_upload_ids),
                attempt_count=attempt_count,
                next_retry_at=_repair_next_retry_at(now, attempt_count),
                created_at=now,
                updated_at=now,
                resolved_at=None,
            )
            self._session.add(row)
        else:
            attempt_count = int(row.attempt_count or 0) + 1
            row.status = "pending"
            row.reason_code = str(reason_code or row.reason_code or "index_write_failed")
            row.affected_upload_ids = _merge_repair_upload_ids(row.affected_upload_ids, affected_upload_ids)
            row.attempt_count = attempt_count
            row.next_retry_at = _repair_next_retry_at(now, attempt_count)
            row.updated_at = now
            row.resolved_at = None
            if row.created_at is None:
                row.created_at = now
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def get_conversation_file_index_repair_marker(
        self,
        conversation_id: str,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        return None if row is None else _row_to_conversation_file_index_repair_marker(row)

    def list_due_conversation_file_index_repairs(
        self,
        *,
        now: datetime,
        limit: int | None = None,
    ) -> list[ConversationFileIndexRepairMarker]:
        statement = (
            select(ConversationFileIndexRepairMarkerRow)
            .where(
                ConversationFileIndexRepairMarkerRow.repair_kind == CONVERSATION_FILE_INDEX_REPAIR_KIND,
                or_(
                    and_(
                        ConversationFileIndexRepairMarkerRow.status == "pending",
                        or_(
                            ConversationFileIndexRepairMarkerRow.next_retry_at.is_(None),
                            ConversationFileIndexRepairMarkerRow.next_retry_at <= now,
                        ),
                    ),
                    and_(
                        ConversationFileIndexRepairMarkerRow.status == "failed",
                        ConversationFileIndexRepairMarkerRow.next_retry_at.is_not(None),
                        ConversationFileIndexRepairMarkerRow.next_retry_at <= now,
                    ),
                ),
            )
            .order_by(
                ConversationFileIndexRepairMarkerRow.next_retry_at,
                ConversationFileIndexRepairMarkerRow.updated_at,
                ConversationFileIndexRepairMarkerRow.conversation_id,
            )
        )
        if limit is not None and limit > 0:
            statement = statement.limit(limit)
        rows = self._session.scalars(statement).all()
        return [_row_to_conversation_file_index_repair_marker(row) for row in rows]

    def mark_conversation_file_index_repairing(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            return None
        row.status = "repairing"
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.next_retry_at = None
        row.updated_at = now
        if row.created_at is None:
            row.created_at = now
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def mark_conversation_file_index_repair_resolved(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            return None
        row.status = "resolved"
        row.next_retry_at = None
        row.updated_at = now
        row.resolved_at = now
        if row.created_at is None:
            row.created_at = now
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def mark_conversation_file_index_repair_failed(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        now: datetime,
        retryable: bool = True,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            return None
        row.status = "failed"
        row.reason_code = str(reason_code or row.reason_code or "index_repair_failed")
        row.updated_at = now
        row.resolved_at = None
        if row.created_at is None:
            row.created_at = now
        row.next_retry_at = _repair_next_retry_at(now, int(row.attempt_count or 0)) if retryable else None
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def save_message(self, message: Message) -> Message:
        row = MessageRow(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            role=str(message.role),
            content=message.content,
            task_id=message.task_id,
            stream_status=message.stream_status,
            created_at=message.created_at,
            message_type=_message_type_value(message.message_type),
            message_metadata=_message_metadata_object(message.metadata),
            updated_at=message.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_message(merged)

    def get_message(self, message_id: str) -> Message | None:
        row = self._session.get(MessageRow, message_id)
        return None if row is None else _row_to_message(row)

    def list_messages_for_conversation(self, conversation_id: str) -> list[Message]:
        rows = self._session.scalars(
            select(MessageRow).where(MessageRow.conversation_id == conversation_id).order_by(MessageRow.created_at, MessageRow.message_id)
        ).all()
        return [_row_to_message(row) for row in rows]

    def upsert_file_upload_message(self, projection: FileUploadMessageProjection, *, now: datetime) -> Message:
        message_id = file_upload_message_id(projection.upload_id)
        metadata = safe_file_upload_message_metadata(projection.metadata, upload_id=projection.upload_id)
        content = render_file_upload_message(metadata)
        row = self._session.execute(
            select(MessageRow).where(MessageRow.message_id == message_id).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            row = MessageRow(
                message_id=message_id,
                conversation_id=projection.conversation_id,
                role=str(MessageRole.SYSTEM),
                content=content,
                task_id=None,
                stream_status="complete",
                created_at=projection.created_at or now,
                message_type=FILE_UPLOAD_MESSAGE_TYPE,
                message_metadata=metadata,
                updated_at=now,
            )
            self._session.add(row)
            self._session.flush()
            self.record_file_upload_message_audit(
                event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
                conversation_id=projection.conversation_id,
                upload_id=projection.upload_id,
                outcome="inserted",
                at=now,
                projection=FileUploadMessageProjection(
                    upload_id=projection.upload_id,
                    conversation_id=projection.conversation_id,
                    content=content,
                    metadata=metadata,
                    created_at=projection.created_at,
                ),
            )
            return _row_to_message(row)
        if row.conversation_id != projection.conversation_id:
            raise ValueError("file_upload message id belongs to another conversation")
        if _message_type_value(row.message_type) != FILE_UPLOAD_MESSAGE_TYPE:
            raise ValueError("file_upload message id conflicts with non-file_upload message")
        existing_metadata = _message_metadata_object(row.message_metadata)
        if existing_metadata.get("file_status") == "deleted" and metadata.get("file_status") != "deleted":
            raise ValueError("deleted file_upload message cannot be resurrected")
        row.role = str(MessageRole.SYSTEM)
        row.content = content
        row.task_id = None
        row.stream_status = "complete"
        row.message_type = FILE_UPLOAD_MESSAGE_TYPE
        row.message_metadata = metadata
        row.updated_at = now
        self._session.flush()
        self.record_file_upload_message_audit(
            event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
            conversation_id=projection.conversation_id,
            upload_id=projection.upload_id,
            outcome="updated",
            at=now,
            projection=FileUploadMessageProjection(
                upload_id=projection.upload_id,
                conversation_id=projection.conversation_id,
                content=content,
                metadata=metadata,
                created_at=projection.created_at,
            ),
        )
        return _row_to_message(row)

    def mark_file_upload_message_deleted(
        self,
        conversation_id: str,
        upload_id: str,
        *,
        deleted_at: datetime,
    ) -> Message | None:
        row = self._session.execute(
            select(MessageRow).where(MessageRow.message_id == file_upload_message_id(upload_id)).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            self.record_file_upload_message_audit(
                event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
                conversation_id=conversation_id,
                upload_id=upload_id,
                outcome="noop",
                reason_code="message_missing",
                at=deleted_at,
            )
            self._session.flush()
            return None
        if row.conversation_id != conversation_id:
            raise ValueError("file_upload message id belongs to another conversation")
        if _message_type_value(row.message_type) != FILE_UPLOAD_MESSAGE_TYPE:
            raise ValueError("file_upload message id conflicts with non-file_upload message")
        metadata = safe_file_upload_message_metadata(row.message_metadata, upload_id=upload_id)
        metadata["file_status"] = "deleted"
        row.role = str(MessageRole.SYSTEM)
        row.content = render_file_upload_message(metadata)
        row.task_id = None
        row.stream_status = "complete"
        row.message_type = FILE_UPLOAD_MESSAGE_TYPE
        row.message_metadata = metadata
        row.updated_at = deleted_at
        self._session.flush()
        self.record_file_upload_message_audit(
            event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
            conversation_id=conversation_id,
            upload_id=upload_id,
            outcome="marked_deleted",
            at=deleted_at,
            projection=FileUploadMessageProjection(
                upload_id=upload_id,
                conversation_id=conversation_id,
                content=row.content,
                metadata=metadata,
                created_at=row.created_at,
            ),
        )
        return _row_to_message(row)

    def record_file_upload_message_audit(
        self,
        *,
        event_type: str,
        conversation_id: str,
        upload_id: str,
        outcome: str,
        at: datetime,
        projection: FileUploadMessageProjection | None = None,
        reason_code: str | None = None,
    ) -> EventRecord:
        event = EventRecord(
            event_id=_file_upload_audit_event_id(
                event_type=event_type,
                conversation_id=conversation_id,
                upload_id=upload_id,
                outcome=outcome,
                reason_code=reason_code,
                at=at,
            ),
            conversation_id=conversation_id,
            task_id=f"conversation_file:{upload_id}",
            event_type=event_type,
            payload=file_upload_message_audit_payload(
                event_type=event_type,
                conversation_id=conversation_id,
                upload_id=upload_id,
                outcome=outcome,
                projection=projection,
                reason_code=reason_code,
            ),
            visibility=EventVisibility.AUDIT_ONLY,
            created_at=at,
        )
        _ensure_event_append_payload_within_rust_contract(event)
        row = EventRecordRow(
            event_id=event.event_id,
            conversation_id=event.conversation_id,
            task_id=event.task_id,
            node_id=event.node_id,
            agent_id=event.agent_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            visibility=event.visibility,
            created_at=event.created_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_event_record(merged)

    def save_task(self, task: Task) -> Task:
        _ensure_runtime_store_write_allowed_by_rust_contract(
            "task_submit",
            task_authority_mode=self._task_authority_mode,
        )
        assignment = _task_mcp_assignment(task)
        existing = self._session.get(TaskRow, task.task_id)
        if existing is not None:
            if (
                existing.conversation_id != task.conversation_id
                or existing.root_message_id != task.root_message_id
                or existing.routing_mode != task.routing_mode
                or existing.requested_capability_id != task.requested_capability_id
                or existing.created_at != task.created_at
            ):
                raise ValueError(
                    "task_identity_immutable: canonical Task identity fields cannot be changed"
                )
            if existing.status in _TERMINAL_TASK_STATUSES and existing.status != task.status:
                raise ValueError(
                    "task_terminal_status_immutable: terminal Task status cannot be changed"
                )
            existing_assignment = _validated_mcp_task_assignment(
                execution_mode=existing.mcp_execution_mode,
                shadow_enabled=existing.mcp_shadow_enabled,
                config_version=existing.mcp_rollout_config_version,
                reason_code=existing.mcp_route_reason_code,
                rollout_mode=existing.mcp_rollout_mode,
            )
            existing_is_assigned = any(
                value is not None for value in existing_assignment.values()
            )
            replacement_is_assigned = any(value is not None for value in assignment.values())
            if (
                self._task_authority_mode == "enforce"
                and not existing_is_assigned
            ):
                existing_task = _row_to_task(existing)
                if existing.status not in _TERMINAL_TASK_STATUSES or task != existing_task:
                    raise ValueError(
                        "mcp_task_route_assignment_migration_required: terminal legacy null assignment is read-only"
                    )
                return existing_task
            if not existing_is_assigned and replacement_is_assigned:
                raise ValueError(
                    "mcp_task_route_assignment_migration_required: legacy all-null assignment cannot become executable"
                )
            if existing_is_assigned and assignment != existing_assignment:
                raise ValueError("mcp_task_route_assignment_immutable: task assignment cannot be changed or removed")
        elif self._task_authority_mode == "enforce" and not any(
            value is not None for value in assignment.values()
        ):
            raise ValueError(
                "mcp_task_route_assignment_migration_required: enforce authority requires a canonical assignment"
            )
        row = TaskRow(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            root_message_id=task.root_message_id,
            status=task.status,
            routing_mode=task.routing_mode,
            requested_capability_id=task.requested_capability_id,
            root_node_id=task.root_node_id,
            summary=task.summary,
            cancel_requested_at=task.cancel_requested_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            **assignment,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task(merged)

    def get_task(self, task_id: str) -> Task | None:
        row = self._session.get(TaskRow, task_id)
        return None if row is None else _row_to_task(row)

    def compare_and_set_task(
        self, task: Task, *, expected_from_status: TaskStatus
    ) -> Task | None:
        existing = self._session.get(TaskRow, task.task_id)
        if existing is None or existing.status != expected_from_status:
            return None
        if (
            existing.conversation_id != task.conversation_id
            or existing.root_message_id != task.root_message_id
            or existing.routing_mode != task.routing_mode
            or existing.requested_capability_id != task.requested_capability_id
            or existing.created_at != task.created_at
        ):
            raise ValueError(
                "task_identity_immutable: canonical Task identity fields cannot be changed"
            )
        if existing.status in _TERMINAL_TASK_STATUSES and existing.status != task.status:
            raise ValueError(
                "task_terminal_status_immutable: terminal Task status cannot be changed"
            )
        assignment = _task_mcp_assignment(task)
        existing_assignment = _validated_mcp_task_assignment(
            execution_mode=existing.mcp_execution_mode,
            shadow_enabled=existing.mcp_shadow_enabled,
            config_version=existing.mcp_rollout_config_version,
            reason_code=existing.mcp_route_reason_code,
            rollout_mode=existing.mcp_rollout_mode,
        )
        if self._task_authority_mode == "enforce" and not any(
            value is not None for value in existing_assignment.values()
        ):
            existing_task = _row_to_task(existing)
            if existing.status in _TERMINAL_TASK_STATUSES and task == existing_task:
                return existing_task
            raise ValueError(
                "mcp_task_route_assignment_migration_required: terminal legacy null assignment is read-only"
            )
        if assignment != existing_assignment:
            raise ValueError(
                "mcp_task_route_assignment_immutable: task assignment cannot be changed or removed"
            )
        result = self._session.execute(
            update(TaskRow)
            .where(
                TaskRow.task_id == task.task_id,
                TaskRow.status == str(expected_from_status),
            )
            .values(
                status=str(task.status),
                routing_mode=str(task.routing_mode),
                requested_capability_id=task.requested_capability_id,
                root_node_id=task.root_node_id,
                summary=task.summary,
                cancel_requested_at=task.cancel_requested_at,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )
        if result.rowcount != 1:
            self._session.rollback()
            return None
        self._session.flush()
        return self.get_task(task.task_id)

    def get_active_task_for_conversation(self, conversation_id: str) -> Task | None:
        row = self._session.scalar(
            select(TaskRow)
            .where(
                TaskRow.conversation_id == conversation_id,
                TaskRow.status.in_(lifecycle_status_list("active_task_statuses")),
            )
            .order_by(TaskRow.created_at.desc(), TaskRow.task_id.desc())
        )
        return None if row is None else _row_to_task(row)

    def list_tasks_for_conversation(
        self,
        conversation_id: str,
        statuses: Iterable[TaskStatus] | None = None,
    ) -> list[Task]:
        query = select(TaskRow).where(TaskRow.conversation_id == conversation_id)
        if statuses is not None:
            query = query.where(TaskRow.status.in_([str(status) for status in statuses]))
        rows = self._session.scalars(query.order_by(TaskRow.created_at.desc(), TaskRow.task_id.desc())).all()
        return [_row_to_task(row) for row in rows]

    def save_task_node(self, node: TaskNode) -> TaskNode:
        _ensure_runtime_store_write_allowed_by_rust_contract(
            "node_state_transition",
            task_authority_mode=self._task_authority_mode,
        )
        existing = self._session.get(TaskNodeRow, node.node_id)
        if existing is not None:
            if existing.task_id != node.task_id or existing.capability_id != node.capability_id:
                raise ValueError(
                    "task_node_identity_immutable: task_id and capability_id cannot be changed"
                )
            if existing.status in _TERMINAL_NODE_STATUSES and existing.status != node.status:
                raise ValueError(
                    "task_node_terminal_status_immutable: terminal TaskNode status cannot be changed"
                )
        row = TaskNodeRow(
            node_id=node.node_id,
            task_id=node.task_id,
            capability_id=node.capability_id,
            assigned_instance_id=node.assigned_instance_id,
            status=node.status,
            criticality=node.criticality,
            dependency_type=node.dependency_type,
            retry_policy=dict(node.retry_policy),
            timeout_policy=dict(node.timeout_policy),
            resource_class=node.resource_class,
            input_refs=list(node.input_refs),
            output_refs=list(node.output_refs),
            started_at=node.started_at,
            finished_at=node.finished_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task_node(merged)

    def get_task_node(self, node_id: str) -> TaskNode | None:
        row = self._session.get(TaskNodeRow, node_id)
        return None if row is None else _row_to_task_node(row)

    def compare_and_set_task_node(
        self, node: TaskNode, *, expected_from_status: NodeStatus
    ) -> TaskNode | None:
        existing = self._session.get(TaskNodeRow, node.node_id)
        if existing is None or existing.status != expected_from_status:
            return None
        if existing.task_id != node.task_id or existing.capability_id != node.capability_id:
            raise ValueError(
                "task_node_identity_immutable: task_id and capability_id cannot be changed"
            )
        result = self._session.execute(
            update(TaskNodeRow)
            .where(
                TaskNodeRow.node_id == node.node_id,
                TaskNodeRow.status == str(expected_from_status),
            )
            .values(
                assigned_instance_id=node.assigned_instance_id,
                status=str(node.status),
                criticality=str(node.criticality),
                dependency_type=str(node.dependency_type),
                retry_policy=dict(node.retry_policy),
                timeout_policy=dict(node.timeout_policy),
                resource_class=node.resource_class,
                input_refs=list(node.input_refs),
                output_refs=list(node.output_refs),
                started_at=node.started_at,
                finished_at=node.finished_at,
            )
        )
        if result.rowcount != 1:
            self._session.rollback()
            return None
        self._session.flush()
        return self.get_task_node(node.node_id)

    def list_task_nodes_for_task(self, task_id: str) -> list[TaskNode]:
        rows = self._session.scalars(
            select(TaskNodeRow).where(TaskNodeRow.task_id == task_id).order_by(TaskNodeRow.node_id)
        ).all()
        return [_row_to_task_node(row) for row in rows]

    def save_task_edge(self, task_id: str, edge: TaskEdge) -> TaskEdge:
        _ensure_runtime_store_write_allowed_by_rust_contract("task_edge_save")
        row = TaskEdgeRow(
            edge_id=build_task_edge_id(task_id, edge.from_node_id, edge.to_node_id),
            task_id=task_id,
            from_node_id=edge.from_node_id,
            to_node_id=edge.to_node_id,
            edge_type=edge.edge_type,
            condition=edge.condition,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task_edge(merged)

    def list_task_edges(self, task_id: str) -> list[TaskEdge]:
        rows = self._session.scalars(
            select(TaskEdgeRow).where(TaskEdgeRow.task_id == task_id).order_by(TaskEdgeRow.from_node_id, TaskEdgeRow.to_node_id)
        ).all()
        return [_row_to_task_edge(row) for row in rows]

    def save_artifact(self, artifact: Artifact) -> Artifact:
        _ensure_runtime_store_write_allowed_by_rust_contract("artifact_save")
        row = ArtifactRow(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            producer_node_id=artifact.producer_node_id,
            artifact_type=artifact.artifact_type,
            storage_ref=artifact.storage_ref,
            summary=artifact.summary,
            is_complete=artifact.is_complete,
            created_at=artifact.created_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_artifact(merged)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        row = self._session.get(ArtifactRow, artifact_id)
        return None if row is None else _row_to_artifact(row)

    def list_artifacts_for_task(self, task_id: str) -> list[Artifact]:
        rows = self._session.scalars(
            select(ArtifactRow).where(ArtifactRow.task_id == task_id).order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
        ).all()
        return [_row_to_artifact(row) for row in rows]

    def list_artifacts_for_conversation(self, conversation_id: str) -> list[Artifact]:
        rows = self._session.scalars(
            select(ArtifactRow)
            .join(TaskRow, ArtifactRow.task_id == TaskRow.task_id)
            .where(TaskRow.conversation_id == conversation_id)
            .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
        ).all()
        return [_row_to_artifact(row) for row in rows]

    def save_task_input_attachment(self, attachment: TaskInputAttachment) -> TaskInputAttachment:
        row = TaskInputAttachmentRow(
            attachment_id=attachment.attachment_id,
            task_id=attachment.task_id,
            conversation_id=attachment.conversation_id,
            source_kind=attachment.source_kind,
            source_upload_id=attachment.source_upload_id,
            source_message_id=attachment.source_message_id,
            interrupt_answer_id=attachment.interrupt_answer_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            file_type=attachment.file_type,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
            prompt_artifact=dict(attachment.prompt_artifact),
            skill_artifact=dict(attachment.skill_artifact),
            source_payload=dict(attachment.source_payload),
            selected_sheet=attachment.selected_sheet,
            created_at=attachment.created_at,
            updated_at=attachment.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task_input_attachment(merged)

    def list_task_input_attachments_for_task(self, task_id: str) -> list[TaskInputAttachment]:
        rows = self._session.scalars(
            select(TaskInputAttachmentRow)
            .where(TaskInputAttachmentRow.task_id == task_id)
            .order_by(TaskInputAttachmentRow.created_at, TaskInputAttachmentRow.attachment_id)
        ).all()
        return [_row_to_task_input_attachment(row) for row in rows]

    def list_task_input_attachments_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
    ) -> list[TaskInputAttachment]:
        statement = (
            select(TaskInputAttachmentRow)
            .where(TaskInputAttachmentRow.conversation_id == conversation_id)
            .order_by(TaskInputAttachmentRow.updated_at.desc(), TaskInputAttachmentRow.created_at.desc(), TaskInputAttachmentRow.attachment_id)
        )
        if limit is not None:
            statement = statement.limit(max(0, int(limit)))
        rows = self._session.scalars(statement).all()
        return [_row_to_task_input_attachment(row) for row in rows]

    def list_user_mcp_servers(self, owner_user_id: str) -> list[UserMCPServer]:
        rows = self._session.scalars(
            select(UserMCPServerRow)
            .where(
                UserMCPServerRow.owner_user_id == owner_user_id,
                UserMCPServerRow.deletion_pending.is_(False),
            )
            .order_by(UserMCPServerRow.created_at, UserMCPServerRow.server_id)
        ).all()
        return [_row_to_user_mcp_server(row) for row in rows]

    def get_user_mcp_server(self, owner_user_id: str, server_id: str) -> UserMCPServer | None:
        row = self._get_user_mcp_server_row(owner_user_id, server_id)
        return None if row is None else _row_to_user_mcp_server(row)

    def _get_user_mcp_server_row(
        self, owner_user_id: str, server_id: str, *, include_deleted: bool = False
    ) -> UserMCPServerRow | None:
        conditions = [
            UserMCPServerRow.owner_user_id == owner_user_id,
            UserMCPServerRow.server_id == server_id,
        ]
        if not include_deleted:
            conditions.append(UserMCPServerRow.deletion_pending.is_(False))
        return self._session.scalar(select(UserMCPServerRow).where(*conditions))

    def create_user_mcp_server(
        self, server: UserMCPServer, credential: UserMCPCredentialRecord | None = None
    ) -> UserMCPServer:
        if credential is not None and (
            credential.owner_user_id != server.owner_user_id or credential.server_id != server.server_id
        ):
            raise ValueError("credential scope does not match MCP server")
        row = UserMCPServerRow(
            server_id=server.server_id,
            owner_user_id=server.owner_user_id,
            display_name=server.display_name,
            routing_description=server.routing_description,
            endpoint_url=server.endpoint_url,
            transport=str(server.transport),
            protocol_preference=str(server.protocol_preference),
            auth_type=str(server.auth_type),
            auth_metadata=dict(server.auth_metadata),
            enabled=server.enabled,
            health_status=str(server.health_status),
            config_version=max(1, int(server.config_version)),
            security_version=max(1, int(server.security_version)),
            last_tested_at=server.last_tested_at,
            last_test_error_code=server.last_test_error_code,
            deletion_pending=False,
            deleted_at=None,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )
        if credential is not None:
            self._replace_user_mcp_credential(row, credential)
        self._session.add(row)
        self._session.flush()
        return _row_to_user_mcp_server(row)

    def create_user_mcp_servers_atomic(
        self,
        candidates: Sequence[tuple[UserMCPServer, UserMCPCredentialRecord | None]],
    ) -> list[UserMCPServer]:
        batch = tuple(candidates)
        identities: set[tuple[str, str]] = set()
        for server, credential in batch:
            identity = (server.owner_user_id, server.server_id)
            if identity in identities:
                raise ValueError("duplicate MCP server identity in atomic create batch")
            identities.add(identity)
            if credential is not None and (
                credential.owner_user_id != server.owner_user_id
                or credential.server_id != server.server_id
            ):
                raise ValueError("credential scope does not match MCP server")

        existing_by_id: dict[str, UserMCPServerRow] = {}
        for server, credential in batch:
            existing = self._session.get(UserMCPServerRow, server.server_id)
            if existing is None:
                continue
            self._validate_user_mcp_server_atomic_replay(existing, server, credential)
            existing_by_id[server.server_id] = existing

        insert_statement = (
            postgresql_insert(UserMCPServerRow)
            if self._session.bind is not None
            and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(UserMCPServerRow)
        )
        for server, credential in batch:
            if server.server_id in existing_by_id:
                continue
            self._session.execute(
                insert_statement.values(
                    **self._user_mcp_server_insert_values(server, credential)
                ).on_conflict_do_nothing()
            )
        self._session.flush()
        self._session.expire_all()

        stored: list[UserMCPServer] = []
        for server, credential in batch:
            row = self._session.get(UserMCPServerRow, server.server_id)
            if row is None:
                raise RuntimeError("atomic MCP server create did not persist candidate")
            self._validate_user_mcp_server_atomic_replay(row, server, credential)
            stored.append(_row_to_user_mcp_server(row))
        return stored

    def get_mcp_legacy_migration_record(
        self, migration_id: str
    ) -> MCPLegacyMigrationRecord | None:
        row = self._session.get(MCPLegacyMigrationRecordRow, migration_id)
        return None if row is None else _row_to_mcp_legacy_migration_record(row)

    def apply_legacy_mcp_migration_atomic(
        self,
        candidates: Sequence[
            tuple[
                UserMCPServer,
                UserMCPCredentialRecord | None,
                MCPLegacyMigrationRecord,
            ]
        ],
    ) -> MCPLegacyMigrationBatchResult:
        batch = tuple(candidates)
        migration_ids: set[str] = set()
        plan_sources: set[tuple[str, str]] = set()
        target_server_ids: set[str] = set()
        server_candidates: list[
            tuple[UserMCPServer, UserMCPCredentialRecord | None]
        ] = []
        records: list[MCPLegacyMigrationRecord] = []
        for server, credential, record in batch:
            self._validate_mcp_legacy_migration_record(record)
            if record.target_server_id != server.server_id:
                raise ValueError("migration record target does not match MCP server")
            plan_source = (record.plan_fingerprint, record.source_server_id)
            if (
                record.migration_id in migration_ids
                or plan_source in plan_sources
                or record.target_server_id in target_server_ids
            ):
                raise ValueError("duplicate legacy MCP migration candidate")
            migration_ids.add(record.migration_id)
            plan_sources.add(plan_source)
            target_server_ids.add(record.target_server_id)
            server_candidates.append((server, credential))
            records.append(record)

        missing_server = any(
            self._session.get(UserMCPServerRow, server.server_id) is None
            for server, _credential in server_candidates
        )
        missing_record = any(
            self._session.get(MCPLegacyMigrationRecordRow, record.migration_id)
            is None
            for record in records
        )
        servers = tuple(self.create_user_mcp_servers_atomic(server_candidates))
        stored_records = tuple(
            self._persist_mcp_legacy_migration_records_atomic(records)
        )
        return MCPLegacyMigrationBatchResult(
            servers=servers,
            records=stored_records,
            applied=missing_server or missing_record,
        )

    def _persist_mcp_legacy_migration_records_atomic(
        self, records: Sequence[MCPLegacyMigrationRecord]
    ) -> list[MCPLegacyMigrationRecord]:
        insert_statement = (
            postgresql_insert(MCPLegacyMigrationRecordRow)
            if self._session.bind is not None
            and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(MCPLegacyMigrationRecordRow)
        )
        for record in records:
            existing = self._find_mcp_legacy_migration_record(record)
            if existing is not None:
                self._validate_mcp_legacy_migration_replay(existing, record)
                continue
            self._session.execute(
                insert_statement.values(
                    **self._mcp_legacy_migration_record_values(record)
                ).on_conflict_do_nothing()
            )
        self._session.flush()
        self._session.expire_all()

        stored: list[MCPLegacyMigrationRecord] = []
        for record in records:
            row = self._find_mcp_legacy_migration_record(record)
            if row is None:
                raise RuntimeError(
                    "atomic legacy MCP migration did not persist audit record"
                )
            self._validate_mcp_legacy_migration_replay(row, record)
            stored.append(_row_to_mcp_legacy_migration_record(row))
        return stored

    def _find_mcp_legacy_migration_record(
        self, record: MCPLegacyMigrationRecord
    ) -> MCPLegacyMigrationRecordRow | None:
        by_id = self._session.get(MCPLegacyMigrationRecordRow, record.migration_id)
        by_plan_source = self._session.scalar(
            select(MCPLegacyMigrationRecordRow).where(
                MCPLegacyMigrationRecordRow.plan_fingerprint
                == record.plan_fingerprint,
                MCPLegacyMigrationRecordRow.source_server_id
                == record.source_server_id,
            )
        )
        by_target = self._session.scalar(
            select(MCPLegacyMigrationRecordRow).where(
                MCPLegacyMigrationRecordRow.target_server_id
                == record.target_server_id
            )
        )
        existing = by_id or by_plan_source or by_target
        if any(value is not None and value is not existing for value in (
            by_id,
            by_plan_source,
            by_target,
        )):
            raise ValueError("legacy MCP migration identity conflicts")
        return existing

    @staticmethod
    def _validate_mcp_legacy_migration_record(
        record: MCPLegacyMigrationRecord,
    ) -> None:
        if record.event_type != "mcp.legacy.config_migrated":
            raise ValueError("legacy MCP migration event type is invalid")
        if record.disposition != "migrate_owner":
            raise ValueError("legacy MCP migration disposition is invalid")
        if not record.source_server_id.strip() or not record.target_server_id.strip():
            raise ValueError("legacy MCP migration server identity is invalid")
        sha_values = (
            record.migration_id,
            record.plan_fingerprint,
            record.source_fingerprint,
            record.target_consumer_set_digest,
            record.capability_obligations_fingerprint,
            record.catalog_fingerprint,
            record.capability_fingerprint,
            record.validator_provenance_fingerprint,
        )
        if any(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None for value in sha_values):
            raise ValueError("legacy MCP migration fingerprint is invalid")
        hmac_values = (record.owner_consumer_ref, record.credential_digest)
        if any(
            re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", value) is None
            for value in hmac_values
        ):
            raise ValueError("legacy MCP migration safe reference is invalid")
        if record.occurred_at >= record.evidence_expires_at:
            raise ValueError("legacy MCP migration evidence window is invalid")

    @staticmethod
    def _mcp_legacy_migration_record_values(
        record: MCPLegacyMigrationRecord,
    ) -> dict[str, object]:
        return {
            "migration_id": record.migration_id,
            "event_type": record.event_type,
            "plan_fingerprint": record.plan_fingerprint,
            "source_server_id": record.source_server_id,
            "source_fingerprint": record.source_fingerprint,
            "owner_consumer_ref": record.owner_consumer_ref,
            "target_server_id": record.target_server_id,
            "target_consumer_set_digest": record.target_consumer_set_digest,
            "capability_obligations_fingerprint": (
                record.capability_obligations_fingerprint
            ),
            "catalog_fingerprint": record.catalog_fingerprint,
            "capability_fingerprint": record.capability_fingerprint,
            "validator_provenance_fingerprint": (
                record.validator_provenance_fingerprint
            ),
            "credential_digest": record.credential_digest,
            "disposition": record.disposition,
            "occurred_at": record.occurred_at,
            "evidence_expires_at": record.evidence_expires_at,
        }

    @classmethod
    def _validate_mcp_legacy_migration_replay(
        cls,
        row: MCPLegacyMigrationRecordRow,
        record: MCPLegacyMigrationRecord,
    ) -> None:
        if cls._mcp_legacy_migration_record_values(
            _row_to_mcp_legacy_migration_record(row)
        ) != cls._mcp_legacy_migration_record_values(record):
            raise ValueError("legacy MCP migration record conflicts")

    @staticmethod
    def _user_mcp_server_insert_values(
        server: UserMCPServer,
        credential: UserMCPCredentialRecord | None,
    ) -> dict[str, object | None]:
        return {
            "server_id": server.server_id,
            "owner_user_id": server.owner_user_id,
            "display_name": server.display_name,
            "routing_description": server.routing_description,
            "endpoint_url": server.endpoint_url,
            "transport": str(server.transport),
            "protocol_preference": str(server.protocol_preference),
            "auth_type": str(server.auth_type),
            "auth_metadata": dict(server.auth_metadata),
            "enabled": server.enabled,
            "health_status": str(server.health_status),
            "config_version": max(1, int(server.config_version)),
            "security_version": max(1, int(server.security_version)),
            "credential_ciphertext": (
                None if credential is None else credential.credential_ciphertext
            ),
            "credential_nonce": None if credential is None else credential.credential_nonce,
            "encryption_version": (
                None if credential is None else credential.encryption_version
            ),
            "credential_updated_at": (
                None if credential is None else credential.credential_updated_at
            ),
            "last_tested_at": server.last_tested_at,
            "last_test_error_code": server.last_test_error_code,
            "deletion_pending": False,
            "deleted_at": None,
            "created_at": server.created_at,
            "updated_at": server.updated_at,
        }

    @classmethod
    def _validate_user_mcp_server_atomic_replay(
        cls,
        row: UserMCPServerRow,
        server: UserMCPServer,
        credential: UserMCPCredentialRecord | None,
    ) -> None:
        expected = cls._user_mcp_server_insert_values(server, credential)
        actual = {
            key: getattr(row, key)
            for key in expected
        }
        if actual != expected:
            raise ValueError(
                f"MCP server {server.server_id!r} conflicts with existing record"
            )

    def update_user_mcp_server(
        self,
        owner_user_id: str,
        server_id: str,
        *,
        changes: Mapping[str, Any],
        credential_operation: str,
        credential: UserMCPCredentialRecord | None,
        security_sensitive: bool,
        expected_config_version: int | None = None,
        expected_security_version: int | None = None,
        updated_at: datetime,
    ) -> UserMCPServer | None:
        if credential_operation not in {"retain", "replace", "clear"}:
            raise ValueError("credential_operation must be retain, replace, or clear")
        if credential_operation == "replace":
            if credential is None or credential.owner_user_id != owner_user_id or credential.server_id != server_id:
                raise ValueError("replacement credential must match MCP server scope")
        elif credential is not None:
            raise ValueError("credential is only valid for replace operation")
        allowed = {
            "display_name", "routing_description", "endpoint_url", "transport", "protocol_preference",
            "auth_type", "auth_metadata", "enabled", "health_status", "last_tested_at", "last_test_error_code",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported MCP server fields: {', '.join(sorted(unknown))}")
        values = dict(changes)
        for enum_field in ("transport", "protocol_preference", "auth_type", "health_status"):
            if enum_field in values:
                values[enum_field] = str(values[enum_field])
        if "auth_metadata" in values:
            values["auth_metadata"] = dict(values["auth_metadata"] or {})
        values["updated_at"] = updated_at
        values["config_version"] = UserMCPServerRow.config_version + 1
        credential_changes_security = credential_operation in {"replace", "clear"}
        security_fields = {
            "endpoint_url", "transport", "protocol_preference", "auth_type", "auth_metadata", "enabled",
        }
        invalidates_grants = bool(
            security_sensitive or credential_changes_security or security_fields.intersection(changes)
        )
        if invalidates_grants:
            values["security_version"] = UserMCPServerRow.security_version + 1
        if credential_operation == "replace":
            assert credential is not None
            values.update(
                credential_ciphertext=credential.credential_ciphertext,
                credential_nonce=credential.credential_nonce,
                encryption_version=credential.encryption_version,
                credential_updated_at=credential.credential_updated_at or updated_at,
            )
        elif credential_operation == "clear":
            values.update(
                credential_ciphertext=None,
                credential_nonce=None,
                encryption_version=None,
                credential_updated_at=updated_at,
            )
        conditions = [
            UserMCPServerRow.owner_user_id == owner_user_id,
            UserMCPServerRow.server_id == server_id,
            UserMCPServerRow.deletion_pending.is_(False),
        ]
        if expected_config_version is not None:
            conditions.append(
                UserMCPServerRow.config_version == expected_config_version
            )
        if expected_security_version is not None:
            conditions.append(
                UserMCPServerRow.security_version == expected_security_version
            )
        result = self._session.execute(
            update(UserMCPServerRow).where(*conditions).values(**values)
        )
        if not result.rowcount:
            return None
        if invalidates_grants:
            self._session.execute(
                update(UserMCPToolGrantRow)
                .where(
                    UserMCPToolGrantRow.owner_user_id == owner_user_id,
                    UserMCPToolGrantRow.server_id == server_id,
                    UserMCPToolGrantRow.invalidated_at.is_(None),
                )
                .values(invalidated_at=updated_at, invalid_reason="security_changed")
            )
        self._session.flush()
        row = self._get_user_mcp_server_row(owner_user_id, server_id)
        return None if row is None else _row_to_user_mcp_server(row)

    @staticmethod
    def _replace_user_mcp_credential(row: UserMCPServerRow, credential: UserMCPCredentialRecord) -> None:
        row.credential_ciphertext = credential.credential_ciphertext
        row.credential_nonce = credential.credential_nonce
        row.encryption_version = credential.encryption_version
        row.credential_updated_at = credential.credential_updated_at

    def get_user_mcp_credential(
        self, owner_user_id: str, server_id: str
    ) -> UserMCPCredentialRecord | None:
        row = self._get_user_mcp_server_row(owner_user_id, server_id)
        return None if row is None else _row_to_user_mcp_credential(row)

    def claim_user_mcp_health_attempt(self, attempt: UserMCPHealthAttempt) -> bool:
        server = self._get_user_mcp_server_row(attempt.owner_user_id, attempt.server_id)
        if (
            server is None
            or int(server.config_version) != attempt.config_version
            or int(server.security_version) != attempt.security_version
        ):
            return False
        claim_at = attempt.updated_at or attempt.created_at or _utcnow_naive()
        self._session.execute(
            delete(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.owner_user_id == attempt.owner_user_id,
                UserMCPHealthAttemptRow.server_id == attempt.server_id,
                or_(
                    UserMCPHealthAttemptRow.lease_expires_at <= claim_at,
                    UserMCPHealthAttemptRow.config_version != attempt.config_version,
                    UserMCPHealthAttemptRow.security_version != attempt.security_version,
                ),
            )
        )
        values = {
            "attempt_id": attempt.attempt_id,
            "owner_user_id": attempt.owner_user_id,
            "server_id": attempt.server_id,
            "config_version": attempt.config_version,
            "security_version": attempt.security_version,
            "runner_instance_id": attempt.runner_instance_id,
            "lease_expires_at": attempt.lease_expires_at,
            "created_at": attempt.created_at,
            "updated_at": attempt.updated_at,
        }
        dialect_name = self._session.get_bind().dialect.name
        statement = postgresql_insert(UserMCPHealthAttemptRow).values(**values) if dialect_name == "postgresql" else sqlite_insert(UserMCPHealthAttemptRow).values(**values)
        result = self._session.execute(
            statement.on_conflict_do_nothing(index_elements=["owner_user_id", "server_id"])
        )
        if not result.rowcount:
            return False
        server.health_status = str(UserMCPHealthStatus.TESTING)
        server.last_test_error_code = None
        server.updated_at = attempt.updated_at
        self._session.flush()
        return True

    def renew_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        result = self._session.execute(
            update(UserMCPHealthAttemptRow)
            .where(
                UserMCPHealthAttemptRow.attempt_id == attempt_id,
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.runner_instance_id == runner_instance_id,
                UserMCPHealthAttemptRow.config_version == config_version,
                UserMCPHealthAttemptRow.security_version == security_version,
                UserMCPHealthAttemptRow.lease_expires_at > updated_at,
                select(UserMCPServerRow.server_id).where(
                    UserMCPServerRow.owner_user_id == owner_user_id,
                    UserMCPServerRow.server_id == server_id,
                    UserMCPServerRow.config_version == config_version,
                    UserMCPServerRow.security_version == security_version,
                    UserMCPServerRow.deletion_pending.is_(False),
                ).exists(),
            )
            .values(lease_expires_at=lease_expires_at, updated_at=updated_at)
        )
        return bool(result.rowcount)

    def complete_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, health_status: str, error_code: str | None,
        completed_at: datetime
    ) -> UserMCPServer | None:
        attempt = self._session.scalar(
            select(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.attempt_id == attempt_id,
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.runner_instance_id == runner_instance_id,
                UserMCPHealthAttemptRow.config_version == config_version,
                UserMCPHealthAttemptRow.security_version == security_version,
                UserMCPHealthAttemptRow.lease_expires_at > completed_at,
            )
        )
        server = self._get_user_mcp_server_row(owner_user_id, server_id)
        if (
            attempt is None or server is None or int(server.config_version) != config_version
            or int(server.security_version) != security_version
        ):
            return None
        server.health_status = str(UserMCPHealthStatus(health_status))
        server.last_tested_at = completed_at
        server.last_test_error_code = error_code
        server.updated_at = completed_at
        self._session.delete(attempt)
        self._session.flush()
        return _row_to_user_mcp_server(server)

    def expire_user_mcp_health_attempts(self, *, now: datetime, error_code: str) -> int:
        attempts = self._session.scalars(
            select(UserMCPHealthAttemptRow).where(UserMCPHealthAttemptRow.lease_expires_at <= now)
        ).all()
        for attempt in attempts:
            server = self._get_user_mcp_server_row(attempt.owner_user_id, attempt.server_id)
            if (
                server is not None
                and int(server.config_version) == int(attempt.config_version)
                and int(server.security_version) == int(attempt.security_version)
                and server.health_status == str(UserMCPHealthStatus.TESTING)
            ):
                server.health_status = str(UserMCPHealthStatus.UNAVAILABLE)
                server.last_tested_at = now
                server.last_test_error_code = error_code
                server.updated_at = now
            self._session.delete(attempt)
        self._session.flush()
        return len(attempts)

    def release_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
    ) -> bool:
        result = self._session.execute(
            delete(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.attempt_id == attempt_id,
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.runner_instance_id == runner_instance_id,
                UserMCPHealthAttemptRow.config_version == config_version,
                UserMCPHealthAttemptRow.security_version == security_version,
            )
        )
        return bool(result.rowcount)

    def acquire_user_mcp_scope_lease(self, lease: UserMCPScopeLease) -> bool:
        server = self._get_user_mcp_server_row(lease.owner_user_id, lease.server_id)
        if (
            server is None or not server.enabled
            or server.health_status != str(UserMCPHealthStatus.AVAILABLE)
            or int(server.security_version) != lease.security_version
        ):
            return False
        if self._session.get(UserMCPScopeLeaseRow, lease.scope_id) is not None:
            return False
        self._session.add(
            UserMCPScopeLeaseRow(
                scope_id=lease.scope_id,
                owner_user_id=lease.owner_user_id,
                server_id=lease.server_id,
                security_version=lease.security_version,
                gateway_instance_id=lease.gateway_instance_id,
                lease_expires_at=lease.lease_expires_at,
                created_at=lease.created_at,
                updated_at=lease.updated_at,
            )
        )
        self._session.flush()
        return True

    def renew_user_mcp_scope_lease(
        self, scope_id: str, owner_user_id: str, server_id: str, *, gateway_instance_id: str,
        security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        result = self._session.execute(
            update(UserMCPScopeLeaseRow)
            .where(
                UserMCPScopeLeaseRow.scope_id == scope_id,
                UserMCPScopeLeaseRow.owner_user_id == owner_user_id,
                UserMCPScopeLeaseRow.server_id == server_id,
                UserMCPScopeLeaseRow.gateway_instance_id == gateway_instance_id,
                UserMCPScopeLeaseRow.security_version == security_version,
                UserMCPScopeLeaseRow.lease_expires_at > updated_at,
                select(UserMCPServerRow.server_id).where(
                    UserMCPServerRow.owner_user_id == owner_user_id,
                    UserMCPServerRow.server_id == server_id,
                    UserMCPServerRow.security_version == security_version,
                    UserMCPServerRow.deletion_pending.is_(False),
                    UserMCPServerRow.enabled.is_(True),
                ).exists(),
            )
            .values(lease_expires_at=lease_expires_at, updated_at=updated_at)
        )
        return bool(result.rowcount)

    def release_user_mcp_scope_lease(self, scope_id: str, *, gateway_instance_id: str) -> bool:
        result = self._session.execute(
            delete(UserMCPScopeLeaseRow).where(
                UserMCPScopeLeaseRow.scope_id == scope_id,
                UserMCPScopeLeaseRow.gateway_instance_id == gateway_instance_id,
            )
        )
        return bool(result.rowcount)

    def list_live_user_mcp_scope_leases(
        self, *, now: datetime, owner_user_id: str | None, server_id: str | None
    ) -> list[UserMCPScopeLease]:
        statement = select(UserMCPScopeLeaseRow).where(UserMCPScopeLeaseRow.lease_expires_at > now)
        if owner_user_id is not None:
            statement = statement.where(UserMCPScopeLeaseRow.owner_user_id == owner_user_id)
        if server_id is not None:
            statement = statement.where(UserMCPScopeLeaseRow.server_id == server_id)
        rows = self._session.scalars(statement.order_by(UserMCPScopeLeaseRow.lease_expires_at)).all()
        return [_row_to_user_mcp_scope_lease(row) for row in rows]

    def expire_user_mcp_scope_leases(self, *, now: datetime) -> int:
        result = self._session.execute(
            delete(UserMCPScopeLeaseRow).where(UserMCPScopeLeaseRow.lease_expires_at <= now)
        )
        return int(result.rowcount or 0)

    def mark_user_mcp_server_deleted(
        self, owner_user_id: str, server_id: str, *, deleted_at: datetime
    ) -> UserMCPServer | None:
        result = self._session.execute(
            update(UserMCPServerRow)
            .where(
                UserMCPServerRow.owner_user_id == owner_user_id,
                UserMCPServerRow.server_id == server_id,
                UserMCPServerRow.deletion_pending.is_(False),
            )
            .values(
                deletion_pending=True,
                deleted_at=deleted_at,
                enabled=False,
                health_status=str(UserMCPHealthStatus.DISABLED),
                config_version=UserMCPServerRow.config_version + 1,
                security_version=UserMCPServerRow.security_version + 1,
                updated_at=deleted_at,
            )
        )
        if not result.rowcount:
            return None
        row = self._get_user_mcp_server_row(owner_user_id, server_id, include_deleted=True)
        return None if row is None else _row_to_user_mcp_server(row)

    def list_pending_user_mcp_server_deletions(self) -> list[UserMCPServer]:
        rows = self._session.scalars(
            select(UserMCPServerRow)
            .where(UserMCPServerRow.deletion_pending.is_(True))
            .order_by(UserMCPServerRow.deleted_at, UserMCPServerRow.server_id)
        ).all()
        return [_row_to_user_mcp_server(row) for row in rows]

    def finalize_user_mcp_server_delete(
        self, owner_user_id: str, server_id: str, *, now: datetime
    ) -> bool:
        server = self._get_user_mcp_server_row(owner_user_id, server_id, include_deleted=True)
        if server is None or not server.deletion_pending:
            return False
        live_health = self._session.scalar(
            select(UserMCPHealthAttemptRow.attempt_id).where(
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.lease_expires_at > now,
            ).limit(1)
        )
        live_scope = self._session.scalar(
            select(UserMCPScopeLeaseRow.scope_id).where(
                UserMCPScopeLeaseRow.owner_user_id == owner_user_id,
                UserMCPScopeLeaseRow.server_id == server_id,
                UserMCPScopeLeaseRow.lease_expires_at > now,
            ).limit(1)
        )
        if live_health is not None or live_scope is not None:
            return False
        self._session.execute(
            delete(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
            )
        )
        self._session.execute(
            delete(UserMCPScopeLeaseRow).where(
                UserMCPScopeLeaseRow.owner_user_id == owner_user_id,
                UserMCPScopeLeaseRow.server_id == server_id,
            )
        )
        self._session.execute(
            delete(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.server_id == server_id,
            )
        )
        self._session.delete(server)
        self._session.flush()
        return True

    def save_user_mcp_tool_grant(self, grant: UserMCPToolGrant) -> UserMCPToolGrant:
        server = self._get_user_mcp_server_row(grant.owner_user_id, grant.server_id)
        if server is None:
            raise ValueError("MCP server not found")
        existing = self._session.get(UserMCPToolGrantRow, grant.grant_id)
        if existing is not None:
            if (
                existing.owner_user_id != grant.owner_user_id
                or existing.server_id != grant.server_id
                or existing.tool_name != grant.tool_name
                or int(existing.server_security_version) != grant.server_security_version
                or existing.input_schema_sha256 != grant.input_schema_sha256
            ):
                raise ValueError("MCP tool grant scope does not match existing grant")
            return _row_to_user_mcp_tool_grant(existing)
        row = UserMCPToolGrantRow(
            grant_id=grant.grant_id,
            owner_user_id=grant.owner_user_id,
            server_id=grant.server_id,
            tool_name=grant.tool_name,
            server_security_version=grant.server_security_version,
            input_schema_sha256=grant.input_schema_sha256,
            granted_at=grant.granted_at,
            invalidated_at=grant.invalidated_at,
            invalid_reason=grant.invalid_reason,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_user_mcp_tool_grant(merged)

    def list_user_mcp_tool_grants(
        self, owner_user_id: str, server_id: str | None = None
    ) -> list[UserMCPToolGrant]:
        conditions = [UserMCPToolGrantRow.owner_user_id == owner_user_id]
        if server_id is not None:
            conditions.append(UserMCPToolGrantRow.server_id == server_id)
        rows = self._session.scalars(
            select(UserMCPToolGrantRow)
            .where(*conditions)
            .order_by(UserMCPToolGrantRow.server_id, UserMCPToolGrantRow.tool_name, UserMCPToolGrantRow.grant_id)
        ).all()
        return [_row_to_user_mcp_tool_grant(row) for row in rows]

    def get_valid_user_mcp_tool_grant(
        self,
        owner_user_id: str,
        server_id: str,
        tool_name: str,
        *,
        server_security_version: int,
        input_schema_sha256: str,
    ) -> UserMCPToolGrant | None:
        row = self._session.scalar(
            select(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.server_id == server_id,
                UserMCPToolGrantRow.tool_name == tool_name,
                UserMCPToolGrantRow.server_security_version == server_security_version,
                UserMCPToolGrantRow.input_schema_sha256 == input_schema_sha256,
                UserMCPToolGrantRow.invalidated_at.is_(None),
            )
        )
        return None if row is None else _row_to_user_mcp_tool_grant(row)

    def delete_user_mcp_tool_grant(self, owner_user_id: str, server_id: str, grant_id: str) -> bool:
        result = self._session.execute(
            delete(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.server_id == server_id,
                UserMCPToolGrantRow.grant_id == grant_id,
            )
        )
        return bool(result.rowcount)

    def delete_user_mcp_tool_grant_by_id(self, owner_user_id: str, grant_id: str) -> bool:
        result = self._session.execute(
            delete(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.grant_id == grant_id,
            )
        )
        return bool(result.rowcount)

    def clear_user_mcp_tool_grants(self, owner_user_id: str, server_id: str) -> int:
        result = self._session.execute(
            delete(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.server_id == server_id,
            )
        )
        return int(result.rowcount or 0)

    def invalidate_user_mcp_tool_grants(
        self,
        owner_user_id: str,
        server_id: str,
        *,
        invalidated_at: datetime,
        invalid_reason: str,
        tool_name: str | None = None,
        input_schema_sha256: str | None = None,
    ) -> int:
        conditions = [
            UserMCPToolGrantRow.owner_user_id == owner_user_id,
            UserMCPToolGrantRow.server_id == server_id,
            UserMCPToolGrantRow.invalidated_at.is_(None),
        ]
        if tool_name is not None:
            conditions.append(UserMCPToolGrantRow.tool_name == tool_name)
        if input_schema_sha256 is not None:
            conditions.append(UserMCPToolGrantRow.input_schema_sha256 == input_schema_sha256)
        result = self._session.execute(
            update(UserMCPToolGrantRow)
            .where(*conditions)
            .values(invalidated_at=invalidated_at, invalid_reason=invalid_reason)
        )
        return int(result.rowcount or 0)

    def save_mcp_branch_record(self, record: MCPBranchRecord) -> MCPBranchRecord:
        existing = self._session.get(MCPBranchRecordRow, record.branch_id)
        if existing is not None and (
            existing.owner_user_id != record.owner_user_id or existing.task_id != record.task_id
        ):
            raise ValueError("MCP branch scope does not match existing record")
        if (
            existing is not None
            and existing.terminal_at is not None
            and record.terminal_at is None
        ):
            # A delayed waiting publication must never resurrect a branch that
            # the recovery transaction has already converged terminal.
            return _row_to_mcp_branch(existing)
        row = MCPBranchRecordRow(
            branch_id=record.branch_id,
            owner_user_id=record.owner_user_id,
            task_id=record.task_id,
            node_id=record.node_id,
            status=record.status,
            initial_server_id=record.initial_server_id,
            tool_call_count=record.tool_call_count,
            max_tool_calls=record.max_tool_calls,
            active_call_ref=record.active_call_ref,
            result_ref=record.result_ref,
            safe_summary=record.safe_summary,
            created_at=record.created_at,
            updated_at=record.updated_at,
            terminal_at=record.terminal_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_mcp_branch(merged)

    def get_mcp_branch_record(
        self, owner_user_id: str, task_id: str, branch_id: str
    ) -> MCPBranchRecord | None:
        row = self._session.scalar(
            select(MCPBranchRecordRow).where(
                MCPBranchRecordRow.branch_id == branch_id,
                MCPBranchRecordRow.owner_user_id == owner_user_id,
                MCPBranchRecordRow.task_id == task_id,
            )
        )
        return None if row is None else _row_to_mcp_branch(row)

    def list_mcp_branch_records(
        self,
        owner_user_id: str,
        *,
        task_id: str | None = None,
        statuses: tuple[str, ...] = (),
    ) -> list[MCPBranchRecord]:
        conditions = [MCPBranchRecordRow.owner_user_id == owner_user_id]
        if task_id is not None:
            conditions.append(MCPBranchRecordRow.task_id == task_id)
        if statuses:
            conditions.append(MCPBranchRecordRow.status.in_(statuses))
        rows = self._session.scalars(
            select(MCPBranchRecordRow)
            .where(*conditions)
            .order_by(MCPBranchRecordRow.created_at, MCPBranchRecordRow.branch_id)
        ).all()
        return [_row_to_mcp_branch(row) for row in rows]

    def reserve_mcp_call(self, record: MCPCallRecord) -> bool:
        branch = self._session.scalar(
            select(MCPBranchRecordRow).where(
                MCPBranchRecordRow.branch_id == record.branch_id,
                MCPBranchRecordRow.owner_user_id == record.owner_user_id,
                MCPBranchRecordRow.task_id == record.task_id,
                MCPBranchRecordRow.node_id == record.node_id,
            )
        )
        if branch is None or branch.active_call_ref is not None:
            return False
        next_sequence = int(branch.tool_call_count) + 1
        if next_sequence > int(branch.max_tool_calls) or record.call_sequence != next_sequence:
            return False
        if self._session.get(MCPCallRecordRow, record.call_ref) is not None:
            return False
        claimed = self._session.execute(
            update(MCPBranchRecordRow)
            .where(
                MCPBranchRecordRow.branch_id == record.branch_id,
                MCPBranchRecordRow.owner_user_id == record.owner_user_id,
                MCPBranchRecordRow.task_id == record.task_id,
                MCPBranchRecordRow.active_call_ref.is_(None),
                MCPBranchRecordRow.tool_call_count == branch.tool_call_count,
            )
            .values(
                tool_call_count=next_sequence,
                active_call_ref=record.call_ref,
                status="active",
                updated_at=record.updated_at,
            )
        )
        if not claimed.rowcount:
            return False
        self._session.add(
            MCPCallRecordRow(
                call_ref=record.call_ref,
                branch_id=record.branch_id,
                owner_user_id=record.owner_user_id,
                task_id=record.task_id,
                node_id=record.node_id,
                server_id=record.server_id,
                tool_name=record.tool_name,
                status=record.status,
                call_sequence=record.call_sequence,
                arguments_sha256=record.arguments_sha256,
                server_security_version=record.server_security_version,
                input_schema_sha256=record.input_schema_sha256,
                protocol_version=record.protocol_version,
                input_field_names=list(record.input_field_names),
                may_have_dispatched=record.may_have_dispatched,
                result_ref=record.result_ref,
                output_size_bytes=record.output_size_bytes,
                safe_error_code=record.safe_error_code,
                created_at=record.created_at,
                updated_at=record.updated_at,
                terminal_at=record.terminal_at,
            )
        )
        self._session.flush()
        return True

    def mark_mcp_call_may_have_dispatched(
        self, owner_user_id: str, task_id: str, call_ref: str, *, updated_at: datetime
    ) -> bool:
        result = self._session.execute(
            update(MCPCallRecordRow)
            .where(
                MCPCallRecordRow.call_ref == call_ref,
                MCPCallRecordRow.owner_user_id == owner_user_id,
                MCPCallRecordRow.task_id == task_id,
                MCPCallRecordRow.terminal_at.is_(None),
            )
            .values(may_have_dispatched=True, status="active", updated_at=updated_at)
        )
        return bool(result.rowcount)

    def get_mcp_call_record(
        self, owner_user_id: str, task_id: str, call_ref: str
    ) -> MCPCallRecord | None:
        row = self._session.scalar(
            select(MCPCallRecordRow).where(
                MCPCallRecordRow.call_ref == call_ref,
                MCPCallRecordRow.owner_user_id == owner_user_id,
                MCPCallRecordRow.task_id == task_id,
            )
        )
        return None if row is None else _row_to_mcp_call(row)

    def list_mcp_call_records(
        self, owner_user_id: str, task_id: str, *, branch_id: str | None = None
    ) -> list[MCPCallRecord]:
        conditions = [
            MCPCallRecordRow.owner_user_id == owner_user_id,
            MCPCallRecordRow.task_id == task_id,
        ]
        if branch_id is not None:
            conditions.append(MCPCallRecordRow.branch_id == branch_id)
        rows = self._session.scalars(
            select(MCPCallRecordRow)
            .where(*conditions)
            .order_by(MCPCallRecordRow.call_sequence, MCPCallRecordRow.call_ref)
        ).all()
        return [_row_to_mcp_call(row) for row in rows]

    def finish_mcp_call(
        self,
        owner_user_id: str,
        task_id: str,
        call_ref: str,
        *,
        status: str,
        terminal_at: datetime,
        result_ref: str | None = None,
        output_size_bytes: int | None = None,
        safe_error_code: str | None = None,
    ) -> MCPCallRecord | None:
        row = self._session.scalar(
            select(MCPCallRecordRow).where(
                MCPCallRecordRow.call_ref == call_ref,
                MCPCallRecordRow.owner_user_id == owner_user_id,
                MCPCallRecordRow.task_id == task_id,
            )
        )
        if row is None or row.terminal_at is not None:
            return None
        row.status = status
        row.result_ref = result_ref
        row.output_size_bytes = output_size_bytes
        row.safe_error_code = safe_error_code
        row.updated_at = terminal_at
        row.terminal_at = terminal_at
        self._session.execute(
            update(MCPBranchRecordRow)
            .where(
                MCPBranchRecordRow.branch_id == row.branch_id,
                MCPBranchRecordRow.owner_user_id == owner_user_id,
                MCPBranchRecordRow.task_id == task_id,
                MCPBranchRecordRow.active_call_ref == call_ref,
            )
            .values(active_call_ref=None, updated_at=terminal_at)
        )
        self._session.flush()
        return _row_to_mcp_call(row)

    def converge_dispatched_mcp_calls_to_unknown(
        self, *, now: datetime, limit: int = 1000
    ) -> list[MCPCallRecord]:
        terminal_call = select(MCPCallRecordRow.call_ref).where(
            MCPCallRecordRow.call_ref == MCPSealedStateRow.call_ref,
            MCPCallRecordRow.owner_user_id == MCPSealedStateRow.owner_user_id,
            MCPCallRecordRow.task_id == MCPSealedStateRow.task_id,
            MCPCallRecordRow.terminal_at.is_not(None),
        ).exists()
        open_mrtr_interrupt = select(InterruptRow.interrupt_id).where(
            InterruptRow.task_id == MCPSealedStateRow.task_id,
            InterruptRow.node_id == MCPSealedStateRow.node_id,
            InterruptRow.reason_code == "mcp_input_required",
            InterruptRow.status == "open",
        ).exists()
        self._session.execute(
            delete(MCPSealedStateRow).where(terminal_call, ~open_mrtr_interrupt)
        )
        has_remote_binding = select(MCPRemoteTaskBindingRow.safe_remote_task_ref).where(
            MCPRemoteTaskBindingRow.owner_user_id == MCPCallRecordRow.owner_user_id,
            MCPRemoteTaskBindingRow.task_id == MCPCallRecordRow.task_id,
            MCPRemoteTaskBindingRow.call_ref == MCPCallRecordRow.call_ref,
        ).exists()
        candidates = self._session.scalars(
            select(MCPCallRecordRow)
            .where(
                MCPCallRecordRow.may_have_dispatched.is_(True),
                MCPCallRecordRow.terminal_at.is_(None),
                ~has_remote_binding,
            )
            .order_by(MCPCallRecordRow.created_at, MCPCallRecordRow.call_ref)
            .limit(max(1, limit))
        ).all()
        converged_refs: list[str] = []
        for candidate in candidates:
            result = self._session.execute(
                update(MCPCallRecordRow)
                .where(
                    MCPCallRecordRow.call_ref == candidate.call_ref,
                    MCPCallRecordRow.owner_user_id == candidate.owner_user_id,
                    MCPCallRecordRow.task_id == candidate.task_id,
                    MCPCallRecordRow.may_have_dispatched.is_(True),
                    MCPCallRecordRow.terminal_at.is_(None),
                    ~has_remote_binding,
                )
                .values(
                    status="unknown",
                    safe_error_code="execution_status_unknown",
                    updated_at=now,
                    terminal_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                continue
            converged_refs.append(candidate.call_ref)
            self._session.execute(
                update(MCPBranchRecordRow)
                .where(
                    MCPBranchRecordRow.branch_id == candidate.branch_id,
                    MCPBranchRecordRow.owner_user_id == candidate.owner_user_id,
                    MCPBranchRecordRow.task_id == candidate.task_id,
                    MCPBranchRecordRow.active_call_ref == candidate.call_ref,
                )
                .values(active_call_ref=None, updated_at=now)
            )
            self._session.execute(
                delete(MCPSealedStateRow).where(
                    MCPSealedStateRow.owner_user_id == candidate.owner_user_id,
                    MCPSealedStateRow.task_id == candidate.task_id,
                    MCPSealedStateRow.call_ref == candidate.call_ref,
                    ~select(InterruptRow.interrupt_id).where(
                        InterruptRow.task_id == MCPSealedStateRow.task_id,
                        InterruptRow.node_id == MCPSealedStateRow.node_id,
                        InterruptRow.reason_code == "mcp_input_required",
                        InterruptRow.status == "open",
                    ).exists(),
                )
            )
        self._session.flush()
        self._session.expire_all()
        return [
            _row_to_mcp_call(row)
            for ref in converged_refs
            if (row := self._session.get(MCPCallRecordRow, ref)) is not None
        ]

    def count_active_mcp_remote_task_bindings(
        self, *, rollout_config_version: str, protocol_version: str
    ) -> int:
        count = self._session.scalar(
            select(func.count(MCPRemoteTaskBindingRow.safe_remote_task_ref))
            .select_from(MCPRemoteTaskBindingRow)
            .join(
                MCPCallRecordRow,
                (
                    MCPCallRecordRow.owner_user_id
                    == MCPRemoteTaskBindingRow.owner_user_id
                )
                & (MCPCallRecordRow.task_id == MCPRemoteTaskBindingRow.task_id)
                & (MCPCallRecordRow.call_ref == MCPRemoteTaskBindingRow.call_ref)
                & (MCPCallRecordRow.server_id == MCPRemoteTaskBindingRow.server_id)
                & (
                    MCPCallRecordRow.protocol_version
                    == MCPRemoteTaskBindingRow.protocol_version
                ),
            )
            .join(TaskRow, TaskRow.task_id == MCPRemoteTaskBindingRow.task_id)
            .join(
                UserMCPServerRow,
                (UserMCPServerRow.owner_user_id == MCPRemoteTaskBindingRow.owner_user_id)
                & (UserMCPServerRow.server_id == MCPRemoteTaskBindingRow.server_id),
            )
            .where(
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.protocol_version == protocol_version,
                TaskRow.mcp_execution_mode == "user_scoped",
                TaskRow.mcp_rollout_mode == "enforce",
                TaskRow.mcp_rollout_config_version == rollout_config_version,
                MCPCallRecordRow.server_security_version
                == UserMCPServerRow.security_version,
                UserMCPServerRow.deleted_at.is_(None),
                UserMCPServerRow.deletion_pending.is_(False),
            )
        )
        return int(count or 0)

    def list_active_mcp_remote_task_binding_task_ids(
        self,
        *,
        protocol_version: str,
    ) -> list[str]:
        rows = self._session.scalars(
            select(MCPRemoteTaskBindingRow.task_id)
            .select_from(MCPRemoteTaskBindingRow)
            .join(
                MCPCallRecordRow,
                (
                    MCPCallRecordRow.owner_user_id
                    == MCPRemoteTaskBindingRow.owner_user_id
                )
                & (MCPCallRecordRow.task_id == MCPRemoteTaskBindingRow.task_id)
                & (MCPCallRecordRow.call_ref == MCPRemoteTaskBindingRow.call_ref)
                & (MCPCallRecordRow.server_id == MCPRemoteTaskBindingRow.server_id)
                & (
                    MCPCallRecordRow.protocol_version
                    == MCPRemoteTaskBindingRow.protocol_version
                ),
            )
            .join(
                UserMCPServerRow,
                (UserMCPServerRow.owner_user_id == MCPRemoteTaskBindingRow.owner_user_id)
                & (UserMCPServerRow.server_id == MCPRemoteTaskBindingRow.server_id),
            )
            .where(
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.protocol_version == protocol_version,
                MCPCallRecordRow.server_security_version
                == UserMCPServerRow.security_version,
                UserMCPServerRow.deleted_at.is_(None),
                UserMCPServerRow.deletion_pending.is_(False),
            )
        ).all()
        return [str(task_id) for task_id in rows]

    def save_mcp_remote_task_binding(
        self, binding: MCPRemoteTaskBinding
    ) -> MCPRemoteTaskBinding:
        values = {
            "safe_remote_task_ref": binding.safe_remote_task_ref,
            "owner_user_id": binding.owner_user_id,
            "task_id": binding.task_id,
            "node_id": binding.node_id,
            "call_ref": binding.call_ref,
            "server_id": binding.server_id,
            "protocol_version": binding.protocol_version,
            "remote_task_ciphertext": binding.remote_task_ciphertext,
            "remote_task_nonce": binding.remote_task_nonce,
            "encryption_version": binding.encryption_version,
            "last_status": binding.last_status,
            "next_poll_at": binding.next_poll_at,
            "published_at": binding.published_at,
            "continuation_plan": dict(binding.continuation_plan),
            "created_at": binding.created_at,
            "updated_at": binding.updated_at,
            "terminal_at": binding.terminal_at,
            "claim_owner": None,
            "claim_token": None,
            "lease_expires_at": None,
            "revision": 0,
        }
        insert_statement = (
            postgresql_insert(MCPRemoteTaskBindingRow)
            if self._session.bind is not None and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(MCPRemoteTaskBindingRow)
        )
        self._session.execute(insert_statement.values(**values).on_conflict_do_nothing())
        self._session.flush()
        self._session.expire_all()
        existing = self._session.get(MCPRemoteTaskBindingRow, binding.safe_remote_task_ref)
        if existing is None:
            raise RuntimeError("MCP remote task binding insert did not persist")
        immutable_values = (
            existing.owner_user_id,
            existing.task_id,
            existing.node_id,
            existing.call_ref,
            existing.server_id,
            existing.protocol_version,
            existing.remote_task_ciphertext,
            existing.remote_task_nonce,
            int(existing.encryption_version),
        )
        incoming_values = (
            binding.owner_user_id,
            binding.task_id,
            binding.node_id,
            binding.call_ref,
            binding.server_id,
            binding.protocol_version,
            binding.remote_task_ciphertext,
            binding.remote_task_nonce,
            binding.encryption_version,
        )
        if immutable_values != incoming_values:
            raise ValueError("MCP remote task immutable identity or ciphertext does not match existing binding")
        return _row_to_mcp_remote_task(existing)

    def get_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> MCPRemoteTaskBinding | None:
        row = self._session.scalar(
            select(MCPRemoteTaskBindingRow).where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
            )
        )
        return None if row is None else _row_to_mcp_remote_task(row)

    def get_mcp_remote_task_binding_for_call(
        self, owner_user_id: str, task_id: str, call_ref: str
    ) -> MCPRemoteTaskBinding | None:
        row = self._session.scalar(
            select(MCPRemoteTaskBindingRow).where(
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
                MCPRemoteTaskBindingRow.call_ref == call_ref,
            )
        )
        return None if row is None else _row_to_mcp_remote_task(row)

    def publish_mcp_remote_task_binding(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        published_at: datetime,
        continuation_plan: Mapping[str, Any] | None = None,
    ) -> MCPRemoteTaskBinding | None:
        publish_values: dict[str, Any] = {
            "next_poll_at": published_at,
            "published_at": published_at,
            "updated_at": published_at,
        }
        if continuation_plan is not None:
            publish_values["continuation_plan"] = dict(continuation_plan)
        result = self._session.execute(
            update(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.published_at.is_(None),
            )
            .values(**publish_values)
        )
        if not result.rowcount:
            row = self._session.get(MCPRemoteTaskBindingRow, safe_remote_task_ref)
            if row is None or row.published_at is None:
                return None
            return _row_to_mcp_remote_task(row)
        self._session.flush()
        row = self._session.get(MCPRemoteTaskBindingRow, safe_remote_task_ref)
        return None if row is None else _row_to_mcp_remote_task(row)

    def list_unpublished_mcp_remote_task_bindings(
        self, *, limit: int = 1000
    ) -> list[MCPRemoteTaskBinding]:
        rows = self._session.scalars(
            select(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.published_at.is_(None),
            )
            .order_by(MCPRemoteTaskBindingRow.created_at)
            .limit(max(1, limit))
        ).all()
        return [_row_to_mcp_remote_task(row) for row in rows]

    def fail_unpublished_mcp_remote_task_binding(
        self, binding: MCPRemoteTaskBinding, *, terminal_at: datetime
    ) -> MCPRemoteTaskBinding | None:
        claim_owner = "mcp-publication-recovery"
        claim_token = f"publication:{binding.call_ref}"
        revision = int(binding.revision or 0)
        result = self._session.execute(
            update(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == binding.safe_remote_task_ref,
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.published_at.is_(None),
                func.coalesce(MCPRemoteTaskBindingRow.revision, 0) == revision,
            )
            .values(
                claim_owner=claim_owner,
                claim_token=claim_token,
                lease_expires_at=terminal_at + timedelta(seconds=1),
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        return self.finish_mcp_remote_task_binding(
            binding.owner_user_id,
            binding.task_id,
            binding.safe_remote_task_ref,
            claim_owner=claim_owner,
            claim_token=claim_token,
            expected_revision=revision,
            remote_status="unknown",
            call_status="unknown",
            terminal_at=terminal_at,
            safe_error_code="execution_status_unknown",
        )

    def list_due_mcp_remote_task_bindings(
        self, *, now: datetime, limit: int = 100
    ) -> list[MCPRemoteTaskBinding]:
        rows = self._session.scalars(
            select(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.next_poll_at.is_not(None),
                MCPRemoteTaskBindingRow.next_poll_at <= now,
            )
            .order_by(MCPRemoteTaskBindingRow.next_poll_at, MCPRemoteTaskBindingRow.safe_remote_task_ref)
            .limit(max(1, limit))
        ).all()
        return [_row_to_mcp_remote_task(row) for row in rows]

    def claim_due_mcp_remote_task_bindings(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskBinding]:
        if not claim_owner or not claim_token:
            raise ValueError("MCP remote task claim owner and token are required")
        if lease_expires_at <= now:
            raise ValueError("MCP remote task claim lease must expire after claim time")
        candidates = self._session.scalars(
            select(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.next_poll_at.is_not(None),
                MCPRemoteTaskBindingRow.next_poll_at <= now,
                or_(
                    MCPRemoteTaskBindingRow.lease_expires_at.is_(None),
                    MCPRemoteTaskBindingRow.lease_expires_at <= now,
                ),
            )
            .order_by(MCPRemoteTaskBindingRow.next_poll_at, MCPRemoteTaskBindingRow.safe_remote_task_ref)
            .limit(max(1, limit))
        ).all()
        claimed_refs: list[str] = []
        for candidate in candidates:
            revision = 0 if candidate.revision is None else int(candidate.revision)
            result = self._session.execute(
                update(MCPRemoteTaskBindingRow)
                .where(
                    MCPRemoteTaskBindingRow.safe_remote_task_ref == candidate.safe_remote_task_ref,
                    MCPRemoteTaskBindingRow.terminal_at.is_(None),
                    MCPRemoteTaskBindingRow.next_poll_at.is_not(None),
                    MCPRemoteTaskBindingRow.next_poll_at <= now,
                    or_(
                        MCPRemoteTaskBindingRow.lease_expires_at.is_(None),
                        MCPRemoteTaskBindingRow.lease_expires_at <= now,
                    ),
                    func.coalesce(MCPRemoteTaskBindingRow.revision, 0) == revision,
                )
                .values(
                    claim_owner=claim_owner,
                    claim_token=claim_token,
                    lease_expires_at=lease_expires_at,
                    revision=revision + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                claimed_refs.append(candidate.safe_remote_task_ref)
        self._session.flush()
        self._session.expire_all()
        return [
            _row_to_mcp_remote_task(row)
            for ref in claimed_refs
            if (row := self._session.get(MCPRemoteTaskBindingRow, ref)) is not None
        ]

    def renew_mcp_remote_task_binding_claim(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None:
        if lease_expires_at <= updated_at:
            raise ValueError("MCP remote task claim lease must expire after renewal time")
        result = self._session.execute(
            update(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.claim_owner == claim_owner,
                MCPRemoteTaskBindingRow.claim_token == claim_token,
                MCPRemoteTaskBindingRow.lease_expires_at > updated_at,
                func.coalesce(MCPRemoteTaskBindingRow.revision, 0) == expected_revision,
            )
            .values(
                lease_expires_at=lease_expires_at,
                revision=expected_revision + 1,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskBindingRow, safe_remote_task_ref)
        return None if row is None else _row_to_mcp_remote_task(row)

    def release_mcp_remote_task_binding_claim(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None:
        result = self._session.execute(
            update(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.claim_owner == claim_owner,
                MCPRemoteTaskBindingRow.claim_token == claim_token,
                func.coalesce(MCPRemoteTaskBindingRow.revision, 0) == expected_revision,
            )
            .values(
                claim_owner=None,
                claim_token=None,
                lease_expires_at=None,
                revision=expected_revision + 1,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskBindingRow, safe_remote_task_ref)
        return None if row is None else _row_to_mcp_remote_task(row)

    def update_mcp_remote_task_binding_status(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        last_status: str,
        next_poll_at: datetime | None,
        updated_at: datetime,
        terminal_at: datetime | None = None,
    ) -> MCPRemoteTaskBinding | None:
        values: dict[str, object | None] = {
            "last_status": last_status,
            "next_poll_at": None if terminal_at is not None else next_poll_at,
            "updated_at": updated_at,
            "terminal_at": terminal_at,
            "revision": expected_revision + 1,
        }
        if terminal_at is not None:
            values.update(claim_owner=None, claim_token=None, lease_expires_at=None)
        result = self._session.execute(
            update(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.claim_owner == claim_owner,
                MCPRemoteTaskBindingRow.claim_token == claim_token,
                MCPRemoteTaskBindingRow.lease_expires_at > updated_at,
                func.coalesce(MCPRemoteTaskBindingRow.revision, 0) == expected_revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskBindingRow, safe_remote_task_ref)
        return None if row is None else _row_to_mcp_remote_task(row)

    def finish_mcp_remote_task_binding(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        remote_status: str,
        call_status: str,
        terminal_at: datetime,
        result_ref: str | None = None,
        safe_error_code: str | None = None,
    ) -> MCPRemoteTaskBinding | None:
        result = self._session.execute(
            update(MCPRemoteTaskBindingRow)
            .where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
                MCPRemoteTaskBindingRow.claim_owner == claim_owner,
                MCPRemoteTaskBindingRow.claim_token == claim_token,
                MCPRemoteTaskBindingRow.lease_expires_at > terminal_at,
                func.coalesce(MCPRemoteTaskBindingRow.revision, 0) == expected_revision,
            )
            .values(
                last_status=remote_status,
                next_poll_at=None,
                updated_at=terminal_at,
                terminal_at=terminal_at,
                claim_owner=None,
                claim_token=None,
                lease_expires_at=None,
                revision=expected_revision + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        binding_row = self._session.get(MCPRemoteTaskBindingRow, safe_remote_task_ref)
        if binding_row is None:
            raise RuntimeError("MCP remote task binding terminal update did not persist")
        call_row = self._session.scalar(
            select(MCPCallRecordRow).where(
                MCPCallRecordRow.call_ref == binding_row.call_ref,
                MCPCallRecordRow.owner_user_id == owner_user_id,
                MCPCallRecordRow.task_id == task_id,
            )
        )
        if call_row is not None and call_row.terminal_at is None:
            call_row.status = call_status
            call_row.result_ref = result_ref
            call_row.output_size_bytes = None
            call_row.safe_error_code = safe_error_code
            call_row.updated_at = terminal_at
            call_row.terminal_at = terminal_at
            self._session.execute(
                update(MCPBranchRecordRow)
                .where(
                    MCPBranchRecordRow.branch_id == call_row.branch_id,
                    MCPBranchRecordRow.owner_user_id == owner_user_id,
                    MCPBranchRecordRow.task_id == task_id,
                    MCPBranchRecordRow.active_call_ref == call_row.call_ref,
                )
                .values(
                    active_call_ref=None,
                    status=call_status,
                    result_ref=result_ref,
                    safe_summary=(
                        "The MCP remote task completed."
                        if call_status == "completed"
                        else "The MCP remote task ended without a completed result."
                    ),
                    updated_at=terminal_at,
                    terminal_at=terminal_at,
                )
            )
            outbox_id = f"mcp-remote-terminal:{call_row.call_ref}"
            insert_statement = (
                postgresql_insert(MCPRemoteTaskOutboxRow)
                if self._session.bind is not None
                and self._session.bind.dialect.name == "postgresql"
                else sqlite_insert(MCPRemoteTaskOutboxRow)
            )
            self._session.execute(
                insert_statement.values(
                    outbox_id=outbox_id,
                    kind="terminal_continuation",
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                    node_id=call_row.node_id,
                    call_ref=call_row.call_ref,
                    safe_remote_task_ref=safe_remote_task_ref,
                    payload={
                        "call_status": call_status,
                        "result_ref": result_ref,
                        "safe_error_code": safe_error_code,
                        "continuation_plan": dict(binding_row.continuation_plan or {}),
                    },
                    status="pending",
                    revision=0,
                    created_at=terminal_at,
                    updated_at=terminal_at,
                ).on_conflict_do_nothing()
            )
        self._session.flush()
        self._session.expire_all()
        persisted = self._session.get(MCPRemoteTaskBindingRow, safe_remote_task_ref)
        return None if persisted is None else _row_to_mcp_remote_task(persisted)

    def claim_mcp_remote_task_outbox(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]:
        candidates = self._session.scalars(
            select(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.completed_at.is_(None),
                MCPRemoteTaskOutboxRow.kind.in_(
                    ["terminal_continuation", "control_update", "control_cancel"]
                ),
                or_(
                    and_(
                        MCPRemoteTaskOutboxRow.kind == "terminal_continuation",
                        or_(
                            MCPRemoteTaskOutboxRow.status == "pending",
                            MCPRemoteTaskOutboxRow.lease_expires_at.is_(None),
                            MCPRemoteTaskOutboxRow.lease_expires_at <= now,
                        ),
                    ),
                    and_(
                        MCPRemoteTaskOutboxRow.kind.in_(["control_update", "control_cancel"]),
                        or_(
                            MCPRemoteTaskOutboxRow.status == "pending",
                            and_(
                                MCPRemoteTaskOutboxRow.status == "claimed",
                                MCPRemoteTaskOutboxRow.lease_expires_at <= now,
                            ),
                        ),
                    ),
                ),
            )
            .order_by(MCPRemoteTaskOutboxRow.created_at, MCPRemoteTaskOutboxRow.outbox_id)
            .limit(max(1, limit))
        ).all()
        claimed: list[MCPRemoteTaskOutbox] = []
        for candidate in candidates:
            revision = int(candidate.revision or 0)
            result = self._session.execute(
                update(MCPRemoteTaskOutboxRow)
                .where(
                    MCPRemoteTaskOutboxRow.outbox_id == candidate.outbox_id,
                    MCPRemoteTaskOutboxRow.completed_at.is_(None),
                    MCPRemoteTaskOutboxRow.revision == revision,
                    or_(
                        and_(
                            MCPRemoteTaskOutboxRow.kind == "terminal_continuation",
                            or_(
                                MCPRemoteTaskOutboxRow.status == "pending",
                                MCPRemoteTaskOutboxRow.lease_expires_at.is_(None),
                                MCPRemoteTaskOutboxRow.lease_expires_at <= now,
                            ),
                        ),
                        and_(
                            MCPRemoteTaskOutboxRow.kind.in_(["control_update", "control_cancel"]),
                            or_(
                                MCPRemoteTaskOutboxRow.status == "pending",
                                and_(
                                    MCPRemoteTaskOutboxRow.status == "claimed",
                                    MCPRemoteTaskOutboxRow.lease_expires_at <= now,
                                ),
                            ),
                        ),
                    ),
                )
                .values(
                    status="claimed",
                    claim_owner=claim_owner,
                    claim_token=claim_token,
                    lease_expires_at=lease_expires_at,
                    revision=revision + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                self._session.flush()
                self._session.expire_all()
                row = self._session.get(MCPRemoteTaskOutboxRow, candidate.outbox_id)
                if row is not None:
                    claimed.append(_row_to_mcp_remote_task_outbox(row))
        return claimed

    def claim_abandoned_mcp_remote_task_controls(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]:
        candidates = self._session.scalars(
            select(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.kind.in_(["control_update", "control_cancel"]),
                MCPRemoteTaskOutboxRow.status.in_(["sending", "abandoning"]),
                MCPRemoteTaskOutboxRow.completed_at.is_(None),
                MCPRemoteTaskOutboxRow.lease_expires_at <= now,
            )
            .order_by(MCPRemoteTaskOutboxRow.updated_at, MCPRemoteTaskOutboxRow.outbox_id)
            .limit(max(1, limit))
        ).all()
        claimed: list[MCPRemoteTaskOutbox] = []
        for candidate in candidates:
            revision = int(candidate.revision or 0)
            result = self._session.execute(
                update(MCPRemoteTaskOutboxRow)
                .where(
                    MCPRemoteTaskOutboxRow.outbox_id == candidate.outbox_id,
                    MCPRemoteTaskOutboxRow.status.in_(["sending", "abandoning"]),
                    MCPRemoteTaskOutboxRow.revision == revision,
                    MCPRemoteTaskOutboxRow.completed_at.is_(None),
                    MCPRemoteTaskOutboxRow.lease_expires_at <= now,
                )
                .values(
                    status="abandoning",
                    claim_owner=claim_owner,
                    claim_token=claim_token,
                    revision=revision + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                self._session.flush()
                self._session.expire_all()
                row = self._session.get(MCPRemoteTaskOutboxRow, candidate.outbox_id)
                if row is not None:
                    claimed.append(_row_to_mcp_remote_task_outbox(row))
        return claimed

    def begin_mcp_remote_task_control_delivery(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        result = self._session.execute(
            update(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.kind.in_(["control_update", "control_cancel"]),
                MCPRemoteTaskOutboxRow.status == "claimed",
                MCPRemoteTaskOutboxRow.claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.claim_token == claim_token,
                MCPRemoteTaskOutboxRow.revision == expected_revision,
                MCPRemoteTaskOutboxRow.completed_at.is_(None),
            )
            .values(
                status="sending",
                lease_expires_at=lease_expires_at,
                revision=expected_revision + 1,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def pause_mcp_remote_task_for_input(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        input_requests: Mapping[str, Any],
        conversation_id: str,
        source_message_id: str,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None:
        binding = self._session.scalar(
            select(MCPRemoteTaskBindingRow).where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
                MCPRemoteTaskBindingRow.claim_owner == claim_owner,
                MCPRemoteTaskBindingRow.claim_token == claim_token,
                MCPRemoteTaskBindingRow.revision == expected_revision,
                MCPRemoteTaskBindingRow.terminal_at.is_(None),
            )
        )
        if binding is None:
            return None
        binding.last_status = "input_required"
        binding.next_poll_at = None
        binding.updated_at = updated_at
        binding.claim_owner = None
        binding.claim_token = None
        binding.lease_expires_at = None
        binding.revision = expected_revision + 1
        interrupt_id = f"mcp-remote-input:{binding.call_ref}"
        interrupt_values = {
            "interrupt_id": interrupt_id,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "node_id": binding.node_id,
            "source_agent": "mcp.remote_task",
            "source_message_id": source_message_id,
            "question": "The MCP remote task requires additional input.",
            "reason_code": "mcp_remote_task_input_required",
            "required_fields": {
                "mcp_input_responses": dict(input_requests),
                "safe_remote_task_ref": safe_remote_task_ref,
                "server_id": binding.server_id,
                "protocol_version": binding.protocol_version,
            },
            "status": "open",
            "created_at": updated_at,
        }
        insert_interrupt = (
            postgresql_insert(InterruptRow)
            if self._session.bind is not None
            and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(InterruptRow)
        )
        self._session.execute(
            insert_interrupt.values(**interrupt_values).on_conflict_do_nothing()
        )
        outbox_id = f"mcp-remote-input:{binding.call_ref}"
        insert_outbox = (
            postgresql_insert(MCPRemoteTaskOutboxRow)
            if self._session.bind is not None
            and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(MCPRemoteTaskOutboxRow)
        )
        self._session.execute(
            insert_outbox.values(
                outbox_id=outbox_id,
                kind="awaiting_input",
                owner_user_id=owner_user_id,
                task_id=task_id,
                node_id=binding.node_id,
                call_ref=binding.call_ref,
                safe_remote_task_ref=safe_remote_task_ref,
                payload={"input_requests": dict(input_requests)},
                status="awaiting_input",
                revision=0,
                created_at=updated_at,
                updated_at=updated_at,
            ).on_conflict_do_nothing()
        )
        self._session.flush()
        return _row_to_mcp_remote_task(binding)

    def enqueue_mcp_remote_task_control(
        self,
        answer: InterruptAnswer,
        *,
        action: str,
        input_responses: Mapping[str, Any],
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        if action not in {"update", "cancel"}:
            raise ValueError("MCP remote task control action is invalid")
        interrupt = self._session.get(InterruptRow, answer.interrupt_id)
        if (
            interrupt is None
            or interrupt.reason_code != "mcp_remote_task_input_required"
            or str(interrupt.status) != "open"
        ):
            return None
        safe_ref = str(
            (interrupt.required_fields or {}).get("safe_remote_task_ref") or ""
        ).strip()
        binding = self._session.get(MCPRemoteTaskBindingRow, safe_ref)
        if (
            binding is None
            or binding.task_id != interrupt.task_id
            or binding.protocol_version != "2026-07-28"
            or binding.last_status != "input_required"
            or binding.terminal_at is not None
        ):
            return None
        outbox_id = f"mcp-remote-input:{binding.call_ref}"
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        if row is None or row.kind != "awaiting_input" or row.status != "awaiting_input":
            return None
        self._session.merge(
            InterruptAnswerRow(
                interrupt_answer_id=answer.interrupt_answer_id,
                interrupt_id=answer.interrupt_id,
                answer_payload=dict(answer.answer_payload),
                source_message_id=answer.source_message_id,
                accepted=True,
                created_at=answer.created_at or updated_at,
                accepted_at=updated_at,
            )
        )
        interrupt.status = "answered"
        interrupt.answered_at = updated_at
        row.kind = "control_update" if action == "update" else "control_cancel"
        row.payload = (
            {"input_responses": dict(input_responses)}
            if action == "update"
            else {"reason": "user_cancelled_remote_input"}
        )
        row.status = "pending"
        row.updated_at = updated_at
        row.revision = int(row.revision or 0) + 1
        self._session.flush()
        return _row_to_mcp_remote_task_outbox(row)

    def apply_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        row = self._session.scalar(
            select(MCPRemoteTaskOutboxRow).where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.claim_token == claim_token,
                MCPRemoteTaskOutboxRow.revision == expected_revision,
                MCPRemoteTaskOutboxRow.completed_at.is_(None),
            )
        )
        if row is None or row.kind != "terminal_continuation":
            return None
        row.status = "applied"
        row.updated_at = updated_at
        row.revision = expected_revision + 1
        self._session.flush()
        return _row_to_mcp_remote_task_outbox(row)

    def get_mcp_remote_task_outbox(
        self, outbox_id: str
    ) -> MCPRemoteTaskOutbox | None:
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def admit_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        admitted_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        result = self._session.execute(
            update(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.kind == "terminal_continuation",
                MCPRemoteTaskOutboxRow.claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.claim_token == claim_token,
                MCPRemoteTaskOutboxRow.revision == expected_revision,
                MCPRemoteTaskOutboxRow.continuation_admitted_at.is_(None),
                MCPRemoteTaskOutboxRow.completed_at.is_(None),
            )
            .values(
                continuation_admitted_at=admitted_at,
                continuation_status="pending",
                revision=expected_revision + 1,
                updated_at=admitted_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def claim_mcp_remote_task_continuations(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]:
        candidates = self._session.scalars(
            select(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.kind == "terminal_continuation",
                MCPRemoteTaskOutboxRow.continuation_admitted_at.is_not(None),
                MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
                or_(
                    MCPRemoteTaskOutboxRow.continuation_status == "pending",
                    and_(
                        MCPRemoteTaskOutboxRow.continuation_status == "claimed",
                        MCPRemoteTaskOutboxRow.continuation_lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(MCPRemoteTaskOutboxRow.created_at, MCPRemoteTaskOutboxRow.outbox_id)
            .limit(max(1, limit))
        ).all()
        claimed: list[MCPRemoteTaskOutbox] = []
        for candidate in candidates:
            command_revision = int(candidate.continuation_revision or 0)
            result = self._session.execute(
                update(MCPRemoteTaskOutboxRow)
                .where(
                    MCPRemoteTaskOutboxRow.outbox_id == candidate.outbox_id,
                    MCPRemoteTaskOutboxRow.continuation_revision == command_revision,
                    MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
                    or_(
                        MCPRemoteTaskOutboxRow.continuation_status == "pending",
                        and_(
                            MCPRemoteTaskOutboxRow.continuation_status == "claimed",
                            MCPRemoteTaskOutboxRow.continuation_lease_expires_at <= now,
                        ),
                    ),
                )
                .values(
                    continuation_status="claimed",
                    continuation_claim_owner=claim_owner,
                    continuation_claim_token=claim_token,
                    continuation_lease_expires_at=lease_expires_at,
                    continuation_revision=command_revision + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                self._session.flush()
                self._session.expire_all()
                row = self._session.get(MCPRemoteTaskOutboxRow, candidate.outbox_id)
                if row is not None:
                    claimed.append(_row_to_mcp_remote_task_outbox(row))
        return claimed

    def begin_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        started_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        result = self._session.execute(
            update(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.continuation_status == "claimed",
                MCPRemoteTaskOutboxRow.continuation_claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.continuation_claim_token == claim_token,
                MCPRemoteTaskOutboxRow.continuation_revision == expected_revision,
                MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
            )
            .values(
                continuation_status="running",
                continuation_revision=expected_revision + 1,
                updated_at=started_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def abandon_expired_mcp_remote_task_continuations(
        self, *, now: datetime, limit: int = 100
    ) -> list[MCPRemoteTaskOutbox]:
        candidates = self._session.scalars(
            select(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.kind == "terminal_continuation",
                MCPRemoteTaskOutboxRow.continuation_status.in_(["running", "abandoning"]),
                MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
                or_(
                    MCPRemoteTaskOutboxRow.continuation_status == "abandoning",
                    MCPRemoteTaskOutboxRow.continuation_lease_expires_at <= now,
                ),
            )
            .order_by(MCPRemoteTaskOutboxRow.created_at, MCPRemoteTaskOutboxRow.outbox_id)
            .limit(max(1, limit))
        ).all()
        abandoned: list[MCPRemoteTaskOutbox] = []
        for candidate in candidates:
            command_revision = int(candidate.continuation_revision or 0)
            result = self._session.execute(
                update(MCPRemoteTaskOutboxRow)
                .where(
                    MCPRemoteTaskOutboxRow.outbox_id == candidate.outbox_id,
                    MCPRemoteTaskOutboxRow.continuation_status.in_(["running", "abandoning"]),
                    MCPRemoteTaskOutboxRow.continuation_revision == command_revision,
                    MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
                )
                .values(
                    continuation_status="abandoning",
                    continuation_safe_error_code="mcp_continuation_execution_unknown",
                    continuation_revision=command_revision + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                self._session.flush()
                self._session.expire_all()
                row = self._session.get(MCPRemoteTaskOutboxRow, candidate.outbox_id)
                if row is not None:
                    abandoned.append(_row_to_mcp_remote_task_outbox(row))
        return abandoned

    def complete_abandoned_mcp_remote_task_continuation(
        self, outbox_id: str, *, expected_revision: int, completed_at: datetime
    ) -> MCPRemoteTaskOutbox | None:
        result = self._session.execute(
            update(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.continuation_status == "abandoning",
                MCPRemoteTaskOutboxRow.continuation_revision == expected_revision,
                MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
            )
            .values(
                continuation_status="failed",
                continuation_dispatched_at=completed_at,
                continuation_revision=expected_revision + 1,
                updated_at=completed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def renew_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        node_ids: tuple[str, ...] | None,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        values: dict[str, Any] = {
            "continuation_lease_expires_at": lease_expires_at,
            "continuation_revision": expected_revision + 1,
            "updated_at": updated_at,
        }
        if node_ids is not None:
            values["continuation_node_ids"] = list(node_ids)
        result = self._session.execute(
            update(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.continuation_status == "running",
                MCPRemoteTaskOutboxRow.continuation_claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.continuation_claim_token == claim_token,
                MCPRemoteTaskOutboxRow.continuation_revision == expected_revision,
                MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def mark_mcp_remote_task_continuation_dispatched(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        dispatched_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        result = self._session.execute(
            update(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.kind == "terminal_continuation",
                MCPRemoteTaskOutboxRow.continuation_claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.continuation_claim_token == claim_token,
                MCPRemoteTaskOutboxRow.continuation_revision == expected_revision,
                MCPRemoteTaskOutboxRow.continuation_status == "running",
                MCPRemoteTaskOutboxRow.continuation_admitted_at.is_not(None),
                MCPRemoteTaskOutboxRow.continuation_dispatched_at.is_(None),
            )
            .values(
                continuation_dispatched_at=dispatched_at,
                continuation_status="completed",
                continuation_claim_owner=None,
                continuation_claim_token=None,
                continuation_lease_expires_at=None,
                continuation_revision=expected_revision + 1,
                updated_at=dispatched_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def complete_mcp_remote_task_outbox(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        result = self._session.execute(
            update(MCPRemoteTaskOutboxRow)
            .where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.claim_token == claim_token,
                MCPRemoteTaskOutboxRow.revision == expected_revision,
                MCPRemoteTaskOutboxRow.completed_at.is_(None),
            )
            .values(
                status="completed",
                completed_at=completed_at,
                updated_at=completed_at,
                claim_owner=None,
                claim_token=None,
                lease_expires_at=None,
                revision=expected_revision + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        self._session.expire_all()
        row = self._session.get(MCPRemoteTaskOutboxRow, outbox_id)
        return None if row is None else _row_to_mcp_remote_task_outbox(row)

    def complete_mcp_remote_task_control(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        outcome: str,
        completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        if outcome not in {"delivered", "ambiguous"}:
            raise ValueError("MCP remote task control outcome is invalid")
        row = self._session.scalar(
            select(MCPRemoteTaskOutboxRow).where(
                MCPRemoteTaskOutboxRow.outbox_id == outbox_id,
                MCPRemoteTaskOutboxRow.claim_owner == claim_owner,
                MCPRemoteTaskOutboxRow.claim_token == claim_token,
                MCPRemoteTaskOutboxRow.revision == expected_revision,
                MCPRemoteTaskOutboxRow.completed_at.is_(None),
            )
        )
        if row is None or row.kind not in {"control_update", "control_cancel"}:
            return None
        binding = self._session.get(
            MCPRemoteTaskBindingRow, row.safe_remote_task_ref
        )
        if binding is None or binding.terminal_at is not None:
            return None
        if outcome == "delivered" and row.kind == "control_update":
            binding.last_status = "working"
            binding.next_poll_at = completed_at
            binding.updated_at = completed_at
            binding.revision = int(binding.revision or 0) + 1
        else:
            call_status = (
                "cancelled"
                if outcome == "delivered" and row.kind == "control_cancel"
                else "unknown"
            )
            safe_error_code = (
                "mcp_remote_task_cancelled"
                if call_status == "cancelled"
                else "execution_status_unknown"
            )
            binding.last_status = call_status
            binding.next_poll_at = None
            binding.updated_at = completed_at
            binding.terminal_at = completed_at
            binding.revision = int(binding.revision or 0) + 1
            call = self._session.get(MCPCallRecordRow, row.call_ref)
            if call is not None and call.terminal_at is None:
                call.status = call_status
                call.safe_error_code = safe_error_code
                call.updated_at = completed_at
                call.terminal_at = completed_at
                self._session.execute(
                    update(MCPBranchRecordRow)
                    .where(MCPBranchRecordRow.branch_id == call.branch_id)
                    .values(
                        status=call_status,
                        active_call_ref=None,
                        safe_summary="The MCP remote task control outcome is terminal.",
                        updated_at=completed_at,
                        terminal_at=completed_at,
                    )
                )
        row.status = "completed"
        row.completed_at = completed_at
        row.updated_at = completed_at
        row.claim_owner = None
        row.claim_token = None
        row.lease_expires_at = None
        row.revision = expected_revision + 1
        self._session.flush()
        return _row_to_mcp_remote_task_outbox(row)

    def delete_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> bool:
        result = self._session.execute(
            delete(MCPRemoteTaskBindingRow).where(
                MCPRemoteTaskBindingRow.safe_remote_task_ref == safe_remote_task_ref,
                MCPRemoteTaskBindingRow.owner_user_id == owner_user_id,
                MCPRemoteTaskBindingRow.task_id == task_id,
            )
        )
        return bool(result.rowcount)

    def save_mcp_sealed_state(self, state: MCPSealedState) -> MCPSealedState:
        values = {
            "sealed_state_ref": state.sealed_state_ref,
            "owner_user_id": state.owner_user_id,
            "task_id": state.task_id,
            "node_id": state.node_id,
            "call_ref": state.call_ref,
            "state_kind": state.state_kind,
            "ciphertext": state.ciphertext,
            "nonce": state.nonce,
            "encryption_version": state.encryption_version,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }
        insert_statement = (
            postgresql_insert(MCPSealedStateRow)
            if self._session.bind is not None and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(MCPSealedStateRow)
        )
        self._session.execute(insert_statement.values(**values).on_conflict_do_nothing())
        self._session.flush()
        self._session.expire_all()
        existing = self._session.get(MCPSealedStateRow, state.sealed_state_ref)
        if existing is None:
            raise RuntimeError("MCP sealed state insert did not persist")
        immutable_values = (
            existing.owner_user_id,
            existing.task_id,
            existing.node_id,
            existing.call_ref,
            existing.state_kind,
            existing.ciphertext,
            existing.nonce,
            int(existing.encryption_version),
        )
        incoming_values = (
            state.owner_user_id,
            state.task_id,
            state.node_id,
            state.call_ref,
            state.state_kind,
            state.ciphertext,
            state.nonce,
            state.encryption_version,
        )
        if immutable_values != incoming_values:
            raise ValueError("MCP sealed state immutable scope or ciphertext does not match existing record")
        return _row_to_mcp_sealed_state(existing)

    def get_mcp_sealed_state(
        self, owner_user_id: str, task_id: str, sealed_state_ref: str
    ) -> MCPSealedState | None:
        row = self._session.scalar(
            select(MCPSealedStateRow).where(
                MCPSealedStateRow.sealed_state_ref == sealed_state_ref,
                MCPSealedStateRow.owner_user_id == owner_user_id,
                MCPSealedStateRow.task_id == task_id,
            )
        )
        return None if row is None else _row_to_mcp_sealed_state(row)

    def delete_mcp_sealed_state(
        self, owner_user_id: str, task_id: str, sealed_state_ref: str
    ) -> bool:
        result = self._session.execute(
            delete(MCPSealedStateRow).where(
                MCPSealedStateRow.sealed_state_ref == sealed_state_ref,
                MCPSealedStateRow.owner_user_id == owner_user_id,
                MCPSealedStateRow.task_id == task_id,
            )
        )
        return bool(result.rowcount)

    def save_mcp_connection_lease(self, lease: MCPConnectionLease) -> MCPConnectionLease:
        existing = self._session.get(MCPConnectionLeaseRow, lease.connection_id)
        if existing is not None and (
            existing.owner_user_id != lease.owner_user_id
            or existing.task_id != lease.task_id
            or existing.instance_id != lease.instance_id
        ):
            raise ValueError("MCP connection lease scope does not match existing lease")
        merged = self._session.merge(
            MCPConnectionLeaseRow(
                connection_id=lease.connection_id,
                owner_user_id=lease.owner_user_id,
                task_id=lease.task_id,
                instance_id=lease.instance_id,
                lease_expires_at=lease.lease_expires_at,
                disconnected_at=lease.disconnected_at,
                auth_generation=lease.auth_generation,
                created_at=lease.created_at,
                updated_at=lease.updated_at,
            )
        )
        self._session.flush()
        return _row_to_mcp_connection_lease(merged)

    def list_live_mcp_connection_leases(
        self, owner_user_id: str, task_id: str, *, now: datetime
    ) -> list[MCPConnectionLease]:
        rows = self._session.scalars(
            select(MCPConnectionLeaseRow)
            .where(
                MCPConnectionLeaseRow.owner_user_id == owner_user_id,
                MCPConnectionLeaseRow.task_id == task_id,
                MCPConnectionLeaseRow.lease_expires_at > now,
            )
            .order_by(MCPConnectionLeaseRow.connection_id)
        ).all()
        return [_row_to_mcp_connection_lease(row) for row in rows]

    def delete_mcp_connection_lease(
        self, owner_user_id: str, task_id: str, connection_id: str
    ) -> bool:
        result = self._session.execute(
            delete(MCPConnectionLeaseRow).where(
                MCPConnectionLeaseRow.connection_id == connection_id,
                MCPConnectionLeaseRow.owner_user_id == owner_user_id,
                MCPConnectionLeaseRow.task_id == task_id,
            )
        )
        return bool(result.rowcount)

    def expire_mcp_connection_leases(self, *, now: datetime, limit: int = 1000) -> int:
        ids = self._session.scalars(
            select(MCPConnectionLeaseRow.connection_id)
            .where(MCPConnectionLeaseRow.lease_expires_at <= now)
            .order_by(MCPConnectionLeaseRow.lease_expires_at)
            .limit(max(1, limit))
        ).all()
        if not ids:
            return 0
        self._session.execute(
            delete(MCPConnectionLeaseRow).where(MCPConnectionLeaseRow.connection_id.in_(ids))
        )
        return len(ids)

    def append_mcp_audit_event(self, event: MCPAuditEvent) -> MCPAuditEvent:
        existing = self._session.get(MCPAuditEventRow, event.audit_event_id)
        if existing is not None:
            if existing.owner_user_id != event.owner_user_id:
                raise ValueError("MCP audit event owner does not match existing event")
            return _row_to_mcp_audit_event(existing)
        row = MCPAuditEventRow(
            audit_event_id=event.audit_event_id,
            owner_user_id=event.owner_user_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            expires_at=event.expires_at,
            task_id=event.task_id,
            node_id=event.node_id,
            server_id=event.server_id,
            call_ref=event.call_ref,
            safe_payload=dict(event.safe_payload),
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_audit_event(row)

    def list_mcp_audit_events(
        self, owner_user_id: str, *, task_id: str | None = None, limit: int = 100
    ) -> list[MCPAuditEvent]:
        conditions = [MCPAuditEventRow.owner_user_id == owner_user_id]
        if task_id is not None:
            conditions.append(MCPAuditEventRow.task_id == task_id)
        rows = self._session.scalars(
            select(MCPAuditEventRow)
            .where(*conditions)
            .order_by(MCPAuditEventRow.occurred_at, MCPAuditEventRow.audit_event_id)
            .limit(max(1, limit))
        ).all()
        return [_row_to_mcp_audit_event(row) for row in rows]

    def delete_expired_mcp_audit_events(self, *, now: datetime, limit: int = 1000) -> int:
        ids = self._session.scalars(
            select(MCPAuditEventRow.audit_event_id)
            .where(MCPAuditEventRow.expires_at <= now)
            .order_by(MCPAuditEventRow.expires_at)
            .limit(max(1, limit))
        ).all()
        if not ids:
            return 0
        self._session.execute(
            delete(MCPAuditEventRow).where(MCPAuditEventRow.audit_event_id.in_(ids))
        )
        return len(ids)

    def ensure_mcp_rollout_gate_scope(
        self, scope: MCPRolloutGateScope
    ) -> MCPRolloutGateScope:
        if not scope.environment_id:
            raise ValueError("MCP rollout environment ID is required")
        if scope.rollout_program != MCP_ROLLOUT_PROGRAM:
            raise ValueError("MCP rollout program is not supported")
        created_at = scope.created_at or datetime.now(timezone.utc)
        values = {
            "environment_id": scope.environment_id,
            "rollout_program": scope.rollout_program,
            "created_at": created_at,
        }
        insert_statement = (
            postgresql_insert(MCPRolloutGateScopeRow)
            if self._session.bind is not None
            and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(MCPRolloutGateScopeRow)
        )
        self._session.execute(
            insert_statement.values(**values).on_conflict_do_nothing(
                index_elements=["environment_id", "rollout_program"]
            )
        )
        self._session.flush()
        row = self._session.get(
            MCPRolloutGateScopeRow,
            (scope.environment_id, scope.rollout_program),
        )
        if row is None:
            raise RuntimeError("MCP rollout gate scope insert did not persist")
        return _row_to_mcp_rollout_gate_scope(row)

    def append_mcp_rollout_drill_observation(
        self, observation: MCPRolloutDrillObservation
    ) -> MCPRolloutDrillObservation:
        blockers = validate_mcp_rollout_drill_observation(observation)
        if blockers:
            raise ValueError(
                "MCP rollout drill observation is invalid: " + ",".join(blockers)
            )
        existing = self._session.get(
            MCPRolloutDrillObservationRow,
            observation.drill_observation_id,
        )
        if existing is not None:
            persisted = _row_to_mcp_rollout_drill_observation(existing)
            if persisted == observation:
                return persisted
            raise ValueError("MCP rollout drill observation ID payload conflict")
        scope_owner = self._session.scalar(
            select(MCPRolloutDrillObservationRow.drill_observation_id).where(
                MCPRolloutDrillObservationRow.environment_id
                == observation.environment_id,
                MCPRolloutDrillObservationRow.rollout_program
                == observation.rollout_program,
                MCPRolloutDrillObservationRow.deployment_id
                == observation.deployment_id,
                MCPRolloutDrillObservationRow.stage == observation.stage,
                MCPRolloutDrillObservationRow.config_fingerprint
                == observation.config_fingerprint,
                MCPRolloutDrillObservationRow.drill == observation.drill,
                MCPRolloutDrillObservationRow.observed_at == observation.observed_at,
            )
        )
        if scope_owner is not None:
            raise ValueError("MCP rollout drill observation scope replay")
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=observation.environment_id,
                rollout_program=observation.rollout_program,
                created_at=observation.recorded_at,
            )
        )
        row = MCPRolloutDrillObservationRow(
            drill_observation_id=observation.drill_observation_id,
            environment_id=observation.environment_id,
            rollout_program=observation.rollout_program,
            deployment_id=observation.deployment_id,
            stage=observation.stage,
            config_fingerprint=observation.config_fingerprint,
            drill=observation.drill,
            outcome=observation.outcome,
            observed_at=observation.observed_at,
            recorded_at=observation.recorded_at,
            expires_at=observation.expires_at,
            payload_digest=observation.payload_digest,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_drill_observation(row)

    def list_mcp_rollout_drill_observations(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPRolloutDrillObservation]:
        _validate_rollout_scope(
            environment_id,
            MCP_ROLLOUT_PROGRAM,
            "internal_enforce",
        )
        if not deployment_id or window_ended_at <= window_started_at:
            raise ValueError("MCP rollout drill observation query scope is invalid")
        rows = self._session.scalars(
            select(MCPRolloutDrillObservationRow)
            .where(
                MCPRolloutDrillObservationRow.environment_id == environment_id,
                MCPRolloutDrillObservationRow.rollout_program
                == MCP_ROLLOUT_PROGRAM,
                MCPRolloutDrillObservationRow.deployment_id == deployment_id,
                MCPRolloutDrillObservationRow.stage == "internal_enforce",
                MCPRolloutDrillObservationRow.observed_at >= window_started_at,
                MCPRolloutDrillObservationRow.observed_at < window_ended_at,
                MCPRolloutDrillObservationRow.expires_at > window_ended_at,
            )
            .order_by(
                MCPRolloutDrillObservationRow.observed_at,
                MCPRolloutDrillObservationRow.drill,
                MCPRolloutDrillObservationRow.drill_observation_id,
            )
        ).all()
        return [_row_to_mcp_rollout_drill_observation(row) for row in rows]

    def upsert_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket:
        return self._write_mcp_rollout_metric_bucket(bucket, additive=True)

    def set_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket:
        return self._write_mcp_rollout_metric_bucket(bucket, additive=False)

    def _write_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket, *, additive: bool
    ) -> MCPRolloutMetricBucket:
        stage = _rollout_value(bucket.stage)
        metric_name = _rollout_value(bucket.metric_name)
        _validate_rollout_scope(bucket.environment_id, bucket.rollout_program, stage)
        if metric_name not in MCP_ROLLOUT_METRIC_NAMES:
            raise ValueError("MCP rollout metric name is not supported")
        if not is_exact_mcp_metric_bucket_window(
            bucket.bucket_started_at, bucket.bucket_ended_at
        ):
            raise ValueError(
                "MCP rollout metric bucket must be one complete UTC-aligned minute"
            )
        if isinstance(bucket.value, bool) or bucket.value < 0:
            raise ValueError("MCP rollout metric bucket value must be non-negative")
        labels = {
            "execution_path": _rollout_value(bucket.execution_path),
            "routing_mode": _rollout_value(bucket.routing_mode),
            "transport": _rollout_value(bucket.transport),
            "protocol_version": _rollout_value(bucket.protocol_version),
            "adapter": _rollout_value(bucket.adapter),
            "result_category": _rollout_value(bucket.result_category),
            "error_category": _rollout_value(bucket.error_category),
            "call_kind": "not_applicable"
            if bucket.call_kind is None
            else _rollout_value(bucket.call_kind),
            "red_line": "not_applicable"
            if bucket.red_line is None
            else _rollout_value(bucket.red_line),
            "latency_bucket": _rollout_value(bucket.latency_bucket),
        }
        for label_name, label_value in labels.items():
            if label_value not in MCP_ROLLOUT_LABEL_VALUES[label_name]:
                raise ValueError(f"MCP rollout metric label {label_name} is not supported")
        if metric_name == "mcp_safety_red_line_total":
            if labels["red_line"] == "not_applicable":
                raise ValueError("MCP safety red-line metric requires a red-line label")
        elif labels["red_line"] != "not_applicable":
            raise ValueError("MCP red-line label is reserved for the safety metric")
        created_at = bucket.created_at or datetime.now(timezone.utc)
        updated_at = bucket.updated_at or created_at
        values = {
            "metric_bucket_id": bucket.metric_bucket_id,
            "environment_id": bucket.environment_id,
            "rollout_program": bucket.rollout_program,
            "deployment_id": bucket.deployment_id,
            "stage": stage,
            "config_fingerprint": bucket.config_fingerprint,
            "metric_name": metric_name,
            "bucket_started_at": bucket.bucket_started_at,
            "bucket_ended_at": bucket.bucket_ended_at,
            **labels,
            "value": bucket.value,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        identity_columns = [
            "environment_id",
            "deployment_id",
            "stage",
            "config_fingerprint",
            "metric_name",
            "bucket_started_at",
            "bucket_ended_at",
            "execution_path",
            "routing_mode",
            "transport",
            "protocol_version",
            "adapter",
            "result_category",
            "error_category",
            "call_kind",
            "red_line",
            "latency_bucket",
        ]
        insert_statement = (
            postgresql_insert(MCPRolloutMetricBucketRow)
            if self._session.bind is not None
            and self._session.bind.dialect.name == "postgresql"
            else sqlite_insert(MCPRolloutMetricBucketRow)
        )
        conflict_value = (
            MCPRolloutMetricBucketRow.value + bucket.value
            if additive
            else bucket.value
        )
        self._session.execute(
            insert_statement.values(**values).on_conflict_do_update(
                index_elements=identity_columns,
                set_={
                    "value": conflict_value,
                    "updated_at": updated_at,
                },
            )
        )
        self._session.flush()
        row = self._session.scalar(
            select(MCPRolloutMetricBucketRow).where(
                *[
                    getattr(MCPRolloutMetricBucketRow, name) == values[name]
                    for name in identity_columns
                ]
            )
        )
        if row is None:
            raise RuntimeError("MCP rollout metric bucket upsert did not persist")
        if metric_name == "mcp_safety_red_line_total" and bucket.value > 0:
            self._derive_mcp_safety_red_line_promotion_block(
                bucket,
                stage=stage,
                created_at=updated_at,
            )
        return _row_to_mcp_rollout_metric_bucket(row)

    def _derive_mcp_safety_red_line_promotion_block(
        self,
        bucket: MCPRolloutMetricBucket,
        *,
        stage: str,
        created_at: datetime,
    ) -> MCPRolloutPromotionBlock:
        activation = self._session.scalar(
            select(MCPRolloutDeploymentActivationRow).where(
                MCPRolloutDeploymentActivationRow.environment_id
                == bucket.environment_id,
                MCPRolloutDeploymentActivationRow.rollout_program
                == bucket.rollout_program,
                MCPRolloutDeploymentActivationRow.deployment_id
                == bucket.deployment_id,
                MCPRolloutDeploymentActivationRow.stage == stage,
                MCPRolloutDeploymentActivationRow.config_fingerprint
                == bucket.config_fingerprint,
            )
        )
        if activation is None:
            raise ValueError(
                "positive MCP safety red-line metric requires an exact activation"
            )
        reason_code = "safety_red_line_nonzero"
        identity = "\0".join(
            (
                bucket.environment_id,
                bucket.rollout_program,
                bucket.deployment_id,
                stage,
                bucket.config_fingerprint,
                activation.evidence_id,
                reason_code,
            )
        )
        block_id = f"mcp-safety-block-{hashlib.sha256(identity.encode()).hexdigest()}"
        existing = self._session.scalar(
            select(MCPRolloutPromotionBlockRow).where(
                MCPRolloutPromotionBlockRow.evidence_id == activation.evidence_id,
                MCPRolloutPromotionBlockRow.reason_code == reason_code,
            )
        )
        if existing is not None:
            expected = (
                block_id,
                bucket.environment_id,
                bucket.rollout_program,
                bucket.deployment_id,
                stage,
                bucket.config_fingerprint,
                activation.evidence_id,
                reason_code,
            )
            actual = (
                existing.block_id,
                existing.environment_id,
                existing.rollout_program,
                existing.deployment_id,
                existing.stage,
                existing.config_fingerprint,
                existing.evidence_id,
                existing.reason_code,
            )
            if actual != expected:
                raise ValueError("MCP safety red-line promotion block identity conflict")
            return _row_to_mcp_rollout_promotion_block(existing)
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=bucket.environment_id,
                rollout_program=bucket.rollout_program,
                created_at=created_at,
            )
        )
        row = MCPRolloutPromotionBlockRow(
            block_id=block_id,
            environment_id=bucket.environment_id,
            rollout_program=bucket.rollout_program,
            deployment_id=bucket.deployment_id,
            stage=stage,
            config_fingerprint=bucket.config_fingerprint,
            evidence_id=activation.evidence_id,
            reason_code=reason_code,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_promotion_block(row)

    def list_mcp_rollout_metric_buckets(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPRolloutMetricBucket]:
        normalized_stage = _rollout_value(stage)
        _validate_rollout_scope(environment_id, MCP_ROLLOUT_PROGRAM, normalized_stage)
        if window_ended_at <= window_started_at:
            raise ValueError("MCP rollout metric query window is invalid")
        rows = self._session.scalars(
            select(MCPRolloutMetricBucketRow)
            .where(
                MCPRolloutMetricBucketRow.environment_id == environment_id,
                MCPRolloutMetricBucketRow.rollout_program == MCP_ROLLOUT_PROGRAM,
                MCPRolloutMetricBucketRow.deployment_id == deployment_id,
                MCPRolloutMetricBucketRow.stage == normalized_stage,
                MCPRolloutMetricBucketRow.bucket_started_at >= window_started_at,
                MCPRolloutMetricBucketRow.bucket_ended_at <= window_ended_at,
            )
            .order_by(
                MCPRolloutMetricBucketRow.bucket_started_at,
                MCPRolloutMetricBucketRow.metric_name,
                MCPRolloutMetricBucketRow.metric_bucket_id,
            )
        ).all()
        return [_row_to_mcp_rollout_metric_bucket(row) for row in rows]

    def save_mcp_shadow_audit_sample(
        self, sample: MCPShadowAuditSample
    ) -> MCPShadowAuditSample:
        from src.integrations.mcp.shadow_evidence import validate_shadow_audit_sample

        blockers = validate_shadow_audit_sample(sample)
        if blockers:
            raise ValueError(f"MCP shadow audit sample is invalid: {','.join(blockers)}")
        existing = self._session.get(MCPShadowAuditSampleRow, sample.sample_id)
        if existing is not None:
            persisted = _row_to_mcp_shadow_audit_sample(existing)
            if persisted == sample:
                return persisted
            raise ValueError("MCP shadow audit sample ID payload conflict")
        nonce_owner = self._session.scalar(
            select(MCPShadowAuditSampleRow.sample_id).where(
                MCPShadowAuditSampleRow.environment_id == sample.environment_id,
                MCPShadowAuditSampleRow.deployment_id == sample.deployment_id,
                MCPShadowAuditSampleRow.stage == sample.stage,
                MCPShadowAuditSampleRow.config_fingerprint == sample.config_fingerprint,
                MCPShadowAuditSampleRow.nonce == sample.nonce,
            )
        )
        if nonce_owner is not None:
            raise ValueError("MCP shadow audit sample nonce replay")
        row = MCPShadowAuditSampleRow(
            sample_id=sample.sample_id,
            environment_id=sample.environment_id,
            rollout_program=sample.rollout_program,
            deployment_id=sample.deployment_id,
            stage=sample.stage,
            config_fingerprint=sample.config_fingerprint,
            manifest_fingerprint=sample.manifest_fingerprint,
            fixture_fingerprint=sample.fixture_fingerprint,
            mapping_fingerprint=sample.mapping_fingerprint,
            scenario=sample.scenario,
            nonce=sample.nonce,
            safe_owner_ref=sample.safe_owner_ref,
            safe_task_ref=sample.safe_task_ref,
            safe_call_ref=sample.safe_call_ref,
            legacy_outcome=sample.legacy_outcome,
            shadow_outcome=sample.shadow_outcome,
            transport=sample.transport,
            endpoint_policy=sample.endpoint_policy,
            comparison=sample.comparison,
            blockers=list(sample.blockers),
            payload_digest=sample.payload_digest,
            observed_at=sample.observed_at,
            recorded_at=sample.recorded_at,
            expires_at=sample.expires_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_shadow_audit_sample(row)

    def list_mcp_shadow_audit_samples(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPShadowAuditSample]:
        _validate_rollout_scope(environment_id, MCP_ROLLOUT_PROGRAM, stage)
        if stage != "internal_shadow" or window_ended_at <= window_started_at:
            raise ValueError("MCP shadow audit sample query scope is invalid")
        rows = self._session.scalars(
            select(MCPShadowAuditSampleRow)
            .where(
                MCPShadowAuditSampleRow.environment_id == environment_id,
                MCPShadowAuditSampleRow.rollout_program == MCP_ROLLOUT_PROGRAM,
                MCPShadowAuditSampleRow.deployment_id == deployment_id,
                MCPShadowAuditSampleRow.stage == stage,
                MCPShadowAuditSampleRow.observed_at >= window_started_at,
                MCPShadowAuditSampleRow.observed_at < window_ended_at,
                MCPShadowAuditSampleRow.expires_at > window_ended_at,
            )
            .order_by(MCPShadowAuditSampleRow.observed_at, MCPShadowAuditSampleRow.sample_id)
        ).all()
        return [_row_to_mcp_shadow_audit_sample(row) for row in rows]

    def delete_expired_mcp_shadow_audit_samples(
        self, *, now: datetime, limit: int = 1000
    ) -> int:
        ids = self._session.scalars(
            select(MCPShadowAuditSampleRow.sample_id)
            .where(MCPShadowAuditSampleRow.expires_at <= now)
            .order_by(MCPShadowAuditSampleRow.expires_at, MCPShadowAuditSampleRow.sample_id)
            .limit(max(1, limit))
        ).all()
        if not ids:
            return 0
        self._session.execute(
            delete(MCPShadowAuditSampleRow).where(MCPShadowAuditSampleRow.sample_id.in_(ids))
        )
        return len(ids)

    def append_mcp_rollout_evidence_snapshot(
        self, snapshot: MCPRolloutEvidenceSnapshot
    ) -> MCPRolloutEvidenceSnapshot:
        stage = _rollout_value(snapshot.stage)
        source = _rollout_value(snapshot.source)
        producer = _rollout_value(snapshot.producer)
        evidence_kind = _rollout_value(snapshot.evidence_kind)
        _validate_rollout_scope(snapshot.environment_id, snapshot.rollout_program, stage)
        if source not in MCP_ROLLOUT_EVIDENCE_SOURCES:
            raise ValueError("MCP rollout evidence source is not supported")
        if producer not in MCP_ROLLOUT_EVIDENCE_PRODUCERS:
            raise ValueError("MCP rollout evidence producer is not supported")
        if (source, producer) not in {
            ("ci", "ci_pipeline"),
            ("production", "production_snapshot_producer"),
        }:
            raise ValueError("MCP rollout evidence provenance is invalid")
        if source == "ci":
            if (
                snapshot.attestation_key_id is not None
                or snapshot.attestation_signature is not None
            ):
                raise ValueError("CI MCP rollout evidence cannot be attested")
        elif (
            not isinstance(snapshot.attestation_key_id, str)
            or MCP_ROLLOUT_ATTESTATION_KEY_ID_RE.fullmatch(
                snapshot.attestation_key_id
            )
            is None
            or not isinstance(snapshot.attestation_signature, str)
            or MCP_ROLLOUT_ATTESTATION_SIGNATURE_RE.fullmatch(
                snapshot.attestation_signature
            )
            is None
        ):
            raise ValueError("production MCP rollout evidence attestation is required")
        if evidence_kind not in MCP_ROLLOUT_EVIDENCE_KINDS:
            raise ValueError("MCP rollout evidence kind is not supported")
        if isinstance(snapshot.snapshot_id, bool) or snapshot.snapshot_id <= 0:
            raise ValueError("MCP rollout evidence snapshot ID must be positive")
        if snapshot.window_ended_at <= snapshot.window_started_at:
            raise ValueError("MCP rollout evidence window is invalid")
        if snapshot.recorded_at < snapshot.window_ended_at:
            raise ValueError("MCP rollout evidence cannot predate its observation window")
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=snapshot.environment_id,
                rollout_program=snapshot.rollout_program,
                created_at=snapshot.recorded_at,
            )
        )
        if self._session.get(MCPRolloutEvidenceSnapshotRow, snapshot.evidence_id) is not None:
            raise ValueError("MCP rollout evidence ID replay is not allowed")
        if self._session.scalar(
            select(MCPRolloutEvidenceSnapshotRow.evidence_id).where(
                MCPRolloutEvidenceSnapshotRow.nonce == snapshot.nonce
            )
        ) is not None:
            raise ValueError("MCP rollout evidence nonce replay is not allowed")
        if self._session.scalar(
            select(MCPRolloutEvidenceSnapshotRow.evidence_id).where(
                MCPRolloutEvidenceSnapshotRow.deployment_id == snapshot.deployment_id,
                MCPRolloutEvidenceSnapshotRow.stage == stage,
                MCPRolloutEvidenceSnapshotRow.snapshot_id == snapshot.snapshot_id,
            )
        ) is not None:
            raise ValueError("MCP rollout evidence snapshot replay is not allowed")
        previous = self._session.scalar(
            select(MCPRolloutEvidenceSnapshotRow)
            .where(
                MCPRolloutEvidenceSnapshotRow.environment_id == snapshot.environment_id,
                MCPRolloutEvidenceSnapshotRow.rollout_program == snapshot.rollout_program,
                MCPRolloutEvidenceSnapshotRow.deployment_id == snapshot.deployment_id,
                MCPRolloutEvidenceSnapshotRow.stage == stage,
            )
            .order_by(
                MCPRolloutEvidenceSnapshotRow.snapshot_id.desc(),
                MCPRolloutEvidenceSnapshotRow.recorded_at.desc(),
            )
            .limit(1)
        )
        if previous is not None:
            if snapshot.snapshot_id <= int(previous.snapshot_id):
                raise ValueError("MCP rollout evidence snapshot ID must be monotonic")
            if snapshot.recorded_at <= previous.recorded_at:
                raise ValueError("MCP rollout evidence recorded time must be monotonic")
            if snapshot.window_ended_at <= previous.window_ended_at:
                raise ValueError("MCP rollout evidence window must advance monotonically")
            if snapshot.window_started_at > previous.window_ended_at:
                raise ValueError("MCP rollout evidence window must remain continuous")
            if snapshot.config_fingerprint != previous.config_fingerprint:
                raise ValueError("MCP rollout evidence config fingerprint changed within a stage")
        row = MCPRolloutEvidenceSnapshotRow(
            evidence_id=snapshot.evidence_id,
            environment_id=snapshot.environment_id,
            rollout_program=snapshot.rollout_program,
            git_sha=snapshot.git_sha,
            deployment_id=snapshot.deployment_id,
            stage=stage,
            config_fingerprint=snapshot.config_fingerprint,
            window_started_at=snapshot.window_started_at,
            window_ended_at=snapshot.window_ended_at,
            recorded_at=snapshot.recorded_at,
            producer=producer,
            source=source,
            snapshot_id=snapshot.snapshot_id,
            nonce=snapshot.nonce,
            evidence_kind=evidence_kind,
            payload=dict(snapshot.payload),
            payload_digest=snapshot.payload_digest,
            attestation_key_id=snapshot.attestation_key_id,
            attestation_signature=snapshot.attestation_signature,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_evidence_snapshot(row)

    def get_mcp_rollout_evidence_snapshot(
        self, evidence_id: str
    ) -> MCPRolloutEvidenceSnapshot | None:
        row = self._session.get(MCPRolloutEvidenceSnapshotRow, evidence_id)
        return None if row is None else _row_to_mcp_rollout_evidence_snapshot(row)

    def list_mcp_rollout_evidence_snapshots(
        self, environment_id: str, deployment_id: str, stage: str
    ) -> list[MCPRolloutEvidenceSnapshot]:
        normalized_stage = _rollout_value(stage)
        _validate_rollout_scope(environment_id, MCP_ROLLOUT_PROGRAM, normalized_stage)
        rows = self._session.scalars(
            select(MCPRolloutEvidenceSnapshotRow)
            .where(
                MCPRolloutEvidenceSnapshotRow.environment_id == environment_id,
                MCPRolloutEvidenceSnapshotRow.rollout_program == MCP_ROLLOUT_PROGRAM,
                MCPRolloutEvidenceSnapshotRow.deployment_id == deployment_id,
                MCPRolloutEvidenceSnapshotRow.stage == normalized_stage,
            )
            .order_by(
                MCPRolloutEvidenceSnapshotRow.snapshot_id,
                MCPRolloutEvidenceSnapshotRow.evidence_id,
            )
        ).all()
        return [_row_to_mcp_rollout_evidence_snapshot(row) for row in rows]

    def append_mcp_rollout_stage_approval(
        self, approval: MCPRolloutStageApproval
    ) -> MCPRolloutStageApproval:
        stage = _rollout_value(approval.stage)
        _validate_rollout_scope(approval.environment_id, approval.rollout_program, stage)
        if not approval.reason or not approval.approver:
            raise ValueError("MCP rollout approval reason and approver are required")
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=approval.environment_id,
                rollout_program=approval.rollout_program,
                created_at=approval.created_at,
            )
        )
        evidence = self._session.get(MCPRolloutEvidenceSnapshotRow, approval.evidence_id)
        if evidence is None:
            raise ValueError("MCP rollout approval evidence does not exist")
        if (
            evidence.environment_id != approval.environment_id
            or evidence.rollout_program != approval.rollout_program
        ):
            raise ValueError("MCP rollout approval evidence scope does not match")
        if self._session.get(MCPRolloutStageApprovalRow, approval.approval_id) is not None:
            raise ValueError("MCP rollout approval replay is not allowed")
        if self._session.scalar(
            select(MCPRolloutStageApprovalRow.approval_id).where(
                or_(
                    MCPRolloutStageApprovalRow.evidence_id == approval.evidence_id,
                    and_(
                        MCPRolloutStageApprovalRow.environment_id == approval.environment_id,
                        MCPRolloutStageApprovalRow.deployment_id == approval.deployment_id,
                        MCPRolloutStageApprovalRow.stage == stage,
                        MCPRolloutStageApprovalRow.config_fingerprint
                        == approval.config_fingerprint,
                    ),
                )
            )
        ) is not None:
            raise ValueError("MCP rollout approval logical target was already approved")
        row = MCPRolloutStageApprovalRow(
            approval_id=approval.approval_id,
            environment_id=approval.environment_id,
            rollout_program=approval.rollout_program,
            deployment_id=approval.deployment_id,
            stage=stage,
            config_fingerprint=approval.config_fingerprint,
            evidence_id=approval.evidence_id,
            reason=approval.reason,
            approver=approval.approver,
            created_at=approval.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_stage_approval(row)

    def activate_mcp_rollout_deployment(
        self, activation: MCPRolloutDeploymentActivation
    ) -> MCPRolloutDeploymentActivation:
        stage = _rollout_value(activation.stage)
        _validate_rollout_scope(activation.environment_id, activation.rollout_program, stage)
        if not activation.operator_reason:
            raise ValueError("MCP rollout activation operator reason is required")
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=activation.environment_id,
                rollout_program=activation.rollout_program,
                created_at=activation.created_at,
            )
        )
        if not activation.is_rollback and self._has_active_mcp_rollout_blocks(
            activation.environment_id, activation.rollout_program
        ):
            raise ValueError("MCP rollout activation is blocked by an active promotion block")
        approval = self._session.get(MCPRolloutStageApprovalRow, activation.approval_id)
        if approval is None:
            raise ValueError("MCP rollout activation approval does not exist")
        expected_approval = (
            activation.environment_id,
            activation.rollout_program,
            activation.deployment_id,
            stage,
            activation.config_fingerprint,
            activation.evidence_id,
        )
        actual_approval = (
            approval.environment_id,
            approval.rollout_program,
            approval.deployment_id,
            approval.stage,
            approval.config_fingerprint,
            approval.evidence_id,
        )
        if actual_approval != expected_approval:
            raise ValueError("MCP rollout activation approval does not match target")
        evidence = self._session.get(MCPRolloutEvidenceSnapshotRow, activation.evidence_id)
        if evidence is None or (
            evidence.environment_id != activation.environment_id
            or evidence.rollout_program != activation.rollout_program
        ):
            raise ValueError("MCP rollout activation evidence scope does not match")
        if activation.previous_activation_id is not None:
            previous = self._session.get(
                MCPRolloutDeploymentActivationRow,
                activation.previous_activation_id,
            )
            if previous is None or (
                previous.environment_id != activation.environment_id
                or previous.rollout_program != activation.rollout_program
            ):
                raise ValueError("MCP rollout previous activation scope does not match")
        if self._session.get(
            MCPRolloutDeploymentActivationRow, activation.activation_id
        ) is not None:
            raise ValueError("MCP rollout activation replay is not allowed")
        if self._session.scalar(
            select(MCPRolloutDeploymentActivationRow.activation_id).where(
                or_(
                    MCPRolloutDeploymentActivationRow.approval_id == activation.approval_id,
                    and_(
                        MCPRolloutDeploymentActivationRow.environment_id
                        == activation.environment_id,
                        MCPRolloutDeploymentActivationRow.deployment_id
                        == activation.deployment_id,
                        MCPRolloutDeploymentActivationRow.stage == stage,
                        MCPRolloutDeploymentActivationRow.config_fingerprint
                        == activation.config_fingerprint,
                    ),
                )
            )
        ) is not None:
            raise ValueError("MCP rollout approval or activation target was already consumed")
        if self._session.scalar(
            select(MCPRolloutBlockResolutionRow.resolution_id).where(
                MCPRolloutBlockResolutionRow.approval_id == activation.approval_id
            )
        ) is not None:
            raise ValueError("MCP rollout approval was already consumed by a block resolution")
        row = MCPRolloutDeploymentActivationRow(
            activation_id=activation.activation_id,
            environment_id=activation.environment_id,
            rollout_program=activation.rollout_program,
            deployment_id=activation.deployment_id,
            stage=stage,
            config_fingerprint=activation.config_fingerprint,
            approval_id=activation.approval_id,
            evidence_id=activation.evidence_id,
            previous_activation_id=activation.previous_activation_id,
            operator_reason=activation.operator_reason,
            is_rollback=activation.is_rollback,
            created_at=activation.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_deployment_activation(row)

    def get_mcp_rollout_deployment_activation(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        config_fingerprint: str,
    ) -> MCPRolloutDeploymentActivation | None:
        normalized_stage = _rollout_value(stage)
        _validate_rollout_scope(environment_id, MCP_ROLLOUT_PROGRAM, normalized_stage)
        row = self._session.scalar(
            select(MCPRolloutDeploymentActivationRow).where(
                MCPRolloutDeploymentActivationRow.environment_id == environment_id,
                MCPRolloutDeploymentActivationRow.rollout_program == MCP_ROLLOUT_PROGRAM,
                MCPRolloutDeploymentActivationRow.deployment_id == deployment_id,
                MCPRolloutDeploymentActivationRow.stage == normalized_stage,
                MCPRolloutDeploymentActivationRow.config_fingerprint == config_fingerprint,
            )
        )
        return None if row is None else _row_to_mcp_rollout_deployment_activation(row)

    def append_mcp_rollout_promotion_block(
        self, block: MCPRolloutPromotionBlock
    ) -> MCPRolloutPromotionBlock:
        stage = _rollout_value(block.stage)
        reason_code = _rollout_value(block.reason_code)
        _validate_rollout_scope(block.environment_id, block.rollout_program, stage)
        if reason_code not in MCP_ROLLOUT_BLOCK_REASONS:
            raise ValueError("MCP rollout promotion block reason is not supported")
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=block.environment_id,
                rollout_program=block.rollout_program,
                created_at=block.created_at,
            )
        )
        evidence = self._session.get(MCPRolloutEvidenceSnapshotRow, block.evidence_id)
        if evidence is None:
            raise ValueError("MCP rollout promotion block evidence does not exist")
        expected_scope = (
            block.environment_id,
            block.rollout_program,
            block.deployment_id,
            stage,
            block.config_fingerprint,
        )
        actual_scope = (
            evidence.environment_id,
            evidence.rollout_program,
            evidence.deployment_id,
            evidence.stage,
            evidence.config_fingerprint,
        )
        if actual_scope != expected_scope:
            raise ValueError("MCP rollout promotion block evidence scope does not match")
        if self._session.get(MCPRolloutPromotionBlockRow, block.block_id) is not None:
            raise ValueError("MCP rollout promotion block replay is not allowed")
        if self._session.scalar(
            select(MCPRolloutPromotionBlockRow.block_id).where(
                MCPRolloutPromotionBlockRow.evidence_id == block.evidence_id,
                MCPRolloutPromotionBlockRow.reason_code == reason_code,
            )
        ) is not None:
            raise ValueError("MCP rollout promotion block was already recorded")
        row = MCPRolloutPromotionBlockRow(
            block_id=block.block_id,
            environment_id=block.environment_id,
            rollout_program=block.rollout_program,
            deployment_id=block.deployment_id,
            stage=stage,
            config_fingerprint=block.config_fingerprint,
            evidence_id=block.evidence_id,
            reason_code=reason_code,
            created_at=block.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_promotion_block(row)

    def list_active_mcp_rollout_promotion_blocks(
        self, environment_id: str, *, rollout_program: str = MCP_ROLLOUT_PROGRAM
    ) -> list[MCPRolloutPromotionBlock]:
        if not environment_id or rollout_program != MCP_ROLLOUT_PROGRAM:
            raise ValueError("MCP rollout block scope is invalid")
        resolution_exists = select(MCPRolloutBlockResolutionRow.resolution_id).where(
            MCPRolloutBlockResolutionRow.block_id == MCPRolloutPromotionBlockRow.block_id
        ).exists()
        rows = self._session.scalars(
            select(MCPRolloutPromotionBlockRow)
            .where(
                MCPRolloutPromotionBlockRow.environment_id == environment_id,
                MCPRolloutPromotionBlockRow.rollout_program == rollout_program,
                ~resolution_exists,
            )
            .order_by(
                MCPRolloutPromotionBlockRow.created_at,
                MCPRolloutPromotionBlockRow.block_id,
            )
        ).all()
        return [_row_to_mcp_rollout_promotion_block(row) for row in rows]

    def append_mcp_rollout_block_resolution(
        self, resolution: MCPRolloutBlockResolution
    ) -> MCPRolloutBlockResolution:
        if not resolution.reason or not resolution.approver:
            raise ValueError("MCP rollout block resolution reason and approver are required")
        block = self._session.get(MCPRolloutPromotionBlockRow, resolution.block_id)
        if block is None:
            raise ValueError("MCP rollout promotion block does not exist")
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=block.environment_id,
                rollout_program=block.rollout_program,
                created_at=resolution.created_at,
            )
        )
        approval = self._session.get(MCPRolloutStageApprovalRow, resolution.approval_id)
        evidence = self._session.get(MCPRolloutEvidenceSnapshotRow, resolution.evidence_id)
        if approval is None or evidence is None:
            raise ValueError("MCP rollout block resolution approval and evidence are required")
        if (
            approval.environment_id != block.environment_id
            or approval.rollout_program != block.rollout_program
            or approval.evidence_id != resolution.evidence_id
            or evidence.environment_id != block.environment_id
            or evidence.rollout_program != block.rollout_program
        ):
            raise ValueError("MCP rollout block resolution scope does not match")
        if self._session.get(MCPRolloutBlockResolutionRow, resolution.resolution_id) is not None:
            raise ValueError("MCP rollout block resolution replay is not allowed")
        if self._session.scalar(
            select(MCPRolloutBlockResolutionRow.resolution_id).where(
                or_(
                    MCPRolloutBlockResolutionRow.block_id == resolution.block_id,
                    MCPRolloutBlockResolutionRow.approval_id == resolution.approval_id,
                )
            )
        ) is not None:
            raise ValueError("MCP rollout block was already resolved")
        if self._session.scalar(
            select(MCPRolloutDeploymentActivationRow.activation_id).where(
                MCPRolloutDeploymentActivationRow.approval_id == resolution.approval_id
            )
        ) is not None:
            raise ValueError("MCP rollout approval was already consumed by an activation")
        row = MCPRolloutBlockResolutionRow(
            resolution_id=resolution.resolution_id,
            block_id=resolution.block_id,
            approval_id=resolution.approval_id,
            evidence_id=resolution.evidence_id,
            reason=resolution.reason,
            approver=resolution.approver,
            created_at=resolution.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_block_resolution(row)

    def save_mcp_rollout_instance_config_lease(
        self, lease: MCPRolloutInstanceConfigLease
    ) -> MCPRolloutInstanceConfigLease:
        stage = _rollout_value(lease.stage)
        _validate_rollout_scope(lease.environment_id, lease.rollout_program, stage)
        if lease.lease_expires_at <= lease.updated_at or lease.updated_at < lease.created_at:
            raise ValueError("MCP rollout instance config lease timestamps are invalid")
        self.ensure_mcp_rollout_gate_scope(
            MCPRolloutGateScope(
                environment_id=lease.environment_id,
                rollout_program=lease.rollout_program,
                created_at=lease.created_at,
            )
        )
        deployment_rows = self._session.scalars(
            select(MCPRolloutInstanceConfigRow).where(
                MCPRolloutInstanceConfigRow.environment_id == lease.environment_id,
                MCPRolloutInstanceConfigRow.rollout_program == lease.rollout_program,
                MCPRolloutInstanceConfigRow.deployment_id == lease.deployment_id,
            )
        ).all()
        for deployment_row in deployment_rows:
            if (
                deployment_row.stage != stage
                or deployment_row.config_fingerprint != lease.config_fingerprint
                or deployment_row.activation_id != lease.activation_id
            ):
                raise ValueError("MCP rollout deployment config fingerprint mismatch")
        activation = self._session.get(
            MCPRolloutDeploymentActivationRow, lease.activation_id
        )
        if activation is None or (
            activation.environment_id,
            activation.rollout_program,
            activation.deployment_id,
            activation.stage,
            activation.config_fingerprint,
        ) != (
            lease.environment_id,
            lease.rollout_program,
            lease.deployment_id,
            stage,
            lease.config_fingerprint,
        ):
            raise ValueError("MCP rollout instance config activation does not match")
        if not bool(activation.is_rollback) and self._has_active_mcp_rollout_blocks(
            lease.environment_id, lease.rollout_program
        ):
            raise ValueError("MCP rollout instance admission is blocked")
        existing = self._session.get(MCPRolloutInstanceConfigRow, lease.instance_config_id)
        natural_existing = self._session.scalar(
            select(MCPRolloutInstanceConfigRow).where(
                MCPRolloutInstanceConfigRow.environment_id == lease.environment_id,
                MCPRolloutInstanceConfigRow.deployment_id == lease.deployment_id,
                MCPRolloutInstanceConfigRow.instance_id == lease.instance_id,
            )
        )
        if existing is not None and natural_existing is not None and existing is not natural_existing:
            raise ValueError("MCP rollout instance config identity conflict")
        row = existing or natural_existing
        if row is not None:
            immutable = (
                row.instance_config_id,
                row.environment_id,
                row.rollout_program,
                row.deployment_id,
                row.instance_id,
                row.stage,
                row.config_fingerprint,
                row.activation_id,
                row.created_at,
            )
            incoming = (
                lease.instance_config_id,
                lease.environment_id,
                lease.rollout_program,
                lease.deployment_id,
                lease.instance_id,
                stage,
                lease.config_fingerprint,
                lease.activation_id,
                lease.created_at,
            )
            if immutable != incoming:
                raise ValueError("MCP rollout instance config immutable fields changed")
            row.lease_expires_at = lease.lease_expires_at
            row.updated_at = lease.updated_at
            self._session.flush()
            return _row_to_mcp_rollout_instance_config(row)
        row = MCPRolloutInstanceConfigRow(
            instance_config_id=lease.instance_config_id,
            environment_id=lease.environment_id,
            rollout_program=lease.rollout_program,
            deployment_id=lease.deployment_id,
            instance_id=lease.instance_id,
            stage=stage,
            config_fingerprint=lease.config_fingerprint,
            activation_id=lease.activation_id,
            lease_expires_at=lease.lease_expires_at,
            created_at=lease.created_at,
            updated_at=lease.updated_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_mcp_rollout_instance_config(row)

    def list_mcp_rollout_instance_config_leases(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        now: datetime | None = None,
    ) -> list[MCPRolloutInstanceConfigLease]:
        conditions = [
            MCPRolloutInstanceConfigRow.environment_id == environment_id,
            MCPRolloutInstanceConfigRow.rollout_program == MCP_ROLLOUT_PROGRAM,
            MCPRolloutInstanceConfigRow.deployment_id == deployment_id,
        ]
        if now is not None:
            conditions.append(MCPRolloutInstanceConfigRow.lease_expires_at > now)
        rows = self._session.scalars(
            select(MCPRolloutInstanceConfigRow)
            .where(*conditions)
            .order_by(MCPRolloutInstanceConfigRow.instance_id)
        ).all()
        return [_row_to_mcp_rollout_instance_config(row) for row in rows]

    def _has_active_mcp_rollout_blocks(
        self, environment_id: str, rollout_program: str
    ) -> bool:
        resolution_exists = select(MCPRolloutBlockResolutionRow.resolution_id).where(
            MCPRolloutBlockResolutionRow.block_id == MCPRolloutPromotionBlockRow.block_id
        ).exists()
        return (
            self._session.scalar(
                select(MCPRolloutPromotionBlockRow.block_id)
                .where(
                    MCPRolloutPromotionBlockRow.environment_id == environment_id,
                    MCPRolloutPromotionBlockRow.rollout_program == rollout_program,
                    ~resolution_exists,
                )
                .limit(1)
            )
            is not None
        )

    def create_or_get_mcp_credential_key_validation(
        self, record: MCPCredentialKeyValidation
    ) -> MCPCredentialKeyValidation:
        values = {
            "validation_id": record.validation_id,
            "singleton_key": 1,
            "validation_nonce": record.validation_nonce,
            "validation_ciphertext": record.validation_ciphertext,
            "encryption_version": record.encryption_version,
            "created_at": record.created_at,
        }
        dialect_name = self._session.get_bind().dialect.name
        statement = postgresql_insert(MCPCredentialKeyValidationRow).values(**values) if dialect_name == "postgresql" else sqlite_insert(MCPCredentialKeyValidationRow).values(**values)
        self._session.execute(statement.on_conflict_do_nothing(index_elements=["singleton_key"]))
        existing = self._session.scalar(
            select(MCPCredentialKeyValidationRow).where(MCPCredentialKeyValidationRow.singleton_key == 1)
        )
        assert existing is not None
        return MCPCredentialKeyValidation(
            validation_id=existing.validation_id,
            validation_nonce=bytes(existing.validation_nonce),
            validation_ciphertext=bytes(existing.validation_ciphertext),
            encryption_version=int(existing.encryption_version),
            created_at=existing.created_at,
        )

    def get_mcp_credential_key_validation(self) -> MCPCredentialKeyValidation | None:
        row = self._session.scalar(select(MCPCredentialKeyValidationRow).limit(1))
        if row is None:
            return None
        return MCPCredentialKeyValidation(
            validation_id=row.validation_id,
            validation_nonce=bytes(row.validation_nonce),
            validation_ciphertext=bytes(row.validation_ciphertext),
            encryption_version=int(row.encryption_version),
            created_at=row.created_at,
        )


class SQLiteCollaborationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_event_record(self, event: EventRecord) -> EventRecord:
        _ensure_event_append_payload_within_rust_contract(event)
        row = EventRecordRow(
            event_id=event.event_id,
            conversation_id=event.conversation_id,
            task_id=event.task_id,
            node_id=event.node_id,
            agent_id=event.agent_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            visibility=event.visibility,
            created_at=event.created_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_event_record(merged)

    def get_event_record(self, event_id: str) -> EventRecord | None:
        row = self._session.get(EventRecordRow, event_id)
        return None if row is None else _row_to_event_record(row)

    def list_events_for_task(self, task_id: str) -> list[EventRecord]:
        event_limit, byte_limit = _ensure_event_replay_policy_compatible_with_rust_contract()
        rows = self._session.scalars(
            select(EventRecordRow)
            .where(EventRecordRow.task_id == task_id)
            .order_by(EventRecordRow.created_at, EventRecordRow.event_id)
            .limit(event_limit + 1)
        ).all()
        events = [_row_to_event_record(row) for row in rows]
        _ensure_event_replay_page_within_rust_contract(events, event_limit, byte_limit)
        return events

    def list_events_for_task_filtered(
        self,
        task_id: str,
        *,
        event_types: Iterable[str] | None = None,
        node_id: str | None = None,
        visibility: EventVisibility | str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        event_limit, byte_limit = _ensure_event_replay_policy_compatible_with_rust_contract()
        resolved_limit = _resolve_event_replay_page_limit(limit, event_limit)
        conditions = [EventRecordRow.task_id == task_id]
        if event_types is not None:
            resolved_event_types = tuple(str(event_type) for event_type in event_types)
            if not resolved_event_types:
                return []
            conditions.append(EventRecordRow.event_type.in_(resolved_event_types))
        if node_id is not None:
            conditions.append(EventRecordRow.node_id == node_id)
        if visibility is not None:
            conditions.append(EventRecordRow.visibility == str(visibility))
        rows = self._session.scalars(
            select(EventRecordRow)
            .where(*conditions)
            .order_by(EventRecordRow.created_at, EventRecordRow.event_id)
            .limit(resolved_limit)
        ).all()
        events = [_row_to_event_record(row) for row in rows]
        _ensure_event_replay_page_within_rust_contract(events, event_limit, byte_limit)
        return events

    def list_event_page_for_task(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        event_limit, byte_limit = _ensure_event_replay_policy_compatible_with_rust_contract()
        resolved_limit = _resolve_event_replay_page_limit(limit, event_limit)
        query = (
            select(EventRecordRow)
            .where(EventRecordRow.task_id == task_id)
            .order_by(EventRecordRow.created_at, EventRecordRow.event_id)
            .limit(resolved_limit)
        )
        if after_event_id is not None:
            cursor = self._session.get(EventRecordRow, after_event_id)
            if cursor is None or cursor.task_id != task_id:
                raise ValueError(f"Unknown event replay cursor: {after_event_id}")
            if cursor.created_at is None:
                query = query.where(
                    or_(
                        EventRecordRow.created_at.is_not(None),
                        (EventRecordRow.created_at.is_(None)) & (EventRecordRow.event_id > after_event_id),
                    )
                )
            else:
                query = query.where(
                    or_(
                        EventRecordRow.created_at > cursor.created_at,
                        (EventRecordRow.created_at == cursor.created_at) & (EventRecordRow.event_id > after_event_id),
                    )
                )
        events = [_row_to_event_record(row) for row in self._session.scalars(query).all()]
        _ensure_event_replay_page_within_rust_contract(events, event_limit, byte_limit)
        return events

    def save_mailbox_message(self, message: MailboxMessage) -> MailboxMessage:
        row = MailboxMessageRow(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            task_id=message.task_id,
            node_id=message.node_id,
            parent_message_id=message.parent_message_id,
            correlation_id=message.correlation_id,
            from_agent=message.from_agent,
            to_agent=message.to_agent,
            to_role=message.to_role,
            channel=message.channel,
            message_type=message.message_type,
            ack_policy=message.ack_policy,
            priority=message.priority,
            payload=dict(message.payload),
            payload_schema_version=message.payload_schema_version,
            created_at=message.created_at,
            resolved_at=message.resolved_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_mailbox_message(merged)

    def get_mailbox_message(self, message_id: str) -> MailboxMessage | None:
        row = self._session.get(MailboxMessageRow, message_id)
        return None if row is None else _row_to_mailbox_message(row)

    def list_mailbox_messages_for_task(self, task_id: str) -> list[MailboxMessage]:
        rows = self._session.scalars(
            select(MailboxMessageRow).where(MailboxMessageRow.task_id == task_id).order_by(
                MailboxMessageRow.created_at, MailboxMessageRow.message_id
            )
        ).all()
        return [_row_to_mailbox_message(row) for row in rows]

    def save_mailbox_delivery(self, delivery: MailboxDelivery) -> MailboxDelivery:
        existing = self._session.scalar(
            select(MailboxDeliveryRow).where(
                MailboxDeliveryRow.message_id == delivery.message_id,
                MailboxDeliveryRow.recipient_agent == delivery.recipient_agent,
            )
        )
        if existing is None:
            row = MailboxDeliveryRow(
                delivery_id=delivery.delivery_id,
                message_id=delivery.message_id,
                recipient_agent=delivery.recipient_agent,
                recipient_role=delivery.recipient_role,
                status=delivery.status,
                attempt_count=delivery.attempt_count,
                max_attempts=delivery.max_attempts,
                ttl_seconds=delivery.ttl_seconds,
                expires_at=delivery.expires_at,
                delivered_at=delivery.delivered_at,
                acknowledged_at=delivery.acknowledged_at,
                resolved_at=delivery.resolved_at,
                next_retry_at=delivery.next_retry_at,
                last_error_code=delivery.last_error_code,
                last_error_message=delivery.last_error_message,
                created_at=delivery.created_at,
                updated_at=delivery.updated_at,
            )
            self._session.add(row)
            self._session.flush()
            return _row_to_mailbox_delivery(row)

        existing.recipient_role = delivery.recipient_role
        existing.status = delivery.status
        existing.attempt_count = delivery.attempt_count
        existing.max_attempts = delivery.max_attempts
        existing.ttl_seconds = delivery.ttl_seconds
        existing.expires_at = delivery.expires_at
        existing.delivered_at = delivery.delivered_at
        existing.acknowledged_at = delivery.acknowledged_at
        existing.resolved_at = delivery.resolved_at
        existing.next_retry_at = delivery.next_retry_at
        existing.last_error_code = delivery.last_error_code
        existing.last_error_message = delivery.last_error_message
        existing.created_at = delivery.created_at
        existing.updated_at = delivery.updated_at
        self._session.flush()
        return _row_to_mailbox_delivery(existing)

    def get_mailbox_delivery(self, delivery_id: str) -> MailboxDelivery | None:
        row = self._session.get(MailboxDeliveryRow, delivery_id)
        return None if row is None else _row_to_mailbox_delivery(row)

    def list_mailbox_deliveries_for_message(self, message_id: str) -> list[MailboxDelivery]:
        rows = self._session.scalars(
            select(MailboxDeliveryRow).where(MailboxDeliveryRow.message_id == message_id).order_by(
                MailboxDeliveryRow.created_at, MailboxDeliveryRow.delivery_id
            )
        ).all()
        return [_row_to_mailbox_delivery(row) for row in rows]

    def save_interrupt(self, interrupt: Interrupt) -> Interrupt:
        existing = self._session.get(InterruptRow, interrupt.interrupt_id)
        incoming_status = str(interrupt.status)
        if (
            existing is not None
            and str(existing.status) in lifecycle_status_list("interrupt_reopen_guard_terminal_statuses")
            and incoming_status == lifecycle_contract_value("interrupt_open_status")
        ):
            return _row_to_interrupt(existing)
        row = InterruptRow(
            interrupt_id=interrupt.interrupt_id,
            conversation_id=interrupt.conversation_id,
            task_id=interrupt.task_id,
            node_id=interrupt.node_id,
            source_agent=interrupt.source_agent,
            source_message_id=interrupt.source_message_id,
            question=interrupt.question,
            reason_code=interrupt.reason_code,
            required_fields=dict(interrupt.required_fields),
            status=interrupt.status,
            expires_at=interrupt.expires_at,
            created_at=interrupt.created_at,
            answered_at=interrupt.answered_at,
            cancelled_at=interrupt.cancelled_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_interrupt(merged)

    def get_interrupt(self, interrupt_id: str) -> Interrupt | None:
        row = self._session.get(InterruptRow, interrupt_id)
        return None if row is None else _row_to_interrupt(row)

    def get_interrupt_for_node(self, task_id: str, node_id: str) -> Interrupt | None:
        row = self._session.scalar(
            select(InterruptRow)
            .where(InterruptRow.task_id == task_id, InterruptRow.node_id == node_id)
            .order_by(InterruptRow.created_at.desc(), InterruptRow.interrupt_id.desc())
        )
        return None if row is None else _row_to_interrupt(row)

    def list_interrupts_for_task(self, task_id: str) -> list[Interrupt]:
        rows = self._session.scalars(
            select(InterruptRow).where(InterruptRow.task_id == task_id).order_by(InterruptRow.created_at, InterruptRow.interrupt_id)
        ).all()
        return [_row_to_interrupt(row) for row in rows]

    def save_interrupt_answer(self, interrupt_answer: InterruptAnswer) -> InterruptAnswer:
        row = InterruptAnswerRow(
            interrupt_answer_id=interrupt_answer.interrupt_answer_id,
            interrupt_id=interrupt_answer.interrupt_id,
            answer_payload=dict(interrupt_answer.answer_payload),
            source_message_id=interrupt_answer.source_message_id,
            accepted=interrupt_answer.accepted,
            created_at=interrupt_answer.created_at,
            accepted_at=interrupt_answer.accepted_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_interrupt_answer(merged)

    def get_interrupt_answer(self, interrupt_answer_id: str) -> InterruptAnswer | None:
        row = self._session.get(InterruptAnswerRow, interrupt_answer_id)
        return None if row is None else _row_to_interrupt_answer(row)

    def list_interrupt_answers(self, interrupt_id: str) -> list[InterruptAnswer]:
        rows = self._session.scalars(
            select(InterruptAnswerRow).where(InterruptAnswerRow.interrupt_id == interrupt_id).order_by(
                InterruptAnswerRow.created_at, InterruptAnswerRow.interrupt_answer_id
            )
        ).all()
        return [_row_to_interrupt_answer(row) for row in rows]

    def save_slot_collection(self, collection: SlotCollection) -> SlotCollection:
        row = SlotCollectionRow(
            collection_id=collection.collection_id,
            **_slot_collection_row_values(collection),
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_slot_collection(merged)

    def get_slot_collection(self, collection_id: str) -> SlotCollection | None:
        row = self._session.get(SlotCollectionRow, collection_id)
        return None if row is None else _row_to_slot_collection(row)

    def get_active_slot_collection_for_node(self, task_id: str, node_id: str) -> SlotCollection | None:
        terminal_statuses = ("completed", "cancelled", "failed")
        row = self._session.scalar(
            select(SlotCollectionRow)
            .where(
                SlotCollectionRow.task_id == task_id,
                SlotCollectionRow.node_id == node_id,
                ~SlotCollectionRow.status.in_(terminal_statuses),
            )
            .order_by(
                SlotCollectionRow.updated_at.desc(),
                SlotCollectionRow.created_at.desc(),
                SlotCollectionRow.collection_id.desc(),
            )
        )
        return None if row is None else _row_to_slot_collection(row)

    def list_slot_collections_for_task(self, task_id: str) -> list[SlotCollection]:
        rows = self._session.scalars(
            select(SlotCollectionRow)
            .where(SlotCollectionRow.task_id == task_id)
            .order_by(SlotCollectionRow.created_at, SlotCollectionRow.collection_id)
        ).all()
        return [_row_to_slot_collection(row) for row in rows]

    def apply_slot_transition(
        self,
        collection_id: str,
        expected_revision: int,
        next_collection: SlotCollection,
        slot_event: SlotEvent,
        *,
        idempotency_key: str | None = None,
    ) -> SlotCollection | None:
        key = idempotency_key or slot_event.idempotency_key
        if key:
            existing_event = self.get_slot_event_by_idempotency_key(collection_id, key)
            if existing_event is not None:
                return self.get_slot_collection(collection_id)
        if next_collection.collection_id != collection_id:
            return None

        result = self._session.execute(
            update(SlotCollectionRow)
            .where(
                SlotCollectionRow.collection_id == collection_id,
                SlotCollectionRow.revision == expected_revision,
            )
            .values(**_slot_collection_row_values(next_collection))
        )
        if result.rowcount != 1:
            self._session.flush()
            return None

        event_to_save = slot_event if key is None or slot_event.idempotency_key == key else replace(slot_event, idempotency_key=key)
        self.append_slot_event(event_to_save)
        row = self._session.get(SlotCollectionRow, collection_id)
        self._session.flush()
        return None if row is None else _row_to_slot_collection(row)

    def append_slot_event(self, event: SlotEvent) -> SlotEvent:
        if event.idempotency_key:
            existing = self.get_slot_event_by_idempotency_key(event.collection_id, event.idempotency_key)
            if existing is not None:
                return existing
        row = SlotEventRow(
            slot_event_id=event.slot_event_id,
            collection_id=event.collection_id,
            task_id=event.task_id,
            node_id=event.node_id,
            conversation_id=event.conversation_id,
            event_type=event.event_type,
            round=event.round,
            revision=event.revision,
            idempotency_key=event.idempotency_key,
            payload_json=dict(event.payload),
            created_at=event.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_slot_event(row)

    def list_slot_events(self, collection_id: str) -> list[SlotEvent]:
        rows = self._session.scalars(
            select(SlotEventRow)
            .where(SlotEventRow.collection_id == collection_id)
            .order_by(SlotEventRow.created_at, SlotEventRow.slot_event_id)
        ).all()
        return [_row_to_slot_event(row) for row in rows]

    def get_slot_event_by_idempotency_key(self, collection_id: str, key: str) -> SlotEvent | None:
        row = self._session.scalar(
            select(SlotEventRow).where(
                SlotEventRow.collection_id == collection_id,
                SlotEventRow.idempotency_key == key,
            )
        )
        return None if row is None else _row_to_slot_event(row)

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        row = CheckpointRow(
            checkpoint_id=checkpoint.checkpoint_id,
            task_id=checkpoint.task_id,
            node_id=checkpoint.node_id,
            agent_id=checkpoint.agent_id,
            snapshot_ref=checkpoint.snapshot_ref,
            snapshot_kind=checkpoint.snapshot_kind,
            resume_token=checkpoint.resume_token,
            source_message_id=checkpoint.source_message_id,
            created_at=checkpoint.created_at,
            invalidated_at=checkpoint.invalidated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_checkpoint(merged)

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        row = self._session.get(CheckpointRow, checkpoint_id)
        return None if row is None else _row_to_checkpoint(row)

    def get_checkpoint_by_resume_token(self, resume_token: str) -> Checkpoint | None:
        row = self._session.scalar(
            select(CheckpointRow).where(CheckpointRow.resume_token == resume_token).order_by(
                CheckpointRow.created_at.desc(), CheckpointRow.checkpoint_id.desc()
            )
        )
        return None if row is None else _row_to_checkpoint(row)

    def list_checkpoints_for_task(self, task_id: str) -> list[Checkpoint]:
        rows = self._session.scalars(
            select(CheckpointRow).where(CheckpointRow.task_id == task_id).order_by(CheckpointRow.created_at, CheckpointRow.checkpoint_id)
        ).all()
        return [_row_to_checkpoint(row) for row in rows]


class SQLiteStorage(StoragePort):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        runtime_sidecar_client: Any | None = None,
        runtime_sidecar_shadow_sink: RuntimeSidecarShadowSink | None = None,
        mcp_task_authority_mode: str | None = None,
    ) -> None:
        if mcp_task_authority_mode not in {None, "off", "shadow", "enforce"}:
            raise ValueError(
                "mcp_task_authority_mode must be one of: off, shadow, enforce"
            )
        if (
            mcp_task_authority_mode in {"shadow", "enforce"}
            and runtime_sidecar_client is None
        ):
            raise RuntimeError(
                "runtime_store_unavailable: MCP Task authority requires a "
                "Rust runtime sidecar client"
            )
        if (
            mcp_task_authority_mode == "shadow"
            and runtime_sidecar_shadow_sink is None
        ):
            raise RuntimeError(
                "runtime_store_unavailable: MCP Task shadow authority requires a "
                "runtime sidecar comparison sink"
            )
        self._session_factory = session_factory
        self._runtime_sidecar_client = runtime_sidecar_client
        self._runtime_sidecar_shadow_sink = runtime_sidecar_shadow_sink
        self._mcp_task_authority_mode = mcp_task_authority_mode

    async def list_user_mcp_servers(self, owner_user_id: str) -> list[UserMCPServer]:
        return await self._run(lambda state, collab: state.list_user_mcp_servers(owner_user_id))

    async def get_user_mcp_server(self, owner_user_id: str, server_id: str) -> UserMCPServer | None:
        return await self._run(lambda state, collab: state.get_user_mcp_server(owner_user_id, server_id))

    async def create_user_mcp_server(
        self, server: UserMCPServer, credential: UserMCPCredentialRecord | None = None
    ) -> UserMCPServer:
        return await self._run(lambda state, collab: state.create_user_mcp_server(server, credential))

    async def create_user_mcp_servers_atomic(
        self,
        candidates: Sequence[tuple[UserMCPServer, UserMCPCredentialRecord | None]],
    ) -> list[UserMCPServer]:
        return await self._run(
            lambda state, collab: state.create_user_mcp_servers_atomic(candidates)
        )

    async def apply_legacy_mcp_migration_atomic(
        self,
        candidates: Sequence[
            tuple[
                UserMCPServer,
                UserMCPCredentialRecord | None,
                MCPLegacyMigrationRecord,
            ]
        ],
    ) -> MCPLegacyMigrationBatchResult:
        return await self._run(
            lambda state, collab: state.apply_legacy_mcp_migration_atomic(candidates)
        )

    async def get_mcp_legacy_migration_record(
        self, migration_id: str
    ) -> MCPLegacyMigrationRecord | None:
        return await self._run(
            lambda state, collab: state.get_mcp_legacy_migration_record(
                migration_id
            )
        )

    async def update_user_mcp_server(
        self, owner_user_id: str, server_id: str, *, changes: Mapping[str, Any],
        credential_operation: str = "retain", credential: UserMCPCredentialRecord | None = None,
        security_sensitive: bool = False, expected_config_version: int | None = None,
        expected_security_version: int | None = None, updated_at: datetime
    ) -> UserMCPServer | None:
        return await self._run(
            lambda state, collab: state.update_user_mcp_server(
                owner_user_id, server_id, changes=changes, credential_operation=credential_operation,
                credential=credential, security_sensitive=security_sensitive,
                expected_config_version=expected_config_version,
                expected_security_version=expected_security_version,
                updated_at=updated_at,
            )
        )

    async def get_user_mcp_credential(
        self, owner_user_id: str, server_id: str
    ) -> UserMCPCredentialRecord | None:
        return await self._run(lambda state, collab: state.get_user_mcp_credential(owner_user_id, server_id))

    async def claim_user_mcp_health_attempt(self, attempt: UserMCPHealthAttempt) -> bool:
        return await self._run(lambda state, collab: state.claim_user_mcp_health_attempt(attempt))

    async def renew_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        return await self._run(
            lambda state, collab: state.renew_user_mcp_health_attempt(
                attempt_id, owner_user_id, server_id, runner_instance_id=runner_instance_id,
                config_version=config_version, security_version=security_version,
                lease_expires_at=lease_expires_at, updated_at=updated_at,
            )
        )

    async def complete_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, health_status: str, error_code: str | None,
        completed_at: datetime
    ) -> UserMCPServer | None:
        return await self._run(
            lambda state, collab: state.complete_user_mcp_health_attempt(
                attempt_id, owner_user_id, server_id, runner_instance_id=runner_instance_id,
                config_version=config_version, security_version=security_version,
                health_status=health_status, error_code=error_code, completed_at=completed_at,
            )
        )

    async def expire_user_mcp_health_attempts(
        self, *, now: datetime, error_code: str = "test_interrupted"
    ) -> int:
        return await self._run(
            lambda state, collab: state.expire_user_mcp_health_attempts(now=now, error_code=error_code)
        )

    async def release_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
    ) -> bool:
        return await self._run(
            lambda state, collab: state.release_user_mcp_health_attempt(
                attempt_id,
                owner_user_id,
                server_id,
                runner_instance_id=runner_instance_id,
                config_version=config_version,
                security_version=security_version,
            )
        )

    async def acquire_user_mcp_scope_lease(self, lease: UserMCPScopeLease) -> bool:
        return await self._run(lambda state, collab: state.acquire_user_mcp_scope_lease(lease))

    async def renew_user_mcp_scope_lease(
        self, scope_id: str, owner_user_id: str, server_id: str, *, gateway_instance_id: str,
        security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        return await self._run(
            lambda state, collab: state.renew_user_mcp_scope_lease(
                scope_id, owner_user_id, server_id, gateway_instance_id=gateway_instance_id,
                security_version=security_version, lease_expires_at=lease_expires_at, updated_at=updated_at,
            )
        )

    async def release_user_mcp_scope_lease(self, scope_id: str, *, gateway_instance_id: str) -> bool:
        return await self._run(
            lambda state, collab: state.release_user_mcp_scope_lease(scope_id, gateway_instance_id=gateway_instance_id)
        )

    async def list_live_user_mcp_scope_leases(
        self, *, now: datetime, owner_user_id: str | None = None, server_id: str | None = None
    ) -> list[UserMCPScopeLease]:
        return await self._run(
            lambda state, collab: state.list_live_user_mcp_scope_leases(
                now=now, owner_user_id=owner_user_id, server_id=server_id,
            )
        )

    async def expire_user_mcp_scope_leases(self, *, now: datetime) -> int:
        return await self._run(lambda state, collab: state.expire_user_mcp_scope_leases(now=now))

    async def mark_user_mcp_server_deleted(
        self, owner_user_id: str, server_id: str, *, deleted_at: datetime
    ) -> UserMCPServer | None:
        return await self._run(
            lambda state, collab: state.mark_user_mcp_server_deleted(
                owner_user_id, server_id, deleted_at=deleted_at,
            )
        )

    async def list_pending_user_mcp_server_deletions(self) -> list[UserMCPServer]:
        return await self._run(lambda state, collab: state.list_pending_user_mcp_server_deletions())

    async def finalize_user_mcp_server_delete(
        self, owner_user_id: str, server_id: str, *, now: datetime
    ) -> bool:
        return await self._run(
            lambda state, collab: state.finalize_user_mcp_server_delete(owner_user_id, server_id, now=now)
        )

    async def save_user_mcp_tool_grant(self, grant: UserMCPToolGrant) -> UserMCPToolGrant:
        return await self._run(lambda state, collab: state.save_user_mcp_tool_grant(grant))

    async def list_user_mcp_tool_grants(
        self, owner_user_id: str, server_id: str | None = None
    ) -> list[UserMCPToolGrant]:
        return await self._run(lambda state, collab: state.list_user_mcp_tool_grants(owner_user_id, server_id))

    async def get_valid_user_mcp_tool_grant(
        self,
        owner_user_id: str,
        server_id: str,
        tool_name: str,
        *,
        server_security_version: int,
        input_schema_sha256: str,
    ) -> UserMCPToolGrant | None:
        return await self._run(
            lambda state, collab: state.get_valid_user_mcp_tool_grant(
                owner_user_id,
                server_id,
                tool_name,
                server_security_version=server_security_version,
                input_schema_sha256=input_schema_sha256,
            )
        )

    async def delete_user_mcp_tool_grant(
        self, owner_user_id: str, server_id: str, grant_id: str
    ) -> bool:
        return await self._run(
            lambda state, collab: state.delete_user_mcp_tool_grant(owner_user_id, server_id, grant_id)
        )

    async def delete_user_mcp_tool_grant_by_id(self, owner_user_id: str, grant_id: str) -> bool:
        return await self._run(
            lambda state, collab: state.delete_user_mcp_tool_grant_by_id(owner_user_id, grant_id)
        )

    async def clear_user_mcp_tool_grants(self, owner_user_id: str, server_id: str) -> int:
        return await self._run(
            lambda state, collab: state.clear_user_mcp_tool_grants(owner_user_id, server_id)
        )

    async def invalidate_user_mcp_tool_grants(
        self,
        owner_user_id: str,
        server_id: str,
        *,
        invalidated_at: datetime,
        invalid_reason: str,
        tool_name: str | None = None,
        input_schema_sha256: str | None = None,
    ) -> int:
        return await self._run(
            lambda state, collab: state.invalidate_user_mcp_tool_grants(
                owner_user_id,
                server_id,
                invalidated_at=invalidated_at,
                invalid_reason=invalid_reason,
                tool_name=tool_name,
                input_schema_sha256=input_schema_sha256,
            )
        )

    async def save_mcp_branch_record(self, record: MCPBranchRecord) -> MCPBranchRecord:
        return await self._run(lambda state, collab: state.save_mcp_branch_record(record))

    async def get_mcp_branch_record(
        self, owner_user_id: str, task_id: str, branch_id: str
    ) -> MCPBranchRecord | None:
        return await self._run(
            lambda state, collab: state.get_mcp_branch_record(owner_user_id, task_id, branch_id)
        )

    async def list_mcp_branch_records(
        self,
        owner_user_id: str,
        *,
        task_id: str | None = None,
        statuses: tuple[str, ...] = (),
    ) -> list[MCPBranchRecord]:
        return await self._run(
            lambda state, collab: state.list_mcp_branch_records(
                owner_user_id, task_id=task_id, statuses=statuses
            )
        )

    async def reserve_mcp_call(self, record: MCPCallRecord) -> bool:
        return await self._run(lambda state, collab: state.reserve_mcp_call(record))

    async def mark_mcp_call_may_have_dispatched(
        self, owner_user_id: str, task_id: str, call_ref: str, *, updated_at: datetime
    ) -> bool:
        return await self._run(
            lambda state, collab: state.mark_mcp_call_may_have_dispatched(
                owner_user_id, task_id, call_ref, updated_at=updated_at
            )
        )

    async def get_mcp_call_record(
        self, owner_user_id: str, task_id: str, call_ref: str
    ) -> MCPCallRecord | None:
        return await self._run(
            lambda state, collab: state.get_mcp_call_record(owner_user_id, task_id, call_ref)
        )

    async def list_mcp_call_records(
        self, owner_user_id: str, task_id: str, *, branch_id: str | None = None
    ) -> list[MCPCallRecord]:
        return await self._run(
            lambda state, collab: state.list_mcp_call_records(
                owner_user_id, task_id, branch_id=branch_id
            )
        )

    async def finish_mcp_call(
        self,
        owner_user_id: str,
        task_id: str,
        call_ref: str,
        *,
        status: str,
        terminal_at: datetime,
        result_ref: str | None = None,
        output_size_bytes: int | None = None,
        safe_error_code: str | None = None,
    ) -> MCPCallRecord | None:
        return await self._run(
            lambda state, collab: state.finish_mcp_call(
                owner_user_id,
                task_id,
                call_ref,
                status=status,
                terminal_at=terminal_at,
                result_ref=result_ref,
                output_size_bytes=output_size_bytes,
                safe_error_code=safe_error_code,
            )
        )

    async def converge_dispatched_mcp_calls_to_unknown(
        self, *, now: datetime, limit: int = 1000
    ) -> list[MCPCallRecord]:
        return await self._run(
            lambda state, collab: state.converge_dispatched_mcp_calls_to_unknown(
                now=now, limit=limit
            )
        )

    async def count_active_mcp_remote_task_bindings(
        self, *, rollout_config_version: str, protocol_version: str
    ) -> int:
        if self._task_authority_mode() == "enforce":
            task_ids = await self._run(
                lambda state, collab: state.list_active_mcp_remote_task_binding_task_ids(
                    protocol_version=protocol_version,
                )
            )
            tasks: dict[str, Task | None] = {}
            for task_id in set(task_ids):
                tasks[task_id] = await self.get_task(task_id)
            return sum(
                1
                for task_id in task_ids
                if (
                    (task := tasks[task_id]) is not None
                    and task.mcp_execution_mode == "user_scoped"
                    and task.mcp_rollout_mode == "enforce"
                    and task.mcp_rollout_config_version == rollout_config_version
                )
            )
        return await self._run(
            lambda state, collab: state.count_active_mcp_remote_task_bindings(
                rollout_config_version=rollout_config_version,
                protocol_version=protocol_version,
            )
        )

    async def save_mcp_remote_task_binding(
        self, binding: MCPRemoteTaskBinding
    ) -> MCPRemoteTaskBinding:
        return await self._run(
            lambda state, collab: state.save_mcp_remote_task_binding(binding)
        )

    async def get_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> MCPRemoteTaskBinding | None:
        return await self._run(
            lambda state, collab: state.get_mcp_remote_task_binding(
                owner_user_id, task_id, safe_remote_task_ref
            )
        )

    async def get_mcp_remote_task_binding_for_call(
        self, owner_user_id: str, task_id: str, call_ref: str
    ) -> MCPRemoteTaskBinding | None:
        return await self._run(
            lambda state, collab: state.get_mcp_remote_task_binding_for_call(
                owner_user_id, task_id, call_ref
            )
        )

    async def publish_mcp_remote_task_binding(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        published_at: datetime,
        continuation_plan: Mapping[str, Any] | None = None,
    ) -> MCPRemoteTaskBinding | None:
        node = None
        binding = await self.get_mcp_remote_task_binding(
            owner_user_id, task_id, safe_remote_task_ref
        )
        if binding is not None:
            node = await self.get_task_node(binding.node_id)
        if binding is None or node is None or node.status != NodeStatus.WAITING_FOR_DEPENDENCY:
            return None
        confirmed = await self.compare_and_set_task_node(
            node, expected_from_status=NodeStatus.WAITING_FOR_DEPENDENCY
        )
        if confirmed is None:
            return None
        return await self._run(
            lambda state, collab: state.publish_mcp_remote_task_binding(
                owner_user_id,
                task_id,
                safe_remote_task_ref,
                published_at=published_at,
                continuation_plan=continuation_plan,
            )
        )

    async def reconcile_unpublished_mcp_remote_task_bindings(
        self, *, now: datetime, limit: int = 1000
    ) -> int:
        bindings = await self._run(
            lambda state, collab: state.list_unpublished_mcp_remote_task_bindings(
                limit=limit
            )
        )
        reconciled = 0
        for binding in bindings:
            node = await self.get_task_node(binding.node_id)
            if node is not None and node.status == NodeStatus.WAITING_FOR_DEPENDENCY:
                published = await self.publish_mcp_remote_task_binding(
                    binding.owner_user_id,
                    binding.task_id,
                    binding.safe_remote_task_ref,
                    published_at=now,
                )
                reconciled += int(published is not None)
                continue
            failed = await self._run(
                lambda state, collab, current=binding: state.fail_unpublished_mcp_remote_task_binding(
                    current, terminal_at=now
                )
            )
            reconciled += int(failed is not None)
        return reconciled

    async def list_due_mcp_remote_task_bindings(
        self, *, now: datetime, limit: int = 100
    ) -> list[MCPRemoteTaskBinding]:
        return await self._run(
            lambda state, collab: state.list_due_mcp_remote_task_bindings(now=now, limit=limit)
        )

    async def claim_due_mcp_remote_task_bindings(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskBinding]:
        return await self._run(
            lambda state, collab: state.claim_due_mcp_remote_task_bindings(
                claim_owner=claim_owner,
                claim_token=claim_token,
                now=now,
                lease_expires_at=lease_expires_at,
                limit=limit,
            )
        )

    async def renew_mcp_remote_task_binding_claim(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None:
        return await self._run(
            lambda state, collab: state.renew_mcp_remote_task_binding_claim(
                owner_user_id,
                task_id,
                safe_remote_task_ref,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                lease_expires_at=lease_expires_at,
                updated_at=updated_at,
            )
        )

    async def release_mcp_remote_task_binding_claim(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None:
        return await self._run(
            lambda state, collab: state.release_mcp_remote_task_binding_claim(
                owner_user_id,
                task_id,
                safe_remote_task_ref,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                updated_at=updated_at,
            )
        )

    async def update_mcp_remote_task_binding_status(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        last_status: str,
        next_poll_at: datetime | None,
        updated_at: datetime,
        terminal_at: datetime | None = None,
    ) -> MCPRemoteTaskBinding | None:
        return await self._run(
            lambda state, collab: state.update_mcp_remote_task_binding_status(
                owner_user_id,
                task_id,
                safe_remote_task_ref,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                last_status=last_status,
                next_poll_at=next_poll_at,
                updated_at=updated_at,
                terminal_at=terminal_at,
            )
        )

    async def finish_mcp_remote_task_binding(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        remote_status: str,
        call_status: str,
        terminal_at: datetime,
        result_ref: str | None = None,
        safe_error_code: str | None = None,
    ) -> MCPRemoteTaskBinding | None:
        return await self._run(
            lambda state, collab: state.finish_mcp_remote_task_binding(
                owner_user_id,
                task_id,
                safe_remote_task_ref,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                remote_status=remote_status,
                call_status=call_status,
                terminal_at=terminal_at,
                result_ref=result_ref,
                safe_error_code=safe_error_code,
            )
        )

    async def claim_mcp_remote_task_outbox(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]:
        return await self._run(
            lambda state, collab: state.claim_mcp_remote_task_outbox(
                claim_owner=claim_owner,
                claim_token=claim_token,
                now=now,
                lease_expires_at=lease_expires_at,
                limit=limit,
            )
        )

    async def claim_abandoned_mcp_remote_task_controls(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]:
        return await self._run(
            lambda state, collab: state.claim_abandoned_mcp_remote_task_controls(
                claim_owner=claim_owner,
                claim_token=claim_token,
                now=now,
                limit=limit,
            )
        )

    async def pause_mcp_remote_task_for_input(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        input_requests: Mapping[str, Any],
        conversation_id: str,
        source_message_id: str,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None:
        existing = await self.get_mcp_remote_task_binding(
            owner_user_id, task_id, safe_remote_task_ref
        )
        if existing is None:
            return None
        node = await self.get_task_node(existing.node_id)
        if node is None or node.status not in {
            NodeStatus.WAITING_FOR_DEPENDENCY,
            NodeStatus.WAITING_FOR_INPUT,
        }:
            return None
        transitioned = await self.compare_and_set_task_node(
            replace(node, status=NodeStatus.WAITING_FOR_INPUT),
            expected_from_status=node.status,
        )
        if transitioned is None:
            return None
        binding = await self._run(
            lambda state, collab: state.pause_mcp_remote_task_for_input(
                owner_user_id,
                task_id,
                safe_remote_task_ref,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                input_requests=input_requests,
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                updated_at=updated_at,
            )
        )
        return binding

    async def begin_mcp_remote_task_control_delivery(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.begin_mcp_remote_task_control_delivery(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                lease_expires_at=lease_expires_at,
                updated_at=updated_at,
            )
        )

    async def enqueue_mcp_remote_task_control(
        self,
        answer: InterruptAnswer,
        *,
        action: str,
        input_responses: Mapping[str, Any],
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        interrupt = await self.get_interrupt(answer.interrupt_id)
        if interrupt is None or interrupt.reason_code != "mcp_remote_task_input_required":
            return None
        task = await self.get_task(interrupt.task_id)
        if task is None:
            return None
        conversation = await self.get_conversation(task.conversation_id)
        safe_ref = str(interrupt.required_fields.get("safe_remote_task_ref") or "").strip()
        if conversation is None or not safe_ref:
            return None
        binding = await self.get_mcp_remote_task_binding(
            conversation.username, task.task_id, safe_ref
        )
        if binding is None:
            return None
        expected_outbox = await self._run(
            lambda state, collab: state.get_mcp_remote_task_outbox(
                f"mcp-remote-input:{binding.call_ref}"
            )
        )
        if (
            expected_outbox is None
            or expected_outbox.kind != "awaiting_input"
            or expected_outbox.status != "awaiting_input"
        ):
            return None
        node = await self.get_task_node(binding.node_id)
        if node is None or node.status not in {
            NodeStatus.WAITING_FOR_INPUT,
            NodeStatus.WAITING_FOR_DEPENDENCY,
        }:
            return None
        transitioned = await self.compare_and_set_task_node(
            replace(node, status=NodeStatus.WAITING_FOR_DEPENDENCY),
            expected_from_status=node.status,
        )
        if transitioned is None:
            return None
        command = await self._run(
            lambda state, collab: state.enqueue_mcp_remote_task_control(
                answer,
                action=action,
                input_responses=input_responses,
                updated_at=updated_at,
            )
        )
        if command is None:
            raise RuntimeError("mcp_remote_task_control_aggregate_conflict")
        return command

    async def apply_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        outbox = await self._run(
            lambda state, collab: state.get_mcp_remote_task_outbox(outbox_id)
        )

        if (
            outbox is None
            or outbox.claim_owner != claim_owner
            or outbox.claim_token != claim_token
            or outbox.revision != expected_revision
            or outbox.kind != "terminal_continuation"
        ):
            return None
        node = await self.get_task_node(outbox.node_id)
        task = await self.get_task(outbox.task_id)
        if node is None or task is None:
            return None
        call_status = str(outbox.payload.get("call_status") or "unknown")
        result_ref = str(outbox.payload.get("result_ref") or "").strip()
        target_node_status = {
            "completed": NodeStatus.COMPLETED,
            "failed": NodeStatus.FAILED,
            "cancelled": NodeStatus.CANCELLED,
        }.get(call_status, NodeStatus.FAILED)
        if node.status not in {
            NodeStatus.WAITING_FOR_DEPENDENCY,
            NodeStatus.RUNNING,
            target_node_status,
        }:
            return None
        target_node = replace(
            node,
            status=target_node_status,
            output_refs=(result_ref,) if result_ref else node.output_refs,
            finished_at=node.finished_at or updated_at,
        )
        if node.status != target_node_status:
            saved_node = await self.compare_and_set_task_node(
                target_node,
                expected_from_status=node.status,
            )
            if saved_node is None:
                return None
        if call_status != "completed":
            target_task_status = (
                TaskStatus.CANCELLED
                if call_status == "cancelled"
                else TaskStatus.FAILED
            )
            if task.status not in {
                TaskStatus.ACCEPTED,
                TaskStatus.PLANNING,
                TaskStatus.RUNNING,
                TaskStatus.CANCELLING,
                target_task_status,
            }:
                return None
            if task.status != target_task_status:
                saved_task = await self._transition_mcp_recovery_task_terminal(
                    task, target_status=target_task_status, updated_at=updated_at
                )
                if saved_task is None:
                    return None
        return await self._run(
            lambda state, collab: state.apply_mcp_remote_task_continuation(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                updated_at=updated_at,
            )
        )

    async def get_mcp_remote_task_outbox(
        self, outbox_id: str
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.get_mcp_remote_task_outbox(outbox_id)
        )

    async def complete_mcp_remote_task_outbox(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.complete_mcp_remote_task_outbox(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                completed_at=completed_at,
            )
        )

    async def admit_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        admitted_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.admit_mcp_remote_task_continuation(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                admitted_at=admitted_at,
            )
        )

    async def mark_mcp_remote_task_continuation_dispatched(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        dispatched_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.mark_mcp_remote_task_continuation_dispatched(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                dispatched_at=dispatched_at,
            )
        )

    async def claim_mcp_remote_task_continuations(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]:
        return await self._run(
            lambda state, collab: state.claim_mcp_remote_task_continuations(
                claim_owner=claim_owner,
                claim_token=claim_token,
                now=now,
                lease_expires_at=lease_expires_at,
                limit=limit,
            )
        )

    async def begin_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        started_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.begin_mcp_remote_task_continuation(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                started_at=started_at,
            )
        )

    async def abandon_expired_mcp_remote_task_continuations(
        self, *, now: datetime, limit: int = 100
    ) -> list[MCPRemoteTaskOutbox]:
        return await self._run(
            lambda state, collab: state.abandon_expired_mcp_remote_task_continuations(
                now=now, limit=limit
            )
        )

    async def complete_abandoned_mcp_remote_task_continuation(
        self, outbox_id: str, *, expected_revision: int, completed_at: datetime
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.complete_abandoned_mcp_remote_task_continuation(
                outbox_id,
                expected_revision=expected_revision,
                completed_at=completed_at,
            )
        )

    async def renew_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        node_ids: tuple[str, ...] | None = None,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        return await self._run(
            lambda state, collab: state.renew_mcp_remote_task_continuation(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                lease_expires_at=lease_expires_at,
                node_ids=node_ids,
                updated_at=updated_at,
            )
        )

    async def complete_mcp_remote_task_control(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        outcome: str,
        completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None:
        outbox = await self._run(
            lambda state, collab: state.get_mcp_remote_task_outbox(outbox_id)
        )
        if (
            outbox is None
            or outbox.claim_owner != claim_owner
            or outbox.claim_token != claim_token
            or outbox.revision != expected_revision
        ):
            return None
        terminal = outcome == "ambiguous" or outbox.kind == "control_cancel"
        if terminal:
            node = await self.get_task_node(outbox.node_id)
            task = await self.get_task(outbox.task_id)
            if node is None or task is None:
                return None
            cancelled = outcome == "delivered" and outbox.kind == "control_cancel"
            node_status = NodeStatus.CANCELLED if cancelled else NodeStatus.FAILED
            task_status = TaskStatus.CANCELLED if cancelled else TaskStatus.FAILED
            if node.status not in {NodeStatus.WAITING_FOR_DEPENDENCY, node_status}:
                return None
            if node.status != node_status:
                saved_node = await self.compare_and_set_task_node(
                    replace(
                        node,
                        status=node_status,
                        finished_at=node.finished_at or completed_at,
                    ),
                    expected_from_status=NodeStatus.WAITING_FOR_DEPENDENCY,
                )
                if saved_node is None:
                    return None
            if task.status not in {
                TaskStatus.ACCEPTED,
                TaskStatus.PLANNING,
                TaskStatus.RUNNING,
                TaskStatus.CANCELLING,
                task_status,
            }:
                return None
            if task.status != task_status:
                saved_task = await self._transition_mcp_recovery_task_terminal(
                    task, target_status=task_status, updated_at=completed_at
                )
                if saved_task is None:
                    return None
        return await self._run(
            lambda state, collab: state.complete_mcp_remote_task_control(
                outbox_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
                expected_revision=expected_revision,
                outcome=outcome,
                completed_at=completed_at,
            )
        )

    async def _transition_mcp_recovery_task_terminal(
        self,
        task: Task,
        *,
        target_status: TaskStatus,
        updated_at: datetime,
    ) -> Task | None:
        if target_status != TaskStatus.CANCELLED:
            return await self.compare_and_set_task(
                replace(task, status=target_status, updated_at=updated_at),
                expected_from_status=task.status,
            )
        current = task
        if current.status != TaskStatus.CANCELLING:
            current = await self.compare_and_set_task(
                replace(
                    current,
                    status=TaskStatus.CANCELLING,
                    cancel_requested_at=current.cancel_requested_at or updated_at,
                    updated_at=updated_at,
                ),
                expected_from_status=current.status,
            )
            if current is None:
                return None
        return await self.compare_and_set_task(
            replace(current, status=TaskStatus.CANCELLED, updated_at=updated_at),
            expected_from_status=TaskStatus.CANCELLING,
        )

    async def delete_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> bool:
        return await self._run(
            lambda state, collab: state.delete_mcp_remote_task_binding(
                owner_user_id, task_id, safe_remote_task_ref
            )
        )

    async def save_mcp_sealed_state(self, state: MCPSealedState) -> MCPSealedState:
        return await self._run(lambda storage, collab: storage.save_mcp_sealed_state(state))

    async def get_mcp_sealed_state(
        self, owner_user_id: str, task_id: str, sealed_state_ref: str
    ) -> MCPSealedState | None:
        return await self._run(
            lambda state, collab: state.get_mcp_sealed_state(
                owner_user_id, task_id, sealed_state_ref
            )
        )

    async def delete_mcp_sealed_state(
        self, owner_user_id: str, task_id: str, sealed_state_ref: str
    ) -> bool:
        return await self._run(
            lambda state, collab: state.delete_mcp_sealed_state(
                owner_user_id, task_id, sealed_state_ref
            )
        )

    async def save_mcp_connection_lease(
        self, lease: MCPConnectionLease
    ) -> MCPConnectionLease:
        return await self._run(lambda state, collab: state.save_mcp_connection_lease(lease))

    async def list_live_mcp_connection_leases(
        self, owner_user_id: str, task_id: str, *, now: datetime
    ) -> list[MCPConnectionLease]:
        return await self._run(
            lambda state, collab: state.list_live_mcp_connection_leases(
                owner_user_id, task_id, now=now
            )
        )

    async def delete_mcp_connection_lease(
        self, owner_user_id: str, task_id: str, connection_id: str
    ) -> bool:
        return await self._run(
            lambda state, collab: state.delete_mcp_connection_lease(
                owner_user_id, task_id, connection_id
            )
        )

    async def expire_mcp_connection_leases(self, *, now: datetime, limit: int = 1000) -> int:
        return await self._run(
            lambda state, collab: state.expire_mcp_connection_leases(now=now, limit=limit)
        )

    async def append_mcp_audit_event(self, event: MCPAuditEvent) -> MCPAuditEvent:
        return await self._run(lambda state, collab: state.append_mcp_audit_event(event))

    async def list_mcp_audit_events(
        self, owner_user_id: str, *, task_id: str | None = None, limit: int = 100
    ) -> list[MCPAuditEvent]:
        return await self._run(
            lambda state, collab: state.list_mcp_audit_events(
                owner_user_id, task_id=task_id, limit=limit
            )
        )

    async def delete_expired_mcp_audit_events(
        self, *, now: datetime, limit: int = 1000
    ) -> int:
        return await self._run(
            lambda state, collab: state.delete_expired_mcp_audit_events(now=now, limit=limit)
        )

    async def ensure_mcp_rollout_gate_scope(
        self, scope: MCPRolloutGateScope
    ) -> MCPRolloutGateScope:
        return await self._run(
            lambda state, collab: state.ensure_mcp_rollout_gate_scope(scope)
        )

    async def append_mcp_rollout_drill_observation(
        self, observation: MCPRolloutDrillObservation
    ) -> MCPRolloutDrillObservation:
        return await self._run(
            lambda state, collab: state.append_mcp_rollout_drill_observation(
                observation
            )
        )

    async def list_mcp_rollout_drill_observations(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPRolloutDrillObservation]:
        return await self._run(
            lambda state, collab: state.list_mcp_rollout_drill_observations(
                environment_id,
                deployment_id,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
            )
        )

    async def upsert_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket:
        return await self._run(
            lambda state, collab: state.upsert_mcp_rollout_metric_bucket(bucket)
        )

    async def set_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket:
        return await self._run(
            lambda state, collab: state.set_mcp_rollout_metric_bucket(bucket)
        )

    async def list_mcp_rollout_metric_buckets(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPRolloutMetricBucket]:
        return await self._run(
            lambda state, collab: state.list_mcp_rollout_metric_buckets(
                environment_id,
                deployment_id,
                stage,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
            )
        )

    async def save_mcp_shadow_audit_sample(
        self, sample: MCPShadowAuditSample
    ) -> MCPShadowAuditSample:
        return await self._run(
            lambda state, collab: state.save_mcp_shadow_audit_sample(sample)
        )

    async def list_mcp_shadow_audit_samples(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPShadowAuditSample]:
        return await self._run(
            lambda state, collab: state.list_mcp_shadow_audit_samples(
                environment_id,
                deployment_id,
                stage,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
            )
        )

    async def produce_mcp_shadow_evidence_snapshot(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
        builder: Callable[
            [list[MCPShadowAuditSample], list[MCPRolloutMetricBucket]],
            MCPRolloutEvidenceSnapshot,
        ],
    ) -> MCPRolloutEvidenceSnapshot:
        def produce(state: SQLiteStateRepository, collab: Any) -> MCPRolloutEvidenceSnapshot:
            del collab
            samples = state.list_mcp_shadow_audit_samples(
                environment_id,
                deployment_id,
                "internal_shadow",
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
            )
            metrics = state.list_mcp_rollout_metric_buckets(
                environment_id,
                deployment_id,
                "internal_shadow",
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
            )
            return state.append_mcp_rollout_evidence_snapshot(builder(samples, metrics))

        return await self._run(produce)

    async def delete_expired_mcp_shadow_audit_samples(
        self, *, now: datetime, limit: int = 1000
    ) -> int:
        return await self._run(
            lambda state, collab: state.delete_expired_mcp_shadow_audit_samples(
                now=now, limit=limit
            )
        )

    async def append_mcp_rollout_evidence_snapshot(
        self, snapshot: MCPRolloutEvidenceSnapshot
    ) -> MCPRolloutEvidenceSnapshot:
        return await self._run(
            lambda state, collab: state.append_mcp_rollout_evidence_snapshot(snapshot)
        )

    async def get_mcp_rollout_evidence_snapshot(
        self, evidence_id: str
    ) -> MCPRolloutEvidenceSnapshot | None:
        return await self._run(
            lambda state, collab: state.get_mcp_rollout_evidence_snapshot(evidence_id)
        )

    async def list_mcp_rollout_evidence_snapshots(
        self, environment_id: str, deployment_id: str, stage: str
    ) -> list[MCPRolloutEvidenceSnapshot]:
        return await self._run(
            lambda state, collab: state.list_mcp_rollout_evidence_snapshots(
                environment_id, deployment_id, stage
            )
        )

    async def append_mcp_rollout_stage_approval(
        self, approval: MCPRolloutStageApproval
    ) -> MCPRolloutStageApproval:
        return await self._run(
            lambda state, collab: state.append_mcp_rollout_stage_approval(approval)
        )

    async def activate_mcp_rollout_deployment(
        self, activation: MCPRolloutDeploymentActivation
    ) -> MCPRolloutDeploymentActivation:
        return await self._run(
            lambda state, collab: state.activate_mcp_rollout_deployment(activation)
        )

    async def get_mcp_rollout_deployment_activation(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        config_fingerprint: str,
    ) -> MCPRolloutDeploymentActivation | None:
        return await self._run(
            lambda state, collab: state.get_mcp_rollout_deployment_activation(
                environment_id,
                deployment_id,
                stage,
                config_fingerprint,
            )
        )

    async def append_mcp_rollout_promotion_block(
        self, block: MCPRolloutPromotionBlock
    ) -> MCPRolloutPromotionBlock:
        return await self._run(
            lambda state, collab: state.append_mcp_rollout_promotion_block(block)
        )

    async def list_active_mcp_rollout_promotion_blocks(
        self,
        environment_id: str,
        *,
        rollout_program: str = MCP_ROLLOUT_PROGRAM,
    ) -> list[MCPRolloutPromotionBlock]:
        return await self._run(
            lambda state, collab: state.list_active_mcp_rollout_promotion_blocks(
                environment_id, rollout_program=rollout_program
            )
        )

    async def append_mcp_rollout_block_resolution(
        self, resolution: MCPRolloutBlockResolution
    ) -> MCPRolloutBlockResolution:
        return await self._run(
            lambda state, collab: state.append_mcp_rollout_block_resolution(resolution)
        )

    async def save_mcp_rollout_instance_config_lease(
        self, lease: MCPRolloutInstanceConfigLease
    ) -> MCPRolloutInstanceConfigLease:
        return await self._run(
            lambda state, collab: state.save_mcp_rollout_instance_config_lease(lease)
        )

    async def list_mcp_rollout_instance_config_leases(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        now: datetime | None = None,
    ) -> list[MCPRolloutInstanceConfigLease]:
        return await self._run(
            lambda state, collab: state.list_mcp_rollout_instance_config_leases(
                environment_id, deployment_id, now=now
            )
        )

    async def create_or_get_mcp_credential_key_validation(
        self, record: MCPCredentialKeyValidation
    ) -> MCPCredentialKeyValidation:
        return await self._run(
            lambda state, collab: state.create_or_get_mcp_credential_key_validation(record)
        )

    async def get_mcp_credential_key_validation(self) -> MCPCredentialKeyValidation | None:
        return await self._run(lambda state, collab: state.get_mcp_credential_key_validation())

    async def _run(
        self,
        callback: Callable[[SQLiteStateRepository, SQLiteCollaborationRepository], object],
    ) -> object:
        def _sync() -> object:
            with self._session_factory() as session:
                state_repo = SQLiteStateRepository(
                    session,
                    task_authority_mode=self._mcp_task_authority_mode,
                )
                collab_repo = SQLiteCollaborationRepository(session)
                result = callback(state_repo, collab_repo)
                session.commit()
                return result

        return await asyncio.to_thread(_sync)

    def _ensure_event_replay_available(self) -> None:
        if runtime_mode_for_component("event_log") != "enforce":
            return
        raise RuntimeError(
            "event_log_replay_unavailable: Rust runtime sidecar enforce mode is active, "
            "but event replay/list operations are not implemented by the configured sidecar facade."
        )

    async def save_auth_user_token(self, token: AuthUserToken, *, auth_generation_reason: str | None = None) -> AuthUserToken:
        return await self._run(lambda state, collab: state.save_auth_user_token(token, auth_generation_reason=auth_generation_reason))

    async def get_auth_user_token(self, username: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_token(username))

    async def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_token_by_hash(api_token_hash))

    async def get_auth_user_generation(self, username: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_generation(username))

    async def list_auth_user_generations(self) -> list[AuthUserToken]:
        return await self._run(lambda state, collab: state.list_auth_user_generations())

    async def touch_auth_user_token_last_used(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.touch_auth_user_token_last_used(
                username,
                api_token_hash=api_token_hash,
                at=at,
            )
        )

    async def clear_auth_user_token(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.clear_auth_user_token(
                username,
                api_token_hash=api_token_hash,
                at=at,
                auth_generation_reason=auth_generation_reason,
            )
        )

    async def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.rotate_auth_user_token(
                username,
                old_api_token_hash=old_api_token_hash,
                new_api_token_hash=new_api_token_hash,
                at=at,
                auth_generation_reason=auth_generation_reason,
            )
        )

    async def save_conversation(self, conversation: Conversation) -> Conversation:
        return await self._run(lambda state, collab: state.save_conversation(conversation))

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return await self._run(lambda state, collab: state.get_conversation(conversation_id))

    async def list_conversations_for_username(self, username: str) -> list[Conversation]:
        return await self._run(lambda state, collab: state.list_conversations_for_username(username))

    async def list_deleting_conversations(self) -> list[Conversation]:
        return await self._run(lambda state, collab: state.list_deleting_conversations())

    async def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_deleting(
                conversation_id,
                runner_id=runner_id,
                requested_at=requested_at,
                started_at=started_at,
                phase=phase,
            )
        )

    async def update_conversation_delete_phase(
        self,
        conversation_id: str,
        *,
        phase: str,
        updated_at: datetime,
        runner_id: str | None = None,
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.update_conversation_delete_phase(
                conversation_id,
                phase=phase,
                updated_at=updated_at,
                runner_id=runner_id,
            )
        )

    async def mark_conversation_delete_failed(
        self,
        conversation_id: str,
        *,
        failed_at: datetime,
        phase: str,
        error_code: str,
        error_summary: str,
        runner_id: str | None = None,
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_delete_failed(
                conversation_id,
                failed_at=failed_at,
                phase=phase,
                error_code=error_code,
                error_summary=error_summary,
                runner_id=runner_id,
            )
        )

    async def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.retry_failed_conversation_delete(
                conversation_id,
                runner_id=runner_id,
                requested_at=requested_at,
                started_at=started_at,
                phase=phase,
            )
        )

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        return await self._run(lambda state, collab: state.delete_conversation(conversation_id))

    async def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]:
        return await self._run(lambda state, collab: state.delete_conversation_physical(conversation_id))

    async def save_conversation_file_resource(self, resource: ConversationFileResource) -> ConversationFileResource:
        return await self._run(lambda state, collab: state.save_conversation_file_resource(resource))

    async def get_conversation_file_resource(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
    ) -> ConversationFileResource | None:
        return await self._run(lambda state, collab: state.get_conversation_file_resource(conversation_id, username, file_id))

    async def get_conversation_file_resource_by_id(self, file_id: str) -> ConversationFileResource | None:
        return await self._run(lambda state, collab: state.get_conversation_file_resource_by_id(file_id))

    async def list_conversation_file_resources(
        self,
        conversation_id: str,
        username: str | None = None,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[ConversationFileResource]:
        return await self._run(
            lambda state, collab: state.list_conversation_file_resources(
                conversation_id,
                username,
                include_deleted=include_deleted,
                limit=limit,
                cursor=cursor,
            )
        )

    async def mark_conversation_file_resource_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_resource_deleted(
                conversation_id,
                username,
                file_id,
                updated_at=updated_at,
            )
        )

    async def save_conversation_file_resource_with_upload_message(
        self,
        resource: ConversationFileResource,
        projection: FileUploadMessageProjection,
        *,
        now: datetime,
    ) -> ConversationFileResource:
        try:
            return await self._run(
                lambda state, collab: state.save_conversation_file_resource_with_upload_message(
                    resource,
                    projection,
                    now=now,
                )
            )
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
                    conversation_id=projection.conversation_id,
                    upload_id=projection.upload_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=now,
                    projection=projection,
                )
            )
            raise

    async def mark_conversation_file_resource_and_upload_message_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        try:
            return await self._run(
                lambda state, collab: state.mark_conversation_file_resource_and_upload_message_deleted(
                    conversation_id,
                    username,
                    file_id,
                    updated_at=updated_at,
                )
            )
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
                    conversation_id=conversation_id,
                    upload_id=file_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=updated_at,
                )
            )
            raise

    async def compensate_failed_conversation_file_upload(
        self,
        conversation_id: str,
        username: str,
        upload_id: str,
        *,
        reason_code: str,
        now: datetime,
    ) -> Mapping[str, Any]:
        return await self._run(
            lambda state, collab: state.compensate_failed_conversation_file_upload(
                conversation_id,
                username,
                upload_id,
                reason_code=reason_code,
                now=now,
            )
        )

    async def record_conversation_file_index_repair_required(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        affected_upload_ids: Iterable[str] = (),
        now: datetime,
    ) -> ConversationFileIndexRepairMarker:
        return await self._run(
            lambda state, collab: state.record_conversation_file_index_repair_required(
                conversation_id,
                reason_code=reason_code,
                affected_upload_ids=affected_upload_ids,
                now=now,
            )
        )

    async def get_conversation_file_index_repair_marker(
        self,
        conversation_id: str,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(lambda state, collab: state.get_conversation_file_index_repair_marker(conversation_id))

    async def list_due_conversation_file_index_repairs(
        self,
        *,
        now: datetime,
        limit: int | None = None,
    ) -> list[ConversationFileIndexRepairMarker]:
        return await self._run(
            lambda state, collab: state.list_due_conversation_file_index_repairs(now=now, limit=limit)
        )

    async def mark_conversation_file_index_repairing(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_index_repairing(conversation_id, now=now)
        )

    async def mark_conversation_file_index_repair_resolved(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_index_repair_resolved(conversation_id, now=now)
        )

    async def mark_conversation_file_index_repair_failed(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        now: datetime,
        retryable: bool = True,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_index_repair_failed(
                conversation_id,
                reason_code=reason_code,
                now=now,
                retryable=retryable,
            )
        )

    async def save_conversation_memory_summary(self, summary: ConversationMemorySummary) -> ConversationMemorySummary:
        return await self._run(lambda state, collab: state.save_conversation_memory_summary(summary))

    async def get_conversation_memory_summary(self, summary_id: str) -> ConversationMemorySummary | None:
        return await self._run(lambda state, collab: state.get_conversation_memory_summary(summary_id))

    async def get_latest_conversation_memory_summary(
        self,
        conversation_id: str,
        username: str | None = None,
    ) -> ConversationMemorySummary | None:
        return await self._run(
            lambda state, collab: state.get_latest_conversation_memory_summary(
                conversation_id,
                username=username,
            )
        )

    async def list_conversation_memory_summaries(self, conversation_id: str) -> list[ConversationMemorySummary]:
        return await self._run(lambda state, collab: state.list_conversation_memory_summaries(conversation_id))

    async def delete_conversation_memory_summaries_for_conversation(self, conversation_id: str) -> int:
        return await self._run(lambda state, collab: state.delete_conversation_memory_summaries_for_conversation(conversation_id))

    async def save_pending_skill_context(self, context: PendingSkillContext) -> PendingSkillContext:
        return await self._run(lambda state, collab: state.save_pending_skill_context(context))

    async def get_pending_skill_context(self, context_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.get_pending_skill_context(context_id))

    async def get_active_pending_skill_context(self, conversation_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.get_active_pending_skill_context(conversation_id))

    async def mark_pending_skill_context_consumed(self, context_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.mark_pending_skill_context_consumed(context_id, updated_at=_utcnow_naive()))

    async def mark_pending_skill_context_cancelled(self, context_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.mark_pending_skill_context_cancelled(context_id, updated_at=_utcnow_naive()))

    async def mark_pending_skill_context_superseded(self, conversation_id: str) -> int:
        return await self._run(lambda state, collab: state.mark_pending_skill_context_superseded(conversation_id, updated_at=_utcnow_naive()))

    async def save_message(self, message: Message) -> Message:
        return await self._run(lambda state, collab: state.save_message(message))

    async def get_message(self, message_id: str) -> Message | None:
        return await self._run(lambda state, collab: state.get_message(message_id))

    async def list_messages_for_conversation(self, conversation_id: str) -> list[Message]:
        return await self._run(lambda state, collab: state.list_messages_for_conversation(conversation_id))

    async def upsert_file_upload_message(self, projection: FileUploadMessageProjection, *, now: datetime) -> Message:
        try:
            return await self._run(lambda state, collab: state.upsert_file_upload_message(projection, now=now))
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
                    conversation_id=projection.conversation_id,
                    upload_id=projection.upload_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=now,
                    projection=projection,
                )
            )
            raise

    async def mark_file_upload_message_deleted(
        self,
        conversation_id: str,
        upload_id: str,
        *,
        deleted_at: datetime,
    ) -> Message | None:
        try:
            return await self._run(
                lambda state, collab: state.mark_file_upload_message_deleted(
                    conversation_id,
                    upload_id,
                    deleted_at=deleted_at,
                )
            )
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
                    conversation_id=conversation_id,
                    upload_id=upload_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=deleted_at,
                )
            )
            raise

    async def save_task(
        self, task: Task, *, expected_from_status: TaskStatus | None = None
    ) -> Task:
        if self._task_authority_mode() == "enforce":
            current = await self.get_task(task.task_id)
        else:
            current = await self._run(
                lambda state, collab: state.get_task(task.task_id)
            )
        effective_expected_status = (
            expected_from_status
            if expected_from_status is not None
            else (None if current is None else current.status)
        )
        if self._mcp_task_authority_mode == "enforce" and task.mcp_execution_mode is None:
            if (
                current is not None
                and current.status in _TERMINAL_TASK_STATUSES
                and task == current
            ):
                return current
            raise ValueError(
                "mcp_task_route_assignment_migration_required: enforce authority requires a canonical assignment; terminal null history is read-only"
            )
        task_record = _task_to_sidecar_record(task)
        idempotency_key = _task_snapshot_idempotency_key(task_record)
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_submit",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.submit_task(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    idempotency_key=idempotency_key,
                    task=task_record,
                    expected_from_status=(
                        None
                        if effective_expected_status is None
                        else str(effective_expected_status)
                    ),
                )
            )
            envelope = _consume_runtime_sidecar_response("task_submit", response)
            if envelope.get("task_id") != task.task_id:
                _raise_task_snapshot_response_invalid("task_submit top-level task_id differs from request")
            response_task = envelope.get("task")
            if not isinstance(response_task, Mapping):
                _raise_task_snapshot_response_invalid("task_submit response omitted the Task snapshot")
            saved = _validated_task_from_sidecar_record(response_task)
            if saved != task:
                _raise_task_snapshot_response_invalid("task_submit returned a different Task snapshot")
            return saved
        if expected_from_status is None:
            saved = await self._run(lambda state, collab: state.save_task(task))
        else:
            saved = await self._run(
                lambda state, collab: state.compare_and_set_task(
                    task, expected_from_status=expected_from_status
                )
            )
            if saved is None:
                raise RuntimeError(
                    "runtime_store_idempotency_conflict: expected Task status is stale"
                )
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_submit",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "task": task_record,
            },
            legacy_output={"task": _task_to_sidecar_record(saved)},
            rust_call=lambda: self._runtime_sidecar_client.submit_task(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                idempotency_key=idempotency_key,
                task=task_record,
                expected_from_status=(
                    None
                    if effective_expected_status is None
                    else str(effective_expected_status)
                ),
            ),
            rust_output=lambda envelope: {"task": envelope.get("task")},
            mode=self._task_authority_mode(),
        )
        return saved

    async def compare_and_set_task(
        self, task: Task, *, expected_from_status: TaskStatus
    ) -> Task | None:
        if self._mcp_task_authority_mode == "enforce" and task.mcp_execution_mode is None:
            current = await self.get_task(task.task_id)
            if (
                current is not None
                and current.status in _TERMINAL_TASK_STATUSES
                and task == current
            ):
                return current
            raise ValueError(
                "mcp_task_route_assignment_migration_required: enforce authority requires a canonical assignment; terminal null history is read-only"
            )
        task_record = _task_to_sidecar_record(task)
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_submit",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            try:
                envelope = _consume_runtime_sidecar_response(
                    "task_submit",
                    await _resolve_runtime_sidecar_call(
                        sidecar_client.submit_task(
                            task_id=task.task_id,
                            conversation_id=task.conversation_id,
                            idempotency_key=_task_snapshot_idempotency_key(task_record),
                            task=task_record,
                            expected_from_status=str(expected_from_status),
                        )
                    ),
                )
            except RuntimeError as exc:
                if str(exc).startswith("runtime_store_idempotency_conflict:"):
                    return None
                raise
            if envelope.get("task_id") != task.task_id:
                _raise_task_snapshot_response_invalid(
                    "task_submit top-level task_id differs from request"
                )
            saved = _validated_task_from_sidecar_record(envelope.get("task"))
            if saved != task:
                _raise_task_snapshot_response_invalid(
                    "task_submit returned a different Task snapshot"
                )
            return saved
        saved = await self._run(
            lambda state, collab: state.compare_and_set_task(
                task, expected_from_status=expected_from_status
            )
        )
        if saved is not None:
            await record_runtime_sidecar_shadow_write(
                component="runtime_store",
                operation_name="task_submit",
                runtime_sidecar_client=self._runtime_sidecar_client,
                shadow_sink=self._runtime_sidecar_shadow_sink,
                input_payload={
                    "task": task_record,
                    "expected_from_status": str(expected_from_status),
                },
                legacy_output={"task": _task_to_sidecar_record(saved)},
                rust_call=lambda: self._runtime_sidecar_client.submit_task(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    idempotency_key=_task_snapshot_idempotency_key(task_record),
                    task=task_record,
                    expected_from_status=str(expected_from_status),
                ),
                rust_output=lambda envelope: {"task": envelope.get("task")},
                mode=self._task_authority_mode(),
            )
        return saved

    async def get_task(self, task_id: str) -> Task | None:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_get",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(sidecar_client.get_task(task_id=task_id))
            envelope = _consume_runtime_sidecar_response("task_get", response)
            if not envelope["found"]:
                return None
            loaded = _validated_task_from_sidecar_record(envelope["task"])
            if loaded.task_id != task_id:
                _raise_task_snapshot_response_invalid("task_get Task snapshot differs from requested task_id")
            return loaded

        loaded = await self._run(lambda state, collab: state.get_task(task_id))
        legacy_output = _task_get_shadow_payload(loaded)
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_get",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={"task_id": task_id},
            legacy_output=legacy_output,
            rust_call=lambda: self._runtime_sidecar_client.get_task(task_id=task_id),
            rust_output=lambda envelope: {
                "found": bool(envelope["found"]),
                "task": envelope.get("task"),
            },
            mode=self._task_authority_mode(),
        )
        return loaded

    async def get_active_task_for_conversation(self, conversation_id: str) -> Task | None:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_get_active_for_conversation",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.get_active_task_for_conversation(conversation_id=conversation_id)
            )
            envelope = _consume_runtime_sidecar_response("task_get_active_for_conversation", response)
            if not envelope["found"]:
                return None
            loaded = _validated_task_from_sidecar_record(envelope["task"])
            if loaded.conversation_id != conversation_id:
                _raise_task_snapshot_response_invalid(
                    "task_get_active_for_conversation Task snapshot differs from requested conversation_id"
                )
            return loaded

        loaded = await self._run(lambda state, collab: state.get_active_task_for_conversation(conversation_id))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_get_active_for_conversation",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={"conversation_id": conversation_id},
            legacy_output=_task_get_shadow_payload(loaded),
            rust_call=lambda: self._runtime_sidecar_client.get_active_task_for_conversation(
                conversation_id=conversation_id
            ),
            rust_output=lambda envelope: {
                "found": bool(envelope["found"]),
                "task": envelope.get("task"),
            },
            mode=self._task_authority_mode(),
        )
        return loaded

    async def list_tasks_for_conversation(
        self,
        conversation_id: str,
        statuses: Iterable[TaskStatus] | None = None,
    ) -> list[Task]:
        status_values = None if statuses is None else tuple(statuses)
        status_strings = () if status_values is None else tuple(sorted(str(status) for status in status_values))
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_list_for_conversation",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.list_tasks_for_conversation(
                    conversation_id=conversation_id,
                    statuses=status_strings,
                )
            )
            envelope = _consume_runtime_sidecar_response("task_list_for_conversation", response)
            tasks = [_validated_task_from_sidecar_record(record) for record in envelope["tasks"]]
            if any(task.conversation_id != conversation_id for task in tasks):
                _raise_task_snapshot_response_invalid(
                    "task_list_for_conversation contains a different conversation_id"
                )
            return tasks

        loaded = await self._run(
            lambda state, collab: state.list_tasks_for_conversation(
                conversation_id,
                statuses=status_values,
            )
        )
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_list_for_conversation",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={"conversation_id": conversation_id, "statuses": list(status_strings)},
            legacy_output={"tasks": [_task_to_sidecar_record(task) for task in loaded]},
            rust_call=lambda: self._runtime_sidecar_client.list_tasks_for_conversation(
                conversation_id=conversation_id,
                statuses=status_strings,
            ),
            rust_output=lambda envelope: {"tasks": envelope["tasks"]},
            mode=self._task_authority_mode(),
        )
        return loaded

    async def save_task_node(
        self, node: TaskNode, *, expected_from_status: NodeStatus | None = None
    ) -> TaskNode:
        if self._task_authority_mode() == "enforce":
            current = await self.get_task_node(node.node_id)
        else:
            current = await self._run(
                lambda state, collab: state.get_task_node(node.node_id)
            )
        effective_expected_status = (
            expected_from_status
            if expected_from_status is not None
            else (None if current is None else current.status)
        )
        node_record = _task_node_to_sidecar_record(node)
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="node_state_transition",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.transition_node(
                    task_id=node.task_id,
                    node_id=node.node_id,
                    to_status=str(node.status),
                    expected_from_status=(
                        ""
                        if effective_expected_status is None
                        else str(effective_expected_status)
                    ),
                    idempotency_key=_task_node_snapshot_idempotency_key(node_record),
                    node=node_record,
                )
            )
            envelope = _consume_runtime_sidecar_response("node_state_transition", response)
            if envelope.get("node_id") != node.node_id:
                _raise_task_snapshot_response_invalid(
                    "node_state_transition top-level node_id differs from request"
                )
            if envelope.get("status") != str(node.status):
                _raise_task_snapshot_response_invalid(
                    "node_state_transition top-level status differs from request"
                )
            saved = _validated_task_node_from_sidecar_record(envelope.get("node"))
            if saved != node:
                _raise_task_snapshot_response_invalid("node_state_transition returned a different TaskNode snapshot")
            return saved
        if expected_from_status is None:
            saved = await self._run(lambda state, collab: state.save_task_node(node))
        else:
            saved = await self._run(
                lambda state, collab: state.compare_and_set_task_node(
                    node, expected_from_status=expected_from_status
                )
            )
            if saved is None:
                raise RuntimeError(
                    "runtime_store_idempotency_conflict: expected TaskNode status is stale"
                )
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="node_state_transition",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "node_id": node.node_id,
                "status": str(node.status),
                "task_id": node.task_id,
                "node": node_record,
            },
            legacy_output={
                "node_id": saved.node_id,
                "status": str(saved.status),
            },
            rust_call=lambda: self._runtime_sidecar_client.transition_node(
                task_id=node.task_id,
                node_id=node.node_id,
                to_status=str(node.status),
                expected_from_status=(
                    ""
                    if effective_expected_status is None
                    else str(effective_expected_status)
                ),
                idempotency_key=_task_node_snapshot_idempotency_key(node_record),
                node=node_record,
            ),
            rust_output=lambda envelope: {
                "node_id": str(envelope.get("node_id", "")),
                "status": str(envelope.get("status", "")),
                "node": envelope.get("node"),
            },
            mode=self._task_authority_mode(),
        )
        return saved

    async def compare_and_set_task_node(
        self, node: TaskNode, *, expected_from_status: NodeStatus
    ) -> TaskNode | None:
        node_record = _task_node_to_sidecar_record(node)
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="node_state_transition",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            try:
                envelope = _consume_runtime_sidecar_response(
                    "node_state_transition",
                    await _resolve_runtime_sidecar_call(
                        sidecar_client.transition_node(
                            task_id=node.task_id,
                            node_id=node.node_id,
                            to_status=str(node.status),
                            expected_from_status=str(expected_from_status),
                            idempotency_key=_task_node_snapshot_idempotency_key(
                                node_record
                            ),
                            node=node_record,
                        )
                    ),
                )
            except RuntimeError as exc:
                if str(exc).startswith("runtime_store_idempotency_conflict:"):
                    return None
                raise
            if envelope.get("node_id") != node.node_id:
                _raise_task_snapshot_response_invalid(
                    "node_state_transition top-level node_id differs from request"
                )
            saved = _validated_task_node_from_sidecar_record(envelope.get("node"))
            if saved != node:
                _raise_task_snapshot_response_invalid(
                    "node_state_transition returned a different TaskNode snapshot"
                )
            return saved
        saved = await self._run(
            lambda state, collab: state.compare_and_set_task_node(
                node, expected_from_status=expected_from_status
            )
        )
        if saved is not None:
            await record_runtime_sidecar_shadow_write(
                component="runtime_store",
                operation_name="node_state_transition",
                runtime_sidecar_client=self._runtime_sidecar_client,
                shadow_sink=self._runtime_sidecar_shadow_sink,
                input_payload={
                    "node": node_record,
                    "expected_from_status": str(expected_from_status),
                },
                legacy_output={"node": _task_node_to_sidecar_record(saved)},
                rust_call=lambda: self._runtime_sidecar_client.transition_node(
                    task_id=node.task_id,
                    node_id=node.node_id,
                    to_status=str(node.status),
                    expected_from_status=str(expected_from_status),
                    idempotency_key=_task_node_snapshot_idempotency_key(node_record),
                    node=node_record,
                ),
                rust_output=lambda envelope: {"node": envelope.get("node")},
                mode=self._task_authority_mode(),
            )
        return saved

    async def get_task_node(self, node_id: str) -> TaskNode | None:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_node_get",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            envelope = _consume_runtime_sidecar_response(
                "task_node_get",
                await _resolve_runtime_sidecar_call(sidecar_client.get_task_node(node_id=node_id)),
            )
            if not envelope["found"]:
                return None
            loaded = _validated_task_node_from_sidecar_record(envelope["node"])
            if loaded.node_id != node_id:
                _raise_task_snapshot_response_invalid(
                    "task_node_get TaskNode snapshot differs from requested node_id"
                )
            return loaded
        loaded = await self._run(lambda state, collab: state.get_task_node(node_id))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_node_get",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={"node_id": node_id},
            legacy_output={"found": loaded is not None, "node": None if loaded is None else _task_node_to_sidecar_record(loaded)},
            rust_call=lambda: self._runtime_sidecar_client.get_task_node(node_id=node_id),
            rust_output=lambda envelope: {"found": envelope["found"], "node": envelope.get("node")},
            mode=self._task_authority_mode(),
        )
        return loaded

    async def list_task_nodes_for_task(self, task_id: str) -> list[TaskNode]:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_node_list",
            unavailable_error_code="runtime_store_unavailable",
            task_authority=True,
        )
        if sidecar_client is not None:
            envelope = _consume_runtime_sidecar_response(
                "task_node_list",
                await _resolve_runtime_sidecar_call(sidecar_client.list_task_nodes_for_task(task_id=task_id)),
            )
            nodes = [
                _validated_task_node_from_sidecar_record(node)
                for node in envelope["nodes"]
            ]
            if any(node.task_id != task_id for node in nodes):
                _raise_task_snapshot_response_invalid(
                    "task_node_list contains a different task_id"
                )
            return nodes
        loaded = await self._run(lambda state, collab: state.list_task_nodes_for_task(task_id))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_node_list",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={"task_id": task_id},
            legacy_output={"nodes": [_task_node_to_sidecar_record(node) for node in loaded]},
            rust_call=lambda: self._runtime_sidecar_client.list_task_nodes_for_task(task_id=task_id),
            rust_output=lambda envelope: {"nodes": envelope["nodes"]},
            mode=self._task_authority_mode(),
        )
        return loaded

    async def save_task_edge(self, task_id: str, edge: TaskEdge) -> TaskEdge:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_edge_save",
            unavailable_error_code="runtime_store_unavailable",
        )
        idempotency_key = build_task_edge_id(task_id, edge.from_node_id, edge.to_node_id)
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.save_task_edge(
                    task_id=task_id,
                    from_node_id=edge.from_node_id,
                    to_node_id=edge.to_node_id,
                    edge_type=str(edge.edge_type),
                    condition=edge.condition or "",
                    idempotency_key=idempotency_key,
                )
            )
            _consume_runtime_sidecar_response("task_edge_save", response)
            return edge
        saved = await self._run(lambda state, collab: state.save_task_edge(task_id, edge))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_edge_save",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload=_task_edge_shadow_payload(task_id, edge),
            legacy_output=_task_edge_shadow_payload(task_id, saved),
            rust_call=lambda: self._runtime_sidecar_client.save_task_edge(
                task_id=task_id,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                edge_type=str(edge.edge_type),
                condition=edge.condition or "",
                idempotency_key=idempotency_key,
            ),
            rust_output=lambda envelope: _task_edge_shadow_payload_from_record(envelope["edge"]),
        )
        return saved

    async def list_task_edges(self, task_id: str) -> list[TaskEdge]:
        if runtime_mode_for_component("runtime_store") == "enforce" and self._runtime_sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                self._runtime_sidecar_client.list_task_edges(task_id=task_id)
            )
            envelope = validate_runtime_sidecar_response(
                "task_edge_list",
                normalize_runtime_sidecar_response("task_edge_list", response),
            )
            return [_task_edge_from_sidecar_record(record) for record in envelope["edges"]]
        return await self._run(lambda state, collab: state.list_task_edges(task_id))

    async def save_artifact(self, artifact: Artifact) -> Artifact:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="artifact_save",
            unavailable_error_code="runtime_store_unavailable",
        )
        record = _artifact_to_sidecar_record(artifact)
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.save_artifact(
                    artifact_id=artifact.artifact_id,
                    task_id=artifact.task_id,
                    producer_node_id=artifact.producer_node_id,
                    artifact_type=str(artifact.artifact_type),
                    storage_ref=artifact.storage_ref,
                    summary=artifact.summary or "",
                    is_complete=artifact.is_complete,
                    created_at=record["created_at"],
                    idempotency_key=artifact.artifact_id,
                )
            )
            _consume_runtime_sidecar_response("artifact_save", response)
            return artifact
        saved = await self._run(lambda state, collab: state.save_artifact(artifact))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="artifact_save",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload=_artifact_shadow_payload(artifact),
            legacy_output=_artifact_shadow_payload(saved),
            rust_call=lambda: self._runtime_sidecar_client.save_artifact(
                artifact_id=artifact.artifact_id,
                task_id=artifact.task_id,
                producer_node_id=artifact.producer_node_id,
                artifact_type=str(artifact.artifact_type),
                storage_ref=artifact.storage_ref,
                summary=artifact.summary or "",
                is_complete=artifact.is_complete,
                created_at=record["created_at"],
                idempotency_key=artifact.artifact_id,
            ),
            rust_output=lambda envelope: _artifact_shadow_payload_from_record(envelope["artifact"]),
        )
        return saved

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        if runtime_mode_for_component("runtime_store") == "enforce" and self._runtime_sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                self._runtime_sidecar_client.get_artifact(artifact_id=artifact_id)
            )
            envelope = validate_runtime_sidecar_response(
                "artifact_get",
                normalize_runtime_sidecar_response("artifact_get", response),
            )
            if not envelope["found"]:
                return None
            return _artifact_from_sidecar_record(envelope["artifact"])
        return await self._run(lambda state, collab: state.get_artifact(artifact_id))

    async def list_artifacts_for_task(self, task_id: str) -> list[Artifact]:
        if runtime_mode_for_component("runtime_store") == "enforce" and self._runtime_sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                self._runtime_sidecar_client.list_artifacts_for_task(task_id=task_id)
            )
            envelope = validate_runtime_sidecar_response(
                "artifact_list",
                normalize_runtime_sidecar_response("artifact_list", response),
            )
            return [_artifact_from_sidecar_record(record) for record in envelope["artifacts"]]
        return await self._run(lambda state, collab: state.list_artifacts_for_task(task_id))

    async def list_artifacts_for_conversation(self, conversation_id: str) -> list[Artifact]:
        return await self._run(lambda state, collab: state.list_artifacts_for_conversation(conversation_id))

    async def save_task_input_attachment(self, attachment: TaskInputAttachment) -> TaskInputAttachment:
        return await self._run(lambda state, collab: state.save_task_input_attachment(attachment))

    async def list_task_input_attachments_for_task(self, task_id: str) -> list[TaskInputAttachment]:
        return await self._run(lambda state, collab: state.list_task_input_attachments_for_task(task_id))

    async def list_task_input_attachments_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
    ) -> list[TaskInputAttachment]:
        return await self._run(lambda state, collab: state.list_task_input_attachments_for_conversation(conversation_id, limit=limit))

    async def append_event(self, event: EventRecord) -> EventRecord:
        sidecar_client = self._runtime_sidecar_client_for(
            component="event_log",
            operation_name="event_append",
            unavailable_error_code="event_log_unavailable",
        )
        if sidecar_client is not None:
            _ensure_event_append_payload_within_rust_limit(event)
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.append_event(
                    conversation_id=event.conversation_id,
                    task_id=event.task_id,
                    event_type=event.event_type,
                    payload_json=json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"),
                    idempotency_key=event.event_id,
                )
            )
            _consume_runtime_sidecar_response("event_append", response)
            return event
        saved = await self._run(lambda state, collab: collab.save_event_record(event))
        payload_json = json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8")
        await record_runtime_sidecar_shadow_write(
            component="event_log",
            operation_name="event_append",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "conversation_id": event.conversation_id,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload_sha256": hashlib.sha256(payload_json).hexdigest(),
                "task_id": event.task_id,
            },
            legacy_output={
                "accepted": "true",
                "conversation_id": saved.conversation_id,
                "task_id": saved.task_id,
            },
            rust_call=lambda: self._runtime_sidecar_client.append_event(
                conversation_id=event.conversation_id,
                task_id=event.task_id,
                event_type=event.event_type,
                payload_json=payload_json,
                idempotency_key=event.event_id,
            ),
            rust_output=lambda envelope: {
                "accepted": "true",
                "conversation_id": str(envelope.get("cursor", {}).get("conversation_id", "")),
                "task_id": str(envelope.get("cursor", {}).get("task_id", "")),
            },
        )
        return saved

    async def list_events_for_task(self, task_id: str) -> list[EventRecord]:
        self._ensure_event_replay_available()
        return await self._run(lambda state, collab: collab.list_events_for_task(task_id))

    async def list_events_for_task_filtered(
        self,
        task_id: str,
        *,
        event_types: Iterable[str] | None = None,
        node_id: str | None = None,
        visibility: EventVisibility | str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        self._ensure_event_replay_available()
        return await self._run(
            lambda state, collab: collab.list_events_for_task_filtered(
                task_id,
                event_types=event_types,
                node_id=node_id,
                visibility=visibility,
                limit=limit,
            )
        )

    async def list_event_page_for_task(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        self._ensure_event_replay_available()
        return await self._run(
            lambda state, collab: collab.list_event_page_for_task(
                task_id,
                after_event_id=after_event_id,
                limit=limit,
            )
        )

    async def save_mailbox_message(self, message: MailboxMessage) -> MailboxMessage:
        return await self._run(lambda state, collab: collab.save_mailbox_message(message))

    async def get_mailbox_message(self, message_id: str) -> MailboxMessage | None:
        return await self._run(lambda state, collab: collab.get_mailbox_message(message_id))

    async def save_mailbox_delivery(self, delivery: MailboxDelivery) -> MailboxDelivery:
        return await self._run(lambda state, collab: collab.save_mailbox_delivery(delivery))

    async def get_mailbox_delivery(self, delivery_id: str) -> MailboxDelivery | None:
        return await self._run(lambda state, collab: collab.get_mailbox_delivery(delivery_id))

    async def list_mailbox_messages_for_task(self, task_id: str) -> list[MailboxMessage]:
        return await self._run(lambda state, collab: collab.list_mailbox_messages_for_task(task_id))

    async def list_mailbox_deliveries_for_message(self, message_id: str) -> list[MailboxDelivery]:
        return await self._run(lambda state, collab: collab.list_mailbox_deliveries_for_message(message_id))

    async def save_interrupt(self, interrupt: Interrupt) -> Interrupt:
        return await self._run(lambda state, collab: collab.save_interrupt(interrupt))

    async def get_interrupt(self, interrupt_id: str) -> Interrupt | None:
        return await self._run(lambda state, collab: collab.get_interrupt(interrupt_id))

    async def get_interrupt_for_node(self, task_id: str, node_id: str) -> Interrupt | None:
        return await self._run(lambda state, collab: collab.get_interrupt_for_node(task_id, node_id))

    async def list_interrupts_for_task(self, task_id: str) -> list[Interrupt]:
        return await self._run(lambda state, collab: collab.list_interrupts_for_task(task_id))

    async def save_interrupt_answer(self, interrupt_answer: InterruptAnswer) -> InterruptAnswer:
        return await self._run(lambda state, collab: collab.save_interrupt_answer(interrupt_answer))

    async def get_interrupt_answer(self, interrupt_answer_id: str) -> InterruptAnswer | None:
        return await self._run(lambda state, collab: collab.get_interrupt_answer(interrupt_answer_id))

    async def list_interrupt_answers(self, interrupt_id: str) -> list[InterruptAnswer]:
        return await self._run(lambda state, collab: collab.list_interrupt_answers(interrupt_id))

    async def save_slot_collection(self, collection: SlotCollection) -> SlotCollection:
        return await self._run(lambda state, collab: collab.save_slot_collection(collection))

    async def get_slot_collection(self, collection_id: str) -> SlotCollection | None:
        return await self._run(lambda state, collab: collab.get_slot_collection(collection_id))

    async def get_active_slot_collection_for_node(self, task_id: str, node_id: str) -> SlotCollection | None:
        return await self._run(lambda state, collab: collab.get_active_slot_collection_for_node(task_id, node_id))

    async def list_slot_collections_for_task(self, task_id: str) -> list[SlotCollection]:
        return await self._run(lambda state, collab: collab.list_slot_collections_for_task(task_id))

    async def apply_slot_transition(
        self,
        collection_id: str,
        expected_revision: int,
        next_collection: SlotCollection,
        slot_event: SlotEvent,
        *,
        idempotency_key: str | None = None,
    ) -> SlotCollection | None:
        return await self._run(
            lambda state, collab: collab.apply_slot_transition(
                collection_id,
                expected_revision,
                next_collection,
                slot_event,
                idempotency_key=idempotency_key,
            )
        )

    async def append_slot_event(self, event: SlotEvent) -> SlotEvent:
        return await self._run(lambda state, collab: collab.append_slot_event(event))

    async def list_slot_events(self, collection_id: str) -> list[SlotEvent]:
        return await self._run(lambda state, collab: collab.list_slot_events(collection_id))

    async def get_slot_event_by_idempotency_key(self, collection_id: str, key: str) -> SlotEvent | None:
        return await self._run(lambda state, collab: collab.get_slot_event_by_idempotency_key(collection_id, key))

    async def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        return await self._run(lambda state, collab: collab.save_checkpoint(checkpoint))

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        return await self._run(lambda state, collab: collab.get_checkpoint(checkpoint_id))

    async def get_checkpoint_by_resume_token(self, resume_token: str) -> Checkpoint | None:
        return await self._run(lambda state, collab: collab.get_checkpoint_by_resume_token(resume_token))

    async def list_checkpoints_for_task(self, task_id: str) -> list[Checkpoint]:
        return await self._run(lambda state, collab: collab.list_checkpoints_for_task(task_id))

    def _runtime_sidecar_client_for(
        self,
        *,
        component: str,
        operation_name: str,
        unavailable_error_code: str,
        task_authority: bool = False,
    ) -> Any | None:
        mode = (
            self._task_authority_mode()
            if task_authority
            else runtime_mode_for_component(component)
        )
        if mode != "enforce":
            return None
        if self._runtime_sidecar_client is None:
            if task_authority and self._mcp_task_authority_mode is not None:
                raise RuntimeError(
                    "runtime_store_unavailable: MCP Task enforce authority is active "
                    "but no Rust runtime sidecar client is configured"
                )
            if operation_name in {
                "task_get",
                "task_list_for_conversation",
                "task_get_active_for_conversation",
                "task_node_get",
                "task_node_list",
            }:
                error_code = runtime_error_policy(unavailable_error_code)["code"]
                raise RuntimeError(
                    f"{error_code}: Rust runtime sidecar enforce mode is active "
                    "but no Rust runtime sidecar client is configured"
                )
            ensure_sidecar_write_allowed(
                component=component,
                operation_name=operation_name,
                unavailable_error_code=unavailable_error_code,
            )
        return self._runtime_sidecar_client

    def _task_authority_mode(self) -> str:
        if self._mcp_task_authority_mode is not None:
            return self._mcp_task_authority_mode
        return runtime_mode_for_component("runtime_store")


def _runtime_sidecar_idempotency_key(*parts: str) -> str:
    return ":".join(parts)


def _task_to_sidecar_record(task: Task) -> dict[str, Any]:
    assignment_fields = _task_mcp_assignment(task)
    assignment = None
    if assignment_fields["mcp_execution_mode"] is not None:
        assignment = {
            "route_mode": assignment_fields["mcp_rollout_mode"],
            "real_path": assignment_fields["mcp_execution_mode"],
            "shadow_path": "user_scoped" if assignment_fields["mcp_shadow_enabled"] else "none",
            "config_version": assignment_fields["mcp_rollout_config_version"],
            "reason_code": assignment_fields["mcp_route_reason_code"],
        }
    return {
        "task_id": task.task_id,
        "conversation_id": task.conversation_id,
        "root_message_id": task.root_message_id,
        "status": str(task.status),
        "routing_mode": str(task.routing_mode),
        "requested_capability_id": task.requested_capability_id,
        "root_node_id": task.root_node_id,
        "summary": task.summary,
        "cancel_requested_at": _optional_datetime_text(task.cancel_requested_at),
        "created_at": _optional_datetime_text(task.created_at),
        "updated_at": _optional_datetime_text(task.updated_at),
        "assignment": assignment,
    }


def _task_snapshot_idempotency_key(task_record: Mapping[str, Any]) -> str:
    snapshot = json.dumps(
        task_record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _runtime_sidecar_idempotency_key(
        str(task_record["task_id"]),
        hashlib.sha256(snapshot).hexdigest(),
    )


def _task_from_sidecar_record(record: Mapping[str, Any]) -> Task:
    required_strings = ("task_id", "conversation_id", "root_message_id", "status", "routing_mode")
    if any(not isinstance(record.get(name), str) or not record[name] for name in required_strings):
        raise ValueError("mcp_task_snapshot_corrupt: required Task fields are missing")
    assignment_record = record.get("assignment")
    if assignment_record is None:
        assignment = _validated_mcp_task_assignment(
            execution_mode=None,
            shadow_enabled=None,
            config_version=None,
            reason_code=None,
            rollout_mode=None,
        )
    elif isinstance(assignment_record, Mapping):
        shadow_path = assignment_record.get("shadow_path")
        if shadow_path not in {"none", "user_scoped"}:
            raise ValueError("mcp_task_route_assignment_invalid: unsupported shadow path")
        assignment = _validated_mcp_task_assignment(
            execution_mode=assignment_record.get("real_path"),
            shadow_enabled=shadow_path == "user_scoped",
            config_version=assignment_record.get("config_version"),
            reason_code=assignment_record.get("reason_code"),
            rollout_mode=assignment_record.get("route_mode"),
        )
    else:
        raise ValueError("mcp_task_route_assignment_corrupt: sidecar assignment is invalid")
    return Task(
        task_id=str(record["task_id"]),
        conversation_id=str(record["conversation_id"]),
        root_message_id=str(record["root_message_id"]),
        status=TaskStatus(str(record["status"])),
        routing_mode=RoutingMode(str(record["routing_mode"])),
        requested_capability_id=_optional_task_string(record, "requested_capability_id"),
        root_node_id=_optional_task_string(record, "root_node_id"),
        summary=_optional_task_string(record, "summary"),
        cancel_requested_at=_optional_task_datetime(record, "cancel_requested_at"),
        created_at=_optional_task_datetime(record, "created_at"),
        updated_at=_optional_task_datetime(record, "updated_at"),
        **assignment,
    )


def _validated_task_from_sidecar_record(record: Any) -> Task:
    if not isinstance(record, Mapping):
        _raise_task_snapshot_response_invalid("sidecar Task snapshot is not a mapping")
    try:
        return _task_from_sidecar_record(record)
    except (KeyError, TypeError, ValueError) as exc:
        _raise_task_snapshot_response_invalid(str(exc))


def _task_get_shadow_payload(task: Task | None) -> dict[str, Any]:
    return {
        "found": task is not None,
        "task": _task_to_sidecar_record(task) if task is not None else None,
    }


def _task_node_to_sidecar_record(node: TaskNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "task_id": node.task_id,
        "capability_id": node.capability_id,
        "assigned_instance_id": node.assigned_instance_id,
        "status": str(node.status),
        "criticality": str(node.criticality),
        "dependency_type": str(node.dependency_type),
        "retry_policy": dict(node.retry_policy),
        "timeout_policy": dict(node.timeout_policy),
        "resource_class": node.resource_class,
        "input_refs": list(node.input_refs),
        "output_refs": list(node.output_refs),
        "started_at": _optional_datetime_text(node.started_at),
        "finished_at": _optional_datetime_text(node.finished_at),
    }


def _task_node_snapshot_idempotency_key(record: Mapping[str, Any]) -> str:
    snapshot = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return _runtime_sidecar_idempotency_key(str(record["node_id"]), hashlib.sha256(snapshot).hexdigest())


def _validated_task_node_from_sidecar_record(record: Any) -> TaskNode:
    if not isinstance(record, Mapping):
        _raise_task_snapshot_response_invalid("sidecar TaskNode snapshot is not a mapping")
    try:
        return TaskNode(
            node_id=str(record["node_id"]),
            task_id=str(record["task_id"]),
            capability_id=str(record["capability_id"]),
            assigned_instance_id=_optional_task_string(record, "assigned_instance_id"),
            status=NodeStatus(str(record["status"])),
            criticality=NodeCriticality(str(record["criticality"])),
            dependency_type=DependencyType(str(record["dependency_type"])),
            retry_policy=dict(record["retry_policy"]),
            timeout_policy=dict(record["timeout_policy"]),
            resource_class=_optional_task_string(record, "resource_class"),
            input_refs=tuple(str(value) for value in record["input_refs"]),
            output_refs=tuple(str(value) for value in record["output_refs"]),
            started_at=_optional_task_datetime(record, "started_at"),
            finished_at=_optional_task_datetime(record, "finished_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _raise_task_snapshot_response_invalid(str(exc))


def _optional_datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_task_string(record: Mapping[str, Any], name: str) -> str | None:
    value = record.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"mcp_task_snapshot_corrupt: {name} must be a string or null")
    return value


def _optional_task_datetime(record: Mapping[str, Any], name: str) -> datetime | None:
    value = _optional_task_string(record, name)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"mcp_task_snapshot_corrupt: {name} is not ISO-8601") from exc


def _raise_task_snapshot_response_invalid(message: str) -> NoReturn:
    error_code = runtime_error_policy("runtime_store_response_invalid")["code"]
    raise RuntimeError(f"{error_code}: {message}")


def _task_edge_from_sidecar_record(record: Mapping[str, Any]) -> TaskEdge:
    condition = str(record.get("condition", ""))
    return TaskEdge(
        from_node_id=str(record["from_node_id"]),
        to_node_id=str(record["to_node_id"]),
        edge_type=EdgeType(str(record["edge_type"])),
        condition=condition or None,
    )


def _task_edge_shadow_payload(task_id: str, edge: TaskEdge) -> dict[str, str]:
    return {
        "task_id": task_id,
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "edge_type": str(edge.edge_type),
        "condition_sha256": hashlib.sha256((edge.condition or "").encode("utf-8")).hexdigest(),
    }


def _task_edge_shadow_payload_from_record(record: Mapping[str, Any]) -> dict[str, str]:
    return {
        "task_id": str(record.get("task_id", "")),
        "from_node_id": str(record.get("from_node_id", "")),
        "to_node_id": str(record.get("to_node_id", "")),
        "edge_type": str(record.get("edge_type", "")),
        "condition_sha256": hashlib.sha256(str(record.get("condition", "")).encode("utf-8")).hexdigest(),
    }


def _artifact_to_sidecar_record(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "task_id": artifact.task_id,
        "producer_node_id": artifact.producer_node_id,
        "artifact_type": str(artifact.artifact_type),
        "storage_ref": artifact.storage_ref,
        "summary": artifact.summary or "",
        "is_complete": artifact.is_complete,
        "created_at": artifact.created_at.isoformat() if artifact.created_at is not None else "",
    }


def _artifact_from_sidecar_record(record: Mapping[str, Any]) -> Artifact:
    created_at = str(record.get("created_at", ""))
    return Artifact(
        artifact_id=str(record["artifact_id"]),
        task_id=str(record["task_id"]),
        producer_node_id=str(record["producer_node_id"]),
        artifact_type=ArtifactType(str(record["artifact_type"])),
        storage_ref=str(record["storage_ref"]),
        summary=str(record.get("summary", "")) or None,
        is_complete=bool(record["is_complete"]),
        created_at=datetime.fromisoformat(created_at) if created_at else None,
    )


def _artifact_shadow_payload(artifact: Artifact) -> dict[str, str]:
    return {
        "artifact_id": artifact.artifact_id,
        "task_id": artifact.task_id,
        "producer_node_id": artifact.producer_node_id,
        "artifact_type": str(artifact.artifact_type),
        "storage_ref_sha256": hashlib.sha256(artifact.storage_ref.encode("utf-8")).hexdigest(),
        "summary_sha256": hashlib.sha256((artifact.summary or "").encode("utf-8")).hexdigest(),
        "is_complete": str(artifact.is_complete),
    }


def _artifact_shadow_payload_from_record(record: Mapping[str, Any]) -> dict[str, str]:
    storage_ref = str(record.get("storage_ref", ""))
    summary = str(record.get("summary", ""))
    return {
        "artifact_id": str(record.get("artifact_id", "")),
        "task_id": str(record.get("task_id", "")),
        "producer_node_id": str(record.get("producer_node_id", "")),
        "artifact_type": str(record.get("artifact_type", "")),
        "storage_ref_sha256": hashlib.sha256(storage_ref.encode("utf-8")).hexdigest(),
        "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "is_complete": str(bool(record.get("is_complete", False))),
    }


def _consume_runtime_sidecar_response(operation_name: str, response: Any) -> dict[str, Any]:
    envelope = validate_runtime_sidecar_response(
        operation_name,
        normalize_runtime_sidecar_response(operation_name, response),
    )
    error = envelope.get("error")
    if isinstance(error, dict):
        raise RuntimeError(f"{error['code']}: {error['message']}")
    return envelope


async def _resolve_runtime_sidecar_call(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def _ensure_event_append_payload_within_rust_limit(event: EventRecord) -> None:
    payload_size = len(json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"))
    limit = runtime_resource_limit("event_payload_bytes")
    if payload_size > limit:
        error_code = runtime_error_policy("event_log_payload_too_large")["code"]
        raise ValueError(f"{error_code}: event payload exceeds Rust runtime sidecar limit of {limit} bytes")
