from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from src.core.enums import NodeCriticality, UserMCPTransport


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class InstanceState(StrEnum):
    ONLINE = "online"
    BUSY = "busy"
    DRAINING = "draining"
    OFFLINE = "offline"


@dataclass(slots=True, frozen=True)
class CapabilityDescriptor:
    capability_id: str
    name: str
    description: str
    display_name: str = ""
    version: str = "1"
    enabled: bool = True
    public: bool = True
    kind: str = "capability"
    source: str = "builtin"
    source_path: str = ""


@dataclass(slots=True, frozen=True)
class UserMCPServerProfile:
    """Planner-safe description of one available user-scoped MCP server."""

    server_id: str
    display_name: str
    routing_description: str
    transport: str

    def __post_init__(self) -> None:
        for field_name in ("server_id", "display_name", "transport"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        try:
            UserMCPTransport(self.transport)
        except ValueError as exc:
            raise ValueError(f"Unsupported MCP transport: {self.transport}") from exc
        object.__setattr__(self, "routing_description", str(self.routing_description or "").strip())


@dataclass(slots=True, frozen=True)
class ExecutionInstance:
    instance_id: str
    supported_capabilities: tuple[str, ...]
    state: InstanceState = InstanceState.ONLINE
    load_score: int = 0
    endpoint: str | None = None


@dataclass(slots=True, frozen=True)
class WorkflowNodePlan:
    node_id: str
    capability_id: str
    input_payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    criticality: NodeCriticality = NodeCriticality.REQUIRED
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    timeout_policy: Mapping[str, Any] = field(default_factory=dict)
    resource_class: str | None = None


@dataclass(slots=True, frozen=True)
class WorkflowPlan:
    task_id: str
    nodes: tuple[WorkflowNodePlan, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    max_replans: int = 0
    max_dynamic_nodes: int = 0

    def node_by_id(self, node_id: str) -> WorkflowNodePlan:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)


@dataclass(slots=True, frozen=True)
class OrchestrationRequest:
    task_id: str
    conversation_id: str
    root_message_id: str
    user_message: str
    requested_capability_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    current_user_message: str | None = None
    resolved_user_message: str | None = None
    memory_context: Mapping[str, Any] | None = None
    available_mcp_servers: tuple[UserMCPServerProfile, ...] = ()

    def __post_init__(self) -> None:
        profiles = tuple(self.available_mcp_servers)
        if any(not isinstance(profile, UserMCPServerProfile) for profile in profiles):
            raise TypeError("available_mcp_servers must contain UserMCPServerProfile values")
        server_ids = tuple(profile.server_id for profile in profiles)
        if len(set(server_ids)) != len(server_ids):
            raise ValueError("available_mcp_servers must not contain duplicate server_id values")
        object.__setattr__(self, "available_mcp_servers", profiles)

    @property
    def effective_user_message(self) -> str:
        resolved = (self.resolved_user_message or "").strip()
        if resolved:
            return resolved
        current = (self.current_user_message or "").strip()
        return current or self.user_message


@dataclass(slots=True, frozen=True)
class OrchestrationRunResult:
    task: Any
    nodes: tuple[Any, ...]
    completion_status: str
