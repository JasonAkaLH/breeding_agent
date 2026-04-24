from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from src.core.enums import NodeCriticality


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
    version: str = "1"
    enabled: bool = True
    public: bool = True


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


@dataclass(slots=True, frozen=True)
class OrchestrationRequest:
    task_id: str
    conversation_id: str
    root_message_id: str
    user_message: str
    requested_capability_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class OrchestrationRunResult:
    task: Any
    nodes: tuple[Any, ...]
    completion_status: str
