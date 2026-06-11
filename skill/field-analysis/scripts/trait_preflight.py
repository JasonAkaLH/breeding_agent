from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Mapping


WIDE_FIXED_COLUMNS = ("loc_id", "rep_num", "ranges", "pass", "entry_id", "ped_id", "check_type")
TEST_CHECK_MARKERS = {"", "0", "0.0", "test", "tester", "non-check", "non_check", "material", "entry", "测试", "测试材料", "试验材料", "非对照", "材料"}
CHECK_MARKERS = {"1", "1.0", "ck", "check", "control", "yes", "true", "对照", "对照材料"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _to_float(value: str) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def trait_columns(fields: list[str]) -> list[str]:
    if "check_type" not in fields:
        return []
    return fields[fields.index("check_type") + 1 :]


def metadata_columns(fields: list[str]) -> list[str]:
    if "check_type" not in fields:
        return [field for field in fields if field in WIDE_FIXED_COLUMNS]
    return fields[: fields.index("check_type") + 1]


def normalize_check_type_value(value: Any) -> tuple[str, str]:
    raw = _text(value)
    key = raw.lower()
    if key in TEST_CHECK_MARKERS:
        return "", "test"
    if key in CHECK_MARKERS:
        return "check", "check"
    numeric = _to_float(key)
    if numeric == 0:
        return "", "test"
    if numeric == 1:
        return "check", "check"
    if raw:
        return raw, "check_by_nonempty"
    return "", "test"


def check_type_summary(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    raw_counts: Counter[str] = Counter()
    normalized_counts: Counter[str] = Counter()
    mapping: dict[str, str] = {}
    for row in records:
        raw = _text(row.get("check_type"))
        normalized, label = normalize_check_type_value(raw)
        raw_counts[raw] += 1
        normalized_counts[label] += 1
        mapping[raw] = label
    return {
        "raw_counts": dict(raw_counts),
        "normalized_counts": dict(normalized_counts),
        "mapping": mapping,
        "normalization_note": "`0`/空值/test 按测试材料处理；`1`/ck/check/对照 按对照材料处理；其他非空值按对照材料处理。",
    }


def summarize_categorical_trait(records: list[Mapping[str, Any]], trait: str, *, top_n: int = 8) -> dict[str, Any]:
    values: Counter[str] = Counter()
    by_material: dict[str, Counter[str]] = defaultdict(Counter)
    by_location: dict[str, Counter[str]] = defaultdict(Counter)
    nonempty = 0
    for row in records:
        value = _text(row.get(trait))
        if not value:
            continue
        nonempty += 1
        values[value] += 1
        material = _text(row.get("ped_id")) or "NA"
        location = _text(row.get("loc_id")) or "NA"
        by_material[material][value] += 1
        by_location[location][value] += 1
    total = len(records)
    return {
        "trait": trait,
        "observations": total,
        "nonempty": nonempty,
        "missing": total - nonempty,
        "missing_pct": round((total - nonempty) / total * 100, 2) if total else 0,
        "category_count": len(values),
        "top_categories": [{"category": key, "count": count} for key, count in values.most_common(top_n)],
        "top_by_material": [
            {"ped_id": ped_id, "top_category": counter.most_common(1)[0][0], "count": counter.most_common(1)[0][1]}
            for ped_id, counter in list(by_material.items())[:top_n]
            if counter
        ],
        "top_by_location": [
            {"loc_id": loc_id, "top_category": counter.most_common(1)[0][0], "count": counter.most_common(1)[0][1]}
            for loc_id, counter in list(by_location.items())[:top_n]
            if counter
        ],
    }


def classify_trait(records: list[Mapping[str, Any]], trait: str, *, min_numeric: int = 3, max_categorical_levels: int = 50) -> dict[str, Any]:
    total = len(records)
    nonempty = 0
    numeric = 0
    bad_values: list[dict[str, Any]] = []
    numeric_values: list[float] = []
    categories: Counter[str] = Counter()
    for index, row in enumerate(records, start=2):
        value = _text(row.get(trait))
        if not value:
            continue
        nonempty += 1
        categories[value] += 1
        number = _to_float(value)
        if number is None:
            if len(bad_values) < 5:
                bad_values.append({"row": index, "value": value})
            continue
        numeric += 1
        numeric_values.append(number)

    missing_pct = round((total - nonempty) / total * 100, 2) if total else 0
    distinct_numeric = len(set(numeric_values))
    distinct_categories = len(categories)
    base = {
        "trait": trait,
        "observations": total,
        "nonempty": nonempty,
        "numeric": numeric,
        "non_numeric": nonempty - numeric,
        "missing": total - nonempty,
        "missing_pct": missing_pct,
        "distinct_numeric": distinct_numeric,
        "distinct_categories": distinct_categories,
        "min": min(numeric_values) if numeric_values else None,
        "max": max(numeric_values) if numeric_values else None,
        "bad_examples": bad_values,
    }
    if nonempty == 0:
        return {**base, "kind": "skipped", "status": "empty", "reason": "全空列，无法分析。"}
    if numeric == nonempty:
        if numeric < min_numeric:
            return {**base, "kind": "skipped", "status": "too_few_numeric", "reason": "可用数值观测过少，无法进入模型分析。"}
        if distinct_numeric < 2:
            return {**base, "kind": "skipped", "status": "constant", "reason": "所有非空数值相同，无法进行差异分析。"}
        return {**base, "kind": "numeric", "status": "numeric_candidate", "reason": "满足连续数值性状预检。"}
    if distinct_categories <= max_categorical_levels:
        return {**base, "kind": "categorical", "status": "categorical", "reason": "非数值/分类性状，仅做类别描述统计，不进入 ANOVA/LSD/BLUP。"}
    return {**base, "kind": "skipped", "status": "non_numeric_high_cardinality", "reason": "非数值且类别过多，暂不做自动分析。"}


def build_trait_preflight(records: list[Mapping[str, Any]], fields: list[str]) -> dict[str, Any]:
    traits = trait_columns(fields)
    classifications = [classify_trait(records, trait) for trait in traits]
    numeric_traits = [item["trait"] for item in classifications if item["kind"] == "numeric"]
    categorical_traits = [item["trait"] for item in classifications if item["kind"] == "categorical"]
    skipped_traits = [
        {"trait": item["trait"], "status": item["status"], "reason": item["reason"]}
        for item in classifications
        if item["kind"] == "skipped"
    ]
    categorical_summary = {
        trait: summarize_categorical_trait(records, trait)
        for trait in categorical_traits
    }
    return {
        "format": "field-analysis-trait-preflight-v1",
        "input_observations": len(records),
        "input_columns": len(fields),
        "metadata_columns": metadata_columns(fields),
        "input_traits": traits,
        "numeric_traits": numeric_traits,
        "categorical_traits": categorical_traits,
        "skipped_traits": skipped_traits,
        "classifications": classifications,
        "categorical_trait_summary": categorical_summary,
        "check_type": check_type_summary(records),
        "counts": {
            "input_traits": len(traits),
            "numeric_traits": len(numeric_traits),
            "categorical_traits": len(categorical_traits),
            "skipped_traits": len(skipped_traits),
        },
    }


def prepare_records_for_numeric_backend(records: list[Mapping[str, Any]], preflight: Mapping[str, Any]) -> list[dict[str, str]]:
    metadata = [str(item) for item in preflight.get("metadata_columns") or []]
    numeric_traits = [str(item) for item in preflight.get("numeric_traits") or []]
    keep = metadata + [trait for trait in numeric_traits if trait not in metadata]
    prepared: list[dict[str, str]] = []
    for row in records:
        out: dict[str, str] = {}
        for column in keep:
            value = _text(row.get(column))
            if column == "check_type":
                value, _ = normalize_check_type_value(value)
            out[column] = value
        prepared.append(out)
    return prepared
