from __future__ import annotations

from collections.abc import Mapping

from src.core.enums import NodeCriticality

from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


class SkillWorkflowProvider:
    """Expand a public skill.* macro into a forced main-agent Skill execution."""

    def __init__(self, skill_name_by_capability_id: Mapping[str, str]) -> None:
        self._skill_name_by_capability_id = dict(skill_name_by_capability_id)

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        capability_id = request.requested_capability_id or ""
        skill_name = self._skill_name_by_capability_id.get(capability_id)
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
