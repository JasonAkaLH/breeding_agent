from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from src.core.models import SlotCollection, SlotEvent

from .contract import SkillResourceRef
from .input_schema import (
    SkillInputClarification,
    SkillInputField,
    SkillInputSchema,
    SkillInputSourcePolicy,
    SkillInputValidationIssue,
    SkillInputValidationRule,
    validate_selected_schema_payload,
)


SLOT_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed"})
SLOT_STATUS_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "collecting": frozenset({"waiting_for_user", "extracting", "validating", "ready", "failed", "cancelled"}),
    "waiting_for_user": frozenset({"extracting", "cancelled", "failed"}),
    "extracting": frozenset({"validating", "waiting_for_user", "failed", "cancelled"}),
    "validating": frozenset({"waiting_for_user", "ready", "failed", "cancelled"}),
    "ready": frozenset({"script_scheduled", "failed", "cancelled"}),
    "script_scheduled": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|cookie|authorization|credential|provider[_-]?config|database[_-]?url|db[_-]?url)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|cookie|authorization)\s*[:=]\s*([^\s,;，；]+)"
)
_DATABASE_URL_RE = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|redis|mongodb|sqlite)://[^\s,;，；]+")
_LOCAL_PATH_RE = re.compile(
    r"(?:(?:/Users|/private|/var|/tmp|/etc|/opt|/home)/[^\s,;，；]+|[A-Za-z]:[\\/][^\s,;，；]+|\\\\[^\s,;，；]+)"
)
_RAW_ARTIFACT_CONTENT_KEYS = frozenset(
    {
        "content",
        "content_base64",
        "raw_content",
        "file_content",
        "bytes",
        "data_uri",
        "provider_config",
        "database_url",
        "db_url",
        "cookie",
        "authorization",
    }
)
_HISTORY_RECALL_PATTERNS = (
    re.compile(r"之前.*(说|告诉|给|发|上传|提供)", re.IGNORECASE),
    re.compile(r"(不是|不都|已经).*(告诉|说过|给过|发过|上传过)", re.IGNORECASE),
    re.compile(r"(as\s+i\s+said|as\s+mentioned|i\s+already\s+(told|gave|sent|uploaded))", re.IGNORECASE),
)
class SlotStateTransitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SlotExtractionCandidate:
    field: str
    raw_value: Any
    value: Any
    source: str = "llm"
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class SlotExtractionResult:
    resolved: Mapping[str, SlotExtractionCandidate]
    missing: tuple[str, ...] = ()
    invalid: tuple[Mapping[str, Any], ...] = ()
    diagnostics: tuple[str, ...] = ()


def build_schema_snapshot(
    schema: SkillInputSchema,
    *,
    resources: Mapping[str, SkillResourceRef | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for name, field in schema.inputs.items():
        if not field.expose:
            continue
        inputs[name] = _field_snapshot(field)
    resource_snapshots: dict[str, Any] = {}
    for resource_id, resource in (resources or {}).items():
        if isinstance(resource, SkillResourceRef):
            resource_snapshots[resource_id] = {
                "resource_id": resource.resource_id,
                "title": resource.title,
                "description": resource.description,
                "audience": list(resource.audience),
            }
        elif isinstance(resource, Mapping):
            safe = redact_prompt_safe(dict(resource))
            if isinstance(safe, Mapping):
                resource_snapshots[resource_id] = dict(safe)
    return {
        "schema_version": schema.schema_version,
        "schema_id": schema.schema_id,
        "title": schema.title,
        "description": schema.description,
        "entrypoint_mapping": schema.entrypoint_mapping,
        "inputs": inputs,
        "constraints": [dict(item) for item in schema.constraints],
        "slot_policy": dict(schema.slot_policy),
        "resources": resource_snapshots,
    }


def schema_from_snapshot(snapshot: Mapping[str, Any]) -> SkillInputSchema:
    schema_id = str(snapshot.get("schema_id") or "").strip()
    if not schema_id:
        raise SlotStateTransitionError("slot schema snapshot is missing schema_id")
    inputs_raw = snapshot.get("inputs")
    if not isinstance(inputs_raw, Mapping):
        raise SlotStateTransitionError("slot schema snapshot is missing inputs")
    inputs = {
        str(name): _field_from_snapshot(str(name), field)
        for name, field in inputs_raw.items()
        if isinstance(field, Mapping) and str(name).strip()
    }
    return SkillInputSchema(
        schema_version=str(snapshot.get("schema_version") or "1"),
        schema_id=schema_id,
        title=str(snapshot.get("title") or schema_id),
        description=str(snapshot.get("description") or ""),
        inputs=inputs,
        constraints=tuple(dict(item) for item in snapshot.get("constraints", ()) if isinstance(item, Mapping)),
        slot_policy=dict(snapshot.get("slot_policy") or {}) if isinstance(snapshot.get("slot_policy"), Mapping) else {},
        entrypoint_mapping=str(snapshot.get("entrypoint_mapping") or ""),
    )


def initialize_input_collection(
    *,
    collection_id: str | None = None,
    task_id: str,
    node_id: str,
    conversation_id: str,
    capability_id: str,
    skill_name: str,
    schema: SkillInputSchema,
    selected_entrypoint: str,
    now: datetime | None = None,
    skill_bundle_revision: str | None = None,
    contract_revision: str | None = None,
    schema_digest: str | None = None,
    resources: Mapping[str, SkillResourceRef | Mapping[str, Any]] | None = None,
) -> tuple[SlotCollection, SlotEvent]:
    at = now or datetime.utcnow()
    snapshot = build_schema_snapshot(schema, resources=resources)
    collection = SlotCollection(
        collection_id=collection_id or f"slotcol_{uuid4().hex}",
        task_id=task_id,
        node_id=node_id,
        conversation_id=conversation_id,
        capability_id=capability_id,
        skill_name=skill_name,
        kind="input_collection",
        status="collecting",
        round=1,
        revision=0,
        selected_schema_id=schema.schema_id,
        selected_entrypoint=selected_entrypoint,
        skill_bundle_revision=skill_bundle_revision,
        contract_revision=contract_revision,
        schema_digest=schema_digest,
        schema_snapshot=snapshot,
        slots=_initial_slots(schema),
        resolved={},
        missing=_required_fields_for_payload(schema, {}),
        invalid=(),
        created_at=at,
        updated_at=at,
    )
    event = _slot_event(
        collection,
        event_type="slot.collection_started",
        payload={
            "kind": collection.kind,
            "selected_schema_id": schema.schema_id,
            "missing": list(collection.missing),
        },
        now=at,
    )
    return collection, event


def transition_slot_collection(
    collection: SlotCollection,
    *,
    to_status: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[SlotCollection, SlotEvent]:
    _ensure_transition_allowed(collection.status, to_status)
    at = now or datetime.utcnow()
    terminal_updates: dict[str, datetime | None] = {
        "completed_at": collection.completed_at,
        "cancelled_at": collection.cancelled_at,
        "failed_at": collection.failed_at,
    }
    if to_status == "completed":
        terminal_updates["completed_at"] = at
    elif to_status == "cancelled":
        terminal_updates["cancelled_at"] = at
    elif to_status == "failed":
        terminal_updates["failed_at"] = at
    next_collection = replace(
        collection,
        status=to_status,
        revision=collection.revision + 1,
        updated_at=at,
        **terminal_updates,
    )
    event = _slot_event(
        next_collection,
        event_type=event_type,
        payload=dict(payload or {}),
        idempotency_key=idempotency_key,
        now=at,
    )
    return next_collection, event


def build_normal_extraction_prompt(
    collection: SlotCollection,
    *,
    current_user_answer: str,
    artifact_summaries: tuple[Mapping[str, Any], ...] = (),
    turn_hint: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "mode": "normal_extraction",
        "instructions": [
            "Return JSON only.",
            "Resolve only fields declared in slot_collection.missing or slot_collection.invalid.",
            "Use turn_hint.target_slots and turn_hint.reason only as mapping hints for the current answer.",
            "Preserve raw user text in raw_value/raw and put canonical value in value when possible.",
            "Do not invent artifacts, file paths, schema fields, or internal execution details.",
        ],
        "current_user_answer": current_user_answer,
        "artifact_summaries": [dict(item) for item in artifact_summaries],
        "slot_collection": _collection_prompt_snapshot(collection),
        "output_schema": {
            "resolved": {"field_name": {"raw_value": "original text", "value": "canonical candidate", "source": "current_answer"}},
            "missing": ["field_name"],
            "invalid": [{"field": "field_name", "reason": "short_code"}],
        },
    }
    hint = _turn_hint_prompt_payload(turn_hint)
    if hint:
        payload["turn_hint"] = hint
    return _json_prompt(payload)


def build_history_recall_prompt(
    collection: SlotCollection,
    *,
    current_user_answer: str,
    accepted_answer_summaries: tuple[Mapping[str, Any], ...] = (),
    turn_hint: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "mode": "history_recall_extraction",
        "instructions": [
            "Use only bounded user-origin accepted answer summaries.",
            "Use turn_hint.target_slots and turn_hint.reason only as mapping hints for the current answer.",
            "Return JSON only in the same resolved/missing/invalid shape as normal extraction.",
            "If history does not contain enough evidence, leave the field missing.",
        ],
        "current_user_answer": current_user_answer,
        "accepted_answer_summaries": [dict(item) for item in accepted_answer_summaries],
        "slot_collection": _collection_prompt_snapshot(collection),
    }
    hint = _turn_hint_prompt_payload(turn_hint)
    if hint:
        payload["turn_hint"] = hint
    return _json_prompt(payload)


def should_trigger_history_recall(text: str) -> bool:
    stripped = str(text or "").strip()
    return bool(stripped) and any(pattern.search(stripped) for pattern in _HISTORY_RECALL_PATTERNS)


def parse_slot_extraction_response(raw_response: str, collection: SlotCollection) -> SlotExtractionResult:
    diagnostics: list[str] = []
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return SlotExtractionResult(resolved={}, diagnostics=("invalid_json",))
    if not isinstance(parsed, Mapping):
        return SlotExtractionResult(resolved={}, diagnostics=("invalid_json",))
    allowed_fields = _allowed_extraction_fields(collection)
    resolved_raw = parsed.get("resolved") or {}
    if not isinstance(resolved_raw, Mapping):
        diagnostics.append("resolved_not_mapping")
        resolved_raw = {}
    resolved: dict[str, SlotExtractionCandidate] = {}
    for field, candidate in resolved_raw.items():
        field_name = str(field)
        if field_name not in allowed_fields:
            diagnostics.append(f"unknown_field:{field_name}")
            continue
        if not isinstance(candidate, Mapping):
            diagnostics.append(f"invalid_candidate:{field_name}")
            continue
        raw_value = candidate.get("raw_value", candidate.get("raw", candidate.get("value")))
        value = candidate.get("value", raw_value)
        confidence_raw = candidate.get("confidence")
        try:
            confidence = None if confidence_raw is None else float(confidence_raw)
        except (TypeError, ValueError):
            confidence = None
        source = str(candidate.get("source") or "llm")
        slot = collection.slots.get(field_name) if isinstance(collection.slots, Mapping) else None
        slot_type = str(slot.get("type") or "") if isinstance(slot, Mapping) else ""
        if slot_type in {"artifact", "file", "data"}:
            source = "llm_artifact_claim"
        resolved[field_name] = SlotExtractionCandidate(
            field=field_name,
            raw_value=redact_prompt_safe(raw_value),
            value=redact_prompt_safe(value),
            source=source,
            confidence=confidence,
        )
    return SlotExtractionResult(
        resolved=resolved,
        missing=_string_tuple(parsed.get("missing")),
        invalid=tuple(dict(item) for item in parsed.get("invalid", ()) if isinstance(item, Mapping)),
        diagnostics=tuple(diagnostics),
    )


def merge_slot_extraction_results(
    primary: SlotExtractionResult,
    fallback: SlotExtractionResult,
    *,
    collection: SlotCollection | None = None,
) -> SlotExtractionResult:
    resolved = dict(primary.resolved)
    for field, candidate in fallback.resolved.items():
        current = resolved.get(field)
        if current is None or _should_replace_extraction_candidate(collection, field, current, candidate):
            resolved[field] = candidate
    return SlotExtractionResult(
        resolved=resolved,
        missing=tuple(dict.fromkeys((*primary.missing, *fallback.missing))),
        invalid=tuple((*primary.invalid, *fallback.invalid)),
        diagnostics=tuple(dict.fromkeys((*primary.diagnostics, *fallback.diagnostics))),
    )


def build_backend_slot_extraction(
    collection: SlotCollection,
    schema: SkillInputSchema,
    *,
    current_user_answer: str = "",
    current_upload_ids: tuple[str, ...] = (),
    artifact_summaries: tuple[Mapping[str, Any], ...] = (),
    accepted_answer_summaries: tuple[Mapping[str, Any], ...] = (),
    history_recall: bool = False,
    turn_target_slots: tuple[str, ...] = (),
    turn_reason: str | None = None,
) -> SlotExtractionResult:
    allowed_fields = _allowed_extraction_fields(collection)
    resolved: dict[str, SlotExtractionCandidate] = {}
    diagnostics: list[str] = []
    target_hint_fields = tuple(
        dict.fromkeys(str(field).strip() for field in turn_target_slots if str(field).strip() in allowed_fields)
    )
    artifact_fields = [
        field_name
        for field_name in allowed_fields
        if (field := schema.inputs.get(field_name)) is not None and field.type in {"artifact", "file", "data"}
    ]
    artifact_upload_ids = _backend_artifact_upload_ids(
        current_upload_ids=current_upload_ids,
        artifact_summaries=artifact_summaries,
        accepted_answer_summaries=accepted_answer_summaries,
        history_recall=history_recall,
        artifact_field_count=len(artifact_fields),
    )
    artifact_by_upload_id = _artifact_summary_by_upload_id(artifact_summaries)
    for field_name in allowed_fields:
        field = schema.inputs.get(field_name)
        if field is None or not field.expose:
            continue
        if field.type in {"artifact", "file", "data"}:
            if artifact_upload_ids:
                value = _artifact_slot_value(artifact_upload_ids, artifact_by_upload_id)
                resolved[field_name] = SlotExtractionCandidate(
                    field=field_name,
                    raw_value=value,
                    value=value,
                    source="task_attachment" if any(upload_id in artifact_by_upload_id for upload_id in artifact_upload_ids) else "upload_ledger",
                    confidence=1.0,
                )
            continue
        allow_bare_scalar_from_hint = _allow_bare_scalar_from_turn_hint(
            field_name=field_name,
            allowed_fields=allowed_fields,
            turn_target_slots=target_hint_fields,
            turn_reason=turn_reason,
        )
        value = _match_backend_scalar_field(field, current_user_answer)
        used_turn_hint_scalar = False
        if value is None and allow_bare_scalar_from_hint:
            value = _match_backend_bare_scalar_field(field, str(current_user_answer or "").strip())
            used_turn_hint_scalar = value is not None
        value_from_history = False
        if value is None and history_recall:
            value = _match_backend_scalar_from_history(field, accepted_answer_summaries)
            value_from_history = value is not None
        if value is not None:
            resolved[field_name] = SlotExtractionCandidate(
                field=field_name,
                raw_value=current_user_answer if current_user_answer else value,
                value=value,
                source="history" if value_from_history else "turn_hint" if used_turn_hint_scalar else "current_answer",
                confidence=1.0,
            )
            if used_turn_hint_scalar:
                diagnostics.append("backend_turn_hint_scalar_match")
    if artifact_upload_ids:
        diagnostics.append("backend_artifact_ledger_match")
    if resolved:
        diagnostics.append("backend_fact_merge")
    return SlotExtractionResult(resolved=resolved, diagnostics=tuple(diagnostics))


def apply_extraction_result_to_collection(
    collection: SlotCollection,
    schema: SkillInputSchema,
    extraction: SlotExtractionResult,
    *,
    now: datetime | None = None,
) -> tuple[SlotCollection, SlotEvent]:
    _ensure_transition_allowed(collection.status, "ready")
    _ensure_transition_allowed(collection.status, "waiting_for_user")
    at = now or datetime.utcnow()
    resolved = {
        str(field): dict(value) if isinstance(value, Mapping) else {"value": value}
        for field, value in dict(collection.resolved).items()
    }
    payload: dict[str, Any] = {}
    candidate_sources: dict[str, str] = {}
    for field, value in resolved.items():
        if isinstance(value, Mapping):
            payload[field] = value.get("value", value.get("raw_value"))
            candidate_sources[field] = str(value.get("source") or "payload")
    for field, candidate in extraction.resolved.items():
        schema_field = schema.inputs.get(field)
        if schema_field is None or not schema_field.expose:
            continue
        canonical_value = _canonicalize_candidate_value(schema_field, candidate.raw_value, candidate.value)
        resolved[field] = {
            "raw_value": candidate.raw_value,
            "value": canonical_value,
            "source": candidate.source or "llm",
        }
        if candidate.confidence is not None:
            resolved[field]["confidence"] = candidate.confidence
        payload[field] = canonical_value
        candidate_sources[field] = candidate.source or "llm"
    validation = validate_selected_schema_payload(schema, payload, candidate_sources=candidate_sources)
    invalid = _validation_issues_to_json(validation.invalid)
    missing = tuple(
        dict.fromkeys(
            (
                *validation.missing,
                *(issue.field for issue in validation.invalid if issue.field in schema.inputs),
                *(field for field in extraction.missing if field in schema.inputs and field not in payload),
            )
        )
    )
    slots = _slots_for_validation(schema, resolved, missing, invalid)
    ready = not missing and not invalid
    status = "ready" if ready else "waiting_for_user"
    event_type = "slot.collection_ready" if ready else "slot.validation_failed" if invalid else "slot.collection_updated"
    next_collection = replace(
        collection,
        status=status,
        revision=collection.revision + 1,
        resolved=resolved,
        slots=slots,
        missing=missing,
        invalid=invalid,
        updated_at=at,
    )
    event = _slot_event(
        next_collection,
        event_type=event_type,
        payload={
            "resolved_fields": sorted(extraction.resolved),
            "missing": list(missing),
            "invalid": list(invalid),
            "diagnostics": list(extraction.diagnostics),
        },
        now=at,
    )
    return next_collection, event


def _should_replace_extraction_candidate(
    collection: SlotCollection | None,
    field: str,
    current: SlotExtractionCandidate,
    candidate: SlotExtractionCandidate,
) -> bool:
    if candidate.source not in {"task_attachment", "upload_ledger", "validated_artifact"}:
        return False
    if current.source not in {"llm_artifact_claim"}:
        return False
    if collection is None:
        return True
    slot = collection.slots.get(field) if isinstance(collection.slots, Mapping) else None
    slot_type = str(slot.get("type") or "") if isinstance(slot, Mapping) else ""
    return slot_type in {"artifact", "file", "data"}


def _artifact_summary_by_upload_id(artifact_summaries: tuple[Mapping[str, Any], ...]) -> dict[str, Mapping[str, Any]]:
    by_upload_id: dict[str, Mapping[str, Any]] = {}
    for summary in artifact_summaries:
        upload_id = _artifact_upload_id(summary)
        if upload_id:
            by_upload_id[upload_id] = summary
    return by_upload_id


def _artifact_upload_id(summary: Mapping[str, Any]) -> str:
    for key in ("upload_id", "source_upload_id"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _backend_artifact_upload_ids(
    *,
    current_upload_ids: tuple[str, ...],
    artifact_summaries: tuple[Mapping[str, Any], ...],
    accepted_answer_summaries: tuple[Mapping[str, Any], ...],
    history_recall: bool,
    artifact_field_count: int,
) -> tuple[str, ...]:
    if current_upload_ids:
        return tuple(dict.fromkeys(str(upload_id).strip() for upload_id in current_upload_ids if str(upload_id).strip()))
    artifact_ids = tuple(_artifact_upload_id(summary) for summary in artifact_summaries)
    artifact_ids = tuple(dict.fromkeys(upload_id for upload_id in artifact_ids if upload_id))
    if history_recall:
        accepted_upload_ids: list[str] = []
        for summary in accepted_answer_summaries:
            raw_upload_ids = summary.get("upload_ids")
            if isinstance(raw_upload_ids, str):
                accepted_upload_ids.append(raw_upload_ids)
            elif isinstance(raw_upload_ids, list | tuple):
                accepted_upload_ids.extend(str(item) for item in raw_upload_ids)
        accepted = tuple(dict.fromkeys(upload_id.strip() for upload_id in accepted_upload_ids if upload_id.strip()))
        matched = tuple(upload_id for upload_id in accepted if not artifact_ids or upload_id in artifact_ids)
        if matched:
            return matched
        if artifact_field_count == 1:
            return artifact_ids
    if artifact_field_count == 1:
        return artifact_ids
    return ()


def _artifact_slot_value(upload_ids: tuple[str, ...], artifact_by_upload_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for upload_id in upload_ids:
        summary = artifact_by_upload_id.get(upload_id)
        if summary is None:
            summaries.append({"upload_id": upload_id})
            continue
        summaries.append(
            {
                key: redact_prompt_safe(value)
                for key, value in summary.items()
                if key
                in {
                    "upload_id",
                    "filename",
                    "content_type",
                    "file_type",
                    "size_bytes",
                    "sha256",
                    "selected_sheet",
                    "columns",
                    "row_count",
                    "column_count",
                    "source_kind",
                    "source_message_id",
                    "interrupt_answer_id",
                    "created_at",
                }
            }
        )
    return {
        "available": True,
        "count": len(upload_ids),
        "upload_ids": list(upload_ids),
        "artifacts": summaries,
    }


def _match_backend_scalar_from_history(field: SkillInputField, accepted_answer_summaries: tuple[Mapping[str, Any], ...]) -> Any | None:
    for summary in reversed(accepted_answer_summaries):
        text = summary.get("text")
        if isinstance(text, str):
            value = _match_backend_scalar_field(field, text)
            if value is not None:
                return value
    return None


def _match_backend_scalar_field(field: SkillInputField, text: str) -> Any | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    if field.const is not None and _text_mentions_field_alias(field, stripped):
        return field.const
    for pattern in field.patterns:
        try:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
        except re.error:
            continue
        if match is None:
            continue
        raw = next((group for group in match.groups() if group not in (None, "")), match.group(0))
        return _coerce_backend_scalar(field, raw)
    if field.type in {"integer", "int"} and _integer_field_uses_column_context(field):
        column_match = re.search(r"([+-]?\d+|[零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟]+)\s*列", stripped)
        if column_match is not None:
            return _parse_positive_integer(column_match.group(1))
    aliases = tuple(dict.fromkeys((field.name, *field.aliases)))
    for alias in aliases:
        alias_text = str(alias or "").strip()
        if not alias_text:
            continue
        if field.type in {"integer", "int"}:
            patterns = (
                rf"(?:{re.escape(alias_text)})\s*[:：=]?\s*([+-]?\d+|[零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟]+)",
                rf"(?:{re.escape(alias_text)})\s*(?:设为|设置为|改为|改成|变为|为|是)\s*([+-]?\d+|[零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟]+)",
                rf"([+-]?\d+|[零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟]+)\s*(?:个|次|列)?\s*(?:{re.escape(alias_text)})",
            )
        elif field.type in {"number", "float"}:
            patterns = (rf"(?:{re.escape(alias_text)})\s*[:：=]?\s*([+-]?\d+(?:\.\d+)?)",)
        elif field.type in {"boolean", "bool"}:
            patterns = (rf"(?:{re.escape(alias_text)})\s*[:：=]?\s*(true|false|yes|no|1|0|是|否|要|不要)",)
        else:
            patterns = (rf"(?:{re.escape(alias_text)})\s*[:：=]\s*([^\s,，。；;]+)",)
        for pattern in patterns:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if match is not None:
                return _coerce_backend_scalar(field, match.group(1))
    if field.enum:
        for alias in aliases:
            alias_text = str(alias or "").strip()
            if alias_text and re.search(rf"(?:{re.escape(alias_text)})\s*[:：=]", stripped, flags=re.IGNORECASE):
                for enum_item in field.enum:
                    if str(enum_item).lower() in stripped.lower():
                        return enum_item
    return None


def _allow_bare_scalar_from_turn_hint(
    *,
    field_name: str,
    allowed_fields: set[str],
    turn_target_slots: tuple[str, ...],
    turn_reason: str | None,
) -> bool:
    if turn_target_slots:
        return len(turn_target_slots) == 1 and turn_target_slots[0] == field_name
    return bool(turn_reason and len(allowed_fields) == 1 and field_name in allowed_fields)


def _match_backend_bare_scalar_field(field: SkillInputField, stripped: str) -> Any | None:
    if field.type in {"integer", "int"} and re.fullmatch(
        r"[+-]?\d+|[零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟]+",
        stripped,
    ):
        return _parse_positive_integer(stripped)
    if field.type in {"number", "float"} and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", stripped):
        return _parse_number(stripped)
    if field.type in {"boolean", "bool"}:
        return _parse_bool(stripped)
    if field.enum:
        for enum_item in field.enum:
            if stripped.lower() == str(enum_item).lower():
                return enum_item
    return None


def _text_mentions_field_alias(field: SkillInputField, text: str) -> bool:
    text_lower = text.lower()
    aliases = tuple(dict.fromkeys((field.name, field.const, *field.aliases)))
    return any(str(alias or "").strip().lower() and str(alias or "").strip().lower() in text_lower for alias in aliases)


def _integer_field_uses_column_context(field: SkillInputField) -> bool:
    aliases = " ".join(str(alias) for alias in (field.name, *field.aliases)).lower()
    description = str(field.description or "")
    return "ncols" in aliases or "列数" in aliases or "列" in description


def _coerce_backend_scalar(field: SkillInputField, value: Any) -> Any | None:
    if field.type in {"integer", "int"}:
        return _parse_positive_integer(value)
    if field.type in {"number", "float"}:
        return _parse_number(value)
    if field.type in {"boolean", "bool"}:
        return _parse_bool(value)
    if field.enum:
        text = str(value or "").strip()
        for enum_item in field.enum:
            if text.lower() == str(enum_item).lower():
                return enum_item
        return None
    if field.const is not None:
        return field.const if _text_mentions_field_alias(field, str(value)) else None
    return str(value).strip() or None


def redact_prompt_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _RAW_ARTIFACT_CONTENT_KEYS or _SECRET_KEY_RE.search(key_text):
                redacted[key_text] = "[REDACTED_SECRET]" if _SECRET_KEY_RE.search(key_text) else "[REDACTED_ARTIFACT_CONTENT]"
                continue
            redacted[key_text] = redact_prompt_safe(item)
        return redacted
    if isinstance(value, list | tuple):
        return [redact_prompt_safe(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _field_snapshot(field: SkillInputField) -> dict[str, Any]:
    validation: dict[str, Any] = {}
    if field.validation.regex:
        validation["regex"] = field.validation.regex
    if field.validation.min is not None:
        validation["min"] = field.validation.min
    if field.validation.max is not None:
        validation["max"] = field.validation.max
    if field.validation.min_length is not None:
        validation["min_length"] = field.validation.min_length
    if field.validation.max_length is not None:
        validation["max_length"] = field.validation.max_length
    if field.validation.message:
        validation["message"] = field.validation.message
    snapshot = {
        "name": field.name,
        "type": field.type,
        "title": field.title,
        "required": field.required,
        "required_when": dict(field.required_when),
        "source": {"allowed": list(field.source.allowed)},
        "aliases": list(field.aliases),
        "patterns": list(field.patterns),
        "description": field.description,
        "question": field.question,
        "reference_resource": field.reference_resource,
        "clarification": {
            "hint": field.clarification.hint,
            "examples": list(field.clarification.examples),
        },
        "validation": validation,
    }
    if field.default is not None:
        snapshot["default"] = field.default
    if field.enum:
        snapshot["enum"] = list(field.enum)
    if field.const is not None:
        snapshot["const"] = field.const
    return snapshot


def _field_from_snapshot(name: str, snapshot: Mapping[str, Any]) -> SkillInputField:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    clarification = snapshot.get("clarification") if isinstance(snapshot.get("clarification"), Mapping) else {}
    validation = snapshot.get("validation") if isinstance(snapshot.get("validation"), Mapping) else {}
    return SkillInputField(
        name=name,
        type=str(snapshot.get("type") or "string"),
        title=str(snapshot.get("title") or ""),
        required=bool(snapshot.get("required", False)),
        required_when=dict(snapshot.get("required_when") or {}) if isinstance(snapshot.get("required_when"), Mapping) else {},
        source=SkillInputSourcePolicy(allowed=_string_tuple(source.get("allowed") if isinstance(source, Mapping) else ())),
        aliases=_string_tuple(snapshot.get("aliases")),
        patterns=_string_tuple(snapshot.get("patterns")),
        default=snapshot.get("default"),
        enum=_string_tuple(snapshot.get("enum")),
        const=snapshot.get("const"),
        description=str(snapshot.get("description") or ""),
        question=str(snapshot.get("question") or ""),
        reference_resource=str(snapshot.get("reference_resource") or ""),
        clarification=SkillInputClarification(
            hint=str(clarification.get("hint") or ""),
            examples=_string_tuple(clarification.get("examples")),
        ),
        validation=SkillInputValidationRule(
            regex=str(validation.get("regex") or ""),
            min=_number_or_none(validation.get("min")),
            max=_number_or_none(validation.get("max")),
            min_length=_int_or_none(validation.get("min_length")),
            max_length=_int_or_none(validation.get("max_length")),
            message=str(validation.get("message") or ""),
        ),
        expose=True,
    )


def _ensure_transition_allowed(from_status: str, to_status: str) -> None:
    if to_status not in SLOT_STATUS_TRANSITIONS:
        raise SlotStateTransitionError(f"Unknown slot status: {to_status}")
    allowed = SLOT_STATUS_TRANSITIONS.get(from_status)
    if allowed is None:
        raise SlotStateTransitionError(f"Unknown current slot status: {from_status}")
    if to_status not in allowed:
        raise SlotStateTransitionError(f"Invalid slot transition: {from_status} -> {to_status}")


def _slot_event(
    collection: SlotCollection,
    *,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> SlotEvent:
    return SlotEvent(
        slot_event_id=f"slotevt_{uuid4().hex}",
        collection_id=collection.collection_id,
        task_id=collection.task_id,
        node_id=collection.node_id,
        conversation_id=collection.conversation_id,
        event_type=event_type,
        round=collection.round,
        revision=collection.revision,
        idempotency_key=idempotency_key,
        payload=redact_prompt_safe(dict(payload or {})),
        created_at=now or datetime.utcnow(),
    )


def _initial_slots(schema: SkillInputSchema) -> dict[str, dict[str, Any]]:
    slots: dict[str, dict[str, Any]] = {}
    for name, field in schema.inputs.items():
        if not field.expose:
            continue
        required_now = field.required or bool(field.required_when)
        slots[name] = {
            "name": name,
            "label": field.description or name,
            "type": field.type,
            "required_now": required_now,
            "source": {"allowed": list(field.source.allowed)},
            "status": "missing" if required_now and field.default is None else "optional",
        }
        if field.enum:
            slots[name]["enum"] = list(field.enum)
        if field.const is not None:
            slots[name]["const"] = field.const
        if field.default is not None:
            slots[name]["default"] = field.default
    return slots


def _required_fields_for_payload(schema: SkillInputSchema, payload: Mapping[str, Any]) -> tuple[str, ...]:
    validation = validate_selected_schema_payload(schema, payload, candidate_sources={})
    return validation.missing


def _collection_prompt_snapshot(collection: SlotCollection) -> dict[str, Any]:
    invalid_fields: list[str] = []
    for item in collection.invalid:
        if isinstance(item, Mapping) and item.get("field"):
            invalid_fields.append(str(item["field"]))
    return {
        "collection_id": collection.collection_id,
        "kind": collection.kind,
        "status": collection.status,
        "selected_schema_id": collection.selected_schema_id,
        "round": collection.round,
        "revision": collection.revision,
        "missing": list(collection.missing),
        "invalid": invalid_fields,
        "resolved": redact_prompt_safe(collection.resolved),
        "slots": redact_prompt_safe(collection.slots),
        "schema_snapshot": redact_prompt_safe(collection.schema_snapshot),
    }


def _turn_hint_prompt_payload(turn_hint: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(turn_hint, Mapping):
        return {}
    payload: dict[str, Any] = {}
    target_slots = turn_hint.get("target_slots")
    if isinstance(target_slots, str):
        slots = (target_slots,)
    elif isinstance(target_slots, list | tuple | set):
        slots = tuple(target_slots)
    else:
        slots = ()
    clean_slots = [str(item).strip() for item in slots if str(item).strip()]
    if clean_slots:
        payload["target_slots"] = clean_slots
    for key in ("reason", "part_id", "source"):
        value = str(turn_hint.get(key) or "").strip()
        if value:
            payload[key] = value
    confidence = turn_hint.get("confidence")
    if confidence is not None:
        try:
            payload["confidence"] = max(0.0, min(float(confidence), 1.0))
        except (TypeError, ValueError):
            pass
    if isinstance(turn_hint.get("uses_uploads"), bool):
        payload["uses_uploads"] = bool(turn_hint["uses_uploads"])
    return payload


def _json_prompt(payload: Mapping[str, Any]) -> str:
    return json.dumps(redact_prompt_safe(dict(payload)), ensure_ascii=False, sort_keys=True)


def _allowed_extraction_fields(collection: SlotCollection) -> set[str]:
    allowed = set(collection.missing)
    for item in collection.invalid:
        if isinstance(item, Mapping) and item.get("field"):
            allowed.add(str(item["field"]))
    return allowed


def _canonicalize_candidate_value(field: SkillInputField, raw_value: Any, candidate_value: Any) -> Any:
    if field.const is not None:
        const_text = str(field.const).strip().lower()
        values = [candidate_value, raw_value]
        aliases = tuple(dict.fromkeys((field.name, field.const, *field.aliases)))
        for value in values:
            text = str(value or "").strip().lower()
            if text == const_text:
                return field.const
            if any(str(alias or "").strip().lower() == text for alias in aliases):
                return field.const
            if any(str(alias or "").strip().lower() and str(alias or "").strip().lower() in text for alias in aliases):
                return field.const
        return candidate_value
    if field.enum:
        for value in (candidate_value, raw_value):
            text = str(value or "").strip()
            for enum_item in field.enum:
                if text.lower() == str(enum_item).lower():
                    return enum_item
    if field.type in {"integer", "int"}:
        return _parse_positive_integer(candidate_value) or _parse_positive_integer(raw_value) or candidate_value
    if field.type in {"number", "float"}:
        return _parse_number(candidate_value) if _parse_number(candidate_value) is not None else _parse_number(raw_value) or candidate_value
    if field.type in {"boolean", "bool"}:
        parsed = _parse_bool(candidate_value)
        return parsed if parsed is not None else (_parse_bool(raw_value) if _parse_bool(raw_value) is not None else candidate_value)
    return candidate_value


def _parse_positive_integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    direct = re.fullmatch(r"[+-]?\d+", text)
    if direct is not None:
        return int(text)
    arabic = re.search(r"(?<![\d.])([+-]?\d+)(?![\d.])", text)
    if arabic is not None:
        return int(arabic.group(1))
    return None


def _parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"[+-]?\d+(?:\.\d+)?", str(value))
    if match is None:
        return None
    return float(match.group(0))


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "是", "要", "随机"}:
        return True
    if text in {"false", "0", "no", "n", "否", "不要", "不随机"}:
        return False
    return None


def _validation_issues_to_json(issues: tuple[SkillInputValidationIssue, ...]) -> tuple[Mapping[str, Any], ...]:
    return tuple({"field": issue.field, "reason": issue.reason, "message": issue.message} for issue in issues)


def _slots_for_validation(
    schema: SkillInputSchema,
    resolved: Mapping[str, Mapping[str, Any]],
    missing: tuple[str, ...],
    invalid: tuple[Mapping[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    slots = _initial_slots(schema)
    invalid_by_field = {str(item["field"]): dict(item) for item in invalid if isinstance(item, Mapping) and item.get("field")}
    for field, value in resolved.items():
        if field not in slots:
            continue
        slots[field].update(
            {
                "status": "resolved",
                "raw_value": value.get("raw_value") if isinstance(value, Mapping) else value,
                "value": value.get("value") if isinstance(value, Mapping) else value,
                "source": value.get("source") if isinstance(value, Mapping) else "payload",
            }
        )
    for field in missing:
        if field in slots:
            slots[field]["status"] = "missing"
    for field, issue in invalid_by_field.items():
        if field in slots:
            slots[field]["status"] = "invalid"
            slots[field]["validation_error"] = issue
    return slots


def _redact_string(value: str) -> str:
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED_SECRET]", value)
    text = _DATABASE_URL_RE.sub("[REDACTED_URL]", text)
    text = _LOCAL_PATH_RE.sub("[REDACTED_PATH]", text)
    return text


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
