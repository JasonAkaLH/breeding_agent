from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, MetaData, Text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class SQLiteBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JSONText(TypeDecorator):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql" or not isinstance(value, str):
            return value
        return json.loads(value)


class DateTimeText(TypeDecorator):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: datetime | None, dialect: Any) -> Any:
        if value is None:
            return None
        if value.utcoffset() is not None:
            raise ValueError("DateTimeText requires a UTC-naive datetime")
        if dialect.name == "postgresql":
            return value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            return parsed
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)


class AwareUTCDateTimeText(DateTimeText):
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> Any:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("AwareUTCDateTimeText requires a timezone-aware datetime")
        normalized = value.astimezone(timezone.utc)
        if dialect.name == "postgresql":
            return normalized
        return normalized.isoformat()

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("AwareUTCDateTimeText requires a timezone-aware datetime")
        return parsed.astimezone(timezone.utc)
