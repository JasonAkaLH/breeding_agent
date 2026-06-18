from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from src.core.models import ConversationFileResource, TaskInputAttachment


@dataclass(slots=True, frozen=True)
class FileRequirementProfile:
    source: str = "query"
    required: bool = False
    allow_multiple: bool = False
    expected_content: tuple[str, ...] = ()
    supported_file_types: tuple[str, ...] = ()
    helpful_columns: tuple[str, ...] = ()
    disambiguation_hint: str = ""
    user_file_reference: str = ""
    context_notes: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None, *, source: str = "schema") -> "FileRequirementProfile":
        raw = dict(value or {})
        return cls(
            source=str(raw.get("source") or source).strip() or source,
            required=bool(raw.get("required") or raw.get("requires_file") or raw.get("required_file")),
            allow_multiple=bool(raw.get("allow_multiple") or raw.get("default_allow_multiple")),
            expected_content=_string_tuple(raw.get("expected_content") or raw.get("description") or ()),
            supported_file_types=_string_tuple(
                raw.get("supported_file_types")
                or raw.get("accepted_file_types")
                or raw.get("accepted_types")
                or raw.get("file_types")
                or ()
            ),
            helpful_columns=_string_tuple(raw.get("helpful_columns") or ()),
            disambiguation_hint=str(raw.get("disambiguation_hint") or "").strip(),
            user_file_reference=str(raw.get("user_file_reference") or raw.get("reference") or "").strip(),
            context_notes=_string_tuple(raw.get("context_notes") or raw.get("notes") or ()),
        )


@dataclass(slots=True, frozen=True)
class RecentFileUsage:
    upload_id: str
    usage_count: int = 0
    last_used_task_id: str = ""
    last_used_at: datetime | None = None
    last_source_kind: str = ""
    selected_sheet: str | None = None


@dataclass(slots=True, frozen=True)
class ConversationFileCandidate:
    upload_id: str
    filename: str
    content_type: str = ""
    file_type: str = ""
    size_bytes: int = 0
    sha256_short: str = ""
    description_summary: str = ""
    preview: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    selected_sheet: str | None = None
    requires_sheet_selection: bool = False
    recent_usage: RecentFileUsage | None = None

    def to_prompt_safe_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "sha256_short": self.sha256_short,
            "description_summary": "",
            "preview": _sanitize_preview(self.preview),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "selected_sheet": self.selected_sheet,
            "requires_sheet_selection": self.requires_sheet_selection,
        }
        if self.recent_usage is not None:
            payload["recent_usage"] = {
                "usage_count": self.recent_usage.usage_count,
                "last_used_task_id": self.recent_usage.last_used_task_id,
                "last_used_at": self.recent_usage.last_used_at.isoformat() if self.recent_usage.last_used_at else None,
                "last_source_kind": self.recent_usage.last_source_kind,
                "selected_sheet": self.recent_usage.selected_sheet,
            }
        return payload


@dataclass(slots=True, frozen=True)
class FileSelectionDecision:
    decision: str
    upload_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    reason_code: str = ""
    question: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def selected_upload_id(self) -> str | None:
        return self.upload_ids[0] if len(self.upload_ids) == 1 else None


_FILE_REFERENCE_RE = re.compile(
    r"(上传|文件|表格|数据|csv|xlsx?|excel|材料|刚才|之前|继续用|这个|那个|行数|列名|sheet|upload[_ -]?id)",
    re.IGNORECASE,
)
_NO_FILE_RE = re.compile(r"(不用|不需要|不要).{0,6}(文件|数据|表|上传)")
_ROW_COUNT_RE = re.compile(r"(\d+)\s*(?:行|rows?)", re.IGNORECASE)
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*(?:个|份|张|个文件|份文件)?")
_SECRET_KEY_RE = re.compile(r"(secret|token|password|storage_key|mount_path|content_base64|content|path)", re.IGNORECASE)


def query_declines_files(text: str) -> bool:
    return bool(_NO_FILE_RE.search(text or ""))


def query_mentions_file(text: str) -> bool:
    return bool(_FILE_REFERENCE_RE.search(text or "")) and not query_declines_files(text)


class FileSelectionTriggerDetector:
    def should_trigger(
        self,
        *,
        text: str,
        profile: FileRequirementProfile,
        has_explicit_uploads: bool,
        active_file_count: int,
    ) -> tuple[bool, str]:
        if has_explicit_uploads or query_declines_files(text):
            return False, "explicit_or_declined"
        if profile.required:
            if active_file_count <= 0:
                return True, "no_files_in_conversation"
            return True, "required_profile"
        if active_file_count <= 0:
            return False, "no_active_files"
        if query_mentions_file(text) or profile.user_file_reference:
            return True, "query_reference"
        return False, "ordinary_query"


def build_recent_usage(attachments: Sequence[TaskInputAttachment]) -> dict[str, RecentFileUsage]:
    grouped: dict[str, RecentFileUsage] = {}
    for attachment in attachments:
        upload_id = str(attachment.source_upload_id or "").strip()
        if not upload_id:
            continue
        previous = grouped.get(upload_id)
        candidate_time = attachment.updated_at or attachment.created_at
        if previous is None:
            grouped[upload_id] = RecentFileUsage(
                upload_id=upload_id,
                usage_count=1,
                last_used_task_id=attachment.task_id,
                last_used_at=candidate_time,
                last_source_kind=attachment.source_kind,
                selected_sheet=attachment.selected_sheet,
            )
            continue
        latest_time = previous.last_used_at
        newer = latest_time is None or (candidate_time is not None and candidate_time >= latest_time)
        grouped[upload_id] = RecentFileUsage(
            upload_id=upload_id,
            usage_count=previous.usage_count + 1,
            last_used_task_id=attachment.task_id if newer else previous.last_used_task_id,
            last_used_at=candidate_time if newer else previous.last_used_at,
            last_source_kind=attachment.source_kind if newer else previous.last_source_kind,
            selected_sheet=attachment.selected_sheet if newer else previous.selected_sheet,
        )
    return grouped


def candidate_from_resource(
    resource: ConversationFileResource,
    *,
    recent_usage: RecentFileUsage | None = None,
) -> ConversationFileCandidate:
    return ConversationFileCandidate(
        upload_id=resource.file_id,
        filename=resource.normalized_filename or resource.original_filename,
        content_type=resource.normalized_content_type or resource.content_type,
        file_type=resource.file_type,
        size_bytes=resource.size_bytes,
        sha256_short=(resource.sha256 or "")[:12],
        description_summary="",
        preview=_sanitize_preview(resource.preview),
        created_at=resource.created_at,
        selected_sheet=resource.selected_sheet,
        requires_sheet_selection=resource.requires_sheet_selection,
        recent_usage=recent_usage,
    )


def parse_selector_decision(
    raw_response: str | Mapping[str, Any],
    *,
    candidates: Sequence[ConversationFileCandidate],
    profile: FileRequirementProfile,
    confidence_threshold: float = 0.75,
    allow_guarded_multi_select: bool = False,
) -> FileSelectionDecision:
    raw: Mapping[str, Any]
    if isinstance(raw_response, Mapping):
        raw = dict(raw_response)
    else:
        text = str(raw_response or "").strip()
        if not text:
            return FileSelectionDecision("ambiguous", reason_code="empty_selector_output")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return FileSelectionDecision("ambiguous", reason_code="invalid_json")
        if not isinstance(parsed, Mapping):
            return FileSelectionDecision("ambiguous", reason_code="invalid_shape")
        raw = dict(parsed)
    candidate_ids = {candidate.upload_id for candidate in candidates}
    decision = str(raw.get("decision") or raw.get("action") or "").strip().lower()
    if decision in {"no_file", "no_file_needed", "none"}:
        if profile.required:
            return FileSelectionDecision("ambiguous", confidence=_float(raw.get("confidence")), reason_code="required_file_cannot_be_skipped", raw=raw)
        return FileSelectionDecision("no_file_needed", confidence=_float(raw.get("confidence")), reason_code=str(raw.get("reason_code") or "no_file_needed"), raw=raw)
    if decision not in {"select_one", "select_many", "ambiguous", "no_usable_file"}:
        return FileSelectionDecision("ambiguous", reason_code="unknown_decision", raw=raw)
    confidence = _float(raw.get("confidence"))
    if decision in {"ambiguous", "no_usable_file"}:
        return FileSelectionDecision(decision, confidence=confidence, reason_code=str(raw.get("reason_code") or decision), question=str(raw.get("question") or ""), raw=raw)
    ids = tuple(str(item).strip() for item in (raw.get("upload_ids") or raw.get("selected_upload_ids") or []) if str(item).strip())
    if any(upload_id not in candidate_ids for upload_id in ids):
        return FileSelectionDecision("ambiguous", upload_ids=ids, confidence=confidence, reason_code="unknown_upload_id", raw=raw)
    if confidence < confidence_threshold:
        return FileSelectionDecision("ambiguous", upload_ids=ids, confidence=confidence, reason_code="low_confidence", raw=raw)
    if decision == "select_one" and len(ids) == 1:
        return FileSelectionDecision("select_one", upload_ids=ids, confidence=confidence, reason_code=str(raw.get("reason_code") or "selected"), raw=raw)
    if decision == "select_many" and len(ids) >= 2:
        if not (profile.allow_multiple or allow_guarded_multi_select):
            return FileSelectionDecision("ambiguous", upload_ids=ids, confidence=confidence, reason_code="multi_select_requires_confirmation", raw=raw)
        if not allow_guarded_multi_select:
            return FileSelectionDecision("ambiguous", upload_ids=ids, confidence=confidence, reason_code="multi_select_rollout_disabled", raw=raw)
        return FileSelectionDecision("select_many", upload_ids=ids, confidence=confidence, reason_code=str(raw.get("reason_code") or "selected_many"), raw=raw)
    return FileSelectionDecision("ambiguous", upload_ids=ids, confidence=confidence, reason_code="cardinality_mismatch", raw=raw)


def deterministic_file_decision(
    *,
    text: str,
    profile: FileRequirementProfile,
    candidates: Sequence[ConversationFileCandidate],
) -> FileSelectionDecision:
    if not candidates:
        return FileSelectionDecision("no_usable_file", reason_code="no_files_in_conversation")
    if query_declines_files(text):
        return FileSelectionDecision("no_file_needed", reason_code="user_declined_file")
    text_l = (text or "").lower()
    exact = [candidate for candidate in candidates if candidate.upload_id.lower() in text_l]
    if len(exact) == 1:
        return FileSelectionDecision("select_one", (exact[0].upload_id,), 0.99, "explicit_upload_id")
    name_hits = [candidate for candidate in candidates if candidate.filename and candidate.filename.lower() in text_l]
    if len(name_hits) == 1:
        return FileSelectionDecision("select_one", (name_hits[0].upload_id,), 0.9, "filename_match")
    if len(candidates) == 1 and (profile.required or query_mentions_file(text)):
        return FileSelectionDecision("select_one", (candidates[0].upload_id,), 0.82, "single_candidate")
    if "刚才" in text or "继续" in text:
        used = [c for c in candidates if c.recent_usage is not None]
        used.sort(key=lambda c: c.recent_usage.last_used_at or datetime.min, reverse=True)  # type: ignore[union-attr]
        if used:
            return FileSelectionDecision("select_one", (used[0].upload_id,), 0.82, "recent_usage")
    return FileSelectionDecision("ambiguous", reason_code="multiple_candidates" if len(candidates) > 1 else "insufficient_reference")


def render_file_selection_question(
    candidates: Sequence[ConversationFileCandidate],
    *,
    reason_code: str = "multiple_candidates",
) -> str:
    if not candidates:
        return "当前会话还没有可用文件。请上传要使用的文件，或说明这次不需要文件。"
    lines = ["我需要确认这次要使用哪个文件。请回复 upload_id、序号，或重新上传文件："]
    for index, candidate in enumerate(candidates, start=1):
        summary = f"；{candidate.description_summary}" if candidate.description_summary else ""
        usage = ""
        if candidate.recent_usage is not None:
            usage = f"；最近使用 {candidate.recent_usage.usage_count} 次"
        created = f"；上传时间 {candidate.created_at.isoformat()}" if candidate.created_at else ""
        lines.append(f"{index}. {candidate.filename}（upload_id: {candidate.upload_id}{created}{summary}{usage}）")
    if reason_code == "multi_select_rollout_disabled":
        lines.append("如果要同时使用多个文件，请明确回复要使用的多个 upload_id。")
    return "\n".join(lines)


class FileSelectionAnswerResolver:
    def resolve(
        self,
        text: str,
        candidates: Sequence[ConversationFileCandidate],
        *,
        replacement_upload_ids: Sequence[str] = (),
        allow_multiple: bool = False,
    ) -> FileSelectionDecision:
        replacement = tuple(str(item).strip() for item in replacement_upload_ids if str(item).strip())
        if replacement:
            if len(replacement) == 1 or allow_multiple:
                return FileSelectionDecision("select_one" if len(replacement) == 1 else "select_many", replacement, 1.0, "replacement_upload")
            return FileSelectionDecision("ambiguous", replacement, 1.0, "multi_select_requires_confirmation")
        if query_declines_files(text):
            return FileSelectionDecision("no_file_needed", confidence=1.0, reason_code="user_declined_file")
        text_l = (text or "").lower()
        by_id = [candidate for candidate in candidates if candidate.upload_id.lower() in text_l]
        if len(by_id) == 1:
            return FileSelectionDecision("select_one", (by_id[0].upload_id,), 1.0, "explicit_upload_id")
        ordinal = _ordinal_index(text)
        if ordinal is not None and 0 <= ordinal < len(candidates):
            return FileSelectionDecision("select_one", (candidates[ordinal].upload_id,), 0.95, "ordinal")
        if "最新" in text or "最近上传" in text or "latest" in text_l or "newest" in text_l:
            newest = sorted(candidates, key=lambda c: c.created_at or datetime.min, reverse=True)
            if newest:
                return FileSelectionDecision("select_one", (newest[0].upload_id,), 0.9, "newest")
        row_match = _ROW_COUNT_RE.search(text or "")
        if row_match:
            row_count = row_match.group(1)
            matches = [c for c in candidates if str(c.preview.get("row_count") or "") == row_count]
            if len(matches) == 1:
                return FileSelectionDecision("select_one", (matches[0].upload_id,), 0.88, "row_count")
        if "都" in text or "全部" in text or "all" in text_l:
            ids = tuple(c.upload_id for c in candidates)
            if allow_multiple:
                return FileSelectionDecision("select_many", ids, 0.9, "all_files")
            return FileSelectionDecision("ambiguous", ids, 0.9, "multi_select_requires_confirmation")
        used = [c for c in candidates if c.recent_usage is not None]
        if ("刚才" in text or "继续" in text or "recent" in text_l) and used:
            used.sort(key=lambda c: c.recent_usage.last_used_at or datetime.min, reverse=True)  # type: ignore[union-attr]
            return FileSelectionDecision("select_one", (used[0].upload_id,), 0.85, "recent_usage")
        return FileSelectionDecision("ambiguous", reason_code="answer_unresolved")


def _ordinal_index(text: str) -> int | None:
    stripped = (text or "").strip()
    m = _ORDINAL_RE.search(stripped)
    if not m:
        if stripped.isdigit():
            return int(stripped) - 1
        return None
    raw = m.group(1)
    if raw.isdigit():
        return int(raw) - 1
    mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return mapping.get(raw, 0) - 1 if raw in mapping else None


def _sanitize_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(value or {})
    safe: dict[str, Any] = {}
    for key in (
        "shape",
        "file_type",
        "row_count",
        "column_count",
        "line_count",
        "char_count",
        "size_bytes",
        "columns_truncated",
        "normalizations_truncated",
        "requires_sheet_selection",
    ):
        item = raw.get(key)
        if item is None or isinstance(item, bool | int | float):
            safe[key] = item
        elif key in {"shape", "file_type"}:
            safe[key] = str(item)[:40]
    sheets = raw.get("excel_sheets")
    if isinstance(sheets, list | tuple):
        safe["sheet_count"] = len(sheets)
    return safe


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _float(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number
