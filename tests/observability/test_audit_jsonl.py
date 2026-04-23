from __future__ import annotations

from tests.api.support import blocking_mysql_adapter
from tests.e2e.support import E2EAPITestCase


class AuditJsonlObservabilityTest(E2EAPITestCase):
    async def test_audit_jsonl_contains_block_and_cancel_evidence(self) -> None:
        await self.reconfigure_runtime(
            sql_generator=lambda context: "INSERT INTO variety(id) VALUES (1)",
        )
        blocked_response = await self.submit_message(conversation_id="conv-blocked", content="查询品种先玉335的基因型信息")
        blocked_task_id = blocked_response.json()["task_id"]
        blocked_terminal = await self.wait_for_terminal_task(blocked_task_id)
        self.assertEqual(blocked_terminal["status"], "failed")

        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        cancel_response = await self.submit_message(conversation_id="conv-cancel", content="查询品种先玉335的基因型信息")
        cancel_task_id = cancel_response.json()["task_id"]

        await self.wait_for_node_status(cancel_task_id, node_suffix=":sql_execute_readonly", status="running")
        await self.client.post(f"/api/v1/tasks/{cancel_task_id}/cancel")
        release.set()
        cancelled_terminal = await self.wait_for_terminal_task(cancel_task_id)
        self.assertEqual(cancelled_terminal["status"], "cancelled")

        records = self.read_audit_records()
        self.assertTrue(records)
        for record in records:
            self.assertIn("event_type", record)
            self.assertIn("task_id", record)
            self.assertIn("payload", record)

        blocked_records = [record for record in records if record["event_type"] == "nl2sql.sql_guard_blocked"]
        self.assertTrue(blocked_records)
        self.assertIn("block_reason", blocked_records[-1]["payload"])
        self.assertIn("route_context", blocked_records[-1]["payload"])

        cancel_records = [record for record in records if record["event_type"] == "task.context_terminated"]
        self.assertTrue(cancel_records)
