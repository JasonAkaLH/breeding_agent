from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from src.auth.invalidation_bus import AuthGenerationChanged, AuthGenerationReason
from src.auth.postgres_invalidation_bus import auth_generation_notify_sql
from src.core.contracts import StoragePort
from src.core.enums import (
    ArtifactType,
    ConversationStatus,
    EdgeType,
    EventVisibility,
    MessageRole,
    TaskStatus,
    UserMCPAuthType,
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import (
    Artifact,
    AuthUserToken,
    Checkpoint,
    Conversation,
    ConversationMemorySummary,
    ConversationFileIndexRepairMarker,
    ConversationFileResource,
    EventRecord,
    FileUploadMessageProjection,
    Interrupt,
    InterruptAnswer,
    MailboxDelivery,
    MailboxMessage,
    Message,
    PendingSkillContext,
    SlotCollection,
    SlotEvent,
    Task,
    TaskEdge,
    TaskInputAttachment,
    TaskNode,
    UserMCPCredentialRecord,
    UserMCPHealthAttempt,
    UserMCPScopeLease,
    UserMCPServer,
    UserMCPToolGrant,
    MCPCredentialKeyValidation,
)
from src.storage.conversation_files import (
    FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
    FILE_UPLOAD_MESSAGE_TYPE,
    FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
    file_upload_message_audit_payload,
    file_upload_message_id,
    render_file_upload_message,
    safe_file_upload_message_metadata,
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
    ConversationFileIndexRepairMarkerRow,
    ConversationFileResourceRow,
    EventRecordRow,
    InterruptAnswerRow,
    InterruptRow,
    MailboxDeliveryRow,
    MailboxMessageRow,
    MessageRow,
    PendingSkillContextRow,
    SlotCollectionRow,
    SlotEventRow,
    TaskInputAttachmentRow,
    TaskEdgeRow,
    TaskNodeRow,
    TaskRow,
    UserMCPHealthAttemptRow,
    UserMCPScopeLeaseRow,
    UserMCPServerRow,
    UserMCPToolGrantRow,
    MCPCredentialKeyValidationRow,
)


CONVERSATION_FILE_INDEX_REPAIR_KIND = "conversation_file_index"


def _row_to_user_mcp_server(row: UserMCPServerRow) -> UserMCPServer:
    return UserMCPServer(
        server_id=row.server_id,
        owner_user_id=row.owner_user_id,
        display_name=row.display_name,
        routing_description=row.routing_description,
        endpoint_url=row.endpoint_url,
        transport=UserMCPTransport(row.transport),
        protocol_preference=UserMCPProtocolPreference(row.protocol_preference),
        auth_type=UserMCPAuthType(row.auth_type),
        auth_metadata=dict(row.auth_metadata or {}),
        enabled=bool(row.enabled),
        health_status=UserMCPHealthStatus(row.health_status),
        config_version=int(row.config_version),
        security_version=int(row.security_version),
        credential_configured=row.credential_ciphertext is not None,
        last_tested_at=row.last_tested_at,
        last_test_error_code=row.last_test_error_code,
        deletion_pending=bool(row.deletion_pending),
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_user_mcp_credential(row: UserMCPServerRow) -> UserMCPCredentialRecord | None:
    if row.credential_ciphertext is None or row.credential_nonce is None or row.encryption_version is None:
        return None
    return UserMCPCredentialRecord(
        owner_user_id=row.owner_user_id,
        server_id=row.server_id,
        credential_ciphertext=bytes(row.credential_ciphertext),
        credential_nonce=bytes(row.credential_nonce),
        encryption_version=int(row.encryption_version),
        credential_updated_at=row.credential_updated_at,
    )


def _row_to_user_mcp_health_attempt(row: UserMCPHealthAttemptRow) -> UserMCPHealthAttempt:
    return UserMCPHealthAttempt(
        attempt_id=row.attempt_id,
        owner_user_id=row.owner_user_id,
        server_id=row.server_id,
        config_version=int(row.config_version),
        security_version=int(row.security_version),
        runner_instance_id=row.runner_instance_id,
        lease_expires_at=cast(datetime, row.lease_expires_at),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_user_mcp_scope_lease(row: UserMCPScopeLeaseRow) -> UserMCPScopeLease:
    return UserMCPScopeLease(
        scope_id=row.scope_id,
        owner_user_id=row.owner_user_id,
        server_id=row.server_id,
        security_version=int(row.security_version),
        gateway_instance_id=row.gateway_instance_id,
        lease_expires_at=cast(datetime, row.lease_expires_at),
        created_at=row.created_at,
        updated_at=row.updated_at,
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
        delete_runner_id=row.delete_runner_id,
        delete_requested_at=row.delete_requested_at,
        delete_started_at=row.delete_started_at,
        delete_finished_at=row.delete_finished_at,
        delete_failed_at=row.delete_failed_at,
        delete_error_code=row.delete_error_code,
        delete_error_summary=row.delete_error_summary,
        delete_phase=row.delete_phase,
    )


def _row_to_conversation_file_resource(row: ConversationFileResourceRow) -> ConversationFileResource:
    return ConversationFileResource(
        file_id=row.file_id,
        conversation_id=row.conversation_id,
        username=row.username,
        original_filename=row.original_filename,
        content_type=row.content_type,
        file_type=row.file_type,
        size_bytes=int(row.size_bytes or 0),
        sha256=row.sha256,
        storage_key=row.storage_key,
        preview=dict(row.preview or {}),
        description_status=row.description_status,
        description_summary=row.description_summary,
        description_ref=row.description_ref,
        status=row.status,
        normalized_filename=row.normalized_filename,
        normalized_content_type=row.normalized_content_type,
        requires_sheet_selection=bool(row.requires_sheet_selection),
        selected_sheet=row.selected_sheet,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _normalize_repair_upload_ids(value: object) -> tuple[str, ...]:
    if value is None or isinstance(value, str):
        values = () if value is None else (value,)
    elif isinstance(value, Iterable):
        values = tuple(value)
    else:
        values = ()
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        upload_id = str(item or "").strip()
        if not upload_id or upload_id in seen:
            continue
        normalized.append(upload_id)
        seen.add(upload_id)
    return tuple(normalized)


def _merge_repair_upload_ids(existing: object, incoming: Iterable[str]) -> list[str]:
    return list(_normalize_repair_upload_ids((*_normalize_repair_upload_ids(existing), *tuple(incoming))))


def _repair_next_retry_at(now: datetime, attempt_count: int) -> datetime:
    if attempt_count <= 1:
        return now + timedelta(seconds=5)
    if attempt_count == 2:
        return now + timedelta(seconds=30)
    return now + timedelta(seconds=120)


def _row_to_conversation_file_index_repair_marker(
    row: ConversationFileIndexRepairMarkerRow,
) -> ConversationFileIndexRepairMarker:
    return ConversationFileIndexRepairMarker(
        conversation_id=row.conversation_id,
        repair_kind=row.repair_kind,
        status=row.status,
        reason_code=row.reason_code,
        affected_upload_ids=_normalize_repair_upload_ids(row.affected_upload_ids),
        attempt_count=int(row.attempt_count or 0),
        next_retry_at=row.next_retry_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        resolved_at=row.resolved_at,
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
        auth_generation=int(row.auth_generation or 0),
        auth_generation_updated_at=row.auth_generation_updated_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_metadata_object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _message_type_value(value: object) -> str:
    text = str(value or "").strip()
    return text or "chat"


def _file_upload_audit_event_id(
    *,
    event_type: str,
    conversation_id: str,
    upload_id: str,
    outcome: str,
    reason_code: str | None,
    at: datetime,
) -> str:
    serialized = json.dumps(
        {
            "at": at.isoformat(),
            "conversation_id": conversation_id,
            "event_type": event_type,
            "outcome": outcome,
            "reason_code": reason_code,
            "upload_id": upload_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f"file_upload_audit:{upload_id}:{event_type.rsplit('.', 1)[-1]}:{digest}"


def _file_upload_message_error_reason(message: str) -> str:
    if "another conversation" in message:
        return "conversation_mismatch"
    if "non-file_upload" in message:
        return "message_type_conflict"
    if "resurrected" in message:
        return "deleted_no_resurrection"
    return "repository_error"


def _row_to_message(row: MessageRow) -> Message:
    return Message(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        task_id=row.task_id,
        stream_status=row.stream_status,
        created_at=row.created_at,
        message_type=_message_type_value(getattr(row, "message_type", None)),
        metadata=_message_metadata_object(getattr(row, "message_metadata", None)),
        updated_at=getattr(row, "updated_at", None),
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


def _row_to_task_input_attachment(row: TaskInputAttachmentRow) -> TaskInputAttachment:
    return TaskInputAttachment(
        attachment_id=row.attachment_id,
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        source_kind=row.source_kind,
        source_upload_id=row.source_upload_id,
        source_message_id=row.source_message_id,
        interrupt_answer_id=row.interrupt_answer_id,
        filename=row.filename,
        content_type=row.content_type,
        file_type=row.file_type,
        size_bytes=int(row.size_bytes or 0),
        sha256=row.sha256,
        prompt_artifact=dict(row.prompt_artifact or {}),
        skill_artifact=dict(row.skill_artifact or {}),
        source_payload=dict(row.source_payload or {}),
        selected_sheet=row.selected_sheet,
        created_at=row.created_at,
        updated_at=row.updated_at,
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


def _slot_collection_row_values(collection: SlotCollection) -> dict[str, object]:
    return {
        "task_id": collection.task_id,
        "node_id": collection.node_id,
        "conversation_id": collection.conversation_id,
        "capability_id": collection.capability_id,
        "skill_name": collection.skill_name,
        "kind": collection.kind,
        "status": collection.status,
        "round": collection.round,
        "revision": collection.revision,
        "selected_schema_id": collection.selected_schema_id,
        "selected_entrypoint": collection.selected_entrypoint,
        "skill_bundle_revision": collection.skill_bundle_revision,
        "contract_revision": collection.contract_revision,
        "schema_digest": collection.schema_digest,
        "schema_snapshot_json": dict(collection.schema_snapshot),
        "slots_json": dict(collection.slots),
        "resolved_json": dict(collection.resolved),
        "missing_json": list(collection.missing),
        "invalid_json": [dict(item) for item in collection.invalid],
        "last_question": collection.last_question,
        "created_at": collection.created_at,
        "updated_at": collection.updated_at,
        "completed_at": collection.completed_at,
        "cancelled_at": collection.cancelled_at,
        "failed_at": collection.failed_at,
    }


def _row_to_slot_collection(row: SlotCollectionRow) -> SlotCollection:
    return SlotCollection(
        collection_id=row.collection_id,
        task_id=row.task_id,
        node_id=row.node_id,
        conversation_id=row.conversation_id,
        capability_id=row.capability_id,
        skill_name=row.skill_name,
        kind=row.kind,
        status=row.status,
        round=int(row.round or 1),
        revision=int(row.revision or 0),
        selected_schema_id=row.selected_schema_id,
        selected_entrypoint=row.selected_entrypoint,
        skill_bundle_revision=row.skill_bundle_revision,
        contract_revision=row.contract_revision,
        schema_digest=row.schema_digest,
        schema_snapshot=dict(row.schema_snapshot_json or {}),
        slots=dict(row.slots_json or {}),
        resolved=dict(row.resolved_json or {}),
        missing=tuple(str(item) for item in (row.missing_json or ())),
        invalid=tuple(dict(item) for item in (row.invalid_json or ()) if isinstance(item, Mapping)),
        last_question=row.last_question,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        cancelled_at=row.cancelled_at,
        failed_at=row.failed_at,
    )


def _row_to_slot_event(row: SlotEventRow) -> SlotEvent:
    return SlotEvent(
        slot_event_id=row.slot_event_id,
        collection_id=row.collection_id,
        task_id=row.task_id,
        node_id=row.node_id,
        conversation_id=row.conversation_id,
        event_type=row.event_type,
        round=int(row.round or 1),
        revision=int(row.revision or 0),
        idempotency_key=row.idempotency_key,
        payload=dict(row.payload_json or {}),
        created_at=row.created_at,
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

    def save_auth_user_token(self, token: AuthUserToken, *, auth_generation_reason: str | None = None) -> AuthUserToken:
        at = token.updated_at or token.auth_generation_updated_at or _utcnow_naive()
        existing = self._session.execute(
            select(AuthUserTokenRow).where(AuthUserTokenRow.username == token.username).with_for_update()
        ).scalar_one_or_none()
        if existing is None:
            row = AuthUserTokenRow(
                username=token.username,
                api_token_hash=token.api_token_hash,
                token_issued_at=token.token_issued_at,
                token_last_used_at=token.token_last_used_at,
                auth_generation=int(token.auth_generation or 1),
                auth_generation_updated_at=at,
                created_at=token.created_at or at,
                updated_at=at,
            )
            self._session.add(row)
            self._session.flush()
            saved = _row_to_auth_user_token(row)
        else:
            existing.api_token_hash = token.api_token_hash
            existing.token_issued_at = token.token_issued_at
            existing.token_last_used_at = token.token_last_used_at
            existing.auth_generation = int(existing.auth_generation or 0) + 1
            existing.auth_generation_updated_at = at
            existing.updated_at = at
            if existing.created_at is None:
                existing.created_at = token.created_at or at
            self._session.flush()
            saved = _row_to_auth_user_token(existing)
        self._notify_auth_generation_change(saved, auth_generation_reason, changed_at=at)
        return saved

    def get_auth_user_token(self, username: str) -> AuthUserToken | None:
        row = self._session.get(AuthUserTokenRow, username)
        return None if row is None else _row_to_auth_user_token(row)

    def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None:
        row = self._session.execute(
            select(AuthUserTokenRow).where(AuthUserTokenRow.api_token_hash == api_token_hash)
        ).scalar_one_or_none()
        return None if row is None else _row_to_auth_user_token(row)

    def get_auth_user_generation(self, username: str) -> AuthUserToken | None:
        return self.get_auth_user_token(username)

    def list_auth_user_generations(self) -> list[AuthUserToken]:
        rows = self._session.scalars(select(AuthUserTokenRow).order_by(AuthUserTokenRow.username)).all()
        return [_row_to_auth_user_token(row) for row in rows]

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
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None:
        result = self._session.execute(
            update(AuthUserTokenRow)
            .where(
                AuthUserTokenRow.username == username,
                AuthUserTokenRow.api_token_hash == api_token_hash,
            )
            .values(
                api_token_hash=None,
                token_issued_at=None,
                token_last_used_at=None,
                auth_generation=AuthUserTokenRow.auth_generation + 1,
                auth_generation_updated_at=at,
                updated_at=at,
            )
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        saved = None if row is None else _row_to_auth_user_token(row)
        if saved is not None:
            self._notify_auth_generation_change(saved, auth_generation_reason, changed_at=at)
        return saved

    def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
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
                auth_generation=AuthUserTokenRow.auth_generation + 1,
                auth_generation_updated_at=at,
                updated_at=at,
            )
        )
        if result.rowcount != 1:
            self._session.flush()
            return None
        self._session.flush()
        row = self._session.get(AuthUserTokenRow, username)
        saved = None if row is None else _row_to_auth_user_token(row)
        if saved is not None:
            self._notify_auth_generation_change(saved, auth_generation_reason, changed_at=at)
        return saved

    def _notify_auth_generation_change(
        self,
        token: AuthUserToken,
        reason: str | None,
        *,
        changed_at: datetime,
    ) -> None:
        if not reason:
            return
        bind = self._session.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return
        sql, params = auth_generation_notify_sql(
            AuthGenerationChanged(
                username=token.username,
                auth_generation=token.auth_generation,
                changed_at=changed_at,
                reason=cast(AuthGenerationReason, reason),
            )
        )
        self._session.execute(text(sql), params)

    def save_conversation(self, conversation: Conversation) -> Conversation:
        existing = self._session.get(ConversationRow, conversation.conversation_id)
        if (
            existing is not None
            and existing.status in {str(ConversationStatus.DELETING), str(ConversationStatus.DELETING_FAILED)}
            and str(conversation.status) == str(ConversationStatus.ACTIVE)
        ):
            raise ValueError(f"Conversation is not available: {conversation.conversation_id}")
        row = ConversationRow(
            conversation_id=conversation.conversation_id,
            username=conversation.username,
            status=conversation.status,
            current_task_id=conversation.current_task_id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            delete_runner_id=conversation.delete_runner_id,
            delete_requested_at=conversation.delete_requested_at,
            delete_started_at=conversation.delete_started_at,
            delete_finished_at=conversation.delete_finished_at,
            delete_failed_at=conversation.delete_failed_at,
            delete_error_code=conversation.delete_error_code,
            delete_error_summary=conversation.delete_error_summary,
            delete_phase=conversation.delete_phase,
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
            .where(ConversationRow.username == username, ConversationRow.status == str(ConversationStatus.ACTIVE))
            .order_by(ConversationRow.updated_at.desc(), ConversationRow.conversation_id.desc())
        ).all()
        return [_row_to_conversation(row) for row in rows]

    def list_deleting_conversations(self) -> list[Conversation]:
        rows = self._session.scalars(
            select(ConversationRow)
            .where(ConversationRow.status.in_([str(ConversationStatus.DELETING), str(ConversationStatus.DELETING_FAILED)]))
            .order_by(ConversationRow.updated_at.asc(), ConversationRow.conversation_id.asc())
        ).all()
        return [_row_to_conversation(row) for row in rows]

    def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None:
            return None
        if row.status == str(ConversationStatus.DELETING_FAILED):
            return _row_to_conversation(row)
        if row.status != str(ConversationStatus.DELETING):
            row.status = str(ConversationStatus.DELETING)
            row.delete_requested_at = requested_at
        row.delete_runner_id = row.delete_runner_id or runner_id
        if started_at is not None:
            row.delete_started_at = started_at
        row.delete_phase = phase
        row.delete_error_code = None
        row.delete_error_summary = None
        row.updated_at = requested_at
        self._session.flush()
        return _row_to_conversation(row)

    def update_conversation_delete_phase(
        self,
        conversation_id: str,
        *,
        phase: str,
        updated_at: datetime,
        runner_id: str | None = None,
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None:
            return None
        row.delete_phase = phase
        row.updated_at = updated_at
        if phase != "marking" and row.delete_started_at is None:
            row.delete_started_at = updated_at
        if runner_id is not None:
            row.delete_runner_id = runner_id
        self._session.flush()
        return _row_to_conversation(row)

    def mark_conversation_delete_failed(
        self,
        conversation_id: str,
        *,
        failed_at: datetime,
        phase: str,
        error_code: str,
        error_summary: str,
        runner_id: str | None = None,
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None:
            return None
        row.status = str(ConversationStatus.DELETING_FAILED)
        row.delete_failed_at = failed_at
        row.delete_finished_at = failed_at
        row.delete_phase = phase
        row.delete_error_code = error_code[:120]
        row.delete_error_summary = error_summary[:500]
        if runner_id is not None:
            row.delete_runner_id = runner_id
        row.updated_at = failed_at
        self._session.flush()
        return _row_to_conversation(row)

    def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        row = self._session.get(ConversationRow, conversation_id)
        if row is None or row.status != str(ConversationStatus.DELETING_FAILED):
            return None
        row.status = str(ConversationStatus.DELETING)
        row.delete_runner_id = runner_id
        row.delete_requested_at = requested_at
        row.delete_started_at = started_at
        row.delete_finished_at = None
        row.delete_failed_at = None
        row.delete_error_code = None
        row.delete_error_summary = None
        row.delete_phase = phase
        row.updated_at = requested_at
        self._session.flush()
        return _row_to_conversation(row)

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
        slot_collection_conditions = [SlotCollectionRow.conversation_id == conversation_id]
        slot_event_conditions = [SlotEventRow.conversation_id == conversation_id]
        if task_ids:
            mailbox_conditions.append(MailboxMessageRow.task_id.in_(task_ids))
            event_conditions.append(EventRecordRow.task_id.in_(task_ids))
            interrupt_conditions.append(InterruptRow.task_id.in_(task_ids))
            message_conditions.append(MessageRow.task_id.in_(task_ids))
            slot_collection_conditions.append(SlotCollectionRow.task_id.in_(task_ids))
            slot_event_conditions.append(SlotEventRow.task_id.in_(task_ids))

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
        slot_collection_ids = list(
            self._session.scalars(
                select(SlotCollectionRow.collection_id).where(or_(*slot_collection_conditions))
            ).all()
        )
        if slot_collection_ids:
            slot_event_conditions.append(SlotEventRow.collection_id.in_(slot_collection_ids))

        deleted_counts: dict[str, int] = {
            "conversation_file_resource": 0,
            "conversation_memory_summary": 0,
            "conversation_pending_skill_context": 0,
            "mailbox_delivery": 0,
            "interrupt_answer": 0,
            "slot_event": 0,
            "slot_collection": 0,
            "checkpoint": 0,
            "interrupt": 0,
            "mailbox_message": 0,
            "event_record": 0,
            "artifact": 0,
            "task_input_attachment": 0,
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
        _delete("slot_event", delete(SlotEventRow).where(or_(*slot_event_conditions)))
        _delete("slot_collection", delete(SlotCollectionRow).where(or_(*slot_collection_conditions)))
        if task_ids:
            _delete("checkpoint", delete(CheckpointRow).where(CheckpointRow.task_id.in_(task_ids)))
        _delete("interrupt", delete(InterruptRow).where(or_(*interrupt_conditions)))
        _delete("mailbox_message", delete(MailboxMessageRow).where(or_(*mailbox_conditions)))
        _delete("event_record", delete(EventRecordRow).where(or_(*event_conditions)))
        if task_ids:
            _delete("artifact", delete(ArtifactRow).where(ArtifactRow.task_id.in_(task_ids)))
            _delete(
                "task_input_attachment",
                delete(TaskInputAttachmentRow).where(TaskInputAttachmentRow.task_id.in_(task_ids)),
            )
            _delete("task_edge", delete(TaskEdgeRow).where(TaskEdgeRow.task_id.in_(task_ids)))
            _delete("task_node", delete(TaskNodeRow).where(TaskNodeRow.task_id.in_(task_ids)))
        _delete(
            "conversation_file_resource",
            delete(ConversationFileResourceRow).where(ConversationFileResourceRow.conversation_id == conversation_id),
        )
        _delete(
            "conversation_memory_summary",
            delete(ConversationMemorySummaryRow).where(ConversationMemorySummaryRow.conversation_id == conversation_id),
        )
        _delete(
            "conversation_pending_skill_context",
            delete(PendingSkillContextRow).where(PendingSkillContextRow.conversation_id == conversation_id),
        )
        _delete(
            "conversation_file_index_repair_marker",
            delete(ConversationFileIndexRepairMarkerRow).where(
                ConversationFileIndexRepairMarkerRow.conversation_id == conversation_id
            ),
        )
        _delete("message", delete(MessageRow).where(or_(*message_conditions)))
        _delete("task", delete(TaskRow).where(TaskRow.conversation_id == conversation_id))
        _delete("conversation", delete(ConversationRow).where(ConversationRow.conversation_id == conversation_id))
        self._session.flush()
        return deleted_counts

    def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]:
        return self.delete_conversation(conversation_id)

    def save_conversation_file_resource(self, resource: ConversationFileResource) -> ConversationFileResource:
        row = ConversationFileResourceRow(
            file_id=resource.file_id,
            conversation_id=resource.conversation_id,
            username=resource.username,
            original_filename=resource.original_filename,
            content_type=resource.content_type,
            file_type=resource.file_type,
            size_bytes=resource.size_bytes,
            sha256=resource.sha256,
            storage_key=resource.storage_key,
            preview=dict(resource.preview),
            description_status=resource.description_status,
            description_summary=resource.description_summary,
            description_ref=resource.description_ref,
            status=resource.status,
            normalized_filename=resource.normalized_filename,
            normalized_content_type=resource.normalized_content_type,
            requires_sheet_selection=resource.requires_sheet_selection,
            selected_sheet=resource.selected_sheet,
            created_at=resource.created_at,
            updated_at=resource.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_conversation_file_resource(merged)

    def get_conversation_file_resource(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
    ) -> ConversationFileResource | None:
        row = self._session.get(ConversationFileResourceRow, file_id)
        if row is None or row.conversation_id != conversation_id or row.username != username:
            return None
        return _row_to_conversation_file_resource(row)

    def get_conversation_file_resource_by_id(self, file_id: str) -> ConversationFileResource | None:
        row = self._session.get(ConversationFileResourceRow, file_id)
        return None if row is None else _row_to_conversation_file_resource(row)

    def list_conversation_file_resources(
        self,
        conversation_id: str,
        username: str | None = None,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[ConversationFileResource]:
        statement = select(ConversationFileResourceRow).where(ConversationFileResourceRow.conversation_id == conversation_id)
        if username is not None:
            statement = statement.where(ConversationFileResourceRow.username == username)
        if not include_deleted:
            statement = statement.where(ConversationFileResourceRow.status != "deleted")
        if cursor:
            cursor_statement = select(ConversationFileResourceRow).where(
                ConversationFileResourceRow.conversation_id == conversation_id,
                ConversationFileResourceRow.file_id == cursor,
            )
            if username is not None:
                cursor_statement = cursor_statement.where(ConversationFileResourceRow.username == username)
            cursor_row = self._session.scalars(cursor_statement).first()
            if cursor_row is None:
                return []
            statement = statement.where(
                or_(
                    ConversationFileResourceRow.created_at > cursor_row.created_at,
                    and_(
                        ConversationFileResourceRow.created_at == cursor_row.created_at,
                        ConversationFileResourceRow.file_id > cursor_row.file_id,
                    ),
                )
            )
        statement = statement.order_by(ConversationFileResourceRow.created_at, ConversationFileResourceRow.file_id)
        if limit is not None and limit > 0:
            statement = statement.limit(limit)
        rows = self._session.scalars(statement).all()
        return [_row_to_conversation_file_resource(row) for row in rows]

    def mark_conversation_file_resource_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        row = self._session.get(ConversationFileResourceRow, file_id)
        if row is None or row.conversation_id != conversation_id or row.username != username:
            return None
        row.status = "deleted"
        row.updated_at = updated_at
        self._session.flush()
        return _row_to_conversation_file_resource(row)

    def save_conversation_file_resource_with_upload_message(
        self,
        resource: ConversationFileResource,
        projection: FileUploadMessageProjection,
        *,
        now: datetime,
    ) -> ConversationFileResource:
        saved = self.save_conversation_file_resource(resource)
        self.upsert_file_upload_message(projection, now=now)
        return saved

    def mark_conversation_file_resource_and_upload_message_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        deleted = self.mark_conversation_file_resource_deleted(
            conversation_id,
            username,
            file_id,
            updated_at=updated_at,
        )
        if deleted is None:
            return None
        self.mark_file_upload_message_deleted(conversation_id, file_id, deleted_at=updated_at)
        return deleted

    def compensate_failed_conversation_file_upload(
        self,
        conversation_id: str,
        username: str,
        upload_id: str,
        *,
        reason_code: str,
        now: datetime,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "upload_id": upload_id,
            "reason_code": reason_code,
            "resource_deleted": 0,
            "message_deleted": 0,
            "status": "noop",
        }
        resource_row = self._session.get(ConversationFileResourceRow, upload_id)
        if (
            resource_row is not None
            and resource_row.conversation_id == conversation_id
            and resource_row.username == username
        ):
            self._session.delete(resource_row)
            result["resource_deleted"] = 1
        message_row = self._session.get(MessageRow, file_upload_message_id(upload_id))
        if (
            message_row is not None
            and message_row.conversation_id == conversation_id
            and _message_type_value(message_row.message_type) == FILE_UPLOAD_MESSAGE_TYPE
        ):
            self._session.delete(message_row)
            result["message_deleted"] = 1
        if result["resource_deleted"] or result["message_deleted"]:
            result["status"] = "removed"
        result["compensated_at"] = now.isoformat()
        self._session.flush()
        return result

    def record_conversation_file_index_repair_required(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        affected_upload_ids: Iterable[str] = (),
        now: datetime,
    ) -> ConversationFileIndexRepairMarker:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            attempt_count = 1
            row = ConversationFileIndexRepairMarkerRow(
                conversation_id=conversation_id,
                repair_kind=CONVERSATION_FILE_INDEX_REPAIR_KIND,
                status="pending",
                reason_code=str(reason_code or "index_write_failed"),
                affected_upload_ids=_merge_repair_upload_ids((), affected_upload_ids),
                attempt_count=attempt_count,
                next_retry_at=_repair_next_retry_at(now, attempt_count),
                created_at=now,
                updated_at=now,
                resolved_at=None,
            )
            self._session.add(row)
        else:
            attempt_count = int(row.attempt_count or 0) + 1
            row.status = "pending"
            row.reason_code = str(reason_code or row.reason_code or "index_write_failed")
            row.affected_upload_ids = _merge_repair_upload_ids(row.affected_upload_ids, affected_upload_ids)
            row.attempt_count = attempt_count
            row.next_retry_at = _repair_next_retry_at(now, attempt_count)
            row.updated_at = now
            row.resolved_at = None
            if row.created_at is None:
                row.created_at = now
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def get_conversation_file_index_repair_marker(
        self,
        conversation_id: str,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        return None if row is None else _row_to_conversation_file_index_repair_marker(row)

    def list_due_conversation_file_index_repairs(
        self,
        *,
        now: datetime,
        limit: int | None = None,
    ) -> list[ConversationFileIndexRepairMarker]:
        statement = (
            select(ConversationFileIndexRepairMarkerRow)
            .where(
                ConversationFileIndexRepairMarkerRow.repair_kind == CONVERSATION_FILE_INDEX_REPAIR_KIND,
                or_(
                    and_(
                        ConversationFileIndexRepairMarkerRow.status == "pending",
                        or_(
                            ConversationFileIndexRepairMarkerRow.next_retry_at.is_(None),
                            ConversationFileIndexRepairMarkerRow.next_retry_at <= now,
                        ),
                    ),
                    and_(
                        ConversationFileIndexRepairMarkerRow.status == "failed",
                        ConversationFileIndexRepairMarkerRow.next_retry_at.is_not(None),
                        ConversationFileIndexRepairMarkerRow.next_retry_at <= now,
                    ),
                ),
            )
            .order_by(
                ConversationFileIndexRepairMarkerRow.next_retry_at,
                ConversationFileIndexRepairMarkerRow.updated_at,
                ConversationFileIndexRepairMarkerRow.conversation_id,
            )
        )
        if limit is not None and limit > 0:
            statement = statement.limit(limit)
        rows = self._session.scalars(statement).all()
        return [_row_to_conversation_file_index_repair_marker(row) for row in rows]

    def mark_conversation_file_index_repairing(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            return None
        row.status = "repairing"
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.next_retry_at = None
        row.updated_at = now
        if row.created_at is None:
            row.created_at = now
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def mark_conversation_file_index_repair_resolved(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            return None
        row.status = "resolved"
        row.next_retry_at = None
        row.updated_at = now
        row.resolved_at = now
        if row.created_at is None:
            row.created_at = now
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def mark_conversation_file_index_repair_failed(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        now: datetime,
        retryable: bool = True,
    ) -> ConversationFileIndexRepairMarker | None:
        row = self._session.get(
            ConversationFileIndexRepairMarkerRow,
            (conversation_id, CONVERSATION_FILE_INDEX_REPAIR_KIND),
        )
        if row is None:
            return None
        row.status = "failed"
        row.reason_code = str(reason_code or row.reason_code or "index_repair_failed")
        row.updated_at = now
        row.resolved_at = None
        if row.created_at is None:
            row.created_at = now
        row.next_retry_at = _repair_next_retry_at(now, int(row.attempt_count or 0)) if retryable else None
        self._session.flush()
        return _row_to_conversation_file_index_repair_marker(row)

    def save_message(self, message: Message) -> Message:
        row = MessageRow(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            role=str(message.role),
            content=message.content,
            task_id=message.task_id,
            stream_status=message.stream_status,
            created_at=message.created_at,
            message_type=_message_type_value(message.message_type),
            message_metadata=_message_metadata_object(message.metadata),
            updated_at=message.updated_at,
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

    def upsert_file_upload_message(self, projection: FileUploadMessageProjection, *, now: datetime) -> Message:
        message_id = file_upload_message_id(projection.upload_id)
        metadata = safe_file_upload_message_metadata(projection.metadata, upload_id=projection.upload_id)
        content = render_file_upload_message(metadata)
        row = self._session.execute(
            select(MessageRow).where(MessageRow.message_id == message_id).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            row = MessageRow(
                message_id=message_id,
                conversation_id=projection.conversation_id,
                role=str(MessageRole.SYSTEM),
                content=content,
                task_id=None,
                stream_status="complete",
                created_at=projection.created_at or now,
                message_type=FILE_UPLOAD_MESSAGE_TYPE,
                message_metadata=metadata,
                updated_at=now,
            )
            self._session.add(row)
            self._session.flush()
            self.record_file_upload_message_audit(
                event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
                conversation_id=projection.conversation_id,
                upload_id=projection.upload_id,
                outcome="inserted",
                at=now,
                projection=FileUploadMessageProjection(
                    upload_id=projection.upload_id,
                    conversation_id=projection.conversation_id,
                    content=content,
                    metadata=metadata,
                    created_at=projection.created_at,
                ),
            )
            return _row_to_message(row)
        if row.conversation_id != projection.conversation_id:
            raise ValueError("file_upload message id belongs to another conversation")
        if _message_type_value(row.message_type) != FILE_UPLOAD_MESSAGE_TYPE:
            raise ValueError("file_upload message id conflicts with non-file_upload message")
        existing_metadata = _message_metadata_object(row.message_metadata)
        if existing_metadata.get("file_status") == "deleted" and metadata.get("file_status") != "deleted":
            raise ValueError("deleted file_upload message cannot be resurrected")
        row.role = str(MessageRole.SYSTEM)
        row.content = content
        row.task_id = None
        row.stream_status = "complete"
        row.message_type = FILE_UPLOAD_MESSAGE_TYPE
        row.message_metadata = metadata
        row.updated_at = now
        self._session.flush()
        self.record_file_upload_message_audit(
            event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
            conversation_id=projection.conversation_id,
            upload_id=projection.upload_id,
            outcome="updated",
            at=now,
            projection=FileUploadMessageProjection(
                upload_id=projection.upload_id,
                conversation_id=projection.conversation_id,
                content=content,
                metadata=metadata,
                created_at=projection.created_at,
            ),
        )
        return _row_to_message(row)

    def mark_file_upload_message_deleted(
        self,
        conversation_id: str,
        upload_id: str,
        *,
        deleted_at: datetime,
    ) -> Message | None:
        row = self._session.execute(
            select(MessageRow).where(MessageRow.message_id == file_upload_message_id(upload_id)).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            self.record_file_upload_message_audit(
                event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
                conversation_id=conversation_id,
                upload_id=upload_id,
                outcome="noop",
                reason_code="message_missing",
                at=deleted_at,
            )
            self._session.flush()
            return None
        if row.conversation_id != conversation_id:
            raise ValueError("file_upload message id belongs to another conversation")
        if _message_type_value(row.message_type) != FILE_UPLOAD_MESSAGE_TYPE:
            raise ValueError("file_upload message id conflicts with non-file_upload message")
        metadata = safe_file_upload_message_metadata(row.message_metadata, upload_id=upload_id)
        metadata["file_status"] = "deleted"
        row.role = str(MessageRole.SYSTEM)
        row.content = render_file_upload_message(metadata)
        row.task_id = None
        row.stream_status = "complete"
        row.message_type = FILE_UPLOAD_MESSAGE_TYPE
        row.message_metadata = metadata
        row.updated_at = deleted_at
        self._session.flush()
        self.record_file_upload_message_audit(
            event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
            conversation_id=conversation_id,
            upload_id=upload_id,
            outcome="marked_deleted",
            at=deleted_at,
            projection=FileUploadMessageProjection(
                upload_id=upload_id,
                conversation_id=conversation_id,
                content=row.content,
                metadata=metadata,
                created_at=row.created_at,
            ),
        )
        return _row_to_message(row)

    def record_file_upload_message_audit(
        self,
        *,
        event_type: str,
        conversation_id: str,
        upload_id: str,
        outcome: str,
        at: datetime,
        projection: FileUploadMessageProjection | None = None,
        reason_code: str | None = None,
    ) -> EventRecord:
        event = EventRecord(
            event_id=_file_upload_audit_event_id(
                event_type=event_type,
                conversation_id=conversation_id,
                upload_id=upload_id,
                outcome=outcome,
                reason_code=reason_code,
                at=at,
            ),
            conversation_id=conversation_id,
            task_id=f"conversation_file:{upload_id}",
            event_type=event_type,
            payload=file_upload_message_audit_payload(
                event_type=event_type,
                conversation_id=conversation_id,
                upload_id=upload_id,
                outcome=outcome,
                projection=projection,
                reason_code=reason_code,
            ),
            visibility=EventVisibility.AUDIT_ONLY,
            created_at=at,
        )
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

    def list_artifacts_for_conversation(self, conversation_id: str) -> list[Artifact]:
        rows = self._session.scalars(
            select(ArtifactRow)
            .join(TaskRow, ArtifactRow.task_id == TaskRow.task_id)
            .where(TaskRow.conversation_id == conversation_id)
            .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
        ).all()
        return [_row_to_artifact(row) for row in rows]

    def save_task_input_attachment(self, attachment: TaskInputAttachment) -> TaskInputAttachment:
        row = TaskInputAttachmentRow(
            attachment_id=attachment.attachment_id,
            task_id=attachment.task_id,
            conversation_id=attachment.conversation_id,
            source_kind=attachment.source_kind,
            source_upload_id=attachment.source_upload_id,
            source_message_id=attachment.source_message_id,
            interrupt_answer_id=attachment.interrupt_answer_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            file_type=attachment.file_type,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
            prompt_artifact=dict(attachment.prompt_artifact),
            skill_artifact=dict(attachment.skill_artifact),
            source_payload=dict(attachment.source_payload),
            selected_sheet=attachment.selected_sheet,
            created_at=attachment.created_at,
            updated_at=attachment.updated_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_task_input_attachment(merged)

    def list_task_input_attachments_for_task(self, task_id: str) -> list[TaskInputAttachment]:
        rows = self._session.scalars(
            select(TaskInputAttachmentRow)
            .where(TaskInputAttachmentRow.task_id == task_id)
            .order_by(TaskInputAttachmentRow.created_at, TaskInputAttachmentRow.attachment_id)
        ).all()
        return [_row_to_task_input_attachment(row) for row in rows]

    def list_task_input_attachments_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
    ) -> list[TaskInputAttachment]:
        statement = (
            select(TaskInputAttachmentRow)
            .where(TaskInputAttachmentRow.conversation_id == conversation_id)
            .order_by(TaskInputAttachmentRow.updated_at.desc(), TaskInputAttachmentRow.created_at.desc(), TaskInputAttachmentRow.attachment_id)
        )
        if limit is not None:
            statement = statement.limit(max(0, int(limit)))
        rows = self._session.scalars(statement).all()
        return [_row_to_task_input_attachment(row) for row in rows]

    def list_user_mcp_servers(self, owner_user_id: str) -> list[UserMCPServer]:
        rows = self._session.scalars(
            select(UserMCPServerRow)
            .where(
                UserMCPServerRow.owner_user_id == owner_user_id,
                UserMCPServerRow.deletion_pending.is_(False),
            )
            .order_by(UserMCPServerRow.created_at, UserMCPServerRow.server_id)
        ).all()
        return [_row_to_user_mcp_server(row) for row in rows]

    def get_user_mcp_server(self, owner_user_id: str, server_id: str) -> UserMCPServer | None:
        row = self._get_user_mcp_server_row(owner_user_id, server_id)
        return None if row is None else _row_to_user_mcp_server(row)

    def _get_user_mcp_server_row(
        self, owner_user_id: str, server_id: str, *, include_deleted: bool = False
    ) -> UserMCPServerRow | None:
        conditions = [
            UserMCPServerRow.owner_user_id == owner_user_id,
            UserMCPServerRow.server_id == server_id,
        ]
        if not include_deleted:
            conditions.append(UserMCPServerRow.deletion_pending.is_(False))
        return self._session.scalar(select(UserMCPServerRow).where(*conditions))

    def create_user_mcp_server(
        self, server: UserMCPServer, credential: UserMCPCredentialRecord | None = None
    ) -> UserMCPServer:
        if credential is not None and (
            credential.owner_user_id != server.owner_user_id or credential.server_id != server.server_id
        ):
            raise ValueError("credential scope does not match MCP server")
        row = UserMCPServerRow(
            server_id=server.server_id,
            owner_user_id=server.owner_user_id,
            display_name=server.display_name,
            routing_description=server.routing_description,
            endpoint_url=server.endpoint_url,
            transport=str(server.transport),
            protocol_preference=str(server.protocol_preference),
            auth_type=str(server.auth_type),
            auth_metadata=dict(server.auth_metadata),
            enabled=server.enabled,
            health_status=str(server.health_status),
            config_version=max(1, int(server.config_version)),
            security_version=max(1, int(server.security_version)),
            last_tested_at=server.last_tested_at,
            last_test_error_code=server.last_test_error_code,
            deletion_pending=False,
            deleted_at=None,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )
        if credential is not None:
            self._replace_user_mcp_credential(row, credential)
        self._session.add(row)
        self._session.flush()
        return _row_to_user_mcp_server(row)

    def update_user_mcp_server(
        self,
        owner_user_id: str,
        server_id: str,
        *,
        changes: Mapping[str, Any],
        credential_operation: str,
        credential: UserMCPCredentialRecord | None,
        security_sensitive: bool,
        expected_config_version: int | None = None,
        expected_security_version: int | None = None,
        updated_at: datetime,
    ) -> UserMCPServer | None:
        if credential_operation not in {"retain", "replace", "clear"}:
            raise ValueError("credential_operation must be retain, replace, or clear")
        if credential_operation == "replace":
            if credential is None or credential.owner_user_id != owner_user_id or credential.server_id != server_id:
                raise ValueError("replacement credential must match MCP server scope")
        elif credential is not None:
            raise ValueError("credential is only valid for replace operation")
        allowed = {
            "display_name", "routing_description", "endpoint_url", "transport", "protocol_preference",
            "auth_type", "auth_metadata", "enabled", "health_status", "last_tested_at", "last_test_error_code",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported MCP server fields: {', '.join(sorted(unknown))}")
        values = dict(changes)
        for enum_field in ("transport", "protocol_preference", "auth_type", "health_status"):
            if enum_field in values:
                values[enum_field] = str(values[enum_field])
        if "auth_metadata" in values:
            values["auth_metadata"] = dict(values["auth_metadata"] or {})
        values["updated_at"] = updated_at
        values["config_version"] = UserMCPServerRow.config_version + 1
        credential_changes_security = credential_operation in {"replace", "clear"}
        security_fields = {
            "endpoint_url", "transport", "protocol_preference", "auth_type", "auth_metadata", "enabled",
        }
        if security_sensitive or credential_changes_security or security_fields.intersection(changes):
            values["security_version"] = UserMCPServerRow.security_version + 1
        if credential_operation == "replace":
            assert credential is not None
            values.update(
                credential_ciphertext=credential.credential_ciphertext,
                credential_nonce=credential.credential_nonce,
                encryption_version=credential.encryption_version,
                credential_updated_at=credential.credential_updated_at or updated_at,
            )
        elif credential_operation == "clear":
            values.update(
                credential_ciphertext=None,
                credential_nonce=None,
                encryption_version=None,
                credential_updated_at=updated_at,
            )
        conditions = [
            UserMCPServerRow.owner_user_id == owner_user_id,
            UserMCPServerRow.server_id == server_id,
            UserMCPServerRow.deletion_pending.is_(False),
        ]
        if expected_config_version is not None:
            conditions.append(
                UserMCPServerRow.config_version == expected_config_version
            )
        if expected_security_version is not None:
            conditions.append(
                UserMCPServerRow.security_version == expected_security_version
            )
        result = self._session.execute(
            update(UserMCPServerRow).where(*conditions).values(**values)
        )
        if not result.rowcount:
            return None
        self._session.flush()
        row = self._get_user_mcp_server_row(owner_user_id, server_id)
        return None if row is None else _row_to_user_mcp_server(row)

    @staticmethod
    def _replace_user_mcp_credential(row: UserMCPServerRow, credential: UserMCPCredentialRecord) -> None:
        row.credential_ciphertext = credential.credential_ciphertext
        row.credential_nonce = credential.credential_nonce
        row.encryption_version = credential.encryption_version
        row.credential_updated_at = credential.credential_updated_at

    def get_user_mcp_credential(
        self, owner_user_id: str, server_id: str
    ) -> UserMCPCredentialRecord | None:
        row = self._get_user_mcp_server_row(owner_user_id, server_id)
        return None if row is None else _row_to_user_mcp_credential(row)

    def claim_user_mcp_health_attempt(self, attempt: UserMCPHealthAttempt) -> bool:
        server = self._get_user_mcp_server_row(attempt.owner_user_id, attempt.server_id)
        if (
            server is None
            or int(server.config_version) != attempt.config_version
            or int(server.security_version) != attempt.security_version
        ):
            return False
        claim_at = attempt.updated_at or attempt.created_at or _utcnow_naive()
        self._session.execute(
            delete(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.owner_user_id == attempt.owner_user_id,
                UserMCPHealthAttemptRow.server_id == attempt.server_id,
                or_(
                    UserMCPHealthAttemptRow.lease_expires_at <= claim_at,
                    UserMCPHealthAttemptRow.config_version != attempt.config_version,
                    UserMCPHealthAttemptRow.security_version != attempt.security_version,
                ),
            )
        )
        values = {
            "attempt_id": attempt.attempt_id,
            "owner_user_id": attempt.owner_user_id,
            "server_id": attempt.server_id,
            "config_version": attempt.config_version,
            "security_version": attempt.security_version,
            "runner_instance_id": attempt.runner_instance_id,
            "lease_expires_at": attempt.lease_expires_at,
            "created_at": attempt.created_at,
            "updated_at": attempt.updated_at,
        }
        dialect_name = self._session.get_bind().dialect.name
        statement = postgresql_insert(UserMCPHealthAttemptRow).values(**values) if dialect_name == "postgresql" else sqlite_insert(UserMCPHealthAttemptRow).values(**values)
        result = self._session.execute(
            statement.on_conflict_do_nothing(index_elements=["owner_user_id", "server_id"])
        )
        if not result.rowcount:
            return False
        server.health_status = str(UserMCPHealthStatus.TESTING)
        server.last_test_error_code = None
        server.updated_at = attempt.updated_at
        self._session.flush()
        return True

    def renew_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        result = self._session.execute(
            update(UserMCPHealthAttemptRow)
            .where(
                UserMCPHealthAttemptRow.attempt_id == attempt_id,
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.runner_instance_id == runner_instance_id,
                UserMCPHealthAttemptRow.config_version == config_version,
                UserMCPHealthAttemptRow.security_version == security_version,
                UserMCPHealthAttemptRow.lease_expires_at > updated_at,
                select(UserMCPServerRow.server_id).where(
                    UserMCPServerRow.owner_user_id == owner_user_id,
                    UserMCPServerRow.server_id == server_id,
                    UserMCPServerRow.config_version == config_version,
                    UserMCPServerRow.security_version == security_version,
                    UserMCPServerRow.deletion_pending.is_(False),
                ).exists(),
            )
            .values(lease_expires_at=lease_expires_at, updated_at=updated_at)
        )
        return bool(result.rowcount)

    def complete_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, health_status: str, error_code: str | None,
        completed_at: datetime
    ) -> UserMCPServer | None:
        attempt = self._session.scalar(
            select(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.attempt_id == attempt_id,
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.runner_instance_id == runner_instance_id,
                UserMCPHealthAttemptRow.config_version == config_version,
                UserMCPHealthAttemptRow.security_version == security_version,
                UserMCPHealthAttemptRow.lease_expires_at > completed_at,
            )
        )
        server = self._get_user_mcp_server_row(owner_user_id, server_id)
        if (
            attempt is None or server is None or int(server.config_version) != config_version
            or int(server.security_version) != security_version
        ):
            return None
        server.health_status = str(UserMCPHealthStatus(health_status))
        server.last_tested_at = completed_at
        server.last_test_error_code = error_code
        server.updated_at = completed_at
        self._session.delete(attempt)
        self._session.flush()
        return _row_to_user_mcp_server(server)

    def expire_user_mcp_health_attempts(self, *, now: datetime, error_code: str) -> int:
        attempts = self._session.scalars(
            select(UserMCPHealthAttemptRow).where(UserMCPHealthAttemptRow.lease_expires_at <= now)
        ).all()
        for attempt in attempts:
            server = self._get_user_mcp_server_row(attempt.owner_user_id, attempt.server_id)
            if (
                server is not None
                and int(server.config_version) == int(attempt.config_version)
                and int(server.security_version) == int(attempt.security_version)
                and server.health_status == str(UserMCPHealthStatus.TESTING)
            ):
                server.health_status = str(UserMCPHealthStatus.UNAVAILABLE)
                server.last_tested_at = now
                server.last_test_error_code = error_code
                server.updated_at = now
            self._session.delete(attempt)
        self._session.flush()
        return len(attempts)

    def release_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
    ) -> bool:
        result = self._session.execute(
            delete(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.attempt_id == attempt_id,
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.runner_instance_id == runner_instance_id,
                UserMCPHealthAttemptRow.config_version == config_version,
                UserMCPHealthAttemptRow.security_version == security_version,
            )
        )
        return bool(result.rowcount)

    def acquire_user_mcp_scope_lease(self, lease: UserMCPScopeLease) -> bool:
        server = self._get_user_mcp_server_row(lease.owner_user_id, lease.server_id)
        if (
            server is None or not server.enabled
            or server.health_status != str(UserMCPHealthStatus.AVAILABLE)
            or int(server.security_version) != lease.security_version
        ):
            return False
        if self._session.get(UserMCPScopeLeaseRow, lease.scope_id) is not None:
            return False
        self._session.add(
            UserMCPScopeLeaseRow(
                scope_id=lease.scope_id,
                owner_user_id=lease.owner_user_id,
                server_id=lease.server_id,
                security_version=lease.security_version,
                gateway_instance_id=lease.gateway_instance_id,
                lease_expires_at=lease.lease_expires_at,
                created_at=lease.created_at,
                updated_at=lease.updated_at,
            )
        )
        self._session.flush()
        return True

    def renew_user_mcp_scope_lease(
        self, scope_id: str, owner_user_id: str, server_id: str, *, gateway_instance_id: str,
        security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        result = self._session.execute(
            update(UserMCPScopeLeaseRow)
            .where(
                UserMCPScopeLeaseRow.scope_id == scope_id,
                UserMCPScopeLeaseRow.owner_user_id == owner_user_id,
                UserMCPScopeLeaseRow.server_id == server_id,
                UserMCPScopeLeaseRow.gateway_instance_id == gateway_instance_id,
                UserMCPScopeLeaseRow.security_version == security_version,
                UserMCPScopeLeaseRow.lease_expires_at > updated_at,
                select(UserMCPServerRow.server_id).where(
                    UserMCPServerRow.owner_user_id == owner_user_id,
                    UserMCPServerRow.server_id == server_id,
                    UserMCPServerRow.security_version == security_version,
                    UserMCPServerRow.deletion_pending.is_(False),
                    UserMCPServerRow.enabled.is_(True),
                ).exists(),
            )
            .values(lease_expires_at=lease_expires_at, updated_at=updated_at)
        )
        return bool(result.rowcount)

    def release_user_mcp_scope_lease(self, scope_id: str, *, gateway_instance_id: str) -> bool:
        result = self._session.execute(
            delete(UserMCPScopeLeaseRow).where(
                UserMCPScopeLeaseRow.scope_id == scope_id,
                UserMCPScopeLeaseRow.gateway_instance_id == gateway_instance_id,
            )
        )
        return bool(result.rowcount)

    def list_live_user_mcp_scope_leases(
        self, *, now: datetime, owner_user_id: str | None, server_id: str | None
    ) -> list[UserMCPScopeLease]:
        statement = select(UserMCPScopeLeaseRow).where(UserMCPScopeLeaseRow.lease_expires_at > now)
        if owner_user_id is not None:
            statement = statement.where(UserMCPScopeLeaseRow.owner_user_id == owner_user_id)
        if server_id is not None:
            statement = statement.where(UserMCPScopeLeaseRow.server_id == server_id)
        rows = self._session.scalars(statement.order_by(UserMCPScopeLeaseRow.lease_expires_at)).all()
        return [_row_to_user_mcp_scope_lease(row) for row in rows]

    def expire_user_mcp_scope_leases(self, *, now: datetime) -> int:
        result = self._session.execute(
            delete(UserMCPScopeLeaseRow).where(UserMCPScopeLeaseRow.lease_expires_at <= now)
        )
        return int(result.rowcount or 0)

    def mark_user_mcp_server_deleted(
        self, owner_user_id: str, server_id: str, *, deleted_at: datetime
    ) -> UserMCPServer | None:
        result = self._session.execute(
            update(UserMCPServerRow)
            .where(
                UserMCPServerRow.owner_user_id == owner_user_id,
                UserMCPServerRow.server_id == server_id,
                UserMCPServerRow.deletion_pending.is_(False),
            )
            .values(
                deletion_pending=True,
                deleted_at=deleted_at,
                enabled=False,
                health_status=str(UserMCPHealthStatus.DISABLED),
                config_version=UserMCPServerRow.config_version + 1,
                security_version=UserMCPServerRow.security_version + 1,
                updated_at=deleted_at,
            )
        )
        if not result.rowcount:
            return None
        row = self._get_user_mcp_server_row(owner_user_id, server_id, include_deleted=True)
        return None if row is None else _row_to_user_mcp_server(row)

    def list_pending_user_mcp_server_deletions(self) -> list[UserMCPServer]:
        rows = self._session.scalars(
            select(UserMCPServerRow)
            .where(UserMCPServerRow.deletion_pending.is_(True))
            .order_by(UserMCPServerRow.deleted_at, UserMCPServerRow.server_id)
        ).all()
        return [_row_to_user_mcp_server(row) for row in rows]

    def finalize_user_mcp_server_delete(
        self, owner_user_id: str, server_id: str, *, now: datetime
    ) -> bool:
        server = self._get_user_mcp_server_row(owner_user_id, server_id, include_deleted=True)
        if server is None or not server.deletion_pending:
            return False
        live_health = self._session.scalar(
            select(UserMCPHealthAttemptRow.attempt_id).where(
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
                UserMCPHealthAttemptRow.lease_expires_at > now,
            ).limit(1)
        )
        live_scope = self._session.scalar(
            select(UserMCPScopeLeaseRow.scope_id).where(
                UserMCPScopeLeaseRow.owner_user_id == owner_user_id,
                UserMCPScopeLeaseRow.server_id == server_id,
                UserMCPScopeLeaseRow.lease_expires_at > now,
            ).limit(1)
        )
        if live_health is not None or live_scope is not None:
            return False
        self._session.execute(
            delete(UserMCPHealthAttemptRow).where(
                UserMCPHealthAttemptRow.owner_user_id == owner_user_id,
                UserMCPHealthAttemptRow.server_id == server_id,
            )
        )
        self._session.execute(
            delete(UserMCPScopeLeaseRow).where(
                UserMCPScopeLeaseRow.owner_user_id == owner_user_id,
                UserMCPScopeLeaseRow.server_id == server_id,
            )
        )
        self._session.execute(
            delete(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.server_id == server_id,
            )
        )
        self._session.delete(server)
        self._session.flush()
        return True

    def save_user_mcp_tool_grant(self, grant: UserMCPToolGrant) -> UserMCPToolGrant:
        server = self._get_user_mcp_server_row(grant.owner_user_id, grant.server_id)
        if server is None:
            raise ValueError("MCP server not found")
        existing = self._session.get(UserMCPToolGrantRow, grant.grant_id)
        if existing is not None and (
            existing.owner_user_id != grant.owner_user_id or existing.server_id != grant.server_id
        ):
            raise ValueError("MCP tool grant scope does not match existing grant")
        row = UserMCPToolGrantRow(
            grant_id=grant.grant_id,
            owner_user_id=grant.owner_user_id,
            server_id=grant.server_id,
            tool_name=grant.tool_name,
            server_security_version=grant.server_security_version,
            input_schema_sha256=grant.input_schema_sha256,
            granted_at=grant.granted_at,
        )
        merged = self._session.merge(row)
        self._session.flush()
        return UserMCPToolGrant(
            grant_id=merged.grant_id, owner_user_id=merged.owner_user_id, server_id=merged.server_id,
            tool_name=merged.tool_name, server_security_version=int(merged.server_security_version),
            input_schema_sha256=merged.input_schema_sha256, granted_at=merged.granted_at,
        )

    def list_user_mcp_tool_grants(self, owner_user_id: str, server_id: str) -> list[UserMCPToolGrant]:
        rows = self._session.scalars(
            select(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.server_id == server_id,
            ).order_by(UserMCPToolGrantRow.tool_name, UserMCPToolGrantRow.grant_id)
        ).all()
        return [
            UserMCPToolGrant(
                grant_id=row.grant_id, owner_user_id=row.owner_user_id, server_id=row.server_id,
                tool_name=row.tool_name, server_security_version=int(row.server_security_version),
                input_schema_sha256=row.input_schema_sha256, granted_at=row.granted_at,
            )
            for row in rows
        ]

    def delete_user_mcp_tool_grant(self, owner_user_id: str, server_id: str, grant_id: str) -> bool:
        result = self._session.execute(
            delete(UserMCPToolGrantRow).where(
                UserMCPToolGrantRow.owner_user_id == owner_user_id,
                UserMCPToolGrantRow.server_id == server_id,
                UserMCPToolGrantRow.grant_id == grant_id,
            )
        )
        return bool(result.rowcount)

    def create_or_get_mcp_credential_key_validation(
        self, record: MCPCredentialKeyValidation
    ) -> MCPCredentialKeyValidation:
        values = {
            "validation_id": record.validation_id,
            "singleton_key": 1,
            "validation_nonce": record.validation_nonce,
            "validation_ciphertext": record.validation_ciphertext,
            "encryption_version": record.encryption_version,
            "created_at": record.created_at,
        }
        dialect_name = self._session.get_bind().dialect.name
        statement = postgresql_insert(MCPCredentialKeyValidationRow).values(**values) if dialect_name == "postgresql" else sqlite_insert(MCPCredentialKeyValidationRow).values(**values)
        self._session.execute(statement.on_conflict_do_nothing(index_elements=["singleton_key"]))
        existing = self._session.scalar(
            select(MCPCredentialKeyValidationRow).where(MCPCredentialKeyValidationRow.singleton_key == 1)
        )
        assert existing is not None
        return MCPCredentialKeyValidation(
            validation_id=existing.validation_id,
            validation_nonce=bytes(existing.validation_nonce),
            validation_ciphertext=bytes(existing.validation_ciphertext),
            encryption_version=int(existing.encryption_version),
            created_at=existing.created_at,
        )

    def get_mcp_credential_key_validation(self) -> MCPCredentialKeyValidation | None:
        row = self._session.scalar(select(MCPCredentialKeyValidationRow).limit(1))
        if row is None:
            return None
        return MCPCredentialKeyValidation(
            validation_id=row.validation_id,
            validation_nonce=bytes(row.validation_nonce),
            validation_ciphertext=bytes(row.validation_ciphertext),
            encryption_version=int(row.encryption_version),
            created_at=row.created_at,
        )


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

    def list_events_for_task_filtered(
        self,
        task_id: str,
        *,
        event_types: Iterable[str] | None = None,
        node_id: str | None = None,
        visibility: EventVisibility | str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        event_limit, byte_limit = _ensure_event_replay_policy_compatible_with_rust_contract()
        resolved_limit = _resolve_event_replay_page_limit(limit, event_limit)
        conditions = [EventRecordRow.task_id == task_id]
        if event_types is not None:
            resolved_event_types = tuple(str(event_type) for event_type in event_types)
            if not resolved_event_types:
                return []
            conditions.append(EventRecordRow.event_type.in_(resolved_event_types))
        if node_id is not None:
            conditions.append(EventRecordRow.node_id == node_id)
        if visibility is not None:
            conditions.append(EventRecordRow.visibility == str(visibility))
        rows = self._session.scalars(
            select(EventRecordRow)
            .where(*conditions)
            .order_by(EventRecordRow.created_at, EventRecordRow.event_id)
            .limit(resolved_limit)
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

    def save_slot_collection(self, collection: SlotCollection) -> SlotCollection:
        row = SlotCollectionRow(
            collection_id=collection.collection_id,
            **_slot_collection_row_values(collection),
        )
        merged = self._session.merge(row)
        self._session.flush()
        return _row_to_slot_collection(merged)

    def get_slot_collection(self, collection_id: str) -> SlotCollection | None:
        row = self._session.get(SlotCollectionRow, collection_id)
        return None if row is None else _row_to_slot_collection(row)

    def get_active_slot_collection_for_node(self, task_id: str, node_id: str) -> SlotCollection | None:
        terminal_statuses = ("completed", "cancelled", "failed")
        row = self._session.scalar(
            select(SlotCollectionRow)
            .where(
                SlotCollectionRow.task_id == task_id,
                SlotCollectionRow.node_id == node_id,
                ~SlotCollectionRow.status.in_(terminal_statuses),
            )
            .order_by(
                SlotCollectionRow.updated_at.desc(),
                SlotCollectionRow.created_at.desc(),
                SlotCollectionRow.collection_id.desc(),
            )
        )
        return None if row is None else _row_to_slot_collection(row)

    def list_slot_collections_for_task(self, task_id: str) -> list[SlotCollection]:
        rows = self._session.scalars(
            select(SlotCollectionRow)
            .where(SlotCollectionRow.task_id == task_id)
            .order_by(SlotCollectionRow.created_at, SlotCollectionRow.collection_id)
        ).all()
        return [_row_to_slot_collection(row) for row in rows]

    def apply_slot_transition(
        self,
        collection_id: str,
        expected_revision: int,
        next_collection: SlotCollection,
        slot_event: SlotEvent,
        *,
        idempotency_key: str | None = None,
    ) -> SlotCollection | None:
        key = idempotency_key or slot_event.idempotency_key
        if key:
            existing_event = self.get_slot_event_by_idempotency_key(collection_id, key)
            if existing_event is not None:
                return self.get_slot_collection(collection_id)
        if next_collection.collection_id != collection_id:
            return None

        result = self._session.execute(
            update(SlotCollectionRow)
            .where(
                SlotCollectionRow.collection_id == collection_id,
                SlotCollectionRow.revision == expected_revision,
            )
            .values(**_slot_collection_row_values(next_collection))
        )
        if result.rowcount != 1:
            self._session.flush()
            return None

        event_to_save = slot_event if key is None or slot_event.idempotency_key == key else replace(slot_event, idempotency_key=key)
        self.append_slot_event(event_to_save)
        row = self._session.get(SlotCollectionRow, collection_id)
        self._session.flush()
        return None if row is None else _row_to_slot_collection(row)

    def append_slot_event(self, event: SlotEvent) -> SlotEvent:
        if event.idempotency_key:
            existing = self.get_slot_event_by_idempotency_key(event.collection_id, event.idempotency_key)
            if existing is not None:
                return existing
        row = SlotEventRow(
            slot_event_id=event.slot_event_id,
            collection_id=event.collection_id,
            task_id=event.task_id,
            node_id=event.node_id,
            conversation_id=event.conversation_id,
            event_type=event.event_type,
            round=event.round,
            revision=event.revision,
            idempotency_key=event.idempotency_key,
            payload_json=dict(event.payload),
            created_at=event.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_slot_event(row)

    def list_slot_events(self, collection_id: str) -> list[SlotEvent]:
        rows = self._session.scalars(
            select(SlotEventRow)
            .where(SlotEventRow.collection_id == collection_id)
            .order_by(SlotEventRow.created_at, SlotEventRow.slot_event_id)
        ).all()
        return [_row_to_slot_event(row) for row in rows]

    def get_slot_event_by_idempotency_key(self, collection_id: str, key: str) -> SlotEvent | None:
        row = self._session.scalar(
            select(SlotEventRow).where(
                SlotEventRow.collection_id == collection_id,
                SlotEventRow.idempotency_key == key,
            )
        )
        return None if row is None else _row_to_slot_event(row)

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

    async def list_user_mcp_servers(self, owner_user_id: str) -> list[UserMCPServer]:
        return await self._run(lambda state, collab: state.list_user_mcp_servers(owner_user_id))

    async def get_user_mcp_server(self, owner_user_id: str, server_id: str) -> UserMCPServer | None:
        return await self._run(lambda state, collab: state.get_user_mcp_server(owner_user_id, server_id))

    async def create_user_mcp_server(
        self, server: UserMCPServer, credential: UserMCPCredentialRecord | None = None
    ) -> UserMCPServer:
        return await self._run(lambda state, collab: state.create_user_mcp_server(server, credential))

    async def update_user_mcp_server(
        self, owner_user_id: str, server_id: str, *, changes: Mapping[str, Any],
        credential_operation: str = "retain", credential: UserMCPCredentialRecord | None = None,
        security_sensitive: bool = False, expected_config_version: int | None = None,
        expected_security_version: int | None = None, updated_at: datetime
    ) -> UserMCPServer | None:
        return await self._run(
            lambda state, collab: state.update_user_mcp_server(
                owner_user_id, server_id, changes=changes, credential_operation=credential_operation,
                credential=credential, security_sensitive=security_sensitive,
                expected_config_version=expected_config_version,
                expected_security_version=expected_security_version,
                updated_at=updated_at,
            )
        )

    async def get_user_mcp_credential(
        self, owner_user_id: str, server_id: str
    ) -> UserMCPCredentialRecord | None:
        return await self._run(lambda state, collab: state.get_user_mcp_credential(owner_user_id, server_id))

    async def claim_user_mcp_health_attempt(self, attempt: UserMCPHealthAttempt) -> bool:
        return await self._run(lambda state, collab: state.claim_user_mcp_health_attempt(attempt))

    async def renew_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        return await self._run(
            lambda state, collab: state.renew_user_mcp_health_attempt(
                attempt_id, owner_user_id, server_id, runner_instance_id=runner_instance_id,
                config_version=config_version, security_version=security_version,
                lease_expires_at=lease_expires_at, updated_at=updated_at,
            )
        )

    async def complete_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, health_status: str, error_code: str | None,
        completed_at: datetime
    ) -> UserMCPServer | None:
        return await self._run(
            lambda state, collab: state.complete_user_mcp_health_attempt(
                attempt_id, owner_user_id, server_id, runner_instance_id=runner_instance_id,
                config_version=config_version, security_version=security_version,
                health_status=health_status, error_code=error_code, completed_at=completed_at,
            )
        )

    async def expire_user_mcp_health_attempts(
        self, *, now: datetime, error_code: str = "test_interrupted"
    ) -> int:
        return await self._run(
            lambda state, collab: state.expire_user_mcp_health_attempts(now=now, error_code=error_code)
        )

    async def release_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
    ) -> bool:
        return await self._run(
            lambda state, collab: state.release_user_mcp_health_attempt(
                attempt_id,
                owner_user_id,
                server_id,
                runner_instance_id=runner_instance_id,
                config_version=config_version,
                security_version=security_version,
            )
        )

    async def acquire_user_mcp_scope_lease(self, lease: UserMCPScopeLease) -> bool:
        return await self._run(lambda state, collab: state.acquire_user_mcp_scope_lease(lease))

    async def renew_user_mcp_scope_lease(
        self, scope_id: str, owner_user_id: str, server_id: str, *, gateway_instance_id: str,
        security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        return await self._run(
            lambda state, collab: state.renew_user_mcp_scope_lease(
                scope_id, owner_user_id, server_id, gateway_instance_id=gateway_instance_id,
                security_version=security_version, lease_expires_at=lease_expires_at, updated_at=updated_at,
            )
        )

    async def release_user_mcp_scope_lease(self, scope_id: str, *, gateway_instance_id: str) -> bool:
        return await self._run(
            lambda state, collab: state.release_user_mcp_scope_lease(scope_id, gateway_instance_id=gateway_instance_id)
        )

    async def list_live_user_mcp_scope_leases(
        self, *, now: datetime, owner_user_id: str | None = None, server_id: str | None = None
    ) -> list[UserMCPScopeLease]:
        return await self._run(
            lambda state, collab: state.list_live_user_mcp_scope_leases(
                now=now, owner_user_id=owner_user_id, server_id=server_id,
            )
        )

    async def expire_user_mcp_scope_leases(self, *, now: datetime) -> int:
        return await self._run(lambda state, collab: state.expire_user_mcp_scope_leases(now=now))

    async def mark_user_mcp_server_deleted(
        self, owner_user_id: str, server_id: str, *, deleted_at: datetime
    ) -> UserMCPServer | None:
        return await self._run(
            lambda state, collab: state.mark_user_mcp_server_deleted(
                owner_user_id, server_id, deleted_at=deleted_at,
            )
        )

    async def list_pending_user_mcp_server_deletions(self) -> list[UserMCPServer]:
        return await self._run(lambda state, collab: state.list_pending_user_mcp_server_deletions())

    async def finalize_user_mcp_server_delete(
        self, owner_user_id: str, server_id: str, *, now: datetime
    ) -> bool:
        return await self._run(
            lambda state, collab: state.finalize_user_mcp_server_delete(owner_user_id, server_id, now=now)
        )

    async def save_user_mcp_tool_grant(self, grant: UserMCPToolGrant) -> UserMCPToolGrant:
        return await self._run(lambda state, collab: state.save_user_mcp_tool_grant(grant))

    async def list_user_mcp_tool_grants(
        self, owner_user_id: str, server_id: str
    ) -> list[UserMCPToolGrant]:
        return await self._run(lambda state, collab: state.list_user_mcp_tool_grants(owner_user_id, server_id))

    async def delete_user_mcp_tool_grant(
        self, owner_user_id: str, server_id: str, grant_id: str
    ) -> bool:
        return await self._run(
            lambda state, collab: state.delete_user_mcp_tool_grant(owner_user_id, server_id, grant_id)
        )

    async def create_or_get_mcp_credential_key_validation(
        self, record: MCPCredentialKeyValidation
    ) -> MCPCredentialKeyValidation:
        return await self._run(
            lambda state, collab: state.create_or_get_mcp_credential_key_validation(record)
        )

    async def get_mcp_credential_key_validation(self) -> MCPCredentialKeyValidation | None:
        return await self._run(lambda state, collab: state.get_mcp_credential_key_validation())

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

    def _ensure_event_replay_available(self) -> None:
        if runtime_mode_for_component("event_log") != "enforce":
            return
        raise RuntimeError(
            "event_log_replay_unavailable: Rust runtime sidecar enforce mode is active, "
            "but event replay/list operations are not implemented by the configured sidecar facade."
        )

    async def save_auth_user_token(self, token: AuthUserToken, *, auth_generation_reason: str | None = None) -> AuthUserToken:
        return await self._run(lambda state, collab: state.save_auth_user_token(token, auth_generation_reason=auth_generation_reason))

    async def get_auth_user_token(self, username: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_token(username))

    async def get_auth_user_token_by_hash(self, api_token_hash: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_token_by_hash(api_token_hash))

    async def get_auth_user_generation(self, username: str) -> AuthUserToken | None:
        return await self._run(lambda state, collab: state.get_auth_user_generation(username))

    async def list_auth_user_generations(self) -> list[AuthUserToken]:
        return await self._run(lambda state, collab: state.list_auth_user_generations())

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
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.clear_auth_user_token(
                username,
                api_token_hash=api_token_hash,
                at=at,
                auth_generation_reason=auth_generation_reason,
            )
        )

    async def rotate_auth_user_token(
        self,
        username: str,
        *,
        old_api_token_hash: str,
        new_api_token_hash: str,
        at: datetime,
        auth_generation_reason: str | None = None,
    ) -> AuthUserToken | None:
        return await self._run(
            lambda state, collab: state.rotate_auth_user_token(
                username,
                old_api_token_hash=old_api_token_hash,
                new_api_token_hash=new_api_token_hash,
                at=at,
                auth_generation_reason=auth_generation_reason,
            )
        )

    async def save_conversation(self, conversation: Conversation) -> Conversation:
        return await self._run(lambda state, collab: state.save_conversation(conversation))

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return await self._run(lambda state, collab: state.get_conversation(conversation_id))

    async def list_conversations_for_username(self, username: str) -> list[Conversation]:
        return await self._run(lambda state, collab: state.list_conversations_for_username(username))

    async def list_deleting_conversations(self) -> list[Conversation]:
        return await self._run(lambda state, collab: state.list_deleting_conversations())

    async def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_deleting(
                conversation_id,
                runner_id=runner_id,
                requested_at=requested_at,
                started_at=started_at,
                phase=phase,
            )
        )

    async def update_conversation_delete_phase(
        self,
        conversation_id: str,
        *,
        phase: str,
        updated_at: datetime,
        runner_id: str | None = None,
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.update_conversation_delete_phase(
                conversation_id,
                phase=phase,
                updated_at=updated_at,
                runner_id=runner_id,
            )
        )

    async def mark_conversation_delete_failed(
        self,
        conversation_id: str,
        *,
        failed_at: datetime,
        phase: str,
        error_code: str,
        error_summary: str,
        runner_id: str | None = None,
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_delete_failed(
                conversation_id,
                failed_at=failed_at,
                phase=phase,
                error_code=error_code,
                error_summary=error_summary,
                runner_id=runner_id,
            )
        )

    async def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await self._run(
            lambda state, collab: state.retry_failed_conversation_delete(
                conversation_id,
                runner_id=runner_id,
                requested_at=requested_at,
                started_at=started_at,
                phase=phase,
            )
        )

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        return await self._run(lambda state, collab: state.delete_conversation(conversation_id))

    async def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]:
        return await self._run(lambda state, collab: state.delete_conversation_physical(conversation_id))

    async def save_conversation_file_resource(self, resource: ConversationFileResource) -> ConversationFileResource:
        return await self._run(lambda state, collab: state.save_conversation_file_resource(resource))

    async def get_conversation_file_resource(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
    ) -> ConversationFileResource | None:
        return await self._run(lambda state, collab: state.get_conversation_file_resource(conversation_id, username, file_id))

    async def get_conversation_file_resource_by_id(self, file_id: str) -> ConversationFileResource | None:
        return await self._run(lambda state, collab: state.get_conversation_file_resource_by_id(file_id))

    async def list_conversation_file_resources(
        self,
        conversation_id: str,
        username: str | None = None,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[ConversationFileResource]:
        return await self._run(
            lambda state, collab: state.list_conversation_file_resources(
                conversation_id,
                username,
                include_deleted=include_deleted,
                limit=limit,
                cursor=cursor,
            )
        )

    async def mark_conversation_file_resource_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_resource_deleted(
                conversation_id,
                username,
                file_id,
                updated_at=updated_at,
            )
        )

    async def save_conversation_file_resource_with_upload_message(
        self,
        resource: ConversationFileResource,
        projection: FileUploadMessageProjection,
        *,
        now: datetime,
    ) -> ConversationFileResource:
        try:
            return await self._run(
                lambda state, collab: state.save_conversation_file_resource_with_upload_message(
                    resource,
                    projection,
                    now=now,
                )
            )
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
                    conversation_id=projection.conversation_id,
                    upload_id=projection.upload_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=now,
                    projection=projection,
                )
            )
            raise

    async def mark_conversation_file_resource_and_upload_message_deleted(
        self,
        conversation_id: str,
        username: str,
        file_id: str,
        *,
        updated_at: datetime,
    ) -> ConversationFileResource | None:
        try:
            return await self._run(
                lambda state, collab: state.mark_conversation_file_resource_and_upload_message_deleted(
                    conversation_id,
                    username,
                    file_id,
                    updated_at=updated_at,
                )
            )
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
                    conversation_id=conversation_id,
                    upload_id=file_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=updated_at,
                )
            )
            raise

    async def compensate_failed_conversation_file_upload(
        self,
        conversation_id: str,
        username: str,
        upload_id: str,
        *,
        reason_code: str,
        now: datetime,
    ) -> Mapping[str, Any]:
        return await self._run(
            lambda state, collab: state.compensate_failed_conversation_file_upload(
                conversation_id,
                username,
                upload_id,
                reason_code=reason_code,
                now=now,
            )
        )

    async def record_conversation_file_index_repair_required(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        affected_upload_ids: Iterable[str] = (),
        now: datetime,
    ) -> ConversationFileIndexRepairMarker:
        return await self._run(
            lambda state, collab: state.record_conversation_file_index_repair_required(
                conversation_id,
                reason_code=reason_code,
                affected_upload_ids=affected_upload_ids,
                now=now,
            )
        )

    async def get_conversation_file_index_repair_marker(
        self,
        conversation_id: str,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(lambda state, collab: state.get_conversation_file_index_repair_marker(conversation_id))

    async def list_due_conversation_file_index_repairs(
        self,
        *,
        now: datetime,
        limit: int | None = None,
    ) -> list[ConversationFileIndexRepairMarker]:
        return await self._run(
            lambda state, collab: state.list_due_conversation_file_index_repairs(now=now, limit=limit)
        )

    async def mark_conversation_file_index_repairing(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_index_repairing(conversation_id, now=now)
        )

    async def mark_conversation_file_index_repair_resolved(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_index_repair_resolved(conversation_id, now=now)
        )

    async def mark_conversation_file_index_repair_failed(
        self,
        conversation_id: str,
        *,
        reason_code: str,
        now: datetime,
        retryable: bool = True,
    ) -> ConversationFileIndexRepairMarker | None:
        return await self._run(
            lambda state, collab: state.mark_conversation_file_index_repair_failed(
                conversation_id,
                reason_code=reason_code,
                now=now,
                retryable=retryable,
            )
        )

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

    async def upsert_file_upload_message(self, projection: FileUploadMessageProjection, *, now: datetime) -> Message:
        try:
            return await self._run(lambda state, collab: state.upsert_file_upload_message(projection, now=now))
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
                    conversation_id=projection.conversation_id,
                    upload_id=projection.upload_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=now,
                    projection=projection,
                )
            )
            raise

    async def mark_file_upload_message_deleted(
        self,
        conversation_id: str,
        upload_id: str,
        *,
        deleted_at: datetime,
    ) -> Message | None:
        try:
            return await self._run(
                lambda state, collab: state.mark_file_upload_message_deleted(
                    conversation_id,
                    upload_id,
                    deleted_at=deleted_at,
                )
            )
        except ValueError as exc:
            reason_code = _file_upload_message_error_reason(str(exc))
            await self._run(
                lambda state, collab: state.record_file_upload_message_audit(
                    event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
                    conversation_id=conversation_id,
                    upload_id=upload_id,
                    outcome="failed",
                    reason_code=reason_code,
                    at=deleted_at,
                )
            )
            raise

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

    async def list_artifacts_for_conversation(self, conversation_id: str) -> list[Artifact]:
        return await self._run(lambda state, collab: state.list_artifacts_for_conversation(conversation_id))

    async def save_task_input_attachment(self, attachment: TaskInputAttachment) -> TaskInputAttachment:
        return await self._run(lambda state, collab: state.save_task_input_attachment(attachment))

    async def list_task_input_attachments_for_task(self, task_id: str) -> list[TaskInputAttachment]:
        return await self._run(lambda state, collab: state.list_task_input_attachments_for_task(task_id))

    async def list_task_input_attachments_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
    ) -> list[TaskInputAttachment]:
        return await self._run(lambda state, collab: state.list_task_input_attachments_for_conversation(conversation_id, limit=limit))

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
        self._ensure_event_replay_available()
        return await self._run(lambda state, collab: collab.list_events_for_task(task_id))

    async def list_events_for_task_filtered(
        self,
        task_id: str,
        *,
        event_types: Iterable[str] | None = None,
        node_id: str | None = None,
        visibility: EventVisibility | str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        self._ensure_event_replay_available()
        return await self._run(
            lambda state, collab: collab.list_events_for_task_filtered(
                task_id,
                event_types=event_types,
                node_id=node_id,
                visibility=visibility,
                limit=limit,
            )
        )

    async def list_event_page_for_task(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        self._ensure_event_replay_available()
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

    async def save_slot_collection(self, collection: SlotCollection) -> SlotCollection:
        return await self._run(lambda state, collab: collab.save_slot_collection(collection))

    async def get_slot_collection(self, collection_id: str) -> SlotCollection | None:
        return await self._run(lambda state, collab: collab.get_slot_collection(collection_id))

    async def get_active_slot_collection_for_node(self, task_id: str, node_id: str) -> SlotCollection | None:
        return await self._run(lambda state, collab: collab.get_active_slot_collection_for_node(task_id, node_id))

    async def list_slot_collections_for_task(self, task_id: str) -> list[SlotCollection]:
        return await self._run(lambda state, collab: collab.list_slot_collections_for_task(task_id))

    async def apply_slot_transition(
        self,
        collection_id: str,
        expected_revision: int,
        next_collection: SlotCollection,
        slot_event: SlotEvent,
        *,
        idempotency_key: str | None = None,
    ) -> SlotCollection | None:
        return await self._run(
            lambda state, collab: collab.apply_slot_transition(
                collection_id,
                expected_revision,
                next_collection,
                slot_event,
                idempotency_key=idempotency_key,
            )
        )

    async def append_slot_event(self, event: SlotEvent) -> SlotEvent:
        return await self._run(lambda state, collab: collab.append_slot_event(event))

    async def list_slot_events(self, collection_id: str) -> list[SlotEvent]:
        return await self._run(lambda state, collab: collab.list_slot_events(collection_id))

    async def get_slot_event_by_idempotency_key(self, collection_id: str, key: str) -> SlotEvent | None:
        return await self._run(lambda state, collab: collab.get_slot_event_by_idempotency_key(collection_id, key))

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
