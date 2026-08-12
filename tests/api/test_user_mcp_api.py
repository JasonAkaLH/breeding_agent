from __future__ import annotations

from datetime import datetime

from src.integrations.mcp.credentials import CredentialCipher
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.invalidation import InMemoryMCPInvalidationBus
from src.integrations.mcp.user_config import UserMCPConfigService
from tests.api.support import APITestCase


class _Resolver:
    def resolve(self, hostname: str, port: int):
        del hostname, port
        return ("8.8.8.8",)


class _HealthRunner:
    def __init__(self) -> None:
        self.servers = []

    async def start_test(self, server):
        self.servers.append(server)
        await self.storage.update_user_mcp_server(
            server.owner_user_id,
            server.server_id,
            changes={"health_status": "testing"},
            updated_at=datetime(2026, 8, 12, 12, 0, 0),
        )
        return f"attempt-{len(self.servers)}"


class UserMCPApiTest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.health_runner = _HealthRunner()
        self.health_runner.storage = self.runtime.storage
        self.runtime.user_mcp_config_service = UserMCPConfigService(
            storage=self.runtime.storage,
            credential_cipher=CredentialCipher(b"z" * 32),
            endpoint_policy=EndpointPolicy(resolver=_Resolver()),
            health_runner=self.health_runner,
            invalidation_bus=InMemoryMCPInvalidationBus(),
            now_fn=lambda: datetime(2026, 8, 12, 12, 0, 0),
        )

    async def test_crud_is_owner_scoped_and_response_never_contains_credential(self) -> None:
        created = await self.client.post(
            "/api/v1/mcp/servers",
            json={
                "display_name": "Private server",
                "endpoint_url": "https://example.com/mcp",
                "auth_type": "bearer",
                "credential": {"secret_value": "never-return-this"},
            },
        )
        self.assertEqual(created.status_code, 202, created.text)
        body = created.json()
        self.assertTrue(body["credential_configured"])
        self.assertNotIn("never-return-this", created.text)
        self.assertNotIn("credential_ciphertext", created.text)

        server_id = body["server_id"]
        await self.login("bob")
        hidden = await self.client.get(f"/api/v1/mcp/servers/{server_id}")
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/mcp/servers")).json(), {"servers": []})

        await self.login("acc-1")
        deleted = await self.client.delete(f"/api/v1/mcp/servers/{server_id}")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual((await self.client.get("/api/v1/mcp/servers")).json(), {"servers": []})

    async def test_reserved_owner_and_unsafe_endpoint_are_rejected(self) -> None:
        owner = await self.client.post(
            "/api/v1/mcp/servers",
            json={
                "display_name": "bad",
                "endpoint_url": "https://example.com/mcp",
                "owner_user_id": "bob",
            },
        )
        self.assertEqual(owner.status_code, 422)

        unsafe = await self.client.post(
            "/api/v1/mcp/servers",
            json={"display_name": "loopback", "endpoint_url": "http://127.0.0.1/mcp"},
        )
        self.assertEqual(unsafe.status_code, 422)
        self.assertEqual(unsafe.json()["detail"]["code"], "mcp_endpoint_ip_forbidden")

    async def test_delete_returns_202_when_a_live_lease_delays_finalization(self) -> None:
        class PendingDeleteService:
            async def delete_server(self, owner_user_id, server_id):
                del owner_user_id, server_id
                return False

            async def aclose(self):
                return None

        self.runtime.user_mcp_config_service = PendingDeleteService()

        response = await self.client.delete("/api/v1/mcp/servers/server-pending")

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            response.json(),
            {"server_id": "server-pending", "deletion_pending": True},
        )
