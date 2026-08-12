from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

from src.lifecycle.mcp_presence import MCPPresenceConnection, MCPTaskPresenceService


class MCPTaskPresenceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cancelled: list[tuple[str, str, str]] = []

        async def cancel(owner: str, task_id: str, reason: str) -> None:
            self.cancelled.append((owner, task_id, reason))

        self.service = MCPTaskPresenceService(
            cancel_mcp_task=cancel,
            grace_period_seconds=0.02,
        )

    async def asyncTearDown(self) -> None:
        await self.service.aclose()

    async def test_only_last_disconnect_starts_offline_cancellation(self) -> None:
        await self.service.connect(MCPPresenceConnection("c1", "task-1", "alice", 1))
        await self.service.connect(MCPPresenceConnection("c2", "task-1", "alice", 1))

        await self.service.disconnect("c1")
        await asyncio.sleep(0.03)
        self.assertEqual(self.cancelled, [])

        await self.service.disconnect("c2")
        await asyncio.sleep(0.03)
        self.assertEqual(self.cancelled, [("alice", "task-1", "offline_grace_expired")])

    async def test_reconnect_during_grace_clears_offline_timer(self) -> None:
        await self.service.connect(MCPPresenceConnection("c1", "task-1", "alice", 1))
        await self.service.disconnect("c1")
        await asyncio.sleep(0.005)
        await self.service.connect(MCPPresenceConnection("c2", "task-1", "alice", 1))

        await asyncio.sleep(0.03)
        self.assertEqual(self.cancelled, [])

    async def test_auth_invalidation_cancels_immediately(self) -> None:
        await self.service.connect(MCPPresenceConnection("c1", "task-1", "alice", 7))

        await self.service.invalidate_owner("alice", auth_generation=7)

        self.assertEqual(self.cancelled, [("alice", "task-1", "auth_invalidated")])
        self.assertEqual(await self.service.active_connection_count("alice", "task-1"), 0)

    async def test_disconnect_removes_each_persistent_lease_before_last_disconnect(self) -> None:
        class Storage:
            def __init__(self) -> None:
                self.leases: dict[str, object] = {}

            async def save_mcp_connection_lease(self, lease: object) -> None:
                self.leases[getattr(lease, "connection_id")] = lease

            async def delete_mcp_connection_lease(
                self, owner_user_id: str, task_id: str, connection_id: str
            ) -> bool:
                return self.leases.pop(connection_id, None) is not None

            async def list_live_mcp_connection_leases(
                self, owner_user_id: str, task_id: str, *, now: datetime
            ) -> list[object]:
                return [
                    lease
                    for lease in self.leases.values()
                    if getattr(lease, "owner_user_id") == owner_user_id
                    and getattr(lease, "task_id") == task_id
                    and getattr(lease, "lease_expires_at") > now
                ]

        storage = Storage()
        await self.service.aclose()
        self.service = MCPTaskPresenceService(
            cancel_mcp_task=lambda *_args: asyncio.sleep(0),
            grace_period_seconds=0.02,
            storage=storage,
        )
        await self.service.connect(MCPPresenceConnection("c1", "task-1", "alice", 1))
        await self.service.connect(MCPPresenceConnection("c2", "task-1", "alice", 1))

        await self.service.disconnect("c1")

        self.assertNotIn("c1", storage.leases)
        self.assertIn("c2", storage.leases)
