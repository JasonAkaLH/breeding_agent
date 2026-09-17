from __future__ import annotations

from collections.abc import Sequence

from src.core.models import (
    Conversation,
    MCPRemoteTaskBinding,
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
)
from src.integrations.mcp.cp7_artifacts import canonical_sha256

from .sqlalchemy_models import (
    ConversationRow,
    MCPRemoteTaskBindingRow,
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
    UserMCPServerRow,
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


def _mcp_owner_server_set_fingerprint(rows: Sequence[UserMCPServerRow]) -> str:
    payload = [
        [
            row.server_id,
            int(row.config_version),
            int(row.security_version),
            bool(row.enabled),
            str(row.health_status),
            bool(row.deletion_pending),
            row.deleted_at is not None,
        ]
        for row in sorted(rows, key=lambda item: item.server_id.encode("utf-8"))
    ]
    return canonical_sha256(payload)
