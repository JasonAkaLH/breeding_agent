from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.models import Interrupt
from src.integrations.mysql_readonly import MySQLReadonlyAdapter

from .helpers import SQL_QUERY_PUBLIC_CAPABILITY_ID, find_dependency_output, make_artifact
from .sql_guard import SQLQuerySQLGuardCapability

_APPROVAL_CROP_TABLES = {
    "corn": "corn_varieties",
    "rice": "rice_varieties",
    "cotton": "cotton_varieties",
    "wheat": "wheat_varieties",
    "soybean": "soybean_varieties",
}
_APPROVAL_CROP_LABELS = {
    "corn": "玉米",
    "rice": "水稻",
    "cotton": "棉花",
    "wheat": "小麦",
    "soybean": "大豆",
}
_GENOTYPE_TABLES = ("variety", "variety_genotype", "qtn", "rice_comp")
_VARIETY_CANDIDATE_PATTERN = re.compile(r"[A-Za-z\u4e00-\u9fff]{1,16}\d+[A-Za-z0-9\u4e00-\u9fff]{0,12}")
_GENERIC_VARIETY_WORDS = ("查询", "品种", "审定", "信息", "详细", "一下", "帮我", "查一下")
_PROBE_FIELDS = ("variety_name", "applicant", "breeder", "approval_num")
_CORE_UNKNOWN_FIELDS = ("variety_name", "applicant", "breeder")
_MATCH_TIERS = ("primary", "secondary", "peer")
_APPLICANT_SIGNALS = ("申请者", "申请单位", "申报单位", "申请", "申报")
_BREEDER_SIGNALS = ("育种者", "育成者", "选育单位", "培育单位", "育成", "选育", "育种")
_ORG_HINTS = ("公司", "企业", "集团", "种业", "研究所", "农科院", "科学院", "大学", "学院", "中心", "合作社", "基地")
_TRANSGENIC_OWNER_SIGNALS = ("转化体所有者", "转基因所有者", "转基因权属", "transgenic_owner")


class SQLQuerySchemaResolutionCapability(CapabilityContract):
    capability_id = SQL_QUERY_PUBLIC_CAPABILITY_ID
    version = "1"
    description = "将 SQLQuery understanding 收敛为后续唯一权威 selected_tables。"

    def __init__(
        self,
        *,
        adapter: MySQLReadonlyAdapter | None = None,
        routing_rules_path: str | None = None,
    ) -> None:
        self._adapter = adapter or MySQLReadonlyAdapter()
        self._guard = SQLQuerySQLGuardCapability(routing_rules_path=routing_rules_path)

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("route_id", "schema_profile_id", "user_question"))
        route_id = str(upstream.get("route_id") or "")
        output_base = {
            "route_id": route_id,
            "schema_profile_id": upstream.get("schema_profile_id"),
            "sql_policy_profile": upstream.get("sql_policy_profile"),
            "allowed_tables": list(upstream.get("allowed_tables", [])),
            "user_question": upstream.get("user_question"),
            "original_user_query": upstream.get("original_user_query") or upstream.get("user_question"),
            "resolved_user_query": upstream.get("resolved_user_query") or upstream.get("user_question"),
            "parent_question": upstream.get("parent_question"),
            "subtask_label": upstream.get("subtask_label"),
            "understanding": dict(upstream),
        }
        if route_id == "genotype_db":
            return self._success(request, {**output_base, "selected_tables": list(_GENOTYPE_TABLES), "selected_crops": [], "resolution_reason": "genotype_fixed_tables"})
        if route_id != "approval_variety_db":
            return self._interrupt(
                request,
                output_base,
                question="当前问题暂不在 SQLQuery 支持的数据查询范围内。请改问审定品种库或基因型数据库中的查询。",
                missing=["route_id"],
                reason_code="route_not_resolved",
            )

        if _mentions_transgenic_owner(output_base["original_user_query"]) or _mentions_transgenic_owner(output_base["user_question"]):
            return self._interrupt(
                request,
                output_base,
                question=(
                    "当前 SQLQuery entity probe 支持按品种名、申请者、育种者和审定编号查询；"
                    "不支持按转化体所有者字段查询，也不会把转化体所有者自动映射为申请者或育种者。"
                    "请改用支持的字段，或补充其它查询条件。"
                ),
                missing=["supported_entity_field"],
                reason_code="unsupported_entity_field",
            )

        entities = _normalized_entities(upstream)
        crop = _clean_crop(upstream.get("inferred_crop") or upstream.get("crop"))
        if crop:
            table = _APPROVAL_CROP_TABLES[crop]
            entity_scope = _match_summary_for_known_tables(entities, [table]) if entities else {}
            return self._success(
                request,
                {
                    **output_base,
                    "entities": entities,
                    "selected_tables": [table],
                    "selected_crops": [crop],
                    "resolution_reason": "approval_crop_mapping",
                    **entity_scope,
                },
            )

        if entities:
            probe = await self._probe_approval_varieties(request, entities)
            hit_tables = [item["table"] for item in probe["table_hits"]]
            if hit_tables:
                selected_tables = list(dict.fromkeys(hit_tables))
                selected_crops = [
                    str(hit["crop"])
                    for hit in probe["table_hits"]
                    if str(hit["table"]) in selected_tables
                ]
                return self._success(
                    request,
                    {
                        **output_base,
                        "entities": entities,
                        "selected_tables": selected_tables,
                        "selected_crops": list(dict.fromkeys(selected_crops)),
                        "resolution_reason": "approval_entity_probe_hits",
                        "probe_summary": probe,
                        "match_summary": probe["match_summary"],
                        "matched_fields": probe["matched_fields"],
                        "match_tiers": probe["match_tiers"],
                        "search_effort_summary": probe["search_effort_summary"],
                    },
                )
            return self._interrupt(
                request,
                {
                    **output_base,
                    "entities": entities,
                    "probe_summary": probe,
                    "search_effort_summary": probe["search_effort_summary"],
                },
                question=(
                    f"{probe['search_effort_summary']}，但没有找到匹配记录。"
                    "请补充作物类型、确认名称，或换一个更准确的机构/品种名称。"
                ),
                missing=["crop"],
                reason_code="approval_entity_probe_no_hits",
            )

        if bool(upstream.get("cross_crop_allowed", upstream.get("no_crop_broad_query"))):
            return self._success(
                request,
                {
                    **output_base,
                    "selected_tables": list(_APPROVAL_CROP_TABLES.values()),
                    "selected_crops": list(_APPROVAL_CROP_TABLES.keys()),
                    "resolution_reason": "approval_cross_crop_allowed",
                },
            )
        return self._interrupt(
            request,
            output_base,
            question="请补充要查询的作物类型（玉米、水稻、棉花、小麦、大豆）。",
            missing=["crop"],
            reason_code="crop_not_resolved",
            options=list(_APPROVAL_CROP_TABLES.keys()),
        )

    async def _probe_approval_varieties(self, request: CapabilityExecutionRequest, entities: list[dict[str, Any]]) -> dict[str, Any]:
        table_hits: dict[str, dict[str, Any]] = {}
        attempts: list[dict[str, Any]] = []
        match_summary: dict[str, list[dict[str, Any]]] = {tier: [] for tier in _MATCH_TIERS}
        searched_fields: list[str] = []
        for entity in entities[:3]:
            candidate = str(entity.get("text") or "").strip()
            if not candidate:
                continue
            like_value = f"%{candidate}%"
            exact_value = candidate
            field_plan = _entity_field_plan(entity)
            for crop, table in _APPROVAL_CROP_TABLES.items():
                for item in field_plan:
                    field = item["field"]
                    tier = item["tier"]
                    if field not in searched_fields:
                        searched_fields.append(field)
                    sql = _probe_sql(table=table, crop=crop, field=field, tier=tier, like_value=like_value, exact_value=exact_value)
                    guard = await self._guard.execute(
                        CapabilityExecutionRequest(
                            capability_id=request.capability_id,
                            conversation_id=request.conversation_id,
                            task_id=request.task_id,
                            node_id=f"{request.node_id}:probe_guard:{table}:{field}",
                            input_payload={},
                            dependency_outputs={"probe": {"sql": sql, "route_id": "approval_variety_db", "selected_tables": [table], "schema_profile_id": "approval_variety_profile"}},
                            metadata=dict(request.metadata),
                        )
                    )
                    if guard.error is not None:
                        attempts.append({"entity_text": candidate, "field": field, "match_tier": tier, "table": table, "crop": crop, "error_code": guard.error.code, "row_count": 0})
                        continue
                    try:
                        result = await self._adapter.execute_readonly(
                            sql,
                            guard_pass_token=str(guard.output_payload.get("guard_pass_token")),
                            row_retention="head",
                        )
                    except TypeError:
                        result = await self._adapter.execute_readonly(sql, guard_pass_token=str(guard.output_payload.get("guard_pass_token")))
                    row_count = int(getattr(result, "source_row_count", None) or getattr(result, "row_count", 0) or 0)
                    rows = [_annotate_probe_row(dict(row), field=field, tier=tier, entity_text=candidate) for row in list(getattr(result, "rows", ()))[:5]]
                    attempts.append({"entity_text": candidate, "field": field, "match_tier": tier, "table": table, "crop": crop, "row_count": row_count, "sample_rows": rows})
                    if row_count <= 0:
                        continue
                    hit = table_hits.setdefault(
                        table,
                        {
                            "table": table,
                            "crop": crop,
                            "row_count": 0,
                            "matched_fields": [],
                            "match_tiers": [],
                            "entities": [],
                            "sample_rows": [],
                        },
                    )
                    hit["row_count"] = int(hit.get("row_count") or 0) + row_count
                    if field not in hit["matched_fields"]:
                        hit["matched_fields"].append(field)
                    if tier not in hit["match_tiers"]:
                        hit["match_tiers"].append(tier)
                    entity_hit = {"text": candidate, "field": field, "match_tier": tier}
                    if entity_hit not in hit["entities"]:
                        hit["entities"].append(entity_hit)
                    hit["sample_rows"].extend(rows[: max(0, 5 - len(hit["sample_rows"]))])
                    summary_item = {"table": table, "crop": crop, "field": field, "entity_text": candidate}
                    if summary_item not in match_summary[tier]:
                        match_summary[tier].append(summary_item)
        matched_fields = [field for field in searched_fields if any(field == item["field"] for items in match_summary.values() for item in items)]
        match_tiers = [tier for tier in _MATCH_TIERS if match_summary[tier]]
        searched_tables = list(_APPROVAL_CROP_TABLES.values())
        return {
            "entities": entities[:3],
            "candidates": [entity["text"] for entity in entities[:3]],
            "searched_tables": searched_tables,
            "searched_fields": searched_fields,
            "table_hits": list(table_hits.values()),
            "attempts": attempts,
            "match_summary": match_summary,
            "matched_fields": matched_fields,
            "match_tiers": match_tiers,
            "search_effort_summary": _search_effort_summary(
                entities=entities[:3],
                searched_fields=searched_fields,
                searched_tables=searched_tables,
            ),
        }

    def _success(self, request: CapabilityExecutionRequest, output: Mapping[str, Any]) -> CapabilityExecutionResult:
        payload = dict(output)
        artifact = make_artifact(
            name="schema_resolution_snapshot",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=payload,
            summary=f"schema resolution selected {len(payload.get('selected_tables') or [])} tables",
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=payload,
            artifacts=(artifact,),
        )

    def _interrupt(
        self,
        request: CapabilityExecutionRequest,
        output_base: Mapping[str, Any],
        *,
        question: str,
        missing: list[str],
        reason_code: str,
        options: list[str] | None = None,
    ) -> CapabilityExecutionResult:
        payload = {
            **dict(output_base),
            "ok": False,
            "domain_kind": "sql_query",
            "status": "missing_input",
            "needs_user_input": True,
            "answer": question,
            "response_text": question,
            "error": {"type": "missing_input", "message": question},
            "missing": list(missing),
            "presentation": "natural_language",
        }
        digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
        required_fields: dict[str, Any] = {name: {"options": list(options or [])} for name in missing}
        required_fields["_sql_query_resolution"] = {
            "domain_kind": "sql_query",
            "presentation": "natural_language",
            "reason_code": reason_code,
            "resolution_state": {key: payload.get(key) for key in ("original_user_query", "resolved_user_query", "understanding", "entities", "probe_summary", "search_effort_summary") if key in payload},
        }
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=payload,
            interrupt=Interrupt(
                interrupt_id=f"{request.node_id}:interrupt:{reason_code}:{digest}",
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                node_id=request.node_id,
                source_agent=self.capability_id,
                source_message_id=f"{request.node_id}:clarification",
                question=question,
                reason_code=reason_code,
                required_fields=required_fields,
            ),
        )


def _clean_crop(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in _APPROVAL_CROP_TABLES else None


def _mentions_transgenic_owner(value: Any) -> bool:
    text = str(value or "")
    return any(signal in text for signal in _TRANSGENIC_OWNER_SIGNALS)


def _normalized_entities(upstream: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_entities = upstream.get("entities")
    if isinstance(raw_entities, list | tuple):
        entities = [_normalize_entity(item, upstream=upstream, source="llm_entities") for item in raw_entities]
        entities = [entity for entity in entities if entity is not None]
        if entities:
            return _dedupe_entities(entities)

    legacy_candidates = _variety_candidates(upstream)
    if legacy_candidates:
        return [
            _build_entity(
                text=candidate,
                entity_type="variety",
                field_intent="variety_name",
                source="legacy_variety_name_candidates",
            )
            for candidate in legacy_candidates[:3]
        ]

    return _dedupe_entities(_heuristic_entities_from_question(str(upstream.get("user_question") or upstream.get("original_user_query") or "")))


def _normalize_entity(item: Any, *, upstream: Mapping[str, Any], source: str) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    text = _clean_candidate(item.get("text") or item.get("name") or item.get("value") or item.get("entity"))
    if not text:
        return None
    entity_type = _normalize_entity_type(item.get("entity_type"))
    if entity_type is None:
        return None
    field_intent = _normalize_field_intent(item.get("field_intent"))
    if field_intent == "unknown":
        legacy_entity_type = _normalize_entity_type(upstream.get("entity_type"))
        if legacy_entity_type == "organization" and entity_type == "other":
            entity_type = "organization"
    return _build_entity(text=text, entity_type=entity_type, field_intent=field_intent, source=source)


def _normalize_entity_type(value: Any) -> str | None:
    text = str(value or "other").strip().lower()
    aliases = {
        "company": "organization",
        "org": "organization",
        "institution": "organization",
        "variety_name": "variety",
        "approval_num": "approval_number",
        "approval_no": "approval_number",
        "approval_code": "approval_number",
    }
    text = aliases.get(text, text)
    if text in {"region", "year"}:
        return None
    if text in {"variety", "organization", "person", "approval_number", "other"}:
        return text
    return "other"


def _normalize_field_intent(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    aliases = {
        "company": "applicant_or_breeder",
        "organization": "applicant_or_breeder",
        "approval_number": "approval_num",
        "approval_no": "approval_num",
        "approval_code": "approval_num",
    }
    text = aliases.get(text, text)
    return text if text in {"variety_name", "applicant", "breeder", "applicant_or_breeder", "approval_num", "unknown"} else "unknown"


def _build_entity(*, text: str, entity_type: str, field_intent: str, source: str) -> dict[str, Any]:
    entity = {
        "text": text,
        "entity_type": entity_type,
        "field_intent": field_intent,
        "source": source,
    }
    plan = _entity_field_plan(entity)
    entity["primary_fields"] = [item["field"] for item in plan if item["tier"] == "primary"]
    entity["secondary_fields"] = [item["field"] for item in plan if item["tier"] == "secondary"]
    entity["peer_fields"] = [item["field"] for item in plan if item["tier"] == "peer"]
    return entity


def _entity_field_plan(entity: Mapping[str, Any]) -> list[dict[str, str]]:
    entity_type = str(entity.get("entity_type") or "other")
    field_intent = str(entity.get("field_intent") or "unknown")
    if entity_type == "variety" or field_intent == "variety_name":
        return [{"field": "variety_name", "tier": "primary"}]
    if entity_type == "approval_number" or field_intent == "approval_num":
        return [{"field": "approval_num", "tier": "primary"}]
    if field_intent == "applicant":
        return [{"field": "applicant", "tier": "primary"}, {"field": "breeder", "tier": "secondary"}]
    if field_intent == "breeder":
        return [{"field": "breeder", "tier": "primary"}, {"field": "applicant", "tier": "secondary"}]
    if field_intent == "applicant_or_breeder" or entity_type in {"organization", "person"}:
        return [{"field": "applicant", "tier": "peer"}, {"field": "breeder", "tier": "peer"}]
    return [{"field": field, "tier": "peer"} for field in _CORE_UNKNOWN_FIELDS]


def _heuristic_entities_from_question(question: str) -> list[dict[str, Any]]:
    text = str(question or "").strip()
    if not text or _mentions_transgenic_owner(text):
        return []
    approval_num = _extract_approval_number(text)
    if approval_num:
        return [_build_entity(text=approval_num, entity_type="approval_number", field_intent="approval_num", source="heuristic_approval_number")]
    for signal in _APPLICANT_SIGNALS:
        candidate = _candidate_before_signal(text, signal) or _candidate_after_signal(text, signal)
        if candidate:
            return [_build_entity(text=candidate, entity_type=_entity_type_for_text(candidate), field_intent="applicant", source="heuristic_applicant_signal")]
    for signal in _BREEDER_SIGNALS:
        candidate = _candidate_before_signal(text, signal) or _candidate_after_signal(text, signal)
        if candidate:
            return [_build_entity(text=candidate, entity_type=_entity_type_for_text(candidate), field_intent="breeder", source="heuristic_breeder_signal")]
    organization = _extract_organization_like_entity(text)
    if organization:
        return [_build_entity(text=organization, entity_type="organization", field_intent="applicant_or_breeder", source="heuristic_organization")]
    unknown = _extract_unknown_entity(text)
    if unknown:
        return [_build_entity(text=unknown, entity_type="other", field_intent="unknown", source="heuristic_unknown")]
    return []


def _extract_approval_number(text: str) -> str | None:
    explicit = re.search(r"(?:审定编号|审定号|编号)[：:为是\\s]*([A-Za-z0-9\u4e00-\u9fff-]{2,40})", text)
    if explicit:
        return _clean_candidate(explicit.group(1))
    generic = re.search(r"([\u4e00-\u9fff]{0,4}审[A-Za-z0-9\u4e00-\u9fff-]{2,40})", text)
    return _clean_candidate(generic.group(1)) if generic else None


def _candidate_before_signal(text: str, signal: str) -> str | None:
    if signal not in text:
        return None
    before = text.split(signal, 1)[0]
    return _clean_entity_phrase(before)


def _candidate_after_signal(text: str, signal: str) -> str | None:
    if signal not in text:
        return None
    after = text.split(signal, 1)[1]
    after = re.sub(r"^(?:是|为|名称是|名称为|单位是|单位为|[:：])", "", after)
    return _clean_entity_phrase(after)


def _extract_organization_like_entity(text: str) -> str | None:
    match = re.search(r"(.{1,40}?)(?:所有|全部|有哪些|有多少|申请|育成|选育)?(?:审定品种|品种)", text)
    if match:
        candidate = _clean_entity_phrase(match.group(1))
        if candidate and any(hint in candidate for hint in _ORG_HINTS):
            return candidate
    for hint in _ORG_HINTS:
        pattern = rf"([A-Za-z0-9\u4e00-\u9fff]{{1,30}}{re.escape(hint)}[A-Za-z0-9\u4e00-\u9fff]{{0,20}})"
        match = re.search(pattern, text)
        if match:
            candidate = _clean_entity_phrase(match.group(1))
            if candidate:
                return candidate
    return None


def _extract_unknown_entity(text: str) -> str | None:
    quoted = re.search(r"[“\"']([^”\"']{1,40})[”\"']", text)
    if quoted:
        return _clean_entity_phrase(quoted.group(1))
    for match in _VARIETY_CANDIDATE_PATTERN.findall(text):
        cleaned = _clean_candidate(match)
        if cleaned:
            return cleaned
    match = re.search(r"(.{1,30}?)(?:的)?(?:审定品种|品种|审定信息)", text)
    if match:
        return _clean_entity_phrase(match.group(1))
    return None


def _clean_entity_phrase(value: Any) -> str | None:
    text = str(value or "").strip()
    text = re.sub(r"^(?:请|帮我|给我|查询|查一下|查查|看看|看一下|了解一下|统计|列出|找一下|找|一下|再)+", "", text)
    text = re.split(r"(?:的|是|为|有没有|是否|多少|哪些|有什么|所有|全部|审定|品种|信息|详情|近[一二三四五六七八九十0-9]+年|近几年|今年|去年|适合|适宜|种植)", text, maxsplit=1)[0]
    text = re.sub(r"[`'\";\\，。；,?？:：]", "", text).strip()
    if not text or len(text) > 40:
        return None
    if text in {"近五年", "近几年", "今年", "去年", "审定", "品种", "公司", "企业", "机构", "申请者", "育种者"}:
        return None
    if re.fullmatch(r"(?:近)?[一二三四五六七八九十0-9]+年", text):
        return None
    return text


def _entity_type_for_text(text: str) -> str:
    if any(hint in text for hint in _ORG_HINTS):
        return "organization"
    return "person" if len(text) <= 4 else "organization"


def _dedupe_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entity in entities:
        key = (str(entity.get("text")), str(entity.get("entity_type")), str(entity.get("field_intent")))
        if key in seen:
            continue
        result.append(entity)
        seen.add(key)
    return result[:3]


def _match_summary_for_known_tables(entities: list[dict[str, Any]], selected_tables: list[str]) -> dict[str, Any]:
    match_summary: dict[str, list[dict[str, Any]]] = {tier: [] for tier in _MATCH_TIERS}
    matched_fields: list[str] = []
    for table in selected_tables:
        crop = _crop_for_table(table)
        for entity in entities[:3]:
            for item in _entity_field_plan(entity):
                field = item["field"]
                tier = item["tier"]
                summary_item = {"table": table, "crop": crop, "field": field, "entity_text": entity["text"]}
                if summary_item not in match_summary[tier]:
                    match_summary[tier].append(summary_item)
                if field not in matched_fields:
                    matched_fields.append(field)
    match_tiers = [tier for tier in _MATCH_TIERS if match_summary[tier]]
    return {
        "match_summary": match_summary,
        "matched_fields": matched_fields,
        "match_tiers": match_tiers,
        "search_effort_summary": _search_effort_summary(
            entities=entities[:3],
            searched_fields=matched_fields,
            searched_tables=selected_tables,
        ),
    }


def _annotate_probe_row(row: dict[str, Any], *, field: str, tier: str, entity_text: str) -> dict[str, Any]:
    row.setdefault("matched_field", field)
    row.setdefault("match_tier", tier)
    row.setdefault("matched_entity", entity_text)
    return row


def _search_effort_summary(*, entities: list[dict[str, Any]], searched_fields: list[str], searched_tables: list[str]) -> str:
    entity_text = "、".join(f"“{entity.get('text')}”" for entity in entities if entity.get("text")) or "用户提供的名称"
    field_label = "、".join(_field_display(field) for field in searched_fields) or "品种名、申请者、育种者"
    if set(searched_tables) == set(_APPROVAL_CROP_TABLES.values()):
        table_label = "玉米、水稻、棉花、小麦、大豆五个审定品种表"
    else:
        table_label = "、".join(
            f"{_APPROVAL_CROP_LABELS.get(_crop_for_table(table) or '', table)}审定品种表"
            for table in searched_tables
        )
    return f"我已在{table_label}中，按{field_label}字段查找{entity_text}"


def _field_display(field: str) -> str:
    return {
        "variety_name": "品种名",
        "applicant": "申请者",
        "breeder": "育种者",
        "approval_num": "审定编号",
    }.get(str(field), str(field))


def _crop_for_table(table: str) -> str | None:
    for crop, mapped_table in _APPROVAL_CROP_TABLES.items():
        if mapped_table == table:
            return crop
    return None


def _variety_candidates(upstream: Mapping[str, Any]) -> list[str]:
    raw = upstream.get("variety_name_candidates") or []
    candidates: list[str] = []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list | tuple):
        for item in raw:
            cleaned = _clean_candidate(item)
            if cleaned:
                candidates.append(cleaned)
    question = str(upstream.get("user_question") or upstream.get("original_user_query") or "")
    for match in _VARIETY_CANDIDATE_PATTERN.findall(question):
        cleaned = _clean_candidate(match)
        if cleaned:
            candidates.append(cleaned)
    return list(dict.fromkeys(candidates))[:3]


def _clean_candidate(value: Any) -> str | None:
    text = str(value or "").strip()
    for word in _GENERIC_VARIETY_WORDS:
        text = text.replace(word, "")
    text = re.split(r"(?:的|是|为|有没有|是否|多少|哪些|有什么)", text, maxsplit=1)[0]
    text = re.sub(r"[`'\";\\]", "", text).strip()
    if not text or len(text) > 40:
        return None
    return text


def _sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _probe_sql(*, table: str, crop: str, field: str, tier: str, like_value: str, exact_value: str) -> str:
    if table not in _APPROVAL_CROP_TABLES.values():
        raise ValueError(f"Probe table is not allowlisted: {table}")
    if field not in _PROBE_FIELDS:
        raise ValueError(f"Probe field is not allowlisted: {field}")
    if tier not in _MATCH_TIERS:
        raise ValueError(f"Probe tier is not allowlisted: {tier}")
    return (
        f"SELECT {_sql_literal(table)} AS source_table, "
        f"{_sql_literal(crop)} AS source_crop, "
        f"{_sql_literal(field)} AS matched_field, "
        f"{_sql_literal(tier)} AS match_tier, "
        "crop_name, variety_name, approval_num, applicant, breeder, year "
        f"FROM {table} "
        f"WHERE {field} LIKE {_sql_literal(like_value)} "
        "ORDER BY "
        f"CASE WHEN {field} = {_sql_literal(exact_value)} THEN 0 ELSE 1 END, "
        "year DESC, variety_name ASC, approval_num ASC"
    )
