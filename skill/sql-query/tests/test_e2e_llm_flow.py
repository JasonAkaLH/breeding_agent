from __future__ import annotations

import _bootstrap  # noqa: F401

import json

from support import SQLQueryE2EAPITestCase


class SQLQueryLLMFlowE2ETest(SQLQueryE2EAPITestCase):
    async def test_fake_llm_happy_path_completes_without_real_provider(self) -> None:
        async def fake_llm(prompt: str) -> str:
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_name": "varchar(100)"},
                        "join_hints_used": [],
                    }
                )
            if '"stage": "result_filtering"' in prompt:
                return json.dumps({"keep_row_indexes": [0], "filter_reason": "保留龙粳33。"})
            raise AssertionError("unexpected prompt")

        await self.reconfigure_runtime(platform_llm_text_generator=fake_llm)
        response = await self.submit_message(conversation_id="conv-llm-happy", content="查询品种龙粳33的基因型信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        records = self.find_audit_records("skill.llm_call")
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["payload"].get("prompt_recorded") is False for record in records))


if __name__ == "__main__":
    import unittest

    unittest.main()
