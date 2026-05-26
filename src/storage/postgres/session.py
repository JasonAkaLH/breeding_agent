from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_postgres_engine(
    dsn: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_pre_ping: bool = True,
) -> Engine:
    if not dsn.strip():
        raise ValueError("PostgreSQL DSN is required")
    return create_engine(
        dsn,
        echo=echo,
        future=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=pool_pre_ping,
        hide_parameters=True,
    )


def create_postgres_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
