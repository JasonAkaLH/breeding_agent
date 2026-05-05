from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


DATA_ACTION_KEYWORDS = (
    "查",
    "查询",
    "检索",
    "搜索",
    "统计",
    "找",
    "列出",
    "看一下",
    "了解一下",
    "多少",
    "有哪些",
    "对应",
)
GENERIC_DATA_SOURCE_KEYWORDS = ("sql", "数据库", "数据表")
COMPOSITE_ROUTE_IDS = ("approval_variety_db", "genotype_db")


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    route_id: str
    score: int
    matched_keywords: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "score": self.score,
            "matched_keywords": list(self.matched_keywords),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class QuerySubquestion:
    user_question: str
    route_hint: str
    subtask_label: str
    parent_question: str

    def as_dict(self) -> dict[str, str]:
        return {
            "user_question": self.user_question,
            "route_hint": self.route_hint,
            "subtask_label": self.subtask_label,
            "parent_question": self.parent_question,
        }


@dataclass(frozen=True, slots=True)
class QueryUnderstandingResult:
    should_use_sql_query: bool
    route: Mapping[str, Any] | None
    route_resolution_strategy: str | None
    inferred_crop: str | None
    candidate_routes: tuple[RouteCandidate, ...]
    needs_decomposition: bool = False
    subquestions: tuple[QuerySubquestion, ...] = ()
    no_crop_broad_query: bool = False
    route_hint: str | None = None
    subtask_label: str | None = None
    parent_question: str | None = None


class QueryUnderstandingService:
    """Configuration-backed SQLQuery route understanding shared by planning and SQLQuery."""

    def __init__(self, routing_rules: Mapping[str, Any]) -> None:
        self._routing_rules = routing_rules
        self._routes = tuple(
            route
            for route in routing_rules.get("routes", [])
            if isinstance(route, Mapping) and route.get("route_id") and route.get("enabled", True)
        )

    @classmethod
    def from_yaml_file(cls, routing_rules_path: str | Path) -> "QueryUnderstandingService":
        file_path = Path(routing_rules_path)
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, Mapping):
            raise ValueError(f"YAML file {file_path} must decode to a mapping.")
        return cls(data)

    def understand(
        self,
        user_question: str,
        *,
        route_hint: str | None = None,
        subtask_label: str | None = None,
        parent_question: str | None = None,
    ) -> QueryUnderstandingResult:
        question = str(user_question or "").strip()
        normalized_question = normalize_text(question)
        if not normalized_question:
            return QueryUnderstandingResult(
                should_use_sql_query=False,
                route=None,
                route_resolution_strategy=None,
                inferred_crop=None,
                candidate_routes=(),
            )

        candidates = self._score_routes(normalized_question)
        route: Mapping[str, Any] | None = None
        strategy: str | None = None
        needs_decomposition = False
        subquestions: tuple[QuerySubquestion, ...] = ()

        hinted_route = self._route_by_id(str(route_hint or ""))
        if hinted_route is not None:
            route = hinted_route
            strategy = "route_hint"
        else:
            explicit_route = self._explicit_route_alias_match(normalized_question)
            if explicit_route is not None:
                route = explicit_route
                strategy = "explicit_route_alias"
            elif self._is_composite_database_question(candidates, normalized_question=normalized_question):
                route = self._route_by_id("variety_overview") or self._route_by_id(candidates[0].route_id)
                strategy = "composite_multi_route"
                needs_decomposition = True
                subquestions = self._build_subquestions(question)
            elif self._looks_like_broad_variety_lookup(question):
                route = self._route_by_id("variety_overview")
                strategy = "first_principles_broad_variety_overview"
            elif candidates:
                route = self._route_by_id(candidates[0].route_id)
                strategy = "keyword_score"

        inferred_crop = self._infer_crop(route, normalized_question) if route is not None else None
        no_crop_broad_query = bool(
            route is not None
            and route.get("route_id") == "approval_variety_db"
            and inferred_crop is None
            and route.get("supports_no_crop_broad_query", False)
        )
        if no_crop_broad_query and strategy not in {"route_hint", "explicit_route_alias"}:
            strategy = "no_crop_approval_broad"

        should_use_sql_query = self._should_use_sql_query(
            normalized_question=normalized_question,
            route=route,
            strategy=strategy,
            needs_decomposition=needs_decomposition,
            route_hint=route_hint,
        )
        return QueryUnderstandingResult(
            should_use_sql_query=should_use_sql_query,
            route=route,
            route_resolution_strategy=strategy,
            inferred_crop=inferred_crop,
            candidate_routes=candidates,
            needs_decomposition=needs_decomposition,
            subquestions=subquestions,
            no_crop_broad_query=no_crop_broad_query,
            route_hint=route_hint,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )

    def _should_use_sql_query(
        self,
        *,
        normalized_question: str,
        route: Mapping[str, Any] | None,
        strategy: str | None,
        needs_decomposition: bool,
        route_hint: str | None,
    ) -> bool:
        if route is None:
            return False
        if needs_decomposition or route_hint:
            return True
        if strategy in {"explicit_route_alias", "first_principles_broad_variety_overview", "composite_multi_route"}:
            return True
        if any(keyword in normalized_question for keyword in DATA_ACTION_KEYWORDS):
            return True
        if any(keyword in normalized_question for keyword in GENERIC_DATA_SOURCE_KEYWORDS):
            return True
        return False

    def _score_routes(self, normalized_question: str) -> tuple[RouteCandidate, ...]:
        candidates: list[RouteCandidate] = []
        for route in self._routes:
            route_id = str(route.get("route_id"))
            if route_id == "variety_overview":
                continue
            score = 0
            matched_keywords: list[str] = []
            reasons: list[str] = []
            for keyword in route.get("intent_keywords", []):
                normalized_keyword = normalize_text(keyword)
                if normalized_keyword and normalized_keyword in normalized_question:
                    score += 3
                    matched_keywords.append(str(keyword))
            if matched_keywords:
                reasons.append("intent keywords matched")
            for alias in self._route_aliases(route):
                if alias and alias in normalized_question:
                    score += 5
                    reasons.append("route alias matched")
            if score > 0:
                candidates.append(
                    RouteCandidate(
                        route_id=route_id,
                        score=score,
                        matched_keywords=tuple(dict.fromkeys(matched_keywords)),
                        reasons=tuple(dict.fromkeys(reasons)),
                    )
                )
        return tuple(sorted(candidates, key=lambda item: (-item.score, item.route_id)))

    def _is_composite_database_question(
        self,
        candidates: Sequence[RouteCandidate],
        *,
        normalized_question: str,
    ) -> bool:
        candidate_by_id = {candidate.route_id: candidate for candidate in candidates if candidate.score > 0}
        if not all(route_id in candidate_by_id for route_id in COMPOSITE_ROUTE_IDS):
            return False
        approval_keywords = set(candidate_by_id["approval_variety_db"].matched_keywords)
        genotype_keywords = set(candidate_by_id["genotype_db"].matched_keywords)
        has_specific_approval_intent = bool(approval_keywords - {"品种"}) or self._has_approval_info_phrase(
            normalized_question
        )
        has_specific_genotype_intent = bool(genotype_keywords)
        return has_specific_approval_intent and has_specific_genotype_intent

    @staticmethod
    def _has_approval_info_phrase(normalized_question: str) -> bool:
        if not any(
            phrase in normalized_question
            for phrase in ("品种信息", "品种资料", "品种详情", "基本信息", "品种情况")
        ):
            return False
        return any(connector in normalized_question for connector in ("和", "及", "以及", "并", "同时", "都"))

    def _build_subquestions(self, user_question: str) -> tuple[QuerySubquestion, ...]:
        entity = self._extract_primary_entity(user_question)
        if entity:
            approval_question = f"查询{entity}的审定信息"
            genotype_question = f"查询{entity}的基因型信息"
        else:
            approval_question = f"{user_question}（审定品种库）"
            genotype_question = f"{user_question}（基因型数据库）"
        return (
            QuerySubquestion(
                user_question=approval_question,
                route_hint="approval_variety_db",
                subtask_label="审定信息",
                parent_question=user_question,
            ),
            QuerySubquestion(
                user_question=genotype_question,
                route_hint="genotype_db",
                subtask_label="基因型信息",
                parent_question=user_question,
            ),
        )

    @staticmethod
    def _extract_primary_entity(user_question: str) -> str | None:
        match = re.search(r"[\u4e00-\u9fffA-Za-z]{1,12}\d+[A-Za-z0-9_-]*", user_question)
        return match.group(0) if match else None

    def _explicit_route_alias_match(self, normalized_question: str) -> Mapping[str, Any] | None:
        matched_routes: dict[str, Mapping[str, Any]] = {}
        for route in self._routes:
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
        aliases.extend(str(alias) for alias in route.get("route_aliases", []))
        return tuple(alias for alias in (normalize_text(item) for item in aliases) if alias)

    def _route_by_id(self, route_id: str) -> Mapping[str, Any] | None:
        normalized_route_id = normalize_text(route_id)
        if not normalized_route_id:
            return None
        for route in self._routes:
            current_route_id = str(route.get("route_id") or "")
            normalized_current = normalize_text(current_route_id)
            if normalized_current in {normalized_route_id, normalized_route_id.replace("_", "")}:
                return route
            if normalized_current.replace("_", "") == normalized_route_id:
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
        has_variety_like_entity = bool(self._extract_primary_entity(user_question))
        return has_lookup_verb and has_variety_like_entity

    @staticmethod
    def _has_specific_route_intent(normalized_question: str) -> bool:
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

    def _infer_crop(self, route: Mapping[str, Any] | None, normalized_question: str) -> str | None:
        if route is None:
            return None
        crop_aliases = route.get("crop_aliases", {})
        for crop in route.get("supported_crops", []):
            candidates = [crop]
            if isinstance(crop_aliases, Mapping):
                candidates.extend(crop_aliases.get(crop, []))
            for candidate in candidates:
                normalized_candidate = normalize_text(candidate)
                if normalized_candidate and normalized_candidate in normalized_question:
                    return str(crop)
        return None


def normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()
