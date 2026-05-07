from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4


SUPPORTED_UPLOAD_EXTENSIONS: dict[str, Literal["json", "csv"]] = {".json": "json", ".csv": "csv"}
SUPPORTED_UPLOAD_CONTENT_TYPES: dict[str, Literal["json", "csv"]] = {
    "application/json": "json",
    "text/json": "json",
    "text/csv": "csv",
    "application/csv": "csv",
    "application/vnd.ms-excel": "csv",
}


class UploadValidationError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class UploadedFileRecord:
    upload_id: str
    account_id: str
    conversation_id: str
    filename: str
    content_type: str
    file_type: Literal["json", "csv"]
    size_bytes: int
    sha256: str
    content_text: str
    preview: dict[str, Any]
    created_at: datetime
    expires_at: datetime

    def to_summary(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "preview": dict(self.preview),
            "expires_at": self.expires_at.isoformat(),
        }

    def to_skill_artifact(self) -> dict[str, Any]:
        artifact = self.to_summary()
        artifact["content"] = self.content_text
        return artifact


class InMemoryUploadStore:
    def __init__(
        self,
        *,
        max_file_bytes: int = 2 * 1024 * 1024,
        ttl_seconds: int = 30 * 60,
        max_files_per_account: int = 20,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.ttl_seconds = ttl_seconds
        self.max_files_per_account = max_files_per_account
        self._now_fn = now_fn or datetime.utcnow
        self._records: dict[str, UploadedFileRecord] = {}

    def save(
        self,
        *,
        account_id: str,
        conversation_id: str,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> UploadedFileRecord:
        self.cleanup_expired()
        normalized_filename = _normalize_filename(filename)
        if len(content) > self.max_file_bytes:
            raise UploadValidationError(f"Uploaded file exceeds {self.max_file_bytes} bytes")
        file_type = _detect_file_type(normalized_filename, content_type)
        if file_type is None:
            raise UploadValidationError("Only JSON and CSV files are supported")
        try:
            content_text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadValidationError("Uploaded file must be UTF-8 encoded") from exc
        preview = _build_preview(file_type, content_text)
        now = self._now_fn()
        self._enforce_account_quota(account_id)
        record = UploadedFileRecord(
            upload_id=f"upl-{uuid4().hex[:12]}",
            account_id=account_id,
            conversation_id=conversation_id,
            filename=normalized_filename,
            content_type=(content_type or "application/octet-stream"),
            file_type=file_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content_text=content_text,
            preview=preview,
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
        )
        self._records[record.upload_id] = record
        return record

    def get_for_message(self, *, upload_id: str, account_id: str, conversation_id: str) -> UploadedFileRecord:
        self.cleanup_expired()
        record = self._records.get(upload_id)
        if record is None:
            raise UploadValidationError(f"Unknown or expired upload_id: {upload_id}")
        if record.account_id != account_id or record.conversation_id != conversation_id:
            raise PermissionError(f"Upload does not belong to conversation: {upload_id}")
        return record

    def list_for_conversation(self, *, account_id: str, conversation_id: str) -> list[UploadedFileRecord]:
        self.cleanup_expired()
        records = [
            record
            for record in self._records.values()
            if record.account_id == account_id and record.conversation_id == conversation_id
        ]
        return sorted(records, key=lambda item: (item.created_at, item.upload_id))

    def delete(self, *, upload_id: str, account_id: str, conversation_id: str) -> bool:
        self.cleanup_expired()
        record = self._records.get(upload_id)
        if record is None:
            return False
        if record.account_id != account_id or record.conversation_id != conversation_id:
            raise PermissionError(f"Upload does not belong to conversation: {upload_id}")
        self._records.pop(upload_id, None)
        return True

    def cleanup_expired(self) -> None:
        now = self._now_fn()
        expired = [upload_id for upload_id, record in self._records.items() if record.expires_at <= now]
        for upload_id in expired:
            self._records.pop(upload_id, None)

    def _enforce_account_quota(self, account_id: str) -> None:
        account_records = [record for record in self._records.values() if record.account_id == account_id]
        overflow = len(account_records) - self.max_files_per_account + 1
        if overflow <= 0:
            return
        for record in sorted(account_records, key=lambda item: item.created_at)[:overflow]:
            self._records.pop(record.upload_id, None)


def _normalize_filename(filename: str) -> str:
    normalized = Path(filename or "upload").name.strip()
    return normalized or "upload"


def _detect_file_type(filename: str, content_type: str | None) -> Literal["json", "csv"] | None:
    suffix_type = SUPPORTED_UPLOAD_EXTENSIONS.get(Path(filename).suffix.lower())
    if suffix_type:
        return suffix_type
    base_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    return SUPPORTED_UPLOAD_CONTENT_TYPES.get(base_content_type)


def _build_preview(file_type: Literal["json", "csv"], content_text: str) -> dict[str, Any]:
    if file_type == "json":
        return _build_json_preview(content_text)
    return _build_csv_preview(content_text)


def _build_json_preview(content_text: str) -> dict[str, Any]:
    try:
        value = json.loads(content_text)
    except json.JSONDecodeError as exc:
        raise UploadValidationError(f"Invalid JSON file: {exc}") from exc
    if isinstance(value, list):
        columns = _columns_from_rows(value)
        return {"row_count": len(value), "columns": columns, "shape": "array"}
    if isinstance(value, dict):
        return {"row_count": 1, "columns": [str(key) for key in value.keys()], "shape": "object"}
    raise UploadValidationError("JSON upload must be an object or an array")


def _build_csv_preview(content_text: str) -> dict[str, Any]:
    try:
        reader = csv.DictReader(StringIO(content_text))
        columns = [str(field) for field in (reader.fieldnames or [])]
        if not columns:
            raise UploadValidationError("CSV upload must include a header row")
        row_count = sum(1 for _ in reader)
    except csv.Error as exc:
        raise UploadValidationError(f"Invalid CSV file: {exc}") from exc
    return {"row_count": row_count, "columns": columns, "shape": "table"}


def _columns_from_rows(value: list[Any]) -> list[str]:
    columns: list[str] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            key_text = str(key)
            if key_text not in columns:
                columns.append(key_text)
    return columns
