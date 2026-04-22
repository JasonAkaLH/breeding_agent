from multi_agent_framework.agents.echo import EchoAgent
from multi_agent_framework.core.registry import AgentRegistry
from multi_agent_framework.orchestration.coordinator import Coordinator


def build_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(EchoAgent())
    return registry


def build_coordinator() -> Coordinator:
    return Coordinator(registry=build_registry())
