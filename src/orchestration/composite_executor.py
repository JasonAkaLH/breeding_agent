from __future__ import annotations

from collections.abc import Iterable

from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort


class CompositeExecutor(ExecutorPort):
    def __init__(self, executors: Iterable[ExecutorPort]) -> None:
        self._executors = tuple(executors)

    def supports(self, capability_id: str) -> bool:
        return any(executor.supports(capability_id) for executor in self._executors)

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        for executor in self._executors:
            if executor.supports(request.capability_id):
                return await executor.execute(request)
        raise ValueError(f"Unsupported capability_id: {request.capability_id}")
