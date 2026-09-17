from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.core.contracts import CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.core.models import Artifact, EventRecord, Interrupt

MCP_DISPATCH_CAPABILITY_ID = "mcp.dispatch"


@dataclass(slots=True, frozen=True)
class MCPDispatchOutcome:
    output_payload: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    events: tuple[EventRecord, ...] = ()
    interrupt: Interrupt | None = None
    error: CapabilityExecutionError | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class MCPDispatchCoordinator(Protocol):
    async def dispatch(self, request: CapabilityExecutionRequest, *, server_id: str) -> MCPDispatchOutcome: ...


class MCPDispatchExecutor(ExecutorPort):
    """Thin capability boundary; owner/server authorization stays in the coordinator."""

    def __init__(self, *, coordinator: MCPDispatchCoordinator) -> None:
        self._coordinator = coordinator

    def supports(self, capability_id: str) -> bool:
        return capability_id == MCP_DISPATCH_CAPABILITY_ID

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        execution_path = str(request.metadata.get("mcp_execution_mode") or "").strip()
        if execution_path != "user_scoped":
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(
                    code="mcp_route_assignment_mismatch",
                    message="This task is not assigned to the user-scoped MCP execution path.",
                    retriable=False,
                ),
            )
        payload = dict(request.input_payload)
        server_id = payload.get("server_id")
        if set(payload) != {"server_id"} or not isinstance(server_id, str) or not server_id.strip():
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(
                    code="mcp_dispatch_payload_invalid",
                    message="mcp.dispatch requires exactly one non-empty server_id.",
                    retriable=False,
                ),
            )
        outcome = await self._coordinator.dispatch(request, server_id=server_id.strip())
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=outcome.output_payload,
            artifacts=outcome.artifacts,
            events=outcome.events,
            interrupt=outcome.interrupt,
            error=outcome.error,
            metadata=outcome.metadata,
        )
