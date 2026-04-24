from __future__ import annotations

from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.models import Interrupt

from .helpers import load_yaml, make_artifact, normalize_text, repo_root


class SQLQueryIntentRouteCapability(CapabilityContract):
    capability_id = "sql_query.intent_route"
    version = "1"
    description = "Detect whether the request belongs to an SQLQuery route and collect minimal routing context."

    def __init__(self, *, routing_rules_path: str | None = None) -> None:
        self._routing_rules_path = routing_rules_path or str(repo_root() / "configs/sql_query/routing_rules.yaml")
        self._routing_rules = load_yaml(self._routing_rules_path)

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        user_question = str(request.input_payload.get("user_question", "")).strip()
        if not user_question:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                interrupt=self._make_route_interrupt(request, question="请先提供要查询的问题内容。", reason_code="missing_user_question"),
            )

        route = self._select_route(user_question)
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

        inferred_crop = self._infer_crop(route, user_question)
        if route.get("route_id") == "approval_variety_db" and inferred_crop is None:
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
        }
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

    def _select_route(self, user_question: str) -> Mapping[str, Any] | None:
        normalized_question = normalize_text(user_question)
        best_route: Mapping[str, Any] | None = None
        best_score = 0
        for route in self._routing_rules.get("routes", []):
            if not isinstance(route, Mapping) or not route.get("enabled", True):
                continue
            score = 0
            for keyword in route.get("intent_keywords", []):
                if normalize_text(keyword) and normalize_text(keyword) in normalized_question:
                    score += 3
            for hint in route.get("semantic_hints", []):
                if normalize_text(hint) and normalize_text(hint) in normalized_question:
                    score += 1
            if score > best_score:
                best_route = route
                best_score = score
        return best_route if best_score > 0 else None

    def _infer_crop(self, route: Mapping[str, Any], user_question: str) -> str | None:
        normalized_question = normalize_text(user_question)
        crop_aliases = route.get("crop_aliases", {})
        for crop in route.get("supported_crops", []):
            aliases = crop_aliases.get(crop, [])
            for candidate in [crop, *aliases]:
                if normalize_text(candidate) and normalize_text(candidate) in normalized_question:
                    return str(crop)
        return None

    def _make_route_interrupt(
        self,
        request: CapabilityExecutionRequest,
        *,
        question: str,
        reason_code: str,
        required_fields: Mapping[str, Any] | None = None,
    ) -> Interrupt:
        return Interrupt(
            interrupt_id=f"{request.node_id}:interrupt",
            conversation_id=request.conversation_id,
            task_id=request.task_id,
            node_id=request.node_id,
            source_agent=self.capability_id,
            source_message_id=f"{request.node_id}:clarification",
            question=question,
            reason_code=reason_code,
            required_fields=required_fields or {},
        )
