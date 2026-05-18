from __future__ import annotations

from collections.abc import Callable, Mapping

from src.core.enums import NodeCriticality
from src.integrations.codex_skills import SkillManifest, resolve_skill_execution_config

from .answer_roles import (
    ANSWER_SCOPE_METADATA_KEY,
    AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY,
    RESPONSE_ROLE_INTERMEDIATE,
    RESPONSE_ROLE_METADATA_KEY,
)
from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


class SkillWorkflowProvider:
    """Expand a public skill.* macro into a forced main-agent Skill execution."""

    def __init__(
        self,
        skill_name_by_capability_id: Mapping[str, str] | None = None,
        *,
        skill_name_resolver: Callable[[str, str | None], str | None] | None = None,
        skill_manifest_resolver: Callable[[str, str | None], SkillManifest | None] | None = None,
    ) -> None:
        self._skill_name_by_capability_id = dict(skill_name_by_capability_id or {})
        self._skill_name_resolver = skill_name_resolver
        self._skill_manifest_resolver = skill_manifest_resolver

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        capability_id = request.requested_capability_id or ""
        revision = self._skill_bundle_revision(request)
        manifest = self._resolve_skill_manifest(capability_id, revision)
        skill_name = manifest.name if manifest is not None else self._resolve_skill_name(capability_id, revision)
        if not skill_name:
            raise ValueError(f"Unknown skill capability: {capability_id}")
        if manifest is not None:
            execution = resolve_skill_execution_config(manifest)
            if execution.mode != "delegated_main_agent":
                return self._build_executor_plan(
                    request,
                    capability_id=capability_id,
                    skill_name=skill_name,
                    answer_mode=execution.answer_mode,
                    execution_mode=execution.mode,
                    revision=revision,
                )
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

    def _build_executor_plan(
        self,
        request: OrchestrationRequest,
        *,
        capability_id: str,
        skill_name: str,
        answer_mode: str,
        execution_mode: str,
        revision: str | None,
    ) -> WorkflowPlan:
        task_id = request.task_id
        skill_node_id = f"{task_id}:skill_execute"
        skill_node = WorkflowNodePlan(
            node_id=skill_node_id,
            capability_id=capability_id,
            input_payload=self._skill_executor_payload(request),
            metadata={
                "skill_name": skill_name,
                "skill_bundle_revision": revision,
                "skill_execution_mode": execution_mode,
                "skill_answer_mode": answer_mode,
            },
            criticality=NodeCriticality.REQUIRED,
            retry_policy={"max_attempts": 1},
            timeout_policy={"seconds": 120},
        )
        nodes = [skill_node]
        finalizer_added = False
        if answer_mode == "requires_finalizer":
            finalizer_added = True
            nodes.append(
                WorkflowNodePlan(
                    node_id=f"{task_id}:main_agent.respond",
                    capability_id="main_agent.respond",
                    input_payload={"user_message": request.effective_user_message},
                    metadata={
                        RESPONSE_ROLE_METADATA_KEY: RESPONSE_ROLE_INTERMEDIATE,
                        ANSWER_SCOPE_METADATA_KEY: f"skill:{capability_id}",
                        "source_skill_node_id": skill_node_id,
                        AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY: False,
                    },
                    depends_on=(skill_node_id,),
                    criticality=NodeCriticality.REQUIRED,
                    retry_policy={"max_attempts": 1},
                    timeout_policy={"seconds": 60},
                )
            )
        return WorkflowPlan(
            task_id=task_id,
            nodes=tuple(nodes),
            metadata={
                "route": "skill",
                "public_capability_id": capability_id,
                "skill_name": skill_name,
                "skill_bundle_revision": revision,
                "skill_execution_mode": execution_mode,
                "skill_answer_mode": answer_mode,
                "skill_finalizer_added": finalizer_added,
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

    def _resolve_skill_manifest(self, capability_id: str, revision: str | None) -> SkillManifest | None:
        if self._skill_manifest_resolver is None:
            return None
        return self._skill_manifest_resolver(capability_id, revision)

    @staticmethod
    def _skill_bundle_revision(request: OrchestrationRequest) -> str | None:
        value = request.metadata.get("skill_bundle_revision")
        return str(value).strip() if value else None

    @staticmethod
    def _skill_executor_payload(request: OrchestrationRequest) -> dict[str, str]:
        payload = {"user_message": request.effective_user_message}
        macro_input_payload = request.metadata.get("macro_input_payload")
        if isinstance(macro_input_payload, dict):
            for key in ("subtask_label", "parent_question"):
                value = macro_input_payload.get(key)
                if isinstance(value, str) and value.strip():
                    payload[key] = value.strip()
        return payload
