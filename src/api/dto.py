from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator



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


class ReasoningEffortOptionResponse(BaseModel):
    value: str
    label: str
    allow_when_thinking_disabled: bool


class ReasoningEffortConfigResponse(BaseModel):
    default: str
    disabled_default: str | None = None
    options: list[ReasoningEffortOptionResponse]


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
    routing_description: str = Field(default="", max_length=2000)
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
            return None
        normalized = value.strip()
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
