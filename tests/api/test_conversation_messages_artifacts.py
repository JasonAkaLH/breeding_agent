from __future__ import annotations

import hashlib
import json
from datetime import datetime
from functools import wraps
from unittest.mock import patch

from src.core.enums import ArtifactType, EventVisibility, MessageRole, TaskStatus
from src.core.models import Artifact, Conversation, EventRecord, Interrupt, Message, Task
from src.storage.artifact_files import build_file_storage_ref
from src.integrations.mcp.result_parsing.projection_store import (
    MCPProjectionBinding,
    MCPProjectionStore,
)
from src.storage.conversation_files import FILE_UPLOAD_MESSAGE_TYPE, file_upload_message_id
from tests.api.support import APITestCase


def _publish_legacy_v1_projection(root, envelope, binding):
    data = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    projection_sha256 = "sha256:" + hashlib.sha256(data).hexdigest()
    identity = json.dumps(
        {
            "owner_user_id": binding.owner_user_id,
            "task_id": binding.task_id,
            "node_id": binding.node_id,
            "call_ref": binding.call_ref,
            "raw_sha256": binding.raw_sha256,
            "output_schema_sha256": binding.output_schema_sha256,
            "source": binding.source,
            "parser_revision": binding.parser_revision,
            "projection_sha256": projection_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    projection_ref = "mcp-projection-" + hashlib.sha256(identity).hexdigest()
    manifest = {
        "schema": "maf.mcp.parsed_result_projection_manifest.v1",
        "projection_ref": projection_ref,
        "owner_user_id": binding.owner_user_id,
        "task_id": binding.task_id,
        "node_id": binding.node_id,
        "call_ref": binding.call_ref,
        "raw_sha256": binding.raw_sha256,
        "output_schema_sha256": binding.output_schema_sha256,
        "source": binding.source,
        "parser_revision": binding.parser_revision,
        "projection_sha256": projection_sha256,
        "size_bytes": len(data),
    }
    projection_path = root / f"{projection_ref}.json"
    manifest_path = root / f"{projection_ref}.manifest.json"
    projection_path.write_bytes(data)
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    projection_path.chmod(0o600)
    manifest_path.chmod(0o600)
    return projection_ref, projection_sha256


class ConversationMessagesArtifactRestoreAPITest(APITestCase):
    async def test_assistant_history_retry_reuses_durable_artifact_timestamp(self) -> None:
        conversation_id = "conv-history-retry-created-at"
        task_id = "task-history-retry-created-at"
        artifact_created_at = datetime(2026, 6, 3, 2, 0, 3)
        await self.runtime.storage.save_conversation(Conversation(conversation_id, "acc-1"))
        await self.runtime.storage.save_task(
            Task(
                task_id,
                conversation_id,
                root_message_id="msg-history-retry-root",
                status=TaskStatus.COMPLETED,
                created_at=datetime(2026, 6, 3, 2, 0, 1),
                updated_at=datetime(2026, 6, 3, 2, 0, 2),
            )
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                "agent-artifact:history-retry:final",
                task_id,
                "node-history-retry-final",
                ArtifactType.TEXT,
                "stable assistant answer",
                is_complete=True,
                created_at=artifact_created_at,
            )
        )
        original_save_message = self.runtime.storage.save_message
        attempted_created_at: list[datetime | None] = []

        async def fail_after_reservation(message, **_kwargs):
            attempted_created_at.append(message.created_at)
            raise RuntimeError("sql_write_failed_after_reservation")

        with patch.object(self.runtime.storage, "save_message", side_effect=fail_after_reservation):
            with self.assertRaisesRegex(RuntimeError, "sql_write_failed_after_reservation"):
                await self.runtime._persist_assistant_history_message(task_id, conversation_id)

        async def save_on_retry(message, **kwargs):
            attempted_created_at.append(message.created_at)
            return await original_save_message(message, **kwargs)

        with patch.object(self.runtime.storage, "save_message", side_effect=save_on_retry):
            await self.runtime._persist_assistant_history_message(task_id, conversation_id)

        self.assertEqual(attempted_created_at, [artifact_created_at, artifact_created_at])
        saved = await self.runtime.storage.get_message(f"{task_id}:assistant")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.created_at, artifact_created_at)

    async def _save_conversation_with_messages(self, conversation_id: str = "conv-history-artifacts") -> None:
        await self.runtime.storage.save_conversation(Conversation(conversation_id, "acc-1"))
        await self.runtime.storage.save_message(
            Message(
                f"{conversation_id}:user",
                conversation_id,
                MessageRole.USER,
                "隆平高科2021年审定了哪些玉米品种？",
                task_id=f"{conversation_id}:task-1",
                created_at=datetime(2026, 6, 3, 1, 0, 0),
            )
        )
        await self.runtime.storage.save_task(
            Task(
                f"{conversation_id}:task-1",
                conversation_id,
                root_message_id=f"{conversation_id}:user",
                status=TaskStatus.COMPLETED,
                created_at=datetime(2026, 6, 3, 1, 0, 1),
            )
        )
        await self.runtime.storage.save_message(
            Message(
                f"{conversation_id}:assistant",
                conversation_id,
                MessageRole.ASSISTANT,
                "最终回答文本",
                task_id=f"{conversation_id}:task-1",
                stream_status="complete",
                created_at=datetime(2026, 6, 3, 1, 0, 2),
            )
        )

    async def _save_json_artifact(self, conversation_id: str, artifact_id: str, payload, *, summary: str | None = None) -> None:
        storage_ref = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        await self.runtime.storage.save_artifact(
            Artifact(
                artifact_id,
                f"{conversation_id}:task-1",
                f"{conversation_id}:skill_data_query",
                ArtifactType.JSON,
                storage_ref,
                summary=summary,
                is_complete=True,
                created_at=datetime(2026, 6, 3, 1, 0, 3),
            )
        )

    async def test_conversation_messages_include_sqlquery_filtered_result_artifact(self) -> None:
        conversation_id = "conv-history-sqlquery-filtered"
        await self._save_conversation_with_messages(conversation_id)
        await self._save_json_artifact(
            conversation_id,
            f"{conversation_id}:filtered_query_result:1",
            {
                "artifact_role": "filtered_query_result",
                "domain_kind": "sql_query",
                "columns": ["品种名称"],
                "rows": [{"品种名称": "隆平381"}],
                "row_count": 1,
                "truncated": False,
            },
            summary="filtered query result with 1 rows",
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        user_message = next(message for message in payload["messages"] if message["role"] == "user")
        assistant_message = next(message for message in payload["messages"] if message["role"] == "assistant")
        self.assertEqual(user_message["artifacts"], [])
        self.assertEqual(len(assistant_message["artifacts"]), 1)
        artifact = assistant_message["artifacts"][0]
        self.assertEqual(artifact["artifact_id"], f"{conversation_id}:filtered_query_result:1")
        self.assertEqual(artifact["artifact_type"], "json")
        self.assertEqual(json.loads(artifact["storage_ref"])["columns"], ["品种名称"])

    async def test_conversation_messages_include_closed_mcp_result_artifact_projection(
        self,
    ) -> None:
        conversation_id = "conv-history-mcp-result-projection"
        await self._save_conversation_with_messages(conversation_id)
        task_id = f"{conversation_id}:task-1"
        await self.runtime.storage.append_event(
            EventRecord(
                event_id="mcp-result-artifact-projection:v1:artifact-1:deferred:projection_failed",
                conversation_id=conversation_id,
                task_id=task_id,
                node_id=f"{conversation_id}:node-1",
                event_type="mcp.result_artifact_projection",
                payload={
                    "schema": "maf.user_mcp.result_artifact_projection.v1",
                    "safe_call_ref": "a" * 64,
                    "status": "deferred",
                    "reason_code": "projection_failed",
                    "artifact_count": 0,
                },
                visibility=EventVisibility.FRONTEND,
                created_at=datetime(2026, 6, 3, 1, 0, 4),
            )
        )

        response = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )

        self.assertEqual(response.status_code, 200)
        messages = response.json()["messages"]
        user_message = next(message for message in messages if message["role"] == "user")
        assistant_message = next(
            message for message in messages if message["role"] == "assistant"
        )
        self.assertEqual(user_message["mcp_result_artifact_projections"], [])
        self.assertEqual(
            assistant_message["mcp_result_artifact_projections"],
            [
                {
                    "schema": "maf.user_mcp.result_artifact_projection.v1",
                    "safe_call_ref": "a" * 64,
                    "status": "deferred",
                    "reason_code": "projection_failed",
                    "artifact_count": 0,
                }
            ],
        )
        task_response = await self.client.get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(task_response.status_code, 200)
        self.assertEqual(
            task_response.json()["mcp_result_artifact_projections"],
            assistant_message["mcp_result_artifact_projections"],
        )

    async def test_conversation_messages_include_query_result_preview_when_no_filtered_result(self) -> None:
        conversation_id = "conv-history-sqlquery-preview"
        await self._save_conversation_with_messages(conversation_id)
        await self._save_json_artifact(
            conversation_id,
            f"{conversation_id}:query_result_preview:1",
            {
                "artifact_role": "query_result_preview",
                "domain_kind": "sql_query",
                "columns": ["品种名称"],
                "rows": [{"品种名称": "隆平381"}],
                "row_count": 1,
            },
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        assistant_message = next(message for message in response.json()["messages"] if message["role"] == "assistant")
        self.assertEqual([artifact["artifact_id"] for artifact in assistant_message["artifacts"]], [f"{conversation_id}:query_result_preview:1"])

    async def test_conversation_messages_exclude_internal_sqlquery_artifacts(self) -> None:
        conversation_id = "conv-history-internal-filter"
        await self._save_conversation_with_messages(conversation_id)
        for role in ("generated_sql", "guard_report", "schema_context_snapshot", "intent_summary"):
            await self._save_json_artifact(
                conversation_id,
                f"{conversation_id}:{role}:1",
                {"artifact_role": role, "domain_kind": "sql_query", "summary": role},
            )
        await self.runtime.storage.save_artifact(
            Artifact(
                f"{conversation_id}:main_agent_response:final:1",
                f"{conversation_id}:task-1",
                f"{conversation_id}:global_final_answer",
                ArtifactType.TEXT,
                "最终回答文本",
                summary="final",
                is_complete=True,
            )
        )
        await self._save_json_artifact(
            conversation_id,
            f"{conversation_id}:filtered_query_result:1",
            {"artifact_role": "filtered_query_result", "columns": ["品种名称"], "rows": [], "row_count": 0},
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        assistant_message = next(message for message in response.json()["messages"] if message["role"] == "assistant")
        self.assertEqual([artifact["artifact_id"] for artifact in assistant_message["artifacts"]], [f"{conversation_id}:filtered_query_result:1"])

    async def test_malformed_or_unknown_json_artifacts_are_skipped_without_failing_messages_route(self) -> None:
        conversation_id = "conv-history-malformed"
        await self._save_conversation_with_messages(conversation_id)
        await self._save_json_artifact(conversation_id, f"{conversation_id}:filtered_query_result:bad", "{bad-json")
        await self._save_json_artifact(conversation_id, f"{conversation_id}:unknown:1", {"artifact_role": "unknown_display"})
        await self._save_json_artifact(
            conversation_id,
            f"{conversation_id}:filtered_query_result:good",
            {"artifact_role": "filtered_query_result", "columns": ["品种名称"], "rows": [], "row_count": 0},
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        assistant_message = next(message for message in response.json()["messages"] if message["role"] == "assistant")
        self.assertEqual([artifact["artifact_id"] for artifact in assistant_message["artifacts"]], [f"{conversation_id}:filtered_query_result:good"])

    async def test_conversation_messages_include_active_file_artifact_download_metadata_and_exclude_inactive(self) -> None:
        conversation_id = "conv-history-file"
        await self._save_conversation_with_messages(conversation_id)
        active_metadata = {
            "source_kind": "skill_output",
            "retention_status": "active",
            "storage_key": "conv-history-file/task/file.txt",
            "filename": "result.txt",
            "mime_type": "text/plain",
            "size_bytes": 12,
            "sha256": "sha256:file",
            "source_file_count": 1,
            "summary": "结果文件",
        }
        inactive_metadata = dict(active_metadata, retention_status="deleted", storage_key="conv-history-file/task/old.txt", filename="old.txt")
        await self.runtime.storage.save_artifact(
            Artifact(
                f"{conversation_id}:file-active",
                f"{conversation_id}:task-1",
                f"{conversation_id}:field_design",
                ArtifactType.FILE,
                build_file_storage_ref(active_metadata),
                summary="结果文件",
                is_complete=True,
                created_at=datetime(2026, 6, 3, 1, 0, 3),
            )
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                f"{conversation_id}:file-inactive",
                f"{conversation_id}:task-1",
                f"{conversation_id}:field_design",
                ArtifactType.FILE,
                build_file_storage_ref(inactive_metadata),
                summary="旧结果文件",
                is_complete=True,
                created_at=datetime(2026, 6, 3, 1, 0, 4),
            )
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        assistant_message = next(message for message in response.json()["messages"] if message["role"] == "assistant")
        self.assertEqual(len(assistant_message["artifacts"]), 1)
        artifact = assistant_message["artifacts"][0]
        self.assertEqual(artifact["artifact_id"], f"{conversation_id}:file-active")
        self.assertEqual(artifact["artifact_type"], "file")
        self.assertEqual(artifact["storage_ref"], "")
        self.assertEqual(artifact["filename"], "result.txt")
        self.assertEqual(artifact["mime_type"], "text/plain")
        self.assertEqual(artifact["download_url"], f"/api/v1/artifacts/{conversation_id}:file-active/download")

    async def test_mcp_result_file_is_safe_hidden_and_cannot_be_downloaded(self) -> None:
        conversation_id = "conv-history-mcp-result-text"
        await self._save_conversation_with_messages(conversation_id)
        task_id = f"{conversation_id}:task-1"
        artifact_id = "mcp-result-artifact:v1:" + "a" * 64
        original_text = '{"result":{"content":[{"type":"text","text":"原始返回"}]}}'
        source_path = self.workspace / "mcp-result-text-source.json"
        source_path.write_text(original_text, encoding="utf-8")
        stored = self.runtime.artifact_file_store.save_file(
            artifact_id=artifact_id,
            filename="01-tool-result.json",
            source_path=source_path,
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                artifact_id,
                task_id,
                f"{conversation_id}:mcp-tool",
                ArtifactType.FILE,
                build_file_storage_ref(
                    {
                        "version": 1,
                        "source_kind": "mcp_result",
                        "storage_key": stored.storage_key,
                        "filename": stored.filename,
                        "mime_type": "application/json",
                        "size_bytes": stored.size_bytes,
                        "sha256": stored.sha256,
                        "summary": "MCP Tool原始返回：tool",
                        "retention_status": "active",
                    }
                ),
                summary="MCP Tool原始返回：tool",
                is_complete=True,
                created_at=datetime(2026, 6, 3, 1, 0, 3),
            )
        )

        task_response = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        history_response = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        download_response = await self.client.get(
            f"/api/v1/artifacts/{artifact_id}/download"
        )

        self.assertEqual(task_response.status_code, 200)
        task_artifact = next(
            item
            for item in task_response.json()["artifacts"]
            if item["artifact_id"] == artifact_id
        )
        self.assertEqual(task_artifact["artifact_type"], "mcp_result")
        self.assertEqual(task_artifact["storage_ref"], "")
        self.assertEqual(
            task_artifact["mcp_business_result"],
            {
                "schema": "maf.mcp.business_result_view.v1",
                "availability": "unavailable",
                "outcome": "succeeded",
                "primary": None,
                "unavailable_reason": "safe_hide",
                "supplemental_texts": None,
                "content_metadata": None,
                "projection_truncated": False,
            },
        )
        self.assertIsNone(task_artifact["download_url"])
        self.assertIsNone(task_artifact["filename"])

        self.assertEqual(history_response.status_code, 200)
        assistant_message = next(
            message
            for message in history_response.json()["messages"]
            if message["role"] == "assistant"
        )
        history_artifact = next(
            item
            for item in assistant_message["artifacts"]
            if item["artifact_id"] == artifact_id
        )
        self.assertEqual(history_artifact["artifact_type"], "mcp_result")
        self.assertEqual(history_artifact["storage_ref"], "")
        self.assertEqual(
            history_artifact["mcp_business_result"]["unavailable_reason"],
            "safe_hide",
        )
        self.assertEqual(download_response.status_code, 404)

    async def test_mcp_result_published_projection_is_ready_for_task_and_history(self) -> None:
        conversation_id = "conv-history-mcp-ready"
        await self._save_conversation_with_messages(conversation_id)
        task_id = f"{conversation_id}:task-1"
        node_id = f"{conversation_id}:mcp-tool"
        call_ref = "call-ready"
        artifact_id = "mcp-result-artifact:v1:" + "c" * 64
        source_path = self.workspace / "mcp-ready-source.json"
        source_path.write_text('{"content":[]}', encoding="utf-8")
        stored = self.runtime.artifact_file_store.save_file(
            artifact_id=artifact_id,
            filename="result.json",
            source_path=source_path,
        )
        projection_store = MCPProjectionStore(self.workspace / "mcp-projections")
        self.runtime._mcp_projection_store = projection_store
        binding = MCPProjectionBinding(
            owner_user_id="acc-1",
            task_id=task_id,
            node_id=node_id,
            call_ref=call_ref,
            raw_sha256="sha256:" + stored.sha256,
            output_schema_sha256=None,
            source="tools_call",
            parser_revision="mcp-result-parser.v2",
        )
        envelope = json.dumps(
            {
                "schema": "maf.mcp.parsed_result_projection.v2",
                "parsed_model_sha256": "sha256:" + "d" * 64,
                "user_view": {
                    "schema": "maf.mcp.business_result_view.v1",
                    "availability": "ready",
                    "outcome": "succeeded",
                    "primary": {
                        "kind": "text",
                        "text": "安全业务结果",
                        "truncated": False,
                    },
                    "projection_truncated": False,
                },
                "agent_projection": "untrusted business result",
                "agent_projection_truncated": False,
                "workflow_control": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        handle = projection_store.stage(envelope, binding=binding)
        published = projection_store.publish(handle)
        await self.runtime.storage.save_artifact(
            Artifact(
                artifact_id,
                task_id,
                node_id,
                ArtifactType.FILE,
                build_file_storage_ref(
                    {
                        "version": 1,
                        "source_kind": "mcp_result",
                        "visibility": "internal_raw",
                        "storage_key": stored.storage_key,
                        "filename": stored.filename,
                        "mime_type": "application/json",
                        "size_bytes": stored.size_bytes,
                        "sha256": stored.sha256,
                        "summary": "MCP result",
                        "result_ref": "internal-result-ref",
                        "retention_status": "active",
                        "protocol_version": "2025-11-25",
                        "terminal_result_source": "tools_call",
                        "output_schema_sha256": None,
                        "parser_revision": "mcp-result-parser.v2",
                        "projection_schema": "maf.mcp.parsed_result_projection.v2",
                        "projection_ref": published.projection_ref,
                        "projection_sha256": published.projection_sha256,
                        "owner_user_id": "acc-1",
                        "call_ref": call_ref,
                    }
                ),
                is_complete=True,
            )
        )

        task_response = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        history_response = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )

        task_artifact = task_response.json()["artifacts"][0]
        history_artifact = next(
            message for message in history_response.json()["messages"]
            if message["role"] == "assistant"
        )["artifacts"][0]
        for projected in (task_artifact, history_artifact):
            self.assertEqual(projected["artifact_type"], "mcp_result")
            self.assertEqual(projected["storage_ref"], "")
            self.assertEqual(
                projected["mcp_business_result"]["primary"]["text"],
                "安全业务结果",
            )

    async def test_retired_v1_mcp_projection_is_unavailable_for_task_and_history(self) -> None:
        conversation_id = "conv-history-mcp-retired-v1"
        await self._save_conversation_with_messages(conversation_id)
        task_id = f"{conversation_id}:task-1"
        node_id = f"{conversation_id}:mcp-tool"
        call_ref = "call-retired-v1"
        artifact_id = "mcp-result-artifact:v1:" + "e" * 64
        source_path = self.workspace / "mcp-retired-v1-source.json"
        source_path.write_text('{"content":[]}', encoding="utf-8")
        stored = self.runtime.artifact_file_store.save_file(
            artifact_id=artifact_id,
            filename="result.json",
            source_path=source_path,
        )
        projection_root = self.workspace / "mcp-retired-v1-projections"
        projection_store = MCPProjectionStore(projection_root)
        self.runtime._mcp_projection_store = projection_store
        binding = MCPProjectionBinding(
            owner_user_id="acc-1",
            task_id=task_id,
            node_id=node_id,
            call_ref=call_ref,
            raw_sha256="sha256:" + stored.sha256,
            output_schema_sha256=None,
            source="tools_call",
            parser_revision="mcp-result-parser.v1",
        )
        projection_ref, projection_sha256 = _publish_legacy_v1_projection(
            projection_root,
            {
                "schema": "maf.mcp.parsed_result_projection.v1",
                "parsed_model_sha256": "sha256:" + "f" * 64,
                "user_view": {
                    "schema": "maf.mcp.business_result_view.v1",
                    "availability": "ready",
                    "outcome": "succeeded",
                    "primary": {
                        "kind": "text",
                        "text": "retired business result",
                        "truncated": False,
                    },
                    "projection_truncated": False,
                },
                "agent_projection": "retired business result",
                "workflow_control": None,
            },
            binding,
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                artifact_id,
                task_id,
                node_id,
                ArtifactType.FILE,
                build_file_storage_ref(
                    {
                        "version": 1,
                        "source_kind": "mcp_result",
                        "visibility": "internal_raw",
                        "storage_key": stored.storage_key,
                        "filename": stored.filename,
                        "mime_type": "application/json",
                        "size_bytes": stored.size_bytes,
                        "sha256": stored.sha256,
                        "summary": "MCP result",
                        "result_ref": "internal-result-ref",
                        "retention_status": "active",
                        "protocol_version": "2025-11-25",
                        "terminal_result_source": "tools_call",
                        "output_schema_sha256": None,
                        "parser_revision": "mcp-result-parser.v1",
                        "projection_schema": "maf.mcp.parsed_result_projection.v1",
                        "projection_ref": projection_ref,
                        "projection_sha256": projection_sha256,
                        "owner_user_id": "acc-1",
                        "call_ref": call_ref,
                    }
                ),
                is_complete=True,
            )
        )

        task_response = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        history_response = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        download_response = await self.client.get(
            f"/api/v1/artifacts/{artifact_id}/download"
        )

        task_artifact = task_response.json()["artifacts"][0]
        history_artifact = next(
            message for message in history_response.json()["messages"]
            if message["role"] == "assistant"
        )["artifacts"][0]
        for projected in (task_artifact, history_artifact):
            self.assertEqual(projected["artifact_type"], "mcp_result")
            self.assertEqual(projected["storage_ref"], "")
            self.assertIsNone(projected["download_url"])
            self.assertEqual(
                projected["mcp_business_result"]["availability"],
                "unavailable",
            )
            self.assertEqual(
                projected["mcp_business_result"]["outcome"],
                "succeeded",
            )
            self.assertEqual(
                projected["mcp_business_result"]["unavailable_reason"],
                "projection_invalid",
            )
            self.assertNotIn("retired business result", json.dumps(projected))
        self.assertEqual(download_response.status_code, 404)

    async def test_conversation_messages_include_ocr_raw_text_artifact_when_text_is_present(self) -> None:
        conversation_id = "conv-history-ocr"
        await self._save_conversation_with_messages(conversation_id)
        await self._save_json_artifact(
            conversation_id,
            f"{conversation_id}:ocr_raw_text:1",
            {
                "artifact_role": "ocr_raw_text",
                "domain_kind": "ocr",
                "raw_text": "品种：龙粳33\n处理：A1",
                "filename": "scan.png",
            },
            summary="OCR 回传原文：scan.png",
        )
        await self._save_json_artifact(
            conversation_id,
            f"{conversation_id}:ocr_raw_text:empty",
            {"artifact_role": "ocr_raw_text", "domain_kind": "ocr", "raw_text": "   "},
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        assistant_message = next(message for message in response.json()["messages"] if message["role"] == "assistant")
        self.assertEqual([artifact["artifact_id"] for artifact in assistant_message["artifacts"]], [f"{conversation_id}:ocr_raw_text:1"])
        payload = json.loads(assistant_message["artifacts"][0]["storage_ref"])
        self.assertEqual(payload["raw_text"], "品种：龙粳33\n处理：A1")

    async def test_user_and_taskless_assistant_messages_have_empty_artifacts(self) -> None:
        conversation_id = "conv-history-empty-boundary"
        await self.runtime.storage.save_conversation(Conversation(conversation_id, "acc-1"))
        await self.runtime.storage.save_message(Message("msg-user", conversation_id, MessageRole.USER, "用户", created_at=datetime(2026, 6, 3, 1, 0, 0)))
        await self.runtime.storage.save_message(Message("msg-assistant", conversation_id, MessageRole.ASSISTANT, "无任务回答", stream_status="complete", created_at=datetime(2026, 6, 3, 1, 0, 1)))

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([message["artifacts"] for message in response.json()["messages"]], [[], []])

    async def test_conversation_messages_public_allowlist_includes_file_upload_and_hides_internal_system(self) -> None:
        conversation_id = "conv-history-public-allowlist"
        updated_at = datetime(2026, 6, 3, 1, 0, 3)
        await self.runtime.storage.save_conversation(Conversation(conversation_id, "acc-1"))
        await self.runtime.storage.save_message(
            Message("msg-user", conversation_id, MessageRole.USER, "用户", created_at=datetime(2026, 6, 3, 1, 0, 0))
        )
        await self.runtime.storage.save_message(
            Message("msg-system-chat", conversation_id, MessageRole.SYSTEM, "内部系统消息", created_at=datetime(2026, 6, 3, 1, 0, 1))
        )
        await self.runtime.storage.save_message(
            Message(
                "msg-system-internal",
                conversation_id,
                MessageRole.SYSTEM,
                "未知内部消息",
                created_at=datetime(2026, 6, 3, 1, 0, 2),
                message_type="internal",
            )
        )
        await self.runtime.storage.save_message(
            Message(
                file_upload_message_id("upl-123abc456def"),
                conversation_id,
                MessageRole.SYSTEM,
                "文件上传：materials.csv",
                stream_status="complete",
                created_at=updated_at,
                message_type=FILE_UPLOAD_MESSAGE_TYPE,
                metadata={
                    "schema_version": 1,
                    "upload_id": "upl-123abc456def",
                    "filename": "materials.csv",
                    "file_status": "active",
                },
                updated_at=updated_at,
            )
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        messages = response.json()["messages"]
        self.assertEqual([message["message_id"] for message in messages], ["msg-user", "file_upload:upl-123abc456def"])
        file_message = messages[1]
        self.assertEqual(file_message["role"], "system")
        self.assertEqual(file_message["message_type"], FILE_UPLOAD_MESSAGE_TYPE)
        self.assertEqual(file_message["metadata"]["upload_id"], "upl-123abc456def")
        self.assertEqual(file_message["updated_at"], "2026-06-03T01:00:03")
        self.assertEqual(file_message["artifacts"], [])

    async def test_conversation_file_upload_history_metadata_is_sanitized_at_public_api_boundary(self) -> None:
        conversation_id = "conv-history-file-upload-sanitize"
        await self.runtime.storage.save_conversation(Conversation(conversation_id, "acc-1"))
        await self.runtime.storage.save_message(
            Message(
                file_upload_message_id("upl-123abc456def"),
                conversation_id,
                MessageRole.SYSTEM,
                "文件上传：materials.csv",
                stream_status="complete",
                created_at=datetime(2026, 6, 3, 1, 0, 0),
                message_type=FILE_UPLOAD_MESSAGE_TYPE,
                metadata={
                    "upload_id": "wrong-id",
                    "filename": "materials.csv",
                    "file_status": "active",
                    "storage_key": "/tmp/secret/materials.csv",
                    "content": "raw file body",
                    "content_base64": "abc",
                },
            )
        )
        await self.runtime.storage.save_message(
            Message(
                "bad-file-upload-id",
                conversation_id,
                MessageRole.SYSTEM,
                "bad id",
                created_at=datetime(2026, 6, 3, 1, 0, 1),
                message_type=FILE_UPLOAD_MESSAGE_TYPE,
                metadata={"upload_id": "upl-bad"},
            )
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        messages = response.json()["messages"]
        self.assertEqual([message["message_id"] for message in messages], ["file_upload:upl-123abc456def"])
        metadata = messages[0]["metadata"]
        self.assertEqual(metadata["upload_id"], "upl-123abc456def")
        self.assertEqual(metadata["filename"], "materials.csv")
        self.assertNotIn("storage_key", metadata)
        self.assertNotIn("content", metadata)
        self.assertNotIn("content_base64", metadata)

    async def test_conversation_messages_backfill_interrupt_questions_without_artifact_duplication(self) -> None:
        conversation_id = "conv-history-interrupt-visible"
        task_id = f"{conversation_id}:task-1"
        await self.runtime.storage.save_conversation(Conversation(conversation_id, "acc-1"))
        await self.runtime.storage.save_task(
            Task(
                task_id,
                conversation_id,
                root_message_id="msg-user",
                status=TaskStatus.COMPLETED,
                created_at=datetime(2026, 6, 3, 1, 0, 0),
            )
        )
        await self.runtime.storage.save_message(
            Message(
                "msg-user",
                conversation_id,
                MessageRole.USER,
                "帮我设计 RCBD",
                task_id=task_id,
                created_at=datetime(2026, 6, 3, 1, 0, 0),
            )
        )
        await self.runtime.storage.save_interrupt(
            Interrupt(
                "interrupt-1",
                conversation_id,
                task_id,
                node_id="node-slot",
                source_agent="skill.field_design",
                source_message_id="msg-user",
                question="请提供试验的区组数（重复次数）。",
                reason_code="missing_blocks",
                created_at=datetime(2026, 6, 3, 1, 0, 1),
            )
        )
        await self.runtime.storage.save_message(
            Message(
                f"{task_id}:assistant",
                conversation_id,
                MessageRole.ASSISTANT,
                "最终回答文本",
                task_id=task_id,
                stream_status="complete",
                created_at=datetime(2026, 6, 3, 1, 0, 3),
            )
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                "art-final",
                task_id,
                "node-final",
                ArtifactType.JSON,
                json.dumps({"artifact_role": "filtered_query_result", "columns": ["设计"], "rows": [{"设计": "RCBD"}], "row_count": 1}),
                is_complete=True,
                created_at=datetime(2026, 6, 3, 1, 0, 4),
            )
        )

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        assistant_messages = [message for message in response.json()["messages"] if message["role"] == "assistant"]
        interrupt_message = next(message for message in assistant_messages if message["stream_status"] == "interrupt_visible")
        final_message = next(message for message in assistant_messages if message["stream_status"] == "complete")
        self.assertEqual(interrupt_message["content"], "请提供试验的区组数（重复次数）。")
        self.assertEqual(interrupt_message["artifacts"], [])
        self.assertEqual([artifact["artifact_id"] for artifact in final_message["artifacts"]], ["art-final"])

    async def test_conversation_artifacts_are_grouped_by_message_task_id_without_task_n_plus_one(self) -> None:
        conversation_id = "conv-history-grouping"
        await self.runtime.storage.save_conversation(Conversation(conversation_id, "acc-1"))
        for index in (1, 2):
            task_id = f"{conversation_id}:task-{index}"
            await self.runtime.storage.save_task(Task(task_id, conversation_id, root_message_id=f"msg-user-{index}", status=TaskStatus.COMPLETED, created_at=datetime(2026, 6, 3, 1, index, 0)))
            await self.runtime.storage.save_message(Message(f"msg-assistant-{index}", conversation_id, MessageRole.ASSISTANT, f"回答 {index}", task_id=task_id, stream_status="complete", created_at=datetime(2026, 6, 3, 1, index, 1)))
            await self.runtime.storage.save_artifact(
                Artifact(
                    f"{conversation_id}:filtered_query_result:{index}",
                    task_id,
                    f"{conversation_id}:skill:{index}",
                    ArtifactType.JSON,
                    json.dumps({"artifact_role": "filtered_query_result", "columns": ["idx"], "rows": [{"idx": index}], "row_count": 1}),
                    is_complete=True,
                    created_at=datetime(2026, 6, 3, 1, index, 2),
                )
            )

        original_list_for_conversation = self.runtime.storage.list_artifacts_for_conversation
        original_list_for_task = self.runtime.storage.list_artifacts_for_task
        calls = {"conversation": 0, "task": 0}

        @wraps(original_list_for_conversation)
        async def counted_list_for_conversation(cid: str):
            calls["conversation"] += 1
            return await original_list_for_conversation(cid)

        @wraps(original_list_for_task)
        async def counted_list_for_task(task_id: str):
            calls["task"] += 1
            return await original_list_for_task(task_id)

        self.runtime.storage.list_artifacts_for_conversation = counted_list_for_conversation
        self.runtime.storage.list_artifacts_for_task = counted_list_for_task

        response = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")

        self.assertEqual(response.status_code, 200)
        assistant_messages = [message for message in response.json()["messages"] if message["role"] == "assistant"]
        self.assertEqual([message["artifacts"][0]["artifact_id"] for message in assistant_messages], [
            f"{conversation_id}:filtered_query_result:1",
            f"{conversation_id}:filtered_query_result:2",
        ])
        self.assertEqual(calls, {"conversation": 1, "task": 0})

    async def test_task_artifacts_endpoint_still_returns_non_file_artifacts(self) -> None:
        conversation_id = "conv-history-task-compat"
        await self._save_conversation_with_messages(conversation_id)
        await self._save_json_artifact(conversation_id, f"{conversation_id}:generated_sql:1", {"artifact_role": "generated_sql", "sql": "SELECT 1"})
        await self._save_json_artifact(conversation_id, f"{conversation_id}:filtered_query_result:1", {"artifact_role": "filtered_query_result", "columns": ["x"], "rows": [], "row_count": 0})

        response = await self.client.get(f"/api/v1/tasks/{conversation_id}:task-1/artifacts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [artifact["artifact_id"] for artifact in response.json()["artifacts"]],
            [f"{conversation_id}:filtered_query_result:1", f"{conversation_id}:generated_sql:1"],
        )
