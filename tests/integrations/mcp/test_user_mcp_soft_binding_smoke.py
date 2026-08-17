from __future__ import annotations

import unittest
from types import SimpleNamespace

from scripts.smoke_user_mcp_soft_binding import build_report
from src.core.enums import UserMCPHealthStatus


class _Gateway:
    def __init__(self) -> None:
        self.opened = []
        self.closed = []
        self.called = False

    async def open_scope(self, principal, task_id, server_id):
        self.opened.append((principal.username, task_id, server_id))
        return SimpleNamespace(scope_id="scope-1")

    async def list_tools(self, scope):
        self.scope = scope
        return SimpleNamespace(
            effective_protocol_version="2025-11-25",
            tools=(SimpleNamespace(name="ocr"), SimpleNamespace(name="status")),
        )

    async def close_scope(self, scope, reason):
        self.closed.append((scope.scope_id, reason))


class _Storage:
    async def get_user_mcp_server(self, owner_user_id, server_id):
        return SimpleNamespace(
            owner_user_id=owner_user_id,
            server_id=server_id,
            enabled=True,
            health_status=UserMCPHealthStatus.AVAILABLE,
            deletion_pending=False,
            deleted_at=None,
        )


class _Signer:
    def safe_reference(self, value, *, context):
        self.args = (value, context)
        return "safe-server-ref"


class UserMCPSoftBindingSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_discover_only_report_is_redacted_and_never_calls_tool(self) -> None:
        gateway = _Gateway()
        signer = _Signer()
        runtime = SimpleNamespace(
            user_mcp_gateway=gateway,
            storage=_Storage(),
            _mcp_audit_reference_signer=signer,
        )

        report = await build_report(
            runtime,
            owner_user_id="alice",
            server_id="mcp-ocr",
        )

        self.assertEqual(
            report,
            {
                "safe_server_ref": "safe-server-ref",
                "protocol_version": "2025-11-25",
                "discovery_succeeded": True,
                "tool_count": 2,
                "scope_closed": True,
                "tool_call_executed": False,
                "attachment_transmitted": False,
            },
        )
        self.assertEqual(signer.args, ("mcp-ocr", "mcp-server-binding-v1"))
        self.assertEqual(len(gateway.opened), 1)
        self.assertEqual(gateway.closed, [("scope-1", "soft_binding_discover_only_smoke")])
        self.assertNotIn("alice", repr(report))
        self.assertNotIn("mcp-ocr", repr(report))


if __name__ == "__main__":
    unittest.main()
