from multi_agent_framework.core.agent import Agent
from multi_agent_framework.core.contracts import AgentRequest, AgentResponse, Message


class EchoAgent(Agent):
    name = "echo"
    description = "Echo back the latest user message for smoke testing."

    async def run(self, request: AgentRequest) -> AgentResponse:
        latest_user_message = next(
            (message for message in reversed(request.messages) if message.role == "user"),
            request.messages[-1],
        )
        return AgentResponse(
            agent_name=self.name,
            message=Message(
                role="assistant",
                content=f"echo: {latest_user_message.content}",
                metadata={"source_agent": self.name},
            ),
            trace=[f"agent:{self.name}", "action:echo-latest-user-message"],
        )
