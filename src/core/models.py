from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import Any, Mapping

from .enums import (
    AckPolicy,
    ArtifactType,
    ConversationStatus,
    DependencyType,
    EdgeType,
    EventVisibility,
    InterruptStatus,
    MailboxChannel,
    MailboxDeliveryStatus,
    MessageRole,
    NodeCriticality,
    NodeStatus,
    RoutingMode,
    StrEnum,
    TaskStatus,
    UserMCPAuthType,
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)


JsonMapping = Mapping[str, Any]

MCP_ROLLOUT_DRILLS = frozenset(
    {
        "cancellation",
        "long_call_120_seconds",
        "disconnect_five_minutes",
        "restart_unknown",
        "mrtr_recovery",
        "tasks_recovery",
        "fair_queueing",
        "flag_rollback",
    }
)
MCP_ROLLOUT_DRILL_OUTCOMES = frozenset({"passed", "failed"})
_MCP_ROLLOUT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(slots=True, frozen=True)
class UserMCPServer:
    server_id: str
    owner_user_id: str
    display_name: str
    routing_description: str
    endpoint_url: str
    transport: UserMCPTransport
    protocol_preference: UserMCPProtocolPreference = UserMCPProtocolPreference.AUTO
    auth_type: UserMCPAuthType = UserMCPAuthType.NONE
    auth_metadata: JsonMapping = field(default_factory=dict)
    enabled: bool = True
    health_status: UserMCPHealthStatus = UserMCPHealthStatus.UNTESTED
    config_version: int = 1
    security_version: int = 1
    credential_configured: bool = False
    last_tested_at: datetime | None = None
    last_test_error_code: str | None = None
    deletion_pending: bool = False
    deleted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True, repr=False)
class UserMCPCredentialRecord:
    owner_user_id: str
    server_id: str
    credential_ciphertext: bytes
    credential_nonce: bytes
    encryption_version: int = 1
    credential_updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class UserMCPToolGrant:
    grant_id: str
    owner_user_id: str
    server_id: str
    tool_name: str
    server_security_version: int
    input_schema_sha256: str
    granted_at: datetime | None = None
    invalidated_at: datetime | None = None
    invalid_reason: str | None = None


@dataclass(slots=True, frozen=True)
class MCPBranchRecord:
    branch_id: str
    owner_user_id: str
    task_id: str
    node_id: str
    status: str
    initial_server_id: str | None = None
    tool_call_count: int = 0
    max_tool_calls: int = 20
    active_call_ref: str | None = None
    result_ref: str | None = None
    safe_summary: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MCPCallRecord:
    call_ref: str
    branch_id: str
    owner_user_id: str
    task_id: str
    node_id: str
    server_id: str
    tool_name: str
    status: str
    call_sequence: int
    arguments_sha256: str
    server_security_version: int
    input_schema_sha256: str
    server_config_version: int | None = None
    protocol_version: str | None = None
    input_field_names: tuple[str, ...] = ()
    may_have_dispatched: bool = False
    result_ref: str | None = None
    output_size_bytes: int | None = None
    safe_error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None


class MCPNoServerIntentTrigger(StrEnum):
    INITIAL_NO_PROFILE = "initial_no_profile"
    TARGET_SERVER_REVALIDATION = "target_server_revalidation"


class MCPNoServerIntentStatus(StrEnum):
    ARMED = "armed"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISPATCHED = "dispatched"
    RESOLVED = "resolved"
    CONVERGED = "converged"
    UNKNOWN = "unknown"


class MCPDispatchResumeOutboxStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    ABORTED = "aborted"


class MCPTerminalState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MCPTerminalResultCompletionMode(StrEnum):
    NORMAL_TERMINAL_PROJECTION = "normal_terminal_projection"
    LATE_RESULT_NO_CONTINUATION = "late_result_no_continuation"


class MCPExecutionTerminalProjectionStatus(StrEnum):
    UNKNOWN = "unknown"
    LATE_RESULT_RESOLVED = "late_result_resolved"


class MCPExecutionTerminalReason(StrEnum):
    TRUSTED_TERMINAL_RESULT_ABSENT = "trusted_terminal_result_absent"


class MCPTerminalErrorCode(StrEnum):
    MCP_RUNTIME_UNAVAILABLE = "mcp_runtime_unavailable"


class MCPUnavailableEventType(StrEnum):
    RUNTIME_UNAVAILABLE = "mcp.runtime_unavailable"


class MCPInitialIntentCreateResult(StrEnum):
    CREATED_UNAVAILABLE = "created_unavailable"
    RETRY_ROUTE = "retry_route"
    ALREADY_CREATED = "already_created"


class MCPTargetIntentArmResult(StrEnum):
    ARMED = "armed"
    UNAVAILABLE = "unavailable"
    ALREADY_ARMED = "already_armed"


class MCPTargetIntentResolveResult(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ALREADY_RESOLVED = "already_resolved"


class MCPNoServerConvergenceResult(StrEnum):
    CONVERGED = "converged"
    ALREADY_CONVERGED = "already_converged"
    ALREADY_TERMINAL = "already_terminal"
    UNKNOWN_REQUIRES_NO_REPLAY = "unknown_requires_no_replay"
    TRUSTED_TERMINAL_RESULT_REQUIRES_COMMIT = "trusted_terminal_result_requires_commit"


class MCPTerminalResultCommitResult(StrEnum):
    COMMITTED_NORMAL = "committed_normal"
    COMMITTED_LATE = "committed_late"
    ALREADY_COMMITTED = "already_committed"
    CONFLICT = "conflict"


class MCPDispatchFinalizeResult(StrEnum):
    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"
    CONFLICT = "conflict"


class MCPLegacyRetirementConvergenceResult(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    CONVERGED = "converged"
    ALREADY_CONVERGED = "already_converged"
    ALREADY_TERMINAL = "already_terminal"


class MCPCP7SafetyRecordKind(StrEnum):
    REGISTRATION = "registration"
    ATTESTATION = "attestation"
    VIOLATION = "violation"
    GAP = "gap"


class MCPCP7ReadyEpochEventKind(StrEnum):
    OPENED = "opened"
    READY = "ready"
    MAINTENANCE_STARTED = "maintenance_started"
    CLOSED = "closed"
    INVALIDATED = "invalidated"


@dataclass(slots=True, frozen=True)
class MCPNoServerIntent:
    intent_id: str
    owner_user_id: str
    task_id: str
    node_id: str | None
    trigger: MCPNoServerIntentTrigger
    requested_server_id: str | None
    requested_server_config_version: int | None
    requested_server_security_version: int | None
    owner_server_set_fingerprint: str | None
    resume_envelope_json: JsonMapping | None
    resume_envelope_sha256: str | None
    status: MCPNoServerIntentStatus
    revision: int
    evidence_sha256: str
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None


@dataclass(slots=True, frozen=True)
class UserMCPOwnerMutationGuard:
    owner_user_id: str
    revision: int
    server_set_fingerprint: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True, frozen=True)
class MCPDispatchResumeOutbox:
    outbox_id: str
    intent_id: str
    owner_user_id: str
    task_id: str
    node_id: str
    server_id: str
    resume_envelope_sha256: str
    payload_sha256: str
    status: MCPDispatchResumeOutboxStatus
    claim_owner: str | None
    claim_token: str | None
    lease_expires_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    result_receipt_id: str | None = None
    completion_mode: str | None = None


@dataclass(slots=True, frozen=True)
class MCPNoServerConvergenceReceipt:
    idempotency_key: str
    task_id: str
    intent_id: str
    owner_user_id: str
    terminal_code: str
    evidence_sha256: str
    runtime_unavailable_event_id: str
    task_failed_event_id: str
    committed_at: datetime


@dataclass(slots=True, frozen=True)
class MCPLegacyRetirementEvidence:
    evidence_id: str
    task_id: str
    inventory_id: str
    inventory_sha256: str
    bundle_revision: str | None
    capability_id: str | None
    may_have_dispatched: bool
    evidence_sha256: str
    created_at: datetime


@dataclass(slots=True, frozen=True)
class MCPLegacyRetirementReceipt:
    idempotency_key: str
    task_id: str
    inventory_id: str
    inventory_sha256: str
    terminal_reason_code: str
    terminal_evidence_sha256: str
    event_id: str
    committed_at: datetime


@dataclass(slots=True, frozen=True)
class MCPValidatedTerminalResultCandidate:
    candidate_id: str
    owner_user_id: str
    conversation_id: str
    task_id: str
    node_id: str
    intent_id: str
    call_id: str
    server_id: str
    server_config_version: int
    server_security_version: int
    terminal_state: MCPTerminalState
    result_payload_sha256: str
    safe_result_ref: str | None
    safe_result_ref_sha256: str | None
    safe_error_code: str | None
    sealed_at: datetime


@dataclass(slots=True, frozen=True)
class MCPTerminalResultReceipt:
    result_receipt_id: str
    candidate_id: str
    owner_user_id: str
    conversation_id: str
    task_id: str
    node_id: str
    intent_id: str
    call_id: str
    server_id: str
    server_config_version: int
    server_security_version: int
    terminal_state: MCPTerminalState
    result_payload_sha256: str
    safe_result_ref: str | None
    safe_result_ref_sha256: str | None
    safe_error_code: str | None
    completion_mode: MCPTerminalResultCompletionMode
    committed_at: datetime


@dataclass(slots=True, frozen=True)
class MCPExecutionTerminalProjection:
    projection_id: str
    owner_user_id: str
    conversation_id: str
    intent_id: str
    call_id: str
    task_id: str
    node_id: str
    status: MCPExecutionTerminalProjectionStatus
    revision: int
    no_replay: bool
    reason_code: MCPExecutionTerminalReason
    unknown_intent_revision: int
    unknown_event_id: str
    task_failed_event_id: str
    unknown_terminal_at: datetime
    task_terminal_status: str
    node_terminal_status: str
    result_receipt_id: str | None
    result_payload_sha256: str | None
    resolved_terminal_state: MCPTerminalState | None
    safe_result_ref: str | None
    safe_result_ref_sha256: str | None
    safe_error_code: str | None
    resolved_intent_revision: int | None
    resolution_event_id: str | None
    correction_event_id: str | None
    result_committed_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True, frozen=True)
class MCPCP7SafetyLedgerRecord:
    record_id: str
    candidate_id: str
    epoch_id: str
    config_fingerprint: str
    record_kind: MCPCP7SafetyRecordKind
    red_line: str | None
    hook_id: str | None
    bucket_started_at: datetime | None
    bucket_ended_at: datetime | None
    reason_code: str
    value: int
    boundary_source_sha256: str | None
    payload_sha256: str
    recorded_at: datetime


@dataclass(slots=True, frozen=True)
class MCPCP7ReadyEpochEvent:
    event_id: str
    candidate_id: str
    epoch_id: str
    predecessor_epoch_id: str | None
    event_kind: MCPCP7ReadyEpochEventKind
    container_id: str
    image_id: str
    config_fingerprint: str
    boundary_at: datetime
    audit_device: str
    audit_inode: int
    audit_offset: int
    ledger_record_count: int
    inflight_state_sha256: str
    payload_sha256: str


@dataclass(slots=True, frozen=True)
class MCPCP7CandidateGuard:
    candidate_id: str
    invalid_latched: bool
    first_invalid_record_id: str | None
    first_invalid_reason: str | None
    first_invalid_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True, frozen=True)
class MCPCP7SafetySnapshot:
    schema: str
    candidate_id: str
    config_fingerprint: str
    registry_definition_sha256: str
    epoch_chain_sha256: str
    ready_epochs: tuple[str, ...]
    maintenance_boundary_count: int
    observation_started_at: datetime
    observation_ended_at: datetime
    registration_count_by_red_line: JsonMapping
    attestation_interval_count_by_red_line: JsonMapping
    violation_count_by_red_line: JsonMapping
    gap_count: int
    invalid_latched: bool
    record_count: int
    ordered_record_payload_sha256s: tuple[str, ...]
    snapshot_sha256: str


@dataclass(slots=True, frozen=True, repr=False)
class MCPRemoteTaskBinding:
    safe_remote_task_ref: str
    owner_user_id: str
    task_id: str
    node_id: str
    call_ref: str
    server_id: str
    protocol_version: str
    remote_task_ciphertext: bytes
    remote_task_nonce: bytes
    encryption_version: int
    last_status: str
    next_poll_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None
    published_at: datetime | None = None
    continuation_plan: JsonMapping = field(default_factory=dict)
    claim_owner: str | None = None
    claim_token: str | None = None
    lease_expires_at: datetime | None = None
    revision: int = 0


@dataclass(slots=True, frozen=True)
class MCPRemoteTaskOutbox:
    outbox_id: str
    kind: str
    owner_user_id: str
    task_id: str
    node_id: str
    call_ref: str
    safe_remote_task_ref: str
    payload: JsonMapping = field(default_factory=dict)
    status: str = "pending"
    claim_owner: str | None = None
    claim_token: str | None = None
    lease_expires_at: datetime | None = None
    revision: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    continuation_admitted_at: datetime | None = None
    continuation_dispatched_at: datetime | None = None
    continuation_status: str | None = None
    continuation_claim_owner: str | None = None
    continuation_claim_token: str | None = None
    continuation_lease_expires_at: datetime | None = None
    continuation_revision: int = 0
    continuation_node_ids: tuple[str, ...] = ()
    continuation_safe_error_code: str | None = None
    completed_at: datetime | None = None


@dataclass(slots=True, frozen=True, repr=False)
class MCPSealedState:
    sealed_state_ref: str
    owner_user_id: str
    task_id: str
    node_id: str
    call_ref: str
    state_kind: str
    ciphertext: bytes
    nonce: bytes
    encryption_version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MCPConnectionLease:
    connection_id: str
    owner_user_id: str
    task_id: str
    instance_id: str
    lease_expires_at: datetime
    disconnected_at: datetime | None = None
    auth_generation: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MCPAuditEvent:
    audit_event_id: str
    owner_user_id: str
    event_type: str
    occurred_at: datetime
    expires_at: datetime
    task_id: str | None = None
    node_id: str | None = None
    server_id: str | None = None
    call_ref: str | None = None
    safe_payload: JsonMapping = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class MCPLegacyMigrationRecord:
    migration_id: str
    event_type: str
    plan_fingerprint: str
    source_server_id: str
    source_fingerprint: str
    owner_consumer_ref: str
    target_server_id: str
    target_consumer_set_digest: str
    capability_obligations_fingerprint: str
    catalog_fingerprint: str
    capability_fingerprint: str
    validator_provenance_fingerprint: str
    credential_digest: str
    disposition: str
    occurred_at: datetime
    evidence_expires_at: datetime


@dataclass(slots=True, frozen=True)
class MCPLegacyMigrationBatchResult:
    servers: tuple[UserMCPServer, ...]
    records: tuple[MCPLegacyMigrationRecord, ...]
    applied: bool


@dataclass(slots=True, frozen=True)
class MCPRolloutGateScope:
    environment_id: str
    rollout_program: str = "user_mcp_phase3"
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MCPRolloutDrillObservation:
    drill_observation_id: str
    environment_id: str
    deployment_id: str
    config_fingerprint: str
    drill: str
    outcome: str
    observed_at: datetime
    recorded_at: datetime
    expires_at: datetime
    payload_digest: str
    rollout_program: str = "user_mcp_phase3"
    stage: str = "internal_enforce"


def canonical_mcp_rollout_drill_observation_digest(
    observation: MCPRolloutDrillObservation,
) -> str:
    from src.integrations.mcp.rollout_evidence import canonical_evidence_content_digest

    return canonical_evidence_content_digest(
        {
            item.name: getattr(observation, item.name)
            for item in fields(observation)
            if item.name != "payload_digest"
        }
    )


def seal_mcp_rollout_drill_observation(
    observation: MCPRolloutDrillObservation,
) -> MCPRolloutDrillObservation:
    draft = replace(observation, payload_digest="")
    return replace(
        draft,
        payload_digest=canonical_mcp_rollout_drill_observation_digest(draft),
    )


def validate_mcp_rollout_drill_observation(
    observation: MCPRolloutDrillObservation,
) -> tuple[str, ...]:
    blockers: list[str] = []
    required = (
        observation.drill_observation_id,
        observation.environment_id,
        observation.deployment_id,
        observation.config_fingerprint,
    )
    if any(not isinstance(value, str) or not value.strip() for value in required):
        blockers.append("required_field_invalid")
    if (
        observation.rollout_program != "user_mcp_phase3"
        or observation.stage != "internal_enforce"
    ):
        blockers.append("scope_invalid")
    if (
        not isinstance(observation.config_fingerprint, str)
        or _MCP_ROLLOUT_SHA256_RE.fullmatch(observation.config_fingerprint) is None
    ):
        blockers.append("config_fingerprint_invalid")
    if not isinstance(observation.drill, str) or observation.drill not in MCP_ROLLOUT_DRILLS:
        blockers.append("drill_invalid")
    if (
        not isinstance(observation.outcome, str)
        or observation.outcome not in MCP_ROLLOUT_DRILL_OUTCOMES
    ):
        blockers.append("outcome_invalid")
    if not all(
        _is_aware_timestamp(value)
        for value in (
            observation.observed_at,
            observation.recorded_at,
            observation.expires_at,
        )
    ):
        blockers.append("timestamp_invalid")
    elif (
        observation.recorded_at < observation.observed_at
        or observation.expires_at <= observation.recorded_at
    ):
        blockers.append("timestamp_order_invalid")
    if (
        not isinstance(observation.payload_digest, str)
        or _MCP_ROLLOUT_SHA256_RE.fullmatch(observation.payload_digest) is None
    ):
        blockers.append("digest_invalid")
    else:
        try:
            expected_digest = canonical_mcp_rollout_drill_observation_digest(observation)
        except (TypeError, ValueError):
            blockers.append("digest_invalid")
        else:
            if not hmac.compare_digest(observation.payload_digest, expected_digest):
                blockers.append("digest_invalid")
    return tuple(dict.fromkeys(blockers))


def _is_aware_timestamp(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


@dataclass(slots=True, frozen=True)
class MCPRolloutMetricBucket:
    metric_bucket_id: str
    environment_id: str
    deployment_id: str
    stage: str
    config_fingerprint: str
    metric_name: str
    bucket_started_at: datetime
    bucket_ended_at: datetime
    execution_path: str
    routing_mode: str
    transport: str
    protocol_version: str
    adapter: str
    result_category: str
    error_category: str
    latency_bucket: str
    value: int
    call_kind: str | None = None
    red_line: str | None = None
    rollout_program: str = "user_mcp_phase3"
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MCPShadowAuditSample:
    sample_id: str
    environment_id: str
    deployment_id: str
    stage: str
    config_fingerprint: str
    manifest_fingerprint: str
    fixture_fingerprint: str
    mapping_fingerprint: str
    scenario: str
    nonce: str
    legacy_outcome: str
    shadow_outcome: str
    transport: str
    endpoint_policy: str
    comparison: str
    blockers: tuple[str, ...]
    payload_digest: str
    observed_at: datetime
    recorded_at: datetime
    expires_at: datetime
    safe_owner_ref: str | None = None
    safe_task_ref: str | None = None
    safe_call_ref: str | None = None
    rollout_program: str = "user_mcp_phase3"


@dataclass(slots=True, frozen=True)
class MCPRolloutEvidenceSnapshot:
    evidence_id: str
    environment_id: str
    git_sha: str
    deployment_id: str
    stage: str
    config_fingerprint: str
    window_started_at: datetime
    window_ended_at: datetime
    recorded_at: datetime
    producer: str
    source: str
    snapshot_id: int
    nonce: str
    evidence_kind: str
    payload: JsonMapping
    payload_digest: str
    rollout_program: str = "user_mcp_phase3"
    attestation_key_id: str | None = None
    attestation_signature: str | None = None


@dataclass(slots=True, frozen=True)
class MCPRolloutStageApproval:
    approval_id: str
    environment_id: str
    deployment_id: str
    stage: str
    config_fingerprint: str
    evidence_id: str
    reason: str
    approver: str
    created_at: datetime
    rollout_program: str = "user_mcp_phase3"


@dataclass(slots=True, frozen=True)
class MCPRolloutDeploymentActivation:
    activation_id: str
    environment_id: str
    deployment_id: str
    stage: str
    config_fingerprint: str
    approval_id: str
    evidence_id: str
    previous_activation_id: str | None
    operator_reason: str
    is_rollback: bool
    created_at: datetime
    rollout_program: str = "user_mcp_phase3"


@dataclass(slots=True, frozen=True)
class MCPRolloutPromotionBlock:
    block_id: str
    environment_id: str
    deployment_id: str
    stage: str
    config_fingerprint: str
    evidence_id: str
    reason_code: str
    created_at: datetime
    rollout_program: str = "user_mcp_phase3"


@dataclass(slots=True, frozen=True)
class MCPRolloutBlockResolution:
    resolution_id: str
    block_id: str
    approval_id: str
    evidence_id: str
    reason: str
    approver: str
    created_at: datetime


@dataclass(slots=True, frozen=True)
class MCPRolloutInstanceConfigLease:
    instance_config_id: str
    environment_id: str
    deployment_id: str
    instance_id: str
    stage: str
    config_fingerprint: str
    activation_id: str
    lease_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    rollout_program: str = "user_mcp_phase3"


@dataclass(slots=True, frozen=True)
class UserMCPHealthAttempt:
    attempt_id: str
    owner_user_id: str
    server_id: str
    config_version: int
    security_version: int
    runner_instance_id: str
    lease_expires_at: datetime
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class UserMCPScopeLease:
    scope_id: str
    owner_user_id: str
    server_id: str
    security_version: int
    gateway_instance_id: str
    lease_expires_at: datetime
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True, repr=False)
class MAFMasterKeyValidation:
    singleton_key: int
    validation_nonce: bytes
    validation_ciphertext: bytes
    derivation_version: int
    created_at: datetime

    def __post_init__(self) -> None:
        if self.singleton_key != 1:
            raise ValueError("MAF master key validation singleton_key must be 1")
        if not isinstance(self.validation_nonce, bytes) or len(self.validation_nonce) != 12:
            raise ValueError("MAF master key validation nonce must be 12 bytes")
        if self.derivation_version != 1:
            raise ValueError("unsupported MAF master key derivation version")
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise ValueError("MAF master key validation created_at must be UTC")
        if self.created_at.utcoffset().total_seconds() != 0:
            raise ValueError("MAF master key validation created_at must be UTC")


@dataclass(slots=True, frozen=True)
class Conversation:
    conversation_id: str
    username: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    current_task_id: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    delete_runner_id: str | None = None
    delete_requested_at: datetime | None = None
    delete_started_at: datetime | None = None
    delete_finished_at: datetime | None = None
    delete_failed_at: datetime | None = None
    delete_error_code: str | None = None
    delete_error_summary: str | None = None
    delete_phase: str | None = None


@dataclass(slots=True, frozen=True)
class ConversationMemorySummary:
    summary_id: str
    conversation_id: str
    username: str
    covered_until_turn_id: str | None
    covered_until_message_id: str | None
    covered_until_created_at: datetime | None
    summary_text: str
    source_message_count: int
    source_message_ids_hash: str
    estimated_tokens: int
    summary_version: str
    compression_policy_version: str
    model_metadata_safe: JsonMapping = field(default_factory=dict)
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None




@dataclass(slots=True, frozen=True)
class ConversationFileResource:
    file_id: str
    conversation_id: str
    username: str
    original_filename: str
    content_type: str
    file_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    preview: JsonMapping = field(default_factory=dict)
    description_status: str = "pending"
    description_summary: str | None = None
    description_ref: str | None = None
    status: str = "active"
    normalized_filename: str | None = None
    normalized_content_type: str | None = None
    requires_sheet_selection: bool = False
    selected_sheet: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ConversationFileIndexRepairMarker:
    conversation_id: str
    repair_kind: str = "conversation_file_index"
    status: str = "pending"
    reason_code: str = ""
    affected_upload_ids: tuple[str, ...] = ()
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class AuthUserToken:
    username: str
    api_token_hash: str | None = None
    token_issued_at: datetime | None = None
    token_last_used_at: datetime | None = None
    auth_generation: int = 0
    auth_generation_updated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Message:
    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    task_id: str | None = None
    stream_status: str | None = None
    created_at: datetime | None = None
    message_type: str = "chat"
    metadata: JsonMapping = field(default_factory=dict)
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class FileUploadMessageProjection:
    upload_id: str
    conversation_id: str
    content: str
    metadata: JsonMapping = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Task:
    task_id: str
    conversation_id: str
    root_message_id: str
    status: TaskStatus = TaskStatus.ACCEPTED
    routing_mode: RoutingMode = RoutingMode.AUTO
    requested_capability_id: str | None = None
    root_node_id: str | None = None
    summary: str | None = None
    cancel_requested_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    mcp_execution_mode: str | None = None
    mcp_shadow_enabled: bool | None = None
    mcp_rollout_config_version: str | None = None
    mcp_route_reason_code: str | None = None
    mcp_rollout_mode: str | None = None


@dataclass(slots=True, frozen=True)
class PendingSkillContext:
    context_id: str
    conversation_id: str
    username: str | None
    capability_id: str
    skill_name: str
    source_task_id: str
    source_message_id: str
    original_user_message: str
    missing_requirements: tuple[str, ...]
    assistant_message: str
    status: str = "pending_user_input"
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TaskNode:
    node_id: str
    task_id: str
    capability_id: str
    assigned_instance_id: str | None = None
    status: NodeStatus = NodeStatus.PENDING
    criticality: NodeCriticality = NodeCriticality.REQUIRED
    dependency_type: DependencyType = DependencyType.HARD
    retry_policy: JsonMapping = field(default_factory=dict)
    timeout_policy: JsonMapping = field(default_factory=dict)
    resource_class: str | None = None
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TaskEdge:
    from_node_id: str
    to_node_id: str
    edge_type: EdgeType = EdgeType.DATA
    condition: str | None = None


@dataclass(slots=True, frozen=True)
class Artifact:
    artifact_id: str
    task_id: str
    producer_node_id: str
    artifact_type: ArtifactType
    storage_ref: str
    summary: str | None = None
    is_complete: bool = False
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TaskInputAttachment:
    attachment_id: str
    task_id: str
    conversation_id: str
    source_kind: str
    source_upload_id: str | None = None
    source_message_id: str | None = None
    interrupt_answer_id: str | None = None
    filename: str = ""
    content_type: str = ""
    file_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    prompt_artifact: JsonMapping = field(default_factory=dict)
    skill_artifact: JsonMapping = field(default_factory=dict)
    source_payload: JsonMapping = field(default_factory=dict)
    selected_sheet: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class EventRecord:
    event_id: str
    conversation_id: str
    task_id: str
    node_id: str | None = None
    agent_id: str | None = None
    event_type: str = ""
    payload: JsonMapping = field(default_factory=dict)
    visibility: EventVisibility = EventVisibility.INTERNAL
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MailboxMessage:
    message_id: str
    conversation_id: str
    task_id: str
    node_id: str | None = None
    parent_message_id: str | None = None
    correlation_id: str | None = None
    from_agent: str = ""
    to_agent: str | None = None
    to_role: str | None = None
    channel: MailboxChannel = MailboxChannel.PEER_COLLABORATION
    message_type: str = ""
    ack_policy: AckPolicy = AckPolicy.LIGHT
    priority: int = 0
    payload: JsonMapping = field(default_factory=dict)
    payload_schema_version: int = 1
    created_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MailboxDelivery:
    delivery_id: str
    message_id: str
    recipient_agent: str
    recipient_role: str | None = None
    status: MailboxDeliveryStatus = MailboxDeliveryStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 1
    ttl_seconds: int | None = None
    expires_at: datetime | None = None
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    next_retry_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Interrupt:
    interrupt_id: str
    conversation_id: str
    task_id: str
    node_id: str
    source_agent: str
    source_message_id: str
    question: str
    reason_code: str
    required_fields: JsonMapping = field(default_factory=dict)
    status: InterruptStatus = InterruptStatus.OPEN
    expires_at: datetime | None = None
    created_at: datetime | None = None
    answered_at: datetime | None = None
    cancelled_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class InterruptAnswer:
    interrupt_answer_id: str
    interrupt_id: str
    answer_payload: JsonMapping
    source_message_id: str | None = None
    accepted: bool = False
    created_at: datetime | None = None
    accepted_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SlotCollection:
    collection_id: str
    task_id: str
    node_id: str
    conversation_id: str
    capability_id: str
    skill_name: str
    kind: str
    status: str
    round: int = 1
    revision: int = 0
    selected_schema_id: str | None = None
    selected_entrypoint: str | None = None
    skill_bundle_revision: str | None = None
    contract_revision: str | None = None
    schema_digest: str | None = None
    schema_snapshot: JsonMapping = field(default_factory=dict)
    slots: JsonMapping = field(default_factory=dict)
    resolved: JsonMapping = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    invalid: tuple[JsonMapping, ...] = ()
    last_question: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    failed_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SlotEvent:
    slot_event_id: str
    collection_id: str
    task_id: str
    node_id: str
    conversation_id: str
    event_type: str
    round: int = 1
    revision: int = 0
    idempotency_key: str | None = None
    payload: JsonMapping = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    node_id: str
    agent_id: str
    snapshot_ref: str
    snapshot_kind: str
    resume_token: str
    source_message_id: str | None = None
    created_at: datetime | None = None
    invalidated_at: datetime | None = None
