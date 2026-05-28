from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.orchestration.models import CapabilityDescriptor

from .manifest import SkillManifest

_PUBLIC_USAGE_KEYS = frozenset(
    {
        "overview",
        "input_formats",
        "examples",
        "outputs",
        "limits",
        "when_to_use",
        "required_data",
        "parameters",
        "data_fields",
        "answerable_questions",
        "notes",
    }
)
_FORBIDDEN_KEY_PARTS = (
    "source_path",
    "script",
    "handler",
    "handler_module",
    "handler_factory",
    "runtime",
    "sidecar",
    "endpoint",
    "base_url",
    "url",
    "dsn",
    "token",
    "secret",
    "password",
    "api_key",
    "authorization",
    "config",
    "module",
    "path",
    "sql",
)
_FORBIDDEN_TEXT_PARTS = (
    "scripts/",
    "runtime/",
    "rscript",
    "wrapper",
    "platform_service",
    "handler",
    "sidecar",
    "config.yaml",
    "mysql://",
    "postgresql://",
    "api_key",
    "token",
    "secret",
)


@dataclass(slots=True, frozen=True)
class PublicSkillProfile:
    capability_id: str
    name: str
    display_name: str
    description: str
    triggers: tuple[str, ...]
    parameters: tuple[dict[str, Any], ...]
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    public_usage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "triggers": list(self.triggers),
            "parameters": list(self.parameters),
            "inputs": self.inputs,
            "outputs": self.outputs,
            "public_usage": self.public_usage,
        }


def build_public_skill_profile(
    manifest: SkillManifest,
    *,
    capability_id: str,
    descriptor: CapabilityDescriptor | None = None,
) -> PublicSkillProfile:
    """Build the LLM-safe public description for soft Skill binding.

    This profile is intentionally derived from manifest frontmatter and a small
    public_usage allowlist. It never reads or serializes ``manifest.body`` or
    runtime/script fields.
    """

    display_name = (descriptor.display_name if descriptor else "") or str(manifest.metadata.get("display_name") or "").strip()
    return PublicSkillProfile(
        capability_id=capability_id,
        name=manifest.name,
        display_name=display_name or manifest.name,
        description=(descriptor.description if descriptor else "") or manifest.description,
        triggers=tuple(str(trigger) for trigger in manifest.triggers if str(trigger).strip()),
        parameters=tuple(_parameter_payload(name, spec) for name, spec in manifest.parameters.items()),
        inputs=_io_contract_payload(manifest.inputs),
        outputs=_io_contract_payload(manifest.outputs),
        public_usage=_public_usage_payload(manifest.metadata.get("public_usage")),
    )


def _parameter_payload(name: str, spec: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": str(name),
        "type": str(getattr(spec, "type", "string") or "string"),
        "required": bool(getattr(spec, "required", False)),
    }
    sources = tuple(str(item) for item in getattr(spec, "sources", ()) if str(item).strip())
    if sources:
        payload["sources"] = list(sources)
    aliases = tuple(str(item) for item in getattr(spec, "aliases", ()) if str(item).strip())
    if aliases:
        payload["aliases"] = list(aliases)
    patterns = tuple(str(item) for item in getattr(spec, "patterns", ()) if str(item).strip())
    if patterns:
        payload["patterns"] = list(patterns)
    enum = tuple(str(item) for item in getattr(spec, "enum", ()) if str(item).strip())
    if enum:
        payload["enum"] = list(enum)
    default = getattr(spec, "default", None)
    if default is not None:
        payload["default"] = default
    return payload


def _io_contract_payload(value: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    required = tuple(str(item) for item in getattr(value, "required", ()) if str(item).strip())
    if required:
        payload["required"] = list(required)
    files = tuple(getattr(value, "files", ()) or ())
    if files:
        safe_files: list[dict[str, Any]] = []
        for file_spec in files:
            entry: dict[str, Any] = {}
            extensions = tuple(str(item) for item in getattr(file_spec, "extensions", ()) if str(item).strip())
            mime_types = tuple(str(item) for item in getattr(file_spec, "mime_types", ()) if str(item).strip())
            if extensions:
                entry["extensions"] = list(extensions)
            if mime_types:
                entry["mime_types"] = list(mime_types)
            if entry:
                safe_files.append(entry)
        if safe_files:
            payload["files"] = safe_files
    return payload


def _public_usage_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for key in _PUBLIC_USAGE_KEYS:
        if key not in value:
            continue
        sanitized = _sanitize_public_value(value[key])
        if sanitized not in (None, "", [], {}):
            payload[key] = sanitized
    return payload


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
    return str(value)


def _is_forbidden_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _FORBIDDEN_KEY_PARTS)


def _contains_forbidden_text(text: str) -> bool:
    normalized = text.lower()
    return any(part in normalized for part in _FORBIDDEN_TEXT_PARTS)
