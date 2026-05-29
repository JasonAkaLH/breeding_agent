from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .parameters import SkillParameterSpec

if TYPE_CHECKING:
    from .manifest import SkillManifest
    from .script_manifest import SkillScriptEntrypoint

SkillInputTextGenerator = Callable[..., Any]

_SCALAR_LLM_TYPES = {"string", "str", "int", "integer", "number", "float"}
_TEXT_SOURCES = {"query", "current_user_message", "resolved_user_message", "recent_user_message"}
_SAFE_ARTIFACT_KEYS = {
    "upload_id",
    "filename",
    "mime_type",
    "content_type",
    "size",
    "row_count",
    "columns",
    "preview",
    "summary",
}


@dataclass(slots=True, frozen=True)
class SkillInputSource:
    source: str
    confidence: str


@dataclass(slots=True, frozen=True)
class SkillInputResolutionContext:
    query: str
    current_user_message: str = ""
    resolved_user_message: str = ""
    recent_user_messages: tuple[str, ...] = ()
    artifact_summaries: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_metadata(
        cls,
        *,
        query: str,
        metadata: Mapping[str, Any],
        artifact_summaries: tuple[Mapping[str, Any], ...] = (),
    ) -> "SkillInputResolutionContext":
        memory = metadata.get("conversation_memory") or metadata.get("memory_context") or {}
        if not isinstance(memory, Mapping):
            memory = {}
        current_user_message = str(memory.get("current_user_message") or "")
        resolved_user_message = str(memory.get("resolved_user_message") or "")
        recent_user_messages = _recent_user_messages(memory.get("recent_messages"))
        return cls(
            query=query,
            current_user_message=current_user_message,
            resolved_user_message=resolved_user_message,
            recent_user_messages=recent_user_messages,
            artifact_summaries=artifact_summaries,
        )


@dataclass(slots=True, frozen=True)
class SkillInputResolutionResult:
    payload: dict[str, Any]
    missing: tuple[str, ...] = ()
    sources: Mapping[str, SkillInputSource] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    prompt_profile: Mapping[str, Any] | None = None

    @property
    def resolved_fields(self) -> tuple[str, ...]:
        return tuple(self.sources.keys())

    def audit_payload(self, *, skill_name: str, entrypoint: str) -> dict[str, Any]:
        return {
            "skill_name": skill_name,
            "entrypoint": entrypoint,
            "resolved_fields": list(self.resolved_fields),
            "sources": {
                field: {"source": source.source, "confidence": source.confidence}
                for field, source in self.sources.items()
            },
            **({"prompt_profile": dict(self.prompt_profile)} if self.prompt_profile is not None else {}),
        }


def resolve_skill_inputs(
    manifest: "SkillManifest",
    script: "SkillScriptEntrypoint",
    base_payload: Mapping[str, Any],
    context: SkillInputResolutionContext,
) -> SkillInputResolutionResult:
    del script  # The script hook is part of the public call shape; current rules are manifest scoped.
    payload = dict(base_payload)
    sources: dict[str, SkillInputSource] = {}
    metadata = payload.get("metadata")
    safe_metadata = metadata if isinstance(metadata, Mapping) else {}

    for name, spec in manifest.parameters.items():
        if _source_allowed(spec, "payload") and name in payload:
            coerced = _coerce_value(payload[name], spec)
            if coerced is not None:
                payload[name] = coerced
                sources[name] = SkillInputSource(source="payload", confidence="high")
                continue
            payload.pop(name, None)
        artifact = _resolve_from_artifacts(spec, payload, context)
        if artifact is not None:
            payload[name] = artifact
            sources[name] = SkillInputSource(source="artifact", confidence="high")
            continue
        explicit = _resolve_from_metadata(spec, safe_metadata)
        if explicit is not None:
            payload[name] = explicit
            sources[name] = SkillInputSource(source="metadata", confidence="high")
            continue
        resolved = _resolve_from_texts(spec, context)
        if resolved is not None:
            value, source = resolved
            payload[name] = value
            sources[name] = source

    missing = tuple(name for name, spec in manifest.parameters.items() if spec.required and name not in payload)
    return SkillInputResolutionResult(payload=payload, missing=missing, sources=sources)


async def resolve_skill_inputs_with_llm(
    manifest: "SkillManifest",
    script: "SkillScriptEntrypoint",
    base_payload: Mapping[str, Any],
    context: SkillInputResolutionContext,
    *,
    text_generator: SkillInputTextGenerator | None = None,
) -> SkillInputResolutionResult:
    """Resolve Skill inputs, using an LLM only as a validated missing-slot fallback.

    The deterministic resolver remains the authority for payload, metadata, artifact,
    and regex-derived values. The LLM path is intentionally constrained to still
    missing scalar parameters and its output is treated as untrusted candidate JSON.
    """

    deterministic = resolve_skill_inputs(manifest, script, base_payload, context)
    if text_generator is None or not deterministic.missing:
        return deterministic

    eligible_specs = _llm_eligible_missing_specs(manifest.parameters, deterministic.missing)
    if not eligible_specs:
        return deterministic

    prompt_resolution = _build_llm_slot_prompt_resolution(
        manifest=manifest,
        script=script,
        context=context,
        resolved_payload=deterministic.payload,
        missing_specs=eligible_specs,
        trim_max_tokens=_metadata_trim_max_tokens(base_payload),
    )
    from src.orchestration.prompt_profiles import optional_profile_kwargs

    try:
        kwargs = optional_profile_kwargs(
            text_generator,
            prompt_profile=prompt_resolution.llm_call_payload,
        )
        raw_response = text_generator(prompt_resolution.prompt, **kwargs) if kwargs else text_generator(prompt_resolution.prompt)
        if inspect.isawaitable(raw_response):
            raw_response = await raw_response
        candidates = _parse_llm_slot_candidates(str(raw_response or ""))
    except json.JSONDecodeError:
        return _copy_result(deterministic, diagnostics=("llm_invalid_json",), prompt_profile=prompt_resolution.llm_call_payload)
    except Exception:
        return _copy_result(deterministic, diagnostics=("llm_failed",), prompt_profile=prompt_resolution.llm_call_payload)

    payload = dict(deterministic.payload)
    sources = dict(deterministic.sources)
    diagnostics: list[str] = []
    for name, candidate in candidates.items():
        spec = eligible_specs.get(name)
        if spec is None:
            diagnostics.append("llm_rejected_unknown_parameter")
            continue
        accepted = _validate_llm_candidate(spec, candidate)
        if accepted is None:
            diagnostics.append("llm_rejected_invalid_value")
            continue
        value, source = accepted
        payload[name] = value
        sources[name] = source

    missing = tuple(name for name, spec in manifest.parameters.items() if spec.required and name not in payload)
    return SkillInputResolutionResult(
        payload=payload,
        missing=missing,
        sources=sources,
        diagnostics=_dedupe((*deterministic.diagnostics, *diagnostics)),
        prompt_profile=prompt_resolution.llm_call_payload,
    )


def _source_allowed(spec: SkillParameterSpec, source: str) -> bool:
    return not spec.sources or source in spec.sources


def _copy_result(
    result: SkillInputResolutionResult,
    *,
    diagnostics: tuple[str, ...] = (),
    prompt_profile: Mapping[str, Any] | None = None,
) -> SkillInputResolutionResult:
    return SkillInputResolutionResult(
        payload=dict(result.payload),
        missing=result.missing,
        sources=dict(result.sources),
        diagnostics=_dedupe((*result.diagnostics, *diagnostics)),
        prompt_profile=prompt_profile if prompt_profile is not None else result.prompt_profile,
    )


def _resolve_from_artifacts(
    spec: SkillParameterSpec,
    payload: Mapping[str, Any],
    context: SkillInputResolutionContext,
) -> dict[str, Any] | None:
    artifact_requested = spec.type in {"artifact", "file", "data"} or "artifact" in spec.sources
    if not artifact_requested:
        return None
    if not _source_allowed(spec, "artifact"):
        return None
    artifacts = payload.get("uploaded_artifacts")
    count = len(artifacts) if isinstance(artifacts, list | tuple) else 0
    if count <= 0:
        count = len(context.artifact_summaries)
    if count <= 0:
        return None
    return {"available": True, "count": count}


def _resolve_from_metadata(spec: SkillParameterSpec, metadata: Mapping[str, Any]) -> Any | None:
    if not _source_allowed(spec, "metadata"):
        return None
    for key in (spec.name, *spec.aliases):
        if key in metadata:
            coerced = _coerce_value(metadata[key], spec)
            if coerced is not None:
                return coerced
    return None


def _resolve_from_texts(
    spec: SkillParameterSpec,
    context: SkillInputResolutionContext,
) -> tuple[Any, SkillInputSource] | None:
    candidates: list[tuple[str, str, str]] = []
    if _source_allowed(spec, "query"):
        candidates.append(("query", context.query, "high"))
    if _source_allowed(spec, "current_user_message") and context.current_user_message and context.current_user_message != context.query:
        candidates.append(("current_user_message", context.current_user_message, "high"))
    if _source_allowed(spec, "recent_user_message"):
        for message in context.recent_user_messages:
            candidates.append(("recent_user_message", message, "medium"))

    patterns = spec.patterns or _default_patterns(spec)
    for source_name, text, confidence in candidates:
        if not text:
            continue
        for pattern in patterns:
            raw_value = _match_pattern(pattern, text)
            if raw_value is None:
                continue
            coerced = _coerce_value(raw_value, spec)
            if coerced is not None:
                return coerced, SkillInputSource(source=source_name, confidence=confidence)
    return None


def _match_pattern(pattern: str, text: str) -> str | None:
    try:
        match = re.search(pattern, text, flags=re.IGNORECASE)
    except re.error:
        return None
    if match is None:
        return None
    if match.groups():
        return next((group for group in match.groups() if group not in (None, "")), None)
    return match.group(0)


def _default_patterns(spec: SkillParameterSpec) -> tuple[str, ...]:
    terms = tuple(dict.fromkeys((spec.name, *spec.aliases)))
    if not terms:
        return ()
    escaped = "|".join(re.escape(term) for term in terms if term)
    if not escaped:
        return ()
    if spec.type in {"int", "integer"}:
        return (
            rf"(?:{escaped})\s*[:：=]?\s*(\d+)",
            rf"(\d+)\s*(?:个|次)?(?:{escaped})",
        )
    return (rf"(?:{escaped})\s*[:：=]\s*([^\s,，。；;]+)",)


def _coerce_value(value: Any, spec: SkillParameterSpec) -> Any | None:
    if value is None or isinstance(value, bool):
        return None
    if spec.type in {"int", "integer"}:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
    if spec.type in {"number", "float"}:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None
    text = str(value).strip()
    return text or None


def _llm_eligible_missing_specs(
    parameters: Mapping[str, SkillParameterSpec],
    missing: tuple[str, ...],
) -> dict[str, SkillParameterSpec]:
    specs: dict[str, SkillParameterSpec] = {}
    for name in missing:
        spec = parameters.get(name)
        if spec is None:
            continue
        if not _llm_can_resolve(spec):
            continue
        specs[name] = spec
    return specs


def _llm_can_resolve(spec: SkillParameterSpec) -> bool:
    if spec.type not in _SCALAR_LLM_TYPES:
        return False
    if not spec.sources:
        return True
    return any(source in _TEXT_SOURCES for source in spec.sources)


def _build_llm_slot_prompt(
    *,
    manifest: "SkillManifest",
    script: "SkillScriptEntrypoint",
    context: SkillInputResolutionContext,
    resolved_payload: Mapping[str, Any],
    missing_specs: Mapping[str, SkillParameterSpec],
) -> str:
    prompt_payload = {
        "skill": {
            "name": manifest.name,
            "description": manifest.description,
            "entrypoint": script.name,
        },
        "parameters_to_resolve": [
            {
                "name": spec.name,
                "type": spec.type,
                "required": spec.required,
                "aliases": list(spec.aliases),
                "allowed_sources": list(spec.sources) if spec.sources else sorted(_TEXT_SOURCES),
            }
            for spec in missing_specs.values()
        ],
        "already_resolved": _safe_resolved_payload(resolved_payload),
        "context": {
            "query": context.query,
            "current_user_message": context.current_user_message,
            "resolved_user_message": context.resolved_user_message,
            "recent_user_messages": list(context.recent_user_messages),
            "artifact_summaries": [_safe_artifact_summary(item) for item in context.artifact_summaries],
        },
    }
    return (
        "你是一个受限的 Skill 参数补槽器。只根据给定上下文抽取缺失参数，禁止编造文件、数据或未声明字段。\n"
        "先判断每个参数是否能从 query/current_user_message/resolved_user_message/recent_user_messages 中直接推出；"
        "不能确定就放入 missing。\n"
        "只返回 JSON 对象，不要返回 Markdown。格式：\n"
        '{"resolved":{"参数名":{"value":值,"source":"query|current_user_message|resolved_user_message|recent_user_message"}},'
        '"missing":["参数名"]}\n'
        "输入如下：\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)}"
    )


def _build_llm_slot_prompt_resolution(
    *,
    manifest: "SkillManifest",
    script: "SkillScriptEntrypoint",
    context: SkillInputResolutionContext,
    resolved_payload: Mapping[str, Any],
    missing_specs: Mapping[str, SkillParameterSpec],
    trim_max_tokens: int | None = None,
):
    from src.orchestration.prompt_envelope import PromptSegment
    from src.orchestration.prompt_profiles import PROMPT_PROFILE_TEMPLATE_VERSION, resolve_profile_prompt_for_mode

    legacy_prompt = _build_llm_slot_prompt(
        manifest=manifest,
        script=script,
        context=context,
        resolved_payload=resolved_payload,
        missing_specs=missing_specs,
    )
    public_skill_schema = {
        "name": manifest.name,
        "description": manifest.description,
        "parameters_to_resolve": [
            {
                "name": spec.name,
                "type": spec.type,
                "required": spec.required,
                "aliases": list(spec.aliases),
                "allowed_sources": list(spec.sources) if spec.sources else sorted(_TEXT_SOURCES),
            }
            for spec in missing_specs.values()
        ],
    }
    safe_context = {
        "query": context.query,
        "current_user_message": context.current_user_message,
        "resolved_user_message": context.resolved_user_message,
        "recent_user_messages": list(context.recent_user_messages),
        "artifact_summaries": [_safe_artifact_summary(item) for item in context.artifact_summaries],
    }
    return resolve_profile_prompt_for_mode(
        legacy_prompt=legacy_prompt,
        template_id="skill_input_resolver",
        template_version=PROMPT_PROFILE_TEMPLATE_VERSION,
        trim_max_tokens=trim_max_tokens,
        segments=(
            PromptSegment(
                name="stable_skill_input_resolver_rules",
                role="system",
                content=(
                    "你是一个受限的 Skill 参数补槽器。只根据给定上下文抽取缺失参数；"
                    "禁止编造文件、数据、未声明字段、内部入口或执行细节。不能确定就放入 missing。"
                ),
                priority=0,
                mutability="stable",
                cache_affinity="prefix",
                trim_policy="required",
                security_role="instruction",
            ),
            PromptSegment(
                name="skill_public_parameter_schema",
                role="context",
                content="# Skill 公开参数 schema\n" + json.dumps(public_skill_schema, ensure_ascii=False, indent=2, default=str),
                priority=0,
                mutability="dynamic",
                cache_affinity="no_cache",
                trim_policy="required",
                security_role="tool_schema",
            ),
            PromptSegment(
                name="already_resolved_payload",
                role="context",
                content="# 已确定参数（脱敏）\n"
                + json.dumps(_safe_resolved_payload(resolved_payload), ensure_ascii=False, indent=2, default=str),
                priority=0,
                mutability="dynamic",
                cache_affinity="no_cache",
                trim_policy="required",
                security_role="active_note",
            ),
            PromptSegment(
                name="resolver_context",
                role="context",
                content="# 可用于补槽的上下文（已限制）\n"
                + json.dumps(safe_context, ensure_ascii=False, indent=2, default=str),
                priority=0,
                mutability="dynamic",
                cache_affinity="no_cache",
                trim_policy="drop_oldest",
                security_role="history",
            ),
            PromptSegment(
                name="slot_resolver_output_guard",
                role="system",
                content=(
                    "只返回 JSON 对象，不要 Markdown。格式："
                    '{"resolved":{"参数名":{"value":值,"source":"query|current_user_message|resolved_user_message|recent_user_message"}},'
                    '"missing":["参数名"]}'
                ),
                priority=0,
                mutability="stable",
                cache_affinity="no_cache",
                trim_policy="required",
                security_role="guard",
            ),
        ),
        audit_context={"stage": "skill_input_resolver", "skill_name": manifest.name},
    )


def _metadata_trim_max_tokens(base_payload: Mapping[str, Any]) -> int | None:
    from src.orchestration.prompt_profiles import coerce_profile_trim_max_tokens

    metadata = base_payload.get("metadata")
    safe_metadata = metadata if isinstance(metadata, Mapping) else {}
    return coerce_profile_trim_max_tokens(
        base_payload.get("skill_input_trim_max_tokens"),
        base_payload.get("trim_max_tokens"),
        safe_metadata.get("skill_input_trim_max_tokens"),
        safe_metadata.get("trim_max_tokens"),
    )


def _safe_resolved_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if key_text in {"metadata", "uploaded_artifacts"}:
            continue
        if isinstance(value, str | int | float):
            safe[key_text] = value
        elif isinstance(value, Mapping) and value.get("available") is True:
            safe[key_text] = {
                "available": True,
                "count": value.get("count"),
            }
    return safe


def _safe_artifact_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, raw_value in value.items():
        key_text = str(key)
        if key_text not in _SAFE_ARTIFACT_KEYS:
            continue
        if isinstance(raw_value, str | int | float | bool) or raw_value is None:
            safe[key_text] = raw_value
        elif isinstance(raw_value, list | tuple):
            safe[key_text] = [item for item in raw_value if isinstance(item, str | int | float | bool) or item is None][:20]
    return safe


def _parse_llm_slot_candidates(text: str) -> dict[str, dict[str, Any]]:
    parsed = _load_json_object(text)
    resolved = parsed.get("resolved")
    if not isinstance(resolved, Mapping):
        resolved = {
            key: value
            for key, value in parsed.items()
            if key not in {"missing", "diagnostics", "reasoning"}
        }
    candidates: dict[str, dict[str, Any]] = {}
    for raw_name, raw_candidate in resolved.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if isinstance(raw_candidate, Mapping):
            candidate = dict(raw_candidate)
            if "value" not in candidate:
                candidate = {"value": raw_candidate}
        else:
            candidate = {"value": raw_candidate}
        candidates[name] = candidate
    return candidates


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


def _validate_llm_candidate(
    spec: SkillParameterSpec,
    candidate: Mapping[str, Any],
) -> tuple[Any, SkillInputSource] | None:
    if not _llm_can_resolve(spec):
        return None
    source_hint = str(candidate.get("source") or "").strip()
    if source_hint and source_hint not in _TEXT_SOURCES:
        return None
    if source_hint and not _source_allowed(spec, source_hint):
        return None
    if not source_hint and spec.sources:
        return None

    coerced = _coerce_value(candidate.get("value"), spec)
    if coerced is None:
        return None
    source_name = f"llm_slot_resolver:{source_hint}" if source_hint else "llm_slot_resolver"
    return coerced, SkillInputSource(source=source_name, confidence="medium")


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _recent_user_messages(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    messages: list[str] = []
    for item in reversed(value):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("role") or "").lower() != "user":
            continue
        content = str(item.get("content") or "").strip()
        if content:
            messages.append(content)
    return tuple(messages)
