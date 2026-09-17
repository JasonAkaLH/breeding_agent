from __future__ import annotations

from typing import Protocol

from .models import AgentModelRequest, AgentSample


class AgentModelPort(Protocol):
    async def sample_agent(self, request: AgentModelRequest) -> AgentSample:
        """Return one closed, provider-neutral Agent sample."""
        ...
