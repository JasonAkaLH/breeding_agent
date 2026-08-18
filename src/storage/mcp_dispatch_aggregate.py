from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.models import MCPPendingActionPayloadSnapshot


@runtime_checkable
class PendingActionPayloadReader(Protocol):
    def revalidate(
        self, snapshot: MCPPendingActionPayloadSnapshot
    ) -> MCPPendingActionPayloadSnapshot: ...


__all__ = [
    "MCPPendingActionPayloadSnapshot",
    "PendingActionPayloadReader",
]
