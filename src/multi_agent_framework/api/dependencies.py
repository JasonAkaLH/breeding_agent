from fastapi import Request

from multi_agent_framework.config import Settings
from multi_agent_framework.core.registry import AgentRegistry
from multi_agent_framework.orchestration.coordinator import Coordinator


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_registry(request: Request) -> AgentRegistry:
    return request.app.state.registry  # type: ignore[no-any-return]


def get_coordinator(request: Request) -> Coordinator:
    return request.app.state.coordinator  # type: ignore[no-any-return]
