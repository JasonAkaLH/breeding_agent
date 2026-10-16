from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

from src.api.runtime import ApiRuntime
from src.core.models import (
    SubmissionAdmissionDisposition,
    SubmissionProjectionAcknowledgementRequest,
)
from src.storage.knowledge_repository import KnowledgeRepository
from src.storage.sqlalchemy_models import TaskRow
from src.storage.sqlite import (
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.repositories import SQLiteStorage
from tests.integrations.test_runtime_sidecar_grpc_client import (
    _connect_with_retry,
    _free_loopback_port,
    _repo_root,
    _terminate_process,
)
from tests.storage.test_submission_admission_sqlite import _request


class KnowledgeScopeRealSidecarRecoveryTest(unittest.IsolatedAsyncioTestCase):
    """Exercise knowledge scope against actual off/shadow/enforce Task authority.

    This uses an existing debug binary and its real protocol handshake. The
    submission-finalization row is an isolated fixture, as in the existing gRPC
    integration tests; it is not production migration or cutover evidence.
    """

    async def asyncSetUp(self):
        self.binary = _repo_root() / "native/target/debug/maf-runtime-sidecar"
        if not self.binary.is_file():
            self.skipTest("verified_runtime_sidecar_debug_binary_not_available")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sidecar_path = self.root / "sidecar.sqlite"
        self.process = None
        self._start_sidecar()
        # Seed only this test's empty Sidecar authority, never an external store.
        with closing(sqlite3.connect(self.sidecar_path)) as connection:
            connection.execute(
                "UPDATE submission_authority_meta SET state='finalized', "
                "finalization_receipt_sha256=?, finalization_receipt_json=?, finalized_at_ms=1 "
                "WHERE singleton_key=1", ("f" * 64, b"{}"),
            )
            connection.commit()
        self.engine = create_sqlite_engine(self.root / "state.sqlite")
        self.sessions = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.repository = KnowledgeRepository(self.sessions)

    def _start_sidecar(self):
        port = _free_loopback_port()
        self.process = subprocess.Popen(
            [str(self.binary), "--serve", f"127.0.0.1:{port}", "--sqlite", str(self.sidecar_path)],
            cwd=_repo_root(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.client = _connect_with_retry(f"http://127.0.0.1:{port}", process=self.process)
        self.client.check_compatibility()

    async def asyncTearDown(self):
        if self.process is not None:
            _terminate_process(self.process)
        if hasattr(self, "engine"):
            self.engine.dispose()

    def _storage(self, mode):
        return SQLiteStorage(
            self.sessions, runtime_sidecar_client=self.client,
            runtime_sidecar_shadow_sink=lambda _event: None,
            mcp_task_authority_mode=mode,
        )

    async def _exercise_mode(self, mode):
        actor, conversation_id, message_id = "owner", f"scope-conversation-{mode}", f"scope-message-{mode}"
        first = self.repository.create_library(actor, "First")
        second = self.repository.create_library(actor, "Second")
        self.repository.save_selection(actor, conversation_id, "selected", [first["library_id"]])
        scope = self.repository.freeze_scope(actor, conversation_id, message_id)
        storage = self._storage(mode)
        conversation = await storage.get_conversation(conversation_id)
        request = _request(
            username=actor, conversation_id=conversation_id, message_id=message_id,
            task_id=f"scope-task-{mode}", now=conversation.created_at.replace(tzinfo=timezone.utc),
        )
        admitted = await storage.admit_submission(request)
        self.assertEqual(admitted.disposition, SubmissionAdmissionDisposition.CREATED)
        await storage.acknowledge_submission_projection(SubmissionProjectionAcknowledgementRequest(
            admitted.handle, request.projection_sha256, request.message_created_at,
        ))
        self.repository.save_selection(actor, conversation_id, "selected", [second["library_id"]])
        self.assertEqual(self.repository.freeze_scope(actor, conversation_id, message_id), scope)
        # Restart the actual server and reconstruct Python storage, retaining only durable data.
        _terminate_process(self.process)
        self._start_sidecar()
        restarted = self._storage(mode)
        canonical_task = await restarted.get_task(request.task.task_id)
        self.assertIsNotNone(canonical_task)
        self.assertEqual(canonical_task.root_message_id, message_id)
        runtime = object.__new__(ApiRuntime)
        runtime.storage, runtime.knowledge_repository = restarted, self.repository
        restored = await runtime._knowledge_scope_for_run(SimpleNamespace(
            task_id=canonical_task.task_id, conversation_id=conversation_id,
        ))
        self.assertEqual(restored["candidate_library_ids"], [first["library_id"]])
        self.assertEqual(restored["root_message_id"], message_id)
        self.assertEqual(restored["actor"], actor)
        replay = await restarted.admit_submission(request)
        self.assertEqual(replay.disposition, SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY)
        self.assertEqual(replay.task_id, canonical_task.task_id)
        with self.sessions() as session:
            sql_task = session.get(TaskRow, canonical_task.task_id)
            self.assertEqual(sql_task is None, mode == "enforce")
        next_scope = self.repository.freeze_scope(actor, conversation_id, f"next-{message_id}")
        self.assertEqual(next_scope["candidate_library_ids"], [second["library_id"]])

    async def test_off_scope_survives_storage_and_sidecar_restart(self):
        await self._exercise_mode("off")

    async def test_shadow_scope_survives_storage_and_sidecar_restart(self):
        await self._exercise_mode("shadow")

    async def test_enforce_scope_survives_restart_without_sql_task_projection(self):
        await self._exercise_mode("enforce")
