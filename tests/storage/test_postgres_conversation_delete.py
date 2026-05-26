from __future__ import annotations

import inspect
import unittest

from src.storage.postgres.repositories import PostgreSQLStorage


class PostgresConversationDeleteTest(unittest.TestCase):
    def test_physical_delete_uses_set_based_sql_not_python_id_materialization(self) -> None:
        source = inspect.getsource(PostgreSQLStorage._delete_conversation_physical_sync)
        self.assertIn("DELETE FROM artifact", source)
        self.assertIn("USING task", source)
        self.assertIn("DELETE FROM mailbox_delivery", source)
        self.assertIn("USING mailbox_message", source)
        self.assertIn("SELECT task_id FROM task WHERE conversation_id", source)
        self.assertNotIn("task_ids =", source)
        self.assertNotIn("mailbox_message_ids =", source)
        self.assertNotIn("interrupt_ids =", source)

    def test_mark_deleting_uses_row_lock_for_cross_process_claim(self) -> None:
        source = inspect.getsource(PostgreSQLStorage._mark_conversation_deleting_sync)
        self.assertIn("with_for_update", source)
        self.assertIn("ConversationStatus.ACTIVE", source)
        self.assertIn("ConversationStatus.DELETING", source)
