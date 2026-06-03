from __future__ import annotations

import base64
import csv
import html.parser
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree as ET

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
ALLOWED_DESIGNS = {"rcbd", "diagonal", "interval"}
SUPPORTED_INPUT_EXTENSIONS = {".csv", ".xls", ".xlsx"}
EXCEL_INPUT_EXTENSIONS = {".xls", ".xlsx"}

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
CHINESE_INTEGER_TOKEN = "零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟"
INTEGER_PHRASE_TOKEN_RE = rf"(?:\d+|[{CHINESE_INTEGER_TOKEN}]+)"
INTEGER_QUERY_TERMS = {
    "blocks": ("blocks?", "区组数", "区组", "重复数", "重复", "reps?", "replications?"),
    "ncols": ("ncols", "列数", "田块列数", "列", "columns?"),
    "seed": ("seed", "随机种子"),
}


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


def parse_chinese_positive_int_token(token: str) -> int | None:
    digit_map = {
        "零": 0,
        "〇": 0,
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
        "壹": 1,
        "贰": 2,
        "叁": 3,
        "肆": 4,
        "伍": 5,
        "陆": 6,
        "柒": 7,
        "捌": 8,
        "玖": 9,
    }
    unit_map = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    if not token or any(char not in digit_map and char not in unit_map and char not in {"万", "萬"} for char in token):
        return None
    if not any(char in unit_map or char in {"万", "萬"} for char in token):
        parsed = int("".join(str(digit_map[char]) for char in token))
        return parsed if parsed > 0 else None

    total = 0
    section = 0
    number = 0
    for char in token:
        if char in digit_map:
            number = digit_map[char]
        elif char in unit_map:
            if number == 0:
                number = 1
            section += number * unit_map[char]
            number = 0
        elif char in {"万", "萬"}:
            section += number
            if section == 0:
                section = 1
            total += section * 10000
            section = 0
            number = 0
    parsed = total + section + number
    return parsed if parsed > 0 else None


def parse_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if value.is_integer() and value > 0:
            return int(value)
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\+?\d+", text):
        parsed = int(text.lstrip("+") or "0")
        return parsed if parsed > 0 else None
    if re.search(r"[-−]\s*\d", text) or re.search(r"\d+\s*\.\s*\d+", text):
        return None
    arabic = re.search(r"(?<![\d.])(\d+)(?![\d.])", text)
    if arabic is not None:
        parsed = int(arabic.group(1))
        return parsed if parsed > 0 else None
    chinese = re.search(rf"[{CHINESE_INTEGER_TOKEN}]+", text)
    if chinese is None:
        return None
    return parse_chinese_positive_int_token(chinese.group(0))


def match_positive_int_near_query_terms(key: str, query: str) -> int | None:
    terms = INTEGER_QUERY_TERMS.get(key, (re.escape(key),))
    escaped = "|".join(terms)
    patterns = (
        rf"(?:{escaped})\s*(?::|：|=|是|为|就是)?\s*({INTEGER_PHRASE_TOKEN_RE})\s*(?:个|次|遍|轮)?",
        rf"({INTEGER_PHRASE_TOKEN_RE})\s*(?:个|次|遍|轮)?\s*(?:{escaped})",
    )
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            for group in match.groups():
                parsed = parse_positive_int(group)
                if parsed is not None:
                    return parsed
    return None


def get_positive_int(payload: Mapping[str, Any], key: str, query_patterns: Iterable[str] = ()) -> int | None:
    raw = payload.get(key)
    if raw is not None:
        return parse_positive_int(raw)
    query = str(payload.get("query") or "")
    for pattern in query_patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            for group in match.groups():
                parsed = parse_positive_int(group)
                if parsed is not None:
                    return parsed
    return match_positive_int_near_query_terms(key, query)


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
            display_ck_columns, display_ck_rows = localize_table_columns(["ck_no", "ped_id", "set"], ck_table[:10])
            return fail(
                answer,
                missing=[item for item in ("ncols" if ncols is None else "", "ck_spec") if item],
                error_type="missing_input",
                status="needs_ck_parameters",
                design="interval",
                ck_table=ck_table,
                columns=display_ck_columns,
                rows=display_ck_rows,
            )
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
    display_columns, display_rows = localize_table_columns(preview_columns, rows, design=design)
    parameters = {"seed": seed, "planter": planter, **extra_parameters}
    if isinstance(result_payload.get("parameters"), Mapping):
        parameters.update(dict(result_payload["parameters"]))
    answer_parts = [
        f"{design.upper()} 试验设计已完成。",
        f"核心参数：" + "，".join(f"{k}={v}" for k, v in parameters.items() if k in {"blocks", "ncols", "requested_ck_ratio", "used_ck_ratio", "auto_upgraded", "actual_check_percent", "seed", "planter", "randomize"}),
        "已生成完整 fieldbook CSV 和 HTML 布局预览。",
    ]
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
                    "缺少试验设计必需输入：请上传 CSV/Excel 材料清单，并说明设计类型 RCBD、Diagonal 或 Interval。",
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
