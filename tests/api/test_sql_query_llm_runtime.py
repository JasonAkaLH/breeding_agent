from __future__ import annotations

import json
from typing import Any

from tests.api.support import APITestCase
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult


class SQLQueryLLMRuntimeAPITest(APITestCase):
    async def test_configured_sqlquery_llm_client_is_used_by_generate_and_filtering(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeSQLQueryLLMClient:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                calls.append(
                    {
                        "prompt": prompt,
                        "thinking": thinking,
                        "reasoning_effort": reasoning_effort,
                        "kwargs": self.kwargs,
                    }
                )
                if "sql_query.sql_generate" in prompt:
                    return json.dumps(
                        {
                            "mode": "answer",
                            "route_id": "genotype_db",
                            "schema_profile_id": "genotype_profile",
                            "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%' LIMIT 20",
                            "tables_used": ["variety"],
                            "columns_used": ["variety.variety_name"],
                            "column_types_used": {"variety.variety_name": "varchar(100)"},
                        },
                        ensure_ascii=False,
                    )
                if "sql_query.result_filtering" in prompt:
                    return json.dumps({"keep_row_indexes": [0], "filter_reason": "只保留龙粳33。"}, ensure_ascii=False)
                raise AssertionError("unexpected SQLQuery prompt")

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            self.assertIn("LIKE '%龙粳33%'", sql)
            return ReadonlyQueryResult(
                columns=("variety_name",),
                rows=(
                    {"variety_name": "龙粳33"},
                    {"variety_name": "龙粳331"},
                ),
                row_count=2,
            )

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            sql_query_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-sql"},
            sql_query_llm_client_factory=FakeSQLQueryLLMClient,
            sql_query_reasoning_effort="low",
            enable_sql_query_llm=True,
            skill_roots=[],
        )

        response = await self.submit_message(
            conversation_id="conv-sqlquery-llm-runtime",
            content="查询品种龙粳33的基因型信息",
            capability_id="sql_query.query",
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        prompt_names = ["sql_query.sql_generate" if "sql_query.sql_generate" in call["prompt"] else "sql_query.result_filtering" for call in calls]
        self.assertEqual(prompt_names, ["sql_query.sql_generate", "sql_query.result_filtering"])
        self.assertTrue(all(call["reasoning_effort"] == "low" for call in calls))
        self.assertTrue(all(call["thinking"] is False for call in calls))
        self.assertTrue(all(call["kwargs"]["config"]["model"] == "fake-sql" for call in calls))

        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        filtered = next(artifact for artifact in artifacts if "filtered_query_result" in artifact.artifact_id)
        filtered_payload = json.loads(filtered.storage_ref)
        self.assertEqual(filtered_payload["filter_source"], "llm")
        self.assertEqual(filtered_payload["rows"], [{"variety_name": "龙粳33"}])

        events = await self.runtime.storage.list_events_for_task(task_id)
        llm_call_nodes = [
            event.payload.get("node_name")
            for event in events
            if event.event_type == "sql_query.llm_call"
        ]
        self.assertIn("sql_query.sql_generate", llm_call_nodes)
        self.assertIn("sql_query.result_filtering", llm_call_nodes)
