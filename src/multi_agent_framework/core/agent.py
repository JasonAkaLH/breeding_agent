from typing import Protocol

from multi_agent_framework.core.contracts import AgentRequest, AgentResponse


class Agent(Protocol):
    name: str
    description: str

    async def run(self, request: AgentRequest) -> AgentResponse:
        ...
