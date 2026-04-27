from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

from sqlalchemy import text


@dataclass(slots=True, frozen=True)
class ReadonlyQueryResult:
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    row_count: int


class TransientReadonlyExecutionError(RuntimeError):
    """Raised for transient database errors that can be retried."""


class MySQLReadonlyAdapter:
    def __init__(
        self,
        *,
        runner: Callable[[str], ReadonlyQueryResult] | None = None,
        engine_factory: Callable[[], Any] | None = None,
        transient_retries: int = 1,
    ) -> None:
        self._runner = runner
        self._engine_factory = engine_factory
        self._transient_retries = transient_retries
        self._engine: Any | None = None
        self._engine_lock = Lock()

    async def execute_readonly(self, sql: str, *, guard_pass_token: str | None) -> ReadonlyQueryResult:
        if not guard_pass_token:
            raise PermissionError("guard_pass_token is required before readonly SQL execution.")
        attempt = 0
        while True:
            try:
                return await asyncio.to_thread(self._execute_sync, sql)
            except TransientReadonlyExecutionError:
                attempt += 1
                if attempt > self._transient_retries:
                    raise

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    def close(self) -> None:
        with self._engine_lock:
            engine = self._engine
            self._engine = None
        if engine is not None:
            engine.dispose()

    def _execute_sync(self, sql: str) -> ReadonlyQueryResult:
        if self._runner is not None:
            return self._runner(sql)

        engine = self._get_engine()
        with engine.connect() as connection:
            result = connection.execute(text(sql))
            rows = tuple(dict(row._mapping) for row in result)
            columns = tuple(result.keys())
        return ReadonlyQueryResult(columns=columns, rows=rows, row_count=len(rows))

    def _get_engine(self) -> Any:
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            self._engine = self._build_engine()
            return self._engine

    def _build_engine(self) -> Any:
        if self._engine_factory is not None:
            return self._engine_factory()
        try:
            from src.mysql_engine import build_sql_engine

            return build_sql_engine()
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
            raise RuntimeError("mysql readonly execution requires the configured SQLAlchemy MySQL driver.") from exc
