from __future__ import annotations

from src.storage.sqlite.agent_repository import SQLiteAgentRepository


class PostgreSQLAgentRepository(SQLiteAgentRepository):
    """PostgreSQL binding of the shared SQLAlchemy Agent transaction contract."""
