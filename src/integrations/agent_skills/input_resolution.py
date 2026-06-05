from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .missing_input_interrupt import SLOT_COLLECTION_METADATA_KEY
from .parameters import SkillParameterSpec

if TYPE_CHECKING:
    from .manifest import SkillManifest
    from .script_manifest import SkillScriptEntrypoint

SkillInputTextGenerator = Callable[..., Any]

_SCALAR_LLM_TYPES = {"string", "str", "int", "integer", "number", "float"}
_TEXT_SOURCES = {"query", "current_user_message", "resolved_user_message", "recent_user_message"}
_CHINESE_INTEGER_TOKEN = "零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟"
_INTEGER_PHRASE_TOKEN_RE = rf"(?:\d+|[{_CHINESE_INTEGER_TOKEN}]+)"
_SAFE_ARTIFACT_KEYS = {
    "upload_id",
    "filename",
    "original_filename",
    "normalized_filename",
    "mime_type",
    "content_type",
    "normalized_content_type",
    "file_type",
    "size",
    "size_bytes",
    "sha256",
    "row_count",
    "columns",
    "preview",
    "summary",
    "selected_sheet",
    "requires_sheet_selection",
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
    active_slot_collection: Mapping[str, Any] | None = None

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
            active_slot_collection=_metadata_slot_collection(metadata),
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
    payload, sources = _resolve_structured_inputs(manifest, base_payload, context)
    _resolve_text_inputs(manifest.parameters, payload, sources, context)
    active_slot_collection = _active_slot_collection(base_payload) or context.active_slot_collection
    missing = _current_missing_fields(
        manifest.parameters,
        payload,
        base_payload,
        active_slot_collection=active_slot_collection,
    )
    return SkillInputResolutionResult(payload=payload, missing=missing, sources=sources)


def _resolve_structured_inputs(
    manifest: "SkillManifest",
    base_payload: Mapping[str, Any],
    context: SkillInputResolutionContext,
) -> tuple[dict[str, Any], dict[str, SkillInputSource]]:
    payload = dict(base_payload)
    sources: dict[str, SkillInputSource] = {}
    metadata = payload.get("metadata")
    safe_metadata = metadata if isinstance(metadata, Mapping) else {}

    for name, spec in manifest.parameters.items():
        if _artifact_requested(spec):
            payload.pop(name, None)
            artifact = _resolve_from_artifacts(spec, payload, context)
            if artifact is not None:
                payload[name] = artifact
                sources[name] = SkillInputSource(source="artifact", confidence="high")
            continue
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
    return payload, sources


def _resolve_text_inputs(
    parameters: Mapping[str, SkillParameterSpec],
    payload: dict[str, Any],
    sources: dict[str, SkillInputSource],
    context: SkillInputResolutionContext,
    *,
    names: tuple[str, ...] | None = None,
) -> None:
    target_names = names if names is not None else tuple(parameters.keys())
    for name in target_names:
        if name in payload:
            continue
        spec = parameters.get(name)
        if spec is None:
            continue
        if _artifact_requested(spec):
            continue
        resolved = _resolve_from_texts(spec, context)
        if resolved is not None:
            value, source = resolved
            payload[name] = value
            sources[name] = source


async def resolve_skill_inputs_with_llm(
    manifest: "SkillManifest",
    script: "SkillScriptEntrypoint",
    base_payload: Mapping[str, Any],
    context: SkillInputResolutionContext,
    *,
    text_generator: SkillInputTextGenerator | None = None,
) -> SkillInputResolutionResult:
    """Resolve Skill inputs with structured facts first and LLM-first text slots.

    Payload, metadata, and artifact-derived facts remain deterministic authorities.
    For still-missing scalar parameters sourced from natural-language context, the
    LLM resolver runs before deterministic regex/text fallback. LLM output is always
    treated as untrusted candidate JSON and validated before entering the payload.
    """

    payload, sources = _resolve_structured_inputs(manifest, base_payload, context)
    diagnostics: list[str] = []
    prompt_profile: Mapping[str, Any] | None = None

    active_slot_collection = _active_slot_collection(base_payload) or context.active_slot_collection
    llm_target_missing = _llm_target_missing_fields(
        manifest.parameters,
        payload,
        base_payload,
        active_slot_collection=active_slot_collection,
    )
    if text_generator is not None and llm_target_missing:
        eligible_specs = _llm_eligible_missing_specs(manifest.parameters, llm_target_missing)
    else:
        eligible_specs = {}

    if eligible_specs:
        prompt_resolution = _build_llm_slot_prompt_resolution(
            manifest=manifest,
            script=script,
            context=context,
            resolved_payload=payload,
            missing_specs=eligible_specs,
            active_slot_collection=active_slot_collection,
            trim_max_tokens=_metadata_trim_max_tokens(base_payload),
        )
        prompt_profile = prompt_resolution.llm_call_payload
        from src.orchestration.prompt_profiles import optional_profile_kwargs

        try:
            kwargs = optional_profile_kwargs(
                text_generator,
                prompt_profile=prompt_resolution.llm_call_payload,
                metadata=base_payload.get("metadata"),
            )
            raw_response = (
                text_generator(prompt_resolution.prompt, **kwargs) if kwargs else text_generator(prompt_resolution.prompt)
            )
            if inspect.isawaitable(raw_response):
                raw_response = await raw_response
            candidates = _parse_llm_slot_candidates(str(raw_response or ""))
        except json.JSONDecodeError:
            diagnostics.append("llm_invalid_json")
        except Exception:
            diagnostics.append("llm_failed")
        else:
            for name, candidate in candidates.items():
                spec = eligible_specs.get(name)
                if spec is None:
                    diagnostics.append("llm_rejected_unknown_parameter")
                    continue
                if name in payload:
                    continue
                accepted = _validate_llm_candidate(spec, candidate)
                if accepted is None:
                    diagnostics.append("llm_rejected_invalid_value")
                    continue
                value, source = accepted
                payload[name] = value
                sources[name] = source

    unresolved_names = tuple(name for name in manifest.parameters if name not in payload)
    _resolve_text_inputs(manifest.parameters, payload, sources, context, names=unresolved_names)

    missing = _current_missing_fields(
        manifest.parameters,
        payload,
        base_payload,
        active_slot_collection=active_slot_collection,
    )
    return SkillInputResolutionResult(
        payload=payload,
        missing=missing,
        sources=sources,
        diagnostics=_dedupe(tuple(diagnostics)),
        prompt_profile=prompt_profile,
    )


def _source_allowed(spec: SkillParameterSpec, source: str) -> bool:
    return not spec.sources or source in spec.sources


def _resolve_from_artifacts(
    spec: SkillParameterSpec,
    payload: Mapping[str, Any],
    context: SkillInputResolutionContext,
) -> dict[str, Any] | None:
    if not _artifact_requested(spec):
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


def _artifact_requested(spec: SkillParameterSpec) -> bool:
    return spec.type in {"artifact", "file", "data"} or "artifact" in spec.sources


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
    if (
        _source_allowed(spec, "resolved_user_message")
        and context.resolved_user_message
        and context.resolved_user_message not in {context.query, context.current_user_message}
    ):
        candidates.append(("resolved_user_message", context.resolved_user_message, "high"))
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
        if spec.type in {"int", "integer"}:
            raw_value = _match_integer_phrase_from_terms(spec, text)
            if raw_value is not None:
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


def _match_integer_phrase_from_terms(spec: SkillParameterSpec, text: str) -> str | None:
    terms = tuple(dict.fromkeys(str(term).strip() for term in (spec.name, *spec.aliases) if str(term).strip()))
    if not terms:
        return None
    escaped = "|".join(re.escape(term) for term in terms)
    patterns = (
        rf"(?:{escaped})\s*(?::|：|=|是|为|就是)?\s*({_INTEGER_PHRASE_TOKEN_RE})\s*(?:个|次|遍|轮)?",
        rf"({_INTEGER_PHRASE_TOKEN_RE})\s*(?:个|次|遍|轮)?\s*(?:{escaped})",
    )
    for pattern in patterns:
        raw_value = _match_pattern(pattern, text)
        if raw_value is not None:
            return raw_value
    return None


def _coerce_value(value: Any, spec: SkillParameterSpec) -> Any | None:
    if value is None or isinstance(value, bool):
        return None
    if spec.type in {"int", "integer"}:
        return _parse_positive_int(value)
    if spec.type in {"number", "float"}:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None
    text = str(value).strip()
    return text or None


def _parse_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if value.is_integer() and value > 0:
            return int(value)
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\+?\d+", text):
        parsed = int(text.lstrip("+") or "0")
        return parsed if parsed > 0 else None
    if re.search(r"[-−]\s*\d", text) or re.search(r"\d+\s*\.\s*\d+", text):
        return None
    arabic = re.search(r"(?<![\d.])(\d+)(?![\d.])", text)
    if arabic is not None:
        parsed = int(arabic.group(1))
        return parsed if parsed > 0 else None
    chinese = re.search(rf"[{_CHINESE_INTEGER_TOKEN}]+", text)
    if chinese is None:
        return None
    parsed = _parse_chinese_positive_int_token(chinese.group(0))
    return parsed if parsed is not None and parsed > 0 else None


def _parse_chinese_positive_int_token(token: str) -> int | None:
    digit_map = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "壹": 1,
        "贰": 2,
        "叁": 3,
        "肆": 4,
        "伍": 5,
        "陆": 6,
        "柒": 7,
        "捌": 8,
        "玖": 9,
    }
    unit_map = {
        "十": 10,
        "拾": 10,
        "百": 100,
        "佰": 100,
        "千": 1000,
        "仟": 1000,
    }
    if not token or any(char not in digit_map and char not in unit_map and char not in {"万", "萬"} for char in token):
        return None
    if not any(char in unit_map or char in {"万", "萬"} for char in token):
        digits = [str(digit_map[char]) for char in token]
        parsed = int("".join(digits)) if digits else 0
        return parsed if parsed > 0 else None

    total = 0
    section = 0
    number = 0
    for char in token:
        if char in digit_map:
            number = digit_map[char]
            continue
        if char in unit_map:
            unit = unit_map[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
            continue
        if char in {"万", "萬"}:
            section += number
            if section == 0:
                section = 1
            total += section * 10000
            section = 0
            number = 0
    parsed = total + section + number
    return parsed if parsed > 0 else None


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
    active_slot_collection: Mapping[str, Any] | None = None,
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
                "required_now": True,
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
            "active_slot_collection": _safe_slot_collection(active_slot_collection),
        },
    }
    return (
        "你是一个受限的 Skill 参数补槽器。只根据给定上下文抽取缺失参数，禁止编造文件、数据或未声明字段。\n"
        "parameters_to_resolve 是当前必须补齐的字段；如果存在 active_slot_collection，"
        "以 active_slot_collection.missing 为本轮补槽权威，不要根据 manifest.required 判断是否抽取。\n"
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
    active_slot_collection: Mapping[str, Any] | None = None,
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
        active_slot_collection=active_slot_collection,
    )
    public_skill_schema = {
        "name": manifest.name,
        "description": manifest.description,
        "parameters_to_resolve": [
            {
                "name": spec.name,
                "type": spec.type,
                "required_now": True,
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
        "active_slot_collection": _safe_slot_collection(active_slot_collection),
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
                    "parameters_to_resolve 是当前必须补齐的字段；如果存在 active_slot_collection，"
                    "以 active_slot_collection.missing 为本轮补槽权威，不要根据 manifest.required 判断是否抽取。"
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


def _active_slot_collection(base_payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = base_payload.get("metadata")
    return _metadata_slot_collection(metadata)


def _metadata_slot_collection(metadata: Any) -> Mapping[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    collection = metadata.get(SLOT_COLLECTION_METADATA_KEY)
    return collection if isinstance(collection, Mapping) else None


def _active_slot_missing_fields(
    parameters: Mapping[str, SkillParameterSpec],
    payload: Mapping[str, Any],
    base_payload: Mapping[str, Any],
    *,
    active_slot_collection: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    collection = active_slot_collection or _active_slot_collection(base_payload)
    if collection is None:
        return ()
    names = _string_tuple(collection.get("missing"))
    return tuple(name for name in names if name in parameters and name not in payload)


def _manifest_required_missing_fields(
    parameters: Mapping[str, SkillParameterSpec],
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    return tuple(name for name, spec in parameters.items() if spec.required and name not in payload)


def _llm_target_missing_fields(
    parameters: Mapping[str, SkillParameterSpec],
    payload: Mapping[str, Any],
    base_payload: Mapping[str, Any],
    *,
    active_slot_collection: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    slot_missing = _active_slot_missing_fields(
        parameters,
        payload,
        base_payload,
        active_slot_collection=active_slot_collection,
    )
    if slot_missing:
        return slot_missing
    return _manifest_required_missing_fields(parameters, payload)


def _current_missing_fields(
    parameters: Mapping[str, SkillParameterSpec],
    payload: Mapping[str, Any],
    base_payload: Mapping[str, Any],
    *,
    active_slot_collection: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    return _dedupe(
        (
            *_manifest_required_missing_fields(parameters, payload),
            *_active_slot_missing_fields(
                parameters,
                payload,
                base_payload,
                active_slot_collection=active_slot_collection,
            ),
        )
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        values: tuple[Any, ...] = ()
    elif isinstance(value, str):
        values = (value,)
    elif isinstance(value, list | tuple | set):
        values = tuple(value)
    else:
        values = (value,)
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _safe_slot_collection(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    safe: dict[str, Any] = {}
    for key in (
        "schema_version",
        "collection_id",
        "round",
        "status",
        "missing",
        "ask_fields",
        "last_question",
        "no_progress_rounds",
    ):
        raw = value.get(key)
        if isinstance(raw, str | int | float | bool) or raw is None:
            safe[key] = raw
        elif isinstance(raw, list | tuple):
            safe[key] = [item for item in raw if isinstance(item, str | int | float | bool) or item is None][:20]
    resolved = value.get("resolved")
    if isinstance(resolved, Mapping):
        safe["resolved"] = _safe_resolved_payload(resolved)
    return safe


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
