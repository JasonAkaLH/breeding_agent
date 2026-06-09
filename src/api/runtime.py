from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import Engine

from src.auth import (
    AuthGenerationCache,
    AuthTokenValidationError,
    InMemoryAuthInvalidationBus,
    UsernameTokenService,
)
from src.capabilities.main_agent import (
    MAIN_AGENT_CAPABILITY_DESCRIPTORS,
    MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    MainAgentExecutor,
    MainAgentRuntimeReplanner,
    MainAgentWorkflowProvider,
    SkillOutputArtifactManager,
    StreamGenerator,
    build_local_main_agent_instance,
)
from src.capabilities.mcp_tool import MCPToolExecutor, build_local_mcp_tool_instance
from src.capabilities.skill_tool import SkillExecutor, build_local_skill_executor_instance
from src.core.enums import ConversationStatus, EventVisibility, InterruptStatus, MessageRole, NodeCriticality, NodeStatus, RoutingMode, TaskStatus
from src.core.models import (
    Conversation,
    EventRecord,
    Interrupt,
    InterruptAnswer,
    Message,
    PendingSkillContext,
    SlotCollection,
    SlotEvent,
    Task,
    TaskInputAttachment,
    TaskNode,
)
from src.integrations.audit_logger import JsonlAuditSink
from src.integrations.agent_skills import (
    SkillCapabilityRegistry,
    SkillCatalog,
    SkillPlatformHandlerRegistry,
    SkillInputTextGenerator,
    SkillServiceRegistry,
    SkillRuntimeRefreshResult,
    SkillRuntimeState,
    SkillScriptRunner,
    SkillResourceService,
    SlotExtractionCandidate,
    SlotExtractionResult,
    apply_extraction_result_to_collection,
    build_backend_slot_extraction,
    build_history_recall_prompt,
    build_normal_extraction_prompt,
    initialize_input_collection,
    load_input_schemas_for_contract,
    merge_slot_extraction_results,
    parse_slot_extraction_response,
    schema_from_snapshot,
    select_input_schema,
    should_trigger_history_recall,
    transition_slot_collection,
)
from src.integrations.agent_skills.missing_input_interrupt import (
    SLOT_COLLECTION_FIELD,
    SLOT_COLLECTION_METADATA_KEY,
    SLOT_COLLECTION_REF_FIELD,
    SLOT_COLLECTION_V2_SCHEMA_VERSION,
    slot_collection_from_required_fields,
    slot_collection_ref_from_required_fields,
    slot_collection_required_fields_ref,
)
from src.integrations.agent_skills.pyo3_policy import try_load_skill_runtime_pyo3_policy_client
from src.integrations.agent_skills.rust_contract import (
    error_policy as skill_runtime_error_policy,
    load_skill_runtime_contract,
)
from src.integrations.agent_skills.skill_sandbox_client import SkillSandboxGrpcClient
from src.integrations.agent_skills.skill_runtime_gates import validate_skill_runtime_artifact_provenance
from src.integrations.llm_client import DEFAULT_CONFIG_PATH, LLMClient, ReasoningEffort, bootstrap_config_env, load_config
from src.integrations.llm_request_options import (
    resolve_llm_model_edition,
    resolve_llm_reasoning_effort,
    resolve_llm_request_options,
    resolve_llm_thinking_enabled,
)
from src.integrations.llm_runtime import SharedLLMRuntime
from src.integrations.model_editions import config_for_model_edition, default_model_edition, model_edition_options, validate_model_edition
from src.integrations.mcp import MCPRuntimeBundle, MCPRuntimeConfig, MCPRuntimeRefreshResult, MCPRuntimeState, load_mcp_server_config
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.integrations.rust_safety_contract import configure_safety_shadow_sink
from src.lifecycle.cancellation_service import CancellationService
from src.lifecycle.conversation_guard import ConversationSerialGuard
from src.lifecycle.interrupt_service import InterruptService
from src.orchestration.answer_selection import select_final_text_artifact
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.conversation_memory import ConversationMemoryBuilder, ConversationMemoryConfig, ResolutionGenerator
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider, WorkflowPlanningError
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, OrchestrationRunResult, WorkflowPlan
from src.orchestration.planner_contract import TextGenerator as PlannerTextGenerator
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy
from src.orchestration.composite_executor import CompositeExecutor
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.runtime_replanner import CompositeRuntimeReplanner, RuntimeReplanner
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from src.orchestration.soft_skill_replanner import SoftSkillBindingReplanner
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.workflow_router import WorkflowRouter
from src.storage import StoragePort
from src.storage.rust_contract import error_policy, mode_for_component as runtime_sidecar_mode_for_component
from src.storage.runtime_sidecar_facade import (
    ensure_sidecar_write_allowed,
    validate_runtime_sidecar_artifact_provenance,
    validate_runtime_sidecar_response,
)
from src.storage.runtime_sidecar_grpc_client import RuntimeSidecarGrpcClient
from src.storage.runtime_sidecar_shadow import record_runtime_sidecar_shadow_write_sync
from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory
from src.storage.postgres import PostgreSQLStorage, bootstrap_postgres_database, create_postgres_engine, create_postgres_session_factory
from src.auth.postgres_invalidation_bus import PostgresAuthInvalidationBus
from src.state.runtime_factory import StatePlatformBackend, build_state_platform_runtime_config
from src.storage.artifact_files import LocalArtifactFileStore, parse_file_storage_ref, is_active_skill_output_file

from .conversation_titles import (
    ConversationTitleGenerator,
    build_conversation_title_prompt,
    build_conversation_title_source,
    call_title_generator,
    normalize_generated_conversation_title,
    validate_conversation_title,
)
from .dto import SubmitMessageRequest
from .sse import InMemoryEventBroker, is_frontend_event
from .table_upload_normalizer import normalize_selected_spreadsheet_sheet
from .upload_store import InMemoryUploadStore, UploadedFileRecord, UploadValidationError


UNFINISHED_TASK_STATUSES = {
    TaskStatus.ACCEPTED,
    TaskStatus.PLANNING,
    TaskStatus.RUNNING,
    TaskStatus.CANCELLING,
}

PENDING_SKILL_METADATA_KEYS = frozenset(
    {
        "continued_from_pending_skill_context",
        "pending_skill_capability_id",
        "pending_skill_missing_requirements",
        "pending_skill_original_user_message",
        "pending_skill_assistant_message",
        "defer_task_completed_until_pending_skill_context_processed",
    }
)
SOFT_SKILL_BINDING_METADATA_KEY = "soft_skill_binding"
SOFT_SKILL_INTERNAL_METADATA_KEYS = frozenset(
    {
        "forced_skill_name",
        "forced_skill_capability_id",
        "forced_skill_source",
        "macro_source",
        "macro_expansion",
        "macro_input_payload",
        "requires_public_skill_dependency",
        "requires_skill_dependency",
        "skill_execution_mode",
        "soft_skill_decision",
    }
)
RESUME_SKILL_INTERNAL_METADATA_KEYS = frozenset(
    {
        "resume_interrupted_node_id",
        "resume_finalizer_node_id",
    }
)
SYSTEM_MANAGED_METADATA_KEYS = frozenset(
    {
        "skill_bundle_revision",
        "mcp_bundle_revision",
        "uploaded_artifacts",
        "skill_artifacts",
        "artifacts",
        "conversation_memory",
        "memory_context",
    }
)
USER_SUPPLIED_METADATA_DENYLIST = frozenset(
    {
        *PENDING_SKILL_METADATA_KEYS,
        *SOFT_SKILL_INTERNAL_METADATA_KEYS,
        *RESUME_SKILL_INTERNAL_METADATA_KEYS,
        *SYSTEM_MANAGED_METADATA_KEYS,
    }
)

_SLOT_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed"})
_SLOT_WAITING_STATUSES = frozenset({"waiting_for_user", "collecting", "extracting", "validating"})
_INTERRUPT_TURN_SLOT_ANSWER_CONFIDENCE = 0.78
_INTERRUPT_TURN_LLM_METADATA = {"deep_thinking": True, "main_agent_reasoning_effort": "max"}
_INTERRUPT_OPEN_TURN_PART_KINDS = frozenset(
    {"slot_answer", "skill_question", "off_topic_guidance", "schema_switch", "ambiguous"}
)
_INTERRUPT_SCHEMA_SWITCH_EXECUTION_CONFIDENCE = 0.85
_V2_INTERRUPT_RAW_ANSWER_ALLOWED_KEYS = frozenset(
    {"text", "upload_ids", "sheet_selections", "upload_sheet_selections"}
)


@dataclass(frozen=True, slots=True)
class InterruptTurnDecision:
    intent: str
    confidence: float
    reason: str = ""
    clarification_answer: str = ""
    extracted_answer: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class InterruptResumeVerification:
    allow_resume: bool
    confidence: float
    reason: str = ""
    clarification_answer: str = ""


@dataclass(frozen=True, slots=True)
class InterruptOpenTurnPart:
    part_id: str
    kind: str
    text: str = ""
    target_slots: tuple[str, ...] = ()
    target_schema_id: str | None = None
    reuse_decision: str = "unspecified"
    execution_confirmation: bool = False
    execution_confirmation_confidence: float = 0.0
    uses_uploads: bool = False
    confidence: float = 0.0
    reason: str = ""
    blocks_resume: bool = False
    block_reason: str = ""
    assistant_message: str = ""


@dataclass(frozen=True, slots=True)
class InterruptOpenTurnPlan:
    parts: tuple[InterruptOpenTurnPart, ...]
    confidence: float = 0.0
    reason: str = ""
    fallback: bool = False
    fallback_reason: str = ""


def _is_v2_slot_collection_payload(value: Mapping[str, Any] | None) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        return int(value.get("schema_version") or 0) == SLOT_COLLECTION_V2_SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


def _sanitize_delete_error(exc: BaseException) -> str:
    message = str(exc).replace("\n", " ").replace("\r", " ").strip()
    redacted = []
    for token in message.split():
        lower = token.lower()
        if any(marker in lower for marker in ("password", "token", "secret", "apikey", "api_key", "postgresql://", "postgresql+psycopg://")):
            redacted.append("[redacted]")
        else:
            redacted.append(token)
    return " ".join(redacted)[:500] or exc.__class__.__name__


@dataclass(frozen=True, slots=True)
class _PendingSkillMissingInput:
    capability_id: str
    skill_name: str
    missing_requirements: tuple[str, ...]
    assistant_message: str
    source_node_id: str | None = None


class ApiRuntime:
    def __init__(
        self,
        *,
        engine: Engine,
        storage: StoragePort,
        capability_registry: CapabilityRegistry,
        instance_registry: InstanceRegistry,
        event_broker: InMemoryEventBroker,
        cancellation_service: CancellationService,
        interrupt_service: InterruptService,
        orchestration_service: OrchestrationService,
        workflow_provider: WorkflowRouter,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
        username_token_service: UsernameTokenService | None = None,
        conversation_title_generator: ConversationTitleGenerator | None = None,
        upload_store: InMemoryUploadStore | None = None,
        conversation_memory_builder: ConversationMemoryBuilder | None = None,
        artifact_file_store: LocalArtifactFileStore | None = None,
        audit_sink: JsonlAuditSink | None = None,
        skill_runtime_state: SkillRuntimeState | None = None,
        skill_input_text_generator: SkillInputTextGenerator | None = None,
        mcp_runtime_state: MCPRuntimeState | None = None,
        runtime_sidecar_client: Any | None = None,
        model_edition_config: Mapping[str, Any] | None = None,
        local_cancelled_task_ids: set[str] | None = None,
        auth_generation_cache: AuthGenerationCache | None = None,
        auth_invalidation_bus: InMemoryAuthInvalidationBus | None = None,
        postgres_auth_invalidation_bus: PostgresAuthInvalidationBus | None = None,
    ) -> None:
        self._engine = engine
        self.storage = storage
        self.capability_registry = capability_registry
        self.instance_registry = instance_registry
        self.event_broker = event_broker
        self.cancellation_service = cancellation_service
        self.interrupt_service = interrupt_service
        self.orchestration_service = orchestration_service
        self.workflow_provider = workflow_provider
        self._mysql_adapter = mysql_adapter
        self.username_token_service = username_token_service
        self.auth_generation_cache = auth_generation_cache or AuthGenerationCache()
        self.auth_invalidation_bus = auth_invalidation_bus
        self.postgres_auth_invalidation_bus = postgres_auth_invalidation_bus
        self._conversation_title_generator = conversation_title_generator
        self.upload_store = upload_store or InMemoryUploadStore(now_fn=self._utcnow_naive)
        self._conversation_memory_builder = conversation_memory_builder
        self.artifact_file_store = artifact_file_store or LocalArtifactFileStore(Path("runtime/artifacts"))
        self._audit_sink = audit_sink
        self._skill_runtime_state = skill_runtime_state
        self._skill_input_text_generator = skill_input_text_generator
        self._mcp_runtime_state = mcp_runtime_state
        self._runtime_sidecar_client = runtime_sidecar_client
        self._model_edition_config = dict(model_edition_config or {})
        self._runtime_sidecar_shadow_sink = _build_runtime_sidecar_shadow_diff_sink(audit_sink)
        configure_safety_shadow_sink(_build_safety_kernel_shadow_diff_sink(audit_sink))
        self._conversation_guard = ConversationSerialGuard(storage)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._conversation_delete_tasks: dict[str, asyncio.Task[dict[str, object]]] = {}
        self._locally_cancelled_task_ids = local_cancelled_task_ids if local_cancelled_task_ids is not None else set()
        self._running_title_tasks: set[asyncio.Task[None]] = set()
        self._task_skill_bundle_revisions: dict[str, str] = {}
        self._task_mcp_bundle_revisions: dict[str, str] = {}
        self._task_sheet_selection_resume_metadata: dict[str, dict[str, Any]] = {}
        self._assistant_history_sync_failure_task_ids: set[str] = set()
        self._assistant_history_sync_failure_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._skill_refresh_lock = asyncio.Lock()
        self._mcp_refresh_lock = asyncio.Lock()

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _make_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:12]}"

    def _make_event(
        self,
        *,
        task_id: str,
        conversation_id: str,
        event_type: str,
        node_id: str | None = None,
        payload: dict | None = None,
        visibility: EventVisibility = EventVisibility.FRONTEND,
        created_at: datetime | None = None,
    ) -> EventRecord:
        return EventRecord(
            event_id=self._make_id("evt"),
            conversation_id=conversation_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload or {},
            visibility=visibility,
            created_at=created_at or self._utcnow_naive(),
        )

    async def _record_event(self, event: EventRecord) -> None:
        event = _ensure_event_created_at(event)
        await self.storage.append_event(event)
        await self.event_broker.publish(event)

    async def _publish_transient_event(self, event: EventRecord) -> None:
        event = _ensure_event_created_at(event)
        await self.event_broker.publish_transient(event)

    async def _iter_event_replay_pages(self, task_id: str):
        after_event_id: str | None = None
        while True:
            page = await self.storage.list_event_page_for_task(task_id, after_event_id=after_event_id)
            if not page:
                return
            for event in page:
                yield event
            after_event_id = page[-1].event_id

    def model_editions_payload(self) -> dict[str, Any]:
        options = model_edition_options(self._model_edition_config)
        return {
            "default_model_edition": default_model_edition(self._model_edition_config),
            "options": [{"value": option.value, "label": option.label} for option in options],
        }

    def _validate_requested_model_edition(self, model_edition: str | None) -> str | None:
        return validate_model_edition(model_edition, config=self._model_edition_config)

    async def login_username(self, username: str):
        if self.username_token_service is None:
            raise RuntimeError("Username token service is not configured.")
        return await self.username_token_service.login_username(username)

    async def get_username_for_bearer(self, raw_token: str):
        if self.username_token_service is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return await self.username_token_service.get_current_token(raw_token)

    async def logout_bearer(self, raw_token: str):
        if self.username_token_service is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return await self.username_token_service.logout_bearer(raw_token)

    async def refresh_bearer(self, raw_token: str):
        if self.username_token_service is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return await self.username_token_service.refresh_bearer(raw_token)

    async def bearer_token_is_current_for_username(self, raw_token: str, username: str, *, touch: bool = True) -> bool:
        if self.username_token_service is None:
            return False
        return await self.username_token_service.token_is_current_for_username(raw_token, username, touch=touch)

    async def submit_chat_message(
        self,
        conversation_id: str,
        request: SubmitMessageRequest,
        *,
        authenticated_username: str | None = None,
    ) -> dict[str, object]:
        interrupt_result = await self._try_submit_chat_as_interrupt_turn(
            conversation_id,
            request,
            authenticated_username=authenticated_username,
        )
        if interrupt_result is not None:
            return interrupt_result
        message, task = await self.submit_message(
            conversation_id,
            request,
            authenticated_username=authenticated_username,
        )
        return {
            "conversation_id": conversation_id,
            "message_id": message.message_id,
            "task_id": task.task_id,
            "status": "accepted",
            "action": "task_accepted",
        }

    async def _try_submit_chat_as_interrupt_turn(
        self,
        conversation_id: str,
        request: SubmitMessageRequest,
        *,
        authenticated_username: str | None,
    ) -> dict[str, object] | None:
        if authenticated_username is None:
            raise ValueError("authenticated_username is required")
        requested_interrupt_id = self._chat_message_requested_interrupt_id(request.metadata)
        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None:
            if requested_interrupt_id:
                raise ValueError(f"Unknown open interrupt for current conversation: {requested_interrupt_id}")
            return None
        if conversation.username != authenticated_username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")

        active_task = await self.storage.get_active_task_for_conversation(conversation_id)
        if active_task is None:
            if requested_interrupt_id:
                replay_interrupt = await self.storage.get_interrupt(requested_interrupt_id)
                if (
                    replay_interrupt is None
                    or replay_interrupt.conversation_id != conversation_id
                    or slot_collection_ref_from_required_fields(replay_interrupt.required_fields) is None
                ):
                    raise ValueError(f"No active task is waiting for interrupt: {requested_interrupt_id}")
                message_id = request.client_message_id or self._make_id("msg")
                answer_payload = self._chat_message_interrupt_answer_payload(replay_interrupt, request, client_request_id=message_id)
                result = await self.answer_interrupt(
                    replay_interrupt.task_id,
                    replay_interrupt.interrupt_id,
                    answer_payload,
                    source_message_id=message_id,
                )
                return {
                    "conversation_id": conversation_id,
                    "message_id": str(result.get("source_message_id") or message_id),
                    "task_id": replay_interrupt.task_id,
                    "status": "accepted",
                    "action": self._chat_message_interrupt_action(result.get("action")),
                    "interrupt_id": replay_interrupt.interrupt_id,
                    "assistant_message": result.get("assistant_message"),
                    "answer_payload": dict(result.get("answer_payload") or {}),
                }
            return None
        open_interrupt = await self._select_open_interrupt_for_chat_turn(active_task.task_id, request.metadata)
        if open_interrupt is None:
            if requested_interrupt_id:
                replay_interrupt = await self.storage.get_interrupt(requested_interrupt_id)
                if (
                    replay_interrupt is not None
                    and replay_interrupt.task_id == active_task.task_id
                    and replay_interrupt.conversation_id == conversation_id
                    and slot_collection_ref_from_required_fields(replay_interrupt.required_fields) is not None
                ):
                    open_interrupt = replay_interrupt
                else:
                    return None
            else:
                return None

        message_id = request.client_message_id or self._make_id("msg")
        answer_payload = self._chat_message_interrupt_answer_payload(open_interrupt, request, client_request_id=message_id)
        result = await self.answer_interrupt(
            active_task.task_id,
            open_interrupt.interrupt_id,
            answer_payload,
            source_message_id=message_id,
        )
        return {
            "conversation_id": conversation_id,
            "message_id": str(result.get("source_message_id") or message_id),
            "task_id": active_task.task_id,
            "status": "accepted",
            "action": self._chat_message_interrupt_action(result.get("action")),
            "interrupt_id": open_interrupt.interrupt_id,
            "assistant_message": result.get("assistant_message"),
            "answer_payload": dict(result.get("answer_payload") or {}),
        }

    @staticmethod
    def _chat_message_interrupt_action(action: object) -> str:
        normalized = str(action or "resumed")
        if normalized.startswith("interrupt_"):
            return normalized
        if normalized == "clarification_answer":
            return "interrupt_clarification_answer"
        if normalized == "resumed":
            return "interrupt_resumed"
        return f"interrupt_{normalized}"

    async def _select_open_interrupt_for_chat_turn(
        self,
        task_id: str,
        metadata: Mapping[str, Any],
    ) -> Interrupt | None:
        await self._recover_missing_v2_slot_interrupts(task_id)
        interrupts = await self.storage.list_interrupts_for_task(task_id)
        open_interrupts = [interrupt for interrupt in interrupts if interrupt.status == InterruptStatus.OPEN]
        requested_interrupt_id = self._chat_message_requested_interrupt_id(metadata)
        if not open_interrupts:
            if requested_interrupt_id:
                replay_interrupt = await self.storage.get_interrupt(requested_interrupt_id)
                if replay_interrupt is not None and replay_interrupt.task_id == task_id:
                    return None
                raise ValueError(f"Unknown open interrupt for current task: {requested_interrupt_id}")
            return None
        if requested_interrupt_id:
            for interrupt in open_interrupts:
                if interrupt.interrupt_id == requested_interrupt_id:
                    return interrupt
            replay_interrupt = await self.storage.get_interrupt(requested_interrupt_id)
            if replay_interrupt is not None and replay_interrupt.task_id == task_id:
                return None
            raise ValueError(f"Unknown open interrupt for current task: {requested_interrupt_id}")
        if len(open_interrupts) == 1:
            return open_interrupts[0]
        raise ValueError("Multiple open interrupts are waiting; submit metadata.interrupt_id to choose one")

    @staticmethod
    def _chat_message_requested_interrupt_id(metadata: Mapping[str, Any]) -> str:
        return str(
            metadata.get("interrupt_id")
            or metadata.get("pending_interrupt_id")
            or ""
        ).strip()

    def _chat_message_interrupt_answer_payload(
        self,
        interrupt: Interrupt,
        request: SubmitMessageRequest,
        *,
        client_request_id: str,
    ) -> dict[str, object]:
        upload_ids = self._chat_message_upload_ids(request.metadata)
        sheet_selections = self._chat_message_sheet_selections(request.metadata)
        if slot_collection_ref_from_required_fields(interrupt.required_fields) is not None:
            answer: dict[str, object] = {"text": request.content}
            if upload_ids:
                answer["upload_ids"] = list(upload_ids)
            if sheet_selections:
                answer["sheet_selections"] = dict(sheet_selections)
            return {"client_request_id": client_request_id, "answer": answer}

        previous_slot_collection = slot_collection_from_required_fields(interrupt.required_fields)
        if _is_v2_slot_collection_payload(previous_slot_collection):
            answer = {"text": request.content}
            if upload_ids:
                answer["upload_ids"] = list(upload_ids)
            if sheet_selections:
                answer["sheet_selections"] = dict(sheet_selections)
            return {"client_request_id": client_request_id, "answer": answer}

        if interrupt.reason_code == "sheet_selection_required" or "upload_sheet_selections" in interrupt.required_fields:
            return {"upload_sheet_selections": dict(sheet_selections)}

        field_names = [field for field in interrupt.required_fields if not str(field).startswith("_")]
        payload: dict[str, object] = {}
        if len(field_names) == 1:
            field_name = str(field_names[0])
            payload[field_name] = (
                {"text": request.content, "upload_ids": list(upload_ids)}
                if upload_ids
                else request.content
            )
        else:
            payload["answer"] = request.content
        if upload_ids:
            payload["upload_ids"] = list(upload_ids)
        return payload

    def _chat_message_upload_ids(self, metadata: Mapping[str, Any]) -> tuple[str, ...]:
        raw_upload_ids = metadata.get("upload_ids")
        if raw_upload_ids in (None, ""):
            return ()
        return self._normalize_upload_ids(raw_upload_ids)

    def _chat_message_sheet_selections(self, metadata: Mapping[str, Any]) -> dict[str, str]:
        raw_sheet_selections = (
            metadata.get("upload_sheet_selections")
            or metadata.get("sheet_selections")
        )
        if raw_sheet_selections in (None, ""):
            return {}
        return self._normalize_upload_sheet_selections(raw_sheet_selections)

    async def submit_message(
        self,
        conversation_id: str,
        request: SubmitMessageRequest,
        *,
        authenticated_username: str | None = None,
    ) -> tuple[Message, Task]:
        if authenticated_username is None:
            raise ValueError("authenticated_username is required")
        selected_model_edition = self._validate_requested_model_edition(request.model_edition)
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if (
            authenticated_username is not None
            and existing_conversation is not None
            and existing_conversation.username != authenticated_username
        ):
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        await self._refresh_skills_for_new_conversation_if_needed(conversation_id, existing_conversation)
        await self._refresh_mcp_for_new_conversation_if_needed(conversation_id, existing_conversation)
        await self._conversation_guard.ensure_conversation_available(conversation_id)
        routing_mode = self._routing_mode(request.routing_mode)
        if routing_mode == RoutingMode.FORCE_CAPABILITY and not request.capability_id:
            raise ValueError("capability_id is required when routing_mode is force_capability")
        requested_capability_id = self._canonical_capability_id(request.capability_id)
        if requested_capability_id is not None and requested_capability_id.startswith("skill."):
            self._record_direct_skill_execution_rejected(
                capability_id=requested_capability_id,
                routing_mode=str(routing_mode),
                conversation_id=conversation_id,
            )
            raise ValueError(
                "direct_skill_execution_disabled: Direct skill execution is disabled; "
                "submit main_agent.respond with metadata.soft_skill_binding instead."
            )
        soft_skill_binding = self._normalize_soft_skill_binding(request.metadata)
        if soft_skill_binding is not None:
            requested_capability_id = "main_agent.respond"
        self._ensure_supported_capability(requested_capability_id)
        explicit_force_capability = routing_mode == RoutingMode.FORCE_CAPABILITY and requested_capability_id is not None
        continued_pending_context: PendingSkillContext | None = None
        superseded_pending_count = 0
        if soft_skill_binding is not None:
            superseded_pending_count = await self.storage.mark_pending_skill_context_superseded(conversation_id)
        elif explicit_force_capability:
            superseded_pending_count = await self.storage.mark_pending_skill_context_superseded(conversation_id)
        elif requested_capability_id is None:
            continued_pending_context = await self.storage.get_active_pending_skill_context(conversation_id)
            if continued_pending_context is not None:
                requested_capability_id = continued_pending_context.capability_id
                self._ensure_supported_capability(requested_capability_id)

        upload_context = await self.resolve_uploads_for_message(
            conversation_id,
            authenticated_username,
            request.metadata.get("upload_ids") or (),
            upload_sheet_selections=request.metadata.get("upload_sheet_selections"),
        )
        self._raise_missing_uploads(upload_context.get("missing_upload_ids"), context="message submission")
        now = self._utcnow_naive()
        message_id = request.client_message_id or self._make_id("msg")
        task_id = self._make_id("task")
        username = authenticated_username

        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None:
            conversation = Conversation(
                conversation_id=conversation_id,
                username=username,
                current_task_id=task_id,
                created_at=now,
                updated_at=now,
            )
        else:
            if authenticated_username is not None and conversation.username != authenticated_username:
                raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
            if conversation.status != ConversationStatus.ACTIVE:
                raise PermissionError(f"Conversation is not available: {conversation_id}")
            conversation = replace(conversation, username=username, current_task_id=task_id, updated_at=now)
        try:
            await self.storage.save_conversation(conversation)
        except ValueError as exc:
            if "Conversation is not available" in str(exc):
                raise PermissionError(f"Conversation is not available: {conversation_id}") from exc
            raise

        message = Message(
            message_id=message_id,
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=request.content,
            task_id=task_id,
            created_at=now,
        )
        await self.storage.save_message(message)
        title_metadata = self._drop_user_supplied_system_metadata(request.metadata)
        if selected_model_edition:
            title_metadata["model_edition"] = selected_model_edition
        await self._maybe_schedule_conversation_title_generation(conversation_id, metadata=title_metadata)

        task = Task(
            task_id=task_id,
            conversation_id=conversation_id,
            root_message_id=message_id,
            status=TaskStatus.ACCEPTED,
            routing_mode=routing_mode,
            requested_capability_id=requested_capability_id,
            summary=request.content,
            created_at=now,
            updated_at=now,
        )
        await self.storage.save_task(task)
        await self._record_event(
            self._make_event(
                task_id=task_id,
                conversation_id=conversation_id,
                event_type="task.accepted",
                payload={
                    "message_id": message_id,
                    "status": str(task.status),
                    **({"model_edition": selected_model_edition} if selected_model_edition else {}),
                },
                created_at=now,
            )
        )
        if superseded_pending_count:
            await self._record_event(
                self._make_event(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    event_type="pending_skill_context.superseded",
                    payload={"count": superseded_pending_count, "reason": "new_forced_capability"},
                    visibility=EventVisibility.AUDIT_ONLY,
                    created_at=now,
                )
            )

        metadata = self._drop_user_supplied_system_metadata(request.metadata)
        if soft_skill_binding is not None:
            metadata[SOFT_SKILL_BINDING_METADATA_KEY] = soft_skill_binding
            metadata["soft_skill_binding_source"] = "slash_command"
            metadata["soft_skill_binding_requested_capability_id"] = requested_capability_id
        if selected_model_edition:
            metadata["model_edition"] = selected_model_edition
        if request.capability_id != requested_capability_id and request.capability_id is not None:
            metadata["requested_capability_alias"] = request.capability_id
            metadata["canonical_capability_id"] = requested_capability_id
        execution_user_message = request.content
        current_user_message = None
        resolved_user_message = None
        if continued_pending_context is not None:
            metadata.update(self._pending_skill_continuation_metadata(continued_pending_context))
            execution_user_message = self._format_pending_skill_continuation_message(continued_pending_context, request.content)
            current_user_message = request.content
            resolved_user_message = execution_user_message
        if (
            soft_skill_binding is None
            and requested_capability_id is not None
            and requested_capability_id.startswith("skill.")
        ):
            metadata["defer_task_completed_until_pending_skill_context_processed"] = True
        if self._skill_runtime_state is not None:
            metadata["skill_bundle_revision"] = self._skill_runtime_state.active_revision
        if self._mcp_runtime_state is not None:
            metadata["mcp_bundle_revision"] = self._mcp_runtime_state.active_revision
        upload_ids = request.metadata.get("upload_ids") or ()
        if upload_context["uploaded_artifacts"]:
            await self._bind_task_input_uploads(
                task=task,
                username=authenticated_username,
                upload_ids=upload_ids,
                source_kind="message_upload",
                source_message_id=message_id,
                upload_sheet_selections=request.metadata.get("upload_sheet_selections"),
            )
            metadata.update(await self._task_input_attachment_metadata(task.task_id))
        if upload_context.get("pending_sheet_selections"):
            await self._open_sheet_selection_interrupt(
                task=task,
                metadata=metadata,
                pending_sheet_selections=upload_context["pending_sheet_selections"],
            )
            return message, task

        orchestration_request = OrchestrationRequest(
            task_id=task_id,
            conversation_id=conversation_id,
            root_message_id=message_id,
            user_message=execution_user_message,
            requested_capability_id=requested_capability_id,
            metadata=metadata,
            current_user_message=current_user_message,
            resolved_user_message=resolved_user_message,
        )
        await self._schedule_execution(orchestration_request)
        return message, task

    @staticmethod
    def _drop_user_supplied_system_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(metadata)
        for key in USER_SUPPLIED_METADATA_DENYLIST:
            values.pop(key, None)
        return values

    def _normalize_soft_skill_binding(self, metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        raw = metadata.get(SOFT_SKILL_BINDING_METADATA_KEY)
        if raw in (None, ""):
            return None
        if not isinstance(raw, Mapping):
            raise ValueError("metadata.soft_skill_binding must be an object")
        capability_id = self._canonical_capability_id(self._metadata_text(raw.get("capability_id")))
        if not capability_id or not capability_id.startswith("skill."):
            raise ValueError("metadata.soft_skill_binding.capability_id must be a public skill capability")
        descriptor = self.capability_registry.get(capability_id)
        if descriptor is None or not descriptor.public or not _is_skill_descriptor(descriptor):
            raise ValueError(f"Unsupported soft_skill_binding capability_id: {capability_id}")
        if self._skill_runtime_state is not None:
            bundle = self._skill_runtime_state.active_bundle
            if capability_id not in bundle.skill_capabilities.skill_name_by_capability_id:
                raise ValueError(f"Unsupported soft_skill_binding capability_id: {capability_id}")
            revision = bundle.revision
        else:
            revision = ""
        command = self._metadata_text(raw.get("command")) or self._metadata_text(metadata.get("slash_command")) or ""
        normalized = {
            "capability_id": capability_id,
        }
        if command:
            normalized["command"] = command
        if revision:
            normalized["skill_bundle_revision"] = revision
        return normalized

    def _record_direct_skill_execution_rejected(
        self,
        *,
        capability_id: str,
        routing_mode: str,
        conversation_id: str,
    ) -> None:
        if self._audit_sink is None:
            return
        self._audit_sink.record_sync(
            "skill.direct_execution_rejected",
            {
                "capability_id": capability_id,
                "routing_mode": routing_mode,
                "conversation_id": conversation_id,
                "reason": "direct_skill_execution_disabled",
            },
        )

    @staticmethod
    def _pending_skill_continuation_metadata(context: PendingSkillContext) -> dict[str, Any]:
        return {
            "continued_from_pending_skill_context": context.context_id,
            "pending_skill_capability_id": context.capability_id,
            "pending_skill_missing_requirements": list(context.missing_requirements),
            "pending_skill_original_user_message": context.original_user_message,
            "pending_skill_assistant_message": context.assistant_message,
        }

    @staticmethod
    def _format_pending_skill_continuation_message(context: PendingSkillContext, user_message: str) -> str:
        missing = "、".join(context.missing_requirements) or "必需信息"
        return "\n\n".join(
            part
            for part in (
                context.original_user_message.strip(),
                f"此前缺少的信息：{missing}",
                f"用户补充：{user_message.strip()}",
            )
            if part
        )

    @staticmethod
    def _routing_mode(value: str | None) -> RoutingMode:
        if not value:
            return RoutingMode.AUTO
        try:
            return RoutingMode(value)
        except ValueError as exc:
            raise ValueError(f"Unsupported routing_mode: {value}") from exc

    @staticmethod
    def _metadata_list(value: Any) -> list[Any]:
        return list(value) if isinstance(value, list | tuple) else []

    async def save_upload(
        self,
        *,
        conversation_id: str,
        username: str,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> UploadedFileRecord:
        existing_conversation = await self.ensure_upload_allowed(conversation_id, username)
        now = self._utcnow_naive()
        record = self.upload_store.save(
            username=username,
            conversation_id=conversation_id,
            filename=filename,
            content_type=content_type,
            content=content,
        )
        if existing_conversation is None:
            await self.storage.save_conversation(
                Conversation(
                    conversation_id=conversation_id,
                    username=username,
                    created_at=now,
                    updated_at=now,
                )
            )
        return record

    async def ensure_upload_allowed(self, conversation_id: str, username: str) -> Conversation | None:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        return existing_conversation

    async def list_uploads(self, conversation_id: str, username: str) -> list[UploadedFileRecord]:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        return self.upload_store.list_for_conversation(username=username, conversation_id=conversation_id)

    async def delete_upload(self, conversation_id: str, username: str, upload_id: str) -> bool:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        return self.upload_store.delete(upload_id=upload_id, username=username, conversation_id=conversation_id)

    async def resolve_uploads_for_message(
        self,
        conversation_id: str,
        username: str,
        upload_ids,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if upload_ids is None:
            upload_ids = ()
        if isinstance(upload_ids, str):
            upload_ids = [upload_ids]
        if not isinstance(upload_ids, list | tuple):
            raise UploadValidationError("metadata.upload_ids must be a list")
        sheet_selections = self._normalize_upload_sheet_selections(upload_sheet_selections)
        uploaded_artifacts: list[dict[str, Any]] = []
        skill_artifacts: list[dict[str, Any]] = []
        pending_sheet_selections: list[dict[str, Any]] = []
        missing_upload_ids: list[str] = []
        for upload_id in upload_ids:
            upload_id_text = str(upload_id).strip()
            if not upload_id_text:
                continue
            try:
                record = self.upload_store.get_for_message(
                    upload_id=upload_id_text,
                    username=username,
                    conversation_id=conversation_id,
                )
            except UploadValidationError:
                missing_upload_ids.append(upload_id_text)
                continue
            selected_sheet = sheet_selections.get(upload_id_text)
            uploaded_artifacts.append(record.to_summary())
            if record.requires_sheet_selection and not selected_sheet:
                pending_sheet_selections.append(record.sheet_selection_payload())
                skill_artifacts.append(record.to_summary())
                continue
            skill_artifacts.append(record.to_skill_artifact(selected_sheet=selected_sheet))
        return {
            "uploaded_artifacts": uploaded_artifacts,
            "skill_artifacts": skill_artifacts,
            "missing_upload_ids": missing_upload_ids,
            "pending_sheet_selections": pending_sheet_selections,
        }

    @staticmethod
    def _raise_missing_uploads(missing_upload_ids: object, *, context: str) -> None:
        if not isinstance(missing_upload_ids, list | tuple) or not missing_upload_ids:
            return
        missing = [str(upload_id).strip() for upload_id in missing_upload_ids if str(upload_id).strip()]
        if not missing:
            return
        raise UploadValidationError(f"Missing or expired uploads required for {context}: {', '.join(missing)}")

    @staticmethod
    def _normalize_upload_sheet_selections(raw: Any) -> dict[str, str]:
        if raw in (None, ""):
            return {}
        if not isinstance(raw, Mapping):
            raise UploadValidationError("upload_sheet_selections must be an object")
        selections: dict[str, str] = {}
        for key, value in raw.items():
            upload_id = str(key).strip()
            sheet_name = str(value).strip()
            if upload_id and sheet_name:
                selections[upload_id] = sheet_name
        return selections

    async def _open_sheet_selection_interrupt(
        self,
        *,
        task: Task,
        metadata: Mapping[str, Any],
        pending_sheet_selections: list[dict[str, Any]],
    ) -> None:
        now = self._utcnow_naive()
        node_id = f"{task.task_id}:sheet_selection"
        node = TaskNode(
            node_id=node_id,
            task_id=task.task_id,
            capability_id=task.requested_capability_id or "main_agent.respond",
            status=NodeStatus.RUNNING,
            criticality=NodeCriticality.REQUIRED,
            started_at=now,
        )
        await self.storage.save_task_node(node)
        await self.storage.save_task(
            replace(
                task,
                status=TaskStatus.RUNNING,
                root_node_id=task.root_node_id or node_id,
                updated_at=now,
            )
        )
        required_upload_ids: list[str] = []
        options_by_upload_id: dict[str, list[str]] = {}
        labels_by_upload_id: dict[str, str] = {}
        details_by_upload_id: dict[str, Any] = {}
        for pending in pending_sheet_selections:
            required_upload_ids.extend(str(item) for item in pending.get("required_upload_ids", []) if str(item).strip())
            options_by_upload_id.update(
                {
                    str(upload_id): [str(option) for option in options]
                    for upload_id, options in dict(pending.get("options_by_upload_id", {})).items()
                    if isinstance(options, list | tuple)
                }
            )
            labels_by_upload_id.update({str(key): str(value) for key, value in dict(pending.get("labels_by_upload_id", {})).items()})
            details_by_upload_id.update(dict(pending.get("details_by_upload_id", {})))
        required_upload_ids = list(dict.fromkeys(required_upload_ids))
        required_fields = {
            "upload_sheet_selections": {
                "type": "sheet_selection",
                "description": "请选择每个 Excel 文件要用于执行的 sheet。",
                "required_upload_ids": required_upload_ids,
                "options_by_upload_id": options_by_upload_id,
                "labels_by_upload_id": labels_by_upload_id,
                "details_by_upload_id": details_by_upload_id,
            }
        }
        interrupt = Interrupt(
            interrupt_id=f"{node_id}:interrupt:sheet_selection_required",
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            node_id=node_id,
            source_agent=task.requested_capability_id or "main_agent.respond",
            source_message_id=task.root_message_id,
            question=self._sheet_selection_question(labels_by_upload_id, options_by_upload_id),
            reason_code="sheet_selection_required",
            required_fields=required_fields,
        )
        self._task_sheet_selection_resume_metadata[task.task_id] = dict(metadata)
        saved_interrupt = await self.interrupt_service.open_interrupt(interrupt, now=now)
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=node_id,
                event_type="node.waiting_for_input",
                payload={
                    "reason": saved_interrupt.reason_code,
                    "reason_code": saved_interrupt.reason_code,
                    "interrupt_id": saved_interrupt.interrupt_id,
                    "required_upload_ids": required_upload_ids,
                },
                created_at=now,
            )
        )

    @staticmethod
    def _sheet_selection_question(labels_by_upload_id: Mapping[str, str], options_by_upload_id: Mapping[str, list[str]]) -> str:
        parts = []
        for upload_id, label in labels_by_upload_id.items():
            options = "、".join(options_by_upload_id.get(upload_id, ()))
            parts.append(f"{label}（可选 sheet：{options}）")
        detail = "；".join(parts) if parts else "上传的 Excel 文件"
        return f"检测到多 sheet Excel：{detail}。请为每个文件选择一个 sheet 后继续。"

    async def _maybe_schedule_conversation_title_generation(
        self,
        conversation_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self._conversation_title_generator is None:
            return
        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None or (conversation.title or "").strip():
            return
        messages = await self.storage.list_messages_for_conversation(conversation_id)
        user_messages = [
            message.content
            for message in messages
            if message.role == MessageRole.USER and message.content.strip()
        ]
        if not user_messages:
            return
        title_source = build_conversation_title_source(user_messages)
        if not title_source:
            return
        expected_user_message_count = len(user_messages)
        task = asyncio.create_task(
            self._generate_and_store_conversation_title(
                conversation_id,
                title_source,
                expected_user_message_count=expected_user_message_count,
                metadata=dict(metadata or {}),
            )
        )
        self._running_title_tasks.add(task)
        task.add_done_callback(self._running_title_tasks.discard)

    async def _generate_and_store_conversation_title(
        self,
        conversation_id: str,
        title_source: str,
        *,
        expected_user_message_count: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            raw_title = await call_title_generator(
                self._conversation_title_generator,
                title_source,
                metadata=metadata,
            )
            title = normalize_generated_conversation_title(raw_title)
        except Exception:
            return
        if title is None:
            return
        async with self._lock:
            conversation = await self.storage.get_conversation(conversation_id)
            if conversation is None:
                return
            if (conversation.title or "").strip():
                return
            messages = await self.storage.list_messages_for_conversation(conversation_id)
            current_user_message_count = sum(
                1 for message in messages if message.role == MessageRole.USER and message.content.strip()
            )
            if current_user_message_count != expected_user_message_count:
                return
            await self.storage.save_conversation(
                replace(conversation, title=title, updated_at=self._utcnow_naive())
            )

    async def _schedule_execution(self, request: OrchestrationRequest) -> None:
        self._retain_task_skill_revision(request)
        self._retain_task_mcp_revision(request)
        async with self._lock:
            active_task_count = len(self._running_tasks)
            handle = asyncio.create_task(self._run_execution(request, active_task_count=active_task_count))
            self._running_tasks[request.task_id] = handle

    async def _run_execution(self, request: OrchestrationRequest, *, active_task_count: int) -> None:
        try:
            request = await self._attach_conversation_memory(request)
            plan_result = self.workflow_provider.build_plan(request)
            plan = await plan_result if inspect.isawaitable(plan_result) else plan_result
            await self._record_plan_built(request, plan)
            result = await self.orchestration_service.execute_request(request, plan, active_task_count=active_task_count)
            restored_cancelled_task = await self._restore_cancelled_task_if_requested(
                request.task_id,
                request.conversation_id,
            )
            if restored_cancelled_task is not None:
                return
            await self._handle_pending_skill_context_after_execution(request, result)
            if result.completion_status == str(TaskStatus.COMPLETED):
                try:
                    await self._persist_assistant_history_message(request.task_id, request.conversation_id)
                except Exception as exc:
                    await self._record_assistant_history_sync_failure(request.task_id, request.conversation_id, exc)
        except Exception as exc:
            restored_cancelled_task = await self._restore_cancelled_task_if_requested(
                request.task_id,
                request.conversation_id,
            )
            if restored_cancelled_task is not None:
                return
            await self._mark_task_failed(request, exc)
        finally:
            try:
                await self._clear_conversation_current_task(request.conversation_id, request.task_id)
                await self._release_task_skill_revision_if_terminal(request.task_id)
                await self._release_task_mcp_revision_if_terminal(request.task_id)
            finally:
                self._locally_cancelled_task_ids.discard(request.task_id)
                async with self._lock:
                    self._running_tasks.pop(request.task_id, None)

    async def _restore_cancelled_task_if_requested(self, task_id: str, conversation_id: str) -> Task | None:
        task = await self.storage.get_task(task_id)
        if task is None:
            return None
        if task.status == TaskStatus.CANCELLED:
            return task
        if (
            task.status != TaskStatus.CANCELLING
            and task.cancel_requested_at is None
            and task_id not in self._locally_cancelled_task_ids
        ):
            return None
        now = self._utcnow_naive()
        restored = replace(
            task,
            status=TaskStatus.CANCELLED,
            cancel_requested_at=task.cancel_requested_at or now,
            updated_at=now,
        )
        await self.storage.save_task(restored)
        await self._record_event(
            self._make_event(
                task_id=task_id,
                conversation_id=conversation_id,
                event_type="task.late_result_discarded",
                payload={"reason": "cancelled_task_status_restored"},
                visibility=EventVisibility.AUDIT_ONLY,
                created_at=now,
            )
        )
        return restored

    async def _attach_conversation_memory(self, request: OrchestrationRequest) -> OrchestrationRequest:
        if self._conversation_memory_builder is None:
            return request
        try:
            conversation = await self.storage.get_conversation(request.conversation_id)
            username = conversation.username if conversation is not None else None
            context = await self._conversation_memory_builder.build(request, username=username)
        except PermissionError:
            raise
        except Exception as exc:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    event_type="conversation.memory_fallback",
                    payload={"fallback_reason": "memory_builder_failed", "error_type": type(exc).__name__},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            return request

        memory_payload = context.to_prompt_payload()
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="conversation.memory_built",
                payload=context.to_audit_payload(),
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        return replace(
            request,
            user_message=context.effective_user_message,
            current_user_message=context.current_user_message,
            resolved_user_message=context.resolved_user_message,
            memory_context=memory_payload,
            metadata={**dict(request.metadata), "conversation_memory": memory_payload},
        )

    async def _mark_task_failed(self, request: OrchestrationRequest, exc: Exception) -> None:
        task = await self.storage.get_task(request.task_id)
        if task is None or task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED} or task.cancel_requested_at is not None:
            return
        failed = replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive())
        await self.storage.save_task(failed)
        payload: dict[str, Any] = {"code": "execution_crash", "message": str(exc)}
        if isinstance(exc, WorkflowPlanningError):
            payload = {
                "code": "planning_failed",
                "message": str(exc),
                "planner_reason": exc.reason,
                "planner_diagnostic": exc.diagnostic[:200],
                "planner_attempts": exc.attempts,
            }
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.failed",
                payload=payload,
            )
        )

    async def sync_assistant_history_messages(self, conversation_id: str) -> None:
        tasks = await self.storage.list_tasks_for_conversation(conversation_id, statuses={TaskStatus.COMPLETED})
        messages = await self.storage.list_messages_for_conversation(conversation_id)
        completed_assistant_task_ids = {
            message.task_id
            for message in messages
            if message.role == MessageRole.ASSISTANT
            and message.task_id is not None
            and message.stream_status == "complete"
        }
        for task in tasks:
            if task.task_id in completed_assistant_task_ids:
                continue
            await self.try_sync_assistant_history_message_for_task(task.task_id, task.conversation_id)

    async def sync_assistant_history_message_for_task(self, task_id: str, conversation_id: str) -> None:
        await self._persist_assistant_history_message(task_id, conversation_id)

    async def try_sync_assistant_history_message_for_task(self, task_id: str, conversation_id: str) -> None:
        try:
            await self._persist_assistant_history_message(task_id, conversation_id)
        except Exception as exc:
            await self._record_assistant_history_sync_failure(task_id, conversation_id, exc)

    async def _persist_assistant_history_message(self, task_id: str, conversation_id: str) -> None:
        message_id = f"{task_id}:assistant"
        if await self.storage.get_message(message_id) is not None:
            return
        artifacts = await self.storage.list_artifacts_for_task(task_id)
        events = await self._list_final_answer_events(task_id)
        text_artifact = select_final_text_artifact(artifacts, events=events)
        if text_artifact is None:
            return
        message = Message(
            message_id=message_id,
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=text_artifact.storage_ref,
            task_id=task_id,
            stream_status="complete",
            created_at=self._utcnow_naive(),
        )
        try:
            await self.storage.save_message(message)
        except Exception:
            if await self.storage.get_message(message_id) is not None:
                return
            raise

    async def _list_final_answer_events(self, task_id: str) -> Iterable[EventRecord]:
        filtered_reader = getattr(self.storage, "list_events_for_task_filtered", None)
        if not callable(filtered_reader):
            return ()
        return await filtered_reader(
            task_id,
            event_types={"main_agent.output_final"},
            visibility=EventVisibility.FRONTEND,
            limit=32,
        )

    async def _record_assistant_history_sync_failure(self, task_id: str, conversation_id: str, exc: Exception) -> None:
        async with self._assistant_history_sync_failure_lock:
            try:
                if task_id in self._assistant_history_sync_failure_task_ids:
                    return
                if await self._assistant_history_sync_failure_already_recorded(task_id):
                    self._assistant_history_sync_failure_task_ids.add(task_id)
                    return
                await self._record_event(
                    self._make_event(
                        task_id=task_id,
                        conversation_id=conversation_id,
                        event_type="assistant_history_sync.failed",
                        payload={
                            "code": "assistant_history_sync_failed",
                            "error_type": type(exc).__name__,
                        },
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                )
                self._assistant_history_sync_failure_task_ids.add(task_id)
            except Exception:
                return

    async def _assistant_history_sync_failure_already_recorded(self, task_id: str) -> bool:
        filtered_reader = getattr(self.storage, "list_events_for_task_filtered", None)
        if not callable(filtered_reader):
            return False
        try:
            events = await filtered_reader(
                task_id,
                event_types={"assistant_history_sync.failed"},
                visibility=EventVisibility.AUDIT_ONLY,
                limit=1,
            )
        except Exception:
            return False
        return bool(events)

    async def _handle_pending_skill_context_after_execution(
        self,
        request: OrchestrationRequest,
        result: OrchestrationRunResult,
    ) -> None:
        missing_input = await self._extract_pending_skill_missing_input(request)
        continued_context_id = self._metadata_text(request.metadata.get("continued_from_pending_skill_context"))
        if missing_input is not None:
            await self._create_pending_skill_context(request, missing_input)
            if result.completion_status != str(TaskStatus.COMPLETED):
                await self._mark_task_completed_for_pending_skill_context(request, reason="pending_skill_context_created")
            return
        if result.completion_status == str(TaskStatus.COMPLETED) and continued_context_id:
            consumed = await self.storage.mark_pending_skill_context_consumed(continued_context_id)
            if consumed is not None:
                await self._record_event(
                    self._make_event(
                        task_id=request.task_id,
                        conversation_id=request.conversation_id,
                        event_type="pending_skill_context.consumed",
                        payload={
                            "context_id": consumed.context_id,
                            "capability_id": consumed.capability_id,
                        },
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                )
            if request.metadata.get("defer_task_completed_until_pending_skill_context_processed") is True:
                await self._mark_task_completed_for_pending_skill_context(request, reason="pending_skill_context_consumed")
            return
        if (
            result.completion_status == str(TaskStatus.COMPLETED)
            and request.metadata.get("defer_task_completed_until_pending_skill_context_processed") is True
        ):
            await self._mark_task_completed_for_pending_skill_context(request, reason="pending_skill_context_processed")

    async def _extract_pending_skill_missing_input(
        self,
        request: OrchestrationRequest,
    ) -> _PendingSkillMissingInput | None:
        capability_id = request.requested_capability_id or ""
        if not capability_id.startswith("skill."):
            return None
        interrupts = await self.storage.list_interrupts_for_task(request.task_id)
        if interrupts:
            return None

        events = await self.storage.list_events_for_task(request.task_id)
        for event in reversed(events):
            if event.event_type != "skill.input_missing":
                continue
            missing = self._missing_requirements_from_payload(event.payload)
            if not missing:
                continue
            skill_name = self._metadata_text(event.payload.get("skill_name")) or capability_id.removeprefix("skill.")
            return _PendingSkillMissingInput(
                capability_id=capability_id,
                skill_name=skill_name,
                missing_requirements=missing,
                assistant_message=self._format_pending_skill_missing_message(missing),
                source_node_id=event.node_id,
            )
        return None

    @staticmethod
    def _missing_requirements_from_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
        raw_missing = payload.get("missing")
        if isinstance(raw_missing, str):
            values = [raw_missing]
        elif isinstance(raw_missing, Iterable):
            values = list(raw_missing)
        else:
            values = []
        seen: set[str] = set()
        missing: list[str] = []
        for value in values:
            item = str(value).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            missing.append(item)
        return tuple(missing)

    @staticmethod
    def _format_pending_skill_missing_message(missing_requirements: tuple[str, ...]) -> str:
        missing = "、".join(missing_requirements) or "必需信息"
        return f"缺少 Skill 必需信息：{missing}。请补充后继续。"

    async def _create_pending_skill_context(
        self,
        request: OrchestrationRequest,
        missing_input: _PendingSkillMissingInput,
    ) -> PendingSkillContext:
        now = self._utcnow_naive()
        conversation = await self.storage.get_conversation(request.conversation_id)
        root_message = await self.storage.get_message(request.root_message_id)
        original_user_message = (
            self._metadata_text(request.metadata.get("pending_skill_original_user_message"))
            or (root_message.content if root_message is not None else "")
            or request.current_user_message
            or request.user_message
        )
        context = PendingSkillContext(
            context_id=self._make_id("pending-skill-context"),
            conversation_id=request.conversation_id,
            username=conversation.username if conversation is not None else None,
            capability_id=missing_input.capability_id,
            skill_name=missing_input.skill_name,
            source_task_id=request.task_id,
            source_message_id=request.root_message_id,
            original_user_message=original_user_message,
            missing_requirements=missing_input.missing_requirements,
            assistant_message=missing_input.assistant_message,
            status="pending_user_input",
            created_at=now,
            updated_at=now,
        )
        saved = await self.storage.save_pending_skill_context(context)
        await self._save_pending_skill_assistant_message(request, missing_input.assistant_message, created_at=now)
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                node_id=missing_input.source_node_id,
                event_type="pending_skill_context.created",
                payload={
                    "context_id": saved.context_id,
                    "capability_id": saved.capability_id,
                    "skill_name": saved.skill_name,
                    "missing_requirements": list(saved.missing_requirements),
                },
                visibility=EventVisibility.AUDIT_ONLY,
                created_at=now,
            )
        )
        return saved

    async def _save_pending_skill_assistant_message(
        self,
        request: OrchestrationRequest,
        content: str,
        *,
        created_at: datetime,
    ) -> None:
        message_id = f"{request.task_id}:assistant"
        if await self.storage.get_message(message_id) is not None:
            return
        await self.storage.save_message(
            Message(
                message_id=message_id,
                conversation_id=request.conversation_id,
                role=MessageRole.ASSISTANT,
                content=content,
                task_id=request.task_id,
                stream_status="complete",
                created_at=created_at,
            )
        )

    async def _mark_task_completed_for_pending_skill_context(self, request: OrchestrationRequest, *, reason: str) -> None:
        task = await self.storage.get_task(request.task_id)
        if task is None or task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            return
        completed = replace(task, status=TaskStatus.COMPLETED, updated_at=self._utcnow_naive())
        await self.storage.save_task(completed)
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.completed",
                payload={"completion_reason": reason},
            )
        )

    @staticmethod
    def _metadata_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    async def _record_plan_built(self, request: OrchestrationRequest, plan: WorkflowPlan) -> None:
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="workflow.plan_built",
                payload={
                    "node_count": len(plan.nodes),
                    "metadata": self._plan_audit_metadata(request, plan),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

    def _plan_audit_metadata(self, request: OrchestrationRequest, plan: WorkflowPlan) -> dict[str, Any]:
        metadata = dict(plan.metadata)
        revision = request.metadata.get("skill_bundle_revision")
        if revision:
            metadata["skill_bundle_revision"] = revision
        for key in PENDING_SKILL_METADATA_KEYS:
            if key in request.metadata:
                metadata[key] = request.metadata[key]
        return self._json_safe_mapping(metadata)

    @staticmethod
    def _json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))

    async def _clear_conversation_current_task(self, conversation_id: str, task_id: str) -> None:
        conversation = await self.storage.get_conversation(conversation_id)
        task = await self.storage.get_task(task_id)
        if conversation is None or task is None:
            return
        if conversation.current_task_id != task_id:
            return
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return
        if conversation.status != ConversationStatus.ACTIVE:
            return
        try:
            await self.storage.save_conversation(
                replace(conversation, current_task_id=None, updated_at=self._utcnow_naive())
            )
        except ValueError as exc:
            if "Conversation is not available" in str(exc):
                return
            raise

    async def cancel_task(self, task_id: str) -> Task:
        existing_task = await self.storage.get_task(task_id)
        if existing_task is not None and existing_task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            self._locally_cancelled_task_ids.add(task_id)
        if existing_task is not None and self._mcp_runtime_state is not None:
            for envelope in await self._mcp_runtime_state.cancel_platform_task(task_id):
                await self._record_event(
                    self._make_event(
                        task_id=existing_task.task_id,
                        conversation_id=existing_task.conversation_id,
                        event_type=str(envelope.get("event_type") or "mcp.long_task_cancel_requested"),
                        payload=dict(envelope.get("payload") or {}),
                    )
                )
        task = await self.cancellation_service.cancel_task_context(task_id)
        await self._cancel_active_slot_collections_for_task(task)
        if task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            await self._cancel_existing_execution(task_id)
        if task.status not in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            self._locally_cancelled_task_ids.discard(task_id)
        await self._clear_conversation_current_task(task.conversation_id, task.task_id)
        return task

    async def _cancel_active_slot_collections_for_task(self, task: Task) -> None:
        now = self._utcnow_naive()
        for collection in await self.storage.list_slot_collections_for_task(task.task_id):
            if collection.status in _SLOT_TERMINAL_STATUSES:
                continue
            cancel_key = f"slot:{collection.collection_id}:cancelled"
            if await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, cancel_key) is not None:
                continue
            try:
                next_collection, event = transition_slot_collection(
                    collection,
                    to_status="cancelled",
                    event_type="slot.collection_cancelled",
                    payload={"reason": "task_cancelled"},
                    idempotency_key=cancel_key,
                    now=now,
                )
            except Exception:
                next_collection = replace(collection, status="cancelled", cancelled_at=now, updated_at=now)
                event = SlotEvent(
                    slot_event_id=f"{collection.collection_id}:event:cancelled:{int(now.timestamp() * 1_000_000)}",
                    collection_id=collection.collection_id,
                    task_id=collection.task_id,
                    node_id=collection.node_id,
                    conversation_id=collection.conversation_id,
                    event_type="slot.collection_cancelled",
                    round=collection.round,
                    revision=collection.revision + 1,
                    idempotency_key=cancel_key,
                    payload={"reason": "task_cancelled", "forced": True},
                    created_at=now,
                )
            saved = await self.storage.apply_slot_transition(
                collection.collection_id,
                collection.revision,
                next_collection,
                event,
                idempotency_key=cancel_key,
            )
            cancelled = saved or await self.storage.get_slot_collection(collection.collection_id)
            if cancelled is None:
                continue
            await self._record_event(
                self._make_event(
                    task_id=cancelled.task_id,
                    conversation_id=cancelled.conversation_id,
                    node_id=cancelled.node_id,
                    event_type="slot.collection_cancelled",
                    payload={
                        "slot_collection_id": cancelled.collection_id,
                        "status": cancelled.status,
                        "reason": "task_cancelled",
                    },
                )
            )

    async def _recover_missing_v2_slot_interrupts(self, task_id: str) -> None:
        task = await self.storage.get_task(task_id)
        if task is None:
            return
        task_interrupts = await self.storage.list_interrupts_for_task(task_id)
        open_slot_collection_ids = {
            str(ref.get("collection_id") or "").strip()
            for interrupt in task_interrupts
            if interrupt.status == InterruptStatus.OPEN
            for ref in (slot_collection_ref_from_required_fields(interrupt.required_fields),)
            if isinstance(ref, Mapping) and str(ref.get("collection_id") or "").strip()
        }
        has_running_execution = await self._has_running_execution(task_id)
        for collection in await self.storage.list_slot_collections_for_task(task_id):
            if collection.status == "ready":
                script_key = self._v2_slot_script_scheduled_key(collection)
                if await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, script_key) is None:
                    scheduled_collection, scheduled_new = await self._mark_v2_slot_script_scheduled(collection)
                    if scheduled_new and scheduled_collection.status == "script_scheduled":
                        latest_interrupt = await self.storage.get_interrupt_for_node(task_id, collection.node_id)
                        if latest_interrupt is not None and latest_interrupt.status == InterruptStatus.OPEN:
                            await self.storage.save_interrupt(
                                replace(
                                    latest_interrupt,
                                    status=InterruptStatus.ANSWERED,
                                    answered_at=self._utcnow_naive(),
                                )
                            )
                        recovery_interrupt = Interrupt(
                            interrupt_id=f"{collection.collection_id}:interrupt:ready_recovery",
                            conversation_id=collection.conversation_id,
                            task_id=collection.task_id,
                            node_id=collection.node_id,
                            source_agent=collection.capability_id,
                            source_message_id="",
                            question=collection.last_question or "",
                            reason_code="ready_v2_slot_recovered",
                            required_fields=slot_collection_required_fields_ref(scheduled_collection),
                            status=InterruptStatus.ANSWERED,
                            created_at=self._utcnow_naive(),
                            answered_at=self._utcnow_naive(),
                        )
                        await self._schedule_v2_slot_resume(
                            task=task,
                            interrupt=recovery_interrupt,
                            collection=scheduled_collection,
                            raw_answer={},
                        )
                continue
            if collection.status not in _SLOT_WAITING_STATUSES:
                continue
            node = await self.storage.get_task_node(collection.node_id)
            if node is None or node.status != NodeStatus.WAITING_FOR_INPUT:
                continue
            if collection.collection_id in open_slot_collection_ids:
                continue
            latest_interrupt = await self.storage.get_interrupt_for_node(task_id, collection.node_id)
            if latest_interrupt is not None and latest_interrupt.status == InterruptStatus.OPEN:
                continue
            if latest_interrupt is None and has_running_execution:
                continue
            now = self._utcnow_naive()
            interrupt_id = f"{collection.collection_id}:interrupt:{collection.round}:{collection.revision}"
            interrupt = Interrupt(
                interrupt_id=interrupt_id,
                conversation_id=collection.conversation_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                source_agent=collection.capability_id,
                source_message_id="",
                question=collection.last_question or "请补充当前 Skill 所需参数。",
                reason_code="missing_v2_slot_input_recovered",
                required_fields=slot_collection_required_fields_ref(collection),
                status=InterruptStatus.OPEN,
                created_at=now,
            )
            await self.storage.save_interrupt(interrupt)
            recovery_key = f"slot:{collection.collection_id}:interrupt_recovered:{collection.round}:{collection.revision}"
            await self.storage.append_slot_event(
                SlotEvent(
                    slot_event_id=f"{collection.collection_id}:event:recovered:{collection.round}:{collection.revision}",
                    collection_id=collection.collection_id,
                    task_id=collection.task_id,
                    node_id=collection.node_id,
                    conversation_id=collection.conversation_id,
                    event_type="slot.interrupt_recovered",
                    round=collection.round,
                    revision=collection.revision,
                    idempotency_key=recovery_key,
                    payload={"interrupt_id": interrupt_id},
                    created_at=now,
                )
            )
            await self._record_event(
                self._make_event(
                    task_id=collection.task_id,
                    conversation_id=collection.conversation_id,
                    node_id=collection.node_id,
                    event_type="slot.interrupt_recovered",
                    payload={"slot_collection_id": collection.collection_id, "interrupt_id": interrupt_id},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )

    async def _has_running_execution(self, task_id: str) -> bool:
        async with self._lock:
            handle = self._running_tasks.get(task_id)
        return handle is not None and not handle.done()

    def _track_conversation_delete_task(
        self,
        conversation_id: str,
        task: asyncio.Task[dict[str, object]],
    ) -> None:
        self._conversation_delete_tasks[conversation_id] = task
        task.add_done_callback(lambda handle, cid=conversation_id: self._finalize_conversation_delete_task(cid, handle))

    def _finalize_conversation_delete_task(
        self,
        conversation_id: str,
        task: asyncio.Task[dict[str, object]],
    ) -> None:
        self._conversation_delete_tasks.pop(conversation_id, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is None:
            return
        if self._audit_sink is not None:
            self._audit_sink.record_sync(
                "conversation.delete_task_failed",
                {"error_type": exception.__class__.__name__},
                conversation_id=conversation_id,
            )

    async def delete_conversation(self, conversation_id: str, *, username: str | None = None) -> dict[str, object]:
        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        if username is not None and conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if conversation.status == ConversationStatus.DELETING_FAILED:
            raise ValueError(f"Unknown conversation: {conversation_id}")

        existing_task = self._conversation_delete_tasks.get(conversation_id)
        if existing_task is not None and not existing_task.done():
            return await asyncio.shield(existing_task)
        if conversation.status == ConversationStatus.DELETING:
            return await self._wait_for_external_conversation_delete(conversation_id, runner_id=conversation.delete_runner_id)
        if conversation.status != ConversationStatus.ACTIVE:
            raise ValueError(f"Unknown conversation: {conversation_id}")

        runner_id = self._make_id("delete")
        now = self._utcnow_naive()
        marked = await self.storage.mark_conversation_deleting(
            conversation_id,
            runner_id=runner_id,
            requested_at=conversation.delete_requested_at or now,
            started_at=conversation.delete_started_at or now,
            phase="marking",
        )
        if marked is None:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        if marked.status == ConversationStatus.DELETING and marked.delete_runner_id != runner_id:
            return await self._wait_for_external_conversation_delete(conversation_id, runner_id=marked.delete_runner_id)
        task = asyncio.create_task(self._run_conversation_delete(marked, runner_id), name=f"delete-conversation:{conversation_id}")
        self._track_conversation_delete_task(conversation_id, task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._conversation_delete_tasks.pop(conversation_id, None)

    async def retry_failed_conversation_delete(self, conversation_id: str) -> dict[str, object]:
        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None or conversation.status != ConversationStatus.DELETING_FAILED:
            raise ValueError(f"Unknown failed conversation deletion: {conversation_id}")
        existing_task = self._conversation_delete_tasks.get(conversation_id)
        if existing_task is not None and not existing_task.done():
            return await asyncio.shield(existing_task)
        runner_id = self._make_id("delete")
        now = self._utcnow_naive()
        marked = await self.storage.retry_failed_conversation_delete(
            conversation_id,
            runner_id=runner_id,
            requested_at=now,
            started_at=now,
            phase="marking",
        )
        if marked is None:
            raise ValueError(f"Unknown failed conversation deletion: {conversation_id}")
        task = asyncio.create_task(self._run_conversation_delete(marked, runner_id), name=f"delete-conversation-retry:{conversation_id}")
        self._track_conversation_delete_task(conversation_id, task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._conversation_delete_tasks.pop(conversation_id, None)

    async def _wait_for_external_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str | None,
    ) -> dict[str, object]:
        started_at = self._utcnow_naive()
        while True:
            current = await self.storage.get_conversation(conversation_id)
            if current is None:
                finished_at = self._utcnow_naive()
                return {
                    "conversation_id": conversation_id,
                    "deleted": True,
                    "cancelled_task_ids": [],
                    "deleted_counts": {"conversation": 1},
                    "delete_status": "completed",
                    "runner_id": runner_id,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "error_code": None,
                }
            if current.status == ConversationStatus.DELETING_FAILED:
                raise RuntimeError(f"Conversation deletion failed: {conversation_id}")
            if current.status != ConversationStatus.DELETING:
                raise ValueError(f"Unknown conversation: {conversation_id}")
            await asyncio.sleep(0.25)

    async def _run_conversation_delete(self, conversation: Conversation, runner_id: str) -> dict[str, object]:
        conversation_id = conversation.conversation_id
        cancelled_task_ids: list[str] = []
        started_at = conversation.delete_started_at or self._utcnow_naive()
        try:
            await self.storage.update_conversation_delete_phase(
                conversation_id,
                phase="cancelling_tasks",
                updated_at=self._utcnow_naive(),
                runner_id=runner_id,
            )
            unfinished_tasks = await self.storage.list_tasks_for_conversation(
                conversation_id,
                statuses=UNFINISHED_TASK_STATUSES,
            )
            for task in unfinished_tasks:
                await self.cancel_task(task.task_id)
                cancelled_task_ids.append(task.task_id)
            for task_id in cancelled_task_ids:
                await self._cancel_existing_execution(task_id)

            await self.storage.update_conversation_delete_phase(
                conversation_id,
                phase="deleting_files",
                updated_at=self._utcnow_naive(),
                runner_id=runner_id,
            )
            await self._delete_conversation_file_artifacts(conversation_id)

            await self.storage.update_conversation_delete_phase(
                conversation_id,
                phase="deleting_db",
                updated_at=self._utcnow_naive(),
                runner_id=runner_id,
            )
            deleted_counts = await self.storage.delete_conversation_physical(conversation_id)
            finished_at = self._utcnow_naive()
            return {
                "conversation_id": conversation_id,
                "deleted": deleted_counts.get("conversation", 0) > 0,
                "cancelled_task_ids": cancelled_task_ids,
                "deleted_counts": deleted_counts,
                "delete_status": "completed",
                "runner_id": runner_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "error_code": None,
            }
        except Exception as exc:
            failed_at = self._utcnow_naive()
            phase = "failed"
            try:
                current = await self.storage.get_conversation(conversation_id)
                phase = current.delete_phase or phase if current is not None else phase
                await self.storage.mark_conversation_delete_failed(
                    conversation_id,
                    failed_at=failed_at,
                    phase=phase,
                    error_code=exc.__class__.__name__,
                    error_summary=_sanitize_delete_error(exc),
                    runner_id=runner_id,
                )
            except Exception as record_exc:
                if self._audit_sink is not None:
                    await self._audit_sink.record(
                        "conversation.delete_failure_record_failed",
                        {
                            "error_type": record_exc.__class__.__name__,
                            "original_error_type": exc.__class__.__name__,
                            "runner_id": runner_id,
                            "phase": phase,
                        },
                        conversation_id=conversation_id,
                    )
            raise

    async def _delete_conversation_file_artifacts(self, conversation_id: str) -> None:
        for artifact in await self.storage.list_artifacts_for_conversation(conversation_id):
            metadata = parse_file_storage_ref(artifact.storage_ref)
            if is_active_skill_output_file(metadata):
                self.artifact_file_store.delete(str(metadata.get("storage_key")))

    async def rename_conversation(self, conversation_id: str, title: str, *, username: str | None = None) -> Conversation:
        normalized_title = validate_conversation_title(title)
        async with self._lock:
            conversation = await self.storage.get_conversation(conversation_id)
            if conversation is None:
                raise ValueError(f"Unknown conversation: {conversation_id}")
            if username is not None and conversation.username != username:
                raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
            if conversation.status != ConversationStatus.ACTIVE:
                raise ValueError(f"Unknown conversation: {conversation_id}")
            updated = replace(conversation, title=normalized_title, updated_at=self._utcnow_naive())
            return await self.storage.save_conversation(updated)

    async def list_interrupts(self, task_id: str) -> list[dict[str, object]]:
        await self._recover_missing_v2_slot_interrupts(task_id)
        interrupts = await self.storage.list_interrupts_for_task(task_id)
        return [
            {
                "interrupt_id": interrupt.interrupt_id,
                "conversation_id": interrupt.conversation_id,
                "task_id": interrupt.task_id,
                "node_id": interrupt.node_id,
                "question": interrupt.question,
                "reason_code": interrupt.reason_code,
                "required_fields": dict(interrupt.required_fields),
                "status": str(interrupt.status),
            }
            for interrupt in interrupts
        ]

    async def answer_interrupt(
        self,
        task_id: str,
        interrupt_id: str,
        answer_payload: dict[str, object],
        *,
        source_message_id: str | None = None,
    ) -> dict[str, object]:
        task = await self.storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        interrupt = await self.storage.get_interrupt(interrupt_id)
        if interrupt is None or interrupt.task_id != task_id:
            raise ValueError(f"Unknown interrupt: {interrupt_id}")
        if interrupt.reason_code == "sheet_selection_required":
            self._validate_sheet_selection_answer(interrupt, answer_payload)
        if slot_collection_ref_from_required_fields(interrupt.required_fields) is not None:
            return await self._answer_v2_slot_interrupt(task=task, interrupt=interrupt, answer_payload=answer_payload, source_message_id=source_message_id)
        previous_slot_collection = slot_collection_from_required_fields(interrupt.required_fields)
        if _is_v2_slot_collection_payload(previous_slot_collection):
            collection_id = str(previous_slot_collection.get("collection_id") or "").strip() if previous_slot_collection else ""
            collection = await self.storage.get_slot_collection(collection_id) if collection_id else None
            if collection is None:
                raise UploadValidationError("slot collection state is missing; restart the Skill")
            recovered_interrupt = replace(interrupt, required_fields=slot_collection_required_fields_ref(collection))
            await self.storage.save_interrupt(recovered_interrupt)
            return await self._answer_v2_slot_interrupt(task=task, interrupt=recovered_interrupt, answer_payload=answer_payload, source_message_id=source_message_id)

        existing_answer_payloads = await self._task_interrupt_answer_payloads(task.task_id)
        answer_payloads = (*existing_answer_payloads, dict(answer_payload))
        root_message = await self.storage.get_message(task.root_message_id)
        merged_answer_payload = self._merge_answer_payloads(answer_payloads)
        combined_message = self._combine_resume_message(
            root_message.content if root_message is not None else task.summary or "",
            merged_answer_payload,
        )
        sheet_selection_resume_metadata = self._task_sheet_selection_resume_metadata.get(task.task_id, {})
        resume_metadata = {
            **sheet_selection_resume_metadata,
            **self._resume_skill_revision_metadata(task.task_id),
        }
        if previous_slot_collection is not None:
            resume_metadata[SLOT_COLLECTION_METADATA_KEY] = dict(previous_slot_collection)
        for payload in answer_payloads:
            resume_metadata.update(self._answer_payload_metadata(payload))
        answer = InterruptAnswer(
            interrupt_answer_id=self._make_id("interrupt-answer"),
            interrupt_id=interrupt_id,
            answer_payload=dict(answer_payload),
            source_message_id=source_message_id or self._make_id("msg"),
            created_at=self._utcnow_naive(),
        )
        upload_ids = self._merged_answer_upload_ids(answer_payloads)
        if upload_ids:
            conversation = await self.storage.get_conversation(task.conversation_id)
            if conversation is None:
                raise ValueError(f"Unknown conversation: {task.conversation_id}")
            await self._bind_or_update_resume_input_attachments(
                task=task,
                username=conversation.username,
                upload_ids=upload_ids,
                source_kind="interrupt_answer_upload",
                source_message_id=answer.source_message_id,
                interrupt_answer_id=answer.interrupt_answer_id,
                upload_sheet_selections=resume_metadata.get("upload_sheet_selections"),
            )
        resume_metadata.update(await self._task_input_attachment_metadata(task.task_id))
        saved_interrupt = await self.interrupt_service.record_answer(answer)

        answer_message = Message(
            message_id=answer.source_message_id or self._make_id("msg"),
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content=self._format_answer_message(answer_payload),
            task_id=task.task_id,
            created_at=self._utcnow_naive(),
        )
        await self.storage.save_message(answer_message)
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_answered",
                payload={"interrupt_id": interrupt_id, "answer_payload": dict(answer_payload)},
            )
        )

        await self._await_existing_execution(task.task_id)
        resume_capability_id = task.requested_capability_id
        interrupted_node = await self.storage.get_task_node(interrupt.node_id)
        if interrupted_node is not None and interrupted_node.capability_id.startswith("skill."):
            resume_capability_id = interrupted_node.capability_id
            resume_metadata["resume_interrupted_node_id"] = interrupted_node.node_id
            resume_finalizer_node_id = await self._resume_finalizer_node_id(task.task_id, interrupted_node.node_id)
            if resume_finalizer_node_id:
                resume_metadata["resume_finalizer_node_id"] = resume_finalizer_node_id
        elif interrupt.source_agent.startswith("skill.") and self.capability_registry.get(interrupt.source_agent) is not None:
            resume_capability_id = interrupt.source_agent
        await self._schedule_execution(
            OrchestrationRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=combined_message,
                requested_capability_id=resume_capability_id,
                metadata=resume_metadata,
            )
        )
        if sheet_selection_resume_metadata:
            self._task_sheet_selection_resume_metadata.pop(task.task_id, None)
        return {
            "interrupt_id": saved_interrupt.interrupt_id,
            "status": str(saved_interrupt.status),
            "node_id": saved_interrupt.node_id,
            "answer_payload": dict(answer_payload),
            "source_message_id": answer.source_message_id,
        }

    async def _answer_v2_slot_interrupt(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        answer_payload: dict[str, object],
        source_message_id: str | None = None,
    ) -> dict[str, object]:
        slot_ref = slot_collection_ref_from_required_fields(interrupt.required_fields)
        if slot_ref is None:
            raise UploadValidationError("v2 slot interrupt is missing slot collection reference")
        client_request_id = str(answer_payload.get("client_request_id") or "").strip()
        raw_answer = answer_payload.get("answer")
        if not client_request_id:
            raise UploadValidationError("v2 slot answers require client_request_id")
        if not isinstance(raw_answer, Mapping):
            raise UploadValidationError("v2 slot answers require answer object")
        legacy_field_keys = [
            key for key in answer_payload
            if key not in {"client_request_id", "answer", "task_id", "interrupt_id"} and not str(key).startswith("_")
        ]
        if legacy_field_keys:
            raise UploadValidationError("v2 slot answers must not submit field-shaped payloads")
        self._validate_v2_interrupt_raw_answer(raw_answer)
        collection_id = str(slot_ref.get("collection_id") or "").strip()
        collection = await self.storage.get_slot_collection(collection_id)
        if collection is None:
            raise UploadValidationError("slot collection state is missing; restart the Skill")

        return await self._process_v2_interrupt_open_turn(
            task=task,
            interrupt=interrupt,
            collection=collection,
            raw_answer=dict(raw_answer),
            client_request_id=client_request_id,
            source_message_id=source_message_id,
        )

    @staticmethod
    def _validate_v2_interrupt_raw_answer(raw_answer: Mapping[str, object]) -> None:
        unsupported = sorted(str(key) for key in raw_answer if str(key) not in _V2_INTERRUPT_RAW_ANSWER_ALLOWED_KEYS)
        if unsupported:
            raise UploadValidationError(
                "v2 slot answer only supports answer.text, answer.upload_ids, "
                "answer.sheet_selections and answer.upload_sheet_selections; "
                f"unsupported fields: {', '.join(unsupported)}"
            )
        ApiRuntime._v2_answer_upload_ids(raw_answer)
        ApiRuntime._v2_answer_sheet_selections(raw_answer)

    async def _process_v2_interrupt_open_turn(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        raw_answer: dict[str, object],
        client_request_id: str,
        source_message_id: str | None = None,
    ) -> dict[str, object]:
        turn_key = f"interrupt_turn:{interrupt.interrupt_id}:{client_request_id}"
        existing_summary = await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, turn_key)
        if existing_summary is not None and existing_summary.event_type == "slot.interrupt_turn_processed":
            payload = dict(existing_summary.payload)
            return self._interrupt_open_turn_response_from_summary(
                interrupt=interrupt,
                summary=payload,
                fallback_source_message_id=source_message_id,
            )

        # Compatibility with pre-query-split idempotency keys. New turns are always
        # persisted with the turn-level key above; legacy keys are read only.
        legacy_answer_key = f"answer:{interrupt.interrupt_id}:{client_request_id}"
        existing_legacy_event = await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, legacy_answer_key)
        if existing_legacy_event is not None:
            if existing_legacy_event.event_type == "slot.clarification_answered":
                return {
                    "interrupt_id": interrupt.interrupt_id,
                    "status": str(interrupt.status),
                    "node_id": interrupt.node_id,
                    "answer_payload": {"client_request_id": client_request_id},
                    "action": "clarification_answer",
                    "assistant_message": str(existing_legacy_event.payload.get("assistant_message") or ""),
                    "source_message_id": str(existing_legacy_event.payload.get("source_message_id") or source_message_id or ""),
                }
            current_collection = await self.storage.get_slot_collection(collection.collection_id) or collection
            return {
                "interrupt_id": interrupt.interrupt_id,
                "status": str(interrupt.status),
                "node_id": interrupt.node_id,
                "answer_payload": {"client_request_id": client_request_id},
                "action": "resumed" if current_collection.status in {"script_scheduled", "completed"} else None,
                "source_message_id": str(existing_legacy_event.payload.get("source_message_id") or source_message_id or ""),
            }

        if interrupt.status != InterruptStatus.OPEN:
            raise UploadValidationError(
                "v2 slot interrupt is not open; retry with the original client_request_id to replay an accepted turn"
            )

        plan = await self._plan_v2_interrupt_open_turn(
            task=task,
            interrupt=interrupt,
            collection=collection,
            raw_answer=raw_answer,
            client_request_id=client_request_id,
        )
        await self._record_v2_interrupt_turn_planned(
            task=task,
            interrupt=interrupt,
            collection=collection,
            client_request_id=client_request_id,
            plan=plan,
        )

        user_message = await self._save_v2_interrupt_user_message(
            task=task,
            raw_answer=raw_answer,
            source_message_id=source_message_id,
        )
        processed_parts: list[dict[str, object]] = []
        assistant_sections: list[str] = []
        schema_switch_metadata: dict[str, object] | None = None
        requires_confirmation = False
        has_schema_switch = any(part.kind == "schema_switch" for part in plan.parts)
        blocking_parts = [part for part in plan.parts if part.blocks_resume]
        verifier_block: InterruptResumeVerification | None = None
        if self._requires_v2_interrupt_resume_verification(plan, raw_answer=raw_answer):
            verifier_decision = InterruptTurnDecision(
                intent="slot_answer",
                confidence=max((part.confidence for part in plan.parts if part.kind == "slot_answer"), default=0.0),
                reason=plan.reason,
            )
            verifier_block_candidate = await self._verify_v2_interrupt_resume(
                task=task,
                interrupt=interrupt,
                collection=collection,
                raw_answer=raw_answer,
                decision=verifier_decision,
            )
            if not verifier_block_candidate.allow_resume:
                verifier_block = verifier_block_candidate
                blocking_parts.append(
                    InterruptOpenTurnPart(
                        part_id="verifier-block",
                        kind="ambiguous",
                        text=self._v2_answer_text(raw_answer),
                        confidence=1.0 - verifier_block_candidate.confidence,
                        reason=verifier_block_candidate.reason,
                        blocks_resume=True,
                        block_reason=verifier_block_candidate.clarification_answer,
                        assistant_message=verifier_block_candidate.clarification_answer,
                    )
                )

        current_collection = collection
        schema_switch_parts = [part for part in plan.parts if part.kind == "schema_switch"]
        if schema_switch_parts:
            current_collection, schema_switch_metadata, schema_message, schema_requires_confirmation = await self._process_v2_schema_switch_part(
                task=task,
                interrupt=interrupt,
                collection=current_collection,
                part=schema_switch_parts[0],
                raw_answer=raw_answer,
                client_request_id=client_request_id,
            )
            processed_parts.append(self._interrupt_part_summary(schema_switch_parts[0], result={"status": current_collection.status}))
            if schema_message:
                assistant_sections.append(schema_message)
            requires_confirmation = requires_confirmation or schema_requires_confirmation

        may_apply_slot_answers = verifier_block is None and not (
            schema_switch_metadata is not None
            and str(schema_switch_metadata.get("reuse_decision") or "unspecified") == "unspecified"
        )
        if may_apply_slot_answers:
            for part in [part for part in plan.parts if part.kind == "slot_answer"]:
                part_answer = self._raw_answer_for_interrupt_part(raw_answer, part)
                current_collection = await self._process_v2_slot_answer_part(
                    task=task,
                    interrupt=interrupt,
                    collection=current_collection,
                    raw_answer=part_answer,
                    client_request_id=client_request_id,
                    source_message_id=user_message.message_id,
                )
                processed_parts.append(
                    self._interrupt_part_summary(
                        part,
                        result={
                            "slot_status": current_collection.status,
                            "resolved": sorted(current_collection.resolved),
                            "missing": list(current_collection.missing),
                        },
                    )
                )

        latest_collection = await self.storage.get_slot_collection(current_collection.collection_id) or current_collection
        for part in [part for part in plan.parts if part.kind == "skill_question"]:
            answer = part.assistant_message.strip()
            if answer:
                await self._record_v2_interrupt_question_answered(
                    task=task,
                    interrupt=interrupt,
                    collection=latest_collection,
                    part=part,
                    answer=answer,
                )
            else:
                answer = await self._process_v2_skill_question_part(
                    task=task,
                    interrupt=interrupt,
                    collection=latest_collection,
                    part=part,
                )
            if answer:
                assistant_sections.append(answer)
                await self._append_v2_question_answer_slot_event(
                    collection=latest_collection,
                    interrupt=interrupt,
                    client_request_id=client_request_id,
                    part=part,
                    assistant_message=answer,
                    source_message_id=user_message.message_id,
                )
            processed_parts.append(self._interrupt_part_summary(part, result={"answered": bool(answer)}))

        for part in [part for part in plan.parts if part.kind == "off_topic_guidance"]:
            answer = self._process_v2_off_topic_guidance_part(collection=latest_collection, part=part)
            assistant_sections.append(answer)
            processed_parts.append(self._interrupt_part_summary(part, result={"guided": True}))

        ambiguous_parts = [part for part in plan.parts if part.kind == "ambiguous"]
        if verifier_block is not None:
            ambiguous_parts.append(
                InterruptOpenTurnPart(
                    part_id="verifier-block",
                    kind="ambiguous",
                    text=self._v2_answer_text(raw_answer),
                    confidence=1.0 - verifier_block.confidence,
                    reason=verifier_block.reason,
                    blocks_resume=True,
                    block_reason=verifier_block.clarification_answer,
                    assistant_message=verifier_block.clarification_answer,
                )
            )
        for part in ambiguous_parts:
            message = part.assistant_message.strip() or part.block_reason.strip()
            if not message:
                message = await self._generate_v2_interrupt_clarification_answer(
                    task=task,
                    interrupt=interrupt,
                    collection=latest_collection,
                    user_text=part.text or self._v2_answer_text(raw_answer),
                    decision=InterruptTurnDecision(intent="ambiguous", confidence=part.confidence, reason=part.reason),
                )
            if not message:
                message = self._interrupt_ambiguity_message(latest_collection)
            if message:
                assistant_sections.append(message)
            processed_parts.append(self._interrupt_part_summary(part, result={"blocked": part.blocks_resume}))

        latest_collection = await self.storage.get_slot_collection(latest_collection.collection_id) or latest_collection
        if (
            verifier_block is None
            and any(part.kind == "slot_answer" for part in plan.parts)
            and latest_collection.status != "ready"
            and latest_collection.status not in {"script_scheduled", "completed"}
        ):
            await self._save_v2_interrupt_answer_without_closing(
                interrupt=interrupt,
                raw_answer=raw_answer,
                client_request_id=client_request_id,
                source_message_id=user_message.message_id,
            )
        blocked = bool(blocking_parts)
        will_resume = False
        saved_interrupt = interrupt
        if latest_collection.status == "ready":
            if has_schema_switch and not self._schema_switch_execution_gates_pass(plan, schema_switch_metadata=schema_switch_metadata):
                requires_confirmation = True
                latest_collection = await self._hold_ready_v2_collection_for_confirmation(
                    latest_collection,
                    question="参数已经补齐。请明确回复“确认执行”后，我再运行当前 Skill。",
                    idempotency_key=f"{turn_key}:hold_confirmation",
                )
                assistant_sections.append("参数已经补齐，但切换设计/schema 后需要你明确确认执行；请回复“确认执行”或继续修改参数。")
            elif blocked:
                requires_confirmation = True
                latest_collection = await self._hold_ready_v2_collection_for_confirmation(
                    latest_collection,
                    question=self._interrupt_ambiguity_message(latest_collection),
                    idempotency_key=f"{turn_key}:hold_blocking_ambiguity",
                )
            else:
                answer = await self._record_v2_interrupt_answer_for_resume(
                    task=task,
                    interrupt=interrupt,
                    raw_answer=raw_answer,
                    client_request_id=client_request_id,
                    source_message_id=user_message.message_id,
                    save_user_message=False,
                )
                saved_interrupt = await self.interrupt_service.record_answer(answer)
                await self._record_event(
                    self._make_event(
                        task_id=task.task_id,
                        conversation_id=task.conversation_id,
                        node_id=interrupt.node_id,
                        event_type="task.interrupt_answered",
                        payload={
                            "interrupt_id": interrupt.interrupt_id,
                            "slot_collection_id": latest_collection.collection_id,
                            "client_request_id": client_request_id,
                        },
                    )
                )
                latest_collection, scheduled_new = await self._mark_v2_slot_script_scheduled(latest_collection)
                if scheduled_new and latest_collection.status == "script_scheduled":
                    await self._schedule_v2_slot_resume(
                        task=task,
                        interrupt=saved_interrupt,
                        collection=latest_collection,
                        raw_answer=raw_answer,
                    )
                will_resume = latest_collection.status in {"script_scheduled", "completed"}

        if not assistant_sections and not will_resume:
            assistant_sections.append(self._slot_progress_message(latest_collection))
        if will_resume and assistant_sections:
            assistant_sections.append("参数已补齐，我会继续执行当前 Skill。")
        if has_schema_switch and not will_resume:
            requires_confirmation = True
        assistant_message = "\n\n".join(dict.fromkeys(section.strip() for section in assistant_sections if section.strip()))
        if assistant_message:
            assistant_message_record = Message(
                message_id=self._make_id("msg"),
                conversation_id=task.conversation_id,
                role=MessageRole.ASSISTANT,
                content=assistant_message,
                task_id=task.task_id,
                created_at=self._utcnow_naive(),
            )
            await self.storage.save_message(assistant_message_record)

        action = self._interrupt_open_turn_action(
            will_resume=will_resume,
            has_schema_switch=has_schema_switch,
            has_slot_answer=verifier_block is None and any(part.kind == "slot_answer" for part in plan.parts),
            has_question=verifier_block is not None or any(part.kind in {"skill_question", "off_topic_guidance", "ambiguous"} for part in plan.parts),
        )
        summary_payload: dict[str, object] = {
            "interrupt_id": interrupt.interrupt_id,
            "client_request_id": client_request_id,
            "source_message_id": user_message.message_id,
            "assistant_message": assistant_message,
            "action": action,
            "processed_parts": processed_parts,
            "slot_status": latest_collection.status,
            "slot_missing": list(latest_collection.missing),
            "will_resume": will_resume,
            "requires_confirmation": requires_confirmation,
            "active_slot_collection_id": latest_collection.collection_id,
        }
        if schema_switch_metadata is not None:
            summary_payload["schema_switch"] = schema_switch_metadata
        await self.storage.append_slot_event(
            SlotEvent(
                slot_event_id=f"{collection.collection_id}:event:interrupt_turn:{client_request_id}",
                collection_id=collection.collection_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                conversation_id=collection.conversation_id,
                event_type="slot.interrupt_turn_processed",
                round=latest_collection.round,
                revision=latest_collection.revision,
                idempotency_key=turn_key,
                payload=summary_payload,
                created_at=self._utcnow_naive(),
            )
        )
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_turn_processed",
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "slot_collection_id": latest_collection.collection_id,
                    "client_request_id": client_request_id,
                    "action": action,
                    "will_resume": will_resume,
                    "requires_confirmation": requires_confirmation,
                },
            )
        )
        return self._interrupt_open_turn_response_from_summary(
            interrupt=saved_interrupt,
            summary=summary_payload,
            fallback_source_message_id=user_message.message_id,
        )

    @staticmethod
    def _interrupt_open_turn_response_from_summary(
        *,
        interrupt: Interrupt,
        summary: Mapping[str, object],
        fallback_source_message_id: str | None = None,
    ) -> dict[str, object]:
        answer_payload: dict[str, object] = {
            "client_request_id": str(summary.get("client_request_id") or ""),
            "processed_parts": list(summary.get("processed_parts") or []),
            "slot_status": str(summary.get("slot_status") or ""),
            "slot_missing": list(summary.get("slot_missing") or []),
            "will_resume": bool(summary.get("will_resume")),
            "requires_confirmation": bool(summary.get("requires_confirmation")),
            "active_slot_collection_id": str(summary.get("active_slot_collection_id") or ""),
        }
        if isinstance(summary.get("schema_switch"), Mapping):
            answer_payload["schema_switch"] = dict(summary["schema_switch"])  # type: ignore[index]
        return {
            "interrupt_id": interrupt.interrupt_id,
            "status": str(interrupt.status),
            "node_id": interrupt.node_id,
            "answer_payload": answer_payload,
            "action": str(summary.get("action") or "mixed_processed"),
            "assistant_message": str(summary.get("assistant_message") or ""),
            "source_message_id": str(summary.get("source_message_id") or fallback_source_message_id or ""),
        }

    async def _plan_v2_interrupt_open_turn(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        raw_answer: dict[str, object],
        client_request_id: str,
    ) -> InterruptOpenTurnPlan:
        text = self._v2_answer_text(raw_answer)
        has_structured_attachment = bool(self._v2_answer_upload_ids(raw_answer) or self._v2_answer_sheet_selections(raw_answer))
        pending_schema_switch = await self._pending_v2_schema_switch_reuse_proposal(collection)
        pending_reuse_decision = self._pending_v2_schema_switch_reuse_decision(text) if pending_schema_switch is not None else None
        if pending_schema_switch is not None and pending_reuse_decision is not None:
            return InterruptOpenTurnPlan(
                parts=(
                    InterruptOpenTurnPart(
                        part_id="pending-schema-switch-reuse",
                        kind="schema_switch",
                        text=text,
                        target_schema_id=str(pending_schema_switch.get("new_schema_id") or collection.selected_schema_id or "") or None,
                        reuse_decision=pending_reuse_decision,
                        confidence=1.0,
                        reason="pending_schema_switch_reuse_confirmation",
                    ),
                ),
                confidence=1.0,
                reason="pending_schema_switch_reuse_confirmation",
            )
        if not text and has_structured_attachment:
            return InterruptOpenTurnPlan(
                parts=(
                    InterruptOpenTurnPart(
                        part_id="part-1",
                        kind="slot_answer",
                        uses_uploads=True,
                        confidence=1.0,
                        reason="structured_upload_or_sheet_selection",
                    ),
                ),
                confidence=1.0,
                reason="structured_upload_or_sheet_selection",
                fallback=True,
                fallback_reason="structured_upload_shortcut",
            )
        prompt = json.dumps(
            {
                "mode": "interrupt_turn_understanding",
                "planning_version": "interrupt_open_query_split_v1",
                "instructions": [
                    "Return JSON only.",
                    "The task is already interrupted and open; do not choose a new skill or start normal chat planning.",
                    "Split the user's current turn into ordered semantic parts.",
                    "Use slot_answer for values, uploads, or choices that should update the current slot collection.",
                    "Use skill_question for questions about this interrupted skill, its input data, examples, tradeoffs, or parameter meaning.",
                    "Use off_topic_guidance for unrelated questions; guide back to the current skill instead of refusing.",
                    "Use schema_switch only for same-skill input schema/design changes. Include target_schema_id and reuse_decision.",
                    "Use ambiguous when a part is unclear; set blocks_resume=true only if it should block execution.",
                    "For schema switches, set execution_confirmation=true only when the user explicitly says to execute/run after the switch.",
                ],
                "output_schema": {
                    "parts": [
                        {
                            "part_id": "stable id",
                            "kind": "slot_answer | skill_question | off_topic_guidance | schema_switch | ambiguous",
                            "text": "part text",
                            "target_slots": ["optional slot ids"],
                            "target_schema_id": "optional schema id for schema_switch",
                            "reuse_decision": "reuse | do_not_reuse | unspecified",
                            "execution_confirmation": False,
                            "execution_confirmation_confidence": 0.0,
                            "uses_uploads": False,
                            "confidence": 0.0,
                            "reason": "brief rationale",
                            "blocks_resume": False,
                            "block_reason": "why execution should be blocked",
                            "assistant_message": "optional already-grounded answer for question/ambiguity",
                        }
                    ],
                    "confidence": 0.0,
                    "reason": "brief plan rationale",
                },
                "task": {"task_id": task.task_id, "summary": task.summary},
                "interrupt": {
                    "interrupt_id": interrupt.interrupt_id,
                    "question": interrupt.question,
                    "reason_code": interrupt.reason_code,
                },
                "current_user_answer": text,
                "current_upload_ids": list(self._v2_answer_upload_ids(raw_answer)),
                "current_sheet_selections": dict(self._v2_answer_sheet_selections(raw_answer)),
                "client_request_id": client_request_id,
                "slot_collection": self._slot_collection_prompt_payload(collection),
                "pending_schema_switch": dict(pending_schema_switch) if pending_schema_switch is not None else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if self._skill_input_text_generator is None:
            return self._heuristic_interrupt_open_turn_plan(text, has_structured_attachment=has_structured_attachment)
        raw_response = await self._call_skill_input_text_generator(prompt, metadata=_INTERRUPT_TURN_LLM_METADATA)
        parsed = self._parse_interrupt_open_turn_plan(raw_response)
        if parsed is not None:
            return parsed
        legacy = self._parse_interrupt_turn_decision(raw_response)
        if legacy is not None:
            return self._turn_decision_to_open_turn_plan(legacy, raw_answer=raw_answer)
        return InterruptOpenTurnPlan(
            parts=(
                InterruptOpenTurnPart(
                    part_id="part-1",
                    kind="ambiguous",
                    text=text,
                    confidence=1.0,
                    reason="interrupt_open_turn_planner_invalid",
                    blocks_resume=True,
                    block_reason="",
                ),
            ),
            confidence=1.0,
            reason="interrupt_open_turn_planner_invalid",
            fallback=True,
            fallback_reason="invalid_llm_output",
        )

    @classmethod
    def _parse_interrupt_open_turn_plan(cls, raw_response: str) -> InterruptOpenTurnPlan | None:
        if not raw_response.strip():
            return None
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("parts"), list):
            return None
        parts: list[InterruptOpenTurnPart] = []
        for idx, raw_part in enumerate(parsed.get("parts") or [], start=1):
            if not isinstance(raw_part, Mapping):
                continue
            kind = str(raw_part.get("kind") or "").strip()
            if kind not in _INTERRUPT_OPEN_TURN_PART_KINDS:
                continue
            reuse_decision = str(raw_part.get("reuse_decision") or "unspecified").strip()
            if reuse_decision not in {"reuse", "do_not_reuse", "unspecified"}:
                reuse_decision = "unspecified"
            target_slots_raw = raw_part.get("target_slots")
            target_slots = tuple(
                str(item).strip()
                for item in (target_slots_raw if isinstance(target_slots_raw, list | tuple) else ())
                if str(item).strip()
            )
            parts.append(
                InterruptOpenTurnPart(
                    part_id=str(raw_part.get("part_id") or f"part-{idx}").strip() or f"part-{idx}",
                    kind=kind,
                    text=str(raw_part.get("text") or "").strip(),
                    target_slots=target_slots,
                    target_schema_id=str(raw_part.get("target_schema_id") or "").strip() or None,
                    reuse_decision=reuse_decision,
                    execution_confirmation=bool(raw_part.get("execution_confirmation")),
                    execution_confirmation_confidence=cls._confidence(raw_part.get("execution_confirmation_confidence")),
                    uses_uploads=bool(raw_part.get("uses_uploads")),
                    confidence=cls._confidence(raw_part.get("confidence")),
                    reason=str(raw_part.get("reason") or "").strip(),
                    blocks_resume=bool(raw_part.get("blocks_resume")),
                    block_reason=str(raw_part.get("block_reason") or "").strip(),
                    assistant_message=str(raw_part.get("assistant_message") or "").strip(),
                )
            )
        if not parts:
            return None
        return InterruptOpenTurnPlan(
            parts=tuple(parts),
            confidence=cls._confidence(parsed.get("confidence")),
            reason=str(parsed.get("reason") or "").strip(),
        )

    @staticmethod
    def _confidence(value: object) -> float:
        try:
            return max(0.0, min(float(value), 1.0))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _turn_decision_to_open_turn_plan(
        cls,
        decision: InterruptTurnDecision,
        *,
        raw_answer: Mapping[str, object],
    ) -> InterruptOpenTurnPlan:
        text = cls._v2_answer_text(raw_answer)
        uses_uploads = bool(cls._v2_answer_upload_ids(raw_answer) or cls._v2_answer_sheet_selections(raw_answer))
        if decision.intent == "slot_answer":
            kind = "slot_answer"
            parts = (
                InterruptOpenTurnPart(
                    part_id="part-1",
                    kind=kind,
                    text=str((decision.extracted_answer or {}).get("text") or text),
                    uses_uploads=uses_uploads,
                    confidence=decision.confidence,
                    reason=decision.reason,
                ),
            )
        elif decision.intent == "mixed":
            part_list: list[InterruptOpenTurnPart] = []
            extracted_text = str((decision.extracted_answer or {}).get("text") or "").strip()
            if extracted_text or uses_uploads:
                part_list.append(
                    InterruptOpenTurnPart(
                        part_id="part-1",
                        kind="slot_answer",
                        text=extracted_text or text,
                        uses_uploads=uses_uploads,
                        confidence=decision.confidence,
                        reason=decision.reason,
                    )
                )
            part_list.append(
                InterruptOpenTurnPart(
                    part_id=f"part-{len(part_list)+1}",
                    kind="skill_question",
                    text=text,
                    confidence=decision.confidence,
                    reason=decision.reason,
                    assistant_message=decision.clarification_answer,
                )
            )
            parts = tuple(part_list)
        elif decision.intent == "clarification_question":
            parts = (
                InterruptOpenTurnPart(
                    part_id="part-1",
                    kind="skill_question",
                    text=text,
                    confidence=decision.confidence,
                    reason=decision.reason,
                    assistant_message=decision.clarification_answer,
                ),
            )
        else:
            parts = (
                InterruptOpenTurnPart(
                    part_id="part-1",
                    kind="ambiguous",
                    text=text,
                    confidence=decision.confidence,
                    reason=decision.reason,
                    blocks_resume=True,
                    block_reason=decision.clarification_answer,
                    assistant_message=decision.clarification_answer,
                ),
            )
        return InterruptOpenTurnPlan(parts=parts, confidence=decision.confidence, reason=decision.reason, fallback=True, fallback_reason="legacy_decision_shape")

    @classmethod
    def _heuristic_interrupt_open_turn_plan(
        cls,
        text: str,
        *,
        has_structured_attachment: bool,
    ) -> InterruptOpenTurnPlan:
        if has_structured_attachment:
            return InterruptOpenTurnPlan(
                parts=(
                    InterruptOpenTurnPart(
                        part_id="part-1",
                        kind="slot_answer",
                        text=text,
                        uses_uploads=True,
                        confidence=1.0,
                        reason="structured_attachment",
                    ),
                ),
                confidence=1.0,
                reason="structured_attachment",
                fallback=True,
                fallback_reason="heuristic",
            )
        decision = cls._heuristic_interrupt_turn_decision(text)
        return cls._turn_decision_to_open_turn_plan(decision, raw_answer={"text": text})

    async def _record_v2_interrupt_turn_planned(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        client_request_id: str,
        plan: InterruptOpenTurnPlan,
    ) -> None:
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_turn_planned",
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "slot_collection_id": collection.collection_id,
                    "client_request_id": client_request_id,
                    "part_kinds": [part.kind for part in plan.parts],
                    "confidence": plan.confidence,
                    "fallback": plan.fallback,
                    "fallback_reason": plan.fallback_reason,
                },
            )
        )

    async def _save_v2_interrupt_user_message(
        self,
        *,
        task: Task,
        raw_answer: Mapping[str, object],
        source_message_id: str | None,
    ) -> Message:
        message = Message(
            message_id=source_message_id or self._make_id("msg"),
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content=self._format_v2_answer_message(raw_answer) or self._v2_answer_text(raw_answer),
            task_id=task.task_id,
            created_at=self._utcnow_naive(),
        )
        await self.storage.save_message(message)
        return message

    @staticmethod
    def _raw_answer_for_interrupt_part(raw_answer: Mapping[str, object], part: InterruptOpenTurnPart) -> dict[str, object]:
        answer = dict(raw_answer)
        if part.text:
            answer["text"] = part.text
        return answer

    async def _process_v2_slot_answer_part(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        raw_answer: dict[str, object],
        client_request_id: str,
        source_message_id: str,
    ) -> SlotCollection:
        upload_ids = self._v2_answer_upload_ids(raw_answer)
        if upload_ids:
            conversation = await self.storage.get_conversation(task.conversation_id)
            if conversation is None:
                raise ValueError(f"Unknown conversation: {task.conversation_id}")
            await self._bind_or_update_resume_input_attachments(
                task=task,
                username=conversation.username,
                upload_ids=upload_ids,
                source_kind="interrupt_answer_upload",
                source_message_id=source_message_id,
                interrupt_answer_id=None,
                upload_sheet_selections=self._v2_answer_sheet_selections(raw_answer),
            )
        return await self._apply_v2_slot_answer(
            collection=collection,
            interrupt=interrupt,
            raw_answer=raw_answer,
            client_request_id=client_request_id,
        )

    async def _record_v2_interrupt_answer_for_resume(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        raw_answer: Mapping[str, object],
        client_request_id: str,
        source_message_id: str,
        save_user_message: bool,
    ) -> InterruptAnswer:
        answer = InterruptAnswer(
            interrupt_answer_id=self._make_id("interrupt-answer"),
            interrupt_id=interrupt.interrupt_id,
            answer_payload={"client_request_id": client_request_id, "answer": dict(raw_answer)},
            source_message_id=source_message_id,
            created_at=self._utcnow_naive(),
        )
        upload_ids = self._v2_answer_upload_ids(raw_answer)
        if upload_ids:
            conversation = await self.storage.get_conversation(task.conversation_id)
            if conversation is None:
                raise ValueError(f"Unknown conversation: {task.conversation_id}")
            await self._bind_or_update_resume_input_attachments(
                task=task,
                username=conversation.username,
                upload_ids=upload_ids,
                source_kind="interrupt_answer_upload",
                source_message_id=source_message_id,
                interrupt_answer_id=answer.interrupt_answer_id,
                upload_sheet_selections=self._v2_answer_sheet_selections(raw_answer),
            )
        if save_user_message:
            await self._save_v2_interrupt_user_message(task=task, raw_answer=raw_answer, source_message_id=source_message_id)
        return answer

    @staticmethod
    def _requires_v2_interrupt_resume_verification(
        plan: InterruptOpenTurnPlan,
        *,
        raw_answer: Mapping[str, object],
    ) -> bool:
        if ApiRuntime._v2_answer_upload_ids(raw_answer) or ApiRuntime._v2_answer_sheet_selections(raw_answer):
            return False
        return (
            len(plan.parts) == 1
            and plan.parts[0].kind == "slot_answer"
            and plan.parts[0].confidence >= _INTERRUPT_TURN_SLOT_ANSWER_CONFIDENCE
        )

    async def _save_v2_interrupt_answer_without_closing(
        self,
        *,
        interrupt: Interrupt,
        raw_answer: Mapping[str, object],
        client_request_id: str,
        source_message_id: str,
    ) -> None:
        existing = await self.storage.list_interrupt_answers(interrupt.interrupt_id)
        if any(
            isinstance(answer.answer_payload, Mapping)
            and str(answer.answer_payload.get("client_request_id") or "").strip() == client_request_id
            for answer in existing
        ):
            return
        await self.storage.save_interrupt_answer(
            InterruptAnswer(
                interrupt_answer_id=self._make_id("interrupt-answer"),
                interrupt_id=interrupt.interrupt_id,
                answer_payload={"client_request_id": client_request_id, "answer": dict(raw_answer)},
                source_message_id=source_message_id,
                accepted=True,
                created_at=self._utcnow_naive(),
                accepted_at=self._utcnow_naive(),
            )
        )

    async def _process_v2_skill_question_part(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        part: InterruptOpenTurnPart,
    ) -> str:
        resource_context, resource_audits = self._slot_question_resource_context(collection)
        await self._record_v2_interrupt_soft_binding_audit(
            task=task,
            interrupt=interrupt,
            collection=collection,
            part=part,
            resource_audits=resource_audits,
        )
        prompt = json.dumps(
            {
                "mode": "interrupt_skill_question_answer",
                "instructions": [
                    "Answer inside the currently interrupted Skill context.",
                    "Use the provided SKILL.md overview, skill resources, and slot schema; do not execute the skill and do not close the interrupt.",
                    "Treat SKILL.md as the current Skill overview/runbook; treat references as detailed user-facing facts.",
                    "If the user asks for examples, include concrete examples from resources or SKILL.md when available.",
                    "Keep the answer concise, then guide the user back to the missing slot values.",
                ],
                "task": {"task_id": task.task_id, "summary": task.summary},
                "interrupt": {"question": interrupt.question, "reason_code": interrupt.reason_code},
                "user_question": part.text,
                "slot_collection": self._slot_collection_prompt_payload(collection),
                "resource_context": resource_context,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        raw_response = await self._call_skill_input_text_generator(prompt, metadata=_INTERRUPT_TURN_LLM_METADATA)
        answer = self._parse_clarification_answer(raw_response)
        if not answer and resource_context:
            snippets = []
            for resource in resource_context[:2]:
                content = str(resource.get("content") or "").strip()
                if content:
                    snippets.append(content[:800])
            if snippets:
                answer = "\n\n".join(snippets)
        if not answer:
            answer = self._slot_progress_message(collection)
        await self._record_v2_interrupt_question_answered(
            task=task,
            interrupt=interrupt,
            collection=collection,
            part=part,
            answer=answer,
            resource_ids=[str(item.get("resource_id") or "") for item in resource_context],
        )
        return answer

    async def _record_v2_interrupt_question_answered(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        part: InterruptOpenTurnPart,
        answer: str,
        resource_ids: Iterable[str] = (),
    ) -> None:
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_question_answered",
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "slot_collection_id": collection.collection_id,
                    "part_id": part.part_id,
                    "answer_length": len(answer),
                    "resource_ids": list(resource_ids),
                },
            )
        )
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_clarification_answered",
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "slot_collection_id": collection.collection_id,
                    "part_id": part.part_id,
                },
            )
        )

    async def _append_v2_question_answer_slot_event(
        self,
        *,
        collection: SlotCollection,
        interrupt: Interrupt,
        client_request_id: str,
        part: InterruptOpenTurnPart,
        assistant_message: str,
        source_message_id: str,
    ) -> None:
        key = f"question:{interrupt.interrupt_id}:{client_request_id}:{part.part_id}"
        if await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, key) is not None:
            return
        await self.storage.append_slot_event(
            SlotEvent(
                slot_event_id=f"{collection.collection_id}:event:question:{client_request_id}:{part.part_id}",
                collection_id=collection.collection_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                conversation_id=collection.conversation_id,
                event_type="slot.clarification_answered",
                round=collection.round,
                revision=collection.revision,
                idempotency_key=key,
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "client_request_id": client_request_id,
                    "part_id": part.part_id,
                    "intent": "skill_question",
                    "confidence": part.confidence,
                    "reason": part.reason,
                    "assistant_message": assistant_message,
                    "source_message_id": source_message_id,
                },
                created_at=self._utcnow_naive(),
            )
        )

    async def _record_v2_interrupt_soft_binding_audit(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        part: InterruptOpenTurnPart,
        resource_audits: Iterable[Mapping[str, object]],
    ) -> None:
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="soft_skill_binding.decision",
                visibility=EventVisibility.AUDIT_ONLY,
                payload={
                    "decision": "answer",
                    "target_capability_id": collection.capability_id,
                    "confidence": part.confidence,
                    "reason_code": "interrupt_skill_question",
                    "interrupt_id": interrupt.interrupt_id,
                    "slot_collection_id": collection.collection_id,
                    "part_id": part.part_id,
                    "execute_suppressed": True,
                },
            )
        )
        for audit_payload in resource_audits:
            await self._record_event(
                self._make_event(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    node_id=interrupt.node_id,
                    event_type="skill.resource_read",
                    visibility=EventVisibility.AUDIT_ONLY,
                    payload=dict(audit_payload),
                )
            )

    def _slot_question_resource_context(self, collection: SlotCollection) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        manifest = self._manifest_for_slot_collection(collection)
        contract = getattr(manifest, "contract", None)
        if manifest is None or contract is None:
            return [], []
        service = SkillResourceService()
        skill_name = getattr(manifest, "name", collection.skill_name)
        contexts: list[dict[str, object]] = []
        audits: list[dict[str, object]] = []

        skill_overview_result = service.read(
            contract,
            skill_name=skill_name,
            audience="slot_question",
            path="SKILL.md",
            max_bytes=8192,
        )
        skill_overview_audit = skill_overview_result.audit_payload()
        skill_overview_audit["resource_id"] = "skill_overview"
        skill_overview_audit["description"] = "Current Skill SKILL.md overview/runbook"
        audits.append(skill_overview_audit)
        if skill_overview_result.ok:
            contexts.append(
                {
                    "resource_id": "skill_overview",
                    "path": skill_overview_result.path or "SKILL.md",
                    "title": "SKILL.md 总纲",
                    "description": "当前 Skill 的 agent-facing 总纲、资源导航和边界。",
                    "content": skill_overview_result.content,
                    "truncated": skill_overview_result.truncated,
                    "redaction_count": skill_overview_result.redaction_count,
                }
            )

        for resource in getattr(contract, "resources", {}).values():
            if "slot_question" not in resource.audience and "main_agent" not in resource.audience:
                continue
            audience = "slot_question" if "slot_question" in resource.audience else "main_agent"
            result = service.read(
                contract,
                skill_name=skill_name,
                audience=audience,
                resource_id=resource.resource_id,
                max_bytes=8192,
            )
            audits.append(result.audit_payload())
            if not result.ok:
                continue
            contexts.append(
                {
                    "resource_id": result.resource_id,
                    "path": result.path,
                    "title": getattr(resource, "title", "") or result.resource_id,
                    "description": getattr(resource, "description", ""),
                    "content": result.content,
                    "truncated": result.truncated,
                    "redaction_count": result.redaction_count,
                }
            )
        return contexts, audits

    def _process_v2_off_topic_guidance_part(self, *, collection: SlotCollection, part: InterruptOpenTurnPart) -> str:
        missing = "、".join(collection.missing) if collection.missing else "当前待确认参数"
        topic = part.text.strip() or "这个问题"
        return (
            f"关于“{topic}”，我先不切换到新的任务；当前还在 {collection.skill_name} 的 interrupt 中。"
            f"你可以把它和当前 Skill 需求关联起来问，或先补充：{missing}。interrupt 会继续保持打开。"
        )

    async def _pending_v2_schema_switch_reuse_proposal(self, collection: SlotCollection) -> dict[str, object] | None:
        for event in reversed(await self.storage.list_slot_events(collection.collection_id)):
            if event.event_type == "slot.schema_switch_reuse_confirmed":
                return None
            if event.event_type != "slot.schema_switched":
                continue
            payload = dict(event.payload)
            if payload.get("pending_reuse_confirmation") is True and str(payload.get("reuse_decision") or "") == "unspecified":
                return payload
            return None
        return None

    @staticmethod
    def _pending_v2_schema_switch_reuse_decision(text: str) -> str | None:
        normalized = str(text or "").strip().lower()
        compact = "".join(ch for ch in normalized if ch not in {" ", "\t", "\n", "\r", "，", ",", "。", ".", "！", "!", "？", "?"})
        if not compact:
            return None
        negative_markers = (
            "不复用",
            "不要复用",
            "不沿用",
            "不要沿用",
            "不保留",
            "重新填",
            "重新提供",
            "空的",
            "空collection",
            "do not reuse",
            "dont reuse",
            "don't reuse",
            "no reuse",
        )
        if any(marker in normalized or marker.replace(" ", "") in compact for marker in negative_markers):
            return "do_not_reuse"
        positive_markers = (
            "复用",
            "沿用",
            "保留",
            "用旧",
            "使用已有",
            "用已有",
            "reuse",
            "yes",
            "确认",
            "可以",
        )
        if any(marker in normalized or marker.replace(" ", "") in compact for marker in positive_markers):
            return "reuse"
        if compact in {"是", "好", "好的", "可以", "确认", "ok", "yes", "y"}:
            return "reuse"
        if compact in {"否", "不用", "不要", "no", "n"}:
            return "do_not_reuse"
        return None

    @staticmethod
    def _schema_switch_const_candidates(schema, *, raw_value: object) -> dict[str, SlotExtractionCandidate]:
        candidates: dict[str, SlotExtractionCandidate] = {}
        for field, schema_field in schema.inputs.items():
            const_value = getattr(schema_field, "const", None)
            if const_value is None:
                continue
            candidates[field] = SlotExtractionCandidate(
                field=field,
                raw_value=raw_value,
                value=const_value,
                source="schema_switch",
                confidence=1.0,
            )
        return candidates

    async def _process_v2_schema_switch_part(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        part: InterruptOpenTurnPart,
        raw_answer: Mapping[str, object],
        client_request_id: str,
    ) -> tuple[SlotCollection, dict[str, object], str, bool]:
        pending_reuse = await self._pending_v2_schema_switch_reuse_proposal(collection)
        if pending_reuse is not None and part.reuse_decision in {"reuse", "do_not_reuse"}:
            pending_target_schema_id = str(pending_reuse.get("new_schema_id") or collection.selected_schema_id or "").strip()
            if not part.target_schema_id or part.target_schema_id == pending_target_schema_id:
                return await self._confirm_pending_v2_schema_switch_reuse(
                    interrupt=interrupt,
                    collection=collection,
                    part=part,
                    proposal=pending_reuse,
                    client_request_id=client_request_id,
                )

        manifest = self._manifest_for_slot_collection(collection)
        contract = getattr(manifest, "contract", None)
        if manifest is None or contract is None:
            metadata = {"allowed": False, "reason": "manifest_unavailable", "reuse_decision": part.reuse_decision}
            return collection, metadata, "当前 Skill 配置不可用，无法切换输入 schema；interrupt 会继续保持打开。", True
        schemas = load_input_schemas_for_contract(contract)
        target_schema_id = str(part.target_schema_id or "").strip()
        if target_schema_id not in schemas:
            metadata = {
                "allowed": False,
                "reason": "target_schema_unavailable",
                "target_schema_id": target_schema_id,
                "available_schema_ids": sorted(schemas),
                "reuse_decision": part.reuse_decision,
            }
            return collection, metadata, "我还不能确认要切换到哪一种设计/schema，请说明目标设计类型。", True

        old_schema_id = collection.selected_schema_id
        schema = schemas[target_schema_id]
        selected_entrypoint = schema.entrypoint_mapping or collection.selected_entrypoint or "run"
        initialized, _ = initialize_input_collection(
            collection_id=collection.collection_id,
            task_id=collection.task_id,
            node_id=collection.node_id,
            conversation_id=collection.conversation_id,
            capability_id=collection.capability_id,
            skill_name=collection.skill_name,
            schema=schema,
            selected_entrypoint=selected_entrypoint,
            now=self._utcnow_naive(),
            skill_bundle_revision=collection.skill_bundle_revision,
            contract_revision=collection.contract_revision,
            schema_digest=None,
            resources=contract.resources,
        )
        initialized = replace(
            initialized,
            revision=collection.revision + 1,
            round=collection.round + 1,
            schema_digest=self._slot_schema_digest(initialized.schema_snapshot),
            created_at=collection.created_at,
            updated_at=self._utcnow_naive(),
        )
        copied_fields: list[str] = []
        old_schema_inputs = set()
        if isinstance(collection.schema_snapshot, Mapping) and isinstance(collection.schema_snapshot.get("inputs"), Mapping):
            old_schema_inputs = {str(field) for field in collection.schema_snapshot["inputs"]}
        discarded_fields = sorted((old_schema_inputs or set(collection.resolved)) - set(schema.inputs))
        copyable_resolved = {
            str(field): dict(value)
            for field, value in collection.resolved.items()
            if str(field) in schema.inputs
            and getattr(schema.inputs[str(field)], "const", None) is None
            and isinstance(value, Mapping)
        }
        next_collection = initialized
        const_candidates = self._schema_switch_const_candidates(schema, raw_value=target_schema_id or self._v2_answer_text(raw_answer))
        if const_candidates:
            validating = replace(next_collection, status="validating")
            next_collection, _ = apply_extraction_result_to_collection(
                validating,
                schema,
                SlotExtractionResult(resolved=const_candidates, diagnostics=("schema_switch_target_const",)),
                now=self._utcnow_naive(),
            )
            if next_collection.status == "waiting_for_user":
                next_collection = replace(next_collection, last_question=self._slot_question_from_collection(next_collection))
        if part.reuse_decision == "reuse":
            candidates: dict[str, SlotExtractionCandidate] = dict(const_candidates)
            for field, value in copyable_resolved.items():
                copied_fields.append(field)
                candidates[field] = SlotExtractionCandidate(
                    field=field,
                    raw_value=value.get("raw_value", value.get("value")),
                    value=value.get("value", value.get("raw_value")),
                    source="slot_collection",
                    confidence=1.0,
                )
            validating = replace(next_collection, status="validating")
            next_collection, event = apply_extraction_result_to_collection(
                validating,
                schema,
                SlotExtractionResult(resolved=candidates, diagnostics=("schema_switch_reuse",)),
                now=self._utcnow_naive(),
            )
            if next_collection.status == "waiting_for_user":
                next_collection = replace(next_collection, last_question=self._slot_question_from_collection(next_collection))
        elif part.reuse_decision == "unspecified":
            next_collection = replace(
                next_collection,
                status="waiting_for_user",
                last_question="你要复用旧设计里同名字段的参数吗？回复“复用”或“不复用”，也可以直接给新的参数。",
            )
        else:
            next_collection = replace(
                next_collection,
                status="waiting_for_user" if next_collection.missing else "waiting_for_user",
                last_question=self._slot_question_from_collection(next_collection) if next_collection.missing else "已切换 schema。请确认是否继续执行，或继续修改参数。",
            )
        event = SlotEvent(
            slot_event_id=f"{collection.collection_id}:event:schema_switched:{client_request_id}",
            collection_id=collection.collection_id,
            task_id=collection.task_id,
            node_id=collection.node_id,
            conversation_id=collection.conversation_id,
            event_type="slot.schema_switched",
            round=next_collection.round,
            revision=next_collection.revision,
            idempotency_key=f"schema_switch:{interrupt.interrupt_id}:{client_request_id}",
            payload={
                "old_schema_id": old_schema_id,
                "new_schema_id": target_schema_id,
                "reuse_decision": part.reuse_decision,
                "copied_fields": copied_fields,
                "discarded_fields": discarded_fields,
                "empty_required_fields": list(next_collection.missing),
                "current_user_text": self._v2_answer_text(raw_answer),
                "pending_reuse_confirmation": part.reuse_decision == "unspecified",
                "copyable_resolved": copyable_resolved if part.reuse_decision == "unspecified" else {},
            },
            created_at=self._utcnow_naive(),
        )
        saved = await self.storage.apply_slot_transition(
            collection.collection_id,
            collection.revision,
            next_collection,
            event,
            idempotency_key=event.idempotency_key,
        )
        active = saved or await self.storage.get_slot_collection(collection.collection_id) or next_collection
        saved_interrupt = replace(
            interrupt,
            question=active.last_question or interrupt.question,
            required_fields=slot_collection_required_fields_ref(active),
            status=InterruptStatus.OPEN,
        )
        await self.storage.save_interrupt(saved_interrupt)
        metadata = {
            "old_schema_id": old_schema_id,
            "new_schema_id": target_schema_id,
            "reuse_decision": part.reuse_decision,
            "copied_fields": copied_fields,
            "discarded_fields": discarded_fields,
            "empty_required_fields": list(active.missing),
            "active_slot_collection_id": active.collection_id,
        }
        if part.reuse_decision == "unspecified":
            return active, metadata, "已切换到新的 schema；你要复用旧参数中同名字段的值吗？回复“复用”或“不复用”。", True
        return active, metadata, f"已切换到 {target_schema_id} schema。" if target_schema_id else "已切换 schema。", False

    async def _confirm_pending_v2_schema_switch_reuse(
        self,
        *,
        interrupt: Interrupt,
        collection: SlotCollection,
        part: InterruptOpenTurnPart,
        proposal: Mapping[str, object],
        client_request_id: str,
    ) -> tuple[SlotCollection, dict[str, object], str, bool]:
        schema = self._schema_for_slot_collection(collection)
        if schema is None:
            metadata = {
                "allowed": False,
                "reason": "schema_snapshot_unavailable",
                "reuse_decision": part.reuse_decision,
                "active_slot_collection_id": collection.collection_id,
            }
            return collection, metadata, "当前 schema 状态不可用，无法确认是否复用旧参数；interrupt 会继续保持打开。", True

        confirm_key = f"schema_switch_reuse:{interrupt.interrupt_id}:{client_request_id}"
        existing = await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, confirm_key)
        if existing is not None:
            current = await self.storage.get_slot_collection(collection.collection_id) or collection
            metadata = dict(existing.payload)
            metadata.setdefault("active_slot_collection_id", current.collection_id)
            return current, metadata, "", False

        copied_fields: list[str] = []
        copyable_raw = proposal.get("copyable_resolved")
        copyable_resolved = dict(copyable_raw) if isinstance(copyable_raw, Mapping) else {}
        discarded_fields = list(proposal.get("discarded_fields") or [])
        next_collection = collection
        if part.reuse_decision == "reuse":
            candidates: dict[str, SlotExtractionCandidate] = {}
            for field, raw_value in copyable_resolved.items():
                field_name = str(field)
                if field_name not in schema.inputs or not isinstance(raw_value, Mapping):
                    continue
                copied_fields.append(field_name)
                value = dict(raw_value)
                candidates[field_name] = SlotExtractionCandidate(
                    field=field_name,
                    raw_value=value.get("raw_value", value.get("value")),
                    value=value.get("value", value.get("raw_value")),
                    source="slot_collection",
                    confidence=1.0,
                )
            validating = replace(collection, status="validating")
            next_collection, event = apply_extraction_result_to_collection(
                validating,
                schema,
                SlotExtractionResult(resolved=candidates, diagnostics=("schema_switch_reuse_confirmed",)),
                now=self._utcnow_naive(),
            )
            if next_collection.status == "waiting_for_user":
                next_collection = replace(
                    next_collection,
                    round=collection.round + 1,
                    last_question=self._slot_question_from_collection(next_collection),
                )
        else:
            next_collection = replace(
                collection,
                status="waiting_for_user",
                revision=collection.revision + 1,
                round=collection.round + 1,
                last_question=self._slot_question_from_collection(collection),
                updated_at=self._utcnow_naive(),
            )
            event = SlotEvent(
                slot_event_id=f"{collection.collection_id}:event:schema_switch_reuse:{client_request_id}",
                collection_id=collection.collection_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                conversation_id=collection.conversation_id,
                event_type="slot.schema_switch_reuse_confirmed",
                round=next_collection.round,
                revision=next_collection.revision,
                idempotency_key=confirm_key,
                payload={},
                created_at=self._utcnow_naive(),
            )

        metadata = {
            "old_schema_id": str(proposal.get("old_schema_id") or ""),
            "new_schema_id": str(proposal.get("new_schema_id") or collection.selected_schema_id or ""),
            "reuse_decision": part.reuse_decision,
            "copied_fields": copied_fields,
            "discarded_fields": discarded_fields,
            "empty_required_fields": list(next_collection.missing),
            "active_slot_collection_id": next_collection.collection_id,
            "pending_reuse_confirmation": False,
        }
        event = replace(
            event,
            event_type="slot.schema_switch_reuse_confirmed",
            idempotency_key=confirm_key,
            payload=metadata,
        )
        saved = await self.storage.apply_slot_transition(
            collection.collection_id,
            collection.revision,
            next_collection,
            event,
            idempotency_key=confirm_key,
        )
        active = saved or await self.storage.get_slot_collection(collection.collection_id) or next_collection
        await self.storage.save_interrupt(
            replace(
                interrupt,
                question=active.last_question or interrupt.question,
                required_fields=slot_collection_required_fields_ref(active),
                status=InterruptStatus.OPEN,
            )
        )
        if part.reuse_decision == "reuse":
            message = "已复用旧 schema 中同名字段的参数。"
        else:
            message = "已确认不复用旧参数；请按新 schema 重新补充缺失字段。"
        return active, {**metadata, "empty_required_fields": list(active.missing)}, message, False

    @staticmethod
    def _schema_switch_execution_gates_pass(
        plan: InterruptOpenTurnPlan,
        *,
        schema_switch_metadata: Mapping[str, object] | None,
    ) -> bool:
        if schema_switch_metadata is None or schema_switch_metadata.get("allowed") is False:
            return False
        if str(schema_switch_metadata.get("reuse_decision") or "unspecified") not in {"reuse", "do_not_reuse"}:
            return False
        if any(part.blocks_resume for part in plan.parts):
            return False
        return any(
            part.kind == "schema_switch"
            and part.execution_confirmation
            and part.execution_confirmation_confidence >= _INTERRUPT_SCHEMA_SWITCH_EXECUTION_CONFIDENCE
            for part in plan.parts
        )

    async def _hold_ready_v2_collection_for_confirmation(
        self,
        collection: SlotCollection,
        *,
        question: str,
        idempotency_key: str,
    ) -> SlotCollection:
        if collection.status != "ready":
            return collection
        held = replace(
            collection,
            status="waiting_for_user",
            revision=collection.revision + 1,
            round=collection.round + 1,
            last_question=question,
            updated_at=self._utcnow_naive(),
        )
        event = SlotEvent(
            slot_event_id=f"{collection.collection_id}:event:confirmation_required:{held.revision}",
            collection_id=collection.collection_id,
            task_id=collection.task_id,
            node_id=collection.node_id,
            conversation_id=collection.conversation_id,
            event_type="slot.confirmation_required",
            round=held.round,
            revision=held.revision,
            idempotency_key=idempotency_key,
            payload={"question": question},
            created_at=self._utcnow_naive(),
        )
        saved = await self.storage.apply_slot_transition(
            collection.collection_id,
            collection.revision,
            held,
            event,
            idempotency_key=idempotency_key,
        )
        return saved or await self.storage.get_slot_collection(collection.collection_id) or held

    @staticmethod
    def _interrupt_part_summary(part: InterruptOpenTurnPart, *, result: Mapping[str, object] | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "part_id": part.part_id,
            "kind": part.kind,
            "text": part.text,
            "target_slots": list(part.target_slots),
            "target_schema_id": part.target_schema_id,
            "reuse_decision": part.reuse_decision,
            "execution_confirmation": part.execution_confirmation,
            "execution_confirmation_confidence": part.execution_confirmation_confidence,
            "uses_uploads": part.uses_uploads,
            "confidence": part.confidence,
            "reason": part.reason,
            "blocks_resume": part.blocks_resume,
            "block_reason": part.block_reason,
        }
        if result:
            payload["result"] = dict(result)
        return payload

    @staticmethod
    def _interrupt_open_turn_action(
        *,
        will_resume: bool,
        has_schema_switch: bool,
        has_slot_answer: bool,
        has_question: bool,
    ) -> str:
        if will_resume:
            return "resumed"
        if has_schema_switch:
            return "schema_switched"
        if has_slot_answer or has_question:
            return "mixed_processed" if has_slot_answer and has_question else "clarification_answer" if has_question else "mixed_processed"
        return "mixed_processed"

    def _slot_progress_message(self, collection: SlotCollection) -> str:
        if collection.missing:
            return f"已保留当前 interrupt 状态。还需要补充：{'、'.join(collection.missing)}。"
        return "已保留当前 interrupt 状态；请确认是否继续执行或继续修改参数。"

    def _interrupt_ambiguity_message(self, collection: SlotCollection) -> str:
        return f"我还不能安全确认这一部分是否应该执行。{self._slot_progress_message(collection)}"

    async def _schedule_v2_slot_resume(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        raw_answer: dict[str, object],
    ) -> None:
        await self._await_existing_execution(task.task_id)
        root_message = await self.storage.get_message(task.root_message_id)
        resume_metadata = {
            **self._resume_skill_revision_metadata(task.task_id),
            SLOT_COLLECTION_METADATA_KEY: self._slot_collection_resume_metadata(collection),
            "slot_collection_id": collection.collection_id,
            "slot_collection_revision": collection.revision,
            "resume_interrupted_node_id": interrupt.node_id,
        }
        resume_metadata.update(await self._task_input_attachment_metadata(task.task_id))
        resume_finalizer_node_id = await self._resume_finalizer_node_id(task.task_id, interrupt.node_id)
        if resume_finalizer_node_id:
            resume_metadata["resume_finalizer_node_id"] = resume_finalizer_node_id
        resume_capability_id = task.requested_capability_id
        interrupted_node = await self.storage.get_task_node(interrupt.node_id)
        if interrupted_node is not None and interrupted_node.capability_id.startswith("skill."):
            resume_capability_id = interrupted_node.capability_id
        elif interrupt.source_agent.startswith("skill.") and self.capability_registry.get(interrupt.source_agent) is not None:
            resume_capability_id = interrupt.source_agent
        await self._schedule_execution(
            OrchestrationRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=self._combine_v2_resume_message(
                    root_message.content if root_message is not None else task.summary or "",
                    raw_answer,
                ),
                requested_capability_id=resume_capability_id,
                metadata=resume_metadata,
            )
        )

    async def _understand_v2_interrupt_turn(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        raw_answer: dict[str, object],
        client_request_id: str,
    ) -> InterruptTurnDecision:
        text = self._v2_answer_text(raw_answer)
        if not text and (self._v2_answer_upload_ids(raw_answer) or self._v2_answer_sheet_selections(raw_answer)):
            return InterruptTurnDecision(intent="slot_answer", confidence=1.0, reason="structured_upload_or_sheet_selection")

        prompt = json.dumps(
            {
                "mode": "interrupt_turn_understanding",
                "instructions": [
                    "Return JSON only.",
                    "Classify the user's current interrupt turn before any state transition.",
                    "Use slot_answer only when the user is clearly providing data/choice needed by the current slot prompt.",
                    "Use clarification_question when the user asks about format, examples, differences, tradeoffs, meaning, or how to decide.",
                    "Use ambiguous when uncertain. State safety rule: ambiguous keeps the interrupt open.",
                ],
                "output_schema": {
                    "intent": "slot_answer | clarification_question | mixed | ambiguous",
                    "confidence": 0.0,
                    "reason": "brief rationale",
                    "clarification_answer": "answer user question if intent is clarification_question or mixed",
                    "extracted_answer": {"text": "optional canonical answer text"},
                },
                "task": {"task_id": task.task_id, "summary": task.summary},
                "interrupt": {
                    "interrupt_id": interrupt.interrupt_id,
                    "question": interrupt.question,
                    "reason_code": interrupt.reason_code,
                },
                "current_user_answer": text,
                "client_request_id": client_request_id,
                "slot_collection": self._slot_collection_prompt_payload(collection),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if self._skill_input_text_generator is None:
            return self._heuristic_interrupt_turn_decision(text)
        raw_response = await self._call_skill_input_text_generator(prompt, metadata=_INTERRUPT_TURN_LLM_METADATA)
        decision = self._parse_interrupt_turn_decision(raw_response)
        if decision is not None:
            return decision
        return InterruptTurnDecision(
            intent="ambiguous",
            confidence=1.0,
            reason="interrupt_turn_llm_unavailable_or_invalid",
        )

    @staticmethod
    def _parse_interrupt_turn_decision(raw_response: str) -> InterruptTurnDecision | None:
        if not raw_response.strip():
            return None
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, Mapping):
            return None
        intent = str(parsed.get("intent") or "").strip()
        if intent not in {"slot_answer", "clarification_question", "mixed", "ambiguous"}:
            return None
        try:
            confidence = float(parsed.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        extracted = parsed.get("extracted_answer")
        return InterruptTurnDecision(
            intent=intent,
            confidence=max(0.0, min(confidence, 1.0)),
            reason=str(parsed.get("reason") or ""),
            clarification_answer=str(parsed.get("clarification_answer") or ""),
            extracted_answer=dict(extracted) if isinstance(extracted, Mapping) else None,
        )

    @staticmethod
    def _heuristic_interrupt_turn_decision(text: str) -> InterruptTurnDecision:
        normalized = str(text or "").strip()
        question_markers = (
            "?",
            "？",
            "什么",
            "怎么",
            "如何",
            "区别",
            "差别",
            "利弊",
            "优缺点",
            "格式",
            "例子",
            "示例",
            "说明",
            "解释",
            "含义",
            "意思",
            "哪种",
            "为什么",
        )
        if normalized and any(marker in normalized for marker in question_markers):
            return InterruptTurnDecision(intent="clarification_question", confidence=0.92, reason="question_marker")
        return InterruptTurnDecision(intent="slot_answer", confidence=0.82 if normalized else 0.0, reason="fallback_preserve_existing_answer_flow")

    @staticmethod
    def _should_resume_from_interrupt_turn(
        decision: InterruptTurnDecision,
        *,
        raw_answer: dict[str, object],
    ) -> bool:
        has_structured_attachment = bool(
            ApiRuntime._v2_answer_upload_ids(raw_answer)
            or ApiRuntime._v2_answer_sheet_selections(raw_answer)
        )
        if has_structured_attachment and not ApiRuntime._v2_answer_text(raw_answer):
            return True
        return decision.intent == "slot_answer" and decision.confidence >= _INTERRUPT_TURN_SLOT_ANSWER_CONFIDENCE

    async def _verify_v2_interrupt_resume(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        raw_answer: dict[str, object],
        decision: InterruptTurnDecision,
    ) -> InterruptResumeVerification:
        if self._v2_answer_upload_ids(raw_answer) or self._v2_answer_sheet_selections(raw_answer):
            return InterruptResumeVerification(allow_resume=True, confidence=1.0, reason="structured_attachment")
        prompt = json.dumps(
            {
                "mode": "interrupt_resume_verification",
                "instructions": [
                    "Return JSON only.",
                    "Act as a conservative state-transition verifier.",
                    "Set allow_resume=true only if the current user turn is clearly a final slot answer, not a question about the slot.",
                    "Set allow_resume=false for format questions, comparison/tradeoff questions, examples, how-to-decide questions, or low-confidence cases.",
                ],
                "output_schema": {
                    "allow_resume": True,
                    "confidence": 0.0,
                    "reason": "brief rationale",
                    "clarification_answer": "optional response if resume should be blocked",
                },
                "task": {"task_id": task.task_id, "summary": task.summary},
                "interrupt": {
                    "interrupt_id": interrupt.interrupt_id,
                    "question": interrupt.question,
                    "reason_code": interrupt.reason_code,
                },
                "current_user_answer": self._v2_answer_text(raw_answer),
                "understanding_decision": {
                    "intent": decision.intent,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                    "extracted_answer": dict(decision.extracted_answer or {}),
                },
                "slot_collection": self._slot_collection_prompt_payload(collection),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if self._skill_input_text_generator is None:
            return InterruptResumeVerification(allow_resume=True, confidence=decision.confidence, reason="verifier_disabled_fallback_to_heuristic_understanding")
        raw_response = await self._call_skill_input_text_generator(prompt, metadata=_INTERRUPT_TURN_LLM_METADATA)
        parsed = self._parse_interrupt_resume_verification(raw_response)
        if parsed is not None:
            return parsed
        return InterruptResumeVerification(
            allow_resume=False,
            confidence=1.0,
            reason="interrupt_resume_verifier_unavailable_or_invalid",
            clarification_answer=decision.clarification_answer,
        )

    @staticmethod
    def _parse_interrupt_resume_verification(raw_response: str) -> InterruptResumeVerification | None:
        if not raw_response.strip():
            return None
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("allow_resume"), bool):
            return None
        try:
            confidence = float(parsed.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        return InterruptResumeVerification(
            allow_resume=bool(parsed.get("allow_resume")),
            confidence=max(0.0, min(confidence, 1.0)),
            reason=str(parsed.get("reason") or ""),
            clarification_answer=str(parsed.get("clarification_answer") or ""),
        )

    async def _record_v2_interrupt_clarification(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        raw_answer: dict[str, object],
        client_request_id: str,
        decision: InterruptTurnDecision,
        idempotency_key: str,
        source_message_id: str | None = None,
    ) -> dict[str, object]:
        user_message = Message(
            message_id=source_message_id or self._make_id("msg"),
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content=self._format_v2_answer_message(raw_answer) or self._v2_answer_text(raw_answer),
            task_id=task.task_id,
            created_at=self._utcnow_naive(),
        )
        await self.storage.save_message(user_message)
        assistant_text = decision.clarification_answer.strip() or await self._generate_v2_interrupt_clarification_answer(
            task=task,
            interrupt=interrupt,
            collection=collection,
            user_text=self._v2_answer_text(raw_answer),
            decision=decision,
        )
        assistant_message = Message(
            message_id=self._make_id("msg"),
            conversation_id=task.conversation_id,
            role=MessageRole.ASSISTANT,
            content=assistant_text,
            task_id=task.task_id,
            created_at=self._utcnow_naive(),
        )
        await self.storage.save_message(assistant_message)
        await self.storage.append_slot_event(
            SlotEvent(
                slot_event_id=f"{collection.collection_id}:event:clarification:{client_request_id}",
                collection_id=collection.collection_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                conversation_id=collection.conversation_id,
                event_type="slot.clarification_answered",
                round=collection.round,
                revision=collection.revision,
                idempotency_key=idempotency_key,
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "client_request_id": client_request_id,
                    "intent": decision.intent,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                    "assistant_message": assistant_text,
                    "source_message_id": user_message.message_id,
                },
                created_at=self._utcnow_naive(),
            )
        )
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_clarification_answered",
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "slot_collection_id": collection.collection_id,
                    "client_request_id": client_request_id,
                },
            )
        )
        return {
            "interrupt_id": interrupt.interrupt_id,
            "status": str(interrupt.status),
            "node_id": interrupt.node_id,
            "answer_payload": {"client_request_id": client_request_id},
            "action": "clarification_answer",
            "assistant_message": assistant_text,
            "source_message_id": user_message.message_id,
        }

    async def _generate_v2_interrupt_clarification_answer(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        collection: SlotCollection,
        user_text: str,
        decision: InterruptTurnDecision,
    ) -> str:
        prompt = json.dumps(
            {
                "mode": "interrupt_clarification_answer",
                "instructions": [
                    "Answer the user's question without closing or resuming the interrupt.",
                    "Use only the provided interrupt, slot schema, resources, and current slot state.",
                    "If the available context is insufficient, say what is known and what remains uncertain.",
                    "Keep the answer concise and useful for choosing or filling the missing data.",
                ],
                "task": {"task_id": task.task_id, "summary": task.summary},
                "interrupt": {
                    "question": interrupt.question,
                    "reason_code": interrupt.reason_code,
                },
                "user_question": user_text,
                "understanding": {
                    "intent": decision.intent,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                },
                "slot_collection": self._slot_collection_prompt_payload(collection),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        raw_response = await self._call_skill_input_text_generator(prompt, metadata=_INTERRUPT_TURN_LLM_METADATA)
        answer = self._parse_clarification_answer(raw_response)
        if answer:
            return answer
        missing_labels = []
        for field in collection.missing:
            slot = collection.slots.get(field) if isinstance(collection.slots, Mapping) else None
            if isinstance(slot, Mapping):
                missing_labels.append(str(slot.get("label") or slot.get("description") or field))
            else:
                missing_labels.append(str(field))
        target = "、".join(missing_labels) if missing_labels else "当前缺失参数"
        return f"当前任务还在等待补充：{target}。你可以继续询问格式、示例或不同选项的区别；确认后直接回复要采用的值即可。"

    @staticmethod
    def _parse_clarification_answer(raw_response: str) -> str:
        stripped = str(raw_response or "").strip()
        if not stripped:
            return ""
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        if isinstance(parsed, Mapping):
            for key in ("answer", "clarification_answer", "message", "content"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return stripped

    async def _call_skill_input_text_generator(self, prompt: str, **kwargs: Any) -> str:
        if self._skill_input_text_generator is None:
            return ""
        try:
            generated = self._skill_input_text_generator(prompt, **kwargs)
            if inspect.isawaitable(generated):
                generated = await generated
            return str(generated or "")
        except Exception:
            return ""

    @staticmethod
    def _slot_collection_prompt_payload(collection: SlotCollection) -> dict[str, object]:
        return {
            "collection_id": collection.collection_id,
            "kind": collection.kind,
            "status": collection.status,
            "round": collection.round,
            "revision": collection.revision,
            "selected_schema_id": collection.selected_schema_id,
            "selected_entrypoint": collection.selected_entrypoint,
            "schema_snapshot": dict(collection.schema_snapshot),
            "slots": dict(collection.slots),
            "resolved": dict(collection.resolved),
            "missing": list(collection.missing),
            "invalid": [dict(item) for item in collection.invalid],
            "last_question": collection.last_question,
        }

    async def _apply_v2_slot_answer(
        self,
        *,
        collection: SlotCollection,
        interrupt: Interrupt,
        raw_answer: dict[str, object],
        client_request_id: str,
    ) -> SlotCollection:
        if collection.status in _SLOT_TERMINAL_STATUSES or collection.status == "script_scheduled":
            return collection
        answer_key = f"answer:{interrupt.interrupt_id}:{client_request_id}"
        if collection.kind == "schema_selection":
            return await self._apply_v2_schema_selection_answer(
                collection=collection,
                raw_answer=raw_answer,
                answer_key=answer_key,
            )
        schema = self._schema_for_slot_collection(collection)
        if schema is None:
            return await self._fail_v2_slot_collection(
                collection,
                reason="schema_snapshot_unavailable",
                idempotency_key=f"{answer_key}:schema_missing",
            )

        extracting = collection
        if extracting.status == "waiting_for_user":
            extracting, _ = await self._transition_v2_slot_collection(
                extracting,
                to_status="extracting",
                event_type="slot.extraction_started",
                payload={"client_request_id": client_request_id, "mode": "history_recall" if should_trigger_history_recall(self._v2_answer_text(raw_answer)) else "normal"},
                idempotency_key=f"{answer_key}:extracting",
            )
        if extracting.status == "extracting":
            validating, _ = await self._transition_v2_slot_collection(
                extracting,
                to_status="validating",
                event_type="slot.validation_started",
                payload={"client_request_id": client_request_id},
                idempotency_key=f"{answer_key}:validating",
            )
        else:
            validating = extracting
        if validating.status != "validating":
            return validating

        answer_text = self._v2_answer_text(raw_answer)
        history_recall = should_trigger_history_recall(answer_text)
        artifact_summaries = tuple(await self._task_input_attachment_prompt_summaries(collection.task_id))
        accepted_answer_summaries = tuple(
            await self._v2_accepted_answer_summaries(
                validating.task_id,
                exclude_client_request_id=client_request_id,
            )
        )
        prompt = (
            build_history_recall_prompt(
                validating,
                current_user_answer=answer_text,
                accepted_answer_summaries=accepted_answer_summaries,
            )
            if history_recall
            else build_normal_extraction_prompt(
                validating,
                current_user_answer=answer_text,
                artifact_summaries=artifact_summaries,
            )
        )
        raw_response = ""
        if self._skill_input_text_generator is not None:
            try:
                generated = self._skill_input_text_generator(prompt)
                if inspect.isawaitable(generated):
                    generated = await generated
                raw_response = str(generated or "")
            except Exception:
                raw_response = ""
        extraction = parse_slot_extraction_response(raw_response, validating) if raw_response else parse_slot_extraction_response("{}", validating)
        backend_extraction = build_backend_slot_extraction(
            validating,
            schema,
            current_user_answer=answer_text,
            current_upload_ids=self._v2_answer_upload_ids(raw_answer),
            artifact_summaries=artifact_summaries,
            accepted_answer_summaries=accepted_answer_summaries,
            history_recall=history_recall,
        )
        extraction = merge_slot_extraction_results(extraction, backend_extraction, collection=validating)
        if not extraction.resolved:
            extraction = self._fallback_v2_slot_extraction(
                validating,
                schema,
                raw_answer=raw_answer,
            )
        next_collection, event = apply_extraction_result_to_collection(
            validating,
            schema,
            extraction,
            now=self._utcnow_naive(),
        )
        if next_collection.status == "waiting_for_user":
            next_collection = replace(
                next_collection,
                round=validating.round + 1,
                last_question=self._slot_question_from_collection(next_collection),
            )
        event = replace(event, idempotency_key=answer_key)
        saved = await self.storage.apply_slot_transition(
            validating.collection_id,
            validating.revision,
            next_collection,
            event,
            idempotency_key=answer_key,
        )
        return saved or await self.storage.get_slot_collection(validating.collection_id) or validating

    async def _apply_v2_schema_selection_answer(
        self,
        *,
        collection: SlotCollection,
        raw_answer: Mapping[str, object],
        answer_key: str,
    ) -> SlotCollection:
        manifest = self._manifest_for_slot_collection(collection)
        if manifest is None or manifest.contract is None:
            return await self._fail_v2_slot_collection(
                collection,
                reason="manifest_unavailable_for_schema_selection",
                idempotency_key=f"{answer_key}:manifest_missing",
            )
        schemas = load_input_schemas_for_contract(manifest.contract)
        selection = select_input_schema(
            manifest.contract,
            schemas,
            query=self._v2_answer_text(raw_answer),
            payload={},
            metadata={},
            artifact_summaries=(),
            llm_text_generator=self._skill_input_text_generator,
        )
        if not selection.selected:
            extracting, _ = await self._transition_v2_slot_collection(
                collection,
                to_status="extracting",
                event_type="slot.schema_selection_started",
                payload={"selected": False},
                idempotency_key=f"{answer_key}:schema_selecting",
            )
            retry = replace(
                extracting,
                status="waiting_for_user",
                revision=extracting.revision + 1,
                round=extracting.round + 1,
                last_question=extracting.last_question or "请确认要使用哪一种输入模式。",
                updated_at=self._utcnow_naive(),
            )
            event = SlotEvent(
                slot_event_id=f"{collection.collection_id}:event:schema_selection_failed:{retry.round}",
                collection_id=collection.collection_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                conversation_id=collection.conversation_id,
                event_type="slot.validation_failed",
                round=retry.round,
                revision=retry.revision,
                idempotency_key=answer_key,
                payload={"missing": list(retry.missing), "reason": "schema_not_selected"},
                created_at=self._utcnow_naive(),
            )
            saved = await self.storage.apply_slot_transition(
                extracting.collection_id,
                extracting.revision,
                retry,
                event,
                idempotency_key=answer_key,
            )
            return saved or await self.storage.get_slot_collection(collection.collection_id) or collection

        extracting, _ = await self._transition_v2_slot_collection(
            collection,
            to_status="extracting",
            event_type="slot.schema_selection_started",
            payload={"selected": True},
            idempotency_key=f"{answer_key}:schema_selecting",
        )
        schema = schemas[selection.selected_schema_id]
        initialized, _ = initialize_input_collection(
            collection_id=collection.collection_id,
            task_id=collection.task_id,
            node_id=collection.node_id,
            conversation_id=collection.conversation_id,
            capability_id=collection.capability_id,
            skill_name=collection.skill_name,
            schema=schema,
            selected_entrypoint=selection.selected_entrypoint,
            now=self._utcnow_naive(),
            skill_bundle_revision=collection.skill_bundle_revision,
            contract_revision=collection.contract_revision,
            schema_digest=None,
            resources=manifest.contract.resources,
        )
        selected_schema_digest = self._slot_schema_digest(initialized.schema_snapshot)
        selector_field = manifest.contract.schema_selector.selector_field or "design"
        resolved = dict(collection.resolved)
        selector_schema_field = schema.inputs.get(selector_field)
        if selector_schema_field is not None and selector_schema_field.const is not None:
            resolved[selector_field] = {
                "raw_value": self._v2_answer_text(raw_answer),
                "value": selector_schema_field.const,
                "source": "schema_selection",
            }
        selected_base = replace(
            initialized,
            status="validating",
            revision=extracting.revision,
            round=collection.round + 1,
            schema_digest=selected_schema_digest,
            resolved=resolved,
            updated_at=self._utcnow_naive(),
        )
        answer_text = self._v2_answer_text(raw_answer)
        history_recall = should_trigger_history_recall(answer_text)
        artifact_summaries = tuple(await self._task_input_attachment_prompt_summaries(collection.task_id))
        backend_extraction = build_backend_slot_extraction(
            selected_base,
            schema,
            current_user_answer=answer_text,
            current_upload_ids=self._v2_answer_upload_ids(raw_answer),
            artifact_summaries=artifact_summaries,
            accepted_answer_summaries=tuple(await self._v2_accepted_answer_summaries(collection.task_id)),
            history_recall=history_recall,
        )
        selected, applied_event = apply_extraction_result_to_collection(
            selected_base,
            schema,
            backend_extraction,
            now=self._utcnow_naive(),
        )
        if selected.status == "waiting_for_user":
            selected = replace(
                selected,
                last_question=self._slot_question_from_collection(selected),
            )
        event = replace(
            applied_event,
            event_type="slot.schema_selected",
            idempotency_key=answer_key,
            payload={
                "selected_schema_id": selection.selected_schema_id,
                "selected_entrypoint": selection.selected_entrypoint,
                "missing": list(selected.missing),
                "resolved_fields": sorted(backend_extraction.resolved),
                "diagnostics": list(backend_extraction.diagnostics),
            },
        )
        saved = await self.storage.apply_slot_transition(
            extracting.collection_id,
            extracting.revision,
            selected,
            event,
            idempotency_key=answer_key,
        )
        return saved or await self.storage.get_slot_collection(collection.collection_id) or collection

    async def _transition_v2_slot_collection(
        self,
        collection: SlotCollection,
        *,
        to_status: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[SlotCollection, bool]:
        if idempotency_key and await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, idempotency_key) is not None:
            current = await self.storage.get_slot_collection(collection.collection_id)
            return current or collection, False
        next_collection, event = transition_slot_collection(
            collection,
            to_status=to_status,
            event_type=event_type,
            payload=payload or {},
            idempotency_key=idempotency_key,
            now=self._utcnow_naive(),
        )
        saved = await self.storage.apply_slot_transition(
            collection.collection_id,
            collection.revision,
            next_collection,
            event,
            idempotency_key=idempotency_key,
        )
        return saved or await self.storage.get_slot_collection(collection.collection_id) or collection, saved is not None

    async def _fail_v2_slot_collection(
        self,
        collection: SlotCollection,
        *,
        reason: str,
        idempotency_key: str,
    ) -> SlotCollection:
        if collection.status in _SLOT_TERMINAL_STATUSES:
            return collection
        try:
            failed, event = transition_slot_collection(
                collection,
                to_status="failed",
                event_type="slot.collection_failed",
                payload={"reason": reason},
                idempotency_key=idempotency_key,
                now=self._utcnow_naive(),
            )
        except Exception:
            failed = replace(collection, status="failed", failed_at=self._utcnow_naive(), updated_at=self._utcnow_naive(), revision=collection.revision + 1)
            event = SlotEvent(
                slot_event_id=f"{collection.collection_id}:event:failed:{int(self._utcnow_naive().timestamp() * 1_000_000)}",
                collection_id=collection.collection_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                conversation_id=collection.conversation_id,
                event_type="slot.collection_failed",
                round=collection.round,
                revision=failed.revision,
                idempotency_key=idempotency_key,
                payload={"reason": reason, "forced": True},
                created_at=self._utcnow_naive(),
            )
        saved = await self.storage.apply_slot_transition(
            collection.collection_id,
            collection.revision,
            failed,
            event,
            idempotency_key=idempotency_key,
        )
        return saved or await self.storage.get_slot_collection(collection.collection_id) or collection

    async def _mark_v2_slot_script_scheduled(self, collection: SlotCollection) -> tuple[SlotCollection, bool]:
        script_key = self._v2_slot_script_scheduled_key(collection)
        existing = await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, script_key)
        if existing is not None:
            current = await self.storage.get_slot_collection(collection.collection_id)
            return current or collection, False
        try:
            next_collection, event = transition_slot_collection(
                collection,
                to_status="script_scheduled",
                event_type="slot.script_scheduled",
                payload={"selected_entrypoint": collection.selected_entrypoint},
                idempotency_key=script_key,
                now=self._utcnow_naive(),
            )
        except Exception:
            return collection, False
        saved = await self.storage.apply_slot_transition(
            collection.collection_id,
            collection.revision,
            next_collection,
            event,
            idempotency_key=script_key,
        )
        if saved is not None:
            return saved, True
        return await self.storage.get_slot_collection(collection.collection_id) or collection, False

    @staticmethod
    def _v2_slot_script_scheduled_key(collection: SlotCollection) -> str:
        return f"slot:{collection.collection_id}:script_scheduled:{collection.revision}"

    def _manifest_for_slot_collection(self, collection: SlotCollection):
        if self._skill_runtime_state is None:
            return None
        try:
            catalog = self._skill_runtime_state.catalog_for_revision(collection.skill_bundle_revision)
        except Exception:
            catalog = self._skill_runtime_state.active_bundle.catalog
        return catalog.get(collection.skill_name)

    def _schema_for_slot_collection(self, collection: SlotCollection):
        if not collection.selected_schema_id:
            return None
        try:
            schema = schema_from_snapshot(collection.schema_snapshot)
        except Exception:
            return None
        if schema.schema_id != collection.selected_schema_id:
            return None
        if collection.schema_digest and self._slot_schema_digest(collection.schema_snapshot) != collection.schema_digest:
            return None
        return schema

    @staticmethod
    def _slot_schema_digest(schema_snapshot: Mapping[str, object]) -> str:
        encoded = json.dumps(dict(schema_snapshot), sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def _slot_question_from_collection(self, collection: SlotCollection) -> str:
        return self._slot_question_from_parts(slots=collection.slots, missing=collection.missing, invalid=collection.invalid)

    @staticmethod
    def _slot_question_from_parts(
        *,
        slots: Mapping[str, object],
        missing: Iterable[str],
        invalid: Iterable[Mapping[str, object]],
    ) -> str:
        labels: list[str] = []
        for field in missing:
            slot = slots.get(field) if isinstance(slots, Mapping) else None
            if isinstance(slot, Mapping):
                labels.append(str(slot.get("label") or slot.get("description") or field))
            else:
                labels.append(str(field))
        invalid_fields = [str(item.get("field")) for item in invalid if isinstance(item, Mapping) and item.get("field")]
        if invalid_fields and labels:
            return f"刚才的 {', '.join(invalid_fields)} 无法通过校验，请重新补充：{'、'.join(labels)}。"
        return f"请补充：{'、'.join(labels)}。" if labels else "请补充缺失参数。"

    async def _task_input_attachment_prompt_summaries(self, task_id: str) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        for attachment in await self.storage.list_task_input_attachments_for_task(task_id):
            prompt_artifact = attachment.prompt_artifact if isinstance(attachment.prompt_artifact, Mapping) else {}
            preview = prompt_artifact.get("preview") if isinstance(prompt_artifact.get("preview"), Mapping) else {}
            summaries.append(
                {
                    "upload_id": attachment.source_upload_id,
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "file_type": attachment.file_type,
                    "size_bytes": attachment.size_bytes,
                    "sha256": attachment.sha256,
                    "selected_sheet": attachment.selected_sheet,
                    "columns": list(preview.get("columns") or ()) if isinstance(preview.get("columns"), list | tuple) else None,
                    "row_count": preview.get("row_count"),
                    "column_count": preview.get("column_count"),
                    "source_kind": attachment.source_kind,
                    "source_message_id": attachment.source_message_id,
                    "interrupt_answer_id": attachment.interrupt_answer_id,
                    "created_at": attachment.created_at.isoformat() if attachment.created_at is not None else None,
                }
            )
        return summaries

    async def _v2_accepted_answer_summaries(
        self,
        task_id: str,
        *,
        exclude_client_request_id: str | None = None,
    ) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        for interrupt in await self.storage.list_interrupts_for_task(task_id):
            for answer in await self.storage.list_interrupt_answers(interrupt.interrupt_id):
                if not answer.accepted:
                    continue
                payload = answer.answer_payload if isinstance(answer.answer_payload, Mapping) else {}
                client_request_id = str(payload.get("client_request_id") or "").strip()
                if exclude_client_request_id and client_request_id == exclude_client_request_id:
                    continue
                raw_answer = payload.get("answer")
                if not isinstance(raw_answer, Mapping):
                    continue
                summary: dict[str, object] = {
                    "interrupt_id": interrupt.interrupt_id,
                    "client_request_id": client_request_id,
                }
                text = self._v2_answer_text(raw_answer)
                if text:
                    summary["text"] = text
                upload_ids = self._v2_answer_upload_ids(raw_answer)
                if upload_ids:
                    summary["upload_ids"] = list(upload_ids)
                sheet_selections = self._v2_answer_sheet_selections(raw_answer)
                if sheet_selections:
                    summary["sheet_selections"] = sheet_selections
                if answer.accepted_at is not None:
                    summary["accepted_at"] = answer.accepted_at.isoformat()
                summaries.append(summary)
        return summaries

    def _fallback_v2_slot_candidates(
        self,
        collection: SlotCollection,
        schema,
        *,
        raw_answer: Mapping[str, object],
    ) -> dict[str, dict[str, object]]:
        extraction = build_backend_slot_extraction(
            collection,
            schema,
            current_user_answer=self._v2_answer_text(raw_answer),
            current_upload_ids=self._v2_answer_upload_ids(raw_answer),
        )
        return {
            field: {
                "raw_value": candidate.raw_value,
                "value": candidate.value,
                "source": candidate.source,
            }
            for field, candidate in extraction.resolved.items()
        }

    def _fallback_v2_slot_extraction(
        self,
        collection: SlotCollection,
        schema,
        *,
        raw_answer: Mapping[str, object],
    ) -> SlotExtractionResult:
        candidates = {
            field: SlotExtractionCandidate(
                field=field,
                raw_value=dict(candidate["raw_value"]) if isinstance(candidate.get("raw_value"), Mapping) else candidate.get("raw_value"),
                value=dict(candidate["value"]) if isinstance(candidate.get("value"), Mapping) else candidate.get("value"),
                source=str(candidate.get("source") or "current_answer"),
            )
            for field, candidate in self._fallback_v2_slot_candidates(collection, schema, raw_answer=raw_answer).items()
        }
        return SlotExtractionResult(resolved=candidates)

    @staticmethod
    def _slot_collection_resume_metadata(collection: SlotCollection) -> dict[str, object]:
        return {
            "schema_version": SLOT_COLLECTION_V2_SCHEMA_VERSION,
            "collection_id": collection.collection_id,
            "task_id": collection.task_id,
            "node_id": collection.node_id,
            "kind": collection.kind,
            "status": collection.status,
            "round": collection.round,
            "revision": collection.revision,
            "selected_schema_id": collection.selected_schema_id,
            "selected_entrypoint": collection.selected_entrypoint,
            "schema_snapshot": dict(collection.schema_snapshot),
            "slots": dict(collection.slots),
            "resolved": dict(collection.resolved),
            "missing": list(collection.missing),
            "invalid": [dict(item) for item in collection.invalid],
            "last_question": collection.last_question,
        }

    @staticmethod
    def _v2_answer_text(answer: Mapping[str, object]) -> str:
        value = answer.get("text")
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _v2_answer_upload_ids(answer: Mapping[str, object]) -> tuple[str, ...]:
        raw = answer.get("upload_ids")
        if raw is None:
            return ()
        if isinstance(raw, str):
            values = (raw,)
        elif isinstance(raw, list | tuple):
            values = raw
        else:
            raise UploadValidationError("answer.upload_ids must be a list")
        return tuple(str(value).strip() for value in values if str(value).strip())

    @staticmethod
    def _v2_answer_sheet_selections(answer: Mapping[str, object]) -> dict[str, str]:
        raw = answer.get("sheet_selections") or answer.get("upload_sheet_selections")
        if raw in (None, ""):
            return {}
        if not isinstance(raw, Mapping):
            raise UploadValidationError("answer.sheet_selections must be an object")
        return {str(key).strip(): str(value).strip() for key, value in raw.items() if str(key).strip() and str(value).strip()}

    @classmethod
    def _format_v2_answer_message(cls, answer: Mapping[str, object]) -> str:
        text = cls._v2_answer_text(answer)
        upload_ids = cls._v2_answer_upload_ids(answer)
        sheet_selections = cls._v2_answer_sheet_selections(answer)
        parts: list[str] = []
        if text:
            parts.append(text)
        if upload_ids:
            parts.append("已上传补充文件")
        if sheet_selections:
            parts.append("已选择工作表")
        return "；".join(parts)

    @classmethod
    def _combine_v2_resume_message(cls, root_content: str, answer: Mapping[str, object]) -> str:
        answer_text = cls._format_v2_answer_message(answer)
        if not answer_text:
            return root_content
        return f"{root_content}\n补充信息：{answer_text}"

    async def _bind_task_input_uploads(
        self,
        *,
        task: Task,
        username: str,
        upload_ids: Any,
        source_kind: str,
        source_message_id: str | None = None,
        interrupt_answer_id: str | None = None,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> tuple[TaskInputAttachment, ...]:
        sheet_selections = self._normalize_upload_sheet_selections(upload_sheet_selections)
        saved: list[TaskInputAttachment] = []
        for upload_id in self._normalize_upload_ids(upload_ids):
            record = self.upload_store.get_for_message(
                upload_id=upload_id,
                username=username,
                conversation_id=task.conversation_id,
            )
            attachment = self._attachment_from_upload_record(
                task=task,
                record=record,
                source_kind=source_kind,
                source_message_id=source_message_id,
                interrupt_answer_id=interrupt_answer_id,
                selected_sheet=sheet_selections.get(upload_id),
            )
            saved.append(await self.storage.save_task_input_attachment(attachment))
        return tuple(saved)

    async def _bind_or_update_resume_input_attachments(
        self,
        *,
        task: Task,
        username: str,
        upload_ids: Any,
        source_kind: str,
        source_message_id: str | None,
        interrupt_answer_id: str | None,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> None:
        sheet_selections = self._normalize_upload_sheet_selections(upload_sheet_selections)
        existing = {
            str(attachment.source_upload_id): attachment
            for attachment in await self.storage.list_task_input_attachments_for_task(task.task_id)
            if attachment.source_upload_id
        }
        missing_upload_ids: list[str] = []
        for upload_id in self._normalize_upload_ids(upload_ids):
            selected_sheet = sheet_selections.get(upload_id)
            existing_attachment = existing.get(upload_id)
            if existing_attachment is not None:
                if selected_sheet:
                    await self._update_task_input_attachment_sheet_selection(
                        existing_attachment,
                        selected_sheet=selected_sheet,
                        interrupt_answer_id=interrupt_answer_id,
                    )
                continue
            try:
                record = self.upload_store.get_for_message(
                    upload_id=upload_id,
                    username=username,
                    conversation_id=task.conversation_id,
                )
            except UploadValidationError:
                missing_upload_ids.append(upload_id)
                continue
            if record.requires_sheet_selection and not selected_sheet:
                raise UploadValidationError("Spreadsheet sheet selection is required before resuming the task")
            attachment = self._attachment_from_upload_record(
                task=task,
                record=record,
                source_kind=source_kind,
                source_message_id=source_message_id,
                interrupt_answer_id=interrupt_answer_id,
                selected_sheet=selected_sheet,
            )
            await self.storage.save_task_input_attachment(attachment)
        self._raise_missing_uploads(missing_upload_ids, context="interrupt resume")

    def _attachment_from_upload_record(
        self,
        *,
        task: Task,
        record: UploadedFileRecord,
        source_kind: str,
        source_message_id: str | None,
        interrupt_answer_id: str | None,
        selected_sheet: str | None,
    ) -> TaskInputAttachment:
        now = self._utcnow_naive()
        skill_artifact = record.to_skill_artifact(selected_sheet=selected_sheet)
        prompt_artifact = self._prompt_artifact_from_skill_artifact(skill_artifact)
        return TaskInputAttachment(
            attachment_id=self._task_input_attachment_id(task.task_id, record.upload_id),
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            source_kind=source_kind,
            source_upload_id=record.upload_id,
            source_message_id=source_message_id,
            interrupt_answer_id=interrupt_answer_id,
            filename=record.filename,
            content_type=record.content_type,
            file_type=record.file_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            prompt_artifact=prompt_artifact,
            skill_artifact=skill_artifact,
            source_payload=self._source_payload_from_upload_record(record),
            selected_sheet=selected_sheet or record.selected_sheet,
            created_at=record.created_at,
            updated_at=now,
        )

    async def _update_task_input_attachment_sheet_selection(
        self,
        attachment: TaskInputAttachment,
        *,
        selected_sheet: str,
        interrupt_answer_id: str | None = None,
    ) -> TaskInputAttachment:
        if attachment.file_type != "spreadsheet":
            return attachment
        source_payload = dict(attachment.source_payload)
        raw_base64 = source_payload.get("content_base64")
        if not isinstance(raw_base64, str) or not raw_base64:
            raise UploadValidationError(
                f"Task-bound spreadsheet upload is missing stored content for interrupt resume: {attachment.source_upload_id}"
            )
        try:
            content = base64.b64decode(raw_base64.encode("ascii"), validate=True)
        except Exception as exc:
            raise UploadValidationError(
                f"Task-bound spreadsheet upload content is invalid for interrupt resume: {attachment.source_upload_id}"
            ) from exc
        normalized = normalize_selected_spreadsheet_sheet(
            filename=attachment.filename,
            content_type=attachment.content_type,
            content=content,
            selected_sheet=selected_sheet,
        )
        if normalized.normalized_content_text is None:
            raise UploadValidationError(
                f"Selected spreadsheet sheet could not be normalized for interrupt resume: {attachment.source_upload_id}"
            )
        prompt_artifact = dict(attachment.prompt_artifact)
        prompt_artifact["preview"] = dict(normalized.preview)
        prompt_artifact["selected_sheet"] = normalized.selected_sheet or selected_sheet
        prompt_artifact["normalized_filename"] = normalized.normalized_filename or attachment.filename
        prompt_artifact["normalized_content_type"] = normalized.normalized_content_type
        prompt_artifact.pop("requires_sheet_selection", None)
        skill_artifact = dict(prompt_artifact)
        skill_artifact.update(
            {
                "content": normalized.normalized_content_text,
                "original_filename": attachment.filename,
                "filename": normalized.normalized_filename or attachment.filename,
                "normalized_filename": normalized.normalized_filename or attachment.filename,
                "content_type": normalized.normalized_content_type or attachment.content_type,
                "normalized_content_type": normalized.normalized_content_type,
                "selected_sheet": normalized.selected_sheet or selected_sheet,
            }
        )
        updated = replace(
            attachment,
            prompt_artifact=self._prompt_artifact_from_skill_artifact(skill_artifact),
            skill_artifact=skill_artifact,
            selected_sheet=normalized.selected_sheet or selected_sheet,
            interrupt_answer_id=interrupt_answer_id or attachment.interrupt_answer_id,
            updated_at=self._utcnow_naive(),
        )
        return await self.storage.save_task_input_attachment(updated)

    async def _task_input_attachment_metadata(self, task_id: str) -> dict[str, Any]:
        attachments = await self.storage.list_task_input_attachments_for_task(task_id)
        if not attachments:
            return {}
        return {
            "uploaded_artifacts": [
                dict(attachment.prompt_artifact)
                for attachment in attachments
                if isinstance(attachment.prompt_artifact, Mapping) and attachment.prompt_artifact
            ],
            "skill_artifacts": [
                dict(attachment.skill_artifact)
                for attachment in attachments
                if isinstance(attachment.skill_artifact, Mapping) and attachment.skill_artifact
            ],
        }

    @staticmethod
    def _task_input_attachment_id(task_id: str, upload_id: str) -> str:
        return f"{task_id}:input:{upload_id}"

    @staticmethod
    def _prompt_artifact_from_skill_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
        prompt_artifact = dict(artifact)
        for raw_key in ("content", "content_base64", "encoding"):
            prompt_artifact.pop(raw_key, None)
        return prompt_artifact

    @staticmethod
    def _source_payload_from_upload_record(record: UploadedFileRecord) -> dict[str, Any]:
        return {
            "encoding": "base64",
            "content_base64": base64.b64encode(record.content_bytes).decode("ascii"),
            "filename": record.filename,
            "content_type": record.content_type,
            "file_type": record.file_type,
            "normalized_filename": record.normalized_filename,
            "normalized_content_type": record.normalized_content_type,
        }

    @staticmethod
    def _normalize_upload_ids(upload_ids: Any) -> tuple[str, ...]:
        if upload_ids is None:
            return ()
        if isinstance(upload_ids, str):
            values = [upload_ids]
        elif isinstance(upload_ids, list | tuple):
            values = upload_ids
        else:
            raise UploadValidationError("upload_ids must be a list")
        return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

    def _validate_sheet_selection_answer(self, interrupt: Interrupt, answer_payload: Mapping[str, object]) -> None:
        fields = interrupt.required_fields.get("upload_sheet_selections")
        if not isinstance(fields, Mapping):
            raise UploadValidationError("sheet selection interrupt is malformed")
        raw_selections = answer_payload.get("upload_sheet_selections")
        selections = self._normalize_upload_sheet_selections(raw_selections)
        required_upload_ids = [str(item) for item in fields.get("required_upload_ids", []) if str(item).strip()]
        options_by_upload_id = {
            str(upload_id): [str(option) for option in options]
            for upload_id, options in dict(fields.get("options_by_upload_id", {})).items()
            if isinstance(options, list | tuple)
        }
        missing = [upload_id for upload_id in required_upload_ids if upload_id not in selections]
        if missing:
            raise UploadValidationError(f"Missing spreadsheet sheet selection for uploads: {', '.join(missing)}")
        invalid: list[str] = []
        for upload_id, sheet_name in selections.items():
            if upload_id not in required_upload_ids or sheet_name not in options_by_upload_id.get(upload_id, ()):
                invalid.append(upload_id)
        if invalid:
            raise UploadValidationError(f"Invalid spreadsheet sheet selection for uploads: {', '.join(invalid)}")

    async def iter_frontend_events(self, task_id: str):
        task = await self.storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")

        yielded_event_ids: set[str] = set()
        terminal_event_types = {"task.completed", "task.failed", "task.cancelled"}
        terminal_event_seen = False
        subscription = self.event_broker.subscribe(task_id)
        try:
            async for event in self._iter_event_replay_pages(task_id):
                if is_frontend_event(event):
                    yielded_event_ids.add(event.event_id)
                    terminal_event_seen = terminal_event_seen or event.event_type in terminal_event_types
                    yield event

            if terminal_event_seen:
                return

            latest_task = await self.storage.get_task(task_id)
            if latest_task is not None and latest_task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                deadline = asyncio.get_running_loop().time() + 0.25
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        event = await asyncio.wait_for(subscription.get(), timeout=0.01)
                    except asyncio.TimeoutError:
                        async for event in self._iter_event_replay_pages(task_id):
                            if event.event_id in yielded_event_ids:
                                continue
                            if not is_frontend_event(event):
                                continue
                            yielded_event_ids.add(event.event_id)
                            terminal_event_seen = terminal_event_seen or event.event_type in terminal_event_types
                            yield event
                        if terminal_event_seen:
                            return
                        continue
                    if event.event_id in yielded_event_ids:
                        continue
                    if is_frontend_event(event):
                        yielded_event_ids.add(event.event_id)
                        yield event
                    if event.event_type in terminal_event_types:
                        return
                return

            while True:
                event = await subscription.get()
                if event.event_id in yielded_event_ids:
                    continue
                if is_frontend_event(event):
                    yielded_event_ids.add(event.event_id)
                    yield event
                if event.event_type in terminal_event_types:
                    return
        finally:
            subscription.close()


    async def start(self) -> None:
        if self.postgres_auth_invalidation_bus is not None:
            await self.postgres_auth_invalidation_bus.start()
        await self.recover_deleting_conversations()

    async def recover_deleting_conversations(self) -> None:
        for conversation in await self.storage.list_deleting_conversations():
            if conversation.status != ConversationStatus.DELETING:
                continue
            if conversation.conversation_id in self._conversation_delete_tasks:
                continue
            runner_id = conversation.delete_runner_id or self._make_id("delete")
            task = asyncio.create_task(
                self._run_conversation_delete(conversation, runner_id),
                name=f"delete-conversation-recovery:{conversation.conversation_id}",
            )
            self._track_conversation_delete_task(conversation.conversation_id, task)

    async def shutdown(self) -> None:
        pending = [*self._running_tasks.values(), *self._running_title_tasks, *self._conversation_delete_tasks.values()]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)
            except asyncio.TimeoutError:
                for handle in pending:
                    handle.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._mysql_adapter is not None:
            await self._mysql_adapter.aclose()
        if self._mcp_runtime_state is not None:
            await self._mcp_runtime_state.aclose()
        if self.postgres_auth_invalidation_bus is not None:
            await self.postgres_auth_invalidation_bus.aclose()
        await asyncio.to_thread(self._engine.dispose)

    @staticmethod
    def _canonical_capability_id(capability_id: str | None) -> str | None:
        if capability_id == "main_agent":
            return "main_agent.respond"
        return capability_id

    def _ensure_supported_capability(self, capability_id: str | None) -> None:
        if capability_id is None:
            return
        descriptor = self.capability_registry.get(capability_id)
        if descriptor is not None and descriptor.public:
            return
        raise ValueError(f"Unsupported capability_id: {capability_id}")

    async def _refresh_skills_for_new_conversation_if_needed(
        self,
        conversation_id: str,
        existing_conversation: Conversation | None,
    ) -> None:
        if self._skill_runtime_state is None:
            return
        if existing_conversation is not None:
            tasks = await self.storage.list_tasks_for_conversation(conversation_id)
            if tasks:
                return
        async with self._skill_refresh_lock:
            if existing_conversation is not None:
                tasks = await self.storage.list_tasks_for_conversation(conversation_id)
                if tasks:
                    return
            self._record_skill_refresh_started("conversation_start")
            previous_revision = self._skill_runtime_state.active_revision
            self._skill_runtime_state.retain_revision(previous_revision)
            try:
                result = self._skill_runtime_state.refresh_if_changed(reason="conversation_start")
                if result.status == "completed":
                    try:
                        self._sync_skill_capability_registry()
                    except Exception:
                        self._skill_runtime_state.activate_revision(previous_revision)
                        raise
                    if self._audit_sink is not None:
                        _record_skill_capability_startup_audit(
                            self._audit_sink,
                            self._skill_runtime_state.active_bundle.skill_capabilities,
                        )
                self._record_skill_refresh_audit(result)
            finally:
                self._skill_runtime_state.release_revision(previous_revision)

    def _sync_skill_capability_registry(self) -> None:
        if self._skill_runtime_state is None:
            return
        _sync_skill_capability_registry(self.capability_registry, self.instance_registry, self._skill_runtime_state)

    async def _refresh_mcp_for_new_conversation_if_needed(
        self,
        conversation_id: str,
        existing_conversation: Conversation | None,
    ) -> None:
        if self._mcp_runtime_state is None or not self._mcp_runtime_state.config.refreshes_on_conversation_start():
            return
        if existing_conversation is not None:
            tasks = await self.storage.list_tasks_for_conversation(conversation_id)
            if tasks:
                return
        async with self._mcp_refresh_lock:
            if existing_conversation is not None:
                tasks = await self.storage.list_tasks_for_conversation(conversation_id)
                if tasks:
                    return
            self._record_mcp_refresh_started("conversation_start")
            pending = await self._mcp_runtime_state.prepare_refresh(reason="conversation_start", force=True)
            result = pending.result
            if result.status == "completed":
                try:
                    self._sync_mcp_capability_registry(pending.bundle)
                except Exception:
                    await self._mcp_runtime_state.discard_activation(pending)
                    raise
                await self._mcp_runtime_state.commit_activation(pending)
            self._record_mcp_refresh_audit(result)

    def _sync_mcp_capability_registry(self, bundle: MCPRuntimeBundle) -> None:
        if self._mcp_runtime_state is None:
            return
        _sync_mcp_capability_registry(self.capability_registry, self.instance_registry, bundle)

    def _record_skill_refresh_audit(self, result: SkillRuntimeRefreshResult) -> None:
        if self._audit_sink is None:
            return
        payload = {
            "reason": result.reason,
            "previous_revision": result.previous_revision,
            "active_revision": result.active_revision,
            "registered_count": result.registered_count,
            "skipped_count": result.skipped_count,
            "duration_ms": result.duration_ms,
            "script_package_snapshot": result.script_package_snapshot,
        }
        if result.status == "completed":
            self._audit_sink.record_sync("skill.bundle_refresh_completed", payload)
        elif result.status == "skipped":
            self._audit_sink.record_sync("skill.bundle_refresh_skipped", payload)
        elif result.status == "failed":
            self._audit_sink.record_sync(
                "skill.bundle_refresh_failed",
                {**payload, "error_type": result.error_type, "fallback_revision": result.active_revision},
            )

    def _record_skill_refresh_started(self, reason: str) -> None:
        if self._audit_sink is None or self._skill_runtime_state is None:
            return
        self._audit_sink.record_sync(
            "skill.bundle_refresh_started",
            {
                "reason": reason,
                "previous_revision": self._skill_runtime_state.active_revision,
            },
        )

    def _record_mcp_refresh_audit(self, result: MCPRuntimeRefreshResult) -> None:
        if self._audit_sink is None:
            return
        payload = {
            "reason": result.reason,
            "previous_revision": result.previous_revision,
            "active_revision": result.active_revision,
            "registered_count": result.registered_count,
            "skipped_count": result.skipped_count,
            "duration_ms": result.duration_ms,
        }
        if result.status == "completed":
            self._audit_sink.record_sync("mcp.server_discovery_completed", payload)
            self._record_mcp_capability_registration_audit()
        elif result.status == "skipped":
            self._audit_sink.record_sync("mcp.server_discovery_skipped", payload)
        elif result.status == "failed":
            self._audit_sink.record_sync(
                "mcp.server_discovery_failed",
                {**payload, "error_type": result.error_type, "fallback_revision": result.active_revision},
            )

    def _record_mcp_refresh_started(self, reason: str) -> None:
        if self._audit_sink is None or self._mcp_runtime_state is None:
            return
        self._audit_sink.record_sync(
            "mcp.server_discovery_started",
            {
                "reason": reason,
                "previous_revision": self._mcp_runtime_state.active_revision,
            },
        )

    def _record_mcp_capability_registration_audit(self) -> None:
        if self._audit_sink is None or self._mcp_runtime_state is None:
            return
        bundle = self._mcp_runtime_state.active_bundle
        for descriptor in bundle.descriptors:
            binding = bundle.bindings.get(descriptor.capability_id)
            self._audit_sink.record_sync(
                "mcp.capability_registered",
                _mcp_capability_audit_payload(
                    descriptor,
                    binding,
                ),
            )
        for diagnostic in bundle.diagnostics:
            self._audit_sink.record_sync(
                "mcp.capability_registration_skipped",
                {
                    "server_id": diagnostic.server_id,
                    "tool_name": diagnostic.tool_name,
                    "capability_id": diagnostic.capability_id,
                    "reason": diagnostic.reason,
                    "message": diagnostic.message,
                    "transport_security": diagnostic.transport_security,
                    "header_names": list(diagnostic.header_names),
                },
            )

    def _retain_task_skill_revision(self, request: OrchestrationRequest) -> None:
        if self._skill_runtime_state is None or request.task_id in self._task_skill_bundle_revisions:
            return
        raw_revision = request.metadata.get("skill_bundle_revision") or self._skill_runtime_state.active_revision
        revision = str(raw_revision).strip()
        if not revision:
            return
        self._pin_bundle_revision_with_sidecar_if_enforced(
            task_id=request.task_id,
            bundle_kind="skill",
            revision=revision,
        )
        self._skill_runtime_state.retain_revision(revision)
        self._task_skill_bundle_revisions[request.task_id] = revision
        self._record_bundle_revision_shadow(
            operation_name="bundle_revision_pin",
            task_id=request.task_id,
            bundle_kind="skill",
            revision=revision,
            legacy_output={"retained": "true", "task_id": request.task_id},
        )

    def _retain_task_mcp_revision(self, request: OrchestrationRequest) -> None:
        if self._mcp_runtime_state is None or request.task_id in self._task_mcp_bundle_revisions:
            return
        raw_revision = request.metadata.get("mcp_bundle_revision") or self._mcp_runtime_state.active_revision
        revision = str(raw_revision).strip()
        if not revision:
            return
        self._pin_bundle_revision_with_sidecar_if_enforced(
            task_id=request.task_id,
            bundle_kind="mcp",
            revision=revision,
        )
        self._mcp_runtime_state.retain_revision(revision)
        self._task_mcp_bundle_revisions[request.task_id] = revision
        self._record_bundle_revision_shadow(
            operation_name="bundle_revision_pin",
            task_id=request.task_id,
            bundle_kind="mcp",
            revision=revision,
            legacy_output={"retained": "true", "task_id": request.task_id},
        )

    async def _release_task_skill_revision_if_terminal(self, task_id: str) -> None:
        if self._skill_runtime_state is None:
            return
        task = await self.storage.get_task(task_id)
        if task is None or task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return
        revision = self._task_skill_bundle_revisions.get(task_id)
        if revision:
            self._release_bundle_revision_with_sidecar_if_enforced(
                task_id=task_id,
                bundle_kind="skill",
                revision=revision,
            )
            self._task_skill_bundle_revisions.pop(task_id, None)
            self._skill_runtime_state.release_revision(revision)
            self._record_bundle_revision_shadow(
                operation_name="bundle_revision_release",
                task_id=task_id,
                bundle_kind="skill",
                revision=revision,
                legacy_output={"released": "true", "task_id": task_id},
            )

    async def _release_task_mcp_revision_if_terminal(self, task_id: str) -> None:
        if self._mcp_runtime_state is None:
            return
        task = await self.storage.get_task(task_id)
        if task is None or task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return
        revision = self._task_mcp_bundle_revisions.get(task_id)
        if revision:
            self._release_bundle_revision_with_sidecar_if_enforced(
                task_id=task_id,
                bundle_kind="mcp",
                revision=revision,
            )
            self._task_mcp_bundle_revisions.pop(task_id, None)
            self._mcp_runtime_state.release_revision(revision)
            self._record_bundle_revision_shadow(
                operation_name="bundle_revision_release",
                task_id=task_id,
                bundle_kind="mcp",
                revision=revision,
                legacy_output={"released": "true", "task_id": task_id},
            )

    def _pin_bundle_revision_with_sidecar_if_enforced(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
    ) -> None:
        sidecar_client = self._task_dispatcher_sidecar_client_for("bundle_revision_pin")
        if sidecar_client is None:
            _ensure_task_dispatcher_write_allowed_by_rust_contract("bundle_revision_pin")
            return
        response = sidecar_client.pin_bundle_revision(
            task_id=task_id,
            bundle_kind=bundle_kind,
            revision=revision,
            idempotency_key=f"{task_id}:{bundle_kind}:{revision}:pin",
        )
        _consume_runtime_sidecar_response("bundle_revision_pin", response)

    def _release_bundle_revision_with_sidecar_if_enforced(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
    ) -> None:
        sidecar_client = self._task_dispatcher_sidecar_client_for("bundle_revision_release")
        if sidecar_client is None:
            _ensure_task_dispatcher_write_allowed_by_rust_contract("bundle_revision_release")
            return
        response = sidecar_client.release_bundle_revision(
            task_id=task_id,
            bundle_kind=bundle_kind,
            revision=revision,
            released_at_ms=int(self._utcnow_naive().timestamp() * 1000),
            idempotency_key=f"{task_id}:{bundle_kind}:{revision}:release",
        )
        _consume_runtime_sidecar_response("bundle_revision_release", response)

    def _record_bundle_revision_shadow(
        self,
        *,
        operation_name: str,
        task_id: str,
        bundle_kind: str,
        revision: str,
        legacy_output: Mapping[str, Any],
    ) -> None:
        released_at_ms = int(self._utcnow_naive().timestamp() * 1000)

        def call_sidecar() -> Any:
            if operation_name == "bundle_revision_pin":
                return self._runtime_sidecar_client.pin_bundle_revision(
                    task_id=task_id,
                    bundle_kind=bundle_kind,
                    revision=revision,
                    idempotency_key=f"{task_id}:{bundle_kind}:{revision}:pin",
                )
            return self._runtime_sidecar_client.release_bundle_revision(
                task_id=task_id,
                bundle_kind=bundle_kind,
                revision=revision,
                released_at_ms=released_at_ms,
                idempotency_key=f"{task_id}:{bundle_kind}:{revision}:release",
            )

        record_runtime_sidecar_shadow_write_sync(
            component="task_dispatcher",
            operation_name=operation_name,
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=self._runtime_sidecar_shadow_sink,
            input_payload={
                "bundle_kind": bundle_kind,
                "operation": operation_name,
                "revision": revision,
                "task_id": task_id,
            },
            legacy_output=legacy_output,
            rust_call=call_sidecar,
            rust_output=lambda envelope: {
                "bundle_kind": str(envelope.get("bundle_kind", "")),
                "revision": str(envelope.get("revision", "")),
                "task_id": str(envelope.get("task_id", "")),
            },
        )

    def _task_dispatcher_sidecar_client_for(self, operation_name: str) -> Any | None:
        if runtime_sidecar_mode_for_component("task_dispatcher") != "enforce":
            return None
        if self._runtime_sidecar_client is None:
            _ensure_task_dispatcher_write_allowed_by_rust_contract(operation_name)
            return None
        return self._runtime_sidecar_client

    def _resume_skill_revision_metadata(self, task_id: str) -> dict[str, object]:
        revision = self._task_skill_bundle_revisions.get(task_id)
        if revision:
            return {"skill_bundle_revision": revision}
        if self._skill_runtime_state is not None:
            return {"skill_bundle_revision": self._skill_runtime_state.active_revision}
        return {}

    @staticmethod
    def _answer_payload_metadata(answer_payload: Mapping[str, object]) -> dict[str, object]:
        metadata: dict[str, object] = {}
        for key, value in answer_payload.items():
            key_text = str(key).strip()
            if (
                not key_text
                or key_text in {"upload_ids", "client_request_id", "answer"}
                or key_text.startswith("_")
                or key_text in {SLOT_COLLECTION_FIELD, SLOT_COLLECTION_REF_FIELD}
            ):
                continue
            if key_text in USER_SUPPLIED_METADATA_DENYLIST:
                continue
            if isinstance(value, Mapping) and "text" in value:
                value = value.get("text")
            metadata[key_text] = value
        return metadata

    async def _resume_finalizer_node_id(self, task_id: str, interrupted_node_id: str) -> str | None:
        nodes = {node.node_id: node for node in await self.storage.list_task_nodes_for_task(task_id)}
        edges = await self.storage.list_task_edges(task_id)
        for edge in edges:
            if edge.from_node_id != interrupted_node_id:
                continue
            node = nodes.get(edge.to_node_id)
            if node is not None and node.capability_id == "main_agent.respond":
                return node.node_id
        return None

    @staticmethod
    def _answer_upload_ids(answer_payload: Mapping[str, object]) -> tuple[str, ...]:
        raw_upload_ids = answer_payload.get("upload_ids")
        if raw_upload_ids is None:
            return ()
        if isinstance(raw_upload_ids, str):
            values = [raw_upload_ids]
        elif isinstance(raw_upload_ids, list | tuple):
            values = raw_upload_ids
        else:
            raise UploadValidationError("answer_payload.upload_ids must be a list")
        return tuple(str(value).strip() for value in values if str(value).strip())

    async def _task_interrupt_answer_payloads(self, task_id: str) -> tuple[dict[str, object], ...]:
        rows: list[tuple[datetime, str, dict[str, object]]] = []
        for task_interrupt in await self.storage.list_interrupts_for_task(task_id):
            for saved_answer in await self.storage.list_interrupt_answers(task_interrupt.interrupt_id):
                payload = saved_answer.answer_payload
                if isinstance(payload, Mapping):
                    rows.append((saved_answer.created_at, saved_answer.interrupt_answer_id, dict(payload)))
        rows.sort(key=lambda item: (item[0], item[1]))
        return tuple(payload for _created_at, _answer_id, payload in rows)

    @classmethod
    def _merge_answer_payloads(cls, answer_payloads: Iterable[Mapping[str, object]]) -> dict[str, object]:
        merged: dict[str, object] = {}
        upload_ids: list[str] = []
        for payload in answer_payloads:
            for key, value in payload.items():
                key_text = str(key).strip()
                if not key_text:
                    continue
                if key_text.startswith("_"):
                    continue
                if key_text == "upload_ids":
                    upload_ids.extend(cls._answer_upload_ids(payload))
                    continue
                merged[key_text] = value
        if upload_ids:
            merged["upload_ids"] = list(dict.fromkeys(upload_ids))
        return merged

    @classmethod
    def _merged_answer_upload_ids(cls, answer_payloads: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
        upload_ids: list[str] = []
        for payload in answer_payloads:
            upload_ids.extend(cls._answer_upload_ids(payload))
            raw_sheet_selections = payload.get("upload_sheet_selections")
            if isinstance(raw_sheet_selections, Mapping):
                upload_ids.extend(str(key).strip() for key in raw_sheet_selections.keys() if str(key).strip())
        return tuple(dict.fromkeys(upload_ids))

    @classmethod
    def _format_answer_message(cls, answer_payload: dict[str, object]) -> str:
        parts: list[str] = []
        for key, value in answer_payload.items():
            if key in {"upload_ids", "client_request_id"} or str(key).startswith("_"):
                continue
            if key == "answer" and isinstance(value, Mapping):
                rendered = cls._format_v2_answer_message(value)
                if rendered:
                    parts.append(rendered)
                continue
            parts.append(f"{key}={cls._format_answer_value(value)}")
        if parts:
            return "；".join(parts)
        if cls._answer_upload_ids(answer_payload):
            return "已上传补充文件"
        return ""

    @staticmethod
    def _format_answer_value(value: object) -> object:
        if isinstance(value, Mapping):
            filenames = value.get("filenames")
            if isinstance(filenames, list | tuple) and filenames:
                return "、".join(str(item) for item in filenames if str(item).strip())
            upload_ids = value.get("upload_ids")
            if isinstance(upload_ids, list | tuple) and upload_ids:
                return "已上传文件"
        return value

    @classmethod
    def _combine_resume_message(cls, root_content: str, answer_payload: dict[str, object]) -> str:
        answer_text = cls._format_answer_message(answer_payload)
        if not answer_text:
            return root_content
        return f"{root_content}\n补充信息：{answer_text}"

    async def _await_existing_execution(self, task_id: str) -> None:
        async with self._lock:
            handle = self._running_tasks.get(task_id)
        if handle is None:
            return
        await asyncio.gather(handle, return_exceptions=True)
        async with self._lock:
            if self._running_tasks.get(task_id) is handle:
                self._running_tasks.pop(task_id, None)

    async def _cancel_existing_execution(self, task_id: str) -> None:
        async with self._lock:
            handle = self._running_tasks.get(task_id)
        if handle is None:
            return
        if handle is asyncio.current_task():
            return
        if not handle.done():
            handle.cancel()
        await asyncio.gather(handle, return_exceptions=True)
        async with self._lock:
            if self._running_tasks.get(task_id) is handle:
                self._running_tasks.pop(task_id, None)


def build_api_runtime(
    *,
    database_path: str | Path,
    audit_log_path: str | Path,
    mysql_adapter: MySQLReadonlyAdapter | None = None,
    platform_llm_text_generator=None,
    platform_llm_config: Mapping[str, Any] | None = None,
    platform_llm_config_path: str | Path | None = None,
    platform_llm_client_factory: Callable[..., Any] | None = None,
    enable_platform_llm: bool = True,
    planner_text_generator: PlannerTextGenerator | None = None,
    planner_llm_config: Mapping[str, Any] | None = None,
    planner_llm_config_path: str | Path | None = None,
    planner_llm_client_factory: Callable[..., Any] | None = None,
    planner_reasoning_effort: ReasoningEffort = "minimal",
    enable_llm_planner: bool = True,
    planner_payload_policies: Mapping[str, CapabilityPayloadPolicy] | None = None,
    main_agent_stream_generator: StreamGenerator | None = None,
    main_agent_llm_config: Mapping[str, Any] | None = None,
    main_agent_llm_config_path: str | Path | None = None,
    main_agent_llm_client_factory: Callable[..., Any] | None = None,
    main_agent_reasoning_effort: ReasoningEffort = "minimal",
    skill_input_text_generator: SkillInputTextGenerator | None = None,
    enable_skill_input_llm: bool = True,
    skill_platform_handlers: Mapping[str, Callable[..., Any]] | None = None,
    trusted_skill_handlers: Mapping[str, str] | None = None,
    trusted_skill_services: Mapping[str, tuple[str, ...] | list[str] | set[str]] | None = None,
    skill_services: Mapping[str, Any] | None = None,
    conversation_title_generator: ConversationTitleGenerator | None = None,
    enable_conversation_title_llm: bool = True,
    skill_roots: Iterable[str | Path] | None = None,
    public_skill_roots: Iterable[str | Path] | None = None,
    skill_catalog: SkillCatalog | None = None,
    mcp_config: Mapping[str, Any] | None = None,
    mcp_client_factory: Callable[..., Any] | None = None,
    mcp_sidecar_client: Any | None = None,
    mcp_runtime_state: MCPRuntimeState | None = None,
    runtime_replanner: RuntimeReplanner | None = None,
    auth_token_hash_secret: str | None = None,
    auth_token_hash_secret_required: bool | None = None,
    upload_store: InMemoryUploadStore | None = None,
    conversation_memory_builder: ConversationMemoryBuilder | None = None,
    enable_conversation_memory: bool = True,
    conversation_memory_resolution_generator: ResolutionGenerator | None = None,
    enable_conversation_memory_resolution_llm: bool = True,
    artifact_store_path: str | Path | None = None,
    runtime_sidecar_client: Any | None = None,
    skill_sandbox_client: Any | None = None,
) -> ApiRuntime:
    _bootstrap_runtime_config_env(
        platform_llm_text_generator=platform_llm_text_generator,
        platform_llm_config=platform_llm_config,
        platform_llm_config_path=platform_llm_config_path,
        platform_llm_client_factory=platform_llm_client_factory,
        enable_platform_llm=enable_platform_llm,
        planner_text_generator=planner_text_generator,
        planner_llm_config=planner_llm_config,
        planner_llm_config_path=planner_llm_config_path,
        planner_llm_client_factory=planner_llm_client_factory,
        enable_llm_planner=enable_llm_planner,
        main_agent_stream_generator=main_agent_stream_generator,
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
        enable_conversation_title_llm=enable_conversation_title_llm,
        enable_conversation_memory=enable_conversation_memory,
    )

    token_secret = auth_token_hash_secret if auth_token_hash_secret is not None else os.environ.get("MAF_AUTH_TOKEN_HASH_SECRET")
    deployment_env = (
        os.environ.get("MAF_API_ENV")
        or os.environ.get("MAF_ENV")
        or os.environ.get("APP_ENV")
        or ""
    ).strip().lower()
    token_secret_required = (
        auth_token_hash_secret_required
        if auth_token_hash_secret_required is not None
        else (
            os.environ.get("MAF_AUTH_TOKEN_HASH_SECRET_REQUIRED", "").strip().lower() in {"1", "true", "yes", "on"}
            or deployment_env in {"prod", "production"}
        )
    )
    if token_secret_required and not token_secret:
        raise AuthTokenValidationError("Auth token hash secret is required.", code="token_secret_required")

    _bootstrap_state_platform_config_env()
    state_config = build_state_platform_runtime_config(
        env=os.environ,
        require_driver=True,
    )
    resolved_runtime_sidecar_client = runtime_sidecar_client or _resolve_runtime_sidecar_client_from_env()
    audit_sink = JsonlAuditSink(audit_log_path)
    auth_generation_cache = AuthGenerationCache()
    auth_invalidation_bus = InMemoryAuthInvalidationBus()
    postgres_auth_invalidation_bus = None
    if state_config.backend == StatePlatformBackend.POSTGRESQL:
        engine = create_postgres_engine(state_config.dsn or "")
        bootstrap_postgres_database(engine)
        storage = PostgreSQLStorage(
            create_postgres_session_factory(engine),
            runtime_sidecar_client=resolved_runtime_sidecar_client,
            runtime_sidecar_shadow_sink=_build_runtime_sidecar_shadow_diff_sink(audit_sink),
        )
        if isinstance(engine, Engine):
            postgres_auth_invalidation_bus = PostgresAuthInvalidationBus(engine, auth_generation_cache)
            postgres_auth_invalidation_bus.check_permission()
            postgres_auth_invalidation_bus.reconcile_once()
        artifact_file_store = LocalArtifactFileStore(artifact_store_path or (Path(database_path).parent / "artifacts"))
    else:
        engine = create_sqlite_engine(database_path)
        bootstrap_sqlite_database(engine)
        storage = SQLiteStorage(
            create_sqlite_session_factory(engine),
            runtime_sidecar_client=resolved_runtime_sidecar_client,
            runtime_sidecar_shadow_sink=_build_runtime_sidecar_shadow_diff_sink(audit_sink),
        )
        artifact_file_store = LocalArtifactFileStore(artifact_store_path or (Path(database_path).parent / "artifacts"))
    username_token_service = UsernameTokenService(
        storage,
        now_fn=ApiRuntime._utcnow_naive,
        secret=token_secret,
        require_secret=token_secret_required,
        auth_generation_cache=auth_generation_cache,
        auth_invalidation_bus=auth_invalidation_bus,
    )

    roots = tuple(skill_roots) if skill_roots is not None else _default_skill_roots()
    resolved_public_skill_roots = tuple(public_skill_roots) if public_skill_roots is not None else roots[:1]

    capability_registry = CapabilityRegistry()
    _register_capability_descriptors(
        capability_registry,
        MAIN_AGENT_CAPABILITY_DESCRIPTORS,
        planner_payload_policies=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    )
    skill_runtime_state = SkillRuntimeState(
        skill_roots=roots,
        public_skill_roots=resolved_public_skill_roots,
        reserved_capability_ids=[descriptor.capability_id for descriptor in capability_registry.list()],
        initial_catalog=skill_catalog,
        refresh_enabled=skill_catalog is None,
    )

    instance_registry = InstanceRegistry()
    instance_registry.register(build_local_main_agent_instance())
    _sync_skill_capability_registry(capability_registry, instance_registry, skill_runtime_state)

    _record_skill_capability_startup_audit(audit_sink, skill_runtime_state.active_bundle.skill_capabilities)
    resolved_mcp_runtime_state = mcp_runtime_state or MCPRuntimeState(
        config=_resolve_mcp_runtime_config(mcp_config),
        client_factory=mcp_client_factory,
        sidecar_client=mcp_sidecar_client,
        reserved_capability_ids=[descriptor.capability_id for descriptor in capability_registry.list()],
    )
    if resolved_mcp_runtime_state.config.enabled:
        audit_sink.record_sync(
            "mcp.server_discovery_started",
            {
                "reason": "startup",
                "previous_revision": resolved_mcp_runtime_state.active_revision,
            },
        )
        pending_mcp_activation = resolved_mcp_runtime_state.prepare_refresh_sync(reason="startup", force=True)
        mcp_refresh_result = pending_mcp_activation.result
        if mcp_refresh_result.status == "completed":
            try:
                _sync_mcp_capability_registry(
                    capability_registry,
                    instance_registry,
                    pending_mcp_activation.bundle,
                )
            except Exception:
                resolved_mcp_runtime_state.discard_activation_sync(pending_mcp_activation)
                raise
            resolved_mcp_runtime_state.commit_activation_sync(pending_mcp_activation)
        _record_mcp_startup_audit(
            audit_sink,
            resolved_mcp_runtime_state,
            mcp_refresh_result,
        )
    event_broker = InMemoryEventBroker(audit_sink=audit_sink)
    runtime_cancelled_task_ids: set[str] = set()

    async def record_live_event(event: EventRecord) -> None:
        event = _ensure_event_created_at(event)
        await storage.append_event(event)
        await event_broker.publish(event)

    async def publish_transient_event(event: EventRecord) -> None:
        event = _ensure_event_created_at(event)
        await event_broker.publish_transient(event)

    main_agent_llm_runtime = _resolve_main_agent_llm_runtime(
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
        planner_llm_config=planner_llm_config,
        planner_llm_config_path=planner_llm_config_path,
        planner_llm_client_factory=planner_llm_client_factory,
    )

    resolved_mysql_adapter = mysql_adapter or MySQLReadonlyAdapter()
    resolved_conversation_title_generator = _resolve_conversation_title_generator(
        conversation_title_generator=conversation_title_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        enable_conversation_title_llm=enable_conversation_title_llm,
    )
    resolved_skill_input_text_generator = _resolve_skill_input_text_generator(
        skill_input_text_generator=skill_input_text_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        enable_skill_input_llm=enable_skill_input_llm,
    )
    skill_output_artifact_manager = SkillOutputArtifactManager(
        storage=storage,
        file_store=artifact_file_store,
        now_fn=ApiRuntime._utcnow_naive,
    )
    resolved_skill_sandbox_client = skill_sandbox_client or _resolve_skill_sandbox_client_from_env()
    resolved_skill_policy_client = _resolve_skill_policy_client(resolved_skill_sandbox_client)
    skill_script_runner = SkillScriptRunner(
        output_processor=skill_output_artifact_manager.process_for_runner,
        rust_sandbox_client=resolved_skill_sandbox_client,
        rust_sandbox_mode=os.environ.get("MAF_RUST_SKILL_RUNTIME_MODE", "off"),
        rust_sandbox_root=os.environ.get("MAF_SKILL_SANDBOX_ROOT") or None,
    )
    resolved_main_agent_stream_generator, main_agent_stream_metadata = _resolve_main_agent_stream_binding(
        main_agent_stream_generator=main_agent_stream_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        main_agent_reasoning_effort=main_agent_reasoning_effort,
    )
    resolved_platform_text_generator = _resolve_platform_text_generator(
        platform_llm_text_generator=platform_llm_text_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        platform_llm_config=platform_llm_config,
        platform_llm_client_factory=platform_llm_client_factory,
        enable_platform_llm=enable_platform_llm,
    )
    resolved_skill_service_registry = SkillServiceRegistry(
        {
            "mysql_readonly": resolved_mysql_adapter,
            "llm.non_stream": resolved_platform_text_generator,
            "artifact_writer": skill_output_artifact_manager,
            "progress_events": record_live_event,
            **dict(skill_services or {}),
        }
    )
    resolved_skill_platform_handler_registry = SkillPlatformHandlerRegistry(
        handlers=dict(skill_platform_handlers or {}),
        trusted_skill_handlers=dict(trusted_skill_handlers or {}),
        trusted_skill_services=dict(trusted_skill_services or {}),
        public_skill_roots=tuple(resolved_public_skill_roots),
        rust_policy_client=resolved_skill_policy_client,
        rust_policy_mode=os.environ.get("MAF_RUST_SKILL_RUNTIME_MODE", "off"),
        rust_policy_shadow_diff_sink=_build_skill_policy_shadow_diff_sink(audit_sink),
    )
    resolved_conversation_memory_builder = _resolve_conversation_memory_builder(
        storage=storage,
        conversation_memory_builder=conversation_memory_builder,
        main_agent_llm_runtime=main_agent_llm_runtime,
        enable_conversation_memory=enable_conversation_memory,
        resolution_generator=conversation_memory_resolution_generator,
        enable_resolution_llm=enable_conversation_memory_resolution_llm,
        model_edition_config=_resolve_model_edition_config(
            main_agent_llm_config=main_agent_llm_config,
            planner_llm_config=planner_llm_config,
            platform_llm_config=platform_llm_config,
        ),
    )

    main_agent_workflow_provider = MainAgentWorkflowProvider()

    def resolve_skill_name(capability_id: str, revision: str | None) -> str | None:
        try:
            return skill_runtime_state.skill_name_for_capability(capability_id, revision)
        except KeyError:
            return None

    def resolve_skill_manifest(capability_id: str, revision: str | None):
        skill_name = resolve_skill_name(capability_id, revision)
        if not skill_name:
            return None
        try:
            return skill_runtime_state.catalog_for_revision(revision).get(skill_name)
        except KeyError:
            return None

    skill_workflow_provider = SkillWorkflowProvider(
        skill_name_resolver=resolve_skill_name,
        skill_manifest_resolver=resolve_skill_manifest,
    )

    def resolve_macro_provider(capability_id: str):
        if capability_id.startswith("skill."):
            return skill_workflow_provider
        return None

    def resolve_active_skill_revision(capability_id: str) -> str | None:
        return (
            skill_runtime_state.active_revision
            if capability_id in skill_runtime_state.active_bundle.skill_capabilities.skill_name_by_capability_id
            else None
        )

    macro_providers = {}

    auto_workflow_provider = AutoWorkflowProvider(
        main_agent_provider=main_agent_workflow_provider,
        macro_providers=macro_providers,
        macro_provider_resolver=resolve_macro_provider,
    )
    resolved_planner_text_generator = _resolve_planner_text_generator(
        planner_text_generator=planner_text_generator,
        main_agent_llm_runtime=main_agent_llm_runtime,
        event_recorder=record_live_event,
        reasoning_event_publisher=publish_transient_event,
        planner_reasoning_effort=planner_reasoning_effort,
        enable_llm_planner=enable_llm_planner,
    )
    default_workflow_provider = LLMWorkflowProvider(
        capability_registry=capability_registry,
        fallback_provider=auto_workflow_provider,
        macro_providers=macro_providers,
        macro_provider_resolver=resolve_macro_provider,
        text_generator=resolved_planner_text_generator,
        payload_policies=planner_payload_policies,
    )
    default_replanners: list[RuntimeReplanner] = [
        SoftSkillBindingReplanner(
            capability_registry=capability_registry,
            macro_providers=macro_providers,
            macro_provider_resolver=resolve_macro_provider,
            active_skill_revision_resolver=resolve_active_skill_revision,
        ),
        MainAgentRuntimeReplanner(
            capability_registry=capability_registry,
            macro_providers=macro_providers,
            macro_provider_resolver=resolve_macro_provider,
            text_generator=resolved_planner_text_generator,
            payload_policies=planner_payload_policies,
        )
    ]
    resolved_runtime_replanner = runtime_replanner or CompositeRuntimeReplanner(default_replanners)

    cancellation_service = CancellationService(
        storage,
        event_sink=event_broker,
        audit_sink=audit_sink,
        runtime_sidecar_client=resolved_runtime_sidecar_client,
    )
    interrupt_service = InterruptService(storage, event_sink=event_broker, audit_sink=audit_sink)
    orchestration_service = OrchestrationService(
        storage=storage,
        capability_registry=capability_registry,
        instance_registry=instance_registry,
        scheduler=Scheduler(instance_registry),
        executor=CompositeExecutor(
            [
                MainAgentExecutor(
                    stream_generator=resolved_main_agent_stream_generator,
                    stream_metadata=main_agent_stream_metadata,
                    default_reasoning_effort=main_agent_reasoning_effort,
                    skill_catalog=skill_runtime_state.active_bundle.catalog,
                    skill_catalog_resolver=skill_runtime_state.catalog_for_revision,
                    script_runner=skill_script_runner,
                    skill_input_text_generator=resolved_skill_input_text_generator,
                    skill_output_artifact_manager=skill_output_artifact_manager,
                    transient_event_publisher=publish_transient_event,
                    cancel_checker=lambda task_id: task_id in runtime_cancelled_task_ids,
                ),
                SkillExecutor(
                    runtime_state=skill_runtime_state,
                    script_runner=skill_script_runner,
                    skill_input_text_generator=resolved_skill_input_text_generator,
                    platform_handler_registry=resolved_skill_platform_handler_registry,
                    service_registry=resolved_skill_service_registry,
                ),
                MCPToolExecutor(runtime_state=resolved_mcp_runtime_state, live_event_recorder=record_live_event),
            ]
        ),
        completion_policy=CompletionPolicy(),
        backpressure=BackpressureGuard(max_active_tasks=10),
        event_sink=event_broker,
        runtime_replanner=resolved_runtime_replanner,
    )

    return ApiRuntime(
        engine=engine,
        storage=storage,
        capability_registry=capability_registry,
        instance_registry=instance_registry,
        event_broker=event_broker,
        cancellation_service=cancellation_service,
        interrupt_service=interrupt_service,
        orchestration_service=orchestration_service,
        workflow_provider=WorkflowRouter(
            default_provider=default_workflow_provider,
            main_agent_provider=main_agent_workflow_provider,
            skill_provider=skill_workflow_provider,
        ),
        mysql_adapter=resolved_mysql_adapter,
        username_token_service=username_token_service,
        conversation_title_generator=resolved_conversation_title_generator,
        upload_store=upload_store,
        conversation_memory_builder=resolved_conversation_memory_builder,
        artifact_file_store=artifact_file_store,
        audit_sink=audit_sink,
        skill_runtime_state=skill_runtime_state,
        skill_input_text_generator=resolved_skill_input_text_generator,
        mcp_runtime_state=resolved_mcp_runtime_state,
        runtime_sidecar_client=resolved_runtime_sidecar_client,
        local_cancelled_task_ids=runtime_cancelled_task_ids,
        model_edition_config=_resolve_model_edition_config(
            main_agent_llm_config=main_agent_llm_config,
            planner_llm_config=planner_llm_config,
            platform_llm_config=platform_llm_config,
        ),
        auth_generation_cache=auth_generation_cache,
        auth_invalidation_bus=auth_invalidation_bus,
        postgres_auth_invalidation_bus=postgres_auth_invalidation_bus,
    )


def _resolve_model_edition_config(
    *,
    main_agent_llm_config: Mapping[str, Any] | None,
    planner_llm_config: Mapping[str, Any] | None,
    platform_llm_config: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    for config in (main_agent_llm_config, planner_llm_config, platform_llm_config):
        if config is not None:
            return config
    try:
        return load_config()
    except Exception:
        return {}


def _bootstrap_runtime_config_env(
    *,
    platform_llm_text_generator,
    platform_llm_config: Mapping[str, Any] | None,
    platform_llm_config_path: str | Path | None,
    platform_llm_client_factory: Callable[..., Any] | None,
    enable_platform_llm: bool,
    planner_text_generator: PlannerTextGenerator | None,
    planner_llm_config: Mapping[str, Any] | None,
    planner_llm_config_path: str | Path | None,
    planner_llm_client_factory: Callable[..., Any] | None,
    enable_llm_planner: bool,
    main_agent_stream_generator: StreamGenerator | None,
    main_agent_llm_config: Mapping[str, Any] | None,
    main_agent_llm_config_path: str | Path | None,
    main_agent_llm_client_factory: Callable[..., Any] | None,
    enable_conversation_title_llm: bool,
    enable_conversation_memory: bool,
) -> None:
    explicit_paths: list[str | Path] = []
    should_bootstrap_default = False

    if platform_llm_config is None:
        if platform_llm_config_path is not None:
            explicit_paths.append(platform_llm_config_path)
        elif enable_platform_llm and platform_llm_text_generator is None and platform_llm_client_factory is None:
            should_bootstrap_default = True

    if planner_llm_config is None:
        if planner_llm_config_path is not None:
            explicit_paths.append(planner_llm_config_path)
        elif enable_llm_planner and planner_text_generator is None and planner_llm_client_factory is None:
            should_bootstrap_default = True

    if main_agent_stream_generator is None and main_agent_llm_config is None:
        if main_agent_llm_config_path is not None:
            explicit_paths.append(main_agent_llm_config_path)
        elif main_agent_llm_client_factory is None:
            should_bootstrap_default = True

    if enable_conversation_title_llm and main_agent_llm_config is None:
        if main_agent_llm_config_path is not None:
            explicit_paths.append(main_agent_llm_config_path)
        elif main_agent_llm_client_factory is None:
            should_bootstrap_default = True

    if enable_conversation_memory:
        should_bootstrap_default = True

    seen_paths: set[Path] = set()
    for config_path in explicit_paths:
        resolved_path = Path(config_path).resolve()
        if resolved_path in seen_paths:
            continue
        seen_paths.add(resolved_path)

    if len(seen_paths) > 1:
        raise ValueError(
            "LLM config path values must resolve to the same startup config file; "
            "use explicit config dictionaries or client factories for component-specific provider settings."
        )

    for resolved_path in seen_paths:
        bootstrap_config_env(resolved_path, override=True)

    if explicit_paths or not should_bootstrap_default:
        return
    bootstrap_config_env(DEFAULT_CONFIG_PATH, strict=False)



def _bootstrap_state_platform_config_env() -> None:
    if os.environ.get("MAF_STATE_STORE_BACKEND"):
        return
    config = load_config()
    state_platform = config.get("state_platform")
    bridge_enabled = os.environ.get("MAF_STATE_PLATFORM_CONFIG_BRIDGE", "").strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(state_platform, Mapping):
        bridge_enabled = bridge_enabled or bool(state_platform.get("enabled"))
    if not bridge_enabled:
        return
    if isinstance(state_platform, Mapping):
        _set_env_from_config("MAF_STATE_STORE_BACKEND", state_platform.get("backend"))
        postgres = state_platform.get("postgres")
        if isinstance(postgres, Mapping):
            _set_env_from_config("MAF_POSTGRES_STATE_DSN", postgres.get("dsn"))
            _set_env_from_config("MAF_POSTGRES_STATE_SCHEMA", postgres.get("schema"))
    postgres_state_env = config.get("postgres_state_env")
    if isinstance(postgres_state_env, Mapping):
        for key, value in postgres_state_env.items():
            if str(key).startswith("MAF_"):
                _set_env_from_config(str(key), value)


def _set_env_from_config(key: str, value: object | None) -> None:
    if value is None or key in os.environ:
        return
    os.environ[key] = str(value)

def _resolve_runtime_sidecar_client_from_env() -> RuntimeSidecarGrpcClient | None:
    endpoint = os.environ.get("MAF_RUNTIME_SIDECAR_ENDPOINT", "").strip()
    if not endpoint:
        return None
    artifact_provenance, allowed_artifact_checksums, allowed_cargo_lock_digests = (
        _resolve_runtime_sidecar_artifact_trust_from_env()
    )
    allowed_hosts = tuple(
        host.strip()
        for host in os.environ.get("MAF_RUNTIME_SIDECAR_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    )
    mtls_enabled = os.environ.get("MAF_RUNTIME_SIDECAR_MTLS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return RuntimeSidecarGrpcClient(
        endpoint,
        config_source="environment_variable",
        allowed_hosts=allowed_hosts,
        mtls_enabled=mtls_enabled,
        tls_ca_path=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_CA_PATH", "").strip() or None,
        tls_cert_path=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_CERT_PATH", "").strip() or None,
        tls_key_path=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_KEY_PATH", "").strip() or None,
        tls_server_name=os.environ.get("MAF_RUNTIME_SIDECAR_TLS_SERVER_NAME", "").strip() or None,
        artifact_provenance=artifact_provenance,
        allowed_artifact_checksums=allowed_artifact_checksums,
        allowed_cargo_lock_digests=allowed_cargo_lock_digests,
    )


def _resolve_runtime_sidecar_artifact_trust_from_env() -> tuple[dict[str, Any] | None, tuple[str, ...], tuple[str, ...]]:
    manifest_path = os.environ.get("MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH", "").strip()
    allowlist_path = os.environ.get("MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH", "").strip()
    if not manifest_path and not allowlist_path:
        if _runtime_sidecar_enforce_enabled():
            _raise_runtime_sidecar_artifact_untrusted(
                "Rust runtime sidecar enforce mode requires an artifact manifest and allowlist"
            )
        return None, (), ()
    if not manifest_path or not allowlist_path:
        _raise_runtime_sidecar_artifact_untrusted(
            "Rust runtime sidecar artifact trust requires both manifest and allowlist paths"
        )

    manifest = _load_artifact_trust_json_file(
        manifest_path,
        "runtime sidecar artifact manifest",
        _raise_runtime_sidecar_artifact_untrusted,
    )
    allowlist = _load_artifact_trust_json_file(
        allowlist_path,
        "runtime sidecar artifact allowlist",
        _raise_runtime_sidecar_artifact_untrusted,
    )
    metadata = _runtime_sidecar_artifact_metadata_from_manifest(manifest)
    allowed_checksums, allowed_cargo_lock_digests = _artifact_allowlist_digests(
        allowlist,
        required_manifest=manifest,
        artifact_label="Rust runtime sidecar",
        raise_untrusted=_raise_runtime_sidecar_artifact_untrusted,
    )
    validate_runtime_sidecar_artifact_provenance(
        metadata,
        allowed_checksums=set(allowed_checksums),
        allowed_cargo_lock_digests=set(allowed_cargo_lock_digests),
    )
    return metadata, allowed_checksums, allowed_cargo_lock_digests


def _runtime_sidecar_enforce_enabled() -> bool:
    return any(
        runtime_sidecar_mode_for_component(component) == "enforce"
        for component in ("runtime_store", "event_log", "task_dispatcher")
    )


def _runtime_sidecar_artifact_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    contract_hashes = manifest.get("contract_hashes")
    proto_hashes = manifest.get("proto_hashes")
    if not isinstance(contract_hashes, Mapping) or not isinstance(proto_hashes, Mapping):
        _raise_runtime_sidecar_artifact_untrusted(
            "Rust runtime sidecar artifact manifest must include contract and proto hashes"
        )
    return {
        "source": manifest.get("source"),
        "artifact_kind": manifest.get("artifact_kind"),
        "checksum_sha256": manifest.get("artifact_sha256"),
        "sbom_digest": manifest.get("sbom_sha256"),
        "cargo_lock_digest": manifest.get("cargo_lock_sha256"),
        "proto_hash": proto_hashes.get("runtime"),
        "schema_hash": contract_hashes.get("runtime_sidecar"),
        "provenance_attestation": manifest.get("provenance_sha256"),
    }


def _raise_runtime_sidecar_artifact_untrusted(message: str) -> None:
    error_code = error_policy("runtime_store_artifact_untrusted")["code"]
    raise RuntimeError(f"{error_code}: {message}")


def _resolve_skill_sandbox_client_from_env() -> SkillSandboxGrpcClient | None:
    endpoint = os.environ.get("MAF_SKILL_SANDBOX_ENDPOINT", "").strip()
    if not endpoint:
        return None
    artifact_provenance, allowed_artifact_checksums, allowed_cargo_lock_digests = (
        _resolve_skill_sandbox_artifact_trust_from_env()
    )
    return SkillSandboxGrpcClient(
        endpoint,
        artifact_provenance=artifact_provenance,
        allowed_artifact_checksums=allowed_artifact_checksums,
        allowed_cargo_lock_digests=allowed_cargo_lock_digests,
    )


def _resolve_skill_sandbox_artifact_trust_from_env() -> tuple[dict[str, Any] | None, tuple[str, ...], tuple[str, ...]]:
    manifest_path = os.environ.get("MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH", "").strip()
    allowlist_path = os.environ.get("MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH", "").strip()
    if not manifest_path and not allowlist_path:
        if os.environ.get("MAF_RUST_SKILL_RUNTIME_MODE", "off").strip().lower() == "enforce":
            _raise_skill_runtime_artifact_untrusted(
                "Rust Skill Sandbox enforce mode requires an artifact manifest and allowlist"
            )
        return None, (), ()
    if not manifest_path or not allowlist_path:
        _raise_skill_runtime_artifact_untrusted(
            "Rust Skill Sandbox artifact trust requires both manifest and allowlist paths"
        )

    manifest = _load_artifact_trust_json_file(
        manifest_path,
        "Rust Skill Sandbox artifact manifest",
        _raise_skill_runtime_artifact_untrusted,
    )
    allowlist = _load_artifact_trust_json_file(
        allowlist_path,
        "Rust Skill Sandbox artifact allowlist",
        _raise_skill_runtime_artifact_untrusted,
    )
    metadata = _skill_sandbox_artifact_metadata_from_manifest(manifest)
    allowed_checksums, allowed_cargo_lock_digests = _artifact_allowlist_digests(
        allowlist,
        required_manifest=manifest,
        artifact_label="Rust Skill Sandbox",
        raise_untrusted=_raise_skill_runtime_artifact_untrusted,
    )
    validate_skill_runtime_artifact_provenance(
        metadata,
        allowed_checksums=set(allowed_checksums),
        allowed_cargo_lock_digests=set(allowed_cargo_lock_digests),
    )
    return metadata, allowed_checksums, allowed_cargo_lock_digests


def _skill_sandbox_artifact_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    contract_hashes = manifest.get("contract_hashes")
    if not isinstance(contract_hashes, Mapping):
        _raise_skill_runtime_artifact_untrusted(
            "Rust Skill Sandbox artifact manifest must include contract hashes"
        )
    contract = load_skill_runtime_contract()
    artifact_kind = manifest.get("artifact_kind")
    artifact_id = manifest.get("artifact_id")
    if artifact_id == "maf_skill_sandbox" and artifact_kind == "sidecar_binary":
        artifact_kind = "skill_sandbox_sidecar_binary"
    return {
        "source": manifest.get("source"),
        "artifact_kind": artifact_kind,
        "checksum_sha256": manifest.get("artifact_sha256"),
        "cargo_lock_digest": manifest.get("cargo_lock_sha256"),
        "contract_version": contract["contract_version"],
        "bundle_revision": manifest.get("git_commit") or manifest.get("artifact_name"),
        "schema_hash": contract_hashes.get("skill_runtime"),
        "sbom_digest": manifest.get("sbom_sha256"),
        "provenance_attestation": manifest.get("provenance_sha256"),
    }


def _load_artifact_trust_json_file(
    path: str,
    label: str,
    raise_untrusted: Callable[[str], None],
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise_untrusted(f"{label} is unavailable or invalid")
        raise AssertionError("unreachable") from exc
    if not isinstance(payload, dict):
        raise_untrusted(f"{label} must be a JSON object")
    return payload


def _artifact_allowlist_digests(
    allowlist: Mapping[str, Any],
    *,
    required_manifest: Mapping[str, Any],
    artifact_label: str,
    raise_untrusted: Callable[[str], None],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    entries = allowlist.get("allowed_artifacts")
    if not isinstance(entries, list):
        raise_untrusted(f"{artifact_label} artifact allowlist must contain allowed_artifacts")
    checksums: list[str] = []
    cargo_lock_digests: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise_untrusted(f"{artifact_label} artifact allowlist entry is invalid")
        checksum = entry.get("artifact_sha256") or entry.get("checksum_sha256")
        cargo_lock = entry.get("cargo_lock_sha256") or entry.get("cargo_lock_digest")
        if isinstance(checksum, str) and checksum:
            checksums.append(checksum)
        if isinstance(cargo_lock, str) and cargo_lock:
            cargo_lock_digests.append(cargo_lock)
    if not any(_artifact_allowlist_entry_matches_manifest(entry, required_manifest) for entry in entries):
        raise_untrusted(f"{artifact_label} artifact manifest is not present in the allowlist")
    if not checksums or not cargo_lock_digests:
        raise_untrusted(f"{artifact_label} artifact allowlist must include checksums and Cargo.lock digests")
    return tuple(sorted(set(checksums))), tuple(sorted(set(cargo_lock_digests)))


def _artifact_allowlist_entry_matches_manifest(entry: object, manifest: Mapping[str, Any]) -> bool:
    if not isinstance(entry, Mapping):
        return False
    exact_fields = (
        "component",
        "artifact_id",
        "artifact_kind",
        "artifact_sha256",
        "cargo_lock_sha256",
        "sbom_sha256",
        "provenance_sha256",
        "source",
        "git_commit",
        "toolchain",
        "target_triple",
        "build_profile",
    )
    if any(entry.get(field) != manifest.get(field) for field in exact_fields):
        return False
    for field in ("cargo_features", "contract_hashes", "proto_hashes"):
        if entry.get(field) != manifest.get(field):
            return False
    return True


def _raise_skill_runtime_artifact_untrusted(message: str) -> None:
    error_code = skill_runtime_error_policy("skill_runtime_artifact_untrusted")["code"]
    raise RuntimeError(f"{error_code}: {message}")


def _resolve_skill_policy_client(fallback_client: Any | None) -> Any | None:
    module_name = os.environ.get("MAF_SKILL_POLICY_PYO3_MODULE", "").strip() or "maf_skill_runtime_pyo3"
    pyo3_client = try_load_skill_runtime_pyo3_policy_client(module_name=module_name)
    return pyo3_client or fallback_client


def _resolve_main_agent_llm_runtime(
    *,
    main_agent_llm_config: Mapping[str, Any] | None,
    main_agent_llm_config_path: str | Path | None,
    main_agent_llm_client_factory: Callable[..., Any] | None,
    planner_llm_config: Mapping[str, Any] | None,
    planner_llm_config_path: str | Path | None,
    planner_llm_client_factory: Callable[..., Any] | None,
) -> SharedLLMRuntime:
    config = main_agent_llm_config or planner_llm_config
    factory = main_agent_llm_client_factory or planner_llm_client_factory or LLMClient
    if main_agent_llm_config is not None:
        config_source = "injected_config"
    elif planner_llm_config is not None:
        config_source = "injected_config"
    elif main_agent_llm_config_path is not None or planner_llm_config_path is not None:
        config_source = "environment"
    elif main_agent_llm_client_factory is not None:
        config_source = "main_agent_factory_default"
    elif planner_llm_client_factory is not None:
        config_source = "planner_factory_default"
    else:
        config_source = "environment"
    return SharedLLMRuntime(client_factory=factory, config=config, config_source=config_source)


def _resolve_platform_text_generator(
    *,
    platform_llm_text_generator,
    main_agent_llm_runtime: SharedLLMRuntime,
    platform_llm_config: Mapping[str, Any] | None,
    platform_llm_client_factory: Callable[..., Any] | None,
    enable_platform_llm: bool,
):
    if platform_llm_text_generator is not None:
        return platform_llm_text_generator
    if not enable_platform_llm:
        return None
    runtime = main_agent_llm_runtime
    if platform_llm_config is not None or platform_llm_client_factory is not None:
        runtime = SharedLLMRuntime(
            client_factory=platform_llm_client_factory or LLMClient,
            config=platform_llm_config,
            config_source="injected_config" if platform_llm_config is not None else "environment",
        )

    async def generate(prompt: str, **kwargs: Any) -> str:
        options = resolve_llm_request_options(_metadata_from_llm_kwargs(kwargs))
        return await runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
        )

    return generate


def _resolve_conversation_title_generator(
    *,
    conversation_title_generator: ConversationTitleGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    enable_conversation_title_llm: bool,
) -> ConversationTitleGenerator | None:
    if conversation_title_generator is not None:
        return conversation_title_generator
    if not enable_conversation_title_llm:
        return None

    async def generate(title_source: str, **kwargs: Any) -> str:
        options = resolve_llm_request_options(_metadata_from_llm_kwargs(kwargs))
        return await main_agent_llm_runtime.generate_text(
            build_conversation_title_prompt(title_source),
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
        )

    return generate


def _resolve_skill_input_text_generator(
    *,
    skill_input_text_generator: SkillInputTextGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    enable_skill_input_llm: bool,
) -> SkillInputTextGenerator | None:
    if skill_input_text_generator is not None:
        return skill_input_text_generator
    if not enable_skill_input_llm:
        return None

    async def generate(prompt: str, **kwargs: Any) -> str:
        options = resolve_llm_request_options(_metadata_from_llm_kwargs(kwargs))
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
        )

    return generate


def _resolve_conversation_memory_builder(
    *,
    storage: SQLiteStorage,
    conversation_memory_builder: ConversationMemoryBuilder | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    enable_conversation_memory: bool,
    resolution_generator: ResolutionGenerator | None,
    enable_resolution_llm: bool,
    model_edition_config: Mapping[str, Any] | None = None,
) -> ConversationMemoryBuilder | None:
    if conversation_memory_builder is not None:
        return conversation_memory_builder
    if not enable_conversation_memory:
        return None

    async def generate_summary(prompt: str, **kwargs: Any) -> str:
        model_edition = resolve_llm_model_edition(_metadata_from_llm_kwargs(kwargs))
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=False,
            reasoning_effort="minimal",
            model_edition=model_edition,
        )

    async def generate_resolution(prompt: str, **kwargs: Any) -> str:
        options = resolve_llm_request_options(_metadata_from_llm_kwargs(kwargs))
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
        )

    def resolve_memory_config(request: OrchestrationRequest) -> ConversationMemoryConfig:
        selected_model_edition = _resolve_request_model_edition(request.metadata)
        return ConversationMemoryConfig.from_runtime_config(
            config_for_model_edition(model_edition_config, selected_model_edition)
        )

    return ConversationMemoryBuilder(
        storage=storage,
        config=ConversationMemoryConfig.from_runtime_config(config_for_model_edition(model_edition_config, None)),
        summary_generator=generate_summary,
        resolution_generator=resolution_generator or (generate_resolution if enable_resolution_llm else None),
        config_resolver=resolve_memory_config,
    )


def _metadata_from_llm_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    request = kwargs.get("request")
    request_metadata = getattr(request, "metadata", None)
    if isinstance(request_metadata, Mapping):
        metadata.update(request_metadata)
    explicit_metadata = kwargs.get("metadata")
    if isinstance(explicit_metadata, Mapping):
        metadata.update(explicit_metadata)
    return metadata


def _coerce_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _register_capability_descriptors(
    capability_registry: CapabilityRegistry,
    descriptors: Iterable[CapabilityDescriptor],
    *,
    planner_payload_policies: Mapping[str, CapabilityPayloadPolicy] | None = None,
) -> None:
    policies = dict(planner_payload_policies or {})
    for descriptor in descriptors:
        capability_registry.register(
            descriptor,
            planner_payload_policy=policies.get(descriptor.capability_id),
        )


def _sync_skill_capability_registry(
    capability_registry: CapabilityRegistry,
    instance_registry: InstanceRegistry,
    runtime_state: SkillRuntimeState,
) -> None:
    registry = runtime_state.active_bundle.skill_capabilities
    previous_descriptors = [descriptor for descriptor in capability_registry.list() if _is_skill_descriptor(descriptor)]
    previous_policies = {
        descriptor.capability_id: capability_registry.get_planner_payload_policy(descriptor.capability_id)
        for descriptor in previous_descriptors
    }
    try:
        for descriptor in previous_descriptors:
            capability_registry.unregister(descriptor.capability_id)
        _register_capability_descriptors(
            capability_registry,
            registry.descriptors,
            planner_payload_policies=registry.payload_policies,
        )
        instance_registry.register(build_local_skill_executor_instance(runtime_state.known_skill_capability_ids()))
    except Exception:
        for descriptor in list(capability_registry.list()):
            if _is_skill_descriptor(descriptor):
                capability_registry.unregister(descriptor.capability_id)
        for descriptor in previous_descriptors:
            capability_registry.register(
                descriptor,
                planner_payload_policy=previous_policies.get(descriptor.capability_id),
            )
        instance_registry.register(build_local_skill_executor_instance(runtime_state.known_skill_capability_ids()))
        raise


def _sync_mcp_capability_registry(
    capability_registry: CapabilityRegistry,
    instance_registry: InstanceRegistry,
    bundle: MCPRuntimeBundle,
) -> None:
    previous_descriptors = [descriptor for descriptor in capability_registry.list() if _is_mcp_descriptor(descriptor)]
    previous_policies = {
        descriptor.capability_id: capability_registry.get_planner_payload_policy(descriptor.capability_id)
        for descriptor in previous_descriptors
    }
    try:
        for descriptor in previous_descriptors:
            capability_registry.unregister(descriptor.capability_id)
        _register_capability_descriptors(
            capability_registry,
            bundle.descriptors,
            planner_payload_policies=bundle.payload_policies,
        )
        if bundle.bindings:
            instance_registry.register(build_local_mcp_tool_instance(tuple(bundle.bindings)))
    except Exception:
        for descriptor in list(capability_registry.list()):
            if _is_mcp_descriptor(descriptor):
                capability_registry.unregister(descriptor.capability_id)
        for descriptor in previous_descriptors:
            capability_registry.register(
                descriptor,
                planner_payload_policy=previous_policies.get(descriptor.capability_id),
            )
        if previous_descriptors:
            instance_registry.register(
                build_local_mcp_tool_instance(tuple(descriptor.capability_id for descriptor in previous_descriptors))
            )
        raise


def _is_mcp_descriptor(descriptor: CapabilityDescriptor) -> bool:
    return descriptor.kind == "mcp_tool" or descriptor.source == "mcp" or descriptor.capability_id.startswith("mcp.")


def _is_skill_descriptor(descriptor: CapabilityDescriptor) -> bool:
    return descriptor.kind == "skill" or descriptor.source == "skill" or descriptor.capability_id.startswith("skill.")


def _resolve_mcp_runtime_config(config: Mapping[str, Any] | None) -> MCPRuntimeConfig:
    if config is not None:
        return load_mcp_server_config(base_config=MCPRuntimeConfig.from_mapping(config))
    loaded = load_config()
    raw = loaded.get("mcp")
    if isinstance(raw, Mapping):
        return load_mcp_server_config(base_config=MCPRuntimeConfig.from_mapping(raw))
    return load_mcp_server_config(base_config=MCPRuntimeConfig.disabled())


def _record_mcp_startup_audit(
    audit_sink: JsonlAuditSink,
    runtime_state: MCPRuntimeState,
    refresh_result: MCPRuntimeRefreshResult,
) -> None:
    payload = {
        "reason": refresh_result.reason,
        "previous_revision": refresh_result.previous_revision,
        "active_revision": refresh_result.active_revision,
        "registered_count": refresh_result.registered_count,
        "skipped_count": refresh_result.skipped_count,
        "duration_ms": refresh_result.duration_ms,
    }
    payload.update(_mcp_security_audit_summary(runtime_state.active_bundle))
    if refresh_result.status == "completed":
        audit_sink.record_sync("mcp.server_discovery_completed", payload)
        for descriptor in runtime_state.active_bundle.descriptors:
            binding = runtime_state.active_bundle.bindings.get(descriptor.capability_id)
            audit_sink.record_sync(
                "mcp.capability_registered",
                _mcp_capability_audit_payload(descriptor, binding),
            )
        for diagnostic in runtime_state.active_bundle.diagnostics:
            audit_sink.record_sync(
                "mcp.capability_registration_skipped",
                {
                    "server_id": diagnostic.server_id,
                    "tool_name": diagnostic.tool_name,
                    "capability_id": diagnostic.capability_id,
                    "reason": diagnostic.reason,
                    "message": diagnostic.message,
                    "transport_security": diagnostic.transport_security,
                    "header_names": list(diagnostic.header_names),
                },
            )
        return
    if refresh_result.status == "failed":
        audit_sink.record_sync(
            "mcp.server_discovery_failed",
            {**payload, "error_type": refresh_result.error_type, "fallback_revision": refresh_result.active_revision},
        )
        return
    audit_sink.record_sync("mcp.server_discovery_skipped", payload)


def _mcp_capability_audit_payload(descriptor: CapabilityDescriptor, binding: Any | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "capability_id": descriptor.capability_id,
        "name": descriptor.name,
        "source_path": descriptor.source_path,
        "source": descriptor.source,
    }
    if binding is not None:
        payload.update(
            {
                "server_id": getattr(binding, "server_id", ""),
                "tool_name": getattr(binding, "tool_name", ""),
                "transport_security": getattr(binding, "transport_security", ""),
                "header_names": list(getattr(binding, "header_names", ()) or ()),
                "credential_over_plaintext_http": bool(getattr(binding, "credential_over_plaintext_http", False)),
            }
        )
    return payload


def _mcp_security_audit_summary(bundle: MCPRuntimeBundle) -> dict[str, Any]:
    transport_security = {
        value
        for value in (
            *(getattr(binding, "transport_security", "") for binding in bundle.bindings.values()),
            *(diagnostic.transport_security for diagnostic in bundle.diagnostics),
        )
        if value
    }
    header_names = {
        name
        for names in (
            *(getattr(binding, "header_names", ()) or () for binding in bundle.bindings.values()),
            *(diagnostic.header_names for diagnostic in bundle.diagnostics),
        )
        for name in names
        if name
    }
    credential_over_plaintext_http = any(
        bool(getattr(binding, "credential_over_plaintext_http", False))
        for binding in bundle.bindings.values()
    )
    return {
        "transport_security": sorted(transport_security),
        "header_names": sorted(header_names),
        "credential_over_plaintext_http": credential_over_plaintext_http,
    }


def _record_skill_capability_startup_audit(
    audit_sink: JsonlAuditSink,
    skill_capabilities: SkillCapabilityRegistry,
) -> None:
    for descriptor in skill_capabilities.descriptors:
        audit_sink.record_sync(
            "skill.capability_registered",
            {
                "capability_id": descriptor.capability_id,
                "skill_name": skill_capabilities.skill_name_by_capability_id[descriptor.capability_id],
                "source_path": skill_capabilities.source_path_by_capability_id.get(descriptor.capability_id, ""),
                "source": descriptor.source,
            },
        )
    for diagnostic in skill_capabilities.diagnostics:
        audit_sink.record_sync(
            "skill.capability_registration_skipped",
            {
                "skill_name": diagnostic.skill_name,
                "reason": diagnostic.reason,
                "message": diagnostic.message,
                "source_path": diagnostic.source_path_summary,
            },
        )


def _ensure_event_created_at(event: EventRecord) -> EventRecord:
    if event.created_at is not None:
        return event
    return replace(event, created_at=datetime.now(timezone.utc).replace(tzinfo=None))


def _ensure_task_dispatcher_write_allowed_by_rust_contract(operation_name: str) -> None:
    ensure_sidecar_write_allowed(
        component="task_dispatcher",
        operation_name=operation_name,
        unavailable_error_code="dispatcher_unavailable",
    )


def _consume_runtime_sidecar_response(operation_name: str, response: Mapping[str, Any]) -> None:
    envelope = validate_runtime_sidecar_response(operation_name, response)
    error = envelope.get("error")
    if isinstance(error, Mapping):
        raise RuntimeError(f"{error['code']}: {error['message']}")


def _build_skill_policy_shadow_diff_sink(
    audit_sink: JsonlAuditSink | None,
) -> Callable[[Mapping[str, str]], None] | None:
    if audit_sink is None:
        return None

    def record_shadow_diff(payload: Mapping[str, str]) -> None:
        audit_sink.record_sync("skill.runtime_policy_shadow_diff", payload)

    return record_shadow_diff


def _build_runtime_sidecar_shadow_diff_sink(
    audit_sink: JsonlAuditSink | None,
) -> Callable[[Mapping[str, str]], None] | None:
    if audit_sink is None:
        return None

    def record_shadow_diff(payload: Mapping[str, str]) -> None:
        audit_sink.record_sync("runtime.sidecar_shadow_diff", payload)

    return record_shadow_diff


def _build_safety_kernel_shadow_diff_sink(
    audit_sink: JsonlAuditSink | None,
) -> Callable[[Mapping[str, str]], None] | None:
    if audit_sink is None:
        return None

    def record_shadow_diff(payload: Mapping[str, str]) -> None:
        audit_sink.record_sync("safety.kernel_shadow_diff", payload)

    return record_shadow_diff


def _resolve_planner_text_generator(
    *,
    planner_text_generator: PlannerTextGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    event_recorder: Callable[[EventRecord], Any],
    reasoning_event_publisher: Callable[[EventRecord], Any] | None,
    planner_reasoning_effort: ReasoningEffort,
    enable_llm_planner: bool,
) -> PlannerTextGenerator | None:
    if planner_text_generator is not None:
        return planner_text_generator
    if not enable_llm_planner:
        return None
    del reasoning_event_publisher  # Planner is a background LLM branch; do not surface reasoning_content.

    call_ordinal = 0

    async def generate(
        prompt: str,
        *,
        request: OrchestrationRequest | None = None,
        stage: str = "orchestration_plan",
        prompt_profile: Mapping[str, Any] | None = None,
    ) -> str:
        nonlocal call_ordinal
        call_ordinal += 1
        call_id = call_ordinal

        if request is not None and prompt_profile is not None:
            maybe_result = event_recorder(
                EventRecord(
                    event_id=f"{request.task_id}:main_agent.{stage}.prompt_profile:{call_id}",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id="main_agent.orchestrator",
                    event_type="main_agent.prompt_profile_rendered",
                    payload={**dict(prompt_profile), "stage": stage, "call_id": call_id},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            if inspect.isawaitable(maybe_result):
                await maybe_result

        metadata = dict(request.metadata) if request is not None else {}
        options = resolve_llm_request_options(metadata, fallback_reasoning_effort=planner_reasoning_effort)
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
        )

    return generate


def _resolve_request_reasoning_effort(
    metadata: Mapping[str, Any],
    *,
    fallback: ReasoningEffort,
    thinking_enabled: bool,
) -> ReasoningEffort:
    return resolve_llm_reasoning_effort(metadata, fallback=fallback, thinking_enabled=thinking_enabled)


def _resolve_request_thinking_enabled(metadata: Mapping[str, Any]) -> bool:
    return resolve_llm_thinking_enabled(metadata)


def _resolve_request_model_edition(metadata: Mapping[str, Any]) -> str | None:
    return resolve_llm_model_edition(metadata)


def _resolve_main_agent_stream_binding(
    *,
    main_agent_stream_generator: StreamGenerator | None,
    main_agent_llm_runtime: SharedLLMRuntime,
    main_agent_reasoning_effort: ReasoningEffort,
) -> tuple[StreamGenerator | None, dict[str, Any]]:
    if main_agent_stream_generator is not None:
        return main_agent_stream_generator, {
            "provider": "injected_stream",
            "config_source": "injected_stream",
            "reasoning_effort": main_agent_reasoning_effort,
        }

    return main_agent_llm_runtime.stream_events, main_agent_llm_runtime.static_metadata(
        reasoning_effort=main_agent_reasoning_effort
    )


def _default_skill_roots() -> tuple[Path, ...]:
    return (Path.cwd() / "skill",)
