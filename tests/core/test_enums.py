from __future__ import annotations

import unittest

from src.core.enums import (
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
    TaskStatus,
)


class CoreEnumDefinitionTest(unittest.TestCase):
    def assert_str_enum_values(self, enum_cls: type, expected_values: list[str]) -> None:
        self.assertTrue(issubclass(enum_cls, str))
        self.assertEqual([member.value for member in enum_cls], expected_values)

    def test_conversation_status_values(self) -> None:
        self.assert_str_enum_values(ConversationStatus, ["active", "archived", "locked", "deleting", "deleting_failed"])

    def test_message_role_values(self) -> None:
        self.assert_str_enum_values(MessageRole, ["user", "assistant", "system"])

    def test_task_status_values(self) -> None:
        self.assert_str_enum_values(
            TaskStatus,
            ["accepted", "planning", "running", "cancelling", "cancelled", "completed", "failed"],
        )

    def test_routing_mode_values(self) -> None:
        self.assert_str_enum_values(RoutingMode, ["auto", "hint", "force_capability"])

    def test_node_status_values(self) -> None:
        self.assert_str_enum_values(
            NodeStatus,
            [
                "pending",
                "ready",
                "running",
                "waiting_for_dependency",
                "waiting_for_input",
                "ready_to_resume",
                "resuming",
                "cancelling",
                "completed",
                "failed",
                "cancelled",
                "blocked_by_cancellation",
                "orphaned",
            ],
        )

    def test_node_criticality_values(self) -> None:
        self.assert_str_enum_values(NodeCriticality, ["required", "optional", "fallback"])

    def test_dependency_type_values(self) -> None:
        self.assert_str_enum_values(DependencyType, ["hard", "soft"])

    def test_edge_type_values(self) -> None:
        self.assert_str_enum_values(EdgeType, ["data", "control", "fallback"])

    def test_artifact_type_values(self) -> None:
        self.assert_str_enum_values(ArtifactType, ["text", "json", "file", "dataset", "summary"])

    def test_event_visibility_values(self) -> None:
        self.assert_str_enum_values(EventVisibility, ["frontend", "internal", "audit_only"])

    def test_mailbox_channel_values(self) -> None:
        self.assert_str_enum_values(MailboxChannel, ["orchestrator_control", "peer_collaboration", "interrupt_resume"])

    def test_ack_policy_values(self) -> None:
        self.assert_str_enum_values(AckPolicy, ["strong", "light"])

    def test_mailbox_delivery_status_values(self) -> None:
        self.assert_str_enum_values(
            MailboxDeliveryStatus,
            ["pending", "delivered", "acknowledged", "resolved", "expired", "cancelled"],
        )

    def test_interrupt_status_values(self) -> None:
        self.assert_str_enum_values(InterruptStatus, ["open", "answered", "expired", "cancelled"])


if __name__ == "__main__":
    unittest.main()
