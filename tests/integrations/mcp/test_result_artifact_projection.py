from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from src.core.enums import ArtifactType, EventVisibility
from src.core.models import (
    Artifact,
    MCPCallRecord,
    MCPDurableResultLifecycle,
    MCPDurableResultLifecycleReason,
    MCPDurableResultLifecycleStatus,
    MCPTerminalResultCompletionMode,
    MCPTerminalResultReceipt,
    MCPTerminalState,
)
from src.integrations.mcp.result_artifact_projection import (
    MCPResultArtifactProjectionReason,
    MCPResultArtifactProjectionStatus,
    MCPResultArtifactProjector,
    fold_mcp_result_artifact_projection_payloads,
    mcp_result_artifact_projection_event_id,
    parse_mcp_result_artifact_projection_payload,
)
from src.storage.artifact_files import LocalArtifactFileStore


NOW = datetime(2026, 8, 19, 12, 0, 0)
RESULT_REF = "mcp-result:v1:" + "a" * 64
CONTENT_SHA = "sha256:" + "b" * 64


def _lifecycle(*, eligible_at: datetime | None = None):
    return MCPDurableResultLifecycle(
        result_ref=RESULT_REF,
        owner_user_id="alice",
        task_id="task-1",
        node_id="node-1",
        call_id="call-1",
        content_sha256=CONTENT_SHA,
        size_bytes=11,
        data_filename="data.json",
        manifest_filename="manifest.json",
        data_file_sha256="c" * 64,
        manifest_file_sha256="d" * 64,
        store_kind="durable_content_addressed",
        status=MCPDurableResultLifecycleStatus.RETAINED,
        reason=MCPDurableResultLifecycleReason.DISPATCH_RESOLVED,
        revision=3,
        created_at=NOW,
        updated_at=NOW,
        eligible_at=eligible_at or NOW + timedelta(hours=24),
    )


def _call(*, status: str = "completed", result_ref: str | None = RESULT_REF):
    return MCPCallRecord(
        call_ref="call-1",
        branch_id="branch-1",
        owner_user_id="alice",
        task_id="task-1",
        node_id="node-1",
        server_id="server-1",
        tool_name="start/parse job",
        status=status,
        call_sequence=1,
        arguments_sha256="e" * 64,
        server_security_version=1,
        input_schema_sha256="f" * 64,
        result_ref=result_ref,
        output_size_bytes=11,
        terminal_at=NOW,
    )


def _receipt(*, state: MCPTerminalState = MCPTerminalState.COMPLETED):
    return MCPTerminalResultReceipt(
        result_receipt_id="receipt-1",
        candidate_id="candidate-1",
        owner_user_id="alice",
        conversation_id="conv-1",
        task_id="task-1",
        node_id="node-1",
        intent_id="intent-1",
        call_id="call-1",
        server_id="server-1",
        server_config_version=1,
        server_security_version=1,
        terminal_state=state,
        result_payload_sha256="0" * 64,
        safe_result_ref=RESULT_REF,
        safe_result_ref_sha256="1" * 64,
        safe_error_code=None,
        completion_mode=MCPTerminalResultCompletionMode.NORMAL_TERMINAL_PROJECTION,
        committed_at=NOW,
        safe_result_content_sha256=CONTENT_SHA,
        safe_result_size_bytes=11,
        safe_result_store_kind="durable_content_addressed",
    )


class _Storage:
    def __init__(self, lifecycle=None, call=None, receipt=None, artifact=None):
        self.lifecycle = lifecycle or _lifecycle()
        self.call = call or _call()
        self.receipt = receipt or _receipt()
        self.artifact = artifact

    async def get_mcp_durable_result_lifecycle(self, result_ref):
        return self.lifecycle if result_ref == RESULT_REF else None

    async def get_mcp_call_record(self, owner_user_id, task_id, call_ref):
        if (owner_user_id, task_id, call_ref) == ("alice", "task-1", "call-1"):
            return self.call
        return None

    async def get_mcp_terminal_result_receipt_for_call(self, call_id):
        return self.receipt if call_id == "call-1" else None

    async def get_artifact(self, artifact_id):
        return self.artifact


class _Manager:
    def __init__(self, storage):
        self.storage = storage
        self.calls = []

    async def promote_to_artifact(self, *, result_ref, filename, summary):
        self.calls.append((result_ref, filename, summary))
        artifact = Artifact(
            artifact_id="artifact-1",
            task_id="task-1",
            producer_node_id="node-1",
            artifact_type=ArtifactType.FILE,
            storage_ref="{}",
            summary=summary,
            is_complete=True,
            created_at=NOW,
        )
        self.storage.artifact = artifact
        self.storage.lifecycle = replace(
            self.storage.lifecycle,
            status=MCPDurableResultLifecycleStatus.ARTIFACT_OWNED,
            reason=MCPDurableResultLifecycleReason.ARTIFACT_PROMOTED,
            revision=self.storage.lifecycle.revision + 1,
        )
        return artifact


class _Signer:
    def safe_reference(self, value, *, context):
        self.last = (value, context)
        return "a" * 64


class ResultArtifactProjectionContractTest(unittest.TestCase):
    def test_exact_payload_and_reason_aware_event_id(self):
        payload = {
            "schema": "maf.user_mcp.result_artifact_projection.v1",
            "safe_call_ref": "a" * 64,
            "status": "deferred",
            "reason_code": "capacity_unavailable",
            "artifact_count": 0,
        }
        parsed = parse_mcp_result_artifact_projection_payload(payload)
        self.assertEqual(parsed.status, MCPResultArtifactProjectionStatus.DEFERRED)
        with self.assertRaises(ValueError):
            parse_mcp_result_artifact_projection_payload({**payload, "extra": True})
        self.assertNotEqual(
            mcp_result_artifact_projection_event_id(
                "artifact-1", parsed.status, parsed.reason_code
            ),
            mcp_result_artifact_projection_event_id(
                "artifact-1",
                parsed.status,
                MCPResultArtifactProjectionReason.PROJECTION_FAILED,
            ),
        )

    def test_fold_is_order_independent_and_terminal_fork_fails(self):
        common = {
            "schema": "maf.user_mcp.result_artifact_projection.v1",
            "safe_call_ref": "a" * 64,
            "artifact_count": 0,
        }
        deferred_capacity = {
            **common,
            "status": "deferred",
            "reason_code": "capacity_unavailable",
        }
        deferred_failure = {
            **common,
            "status": "deferred",
            "reason_code": "projection_failed",
        }
        folded = fold_mcp_result_artifact_projection_payloads(
            [deferred_failure, deferred_capacity]
        )
        self.assertEqual(len(folded), 1)
        self.assertEqual(
            folded[0].reason_code,
            MCPResultArtifactProjectionReason.PROJECTION_FAILED,
        )
        ready = {
            **common,
            "status": "ready",
            "reason_code": "promoted",
            "artifact_count": 1,
        }
        permanent = {
            **common,
            "status": "permanent_failure",
            "reason_code": "source_expired",
        }
        with self.assertRaises(ValueError):
            fold_mcp_result_artifact_projection_payloads([ready, permanent])


class ResultArtifactProjectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_result_uses_authoritative_filename_and_stable_event(self):
        storage = _Storage()
        manager = _Manager(storage)
        events = []
        observations = []
        with tempfile.TemporaryDirectory() as directory:
            projector = MCPResultArtifactProjector(
                storage=storage,
                lifecycle_manager=manager,
                artifact_file_store=LocalArtifactFileStore(Path(directory)),
                audit_reference_signer=_Signer(),
                event_sink=events.append,
                observer=lambda item: observations.append(item),
                artifact_disk_low_watermark_bytes=10,
                free_bytes=lambda _path: 10_000,
                now_fn=lambda: NOW,
            )
            first = await projector.project_completed_result(
                RESULT_REF, source="immediate"
            )
            second = await projector.project_completed_result(
                RESULT_REF, source="reconciler"
            )
        self.assertEqual(first.status, MCPResultArtifactProjectionStatus.READY)
        self.assertEqual(manager.calls[0][1], "01-start_parse_job-result.json")
        self.assertEqual(manager.calls[0][2], "MCP Tool原始返回：start/parse job")
        self.assertEqual(events[0].visibility, EventVisibility.FRONTEND)
        self.assertEqual(events[0].created_at, NOW)
        self.assertEqual(second.status, MCPResultArtifactProjectionStatus.READY)
        self.assertEqual(second.reason_code, MCPResultArtifactProjectionReason.ALREADY_PROMOTED)
        self.assertNotEqual(events[0].event_id, events[1].event_id)
        self.assertEqual(len(observations), 2)

    async def test_capacity_is_deferred_before_deadline_and_permanent_after(self):
        for eligible_at, expected_status in (
            (NOW + timedelta(seconds=1), MCPResultArtifactProjectionStatus.DEFERRED),
            (NOW, MCPResultArtifactProjectionStatus.PERMANENT_FAILURE),
        ):
            storage = _Storage(lifecycle=_lifecycle(eligible_at=eligible_at))
            manager = _Manager(storage)
            events = []
            with tempfile.TemporaryDirectory() as directory:
                projector = MCPResultArtifactProjector(
                    storage=storage,
                    lifecycle_manager=manager,
                    artifact_file_store=LocalArtifactFileStore(Path(directory)),
                    audit_reference_signer=_Signer(),
                    event_sink=events.append,
                    artifact_disk_low_watermark_bytes=10,
                    free_bytes=lambda _path: 20,
                    now_fn=lambda: NOW,
                )
                result = await projector.project_completed_result(
                    RESULT_REF, source="immediate"
                )
            self.assertEqual(result.status, expected_status)
            self.assertEqual(
                result.reason_code,
                MCPResultArtifactProjectionReason.CAPACITY_UNAVAILABLE
                if expected_status is MCPResultArtifactProjectionStatus.DEFERRED
                else MCPResultArtifactProjectionReason.PROJECTION_FAILED,
            )
            self.assertEqual(manager.calls, [])

    async def test_failed_call_is_never_promoted(self):
        storage = _Storage(call=_call(status="failed", result_ref=None))
        manager = _Manager(storage)
        with tempfile.TemporaryDirectory() as directory:
            projector = MCPResultArtifactProjector(
                storage=storage,
                lifecycle_manager=manager,
                artifact_file_store=LocalArtifactFileStore(Path(directory)),
                audit_reference_signer=_Signer(),
                artifact_disk_low_watermark_bytes=10,
                free_bytes=lambda _path: 10_000,
                now_fn=lambda: NOW,
            )
            result = await projector.project_completed_result(
                RESULT_REF, source="immediate"
            )
        self.assertEqual(result.status, MCPResultArtifactProjectionStatus.DEFERRED)
        self.assertEqual(manager.calls, [])


if __name__ == "__main__":
    unittest.main()
