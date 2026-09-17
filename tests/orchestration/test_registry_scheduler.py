from __future__ import annotations

import unittest

from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState
from src.orchestration.agent_loop.tool_catalog import CapabilityInvocationPolicy
from src.orchestration.instance_selector import InstanceSelector, NoAvailableInstanceError
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry


class RegistryInstanceSelectorTest(unittest.TestCase):
    def test_capability_registry_keeps_invocation_policy_with_descriptor(self) -> None:
        capability_registry = CapabilityRegistry()
        descriptor = CapabilityDescriptor(capability_id="cap.report", name="report", description="report capability")
        policy = CapabilityInvocationPolicy(
            model_allowed_fields=("format",),
            input_schema={"type": "object", "properties": {"format": {"type": "string"}}},
        )

        capability_registry.register(descriptor, invocation_policy=policy)

        self.assertIs(capability_registry.invocation_policies()["cap.report"], policy)

    def test_capability_registry_reregister_without_policy_removes_stale_policy(self) -> None:
        capability_registry = CapabilityRegistry()
        descriptor = CapabilityDescriptor(capability_id="cap.report", name="report", description="report capability")

        capability_registry.register(
            descriptor,
            invocation_policy=CapabilityInvocationPolicy(
                model_allowed_fields=("format",),
                input_schema={"type": "object", "properties": {"format": {"type": "string"}}},
            ),
        )
        capability_registry.register(descriptor)

        self.assertNotIn("cap.report", capability_registry.invocation_policies())

    def test_scheduler_picks_lowest_load_matching_online_instance(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(
            CapabilityDescriptor(capability_id="cap.route", name="route", description="route capability")
        )
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(instance_id="inst-busy", supported_capabilities=("cap.route",), state=InstanceState.ONLINE, load_score=5)
        )
        instance_registry.register(
            ExecutionInstance(instance_id="inst-idle", supported_capabilities=("cap.route",), state=InstanceState.ONLINE, load_score=1)
        )
        selector = InstanceSelector(instance_registry)

        chosen = selector.select_instance("cap.route")
        self.assertEqual(chosen.instance_id, "inst-idle")

    def test_scheduler_rejects_when_no_matching_online_instance(self) -> None:
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(instance_id="inst-offline", supported_capabilities=("cap.route",), state=InstanceState.OFFLINE, load_score=0)
        )
        selector = InstanceSelector(instance_registry)

        with self.assertRaises(NoAvailableInstanceError):
            selector.select_instance("cap.route")
