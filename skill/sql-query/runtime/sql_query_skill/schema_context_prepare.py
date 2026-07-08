from __future__ import annotations

from src.core.contracts import CapabilityContract, CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult
from .schema_ddl import render_mysql_schema_ddl
from .query_constraints import build_query_constraints

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

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("route_id", "schema_profile_id", "user_question"))
        route = next(
            (
                item
                for item in self._routing_rules.get("routes", [])
                if isinstance(item, dict) and item.get("route_id") == upstream.get("route_id")
            ),
            None,
        )
        if route is not None and route.get("schema_profile_id") and route.get("schema_profile_id") != upstream.get("schema_profile_id"):
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(
                    code="route_profile_mismatch",
                    message=(
                        f"Route {upstream.get('route_id')} expects schema_profile_id={route.get('schema_profile_id')}, "
                        f"got {upstream.get('schema_profile_id')}"
                    ),
                    retriable=False,
                    metadata={
                        "domain_kind": "sql_query",
                        "capability_id": SQL_QUERY_PUBLIC_CAPABILITY_ID,
                        "failed_stage": "schema_context_prepare",
                        "expected_schema_profile_id": route.get("schema_profile_id"),
                    },
                ),
            )
        if upstream.get("selected_tables"):
            return self._materialize_selected_tables(request, upstream)
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            error=CapabilityExecutionError(
                code="schema_resolution_required",
                message="Schema context materialization requires upstream selected_tables from schema_resolution.",
                retriable=False,
                metadata={
                    "domain_kind": "sql_query",
                    "capability_id": SQL_QUERY_PUBLIC_CAPABILITY_ID,
                    "failed_stage": "schema_context_prepare",
                },
            ),
        )

    @staticmethod
    def _current_year(upstream: dict, request: CapabilityExecutionRequest) -> int | None:
        for source in (upstream, request.metadata):
            value = source.get("current_year") if isinstance(source, dict) else None
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None


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

    def _materialize_selected_tables(self, request: CapabilityExecutionRequest, upstream: dict) -> CapabilityExecutionResult:
        route_id = str(upstream["route_id"])
        schema_profile_id = str(upstream["schema_profile_id"])
        selected_tables = [str(table) for table in list(upstream.get("selected_tables") or [])]
        profile = next(
            (
                item
                for item in self._schema_metadata.get("schema_profiles", [])
                if isinstance(item, dict) and item.get("profile_id") == schema_profile_id and item.get("route_id") == route_id
            ),
            None,
        )
        if profile is None:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(
                    code="schema_profile_not_found",
                    message=f"Unknown schema profile for route {route_id}: {schema_profile_id}",
                    retriable=False,
                ),
            )
        profile_tables = {str(table) for table in list(profile.get("tables") or [])}
        invalid_tables = [table for table in selected_tables if table not in profile_tables]
        if invalid_tables:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(
                    code="selected_table_outside_profile",
                    message=f"Selected tables are outside schema profile: {', '.join(invalid_tables)}",
                    retriable=False,
                ),
            )
        selected_columns: dict[str, list[str]] = {}
        tables_meta = self._schema_metadata.get("tables", {})
        for table in selected_tables:
            table_meta = tables_meta.get(table, {}) if isinstance(tables_meta, dict) else {}
            columns_meta = table_meta.get("columns", {}) if isinstance(table_meta, dict) else {}
            selected_columns[table] = [
                str(name)
                for name, meta in columns_meta.items()
                if isinstance(meta, dict) and meta.get("expose_to_llm", True)
            ]
        join_hints = []
        for hint in self._schema_metadata.get("join_hints", []):
            if not isinstance(hint, dict):
                continue
            if hint.get("left_table") in selected_tables and hint.get("right_table") in selected_tables:
                join_hints.append(
                    {
                        "left_table": hint.get("left_table"),
                        "left_column": hint.get("left_column"),
                        "right_table": hint.get("right_table"),
                        "right_column": hint.get("right_column"),
                        "reason": hint.get("description") or hint.get("reason") or "",
                    }
                )
        metadata = {
            **dict(upstream.get("metadata") or {}),
            "materialization_strategy": "resolution_selected_tables",
            "table_scope_authority": "schema_resolution",
            "column_selection_strategy": "llm_visible_all_exposed_columns",
        }
        if upstream.get("no_crop_broad_query") or upstream.get("resolution_reason") == "approval_cross_crop_allowed":
            metadata["no_crop_broad_query"] = True
            route_notes = list(metadata.get("route_notes") or [])
            route_notes.append("all approval crop tables selected by schema_resolution")
            metadata["route_notes"] = route_notes
        output = {
            **{key: value for key, value in upstream.items() if key not in {"understanding"}},
            "route_id": route_id,
            "schema_profile_id": schema_profile_id,
            "selected_tables": selected_tables,
            "selected_columns": selected_columns,
            "selected_column_details": self._selected_column_details(selected_columns),
            "schema_ddl": render_mysql_schema_ddl(
                self._schema_metadata,
                selected_tables,
                selected_columns=selected_columns,
            ),
            "join_hints": join_hints,
            "context_summary": (
                f"Resolved schema context for route {route_id}; selected tables: {', '.join(selected_tables)}. "
                "Table scope was determined by schema_resolution and was not expanded during materialization."
            ),
            "metadata": metadata,
        }
        output["query_constraints"] = build_query_constraints(
            output,
            current_year=self._current_year(upstream, request),
        )
        artifact = make_artifact(
            name="schema_context_snapshot",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=f"schema context materialized with {len(selected_tables)} tables",
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
        )
