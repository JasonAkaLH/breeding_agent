from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sqlalchemy import select, text

from src.core.enums import ConversationStatus
from src.core.models import Conversation
from src.storage.sqlite.models import ConversationRow
from src.storage.sqlite.repositories import SQLiteStorage, _row_to_conversation


class PostgreSQLStorage(SQLiteStorage):
    """StoragePort facade backed by PostgreSQL SQLAlchemy sessions.

    Most repository behavior reuses the mature SQLiteStorage contract while
    PostgreSQL-specific hot paths can override operations that need dialect
    features. Long conversation deletion is one such path: it must stay
    set-based inside PostgreSQL and avoid pulling large task/message id lists
    into Python.
    """

    async def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await asyncio.to_thread(
            self._mark_conversation_deleting_sync,
            conversation_id,
            runner_id=runner_id,
            requested_at=requested_at,
            started_at=started_at,
            phase=phase,
        )

    def _mark_conversation_deleting_sync(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None,
        phase: str,
    ) -> Conversation | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConversationRow)
                .where(ConversationRow.conversation_id == conversation_id)
                .with_for_update()
            )
            if row is None:
                return None
            if row.status == str(ConversationStatus.DELETING_FAILED):
                session.commit()
                return _row_to_conversation(row)
            if row.status == str(ConversationStatus.DELETING):
                session.commit()
                return _row_to_conversation(row)
            if row.status != str(ConversationStatus.ACTIVE):
                session.commit()
                return None
            row.status = str(ConversationStatus.DELETING)
            row.delete_runner_id = runner_id
            row.delete_requested_at = requested_at
            row.delete_started_at = started_at
            row.delete_finished_at = None
            row.delete_failed_at = None
            row.delete_error_code = None
            row.delete_error_summary = None
            row.delete_phase = phase
            row.updated_at = requested_at
            session.commit()
            return _row_to_conversation(row)

    async def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await asyncio.to_thread(
            self._retry_failed_conversation_delete_sync,
            conversation_id,
            runner_id=runner_id,
            requested_at=requested_at,
            started_at=started_at,
            phase=phase,
        )

    def _retry_failed_conversation_delete_sync(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None,
        phase: str,
    ) -> Conversation | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConversationRow)
                .where(ConversationRow.conversation_id == conversation_id)
                .with_for_update()
            )
            if row is None or row.status != str(ConversationStatus.DELETING_FAILED):
                session.commit()
                return None
            row.status = str(ConversationStatus.DELETING)
            row.delete_runner_id = runner_id
            row.delete_requested_at = requested_at
            row.delete_started_at = started_at
            row.delete_finished_at = None
            row.delete_failed_at = None
            row.delete_error_code = None
            row.delete_error_summary = None
            row.delete_phase = phase
            row.updated_at = requested_at
            session.commit()
            return _row_to_conversation(row)

    async def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._delete_conversation_physical_sync, conversation_id)

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        return await self.delete_conversation_physical(conversation_id)

    def _delete_conversation_physical_sync(self, conversation_id: str) -> dict[str, int]:
        deleted_counts: dict[str, int] = {
            "conversation_memory_summary": 0,
            "conversation_pending_skill_context": 0,
            "mailbox_delivery": 0,
            "interrupt_answer": 0,
            "slot_event": 0,
            "slot_collection": 0,
            "checkpoint": 0,
            "interrupt": 0,
            "mailbox_message": 0,
            "event_record": 0,
            "artifact": 0,
            "task_input_attachment": 0,
            "task_edge": 0,
            "task_node": 0,
            "message": 0,
            "task": 0,
            "conversation": 0,
        }

        def _rowcount(result: Any) -> int:
            rowcount = getattr(result, "rowcount", 0)
            return int(rowcount if rowcount is not None and rowcount > 0 else 0)

        statements: tuple[tuple[str, str], ...] = (
            (
                "mailbox_delivery",
                """
                DELETE FROM mailbox_delivery d
                USING mailbox_message m
                WHERE d.message_id = m.message_id
                  AND (
                    m.conversation_id = :conversation_id
                    OR m.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                  )
                """,
            ),
            (
                "interrupt_answer",
                """
                DELETE FROM interrupt_answer a
                USING interrupt i
                WHERE a.interrupt_id = i.interrupt_id
                  AND (
                    i.conversation_id = :conversation_id
                    OR i.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                  )
                """,
            ),
            (
                "slot_event",
                """
                DELETE FROM slot_event e
                USING slot_collection c
                WHERE e.collection_id = c.collection_id
                  AND (
                    c.conversation_id = :conversation_id
                    OR c.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                  )
                """,
            ),
            (
                "slot_collection",
                """
                DELETE FROM slot_collection c
                WHERE c.conversation_id = :conversation_id
                   OR c.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "checkpoint",
                """
                DELETE FROM checkpoint c
                WHERE c.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "interrupt",
                """
                DELETE FROM interrupt i
                WHERE i.conversation_id = :conversation_id
                   OR i.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "mailbox_message",
                """
                DELETE FROM mailbox_message m
                WHERE m.conversation_id = :conversation_id
                   OR m.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "event_record",
                """
                DELETE FROM event_record e
                WHERE e.conversation_id = :conversation_id
                   OR e.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "artifact",
                """
                DELETE FROM artifact a
                USING task t
                WHERE a.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "task_input_attachment",
                """
                DELETE FROM task_input_attachment a
                USING task t
                WHERE a.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "task_edge",
                """
                DELETE FROM task_edge e
                USING task t
                WHERE e.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "task_node",
                """
                DELETE FROM task_node n
                USING task t
                WHERE n.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "conversation_memory_summary",
                "DELETE FROM conversation_memory_summary WHERE conversation_id = :conversation_id",
            ),
            (
                "conversation_pending_skill_context",
                "DELETE FROM conversation_pending_skill_context WHERE conversation_id = :conversation_id",
            ),
            (
                "message",
                """
                DELETE FROM message m
                WHERE m.conversation_id = :conversation_id
                   OR m.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "task",
                "DELETE FROM task WHERE conversation_id = :conversation_id",
            ),
            (
                "conversation",
                "DELETE FROM conversation WHERE conversation_id = :conversation_id",
            ),
        )

        with self._session_factory() as session:
            for name, sql in statements:
                deleted_counts[name] = _rowcount(session.execute(text(sql), {"conversation_id": conversation_id}))
            session.commit()
        return deleted_counts
