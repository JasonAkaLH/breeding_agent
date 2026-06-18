from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.orchestration.models import CapabilityDescriptor

from .input_schema import load_input_schemas_for_contract
from .manifest import SkillManifest

_FORBIDDEN_KEY_PARTS = (
    "source_path", "script", "handler", "handler_module", "handler_factory", "runtime", "sidecar", "endpoint",
    "base_url", "url", "dsn", "token", "secret", "password", "api_key", "authorization", "config", "module", "path", "sql",
)
_FORBIDDEN_TEXT_PARTS = (
    "scripts/", "runtime/", "python_subprocess", "platform_service", "handler", "config.yaml", "mysql://",
    "postgresql://", "api_key", "token", "secret",
)


@dataclass(slots=True, frozen=True)
class PublicSkillProfile:
    capability_id: str
    name: str
    display_name: str
    description: str
    triggers: tuple[str, ...]
    parameters: tuple[dict[str, Any], ...] = ()
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    public_usage: dict[str, Any] | None = None
    resource_index: tuple[dict[str, Any], ...] = ()
    schema_summaries: tuple[dict[str, Any], ...] = ()
    routing_examples: tuple[str, ...] = ()
    file_intent: dict[str, Any] | None = None
    file_selection_summaries: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "triggers": list(self.triggers),
            "parameters": list(self.parameters),
            "inputs": self.inputs or {},
            "outputs": self.outputs or {},
            "public_usage": self.public_usage or {},
            "resource_index": list(self.resource_index),
            "schema_summaries": list(self.schema_summaries),
            "routing_examples": list(self.routing_examples),
            "file_intent": self.file_intent or {},
            "file_selection_summaries": list(self.file_selection_summaries),
        }


def build_public_skill_profile(
    manifest: SkillManifest,
    *,
    capability_id: str,
    descriptor: CapabilityDescriptor | None = None,
) -> PublicSkillProfile:
    if manifest.contract is not None:
        contract = manifest.contract
        resolved_capability_id = contract.capability.id
        display_name = _public_text(contract.capability.display_name, fallback=manifest.name)
        description = _public_text(contract.capability.description or manifest.description)
        file_intent = _public_file_intent(contract)
        file_selection_summaries = _public_file_selection_summaries(contract)
        return PublicSkillProfile(
            capability_id=resolved_capability_id,
            name=_public_text(manifest.name, fallback=resolved_capability_id),
            display_name=display_name,
            description=description,
            triggers=_public_text_tuple((*contract.routing.triggers, *contract.routing.intent_aliases)),
            parameters=(),
            inputs={
                "schema_count": len(contract.input_schemas),
                **({"file_intent": file_intent} if file_intent else {}),
                **({"file_selection": list(file_selection_summaries)} if file_selection_summaries else {}),
            },
            outputs={"output_contracts": sorted(contract.outputs)},
            public_usage={},
            resource_index=tuple(
                {
                    "resource_id": resource.resource_id,
                    "title": _public_text(resource.title, fallback=resource.resource_id),
                    "description": _public_text(resource.description),
                    "audience": [item for item in resource.audience if item in {"main_agent", "slot_question"}],
                }
                for resource in contract.resources.values()
                if any(item in {"main_agent", "slot_question"} for item in resource.audience)
            ),
            schema_summaries=tuple(
                {
                    "schema_id": ref.schema_id,
                    "title": _public_text(ref.title, fallback=ref.schema_id),
                    "description": _public_text(ref.description),
                    "aliases": list(_public_text_tuple(ref.aliases)),
                }
                for ref in contract.input_schemas.values()
            ),
            routing_examples=_public_text_tuple(contract.routing.examples),
            file_intent=file_intent,
            file_selection_summaries=file_selection_summaries,
        )

    # Historical fallback for non-contract tests/fixtures. Public registry no longer uses this path.
    name = _public_text(manifest.name, fallback=capability_id)
    raw_display_name = (descriptor.display_name if descriptor else "") or str(manifest.metadata.get("display_name") or "").strip()
    display_name = _public_text(raw_display_name, fallback=name)
    raw_description = (descriptor.description if descriptor else "") or manifest.description
    public_usage = _sanitize_public_value(manifest.metadata.get("public_usage"))
    if not isinstance(public_usage, Mapping):
        public_usage = {}
    parameters = _public_parameters(manifest.metadata.get("parameters"))
    return PublicSkillProfile(
        capability_id=capability_id,
        name=name,
        display_name=display_name,
        description=_public_text(raw_description),
        triggers=_public_text_tuple(manifest.triggers),
        parameters=parameters,
        inputs={},
        outputs={},
        public_usage=dict(public_usage),
    )


def _public_file_intent(contract: Any) -> dict[str, Any]:
    file_intent = getattr(contract, "file_intent", None)
    if file_intent is None:
        return {}
    payload = {
        "requires_file": bool(getattr(file_intent, "requires_file", False)),
        "default_allow_multiple": bool(getattr(file_intent, "default_allow_multiple", False)),
        "supported_file_types": list(_public_text_tuple(getattr(file_intent, "supported_file_types", ()))),
        "description": _public_text(getattr(file_intent, "description", "")),
    }
    return {key: value for key, value in payload.items() if value not in (False, "", [], {})}


def _public_file_selection_summaries(contract: Any) -> tuple[dict[str, Any], ...]:
    try:
        schemas = load_input_schemas_for_contract(contract)
    except Exception:
        return ()
    summaries: list[dict[str, Any]] = []
    for schema in schemas.values():
        for input_field in schema.inputs.values():
            field_selection = input_field.file_selection
            is_file_field = input_field.type in {"artifact", "file", "data"}
            has_selection_metadata = any((
                field_selection.required,
                field_selection.allow_multiple,
                field_selection.expected_content,
                field_selection.supported_file_types,
                field_selection.helpful_columns,
                field_selection.disambiguation_hint,
            ))
            if not (is_file_field or has_selection_metadata):
                continue
            summary = {
                "schema_id": schema.schema_id,
                "field": input_field.name,
                "type": input_field.type,
                "required": bool(input_field.required or field_selection.required),
                "allow_multiple": bool(field_selection.allow_multiple),
                "expected_content": list(_public_text_tuple(field_selection.expected_content)),
                "supported_file_types": list(_public_text_tuple(field_selection.supported_file_types)),
                "helpful_columns": list(_public_text_tuple(field_selection.helpful_columns)),
                "disambiguation_hint": _public_text(field_selection.disambiguation_hint),
            }
            if input_field.title:
                summary["title"] = _public_text(input_field.title)
            if input_field.description:
                summary["description"] = _public_text(input_field.description)
            summaries.append({key: value for key, value in summary.items() if value not in (False, "", [], {})})
    return tuple(summaries)

def _public_text(value: Any, *, fallback: str = "") -> str:
    sanitized = _sanitize_public_value(value)
    if isinstance(sanitized, str) and sanitized:
        return sanitized
    return fallback


def _public_text_tuple(value: Any) -> tuple[str, ...]:
    sanitized = _sanitize_public_value(list(value) if isinstance(value, tuple) else value)
    if not isinstance(sanitized, list | tuple):
        return ()
    return tuple(str(item).strip() for item in sanitized if isinstance(item, str) and str(item).strip())


def _public_parameters(value: Any) -> tuple[dict[str, Any], ...]:
    sanitized = _sanitize_public_value(value)
    if not isinstance(sanitized, Mapping):
        return ()
    parameters: list[dict[str, Any]] = []
    for name, spec in sanitized.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(spec, Mapping):
            item = {"name": name.strip(), **dict(spec)}
        else:
            item = {"name": name.strip(), "description": spec}
        parameters.append(item)
    return tuple(parameters)


def _sanitize_public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key).strip()
            if not key_text or _is_forbidden_key(key_text):
                continue
            safe_child = _sanitize_public_value(child)
            if safe_child not in (None, "", [], {}):
                sanitized[key_text] = safe_child
        return sanitized
    if isinstance(value, list | tuple):
        sanitized_items = [_sanitize_public_value(item) for item in value]
        return [item for item in sanitized_items if item not in (None, "", [], {})]
    if isinstance(value, str):
        if _contains_forbidden_text(value):
            return None
        return value.strip()
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value).strip()
    if _contains_forbidden_text(text):
        return None
    return text


def _is_forbidden_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _FORBIDDEN_KEY_PARTS)


def _contains_forbidden_text(text: str) -> bool:
    normalized = text.lower()
    return any(part in normalized for part in _FORBIDDEN_TEXT_PARTS)
