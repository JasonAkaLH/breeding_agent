from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping

_CONTRACT_NAME = "sqlquery.constraint_contract"
_APPROVAL_ENTITY_FIELDS = {"variety_name", "applicant", "breeder", "approval_num"}
_ALLOWED_STRUCTURED_FIELDS = _APPROVAL_ENTITY_FIELDS | {"suitable_area", "suit_area", "crop_name", "year"}
_ALLOWED_OPERATORS = {"=", "LIKE", "BETWEEN", "IN", ">=", "<=", "COUNT", "LIMIT", "ORDER_BY"}
_PROVINCES = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
)
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CROP_TABLES = {
    "玉米": "corn_varieties",
    "水稻": "rice_varieties",
    "棉花": "cotton_varieties",
    "小麦": "wheat_varieties",
    "大豆": "soybean_varieties",
}


def build_query_constraints(
    context: Mapping[str, Any],
    *,
    current_year: int | None = None,
    structured_llm_output: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build SQLQuery's internal query-constraint contract from resolved schema context."""

    question = str(context.get("user_question") or "")
    source_question = str(context.get("original_user_query") or question)
    resolved_question = str(context.get("resolved_user_query") or question)
    text = resolved_question or source_question or question
    year = _safe_current_year(current_year)
    selected_tables = _string_list(context.get("selected_tables"))
    selected_columns = {
        str(table): [str(column) for column in list(columns)]
        for table, columns in dict(context.get("selected_columns", {})).items()
    }

    required: list[dict[str, Any]] = []
    soft: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    extraction_sources: list[str] = []

    def add_required(item: dict[str, Any]) -> dict[str, Any]:
        item = _finalize_constraint(item, selected_tables=selected_tables, selected_columns=selected_columns)
        if not item.get("tables") and item.get("scope") not in {"query_level", "aggregate"}:
            soft.append({**item, "required": False, "demotion_reason": "field_not_in_selected_schema"})
            return item
        key = _constraint_key(item)
        if all(_constraint_key(existing) != key for existing in required):
            required.append(item)
        return item

    def mark(source: str) -> None:
        if source not in extraction_sources:
            extraction_sources.append(source)

    clarification_needed: dict[str, Any] | None = None

    temporal_items, temporal_soft, temporal_clarification = _resolve_temporal_conflicts(
        _deterministic_temporal_constraints(text, current_year=year)
    )
    soft.extend(temporal_soft)
    if temporal_clarification:
        clarification_needed = temporal_clarification
        mark("deterministic")
    for item in temporal_items:
        add_required(item)
        mark("deterministic")
    for item in _deterministic_approval_number_constraints(text):
        add_required(item)
        mark("deterministic")
    for item in _deterministic_region_constraints(text):
        add_required(item)
        mark("deterministic")
    for item in _deterministic_crop_constraints(text):
        add_required(item)
        mark("deterministic")
    for item in _deterministic_result_shape_constraints(text):
        add_required(item)
        mark("deterministic")

    entity_items, entity_groups = _entity_constraints(context, text)
    for item in entity_items:
        add_required(item)
        mark(str(item.get("source") or "entity_probe"))
    groups.extend(_normalize_groups(entity_groups, required))

    structured = validate_structured_extractor_output(structured_llm_output or {}, selected_columns=selected_columns)
    for item in structured.get("suggested_constraints", []):
        if item.get("required"):
            add_required(item)
        else:
            soft.append(item)
        mark("structured_llm")

    groups = _dedupe_groups(groups)
    summary = _constraint_summary(required, groups)
    return {
        "contract": _CONTRACT_NAME,
        "source_question": source_question,
        "resolved_question": resolved_question,
        "required_constraints": required,
        "soft_constraints": soft,
        "constraint_groups": groups,
        "constraint_summary": summary,
        "coverage_requirements": (
            "global_filter constraints must be present in every relevant SELECT/UNION branch; "
            "branch_union members must be represented as traceable UNION ALL branches; "
            "query_level order/limit must apply to the final result."
        ),
        "extraction_sources": extraction_sources,
        "clarification_needed": clarification_needed,
    }


def validate_structured_extractor_output(
    payload: Mapping[str, Any],
    *,
    selected_columns: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate optional LLM structured suggestions and return safe, non-authoritative items."""

    if not isinstance(payload, Mapping):
        return {"entities": [], "suggested_constraints": [], "discarded": ["payload_not_mapping"]}
    selected_columns = selected_columns or {}
    allowed_fields = set(_ALLOWED_STRUCTURED_FIELDS)
    for columns in selected_columns.values():
        allowed_fields.update(str(column) for column in list(columns))

    safe_constraints: list[dict[str, Any]] = []
    discarded: list[dict[str, Any] | str] = []
    raw_constraints = payload.get("suggested_constraints") or []
    if not isinstance(raw_constraints, list | tuple):
        raw_constraints = []
    for index, raw in enumerate(raw_constraints):
        if not isinstance(raw, Mapping):
            discarded.append({"index": index, "reason": "constraint_not_mapping"})
            continue
        field = _field_from_intent(str(raw.get("field") or raw.get("field_intent") or ""))
        operator = str(raw.get("operator") or "").strip().upper()
        source_span = str(raw.get("source_span") or "").strip()
        confidence = str(raw.get("confidence") or "").strip().lower()
        value = raw.get("value")
        if field not in allowed_fields:
            discarded.append({"index": index, "reason": "unknown_field", "field": field})
            continue
        if operator not in _ALLOWED_OPERATORS:
            discarded.append({"index": index, "reason": "unknown_operator", "operator": operator})
            continue
        if confidence not in {"high", "medium"}:
            discarded.append({"index": index, "reason": "low_confidence", "confidence": confidence})
            continue
        if not source_span:
            discarded.append({"index": index, "reason": "missing_source_span"})
            continue
        safe_constraints.append(
            {
                "id": _constraint_id(str(raw.get("kind") or "structured"), field, operator, value),
                "kind": str(raw.get("kind") or _kind_for_field(field)),
                "field": field,
                "operator": operator,
                "value": value,
                "required": confidence == "high",
                "scope": _scope_for_operator(operator, field),
                "tables": [],
                "source": "structured_llm",
                "source_span": source_span,
                "confidence": confidence,
            }
        )
    return {
        "entities": [item for item in list(payload.get("entities") or []) if isinstance(item, Mapping)],
        "suggested_constraints": safe_constraints,
        "discarded": discarded,
        "clarification_needed": payload.get("clarification_needed"),
    }


def summarize_constraints(query_constraints: Mapping[str, Any] | None) -> str:
    if not isinstance(query_constraints, Mapping):
        return ""
    return str(query_constraints.get("constraint_summary") or "").strip()


def _deterministic_temporal_constraints(text: str, *, current_year: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    consumed_spans: list[tuple[int, int]] = []
    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})\s*(?:到|至|[-—~～])\s*((?:19|20)\d{2})\s*年?", text):
        start, end = int(match.group(1)), int(match.group(2))
        if start > end:
            start, end = end, start
        if _valid_year(start, current_year) and _valid_year(end, current_year):
            consumed_spans.append(match.span())
            result.append(_constraint("temporal", "year", "BETWEEN", [start, end], match.group(0), scope="global_filter"))

    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})\s*年", text):
        if any(match.start() >= start and match.end() <= end for start, end in consumed_spans):
            continue
        value = int(match.group(1))
        if _valid_year(value, current_year):
            result.append(_constraint("temporal", "year", "=", value, match.group(0), scope="global_filter"))

    for match in re.finditer(r"近\s*([一二两三四五六七八九十\d]{1,2})\s*年", text):
        count = _parse_small_number(match.group(1))
        if count and 1 <= count <= 30:
            result.append(_constraint("temporal", "year", ">=", current_year - count + 1, match.group(0), scope="global_filter"))

    if "今年" in text:
        result.append(_constraint("temporal", "year", "=", current_year, "今年", scope="global_filter"))
    if "去年" in text:
        result.append(_constraint("temporal", "year", "=", current_year - 1, "去年", scope="global_filter"))
    return result


def _resolve_temporal_conflicts(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    exact_years = [
        int(item["value"])
        for item in items
        if str(item.get("field")) == "year"
        and str(item.get("operator")).upper() == "="
        and isinstance(item.get("value"), int)
    ]
    distinct_years = list(dict.fromkeys(exact_years))
    if len(distinct_years) <= 1:
        return items, [], None

    conflicting = [dict(item, required=False, demotion_reason="conflicting_temporal_constraints") for item in items]
    years_text = "、".join(str(year) for year in distinct_years)
    return (
        [],
        conflicting,
        {
            "reason": "conflicting_temporal_constraints",
            "missing": ["year"],
            "question": f"你提到了多个年份（{years_text}）。请确认要查询哪一年，或改成明确的年份范围。",
            "source_spans": [str(item.get("source_span") or item.get("value")) for item in items if item.get("operator") == "="],
            "candidate_years": distinct_years,
        },
    )


def _deterministic_approval_number_constraints(text: str) -> list[dict[str, Any]]:
    result = []
    pattern = r"(?:国审|省审|京审|津审|冀审|晋审|蒙审|辽审|吉审|黑审|沪审|苏审|浙审|皖审|闽审|赣审|鲁审|豫审|鄂审|湘审|粤审|桂审|琼审|渝审|川审|黔审|滇审|藏审|陕审|甘审|青审|宁审|新审)[\u4e00-\u9fffA-Za-z]*\d{4,}"
    for match in re.finditer(pattern, text):
        result.append(_constraint("approval_number", "approval_num", "LIKE", match.group(0), match.group(0), scope="global_filter"))
    return result


def _deterministic_region_constraints(text: str) -> list[dict[str, Any]]:
    result = []
    region_context = any(word in text for word in ("适合", "适宜", "适种", "种植", "区域", "地区"))
    if not region_context:
        return result
    for province in _PROVINCES:
        if province in text:
            result.append(_constraint("region", "suitable_area", "LIKE", province, province, scope="global_filter"))
    return result


def _deterministic_crop_constraints(text: str) -> list[dict[str, Any]]:
    result = []
    for crop, table in _CROP_TABLES.items():
        if crop in text:
            item = _constraint("crop", "crop_name", "LIKE", crop, crop, scope="global_filter")
            item["tables"] = [table]
            result.append(item)
    return result


def _deterministic_result_shape_constraints(text: str) -> list[dict[str, Any]]:
    result = []
    if any(keyword in text for keyword in ("有多少", "多少个", "数量", "几条")):
        result.append(_constraint("aggregate", "*", "COUNT", "*", "数量", scope="aggregate"))
    limit_match = re.search(r"(?:前|最前|前面)\s*(\d{1,3})\s*(?:个|条|项|种)?", text)
    if limit_match:
        result.append(_constraint("limit", "*", "LIMIT", int(limit_match.group(1)), limit_match.group(0), scope="query_level"))
    else:
        generic_limit_match = re.search(r"(?<!\d)(\d{1,3})\s*(?:条记录|条|个品种|个)", text)
        if generic_limit_match:
            result.append(
                _constraint("limit", "*", "LIMIT", int(generic_limit_match.group(1)), generic_limit_match.group(0), scope="query_level")
            )
    if any(keyword in text for keyword in ("最新", "最近")):
        result.append(_constraint("order", "year", "ORDER_BY", {"direction": "DESC"}, "最新/最近", scope="query_level"))
    return result


def _entity_constraints(context: Mapping[str, Any], text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    match_summary = context.get("match_summary")
    if isinstance(match_summary, Mapping) and any(match_summary.get(tier) for tier in ("primary", "secondary", "peer")):
        peer_ids: list[str] = []
        peer_text = ""
        for tier in ("primary", "secondary", "peer"):
            for entry in list(match_summary.get(tier) or []):
                if not isinstance(entry, Mapping):
                    continue
                field = _field_from_intent(str(entry.get("field") or ""))
                entity_text = str(entry.get("entity_text") or entry.get("text") or "").strip()
                if field not in _APPROVAL_ENTITY_FIELDS or not entity_text:
                    continue
                item = _constraint(_kind_for_field(field), field, "LIKE", entity_text, entity_text, scope="branch_filter", source="entity_probe")
                item["match_tier"] = tier
                if entry.get("table"):
                    item["tables"] = [str(entry.get("table"))]
                items.append(item)
                if tier == "peer":
                    peer_ids.append(item["id"])
                    peer_text = peer_text or entity_text
        if len(peer_ids) >= 2:
            groups.append(_branch_group(peer_text or "entity", peer_ids, match_tier="peer"))
        return items, groups

    explicit = _explicit_role_entity(text)
    if explicit is not None:
        entity_text, primary_field, secondary_field, source_span = explicit
        primary = _constraint("entity", primary_field, "LIKE", entity_text, source_span, scope="branch_filter", source="deterministic")
        primary["match_tier"] = "primary"
        secondary = _constraint("entity", secondary_field, "LIKE", entity_text, source_span, scope="branch_filter", source="deterministic")
        secondary["match_tier"] = "secondary"
        items.extend([primary, secondary])
        groups.append(_branch_group(entity_text, [primary["id"], secondary["id"]], match_tier="primary_secondary"))
        return items, groups

    entity_text = _peer_entity_text(context, text)
    if entity_text:
        applicant = _constraint("entity", "applicant", "LIKE", entity_text, entity_text, scope="branch_filter", source="deterministic")
        applicant["match_tier"] = "peer"
        breeder = _constraint("entity", "breeder", "LIKE", entity_text, entity_text, scope="branch_filter", source="deterministic")
        breeder["match_tier"] = "peer"
        items.extend([applicant, breeder])
        groups.append(_branch_group(entity_text, [applicant["id"], breeder["id"]], match_tier="peer"))
    return items, groups


def _explicit_role_entity(text: str) -> tuple[str, str, str, str] | None:
    applicant_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,40})(?:作为)?(?:申请者|申请单位|申请|申报)", text)
    if applicant_match:
        entity = _clean_entity_text(applicant_match.group(1))
        if entity:
            return entity, "applicant", "breeder", applicant_match.group(0)
    breeder_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,40})(?:作为)?(?:育种者|选育|培育)", text)
    if breeder_match:
        entity = _clean_entity_text(breeder_match.group(1))
        if entity:
            return entity, "breeder", "applicant", breeder_match.group(0)
    return None


def _peer_entity_text(context: Mapping[str, Any], text: str) -> str:
    for entity in list(context.get("entities") or []):
        if not isinstance(entity, Mapping):
            continue
        field_intent = _field_from_intent(str(entity.get("field_intent") or ""))
        entity_type = str(entity.get("entity_type") or "").strip().lower()
        value = str(entity.get("text") or "").strip()
        if _meaningful_entity_text(value) and (field_intent == "applicant_or_breeder" or entity_type in {"organization", "person"}):
            return value
    patterns = [
        r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,40})(?:(?:19|20)\d{2}年)?(?:都)?审定",
        r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,40})(?:有哪些|有什么|都有什么)品种",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            entity = _clean_entity_text(match.group(1))
            if _meaningful_entity_text(entity) and not any(crop in entity for crop in _CROP_TABLES):
                return entity
    return ""


def _clean_entity_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"近[一二两三四五六七八九十\d]{1,2}年", "", text)
    text = re.sub(r"(?:19|20)\d{2}年", "", text)
    for token in ("今年", "去年", "请", "帮我", "给我", "查询", "查一下", "在", "的", "都", "哪些", "什么", "品种", "最新", "最近"):
        text = text.replace(token, "")
    return text.strip(" ，。；,;?？、")[:80]


def _meaningful_entity_text(value: str) -> bool:
    text = str(value or "").strip(" ，。；,;?？、")
    if len(text) < 2:
        return False
    if re.fullmatch(r"(?:和|及|与|以及|或|或者|还是|并且)+", text):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def _finalize_constraint(item: dict[str, Any], *, selected_tables: list[str], selected_columns: Mapping[str, list[str]]) -> dict[str, Any]:
    item = dict(item)
    field = str(item.get("field") or "")
    scope = str(item.get("scope") or "global_filter")
    if item.get("tables"):
        tables = [table for table in _string_list(item.get("tables")) if not selected_tables or table in selected_tables]
    elif scope in {"query_level", "aggregate"}:
        tables = list(selected_tables)
    else:
        tables = [table for table in selected_tables if _field_for_table(field, table, selected_columns)]
    item["tables"] = tables
    if field == "suitable_area":
        field_by_table = {
            table: _field_for_table(field, table, selected_columns)
            for table in tables
            if _field_for_table(field, table, selected_columns)
        }
        if field_by_table:
            item["field_by_table"] = field_by_table
    item.setdefault("required", True)
    item.setdefault("confidence", "high")
    item.setdefault("group_id", None)
    item.setdefault("match_tier", None)
    return item


def _field_for_table(field: str, table: str, selected_columns: Mapping[str, list[str]]) -> str | None:
    columns = {str(column) for column in list(selected_columns.get(table, []))}
    if field in {"*", ""}:
        return field
    if field in columns:
        return field
    if field == "suitable_area" and "suit_area" in columns:
        return "suit_area"
    if field == "characteristics" and "chars" in columns:
        return "chars"
    return None


def _normalize_groups(groups: list[dict[str, Any]], constraints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known_ids = {str(item.get("id")) for item in constraints}
    normalized: list[dict[str, Any]] = []
    for group in groups:
        members = [member for member in _string_list(group.get("members")) if member in known_ids]
        if len(members) < 2:
            continue
        normalized.append({**group, "members": members, "required": True})
    return normalized


def _dedupe_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for group in groups:
        key = (str(group.get("mode")), tuple(_string_list(group.get("members"))))
        if key in seen:
            continue
        result.append(group)
        seen.add(key)
    return result


def _branch_group(entity_text: str, members: list[str], *, match_tier: str) -> dict[str, Any]:
    return {
        "id": _constraint_id("group", "applicant_or_breeder", "BRANCH_UNION", entity_text),
        "kind": "entity_field_group",
        "mode": "branch_union",
        "required": True,
        "members": members,
        "compile_policy": "compile_each_member_as_union_all_branch",
        "answer_policy": "report primary hits and attached secondary/peer hits separately",
        "match_tier": match_tier,
    }


def _constraint(kind: str, field: str, operator: str, value: Any, source_span: str, *, scope: str, source: str = "deterministic") -> dict[str, Any]:
    return {
        "id": _constraint_id(kind, field, operator, value),
        "kind": kind,
        "field": field,
        "operator": operator,
        "value": value,
        "required": True,
        "scope": scope,
        "tables": [],
        "source": source,
        "source_span": source_span,
        "confidence": "high",
    }


def _constraint_id(kind: str, field: str, operator: str, value: Any) -> str:
    raw = f"{kind}_{field}_{operator}_{value}"
    safe = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "_", raw).strip("_").lower()
    return f"c_{safe[:80]}" if not safe.startswith("group") else f"g_{safe[:80]}"


def _constraint_key(item: Mapping[str, Any]) -> tuple[str, str, str, str, str | None]:
    return (
        str(item.get("kind")),
        str(item.get("field")),
        str(item.get("operator")),
        str(item.get("value")),
        str(item.get("match_tier")) if item.get("match_tier") else None,
    )


def _constraint_summary(required: list[dict[str, Any]], groups: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in required:
        kind = item.get("kind")
        field = item.get("field")
        operator = item.get("operator")
        value = item.get("value")
        if kind == "temporal":
            if operator == "BETWEEN" and isinstance(value, list) and len(value) == 2:
                parts.append(f"年份在 {value[0]} 到 {value[1]} 之间")
            elif operator == ">=":
                parts.append(f"年份不早于 {value}")
            elif operator == "=":
                parts.append(f"年份为 {value}")
        elif kind == "region":
            parts.append(f"适种区域包含 {value}")
        elif kind == "approval_number":
            parts.append(f"审定编号包含 {value}")
        elif kind == "entity" and field in {"applicant", "breeder"}:
            tier = item.get("match_tier")
            role = "申请者" if field == "applicant" else "育种者"
            suffix = "主命中" if tier == "primary" else "附带命中" if tier == "secondary" else "命中"
            parts.append(f"{role}包含 {value}（{suffix}）")
        elif kind == "aggregate":
            parts.append("返回数量统计")
        elif kind == "limit":
            parts.append(f"最多返回 {value} 条")
        elif kind == "order":
            parts.append("按年份倒序")
    if groups:
        parts.append("多字段实体查询按独立分支保留命中字段来源")
    return "；".join(dict.fromkeys(part for part in parts if part))


def _field_from_intent(value: str) -> str:
    normalized = value.strip().lower()
    mapping = {
        "organization": "applicant_or_breeder",
        "company": "applicant_or_breeder",
        "person": "applicant_or_breeder",
        "approval_number": "approval_num",
        "approval_no": "approval_num",
        "approval_code": "approval_num",
    }
    return mapping.get(normalized, normalized)


def _kind_for_field(field: str) -> str:
    if field == "year":
        return "temporal"
    if field in {"applicant", "breeder"}:
        return "entity"
    if field == "approval_num":
        return "approval_number"
    if field == "variety_name":
        return "variety_name"
    if field in {"suitable_area", "suit_area"}:
        return "region"
    if field == "crop_name":
        return "crop"
    return "structured"


def _scope_for_operator(operator: str, field: str) -> str:
    if operator in {"LIMIT", "ORDER_BY"}:
        return "query_level"
    if operator == "COUNT":
        return "aggregate"
    if field in _APPROVAL_ENTITY_FIELDS:
        return "branch_filter"
    return "global_filter"


def _safe_current_year(current_year: int | None) -> int:
    if current_year is not None:
        return int(current_year)
    return date.today().year


def _valid_year(value: int, current_year: int) -> bool:
    return 1900 <= value <= current_year + 1


def _parse_small_number(value: str) -> int | None:
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text in _CHINESE_NUMBERS:
        return _CHINESE_NUMBERS[text]
    if len(text) == 2 and text[0] == "十" and text[1] in _CHINESE_NUMBERS:
        return 10 + _CHINESE_NUMBERS[text[1]]
    if len(text) == 2 and text[1] == "十" and text[0] in _CHINESE_NUMBERS:
        return _CHINESE_NUMBERS[text[0]] * 10
    if len(text) == 3 and text[1] == "十" and text[0] in _CHINESE_NUMBERS and text[2] in _CHINESE_NUMBERS:
        return _CHINESE_NUMBERS[text[0]] * 10 + _CHINESE_NUMBERS[text[2]]
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result
