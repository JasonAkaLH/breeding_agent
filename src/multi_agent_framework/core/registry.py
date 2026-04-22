from multi_agent_framework.core.agent import Agent
from multi_agent_framework.core.contracts import AgentDescriptor


class AgentNotFoundError(LookupError):
    pass


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        if agent.name in self._agents:
            msg = f"Agent already registered: {agent.name}"
            raise ValueError(msg)
        self._agents[agent.name] = agent

    def get(self, name: str) -> Agent:
        try:
            return self._agents[name]
        except KeyError as exc:
            msg = f"Agent not found: {name}"
            raise AgentNotFoundError(msg) from exc

    def list_agents(self) -> list[AgentDescriptor]:
        return [
            AgentDescriptor(name=agent.name, description=agent.description)
            for agent in self._agents.values()
        ]
