from __future__ import annotations

import unittest

from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import NoAvailableInstanceError, Scheduler


class RegistrySchedulerTest(unittest.TestCase):
    def test_capability_registry_keeps_planner_payload_policy_with_descriptor(self) -> None:
        capability_registry = CapabilityRegistry()
        descriptor = CapabilityDescriptor(capability_id="cap.report", name="report", description="report capability")
        policy = CapabilityPayloadPolicy(planner_allowed_fields=("format",))

        capability_registry.register(descriptor, planner_payload_policy=policy)

        self.assertIs(capability_registry.get_planner_payload_policy("cap.report"), policy)
        self.assertEqual(capability_registry.planner_payload_policies(), {"cap.report": policy})

    def test_capability_registry_reregister_without_policy_removes_stale_policy(self) -> None:
        capability_registry = CapabilityRegistry()
        descriptor = CapabilityDescriptor(capability_id="cap.report", name="report", description="report capability")

        capability_registry.register(
            descriptor,
            planner_payload_policy=CapabilityPayloadPolicy(planner_allowed_fields=("format",)),
        )
        capability_registry.register(descriptor)

        self.assertIsNone(capability_registry.get_planner_payload_policy("cap.report"))

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
        scheduler = Scheduler(instance_registry)

        chosen = scheduler.select_instance("cap.route")
        self.assertEqual(chosen.instance_id, "inst-idle")

    def test_scheduler_rejects_when_no_matching_online_instance(self) -> None:
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(instance_id="inst-offline", supported_capabilities=("cap.route",), state=InstanceState.OFFLINE, load_score=0)
        )
        scheduler = Scheduler(instance_registry)

        with self.assertRaises(NoAvailableInstanceError):
            scheduler.select_instance("cap.route")
