from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.models import Interrupt

from .helpers import load_yaml, make_artifact, normalize_text, repo_root


class SQLQueryIntentRouteCapability(CapabilityContract):
    capability_id = "sql_query.intent_route"
    version = "1"
    description = "判断请求是否属于 SQLQuery 路由，并收集最小路由上下文。"

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
            "route_resolution_strategy": self._route_resolution_strategy(route, user_question),
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
        explicit_route = self._explicit_route_alias_match(user_question)
        if explicit_route is not None:
            return explicit_route

        if self._looks_like_broad_variety_lookup(user_question):
            overview = self._route_by_id("variety_overview")
            if overview is not None:
                return overview

        return self._best_scored_route(user_question, include_overview=False)

    def _explicit_route_alias_match(self, user_question: str) -> Mapping[str, Any] | None:
        normalized_question = normalize_text(user_question)
        if not normalized_question:
            return None

        matched_routes: dict[str, Mapping[str, Any]] = {}
        for route in self._routing_rules.get("routes", []):
            if not isinstance(route, Mapping) or not route.get("enabled", True):
                continue
            route_id = str(route.get("route_id") or "")
            if any(alias in normalized_question for alias in self._route_aliases(route)):
                matched_routes[route_id] = route

        if len(matched_routes) == 1:
            return next(iter(matched_routes.values()))
        return None

    def _route_aliases(self, route: Mapping[str, Any]) -> tuple[str, ...]:
        aliases: list[str] = []
        route_id = str(route.get("route_id") or "")
        if route_id:
            aliases.extend([route_id, route_id.replace("_", "")])
        display_name = str(route.get("display_name") or "")
        if display_name:
            aliases.append(display_name)
        for alias in route.get("route_aliases", []):
            aliases.append(str(alias))
        return tuple(alias for alias in (normalize_text(item) for item in aliases) if alias)

    def _best_scored_route(self, user_question: str, *, include_overview: bool) -> Mapping[str, Any] | None:
        normalized_question = normalize_text(user_question)
        best_route: Mapping[str, Any] | None = None
        best_score = 0
        for route in self._routing_rules.get("routes", []):
            if not isinstance(route, Mapping) or not route.get("enabled", True):
                continue
            if not include_overview and route.get("route_id") == "variety_overview":
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

    def _route_by_id(self, route_id: str) -> Mapping[str, Any] | None:
        for route in self._routing_rules.get("routes", []):
            if isinstance(route, Mapping) and route.get("route_id") == route_id and route.get("enabled", True):
                return route
        return None

    def _looks_like_broad_variety_lookup(self, user_question: str) -> bool:
        normalized_question = normalize_text(user_question)
        if not normalized_question or self._has_specific_route_intent(normalized_question):
            return False
        has_lookup_verb = any(
            keyword in normalized_question
            for keyword in ("查一下", "看一下", "了解一下", "查询", "查查", "看看", "品种", "信息")
        )
        has_variety_like_entity = bool(
            re.search(r"[\u4e00-\u9fffA-Za-z]{1,16}\d+[A-Za-z0-9\u4e00-\u9fff_-]*", user_question)
        )
        return has_lookup_verb and has_variety_like_entity

    def _has_specific_route_intent(self, normalized_question: str) -> bool:
        specific_keywords = (
            "审定",
            "公告",
            "申请者",
            "育种者",
            "特征",
            "产量",
            "适种",
            "区域",
            "审定编号",
            "基因",
            "基因型",
            "qtn",
            "变异",
            "位点",
            "成分",
            "比例",
            "籼稻",
            "粳稻",
            "籼型",
            "粳型",
        )
        return any(keyword in normalized_question for keyword in specific_keywords)

    def _route_resolution_strategy(self, route: Mapping[str, Any], user_question: str) -> str:
        explicit_route = self._explicit_route_alias_match(user_question)
        if explicit_route is not None and explicit_route.get("route_id") == route.get("route_id"):
            return "explicit_route_alias"
        if route.get("route_id") == "variety_overview" and self._looks_like_broad_variety_lookup(user_question):
            return "first_principles_broad_variety_overview"
        return "keyword_score"

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
