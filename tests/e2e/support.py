from __future__ import annotations

import json
from pathlib import Path

from tests.api.support import APITestCase


class E2EAPITestCase(APITestCase):
    async def wait_for_open_interrupt(self, task_id: str, *, timeout: float = 5.0) -> dict:
        async def _load_interrupt() -> dict | None:
            interrupts = await self.runtime.list_interrupts(task_id)
            for interrupt in interrupts:
                if interrupt["status"] == "open":
                    return interrupt
            return None

        found: dict | None = None

        async def _predicate() -> bool:
            nonlocal found
            found = await _load_interrupt()
            return found is not None

        await self.wait_for_condition(_predicate, timeout=timeout)
        assert found is not None
        return found

    def read_audit_records(self) -> list[dict]:
        audit_path = self.workspace / "audit.jsonl"
        if not audit_path.exists():
            return []
        return [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def find_audit_records(self, event_type: str) -> list[dict]:
        return [record for record in self.read_audit_records() if record["event_type"] == event_type]

    async def wait_for_node_status(self, task_id: str, *, node_suffix: str, status: str, timeout: float = 5.0) -> None:
        async def _predicate() -> bool:
            nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
            return any(
                node.node_id.endswith(node_suffix) and str(node.status) == status
                for node in nodes
            )

        await self.wait_for_condition(_predicate, timeout=timeout)

    async def wait_for_audit_event(self, event_type: str, *, timeout: float = 5.0) -> list[dict]:
        records: list[dict] = []

        async def _predicate() -> bool:
            nonlocal records
            records = self.find_audit_records(event_type)
            return bool(records)

        await self.wait_for_condition(_predicate, timeout=timeout)
        return records
