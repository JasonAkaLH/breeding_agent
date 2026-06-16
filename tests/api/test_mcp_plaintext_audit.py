from __future__ import annotations

import json

from tests.api.support import APITestCase


class FakeMCPClient:
    server_capabilities = {"tools": {}}

    async def list_tools(self):
        return [{"name": "echo", "inputSchema": {"type": "object"}}]

    async def call_tool(self, tool_name, arguments, **_kwargs):
        return {"content": [], "structuredContent": dict(arguments)}

    async def close(self):
        return None


class MCPPlaintextAuditTests(APITestCase):
    async def test_plaintext_http_audit_records_security_fields_without_header_values(self) -> None:
        await self.reconfigure_runtime(
            mcp_config={
                "enabled": True,
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "http://mcp.internal/rpc",
                        "transport": "streamable_http",
                        "headers": {"X-Example-Tenant": "tenant-test"},
                        "tools": [
                            {
                                "tool_name": "echo",
                                "expose": True,
                                "capability_id": "mcp.crm.echo",
                                "public_name": "Echo",
                                "public_description": "Echo via MCP.",
                                "risk_level": "read_only",
                            }
                        ],
                    }
                ],
            },
            mcp_client_factory=lambda _server: FakeMCPClient(),
        )

        records = [json.loads(line) for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        completed = [record for record in records if record["event_type"] == "mcp.server_discovery_completed"][-1]
        registered = [record for record in records if record["event_type"] == "mcp.capability_registered" and record["payload"].get("capability_id") == "mcp.crm.echo"][-1]
        encoded = json.dumps(records, ensure_ascii=False)

        self.assertEqual(completed["payload"]["transport_security"], ["plaintext_http"])
        self.assertEqual(completed["payload"]["header_names"], ["X-Example-Tenant"])
        self.assertFalse(completed["payload"]["credential_over_plaintext_http"])
        self.assertEqual(registered["payload"]["transport_security"], "plaintext_http")
        self.assertEqual(registered["payload"]["header_names"], ["X-Example-Tenant"])
        self.assertNotIn("tenant-test", encoded)


if __name__ == "__main__":
    import unittest

    unittest.main()
