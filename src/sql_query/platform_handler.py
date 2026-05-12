from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.integrations.codex_skills import SkillPlatformExecutionContext, SkillPlatformHandlerResult
from src.integrations.mysql_readonly import MySQLReadonlyAdapter

from .engine import SQLQueryEngine, SQLQueryEngineRequest


class SQLQueryPlatformHandler:
    def __init__(self, *, sql_generator: Callable[[dict[str, Any]], str] | None = None, trim_max_tokens: int | None = None) -> None:
        self._sql_generator = sql_generator
        self._trim_max_tokens = trim_max_tokens

    async def __call__(self, context: SkillPlatformExecutionContext) -> SkillPlatformHandlerResult:
        query = str(context.input_payload.get("query") or context.input_payload.get("user_message") or "").strip()
        if not query:
            from src.core.contracts import CapabilityExecutionError
            return SkillPlatformHandlerResult(
                output_payload={"domain_kind": "sql_query", "capability_id": "skill.sql_query"},
                error=CapabilityExecutionError(code="skill_input_missing", message="Missing SQLQuery question.", retriable=False),
            )
        mysql_adapter = context.services.get("mysql_readonly")
        if not isinstance(mysql_adapter, MySQLReadonlyAdapter):
            from src.core.contracts import CapabilityExecutionError
            return SkillPlatformHandlerResult(
                output_payload={"domain_kind": "sql_query", "capability_id": "skill.sql_query"},
                error=CapabilityExecutionError(code="skill_service_invalid", message="mysql_readonly service is invalid.", retriable=False),
            )
        llm_text_generator = context.services.get("llm.sql_query")
        engine = SQLQueryEngine(
            mysql_adapter=mysql_adapter,
            llm_text_generator=llm_text_generator,
            sql_generator=self._sql_generator,
            trim_max_tokens=self._trim_max_tokens,
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
            )
        )
        return SkillPlatformHandlerResult(
            output_payload=dict(result.output_payload),
            artifacts=result.artifacts,
            events=result.events,
            interrupt=result.interrupt,
            error=result.error,
        )


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
