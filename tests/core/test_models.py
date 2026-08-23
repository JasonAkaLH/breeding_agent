from __future__ import annotations

import dataclasses
import unittest

from src.core.enums import (
    AckPolicy,
    ArtifactType,
    ConversationStatus,
    EventVisibility,
    MailboxChannel,
    MailboxDeliveryStatus,
    NodeStatus,
    RoutingMode,
    TaskStatus,
)
from src.core.models import (
    Artifact,
    Checkpoint,
    Conversation,
    ConversationFileIndexRepairMarker,
    EventRecord,
    FileUploadMessageProjection,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    Message,
    Task,
    TaskNode,
)


class CoreModelDefinitionTest(unittest.TestCase):
    def assert_dataclass_contract(self, model: type, expected_fields: list[str]) -> None:
        self.assertTrue(dataclasses.is_dataclass(model))
        self.assertTrue(model.__dataclass_params__.frozen)
        self.assertTrue(hasattr(model, "__slots__"))
        self.assertEqual([field.name for field in dataclasses.fields(model)], expected_fields)

    def test_conversation_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            Conversation,
            [
                "conversation_id",
                "username",
                "status",
                "current_task_id",
                "title",
                "created_at",
                "updated_at",
                "delete_runner_id",
                "delete_requested_at",
                "delete_started_at",
                "delete_finished_at",
                "delete_failed_at",
                "delete_error_code",
                "delete_error_summary",
                "delete_phase",
            ],
        )

    def test_message_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            Message,
            [
                "message_id",
                "conversation_id",
                "role",
                "content",
                "task_id",
                "stream_status",
                "created_at",
                "message_type",
                "metadata",
                "updated_at",
            ],
        )

    def test_message_metadata_default_is_not_shared(self) -> None:
        first = Message("msg-1", "conv-1", "user", "hello")
        second = Message("msg-2", "conv-1", "assistant", "hi")

        self.assertEqual(first.message_type, "chat")
        self.assertEqual(dict(first.metadata), {})
        self.assertIsNot(first.metadata, second.metadata)

    def test_file_upload_message_projection_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            FileUploadMessageProjection,
            [
                "upload_id",
                "conversation_id",
                "content",
                "metadata",
                "created_at",
            ],
        )

    def test_conversation_file_index_repair_marker_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            ConversationFileIndexRepairMarker,
            [
                "conversation_id",
                "repair_kind",
                "status",
                "reason_code",
                "affected_upload_ids",
                "attempt_count",
                "next_retry_at",
                "created_at",
                "updated_at",
                "resolved_at",
            ],
        )

    def test_task_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            Task,
            [
                "task_id",
                "conversation_id",
                "root_message_id",
                "status",
                "routing_mode",
                "requested_capability_id",
                "summary",
                "cancel_requested_at",
                "created_at",
                "updated_at",
                "mcp_execution_mode",
                "mcp_shadow_enabled",
                "mcp_rollout_config_version",
                "mcp_route_reason_code",
                "mcp_rollout_mode",
            ],
        )

    def test_task_node_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            TaskNode,
            [
                "node_id",
                "task_id",
                "capability_id",
                "assigned_instance_id",
                "status",
                "input_refs",
                "output_refs",
                "started_at",
                "finished_at",
            ],
        )

    def test_artifact_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            Artifact,
            [
                "artifact_id",
                "task_id",
                "producer_node_id",
                "artifact_type",
                "storage_ref",
                "summary",
                "is_complete",
                "created_at",
            ],
        )

    def test_event_record_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            EventRecord,
            [
                "event_id",
                "conversation_id",
                "task_id",
                "node_id",
                "agent_id",
                "event_type",
                "payload",
                "visibility",
                "created_at",
            ],
        )

    def test_mailbox_message_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            MailboxMessage,
            [
                "message_id",
                "conversation_id",
                "task_id",
                "node_id",
                "parent_message_id",
                "correlation_id",
                "from_agent",
                "to_agent",
                "to_role",
                "channel",
                "message_type",
                "ack_policy",
                "priority",
                "payload",
                "payload_schema_version",
                "created_at",
                "resolved_at",
            ],
        )

    def test_mailbox_delivery_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            MailboxDelivery,
            [
                "delivery_id",
                "message_id",
                "recipient_agent",
                "recipient_role",
                "status",
                "attempt_count",
                "max_attempts",
                "ttl_seconds",
                "expires_at",
                "delivered_at",
                "acknowledged_at",
                "resolved_at",
                "next_retry_at",
                "last_error_code",
                "last_error_message",
                "created_at",
                "updated_at",
            ],
        )

    def test_interrupt_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            Interrupt,
            [
                "interrupt_id",
                "conversation_id",
                "task_id",
                "node_id",
                "source_agent",
                "source_message_id",
                "question",
                "reason_code",
                "required_fields",
                "status",
                "expires_at",
                "created_at",
                "answered_at",
                "cancelled_at",
            ],
        )

    def test_checkpoint_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            Checkpoint,
            [
                "checkpoint_id",
                "task_id",
                "node_id",
                "agent_id",
                "snapshot_ref",
                "snapshot_kind",
                "resume_token",
                "source_message_id",
                "created_at",
                "invalidated_at",
            ],
        )

    def test_interrupt_answer_fields_match_prd(self) -> None:
        self.assert_dataclass_contract(
            InterruptAnswer,
            [
                "interrupt_answer_id",
                "interrupt_id",
                "answer_payload",
                "source_message_id",
                "accepted",
                "created_at",
                "accepted_at",
            ],
        )


class CoreModelDefaultValueTest(unittest.TestCase):
    def test_models_use_generic_defaults_not_business_specific_defaults(self) -> None:
        conversation = Conversation(conversation_id="conv-1", username="acc-1")
        task = Task(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
        )
        node = TaskNode(node_id="node-1", task_id="task-1", capability_id="cap.example")
        artifact = Artifact(
            artifact_id="art-1",
            task_id="task-1",
            producer_node_id="node-1",
            artifact_type=ArtifactType.JSON,
            storage_ref="memory://artifact/art-1",
        )
        event = EventRecord(
            event_id="evt-1",
            conversation_id="conv-1",
            task_id="task-1",
            event_type="task.accepted",
        )
        mailbox = MailboxMessage(
            message_id="mail-1",
            conversation_id="conv-1",
            task_id="task-1",
            from_agent="orchestrator",
            channel=MailboxChannel.ORCHESTRATOR_CONTROL,
            message_type="dispatch",
        )
        delivery = MailboxDelivery(
            delivery_id="delivery-1",
            message_id="mail-1",
            recipient_agent="worker-1",
        )
        interrupt = Interrupt(
            interrupt_id="interrupt-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            source_agent="agent-1",
            source_message_id="mail-1",
            question="Need more details?",
            reason_code="missing_information",
        )
        interrupt_answer = InterruptAnswer(
            interrupt_answer_id="answer-1",
            interrupt_id="interrupt-1",
            answer_payload={"region": "east"},
        )
        checkpoint = Checkpoint(
            checkpoint_id="ckpt-1",
            task_id="task-1",
            node_id="node-1",
            agent_id="agent-1",
            snapshot_ref="memory://checkpoint/ckpt-1",
            snapshot_kind="json",
            resume_token="resume-1",
        )

        self.assertEqual(conversation.status, ConversationStatus.ACTIVE)
        self.assertEqual(task.status, TaskStatus.ACCEPTED)
        self.assertEqual(task.routing_mode, RoutingMode.AUTO)
        self.assertEqual(node.status, NodeStatus.PENDING)
        self.assertEqual(artifact.summary, None)
        self.assertEqual(event.visibility, EventVisibility.INTERNAL)
        self.assertEqual(mailbox.ack_policy, AckPolicy.LIGHT)
        self.assertEqual(delivery.status, MailboxDeliveryStatus.PENDING)
        self.assertEqual(interrupt.required_fields, {})
        self.assertEqual(interrupt_answer.source_message_id, None)
        self.assertEqual(interrupt_answer.accepted, False)
        self.assertEqual(checkpoint.source_message_id, None)


if __name__ == "__main__":
    unittest.main()
