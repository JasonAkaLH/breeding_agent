from __future__ import annotations

import asyncio

from tests.api.support import APITestCase


class MainAgentLLMAPITest(APITestCase):
    async def test_default_message_uses_main_agent_and_streams_output_events(self) -> None:
        async def streamer(prompt: str):
            yield "你好"
            yield "，我是主代理"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=[])
        response = await self.client.post(
            "/api/v1/conversations/conv-main/messages",
            json={
                "account_id": "acc-1",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 1)

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        seen_types = set()
        deltas: list[str] = []
        while "task.completed" not in seen_types:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            seen_types.add(event.event_type)
            if event.event_type == "main_agent.output_delta":
                deltas.append(event.payload["delta"])

        self.assertEqual(deltas, ["你好", "，我是主代理"])
        self.assertIn("main_agent.output_final", seen_types)

    async def test_explicit_sql_query_capability_keeps_existing_chain(self) -> None:
        response = await self.submit_message(content="查询品种龙粳33的基因型信息", capability_id="sql_query.query")
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 6)


    async def test_rejects_old_and_internal_sql_query_capability_ids(self) -> None:
        for capability_id in ("nl2sql", "nl2sql.sql_generate", "sqlquery.query", "sql_query.sql_generate"):
            response = await self.submit_message(content="查询品种龙粳33的基因型信息", capability_id=capability_id)
            self.assertEqual(response.status_code, 400, capability_id)
            self.assertIn("Unsupported capability_id", response.json()["detail"])

    async def test_default_main_agent_uses_skill_catalog_from_configured_roots(self) -> None:
        skill_dir = self.workspace / "skills" / "report"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: report-writer
description: 生成周报
triggers:
  - 周报
---

# Report Writer
请使用汇报格式。
""",
            encoding="utf-8",
        )
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "ok"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=[self.workspace / "skills"])
        response = await self.client.post(
            "/api/v1/conversations/conv-skill/messages",
            json={
                "account_id": "acc-1",
                "content": "帮我写周报",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertIn("请使用汇报格式", prompts[0])


if __name__ == "__main__":
    import unittest

    unittest.main()
