from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from .client import MCPClientError

_RELATED_TASK_META_KEY = "io.modelcontextprotocol/related-task"
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


@dataclass(slots=True, frozen=True)
class MCPTaskRecord:
    safe_ref: str
    server_id: str
    tool_name: str
    capability_id: str
    mcp_task_id: str
    progress_token: str | int
    status: str
    status_message: str = ""
    poll_interval_ms: int | None = None
    platform_task_id: str = ""
    platform_node_id: str = ""
    conversation_id: str = ""


class InMemoryMCPTaskRegistry:
    """Process-local task registry for tests/dev/shadow; production enforce uses sidecar durable state."""

    durable = False

    def __init__(self) -> None:
        self._counter = 0
        self._records_by_ref: dict[str, MCPTaskRecord] = {}
        self._records_by_task_id: dict[str, MCPTaskRecord] = {}
        self._progress_by_token: dict[str | int, float | int] = {}

    def make_progress_token(self, *, server_id: str, tool_name: str) -> str:
        self._counter += 1
        return f"mcp-progress:{_slug(server_id)}:{_slug(tool_name)}:{self._counter:06d}"

    def make_safe_ref(self, *, server_id: str, tool_name: str) -> str:
        return f"mcp-task:{_slug(server_id)}:{_slug(tool_name)}:{uuid4().hex}"

    def create_record(
        self,
        *,
        server_id: str,
        tool_name: str,
        capability_id: str,
        mcp_task_id: str,
        progress_token: str | int,
        status_payload: Mapping[str, Any] | None = None,
        poll_interval_ms: int | None = None,
        platform_task_id: str = "",
        platform_node_id: str = "",
        conversation_id: str = "",
    ) -> MCPTaskRecord:
        safe_ref = self.make_safe_ref(server_id=server_id, tool_name=tool_name)
        state, message = normalize_task_status(status_payload or {})
        record = MCPTaskRecord(
            safe_ref=safe_ref,
            server_id=server_id,
            tool_name=tool_name,
            capability_id=capability_id,
            mcp_task_id=mcp_task_id,
            progress_token=progress_token,
            status=state or "working",
            status_message=message,
            poll_interval_ms=poll_interval_ms,
            platform_task_id=platform_task_id,
            platform_node_id=platform_node_id,
            conversation_id=conversation_id,
        )
        self._records_by_ref[safe_ref] = record
        self._records_by_task_id[mcp_task_id] = record
        return record

    def update_status(self, mcp_task_id: str, status_payload: Mapping[str, Any]) -> MCPTaskRecord:
        record = self._records_by_task_id[mcp_task_id]
        next_state, message = normalize_task_status(status_payload)
        if record.status in _TERMINAL_STATES and next_state not in {"", record.status}:
            return record
        updated = replace(record, status=next_state or record.status, status_message=message)
        self._records_by_ref[updated.safe_ref] = updated
        self._records_by_task_id[mcp_task_id] = updated
        return updated

    def record_progress(self, progress_token: str | int, progress: float | int) -> None:
        if not isinstance(progress_token, (str, int)):
            raise MCPClientError("MCP progress token must be string or integer.", code="mcp_runtime_progress_token_invalid")
        previous = self._progress_by_token.get(progress_token)
        if previous is not None and progress < previous:
            raise MCPClientError("MCP progress must be monotonic.", code="mcp_runtime_progress_token_invalid")
        self._progress_by_token[progress_token] = progress

    def records(self) -> list[MCPTaskRecord]:
        return list(self._records_by_ref.values())

    def records_for_platform_task(self, platform_task_id: str) -> list[MCPTaskRecord]:
        return [record for record in self._records_by_ref.values() if record.platform_task_id == platform_task_id]


def normalize_task_status(payload: Mapping[str, Any]) -> tuple[str, str]:
    status = payload.get("status") if isinstance(payload.get("status"), Mapping) else payload
    state = str(status.get("state") or status.get("status") or "").strip().lower() if isinstance(status, Mapping) else ""
    message = str(status.get("message") or "").strip() if isinstance(status, Mapping) else ""
    return state, message


def validate_related_task_result_metadata(result: Mapping[str, Any], task_id: str) -> None:
    meta = result.get("_meta") if isinstance(result.get("_meta"), Mapping) else {}
    related = meta.get(_RELATED_TASK_META_KEY) if isinstance(meta, Mapping) else None
    if not isinstance(related, Mapping) or str(related.get("taskId") or "") != task_id:
        raise MCPClientError(
            "MCP tasks/result result must include related-task metadata for the task id.",
            code="mcp_runtime_task_related_metadata_invalid",
            retriable=False,
        )


def is_create_task_result(result: Mapping[str, Any]) -> bool:
    return bool(str(result.get("taskId") or "").strip()) and isinstance(result.get("status"), Mapping)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return slug or "unknown"
