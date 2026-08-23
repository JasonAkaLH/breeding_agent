from __future__ import annotations

import unittest

from src.orchestration.agent_loop.tool_catalog import (
    AgentToolCatalogBuilder,
    CapabilityInvocationPolicy,
    CapabilityVisibilityContext,
)
from src.orchestration.models import CapabilityDescriptor, UserMCPServerProfile
from src.orchestration.registry import CapabilityRegistry


def _policy(*fields: str, system: dict[str, object] | None = None) -> CapabilityInvocationPolicy:
    properties = {field: {"type": "string"} for field in fields}
    for key in system or {}:
        properties[key] = {"type": "string"}
    return CapabilityInvocationPolicy(
        model_allowed_fields=fields,
        input_schema={
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
        system_payload_factory=lambda _context: dict(system or {}),
    )


class AgentToolCatalogTest(unittest.TestCase):
    def test_visibility_is_closed_and_outer_catalog_does_not_expand_mcp_tools(self) -> None:
        registry = CapabilityRegistry()
        for descriptor in (
            CapabilityDescriptor("skill.public", "public", "safe", kind="skill", source="skill"),
            CapabilityDescriptor("skill.private", "private", "hidden", public=False, kind="skill", source="skill"),
            CapabilityDescriptor("skill.disabled", "disabled", "hidden", enabled=False, kind="skill", source="skill"),
            CapabilityDescriptor("mcp.dispatch", "dispatch", "safe dispatch", kind="mcp_dispatch"),
            CapabilityDescriptor("mcp.server.tool", "expanded", "must stay hidden", kind="mcp_tool", source="mcp"),
            CapabilityDescriptor("system.internal", "internal", "not an outer tool"),
        ):
            registry.register(descriptor)
        policies = {
            "skill.public": _policy("query"),
            "skill.private": _policy("query"),
            "skill.disabled": _policy("query"),
            "mcp.dispatch": _policy("server_id"),
            "mcp.server.tool": _policy("query"),
            "system.internal": _policy("query"),
        }
        profile = UserMCPServerProfile("server-1", "Server", "safe", "streamable_http")
        context = CapabilityVisibilityContext(
            authenticated_owner_scope="owner-1",
            execution_path="user_scoped",
            safe_mcp_server_profiles=(profile,),
            public_capability_allowlist=frozenset({"skill.public", "mcp.dispatch", "mcp.server.tool"}),
        )

        catalog = AgentToolCatalogBuilder(registry, policies).build(context)

        self.assertEqual(
            [tool.capability_id for tool in catalog.tools],
            ["mcp.dispatch", "skill.public"],
        )
        dispatch = catalog.tools[0]
        self.assertEqual(dispatch.input_schema["properties"]["server_id"]["enum"], ["server-1"])
        self.assertNotIn("mcp.server.tool", repr(catalog.tools))

    def test_missing_policy_and_hot_reload_are_reflected_without_stale_catalog(self) -> None:
        registry = CapabilityRegistry()
        first = CapabilityDescriptor("skill.first", "first", "first", kind="skill", source="skill")
        registry.register(first, invocation_policy=_policy("query"))
        builder = AgentToolCatalogBuilder(registry)
        context = CapabilityVisibilityContext("owner")
        self.assertEqual([tool.capability_id for tool in builder.build(context).tools], ["skill.first"])

        registry.unregister("skill.first")
        registry.register(CapabilityDescriptor("skill.no_policy", "none", "none", kind="skill", source="skill"))
        self.assertEqual(builder.build(context).tools, ())
        registry.register(
            CapabilityDescriptor("skill.hot", "hot", "hot", kind="skill", source="skill"),
            invocation_policy=_policy("query"),
        )
        self.assertEqual([tool.capability_id for tool in builder.build(context).tools], ["skill.hot"])

    def test_policy_filters_model_fields_then_system_authority_wins(self) -> None:
        policy = _policy("query", "owner_id", system={"owner_id": "trusted-owner"})
        effective = policy.effective_payload(
            {"query": "safe", "owner_id": "attacker", "credential": "secret"},
            context=CapabilityVisibilityContext("trusted-owner"),
        )
        self.assertEqual(effective, {"query": "safe", "owner_id": "trusted-owner"})
        self.assertNotIn("credential", effective)
