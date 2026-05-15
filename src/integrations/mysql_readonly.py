from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
from threading import Lock
from typing import Any

from sqlalchemy import text

from .rust_safety_contract import resource_limit


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
        _ensure_readonly_sql(sql)
        attempt = 0
        while True:
            try:
                result = await asyncio.to_thread(self._execute_sync, sql)
                _ensure_result_limits(result)
                return result
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


def _ensure_readonly_sql(sql: str) -> None:
    normalized = sql.strip().lower()
    if not (normalized.startswith("select") or normalized.startswith("with")):
        raise PermissionError("SQL is not readonly.")
    padded = f" {normalized} "
    for forbidden in (
        " insert ",
        " update ",
        " delete ",
        " drop ",
        " alter ",
        " truncate ",
        " create ",
        " replace ",
        " grant ",
        " revoke ",
        " merge ",
        " call ",
    ):
        if forbidden in padded:
            raise PermissionError("SQL is not readonly.")


def _ensure_result_limits(result: ReadonlyQueryResult) -> None:
    row_limit = resource_limit("db_row_limit")
    column_limit = resource_limit("db_column_limit")
    result_bytes_limit = resource_limit("db_result_bytes")
    if result.row_count > row_limit or len(result.rows) > row_limit:
        raise RuntimeError("readonly query result exceeds row limit")
    if len(result.columns) > column_limit:
        raise RuntimeError("readonly query result exceeds column limit")
    result_size = len(json.dumps({"columns": result.columns, "rows": result.rows}, ensure_ascii=False, default=str).encode("utf-8"))
    if result_size > result_bytes_limit:
        raise RuntimeError("readonly query result exceeds byte limit")
