from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .contract import SkillContract


class SkillInputSchemaParseError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class SkillInputSourcePolicy:
    allowed: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SkillInputValidationRule:
    regex: str = ""
    min: float | None = None
    max: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    message: str = ""


@dataclass(slots=True, frozen=True)
class SkillInputClarification:
    hint: str = ""
    examples: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SkillInputField:
    name: str
    type: str = "string"
    title: str = ""
    required: bool = False
    required_when: Mapping[str, Any] = field(default_factory=dict)
    source: SkillInputSourcePolicy = SkillInputSourcePolicy()
    aliases: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    default: Any | None = None
    enum: tuple[str, ...] = ()
    const: Any | None = None
    description: str = ""
    question: str = ""
    reference_resource: str = ""
    clarification: SkillInputClarification = SkillInputClarification()
    validation: SkillInputValidationRule = SkillInputValidationRule()
    expose: bool = True


@dataclass(slots=True, frozen=True)
class SkillInputSchema:
    schema_version: str
    schema_id: str
    title: str
    description: str
    applies_when: Mapping[str, Any] = field(default_factory=dict)
    inputs: Mapping[str, SkillInputField] = field(default_factory=dict)
    constraints: tuple[Mapping[str, Any], ...] = ()
    slot_policy: Mapping[str, Any] = field(default_factory=dict)
    entrypoint_mapping: str = ""
    source_path: Path = Path("schema.input.yaml")


@dataclass(slots=True, frozen=True)
class SkillInputValidationIssue:
    field: str
    reason: str
    message: str = ""


@dataclass(slots=True, frozen=True)
class SkillInputValidationResult:
    schema_id: str
    payload: Mapping[str, Any]
    missing: tuple[str, ...] = ()
    invalid: tuple[SkillInputValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing and not self.invalid


_ALLOWED_TYPES = {"string", "integer", "int", "number", "float", "boolean", "bool", "object", "array", "artifact", "file", "data"}
_ARTIFACT_TYPES = {"artifact", "file", "data"}
_ARTIFACT_TRUSTED_SOURCES = {"payload", "slot_collection", "artifact", "task_attachment", "validated_artifact", "upload_ledger"}
_LLM_TEXT_SOURCES = {
    "llm",
    "query",
    "current_answer",
    "current_user_message",
    "resolved_user_message",
    "recent_user_message",
    "text",
    "history",
    "artifact",
}


def parse_input_schema_file(path: str | Path) -> SkillInputSchema:
    source_path = Path(path)
    try:
        raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SkillInputSchemaParseError(f"Invalid input schema YAML: {source_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SkillInputSchemaParseError(f"Input schema must be a mapping: {source_path}")
    schema_version = str(raw.get("schema_version") or raw.get("version") or "1").strip()
    schema_id = str(raw.get("schema_id") or raw.get("id") or "").strip()
    if not schema_id:
        raise SkillInputSchemaParseError(f"Input schema requires schema_id: {source_path}")
    inputs_raw = raw.get("inputs") or {}
    if not isinstance(inputs_raw, Mapping):
        raise SkillInputSchemaParseError(f"Input schema inputs must be a mapping: {source_path}")
    inputs = {str(name): _parse_field(str(name), value, source_path) for name, value in inputs_raw.items() if str(name).strip()}
    constraints = raw.get("constraints") or ()
    if isinstance(constraints, Mapping):
        constraints_tuple = (dict(constraints),)
    elif isinstance(constraints, list | tuple):
        constraints_tuple = tuple(dict(item) for item in constraints if isinstance(item, Mapping))
    else:
        raise SkillInputSchemaParseError(f"Input schema constraints must be mapping/list: {source_path}")
    return SkillInputSchema(
        schema_version=schema_version,
        schema_id=schema_id,
        title=str(raw.get("title") or schema_id).strip(),
        description=str(raw.get("description") or "").strip(),
        applies_when=dict(raw.get("applies_when") or {}) if isinstance(raw.get("applies_when"), Mapping) else {},
        inputs=inputs,
        constraints=constraints_tuple,
        slot_policy=dict(raw.get("slot_policy") or {}) if isinstance(raw.get("slot_policy"), Mapping) else {},
        entrypoint_mapping=str(raw.get("entrypoint_mapping") or raw.get("entrypoint") or "").strip(),
        source_path=source_path,
    )


def load_input_schemas_for_contract(contract: SkillContract) -> dict[str, SkillInputSchema]:
    schemas: dict[str, SkillInputSchema] = {}
    for schema_id, ref in contract.input_schemas.items():
        schema = parse_input_schema_file(contract.root_dir / ref.path)
        if schema.schema_id != schema_id:
            raise SkillInputSchemaParseError(
                f"Input schema id mismatch for {ref.path}: contract={schema_id}, file={schema.schema_id}"
            )
        schemas[schema_id] = schema
    return schemas


def validate_selected_schema_payload(
    schema: SkillInputSchema,
    payload: Mapping[str, Any],
    *,
    candidate_sources: Mapping[str, str] | None = None,
) -> SkillInputValidationResult:
    sources = {str(k): str(v) for k, v in dict(candidate_sources or {}).items()}
    missing: list[str] = []
    invalid: list[SkillInputValidationIssue] = []
    for name, field in schema.inputs.items():
        present = name in payload and payload[name] not in (None, "")
        if not present:
            if field.default is not None:
                continue
            if field.required or _required_when_matches(field.required_when, payload):
                missing.append(name)
            continue
        issue = _validate_field_value(field, payload[name], source=sources.get(name, "payload"))
        if issue is not None:
            invalid.append(issue)
    invalid.extend(_validate_constraints(schema.constraints, payload))
    # Constraints can introduce missing requirements (e.g. any_of none present).
    constraint_missing = [issue.field for issue in invalid if issue.reason in {"any_of_missing", "one_of_missing", "dependency_missing"}]
    for field in constraint_missing:
        if field and field not in missing and field in schema.inputs:
            missing.append(field)
    return SkillInputValidationResult(schema_id=schema.schema_id, payload=payload, missing=tuple(dict.fromkeys(missing)), invalid=tuple(invalid))


def _parse_field(name: str, value: Any, source_path: Path) -> SkillInputField:
    if not isinstance(value, Mapping):
        value = {}
    field_type = str(value.get("type") or "string").strip().lower() or "string"
    if field_type not in _ALLOWED_TYPES:
        raise SkillInputSchemaParseError(f"Unsupported input field type: {name}={field_type}: {source_path}")
    source = value.get("source") or {}
    source_allowed = source.get("allowed") if isinstance(source, Mapping) else source
    validation = value.get("validation") or {}
    clarification = value.get("clarification") or {}
    return SkillInputField(
        name=name,
        type=field_type,
        title=str(value.get("title") or "").strip(),
        required=bool(value.get("required", False)),
        required_when=dict(value.get("required_when") or {}) if isinstance(value.get("required_when"), Mapping) else {},
        source=SkillInputSourcePolicy(allowed=_string_tuple(source_allowed)),
        aliases=_string_tuple(value.get("aliases") or value.get("alias")),
        patterns=_string_tuple(value.get("patterns") or value.get("pattern")),
        default=value.get("default"),
        enum=_string_tuple(value.get("enum") or value.get("choices")),
        const=value.get("const"),
        description=str(value.get("description") or "").strip(),
        question=str(value.get("question") or "").strip(),
        reference_resource=str(value.get("reference_resource") or "").strip(),
        clarification=SkillInputClarification(
            hint=str(clarification.get("hint") or "").strip() if isinstance(clarification, Mapping) else "",
            examples=_string_tuple(clarification.get("examples") if isinstance(clarification, Mapping) else ()),
        ),
        validation=SkillInputValidationRule(
            regex=str(validation.get("regex") or "").strip() if isinstance(validation, Mapping) else "",
            min=_float_or_none(validation.get("min") if isinstance(validation, Mapping) else None),
            max=_float_or_none(validation.get("max") if isinstance(validation, Mapping) else None),
            min_length=_int_or_none(validation.get("min_length") if isinstance(validation, Mapping) else None),
            max_length=_int_or_none(validation.get("max_length") if isinstance(validation, Mapping) else None),
            message=str(validation.get("message") or "").strip() if isinstance(validation, Mapping) else "",
        ),
        expose=bool(value.get("expose", True)),
    )


def _validate_field_value(field: SkillInputField, value: Any, *, source: str) -> SkillInputValidationIssue | None:
    if field.type in _ARTIFACT_TYPES and source not in _ARTIFACT_TRUSTED_SOURCES:
        return SkillInputValidationIssue(field.name, "artifact_source_denied", "Artifact inputs must come from artifact metadata/ledger.")
    if field.const is not None and value != field.const:
        return SkillInputValidationIssue(field.name, "const_mismatch", f"Expected const value {field.const!r}.")
    if field.enum and str(value) not in field.enum:
        return SkillInputValidationIssue(field.name, "enum", "Value is not in enum.")
    coerced = _coerce_for_validation(field, value)
    if coerced is None:
        return SkillInputValidationIssue(field.name, "type", f"Value does not match type {field.type}.")
    rule = field.validation
    text = str(value)
    if rule.regex:
        try:
            if re.fullmatch(rule.regex, text) is None:
                return SkillInputValidationIssue(field.name, "regex", rule.message or "Value does not match regex.")
        except re.error:
            return SkillInputValidationIssue(field.name, "regex_invalid", "Validation regex is invalid.")
    if isinstance(coerced, int | float):
        if rule.min is not None and coerced < rule.min:
            return SkillInputValidationIssue(field.name, "min", rule.message or "Value is below minimum.")
        if rule.max is not None and coerced > rule.max:
            return SkillInputValidationIssue(field.name, "max", rule.message or "Value is above maximum.")
    if isinstance(value, str):
        if rule.min_length is not None and len(value) < rule.min_length:
            return SkillInputValidationIssue(field.name, "min_length", rule.message or "Value is too short.")
        if rule.max_length is not None and len(value) > rule.max_length:
            return SkillInputValidationIssue(field.name, "max_length", rule.message or "Value is too long.")
    return None


def _validate_constraints(constraints: tuple[Mapping[str, Any], ...], payload: Mapping[str, Any]) -> list[SkillInputValidationIssue]:
    issues: list[SkillInputValidationIssue] = []
    for constraint in constraints:
        if "any_of" in constraint:
            fields = _string_tuple(constraint.get("any_of"))
            if fields and not any(_present(payload, field) for field in fields):
                issues.append(SkillInputValidationIssue(fields[0], "any_of_missing", "At least one field is required."))
        if "one_of" in constraint:
            fields = _string_tuple(constraint.get("one_of"))
            count = sum(1 for field in fields if _present(payload, field))
            if count == 0 and fields:
                issues.append(SkillInputValidationIssue(fields[0], "one_of_missing", "Exactly one field is required."))
            elif count > 1:
                issues.append(SkillInputValidationIssue(fields[0], "one_of", "Fields are mutually exclusive; provide exactly one."))
        if "mutually_exclusive" in constraint:
            fields = _string_tuple(constraint.get("mutually_exclusive"))
            if sum(1 for field in fields if _present(payload, field)) > 1:
                issues.append(SkillInputValidationIssue(fields[0] if fields else "", "mutually_exclusive", "Fields are mutually exclusive."))
        deps = constraint.get("dependencies")
        if isinstance(deps, Mapping):
            for field, required in deps.items():
                if not _present(payload, str(field)):
                    continue
                for dep in _string_tuple(required):
                    if not _present(payload, dep):
                        issues.append(SkillInputValidationIssue(dep, "dependency_missing", f"{dep} is required when {field} is present."))
    return issues


def _required_when_matches(required_when: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    if not required_when:
        return False
    for key, expected in required_when.items():
        if payload.get(key) != expected:
            return False
    return True


def _present(payload: Mapping[str, Any], field: str) -> bool:
    return field in payload and payload[field] not in (None, "")


def _coerce_for_validation(field: SkillInputField, value: Any) -> Any | None:
    if field.type in {"string"}:
        return value if isinstance(value, str) else str(value)
    if field.type in {"integer", "int"}:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed
    if field.type in {"number", "float"}:
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field.type in {"boolean", "bool"}:
        return value if isinstance(value, bool) else None
    if field.type == "object":
        return value if isinstance(value, Mapping) else None
    if field.type == "array":
        return value if isinstance(value, list | tuple) else None
    if field.type in _ARTIFACT_TYPES:
        return value if isinstance(value, Mapping) else None
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _float_or_none(value: Any) -> float | None:
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
