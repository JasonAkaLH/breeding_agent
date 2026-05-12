from __future__ import annotations

from tests.e2e.support import E2EAPITestCase


class InterruptResumeE2ETest(E2EAPITestCase):
    async def test_interrupt_answer_resume_completes_original_task(self) -> None:
        response = await self.submit_message(content="帮我查询一下", capability_id="sql_query.query")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        interrupt = await self.wait_for_open_interrupt(task_id)
        self.assertEqual(interrupt["reason_code"], "route_not_resolved")

        resumed = await self.runtime.answer_interrupt(
            task_id,
            interrupt["interrupt_id"],
            {"route_id": "approval_variety_db"},
        )
        self.assertEqual(resumed["status"], "answered")

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)

        answered_records = self.find_audit_records("lifecycle.interrupt_answered")
        self.assertTrue(answered_records)
        self.assertEqual(answered_records[-1]["task_id"], task_id)
