from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from src.core.models import TaskNode

from .completion_policy import CompletionStatus
from .models import OrchestrationRequest, WorkflowPlan


@dataclass(slots=True, frozen=True)
class RuntimeReplanContext:
    request: OrchestrationRequest
    plan: WorkflowPlan
    nodes: Mapping[str, TaskNode]
    node_outputs: Mapping[str, Mapping[str, Any]]
    completion_status: CompletionStatus
    replan_count: int = 0
    dynamic_node_count: int = 0
    unresolved_interrupt: bool = False


@dataclass(slots=True, frozen=True)
class RuntimeReplanDecision:
    plan: WorkflowPlan
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class RuntimeReplanner(Protocol):
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None | Awaitable[RuntimeReplanDecision | None]:
        ...


class NoopRuntimeReplanner:
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        return None


class CompositeRuntimeReplanner:
    def __init__(self, replanners: tuple[RuntimeReplanner, ...] | list[RuntimeReplanner]) -> None:
        self._replanners = tuple(replanners)

    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None | Awaitable[RuntimeReplanDecision | None]:
        return self._build_replan(context)

    async def _build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        for replanner in self._replanners:
            decision = replanner.build_replan(context)
            if inspect.isawaitable(decision):
                decision = await decision
            if decision is not None:
                return decision
        return None
