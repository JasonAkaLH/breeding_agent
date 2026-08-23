from __future__ import annotations

from typing import TYPE_CHECKING

from .models import CapabilityDescriptor, ExecutionInstance
if TYPE_CHECKING:
    from .agent_loop.tool_catalog import CapabilityInvocationPolicy


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDescriptor] = {}
        self._invocation_policies: dict[str, CapabilityInvocationPolicy] = {}

    def register(
        self,
        descriptor: CapabilityDescriptor,
        *,
        invocation_policy: CapabilityInvocationPolicy | None = None,
    ) -> None:
        self._capabilities[descriptor.capability_id] = descriptor
        if invocation_policy is None:
            self._invocation_policies.pop(descriptor.capability_id, None)
        else:
            self._invocation_policies[descriptor.capability_id] = invocation_policy

    def unregister(self, capability_id: str) -> None:
        self._capabilities.pop(capability_id, None)
        self._invocation_policies.pop(capability_id, None)

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        return self._capabilities.get(capability_id)

    def invocation_policies(self) -> dict[str, CapabilityInvocationPolicy]:
        return dict(self._invocation_policies)

    def list(self, *, public_only: bool = False) -> list[CapabilityDescriptor]:
        descriptors = list(self._capabilities.values())
        if public_only:
            return [descriptor for descriptor in descriptors if descriptor.public]
        return descriptors

    def list_for_visibility(
        self,
        context: object,
        *,
        public_only: bool = True,
    ) -> list[CapabilityDescriptor]:
        descriptors = self.list(public_only=public_only)
        descriptors = [descriptor for descriptor in descriptors if descriptor.enabled]
        allowlist = getattr(context, "public_capability_allowlist", None)
        if allowlist is not None:
            descriptors = [
                descriptor
                for descriptor in descriptors
                if descriptor.capability_id in allowlist
            ]
        execution_path = str(getattr(context, "execution_path", "default") or "default").strip()
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

        safe_profiles = tuple(getattr(context, "safe_mcp_server_profiles", ()) or ())
        if safe_profiles and execution_path not in {"legacy", "unavailable"}:
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
