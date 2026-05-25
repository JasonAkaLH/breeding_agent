from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from src.core.contracts import StoragePort
from src.core.enums import ArtifactType, EdgeType, TaskStatus
from src.core.models import (
    Artifact,
    AuthUserToken,
    Checkpoint,
    Conversation,
    ConversationMemorySummary,
    EventRecord,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    Message,
    PendingSkillContext,
    Task,
    TaskEdge,
    TaskNode,
)
from src.lifecycle.rust_contract import contract_value as lifecycle_contract_value
from src.lifecycle.rust_contract import status_list as lifecycle_status_list
from src.storage.rust_contract import error_policy as runtime_error_policy
from src.storage.rust_contract import mode_for_component as runtime_mode_for_component
from src.storage.rust_contract import operation_policy as runtime_operation_policy
from src.storage.rust_contract import resource_limit as runtime_resource_limit
from src.storage.runtime_sidecar_facade import ensure_sidecar_write_allowed, validate_runtime_sidecar_response
from src.storage.runtime_sidecar_shadow import (
    RuntimeSidecarShadowSink,
    normalize_runtime_sidecar_response,
    record_runtime_sidecar_shadow_write,
)

from .base import build_task_edge_id
from .models import (
    ArtifactRow,
    AuthUserTokenRow,
    CheckpointRow,
    ConversationRow,
    ConversationMemorySummaryRow,
    EventRecordRow,
    InterruptAnswerRow,
    InterruptRow,
    MailboxDeliveryRow,
    MailboxMessageRow,
    MessageRow,
    PendingSkillContextRow,
    TaskEdgeRow,
    TaskNodeRow,
    TaskRow,
)

def _row_to_conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        conversation_id=row.conversation_id,
        username=row.username,
        status=row.status,
        current_task_id=row.current_task_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_conversation_memory_summary(row: ConversationMemorySummaryRow) -> ConversationMemorySummary:
    return ConversationMemorySummary(
        summary_id=row.summary_id,
        conversation_id=row.conversation_id,
        username=row.username,
        covered_until_turn_id=row.covered_until_turn_id,
        covered_until_message_id=row.covered_until_message_id,
        covered_until_created_at=row.covered_until_created_at,
        summary_text=row.summary_text,
        source_message_count=row.source_message_count,
        source_message_ids_hash=row.source_message_ids_hash,
        estimated_tokens=row.estimated_tokens,
        summary_version=row.summary_version,
        compression_policy_version=row.compression_policy_version,
        model_metadata_safe=dict(row.model_metadata_safe or {}),
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_pending_skill_context(row: PendingSkillContextRow) -> PendingSkillContext:
    return PendingSkillContext(
        context_id=row.context_id,
        conversation_id=row.conversation_id,
        username=row.username,
        capability_id=row.capability_id,
        skill_name=row.skill_name,
        source_task_id=row.source_task_id,
        source_message_id=row.source_message_id,
        original_user_message=row.original_user_message,
        missing_requirements=tuple(str(item) for item in (row.missing_requirements or ())),
        assistant_message=row.assistant_message,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row_to_auth_user_token(row: AuthUserTokenRow) -> AuthUserToken:
    return AuthUserToken(
        username=row.username,
        api_token_hash=row.api_token_hash,
        token_issued_at=row.token_issued_at,
        token_last_used_at=row.token_last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_message(row: MessageRow) -> Message:
    return Message(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        task_id=row.task_id,
        stream_status=row.stream_status,
        created_at=row.created_at,
    )


def _row_to_task(row: TaskRow) -> Task:
    return Task(
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        root_message_id=row.root_message_id,
        status=row.status,
        routing_mode=row.routing_mode,
        requested_capability_id=row.requested_capability_id,
        root_node_id=row.root_node_id,
        summary=row.summary,
        cancel_requested_at=row.cancel_requested_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_task_node(row: TaskNodeRow) -> TaskNode:
    return TaskNode(
        node_id=row.node_id,
        task_id=row.task_id,
        capability_id=row.capability_id,
        assigned_instance_id=row.assigned_instance_id,
        status=row.status,
        criticality=row.criticality,
        dependency_type=row.dependency_type,
        retry_policy=row.retry_policy or {},
        timeout_policy=row.timeout_policy or {},
        resource_class=row.resource_class,
        input_refs=tuple(row.input_refs or ()),
        output_refs=tuple(row.output_refs or ()),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _row_to_task_edge(row: TaskEdgeRow) -> TaskEdge:
    return TaskEdge(from_node_id=row.from_node_id, to_node_id=row.to_node_id, edge_type=row.edge_type, condition=row.condition)


def _row_to_artifact(row: ArtifactRow) -> Artifact:
    return Artifact(
        artifact_id=row.artifact_id,
        task_id=row.task_id,
        producer_node_id=row.producer_node_id,
        artifact_type=row.artifact_type,
        storage_ref=row.storage_ref,
        summary=row.summary,
        is_complete=bool(row.is_complete),
        created_at=row.created_at,
    )


def _row_to_event_record(row: EventRecordRow) -> EventRecord:
    return EventRecord(
        event_id=row.event_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        node_id=row.node_id,
        agent_id=row.agent_id,
        event_type=row.event_type,
        payload=row.payload or {},
        visibility=row.visibility,
        created_at=row.created_at,
    )


def _row_to_mailbox_message(row: MailboxMessageRow) -> MailboxMessage:
    return MailboxMessage(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        node_id=row.node_id,
        parent_message_id=row.parent_message_id,
        correlation_id=row.correlation_id,
        from_agent=row.from_agent,
        to_agent=row.to_agent,
        to_role=row.to_role,
        channel=row.channel,
        message_type=row.message_type,
        ack_policy=row.ack_policy,
        priority=row.priority,
        payload=row.payload or {},
        payload_schema_version=row.payload_schema_version,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


def _row_to_mailbox_delivery(row: MailboxDeliveryRow) -> MailboxDelivery:
    return MailboxDelivery(
        delivery_id=row.delivery_id,
        message_id=row.message_id,
        recipient_agent=row.recipient_agent,
        recipient_role=row.recipient_role,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        ttl_seconds=row.ttl_seconds,
        expires_at=row.expires_at,
        delivered_at=row.delivered_at,
        acknowledged_at=row.acknowledged_at,
        resolved_at=row.resolved_at,
        next_retry_at=row.next_retry_at,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_interrupt(row: InterruptRow) -> Interrupt:
    return Interrupt(
        interrupt_id=row.interrupt_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        node_id=row.node_id,
        source_agent=row.source_agent,
        source_message_id=row.source_message_id,
        question=row.question,
        reason_code=row.reason_code,
        required_fields=row.required_fields or {},
        status=row.status,
        expires_at=row.expires_at,
        created_at=row.created_at,
        answered_at=row.answered_at,
        cancelled_at=row.cancelled_at,
    )


def _row_to_interrupt_answer(row: InterruptAnswerRow) -> InterruptAnswer:
    return InterruptAnswer(
        interrupt_answer_id=row.interrupt_answer_id,
        interrupt_id=row.interrupt_id,
        answer_payload=row.answer_payload or {},
        source_message_id=row.source_message_id,
        accepted=bool(row.accepted),
        created_at=row.created_at,
        accepted_at=row.accepted_at,
    )


def _row_to_checkpoint(row: CheckpointRow) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=row.checkpoint_id,
        task_id=row.task_id,
        node_id=row.node_id,
        agent_id=row.agent_id,
        snapshot_ref=row.snapshot_ref,
        snapshot_kind=row.snapshot_kind,
        resume_token=row.resume_token,
        source_message_id=row.source_message_id,
        created_at=row.created_at,
        invalidated_at=row.invalidated_at,
    )


def _ensure_event_append_payload_within_rust_contract(event: EventRecord) -> None:
    ensure_sidecar_write_allowed(
        component="event_log",
        operation_name="event_append",
        unavailable_error_code="event_log_unavailable",
    )
    payload_size = len(json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"))
    limit = runtime_resource_limit("event_payload_bytes")
    if payload_size > limit:
        error_code = runtime_error_policy("event_log_payload_too_large")["code"]
        raise ValueError(f"{error_code}: event payload exceeds Rust runtime sidecar limit of {limit} bytes")


def _ensure_event_replay_policy_compatible_with_rust_contract() -> tuple[int, int]:
    policy = runtime_operation_policy("event_replay")
    if policy.get("kind") != "read" or policy.get("python_legacy_write_fallback") is not False:
        raise RuntimeError("Rust runtime sidecar event_replay policy is incompatible")
    return (
        runtime_resource_limit("replay_page_events"),
        runtime_resource_limit("replay_page_bytes"),
    )


def _resolve_event_replay_page_limit(requested_limit: int | None, event_limit: int) -> int:
    if requested_limit is None:
        return event_limit
    if requested_limit < 1 or requested_limit > event_limit:
        error_code = runtime_error_policy("event_log_replay_page_exceeded")["code"]
        raise ValueError(f"{error_code}: requested event replay page exceeds Rust runtime sidecar limit")
    return requested_limit


def _ensure_event_replay_page_within_rust_contract(events: list[EventRecord], event_limit: int, byte_limit: int) -> None:
    if len(events) > event_limit:
        error_code = runtime_error_policy("event_log_replay_page_exceeded")["code"]
        raise ValueError(f"{error_code}: event replay exceeds Rust runtime sidecar page limit")
    payload_bytes = sum(
        len(json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"))
        for event in events
    )
    if payload_bytes > byte_limit:
        error_code = runtime_error_policy("event_log_replay_page_exceeded")["code"]
        raise ValueError(f"{error_code}: event replay exceeds Rust runtime sidecar page limit")


def _ensure_runtime_store_write_allowed_by_rust_contract(operation_name: str) -> None:
    ensure_sidecar_write_allowed(
        component="runtime_store",
        operation_name=operation_name,
        unavailable_error_code="runtime_store_unavailable",
    )


class SQLiteStateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_auth_user_token(self, token: AuthUserToken) -> AuthUserToken:
        row = AuthUserTokenRow(
            username=token.username,
            api_token_hash=token.api_token_hash,
            token_issued_at=token.token_issued_at,
            token_last_used_at=token.token_last_used_at,
            created_at=token.created_at,
            updated_at=token.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_auth_user_token(merged)

    def get_auth_user_token(self, username: str) -> AuthUserToken | None:
        row = self._session.get(AuthUserTokenRow, username)
        return None if row is None else _row_to_auth_user_token(row)

    def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None:
        row = self._session.execute(
            select(AuthUserTokenRow).where(AuthUserTokenRow.api_token_hash == api_token_hash)
        ).scalar_one_or_none()
        return None if row is None else _row_to_auth_user_token(row)

    def touch_auth_user_token_last_used(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        result = self._session.execute(
            update(AuthUserTokenRow)
            .where(
                AuthUserTokenRow.username == username,
                AuthUserTokenRow.api_token_hash == api_token_hash,
            )
            .values(token_last_used_at=at, updated_at=at)
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        return None if row is None else _row_to_auth_user_token(row)

    def clear_auth_user_token(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        result = self._session.execute(
            update(AuthUserTokenRow)
            .where(
                AuthUserTokenRow.username == username,
                AuthUserTokenRow.api_token_hash == api_token_hash,
            )
            .values(api_token_hash=None, token_issued_at=None, token_last_used_at=None, updated_at=at)
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        return None if row is None else _row_to_auth_user_token(row)

    def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        result = self._session.execute(
            update(AuthUserTokenRow)
            .where(
                AuthUserTokenRow.username == username,
                AuthUserTokenRow.api_token_hash == old_api_token_hash,
            )
            .values(
                api_token_hash=new_api_token_hash,
                token_issued_at=at,
                token_last_used_at=None,
                updated_at=at,
            )
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        return None if row is None else _row_to_auth_user_token(row)

    def save_conversation(self, conversation: Conversation) -> Conversation:
        row = ConversationRow(
            conversation_id=conversation.conversation_id,
            username=conversation.username,
            status=conversation.status,
            current_task_id=conversation.current_task_id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_conversation(merged)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        return None if row is None else _row_to_conversation(row)

    def list_conversations_for_username(self, username: str) -> list[Conversation]:
        rows = self._session.scalars(
            select(ConversationRow)
            .where(ConversationRow.username == username)
            .order_by(ConversationRow.updated_at.desc(), ConversationRow.conversation_id.desc())
        ).all()
        return [_row_to_conversation(row) for row in rows]

    def save_conversation_memory_summary(self, summary: ConversationMemorySummary) -> ConversationMemorySummary:
        row = ConversationMemorySummaryRow(
            summary_id=summary.summary_id,
            conversation_id=summary.conversation_id,
            username=summary.username,
            covered_until_turn_id=summary.covered_until_turn_id,
            covered_until_message_id=summary.covered_until_message_id,
            covered_until_created_at=summary.covered_until_created_at,
            summary_text=summary.summary_text,
            source_message_count=summary.source_message_count,
            source_message_ids_hash=summary.source_message_ids_hash,
            estimated_tokens=summary.estimated_tokens,
            summary_version=summary.summary_version,
            compression_policy_version=summary.compression_policy_version,
            model_metadata_safe=dict(summary.model_metadata_safe),
            last_error=summary.last_error,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_conversation_memory_summary(merged)

    def get_conversation_memory_summary(self, summary_id: str) -> ConversationMemorySummary | None:
        row = self._session.get(ConversationMemorySummaryRow, summary_id)
        return None if row is None else _row_to_conversation_memory_summary(row)

    def get_latest_conversation_memory_summary(
        self,
        conversation_id: str,
        *,
        username: str | None = None,
    ) -> ConversationMemorySummary | None:
        statement = select(ConversationMemorySummaryRow).where(
            ConversationMemorySummaryRow.conversation_id == conversation_id
        )
        if username is not None:
            statement = statement.where(ConversationMemorySummaryRow.username == username)
        row = self._session.scalar(
            statement.order_by(
                ConversationMemorySummaryRow.covered_until_created_at.desc(),
                ConversationMemorySummaryRow.covered_until_turn_id.desc(),
                ConversationMemorySummaryRow.covered_until_message_id.desc(),
                ConversationMemorySummaryRow.updated_at.desc(),
                ConversationMemorySummaryRow.created_at.desc(),
                ConversationMemorySummaryRow.summary_id.desc(),
            )
        )
        return None if row is None else _row_to_conversation_memory_summary(row)

    def list_conversation_memory_summaries(self, conversation_id: str) -> list[ConversationMemorySummary]:
        rows = self._session.scalars(
            select(ConversationMemorySummaryRow)
            .where(ConversationMemorySummaryRow.conversation_id == conversation_id)
            .order_by(
                ConversationMemorySummaryRow.covered_until_created_at.desc(),
                ConversationMemorySummaryRow.covered_until_turn_id.desc(),
                ConversationMemorySummaryRow.covered_until_message_id.desc(),
                ConversationMemorySummaryRow.updated_at.desc(),
                ConversationMemorySummaryRow.created_at.desc(),
                ConversationMemorySummaryRow.summary_id.desc(),
            )
        ).all()
        return [_row_to_conversation_memory_summary(row) for row in rows]

    def delete_conversation_memory_summaries_for_conversation(self, conversation_id: str) -> int:
        result = self._session.execute(
            delete(ConversationMemorySummaryRow).where(ConversationMemorySummaryRow.conversation_id == conversation_id)
        )
        self._session.flush()
        return int(result.rowcount if result.rowcount is not None and result.rowcount > 0 else 0)

    def save_pending_skill_context(self, context: PendingSkillContext) -> PendingSkillContext:
        if context.status == "pending_user_input":
            self.mark_pending_skill_context_superseded(
                context.conversation_id,
                exclude_context_id=context.context_id,
                updated_at=context.updated_at or context.created_at,
            )
        row = PendingSkillContextRow(
            context_id=context.context_id,
            conversation_id=context.conversation_id,
            username=context.username,
            capability_id=context.capability_id,
            skill_name=context.skill_name,
            source_task_id=context.source_task_id,
            source_message_id=context.source_message_id,
            original_user_message=context.original_user_message,
            missing_requirements=list(context.missing_requirements),
            assistant_message=context.assistant_message,
            status=context.status,
            created_at=context.created_at,
            updated_at=context.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_pending_skill_context(merged)

    def get_pending_skill_context(self, context_id: str) -> PendingSkillContext | None:
        row = self._session.get(PendingSkillContextRow, context_id)
        return None if row is None else _row_to_pending_skill_context(row)

    def get_active_pending_skill_context(self, conversation_id: str) -> PendingSkillContext | None:
        row = self._session.scalar(
            select(PendingSkillContextRow)
            .where(
                PendingSkillContextRow.conversation_id == conversation_id,
                PendingSkillContextRow.status == "pending_user_input",
            )
            .order_by(PendingSkillContextRow.updated_at.desc(), PendingSkillContextRow.created_at.desc(), PendingSkillContextRow.context_id.desc())
        )
        return None if row is None else _row_to_pending_skill_context(row)

    def mark_pending_skill_context_consumed(self, context_id: str, *, updated_at: datetime | None = None) -> PendingSkillContext | None:
        return self._mark_pending_skill_context_status(context_id, "consumed", updated_at=updated_at)

    def mark_pending_skill_context_cancelled(self, context_id: str, *, updated_at: datetime | None = None) -> PendingSkillContext | None:
        return self._mark_pending_skill_context_status(context_id, "cancelled", updated_at=updated_at)

    def mark_pending_skill_context_superseded(
        self,
        conversation_id: str,
        *,
        exclude_context_id: str | None = None,
        updated_at: datetime | None = None,
    ) -> int:
        status_updated_at = updated_at or _utcnow_naive()
        rows = self._session.scalars(
            select(PendingSkillContextRow).where(
                PendingSkillContextRow.conversation_id == conversation_id,
                PendingSkillContextRow.status == "pending_user_input",
            )
        ).all()
        count = 0
        for row in rows:
            if exclude_context_id is not None and row.context_id == exclude_context_id:
                continue
            row.status = "superseded"
            row.updated_at = status_updated_at
            count += 1
        self._session.flush()
        return count

    def _mark_pending_skill_context_status(
        self,
        context_id: str,
        status: str,
        *,
        updated_at: datetime | None = None,
    ) -> PendingSkillContext | None:
        row = self._session.get(PendingSkillContextRow, context_id)
        if row is None:
            return None
        row.status = status
        row.updated_at = updated_at or _utcnow_naive()
        self._session.flush()
        return _row_to_pending_skill_context(row)

    def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        task_ids = list(
            self._session.scalars(select(TaskRow.task_id).where(TaskRow.conversation_id == conversation_id)).all()
        )
        mailbox_conditions = [MailboxMessageRow.conversation_id == conversation_id]
        event_conditions = [EventRecordRow.conversation_id == conversation_id]
        interrupt_conditions = [InterruptRow.conversation_id == conversation_id]
        message_conditions = [MessageRow.conversation_id == conversation_id]
        if task_ids:
            mailbox_conditions.append(MailboxMessageRow.task_id.in_(task_ids))
            event_conditions.append(EventRecordRow.task_id.in_(task_ids))
            interrupt_conditions.append(InterruptRow.task_id.in_(task_ids))
            message_conditions.append(MessageRow.task_id.in_(task_ids))

        mailbox_message_ids = list(
            self._session.scalars(
                select(MailboxMessageRow.message_id).where(or_(*mailbox_conditions))
            ).all()
        )
        interrupt_ids = list(
            self._session.scalars(
                select(InterruptRow.interrupt_id).where(or_(*interrupt_conditions))
            ).all()
        )

        deleted_counts: dict[str, int] = {
            "conversation_memory_summary": 0,
            "conversation_pending_skill_context": 0,
            "mailbox_delivery": 0,
            "interrupt_answer": 0,
            "checkpoint": 0,
            "interrupt": 0,
            "mailbox_message": 0,
            "event_record": 0,
            "artifact": 0,
            "task_edge": 0,
            "task_node": 0,
            "message": 0,
            "task": 0,
            "conversation": 0,
        }

        def _delete(name: str, statement) -> None:
            result = self._session.execute(statement)
            rowcount = result.rowcount if result.rowcount is not None and result.rowcount > 0 else 0
            deleted_counts[name] = int(rowcount)

        if mailbox_message_ids:
            _delete("mailbox_delivery", delete(MailboxDeliveryRow).where(MailboxDeliveryRow.message_id.in_(mailbox_message_ids)))
        if interrupt_ids:
            _delete("interrupt_answer", delete(InterruptAnswerRow).where(InterruptAnswerRow.interrupt_id.in_(interrupt_ids)))
        if task_ids:
            _delete("checkpoint", delete(CheckpointRow).where(CheckpointRow.task_id.in_(task_ids)))
        _delete("interrupt", delete(InterruptRow).where(or_(*interrupt_conditions)))
        _delete("mailbox_message", delete(MailboxMessageRow).where(or_(*mailbox_conditions)))
        _delete("event_record", delete(EventRecordRow).where(or_(*event_conditions)))
        if task_ids:
            _delete("artifact", delete(ArtifactRow).where(ArtifactRow.task_id.in_(task_ids)))
            _delete("task_edge", delete(TaskEdgeRow).where(TaskEdgeRow.task_id.in_(task_ids)))
            _delete("task_node", delete(TaskNodeRow).where(TaskNodeRow.task_id.in_(task_ids)))
        _delete(
            "conversation_memory_summary",
            delete(ConversationMemorySummaryRow).where(ConversationMemorySummaryRow.conversation_id == conversation_id),
        )
        _delete(
            "conversation_pending_skill_context",
            delete(PendingSkillContextRow).where(PendingSkillContextRow.conversation_id == conversation_id),
        )
        _delete("message", delete(MessageRow).where(or_(*message_conditions)))
        _delete("task", delete(TaskRow).where(TaskRow.conversation_id == conversation_id))
        _delete("conversation", delete(ConversationRow).where(ConversationRow.conversation_id == conversation_id))
        self._session.flush()
        return deleted_counts

    def save_message(self, message: Message) -> Message:
        row = MessageRow(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            role=message.role,
            content=message.content,
            task_id=message.task_id,
            stream_status=message.stream_status,
            created_at=message.created_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_message(merged)

    def get_message(self, message_id: str) -> Message | None:
        row = self._session.get(MessageRow, message_id)
        return None if row is None else _row_to_message(row)

    def list_messages_for_conversation(self, conversation_id: str) -> list[Message]:
        rows = self._session.scalars(
            select(MessageRow).where(MessageRow.conversation_id == conversation_id).order_by(MessageRow.created_at, MessageRow.message_id)
        ).all()
        return [_row_to_message(row) for row in rows]

    def save_task(self, task: Task) -> Task:
        _ensure_runtime_store_write_allowed_by_rust_contract("task_submit")
        row = TaskRow(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            root_message_id=task.root_message_id,
            status=task.status,
            routing_mode=task.routing_mode,
            requested_capability_id=task.requested_capability_id,
            root_node_id=task.root_node_id,
            summary=task.summary,
            cancel_requested_at=task.cancel_requested_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task(merged)

    def get_task(self, task_id: str) -> Task | None:
        row = self._session.get(TaskRow, task_id)
        return None if row is None else _row_to_task(row)

    def get_active_task_for_conversation(self, conversation_id: str) -> Task | None:
        row = self._session.scalar(
            select(TaskRow)
            .where(
                TaskRow.conversation_id == conversation_id,
                TaskRow.status.in_(lifecycle_status_list("active_task_statuses")),
            )
            .order_by(TaskRow.created_at.desc(), TaskRow.task_id.desc())
        )
        return None if row is None else _row_to_task(row)

    def list_tasks_for_conversation(
        self,
        conversation_id: str,
        statuses: Iterable[TaskStatus] | None = None,
    ) -> list[Task]:
        query = select(TaskRow).where(TaskRow.conversation_id == conversation_id)
        if statuses is not None:
            query = query.where(TaskRow.status.in_([str(status) for status in statuses]))
        rows = self._session.scalars(query.order_by(TaskRow.created_at.desc(), TaskRow.task_id.desc())).all()
        return [_row_to_task(row) for row in rows]

    def save_task_node(self, node: TaskNode) -> TaskNode:
        _ensure_runtime_store_write_allowed_by_rust_contract("node_state_transition")
        row = TaskNodeRow(
            node_id=node.node_id,
            task_id=node.task_id,
            capability_id=node.capability_id,
            assigned_instance_id=node.assigned_instance_id,
            status=node.status,
            criticality=node.criticality,
            dependency_type=node.dependency_type,
            retry_policy=dict(node.retry_policy),
            timeout_policy=dict(node.timeout_policy),
            resource_class=node.resource_class,
            input_refs=list(node.input_refs),
            output_refs=list(node.output_refs),
            started_at=node.started_at,
            finished_at=node.finished_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task_node(merged)

    def get_task_node(self, node_id: str) -> TaskNode | None:
        row = self._session.get(TaskNodeRow, node_id)
        return None if row is None else _row_to_task_node(row)

    def list_task_nodes_for_task(self, task_id: str) -> list[TaskNode]:
        rows = self._session.scalars(
            select(TaskNodeRow).where(TaskNodeRow.task_id == task_id).order_by(TaskNodeRow.node_id)
        ).all()
        return [_row_to_task_node(row) for row in rows]

    def save_task_edge(self, task_id: str, edge: TaskEdge) -> TaskEdge:
        _ensure_runtime_store_write_allowed_by_rust_contract("task_edge_save")
        row = TaskEdgeRow(
            edge_id=build_task_edge_id(task_id, edge.from_node_id, edge.to_node_id),
            task_id=task_id,
            from_node_id=edge.from_node_id,
            to_node_id=edge.to_node_id,
            edge_type=edge.edge_type,
            condition=edge.condition,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task_edge(merged)

    def list_task_edges(self, task_id: str) -> list[TaskEdge]:
        rows = self._session.scalars(
            select(TaskEdgeRow).where(TaskEdgeRow.task_id == task_id).order_by(TaskEdgeRow.from_node_id, TaskEdgeRow.to_node_id)
        ).all()
        return [_row_to_task_edge(row) for row in rows]

    def save_artifact(self, artifact: Artifact) -> Artifact:
        _ensure_runtime_store_write_allowed_by_rust_contract("artifact_save")
        row = ArtifactRow(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            producer_node_id=artifact.producer_node_id,
            artifact_type=artifact.artifact_type,
            storage_ref=artifact.storage_ref,
            summary=artifact.summary,
            is_complete=artifact.is_complete,
            created_at=artifact.created_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_artifact(merged)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        row = self._session.get(ArtifactRow, artifact_id)
        return None if row is None else _row_to_artifact(row)

    def list_artifacts_for_task(self, task_id: str) -> list[Artifact]:
        rows = self._session.scalars(
            select(ArtifactRow).where(ArtifactRow.task_id == task_id).order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
        ).all()
        return [_row_to_artifact(row) for row in rows]


class SQLiteCollaborationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_event_record(self, event: EventRecord) -> EventRecord:
        _ensure_event_append_payload_within_rust_contract(event)
        row = EventRecordRow(
            event_id=event.event_id,
            conversation_id=event.conversation_id,
            task_id=event.task_id,
            node_id=event.node_id,
            agent_id=event.agent_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            visibility=event.visibility,
            created_at=event.created_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_event_record(merged)

    def get_event_record(self, event_id: str) -> EventRecord | None:
        row = self._session.get(EventRecordRow, event_id)
        return None if row is None else _row_to_event_record(row)

    def list_events_for_task(self, task_id: str) -> list[EventRecord]:
        event_limit, byte_limit = _ensure_event_replay_policy_compatible_with_rust_contract()
        rows = self._session.scalars(
            select(EventRecordRow)
            .where(EventRecordRow.task_id == task_id)
            .order_by(EventRecordRow.created_at, EventRecordRow.event_id)
            .limit(event_limit + 1)
        ).all()
        events = [_row_to_event_record(row) for row in rows]
        _ensure_event_replay_page_within_rust_contract(events, event_limit, byte_limit)
        return events

    def list_event_page_for_task(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        event_limit, byte_limit = _ensure_event_replay_policy_compatible_with_rust_contract()
        resolved_limit = _resolve_event_replay_page_limit(limit, event_limit)
        query = (
            select(EventRecordRow)
            .where(EventRecordRow.task_id == task_id)
            .order_by(EventRecordRow.created_at, EventRecordRow.event_id)
            .limit(resolved_limit)
        )
        if after_event_id is not None:
            cursor = self._session.get(EventRecordRow, after_event_id)
            if cursor is None or cursor.task_id != task_id:
                raise ValueError(f"Unknown event replay cursor: {after_event_id}")
            if cursor.created_at is None:
                query = query.where(
                    or_(
                        EventRecordRow.created_at.is_not(None),
                        (EventRecordRow.created_at.is_(None)) & (EventRecordRow.event_id > after_event_id),
                    )
                )
            else:
                query = query.where(
                    or_(
                        EventRecordRow.created_at > cursor.created_at,
                        (EventRecordRow.created_at == cursor.created_at) & (EventRecordRow.event_id > after_event_id),
                    )
                )
        events = [_row_to_event_record(row) for row in self._session.scalars(query).all()]
        _ensure_event_replay_page_within_rust_contract(events, event_limit, byte_limit)
        return events

    def save_mailbox_message(self, message: MailboxMessage) -> MailboxMessage:
        row = MailboxMessageRow(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            task_id=message.task_id,
            node_id=message.node_id,
            parent_message_id=message.parent_message_id,
            correlation_id=message.correlation_id,
            from_agent=message.from_agent,
            to_agent=message.to_agent,
            to_role=message.to_role,
            channel=message.channel,
            message_type=message.message_type,
            ack_policy=message.ack_policy,
            priority=message.priority,
            payload=dict(message.payload),
            payload_schema_version=message.payload_schema_version,
            created_at=message.created_at,
            resolved_at=message.resolved_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_mailbox_message(merged)

    def get_mailbox_message(self, message_id: str) -> MailboxMessage | None:
        row = self._session.get(MailboxMessageRow, message_id)
        return None if row is None else _row_to_mailbox_message(row)

    def list_mailbox_messages_for_task(self, task_id: str) -> list[MailboxMessage]:
        rows = self._session.scalars(
            select(MailboxMessageRow).where(MailboxMessageRow.task_id == task_id).order_by(
                MailboxMessageRow.created_at, MailboxMessageRow.message_id
            )
        ).all()
        return [_row_to_mailbox_message(row) for row in rows]

    def save_mailbox_delivery(self, delivery: MailboxDelivery) -> MailboxDelivery:
        existing = self._session.scalar(
            select(MailboxDeliveryRow).where(
                MailboxDeliveryRow.message_id == delivery.message_id,
                MailboxDeliveryRow.recipient_agent == delivery.recipient_agent,
            )
        )
        if existing is None:
            row = MailboxDeliveryRow(
                delivery_id=delivery.delivery_id,
                message_id=delivery.message_id,
                recipient_agent=delivery.recipient_agent,
                recipient_role=delivery.recipient_role,
                status=delivery.status,
                attempt_count=delivery.attempt_count,
                max_attempts=delivery.max_attempts,
                ttl_seconds=delivery.ttl_seconds,
                expires_at=delivery.expires_at,
                delivered_at=delivery.delivered_at,
                acknowledged_at=delivery.acknowledged_at,
                resolved_at=delivery.resolved_at,
                next_retry_at=delivery.next_retry_at,
                last_error_code=delivery.last_error_code,
                last_error_message=delivery.last_error_message,
                created_at=delivery.created_at,
                updated_at=delivery.updated_at,
            )
            self._session.add(row)
            self._session.flush()
            return _row_to_mailbox_delivery(row)

        existing.recipient_role = delivery.recipient_role
        existing.status = delivery.status
        existing.attempt_count = delivery.attempt_count
        existing.max_attempts = delivery.max_attempts
        existing.ttl_seconds = delivery.ttl_seconds
        existing.expires_at = delivery.expires_at
        existing.delivered_at = delivery.delivered_at
        existing.acknowledged_at = delivery.acknowledged_at
        existing.resolved_at = delivery.resolved_at
        existing.next_retry_at = delivery.next_retry_at
        existing.last_error_code = delivery.last_error_code
        existing.last_error_message = delivery.last_error_message
        existing.created_at = delivery.created_at
        existing.updated_at = delivery.updated_at
        self._session.flush()
        return _row_to_mailbox_delivery(existing)

    def get_mailbox_delivery(self, delivery_id: str) -> MailboxDelivery | None:
        row = self._session.get(MailboxDeliveryRow, delivery_id)
        return None if row is None else _row_to_mailbox_delivery(row)

    def list_mailbox_deliveries_for_message(self, message_id: str) -> list[MailboxDelivery]:
        rows = self._session.scalars(
            select(MailboxDeliveryRow).where(MailboxDeliveryRow.message_id == message_id).order_by(
                MailboxDeliveryRow.created_at, MailboxDeliveryRow.delivery_id
            )
        ).all()
        return [_row_to_mailbox_delivery(row) for row in rows]

    def save_interrupt(self, interrupt: Interrupt) -> Interrupt:
        existing = self._session.get(InterruptRow, interrupt.interrupt_id)
        incoming_status = str(interrupt.status)
        if (
            existing is not None
            and str(existing.status) in lifecycle_status_list("interrupt_reopen_guard_terminal_statuses")
            and incoming_status == lifecycle_contract_value("interrupt_open_status")
        ):
            return _row_to_interrupt(existing)
        row = InterruptRow(
            interrupt_id=interrupt.interrupt_id,
            conversation_id=interrupt.conversation_id,
            task_id=interrupt.task_id,
            node_id=interrupt.node_id,
            source_agent=interrupt.source_agent,
            source_message_id=interrupt.source_message_id,
            question=interrupt.question,
            reason_code=interrupt.reason_code,
            required_fields=dict(interrupt.required_fields),
            status=interrupt.status,
            expires_at=interrupt.expires_at,
            created_at=interrupt.created_at,
            answered_at=interrupt.answered_at,
            cancelled_at=interrupt.cancelled_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_interrupt(merged)

    def get_interrupt(self, interrupt_id: str) -> Interrupt | None:
        row = self._session.get(InterruptRow, interrupt_id)
        return None if row is None else _row_to_interrupt(row)

    def get_interrupt_for_node(self, task_id: str, node_id: str) -> Interrupt | None:
        row = self._session.scalar(
            select(InterruptRow)
            .where(InterruptRow.task_id == task_id, InterruptRow.node_id == node_id)
            .order_by(InterruptRow.created_at.desc(), InterruptRow.interrupt_id.desc())
        )
        return None if row is None else _row_to_interrupt(row)

    def list_interrupts_for_task(self, task_id: str) -> list[Interrupt]:
        rows = self._session.scalars(
            select(InterruptRow).where(InterruptRow.task_id == task_id).order_by(InterruptRow.created_at, InterruptRow.interrupt_id)
        ).all()
        return [_row_to_interrupt(row) for row in rows]

    def save_interrupt_answer(self, interrupt_answer: InterruptAnswer) -> InterruptAnswer:
        row = InterruptAnswerRow(
            interrupt_answer_id=interrupt_answer.interrupt_answer_id,
            interrupt_id=interrupt_answer.interrupt_id,
            answer_payload=dict(interrupt_answer.answer_payload),
            source_message_id=interrupt_answer.source_message_id,
            accepted=interrupt_answer.accepted,
            created_at=interrupt_answer.created_at,
            accepted_at=interrupt_answer.accepted_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_interrupt_answer(merged)

    def get_interrupt_answer(self, interrupt_answer_id: str) -> InterruptAnswer | None:
        row = self._session.get(InterruptAnswerRow, interrupt_answer_id)
        return None if row is None else _row_to_interrupt_answer(row)

    def list_interrupt_answers(self, interrupt_id: str) -> list[InterruptAnswer]:
        rows = self._session.scalars(
            select(InterruptAnswerRow).where(InterruptAnswerRow.interrupt_id == interrupt_id).order_by(
                InterruptAnswerRow.created_at, InterruptAnswerRow.interrupt_answer_id
            )
        ).all()
        return [_row_to_interrupt_answer(row) for row in rows]

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        row = CheckpointRow(
            checkpoint_id=checkpoint.checkpoint_id,
            task_id=checkpoint.task_id,
            node_id=checkpoint.node_id,
            agent_id=checkpoint.agent_id,
            snapshot_ref=checkpoint.snapshot_ref,
            snapshot_kind=checkpoint.snapshot_kind,
            resume_token=checkpoint.resume_token,
            source_message_id=checkpoint.source_message_id,
            created_at=checkpoint.created_at,
            invalidated_at=checkpoint.invalidated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_checkpoint(merged)

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        row = self._session.get(CheckpointRow, checkpoint_id)
        return None if row is None else _row_to_checkpoint(row)

    def get_checkpoint_by_resume_token(self, resume_token: str) -> Checkpoint | None:
        row = self._session.scalar(
            select(CheckpointRow).where(CheckpointRow.resume_token == resume_token).order_by(
                CheckpointRow.created_at.desc(), CheckpointRow.checkpoint_id.desc()
            )
        )
        return None if row is None else _row_to_checkpoint(row)

    def list_checkpoints_for_task(self, task_id: str) -> list[Checkpoint]:
        rows = self._session.scalars(
            select(CheckpointRow).where(CheckpointRow.task_id == task_id).order_by(CheckpointRow.created_at, CheckpointRow.checkpoint_id)
        ).all()
        return [_row_to_checkpoint(row) for row in rows]


class SQLiteStorage(StoragePort):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        runtime_sidecar_client: Any | None = None,
        runtime_sidecar_shadow_sink: RuntimeSidecarShadowSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime_sidecar_client = runtime_sidecar_client
        self._runtime_sidecar_shadow_sink = runtime_sidecar_shadow_sink

    async def _run(
        self,
        callback: Callable[[SQLiteStateRepository, SQLiteCollaborationRepository], object],
    ) -> object:
        def _sync() -> object:
            with self._session_factory() as session:
                state_repo = SQLiteStateRepository(session)
                collab_repo = SQLiteCollaborationRepository(session)
                result = callback(state_repo, collab_repo)
                session.commit()
                return result

        return await asyncio.to_thread(_sync)

    async def save_auth_user_token(self, token: AuthUserToken) -> AuthUserToken:
        return await self._run(lambda state, collab: state.save_auth_user_token(token))

    async def get_auth_user_token(self, username: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_token(username))

    async def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_token_by_hash(api_token_hash))

    async def touch_auth_user_token_last_used(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.touch_auth_user_token_last_used(
                username,
                api_token_hash=api_token_hash,
                at=at,
            )
        )

    async def clear_auth_user_token(
        self,
        username: str,
        *,
        api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.clear_auth_user_token(
                username,
                api_token_hash=api_token_hash,
                at=at,
            )
        )

    async def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.rotate_auth_user_token(
                username,
                old_api_token_hash=old_api_token_hash,
                new_api_token_hash=new_api_token_hash,
                at=at,
            )
        )

    async def save_conversation(self, conversation: Conversation) -> Conversation:
        return await self._run(lambda state, collab: state.save_conversation(conversation))

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return await self._run(lambda state, collab: state.get_conversation(conversation_id))

    async def list_conversations_for_username(self, username: str) -> list[Conversation]:
        return await self._run(lambda state, collab: state.list_conversations_for_username(username))

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        return await self._run(lambda state, collab: state.delete_conversation(conversation_id))

    async def save_conversation_memory_summary(self, summary: ConversationMemorySummary) -> ConversationMemorySummary:
        return await self._run(lambda state, collab: state.save_conversation_memory_summary(summary))

    async def get_conversation_memory_summary(self, summary_id: str) -> ConversationMemorySummary | None:
        return await self._run(lambda state, collab: state.get_conversation_memory_summary(summary_id))

    async def get_latest_conversation_memory_summary(
        self,
        conversation_id: str,
        username: str | None = None,
    ) -> ConversationMemorySummary | None:
        return await self._run(
            lambda state, collab: state.get_latest_conversation_memory_summary(
                conversation_id,
                username=username,
            )
        )

    async def list_conversation_memory_summaries(self, conversation_id: str) -> list[ConversationMemorySummary]:
        return await self._run(lambda state, collab: state.list_conversation_memory_summaries(conversation_id))

    async def delete_conversation_memory_summaries_for_conversation(self, conversation_id: str) -> int:
        return await self._run(lambda state, collab: state.delete_conversation_memory_summaries_for_conversation(conversation_id))

    async def save_pending_skill_context(self, context: PendingSkillContext) -> PendingSkillContext:
        return await self._run(lambda state, collab: state.save_pending_skill_context(context))

    async def get_pending_skill_context(self, context_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.get_pending_skill_context(context_id))

    async def get_active_pending_skill_context(self, conversation_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.get_active_pending_skill_context(conversation_id))

    async def mark_pending_skill_context_consumed(self, context_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.mark_pending_skill_context_consumed(context_id, updated_at=_utcnow_naive()))

    async def mark_pending_skill_context_cancelled(self, context_id: str) -> PendingSkillContext | None:
        return await self._run(lambda state, collab: state.mark_pending_skill_context_cancelled(context_id, updated_at=_utcnow_naive()))

    async def mark_pending_skill_context_superseded(self, conversation_id: str) -> int:
        return await self._run(lambda state, collab: state.mark_pending_skill_context_superseded(conversation_id, updated_at=_utcnow_naive()))

    async def save_message(self, message: Message) -> Message:
        return await self._run(lambda state, collab: state.save_message(message))

    async def get_message(self, message_id: str) -> Message | None:
        return await self._run(lambda state, collab: state.get_message(message_id))

    async def list_messages_for_conversation(self, conversation_id: str) -> list[Message]:
        return await self._run(lambda state, collab: state.list_messages_for_conversation(conversation_id))

    async def save_task(self, task: Task) -> Task:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_submit",
            unavailable_error_code="runtime_store_unavailable",
        )
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.submit_task(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    idempotency_key=task.task_id,
                )
            )
            _consume_runtime_sidecar_response("task_submit", response)
            return task
        saved = await self._run(lambda state, collab: state.save_task(task))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_submit",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "conversation_id": task.conversation_id,
                "task_id": task.task_id,
            },
            legacy_output={
                "task_id": saved.task_id,
            },
            rust_call=lambda: self._runtime_sidecar_client.submit_task(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                idempotency_key=task.task_id,
            ),
            rust_output=lambda envelope: {
                "task_id": str(envelope.get("task_id", "")),
            },
        )
        return saved

    async def get_task(self, task_id: str) -> Task | None:
        return await self._run(lambda state, collab: state.get_task(task_id))

    async def get_active_task_for_conversation(self, conversation_id: str) -> Task | None:
        return await self._run(lambda state, collab: state.get_active_task_for_conversation(conversation_id))

    async def list_tasks_for_conversation(
        self,
        conversation_id: str,
        statuses: Iterable[TaskStatus] | None = None,
    ) -> list[Task]:
        return await self._run(lambda state, collab: state.list_tasks_for_conversation(conversation_id, statuses=statuses))

    async def save_task_node(self, node: TaskNode) -> TaskNode:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="node_state_transition",
            unavailable_error_code="runtime_store_unavailable",
        )
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.transition_node(
                    task_id=node.task_id,
                    node_id=node.node_id,
                    to_status=str(node.status),
                    idempotency_key=_runtime_sidecar_idempotency_key(node.node_id, str(node.status)),
                )
            )
            _consume_runtime_sidecar_response("node_state_transition", response)
            return node
        saved = await self._run(lambda state, collab: state.save_task_node(node))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="node_state_transition",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "node_id": node.node_id,
                "status": str(node.status),
                "task_id": node.task_id,
            },
            legacy_output={
                "node_id": saved.node_id,
                "status": str(saved.status),
            },
            rust_call=lambda: self._runtime_sidecar_client.transition_node(
                task_id=node.task_id,
                node_id=node.node_id,
                to_status=str(node.status),
                idempotency_key=_runtime_sidecar_idempotency_key(node.node_id, str(node.status)),
            ),
            rust_output=lambda envelope: {
                "node_id": str(envelope.get("node_id", "")),
                "status": str(envelope.get("status", "")),
            },
        )
        return saved

    async def get_task_node(self, node_id: str) -> TaskNode | None:
        return await self._run(lambda state, collab: state.get_task_node(node_id))

    async def list_task_nodes_for_task(self, task_id: str) -> list[TaskNode]:
        return await self._run(lambda state, collab: state.list_task_nodes_for_task(task_id))

    async def save_task_edge(self, task_id: str, edge: TaskEdge) -> TaskEdge:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="task_edge_save",
            unavailable_error_code="runtime_store_unavailable",
        )
        idempotency_key = build_task_edge_id(task_id, edge.from_node_id, edge.to_node_id)
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.save_task_edge(
                    task_id=task_id,
                    from_node_id=edge.from_node_id,
                    to_node_id=edge.to_node_id,
                    edge_type=str(edge.edge_type),
                    condition=edge.condition or "",
                    idempotency_key=idempotency_key,
                )
            )
            _consume_runtime_sidecar_response("task_edge_save", response)
            return edge
        saved = await self._run(lambda state, collab: state.save_task_edge(task_id, edge))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="task_edge_save",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload=_task_edge_shadow_payload(task_id, edge),
            legacy_output=_task_edge_shadow_payload(task_id, saved),
            rust_call=lambda: self._runtime_sidecar_client.save_task_edge(
                task_id=task_id,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                edge_type=str(edge.edge_type),
                condition=edge.condition or "",
                idempotency_key=idempotency_key,
            ),
            rust_output=lambda envelope: _task_edge_shadow_payload_from_record(envelope["edge"]),
        )
        return saved

    async def list_task_edges(self, task_id: str) -> list[TaskEdge]:
        if runtime_mode_for_component("runtime_store") == "enforce" and self._runtime_sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                self._runtime_sidecar_client.list_task_edges(task_id=task_id)
            )
            envelope = validate_runtime_sidecar_response(
                "task_edge_list",
                normalize_runtime_sidecar_response("task_edge_list", response),
            )
            return [_task_edge_from_sidecar_record(record) for record in envelope["edges"]]
        return await self._run(lambda state, collab: state.list_task_edges(task_id))

    async def save_artifact(self, artifact: Artifact) -> Artifact:
        sidecar_client = self._runtime_sidecar_client_for(
            component="runtime_store",
            operation_name="artifact_save",
            unavailable_error_code="runtime_store_unavailable",
        )
        record = _artifact_to_sidecar_record(artifact)
        if sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.save_artifact(
                    artifact_id=artifact.artifact_id,
                    task_id=artifact.task_id,
                    producer_node_id=artifact.producer_node_id,
                    artifact_type=str(artifact.artifact_type),
                    storage_ref=artifact.storage_ref,
                    summary=artifact.summary or "",
                    is_complete=artifact.is_complete,
                    created_at=record["created_at"],
                    idempotency_key=artifact.artifact_id,
                )
            )
            _consume_runtime_sidecar_response("artifact_save", response)
            return artifact
        saved = await self._run(lambda state, collab: state.save_artifact(artifact))
        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="artifact_save",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload=_artifact_shadow_payload(artifact),
            legacy_output=_artifact_shadow_payload(saved),
            rust_call=lambda: self._runtime_sidecar_client.save_artifact(
                artifact_id=artifact.artifact_id,
                task_id=artifact.task_id,
                producer_node_id=artifact.producer_node_id,
                artifact_type=str(artifact.artifact_type),
                storage_ref=artifact.storage_ref,
                summary=artifact.summary or "",
                is_complete=artifact.is_complete,
                created_at=record["created_at"],
                idempotency_key=artifact.artifact_id,
            ),
            rust_output=lambda envelope: _artifact_shadow_payload_from_record(envelope["artifact"]),
        )
        return saved

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        if runtime_mode_for_component("runtime_store") == "enforce" and self._runtime_sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                self._runtime_sidecar_client.get_artifact(artifact_id=artifact_id)
            )
            envelope = validate_runtime_sidecar_response(
                "artifact_get",
                normalize_runtime_sidecar_response("artifact_get", response),
            )
            if not envelope["found"]:
                return None
            return _artifact_from_sidecar_record(envelope["artifact"])
        return await self._run(lambda state, collab: state.get_artifact(artifact_id))

    async def list_artifacts_for_task(self, task_id: str) -> list[Artifact]:
        if runtime_mode_for_component("runtime_store") == "enforce" and self._runtime_sidecar_client is not None:
            response = await _resolve_runtime_sidecar_call(
                self._runtime_sidecar_client.list_artifacts_for_task(task_id=task_id)
            )
            envelope = validate_runtime_sidecar_response(
                "artifact_list",
                normalize_runtime_sidecar_response("artifact_list", response),
            )
            return [_artifact_from_sidecar_record(record) for record in envelope["artifacts"]]
        return await self._run(lambda state, collab: state.list_artifacts_for_task(task_id))

    async def append_event(self, event: EventRecord) -> EventRecord:
        sidecar_client = self._runtime_sidecar_client_for(
            component="event_log",
            operation_name="event_append",
            unavailable_error_code="event_log_unavailable",
        )
        if sidecar_client is not None:
            _ensure_event_append_payload_within_rust_limit(event)
            response = await _resolve_runtime_sidecar_call(
                sidecar_client.append_event(
                    conversation_id=event.conversation_id,
                    task_id=event.task_id,
                    event_type=event.event_type,
                    payload_json=json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"),
                    idempotency_key=event.event_id,
                )
            )
            _consume_runtime_sidecar_response("event_append", response)
            return event
        saved = await self._run(lambda state, collab: collab.save_event_record(event))
        payload_json = json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8")
        await record_runtime_sidecar_shadow_write(
            component="event_log",
            operation_name="event_append",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "conversation_id": event.conversation_id,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload_sha256": hashlib.sha256(payload_json).hexdigest(),
                "task_id": event.task_id,
            },
            legacy_output={
                "accepted": "true",
                "conversation_id": saved.conversation_id,
                "task_id": saved.task_id,
            },
            rust_call=lambda: self._runtime_sidecar_client.append_event(
                conversation_id=event.conversation_id,
                task_id=event.task_id,
                event_type=event.event_type,
                payload_json=payload_json,
                idempotency_key=event.event_id,
            ),
            rust_output=lambda envelope: {
                "accepted": "true",
                "conversation_id": str(envelope.get("cursor", {}).get("conversation_id", "")),
                "task_id": str(envelope.get("cursor", {}).get("task_id", "")),
            },
        )
        return saved

    async def list_events_for_task(self, task_id: str) -> list[EventRecord]:
        return await self._run(lambda state, collab: collab.list_events_for_task(task_id))

    async def list_event_page_for_task(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        return await self._run(
            lambda state, collab: collab.list_event_page_for_task(
                task_id,
                after_event_id=after_event_id,
                limit=limit,
            )
        )

    async def save_mailbox_message(self, message: MailboxMessage) -> MailboxMessage:
        return await self._run(lambda state, collab: collab.save_mailbox_message(message))

    async def get_mailbox_message(self, message_id: str) -> MailboxMessage | None:
        return await self._run(lambda state, collab: collab.get_mailbox_message(message_id))

    async def save_mailbox_delivery(self, delivery: MailboxDelivery) -> MailboxDelivery:
        return await self._run(lambda state, collab: collab.save_mailbox_delivery(delivery))

    async def get_mailbox_delivery(self, delivery_id: str) -> MailboxDelivery | None:
        return await self._run(lambda state, collab: collab.get_mailbox_delivery(delivery_id))

    async def list_mailbox_messages_for_task(self, task_id: str) -> list[MailboxMessage]:
        return await self._run(lambda state, collab: collab.list_mailbox_messages_for_task(task_id))

    async def list_mailbox_deliveries_for_message(self, message_id: str) -> list[MailboxDelivery]:
        return await self._run(lambda state, collab: collab.list_mailbox_deliveries_for_message(message_id))

    async def save_interrupt(self, interrupt: Interrupt) -> Interrupt:
        return await self._run(lambda state, collab: collab.save_interrupt(interrupt))

    async def get_interrupt(self, interrupt_id: str) -> Interrupt | None:
        return await self._run(lambda state, collab: collab.get_interrupt(interrupt_id))

    async def get_interrupt_for_node(self, task_id: str, node_id: str) -> Interrupt | None:
        return await self._run(lambda state, collab: collab.get_interrupt_for_node(task_id, node_id))

    async def list_interrupts_for_task(self, task_id: str) -> list[Interrupt]:
        return await self._run(lambda state, collab: collab.list_interrupts_for_task(task_id))

    async def save_interrupt_answer(self, interrupt_answer: InterruptAnswer) -> InterruptAnswer:
        return await self._run(lambda state, collab: collab.save_interrupt_answer(interrupt_answer))

    async def get_interrupt_answer(self, interrupt_answer_id: str) -> InterruptAnswer | None:
        return await self._run(lambda state, collab: collab.get_interrupt_answer(interrupt_answer_id))

    async def list_interrupt_answers(self, interrupt_id: str) -> list[InterruptAnswer]:
        return await self._run(lambda state, collab: collab.list_interrupt_answers(interrupt_id))

    async def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        return await self._run(lambda state, collab: collab.save_checkpoint(checkpoint))

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        return await self._run(lambda state, collab: collab.get_checkpoint(checkpoint_id))

    async def get_checkpoint_by_resume_token(self, resume_token: str) -> Checkpoint | None:
        return await self._run(lambda state, collab: collab.get_checkpoint_by_resume_token(resume_token))

    async def list_checkpoints_for_task(self, task_id: str) -> list[Checkpoint]:
        return await self._run(lambda state, collab: collab.list_checkpoints_for_task(task_id))

    def _runtime_sidecar_client_for(
        self,
        *,
        component: str,
        operation_name: str,
        unavailable_error_code: str,
    ) -> Any | None:
        if runtime_mode_for_component(component) != "enforce":
            return None
        if self._runtime_sidecar_client is None:
            ensure_sidecar_write_allowed(
                component=component,
                operation_name=operation_name,
                unavailable_error_code=unavailable_error_code,
            )
        return self._runtime_sidecar_client

def _runtime_sidecar_idempotency_key(*parts: str) -> str:
    return ":".join(parts)


def _task_edge_from_sidecar_record(record: Mapping[str, Any]) -> TaskEdge:
    condition = str(record.get("condition", ""))
    return TaskEdge(
        from_node_id=str(record["from_node_id"]),
        to_node_id=str(record["to_node_id"]),
        edge_type=EdgeType(str(record["edge_type"])),
        condition=condition or None,
    )


def _task_edge_shadow_payload(task_id: str, edge: TaskEdge) -> dict[str, str]:
    return {
        "task_id": task_id,
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "edge_type": str(edge.edge_type),
        "condition_sha256": hashlib.sha256((edge.condition or "").encode("utf-8")).hexdigest(),
    }


def _task_edge_shadow_payload_from_record(record: Mapping[str, Any]) -> dict[str, str]:
    return {
        "task_id": str(record.get("task_id", "")),
        "from_node_id": str(record.get("from_node_id", "")),
        "to_node_id": str(record.get("to_node_id", "")),
        "edge_type": str(record.get("edge_type", "")),
        "condition_sha256": hashlib.sha256(str(record.get("condition", "")).encode("utf-8")).hexdigest(),
    }


def _artifact_to_sidecar_record(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "task_id": artifact.task_id,
        "producer_node_id": artifact.producer_node_id,
        "artifact_type": str(artifact.artifact_type),
        "storage_ref": artifact.storage_ref,
        "summary": artifact.summary or "",
        "is_complete": artifact.is_complete,
        "created_at": artifact.created_at.isoformat() if artifact.created_at is not None else "",
    }


def _artifact_from_sidecar_record(record: Mapping[str, Any]) -> Artifact:
    created_at = str(record.get("created_at", ""))
    return Artifact(
        artifact_id=str(record["artifact_id"]),
        task_id=str(record["task_id"]),
        producer_node_id=str(record["producer_node_id"]),
        artifact_type=ArtifactType(str(record["artifact_type"])),
        storage_ref=str(record["storage_ref"]),
        summary=str(record.get("summary", "")) or None,
        is_complete=bool(record["is_complete"]),
        created_at=datetime.fromisoformat(created_at) if created_at else None,
    )


def _artifact_shadow_payload(artifact: Artifact) -> dict[str, str]:
    return {
        "artifact_id": artifact.artifact_id,
        "task_id": artifact.task_id,
        "producer_node_id": artifact.producer_node_id,
        "artifact_type": str(artifact.artifact_type),
        "storage_ref_sha256": hashlib.sha256(artifact.storage_ref.encode("utf-8")).hexdigest(),
        "summary_sha256": hashlib.sha256((artifact.summary or "").encode("utf-8")).hexdigest(),
        "is_complete": str(artifact.is_complete),
    }


def _artifact_shadow_payload_from_record(record: Mapping[str, Any]) -> dict[str, str]:
    storage_ref = str(record.get("storage_ref", ""))
    summary = str(record.get("summary", ""))
    return {
        "artifact_id": str(record.get("artifact_id", "")),
        "task_id": str(record.get("task_id", "")),
        "producer_node_id": str(record.get("producer_node_id", "")),
        "artifact_type": str(record.get("artifact_type", "")),
        "storage_ref_sha256": hashlib.sha256(storage_ref.encode("utf-8")).hexdigest(),
        "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "is_complete": str(bool(record.get("is_complete", False))),
    }


def _consume_runtime_sidecar_response(operation_name: str, response: Any) -> None:
    envelope = validate_runtime_sidecar_response(
        operation_name,
        normalize_runtime_sidecar_response(operation_name, response),
    )
    error = envelope.get("error")
    if isinstance(error, dict):
        raise RuntimeError(f"{error['code']}: {error['message']}")


async def _resolve_runtime_sidecar_call(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def _ensure_event_append_payload_within_rust_limit(event: EventRecord) -> None:
    payload_size = len(json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8"))
    limit = runtime_resource_limit("event_payload_bytes")
    if payload_size > limit:
        error_code = runtime_error_policy("event_log_payload_too_large")["code"]
        raise ValueError(f"{error_code}: event payload exceeds Rust runtime sidecar limit of {limit} bytes")
