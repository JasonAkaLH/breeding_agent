from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool


def build_sql_engine() -> object:
    return create_engine(
        "MAF_MYSQL_READONLY_URL",
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
