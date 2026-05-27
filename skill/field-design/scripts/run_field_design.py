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
from typing import Any, Iterable, Mapping

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
ALLOWED_DESIGNS = {"rcbd", "diagonal", "interval"}


def emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def fail(answer: str, *, missing: list[str] | None = None, error_type: str = "field_design_error", **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "answer": answer, "error": {"type": error_type, "message": answer}}
    if missing:
        result["missing"] = missing
    result.update(extra)
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


def safe_token(value: Any, default: str = "field_design") -> str:
    text = str(value or "").strip() or default
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text[:80] or default


def normalize_design(value: Any, query: str = "") -> str | None:
    text = str(value or "").strip().lower()
    source = f"{text} {query.lower()}"
    if re.search(r"\b(rcbd|randomi[sz]ed complete block|randomized complete block)\b|随机区组|随机完全区组|完全随机区组", source):
        return "rcbd"
    if re.search(r"\bdiagonal\b|对角线|增广", source):
        return "diagonal"
    if re.search(r"\binterval\b|间比", source):
        return "interval"
    return text if text in ALLOWED_DESIGNS else None


def get_positive_int(payload: Mapping[str, Any], key: str, query_patterns: Iterable[str] = ()) -> int | None:
    raw = payload.get(key)
    if raw is not None and not isinstance(raw, bool):
        try:
            value = int(str(raw).strip())
            return value if value > 0 else None
        except ValueError:
            return None
    query = str(payload.get("query") or "")
    for pattern in query_patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            try:
                value = int(next(group for group in match.groups() if group))
                return value if value > 0 else None
            except (StopIteration, ValueError):
                continue
    return None


def get_bool(payload: Mapping[str, Any], key: str, default: bool) -> bool:
    raw = payload.get(key)
    if raw is not None and not isinstance(raw, bool):
        text = str(raw).strip().lower()
        if text in {"true", "t", "1", "yes", "y", "是", "随机"}:
            return True
        if text in {"false", "f", "0", "no", "n", "否", "不", "不随机", "不要随机", "保持原始顺序"}:
            return False
    if isinstance(raw, bool):
        return raw
    query = str(payload.get("query") or "")
    if re.search(r"不要随机|不随机|保持原始顺序|按.*清单顺序", query):
        return False
    return default


def get_string(payload: Mapping[str, Any], key: str, default: str | None = None) -> str | None:
    value = payload.get(key)
    if value is None or isinstance(value, bool):
        return default
    text = str(value).strip()
    return text or default


def artifact_filename(artifact: Mapping[str, Any], default: str = "materials.csv") -> str:
    raw = str(artifact.get("filename") or default)
    name = Path(raw).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return default
    ext = Path(name).suffix.lower()
    if ext not in {".csv", ".json"}:
        return f"{Path(name).stem or 'materials'}.csv"
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
        return path
    return None


def write_input_from_metadata(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    material_data = metadata.get("material_data") or metadata.get("input_data") or payload.get("material_data")
    if material_data is None:
        return None
    path = work_dir / "materials.csv"
    if isinstance(material_data, str):
        path.write_text(material_data, encoding="utf-8")
        return path
    if isinstance(material_data, list | tuple) and all(isinstance(row, Mapping) for row in material_data):
        rows = [dict(row) for row in material_data]
        fieldnames: list[str] = []
        for preferred in ("ped_id", "plot_id", "hyb_check", "design_check", "set"):
            if any(preferred in row for row in rows):
                fieldnames.append(preferred)
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
            if candidate.exists() and candidate.is_file() and candidate.suffix.lower() in {".csv", ".json"}:
                return candidate.resolve()
    return None


def run_command(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env={"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    )


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path.name}")
    return data


def error_from_process(process: subprocess.CompletedProcess[str], result_path: Path | None = None) -> str:
    if result_path is not None and result_path.exists():
        try:
            data = read_json(result_path)
            error = data.get("error")
            if isinstance(error, Mapping) and error.get("message"):
                return str(error.get("message"))
        except Exception:
            pass
    return (process.stderr or process.stdout or f"process exited with {process.returncode}")[-1200:].strip()


def preview_rows(csv_path: Path, columns: list[str], limit: int = 10) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        available = [column for column in columns if column in (reader.fieldnames or [])]
        rows: list[dict[str, str]] = []
        for row in reader:
            rows.append({column: str(row.get(column, "")) for column in available})
            if len(rows) >= limit:
                break
    return available, rows


def markdown_table(columns: list[str], rows: list[Mapping[str, Any]]) -> str:
    if not columns or not rows:
        return ""
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def build_output_file(path: Path, *, mime_type: str, label: str, summary: str) -> dict[str, str]:
    return {
        "path": f"outputs/{path.name}",
        "filename": path.name,
        "mime_type": mime_type,
        "label": label,
        "summary": summary,
    }


def run_design_pipeline(payload: Mapping[str, Any], input_path: Path, output_dir: Path, rscript: str, design: str) -> dict[str, Any]:
    run_id = safe_token(payload.get("run_id") or payload.get("run-id") or design, design)
    seed = get_positive_int(payload, "seed", (r"(?:seed|随机种子)\s*[:：=]?\s*(\d+)",)) or 20260512
    planter = get_string(payload, "planter", "serpentine") or "serpentine"
    if planter not in {"serpentine", "cartesian"}:
        return fail("planter 必须是 serpentine 或 cartesian。", error_type="invalid_parameter")

    result_json = output_dir / f"field-design-{design}-{run_id}-result.json"
    fieldbook_csv = output_dir / f"field-design-{design}-{run_id}-fieldbook.csv"
    layout_html = output_dir / f"field-design-{design}-{run_id}-layout.html"

    if design == "rcbd":
        blocks = get_positive_int(
            payload,
            "blocks",
            (
                r"(?:blocks?|区组数|区组|重复数|重复|reps?|replications?)\s*[:：=]?\s*(\d+)",
                r"(\d+)\s*(?:个|次)?(?:区组|重复|rep|reps|blocks?)",
            ),
        )
        if blocks is None:
            return fail("缺少 RCBD 必需参数 blocks/重复数。", missing=["blocks"], error_type="missing_input")
        command = [
            rscript,
            str(SCRIPTS_DIR / "run_rcbd_local.R"),
            "--input",
            str(input_path),
            "--blocks",
            str(blocks),
            "--planter",
            planter,
            "--seed",
            str(seed),
            "--output",
            str(result_json),
        ]
        columns = ["plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type"]
        title = "Field Design RCBD Layout"
        render_script = "render_rcbd_interval_layout_html.R"
        extra_parameters = {"blocks": blocks}
    elif design == "diagonal":
        ncols = get_positive_int(payload, "ncols", (r"(?:ncols|列数|田块列数)\s*[:：=]?\s*(\d+)", r"(\d+)\s*(?:列|columns?)"))
        if ncols is None:
            return fail("缺少 Diagonal 必需参数 ncols/田块列数。", missing=["ncols"], error_type="missing_input")
        ck_ratio = (get_string(payload, "ck_ratio") or get_string(payload, "ck-ratio") or "A").upper()
        randomize = get_bool(payload, "randomize", True)
        command = [
            rscript,
            str(SCRIPTS_DIR / "run_diagonal_local.R"),
            "--input",
            str(input_path),
            "--ncols",
            str(ncols),
            "--ck-ratio",
            ck_ratio,
            "--planter",
            planter,
            "--randomize",
            "true" if randomize else "false",
            "--seed",
            str(seed),
            "--output",
            str(result_json),
        ]
        columns = ["plots", "ped_id", "hyb_type", "ranges", "pass", "set", "design_check"]
        title = "Field Design Diagonal Layout"
        render_script = "render_diagonal_layout_html.R"
        extra_parameters = {"ncols": ncols, "requested_ck_ratio": ck_ratio, "randomize": randomize}
    else:
        ncols = get_positive_int(payload, "ncols", (r"(?:ncols|列数|田块列数)\s*[:：=]?\s*(\d+)", r"(\d+)\s*(?:列|columns?)"))
        ck_spec = get_string(payload, "ck_spec") or get_string(payload, "ck-spec")
        if not ck_spec:
            list_path = output_dir / f"field-design-interval-{run_id}-ck-table.json"
            list_proc = run_command(
                [
                    rscript,
                    str(SCRIPTS_DIR / "run_interval_contrast_local.R"),
                    "--input",
                    str(input_path),
                    "--list-checks",
                    "true",
                    "--output",
                    str(list_path),
                ]
            )
            if list_proc.returncode != 0:
                return fail("间比法 CK 识别失败：" + error_from_process(list_proc, list_path), error_type="rscript_failed")
            data = read_json(list_path)
            ck_table = data.get("ck_table") if isinstance(data.get("ck_table"), list) else []
            answer = "已识别 Interval/间比法 CK 材料。请补充每个 CK 的起始位置和间隔，格式：ck_no,start_pos,interval；多个 CK 用分号分隔。"
            if ncols is None:
                answer += " 同时请提供 ncols/田块列数。"
            return {
                "ok": True,
                "status": "needs_ck_parameters",
                "answer": answer,
                "design": "interval",
                "missing": [item for item in ("ncols" if ncols is None else "", "ck_spec") if item],
                "ck_table": ck_table,
                "columns": ["ck_no", "ped_id", "set"],
                "rows": ck_table[:10],
            }
        if ncols is None:
            return fail("缺少 Interval 必需参数 ncols/田块列数。", missing=["ncols"], error_type="missing_input")
        randomize = get_bool(payload, "randomize", True)
        command = [
            rscript,
            str(SCRIPTS_DIR / "run_interval_contrast_local.R"),
            "--input",
            str(input_path),
            "--ncols",
            str(ncols),
            "--ck-spec",
            ck_spec,
            "--planter",
            planter,
            "--randomize",
            "true" if randomize else "false",
            "--seed",
            str(seed),
            "--output",
            str(result_json),
        ]
        columns = ["plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type"]
        title = "Field Design Interval Layout"
        render_script = "render_rcbd_interval_layout_html.R"
        extra_parameters = {"ncols": ncols, "ck_spec": ck_spec, "randomize": randomize}

    process = run_command(command)
    if process.returncode != 0:
        return fail("试验设计执行失败：" + error_from_process(process, result_json), error_type="rscript_failed")
    result_payload = read_json(result_json)
    if result_payload.get("ok") is False:
        error = result_payload.get("error") if isinstance(result_payload.get("error"), Mapping) else {}
        return fail("试验设计执行失败：" + str(error.get("message") or "unknown error"), error_type="design_failed")

    csv_process = run_command(
        [
            rscript,
            str(SCRIPTS_DIR / "json_to_fieldbook_csv.R"),
            "--input",
            str(result_json),
            "--design",
            design,
            "--output",
            str(fieldbook_csv),
        ]
    )
    if csv_process.returncode != 0:
        return fail("Fieldbook CSV 导出失败：" + error_from_process(csv_process), error_type="csv_export_failed")

    render_process = run_command(
        [
            rscript,
            str(SCRIPTS_DIR / render_script),
            "--input",
            str(result_json),
            "--output",
            str(layout_html),
            "--title",
            title,
        ]
    )
    if render_process.returncode != 0:
        return fail("HTML 布局预览生成失败：" + error_from_process(render_process), error_type="html_render_failed")

    preview_columns, rows = preview_rows(fieldbook_csv, columns)
    parameters = {"seed": seed, "planter": planter, **extra_parameters}
    if isinstance(result_payload.get("parameters"), Mapping):
        parameters.update(dict(result_payload["parameters"]))
    answer_parts = [
        f"{design.upper()} 试验设计已完成。",
        f"核心参数：" + "，".join(f"{k}={v}" for k, v in parameters.items() if k in {"blocks", "ncols", "requested_ck_ratio", "used_ck_ratio", "auto_upgraded", "actual_check_percent", "seed", "planter", "randomize"}),
        "已生成完整 fieldbook CSV 和 HTML 布局预览。",
    ]
    table = markdown_table(preview_columns, rows)
    if table:
        answer_parts.append("前 10 行种植顺序预览：\n" + table)

    return {
        "ok": True,
        "answer": "\n\n".join(part for part in answer_parts if part),
        "design": design,
        "run_id": run_id,
        "parameters": parameters,
        "columns": preview_columns,
        "rows": rows,
        "row_count_preview": len(rows),
        "output_files": [
            build_output_file(fieldbook_csv, mime_type="text/csv", label="完整 fieldbook CSV", summary="完整种植顺序 fieldbook。"),
            build_output_file(layout_html, mime_type="text/html", label="HTML 布局预览", summary="田间布局可视化预览。"),
        ],
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit(fail("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0
    if not isinstance(payload, dict):
        emit(fail("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0

    design = normalize_design(payload.get("design") or payload.get("design_type"), str(payload.get("query") or ""))
    output_dir = Path(os.environ.get("MAF_SKILL_OUTPUT_DIR") or "outputs").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="field-design-input-") as tmp:
        input_path = resolve_input_file(payload, Path(tmp))
        missing: list[str] = []
        if input_path is None:
            missing.append("material_data")
        if design is None:
            missing.append("design")
        if missing:
            emit(
                fail(
                    "缺少试验设计必需输入：请上传 CSV/JSON 材料清单，并说明设计类型 RCBD、Diagonal 或 Interval。",
                    missing=missing,
                    error_type="missing_input",
                )
            )
            return 0
        try:
            rscript = find_rscript()
            result = run_design_pipeline(payload, input_path, output_dir, rscript, design)
        except subprocess.TimeoutExpired:
            result = fail("试验设计执行超时。", error_type="timeout")
        except Exception as exc:  # noqa: BLE001 - script boundary returns structured JSON for all failures.
            result = fail(f"试验设计执行失败：{exc}", error_type="unhandled_error")
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
