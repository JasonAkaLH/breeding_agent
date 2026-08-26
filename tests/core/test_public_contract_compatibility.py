from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src.core import StoragePort as CoreExportedStoragePort
from src.core.contracts import StoragePort as CoreContractsStoragePort
from src.storage import StoragePort as StorageExportedStoragePort
from src.storage.interfaces import StoragePort as StorageInterfacesStoragePort


EXPECTED_STORAGE_METHOD_SIGNATURES = {
  "acknowledge_submission_handoff": "(self, request: 'SubmissionHandoffAcknowledgementRequest') -> 'SubmissionAdmissionPhase'",
  "acknowledge_submission_projection": "(self, request: 'SubmissionProjectionAcknowledgementRequest') -> 'SubmissionAdmissionPhase'",
  "abandon_expired_mcp_remote_task_continuations": "(self, *, now: 'datetime', limit: 'int' = 100) -> 'list[MCPRemoteTaskOutbox]'",
  "abort_mcp_dispatch_resume_outbox": "(self, outbox_id: 'str', expected_revision: 'int', occurred_at: 'datetime') -> 'MCPDispatchResumeOutbox | None'",
  "accept_mcp_mrtr_answer": "(self, interrupt_id: 'str', answer: 'InterruptAnswer', occurred_at: 'datetime') -> 'MCPMRTRAnswerResult'",
  "accept_mcp_tool_approval": "(self, interrupt_id: 'str', answer: 'InterruptAnswer', decision: 'str', occurred_at: 'datetime') -> 'MCPApprovalDecisionResult'",
  "acquire_user_mcp_scope_lease": "(self, lease: 'UserMCPScopeLease') -> 'bool'",
  "activate_mcp_rollout_deployment": "(self, activation: 'MCPRolloutDeploymentActivation') -> 'MCPRolloutDeploymentActivation'",
  "admit_approved_mcp_action": "(self, intent_id: 'str', outbox_id: 'str', action_id: 'str', expected_intent_revision: 'int', expected_outbox_revision: 'int', expected_action_revision: 'int', claim_owner: 'str', claim_token: 'str', payload_snapshot: 'MCPPendingActionPayloadSnapshot', record: 'MCPCallRecord', occurred_at: 'datetime', *, action_candidate: 'MCPPendingToolAction | None' = None, cp7_candidate_id: 'str | None' = None, cp7_epoch_id: 'str | None' = None) -> 'bool'",
  "admit_mcp_remote_task_continuation": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', admitted_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "admit_mcp_tool_call": "(self, intent_id: 'str', outbox_id: 'str', expected_intent_revision: 'int', expected_outbox_revision: 'int', record: 'MCPCallRecord', occurred_at: 'datetime', *, cp7_candidate_id: 'str | None' = None, cp7_epoch_id: 'str | None' = None) -> 'bool'",
  "admit_mrtr_continuation": "(self, intent_id: 'str', outbox_id: 'str', original_call_id: 'str', sealed_state_ref: 'str', answer_id: 'str', expected_intent_revision: 'int', expected_outbox_revision: 'int', claim_owner: 'str', claim_token: 'str', payload_snapshot: 'MCPPendingActionPayloadSnapshot', record: 'MCPCallRecord', occurred_at: 'datetime', *, cp7_candidate_id: 'str | None' = None, cp7_epoch_id: 'str | None' = None) -> 'bool'",
  "admit_submission": "(self, request: 'SubmissionAdmissionRequest') -> 'SubmissionAdmissionResult'",
  "append_event": "(self, event: 'EventRecord') -> 'EventRecord'",
  "append_mcp_audit_event": "(self, event: 'MCPAuditEvent') -> 'MCPAuditEvent'",
  "append_mcp_cp7_ready_epoch_event": "(self, event: 'MCPCP7ReadyEpochEvent') -> 'MCPCP7ReadyEpochEvent'",
  "append_mcp_cp7_safety_ledger_record": "(self, record: 'MCPCP7SafetyLedgerRecord') -> 'MCPCP7SafetyLedgerRecord'",
  "append_mcp_legacy_retirement_evidence": "(self, evidence: 'MCPLegacyRetirementEvidence') -> 'MCPLegacyRetirementEvidence'",
  "append_mcp_rollout_block_resolution": "(self, resolution: 'MCPRolloutBlockResolution') -> 'MCPRolloutBlockResolution'",
  "append_mcp_rollout_drill_observation": "(self, observation: 'MCPRolloutDrillObservation') -> 'MCPRolloutDrillObservation'",
  "append_mcp_rollout_evidence_snapshot": "(self, snapshot: 'MCPRolloutEvidenceSnapshot') -> 'MCPRolloutEvidenceSnapshot'",
  "append_mcp_rollout_promotion_block": "(self, block: 'MCPRolloutPromotionBlock') -> 'MCPRolloutPromotionBlock'",
  "append_mcp_rollout_stage_approval": "(self, approval: 'MCPRolloutStageApproval') -> 'MCPRolloutStageApproval'",
  "append_slot_event": "(self, event: 'SlotEvent') -> 'SlotEvent'",
  "apply_legacy_mcp_migration_atomic": "(self, candidates: 'Sequence[tuple[UserMCPServer, UserMCPCredentialRecord | None, MCPLegacyMigrationRecord]]') -> 'MCPLegacyMigrationBatchResult'",
  "apply_mcp_remote_task_continuation": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', updated_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "apply_slot_transition": "(self, collection_id: 'str', expected_revision: 'int', next_collection: 'SlotCollection', slot_event: 'SlotEvent', *, idempotency_key: 'str | None' = None) -> 'SlotCollection | None'",
  "arm_user_mcp_target_intent": "(self, task_id: 'str', node_id: 'str', requested_server_id: 'str', resume_envelope: 'Mapping[str, Any]', occurred_at: 'datetime') -> 'MCPTargetIntentArmResult'",
  "answer_interrupt_atomic": "(self, answer: 'InterruptAnswer', *, now: 'datetime') -> 'tuple[Interrupt, TaskNode, bool]'",
  "begin_mcp_remote_task_continuation": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', started_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "begin_mcp_remote_task_control_delivery": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', lease_expires_at: 'datetime', updated_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "cancel_mcp_dispatch": "(self, intent_id: 'str', outbox_id: 'str', node_id: 'str', occurred_at: 'datetime') -> 'MCPDispatchFinalizeResult'",
  "claim_abandoned_mcp_remote_task_controls": "(self, *, claim_owner: 'str', claim_token: 'str', now: 'datetime', limit: 'int' = 100) -> 'list[MCPRemoteTaskOutbox]'",
  "claim_due_mcp_remote_task_bindings": "(self, *, claim_owner: 'str', claim_token: 'str', now: 'datetime', lease_expires_at: 'datetime', limit: 'int' = 100) -> 'list[MCPRemoteTaskBinding]'",
  "claim_mcp_dispatch": "(self, outbox_id: 'str', claim_owner: 'str', claim_token: 'str', expected_revision: 'int', now: 'datetime', lease_expires_at: 'datetime') -> 'MCPDispatchResumeOutbox | None'",
  "claim_mcp_dispatch_result_deletion": "(self, result_ref: 'str', expected_revision: 'int', now: 'datetime') -> 'MCPDurableResultLifecycle | None'",
  "claim_mcp_dispatch_resume_outbox": "(self, outbox_id: 'str', claim_owner: 'str', claim_token: 'str', now: 'datetime', lease_expires_at: 'datetime') -> 'MCPDispatchResumeOutbox | None'",
  "claim_mcp_durable_result_deletions": "(self, now: 'datetime', *, limit: 'int' = 1000) -> 'list[MCPDurableResultLifecycle]'",
  "claim_mcp_remote_task_continuations": "(self, *, claim_owner: 'str', claim_token: 'str', now: 'datetime', lease_expires_at: 'datetime', limit: 'int' = 100) -> 'list[MCPRemoteTaskOutbox]'",
  "claim_mcp_remote_task_outbox": "(self, *, claim_owner: 'str', claim_token: 'str', now: 'datetime', lease_expires_at: 'datetime', limit: 'int' = 100) -> 'list[MCPRemoteTaskOutbox]'",
  "claim_mcp_terminal_candidate_archives": "(self, now: 'datetime', *, limit: 'int' = 1000) -> 'list[MCPTerminalCandidateLifecycle]'",
  "claim_mcp_terminal_candidate_deletions": "(self, now: 'datetime', *, limit: 'int' = 1000) -> 'list[MCPTerminalCandidateLifecycle]'",
  "claim_pending_submission": "(self, request: 'SubmissionClaimRequest') -> 'SubmissionClaimResult'",
  "claim_user_mcp_health_attempt": "(self, attempt: 'UserMCPHealthAttempt') -> 'bool'",
  "clear_auth_user_token": "(self, username: 'str', *, api_token_hash: 'str', at: 'datetime', auth_generation_reason: 'str | None' = None) -> 'AuthUserToken | None'",
  "clear_user_mcp_tool_grants": "(self, owner_user_id: 'str', server_id: 'str') -> 'int'",
  "close_conversation_admission": "(self, request: 'ConversationAdmissionCloseRequest') -> 'ConversationAdmissionCloseResult'",
  "close_submission_preparation_receipt": "(self, *, username: 'str', conversation_id: 'str', task_id: 'str', closed_at: 'datetime') -> 'SubmissionPreparationReceipt'",
  "commit_authoritative_mcp_terminal_result": "(self, call_id: 'str', candidate_id: 'str', occurred_at: 'datetime') -> 'MCPTerminalResultCommitResult'",
  "commit_mcp_call_terminal": "(self, call_id: 'str', candidate_id: 'str', outbox_id: 'str', expected_outbox_revision: 'int', claim_owner: 'str | None', claim_token: 'str | None', candidate_snapshot: 'MCPTerminalCandidateSnapshot', result_snapshot: 'MCPDurableResultSnapshot | None', occurred_at: 'datetime', *, remote_binding_ref: 'str | None' = None, remote_claim_owner: 'str | None' = None, remote_claim_token: 'str | None' = None, remote_expected_revision: 'int | None' = None) -> 'MCPTerminalResultCommitResult'",
  "compare_and_set_artifact_storage_ref": "(self, artifact_id: 'str', expected_storage_ref: 'str', replacement_storage_ref: 'str') -> 'bool'",
  "compare_and_set_task": "(self, task: 'Task', *, expected_from_status: 'TaskStatus') -> 'Task | None'",
  "compare_and_set_task_node": "(self, node: 'TaskNode', *, expected_from_status: 'NodeStatus') -> 'TaskNode | None'",
  "compensate_failed_conversation_file_upload": "(self, conversation_id: 'str', username: 'str', upload_id: 'str', *, reason_code: 'str', now: 'datetime') -> 'Mapping[str, Any]'",
  "complete_abandoned_mcp_remote_task_continuation": "(self, outbox_id: 'str', *, expected_revision: 'int', completed_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "complete_mcp_remote_task_control": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', outcome: 'str', completed_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "complete_mcp_remote_task_outbox": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', completed_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "complete_user_mcp_health_attempt": "(self, attempt_id: 'str', owner_user_id: 'str', server_id: 'str', *, runner_instance_id: 'str', config_version: 'int', security_version: 'int', health_status: 'str', error_code: 'str | None', completed_at: 'datetime') -> 'UserMCPServer | None'",
  "consume_mcp_dispatch_selector_step": "(self, outbox_id: 'str', claim_owner: 'str', claim_token: 'str', expected_revision: 'int', occurred_at: 'datetime') -> 'MCPDispatchResumeOutbox | None'",
  "converge_dispatched_mcp_calls_to_unknown": "(self, *, now: 'datetime', limit: 'int' = 1000) -> 'list[MCPCallRecord]'",
  "converge_inactive_mcp_dispatch": "(self, intent_id: 'str', outbox_id: 'str', node_id: 'str', occurred_at: 'datetime') -> 'MCPDispatchFinalizeResult'",
  "converge_legacy_runtime_retirement": "(self, task_id: 'str', inventory_id: 'str', inventory_sha256: 'str', idempotency_key: 'str', occurred_at: 'datetime') -> 'MCPLegacyRetirementConvergenceResult'",
  "converge_mcp_unknown_no_replay": "(self, task_id: 'str', occurred_at: 'datetime') -> 'MCPNoServerConvergenceResult'",
  "converge_user_mcp_no_server": "(self, task_id: 'str', occurred_at: 'datetime') -> 'MCPNoServerConvergenceResult'",
  "count_active_mcp_remote_task_bindings": "(self, *, rollout_config_version: 'str', protocol_version: 'str') -> 'int'",
  "create_or_get_maf_master_key_validation": "(self, record: 'MAFMasterKeyValidation') -> 'MAFMasterKeyValidation'",
  "create_user_mcp_initial_intent": "(self, task: 'Task', occurred_at: 'datetime') -> 'MCPInitialIntentCreateResult'",
  "create_user_mcp_server": "(self, server: 'UserMCPServer', credential: 'UserMCPCredentialRecord | None' = None) -> 'UserMCPServer'",
  "create_user_mcp_servers_atomic": "(self, candidates: 'Sequence[tuple[UserMCPServer, UserMCPCredentialRecord | None]]') -> 'list[UserMCPServer]'",
  "delete_conversation": "(self, conversation_id: 'str') -> 'dict[str, int]'",
  "delete_conversation_memory_summaries_for_conversation": "(self, conversation_id: 'str') -> 'int'",
  "delete_conversation_physical": "(self, conversation_id: 'str') -> 'dict[str, int]'",
  "delete_expired_mcp_audit_events": "(self, *, now: 'datetime', limit: 'int' = 1000) -> 'int'",
  "delete_expired_mcp_shadow_audit_samples": "(self, *, now: 'datetime', limit: 'int' = 1000) -> 'int'",
  "delete_mcp_connection_lease": "(self, owner_user_id: 'str', task_id: 'str', connection_id: 'str') -> 'bool'",
  "delete_mcp_remote_task_binding": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str') -> 'bool'",
  "delete_mcp_sealed_state": "(self, owner_user_id: 'str', task_id: 'str', sealed_state_ref: 'str') -> 'bool'",
  "delete_user_mcp_tool_grant": "(self, owner_user_id: 'str', server_id: 'str', grant_id: 'str') -> 'bool'",
  "delete_user_mcp_tool_grant_by_id": "(self, owner_user_id: 'str', grant_id: 'str') -> 'bool'",
  "enqueue_mcp_remote_task_control": "(self, answer: 'InterruptAnswer', *, action: 'str', input_responses: 'Mapping[str, Any]', updated_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "ensure_mcp_rollout_gate_scope": "(self, scope: 'MCPRolloutGateScope') -> 'MCPRolloutGateScope'",
  "expire_mcp_connection_leases": "(self, *, now: 'datetime', limit: 'int' = 1000) -> 'int'",
  "expire_user_mcp_health_attempts": "(self, *, now: 'datetime', error_code: 'str' = 'test_interrupted') -> 'int'",
  "expire_user_mcp_scope_leases": "(self, *, now: 'datetime') -> 'int'",
  "finalize_mcp_dispatch": "(self, intent_id: 'str', outbox_id: 'str', node_id: 'str', outcome: 'str', safe_error_code: 'str | None', expected_outbox_revision: 'int', claim_owner: 'str | None', claim_token: 'str | None', occurred_at: 'datetime') -> 'MCPDispatchFinalizeResult'",
  "finalize_mcp_dispatch_intent": "(self, intent_id: 'str', node_id: 'str', result_receipt_id: 'str', occurred_at: 'datetime') -> 'MCPDispatchFinalizeResult'",
  "finalize_mcp_dispatch_no_call": "(self, intent_id: 'str', outbox_id: 'str', node_id: 'str', outcome: 'str', safe_error_code: 'str | None', occurred_at: 'datetime') -> 'MCPDispatchFinalizeResult'",
  "finalize_user_mcp_server_delete": "(self, owner_user_id: 'str', server_id: 'str', *, now: 'datetime') -> 'bool'",
  "finish_mcp_call": "(self, owner_user_id: 'str', task_id: 'str', call_ref: 'str', *, status: 'str', terminal_at: 'datetime', result_ref: 'str | None' = None, output_size_bytes: 'int | None' = None, safe_error_code: 'str | None' = None) -> 'MCPCallRecord | None'",
  "finish_mcp_durable_result_deletion": "(self, result_ref: 'str', expected_revision: 'int', deleted_at: 'datetime') -> 'MCPDurableResultLifecycle | None'",
  "finish_mcp_remote_task_binding": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', remote_status: 'str', call_status: 'str', terminal_at: 'datetime', result_ref: 'str | None' = None, safe_error_code: 'str | None' = None, result_receipt_id: 'str | None' = None) -> 'MCPRemoteTaskBinding | None'",
  "finish_mcp_remote_task_binding_from_receipt": "(self, call_id: 'str', result_receipt_id: 'str', occurred_at: 'datetime') -> 'MCPRemoteTaskBinding | None'",
  "finish_mcp_terminal_candidate_archive": "(self, candidate_id: 'str', expected_revision: 'int', archived_at: 'datetime') -> 'MCPTerminalCandidateLifecycle | None'",
  "finish_mcp_terminal_candidate_deletion": "(self, candidate_id: 'str', expected_revision: 'int', deleted_at: 'datetime') -> 'MCPTerminalCandidateLifecycle | None'",
  "get_active_pending_skill_context": "(self, conversation_id: 'str') -> 'PendingSkillContext | None'",
  "get_active_slot_collection_for_node": "(self, task_id: 'str', node_id: 'str') -> 'SlotCollection | None'",
  "get_active_task_for_conversation": "(self, conversation_id: 'str') -> 'Task | None'",
  "get_artifact": "(self, artifact_id: 'str') -> 'Artifact | None'",
  "get_auth_user_generation": "(self, username: 'str') -> 'AuthUserToken | None'",
  "get_auth_user_token": "(self, username: 'str') -> 'AuthUserToken | None'",
  "get_auth_user_token_by_hash": "(self, api_token_hash: 'str') -> 'AuthUserToken | None'",
  "get_checkpoint": "(self, checkpoint_id: 'str') -> 'Checkpoint | None'",
  "get_checkpoint_by_resume_token": "(self, resume_token: 'str') -> 'Checkpoint | None'",
  "get_conversation": "(self, conversation_id: 'str') -> 'Conversation | None'",
  "get_conversation_file_index_repair_marker": "(self, conversation_id: 'str') -> 'ConversationFileIndexRepairMarker | None'",
  "get_conversation_file_resource": "(self, conversation_id: 'str', username: 'str', file_id: 'str') -> 'ConversationFileResource | None'",
  "get_conversation_file_resource_by_id": "(self, file_id: 'str') -> 'ConversationFileResource | None'",
  "get_conversation_memory_summary": "(self, summary_id: 'str') -> 'ConversationMemorySummary | None'",
  "get_interrupt": "(self, interrupt_id: 'str') -> 'Interrupt | None'",
  "get_interrupt_answer": "(self, interrupt_answer_id: 'str') -> 'InterruptAnswer | None'",
  "get_interrupt_for_node": "(self, task_id: 'str', node_id: 'str') -> 'Interrupt | None'",
  "get_latest_approved_mcp_tool_action": "(self, owner_user_id: 'str', task_id: 'str', node_id: 'str') -> 'MCPPendingToolAction | None'",
  "get_latest_conversation_memory_summary": "(self, conversation_id: 'str', username: 'str | None' = None) -> 'ConversationMemorySummary | None'",
  "get_maf_master_key_validation": "(self) -> 'MAFMasterKeyValidation | None'",
  "get_mailbox_delivery": "(self, delivery_id: 'str') -> 'MailboxDelivery | None'",
  "get_mailbox_message": "(self, message_id: 'str') -> 'MailboxMessage | None'",
  "get_mcp_branch_record": "(self, owner_user_id: 'str', task_id: 'str', branch_id: 'str') -> 'MCPBranchRecord | None'",
  "get_mcp_call_record": "(self, owner_user_id: 'str', task_id: 'str', call_ref: 'str') -> 'MCPCallRecord | None'",
  "get_mcp_cp7_candidate_guard": "(self, candidate_id: 'str') -> 'MCPCP7CandidateGuard | None'",
  "get_mcp_cp7_ready_epoch_event": "(self, candidate_id: 'str', epoch_id: 'str', event_kind: 'MCPCP7ReadyEpochEventKind') -> 'MCPCP7ReadyEpochEvent | None'",
  "get_mcp_dispatch_resume_outbox": "(self, outbox_id: 'str') -> 'MCPDispatchResumeOutbox | None'",
  "get_mcp_durable_result_lifecycle": "(self, result_ref: 'str') -> 'MCPDurableResultLifecycle | None'",
  "get_mcp_execution_terminal_projection": "(self, call_id: 'str') -> 'MCPExecutionTerminalProjection | None'",
  "get_mcp_legacy_migration_record": "(self, migration_id: 'str') -> 'MCPLegacyMigrationRecord | None'",
  "get_mcp_no_server_convergence_receipt": "(self, task_id: 'str') -> 'MCPNoServerConvergenceReceipt | None'",
  "get_mcp_no_server_intent": "(self, intent_id: 'str') -> 'MCPNoServerIntent | None'",
  "get_mcp_pending_tool_action": "(self, action_id: 'str') -> 'MCPPendingToolAction | None'",
  "get_mcp_pending_tool_action_for_interrupt": "(self, interrupt_id: 'str') -> 'MCPPendingToolAction | None'",
  "get_mcp_remote_task_binding": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str') -> 'MCPRemoteTaskBinding | None'",
  "get_mcp_remote_task_binding_for_call": "(self, owner_user_id: 'str', task_id: 'str', call_ref: 'str') -> 'MCPRemoteTaskBinding | None'",
  "get_mcp_remote_task_outbox": "(self, outbox_id: 'str') -> 'MCPRemoteTaskOutbox | None'",
  "get_mcp_rollout_deployment_activation": "(self, environment_id: 'str', deployment_id: 'str', stage: 'str', config_fingerprint: 'str') -> 'MCPRolloutDeploymentActivation | None'",
  "get_mcp_rollout_evidence_snapshot": "(self, evidence_id: 'str') -> 'MCPRolloutEvidenceSnapshot | None'",
  "get_mcp_sealed_state": "(self, owner_user_id: 'str', task_id: 'str', sealed_state_ref: 'str') -> 'MCPSealedState | None'",
  "get_mcp_terminal_result_receipt": "(self, result_receipt_id: 'str') -> 'MCPTerminalResultReceipt | None'",
  "get_mcp_terminal_result_receipt_for_call": "(self, call_id: 'str') -> 'MCPTerminalResultReceipt | None'",
  "get_message": "(self, message_id: 'str') -> 'Message | None'",
  "get_pending_skill_context": "(self, context_id: 'str') -> 'PendingSkillContext | None'",
  "get_slot_collection": "(self, collection_id: 'str') -> 'SlotCollection | None'",
  "get_slot_event_by_idempotency_key": "(self, collection_id: 'str', key: 'str') -> 'SlotEvent | None'",
  "get_submission_preparation": "(self, request: 'SubmissionPreparationLookup') -> 'SubmissionPreparationRecord | None'",
  "get_submission_preparation_receipt": "(self, *, username: 'str', conversation_id: 'str', task_id: 'str') -> 'SubmissionPreparationReceipt | None'",
  "get_task": "(self, task_id: 'str') -> 'Task | None'",
  "get_task_node": "(self, node_id: 'str') -> 'TaskNode | None'",
  "get_user_mcp_credential": "(self, owner_user_id: 'str', server_id: 'str') -> 'UserMCPCredentialRecord | None'",
  "get_user_mcp_owner_mutation_guard": "(self, owner_user_id: 'str') -> 'UserMCPOwnerMutationGuard | None'",
  "get_user_mcp_server": "(self, owner_user_id: 'str', server_id: 'str') -> 'UserMCPServer | None'",
  "get_valid_user_mcp_tool_grant": "(self, owner_user_id: 'str', server_id: 'str', tool_name: 'str', *, server_security_version: 'int', input_schema_sha256: 'str') -> 'UserMCPToolGrant | None'",
  "invalidate_user_mcp_tool_grants": "(self, owner_user_id: 'str', server_id: 'str', *, invalidated_at: 'datetime', invalid_reason: 'str', tool_name: 'str | None' = None, input_schema_sha256: 'str | None' = None) -> 'int'",
  "list_active_mcp_rollout_promotion_blocks": "(self, environment_id: 'str', *, rollout_program: 'str' = 'user_mcp_phase3') -> 'list[MCPRolloutPromotionBlock]'",
  "list_artifacts_for_conversation": "(self, conversation_id: 'str') -> 'list[Artifact]'",
  "list_artifacts_for_task": "(self, task_id: 'str') -> 'list[Artifact]'",
  "list_auth_user_generations": "(self) -> 'list[AuthUserToken]'",
  "list_checkpoints_for_task": "(self, task_id: 'str') -> 'list[Checkpoint]'",
  "list_completed_mcp_calls_for_result_reprojection": "(self, *, after_call_ref: 'str | None' = None, limit: 'int' = 1000) -> 'list[MCPCallRecord]'",
  "list_conversation_file_resources": "(self, conversation_id: 'str', username: 'str | None' = None, *, include_deleted: 'bool' = False, limit: 'int | None' = None, cursor: 'str | None' = None) -> 'list[ConversationFileResource]'",
  "list_conversation_memory_summaries": "(self, conversation_id: 'str') -> 'list[ConversationMemorySummary]'",
  "list_conversations_for_username": "(self, username: 'str') -> 'list[Conversation]'",
  "list_deleting_conversations": "(self) -> 'list[Conversation]'",
  "list_due_conversation_file_index_repairs": "(self, *, now: 'datetime', limit: 'int | None' = None) -> 'list[ConversationFileIndexRepairMarker]'",
  "list_due_mcp_remote_task_bindings": "(self, *, now: 'datetime', limit: 'int' = 100) -> 'list[MCPRemoteTaskBinding]'",
  "list_event_page_for_task": "(self, task_id: 'str', *, after_event_id: 'str | None' = None, limit: 'int | None' = None) -> 'list[EventRecord]'",
  "list_events_for_task": "(self, task_id: 'str') -> 'list[EventRecord]'",
  "list_events_for_task_filtered": "(self, task_id: 'str', *, event_types: 'Iterable[str] | None' = None, node_id: 'str | None' = None, visibility: 'EventVisibility | str | None' = None, limit: 'int | None' = None) -> 'list[EventRecord]'",
  "list_incomplete_mcp_durable_result_lifecycles": "(self, *, limit: 'int' = 1000) -> 'list[MCPDurableResultLifecycle]'",
  "list_incomplete_mcp_terminal_candidate_lifecycles": "(self, *, limit: 'int' = 1000) -> 'list[MCPTerminalCandidateLifecycle]'",
  "list_interrupt_answers": "(self, interrupt_id: 'str') -> 'list[InterruptAnswer]'",
  "list_interrupts_for_task": "(self, task_id: 'str') -> 'list[Interrupt]'",
  "list_live_mcp_connection_leases": "(self, owner_user_id: 'str', task_id: 'str', *, now: 'datetime') -> 'list[MCPConnectionLease]'",
  "list_live_user_mcp_scope_leases": "(self, *, now: 'datetime', owner_user_id: 'str | None' = None, server_id: 'str | None' = None) -> 'list[UserMCPScopeLease]'",
  "list_mailbox_deliveries_for_message": "(self, message_id: 'str') -> 'list[MailboxDelivery]'",
  "list_mailbox_messages_for_task": "(self, task_id: 'str') -> 'list[MailboxMessage]'",
  "list_mcp_audit_events": "(self, owner_user_id: 'str', *, task_id: 'str | None' = None, limit: 'int' = 100) -> 'list[MCPAuditEvent]'",
  "list_mcp_branch_records": "(self, owner_user_id: 'str', *, task_id: 'str | None' = None, statuses: 'tuple[str, ...]' = ()) -> 'list[MCPBranchRecord]'",
  "list_mcp_call_records": "(self, owner_user_id: 'str', task_id: 'str', *, branch_id: 'str | None' = None) -> 'list[MCPCallRecord]'",
  "list_mcp_dispatch_resume_outboxes": "(self, *, statuses: 'tuple[str, ...]' = (), after_updated_at: 'datetime | None' = None, after_outbox_id: 'str | None' = None, limit: 'int' = 10000) -> 'list[MCPDispatchResumeOutbox]'",
  "list_mcp_legacy_retirement_task_ids": "(self, inventory_id: 'str', inventory_sha256: 'str', *, limit: 'int' = 10000) -> 'list[str]'",
  "list_mcp_no_server_intents": "(self, *, statuses: 'tuple[str, ...]' = (), after_updated_at: 'datetime | None' = None, after_intent_id: 'str | None' = None, limit: 'int' = 10000) -> 'list[MCPNoServerIntent]'",
  "list_mcp_rollout_drill_observations": "(self, environment_id: 'str', deployment_id: 'str', *, window_started_at: 'datetime', window_ended_at: 'datetime') -> 'list[MCPRolloutDrillObservation]'",
  "list_mcp_rollout_evidence_snapshots": "(self, environment_id: 'str', deployment_id: 'str', stage: 'str') -> 'list[MCPRolloutEvidenceSnapshot]'",
  "list_mcp_rollout_instance_config_leases": "(self, environment_id: 'str', deployment_id: 'str', *, now: 'datetime | None' = None) -> 'list[MCPRolloutInstanceConfigLease]'",
  "list_mcp_rollout_metric_buckets": "(self, environment_id: 'str', deployment_id: 'str', stage: 'str', *, window_started_at: 'datetime', window_ended_at: 'datetime') -> 'list[MCPRolloutMetricBucket]'",
  "list_mcp_shadow_audit_samples": "(self, environment_id: 'str', deployment_id: 'str', stage: 'str', *, window_started_at: 'datetime', window_ended_at: 'datetime') -> 'list[MCPShadowAuditSample]'",
  "list_messages_for_conversation": "(self, conversation_id: 'str') -> 'list[Message]'",
  "list_pending_user_mcp_server_deletions": "(self) -> 'list[UserMCPServer]'",
  "list_projectable_mcp_durable_result_lifecycles": "(self, *, after_updated_at: 'datetime | None' = None, after_result_ref: 'str | None' = None, limit: 'int' = 1000) -> 'list[MCPDurableResultLifecycle]'",
  "list_slot_collections_for_task": "(self, task_id: 'str') -> 'list[SlotCollection]'",
  "list_slot_events": "(self, collection_id: 'str') -> 'list[SlotEvent]'",
  "list_task_input_attachments_for_conversation": "(self, conversation_id: 'str', *, limit: 'int | None' = None) -> 'list[TaskInputAttachment]'",
  "list_task_input_attachments_for_task": "(self, task_id: 'str') -> 'list[TaskInputAttachment]'",
  "list_task_nodes_for_task": "(self, task_id: 'str') -> 'list[TaskNode]'",
  "list_tasks_for_conversation": "(self, conversation_id: 'str', statuses: 'Iterable[TaskStatus] | None' = None) -> 'list[Task]'",
  "list_unresolved_mcp_no_server_intents": "(self) -> 'list[MCPNoServerIntent]'",
  "list_user_mcp_servers": "(self, owner_user_id: 'str') -> 'list[UserMCPServer]'",
  "list_user_mcp_tool_grants": "(self, owner_user_id: 'str', server_id: 'str | None' = None) -> 'list[UserMCPToolGrant]'",
  "mark_conversation_delete_failed": "(self, conversation_id: 'str', *, failed_at: 'datetime', phase: 'str', error_code: 'str', error_summary: 'str', runner_id: 'str | None' = None) -> 'Conversation | None'",
  "mark_conversation_deleting": "(self, conversation_id: 'str', *, runner_id: 'str', requested_at: 'datetime', started_at: 'datetime | None' = None, phase: 'str' = 'marking') -> 'Conversation | None'",
  "mark_conversation_file_index_repair_failed": "(self, conversation_id: 'str', *, reason_code: 'str', now: 'datetime', retryable: 'bool' = True) -> 'ConversationFileIndexRepairMarker | None'",
  "mark_conversation_file_index_repair_resolved": "(self, conversation_id: 'str', *, now: 'datetime') -> 'ConversationFileIndexRepairMarker | None'",
  "mark_conversation_file_index_repairing": "(self, conversation_id: 'str', *, now: 'datetime') -> 'ConversationFileIndexRepairMarker | None'",
  "mark_conversation_file_resource_and_upload_message_deleted": "(self, conversation_id: 'str', username: 'str', file_id: 'str', *, updated_at: 'datetime') -> 'ConversationFileResource | None'",
  "mark_conversation_file_resource_deleted": "(self, conversation_id: 'str', username: 'str', file_id: 'str', *, updated_at: 'datetime') -> 'ConversationFileResource | None'",
  "mark_file_upload_message_deleted": "(self, conversation_id: 'str', upload_id: 'str', *, deleted_at: 'datetime') -> 'Message | None'",
  "mark_mcp_call_may_have_dispatched": "(self, owner_user_id: 'str', task_id: 'str', call_ref: 'str', *, updated_at: 'datetime') -> 'bool'",
  "mark_mcp_durable_result_artifact_owned": "(self, result_ref: 'str', expected_revision: 'int', artifact_id: 'str', expected_size_bytes: 'int', expected_content_sha256: 'str', occurred_at: 'datetime') -> 'MCPDurableResultLifecycle | None'",
  "mark_mcp_remote_task_continuation_dispatched": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', dispatched_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "mark_pending_skill_context_cancelled": "(self, context_id: 'str') -> 'PendingSkillContext | None'",
  "mark_pending_skill_context_consumed": "(self, context_id: 'str') -> 'PendingSkillContext | None'",
  "mark_pending_skill_context_superseded": "(self, conversation_id: 'str') -> 'int'",
  "mark_user_mcp_server_deleted": "(self, owner_user_id: 'str', server_id: 'str', *, deleted_at: 'datetime') -> 'UserMCPServer | None'",
  "pause_mcp_remote_task_for_input": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', input_requests: 'Mapping[str, Any]', conversation_id: 'str', source_message_id: 'str', updated_at: 'datetime') -> 'MCPRemoteTaskBinding | None'",
  "produce_mcp_cp7_safety_snapshot": "(self, candidate_id: 'str') -> 'MCPCP7SafetySnapshot'",
  "produce_mcp_shadow_evidence_snapshot": "(self, environment_id: 'str', deployment_id: 'str', *, window_started_at: 'datetime', window_ended_at: 'datetime', builder: 'Callable[[list[MCPShadowAuditSample], list[MCPRolloutMetricBucket]], MCPRolloutEvidenceSnapshot]') -> 'MCPRolloutEvidenceSnapshot'",
  "prepare_submission_handoff": "(self, request: 'SubmissionPreparationRequest') -> 'SubmissionPreparationRecord'",
  "publish_mcp_remote_task": "(self, intent_id: 'str', outbox_id: 'str', call_id: 'str', safe_remote_task_ref: 'str', expected_intent_revision: 'int', expected_outbox_revision: 'int', claim_owner: 'str', claim_token: 'str', occurred_at: 'datetime') -> 'MCPRemoteTaskBinding | None'",
  "publish_mcp_remote_task_binding": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str', *, published_at: 'datetime', continuation_plan: 'Mapping[str, Any] | None' = None) -> 'MCPRemoteTaskBinding | None'",
  "reclaim_mcp_dispatch_resume_outbox": "(self, outbox_id: 'str', expected_revision: 'int', now: 'datetime') -> 'MCPDispatchResumeOutbox | None'",
  "reconcile_mcp_durable_result_lifecycle": "(self, snapshot: 'MCPDurableResultSnapshot', occurred_at: 'datetime') -> 'MCPDurableResultLifecycle | None'",
  "reconcile_unpublished_mcp_remote_task_bindings": "(self, *, now: 'datetime', limit: 'int' = 1000) -> 'int'",
  "record_conversation_file_index_repair_required": "(self, conversation_id: 'str', *, reason_code: 'str', affected_upload_ids: 'Iterable[str]' = (), now: 'datetime') -> 'ConversationFileIndexRepairMarker'",
  "recover_mcp_terminal_candidate": "(self, candidate_snapshot: 'MCPTerminalCandidateSnapshot', result_snapshot: 'MCPDurableResultSnapshot | None', occurred_at: 'datetime') -> 'MCPTerminalResultCommitResult'",
  "release_mcp_durable_result_deletion": "(self, result_ref: 'str', expected_revision: 'int', retry_at: 'datetime') -> 'MCPDurableResultLifecycle | None'",
  "release_mcp_remote_task_binding_claim": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', updated_at: 'datetime') -> 'MCPRemoteTaskBinding | None'",
  "release_or_recover_mcp_dispatch_claim": "(self, outbox_id: 'str', expected_revision: 'int', now: 'datetime') -> 'MCPDispatchResumeOutbox | None'",
  "release_user_mcp_health_attempt": "(self, attempt_id: 'str', owner_user_id: 'str', server_id: 'str', *, runner_instance_id: 'str', config_version: 'int', security_version: 'int') -> 'bool'",
  "release_user_mcp_scope_lease": "(self, scope_id: 'str', *, gateway_instance_id: 'str') -> 'bool'",
  "renew_mcp_dispatch_claim": "(self, outbox_id: 'str', claim_owner: 'str', claim_token: 'str', expected_revision: 'int', now: 'datetime', lease_expires_at: 'datetime') -> 'MCPDispatchResumeOutbox | None'",
  "renew_mcp_remote_task_binding_claim": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', lease_expires_at: 'datetime', updated_at: 'datetime') -> 'MCPRemoteTaskBinding | None'",
  "renew_mcp_remote_task_continuation": "(self, outbox_id: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', lease_expires_at: 'datetime', node_ids: 'tuple[str, ...] | None' = None, updated_at: 'datetime') -> 'MCPRemoteTaskOutbox | None'",
  "renew_submission_claim": "(self, request: 'SubmissionClaimRenewalRequest') -> 'SubmissionAdmissionHandle'",
  "renew_user_mcp_health_attempt": "(self, attempt_id: 'str', owner_user_id: 'str', server_id: 'str', *, runner_instance_id: 'str', config_version: 'int', security_version: 'int', lease_expires_at: 'datetime', updated_at: 'datetime') -> 'bool'",
  "renew_user_mcp_scope_lease": "(self, scope_id: 'str', owner_user_id: 'str', server_id: 'str', *, gateway_instance_id: 'str', security_version: 'int', lease_expires_at: 'datetime', updated_at: 'datetime') -> 'bool'",
  "reserve_mcp_call": "(self, record: 'MCPCallRecord') -> 'bool'",
  "reserve_message_identity": "(self, request: 'MessageIdentityReservationRequest') -> 'MessageIdentityReservationResult'",
  "resolve_user_mcp_target_intent": "(self, intent_id: 'str', occurred_at: 'datetime') -> 'MCPTargetIntentResolveResult'",
  "retry_failed_conversation_delete": "(self, conversation_id: 'str', *, runner_id: 'str', requested_at: 'datetime', started_at: 'datetime | None' = None, phase: 'str' = 'marking') -> 'Conversation | None'",
  "rotate_auth_user_token": "(self, username: 'str', *, old_api_token_hash: 'str', new_api_token_hash: 'str', at: 'datetime', auth_generation_reason: 'str | None' = None) -> 'AuthUserToken | None'",
  "save_artifact": "(self, artifact: 'Artifact') -> 'Artifact'",
  "save_auth_user_token": "(self, token: 'AuthUserToken', *, auth_generation_reason: 'str | None' = None) -> 'AuthUserToken'",
  "save_checkpoint": "(self, checkpoint: 'Checkpoint') -> 'Checkpoint'",
  "save_conversation": "(self, conversation: 'Conversation') -> 'Conversation'",
  "save_conversation_file_resource": "(self, resource: 'ConversationFileResource') -> 'ConversationFileResource'",
  "save_conversation_file_resource_with_upload_message": "(self, resource: 'ConversationFileResource', projection: 'FileUploadMessageProjection', *, now: 'datetime') -> 'ConversationFileResource'",
  "save_conversation_memory_summary": "(self, summary: 'ConversationMemorySummary') -> 'ConversationMemorySummary'",
  "save_interrupt": "(self, interrupt: 'Interrupt') -> 'Interrupt'",
  "save_interrupt_answer": "(self, interrupt_answer: 'InterruptAnswer') -> 'InterruptAnswer'",
  "save_mailbox_delivery": "(self, delivery: 'MailboxDelivery') -> 'MailboxDelivery'",
  "save_mailbox_message": "(self, message: 'MailboxMessage') -> 'MailboxMessage'",
  "save_mcp_branch_record": "(self, record: 'MCPBranchRecord') -> 'MCPBranchRecord'",
  "save_mcp_connection_lease": "(self, lease: 'MCPConnectionLease') -> 'MCPConnectionLease'",
  "save_mcp_remote_task_binding": "(self, binding: 'MCPRemoteTaskBinding') -> 'MCPRemoteTaskBinding'",
  "save_mcp_rollout_instance_config_lease": "(self, lease: 'MCPRolloutInstanceConfigLease') -> 'MCPRolloutInstanceConfigLease'",
  "save_mcp_sealed_state": "(self, state: 'MCPSealedState') -> 'MCPSealedState'",
  "save_mcp_shadow_audit_sample": "(self, sample: 'MCPShadowAuditSample') -> 'MCPShadowAuditSample'",
  "save_message": "(self, message: 'Message', *, identity_reservation: 'MessageIdentityReservationRequest | None' = None) -> 'Message'",
  "save_pending_skill_context": "(self, context: 'PendingSkillContext') -> 'PendingSkillContext'",
  "save_slot_collection": "(self, collection: 'SlotCollection') -> 'SlotCollection'",
  "save_task": "(self, task: 'Task', *, expected_from_status: 'TaskStatus | None' = None) -> 'Task'",
  "save_task_input_attachment": "(self, attachment: 'TaskInputAttachment') -> 'TaskInputAttachment'",
  "save_task_node": "(self, node: 'TaskNode', *, expected_from_status: 'NodeStatus | None' = None) -> 'TaskNode'",
  "save_user_mcp_tool_grant": "(self, grant: 'UserMCPToolGrant') -> 'UserMCPToolGrant'",
  "set_mcp_rollout_metric_bucket": "(self, bucket: 'MCPRolloutMetricBucket') -> 'MCPRolloutMetricBucket'",
  "summarize_mcp_durable_result_backfill": "(self, now: 'datetime') -> 'Mapping[str, int]'",
  "suspend_mcp_for_approval": "(self, intent_id: 'str', outbox_id: 'str', expected_intent_revision: 'int', expected_outbox_revision: 'int', claim_owner: 'str', claim_token: 'str', action: 'MCPPendingToolAction', interrupt: 'Interrupt', payload_snapshot: 'MCPPendingActionPayloadSnapshot', occurred_at: 'datetime') -> 'MCPApprovalSuspendResult'",
  "suspend_mcp_for_input": "(self, intent_id: 'str', outbox_id: 'str', call_id: 'str', sealed_state_ref: 'str', expected_intent_revision: 'int', expected_outbox_revision: 'int', claim_owner: 'str', claim_token: 'str', interrupt: 'Interrupt', occurred_at: 'datetime') -> 'MCPInputSuspendResult'",
  "touch_auth_user_token_last_used": "(self, username: 'str', *, api_token_hash: 'str', at: 'datetime') -> 'AuthUserToken | None'",
  "update_conversation_delete_phase": "(self, conversation_id: 'str', *, phase: 'str', updated_at: 'datetime', runner_id: 'str | None' = None) -> 'Conversation | None'",
  "update_mcp_remote_task_binding_status": "(self, owner_user_id: 'str', task_id: 'str', safe_remote_task_ref: 'str', *, claim_owner: 'str', claim_token: 'str', expected_revision: 'int', last_status: 'str', next_poll_at: 'datetime | None', updated_at: 'datetime', terminal_at: 'datetime | None' = None) -> 'MCPRemoteTaskBinding | None'",
  "update_user_mcp_server": "(self, owner_user_id: 'str', server_id: 'str', *, changes: 'Mapping[str, Any]', credential_operation: 'str' = 'retain', credential: 'UserMCPCredentialRecord | None' = None, security_sensitive: 'bool' = False, expected_config_version: 'int | None' = None, expected_security_version: 'int | None' = None, updated_at: 'datetime') -> 'UserMCPServer | None'",
  "upsert_file_upload_message": "(self, projection: 'FileUploadMessageProjection', *, now: 'datetime') -> 'Message'",
  "upsert_mcp_rollout_metric_bucket": "(self, bucket: 'MCPRolloutMetricBucket') -> 'MCPRolloutMetricBucket'",
  "write_submission_preparation_component": "(self, *, username: 'str', conversation_id: 'str', task_id: 'str', component: 'SubmissionPreparationReceiptComponent', canonical_json: 'bytes', component_sha256: 'str', written_at: 'datetime') -> 'SubmissionPreparationReceipt'"
}

FORBIDDEN_CORE_IMPORT_PREFIXES = (
    "src.api",
    "src.capabilities",
    "src.integrations",
    "src.lifecycle",
    "src.orchestration",
    "src.state",
    "src.storage",
    "sqlalchemy",
)

EXPECTED_BOUNDED_CORE_IMPORTS = {
    (
        "models.py",
        "canonical_mcp_rollout_drill_observation_digest",
        "src.integrations.mcp.rollout_evidence",
        ("canonical_evidence_content_digest",),
    )
}


class PublicContractCompatibilityTest(unittest.TestCase):
    def test_storage_port_four_public_paths_share_one_canonical_object(self) -> None:
        self.assertIs(CoreContractsStoragePort, CoreExportedStoragePort)
        self.assertIs(CoreContractsStoragePort, StorageInterfacesStoragePort)
        self.assertIs(CoreContractsStoragePort, StorageExportedStoragePort)

    def test_storage_port_has_exact_async_method_signatures(self) -> None:
        actual = {
            name: str(inspect.signature(value))
            for name, value in inspect.getmembers(
                CoreContractsStoragePort,
                predicate=inspect.iscoroutinefunction,
            )
        }

        self.assertEqual(len(actual), 272)
        self.assertEqual(set(actual), set(EXPECTED_STORAGE_METHOD_SIGNATURES))
        self.assertEqual(actual, EXPECTED_STORAGE_METHOD_SIGNATURES)
        self.assertTrue(
            all(
                inspect.iscoroutinefunction(getattr(CoreContractsStoragePort, name))
                for name in EXPECTED_STORAGE_METHOD_SIGNATURES
            )
        )

    def test_core_has_only_the_documented_bounded_reverse_import(self) -> None:
        root = Path(__file__).resolve().parents[2]
        violations: list[str] = []
        bounded_imports: set[tuple[str, str, str, tuple[str, ...]]] = set()
        for source_path in sorted((root / "src" / "core").glob("*.py")):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                imported: tuple[str, ...]
                if isinstance(node, ast.Import):
                    imported = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported = (node.module or "",)
                else:
                    continue
                for module_name in imported:
                    if module_name.startswith(FORBIDDEN_CORE_IMPORT_PREFIXES):
                        containing_functions = [
                            candidate.name
                            for candidate in ast.walk(tree)
                            if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and any(child is node for child in ast.walk(candidate))
                        ]
                        function_name = containing_functions[-1] if containing_functions else "<module>"
                        imported_names = tuple(
                            alias.name for alias in node.names
                        )
                        edge = (
                            source_path.name,
                            function_name,
                            module_name,
                            imported_names,
                        )
                        if edge in EXPECTED_BOUNDED_CORE_IMPORTS:
                            bounded_imports.add(edge)
                        else:
                            violations.append(
                                f"{source_path.name}:{function_name}:{module_name}:{imported_names}"
                            )

        self.assertEqual(violations, [])
        self.assertEqual(bounded_imports, EXPECTED_BOUNDED_CORE_IMPORTS)


if __name__ == "__main__":
    unittest.main()
