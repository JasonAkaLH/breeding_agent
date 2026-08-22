from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from src.lifecycle.agent_run_recovery import AgentAuthorityResolution
from src.orchestration.agent_loop.continuation import (
    AgentContinuationLocator,
    AgentResumeKind,
)
from src.orchestration.agent_loop.models import AgentCallOutcomeStatus, AgentStorageConflict


class MCPAgentAuthorityKind(StrEnum):
    APPROVAL = "approval"
    ELICITATION = "elicitation"
    REMOTE_TASK = "remote_task"


class MCPAgentAuthorityState(StrEnum):
    WAITING = "waiting"
    TERMINAL_COMPLETED = "terminal_completed"
    TERMINAL_FAILED = "terminal_failed"
    UNKNOWN_SIDE_EFFECT = "unknown_side_effect"


@dataclass(frozen=True, slots=True)
class MCPAgentAuthoritySnapshot:
    kind: MCPAgentAuthorityKind
    state: MCPAgentAuthorityState
    locator_digest: str
    authority_digest: str
    safe_result_payload: Any = None
    safe_error_code: str | None = None
    result_receipt_ref: str | None = None


class MCPAgentRecoveryAdapter:
    """Projects existing durable MCP authority; it never invokes tools/call."""

    _RESUME_KIND = {
        MCPAgentAuthorityKind.APPROVAL: AgentResumeKind.MCP_APPROVAL,
        MCPAgentAuthorityKind.ELICITATION: AgentResumeKind.MCP_ELICITATION,
        MCPAgentAuthorityKind.REMOTE_TASK: AgentResumeKind.MCP_REMOTE_TASK,
    }

    def project(
        self,
        locator: AgentContinuationLocator,
        snapshot: MCPAgentAuthoritySnapshot,
    ) -> AgentAuthorityResolution:
        if (
            not isinstance(snapshot.kind, MCPAgentAuthorityKind)
            or not isinstance(snapshot.state, MCPAgentAuthorityState)
            or snapshot.kind not in self._RESUME_KIND
            or not isinstance(snapshot.authority_digest, str)
            or not isinstance(snapshot.locator_digest, str)
            or snapshot.locator_digest != locator.digest
            or snapshot.authority_digest != locator.authority_digest
            or self._RESUME_KIND[snapshot.kind] is not locator.resume_kind
        ):
            raise AgentStorageConflict("agent_mcp_authority_identity_mismatch")
        if snapshot.state is MCPAgentAuthorityState.WAITING:
            status = locator.resume_kind.waiting_status
            result = {"status": "waiting"}
            reason = None
        elif snapshot.state in {
            MCPAgentAuthorityState.TERMINAL_COMPLETED,
            MCPAgentAuthorityState.TERMINAL_FAILED,
        }:
            if (
                not isinstance(snapshot.result_receipt_ref, str)
                or not snapshot.result_receipt_ref.strip()
            ):
                raise AgentStorageConflict("agent_mcp_terminal_receipt_missing")
            status = (
                AgentCallOutcomeStatus.COMPLETED
                if snapshot.state is MCPAgentAuthorityState.TERMINAL_COMPLETED
                else AgentCallOutcomeStatus.FAILED
            )
            result = snapshot.safe_result_payload
            reason = (
                None
                if status is AgentCallOutcomeStatus.COMPLETED
                else snapshot.safe_error_code or "mcp_authority_failed"
            )
        else:
            status = AgentCallOutcomeStatus.ABORTED
            result = {"status": "aborted"}
            reason = "mcp_side_effect_unknown_no_replay"
        facts = {
            "authority_kind": snapshot.kind.value,
            "authority_state": snapshot.state.value,
        }
        if snapshot.result_receipt_ref is not None:
            facts["result_receipt_ref"] = snapshot.result_receipt_ref
        return AgentAuthorityResolution(
            authority_digest=snapshot.authority_digest,
            status=status,
            safe_result_payload=result,
            safe_continuation_facts=facts,
            safe_error_code=reason,
        )


__all__ = [
    "MCPAgentAuthorityKind",
    "MCPAgentAuthoritySnapshot",
    "MCPAgentAuthorityState",
    "MCPAgentRecoveryAdapter",
]
