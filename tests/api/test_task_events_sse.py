from __future__ import annotations

import asyncio

from tests.api.support import APITestCase, blocking_mysql_adapter


class TaskEventsSSEAPITest(APITestCase):
    async def test_task_events_endpoint_replays_history_and_streams_live_completion(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message()
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        first = await asyncio.wait_for(iterator.__anext__(), timeout=2)
        second = await asyncio.wait_for(iterator.__anext__(), timeout=2)
        replay_types = {first.event_type, second.event_type}
        self.assertIn("task.accepted", replay_types)
        self.assertIn("task.graph_created", replay_types)

        release.set()

        seen_types = set(replay_types)
        while "task.completed" not in seen_types:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            seen_types.add(event.event_type)

        self.assertIn("node.started", seen_types)
        self.assertIn("task.completed", seen_types)
