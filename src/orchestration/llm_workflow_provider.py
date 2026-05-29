from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Protocol

from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from .planner_contract import (
    PlannerOutputError,
    TextGenerator,
    build_planner_profile_resolution,
    build_planner_repair_profile_resolution,
    call_text_generator,
    parse_planner_output,
)
from .planner_payload_policy import CapabilityPayloadPolicy, PlannerPayloadPolicy
from .registry import CapabilityRegistry
from .workflow_expander import WorkflowExpander, WorkflowExpansionError
from .workflow_plan_validator import WorkflowPlanValidationError, WorkflowPlanValidator


class WorkflowProvider(Protocol):
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan | Awaitable[WorkflowPlan]:
        ...


class WorkflowPlanningError(RuntimeError):
    """Raised when LLM-only workflow planning cannot produce a valid plan."""

    def __init__(self, *, reason: str, diagnostic: str, attempts: int) -> None:
        self.reason = reason
        self.diagnostic = diagnostic
        self.attempts = attempts
        super().__init__(f"LLM workflow planning failed after {attempts} attempt(s): {reason}: {diagnostic}")


class LLMWorkflowProvider:
    """Build an LLM-generated public DAG and fail closed on invalid planner output.

    The LLM is only trusted to choose public capabilities and high-level
    dependencies.  Every candidate plan is validated before expansion,
    then expanded through system-owned providers such as SkillWorkflowProvider.
    Invalid planner output is repaired by the LLM itself once, then
    fails without deterministic capability routing.  The fallback provider is
    reserved for explicitly disabled planner configurations.
    """

    def __init__(
        self,
        *,
        capability_registry: CapabilityRegistry,
        fallback_provider: WorkflowProvider,
        macro_providers: Mapping[str, WorkflowProvider],
        macro_provider_resolver: Callable[[str], WorkflowProvider | None] | None = None,
        text_generator: TextGenerator | None = None,
        payload_policies: Mapping[str, CapabilityPayloadPolicy] | None = None,
        max_repair_attempts: int = 1,
    ) -> None:
        self._capability_registry = capability_registry
        self._fallback_provider = fallback_provider
        self._macro_providers = dict(macro_providers)
        self._text_generator = text_generator
        self._expander = WorkflowExpander(
            self._macro_providers,
            macro_provider_resolver=macro_provider_resolver,
        )
        self._public_validator = WorkflowPlanValidator(capability_registry, public_only=True)
        self._internal_validator = WorkflowPlanValidator(capability_registry, public_only=False)
        self._payload_policy_overrides = dict(payload_policies or {})
        self._max_repair_attempts = max(0, max_repair_attempts)

    async def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        if self._text_generator is None:
            return self._planner_disabled_plan(request)

        payload_policies = self._resolve_payload_policies()
        planner_payload_allowlist = {
            capability_id: policy.planner_allowed_fields
            for capability_id, policy in payload_policies.items()
        }
        original_prompt_resolution = build_planner_profile_resolution(
            request,
            public_capabilities=self._capability_registry.list(public_only=True),
            planner_payload_allowlist=planner_payload_allowlist,
        )
        original_prompt = original_prompt_resolution.prompt
        prompt = original_prompt
        prompt_profile = original_prompt_resolution.llm_call_payload
        previous_output = ""
        attempts = 0
        while attempts <= self._max_repair_attempts:
            attempts += 1
            try:
                raw_output = call_text_generator(
                    self._text_generator,
                    prompt,
                    request=request,
                    prompt_profile=prompt_profile,
                )
                if inspect.isawaitable(raw_output):
                    raw_output = await raw_output
                if not isinstance(raw_output, str):
                    raise PlannerOutputError("Planner text generator must return a string.")
                previous_output = raw_output
                public_plan = parse_planner_output(raw_output, task_id=request.task_id)
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
                        "planner_attempt_count": attempts,
                        "planner_repair_attempts": attempts - 1,
                        "public_node_count": len(public_plan.nodes),
                    },
                    max_replans=expanded.max_replans,
                    max_dynamic_nodes=expanded.max_dynamic_nodes,
                )
            except (PlannerOutputError, WorkflowPlanValidationError, WorkflowExpansionError) as exc:
                if attempts <= self._max_repair_attempts:
                    repair_resolution = build_planner_repair_profile_resolution(
                        original_prompt,
                        previous_output=previous_output,
                        error_reason=type(exc).__name__,
                        diagnostic=str(exc),
                        trim_max_tokens=(request.metadata or {}).get("planner_trim_max_tokens")
                        or (request.metadata or {}).get("trim_max_tokens"),
                    )
                    prompt = repair_resolution.prompt
                    prompt_profile = repair_resolution.llm_call_payload
                    continue
                raise WorkflowPlanningError(
                    reason=type(exc).__name__,
                    diagnostic=str(exc),
                    attempts=attempts,
                ) from exc
            except Exception as exc:  # Provider/network failures must not be converted into deterministic routing.
                raise WorkflowPlanningError(
                    reason=type(exc).__name__,
                    diagnostic="planner_provider_failed",
                    attempts=attempts,
                ) from exc

        raise WorkflowPlanningError(
            reason="PlannerOutputError",
            diagnostic="planner_repair_attempts_exhausted",
            attempts=attempts,
        )

    def _planner_disabled_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        fallback = self._fallback_provider.build_plan(request)
        return WorkflowPlan(
            task_id=fallback.task_id,
            nodes=fallback.nodes,
            metadata={
                **dict(fallback.metadata),
                "planner_source": "disabled",
                "planner_fallback_used": True,
                "planner_fallback_reason": "planner_disabled",
                "planner_diagnostic": "No planner text generator configured.",
            },
            max_replans=fallback.max_replans,
            max_dynamic_nodes=fallback.max_dynamic_nodes,
        )

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
        non_answering_tail_ids = tuple(node.node_id for node in tail_nodes if not self._is_answer_producing(node.capability_id))
        tail_main_nodes = tuple(node for node in tail_nodes if node.capability_id == "main_agent.respond")

        if tail_main_nodes:
            if not non_answering_tail_ids:
                return nodes, False, False
            target = tail_main_nodes[-1]
            existing_dependencies = tuple(target.depends_on)
            missing_dependencies = tuple(node_id for node_id in non_answering_tail_ids if node_id not in existing_dependencies)
            if not missing_dependencies:
                return nodes, False, False
            rewired = replace(target, depends_on=existing_dependencies + missing_dependencies)
            return tuple(rewired if node.node_id == target.node_id else node for node in nodes), False, True

        if not non_answering_tail_ids:
            return nodes, False, False

        final_node_id = self._unique_node_id("answer_user", node_ids)
        final_node = payload_policy.apply(
            WorkflowNodePlan(
                node_id=final_node_id,
                capability_id="main_agent.respond",
                depends_on=non_answering_tail_ids,
            ),
            request=request,
        )
        return (*nodes, final_node), True, False

    @staticmethod
    def _is_answer_producing(capability_id: str) -> bool:
        return capability_id == "main_agent.respond" or capability_id.startswith("skill.")

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
