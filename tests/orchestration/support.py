from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.core.contracts import CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory


class OrchestrationSQLiteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "phase4-orchestration.sqlite3"
        self.engine = create_sqlite_engine(self.db_path)
        self.session_factory = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(self.session_factory)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()
        super().tearDown()


class FakeExecutor(ExecutorPort):
    def __init__(self, handlers: dict[str, Callable[[CapabilityExecutionRequest], CapabilityExecutionResult] | CapabilityExecutionResult]) -> None:
        self._handlers = handlers

    def supports(self, capability_id: str) -> bool:
        return capability_id in self._handlers

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        handler = self._handlers[request.capability_id]
        if callable(handler):
            return handler(request)
        return replace(handler, task_id=request.task_id, node_id=request.node_id, capability_id=request.capability_id)


def success_result(
    *,
    output_payload: dict[str, Any] | None = None,
) -> CapabilityExecutionResult:
    return CapabilityExecutionResult(
        capability_id="",
        task_id="",
        node_id="",
        output_payload=output_payload or {},
    )


def error_result(*, code: str, message: str) -> CapabilityExecutionResult:
    return CapabilityExecutionResult(
        capability_id="",
        task_id="",
        node_id="",
        error=CapabilityExecutionError(code=code, message=message),
    )
