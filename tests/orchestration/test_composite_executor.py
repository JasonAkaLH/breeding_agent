from __future__ import annotations

import unittest

from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.orchestration.composite_executor import CompositeExecutor


class _FakeExecutor(ExecutorPort):
    def __init__(self, supported: set[str]) -> None:
        self.supported = supported
        self.calls: list[str] = []

    def supports(self, capability_id: str) -> bool:
        return capability_id in self.supported

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        self.calls.append(request.capability_id)
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload={"handled_by": request.capability_id},
        )


class CompositeExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_to_first_supporting_executor(self) -> None:
        first = _FakeExecutor({"a.one"})
        second = _FakeExecutor({"b.one"})
        executor = CompositeExecutor([first, second])

        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="b.one",
                conversation_id="conv",
                task_id="task",
                node_id="node",
            )
        )

        self.assertEqual(result.output_payload["handled_by"], "b.one")
        self.assertEqual(first.calls, [])
        self.assertEqual(second.calls, ["b.one"])
        self.assertTrue(executor.supports("a.one"))
        self.assertFalse(executor.supports("missing"))


if __name__ == "__main__":
    unittest.main()
