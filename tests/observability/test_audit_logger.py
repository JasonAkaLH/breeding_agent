from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.integrations.audit_logger import JsonlAuditSink


class JsonlAuditSinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_and_sync_records_are_appended_as_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "audit" / "events.jsonl"
            sink = JsonlAuditSink(path)

            sink.record_sync("runtime.started", {"component": "api"}, task_id="task-sync")
            await sink.record("task.completed", {"status": "ok"}, conversation_id="conv-1", task_id="task-1", node_id="node-1")

            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([record["event_type"] for record in records], ["runtime.started", "task.completed"])
        self.assertEqual(records[0]["payload"], {"component": "api"})
        self.assertEqual(records[1]["conversation_id"], "conv-1")
        self.assertEqual(records[1]["node_id"], "node-1")
        self.assertIn("recorded_at", records[1])


if __name__ == "__main__":
    unittest.main()
