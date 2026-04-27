from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .models import OrchestrationRequest, WorkflowNodePlan

PayloadFactory = Callable[[OrchestrationRequest], Mapping[str, Any]]


def _empty_system_payload(_request: OrchestrationRequest) -> Mapping[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class CapabilityPayloadPolicy:
    """Allowlist for planner-provided payload fields on one public capability.

    Planner fields are fail-closed: only names in ``planner_allowed_fields`` are
    copied from the LLM output. System payload is computed from trusted request
    context and wins over planner fields with the same key.
    """

    planner_allowed_fields: tuple[str, ...] = ()
    system_payload_factory: PayloadFactory = _empty_system_payload

    def __post_init__(self) -> None:
        object.__setattr__(self, "planner_allowed_fields", tuple(self.planner_allowed_fields))

    def apply(self, node: WorkflowNodePlan, *, request: OrchestrationRequest) -> WorkflowNodePlan:
        allowed = set(self.planner_allowed_fields)
        planner_payload = {
            key: value
            for key, value in dict(node.input_payload).items()
            if key in allowed
        }
        system_payload = dict(self.system_payload_factory(request))
        return replace(node, input_payload={**planner_payload, **system_payload})


class PlannerPayloadPolicy:
    """Apply per-capability payload allowlists to LLM planner nodes."""

    def __init__(self, policies: Mapping[str, CapabilityPayloadPolicy] | None = None) -> None:
        self._policies = dict(policies or {})
        self._default_policy = CapabilityPayloadPolicy()

    def apply(self, node: WorkflowNodePlan, *, request: OrchestrationRequest) -> WorkflowNodePlan:
        policy = self._policies.get(node.capability_id, self._default_policy)
        return policy.apply(node, request=request)
