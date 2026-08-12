from __future__ import annotations

from collections.abc import Awaitable

from src.orchestration.models import OrchestrationRequest, WorkflowPlan


class WorkflowRouter:
    def __init__(self, *, default_provider, main_agent_provider, skill_provider=None, mcp_provider=None) -> None:
        self._default_provider = default_provider
        self._main_agent_provider = main_agent_provider
        self._skill_provider = skill_provider
        self._mcp_provider = mcp_provider

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan | Awaitable[WorkflowPlan]:
        capability_id = request.requested_capability_id
        if capability_id is None:
            return self._default_provider.build_plan(request)
        if capability_id == "main_agent" or capability_id.startswith("main_agent."):
            return self._main_agent_provider.build_plan(request)
        if capability_id.startswith("skill.") and self._skill_provider is not None:
            return self._skill_provider.build_plan(request)
        if capability_id == "mcp.dispatch" and self._mcp_provider is not None:
            return self._mcp_provider.build_plan(request)
        return self._default_provider.build_plan(request)
