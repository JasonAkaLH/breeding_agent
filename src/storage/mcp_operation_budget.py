"""A dispatch claim's remaining window, propagated into its storage thread.

Limits are scoped to an individual operation. Cancellation never proves rollback;
the caller must stop dispatch and let durable recovery inspect receipts.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable
from time import monotonic

from sqlalchemy import event


_deadline: ContextVar[float | Callable[[], float | None] | None] = ContextVar(
    "mcp_operation_deadline", default=None
)


@contextmanager
def mcp_operation_budget(deadline: float | Callable[[], float | None]):
    token = _deadline.set(deadline)
    try:
        check_mcp_operation_budget()
        yield
    finally:
        _deadline.reset(token)


def _current_deadline() -> float | None:
    deadline = _deadline.get()
    return deadline() if callable(deadline) else deadline


@contextmanager
def capture_mcp_operation_budget():
    """Freeze an execution's current window before queuing a storage thread."""
    token = _deadline.set(_current_deadline())
    try:
        yield
    finally:
        _deadline.reset(token)


def check_mcp_operation_budget() -> int | None:
    deadline = _current_deadline()
    if deadline is None:
        return None
    remaining = int((deadline - monotonic()) * 1000)
    if remaining <= 0:
        raise TimeoutError("mcp_dispatch_storage_deadline")
    return remaining


def mcp_operation_budget_active() -> bool:
    return _deadline.get() is not None


def install_mcp_operation_budget(engine) -> None:
    if event.contains(engine, "before_cursor_execute", _before_statement):
        return
    event.listen(engine, "before_cursor_execute", _before_statement)
    event.listen(engine, "commit", _before_commit)
    event.listen(engine, "checkin", _restore_sqlite_timeout)


def _before_statement(connection, cursor, statement, parameters, context, executemany):
    remaining = check_mcp_operation_budget()
    if remaining is None:
        return
    if connection.dialect.name == "postgresql":
        # Raw cursor avoids recursively entering the SQLAlchemy statement hook.
        # Transaction-local values disappear on commit/rollback.
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true), "
            "set_config('lock_timeout', %s, true)",
            (str(remaining), str(remaining)),
        )
    elif connection.dialect.name == "sqlite":
        if "mcp_previous_busy_timeout" not in connection.info:
            connection.info["mcp_previous_busy_timeout"] = cursor.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0]
        cursor.execute(f"PRAGMA busy_timeout={remaining}")


def _before_commit(connection):
    check_mcp_operation_budget()


def _restore_sqlite_timeout(dbapi_connection, connection_record):
    previous = connection_record.info.pop("mcp_previous_busy_timeout", None)
    if previous is not None and dbapi_connection is not None:
        dbapi_connection.execute(f"PRAGMA busy_timeout={previous}")
