from __future__ import annotations

from src.core.contracts import CapabilityContract, CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, TransientReadonlyExecutionError

from .helpers import find_dependency_output, make_artifact


class SQLQuerySQLExecuteReadonlyCapability(CapabilityContract):
    capability_id = "sql_query.sql_execute_readonly"
    version = "1"
    description = "Execute readonly SQL behind a guard-pass contract and explicit async boundary."

    def __init__(self, *, adapter: MySQLReadonlyAdapter | None = None) -> None:
        self._adapter = adapter or MySQLReadonlyAdapter()

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("sql",))
        guard_pass_token = upstream.get("guard_pass_token")
        if not guard_pass_token:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(code="guard_token_missing", message="Readonly execution requires a guard pass token."),
            )

        try:
            query_result = await self._adapter.execute_readonly(str(upstream["sql"]), guard_pass_token=str(guard_pass_token))
        except PermissionError as exc:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(code="guard_token_missing", message=str(exc)),
            )
        except TransientReadonlyExecutionError as exc:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(code="db_transient_error", message=str(exc), retriable=True),
            )
        except Exception as exc:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(code="db_execution_failed", message=str(exc), retriable=False),
            )

        output = {
            "route_id": upstream.get("route_id"),
            "schema_profile_id": upstream.get("schema_profile_id"),
            "sql": upstream["sql"],
            "guard_pass_token": guard_pass_token,
            "columns": list(query_result.columns),
            "rows": list(query_result.rows),
            "row_count": query_result.row_count,
            "preview_row_count": len(query_result.rows),
            "truncated": False,
        }
        artifact = make_artifact(
            name="query_result_preview",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=f"query executed with {query_result.row_count} rows",
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
        )
