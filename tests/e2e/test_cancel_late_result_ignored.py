from __future__ import annotations

from tests.api.support import blocking_mysql_adapter
from tests.e2e.support import E2EAPITestCase


class CancelLateResultE2ETest(E2EAPITestCase):
    async def test_cancelled_task_discards_late_result(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message(content="查询品种龙粳33的基因型信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        await self.wait_for_node_status(task_id, node_suffix=":skill_execute", status="running")

        cancel_response = await self.client.post(f"/api/v1/tasks/{task_id}/cancel")
        self.assertEqual(cancel_response.status_code, 202)

        release.set()
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "cancelled")

        late_records = await self.wait_for_audit_event("task.late_result_discarded")
        self.assertTrue(late_records)
        self.assertEqual(late_records[-1]["task_id"], task_id)
