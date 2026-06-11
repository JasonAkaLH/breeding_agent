from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


FIELD_LABELS = {
    "ADJ_ESTIMATE": "调整后估计",
    "BIC": "贝叶斯信息准则",
    "CHECK_MEAN": "对照均值",
    "CHECK_MEAN_EXP": "对照均值",
    "CHECK_MEAN_LOC": "地点对照均值",
    "CHECK_MS_ERROR": "对照误差均方",
    "CHECK_PHENO_MEAN": "对照表型均值",
    "CK_MEAN_EXP": "对照均值",
    "CK_MEAN_LOC": "地点对照均值",
    "CV_ENTRY": "材料变异系数",
    "CV_ERROR": "误差变异系数",
    "CV_REP": "重复变异系数",
    "DF": "自由度",
    "DF_ENTRY": "材料自由度",
    "DF_LOC": "地点自由度",
    "DF_REP": "重复自由度",
    "DF_TREAT": "处理自由度",
    "ESTIMATE": "估计值",
    "ERROR_MS_ERROR": "误差均方",
    "EXP_CHECK_MEAN": "环境对照平均值",
    "F_VALUE": "F 值",
    "FVALUE_LOC": "地点 F 值",
    "FVALUE_REP": "重复 F 值",
    "FVALUE_TREAT": "处理 F 值",
    "GCV": "遗传变异系数",
    "GENOTYPIC_CV_MEAN": "遗传变异系数均值",
    "HYBRID": "材料",
    "HYBRID_BLUP": "材料 BLUP",
    "HYBRID_COUNT": "材料数量",
    "HYBRID_MAX": "材料最大值",
    "HYBRID_MEAN": "材料平均值",
    "HYBRID_MIN": "材料最小值",
    "HYBRID_STD_DEV": "材料标准差",
    "INC_VALUE": "增减值",
    "INTERACTION_VARIANCE": "互作方差",
    "LCC": "试点",
    "LCC_CHECK_MEAN": "试点对照平均值",
    "LCC_MAX": "试点最大值",
    "LCC_MEAN": "试点平均值",
    "LCC_MIN": "试点最小值",
    "LOCATION_COUNT": "地点数",
    "LOWER_CI": "置信区间下限",
    "LSD_01": "LSD 0.01 显著性",
    "LSD_05": "LSD 0.05 显著性",
    "LSD_10": "LSD 0.10 显著性",
    "LSMEAN": "最小二乘均值",
    "MATURITY_CHECK_MEAN": "成熟对照平均值",
    "MAX": "最大值",
    "MEAN": "平均值",
    "MEDIAN": "中位数",
    "MIN": "最小值",
    "MS_ENTRY": "材料均方",
    "MS_ERROR": "误差均方",
    "MS_LOC": "地点均方",
    "MS_REP": "重复均方",
    "MS_TREAT": "处理均方",
    "NO_ZERO_MIN": "非零最小值",
    "PCT_CHECK": "相对对照百分比",
    "PCT_CHECK_MEAN": "相对对照均值百分比",
    "PCT_DT_CHECK": "相对 DT 对照百分比",
    "PCT_GNTC_CHECK": "相对 GNTC 对照百分比",
    "PCT_LOCATION_MEAN": "相对地点均值百分比",
    "PCT_MATURITY_CHECK": "相对成熟对照百分比",
    "PCT_PERFORMANCE_CHECK": "相对表现对照百分比",
    "PCT_RESISTANCE_CHECK": "相对抗性对照百分比",
    "PCT_SUSCEPTIBLE_CHECK": "相对感病对照百分比",
    "PCT_TRIAL_MEAN": "相对试验均值百分比",
    "PCT_YIELD_INC_LOCS": "增产点比例",
    "PED_DIFF_EXP": "与对照差异",
    "PED_PCT_EXP": "与对照差异百分比",
    "PERFORMANCE_CHECK_MEAN": "表现对照平均值",
    "PLUS_MINUS_PCT_CHECK_MEAN": "相对对照增减百分比",
    "PROBABILITY": "概率",
    "PROBABILITY_ENTRY": "材料显著性概率",
    "PROBABILITY_LOC": "地点显著性概率",
    "PROBABILITY_REP": "重复显著性概率",
    "RANK": "排名",
    "RANK_LSMEAN": "最小二乘均值排名",
    "RANK_MS_BLUP": "材料 BLUP 排名",
    "REF_COUNT": "重复数",
    "RESISTANCE_CHECK_MEAN": "抗性对照平均值",
    "RK_STABILITY": "稳定性排名",
    "SE": "标准误",
    "SIGNIFICANCE": "显著性",
    "SIGNIFICANCE_IDX": "显著性指标",
    "SS_ENTRY": "材料平方和",
    "SS_ERROR": "误差平方和",
    "SS_LOC": "地点平方和",
    "SS_REP": "重复平方和",
    "SS_TOTAL": "总平方和",
    "STDDEV": "标准差",
    "STD_ERROR": "标准误",
    "SUM": "总和",
    "SUPERIORITY_MEASURE": "优势值",
    "SUSCEPTIBLE_CHECK_MEAN": "感病对照平均值",
    "TRAIT_VALUE": "性状值",
    "UPPER_CI": "置信区间上限",
    "YIELD_INC_LOCS": "增产点数",
    "metric": "指标",
    "value": "数值",
    "design": "试验设计",
    "run_id": "运行编号",
    "analysis_profile": "分析方案",
    "locations": "地点数",
    "materials": "材料数",
    "traits": "性状数",
    "observations": "观测数",
    "reps": "重复数",
    "trait": "性状",
    "direction": "评价方向",
    "material_count": "材料数",
    "location_count": "地点数",
    "rep_count": "重复数",
    "mean": "均值",
    "stddev": "标准差",
    "std_error": "标准误",
    "cv": "变异系数",
    "min": "最小值",
    "max": "最大值",
    "median": "中位数",
    "sum": "总和",
    "quality": "数据质量",
    "ped_id": "材料编号",
    "entry_id": "参试编号",
    "loc_id": "地点编号",
    "rank": "排名",
    "performance_rank": "表现排名",
    "stability_rank": "稳定性排名",
    "pct_check_mean": "相对对照均值百分比",
    "pct_trial_mean": "相对试验均值百分比",
    "pct_location_mean": "相对地点均值百分比",
    "locations_above_check": "优于对照的地点数",
    "pct_locations_above_check": "优于对照的地点比例",
    "check_mean": "对照均值",
    "check_max": "对照最大值",
    "check_min": "对照最小值",
    "check_type": "对照类型",
    "mean_across_locations": "跨地点均值",
    "sd_across_locations": "跨地点标准差",
    "cv_across_locations": "跨地点变异系数",
    "term": "变异来源",
    "df": "自由度",
    "sum_sq": "平方和",
    "mean_sq": "均方",
    "f_value": "F 值",
    "p_value": "P 值",
    "significance": "显著性",
    "group": "分组",
    "method": "方法",
    "model": "模型",
    "status": "状态",
    "input_traits": "输入性状数",
    "numeric_traits": "完整数值分析性状数",
    "categorical_traits": "分类描述性状数",
    "skipped_traits": "跳过性状数",
    "category": "类别",
    "count": "数量",
    "top_category": "主导类别",
    "category_count": "类别数",
    "nonempty": "非空观测",
    "missing": "缺失数",
    "missing_pct": "缺失比例",
    "summary": "摘要",
    "title": "标题",
    "warnings": "警告",
    "reason": "原因",
    "singular_fit": "奇异拟合",
    "blup": "BLUP",
    "estimate": "估计值",
    "lower_ci": "置信区间下限",
    "upper_ci": "置信区间上限",
    "lcl": "置信区间下限",
    "ucl": "置信区间上限",
    "lsd_01": "LSD 0.01 显著性",
    "lsd_05": "LSD 0.05 显著性",
    "lsd_10": "LSD 0.10 显著性",
    "se": "标准误",
    "ms_error": "误差均方",
    "ms_rep": "重复均方",
    "ms_treat": "处理均方",
    "probability": "概率",
    "response_column": "响应变量列",
    "No displayable data.": "无可展示数据。",
}

VALUE_LABELS = {
    "rcbd": "随机区组试验",
    "diagonal": "对角线增广试验",
    "full_report": "完整报告",
    "not_calculable": "不可计算",
    "completed": "已完成",
    "completed_with_warnings": "已完成（有提示）",
    "failed": "失败",
    "not_applicable": "不适用",
    "unknown": "未知",
    "good": "良好",
    "warning": "需注意",
    "poor": "较差",
    "higher_is_better": "越高越好",
    "lower_is_better": "越低越好",
    "neutral": "仅描述",
    "true": "是",
    "false": "否",
}


def _label_text(value: Any) -> str:
    text = str(value)
    return FIELD_LABELS.get(text, FIELD_LABELS.get(text.lower(), FIELD_LABELS.get(text.upper(), text)))


def _display_text(value: Any) -> str:
    text = _value_text(value)
    return VALUE_LABELS.get(
        text,
        VALUE_LABELS.get(text.lower(), FIELD_LABELS.get(text, FIELD_LABELS.get(text.lower(), FIELD_LABELS.get(text.upper(), text)))),
    )


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping) and not value:
        return "not_calculable"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _records_to_rows(fields: Sequence[str] | None, records: Sequence[Any] | None, limit: int | None = None) -> list[dict[str, str]]:
    if not fields or not records:
        return []
    rows: list[dict[str, str]] = []
    for raw in list(records)[:limit]:
        if isinstance(raw, Mapping):
            row = {str(field): _value_text(raw.get(field)) for field in fields}
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            values = list(raw)
            row = {
                str(field): _value_text(values[index] if index < len(values) else None)
                for index, field in enumerate(fields)
            }
        else:
            row = {str(field): "" for field in fields}
        rows.append(row)
    return rows


def _render_table(rows: list[dict[str, str]], empty: str = "No displayable data.") -> str:
    if not rows:
        return f'<p class="muted">{html.escape(_display_text(empty))}</p>'
    fields = list(rows[0].keys())
    header = "".join(f"<th>{html.escape(_label_text(field))}</th>" for field in fields)
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(_display_text(row.get(field, '')))}</td>" for field in fields)
        body.append(f"<tr>{cells}</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _chapter_cards(chapters: Mapping[str, Any] | None) -> str:
    if not chapters:
        return ""
    cards = []
    for name, item in chapters.items():
        item = item if isinstance(item, Mapping) else {}
        title = html.escape(str(item.get("title") or name))
        status = html.escape(_display_text(item.get("status") or ""))
        summary = html.escape(str(item.get("summary") or ""))
        cards.append(
            '<section class="card">'
            f'<div class="card-head"><h3>{title}</h3><span class="status">{status}</span></div>'
            f"<p>{summary}</p>"
            "</section>"
        )
    return "\n".join(cards)


def _trait_preflight_sections(preflight: Mapping[str, Any] | None) -> str:
    if not isinstance(preflight, Mapping):
        return ""
    counts = preflight.get("counts") if isinstance(preflight.get("counts"), Mapping) else {}
    overview_rows = [
        {"metric": "input_traits", "value": _value_text(counts.get("input_traits"))},
        {"metric": "numeric_traits", "value": _value_text(counts.get("numeric_traits"))},
        {"metric": "categorical_traits", "value": _value_text(counts.get("categorical_traits"))},
        {"metric": "skipped_traits", "value": _value_text(counts.get("skipped_traits"))},
    ]
    skipped = preflight.get("skipped_traits") if isinstance(preflight.get("skipped_traits"), list) else []
    skipped_rows = [
        {"trait": _value_text(item.get("trait")), "status": _value_text(item.get("status")), "reason": _value_text(item.get("reason"))}
        for item in skipped
        if isinstance(item, Mapping)
    ]
    categorical_summary = preflight.get("categorical_trait_summary")
    categorical_sections: list[str] = []
    if isinstance(categorical_summary, Mapping):
        for trait, item in list(categorical_summary.items())[:12]:
            if not isinstance(item, Mapping):
                continue
            top_rows = item.get("top_categories") if isinstance(item.get("top_categories"), list) else []
            top_categories = [
                {"category": _value_text(row.get("category")), "count": _value_text(row.get("count"))}
                for row in top_rows
                if isinstance(row, Mapping)
            ]
            meta_rows = [
                {"metric": "trait", "value": _value_text(trait)},
                {"metric": "nonempty", "value": _value_text(item.get("nonempty"))},
                {"metric": "missing", "value": _value_text(item.get("missing"))},
                {"metric": "missing_pct", "value": _value_text(item.get("missing_pct"))},
                {"metric": "category_count", "value": _value_text(item.get("category_count"))},
            ]
            categorical_sections.append(
                '<section class="trait-section">'
                f"<h3>{html.escape(str(trait))}</h3>"
                f"{_render_table(meta_rows)}"
                "<h4>类别频数</h4>"
                f"{_render_table(top_categories)}"
                "</section>"
            )
    check_type = preflight.get("check_type") if isinstance(preflight.get("check_type"), Mapping) else {}
    check_rows = [
        {"metric": key, "value": value}
        for key, value in (check_type.get("mapping") or {}).items()
    ] if isinstance(check_type.get("mapping"), Mapping) else []
    return (
        "<h2>性状预检与分流</h2>"
        f"{_render_table(overview_rows)}"
        "<h3>check_type 识别</h3>"
        f'<p class="muted">{html.escape(_value_text(check_type.get("normalization_note")))}</p>'
        f"{_render_table(check_rows)}"
        "<h3>跳过性状</h3>"
        f"{_render_table(skipped_rows)}"
        "<h3>分类性状描述统计</h3>"
        f"{''.join(categorical_sections) if categorical_sections else _render_table([])}"
    )


def _analysis_status(analysis: Mapping[str, Any] | None, trait: str) -> str:
    if not isinstance(analysis, Mapping):
        return "unknown"
    by_trait = analysis.get("by_trait")
    if not isinstance(by_trait, Mapping):
        return str(analysis.get("status") or "unknown")
    item = by_trait.get(trait)
    if not isinstance(item, Mapping):
        return "unknown"
    return str(item.get("status") or "unknown")


def _analysis_for_trait(analysis: Mapping[str, Any] | None, trait: str) -> Mapping[str, Any] | None:
    if not isinstance(analysis, Mapping):
        return None
    by_trait = analysis.get("by_trait")
    if isinstance(by_trait, Mapping) and isinstance(by_trait.get(trait), Mapping):
        return by_trait.get(trait)
    return analysis


def _hybrid_blup_table(analyses_node: Mapping[str, Any], trait: str) -> str:
    blup = _analysis_for_trait(analyses_node.get("hybrid_blup"), trait)
    if not isinstance(blup, Mapping):
        return _render_table([])
    rows = _records_to_rows(blup.get("blup_fields"), blup.get("blup"), limit=10)
    if rows:
        meta = [
            f"status: {_display_text(blup.get('status') or 'unknown')}",
            f"model: {_value_text(blup.get('model'))}",
            f"singular_fit: {_display_text(blup.get('singular_fit'))}",
        ]
        meta_text = " | ".join(item for item in meta if item and not item.endswith(": "))
        return f'<p class="muted">{html.escape(meta_text)}</p>{_render_table(rows)}'
    reason = blup.get("reason") or "No displayable data."
    return f'<p class="muted">{html.escape(_display_text(reason))}</p>'


def _trait_sections(report: Mapping[str, Any], limit: int = 12) -> str:
    traits_node = report.get("traits") if isinstance(report.get("traits"), Mapping) else {}
    materials_node = report.get("materials") if isinstance(report.get("materials"), Mapping) else {}
    locations_node = report.get("locations") if isinstance(report.get("locations"), Mapping) else {}
    analyses_node = report.get("analyses") if isinstance(report.get("analyses"), Mapping) else {}

    trait_rows = _records_to_rows(traits_node.get("trait_summary_fields"), traits_node.get("trait_summary"), limit=limit)
    if not trait_rows or "trait" not in trait_rows[0]:
        return ""

    sections: list[str] = []
    material_fields = materials_node.get("material_summary_fields")
    material_by_trait = materials_node.get("by_trait") if isinstance(materials_node.get("by_trait"), Mapping) else {}
    location_fields = locations_node.get("location_summary_fields")
    location_by_trait = locations_node.get("summary_by_trait") if isinstance(locations_node.get("summary_by_trait"), Mapping) else {}

    for row in trait_rows:
        trait = row.get("trait", "")
        material_rows = _records_to_rows(material_fields, material_by_trait.get(trait), limit=10)
        location_rows = _records_to_rows(location_fields, location_by_trait.get(trait), limit=10)
        status_bits = [
            f"方差分析: {_display_text(_analysis_status(analyses_node.get('anova'), trait))}",
            f"LSD 分组: {_display_text(_analysis_status(analyses_node.get('lsd_grouping'), trait))}",
            f"空间校正: {_display_text(_analysis_status(analyses_node.get('spatial_adjustment'), trait))}",
        ]
        status_bits.append(f"BLUP: {_display_text(_analysis_status(analyses_node.get('hybrid_blup'), trait))}")
        sections.append(
            '<section class="trait-section">'
            f"<h3>{html.escape(trait)}</h3>"
            f'<p class="muted">{html.escape(" | ".join(status_bits))}</p>'
            "<h4>材料表现排名</h4>"
            f"{_render_table(material_rows)}"
            "<h4>材料 BLUP</h4>"
            f"{_hybrid_blup_table(analyses_node, trait)}"
            "<h4>地点汇总</h4>"
            f"{_render_table(location_rows)}"
            "</section>"
        )
    return "\n".join(sections)


def render_report_html(report: Mapping[str, Any]) -> str:
    metadata = report.get("metadata") if isinstance(report.get("metadata"), Mapping) else {}
    counts = metadata.get("counts") if isinstance(metadata.get("counts"), Mapping) else {}
    traits_node = report.get("traits") if isinstance(report.get("traits"), Mapping) else {}
    overview_rows = [
        {"metric": "design", "value": _value_text(metadata.get("design"))},
        {"metric": "run_id", "value": _value_text(metadata.get("run_id"))},
        {"metric": "locations", "value": _value_text(counts.get("locations"))},
        {"metric": "materials", "value": _value_text(counts.get("materials"))},
        {"metric": "traits", "value": _value_text(counts.get("traits"))},
        {"metric": "observations", "value": _value_text(counts.get("observations"))},
    ]
    trait_rows = _records_to_rows(traits_node.get("trait_summary_fields"), traits_node.get("trait_summary"))
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    preflight = report.get("trait_preflight") if isinstance(report.get("trait_preflight"), Mapping) else {}
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>田间数据分析报告</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#f6f7f9;color:#17202a;}"
        "main{max-width:1160px;margin:0 auto;padding:28px 22px 48px;}"
        "h1{font-size:28px;margin:0 0 8px;} h2{font-size:22px;margin:28px 0 12px;} h3{font-size:18px;margin:0;} h4{font-size:15px;margin:18px 0 8px;}"
        ".muted{color:#5f6b7a;} .hero{background:#fff;border:1px solid #dce1e7;border-radius:8px;padding:22px;margin-bottom:18px;}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;}"
        ".card,.trait-section{background:#fff;border:1px solid #dce1e7;border-radius:8px;padding:16px;margin-bottom:12px;}"
        ".card-head{display:flex;justify-content:space-between;gap:12px;align-items:center;} .status{font-size:12px;border:1px solid #cfd6de;border-radius:999px;padding:3px 8px;color:#354253;background:#f8fafc;}"
        ".table-wrap{overflow:auto;border:1px solid #dce1e7;border-radius:6px;background:#fff;} table{border-collapse:collapse;width:100%;font-size:13px;} th,td{padding:8px 10px;border-bottom:1px solid #e7ebef;text-align:left;white-space:nowrap;} th{background:#f1f4f7;font-weight:600;} tr:last-child td{border-bottom:0;}"
        "</style></head><body><main>"
        '<section class="hero"><h1>田间数据分析报告</h1>'
        '<p class="muted">本报告由 field-analysis-report 结果渲染；完整 JSON 事实源已保留，可用于后续追问材料排名、对照比较、方差分析、LSD 分组和稳定性结果。</p>'
        f"{_render_table(overview_rows)}"
        "</section>"
        f'<h2>章节状态</h2><div class="grid">{_chapter_cards(chapters)}</div>'
        f"{_trait_preflight_sections(preflight)}"
        f"<h2>性状概览</h2>{_render_table(trait_rows)}"
        f"<h2>性状结果</h2>{_trait_sections(report)}"
        "</main></body></html>"
    )


def render_report_file(input_path: str | Path, output_path: str | Path) -> None:
    input_path = Path(input_path)
    output_path = Path(output_path)
    report = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(report, Mapping) or report.get("format") != "field-analysis-report":
        raise ValueError("Input report is not field-analysis-report.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report_html(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render field-analysis-report JSON as user-facing HTML.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    render_report_file(args.input, args.output)
    print(f"HTML report exported: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
