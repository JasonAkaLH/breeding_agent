from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .prompt_envelope import (
    LLMMessage,
    PromptEnvelope,
    PromptEnvelopeRenderError,
    PromptSegment,
    RenderedPrompt,
    RenderedMessages,
    TokenEstimator,
    render_prompt_envelope,
    render_prompt_envelope_messages,
)

PromptProfileMode = Literal["off", "shadow", "string", "messages"]

PROMPT_PROFILE_MODE_ENV = "MAF_PROMPT_ENVELOPE_MODE"
PROMPT_PROFILE_TEMPLATE_VERSION = "p5-multi-call-v1"


@dataclass(frozen=True, slots=True)
class PromptProfileResolution:
    prompt: str | tuple[LLMMessage, ...]
    mode: PromptProfileMode
    effective_mode: PromptProfileMode
    rendered: RenderedPrompt | RenderedMessages | None
    audit_payload: dict[str, Any] | None
    llm_call_payload: dict[str, Any] | None


def resolve_prompt_profile_mode(value: str | None = None) -> PromptProfileMode:
    raw_value = value if value is not None else os.environ.get(PROMPT_PROFILE_MODE_ENV)
    mode = str(raw_value or "off").strip().lower()
    if mode in {"off", "shadow", "string", "messages"}:
        return mode  # type: ignore[return-value]
    return "off"


def render_profile_prompt(
    *,
    template_id: str,
    template_version: str = PROMPT_PROFILE_TEMPLATE_VERSION,
    segments: tuple[PromptSegment, ...],
    model_edition: str | None = None,
    trim_max_tokens: int | None = None,
    token_estimator: TokenEstimator | None = None,
    token_estimator_is_fallback: bool = False,
) -> RenderedPrompt:
    return render_prompt_envelope(
        PromptEnvelope(
            template_id=template_id,
            template_version=template_version,
            model_edition=model_edition,
            trim_max_tokens=trim_max_tokens,
            segments=segments,
        ),
        token_estimator=token_estimator,
        token_estimator_is_fallback=token_estimator_is_fallback,
    )


def render_profile_messages(
    *,
    template_id: str,
    template_version: str = PROMPT_PROFILE_TEMPLATE_VERSION,
    segments: tuple[PromptSegment, ...],
    model_edition: str | None = None,
    trim_max_tokens: int | None = None,
    role_capabilities: Mapping[str, Any] | tuple[str, ...] | None = None,
    token_estimator: TokenEstimator | None = None,
    token_estimator_is_fallback: bool = False,
) -> RenderedMessages:
    return render_prompt_envelope_messages(
        PromptEnvelope(
            template_id=template_id,
            template_version=template_version,
            model_edition=model_edition,
            trim_max_tokens=trim_max_tokens,
            segments=segments,
        ),
        role_capabilities=role_capabilities,
        token_estimator=token_estimator,
        token_estimator_is_fallback=token_estimator_is_fallback,
    )


def resolve_profile_prompt_for_mode(
    *,
    legacy_prompt: str,
    template_id: str,
    segments: tuple[PromptSegment, ...],
    template_version: str = PROMPT_PROFILE_TEMPLATE_VERSION,
    model_edition: str | None = None,
    trim_max_tokens: int | None = None,
    mode: str | None = None,
    token_estimator: TokenEstimator | None = None,
    token_estimator_is_fallback: bool = False,
    audit_context: Mapping[str, Any] | None = None,
    role_capabilities: Mapping[str, Any] | tuple[str, ...] | None = None,
) -> PromptProfileResolution:
    requested_mode = resolve_prompt_profile_mode(mode)
    if requested_mode == "off":
        return PromptProfileResolution(
            prompt=legacy_prompt,
            mode=requested_mode,
            effective_mode="off",
            rendered=None,
            audit_payload=None,
            llm_call_payload=None,
        )

    try:
        if requested_mode == "messages":
            rendered = render_profile_messages(
                template_id=template_id,
                template_version=template_version,
                segments=segments,
                model_edition=model_edition,
                trim_max_tokens=trim_max_tokens,
                role_capabilities=role_capabilities,
                token_estimator=token_estimator,
                token_estimator_is_fallback=token_estimator_is_fallback,
            )
        else:
            rendered = render_profile_prompt(
                template_id=template_id,
                template_version=template_version,
                segments=segments,
                model_edition=model_edition,
                trim_max_tokens=trim_max_tokens,
                token_estimator=token_estimator,
                token_estimator_is_fallback=token_estimator_is_fallback,
            )
    except PromptEnvelopeRenderError as exc:
        if requested_mode != "shadow":
            raise
        audit_payload = render_error_audit_payload(
            mode=requested_mode,
            effective_mode="shadow",
            template_id=template_id,
            template_version=template_version,
            reason=exc.reason,
            details=exc.details,
            audit_context=audit_context,
        )
        return PromptProfileResolution(
            prompt=legacy_prompt,
            mode=requested_mode,
            effective_mode="shadow",
            rendered=None,
            audit_payload=audit_payload,
            llm_call_payload=llm_call_payload_from_audit(audit_payload),
        )

    effective_mode: PromptProfileMode = "shadow" if requested_mode == "shadow" else requested_mode
    audit_payload = prompt_profile_audit_payload(
        rendered,
        mode=requested_mode,
        effective_mode=effective_mode,
        audit_context=audit_context,
        provider_role_capabilities=role_capabilities if requested_mode == "messages" else None,
    )
    return PromptProfileResolution(
        prompt=legacy_prompt if requested_mode == "shadow" else (rendered.messages if isinstance(rendered, RenderedMessages) else rendered.prompt),
        mode=requested_mode,
        effective_mode=effective_mode,
        rendered=rendered,
        audit_payload=audit_payload,
        llm_call_payload=llm_call_payload_from_audit(audit_payload),
    )


def prompt_profile_audit_payload(
    rendered: RenderedPrompt | RenderedMessages,
    *,
    mode: PromptProfileMode,
    effective_mode: PromptProfileMode,
    audit_context: Mapping[str, Any] | None = None,
    provider_role_capabilities: Mapping[str, Any] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    audit = rendered.audit
    payload: dict[str, Any] = {
        "status": "rendered",
        "mode": mode,
        "effective_mode": effective_mode,
        "template_id": audit.template_id,
        "template_version": audit.template_version,
        "trim_max_tokens": audit.trim_max_tokens,
        "trim_max_tokens_source": audit.trim_max_tokens_source,
        "token_estimator": audit.token_estimator,
        "safety_margin_tokens": audit.safety_margin_tokens,
        "final_input_token_budget": audit.final_input_token_budget,
        "final_input_tokens": audit.final_input_tokens,
        "preflight_retry_count": audit.preflight_retry_count,
        "history_compression_retry": audit.history_compression_retry,
        "cacheable_prefix_hash": audit.cacheable_prefix_hash,
        "cacheable_prefix_tokens": audit.cacheable_prefix_tokens,
        "first_dynamic_segment": audit.first_dynamic_segment,
        "non_history_tokens": audit.non_history_tokens,
        "bulk_history_budget": audit.bulk_history_budget,
        "bulk_history_tokens_used": audit.bulk_history_tokens_used,
        "candidate_history_tokens": audit.candidate_history_tokens,
        "memory_candidate_count": audit.memory_candidate_count,
        "history_truncated": audit.history_truncated,
        "role_fallbacks": [
            {
                "segment_name": fallback.segment_name,
                "source_role": fallback.source_role,
                "target_role": fallback.target_role,
                "reason": fallback.reason,
            }
            for fallback in audit.role_fallbacks
        ],
        "segments": [
            {
                "name": segment.name,
                "role": segment.role,
                "security_role": segment.security_role,
                "tokens_before": segment.tokens_before,
                "tokens_after": segment.tokens_after,
                "trimmed": segment.trimmed,
                "trim_reason": segment.trim_reason,
                "content_hash": segment.content_hash,
                "metadata": dict(segment.metadata),
            }
            for segment in audit.segments
        ],
    }
    safe_role_capabilities = _safe_role_capabilities(provider_role_capabilities)
    if safe_role_capabilities:
        payload["provider_role_capabilities"] = safe_role_capabilities
    safe_context = _safe_audit_context(audit_context or {})
    if safe_context:
        payload["context"] = safe_context
    return payload


def render_error_audit_payload(
    *,
    mode: PromptProfileMode,
    effective_mode: PromptProfileMode,
    template_id: str,
    template_version: str,
    reason: str,
    details: Mapping[str, Any],
    audit_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "render_failed",
        "mode": mode,
        "effective_mode": effective_mode,
        "template_id": template_id,
        "template_version": template_version,
        "error_reason": reason,
        "details": _safe_error_details(details),
        "segments": [],
    }
    safe_context = _safe_audit_context(audit_context or {})
    if safe_context:
        payload["context"] = safe_context
    return payload


def llm_call_payload_from_audit(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    keys = (
        "status",
        "mode",
        "effective_mode",
        "template_id",
        "template_version",
        "trim_max_tokens",
        "trim_max_tokens_source",
        "token_estimator",
        "safety_margin_tokens",
        "final_input_token_budget",
        "final_input_tokens",
        "preflight_retry_count",
        "history_compression_retry",
        "cacheable_prefix_hash",
        "cacheable_prefix_tokens",
        "first_dynamic_segment",
        "non_history_tokens",
        "bulk_history_budget",
        "bulk_history_tokens_used",
        "candidate_history_tokens",
        "memory_candidate_count",
        "history_truncated",
        "error_reason",
        "role_fallbacks",
        "provider_role_capabilities",
    )
    return {key: payload[key] for key in keys if key in payload}


def coerce_profile_trim_max_tokens(*values: Any) -> int | None:
    for value in values:
        parsed = _coerce_positive_int(value)
        if parsed is not None:
            return parsed
    return None


def optional_profile_kwargs(
    callable_obj: Callable[..., Any],
    *,
    prompt_profile: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    accepted: dict[str, Any] = {}
    try:
        import inspect

        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return accepted
    accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    candidates = {key: value for key, value in kwargs.items() if value is not None}
    if prompt_profile is not None:
        candidates["prompt_profile"] = prompt_profile
    for key, value in candidates.items():
        if accepts_kwargs or key in signature.parameters:
            accepted[key] = value
    return accepted


def _safe_error_details(details: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in details.items()
        if isinstance(value, str | int | float | bool) or value is None
    }


def _safe_audit_context(context: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in context.items():
        key_text = str(key)
        if isinstance(value, str | int | float | bool) or value is None:
            safe[key_text] = value
        elif isinstance(value, list | tuple):
            projected = [item for item in value if isinstance(item, str | int | float | bool) or item is None]
            if projected:
                safe[key_text] = projected[:32]
    return safe


def _safe_role_capabilities(value: Mapping[str, Any] | tuple[str, ...] | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        if "supports_messages" in value:
            safe["supports_messages"] = _truthy(value.get("supports_messages"))
        elif "messages_supported" in value:
            safe["supports_messages"] = _truthy(value.get("messages_supported"))
        roles = value.get("roles") or value.get("supported_roles") or value.get("message_roles") or value.get("supported_message_roles")
        role_list = _safe_role_list(roles)
        if role_list:
            safe["roles"] = role_list
        return safe
    role_list = _safe_role_list(value)
    return {"roles": role_list} if role_list else {}


def _safe_role_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = value.replace("\n", ",").split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        candidates = value
    else:
        return []
    return sorted({str(role).strip().lower() for role in candidates if str(role).strip()})


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "supported"}
    return bool(value)


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
