from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from src.orchestration.models import UserMCPServerProfile


class MCPSelectorActionType(str, Enum):
    CALL_TOOL = "call_tool"
    FINISH = "finish"
    ROUTE_ANOTHER_SERVER = "route_another_server"
    STOP = "stop"


class MCPBindingMode(str, Enum):
    AUTOMATIC = "automatic"
    EXPLICIT_COMMAND = "explicit_command"


@dataclass(slots=True, frozen=True)
class MCPToolProfile:
    name: str
    title: str = ""
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class MCPAttachmentSummary:
    basename: str
    content_type: str
    size_bytes: int


@dataclass(slots=True, frozen=True)
class MCPSelectorContext:
    user_request: str
    server: UserMCPServerProfile
    tools: tuple[MCPToolProfile, ...]
    binding_mode: MCPBindingMode
    allow_route_another_server: bool
    attachments: tuple[MCPAttachmentSummary, ...] = ()
    upstream_facts: tuple[str, ...] = ()
    completed_result_refs: tuple[str, ...] = ()
    failed_call_fingerprints: frozenset[str] = frozenset()
    rejected_call_fingerprints: frozenset[str] = frozenset()
    remaining_call_budget: int = 20

    def __post_init__(self) -> None:
        expected = self.binding_mode is MCPBindingMode.AUTOMATIC
        if self.allow_route_another_server is not expected:
            raise ValueError("MCP binding mode and route policy are inconsistent")


def build_mcp_selector_context(
    *,
    user_request: str,
    server: UserMCPServerProfile,
    tools: tuple[MCPToolProfile, ...],
    binding_mode: MCPBindingMode,
    attachments: tuple[MCPAttachmentSummary, ...] = (),
    upstream_facts: tuple[str, ...] = (),
    completed_result_refs: tuple[str, ...] = (),
    failed_call_fingerprints: frozenset[str] = frozenset(),
    rejected_call_fingerprints: frozenset[str] = frozenset(),
    remaining_call_budget: int = 20,
) -> MCPSelectorContext:
    return MCPSelectorContext(
        user_request=user_request,
        server=server,
        tools=tools,
        binding_mode=binding_mode,
        allow_route_another_server=binding_mode is MCPBindingMode.AUTOMATIC,
        attachments=attachments,
        upstream_facts=upstream_facts,
        completed_result_refs=completed_result_refs,
        failed_call_fingerprints=failed_call_fingerprints,
        rejected_call_fingerprints=rejected_call_fingerprints,
        remaining_call_budget=remaining_call_budget,
    )


@dataclass(slots=True, frozen=True)
class MCPSelectorAction:
    action: MCPSelectorActionType
    tool_name: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""


class MCPServerRouteActionType(str, Enum):
    ROUTE_SERVER = "route_server"
    STOP = "stop"


@dataclass(slots=True, frozen=True)
class MCPServerRouteAction:
    action: MCPServerRouteActionType
    server_id: str | None = None
    reason: str = ""


@dataclass(slots=True, frozen=True)
class MCPCallReservation:
    call_number: int
    fingerprint: str


class MCPCallBudgetExhausted(RuntimeError):
    pass


class MCPCallFingerprintBlocked(RuntimeError):
    pass


class MCPCallBudget:
    """Task-scoped selector guard; durable reservation is supplied by storage later."""

    def __init__(self, *, max_calls: int = 20) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self._max_calls = max_calls
        self._used_calls = 0
        self._failed: set[str] = set()
        self._rejected: set[str] = set()

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def used_calls(self) -> int:
        return self._used_calls

    @property
    def remaining_calls(self) -> int:
        return self._max_calls - self._used_calls

    @property
    def failed_fingerprints(self) -> frozenset[str]:
        return frozenset(self._failed)

    @property
    def rejected_fingerprints(self) -> frozenset[str]:
        return frozenset(self._rejected)

    def reserve(self, *, server_id: str, tool_name: str, arguments: Mapping[str, Any]) -> MCPCallReservation:
        fingerprint = build_mcp_call_fingerprint(
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        if fingerprint in self._failed or fingerprint in self._rejected:
            raise MCPCallFingerprintBlocked(fingerprint)
        if self._used_calls >= self._max_calls:
            raise MCPCallBudgetExhausted("MCP tools/call budget exhausted")
        self._used_calls += 1
        return MCPCallReservation(call_number=self._used_calls, fingerprint=fingerprint)

    def record_failed(self, fingerprint: str) -> None:
        self._failed.add(fingerprint)

    def record_rejected(self, fingerprint: str) -> None:
        self._rejected.add(fingerprint)


def build_mcp_call_fingerprint(*, server_id: str, tool_name: str, arguments: Mapping[str, Any]) -> str:
    canonical_arguments = json.dumps(
        dict(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    material = json.dumps(
        [str(server_id), str(tool_name), canonical_arguments],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
