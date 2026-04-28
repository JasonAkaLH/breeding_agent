from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from typing import Mapping, Protocol

from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


class WorkflowProvider(Protocol):
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        ...


class WorkflowExpansionError(ValueError):
    """Raised when a high-level workflow cannot be expanded safely."""


class WorkflowExpander:
    def __init__(self, macro_providers: Mapping[str, WorkflowProvider]) -> None:
        self._macro_providers = dict(macro_providers)

    def expand(self, plan: WorkflowPlan, *, request: OrchestrationRequest) -> WorkflowPlan:
        ordered_nodes = self._topological_nodes(plan)
        expanded_nodes: list[WorkflowNodePlan] = []
        expanded_tail_ids_by_original: dict[str, tuple[str, ...]] = {}
        expanded_macro_nodes: dict[str, dict[str, object]] = {}
        max_replans = plan.max_replans
        max_dynamic_nodes = plan.max_dynamic_nodes

        for node in ordered_nodes:
            high_level_dependencies = tuple(
                tail_id
                for dependency in node.depends_on
                for tail_id in expanded_tail_ids_by_original[dependency]
            )
            provider = self._macro_providers.get(node.capability_id)
            if provider is None:
                expanded_node = replace(node, depends_on=high_level_dependencies)
                expanded_nodes.append(expanded_node)
                expanded_tail_ids_by_original[node.node_id] = (expanded_node.node_id,)
                continue

            macro_plan = provider.build_plan(
                OrchestrationRequest(
                    task_id=f"{plan.task_id}:{node.node_id}",
                    conversation_id=request.conversation_id,
                    root_message_id=request.root_message_id,
                    user_message=self._resolve_macro_user_message(node, request),
                    requested_capability_id=node.capability_id,
                    metadata=dict(request.metadata),
                )
            )
            max_replans = max(max_replans, macro_plan.max_replans)
            max_dynamic_nodes = max(max_dynamic_nodes, macro_plan.max_dynamic_nodes)
            macro_nodes = tuple(macro_plan.nodes)
            if not macro_nodes:
                raise WorkflowExpansionError(f"Macro capability produced no nodes: {node.capability_id}")

            macro_node_ids = {macro_node.node_id for macro_node in macro_nodes}
            macro_dependency_ids = {
                dependency
                for macro_node in macro_nodes
                for dependency in macro_node.depends_on
            }
            macro_roots = {macro_node.node_id for macro_node in macro_nodes if not macro_node.depends_on}
            macro_tails = tuple(
                macro_node.node_id
                for macro_node in macro_nodes
                if macro_node.node_id not in macro_dependency_ids
            )
            if not macro_roots or not macro_tails:
                raise WorkflowExpansionError(f"Macro capability did not produce roots and tails: {node.capability_id}")

            for macro_node in macro_nodes:
                for dependency in macro_node.depends_on:
                    if dependency not in macro_node_ids:
                        raise WorkflowExpansionError(
                            f"Macro node {macro_node.node_id} has dependency outside macro plan: {dependency}"
                        )
                depends_on = macro_node.depends_on
                if macro_node.node_id in macro_roots:
                    depends_on = high_level_dependencies + depends_on
                expanded_nodes.append(
                    replace(
                        macro_node,
                        depends_on=depends_on,
                    )
                )
            expanded_tail_ids_by_original[node.node_id] = macro_tails
            expanded_macro_nodes[node.node_id] = {
                "capability_id": node.capability_id,
                "root_node_ids": tuple(sorted(macro_roots)),
                "tail_node_ids": macro_tails,
            }

        return WorkflowPlan(
            task_id=plan.task_id,
            nodes=tuple(expanded_nodes),
            metadata={
                **dict(plan.metadata),
                "expanded": True,
                "macro_capabilities": tuple(sorted(self._macro_providers)),
                "expanded_macro_nodes": expanded_macro_nodes,
            },
            max_replans=max_replans,
            max_dynamic_nodes=max_dynamic_nodes,
        )

    @staticmethod
    def _resolve_macro_user_message(node: WorkflowNodePlan, request: OrchestrationRequest) -> str:
        for key in ("user_question", "user_message", "content"):
            value = node.input_payload.get(key)
            if isinstance(value, str) and value:
                return value
        return request.user_message

    @staticmethod
    def _topological_nodes(plan: WorkflowPlan) -> tuple[WorkflowNodePlan, ...]:
        nodes_by_id = {node.node_id: node for node in plan.nodes}
        if len(nodes_by_id) != len(plan.nodes):
            raise WorkflowExpansionError("Workflow plan contains duplicate node ids.")

        outgoing: dict[str, list[str]] = defaultdict(list)
        indegree: dict[str, int] = {node.node_id: 0 for node in plan.nodes}
        for node in plan.nodes:
            for dependency in node.depends_on:
                if dependency not in nodes_by_id:
                    raise WorkflowExpansionError(f"Node {node.node_id} has unknown dependency: {dependency}")
                outgoing[dependency].append(node.node_id)
                indegree[node.node_id] += 1

        queue = deque(node_id for node_id, count in indegree.items() if count == 0)
        ordered: list[WorkflowNodePlan] = []
        while queue:
            current = queue.popleft()
            ordered.append(nodes_by_id[current])
            for downstream in outgoing[current]:
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    queue.append(downstream)

        if len(ordered) != len(plan.nodes):
            raise WorkflowExpansionError("Workflow plan contains a dependency cycle.")
        return tuple(ordered)
