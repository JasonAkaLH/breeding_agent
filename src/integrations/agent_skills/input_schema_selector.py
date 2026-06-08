from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contract import SkillContract
from .input_schema import SkillInputSchema

SchemaSelectorTextGenerator = Callable[..., Any]


@dataclass(slots=True, frozen=True)
class SkillSchemaSelectionResult:
    selected_schema_id: str = ""
    selected_entrypoint: str = ""
    status: str = "missing"
    reason: str = ""
    missing_selector_field: str = ""
    confidence: float = 0.0

    @property
    def selected(self) -> bool:
        return self.status == "selected" and bool(self.selected_schema_id)


def select_input_schema(
    contract: SkillContract,
    schemas: Mapping[str, SkillInputSchema],
    *,
    query: str,
    payload: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    artifact_summaries: tuple[Mapping[str, Any], ...] = (),
    llm_text_generator: SchemaSelectorTextGenerator | None = None,
) -> SkillSchemaSelectionResult:
    metadata = metadata or {}
    pinned = _pinned_schema_id(payload or {}, metadata)
    if pinned:
        if pinned in schemas:
            return _selected(contract, pinned, confidence=1.0, reason="resume_pinned")
        return SkillSchemaSelectionResult(status="missing", reason="pinned_schema_not_allowed", missing_selector_field=_selector_field(contract))
    if len(schemas) == 1 or (not schemas and len(contract.input_schemas) == 1):
        schema_id = next(iter(schemas or contract.input_schemas))
        return _selected(contract, schema_id, confidence=1.0, reason="single_schema")
    strong_deterministic = _deterministic_candidates_from_parts(
        contract,
        schemas,
        haystack_parts=_strong_selector_parts(query=query, payload=payload or {}),
    )
    if len(strong_deterministic) == 1:
        return _selected(contract, strong_deterministic[0], confidence=1.0, reason="deterministic_alias")
    if len(strong_deterministic) > 1:
        if contract.schema_selector.strategy == "deterministic_then_llm" and llm_text_generator is not None:
            llm_result = _llm_candidate(contract, schemas, query, payload or {}, artifact_summaries, llm_text_generator)
            if llm_result.selected or llm_result.reason.startswith("llm_"):
                return llm_result
        return SkillSchemaSelectionResult(status="missing", reason="ambiguous", missing_selector_field=_selector_field(contract))
    weak_artifact_deterministic = _deterministic_candidates_from_parts(
        contract,
        schemas,
        haystack_parts=_weak_artifact_selector_parts(artifact_summaries),
    )
    if len(weak_artifact_deterministic) == 1:
        return _selected(contract, weak_artifact_deterministic[0], confidence=0.6, reason="deterministic_artifact_alias")
    if len(weak_artifact_deterministic) > 1:
        if contract.schema_selector.strategy == "deterministic_then_llm" and llm_text_generator is not None:
            llm_result = _llm_candidate(contract, schemas, query, payload or {}, artifact_summaries, llm_text_generator)
            if llm_result.selected or llm_result.reason.startswith("llm_"):
                return llm_result
        return SkillSchemaSelectionResult(status="missing", reason="ambiguous", missing_selector_field=_selector_field(contract))
    if contract.schema_selector.strategy == "deterministic_then_llm" and llm_text_generator is not None:
        llm_result = _llm_candidate(contract, schemas, query, payload or {}, artifact_summaries, llm_text_generator)
        if llm_result.selected or llm_result.reason.startswith("llm_"):
            return llm_result
    return SkillSchemaSelectionResult(status="missing", reason="ambiguous", missing_selector_field=_selector_field(contract))


def _deterministic_candidates(contract: SkillContract, schemas: Mapping[str, SkillInputSchema], *, query: str, payload: Mapping[str, Any], artifact_summaries: tuple[Mapping[str, Any], ...]) -> tuple[str, ...]:
    haystack_parts = [*_strong_selector_parts(query=query, payload=payload), *_weak_artifact_selector_parts(artifact_summaries)]
    return _deterministic_candidates_from_parts(contract, schemas, haystack_parts=haystack_parts)


def _strong_selector_parts(*, query: str, payload: Mapping[str, Any]) -> tuple[str, ...]:
    parts = [query]
    parts.extend(str(v) for v in payload.values() if isinstance(v, str | int | float))
    return tuple(parts)


def _weak_artifact_selector_parts(artifact_summaries: tuple[Mapping[str, Any], ...]) -> tuple[str, ...]:
    parts: list[str] = []
    for artifact in artifact_summaries:
        parts.extend(str(v) for v in artifact.values() if isinstance(v, str))
        preview = artifact.get("preview")
        if isinstance(preview, Mapping):
            parts.extend(str(v) for v in preview.values() if isinstance(v, str))
            columns = preview.get("columns")
            if isinstance(columns, list | tuple):
                parts.extend(str(item) for item in columns if isinstance(item, str))
    return tuple(parts)


def _deterministic_candidates_from_parts(contract: SkillContract, schemas: Mapping[str, SkillInputSchema], *, haystack_parts: tuple[str, ...]) -> tuple[str, ...]:
    haystack = "\n".join(haystack_parts).lower()
    if not haystack.strip():
        return ()
    selected: list[str] = []
    for schema_id in schemas or contract.input_schemas:
        for alias in _schema_aliases(contract, schemas, schema_id):
            alias_text = str(alias or "").strip().lower()
            if alias_text and re.search(re.escape(alias_text), haystack):
                selected.append(schema_id)
                break
    return tuple(dict.fromkeys(selected))


def _schema_aliases(contract: SkillContract, schemas: Mapping[str, SkillInputSchema], schema_id: str) -> tuple[str, ...]:
    ref = contract.input_schemas.get(schema_id)
    schema = schemas.get(schema_id)
    aliases: list[str] = []
    if ref is not None:
        aliases.extend(ref.aliases)
        aliases.extend([ref.title, ref.description])
    if schema is not None:
        aliases.extend([schema.title, schema.description])
        applies_aliases = schema.applies_when.get("aliases") if isinstance(schema.applies_when, Mapping) else None
        if isinstance(applies_aliases, list | tuple | set):
            aliases.extend(str(item) for item in applies_aliases)
    aliases.append(schema_id)
    return tuple(str(item) for item in aliases if str(item or "").strip())


def _llm_candidate(contract: SkillContract, schemas: Mapping[str, SkillInputSchema], query: str, payload: Mapping[str, Any], artifact_summaries: tuple[Mapping[str, Any], ...], generator: SchemaSelectorTextGenerator) -> SkillSchemaSelectionResult:
    prompt_payload = {
        "query": query,
        "payload_keys": sorted(str(key) for key in payload),
        "artifact_summaries": [dict(item) for item in artifact_summaries],
        "allowed_schema_ids": sorted(schemas or contract.input_schemas),
        "schema_options": {
            schema_id: {
                "title": (schemas.get(schema_id).title if schemas.get(schema_id) is not None else ""),
                "description": (schemas.get(schema_id).description if schemas.get(schema_id) is not None else ""),
                "aliases": list(_schema_aliases(contract, schemas, schema_id)),
            }
            for schema_id in sorted(schemas or contract.input_schemas)
        },
    }
    prompt = "Select one input schema. Return JSON only: {\"schema_id\": str, \"confidence\": number, \"reason\": str}\n" + json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)
    try:
        raw = generator(prompt)
        if inspect.isawaitable(raw):
            raise TypeError("async selector generator is not supported by sync selector")
        parsed = json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return SkillSchemaSelectionResult(status="missing", reason="llm_invalid_json", missing_selector_field=_selector_field(contract))
    except Exception:
        return SkillSchemaSelectionResult(status="missing", reason="llm_failed", missing_selector_field=_selector_field(contract))
    if not isinstance(parsed, Mapping):
        return SkillSchemaSelectionResult(status="missing", reason="llm_invalid_json", missing_selector_field=_selector_field(contract))
    schema_id = str(parsed.get("schema_id") or "").strip()
    try:
        confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if schema_id not in (schemas or contract.input_schemas):
        return SkillSchemaSelectionResult(status="missing", reason="llm_schema_not_allowed", missing_selector_field=_selector_field(contract), confidence=confidence)
    if confidence < contract.schema_selector.min_confidence:
        return SkillSchemaSelectionResult(status="missing", reason="llm_low_confidence", missing_selector_field=_selector_field(contract), confidence=confidence)
    return _selected(contract, schema_id, confidence=confidence, reason="llm")


def _pinned_schema_id(payload: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    for source in (payload, metadata):
        value = source.get("selected_schema_id") or source.get("skill_selected_schema_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        collection = source.get("skill_slot_collection") or source.get("_slot_collection")
        if isinstance(collection, Mapping):
            selected = collection.get("selected_schema_id")
            if isinstance(selected, str) and selected.strip():
                return selected.strip()
    return ""


def _selected(contract: SkillContract, schema_id: str, *, confidence: float, reason: str) -> SkillSchemaSelectionResult:
    ref = contract.input_schemas.get(schema_id)
    entrypoint = ref.entrypoint if ref and ref.entrypoint else ""
    if not entrypoint:
        for candidate in contract.entrypoints.values():
            if candidate.input_schema == schema_id:
                entrypoint = candidate.name
                break
    if not entrypoint and len(contract.entrypoints) == 1:
        entrypoint = next(iter(contract.entrypoints))
    return SkillSchemaSelectionResult(selected_schema_id=schema_id, selected_entrypoint=entrypoint, status="selected", reason=reason, confidence=confidence)


def _selector_field(contract: SkillContract) -> str:
    return contract.schema_selector.selector_field or "schema"
