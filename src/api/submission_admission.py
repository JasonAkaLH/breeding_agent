from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from src.core.contracts import (
    ConversationTaskAdmissionPort,
    SubmissionPreparationReceiptStoragePort,
)
from src.core.models import (
    SubmissionAdmissionHandle,
    SubmissionAdmissionDisposition,
    SubmissionAdmissionRequest,
    SubmissionAdmissionResult,
    SubmissionAdmissionState,
    SubmissionAuthorityState,
    SubmissionClaimRenewalRequest,
    SubmissionClaimRequest,
    SubmissionHandoffAcknowledgementRequest,
    SubmissionHandoffState,
    SubmissionPreparationLookup,
    SubmissionPreparationReceipt,
    SubmissionPreparationReceiptComponent,
    SubmissionPreparationRecord,
    SubmissionPreparationRequest,
    SubmissionPreparationState,
    SubmissionProjectionAcknowledgementRequest,
    SubmissionProjectionState,
    SubmissionRecoveryRecord,
    Task,
)
from src.integrations.mcp.result_parsing.json_values import canonical_json_bytes
from src.integrations.mcp.cp7_artifacts import mcp_no_server_intent_id
from src.orchestration.agent_loop.models import provider_safe_tool_name
from src.orchestration.models import UserMCPServerProfile
from src.orchestration.conversation_memory import (
    COMPRESSION_POLICY_VERSION,
    SUMMARY_VERSION,
    _stable_memory_summary_id,
)
from src.storage.runtime_sidecar_facade import (
    _validate_available_mcp_servers,
    _validate_bundle_revisions,
    _validate_execution_metadata,
    _validate_model_options,
    _validate_safe_references,
    validate_runtime_sidecar_submission_envelopes,
)
from src.storage.agent_payload import AgentPayloadError, canonicalize_agent_payload


_PREPARED_SCHEMA_V1 = "maf.submission.prepared_execution.v1"
_PREPARED_SCHEMA_V2 = "maf.submission.prepared_execution.v2"
_PREPARED_DOMAINS = {
    _PREPARED_SCHEMA_V1: b"maf.submission.prepared_execution.v1\0",
    _PREPARED_SCHEMA_V2: b"maf.submission.prepared_execution.v2\0",
}
_REQUEST_DOMAIN = b"maf.submission.request.v1\0"
_PROJECTION_DOMAIN = b"maf.submission.projection.v1\0"
_CONTINUATION_DOMAIN = b"maf.submission.continuation.v1\0"
_HEX = frozenset("0123456789abcdef")
_MAX_PREPARED_BYTES = 128 * 1024
_DEFAULT_RECOVERY_LIMIT = 128
_FORBIDDEN_KEYS = {
    "credential",
    "credentials",
    "password",
    "api_key",
    "access_token",
    "refresh_token",
    "file_content",
    "base64",
    "tool_arguments",
    "raw_arguments",
    "llm_prompt",
    "provider_response",
}
_MEMORY_PROMPT_KEYS = {
    "current_user_message",
    "resolved_user_message",
    "history_summary",
    "recent_messages",
    "clarification_messages",
    "capability_summaries",
    "memory_candidates",
    "compression_level",
    "token_budget",
    "estimated_tokens_before",
    "estimated_tokens_after",
    "truncated",
    "fallback_reason",
    "resolution_metadata",
}
_MEMORY_PROMPT_REQUIRED_KEYS = _MEMORY_PROMPT_KEYS - {
    "resolved_user_message",
    "history_summary",
    "fallback_reason",
}
_MEMORY_MESSAGE_KEYS = {
    "message_id",
    "role",
    "content",
    "task_id",
    "kind",
    "created_at",
}
_MEMORY_CANDIDATE_KEYS = {
    "candidate_id",
    "kind",
    "content",
    "priority",
    "trim_policy",
    "token_estimate",
    "metadata",
}
_MEMORY_SUMMARY_WRITE_KEYS = {
    "schema",
    "summary_id",
    "summary_sha256",
    "conversation_id",
    "username",
    "covered_until_turn_id",
    "covered_until_message_id",
    "covered_until_created_at",
    "summary_text",
    "source_message_count",
    "source_message_ids_hash",
    "estimated_tokens",
    "summary_version",
    "compression_policy_version",
    "model_metadata_safe",
    "created_at",
    "updated_at",
}
_MEMORY_EVENT_WRITE_KEYS = {
    "schema",
    "event_id",
    "event_sha256",
    "event_subject_sha256",
    "memory_identity_sha256",
    "conversation_id",
    "task_id",
    "node_id",
    "agent_id",
    "event_type",
    "payload",
    "visibility",
    "created_at",
}


class SubmissionRecoveryStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class SubmissionRecoveryError(RuntimeError):
    """Fail-closed error for the isolated submission recovery primitive."""


@dataclass(frozen=True, slots=True)
class SubmissionRecoveryBatchResult:
    status: SubmissionRecoveryStatus
    recovered_count: int
    pending_count: int
    after_created_at: datetime | None = None
    after_message_id: str | None = None
    earliest_claim_expires_at: datetime | None = None


def build_submission_admission_request(
    *,
    username: str,
    conversation_id: str,
    message_id: str,
    content: str,
    task: Task,
    conversation_created_at: datetime,
    conversation_updated_at: datetime,
    create_conversation_if_missing: bool,
    message_created_at: datetime,
    message_type: str,
    message_metadata: Mapping[str, Any],
    model_options: Mapping[str, Any],
    bundle_revisions: Mapping[str, Any],
    execution_metadata: Mapping[str, Any],
    explicit_upload_ids: tuple[str, ...],
    request_sheet_selections: Mapping[str, str],
    upload_refs: tuple[Mapping[str, Any], ...],
    mcp_binding: Mapping[str, Any] | None,
    mcp_assignment: Mapping[str, Any] | None,
    available_mcp_servers: tuple[UserMCPServerProfile, ...],
    pending_context: Mapping[str, Any] | None,
    initial_no_server_eligible: bool,
    claim_owner: str,
    claim_expires_at: datetime,
    skill_activation: Mapping[str, Any] | None = None,
) -> SubmissionAdmissionRequest:
    """Build one closed, canonical submission request without side effects."""

    if (
        task.conversation_id != conversation_id
        or task.root_message_id != message_id
        or str(task.status) != "accepted"
    ):
        raise ValueError("submission_task_identity_invalid")

    normalized_upload_ids = sorted(set(explicit_upload_ids))
    if any(
        not isinstance(upload_id, str) or not upload_id
        for upload_id in normalized_upload_ids
    ):
        raise ValueError("submission_upload_ids_invalid")
    normalized_request_sheets = {
        upload_id: request_sheet_selections[upload_id]
        for upload_id in sorted(request_sheet_selections)
    }
    if any(
        not isinstance(upload_id, str)
        or not upload_id
        or upload_id not in normalized_upload_ids
        or not isinstance(sheet, str)
        or not sheet
        for upload_id, sheet in normalized_request_sheets.items()
    ):
        raise ValueError("submission_sheet_selections_invalid")

    upload_refs_by_id: dict[str, dict[str, Any]] = {}
    upload_ref_keys = {
        "upload_id",
        "conversation_id",
        "sha256",
        "size_bytes",
        "selected_sheet",
    }
    for raw_ref in upload_refs:
        if set(raw_ref) != upload_ref_keys:
            raise ValueError("submission_upload_refs_invalid")
        ref = {key: raw_ref[key] for key in upload_ref_keys}
        upload_id = ref["upload_id"]
        if not isinstance(upload_id, str):
            raise ValueError("submission_upload_refs_invalid")
        existing = upload_refs_by_id.get(upload_id)
        if existing is not None and existing != ref:
            raise ValueError("submission_upload_refs_invalid")
        upload_refs_by_id[upload_id] = ref
    normalized_upload_refs = [
        upload_refs_by_id[upload_id] for upload_id in sorted(upload_refs_by_id)
    ]
    continuation_sheets = {
        ref["upload_id"]: ref["selected_sheet"]
        for ref in normalized_upload_refs
        if ref["selected_sheet"] is not None
    }

    normalized_servers_by_id: dict[str, dict[str, str]] = {}
    for server in available_mcp_servers:
        profile = {
            "server_id": server.server_id,
            "display_name": server.display_name,
            "routing_description": server.routing_description,
            "transport": server.transport,
        }
        existing = normalized_servers_by_id.get(server.server_id)
        if existing is not None and existing != profile:
            raise ValueError("submission_available_mcp_servers_invalid")
        normalized_servers_by_id[server.server_id] = profile
    normalized_servers = [
        normalized_servers_by_id[server_id]
        for server_id in sorted(normalized_servers_by_id)
    ]

    normalized_pending = dict(pending_context) if pending_context is not None else None
    if normalized_pending is not None and isinstance(
        normalized_pending.get("missing_requirements"), (list, tuple)
    ):
        normalized_pending["missing_requirements"] = sorted(
            set(normalized_pending["missing_requirements"])
        )

    canonical_request = {
        "schema": "maf.submission.request.v1",
        "username": username,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "role": "user",
        "content": content,
        "routing_mode": str(task.routing_mode),
        "requested_capability_id": task.requested_capability_id,
        "model_edition": model_options.get("model_edition"),
        "model_options": dict(model_options),
        "bundle_revisions": dict(bundle_revisions),
        "execution_metadata": dict(execution_metadata),
        "upload_ids": normalized_upload_ids,
        "sheet_selections": normalized_request_sheets,
        "mcp_binding": dict(mcp_binding) if mcp_binding is not None else None,
        "mcp_assignment": dict(mcp_assignment) if mcp_assignment is not None else None,
        "pending_context": normalized_pending,
    }
    request_fingerprint = _domain_sha256(
        _REQUEST_DOMAIN, canonical_json_bytes(canonical_request)
    )

    def timestamp(value: datetime) -> str:
        rendered = value.isoformat()
        return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered

    conversation_projection = canonical_json_bytes(
        {
            "schema": "maf.submission.conversation_projection.v1",
            "conversation_id": conversation_id,
            "username": username,
            "status": "active",
            "current_task_id": task.task_id,
            "created_at": timestamp(conversation_created_at),
            "updated_at": timestamp(conversation_updated_at),
            "create_if_missing": create_conversation_if_missing,
        }
    )
    message_projection = canonical_json_bytes(
        {
            "schema": "maf.submission.message_projection.v1",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "role": "user",
            "content": content,
            "task_id": task.task_id,
            "stream_status": "complete",
            "message_created_at": timestamp(message_created_at),
            "message_type": message_type,
            "metadata": dict(message_metadata),
            "updated_at": timestamp(message_created_at),
        }
    )
    projection_sha256 = hashlib.sha256(
        _PROJECTION_DOMAIN
        + conversation_projection
        + b"\0"
        + message_projection
    ).hexdigest()
    continuation = canonical_json_bytes(
        {
            "schema": "maf.submission.continuation.v1",
            "request_fingerprint": request_fingerprint,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "task_id": task.task_id,
            "owner_scope": username,
            "message_content_sha256": hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),
            "routing_mode": str(task.routing_mode),
            "requested_capability_id": task.requested_capability_id,
            "model_options": dict(model_options),
            "bundle_revisions": dict(bundle_revisions),
            "execution_metadata": dict(execution_metadata),
            "upload_refs": normalized_upload_refs,
            "sheet_selections": continuation_sheets,
            "mcp_binding": dict(mcp_binding) if mcp_binding is not None else None,
            "mcp_assignment": dict(mcp_assignment) if mcp_assignment is not None else None,
            "available_mcp_servers": normalized_servers,
            "pending_context": normalized_pending,
            "initial_no_server_eligible": initial_no_server_eligible,
            "skill_activation": (
                dict(skill_activation) if skill_activation is not None else None
            ),
        }
    )
    continuation_sha256 = _domain_sha256(_CONTINUATION_DOMAIN, continuation)
    validate_runtime_sidecar_submission_envelopes(
        conversation_projection=conversation_projection,
        message_projection=message_projection,
        continuation=continuation,
        projection_sha256=projection_sha256,
        continuation_sha256=continuation_sha256,
        username=username,
        conversation_id=conversation_id,
        message_id=message_id,
        task_id=task.task_id,
        request_fingerprint=request_fingerprint,
        routing_mode=str(task.routing_mode),
        requested_capability_id=task.requested_capability_id,
    )
    return SubmissionAdmissionRequest(
        username=username,
        conversation_id=conversation_id,
        message_id=message_id,
        task=task,
        idempotency_key=f"submission:{username}:{message_id}",
        request_fingerprint=request_fingerprint,
        conversation_projection=conversation_projection,
        message_projection=message_projection,
        projection_sha256=projection_sha256,
        continuation=continuation,
        continuation_sha256=continuation_sha256,
        message_created_at=message_created_at,
        claim_owner=claim_owner,
        claim_expires_at=claim_expires_at,
    )


@dataclass(frozen=True, slots=True)
class DurableSubmissionHandoff:
    kind: str
    identity: str


@dataclass(frozen=True, slots=True)
class PreparedAgentRecoveryContext:
    username: str
    current_user_input: str
    initial_required_tool_name: str | None
    model_options: Mapping[str, Any]
    bundle_revisions: Mapping[str, Any]
    execution_metadata: Mapping[str, Any]
    memory_context: Mapping[str, Any] | None
    mcp_binding: Mapping[str, Any] | None
    mcp_assignment: Mapping[str, Any] | None
    available_mcp_servers: tuple[UserMCPServerProfile, ...]
    routing_mode: str = "auto"
    upload_refs: tuple[Mapping[str, Any], ...] = ()
    selected_upload_ids: tuple[str, ...] = ()
    skill_activation_payload_json: str | None = None
    skill_activation_payload_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedSubmissionRecoveryAuthority:
    preparation: SubmissionPreparationRecord
    prepared: Mapping[str, Any]
    receipt: SubmissionPreparationReceipt
    agent_context: PreparedAgentRecoveryContext | None = None


class PreparedAgentRecoveryLoader(Protocol):
    async def load(
        self,
        *,
        username: str,
        conversation_id: str,
        task_id: str,
        message_id: str,
        root_message_content: str | None,
    ) -> PreparedAgentRecoveryContext | None: ...

    async def load_authority(
        self,
        *,
        username: str,
        conversation_id: str,
        task_id: str,
        message_id: str,
        root_message_content: str | None,
    ) -> PreparedSubmissionRecoveryAuthority | None: ...


class SubmissionPreparedAgentRecoveryLoader:
    """Read and verify one handed-off Agent preparation without recomputation."""

    def __init__(
        self,
        *,
        admission: ConversationTaskAdmissionPort,
        receipts: SubmissionPreparationReceiptStoragePort,
    ) -> None:
        self._admission = admission
        self._receipts = receipts

    async def load(
        self,
        *,
        username: str,
        conversation_id: str,
        task_id: str,
        message_id: str,
        root_message_content: str | None,
    ) -> PreparedAgentRecoveryContext | None:
        authority = await self.load_authority(
            username=username,
            conversation_id=conversation_id,
            task_id=task_id,
            message_id=message_id,
            root_message_content=root_message_content,
        )
        if authority is None:
            return None
        preparation = authority.preparation
        prepared = authority.prepared
        if (
            preparation.handoff_state is not SubmissionHandoffState.HANDED_OFF
            or preparation.handoff_kind != "agent_run"
            or preparation.handoff_identity != f"agent-run:{task_id}"
            or prepared["prepared_kind"] != "agent_run"
            or prepared["planned_handoff_kind"] != "agent_run"
            or authority.agent_context is None
        ):
            raise SubmissionRecoveryError("submission_prepared_agent_handoff_drift")
        return authority.agent_context

    async def load_authority(
        self,
        *,
        username: str,
        conversation_id: str,
        task_id: str,
        message_id: str,
        root_message_content: str | None,
    ) -> PreparedSubmissionRecoveryAuthority | None:
        preparation = await self._admission.get_submission_preparation(
            SubmissionPreparationLookup(
                username=username,
                conversation_id=conversation_id,
                task_id=task_id,
            )
        )
        if preparation is None:
            return None
        if (
            preparation.conversation_id != conversation_id
            or preparation.message_id != message_id
            or preparation.task_id != task_id
        ):
            raise SubmissionRecoveryError("submission_prepared_authority_identity_drift")
        if preparation.prepared_execution_sha256 != _prepared_execution_digest(
            preparation.prepared_execution
        ):
            raise SubmissionRecoveryError("submission_prepared_digest_mismatch")

        prepared = _validated_prepared_content(
            preparation.prepared_execution,
            conversation_id=conversation_id,
            message_id=message_id,
            task_id=task_id,
        )
        if (
            prepared["owner_scope"] != username
            or prepared["prepared_kind"] != prepared["planned_handoff_kind"]
        ):
            raise SubmissionRecoveryError("submission_prepared_agent_identity_drift")
        expected_identity = _expected_handoff_identity(task_id, prepared)
        if preparation.handoff_state is SubmissionHandoffState.PENDING:
            if (
                preparation.handoff_kind is not None
                or preparation.handoff_identity is not None
            ):
                raise SubmissionRecoveryError("submission_prepared_handoff_drift")
        elif (
            preparation.handoff_state is not SubmissionHandoffState.HANDED_OFF
            or preparation.handoff_kind != prepared["planned_handoff_kind"]
            or preparation.handoff_identity != expected_identity
        ):
            raise SubmissionRecoveryError("submission_prepared_handoff_drift")
        receipt = await self._receipts.get_submission_preparation_receipt(
            username=username,
            conversation_id=conversation_id,
            task_id=task_id,
        )
        _validate_closed_receipt_exact(
            username=username,
            conversation_id=conversation_id,
            task_id=task_id,
            locator=prepared["preparation_receipt"],
            receipt=receipt,
        )
        assert receipt is not None
        _validate_prepared_receipt_facts(prepared, prepared, receipt)
        current_user_input = _execution_text_from_facts(
            root_message_content=root_message_content,
            facts=prepared,
            memory_component=receipt.memory_context,
            source=prepared["execution_text_source"],
        )
        if hashlib.sha256(current_user_input.encode("utf-8")).hexdigest() != prepared.get(
            "execution_text_sha256"
        ):
            raise SubmissionRecoveryError("submission_execution_text_digest_mismatch")
        agent_context = (
            _build_prepared_agent_recovery_context(
                preparation,
                prepared=prepared,
                receipt=receipt,
                root_message_content=root_message_content,
            )
            if prepared["planned_handoff_kind"] == "agent_run"
            else None
        )
        return PreparedSubmissionRecoveryAuthority(
            preparation=preparation,
            prepared=prepared,
            receipt=receipt,
            agent_context=agent_context,
        )


def _build_prepared_agent_recovery_context(
    record: SubmissionRecoveryRecord | SubmissionPreparationRecord,
    *,
    prepared: Mapping[str, Any],
    receipt: SubmissionPreparationReceipt,
    root_message_content: str | None,
) -> PreparedAgentRecoveryContext:
    if (
        prepared["conversation_id"] != record.conversation_id
        or prepared["message_id"] != record.message_id
        or prepared["task_id"] != record.task_id
        or prepared["prepared_kind"] != "agent_run"
        or prepared["planned_handoff_kind"] != "agent_run"
    ):
        raise SubmissionRecoveryError("submission_prepared_agent_identity_drift")
    _validate_prepared_receipt_facts(prepared, prepared, receipt)
    current_user_input = _execution_text_from_facts(
        root_message_content=root_message_content,
        facts=prepared,
        memory_component=receipt.memory_context,
        source=prepared["execution_text_source"],
    )
    if hashlib.sha256(current_user_input.encode("utf-8")).hexdigest() != prepared.get(
        "execution_text_sha256"
    ):
        raise SubmissionRecoveryError("submission_execution_text_digest_mismatch")

    memory = _parse_component(receipt.memory_context)
    memory_context = (
        dict(memory["prompt_payload"])
        if isinstance(memory, Mapping)
        and not (
            isinstance(memory.get("event_write"), Mapping)
            and memory["event_write"].get("event_type")
            == "conversation.memory_fallback"
        )
        else None
    )
    selector = _parse_component(receipt.selector_decision)
    selected_upload_ids = (
        tuple(selector["upload_ids"])
        if isinstance(selector, Mapping)
        and selector.get("interrupt_kind") is None
        else ()
    )
    upload_refs = tuple(dict(item) for item in prepared["upload_refs"])
    if not set(selected_upload_ids) <= {
        str(item["upload_id"]) for item in upload_refs
    }:
        raise SubmissionRecoveryError("submission_prepared_upload_ref_drift")
    return PreparedAgentRecoveryContext(
        username=prepared["owner_scope"],
        current_user_input=current_user_input,
        routing_mode=_prepared_routing_mode(prepared),
        initial_required_tool_name=prepared["initial_required_tool_name"],
        model_options=dict(prepared["model_options"]),
        bundle_revisions=dict(prepared["bundle_revisions"]),
        execution_metadata=dict(prepared["execution_metadata"]),
        memory_context=memory_context,
        mcp_binding=(
            dict(prepared["mcp_binding"])
            if isinstance(prepared["mcp_binding"], Mapping)
            else None
        ),
        mcp_assignment=(
            dict(prepared["mcp_assignment"])
            if isinstance(prepared["mcp_assignment"], Mapping)
            else None
        ),
        available_mcp_servers=tuple(
            UserMCPServerProfile(**server)
            for server in prepared["available_mcp_servers"]
        ),
        upload_refs=upload_refs,
        selected_upload_ids=selected_upload_ids,
        skill_activation_payload_json=_prepared_activation_payload(prepared),
        skill_activation_payload_sha256=_prepared_activation_sha256(prepared),
    )


class SubmissionPreparationCallbacks(Protocol):
    """Narrow injected seams; compute methods must not mutate durable state."""

    async def settle_route_decision_exact(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
        written_at: datetime,
    ) -> SubmissionPreparationReceipt: ...

    async def compute_memory_context(
        self, record: SubmissionRecoveryRecord, continuation: Mapping[str, Any]
    ) -> object: ...

    async def compute_selector_decision(
        self, record: SubmissionRecoveryRecord, continuation: Mapping[str, Any]
    ) -> object: ...

    async def materialize_route_decision(
        self, record: SubmissionRecoveryRecord, canonical_component: bytes
    ) -> None: ...

    async def materialize_memory_context(
        self, record: SubmissionRecoveryRecord, canonical_component: bytes
    ) -> None: ...

    async def materialize_selector_decision(
        self, record: SubmissionRecoveryRecord, canonical_component: bytes
    ) -> None: ...

    async def initialize_agent_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
        context: PreparedAgentRecoveryContext,
    ) -> DurableSubmissionHandoff: ...

    async def materialize_interrupt_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
    ) -> DurableSubmissionHandoff: ...

    async def materialize_no_server_intent_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
    ) -> DurableSubmissionHandoff: ...

    def skill_revision_error_code(self, revision: object) -> str | None: ...

    async def materialize_terminal_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
        receipt: SubmissionPreparationReceipt,
        safe_error_code: str,
    ) -> DurableSubmissionHandoff: ...

    async def wakeup_agent(
        self, record: SubmissionRecoveryRecord, handoff_identity: str
    ) -> None: ...


class _ClaimLeaseKeeper:
    def __init__(
        self,
        *,
        admission: ConversationTaskAdmissionPort,
        handle: SubmissionAdmissionHandle,
        now: Callable[[], datetime],
        wait_until: Callable[[datetime], Awaitable[None]],
        claim_ttl: timedelta,
    ) -> None:
        self._admission = admission
        self._handle = handle
        self._now = now
        self._wait_until = wait_until
        self._claim_ttl = claim_ttl
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("submission_claim_keeper_already_started")
        self._task = asyncio.create_task(self._run())

    async def call(self, operation: Callable[[SubmissionAdmissionHandle], Awaitable[Any]]) -> Any:
        async with self._lock:
            self._raise_failure()
            return await operation(self._handle)

    async def renew_now(self) -> None:
        async with self._lock:
            self._raise_failure()
            await self._renew_locked()

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                await self._wait_until(self._now() + self._claim_ttl / 3)
                if self._stopped.is_set():
                    return
                async with self._lock:
                    await self._renew_locked()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._failure = exc

    async def _renew_locked(self) -> None:
        now = self._now()
        try:
            self._handle = await self._admission.renew_submission_claim(
                SubmissionClaimRenewalRequest(
                    handle=self._handle,
                    now=now,
                    claim_expires_at=now + self._claim_ttl,
                )
            )
        except BaseException as exc:
            self._failure = exc
            raise SubmissionRecoveryError("submission_claim_renewal_failed") from exc

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise SubmissionRecoveryError("submission_claim_renewal_failed") from self._failure


class SubmissionAdmissionCoordinator:
    """Checkpoint-A recovery sequence, isolated from the production submit path."""

    def __init__(
        self,
        *,
        admission: ConversationTaskAdmissionPort,
        receipts: SubmissionPreparationReceiptStoragePort,
        callbacks: SubmissionPreparationCallbacks,
        claim_owner: str,
        now: Callable[[], datetime],
        wait_until: Callable[[datetime], Awaitable[None]],
        expected_finalization_receipt_sha256: str | None,
        claim_ttl: timedelta = timedelta(seconds=30),
        recovery_limit: int = _DEFAULT_RECOVERY_LIMIT,
    ) -> None:
        if (
            not claim_owner
            or claim_ttl <= timedelta(0)
            or recovery_limit < 1
            or expected_finalization_receipt_sha256 is not None
            and not _is_sha256(expected_finalization_receipt_sha256)
        ):
            raise ValueError("submission_recovery_configuration_invalid")
        self._admission = admission
        self._receipts = receipts
        self._callbacks = callbacks
        self._claim_owner = claim_owner
        self._now = now
        self._wait_until = wait_until
        self._expected_finalization_receipt_sha256 = (
            expected_finalization_receipt_sha256
        )
        self._claim_ttl = claim_ttl
        self._recovery_limit = recovery_limit
        self._pending_batch: list[
            tuple[SubmissionRecoveryRecord, _ClaimLeaseKeeper]
        ] | None = None
        self._phase = "idle"
        self._operation_lock = asyncio.Lock()

    async def continue_admitted(
        self,
        result: SubmissionAdmissionResult,
        *,
        reclaim_exact: Callable[
            [SubmissionRecoveryRecord], Awaitable[SubmissionAdmissionResult]
        ]
        | None = None,
    ) -> SubmissionRecoveryBatchResult:
        """Continue one live admission directly from its supplied claim."""

        if result.disposition not in {
            SubmissionAdmissionDisposition.CREATED,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        }:
            raise SubmissionRecoveryError("submission_admission_result_invalid")
        if (
            not result.conversation_id
            or not result.message_id
            or not result.task_id
        ):
            raise SubmissionRecoveryError("submission_admission_result_invalid")
        record = result.record
        if record is not None and (
            record.conversation_id != result.conversation_id
            or record.message_id != result.message_id
            or record.task_id != result.task_id
            or result.phase != record.phase
        ):
            raise SubmissionRecoveryError("submission_admission_result_invalid")
        if (
            record is not None
            and record.phase.handoff_state is SubmissionHandoffState.HANDED_OFF
        ):
            await self._retry_handed_off_wakeup(record)
            return SubmissionRecoveryBatchResult(
                status=SubmissionRecoveryStatus.COMPLETED,
                recovered_count=0,
                pending_count=0,
            )
        if result.handle is None:
            if record is None:
                raise SubmissionRecoveryError("submission_admission_result_invalid")
            if reclaim_exact is None:
                raise SubmissionRecoveryError("submission_exact_reclaim_missing")
            while result.handle is None:
                await self._wait_until(self._now() + self._claim_ttl)
                result = await reclaim_exact(record)
                self._validate_exact_reclaim(record, result)
                assert result.record is not None
                record = result.record
                if record.phase.handoff_state is SubmissionHandoffState.HANDED_OFF:
                    await self._retry_handed_off_wakeup(record)
                    return SubmissionRecoveryBatchResult(
                        status=SubmissionRecoveryStatus.COMPLETED,
                        recovered_count=0,
                        pending_count=0,
                    )
            handle = result.handle
            if handle is None:
                return SubmissionRecoveryBatchResult(
                    status=SubmissionRecoveryStatus.COMPLETED,
                    recovered_count=0,
                    pending_count=0,
                )
        else:
            handle = result.handle
        if (
            record is None
            or record.phase.admission_state is not SubmissionAdmissionState.OPEN
            or record.phase.handoff_state is not SubmissionHandoffState.PENDING
        ):
            raise SubmissionRecoveryError("submission_admission_phase_invalid")

        keeper = _ClaimLeaseKeeper(
            admission=self._admission,
            handle=handle,
            now=self._now,
            wait_until=self._wait_until,
            claim_ttl=self._claim_ttl,
        )
        keeper.start()
        try:
            if (
                record.phase.projection_state
                is SubmissionProjectionState.PENDING
            ):
                projected_phase = await keeper.call(
                    lambda current_handle: self._admission.acknowledge_submission_projection(
                        SubmissionProjectionAcknowledgementRequest(
                            handle=current_handle,
                            projection_sha256=record.projection_sha256,
                            acknowledged_at=self._now(),
                        )
                    )
                )
            else:
                projected_phase = record.phase
            if (
                projected_phase.admission_state is not SubmissionAdmissionState.OPEN
                or projected_phase.projection_state
                is not SubmissionProjectionState.PROJECTED
                or projected_phase.handoff_state is not SubmissionHandoffState.PENDING
            ):
                raise SubmissionRecoveryError(
                    "submission_projection_ack_phase_invalid"
                )
            projected_record = replace(record, phase=projected_phase)
            await self._recover_handoff(projected_record, keeper)
            return SubmissionRecoveryBatchResult(
                status=SubmissionRecoveryStatus.COMPLETED,
                recovered_count=1,
                pending_count=0,
                after_created_at=projected_record.created_at,
                after_message_id=projected_record.message_id,
            )
        finally:
            await keeper.stop()

    @staticmethod
    def _validate_exact_reclaim(
        expected: SubmissionRecoveryRecord,
        result: SubmissionAdmissionResult,
    ) -> None:
        record = result.record
        if (
            result.disposition is not SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY
            or result.conversation_id != expected.conversation_id
            or result.message_id != expected.message_id
            or result.task_id != expected.task_id
            or record is None
            or record.username != expected.username
            or record.conversation_id != expected.conversation_id
            or record.message_id != expected.message_id
            or record.task_id != expected.task_id
            or record.created_at != expected.created_at
            or record.projection_sha256 != expected.projection_sha256
            or record.continuation_sha256 != expected.continuation_sha256
        ):
            raise SubmissionRecoveryError("submission_replay_claim_identity_mismatch")

    async def _retry_handed_off_wakeup(
        self,
        record: SubmissionRecoveryRecord,
    ) -> None:
        preparation = await self._admission.get_submission_preparation(
            SubmissionPreparationLookup(
                username=record.username,
                conversation_id=record.conversation_id,
                task_id=record.task_id,
            )
        )
        if (
            preparation is None
            or preparation.conversation_id != record.conversation_id
            or preparation.message_id != record.message_id
            or preparation.task_id != record.task_id
            or preparation.handoff_state is not SubmissionHandoffState.HANDED_OFF
        ):
            raise SubmissionRecoveryError("submission_replay_preparation_identity_mismatch")
        if preparation.handoff_kind == "agent_run":
            if preparation.handoff_identity != f"agent-run:{record.task_id}":
                raise SubmissionRecoveryError("submission_handoff_identity_drift")
            await self._callbacks.wakeup_agent(
                record,
                preparation.handoff_identity,
            )
        elif preparation.handoff_kind not in {"interrupt", "no_server_intent"}:
            raise SubmissionRecoveryError("submission_handoff_identity_drift")

    async def project_pending(self) -> SubmissionRecoveryBatchResult:
        """Claim and project a private batch without invoking preparation callbacks."""

        async with self._operation_lock:
            return await self._project_pending()

    async def _project_pending(self) -> SubmissionRecoveryBatchResult:
        if self._phase != "idle":
            raise SubmissionRecoveryError("submission_recovery_phase_conflict")
        self._phase = "projecting"
        claimed: list[tuple[SubmissionRecoveryRecord, _ClaimLeaseKeeper]] = []
        keepers: list[_ClaimLeaseKeeper] = []
        cursor_created_at: datetime | None = None
        cursor_message_id: str | None = None
        initial_pending_count = 0

        try:
            while True:
                now = self._now()
                claim = await self._admission.claim_pending_submission(
                    SubmissionClaimRequest(
                        claim_owner=self._claim_owner,
                        now=now,
                        claim_expires_at=now + self._claim_ttl,
                        after_created_at=cursor_created_at,
                        after_message_id=cursor_message_id,
                    )
                )
                self._validate_authority(claim)
                if not claimed:
                    initial_pending_count = claim.pending_count
                    if initial_pending_count > self._recovery_limit:
                        raise SubmissionRecoveryError(
                            "submission_recovery_backlog_exceeded"
                        )
                if not claim.found:
                    if claim.pending_count:
                        expiry = claim.earliest_claim_expires_at
                        if expiry is None:
                            raise SubmissionRecoveryError(
                                "submission_recovery_blocked_without_expiry"
                            )
                        await self._wait_until(expiry)
                        continue
                    break
                if claim.record is None or claim.handle is None:
                    raise SubmissionRecoveryError("submission_claim_incomplete")
                if len(claimed) >= self._recovery_limit:
                    raise SubmissionRecoveryError(
                        "submission_recovery_backlog_exceeded"
                    )
                record = claim.record
                if (
                    record.phase.admission_state is not SubmissionAdmissionState.OPEN
                    or record.phase.handoff_state is not SubmissionHandoffState.PENDING
                ):
                    raise SubmissionRecoveryError("submission_claim_phase_invalid")
                if cursor_created_at is not None and (
                    record.created_at,
                    record.message_id,
                ) <= (cursor_created_at, cursor_message_id or ""):
                    raise SubmissionRecoveryError(
                        "submission_recovery_cursor_not_stable"
                    )
                keeper = _ClaimLeaseKeeper(
                    admission=self._admission,
                    handle=claim.handle,
                    now=self._now,
                    wait_until=self._wait_until,
                    claim_ttl=self._claim_ttl,
                )
                keeper.start()
                keepers.append(keeper)
                projected_phase = await keeper.call(
                    lambda handle: self._admission.acknowledge_submission_projection(
                        SubmissionProjectionAcknowledgementRequest(
                            handle=handle,
                            projection_sha256=record.projection_sha256,
                            acknowledged_at=self._now(),
                        )
                    )
                )
                if (
                    projected_phase.projection_state
                    is not SubmissionProjectionState.PROJECTED
                ):
                    raise SubmissionRecoveryError(
                        "submission_projection_ack_phase_invalid"
                    )
                claimed.append((record, keeper))
                cursor_created_at, cursor_message_id = (
                    record.created_at,
                    record.message_id,
                )

            self._pending_batch = claimed
            self._phase = "projected"
            return SubmissionRecoveryBatchResult(
                status=SubmissionRecoveryStatus.COMPLETED,
                recovered_count=len(claimed),
                pending_count=0,
                after_created_at=cursor_created_at,
                after_message_id=cursor_message_id,
            )
        except BaseException:
            await asyncio.gather(
                *(keeper.stop() for keeper in keepers), return_exceptions=True
            )
            self._pending_batch = None
            self._phase = "idle"
            raise

    async def recover_projected_handoffs(self) -> SubmissionRecoveryBatchResult:
        """Continue only a projection-complete private batch."""

        async with self._operation_lock:
            return await self._recover_projected_handoffs()

    async def _recover_projected_handoffs(self) -> SubmissionRecoveryBatchResult:
        if self._phase != "projected" or self._pending_batch is None:
            raise SubmissionRecoveryError("submission_recovery_phase_conflict")
        self._phase = "recovering"
        claimed = self._pending_batch
        try:
            for record, keeper in claimed:
                await self._recover_handoff(record, keeper)
            return SubmissionRecoveryBatchResult(
                status=SubmissionRecoveryStatus.COMPLETED,
                recovered_count=len(claimed),
                pending_count=0,
                after_created_at=(claimed[-1][0].created_at if claimed else None),
                after_message_id=(claimed[-1][0].message_id if claimed else None),
            )
        finally:
            await self._abort_pending()

    async def abort_pending(self) -> None:
        """Stop every private claim keeper and discard the current batch."""

        async with self._operation_lock:
            await self._abort_pending()

    async def _abort_pending(self) -> None:
        claimed = self._pending_batch or []
        self._pending_batch = None
        self._phase = "idle"
        await asyncio.gather(
            *(keeper.stop() for _, keeper in claimed), return_exceptions=True
        )

    async def recover_pending(self) -> SubmissionRecoveryBatchResult:
        """Compatibility facade for tests; production readiness calls both phases."""

        async with self._operation_lock:
            await self._project_pending()
            return await self._recover_projected_handoffs()

    async def _recover_handoff(
        self,
        record: SubmissionRecoveryRecord,
        keeper: "_ClaimLeaseKeeper",
    ) -> None:
        continuation = _validated_continuation(record)
        await keeper.renew_now()
        if record.phase.preparation_state is SubmissionPreparationState.PREPARED:
            if (
                record.phase.projection_state
                is not SubmissionProjectionState.PROJECTED
                or record.prepared_execution is None
                or record.prepared_execution_sha256 is None
                or record.prepared_execution_sha256
                != _prepared_execution_digest(record.prepared_execution)
            ):
                raise SubmissionRecoveryError("submission_prepared_snapshot_missing")
            prepared_bytes = record.prepared_execution
            prepared = _validated_prepared_snapshot(
                record, continuation, prepared_bytes
            )
            receipt = await self._require_closed_receipt(
                record, continuation, prepared
            )
        else:
            receipt = await self._settle_receipt(
                record, continuation, keeper
            )
            prepared_bytes = _build_prepared_snapshot(record, continuation, receipt)
            prepared_sha256 = _prepared_execution_digest(prepared_bytes)
            await keeper.renew_now()
            prepared_record = await keeper.call(
                lambda handle: self._admission.prepare_submission_handoff(
                    SubmissionPreparationRequest(
                        handle=handle,
                        prepared_execution=prepared_bytes,
                        prepared_execution_sha256=prepared_sha256,
                        prepared_at=self._now(),
                    )
                )
            )
            if (
                prepared_record.conversation_id != record.conversation_id
                or prepared_record.message_id != record.message_id
                or prepared_record.task_id != record.task_id
                or prepared_record.handoff_state
                is not SubmissionHandoffState.PENDING
                or prepared_record.handoff_kind is not None
                or prepared_record.handoff_identity is not None
            ):
                raise SubmissionRecoveryError("submission_prepare_response_invalid")
            prepared_bytes = prepared_record.prepared_execution
            if prepared_record.prepared_execution_sha256 != _prepared_execution_digest(
                prepared_bytes
            ):
                raise SubmissionRecoveryError("submission_prepared_digest_mismatch")
            prepared = _validated_prepared_snapshot(
                record, continuation, prepared_bytes
            )
            receipt = await self._require_closed_receipt(
                record, continuation, prepared
            )

        agent_context = (
            _build_prepared_agent_recovery_context(
                record,
                prepared=prepared,
                receipt=receipt,
                root_message_content=_root_message_content(record),
            )
            if prepared["planned_handoff_kind"] == "agent_run"
            else None
        )
        materialization_record = replace(
            record,
            prepared_execution=prepared_bytes,
            prepared_execution_sha256=_prepared_execution_digest(prepared_bytes),
        )
        safe_error_code = self._callbacks.skill_revision_error_code(
            prepared["bundle_revisions"].get("skill_bundle_revision")
        )
        if safe_error_code is not None:
            await keeper.renew_now()
            handoff = await self._callbacks.materialize_terminal_handoff(
                materialization_record,
                prepared,
                receipt,
                safe_error_code,
            )
            _validate_handoff(record, prepared, handoff)
            await keeper.renew_now()
            handoff_phase = await keeper.call(
                lambda handle: self._admission.acknowledge_submission_handoff(
                    SubmissionHandoffAcknowledgementRequest(
                        handle=handle,
                        prepared_execution_sha256=_prepared_execution_digest(
                            prepared_bytes
                        ),
                        handoff_kind=handoff.kind,
                        handoff_identity=handoff.identity,
                        acknowledged_at=self._now(),
                    )
                )
            )
            if handoff_phase.handoff_state is not SubmissionHandoffState.HANDED_OFF:
                raise SubmissionRecoveryError("submission_handoff_ack_phase_invalid")
            await keeper.stop()
            return
        await keeper.renew_now()
        await self._callbacks.materialize_route_decision(
            materialization_record, _required_component(receipt.route_decision)
        )
        if prepared["prepared_kind"] != "no_server_intent":
            await self._callbacks.materialize_memory_context(
                materialization_record, _required_component(receipt.memory_context)
            )
            await self._callbacks.materialize_selector_decision(
                materialization_record, _required_component(receipt.selector_decision)
            )
        handoff = await self._durable_handoff(record, prepared, agent_context)
        _validate_handoff(record, prepared, handoff)
        await keeper.renew_now()
        handoff_phase = await keeper.call(
            lambda handle: self._admission.acknowledge_submission_handoff(
                SubmissionHandoffAcknowledgementRequest(
                    handle=handle,
                    prepared_execution_sha256=_prepared_execution_digest(prepared_bytes),
                    handoff_kind=handoff.kind,
                    handoff_identity=handoff.identity,
                    acknowledged_at=self._now(),
                )
            )
        )
        if handoff_phase.handoff_state is not SubmissionHandoffState.HANDED_OFF:
            raise SubmissionRecoveryError("submission_handoff_ack_phase_invalid")
        await keeper.stop()
        if handoff.kind == "agent_run":
            await self._callbacks.wakeup_agent(record, handoff.identity)

    async def _settle_receipt(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
        keeper: "_ClaimLeaseKeeper",
    ) -> SubmissionPreparationReceipt:
        receipt = await self._receipts.get_submission_preparation_receipt(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
        )
        if receipt is None or receipt.route_decision is None:
            await keeper.renew_now()
            receipt = await self._callbacks.settle_route_decision_exact(
                record, continuation, self._now()
            )
            await keeper.renew_now()
        if (
            receipt.task_id != record.task_id
            or receipt.conversation_id != record.conversation_id
            or receipt.route_decision is None
            or receipt.route_decision_sha256
            != hashlib.sha256(receipt.route_decision).hexdigest()
        ):
            raise SubmissionRecoveryError("submission_route_receipt_invalid")
        _validate_component_bytes(
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            receipt.route_decision,
        )
        component_specs: tuple[
            tuple[
                SubmissionPreparationReceiptComponent,
                str,
                Callable[[SubmissionRecoveryRecord, Mapping[str, Any]], Awaitable[object]],
            ],
            ...,
        ] = (
            (
                SubmissionPreparationReceiptComponent.MEMORY_CONTEXT,
                "memory_context",
                self._callbacks.compute_memory_context,
            ),
            (
                SubmissionPreparationReceiptComponent.SELECTOR_DECISION,
                "selector_decision",
                self._callbacks.compute_selector_decision,
            ),
        )
        for component, attribute, compute in component_specs:
            existing = None if receipt is None else getattr(receipt, attribute)
            if existing is None:
                route_is_no_server = (
                    receipt is not None
                    and receipt.route_decision is not None
                    and isinstance(
                        route_value := _parse_component(receipt.route_decision),
                        Mapping,
                    )
                    and route_value.get("decision") == "no_server"
                )
                value = (
                    None
                    if route_is_no_server
                    and component
                    in {
                        SubmissionPreparationReceiptComponent.MEMORY_CONTEXT,
                        SubmissionPreparationReceiptComponent.SELECTOR_DECISION,
                    }
                    else await compute(record, continuation)
                )
                _validate_component_value(component, value)
                if component is SubmissionPreparationReceiptComponent.MEMORY_CONTEXT:
                    _validate_memory_record_binding(record, value)
                _validate_no_server_component(receipt, component, value)
                canonical = canonical_json_bytes(value)
                # A long pure computation cannot authorize a SQL write with an
                # expired or asynchronously failed claim.
                await keeper.renew_now()
                receipt = await self._receipts.write_submission_preparation_component(
                    username=record.username,
                    conversation_id=record.conversation_id,
                    task_id=record.task_id,
                    component=component,
                    canonical_json=canonical,
                    component_sha256=hashlib.sha256(canonical).hexdigest(),
                    written_at=self._now(),
                )
                await keeper.renew_now()
            else:
                _validate_component_bytes(component, existing)
                if component is SubmissionPreparationReceiptComponent.MEMORY_CONTEXT:
                    _validate_memory_record_binding(
                        record, _parse_component(existing)
                    )
                _validate_no_server_component(
                    receipt, component, _parse_component(existing)
                )
        return await self._receipts.close_submission_preparation_receipt(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            closed_at=self._now(),
        )

    async def _require_closed_receipt(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
        prepared: Mapping[str, Any],
    ) -> SubmissionPreparationReceipt:
        receipt = await self._receipts.get_submission_preparation_receipt(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
        )
        _validate_closed_receipt_exact(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            locator=prepared["preparation_receipt"],
            receipt=receipt,
        )
        assert receipt is not None
        _validate_route_for_continuation(
            _parse_component(receipt.route_decision), continuation
        )
        _validate_prepared_receipt_facts(continuation, prepared, receipt)
        _validate_prepared_execution_text(record, continuation, prepared, receipt)
        return receipt

    async def _durable_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
        agent_context: PreparedAgentRecoveryContext | None,
    ) -> DurableSubmissionHandoff:
        kind = prepared["planned_handoff_kind"]
        if kind == "agent_run":
            if agent_context is None:
                raise SubmissionRecoveryError("submission_prepared_agent_context_missing")
            return await self._callbacks.initialize_agent_handoff(
                record, prepared, agent_context
            )
        if kind == "interrupt":
            return await self._callbacks.materialize_interrupt_handoff(record, prepared)
        return await self._callbacks.materialize_no_server_intent_handoff(record, prepared)

    def _validate_authority(self, claim: Any) -> None:
        if (
            claim.authority_state is not SubmissionAuthorityState.FINALIZED
            or claim.finalization_receipt_sha256
            != self._expected_finalization_receipt_sha256
        ):
            raise SubmissionRecoveryError("submission_authority_receipt_mismatch")


def _validated_continuation(record: SubmissionRecoveryRecord) -> dict[str, Any]:
    value = _parse_canonical_mapping(record.continuation)
    try:
        validate_runtime_sidecar_submission_envelopes(
            conversation_projection=record.conversation_projection,
            message_projection=record.message_projection,
            continuation=record.continuation,
            projection_sha256=record.projection_sha256,
            continuation_sha256=record.continuation_sha256,
            username=record.username,
            conversation_id=record.conversation_id,
            message_id=record.message_id,
            task_id=record.task_id,
            request_fingerprint=str(value.get("request_fingerprint")),
            routing_mode=str(value.get("routing_mode")),
            requested_capability_id=value.get("requested_capability_id"),
        )
    except (TypeError, ValueError) as exc:
        raise SubmissionRecoveryError("submission_continuation_invalid") from exc
    return value


def _build_prepared_snapshot(
    record: SubmissionRecoveryRecord,
    continuation: Mapping[str, Any],
    receipt: SubmissionPreparationReceipt,
) -> bytes:
    if receipt.receipt_sha256 is None:
        raise SubmissionRecoveryError("submission_preparation_receipt_not_closed")
    route = _parse_component(receipt.route_decision)
    memory = _parse_component(receipt.memory_context)
    selector = _parse_component(receipt.selector_decision)
    _validate_route_for_continuation(route, continuation)
    if (
        isinstance(route, Mapping)
        and route.get("decision") == "no_server"
        and (memory is not None or selector is not None)
    ):
        raise SubmissionRecoveryError("submission_no_server_component_conflict")
    route_decision = route.get("decision") if isinstance(route, Mapping) else None
    planned_kind = (
        "no_server_intent"
        if route_decision == "no_server"
        else "interrupt"
        if isinstance(selector, Mapping) and selector.get("interrupt_kind") is not None
        else "agent_run"
    )
    memory_prompt = (
        memory.get("prompt_payload")
        if isinstance(memory, Mapping)
        and not (
            isinstance(memory.get("event_write"), Mapping)
            and memory["event_write"].get("event_type")
            == "conversation.memory_fallback"
        )
        else None
    )
    execution_source = (
        "pending_context"
        if continuation["pending_context"] is not None
        else "memory_context"
        if memory_prompt is not None
        else "root_message"
    )
    execution_text = _execution_text_from_components(
        record,
        continuation,
        receipt.memory_context,
        execution_source,
    )
    value = {
        "schema": _PREPARED_SCHEMA_V2,
        "task_id": record.task_id,
        "conversation_id": record.conversation_id,
        "message_id": record.message_id,
        "prepared_kind": planned_kind,
        "owner_scope": continuation["owner_scope"],
        "execution_text_source": execution_source,
        "execution_text_sha256": hashlib.sha256(execution_text.encode("utf-8")).hexdigest(),
        "requested_capability_id": continuation["requested_capability_id"],
        "initial_required_tool_name": _initial_required_tool_name(
            continuation["routing_mode"], continuation["requested_capability_id"]
        ),
        "model_options": continuation["model_options"],
        "bundle_revisions": continuation["bundle_revisions"],
        "execution_metadata": continuation["execution_metadata"],
        "preparation_receipt": {
            "task_id": receipt.task_id,
            "receipt_sha256": receipt.receipt_sha256,
            "route_decision_sha256": receipt.route_decision_sha256,
            "memory_context_sha256": receipt.memory_context_sha256,
            "selector_decision_sha256": receipt.selector_decision_sha256,
        },
        "upload_refs": continuation["upload_refs"],
        "sheet_selections": continuation["sheet_selections"],
        "mcp_binding": continuation["mcp_binding"],
        "mcp_assignment": continuation["mcp_assignment"],
        "available_mcp_servers": (
            route["available_mcp_servers"]
            if isinstance(route, Mapping)
            and route.get("decision") in {"retry_route", "no_server"}
            else continuation["available_mcp_servers"]
        ),
        "pending_context": continuation["pending_context"],
        "planned_handoff_kind": planned_kind,
        "routing_mode": continuation["routing_mode"],
        "skill_activation": continuation.get("skill_activation"),
    }
    _reject_forbidden(value)
    rendered = canonical_json_bytes(value)
    if len(rendered) > _MAX_PREPARED_BYTES:
        raise SubmissionRecoveryError("submission_prepared_snapshot_oversize")
    _validated_prepared_snapshot(record, continuation, rendered)
    return rendered


def _validated_prepared_snapshot(
    record: SubmissionRecoveryRecord,
    continuation: Mapping[str, Any],
    content: bytes,
) -> dict[str, Any]:
    value = _validated_prepared_content(
        content,
        conversation_id=record.conversation_id,
        message_id=record.message_id,
        task_id=record.task_id,
    )
    if (
        value.get("owner_scope") != continuation["owner_scope"]
        or value.get("requested_capability_id")
        != continuation["requested_capability_id"]
        or value.get("routing_mode") != continuation["routing_mode"]
        or value.get("skill_activation") != continuation.get("skill_activation")
        or value.get("initial_required_tool_name")
        != _initial_required_tool_name(
            continuation["routing_mode"], continuation["requested_capability_id"]
        )
        or value.get("model_options") != continuation["model_options"]
        or value.get("bundle_revisions") != continuation["bundle_revisions"]
        or value.get("execution_metadata") != continuation["execution_metadata"]
        or value.get("upload_refs") != continuation["upload_refs"]
        or value.get("sheet_selections") != continuation["sheet_selections"]
        or value.get("mcp_binding") != continuation["mcp_binding"]
        or value.get("mcp_assignment") != continuation["mcp_assignment"]
        or value.get("pending_context") != continuation["pending_context"]
    ):
        raise SubmissionRecoveryError("submission_prepared_snapshot_invalid")
    return value


def _validated_prepared_content(
    content: bytes,
    *,
    conversation_id: str,
    message_id: str,
    task_id: str,
) -> dict[str, Any]:
    if len(content) > _MAX_PREPARED_BYTES:
        raise SubmissionRecoveryError("submission_prepared_snapshot_oversize")
    value = _parse_canonical_mapping(content)
    legacy_keys = {
        "schema",
        "task_id",
        "conversation_id",
        "message_id",
        "prepared_kind",
        "owner_scope",
        "execution_text_source",
        "execution_text_sha256",
        "requested_capability_id",
        "initial_required_tool_name",
        "model_options",
        "bundle_revisions",
        "execution_metadata",
        "preparation_receipt",
        "upload_refs",
        "sheet_selections",
        "mcp_binding",
        "mcp_assignment",
        "available_mcp_servers",
        "pending_context",
        "planned_handoff_kind",
    }
    schema = value.get("schema")
    expected_keys = (
        legacy_keys
        if schema == _PREPARED_SCHEMA_V1
        else legacy_keys | {"routing_mode", "skill_activation"}
        if schema == _PREPARED_SCHEMA_V2
        else None
    )
    try:
        if expected_keys is None or set(value) != expected_keys or (
            schema not in {_PREPARED_SCHEMA_V1, _PREPARED_SCHEMA_V2}
            or value.get("task_id") != task_id
            or value.get("conversation_id") != conversation_id
            or value.get("message_id") != message_id
            or not isinstance(value.get("owner_scope"), str)
            or not value["owner_scope"].strip()
            or value.get("prepared_kind")
            not in {"agent_run", "interrupt", "no_server_intent"}
            or value.get("planned_handoff_kind") != value.get("prepared_kind")
            or value.get("execution_text_source")
            not in {"root_message", "pending_context", "memory_context"}
            or not _is_sha256(value.get("execution_text_sha256"))
        ):
            raise SubmissionRecoveryError("submission_prepared_snapshot_invalid")
        if schema == _PREPARED_SCHEMA_V1:
            if value.get("initial_required_tool_name") != _legacy_initial_required_tool_name(
                value.get("requested_capability_id")
            ):
                raise SubmissionRecoveryError("submission_prepared_snapshot_invalid")
        else:
            _validate_prepared_v2_relations(value)
        _validate_model_options(value.get("model_options"))
        _validate_bundle_revisions(value.get("bundle_revisions"))
        _validate_execution_metadata(value.get("execution_metadata"))
        _validate_safe_references(value, conversation_id)
        _validate_prepared_mcp_relations(value)
    except (RuntimeError, TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, SubmissionRecoveryError):
            raise
        raise SubmissionRecoveryError("submission_prepared_snapshot_invalid") from exc
    receipt = value.get("preparation_receipt")
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "task_id",
        "receipt_sha256",
        "route_decision_sha256",
        "memory_context_sha256",
        "selector_decision_sha256",
    } or receipt.get("task_id") != task_id or any(
        not _is_sha256(receipt.get(key))
        for key in (
            "receipt_sha256",
            "route_decision_sha256",
            "memory_context_sha256",
            "selector_decision_sha256",
        )
    ):
        raise SubmissionRecoveryError("submission_prepared_snapshot_invalid")
    _reject_forbidden(value)
    return value


def _prepared_execution_digest(content: bytes) -> str:
    if len(content) > _MAX_PREPARED_BYTES:
        raise SubmissionRecoveryError("submission_prepared_snapshot_oversize")
    value = _parse_canonical_mapping(content)
    domain = _PREPARED_DOMAINS.get(value.get("schema"))
    if domain is None:
        raise SubmissionRecoveryError("submission_prepared_schema_unknown")
    return _domain_sha256(domain, content)


def _validate_prepared_v2_relations(value: Mapping[str, Any]) -> None:
    routing_mode = value.get("routing_mode")
    requested_capability_id = value.get("requested_capability_id")
    activation = value.get("skill_activation")
    if routing_mode not in {"auto", "hint", "force_capability"}:
        raise SubmissionRecoveryError("submission_prepared_routing_invalid")
    expected_required = _initial_required_tool_name(
        routing_mode,
        requested_capability_id,
    )
    if value.get("initial_required_tool_name") != expected_required:
        raise SubmissionRecoveryError("submission_prepared_routing_invalid")
    if routing_mode == "hint":
        if (
            not isinstance(requested_capability_id, str)
            or not requested_capability_id.startswith("skill.")
        ):
            raise SubmissionRecoveryError("submission_prepared_hint_invalid")
        _validate_prepared_skill_activation(
            activation,
            requested_capability_id=requested_capability_id,
            skill_bundle_revision=value["bundle_revisions"].get(
                "skill_bundle_revision"
            ),
        )
        if value.get("pending_context") is not None:
            raise SubmissionRecoveryError("submission_prepared_hint_invalid")
        return
    if activation is not None:
        raise SubmissionRecoveryError("submission_prepared_activation_unexpected")
    if routing_mode == "force_capability":
        if not isinstance(requested_capability_id, str) or not requested_capability_id:
            raise SubmissionRecoveryError("submission_prepared_force_invalid")
        return
    if requested_capability_id is None:
        return
    pending = value.get("pending_context")
    if (
        not isinstance(pending, Mapping)
        or pending.get("capability_id") != requested_capability_id
    ):
        raise SubmissionRecoveryError("submission_prepared_auto_capability_invalid")


def _validate_prepared_skill_activation(
    activation: object,
    *,
    requested_capability_id: str,
    skill_bundle_revision: object,
) -> None:
    if not isinstance(activation, Mapping) or set(activation) != {
        "payload",
        "payload_sha256",
    }:
        raise SubmissionRecoveryError("submission_prepared_hint_activation_invalid")
    payload_text = activation.get("payload")
    payload_sha256 = activation.get("payload_sha256")
    if (
        not isinstance(payload_text, str)
        or not _is_sha256(payload_sha256)
        or hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        != payload_sha256
    ):
        raise SubmissionRecoveryError("submission_prepared_hint_activation_invalid")
    try:
        payload_value = json.loads(payload_text)
        canonical = canonicalize_agent_payload(payload_value)
    except (json.JSONDecodeError, AgentPayloadError, TypeError, ValueError) as exc:
        raise SubmissionRecoveryError(
            "submission_prepared_hint_activation_invalid"
        ) from exc
    if canonical.json_text != payload_text or set(payload_value) != {
        "binding_mode",
        "pinned_bundle_revision",
        "profile",
        "profile_digest",
    }:
        raise SubmissionRecoveryError("submission_prepared_hint_activation_invalid")
    profile = payload_value.get("profile")
    if not isinstance(profile, Mapping):
        raise SubmissionRecoveryError("submission_prepared_hint_activation_invalid")
    try:
        profile_payload = canonicalize_agent_payload(dict(profile))
    except AgentPayloadError as exc:
        raise SubmissionRecoveryError(
            "submission_prepared_hint_activation_invalid"
        ) from exc
    if (
        payload_value.get("binding_mode") != "hint"
        or payload_value.get("pinned_bundle_revision") != skill_bundle_revision
        or not isinstance(skill_bundle_revision, str)
        or not skill_bundle_revision
        or profile.get("capability_id") != requested_capability_id
        or payload_value.get("profile_digest") != profile_payload.sha256
    ):
        raise SubmissionRecoveryError("submission_prepared_hint_activation_invalid")


def _prepared_routing_mode(prepared: Mapping[str, Any]) -> str:
    if prepared.get("schema") == _PREPARED_SCHEMA_V2:
        return str(prepared["routing_mode"])
    return (
        "force_capability"
        if prepared.get("initial_required_tool_name") is not None
        else "auto"
    )


def _prepared_activation_payload(prepared: Mapping[str, Any]) -> str | None:
    activation = prepared.get("skill_activation")
    return str(activation["payload"]) if isinstance(activation, Mapping) else None


def _prepared_activation_sha256(prepared: Mapping[str, Any]) -> str | None:
    activation = prepared.get("skill_activation")
    return (
        str(activation["payload_sha256"])
        if isinstance(activation, Mapping)
        else None
    )


def _validate_prepared_mcp_relations(prepared: Mapping[str, Any]) -> None:
    metadata = prepared["execution_metadata"]
    binding = prepared["mcp_binding"]
    profiles = prepared["available_mcp_servers"]
    if binding is None:
        if any(
            metadata[key] is not None
            for key in (
                "mcp_dispatch_server_id",
                "mcp_binding_mode",
                "mcp_command",
                "forced_by_mcp_command",
            )
        ):
            raise SubmissionRecoveryError("submission_prepared_mcp_binding_drift")
    elif (
        metadata["mcp_dispatch_server_id"] != binding["server_id"]
        or metadata["mcp_binding_mode"] != binding["binding_mode"]
        or metadata["mcp_command"] != binding["command"]
        or metadata["forced_by_mcp_command"] is not True
        or len(profiles) != 1
        or profiles[0]["server_id"] != binding["server_id"]
        or profiles[0]["display_name"] != binding["display_name"]
    ):
        raise SubmissionRecoveryError("submission_prepared_mcp_binding_drift")

    assignment = prepared["mcp_assignment"]
    assignment_fields = {
        "execution_mode": "mcp_execution_mode",
        "shadow_enabled": "mcp_shadow_enabled",
        "rollout_config_version": "mcp_rollout_config_version",
        "route_reason_code": "mcp_route_reason_code",
        "rollout_mode": "mcp_rollout_mode",
    }
    if assignment is None:
        if any(metadata[target] is not None for target in assignment_fields.values()):
            raise SubmissionRecoveryError("submission_prepared_mcp_assignment_drift")
    elif any(
        assignment[source] != metadata[target]
        for source, target in assignment_fields.items()
    ):
        raise SubmissionRecoveryError("submission_prepared_mcp_assignment_drift")


def _validate_component_value(
    component: SubmissionPreparationReceiptComponent, value: object
) -> None:
    if component is SubmissionPreparationReceiptComponent.ROUTE_DECISION:
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "decision",
            "owner_server_set_fingerprint",
            "available_mcp_servers",
        } or value.get("schema") != "maf.submission.route_decision.v1" or value.get(
            "decision"
        ) not in {"retry_route", "no_server", "not_applicable"}:
            raise SubmissionRecoveryError("submission_route_decision_invalid")
        decision = value["decision"]
        fingerprint = value["owner_server_set_fingerprint"]
        servers = value["available_mcp_servers"]
        try:
            _validate_available_mcp_servers(servers)
        except RuntimeError as exc:
            raise SubmissionRecoveryError("submission_route_decision_invalid") from exc
        if (
            not isinstance(servers, list)
            or (
            decision == "retry_route" and (not _is_sha256(fingerprint) or not servers)
            )
            or (
            decision == "no_server" and (not _is_sha256(fingerprint) or servers)
            )
            or (
            decision == "not_applicable" and (fingerprint is not None or servers)
            )
        ):
            raise SubmissionRecoveryError("submission_route_decision_invalid")
    elif component is SubmissionPreparationReceiptComponent.MEMORY_CONTEXT:
        _validate_memory_preparation(value)
    else:
        if value is not None and (
            not isinstance(value, Mapping)
            or set(value)
            != {
                "decision",
                "reason_code",
                "candidate_digest",
                "resume_action",
                "upload_ids",
                "interrupt_kind",
            }
            or not all(isinstance(value.get(key), str) for key in ("decision", "reason_code", "resume_action"))
            or not _is_sha256(value.get("candidate_digest"))
            or not isinstance(value.get("upload_ids"), list)
            or value["upload_ids"] != sorted(set(value["upload_ids"]))
            or not all(isinstance(item, str) and item for item in value["upload_ids"])
            or (value.get("interrupt_kind") is not None and not isinstance(value["interrupt_kind"], str))
        ):
            raise SubmissionRecoveryError("submission_selector_decision_invalid")
    _reject_forbidden(value)


def _validate_memory_preparation(value: object) -> None:
    if value is None:
        return
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema", "prompt_payload", "summary_write", "event_write"}
        or value.get("schema") != "maf.submission.memory_preparation.v1"
        or not isinstance(value.get("prompt_payload"), Mapping)
    ):
        raise SubmissionRecoveryError("submission_memory_context_invalid")
    prompt = value["prompt_payload"]
    keys = set(prompt)
    if not _MEMORY_PROMPT_REQUIRED_KEYS <= keys <= _MEMORY_PROMPT_KEYS:
        raise SubmissionRecoveryError("submission_memory_context_invalid")
    optional_strings = {"resolved_user_message", "history_summary", "fallback_reason"}
    if (
        not isinstance(prompt.get("current_user_message"), str)
        or not isinstance(prompt.get("compression_level"), str)
        or any(
            key in prompt and not isinstance(prompt[key], str)
            for key in optional_strings
        )
        or any(
            not _plain_nonnegative_int(prompt.get(key))
            for key in (
                "token_budget",
                "estimated_tokens_before",
                "estimated_tokens_after",
            )
        )
        or not isinstance(prompt.get("truncated"), bool)
        or not isinstance(prompt.get("resolution_metadata"), Mapping)
        or not isinstance(prompt.get("capability_summaries"), list)
    ):
        raise SubmissionRecoveryError("submission_memory_context_invalid")
    for key in ("recent_messages", "clarification_messages"):
        messages = prompt.get(key)
        if not isinstance(messages, list) or any(
            not _valid_memory_message(message) for message in messages
        ):
            raise SubmissionRecoveryError("submission_memory_context_invalid")
    candidates = prompt.get("memory_candidates")
    if not isinstance(candidates, list) or any(
        not _valid_memory_candidate(candidate) for candidate in candidates
    ):
        raise SubmissionRecoveryError("submission_memory_context_invalid")
    _validate_memory_summary_write(value.get("summary_write"))
    _validate_memory_event_write(value.get("event_write"))


def _valid_memory_message(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _MEMORY_MESSAGE_KEYS
        and all(
            isinstance(value.get(key), str) and bool(value[key])
            for key in ("message_id", "role", "kind")
        )
        and isinstance(value.get("content"), str)
        and (
            value.get("task_id") is None or isinstance(value.get("task_id"), str)
        )
        and (
            value.get("created_at") is None
            or isinstance(value.get("created_at"), str)
        )
    )


def _valid_memory_candidate(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _MEMORY_CANDIDATE_KEYS
        and all(
            isinstance(value.get(key), str) and bool(value[key])
            for key in ("candidate_id", "kind", "trim_policy")
        )
        and isinstance(value.get("content"), str)
        and _plain_nonnegative_int(value.get("priority"))
        and _plain_nonnegative_int(value.get("token_estimate"))
        and isinstance(value.get("metadata"), Mapping)
    )


def _validate_memory_summary_write(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != _MEMORY_SUMMARY_WRITE_KEYS:
        raise SubmissionRecoveryError("submission_memory_summary_write_invalid")
    digest = value.get("summary_sha256")
    subject = {key: item for key, item in value.items() if key != "summary_sha256"}
    if (
        value.get("schema") != "maf.submission.memory_summary_write.v1"
        or not _is_sha256(digest)
        or digest
        != _domain_sha256(
            b"maf.submission.memory_summary_write.v1\0",
            canonical_json_bytes(subject),
        )
        or not all(
            isinstance(value.get(key), str) and bool(value[key])
            for key in (
                "summary_id",
                "conversation_id",
                "username",
                "covered_until_turn_id",
                "covered_until_message_id",
                "summary_text",
                "summary_version",
                "compression_policy_version",
                "created_at",
                "updated_at",
            )
        )
        or not str(value["summary_id"]).startswith("memory-summary-")
        or value.get("summary_version") != SUMMARY_VERSION
        or value.get("compression_policy_version") != COMPRESSION_POLICY_VERSION
        or not _is_sha256(value.get("source_message_ids_hash"))
        or not _plain_nonnegative_int(value.get("source_message_count"))
        or not _plain_nonnegative_int(value.get("estimated_tokens"))
        or not isinstance(value.get("model_metadata_safe"), Mapping)
        or (
            value.get("covered_until_created_at") is not None
            and not isinstance(value.get("covered_until_created_at"), str)
        )
    ):
        raise SubmissionRecoveryError("submission_memory_summary_write_invalid")


def _validate_memory_event_write(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != _MEMORY_EVENT_WRITE_KEYS:
        raise SubmissionRecoveryError("submission_memory_event_write_invalid")
    digest = value.get("event_sha256")
    event_subject = {
        key: item
        for key, item in value.items()
        if key not in {"event_id", "event_sha256", "event_subject_sha256"}
    }
    event_subject_sha256 = _domain_sha256(
        b"maf.submission.memory_event.subject.v1\0",
        canonical_json_bytes(event_subject),
    )
    try:
        expected_event_id = submission_memory_event_id(
            str(value.get("task_id")),
            str(value.get("event_type")),
            event_subject_sha256,
        )
    except ValueError as exc:
        raise SubmissionRecoveryError("submission_memory_event_write_invalid") from exc
    subject = {key: item for key, item in value.items() if key != "event_sha256"}
    if (
        value.get("schema") != "maf.submission.memory_event_write.v1"
        or not _is_sha256(digest)
        or value.get("event_subject_sha256") != event_subject_sha256
        or value.get("event_id") != expected_event_id
        or digest
        != _domain_sha256(
            b"maf.submission.memory_event_write.v1\0",
            canonical_json_bytes(subject),
        )
        or not all(
            isinstance(value.get(key), str) and bool(value[key])
            for key in (
                "event_id",
                "conversation_id",
                "task_id",
                "event_type",
                "visibility",
                "created_at",
            )
        )
        or not isinstance(value.get("payload"), Mapping)
        or not _is_sha256(value.get("memory_identity_sha256"))
        or any(
            value.get(key) is not None and not isinstance(value.get(key), str)
            for key in ("node_id", "agent_id")
        )
    ):
        raise SubmissionRecoveryError("submission_memory_event_write_invalid")


def _validate_memory_record_binding(
    record: SubmissionRecoveryRecord, value: object
) -> None:
    _validate_memory_identity_binding(
        username=record.username,
        conversation_id=record.conversation_id,
        task_id=record.task_id,
        value=value,
    )


def _validate_memory_identity_binding(
    *,
    username: str,
    conversation_id: str,
    task_id: str,
    value: object,
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise SubmissionRecoveryError("submission_memory_context_invalid")
    summary = value.get("summary_write")
    summary_identity = hashlib.sha256(b"null").hexdigest()
    if summary is not None:
        if not isinstance(summary, Mapping) or (
            summary.get("conversation_id") != conversation_id
            or summary.get("username") != username
        ):
            raise SubmissionRecoveryError("submission_memory_summary_binding_conflict")
        try:
            expected_summary_id = _stable_memory_summary_id(
                conversation_id=conversation_id,
                username=username,
                covered_until_turn_id=str(summary["covered_until_turn_id"]),
                covered_until_message_id=str(summary["covered_until_message_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SubmissionRecoveryError(
                "submission_memory_summary_binding_conflict"
            ) from exc
        if summary.get("summary_id") != expected_summary_id:
            raise SubmissionRecoveryError("submission_memory_summary_binding_conflict")
        summary_identity = str(summary["summary_sha256"])
    event = value.get("event_write")
    if event is None:
        return
    if not isinstance(event, Mapping) or (
        event.get("conversation_id") != conversation_id
        or event.get("task_id") != task_id
        or event.get("memory_identity_sha256") != summary_identity
        or event.get("event_id")
        != submission_memory_event_id(
            task_id,
            str(event.get("event_type")),
            str(event.get("event_subject_sha256")),
        )
    ):
        raise SubmissionRecoveryError("submission_memory_event_binding_conflict")


def submission_memory_event_id(
    task_id: str, event_type: str, event_subject_sha256: str
) -> str:
    if not task_id or not event_type or not _is_sha256(event_subject_sha256):
        raise ValueError("submission_memory_event_identity_invalid")
    digest = hashlib.sha256(
        b"maf.submission.memory_event.identity.v1\0"
        + canonical_json_bytes(
            {
                "event_type": event_type,
                "event_subject_sha256": event_subject_sha256,
                "task_id": task_id,
            }
        )
    ).hexdigest()
    return f"submission-memory-event:v1:{task_id}:{digest}"


def _validate_closed_receipt_exact(
    *,
    username: str,
    conversation_id: str,
    task_id: str,
    locator: object,
    receipt: SubmissionPreparationReceipt | None,
) -> None:
    if (
        receipt is None
        or receipt.receipt_sha256 is None
        or receipt.task_id != task_id
        or receipt.conversation_id != conversation_id
    ):
        raise SubmissionRecoveryError("submission_preparation_receipt_not_closed")
    component_values = (
        (
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            receipt.route_decision,
            receipt.route_decision_sha256,
        ),
        (
            SubmissionPreparationReceiptComponent.MEMORY_CONTEXT,
            receipt.memory_context,
            receipt.memory_context_sha256,
        ),
        (
            SubmissionPreparationReceiptComponent.SELECTOR_DECISION,
            receipt.selector_decision,
            receipt.selector_decision_sha256,
        ),
    )
    for component, content, saved_sha256 in component_values:
        required = _required_component(content)
        if hashlib.sha256(required).hexdigest() != saved_sha256:
            raise SubmissionRecoveryError("submission_preparation_component_digest_drift")
        _validate_component_bytes(component, required)
    assert receipt.route_decision is not None
    assert receipt.memory_context is not None
    assert receipt.selector_decision is not None
    expected_receipt_sha256 = hashlib.sha256(
        b"maf.submission.preparation_receipt.v1\0"
        + receipt.route_decision
        + b"\0"
        + receipt.memory_context
        + b"\0"
        + receipt.selector_decision
    ).hexdigest()
    if receipt.receipt_sha256 != expected_receipt_sha256:
        raise SubmissionRecoveryError("submission_preparation_receipt_digest_drift")
    expected_locator = {
        "task_id": task_id,
        "receipt_sha256": expected_receipt_sha256,
        "route_decision_sha256": receipt.route_decision_sha256,
        "memory_context_sha256": receipt.memory_context_sha256,
        "selector_decision_sha256": receipt.selector_decision_sha256,
    }
    if locator != expected_locator:
        raise SubmissionRecoveryError("submission_preparation_receipt_drift")
    _validate_memory_identity_binding(
        username=username,
        conversation_id=conversation_id,
        task_id=task_id,
        value=_parse_component(receipt.memory_context),
    )


def _plain_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_component_bytes(
    component: SubmissionPreparationReceiptComponent, content: bytes
) -> None:
    value = _parse_component(content)
    _validate_component_value(component, value)


def _parse_component(content: bytes | None) -> object:
    if content is None:
        raise SubmissionRecoveryError("submission_preparation_component_missing")
    return _parse_canonical(content)


def _parse_canonical_mapping(content: bytes) -> dict[str, Any]:
    value = _parse_canonical(content)
    if not isinstance(value, dict):
        raise SubmissionRecoveryError("submission_canonical_object_required")
    return value


def _parse_canonical(content: bytes) -> object:
    if not isinstance(content, bytes):
        raise SubmissionRecoveryError("submission_canonical_bytes_required")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in items:
            if key in value:
                raise SubmissionRecoveryError("submission_canonical_duplicate_key")
            value[key] = item
        return value

    try:
        value = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if canonical_json_bytes(value) != content:
            raise SubmissionRecoveryError("submission_json_not_canonical")
    except SubmissionRecoveryError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SubmissionRecoveryError("submission_json_invalid") from exc
    return value


def _root_message_content(record: SubmissionRecoveryRecord) -> str:
    message = _parse_canonical_mapping(record.message_projection)
    content = message.get("content")
    if not isinstance(content, str):
        raise SubmissionRecoveryError("submission_message_projection_invalid")
    return content


def _domain_sha256(domain: bytes, content: bytes) -> str:
    return hashlib.sha256(domain + content).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _reject_forbidden(value: object) -> None:
    if isinstance(value, Mapping):
        if _FORBIDDEN_KEYS.intersection(value):
            raise SubmissionRecoveryError("submission_envelope_contains_forbidden_key")
        for nested in value.values():
            _reject_forbidden(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden(nested)


def _required_component(value: bytes | None) -> bytes:
    if value is None:
        raise SubmissionRecoveryError("submission_preparation_component_missing")
    return value


def _validate_handoff(
    record: SubmissionRecoveryRecord,
    prepared: Mapping[str, Any],
    handoff: DurableSubmissionHandoff,
) -> None:
    if handoff.kind != prepared["planned_handoff_kind"] or not handoff.identity:
        raise SubmissionRecoveryError("submission_handoff_identity_drift")
    expected = _expected_handoff_identity(record.task_id, prepared)
    if handoff.identity != expected:
        raise SubmissionRecoveryError("submission_handoff_identity_drift")


def _expected_handoff_identity(
    task_id: str,
    prepared: Mapping[str, Any],
) -> str:
    kind = prepared["planned_handoff_kind"]
    if kind == "agent_run":
        return f"agent-run:{task_id}"
    if kind == "no_server_intent":
        return mcp_no_server_intent_id(task_id)
    return submission_interrupt_handoff_id(
        task_id,
        str(prepared["preparation_receipt"]["selector_decision_sha256"]),
    )


def submission_interrupt_handoff_id(task_id: str, selector_decision_sha256: str) -> str:
    if not task_id or not _is_sha256(selector_decision_sha256):
        raise ValueError("submission_interrupt_identity_invalid")
    return f"submission-interrupt:v1:{task_id}:{selector_decision_sha256}"


def _initial_required_tool_name(
    routing_mode: object,
    requested_capability_id: object,
) -> str | None:
    if routing_mode != "force_capability":
        return None
    if not isinstance(requested_capability_id, str) or not requested_capability_id:
        raise SubmissionRecoveryError("submission_prepared_routing_invalid")
    return provider_safe_tool_name(requested_capability_id)


def _legacy_initial_required_tool_name(capability_id: object) -> str | None:
    return (
        None
        if capability_id is None
        else provider_safe_tool_name(str(capability_id))
    )


def _execution_text_from_components(
    record: SubmissionRecoveryRecord,
    continuation: Mapping[str, Any],
    memory_component: bytes | None,
    source: object,
) -> str:
    return _execution_text_from_facts(
        root_message_content=_root_message_content(record),
        facts=continuation,
        memory_component=memory_component,
        source=source,
    )


def _execution_text_from_facts(
    *,
    root_message_content: str | None,
    facts: Mapping[str, Any],
    memory_component: bytes | None,
    source: object,
) -> str:
    if root_message_content is None:
        raise SubmissionRecoveryError("submission_root_message_missing")
    root = root_message_content
    if source == "root_message":
        return root
    if source == "pending_context":
        pending = facts.get("pending_context")
        if not isinstance(pending, Mapping):
            raise SubmissionRecoveryError("submission_pending_context_missing")
        missing = "、".join(pending["missing_requirements"]) or "必需信息"
        return "\n\n".join(
            part
            for part in (
                pending["original_user_message"].strip(),
                f"此前缺少的信息：{missing}",
                f"用户补充：{root.strip()}",
            )
            if part
        )
    memory = _parse_component(memory_component)
    if not isinstance(memory, Mapping):
        raise SubmissionRecoveryError("submission_memory_context_missing")
    prompt = memory.get("prompt_payload")
    if not isinstance(prompt, Mapping):
        raise SubmissionRecoveryError("submission_memory_context_invalid")
    resolved = prompt.get("resolved_user_message")
    current = prompt.get("current_user_message")
    return (
        resolved.strip()
        if isinstance(resolved, str) and resolved.strip()
        else current.strip()
        if isinstance(current, str) and current.strip()
        else root
    )


def _validate_prepared_execution_text(
    record: SubmissionRecoveryRecord,
    continuation: Mapping[str, Any],
    prepared: Mapping[str, Any],
    receipt: SubmissionPreparationReceipt,
) -> None:
    execution_text = _execution_text_from_components(
        record,
        continuation,
        receipt.memory_context,
        prepared["execution_text_source"],
    )
    if hashlib.sha256(execution_text.encode("utf-8")).hexdigest() != prepared.get(
        "execution_text_sha256"
    ):
        raise SubmissionRecoveryError("submission_execution_text_digest_mismatch")


def _validate_prepared_receipt_facts(
    continuation: Mapping[str, Any],
    prepared: Mapping[str, Any],
    receipt: SubmissionPreparationReceipt,
) -> None:
    route = _parse_component(receipt.route_decision)
    memory = _parse_component(receipt.memory_context)
    selector = _parse_component(receipt.selector_decision)
    if not isinstance(route, Mapping):
        raise SubmissionRecoveryError("submission_route_decision_invalid")
    if route.get("decision") == "no_server" and (
        memory is not None or selector is not None
    ):
        raise SubmissionRecoveryError("submission_no_server_component_conflict")
    expected_kind = (
        "no_server_intent"
        if route.get("decision") == "no_server"
        else "interrupt"
        if isinstance(selector, Mapping) and selector.get("interrupt_kind") is not None
        else "agent_run"
    )
    expected_source = (
        "pending_context"
        if continuation.get("pending_context") is not None
        else "memory_context"
        if isinstance(memory, Mapping)
        and not (
            isinstance(memory.get("event_write"), Mapping)
            and memory["event_write"].get("event_type")
            == "conversation.memory_fallback"
        )
        else "root_message"
    )
    expected_servers = (
        route.get("available_mcp_servers")
        if route.get("decision") in {"retry_route", "no_server"}
        else continuation.get("available_mcp_servers")
    )
    if (
        prepared.get("prepared_kind") != expected_kind
        or prepared.get("planned_handoff_kind") != expected_kind
        or prepared.get("execution_text_source") != expected_source
        or prepared.get("available_mcp_servers") != expected_servers
    ):
        raise SubmissionRecoveryError("submission_prepared_receipt_fact_drift")


def _validate_route_for_continuation(
    route: object, continuation: Mapping[str, Any]
) -> None:
    if not isinstance(route, Mapping):
        raise SubmissionRecoveryError("submission_route_decision_invalid")
    eligible = continuation.get("initial_no_server_eligible")
    decision = route.get("decision")
    if (eligible is False and decision != "not_applicable") or (
        eligible is True and decision not in {"retry_route", "no_server"}
    ):
        raise SubmissionRecoveryError("submission_route_decision_conflict")


def _validate_no_server_component(
    receipt: SubmissionPreparationReceipt | None,
    component: SubmissionPreparationReceiptComponent,
    value: object,
) -> None:
    if (
        receipt is None
        or receipt.route_decision is None
        or component is SubmissionPreparationReceiptComponent.ROUTE_DECISION
    ):
        return
    route = _parse_component(receipt.route_decision)
    if (
        isinstance(route, Mapping)
        and route.get("decision") == "no_server"
        and value is not None
    ):
        raise SubmissionRecoveryError("submission_no_server_component_conflict")


__all__ = [
    "build_submission_admission_request",
    "DurableSubmissionHandoff",
    "PreparedAgentRecoveryContext",
    "PreparedAgentRecoveryLoader",
    "SubmissionAdmissionCoordinator",
    "SubmissionPreparedAgentRecoveryLoader",
    "SubmissionPreparationCallbacks",
    "SubmissionRecoveryBatchResult",
    "SubmissionRecoveryError",
    "SubmissionRecoveryStatus",
    "submission_interrupt_handoff_id",
    "submission_memory_event_id",
]
