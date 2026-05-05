from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubmitMessageRequest(BaseModel):
    account_id: str
    content: str
    routing_mode: str = "auto"
    capability_id: str | None = None
    client_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaptchaChallengeResponse(BaseModel):
    captcha_id: str
    image_svg: str
    expires_in_seconds: int


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: str
    captcha_code: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    captcha_id: str
    captcha_code: str


class UserResponse(BaseModel):
    username: str


class AuthUserResponse(BaseModel):
    user: UserResponse


class LogoutResponse(BaseModel):
    logged_out: bool


class MessageAcceptedResponse(BaseModel):
    conversation_id: str
    message_id: str
    task_id: str
    status: str


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
    account_id: str
    status: str
    current_task_id: str | None
    title: str | None
    created_at: datetime | None
    updated_at: datetime | None


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummaryResponse]


class RenameConversationRequest(BaseModel):
    title: str


class DeleteConversationResponse(BaseModel):
    conversation_id: str
    deleted: bool
    cancelled_task_ids: list[str] = Field(default_factory=list)
    deleted_counts: dict[str, int] = Field(default_factory=dict)


class MessageResponse(BaseModel):
    message_id: str
    conversation_id: str
    role: str
    content: str
    task_id: str | None
    stream_status: str | None
    created_at: datetime | None


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


class ArtifactResponse(BaseModel):
    artifact_id: str
    producer_node_id: str
    artifact_type: str
    storage_ref: str
    summary: str | None
    is_complete: bool
    created_at: datetime | None


class TaskArtifactsResponse(BaseModel):
    task_id: str
    artifacts: list[ArtifactResponse]


class CancelTaskResponse(BaseModel):
    task_id: str
    status: str
    accepted: bool


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


class AnswerInterruptRequest(BaseModel):
    answer_payload: dict[str, Any] = Field(default_factory=dict)


class AnswerInterruptResponse(BaseModel):
    interrupt_id: str
    status: str
    node_id: str
    answer_payload: dict[str, Any]


class CapabilityResponse(BaseModel):
    capability_id: str
    name: str
    description: str
    version: str
    status: str


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityResponse]
