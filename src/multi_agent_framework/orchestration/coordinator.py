from multi_agent_framework.core.contracts import AgentRequest, AgentResponse
from multi_agent_framework.core.registry import AgentRegistry


class Coordinator:
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    async def execute(self, request: AgentRequest) -> AgentResponse:
        agent = self._registry.get(request.agent_name)
        return await agent.run(request)
