from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator, model_validator

from .mcp_binding import (
    MCP_BINDING_REQUEST_ALLOWED_METADATA_KEYS,
    MCP_SERVER_BINDING_METADATA_KEY,
    normalize_mcp_server_id,
)


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")
_RESERVED_IDENTITY_KEY_SEGMENTS = frozenset({
    "account",
    "auth",
    "authorization",
    "bearer",
    "captcha",
    "identity",
    "owner",
    "password",
    "session",
    "token",
    "user",
    "username",
})
_RESERVED_IDENTITY_KEY_COMPACT = frozenset({
    "accountid",
    "accesstoken",
    "apikey",
    "apitoken",
    "authentication",
    "authorization",
    "authtoken",
    "bearertoken",
    "captchaid",
    "captchacode",
    "identity",
    "ownerusername",
    "passwordhash",
    "refreshtoken",
    "sessionid",
    "userid",
    "username",
})


def _identity_key_parts(key: object) -> tuple[tuple[str, ...], str]:
    key_text = str(key).strip()
    separated = _CAMEL_CASE_BOUNDARY.sub("_", key_text)
    normalized = _KEY_SEPARATOR.sub("_", separated.lower()).strip("_")
    parts = tuple(part for part in normalized.split("_") if part)
    compact = "".join(parts)
    return parts, compact


def is_reserved_identity_key(key: object) -> bool:
    parts, compact = _identity_key_parts(key)
    return compact in _RESERVED_IDENTITY_KEY_COMPACT or any(
        part in _RESERVED_IDENTITY_KEY_SEGMENTS for part in parts
    )


def _reserved_identity_paths(value: Any, *, path: str = "payload") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if is_reserved_identity_key(key_text):
                paths.append(child_path)
            paths.extend(_reserved_identity_paths(child, path=child_path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_reserved_identity_paths(child, path=f"{path}[{index}]"))
        return paths
    return []


def _reject_reserved_identity_fields(value: dict[str, Any], *, field_name: str) -> dict[str, Any]:
    forbidden = _reserved_identity_paths(value, path=field_name)
    if forbidden:
        raise ValueError(f"{field_name} contains reserved identity fields: {', '.join(sorted(forbidden))}")
    return value


class MCPServerBindingRequest(StrictRequestModel):
    server_id: str

    @field_validator("server_id")
    @classmethod
    def normalize_server_id(cls, value: str) -> str:
        return normalize_mcp_server_id(value)


class SubmitMessageRequest(StrictRequestModel):
    conversation_id: str
    content: str
    routing_mode: str = "auto"
    capability_id: str | None = None
    client_message_id: str | None = None
    model_edition: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def reject_identity_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_reserved_identity_fields(value, field_name="metadata")

    @field_validator("model_edition")
    @classmethod
    def normalize_model_edition(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_routing_shape(self) -> "SubmitMessageRequest":
        if self.routing_mode not in {"auto", "hint", "force_capability"}:
            raise ValueError("routing_mode must be auto, hint, or force_capability")
        has_capability = bool((self.capability_id or "").strip())
        if self.routing_mode == "auto" and has_capability:
            raise ValueError("routing_mode=auto does not accept capability_id")
        if self.routing_mode in {"hint", "force_capability"} and not has_capability:
            raise ValueError(f"routing_mode={self.routing_mode} requires capability_id")
        return self

    @model_validator(mode="after")
    def validate_mcp_server_binding(self) -> "SubmitMessageRequest":
        raw_binding = self.metadata.get(MCP_SERVER_BINDING_METADATA_KEY)
        forced_mcp_dispatch = (
            self.routing_mode == "force_capability"
            and self.capability_id == "mcp.dispatch"
        )
        if raw_binding is None:
            if forced_mcp_dispatch:
                raise ValueError("force_capability mcp.dispatch requires metadata.mcp_server_binding")
            return self
        if not forced_mcp_dispatch:
            raise ValueError(
                "metadata.mcp_server_binding requires routing_mode=force_capability and capability_id=mcp.dispatch"
            )
        unknown = set(self.metadata) - MCP_BINDING_REQUEST_ALLOWED_METADATA_KEYS
        if unknown:
            raise ValueError(f"mcp_server_binding metadata contains unknown fields: {sorted(unknown)}")
        binding = MCPServerBindingRequest.model_validate(raw_binding)
        self.metadata[MCP_SERVER_BINDING_METADATA_KEY] = binding.model_dump()
        return self


class ReasoningEffortOptionResponse(BaseModel):
    value: str
    label: str


class ReasoningEffortStatePolicyResponse(BaseModel):
    default: str | None
    supported: list[str]


class ReasoningEffortThinkingPolicyResponse(BaseModel):
    enabled: ReasoningEffortStatePolicyResponse
    disabled: ReasoningEffortStatePolicyResponse


class ReasoningEffortConfigResponse(BaseModel):
    options: list[ReasoningEffortOptionResponse]
    thinking: ReasoningEffortThinkingPolicyResponse


class ModelEditionOptionResponse(BaseModel):
    value: str
    label: str
    reasoning_efforts: ReasoningEffortConfigResponse


class ModelEditionsResponse(BaseModel):
    default_model_edition: str | None
    options: list[ModelEditionOptionResponse]


class LoginRequest(StrictRequestModel):
    username: str


class UserResponse(BaseModel):
    username: str


class AuthUserResponse(BaseModel):
    user: UserResponse


class AuthTokenResponse(AuthUserResponse):
    access_token: str


class LogoutResponse(BaseModel):
    logged_out: bool


class MessageAcceptedResponse(BaseModel):
    conversation_id: str
    message_id: str
    task_id: str
    status: str
    action: str | None = None
    interrupt_id: str | None = None
    assistant_message: str | None = None
    answer_payload: dict[str, Any] | None = None


class UploadPreviewResponse(BaseModel):
    row_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    shape: str | None = None
    source_encoding: str | None = None
    original_columns: list[str] = Field(default_factory=list)
    column_normalizations: list[dict[str, Any]] = Field(default_factory=list)
    column_count: int | None = None
    columns_truncated: bool | None = None
    column_normalization_count: int | None = None
    column_normalizations_truncated: bool | None = None
    normalized_content_type: str | None = None
    char_count: int | None = None
    line_count: int | None = None
    size_bytes: int | None = None
    file_type: str | None = None
    requires_sheet_selection: bool | None = None
    selected_sheet: str | None = None
    excel_sheets: list[dict[str, Any]] = Field(default_factory=list)
    excel_sheet_count: int | None = None
    excel_sheets_truncated: bool | None = None


class UploadFileResponse(BaseModel):
    upload_id: str
    conversation_id: str
    filename: str
    content_type: str
    file_type: str
    size_bytes: int
    sha256: str
    expires_at: datetime
    status: str | None = None
    description_status: str | None = None
    preview: UploadPreviewResponse


class UploadListResponse(BaseModel):
    conversation_id: str
    uploads: list[UploadFileResponse]
    next_cursor: str | None = None


class DeleteUploadResponse(BaseModel):
    upload_id: str
    deleted: bool


class DeleteUploadRequest(StrictRequestModel):
    conversation_id: str
    upload_id: str


class MCPResultArtifactProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["maf.user_mcp.result_artifact_projection.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    safe_call_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready", "deferred", "permanent_failure"]
    reason_code: Literal[
        "promoted",
        "already_promoted",
        "capacity_unavailable",
        "projection_failed",
        "source_expired",
    ]
    artifact_count: Literal[0, 1]

    @model_validator(mode="after")
    def validate_projection_state(self):
        allowed = {
            "ready": {"promoted", "already_promoted"},
            "deferred": {"capacity_unavailable", "projection_failed"},
            "permanent_failure": {"projection_failed", "source_expired"},
        }
        if self.reason_code not in allowed[self.status]:
            raise ValueError("mcp_result_artifact_projection_reason_invalid")
        if (self.status == "ready") != (self.artifact_count == 1):
            raise ValueError("mcp_result_artifact_projection_count_invalid")
        return self


class TaskSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    conversation_id: str
    status: str
    root_node_id: str | None
    summary: str | None
    requested_capability_id: str | None
    active_node_count: int
    completed_node_count: int
    failed_node_count: int
    cancel_requested: bool
    created_at: datetime | None
    updated_at: datetime | None
    mcp_terminal_projection: dict[str, Any] | None = None
    mcp_result_artifact_projections: list[
        MCPResultArtifactProjectionResponse
    ] = Field(default_factory=list, max_length=20)


class TaskListResponse(BaseModel):
    conversation_id: str
    tasks: list[TaskSummaryResponse]


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    username: str
    status: str
    current_task_id: str | None
    title: str | None
    created_at: datetime | None
    updated_at: datetime | None


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummaryResponse]


class RenameConversationRequest(StrictRequestModel):
    conversation_id: str
    title: str


class DeleteConversationResponse(BaseModel):
    conversation_id: str
    deleted: bool
    cancelled_task_ids: list[str] = Field(default_factory=list)
    deleted_counts: dict[str, int] = Field(default_factory=dict)
    delete_status: str = "completed"
    runner_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None


class DeleteConversationRequest(StrictRequestModel):
    conversation_id: str


class MCPStructuredResultPrimary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["structured"]
    value: JsonValue
    truncated: Literal[False] = False

    @field_validator("value", mode="before")
    @classmethod
    def validate_strict_json_value(cls, value: object) -> object:
        pending: list[tuple[object, int]] = [(value, 0)]
        nodes = 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 64 or nodes > 100_000:
                raise ValueError("structured MCP result exceeds JSON limits")
            if item is None or isinstance(item, (str, bool)):
                continue
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueError("structured MCP result contains non-finite number")
                continue
            if isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
                continue
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str) or len(key) > 1_024:
                        raise ValueError("structured MCP result contains invalid key")
                    pending.append((child, depth + 1))
                continue
            raise ValueError("structured MCP result contains non-JSON value")
        return value


class MCPStructuredPreviewResultPrimary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["structured_preview"]
    preview: str
    truncated: Literal[True] = True


class MCPTextResultPrimary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"]
    text: str
    truncated: bool


class MCPEmptyResultPrimary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["empty"]
    message: str
    truncated: Literal[False] = False


MCPBusinessResultPrimary = Annotated[
    MCPStructuredResultPrimary
    | MCPStructuredPreviewResultPrimary
    | MCPTextResultPrimary
    | MCPEmptyResultPrimary,
    Field(discriminator="kind"),
]


class MCPBinaryResultMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["image", "audio", "embedded_blob_resource"]
    mime_type: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MCPResourceLinkResultMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["resource_link"]
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    uri_scheme: str = Field(pattern=r"^[a-z][a-z0-9+.-]{0,31}$")


class MCPEmbeddedTextResourceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["embedded_text_resource"]
    mime_type: str | None = None
    uri_scheme: str = Field(pattern=r"^[a-z][a-z0-9+.-]{0,31}$")


MCPBusinessResultContentMetadata = Annotated[
    MCPBinaryResultMetadata
    | MCPResourceLinkResultMetadata
    | MCPEmbeddedTextResourceMetadata,
    Field(discriminator="kind"),
]


class MCPBusinessResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["maf.mcp.business_result_view.v1"] = Field(alias="schema")
    availability: Literal["ready", "unavailable"]
    outcome: Literal["succeeded"] = "succeeded"
    primary: MCPBusinessResultPrimary | None = None
    unavailable_reason: Literal[
        "safe_hide",
        "projection_missing",
        "historical_authority_invalid",
        "projection_invalid",
    ] | None = None
    supplemental_texts: list[str] | None = None
    content_metadata: list[MCPBusinessResultContentMetadata] | None = None
    projection_truncated: bool = False

    @model_validator(mode="after")
    def validate_availability_shape(self) -> "MCPBusinessResultView":
        if self.availability == "ready":
            if self.primary is None or self.unavailable_reason is not None:
                raise ValueError("ready MCP result must contain exactly one primary")
        elif (
            self.primary is not None
            or self.unavailable_reason is None
            or self.supplemental_texts is not None
            or self.content_metadata is not None
            or self.projection_truncated
        ):
            raise ValueError("unavailable MCP result contains display content")
        encoded = self.model_dump_json(by_alias=True).encode("utf-8")
        if len(encoded.decode("utf-8")) > 20_000 or len(encoded) > 80_000:
            raise ValueError("MCP result view exceeds public projection budget")
        return self


class ArtifactResponse(BaseModel):
    artifact_id: str
    producer_node_id: str
    artifact_type: str
    storage_ref: str
    summary: str | None
    is_complete: bool
    created_at: datetime | None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    download_url: str | None = None
    source_file_count: int | None = None
    archive_format: str | None = None
    retention_status: str | None = None
    mcp_business_result: MCPBusinessResultView | None = None


class MessageResponse(BaseModel):
    message_id: str
    conversation_id: str
    role: str
    content: str
    task_id: str | None
    stream_status: str | None
    created_at: datetime | None
    message_type: str = "chat"
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None
    artifacts: list[ArtifactResponse] = Field(default_factory=list)
    mcp_result_artifact_projections: list[
        MCPResultArtifactProjectionResponse
    ] = Field(default_factory=list, max_length=20)


class ConversationMessagesResponse(BaseModel):
    conversation_id: str
    messages: list[MessageResponse]


class TaskNodeResponse(BaseModel):
    node_id: str
    capability_id: str
    status: str
    criticality: str
    dependency_type: str
    assigned_instance_id: str | None
    started_at: datetime | None
    finished_at: datetime | None


class TaskEdgeResponse(BaseModel):
    from_node_id: str
    to_node_id: str
    edge_type: str
    condition: str | None


class TaskGraphResponse(BaseModel):
    task_id: str
    nodes: list[TaskNodeResponse]
    edges: list[TaskEdgeResponse]


class TaskArtifactsResponse(BaseModel):
    task_id: str
    artifacts: list[ArtifactResponse]


class CancelTaskResponse(BaseModel):
    task_id: str
    status: str
    accepted: bool


class CancelTaskRequest(StrictRequestModel):
    task_id: str


class InterruptResponse(BaseModel):
    interrupt_id: str
    conversation_id: str
    task_id: str
    node_id: str
    question: str
    reason_code: str
    required_fields: dict[str, Any]
    status: str


class TaskInterruptsResponse(BaseModel):
    task_id: str
    interrupts: list[InterruptResponse]


class CapabilityResponse(BaseModel):
    capability_id: str
    name: str
    display_name: str = ""
    description: str
    version: str
    status: str
    kind: str = "capability"
    source: str = "builtin"
    source_path: str = ""


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityResponse]


class UserMCPCredentialInput(StrictRequestModel):
    secret_value: SecretStr | None = None
    static_headers: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_exactly_one_shape(self) -> "UserMCPCredentialInput":
        has_secret = self.secret_value is not None and bool(self.secret_value.get_secret_value())
        has_headers = bool(self.static_headers)
        if has_secret == has_headers:
            raise ValueError("credential requires exactly one of secret_value or static_headers")
        if has_headers and any(not value.get_secret_value() for value in self.static_headers.values()):
            raise ValueError("static header credential values must not be empty")
        return self


class CreateUserMCPServerRequest(StrictRequestModel):
    display_name: str = Field(min_length=1, max_length=100)
    routing_description: str = Field(max_length=2000)
    endpoint_url: str
    transport: Literal["streamable_http", "legacy_http_sse"] = "streamable_http"
    protocol_preference: str = "auto"
    auth_type: Literal["none", "bearer", "api_key_header", "static_headers"] = "none"
    auth_metadata: dict[str, Any] = Field(default_factory=dict)
    credential: UserMCPCredentialInput | None = None
    enabled: bool = True

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("display_name must be non-empty and contain no control characters")
        return normalized

    @field_validator("routing_description")
    @classmethod
    def normalize_routing_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("routing_description must be non-empty")
        if any(ord(char) < 32 and char not in "\n\t" for char in normalized):
            raise ValueError("routing_description contains unsupported control characters")
        return normalized

    @field_validator("endpoint_url")
    @classmethod
    def enforce_endpoint_size(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized.encode("utf-8")) > 2048:
            raise ValueError("endpoint_url must be non-empty and at most 2048 UTF-8 bytes")
        return normalized

    @field_validator("auth_metadata")
    @classmethod
    def reject_identity_auth_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_reserved_identity_fields(value, field_name="auth_metadata")

    @model_validator(mode="after")
    def validate_credential_shape(self) -> "CreateUserMCPServerRequest":
        if self.auth_type == "none" and self.credential is not None:
            raise ValueError("none auth must not include a credential")
        if self.auth_type != "none" and self.credential is None:
            raise ValueError("configured auth requires a credential")
        return self


class PatchUserMCPServerRequest(StrictRequestModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    routing_description: str | None = Field(default=None, max_length=2000)
    endpoint_url: str | None = None
    transport: Literal["streamable_http", "legacy_http_sse"] | None = None
    protocol_preference: str | None = None
    auth_type: Literal["none", "bearer", "api_key_header", "static_headers"] | None = None
    auth_metadata: dict[str, Any] | None = None
    enabled: bool | None = None
    credential_action: Literal["retain", "replace", "clear"] = "retain"
    credential: UserMCPCredentialInput | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("display_name must be non-empty and contain no control characters")
        return normalized

    @field_validator("routing_description")
    @classmethod
    def normalize_routing_description(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("routing_description must be non-empty when provided")
        normalized = value.strip()
        if not normalized:
            raise ValueError("routing_description must be non-empty when provided")
        if any(ord(char) < 32 and char not in "\n\t" for char in normalized):
            raise ValueError("routing_description contains unsupported control characters")
        return normalized

    @field_validator("endpoint_url")
    @classmethod
    def enforce_endpoint_size(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized.encode("utf-8")) > 2048:
            raise ValueError("endpoint_url must be non-empty and at most 2048 UTF-8 bytes")
        return normalized

    @field_validator("auth_metadata")
    @classmethod
    def reject_identity_auth_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _reject_reserved_identity_fields(value, field_name="auth_metadata")

    @model_validator(mode="after")
    def validate_credential_action(self) -> "PatchUserMCPServerRequest":
        if self.credential_action == "replace" and self.credential is None:
            raise ValueError("credential_action=replace requires credential")
        if self.credential_action != "replace" and self.credential is not None:
            raise ValueError("credential is only accepted with credential_action=replace")
        if self.auth_type == "none" and self.credential_action == "replace":
            raise ValueError("none auth cannot replace a credential")
        return self


class UserMCPServerResponse(BaseModel):
    server_id: str
    display_name: str
    routing_description: str
    endpoint_url: str
    transport: str
    protocol_preference: str
    auth_type: str
    auth_metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    health_status: str
    credential_configured: bool
    config_version: int
    security_version: int
    last_tested_at: datetime | None = None
    last_test_error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class UserMCPServerListResponse(BaseModel):
    servers: list[UserMCPServerResponse]


class UserMCPDeletePendingResponse(BaseModel):
    server_id: str
    deletion_pending: bool = True


class UserMCPToolGrantResponse(BaseModel):
    grant_id: str
    server_id: str
    server_display_name: str
    tool_name: str
    granted_at: datetime | None = None
    valid: bool
    invalid_reason: str | None = None


class UserMCPToolGrantListResponse(BaseModel):
    grants: list[UserMCPToolGrantResponse]


class MCPCallControlResponse(BaseModel):
    task_id: str
    call_ref: str
    status: str
    accepted: bool = True
