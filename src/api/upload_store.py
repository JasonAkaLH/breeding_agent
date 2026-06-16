from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from src.integrations.rust_safety_contract import normalize_storage_key, resource_limit, sha256_hex

from .table_upload_normalizer import (
    TEXT_ENCODINGS,
    detect_table_file_type,
    normalize_selected_spreadsheet_sheet,
    normalize_table_upload,
)
from .upload_errors import UploadValidationError


UploadFileType = Literal["json", "csv", "spreadsheet", "text", "image", "pdf", "vcf"]
SUPPORTED_UPLOAD_DESCRIPTION = "JSON, CSV, Excel, TXT, PNG, JPG/JPEG, PDF, VCF, and VCF.GZ"

SUPPORTED_UPLOAD_EXTENSIONS: dict[str, UploadFileType] = {
    ".json": "json",
    ".csv": "csv",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".vcf": "vcf",
    ".txt": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".pdf": "pdf",
}
SUPPORTED_UPLOAD_CONTENT_TYPES: dict[str, UploadFileType] = {
    "application/json": "json",
    "text/json": "json",
    "text/csv": "csv",
    "application/csv": "csv",
    "application/vnd.ms-excel": "csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "text/plain": "text",
    "image/png": "image",
    "image/jpeg": "image",
    "application/pdf": "pdf",
}
DEFAULT_MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class UploadedFileRecord:
    upload_id: str
    username: str
    conversation_id: str
    filename: str
    content_type: str
    file_type: UploadFileType
    size_bytes: int
    sha256: str
    content_bytes: bytes
    # Execution-oriented normalized text for table uploads.  This is not the
    # original file text; original bytes remain in ``content_bytes`` and own the
    # sha256.
    content_text: str | None
    preview: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    normalized_content_type: str | None = None
    normalized_filename: str | None = None
    requires_sheet_selection: bool = False
    selected_sheet: str | None = None
    status: str = "active"
    description_status: str | None = None

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "preview": dict(self.preview),
            "expires_at": self.expires_at.isoformat(),
        }
        if self.normalized_filename:
            summary["normalized_filename"] = self.normalized_filename
        if self.normalized_content_type:
            summary["normalized_content_type"] = self.normalized_content_type
        if self.requires_sheet_selection:
            summary["requires_sheet_selection"] = True
        if self.selected_sheet:
            summary["selected_sheet"] = self.selected_sheet
        return summary

    def to_skill_artifact(self, *, selected_sheet: str | None = None) -> dict[str, Any]:
        artifact = self.to_summary()
        content_text = self.content_text
        normalized_filename = self.normalized_filename
        normalized_content_type = self.normalized_content_type
        selected_sheet_text = selected_sheet or self.selected_sheet
        if self.file_type == "spreadsheet" and selected_sheet:
            normalized = normalize_selected_spreadsheet_sheet(
                filename=self.filename,
                content_type=self.content_type,
                content=self.content_bytes,
                selected_sheet=selected_sheet,
            )
            content_text = normalized.normalized_content_text
            normalized_filename = normalized.normalized_filename
            normalized_content_type = normalized.normalized_content_type
            selected_sheet_text = normalized.selected_sheet
            artifact["preview"] = dict(normalized.preview)
        if self.file_type == "spreadsheet" and self.requires_sheet_selection and selected_sheet_text is None:
            return artifact
        if content_text is not None:
            artifact["content"] = content_text
            artifact["original_filename"] = self.filename
            artifact["normalized_filename"] = normalized_filename or self.filename
            artifact["filename"] = normalized_filename or self.filename
            artifact["normalized_content_type"] = normalized_content_type
            artifact["content_type"] = normalized_content_type or self.content_type
            if selected_sheet_text:
                artifact["selected_sheet"] = selected_sheet_text
            artifact.pop("requires_sheet_selection", None)
        else:
            artifact["encoding"] = "base64"
            artifact["content_base64"] = base64.b64encode(self.content_bytes).decode("ascii")
        return artifact

    def sheet_selection_payload(self) -> dict[str, Any]:
        preview = self.preview
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "required_upload_ids": [self.upload_id],
            "options_by_upload_id": {
                self.upload_id: [
                    str(sheet.get("sheet_name"))
                    for sheet in preview.get("excel_sheets", [])
                    if isinstance(sheet, dict) and str(sheet.get("sheet_name") or "").strip()
                ]
            },
            "labels_by_upload_id": {self.upload_id: self.filename},
            "details_by_upload_id": {self.upload_id: list(preview.get("excel_sheets", []))},
        }


class InMemoryUploadStore:
    def __init__(
        self,
        *,
        max_file_bytes: int = DEFAULT_MAX_UPLOAD_FILE_BYTES,
        max_preview_bytes: int | None = None,
        ttl_seconds: int = 30 * 60,
        max_files_per_account: int = 20,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.max_preview_bytes = resource_limit("upload_preview_bytes") if max_preview_bytes is None else max_preview_bytes
        self.ttl_seconds = ttl_seconds
        self.max_files_per_account = max_files_per_account
        self._now_fn = now_fn or _utcnow_naive
        self._records: dict[str, UploadedFileRecord] = {}

    def save(
        self,
        *,
        username: str,
        conversation_id: str,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> UploadedFileRecord:
        self.cleanup_expired()
        normalized_filename = _normalize_filename(filename)
        _validate_managed_upload_key(normalized_filename)
        if len(content) > self.max_file_bytes:
            raise UploadValidationError(f"Uploaded file exceeds {self.max_file_bytes} bytes")
        file_type = _detect_file_type(normalized_filename, content_type, content)
        if file_type is None:
            raise UploadValidationError(f"Only {SUPPORTED_UPLOAD_DESCRIPTION} files are supported")
        normalized_content_type = None
        normalized_content_filename = None
        requires_sheet_selection = False
        selected_sheet = None
        if file_type in {"json", "csv", "spreadsheet"}:
            if len(content) > self.max_preview_bytes:
                raise UploadValidationError(f"Uploaded file exceeds preview limit of {self.max_preview_bytes} bytes")
            normalized = normalize_table_upload(
                filename=normalized_filename,
                content_type=content_type,
                content=content,
            )
            file_type = normalized.file_type
            content_text = normalized.normalized_content_text
            preview = normalized.preview
            normalized_content_type = normalized.normalized_content_type
            normalized_content_filename = normalized.normalized_filename
            requires_sheet_selection = normalized.requires_sheet_selection
            selected_sheet = normalized.selected_sheet
        elif file_type == "text":
            if len(content) > self.max_preview_bytes:
                raise UploadValidationError(f"Uploaded file exceeds preview limit of {self.max_preview_bytes} bytes")
            decoded_text, source_encoding = _decode_plain_text_upload(content)
            content_text = decoded_text
            normalized_content_type = "text/plain"
            normalized_content_filename = normalized_filename
            preview = _build_text_preview(decoded_text, source_encoding=source_encoding, size_bytes=len(content))
        else:
            content_text = None
            preview = _build_binary_preview(file_type, len(content))
        now = self._now_fn()
        self._enforce_account_quota(username)
        record = UploadedFileRecord(
            upload_id=f"upl-{uuid4().hex[:12]}",
            username=username,
            conversation_id=conversation_id,
            filename=normalized_filename,
            content_type=(content_type or "application/octet-stream"),
            file_type=file_type,
            size_bytes=len(content),
            sha256=sha256_hex(content),
            content_bytes=bytes(content),
            content_text=content_text,
            preview=preview,
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            normalized_content_type=normalized_content_type,
            normalized_filename=normalized_content_filename,
            requires_sheet_selection=requires_sheet_selection,
            selected_sheet=selected_sheet,
        )
        self._records[record.upload_id] = record
        return record

    def get_for_message(self, *, upload_id: str, username: str, conversation_id: str) -> UploadedFileRecord:
        self.cleanup_expired()
        record = self._records.get(upload_id)
        if record is None:
            raise UploadValidationError(f"Unknown or expired upload_id: {upload_id}")
        if record.username != username or record.conversation_id != conversation_id:
            raise PermissionError(f"Upload does not belong to conversation: {upload_id}")
        return record

    def list_for_conversation(self, *, username: str, conversation_id: str) -> list[UploadedFileRecord]:
        self.cleanup_expired()
        records = [
            record
            for record in self._records.values()
            if record.username == username and record.conversation_id == conversation_id
        ]
        return sorted(records, key=lambda item: (item.created_at, item.upload_id))

    def delete(self, *, upload_id: str, username: str, conversation_id: str) -> bool:
        self.cleanup_expired()
        record = self._records.get(upload_id)
        if record is None:
            return False
        if record.username != username or record.conversation_id != conversation_id:
            raise PermissionError(f"Upload does not belong to conversation: {upload_id}")
        self._records.pop(upload_id, None)
        return True

    def cleanup_expired(self) -> None:
        now = self._now_fn()
        expired = [upload_id for upload_id, record in self._records.items() if record.expires_at <= now]
        for upload_id in expired:
            self._records.pop(upload_id, None)

    def _enforce_account_quota(self, username: str) -> None:
        account_records = [record for record in self._records.values() if record.username == username]
        overflow = len(account_records) - self.max_files_per_account + 1
        if overflow <= 0:
            return
        for record in sorted(account_records, key=lambda item: item.created_at)[:overflow]:
            self._records.pop(record.upload_id, None)


def _normalize_filename(filename: str) -> str:
    raw = str(filename or "upload").replace("\\", "/")
    if "/" in raw:
        raise UploadValidationError("Uploaded filename must not contain path components")
    normalized = Path(raw).name.strip()
    return normalized or "upload"


def _validate_managed_upload_key(filename: str) -> None:
    try:
        normalize_storage_key(f"uploads/{filename}")
    except ValueError as exc:
        raise UploadValidationError("Uploaded filename failed artifact safety validation") from exc


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _detect_file_type(filename: str, content_type: str | None, content: bytes) -> UploadFileType | None:
    suffix_type = _detect_file_type_from_filename(filename)
    if suffix_type:
        if suffix_type in {"json", "csv", "spreadsheet"}:
            return detect_table_file_type(filename, content_type, content)
        return suffix_type
    table_type = detect_table_file_type(filename, content_type, content)
    if table_type:
        return table_type
    base_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    return SUPPORTED_UPLOAD_CONTENT_TYPES.get(base_content_type)


def _detect_file_type_from_filename(filename: str) -> UploadFileType | None:
    lower_name = filename.lower()
    if lower_name.endswith(".vcf.gz"):
        return "vcf"
    return SUPPORTED_UPLOAD_EXTENSIONS.get(Path(filename).suffix.lower())


def _decode_plain_text_upload(content: bytes) -> tuple[str, str]:
    for encoding in TEXT_ENCODINGS:
        try:
            return content.decode(encoding, errors="strict"), encoding
        except UnicodeDecodeError:
            continue
    raise UploadValidationError("Unable to detect text encoding; please save as UTF-8 TXT and upload again")


def _build_text_preview(text: str, *, source_encoding: str, size_bytes: int) -> dict[str, Any]:
    line_count = 0 if text == "" else len(text.splitlines())
    return {
        "row_count": line_count,
        "columns": [],
        "shape": "text",
        "file_type": "text",
        "size_bytes": size_bytes,
        "source_encoding": source_encoding,
        "char_count": len(text),
        "line_count": line_count,
        "normalized_content_type": "text/plain",
    }


def _build_binary_preview(file_type: Literal["image", "pdf", "vcf"], size_bytes: int) -> dict[str, Any]:
    return {"row_count": None, "columns": [], "shape": "binary", "size_bytes": size_bytes, "file_type": file_type}
