from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.core.models import Task, TaskInputAttachment, TaskNode
from src.integrations.mcp.cp7_artifacts import canonical_json_bytes


MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2 = "maf.user_mcp.dispatch_resume.v2"
MCP_DISPATCH_RESUME_ENVELOPE_MAX_BYTES = 64 * 1024
MCP_DISPATCH_RESUME_ENVELOPE_REVIEW_BYTES = 48 * 1024
MCP_DISPATCH_RESUME_ENVELOPE_MAX_ATTACHMENTS = 20
MCP_DISPATCH_RESUME_ENVELOPE_MAX_SERVER_ID_BYTES = 128
MCP_DISPATCH_RESUME_ENVELOPE_MAX_ID_BYTES = 512

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "capability_id",
        "conversation_id",
        "task_id",
        "root_message_id",
        "node_id",
        "server_id",
        "task_assignment",
        "node_snapshot",
        "input_attachment_ids",
    }
)
_ASSIGNMENT_KEYS = frozenset(
    {
        "mcp_execution_mode",
        "mcp_shadow_enabled",
        "mcp_rollout_config_version",
        "mcp_route_reason_code",
        "mcp_rollout_mode",
    }
)
_NODE_KEYS = frozenset(
    {
        "capability_id",
        "input_refs",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "metadata",
        "input_payload",
        "dependency_outputs",
        "uploaded_artifacts",
        "skill_artifacts",
        "content_base64",
        "arguments",
        "result",
        "results",
        "tool_arguments",
        "tool_input",
        "tool_output",
        "tool_result",
        "tool_results",
        "endpoint",
        "endpoint_url",
        "auth",
        "credential",
        "credentials",
        "auth_metadata",
    }
)


class MCPDispatchResumeEnvelopeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def mcp_dispatch_resume_envelope_version(
    envelope: Mapping[str, Any],
) -> str:
    schema = envelope.get("schema")
    if schema is None:
        return "legacy_v1"
    if schema == MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2:
        return "v2"
    raise MCPDispatchResumeEnvelopeError(
        "mcp_dispatch_resume_envelope_schema_unsupported"
    )


def build_mcp_dispatch_resume_envelope_v2(
    *,
    task: Task,
    node: TaskNode,
    attachments: Sequence[TaskInputAttachment],
    server_id: str,
) -> dict[str, Any]:
    attachment_ids = sorted(
        {attachment.attachment_id for attachment in attachments}
    )
    envelope: dict[str, Any] = {
        "schema": MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2,
        "capability_id": "mcp.dispatch",
        "conversation_id": task.conversation_id,
        "task_id": task.task_id,
        "root_message_id": task.root_message_id,
        "node_id": node.node_id,
        "server_id": server_id,
        "task_assignment": {
            "mcp_execution_mode": task.mcp_execution_mode,
            "mcp_shadow_enabled": task.mcp_shadow_enabled,
            "mcp_rollout_config_version": task.mcp_rollout_config_version,
            "mcp_route_reason_code": task.mcp_route_reason_code,
            "mcp_rollout_mode": task.mcp_rollout_mode,
        },
        "node_snapshot": {
            "capability_id": node.capability_id,
            "input_refs": sorted(set(node.input_refs)),
        },
        "input_attachment_ids": attachment_ids,
    }
    validate_mcp_dispatch_resume_envelope_v2(envelope)
    return envelope


def validate_mcp_dispatch_resume_envelope_v2(
    envelope: Mapping[str, Any],
) -> None:
    if mcp_dispatch_resume_envelope_version(envelope) != "v2":
        raise MCPDispatchResumeEnvelopeError(
            "mcp_dispatch_resume_envelope_v2_required"
        )
    _require_exact_keys(envelope, _TOP_LEVEL_KEYS)
    _reject_forbidden_keys(envelope)
    if envelope.get("capability_id") != "mcp.dispatch":
        _invalid()

    for key in (
        "conversation_id",
        "task_id",
        "root_message_id",
        "node_id",
    ):
        _require_bounded_string(
            envelope.get(key), MCP_DISPATCH_RESUME_ENVELOPE_MAX_ID_BYTES
        )
    _require_bounded_string(
        envelope.get("server_id"), MCP_DISPATCH_RESUME_ENVELOPE_MAX_SERVER_ID_BYTES
    )

    assignment = _require_mapping(envelope.get("task_assignment"))
    _require_exact_keys(assignment, _ASSIGNMENT_KEYS)
    if (
        assignment.get("mcp_execution_mode") != "user_scoped"
        or assignment.get("mcp_shadow_enabled") is not False
        or assignment.get("mcp_route_reason_code") != "enforce_selected"
        or assignment.get("mcp_rollout_mode") != "enforce"
    ):
        raise MCPDispatchResumeEnvelopeError(
            "mcp_target_intent_task_assignment_invalid"
        )
    _require_bounded_string(
        assignment.get("mcp_rollout_config_version"),
        MCP_DISPATCH_RESUME_ENVELOPE_MAX_ID_BYTES,
    )

    node = _require_mapping(envelope.get("node_snapshot"))
    _require_exact_keys(node, _NODE_KEYS)
    if node.get("capability_id") != "mcp.dispatch":
        _invalid()
    input_refs = _require_string_list(node.get("input_refs"))
    _require_sorted_unique(input_refs)
    for value in input_refs:
        _require_bounded_string(value, MCP_DISPATCH_RESUME_ENVELOPE_MAX_ID_BYTES)
    attachment_ids = _require_string_list(envelope.get("input_attachment_ids"))
    if len(attachment_ids) > MCP_DISPATCH_RESUME_ENVELOPE_MAX_ATTACHMENTS:
        _invalid()
    _require_sorted_unique(attachment_ids)
    for attachment_id in attachment_ids:
        _require_bounded_string(
            attachment_id, MCP_DISPATCH_RESUME_ENVELOPE_MAX_ID_BYTES
        )

    try:
        rendered = canonical_json_bytes(dict(envelope))
    except (TypeError, ValueError):
        _invalid()
    if len(rendered) > MCP_DISPATCH_RESUME_ENVELOPE_MAX_BYTES:
        raise MCPDispatchResumeEnvelopeError(
            "mcp_target_intent_resume_envelope_too_large"
        )


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise MCPDispatchResumeEnvelopeError(
                    "mcp_dispatch_resume_envelope_forbidden_field"
                )
            _reject_forbidden_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_forbidden_keys(nested)


def _require_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str]
) -> None:
    if set(value) != expected:
        _invalid()


def _require_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _invalid()
    return value


def _require_string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        _invalid()
    return value


def _require_sorted_unique(values: Sequence[str]) -> None:
    if list(values) != sorted(set(values)):
        _invalid()


def _require_bounded_string(value: Any, max_bytes: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > max_bytes
    ):
        _invalid()


def _invalid() -> None:
    raise MCPDispatchResumeEnvelopeError(
        "mcp_dispatch_resume_envelope_invalid"
    )
