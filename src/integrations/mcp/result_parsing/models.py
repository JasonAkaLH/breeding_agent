from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, TypeAlias


JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class MCPResultSource(StrEnum):
    TOOLS_CALL = "tools_call"
    TASKS_RESULT = "tasks_result"
    TASKS_GET = "tasks_get"


class MCPResultOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    TOOL_ERROR = "tool_error"


class MCPStructuredSchemaStatus(StrEnum):
    NOT_SUPPORTED_BY_VERSION = "not_supported_by_version"
    NOT_DECLARED = "not_declared"
    VALID = "valid"
    UNAVAILABLE_LEGACY = "unavailable_legacy"


class MCPResultDiagnostic(StrEnum):
    LEGACY_OUTPUT_SCHEMA_UNAVAILABLE = "legacy_output_schema_unavailable"
    LEGACY_MISSING_RESULT_TYPE = "legacy_missing_result_type"
    STRUCTURED_TEXT_DUPLICATE = "structured_text_duplicate"
    USER_PROJECTION_TRUNCATED = "user_projection_truncated"
    AGENT_PROJECTION_TRUNCATED = "agent_projection_truncated"


@dataclass(frozen=True, slots=True)
class MCPStructuredContent:
    present: bool
    value: JSONValue = None
    schema_status: MCPStructuredSchemaStatus = MCPStructuredSchemaStatus.NOT_DECLARED


@dataclass(frozen=True, slots=True)
class MCPTextResultBlock:
    text: str
    audience: tuple[str, ...] = ()
    priority: float | None = None
    kind: str = "text"


@dataclass(frozen=True, slots=True)
class MCPImageResultBlock:
    mime_type: str
    byte_size: int
    sha256: str
    audience: tuple[str, ...] = ()
    priority: float | None = None
    kind: str = "image"


@dataclass(frozen=True, slots=True)
class MCPAudioResultBlock:
    mime_type: str
    byte_size: int
    sha256: str
    audience: tuple[str, ...] = ()
    priority: float | None = None
    kind: str = "audio"


@dataclass(frozen=True, slots=True)
class MCPResourceLinkResultBlock:
    name: str
    uri_scheme: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    kind: str = "resource_link"


@dataclass(frozen=True, slots=True)
class MCPEmbeddedTextResourceBlock:
    uri_scheme: str
    text: str
    mime_type: str | None = None
    kind: str = "embedded_text_resource"


@dataclass(frozen=True, slots=True)
class MCPEmbeddedBlobResourceBlock:
    uri_scheme: str
    mime_type: str
    byte_size: int
    sha256: str
    kind: str = "embedded_blob_resource"


MCPResultBlock: TypeAlias = (
    MCPTextResultBlock
    | MCPImageResultBlock
    | MCPAudioResultBlock
    | MCPResourceLinkResultBlock
    | MCPEmbeddedTextResourceBlock
    | MCPEmbeddedBlobResourceBlock
)


@dataclass(frozen=True, slots=True)
class MCPParsedToolResult:
    protocol_version: str
    source: MCPResultSource
    outcome: MCPResultOutcome
    structured_content: MCPStructuredContent
    content_blocks: tuple[MCPResultBlock, ...]
    safe_error_code: str | None
    diagnostics: tuple[MCPResultDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class MCPResultDecodeRequest:
    protocol_version: str
    source: MCPResultSource | str
    payload: Mapping[str, Any] | bytes | str
    output_schema: Mapping[str, Any] | None = None
    output_schema_sha256: str | None = None
    historical_compatibility: bool = False
