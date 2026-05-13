from __future__ import annotations

from collections.abc import Callable
from typing import Mapping, Protocol

from .models import OrchestrationRequest, WorkflowPlan
from .workflow_expander import WorkflowExpander


class WorkflowProvider(Protocol):
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        ...


class AutoWorkflowProvider:
    """Deterministic fallback provider with no business-specific routing.

    Capability selection belongs to the LLM planner or explicit public
    capability requests. When planner is disabled/unavailable, this provider
    falls back to the main agent only and does not hardcode any specific Skill or
    other business capability.
    """

    def __init__(
        self,
        *,
        main_agent_provider: WorkflowProvider,
        macro_providers: Mapping[str, WorkflowProvider] | None = None,
        macro_provider_resolver: Callable[[str], WorkflowProvider | None] | None = None,
    ) -> None:
        self._main_agent_provider = main_agent_provider
        self._macro_providers = dict(macro_providers or {})
        self._expander = WorkflowExpander(
            self._macro_providers,
            macro_provider_resolver=macro_provider_resolver,
        )

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        return self._main_agent_provider.build_plan(request)
