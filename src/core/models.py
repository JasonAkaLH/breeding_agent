from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .enums import (
    AckPolicy,
    ArtifactType,
    ConversationStatus,
    DependencyType,
    EdgeType,
    EventVisibility,
    InterruptStatus,
    MailboxChannel,
    MailboxDeliveryStatus,
    MessageRole,
    NodeCriticality,
    NodeStatus,
    RoutingMode,
    TaskStatus,
)


JsonMapping = Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class Conversation:
    conversation_id: str
    account_id: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    current_task_id: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ConversationMemorySummary:
    summary_id: str
    conversation_id: str
    account_id: str
    covered_until_turn_id: str | None
    covered_until_message_id: str | None
    covered_until_created_at: datetime | None
    summary_text: str
    source_message_count: int
    source_message_ids_hash: str
    estimated_tokens: int
    summary_version: str
    compression_policy_version: str
    model_metadata_safe: JsonMapping = field(default_factory=dict)
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class AuthUser:
    username: str
    password_hash: str
    password_salt: str
    password_scheme: str
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class CaptchaChallenge:
    captcha_id: str
    code_hash: str
    expires_at: datetime
    attempt_count: int = 0
    consumed_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class AuthSession:
    session_id: str
    username: str
    expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Message:
    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    task_id: str | None = None
    stream_status: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Task:
    task_id: str
    conversation_id: str
    root_message_id: str
    status: TaskStatus = TaskStatus.ACCEPTED
    routing_mode: RoutingMode = RoutingMode.AUTO
    requested_capability_id: str | None = None
    root_node_id: str | None = None
    summary: str | None = None
    cancel_requested_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class PendingSkillContext:
    context_id: str
    conversation_id: str
    account_id: str | None
    capability_id: str
    skill_name: str
    source_task_id: str
    source_message_id: str
    original_user_message: str
    missing_requirements: tuple[str, ...]
    assistant_message: str
    status: str = "pending_user_input"
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TaskNode:
    node_id: str
    task_id: str
    capability_id: str
    assigned_instance_id: str | None = None
    status: NodeStatus = NodeStatus.PENDING
    criticality: NodeCriticality = NodeCriticality.REQUIRED
    dependency_type: DependencyType = DependencyType.HARD
    retry_policy: JsonMapping = field(default_factory=dict)
    timeout_policy: JsonMapping = field(default_factory=dict)
    resource_class: str | None = None
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TaskEdge:
    from_node_id: str
    to_node_id: str
    edge_type: EdgeType = EdgeType.DATA
    condition: str | None = None


@dataclass(slots=True, frozen=True)
class Artifact:
    artifact_id: str
    task_id: str
    producer_node_id: str
    artifact_type: ArtifactType
    storage_ref: str
    summary: str | None = None
    is_complete: bool = False
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class EventRecord:
    event_id: str
    conversation_id: str
    task_id: str
    node_id: str | None = None
    agent_id: str | None = None
    event_type: str = ""
    payload: JsonMapping = field(default_factory=dict)
    visibility: EventVisibility = EventVisibility.INTERNAL
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MailboxMessage:
    message_id: str
    conversation_id: str
    task_id: str
    node_id: str | None = None
    parent_message_id: str | None = None
    correlation_id: str | None = None
    from_agent: str = ""
    to_agent: str | None = None
    to_role: str | None = None
    channel: MailboxChannel = MailboxChannel.PEER_COLLABORATION
    message_type: str = ""
    ack_policy: AckPolicy = AckPolicy.LIGHT
    priority: int = 0
    payload: JsonMapping = field(default_factory=dict)
    payload_schema_version: int = 1
    created_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MailboxDelivery:
    delivery_id: str
    message_id: str
    recipient_agent: str
    recipient_role: str | None = None
    status: MailboxDeliveryStatus = MailboxDeliveryStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 1
    ttl_seconds: int | None = None
    expires_at: datetime | None = None
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    next_retry_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Interrupt:
    interrupt_id: str
    conversation_id: str
    task_id: str
    node_id: str
    source_agent: str
    source_message_id: str
    question: str
    reason_code: str
    required_fields: JsonMapping = field(default_factory=dict)
    status: InterruptStatus = InterruptStatus.OPEN
    expires_at: datetime | None = None
    created_at: datetime | None = None
    answered_at: datetime | None = None
    cancelled_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class InterruptAnswer:
    interrupt_answer_id: str
    interrupt_id: str
    answer_payload: JsonMapping
    source_message_id: str | None = None
    accepted: bool = False
    created_at: datetime | None = None
    accepted_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    node_id: str
    agent_id: str
    snapshot_ref: str
    snapshot_kind: str
    resume_token: str
    source_message_id: str | None = None
    created_at: datetime | None = None
    invalidated_at: datetime | None = None
