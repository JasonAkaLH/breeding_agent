from __future__ import annotations

from enum import Enum

from src.core.enums import NodeCriticality, NodeStatus

from .models import WorkflowPlan


class CompletionStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    REPLAN_AVAILABLE = "replan_available"
    COMPLETED = "completed"
    FAILED = "failed"


class CompletionPolicy:
    def evaluate(
        self,
        plan: WorkflowPlan,
        node_statuses: dict[str, NodeStatus],
        *,
        replan_count: int = 0,
        unresolved_interrupt: bool = False,
    ) -> CompletionStatus:
        if unresolved_interrupt:
            return CompletionStatus.WAITING_FOR_INPUT

        required_failures = False
        incomplete_required = False
        for node in plan.nodes:
            status = node_statuses.get(node.node_id, NodeStatus.PENDING)
            if node.criticality != NodeCriticality.REQUIRED:
                continue
            if status in {
                NodeStatus.FAILED,
                NodeStatus.CANCELLED,
                NodeStatus.BLOCKED_BY_CANCELLATION,
                NodeStatus.ORPHANED,
            }:
                required_failures = True
            elif status != NodeStatus.COMPLETED:
                incomplete_required = True

        if required_failures:
            if replan_count < plan.max_replans:
                return CompletionStatus.REPLAN_AVAILABLE
            return CompletionStatus.FAILED

        if incomplete_required:
            return CompletionStatus.RUNNING

        return CompletionStatus.COMPLETED
