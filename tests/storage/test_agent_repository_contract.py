from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.orchestration.agent_loop.models import AgentStorageConflict
from src.storage.postgres import PostgreSQLAgentRepository
from src.storage.postgres.agent_repository import (
    PostgreSQLAgentRepository as PostgreSQLAgentRepositoryDefinition,
)
from src.storage.runtime_sidecar_agent_repository import RuntimeSidecarAgentRepository
from src.storage.sqlite import (
    SQLiteAgentRepository,
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.agent_repository import (
    SQLiteAgentRepository as SQLiteAgentRepositoryDefinition,
)


COMMON_OPERATIONS = frozenset(
    {
        "cancel_agent_run",
        "commit_agent_call_outcome",
        "commit_agent_compaction",
        "commit_agent_final_output",
        "commit_agent_sample",
        "commit_agent_user_message",
        "create_run",
        "create_terminal_run",
        "fail_agent_run",
        "get_run",
        "get_run_for_task",
        "list_items",
        "list_recoverable_runs",
        "reconcile_agent_run_consistency",
    }
)
SQL_ONLY_TASK_LEASE_OPERATIONS = frozenset(
    {
        "acquire_task_lease",
        "release_waiting_task_lease",
        "renew_task_lease",
    }
)
SQL_OPERATIONS = COMMON_OPERATIONS | SQL_ONLY_TASK_LEASE_OPERATIONS

EXPECTED_COMMON_SIGNATURES = {
    "cancel_agent_run": "(self, run_id: 'str', *, expected_revision: 'int', expected_claim_token: 'str | None', safe_reason_code: 'str') -> 'AgentRun'",
    "commit_agent_call_outcome": "(self, commit: 'AgentCallOutcomeCommit') -> 'AgentItem'",
    "commit_agent_compaction": "(self, commit: 'AgentCompactionCommit') -> 'AgentCompactionResult'",
    "commit_agent_final_output": "(self, commit: 'AgentFinalOutputCommit') -> 'AgentFinalOutputResult'",
    "commit_agent_sample": "(self, commit: 'AgentSampleCommit') -> 'AgentSampleCommitResult'",
    "commit_agent_user_message": "(self, commit: 'AgentUserMessageCommit') -> 'AgentUserMessageCommitResult'",
    "create_run": "(self, run: 'AgentRun') -> 'AgentRun'",
    "create_terminal_run": "(self, run: 'AgentRun', *, task: 'Task') -> 'AgentRun'",
    "fail_agent_run": "(self, run_id: 'str', *, expected_revision: 'int', expected_claim_token: 'str | None', safe_error_code: 'str') -> 'AgentRun'",
    "get_run": "(self, run_id: 'str') -> 'AgentRun | None'",
    "get_run_for_task": "(self, task_id: 'str') -> 'AgentRun | None'",
    "list_items": "(self, run_id: 'str') -> 'tuple[AgentItem, ...]'",
    "list_recoverable_runs": "(self) -> 'tuple[AgentRun, ...]'",
    "reconcile_agent_run_consistency": "(self, run_id: 'str') -> 'AgentRun'",
}
EXPECTED_SQL_ONLY_SIGNATURES = {
    "acquire_task_lease": "(self, run_id: 'str', *, owner_id: 'str', ttl_seconds: 'float') -> 'AgentTaskLease'",
    "release_waiting_task_lease": "(self, run_id: 'str', *, owner_id: 'str', token: 'str') -> 'AgentRun'",
    "renew_task_lease": "(self, run_id: 'str', *, owner_id: 'str', token: 'str', ttl_seconds: 'float') -> 'AgentTaskLease'",
}


def _public_async_surface(repository_type: type) -> frozenset[str]:
    return frozenset(
        name
        for name in dir(repository_type)
        if not name.startswith("_")
        and inspect.iscoroutinefunction(getattr(repository_type, name))
    )


class _MissingSidecarClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_agent_run(self, *, run_id: str) -> dict[str, object]:
        self.calls.append("get_agent_run")
        return {"found": False, "run": None}

    def list_agent_items(self, *, run_id: str) -> dict[str, object]:
        self.calls.append("list_agent_items")
        return {"items": []}


class _CountingSessionFactory:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    def __call__(self):
        self.calls.append("session")
        return self._delegate()


class _TraceSession:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self):
        self._events.append("enter")
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        name = "None" if exc_type is None else exc_type.__name__
        self._events.append(f"exit:{name}")

    def get_bind(self):
        self._events.append("bind")
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def execute(self, statement) -> None:
        self._events.append(str(statement))

    def commit(self) -> None:
        self._events.append("commit")

    def rollback(self) -> None:
        self._events.append("rollback")


class _TraceSessionFactory:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __call__(self) -> _TraceSession:
        self._events.append("factory")
        return _TraceSession(self._events)


class AgentRepositoryPublicContractTest(unittest.TestCase):
    def test_import_paths_modules_constructors_and_mro_are_exact(self) -> None:
        self.assertIs(SQLiteAgentRepository, SQLiteAgentRepositoryDefinition)
        self.assertIs(PostgreSQLAgentRepository, PostgreSQLAgentRepositoryDefinition)
        self.assertEqual(
            SQLiteAgentRepository.__module__,
            "src.storage.sqlite.agent_repository",
        )
        self.assertEqual(
            PostgreSQLAgentRepository.__module__,
            "src.storage.postgres.agent_repository",
        )
        self.assertEqual(
            RuntimeSidecarAgentRepository.__module__,
            "src.storage.runtime_sidecar_agent_repository",
        )
        self.assertEqual(
            str(inspect.signature(SQLiteAgentRepository)),
            "(session_factory: 'sessionmaker[Session]', *, now_fn: 'Callable[[], datetime] | None' = None, fault_injector: 'FaultInjector | None' = None, token_factory: 'Callable[[], str] | None' = None) -> 'None'",
        )
        self.assertEqual(
            inspect.signature(PostgreSQLAgentRepository),
            inspect.signature(SQLiteAgentRepository),
        )
        self.assertEqual(
            str(inspect.signature(RuntimeSidecarAgentRepository)),
            "(client: 'RuntimeSidecarGrpcClient', *, now_fn: 'Callable[[], datetime] | None' = None) -> 'None'",
        )
        self.assertEqual(
            SQLiteAgentRepository.__mro__,
            (SQLiteAgentRepository, object),
        )
        self.assertEqual(
            PostgreSQLAgentRepository.__mro__,
            (PostgreSQLAgentRepository, SQLiteAgentRepository, object),
        )
        self.assertEqual(
            RuntimeSidecarAgentRepository.__mro__,
            (RuntimeSidecarAgentRepository, object),
        )

    def test_effective_async_surfaces_and_signatures_are_literal(self) -> None:
        self.assertEqual(_public_async_surface(SQLiteAgentRepository), SQL_OPERATIONS)
        self.assertEqual(_public_async_surface(PostgreSQLAgentRepository), SQL_OPERATIONS)
        self.assertEqual(
            _public_async_surface(RuntimeSidecarAgentRepository),
            COMMON_OPERATIONS,
        )
        for operation, expected in EXPECTED_COMMON_SIGNATURES.items():
            for repository_type in (
                SQLiteAgentRepository,
                PostgreSQLAgentRepository,
                RuntimeSidecarAgentRepository,
            ):
                with self.subTest(repository=repository_type.__name__, operation=operation):
                    self.assertEqual(
                        str(inspect.signature(getattr(repository_type, operation))),
                        expected,
                    )
        for operation, expected in EXPECTED_SQL_ONLY_SIGNATURES.items():
            for repository_type in (SQLiteAgentRepository, PostgreSQLAgentRepository):
                with self.subTest(repository=repository_type.__name__, operation=operation):
                    self.assertEqual(
                        str(inspect.signature(getattr(repository_type, operation))),
                        expected,
                    )

    def test_sidecar_task_lease_operations_remain_explicitly_unsupported(self) -> None:
        for operation in SQL_ONLY_TASK_LEASE_OPERATIONS:
            self.assertFalse(hasattr(RuntimeSidecarAgentRepository, operation))

    def test_persistence_adapters_do_not_own_mode_or_backend_selection(self) -> None:
        for repository_type in (
            SQLiteAgentRepository,
            PostgreSQLAgentRepository,
            RuntimeSidecarAgentRepository,
        ):
            source = inspect.getsource(repository_type)
            with self.subTest(repository=repository_type.__name__):
                self.assertNotIn("os.environ", source)
                self.assertNotIn("mode_for_component", source)
                self.assertNotIn("runtime_mode_for_component", source)
        sidecar_source = inspect.getsource(RuntimeSidecarAgentRepository).lower()
        self.assertNotIn("sqliteagentrepository", sidecar_source)
        self.assertNotIn("postgresqlagentrepository", sidecar_source)
        self.assertNotIn("session_factory", sidecar_source)


class AgentRepositoryBehaviorContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_common_missing_run_fixture_has_equal_semantics_and_explicit_backend_traces(
        self,
    ) -> None:
        results: dict[str, tuple[object, object, str]] = {}
        sql_traces: dict[str, list[str]] = {}
        with tempfile.TemporaryDirectory() as directory:
            for name, repository_type in (
                ("sqlite", SQLiteAgentRepository),
                ("postgres_subclass", PostgreSQLAgentRepository),
            ):
                engine = create_sqlite_engine(Path(directory) / f"{name}.sqlite3")
                try:
                    bootstrap_sqlite_database(engine)
                    factory = _CountingSessionFactory(
                        create_sqlite_session_factory(engine)
                    )
                    repository = repository_type(factory)
                    results[name] = await self._missing_run_trace(repository)
                    sql_traces[name] = factory.calls
                finally:
                    engine.dispose()

        client = _MissingSidecarClient()
        sidecar_repository = RuntimeSidecarAgentRepository(client)
        results["sidecar"] = await self._missing_run_trace(sidecar_repository)

        self.assertEqual(
            results,
            {
                "sqlite": (None, (), "agent_run_missing"),
                "postgres_subclass": (None, (), "agent_run_missing"),
                "sidecar": (None, (), "agent_run_missing"),
            },
        )
        self.assertEqual(sql_traces, {"sqlite": ["session"] * 3, "postgres_subclass": ["session"] * 3})
        self.assertEqual(
            client.calls,
            ["get_agent_run", "list_agent_items", "get_agent_run"],
        )

    async def test_sql_agent_write_owns_begin_commit_rollback_and_shield_boundary(self) -> None:
        success_events: list[str] = []
        repository = SQLiteAgentRepository(_TraceSessionFactory(success_events))

        def succeed(_session) -> str:
            success_events.append("callback")
            return "ok"

        self.assertEqual(await repository._write(succeed), "ok")
        self.assertEqual(
            success_events,
            ["factory", "enter", "bind", "BEGIN IMMEDIATE", "callback", "commit", "exit:None"],
        )

        failure_events: list[str] = []
        repository = SQLiteAgentRepository(_TraceSessionFactory(failure_events))

        def fail(_session) -> None:
            failure_events.append("callback")
            raise RuntimeError("expected-write-failure")

        with self.assertRaisesRegex(RuntimeError, "expected-write-failure"):
            await repository._write(fail)
        self.assertEqual(
            failure_events,
            [
                "factory",
                "enter",
                "bind",
                "BEGIN IMMEDIATE",
                "callback",
                "rollback",
                "exit:RuntimeError",
            ],
        )
        source = inspect.getsource(SQLiteAgentRepository._write)
        self.assertIn("asyncio.shield(asyncio.create_task(asyncio.to_thread(execute)))", source)

    async def test_storage_run_shares_one_session_and_one_commit_for_state_and_collaboration(
        self,
    ) -> None:
        events: list[str] = []
        storage = SQLiteStorage(_TraceSessionFactory(events))
        constructed: list[tuple[str, object]] = []

        class StateRepository:
            def __init__(self, session, **_kwargs) -> None:
                constructed.append(("state", session))

        class CollaborationRepository:
            def __init__(self, session) -> None:
                constructed.append(("collaboration", session))

        def callback(state, collaboration) -> str:
            events.append("callback")
            self.assertIsInstance(state, StateRepository)
            self.assertIsInstance(collaboration, CollaborationRepository)
            return "shared"

        with (
            patch(
                "src.storage.sqlite.repositories.SQLiteStateRepository",
                StateRepository,
            ),
            patch(
                "src.storage.sqlite.repositories.SQLiteCollaborationRepository",
                CollaborationRepository,
            ),
        ):
            self.assertEqual(await storage._run(callback), "shared")

        self.assertEqual([name for name, _session in constructed], ["state", "collaboration"])
        self.assertIs(constructed[0][1], constructed[1][1])
        self.assertEqual(
            events,
            ["factory", "enter", "bind", "BEGIN IMMEDIATE", "callback", "commit", "exit:None"],
        )
        source = inspect.getsource(SQLiteStorage._run)
        self.assertIn("worker = asyncio.create_task(asyncio.to_thread(_sync))", source)
        self.assertIn("return await asyncio.shield(worker)", source)
        self.assertIn("except asyncio.CancelledError:", source)
        self.assertIn("await worker", source)

    @staticmethod
    async def _missing_run_trace(repository) -> tuple[object, object, str]:
        run = await repository.get_run("missing-run")
        items = await repository.list_items("missing-run")
        try:
            await repository.reconcile_agent_run_consistency("missing-run")
        except AgentStorageConflict as exc:
            error = str(exc)
        else:  # pragma: no cover - contract failure path
            error = ""
        return run, items, error


if __name__ == "__main__":
    unittest.main()
