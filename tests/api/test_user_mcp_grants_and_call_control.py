from __future__ import annotations

from datetime import datetime

from src.core.enums import TaskStatus
from src.core.models import Conversation, Task
from src.core.models import UserMCPToolGrant
from src.integrations.mcp.gateway_models import (
    CancelOutcome,
    ContinueOutcome,
    MCPCancelStatus,
    MCPContinueStatus,
)
from tests.api.test_user_mcp_api import UserMCPApiTest


class UserMCPGrantApiTest(UserMCPApiTest):
    async def _create_server(self) -> dict:
        response = await self.client.post(
            "/api/v1/mcp/servers",
            json={
                "display_name": "Grant server",
                "endpoint_url": "https://example.com/mcp",
                "auth_type": "none",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    async def test_grants_are_owner_scoped_and_can_be_cleared(self) -> None:
        server = await self._create_server()
        await self.runtime.storage.save_user_mcp_tool_grant(
            UserMCPToolGrant(
                grant_id="grant-1",
                owner_user_id="acc-1",
                server_id=server["server_id"],
                tool_name="lookup",
                server_security_version=1,
                input_schema_sha256="a" * 64,
                granted_at=datetime(2026, 8, 12, 12, 0, 0),
            )
        )

        listed = await self.client.get("/api/v1/mcp/grants")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["grants"][0]["server_display_name"], "Grant server")
        self.assertTrue(listed.json()["grants"][0]["valid"])

        await self.login("bob")
        self.assertEqual((await self.client.get("/api/v1/mcp/grants")).json(), {"grants": []})
        hidden = await self.client.delete("/api/v1/mcp/grants/grant-1")
        self.assertEqual(hidden.status_code, 404)

        await self.login("acc-1")
        cleared = await self.client.delete(
            f"/api/v1/mcp/servers/{server['server_id']}/grants"
        )
        self.assertEqual(cleared.status_code, 204, cleared.text)
        self.assertEqual((await self.client.get("/api/v1/mcp/grants")).json(), {"grants": []})


class UserMCPCallControlApiTest(UserMCPApiTest):
    async def test_call_control_maps_unknown_and_terminal_states(self) -> None:
        class Gateway:
            async def aclose(self):
                return None

            async def continue_call_for_task(self, task_id, call_ref):
                del task_id
                return ContinueOutcome(
                    MCPContinueStatus.UNKNOWN_CALL
                    if call_ref == "missing"
                    else MCPContinueStatus.ALREADY_TERMINAL
                )

            async def cancel_call_for_task(self, task_id, call_ref, reason):
                del task_id, reason
                return CancelOutcome(
                    MCPCancelStatus.UNKNOWN_CALL
                    if call_ref == "missing"
                    else MCPCancelStatus.REMOTE_STOP_UNKNOWN,
                    False,
                )

        self.runtime.user_mcp_gateway = Gateway()
        task_id = "task-call-control"
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-call-control", username="acc-1")
        )
        await self.runtime.storage.save_task(
            Task(
                task_id=task_id,
                conversation_id="conv-call-control",
                root_message_id="msg-call-control",
                status=TaskStatus.RUNNING,
            )
        )

        missing = await self.client.post(
            f"/api/v1/tasks/{task_id}/mcp-calls/missing/continue"
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        terminal = await self.client.post(
            f"/api/v1/tasks/{task_id}/mcp-calls/terminal/continue"
        )
        self.assertEqual(terminal.status_code, 409, terminal.text)
        cancel = await self.client.post(
            f"/api/v1/tasks/{task_id}/mcp-calls/running/cancel"
        )
        self.assertEqual(cancel.status_code, 202, cancel.text)
        self.assertEqual(cancel.json()["status"], "remote_stop_unknown")
