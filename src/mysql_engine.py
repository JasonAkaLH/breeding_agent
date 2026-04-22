import os

from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool


def build_sql_engine() -> object:
    user = os.getenv("MYSQL_READONLY_USER", "readonly_user")
    password = os.getenv("MYSQL_READONLY_PASSWORD", "readonly_password")
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = os.getenv("MYSQL_PORT", "3306")
    database = os.getenv("MYSQL_DATABASE", "chatudb")

    return create_engine(
        f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4",
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
        connect_args={
            "connect_timeout": 10,
            "read_timeout": 30,
        },
        execution_options={"timeout": 60},
    )
