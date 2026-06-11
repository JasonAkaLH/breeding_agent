#!/usr/bin/env python
"""Extract lightweight customer-facing views from rice gene_check JSON."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence


def load_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def review_note_text(data: dict, value: str) -> str:
    codes = data.get("metadata", {}).get("review_note_codes") or data.get("review_note_codes") or {}
    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
    if not parts:
        return ""
    return "; ".join(codes.get(part, part) for part in parts)


def get_samples(data: dict) -> List[str]:
    metadata_samples = data.get("metadata", {}).get("samples") or []
    if metadata_samples:
        return list(metadata_samples)
    return list((data.get("samples") or {}).keys())


def choose_sample(samples: Sequence[str], requested: Optional[str], seed: Optional[int]) -> str:
    if not samples:
        raise SystemExit("No samples found in gene_check JSON.")
    if requested:
        if requested not in samples:
            raise SystemExit(f"Sample not found: {requested}. Available: {', '.join(samples)}")
        return requested
    rng = random.Random(seed)
    return rng.choice(list(samples))


def row_from_fields(fields: Sequence[str], values: Sequence[object]) -> dict:
    return {
        field: values[i] if i < len(values) else ""
        for i, field in enumerate(fields)
    }


def catalog_rows(data: dict) -> List[dict]:
    fields = data.get("metadata", {}).get("qtn_catalog_fields") or data.get("qtn_catalog_fields") or []
    catalog = data.get("qtn_catalog") or []
    rows = []
    for item in catalog:
        if isinstance(item, dict):
            rows.append(dict(item))
        else:
            rows.append(row_from_fields(fields, item))
    return rows


def expand_catalog_matrix(data: dict, payload: dict) -> List[dict]:
    catalog = catalog_rows(data)
    call_fields = data.get("metadata", {}).get("sample_call_fields") or data.get("sample_call_fields") or []
    calls = payload.get("calls") or []
    rows = []
    for index, call_values in enumerate(calls):
        row = dict(catalog[index]) if index < len(catalog) else {}
        if isinstance(call_values, dict):
            row.update(call_values)
        else:
            row.update(row_from_fields(call_fields, call_values))
        rows.append(row)
    return rows


def sample_calls(data: dict, sample: str) -> List[dict]:
    samples = data.get("samples") or {}
    payload = samples.get(sample)
    if not payload:
        raise SystemExit(f"Sample not found in payload: {sample}")
    if "calls" in payload:
        return expand_catalog_matrix(data, payload)

    qtn_calls = payload.get("qtn_calls") or []
    fields = data.get("metadata", {}).get("qtn_call_fields") or []
    result = []
    for item in qtn_calls:
        if isinstance(item, dict):
            result.append(dict(item))
        else:
            result.append(row_from_fields(fields, item))
    return result


def trait_summary_for_sample(data: dict, sample: str) -> List[dict]:
    return [
        row
        for row in data.get("trait_summary", [])
        if row.get("sample") == sample
    ]


def favorable_calls(calls: Sequence[dict]) -> List[dict]:
    return [
        row
        for row in calls
        if str(row.get("favorable_detected_variant", "")) == "1"
        and row.get("sample_genotype_type", row.get("检测材料基因型", "")) == "突变型"
    ]


def material_list_markdown(data: dict) -> str:
    samples = get_samples(data)
    lines = [
        f"当前水稻体检共有 {len(samples)} 份材料。",
        "",
        "| 序号 | 材料名称 |",
        "|---:|---|",
    ]
    for i, sample in enumerate(samples, start=1):
        lines.append(f"| {i} | {sample} |")
    return "\n".join(lines)


def sample_summary_markdown(data: dict, sample: str, random_selected: bool) -> str:
    summary = sample_summary_parts(data, sample, random_selected)
    return "\n".join(summary)


def sample_summary_parts(data: dict, sample: str, random_selected: bool) -> List[str]:
    calls = sample_calls(data, sample)
    favorable = favorable_calls(calls)
    qtn_total = int(data.get("metadata", {}).get("qtn_records") or len(calls))
    favorable_count = len(favorable)
    favorable_ratio = favorable_count / qtn_total if qtn_total else 0
    trait_counts = Counter(row.get("trait_type") or "未分类" for row in favorable)
    top_traits = trait_counts.most_common(3)
    trait_summary_text = "、".join(f"{trait}相关优良位点（{count}个）" for trait, count in top_traits)
    if not trait_summary_text:
        trait_summary_text = "暂未检测到可直接判定的优良位点"
    core_features = []
    if trait_counts.get("产量", 0):
        core_features.append("高产")
    if trait_counts.get("生物胁迫", 0):
        core_features.append("抗病")
    if trait_counts.get("株型", 0):
        core_features.append("株型紧凑")
    if trait_counts.get("品质", 0):
        core_features.append("优良品质")
    if not core_features:
        core_features.append("综合农艺性状")
    core_feature_text = "、".join(core_features[:3])

    trait_order = [trait for trait, _count in trait_counts.most_common()]
    for row in trait_summary_for_sample(data, sample):
        trait = row.get("trait_type") or "未分类"
        if trait not in trait_order:
            trait_order.append(trait)
    for trait in trait_counts:
        if trait not in trait_order:
            trait_order.append(trait)

    trait_headers = trait_order or ["未分类"]
    count_values = [str(trait_counts.get(trait, 0)) for trait in trait_headers]

    return [
        "一、基因型体检总体概况",
        "",
        f"本次水稻基因型体检共包含 {len(get_samples(data))} 份材料。",
        "",
        (
            f"针对目标样本 {sample} 的全景解析显示：在总计 {qtn_total} 个核心 QTN 位点中，"
            f"该样本共检测到 {favorable_count} 个优良变异位点，"
            f"优良变异检出率达 {favorable_ratio:.2%}。"
        ),
        "",
        (
            f"综合评价：该材料表现出较明显的“{core_feature_text}”特征。"
            f"其{trait_summary_text}较为突出，具备作为育种材料进一步评估和利用的潜力。"
        ),
        "",
        f"### {sample} 样本优良位点统计",
        "",
        "| 性状类型 | " + " | ".join(trait_headers) + " |",
        "|---|" + "|".join(["---:"] * len(trait_headers)) + "|",
        "| 优良位点个数 | " + " | ".join(count_values) + " |",
    ]


def favorable_table_markdown(data: dict, sample: str, random_selected: bool) -> str:
    favorable = favorable_calls(sample_calls(data, sample))
    lines = sample_summary_parts(data, sample, random_selected)
    lines.extend(
        [
            "",
            f"### {sample} 样本优良变异位点明细",
            "",
        "| QTN | 基因 | 性状类型 | 功能表现型 | 检测材料基因型 | 复核提示 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in favorable:
        values = [
            row.get("qtn_id", ""),
            row.get("gene_name", ""),
            row.get("trait_type", ""),
            row.get("phenotype", ""),
            row.get("sample_genotype_type", row.get("检测材料基因型", "")),
            review_note_text(data, row.get("review_note", "")),
        ]
        values = [str(value).replace("|", "\\|").replace("\n", "<br>") for value in values]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def key_trait_narrative(data: dict, sample: str) -> str:
    target_traits = ["产量", "品质", "株型", "生物胁迫"]
    favorable = favorable_calls(sample_calls(data, sample))
    by_trait: Dict[str, List[dict]] = defaultdict(list)
    for row in favorable:
        trait = row.get("trait_type") or "未分类"
        if trait in target_traits:
            by_trait[trait].append(row)

    parts = []
    for trait in target_traits:
        rows = by_trait.get(trait, [])
        if not rows:
            parts.append(f"{trait}性状中暂未检测到可直接判定的优良变异位点")
            continue
        phenotype_counts = Counter(row.get("phenotype") or "未分类功能表现型" for row in rows)
        top_items = phenotype_counts.most_common()
        fragments = []
        for phenotype, count in top_items:
            fragments.append(f"{phenotype}位点 {count} 个")
        parts.append(f"{trait}性状中包括" + "，".join(fragments))

    return (
        f"在 {sample} 材料的重点性状优良变异中，"
        + "；".join(parts)
        + "。"
    )


def clean_gene_name(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return value.split("/")[0].split("|")[0].strip()


def unique_join(values: Sequence[str], limit: int = 6, fallback: str = "相关基因") -> str:
    seen = []
    for value in values:
        text = clean_gene_name(value)
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return "、".join(seen) if seen else fallback


def phenotype_join(values: Sequence[str], limit: int = 4, fallback: str = "相关优良表型") -> str:
    seen = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return "、".join(seen) if seen else fallback


def filter_rows(rows: Sequence[dict], trait: str, keywords: Sequence[str]) -> List[dict]:
    result = []
    for row in rows:
        if row.get("trait_type") != trait:
            continue
        text = f"{row.get('phenotype','')} {row.get('gene_name','')}"
        if any(keyword in text for keyword in keywords):
            result.append(row)
    return result


def rows_by_trait(rows: Sequence[dict], trait: str) -> List[dict]:
    return [row for row in rows if row.get("trait_type") == trait]


def chinese_number(value: int) -> str:
    numbers = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    if 0 <= value <= 10:
        return numbers[value]
    return str(value)


def display_gene_name(value: str) -> str:
    value = str(value or "").strip()
    return value if value else ""


def gene_join(values: Sequence[str], limit: int = 8, fallback: str = "相关基因") -> str:
    seen = []
    for value in values:
        text = display_gene_name(value)
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return "、".join(seen) if seen else fallback


TRAIT_LABELS = {
    "产量": "高产",
    "生物胁迫": "抗病",
    "品质": "品质",
    "株型": "株型",
    "种子形态": "种子形态",
    "非生物胁迫": "抗逆",
    "其他": "其他",
    "次生代谢": "次生代谢",
    "抽穗期": "抽穗期",
}

TRAIT_PRIORITY = {
    "产量": 0,
    "生物胁迫": 1,
    "品质": 2,
    "株型": 3,
    "种子形态": 4,
    "非生物胁迫": 5,
    "其他": 6,
    "次生代谢": 7,
    "抽穗期": 8,
}


def sample_profile(data: dict, sample: str) -> dict:
    calls = sample_calls(data, sample)
    favorable = favorable_calls(calls)
    qtn_total = int(data.get("metadata", {}).get("qtn_records") or len(calls))
    trait_counts = Counter(row.get("trait_type") or "未分类" for row in favorable)
    return {
        "sample": sample,
        "calls": calls,
        "favorable": favorable,
        "qtn_total": qtn_total,
        "favorable_count": len(favorable),
        "favorable_ratio": len(favorable) / qtn_total if qtn_total else 0,
        "trait_counts": trait_counts,
    }


def trait_count(profile: dict, trait: str) -> int:
    return int(profile["trait_counts"].get(trait, 0))


def top_trait_labels(profile: dict, limit: int = 3) -> str:
    ranked = sorted(
        profile["trait_counts"].items(),
        key=lambda item: (-item[1], TRAIT_PRIORITY.get(item[0], 99), item[0]),
    )
    parts = [
        f"{TRAIT_LABELS.get(trait, trait)}（{count}）"
        for trait, count in ranked[:limit]
        if count
    ]
    return "、".join(parts) if parts else "暂无明确优势"


def core_advantage_names(profile: dict) -> List[str]:
    ranked = sorted(
        profile["trait_counts"].items(),
        key=lambda item: (-item[1], TRAIT_PRIORITY.get(item[0], 99), item[0]),
    )
    names = [TRAIT_LABELS.get(trait, trait) for trait, count in ranked if count]
    while len(names) < 3:
        names.append("综合农艺性状")
    return names[:3]


def recommended_use(profile: dict) -> str:
    has_yield = trait_count(profile, "产量") > 0
    has_biotic = trait_count(profile, "生物胁迫") > 0
    has_abiotic = trait_count(profile, "非生物胁迫") > 0
    has_quality = trait_count(profile, "品质") > 0
    has_architecture = trait_count(profile, "株型") > 0

    if has_yield and has_architecture and trait_count(profile, "株型") >= 4:
        return "高产紧凑株型育种"
    if has_biotic and has_abiotic and has_quality:
        return "抗病抗逆优质育种"
    if has_yield and has_architecture:
        return "高产株型改良育种"
    if has_biotic and has_quality:
        return "抗病优质育种"
    if has_abiotic:
        return "抗逆适应性育种"
    if has_yield:
        return "高产育种"
    return "综合农艺性状改良"


def rows_with_keywords(rows: Sequence[dict], keywords: Sequence[str]) -> List[dict]:
    result = []
    for row in rows:
        text = f"{row.get('phenotype','')} {row.get('gene_name','')} {row.get('trait_type','')}"
        if any(keyword in text for keyword in keywords):
            result.append(row)
    return result


def rows_not_in(rows: Sequence[dict], excluded: Sequence[dict]) -> List[dict]:
    excluded_ids = {id(row) for row in excluded}
    return [row for row in rows if id(row) not in excluded_ids]


def signal_sentence(rows: Sequence[dict], action: str, fallback: str = "当前结果未检出明确优良变异信号。") -> str:
    if not rows:
        return fallback
    return f"携带 {gene_join([row.get('gene_name') for row in rows])} 等变异，{action}{phenotype_join([row.get('phenotype') for row in rows], limit=4)}。"


def count_gene_phrase(rows: Sequence[dict], label: str, action: str) -> str:
    if not rows:
        return f"{label}：当前结果未检出明确优良变异信号。"
    genes = gene_join([row.get("gene_name") for row in rows], limit=10)
    gene_count = len({display_gene_name(row.get("gene_name")) for row in rows if display_gene_name(row.get("gene_name"))})
    count_text = f"共 {gene_count} 个相关基因" if gene_count else "多个相关位点"
    return f"{label}：聚合了 {genes} 等变异，{count_text}，{action}{phenotype_join([row.get('phenotype') for row in rows], limit=4)}。"


def sample_deep_section(profile: dict, section_number: int) -> List[str]:
    sample = profile["sample"]
    favorable = profile["favorable"]
    advantages = core_advantage_names(profile)

    yield_rows = rows_by_trait(favorable, "产量")
    quality_rows = rows_by_trait(favorable, "品质")
    architecture_rows = rows_by_trait(favorable, "株型")
    seed_rows = rows_by_trait(favorable, "种子形态")
    biotic_rows = rows_by_trait(favorable, "生物胁迫")
    abiotic_rows = rows_by_trait(favorable, "非生物胁迫")
    other_rows = rows_by_trait(favorable, "其他")

    grain_weight_rows = rows_with_keywords(yield_rows, ["粒重", "粒长", "谷粒", "粒型", "长粒"])
    grain_width_rows = rows_with_keywords(yield_rows, ["粒宽"])
    grain_number_rows = rows_with_keywords(yield_rows, ["粒数", "穗粒", "每穗", "分枝"])
    seed_setting_rows = rows_with_keywords(yield_rows, ["结实"])
    population_rows = rows_with_keywords(yield_rows, ["有效穗", "穗数", "株高", "群体", "分蘖"])

    blast_rows = rows_with_keywords(biotic_rows, ["稻瘟病", "Pi", "Pid", "Pia", "Pb1", "Ptr"])
    insect_rows = rows_with_keywords(biotic_rows, ["虫", "飞虱", "Bph", "BPH"])
    virus_rows = rows_with_keywords(biotic_rows, ["病毒", "STV"])
    other_disease_rows = rows_not_in(biotic_rows, blast_rows + insect_rows + virus_rows)

    salt_rows = rows_with_keywords(abiotic_rows, ["盐", "SKC", "HKT"])
    cold_rows = rows_with_keywords(abiotic_rows, ["寒", "冷", "COLDF"])
    flooding_rows = rows_with_keywords(abiotic_rows + architecture_rows, ["淹", "厌氧", "洪涝", "萌发", "TPP7", "UGT75A"])
    boron_rows = rows_with_keywords(abiotic_rows, ["硼", "BET1"])
    herbicide_rows = rows_with_keywords(abiotic_rows, ["除草剂"])
    other_abiotic_rows = rows_not_in(abiotic_rows, salt_rows + cold_rows + flooding_rows + boron_rows + herbicide_rows)

    nitrogen_rows = rows_with_keywords(other_rows + yield_rows, ["氮", "NRT", "NR", "ARE1", "利用效率"])

    lines = [
        f"### {chinese_number(section_number)}、样本 {sample} 深度解读",
        "",
        (
            f"经基因型检测与分析，样本 {sample} 共鉴定出 **{profile['favorable_count']} 个优良变异位点**。"
            f"整体表现型在**{advantages[0]}、{advantages[1]}、{advantages[2]}**方面具有优势，"
            "可作为优良育种材料进一步评估。"
        ),
        "",
        "#### 1. 产量潜力分析",
        "",
        "该样本在产量构成三要素相关位点上聚合了关键优良变异：",
        "",
        f"- **粒重与粒型**：{signal_sentence(grain_weight_rows, '预期表现为 ')}",
        f"- **穗粒数**：{signal_sentence(grain_number_rows, '提示具有增加单穗粒数的潜力，相关表型包括 ')}",
    ]
    if seed_setting_rows:
        lines.append(f"- **结实率**：{signal_sentence(seed_setting_rows, '提示具有改善结实率的潜力，相关表型包括 ')}")
    lines.append(f"- **群体结构**：{signal_sentence(population_rows, '有助于增加有效穗数或优化群体结构，相关表型包括 ')}")
    if grain_width_rows:
        lines.append(f"- **粒宽**：{signal_sentence(grain_width_rows, '预期有助于粒宽或粒型改良，相关表型包括 ')}")

    lines.extend(
        [
            "",
            "#### 2. 抗性评价",
            "",
            f"- **抗稻瘟病**：{count_gene_phrase(blast_rows, '', '提示具有稻瘟病抗性潜力，相关表型包括 ').lstrip('：')}",
            f"- **抗虫害及其他病害**：{count_gene_phrase(insect_rows + other_disease_rows, '', '提示具有病虫害抗性潜力，相关表型包括 ').lstrip('：')}",
            f"- **抗病毒**：{signal_sentence(virus_rows, '提示具有病毒病抗性潜力，相关表型包括 ')}",
            "",
            "#### 3. 环境适应性",
            "",
        ]
    )
    env_lines = []
    for label, rows, action in [
        ("耐盐性", salt_rows, "提示具有耐盐相关潜力，相关表型包括 "),
        ("耐寒性", cold_rows, "提示具有耐寒相关潜力，相关表型包括 "),
        ("耐淹性", flooding_rows, "提示具有耐淹或厌氧萌发耐受潜力，相关表型包括 "),
        ("耐硼毒", boron_rows, "提示具有硼毒耐受相关潜力，相关表型包括 "),
        ("除草剂耐受", herbicide_rows, "提示具有除草剂耐受相关潜力，相关表型包括 "),
    ]:
        if rows:
            env_lines.append(f"- **{label}**：{signal_sentence(rows, action)}")
    if other_abiotic_rows:
        env_lines.append(f"- **其他抗逆性**：{signal_sentence(other_abiotic_rows, '提示具有环境适应性补充价值，相关表型包括 ')}")
    if not env_lines:
        env_lines.append("- 当前结果未检出明确的非生物胁迫优良变异信号。")
    lines.extend(env_lines)

    lines.extend(
        [
            "",
            "#### 4. 品质与株型",
            "",
            f"- **品质**：{signal_sentence(quality_rows, '提示具有品质改良相关潜力，相关表型包括 ')}",
            f"- **株型**：{signal_sentence(architecture_rows, '预期具有株型改良相关优势，相关表型包括 ')}",
            f"- **种子形态**：{signal_sentence(seed_rows, '提示具有种子外观或形态改良相关潜力，相关表型包括 ')}",
        ]
    )
    if 0 < len(nitrogen_rows) < 3:
        lines.append(f"- **其他农艺性状**：{signal_sentence(nitrogen_rows, '可作为氮效率或营养利用相关性状的补充参考，相关表型包括 ')}")
    if len(nitrogen_rows) >= 3:
        lines.extend(
            [
                "",
                "#### 5. 氮高效利用",
                "",
                f"- 携带 {gene_join([row.get('gene_name') for row in nitrogen_rows])} 等氮利用效率相关基因，提示该材料在氮效率或营养利用相关性状上具有潜力。",
            ]
        )
    return lines


def comparison_section(profiles: Sequence[dict], section_number: int) -> List[str]:
    if len(profiles) < 2:
        return []
    traits = [
        ("优良变异总数", lambda p: str(p["favorable_count"])),
        ("产量相关", lambda p: str(trait_count(p, "产量"))),
        ("抗稻瘟病相关", lambda p: str(len(rows_with_keywords(rows_by_trait(p["favorable"], "生物胁迫"), ["稻瘟病", "Pi", "Pid", "Pia", "Pb1", "Ptr"])))),
        ("品质相关", lambda p: str(trait_count(p, "品质"))),
        ("氮高效", lambda p: str(len(rows_with_keywords(rows_by_trait(p["favorable"], "其他") + rows_by_trait(p["favorable"], "产量"), ["氮", "NRT", "NR", "ARE1", "利用效率"])))),
        ("株型相关", lambda p: str(trait_count(p, "株型"))),
        ("非生物胁迫", lambda p: str(trait_count(p, "非生物胁迫"))),
        ("种子形态", lambda p: str(trait_count(p, "种子形态"))),
    ]
    header = "| 性状维度 | " + " | ".join(profile["sample"] for profile in profiles) + " | 差异说明 |"
    sep = "|---|" + "|".join(["---:"] * len(profiles)) + "|---|"
    lines = [
        f"### {chinese_number(section_number)}、{'两材料' if len(profiles) == 2 else '多材料'}差异对比",
        "",
        header,
        sep,
    ]
    for label, getter in traits:
        values = [getter(profile) for profile in profiles]
        max_value = max(int(value) if str(value).isdigit() else 0 for value in values)
        leaders = [
            profile["sample"]
            for profile, value in zip(profiles, values)
            if str(value).isdigit() and int(value) == max_value and max_value > 0
        ]
        note = "、".join(leaders) + " 较突出" if leaders else "差异不明显"
        lines.append("| " + " | ".join([label] + values + [note]) + " |")
    return lines


def breeding_suggestions(profiles: Sequence[dict], section_number: int) -> List[str]:
    lines = [f"### {chinese_number(section_number)}、育种建议", ""]
    for profile in profiles:
        lines.append(
            f"- **{profile['sample']}**：{top_trait_labels(profile)} 较为突出，"
            f"可作为**{recommended_use(profile)}**的候选亲本或材料进一步验证。"
        )
    if len(profiles) >= 2:
        lines.append("- 多材料组合利用时，可优先关注产量、抗病、品质和抗逆位点的互补聚合，并结合田间表型继续筛选。")
    lines.extend(
        [
            "",
            "> 以上解读基于当前基因型检测结果，使用“预期”“提示”“具有潜力”等证据边界表述，可作为育种材料筛选参考，不等同于田间表现保证。",
        ]
    )
    return lines


def parse_sample_names(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,，;；\s]+", value) if part.strip()]


def key_trait_report(data: dict, selected_samples: Optional[Sequence[str]] = None) -> str:
    all_samples = get_samples(data)
    samples = list(selected_samples or all_samples)
    missing = [sample for sample in samples if sample not in all_samples]
    if missing:
        raise SystemExit(f"Sample not found: {', '.join(missing)}. Available: {', '.join(all_samples)}")
    if not samples:
        raise SystemExit("No samples found in gene_check JSON.")

    profiles = [sample_profile(data, sample) for sample in samples]
    qtn_total = profiles[0]["qtn_total"] if profiles else 0
    deep_profiles = profiles[:3]
    remaining = profiles[3:]

    lines = [
        "## 水稻基因型体检报告",
        "",
        f"本次水稻基因型体检共包含 **{len(profiles)} 份材料**，基于 {qtn_total} 个核心 QTN 位点进行匹配分析。",
        "",
    ]

    section_number = 1
    if len(profiles) > 1:
        lines.extend(
            [
                f"### {chinese_number(section_number)}、多样本对比总览",
                "",
                "| 样本编号 | 优良变异总数 | 检出率 | 核心优势 | 推荐用途 |",
                "|---|---:|---:|---|---|",
            ]
        )
        for profile in profiles:
            lines.append(
                f"| **{profile['sample']}** | {profile['favorable_count']} | {profile['favorable_ratio']:.2%} | "
                f"{top_trait_labels(profile)} | {recommended_use(profile)} |"
            )
        best_biotic = max(profiles, key=lambda profile: (trait_count(profile, "生物胁迫"), profile["favorable_count"]))
        best_yield = max(profiles, key=lambda profile: (trait_count(profile, "产量"), profile["favorable_count"]))
        lines.extend(
            [
                "",
                (
                    f"总体来看，{best_biotic['sample']} 的抗病相关优良位点较为突出，"
                    f"{best_yield['sample']} 的产量相关优良位点较为突出；后续解读重点围绕产量、抗性、环境适应性、品质与株型展开。"
                ),
                "",
            ]
        )
        section_number += 1

    for profile in deep_profiles:
        lines.extend(sample_deep_section(profile, section_number))
        lines.append("")
        section_number += 1

    if remaining:
        remaining_names = "、".join(profile["sample"] for profile in remaining)
        lines.extend(
            [
                f"### {chinese_number(section_number)}、其余材料说明",
                "",
                (
                    f"本次共有 {len(profiles)} 份材料，默认仅对前 3 份进行深度解读。"
                    f"其余材料已纳入总览统计：{remaining_names}。如需继续深度解读，"
                    "请提供单个样本编号，或提供多个样本编号组合。"
                ),
                "",
            ]
        )
        section_number += 1

    comparison_profiles = deep_profiles if len(profiles) > 3 else profiles
    comparison = comparison_section(comparison_profiles, section_number)
    if comparison:
        lines.extend(comparison)
        lines.append("")
        section_number += 1

    lines.extend(breeding_suggestions(deep_profiles, section_number))
    return "\n".join(lines)


def inline_markdown_to_html(text: str) -> str:
    escaped = html_lib.escape(str(text))
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def parse_markdown_table(lines: Sequence[str], start: int) -> tuple[str, int] | None:
    if start + 1 >= len(lines):
        return None
    header = lines[start].strip()
    separator = lines[start + 1].strip()
    if not (header.startswith("|") and header.endswith("|") and separator.startswith("|")):
        return None
    separator_cells = [cell.strip() for cell in separator.strip("|").split("|")]
    if not separator_cells or not all(re.match(r"^:?-{3,}:?$", cell) for cell in separator_cells):
        return None

    def split_row(row: str) -> List[str]:
        return [cell.strip() for cell in row.strip().strip("|").split("|")]

    headers = split_row(header)
    index = start + 2
    body_rows: List[List[str]] = []
    while index < len(lines):
        row = lines[index].strip()
        if not (row.startswith("|") and row.endswith("|")):
            break
        body_rows.append(split_row(row))
        index += 1

    html_lines = ["<div class=\"table-wrap\"><table>", "<thead><tr>"]
    for cell in headers:
        html_lines.append(f"<th>{inline_markdown_to_html(cell)}</th>")
    html_lines.extend(["</tr></thead>", "<tbody>"])
    for row in body_rows:
        html_lines.append("<tr>")
        for cell in row:
            html_lines.append(f"<td>{inline_markdown_to_html(cell)}</td>")
        html_lines.append("</tr>")
    html_lines.extend(["</tbody>", "</table></div>"])
    return "\n".join(html_lines), index


def markdown_to_html_body(markdown: str) -> str:
    lines = markdown.splitlines()
    result: List[str] = []
    paragraph: List[str] = []
    in_list = False
    in_quote = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            text = " ".join(item.strip() for item in paragraph if item.strip())
            result.append(f"<p>{inline_markdown_to_html(text)}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            result.append("</ul>")
            in_list = False

    def close_quote() -> None:
        nonlocal in_quote
        if in_quote:
            result.append("</blockquote>")
            in_quote = False

    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            flush_paragraph()
            close_list()
            close_quote()
            index += 1
            continue

        table = parse_markdown_table(lines, index)
        if table is not None:
            flush_paragraph()
            close_list()
            close_quote()
            table_html, index = table
            result.append(table_html)
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            close_list()
            close_quote()
            level = min(len(heading.group(1)), 4)
            result.append(f"<h{level}>{inline_markdown_to_html(heading.group(2))}</h{level}>")
            index += 1
            continue

        if stripped.startswith("- "):
            flush_paragraph()
            close_quote()
            if not in_list:
                result.append("<ul>")
                in_list = True
            result.append(f"<li>{inline_markdown_to_html(stripped[2:])}</li>")
            index += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            close_list()
            if not in_quote:
                result.append("<blockquote>")
                in_quote = True
            result.append(f"<p>{inline_markdown_to_html(stripped.lstrip('>').strip())}</p>")
            index += 1
            continue

        close_list()
        close_quote()
        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    close_list()
    close_quote()
    return "\n".join(result)


def report_title_from_markdown(markdown: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+)$", line.strip())
        if match:
            return re.sub(r"\*\*(.+?)\*\*", r"\1", match.group(1)).strip()
    return "RiceGenie Report"


def render_html_report(markdown: str) -> str:
    title = html_lib.escape(report_title_from_markdown(markdown))
    body = markdown_to_html_body(markdown)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8f4;
      --panel: #ffffff;
      --ink: #20312a;
      --muted: #637167;
      --line: #dbe4d8;
      --accent: #2f7d50;
      --accent-soft: #e8f3eb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", Arial, sans-serif;
      line-height: 1.72;
    }}
    main {{
      width: min(1080px, calc(100% - 32px));
      margin: 32px auto 48px;
      padding: 36px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 18px 45px rgba(32, 49, 42, 0.08);
    }}
    h1, h2, h3, h4 {{
      color: #173b29;
      line-height: 1.3;
      margin: 1.25em 0 0.55em;
    }}
    h1, h2 {{ border-bottom: 1px solid var(--line); padding-bottom: 0.35em; }}
    h1:first-child, h2:first-child {{ margin-top: 0; }}
    p {{ margin: 0.65em 0; }}
    strong {{ color: #174c31; }}
    ul {{ margin: 0.55em 0 0.9em; padding-left: 1.35em; }}
    li {{ margin: 0.35em 0; }}
    blockquote {{
      margin: 1.2em 0 0;
      padding: 0.8em 1em;
      background: var(--accent-soft);
      border-left: 4px solid var(--accent);
      color: #2f4a3a;
    }}
    .table-wrap {{
      overflow-x: auto;
      margin: 1em 0 1.25em;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 640px;
      font-size: 14px;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #eef5ef;
      color: #183f2a;
      font-weight: 700;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    @media (max-width: 720px) {{
      main {{ width: calc(100% - 20px); margin: 10px auto 24px; padding: 18px; }}
      table {{ min-width: 560px; }}
    }}
  </style>
</head>
<body>
  <main>
{body}
  </main>
</body>
</html>"""


def write_output(text: str, output: Optional[Path], output_format: str) -> None:
    rendered = render_html_report(text) if output_format == "html" else text
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Path to {prefix}.gene_check.json.")
    parser.add_argument(
        "--mode",
        choices=[
            "materials",
            "sample-summary",
            "favorable-table",
            "key-trait-narrative",
            "key-trait-report",
        ],
        default="key-trait-report",
    )
    parser.add_argument("--sample", help="Material/sample name. If omitted for non-report sample modes, pick one randomly.")
    parser.add_argument("--samples", help="Comma/space-separated material names for a combined key-trait report.")
    parser.add_argument("--seed", type=int, help="Random seed for reproducible sample choice.")
    parser.add_argument("--output", type=Path, help="Optional report output path.")
    parser.add_argument("--format", choices=["markdown", "html"], default="markdown", help="Output report format.")
    args = parser.parse_args(argv)

    data = load_result(args.input)
    samples = get_samples(data)

    if args.mode == "materials":
        text = material_list_markdown(data)
    elif args.mode == "key-trait-report":
        requested_samples = parse_sample_names(args.samples)
        if args.sample:
            requested_samples = [args.sample]
        text = key_trait_report(data, selected_samples=requested_samples or None)
    else:
        sample = choose_sample(samples, args.sample, args.seed)
        if args.mode == "sample-summary":
            text = sample_summary_markdown(data, sample, random_selected=not bool(args.sample))
        elif args.mode == "favorable-table":
            text = favorable_table_markdown(data, sample, random_selected=not bool(args.sample))
        elif args.mode == "key-trait-narrative":
            text = key_trait_narrative(data, sample)

    write_output(text, args.output, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
