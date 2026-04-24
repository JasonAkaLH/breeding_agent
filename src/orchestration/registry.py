from __future__ import annotations

from .models import CapabilityDescriptor, ExecutionInstance


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDescriptor] = {}

    def register(self, descriptor: CapabilityDescriptor) -> None:
        self._capabilities[descriptor.capability_id] = descriptor

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        return self._capabilities.get(capability_id)

    def list(self, *, public_only: bool = False) -> list[CapabilityDescriptor]:
        descriptors = list(self._capabilities.values())
        if public_only:
            return [descriptor for descriptor in descriptors if descriptor.public]
        return descriptors

    def require(self, capability_id: str) -> CapabilityDescriptor:
        descriptor = self.get(capability_id)
        if descriptor is None or not descriptor.enabled:
            raise ValueError(f"Unknown or disabled capability: {capability_id}")
        return descriptor


class InstanceRegistry:
    def __init__(self) -> None:
        self._instances: dict[str, ExecutionInstance] = {}

    def register(self, instance: ExecutionInstance) -> None:
        self._instances[instance.instance_id] = instance

    def list(self) -> list[ExecutionInstance]:
        return list(self._instances.values())
