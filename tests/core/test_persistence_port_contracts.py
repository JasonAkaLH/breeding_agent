from __future__ import annotations

import inspect
import unittest

from src.core import contracts
from src.core import StoragePort as CoreStoragePort
from src.storage import StoragePort as StorageExportedPort
from src.storage.interfaces import StoragePort as StorageInterfacesPort
from tests.core.test_public_contract_compatibility import (
    EXPECTED_STORAGE_METHOD_SIGNATURES,
)


EXPECTED_METHODS_BY_PORT = {
    "UserMCPConfigurationStoragePort": (
        "list_user_mcp_servers",
        "get_user_mcp_server",
        "create_user_mcp_server",
        "create_user_mcp_servers_atomic",
        "apply_legacy_mcp_migration_atomic",
        "get_mcp_legacy_migration_record",
        "update_user_mcp_server",
        "get_user_mcp_credential",
        "claim_user_mcp_health_attempt",
        "renew_user_mcp_health_attempt",
        "complete_user_mcp_health_attempt",
        "expire_user_mcp_health_attempts",
        "release_user_mcp_health_attempt",
        "acquire_user_mcp_scope_lease",
        "renew_user_mcp_scope_lease",
        "release_user_mcp_scope_lease",
        "list_live_user_mcp_scope_leases",
        "expire_user_mcp_scope_leases",
        "mark_user_mcp_server_deleted",
        "list_pending_user_mcp_server_deletions",
        "finalize_user_mcp_server_delete",
        "save_user_mcp_tool_grant",
        "list_user_mcp_tool_grants",
        "get_valid_user_mcp_tool_grant",
        "delete_user_mcp_tool_grant",
        "delete_user_mcp_tool_grant_by_id",
        "clear_user_mcp_tool_grants",
        "invalidate_user_mcp_tool_grants",
    ),
    "MCPDispatchStoragePort": (
        "save_mcp_branch_record",
        "get_mcp_branch_record",
        "list_mcp_branch_records",
        "reserve_mcp_call",
        "mark_mcp_call_may_have_dispatched",
        "get_mcp_call_record",
        "list_mcp_call_records",
        "list_completed_mcp_calls_for_result_reprojection",
        "finish_mcp_call",
        "get_user_mcp_owner_mutation_guard",
        "get_mcp_no_server_intent",
        "list_unresolved_mcp_no_server_intents",
        "list_mcp_no_server_intents",
        "create_user_mcp_initial_intent",
        "arm_user_mcp_target_intent",
        "resolve_user_mcp_target_intent",
        "get_mcp_dispatch_resume_outbox",
        "get_mcp_pending_tool_action",
        "get_latest_approved_mcp_tool_action",
        "get_mcp_pending_tool_action_for_interrupt",
        "list_mcp_dispatch_resume_outboxes",
        "claim_mcp_dispatch_resume_outbox",
        "reclaim_mcp_dispatch_resume_outbox",
        "abort_mcp_dispatch_resume_outbox",
        "claim_mcp_dispatch",
        "renew_mcp_dispatch_claim",
        "consume_mcp_dispatch_selector_step",
        "release_or_recover_mcp_dispatch_claim",
        "suspend_mcp_for_approval",
        "accept_mcp_tool_approval",
        "suspend_mcp_for_input",
        "accept_mcp_mrtr_answer",
        "admit_approved_mcp_action",
        "admit_mrtr_continuation",
        "commit_mcp_call_terminal",
    ),
    "MCPResultLifecycleStoragePort": (
        "recover_mcp_terminal_candidate",
        "list_incomplete_mcp_terminal_candidate_lifecycles",
        "claim_mcp_terminal_candidate_archives",
        "finish_mcp_terminal_candidate_archive",
        "claim_mcp_terminal_candidate_deletions",
        "finish_mcp_terminal_candidate_deletion",
        "list_incomplete_mcp_durable_result_lifecycles",
        "get_mcp_durable_result_lifecycle",
        "list_projectable_mcp_durable_result_lifecycles",
        "summarize_mcp_durable_result_backfill",
        "reconcile_mcp_durable_result_lifecycle",
        "mark_mcp_durable_result_artifact_owned",
        "claim_mcp_durable_result_deletions",
        "claim_mcp_dispatch_result_deletion",
        "finish_mcp_durable_result_deletion",
        "release_mcp_durable_result_deletion",
    ),
    "MCPDispatchFinalizationStoragePort": (
        "finalize_mcp_dispatch",
        "converge_mcp_unknown_no_replay",
        "cancel_mcp_dispatch",
        "converge_inactive_mcp_dispatch",
        "admit_mcp_tool_call",
        "finalize_mcp_dispatch_no_call",
        "converge_user_mcp_no_server",
        "get_mcp_no_server_convergence_receipt",
        "commit_authoritative_mcp_terminal_result",
        "finalize_mcp_dispatch_intent",
        "get_mcp_terminal_result_receipt",
        "get_mcp_terminal_result_receipt_for_call",
        "get_mcp_execution_terminal_projection",
    ),
    "MCPLegacyRetirementStoragePort": (
        "converge_legacy_runtime_retirement",
        "append_mcp_legacy_retirement_evidence",
        "list_mcp_legacy_retirement_task_ids",
    ),
    "MCPCP7StoragePort": (
        "append_mcp_cp7_safety_ledger_record",
        "append_mcp_cp7_ready_epoch_event",
        "get_mcp_cp7_ready_epoch_event",
        "get_mcp_cp7_candidate_guard",
        "produce_mcp_cp7_safety_snapshot",
    ),
    "MCPRemoteTaskStoragePort": (
        "save_mcp_remote_task_binding",
        "get_mcp_remote_task_binding",
        "get_mcp_remote_task_binding_for_call",
        "publish_mcp_remote_task_binding",
        "publish_mcp_remote_task",
        "reconcile_unpublished_mcp_remote_task_bindings",
        "list_due_mcp_remote_task_bindings",
        "claim_due_mcp_remote_task_bindings",
        "renew_mcp_remote_task_binding_claim",
        "release_mcp_remote_task_binding_claim",
        "update_mcp_remote_task_binding_status",
        "finish_mcp_remote_task_binding",
        "finish_mcp_remote_task_binding_from_receipt",
        "claim_mcp_remote_task_outbox",
        "claim_abandoned_mcp_remote_task_controls",
        "pause_mcp_remote_task_for_input",
        "enqueue_mcp_remote_task_control",
        "apply_mcp_remote_task_continuation",
        "get_mcp_remote_task_outbox",
        "admit_mcp_remote_task_continuation",
        "mark_mcp_remote_task_continuation_dispatched",
        "claim_mcp_remote_task_continuations",
        "begin_mcp_remote_task_continuation",
        "abandon_expired_mcp_remote_task_continuations",
        "complete_abandoned_mcp_remote_task_continuation",
        "renew_mcp_remote_task_continuation",
        "begin_mcp_remote_task_control_delivery",
        "complete_mcp_remote_task_outbox",
        "complete_mcp_remote_task_control",
        "delete_mcp_remote_task_binding",
        "converge_dispatched_mcp_calls_to_unknown",
        "count_active_mcp_remote_task_bindings",
        "save_mcp_sealed_state",
        "get_mcp_sealed_state",
        "delete_mcp_sealed_state",
        "save_mcp_connection_lease",
        "list_live_mcp_connection_leases",
        "delete_mcp_connection_lease",
        "expire_mcp_connection_leases",
    ),
    "MCPRolloutStoragePort": (
        "append_mcp_audit_event",
        "list_mcp_audit_events",
        "delete_expired_mcp_audit_events",
        "ensure_mcp_rollout_gate_scope",
        "append_mcp_rollout_drill_observation",
        "list_mcp_rollout_drill_observations",
        "upsert_mcp_rollout_metric_bucket",
        "set_mcp_rollout_metric_bucket",
        "list_mcp_rollout_metric_buckets",
        "save_mcp_shadow_audit_sample",
        "list_mcp_shadow_audit_samples",
        "delete_expired_mcp_shadow_audit_samples",
        "produce_mcp_shadow_evidence_snapshot",
        "append_mcp_rollout_evidence_snapshot",
        "get_mcp_rollout_evidence_snapshot",
        "list_mcp_rollout_evidence_snapshots",
        "append_mcp_rollout_stage_approval",
        "activate_mcp_rollout_deployment",
        "get_mcp_rollout_deployment_activation",
        "append_mcp_rollout_promotion_block",
        "list_active_mcp_rollout_promotion_blocks",
        "append_mcp_rollout_block_resolution",
        "save_mcp_rollout_instance_config_lease",
        "list_mcp_rollout_instance_config_leases",
    ),
    "AuthStoragePort": (
        "create_or_get_maf_master_key_validation",
        "get_maf_master_key_validation",
        "save_auth_user_token",
        "get_auth_user_token",
        "get_auth_user_token_by_hash",
        "get_auth_user_generation",
        "list_auth_user_generations",
        "touch_auth_user_token_last_used",
        "clear_auth_user_token",
        "rotate_auth_user_token",
    ),
    "ConversationTaskAdmissionPort": (
        "admit_submission",
        "claim_pending_submission",
        "renew_submission_claim",
        "acknowledge_submission_projection",
        "prepare_submission_handoff",
        "get_submission_preparation",
        "acknowledge_submission_handoff",
        "close_conversation_admission",
        "reserve_message_identity",
    ),
    "SubmissionPreparationReceiptStoragePort": (
        "write_submission_preparation_component",
        "close_submission_preparation_receipt",
        "get_submission_preparation_receipt",
    ),
    "ConversationStoragePort": (
        "save_conversation",
        "get_conversation",
        "list_conversations_for_username",
        "list_deleting_conversations",
        "mark_conversation_deleting",
        "update_conversation_delete_phase",
        "mark_conversation_delete_failed",
        "retry_failed_conversation_delete",
        "delete_conversation",
        "delete_conversation_physical",
        "save_conversation_file_resource",
        "get_conversation_file_resource",
        "get_conversation_file_resource_by_id",
        "list_conversation_file_resources",
        "mark_conversation_file_resource_deleted",
        "save_conversation_file_resource_with_upload_message",
        "mark_conversation_file_resource_and_upload_message_deleted",
        "compensate_failed_conversation_file_upload",
        "record_conversation_file_index_repair_required",
        "get_conversation_file_index_repair_marker",
        "list_due_conversation_file_index_repairs",
        "mark_conversation_file_index_repairing",
        "mark_conversation_file_index_repair_resolved",
        "mark_conversation_file_index_repair_failed",
        "save_conversation_memory_summary",
        "get_conversation_memory_summary",
        "get_latest_conversation_memory_summary",
        "list_conversation_memory_summaries",
        "delete_conversation_memory_summaries_for_conversation",
    ),
    "PendingSkillContextStoragePort": (
        "save_pending_skill_context",
        "get_pending_skill_context",
        "get_active_pending_skill_context",
        "mark_pending_skill_context_consumed",
        "mark_pending_skill_context_cancelled",
        "mark_pending_skill_context_superseded",
    ),
    "MessageStoragePort": (
        "save_message",
        "get_message",
        "list_messages_for_conversation",
        "upsert_file_upload_message",
        "mark_file_upload_message_deleted",
    ),
    "TaskStoragePort": (
        "save_task",
        "compare_and_set_task",
        "get_task",
        "get_active_task_for_conversation",
        "list_tasks_for_conversation",
        "save_task_node",
        "compare_and_set_task_node",
        "get_task_node",
        "list_task_nodes_for_task",
    ),
    "ArtifactStoragePort": (
        "save_artifact",
        "compare_and_set_artifact_storage_ref",
        "get_artifact",
        "list_artifacts_for_task",
        "list_artifacts_for_conversation",
        "save_task_input_attachment",
        "list_task_input_attachments_for_task",
        "list_task_input_attachments_for_conversation",
    ),
    "EventStoragePort": (
        "append_event",
        "list_events_for_task",
        "list_events_for_task_filtered",
        "list_event_page_for_task",
    ),
    "MailboxStoragePort": (
        "save_mailbox_message",
        "get_mailbox_message",
        "save_mailbox_delivery",
        "get_mailbox_delivery",
        "list_mailbox_messages_for_task",
        "list_mailbox_deliveries_for_message",
    ),
    "InterruptStoragePort": (
        "save_interrupt",
        "get_interrupt",
        "get_interrupt_for_node",
        "list_interrupts_for_task",
        "save_interrupt_answer",
        "get_interrupt_answer",
        "list_interrupt_answers",
    ),
    "SlotStoragePort": (
        "save_slot_collection",
        "get_slot_collection",
        "get_active_slot_collection_for_node",
        "list_slot_collections_for_task",
        "apply_slot_transition",
        "append_slot_event",
        "list_slot_events",
        "get_slot_event_by_idempotency_key",
    ),
    "CheckpointStoragePort": (
        "save_checkpoint",
        "get_checkpoint",
        "get_checkpoint_by_resume_token",
        "list_checkpoints_for_task",
    ),
}


def _direct_async_methods(port: type) -> dict[str, object]:
    return {
        name: value
        for name, value in port.__dict__.items()
        if inspect.iscoroutinefunction(value)
    }


class PersistencePortContractsTest(unittest.TestCase):
    def test_narrow_ports_own_one_exact_disjoint_partition(self) -> None:
        actual_names: list[str] = []
        for port_name, expected_names in EXPECTED_METHODS_BY_PORT.items():
            self.assertTrue(hasattr(contracts, port_name), port_name)
            port = getattr(contracts, port_name)
            direct = _direct_async_methods(port)
            self.assertEqual(tuple(direct), expected_names, port_name)
            self.assertTrue(getattr(port, "_is_runtime_protocol", False), port_name)
            actual_names.extend(direct)

        self.assertEqual(len(actual_names), 271)
        self.assertEqual(len(set(actual_names)), 271)
        self.assertEqual(set(actual_names), set(EXPECTED_STORAGE_METHOD_SIGNATURES))

    def test_aggregate_is_thin_and_preserves_exact_inherited_surface(self) -> None:
        aggregate = contracts.StoragePort
        narrow_ports = tuple(
            getattr(contracts, port_name) for port_name in EXPECTED_METHODS_BY_PORT
        )
        self.assertEqual(_direct_async_methods(aggregate), {})
        self.assertTrue(all(issubclass(aggregate, port) for port in narrow_ports))

        actual = {
            name: str(inspect.signature(value))
            for name, value in inspect.getmembers(
                aggregate, predicate=inspect.iscoroutinefunction
            )
        }
        self.assertEqual(actual, EXPECTED_STORAGE_METHOD_SIGNATURES)
        self.assertNotIn("write_cancellation_token", actual)

    def test_aggregate_keeps_all_four_public_identities(self) -> None:
        self.assertIs(contracts.StoragePort, CoreStoragePort)
        self.assertIs(contracts.StoragePort, StorageInterfacesPort)
        self.assertIs(contracts.StoragePort, StorageExportedPort)
        self.assertEqual(contracts.StoragePort.__module__, "src.core.contracts")


if __name__ == "__main__":
    unittest.main()
