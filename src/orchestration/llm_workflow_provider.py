from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import replace
from typing import Protocol

from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from .planner_contract import PlannerOutputError, TextGenerator, build_plan_from_llm_output
from .planner_payload_policy import CapabilityPayloadPolicy, PlannerPayloadPolicy
from .registry import CapabilityRegistry
from .workflow_expander import WorkflowExpander, WorkflowExpansionError
from .workflow_plan_validator import WorkflowPlanValidationError, WorkflowPlanValidator


class WorkflowProvider(Protocol):
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan | Awaitable[WorkflowPlan]:
        ...


class LLMWorkflowProvider:
    """Try an LLM-generated public DAG, then fall back to deterministic auto planning.

    The LLM is only trusted to choose public capabilities and high-level
    dependencies.  Every candidate plan is validated before macro expansion,
    then expanded through system-owned providers such as SQLQuery's fixed
    workflow.  Any planner problem becomes a deterministic fallback rather than
    a user-visible task failure.
    """

    def __init__(
        self,
        *,
        capability_registry: CapabilityRegistry,
        fallback_provider: WorkflowProvider,
        macro_providers: Mapping[str, WorkflowProvider],
        text_generator: TextGenerator | None = None,
        payload_policies: Mapping[str, CapabilityPayloadPolicy] | None = None,
    ) -> None:
        self._capability_registry = capability_registry
        self._fallback_provider = fallback_provider
        self._macro_providers = dict(macro_providers)
        self._text_generator = text_generator
        self._expander = WorkflowExpander(self._macro_providers)
        self._public_validator = WorkflowPlanValidator(capability_registry, public_only=True)
        self._internal_validator = WorkflowPlanValidator(capability_registry, public_only=False)
        self._payload_policy_overrides = dict(payload_policies or {})

    async def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        if self._text_generator is None:
            return self._fallback_plan(
                request,
                reason="planner_disabled",
                diagnostic="No planner text generator configured.",
            )
        try:
            payload_policies = self._resolve_payload_policies()
            public_plan = await build_plan_from_llm_output(
                request,
                text_generator=self._text_generator,
                public_capabilities=self._capability_registry.list(public_only=True),
                planner_payload_allowlist={
                    capability_id: policy.planner_allowed_fields
                    for capability_id, policy in payload_policies.items()
                },
            )
            public_plan = self._enrich_public_plan(public_plan, request=request, payload_policies=payload_policies)
            self._public_validator.validate(public_plan)
            expanded = self._expander.expand(public_plan, request=request)
            self._internal_validator.validate(expanded)
            return WorkflowPlan(
                task_id=expanded.task_id,
                nodes=expanded.nodes,
                metadata={
                    **dict(expanded.metadata),
                    "route": "llm_planner",
                    "planner_source": "llm",
                    "planner_fallback_used": False,
                    "public_node_count": len(public_plan.nodes),
                },
                max_replans=expanded.max_replans,
                max_dynamic_nodes=expanded.max_dynamic_nodes,
            )
        except (PlannerOutputError, WorkflowPlanValidationError, WorkflowExpansionError) as exc:
            return self._fallback_plan(request, reason=type(exc).__name__, diagnostic=str(exc))
        except Exception as exc:  # Provider/network failures must not fail the task at planning time.
            return self._fallback_plan(request, reason=type(exc).__name__, diagnostic="planner_provider_failed")

    def _enrich_public_plan(
        self,
        plan: WorkflowPlan,
        *,
        request: OrchestrationRequest,
        payload_policies: Mapping[str, CapabilityPayloadPolicy],
    ) -> WorkflowPlan:
        payload_policy = PlannerPayloadPolicy(payload_policies)
        nodes = tuple(payload_policy.apply(node, request=request) for node in plan.nodes)
        nodes, finalizer_added, finalizer_rewired = self._ensure_final_main_agent(
            nodes,
            request=request,
            payload_policy=payload_policy,
        )
        return WorkflowPlan(
            task_id=plan.task_id,
            nodes=nodes,
            metadata={
                **dict(plan.metadata),
                "source": "llm_planner_output",
                "planner_finalizer_added": finalizer_added,
                "planner_finalizer_rewired": finalizer_rewired,
            },
            max_replans=plan.max_replans,
            max_dynamic_nodes=plan.max_dynamic_nodes,
        )

    def _ensure_final_main_agent(
        self,
        nodes: tuple[WorkflowNodePlan, ...],
        *,
        request: OrchestrationRequest,
        payload_policy: PlannerPayloadPolicy,
    ) -> tuple[tuple[WorkflowNodePlan, ...], bool, bool]:
        if not nodes:
            return nodes, False, False

        node_ids = {node.node_id for node in nodes}
        downstream_dependencies = {dependency for node in nodes for dependency in node.depends_on}
        tail_nodes = tuple(node for node in nodes if node.node_id not in downstream_dependencies)
        tail_node_ids = tuple(node.node_id for node in tail_nodes)
        tail_main_nodes = tuple(node for node in tail_nodes if node.capability_id == "main_agent.respond")
        non_main_tail_ids = tuple(node.node_id for node in tail_nodes if node.capability_id != "main_agent.respond")

        if tail_main_nodes:
            if not non_main_tail_ids:
                return nodes, False, False
            target = tail_main_nodes[-1]
            existing_dependencies = tuple(target.depends_on)
            missing_dependencies = tuple(node_id for node_id in non_main_tail_ids if node_id not in existing_dependencies)
            if not missing_dependencies:
                return nodes, False, False
            rewired = replace(target, depends_on=existing_dependencies + missing_dependencies)
            return tuple(rewired if node.node_id == target.node_id else node for node in nodes), False, True

        final_node_id = self._unique_node_id("answer_user", node_ids)
        final_node = payload_policy.apply(
            WorkflowNodePlan(
                node_id=final_node_id,
                capability_id="main_agent.respond",
                depends_on=tail_node_ids,
            ),
            request=request,
        )
        return (*nodes, final_node), True, False

    def _resolve_payload_policies(self) -> dict[str, CapabilityPayloadPolicy]:
        payload_policies = self._capability_registry.planner_payload_policies()
        payload_policies.update(self._payload_policy_overrides)
        return payload_policies

    @staticmethod
    def _unique_node_id(preferred: str, existing: set[str]) -> str:
        if preferred not in existing:
            return preferred
        index = 2
        while f"{preferred}_{index}" in existing:
            index += 1
        return f"{preferred}_{index}"

    def _fallback_plan(self, request: OrchestrationRequest, *, reason: str, diagnostic: str | None) -> WorkflowPlan:
        fallback = self._fallback_provider.build_plan(request)
        metadata = {
            **dict(fallback.metadata),
            "planner_source": "fallback",
            "planner_fallback_used": True,
            "planner_fallback_reason": reason,
        }
        if diagnostic:
            metadata["planner_diagnostic"] = diagnostic[:200]
        return WorkflowPlan(
            task_id=fallback.task_id,
            nodes=fallback.nodes,
            metadata=metadata,
            max_replans=fallback.max_replans,
            max_dynamic_nodes=fallback.max_dynamic_nodes,
        )
