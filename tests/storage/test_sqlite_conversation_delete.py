from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import func, select

from src.core.enums import ArtifactType, EventVisibility, MessageRole, NodeStatus, TaskStatus
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
                EventRecordRow,
                MailboxMessageRow,
                MailboxDeliveryRow,
                InterruptRow,
                InterruptAnswerRow,
                CheckpointRow,
            ):
                self.assertEqual(session.scalar(select(func.count()).select_from(row_type)), 0, row_type.__name__)
            self.assertEqual(session.scalar(select(func.count()).select_from(AuthUserTokenRow)), 1)
