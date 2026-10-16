from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from typing import Any

from src.core.contracts import CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import EventVisibility
from src.capabilities.main_agent.helpers import make_event
from src.orchestration.agent_loop.knowledge_results import (
    KNOWLEDGE_CAPABILITY_IDS,
    KNOWLEDGE_RESULT_SCHEMA,
    canonical_json,
    decode_knowledge_cursor,
    encode_knowledge_cursor,
    expected_knowledge_failure,
    knowledge_model_view,
    load_knowledge_resource,
    trusted_knowledge_scope,
    validate_knowledge_result,
)


class KnowledgeExecutor:
    """Three read-only tools; model input never supplies identity or file references."""

    def __init__(self, service: Any, *, token_counter: Any | None = None) -> None:
        self._service = service
        self._token_counter = token_counter or _count_page

    def supports(self, capability_id: str) -> bool:
        return capability_id in KNOWLEDGE_CAPABILITY_IDS

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        started = time.monotonic()
        token_count = 0
        try:
            scope = trusted_knowledge_scope(request.metadata, conversation_id=request.conversation_id)
            run_id = request.metadata.get("agent_run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("agent_knowledge_scope_invalid")
            result, token_count = await self._read(request, scope, run_id)
        except Exception as exc:
            status = expected_knowledge_failure(exc)
            if status is None:
                return CapabilityExecutionResult(
                    capability_id=request.capability_id, task_id=request.task_id, node_id=request.node_id,
                    error=CapabilityExecutionError(
                        code="knowledge_read_failed", message="Knowledge reading failed; no complete read is claimed.",
                        retriable=False,
                    ),
                    events=(self._event(request, started, "failed", 0, None),),
                )
            result = {"schema": KNOWLEDGE_RESULT_SCHEMA, "status": status, "reference": None}
        return CapabilityExecutionResult(
            capability_id=request.capability_id, task_id=request.task_id, node_id=request.node_id,
            output_payload=result,
            events=(self._event(request, started, result["status"], token_count, result["reference"]),),
        )

    @staticmethod
    def _event(request, started, status, token_count, reference):
        return make_event(request, event_type="knowledge.read_completed", visibility=EventVisibility.AUDIT_ONLY,
                          payload={"action": request.capability_id, "status": status,
                                   "duration_ms": int((time.monotonic() - started) * 1000),
                                   "tokens": token_count, "start": reference["start"] if reference else None,
                                   "end": reference["end"] if reference else None})

    async def _read(self, request: CapabilityExecutionRequest, scope: Mapping[str, Any],
                    run_id: str) -> tuple[dict[str, Any], int]:
        if not self.supports(request.capability_id):
            raise ValueError("knowledge_unknown_tool")
        payload = request.input_payload
        allowed = {"cursor"}
        if request.capability_id != "knowledge.list_libraries":
            allowed.add("library_id")
        if request.capability_id == "knowledge.read_document":
            allowed.add("document_id")
        if set(payload) - allowed:
            raise ValueError("knowledge_invalid_arguments")
        library_id, document_id = payload.get("library_id"), payload.get("document_id")
        resource = await load_knowledge_resource(
            self._service, scope, capability_id=request.capability_id,
            library_id=library_id, document_id=document_id,
        )
        text = resource["text"]
        if not isinstance(text, str):
            raise ValueError("knowledge_content_invalid")
        start = 0
        ref = {
            "actor": scope["actor"], "root_message_id": scope["root_message_id"],
            "conversation_id": request.conversation_id, "task_id": request.task_id, "run_id": run_id,
            "capability_id": request.capability_id, "library_id": library_id, "document_id": document_id,
            "version": resource["version"], "start": 0, "end": 0, "total_characters": len(text),
            "next_cursor": None, "metadata": resource["metadata"],
        }
        if payload.get("cursor") is not None:
            cursor = decode_knowledge_cursor(payload["cursor"])
            if any(cursor[key] != ref[key] for key in ("capability_id", "library_id", "document_id")):
                raise ValueError("agent_knowledge_cursor_invalid")
            if cursor["version"] != ref["version"]:
                return {"schema": KNOWLEDGE_RESULT_SCHEMA, "status": "changed", "reference": None}, 0
            start = cursor["offset"]
            if start >= len(text) and text:
                raise ValueError("agent_knowledge_cursor_invalid")
        ref["start"] = start
        budget = request.metadata.get("knowledge_result_token_budget", 50_000)
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError("knowledge_budget_invalid")
        budget = min(budget, 50_000)
        low, high = start, min(len(text), start + 120_000)
        best = None
        best_count = 0
        first_attempt = True
        while low <= high:
            end = high if first_attempt else (low + high) // 2
            first_attempt = False
            candidate_ref = {**ref, "end": end,
                             "next_cursor": encode_knowledge_cursor(ref, end) if end < len(text) else None}
            result = {"schema": KNOWLEDGE_RESULT_SCHEMA, "status": "available", "reference": candidate_ref}
            validate_knowledge_result(result)
            view = knowledge_model_view(result, text[start:end], resource["metadata"])
            envelope = {"outcome": "completed", "safe_error_code": None, "safe_result": view, "artifact_refs": []}
            fragment = canonical_json({"message": {"content": canonical_json(envelope), "role": "tool",
                                                   "tool_call_id": request.metadata.get("knowledge_provider_call_id", "")}})
            count = self._token_counter(fragment, str(request.metadata.get("agent_model_edition") or ""))
            if inspect.isawaitable(count):
                count = await count
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("knowledge_token_count_invalid")
            if count <= budget:
                best = result
                best_count = count
                low = end + 1
            else:
                high = end - 1
        if best is None or (best["reference"]["end"] == start and start < len(text)):
            return {"schema": KNOWLEDGE_RESULT_SCHEMA, "status": "budget_exceeded", "reference": None}, 0
        return best, best_count


async def _count_page(text: str, model_edition: str) -> int:
    from src.integrations.llm_client import load_config
    from src.integrations.model_editions import config_for_model_edition
    from src.integrations.token_counter import get_num_of_tokens_from_text_async

    return await get_num_of_tokens_from_text_async(text, config=config_for_model_edition(load_config(), model_edition))
