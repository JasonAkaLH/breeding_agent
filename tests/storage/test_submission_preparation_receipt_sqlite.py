from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import inspect as inspect_schema
from sqlalchemy.orm import Session

from src.core.enums import ConversationStatus
from src.core.models import (
    Conversation,
    SubmissionPreparationReceiptComponent,
)
from src.storage.sqlalchemy_models import SubmissionPreparationReceiptRow
from src.storage.sqlite.repositories import SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class SubmissionPreparationReceiptSQLiteTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        self.storage = SQLiteStorage(self.session_factory)
        asyncio.run(
            self.storage.save_conversation(
                Conversation(
                    conversation_id="conversation-1",
                    username="alice",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        )

    def test_schema_is_the_single_narrow_receipt_table(self) -> None:
        inspector = inspect_schema(self.engine)
        columns = {
            column["name"]
            for column in inspector.get_columns("submission_preparation_receipts")
        }
        self.assertEqual(
            columns,
            {
                "task_id",
                "conversation_id",
                "route_decision_json",
                "route_decision_sha256",
                "memory_context_json",
                "memory_context_sha256",
                "selector_decision_json",
                "selector_decision_sha256",
                "receipt_sha256",
                "created_at",
                "updated_at",
            },
        )
        self.assertTrue(
            {
                "owner",
                "job",
                "status",
                "lease",
                "retry",
                "schedule",
                "revision",
            }.isdisjoint(columns)
        )
        self.assertEqual(inspector.get_foreign_keys("submission_preparation_receipts"), [])
        self.assertIn(
            "idx_submission_preparation_receipt_conversation",
            {index["name"] for index in inspector.get_indexes("submission_preparation_receipts")},
        )
        self.assertEqual(
            len(inspector.get_check_constraints("submission_preparation_receipts")),
            4,
        )

    def test_partial_components_close_and_exact_replay_are_immutable(self) -> None:
        route = _canonical({"decision": "agent_run"})
        memory = b"null"
        selector = _canonical({"selected": []})

        first = self._write(
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            route,
            at=self.now,
        )
        self.assertEqual(first.route_decision, route)
        self.assertIsNone(first.memory_context)
        self.assertIsNone(first.receipt_sha256)

        self._write(
            SubmissionPreparationReceiptComponent.MEMORY_CONTEXT,
            memory,
            at=self.now + timedelta(seconds=1),
        )
        settled = self._write(
            SubmissionPreparationReceiptComponent.SELECTOR_DECISION,
            selector,
            at=self.now + timedelta(seconds=2),
        )
        self.assertEqual(settled.memory_context, b"null")
        self.assertIsNone(settled.receipt_sha256)

        closed = asyncio.run(
            self.storage.close_submission_preparation_receipt(
                username="alice",
                conversation_id="conversation-1",
                task_id="task-1",
                closed_at=self.now + timedelta(seconds=3),
            )
        )
        self.assertEqual(
            closed.receipt_sha256,
            hashlib.sha256(
                b"maf.submission.preparation_receipt.v1\0"
                + route
                + b"\0"
                + memory
                + b"\0"
                + selector
            ).hexdigest(),
        )
        replay = self._write(
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            route,
            at=self.now + timedelta(hours=1),
        )
        reclosed = asyncio.run(
            self.storage.close_submission_preparation_receipt(
                username="alice",
                conversation_id="conversation-1",
                task_id="task-1",
                closed_at=self.now + timedelta(hours=1),
            )
        )
        self.assertEqual(replay, closed)
        self.assertEqual(reclosed, closed)
        self.assertEqual(
            asyncio.run(
                self.storage.get_submission_preparation_receipt(
                    username="alice",
                    conversation_id="conversation-1",
                    task_id="task-1",
                )
            ),
            closed,
        )

    def test_component_is_first_write_exact_and_close_requires_all_three(self) -> None:
        self._write(
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            _canonical({"decision": "first"}),
        )
        with self.assertRaisesRegex(RuntimeError, "submission_preparation_receipt_conflict"):
            self._write(
                SubmissionPreparationReceiptComponent.ROUTE_DECISION,
                _canonical({"decision": "different"}),
            )
        with self.assertRaisesRegex(RuntimeError, "submission_preparation_receipt_incomplete"):
            asyncio.run(
                self.storage.close_submission_preparation_receipt(
                    username="alice",
                    conversation_id="conversation-1",
                    task_id="task-1",
                    closed_at=self.now,
                )
            )

    def test_rejects_noncanonical_or_digest_drift_but_accepts_large_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "not_canonical"):
            self._write(
                SubmissionPreparationReceiptComponent.ROUTE_DECISION,
                b'{"z": 1, "a": 2}',
            )
        canonical = _canonical({"decision": "agent_run"})
        with self.assertRaisesRegex(ValueError, "sha256_mismatch"):
            asyncio.run(
                self.storage.write_submission_preparation_component(
                    username="alice",
                    conversation_id="conversation-1",
                    task_id="task-1",
                    component=SubmissionPreparationReceiptComponent.ROUTE_DECISION,
                    canonical_json=canonical,
                    component_sha256="0" * 64,
                    written_at=self.now,
                )
            )

        large = _canonical({"prompt_payload": "x" * (2 * 1024 * 1024)})
        saved = self._write(
            SubmissionPreparationReceiptComponent.MEMORY_CONTEXT,
            large,
        )
        self.assertEqual(saved.memory_context, large)

    def test_owner_and_active_conversation_are_required_without_task_access(self) -> None:
        source = "\n".join(
            inspect.getsource(method)
            for method in (
                SQLiteStateRepository.write_submission_preparation_component,
                SQLiteStateRepository.close_submission_preparation_receipt,
                SQLiteStateRepository.get_submission_preparation_receipt,
            )
        )
        self.assertNotIn("TaskRow", source)
        with self.assertRaisesRegex(RuntimeError, "conversation_not_available"):
            asyncio.run(
                self.storage.get_submission_preparation_receipt(
                    username="bob",
                    conversation_id="conversation-1",
                    task_id="task-1",
                )
            )
        conversation = asyncio.run(self.storage.get_conversation("conversation-1"))
        assert conversation is not None
        asyncio.run(
            self.storage.save_conversation(
                replace(
                    conversation,
                    status=ConversationStatus.DELETING,
                    updated_at=self.now + timedelta(seconds=1),
                )
            )
        )
        with self.assertRaisesRegex(RuntimeError, "conversation_not_available"):
            self._write(
                SubmissionPreparationReceiptComponent.ROUTE_DECISION,
                _canonical({"decision": "agent_run"}),
            )

    def test_concurrent_writers_converge_on_one_component(self) -> None:
        value = _canonical({"decision": "agent_run"})

        async def run() -> list[object]:
            return await asyncio.gather(
                self.storage.write_submission_preparation_component(
                    username="alice",
                    conversation_id="conversation-1",
                    task_id="task-1",
                    component=SubmissionPreparationReceiptComponent.ROUTE_DECISION,
                    canonical_json=value,
                    component_sha256=hashlib.sha256(value).hexdigest(),
                    written_at=self.now,
                ),
                self.storage.write_submission_preparation_component(
                    username="alice",
                    conversation_id="conversation-1",
                    task_id="task-1",
                    component=SubmissionPreparationReceiptComponent.ROUTE_DECISION,
                    canonical_json=value,
                    component_sha256=hashlib.sha256(value).hexdigest(),
                    written_at=self.now + timedelta(seconds=1),
                ),
            )

        first, second = asyncio.run(run())
        self.assertEqual(first, second)
        with self.session_factory() as session:
            self.assertEqual(session.query(SubmissionPreparationReceiptRow).count(), 1)

    def test_fault_rolls_back_receipt_creation(self) -> None:
        original_flush = Session.flush

        def fail_receipt(session: Session, *args: object, **kwargs: object) -> None:
            if any(isinstance(row, SubmissionPreparationReceiptRow) for row in session.new):
                raise RuntimeError("receipt-write-fault")
            original_flush(session, *args, **kwargs)

        with patch.object(Session, "flush", new=fail_receipt):
            with self.assertRaisesRegex(RuntimeError, "receipt-write-fault"):
                self._write(
                    SubmissionPreparationReceiptComponent.ROUTE_DECISION,
                    _canonical({"decision": "agent_run"}),
                )
        with self.session_factory() as session:
            self.assertEqual(session.query(SubmissionPreparationReceiptRow).count(), 0)

    def test_physical_delete_cleans_receipt_and_reports_count(self) -> None:
        self._write(
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            _canonical({"decision": "agent_run"}),
        )
        counts = asyncio.run(self.storage.delete_conversation_physical("conversation-1"))
        self.assertEqual(counts["submission_preparation_receipts"], 1)
        with self.session_factory() as session:
            self.assertEqual(session.query(SubmissionPreparationReceiptRow).count(), 0)

    def _write(
        self,
        component: SubmissionPreparationReceiptComponent,
        value: bytes,
        *,
        at: datetime | None = None,
    ):
        return asyncio.run(
            self.storage.write_submission_preparation_component(
                username="alice",
                conversation_id="conversation-1",
                task_id="task-1",
                component=component,
                canonical_json=value,
                component_sha256=hashlib.sha256(value).hexdigest(),
                written_at=at or self.now,
            )
        )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
