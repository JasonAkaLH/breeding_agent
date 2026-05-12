from __future__ import annotations

import json
import unittest

from src.capabilities.sql_query.executor import SQLQueryExecutor
from src.core.contracts import CapabilityExecutionRequest


class SQLQueryExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_executor_wires_llm_text_generator_into_intent_route(self) -> None:
        prompts: list[str] = []

        async def llm_text_generator(prompt: str, *, request: CapabilityExecutionRequest | None = None) -> str:
            prompts.append(prompt)
            self.assertIsNotNone(request)
            return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)

        executor = SQLQueryExecutor(llm_text_generator=llm_text_generator)

        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="sql_query.intent_route",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:intent_route",
                input_payload={"user_question": "帮我看看这个材料的分型信息"},
            )
        )

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "llm_semantic")
        self.assertTrue(result.output_payload["llm_router_used"])
        self.assertEqual(len(prompts), 1)
        self.assertIn("sql_query.intent_route", prompts[0])


if __name__ == "__main__":
    unittest.main()
