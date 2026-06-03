from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import func, select

from src.core.enums import ArtifactType, ConversationStatus, EventVisibility, MessageRole, NodeStatus, TaskStatus
from src.core.models import (
    Artifact,
    AuthUserToken,
    Checkpoint,
    Conversation,
    EventRecord,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    Message,
    Task,
    TaskEdge,
    TaskInputAttachment,
    TaskNode,
)
from src.storage.sqlite.models import (
    ArtifactRow,
    AuthUserTokenRow,
    CheckpointRow,
    ConversationRow,
    EventRecordRow,
    InterruptAnswerRow,
    InterruptRow,
    MailboxDeliveryRow,
    MailboxMessageRow,
    MessageRow,
    TaskEdgeRow,
    TaskInputAttachmentRow,
    TaskNodeRow,
    TaskRow,
)
from src.storage.sqlite.repositories import SQLiteCollaborationRepository, SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class SQLiteConversationDeleteTest(SQLiteStorageTestCase):
    def test_delete_conversation_purges_business_facts_but_keeps_current_auth_token(self) -> None:
        now = datetime(2026, 5, 4, 12, 0, 0)
        with self.session_factory() as session:
            state_repo = SQLiteStateRepository(session)
            collab_repo = SQLiteCollaborationRepository(session)
            state_repo.save_auth_user_token(
                AuthUserToken(
                    username="alice",
                    api_token_hash="token-hash",
                    token_issued_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            state_repo.save_conversation(
                Conversation(
                    conversation_id="conv-delete",
                    username="alice",
                    current_task_id="task-delete",
                    title="待删除会话",
                    created_at=now,
                    updated_at=now,
                )
            )
            state_repo.save_message(
                Message(
                    message_id="msg-user",
                    conversation_id="conv-delete",
                    role=MessageRole.USER,
                    content="你好",
                    task_id="task-delete",
                    created_at=now,
                )
            )
            state_repo.save_task(
                Task(
                    task_id="task-delete",
                    conversation_id="conv-delete",
                    root_message_id="msg-user",
                    status=TaskStatus.RUNNING,
                    root_node_id="node-delete",
                    created_at=now,
                    updated_at=now,
                )
            )
            state_repo.save_task_node(
                TaskNode(
                    node_id="node-delete",
                    task_id="task-delete",
                    capability_id="main_agent.respond",
                    status=NodeStatus.RUNNING,
                )
            )
            state_repo.save_task_edge("task-delete", TaskEdge(from_node_id="node-delete", to_node_id="node-next"))
            state_repo.save_artifact(
                Artifact(
                    artifact_id="artifact-delete",
                    task_id="task-delete",
                    producer_node_id="node-delete",
                    artifact_type=ArtifactType.TEXT,
                    storage_ref="answer",
                    is_complete=True,
                )
            )
            state_repo.save_task_input_attachment(
                TaskInputAttachment(
                    attachment_id="attachment-delete",
                    task_id="task-delete",
                    conversation_id="conv-delete",
                    source_kind="message_upload",
                    source_upload_id="upl-delete",
                    source_message_id="msg-user",
                    interrupt_answer_id="answer-delete",
                    filename="materials.csv",
                    content_type="text/csv",
                    file_type="csv",
                    size_bytes=42,
                    sha256="sha-delete",
                    prompt_artifact={"upload_id": "upl-delete", "filename": "materials.csv"},
                    skill_artifact={
                        "upload_id": "upl-delete",
                        "filename": "materials.csv",
                        "content": "ped_id\nA001\n",
                    },
                    source_payload={"encoding": "base64", "content_base64": "cGVkX2lkCkEwMDEK"},
                    selected_sheet=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            collab_repo.save_event_record(
                EventRecord(
                    event_id="event-delete",
                    conversation_id="conv-delete",
                    task_id="task-delete",
                    event_type="task.accepted",
                    payload={"status": "accepted"},
                    visibility=EventVisibility.FRONTEND,
                )
            )
            collab_repo.save_mailbox_message(
                MailboxMessage(
                    message_id="mail-delete",
                    conversation_id="conv-delete",
                    task_id="task-delete",
                    node_id="node-delete",
                    from_agent="orchestrator",
                    to_agent="main",
                    message_type="dispatch",
                )
            )
            collab_repo.save_mailbox_delivery(
                MailboxDelivery(
                    delivery_id="delivery-delete",
                    message_id="mail-delete",
                    recipient_agent="main",
                )
            )
            collab_repo.save_interrupt(
                Interrupt(
                    interrupt_id="interrupt-delete",
                    conversation_id="conv-delete",
                    task_id="task-delete",
                    node_id="node-delete",
                    source_agent="main",
                    source_message_id="mail-delete",
                    question="补充信息？",
                    reason_code="missing_info",
                )
            )
            collab_repo.save_interrupt_answer(
                InterruptAnswer(
                    interrupt_answer_id="answer-delete",
                    interrupt_id="interrupt-delete",
                    answer_payload={"missing_info": "水稻"},
                    source_message_id="msg-user",
                )
            )
            collab_repo.save_checkpoint(
                Checkpoint(
                    checkpoint_id="checkpoint-delete",
                    task_id="task-delete",
                    node_id="node-delete",
                    agent_id="main",
                    snapshot_ref="memory://checkpoint",
                    snapshot_kind="json",
                    resume_token="resume-delete",
                )
            )
            session.commit()

        storage = SQLiteStorage(self.session_factory)
        deleted_counts = asyncio.run(storage.delete_conversation("conv-delete"))

        self.assertGreaterEqual(deleted_counts["conversation"], 1)
        self.assertGreaterEqual(deleted_counts["message"], 1)
        self.assertGreaterEqual(deleted_counts["task"], 1)
        self.assertGreaterEqual(deleted_counts["event_record"], 1)
        self.assertGreaterEqual(deleted_counts["task_input_attachment"], 1)
        self.assertGreaterEqual(deleted_counts["mailbox_delivery"], 1)
        self.assertGreaterEqual(deleted_counts["interrupt_answer"], 1)
        with self.session_factory() as session:
            for row_type in (
                ConversationRow,
                MessageRow,
                TaskRow,
                TaskNodeRow,
                TaskEdgeRow,
                ArtifactRow,
                TaskInputAttachmentRow,
                EventRecordRow,
                MailboxMessageRow,
                MailboxDeliveryRow,
                InterruptRow,
                InterruptAnswerRow,
                CheckpointRow,
            ):
                self.assertEqual(session.scalar(select(func.count()).select_from(row_type)), 0, row_type.__name__)
            self.assertEqual(session.scalar(select(func.count()).select_from(AuthUserTokenRow)), 1)


    def test_deleting_conversation_is_hidden_from_ordinary_history_and_keeps_metadata(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        with self.session_factory() as session:
            state_repo = SQLiteStateRepository(session)
            state_repo.save_conversation(
                Conversation(
                    conversation_id="conv-active",
                    username="alice",
                    title="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            state_repo.save_conversation(
                Conversation(
                    conversation_id="conv-deleting",
                    username="alice",
                    status=ConversationStatus.DELETING,
                    title="deleting",
                    delete_runner_id="delete-runner",
                    delete_requested_at=now,
                    delete_phase="deleting_db",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        storage = SQLiteStorage(self.session_factory)
        visible = asyncio.run(storage.list_conversations_for_username("alice"))
        self.assertEqual([conversation.conversation_id for conversation in visible], ["conv-active"])

        deleting = asyncio.run(storage.list_deleting_conversations())
        self.assertEqual([conversation.conversation_id for conversation in deleting], ["conv-deleting"])
        self.assertEqual(deleting[0].delete_runner_id, "delete-runner")
        self.assertEqual(deleting[0].delete_phase, "deleting_db")

    def test_delete_failure_status_remains_hidden_and_sanitized(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        with self.session_factory() as session:
            state_repo = SQLiteStateRepository(session)
            state_repo.save_conversation(
                Conversation(
                    conversation_id="conv-fail",
                    username="alice",
                    title="will fail",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        storage = SQLiteStorage(self.session_factory)
        marked = asyncio.run(storage.mark_conversation_deleting(
            "conv-fail",
            runner_id="runner-fail",
            requested_at=now,
            phase="deleting_files",
        ))
        self.assertIsNotNone(marked)
        failed = asyncio.run(storage.mark_conversation_delete_failed(
            "conv-fail",
            failed_at=now,
            phase="deleting_files",
            error_code="FileDeleteError",
            error_summary="password secret token should not be emitted in full" * 20,
            runner_id="runner-fail",
        ))
        self.assertIsNotNone(failed)
        self.assertEqual(failed.status, ConversationStatus.DELETING_FAILED)
        self.assertEqual(failed.delete_error_code, "FileDeleteError")
        self.assertLessEqual(len(failed.delete_error_summary or ""), 500)
        self.assertEqual(asyncio.run(storage.list_conversations_for_username("alice")), [])
    def test_retry_failed_conversation_delete_transitions_back_to_deleting(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        later = datetime(2026, 5, 26, 12, 5, 0)
        with self.session_factory() as session:
            state_repo = SQLiteStateRepository(session)
            state_repo.save_conversation(
                Conversation(
                    conversation_id="conv-retry",
                    username="alice",
                    status=ConversationStatus.DELETING_FAILED,
                    title="retry",
                    delete_runner_id="old-runner",
                    delete_requested_at=now,
                    delete_started_at=now,
                    delete_finished_at=now,
                    delete_failed_at=now,
                    delete_error_code="OldError",
                    delete_error_summary="old sanitized error",
                    delete_phase="deleting_db",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        storage = SQLiteStorage(self.session_factory)
        retried = asyncio.run(storage.retry_failed_conversation_delete(
            "conv-retry",
            runner_id="new-runner",
            requested_at=later,
            started_at=later,
            phase="marking",
        ))

        self.assertIsNotNone(retried)
        assert retried is not None
        self.assertEqual(retried.status, ConversationStatus.DELETING)
        self.assertEqual(retried.delete_runner_id, "new-runner")
        self.assertEqual(retried.delete_requested_at, later)
        self.assertEqual(retried.delete_started_at, later)
        self.assertIsNone(retried.delete_finished_at)
        self.assertIsNone(retried.delete_failed_at)
        self.assertIsNone(retried.delete_error_code)
        self.assertIsNone(retried.delete_error_summary)
        self.assertEqual(retried.delete_phase, "marking")
        self.assertEqual(asyncio.run(storage.list_conversations_for_username("alice")), [])
    def test_save_conversation_does_not_downgrade_deleting_to_active(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        storage = SQLiteStorage(self.session_factory)
        asyncio.run(storage.save_conversation(
            Conversation(
                conversation_id="conv-no-revive",
                username="alice",
                status=ConversationStatus.DELETING,
                title="deleting",
                delete_runner_id="runner",
                delete_requested_at=now,
                delete_phase="deleting_db",
                created_at=now,
                updated_at=now,
            )
        ))

        with self.assertRaises(ValueError):
            asyncio.run(storage.save_conversation(
                Conversation(
                    conversation_id="conv-no-revive",
                    username="alice",
                    status=ConversationStatus.ACTIVE,
                    current_task_id="task-stale",
                    title="stale active snapshot",
                    created_at=now,
                    updated_at=now,
                )
            ))

        current = asyncio.run(storage.get_conversation("conv-no-revive"))
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, ConversationStatus.DELETING)
        self.assertIsNone(current.current_task_id)
