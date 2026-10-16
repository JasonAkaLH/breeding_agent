from __future__ import annotations

import unittest

from sqlalchemy import delete

from src.storage.knowledge_repository import KnowledgeRepository
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.sqlalchemy_models import (
    ConversationKnowledgeSelectionRow,
    ConversationRow,
    KnowledgeDocumentRow,
    KnowledgeImportRow,
    KnowledgeLibraryRow,
    KnowledgeQueryScopeRow,
    LibraryGrantRow,
)
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason
from tests.storage.test_knowledge_repository import KnowledgeRepositoryContract


class KnowledgePostgresIntegrationTest(KnowledgeRepositoryContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_KNOWLEDGE_TEST_DSN", fallback_env="MAF_POSTGRES_TEST_DSN"
        )
        if reason:
            raise unittest.SkipTest(reason)
        cls.engine = create_postgres_engine(dsn, pool_size=6, max_overflow=0)
        bootstrap_postgres_database(cls.engine)
        cls.factory = create_postgres_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.created_libraries = set()
        self.created_scopes = set()
        self.created_conversations = set()
        outer = self

        class TrackingRepository(KnowledgeRepository):
            def save_selection(self, actor, conversation_id, *args, **kwargs):
                outer.created_conversations.add(conversation_id)
                return super().save_selection(actor, conversation_id, *args, **kwargs)

            def freeze_scope(self, actor, conversation_id, root_message_id):
                outer.created_scopes.add(root_message_id)
                return super().freeze_scope(actor, conversation_id, root_message_id)

        self.repo = TrackingRepository(self.factory)

    def library(self, owner="alice"):
        # Avoid touching any pre-existing data in a shared isolated test database.
        # The contract uses literal alice/bob for access, so test libraries retain it.
        row = self.repo.create_library(owner, "研究", "说明")
        self.created_libraries.add(row["library_id"])
        return row

    def tearDown(self):
        libraries = self.created_libraries
        scopes = self.created_scopes
        conversations = self.created_conversations
        with self.factory() as session:
            for model in (
                LibraryGrantRow,
                KnowledgeImportRow,
                KnowledgeDocumentRow,
                KnowledgeLibraryRow,
            ):
                session.execute(delete(model).where(model.library_id.in_(libraries)))
            session.execute(
                delete(KnowledgeQueryScopeRow).where(
                    KnowledgeQueryScopeRow.root_message_id.in_(scopes)
                )
            )
            session.execute(
                delete(ConversationKnowledgeSelectionRow).where(
                    ConversationKnowledgeSelectionRow.conversation_id.in_(conversations)
                )
            )
            session.execute(
                delete(ConversationRow).where(
                    ConversationRow.conversation_id.in_(conversations)
                )
            )
            session.commit()
