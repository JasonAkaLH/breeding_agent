from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator



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


class ModelEditionOptionResponse(BaseModel):
    value: str
    label: str


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
    preview: UploadPreviewResponse


class UploadListResponse(BaseModel):
    conversation_id: str
    uploads: list[UploadFileResponse]


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
