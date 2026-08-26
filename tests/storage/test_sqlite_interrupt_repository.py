from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from src.core.enums import InterruptStatus, NodeStatus
from src.core.models import Checkpoint, Interrupt, InterruptAnswer, TaskNode
from src.lifecycle.errors import LifecycleTransitionError
from src.lifecycle.rust_contract import contract_value, status_list
from src.storage.sqlite.repositories import (
    SQLiteCollaborationRepository,
    SQLiteStateRepository,
)
from tests.storage.support import SQLiteStorageTestCase


class SQLiteInterruptRepositoryTest(SQLiteStorageTestCase):
    @staticmethod
    def _interrupt_answer_fixture(
        suffix: str,
    ) -> tuple[Interrupt, InterruptAnswer, TaskNode]:
        interrupt = Interrupt(
            interrupt_id=f"interrupt-atomic-{suffix}",
            conversation_id="conv-1",
            task_id=f"task-atomic-{suffix}",
            node_id=f"node-atomic-{suffix}",
            source_agent="agent-1",
            source_message_id="mail-1",
            question="Which region?",
            reason_code="missing_region",
            status=InterruptStatus.OPEN,
            created_at=datetime(2026, 4, 23, 12, 0, 0),
        )
        answer = InterruptAnswer(
            interrupt_answer_id=f"answer-atomic-{suffix}",
            interrupt_id=interrupt.interrupt_id,
            answer_payload={"region": "east"},
            source_message_id=f"message-atomic-{suffix}",
            created_at=datetime(2026, 4, 23, 12, 1, 0),
        )
        node = TaskNode(
            node_id=interrupt.node_id,
            task_id=interrupt.task_id,
            capability_id="skill.data_query",
            status=NodeStatus.WAITING_FOR_INPUT,
        )
        return interrupt, answer, node

    def test_interrupt_reopen_guard_uses_rust_lifecycle_contract_statuses(self) -> None:
        source = inspect.getsource(SQLiteCollaborationRepository.save_interrupt)
        self.assertIn("interrupt_reopen_guard_terminal_statuses", source)
        self.assertIn("interrupt_open_status", source)
        self.assertNotIn('{"answered", "cancelled", "expired"}', source)
        self.assertNotIn("incoming_status == \"open\"", source)
        self.assertEqual(status_list("interrupt_reopen_guard_terminal_statuses"), frozenset({"answered", "cancelled", "expired"}))
        self.assertEqual(contract_value("interrupt_open_status"), "open")

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

    def test_late_open_interrupt_save_does_not_downgrade_answered_interrupt(self) -> None:
        opened = Interrupt(
            interrupt_id="interrupt-race-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-race-1",
            source_agent="agent-1",
            source_message_id="mail-1",
            question="Which crop?",
            reason_code="missing_crop",
            status=InterruptStatus.OPEN,
        )
        answered = Interrupt(
            interrupt_id=opened.interrupt_id,
            conversation_id=opened.conversation_id,
            task_id=opened.task_id,
            node_id=opened.node_id,
            source_agent=opened.source_agent,
            source_message_id=opened.source_message_id,
            question=opened.question,
            reason_code=opened.reason_code,
            status=InterruptStatus.ANSWERED,
            answered_at=datetime(2026, 4, 23, 12, 2, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            repo.save_interrupt(opened)
            repo.save_interrupt(answered)
            saved = repo.save_interrupt(opened)
            session.commit()

        self.assertEqual(saved.status, InterruptStatus.ANSWERED)
        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            loaded = repo.get_interrupt(opened.interrupt_id)
        self.assertEqual(loaded.status, InterruptStatus.ANSWERED)

    def test_answer_interrupt_atomic_rolls_back_after_each_durable_write(self) -> None:
        for fail_after_flush in (1, 2, 3):
            with self.subTest(fail_after_flush=fail_after_flush):
                interrupt, answer, node = self._interrupt_answer_fixture(
                    str(fail_after_flush)
                )
                with self.session_factory() as session:
                    state_repo = SQLiteStateRepository(session)
                    repo = SQLiteCollaborationRepository(session)
                    state_repo.save_task_node(node)
                    repo.save_interrupt(interrupt)
                    session.commit()

                with self.assertRaisesRegex(RuntimeError, "injected_write_failure"):
                    with self.session_factory() as session:
                        repo = SQLiteCollaborationRepository(session)
                        original_flush = session.flush
                        flush_count = 0

                        def fail_after_write(*args, **kwargs) -> None:
                            nonlocal flush_count
                            original_flush(*args, **kwargs)
                            flush_count += 1
                            if flush_count == fail_after_flush:
                                raise RuntimeError("injected_write_failure")

                        with patch.object(session, "flush", side_effect=fail_after_write):
                            repo.answer_interrupt_atomic(
                                answer,
                                now=datetime(2026, 4, 23, 12, 1, 1),
                            )
                            session.commit()

                with self.session_factory() as session:
                    state_repo = SQLiteStateRepository(session)
                    repo = SQLiteCollaborationRepository(session)
                    self.assertEqual(
                        repo.get_interrupt(interrupt.interrupt_id), interrupt
                    )
                    self.assertEqual(state_repo.get_task_node(node.node_id), node)
                    self.assertEqual(
                        repo.list_interrupt_answers(interrupt.interrupt_id), []
                    )

    def test_answer_interrupt_atomic_repairs_supported_legacy_partial_shapes(self) -> None:
        accepted_at = datetime(2026, 4, 23, 12, 1, 1)
        for interrupt_status, node_status, expected_changed in (
            (InterruptStatus.OPEN, NodeStatus.WAITING_FOR_INPUT, True),
            (InterruptStatus.OPEN, NodeStatus.READY_TO_RESUME, True),
            (InterruptStatus.ANSWERED, NodeStatus.READY_TO_RESUME, False),
        ):
            with self.subTest(
                interrupt_status=interrupt_status,
                node_status=node_status,
            ):
                suffix = f"{interrupt_status}-{node_status}"
                interrupt, answer, node = self._interrupt_answer_fixture(suffix)
                accepted_answer = replace(
                    answer,
                    accepted=True,
                    accepted_at=accepted_at,
                )
                stored_interrupt = replace(
                    interrupt,
                    status=interrupt_status,
                    answered_at=(
                        accepted_at
                        if interrupt_status == InterruptStatus.ANSWERED
                        else None
                    ),
                )
                stored_node = replace(node, status=node_status)
                with self.session_factory() as session:
                    state_repo = SQLiteStateRepository(session)
                    repo = SQLiteCollaborationRepository(session)
                    state_repo.save_task_node(stored_node)
                    repo.save_interrupt(stored_interrupt)
                    repo.save_interrupt_answer(accepted_answer)
                    session.commit()

                with self.session_factory() as session:
                    repo = SQLiteCollaborationRepository(session)
                    saved_interrupt, saved_node, changed = (
                        repo.answer_interrupt_atomic(
                            answer,
                            now=datetime(2026, 4, 23, 12, 2, 0),
                        )
                    )
                    session.commit()

                self.assertEqual(saved_interrupt.status, InterruptStatus.ANSWERED)
                self.assertEqual(saved_interrupt.answered_at, accepted_at)
                self.assertEqual(saved_node.status, NodeStatus.READY_TO_RESUME)
                self.assertEqual(changed, expected_changed)
                with self.session_factory() as session:
                    repo = SQLiteCollaborationRepository(session)
                    self.assertEqual(
                        repo.list_interrupt_answers(interrupt.interrupt_id),
                        [accepted_answer],
                    )

    def test_answer_interrupt_atomic_allows_v2_open_turn_evidence_before_final_answer(self) -> None:
        interrupt, final_answer, node = self._interrupt_answer_fixture(
            "v2-multi-turn"
        )
        open_turn_answer = InterruptAnswer(
            interrupt_answer_id="answer-v2-open-turn",
            interrupt_id=interrupt.interrupt_id,
            answer_payload={"region": "east", "needs_more_input": True},
            source_message_id="message-v2-open-turn",
            accepted=True,
            created_at=datetime(2026, 4, 23, 12, 0, 30),
            accepted_at=datetime(2026, 4, 23, 12, 0, 31),
        )
        with self.session_factory() as session:
            state_repo = SQLiteStateRepository(session)
            repo = SQLiteCollaborationRepository(session)
            state_repo.save_task_node(node)
            repo.save_interrupt(interrupt)
            repo.save_interrupt_answer(open_turn_answer)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            saved_interrupt, saved_node, changed = repo.answer_interrupt_atomic(
                final_answer,
                now=datetime(2026, 4, 23, 12, 1, 1),
            )
            session.commit()

        self.assertTrue(changed)
        self.assertEqual(saved_interrupt.status, InterruptStatus.ANSWERED)
        self.assertEqual(saved_node.status, NodeStatus.READY_TO_RESUME)
        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            answers = repo.list_interrupt_answers(interrupt.interrupt_id)
        self.assertEqual(
            [answer.interrupt_answer_id for answer in answers],
            [open_turn_answer.interrupt_answer_id, final_answer.interrupt_answer_id],
        )
        self.assertTrue(all(answer.accepted for answer in answers))

    def test_answer_interrupt_atomic_rejects_different_final_answer_after_close(self) -> None:
        interrupt, answer, node = self._interrupt_answer_fixture("changed-final")
        with self.session_factory() as session:
            state_repo = SQLiteStateRepository(session)
            repo = SQLiteCollaborationRepository(session)
            state_repo.save_task_node(
                replace(node, status=NodeStatus.READY_TO_RESUME)
            )
            repo.save_interrupt(
                replace(
                    interrupt,
                    status=InterruptStatus.ANSWERED,
                    answered_at=datetime(2026, 4, 23, 12, 1, 1),
                )
            )
            repo.save_interrupt_answer(
                replace(
                    answer,
                    accepted=True,
                    accepted_at=datetime(2026, 4, 23, 12, 1, 1),
                )
            )
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            with self.assertRaises(LifecycleTransitionError):
                repo.answer_interrupt_atomic(
                    replace(
                        answer,
                        interrupt_answer_id="answer-changed-final-retry",
                    ),
                    now=datetime(2026, 4, 23, 12, 2, 0),
                )

    def test_answer_interrupt_atomic_rejects_non_exact_or_unsupported_partial_state(self) -> None:
        interrupt, answer, node = self._interrupt_answer_fixture("inconsistent")
        accepted_answer = replace(
            answer,
            accepted=True,
            accepted_at=datetime(2026, 4, 23, 12, 1, 1),
        )
        with self.session_factory() as session:
            state_repo = SQLiteStateRepository(session)
            repo = SQLiteCollaborationRepository(session)
            state_repo.save_task_node(node)
            repo.save_interrupt(
                replace(
                    interrupt,
                    status=InterruptStatus.ANSWERED,
                    answered_at=accepted_answer.accepted_at,
                )
            )
            repo.save_interrupt_answer(accepted_answer)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            with self.assertRaises(LifecycleTransitionError):
                repo.answer_interrupt_atomic(
                    answer,
                    now=datetime(2026, 4, 23, 12, 2, 0),
                )

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            with self.assertRaises(LifecycleTransitionError):
                repo.answer_interrupt_atomic(
                    replace(answer, answer_payload={"region": "west"}),
                    now=datetime(2026, 4, 23, 12, 2, 0),
                )
