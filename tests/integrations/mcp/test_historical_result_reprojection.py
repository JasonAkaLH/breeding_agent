from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from src.core.enums import ArtifactType
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
from src.integrations.mcp.cp7_artifacts import mcp_durable_result_artifact_id
from src.integrations.mcp.result_artifact_projection import (
    MCPResultArtifactProjector,
)
from src.integrations.mcp.result_parsing import (
    MCPHistoricalResultReprojector,
    MCPIsolatedResultService,
    MCPProjectionStore,
    MCPRawResultAuthorityResolver,
)
from src.integrations.mcp.result_parsing.json_values import canonical_json_bytes
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    build_file_storage_ref,
    parse_file_storage_ref,
)


NOW = datetime(2026, 8, 20, 12, 0, 0)


class _DeletedSnapshotAuthority:
    def open_result_parser_descriptor(self, **kwargs):
        raise AssertionError("deleted durable source must not be opened")


class _Signer:
    def safe_reference(self, value, *, context):
        return "a" * 64


class _Storage:
    def __init__(self, call, receipt, lifecycle, artifact):
        self.call = call
        self.receipt = receipt
        self.lifecycle = lifecycle
        self.artifact = artifact
        self.scan_cursors = []

    async def list_completed_mcp_calls_for_result_reprojection(
        self, *, after_call_ref=None, limit=1000
    ):
        self.scan_cursors.append(after_call_ref)
        if after_call_ref is None:
            return [self.call]
        return []

    async def get_mcp_terminal_result_receipt_for_call(self, call_ref):
        return self.receipt if call_ref == self.call.call_ref else None

    async def get_mcp_durable_result_lifecycle(self, result_ref):
        return self.lifecycle if result_ref == self.lifecycle.result_ref else None

    async def get_artifact(self, artifact_id):
        return self.artifact if artifact_id == self.artifact.artifact_id else None

    async def get_mcp_call_record(self, owner_user_id, task_id, call_ref):
        if (owner_user_id, task_id, call_ref) == (
            self.call.owner_user_id,
            self.call.task_id,
            self.call.call_ref,
        ):
            return self.call
        return None

    async def compare_and_set_artifact_storage_ref(
        self, artifact_id, expected_storage_ref, replacement_storage_ref
    ):
        if (
            artifact_id != self.artifact.artifact_id
            or expected_storage_ref != self.artifact.storage_ref
        ):
            return False
        self.artifact = replace(
            self.artifact, storage_ref=replacement_storage_ref
        )
        return True


class HistoricalResultReprojectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        raw = {
            "content": [{"type": "text", "text": "safe business text"}],
            "structuredContent": {"answer": 42},
        }
        raw_bytes = json.dumps(
            raw, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        source = root / "source.json"
        source.write_bytes(raw_bytes)
        self.raw_sha = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        self.result_ref = "mcp-result:v1:" + "b" * 64
        self.artifact_id = mcp_durable_result_artifact_id(self.result_ref)
        self.file_store = LocalArtifactFileStore(root / "artifacts")
        stored = self.file_store.save_file(
            artifact_id=self.artifact_id,
            filename="result.json",
            source_path=source,
        )
        self.schema = {
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        self.schema_sha = "sha256:" + hashlib.sha256(
            canonical_json_bytes(self.schema)
        ).hexdigest()
        call = MCPCallRecord(
            call_ref="call-history-1",
            branch_id="branch-1",
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            server_id="server-1",
            tool_name="lookup",
            status="completed",
            call_sequence=1,
            arguments_sha256="c" * 64,
            server_security_version=1,
            input_schema_sha256="d" * 64,
            protocol_version="2025-11-25",
            output_schema=self.schema,
            output_schema_sha256=self.schema_sha,
            terminal_result_source="tools_call",
            result_ref=self.result_ref,
            output_size_bytes=len(raw_bytes),
            terminal_at=NOW,
        )
        receipt = MCPTerminalResultReceipt(
            result_receipt_id="receipt-1",
            candidate_id="candidate-1",
            owner_user_id="alice",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            intent_id="intent-1",
            call_id=call.call_ref,
            server_id="server-1",
            server_config_version=1,
            server_security_version=1,
            terminal_state=MCPTerminalState.COMPLETED,
            result_payload_sha256="e" * 64,
            safe_result_ref=self.result_ref,
            safe_result_ref_sha256="f" * 64,
            safe_error_code=None,
            completion_mode=(
                MCPTerminalResultCompletionMode.NORMAL_TERMINAL_PROJECTION
            ),
            committed_at=NOW,
            safe_result_content_sha256=self.raw_sha,
            safe_result_size_bytes=len(raw_bytes),
            safe_result_store_kind="durable_content_addressed",
            result_parser_revision=None,
        )
        lifecycle = MCPDurableResultLifecycle(
            result_ref=self.result_ref,
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_id=call.call_ref,
            content_sha256=self.raw_sha,
            size_bytes=len(raw_bytes),
            data_filename="deleted.json",
            manifest_filename="deleted.manifest.json",
            data_file_sha256="1" * 64,
            manifest_file_sha256="2" * 64,
            store_kind="durable_content_addressed",
            status=MCPDurableResultLifecycleStatus.DELETED,
            reason=MCPDurableResultLifecycleReason.ARTIFACT_PROMOTED,
            revision=3,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=NOW,
        )
        artifact = Artifact(
            artifact_id=self.artifact_id,
            task_id="task-1",
            producer_node_id="node-1",
            artifact_type=ArtifactType.FILE,
            storage_ref=build_file_storage_ref(
                {
                    "version": 1,
                    "source_kind": "mcp_result",
                    "storage_key": stored.storage_key,
                    "filename": stored.filename,
                    "mime_type": "application/json",
                    "size_bytes": stored.size_bytes,
                    "sha256": stored.sha256,
                    "summary": "MCP result",
                    "result_ref": self.result_ref,
                    "retention_status": "active",
                }
            ),
            summary="MCP result",
            is_complete=True,
            created_at=NOW,
        )
        self.storage = _Storage(call, receipt, lifecycle, artifact)
        self.projection_store = MCPProjectionStore(root / "projections")
        service = MCPIsolatedResultService(
            projection_store=self.projection_store
        )
        projector = MCPResultArtifactProjector(
            storage=self.storage,
            lifecycle_manager=object(),
            artifact_file_store=self.file_store,
            audit_reference_signer=_Signer(),
            artifact_disk_low_watermark_bytes=1,
        )
        self.reprojector = MCPHistoricalResultReprojector(
            storage=self.storage,
            authority_resolver=MCPRawResultAuthorityResolver(
                storage=self.storage,
                snapshot_authority=_DeletedSnapshotAuthority(),
                artifact_file_store=self.file_store,
            ),
            result_service=service,
            projection_store=self.projection_store,
            projection_attacher=projector.attach_published_projection,
        )

    async def test_source_deleted_managed_copy_reprojects_with_zero_network_calls(self) -> None:
        network_calls = 0

        summary = await self.reprojector.run_once(limit=1)

        self.assertEqual(network_calls, 0)
        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.ready, 1)
        self.assertEqual(self.storage.scan_cursors, [None, "call-history-1"])
        metadata = parse_file_storage_ref(self.storage.artifact.storage_ref)
        self.assertRegex(metadata["projection_ref"], r"^mcp-projection-[0-9a-f]{64}$")
        self.assertNotIn("mcp_projection_unavailable_reason", metadata)
        envelope = self.projection_store.load(
            metadata["projection_ref"],
            binding=self._binding(metadata),
            expected_projection_sha256=metadata["projection_sha256"],
        )
        self.assertEqual(
            envelope["user_view"]["primary"]["value"], {"answer": 42}
        )
        self.assertNotIn("content", json.dumps(envelope["user_view"]))

    async def test_missing_schema_authority_marks_closed_unavailable_without_parsing(self) -> None:
        self.storage.call = replace(
            self.storage.call,
            output_schema=None,
            output_schema_sha256=None,
        )

        summary = await self.reprojector.run_once(limit=1000)

        self.assertEqual(summary.historical_authority_invalid, 1)
        metadata = parse_file_storage_ref(self.storage.artifact.storage_ref)
        self.assertEqual(
            metadata["mcp_projection_unavailable_reason"],
            "historical_authority_invalid",
        )
        self.assertNotIn("projection_ref", metadata)

    def _binding(self, metadata):
        from src.integrations.mcp.result_parsing.projection_store import (
            MCPProjectionBinding,
        )

        return MCPProjectionBinding(
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_ref="call-history-1",
            raw_sha256=self.raw_sha,
            output_schema_sha256=self.schema_sha,
            source="tools_call",
            parser_revision=metadata["parser_revision"],
        )
