from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from datetime import datetime

from src.core.enums import ArtifactType, DependencyType, NodeCriticality, NodeStatus, RoutingMode, TaskStatus
from src.core.models import Artifact, Task, TaskEdge, TaskInputAttachment, TaskNode
from src.lifecycle.rust_contract import status_list
from src.storage.sqlite.repositories import SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class SQLiteTaskRepositoryTest(SQLiteStorageTestCase):
    def test_active_task_lookup_uses_rust_lifecycle_contract_statuses(self) -> None:
        source = inspect.getsource(SQLiteStateRepository.get_active_task_for_conversation)
        self.assertIn("active_task_statuses", source)
        self.assertNotIn("accepted", source)
        self.assertEqual(status_list("active_task_statuses"), frozenset({"accepted", "planning", "running", "cancelling"}))

    def test_task_node_edge_and_artifact_round_trip(self) -> None:
        task = Task(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.RUNNING,
            routing_mode=RoutingMode.AUTO,
            requested_capability_id="cap.generic_data_lookup",
            root_node_id="node-1",
            summary="task summary",
            cancel_requested_at=datetime(2026, 4, 23, 11, 30, 0),
            created_at=datetime(2026, 4, 23, 11, 0, 0),
            updated_at=datetime(2026, 4, 23, 11, 5, 0),
        )
        node = TaskNode(
            node_id="node-1",
            task_id="task-1",
            capability_id="cap.generic_data_lookup",
            assigned_instance_id="inst-1",
            status=NodeStatus.RUNNING,
            criticality=NodeCriticality.REQUIRED,
            dependency_type=DependencyType.HARD,
            retry_policy={"max_attempts": 1},
            timeout_policy={"seconds": 30},
            resource_class="default",
            input_refs=("msg:1",),
            output_refs=("artifact:1",),
            started_at=datetime(2026, 4, 23, 11, 1, 0),
            finished_at=datetime(2026, 4, 23, 11, 2, 0),
        )
        edge = TaskEdge(from_node_id="node-1", to_node_id="node-2")
        artifact = Artifact(
            artifact_id="artifact-1",
            task_id="task-1",
            producer_node_id="node-1",
            artifact_type=ArtifactType.JSON,
            storage_ref="memory://artifact/1",
            summary="artifact summary",
            is_complete=True,
            created_at=datetime(2026, 4, 23, 11, 2, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_task(task)
            repo.save_task_node(node)
            repo.save_task_edge(task.task_id, edge)
            repo.save_artifact(artifact)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded_task = repo.get_task("task-1")
            loaded_node = repo.get_task_node("node-1")
            loaded_edges = repo.list_task_edges("task-1")
            loaded_artifact = repo.get_artifact("artifact-1")
        self.assertEqual(loaded_task, task)
        self.assertEqual(loaded_node, node)
        self.assertEqual(loaded_edges, [edge])
        self.assertEqual(loaded_artifact, artifact)

    def test_task_identity_and_terminal_status_are_immutable(self) -> None:
        task = Task(
            task_id="task-invariants",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.COMPLETED,
        )
        with self.session_factory() as session:
            SQLiteStateRepository(session).save_task(task)
            session.commit()

        for replacement in (
            replace(task, conversation_id="conv-2"),
            replace(task, root_message_id="msg-2"),
            replace(task, routing_mode=RoutingMode.FORCE_CAPABILITY),
            replace(task, requested_capability_id="skill.other"),
            replace(task, created_at=datetime(2026, 4, 24, 0, 0, 0)),
            replace(task, status=TaskStatus.RUNNING),
        ):
            with self.session_factory() as session:
                with self.assertRaisesRegex(ValueError, "task_.*_immutable"):
                    SQLiteStateRepository(session).save_task(replacement)

    def test_enforce_rejects_new_or_active_null_assignment_and_keeps_terminal_history_read_only(self) -> None:
        active = Task(
            task_id="task-null-active",
            conversation_id="conv-null",
            root_message_id="msg-active",
            status=TaskStatus.RUNNING,
        )
        terminal = replace(
            active,
            task_id="task-null-terminal",
            root_message_id="msg-terminal",
            status=TaskStatus.COMPLETED,
        )
        from tests.storage.test_mcp_task_route_assignment import _TaskSidecar

        sidecar = _TaskSidecar()
        sidecar.tasks[terminal.task_id] = {
            "task_id": terminal.task_id,
            "conversation_id": terminal.conversation_id,
            "root_message_id": terminal.root_message_id,
            "status": str(terminal.status),
            "routing_mode": str(terminal.routing_mode),
            "requested_capability_id": None,
            "root_node_id": None,
            "summary": None,
            "cancel_requested_at": None,
            "created_at": None,
            "updated_at": None,
            "assignment": None,
        }
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )
        for candidate in (
            replace(active, task_id="task-null-new", root_message_id="msg-new"),
            active,
            replace(terminal, summary="changed"),
        ):
            with self.assertRaisesRegex(ValueError, "migration_required"):
                asyncio.run(storage.save_task(candidate))
        with self.assertRaisesRegex(ValueError, "migration_required"):
            asyncio.run(
                storage.compare_and_set_task(
                    active,
                    expected_from_status=TaskStatus.RUNNING,
                )
            )
        self.assertEqual(asyncio.run(storage.save_task(terminal)), terminal)

    def test_task_node_identity_and_terminal_status_are_immutable(self) -> None:
        node = TaskNode(
            node_id="node-invariants",
            task_id="task-1",
            capability_id="main_agent.respond",
            status=NodeStatus.COMPLETED,
        )
        with self.session_factory() as session:
            SQLiteStateRepository(session).save_task_node(node)
            session.commit()

        for replacement in (
            replace(node, task_id="task-2"),
            replace(node, capability_id="skill.other"),
            replace(node, status=NodeStatus.RUNNING),
        ):
            with self.session_factory() as session:
                with self.assertRaisesRegex(ValueError, "task_node_.*_immutable"):
                    SQLiteStateRepository(session).save_task_node(replacement)

    def test_task_and_node_compare_and_set_reject_stale_status_without_mutation(self) -> None:
        task = Task(
            task_id="task-cas",
            conversation_id="conv-cas",
            root_message_id="msg-cas",
            status=TaskStatus.RUNNING,
        )
        node = TaskNode(
            node_id="node-cas",
            task_id=task.task_id,
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_task(task)
            repo.save_task_node(node)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            self.assertIsNone(
                repo.compare_and_set_task(
                    replace(task, status=TaskStatus.COMPLETED),
                    expected_from_status=TaskStatus.PLANNING,
                )
            )
            self.assertIsNone(
                repo.compare_and_set_task_node(
                    replace(node, status=NodeStatus.COMPLETED),
                    expected_from_status=NodeStatus.READY,
                )
            )
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            self.assertEqual(repo.get_task(task.task_id), task)
            self.assertEqual(repo.get_task_node(node.node_id), node)

    def test_task_and_node_compare_and_set_allow_only_one_competing_transition(self) -> None:
        task = Task(
            task_id="task-cas-winner",
            conversation_id="conv-cas",
            root_message_id="msg-cas",
            status=TaskStatus.RUNNING,
        )
        node = TaskNode(
            node_id="node-cas-winner",
            task_id=task.task_id,
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_task(task)
            repo.save_task_node(node)
            session.commit()
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            self.assertIsNotNone(
                repo.compare_and_set_task(
                    replace(task, status=TaskStatus.COMPLETED),
                    expected_from_status=TaskStatus.RUNNING,
                )
            )
            self.assertIsNotNone(
                repo.compare_and_set_task_node(
                    replace(node, status=NodeStatus.COMPLETED),
                    expected_from_status=NodeStatus.RUNNING,
                )
            )
            session.commit()
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            self.assertIsNone(
                repo.compare_and_set_task(
                    replace(task, status=TaskStatus.FAILED),
                    expected_from_status=TaskStatus.RUNNING,
                )
            )
            self.assertIsNone(
                repo.compare_and_set_task_node(
                    replace(node, status=NodeStatus.FAILED),
                    expected_from_status=NodeStatus.RUNNING,
                )
            )

    def test_task_input_attachment_round_trip(self) -> None:
        attachment = TaskInputAttachment(
            attachment_id="tia-task-1-upl-1",
            task_id="task-1",
            conversation_id="conv-1",
            source_kind="message_upload",
            source_upload_id="upl-1",
            source_message_id="msg-1",
            filename="materials.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=32,
            sha256="sha-1",
            prompt_artifact={"upload_id": "upl-1", "filename": "materials.csv", "preview": {"row_count": 1}},
            skill_artifact={"upload_id": "upl-1", "filename": "materials.csv", "content": "ped_id\nA001\n"},
            source_payload={"encoding": "base64", "content_base64": "cGVkX2lkCkEwMDEK"},
            created_at=datetime(2026, 6, 3, 9, 0, 0),
            updated_at=datetime(2026, 6, 3, 9, 1, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            saved = repo.save_task_input_attachment(attachment)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded = repo.list_task_input_attachments_for_task("task-1")

        self.assertEqual(saved, attachment)
        self.assertEqual(loaded, [attachment])
        self.assertNotIn("content", loaded[0].prompt_artifact)
        self.assertEqual(loaded[0].skill_artifact["content"], "ped_id\nA001\n")

    def test_lists_task_input_attachments_for_conversation_by_recent_update(self) -> None:
        older = TaskInputAttachment(
            attachment_id="tia-task-1-upl-1",
            task_id="task-1",
            conversation_id="conv-1",
            source_kind="message_upload",
            source_upload_id="upl-1",
            filename="old.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=10,
            sha256="sha-old",
            created_at=datetime(2026, 6, 3, 9, 0, 0),
            updated_at=datetime(2026, 6, 3, 9, 1, 0),
        )
        newer = TaskInputAttachment(
            attachment_id="tia-task-2-upl-2",
            task_id="task-2",
            conversation_id="conv-1",
            source_kind="file_selector",
            source_upload_id="upl-2",
            filename="new.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=10,
            sha256="sha-new",
            created_at=datetime(2026, 6, 3, 10, 0, 0),
            updated_at=datetime(2026, 6, 3, 10, 1, 0),
        )
        foreign = TaskInputAttachment(
            attachment_id="tia-task-3-upl-3",
            task_id="task-3",
            conversation_id="conv-2",
            source_kind="message_upload",
            source_upload_id="upl-3",
            filename="foreign.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=10,
            sha256="sha-foreign",
            created_at=datetime(2026, 6, 3, 11, 0, 0),
            updated_at=datetime(2026, 6, 3, 11, 1, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_task_input_attachment(older)
            repo.save_task_input_attachment(newer)
            repo.save_task_input_attachment(foreign)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded = repo.list_task_input_attachments_for_conversation("conv-1", limit=2)

        self.assertEqual([item.attachment_id for item in loaded], ["tia-task-2-upl-2", "tia-task-1-upl-1"])

    def test_active_task_lookup_by_conversation(self) -> None:
        active = Task(task_id="task-active", conversation_id="conv-1", root_message_id="msg-1", status=TaskStatus.RUNNING)
        done = Task(task_id="task-done", conversation_id="conv-1", root_message_id="msg-2", status=TaskStatus.COMPLETED)

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_task(done)
            repo.save_task(active)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded = repo.get_active_task_for_conversation("conv-1")

        self.assertEqual(loaded, active)

    def test_list_tasks_for_conversation_can_filter_unfinished_statuses(self) -> None:
        accepted = Task(
            task_id="task-accepted",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.ACCEPTED,
            created_at=datetime(2026, 4, 23, 11, 0, 0),
        )
        running = Task(
            task_id="task-running",
            conversation_id="conv-1",
            root_message_id="msg-2",
            status=TaskStatus.RUNNING,
            created_at=datetime(2026, 4, 23, 11, 1, 0),
        )
        completed = Task(
            task_id="task-completed",
            conversation_id="conv-1",
            root_message_id="msg-3",
            status=TaskStatus.COMPLETED,
            created_at=datetime(2026, 4, 23, 11, 2, 0),
        )
        other_conversation = Task(
            task_id="task-other",
            conversation_id="conv-2",
            root_message_id="msg-4",
            status=TaskStatus.RUNNING,
            created_at=datetime(2026, 4, 23, 11, 3, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            for task in [accepted, running, completed, other_conversation]:
                repo.save_task(task)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded = repo.list_tasks_for_conversation(
                "conv-1",
                statuses={TaskStatus.ACCEPTED, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLING},
            )

        self.assertEqual([task.task_id for task in loaded], ["task-running", "task-accepted"])
