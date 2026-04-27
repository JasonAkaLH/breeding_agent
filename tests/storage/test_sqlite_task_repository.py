from __future__ import annotations

from datetime import datetime

from src.core.enums import ArtifactType, DependencyType, NodeCriticality, NodeStatus, RoutingMode, TaskStatus
from src.core.models import Artifact, Task, TaskEdge, TaskNode
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteTaskRepositoryTest(SQLiteStorageTestCase):
    def test_task_node_edge_and_artifact_round_trip(self) -> None:
        task = Task(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.RUNNING,
            routing_mode=RoutingMode.AUTO,
            requested_capability_id="cap.sql_query",
            root_node_id="node-1",
            summary="task summary",
            cancel_requested_at=datetime(2026, 4, 23, 11, 30, 0),
            created_at=datetime(2026, 4, 23, 11, 0, 0),
            updated_at=datetime(2026, 4, 23, 11, 5, 0),
        )
        node = TaskNode(
            node_id="node-1",
            task_id="task-1",
            capability_id="cap.sql_query",
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
