from __future__ import annotations

import hashlib
import re
from typing import Any

from src.core.contracts import (
    CapabilityContract,
    CapabilityExecutionError,
    CapabilityExecutionRequest,
    CapabilityExecutionResult,
)
from src.core.enums import EventVisibility
from src.core.models import EventRecord

from .helpers import (
    SQL_QUERY_AUDIT_GUARD_BLOCKED_EVENT,
    SQL_QUERY_AUDIT_GUARD_PASSED_EVENT,
    SQL_QUERY_PUBLIC_CAPABILITY_ID,
    find_dependency_output,
    load_yaml,
    make_artifact,
    normalize_text,
    skill_root,
)


class SQLQuerySQLGuardCapability(CapabilityContract):
    capability_id = SQL_QUERY_PUBLIC_CAPABILITY_ID
    version = "1"
    description = "校验生成 SQL 是否严格只读且符合当前路由策略。"

    def __init__(self, *, routing_rules_path: str | None = None, guard_rules_path: str | None = None) -> None:
        routing_path = routing_rules_path or str(skill_root() / "configs/routing_rules.yaml")
        guard_path = guard_rules_path or str(skill_root() / "configs/sql_guard_rules.yaml")
        self._routing_rules = load_yaml(routing_path)
        self._guard_rules = load_yaml(guard_path)
        self._rule_set = self._guard_rules["rule_sets"][0]

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("sql", "route_id"))
        sql = str(upstream["sql"])
        normalized = self._normalize_sql(sql)
        allowed_tables = set(
            upstream.get("selected_tables")
            or upstream.get("allowed_tables")
            or self._route_allowed_tables(str(upstream["route_id"]))
        )
        guard_error = self._validate_sql(normalized, allowed_tables=allowed_tables)
        if guard_error is not None:
            event = self._make_event(
                request,
                event_type=SQL_QUERY_AUDIT_GUARD_BLOCKED_EVENT,
                payload={
                    "capability_id": self.capability_id,
                    "stage": "sql_guard",
                    "code": guard_error.code,
                    "message": guard_error.message,
                    "block_reason": guard_error.code,
                    "route_context": {
                        "route_id": upstream.get("route_id"),
                        "schema_profile_id": upstream.get("schema_profile_id"),
                    },
                },
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=guard_error,
                events=(event,),
            )

        guard_pass_token = self._make_guard_pass_token(normalized, str(upstream["route_id"]))
        output = {
            "route_id": upstream["route_id"],
            "schema_profile_id": upstream.get("schema_profile_id"),
            "sql": normalized,
            "guard_pass_token": guard_pass_token,
            "guard_report": {"status": "passed", "rule_set_id": self._rule_set["rule_set_id"]},
        }
        artifact = make_artifact(
            name="guard_report",
            task_id=request.task_id,
            node_id=request.node_id,
            payload={key: value for key, value in output.items() if key != "guard_pass_token"},
            summary="guard passed",
        )
        event = self._make_event(
            request,
            event_type=SQL_QUERY_AUDIT_GUARD_PASSED_EVENT,
            payload={"capability_id": self.capability_id, "stage": "sql_guard", "status": "passed"},
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
            events=(event,),
        )

    def _normalize_sql(self, sql: str) -> str:
        normalized = sql.strip()
        normalized = re.sub(r"/\\*.*?\\*/", " ", normalized, flags=re.S)
        normalized = re.sub(r"--.*?$", " ", normalized, flags=re.M)
        normalized = re.sub(r"#.*?$", " ", normalized, flags=re.M)
        normalized = normalized.rstrip(";").strip()
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    def _validate_sql(self, normalized_sql: str, *, allowed_tables: set[str]) -> CapabilityExecutionError | None:
        if not normalized_sql:
            return self._guard_error("empty_sql", "SQL is empty.", retriable=False)

        if ";" in normalized_sql:
            return self._guard_error("multiple_statements", "Multiple statements are not allowed.", retriable=False)

        lowered = normalized_sql.lower()
        root = lowered.split(" ", 1)[0]
        if root not in {"select", "with"}:
            return self._guard_error("statement_root_denied", f"SQL root statement {root!r} is not allowed.", retriable=False)

        for denied in self._rule_set["pattern_rules"]["deny_substrings"]:
            if denied.lower() in lowered:
                return self._guard_error("write_pattern_detected", f"Blocked SQL pattern detected: {denied}", retriable=False)

        for pattern in self._rule_set["pattern_rules"]["deny_regex"]:
            if re.search(pattern, normalized_sql):
                return self._guard_error("write_pattern_detected", f"Blocked SQL regex detected: {pattern}", retriable=False)

        table_names = set(self._extract_tables(lowered))
        for table in table_names:
            if "." in table:
                schema_name, table_name = table.split(".", 1)
                if schema_name in {name.lower() for name in self._rule_set["identifier_policies"]["deny_system_schemas"]}:
                    return self._guard_error("system_schema_access_denied", f"System schema access denied: {schema_name}", retriable=False)
                candidate_table = table_name
            else:
                candidate_table = table
            if allowed_tables and candidate_table not in {name.lower() for name in allowed_tables}:
                return self._guard_error("table_not_in_route_whitelist", f"Table {candidate_table} is not allowed for this route.", retriable=False)

        return None

    def _extract_tables(self, lowered_sql: str) -> list[str]:
        return re.findall(r"(?:from|join)\s+([a-zA-Z0-9_\.]+)", lowered_sql)

    def _route_allowed_tables(self, route_id: str) -> list[str]:
        for route in self._routing_rules.get("routes", []):
            if route.get("route_id") == route_id:
                return [str(table).lower() for table in route.get("allowed_tables", [])]
        return []

    def _make_guard_pass_token(self, normalized_sql: str, route_id: str) -> str:
        digest = hashlib.sha256(f"{route_id}:{normalized_sql}".encode("utf-8")).hexdigest()[:16]
        return f"guard:{route_id}:{digest}"

    def _guard_error(self, code: str, message: str, *, retriable: bool) -> CapabilityExecutionError:
        return CapabilityExecutionError(code=code, message=message, retriable=retriable)

    def _make_event(self, request: CapabilityExecutionRequest, *, event_type: str, payload: dict[str, Any]) -> EventRecord:
        return EventRecord(
            event_id=f"{request.node_id}:{event_type}",
            conversation_id=request.conversation_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=event_type,
            payload=payload,
            visibility=EventVisibility.AUDIT_ONLY,
        )
