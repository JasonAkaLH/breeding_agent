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
    active_node_count: int
    completed_node_count: int
    failed_node_count: int
    cancel_requested: bool
    created_at: datetime | None
    updated_at: datetime | None


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


class CapabilityResponse(BaseModel):
    capability_id: str
    name: str
    description: str
    version: str
    status: str


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityResponse]
