from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from src.capabilities.sql_query.intent_route import SQLQueryIntentRouteCapability
from src.capabilities.sql_query.result_filtering import SQLQueryResultFilteringCapability
from src.capabilities.sql_query.schema_context_prepare import SQLQuerySchemaContextPrepareCapability
from src.capabilities.sql_query.sql_execute_readonly import SQLQuerySQLExecuteReadonlyCapability
from src.capabilities.sql_query.sql_generate import SQLQuerySQLGenerateCapability
from src.capabilities.sql_query.sql_guard import SQLQuerySQLGuardCapability
from src.core.contracts import CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import EventVisibility
from src.core.models import Artifact, EventRecord, Interrupt
from src.integrations.mysql_readonly import MySQLReadonlyAdapter


@dataclass(slots=True, frozen=True)
class SQLQueryEngineRequest:
    query: str
    conversation_id: str
    task_id: str
    node_id: str
    metadata: Mapping[str, Any]
    subtask_label: str | None = None
    parent_question: str | None = None


@dataclass(slots=True, frozen=True)
class SQLQueryEngineResult:
    output_payload: Mapping[str, Any]
    artifacts: tuple[Artifact, ...]
    events: tuple[EventRecord, ...]
    interrupt: Interrupt | None = None
    error: CapabilityExecutionError | None = None


class SQLQueryEngine:
    """Run the SQLQuery domain flow behind a single platform-service Skill node.

    The public orchestration graph sees only `skill.sql_query`; this engine keeps
    SQLQuery's internal route/schema/generate/guard/execute/filter behavior local
    to the SQLQuery domain and emits domain progress/events/artifacts.
    """

    _STAGES = (
        ("intent_route", "sql_query.intent_route", "正在理解查询意图"),
        ("schema_context_prepare", "sql_query.schema_context_prepare", "正在准备数据库查询"),
        ("sql_generate", "sql_query.sql_generate", "正在生成安全查询语句"),
        ("sql_guard", "sql_query.sql_guard", "正在检查查询安全边界"),
        ("sql_execute_readonly", "sql_query.sql_execute_readonly", "正在检索数据库"),
        ("result_filtering", "sql_query.result_filtering", "正在筛选查询结果"),
    )

    def __init__(
        self,
        *,
        mysql_adapter: MySQLReadonlyAdapter,
        llm_text_generator=None,
        sql_generator: Callable[[dict[str, Any]], str] | None = None,
        trim_max_tokens: int | None = None,
    ) -> None:
        self._capabilities = {
            "sql_query.intent_route": SQLQueryIntentRouteCapability(semantic_text_generator=llm_text_generator),
            "sql_query.schema_context_prepare": SQLQuerySchemaContextPrepareCapability(),
            "sql_query.sql_generate": SQLQuerySQLGenerateCapability(generator=sql_generator, llm_text_generator=llm_text_generator),
            "sql_query.sql_guard": SQLQuerySQLGuardCapability(),
            "sql_query.sql_execute_readonly": SQLQuerySQLExecuteReadonlyCapability(adapter=mysql_adapter),
            "sql_query.result_filtering": SQLQueryResultFilteringCapability(llm_text_generator=llm_text_generator, trim_max_tokens=trim_max_tokens),
        }

    async def execute(self, request: SQLQueryEngineRequest) -> SQLQueryEngineResult:
        dependency_outputs: dict[str, Mapping[str, Any]] = {}
        previous_stage_node_id: str | None = None
        artifacts: list[Artifact] = []
        events: list[EventRecord] = []
        last_output: Mapping[str, Any] = {}

        for stage, capability_id, label in self._STAGES:
            stage_node_id = f"{request.node_id}:{stage}"
            events.append(self._progress_event(request, stage=stage, label=label))
            capability = self._capabilities[capability_id]
            input_payload: dict[str, Any] = {}
            if stage == "intent_route":
                input_payload = {"user_question": request.query}
                if request.subtask_label:
                    input_payload["subtask_label"] = request.subtask_label
                if request.parent_question:
                    input_payload["parent_question"] = request.parent_question
            result = await capability.execute(
                CapabilityExecutionRequest(
                    capability_id=capability_id,
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id=stage_node_id,
                    input_payload=input_payload,
                    dependency_outputs=(
                        {previous_stage_node_id: dependency_outputs[previous_stage_node_id]}
                        if previous_stage_node_id is not None
                        else {}
                    ),
                    metadata=self._safe_metadata(request.metadata),
                )
            )
            events.extend(result.events)
            artifacts.extend(self._annotate_artifacts(result.artifacts, stage=stage, public_node_id=request.node_id))
            last_output = dict(result.output_payload)
            dependency_outputs[stage_node_id] = last_output
            previous_stage_node_id = stage_node_id
            if result.interrupt is not None:
                return SQLQueryEngineResult(
                    output_payload=self._final_payload(last_output, stage=stage),
                    artifacts=tuple(artifacts),
                    events=tuple(events),
                    interrupt=replace(result.interrupt, node_id=request.node_id),
                )
            if result.error is not None:
                return SQLQueryEngineResult(
                    output_payload=self._final_payload(last_output, stage=stage),
                    artifacts=tuple(artifacts),
                    events=tuple(events),
                    error=result.error,
                )

        return SQLQueryEngineResult(
            output_payload=self._final_payload(last_output, stage="result_filtering"),
            artifacts=tuple(artifacts),
            events=tuple(events),
        )

    @staticmethod
    def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        blocked = {"conversation_memory", "memory_context", "recent_messages", "history_summary", "resolved_user_message"}
        return {str(key): value for key, value in metadata.items() if str(key) not in blocked}

    @staticmethod
    def _final_payload(output: Mapping[str, Any], *, stage: str) -> dict[str, Any]:
        payload = dict(output)
        payload.setdefault("domain_kind", "sql_query")
        payload.setdefault("capability_id", "skill.sql_query")
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
            "domain_kind": "sql_query",
            "capability_id": "skill.sql_query",
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
                payload.setdefault("domain_kind", "sql_query")
                payload.setdefault("capability_id", "skill.sql_query")
                payload.setdefault("stage", stage)
                payload.setdefault("artifact_role", _artifact_role(artifact.artifact_id, stage))
                storage_ref = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            else:
                storage_ref = artifact.storage_ref
            annotated.append(
                Artifact(
                    artifact_id=artifact.artifact_id.replace("sql_query.", "sql_query_"),
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
    except Exception:
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
