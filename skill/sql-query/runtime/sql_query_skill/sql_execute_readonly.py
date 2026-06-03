from __future__ import annotations

import re
from typing import Any

from src.core.contracts import CapabilityContract, CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryTimeoutError, TransientReadonlyExecutionError
from src.integrations.rust_safety_contract import DataAccessContractError

from .helpers import SQL_QUERY_PUBLIC_CAPABILITY_ID, find_dependency_output, make_artifact, sql_fingerprint


class SQLQuerySQLExecuteReadonlyCapability(CapabilityContract):
    capability_id = SQL_QUERY_PUBLIC_CAPABILITY_ID
    version = "1"
    description = "在 SQL Guard 通过后通过显式异步边界执行只读 SQL。"

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
                error=CapabilityExecutionError(code="data_access_write_denied", message=str(exc), retriable=False),
            )
        except ReadonlyQueryTimeoutError as exc:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(code=exc.code, message=str(exc), retriable=False),
            )
        except DataAccessContractError as exc:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(code=exc.code, message=str(exc), retriable=exc.retriable),
            )
        except TransientReadonlyExecutionError as exc:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(code="db_transient_error", message=str(exc), retriable=True),
            )
        except Exception as exc:
            classified = _classify_sql_execution_error(exc, sql=str(upstream["sql"]))
            if classified is not None:
                return CapabilityExecutionResult(
                    capability_id=request.capability_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    error=classified,
                )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(
                    code="db_execution_failed",
                    message=_sanitize_db_error_message(str(exc)),
                    retriable=False,
                    metadata={
                        "failed_stage": "sql_execute_readonly",
                        "sql_fingerprint": sql_fingerprint(upstream["sql"]),
                        "repairable_sql_error": False,
                    },
                ),
            )

        source_row_count = getattr(query_result, "source_row_count", None)
        row_limit_trimmed = bool(getattr(query_result, "row_limit_trimmed", False))
        row_limit = getattr(query_result, "row_limit", None)
        row_limit_removed_row_count = int(getattr(query_result, "row_limit_removed_row_count", 0) or 0)
        truncated = bool(getattr(query_result, "truncated", False))
        output = {
            "route_id": upstream.get("route_id"),
            "schema_profile_id": upstream.get("schema_profile_id"),
            "sql": upstream["sql"],
            "columns": list(query_result.columns),
            "rows": list(query_result.rows),
            "row_count": query_result.row_count,
            "source_row_count": source_row_count if source_row_count is not None else query_result.row_count,
            "preview_row_count": len(query_result.rows),
            "truncated": truncated,
            "row_limit_trimmed": row_limit_trimmed,
            "row_limit": row_limit,
            "row_limit_removed_row_count": row_limit_removed_row_count,
        }
        artifact = make_artifact(
            name="query_result_preview",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary="query executed with retained rows" if truncated else f"query executed with {query_result.row_count} rows",
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
        )


def _classify_sql_execution_error(exc: Exception, *, sql: str) -> CapabilityExecutionError | None:
    text = _exception_text(exc)
    lowered = text.lower()
    code: str | None = None
    if any(marker in lowered for marker in ("1064", "syntax error", "sql syntax", "near '", 'near "')):
        code = "db_sql_syntax_error"
    elif any(marker in lowered for marker in ("1054", "unknown column", "no such column")):
        code = "db_unknown_column"
    elif any(marker in lowered for marker in ("1052", "ambiguous column", "column", "ambiguous")) and "ambiguous" in lowered:
        code = "db_ambiguous_column"
    elif any(marker in lowered for marker in ("1305", "function", "does not exist", "not found")) and "function" in lowered:
        code = "db_unknown_function"
    elif any(marker in lowered for marker in ("1146", "unknown table", "doesn't exist", "does not exist", "no such table")):
        code = "db_unknown_table"

    if code is None:
        return None

    metadata = {
        "failed_stage": "sql_execute_readonly",
        "db_error_code": code,
        "db_error_message": _sanitize_db_error_message(text),
        "sql_fingerprint": sql_fingerprint(sql),
        "repairable_sql_error": True,
    }
    return CapabilityExecutionError(
        code=code,
        message=metadata["db_error_message"],
        retriable=False,
        metadata=metadata,
    )


def _exception_text(exc: Exception) -> str:
    parts: list[str] = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None and orig is not exc:
        parts.append(str(orig))
        args = getattr(orig, "args", ())
        if args:
            parts.extend(str(arg) for arg in args)
    args = getattr(exc, "args", ())
    if args:
        parts.extend(str(arg) for arg in args)
    return " | ".join(part for part in parts if part)


def _sanitize_db_error_message(message: Any) -> str:
    value = str(message or "")
    value = re.sub(r"(?i)(password|passwd|pwd)\s*=\s*[^\s;]+", r"\1=<redacted>", value)
    value = re.sub(r"(?i)(mysql|postgres(?:ql)?)://[^\s]+", r"\1://<redacted>", value)
    value = re.sub(r"(?i)(guard_pass_token|guard token|token)\s*[:=]\s*[^\s;]+", r"\1=<redacted>", value)
    return value[:500]
