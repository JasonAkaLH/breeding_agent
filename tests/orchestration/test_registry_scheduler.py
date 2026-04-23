from __future__ import annotations

import unittest

from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import NoAvailableInstanceError, Scheduler


class RegistrySchedulerTest(unittest.TestCase):
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
