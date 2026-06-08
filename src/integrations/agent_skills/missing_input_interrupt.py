from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from collections.abc import Iterable, Mapping
from typing import Any

from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import InterruptStatus
from src.core.models import Interrupt, SlotCollection, SlotEvent
from src.integrations.llm_request_options import llm_option_metadata

from .input_schema import load_input_schemas_for_contract
from .manifest import SkillManifest
from .parameters import SkillParameterSpec
from .slot_state import build_schema_snapshot, redact_prompt_safe


_FIELD_LABELS = {
    "blocks": "区组数/重复数",
    "ck_spec": "CK 起始位置和间隔",
    "design": "设计类型",
    "field_data": "田间表型数据文件",
    "file_path": "图片/PDF 文件或本地文件路径",
    "material_data": "试验材料 CSV/JSON 文件",
    "ncols": "田块列数",
    "query": "用户问题",
    "rice_input": "水稻 VCF/VCF.GZ 或 gene_check JSON 文件",
    "sample": "样本名",
    "samples": "样本列表",
    "variety": "品种名称",
}

_FIELD_DESCRIPTIONS = {
    "blocks": "随机区组重复数，例如 3。",
    "ck_spec": "Interval 间比法 CK 参数，格式：ck_no,start_pos,interval；多个 CK 用分号分隔。",
    "design": "设计类型，例如 rcbd、diagonal 或 interval。",
    "field_data": "请上传包含田间表型数据的 CSV/JSON 文件。",
    "file_path": "请上传图片/PDF，或提供可访问的本地文件路径。",
    "material_data": "请上传试验材料 CSV/JSON 文件；推荐列名 ped_id,hyb_check,set。",
    "ncols": "田块列数，例如 10。",
    "query": "请补充要处理的问题或指令。",
    "rice_input": "请上传水稻 VCF/VCF.GZ 文件，或已有 gene_check JSON 结果。",
    "sample": "请补充样本名。",
    "samples": "请补充样本列表。",
    "variety": "请补充品种名称。",
}

_ARTIFACT_FIELDS = {"field_data", "file_path", "material_data", "rice_input"}
SLOT_COLLECTION_FIELD = "_slot_collection"
SLOT_COLLECTION_REF_FIELD = "_slot_collection_ref"
SLOT_COLLECTION_SCHEMA_VERSION = 1
SLOT_COLLECTION_V2_SCHEMA_VERSION = 2
SLOT_COLLECTION_METADATA_KEY = "skill_slot_collection"
_SENSITIVE_SLOT_KEYS = {
    "authorization",
    "base_url",
    "content",
    "content_base64",
    "cookie",
    "db_url",
    "password",
    "provider_config",
    "secret",
    "token",
}


def missing_input_fields_from_payload(output_payload: Mapping[str, Any]) -> tuple[str, ...]:
    error = output_payload.get("error") if isinstance(output_payload.get("error"), Mapping) else {}
    error_type = str(error.get("type") or output_payload.get("error_type") or "").strip().lower()
    if error_type != "missing_input":
        return ()
    return _clean_missing_fields(output_payload.get("missing"))


def build_missing_input_interrupt(
    *,
    request: CapabilityExecutionRequest,
    manifest: SkillManifest,
    skill_name: str,
    entrypoint: str,
    missing: Iterable[str],
    resolved_payload: Mapping[str, Any] | None = None,
    sources: Mapping[str, Any] | None = None,
) -> Interrupt | None:
    missing_fields = _clean_missing_fields(missing)
    if not missing_fields:
        return None
    previous_collection = _slot_collection_from_metadata(request.metadata)
    slot_collection = build_slot_collection(
        request=request,
        manifest=manifest,
        skill_name=skill_name,
        entrypoint=entrypoint,
        missing_fields=missing_fields,
        resolved_payload=resolved_payload or {},
        sources=sources or {},
        previous_collection=previous_collection,
    )
    digest = hashlib.sha256(
        f"{request.node_id}:{skill_name}:{entrypoint}:{slot_collection['round']}:{','.join(missing_fields)}".encode("utf-8")
    ).hexdigest()[:12]
    required_fields = {} if manifest.contract is not None else {
        name: _required_field_payload(name, manifest.parameters.get(name))
        for name in missing_fields
    }
    required_fields[SLOT_COLLECTION_FIELD] = slot_collection
    return Interrupt(
        interrupt_id=f"{request.node_id}:interrupt:skill_input_missing:{digest}",
        conversation_id=request.conversation_id,
        task_id=request.task_id,
        node_id=request.node_id,
        source_agent=f"skill.{skill_name}",
        source_message_id=str(request.input_payload.get("message_id") or request.task_id),
        question=slot_collection["last_question"],
        reason_code=_missing_reason_code(missing_fields),
        required_fields=required_fields,
        status=InterruptStatus.OPEN,
    )


async def build_missing_input_interrupt_with_question(
    *,
    request: CapabilityExecutionRequest,
    manifest: SkillManifest,
    skill_name: str,
    entrypoint: str,
    missing: Iterable[str],
    resolved_payload: Mapping[str, Any] | None = None,
    sources: Mapping[str, Any] | None = None,
    question_text_generator: Any | None = None,
) -> Interrupt | None:
    interrupt = build_missing_input_interrupt(
        request=request,
        manifest=manifest,
        skill_name=skill_name,
        entrypoint=entrypoint,
        missing=missing,
        resolved_payload=resolved_payload,
        sources=sources,
    )
    if interrupt is None or question_text_generator is None:
        return interrupt
    question_payload = await generate_slot_question(
        manifest=manifest,
        slot_collection=interrupt.required_fields[SLOT_COLLECTION_FIELD],
        text_generator=question_text_generator,
        metadata=llm_option_metadata(request.metadata),
    )
    if question_payload is None:
        return interrupt
    slot_collection = dict(interrupt.required_fields[SLOT_COLLECTION_FIELD])
    slot_collection.update(
        {
            "last_question": question_payload["question"],
            "question_source": "llm",
            "ask_fields": list(question_payload["ask_fields"]),
            "answer_hint": question_payload.get("answer_hint"),
            "question_style": question_payload.get("style") or "assistant_dialogue",
        }
    )
    required_fields = dict(interrupt.required_fields)
    required_fields[SLOT_COLLECTION_FIELD] = slot_collection
    return replace(interrupt, question=question_payload["question"], required_fields=required_fields)


async def generate_slot_question(
    *,
    manifest: SkillManifest,
    slot_collection: Mapping[str, Any],
    text_generator: Any,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    missing_fields = set(_clean_missing_fields(slot_collection.get("missing")))
    if not missing_fields:
        return None
    prompt = _slot_question_prompt(manifest=manifest, slot_collection=slot_collection)
    try:
        raw_response = _call_question_text_generator(text_generator, prompt, metadata=metadata)
        if inspect.isawaitable(raw_response):
            raw_response = await raw_response
        parsed = _load_json_object(str(raw_response or ""))
    except Exception:
        return None
    question = str(parsed.get("question") or "").strip()
    if not question or _looks_sensitive(question):
        return None
    ask_fields = _clean_missing_fields(parsed.get("ask_fields"))
    if not ask_fields or not set(ask_fields).issubset(missing_fields):
        return None
    return {
        "question": question[:500],
        "ask_fields": list(ask_fields[:3]),
        "answer_hint": _safe_source_text(parsed.get("answer_hint")),
        "style": _safe_source_text(parsed.get("style")) or "assistant_dialogue",
    }


def _call_question_text_generator(
    text_generator: Any,
    prompt: str,
    *,
    metadata: Mapping[str, Any] | None = None,
):
    kwargs: dict[str, Any] = {}
    if metadata:
        try:
            signature = inspect.signature(text_generator)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
            if accepts_kwargs or "metadata" in signature.parameters:
                kwargs["metadata"] = metadata
    return text_generator(prompt, **kwargs) if kwargs else text_generator(prompt)


def build_slot_collection(
    *,
    request: CapabilityExecutionRequest,
    manifest: SkillManifest,
    skill_name: str,
    entrypoint: str,
    missing_fields: tuple[str, ...],
    resolved_payload: Mapping[str, Any],
    sources: Mapping[str, Any],
    previous_collection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if manifest.contract is not None:
        return _build_v2_slot_collection(
            request=request,
            manifest=manifest,
            skill_name=skill_name,
            entrypoint=entrypoint,
            missing_fields=missing_fields,
            resolved_payload=resolved_payload,
            sources=sources,
            previous_collection=previous_collection,
        )

    previous_slots = _previous_slots(previous_collection)
    resolved: dict[str, Any] = {}
    slots: list[dict[str, Any]] = []
    validation_errors: dict[str, str] = {}
    previous_round = _positive_int(previous_collection.get("round")) if isinstance(previous_collection, Mapping) else 0
    round_number = previous_round + 1 if previous_round else 1

    parameter_names = tuple(dict.fromkeys((*manifest.parameters.keys(), *missing_fields, *previous_slots.keys())))
    for name in parameter_names:
        spec = manifest.parameters.get(name)
        previous = previous_slots.get(name, {})
        value_source = _source_payload(sources.get(name))
        value = _safe_slot_value(resolved_payload.get(name))
        if value is None and previous.get("status") == "resolved":
            value = _safe_slot_value(previous.get("value"))
            if value_source is None:
                value_source = _safe_source_text(previous.get("source"))
        is_missing = name in missing_fields
        status = "missing" if is_missing else ("resolved" if value is not None else str(previous.get("status") or "unknown"))
        validation_error = _slot_validation_error(
            name=name,
            spec=spec,
            request=request,
            is_missing=is_missing,
        )
        if is_missing and validation_error != "missing":
            validation_errors[name] = validation_error
        if status == "resolved" and value is not None:
            resolved[name] = value
        slot = {
            "name": name,
            "label": _field_label(name, spec),
            "type": spec.type if spec is not None else str(previous.get("type") or "string"),
            "required": bool(spec.required if spec is not None else previous.get("required", name in missing_fields)),
            "status": status,
            "value": value if status == "resolved" else None,
            "source": value_source if status == "resolved" else None,
            "description": _field_description(name, spec),
            "aliases": list(spec.aliases) if spec is not None and spec.aliases else list(previous.get("aliases") or ()),
            "examples": _field_examples(name, spec),
            "validation": _field_validation(spec),
            "last_validation_error": validation_error if is_missing else None,
        }
        slots.append(slot)

    collection_id = f"{request.node_id}:slot:{round_number}:{_collection_digest(skill_name, entrypoint, missing_fields, resolved)}"
    missing_labels = [_field_label(field, manifest.parameters.get(field)) for field in missing_fields]
    resolved_labels = [
        slot["label"]
        for slot in slots
        if slot.get("status") == "resolved" and str(slot.get("name") or "") not in missing_fields
    ]
    question = _slot_question(
        manifest=manifest,
        missing_fields=missing_fields,
        missing_labels=tuple(missing_labels),
        resolved_labels=tuple(resolved_labels),
    )
    no_progress_rounds = _positive_int(previous_collection.get("no_progress_rounds")) if isinstance(previous_collection, Mapping) else 0
    if previous_collection and set(_clean_missing_fields(previous_collection.get("missing"))) == set(missing_fields):
        no_progress_rounds += 1
    selected_schema_id = _safe_source_text(resolved_payload.get("_selected_schema_id"))
    selected_entrypoint = _safe_source_text(resolved_payload.get("_selected_entrypoint")) or entrypoint
    invalid = _v2_invalid_fields(resolved_payload)
    schema_version = SLOT_COLLECTION_V2_SCHEMA_VERSION if getattr(manifest, "contract", None) is not None else SLOT_COLLECTION_SCHEMA_VERSION
    return {
        "schema_version": schema_version,
        "collection_id": collection_id,
        "task_id": request.task_id,
        "node_id": request.node_id,
        "capability_id": request.capability_id,
        "skill_name": skill_name,
        "entrypoint": entrypoint,
        "selected_schema_id": selected_schema_id,
        "selected_entrypoint": selected_entrypoint,
        "round": round_number,
        "status": "collecting",
        "slots": slots,
        "resolved": resolved,
        "missing": list(missing_fields),
        "invalid": invalid,
        "resource_hints": _v2_resource_hints(manifest, missing_fields),
        "validation_errors": validation_errors,
        "no_progress_rounds": no_progress_rounds,
        "last_question": question,
        "question_source": "fallback",
    }


def _v2_invalid_fields(resolved_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = resolved_payload.get("_invalid")
    if not isinstance(raw, list | tuple):
        return []
    invalid: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        field = _safe_source_text(item.get("field"))
        reason = _safe_source_text(item.get("reason"))
        if field:
            invalid.append({"field": field, "reason": reason or "invalid"})
    return invalid


def _v2_resource_hints(manifest: SkillManifest, missing_fields: tuple[str, ...]) -> list[dict[str, str]]:
    contract = getattr(manifest, "contract", None)
    if contract is None:
        return []
    hints: list[dict[str, str]] = []
    for field in missing_fields:
        for resource in contract.resources.values():
            if "slot_question" in resource.audience:
                hints.append({"field": field, "resource_id": resource.resource_id, "title": resource.title or resource.resource_id})
                break
    return hints


def _build_v2_slot_collection(
    *,
    request: CapabilityExecutionRequest,
    manifest: SkillManifest,
    skill_name: str,
    entrypoint: str,
    missing_fields: tuple[str, ...],
    resolved_payload: Mapping[str, Any],
    sources: Mapping[str, Any],
    previous_collection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    assert manifest.contract is not None
    selected_schema_id = _safe_source_text(resolved_payload.get("_selected_schema_id"))
    selected_entrypoint = _safe_source_text(resolved_payload.get("_selected_entrypoint")) or entrypoint
    schemas = {}
    try:
        schemas = load_input_schemas_for_contract(manifest.contract)
    except Exception:
        schemas = {}
    selected_schema = schemas.get(selected_schema_id) if selected_schema_id else None
    previous_round = _positive_int(previous_collection.get("round")) if isinstance(previous_collection, Mapping) else 0
    round_number = previous_round + 1 if previous_round else 1
    previous_id = _safe_source_text(previous_collection.get("collection_id")) if isinstance(previous_collection, Mapping) else ""
    kind = "input_collection" if selected_schema is not None else "schema_selection"
    collection_id = previous_id or f"{request.node_id}:slot:{_collection_digest(skill_name, selected_schema_id or 'schema', missing_fields, resolved_payload)}"
    resolved: dict[str, Any] = {}
    slots: dict[str, Any] = {}
    if selected_schema is not None:
        schema_snapshot = build_schema_snapshot(selected_schema, resources=manifest.contract.resources)
        for name, field in selected_schema.inputs.items():
            if not field.expose:
                continue
            value = _safe_slot_value(_resolved_payload_value(resolved_payload, name))
            source_payload = _source_payload(sources.get(name))
            if value is None and field.default is not None and name not in missing_fields:
                value = field.default
                source_payload = "schema_default"
            status = "missing" if name in missing_fields else ("resolved" if value is not None else "optional")
            slot: dict[str, Any] = {
                "name": name,
                "label": field.description or _FIELD_LABELS.get(name, name),
                "type": field.type,
                "required_now": bool(field.required or field.required_when),
                "status": status,
                "source": {"allowed": list(field.source.allowed)},
                "aliases": list(field.aliases),
            }
            if field.const is not None:
                slot["const"] = field.const
            if field.enum:
                slot["enum"] = list(field.enum)
            if field.default is not None:
                slot["default"] = field.default
            if value is not None and status == "resolved":
                resolved[name] = {
                    "raw_value": value,
                    "value": value,
                    "source": source_payload or "payload",
                }
                slot["raw_value"] = value
                slot["value"] = value
            slots[name] = slot
    else:
        selector_field = manifest.contract.schema_selector.selector_field or (missing_fields[0] if missing_fields else "schema")
        allowed = []
        for schema_id, ref in manifest.contract.input_schemas.items():
            allowed.append(
                {
                    "schema_id": schema_id,
                    "title": ref.title or schema_id,
                    "description": ref.description,
                    "aliases": list(ref.aliases),
                }
            )
        schema_snapshot = {
            "kind": "schema_selection",
            "selector_field": selector_field,
            "allowed_schemas": allowed,
        }
        slots[selector_field] = {
            "name": selector_field,
            "label": _FIELD_LABELS.get(selector_field, selector_field),
            "type": "string",
            "required_now": True,
            "status": "missing",
            "allowed_schemas": allowed,
        }
    invalid = _v2_invalid_fields(resolved_payload)
    question = _v2_slot_question(
        schema_snapshot=schema_snapshot,
        missing_fields=missing_fields,
        invalid=invalid,
        kind=kind,
    )
    return {
        "schema_version": SLOT_COLLECTION_V2_SCHEMA_VERSION,
        "collection_id": collection_id,
        "task_id": request.task_id,
        "node_id": request.node_id,
        "conversation_id": request.conversation_id,
        "capability_id": request.capability_id,
        "skill_name": skill_name,
        "kind": kind,
        "status": "waiting_for_user",
        "entrypoint": entrypoint,
        "selected_schema_id": selected_schema_id,
        "selected_entrypoint": selected_entrypoint,
        "skill_bundle_revision": _safe_source_text(request.metadata.get("skill_bundle_revision")),
        "contract_revision": _safe_source_text(request.metadata.get("contract_revision")),
        "schema_digest": _schema_snapshot_digest(schema_snapshot),
        "schema_snapshot": schema_snapshot,
        "round": round_number,
        "revision": _positive_int(previous_collection.get("revision")) if isinstance(previous_collection, Mapping) else 0,
        "slots": slots,
        "resolved": resolved,
        "missing": list(missing_fields),
        "invalid": invalid,
        "validation_errors": {str(item.get("field")): str(item.get("reason")) for item in invalid if isinstance(item, Mapping) and item.get("field")},
        "resource_hints": _v2_resource_hints(manifest, missing_fields),
        "last_question": question,
        "question_source": "schema_snapshot",
    }


def _resolved_payload_value(payload: Mapping[str, Any], name: str) -> Any:
    if name in payload:
        return payload[name]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping) and name in metadata:
        return metadata[name]
    return None


def _v2_slot_question(
    *,
    schema_snapshot: Mapping[str, Any],
    missing_fields: tuple[str, ...],
    invalid: list[dict[str, str]],
    kind: str,
) -> str:
    if kind == "schema_selection":
        allowed = schema_snapshot.get("allowed_schemas") if isinstance(schema_snapshot, Mapping) else ()
        titles = []
        if isinstance(allowed, list | tuple):
            titles = [str(item.get("title") or item.get("schema_id")) for item in allowed if isinstance(item, Mapping)]
        choices = " / ".join(title for title in titles if title)
        return f"请确认要使用哪一种设计类型：{choices}。" if choices else "请确认要使用哪一种输入模式。"
    inputs = schema_snapshot.get("inputs") if isinstance(schema_snapshot, Mapping) else {}
    labels = []
    for field in missing_fields:
        field_snapshot = inputs.get(field) if isinstance(inputs, Mapping) else None
        if isinstance(field_snapshot, Mapping):
            labels.append(str(field_snapshot.get("description") or field_snapshot.get("name") or field))
        else:
            labels.append(_FIELD_LABELS.get(field, field))
    if invalid:
        invalid_labels = [str(item.get("field")) for item in invalid if item.get("field")]
        if invalid_labels:
            return f"刚才的 {', '.join(invalid_labels)} 无法通过校验，请重新补充：{ '、'.join(labels) }。"
    return f"请补充：{'、'.join(labels)}。" if labels else "请补充缺失参数。"


def slot_collection_from_required_fields(required_fields: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = required_fields.get(SLOT_COLLECTION_FIELD)
    return value if isinstance(value, Mapping) else None


def slot_collection_ref_from_required_fields(required_fields: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = required_fields.get(SLOT_COLLECTION_REF_FIELD)
    return value if isinstance(value, Mapping) else None


def slot_collection_required_fields_ref(collection: SlotCollection) -> dict[str, Any]:
    slots = collection.slots if isinstance(collection.slots, Mapping) else {}
    slot_summaries = []
    for name, slot in slots.items():
        if isinstance(slot, Mapping):
            slot_summaries.append(
                {
                    "name": str(slot.get("name") or name),
                    "label": str(slot.get("label") or slot.get("description") or name),
                    "type": str(slot.get("type") or "string"),
                    "status": str(slot.get("status") or ""),
                    "required_now": bool(slot.get("required_now", slot.get("required", False))),
                    **({"validation_error": dict(slot["validation_error"])} if isinstance(slot.get("validation_error"), Mapping) else {}),
                }
            )
    return {
        SLOT_COLLECTION_REF_FIELD: {
            "schema_version": SLOT_COLLECTION_V2_SCHEMA_VERSION,
            "collection_id": collection.collection_id,
            "task_id": collection.task_id,
            "node_id": collection.node_id,
            "kind": collection.kind,
            "status": collection.status,
            "round": collection.round,
            "revision": collection.revision,
            "selected_schema_id": collection.selected_schema_id,
            "selected_entrypoint": collection.selected_entrypoint,
            "missing": list(collection.missing),
            "invalid": [dict(item) for item in collection.invalid],
            "last_question": collection.last_question,
            "slots": slot_summaries,
        }
    }


def slot_collection_model_from_carrier(carrier: Mapping[str, Any], *, now: Any | None = None) -> SlotCollection:
    slots = carrier.get("slots") if isinstance(carrier.get("slots"), Mapping) else {}
    resolved = carrier.get("resolved") if isinstance(carrier.get("resolved"), Mapping) else {}
    invalid_raw = carrier.get("invalid") if isinstance(carrier.get("invalid"), list | tuple) else ()
    return SlotCollection(
        collection_id=str(carrier.get("collection_id") or ""),
        task_id=str(carrier.get("task_id") or ""),
        node_id=str(carrier.get("node_id") or ""),
        conversation_id=str(carrier.get("conversation_id") or ""),
        capability_id=str(carrier.get("capability_id") or ""),
        skill_name=str(carrier.get("skill_name") or ""),
        kind=str(carrier.get("kind") or "input_collection"),
        status=str(carrier.get("status") or "waiting_for_user"),
        round=_positive_int(carrier.get("round")) or 1,
        revision=_positive_int(carrier.get("revision")) or 0,
        selected_schema_id=_safe_source_text(carrier.get("selected_schema_id")) or None,
        selected_entrypoint=_safe_source_text(carrier.get("selected_entrypoint")) or _safe_source_text(carrier.get("entrypoint")) or None,
        skill_bundle_revision=_safe_source_text(carrier.get("skill_bundle_revision")) or None,
        contract_revision=_safe_source_text(carrier.get("contract_revision")) or None,
        schema_digest=_safe_source_text(carrier.get("schema_digest")) or None,
        schema_snapshot=redact_prompt_safe(carrier.get("schema_snapshot") if isinstance(carrier.get("schema_snapshot"), Mapping) else {}),
        slots=redact_prompt_safe(slots),
        resolved=redact_prompt_safe(resolved),
        missing=_clean_missing_fields(carrier.get("missing")),
        invalid=tuple(dict(item) for item in invalid_raw if isinstance(item, Mapping)),
        last_question=_safe_source_text(carrier.get("last_question")) or None,
        created_at=now,
        updated_at=now,
    )


def slot_collection_bootstrap_events(collection: SlotCollection, *, now: Any | None = None) -> tuple[SlotEvent, ...]:
    started = SlotEvent(
        slot_event_id=f"{collection.collection_id}:event:000:started",
        collection_id=collection.collection_id,
        task_id=collection.task_id,
        node_id=collection.node_id,
        conversation_id=collection.conversation_id,
        event_type="slot.collection_started",
        round=collection.round,
        revision=collection.revision,
        idempotency_key=f"slot:{collection.collection_id}:started",
        payload={"kind": collection.kind, "missing": list(collection.missing)},
        created_at=now,
    )
    prompt = SlotEvent(
        slot_event_id=f"{collection.collection_id}:event:001:prompt:{collection.round}",
        collection_id=collection.collection_id,
        task_id=collection.task_id,
        node_id=collection.node_id,
        conversation_id=collection.conversation_id,
        event_type="slot.prompt_generated",
        round=collection.round,
        revision=collection.revision,
        idempotency_key=f"slot:{collection.collection_id}:prompt:{collection.round}",
        payload={"question": collection.last_question, "missing": list(collection.missing)},
        created_at=now,
    )
    return (started, prompt)


def slot_collection_event_payload(required_fields: Mapping[str, Any]) -> dict[str, Any]:
    collection = slot_collection_ref_from_required_fields(required_fields) or slot_collection_from_required_fields(required_fields)
    if collection is None:
        return {}
    payload: dict[str, Any] = {}
    for key in ("collection_id", "round", "revision", "missing", "question_source"):
        value = collection.get(key)
        if value is not None:
            output_key = "slot_collection_id" if key == "collection_id" else key
            payload[output_key] = list(value) if isinstance(value, tuple) else value
    if collection.get("kind"):
        payload["kind"] = collection.get("kind")
    if collection.get("selected_schema_id"):
        payload["selected_schema_id"] = collection.get("selected_schema_id")
    validation_errors = collection.get("validation_errors")
    if isinstance(validation_errors, Mapping) and validation_errors:
        payload["validation_errors"] = {
            str(key): str(value)
            for key, value in validation_errors.items()
            if str(key).strip() and str(value).strip()
        }
    return payload


def _slot_collection_from_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = metadata.get(SLOT_COLLECTION_METADATA_KEY)
    return value if isinstance(value, Mapping) else None


def _previous_slots(previous_collection: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(previous_collection, Mapping):
        return {}
    slots = previous_collection.get("slots")
    if not isinstance(slots, Iterable) or isinstance(slots, str | bytes):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for raw_slot in slots:
        if not isinstance(raw_slot, Mapping):
            continue
        name = str(raw_slot.get("name") or "").strip()
        if name:
            result[name] = raw_slot
    return result


def _clean_missing_fields(raw_missing: Any) -> tuple[str, ...]:
    if raw_missing is None:
        values: Iterable[Any] = ()
    elif isinstance(raw_missing, str):
        values = (raw_missing,)
    elif isinstance(raw_missing, Iterable):
        values = raw_missing
    else:
        values = (raw_missing,)
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _missing_reason_code(missing_fields: tuple[str, ...]) -> str:
    if len(missing_fields) == 1:
        return f"missing_{missing_fields[0]}"
    return "missing_skill_input"


def _slot_validation_error(
    *,
    name: str,
    spec: SkillParameterSpec | None,
    request: CapabilityExecutionRequest,
    is_missing: bool,
) -> str | None:
    if not is_missing:
        return None
    if not _metadata_has_field_attempt(request.metadata, name, spec):
        return "missing"
    field_type = spec.type if spec is not None else ""
    if name in _ARTIFACT_FIELDS or field_type in {"artifact", "file", "data"}:
        return "invalid_artifact_source"
    return "invalid_value"


def _metadata_has_field_attempt(metadata: Mapping[str, Any], name: str, spec: SkillParameterSpec | None) -> bool:
    candidate_keys = (name, *(spec.aliases if spec is not None else ()))
    return any(str(key) in metadata for key in candidate_keys)


def _slot_question_prompt(*, manifest: SkillManifest, slot_collection: Mapping[str, Any]) -> str:
    safe_slots = []
    for raw_slot in slot_collection.get("slots") or ():
        if not isinstance(raw_slot, Mapping):
            continue
        safe_slots.append(
            {
                key: raw_slot.get(key)
                for key in (
                    "name",
                    "label",
                    "type",
                    "required",
                    "status",
                    "description",
                    "aliases",
                    "examples",
                    "validation",
                    "last_validation_error",
                )
            }
        )
    payload = {
        "skill": {
            "name": manifest.name,
            "display_name": manifest.metadata.get("display_name"),
            "description": manifest.description,
        },
        "slot_collection": {
            "schema_version": slot_collection.get("schema_version"),
            "round": slot_collection.get("round"),
            "missing": list(_clean_missing_fields(slot_collection.get("missing"))),
            "resolved": _safe_slot_value(slot_collection.get("resolved")) or {},
            "slots": safe_slots,
            "no_progress_rounds": slot_collection.get("no_progress_rounds"),
        },
    }
    return (
        "你是一个受限的 Skill 补槽追问生成器。根据 slot_collection 生成一次自然语言追问。"
        "只能询问 missing 中的字段；不要提内部脚本、handler、JSON key、数据库、token 或文件原文。"
        "如果已有 resolved 字段，可以先简短确认已收到，再只追问仍缺字段。"
        "只返回 JSON 对象，不要 Markdown。格式："
        '{"question":"给用户看的问题","ask_fields":["missing字段名"],"answer_hint":"可选回答提示","style":"assistant_dialogue"}'
        "\n输入如下：\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}"
    )


def _load_json_object(text: str) -> Mapping[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise json.JSONDecodeError("empty response", text, 0)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, Mapping):
        raise json.JSONDecodeError("response is not a JSON object", stripped, 0)
    return parsed


def _required_field_payload(name: str, spec: SkillParameterSpec | None) -> dict[str, Any]:
    field_type = spec.type if spec is not None else "string"
    payload: dict[str, Any] = {
        "type": field_type,
        "label": _field_label(name, spec),
        "description": _field_description(name, spec),
    }
    if spec is not None and spec.aliases:
        payload["aliases"] = list(spec.aliases)
    examples = _field_examples(name, spec)
    if examples:
        payload["examples"] = examples
    if name in _ARTIFACT_FIELDS or field_type in {"artifact", "file", "data"}:
        payload["accepts_upload"] = True
    return payload


def _field_label(name: str, spec: SkillParameterSpec | None) -> str:
    if name in _FIELD_LABELS:
        return _FIELD_LABELS[name]
    if spec is not None and spec.aliases:
        return str(spec.aliases[0])
    return name


def _field_description(name: str, spec: SkillParameterSpec | None) -> str:
    if name in _FIELD_DESCRIPTIONS:
        return _FIELD_DESCRIPTIONS[name]
    if spec is not None and spec.enum:
        return f"请补充 {name}，可选值：{', '.join(spec.enum)}。"
    return f"请补充 {name}。"


def _field_examples(name: str, spec: SkillParameterSpec | None) -> list[str]:
    if spec is not None and spec.enum:
        return list(spec.enum[:3])
    if spec is not None and spec.type in {"int", "integer"}:
        return ["3"]
    if name in _ARTIFACT_FIELDS or (spec is not None and spec.type in {"artifact", "file", "data"}):
        return ["上传文件"]
    return []


def _field_validation(spec: SkillParameterSpec | None) -> dict[str, Any]:
    if spec is None:
        return {}
    validation: dict[str, Any] = {"type": spec.type}
    if spec.type in {"int", "integer"}:
        validation["positive_integer"] = True
    if spec.enum:
        validation["enum"] = list(spec.enum)
    return validation


def _slot_question(
    *,
    manifest: SkillManifest,
    missing_fields: tuple[str, ...],
    missing_labels: tuple[str, ...],
    resolved_labels: tuple[str, ...],
) -> str:
    skill_label = str(manifest.metadata.get("display_name") or manifest.name).strip() or manifest.name
    missing = "、".join(missing_labels) or "必需信息"
    prefix = f"{'、'.join(resolved_labels[:3])}已收到。" if resolved_labels else ""
    if any(field in _ARTIFACT_FIELDS or (manifest.parameters.get(field) is not None and manifest.parameters[field].type in {"artifact", "file", "data"}) for field in missing_fields):
        return f"{prefix}{skill_label} 还差 {missing}。请通过上传入口补充对应文件后继续。"
    examples = []
    for field in missing_fields:
        examples.extend(_field_examples(field, manifest.parameters.get(field)))
    hint = f"例如可以回复：{examples[0]}。" if examples else "请直接回复这些信息。"
    return f"{prefix}{skill_label} 还差 {missing}。{hint}"


def _safe_slot_value(value: Any) -> Any | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str | int | float):
        text = str(value)
        if _looks_sensitive(text):
            return None
        return value
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key.lower() in _SENSITIVE_SLOT_KEYS:
                continue
            if isinstance(raw_value, str | int | float | bool) or raw_value is None:
                if isinstance(raw_value, str) and _looks_sensitive(raw_value):
                    continue
                safe[key] = raw_value
            elif isinstance(raw_value, list | tuple):
                safe[key] = [
                    item
                    for item in raw_value[:20]
                    if isinstance(item, str | int | float | bool) or item is None
                ]
        if safe.get("available") is True:
            artifact_value: dict[str, Any] = {"available": True}
            for key in ("count", "upload_ids", "filename", "filenames", "selected_sheet"):
                if key in safe:
                    artifact_value[key] = safe[key]
            return artifact_value
        return safe or None
    return None


def _source_payload(source: Any) -> str | None:
    raw = getattr(source, "source", source)
    return _safe_source_text(raw)


def _safe_source_text(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text or _looks_sensitive(text):
        return None
    return text[:80]


def _looks_sensitive(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in ("bearer ", "password", "secret", "token", "cookie", "postgresql://", "mysql://"))


def _positive_int(value: Any) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _collection_digest(
    skill_name: str,
    entrypoint: str,
    missing_fields: tuple[str, ...],
    resolved: Mapping[str, Any],
) -> str:
    return hashlib.sha256(
        repr((skill_name, entrypoint, missing_fields, sorted(resolved))).encode("utf-8")
    ).hexdigest()[:10]


def _schema_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    rendered = json.dumps(dict(snapshot), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
