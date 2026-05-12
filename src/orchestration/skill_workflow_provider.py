from __future__ import annotations

from collections.abc import Callable, Mapping

from src.core.enums import NodeCriticality

from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


class SkillWorkflowProvider:
    """Expand a public skill.* macro into a forced main-agent Skill execution."""

    def __init__(
        self,
        skill_name_by_capability_id: Mapping[str, str] | None = None,
        *,
        skill_name_resolver: Callable[[str, str | None], str | None] | None = None,
    ) -> None:
        self._skill_name_by_capability_id = dict(skill_name_by_capability_id or {})
        self._skill_name_resolver = skill_name_resolver

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        capability_id = request.requested_capability_id or ""
        revision = self._skill_bundle_revision(request)
        skill_name = self._resolve_skill_name(capability_id, revision)
        if not skill_name:
            raise ValueError(f"Unknown skill capability: {capability_id}")
        node_id = f"{request.task_id}:main_agent.respond"
        forced_source = self._forced_skill_source(request)
        return WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id=node_id,
                    capability_id="main_agent.respond",
                    input_payload={"user_message": request.effective_user_message},
                    metadata={
                        "forced_skill_capability_id": capability_id,
                        "forced_skill_name": skill_name,
                        "forced_skill_source": forced_source,
                        "skill_bundle_revision": revision,
                    },
                    criticality=NodeCriticality.REQUIRED,
                    retry_policy={"max_attempts": 1},
                    timeout_policy={"seconds": 60},
                ),
            ),
            metadata={
                "route": "skill",
                "public_capability_id": capability_id,
                "forced_skill_name": skill_name,
                "forced_skill_source": forced_source,
                "skill_bundle_revision": revision,
            },
            max_replans=0,
            max_dynamic_nodes=0,
        )

    @staticmethod
    def _forced_skill_source(request: OrchestrationRequest) -> str:
        macro_source = str(request.metadata.get("macro_source") or "")
        if macro_source == "llm_planner_output":
            return "planner"
        if macro_source == "main_agent_runtime_replan_output":
            return "replanner"
        if macro_source:
            return macro_source
        return "explicit_request"

    def _resolve_skill_name(self, capability_id: str, revision: str | None) -> str | None:
        if self._skill_name_resolver is not None:
            return self._skill_name_resolver(capability_id, revision)
        return self._skill_name_by_capability_id.get(capability_id)

    @staticmethod
    def _skill_bundle_revision(request: OrchestrationRequest) -> str | None:
        value = request.metadata.get("skill_bundle_revision")
        return str(value).strip() if value else None
