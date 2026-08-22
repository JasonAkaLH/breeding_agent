from __future__ import annotations

import hashlib

from .model_port import AgentModelPort
from .models import AgentMessage, AgentModelRequest, AgentToolChoice
from .repository import AgentRunRepository


class RunBoundMCPTextGenerator:
    """Generate Router/Selector JSON with the immutable binding stored on one AgentRun."""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        model: AgentModelPort,
    ) -> None:
        self._runs = runs
        self._model = model

    async def generate(self, prompt: str, *, run_id: str, purpose: str) -> str:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise RuntimeError("agent_mcp_binding_run_missing")
        digest = hashlib.sha256(f"{purpose}\0{prompt}".encode()).hexdigest()[:24]
        sample = await self._model.sample(
            AgentModelRequest(
                request_id=f"agent-mcp:{run.run_id}:{digest}",
                binding=run.binding,
                messages=(
                    AgentMessage(
                        role="system",
                        content=(
                            "Return only the strict JSON object requested by this internal MCP "
                            "routing prompt. Do not call tools."
                        ),
                    ),
                    AgentMessage(role="user", content=prompt),
                ),
                tools=(),
                tool_choice=AgentToolChoice(mode="none"),
            )
        )
        if sample.tool_calls or not sample.visible_text.strip():
            raise RuntimeError("agent_mcp_binding_model_output_invalid")
        if sample.binding != run.binding:
            raise RuntimeError("agent_mcp_binding_model_drift")
        return sample.visible_text
