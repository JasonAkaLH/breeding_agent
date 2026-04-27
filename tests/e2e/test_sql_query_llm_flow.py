from __future__ import annotations

import json

from tests.e2e.support import E2EAPITestCase


class SQLQueryLLMFlowE2ETest(E2EAPITestCase):
    async def test_fake_llm_happy_path_completes_without_real_provider(self) -> None:
        async def fake_llm(prompt: str) -> str:
            if "sql_query.sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety LIMIT 20",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_name": "varchar(100)"},
                        "join_hints_used": [],
                    }
                )
            if "sql_query.result_summarize" in prompt:
                return json.dumps({"summary": "查询返回 1 行，品种为龙粳33。"})
            raise AssertionError("unexpected prompt")

        await self.reconfigure_runtime(llm_text_generator=fake_llm)
        response = await self.submit_message(conversation_id="conv-llm-happy", content="查询品种龙粳33的基因型信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        records = self.find_audit_records("sql_query.llm_call")
        self.assertGreaterEqual(len(records), 2)
        self.assertTrue(all(record["payload"].get("prompt_recorded") is False for record in records))


if __name__ == "__main__":
    import unittest

    unittest.main()
