from __future__ import annotations

from src.core.contracts import CapabilityContract, CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.models import Interrupt
from .models import SchemaContextRequest
from .schema_ddl import render_mysql_schema_ddl
from .schema_context_builder import SchemaContextBuilder

from .helpers import SQL_QUERY_PUBLIC_CAPABILITY_ID, find_dependency_output, load_yaml, make_artifact, skill_root


class SQLQuerySchemaContextPrepareCapability(CapabilityContract):
    capability_id = SQL_QUERY_PUBLIC_CAPABILITY_ID
    version = "1"
    description = "根据路由元数据和 schema 配置构建 LLM 可见的路由级 schema 上下文。"

    def __init__(self, *, routing_rules_path: str | None = None, schema_metadata_path: str | None = None) -> None:
        routing_path = routing_rules_path or str(skill_root() / "configs/routing_rules.yaml")
        metadata_path = schema_metadata_path or str(skill_root() / "configs/schema_metadata.yaml")
        self._routing_rules = load_yaml(routing_path)
        self._schema_metadata = load_yaml(metadata_path)
        self._builder = SchemaContextBuilder(self._routing_rules, self._schema_metadata)

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("route_id", "schema_profile_id", "user_question"))
        route_id = str(upstream["route_id"])
        allowed_tables = list(upstream.get("allowed_tables", []))
        max_tables = max(4, len(allowed_tables)) if upstream.get("no_crop_broad_query") else 4
        schema_request = SchemaContextRequest(
            route_id=route_id,
            schema_profile_id=str(upstream["schema_profile_id"]),
            user_question=str(upstream["user_question"]),
            hints={"crop": upstream.get("inferred_crop")} if upstream.get("inferred_crop") else None,
            max_tables=max_tables,
        )
        result = await self._builder.build_context(schema_request)
        if result.ok:
            selected_columns = {table: list(columns) for table, columns in result.selected_columns.items()}
            output = {
                "route_id": result.route_id,
                "schema_profile_id": result.schema_profile_id,
                "user_question": upstream["user_question"],
                "sql_policy_profile": upstream.get("sql_policy_profile"),
                "allowed_tables": list(upstream.get("allowed_tables", [])),
                "selected_tables": list(result.selected_tables),
                "selected_columns": selected_columns,
                "selected_column_details": self._selected_column_details(result.selected_columns),
                "schema_ddl": render_mysql_schema_ddl(
                    self._schema_metadata,
                    result.selected_tables,
                    selected_columns=selected_columns,
                ),
                "join_hints": [
                    {
                        "left_table": hint.left_table,
                        "left_column": hint.left_column,
                        "right_table": hint.right_table,
                        "right_column": hint.right_column,
                        "reason": hint.reason,
                    }
                    for hint in result.join_hints
                ],
                "context_summary": result.context_summary,
                "metadata": dict(result.metadata),
            }
            artifact = make_artifact(
                name="schema_context_snapshot",
                task_id=request.task_id,
                node_id=request.node_id,
                payload=output,
                summary=f"schema context prepared with {len(result.selected_tables)} tables",
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload=output,
                artifacts=(artifact,),
            )

        if result.failure and result.failure.code == "crop_not_resolved":
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                interrupt=Interrupt(
                    interrupt_id=f"{request.node_id}:interrupt",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    source_agent=self.capability_id,
                    source_message_id=f"{request.node_id}:clarification",
                    question="请补充作物类型，系统才能继续裁剪审定品种库的 schema 上下文。",
                    reason_code="crop_not_resolved",
                    required_fields={"crop": {"options": ["corn", "rice", "cotton", "wheat", "soybean"]}},
                ),
            )

        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            error=CapabilityExecutionError(
                code=result.failure.code if result.failure else "schema_context_prepare_failed",
                message=result.failure.message if result.failure else "schema context prepare failed",
                retriable=result.failure.retriable if result.failure else False,
                metadata=dict(result.failure.metadata) if result.failure else {},
            ),
        )


    def _selected_column_details(self, selected_columns):
        tables = self._schema_metadata.get("tables", {})
        details = {}
        for table, columns in dict(selected_columns).items():
            table_meta = tables.get(table, {}) if isinstance(tables, dict) else {}
            columns_meta = table_meta.get("columns", {}) if isinstance(table_meta, dict) else {}
            details[table] = []
            for column in list(columns):
                column_meta = columns_meta.get(column, {}) if isinstance(columns_meta, dict) else {}
                details[table].append(
                    {
                        "name": column,
                        "sql_type": str(column_meta.get("sql_type", "")),
                        "description": str(column_meta.get("description", "")),
                    }
                )
        return details
