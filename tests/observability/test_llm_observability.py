from __future__ import annotations

from tests.e2e.support import E2EAPITestCase


class LLMObservabilityTest(E2EAPITestCase):
    async def test_llm_fallback_audit_records_metadata_without_prompt_or_rows(self) -> None:
        async def broken_llm(_: str) -> str:
            return "not json"

        await self.reconfigure_runtime(llm_text_generator=broken_llm)
        response = await self.submit_message(conversation_id="conv-llm-fallback", content="查询品种龙粳33的基因型信息")
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        records = self.find_audit_records("sql_query.llm_fallback")
        self.assertTrue(records)
        for record in records:
            payload = record["payload"]
            self.assertIn("fallback_reason", payload)
            self.assertFalse(payload.get("prompt_recorded"))
            self.assertNotIn("prompt", payload)
            self.assertNotIn("rows", payload)
            self.assertNotIn("api_key", payload)


if __name__ == "__main__":
    import unittest

    unittest.main()
