from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult

from .helpers import find_dependency_output, make_artifact, normalize_text


class NL2SQLSQLGenerateCapability(CapabilityContract):
    capability_id = "nl2sql.sql_generate"
    version = "1"
    description = "Generate a readonly SQL candidate from NL2SQL route + schema context."

    def __init__(self, *, generator: Callable[[dict[str, Any]], str] | None = None) -> None:
        self._generator = generator or self._generate_sql

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        context = find_dependency_output(request, ("selected_tables", "selected_columns", "user_question"))
        sql = self._generator(context)
        output = {
            "route_id": context.get("route_id"),
            "schema_profile_id": context.get("schema_profile_id"),
            "allowed_tables": list(context.get("allowed_tables", [])),
            "selected_tables": list(context.get("selected_tables", [])),
            "selected_columns": dict(context.get("selected_columns", {})),
            "sql": sql,
            "user_question": context.get("user_question"),
        }
        artifact = make_artifact(
            name="generated_sql",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=sql,
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
        )

    def _generate_sql(self, context: dict[str, Any]) -> str:
        selected_tables = list(context.get("selected_tables", []))
        selected_columns = dict(context.get("selected_columns", {}))
        join_hints = list(context.get("join_hints", []))
        user_question = normalize_text(context.get("user_question", ""))

        base_table = selected_tables[0]
        projected_columns: list[str] = []
        for table in selected_tables[:2]:
            for column in selected_columns.get(table, [])[:2]:
                if len(selected_tables) > 1:
                    projected_columns.append(f"{table}.{column}")
                else:
                    projected_columns.append(column)
        if not projected_columns:
            projected_columns = ["*"]

        if any(keyword in user_question for keyword in ("多少", "数量", "count", "几条")):
            projected_columns = ["COUNT(*) AS total"]

        sql = f"SELECT {', '.join(projected_columns)} FROM {base_table}"
        joined_tables = {base_table}
        for hint in join_hints:
            right_table = hint["right_table"]
            if right_table in joined_tables:
                continue
            if right_table not in selected_tables:
                continue
            sql += (
                f" JOIN {right_table} ON "
                f"{hint['left_table']}.{hint['left_column']} = {hint['right_table']}.{hint['right_column']}"
            )
            joined_tables.add(right_table)

        sql += " LIMIT 50"
        return sql
