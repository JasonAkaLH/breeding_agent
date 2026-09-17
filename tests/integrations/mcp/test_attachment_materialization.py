from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import unittest

from src.core.models import TaskInputAttachment
from src.integrations.mcp.attachment_materialization import (
    MCPAttachmentMaterializationError,
    MCPJobWorkflowKind,
    identify_mcp_job_workflow,
    materialize_mcp_attachment_action,
)
from src.integrations.mcp.gateway_models import (
    MCPToolDescriptor,
    ToolCatalogSnapshot,
)


def _catalog() -> ToolCatalogSnapshot:
    source_schema = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "type": {"const": "base64"},
                    "data": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "filename": {"type": "string"},
                    "sha256": {"type": "string"},
                },
                "required": ["type", "data", "mime_type"],
                "additionalProperties": False,
            }
        ]
    }
    tools = []
    for name in (
        "get_ocr_capabilities",
        "start_parse_job",
        "get_parse_job",
        "ack_parse_job",
        "cancel_parse_job",
    ):
        schema = (
            {
                "type": "object",
                "properties": {"source": source_schema},
                "required": ["source"],
                "additionalProperties": False,
            }
            if name == "start_parse_job"
            else {"type": "object"}
        )
        tools.append(
            MCPToolDescriptor(
                name=name,
                description=name,
                input_schema=schema,
                input_schema_sha256=f"schema-{name}",
            )
        )
    return ToolCatalogSnapshot("ocr-server", "2025-11-25", tuple(tools))


def _attachment(content: bytes, *, content_type: str = "image/png") -> TaskInputAttachment:
    digest = hashlib.sha256(content).hexdigest()
    return TaskInputAttachment(
        attachment_id="attachment-1",
        task_id="task-1",
        conversation_id="conversation-1",
        source_kind="message_upload",
        source_upload_id="upload-1",
        source_message_id="message-1",
        filename="newspaper.png",
        content_type=content_type,
        file_type="image",
        size_bytes=len(content),
        sha256=digest,
        source_payload={
            "encoding": "base64",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "filename": "newspaper.png",
            "content_type": content_type,
        },
    )


class MCPAttachmentMaterializationTests(unittest.TestCase):
    def test_materializes_realistic_png_without_exposing_internal_attachment_id(self) -> None:
        content = b"\x89PNG\r\n\x1a\n" + b"x" * 2_326_763
        result = materialize_mcp_attachment_action(
            catalog=_catalog(),
            tool_name="start_parse_job",
            arguments={
                "source": {"file_path": "newspaper.png"},
                "result_format": "markdown",
                "return_markdown": True,
            },
            attachments=(_attachment(content),),
            explicit_binding=True,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.workflow_kind, MCPJobWorkflowKind.OCR_ASYNC_JOB_V1)
        self.assertEqual(result.arguments["result_format"], "both")
        source = result.arguments["source"]
        self.assertEqual(source["type"], "base64")
        self.assertEqual(base64.b64decode(source["data"], validate=True), content)
        self.assertEqual(source["mime_type"], "image/png")
        self.assertEqual(source["filename"], "newspaper.png")
        self.assertEqual(source["sha256"], hashlib.sha256(content).hexdigest())
        self.assertNotIn("attachment-1", repr(result.arguments))
        self.assertLess(len(source["data"]), 32 * 1024 * 1024)

    def test_materialization_is_idempotent_for_approved_resume_payload(self) -> None:
        content = b"\x89PNG\r\n\x1a\nbody"
        first = materialize_mcp_attachment_action(
            catalog=_catalog(),
            tool_name="start_parse_job",
            arguments={},
            attachments=(_attachment(content),),
            explicit_binding=True,
        )
        assert first is not None
        second = materialize_mcp_attachment_action(
            catalog=_catalog(),
            tool_name="start_parse_job",
            arguments=first.arguments,
            attachments=(_attachment(content),),
            explicit_binding=True,
        )
        self.assertEqual(second, first)

    def test_non_ocr_or_automatic_routes_are_not_materialized(self) -> None:
        content = b"\x89PNG\r\n\x1a\nbody"
        self.assertIsNone(
            materialize_mcp_attachment_action(
                catalog=_catalog(),
                tool_name="get_ocr_capabilities",
                arguments={},
                attachments=(_attachment(content),),
                explicit_binding=True,
            )
        )

    def test_identifies_only_an_already_materialized_exact_ocr_action(self) -> None:
        content = b"\x89PNG\r\n\x1a\nbody"
        materialized = materialize_mcp_attachment_action(
            catalog=_catalog(),
            tool_name="start_parse_job",
            arguments={},
            attachments=(_attachment(content),),
            explicit_binding=True,
        )
        assert materialized is not None

        self.assertEqual(
            identify_mcp_job_workflow(
                catalog=_catalog(),
                tool_name="start_parse_job",
                arguments=materialized.arguments,
            ),
            MCPJobWorkflowKind.OCR_ASYNC_JOB_V1,
        )
        self.assertIsNone(
            identify_mcp_job_workflow(
                catalog=_catalog(),
                tool_name="start_parse_job",
                arguments={"source": {"file_path": "newspaper.png"}},
            )
        )
        self.assertIsNone(
            materialize_mcp_attachment_action(
                catalog=_catalog(),
                tool_name="start_parse_job",
                arguments={},
                attachments=(_attachment(content),),
                explicit_binding=False,
            )
        )
        self.assertIsNone(
            materialize_mcp_attachment_action(
                catalog=_catalog(),
                tool_name="start_parse_job",
                arguments={"source": {"type": "url", "url": "https://example.com/a.png"}},
                attachments=(),
                explicit_binding=True,
            )
        )

    def test_rejects_multiple_oversized_or_corrupt_attachments(self) -> None:
        content = b"\x89PNG\r\n\x1a\nbody"
        with self.assertRaisesRegex(
            MCPAttachmentMaterializationError,
            "mcp_attachment_materialization_ambiguous",
        ):
            materialize_mcp_attachment_action(
                catalog=_catalog(),
                tool_name="start_parse_job",
                arguments={},
                attachments=(_attachment(content), _attachment(content)),
                explicit_binding=True,
            )

        oversized = _attachment(b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024))
        with self.assertRaisesRegex(
            MCPAttachmentMaterializationError,
            "mcp_attachment_materialization_too_large",
        ):
            materialize_mcp_attachment_action(
                catalog=_catalog(),
                tool_name="start_parse_job",
                arguments={},
                attachments=(oversized,),
                explicit_binding=True,
            )

        corrupt = replace(_attachment(content), sha256="0" * 64)
        with self.assertRaisesRegex(
            MCPAttachmentMaterializationError,
            "mcp_attachment_materialization_integrity_conflict",
        ):
            materialize_mcp_attachment_action(
                catalog=_catalog(),
                tool_name="start_parse_job",
                arguments={},
                attachments=(corrupt,),
                explicit_binding=True,
            )
