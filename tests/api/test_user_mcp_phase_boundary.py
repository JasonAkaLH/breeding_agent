from __future__ import annotations

import inspect
import json
from dataclasses import dataclass

from src.orchestration.models import OrchestrationRequest
from tests.api.support import APITestCase


@dataclass(frozen=True)
class PersistedUserMCPServer:
    owner_user_id: str
    server_id: str
    display_name: str
    endpoint_url: str
    tool_name: str


class FakePersistedUserMCPConfigSource:
    """Test seam for the user-config repository introduced after this boundary."""

    def __init__(self, servers: tuple[PersistedUserMCPServer, ...]) -> None:
        self.servers = servers

    def list_for_owner(self, owner_user_id: str) -> tuple[PersistedUserMCPServer, ...]:
        return tuple(server for server in self.servers if server.owner_user_id == owner_user_id)


class RecordingLegacyMCPClientFactory:
    def __init__(self) -> None:
        self.server_ids: list[str] = []

    def __call__(self, server):
        self.server_ids.append(server.server_id)
        raise AssertionError("disabled legacy MCP startup must not construct a client")


class UserMCPPhaseBoundaryAPITests(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.user_config_source = FakePersistedUserMCPConfigSource(
            (
                PersistedUserMCPServer(
                    owner_user_id="acc-1",
                    server_id="user-private-crm",
                    display_name="Private CRM",
                    endpoint_url="https://user-mcp.example.test/rpc",
                    tool_name="lookup_private_customer",
                ),
            )
        )
        self.legacy_client_factory = RecordingLegacyMCPClientFactory()

    async def _configure_boundary_runtime(self, *, planner_text_generator=None) -> None:
        self.assertEqual(len(self.user_config_source.list_for_owner("acc-1")), 1)
        await self.reconfigure_runtime(
            mcp_config={"enabled": False, "servers": []},
            mcp_client_factory=self.legacy_client_factory,
            planner_text_generator=planner_text_generator,
        )

    async def test_persisted_user_server_does_not_enter_capability_registry(self) -> None:
        await self._configure_boundary_runtime()

        capability_ids = {descriptor.capability_id for descriptor in self.runtime.capability_registry.list()}

        self.assertNotIn("mcp.user-private-crm.lookup_private_customer", capability_ids)

    async def test_persisted_user_server_does_not_enter_planner_prompt(self) -> None:
        planner_prompts: list[str] = []

        def planner(prompt, **_kwargs):
            planner_prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "respond",
                            "capability_id": "main_agent.respond",
                            "input_payload": {},
                        }
                    ]
                }
            )

        await self._configure_boundary_runtime(planner_text_generator=planner)
        request = OrchestrationRequest(
            task_id="task-phase-boundary",
            conversation_id="conv-phase-boundary",
            root_message_id="msg-phase-boundary",
            user_message="查一下我的私有客户",
        )

        plan = self.runtime.workflow_provider.build_plan(request)
        if inspect.isawaitable(plan):
            await plan

        self.assertNotIn("user-private-crm", planner_prompts[0])
        self.assertNotIn("lookup_private_customer", planner_prompts[0])

    async def test_persisted_user_server_does_not_enter_legacy_active_bundle(self) -> None:
        await self._configure_boundary_runtime()

        capability_ids = {
            descriptor.capability_id
            for descriptor in self.runtime._mcp_runtime_state.active_bundle.descriptors
        }

        self.assertNotIn("mcp.user-private-crm.lookup_private_customer", capability_ids)

    async def test_persisted_user_server_does_not_trigger_legacy_startup_discovery(self) -> None:
        await self._configure_boundary_runtime()

        self.assertEqual(self.legacy_client_factory.server_ids, [])
