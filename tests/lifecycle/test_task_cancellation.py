from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.enums import AckPolicy, MailboxChannel, MailboxDeliveryStatus, NodeStatus, TaskStatus
from src.core.models import Checkpoint, Interrupt, MailboxDelivery, MailboxMessage, Task, TaskNode
from src.lifecycle.cancellation_service import CancellationService
from tests.lifecycle.support import LifecycleSQLiteTestCase


class TaskCancellationTest(LifecycleSQLiteTestCase):
    def test_cancel_task_context_blocks_future_work_and_discards_late_results(self) -> None:
        service = CancellationService(self.storage)

        task = Task(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.RUNNING,
            created_at=datetime(2026, 4, 23, 17, 0, 0),
            updated_at=datetime(2026, 4, 23, 17, 0, 0),
        )
        pending_node = TaskNode(node_id="node-pending", task_id="task-1", capability_id="cap.example", status=NodeStatus.PENDING)
        running_node = TaskNode(node_id="node-running", task_id="task-1", capability_id="cap.example", status=NodeStatus.RUNNING)
        waiting_node = TaskNode(node_id="node-waiting", task_id="task-1", capability_id="cap.example", status=NodeStatus.WAITING_FOR_INPUT)
        completed_node = TaskNode(node_id="node-completed", task_id="task-1", capability_id="cap.example", status=NodeStatus.COMPLETED)
        interrupt = Interrupt(
            interrupt_id="interrupt-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-waiting",
            source_agent="agent-1",
            source_message_id="mail-1",
            question="Need more input?",
            reason_code="missing_info",
        )
        checkpoint = Checkpoint(
            checkpoint_id="checkpoint-1",
            task_id="task-1",
            node_id="node-running",
            agent_id="agent-1",
            snapshot_ref="memory://checkpoint/1",
            snapshot_kind="json",
            resume_token="resume-1",
        )
        message = MailboxMessage(
            message_id="mail-1",
            conversation_id="conv-1",
            task_id="task-1",
            channel=MailboxChannel.ORCHESTRATOR_CONTROL,
            message_type="cancel_notice",
            ack_policy=AckPolicy.STRONG,
        )
        delivery = MailboxDelivery(
            delivery_id="delivery-1",
            message_id="mail-1",
            recipient_agent="worker-1",
            status=MailboxDeliveryStatus.DELIVERED,
            attempt_count=0,
            max_attempts=1,
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 17, 1, 0),
            created_at=datetime(2026, 4, 23, 17, 0, 0),
            updated_at=datetime(2026, 4, 23, 17, 0, 0),
        )

        asyncio.run(self.storage.save_task(task))
        asyncio.run(self.storage.save_task_node(pending_node))
        asyncio.run(self.storage.save_task_node(running_node))
        asyncio.run(self.storage.save_task_node(waiting_node))
        asyncio.run(self.storage.save_task_node(completed_node))
        asyncio.run(self.storage.save_interrupt(interrupt))
        asyncio.run(self.storage.save_checkpoint(checkpoint))
        asyncio.run(self.storage.save_mailbox_message(message))
        asyncio.run(self.storage.save_mailbox_delivery(delivery))

        cancelled_task = asyncio.run(service.cancel_task_context("task-1", now=datetime(2026, 4, 23, 17, 0, 30)))
        self.assertEqual(cancelled_task.status, TaskStatus.CANCELLED)

        reloaded_pending = asyncio.run(self.storage.get_task_node("node-pending"))
        reloaded_running = asyncio.run(self.storage.get_task_node("node-running"))
        reloaded_waiting = asyncio.run(self.storage.get_task_node("node-waiting"))
        reloaded_completed = asyncio.run(self.storage.get_task_node("node-completed"))
        reloaded_interrupt = asyncio.run(self.storage.get_interrupt("interrupt-1"))
        reloaded_checkpoint = asyncio.run(self.storage.get_checkpoint("checkpoint-1"))
        reloaded_delivery = asyncio.run(self.storage.get_mailbox_delivery("delivery-1"))

        self.assertEqual(reloaded_pending.status, NodeStatus.BLOCKED_BY_CANCELLATION)
        self.assertEqual(reloaded_running.status, NodeStatus.CANCELLED)
        self.assertEqual(reloaded_waiting.status, NodeStatus.CANCELLED)
        self.assertEqual(reloaded_completed.status, NodeStatus.COMPLETED)
        self.assertEqual(reloaded_interrupt.status, "cancelled")
        self.assertEqual(reloaded_delivery.status, MailboxDeliveryStatus.CANCELLED)
        self.assertEqual(reloaded_checkpoint.invalidated_at, datetime(2026, 4, 23, 17, 0, 30))
        self.assertFalse(asyncio.run(service.can_accept_late_result("task-1")))
        events = asyncio.run(self.storage.list_events_for_task("task-1"))
        self.assertEqual(len({event.event_id for event in events}), len(events))
        cancelled_node_ids = {
            event.node_id
            for event in events
            if event.event_type == "node.cancelled"
        }
        self.assertEqual(cancelled_node_ids, {"node-running", "node-waiting"})
        blocked_node_ids = {
            event.node_id
            for event in events
            if event.event_type == "node.blocked_by_cancellation"
        }
        self.assertEqual(blocked_node_ids, {"node-pending"})

    def test_cancel_task_context_preserves_terminal_task_statuses(self) -> None:
        service = CancellationService(self.storage)

        for status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            task_id = f"task-terminal-{status.value}"
            task = Task(
                task_id=task_id,
                conversation_id="conv-1",
                root_message_id=f"msg-{status.value}",
                status=status,
                created_at=datetime(2026, 4, 23, 18, 0, 0),
                updated_at=datetime(2026, 4, 23, 18, 1, 0),
            )
            asyncio.run(self.storage.save_task(task))

            unchanged = asyncio.run(service.cancel_task_context(task_id, now=datetime(2026, 4, 23, 18, 2, 0)))
            reloaded = asyncio.run(self.storage.get_task(task_id))

            self.assertEqual(unchanged.status, status)
            self.assertEqual(reloaded.status, status)
            self.assertIsNone(reloaded.cancel_requested_at)
