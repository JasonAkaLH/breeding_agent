from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from src.orchestration.models import CapabilityDescriptor

from .input_schema import load_input_schemas_for_contract
from .manifest import SkillManifest

_FORBIDDEN_KEY_PARTS = (
    "source_path", "script", "handler", "handler_module", "handler_factory", "runtime", "sidecar", "endpoint",
    "base_url", "url", "dsn", "token", "secret", "password", "api_key", "authorization", "config", "module", "path", "storage", "sql",
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
    file_selection: dict[str, Any] | None = None
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
            "file_selection": self.file_selection or {},
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
        if capability_id != resolved_capability_id:
            raise ValueError("public_skill_profile_capability_mismatch")
        schemas = load_input_schemas_for_contract(contract)
        display_name = _public_text(contract.capability.display_name, fallback=manifest.name)
        description = _public_text(contract.capability.description or manifest.description)
        file_selection = _public_file_selection(contract)
        file_selection_summaries = _public_file_selection_summaries(schemas)
        return PublicSkillProfile(
            capability_id=resolved_capability_id,
            name=_public_text(manifest.name, fallback=resolved_capability_id),
            display_name=display_name,
            description=description,
            triggers=_public_text_tuple((*contract.routing.triggers, *contract.routing.intent_aliases)),
            parameters=(),
            inputs={
                "schema_count": len(contract.input_schemas),
                **({"file_selection": file_selection} if file_selection else {}),
                **({"file_selection_summaries": list(file_selection_summaries)} if file_selection_summaries else {}),
            },
            outputs={"output_contracts": _public_output_contracts(contract)},
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
            schema_summaries=_public_schema_summaries(contract, schemas),
            routing_examples=_public_text_tuple(contract.routing.examples),
            file_selection=file_selection,
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


def _public_file_selection(contract: Any) -> dict[str, Any]:
    file_selection = getattr(contract, "file_selection", None)
    if file_selection is None:
        return {}
    payload = {
        "required": bool(getattr(file_selection, "required", False)),
        "allow_multiple": bool(getattr(file_selection, "allow_multiple", False)),
        "expected_content": list(_public_text_tuple(getattr(file_selection, "expected_content", ()))),
        "supported_file_types": list(_public_text_tuple(getattr(file_selection, "supported_file_types", ()))),
        "helpful_columns": list(_public_text_tuple(getattr(file_selection, "helpful_columns", ()))),
        "disambiguation_hint": _public_text(getattr(file_selection, "disambiguation_hint", "")),
    }
    return {key: value for key, value in payload.items() if value not in (False, "", [], {})}


def _public_file_selection_summaries(schemas: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    summaries: list[dict[str, Any]] = []
    for schema in schemas.values():
        for input_field in schema.inputs.values():
            field_selection = input_field.file_selection
            has_selection_metadata = any((
                field_selection.required,
                field_selection.allow_multiple,
                field_selection.expected_content,
                field_selection.supported_file_types,
                field_selection.helpful_columns,
                field_selection.disambiguation_hint,
            ))
            if not has_selection_metadata:
                continue
            summary = {
                "schema_id": schema.schema_id,
                "field": input_field.name,
                "type": input_field.type,
                "required": bool(field_selection.required),
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


def _public_schema_summaries(contract: Any, schemas: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    summaries: list[dict[str, Any]] = []
    for schema_id in sorted(contract.input_schemas):
        ref = contract.input_schemas[schema_id]
        schema = schemas.get(schema_id)
        if schema is None:
            raise ValueError("public_skill_profile_schema_missing")
        exposed_names = {
            field.name for field in schema.inputs.values() if field.expose
        }
        fields = [
            _public_input_field(field, exposed_names=exposed_names)
            for field in sorted(schema.inputs.values(), key=lambda item: item.name)
            if field.expose
        ]
        summaries.append(
            {
                "schema_id": schema_id,
                "title": _public_text(ref.title or schema.title, fallback=schema_id),
                "description": _public_text(ref.description or schema.description),
                "aliases": list(_public_text_tuple(ref.aliases)),
                "fields": fields,
                "constraints": _public_constraints(
                    schema.constraints,
                    exposed_names=exposed_names,
                ),
            }
        )
    return tuple(summaries)


def _public_input_field(field: Any, *, exposed_names: set[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": field.name,
        "type": field.type,
        "required": bool(field.required),
    }
    for key in ("title", "description", "question"):
        value = _public_text(getattr(field, key, ""))
        if value:
            payload[key] = value
    aliases = _public_text_tuple(field.aliases)
    if aliases:
        payload["aliases"] = list(aliases)
    if field.required_when:
        unknown = set(field.required_when) - exposed_names
        if unknown:
            raise ValueError("public_skill_profile_required_when_hidden_field")
        payload["required_when"] = _strict_public_json(field.required_when)
    if field.default is not None:
        payload["default"] = _strict_public_json(field.default)
    if field.enum:
        payload["enum"] = _strict_public_json(list(field.enum))
    if field.const is not None:
        payload["const"] = _strict_public_json(field.const)
    examples = _public_text_tuple(field.clarification.examples)
    if examples:
        payload["clarification"] = {"examples": list(examples)}
    validation = {
        key: value
        for key, value in {
            "min": field.validation.min,
            "max": field.validation.max,
            "min_length": field.validation.min_length,
            "max_length": field.validation.max_length,
            "file_extensions": list(field.validation.file_extensions),
        }.items()
        if value not in (None, [], ())
    }
    if validation:
        payload["validation"] = _strict_public_json(validation)
    file_selection = {
        key: value
        for key, value in {
            "required": bool(field.file_selection.required),
            "allow_multiple": bool(field.file_selection.allow_multiple),
            "expected_content": list(_public_text_tuple(field.file_selection.expected_content)),
            "supported_file_types": list(_public_text_tuple(field.file_selection.supported_file_types)),
            "helpful_columns": list(_public_text_tuple(field.file_selection.helpful_columns)),
            "disambiguation_hint": _public_text(field.file_selection.disambiguation_hint),
        }.items()
        if value not in (False, "", [], {})
    }
    if file_selection:
        payload["file_selection"] = file_selection
    return payload


def _public_constraints(
    constraints: tuple[Mapping[str, Any], ...],
    *,
    exposed_names: set[str],
) -> list[dict[str, Any]]:
    supported = {"any_of", "one_of", "mutually_exclusive", "dependencies"}
    normalized: list[dict[str, Any]] = []
    for constraint in constraints:
        unknown = set(constraint) - supported
        if unknown or not constraint:
            raise ValueError("public_skill_profile_constraint_unsupported")
        item: dict[str, Any] = {}
        for key in ("any_of", "one_of", "mutually_exclusive"):
            if key not in constraint:
                continue
            fields = tuple(sorted(_public_text_tuple(constraint.get(key))))
            if not fields or any(field not in exposed_names for field in fields):
                raise ValueError("public_skill_profile_constraint_field_invalid")
            item[key] = list(fields)
        if "dependencies" in constraint:
            dependencies = constraint.get("dependencies")
            if not isinstance(dependencies, Mapping):
                raise ValueError("public_skill_profile_constraint_dependencies_invalid")
            public_dependencies: dict[str, list[str]] = {}
            for field_name in sorted(str(key) for key in dependencies):
                required = tuple(sorted(_public_text_tuple(dependencies[field_name])))
                if (
                    field_name not in exposed_names
                    or not required
                    or any(name not in exposed_names for name in required)
                ):
                    raise ValueError("public_skill_profile_constraint_field_invalid")
                public_dependencies[field_name] = list(required)
            item["dependencies"] = public_dependencies
        if not item:
            raise ValueError("public_skill_profile_constraint_unsupported")
        normalized.append(item)
    return normalized


def _public_output_contracts(contract: Any) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for output_id in sorted(contract.outputs):
        output = contract.outputs[output_id]
        artifacts: list[dict[str, Any]] = []
        for artifact in output.artifacts:
            summary = {
                key: list(_public_text_tuple(artifact.get(key)))
                for key in ("extensions", "mime_types")
                if _public_text_tuple(artifact.get(key))
            }
            if summary:
                artifacts.append(summary)
        summaries.append(
            {
                "output_id": output.output_id,
                "required_fields": list(_public_text_tuple(output.required)),
                "artifacts": artifacts,
            }
        )
    return summaries


def _strict_public_json(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        if isinstance(value, str) and _contains_forbidden_text(value):
            raise ValueError("public_skill_profile_forbidden_text")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("public_skill_profile_non_finite_number")
        return value
    if isinstance(value, list | tuple):
        return [_strict_public_json(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("public_skill_profile_non_string_key")
            key_text = key.strip()
            if not key_text or _is_forbidden_key(key_text):
                raise ValueError("public_skill_profile_forbidden_key")
            normalized[key_text] = _strict_public_json(child)
        return {key: normalized[key] for key in sorted(normalized)}
    raise ValueError("public_skill_profile_non_json_value")


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
