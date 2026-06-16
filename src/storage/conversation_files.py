from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from src.core.models import ConversationFileResource


@dataclass(slots=True, frozen=True)
class StoredConversationFile:
    storage_key: str
    size_bytes: int
    sha256: str


class LocalConversationFileStore:
    """Local managed file store for conversation-scoped uploaded file resources."""

    def __init__(self, root_dir: str | Path) -> None:
        self._root_dir = Path(root_dir).resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    def save_original(self, *, conversation_id: str, upload_id: str, content: bytes) -> StoredConversationFile:
        safe_conversation_id = encode_storage_component(conversation_id)
        safe_upload_id = encode_storage_component(upload_id)
        storage_key = _normalize_storage_key(f"{safe_conversation_id}/{safe_upload_id}/original")
        resource_dir = self._safe_resource_dir(conversation_id=conversation_id, upload_id=upload_id)
        if resource_dir.exists():
            shutil.rmtree(resource_dir)
        resource_dir.mkdir(parents=True, exist_ok=False)
        target = resource_dir / "original"
        try:
            target.write_bytes(bytes(content))
        except Exception:
            shutil.rmtree(resource_dir, ignore_errors=True)
            raise
        return StoredConversationFile(
            storage_key=storage_key,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def read_bytes(self, storage_key: str) -> bytes:
        return self.open_path(storage_key).read_bytes()

    def open_path(self, storage_key: str) -> Path:
        return self._resolve_storage_key(storage_key)

    def copy_to(self, storage_key: str, target_path: str | Path) -> Path:
        source = self.open_path(storage_key)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    def write_description(self, *, conversation_id: str, upload_id: str, description: Mapping[str, Any]) -> str:
        resource_dir = self._safe_resource_dir(conversation_id=conversation_id, upload_id=upload_id)
        resource_dir.mkdir(parents=True, exist_ok=True)
        target = resource_dir / "description.json"
        _atomic_write_text(target, json.dumps(dict(description), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        safe_conversation_id = encode_storage_component(conversation_id)
        safe_upload_id = encode_storage_component(upload_id)
        return _normalize_storage_key(f"{safe_conversation_id}/{safe_upload_id}/description.json")

    def read_description(self, description_ref: str | None) -> dict[str, Any] | None:
        if not description_ref:
            return None
        path = self._resolve_storage_key(description_ref, expected_parts=3, leaf_names={"description.json"})
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None

    def conversation_dir(self, conversation_id: str) -> Path:
        safe_conversation_id = encode_storage_component(conversation_id)
        path = (self._root_dir / safe_conversation_id).resolve()
        if not path.is_relative_to(self._root_dir):
            raise ValueError("Conversation id escapes conversation file store")
        return path

    def delete_conversation_dir(self, conversation_id: str) -> bool:
        path = self.conversation_dir(conversation_id)
        existed = path.exists()
        if existed:
            shutil.rmtree(path)
        return existed

    def delete_resource_dir(self, *, conversation_id: str, upload_id: str) -> bool:
        path = self._safe_resource_dir(conversation_id=conversation_id, upload_id=upload_id)
        existed = path.exists()
        if existed:
            shutil.rmtree(path)
        return existed

    def _safe_resource_dir(self, *, conversation_id: str, upload_id: str) -> Path:
        path = (self.conversation_dir(conversation_id) / encode_storage_component(upload_id)).resolve()
        if not path.is_relative_to(self._root_dir):
            raise ValueError("Conversation file resource escapes conversation file store")
        return path

    def _resolve_storage_key(
        self,
        storage_key: str,
        *,
        expected_parts: int = 3,
        leaf_names: set[str] | None = None,
    ) -> Path:
        storage_key = _normalize_storage_key(storage_key)
        pure = PurePosixPath(storage_key)
        if pure.is_absolute() or len(pure.parts) != expected_parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("Invalid conversation file storage key")
        if leaf_names is not None and pure.parts[-1] not in leaf_names:
            raise ValueError("Invalid conversation file storage leaf")
        path = self._root_dir
        for part in pure.parts:
            if part == pure.parts[-1] and part == "original":
                safe = "original"
            elif leaf_names is not None and part == pure.parts[-1]:
                safe = part
            else:
                safe = validate_storage_component(part)
            path = path / safe
        resolved = path.resolve()
        if not resolved.is_relative_to(self._root_dir):
            raise ValueError("Conversation file storage key escapes store")
        return resolved


class ConversationFileIndexWriter:
    def __init__(self, file_store: LocalConversationFileStore) -> None:
        self._file_store = file_store

    def write_index(self, *, conversation_id: str, resources: Iterable[ConversationFileResource]) -> Path:
        conversation_dir = self._file_store.conversation_dir(conversation_id)
        conversation_dir.mkdir(parents=True, exist_ok=True)
        target = conversation_dir / "index.md"
        _atomic_write_text(target, self.render_index(conversation_id=conversation_id, resources=resources))
        return target

    def render_index(self, *, conversation_id: str, resources: Iterable[ConversationFileResource]) -> str:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        lines: list[str] = [
            "# Conversation Files Index",
            "",
            f"conversation_id: {conversation_id}",
            f"updated_at: {now}",
            "",
        ]
        for resource in sorted(resources, key=lambda item: (item.created_at or datetime.min, item.file_id)):
            lines.extend(self._render_resource(resource))
        return "\n".join(lines).rstrip() + "\n"

    def _render_resource(self, resource: ConversationFileResource) -> list[str]:
        relative_path = (
            "文件本体已物理删除"
            if resource.status == "deleted"
            else _relative_resource_path(resource.storage_key)
        )
        lines = [
            f"## {resource.file_id} — {resource.original_filename}",
            "",
            f"- 原始文件名: {resource.original_filename}",
            f"- 类型: {resource.file_type}",
            f"- MIME: {resource.content_type}",
            f"- 大小: {resource.size_bytes} bytes",
            f"- SHA256: {resource.sha256}",
            f"- 相对路径: {relative_path}",
            f"- 状态: {resource.status}",
            f"- 描述状态: {resource.description_status}",
            "",
            "### 文件描述",
            "",
        ]
        if resource.file_type == "image":
            lines.append("图片文件不自动生成描述。如需识别图片文字，请调用 OCR Skill。")
        elif resource.description_summary:
            lines.append(resource.description_summary)
        elif resource.description_status == "failed":
            lines.append("文件描述生成失败，但文件本体仍可作为 Skill 输入使用。")
        elif resource.description_status == "pending":
            lines.append("文件描述正在生成中，文件本体已可作为 Skill 输入使用。")
        else:
            lines.append("暂无描述。")
        preview = dict(resource.preview or {})
        structure = _structure_lines(preview)
        if structure:
            lines.extend(["", "### 结构", "", *structure])
        lines.append("")
        return lines


def _relative_resource_path(storage_key: str) -> str:
    pure = PurePosixPath(_normalize_storage_key(storage_key))
    if len(pure.parts) >= 3:
        return str(PurePosixPath(*pure.parts[1:]))
    return pure.name


def _structure_lines(preview: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    columns = preview.get("columns")
    if isinstance(columns, list) and columns:
        lines.append("- Columns: " + ", ".join(str(column) for column in columns[:50]))
    row_count = preview.get("row_count")
    if row_count is not None:
        lines.append(f"- Rows: {row_count}")
    sheets = preview.get("excel_sheets")
    if isinstance(sheets, list) and sheets:
        sheet_names = [str(sheet.get("sheet_name")) for sheet in sheets if isinstance(sheet, Mapping) and sheet.get("sheet_name")]
        if sheet_names:
            lines.append("- Sheets: " + ", ".join(sheet_names[:50]))
    return lines


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)
        handle.write(text)
    try:
        tmp_path.replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def mounted_input_filename(*, upload_id: str, filename: str) -> str:
    safe_upload = encode_storage_component(upload_id)
    safe_name = sanitize_download_filename(filename)
    return f"{safe_upload}__{safe_name}"


def encode_storage_component(value: str) -> str:
    text = str(value).strip()
    if not text or text in {".", ".."}:
        raise ValueError("Storage component cannot be empty")
    encoded = quote(text, safe="-_.:")
    if not encoded or encoded in {".", ".."}:
        raise ValueError("Storage component cannot be empty")
    if len(encoded.encode("utf-8")) > 240:
        raise ValueError("Storage component is too long")
    return encoded


def validate_storage_component(value: str) -> str:
    text = str(value).strip()
    if not text or text in {".", ".."}:
        raise ValueError("Invalid storage component")
    if "/" in text or "\\" in text or "\x00" in text:
        raise ValueError("Invalid storage component")
    if quote(text, safe="-_.:%") != text:
        raise ValueError("Invalid storage component")
    return text


def sanitize_download_filename(value: str) -> str:
    text = Path(str(value).replace("\\", "/")).name.strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", "_", text)
    text = re.sub(r"[/\\]+", "_", text)
    text = text.strip(" .")
    return (text or "download.bin")[:200]


def _normalize_storage_key(value: str) -> str:
    pure = PurePosixPath(str(value).replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("Invalid storage key")
    return str(pure)
