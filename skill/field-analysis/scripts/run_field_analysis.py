from __future__ import annotations

import base64
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

SKILL_DIR = Path(__file__).resolve().parents[1]
R_RUNNER = Path(__file__).resolve().with_name("run_field_analysis.R")
REQUIRED_COLUMNS = (
    "loc_id",
    "rep_num",
    "entry_id",
    "ped_id",
    "trait",
    "value",
    "check_type",
    "ranges",
    "pass",
)
_ALLOWED_DESIGNS = {"rcbd", "diagonal"}


def _json_response(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def _failure(answer: str, *, missing: list[str] | None = None, error_type: str = "field_analysis_error") -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "answer": answer, "error": {"type": error_type, "message": answer}}
    if missing:
        result["missing"] = missing
    return result


def find_rscript() -> str:
    for candidate in (
        shutil.which("Rscript"),
        "/usr/local/bin/Rscript",
        "/opt/homebrew/bin/Rscript",
        "/Library/Frameworks/R.framework/Resources/bin/Rscript",
        "/usr/bin/Rscript",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise RuntimeError("Rscript is not available in the backend runtime")


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


def _safe_run_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        text = "field_analysis"
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text[:80] or "field_analysis"


def _resolve_design(payload: Mapping[str, Any]) -> str | None:
    explicit = str(payload.get("design") or payload.get("design_type") or "").strip().lower()
    if explicit in _ALLOWED_DESIGNS:
        return explicit
    query = str(payload.get("query") or "").lower()
    if re.search(r"\b(rcbd|randomi[sz]ed complete block|随机区组|随机完全区组)\b", query, flags=re.IGNORECASE):
        return "rcbd"
    if re.search(r"\b(diagonal|对角线|增广)\b", query, flags=re.IGNORECASE):
        return "diagonal"
    return None


def _artifact_filename(artifact: Mapping[str, Any], default: str = "field_data.csv") -> str:
    raw = str(artifact.get("filename") or default)
    name = Path(raw).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return default
    ext = Path(name).suffix.lower()
    if ext not in {".csv", ".json"}:
        return f"{Path(name).stem or 'field_data'}.csv"
    return name


def _decode_artifact_content(artifact: Mapping[str, Any]) -> bytes | None:
    if isinstance(artifact.get("content"), str):
        return str(artifact["content"]).encode("utf-8")
    if isinstance(artifact.get("content_base64"), str):
        try:
            return base64.b64decode(str(artifact["content_base64"]), validate=True)
        except Exception:
            return None
    return None


def _write_input_from_artifact(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    artifacts = payload.get("uploaded_artifacts")
    if not isinstance(artifacts, list | tuple):
        return None
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        content = _decode_artifact_content(item)
        if content is None:
            continue
        filename = _artifact_filename(item)
        path = work_dir / filename
        path.write_bytes(content)
        return path
    return None


def _write_input_from_metadata(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    input_data = metadata.get("input_data") or payload.get("input_data")
    if input_data is None:
        return None
    path = work_dir / "field_data.csv"
    if isinstance(input_data, str):
        path.write_text(input_data, encoding="utf-8")
        return path
    if isinstance(input_data, list | tuple) and all(isinstance(row, Mapping) for row in input_data):
        rows = [dict(row) for row in input_data]
        fieldnames: list[str] = []
        for key in REQUIRED_COLUMNS:
            if any(key in row for row in rows):
                fieldnames.append(key)
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


def _resolve_input_file(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    uploaded = _write_input_from_artifact(payload, work_dir)
    if uploaded is not None:
        return uploaded
    metadata_input = _write_input_from_metadata(payload, work_dir)
    if metadata_input is not None:
        return metadata_input
    for key in ("input_file", "file_path", "path"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file() and candidate.suffix.lower() in {".csv", ".json"}:
                return candidate.resolve()
    return None


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path.name}")
    return data


def _chapter_statuses(report: Mapping[str, Any]) -> dict[str, str]:
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


def _available_traits(report: Mapping[str, Any], limit: int = 12) -> list[str]:
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
        if isinstance(row, list | tuple) and len(row) > trait_index:
            value = str(row[trait_index])
            if value and value not in values:
                values.append(value)
    return values[:limit]


def _build_answer(*, design: str, run_id: str, summary: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    statuses = _chapter_statuses(report)
    completed = sum(1 for status in statuses.values() if status == "completed")
    warning = sum(1 for status in statuses.values() if status == "completed_with_warnings")
    failed = sum(1 for status in statuses.values() if status == "failed")
    traits = _available_traits(report)
    chapter_text = "、".join(f"{key}={value}" for key, value in list(statuses.items())[:8]) or "未读取到章节状态"
    trait_text = "、".join(traits) if traits else "未读取到性状列表"
    return (
        f"田间数据分析已完成。设计类型：{design}；run_id：{run_id}。"
        f"章节状态：completed {completed} 个，completed_with_warnings {warning} 个，failed {failed} 个。"
        f"主要章节：{chapter_text}。"
        f"可用性状：{trait_text}。"
        "已生成完整报告 JSON 和摘要 JSON，可继续追问数据质量、材料排名、check 对比、ANOVA/LSD、空间校正或稳定性结果。"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _json_response(_failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0
    if not isinstance(payload, dict):
        _json_response(_failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0

    design = _resolve_design(payload)
    missing: list[str] = []
    if design is None:
        missing.append("design")

    run_id = _safe_run_id(payload.get("run_id") or payload.get("run-id") or "field_analysis")
    output_dir = Path(os.environ.get("MAF_SKILL_OUTPUT_DIR") or "outputs").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="field-analysis-input-") as tmp:
        work_dir = Path(tmp)
        input_path = _resolve_input_file(payload, work_dir)
        if input_path is None:
            missing.append("field_data")
        if missing:
            _json_response(
                _failure(
                    "缺少田间数据分析必需输入：请上传 CSV/JSON 田间表型数据，并说明设计类型 rcbd 或 diagonal。",
                    missing=missing,
                    error_type="missing_input",
                )
            )
            return 0

        try:
            rscript = find_rscript()
        except RuntimeError as exc:
            _json_response(_failure(str(exc), error_type="runtime_unavailable"))
            return 0

        command = [
            rscript,
            str(R_RUNNER),
            "--input",
            str(input_path),
            "--design",
            str(design),
            "--output-dir",
            str(output_dir),
            "--run-id",
            run_id,
        ]
        process = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
            env=_rscript_env(),
        )
        if process.returncode != 0:
            diagnostic = (process.stderr or process.stdout or "R field analysis failed")[-1200:]
            _json_response(
                _failure(
                    "田间数据分析执行失败：" + diagnostic.strip(),
                    error_type="rscript_failed",
                )
            )
            return 0

    report_file = output_dir / f"field-analysis-{design}-full-report-{run_id}.json"
    summary_file = output_dir / f"field-analysis-summary-{design}-full-report-{run_id}.json"
    try:
        report = _read_json(report_file)
        summary = _read_json(summary_file)
    except Exception as exc:
        _json_response(_failure(f"田间数据分析已运行，但读取报告失败：{exc}", error_type="report_read_failed"))
        return 0

    result = {
        "ok": True,
        "answer": _build_answer(design=design, run_id=run_id, summary=summary, report=report),
        "design": design,
        "run_id": run_id,
        "format": "field-analysis-report-v1",
        "chapter_statuses": _chapter_statuses(report),
        "available_traits": _available_traits(report),
        "output_files": [
            {
                "path": f"outputs/{report_file.name}",
                "filename": report_file.name,
                "mime_type": "application/json",
                "label": "完整田间数据分析报告 JSON",
                "summary": "章节化 field-analysis-report-v1 完整报告。",
            },
            {
                "path": f"outputs/{summary_file.name}",
                "filename": summary_file.name,
                "mime_type": "application/json",
                "label": "田间数据分析摘要 JSON",
                "summary": "报告路径、章节和运行参数摘要。",
            },
        ],
    }
    _json_response(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
