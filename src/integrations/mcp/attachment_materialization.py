from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from types import MappingProxyType
from typing import Any

from src.core.models import TaskInputAttachment

from .gateway_models import ToolCatalogSnapshot


MAX_OCR_BASE64_SOURCE_BYTES = 10 * 1024 * 1024
_OCR_WORKFLOW_TOOLS = frozenset(
    {
        "get_ocr_capabilities",
        "start_parse_job",
        "get_parse_job",
        "ack_parse_job",
        "cancel_parse_job",
    }
)
_SUPPORTED_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "application/pdf"}
)


class MCPJobWorkflowKind(StrEnum):
    OCR_ASYNC_JOB_V1 = "ocr_async_job_v1"


class MCPAttachmentMaterializationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MCPMaterializedAttachmentAction:
    arguments: Mapping[str, Any]
    workflow_kind: MCPJobWorkflowKind

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


def materialize_mcp_attachment_action(
    *,
    catalog: ToolCatalogSnapshot,
    tool_name: str,
    arguments: Mapping[str, Any],
    attachments: Sequence[TaskInputAttachment],
    explicit_binding: bool,
) -> MCPMaterializedAttachmentAction | None:
    if not explicit_binding or tool_name != "start_parse_job":
        return None
    if not _is_ocr_workflow_catalog(catalog):
        return None
    descriptor = catalog.get(tool_name)
    if descriptor is None or not _has_source_object(descriptor.input_schema):
        return None
    if not attachments:
        return None
    if len(attachments) != 1:
        raise MCPAttachmentMaterializationError(
            "mcp_attachment_materialization_ambiguous"
        )
    attachment = attachments[0]
    content_type = str(attachment.content_type or "").strip().lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type not in _SUPPORTED_CONTENT_TYPES:
        raise MCPAttachmentMaterializationError(
            "mcp_attachment_materialization_unsupported_type"
        )
    source_payload = attachment.source_payload
    encoded = source_payload.get("content_base64")
    if (
        source_payload.get("encoding") != "base64"
        or not isinstance(encoded, str)
        or not encoded
    ):
        raise MCPAttachmentMaterializationError(
            "mcp_attachment_materialization_content_unavailable"
        )
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise MCPAttachmentMaterializationError(
            "mcp_attachment_materialization_content_invalid"
        ) from exc
    if len(content) > MAX_OCR_BASE64_SOURCE_BYTES:
        raise MCPAttachmentMaterializationError(
            "mcp_attachment_materialization_too_large"
        )
    expected_size = int(attachment.size_bytes or 0)
    expected_sha256 = str(attachment.sha256 or "").removeprefix("sha256:")
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if (
        expected_size != len(content)
        or len(expected_sha256) != 64
        or expected_sha256 != actual_sha256
    ):
        raise MCPAttachmentMaterializationError(
            "mcp_attachment_materialization_integrity_conflict"
        )
    filename = _safe_basename(attachment.filename)
    materialized: dict[str, Any] = {
        "source": {
            "type": "base64",
            "data": encoded,
            "mime_type": content_type,
            "filename": filename,
            "sha256": actual_sha256,
        },
        "result_format": "both",
        "return_markdown": True,
    }
    pages = arguments.get("pages")
    if isinstance(pages, str) or (
        isinstance(pages, list)
        and pages
        and all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in pages)
    ):
        materialized["pages"] = pages
    return MCPMaterializedAttachmentAction(
        arguments=materialized,
        workflow_kind=MCPJobWorkflowKind.OCR_ASYNC_JOB_V1,
    )


def identify_mcp_job_workflow(
    *,
    catalog: ToolCatalogSnapshot,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> MCPJobWorkflowKind | None:
    if tool_name != "start_parse_job" or not _is_ocr_workflow_catalog(catalog):
        return None
    descriptor = catalog.get(tool_name)
    source = arguments.get("source")
    if (
        descriptor is None
        or not _has_source_object(descriptor.input_schema)
        or not isinstance(source, Mapping)
        or source.get("type") != "base64"
        or not isinstance(source.get("data"), str)
        or not source.get("data")
        or not isinstance(source.get("mime_type"), str)
        or not source.get("mime_type")
    ):
        return None
    return MCPJobWorkflowKind.OCR_ASYNC_JOB_V1


def _is_ocr_workflow_catalog(catalog: ToolCatalogSnapshot) -> bool:
    return {tool.name for tool in catalog.tools} == _OCR_WORKFLOW_TOOLS


def _has_source_object(schema: Mapping[str, Any]) -> bool:
    properties = schema.get("properties")
    return isinstance(properties, Mapping) and isinstance(
        properties.get("source"), Mapping
    )


def _safe_basename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/")
    basename = PurePath(normalized).name
    basename = "".join(
        char
        for char in basename
        if not (ord(char) < 32 or 127 <= ord(char) <= 159)
    ).strip()
    if not basename:
        raise MCPAttachmentMaterializationError(
            "mcp_attachment_materialization_filename_invalid"
        )
    encoded = basename.encode("utf-8")
    if len(encoded) > 255:
        encoded = encoded[:255]
        while True:
            try:
                basename = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
    return basename


__all__ = [
    "MAX_OCR_BASE64_SOURCE_BYTES",
    "MCPAttachmentMaterializationError",
    "MCPJobWorkflowKind",
    "MCPMaterializedAttachmentAction",
    "identify_mcp_job_workflow",
    "materialize_mcp_attachment_action",
]
