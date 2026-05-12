from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.models import Interrupt
from src.sql_query.route_understanding import QueryUnderstandingService

from .helpers import load_yaml, make_artifact, repo_root
from .llm_utils import LLMOutputError, TextGenerator, call_text_generator, parse_json_object


class SQLQueryIntentRouteCapability(CapabilityContract):
    capability_id = "sql_query.intent_route"
    version = "1"
    description = "判断请求是否属于 SQLQuery 路由，并收集最小路由上下文。"

    def __init__(self, *, routing_rules_path: str | None = None, semantic_text_generator: TextGenerator | None = None) -> None:
        self._routing_rules_path = routing_rules_path or str(repo_root() / "configs/sql_query/routing_rules.yaml")
        self._routing_rules = load_yaml(self._routing_rules_path)
        self._understanding = QueryUnderstandingService(self._routing_rules)
        self._semantic_text_generator = semantic_text_generator

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        user_question = str(request.input_payload.get("user_question", "")).strip()
        if not user_question:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                interrupt=self._make_route_interrupt(request, question="请先提供要查询的问题内容。", reason_code="missing_user_question"),
            )

        route_hint = self._optional_string(request.input_payload.get("route_hint"))
        subtask_label = self._optional_string(request.input_payload.get("subtask_label"))
        parent_question = self._optional_string(request.input_payload.get("parent_question"))
        semantic_fallback_reason: str | None = None
        semantic_understanding = None
        if route_hint is None and self._semantic_text_generator is not None:
            semantic_understanding, semantic_fallback_reason = await self._try_semantic_route_understanding(
                user_question,
                request=request,
                subtask_label=subtask_label,
                parent_question=parent_question,
            )
        understanding = semantic_understanding or self._understanding.understand(
            user_question,
            route_hint=route_hint,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )
        route = understanding.route
        if route is None:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                interrupt=self._make_route_interrupt(
                    request,
                    question="请确认你要查的是审定品种库，还是基因型数据库。",
                    reason_code="route_not_resolved",
                    required_fields={"route_id": {"options": ["approval_variety_db", "genotype_db"]}},
                ),
            )

        inferred_crop = understanding.inferred_crop
        if route.get("route_id") == "approval_variety_db" and inferred_crop is None and not understanding.no_crop_broad_query:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                interrupt=self._make_route_interrupt(
                    request,
                    question="请补充要查询的作物类型（玉米、水稻、棉花、小麦、大豆）。",
                    reason_code="crop_not_resolved",
                    required_fields={"crop": {"options": list(route.get("supported_crops", []))}},
                ),
            )

        output = {
            "route_id": route.get("route_id"),
            "schema_profile_id": route.get("schema_profile_id"),
            "sql_policy_profile": route.get("sql_policy_profile"),
            "allowed_tables": list(route.get("allowed_tables", [])),
            "user_question": user_question,
            "inferred_crop": inferred_crop,
            "route_resolution_strategy": understanding.route_resolution_strategy,
            "candidate_routes": [candidate.as_dict() for candidate in understanding.candidate_routes],
            "needs_decomposition": understanding.needs_decomposition,
            "subquestions": [subquestion.as_dict() for subquestion in understanding.subquestions],
            "no_crop_broad_query": understanding.no_crop_broad_query,
            "llm_router_used": semantic_understanding is not None,
        }
        if semantic_fallback_reason:
            output["llm_router_fallback_reason"] = semantic_fallback_reason
        if understanding.route_hint:
            output["route_hint"] = understanding.route_hint
        if understanding.subtask_label:
            output["subtask_label"] = understanding.subtask_label
        if understanding.parent_question:
            output["parent_question"] = understanding.parent_question
        artifact = make_artifact(
            name="intent_summary",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=f"intent route resolved to {output['route_id']}",
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
        )

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    async def _try_semantic_route_understanding(
        self,
        user_question: str,
        *,
        request: CapabilityExecutionRequest,
        subtask_label: str | None,
        parent_question: str | None,
    ):
        try:
            raw_output = await call_text_generator(
                self._semantic_text_generator,
                self._build_semantic_route_prompt(user_question),
                request=self._semantic_route_request(request),
            )
            decoded = parse_json_object(raw_output)
        except LLMOutputError as exc:
            return None, exc.reason
        except Exception:
            return None, "provider_failed"

        if self._decoded_bool(decoded.get("clarification_needed")) or str(decoded.get("intent") or "").lower() == "non_database":
            return None, "semantic_router_declined"

        route_id = self._optional_string(decoded.get("route_id"))
        if route_id is None:
            route_ids = decoded.get("route_ids")
            if isinstance(route_ids, list) and route_ids:
                route_id = self._optional_string(route_ids[0])
        if route_id is None:
            return None, "missing_route_id"

        understanding = self._understanding.understand(
            user_question,
            route_hint=route_id,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )
        if understanding.route is None or understanding.route_resolution_strategy != "route_hint":
            return None, "unsupported_route_id"

        return replace(
            understanding,
            route_resolution_strategy="llm_semantic",
            route_hint=None,
        ), None

    @staticmethod
    def _semantic_route_request(request: CapabilityExecutionRequest) -> CapabilityExecutionRequest:
        """Use the SQLQuery LLM non-streaming path but disable thinking for route choice."""

        metadata = dict(request.metadata)
        metadata["deep_thinking"] = False
        metadata["main_agent_thinking_enabled"] = False
        return replace(request, metadata=metadata)

    def _build_semantic_route_prompt(self, user_question: str) -> str:
        routes = [
            {
                "route_id": route.get("route_id"),
                "display_name": route.get("display_name"),
                "description": route.get("description"),
                "intent_keywords": route.get("intent_keywords", []),
                "semantic_hints": route.get("semantic_hints", []),
                "supported_crops": route.get("supported_crops", []),
            }
            for route in self._routing_rules.get("routes", [])
            if isinstance(route, Mapping) and route.get("enabled", True)
        ]
        return (
            "节点：sql_query.intent_route。\n"
            "你是 SQLQuery 的受控语义路由器。只判断用户问题属于哪个已配置数据库路由，不生成 SQL。"
            "只能返回 JSON 对象，字段包括：intent、route_id、route_ids、inferred_crop、clarification_needed。"
            "route_id 必须来自给定 routes；如果不是数据库问题，intent=non_database。\n\n"
            f"routes={json.dumps(routes, ensure_ascii=False)}\n\n"
            f"user_question={user_question}"
        )

    @staticmethod
    def _decoded_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "是"}
        return bool(value)

    def _make_route_interrupt(
        self,
        request: CapabilityExecutionRequest,
        *,
        question: str,
        reason_code: str,
        required_fields: Mapping[str, Any] | None = None,
    ) -> Interrupt:
        fingerprint = json.dumps(
            {
                "reason_code": reason_code,
                "question": question,
                "required_fields": required_fields or {},
                "input_payload": request.input_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
        return Interrupt(
            interrupt_id=f"{request.node_id}:interrupt:{reason_code}:{digest}",
            conversation_id=request.conversation_id,
            task_id=request.task_id,
            node_id=request.node_id,
            source_agent=self.capability_id,
            source_message_id=f"{request.node_id}:clarification",
            question=question,
            reason_code=reason_code,
            required_fields=required_fields or {},
        )
