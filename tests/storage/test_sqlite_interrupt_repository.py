from __future__ import annotations

from datetime import datetime

from src.core.enums import InterruptStatus
from src.core.models import Checkpoint, Interrupt, InterruptAnswer
from src.storage.sqlite.repositories import SQLiteCollaborationRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteInterruptRepositoryTest(SQLiteStorageTestCase):
    def test_interrupt_answer_and_checkpoint_round_trip(self) -> None:
        interrupt = Interrupt(
            interrupt_id="interrupt-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            source_agent="agent-1",
            source_message_id="mail-1",
            question="Which region?",
            reason_code="missing_region",
            required_fields={"region": {"type": "string"}},
            status=InterruptStatus.OPEN,
            expires_at=datetime(2026, 4, 23, 12, 10, 0),
            created_at=datetime(2026, 4, 23, 12, 0, 0),
        )
        answer = InterruptAnswer(
            interrupt_answer_id="answer-1",
            interrupt_id="interrupt-1",
            answer_payload={"region": "east"},
            source_message_id="msg-2",
            accepted=True,
            created_at=datetime(2026, 4, 23, 12, 1, 0),
            accepted_at=datetime(2026, 4, 23, 12, 1, 1),
        )
        checkpoint = Checkpoint(
            checkpoint_id="checkpoint-1",
            task_id="task-1",
            node_id="node-1",
            agent_id="agent-1",
            snapshot_ref="memory://checkpoints/1",
            snapshot_kind="json",
            resume_token="resume-1",
            source_message_id="mail-1",
            created_at=datetime(2026, 4, 23, 12, 0, 30),
        )

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            repo.save_interrupt(interrupt)
            repo.save_interrupt_answer(answer)
            repo.save_checkpoint(checkpoint)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            loaded_interrupt = repo.get_interrupt("interrupt-1")
            loaded_answer = repo.get_interrupt_answer("answer-1")
            listed_answers = repo.list_interrupt_answers("interrupt-1")
            loaded_checkpoint = repo.get_checkpoint("checkpoint-1")
        self.assertEqual(loaded_interrupt, interrupt)
        self.assertEqual(loaded_answer, answer)
        self.assertEqual(listed_answers, [answer])
        self.assertEqual(loaded_checkpoint, checkpoint)

    def test_interrupt_and_checkpoint_lookup_helpers(self) -> None:
        interrupt = Interrupt(
            interrupt_id="interrupt-lookup-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-lookup-1",
            source_agent="agent-1",
            source_message_id="mail-1",
            question="Which region?",
            reason_code="missing_region",
        )
        checkpoint = Checkpoint(
            checkpoint_id="checkpoint-lookup-1",
            task_id="task-1",
            node_id="node-lookup-1",
            agent_id="agent-1",
            snapshot_ref="memory://checkpoints/lookup",
            snapshot_kind="json",
            resume_token="resume-lookup-1",
        )

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            repo.save_interrupt(interrupt)
            repo.save_checkpoint(checkpoint)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            loaded_interrupt = repo.get_interrupt_for_node("task-1", "node-lookup-1")
            loaded_checkpoint = repo.get_checkpoint_by_resume_token("resume-lookup-1")

        self.assertEqual(loaded_interrupt, interrupt)
        self.assertEqual(loaded_checkpoint, checkpoint)
