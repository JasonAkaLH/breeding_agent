from __future__ import annotations

import unittest

from src.integrations.mcp.invalidation import (
    InMemoryMCPInvalidationBus,
    MCPInvalidationAction,
    MCPServerInvalidated,
    mcp_invalidation_notify_sql,
    validate_mcp_server_invalidation,
)


class UserMCPInvalidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_bus_publishes_redacted_identity_and_version_only(self) -> None:
        received: list[MCPServerInvalidated] = []
        bus = InMemoryMCPInvalidationBus()
        unsubscribe = bus.subscribe(received.append)
        event = MCPServerInvalidated(
            owner_user_id="alice",
            server_id="srv-1",
            security_version=3,
            action=MCPInvalidationAction.SECURITY_UPDATED,
        )

        await bus.publish(event)
        unsubscribe()
        await bus.publish(event)

        self.assertEqual(received, [event])
        self.assertEqual(
            event.public_payload(),
            {
                "owner_user_id": "alice",
                "server_id": "srv-1",
                "security_version": 3,
                "action": "security_updated",
            },
        )

    def test_payload_validation_rejects_unknown_action_and_negative_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate_mcp_server_invalidation(
                {"owner_user_id": "alice", "server_id": "srv", "security_version": 1, "action": "credential"}
            )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            validate_mcp_server_invalidation(
                {"owner_user_id": "alice", "server_id": "srv", "security_version": -1, "action": "deleted"}
            )

    def test_postgres_notify_payload_contains_no_endpoint_or_credentials(self) -> None:
        sql, parameters = mcp_invalidation_notify_sql(
            MCPServerInvalidated("alice", "srv", 7, MCPInvalidationAction.DELETED)
        )
        self.assertIn("pg_notify", sql)
        self.assertEqual(
            parameters["payload"],
            '{"action": "deleted", "owner_user_id": "alice", "security_version": 7, "server_id": "srv"}',
        )
