from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.models import (
    MCPDurableResultSnapshot,
    MCPMRTRRequestStateEvidence,
    MCPSealedState,
    MCPPendingActionPayloadSnapshot,
    MCPTerminalCandidateSnapshot,
)


@runtime_checkable
class PendingActionPayloadReader(Protocol):
    def revalidate(
        self, snapshot: MCPPendingActionPayloadSnapshot
    ) -> MCPPendingActionPayloadSnapshot: ...


@runtime_checkable
class TerminalCandidateSnapshotReader(Protocol):
    def revalidate(
        self, snapshot: MCPTerminalCandidateSnapshot
    ) -> MCPTerminalCandidateSnapshot: ...


@runtime_checkable
class DurableResultSnapshotReader(Protocol):
    def revalidate(
        self, snapshot: MCPDurableResultSnapshot
    ) -> MCPDurableResultSnapshot: ...


@runtime_checkable
class MRTRRequestStateEvidenceReader(Protocol):
    def read(
        self,
        record: MCPSealedState,
        *,
        server_id: str,
        protocol_version: str,
    ) -> MCPMRTRRequestStateEvidence: ...


__all__ = [
    "MCPPendingActionPayloadSnapshot",
    "PendingActionPayloadReader",
    "TerminalCandidateSnapshotReader",
    "DurableResultSnapshotReader",
    "MRTRRequestStateEvidenceReader",
]
