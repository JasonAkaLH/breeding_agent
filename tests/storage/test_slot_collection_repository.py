from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

from src.core.enums import MessageRole, TaskStatus
from src.core.models import Conversation, Message, SlotCollection, SlotEvent, Task
from src.state.postgres.runtime_schema import (
    build_postgres_fresh_cutover_schema_manifest,
    build_runtime_index_schema_ddl,
    build_runtime_table_schema_ddl,
)
from src.storage.sqlite.repositories import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


NOW = datetime(2026, 6, 8, 10, 0, 0)


def _collection(
    *,
    collection_id: str = "slot-collection-1",
    task_id: str = "task-slot-1",
    node_id: str = "node-slot-1",
    conversation_id: str = "conv-slot-1",
    status: str = "waiting_for_user",
    revision: int = 0,
    updated_at: datetime = NOW,
) -> SlotCollection:
    return SlotCollection(
        collection_id=collection_id,
        task_id=task_id,
        node_id=node_id,
        conversation_id=conversation_id,
        capability_id="skill.field-design",
        skill_name="field-design",
        kind="input_collection",
        status=status,
        round=1,
        revision=revision,
        selected_schema_id="diagonal",
        selected_entrypoint="run",
        skill_bundle_revision="bundle-rev-1",
        contract_revision="contract-rev-1",
        schema_digest="sha256:test-schema",
        schema_snapshot={
            "schema_id": "diagonal",
            "inputs": {
                "design": {"type": "string", "const": "diagonal", "aliases": ["对角线增广"]},
                "ncols": {"type": "integer", "validation": {"min": 1}},
            },
        },
        slots={"design": {"status": "missing"}, "ncols": {"status": "missing"}},
        resolved={},
        missing=("design", "ncols"),
        invalid=(),
        last_question="请补充设计类型和列数。",
        created_at=NOW,
        updated_at=updated_at,
    )


def _event(
    *,
    slot_event_id: str = "slot-event-1",
    collection_id: str = "slot-collection-1",
    task_id: str = "task-slot-1",
    node_id: str = "node-slot-1",
    conversation_id: str = "conv-slot-1",
    event_type: str = "slot.collection_updated",
    revision: int = 1,
    idempotency_key: str | None = "answer:interrupt-1:request-1",
    created_at: datetime = NOW,
) -> SlotEvent:
    return SlotEvent(
        slot_event_id=slot_event_id,
        collection_id=collection_id,
        task_id=task_id,
        node_id=node_id,
        conversation_id=conversation_id,
        event_type=event_type,
        round=1,
        revision=revision,
        idempotency_key=idempotency_key,
        payload={"fields": ["design"]},
        created_at=created_at,
    )


class SlotCollectionRepositoryTest(SQLiteStorageTestCase):
    def test_save_get_list_and_active_collection_round_trip(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        first = _collection(updated_at=NOW)
        newer = _collection(
            collection_id="slot-collection-2",
            status="extracting",
            revision=2,
            updated_at=NOW + timedelta(minutes=2),
        )
        completed = _collection(
            collection_id="slot-collection-completed",
            status="completed",
            revision=3,
            updated_at=NOW + timedelta(minutes=3),
        )

        self.assertEqual(asyncio.run(storage.save_slot_collection(first)), first)
        self.assertEqual(asyncio.run(storage.save_slot_collection(newer)), newer)
        self.assertEqual(asyncio.run(storage.save_slot_collection(completed)), completed)

        self.assertEqual(asyncio.run(storage.get_slot_collection(first.collection_id)), first)
        self.assertEqual(
            [collection.collection_id for collection in asyncio.run(storage.list_slot_collections_for_task(first.task_id))],
            ["slot-collection-1", "slot-collection-2", "slot-collection-completed"],
        )
        self.assertEqual(
            asyncio.run(storage.get_active_slot_collection_for_node(first.task_id, first.node_id)),
            newer,
        )

    def test_apply_slot_transition_uses_revision_cas_and_answer_idempotency(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        initial = _collection()
        asyncio.run(storage.save_slot_collection(initial))

        next_collection = replace(
            initial,
            revision=1,
            status="waiting_for_user",
            resolved={"design": {"raw_value": "对角线增广", "value": "diagonal"}},
            missing=("ncols",),
            updated_at=NOW + timedelta(seconds=1),
        )
        event = _event(revision=1)

        saved = asyncio.run(
            storage.apply_slot_transition(
                initial.collection_id,
                0,
                next_collection,
                event,
                idempotency_key=event.idempotency_key,
            )
        )
        self.assertEqual(saved, next_collection)
        self.assertEqual(asyncio.run(storage.list_slot_events(initial.collection_id)), [event])

        repeated = asyncio.run(
            storage.apply_slot_transition(
                initial.collection_id,
                0,
                replace(next_collection, revision=99, status="failed"),
                _event(slot_event_id="slot-event-duplicate", revision=99, idempotency_key=event.idempotency_key),
                idempotency_key=event.idempotency_key,
            )
        )
        self.assertEqual(repeated, next_collection)
        self.assertEqual(asyncio.run(storage.list_slot_events(initial.collection_id)), [event])

        stale = asyncio.run(
            storage.apply_slot_transition(
                initial.collection_id,
                0,
                replace(next_collection, revision=2),
                _event(slot_event_id="slot-event-stale", revision=2, idempotency_key="answer:interrupt-1:request-stale"),
            )
        )
        self.assertIsNone(stale)
        self.assertIsNone(
            asyncio.run(
                storage.get_slot_event_by_idempotency_key(initial.collection_id, "answer:interrupt-1:request-stale")
            )
        )

        final_collection = replace(next_collection, revision=2, status="ready", missing=(), updated_at=NOW + timedelta(seconds=2))
        final_event = _event(
            slot_event_id="slot-event-2",
            revision=2,
            event_type="slot.collection_ready",
            idempotency_key="answer:interrupt-1:request-2",
            created_at=NOW + timedelta(seconds=2),
        )
        self.assertEqual(
            asyncio.run(storage.apply_slot_transition(initial.collection_id, 1, final_collection, final_event)),
            final_collection,
        )
        self.assertEqual(asyncio.run(storage.list_slot_events(initial.collection_id)), [event, final_event])

    def test_append_slot_event_idempotency_and_ordering(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        collection = _collection()
        asyncio.run(storage.save_slot_collection(collection))
        later = _event(
            slot_event_id="slot-event-later",
            event_type="slot.prompt_generated",
            revision=0,
            idempotency_key="prompt:1",
            created_at=NOW + timedelta(seconds=2),
        )
        earlier = _event(
            slot_event_id="slot-event-earlier",
            event_type="slot.collection_started",
            revision=0,
            idempotency_key="start:1",
            created_at=NOW,
        )

        asyncio.run(storage.append_slot_event(later))
        self.assertEqual(asyncio.run(storage.append_slot_event(earlier)), earlier)
        self.assertEqual(
            [event.slot_event_id for event in asyncio.run(storage.list_slot_events(collection.collection_id))],
            ["slot-event-earlier", "slot-event-later"],
        )
        self.assertEqual(
            asyncio.run(storage.append_slot_event(_event(slot_event_id="slot-event-duplicate", idempotency_key="start:1"))),
            earlier,
        )
        self.assertEqual(len(asyncio.run(storage.list_slot_events(collection.collection_id))), 2)

    def test_delete_conversation_removes_slot_rows_and_reports_counts(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        conversation = Conversation(
            conversation_id="conv-slot-delete",
            username="slot-user",
            current_task_id="task-slot-delete",
            created_at=NOW,
            updated_at=NOW,
        )
        root_message = Message(
            message_id="msg-slot-delete",
            conversation_id=conversation.conversation_id,
            task_id="task-slot-delete",
            role=MessageRole.USER,
            content="运行田间设计",
            created_at=NOW,
        )
        task = Task(
            task_id="task-slot-delete",
            conversation_id=conversation.conversation_id,
            root_message_id=root_message.message_id,
            status=TaskStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        collection = _collection(
            collection_id="slot-delete-collection",
            task_id=task.task_id,
            conversation_id=conversation.conversation_id,
        )
        event = _event(
            slot_event_id="slot-delete-event",
            collection_id=collection.collection_id,
            task_id=task.task_id,
            conversation_id=conversation.conversation_id,
        )

        asyncio.run(storage.save_conversation(conversation))
        asyncio.run(storage.save_message(root_message))
        asyncio.run(storage.save_task(task))
        asyncio.run(storage.save_slot_collection(collection))
        asyncio.run(storage.append_slot_event(event))

        deleted_counts = asyncio.run(storage.delete_conversation(conversation.conversation_id))

        self.assertEqual(deleted_counts["slot_event"], 1)
        self.assertEqual(deleted_counts["slot_collection"], 1)
        self.assertIsNone(asyncio.run(storage.get_slot_collection(collection.collection_id)))
        self.assertEqual(asyncio.run(storage.list_slot_events(collection.collection_id)), [])

    def test_postgres_runtime_schema_includes_slot_tables_indexes_and_idempotency_constraint(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        table_ddl = build_runtime_table_schema_ddl().lower()
        index_ddl = build_runtime_index_schema_ddl().lower()

        self.assertIn("slot_collection", manifest.runtime_table_names)
        self.assertIn("slot_event", manifest.runtime_table_names)
        self.assertEqual(manifest.table_columns["slot_collection"]["schema_snapshot_json"], "jsonb")
        self.assertEqual(manifest.table_columns["slot_event"]["payload_json"], "jsonb")
        self.assertIn("create table if not exists slot_collection", table_ddl)
        self.assertIn("create table if not exists slot_event", table_ddl)
        self.assertIn("uq_slot_event_collection_idempotency", table_ddl)
        self.assertIn("idx_slot_collection_task_node_status", index_ddl)
        self.assertIn("idx_slot_event_collection_created", index_ddl)
