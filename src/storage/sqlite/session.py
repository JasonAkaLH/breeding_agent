from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_sqlite_engine(database_path: str | Path, *, echo: bool = False) -> Engine:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite+pysqlite:///{path}",
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False},
    )


def create_sqlite_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
