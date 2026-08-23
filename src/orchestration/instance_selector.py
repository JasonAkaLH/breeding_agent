from __future__ import annotations

from .models import ExecutionInstance, InstanceState
from .registry import InstanceRegistry


class NoAvailableInstanceError(RuntimeError):
    """Raised when no compatible execution instance is available."""


class InstanceSelector:
    def __init__(self, instance_registry: InstanceRegistry) -> None:
        self._instance_registry = instance_registry

    def select_instance(self, capability_id: str) -> ExecutionInstance:
        candidates = [
            instance
            for instance in self._instance_registry.list()
            if capability_id in instance.supported_capabilities
            and instance.state == InstanceState.ONLINE
        ]
        if not candidates:
            raise NoAvailableInstanceError(
                f"No online instance supports capability {capability_id}."
            )
        return sorted(candidates, key=lambda item: (item.load_score, item.instance_id))[0]
