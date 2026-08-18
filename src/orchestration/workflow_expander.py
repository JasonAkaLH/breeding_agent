from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import replace
from typing import Mapping, Protocol

from src.core.enums import NodeCriticality

from .answer_roles import (
    ANSWER_SCOPE_METADATA_KEY,
    AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY,
    RESPONSE_ROLE_FINAL,
    RESPONSE_ROLE_INTERMEDIATE,
    RESPONSE_ROLE_METADATA_KEY,
    response_role_from_metadata,
)
from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


class WorkflowProvider(Protocol):
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        ...


class WorkflowExpansionError(ValueError):
    """Raised when a high-level workflow cannot be expanded safely."""


class WorkflowExpander:
    def __init__(
        self,
        macro_providers: Mapping[str, WorkflowProvider],
        *,
        macro_provider_resolver: Callable[[str], WorkflowProvider | None] | None = None,
    ) -> None:
        self._macro_providers = dict(macro_providers)
        self._macro_provider_resolver = macro_provider_resolver

    def expand(
        self,
        plan: WorkflowPlan,
        *,
        request: OrchestrationRequest,
        preserved_node_ids: frozenset[str] = frozenset(),
    ) -> WorkflowPlan:
        ordered_nodes = self._topological_nodes(plan)
        nodes_by_original_id = {node.node_id: node for node in ordered_nodes}
        dropped_public_skill_dependencies: dict[str, tuple[str, ...]] = {}
        expanded_nodes: list[WorkflowNodePlan] = []
        expanded_tail_ids_by_original: dict[str, tuple[str, ...]] = {}
        expanded_answer_ids_by_original: dict[str, tuple[str, ...]] = {}
        answer_source_original_ids: list[str] = []
        required_finalizer_original_ids: list[str] = []
        expanded_macro_nodes: dict[str, dict[str, object]] = {}
        expanded_main_agent_node_ids: set[str] = set()
        last_main_agent_node_id: str | None = None
        max_replans = plan.max_replans
        max_dynamic_nodes = plan.max_dynamic_nodes

        def answer_ids_for(dependency_id: str) -> tuple[str, ...]:
            return expanded_answer_ids_by_original.get(dependency_id) or expanded_tail_ids_by_original[dependency_id]

        for node in ordered_nodes:
            preserved_dependency_ids, dropped_dependency_ids = self._preserved_high_level_dependency_ids(
                node,
                nodes_by_original_id,
            )
            if dropped_dependency_ids:
                dropped_public_skill_dependencies[node.node_id] = dropped_dependency_ids
            high_level_dependencies = tuple(
                tail_id
                for dependency in preserved_dependency_ids
                for tail_id in expanded_tail_ids_by_original[dependency]
            )
            high_level_answer_dependencies = tuple(
                self._dedupe(
                    answer_id
                    for dependency in preserved_dependency_ids
                    for answer_id in answer_ids_for(dependency)
                )
            )
            high_level_answer_source_count = sum(
                1 for dependency in preserved_dependency_ids if dependency in expanded_answer_ids_by_original
            )
            if node.node_id in preserved_node_ids:
                expanded_nodes.append(node)
                expanded_tail_ids_by_original[node.node_id] = (node.node_id,)
                if node.capability_id == "main_agent.respond":
                    expanded_main_agent_node_ids.add(node.node_id)
                    last_main_agent_node_id = node.node_id
                if response_role_from_metadata(node.metadata) == RESPONSE_ROLE_FINAL:
                    expanded_answer_ids_by_original[node.node_id] = (node.node_id,)
                continue

            provider = self._resolve_macro_provider(node.capability_id)
            if provider is None:
                if node.capability_id == "main_agent.respond" and not high_level_dependencies and last_main_agent_node_id:
                    expanded_tail_ids_by_original[node.node_id] = (last_main_agent_node_id,)
                    continue
                if (
                    node.capability_id == "main_agent.respond"
                    and high_level_dependencies
                    and all(dependency in expanded_main_agent_node_ids for dependency in high_level_dependencies)
                    and len(high_level_dependencies) <= 1
                ):
                    expanded_tail_ids_by_original[node.node_id] = high_level_dependencies
                    expanded_answer_ids_by_original[node.node_id] = high_level_answer_dependencies
                    continue
                metadata = dict(node.metadata)
                depends_on = high_level_dependencies
                if node.capability_id == "main_agent.respond" and high_level_answer_dependencies:
                    depends_on = high_level_answer_dependencies
                    metadata.setdefault(AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY, False)
                    if high_level_answer_source_count >= 2:
                        metadata.setdefault(RESPONSE_ROLE_METADATA_KEY, RESPONSE_ROLE_FINAL)
                        metadata.setdefault(ANSWER_SCOPE_METADATA_KEY, "task")
                    else:
                        metadata.setdefault(RESPONSE_ROLE_METADATA_KEY, RESPONSE_ROLE_INTERMEDIATE)
                        metadata.setdefault(ANSWER_SCOPE_METADATA_KEY, "partial")
                expanded_node = replace(node, depends_on=depends_on, metadata=metadata)
                expanded_nodes.append(expanded_node)
                expanded_tail_ids_by_original[node.node_id] = (expanded_node.node_id,)
                if expanded_node.capability_id == "main_agent.respond":
                    expanded_main_agent_node_ids.add(expanded_node.node_id)
                    last_main_agent_node_id = expanded_node.node_id
                if response_role_from_metadata(expanded_node.metadata) == RESPONSE_ROLE_FINAL:
                    expanded_answer_ids_by_original[node.node_id] = (expanded_node.node_id,)
                continue

            macro_plan = provider.build_plan(
                OrchestrationRequest(
                    task_id=f"{plan.task_id}:{node.node_id}",
                    conversation_id=request.conversation_id,
                    root_message_id=request.root_message_id,
                    user_message=self._resolve_macro_user_message(node, request),
                    requested_capability_id=node.capability_id,
                    metadata={
                        **dict(request.metadata),
                        "macro_input_payload": dict(node.input_payload),
                        "macro_expansion": True,
                        "macro_source": str(plan.metadata.get("source") or plan.metadata.get("route") or ""),
                    },
                    current_user_message=request.current_user_message,
                    resolved_user_message=self._resolve_macro_user_message(node, request),
                    memory_context=request.memory_context,
                )
            )
            max_replans = max(max_replans, macro_plan.max_replans)
            max_dynamic_nodes = max(max_dynamic_nodes, macro_plan.max_dynamic_nodes)
            macro_nodes = tuple(macro_plan.nodes)
            if not macro_nodes:
                raise WorkflowExpansionError(f"Macro capability produced no nodes: {node.capability_id}")
            macro_requires_finalizer = self._macro_requires_finalizer(macro_plan, macro_nodes)

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
                metadata = dict(macro_node.metadata)
                if macro_node.capability_id == "main_agent.respond" and response_role_from_metadata(metadata) is None:
                    metadata.update(
                        {
                            RESPONSE_ROLE_METADATA_KEY: RESPONSE_ROLE_INTERMEDIATE,
                            ANSWER_SCOPE_METADATA_KEY: f"skill:{node.capability_id}",
                            AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY: False,
                        }
                    )
                expanded_node = replace(
                    macro_node,
                    depends_on=depends_on,
                    metadata=metadata,
                )
                expanded_nodes.append(expanded_node)
                if expanded_node.capability_id == "main_agent.respond":
                    expanded_main_agent_node_ids.add(expanded_node.node_id)
                    last_main_agent_node_id = expanded_node.node_id
            expanded_tail_ids_by_original[node.node_id] = macro_tails
            macro_tail_ids = set(macro_tails)
            macro_answer_ids = tuple(
                self._dedupe(
                    macro_node.node_id
                    for macro_node in macro_nodes
                    if macro_node.capability_id != "main_agent.respond" or macro_node.node_id in macro_tail_ids
                )
            )
            expanded_answer_ids_by_original[node.node_id] = macro_answer_ids
            if macro_answer_ids:
                answer_source_original_ids.append(node.node_id)
                if macro_requires_finalizer:
                    required_finalizer_original_ids.append(node.node_id)
            expanded_macro_nodes[node.node_id] = {
                "capability_id": node.capability_id,
                "root_node_ids": tuple(sorted(macro_roots)),
                "tail_node_ids": macro_tails,
                "answer_node_ids": macro_answer_ids,
                "requires_finalizer": macro_requires_finalizer,
            }

        finalizer_original_ids = (
            answer_source_original_ids
            if len(answer_source_original_ids) >= 2
            else required_finalizer_original_ids
        )
        required_finalizer_dependencies = tuple(
            self._dedupe(
                answer_id
                for original_id in finalizer_original_ids
                for answer_id in expanded_answer_ids_by_original[original_id]
            )
        )
        global_finalizer_added = False
        if required_finalizer_dependencies:
            covering_answer_index = self._find_answer_node_covering(
                expanded_nodes,
                required_finalizer_dependencies,
            )
            if covering_answer_index is None:
                global_finalizer_added = True
                expanded_nodes.append(
                    WorkflowNodePlan(
                        node_id=self._unique_node_id(plan.task_id, {node.node_id for node in expanded_nodes}),
                        capability_id="main_agent.respond",
                        input_payload={"user_message": request.effective_user_message},
                        metadata={
                            RESPONSE_ROLE_METADATA_KEY: RESPONSE_ROLE_FINAL,
                            ANSWER_SCOPE_METADATA_KEY: "task",
                            AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY: False,
                            "finalizer_source": "workflow_expander",
                        },
                        depends_on=required_finalizer_dependencies,
                        criticality=NodeCriticality.REQUIRED,
                        retry_policy={"max_attempts": 1},
                        timeout_policy={"seconds": 60},
                    )
                )
            else:
                expanded_nodes[covering_answer_index] = self._as_final_answer_node(
                    expanded_nodes[covering_answer_index]
                )

        return WorkflowPlan(
            task_id=plan.task_id,
            nodes=tuple(expanded_nodes),
            metadata={
                **dict(plan.metadata),
                "expanded": True,
                "macro_capabilities": tuple(sorted(self._macro_providers)),
                "expanded_macro_nodes": expanded_macro_nodes,
                "global_finalizer_added": global_finalizer_added,
                "dropped_public_skill_dependencies": dropped_public_skill_dependencies,
            },
            max_replans=max_replans,
            max_dynamic_nodes=max_dynamic_nodes,
        )

    @classmethod
    def _preserved_high_level_dependency_ids(
        cls,
        node: WorkflowNodePlan,
        nodes_by_original_id: Mapping[str, WorkflowNodePlan],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not cls._is_public_skill_capability(node.capability_id) or cls._requires_public_skill_dependency(node):
            return node.depends_on, ()

        preserved: list[str] = []
        dropped: list[str] = []
        for dependency_id in node.depends_on:
            dependency = nodes_by_original_id.get(dependency_id)
            if dependency is not None and cls._is_public_skill_capability(dependency.capability_id):
                dropped.append(dependency_id)
                continue
            preserved.append(dependency_id)
        return tuple(preserved), tuple(dropped)

    @staticmethod
    def _is_public_skill_capability(capability_id: str) -> bool:
        return capability_id.startswith("skill.")

    @staticmethod
    def _requires_public_skill_dependency(node: WorkflowNodePlan) -> bool:
        for container in (node.input_payload, node.metadata):
            value = container.get("requires_public_skill_dependency")
            if value is True:
                return True
            value = container.get("requires_skill_dependency")
            if value is True:
                return True
        return False

    def _resolve_macro_provider(self, capability_id: str) -> WorkflowProvider | None:
        provider = self._macro_providers.get(capability_id)
        if provider is not None:
            return provider
        if self._macro_provider_resolver is None:
            return None
        return self._macro_provider_resolver(capability_id)

    @staticmethod
    def _resolve_macro_user_message(node: WorkflowNodePlan, request: OrchestrationRequest) -> str:
        for key in ("user_question", "user_message", "content"):
            value = node.input_payload.get(key)
            if isinstance(value, str) and value:
                return value
        return request.user_message

    @staticmethod
    def _dedupe(values) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return tuple(ordered)

    @staticmethod
    def _macro_requires_finalizer(
        macro_plan: WorkflowPlan,
        macro_nodes: tuple[WorkflowNodePlan, ...],
    ) -> bool:
        if str(macro_plan.metadata.get("skill_answer_mode") or "").strip().lower() == "requires_finalizer":
            return True
        if macro_plan.metadata.get("skill_requires_finalizer") is True:
            return True
        return any(
            str(macro_node.metadata.get("skill_answer_mode") or "").strip().lower() == "requires_finalizer"
            or macro_node.metadata.get("skill_requires_finalizer") is True
            for macro_node in macro_nodes
        )

    @staticmethod
    def _find_answer_node_covering(
        nodes: list[WorkflowNodePlan],
        required_dependencies: tuple[str, ...],
    ) -> int | None:
        required = set(required_dependencies)
        for index, node in enumerate(nodes):
            if node.capability_id == "main_agent.respond" and required.issubset(set(node.depends_on)):
                return index
        return None

    @staticmethod
    def _as_final_answer_node(node: WorkflowNodePlan) -> WorkflowNodePlan:
        metadata = dict(node.metadata)
        metadata[RESPONSE_ROLE_METADATA_KEY] = RESPONSE_ROLE_FINAL
        metadata[ANSWER_SCOPE_METADATA_KEY] = "task"
        metadata.setdefault(AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY, False)
        return replace(node, metadata=metadata)

    @staticmethod
    def _unique_node_id(task_id: str, existing_node_ids: set[str]) -> str:
        base = f"{task_id}:global_final_answer"
        if base not in existing_node_ids:
            return base
        index = 2
        while f"{base}_{index}" in existing_node_ids:
            index += 1
        return f"{base}_{index}"

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
