from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

TokenEstimator = Callable[[str], int]

_DEFAULT_TRIM_MAX_TOKENS = 8_000
_INPUT_BUDGET_RATIO = 0.75
_TRUSTED_MARGIN_RATIO = 0.01
_FALLBACK_MARGIN_RATIO = 0.02
_TRUSTED_MIN_MARGIN = 1_024
_FALLBACK_MIN_MARGIN = 2_048

_SECURITY_ROLE_ORDER: dict[str, int] = {
    "instruction": 0,
    "tool_rule": 10,
    "tool_profile": 20,
    "tool_schema": 30,
    "history": 40,
    "tool_result": 50,
    "active_note": 60,
    "user_input": 70,
    "guard": 80,
}


class PromptEnvelopeRenderError(RuntimeError):
    """Raised when a PromptEnvelope cannot be rendered without violating budget rules."""

    def __init__(self, reason: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.reason = reason
        self.details = dict(details or {})
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class PromptSegment:
    name: str
    role: str
    content: str
    priority: int
    mutability: str
    cache_affinity: str
    trim_policy: str
    security_role: str
    token_estimate: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromptEnvelope:
    template_id: str
    template_version: str
    model_edition: str | None
    trim_max_tokens: int | None
    segments: tuple[PromptSegment, ...]


@dataclass(frozen=True, slots=True)
class PromptSegmentAudit:
    name: str
    role: str
    security_role: str
    tokens_before: int
    tokens_after: int
    trimmed: bool
    trim_reason: str | None = None
    content_hash: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromptRenderAudit:
    template_id: str
    template_version: str
    trim_max_tokens: int
    trim_max_tokens_source: str
    token_estimator: str
    safety_margin_tokens: int
    final_input_token_budget: int
    final_input_tokens: int
    preflight_retry_count: int
    history_compression_retry: bool
    cacheable_prefix_hash: str
    cacheable_prefix_tokens: int
    first_dynamic_segment: str | None
    non_history_tokens: int
    bulk_history_budget: int
    bulk_history_tokens_used: int
    candidate_history_tokens: int
    memory_candidate_count: int
    history_truncated: bool
    segments: tuple[PromptSegmentAudit, ...]


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    prompt: str
    audit: PromptRenderAudit


@dataclass(frozen=True, slots=True)
class RenderedMessages:
    messages: tuple[Mapping[str, str], ...]
    audit: PromptRenderAudit


def render_prompt_envelope(
    envelope: PromptEnvelope,
    *,
    token_estimator: TokenEstimator | None = None,
    token_estimator_is_fallback: bool = False,
) -> RenderedPrompt:
    estimator = token_estimator or _default_char_token_estimator
    estimator_name = "fallback" if token_estimator_is_fallback or token_estimator is None else "trusted"
    trim_max_tokens, trim_max_tokens_source = _normalize_trim_max_tokens(envelope.trim_max_tokens)
    final_input_token_budget = math.floor(trim_max_tokens * _INPUT_BUDGET_RATIO)
    safety_margin_tokens = _safety_margin(trim_max_tokens, fallback=(estimator_name == "fallback"))

    rendered = _render_once(
        envelope,
        estimator=estimator,
        trim_max_tokens=trim_max_tokens,
        trim_max_tokens_source=trim_max_tokens_source,
        estimator_name=estimator_name,
        safety_margin_tokens=safety_margin_tokens,
        final_input_token_budget=final_input_token_budget,
        preflight_retry_count=0,
        history_compression_retry=False,
        history_budget_override=None,
    )
    if rendered.audit.final_input_tokens <= final_input_token_budget:
        return rendered

    retry_history_budget = max(
        0,
        rendered.audit.bulk_history_budget
        - (rendered.audit.final_input_tokens - final_input_token_budget)
        - safety_margin_tokens,
    )
    retried = _render_once(
        envelope,
        estimator=estimator,
        trim_max_tokens=trim_max_tokens,
        trim_max_tokens_source=trim_max_tokens_source,
        estimator_name=estimator_name,
        safety_margin_tokens=safety_margin_tokens,
        final_input_token_budget=final_input_token_budget,
        preflight_retry_count=1,
        history_compression_retry=True,
        history_budget_override=retry_history_budget,
    )
    if retried.audit.final_input_tokens <= final_input_token_budget:
        return retried
    raise PromptEnvelopeRenderError(
        "final_input_over_budget",
        details={
            "final_input_tokens": retried.audit.final_input_tokens,
            "final_input_token_budget": final_input_token_budget,
            "preflight_retry_count": 1,
        },
    )


def _render_once(
    envelope: PromptEnvelope,
    *,
    estimator: TokenEstimator,
    trim_max_tokens: int,
    trim_max_tokens_source: str,
    estimator_name: str,
    safety_margin_tokens: int,
    final_input_token_budget: int,
    preflight_retry_count: int,
    history_compression_retry: bool,
    history_budget_override: int | None,
) -> RenderedPrompt:
    ordered_segments = _ordered_segments(envelope.segments)
    segment_tokens_before = {segment.name: _count_tokens(estimator, segment.content) for segment in ordered_segments}
    required_total_tokens = sum(
        segment_tokens_before[segment.name]
        for segment in ordered_segments
        if segment.trim_policy == "required"
    )
    required_non_history_tokens = sum(
        segment_tokens_before[segment.name]
        for segment in ordered_segments
        if segment.trim_policy == "required" and not _is_history_segment(segment)
    )
    if required_total_tokens > final_input_token_budget:
        raise PromptEnvelopeRenderError(
            "required_segments_over_budget",
            details={
                "required_tokens": required_total_tokens,
                "required_non_history_tokens": required_non_history_tokens,
                "final_input_token_budget": final_input_token_budget,
            },
        )

    computed_bulk_history_budget = max(0, final_input_token_budget - required_non_history_tokens - safety_margin_tokens)
    bulk_history_budget = computed_bulk_history_budget if history_budget_override is None else max(0, history_budget_override)
    history_budget_remaining = bulk_history_budget
    flexible_budget_remaining = max(0, final_input_token_budget - required_non_history_tokens)

    rendered_parts: list[str] = []
    segment_audits: list[PromptSegmentAudit] = []
    included_by_name: dict[str, str] = {}
    bulk_history_tokens_used = 0
    candidate_history_tokens = 0
    memory_candidate_count = 0
    history_truncated = False
    non_history_tokens = 0

    for segment in ordered_segments:
        tokens_before = segment_tokens_before[segment.name]
        rendered_content = segment.content
        trim_reason: str | None = None

        if segment.trim_policy == "required":
            tokens_after = tokens_before
        elif _is_history_segment(segment):
            available = min(history_budget_remaining, flexible_budget_remaining)
            rendered_content, tokens_after = _apply_trim_policy(
                segment,
                available,
                estimator=estimator,
                history_context=True,
            )
            history_budget_remaining = max(0, history_budget_remaining - tokens_after)
            flexible_budget_remaining = max(0, flexible_budget_remaining - tokens_after)
            if tokens_after < tokens_before:
                trim_reason = _history_trim_reason(segment)
                history_truncated = True
        else:
            available = flexible_budget_remaining
            rendered_content, tokens_after = _apply_trim_policy(
                segment,
                available,
                estimator=estimator,
                history_context=False,
            )
            flexible_budget_remaining = max(0, flexible_budget_remaining - tokens_after)
            if tokens_after < tokens_before:
                trim_reason = _non_history_trim_reason(segment)

        if tokens_after > 0 and rendered_content:
            rendered_parts.append(rendered_content)
            included_by_name[segment.name] = rendered_content

        if _is_history_segment(segment):
            bulk_history_tokens_used += tokens_after
            candidate_history_tokens += _safe_int(segment.metadata.get("candidate_history_tokens"))
            memory_candidate_count += _safe_int(segment.metadata.get("memory_candidate_count"))
        else:
            non_history_tokens += tokens_after

        audit_metadata = _safe_segment_audit_metadata(segment.metadata)
        segment_audits.append(
            PromptSegmentAudit(
                name=segment.name,
                role=segment.role,
                security_role=segment.security_role,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                trimmed=tokens_after < tokens_before,
                trim_reason=trim_reason,
                content_hash=_content_hash(segment.content),
                metadata=audit_metadata,
            )
        )

    prompt = "\n\n".join(rendered_parts)
    final_input_tokens = _count_tokens(estimator, prompt)
    cacheable_prefix_hash, cacheable_prefix_tokens = _cacheable_prefix(
        ordered_segments,
        included_by_name=included_by_name,
        estimator=estimator,
    )
    audit = PromptRenderAudit(
        template_id=envelope.template_id,
        template_version=envelope.template_version,
        trim_max_tokens=trim_max_tokens,
        trim_max_tokens_source=trim_max_tokens_source,
        token_estimator=estimator_name,
        safety_margin_tokens=safety_margin_tokens,
        final_input_token_budget=final_input_token_budget,
        final_input_tokens=final_input_tokens,
        preflight_retry_count=preflight_retry_count,
        history_compression_retry=history_compression_retry,
        cacheable_prefix_hash=cacheable_prefix_hash,
        cacheable_prefix_tokens=cacheable_prefix_tokens,
        first_dynamic_segment=_first_dynamic_segment_name(ordered_segments),
        non_history_tokens=non_history_tokens,
        bulk_history_budget=bulk_history_budget,
        bulk_history_tokens_used=bulk_history_tokens_used,
        candidate_history_tokens=candidate_history_tokens,
        memory_candidate_count=memory_candidate_count,
        history_truncated=history_truncated,
        segments=tuple(segment_audits),
    )
    return RenderedPrompt(prompt=prompt, audit=audit)


def _ordered_segments(segments: tuple[PromptSegment, ...]) -> tuple[PromptSegment, ...]:
    return tuple(
        sorted(
            segments,
            key=lambda segment: (
                _SECURITY_ROLE_ORDER.get(segment.security_role, 50),
                segment.priority,
                segment.name,
            ),
        )
    )


def _apply_trim_policy(
    segment: PromptSegment,
    available_budget: int,
    *,
    estimator: TokenEstimator,
    history_context: bool,
) -> tuple[str, int]:
    available_budget = max(0, available_budget)
    tokens_before = _count_tokens(estimator, segment.content)
    if tokens_before <= available_budget:
        return segment.content, tokens_before
    if available_budget == 0 or segment.trim_policy == "drop_if_needed":
        return "", 0
    if segment.trim_policy == "drop_oldest" or history_context:
        return _fit_content(segment.content, available_budget, estimator=estimator, keep="suffix")
    if segment.trim_policy == "compressible":
        return _fit_content(segment.content, available_budget, estimator=estimator, keep="prefix")
    return "", 0


def _fit_content(content: str, budget: int, *, estimator: TokenEstimator, keep: str) -> tuple[str, int]:
    if budget <= 0 or not content:
        return "", 0
    low = 0
    high = len(content)
    best = ""
    best_tokens = 0
    while low <= high:
        length = (low + high) // 2
        candidate = content[-length:] if keep == "suffix" and length else content[:length]
        tokens = _count_tokens(estimator, candidate)
        if tokens <= budget:
            best = candidate
            best_tokens = tokens
            low = length + 1
        else:
            high = length - 1
    return best.strip(), _count_tokens(estimator, best.strip()) if best.strip() else 0


def _history_trim_reason(segment: PromptSegment) -> str:
    if segment.trim_policy == "compressible":
        return "compressible_to_bulk_history_budget"
    if segment.trim_policy == "drop_if_needed":
        return "drop_if_needed_for_bulk_history_budget"
    return "drop_oldest_to_bulk_history_budget"


def _non_history_trim_reason(segment: PromptSegment) -> str:
    if segment.trim_policy == "compressible":
        return "compressible_to_available_budget"
    if segment.trim_policy == "drop_oldest":
        return "drop_oldest_to_available_budget"
    return "drop_if_needed_for_available_budget"


def _cacheable_prefix(
    segments: tuple[PromptSegment, ...],
    *,
    included_by_name: Mapping[str, str],
    estimator: TokenEstimator,
) -> tuple[str, int]:
    chunks: list[str] = []
    token_count = 0
    for segment in segments:
        content = included_by_name.get(segment.name)
        if content is None:
            continue
        if segment.cache_affinity == "prefix" and segment.mutability == "stable":
            chunks.append(f"{segment.name}:{segment.role}:{_content_hash(content)}")
            token_count += _count_tokens(estimator, content)
    joined = "\n".join(chunks)
    return _content_hash(joined), token_count


def _first_dynamic_segment_name(segments: tuple[PromptSegment, ...]) -> str | None:
    for segment in segments:
        if segment.mutability != "stable":
            return segment.name
    return None


_SAFE_SEGMENT_AUDIT_METADATA_KEYS = frozenset(
    {
        "candidate_history_tokens",
        "memory_candidate_count",
        "candidate_kinds",
        "candidate_priority_min",
        "candidate_priority_max",
        "candidate_trim_policies",
    }
)


def _safe_segment_audit_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in metadata.items():
        key_text = str(key)
        if key_text not in _SAFE_SEGMENT_AUDIT_METADATA_KEYS:
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            safe[key_text] = value
            continue
        if isinstance(value, list | tuple):
            projected = tuple(item for item in value if isinstance(item, str | int | float | bool))
            if projected:
                safe[key_text] = projected
    return safe


def _safe_int(value: object) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _normalize_trim_max_tokens(trim_max_tokens: int | None) -> tuple[int, str]:
    if trim_max_tokens is None or trim_max_tokens <= 0:
        return _DEFAULT_TRIM_MAX_TOKENS, "default_8000"
    return int(trim_max_tokens), "envelope"


def _safety_margin(trim_max_tokens: int, *, fallback: bool) -> int:
    if fallback:
        return max(_FALLBACK_MIN_MARGIN, math.floor(trim_max_tokens * _FALLBACK_MARGIN_RATIO))
    return max(_TRUSTED_MIN_MARGIN, math.floor(trim_max_tokens * _TRUSTED_MARGIN_RATIO))


def _count_tokens(estimator: TokenEstimator, text: str) -> int:
    return max(0, int(estimator(str(text))))


def _default_char_token_estimator(text: str) -> int:
    text = str(text)
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _content_hash(content: str) -> str:
    digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _is_history_segment(segment: PromptSegment) -> bool:
    return segment.security_role == "history" or segment.name == "bulk_conversation_history"
