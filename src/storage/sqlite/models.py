from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import DateTimeText, JSONText, SQLiteBase


class ConversationRow(SQLiteBase):
    __tablename__ = "conversation"
    __table_args__ = (
        Index("idx_conversation_account_updated", "account_id", "updated_at"),
        Index("idx_conversation_current_task", "current_task_id"),
    )

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class AuthUserRow(SQLiteBase):
    __tablename__ = "auth_user"
    __table_args__ = (Index("idx_auth_user_status_updated", "status", "updated_at"),)

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_salt: Mapped[str] = mapped_column(Text, nullable=False)
    password_scheme: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    last_login_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class CaptchaChallengeRow(SQLiteBase):
    __tablename__ = "auth_captcha_challenge"
    __table_args__ = (
        Index("idx_auth_captcha_expires", "expires_at"),
        Index("idx_auth_captcha_consumed", "consumed_at"),
    )

    captcha_id: Mapped[str] = mapped_column(Text, primary_key=True)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class AuthSessionRow(SQLiteBase):
    __tablename__ = "auth_session"
    __table_args__ = (
        Index("idx_auth_session_username_expires", "username", "expires_at"),
        Index("idx_auth_session_revoked", "revoked_at"),
    )

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


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
