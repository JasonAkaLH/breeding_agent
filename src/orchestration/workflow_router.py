from __future__ import annotations

from src.orchestration.models import OrchestrationRequest, WorkflowPlan


class WorkflowRouter:
    def __init__(self, *, default_provider, main_agent_provider, sql_query_provider) -> None:
        self._default_provider = default_provider
        self._main_agent_provider = main_agent_provider
        self._sql_query_provider = sql_query_provider

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        capability_id = request.requested_capability_id
        if capability_id is None:
            return self._default_provider.build_plan(request)
        if capability_id in {"sql_query", "sql_query.query"}:
            return self._sql_query_provider.build_plan(request)
        if capability_id == "main_agent" or capability_id.startswith("main_agent."):
            return self._main_agent_provider.build_plan(request)
        return self._default_provider.build_plan(request)
