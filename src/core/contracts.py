from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .enums import EventVisibility, NodeStatus, TaskStatus
from .models import (
    Artifact,
    AuthUserToken,
    Checkpoint,
    Conversation,
    ConversationMemorySummary,
    ConversationFileResource,
    ConversationFileIndexRepairMarker,
    EventRecord,
    FileUploadMessageProjection,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    MCPAuditEvent,
    MCPBranchRecord,
    MCPCallRecord,
    MCPApprovalDecisionResult,
    MCPApprovalSuspendResult,
    MCPConnectionLease,
    MCPCP7CandidateGuard,
    MCPCP7ReadyEpochEvent,
    MCPCP7ReadyEpochEventKind,
    MCPCP7SafetyLedgerRecord,
    MCPCP7SafetySnapshot,
    MCPDispatchResumeOutbox,
    MCPDurableResultSnapshot,
    MCPDurableResultLifecycle,
    MCPDispatchFinalizeResult,
    MCPExecutionTerminalProjection,
    MCPInitialIntentCreateResult,
    MCPInputSuspendResult,
    MCPMRTRAnswerResult,
    MCPLegacyRetirementEvidence,
    MCPLegacyRetirementConvergenceResult,
    MCPLegacyMigrationBatchResult,
    MCPLegacyMigrationRecord,
    MCPNoServerConvergenceResult,
    MCPNoServerConvergenceReceipt,
    MCPNoServerIntent,
    MCPPendingActionPayloadSnapshot,
    MCPPendingToolAction,
    MCPRemoteTaskBinding,
    MCPRemoteTaskOutbox,
    MCPRolloutBlockResolution,
    MCPRolloutDeploymentActivation,
    MCPRolloutDrillObservation,
    MCPRolloutEvidenceSnapshot,
    MCPRolloutGateScope,
    MCPRolloutInstanceConfigLease,
    MCPRolloutMetricBucket,
    MCPShadowAuditSample,
    MCPRolloutPromotionBlock,
    MCPRolloutStageApproval,
    MCPSealedState,
    MCPTargetIntentArmResult,
    MCPTargetIntentResolveResult,
    MCPTerminalResultCommitResult,
    MCPTerminalResultReceipt,
    MCPTerminalCandidateLifecycle,
    MCPTerminalCandidateSnapshot,
    Message,
    PendingSkillContext,
    PlannerReplanClaim,
    SlotCollection,
    SlotEvent,
    Task,
    TaskEdge,
    TaskInputAttachment,
    TaskNode,
    UserMCPCredentialRecord,
    UserMCPOwnerMutationGuard,
    UserMCPHealthAttempt,
    UserMCPScopeLease,
    UserMCPServer,
    UserMCPToolGrant,
    MAFMasterKeyValidation,
)


Payload = Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class CapabilityExecutionRequest:
    capability_id: str
    conversation_id: str
    task_id: str
    node_id: str
    input_payload: Payload = field(default_factory=dict)
    context_refs: tuple[str, ...] = ()
    dependency_outputs: Mapping[str, Payload] = field(default_factory=dict)
    metadata: Payload = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CapabilityExecutionError:
    code: str
    message: str
    retriable: bool = False
    metadata: Payload = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CapabilityExecutionResult:
    capability_id: str
    task_id: str
    node_id: str
    output_payload: Payload = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    events: tuple[EventRecord, ...] = ()
    interrupt: Interrupt | None = None
    error: CapabilityExecutionError | None = None
    metadata: Payload = field(default_factory=dict)


@runtime_checkable
class StoragePort(Protocol):
    async def list_user_mcp_servers(self, owner_user_id: str) -> list[UserMCPServer]: ...

    async def get_user_mcp_server(self, owner_user_id: str, server_id: str) -> UserMCPServer | None: ...

    async def create_user_mcp_server(
        self,
        server: UserMCPServer,
        credential: UserMCPCredentialRecord | None = None,
    ) -> UserMCPServer: ...

    async def create_user_mcp_servers_atomic(
        self,
        candidates: Sequence[tuple[UserMCPServer, UserMCPCredentialRecord | None]],
    ) -> list[UserMCPServer]: ...

    async def apply_legacy_mcp_migration_atomic(
        self,
        candidates: Sequence[
            tuple[
                UserMCPServer,
                UserMCPCredentialRecord | None,
                MCPLegacyMigrationRecord,
            ]
        ],
    ) -> MCPLegacyMigrationBatchResult: ...

    async def get_mcp_legacy_migration_record(
        self, migration_id: str
    ) -> MCPLegacyMigrationRecord | None: ...

    async def update_user_mcp_server(
        self,
        owner_user_id: str,
        server_id: str,
        *,
        changes: Mapping[str, Any],
        credential_operation: str = "retain",
        credential: UserMCPCredentialRecord | None = None,
        security_sensitive: bool = False,
        expected_config_version: int | None = None,
        expected_security_version: int | None = None,
        updated_at: datetime,
    ) -> UserMCPServer | None: ...

    async def get_user_mcp_credential(
        self, owner_user_id: str, server_id: str
    ) -> UserMCPCredentialRecord | None: ...

    async def claim_user_mcp_health_attempt(self, attempt: UserMCPHealthAttempt) -> bool: ...

    async def renew_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> bool: ...

    async def complete_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
        health_status: str,
        error_code: str | None,
        completed_at: datetime,
    ) -> UserMCPServer | None: ...

    async def expire_user_mcp_health_attempts(
        self, *, now: datetime, error_code: str = "test_interrupted"
    ) -> int: ...

    async def release_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
    ) -> bool: ...

    async def acquire_user_mcp_scope_lease(self, lease: UserMCPScopeLease) -> bool: ...

    async def renew_user_mcp_scope_lease(
        self,
        scope_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        gateway_instance_id: str,
        security_version: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> bool: ...

    async def release_user_mcp_scope_lease(
        self, scope_id: str, *, gateway_instance_id: str
    ) -> bool: ...

    async def list_live_user_mcp_scope_leases(
        self,
        *,
        now: datetime,
        owner_user_id: str | None = None,
        server_id: str | None = None,
    ) -> list[UserMCPScopeLease]: ...

    async def expire_user_mcp_scope_leases(self, *, now: datetime) -> int: ...

    async def mark_user_mcp_server_deleted(
        self, owner_user_id: str, server_id: str, *, deleted_at: datetime
    ) -> UserMCPServer | None: ...

    async def list_pending_user_mcp_server_deletions(self) -> list[UserMCPServer]: ...

    async def finalize_user_mcp_server_delete(
        self, owner_user_id: str, server_id: str, *, now: datetime
    ) -> bool: ...

    async def save_user_mcp_tool_grant(self, grant: UserMCPToolGrant) -> UserMCPToolGrant: ...

    async def list_user_mcp_tool_grants(
        self, owner_user_id: str, server_id: str | None = None
    ) -> list[UserMCPToolGrant]: ...

    async def get_valid_user_mcp_tool_grant(
        self,
        owner_user_id: str,
        server_id: str,
        tool_name: str,
        *,
        server_security_version: int,
        input_schema_sha256: str,
    ) -> UserMCPToolGrant | None: ...

    async def delete_user_mcp_tool_grant(
        self, owner_user_id: str, server_id: str, grant_id: str
    ) -> bool: ...

    async def delete_user_mcp_tool_grant_by_id(
        self, owner_user_id: str, grant_id: str
    ) -> bool: ...

    async def clear_user_mcp_tool_grants(self, owner_user_id: str, server_id: str) -> int: ...

    async def invalidate_user_mcp_tool_grants(
        self,
        owner_user_id: str,
        server_id: str,
        *,
        invalidated_at: datetime,
        invalid_reason: str,
        tool_name: str | None = None,
        input_schema_sha256: str | None = None,
    ) -> int: ...

    async def save_mcp_branch_record(self, record: MCPBranchRecord) -> MCPBranchRecord: ...

    async def get_mcp_branch_record(
        self, owner_user_id: str, task_id: str, branch_id: str
    ) -> MCPBranchRecord | None: ...

    async def list_mcp_branch_records(
        self,
        owner_user_id: str,
        *,
        task_id: str | None = None,
        statuses: tuple[str, ...] = (),
    ) -> list[MCPBranchRecord]: ...

    async def reserve_mcp_call(self, record: MCPCallRecord) -> bool: ...

    async def mark_mcp_call_may_have_dispatched(
        self, owner_user_id: str, task_id: str, call_ref: str, *, updated_at: datetime
    ) -> bool: ...

    async def get_mcp_call_record(
        self, owner_user_id: str, task_id: str, call_ref: str
    ) -> MCPCallRecord | None: ...

    async def list_mcp_call_records(
        self, owner_user_id: str, task_id: str, *, branch_id: str | None = None
    ) -> list[MCPCallRecord]: ...

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
    ) -> MCPCallRecord | None: ...

    async def get_user_mcp_owner_mutation_guard(
        self, owner_user_id: str
    ) -> UserMCPOwnerMutationGuard | None: ...

    async def get_mcp_no_server_intent(
        self, intent_id: str
    ) -> MCPNoServerIntent | None: ...

    async def list_unresolved_mcp_no_server_intents(
        self,
    ) -> list[MCPNoServerIntent]: ...

    async def list_mcp_no_server_intents(
        self,
        *,
        statuses: tuple[str, ...] = (),
        after_updated_at: datetime | None = None,
        after_intent_id: str | None = None,
        limit: int = 10_000,
    ) -> list[MCPNoServerIntent]: ...

    async def create_user_mcp_initial_intent(
        self,
        task: Task,
        occurred_at: datetime,
    ) -> MCPInitialIntentCreateResult: ...

    async def arm_user_mcp_target_intent(
        self,
        task_id: str,
        node_id: str,
        requested_server_id: str,
        resume_envelope: Mapping[str, Any],
        occurred_at: datetime,
    ) -> MCPTargetIntentArmResult: ...

    async def resolve_user_mcp_target_intent(
        self,
        intent_id: str,
        occurred_at: datetime,
    ) -> MCPTargetIntentResolveResult: ...

    async def get_mcp_dispatch_resume_outbox(
        self, outbox_id: str
    ) -> MCPDispatchResumeOutbox | None: ...

    async def get_mcp_pending_tool_action(
        self, action_id: str
    ) -> MCPPendingToolAction | None: ...

    async def get_latest_approved_mcp_tool_action(
        self, owner_user_id: str, task_id: str, node_id: str
    ) -> MCPPendingToolAction | None: ...

    async def get_mcp_pending_tool_action_for_interrupt(
        self, interrupt_id: str
    ) -> MCPPendingToolAction | None: ...

    async def list_mcp_dispatch_resume_outboxes(
        self,
        *,
        statuses: tuple[str, ...] = (),
        after_updated_at: datetime | None = None,
        after_outbox_id: str | None = None,
        limit: int = 10_000,
    ) -> list[MCPDispatchResumeOutbox]: ...

    async def claim_mcp_dispatch_resume_outbox(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MCPDispatchResumeOutbox | None: ...

    async def reclaim_mcp_dispatch_resume_outbox(
        self, outbox_id: str, expected_revision: int, now: datetime
    ) -> MCPDispatchResumeOutbox | None: ...

    async def abort_mcp_dispatch_resume_outbox(
        self, outbox_id: str, expected_revision: int, occurred_at: datetime
    ) -> MCPDispatchResumeOutbox | None: ...

    async def claim_mcp_dispatch(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MCPDispatchResumeOutbox | None: ...

    async def renew_mcp_dispatch_claim(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MCPDispatchResumeOutbox | None: ...

    async def consume_mcp_dispatch_selector_step(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        occurred_at: datetime,
    ) -> MCPDispatchResumeOutbox | None: ...

    async def release_or_recover_mcp_dispatch_claim(
        self,
        outbox_id: str,
        expected_revision: int,
        now: datetime,
    ) -> MCPDispatchResumeOutbox | None: ...

    async def suspend_mcp_for_approval(
        self,
        intent_id: str,
        outbox_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        action: MCPPendingToolAction,
        interrupt: Interrupt,
        payload_snapshot: MCPPendingActionPayloadSnapshot,
        occurred_at: datetime,
    ) -> MCPApprovalSuspendResult: ...

    async def accept_mcp_tool_approval(
        self,
        interrupt_id: str,
        answer: InterruptAnswer,
        decision: str,
        occurred_at: datetime,
    ) -> MCPApprovalDecisionResult: ...

    async def suspend_mcp_for_input(
        self,
        intent_id: str,
        outbox_id: str,
        call_id: str,
        sealed_state_ref: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        interrupt: Interrupt,
        occurred_at: datetime,
    ) -> MCPInputSuspendResult: ...

    async def accept_mcp_mrtr_answer(
        self,
        interrupt_id: str,
        answer: InterruptAnswer,
        occurred_at: datetime,
    ) -> MCPMRTRAnswerResult: ...

    async def admit_approved_mcp_action(
        self,
        intent_id: str,
        outbox_id: str,
        action_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        expected_action_revision: int,
        claim_owner: str,
        claim_token: str,
        payload_snapshot: MCPPendingActionPayloadSnapshot,
        record: MCPCallRecord,
        occurred_at: datetime,
        *,
        action_candidate: MCPPendingToolAction | None = None,
        cp7_candidate_id: str | None = None,
        cp7_epoch_id: str | None = None,
    ) -> bool: ...

    async def admit_mrtr_continuation(
        self,
        intent_id: str,
        outbox_id: str,
        original_call_id: str,
        sealed_state_ref: str,
        answer_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        payload_snapshot: MCPPendingActionPayloadSnapshot,
        record: MCPCallRecord,
        occurred_at: datetime,
        *,
        cp7_candidate_id: str | None = None,
        cp7_epoch_id: str | None = None,
    ) -> bool: ...

    async def commit_mcp_call_terminal(
        self,
        call_id: str,
        candidate_id: str,
        outbox_id: str,
        expected_outbox_revision: int,
        claim_owner: str | None,
        claim_token: str | None,
        candidate_snapshot: MCPTerminalCandidateSnapshot,
        result_snapshot: MCPDurableResultSnapshot | None,
        occurred_at: datetime,
        *,
        remote_binding_ref: str | None = None,
        remote_claim_owner: str | None = None,
        remote_claim_token: str | None = None,
        remote_expected_revision: int | None = None,
    ) -> MCPTerminalResultCommitResult: ...

    async def recover_mcp_terminal_candidate(
        self,
        candidate_snapshot: MCPTerminalCandidateSnapshot,
        result_snapshot: MCPDurableResultSnapshot | None,
        occurred_at: datetime,
    ) -> MCPTerminalResultCommitResult: ...

    async def list_incomplete_mcp_terminal_candidate_lifecycles(
        self, *, limit: int = 1000
    ) -> list[MCPTerminalCandidateLifecycle]: ...

    async def claim_mcp_terminal_candidate_archives(
        self, now: datetime, *, limit: int = 1000
    ) -> list[MCPTerminalCandidateLifecycle]: ...

    async def finish_mcp_terminal_candidate_archive(
        self, candidate_id: str, expected_revision: int, archived_at: datetime
    ) -> MCPTerminalCandidateLifecycle | None: ...

    async def claim_mcp_terminal_candidate_deletions(
        self, now: datetime, *, limit: int = 1000
    ) -> list[MCPTerminalCandidateLifecycle]: ...

    async def finish_mcp_terminal_candidate_deletion(
        self, candidate_id: str, expected_revision: int, deleted_at: datetime
    ) -> MCPTerminalCandidateLifecycle | None: ...

    async def list_incomplete_mcp_durable_result_lifecycles(
        self, *, limit: int = 1000
    ) -> list[MCPDurableResultLifecycle]: ...

    async def get_mcp_durable_result_lifecycle(
        self, result_ref: str
    ) -> MCPDurableResultLifecycle | None: ...

    async def reconcile_mcp_durable_result_lifecycle(
        self,
        snapshot: MCPDurableResultSnapshot,
        occurred_at: datetime,
    ) -> MCPDurableResultLifecycle | None: ...

    async def mark_mcp_durable_result_artifact_owned(
        self,
        result_ref: str,
        expected_revision: int,
        artifact_id: str,
        expected_size_bytes: int,
        expected_content_sha256: str,
        occurred_at: datetime,
    ) -> MCPDurableResultLifecycle | None: ...

    async def claim_mcp_durable_result_deletions(
        self, now: datetime, *, limit: int = 1000
    ) -> list[MCPDurableResultLifecycle]: ...

    async def finish_mcp_durable_result_deletion(
        self, result_ref: str, expected_revision: int, deleted_at: datetime
    ) -> MCPDurableResultLifecycle | None: ...

    async def release_mcp_durable_result_deletion(
        self, result_ref: str, expected_revision: int, retry_at: datetime
    ) -> MCPDurableResultLifecycle | None: ...

    async def finalize_mcp_dispatch(
        self,
        intent_id: str,
        outbox_id: str,
        node_id: str,
        outcome: str,
        safe_error_code: str | None,
        expected_outbox_revision: int,
        claim_owner: str | None,
        claim_token: str | None,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult: ...

    async def converge_mcp_unknown_no_replay(
        self,
        task_id: str,
        occurred_at: datetime,
    ) -> MCPNoServerConvergenceResult: ...

    async def cancel_mcp_dispatch(
        self,
        intent_id: str,
        outbox_id: str,
        node_id: str,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult: ...

    async def converge_inactive_mcp_dispatch(
        self,
        intent_id: str,
        outbox_id: str,
        node_id: str,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult: ...

    async def admit_mcp_tool_call(
        self,
        intent_id: str,
        outbox_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        record: MCPCallRecord,
        occurred_at: datetime,
        *,
        cp7_candidate_id: str | None = None,
        cp7_epoch_id: str | None = None,
    ) -> bool: ...

    async def finalize_mcp_dispatch_no_call(
        self,
        intent_id: str,
        outbox_id: str,
        node_id: str,
        outcome: str,
        safe_error_code: str | None,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult: ...

    async def converge_user_mcp_no_server(
        self,
        task_id: str,
        occurred_at: datetime,
    ) -> MCPNoServerConvergenceResult: ...

    async def get_mcp_no_server_convergence_receipt(
        self, task_id: str
    ) -> MCPNoServerConvergenceReceipt | None: ...

    async def commit_authoritative_mcp_terminal_result(
        self,
        call_id: str,
        candidate_id: str,
        occurred_at: datetime,
    ) -> MCPTerminalResultCommitResult: ...

    async def finalize_mcp_dispatch_intent(
        self,
        intent_id: str,
        node_id: str,
        result_receipt_id: str,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult: ...

    async def get_mcp_terminal_result_receipt(
        self, result_receipt_id: str
    ) -> MCPTerminalResultReceipt | None: ...

    async def get_mcp_terminal_result_receipt_for_call(
        self, call_id: str
    ) -> MCPTerminalResultReceipt | None: ...

    async def get_mcp_execution_terminal_projection(
        self, call_id: str
    ) -> MCPExecutionTerminalProjection | None: ...

    async def converge_legacy_runtime_retirement(
        self,
        task_id: str,
        inventory_id: str,
        inventory_sha256: str,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> MCPLegacyRetirementConvergenceResult: ...

    async def append_mcp_legacy_retirement_evidence(
        self, evidence: MCPLegacyRetirementEvidence
    ) -> MCPLegacyRetirementEvidence: ...

    async def list_mcp_legacy_retirement_task_ids(
        self,
        inventory_id: str,
        inventory_sha256: str,
        *,
        limit: int = 10_000,
    ) -> list[str]: ...

    async def append_mcp_cp7_safety_ledger_record(
        self, record: MCPCP7SafetyLedgerRecord
    ) -> MCPCP7SafetyLedgerRecord: ...

    async def append_mcp_cp7_ready_epoch_event(
        self, event: MCPCP7ReadyEpochEvent
    ) -> MCPCP7ReadyEpochEvent: ...

    async def get_mcp_cp7_ready_epoch_event(
        self,
        candidate_id: str,
        epoch_id: str,
        event_kind: MCPCP7ReadyEpochEventKind,
    ) -> MCPCP7ReadyEpochEvent | None: ...

    async def get_mcp_cp7_candidate_guard(
        self, candidate_id: str
    ) -> MCPCP7CandidateGuard | None: ...

    async def produce_mcp_cp7_safety_snapshot(
        self, candidate_id: str
    ) -> MCPCP7SafetySnapshot: ...

    async def save_mcp_remote_task_binding(
        self, binding: MCPRemoteTaskBinding
    ) -> MCPRemoteTaskBinding: ...

    async def get_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> MCPRemoteTaskBinding | None: ...

    async def get_mcp_remote_task_binding_for_call(
        self, owner_user_id: str, task_id: str, call_ref: str
    ) -> MCPRemoteTaskBinding | None: ...

    async def publish_mcp_remote_task_binding(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        published_at: datetime,
        continuation_plan: Mapping[str, Any] | None = None,
    ) -> MCPRemoteTaskBinding | None: ...

    async def publish_mcp_remote_task(
        self,
        intent_id: str,
        outbox_id: str,
        call_id: str,
        safe_remote_task_ref: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        occurred_at: datetime,
    ) -> MCPRemoteTaskBinding | None: ...

    async def reconcile_unpublished_mcp_remote_task_bindings(
        self, *, now: datetime, limit: int = 1000
    ) -> int: ...

    async def list_due_mcp_remote_task_bindings(
        self, *, now: datetime, limit: int = 100
    ) -> list[MCPRemoteTaskBinding]: ...

    async def claim_due_mcp_remote_task_bindings(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskBinding]: ...

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
    ) -> MCPRemoteTaskBinding | None: ...

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
    ) -> MCPRemoteTaskBinding | None: ...

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
    ) -> MCPRemoteTaskBinding | None: ...

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
        result_receipt_id: str | None = None,
    ) -> MCPRemoteTaskBinding | None: ...

    async def finish_mcp_remote_task_binding_from_receipt(
        self,
        call_id: str,
        result_receipt_id: str,
        occurred_at: datetime,
    ) -> MCPRemoteTaskBinding | None: ...

    async def claim_mcp_remote_task_outbox(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]: ...

    async def claim_abandoned_mcp_remote_task_controls(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]: ...

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
    ) -> MCPRemoteTaskBinding | None: ...

    async def enqueue_mcp_remote_task_control(
        self,
        answer: InterruptAnswer,
        *,
        action: str,
        input_responses: Mapping[str, Any],
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def apply_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def get_mcp_remote_task_outbox(
        self, outbox_id: str
    ) -> MCPRemoteTaskOutbox | None: ...

    async def admit_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        admitted_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def mark_mcp_remote_task_continuation_dispatched(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        dispatched_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def claim_mcp_remote_task_continuations(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]: ...

    async def begin_mcp_remote_task_continuation(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        started_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def abandon_expired_mcp_remote_task_continuations(
        self, *, now: datetime, limit: int = 100
    ) -> list[MCPRemoteTaskOutbox]: ...

    async def complete_abandoned_mcp_remote_task_continuation(
        self, outbox_id: str, *, expected_revision: int, completed_at: datetime
    ) -> MCPRemoteTaskOutbox | None: ...

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
    ) -> MCPRemoteTaskOutbox | None: ...

    async def begin_mcp_remote_task_control_delivery(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def complete_mcp_remote_task_outbox(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def complete_mcp_remote_task_control(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        outcome: str,
        completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def delete_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> bool: ...

    async def converge_dispatched_mcp_calls_to_unknown(
        self, *, now: datetime, limit: int = 1000
    ) -> list[MCPCallRecord]: ...

    async def count_active_mcp_remote_task_bindings(
        self, *, rollout_config_version: str, protocol_version: str
    ) -> int: ...

    async def save_mcp_sealed_state(self, state: MCPSealedState) -> MCPSealedState: ...

    async def get_mcp_sealed_state(
        self, owner_user_id: str, task_id: str, sealed_state_ref: str
    ) -> MCPSealedState | None: ...

    async def delete_mcp_sealed_state(
        self, owner_user_id: str, task_id: str, sealed_state_ref: str
    ) -> bool: ...

    async def save_mcp_connection_lease(
        self, lease: MCPConnectionLease
    ) -> MCPConnectionLease: ...

    async def list_live_mcp_connection_leases(
        self, owner_user_id: str, task_id: str, *, now: datetime
    ) -> list[MCPConnectionLease]: ...

    async def delete_mcp_connection_lease(
        self, owner_user_id: str, task_id: str, connection_id: str
    ) -> bool: ...

    async def expire_mcp_connection_leases(self, *, now: datetime, limit: int = 1000) -> int: ...

    async def append_mcp_audit_event(self, event: MCPAuditEvent) -> MCPAuditEvent: ...

    async def list_mcp_audit_events(
        self, owner_user_id: str, *, task_id: str | None = None, limit: int = 100
    ) -> list[MCPAuditEvent]: ...

    async def delete_expired_mcp_audit_events(
        self, *, now: datetime, limit: int = 1000
    ) -> int: ...

    async def ensure_mcp_rollout_gate_scope(
        self, scope: MCPRolloutGateScope
    ) -> MCPRolloutGateScope: ...

    async def append_mcp_rollout_drill_observation(
        self, observation: MCPRolloutDrillObservation
    ) -> MCPRolloutDrillObservation: ...

    async def list_mcp_rollout_drill_observations(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPRolloutDrillObservation]: ...

    async def upsert_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket: ...

    async def set_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket: ...

    async def list_mcp_rollout_metric_buckets(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPRolloutMetricBucket]: ...

    async def save_mcp_shadow_audit_sample(
        self, sample: MCPShadowAuditSample
    ) -> MCPShadowAuditSample: ...

    async def list_mcp_shadow_audit_samples(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPShadowAuditSample]: ...

    async def delete_expired_mcp_shadow_audit_samples(
        self, *, now: datetime, limit: int = 1000
    ) -> int: ...

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
    ) -> MCPRolloutEvidenceSnapshot: ...

    async def append_mcp_rollout_evidence_snapshot(
        self, snapshot: MCPRolloutEvidenceSnapshot
    ) -> MCPRolloutEvidenceSnapshot: ...

    async def get_mcp_rollout_evidence_snapshot(
        self, evidence_id: str
    ) -> MCPRolloutEvidenceSnapshot | None: ...

    async def list_mcp_rollout_evidence_snapshots(
        self, environment_id: str, deployment_id: str, stage: str
    ) -> list[MCPRolloutEvidenceSnapshot]: ...

    async def append_mcp_rollout_stage_approval(
        self, approval: MCPRolloutStageApproval
    ) -> MCPRolloutStageApproval: ...

    async def activate_mcp_rollout_deployment(
        self, activation: MCPRolloutDeploymentActivation
    ) -> MCPRolloutDeploymentActivation: ...

    async def get_mcp_rollout_deployment_activation(
        self,
        environment_id: str,
        deployment_id: str,
        stage: str,
        config_fingerprint: str,
    ) -> MCPRolloutDeploymentActivation | None: ...

    async def append_mcp_rollout_promotion_block(
        self, block: MCPRolloutPromotionBlock
    ) -> MCPRolloutPromotionBlock: ...

    async def list_active_mcp_rollout_promotion_blocks(
        self, environment_id: str, *, rollout_program: str = "user_mcp_phase3"
    ) -> list[MCPRolloutPromotionBlock]: ...

    async def append_mcp_rollout_block_resolution(
        self, resolution: MCPRolloutBlockResolution
    ) -> MCPRolloutBlockResolution: ...

    async def save_mcp_rollout_instance_config_lease(
        self, lease: MCPRolloutInstanceConfigLease
    ) -> MCPRolloutInstanceConfigLease: ...

    async def list_mcp_rollout_instance_config_leases(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        now: datetime | None = None,
    ) -> list[MCPRolloutInstanceConfigLease]: ...

    async def create_or_get_maf_master_key_validation(
        self, record: MAFMasterKeyValidation
    ) -> MAFMasterKeyValidation: ...

    async def get_maf_master_key_validation(self) -> MAFMasterKeyValidation | None: ...

    async def save_auth_user_token(self, token: AuthUserToken, *, auth_generation_reason: str | None = None) -> AuthUserToken: ...

    async def get_auth_user_token(self, username: str) -> AuthUserToken | None: ...

    async def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None: ...

    async def get_auth_user_generation(self, username: str) -> AuthUserToken | None: ...

    async def list_auth_user_generations(self) -> list[AuthUserToken]: ...

    async def touch_auth_user_token_last_used(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None: ...

    async def clear_auth_user_token(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None: ...

    async def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None: ...

    async def save_conversation(self, conversation: Conversation) -> Conversation: ...

    async def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    async def list_conversations_for_username(self, username: str) -> list[Conversation]: ...

    async def list_deleting_conversations(self) -> list[Conversation]: ...

    async def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None: ...

    async def update_conversation_delete_phase(
        self,
        conversation_id: str,
        *,
        phase: str,
        updated_at: datetime,
        runner_id: str | None = None,
    ) -> Conversation | None: ...

    async def mark_conversation_delete_failed(
        self,
        conversation_id: str,
        *,
        failed_at: datetime,
        phase: str,
        error_code: str,
        error_summary: str,
        runner_id: str | None = None,
    ) -> Conversation | None: ...

    async def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None: ...

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]: ...

    async def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]: ...

    async def save_conversation_file_resource(self, resource: ConversationFileResource) -> ConversationFileResource: ...

    async def get_conversation_file_resource(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
    ) -> ConversationFileResource | None: ...

    async def get_conversation_file_resource_by_id(self, file_id: str) -> ConversationFileResource | None: ...

    async def list_conversation_file_resources(
        self,
        conversation_id: str,
        username: str | None = None,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[ConversationFileResource]: ...

    async def mark_conversation_file_resource_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None: ...

    async def save_conversation_file_resource_with_upload_message(
        self,
        resource: ConversationFileResource,
        projection: FileUploadMessageProjection,
        *,
        now: datetime,
    ) -> ConversationFileResource: ...

    async def mark_conversation_file_resource_and_upload_message_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None: ...

    async def compensate_failed_conversation_file_upload(
        self,
        conversation_id: str,
        username: str,
        upload_id: str,
        *,
        reason_code: str,
        now: datetime,
    ) -> Mapping[str, Any]: ...

    async def record_conversation_file_index_repair_required(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        affected_upload_ids: Iterable[str] = (),
        now: datetime,
    ) -> ConversationFileIndexRepairMarker: ...

    async def get_conversation_file_index_repair_marker(
        self,
        conversation_id: str,
    ) -> ConversationFileIndexRepairMarker | None: ...

    async def list_due_conversation_file_index_repairs(
        self,
        *,
        now: datetime,
        limit: int | None = None,
    ) -> list[ConversationFileIndexRepairMarker]: ...

    async def mark_conversation_file_index_repairing(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None: ...

    async def mark_conversation_file_index_repair_resolved(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None: ...

    async def mark_conversation_file_index_repair_failed(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        now: datetime,
        retryable: bool = True,
    ) -> ConversationFileIndexRepairMarker | None: ...

    async def save_conversation_memory_summary(self, summary: ConversationMemorySummary) -> ConversationMemorySummary: ...

    async def get_conversation_memory_summary(self, summary_id: str) -> ConversationMemorySummary | None: ...

    async def get_latest_conversation_memory_summary(
        self,
        conversation_id: str,
        username: str | None = None,
    ) -> ConversationMemorySummary | None: ...

    async def list_conversation_memory_summaries(self, conversation_id: str) -> list[ConversationMemorySummary]: ...

    async def delete_conversation_memory_summaries_for_conversation(self, conversation_id: str) -> int: ...

    async def save_pending_skill_context(self, context: PendingSkillContext) -> PendingSkillContext: ...

    async def get_pending_skill_context(self, context_id: str) -> PendingSkillContext | None: ...

    async def get_active_pending_skill_context(self, conversation_id: str) -> PendingSkillContext | None: ...

    async def mark_pending_skill_context_consumed(self, context_id: str) -> PendingSkillContext | None: ...

    async def mark_pending_skill_context_cancelled(self, context_id: str) -> PendingSkillContext | None: ...

    async def mark_pending_skill_context_superseded(self, conversation_id: str) -> int: ...

    async def save_message(self, message: Message) -> Message: ...

    async def get_message(self, message_id: str) -> Message | None: ...

    async def list_messages_for_conversation(self, conversation_id: str) -> list[Message]: ...

    async def upsert_file_upload_message(
        self,
        projection: FileUploadMessageProjection,
        *,
        now: datetime,
    ) -> Message: ...

    async def mark_file_upload_message_deleted(
        self,
        conversation_id: str,
        upload_id: str,
        *,
        deleted_at: datetime,
    ) -> Message | None: ...

    async def save_task(
        self, task: Task, *, expected_from_status: TaskStatus | None = None
    ) -> Task: ...

    async def compare_and_set_task(
        self, task: Task, *, expected_from_status: TaskStatus
    ) -> Task | None: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def claim_planner_replan(
        self,
        task_id: str,
        decision_digest: str,
        *,
        now: datetime,
    ) -> PlannerReplanClaim: ...

    async def get_planner_replan_claim(
        self,
        task_id: str,
        decision_digest: str,
    ) -> PlannerReplanClaim | None: ...

    async def mark_planner_replan_claim(
        self,
        task_id: str,
        decision_digest: str,
        *,
        status: str,
        now: datetime,
    ) -> PlannerReplanClaim: ...

    async def get_active_task_for_conversation(self, conversation_id: str) -> Task | None: ...

    async def list_tasks_for_conversation(self, conversation_id: str, statuses: Iterable[TaskStatus] | None = None) -> list[Task]: ...

    async def save_task_node(
        self, node: TaskNode, *, expected_from_status: NodeStatus | None = None
    ) -> TaskNode: ...

    async def compare_and_set_task_node(
        self, node: TaskNode, *, expected_from_status: NodeStatus
    ) -> TaskNode | None: ...

    async def get_task_node(self, node_id: str) -> TaskNode | None: ...

    async def list_task_nodes_for_task(self, task_id: str) -> list[TaskNode]: ...

    async def save_task_edge(self, task_id: str, edge: TaskEdge) -> TaskEdge: ...

    async def list_task_edges(self, task_id: str) -> list[TaskEdge]: ...

    async def save_artifact(self, artifact: Artifact) -> Artifact: ...

    async def get_artifact(self, artifact_id: str) -> Artifact | None: ...

    async def list_artifacts_for_task(self, task_id: str) -> list[Artifact]: ...

    async def list_artifacts_for_conversation(self, conversation_id: str) -> list[Artifact]: ...

    async def save_task_input_attachment(self, attachment: TaskInputAttachment) -> TaskInputAttachment: ...

    async def list_task_input_attachments_for_task(self, task_id: str) -> list[TaskInputAttachment]: ...

    async def list_task_input_attachments_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
    ) -> list[TaskInputAttachment]: ...

    async def append_event(self, event: EventRecord) -> EventRecord: ...

    async def list_events_for_task(self, task_id: str) -> list[EventRecord]: ...

    async def list_events_for_task_filtered(
        self,
        task_id: str,
        *,
        event_types: Iterable[str] | None = None,
        node_id: str | None = None,
        visibility: EventVisibility | str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]: ...

    async def list_event_page_for_task(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]: ...

    async def save_mailbox_message(self, message: MailboxMessage) -> MailboxMessage: ...

    async def get_mailbox_message(self, message_id: str) -> MailboxMessage | None: ...

    async def save_mailbox_delivery(self, delivery: MailboxDelivery) -> MailboxDelivery: ...

    async def get_mailbox_delivery(self, delivery_id: str) -> MailboxDelivery | None: ...

    async def list_mailbox_messages_for_task(self, task_id: str) -> list[MailboxMessage]: ...

    async def list_mailbox_deliveries_for_message(self, message_id: str) -> list[MailboxDelivery]: ...

    async def save_interrupt(self, interrupt: Interrupt) -> Interrupt: ...

    async def get_interrupt(self, interrupt_id: str) -> Interrupt | None: ...

    async def get_interrupt_for_node(self, task_id: str, node_id: str) -> Interrupt | None: ...

    async def list_interrupts_for_task(self, task_id: str) -> list[Interrupt]: ...

    async def save_interrupt_answer(self, interrupt_answer: InterruptAnswer) -> InterruptAnswer: ...

    async def get_interrupt_answer(self, interrupt_answer_id: str) -> InterruptAnswer | None: ...

    async def list_interrupt_answers(self, interrupt_id: str) -> list[InterruptAnswer]: ...

    async def save_slot_collection(self, collection: SlotCollection) -> SlotCollection: ...

    async def get_slot_collection(self, collection_id: str) -> SlotCollection | None: ...

    async def get_active_slot_collection_for_node(self, task_id: str, node_id: str) -> SlotCollection | None: ...

    async def list_slot_collections_for_task(self, task_id: str) -> list[SlotCollection]: ...

    async def apply_slot_transition(
        self,
        collection_id: str,
        expected_revision: int,
        next_collection: SlotCollection,
        slot_event: SlotEvent,
        *,
        idempotency_key: str | None = None,
    ) -> SlotCollection | None: ...

    async def append_slot_event(self, event: SlotEvent) -> SlotEvent: ...

    async def list_slot_events(self, collection_id: str) -> list[SlotEvent]: ...

    async def get_slot_event_by_idempotency_key(self, collection_id: str, key: str) -> SlotEvent | None: ...

    async def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint: ...

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None: ...

    async def get_checkpoint_by_resume_token(self, resume_token: str) -> Checkpoint | None: ...

    async def list_checkpoints_for_task(self, task_id: str) -> list[Checkpoint]: ...


@runtime_checkable
class CapabilityContract(Protocol):
    capability_id: str
    version: str
    description: str

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult: ...


@runtime_checkable
class ExecutorPort(Protocol):
    def supports(self, capability_id: str) -> bool: ...

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult: ...


@runtime_checkable
class EventSink(Protocol):
    async def publish(self, event: EventRecord) -> None: ...


@runtime_checkable
class AuditSink(Protocol):
    async def record(
        self,
        event_type: str,
        payload: Payload,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> None: ...
