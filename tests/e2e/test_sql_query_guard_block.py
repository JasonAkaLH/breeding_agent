from __future__ import annotations

from tests.e2e.support import E2EAPITestCase


class SQLQueryGuardBlockedE2ETest(E2EAPITestCase):
    async def test_guard_blocked_path_fails_task_and_writes_audit_evidence(self) -> None:
        await self.reconfigure_runtime(
            sql_generator=lambda context: "INSERT INTO variety(id) VALUES (1)",
        )

        response = await self.submit_message(content="查询品种龙粳33的基因型信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "failed")

        records = self.find_audit_records("sql_query.sql_guard_blocked")
        self.assertTrue(records)
        latest = records[-1]
        self.assertEqual(latest["task_id"], task_id)
        self.assertIn(latest["payload"]["block_reason"], {"statement_root_denied", "write_pattern_detected"})
        self.assertEqual(latest["payload"]["route_context"]["route_id"], "genotype_db")
