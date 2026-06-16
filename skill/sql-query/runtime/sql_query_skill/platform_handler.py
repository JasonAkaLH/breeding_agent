from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.contracts import CapabilityExecutionError
from src.integrations.codex_skills import SkillPlatformExecutionContext, SkillPlatformHandlerResult
from src.integrations.mysql_readonly import MySQLReadonlyAdapter

from .engine import SQLQueryEngine, SQLQueryEngineRequest

SQL_QUERY_PUBLIC_INTERNAL_ERROR_MESSAGE = "服务器内部错误，请稍后重试。"


class SQLQueryPlatformHandler:
    def __init__(self, *, sql_generator: Callable[[dict[str, Any]], str] | None = None, trim_max_tokens: int | None = None) -> None:
        self._sql_generator = sql_generator
        self._trim_max_tokens = trim_max_tokens

    async def __call__(self, context: SkillPlatformExecutionContext) -> SkillPlatformHandlerResult:
        query = str(context.input_payload.get("query") or context.input_payload.get("user_message") or "").strip()
        if not query:
            return SkillPlatformHandlerResult(
                output_payload={"domain_kind": "sql_query", "capability_id": "skill.sql_query"},
                error=CapabilityExecutionError(code="skill_input_missing", message="Missing SQLQuery question.", retriable=False),
            )
        mysql_adapter = context.services.get("mysql_readonly")
        if not isinstance(mysql_adapter, MySQLReadonlyAdapter):
            return SkillPlatformHandlerResult(
                output_payload={"domain_kind": "sql_query", "capability_id": "skill.sql_query"},
                error=CapabilityExecutionError(code="skill_service_invalid", message="mysql_readonly service is invalid.", retriable=False),
            )
        llm_text_generator = context.services.get("llm.non_stream")
        progress_event_recorder = context.services.get("progress_events")
        engine = SQLQueryEngine(
            mysql_adapter=mysql_adapter,
            llm_text_generator=llm_text_generator,
            sql_generator=self._sql_generator,
            trim_max_tokens=self._trim_max_tokens,
            progress_event_recorder=progress_event_recorder if callable(progress_event_recorder) else None,
        )
        result = await engine.execute(
            SQLQueryEngineRequest(
                query=query,
                conversation_id=context.conversation_id,
                task_id=context.task_id,
                node_id=context.node_id,
                metadata=context.safe_metadata,
                subtask_label=_optional_str(context.input_payload.get("subtask_label")),
                parent_question=_optional_str(context.input_payload.get("parent_question")),
                skill_name=context.manifest.name if context.manifest is not None else "sql-query",
            )
        )
        return SkillPlatformHandlerResult(
            output_payload=_public_output(result.output_payload, result.error),
            artifacts=result.artifacts,
            events=result.events,
            interrupt=result.interrupt,
            error=_public_error(result.error),
        )


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _public_error(error: CapabilityExecutionError | None) -> CapabilityExecutionError | None:
    if error is None:
        return None
    if error.code == "skill_input_missing":
        return error
    return CapabilityExecutionError(
        code=error.code,
        message=SQL_QUERY_PUBLIC_INTERNAL_ERROR_MESSAGE,
        retriable=False,
        metadata={
            "domain_kind": "sql_query",
            "capability_id": "skill.sql_query",
            "public_message": SQL_QUERY_PUBLIC_INTERNAL_ERROR_MESSAGE,
            "retriable": False,
        },
    )


def _public_output(output_payload: Any, error: CapabilityExecutionError | None) -> dict[str, Any]:
    payload = dict(output_payload or {})
    payload.setdefault("domain_kind", "sql_query")
    payload.setdefault("capability_id", "skill.sql_query")
    if error is None:
        return payload
    payload["summary"] = SQL_QUERY_PUBLIC_INTERNAL_ERROR_MESSAGE
    payload.setdefault(
        "filtered_query_result",
        {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "filter_source": "error",
        },
    )
    payload["error"] = {
        "code": error.code,
        "message": SQL_QUERY_PUBLIC_INTERNAL_ERROR_MESSAGE,
        "type": "internal_error",
    }
    return payload


def build_handler() -> SQLQueryPlatformHandler:
    return SQLQueryPlatformHandler(trim_max_tokens=_trim_max_tokens_from_env())


def _trim_max_tokens_from_env() -> int | None:
    import os

    raw = os.environ.get("MAF_CONFIG_TRIM_MAX_TOKENS")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
