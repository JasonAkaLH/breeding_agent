from __future__ import annotations

import _bootstrap  # noqa: F401

from tests.api.support import blocking_mysql_adapter
from support import SQLQueryE2EAPITestCase
from sql_query_skill.platform_handler import SQLQueryPlatformHandler


class AuditJsonlObservabilityTest(SQLQueryE2EAPITestCase):
    async def test_audit_jsonl_contains_block_and_cancel_evidence(self) -> None:
        await self.reconfigure_runtime(
            skill_platform_handlers={
                "skill.sql_query.platform_handler": SQLQueryPlatformHandler(
                    sql_generator=lambda context: "INSERT INTO variety(id) VALUES (1)"
                )
            },
            trusted_skill_handlers={"skill.sql_query": "skill.sql_query.platform_handler"},
            trusted_skill_services={
                "skill.sql_query": ("mysql_readonly", "llm.non_stream", "artifact_writer", "progress_events")
            },
        )
        blocked_response = await self.submit_message(conversation_id="conv-blocked", content="查询品种龙粳33的基因型信息")
        blocked_task_id = blocked_response.json()["task_id"]
        blocked_terminal = await self.wait_for_terminal_task(blocked_task_id)
        self.assertEqual(blocked_terminal["status"], "failed")

        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        cancel_response = await self.submit_message(conversation_id="conv-cancel", content="查询品种龙粳33的基因型信息")
        cancel_task_id = cancel_response.json()["task_id"]

        await self.wait_for_node_status(cancel_task_id, node_suffix=":skill_execute", status="running")
        await self.client.post("/api/v1/tasks/cancel", json={"task_id": cancel_task_id})
        release.set()
        cancelled_terminal = await self.wait_for_terminal_task(cancel_task_id)
        self.assertEqual(cancelled_terminal["status"], "cancelled")

        records = self.read_audit_records()
        self.assertTrue(records)
        for record in records:
            self.assertIn("event_type", record)
            self.assertIn("task_id", record)
            self.assertIn("payload", record)

        blocked_records = [record for record in records if record["event_type"] == "skill.sql_guard_blocked"]
        self.assertTrue(blocked_records)
        self.assertIn("block_reason", blocked_records[-1]["payload"])
        self.assertIn("route_context", blocked_records[-1]["payload"])

        cancel_records = [record for record in records if record["event_type"] == "task.context_terminated"]
        self.assertTrue(cancel_records)
