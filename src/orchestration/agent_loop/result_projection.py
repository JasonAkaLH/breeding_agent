from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.storage.agent_payload import AgentPayloadError, canonicalize_agent_payload

from .transient_results import (
    AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION,
    transient_skill_result_stage_ref,
)


MODEL_VIEW_MAX_CODE_POINTS = 20_000
MODEL_RESULT_MAX_BYTES = 80_000
SKILL_RESULT_PROJECTION_REVISION = "skill-result-v1"
MCP_RESULT_PROJECTION_REVISION = "mcp-result-v1"
DELEGATED_RESULT_PROJECTION_REVISION = "delegated-skill-instruction-v1"
SKILL_RESULT_PROJECTION_POLICY_LEGACY = "legacy"
SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY = (
    "full_inline_then_legacy"
)
SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT = (
    "full_inline_then_transient"
)
_SKILL_RESULT_PROJECTION_POLICIES = frozenset(
    {
        SKILL_RESULT_PROJECTION_POLICY_LEGACY,
        SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY,
        SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT,
    }
)
_RAW_MAX_DEPTH = 64
_RAW_MAX_NODES = 200_000
_SKILL_PREVIEW_KEYS = (
    "answer",
    "response_text",
    "summary",
    "search_summary",
    "status",
    "missing",
    "error",
    "files",
    "artifacts",
    "outputs",
)
_MCP_MODEL_KEYS = frozenset(
    {
        "agent_projection",
        "external_content_notice",
        "is_error",
        "mcp_status",
        "mcp_tool",
        "output_size_bytes",
        "safe_call_ref",
        "safe_remote_task_ref",
        "status",
        "text",
        "truncated",
    }
)
_FORBIDDEN_RAW_KEYS = frozenset(
    {
        "access_token",
        "activation_token",
        "api_key",
        "api_token",
        "authorization",
        "config",
        "credential",
        "credentials",
        "handler",
        "internal_path",
        "internal_source",
        "password",
        "raw_tool_arguments",
        "refresh_token",
        "runtime",
        "secret",
        "source_path",
        "storage_key",
        "storage_path",
        "storage_ref",
        "tool_arguments",
    }
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:access[_-]?token|api[_-]?(?:key|token)|authorization|credential|"
    r"password|refresh[_-]?token|secret)\s*[:=]\s*[^\s,;]+"
)


@dataclass(frozen=True, slots=True)
class AgentCallResultProjection:
    safe_result_payload: Mapping[str, Any] | None
    canonical_raw_bytes: bytes | None
    raw_sha256: str | None
    original_size_bytes: int
    projection_revision: str | None
    projection_mode: str | None
    projection_truncated: bool
    spill_required: bool
    spill_artifact_id: str | None
    error_code: str | None = None
    transient_stage_required: bool = False
    transient_stage_ref: str | None = None

    @property
    def accepted(self) -> bool:
        return self.error_code is None and self.safe_result_payload is not None


class AgentCallResultProjector:
    """Pure, deterministic Capability-result boundary for Agent persistence."""

    def project(
        self,
        *,
        capability_id: str,
        output_payload: Mapping[str, Any],
        call_item_id: str,
        outcome: str,
        safe_error_code: str | None,
        artifact_ids: Sequence[str] = (),
        continuation_locator: Mapping[str, Any] | None = None,
        skill_projection_policy: str = SKILL_RESULT_PROJECTION_POLICY_LEGACY,
    ) -> AgentCallResultProjection:
        try:
            raw_value = _strict_json_value(output_payload)
            canonical_raw = _canonical_json_bytes(raw_value)
        except (TypeError, ValueError):
            return _rejected("agent_result_invalid")
        raw_sha256 = hashlib.sha256(canonical_raw).hexdigest()

        if _is_delegated_model_result(raw_value):
            return self._delegated(
                raw_value=raw_value,
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
                call_item_id=call_item_id,
                outcome=outcome,
                safe_error_code=safe_error_code,
                artifact_ids=artifact_ids,
            )
        if capability_id == "mcp.dispatch" or capability_id.startswith("mcp."):
            return self._mcp(
                raw_value=raw_value,
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
                call_item_id=call_item_id,
                outcome=outcome,
                safe_error_code=safe_error_code,
                artifact_ids=artifact_ids,
                continuation_locator=continuation_locator,
            )
        return self._skill(
            capability_id=capability_id,
            raw_value=raw_value,
            canonical_raw=canonical_raw,
            raw_sha256=raw_sha256,
            call_item_id=call_item_id,
            outcome=outcome,
            safe_error_code=safe_error_code,
            artifact_ids=artifact_ids,
            continuation_locator=continuation_locator,
            skill_projection_policy=skill_projection_policy,
        )

    def _delegated(
        self,
        *,
        raw_value: dict[str, Any],
        canonical_raw: bytes,
        raw_sha256: str,
        call_item_id: str,
        outcome: str,
        safe_error_code: str | None,
        artifact_ids: Sequence[str],
    ) -> AgentCallResultProjection:
        if (
            raw_value.get("projection_revision")
            != DELEGATED_RESULT_PROJECTION_REVISION
            or raw_value.get("projection_mode") != "inline"
            or raw_value.get("projection_truncated") is not False
            or not isinstance(raw_value.get("model_view"), dict)
            or raw_value["model_view"].get("schema")
            != "maf.agent.delegated_skill_activation.v1"
        ):
            return _rejected(
                "agent_result_invalid",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        try:
            _validate_model_result(raw_value)
            _preflight_tool_result(
                call_item_id=call_item_id,
                outcome=outcome,
                safe_result=raw_value,
                safe_error_code=safe_error_code,
                artifact_ids=artifact_ids,
            )
        except (AgentPayloadError, ValueError):
            return _rejected(
                "agent_result_projection_too_large",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        return AgentCallResultProjection(
            safe_result_payload=raw_value,
            canonical_raw_bytes=canonical_raw,
            raw_sha256=raw_sha256,
            original_size_bytes=len(canonical_raw),
            projection_revision=DELEGATED_RESULT_PROJECTION_REVISION,
            projection_mode="inline",
            projection_truncated=False,
            spill_required=False,
            spill_artifact_id=None,
        )

    def _mcp(
        self,
        *,
        raw_value: dict[str, Any],
        canonical_raw: bytes,
        raw_sha256: str,
        call_item_id: str,
        outcome: str,
        safe_error_code: str | None,
        artifact_ids: Sequence[str],
        continuation_locator: Mapping[str, Any] | None,
    ) -> AgentCallResultProjection:
        model_view = {
            key: _sanitize_model_value(raw_value[key])
            for key in sorted(raw_value)
            if key in _MCP_MODEL_KEYS
        }
        model_view = {
            key: value for key, value in model_view.items() if value is not None
        }
        if continuation_locator is not None:
            model_view["continuation_locator"] = _strict_json_value(
                continuation_locator
            )
        truncated = bool(raw_value.get("truncated"))
        safe_result = _fit_inline_model_result(
            projection_revision=MCP_RESULT_PROJECTION_REVISION,
            model_view=model_view,
            original_size_bytes=len(canonical_raw),
            raw_sha256=raw_sha256,
            projection_truncated=truncated,
            shrink_text_keys=("text", "agent_projection"),
        )
        if safe_result is None:
            return _rejected(
                "agent_result_projection_too_large",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        try:
            _preflight_tool_result(
                call_item_id=call_item_id,
                outcome=outcome,
                safe_result=safe_result,
                safe_error_code=safe_error_code,
                artifact_ids=artifact_ids,
            )
        except AgentPayloadError:
            return _rejected(
                "agent_result_projection_too_large",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        return AgentCallResultProjection(
            safe_result_payload=safe_result,
            canonical_raw_bytes=canonical_raw,
            raw_sha256=raw_sha256,
            original_size_bytes=len(canonical_raw),
            projection_revision=MCP_RESULT_PROJECTION_REVISION,
            projection_mode="inline",
            projection_truncated=truncated,
            spill_required=False,
            spill_artifact_id=None,
        )

    def _skill(
        self,
        *,
        capability_id: str,
        raw_value: dict[str, Any],
        canonical_raw: bytes,
        raw_sha256: str,
        call_item_id: str,
        outcome: str,
        safe_error_code: str | None,
        artifact_ids: Sequence[str],
        continuation_locator: Mapping[str, Any] | None,
        skill_projection_policy: str,
    ) -> AgentCallResultProjection:
        if skill_projection_policy not in _SKILL_RESULT_PROJECTION_POLICIES:
            return _rejected(
                "agent_result_invalid",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        if skill_projection_policy != SKILL_RESULT_PROJECTION_POLICY_LEGACY:
            if (
                not capability_id.startswith("skill.")
                or outcome != "completed"
                or safe_error_code is not None
                or continuation_locator is not None
                or (
                    skill_projection_policy
                    == SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
                    and artifact_ids
                )
                or (
                    skill_projection_policy
                    == SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY
                    and not artifact_ids
                )
                or _contains_forbidden_raw_value(raw_value)
            ):
                return _rejected(
                    "agent_result_invalid",
                    canonical_raw=canonical_raw,
                    raw_sha256=raw_sha256,
                )
            try:
                full_result = build_model_result_envelope(
                    projection_revision=SKILL_RESULT_PROJECTION_REVISION,
                    projection_mode="inline",
                    model_view=raw_value,
                    original_size_bytes=len(canonical_raw),
                    raw_sha256=raw_sha256,
                    projection_truncated=False,
                )
                _preflight_tool_result(
                    call_item_id=call_item_id,
                    outcome=outcome,
                    safe_result=full_result,
                    safe_error_code=safe_error_code,
                    artifact_ids=artifact_ids,
                )
            except (AgentPayloadError, ValueError):
                full_result = None
            if full_result is not None:
                return AgentCallResultProjection(
                    safe_result_payload=full_result,
                    canonical_raw_bytes=canonical_raw,
                    raw_sha256=raw_sha256,
                    original_size_bytes=len(canonical_raw),
                    projection_revision=SKILL_RESULT_PROJECTION_REVISION,
                    projection_mode="inline",
                    projection_truncated=False,
                    spill_required=False,
                    spill_artifact_id=None,
                )
            if (
                skill_projection_policy
                == SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
            ):
                stage_ref = transient_skill_result_stage_ref(
                    call_item_id=call_item_id,
                    raw_sha256=raw_sha256,
                    projection_revision=(
                        AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
                    ),
                )
                try:
                    receipt = build_model_result_envelope(
                        projection_revision=(
                            AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
                        ),
                        projection_mode="transient_staged",
                        model_view={
                            "complete_result_pending_context_injection": True,
                            "schema": (
                                "maf.agent.transient_skill_result_receipt.v1"
                            ),
                            "stage_ref": stage_ref,
                        },
                        original_size_bytes=len(canonical_raw),
                        raw_sha256=raw_sha256,
                        projection_truncated=True,
                    )
                    _preflight_tool_result(
                        call_item_id=call_item_id,
                        outcome=outcome,
                        safe_result=receipt,
                        safe_error_code=safe_error_code,
                        artifact_ids=(),
                    )
                except (AgentPayloadError, ValueError):
                    return _rejected(
                        "agent_result_projection_too_large",
                        canonical_raw=canonical_raw,
                        raw_sha256=raw_sha256,
                    )
                return AgentCallResultProjection(
                    safe_result_payload=receipt,
                    canonical_raw_bytes=canonical_raw,
                    raw_sha256=raw_sha256,
                    original_size_bytes=len(canonical_raw),
                    projection_revision=(
                        AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
                    ),
                    projection_mode="transient_staged",
                    projection_truncated=True,
                    spill_required=False,
                    spill_artifact_id=None,
                    transient_stage_required=True,
                    transient_stage_ref=stage_ref,
                )
        model_view = _sanitize_model_value(raw_value)
        if not isinstance(model_view, dict):
            model_view = {}
        if continuation_locator is not None:
            try:
                model_view["continuation_locator"] = _strict_json_value(
                    continuation_locator
                )
            except (TypeError, ValueError):
                return _rejected(
                    "agent_result_invalid",
                    canonical_raw=canonical_raw,
                    raw_sha256=raw_sha256,
                )
        safe_result = _fit_inline_model_result(
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
            model_view=model_view,
            original_size_bytes=len(canonical_raw),
            raw_sha256=raw_sha256,
            projection_truncated=False,
        )
        if safe_result is not None:
            try:
                _preflight_tool_result(
                    call_item_id=call_item_id,
                    outcome=outcome,
                    safe_result=safe_result,
                    safe_error_code=safe_error_code,
                    artifact_ids=artifact_ids,
                )
            except AgentPayloadError:
                safe_result = None
        if safe_result is not None:
            return AgentCallResultProjection(
                safe_result_payload=safe_result,
                canonical_raw_bytes=canonical_raw,
                raw_sha256=raw_sha256,
                original_size_bytes=len(canonical_raw),
                projection_revision=SKILL_RESULT_PROJECTION_REVISION,
                projection_mode="inline",
                projection_truncated=False,
                spill_required=False,
                spill_artifact_id=None,
            )
        if _contains_forbidden_raw_value(raw_value):
            return _rejected(
                "agent_result_invalid",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        spill_artifact_id = skill_result_artifact_id(
            call_item_id=call_item_id,
            raw_sha256=raw_sha256,
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
        )
        preview = _skill_preview(raw_value)
        if continuation_locator is not None:
            preview["continuation_locator"] = _strict_json_value(
                continuation_locator
            )
        safe_result = _fit_inline_model_result(
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
            model_view=preview,
            original_size_bytes=len(canonical_raw),
            raw_sha256=raw_sha256,
            projection_truncated=True,
            projection_mode="artifact_backed",
        )
        if safe_result is None:
            return _rejected(
                "agent_result_projection_too_large",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        try:
            _preflight_tool_result(
                call_item_id=call_item_id,
                outcome=outcome,
                safe_result=safe_result,
                safe_error_code=safe_error_code,
                artifact_ids=(*artifact_ids, spill_artifact_id),
            )
        except AgentPayloadError:
            return _rejected(
                "agent_result_projection_too_large",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        return AgentCallResultProjection(
            safe_result_payload=safe_result,
            canonical_raw_bytes=canonical_raw,
            raw_sha256=raw_sha256,
            original_size_bytes=len(canonical_raw),
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
            projection_mode="artifact_backed",
            projection_truncated=True,
            spill_required=True,
            spill_artifact_id=spill_artifact_id,
        )


def build_model_result_envelope(
    *,
    projection_revision: str,
    projection_mode: str,
    model_view: Mapping[str, Any],
    original_size_bytes: int,
    raw_sha256: str,
    projection_truncated: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "maf.agent.model_result.v1",
        "projection_revision": projection_revision,
        "projection_mode": projection_mode,
        "model_view": dict(model_view),
        "original_size_bytes": original_size_bytes,
        "projected_size_bytes": 0,
        "raw_sha256": raw_sha256,
        "projection_truncated": projection_truncated,
    }
    for _ in range(4):
        size = canonicalize_agent_payload(result).size_bytes
        if result["projected_size_bytes"] == size:
            break
        result["projected_size_bytes"] = size
    canonical = canonicalize_agent_payload(result)
    if result["projected_size_bytes"] != canonical.size_bytes:
        raise ValueError("agent_result_projection_size_unstable")
    return result


def skill_result_artifact_id(
    *,
    call_item_id: str,
    raw_sha256: str,
    projection_revision: str,
) -> str:
    identity = hashlib.sha256(
        b"maf.agent.skill_result_artifact.v1\0"
        + call_item_id.encode("utf-8")
        + b"\0"
        + raw_sha256.encode("ascii")
        + b"\0"
        + projection_revision.encode("utf-8")
    ).hexdigest()
    return f"agent-skill-result:{identity}"


def _fit_inline_model_result(
    *,
    projection_revision: str,
    model_view: dict[str, Any],
    original_size_bytes: int,
    raw_sha256: str,
    projection_truncated: bool,
    projection_mode: str = "inline",
    shrink_text_keys: Sequence[str] = (),
) -> dict[str, Any] | None:
    candidate = dict(model_view)
    while True:
        try:
            result = build_model_result_envelope(
                projection_revision=projection_revision,
                projection_mode=projection_mode,
                model_view=candidate,
                original_size_bytes=original_size_bytes,
                raw_sha256=raw_sha256,
                projection_truncated=projection_truncated,
            )
            if _model_view_code_points(candidate) <= MODEL_VIEW_MAX_CODE_POINTS:
                _validate_model_result(result)
                return result
        except (AgentPayloadError, ValueError):
            pass
        shrink_key = next(
            (
                key
                for key in shrink_text_keys
                if isinstance(candidate.get(key), str) and candidate[key]
            ),
            None,
        )
        if shrink_key is None:
            return None
        value = candidate[shrink_key]
        assert isinstance(value, str)
        if len(value) <= 1:
            candidate.pop(shrink_key, None)
        else:
            candidate[shrink_key] = value[: len(value) // 2]


def _validate_model_result(result: Mapping[str, Any]) -> None:
    canonical = canonicalize_agent_payload(dict(result))
    if canonical.size_bytes > MODEL_RESULT_MAX_BYTES:
        raise ValueError("agent_result_projection_too_large")
    model_view = result.get("model_view")
    if not isinstance(model_view, Mapping) or _model_view_code_points(
        model_view
    ) > MODEL_VIEW_MAX_CODE_POINTS:
        raise ValueError("agent_result_projection_too_large")
    if result.get("projected_size_bytes") != canonical.size_bytes:
        raise ValueError("agent_result_projection_size_invalid")


def _preflight_tool_result(
    *,
    call_item_id: str,
    outcome: str,
    safe_result: Mapping[str, Any],
    safe_error_code: str | None,
    artifact_ids: Sequence[str],
) -> None:
    canonicalize_agent_payload(
        {
            "artifact_refs": list(artifact_ids),
            "call_item_id": call_item_id,
            "outcome": outcome,
            "safe_result": dict(safe_result),
            "safe_error_code": safe_error_code,
        }
    )


def _strict_json_value(value: Any, *, _depth: int = 0, _counter: list[int] | None = None) -> Any:
    if _counter is None:
        _counter = [0]
    _counter[0] += 1
    if _counter[0] > _RAW_MAX_NODES or _depth > _RAW_MAX_DEPTH:
        raise ValueError("agent_result_json_complexity_invalid")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("agent_result_non_finite_number")
        return value
    if isinstance(value, str):
        _validate_unicode(value)
        return value
    if isinstance(value, (list, tuple)):
        return [
            _strict_json_value(item, _depth=_depth + 1, _counter=_counter)
            for item in value
        ]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("agent_result_non_string_key")
            _validate_unicode(key)
            normalized[key] = _strict_json_value(
                child, _depth=_depth + 1, _counter=_counter
            )
        return {key: normalized[key] for key in sorted(normalized)}
    raise TypeError("agent_result_unsupported_json_type")


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _validate_unicode(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("agent_result_surrogate_invalid")


def _sanitize_model_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key in sorted(value):
            if _is_forbidden_key(key):
                continue
            child = _sanitize_model_value(value[key])
            if child is not None:
                sanitized[key] = child
        return sanitized
    if isinstance(value, list):
        return [
            child
            for item in value
            if (child := _sanitize_model_value(item)) is not None
        ]
    if isinstance(value, str) and _SECRET_ASSIGNMENT_RE.search(value):
        return None
    return value


def _contains_forbidden_raw_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _is_forbidden_key(key) or _contains_forbidden_raw_value(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_raw_value(item) for item in value)
    return isinstance(value, str) and _SECRET_ASSIGNMENT_RE.search(value) is not None


def _is_forbidden_key(key: str) -> bool:
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if normalized in _FORBIDDEN_RAW_KEYS:
        return True
    return normalized.endswith(
        ("_password", "_secret", "_api_key", "_access_token", "_refresh_token")
    )


def _skill_preview(raw_value: dict[str, Any]) -> dict[str, Any]:
    summary_fields: dict[str, Any] = {}
    for key in _SKILL_PREVIEW_KEYS:
        if key not in raw_value or _is_forbidden_key(key):
            continue
        value = _sanitize_model_value(raw_value[key])
        if _is_small_preview_value(value):
            summary_fields[key] = value
    return {
        "schema": "maf.agent.skill_result_preview.v1",
        "summary_fields": summary_fields,
        "available_top_level_fields": [
            key for key in sorted(raw_value) if not _is_forbidden_key(key)
        ][:128],
        "complete_result_available_as_artifact": True,
    }


def _is_small_preview_value(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return len(value) <= 8_000
    if isinstance(value, list):
        return len(value) <= 16 and _model_view_code_points({"value": value}) <= 8_000
    if isinstance(value, dict):
        return len(value) <= 32 and _model_view_code_points({"value": value}) <= 8_000
    return False


def _model_view_code_points(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _is_delegated_model_result(value: Mapping[str, Any]) -> bool:
    return (
        value.get("schema") == "maf.agent.model_result.v1"
        and value.get("projection_revision")
        == DELEGATED_RESULT_PROJECTION_REVISION
    )


def _rejected(
    error_code: str,
    *,
    canonical_raw: bytes | None = None,
    raw_sha256: str | None = None,
) -> AgentCallResultProjection:
    return AgentCallResultProjection(
        safe_result_payload=None,
        canonical_raw_bytes=None,
        raw_sha256=raw_sha256,
        original_size_bytes=len(canonical_raw) if canonical_raw is not None else 0,
        projection_revision=None,
        projection_mode=None,
        projection_truncated=False,
        spill_required=False,
        spill_artifact_id=None,
        error_code=error_code,
    )
