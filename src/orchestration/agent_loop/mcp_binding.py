from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence

from .model_port import AgentModelPort
from .models import AgentMessage, AgentModelBinding, AgentModelRequest, AgentToolChoice
from .repository import AgentRunRepository


MCPPromptPreflight = Callable[
    [Sequence[str], AgentModelBinding], Awaitable[bool]
]


class RunBoundMCPTextGenerator:
    """Generate Router/Selector JSON with the immutable binding stored on one AgentRun."""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        model: AgentModelPort,
        prompt_preflight: MCPPromptPreflight | None = None,
    ) -> None:
        self._runs = runs
        self._model = model
        self._prompt_preflight = prompt_preflight

    async def generate(self, prompt: str, *, run_id: str, purpose: str) -> str:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise RuntimeError("agent_mcp_binding_run_missing")
        digest = hashlib.sha256(f"{purpose}\0{prompt}".encode()).hexdigest()[:24]
        system_prompt = (
            "Return only the strict JSON object requested by this internal MCP "
            "routing prompt. Do not call tools."
        )
        if self._prompt_preflight is not None and not await self._prompt_preflight(
            (system_prompt, prompt),
            run.binding,
        ):
            raise ValueError("agent_context_required_segments_too_large")
        sample = await self._model.sample_agent(
            AgentModelRequest(
                request_id=f"agent-mcp:{run.run_id}:{digest}",
                binding=run.binding,
                messages=(
                    AgentMessage(
                        role="system",
                        content=system_prompt,
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
