from __future__ import annotations

import hashlib
import unittest

from src.integrations.mcp.agent_recovery import (
    MCPAgentAuthorityKind,
    MCPAgentAuthoritySnapshot,
    MCPAgentAuthorityState,
    MCPAgentRecoveryAdapter,
)
from src.orchestration.agent_loop.continuation import (
    AgentContinuationLocator,
    AgentResumeKind,
)
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeStatus,
    AgentModelBinding,
    AgentStorageConflict,
)


def _locator(kind: AgentResumeKind) -> AgentContinuationLocator:
    return AgentContinuationLocator(
        run_id="run-1",
        sample_item_id="sample-1",
        call_item_id="call-item-1",
        provider_call_id="provider-call-1",
        capability_id="mcp.dispatch",
        task_id="task-1",
        node_id="node-1",
        owner_scope="owner-1",
        conversation_id="conv-1",
        resume_kind=kind,
        authority_digest=hashlib.sha256(b"authority").hexdigest(),
        pinned_bundle_revision=None,
        model_binding=AgentModelBinding("edition-fixed"),
    )


class MCPAgentRecoveryAdapterTest(unittest.TestCase):
    def test_approval_elicitation_and_remote_terminal_authority_project_closed_result(self) -> None:
        adapter = MCPAgentRecoveryAdapter()
        for resume_kind, authority_kind in (
            (AgentResumeKind.MCP_APPROVAL, MCPAgentAuthorityKind.APPROVAL),
            (AgentResumeKind.MCP_ELICITATION, MCPAgentAuthorityKind.ELICITATION),
            (AgentResumeKind.MCP_REMOTE_TASK, MCPAgentAuthorityKind.REMOTE_TASK),
        ):
            with self.subTest(kind=authority_kind):
                locator = _locator(resume_kind)
                resolution = adapter.project(
                    locator,
                    MCPAgentAuthoritySnapshot(
                        kind=authority_kind,
                        state=MCPAgentAuthorityState.TERMINAL_COMPLETED,
                        locator_digest=locator.digest,
                        authority_digest=locator.authority_digest,
                        safe_result_payload={"projection": "closed"},
                        result_receipt_ref="receipt-1",
                    ),
                )
                self.assertEqual(resolution.status, AgentCallOutcomeStatus.COMPLETED)
                self.assertEqual(
                    resolution.safe_continuation_facts["result_receipt_ref"],
                    "receipt-1",
                )

    def test_unknown_side_effect_maps_to_aborted_without_replay_instruction(self) -> None:
        locator = _locator(AgentResumeKind.MCP_REMOTE_TASK)
        resolution = MCPAgentRecoveryAdapter().project(
            locator,
            MCPAgentAuthoritySnapshot(
                kind=MCPAgentAuthorityKind.REMOTE_TASK,
                state=MCPAgentAuthorityState.UNKNOWN_SIDE_EFFECT,
                locator_digest=locator.digest,
                authority_digest=locator.authority_digest,
            ),
        )

        self.assertEqual(resolution.status, AgentCallOutcomeStatus.ABORTED)
        self.assertEqual(
            resolution.safe_error_code,
            "mcp_side_effect_unknown_no_replay",
        )

    def test_locator_or_authority_digest_mismatch_fails_closed(self) -> None:
        locator = _locator(AgentResumeKind.MCP_APPROVAL)
        with self.assertRaisesRegex(AgentStorageConflict, "identity_mismatch"):
            MCPAgentRecoveryAdapter().project(
                locator,
                MCPAgentAuthoritySnapshot(
                    kind=MCPAgentAuthorityKind.APPROVAL,
                    state=MCPAgentAuthorityState.TERMINAL_COMPLETED,
                    locator_digest="0" * 64,
                    authority_digest=locator.authority_digest,
                    result_receipt_ref="receipt-1",
                ),
            )

        with self.assertRaisesRegex(AgentStorageConflict, "identity_mismatch"):
            MCPAgentRecoveryAdapter().project(
                locator,
                MCPAgentAuthoritySnapshot(
                    kind="invalid",  # type: ignore[arg-type]
                    state=MCPAgentAuthorityState.UNKNOWN_SIDE_EFFECT,
                    locator_digest=locator.digest,
                    authority_digest=locator.authority_digest,
                ),
            )
