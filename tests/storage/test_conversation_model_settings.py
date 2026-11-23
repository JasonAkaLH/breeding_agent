from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select

from src.core.models import Conversation, ConversationModelSettings
from src.storage.sqlalchemy_models import ConversationModelSettingsRow
from src.storage.sqlite import bootstrap_sqlite_database
from src.storage.sqlite.repositories import SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase
from tests.storage.test_submission_admission_sqlite import (
    _request,
    _FakeSubmissionSidecar,
)


def settings(conversation_id="conversation-1", *, effort="high", thinking=True):
    return ConversationModelSettings(
        conversation_id, "model-test", effort, thinking, 1, datetime(2026, 11, 23)
    )


def selected_request(**kwargs):
    request = _request(**kwargs)
    continuation = json.loads(request.continuation)
    continuation["model_options"] = {
        "model_edition": "model-test",
        "reasoning_effort": "high",
        "thinking_enabled": True,
    }
    payload = json.dumps(
        continuation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return replace(
        request,
        continuation=payload,
        continuation_sha256=hashlib.sha256(
            b"maf.submission.continuation.v1\0" + payload
        ).hexdigest(),
    )


class ConversationModelSettingsTest(SQLiteStorageTestCase):
    def setUp(self):
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)

    def test_atomic_tuple_revision_and_parent_fields_unchanged(self):
        async def run():
            parent = Conversation(
                "conversation-1", "owner", title="title", current_task_id="running-task"
            )
            await self.storage.save_conversation(parent)
            self.assertIsNone(
                await self.storage.get_conversation_model_settings(
                    "conversation-1", username="owner"
                )
            )
            first = await self.storage.put_conversation_model_settings(
                settings(), username="owner", expected_revision=0
            )
            self.assertEqual(first.revision, 1)
            stale = await self.storage.put_conversation_model_settings(
                settings(effort="low", thinking=False),
                username="owner",
                expected_revision=0,
            )
            self.assertIsNone(stale)
            self.assertEqual(
                await self.storage.get_conversation_model_settings(
                    "conversation-1", username="owner"
                ),
                first,
            )
            self.assertEqual(
                await self.storage.get_conversation("conversation-1"), parent
            )
            second = await self.storage.put_conversation_model_settings(
                settings(effort="low", thinking=False),
                username="owner",
                expected_revision=1,
            )
            self.assertEqual(
                (second.revision, second.reasoning_effort, second.deep_thinking),
                (2, "low", False),
            )

        asyncio.run(run())

    def test_owner_missing_parent_and_deletion_reject_without_orphan(self):
        async def run():
            with self.assertRaises(PermissionError):
                await self.storage.put_conversation_model_settings(
                    settings(), username="owner", expected_revision=0
                )
            self.assertIsNone(await self.storage.get_conversation("conversation-1"))
            await self.storage.save_conversation(
                Conversation("conversation-1", "owner")
            )
            with self.assertRaises(PermissionError):
                await self.storage.put_conversation_model_settings(
                    settings(), username="other", expected_revision=0
                )
            await self.storage.put_conversation_model_settings(
                settings(), username="owner", expected_revision=0
            )
            await self.storage.mark_conversation_deleting(
                "conversation-1",
                runner_id="delete",
                requested_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            with self.assertRaises(PermissionError):
                await self.storage.put_conversation_model_settings(
                    settings(), username="owner", expected_revision=1
                )
            counts = await self.storage.delete_conversation_physical("conversation-1")
            self.assertEqual(counts["conversation_model_settings"], 1)
            with self.assertRaises(PermissionError):
                await self.storage.put_conversation_model_settings(
                    settings(), username="owner", expected_revision=0
                )
            with self.session_factory() as session:
                self.assertIsNone(
                    session.get(ConversationModelSettingsRow, "conversation-1")
                )

        asyncio.run(run())

    def test_upgrade_creates_table_without_backfilling_old_conversations(self):
        asyncio.run(
            self.storage.save_conversation(Conversation("old", "owner", title="keep"))
        )
        ConversationModelSettingsRow.__table__.drop(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.assertIsNone(
            asyncio.run(
                self.storage.get_conversation_model_settings("old", username="owner")
            )
        )
        self.assertEqual(
            asyncio.run(self.storage.get_conversation("old")).title, "keep"
        )

    def test_first_admission_initializes_frozen_tuple_and_replay_never_overwrites(self):
        async def run():
            request = selected_request()
            admitted = await self.storage.admit_submission(request)
            saved = await self.storage.get_conversation_model_settings(
                "conversation-1", username="owner"
            )
            self.assertEqual(
                (saved.model_edition, saved.reasoning_effort, saved.deep_thinking),
                ("model-test", "high", True),
            )
            edited = await self.storage.put_conversation_model_settings(
                settings(effort="low"), username="owner", expected_revision=1
            )
            await self.storage.admit_submission(request)
            # Sidecar exact projection must preserve settings too.
            await self.storage._run_submission_projection(admitted.record)
            self.assertEqual(
                await self.storage.get_conversation_model_settings(
                    "conversation-1", username="owner"
                ),
                edited,
            )

        asyncio.run(run())

    def test_sidecar_first_projection_initializes_and_existing_parent_is_not_backfilled(
        self,
    ):
        async def run():
            authority = _FakeSubmissionSidecar()
            enforce = SQLiteStorage(
                self.session_factory,
                runtime_sidecar_client=authority,
                mcp_task_authority_mode="enforce",
            )
            admitted = await enforce.admit_submission(selected_request())
            await enforce._run_submission_projection(admitted.record)
            saved = await enforce.get_conversation_model_settings(
                "conversation-1", username="owner"
            )
            self.assertTrue(saved.deep_thinking)
            await self.storage.save_conversation(Conversation("old", "owner"))
            await self.storage.admit_submission(
                selected_request(conversation_id="old", message_id="m2", task_id="t2")
            )
            self.assertIsNone(
                await self.storage.get_conversation_model_settings(
                    "old", username="owner"
                )
            )

        asyncio.run(run())

    def test_admission_rollback_removes_parent_and_settings_together(self):
        with patch.object(
            SQLiteStateRepository, "save_task", side_effect=RuntimeError("rollback")
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                asyncio.run(self.storage.admit_submission(selected_request()))
        with self.session_factory() as session:
            self.assertEqual(
                list(session.scalars(select(ConversationModelSettingsRow))), []
            )
        self.assertIsNone(asyncio.run(self.storage.get_conversation("conversation-1")))
