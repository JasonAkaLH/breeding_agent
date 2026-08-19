from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


RecoveryStage = Callable[[], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class MCPAggregateRecoveryStages:
    repair_lifecycle_markers: RecoveryStage
    enumerate_terminal_candidates: RecoveryStage
    reconcile_terminal_candidates: RecoveryStage
    reconcile_remote_bindings: RecoveryStage
    reconcile_mrtr_evidence: RecoveryStage
    reconcile_pending_actions: RecoveryStage
    reconcile_resume_envelopes: RecoveryStage
    recover_expired_claims: RecoveryStage
    converge_unknown_no_replay: RecoveryStage
    validate_invariants: RecoveryStage


class MCPAggregateStartupReconciler:
    """Runs local-only MCP recovery stages in one fixed, reviewable order."""

    _ORDER = (
        "repair_lifecycle_markers",
        "enumerate_terminal_candidates",
        "reconcile_terminal_candidates",
        "reconcile_remote_bindings",
        "reconcile_mrtr_evidence",
        "reconcile_pending_actions",
        "reconcile_resume_envelopes",
        "recover_expired_claims",
        "converge_unknown_no_replay",
        "validate_invariants",
    )

    def __init__(self, stages: MCPAggregateRecoveryStages) -> None:
        if not isinstance(stages, MCPAggregateRecoveryStages):
            raise TypeError("MCP aggregate recovery stages are required")
        self._stages = stages

    async def run(self) -> tuple[str, ...]:
        completed: list[str] = []
        for name in self._ORDER:
            stage = getattr(self._stages, name)
            await stage()
            completed.append(name)
        return tuple(completed)

__all__ = [
    "MCPAggregateRecoveryStages",
    "MCPAggregateStartupReconciler",
]
