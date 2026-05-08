from __future__ import annotations

import json
import unittest

from src.capabilities.sql_query.executor import SQLQueryExecutor
from src.core.contracts import CapabilityExecutionRequest


class SQLQueryConversationMemoryBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_sqlquery_llm_request_metadata_excludes_full_conversation_memory(self) -> None:
        seen_metadata: list[dict] = []

        async def llm_text_generator(_prompt: str, *, request: CapabilityExecutionRequest | None = None) -> str:
            seen_metadata.append(dict(request.metadata if request is not None else {}))
            return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)

        executor = SQLQueryExecutor(llm_text_generator=llm_text_generator)
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="sql_query.result_filtering",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="filter",
                dependency_outputs={
                    "execute": {
                        "columns": ["variety_name"],
                        "rows": [{"variety_name": "龙粳33"}],
                        "row_count": 1,
                    },
                    "intent": {
                        "user_question": "查询龙粳33的基因型信息",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                    },
                },
                metadata={
                    "conversation_memory": {"history_summary": "secret memory"},
                    "memory_context": {"recent_messages": ["secret"]},
                    "history_summary": "secret",
                    "resolved_user_message": "secret",
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "high",
                },
            )
        )

        self.assertEqual(result.output_payload["filter_source"], "llm")
        self.assertEqual(seen_metadata, [{"deep_thinking": True, "main_agent_reasoning_effort": "high"}])
