from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import json
from threading import Lock
from typing import Any

from sqlalchemy import text

from .rust_safety_contract import ensure_readonly_sql, resource_limit, validate_data_access_shape


@dataclass(slots=True, frozen=True)
class ReadonlyQueryResult:
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    row_count: int
    source_row_count: int | None = None
    row_limit_trimmed: bool = False
    row_limit: int | None = None
    row_limit_removed_row_count: int = 0
    truncated: bool = False


class TransientReadonlyExecutionError(RuntimeError):
    """Raised for transient database errors that can be retried."""


class ReadonlyQueryTimeoutError(TimeoutError):
    code = "data_access_deadline_exceeded"

    def __init__(self, deadline_ms: int) -> None:
        super().__init__(f"readonly query exceeded configured deadline of {deadline_ms} ms")
        self.deadline_ms = deadline_ms


class MySQLReadonlyAdapter:
    def __init__(
        self,
        *,
        runner: Callable[[str], ReadonlyQueryResult] | None = None,
        engine_factory: Callable[[], Any] | None = None,
        transient_retries: int = 1,
        deadline_ms: int | None = None,
    ) -> None:
        self._runner = runner
        self._engine_factory = engine_factory
        self._transient_retries = transient_retries
        self._deadline_ms = _clamped_deadline_ms(deadline_ms)
        self._engine: Any | None = None
        self._engine_lock = Lock()

    @property
    def deadline_ms(self) -> int:
        return self._deadline_ms

    async def execute_readonly(
        self,
        sql: str,
        *,
        guard_pass_token: str | None,
        row_retention: str = "tail",
    ) -> ReadonlyQueryResult:
        if not guard_pass_token:
            raise PermissionError("guard_pass_token is required before readonly SQL execution.")
        ensure_readonly_sql(sql)
        retention = row_retention if row_retention in {"head", "tail"} else "tail"
        attempt = 0
        while True:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._execute_sync, sql, retention),
                    timeout=self._deadline_ms / 1000,
                )
                result = _soft_trim_row_limit(result, row_retention=retention)
                _ensure_result_limits(result)
                return result
            except TimeoutError as exc:
                raise ReadonlyQueryTimeoutError(self._deadline_ms) from exc
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

    def _execute_sync(self, sql: str, row_retention: str = "tail") -> ReadonlyQueryResult:
        if self._runner is not None:
            return self._runner(sql)

        engine = self._get_engine()
        with engine.connect() as connection:
            result = connection.execute(text(sql))
            columns = tuple(result.keys())
            row_limit = resource_limit("db_row_limit")
            retained_rows: deque[dict[str, Any]] = deque(maxlen=row_limit if row_retention == "tail" else None)
            source_row_count = 0
            for row in result:
                source_row_count += 1
                if row_retention == "head" and len(retained_rows) >= row_limit:
                    continue
                retained_rows.append(dict(row._mapping))
            rows = tuple(retained_rows)
        return ReadonlyQueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            source_row_count=source_row_count,
            row_limit_trimmed=source_row_count > len(rows),
            row_limit=row_limit if source_row_count > len(rows) else None,
            row_limit_removed_row_count=max(source_row_count - len(rows), 0),
            truncated=source_row_count > len(rows),
        )

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


def _ensure_result_limits(result: ReadonlyQueryResult) -> None:
    result_size = len(
        json.dumps(
            {"columns": result.columns, "rows": result.rows},
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )
    validate_data_access_shape(
        row_count=len(result.rows),
        column_count=len(result.columns),
        result_bytes=result_size,
    )


def _soft_trim_row_limit(result: ReadonlyQueryResult, *, row_retention: str = "tail") -> ReadonlyQueryResult:
    row_limit = resource_limit("db_row_limit")
    rows = tuple(result.rows)
    source_row_count = getattr(result, "source_row_count", None)
    if source_row_count is None:
        source_row_count = max(result.row_count, len(rows))

    row_limit_trimmed = bool(getattr(result, "row_limit_trimmed", False))
    retained_rows = rows
    if len(rows) > row_limit:
        retained_rows = rows[:row_limit] if row_retention == "head" else rows[-row_limit:]
        row_limit_trimmed = True
    elif source_row_count > len(rows):
        row_limit_trimmed = True

    existing_removed = int(getattr(result, "row_limit_removed_row_count", 0) or 0)
    removed_row_count = (
        max(source_row_count - len(retained_rows), 0)
        if row_limit_trimmed
        else existing_removed
    )
    return ReadonlyQueryResult(
        columns=tuple(result.columns),
        rows=retained_rows,
        row_count=len(retained_rows),
        source_row_count=source_row_count,
        row_limit_trimmed=row_limit_trimmed,
        row_limit=row_limit if row_limit_trimmed else getattr(result, "row_limit", None),
        row_limit_removed_row_count=removed_row_count,
        truncated=bool(getattr(result, "truncated", False) or row_limit_trimmed),
    )


def _clamped_deadline_ms(deadline_ms: int | None) -> int:
    default_ms = resource_limit("db_deadline_ms")
    hard_cap_ms = resource_limit("db_hard_cap_ms")
    raw_ms = default_ms if deadline_ms is None else int(deadline_ms)
    return max(1, min(raw_ms, hard_cap_ms))
