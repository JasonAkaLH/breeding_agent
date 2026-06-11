from __future__ import annotations

import argparse
import base64
import csv
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Mapping
import xml.etree.ElementTree as ET

from render_adaptability import render_result


DEFAULT_BREEDCORE_URL = "http://breedcore:8000"
DEFAULT_REGION = "zhongwanshu"
SUPPORTED_REGIONS = {
    "1": "zhongwanshu",
    "2": "huanghuaihai",
    "zhongwanshu": "zhongwanshu",
    "huanghuaihai": "huanghuaihai",
    "东北中晚熟": "zhongwanshu",
    "东北中晚熟区": "zhongwanshu",
    "黄淮海": "huanghuaihai",
    "黄淮海区": "huanghuaihai",
}
REGION_LABELS = {
    "zhongwanshu": "东北中晚熟区",
    "huanghuaihai": "黄淮海区",
}
SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
VARIETY_COLUMN_ALIASES = ["品种测试名", "品种", "品种名称", "Variety", "Name", "variety", "name"]
YEAR_COLUMN_ALIASES = ["年份", "Year", "year"]
XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
PACKAGE_REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
FREE_TEXT_KEYS = ("message", "user_text", "query", "text", "input_text", "prompt", "utterance")


def json_response(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def failure(
    answer: str,
    *,
    missing: list[str] | None = None,
    error_type: str = "seed_navi_error",
    details: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "is_error": True,
        "answer": answer,
        "response_text": answer,
        "error": {"type": error_type, "message": answer},
    }
    if details:
        result["error"]["details"] = dict(details)
    if diagnostics:
        result["diagnostics"] = dict(diagnostics)
    if missing:
        result["missing"] = missing
    return result


def safe_run_id(value: Any) -> str:
    text = str(value or "").strip() or "seed_navi"
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text[:80] or "seed_navi"


def running_in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def breedcore_base_url() -> str:
    return (os.getenv("SEED_NAVI_BREEDCORE_URL") or os.getenv("BREEDCORE_URL") or DEFAULT_BREEDCORE_URL).rstrip("/")


def is_loopback_url(value: str) -> bool:
    try:
        host = urlparse(value).hostname
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def breedcore_url_from_payload(payload: Mapping[str, Any]) -> str:
    explicit = str(payload.get("breedcore_url") or "").strip()
    if explicit:
        if running_in_container() and is_loopback_url(explicit):
            return breedcore_base_url()
        return explicit.rstrip("/")
    return breedcore_base_url()


def post_json(base_url: str, endpoint: str, payload: Mapping[str, Any], timeout: int = 300) -> dict[str, Any]:
    url = base_url.rstrip("/") + endpoint
    data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{endpoint} failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot connect to BreedCore at {base_url}: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{endpoint} did not return a JSON object.")
    return result


def get_json(base_url: str, endpoint: str, timeout: int = 300) -> dict[str, Any]:
    url = base_url.rstrip("/") + endpoint
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{endpoint} failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot connect to BreedCore at {base_url}: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{endpoint} did not return a JSON object.")
    return result


def multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def upload_to_breedcore(
    base_url: str,
    input_file: Path,
    file_type: str | None,
    source: str,
    *,
    run_id: str | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    src = input_file.expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    parsed = urlparse(base_url.rstrip("/") + "/uploads")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Invalid BreedCore URL: {base_url}")

    boundary = f"----seed-navi-{uuid.uuid4().hex}"
    fields = {"source": source}
    if file_type:
        fields["file_type"] = file_type
    if run_id:
        fields["run_id"] = run_id

    field_parts = [multipart_field(boundary, key, value) for key, value in fields.items()]
    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{src.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_length = sum(len(part) for part in field_parts) + len(file_header) + src.stat().st_size + len(footer)

    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = connection_cls(parsed.netloc, timeout=timeout)
    target = parsed.path or "/uploads"
    if parsed.query:
        target += f"?{parsed.query}"
    try:
        conn.putrequest("POST", target)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(content_length))
        conn.endheaders()
        for part in field_parts:
            conn.send(part)
        conn.send(file_header)
        with src.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(footer)
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Cannot upload file to BreedCore at {base_url}: {exc}") from exc
    finally:
        conn.close()

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"BreedCore /uploads returned non-JSON response: {body[:500]}") from exc
    if response.status >= 400:
        raise RuntimeError(f"BreedCore /uploads failed with HTTP {response.status}: {json.dumps(result, ensure_ascii=False)}")
    if not isinstance(result, dict) or not result.get("upload_id"):
        raise RuntimeError("BreedCore /uploads did not return upload_id.")
    return result


def save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_column_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def find_column(headers: list[str], aliases: list[str]) -> str | None:
    normalized = {normalize_column_name(header): header for header in headers}
    for alias in aliases:
        found = normalized.get(normalize_column_name(alias))
        if found:
            return found
    return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return [{str(k or ""): str(v or "") for k, v in row.items()} for row in reader]


def xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall("a:si", XLSX_NS):
        parts = [node.text or "" for node in item.findall(".//a:t", XLSX_NS)]
        values.append("".join(parts))
    return values


def xlsx_first_sheet_path(archive: zipfile.ZipFile) -> str:
    names = set(archive.namelist())
    workbook_path = "xl/workbook.xml"
    rels_path = "xl/_rels/workbook.xml.rels"
    if workbook_path not in names or rels_path not in names:
        return "xl/worksheets/sheet1.xml"

    workbook = ET.fromstring(archive.read(workbook_path))
    first_sheet = workbook.find("a:sheets/a:sheet", XLSX_NS)
    rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id") if first_sheet is not None else None
    if not rel_id:
        return "xl/worksheets/sheet1.xml"

    rels = ET.fromstring(archive.read(rels_path))
    for rel in rels.findall("r:Relationship", PACKAGE_REL_NS):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "worksheets/sheet1.xml")
            return "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
    return "xl/worksheets/sheet1.xml"


def xlsx_column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Za-z]", "", cell_ref)
    index = 0
    for char in letters.upper():
        index = index * 26 + ord(char) - ord("A") + 1
    return max(index - 1, 0)


def xlsx_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//a:t", XLSX_NS)).strip()

    value = cell.find("a:v", XLSX_NS)
    raw = value.text if value is not None else ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)].strip()
        except Exception:
            return ""
    return str(raw or "").strip()


def read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = xlsx_shared_strings(archive)
        sheet_path = xlsx_first_sheet_path(archive)
        if sheet_path not in archive.namelist():
            raise RuntimeError("XLSX 文件中没有找到第一个工作表。")
        root = ET.fromstring(archive.read(sheet_path))

    rows: list[list[str]] = []
    for row in root.findall(".//a:sheetData/a:row", XLSX_NS):
        values: dict[int, str] = {}
        for cell in row.findall("a:c", XLSX_NS):
            ref = cell.attrib.get("r", "")
            values[xlsx_column_index(ref)] = xlsx_cell_text(cell, shared_strings)
        if values:
            max_index = max(values)
            rows.append([values.get(index, "") for index in range(max_index + 1)])

    header_row = next((row for row in rows if any(value.strip() for value in row)), None)
    if not header_row:
        return []
    header_index = rows.index(header_row)
    headers = [value.strip() for value in header_row]
    records: list[dict[str, str]] = []
    for row in rows[header_index + 1 :]:
        if not any(value.strip() for value in row):
            continue
        records.append({headers[index]: row[index].strip() if index < len(row) else "" for index in range(len(headers)) if headers[index]})
    return records


def read_trial_rows(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_csv_rows(path)
    if suffix == ".xlsx":
        return read_xlsx_rows(path)

    try:
        import pandas as pd  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("本地品种识别阶段暂不能读取 .xls；请改传 .xlsx/.csv，或直接提供目标品种。") from exc

    frame = pd.read_excel(path)
    return [{str(k or ""): "" if v is None else str(v) for k, v in row.items()} for row in frame.to_dict(orient="records")]


def local_variety_result(input_file: Path, control_variety: str, top_n: int) -> dict[str, Any]:
    rows = read_trial_rows(input_file)
    headers = list(rows[0].keys()) if rows else []
    variety_column = find_column(headers, VARIETY_COLUMN_ALIASES)
    if not variety_column:
        return {
            "ok": False,
            "error": {
                "type": "missing_variety_column",
                "message": "试验表中没有识别到品种列。",
                "supported_aliases": VARIETY_COLUMN_ALIASES,
                "columns": headers,
            },
        }
    year_column = find_column(headers, YEAR_COLUMN_ALIASES)
    counts: dict[str, int] = {}
    years: dict[str, set[int]] = {}
    control = control_variety.strip()

    for row in rows:
        variety = str(row.get(variety_column, "")).strip()
        if not variety or variety in {"nan", "None"} or (control and variety == control):
            continue
        counts[variety] = counts.get(variety, 0) + 1
        if year_column:
            year_match = re.search(r"\d{4}", str(row.get(year_column, "")))
            if year_match:
                years.setdefault(variety, set()).add(int(year_match.group(0)))

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(1, top_n)]
    variety_stats = [
        {"variety": variety, "record_count": count, "years": sorted(years.get(variety, set()))}
        for variety, count in ordered
    ]
    return {
        "ok": True,
        "analysis_type": "local-variety-list",
        "summary": {
            "source": "skill_local_table_parse",
            "total_records": len(rows),
            "total_varieties": len(counts),
            "returned_varieties": len(variety_stats),
            "variety_column": variety_column,
            "year_column": year_column,
            "control_variety": control or None,
        },
        "tables": {"variety_stats": variety_stats},
    }


def artifact_filename(artifact: Mapping[str, Any], default: str = "seed_navi_input.xlsx") -> str:
    raw = str(artifact.get("filename") or default)
    name = Path(raw).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return default
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


def path_from_artifact(artifact: Mapping[str, Any]) -> Path | None:
    for key in ("path", "file_path", "local_path", "tmp_path"):
        raw = artifact.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
    return None


def input_path_from_value(value: Any, work_dir: Path) -> Path | None:
    if isinstance(value, Mapping):
        content = decode_artifact_content(value)
        if content is not None:
            path = work_dir / artifact_filename(value)
            path.write_bytes(content)
            return path
        return path_from_artifact(value)

    if isinstance(value, str) and value.strip():
        candidate = Path(value).expanduser()
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    return None


def resolve_input_file(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    artifacts = payload.get("uploaded_artifacts")
    if isinstance(artifacts, list | tuple):
        for item in artifacts:
            if not isinstance(item, Mapping):
                continue
            path = input_path_from_value(item, work_dir)
            if path is not None:
                return path

    for key in ("trial_file", "file_path", "path", "input_file", "input"):
        path = input_path_from_value(payload.get(key), work_dir)
        if path is not None:
            return path
    return None


def normalize_region(region: Any) -> str | None:
    if region is None:
        return None
    text = str(region).strip()
    return SUPPORTED_REGIONS.get(text)


def payload_text(payload: Mapping[str, Any]) -> str:
    parts = []
    for key in FREE_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def infer_region_from_text(text: str) -> str | None:
    clean = text.strip()
    if not clean:
        return None
    for label in sorted(SUPPORTED_REGIONS, key=len, reverse=True):
        if label in {"1", "2"}:
            continue
        if label and label in clean:
            return SUPPORTED_REGIONS[label]
    return None


def explicit_target_variety(payload: Mapping[str, Any]) -> str:
    return str(payload.get("target_variety") or payload.get("target") or payload.get("variety") or "").strip()


def _candidate_variety(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("variety") or "").strip()


def resolve_target_variety(value: Any, candidates: list[dict[str, Any]]) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""

    for item in candidates:
        variety = _candidate_variety(item)
        if variety and (clean == variety or variety in clean):
            return variety

    pattern = re.search(r"(?:目标品种|待分析品种|分析品种|品种)\s*(?:是|为|:|：)?\s*([^\s，,。；;]+)", clean)
    if pattern:
        return resolve_target_variety(pattern.group(1), candidates) or pattern.group(1).strip()
    return ""


def infer_target_from_text(text: str, candidates: list[dict[str, Any]]) -> str:
    return resolve_target_variety(text, candidates)


def match_candidate_variety(target_variety: str, candidates: list[dict[str, Any]]) -> str:
    clean = str(target_variety or "").strip()
    if not clean:
        return ""
    for item in candidates:
        variety = _candidate_variety(item)
        if variety == clean:
            return variety
    for item in candidates:
        variety = _candidate_variety(item)
        if variety and variety.lower() == clean.lower():
            return variety
    return ""


def target_variety_exists_in_trial(input_file: Path, control_variety: str, target_variety: str) -> bool:
    clean_target = str(target_variety or "").strip()
    if not clean_target:
        return False

    rows = read_trial_rows(input_file)
    headers = list(rows[0].keys()) if rows else []
    variety_column = find_column(headers, VARIETY_COLUMN_ALIASES)
    if not variety_column:
        return False

    control = str(control_variety or "").strip()
    for row in rows:
        variety = str(row.get(variety_column, "")).strip()
        if not variety or variety in {"nan", "None"} or (control and variety == control):
            continue
        if variety == clean_target:
            return True
    return False


def target_not_found_response(target_variety: str, candidates: list[dict[str, Any]], varieties_result: Mapping[str, Any]) -> dict[str, Any]:
    answer = (
        f"没有在当前试验表的候选品种中匹配到目标品种“{target_variety}”。"
        "请提供试验表中的完整品种名称或品种代号后再运行适应性分析。\n\n"
        f"{format_candidate_varieties(candidates)}"
    )
    missing = ["target_variety"]
    return {
        "ok": False,
        "needs_user_input": True,
        **visible_text_fields(answer),
        "missing": missing,
        "error": {
            "type": "target_variety_not_in_local_candidates",
            "message": answer,
            "details": {"target_variety": target_variety},
        },
        "candidate_varieties": candidates,
        "structured_content": {
            "candidate_varieties": candidates,
            "variety_detection": varieties_result.get("summary"),
            "next_required_fields": missing,
        },
        "variety_detection": varieties_result.get("summary"),
    }


def prepare_input(input_path: Path, host_upload_dir: Path, container_upload_dir: str) -> str:
    if str(input_path).replace("\\", "/").startswith("/work/"):
        return str(input_path).replace("\\", "/")

    src = input_path.expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    host_upload_dir.mkdir(parents=True, exist_ok=True)
    dest = host_upload_dir / src.name
    if src != dest.resolve():
        shutil.copy2(src, dest)
    return f"{container_upload_dir.rstrip('/')}/{dest.name}"


def build_output_dirs(run_id: str) -> dict[str, Path]:
    root = Path(os.environ.get("MAF_SKILL_OUTPUT_DIR") or Path("outputs") / "seed-navi" / run_id).resolve()
    dirs = {
        "root": root,
        "api": root / "api",
        "reports": root / "reports",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def output_artifact_path(path: Path, output_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(output_root.resolve())
    except ValueError:
        return f"outputs/{path.name}"
    return str(Path("outputs") / relative).replace(os.sep, "/")


def breeding_runtime_root() -> Path:
    return Path(os.getenv("BREEDING_RUNTIME_ROOT") or "/runtime")


def default_breedcore_upload_dir() -> Path:
    return breeding_runtime_root() / "breedcore" / "uploads"


def default_breedcore_runs_dir() -> Path:
    return breeding_runtime_root() / "breedcore" / "runs"


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def input_ref_for(input_file: Path, payload: Mapping[str, Any], breedcore_url: str) -> tuple[dict[str, Any], str]:
    suffix = input_file.suffix.lstrip(".").lower() or None
    if not (truthy(payload.get("use_legacy_input_path")) or truthy(os.getenv("SEED_NAVI_USE_LEGACY_INPUT_PATH"))):
        upload = upload_to_breedcore(
            breedcore_url,
            input_file,
            suffix,
            "seed-navi",
            run_id=str(payload.get("run_id") or "") or None,
        )
        return {"upload_id": upload["upload_id"], "file_type": upload.get("file_type") or suffix}, str(upload["upload_id"])

    host_upload_dir = Path(
        str(
            payload.get("host_upload_dir")
            or os.getenv("SEED_NAVI_HOST_UPLOAD_DIR")
            or os.getenv("BREEDCORE_HOST_UPLOAD_DIR")
            or default_breedcore_upload_dir()
        )
    )
    container_upload_dir = str(
        payload.get("container_upload_dir")
        or os.getenv("SEED_NAVI_CONTAINER_UPLOAD_DIR")
        or os.getenv("BREEDCORE_CONTAINER_UPLOAD_DIR")
        or "/work/uploads"
    )
    container_input = prepare_input(input_file, host_upload_dir, container_upload_dir)
    return {"path": container_input, "file_type": suffix}, container_input


def breedcore_error(result: Mapping[str, Any]) -> dict[str, Any]:
    raw = result.get("error")
    if isinstance(raw, Mapping):
        return {
            "code": raw.get("code"),
            "message": raw.get("message"),
            "details": raw.get("details") if isinstance(raw.get("details"), Mapping) else {},
        }
    return {}


def summarize_backend_failure(
    *,
    default_answer: str,
    result: Mapping[str, Any],
    input_ref: Mapping[str, Any],
    breedcore_url: str,
    target_variety: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    err = breedcore_error(result)
    code = str(err.get("code") or "").strip()
    message = str(err.get("message") or "").strip()

    if code == "INPUT_FILE_NOT_FOUND" and input_ref.get("path"):
        answer = (
            f"{default_answer} BreedCore 返回 INPUT_FILE_NOT_FOUND："
            f"skill 传入的文件路径是 {input_ref.get('path')}，但 BreedCore 容器内没有这个文件。"
            "这通常表示 skill 写入的宿主机 upload 目录没有挂载到 BreedCore 的 /work/uploads，"
            "或两个运行时没有使用同一个宿主机 runtime 目录。"
        )
    elif code:
        answer = f"{default_answer} BreedCore 返回 {code}"
        if message:
            answer += f"：{message}"
        if code == "TARGET_VARIETY_NOT_FOUND" and target_variety:
            answer += f"。当前传入的目标品种是“{target_variety}”，请确认它与试验表中的品种名称或品种代号完全一致"
        answer += "。"
    else:
        answer = default_answer

    details = {
        "breedcore_error": err,
        "breedcore_status": result.get("status"),
        "job_id": result.get("job_id"),
    }
    diagnostics = {
        "breedcore_url": breedcore_url,
        "input_ref": dict(input_ref),
        "breedcore_response": {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "analysis_type": result.get("analysis_type"),
            "summary": result.get("summary") if isinstance(result.get("summary"), Mapping) else {},
            "error": err,
        },
        "deployment_hint": "Default Seed Navi transfer uses BreedCore /uploads and does not require a shared upload mount.",
    }
    return answer, details, diagnostics


def run_adaptability(
    *,
    breedcore_url: str,
    input_ref: Mapping[str, Any],
    region: str,
    target_variety: str,
    control_variety: str,
    quantile: float,
    distance_limit_km: float,
    year_window: int,
) -> dict[str, Any]:
    return post_json(
        breedcore_url,
        "/jobs/adaptability",
        {
            "input": input_ref,
            "region": region,
            "target_variety": target_variety,
            "control_variety": control_variety,
            "quantile": quantile,
            "distance_limit_km": distance_limit_km,
            "year_window": year_window,
        },
    )


def candidate_varieties(varieties_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = varieties_result.get("tables", {}).get("variety_stats", []) if isinstance(varieties_result.get("tables"), Mapping) else []
    if not isinstance(rows, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, Mapping):
            candidates.append(
                {
                    "variety": item.get("variety"),
                    "record_count": item.get("record_count"),
                    "years": item.get("years", []),
                }
            )
    return candidates


def format_candidate_varieties(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "候选品种：未识别到可展示的候选品种。"

    lines = ["候选品种："]
    for item in candidates:
        variety = str(item.get("variety") or "").strip() or "未命名品种"
        record_count = item.get("record_count")
        years = item.get("years") if isinstance(item.get("years"), list) else []
        parts = []
        if record_count is not None:
            parts.append(f"记录数：{record_count}")
        if years:
            parts.append("年份：" + "、".join(str(year) for year in years))
        suffix = f"（{'；'.join(parts)}）" if parts else ""
        lines.append(f"- {variety}{suffix}")
    return "\n".join(lines)


def visible_text_fields(text: str) -> dict[str, str]:
    return {
        "answer": text,
        "response_text": text,
        "message": text,
        "content": text,
        "display_text": text,
    }


def render_html_report(
    *,
    result_json: Path,
    output_html: Path,
    payload: Mapping[str, Any],
) -> Path:
    host_runs_dir = Path(str(payload.get("host_runs_dir") or os.getenv("BREEDCORE_HOST_RUNS_DIR") or default_breedcore_runs_dir()))
    container_runs_dir = str(payload.get("container_runs_dir") or "/work/runs")
    return render_result(result_json, output_html, host_runs_dir, container_runs_dir)


def download_adaptability_artifacts(
    *,
    breedcore_url: str,
    analysis_result: Mapping[str, Any],
    output_dir: Path,
) -> dict[str, str]:
    job_id = str(analysis_result.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("BreedCore adaptability result did not include job_id.")
    output_dir.mkdir(parents=True, exist_ok=True)
    local_files: dict[str, str] = {}
    for key in ("grid_geojson", "scatter_geojson"):
        artifact = get_json(breedcore_url, f"/jobs/{job_id}/artifacts/{key}")
        path = output_dir / f"{key}.geojson"
        save_json(path, artifact)
        local_files[key] = str(path)
    return local_files


def write_session(
    *,
    run_id: str,
    input_file: Path,
    input_ref: Mapping[str, Any],
    region: str,
    target_variety: str,
    control_variety: str,
    varieties_result: Mapping[str, Any] | None,
    analysis_result: Mapping[str, Any],
    html_file: Path,
    dirs: Mapping[str, Path],
) -> Path:
    session = {
        "session_id": run_id,
        "input": {
            "original_file": str(input_file),
            "input_ref": dict(input_ref),
            "region": region,
            "region_label": REGION_LABELS.get(region, region),
            "target_variety": target_variety,
            "control_variety": control_variety,
        },
        "output": {
            "output_dir": str(dirs["root"]),
            "html_report": str(html_file),
            "api_results": {
                "varieties": str(dirs["api"] / "varieties_result.json") if varieties_result else None,
                "adaptability": str(dirs["api"] / "adaptability_result.json"),
            },
        },
        "jobs": {
            "varieties": {
                "source": varieties_result.get("summary", {}).get("source") if varieties_result else None,
                "job_id": varieties_result.get("job_id") if varieties_result else None,
                "status": varieties_result.get("status") if varieties_result else None,
            },
            "adaptability": {
                "job_id": analysis_result.get("job_id"),
                "status": analysis_result.get("status"),
                "summary": analysis_result.get("summary"),
            },
        },
    }
    session_file = dirs["root"] / ".seed-navi-session.json"
    save_json(session_file, session)
    return session_file


def build_answer(
    *,
    target_variety: str,
    control_variety: str,
    region: str,
    analysis_result: Mapping[str, Any],
    html_file: Path,
) -> str:
    summary = analysis_result.get("summary") if isinstance(analysis_result.get("summary"), Mapping) else {}
    lines = [
        "Seed Navi 品种环境适应性分析已完成。",
        f"目标品种：{target_variety}；对照品种：{control_variety}；当前分析生态区：{REGION_LABELS.get(region, region)}。",
    ]
    metrics = [
        ("匹配试验点", summary.get("matched_points")),
        ("建模有效点", summary.get("model_points")),
        ("适应性网格", summary.get("grid_count")),
        ("实测增产点", summary.get("scatter_count")),
    ]
    metric_text = "；".join(f"{label}：{value}" for label, value in metrics if value is not None)
    if metric_text:
        lines.append(metric_text + "。")
    lines.append(f"HTML 地图报告：{html_file}")
    lines.append("请把适应性指数视为基于当前试验数据和模型假设的环境匹配证据，而不是品种表现保证。")
    return "\n".join(lines)


def run_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="seed-navi-input-") as tmp:
        work_dir = Path(tmp)
        input_file = resolve_input_file(payload, work_dir)
        if input_file is None:
            return failure("请提供玉米品种试验 Excel 或 CSV 文件。", missing=["trial_file"], error_type="missing_input")
        if input_file.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return failure("Seed Navi 目前只支持 .xlsx、.xls 或 .csv 试验文件。", error_type="unsupported_file")

        control_variety = str(payload.get("control_variety") or payload.get("control") or payload.get("ck") or "CK").strip() or "CK"
        text = payload_text(payload)
        top_n = int(payload.get("top_n") or 20)
        varieties_result: Mapping[str, Any] | None = local_variety_result(input_file, control_variety, top_n)
        candidates = candidate_varieties(varieties_result) if varieties_result.get("ok") else []
        raw_target = explicit_target_variety(payload)
        inferred_text_target = infer_target_from_text(text, candidates)
        target_variety = resolve_target_variety(raw_target, candidates) or inferred_text_target
        region = normalize_region(payload.get("region")) or infer_region_from_text(text) or DEFAULT_REGION
        if region != DEFAULT_REGION:
            return failure(
                "Seed Navi 当前仅支持东北中晚熟区品种定位；生态区参数已暂时跳过，其他生态区会在后续更新中恢复。",
                error_type="unsupported_region",
            )

        if not target_variety:
            if not varieties_result.get("ok"):
                error = varieties_result.get("error") if isinstance(varieties_result.get("error"), Mapping) else {}
                return failure(
                    "未能在试验表中识别品种列。请确认表格包含品种测试名/品种/Variety/Name 等列，或直接提供目标品种。",
                    missing=["target_variety"],
                    error_type=str(error.get("type") or "local_variety_list_failed"),
                    diagnostics={"local_variety_result": varieties_result},
                )
            missing = ["target_variety"]
            answer = (
                "已在 skill 层本地识别试验表中的候选品种。当前分析生态区：东北中晚熟区。"
                "请选择一个目标品种。"
                "\n\n"
                f"{format_candidate_varieties(candidates)}"
            )
            return {
                "ok": False,
                "needs_user_input": True,
                **visible_text_fields(answer),
                "missing": missing,
                "candidate_varieties": candidates,
                "structured_content": {
                    "candidate_varieties": candidates,
                    "variety_detection": varieties_result.get("summary"),
                    "next_required_fields": missing,
                },
                "variety_detection": varieties_result.get("summary"),
            }

        if varieties_result.get("ok") and candidates:
            matched_target = match_candidate_variety(target_variety, candidates)
            if matched_target:
                target_variety = matched_target
            elif target_variety_exists_in_trial(input_file, control_variety, target_variety):
                pass
            else:
                return target_not_found_response(target_variety, candidates, varieties_result)

        run_id = safe_run_id(payload.get("run_id") or f"{input_file.stem}-{region}")
        dirs = build_output_dirs(run_id)
        breedcore_url = breedcore_url_from_payload(payload)
        input_ref, _ = input_ref_for(input_file, payload, breedcore_url)

        analysis_result = run_adaptability(
            breedcore_url=breedcore_url,
            input_ref=input_ref,
            region=region,
            target_variety=target_variety,
            control_variety=control_variety,
            quantile=float(payload.get("quantile") or 0.6),
            distance_limit_km=float(payload.get("distance_limit_km") or 50.0),
            year_window=int(payload.get("year_window") or 5),
        )
        save_json(dirs["api"] / "adaptability_result.json", analysis_result)
        if not analysis_result.get("ok"):
            answer, details, diagnostics = summarize_backend_failure(
                default_answer="BreedCore 适应性分析失败。",
                result=analysis_result,
                input_ref=input_ref,
                breedcore_url=breedcore_url,
                target_variety=target_variety,
            )
            return failure(
                answer,
                error_type="adaptability_failed",
                details=details,
                diagnostics=diagnostics,
            )

        local_artifacts = download_adaptability_artifacts(
            breedcore_url=breedcore_url,
            analysis_result=analysis_result,
            output_dir=dirs["root"] / "artifacts",
        )
        analysis_result.setdefault("files", {}).update(local_artifacts)
        save_json(dirs["api"] / "adaptability_result.json", analysis_result)

        html_file = dirs["reports"] / "adaptability_report.html"
        render_html_report(result_json=dirs["api"] / "adaptability_result.json", output_html=html_file, payload=payload)
        analysis_result.setdefault("files", {})["html_report"] = str(html_file)
        save_json(dirs["api"] / "adaptability_result.json", analysis_result)
        write_session(
            run_id=run_id,
            input_file=input_file,
            input_ref=input_ref,
            region=region,
            target_variety=target_variety,
            control_variety=control_variety,
            varieties_result=varieties_result,
            analysis_result=analysis_result,
            html_file=html_file,
            dirs=dirs,
        )
        return {
            "ok": True,
            "answer": build_answer(
                target_variety=target_variety,
                control_variety=control_variety,
                region=region,
                analysis_result=analysis_result,
                html_file=html_file,
            ),
            "run_id": run_id,
            "region": region,
            "target_variety": target_variety,
            "control_variety": control_variety,
            "output_dir": str(dirs["root"]),
            "output_files": [
                {
                    "path": output_artifact_path(html_file, dirs["root"]),
                    "filename": html_file.name,
                    "mime_type": "text/html",
                    "label": "Seed Navi HTML 地图报告",
                },
            ],
        }


def payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "file_path": args.input,
        "region": args.region,
        "control_variety": args.control_variety,
        "target_variety": args.target_variety,
        "top_n": args.top_n,
        "quantile": args.quantile,
        "distance_limit_km": args.distance_limit_km,
        "year_window": args.year_window,
        "host_upload_dir": args.host_upload_dir,
        "container_upload_dir": args.container_upload_dir,
        "host_runs_dir": args.host_runs_dir,
        "container_runs_dir": args.container_runs_dir,
        "run_id": args.run_id,
    }
    if args.breedcore_url:
        payload["breedcore_url"] = args.breedcore_url
    return payload


def main() -> int:
    if not sys.stdin.isatty():
        try:
            payload = json.load(sys.stdin)
        except Exception:
            json_response(failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
            return 0
        if not isinstance(payload, dict):
            json_response(failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
            return 0
        try:
            json_response(run_from_payload(payload))
        except Exception as exc:
            json_response(failure(f"Seed Navi 执行失败：{exc}", error_type="analysis_failed"))
        return 0

    parser = argparse.ArgumentParser(description="Run the Seed Navi variety selection and adaptability workflow.")
    parser.add_argument("--input", required=True, help="Local Excel/CSV file or container path.")
    parser.add_argument("--region", default=None, help="Ecological region: 东北中晚熟区/黄淮海区 or canonical value.")
    parser.add_argument("--control-variety", default="CK")
    parser.add_argument("--target-variety", default=None)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--quantile", type=float, default=0.6)
    parser.add_argument("--distance-limit-km", type=float, default=50.0)
    parser.add_argument("--year-window", type=int, default=5)
    parser.add_argument("--breedcore-url", default=None)
    parser.add_argument("--host-upload-dir", default=str(default_breedcore_upload_dir()))
    parser.add_argument("--container-upload-dir", default="/work/uploads")
    parser.add_argument("--host-runs-dir", default=str(default_breedcore_runs_dir()))
    parser.add_argument("--container-runs-dir", default="/work/runs")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    result = run_from_payload(payload_from_args(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") or result.get("needs_user_input") else 1


if __name__ == "__main__":
    raise SystemExit(main())
