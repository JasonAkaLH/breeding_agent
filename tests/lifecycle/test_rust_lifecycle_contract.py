from __future__ import annotations

import inspect
import unittest
from datetime import datetime

from src.core.enums import NodeStatus, TaskStatus
from src.core.models import Task, TaskNode
from src.lifecycle import task_state_machine
from src.lifecycle.rust_contract import load_lifecycle_contract


class LifecycleRustContractArtifactTest(unittest.TestCase):
    def test_lifecycle_transition_rules_are_backed_by_rust_contract_artifact(self) -> None:
        contract = load_lifecycle_contract()
        self.assertEqual(contract["component"], "maf_lifecycle")
        self.assertEqual(contract["contract_version"], "lifecycle.v1")
        self.assertIn("node.open_interrupt", contract["transitions"])
        self.assertEqual(
            contract["transitions"]["node.begin_resume"],
            {"from": ["ready_to_resume"], "to": "resuming"},
        )

    def test_cancel_and_late_result_policy_comes_from_contract(self) -> None:
        contract = load_lifecycle_contract()
        self.assertEqual(
            contract["cancel_node_targets"]["pending"],
            "blocked_by_cancellation",
        )
        self.assertEqual(contract["cancel_node_targets"]["running"], "cancelled")
        self.assertEqual(
            contract["late_result_rejected_task_statuses"],
            ["cancelling", "cancelled"],
        )

        pending = TaskNode(node_id="node-1", task_id="task-1", capability_id="cap", status=NodeStatus.PENDING)
        running = TaskNode(node_id="node-2", task_id="task-1", capability_id="cap", status=NodeStatus.RUNNING)
        self.assertEqual(task_state_machine.cancel_node(pending).status, NodeStatus.BLOCKED_BY_CANCELLATION)
        self.assertEqual(task_state_machine.cancel_node(running).status, NodeStatus.CANCELLED)

        self.assertFalse(
            task_state_machine.can_accept_late_result(
                Task(task_id="task-1", conversation_id="conv", root_message_id="msg", status=TaskStatus.CANCELLING),
            )
        )

    def test_cancellation_and_timeout_targets_come_from_rust_contract(self) -> None:
        contract = load_lifecycle_contract()
        self.assertEqual(
            contract["transitions"]["mailbox_delivery.retry_timeout"],
            {"from": ["pending", "delivered", "acknowledged"], "to": "pending"},
        )
        self.assertEqual(contract["transitions"]["mailbox_delivery.expire_timeout"]["to"], "expired")
        self.assertEqual(contract["transitions"]["mailbox_delivery.cancel"]["to"], "cancelled")
        self.assertEqual(contract["transitions"]["interrupt.cancel"]["to"], "cancelled")
        self.assertEqual(contract["transitions"]["task.finalize_cancellation"], {"from": ["cancelling", "cancelled"], "to": "cancelled"})
        self.assertEqual(contract["delivery_timeout_error_code"], "ttl_expired")
        self.assertEqual(contract["delivery_timeout_error_message"], "delivery exceeded ttl window")

    def test_python_lifecycle_facade_has_no_inline_cancellation_targets(self) -> None:
        state_machine_source = inspect.getsource(task_state_machine)
        self.assertNotIn("status=TaskStatus.CANCELLED", state_machine_source)
        self.assertNotIn("status=InterruptStatus.CANCELLED", state_machine_source)
        self.assertNotIn("status=MailboxDeliveryStatus.PENDING", state_machine_source)
        self.assertNotIn("status=MailboxDeliveryStatus.EXPIRED", state_machine_source)
        self.assertNotIn("ttl_expired", state_machine_source)
        self.assertNotIn("delivery exceeded ttl window", state_machine_source)

        from src.lifecycle.cancellation_service import CancellationService

        cancellation_source = inspect.getsource(CancellationService)
        self.assertNotIn("MailboxDeliveryStatus.RESOLVED", cancellation_source)
        self.assertNotIn("status=MailboxDeliveryStatus.CANCELLED", cancellation_source)

    def test_python_state_machine_no_longer_owns_inline_transition_sets(self) -> None:
        source = inspect.getsource(task_state_machine)
        self.assertNotIn("NodeStatus.PENDING, NodeStatus.READY, NodeStatus.RUNNING", source)
        self.assertNotIn("TaskStatus.CANCELLING, TaskStatus.CANCELLED", source)

    def test_terminal_task_never_accepts_late_result(self) -> None:
        for status in [TaskStatus.CANCELLING, TaskStatus.CANCELLED]:
            task = Task(
                task_id=f"task-{status.value}",
                conversation_id="conv",
                root_message_id="msg",
                status=status,
                updated_at=datetime(2026, 5, 15, 0, 0, 0),
            )
            self.assertFalse(task_state_machine.can_accept_late_result(task))


if __name__ == "__main__":
    unittest.main()
