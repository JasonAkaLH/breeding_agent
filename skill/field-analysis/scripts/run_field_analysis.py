from __future__ import annotations

import base64
import csv
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_field_analysis_html import render_report_file
from trait_preflight import build_trait_preflight, prepare_records_for_numeric_backend

SUPPORTED_INPUT_EXTENSIONS = {".csv", ".xlsx"}
EXCEL_INPUT_EXTENSIONS = {".xlsx"}
DEFAULT_BREEDSTAT2_URL = "http://breedstat2:8000"
LOCAL_BREEDSTAT2_URL = "http://127.0.0.1:8020"
CONTAINER_BREEDSTAT2_URL = "http://breedstat2:8000"
WIDE_FIXED_COLUMNS = ("loc_id", "rep_num", "ranges", "pass", "entry_id", "ped_id", "check_type")
ALLOWED_DESIGNS = {"rcbd", "diagonal"}


def json_response(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def failure(answer: str, *, missing: list[str] | None = None, error_type: str = "field_analysis_error") -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "is_error": True,
        "answer": answer,
        "mode": "error",
        "result_facts": {},
        "error": {"type": error_type, "message": answer},
    }
    if missing:
        result["missing"] = missing
    return result


def breedstat2_base_url() -> str:
    return (
        os.environ.get("FIELD_ANALYSIS_BREEDSTAT2_URL")
        or os.environ.get("BREEDSTAT2_URL")
        or default_breedstat2_url()
    ).rstrip("/")


def default_breedstat2_url() -> str:
    if Path("/.dockerenv").exists() or os.environ.get("KUBERNETES_SERVICE_HOST"):
        return CONTAINER_BREEDSTAT2_URL
    if os.name == "nt":
        return LOCAL_BREEDSTAT2_URL
    return CONTAINER_BREEDSTAT2_URL


def _utf8_locale(value: str | None) -> str:
    text = str(value or "").strip()
    normalized = text.upper().replace("_", "-")
    if "UTF-8" in normalized or "UTF8" in normalized:
        return text
    return "C.UTF-8"


def _rscript_env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": _utf8_locale(os.environ.get("LANG")),
        "LC_ALL": _utf8_locale(os.environ.get("LC_ALL") or os.environ.get("LANG")),
        "LC_CTYPE": _utf8_locale(os.environ.get("LC_CTYPE") or os.environ.get("LC_ALL") or os.environ.get("LANG")),
    }


def safe_run_id(value: Any) -> str:
    text = str(value or "").strip() or "field_analysis"
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text[:80] or "field_analysis"


def resolve_design(payload: Mapping[str, Any]) -> str | None:
    explicit = str(payload.get("design") or payload.get("design_type") or "").strip().lower()
    if explicit in ALLOWED_DESIGNS:
        return explicit
    query = " ".join(
        str(value or "")
        for value in (
            payload.get("query"),
            payload.get("message"),
            payload.get("user_text"),
            payload.get("analysis_request"),
            payload.get("design"),
            payload.get("design_type"),
        )
    ).lower()
    if re.search(
        r"(rcbd|randomi[sz]ed complete block|随机区组|随机区组设计|随机区组试验|随机区组试验数据分析|随机完全区组|随机完全区组设计|随机完全区组试验|完全随机区组|完全随机区组设计|完全随机区组试验)",
        query,
        flags=re.IGNORECASE,
    ):
        return "rcbd"
    if re.search(r"(diagonal|对角线|对角线设计|对角线增广|对角线增广设计|对角线试验|增广)", query, flags=re.IGNORECASE):
        return "diagonal"
    return None


def artifact_filename(artifact: Mapping[str, Any], default: str = "field_data.csv") -> str:
    raw = str(artifact.get("filename") or default)
    name = Path(raw).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return default
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_INPUT_EXTENSIONS:
        return f"{Path(name).stem or 'field_data'}.csv"
    return name


def decode_artifact_content(artifact: Mapping[str, Any]) -> bytes | None:
    if isinstance(artifact.get("content"), str):
        return str(artifact["content"]).encode("utf-8")
    if isinstance(artifact.get("content_base64"), str):
        try:
            return base64.b64decode(str(artifact["content_base64"]), validate=True)
        except Exception:
            return None
    return None


def write_input_from_artifact(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    artifacts = payload.get("uploaded_artifacts")
    if not isinstance(artifacts, list | tuple):
        return None
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        content = decode_artifact_content(item)
        if content is None:
            continue
        path = work_dir / artifact_filename(item)
        path.write_bytes(content)
        return normalize_input_file(path, work_dir)
    return None


def write_input_from_metadata(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    input_data = (
        metadata.get("pasted_field_data")
        or metadata.get("input_data")
        or payload.get("pasted_field_data")
        or payload.get("input_data")
    )
    if input_data is None:
        return None
    path = work_dir / "field_data.csv"
    if isinstance(input_data, str):
        path.write_text(input_data, encoding="utf-8")
        return path
    if isinstance(input_data, list | tuple) and all(isinstance(row, Mapping) for row in input_data):
        rows = [dict(row) for row in input_data]
        fieldnames = list(WIDE_FIXED_COLUMNS)
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(str(key))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path
    return None


def resolve_input_file(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    uploaded = write_input_from_artifact(payload, work_dir)
    if uploaded is not None:
        return uploaded
    metadata_input = write_input_from_metadata(payload, work_dir)
    if metadata_input is not None:
        return metadata_input
    for key in ("input_file", "file_path", "path"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file() and candidate.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS:
                return normalize_input_file(candidate.resolve(), work_dir)
    return None


def normalize_input_file(path: Path, work_dir: Path) -> Path:
    if path.suffix.lower() in EXCEL_INPUT_EXTENSIONS:
        return convert_excel_to_csv(path, work_dir)
    return path


def convert_excel_to_csv(path: Path, work_dir: Path) -> Path:
    rows = read_excel_rows(path)
    if not rows:
        raise RuntimeError("Excel file does not contain any rows.")
    csv_path = work_dir / f"{path.stem or 'field_data'}-excel.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    return csv_path


def read_csv_records(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise RuntimeError("Input table must include a header row.")
                return [{str(key): ("" if value is None else str(value)) for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Input CSV encoding is not supported: {last_error}")


def fieldnames_from_records(records: list[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in records:
        for key in row:
            text = str(key)
            if text not in names:
                names.append(text)
    return names


def post_json(url: str, payload: Mapping[str, Any], timeout: int = 300) -> dict[str, Any]:
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read().decode("utf-8")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError("breedstat2 response must be a JSON object.")
    return data


def run_breedstat2_report(*, records: list[Mapping[str, Any]], design: str, run_id: str) -> dict[str, Any]:
    return post_json(
        f"{breedstat2_base_url()}/field-analysis/report",
        {
            "data": records,
            "design": design,
            "run_id": run_id,
            "profile": "full_report",
            "write_files": False,
        },
    )


def extract_api_report(api_result: Mapping[str, Any]) -> dict[str, Any]:
    if api_result.get("ok") is False:
        error = api_result.get("error")
        message = error.get("message") if isinstance(error, Mapping) else None
        raise RuntimeError(str(message or "breedstat2 field-analysis/report returned ok=false."))
    report = api_result.get("report")
    if not isinstance(report, dict):
        raise RuntimeError("breedstat2 field-analysis/report did not return a report object.")
    return report


def unique_count(records: list[Mapping[str, Any]], column: str) -> int:
    values = {str(row.get(column) or "").strip() for row in records}
    values.discard("")
    return len(values)


def preflight_trait_rows(preflight: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    classifications = preflight.get("classifications")
    if not isinstance(classifications, list):
        return rows
    for item in classifications:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            {
                "trait": item.get("trait"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "observations": item.get("observations"),
                "nonempty": item.get("nonempty"),
                "missing": item.get("missing"),
                "missing_pct": item.get("missing_pct"),
                "reason": item.get("reason"),
            }
        )
    return rows


def not_applicable_by_trait(preflight: Mapping[str, Any], reason: str) -> dict[str, Any]:
    traits = [str(item) for item in preflight.get("input_traits") or []]
    return {
        "status": "not_applicable",
        "reason": reason,
        "by_trait": {
            trait: {
                "status": "not_applicable",
                "reason": reason,
            }
            for trait in traits
        },
    }


def build_preflight_only_report(
    *,
    records: list[Mapping[str, Any]],
    fields: list[str],
    design: str,
    run_id: str,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    counts = preflight.get("counts") if isinstance(preflight.get("counts"), Mapping) else {}
    no_model_reason = "没有满足完整模型分析要求的连续数值性状；未调用 ANOVA/LSD/BLUP 模型分析。"
    return {
        "format": "field-analysis-report",
        "metadata": {
            "design": design,
            "run_id": run_id,
            "analysis_profile": "trait_preflight_only",
            "counts": {
                "observations": len(records),
                "materials": unique_count(records, "ped_id"),
                "locations": unique_count(records, "loc_id"),
                "reps": unique_count(records, "rep_num"),
                "traits": 0,
                "input_traits": counts.get("input_traits", 0),
            },
            "trait_preflight_counts": dict(counts),
            "input_columns": len(fields),
        },
        "chapters": {
            "trait_preflight": {
                "status": "completed",
                "title": "性状预检与分流",
                "summary": (
                    f"输入性状 {counts.get('input_traits', 0)} 个；"
                    f"进入完整数值分析 {counts.get('numeric_traits', 0)} 个；"
                    f"分类描述 {counts.get('categorical_traits', 0)} 个；"
                    f"跳过 {counts.get('skipped_traits', 0)} 个。"
                ),
            },
            "data_overview": {
                "status": "completed",
                "title": "数据概览",
                "summary": f"读取 {len(records)} 条观测、{len(fields)} 个输入列。",
            },
            "descriptive_stats": {
                "status": "completed" if preflight.get("categorical_traits") else "not_applicable",
                "title": "分类性状描述",
                "summary": "分类性状仅输出类别频数和材料/地点分布，不进入数值模型。",
            },
            "anova": {"status": "not_applicable", "title": "ANOVA", "summary": no_model_reason},
            "lsd_grouping": {"status": "not_applicable", "title": "LSD 分组", "summary": no_model_reason},
            "hybrid_blup": {"status": "not_applicable", "title": "材料 BLUP", "summary": no_model_reason},
        },
        "traits": {
            "trait_summary_fields": ["trait", "kind", "status", "observations", "nonempty", "missing", "missing_pct", "reason"],
            "trait_summary": preflight_trait_rows(preflight),
        },
        "materials": {},
        "locations": {},
        "analyses": {
            "anova": not_applicable_by_trait(preflight, no_model_reason),
            "lsd_grouping": not_applicable_by_trait(preflight, no_model_reason),
            "hybrid_blup": not_applicable_by_trait(preflight, no_model_reason),
        },
        "trait_preflight": dict(preflight),
    }


def attach_preflight_to_report(report: dict[str, Any], preflight: Mapping[str, Any]) -> dict[str, Any]:
    report["format"] = "field-analysis-report"
    report["trait_preflight"] = dict(preflight)
    metadata = report.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        report["metadata"] = metadata
    metadata["trait_preflight_counts"] = dict(preflight.get("counts") or {})
    chapters = report.get("chapters")
    if not isinstance(chapters, dict):
        chapters = {}
        report["chapters"] = chapters
    counts = preflight.get("counts") if isinstance(preflight.get("counts"), Mapping) else {}
    chapters["trait_preflight"] = {
        "status": "completed",
        "title": "性状预检与分流",
        "summary": (
            f"输入性状 {counts.get('input_traits', 0)} 个；"
            f"进入完整数值分析 {counts.get('numeric_traits', 0)} 个；"
            f"分类描述 {counts.get('categorical_traits', 0)} 个；"
            f"跳过 {counts.get('skipped_traits', 0)} 个。"
        ),
    }
    return report


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path.name}")
    return data


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8")


def chapter_statuses(report: Mapping[str, Any]) -> dict[str, str]:
    chapters = report.get("chapters")
    if not isinstance(chapters, Mapping):
        return {}
    statuses: dict[str, str] = {}
    for name, value in chapters.items():
        if isinstance(value, Mapping):
            status = str(value.get("status") or "").strip()
            if status:
                statuses[str(name)] = status
    return statuses


def available_traits(report: Mapping[str, Any], limit: int = 12) -> list[str]:
    preflight = report.get("trait_preflight")
    if isinstance(preflight, Mapping):
        values: list[str] = []
        for key in ("numeric_traits", "categorical_traits", "input_traits"):
            raw_traits = preflight.get(key)
            if not isinstance(raw_traits, list):
                continue
            for item in raw_traits:
                trait = str(item or "").strip()
                if trait and trait not in values:
                    values.append(trait)
                if len(values) >= limit:
                    return values
        if values:
            return values[:limit]
    traits = report.get("traits")
    if not isinstance(traits, Mapping):
        return []
    summary = traits.get("trait_summary")
    fields = traits.get("trait_summary_fields")
    if not isinstance(summary, list) or not isinstance(fields, list):
        return []
    try:
        trait_index = [str(item) for item in fields].index("trait")
    except ValueError:
        return []
    values: list[str] = []
    for row in summary:
        if isinstance(row, Mapping):
            value = str(row.get("trait") or "")
        elif isinstance(row, list | tuple) and len(row) > trait_index:
            value = str(row[trait_index])
        else:
            value = ""
        if value and value not in values:
            values.append(value)
    return values[:limit]


def rows_from_records(fields: Any, records: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    if isinstance(fields, list):
        field_names = [str(item) for item in fields]
    else:
        field_names = []
    rows: list[dict[str, Any]] = []
    for item in records:
        if isinstance(item, Mapping):
            row = {str(key): value for key, value in item.items()}
        elif isinstance(item, list | tuple):
            row = {field_names[index]: value for index, value in enumerate(item) if index < len(field_names)}
        else:
            continue
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def trait_summary_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    traits = report.get("traits")
    if not isinstance(traits, Mapping):
        return []
    return rows_from_records(traits.get("trait_summary_fields"), traits.get("trait_summary"))


def material_rows_for_trait(report: Mapping[str, Any], trait: str, *, limit: int = 5) -> list[dict[str, Any]]:
    materials = report.get("materials")
    if not isinstance(materials, Mapping):
        return []
    by_trait = materials.get("by_trait")
    if not isinstance(by_trait, Mapping):
        return []
    return rows_from_records(materials.get("material_summary_fields"), by_trait.get(trait), limit=limit)


def analysis_for_trait(report: Mapping[str, Any], analysis_name: str, trait: str) -> Mapping[str, Any] | None:
    analyses = report.get("analyses")
    if not isinstance(analyses, Mapping):
        return None
    analysis = analyses.get(analysis_name)
    if not isinstance(analysis, Mapping):
        return None
    by_trait = analysis.get("by_trait")
    if isinstance(by_trait, Mapping) and isinstance(by_trait.get(trait), Mapping):
        return by_trait.get(trait)
    return analysis


def hybrid_blup_for_trait(report: Mapping[str, Any], trait: str) -> Mapping[str, Any] | None:
    return analysis_for_trait(report, "hybrid_blup", trait)


def hybrid_blup_rows(report: Mapping[str, Any], trait: str, *, limit: int = 5) -> list[dict[str, Any]]:
    blup = hybrid_blup_for_trait(report, trait)
    if not isinstance(blup, Mapping):
        return []
    return rows_from_records(blup.get("blup_fields"), blup.get("blup"), limit=limit)


def hybrid_blup_status_line(report: Mapping[str, Any], trait: str) -> str:
    blup = hybrid_blup_for_trait(report, trait)
    if not isinstance(blup, Mapping):
        return "BLUP：当前报告没有提供 analyses.hybrid_blup。"
    status = str(blup.get("status") or "unknown")
    method = str(blup.get("method") or "")
    model = str(blup.get("model") or "")
    response_column = str(blup.get("response_column") or "")
    singular = blup.get("singular_fit")
    warnings = blup.get("warnings")
    warning_count = len(warnings) if isinstance(warnings, list) else 0
    parts = [f"BLUP 状态：{status}"]
    if method:
        parts.append(f"method: {method}")
    if model:
        parts.append(f"model: {model}")
    if response_column:
        parts.append(f"response_column: {response_column}")
    if singular is not None:
        parts.append(f"singular_fit={value_text(singular)}")
    if warning_count:
        parts.append(f"warnings: {warning_count} 条")
    reason = blup.get("reason")
    if reason:
        parts.append(f"原因：{reason}")
    return "; ".join(parts) + "."


def top_blup_line(row: Mapping[str, Any]) -> str:
    return (
        f"{row.get('ped_id') or 'NA'}, "
        f"BLUP {value_text(row.get('hyb_blup'))}, "
        f"排名 {value_text(row.get('rank_hyb_blup'), digits=0)}"
    )


def hybrid_blup_lines(report: Mapping[str, Any], trait: str, *, top_n: int = 5) -> list[str]:
    blup = hybrid_blup_for_trait(report, trait)
    if not isinstance(blup, Mapping):
        return []
    lines = [hybrid_blup_status_line(report, trait)]
    rows = hybrid_blup_rows(report, trait, limit=top_n)
    if rows:
        lines.append("BLUP 前列材料：" + "；".join(top_blup_line(row) for row in rows) + "。")
    return lines


def compact_hybrid_blup_facts(report: Mapping[str, Any], traits: list[str], *, top_n: int = 3) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for trait in traits:
        blup = hybrid_blup_for_trait(report, trait)
        if not isinstance(blup, Mapping):
            continue
        item: dict[str, Any] = {
            "status": blup.get("status"),
            "method": blup.get("method"),
            "model": blup.get("model"),
            "response_column": blup.get("response_column"),
            "singular_fit": blup.get("singular_fit"),
            "reason": blup.get("reason"),
            "warnings": blup.get("warnings"),
            "top": hybrid_blup_rows(report, trait, limit=top_n),
        }
        facts[trait] = {key: value for key, value in item.items() if value not in (None, "", [])}
    return facts


def value_text(value: Any, *, digits: int = 3) -> str:
    if value is None:
        return "NA"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "NA"
        try:
            number = float(stripped)
        except ValueError:
            return stripped
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        number = value
    else:
        return str(value)
    if number != number:
        return "NA"
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def percent_text(value: Any) -> str:
    text = value_text(value, digits=1)
    return "NA" if text == "NA" else f"{text}%"


def first_trait_summary(report: Mapping[str, Any], trait: str) -> dict[str, Any] | None:
    for row in trait_summary_rows(report):
        if str(row.get("trait") or "") == trait:
            return row
    return None


def preflight_classification(report: Mapping[str, Any], trait: str) -> Mapping[str, Any] | None:
    preflight = report.get("trait_preflight")
    if not isinstance(preflight, Mapping):
        return None
    classifications = preflight.get("classifications")
    if not isinstance(classifications, list):
        return None
    for item in classifications:
        if isinstance(item, Mapping) and str(item.get("trait") or "").lower() == trait.lower():
            return item
    return None


def categorical_summary_for_trait(report: Mapping[str, Any], trait: str) -> Mapping[str, Any] | None:
    preflight = report.get("trait_preflight")
    if not isinstance(preflight, Mapping):
        return None
    summary = preflight.get("categorical_trait_summary")
    if not isinstance(summary, Mapping):
        return None
    for key, value in summary.items():
        if str(key).lower() == trait.lower() and isinstance(value, Mapping):
            return value
    return None


def term_label(term: Any) -> str:
    raw = str(term or "")
    labels = {
        "ped_id": "材料/品种",
        "entry_id": "条目",
        "loc_id": "地点",
        "rep_num": "重复/区组",
        "ranges": "行号",
        "pass": "列号",
        "Residuals": "残差",
    }
    return labels.get(raw, raw or "未知项")


def anova_lines(report: Mapping[str, Any], trait: str, *, limit: int = 4) -> list[str]:
    anova = analysis_for_trait(report, "anova", trait)
    if not isinstance(anova, Mapping):
        return []
    rows = rows_from_records(anova.get("term_fields"), anova.get("terms"), limit=limit)
    lines: list[str] = []
    status = str(anova.get("status") or "")
    if status and status != "completed":
        lines.append(f"ANOVA 状态：{status}。")
    for row in rows:
        term = term_label(row.get("term"))
        p_value = row.get("p_value")
        sig = str(row.get("significance") or "").strip()
        lines.append(
            f"{term}: F={value_text(row.get('f_value'))}, p={value_text(p_value, digits=4)}, 显著性={sig or 'NA'}"
        )
    reason = anova.get("reason")
    if reason and not lines:
        lines.append(f"ANOVA 未给出可用表格：{reason}")
    return lines


def lsd_rows(report: Mapping[str, Any], trait: str, *, limit: int = 5) -> list[dict[str, Any]]:
    lsd = analysis_for_trait(report, "lsd_grouping", trait)
    if not isinstance(lsd, Mapping):
        return []
    return rows_from_records(lsd.get("grouping_fields"), lsd.get("grouping"), limit=limit)


def lsd_status_text(report: Mapping[str, Any], trait: str) -> str:
    lsd = analysis_for_trait(report, "lsd_grouping", trait)
    if not isinstance(lsd, Mapping):
        return "LSD 分组：当前报告未提供。"
    status = str(lsd.get("status") or "unknown")
    method = str(lsd.get("method") or "")
    warnings = lsd.get("warnings")
    warning_count = len(warnings) if isinstance(warnings, list) else 0
    suffix = f"，warning {warning_count} 条" if warning_count else ""
    return f"LSD 分组状态：{status}{f'，方法：{method}' if method else ''}{suffix}。"


def count_lsd_overlaps(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        group = str(row.get("group_0_05") or "")
        if len(group.strip()) > 1:
            count += 1
    return count


def top_material_line(row: Mapping[str, Any]) -> str:
    parts = [
        f"{row.get('ped_id') or 'NA'}",
        f"均值 {value_text(row.get('mean'))}",
        f"rank {value_text(row.get('rank'), digits=0)}",
    ]
    if row.get("pct_check_mean") is not None:
        parts.append(f"为 check 均值的 {percent_text(row.get('pct_check_mean'))}")
    if row.get("locations_above_check") is not None:
        parts.append(
            f"{value_text(row.get('locations_above_check'), digits=0)}/{value_text(row.get('location_count'), digits=0)} 个地点高于 check"
        )
    return "，".join(parts)


def trait_fact_lines(report: Mapping[str, Any], trait: str, *, top_n: int = 3) -> list[str]:
    summary = first_trait_summary(report, trait)
    lines: list[str] = []
    preflight_item = preflight_classification(report, trait)
    if preflight_item:
        kind = preflight_item.get("kind") or "unknown"
        status = preflight_item.get("status") or "unknown"
        reason = preflight_item.get("reason") or ""
        lines.append(f"预检状态：{kind}/{status}{f'，原因：{reason}' if reason else ''}。")
        if kind == "categorical":
            categorical = categorical_summary_for_trait(report, trait)
            if categorical:
                top = categorical.get("top_categories") if isinstance(categorical.get("top_categories"), list) else []
                top_text = "；".join(
                    f"{row.get('category')}: {value_text(row.get('count'), digits=0)}"
                    for row in top[:top_n]
                    if isinstance(row, Mapping)
                )
                lines.append(
                    f"分类描述：非空 {value_text(categorical.get('nonempty'), digits=0)} 条，"
                    f"缺失 {value_text(categorical.get('missing'), digits=0)} 条，"
                    f"类别数 {value_text(categorical.get('category_count'), digits=0)}"
                    f"{f'，主要类别：{top_text}' if top_text else ''}。"
                )
            return lines
        if kind == "skipped":
            return lines
    if summary:
        lines.append(
            f"{trait}: 观测 {value_text(summary.get('observations'), digits=0)} 条，材料 {value_text(summary.get('material_count'), digits=0)} 个，"
            f"地点 {value_text(summary.get('location_count'), digits=0)} 个，均值 {value_text(summary.get('mean'))}，"
            f"CV {percent_text(summary.get('cv'))}，check 均值 {value_text(summary.get('check_mean'))}，质量标记 {summary.get('quality') or 'NA'}。"
        )
    materials = material_rows_for_trait(report, trait, limit=top_n)
    if materials:
        lines.append("材料排名前列：" + "；".join(top_material_line(row) for row in materials) + "。")
    blup = hybrid_blup_lines(report, trait, top_n=top_n)
    if blup:
        lines.append("BLUP: " + " ".join(blup))
    anova = anova_lines(report, trait, limit=3)
    if anova:
        lines.append("ANOVA：" + "；".join(anova) + "。")
    lsd = lsd_rows(report, trait, limit=top_n)
    if lsd:
        lsd_bits = [
            f"{row.get('ped_id') or 'NA'}: LSMean {value_text(row.get('lsmean'))}, 0.05组 {row.get('group_0_05') or 'NA'}"
            for row in lsd
        ]
        overlap = count_lsd_overlaps(lsd)
        lines.append("LSD：" + lsd_status_text(report, trait) + " 前列分组：" + "；".join(lsd_bits) + f"。前 {len(lsd)} 个中重叠分组 {overlap} 个。")
    return lines


def compact_result_facts(report: Mapping[str, Any], *, trait_limit: int = 5) -> dict[str, Any]:
    metadata = report.get("metadata") if isinstance(report.get("metadata"), Mapping) else {}
    counts = metadata.get("counts") if isinstance(metadata.get("counts"), Mapping) else {}
    traits = available_traits(report, limit=trait_limit)
    preflight = report.get("trait_preflight") if isinstance(report.get("trait_preflight"), Mapping) else {}
    return {
        "format": report.get("format") or "field-analysis-report",
        "design": metadata.get("design"),
        "run_id": metadata.get("run_id"),
        "counts": dict(counts),
        "available_traits": traits,
        "chapter_statuses": chapter_statuses(report),
        "trait_highlights": {trait: trait_fact_lines(report, trait, top_n=3) for trait in traits},
        "hybrid_blup": compact_hybrid_blup_facts(report, traits),
        "trait_preflight": {
            "counts": dict(preflight.get("counts") or {}),
            "input_traits": list(preflight.get("input_traits") or []),
            "numeric_traits": list(preflight.get("numeric_traits") or []),
            "categorical_traits": list(preflight.get("categorical_traits") or []),
            "skipped_traits": list(preflight.get("skipped_traits") or []),
            "classifications": list(preflight.get("classifications") or []),
            "check_type": dict(preflight.get("check_type") or {}),
            "categorical_trait_summary": dict(preflight.get("categorical_trait_summary") or {}),
        },
    }


def requested_trait(payload: Mapping[str, Any], report: Mapping[str, Any]) -> str | None:
    explicit = payload.get("trait") or payload.get("target_trait") or payload.get("trait_id")
    traits = available_traits(report, limit=1000)
    if explicit:
        explicit_text = str(explicit).strip()
        for trait in traits:
            if trait.lower() == explicit_text.lower():
                return trait
    query = " ".join(
        str(value or "")
        for value in (
            payload.get("query"),
            payload.get("message"),
            payload.get("user_text"),
            payload.get("analysis_request"),
        )
    )
    for trait in traits:
        if trait and re.search(rf"(?<![A-Za-z0-9_]){re.escape(trait)}(?![A-Za-z0-9_])", query, flags=re.IGNORECASE):
            return trait
    match = re.search(r"(?<![A-Za-z0-9_])(T\d{2,6})(?![A-Za-z0-9_])", query, flags=re.IGNORECASE)
    if match:
        candidate = match.group(1).upper()
        for trait in traits:
            if trait.upper() == candidate:
                return trait
    return None


def build_trait_answer(*, design: str, run_id: str, report: Mapping[str, Any], trait: str) -> str:
    lines = trait_fact_lines(report, trait, top_n=5)
    if not lines:
        available = "、".join(available_traits(report)) or "未读取到性状列表"
        return f"当前报告中没有找到 {trait} 的可解释结果。可用性状：{available}。"
    return (
        f"基于本轮 field-analysis JSON 事实源，单独解读 {trait}：\n\n"
        + "\n".join(f"- {line}" for line in lines)
        + f"\n\n结论只来自当前 {design} 分析结果（run_id: {run_id}）。后续可以继续追问 {trait} 的某个材料、check 对比、ANOVA 项或 LSD 分组。"
    )


def build_answer(*, design: str, run_id: str, report: Mapping[str, Any]) -> str:
    facts = compact_result_facts(report)
    counts = facts.get("counts") if isinstance(facts.get("counts"), Mapping) else {}
    preflight = facts.get("trait_preflight") if isinstance(facts.get("trait_preflight"), Mapping) else {}
    preflight_counts = preflight.get("counts") if isinstance(preflight.get("counts"), Mapping) else {}
    trait_text = "、".join(available_traits(report, limit=6)) or "未读取到性状列表"
    metadata = report.get("metadata") if isinstance(report.get("metadata"), Mapping) else {}
    analysis_profile = str(metadata.get("analysis_profile") or "full_report")
    if analysis_profile == "trait_preflight_only":
        lead = (
            "田间数据已完成性状预检；由于没有满足完整模型分析要求的连续数值性状，"
            "ANOVA/LSD/BLUP 未运行。"
        )
    else:
        lead = "田间数据分析计算已完成。"
    return (
        f"{lead}请作为最终回答生成器，严格基于本次 skill 输出的 "
        "structured_content.result_facts / result_facts / report_json / session_json 作答，不要补充 JSON 中没有的统计结论。\n\n"
        f"事实锚点：设计类型 {design}；run_id {run_id}；"
        f"{value_text(counts.get('observations'), digits=0)} 条观测，"
        f"{value_text(counts.get('materials'), digits=0)} 个材料，"
        f"{value_text(counts.get('locations'), digits=0)} 个地点，"
        f"{value_text(counts.get('reps'), digits=0)} 个重复/区组，"
        f"{value_text(counts.get('traits'), digits=0)} 个性状（{trait_text}）。\n\n"
        f"性状预检：输入性状 {value_text(preflight_counts.get('input_traits'), digits=0)} 个，"
        f"进入完整数值分析 {value_text(preflight_counts.get('numeric_traits'), digits=0)} 个，"
        f"分类描述 {value_text(preflight_counts.get('categorical_traits'), digits=0)} 个，"
        f"跳过 {value_text(preflight_counts.get('skipped_traits'), digits=0)} 个。"
        "分类性状只解释类别分布，空列、常量列和不满足要求的性状要明确列出原因。\n\n"
        "推荐展示格式：先给一个概览表，再给性状预检与分流摘要，再给主要数值性状材料排名表，然后解释 ANOVA/LSD/check 对比和 BLUP 的可用结论，"
        "最后列出 HTML 报告和可继续追问方向。若某项事实缺失或章节失败，请明确说该项未在 JSON 中提供。"
    )


def build_structured_content(*, design: str, run_id: str, report: Mapping[str, Any], mode: str) -> dict[str, Any]:
    statuses = chapter_statuses(report)
    completed = sum(1 for status in statuses.values() if status == "completed")
    warning = sum(1 for status in statuses.values() if status == "completed_with_warnings")
    failed = sum(1 for status in statuses.values() if status == "failed")
    return {
        "kind": "field-analysis-finalizer-context",
        "mode": mode,
        "design": design,
        "run_id": run_id,
        "format": "field-analysis-report",
        "result_facts": compact_result_facts(report),
        "chapter_summary": {
            "completed": completed,
            "completed_with_warnings": warning,
            "failed": failed,
            "statuses": statuses,
        },
        "response_contract": {
            "language": "zh-CN",
            "style": "natural_breeding_report",
            "must_use_only_json_facts": True,
            "must_not_invent_missing_statistics": True,
            "preferred_sections": [
                "概览表",
                "性状预检与分流",
                "主要性状材料排名",
                "分类性状描述统计",
                "ANOVA/LSD/check 对比解释",
                "BLUP 结果或未计算原因",
                "报告文件",
                "可继续追问方向",
            ],
            "missing_fact_policy": "如果 JSON 中没有 p 值、分组、check 均值、BLUP 或稳定性结果，明确说明该项未提供或章节未完成；BLUP 不做兜底推断；分类、空列和常量性状不得冒充完整模型分析。",
        },
    }


def write_session_files(
    *,
    api_result: Mapping[str, Any],
    report: Mapping[str, Any],
    input_path: Path,
    output_dir: Path,
    design: str,
    run_id: str,
    preflight: Mapping[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    report_file = output_dir / f"field-analysis-{design}-full-report-{run_id}.json"
    summary_file = output_dir / f"field-analysis-summary-{design}-full-report-{run_id}.json"
    session_file = output_dir / ".field-analysis-session.json"
    write_json(report_file, report)
    analysis_profile = str(api_result.get("analysis_profile") or "full_report")
    compute_backend = str(
        api_result.get("compute_backend")
        or ("skill_trait_preflight" if analysis_profile == "trait_preflight_only" else "breedstat2_api")
    )
    summary = {
        "ok": True,
        "design": design,
        "analysis_profile": analysis_profile,
        "run_id": run_id,
        "input": str(input_path),
        "output_dir": str(output_dir),
        "output_json": str(report_file),
        "summary_json": str(summary_file),
        "session_json": str(session_file),
        "format": "field-analysis-report",
        "chapters": list((report.get("chapters") or {}).keys()) if isinstance(report.get("chapters"), Mapping) else [],
        "compute_backend": compute_backend,
        "trait_preflight": dict(preflight or {}),
    }
    write_json(summary_file, summary)
    write_json(
        session_file,
        {
            "active_report": str(report_file),
            "active_summary": str(summary_file),
            "format": "field-analysis-report",
            "design": design,
            "analysis_profile": analysis_profile,
            "run_id": run_id,
            "input": str(input_path),
            "output_dir": str(output_dir),
            "compute_backend": compute_backend,
            "available_traits": available_traits(report),
            "available_chapters": summary["chapters"],
            "trait_preflight": dict(preflight or {}),
        },
    )
    return report_file, summary_file, session_file


def render_html_report(*, report_file: Path, html_file: Path) -> str | None:
    try:
        render_report_file(report_file, html_file)
    except Exception as exc:
        return str(exc)[-1200:]
    return None


def active_session_file(output_dir: Path) -> Path:
    return output_dir / ".field-analysis-session.json"


def read_active_report(output_dir: Path) -> tuple[dict[str, Any], dict[str, Any], Path] | None:
    session_path = active_session_file(output_dir)
    if not session_path.exists():
        return None
    session = read_json(session_path)
    report_path_raw = session.get("active_report")
    if not isinstance(report_path_raw, str) or not report_path_raw.strip():
        return None
    report_path = Path(report_path_raw)
    if not report_path.is_absolute():
        report_path = (output_dir / report_path).resolve()
    if not report_path.exists():
        return None
    return read_json(report_path), session, report_path


def query_text(payload: Mapping[str, Any]) -> str:
    return " ".join(str(payload.get(key) or "") for key in ("query", "message", "user_text", "analysis_request"))


def is_blup_query(query: str) -> bool:
    return bool(re.search(r"BLUP|blup|hyb_blup|hybrid_blup|\u6750\u6599\s*BLUP", query, flags=re.IGNORECASE))


def wants_followup_answer(payload: Mapping[str, Any]) -> bool:
    query = query_text(payload)
    if not query.strip():
        return False
    followup_patterns = [
        r"\u89e3\u8bfb",
        r"\u67e5\u8be2",
        r"\u8ffd\u95ee",
        r"\u5355\u72ec",
        r"\u6392\u540d",
        r"\u6700\u597d",
        r"\u6700\u5dee",
        r"\u663e\u8457",
        r"ANOVA",
        r"LSD",
        r"check",
        r"\u5bf9\u7167",
        r"BLUP",
        r"blup",
        r"hyb_blup",
        r"hybrid_blup",
        r"T\d{2,6}",
    ]
    return any(re.search(pattern, query, flags=re.IGNORECASE) for pattern in followup_patterns)


def build_blup_followup_answer(*, payload: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    trait = requested_trait(payload, report)
    traits = [trait] if trait else available_traits(report, limit=8)
    blocks = []
    not_completed = []
    for trait_id in traits:
        blup = hybrid_blup_for_trait(report, trait_id)
        if not isinstance(blup, Mapping):
            not_completed.append(f"{trait_id}: 当前报告没有提供 analyses.hybrid_blup。")
            continue
        status = str(blup.get("status") or "unknown")
        if status != "completed":
            reason = blup.get("reason") or "计算后端未返回原因"
            not_completed.append(f"{trait_id}: BLUP 状态为 {status}；原因：{reason}。")
            continue
        rows = hybrid_blup_rows(report, trait_id, limit=5)
        if rows:
            blocks.append(f"{trait_id}: " + " ".join(hybrid_blup_lines(report, trait_id, top_n=5)))
        else:
            not_completed.append(f"{trait_id}: BLUP 状态为 completed，但没有返回 blup 明细行。")
    if blocks:
        answer = "基于当前 field-analysis JSON，BLUP 结果如下：\n\n" + "\n".join(f"- {block}" for block in blocks)
        if not_completed:
            answer += "\n\n以下性状没有可展示的 BLUP 结果：\n" + "\n".join(f"- {item}" for item in not_completed)
        return answer
    if not_completed:
        return "当前 field-analysis JSON 没有可展示的 BLUP 结果：\n\n" + "\n".join(f"- {item}" for item in not_completed)
    return "当前 field-analysis JSON 没有可展示的 BLUP 结果。"


def build_followup_answer(*, payload: Mapping[str, Any], report: Mapping[str, Any], session: Mapping[str, Any]) -> str:
    design = str(session.get("design") or (report.get("metadata") or {}).get("design") or "unknown")
    run_id = str(session.get("run_id") or (report.get("metadata") or {}).get("run_id") or "field_analysis")
    query = query_text(payload)
    if is_blup_query(query):
        return build_blup_followup_answer(payload=payload, report=report)
    trait = requested_trait(payload, report)
    if trait:
        return build_trait_answer(design=design, run_id=run_id, report=report, trait=trait)
    if re.search(r"\u6392\u540d|\u6700\u597d|top|\u524d\d+", query, flags=re.IGNORECASE):
        blocks = []
        for trait_id in available_traits(report, limit=5):
            materials = material_rows_for_trait(report, trait_id, limit=3)
            if materials:
                blocks.append(f"{trait_id}: " + "；".join(top_material_line(row) for row in materials))
        if blocks:
            return "基于当前 field-analysis JSON，材料排名前列如下：\n\n" + "\n".join(f"- {block}" for block in blocks)
    if re.search(r"\u663e\u8457|ANOVA|\u65b9\u5dee", query, flags=re.IGNORECASE):
        blocks = []
        for trait_id in available_traits(report, limit=8):
            lines = anova_lines(report, trait_id, limit=3)
            if lines:
                blocks.append(f"{trait_id}: " + "；".join(lines))
        if blocks:
            return "基于当前 field-analysis JSON，ANOVA 结果摘要如下：\n\n" + "\n".join(f"- {block}" for block in blocks)
    return build_answer(design=design, run_id=run_id, report=report)

def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        json_response(failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0
    if not isinstance(payload, dict):
        json_response(failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0

    design = resolve_design(payload)
    missing: list[str] = []
    if design is None:
        missing.append("design")

    run_id = safe_run_id(payload.get("run_id") or payload.get("run-id") or "field_analysis")
    output_dir = Path(os.environ.get("MAF_SKILL_OUTPUT_DIR") or "outputs").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="field-analysis-input-") as tmp:
        work_dir = Path(tmp)
        input_path = resolve_input_file(payload, work_dir)
        if input_path is None:
            active = read_active_report(output_dir)
            if active is not None and wants_followup_answer(payload):
                report, session, report_path = active
                json_response(
                    {
                        "ok": True,
                        "answer": build_followup_answer(payload=payload, report=report, session=session),
                        "design": session.get("design"),
                        "run_id": session.get("run_id"),
                        "format": "field-analysis-report",
                        "mode": "followup",
                        "compute_backend": session.get("compute_backend") or "breedstat2_api",
                        "report_json": str(report_path),
                        "session_json": str(active_session_file(output_dir)),
                        "chapter_statuses": chapter_statuses(report),
                        "available_traits": available_traits(report),
                        "trait_preflight": report.get("trait_preflight") if isinstance(report.get("trait_preflight"), Mapping) else {},
                        "result_facts": compact_result_facts(report),
                        "structured_content": build_structured_content(
                            design=str(session.get("design") or "unknown"),
                            run_id=str(session.get("run_id") or "field_analysis"),
                            report=report,
                            mode="followup",
                        ),
                    }
                )
                return 0
            missing.append("field_data")
        if missing:
            json_response(
                failure(
                    "缺少田间数据分析必需输入：请上传 CSV 或 XLSX 宽表田间表型数据，并说明设计类型 rcbd 或 diagonal。",
                    missing=missing,
                    error_type="missing_input",
                )
            )
            return 0

        try:
            input_records = read_csv_records(input_path)
            input_fields = fieldnames_from_records(input_records)
            preflight = build_trait_preflight(input_records, input_fields)
            numeric_records = prepare_records_for_numeric_backend(input_records, preflight)
            if not preflight.get("numeric_traits"):
                api_result = {
                    "ok": True,
                    "analysis_profile": "trait_preflight_only",
                    "note": "no numeric traits; model analysis was not run",
                }
                report = build_preflight_only_report(
                    records=input_records,
                    fields=input_fields,
                    design=str(design),
                    run_id=run_id,
                    preflight=preflight,
                )
            else:
                api_result = run_breedstat2_report(records=numeric_records, design=str(design), run_id=run_id)
                report = extract_api_report(api_result)
                report = attach_preflight_to_report(report, preflight)
            report_file, summary_file, _ = write_session_files(
                api_result=api_result,
                report=report,
                input_path=input_path,
                output_dir=output_dir,
                design=str(design),
                run_id=run_id,
                preflight=preflight,
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            json_response(
                failure(
                    "breedstat2 /field-analysis/report 调用失败：" + str(exc)[-1200:],
                    error_type="breedstat2_api_failed",
                )
            )
            return 0

    try:
        report = read_json(report_file)
        summary = read_json(summary_file)
    except Exception as exc:
        json_response(failure(f"田间数据分析已运行，但读取报告失败：{exc}", error_type="report_read_failed"))
        return 0

    html_file = output_dir / f"field-analysis-{design}-report-{run_id}.html"
    diagnostic = render_html_report(report_file=report_file, html_file=html_file)
    if diagnostic is not None:
        json_response(failure("HTML 报告生成失败：" + diagnostic.strip(), error_type="html_render_failed"))
        return 0

    json_response(
        {
            "ok": True,
            "answer": build_answer(design=str(design), run_id=run_id, report=report),
            "design": design,
            "run_id": run_id,
            "format": "field-analysis-report",
            "compute_backend": summary.get("compute_backend") or "breedstat2_api",
            "mode": "analysis",
            "report_json": str(report_file),
            "summary_json": str(summary_file),
            "session_json": str(active_session_file(output_dir)),
            "chapter_statuses": chapter_statuses(report),
            "available_traits": available_traits(report),
            "trait_preflight": report.get("trait_preflight") if isinstance(report.get("trait_preflight"), Mapping) else {},
            "result_facts": compact_result_facts(report),
            "structured_content": build_structured_content(
                design=str(design),
                run_id=run_id,
                report=report,
                mode="analysis",
            ),
            "output_files": [
                {
                    "path": f"outputs/{html_file.name}",
                    "filename": html_file.name,
                    "mime_type": "text/html",
                    "label": "田间数据分析 HTML 报告",
                    "summary": "面向用户阅读的田间数据分析报告。",
                }
            ],
        }
    )
    return 0


def trim_excel_rows(rows: list[list[str]]) -> list[list[str]]:
    while rows and not any(str(cell).strip() for cell in rows[-1]):
        rows.pop()
    max_len = max((len(row) for row in rows), default=0)
    return [row + [""] * (max_len - len(row)) for row in rows]


def read_excel_rows(path: Path) -> list[list[str]]:
    content = path.read_bytes()
    if content[:2] == b"PK":
        return read_xlsx_rows(path)
    raise RuntimeError("Only .xlsx Excel files are supported by the field-analysis Python runner.")


def read_xlsx_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared = parse_shared_strings(zf) if "xl/sharedStrings.xml" in names else []
        sheet_name = first_sheet_name(zf)
        root = ET.fromstring(zf.read(sheet_name))
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//main:sheetData/main:row", ns):
        values: dict[int, str] = {}
        for cell in row.findall("main:c", ns):
            ref = cell.attrib.get("r", "")
            col = xlsx_col_index(ref)
            cell_type = cell.attrib.get("t", "")
            value_node = cell.find("main:v", ns)
            inline_node = cell.find("main:is/main:t", ns)
            if cell_type == "inlineStr" and inline_node is not None:
                value = inline_node.text or ""
            elif value_node is None:
                value = ""
            elif cell_type == "s":
                idx = int(value_node.text or "0")
                value = shared[idx] if idx < len(shared) else ""
            else:
                value = value_node.text or ""
            values[col] = value
        if values:
            rows.append([values.get(i, "") for i in range(max(values) + 1)])
    return trim_excel_rows(rows)


def parse_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out: list[str] = []
    for item in root.findall("main:si", ns):
        texts = [node.text or "" for node in item.findall(".//main:t", ns)]
        out.append("".join(texts))
    return out


def first_sheet_name(zf: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    first_sheet = workbook.find(".//main:sheets/main:sheet", ns)
    if first_sheet is None:
        raise RuntimeError("Excel workbook has no sheets.")
    rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    for rel in rels.findall("rel:Relationship", ns):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "")
            return "xl/" + target.lstrip("/")
    raise RuntimeError("Excel workbook first sheet relationship not found.")


def xlsx_col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return max(value - 1, 0)


if __name__ == "__main__":
    raise SystemExit(main())
