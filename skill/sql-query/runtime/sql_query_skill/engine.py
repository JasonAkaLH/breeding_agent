from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .intent_route import SQLQueryIntentRouteCapability
from .result_filtering import SQLQueryResultFilteringCapability
from .schema_resolution import SQLQuerySchemaResolutionCapability
from .schema_context_prepare import SQLQuerySchemaContextPrepareCapability
from .sql_execute_readonly import SQLQuerySQLExecuteReadonlyCapability
from .sql_generate import SQLQuerySQLGenerateCapability
from .sql_guard import SQLQuerySQLGuardCapability
from src.core.contracts import CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import EventVisibility
from src.core.models import Artifact, EventRecord, Interrupt
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from .helpers import (
    SQL_QUERY_AUDIT_REPAIR_ATTEMPTED_EVENT,
    SQL_QUERY_AUDIT_REPAIR_FAILED_EVENT,
    SQL_QUERY_AUDIT_REPAIR_SUCCEEDED_EVENT,
    SQL_QUERY_DOMAIN_KIND,
    SQL_QUERY_PUBLIC_CAPABILITY_ID,
    SQL_QUERY_SKILL_NAME,
    make_audit_event,
    sql_fingerprint,
)


SQL_REPAIR_MAX_ATTEMPTS = 5


@dataclass(slots=True, frozen=True)
class SQLQueryEngineRequest:
    query: str
    conversation_id: str
    task_id: str
    node_id: str
    metadata: Mapping[str, Any]
    subtask_label: str | None = None
    parent_question: str | None = None
    skill_name: str = SQL_QUERY_SKILL_NAME


@dataclass(slots=True, frozen=True)
class SQLQueryEngineResult:
    output_payload: Mapping[str, Any]
    artifacts: tuple[Artifact, ...]
    events: tuple[EventRecord, ...]
    interrupt: Interrupt | None = None
    error: CapabilityExecutionError | None = None


@dataclass(slots=True, frozen=True)
class _StageRunResult:
    stage_node_id: str
    output_payload: Mapping[str, Any]
    interrupt: Interrupt | None = None
    error: CapabilityExecutionError | None = None
    terminal: SQLQueryEngineResult | None = None


class SQLQueryEngine:
    """Run the SQLQuery domain flow behind a single platform-service Skill node.

    The public orchestration graph sees only `skill.sql_query`; this engine keeps
    SQLQuery's internal route/schema/generate/guard/execute/filter behavior local
    to the SQLQuery domain and emits domain progress/events/artifacts.
    """

    _STAGES = (
        ("intent_route", "正在理解查询意图"),
        ("schema_resolution", "正在确定查询表范围"),
        ("schema_context_prepare", "正在准备数据库查询"),
        ("sql_generate", "正在生成安全查询语句"),
        ("sql_guard", "正在检查查询安全边界"),
        ("sql_execute_readonly", "正在检索数据库"),
        ("result_filtering", "正在筛选查询结果"),
    )

    def __init__(
        self,
        *,
        mysql_adapter: MySQLReadonlyAdapter,
        llm_text_generator=None,
        sql_generator: Callable[[dict[str, Any]], str] | None = None,
        trim_max_tokens: int | None = None,
        progress_event_recorder: Callable[[EventRecord], Awaitable[None] | None] | None = None,
    ) -> None:
        self._progress_event_recorder = progress_event_recorder
        self._capabilities = {
            "intent_route": SQLQueryIntentRouteCapability(semantic_text_generator=llm_text_generator),
            "schema_resolution": SQLQuerySchemaResolutionCapability(adapter=mysql_adapter),
            "schema_context_prepare": SQLQuerySchemaContextPrepareCapability(),
            "sql_generate": SQLQuerySQLGenerateCapability(generator=sql_generator, llm_text_generator=llm_text_generator),
            "sql_guard": SQLQuerySQLGuardCapability(),
            "sql_execute_readonly": SQLQuerySQLExecuteReadonlyCapability(adapter=mysql_adapter),
            "result_filtering": SQLQueryResultFilteringCapability(llm_text_generator=llm_text_generator, trim_max_tokens=trim_max_tokens),
        }

    async def execute(self, request: SQLQueryEngineRequest) -> SQLQueryEngineResult:
        dependency_outputs: dict[str, Mapping[str, Any]] = {}
        artifacts: list[Artifact] = []
        events: list[EventRecord] = []
        last_output: Mapping[str, Any] = {}

        repair_state: dict[str, Any] = {"attempted": False, "attempts": 0}

        previous_stage_node_id: str | None = None
        for stage, label in self._STAGES[:3]:
            stage_result = await self._run_stage(
                request,
                stage=stage,
                label=label,
                previous_stage_node_id=previous_stage_node_id,
                dependency_outputs=dependency_outputs,
                events=events,
                artifacts=artifacts,
            )
            last_output = stage_result.output_payload
            previous_stage_node_id = stage_result.stage_node_id
            if stage_result.terminal is not None:
                return stage_result.terminal

        schema_stage_node_id = previous_stage_node_id
        execute_stage_node_id: str | None = None
        pending_repair_context: dict[str, Any] | None = None
        repair_iteration = 0
        local_repair_attempted = False
        remote_repair_attempts = 0
        while True:
            suffix = f":repair{repair_iteration}" if repair_iteration else ""
            is_repair = repair_iteration > 0
            if is_repair:
                repair_progress = self._progress_event(request, stage="sql_repair", label="正在修正查询语句")
                await self._record_or_append_progress(repair_progress, events)

            generate_result = await self._run_stage(
                request,
                stage="sql_generate",
                label="正在重新生成安全查询语句" if is_repair else "正在生成安全查询语句",
                previous_stage_node_id=schema_stage_node_id,
                dependency_outputs=dependency_outputs,
                events=events,
                artifacts=artifacts,
                node_id_suffix=suffix,
                emit_progress=not is_repair,
                input_payload={"sql_repair_context": pending_repair_context} if pending_repair_context else None,
            )
            last_output = generate_result.output_payload
            if generate_result.interrupt is not None and is_repair:
                error = self._repair_generation_error(pending_repair_context, reason="repair_returned_interrupt")
                self._append_repair_event(
                    request,
                    events,
                    event_type=SQL_QUERY_AUDIT_REPAIR_FAILED_EVENT,
                    repair_context=pending_repair_context,
                    final_error=error,
                )
                return SQLQueryEngineResult(
                    output_payload=self._with_repair_payload(self._final_payload(last_output, stage="sql_generate"), repair_state),
                    artifacts=tuple(artifacts),
                    events=tuple(events),
                    error=error,
                )
            if generate_result.terminal is not None:
                if (
                    not is_repair
                    and generate_result.error is not None
                    and not local_repair_attempted
                    and self._is_repairable_local_error(generate_result.error)
                ):
                    local_repair_attempted = True
                    repair_iteration += 1
                    repair_state = {
                        "attempted": True,
                        "attempts": repair_iteration,
                        "local_attempts": 1,
                        "remote_attempts": remote_repair_attempts,
                        "repaired_from_stage": "sql_generate",
                        "last_error_code": generate_result.error.code,
                    }
                    pending_repair_context = self._make_repair_context(
                        failed_output=generate_result.output_payload or last_output,
                        error=generate_result.error,
                        attempt=repair_iteration,
                        default_failed_stage="sql_generate",
                    )
                    self._append_repair_event(
                        request,
                        events,
                        event_type=SQL_QUERY_AUDIT_REPAIR_ATTEMPTED_EVENT,
                        repair_context=pending_repair_context,
                        final_error=generate_result.error,
                    )
                    continue
                if is_repair:
                    error = generate_result.error or self._repair_generation_error(
                        pending_repair_context, reason="repair_generation_failed"
                    )
                    self._append_repair_event(
                        request,
                        events,
                        event_type=SQL_QUERY_AUDIT_REPAIR_FAILED_EVENT,
                        repair_context=pending_repair_context,
                        final_error=error,
                    )
                    return SQLQueryEngineResult(
                        output_payload=self._with_repair_payload(
                            self._final_payload(last_output, stage="sql_generate"),
                            repair_state,
                        ),
                        artifacts=tuple(artifacts),
                        events=tuple(events),
                        error=error,
                    )
                return generate_result.terminal

            guard_result = await self._run_stage(
                request,
                stage="sql_guard",
                label="正在检查修复后查询安全边界" if is_repair else "正在检查查询安全边界",
                previous_stage_node_id=generate_result.stage_node_id,
                dependency_outputs=dependency_outputs,
                events=events,
                artifacts=artifacts,
                node_id_suffix=suffix,
                emit_progress=not is_repair,
            )
            last_output = guard_result.output_payload
            if guard_result.error is not None:
                if (
                    not is_repair
                    and not local_repair_attempted
                    and self._is_repairable_local_error(guard_result.error)
                ):
                    local_repair_attempted = True
                    repair_iteration += 1
                    repair_state = {
                        "attempted": True,
                        "attempts": repair_iteration,
                        "local_attempts": 1,
                        "remote_attempts": remote_repair_attempts,
                        "repaired_from_stage": "sql_guard",
                        "last_error_code": guard_result.error.code,
                    }
                    pending_repair_context = self._make_repair_context(
                        failed_output=generate_result.output_payload or last_output,
                        error=guard_result.error,
                        attempt=repair_iteration,
                        default_failed_stage="sql_guard",
                    )
                    self._append_repair_event(
                        request,
                        events,
                        event_type=SQL_QUERY_AUDIT_REPAIR_ATTEMPTED_EVENT,
                        repair_context=pending_repair_context,
                        final_error=guard_result.error,
                    )
                    continue
                if is_repair:
                    self._append_repair_event(
                        request,
                        events,
                        event_type=SQL_QUERY_AUDIT_REPAIR_FAILED_EVENT,
                        repair_context=pending_repair_context,
                        final_error=guard_result.error,
                    )
                return SQLQueryEngineResult(
                    output_payload=self._with_repair_payload(self._final_payload(last_output, stage="sql_guard"), repair_state),
                    artifacts=tuple(artifacts),
                    events=tuple(events),
                    error=guard_result.error,
                )

            execute_result = await self._run_stage(
                request,
                stage="sql_execute_readonly",
                label="正在检索数据库",
                previous_stage_node_id=guard_result.stage_node_id,
                dependency_outputs=dependency_outputs,
                events=events,
                artifacts=artifacts,
                node_id_suffix=suffix,
                emit_progress=not is_repair,
            )
            last_output = execute_result.output_payload
            execute_stage_node_id = execute_result.stage_node_id
            if execute_result.error is not None:
                if remote_repair_attempts >= SQL_REPAIR_MAX_ATTEMPTS or not self._is_repairable_sql_error(execute_result.error):
                    if is_repair:
                        self._append_repair_event(
                            request,
                            events,
                            event_type=SQL_QUERY_AUDIT_REPAIR_FAILED_EVENT,
                            repair_context=pending_repair_context,
                            final_error=execute_result.error,
                        )
                    return SQLQueryEngineResult(
                        output_payload=self._with_repair_payload(
                            self._final_payload(last_output, stage="sql_execute_readonly"),
                            repair_state,
                        ),
                        artifacts=tuple(artifacts),
                        events=tuple(events),
                        error=execute_result.error,
                    )
                remote_repair_attempts += 1
                repair_iteration += 1
                repair_state = {
                    "attempted": True,
                    "attempts": repair_iteration,
                    "local_attempts": 1 if local_repair_attempted else 0,
                    "remote_attempts": remote_repair_attempts,
                    "repaired_from_stage": "sql_execute_readonly",
                    "last_error_code": execute_result.error.code,
                }
                pending_repair_context = self._make_repair_context(
                    failed_output=guard_result.output_payload or generate_result.output_payload,
                    error=execute_result.error,
                    attempt=remote_repair_attempts,
                )
                self._append_repair_event(
                    request,
                    events,
                    event_type=SQL_QUERY_AUDIT_REPAIR_ATTEMPTED_EVENT,
                    repair_context=pending_repair_context,
                    final_error=execute_result.error,
                )
                continue
            if is_repair:
                self._append_repair_event(
                    request,
                    events,
                    event_type=SQL_QUERY_AUDIT_REPAIR_SUCCEEDED_EVENT,
                    repair_context=pending_repair_context,
                )
            break

        if execute_stage_node_id is None:
            error = CapabilityExecutionError(code="sql_execute_missing", message="SQL execution did not produce output.")
            return SQLQueryEngineResult(
                output_payload=self._with_repair_payload(self._final_payload(last_output, stage="sql_execute_readonly"), repair_state),
                artifacts=tuple(artifacts),
                events=tuple(events),
                error=error,
            )

        result_stage, result_label = self._STAGES[-1]
        result_filtering_result = await self._run_stage(
            request,
            stage=result_stage,
            label=result_label,
            previous_stage_node_id=execute_stage_node_id,
            dependency_outputs=dependency_outputs,
            events=events,
            artifacts=artifacts,
        )
        last_output = result_filtering_result.output_payload
        if result_filtering_result.terminal is not None:
            terminal = result_filtering_result.terminal
            return SQLQueryEngineResult(
                output_payload=self._with_repair_payload(terminal.output_payload, repair_state),
                artifacts=terminal.artifacts,
                events=terminal.events,
                interrupt=terminal.interrupt,
                error=terminal.error,
            )

        return SQLQueryEngineResult(
            output_payload=self._with_repair_payload(self._final_payload(last_output, stage="result_filtering"), repair_state),
            artifacts=tuple(artifacts),
            events=tuple(events),
        )

    @staticmethod
    def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        blocked = {"conversation_memory", "memory_context", "recent_messages", "history_summary", "resolved_user_message"}
        return {str(key): value for key, value in metadata.items() if str(key) not in blocked}

    async def _run_stage(
        self,
        request: SQLQueryEngineRequest,
        *,
        stage: str,
        label: str,
        previous_stage_node_id: str | None,
        dependency_outputs: dict[str, Mapping[str, Any]],
        events: list[EventRecord],
        artifacts: list[Artifact],
        node_id_suffix: str = "",
        emit_progress: bool = True,
        input_payload: Mapping[str, Any] | None = None,
    ) -> _StageRunResult:
        stage_node_id = f"{request.node_id}:{stage}{node_id_suffix}"
        if emit_progress:
            progress_event = self._progress_event(request, stage=stage, label=label)
            await self._record_or_append_progress(progress_event, events)
        capability = self._capabilities[stage]
        stage_input_payload: dict[str, Any] = dict(input_payload or {})
        if stage == "intent_route":
            stage_input_payload = {"user_question": request.query}
            if request.subtask_label:
                stage_input_payload["subtask_label"] = request.subtask_label
            if request.parent_question:
                stage_input_payload["parent_question"] = request.parent_question
        result = await capability.execute(
            CapabilityExecutionRequest(
                capability_id=SQL_QUERY_PUBLIC_CAPABILITY_ID,
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                node_id=stage_node_id,
                input_payload=stage_input_payload,
                dependency_outputs=(
                    {previous_stage_node_id: dependency_outputs[previous_stage_node_id]}
                    if previous_stage_node_id is not None
                    else {}
                ),
                metadata={**self._safe_metadata(request.metadata), "stage": stage, "component": stage},
            )
        )
        events.extend(result.events)
        artifacts.extend(self._annotate_artifacts(result.artifacts, stage=stage, public_node_id=request.node_id))
        output_payload = dict(result.output_payload)
        dependency_outputs[stage_node_id] = output_payload
        terminal: SQLQueryEngineResult | None = None
        if result.interrupt is not None:
            terminal = SQLQueryEngineResult(
                output_payload=self._final_payload(output_payload, stage=stage),
                artifacts=tuple(artifacts),
                events=tuple(events),
                interrupt=replace(result.interrupt, node_id=request.node_id),
            )
        elif result.error is not None:
            terminal = SQLQueryEngineResult(
                output_payload=self._final_payload(output_payload, stage=stage),
                artifacts=tuple(artifacts),
                events=tuple(events),
                error=result.error,
            )
        return _StageRunResult(
            stage_node_id=stage_node_id,
            output_payload=output_payload,
            interrupt=result.interrupt,
            error=result.error,
            terminal=terminal,
        )

    async def _record_or_append_progress(self, event: EventRecord, events: list[EventRecord]) -> None:
        if self._progress_event_recorder is not None:
            maybe_result = self._progress_event_recorder(event)
            if inspect.isawaitable(maybe_result):
                await maybe_result
        else:
            events.append(event)

    @staticmethod
    def _is_repairable_sql_error(error: CapabilityExecutionError) -> bool:
        return bool(error.metadata.get("repairable_sql_error"))

    @staticmethod
    def _is_repairable_local_error(error: CapabilityExecutionError) -> bool:
        if bool(error.metadata.get("repairable_sql_error")):
            return True
        return error.code in {
            "sql_generation_validation_failed",
            "empty_sql",
        }

    @staticmethod
    def _make_repair_context(
        *,
        failed_output: Mapping[str, Any],
        error: CapabilityExecutionError,
        attempt: int,
        default_failed_stage: str = "sql_execute_readonly",
    ) -> dict[str, Any]:
        failed_sql = str(failed_output.get("sql") or "")
        metadata = dict(error.metadata or {})
        return {
            "failed_sql": failed_sql,
            "failed_stage": metadata.get("failed_stage") or default_failed_stage,
            "error_code": error.code,
            "error_message": metadata.get("db_error_message") or error.message,
            "sql_fingerprint": metadata.get("sql_fingerprint") or sql_fingerprint(failed_sql),
            "attempt": attempt,
            "max_attempts": SQL_REPAIR_MAX_ATTEMPTS,
            "route_id": failed_output.get("route_id"),
            "schema_profile_id": failed_output.get("schema_profile_id"),
            "selected_tables": list(failed_output.get("selected_tables", [])),
            "original_user_query": failed_output.get("original_user_query") or failed_output.get("user_question"),
            "resolved_user_query": failed_output.get("resolved_user_query") or failed_output.get("user_question"),
            "schema_ddl": failed_output.get("schema_ddl"),
        }

    def _append_repair_event(
        self,
        request: SQLQueryEngineRequest,
        events: list[EventRecord],
        *,
        event_type: str,
        repair_context: Mapping[str, Any] | None,
        final_error: CapabilityExecutionError | None = None,
    ) -> None:
        context = dict(repair_context or {})
        payload = {
            "capability_id": SQL_QUERY_PUBLIC_CAPABILITY_ID,
            "stage": "sql_repair",
            "attempt": context.get("attempt"),
            "max_attempts": context.get("max_attempts") or SQL_REPAIR_MAX_ATTEMPTS,
            "failed_stage": context.get("failed_stage"),
            "error_code": (final_error.code if final_error is not None else context.get("error_code")),
            "sql_fingerprint": context.get("sql_fingerprint"),
            "route_id": context.get("route_id"),
            "schema_profile_id": context.get("schema_profile_id"),
        }
        if final_error is not None:
            payload["final_error_code"] = final_error.code
        event = make_audit_event(
            CapabilityExecutionRequest(
                capability_id=SQL_QUERY_PUBLIC_CAPABILITY_ID,
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                node_id=request.node_id,
                metadata=self._safe_metadata(request.metadata),
            ),
            event_type=event_type,
            payload=payload,
        )
        events.append(event)

    @staticmethod
    def _repair_generation_error(
        repair_context: Mapping[str, Any] | None,
        *,
        reason: str,
    ) -> CapabilityExecutionError:
        return CapabilityExecutionError(
            code="sql_repair_generation_failed",
            message=f"SQL repair generation failed: {reason}",
            retriable=False,
            metadata={
                "repairable_sql_error": False,
                "repair_attempt": int((repair_context or {}).get("attempt") or 1),
                "failed_stage": (repair_context or {}).get("failed_stage"),
                "last_error_code": (repair_context or {}).get("error_code"),
            },
        )

    @staticmethod
    def _with_repair_payload(payload: Mapping[str, Any], repair_state: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        if repair_state.get("attempted"):
            result["sql_repair"] = dict(repair_state)
        return result

    @staticmethod
    def _final_payload(output: Mapping[str, Any], *, stage: str) -> dict[str, Any]:
        payload = dict(output)
        payload.setdefault("domain_kind", SQL_QUERY_DOMAIN_KIND)
        payload.setdefault("capability_id", SQL_QUERY_PUBLIC_CAPABILITY_ID)
        payload.setdefault("stage", stage)
        if "summary" not in payload:
            row_count = payload.get("row_count")
            if isinstance(row_count, int):
                payload["summary"] = f"查询已完成，共返回 {row_count} 行结果。"
            else:
                payload["summary"] = "SQLQuery 查询已完成。"
        if "filtered_query_result" not in payload and {"columns", "rows", "row_count"}.issubset(payload):
            payload["filtered_query_result"] = {
                "columns": list(payload.get("columns") or ()),
                "rows": list(payload.get("rows") or ()),
                "row_count": payload.get("row_count"),
                "truncated": bool(payload.get("truncated", False)),
            }
        return payload

    def _progress_event(self, request: SQLQueryEngineRequest, *, stage: str, label: str) -> EventRecord:
        payload = {
            "domain_kind": SQL_QUERY_DOMAIN_KIND,
            "capability_id": SQL_QUERY_PUBLIC_CAPABILITY_ID,
            "skill_name": request.skill_name or SQL_QUERY_SKILL_NAME,
            "stage": stage,
            "label": label,
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(f"{request.node_id}:{stage}:{serialized}".encode("utf-8")).hexdigest()[:12]
        return EventRecord(
            event_id=f"{request.node_id}:skill.progress:{stage}:{digest}",
            conversation_id=request.conversation_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type="skill.progress",
            payload=payload,
            visibility=EventVisibility.FRONTEND,
        )

    @staticmethod
    def _annotate_artifacts(artifacts: tuple[Artifact, ...], *, stage: str, public_node_id: str) -> tuple[Artifact, ...]:
        annotated = []
        for artifact in artifacts:
            payload = _decode_json_object(artifact.storage_ref)
            if payload is not None:
                payload.setdefault("domain_kind", SQL_QUERY_DOMAIN_KIND)
                payload.setdefault("capability_id", SQL_QUERY_PUBLIC_CAPABILITY_ID)
                payload.setdefault("stage", stage)
                payload.setdefault("artifact_role", _artifact_role(artifact.artifact_id, stage))
                storage_ref = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            else:
                storage_ref = artifact.storage_ref
            annotated.append(
                Artifact(
                    artifact_id=artifact.artifact_id,
                    task_id=artifact.task_id,
                    producer_node_id=public_node_id,
                    artifact_type=artifact.artifact_type,
                    storage_ref=storage_ref,
                    summary=artifact.summary,
                    is_complete=artifact.is_complete,
                    created_at=artifact.created_at,
                )
            )
        return tuple(annotated)


def _decode_json_object(value: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _artifact_role(artifact_id: str, stage: str) -> str:
    if "filtered_query_result" in artifact_id:
        return "filtered_query_result"
    if "query_result_preview" in artifact_id:
        return "query_result_preview"
    if "generated_sql" in artifact_id:
        return "generated_sql"
    if "guard_report" in artifact_id:
        return "guard_report"
    if "schema_context_snapshot" in artifact_id:
        return "schema_context_snapshot"
    if "intent_summary" in artifact_id:
        return "intent_summary"
    return stage
