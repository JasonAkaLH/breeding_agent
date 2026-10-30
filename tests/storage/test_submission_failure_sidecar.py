from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.api.runtime import ApiRuntime
from src.api.sse import InMemoryEventBroker
from src.core.enums import TaskStatus
from src.core.models import (
    SubmissionClaimRequest,
    SubmissionFailureRequest,
    SubmissionProjectionAcknowledgementRequest,
    TaskNode,
)
from src.orchestration.agent_loop.models import (
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)
from src.storage.runtime_sidecar_agent_repository import RuntimeSidecarAgentRepository
from src.storage.sqlite.repositories import SQLiteStorage
from tests.integrations.test_runtime_sidecar_grpc_client import (
    _connect_with_retry,
    _ensure_runtime_sidecar_binary,
    _free_loopback_port,
    _repo_root,
    _terminate_process,
)
from tests.storage.support import SQLiteStorageTestCase
from tests.storage.test_submission_admission_sqlite import _request


class SubmissionFailureSidecarIntegrationTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        port = _free_loopback_port()
        sidecar_db = Path(self._tmpdir.name) / "sidecar.sqlite"
        self.process = subprocess.Popen(
            [
                str(_ensure_runtime_sidecar_binary()),
                "--serve",
                f"127.0.0.1:{port}",
                "--sqlite",
                str(sidecar_db),
            ],
            cwd=_repo_root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.client = _connect_with_retry(
                f"http://127.0.0.1:{port}", process=self.process
            )
            # Isolated empty-authority fixture, matching the existing gRPC integration gate.
            with closing(sqlite3.connect(sidecar_db)) as connection:
                connection.execute(
                    "UPDATE submission_authority_meta SET state='finalized', finalization_receipt_sha256=?, finalization_receipt_json=?, finalized_at_ms=1 WHERE singleton_key=1",
                    ("f" * 64, b"{}"),
                )
                connection.commit()
        except BaseException:
            _terminate_process(self.process)
            raise

    def tearDown(self) -> None:
        _terminate_process(self.process)
        super().tearDown()

    def _storage(self) -> SQLiteStorage:
        return SQLiteStorage(
            self.session_factory,
            mcp_task_authority_mode="enforce",
            runtime_sidecar_client=self.client,
        )

    def _runtime(self) -> ApiRuntime:
        runtime = object.__new__(ApiRuntime)
        runtime.storage = self._storage()
        runtime.agent_run_repository = RuntimeSidecarAgentRepository(self.client)
        runtime.event_broker = InMemoryEventBroker()
        runtime._lock = asyncio.Lock()
        return runtime

    async def _admit(self, storage, request):
        admitted = await storage.admit_submission(request)
        await storage.acknowledge_submission_projection(
            SubmissionProjectionAcknowledgementRequest(
                handle=admitted.handle,
                projection_sha256=admitted.record.projection_sha256,
                acknowledged_at=request.message_created_at,
            )
        )
        return admitted

    def test_failed_close_is_readable_and_recovers_after_callback_crash(self) -> None:
        async def scenario():
            storage = self._storage()
            request = _request()
            admitted = await self._admit(storage, request)
            await storage.save_task_node(
                TaskNode(
                    node_id=f"{request.task.task_id}:file_selection",
                    task_id=request.task.task_id,
                    capability_id="agent.file_selection",
                    status="running",
                )
            )
            self.assertTrue(
                (
                    await storage.fail_submission(
                        SubmissionFailureRequest(
                            handle=admitted.handle,
                            failed_at=request.message_created_at + timedelta(seconds=1),
                        )
                    )
                ).failed
            )
            task = await storage.get_task(request.task.task_id)
            self.assertEqual(task.status, TaskStatus.FAILED)
            lookup = dict(
                username=request.username,
                conversation_id=request.conversation_id,
                task_id=request.task.task_id,
            )
            self.assertFalse(self.client.get_submission_preparation(**lookup)["found"])
            self.assertTrue(
                self.client.get_submission_preparation(**lookup, include_closed=True)[
                    "admission"
                ]["closed"]
            )
            self.assertFalse(
                self.client.get_submission_preparation(
                    **{**lookup, "username": "foreign"}, include_closed=True
                )["found"]
            )
            self.assertEqual(
                task.updated_at, request.message_created_at + timedelta(seconds=1)
            )
            node = await storage.get_task_node(f"{task.task_id}:file_selection")
            self.assertEqual(str(node.status), "failed")
            self.assertEqual(node.finished_at, task.updated_at)
            pending = await storage.list_pending_submission_failure_projections()
            self.assertEqual([item.task_id for item in pending], [task.task_id])
            with self.assertRaisesRegex(RuntimeError, "identity_mismatch"):
                await storage.complete_submission_failure_projection(
                    replace(pending[0], username="foreign")
                )
            message = await storage.get_message(request.message_id)
            self.assertEqual(message.metadata, {"visible": "yes"})
            await storage.save_message(message)
            self.assertEqual(
                await storage.list_pending_submission_failure_projections(), pending
            )
            runtime = self._runtime()
            await runtime._recover_submission_failure_projections()
            self.assertEqual(
                await storage.list_pending_submission_failure_projections(), []
            )
            self.assertIsNone(
                (
                    await storage.get_conversation(request.conversation_id)
                ).current_task_id
            )
            events = await storage.list_events_for_task(task.task_id)
            self.assertEqual(
                [(event.event_id, event.event_type) for event in events],
                [(f"evt-submission-failed:{task.task_id}", "task.failed")],
            )
            # A repeated closed reply must not release a later task's conversation pointer.
            next_request = _request(message_id="next-message", task_id="next-task")
            await self._admit(storage, next_request)
            await runtime.materialize_submission_failure(admitted.record)
            self.assertEqual(
                (
                    await storage.get_conversation(request.conversation_id)
                ).current_task_id,
                "next-task",
            )
            self.assertEqual(len(await storage.list_events_for_task(task.task_id)), 1)

        asyncio.run(scenario())

    def test_lost_close_response_keeps_a_recoverable_failure_marker(self) -> None:
        async def scenario():
            storage = self._storage()
            request = _request()
            admitted = await self._admit(storage, request)
            original = self.client.fail_submission

            def lose_response(**kwargs):
                original(**kwargs)
                raise TimeoutError("close response lost after authority commit")

            with patch.object(
                self.client, "fail_submission", side_effect=lose_response
            ):
                with self.assertRaises(TimeoutError):
                    await storage.fail_submission(
                        SubmissionFailureRequest(
                            handle=admitted.handle, failed_at=request.message_created_at
                        )
                    )
            self.assertEqual(
                len(await storage.list_pending_submission_failure_projections()), 1
            )
            await self._runtime()._recover_submission_failure_projections()
            self.assertEqual(
                await storage.list_pending_submission_failure_projections(), []
            )
            self.assertEqual(
                (await storage.get_task(request.task.task_id)).status, TaskStatus.FAILED
            )
            self.assertEqual(
                len(await storage.list_events_for_task(request.task.task_id)), 1
            )
            replay = await storage.admit_submission(request)
            self.assertEqual(replay.task_id, request.task.task_id)
            self.assertIsNone(replay.handle)

        asyncio.run(scenario())

    def test_recovery_retries_partial_materialization_without_duplicate_event(
        self,
    ) -> None:
        async def scenario():
            storage = self._storage()
            request = _request()
            admitted = await self._admit(storage, request)
            await storage.fail_submission(
                SubmissionFailureRequest(
                    handle=admitted.handle, failed_at=request.message_created_at
                )
            )
            runtime = self._runtime()
            with patch.object(
                runtime,
                "_clear_conversation_current_task",
                new=AsyncMock(side_effect=RuntimeError("crash after event")),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash after event"):
                    await runtime._recover_submission_failure_projections()
            self.assertEqual(
                len(await storage.list_pending_submission_failure_projections()), 1
            )
            await self._runtime()._recover_submission_failure_projections()
            self.assertEqual(
                await storage.list_pending_submission_failure_projections(), []
            )
            self.assertEqual(
                len(await storage.list_events_for_task(request.task.task_id)), 1
            )
            self.assertIsNone(
                (
                    await storage.get_conversation(request.conversation_id)
                ).current_task_id
            )

        asyncio.run(scenario())

    def test_uncommitted_close_does_not_relabel_an_unrelated_terminal_failure(
        self,
    ) -> None:
        async def scenario():
            storage = self._storage()
            request = _request()
            admitted = await self._admit(storage, request)
            with patch.object(
                self.client,
                "fail_submission",
                side_effect=TimeoutError("no close committed"),
            ):
                with self.assertRaises(TimeoutError):
                    await storage.fail_submission(
                        SubmissionFailureRequest(
                            handle=admitted.handle, failed_at=request.message_created_at
                        )
                    )
            wire = self.client.get_task(task_id=request.task.task_id)["task"]
            self.client.submit_task(
                task_id=request.task.task_id,
                conversation_id=request.conversation_id,
                idempotency_key="other-terminal-failure",
                expected_from_status="accepted",
                task={**wire, "status": "failed", "updated_at": "2026-08-26T00:00:01Z"},
            )
            await self._runtime()._recover_submission_failure_projections()
            self.assertEqual(
                await storage.list_pending_submission_failure_projections(), []
            )
            self.assertEqual(
                await storage.list_events_for_task(request.task.task_id), []
            )

        asyncio.run(scenario())

    def test_unknown_failure_that_never_committed_does_not_kill_a_later_run(
        self,
    ) -> None:
        async def scenario():
            storage = self._storage()
            request = _request()
            admitted = await self._admit(storage, request)
            with patch.object(
                self.client,
                "fail_submission",
                side_effect=TimeoutError("request did not reach authority"),
            ):
                with self.assertRaises(TimeoutError):
                    await storage.fail_submission(
                        SubmissionFailureRequest(
                            handle=admitted.handle, failed_at=request.message_created_at
                        )
                    )
            runtime = self._runtime()
            await runtime._recover_submission_failure_projections()
            self.assertEqual(
                len(await storage.list_pending_submission_failure_projections()), 1
            )
            self.assertEqual(
                (await storage.get_task(request.task.task_id)).status,
                TaskStatus.ACCEPTED,
            )
            await runtime.agent_run_repository.create_run(
                AgentRun(
                    run_id=f"agent-run:{request.task.task_id}",
                    task_id=request.task.task_id,
                    conversation_id=request.conversation_id,
                    status=AgentRunStatus.RUNNING,
                    binding=AgentModelBinding("test"),
                )
            )
            await runtime._recover_submission_failure_projections()
            self.assertEqual(
                await storage.list_pending_submission_failure_projections(), []
            )
            self.assertEqual(
                (await storage.get_task(request.task.task_id)).status,
                TaskStatus.ACCEPTED,
            )
            self.assertEqual(
                await storage.list_events_for_task(request.task.task_id), []
            )

        asyncio.run(scenario())

    def test_stale_sidecar_projection_does_not_restore_old_conversation_pointer(
        self,
    ) -> None:
        async def scenario():
            original = self._storage()
            recovery = self._storage()
            request = _request()
            old = await self._admit(original, request)
            later = request.claim_expires_at + timedelta(seconds=1)
            reclaimed = await recovery.claim_pending_submission(
                SubmissionClaimRequest(
                    claim_owner="recovery-worker",
                    now=later,
                    claim_expires_at=later + timedelta(seconds=30),
                )
            )
            await recovery.fail_submission(
                SubmissionFailureRequest(handle=reclaimed.handle, failed_at=later)
            )
            await self._admit(
                recovery, _request(message_id="next-message", task_id="next-task")
            )
            with self.assertRaises(RuntimeError):
                await original.acknowledge_submission_projection(
                    SubmissionProjectionAcknowledgementRequest(
                        handle=old.handle,
                        projection_sha256=old.record.projection_sha256,
                        acknowledged_at=later,
                    )
                )
            self.assertEqual(
                (
                    await recovery.get_conversation(request.conversation_id)
                ).current_task_id,
                "next-task",
            )

        asyncio.run(scenario())
