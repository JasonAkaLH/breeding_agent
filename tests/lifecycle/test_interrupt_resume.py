from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.enums import NodeStatus
from src.core.models import Checkpoint, Interrupt, InterruptAnswer, Task, TaskNode
from src.lifecycle.interrupt_service import InterruptService
from tests.lifecycle.support import LifecycleSQLiteTestCase


class InterruptResumeTest(LifecycleSQLiteTestCase):
    def test_answer_drives_node_to_ready_to_resume_then_resuming(self) -> None:
        service = InterruptService(self.storage)

        task = Task(task_id="task-1", conversation_id="conv-1", root_message_id="msg-1")
        node = TaskNode(node_id="node-1", task_id="task-1", capability_id="cap.example", status=NodeStatus.READY)
        interrupt = Interrupt(
            interrupt_id="interrupt-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            source_agent="agent-1",
            source_message_id="mail-1",
            question="Which region?",
            reason_code="missing_region",
            created_at=datetime(2026, 4, 23, 16, 0, 0),
        )
        answer = InterruptAnswer(
            interrupt_answer_id="answer-1",
            interrupt_id="interrupt-1",
            answer_payload={"region": "east"},
            created_at=datetime(2026, 4, 23, 16, 1, 0),
            accepted=True,
            accepted_at=datetime(2026, 4, 23, 16, 1, 1),
        )
        checkpoint = Checkpoint(
            checkpoint_id="checkpoint-1",
            task_id="task-1",
            node_id="node-1",
            agent_id="agent-1",
            snapshot_ref="memory://checkpoint/1",
            snapshot_kind="json",
            resume_token="resume-1",
            created_at=datetime(2026, 4, 23, 16, 0, 30),
        )

        asyncio.run(self.storage.save_task(task))
        asyncio.run(self.storage.save_task_node(node))
        asyncio.run(self.storage.save_checkpoint(checkpoint))
        opened_interrupt = asyncio.run(service.open_interrupt(interrupt, now=datetime(2026, 4, 23, 16, 0, 0)))
        self.assertEqual(opened_interrupt.status, "open")

        answered_interrupt = asyncio.run(service.record_answer(answer))
        ready_node = asyncio.run(self.storage.get_task_node("node-1"))

        self.assertEqual(answered_interrupt.status, "answered")
        self.assertEqual(ready_node.status, NodeStatus.READY_TO_RESUME)

        resumed_node = asyncio.run(service.begin_resume("resume-1"))
        self.assertEqual(resumed_node.status, NodeStatus.RESUMING)
