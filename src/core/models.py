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
    username: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    current_task_id: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    delete_runner_id: str | None = None
    delete_requested_at: datetime | None = None
    delete_started_at: datetime | None = None
    delete_finished_at: datetime | None = None
    delete_failed_at: datetime | None = None
    delete_error_code: str | None = None
    delete_error_summary: str | None = None
    delete_phase: str | None = None


@dataclass(slots=True, frozen=True)
class ConversationMemorySummary:
    summary_id: str
    conversation_id: str
    username: str
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
class ConversationFileResource:
    file_id: str
    conversation_id: str
    username: str
    original_filename: str
    content_type: str
    file_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    preview: JsonMapping = field(default_factory=dict)
    description_status: str = "pending"
    description_summary: str | None = None
    description_ref: str | None = None
    status: str = "active"
    normalized_filename: str | None = None
    normalized_content_type: str | None = None
    requires_sheet_selection: bool = False
    selected_sheet: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ConversationFileIndexRepairMarker:
    conversation_id: str
    repair_kind: str = "conversation_file_index"
    status: str = "pending"
    reason_code: str = ""
    affected_upload_ids: tuple[str, ...] = ()
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class AuthUserToken:
    username: str
    api_token_hash: str | None = None
    token_issued_at: datetime | None = None
    token_last_used_at: datetime | None = None
    auth_generation: int = 0
    auth_generation_updated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class Message:
    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    task_id: str | None = None
    stream_status: str | None = None
    created_at: datetime | None = None
    message_type: str = "chat"
    metadata: JsonMapping = field(default_factory=dict)
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class FileUploadMessageProjection:
    upload_id: str
    conversation_id: str
    content: str
    metadata: JsonMapping = field(default_factory=dict)
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
    username: str | None
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
class TaskInputAttachment:
    attachment_id: str
    task_id: str
    conversation_id: str
    source_kind: str
    source_upload_id: str | None = None
    source_message_id: str | None = None
    interrupt_answer_id: str | None = None
    filename: str = ""
    content_type: str = ""
    file_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    prompt_artifact: JsonMapping = field(default_factory=dict)
    skill_artifact: JsonMapping = field(default_factory=dict)
    source_payload: JsonMapping = field(default_factory=dict)
    selected_sheet: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


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
class SlotCollection:
    collection_id: str
    task_id: str
    node_id: str
    conversation_id: str
    capability_id: str
    skill_name: str
    kind: str
    status: str
    round: int = 1
    revision: int = 0
    selected_schema_id: str | None = None
    selected_entrypoint: str | None = None
    skill_bundle_revision: str | None = None
    contract_revision: str | None = None
    schema_digest: str | None = None
    schema_snapshot: JsonMapping = field(default_factory=dict)
    slots: JsonMapping = field(default_factory=dict)
    resolved: JsonMapping = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    invalid: tuple[JsonMapping, ...] = ()
    last_question: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    failed_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SlotEvent:
    slot_event_id: str
    collection_id: str
    task_id: str
    node_id: str
    conversation_id: str
    event_type: str
    round: int = 1
    revision: int = 0
    idempotency_key: str | None = None
    payload: JsonMapping = field(default_factory=dict)
    created_at: datetime | None = None


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
