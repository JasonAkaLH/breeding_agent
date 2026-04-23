from __future__ import annotations

from tests.e2e.support import E2EAPITestCase


class InterruptResumeE2ETest(E2EAPITestCase):
    async def test_interrupt_answer_resume_completes_original_task(self) -> None:
        response = await self.submit_message(content="查询某个品种的审定信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        interrupt = await self.wait_for_open_interrupt(task_id)
        self.assertEqual(interrupt["reason_code"], "crop_not_resolved")

        resumed = await self.runtime.answer_interrupt(
            task_id,
            interrupt["interrupt_id"],
            {"crop": "玉米"},
        )
        self.assertEqual(resumed["status"], "answered")

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 6)

        answered_records = self.find_audit_records("lifecycle.interrupt_answered")
        self.assertTrue(answered_records)
        self.assertEqual(answered_records[-1]["task_id"], task_id)
