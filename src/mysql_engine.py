from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.pool import QueuePool

from src.integrations.llm_client import load_config


MYSQL_READONLY_URL_ENV = "MAF_MYSQL_READONLY_URL"
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 10
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_READ_TIMEOUT = 30
DEFAULT_EXECUTION_TIMEOUT = 60


def build_sql_engine(config: Mapping[str, Any] | None = None) -> object:
    mysql_config = _resolve_mysql_config(config)
    connection_url = _resolve_connection_url(mysql_config)
    if not connection_url:
        raise RuntimeError(
            "Missing MySQL readonly database config. Set mysql_readonly.url in local config.yaml "
            f"or {MYSQL_READONLY_URL_ENV} in the process environment."
        )

    return create_engine(
        connection_url,
        poolclass=QueuePool,
        pool_size=_int_config(mysql_config, "pool_size", DEFAULT_POOL_SIZE),
        max_overflow=_int_config(mysql_config, "max_overflow", DEFAULT_MAX_OVERFLOW),
        pool_pre_ping=True,
        echo=False,
        connect_args={
            "connect_timeout": _int_config(mysql_config, "connect_timeout", DEFAULT_CONNECT_TIMEOUT),
            "read_timeout": _int_config(mysql_config, "read_timeout", DEFAULT_READ_TIMEOUT),
        },
        execution_options={"timeout": _int_config(mysql_config, "execution_timeout", DEFAULT_EXECUTION_TIMEOUT)},
    )


def _resolve_mysql_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    loaded_config = dict(config) if config is not None else load_config()
    for key in ("mysql_readonly", "mysql", "database"):
        section = loaded_config.get(key)
        if isinstance(section, Mapping):
            return section
    return loaded_config


def _resolve_connection_url(mysql_config: Mapping[str, Any]) -> str | URL | None:
    env_url = os.environ.get(MYSQL_READONLY_URL_ENV)
    if env_url:
        return env_url
    for key in ("url", "connection_url", "dsn", "database_url"):
        value = mysql_config.get(key)
        if value:
            return str(value)

    host = mysql_config.get("host")
    database = mysql_config.get("database") or mysql_config.get("database_name") or mysql_config.get("name")
    username = mysql_config.get("username") or mysql_config.get("user")
    password = mysql_config.get("password")
    if not host or not database or not username:
        return None

    return URL.create(
        drivername=str(mysql_config.get("driver") or "mysql+pymysql"),
        username=str(username),
        password=None if password is None else str(password),
        host=str(host),
        port=_optional_int(mysql_config.get("port")),
        database=str(database),
        query=_query_config(mysql_config),
    )


def _query_config(mysql_config: Mapping[str, Any]) -> dict[str, str]:
    query = mysql_config.get("query")
    if isinstance(query, Mapping):
        return {str(key): str(value) for key, value in query.items() if value is not None}
    charset = mysql_config.get("charset")
    return {"charset": str(charset)} if charset else {}


def _int_config(config: Mapping[str, Any], key: str, default: int) -> int:
    parsed = _optional_int(config.get(key))
    return parsed if parsed is not None else default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
