from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import text

from src.core.enums import RoutingMode, TaskStatus
from src.core.models import Task
from src.storage.runtime_sidecar_facade import validate_runtime_sidecar_response
from src.storage.sqlite.repositories import SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


def _assigned_task(task_id: str = "task-assigned") -> Task:
    return Task(
        task_id=task_id,
        conversation_id="conv-1",
        root_message_id="msg-1",
        status=TaskStatus.ACCEPTED,
        routing_mode=RoutingMode.AUTO,
        requested_capability_id="mcp.dispatch",
        root_node_id="node-1",
        summary="initial",
        created_at=datetime(2026, 8, 13, 9, 0, 0),
        updated_at=datetime(2026, 8, 13, 9, 1, 0),
        mcp_execution_mode="legacy",
        mcp_shadow_enabled=True,
        mcp_rollout_config_version="mcp-rollout-v1:abc123",
        mcp_route_reason_code="shadow_enabled",
        mcp_rollout_mode="shadow",
    )


class _TaskSidecar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.tasks: dict[str, dict[str, object]] = {}

    def submit_task(self, **payload: object) -> dict[str, object]:
        self.calls.append(("task_submit", payload))
        task = dict(payload["task"])  # type: ignore[arg-type]
        self.tasks[str(payload["task_id"])] = task
        return {
            "operation": "task_submit",
            "task_id": payload["task_id"],
            "duplicate": False,
            "task": task,
            "error": None,
        }

    def get_task(self, **payload: object) -> dict[str, object]:
        self.calls.append(("task_get", payload))
        task = self.tasks.get(str(payload["task_id"]))
        return {
            "operation": "task_get",
            "found": task is not None,
            "task": task,
            "error": None,
        }


class MCPTaskRouteAssignmentStorageTest(SQLiteStorageTestCase):
    def test_explicit_mcp_task_authority_mode_rejects_unusable_configuration(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "MCP Task authority requires a Rust runtime sidecar client",
        ):
            SQLiteStorage(
                self.session_factory,
                mcp_task_authority_mode="enforce",
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "MCP Task shadow authority requires a runtime sidecar comparison sink",
        ):
            SQLiteStorage(
                self.session_factory,
                runtime_sidecar_client=_TaskSidecar(),
                mcp_task_authority_mode="shadow",
            )

    def test_explicit_off_authority_overrides_legacy_runtime_store_enforce_env(
        self,
    ) -> None:
        task = _assigned_task("task-canonical-off")
        sidecar = _TaskSidecar()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="off",
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            self.assertEqual(asyncio.run(storage.save_task(task)), task)
            self.assertEqual(asyncio.run(storage.get_task(task.task_id)), task)

        self.assertEqual(sidecar.calls, [])

    def test_explicit_shadow_authority_overrides_legacy_runtime_store_off_env(
        self,
    ) -> None:
        task = _assigned_task("task-canonical-shadow")
        sidecar = _TaskSidecar()
        audit_events: list[dict[str, str]] = []
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=audit_events.append,
            mcp_task_authority_mode="shadow",
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "off"}):
            self.assertEqual(asyncio.run(storage.save_task(task)), task)
            self.assertEqual(asyncio.run(storage.get_task(task.task_id)), task)

        self.assertEqual(
            [operation for operation, _payload in sidecar.calls],
            ["task_submit", "task_get"],
        )
        self.assertEqual(
            [event["operation"] for event in audit_events],
            ["task_submit", "task_get"],
        )

    def test_explicit_enforce_authority_overrides_legacy_runtime_store_off_env(
        self,
    ) -> None:
        task = _assigned_task("task-canonical-enforce")
        sidecar = _TaskSidecar()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "off"}):
            self.assertEqual(asyncio.run(storage.save_task(task)), task)
            self.assertEqual(asyncio.run(storage.get_task(task.task_id)), task)

        self.assertEqual(
            [operation for operation, _payload in sidecar.calls],
            ["task_get", "task_submit", "task_get"],
        )
        self.assertIsNone(
            asyncio.run(SQLiteStorage(self.session_factory).get_task(task.task_id))
        )

    def test_sqlite_round_trip_and_normal_update_preserve_assignment(self) -> None:
        task = _assigned_task()
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_task(task)
            session.commit()

        updated = replace(task, status=TaskStatus.RUNNING, summary="running")
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            saved = repo.save_task(updated)
            session.commit()

        self.assertEqual(saved, updated)
        with self.session_factory() as session:
            self.assertEqual(SQLiteStateRepository(session).get_task(task.task_id), updated)

    def test_partial_assignment_is_rejected_and_all_null_history_remains_readable(self) -> None:
        partial = replace(
            _assigned_task("task-partial"),
            mcp_rollout_config_version=None,
        )
        with self.session_factory() as session:
            with self.assertRaisesRegex(ValueError, "mcp_task_route_assignment_corrupt"):
                SQLiteStateRepository(session).save_task(partial)

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task "
                    "(task_id, conversation_id, root_message_id, status, routing_mode) "
                    "VALUES ('task-history', 'conv-1', 'msg-history', 'completed', 'auto')"
                )
            )
        with self.session_factory() as session:
            history = SQLiteStateRepository(session).get_task("task-history")

        self.assertIsNotNone(history)
        assert history is not None
        self.assertIsNone(history.mcp_execution_mode)
        self.assertIsNone(history.mcp_rollout_mode)

    def test_assignment_is_write_once_but_mutable_task_fields_can_change(self) -> None:
        task = _assigned_task()
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_task(task)
            session.commit()

        for changed in (
            replace(task, mcp_execution_mode="user_scoped"),
            replace(
                task,
                mcp_execution_mode=None,
                mcp_shadow_enabled=None,
                mcp_rollout_config_version=None,
                mcp_route_reason_code=None,
                mcp_rollout_mode=None,
            ),
        ):
            with self.session_factory() as session:
                with self.assertRaisesRegex(ValueError, "mcp_task_route_assignment_immutable"):
                    SQLiteStateRepository(session).save_task(changed)

    def test_legacy_all_null_assignment_cannot_become_executable(self) -> None:
        legacy = replace(
            _assigned_task("task-legacy-null"),
            mcp_execution_mode=None,
            mcp_shadow_enabled=None,
            mcp_rollout_config_version=None,
            mcp_route_reason_code=None,
            mcp_rollout_mode=None,
        )
        with self.session_factory() as session:
            SQLiteStateRepository(session).save_task(legacy)
            session.commit()

        with self.session_factory() as session:
            with self.assertRaisesRegex(ValueError, "mcp_task_route_assignment_migration_required"):
                SQLiteStateRepository(session).save_task(
                    replace(
                        legacy,
                        mcp_execution_mode="user_scoped",
                        mcp_shadow_enabled=False,
                        mcp_rollout_config_version="mcp-rollout-v1:new",
                        mcp_route_reason_code="enforce_selected",
                        mcp_rollout_mode="enforce",
                    )
                )

    def test_sidecar_save_uses_full_snapshot_hash_and_get_is_authoritative(self) -> None:
        task = _assigned_task("task-sidecar")
        sidecar = _TaskSidecar()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            saved = asyncio.run(storage.save_task(task))
            loaded = asyncio.run(storage.get_task(task.task_id))

        self.assertEqual(saved, task)
        self.assertEqual(loaded, task)
        operation, submit = sidecar.calls[1]
        self.assertEqual(operation, "task_submit")
        snapshot = submit["task"]
        assert isinstance(snapshot, dict)
        self.assertEqual(
            set(snapshot),
            {
                "task_id",
                "conversation_id",
                "root_message_id",
                "status",
                "routing_mode",
                "requested_capability_id",
                "root_node_id",
                "summary",
                "cancel_requested_at",
                "created_at",
                "updated_at",
                "assignment",
            },
        )
        assignment = snapshot["assignment"]
        assert isinstance(assignment, dict)
        self.assertEqual(
            assignment,
            {
                "route_mode": "shadow",
                "real_path": "legacy",
                "shadow_path": "user_scoped",
                "config_version": "mcp-rollout-v1:abc123",
                "reason_code": "shadow_enabled",
            },
        )
        digest = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(submit["idempotency_key"], f"{task.task_id}:{digest}")
        self.assertEqual(sidecar.calls[2], ("task_get", {"task_id": task.task_id}))
        self.assertIsNone(asyncio.run(SQLiteStorage(self.session_factory).get_task(task.task_id)))

    def test_enforce_get_does_not_fall_back_to_python_store(self) -> None:
        task = _assigned_task("task-python-only")
        asyncio.run(SQLiteStorage(self.session_factory).save_task(task))
        sidecar = _TaskSidecar()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            self.assertIsNone(asyncio.run(storage.get_task(task.task_id)))

        self.assertEqual(sidecar.calls, [("task_get", {"task_id": task.task_id})])

    def test_shadow_get_compares_python_and_sidecar_snapshots(self) -> None:
        task = _assigned_task("task-shadow-get")
        sidecar = _TaskSidecar()
        audit_events: list[dict[str, str]] = []
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=audit_events.append,
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "shadow"}):
            asyncio.run(storage.save_task(task))
            sidecar.calls.clear()
            audit_events.clear()
            loaded = asyncio.run(storage.get_task(task.task_id))

        self.assertEqual(loaded, task)
        self.assertEqual(sidecar.calls, [("task_get", {"task_id": task.task_id})])
        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0]["operation"], "task_get")
        self.assertEqual(audit_events[0]["rust_status"], "ok")
        self.assertEqual(
            audit_events[0]["legacy_output_fingerprint"],
            audit_events[0]["rust_output_fingerprint"],
        )

    def test_shared_sidecar_validator_rejects_open_task_and_assignment_values(self) -> None:
        task = _TaskSidecar()
        snapshot = {
            "task_id": "task-invalid",
            "conversation_id": "conv-1",
            "root_message_id": "msg-1",
            "status": "invented",
            "routing_mode": "auto",
            "requested_capability_id": None,
            "root_node_id": None,
            "summary": None,
            "cancel_requested_at": None,
            "created_at": None,
            "updated_at": None,
            "assignment": None,
        }
        task.tasks["task-invalid"] = snapshot
        response = task.get_task(task_id="task-invalid")
        with self.assertRaisesRegex(RuntimeError, "runtime_store_response_invalid"):
            validate_runtime_sidecar_response("task_get", response)

        snapshot["status"] = "accepted"
        snapshot["assignment"] = {
            "route_mode": "shadow",
            "real_path": "legacy",
            "shadow_path": "user_scoped",
            "config_version": "config-v1",
            "reason_code": "open_string",
        }
        response = task.get_task(task_id="task-invalid")
        with self.assertRaisesRegex(RuntimeError, "runtime_store_response_invalid"):
            validate_runtime_sidecar_response("task_get", response)
