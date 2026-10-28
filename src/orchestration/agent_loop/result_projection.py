from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.storage.agent_payload import AgentPayloadError, canonicalize_agent_payload

from .transient_results import (
    AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION,
    transient_skill_result_stage_ref,
)


SKILL_RESULT_PROJECTION_REVISION = "skill-result-v1"
MCP_RESULT_PROJECTION_REVISION = "mcp-result-v1"
MCP_AGENT_RESULT_BUNDLE_SCHEMA = "maf.mcp.agent_result_bundle.v1"
DELEGATED_RESULT_PROJECTION_REVISION = "delegated-skill-instruction-v1"
TOOL_RESULT_REUSE_RECEIPT_SCHEMA = "maf.agent.tool_result_reuse_receipt.v1"
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
def agent_tool_repeat_key(*, capability_id: str, arguments_json: str) -> str:
    if not capability_id.strip() or not arguments_json:
        raise ValueError("agent_tool_repeat_identity_invalid")
    return f"{capability_id}\0{arguments_json}"


def build_tool_result_reuse_receipt(
    *,
    source_result_item_id: str,
    source_result_payload_sha256: str,
) -> dict[str, str]:
    if (
        not source_result_item_id.strip()
        or len(source_result_payload_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in source_result_payload_sha256
        )
    ):
        raise ValueError("agent_reused_tool_result_unavailable")
    return {
        "schema": TOOL_RESULT_REUSE_RECEIPT_SCHEMA,
        "source_result_item_id": source_result_item_id,
        "source_result_payload_sha256": source_result_payload_sha256,
    }


def parse_tool_result_reuse_receipt(
    value: object,
) -> tuple[str, str] | None:
    if not isinstance(value, Mapping) or value.get("schema") != TOOL_RESULT_REUSE_RECEIPT_SCHEMA:
        return None
    if set(value) != {
        "schema",
        "source_result_item_id",
        "source_result_payload_sha256",
    }:
        raise ValueError("agent_reused_tool_result_unavailable")
    source_result_item_id = value.get("source_result_item_id")
    source_result_payload_sha256 = value.get("source_result_payload_sha256")
    if (
        not isinstance(source_result_item_id, str)
        or not source_result_item_id.strip()
        or not isinstance(source_result_payload_sha256, str)
        or len(source_result_payload_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in source_result_payload_sha256
        )
    ):
        raise ValueError("agent_reused_tool_result_unavailable")
    return source_result_item_id, source_result_payload_sha256


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
    transient_content_bytes: bytes | None = None
    transient_content_sha256: str | None = None
    spill_content_bytes: bytes | None = None
    spill_content_sha256: str | None = None

    @property
    def accepted(self) -> bool:
        return self.error_code is None and self.safe_result_payload is not None


class AgentCallResultProjector:
    """Model-bound Capability-result boundary for Agent persistence."""

    def __init__(
        self,
        *,
        tokenization_config: Mapping[str, Any] | None = None,
        token_budgeter: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        from src.integrations.token_counter import (
            TOOL_RESULT_BUSINESS_MAX_TOKENS,
            truncate_text_to_token_budget_async,
        )

        self._tokenization_config = dict(tokenization_config or {})
        self._token_budgeter = (
            token_budgeter or truncate_text_to_token_budget_async
        )
        self._max_business_tokens = TOOL_RESULT_BUSINESS_MAX_TOKENS

    async def project(
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
        model_edition: str,
    ) -> AgentCallResultProjection:
        try:
            raw_value = _strict_json_value(output_payload)
            canonical_raw = _canonical_json_bytes(raw_value)
        except (TypeError, ValueError):
            return _rejected("agent_result_invalid")
        raw_sha256 = hashlib.sha256(canonical_raw).hexdigest()

        if _is_delegated_model_result(raw_value):
            return await self._delegated(
                raw_value=raw_value,
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
                call_item_id=call_item_id,
                outcome=outcome,
                safe_error_code=safe_error_code,
                artifact_ids=artifact_ids,
                model_edition=model_edition,
            )
        if capability_id == "mcp.dispatch" or capability_id.startswith("mcp."):
            return await self._mcp(
                raw_value=raw_value,
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
                call_item_id=call_item_id,
                outcome=outcome,
                safe_error_code=safe_error_code,
                artifact_ids=artifact_ids,
                continuation_locator=continuation_locator,
                model_edition=model_edition,
            )
        return await self._skill(
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
            model_edition=model_edition,
        )

    async def _delegated(
        self,
        *,
        raw_value: dict[str, Any],
        canonical_raw: bytes,
        raw_sha256: str,
        call_item_id: str,
        outcome: str,
        safe_error_code: str | None,
        artifact_ids: Sequence[str],
        model_edition: str,
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
        model_view = dict(raw_value["model_view"])
        truncated = False
        if outcome == "completed" and safe_error_code is None:
            model_view, truncated = await self._budget_model_view(
                model_view,
                model_edition=model_edition,
            )
        projected = build_model_result_envelope(
            projection_revision=DELEGATED_RESULT_PROJECTION_REVISION,
            projection_mode="inline",
            model_view=model_view,
            original_size_bytes=len(canonical_raw),
            raw_sha256=raw_sha256,
            projection_truncated=truncated,
        )
        try:
            _validate_model_result(projected)
            _preflight_tool_result(
                call_item_id=call_item_id,
                outcome=outcome,
                safe_result=projected,
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
            safe_result_payload=projected,
            canonical_raw_bytes=canonical_raw,
            raw_sha256=raw_sha256,
            original_size_bytes=len(canonical_raw),
            projection_revision=DELEGATED_RESULT_PROJECTION_REVISION,
            projection_mode="inline",
            projection_truncated=truncated,
            spill_required=False,
            spill_artifact_id=None,
        )

    async def _mcp(
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
        model_edition: str,
    ) -> AgentCallResultProjection:
        has_agent_projection = "agent_projection" in raw_value
        if has_agent_projection:
            try:
                _validate_mcp_agent_result_bundle(raw_value["agent_projection"])
            except ValueError:
                return _rejected(
                    "agent_result_invalid",
                    canonical_raw=canonical_raw,
                    raw_sha256=raw_sha256,
                )
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
        source_truncated = bool(raw_value.get("truncated"))
        if has_agent_projection:
            try:
                bundle = _validate_mcp_agent_result_bundle(
                    model_view.get("agent_projection")
                )
            except ValueError:
                return _rejected(
                    "agent_result_invalid",
                    canonical_raw=canonical_raw,
                    raw_sha256=raw_sha256,
                )
            if (
                type(raw_value.get("truncated")) is not bool
                or raw_value["truncated"] != bundle["truncated"]
            ):
                return _rejected(
                    "agent_result_invalid",
                    canonical_raw=canonical_raw,
                    raw_sha256=raw_sha256,
                )
        elif outcome == "completed" and safe_error_code is None:
            model_view, token_truncated = await self._budget_model_view(
                model_view,
                model_edition=model_edition,
            )
            source_truncated = bool(source_truncated or token_truncated)
        full_result = build_model_result_envelope(
            projection_revision=MCP_RESULT_PROJECTION_REVISION,
            projection_mode="inline",
            model_view=model_view,
            original_size_bytes=len(canonical_raw),
            raw_sha256=raw_sha256,
            projection_truncated=source_truncated,
        )
        if _inline_result_fits(
            full_result,
            call_item_id=call_item_id,
            outcome=outcome,
            safe_error_code=safe_error_code,
            artifact_ids=artifact_ids,
        ):
            return AgentCallResultProjection(
                safe_result_payload=full_result,
                canonical_raw_bytes=canonical_raw,
                raw_sha256=raw_sha256,
                original_size_bytes=len(canonical_raw),
                projection_revision=MCP_RESULT_PROJECTION_REVISION,
                projection_mode="inline",
                projection_truncated=source_truncated,
                spill_required=False,
                spill_artifact_id=None,
            )
        if outcome != "completed" or safe_error_code is not None or artifact_ids:
            return _rejected(
                "agent_result_projection_too_large",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        return _transient_projection(
            full_result=full_result,
            canonical_raw=canonical_raw,
            original_raw_sha256=raw_sha256,
            call_item_id=call_item_id,
            projection_revision=MCP_RESULT_PROJECTION_REVISION,
            projection_truncated=source_truncated,
        )

    async def _skill(
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
        model_edition: str,
    ) -> AgentCallResultProjection:
        if skill_projection_policy not in _SKILL_RESULT_PROJECTION_POLICIES:
            return _rejected(
                "agent_result_invalid",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        if (
            (
                skill_projection_policy
                != SKILL_RESULT_PROJECTION_POLICY_LEGACY
                and not capability_id.startswith("skill.")
            )
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
        ):
            return _rejected(
                "agent_result_invalid",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
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
        token_truncated = False
        if outcome == "completed" and safe_error_code is None:
            model_view, token_truncated = await self._budget_model_view(
                model_view,
                model_edition=model_edition,
            )
        full_result = build_model_result_envelope(
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
            model_view=model_view,
            original_size_bytes=len(canonical_raw),
            raw_sha256=raw_sha256,
            projection_truncated=token_truncated,
            projection_mode="inline",
        )
        if _inline_result_fits(
            full_result,
            call_item_id=call_item_id,
            outcome=outcome,
            safe_error_code=safe_error_code,
            artifact_ids=artifact_ids,
        ):
            return AgentCallResultProjection(
                safe_result_payload=full_result,
                canonical_raw_bytes=canonical_raw,
                raw_sha256=raw_sha256,
                original_size_bytes=len(canonical_raw),
                projection_revision=SKILL_RESULT_PROJECTION_REVISION,
                projection_mode="inline",
                projection_truncated=token_truncated,
                spill_required=False,
                spill_artifact_id=None,
            )
        if outcome != "completed" or safe_error_code is not None:
            return _rejected(
                "agent_result_projection_too_large",
                canonical_raw=canonical_raw,
                raw_sha256=raw_sha256,
            )
        if (
            skill_projection_policy
            == SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
        ):
            return _transient_projection(
                full_result=full_result,
                canonical_raw=canonical_raw,
                original_raw_sha256=raw_sha256,
                call_item_id=call_item_id,
                projection_revision=(
                    AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION
                ),
                projection_truncated=token_truncated,
            )
        return _artifact_projection(
            full_result=full_result,
            canonical_raw=canonical_raw,
            original_raw_sha256=raw_sha256,
            call_item_id=call_item_id,
            artifact_ids=artifact_ids,
            projection_truncated=token_truncated,
        )

    async def _budget_model_view(
        self,
        model_view: Mapping[str, Any],
        *,
        model_edition: str,
    ) -> tuple[dict[str, Any], bool]:
        source = _canonical_json_bytes(dict(model_view)).decode("utf-8").removesuffix(
            "\n"
        )
        bounded = await self._token_budgeter(
            source,
            max_tokens=self._max_business_tokens,
            model_edition=model_edition,
            config=self._tokenization_config,
        )
        if not bounded.truncated:
            return dict(model_view), False
        return (
            {
                "schema": "maf.agent.tool_result_preview.v1",
                "structured_preview": bounded.text,
            },
            True,
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
        size = len(_canonical_json_bytes(result))
        if result["projected_size_bytes"] == size:
            break
        result["projected_size_bytes"] = size
    if result["projected_size_bytes"] != len(_canonical_json_bytes(result)):
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


def _inline_result_fits(
    result: Mapping[str, Any],
    *,
    call_item_id: str,
    outcome: str,
    safe_error_code: str | None,
    artifact_ids: Sequence[str],
) -> bool:
    try:
        _validate_model_result(result)
        _preflight_tool_result(
            call_item_id=call_item_id,
            outcome=outcome,
            safe_result=result,
            safe_error_code=safe_error_code,
            artifact_ids=artifact_ids,
        )
    except (AgentPayloadError, ValueError):
        return False
    return True


def _transient_projection(
    *,
    full_result: Mapping[str, Any],
    canonical_raw: bytes,
    original_raw_sha256: str,
    call_item_id: str,
    projection_revision: str,
    projection_truncated: bool,
) -> AgentCallResultProjection:
    content = _canonical_json_bytes(dict(full_result))
    content_sha256 = hashlib.sha256(content).hexdigest()
    stage_ref = transient_skill_result_stage_ref(
        call_item_id=call_item_id,
        raw_sha256=content_sha256,
        projection_revision=projection_revision,
    )
    receipt = build_model_result_envelope(
        projection_revision=projection_revision,
        projection_mode="transient_staged",
        model_view={
            "complete_result_pending_context_injection": True,
            "schema": "maf.agent.transient_skill_result_receipt.v1",
            "stage_ref": stage_ref,
        },
        original_size_bytes=len(content),
        raw_sha256=content_sha256,
        projection_truncated=projection_truncated,
    )
    if not _inline_result_fits(
        receipt,
        call_item_id=call_item_id,
        outcome="completed",
        safe_error_code=None,
        artifact_ids=(),
    ):
        return _rejected(
            "agent_result_projection_too_large",
            canonical_raw=canonical_raw,
            raw_sha256=original_raw_sha256,
        )
    return AgentCallResultProjection(
        safe_result_payload=receipt,
        canonical_raw_bytes=canonical_raw,
        raw_sha256=original_raw_sha256,
        original_size_bytes=len(canonical_raw),
        projection_revision=projection_revision,
        projection_mode="transient_staged",
        projection_truncated=projection_truncated,
        spill_required=False,
        spill_artifact_id=None,
        transient_stage_required=True,
        transient_stage_ref=stage_ref,
        transient_content_bytes=content,
        transient_content_sha256=content_sha256,
    )


def _artifact_projection(
    *,
    full_result: Mapping[str, Any],
    canonical_raw: bytes,
    original_raw_sha256: str,
    call_item_id: str,
    artifact_ids: Sequence[str],
    projection_truncated: bool,
) -> AgentCallResultProjection:
    content = _canonical_json_bytes(dict(full_result))
    content_sha256 = hashlib.sha256(content).hexdigest()
    spill_artifact_id = skill_result_artifact_id(
        call_item_id=call_item_id,
        raw_sha256=content_sha256,
        projection_revision=SKILL_RESULT_PROJECTION_REVISION,
    )
    receipt = build_model_result_envelope(
        projection_revision=SKILL_RESULT_PROJECTION_REVISION,
        projection_mode="artifact_backed",
        model_view={
            "complete_result_available_as_artifact": True,
            "schema": "maf.agent.skill_result_projection_receipt.v1",
        },
        original_size_bytes=len(content),
        raw_sha256=content_sha256,
        projection_truncated=projection_truncated,
    )
    if not _inline_result_fits(
        receipt,
        call_item_id=call_item_id,
        outcome="completed",
        safe_error_code=None,
        artifact_ids=(*artifact_ids, spill_artifact_id),
    ):
        return _rejected(
            "agent_result_projection_too_large",
            canonical_raw=canonical_raw,
            raw_sha256=original_raw_sha256,
        )
    return AgentCallResultProjection(
        safe_result_payload=receipt,
        canonical_raw_bytes=canonical_raw,
        raw_sha256=original_raw_sha256,
        original_size_bytes=len(canonical_raw),
        projection_revision=SKILL_RESULT_PROJECTION_REVISION,
        projection_mode="artifact_backed",
        projection_truncated=projection_truncated,
        spill_required=True,
        spill_artifact_id=spill_artifact_id,
        spill_content_bytes=content,
        spill_content_sha256=content_sha256,
    )


def _validate_mcp_agent_result_bundle(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "result_count",
        "included_count",
        "omitted_count",
        "truncated",
        "results",
    }:
        raise ValueError("agent_mcp_result_bundle_invalid")
    result_count = value.get("result_count")
    included_count = value.get("included_count")
    omitted_count = value.get("omitted_count")
    results = value.get("results")
    if (
        value.get("schema") != MCP_AGENT_RESULT_BUNDLE_SCHEMA
        or type(result_count) is not int
        or result_count < 1
        or type(included_count) is not int
        or included_count < 1
        or type(omitted_count) is not int
        or omitted_count < 0
        or type(value.get("truncated")) is not bool
        or not isinstance(results, list)
        or included_count != len(results)
        or result_count != included_count + omitted_count
    ):
        raise ValueError("agent_mcp_result_bundle_invalid")
    sequences: list[int] = []
    expected_truncated = omitted_count > 0
    normalized_results: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or set(item) != {
            "call_sequence",
            "content",
            "source_truncated",
            "carrier_truncated",
        }:
            raise ValueError("agent_mcp_result_bundle_invalid")
        sequence = item.get("call_sequence")
        if (
            type(sequence) is not int
            or sequence < 1
            or not isinstance(item.get("content"), str)
            or type(item.get("source_truncated")) is not bool
            or type(item.get("carrier_truncated")) is not bool
        ):
            raise ValueError("agent_mcp_result_bundle_invalid")
        sequences.append(sequence)
        expected_truncated = bool(
            expected_truncated
            or item["source_truncated"]
            or item["carrier_truncated"]
        )
        normalized_results.append(dict(item))
    if sequences != sorted(set(sequences)) or value["truncated"] != expected_truncated:
        raise ValueError("agent_mcp_result_bundle_invalid")
    return {
        "schema": MCP_AGENT_RESULT_BUNDLE_SCHEMA,
        "result_count": result_count,
        "included_count": included_count,
        "omitted_count": omitted_count,
        "truncated": expected_truncated,
        "results": normalized_results,
    }


def _validate_model_result(result: Mapping[str, Any]) -> None:
    canonical = canonicalize_agent_payload(dict(result))
    if not isinstance(result.get("model_view"), Mapping):
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


def _is_forbidden_key(key: str) -> bool:
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if normalized in _FORBIDDEN_RAW_KEYS:
        return True
    return normalized.endswith(
        ("_password", "_secret", "_api_key", "_access_token", "_refresh_token")
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
