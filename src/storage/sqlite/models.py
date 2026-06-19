from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, Integer, Text, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import DateTimeText, JSONText, SQLiteBase


class ConversationRow(SQLiteBase):
    __tablename__ = "conversation"
    __table_args__ = (
        Index("idx_conversation_username_status_updated", "username", "status", "updated_at"),
        Index("idx_conversation_username_updated", "username", "updated_at"),
        Index("idx_conversation_current_task", "current_task_id"),
        Index("idx_conversation_delete_status_updated", "status", "delete_phase", "updated_at"),
    )

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_runner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    delete_requested_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_started_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_finished_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_failed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    delete_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    delete_phase: Mapped[str | None] = mapped_column(Text, nullable=True)




class ConversationFileResourceRow(SQLiteBase):
    __tablename__ = "conversation_file_resource"
    __table_args__ = (
        Index("idx_conversation_file_conversation_status_created", "conversation_id", "status", "created_at"),
        Index("idx_conversation_file_username_conversation", "username", "conversation_id"),
        Index("idx_conversation_file_storage_key", "storage_key"),
    )

    file_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    preview: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    description_status: Mapped[str] = mapped_column(Text, nullable=False)
    description_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_sheet_selection: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    selected_sheet: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class ConversationFileIndexRepairMarkerRow(SQLiteBase):
    __tablename__ = "conversation_file_index_repair_marker"
    __table_args__ = (
        Index("idx_conversation_file_index_repair_status_retry", "status", "next_retry_at", "updated_at"),
    )

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    repair_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    affected_upload_ids: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    resolved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class ConversationMemorySummaryRow(SQLiteBase):
    __tablename__ = "conversation_memory_summary"
    __table_args__ = (
        Index("idx_conversation_memory_summary_scope_updated", "conversation_id", "username", "updated_at"),
        Index("idx_conversation_memory_summary_conversation_created", "conversation_id", "created_at"),
    )

    summary_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    covered_until_turn_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    covered_until_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    covered_until_created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_ids_hash: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_version: Mapped[str] = mapped_column(Text, nullable=False)
    compression_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_metadata_safe: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class PendingSkillContextRow(SQLiteBase):
    __tablename__ = "conversation_pending_skill_context"
    __table_args__ = (
        Index("idx_pending_skill_context_conversation_status", "conversation_id", "status", "updated_at"),
        Index("idx_pending_skill_context_source_task", "source_task_id"),
    )

    context_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_task_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    original_user_message: Mapped[str] = mapped_column(Text, nullable=False)
    missing_requirements: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    assistant_message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class AuthUserTokenRow(SQLiteBase):
    __tablename__ = "auth_user_token"
    __table_args__ = (
        UniqueConstraint("api_token_hash", name="uq_auth_user_token_hash"),
        Index("idx_auth_user_token_updated", "updated_at"),
    )

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    api_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_issued_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    token_last_used_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    auth_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    auth_generation_updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MessageRow(SQLiteBase):
    __tablename__ = "message"
    __table_args__ = (
        Index("idx_message_conversation_created", "conversation_id", "created_at"),
        Index("idx_message_task_created", "task_id", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    message_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'chat'"))
    message_metadata: Mapped[dict | None] = mapped_column("metadata", JSONText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class TaskRow(SQLiteBase):
    __tablename__ = "task"
    __table_args__ = (
        Index("idx_task_conversation_created", "conversation_id", "created_at"),
        Index("idx_task_status_updated", "status", "updated_at"),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    root_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    routing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    requested_capability_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class TaskNodeRow(SQLiteBase):
    __tablename__ = "task_node"
    __table_args__ = (
        Index("idx_task_node_task_status", "task_id", "status"),
        Index("idx_task_node_capability_status", "capability_id", "status"),
        Index("idx_task_node_started", "started_at"),
    )

    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    criticality: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_type: Mapped[str] = mapped_column(Text, nullable=False)
    retry_policy: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    timeout_policy: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    resource_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_refs: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    output_refs: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    started_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    finished_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class TaskEdgeRow(SQLiteBase):
    __tablename__ = "task_edge"
    __table_args__ = (
        UniqueConstraint("task_id", "from_node_id", "to_node_id"),
        Index("idx_task_edge_to_node", "task_id", "to_node_id"),
    )

    edge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArtifactRow(SQLiteBase):
    __tablename__ = "artifact"
    __table_args__ = (
        Index("idx_artifact_task_created", "task_id", "created_at"),
        Index("idx_artifact_node_created", "producer_node_id", "created_at"),
    )

    artifact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class TaskInputAttachmentRow(SQLiteBase):
    __tablename__ = "task_input_attachment"
    __table_args__ = (
        Index("idx_task_input_attachment_task_created", "task_id", "created_at"),
        Index("idx_task_input_attachment_conversation_task", "conversation_id", "task_id"),
        Index("idx_task_input_attachment_conversation_recent", "conversation_id", "updated_at", "created_at", "attachment_id"),
        Index("idx_task_input_attachment_upload", "source_upload_id"),
    )

    attachment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    interrupt_answer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_artifact: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    skill_artifact: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    source_payload: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    selected_sheet: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class EventRecordRow(SQLiteBase):
    __tablename__ = "event_record"
    __table_args__ = (
        Index("idx_event_task_created", "task_id", "created_at"),
        Index("idx_event_conversation_created", "conversation_id", "created_at"),
        Index("idx_event_type_created", "event_type", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MailboxMessageRow(SQLiteBase):
    __tablename__ = "mailbox_message"
    __table_args__ = (
        Index("idx_mailbox_message_conversation_created", "conversation_id", "created_at"),
        Index("idx_mailbox_message_task_created", "task_id", "created_at"),
        Index("idx_mailbox_message_node_created", "node_id", "created_at"),
        Index("idx_mailbox_message_channel_type_created", "channel", "message_type", "created_at"),
        Index("idx_mailbox_message_correlation", "correlation_id"),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_agent: Mapped[str] = mapped_column(Text, nullable=False)
    to_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    ack_policy: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    resolved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MailboxDeliveryRow(SQLiteBase):
    __tablename__ = "mailbox_delivery"
    __table_args__ = (
        UniqueConstraint("message_id", "recipient_agent"),
        Index("idx_mailbox_delivery_message", "message_id"),
        Index("idx_mailbox_delivery_status_expires", "status", "expires_at"),
        Index("idx_mailbox_delivery_recipient_status", "recipient_agent", "status", "created_at"),
        Index("idx_mailbox_delivery_retry", "next_retry_at"),
    )

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_agent: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delivered_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    acknowledged_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    resolved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    next_retry_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class InterruptRow(SQLiteBase):
    __tablename__ = "interrupt"
    __table_args__ = (
        Index("idx_interrupt_conversation_status", "conversation_id", "status", "created_at"),
        Index("idx_interrupt_task_node", "task_id", "node_id"),
        Index("idx_interrupt_expires", "expires_at"),
    )

    interrupt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_agent: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    required_fields: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    answered_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class InterruptAnswerRow(SQLiteBase):
    __tablename__ = "interrupt_answer"
    __table_args__ = (Index("idx_interrupt_answer_interrupt_created", "interrupt_id", "created_at"),)

    interrupt_answer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    interrupt_id: Mapped[str] = mapped_column(Text, nullable=False)
    answer_payload: Mapped[dict] = mapped_column(JSONText(), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    accepted_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class SlotCollectionRow(SQLiteBase):
    __tablename__ = "slot_collection"
    __table_args__ = (
        Index("idx_slot_collection_task_node_status", "task_id", "node_id", "status"),
        Index("idx_slot_collection_task_status", "task_id", "status"),
        Index("idx_slot_collection_conversation_status_updated", "conversation_id", "status", "updated_at"),
    )

    collection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_schema_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_entrypoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_bundle_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    contract_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_snapshot_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    slots_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    resolved_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    missing_json: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    invalid_json: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    last_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    completed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    failed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class SlotEventRow(SQLiteBase):
    __tablename__ = "slot_event"
    __table_args__ = (
        UniqueConstraint("collection_id", "idempotency_key", name="uq_slot_event_collection_idempotency"),
        Index("idx_slot_event_collection_created", "collection_id", "created_at"),
        Index("idx_slot_event_task_created", "task_id", "created_at"),
    )

    slot_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    collection_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class CheckpointRow(SQLiteBase):
    __tablename__ = "checkpoint"
    __table_args__ = (
        Index("idx_checkpoint_task_node", "task_id", "node_id", "created_at"),
        Index("idx_checkpoint_resume_token", "resume_token"),
    )

    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_kind: Mapped[str] = mapped_column(Text, nullable=False)
    resume_token: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    invalidated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
