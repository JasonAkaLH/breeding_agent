from __future__ import annotations

import json
from collections import defaultdict, deque

from .models import WorkflowPlan
from .registry import CapabilityRegistry


class WorkflowPlanValidationError(ValueError):
    """Raised when a workflow plan violates the bounded DAG contract."""


class WorkflowPlanValidator:
    def __init__(self, capability_registry: CapabilityRegistry, *, public_only: bool = False) -> None:
        self._capability_registry = capability_registry
        self._public_only = public_only

    def validate(self, plan: WorkflowPlan) -> None:
        if not plan.nodes:
            raise WorkflowPlanValidationError("Workflow plan must contain at least one node.")

        node_ids: set[str] = set()
        for node in plan.nodes:
            if not node.node_id:
                raise WorkflowPlanValidationError("Workflow node_id must not be empty.")
            if node.node_id in node_ids:
                raise WorkflowPlanValidationError(f"Duplicate workflow node_id: {node.node_id}")
            node_ids.add(node.node_id)

            descriptor = self._capability_registry.get(node.capability_id)
            if descriptor is None or not descriptor.enabled:
                raise WorkflowPlanValidationError(f"Unknown or disabled capability: {node.capability_id}")
            if self._public_only and not descriptor.public:
                raise WorkflowPlanValidationError(f"Capability is not public: {node.capability_id}")

            try:
                json.dumps(node.input_payload)
            except (TypeError, ValueError) as exc:
                raise WorkflowPlanValidationError(
                    f"Input payload for node {node.node_id} must be JSON serializable."
                ) from exc
            try:
                json.dumps(node.metadata)
            except (TypeError, ValueError) as exc:
                raise WorkflowPlanValidationError(
                    f"Metadata for node {node.node_id} must be JSON serializable."
                ) from exc

        for node in plan.nodes:
            for dependency in node.depends_on:
                if dependency not in node_ids:
                    raise WorkflowPlanValidationError(
                        f"Node {node.node_id} has unknown dependency: {dependency}"
                    )

        self._ensure_acyclic(plan)

    @staticmethod
    def _ensure_acyclic(plan: WorkflowPlan) -> None:
        outgoing: dict[str, list[str]] = defaultdict(list)
        indegree: dict[str, int] = {node.node_id: 0 for node in plan.nodes}
        for node in plan.nodes:
            for dependency in node.depends_on:
                outgoing[dependency].append(node.node_id)
                indegree[node.node_id] += 1

        queue = deque(node_id for node_id, count in indegree.items() if count == 0)
        visited_count = 0
        while queue:
            current = queue.popleft()
            visited_count += 1
            for downstream in outgoing[current]:
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    queue.append(downstream)

        if visited_count != len(indegree):
            raise WorkflowPlanValidationError("Workflow plan contains a dependency cycle.")
