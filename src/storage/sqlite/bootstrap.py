from __future__ import annotations

from sqlalchemy import Engine

from .base import SQLiteBase


def bootstrap_sqlite_database(engine: Engine) -> None:
    SQLiteBase.metadata.create_all(engine)
