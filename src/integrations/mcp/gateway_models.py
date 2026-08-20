from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class MCPCallOutcomeKind(StrEnum):
    COMPLETED = "completed"
    INPUT_REQUIRED = "input_required"
    TASK_CREATED = "task_created"


class MCPCancelStatus(StrEnum):
    CANCELLED = "cancelled"
    REMOTE_STOP_UNKNOWN = "remote_stop_unknown"
    ALREADY_TERMINAL = "already_terminal"
    UNKNOWN_CALL = "unknown_call"


class MCPContinueStatus(StrEnum):
    RESET = "reset"
    ALREADY_TERMINAL = "already_terminal"
    UNKNOWN_CALL = "unknown_call"


@dataclass(slots=True, frozen=True)
class MCPTaskServerScope:
    scope_id: str
    owner_user_id: str
    platform_task_id: str
    server_id: str
    config_version: int
    security_version: int


@dataclass(slots=True, frozen=True)
class MCPToolDescriptor:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    input_schema_sha256: str
    output_schema: Mapping[str, Any] | None = None
    output_schema_sha256: str | None = None
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _deep_freeze_mapping(self.input_schema))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", _deep_freeze_mapping(self.output_schema))
        if (self.output_schema is None) != (self.output_schema_sha256 is None):
            raise ValueError("MCP output schema snapshot and digest must be present together")
        object.__setattr__(self, "annotations", _deep_freeze_mapping(self.annotations))


@dataclass(slots=True, frozen=True)
class ToolCatalogSnapshot:
    server_id: str
    effective_protocol_version: str
    tools: tuple[MCPToolDescriptor, ...]

    def get(self, tool_name: str) -> MCPToolDescriptor | None:
        return next((tool for tool in self.tools if tool.name == tool_name), None)


@dataclass(slots=True, frozen=True)
class MCPCallOutcome:
    kind: MCPCallOutcomeKind
    result_ref: str | None = None
    requests: tuple[Mapping[str, Any], ...] = ()
    sealed_request_state_ref: str | None = None
    safe_remote_task_ref: str | None = None
    status: str | None = None
    next_poll_at: str | None = None
    content_type: str | None = None
    byte_size: int | None = None
    result_content_sha256: str | None = None
    result_store_kind: str | None = None
    external_text: str | None = None

    @classmethod
    def completed(
        cls,
        result_ref: str,
        *,
        content_type: str | None = None,
        byte_size: int | None = None,
        result_content_sha256: str | None = None,
        result_store_kind: str | None = None,
        external_text: str | None = None,
    ) -> "MCPCallOutcome":
        return cls(
            kind=MCPCallOutcomeKind.COMPLETED,
            result_ref=result_ref,
            content_type=content_type,
            byte_size=byte_size,
            result_content_sha256=result_content_sha256,
            result_store_kind=result_store_kind,
            external_text=external_text,
        )

    @classmethod
    def input_required(
        cls,
        requests: tuple[Mapping[str, Any], ...],
        sealed_request_state_ref: str | None,
    ) -> "MCPCallOutcome":
        return cls(
            kind=MCPCallOutcomeKind.INPUT_REQUIRED,
            requests=tuple(MappingProxyType(dict(request)) for request in requests),
            sealed_request_state_ref=sealed_request_state_ref,
        )

    @classmethod
    def task_created(
        cls,
        safe_remote_task_ref: str,
        *,
        status: str,
        next_poll_at: str | None = None,
    ) -> "MCPCallOutcome":
        return cls(
            kind=MCPCallOutcomeKind.TASK_CREATED,
            safe_remote_task_ref=safe_remote_task_ref,
            status=status,
            next_poll_at=next_poll_at,
        )


@dataclass(slots=True, frozen=True)
class CancelOutcome:
    status: MCPCancelStatus
    remote_stop_confirmed: bool


@dataclass(slots=True, frozen=True)
class ContinueOutcome:
    status: MCPContinueStatus


def _deep_freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _deep_freeze_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value
