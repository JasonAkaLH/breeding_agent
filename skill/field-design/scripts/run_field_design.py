from __future__ import annotations

import base64
import csv
import html.parser
import json
import os
import re
import struct
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_field_design_layout import render_layout_html

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
ALLOWED_DESIGNS = {"rcbd", "diagonal", "interval"}
SUPPORTED_INPUT_EXTENSIONS = {".csv", ".xls", ".xlsx"}
EXCEL_INPUT_EXTENSIONS = {".xls", ".xlsx"}
DEFAULT_BREEDSTAT2_URL = "http://breedstat2:8000"
LOCAL_BREEDSTAT2_URL = "http://127.0.0.1:8020"
CONTAINER_BREEDSTAT2_URL = "http://breedstat2:8000"


class BreedStat2FieldDesignError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
        raw_body: str = "",
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.raw_body = raw_body

DISPLAY_COLUMN_LABELS = {
    "plots": "小区编号",
    "r": "区组/重复",
    "ped_id": "材料编号",
    "ranges": "行号",
    "pass": "列号",
    "set": "组别",
    "hyb_check": "对照标记",
    "hyb_type": "材料类型",
    "design_check": "设计对照标记",
    "ck_no": "CK编号",
    "start_pos": "起始位置",
    "interval": "间隔数量",
}

DESIGN_DISPLAY_COLUMN_LABELS = {
    "rcbd": {"r": "区组"},
    "interval": {"r": "重复"},
}

CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}
CHINESE_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}


def parse_positive_integer_text(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.search(r"[-−]\s*\d|\d+\.\d+", text):
        return None
    match = re.search(r"\d+", text)
    if match:
        number = int(match.group(0))
        return number if number > 0 else None
    chars = [char for char in text if char in CHINESE_DIGITS or char in CHINESE_UNITS]
    if not chars:
        return None
    total = 0
    current = 0
    for char in chars:
        if char in CHINESE_DIGITS:
            current = CHINESE_DIGITS[char]
            continue
        unit = CHINESE_UNITS[char]
        if current == 0:
            current = 1
        total += current * unit
        current = 0
    total += current
    return total if total > 0 else None


def emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def fail(answer: str, *, missing: list[str] | None = None, error_type: str = "field_design_error", **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "is_error": True,
        "answer": answer,
        "error": {"type": error_type, "message": answer},
    }
    if missing:
        result["missing"] = missing
    result.update(extra)
    return result


def safe_token(value: Any, default: str = "field_design") -> str:
    text = str(value or "").strip() or default
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text[:80] or default


def breedstat2_base_url() -> str:
    return (
        os.environ.get("FIELD_DESIGN_BREEDSTAT2_URL")
        or os.environ.get("BREEDSTAT2_URL")
        or default_breedstat2_url()
    ).rstrip("/")


def default_breedstat2_url() -> str:
    if Path("/.dockerenv").exists() or os.environ.get("KUBERNETES_SERVICE_HOST"):
        return CONTAINER_BREEDSTAT2_URL
    if os.name == "nt":
        return LOCAL_BREEDSTAT2_URL
    return CONTAINER_BREEDSTAT2_URL


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


def resolve_design(payload: Mapping[str, Any]) -> str | None:
    query = str(payload.get("query") or "")
    for key in ("design", "design_type", "_selected_schema_id", "selected_schema_id", "schema", "_schema"):
        design = normalize_design(payload.get(key), "" if key.startswith("_") or "schema" in key else query)
        if design:
            return design
    return normalize_design(None, query)


def get_positive_int(payload: Mapping[str, Any], key: str, query_patterns: Iterable[str] = ()) -> int | None:
    raw = payload.get(key)
    if raw is not None and not isinstance(raw, bool):
        value = parse_positive_integer_text(raw)
        return value if value and value > 0 else None
    query = str(payload.get("query") or "")
    for pattern in query_patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            for group in match.groups():
                value = parse_positive_integer_text(group)
                if value and value > 0:
                    return value
    if query_patterns:
        return parse_positive_integer_text(query)
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


def extract_interval_ck_spec(payload: Mapping[str, Any]) -> str | None:
    explicit = get_string(payload, "ck_spec") or get_string(payload, "ck-spec")
    if explicit:
        return explicit
    return None


def normalize_table_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s_\-（）()：:]+", "", text)


def find_table_column(headers: Iterable[str], aliases: Iterable[str]) -> str | None:
    normalized = {normalize_table_header(header): header for header in headers}
    for alias in aliases:
        header = normalized.get(normalize_table_header(alias))
        if header:
            return header
    return None


def parse_positive_int_cell(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        match = re.fullmatch(r"\s*(\d+)\s*", text)
        if not match:
            return None
        number = float(match.group(1))
    if not number.is_integer() or number <= 0:
        return None
    return int(number)


def read_table_records(path: Path, work_dir: Path) -> list[dict[str, str]]:
    normalized = normalize_input_file(path, work_dir)
    return read_csv_records(normalized)


CK_NO_ALIASES = ("CK编号", "对照编号", "ck_no", "ckno", "ck", "check_no", "check编号")
CK_PED_ID_ALIASES = ("材料编号", "ped_id", "品种编号", "材料名称")
CK_SET_ALIASES = ("组别", "set", "group")
CK_START_ALIASES = ("起始位置", "start_pos", "start", "first_position", "开始位置")
CK_INTERVAL_ALIASES = ("间隔数量", "interval", "间隔", "spacing", "gap")


def ck_parameter_columns(headers: Iterable[str]) -> dict[str, str | None]:
    header_list = list(headers)
    return {
        "ck_no": find_table_column(header_list, CK_NO_ALIASES),
        "ped_id": find_table_column(header_list, CK_PED_ID_ALIASES),
        "set": find_table_column(header_list, CK_SET_ALIASES),
        "start_pos": find_table_column(header_list, CK_START_ALIASES),
        "interval": find_table_column(header_list, CK_INTERVAL_ALIASES),
    }


def is_ck_parameter_records(records: list[Mapping[str, Any]]) -> bool:
    if not records:
        return False
    columns = ck_parameter_columns(records[0].keys())
    return bool(columns["ck_no"] and columns["start_pos"] and columns["interval"])


def is_ck_parameter_file(path: Path, work_dir: Path) -> bool:
    try:
        return is_ck_parameter_records(read_table_records(path, work_dir))
    except Exception:
        return False


def interval_ck_spec_from_records(records: list[Mapping[str, Any]]) -> str | None:
    if not records:
        return None
    columns = ck_parameter_columns(records[0].keys())
    ck_no_column = columns["ck_no"]
    start_column = columns["start_pos"]
    interval_column = columns["interval"]
    if not ck_no_column or not start_column or not interval_column:
        return None
    parts: list[str] = []
    for row in records:
        ck_no = parse_positive_int_cell(row.get(ck_no_column))
        start_pos = parse_positive_int_cell(row.get(start_column))
        interval = parse_positive_int_cell(row.get(interval_column))
        if ck_no is None and start_pos is None and interval is None:
            continue
        if ck_no is None or start_pos is None or interval is None:
            raw = ",".join(str(row.get(column) or "").strip() for column in (ck_no_column, start_column, interval_column))
            parts.append(raw)
        else:
            parts.append(f"{ck_no},{start_pos},{interval}")
    return "; ".join(parts) if parts else None


def interval_ck_spec_from_file(path: Path, work_dir: Path) -> str | None:
    return interval_ck_spec_from_records(read_table_records(path, work_dir))


def resolve_interval_ck_spec(payload: Mapping[str, Any], work_dir: Path) -> str | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    for key in ("ck_spec_file", "ck_params_file", "ck_position_file", "对照位置约束表"):
        raw = payload.get(key) or metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file() and candidate.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS:
                spec = interval_ck_spec_from_file(candidate.resolve(), work_dir)
                if spec:
                    return spec
    rows = payload.get("ck_spec_data") or payload.get("ck_params_data") or metadata.get("ck_spec_data")
    if isinstance(rows, list | tuple) and all(isinstance(row, Mapping) for row in rows):
        spec = interval_ck_spec_from_records([dict(row) for row in rows])
        if spec:
            return spec
    artifacts = payload.get("uploaded_artifacts")
    if isinstance(artifacts, list | tuple):
        for index, item in enumerate(artifacts):
            if not isinstance(item, Mapping):
                continue
            content = decode_artifact_content(item)
            if content is None:
                continue
            path = work_dir / f"ck-params-{index}-{artifact_filename(item, 'ck-params.csv')}"
            path.write_bytes(content)
            if not is_ck_parameter_file(path, work_dir):
                continue
            spec = interval_ck_spec_from_file(path, work_dir)
            if spec:
                return spec
    return None


def interval_ck_spec_numbers(ck_spec: str) -> tuple[set[int], list[int], list[str]]:
    seen: set[int] = set()
    duplicated: list[int] = []
    invalid_items: list[str] = []
    for item in str(ck_spec or "").split(";"):
        text = item.strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split(",")]
        if len(parts) != 3:
            invalid_items.append(text)
            continue
        try:
            ck_no, start_pos, interval = (int(part) for part in parts)
        except ValueError:
            invalid_items.append(text)
            continue
        if ck_no <= 0 or start_pos <= 0 or interval <= 0:
            invalid_items.append(text)
            continue
        if ck_no in seen:
            duplicated.append(ck_no)
        seen.add(ck_no)
    return seen, duplicated, invalid_items


def ck_table_numbers(ck_table: list[Any]) -> set[int]:
    numbers: set[int] = set()
    for index, item in enumerate(ck_table, start=1):
        if isinstance(item, Mapping):
            raw = item.get("ck_no", index)
        else:
            raw = index
        try:
            ck_no = int(str(raw).strip())
        except ValueError:
            ck_no = index
        if ck_no > 0:
            numbers.add(ck_no)
    return numbers


def interval_ck_example(ck_table: list[Any], limit: int = 4) -> str:
    example_parts = []
    for index, item in enumerate(ck_table[:limit], start=1):
        ck_no = item.get("ck_no", index) if isinstance(item, Mapping) else index
        start_pos = 1 if index % 2 == 1 else 5
        example_parts.append(f"{ck_no},{start_pos},9")
    return "; ".join(example_parts) if example_parts else "1,1,9"


def write_interval_ck_template(path: Path, ck_table: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns, rows = interval_ck_template_preview_rows(ck_table, limit=None)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def interval_ck_template_preview_rows(
    ck_table: list[Any],
    *,
    limit: int | None = 30,
) -> tuple[list[str], list[dict[str, Any]]]:
    columns = ["CK编号", "材料编号", "组别", "起始位置", "间隔数量"]
    rows: list[dict[str, Any]] = []
    source = ck_table if limit is None else ck_table[:limit]
    for index, item in enumerate(source, start=1):
        if isinstance(item, Mapping):
            ck_no = item.get("ck_no", index)
            ped_id = item.get("ped_id", "")
            set_name = item.get("set", "")
        else:
            ck_no = index
            ped_id = ""
            set_name = ""
        rows.append(
            {
                "CK编号": ck_no,
                "材料编号": ped_id,
                "组别": set_name,
                "起始位置": "",
                "间隔数量": "",
            }
        )
    return columns, rows


def needs_interval_ck_parameters_response(
    ck_table: list[Any],
    *,
    output_dir: Path,
    run_id: str,
    provided_ck_spec: str | None = None,
    missing_ck_nos: list[int] | None = None,
    invalid_items: list[str] | None = None,
    duplicated_ck_nos: list[int] | None = None,
    unknown_ck_nos: list[int] | None = None,
) -> dict[str, Any]:
    template_csv = output_dir / f"field-design-interval-{run_id}-ck-position-template.csv"
    write_interval_ck_template(template_csv, ck_table)
    detail_parts: list[str] = []
    if provided_ck_spec:
        detail_parts.append("已收到上传的对照位置参数清单，但内容还不完整。")
    if missing_ck_nos:
        detail_parts.append(f"还缺少这些 CK 的对照位置约束：{', '.join(str(item) for item in missing_ck_nos)}。")
    if unknown_ck_nos:
        detail_parts.append(f"以下对照编号不在 CK 清单中：{', '.join(str(item) for item in unknown_ck_nos)}。")
    if duplicated_ck_nos:
        detail_parts.append(f"以下对照编号被重复填写：{', '.join(str(item) for item in duplicated_ck_nos)}。")
    if invalid_items:
        detail_parts.append(f"以下条目格式不正确：{'; '.join(invalid_items)}。")
    detail = ("\n\n" + "\n".join(detail_parts)) if detail_parts else ""
    display_ck_columns, display_ck_rows = interval_ck_template_preview_rows(ck_table)
    table = markdown_table(display_ck_columns, display_ck_rows)
    table_note = ""
    if len(ck_table) > len(display_ck_rows):
        table_note = f"\n\n对话中先展示前 {len(display_ck_rows)} 行，完整清单请使用下方 CSV 模板。"
    answer = (
        f"已识别到 {len(ck_table)} 个 CK。请下载并填写“对照位置参数清单”，然后把补好的 CSV 或 Excel 上传回来。"
        "这份清单用于填写间比法的对照位置约束，不是品种规格。\n\n"
        "清单中已有 CK编号、材料编号、组别；请只补充两列：起始位置、间隔数量。\n"
        "字段含义：起始位置=该 CK 第一次出现的位置；间隔数量=两次插入该 CK 之间间隔的测试材料数量。"
        f"{detail}\n\n"
        f"需要补充的清单：\n{table}{table_note}\n\n"
        "请不要直接手写一串对照参数；对照较多时容易漏填或错位，以上传补好的清单为准。"
    )
    missing_labels = {"ck_spec": "对照位置约束"}
    missing_questions = {
        "ck_spec": (
            "请下载对照位置参数清单，补充“起始位置”和“间隔数量”两列后上传 CSV 或 Excel。"
        )
    }
    return fail(
        answer,
        missing=["ck_spec"],
        error_type="needs_ck_parameters",
        status="needs_ck_parameters",
        design="interval",
        missing_labels=missing_labels,
        missing_questions=missing_questions,
        missing_details={
            "ck_spec": {
                "label": "对照位置约束",
                "description": "间比法设计中的 CK 对照位置约束，不是品种规格。",
                "format": "上传包含 CK编号、起始位置、间隔数量 的 CSV 或 Excel 清单",
                "template": f"outputs/{template_csv.name}",
            }
        },
        output_files=[
            build_output_file(
                template_csv,
                mime_type="text/csv",
                label="对照位置参数清单模板",
                summary="请补充起始位置和间隔数量两列后上传，用于继续间比法设计。",
            )
        ],
        provided_ck_spec=provided_ck_spec,
        missing_ck_nos=missing_ck_nos or [],
        invalid_ck_spec_items=invalid_items or [],
        duplicated_ck_nos=duplicated_ck_nos or [],
        unknown_ck_nos=unknown_ck_nos or [],
        ck_table=ck_table,
        columns=display_ck_columns,
        rows=display_ck_rows,
    )


def artifact_filename(artifact: Mapping[str, Any], default: str = "materials.csv") -> str:
    raw = str(artifact.get("filename") or default)
    name = Path(raw).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return default
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_INPUT_EXTENSIONS:
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
        if is_ck_parameter_file(path, work_dir):
            continue
        return path
    return None


def xml_text(element: ET.Element) -> str:
    return "".join(element.itertext())


def column_ref_to_index(ref: str) -> int | None:
    letters = re.match(r"([A-Za-z]+)", ref or "")
    if not letters:
        return None
    index = 0
    for char in letters.group(1).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def trim_excel_rows(rows: list[list[str]]) -> list[list[str]]:
    while rows and all(cell == "" for cell in rows[-1]):
        rows.pop()
    max_width = 0
    for row in rows:
        while row and row[-1] == "":
            row.pop()
        max_width = max(max_width, len(row))
    return [row + [""] * (max_width - len(row)) for row in rows]


def scalar_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    return str(value)


def read_xlsx_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                if item.tag.rsplit("}", 1)[-1] == "si":
                    shared_strings.append(xml_text(item))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        first_sheet = None
        for element in workbook.iter():
            if element.tag.rsplit("}", 1)[-1] == "sheet":
                first_sheet = element
                break
        if first_sheet is None:
            raise RuntimeError("Excel workbook does not contain a sheet.")

        rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        sheet_target = None
        rels_path = "xl/_rels/workbook.xml.rels"
        if rel_id and rels_path in archive.namelist():
            rels = ET.fromstring(archive.read(rels_path))
            for rel in rels:
                if rel.attrib.get("Id") == rel_id:
                    sheet_target = rel.attrib.get("Target")
                    break
        if not sheet_target:
            sheet_target = "worksheets/sheet1.xml"
        sheet_path = "xl/" + sheet_target.lstrip("/")
        sheet_path = str(Path(sheet_path).as_posix())
        if sheet_path not in archive.namelist():
            sheet_path = "xl/worksheets/sheet1.xml"

        sheet = ET.fromstring(archive.read(sheet_path))
        rows: list[list[str]] = []
        for row_element in sheet.iter():
            if row_element.tag.rsplit("}", 1)[-1] != "row":
                continue
            values: list[str] = []
            next_index = 0
            for cell in row_element:
                if cell.tag.rsplit("}", 1)[-1] != "c":
                    continue
                cell_index = column_ref_to_index(cell.attrib.get("r", "")) if cell.attrib.get("r") else None
                if cell_index is None:
                    cell_index = next_index
                while len(values) <= cell_index:
                    values.append("")
                cell_type = cell.attrib.get("t", "")
                raw_value = ""
                value_node = None
                inline_node = None
                for child in cell:
                    local = child.tag.rsplit("}", 1)[-1]
                    if local == "v":
                        value_node = child
                    elif local == "is":
                        inline_node = child
                if cell_type == "inlineStr" and inline_node is not None:
                    raw_value = xml_text(inline_node)
                elif value_node is not None and value_node.text is not None:
                    raw_value = value_node.text
                    if cell_type == "s":
                        try:
                            raw_value = shared_strings[int(raw_value)]
                        except (ValueError, IndexError):
                            raw_value = ""
                    elif cell_type == "b":
                        raw_value = "TRUE" if raw_value == "1" else "FALSE"
                values[cell_index] = raw_value
                next_index = cell_index + 1
            rows.append(values)
    return trim_excel_rows(rows)


def read_spreadsheetml_rows(content: bytes) -> list[list[str]]:
    root = ET.fromstring(content)
    rows: list[list[str]] = []
    table = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Table":
            table = element
            break
    if table is None:
        raise RuntimeError("Excel XML file does not contain a table.")
    for row_element in table:
        if row_element.tag.rsplit("}", 1)[-1] != "Row":
            continue
        values: list[str] = []
        next_index = 0
        for cell in row_element:
            if cell.tag.rsplit("}", 1)[-1] != "Cell":
                continue
            raw_index = cell.attrib.get("{urn:schemas-microsoft-com:office:spreadsheet}Index")
            cell_index = int(raw_index) - 1 if raw_index and raw_index.isdigit() else next_index
            while len(values) <= cell_index:
                values.append("")
            data = ""
            for child in cell:
                if child.tag.rsplit("}", 1)[-1] == "Data":
                    data = xml_text(child)
                    break
            values[cell_index] = data
            next_index = cell_index + 1
        rows.append(values)
    return trim_excel_rows(rows)


class FirstTableParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_table = False
        self._done = False
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        tag = tag.lower()
        if tag == "table" and not self._in_table:
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._current_row = []
        elif self._in_table and tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._done:
            return
        tag = tag.lower()
        if self._in_table and tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            self._current_row.append("".join(self._current_cell).strip())
            self._current_cell = None
        elif self._in_table and tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None
        elif self._in_table and tag == "table":
            self._in_table = False
            self._done = True

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)


def read_html_table_rows(content: bytes) -> list[list[str]]:
    parser = FirstTableParser()
    parser.feed(content.decode("utf-8-sig", errors="replace"))
    if not parser.rows:
        raise RuntimeError("HTML Excel file does not contain a table.")
    return trim_excel_rows(parser.rows)


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def number_to_excel_text(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return format(value, ".15g")


def decode_biff_string(data: bytes, offset: int = 0, *, short_len: bool = False) -> tuple[str, int]:
    if short_len:
        if offset >= len(data):
            return "", offset
        length = data[offset]
        offset += 1
    else:
        if offset + 2 > len(data):
            return "", offset
        length = u16(data, offset)
        offset += 2
    if offset >= len(data):
        return "", offset
    flags = data[offset]
    offset += 1
    is_utf16 = bool(flags & 0x01)
    rich_runs = u16(data, offset) if flags & 0x08 and offset + 2 <= len(data) else 0
    if flags & 0x08:
        offset += 2
    ext_size = u32(data, offset) if flags & 0x04 and offset + 4 <= len(data) else 0
    if flags & 0x04:
        offset += 4
    byte_len = length * (2 if is_utf16 else 1)
    raw = data[offset : offset + byte_len]
    text = raw.decode("utf-16le" if is_utf16 else "latin1", errors="replace")
    offset += byte_len + rich_runs * 4 + ext_size
    return text, offset


def decode_rk(raw: int) -> float:
    if raw & 0x02:
        value = raw >> 2
        if value & (1 << 29):
            value -= 1 << 30
        number = float(value)
    else:
        packed = (raw & 0xFFFFFFFC) << 32
        number = struct.unpack("<d", struct.pack("<Q", packed))[0]
    if raw & 0x01:
        number /= 100
    return number


class CompoundDocument:
    END_OF_CHAIN = 0xFFFFFFFE
    FREE_SECTOR = 0xFFFFFFFF

    def __init__(self, data: bytes) -> None:
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise RuntimeError("Not an OLE compound document.")
        self.data = data
        self.sector_size = 1 << u16(data, 30)
        self.mini_sector_size = 1 << u16(data, 32)
        self.first_dir_sector = u32(data, 48)
        self.mini_cutoff = u32(data, 56)
        self.first_mini_fat_sector = u32(data, 60)
        self.num_mini_fat_sectors = u32(data, 64)
        self.first_difat_sector = u32(data, 68)
        self.num_difat_sectors = u32(data, 72)
        self.fat = self._read_fat()
        self.entries = self._read_directory()
        self.root = next((entry for entry in self.entries if entry["type"] == 5), None)
        self.mini_fat = self._read_mini_fat()
        self.mini_stream = self._read_regular_stream(self.root["start"], self.root["size"]) if self.root else b""

    def _sector_offset(self, sector: int) -> int:
        return 512 + sector * self.sector_size

    def _sector(self, sector: int) -> bytes:
        offset = self._sector_offset(sector)
        return self.data[offset : offset + self.sector_size]

    def _sector_chain(self, start: int, fat: list[int] | None = None) -> list[int]:
        if start in {self.END_OF_CHAIN, self.FREE_SECTOR}:
            return []
        table = fat if fat is not None else self.fat
        chain: list[int] = []
        seen: set[int] = set()
        sector = start
        while sector not in {self.END_OF_CHAIN, self.FREE_SECTOR} and sector < len(table) and sector not in seen:
            seen.add(sector)
            chain.append(sector)
            sector = table[sector]
        return chain

    def _read_fat(self) -> list[int]:
        fat_sectors = [u32(self.data, 76 + i * 4) for i in range(109)]
        fat_sectors = [sector for sector in fat_sectors if sector != self.FREE_SECTOR]
        next_difat = self.first_difat_sector
        for _ in range(self.num_difat_sectors):
            if next_difat in {self.END_OF_CHAIN, self.FREE_SECTOR}:
                break
            sector_data = self._sector(next_difat)
            entries = self.sector_size // 4 - 1
            fat_sectors.extend(
                value for value in (u32(sector_data, i * 4) for i in range(entries)) if value != self.FREE_SECTOR
            )
            next_difat = u32(sector_data, self.sector_size - 4)
        fat: list[int] = []
        for sector in fat_sectors:
            sector_data = self._sector(sector)
            fat.extend(u32(sector_data, i) for i in range(0, self.sector_size, 4))
        return fat

    def _read_regular_stream(self, start: int, size: int) -> bytes:
        chunks = [self._sector(sector) for sector in self._sector_chain(start)]
        return b"".join(chunks)[:size]

    def _read_mini_fat(self) -> list[int]:
        if self.first_mini_fat_sector in {self.END_OF_CHAIN, self.FREE_SECTOR} or self.num_mini_fat_sectors == 0:
            return []
        data = b"".join(self._sector(sector) for sector in self._sector_chain(self.first_mini_fat_sector))
        return [u32(data, i) for i in range(0, len(data), 4)]

    def _read_mini_stream(self, start: int, size: int) -> bytes:
        chunks: list[bytes] = []
        seen: set[int] = set()
        sector = start
        while sector not in {self.END_OF_CHAIN, self.FREE_SECTOR} and sector < len(self.mini_fat) and sector not in seen:
            seen.add(sector)
            offset = sector * self.mini_sector_size
            chunks.append(self.mini_stream[offset : offset + self.mini_sector_size])
            sector = self.mini_fat[sector]
        return b"".join(chunks)[:size]

    def _read_directory(self) -> list[dict[str, Any]]:
        directory = self._read_regular_stream(self.first_dir_sector, len(self.data))
        entries: list[dict[str, Any]] = []
        for offset in range(0, len(directory), 128):
            entry = directory[offset : offset + 128]
            if len(entry) < 128:
                continue
            name_len = u16(entry, 64)
            raw_name = entry[: max(0, name_len - 2)]
            name = raw_name.decode("utf-16le", errors="replace")
            entry_type = entry[66]
            if entry_type == 0:
                continue
            entries.append({"name": name, "type": entry_type, "start": u32(entry, 116), "size": struct.unpack_from("<Q", entry, 120)[0]})
        return entries

    def stream(self, name: str) -> bytes:
        lowered = name.lower()
        for entry in self.entries:
            if entry["type"] == 2 and entry["name"].lower() == lowered:
                if entry["size"] < self.mini_cutoff and self.mini_fat:
                    return self._read_mini_stream(entry["start"], entry["size"])
                return self._read_regular_stream(entry["start"], entry["size"])
        raise RuntimeError(f"Excel stream not found: {name}")


def parse_sst(data: bytes) -> list[str]:
    if len(data) < 8:
        return []
    offset = 8
    strings: list[str] = []
    while offset < len(data):
        text, next_offset = decode_biff_string(data, offset)
        if next_offset <= offset:
            break
        strings.append(text)
        offset = next_offset
    return strings


def biff_records(data: bytes, start: int = 0) -> Iterable[tuple[int, bytes, int]]:
    offset = start
    while offset + 4 <= len(data):
        sid = u16(data, offset)
        length = u16(data, offset + 2)
        record_start = offset
        offset += 4
        yield sid, data[offset : offset + length], record_start
        offset += length


def read_biff_rows(content: bytes) -> list[list[str]]:
    document = CompoundDocument(content)
    try:
        workbook = document.stream("Workbook")
    except RuntimeError:
        workbook = document.stream("Book")

    sheet_offsets: list[int] = []
    shared_strings: list[str] = []
    records = list(biff_records(workbook))
    index = 0
    while index < len(records):
        sid, payload, _ = records[index]
        if sid == 0x0085 and len(payload) >= 8:
            sheet_offsets.append(u32(payload, 0))
        elif sid == 0x00FC:
            sst_data = bytearray(payload)
            next_index = index + 1
            while next_index < len(records) and records[next_index][0] == 0x003C:
                sst_data.extend(records[next_index][1])
                next_index += 1
            shared_strings = parse_sst(bytes(sst_data))
            index = next_index - 1
        index += 1

    start = sheet_offsets[0] if sheet_offsets else 0
    cells: dict[tuple[int, int], str] = {}
    for sid, payload, record_start in biff_records(workbook, start):
        if record_start > start and sid == 0x0809:
            break
        if sid == 0x000A:
            break
        if sid == 0x0203 and len(payload) >= 14:
            row, col = u16(payload, 0), u16(payload, 2)
            cells[(row, col)] = number_to_excel_text(struct.unpack_from("<d", payload, 6)[0])
        elif sid == 0x00FD and len(payload) >= 10:
            row, col, sst_index = u16(payload, 0), u16(payload, 2), u32(payload, 6)
            cells[(row, col)] = shared_strings[sst_index] if sst_index < len(shared_strings) else ""
        elif sid == 0x0204 and len(payload) >= 8:
            row, col = u16(payload, 0), u16(payload, 2)
            text, _ = decode_biff_string(payload, 6, short_len=False)
            cells[(row, col)] = text
        elif sid == 0x027E and len(payload) >= 10:
            row, col = u16(payload, 0), u16(payload, 2)
            cells[(row, col)] = number_to_excel_text(decode_rk(u32(payload, 6)))
        elif sid == 0x00BD and len(payload) >= 10:
            row, first_col = u16(payload, 0), u16(payload, 2)
            last_col = u16(payload, len(payload) - 2)
            pos = 4
            for col in range(first_col, last_col + 1):
                if pos + 6 > len(payload) - 2:
                    break
                cells[(row, col)] = number_to_excel_text(decode_rk(u32(payload, pos + 2)))
                pos += 6
        elif sid == 0x0006 and len(payload) >= 14:
            row, col = u16(payload, 0), u16(payload, 2)
            marker = payload[12:14]
            if marker != b"\xff\xff":
                cells[(row, col)] = number_to_excel_text(struct.unpack_from("<d", payload, 6)[0])
        elif sid == 0x0205 and len(payload) >= 8:
            row, col = u16(payload, 0), u16(payload, 2)
            cells[(row, col)] = "TRUE" if payload[6] else "FALSE"

    if not cells:
        raise RuntimeError("Excel workbook sheet is empty or unsupported.")
    max_row = max(row for row, _ in cells)
    max_col = max(col for _, col in cells)
    rows = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
    for (row, col), value in cells.items():
        rows[row][col] = value
    return trim_excel_rows(rows)


def read_excel_rows(path: Path) -> list[list[str]]:
    content = path.read_bytes()
    stripped = content.lstrip()
    if content[:2] == b"PK":
        return read_xlsx_rows(path)
    if stripped.startswith((b"<?xml", b"<Workbook")):
        return read_spreadsheetml_rows(content)
    if stripped[:20].lower().startswith((b"<html", b"<!doctype html")):
        return read_html_table_rows(content)
    return read_biff_rows(content)


def convert_excel_to_csv(path: Path, work_dir: Path) -> Path:
    rows = read_excel_rows(path)
    if not rows:
        raise RuntimeError("Excel file does not contain any rows.")
    csv_path = work_dir / f"{path.stem or 'materials'}-excel.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows([[scalar_to_text(cell) for cell in row] for row in rows])
    return csv_path


def normalize_input_file(path: Path, work_dir: Path) -> Path:
    if path.suffix.lower() in EXCEL_INPUT_EXTENSIONS:
        return convert_excel_to_csv(path, work_dir)
    return path


def write_input_from_metadata(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    material_data = (
        metadata.get("material_data")
        or metadata.get("input_data")
        or metadata.get("input_file")
        or metadata.get("file_path")
        or payload.get("material_data")
        or payload.get("input_data")
        or payload.get("pasted_material_data")
        or payload.get("pasted_data")
    )
    if material_data is None:
        return None
    path = work_dir / "materials.csv"
    if isinstance(material_data, str):
        candidate = Path(material_data).expanduser()
        if candidate.exists() and candidate.is_file() and candidate.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS:
            return normalize_input_file(candidate.resolve(), work_dir)
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
        return normalize_input_file(uploaded, work_dir)
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


def read_csv_records(path: Path) -> list[dict[str, str]]:
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


def extract_breedstat2_error_message(status_code: int | None, detail: str) -> tuple[str, str | None]:
    text = (detail or "").strip()
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, Mapping):
            error = data.get("error")
            code = data.get("error_code")
            if isinstance(error, Mapping):
                message = str(error.get("message") or error.get("code") or "").strip()
                code = code or error.get("code")
            else:
                message = str(data.get("message") or data.get("error") or "").strip()
            if message:
                return message, str(code) if code else None
    if text:
        return text[:1200], None
    if status_code is not None:
        return f"breedstat2 returned HTTP {status_code}", None
    return "breedstat2 request failed", None


def post_json(endpoint: str, payload: Mapping[str, Any], timeout: int = 300) -> dict[str, Any]:
    url = f"{breedstat2_base_url()}{endpoint}"
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        message, error_code = extract_breedstat2_error_message(exc.code, detail)
        raise BreedStat2FieldDesignError(
            endpoint=endpoint,
            status_code=exc.code,
            message=message,
            error_code=error_code,
            raw_body=detail[:1200],
        ) from exc
    except urllib.error.URLError as exc:
        raise BreedStat2FieldDesignError(
            endpoint=endpoint,
            message=f"Cannot connect to breedstat2 at {url}: {exc}",
            error_code="field_design_service_unavailable",
        ) from exc
    data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError("breedstat2 response must be a JSON object.")
    if data.get("ok") is False:
        message = api_error_message(data)
        error = data.get("error")
        error_code = str(error.get("code")) if isinstance(error, Mapping) and error.get("code") else None
        raise BreedStat2FieldDesignError(endpoint=endpoint, message=message, error_code=error_code, raw_body=content[:1200])
    return data


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path.name}")
    return data


def api_error_message(result: Mapping[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or error.get("code") or "unknown error")
    return "unknown error"


def classify_field_design_error(message: str) -> dict[str, str]:
    text = str(message or "")
    if "500 - Internal server error" in text or '"error":"500' in text:
        return {
            "error_code": "field_design_unstructured_server_error",
            "explanation": "试验设计服务返回了未结构化的内部错误，未提供具体业务原因；当前无法仅凭返回内容判断是材料表问题还是服务端实现问题。",
            "suggestion": "请维护者查看 breedstat2 服务日志，或确认已部署包含 field-design 结构化错误处理和间比法多 set CK 修复的新镜像。",
        }
    if "Not enough space for constrained checks" in text:
        return {
            "error_code": "field_design_constraint_unsatisfied",
            "explanation": "当前对照数量和布局约束过紧，已经没有足够位置满足对照摆放约束。",
            "suggestion": "可以减少对照数量、增加田块空间，或放宽 check_position_constraint 后重试。",
        }
    if "Hybrid constraint solution not found" in text:
        return {
            "error_code": "field_design_constraint_unsatisfied",
            "explanation": "当前设计无法同时满足测试材料的位置约束。",
            "suggestion": "可以使用 test_position_constraint=false 放宽测试材料跨区组位置约束后重试。",
        }
    if "Check overlap detected" in text:
        position = None
        match = re.search(r"position\s+(\d+)", text)
        if match:
            position = match.group(1)
        ck_names = ""
        ck_match = re.search(r"CK\(s\):\s*(.+)$", text)
        if ck_match:
            ck_names = ck_match.group(1).strip()
        location = f"位置 {position}" if position else "同一位置"
        names_note = f"；发生重叠的 CK 包括：{ck_names}" if ck_names else ""
        return {
            "error_code": "field_design_constraint_unsatisfied",
            "explanation": f"间比法 CK 放置参数发生冲突：多个 CK 被安排到{location}{names_note}。",
            "suggestion": "请调整这些 CK 的起始位置或间隔数量，确保同一播种位置最多只插入一个 CK。",
        }
    if "Missing interval parameters for ck_no" in text:
        missing = text.rsplit(":", 1)[-1].strip() if ":" in text else ""
        suffix = f"缺少的对照编号为：{missing}。" if missing else "仍有 CK 没有提供参数。"
        return {
            "error_code": "field_design_missing_ck_parameters",
            "explanation": f"间比法需要为每个 CK 都提供起始位置和间隔数量，{suffix}",
            "suggestion": "请按“对照编号,起始位置,间隔数量”的格式补齐所有 CK，多个 CK 用分号分隔。",
        }
    if "Unknown ck_no in ck_spec" in text:
        unknown = text.rsplit(":", 1)[-1].strip() if ":" in text else ""
        suffix = f"未知编号为：{unknown}。" if unknown else "存在不在 CK 清单中的编号。"
        return {
            "error_code": "field_design_invalid_ck_parameters",
            "explanation": f"提供的 CK 参数中包含无法识别的对照编号，{suffix}",
            "suggestion": "请只使用系统识别出的 CK 编号，并按“对照编号,起始位置,间隔数量”填写。",
        }
    if "Duplicate ck_no in ck_spec" in text:
        duplicated = text.rsplit(":", 1)[-1].strip() if ":" in text else ""
        suffix = f"重复编号为：{duplicated}。" if duplicated else "有 CK 编号被填写了多次。"
        return {
            "error_code": "field_design_invalid_ck_parameters",
            "explanation": f"同一个 CK 编号不能重复填写，{suffix}",
            "suggestion": "请保留每个 CK 编号的一条参数记录。",
        }
    if "Invalid CK spec item" in text:
        return {
            "error_code": "field_design_invalid_ck_parameters",
            "explanation": "CK 放置参数格式不正确。",
            "suggestion": "请使用“对照编号,起始位置,间隔数量”的格式，例如：1,1,9; 2,5,9。",
        }
    if "CK ped_id must be unique within each set" in text:
        duplicated = text.rsplit("Duplicated CK(s):", 1)[-1].strip() if "Duplicated CK(s):" in text else ""
        suffix = f"重复项为：{duplicated}。" if duplicated else "同一组别内存在重复 CK。"
        return {
            "error_code": "field_design_invalid_input",
            "explanation": f"间比法要求同一个组别 set 内 CK 材料编号不能重复，{suffix}",
            "suggestion": "请检查材料表，保留每个 set 内唯一的 CK 材料编号。",
        }
    if "At least one row must have hyb_check = 2" in text:
        return {
            "error_code": "field_design_missing_diagonal_check",
            "explanation": "对角线增广设计至少需要一个对角线对照材料。",
            "suggestion": "请在材料表中至少把一个对照材料标记为 hyb_check=2。",
        }
    return {
        "error_code": "field_design_api_failed",
        "explanation": "breedstat2 未能完成当前试验设计。",
        "suggestion": "请检查材料表、设计类型和参数是否满足该设计的输入要求。",
    }


def field_design_error_response(exc: BreedStat2FieldDesignError, *, design: str, retried: bool = False) -> dict[str, Any]:
    classified = classify_field_design_error(exc.message)
    retry_note = " 已自动尝试放宽测试材料位置约束，但仍未找到可行设计。" if retried else ""
    if exc.error_code == "field_design_service_unavailable":
        title = "试验设计服务暂时无法连接。"
    elif classified["error_code"] == "field_design_api_failed":
        title = "试验设计执行失败。"
    else:
        title = "试验设计约束无法满足。"
    answer = (
        f"{title}\n\n"
        f"原因：{classified['explanation']}{retry_note}\n\n"
        f"建议：{classified['suggestion']}\n\n"
        f"breedstat2 返回信息：{exc.message}"
    )
    return fail(
        answer,
        error_type=classified["error_code"],
        design=design,
        endpoint=exc.endpoint,
        status_code=exc.status_code,
        suggestion=classified["suggestion"],
        original_error=exc.message,
    )


def format_success_parameters(parameters: Mapping[str, Any]) -> str:
    labels = {
        "blocks": "区组数",
        "ncols": "田块列数",
        "requested_ck_ratio": "请求对照密度等级",
        "used_ck_ratio": "实际对照密度等级",
        "auto_upgraded": "是否自动升级密度等级",
        "actual_check_percent": "实际对照比例",
        "seed": "随机种子",
        "randomize": "是否随机排列",
    }
    planter_labels = {
        "serpentine": "蛇形排列",
        "cartesian": "顺序排列",
    }
    ordered_keys = (
        "blocks",
        "ncols",
        "requested_ck_ratio",
        "used_ck_ratio",
        "auto_upgraded",
        "actual_check_percent",
        "seed",
        "planter",
        "randomize",
    )
    parts: list[str] = []
    for key in ordered_keys:
        if key not in parameters:
            continue
        value = parameters[key]
        if key == "planter":
            value = planter_labels.get(str(value), str(value))
            parts.append(f"排列方式：{value}")
        else:
            parts.append(f"{labels.get(key, key)}：{value}")
    return "，".join(parts)


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


def display_column_label(column: str, design: str | None = None) -> str:
    if design and column in DESIGN_DISPLAY_COLUMN_LABELS.get(design, {}):
        return DESIGN_DISPLAY_COLUMN_LABELS[design][column]
    return DISPLAY_COLUMN_LABELS.get(column, column)


def localize_table_columns(
    columns: list[str],
    rows: list[Mapping[str, Any]],
    *,
    design: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    display_columns = [display_column_label(column, design) for column in columns]
    display_rows: list[dict[str, Any]] = []
    for row in rows:
        display_rows.append(
            {
                display_column: row.get(source_column, "")
                for source_column, display_column in zip(columns, display_columns, strict=True)
            }
        )
    return display_columns, display_rows


def build_output_file(path: Path, *, mime_type: str, label: str, summary: str) -> dict[str, str]:
    return {
        "path": f"outputs/{path.name}",
        "filename": path.name,
        "mime_type": mime_type,
        "label": label,
        "summary": summary,
    }


def coerce_output_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        rows: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                rows.append({str(key): cell for key, cell in item.items()})
        return rows
    return []


def extract_design_rows(result_payload: Mapping[str, Any], design: str) -> list[dict[str, Any]]:
    rows = coerce_output_rows(result_payload.get("out_design"))
    if rows:
        return rows
    if design == "rcbd" and isinstance(result_payload.get("results"), list) and result_payload["results"]:
        first = result_payload["results"][0]
        if isinstance(first, Mapping):
            return coerce_output_rows(first.get("out_design"))
    return []


def write_fieldbook_csv(path: Path, rows: list[Mapping[str, Any]], preferred_columns: list[str]) -> None:
    columns = [column for column in preferred_columns if any(column in row for row in rows)]
    for row in rows:
        for key in row:
            text_key = str(key)
            if text_key not in columns:
                columns.append(text_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def run_breedstat2_design(endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return post_json(endpoint, payload)


def run_design_pipeline(payload: Mapping[str, Any], input_path: Path, output_dir: Path, design: str) -> dict[str, Any]:
    run_id = safe_token(payload.get("run_id") or payload.get("run-id") or design, design)
    seed = get_positive_int(payload, "seed", (r"(?:seed|随机种子)\s*[:：=]?\s*(\d+)",)) or 20260512
    planter = get_string(payload, "planter", "serpentine") or "serpentine"
    if planter not in {"serpentine", "cartesian"}:
        return fail("planter 必须是 serpentine 或 cartesian。", error_type="invalid_parameter")

    result_json = output_dir / f"field-design-{design}-{run_id}-result.json"
    fieldbook_csv = output_dir / f"field-design-{design}-{run_id}-fieldbook.csv"
    layout_html = output_dir / f"field-design-{design}-{run_id}-layout.html"
    records = read_csv_records(input_path)
    if not records:
        return fail("材料清单为空或无法读取表头。", error_type="empty_materials")

    if design == "rcbd":
        relaxed_test_position_constraint = False
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
        api_payload = {
            "data": records,
            "blocks": blocks,
            "planter": planter,
            "seed": seed,
            "site_num": get_positive_int(payload, "site_num") or 1,
            "site_random": get_bool(payload, "site_random", False),
            "check_position_constraint": get_bool(payload, "check_position_constraint", True),
            "test_position_constraint": get_bool(payload, "test_position_constraint", True),
        }
        try:
            result_payload = run_breedstat2_design("/field-design/rcbd", api_payload)
        except BreedStat2FieldDesignError as exc:
            if (
                "Hybrid constraint solution not found" in exc.message
                and api_payload["test_position_constraint"] is True
            ):
                retry_payload = dict(api_payload)
                retry_payload["test_position_constraint"] = False
                try:
                    result_payload = run_breedstat2_design("/field-design/rcbd", retry_payload)
                except BreedStat2FieldDesignError as retry_exc:
                    return field_design_error_response(retry_exc, design=design, retried=True)
                relaxed_test_position_constraint = True
                api_payload = retry_payload
            else:
                return field_design_error_response(exc, design=design)
        columns = ["plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type"]
        title = "Field Design RCBD Layout"
        extra_parameters = {
            "blocks": blocks,
            "test_position_constraint": api_payload["test_position_constraint"],
            "check_position_constraint": api_payload["check_position_constraint"],
        }
        if relaxed_test_position_constraint:
            extra_parameters["auto_relaxed_test_position_constraint"] = True
    elif design == "diagonal":
        ncols = get_positive_int(payload, "ncols", (r"(?:ncols|列数|田块列数)\s*[:：=]?\s*(\d+)", r"(\d+)\s*(?:列|columns?)"))
        if ncols is None:
            return fail("缺少 Diagonal 必需参数 ncols/田块列数。", missing=["ncols"], error_type="missing_input")
        ck_ratio = (get_string(payload, "ck_ratio") or get_string(payload, "ck-ratio") or "A").upper()
        randomize = get_bool(payload, "randomize", True)
        api_payload = {
            "data": records,
            "ncols": ncols,
            "nrows": get_positive_int(payload, "nrows"),
            "ck_ratio": ck_ratio,
            "planter": planter,
            "randomize": randomize,
            "seed": seed,
        }
        try:
            result_payload = run_breedstat2_design("/field-design/diagonal", api_payload)
        except BreedStat2FieldDesignError as exc:
            return field_design_error_response(exc, design=design)
        columns = ["plots", "ped_id", "hyb_type", "ranges", "pass", "set", "design_check"]
        title = "Field Design Diagonal Layout"
        extra_parameters = {"ncols": ncols, "requested_ck_ratio": ck_ratio, "randomize": randomize}
    else:
        ncols = get_positive_int(payload, "ncols", (r"(?:ncols|列数|田块列数)\s*[:：=]?\s*(\d+)", r"(\d+)\s*(?:列|columns?)"))
        if ncols is None:
            return fail(
                "已收到材料清单，设计类型为间比法设计。还需要你补充田块列数 ncols，例如：列数10。",
                missing=["ncols"],
                error_type="missing_input",
                design="interval",
            )
        ck_spec = resolve_interval_ck_spec(payload, output_dir)
        try:
            data = run_breedstat2_design("/field-design/interval/checks", {"data": records})
        except BreedStat2FieldDesignError as exc:
            return field_design_error_response(exc, design=design)
        ck_table = data.get("ck_table") if isinstance(data.get("ck_table"), list) else []
        if not ck_spec:
            return needs_interval_ck_parameters_response(ck_table, output_dir=output_dir, run_id=run_id)
        expected_ck_nos = ck_table_numbers(ck_table)
        provided_ck_nos, duplicated_ck_nos, invalid_items = interval_ck_spec_numbers(ck_spec)
        missing_ck_nos = sorted(expected_ck_nos - provided_ck_nos)
        unknown_ck_nos = sorted(provided_ck_nos - expected_ck_nos)
        if missing_ck_nos or unknown_ck_nos or duplicated_ck_nos or invalid_items:
            return needs_interval_ck_parameters_response(
                ck_table,
                output_dir=output_dir,
                run_id=run_id,
                provided_ck_spec=ck_spec,
                missing_ck_nos=missing_ck_nos,
                invalid_items=invalid_items,
                duplicated_ck_nos=duplicated_ck_nos,
                unknown_ck_nos=unknown_ck_nos,
            )
        if ncols is None:
            return fail("缺少 Interval 必需参数 ncols/田块列数。", missing=["ncols"], error_type="missing_input")
        randomize = get_bool(payload, "randomize", True)
        api_payload = {
            "data": records,
            "ck_spec": ck_spec,
            "ncols": ncols,
            "nrows": get_positive_int(payload, "nrows"),
            "planter": planter,
            "randomize": randomize,
            "seed": seed,
        }
        try:
            result_payload = run_breedstat2_design("/field-design/interval", api_payload)
        except BreedStat2FieldDesignError as exc:
            return field_design_error_response(exc, design=design)
        columns = ["plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type"]
        title = "Field Design Interval Layout"
        extra_parameters = {"ncols": ncols, "ck_spec": ck_spec, "randomize": randomize}

    if result_payload.get("ok") is False:
        return fail("试验设计执行失败：" + api_error_message(result_payload), error_type="api_failed")
    result_json.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    design_rows = extract_design_rows(result_payload, design)
    if not design_rows:
        return fail("breedstat2 未返回可导出的 out_design。", error_type="missing_design_rows")
    write_fieldbook_csv(fieldbook_csv, design_rows, columns)

    preview_columns, rows = preview_rows(fieldbook_csv, columns)
    display_columns, display_rows = localize_table_columns(preview_columns, rows, design=design)
    parameters = {"seed": seed, "planter": planter, **extra_parameters}
    if isinstance(result_payload.get("parameters"), Mapping):
        parameters.update(dict(result_payload["parameters"]))
    quality_control = result_payload.get("quality_control") if isinstance(result_payload.get("quality_control"), Mapping) else {}
    render_layout_html(
        layout_html,
        title=title,
        rows=design_rows,
        columns=columns,
        design=design,
        parameters=parameters,
        quality_control=quality_control,
    )
    answer_parts = [
        f"{design.upper()} 试验设计已完成。",
        f"核心参数：{format_success_parameters(parameters)}",
        "已生成完整 fieldbook CSV 和 HTML 布局预览。",
    ]
    if parameters.get("auto_relaxed_test_position_constraint"):
        answer_parts.append(
            "提示：首次设计时位置约束过紧，系统已自动放宽测试材料的位置约束后完成设计。"
            "这表示测试材料在不同区组中的物理位置不再强制完全错开；对照材料的位置约束仍保持开启，"
            "材料数量、区组数和随机种子没有改变。"
        )
    table = markdown_table(display_columns, display_rows)
    if table:
        answer_parts.append("前 10 行种植顺序预览：\n" + table)

    return {
        "ok": True,
        "answer": "\n\n".join(part for part in answer_parts if part),
        "design": design,
        "run_id": run_id,
        "parameters": parameters,
        "columns": display_columns,
        "rows": display_rows,
        "row_count_preview": len(display_rows),
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

    design = resolve_design(payload)
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
                    "缺少试验设计必需输入：请上传 CSV/Excel 材料清单，并说明设计类型 RCBD、Diagonal 或 Interval。",
                    missing=missing,
                    error_type="missing_input",
                )
            )
            return 0
        try:
            result = run_design_pipeline(payload, input_path, output_dir, design)
        except Exception as exc:  # noqa: BLE001 - script boundary returns structured JSON for all failures.
            result = fail(f"试验设计执行失败：{exc}", error_type="unhandled_error")
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
