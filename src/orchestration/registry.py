from __future__ import annotations

from .models import CapabilityDescriptor, ExecutionInstance, OrchestrationRequest
from .planner_payload_policy import CapabilityPayloadPolicy


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDescriptor] = {}
        self._planner_payload_policies: dict[str, CapabilityPayloadPolicy] = {}

    def register(
        self,
        descriptor: CapabilityDescriptor,
        *,
        planner_payload_policy: CapabilityPayloadPolicy | None = None,
    ) -> None:
        self._capabilities[descriptor.capability_id] = descriptor
        if planner_payload_policy is None:
            self._planner_payload_policies.pop(descriptor.capability_id, None)
        else:
            self._planner_payload_policies[descriptor.capability_id] = planner_payload_policy

    def unregister(self, capability_id: str) -> None:
        self._capabilities.pop(capability_id, None)
        self._planner_payload_policies.pop(capability_id, None)

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        return self._capabilities.get(capability_id)

    def get_planner_payload_policy(self, capability_id: str) -> CapabilityPayloadPolicy | None:
        return self._planner_payload_policies.get(capability_id)

    def planner_payload_policies(self) -> dict[str, CapabilityPayloadPolicy]:
        return dict(self._planner_payload_policies)

    def list(self, *, public_only: bool = False) -> list[CapabilityDescriptor]:
        descriptors = list(self._capabilities.values())
        if public_only:
            return [descriptor for descriptor in descriptors if descriptor.public]
        return descriptors

    def list_for_request(
        self,
        request: OrchestrationRequest,
        *,
        public_only: bool = False,
    ) -> list[CapabilityDescriptor]:
        """Return descriptors visible for one trusted orchestration request.

        ``mcp.dispatch`` is intentionally absent unless the API/runtime supplied
        at least one safe, available server profile for the authenticated user.
        """

        descriptors = self.list(public_only=public_only)
        execution_path = str(request.metadata.get("mcp_execution_mode") or "").strip()
        if execution_path == "user_scoped":
            descriptors = [descriptor for descriptor in descriptors if not _is_legacy_mcp_descriptor(descriptor)]
        elif execution_path == "legacy":
            descriptors = [descriptor for descriptor in descriptors if descriptor.capability_id != "mcp.dispatch"]
        elif execution_path == "unavailable":
            descriptors = [
                descriptor
                for descriptor in descriptors
                if descriptor.capability_id != "mcp.dispatch" and not _is_legacy_mcp_descriptor(descriptor)
            ]

        if request.available_mcp_servers and execution_path not in {"legacy", "unavailable"}:
            return descriptors
        return [descriptor for descriptor in descriptors if descriptor.capability_id != "mcp.dispatch"]

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


def _is_legacy_mcp_descriptor(descriptor: CapabilityDescriptor) -> bool:
    return (
        descriptor.capability_id != "mcp.dispatch"
        and (
            descriptor.kind == "mcp_tool"
            or descriptor.source == "mcp"
            or descriptor.capability_id.startswith("mcp.")
        )
    )
