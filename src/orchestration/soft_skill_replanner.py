from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.core.enums import NodeCriticality, NodeStatus

from .answer_roles import AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY
from .capability_fallback import CAPABILITY_MISSING_FALLBACK_KEY, build_capability_missing_fallback_metadata
from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from .runtime_replanner import RuntimeReplanContext, RuntimeReplanDecision
from .workflow_expander import WorkflowExpander, WorkflowExpansionError
from .workflow_plan_validator import WorkflowPlanValidationError, WorkflowPlanValidator
from .registry import CapabilityRegistry


class SoftSkillBindingReplanner:
    """Deterministic replan path for slash Skill soft binding execute signals."""

    def __init__(
        self,
        *,
        capability_registry: CapabilityRegistry,
        macro_providers: Mapping[str, Any],
        macro_provider_resolver: Callable[[str], Any | None] | None = None,
        active_skill_revision_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._capability_registry = capability_registry
        self._expander = WorkflowExpander(dict(macro_providers), macro_provider_resolver=macro_provider_resolver)
        self._validator = WorkflowPlanValidator(capability_registry, public_only=False)
        self._active_skill_revision_resolver = active_skill_revision_resolver

    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.unresolved_interrupt:
            return None
        if context.replan_count >= context.plan.max_replans:
            return None
        signal = self._find_execute_signal(context)
        if signal is None:
            return None
        source_node_id, target_capability_id = signal
        binding = context.request.metadata.get("soft_skill_binding")
        if not isinstance(binding, Mapping):
            return None
        bound_capability_id = str(binding.get("capability_id") or "").strip()
        if target_capability_id != bound_capability_id:
            return None
        expected_revision = str(binding.get("skill_bundle_revision") or context.request.metadata.get("skill_bundle_revision") or "").strip()
        if self._active_skill_revision_resolver is not None:
            active_revision = self._active_skill_revision_resolver(target_capability_id)
            if active_revision and expected_revision and active_revision != expected_revision:
                return None
        descriptor = self._capability_registry.get(target_capability_id)
        if descriptor is None or not descriptor.public or not target_capability_id.startswith("skill."):
            return self._missing_capability_fallback_decision(
                context,
                source_node_id=source_node_id,
                target_capability_id=target_capability_id,
            )

        public_plan = self._public_execute_plan(
            context,
            source_node_id=source_node_id,
            target_capability_id=target_capability_id,
        )
        try:
            expanded = self._expander.expand(public_plan, request=context.request)
            self._validator.validate(expanded)
        except (WorkflowExpansionError, WorkflowPlanValidationError, ValueError):
            return None
        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=expanded.task_id,
                nodes=expanded.nodes,
                metadata={
                    **dict(expanded.metadata),
                    "runtime_replan_source": "soft_skill_binding",
                    "runtime_replan_reason": "soft_skill_execute",
                    "soft_skill_binding_capability_id": target_capability_id,
                },
                max_replans=context.plan.max_replans,
                max_dynamic_nodes=context.plan.max_dynamic_nodes,
            ),
            reason="soft_skill_execute",
            metadata={
                "replan_source": "soft_skill_binding",
                "target_capability_id": target_capability_id,
            },
        )

    @staticmethod
    def _missing_capability_fallback_decision(
        context: RuntimeReplanContext,
        *,
        source_node_id: str,
        target_capability_id: str,
    ) -> RuntimeReplanDecision | None:
        if any(CAPABILITY_MISSING_FALLBACK_KEY in node.metadata for node in context.plan.nodes):
            return None
        existing_nodes = tuple(context.plan.nodes)
        existing_ids = {node.node_id for node in existing_nodes}
        fallback_node_id = "soft_skill_missing_fallback"
        suffix = 2
        while fallback_node_id in existing_ids:
            fallback_node_id = f"soft_skill_missing_fallback_{suffix}"
            suffix += 1
        business_dependency_ids = tuple(
            node_id
            for node_id, node in context.nodes.items()
            if node.status == NodeStatus.COMPLETED
            and node.capability_id != "main_agent.respond"
            and isinstance(context.node_outputs.get(node_id), Mapping)
            and bool(context.node_outputs.get(node_id))
        )
        has_business_result = bool(business_dependency_ids)
        depends_on = tuple(dict.fromkeys((source_node_id, *business_dependency_ids)))
        fallback_metadata = build_capability_missing_fallback_metadata(
            reason_code="skill_missing",
            scope="partial" if has_business_result else "full",
            missing_capability_summary=f"软绑定请求的 Skill 当前未注册或不可用：{target_capability_id}",
            attempted_capability_summary="已先执行并保留可用业务能力结果" if has_business_result else None,
            fallback_content_scope="仅说明该 Skill 缺口并给出可手工复核的通用建议，不执行 Skill 或生成下载文件。",
        )
        fallback_node = WorkflowNodePlan(
            node_id=fallback_node_id,
            capability_id="main_agent.respond",
            input_payload={"user_message": context.request.effective_user_message},
            metadata={
                CAPABILITY_MISSING_FALLBACK_KEY: fallback_metadata,
                AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY: False,
            },
            depends_on=depends_on,
            criticality=NodeCriticality.REQUIRED,
            retry_policy={"max_attempts": 1},
            timeout_policy={"seconds": 60},
        )
        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=context.plan.task_id,
                nodes=(*existing_nodes, fallback_node),
                metadata={
                    **dict(context.plan.metadata),
                    "runtime_replan_source": "soft_skill_binding",
                    "runtime_replan_reason": "soft_skill_missing_fallback",
                    "soft_skill_binding_capability_id": target_capability_id,
                    CAPABILITY_MISSING_FALLBACK_KEY: fallback_metadata,
                },
                max_replans=context.plan.max_replans,
                max_dynamic_nodes=context.plan.max_dynamic_nodes,
            ),
            reason="soft_skill_missing_fallback",
            metadata={
                "replan_source": "soft_skill_binding",
                "target_capability_id": target_capability_id,
                CAPABILITY_MISSING_FALLBACK_KEY: fallback_metadata,
            },
        )


    @staticmethod
    def _find_execute_signal(context: RuntimeReplanContext) -> tuple[str, str] | None:
        for node_id, output in context.node_outputs.items():
            node = context.nodes.get(node_id)
            if node is not None and node.status != NodeStatus.COMPLETED:
                continue
            if not isinstance(output, Mapping):
                continue
            decision = output.get("soft_skill_decision")
            if not isinstance(decision, Mapping):
                continue
            if decision.get("decision") != "execute":
                continue
            target = str(decision.get("target_capability_id") or "").strip()
            if target.startswith("skill."):
                return node_id, target
        return None

    @staticmethod
    def _public_execute_plan(
        context: RuntimeReplanContext,
        *,
        source_node_id: str,
        target_capability_id: str,
    ) -> WorkflowPlan:
        existing_nodes = tuple(context.plan.nodes)
        existing_ids = {node.node_id for node in existing_nodes}
        macro_node_id = "soft_skill_execute"
        suffix = 2
        while macro_node_id in existing_ids:
            macro_node_id = f"soft_skill_execute_{suffix}"
            suffix += 1
        return WorkflowPlan(
            task_id=context.plan.task_id,
            nodes=(
                *existing_nodes,
                WorkflowNodePlan(
                    node_id=macro_node_id,
                    capability_id=target_capability_id,
                    input_payload={"user_message": context.request.effective_user_message},
                    metadata={
                        "macro_source": "soft_skill_binding_replanner",
                        "requires_public_skill_dependency": True,
                    },
                    depends_on=(source_node_id,),
                ),
            ),
            metadata={
                **dict(context.plan.metadata),
                "source": "soft_skill_binding_replanner",
                "soft_skill_binding_capability_id": target_capability_id,
            },
            max_replans=context.plan.max_replans,
            max_dynamic_nodes=context.plan.max_dynamic_nodes,
        )
