from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
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
from src.capabilities.main_agent.prompt_builder import MAIN_AGENT_SKILL_DOCUMENT_GROUNDING_CONSTRAINT
from src.capabilities.mcp_dispatch import (
    MCP_DISPATCH_CAPABILITY_DESCRIPTOR,
    MCP_DISPATCH_PLANNER_PAYLOAD_POLICY,
    MCPDispatchExecutor,
    MCPDispatchWorkflowProvider,
    MCPServerRouter,
    MCPToolSelector,
    build_local_mcp_dispatch_instance,
)
from src.capabilities.mcp_tool import MCPToolExecutor, build_local_mcp_tool_instance
from src.capabilities.skill_tool import SkillExecutor, build_local_skill_executor_instance
from src.core.enums import ConversationStatus, EventVisibility, InterruptStatus, MessageRole, NodeCriticality, NodeStatus, RoutingMode, TaskStatus, UserMCPTransport
from src.core.models import (
    Conversation,
    ConversationFileResource,
    EventRecord,
    Interrupt,
    InterruptAnswer,
    Message,
    MCPShadowAuditSample,
    MCPRolloutInstanceConfigLease,
    PendingSkillContext,
    SlotCollection,
    SlotEvent,
    Task,
    TaskInputAttachment,
    TaskNode,
)
from src.api.file_selection_runtime import ConversationFileSelectionRuntimeMixin
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
    LLMRequestOptions,
    resolve_llm_model_edition,
    resolve_llm_reasoning_effort,
    resolve_llm_request_options,
    resolve_llm_thinking_enabled,
)
from src.integrations.llm_runtime import SharedLLMRuntime
from src.integrations.model_editions import (
    config_for_model_edition,
    default_model_edition,
    model_edition_options,
    model_reasoning_effort_configs,
    validate_model_edition,
    validate_model_reasoning_effort_configs,
)
from src.integrations.mcp import MCPRuntimeBundle, MCPRuntimeConfig, MCPRuntimeRefreshResult, MCPRuntimeState, load_mcp_server_config
from src.integrations.mcp.credentials import CredentialCipher, MCPRecoveryService
from src.integrations.mcp.audit import MCPAuditService
from src.integrations.mcp.endpoint_policy import EndpointAllowlist, EndpointPolicy
from src.integrations.mcp.gateway import MCPGateway
from src.integrations.mcp.dispatch_coordinator import (
    MCPDispatchMetricContext,
    UserMCPDispatchCoordinator,
)
from src.integrations.mcp.health import MCPHealthRunner
from src.integrations.mcp.recovery_worker import (
    MCPContinuationAdmissionResult,
    MCPRemoteTaskRecoveryWorker,
    MCPRemoteTaskTerminalMetricSample,
)
from src.integrations.mcp.shadow_compare import (
    MCPShadowRuntimeObserver,
    RuntimeShadowMappingResolution,
    ShadowManifestError,
    ShadowComparison,
    ShadowOutcome,
    ShadowScenario,
    VerifiedShadowScenarioManifest,
    approved_shadow_mapping_set_fingerprint,
    compare_live_shadow_sample,
    derive_shadow_catalog_digest_key,
    load_signed_shadow_manifest_file,
    migration_target_credential_digest,
    resolve_approved_migration_mapping,
    shadow_fixture_bindings_fingerprint,
)
from src.integrations.mcp.shadow_evidence import (
    MCP_SHADOW_SAMPLE_RETENTION,
    seal_shadow_audit_sample,
)
from src.integrations.mcp.rollout import (
    MCPExecutionPath,
    MCPRoutingMode,
    MCPRolloutConfig,
    mcp_rollout_env_is_configured,
)
from src.integrations.mcp.observability import (
    MCPRolloutMetricContext,
    MCPRolloutMetricRecorder,
)
from src.integrations.mcp.safety_detectors import (
    AuthoritativeMCPSafetyDetectorRegistry,
    MCPSafetyMetricGap,
    register_authoritative_mcp_safety_detectors,
)
from src.integrations.mcp.protocol import (
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_PROTOCOL_VERSION_2026_07_28,
)
from src.integrations.mcp.rollout_evidence import (
    MCPCallKind,
    MCPMetricAdapter,
    MCPMetricErrorCategory,
    MCPMetricExecutionPath,
    MCPMetricLabels,
    MCPMetricName,
    MCPMetricProtocolVersion,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
    MCPMetricTransport,
    MCPRolloutStage,
    MCPSafetyRedLine,
)
from src.integrations.mcp.invalidation import (
    CompositeMCPInvalidationPublisher,
    InMemoryMCPInvalidationBus,
    MCPInvalidationAction,
    PostgresMCPInvalidationBus,
)
from src.integrations.mcp.temporary_results import (
    MCPTemporaryResultCapacity,
    MCPTemporaryResultCapacityConfig,
    MCPTemporaryResultJanitor,
    MCPTemporaryResultStore,
)
from src.integrations.mcp.user_client import UserMCPClientFactory, UserMCPCredentialResolver
from src.integrations.mcp.user_config import UserMCPConfigService
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.integrations.rust_safety_contract import configure_safety_shadow_sink
from src.lifecycle.cancellation_service import CancellationService
from src.lifecycle.conversation_guard import ConversationSerialGuard
from src.lifecycle.interrupt_service import InterruptService
from src.lifecycle.mcp_presence import MCPTaskPresenceService
from src.orchestration.answer_selection import select_final_text_artifact
from src.orchestration.capability_fallback import (
    CAPABILITY_MISSING_FALLBACK_EVENT,
    CAPABILITY_MISSING_FALLBACK_KEY,
    build_capability_missing_fallback_metadata,
    merge_capability_missing_fallback_metadata,
    sanitize_capability_missing_fallback_metadata,
)
from src.orchestration.backpressure import DEFAULT_MAX_ACTIVE_TASKS, BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.conversation_memory import ConversationMemoryBuilder, ConversationMemoryConfig, ResolutionGenerator
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider, WorkflowPlanningError
from src.orchestration.models import (
    CapabilityDescriptor,
    OrchestrationRequest,
    OrchestrationRunResult,
    UserMCPServerProfile,
    WorkflowPlan,
    WorkflowNodePlan,
)
from src.orchestration.planner_contract import TextGenerator as PlannerTextGenerator
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy
from src.orchestration.visible_message_history import INTERRUPT_VISIBLE_STREAM_STATUS, persist_interrupt_question_message
from src.orchestration.composite_executor import CompositeExecutor
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.runtime_replanner import CompositeRuntimeReplanner, RuntimeReplanner
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from src.orchestration.soft_skill_replanner import SoftSkillBindingReplanner
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.workflow_router import WorkflowRouter
from src.storage import StoragePort
from src.storage.rust_contract import (
    error_policy,
    migration_policy,
    mode_for_component as runtime_sidecar_mode_for_component,
)
from src.storage.runtime_sidecar_facade import (
    ensure_sidecar_write_allowed,
    load_runtime_sidecar_migration_evidence_artifact,
    validate_runtime_sidecar_artifact_provenance,
    validate_runtime_sidecar_response,
)
from src.storage.runtime_sidecar_grpc_client import RuntimeSidecarGrpcClient
from src.storage.runtime_sidecar_shadow import record_runtime_sidecar_shadow_write_sync
from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory
from src.storage.postgres import PostgreSQLStorage, bootstrap_postgres_database, create_postgres_engine, create_postgres_session_factory
from src.storage.postgres.session import validate_mcp_rollout_connection_role
from src.auth.postgres_invalidation_bus import PostgresAuthInvalidationBus
from src.state.runtime_factory import StatePlatformBackend, build_state_platform_runtime_config
from src.storage.artifact_files import LocalArtifactFileStore, parse_file_storage_ref, is_active_skill_output_file
from src.storage.conversation_files import (
    ConversationFileIndexWriter,
    LocalConversationFileStore,
    build_file_upload_message_projection,
)

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
from .table_upload_normalizer import normalize_selected_spreadsheet_sheet, normalize_table_upload
from .upload_store import InMemoryUploadStore, UploadedFileRecord, UploadValidationError, _decode_plain_text_upload


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
        "mcp_dispatch_server_id",
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
        "mcp_execution_mode",
        "mcp_shadow_enabled",
        "mcp_rollout_config_version",
        "mcp_route_reason_code",
        "mcp_rollout_mode",
    }
)


async def _mark_remote_continuation_dispatched(
    storage: StoragePort,
    outbox,
    dispatched_at: datetime,
):
    if outbox.continuation_dispatched_at is not None:
        return outbox
    dispatched = await storage.mark_mcp_remote_task_continuation_dispatched(
        outbox.outbox_id,
        claim_owner=str(outbox.continuation_claim_owner or ""),
        claim_token=str(outbox.continuation_claim_token or ""),
        expected_revision=outbox.continuation_revision,
        dispatched_at=dispatched_at,
    )
    if dispatched is None:
        raise RuntimeError("mcp_continuation_dispatch_receipt_lost")
    return dispatched


def _deserialize_mcp_continuation_plan(payload: Mapping[str, Any]) -> WorkflowPlan:
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise RuntimeError("mcp_continuation_plan_missing")
    nodes: list[WorkflowNodePlan] = []
    for raw in raw_nodes:
        if not isinstance(raw, Mapping):
            raise RuntimeError("mcp_continuation_plan_invalid")
        nodes.append(
            WorkflowNodePlan(
                node_id=str(raw.get("node_id") or ""),
                capability_id=str(raw.get("capability_id") or ""),
                input_payload=dict(raw.get("input_payload") or {}),
                metadata=dict(raw.get("metadata") or {}),
                depends_on=tuple(str(item) for item in raw.get("depends_on") or ()),
                criticality=NodeCriticality(
                    str(raw.get("criticality") or NodeCriticality.REQUIRED.value)
                ),
                retry_policy=dict(raw.get("retry_policy") or {}),
                timeout_policy=dict(raw.get("timeout_policy") or {}),
                resource_class=(
                    str(raw["resource_class"])
                    if raw.get("resource_class") is not None
                    else None
                ),
            )
        )
    return WorkflowPlan(
        task_id=str(payload.get("task_id") or ""),
        nodes=tuple(nodes),
        metadata=dict(payload.get("metadata") or {}),
        max_replans=int(payload.get("max_replans") or 0),
        max_dynamic_nodes=int(payload.get("max_dynamic_nodes") or 0),
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
_INTERRUPT_TURN_LLM_METADATA = {"deep_thinking": True}
_INTERRUPT_OPEN_TURN_PART_KINDS = frozenset(
    {"slot_answer", "skill_question", "off_topic_guidance", "schema_switch", "ambiguous"}
)
_INTERRUPT_SCHEMA_SWITCH_EXECUTION_CONFIDENCE = 0.85
_V2_INTERRUPT_RAW_ANSWER_ALLOWED_KEYS = frozenset(
    {"text", "upload_ids", "sheet_selections", "upload_sheet_selections"}
)
CONVERSATION_FILE_INDEX_REPAIR_REQUIRED_EVENT = "conversation_file.file_upload_index_repair_required"
CONVERSATION_FILE_INDEX_REPAIR_RESOLVED_EVENT = "conversation_file.file_upload_index_repair_resolved"
CONVERSATION_FILE_INDEX_REPAIR_FAILED_EVENT = "conversation_file.file_upload_index_repair_failed"
CONVERSATION_FILE_SELECTOR_CONFIG_INVALID_EVENT = "conversation_file.file_selector_config_invalid"
CONVERSATION_FILE_SELECTOR_ALLOWED_MODES = frozenset(
    {"disabled", "shadow", "enforce_narrow", "enforce_guarded_multi"}
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


@dataclass(frozen=True, slots=True)
class _MCPRolloutInstanceAdmission:
    environment_id: str
    deployment_id: str
    stage: str
    activation_id: str
    instance_id: str


@dataclass(frozen=True, slots=True)
class _MCPShadowNodeObservation:
    node_id: str
    scenario: ShadowScenario
    binding: Any
    legacy_transport: str
    mapping_resolution: RuntimeShadowMappingResolution
    task: asyncio.Task[Any]


@dataclass(frozen=True, slots=True)
class _MCPShadowExecutionHandle:
    request: OrchestrationRequest
    owner_user_id: str
    approved_mappings: tuple[Any, ...]
    observations: tuple[_MCPShadowNodeObservation, ...]


class ApiRuntime(ConversationFileSelectionRuntimeMixin):
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
        conversation_file_store: LocalConversationFileStore | None = None,
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
        user_mcp_config_service: UserMCPConfigService | None = None,
        user_mcp_health_runner: MCPHealthRunner | None = None,
        user_mcp_gateway: MCPGateway | None = None,
        mcp_credential_cipher: CredentialCipher | None = None,
        mcp_remote_task_recovery_worker: MCPRemoteTaskRecoveryWorker | None = None,
        mcp_invalidation_bus: InMemoryMCPInvalidationBus | None = None,
        postgres_mcp_invalidation_bus: PostgresMCPInvalidationBus | None = None,
        user_mcp_result_store: MCPTemporaryResultStore | None = None,
        user_mcp_result_janitor: MCPTemporaryResultJanitor | None = None,
        user_mcp_presence_service: MCPTaskPresenceService | None = None,
        user_mcp_audit_service: MCPAuditService | None = None,
        mcp_shadow_observer: MCPShadowRuntimeObserver | None = None,
        mcp_shadow_manifest: VerifiedShadowScenarioManifest | None = None,
        mcp_shadow_scenario_bindings: Mapping[str, ShadowScenario] | None = None,
        mcp_shadow_manifest_gap_reason: str | None = None,
        user_mcp_routing_enabled: bool = False,
        mcp_rollout_config: MCPRolloutConfig | None = None,
        mcp_rollout_instance_admission: _MCPRolloutInstanceAdmission | None = None,
        mcp_rollout_metric_recorder: MCPRolloutMetricRecorder | None = None,
        mcp_rollout_engine: Engine | None = None,
    ) -> None:
        self._engine = engine
        self._mcp_rollout_engine = mcp_rollout_engine
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
        self.user_mcp_config_service = user_mcp_config_service
        self.user_mcp_health_runner = user_mcp_health_runner
        self.user_mcp_gateway = user_mcp_gateway
        self.mcp_credential_cipher = mcp_credential_cipher
        self.mcp_remote_task_recovery_worker = mcp_remote_task_recovery_worker
        self.mcp_invalidation_bus = mcp_invalidation_bus
        self.postgres_mcp_invalidation_bus = postgres_mcp_invalidation_bus
        self.user_mcp_result_store = user_mcp_result_store
        self.user_mcp_result_janitor = user_mcp_result_janitor
        self.user_mcp_presence_service = user_mcp_presence_service
        self.user_mcp_audit_service = user_mcp_audit_service
        self.mcp_shadow_observer = mcp_shadow_observer
        self._mcp_shadow_manifest = mcp_shadow_manifest
        self._mcp_shadow_scenario_bindings = dict(
            mcp_shadow_scenario_bindings or {}
        )
        self._mcp_shadow_manifest_gap_reason = mcp_shadow_manifest_gap_reason
        self._mcp_shadow_terminal_timeout_seconds = 5.0
        self.user_mcp_routing_enabled = user_mcp_routing_enabled
        self.mcp_rollout_config = mcp_rollout_config or MCPRolloutConfig.from_env({})
        self._mcp_rollout_instance_admission = mcp_rollout_instance_admission
        self._mcp_rollout_metric_recorder = mcp_rollout_metric_recorder
        self._mcp_rollout_zero_series_task: asyncio.Task[None] | None = None
        self._mcp_rollout_instance_lease_created_at: datetime | None = None
        self._mcp_rollout_instance_lease_valid_until: datetime | None = None
        self._mcp_rollout_instance_admission_error: str | None = None
        self._mcp_rollout_instance_lease_task: asyncio.Task[None] | None = None
        self._mcp_rollout_lease_duration_seconds = 60
        self._mcp_rollout_lease_renew_interval_seconds = 20
        self._conversation_title_generator = conversation_title_generator
        self.upload_store = upload_store or InMemoryUploadStore(now_fn=self._utcnow_naive)
        self._conversation_memory_builder = conversation_memory_builder
        self.artifact_file_store = artifact_file_store or LocalArtifactFileStore(Path("runtime/artifacts"))
        self.conversation_file_store = conversation_file_store or LocalConversationFileStore(Path("runtime/conversation_files"))
        self._conversation_file_index_writer = ConversationFileIndexWriter(self.conversation_file_store)
        self._audit_sink = audit_sink
        self._skill_runtime_state = skill_runtime_state
        self._skill_input_text_generator = skill_input_text_generator
        self._mcp_runtime_state = mcp_runtime_state
        self._runtime_sidecar_client = runtime_sidecar_client
        self._model_edition_config = dict(model_edition_config or {})
        validate_model_reasoning_effort_configs(self._model_edition_config)
        self._model_reasoning_configs = model_reasoning_effort_configs(self._model_edition_config)
        self._default_model_edition = default_model_edition(self._model_edition_config)
        self._runtime_sidecar_shadow_sink = _build_runtime_sidecar_shadow_diff_sink(audit_sink)
        configure_safety_shadow_sink(_build_safety_kernel_shadow_diff_sink(audit_sink))
        self._conversation_guard = ConversationSerialGuard(storage)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._conversation_delete_tasks: dict[str, asyncio.Task[dict[str, object]]] = {}
        self._locally_cancelled_task_ids = local_cancelled_task_ids if local_cancelled_task_ids is not None else set()
        self._running_title_tasks: set[asyncio.Task[None]] = set()
        self._running_mcp_shadow_tasks: set[asyncio.Task[None]] = set()
        self._task_skill_bundle_revisions: dict[str, str] = {}
        self._task_mcp_bundle_revisions: dict[str, str] = {}
        self._task_sheet_selection_resume_metadata: dict[str, dict[str, Any]] = {}
        self._task_file_selection_resume_metadata: dict[str, dict[str, Any]] = {}
        self._assistant_history_sync_failure_task_ids: set[str] = set()
        self._assistant_history_sync_failure_lock = asyncio.Lock()
        self._mcp_auth_invalidation_queue = None
        self._mcp_auth_invalidation_task: asyncio.Task[None] | None = None
        self._mcp_audit_retention_task: asyncio.Task[None] | None = None
        self._mcp_continuation_consumer_task: asyncio.Task[None] | None = None
        self._mcp_continuation_consumer_id = f"api-continuation:{uuid4().hex}"
        self._lock = asyncio.Lock()
        self._skill_refresh_lock = asyncio.Lock()
        self._mcp_refresh_lock = asyncio.Lock()
        raw_selector_mode = os.environ.get("MAF_CONVERSATION_FILE_SELECTOR_MODE", "disabled")
        self._conversation_file_selector_mode = self._normalize_conversation_file_selector_mode(raw_selector_mode)
        self._record_invalid_conversation_file_selector_mode(raw_selector_mode, self._conversation_file_selector_mode)
        self._conversation_file_selector_guarded_multi_select = (
            self._conversation_file_selector_mode == "enforce_guarded_multi"
        )

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
            "default_model_edition": self._default_model_edition,
            "options": [
                {
                    "value": option.value,
                    "label": option.label,
                    "reasoning_efforts": (
                        {
                            "default": option.reasoning_efforts.default,
                            "disabled_default": option.reasoning_efforts.disabled_default,
                            "options": [
                                {
                                    "value": effort.value,
                                    "label": effort.label,
                                    "allow_when_thinking_disabled": effort.allow_when_thinking_disabled,
                                }
                                for effort in option.reasoning_efforts.options
                            ],
                        }
                        if option.reasoning_efforts is not None
                        else None
                    ),
                }
                for option in options
            ],
        }

    def _validate_requested_model_edition(self, model_edition: str | None) -> str | None:
        return validate_model_edition(model_edition, config=self._model_edition_config)

    def _resolve_llm_request_options(
        self,
        metadata: Mapping[str, Any] | None,
        *,
        fallback_reasoning_effort: ReasoningEffort | None = None,
    ) -> LLMRequestOptions:
        return resolve_llm_request_options(
            metadata,
            fallback_reasoning_effort=fallback_reasoning_effort,
            model_reasoning_configs=self._model_reasoning_configs,
            default_model_edition=self._default_model_edition,
        )

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
                    request_metadata=self._chat_message_request_llm_metadata(request),
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
            request_metadata=self._chat_message_request_llm_metadata(request),
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

    def _chat_message_request_llm_metadata(self, request: SubmitMessageRequest) -> dict[str, object]:
        metadata = self._drop_user_supplied_system_metadata(request.metadata)
        selected_model_edition = self._validate_requested_model_edition(request.model_edition)
        if selected_model_edition:
            metadata["model_edition"] = selected_model_edition
        else:
            metadata.pop("model_edition", None)
        return self._llm_request_metadata(metadata, include_defaults=True)

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
        if interrupt.reason_code == "mcp_tool_approval_required":
            decision = str(request.metadata.get("mcp_tool_approval") or "").strip()
            if decision not in {"allow_once", "always_allow", "deny"}:
                raise ValueError(
                    "mcp_tool_approval must be allow_once, always_allow, or deny"
                )
            return {"mcp_tool_approval": decision}
        if interrupt.reason_code in {
            "mcp_input_required",
            "mcp_remote_task_input_required",
        }:
            if (
                interrupt.reason_code == "mcp_remote_task_input_required"
                and request.metadata.get("mcp_remote_task_cancel") is True
            ):
                return {"mcp_remote_task_cancel": True}
            responses = request.metadata.get("mcp_input_responses")
            if not isinstance(responses, Mapping):
                raise ValueError("mcp_input_responses must be an object")
            return {"mcp_input_responses": dict(responses)}
        if interrupt.reason_code == "file_selection_ambiguous":
            answer: dict[str, object] = {"text": request.content}
            if upload_ids:
                answer["upload_ids"] = list(upload_ids)
            return {
                "client_request_id": client_request_id,
                "answer": answer,
                "upload_ids": list(upload_ids),
                "file_selection_answer": request.content,
            }
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
        self._ensure_mcp_rollout_instance_admitted()
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
        requested_descriptor = (
            self.capability_registry.get(requested_capability_id)
            if requested_capability_id is not None
            else None
        )
        explicit_legacy_capability = (
            requested_descriptor is not None
            and _is_mcp_descriptor(requested_descriptor)
        )
        has_user_scoped_server = bool(
            await self.storage.list_user_mcp_servers(authenticated_username)
        )
        mcp_assignment = self.mcp_rollout_config.assign_authenticated_user(
            authenticated_username,
            has_user_scoped_server=has_user_scoped_server,
            explicit_legacy_capability=explicit_legacy_capability,
        )
        if mcp_assignment.real_path is MCPExecutionPath.LEGACY:
            await self._refresh_mcp_for_new_conversation_if_needed(
                conversation_id,
                existing_conversation,
            )
        self._ensure_mcp_capability_matches_assignment(
            requested_capability_id,
            execution_mode=mcp_assignment.real_path.value,
        )

        upload_context = await self.resolve_uploads_for_message(
            conversation_id,
            authenticated_username,
            request.metadata.get("upload_ids") or (),
            upload_sheet_selections=request.metadata.get("upload_sheet_selections"),
        )
        self._raise_missing_uploads(upload_context.get("missing_upload_ids"), context="message submission")
        conversation_upload_context = await self.resolve_conversation_uploads_for_message(
            conversation_id,
            authenticated_username,
            upload_sheet_selections=request.metadata.get("upload_sheet_selections"),
        )
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
            mcp_execution_mode=mcp_assignment.real_path.value,
            mcp_shadow_enabled=mcp_assignment.shadow_enabled,
            mcp_rollout_config_version=mcp_assignment.config_version,
            mcp_route_reason_code=mcp_assignment.reason_code.value,
            mcp_rollout_mode=mcp_assignment.routing_mode.value,
        )
        await self.storage.save_task(task)
        await self._record_mcp_route_assignment_metric(task)
        await self._record_event(
            self._make_event(
                task_id=task_id,
                conversation_id=conversation_id,
                event_type="task.accepted",
                payload={
                    "message_id": message_id,
                    "status": str(task.status),
                    **self._llm_request_metadata(
                        self._accepted_task_llm_source_metadata(request.metadata, selected_model_edition),
                        include_defaults=True,
                    ),
                    **({"model_edition": selected_model_edition} if selected_model_edition else {}),
                },
                created_at=now,
            )
        )
        await self._record_event(
            self._make_event(
                task_id=task_id,
                conversation_id=conversation_id,
                event_type="mcp.rollout.route_assigned",
                payload={
                    **(
                        {
                            "safe_owner_ref": self.mcp_credential_cipher.safe_owner_reference(
                                authenticated_username,
                                context=mcp_assignment.config_version,
                            )
                        }
                        if self.mcp_credential_cipher is not None
                        else {}
                    ),
                    "safe_task_ref": hashlib.sha256(
                        f"{mcp_assignment.config_version}:{task_id}".encode("utf-8")
                    ).hexdigest(),
                    "real_path": mcp_assignment.real_path.value,
                    "shadow_enabled": mcp_assignment.shadow_enabled,
                    "config_version": mcp_assignment.config_version,
                    "reason_code": mcp_assignment.reason_code.value,
                    "rollout_mode": mcp_assignment.routing_mode.value,
                },
                visibility=EventVisibility.AUDIT_ONLY,
                created_at=now,
            )
        )
        if mcp_assignment.real_path is MCPExecutionPath.UNAVAILABLE:
            await self._record_event(
                self._make_event(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    event_type="mcp.runtime_unavailable",
                    payload={
                        "status": "unavailable",
                        "reason_code": mcp_assignment.reason_code.value,
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
            fallback_metadata = sanitize_capability_missing_fallback_metadata(
                soft_skill_binding.get(CAPABILITY_MISSING_FALLBACK_KEY),
                mode="history",
            )
            if fallback_metadata is not None:
                metadata[CAPABILITY_MISSING_FALLBACK_KEY] = fallback_metadata
        if selected_model_edition:
            metadata["model_edition"] = selected_model_edition
        else:
            metadata.pop("model_edition", None)
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
        metadata.update(self._mcp_task_assignment_metadata(task))
        if (
            task.mcp_execution_mode == MCPExecutionPath.LEGACY.value
            and self._mcp_runtime_state is not None
        ):
            metadata["mcp_bundle_revision"] = self._mcp_runtime_state.active_revision
        upload_ids = request.metadata.get("upload_ids") or ()
        explicit_upload_ids = self._normalize_upload_ids(upload_ids)
        if upload_context["uploaded_artifacts"]:
            await self._bind_task_input_uploads(
                task=task,
                username=authenticated_username,
                upload_ids=upload_ids,
                source_kind="message_upload",
                source_message_id=message_id,
                upload_sheet_selections=request.metadata.get("upload_sheet_selections"),
            )
        metadata.update(self._upload_context_metadata(conversation_upload_context))
        should_run_file_selector = not explicit_upload_ids and self._conversation_file_selector_mode != "disabled"
        if should_run_file_selector and await self._maybe_handle_conversation_file_selection(
            task=task,
            username=authenticated_username,
            request=request,
            metadata=metadata,
            requested_capability_id=requested_capability_id,
            continued_pending_context=continued_pending_context,
            explicit_upload_ids=explicit_upload_ids,
        ):
            return message, task
        if conversation_upload_context.get("pending_sheet_selections"):
            await self._open_sheet_selection_interrupt(
                task=task,
                metadata=metadata,
                pending_sheet_selections=conversation_upload_context["pending_sheet_selections"],
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
            available_mcp_servers=await self.available_user_mcp_server_profiles(
                authenticated_username,
                execution_mode=task.mcp_execution_mode,
            ),
        )
        await self._schedule_execution(orchestration_request)
        return message, task

    async def _record_mcp_route_assignment_metric(self, task: Task) -> None:
        recorder = self._mcp_rollout_metric_recorder
        if recorder is None:
            return
        try:
            execution_path = MCPMetricExecutionPath(
                task.mcp_execution_mode or MCPExecutionPath.UNAVAILABLE.value
            )
            routing_mode = MCPMetricRoutingMode(
                task.mcp_rollout_mode or MCPRoutingMode.OFF.value
            )
            result_category = (
                MCPMetricResultCategory.FAILED
                if execution_path is MCPMetricExecutionPath.UNAVAILABLE
                else MCPMetricResultCategory.SUCCEEDED
            )
            observed_at = task.created_at or self._utcnow_naive()
            observed_at = (
                observed_at.replace(tzinfo=timezone.utc)
                if observed_at.tzinfo is None
                else observed_at.astimezone(timezone.utc)
            )
            bucket_started_at = observed_at.replace(second=0, microsecond=0)
            await recorder.record_count(
                MCPMetricName.ROUTE_REQUESTS_TOTAL,
                labels=MCPMetricLabels(
                    execution_path=execution_path,
                    routing_mode=routing_mode,
                    result_category=result_category,
                    error_category=(
                        MCPMetricErrorCategory.UNKNOWN
                        if result_category is MCPMetricResultCategory.FAILED
                        else MCPMetricErrorCategory.NONE
                    ),
                ),
                bucket_started_at=bucket_started_at,
                bucket_ended_at=bucket_started_at + timedelta(minutes=1),
            )
        except Exception:
            if self._audit_sink is None:
                return
            try:
                self._audit_sink.record_sync(
                    "mcp.rollout_metric_gap",
                    {
                        "metric_family": "route_assignment",
                        "gap_reason": "route_assignment_recording_failed",
                    },
                )
            except Exception:
                return

    async def available_user_mcp_server_profiles(
        self,
        owner_user_id: str,
        *,
        execution_mode: str | None = None,
    ) -> tuple[UserMCPServerProfile, ...]:
        if (
            execution_mode is not None
            and execution_mode != MCPExecutionPath.USER_SCOPED.value
        ):
            return ()
        if not self.user_mcp_routing_enabled or self.user_mcp_config_service is None:
            return ()
        servers = await self.user_mcp_config_service.list_servers(owner_user_id)
        return tuple(
            UserMCPServerProfile(
                server_id=server.server_id,
                display_name=server.display_name,
                routing_description=server.routing_description,
                transport=str(server.transport),
            )
            for server in servers
            if server.enabled
            and str(server.health_status) == "available"
            and not server.deletion_pending
        )

    async def _begin_mcp_shadow_observation(
        self,
        *,
        request: OrchestrationRequest,
        plan: WorkflowPlan,
    ) -> _MCPShadowExecutionHandle | None:
        if (
            request.metadata.get("mcp_shadow_enabled") is not True
            or request.metadata.get("mcp_execution_mode")
            != MCPExecutionPath.LEGACY.value
        ):
            return None
        if self._mcp_shadow_manifest is None:
            await self._record_mcp_shadow_setup_failure(
                request,
                reason_code=(
                    self._mcp_shadow_manifest_gap_reason
                    or "shadow_verified_manifest_missing"
                ),
            )
            return None
        if (
            self.mcp_shadow_observer is None
            or self._mcp_runtime_state is None
            or self.user_mcp_config_service is None
        ):
            await self._record_mcp_shadow_setup_failure(
                request,
                reason_code="shadow_runtime_unavailable",
            )
            return None
        revision = str(
            request.metadata.get("mcp_bundle_revision") or ""
        ).strip()
        if not revision:
            await self._record_mcp_shadow_setup_failure(
                request,
                reason_code="shadow_pinned_revision_missing",
            )
            return None
        try:
            bundle = self._mcp_runtime_state.bundle_for_revision(revision)
            selected_nodes = tuple(
                node for node in plan.nodes if node.capability_id in bundle.bindings
            )
            if not selected_nodes:
                return None
            legacy_server_configs = tuple(self._mcp_runtime_state.config.servers)
        except Exception:
            await self._record_mcp_shadow_setup_failure(
                request,
                reason_code="shadow_pinned_revision_unavailable",
            )
            return None
        try:
            task = await self.storage.get_task(request.task_id)
            conversation = await self.storage.get_conversation(
                request.conversation_id
            )
            if (
                task is None
                or task.conversation_id != request.conversation_id
                or task.mcp_shadow_enabled is not True
                or task.mcp_execution_mode != MCPExecutionPath.LEGACY.value
                or task.mcp_rollout_mode != MCPRoutingMode.SHADOW.value
                or request.metadata.get("mcp_rollout_config_version")
                != task.mcp_rollout_config_version
                or conversation is None
                or not conversation.username
            ):
                return None
            user_servers = tuple(
                await self.user_mcp_config_service.list_servers(
                    conversation.username
                )
            )
            target_credential_digests = await self._mcp_shadow_credential_digests(
                user_servers
            )
            profiles = tuple(
                UserMCPServerProfile(
                    server_id=server.server_id,
                    display_name=server.display_name,
                    routing_description=server.routing_description,
                    transport=str(server.transport),
                )
                for server in user_servers
                if server.enabled
                and str(server.health_status) == "available"
                and not server.deletion_pending
            )
            legacy_servers = {
                server.server_id: server
                for server in legacy_server_configs
            }
            config_fingerprint = self._mcp_shadow_manifest.manifest.config_fingerprint
            all_mapping_resolutions: dict[str, RuntimeShadowMappingResolution] = {}
            for legacy_server in legacy_server_configs:
                all_mapping_resolutions[legacy_server.server_id] = (
                    resolve_approved_migration_mapping(
                        legacy_server_id=legacy_server.server_id,
                        owner_user_id=conversation.username,
                        legacy_server=legacy_server,
                        user_servers=user_servers,
                        target_credential_digests=target_credential_digests,
                        config_fingerprint=config_fingerprint,
                    )
                )
            approved_mappings = tuple(
                resolution.mapping
                for resolution in all_mapping_resolutions.values()
                if resolution.mapping is not None
            )
            if (
                approved_shadow_mapping_set_fingerprint(approved_mappings)
                != self._mcp_shadow_manifest.manifest.mapping_fingerprint
            ):
                await self._record_mcp_shadow_setup_failure(
                    request,
                    reason_code="shadow_mapping_set_fingerprint_mismatch",
                )
                return None
            observations: list[_MCPShadowNodeObservation] = []
            for node in selected_nodes:
                scenario = self._mcp_shadow_scenario_bindings.get(
                    node.capability_id
                )
                if not isinstance(scenario, ShadowScenario):
                    await self._record_mcp_shadow_setup_failure(
                        request,
                        reason_code="shadow_scenario_binding_missing",
                    )
                    continue
                binding = bundle.bindings[node.capability_id]
                legacy_server = legacy_servers.get(binding.server_id)
                mapping_resolution = all_mapping_resolutions.get(
                    binding.server_id,
                    RuntimeShadowMappingResolution(
                        None,
                        ("legacy_source_config_missing",),
                    ),
                )
                legacy_transport = (
                    legacy_server.transport
                    if legacy_server is not None
                    else "not_applicable"
                )
                observer_task = asyncio.create_task(
                    self.mcp_shadow_observer.compare_task(
                        owner_user_id=conversation.username,
                        task_id=task.task_id,
                        user_request=request.effective_user_message,
                        profiles=profiles,
                        legacy_binding=binding,
                        legacy_server_bindings=tuple(
                            candidate
                            for candidate in bundle.bindings.values()
                            if candidate.server_id == binding.server_id
                        ),
                        legacy_transport=legacy_transport,
                        legacy_endpoint_url=(
                            legacy_server.endpoint
                            if legacy_server is not None
                            else None
                        ),
                        mapping=mapping_resolution.mapping,
                        config_fingerprint=config_fingerprint,
                        mapping_blockers=mapping_resolution.blockers,
                    ),
                    name=f"mcp-shadow-observe:{request.task_id}:{node.node_id}",
                )
                self._running_mcp_shadow_tasks.add(observer_task)
                observer_task.add_done_callback(
                    self._running_mcp_shadow_tasks.discard
                )
                observations.append(
                    _MCPShadowNodeObservation(
                        node_id=node.node_id,
                        scenario=scenario,
                        binding=binding,
                        legacy_transport=legacy_transport,
                        mapping_resolution=mapping_resolution,
                        task=observer_task,
                    )
                )
            if not observations:
                return None
            await asyncio.sleep(0)
            return _MCPShadowExecutionHandle(
                request=request,
                owner_user_id=conversation.username,
                approved_mappings=approved_mappings,
                observations=tuple(observations),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Shadow is observational: planner/executor output remains authoritative.
            await self._record_mcp_shadow_setup_failure(
                request,
                reason_code="shadow_observation_setup_failed",
            )
            return None

    async def _finish_mcp_shadow_observation(
        self,
        handle: _MCPShadowExecutionHandle | None,
        result: OrchestrationRunResult | None,
    ) -> None:
        if handle is None:
            return
        tasks = tuple(item.task for item in handle.observations)
        try:
            observed = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._mcp_shadow_terminal_timeout_seconds,
            )
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._record_mcp_shadow_setup_failure(
                handle.request,
                reason_code="shadow_observation_timeout",
            )
            return
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        terminal_nodes = {
            str(getattr(node, "node_id", "")): node
            for node in (() if result is None else result.nodes)
        }
        for context, shadow_result in zip(
            handle.observations, observed, strict=True
        ):
            if isinstance(shadow_result, BaseException):
                await self._record_mcp_shadow_setup_failure(
                    handle.request,
                    reason_code="shadow_observer_failed",
                )
                continue
            await self._record_terminal_mcp_shadow_sample(
                handle=handle,
                context=context,
                shadow_result=shadow_result,
                terminal_node=terminal_nodes.get(context.node_id),
            )

    async def _record_terminal_mcp_shadow_sample(
        self,
        *,
        handle: _MCPShadowExecutionHandle,
        context: _MCPShadowNodeObservation,
        shadow_result: Any,
        terminal_node: Any | None,
    ) -> None:
        manifest = self._mcp_shadow_manifest
        audit_service = self.user_mcp_audit_service
        observation = getattr(shadow_result, "observation", None)
        legacy_summary = getattr(shadow_result, "legacy_summary", None)
        if (
            manifest is None
            or audit_service is None
        ):
            await self._record_mcp_shadow_setup_failure(
                handle.request,
                reason_code="shadow_terminal_evidence_incomplete",
            )
            return
        if observation is None or legacy_summary is None:
            comparison = getattr(
                shadow_result,
                "comparison",
                ShadowComparison.NOT_COMPARABLE,
            )
            blockers = tuple(getattr(shadow_result, "blockers", ()))
            await self._record_mcp_shadow_terminal_comparison_event(
                handle.request,
                node_id=context.node_id,
                comparison=comparison,
                blockers=blockers,
                result_category=comparison.value,
            )
            await self._record_mcp_shadow_setup_failure(
                handle.request,
                reason_code="shadow_terminal_evidence_incomplete",
            )
            return

        expected = manifest.manifest.expectation_for(context.scenario)
        legacy_outcome, terminal = await self._mcp_shadow_legacy_outcome(
            handle.request.task_id,
            context.node_id,
            terminal_node,
            excluded_fallback=expected.legacy_outcome,
        )
        nonce = hashlib.sha256(
            (
                f"{manifest.fingerprint}:{handle.request.task_id}:"
                f"{context.node_id}"
            ).encode("utf-8")
        ).hexdigest()
        try:
            compared = compare_live_shadow_sample(
                verified_manifest=manifest,
                scenario=context.scenario,
                nonce=nonce,
                legacy_outcome=legacy_outcome,
                observation=observation,
                legacy_summary=legacy_summary,
                legacy_route=str(
                    getattr(context.binding, "server_id", "") or ""
                ),
                mapping=getattr(shadow_result, "mapping", None),
                approved_mappings=handle.approved_mappings,
                terminal=terminal,
            )
        except Exception:
            await self._record_mcp_shadow_setup_failure(
                handle.request,
                reason_code="shadow_terminal_comparison_failed",
            )
            return

        observed_at = datetime.now(timezone.utc)
        sample_id = "mcp-shadow-" + hashlib.sha256(
            (
                f"{manifest.fingerprint}:{handle.request.task_id}:"
                f"{context.node_id}:{nonce}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        admission = self._mcp_rollout_instance_admission
        transport = str(observation.summary.transport or "").strip()
        endpoint_policy = str(
            observation.summary.endpoint_policy or ""
        ).strip()
        if (
            admission is None
            or admission.stage != "internal_shadow"
            or not transport
            or not endpoint_policy
        ):
            await self._record_mcp_shadow_setup_failure(
                handle.request,
                reason_code="shadow_audit_scope_unavailable",
            )
            return
        sample = seal_shadow_audit_sample(
            MCPShadowAuditSample(
                sample_id=sample_id,
                environment_id=admission.environment_id,
                deployment_id=admission.deployment_id,
                stage=admission.stage,
                config_fingerprint=manifest.manifest.config_fingerprint,
                manifest_fingerprint=manifest.fingerprint,
                fixture_fingerprint=manifest.manifest.fixture_fingerprint,
                mapping_fingerprint=manifest.manifest.mapping_fingerprint,
                scenario=context.scenario.value,
                nonce=nonce,
                legacy_outcome=legacy_outcome.value,
                shadow_outcome=observation.outcome.value,
                transport=transport,
                endpoint_policy=endpoint_policy,
                comparison=compared.result.comparison.value,
                blockers=compared.result.blockers,
                payload_digest="",
                observed_at=observed_at,
                recorded_at=observed_at,
                expires_at=observed_at + MCP_SHADOW_SAMPLE_RETENTION,
                safe_owner_ref=None,
                safe_task_ref=None,
            )
        )
        await self._record_mcp_shadow_terminal_comparison_event(
            handle.request,
            node_id=context.node_id,
            comparison=compared.result.comparison,
            blockers=compared.result.blockers,
            result_category=observation.outcome.value,
        )
        try:
            await audit_service.record_shadow_sample(sample)
        except Exception:
            await self._record_mcp_shadow_setup_failure(
                handle.request,
                reason_code="shadow_sample_persistence_failed",
            )
            return

    async def _mcp_shadow_legacy_outcome(
        self,
        task_id: str,
        node_id: str,
        terminal_node: Any | None,
        *,
        excluded_fallback: ShadowOutcome,
    ) -> tuple[ShadowOutcome, bool]:
        if terminal_node is None:
            return excluded_fallback, False
        status = getattr(terminal_node, "status", None)
        try:
            events = await self.storage.list_events_for_task_filtered(
                task_id,
                event_types={
                    "mcp.tool_call_completed",
                    "mcp.tool_call_failed",
                },
                node_id=node_id,
                visibility=EventVisibility.AUDIT_ONLY,
            )
        except Exception:
            return excluded_fallback, False
        if len(events) != 1:
            return excluded_fallback, False
        event = events[0]
        if status == NodeStatus.COMPLETED and event.event_type == "mcp.tool_call_completed":
            output_size = event.payload.get("output_size_bytes")
            truncated = event.payload.get("truncated")
            if (
                isinstance(output_size, int)
                and output_size >= 0
                and truncated is True
            ):
                return ShadowOutcome.TOOL_CALL_SUCCEEDED_LARGE_RESULT, True
            return ShadowOutcome.TOOL_CALL_SUCCEEDED, True
        if status != NodeStatus.FAILED:
            return excluded_fallback, False
        error_code = str(event.payload.get("error_code") or "").strip()
        closed_errors = {
            "mcp_auth_required": ShadowOutcome.AUTHENTICATION_FAILED,
            "mcp_timeout": ShadowOutcome.TIMEOUT,
            "mcp_permission_denied": ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
        }
        outcome = closed_errors.get(error_code)
        if outcome is not None:
            return outcome, True
        # Generic error_type/status values are not closed terminal evidence.
        return excluded_fallback, False

    async def _record_mcp_shadow_terminal_comparison_event(
        self,
        request: OrchestrationRequest,
        *,
        node_id: str,
        comparison: ShadowComparison,
        blockers: tuple[str, ...],
        result_category: str,
    ) -> None:
        metric_recorded = True
        if comparison is ShadowComparison.MISMATCHED:
            metric_recorded = await self._record_mcp_shadow_mismatch_metric()
        payload: dict[str, Any] = {
            "safe_task_ref": hashlib.sha256(
                f"{self.mcp_rollout_config.fingerprint}:{request.task_id}".encode(
                    "utf-8"
                )
            ).hexdigest(),
            "config_version": self.mcp_rollout_config.fingerprint,
            "rollout_mode": MCPRoutingMode.SHADOW.value,
            "diff_category": comparison.value,
            "result_category": result_category,
            "status": comparison.value,
        }
        if blockers:
            payload["reason_code"] = blockers[0]
        try:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    node_id=node_id,
                    event_type="mcp.rollout.shadow_compared",
                    payload=payload,
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        except Exception:
            self._record_mcp_shadow_audit_fallback(payload)
        if comparison is ShadowComparison.MISMATCHED and not metric_recorded:
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_mismatch_recording_failed"
            )

    async def _mcp_shadow_credential_digests(
        self,
        user_servers: tuple[Any, ...],
    ) -> dict[str, str]:
        cipher = self.mcp_credential_cipher
        if cipher is None:
            return {}
        digests: dict[str, str] = {}
        for server in user_servers:
            metadata = getattr(server, "auth_metadata", {})
            provenance = (
                metadata.get("migration_provenance")
                if isinstance(metadata, Mapping)
                else None
            )
            if not isinstance(provenance, Mapping):
                continue
            source_fingerprint = str(
                provenance.get("source_fingerprint") or ""
            ).strip()
            try:
                credential = await self.storage.get_user_mcp_credential(
                    server.owner_user_id,
                    server.server_id,
                )
            except Exception:
                continue
            digest = migration_target_credential_digest(
                cipher,
                server=server,
                credential_record=credential,
                source_fingerprint=source_fingerprint,
            )
            if digest is not None:
                digests[server.server_id] = digest
        return digests

    async def _compare_and_record_mcp_shadow_route(
        self,
        *,
        request: OrchestrationRequest,
        task: Task,
        node_id: str,
        owner_user_id: str,
        profiles: tuple[UserMCPServerProfile, ...],
        binding: Any,
        server_bindings: tuple[Any, ...],
        legacy_transport: str,
        legacy_endpoint_url: str | None = None,
        mapping_resolution: RuntimeShadowMappingResolution,
        config_fingerprint: str,
    ) -> None:
        try:
            result = await self.mcp_shadow_observer.compare_task(
                owner_user_id=owner_user_id,
                task_id=task.task_id,
                user_request=request.effective_user_message,
                profiles=profiles,
                legacy_binding=binding,
                legacy_server_bindings=server_bindings,
                legacy_transport=legacy_transport,
                legacy_endpoint_url=legacy_endpoint_url,
                mapping=mapping_resolution.mapping,
                config_fingerprint=config_fingerprint,
                mapping_blockers=mapping_resolution.blockers,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_observer_failed"
            )
            return

        metric_recorded = True
        if result.comparison is ShadowComparison.MISMATCHED:
            metric_recorded = await self._record_mcp_shadow_mismatch_metric()
        observation = result.observation
        result_category = (
            observation.outcome.value
            if observation is not None
            else result.comparison.value
        )
        reason_code = result.blockers[0] if result.blockers else None
        error_code = (
            observation.error_code
            if observation is not None and observation.error_code
            else reason_code
        )
        payload: dict[str, Any] = {
            "safe_task_ref": hashlib.sha256(
                f"{config_fingerprint}:{task.task_id}".encode("utf-8")
            ).hexdigest(),
            "config_version": config_fingerprint,
            "rollout_mode": task.mcp_rollout_mode,
            "diff_category": result.comparison.value,
            "result_category": result_category,
            "status": result.comparison.value,
        }
        if reason_code:
            payload["reason_code"] = reason_code
        if error_code:
            payload["error_code"] = error_code
        try:
            await self._record_event(
                self._make_event(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    node_id=node_id,
                    event_type="mcp.rollout.shadow_compared",
                    payload=payload,
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        except Exception:
            self._record_mcp_shadow_audit_fallback(
                payload,
            )
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_comparison_event_recording_failed"
            )
        if result.comparison is ShadowComparison.MISMATCHED and not metric_recorded:
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_mismatch_recording_failed"
            )

    async def _record_mcp_shadow_setup_failure(
        self,
        request: OrchestrationRequest,
        *,
        reason_code: str,
    ) -> None:
        del request
        await self._record_mcp_shadow_metric_gap(reason_code=reason_code)

    async def _record_mcp_shadow_mismatch_metric(self) -> bool:
        recorder = self._mcp_rollout_metric_recorder
        if recorder is None:
            return False
        try:
            await recorder.record_shadow_mismatch()
        except Exception:
            return False
        return True

    def _record_mcp_shadow_audit_fallback(
        self,
        payload: Mapping[str, Any],
    ) -> None:
        if self._audit_sink is None:
            return
        fallback = dict(payload)
        try:
            self._audit_sink.record_sync(
                "mcp.rollout.shadow_compared",
                fallback,
            )
        except Exception:
            return

    async def _record_mcp_shadow_metric_gap(self, *, reason_code: str) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink.record_sync(
                "mcp.rollout_metric_gap",
                {
                    "metric_family": "route_shadow_mismatch",
                    "gap_reason": reason_code,
                },
            )
        except Exception:
            return

    @staticmethod
    def _drop_user_supplied_system_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(metadata)
        for key in USER_SUPPLIED_METADATA_DENYLIST:
            values.pop(key, None)
        return values

    def _accepted_task_llm_source_metadata(
        self,
        metadata: Mapping[str, Any],
        selected_model_edition: str | None,
    ) -> dict[str, Any]:
        values = self._drop_user_supplied_system_metadata(metadata)
        if selected_model_edition:
            values["model_edition"] = selected_model_edition
        else:
            values.pop("model_edition", None)
        return values

    def _llm_request_metadata(
        self,
        metadata: Mapping[str, Any] | None,
        *,
        include_defaults: bool = False,
    ) -> dict[str, object]:
        values = metadata if isinstance(metadata, Mapping) else {}
        result: dict[str, object] = {}
        thinking_was_explicit = "deep_thinking" in values or "main_agent_thinking_enabled" in values
        effort_was_explicit = "main_agent_reasoning_effort" in values
        if include_defaults or thinking_was_explicit or effort_was_explicit or "model_edition" in values:
            options = self._resolve_llm_request_options(values)
            if options.model_edition:
                result["model_edition"] = options.model_edition
            if include_defaults or thinking_was_explicit:
                result["deep_thinking"] = options.thinking
                result["main_agent_thinking_enabled"] = options.thinking
            if include_defaults or thinking_was_explicit or effort_was_explicit:
                if options.requested_reasoning_effort is not None:
                    result["requested_reasoning_effort"] = options.requested_reasoning_effort
                result["main_agent_reasoning_effort"] = options.reasoning_effort
        return result

    async def _task_accepted_llm_metadata(self, task_id: str) -> dict[str, object]:
        events = await self.storage.list_events_for_task(task_id)
        for event in events:
            if event.event_type != "task.accepted":
                continue
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            return self._llm_request_metadata(payload, include_defaults=False)
        return {}

    async def _resume_llm_metadata(
        self,
        task: Task,
        request_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        metadata = await self._task_accepted_llm_metadata(task.task_id)
        if request_metadata is not None:
            metadata.update(self._llm_request_metadata(request_metadata, include_defaults=True))
        return metadata

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
        unavailable = descriptor is None or not descriptor.public or not _is_skill_descriptor(descriptor)
        contract = None
        revision = ""
        if self._skill_runtime_state is not None:
            bundle = self._skill_runtime_state.active_bundle
            revision = bundle.revision
            if capability_id not in bundle.skill_capabilities.skill_name_by_capability_id:
                unavailable = True
            else:
                contract = bundle.contract_by_capability_id.get(capability_id)
        command = self._metadata_text(raw.get("command")) or self._metadata_text(metadata.get("slash_command")) or ""
        normalized = {
            "capability_id": capability_id,
        }
        if unavailable:
            normalized[CAPABILITY_MISSING_FALLBACK_KEY] = build_capability_missing_fallback_metadata(
                reason_code="skill_missing",
                scope="full",
                missing_capability_summary=f"点名的 Skill 当前未注册或不可用：{capability_id}",
                fallback_content_scope="仅说明该 Skill 缺口并给出可手工复核的通用建议，不执行 Skill 或生成下载文件。",
            )
        if contract is not None:
            normalized.update(self._soft_skill_file_selection_metadata(contract))
        if command:
            normalized["command"] = command
        if revision:
            normalized["skill_bundle_revision"] = revision
        return normalized

    @staticmethod
    def _soft_skill_file_selection_metadata(contract: Any, *, source: str = "soft_skill_binding") -> dict[str, Any]:
        file_selection: dict[str, Any] = {}
        context_notes: list[str] = []
        contract_selection = getattr(contract, "file_selection", None)
        if contract_selection is not None:
            if getattr(contract_selection, "required", False):
                file_selection["required"] = True
            if getattr(contract_selection, "allow_multiple", False):
                file_selection["allow_multiple"] = True
            if getattr(contract_selection, "expected_content", ()):
                file_selection.setdefault("expected_content", []).extend(contract_selection.expected_content)
            if getattr(contract_selection, "supported_file_types", ()):
                file_selection.setdefault("supported_file_types", []).extend(contract_selection.supported_file_types)
            if getattr(contract_selection, "helpful_columns", ()):
                file_selection.setdefault("helpful_columns", []).extend(contract_selection.helpful_columns)
            if getattr(contract_selection, "disambiguation_hint", ""):
                file_selection["disambiguation_hint"] = contract_selection.disambiguation_hint
            if any((
                getattr(contract_selection, "required", False),
                getattr(contract_selection, "allow_multiple", False),
                getattr(contract_selection, "expected_content", ()),
                getattr(contract_selection, "supported_file_types", ()),
                getattr(contract_selection, "helpful_columns", ()),
                getattr(contract_selection, "disambiguation_hint", ""),
            )):
                context_notes.append("Skill contract declares final file_selection.")
        try:
            schemas = load_input_schemas_for_contract(contract)
        except Exception:
            schemas = {}
        expected: list[str] = list(file_selection.get("expected_content") or [])
        supported: list[str] = list(file_selection.get("supported_file_types") or [])
        helpful_columns: list[str] = list(file_selection.get("helpful_columns") or [])
        hints: list[str] = [str(file_selection.get("disambiguation_hint") or "").strip()] if file_selection.get("disambiguation_hint") else []
        for schema in schemas.values():
            for input_field in schema.inputs.values():
                field_selection = input_field.file_selection
                has_selection_metadata = any((
                    field_selection.required,
                    field_selection.allow_multiple,
                    field_selection.expected_content,
                    field_selection.supported_file_types,
                    field_selection.helpful_columns,
                    field_selection.disambiguation_hint,
                ))
                if not has_selection_metadata:
                    continue
                if field_selection.required:
                    file_selection["required"] = True
                if field_selection.allow_multiple:
                    file_selection["allow_multiple"] = True
                expected.extend(field_selection.expected_content)
                supported.extend(field_selection.supported_file_types)
                helpful_columns.extend(field_selection.helpful_columns)
                if field_selection.disambiguation_hint:
                    hints.append(field_selection.disambiguation_hint)
                context_notes.append(f"Input schema {schema.schema_id} field {input_field.name} declares final file_selection.")
        if expected:
            file_selection["expected_content"] = list(dict.fromkeys(str(item).strip() for item in expected if str(item).strip()))
        if supported:
            file_selection["supported_file_types"] = list(dict.fromkeys(str(item).strip() for item in supported if str(item).strip()))
        if helpful_columns:
            file_selection["helpful_columns"] = list(dict.fromkeys(str(item).strip() for item in helpful_columns if str(item).strip()))
        if hints:
            file_selection["disambiguation_hint"] = "；".join(dict.fromkeys(str(item).strip() for item in hints if str(item).strip()))
        if context_notes:
            file_selection["context_notes"] = list(dict.fromkeys(context_notes))
        if file_selection:
            file_selection["source"] = source
        return {"file_selection": file_selection} if file_selection else {}

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
        if existing_conversation is not None:
            await self._repair_conversation_file_index_if_due(conversation_id, username)
        now = self._utcnow_naive()
        try:
            self.conversation_file_store.conversation_dir(conversation_id)
        except ValueError as exc:
            raise UploadValidationError("conversation_id failed file storage safety validation") from exc
        record = self.upload_store.save(
            username=username,
            conversation_id=conversation_id,
            filename=filename,
            content_type=content_type,
            content=content,
        )
        try:
            stored = self.conversation_file_store.save_original(
                conversation_id=conversation_id,
                upload_id=record.upload_id,
                content=record.content_bytes,
            )
            description_status, description_summary, description_ref = self._initial_file_description(record)
        except Exception as exc:
            await self._delete_request_upload_artifacts(
                conversation_id=conversation_id,
                username=username,
                upload_id=record.upload_id,
            )
            raise UploadValidationError("Uploaded file failed file storage safety validation") from exc
        resource = ConversationFileResource(
            file_id=record.upload_id,
            conversation_id=conversation_id,
            username=username,
            original_filename=record.filename,
            content_type=record.content_type,
            file_type=record.file_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            storage_key=stored.storage_key,
            preview=dict(record.preview),
            description_status=description_status,
            description_summary=description_summary,
            description_ref=description_ref,
            status="active",
            normalized_filename=record.normalized_filename,
            normalized_content_type=record.normalized_content_type,
            requires_sheet_selection=record.requires_sheet_selection,
            selected_sheet=record.selected_sheet,
            created_at=record.created_at,
            updated_at=now,
        )
        conversation_created = False
        try:
            if existing_conversation is None:
                await self.storage.save_conversation(
                    Conversation(
                        conversation_id=conversation_id,
                        username=username,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conversation_created = True
            await self.storage.save_conversation_file_resource_with_upload_message(
                resource,
                build_file_upload_message_projection(resource),
                now=now,
            )
        except Exception as exc:
            await self._delete_request_upload_artifacts(
                conversation_id=conversation_id,
                username=username,
                upload_id=record.upload_id,
            )
            if conversation_created:
                await self.storage.delete_conversation_physical(conversation_id)
            raise UploadValidationError("Uploaded file could not be persisted") from exc
        try:
            index_ok = await self._rewrite_conversation_file_index_with_repair(
                conversation_id,
                username,
                affected_upload_ids=(record.upload_id,),
                reason_code="upload_index_write_failed",
            )
        except Exception as exc:
            try:
                await self._compensate_failed_upload_after_index_error(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                    reason_code="index_write_failed_marker_failed",
                    now=self._utcnow_naive(),
                )
            finally:
                await self._delete_request_upload_artifacts(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                )
            raise UploadValidationError("Uploaded file could not be indexed") from exc
        if not index_ok:
            try:
                await self._compensate_failed_upload_after_index_error(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                    reason_code="index_write_failed",
                    now=self._utcnow_naive(),
                )
            finally:
                await self._delete_request_upload_artifacts(
                    conversation_id=conversation_id,
                    username=username,
                    upload_id=record.upload_id,
                )
            raise UploadValidationError("Uploaded file could not be indexed")
        if self._audit_sink is not None:
            await self._audit_sink.record(
                "conversation_file.upload_persisted",
                {
                    "upload_id": record.upload_id,
                    "file_type": record.file_type,
                    "size_bytes": record.size_bytes,
                    "description_status": description_status,
                },
                conversation_id=conversation_id,
            )
        return record

    async def ensure_upload_allowed(self, conversation_id: str, username: str) -> Conversation | None:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        return existing_conversation

    async def list_uploads(
        self,
        conversation_id: str,
        username: str,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[UploadedFileRecord]:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        if existing_conversation is not None:
            await self._repair_conversation_file_index_if_due(conversation_id, username)
        resources = await self.storage.list_conversation_file_resources(
            conversation_id,
            username,
            include_deleted=include_deleted,
            limit=limit,
            cursor=cursor,
        )
        return [self._upload_record_from_resource(resource, content_bytes=b"") for resource in resources]

    async def delete_upload(self, conversation_id: str, username: str, upload_id: str) -> bool:
        existing_conversation = await self.storage.get_conversation(conversation_id)
        if existing_conversation is not None and existing_conversation.username != username:
            raise PermissionError(f"Conversation does not belong to username: {conversation_id}")
        if existing_conversation is not None and existing_conversation.status != ConversationStatus.ACTIVE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        if existing_conversation is not None:
            await self._repair_conversation_file_index_if_due(conversation_id, username)
        deleted = await self.storage.mark_conversation_file_resource_and_upload_message_deleted(
            conversation_id,
            username,
            upload_id,
            updated_at=self._utcnow_naive(),
        )
        if deleted is None:
            return False
        local_files_deleted = True
        try:
            self.conversation_file_store.delete_resource_dir(
                conversation_id=deleted.conversation_id,
                upload_id=deleted.file_id,
            )
        except Exception as exc:
            local_files_deleted = False
            if self._audit_sink is not None:
                await self._audit_sink.record(
                    "conversation_file.upload_directory_cleanup_failed",
                    {"upload_id": upload_id, "error_type": exc.__class__.__name__},
                    conversation_id=conversation_id,
                )
        try:
            await self._rewrite_conversation_file_index_with_repair(
                conversation_id,
                username,
                affected_upload_ids=(upload_id,),
                reason_code="delete_index_write_failed",
            )
        except Exception as exc:
            if self._audit_sink is not None:
                await self._audit_sink.record(
                    "conversation_file.delete_index_repair_marker_failed",
                    {"upload_id": upload_id, "error_type": exc.__class__.__name__},
                    conversation_id=conversation_id,
                )
            raise UploadValidationError("Deleted file could not be indexed") from exc
        if self._audit_sink is not None:
            await self._audit_sink.record(
                "conversation_file.delete_marked",
                {"upload_id": upload_id, "local_files_deleted": local_files_deleted},
                conversation_id=conversation_id,
            )
        return True

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
        await self._repair_conversation_file_index_if_due(conversation_id, username)
        sheet_selections = self._normalize_upload_sheet_selections(upload_sheet_selections)
        uploaded_artifacts: list[dict[str, Any]] = []
        skill_artifacts: list[dict[str, Any]] = []
        pending_sheet_selections: list[dict[str, Any]] = []
        missing_upload_ids: list[str] = []
        for upload_id in upload_ids:
            upload_id_text = str(upload_id).strip()
            if not upload_id_text:
                continue
            resource = await self.storage.get_conversation_file_resource(conversation_id, username, upload_id_text)
            if resource is None:
                existing_resource = await self.storage.get_conversation_file_resource_by_id(upload_id_text)
                if existing_resource is not None:
                    raise PermissionError(f"Upload does not belong to conversation: {upload_id_text}")
                missing_upload_ids.append(upload_id_text)
                continue
            if resource.status == "deleted":
                missing_upload_ids.append(upload_id_text)
                continue
            selected_sheet = sheet_selections.get(upload_id_text)
            if selected_sheet:
                resource = await self._apply_conversation_file_sheet_selection(resource, selected_sheet)
            selected_sheet = selected_sheet or resource.selected_sheet
            record = self._upload_record_from_resource(
                resource,
                content_bytes=self.conversation_file_store.read_bytes(resource.storage_key),
                selected_sheet=selected_sheet,
            )
            uploaded_artifacts.append(record.to_summary())
            if record.requires_sheet_selection and not selected_sheet:
                pending_sheet_selections.append(record.sheet_selection_payload())
                skill_artifacts.append(record.to_summary())
                continue
            skill_artifact = record.to_skill_artifact(selected_sheet=selected_sheet)
            skill_artifact["storage_key"] = resource.storage_key
            skill_artifact["conversation_id"] = resource.conversation_id
            skill_artifacts.append(skill_artifact)
        return {
            "uploaded_artifacts": uploaded_artifacts,
            "skill_artifacts": skill_artifacts,
            "missing_upload_ids": missing_upload_ids,
            "pending_sheet_selections": pending_sheet_selections,
        }

    async def resolve_conversation_uploads_for_message(
        self,
        conversation_id: str,
        username: str,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._repair_conversation_file_index_if_due(conversation_id, username)
        resources = await self.storage.list_conversation_file_resources(
            conversation_id,
            username,
            include_deleted=False,
        )
        upload_ids = [resource.file_id for resource in resources if resource.status != "deleted"]
        return await self.resolve_uploads_for_message(
            conversation_id,
            username,
            upload_ids,
            upload_sheet_selections=upload_sheet_selections,
        )

    @staticmethod
    def _upload_context_metadata(upload_context: Mapping[str, Any]) -> dict[str, Any]:
        uploaded_artifacts = [
            dict(item)
            for item in upload_context.get("uploaded_artifacts", [])
            if isinstance(item, Mapping)
        ]
        skill_artifacts = [
            dict(item)
            for item in upload_context.get("skill_artifacts", [])
            if isinstance(item, Mapping)
        ]
        metadata: dict[str, Any] = {}
        if uploaded_artifacts:
            metadata["uploaded_artifacts"] = uploaded_artifacts
        if skill_artifacts:
            metadata["skill_artifacts"] = skill_artifacts
        return metadata

    async def _conversation_file_context_metadata_for_task(
        self,
        task: Task,
        *,
        upload_sheet_selections: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation = await self.storage.get_conversation(task.conversation_id)
        if conversation is None:
            return {}
        upload_context = await self.resolve_conversation_uploads_for_message(
            task.conversation_id,
            conversation.username,
            upload_sheet_selections=upload_sheet_selections,
        )
        return self._upload_context_metadata(upload_context)

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
        await persist_interrupt_question_message(self.storage, saved_interrupt, created_at=now)
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

    async def _schedule_execution(
        self, request: OrchestrationRequest
    ) -> asyncio.Task[None]:
        self._retain_task_skill_revision(request)
        self._retain_task_mcp_revision(request)
        async with self._lock:
            active_task_count = len(self._running_tasks)
            handle = asyncio.create_task(self._run_execution(request, active_task_count=active_task_count))
            self._running_tasks[request.task_id] = handle
            return handle

    async def _run_execution(self, request: OrchestrationRequest, *, active_task_count: int) -> None:
        shadow_handle: _MCPShadowExecutionHandle | None = None
        shadow_finalize_cancelled: asyncio.CancelledError | None = None
        result: OrchestrationRunResult | None = None
        try:
            request = await self._scrub_deleted_file_context_for_execution(request)
            request = await self._attach_conversation_memory(request)
            persisted_continuation_plan = request.metadata.get(
                "mcp_remote_task_continuation_plan"
            )
            if isinstance(persisted_continuation_plan, Mapping):
                plan = _deserialize_mcp_continuation_plan(
                    persisted_continuation_plan
                )
            else:
                plan_result = self.workflow_provider.build_plan(request)
                plan = (
                    await plan_result
                    if inspect.isawaitable(plan_result)
                    else plan_result
                )
            await self._record_plan_built(request, plan)
            shadow_handle = await self._begin_mcp_shadow_observation(
                request=request,
                plan=plan,
            )
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
                try:
                    await self._finish_mcp_shadow_observation(
                        shadow_handle,
                        result,
                    )
                except asyncio.CancelledError as exc:
                    shadow_finalize_cancelled = exc
                    if shadow_handle is not None:
                        pending = tuple(
                            item.task
                            for item in shadow_handle.observations
                            if not item.task.done()
                        )
                        for pending_task in pending:
                            pending_task.cancel()
                        if pending:
                            await asyncio.gather(
                                *pending,
                                return_exceptions=True,
                            )
                except Exception:
                    await self._record_mcp_shadow_setup_failure(
                        request,
                        reason_code="shadow_terminal_finalize_failed",
                    )
                    # Shadow is fail-open, but its readonly sessions must finish
                    # before gateway task scope and pinned revisions are released.
                    if shadow_handle is not None:
                        pending = tuple(
                            item.task
                            for item in shadow_handle.observations
                            if not item.task.done()
                        )
                        for pending_task in pending:
                            pending_task.cancel()
                        if pending:
                            await asyncio.gather(
                                *pending,
                                return_exceptions=True,
                            )
                await self._clear_conversation_current_task(request.conversation_id, request.task_id)
                if self.user_mcp_gateway is not None:
                    try:
                        latest_task = await self.storage.get_task(request.task_id)
                        if latest_task is not None and latest_task.status in {
                            TaskStatus.COMPLETED,
                            TaskStatus.FAILED,
                            TaskStatus.CANCELLED,
                        }:
                            await self.user_mcp_gateway.close_task(
                                request.task_id,
                                "task_terminal",
                            )
                    except Exception:
                        pass
                await self._release_task_skill_revision_if_terminal(request.task_id)
                await self._release_task_mcp_revision_if_terminal(request.task_id)
            finally:
                self._locally_cancelled_task_ids.discard(request.task_id)
                async with self._lock:
                    self._running_tasks.pop(request.task_id, None)
        if shadow_finalize_cancelled is not None:
            raise shadow_finalize_cancelled

    async def _scrub_deleted_file_context_for_execution(self, request: OrchestrationRequest) -> OrchestrationRequest:
        await self._fail_if_effective_uploads_inactive_for_execution(request)
        metadata = dict(request.metadata)
        changed = False
        for key in ("uploaded_artifacts", "skill_artifacts", "artifacts"):
            if key not in metadata:
                continue
            filtered, key_changed = await self._filter_active_upload_artifact_items(
                request.conversation_id,
                metadata.get(key),
            )
            changed = changed or key_changed
            if filtered:
                metadata[key] = filtered
            else:
                metadata.pop(key, None)
        if not changed:
            return request
        return replace(request, metadata=metadata)

    async def _filter_active_upload_artifact_items(
        self,
        conversation_id: str,
        raw_items: object,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(raw_items, list | tuple):
            return [], bool(raw_items)
        conversation = await self.storage.get_conversation(conversation_id)
        username = conversation.username if conversation is not None else None
        filtered: list[dict[str, Any]] = []
        changed = False
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                changed = True
                continue
            item = dict(raw)
            upload_id = str(item.get("upload_id") or item.get("file_id") or "").strip()
            if self._artifact_item_marked_deleted(item):
                changed = True
                continue
            if upload_id:
                if username is None:
                    changed = True
                    continue
                resource = await self.storage.get_conversation_file_resource(conversation_id, username, upload_id)
                if resource is None or resource.status == "deleted":
                    changed = True
                    continue
            filtered.append(item)
        return filtered, changed or len(filtered) != len(raw_items)

    async def _fail_if_effective_uploads_inactive_for_execution(self, request: OrchestrationRequest) -> None:
        upload_ids = set(self._normalize_upload_ids(request.metadata.get("upload_ids") or ()))
        inactive_upload_ids: set[str] = set()
        for attachment in await self.storage.list_task_input_attachments_for_task(request.task_id):
            upload_id = str(attachment.source_upload_id or "").strip()
            if not upload_id:
                continue
            upload_ids.add(upload_id)
            if (
                isinstance(attachment.prompt_artifact, Mapping)
                and self._artifact_item_marked_deleted(attachment.prompt_artifact)
            ) or (
                isinstance(attachment.skill_artifact, Mapping)
                and self._artifact_item_marked_deleted(attachment.skill_artifact)
            ):
                inactive_upload_ids.add(upload_id)
        if not upload_ids:
            return
        conversation = await self.storage.get_conversation(request.conversation_id)
        if conversation is None:
            inactive_upload_ids.update(upload_ids)
        else:
            for upload_id in upload_ids:
                resource = await self.storage.get_conversation_file_resource(
                    request.conversation_id,
                    conversation.username,
                    upload_id,
                )
                if resource is None or resource.status == "deleted":
                    inactive_upload_ids.add(upload_id)
        if inactive_upload_ids:
            missing = ", ".join(sorted(inactive_upload_ids))
            raise UploadValidationError(
                f"Task-bound uploads are no longer available for execution: {missing}. "
                "Please re-upload the file or select another active file."
            )

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
        all_tasks = await self.storage.list_tasks_for_conversation(conversation_id)
        await self._sync_interrupt_visible_messages_for_tasks(all_tasks)
        tasks = [task for task in all_tasks if task.status == TaskStatus.COMPLETED]
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
        task = await self.storage.get_task(task_id)
        if task is not None and task.conversation_id == conversation_id:
            await self._sync_interrupt_visible_messages_for_tasks([task])
        await self._persist_assistant_history_message(task_id, conversation_id)

    async def _sync_interrupt_visible_messages_for_tasks(self, tasks: Iterable[Task]) -> None:
        for task in tasks:
            for interrupt in await self.storage.list_interrupts_for_task(task.task_id):
                await persist_interrupt_question_message(
                    self.storage,
                    interrupt,
                    created_at=interrupt.created_at,
                )

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
        fallback_metadata = await self._assistant_history_fallback_metadata(task_id)
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
            metadata={CAPABILITY_MISSING_FALLBACK_KEY: fallback_metadata} if fallback_metadata is not None else {},
        )
        try:
            await self.storage.save_message(message)
        except Exception:
            if await self.storage.get_message(message_id) is not None:
                return
            raise

    async def _assistant_history_fallback_metadata(self, task_id: str) -> dict[str, Any] | None:
        filtered_reader = getattr(self.storage, "list_events_for_task_filtered", None)
        try:
            if callable(filtered_reader):
                events = await filtered_reader(
                    task_id,
                    event_types={CAPABILITY_MISSING_FALLBACK_EVENT},
                    visibility=EventVisibility.FRONTEND,
                    limit=32,
                )
            else:
                events = await self.storage.list_events_for_task(task_id)
        except Exception:
            return None
        values = [
            event.payload
            for event in events
            if event.event_type == CAPABILITY_MISSING_FALLBACK_EVENT
        ]
        return merge_capability_missing_fallback_metadata(values, mode="history")


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
        if (
            existing_task is not None
            and existing_task.mcp_execution_mode == MCPExecutionPath.LEGACY.value
            and self._mcp_runtime_state is not None
        ):
            try:
                for envelope in await self._mcp_runtime_state.cancel_platform_task(task_id):
                    await self._record_event(
                        self._make_event(
                            task_id=existing_task.task_id,
                            conversation_id=existing_task.conversation_id,
                            event_type=str(envelope.get("event_type") or "mcp.long_task_cancel_requested"),
                            payload=dict(envelope.get("payload") or {}),
                        )
                    )
            except Exception:
                pass
        task = await self.cancellation_service.cancel_task_context(task_id)
        if (
            existing_task is not None
            and existing_task.mcp_execution_mode == MCPExecutionPath.USER_SCOPED.value
            and self.user_mcp_gateway is not None
        ):
            try:
                await self.user_mcp_gateway.close_task(task_id, "task_cancelled")
            except Exception:
                pass
        await self._cancel_active_slot_collections_for_task(task)
        if task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            await self._cancel_existing_execution(task_id)
            restored = await self._restore_cancelled_task_if_requested(task_id, task.conversation_id)
            if restored is not None:
                task = restored
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
            await persist_interrupt_question_message(self.storage, interrupt, created_at=now)
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
        try:
            self.conversation_file_store.delete_conversation_dir(conversation_id)
        except Exception as exc:
            if self._audit_sink is not None:
                await self._audit_sink.record(
                    "conversation_file.directory_cleanup_failed",
                    {"error_type": exc.__class__.__name__},
                    conversation_id=conversation_id,
                )
            raise

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
        request_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        task = await self.storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        interrupt = await self.storage.get_interrupt(interrupt_id)
        if interrupt is None or interrupt.task_id != task_id:
            raise ValueError(f"Unknown interrupt: {interrupt_id}")
        if interrupt.reason_code == "mcp_remote_task_input_required":
            responses = answer_payload.get("mcp_input_responses")
            cancel_requested = answer_payload.get("mcp_remote_task_cancel") is True
            if not cancel_requested and not isinstance(responses, Mapping):
                raise ValueError(
                    "mcp_input_responses must be an object for remote task input"
                )
            answer = InterruptAnswer(
                interrupt_answer_id=self._make_id("interrupt-answer"),
                interrupt_id=interrupt_id,
                answer_payload=dict(answer_payload),
                source_message_id=source_message_id or self._make_id("msg"),
                accepted=True,
                created_at=self._utcnow_naive(),
                accepted_at=self._utcnow_naive(),
            )
            command = await self.interrupt_service.record_mcp_remote_task_control(
                answer,
                action="cancel" if cancel_requested else "update",
                input_responses=(
                    {} if cancel_requested else dict(responses or {})
                ),
                now=self._utcnow_naive(),
            )
            await self._record_event(
                self._make_event(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    node_id=interrupt.node_id,
                    event_type="mcp.input_submitted",
                    payload={
                        "interrupt_id": interrupt_id,
                        "action": command.kind,
                    },
                )
            )
            if self.mcp_remote_task_recovery_worker is not None:
                await self.mcp_remote_task_recovery_worker.run_once()
            return {
                "task_id": task.task_id,
                "action": (
                    "mcp_remote_task_cancel_submitted"
                    if cancel_requested
                    else "mcp_remote_task_input_submitted"
                ),
                "interrupt_id": interrupt_id,
            }
        if interrupt.reason_code == "sheet_selection_required":
            self._validate_sheet_selection_answer(interrupt, answer_payload)
        if interrupt.reason_code == "file_selection_ambiguous":
            return await self._answer_file_selection_interrupt(
                task=task,
                interrupt=interrupt,
                answer_payload=answer_payload,
                source_message_id=source_message_id,
                request_metadata=request_metadata,
            )
        if slot_collection_ref_from_required_fields(interrupt.required_fields) is not None:
            return await self._answer_v2_slot_interrupt(
                task=task,
                interrupt=interrupt,
                answer_payload=answer_payload,
                source_message_id=source_message_id,
                request_metadata=request_metadata,
            )
        previous_slot_collection = slot_collection_from_required_fields(interrupt.required_fields)
        if _is_v2_slot_collection_payload(previous_slot_collection):
            collection_id = str(previous_slot_collection.get("collection_id") or "").strip() if previous_slot_collection else ""
            collection = await self.storage.get_slot_collection(collection_id) if collection_id else None
            if collection is None:
                raise UploadValidationError("slot collection state is missing; restart the Skill")
            recovered_interrupt = replace(interrupt, required_fields=slot_collection_required_fields_ref(collection))
            await self.storage.save_interrupt(recovered_interrupt)
            await persist_interrupt_question_message(self.storage, recovered_interrupt)
            return await self._answer_v2_slot_interrupt(
                task=task,
                interrupt=recovered_interrupt,
                answer_payload=answer_payload,
                source_message_id=source_message_id,
                request_metadata=request_metadata,
            )

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
        pending_file_selection_upload_ids = self._normalize_upload_ids(
            sheet_selection_resume_metadata.get("_file_selection_pending_upload_ids") or ()
        )
        pending_file_selection_source_kind = str(
            sheet_selection_resume_metadata.get("_file_selection_pending_source_kind") or ""
        ).strip()
        if pending_file_selection_source_kind not in {"file_selector", "interrupt_answer_upload"}:
            pending_file_selection_source_kind = "interrupt_answer_upload"
        pending_file_selection_answer_id = str(
            sheet_selection_resume_metadata.get("_file_selection_pending_interrupt_answer_id") or ""
        ).strip()
        pending_file_selection_binding_source = "interrupt_answer_upload"
        if pending_file_selection_upload_ids:
            pending_file_selection_binding_source = pending_file_selection_source_kind
        for internal_key in (
            "_file_selection_pending_upload_ids",
            "_file_selection_pending_source_kind",
            "_file_selection_pending_interrupt_answer_id",
        ):
            resume_metadata.pop(internal_key, None)
        upload_ids = pending_file_selection_upload_ids or self._merged_answer_upload_ids(answer_payloads)
        if upload_ids:
            conversation = await self.storage.get_conversation(task.conversation_id)
            if conversation is None:
                raise ValueError(f"Unknown conversation: {task.conversation_id}")
            await self._bind_or_update_resume_input_attachments(
                task=task,
                username=conversation.username,
                upload_ids=upload_ids,
                source_kind=pending_file_selection_binding_source,
                source_message_id=answer.source_message_id,
                interrupt_answer_id=pending_file_selection_answer_id or answer.interrupt_answer_id,
                upload_sheet_selections=resume_metadata.get("upload_sheet_selections"),
            )
        resume_metadata.update(
            await self._conversation_file_context_metadata_for_task(
                task,
                upload_sheet_selections=resume_metadata.get("upload_sheet_selections"),
            )
        )
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
        if (
            interrupted_node is not None
            and interrupted_node.capability_id == "mcp.dispatch"
        ):
            resume_capability_id = "mcp.dispatch"
            resume_metadata["resume_interrupted_node_id"] = interrupted_node.node_id
            server_id = str(interrupt.required_fields.get("server_id") or "").strip()
            if server_id:
                resume_metadata["mcp_dispatch_server_id"] = server_id
            resume_finalizer_node_id = await self._resume_finalizer_node_id(
                task.task_id,
                interrupted_node.node_id,
            )
            if resume_finalizer_node_id:
                resume_metadata["resume_finalizer_node_id"] = resume_finalizer_node_id
        elif interrupted_node is not None and interrupted_node.capability_id.startswith("skill."):
            resume_capability_id = interrupted_node.capability_id
            resume_metadata["resume_interrupted_node_id"] = interrupted_node.node_id
            resume_finalizer_node_id = await self._resume_finalizer_node_id(task.task_id, interrupted_node.node_id)
            if resume_finalizer_node_id:
                resume_metadata["resume_finalizer_node_id"] = resume_finalizer_node_id
        elif interrupt.source_agent.startswith("skill.") and self.capability_registry.get(interrupt.source_agent) is not None:
            resume_capability_id = interrupt.source_agent
        owner_conversation = await self.storage.get_conversation(task.conversation_id)
        if owner_conversation is None:
            raise ValueError(f"Unknown conversation: {task.conversation_id}")
        resume_metadata.update(self._mcp_task_assignment_metadata(task))
        await self._schedule_execution(
            OrchestrationRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=combined_message,
                requested_capability_id=resume_capability_id,
                metadata=resume_metadata,
                available_mcp_servers=await self.available_user_mcp_server_profiles(
                    owner_conversation.username,
                    execution_mode=task.mcp_execution_mode,
                ),
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
        request_metadata: Mapping[str, Any] | None = None,
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
            request_metadata=request_metadata,
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
        request_metadata: Mapping[str, Any] | None = None,
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

        turn_llm_metadata = await self._resume_llm_metadata(task, request_metadata)
        plan = await self._plan_v2_interrupt_open_turn(
            task=task,
            interrupt=interrupt,
            collection=collection,
            raw_answer=raw_answer,
            client_request_id=client_request_id,
            llm_metadata=turn_llm_metadata,
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
                llm_metadata=turn_llm_metadata,
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
                    part=part,
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
                    llm_metadata=turn_llm_metadata,
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
                    llm_metadata=turn_llm_metadata,
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
                        request_metadata=request_metadata,
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
                stream_status=INTERRUPT_VISIBLE_STREAM_STATUS,
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
        llm_metadata: Mapping[str, Any] | None = None,
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
        raw_response = await self._call_skill_input_text_generator(
            prompt,
            metadata=dict(llm_metadata or _INTERRUPT_TURN_LLM_METADATA),
            reasoning_context=self._interrupt_reasoning_context(
                task=task,
                interrupt=interrupt,
                collection=collection,
                stage="interrupt_open_turn",
            ),
        )
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
            part_text = str(raw_part.get("text") or "").strip()
            execution_confirmation = bool(raw_part.get("execution_confirmation"))
            execution_confirmation_confidence = cls._confidence(raw_part.get("execution_confirmation_confidence"))
            if kind == "schema_switch" and cls._schema_switch_text_confirms_execution(part_text):
                execution_confirmation = True
                execution_confirmation_confidence = max(execution_confirmation_confidence, 1.0)
            parts.append(
                InterruptOpenTurnPart(
                    part_id=str(raw_part.get("part_id") or f"part-{idx}").strip() or f"part-{idx}",
                    kind=kind,
                    text=part_text,
                    target_slots=target_slots,
                    target_schema_id=str(raw_part.get("target_schema_id") or "").strip() or None,
                    reuse_decision=reuse_decision,
                    execution_confirmation=execution_confirmation,
                    execution_confirmation_confidence=execution_confirmation_confidence,
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
    def _schema_switch_text_confirms_execution(text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        negative_markers = ("不执行", "先不执行", "暂不执行", "不要执行", "别执行", "不运行", "不要运行", "don't run", "do not run")
        if any(marker in normalized for marker in negative_markers):
            return False
        positive_markers = (
            "立即执行",
            "马上执行",
            "直接执行",
            "并执行",
            "继续执行",
            "确认执行",
            "开始执行",
            "运行",
            "run it",
            "execute",
        )
        return any(marker in normalized for marker in positive_markers)

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
        part: InterruptOpenTurnPart,
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
            planner_part=part,
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
        llm_metadata: Mapping[str, Any] | None = None,
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
                    MAIN_AGENT_SKILL_DOCUMENT_GROUNDING_CONSTRAINT,
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
        raw_response = await self._call_skill_input_text_generator(
            prompt,
            metadata=dict(llm_metadata or _INTERRUPT_TURN_LLM_METADATA),
            reasoning_context=self._interrupt_reasoning_context(
                task=task,
                interrupt=interrupt,
                collection=collection,
                stage="interrupt_skill_question_answer",
            ),
        )
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

    async def _schema_switch_text_extraction(
        self,
        *,
        task: Task,
        collection: SlotCollection,
        schema,
        part: InterruptOpenTurnPart,
        raw_answer: Mapping[str, object],
    ) -> SlotExtractionResult:
        text = part.text or self._v2_answer_text(raw_answer)
        if not text and not self._v2_answer_upload_ids(raw_answer):
            return SlotExtractionResult(resolved={}, diagnostics=())
        artifact_summaries = tuple(await self._task_input_attachment_prompt_summaries(task.task_id))
        return build_backend_slot_extraction(
            collection,
            schema,
            current_user_answer=text,
            current_upload_ids=self._v2_answer_upload_ids(raw_answer),
            artifact_summaries=artifact_summaries,
            planner_target_slots=part.target_slots,
            planner_reason=part.reason,
        )

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
        text_extraction = await self._schema_switch_text_extraction(
            task=task,
            collection=next_collection,
            schema=schema,
            part=part,
            raw_answer=raw_answer,
        )
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
            candidates.update(text_extraction.resolved)
            validating = replace(next_collection, status="validating")
            next_collection, event = apply_extraction_result_to_collection(
                validating,
                schema,
                SlotExtractionResult(resolved=candidates, diagnostics=("schema_switch_reuse", *text_extraction.diagnostics)),
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
        await persist_interrupt_question_message(self.storage, saved_interrupt, created_at=self._utcnow_naive())
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
        saved_interrupt = replace(
            interrupt,
            question=active.last_question or interrupt.question,
            required_fields=slot_collection_required_fields_ref(active),
            status=InterruptStatus.OPEN,
        )
        await self.storage.save_interrupt(saved_interrupt)
        await persist_interrupt_question_message(self.storage, saved_interrupt, created_at=self._utcnow_naive())
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
        request_metadata: Mapping[str, Any] | None = None,
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
        resume_metadata.update(await self._conversation_file_context_metadata_for_task(task))
        resume_metadata.update(await self._resume_llm_metadata(task, request_metadata))
        resume_metadata.update(self._mcp_task_assignment_metadata(task))
        resume_finalizer_node_id = await self._resume_finalizer_node_id(task.task_id, interrupt.node_id)
        if resume_finalizer_node_id:
            resume_metadata["resume_finalizer_node_id"] = resume_finalizer_node_id
        resume_capability_id = task.requested_capability_id
        interrupted_node = await self.storage.get_task_node(interrupt.node_id)
        if interrupted_node is not None and interrupted_node.capability_id.startswith("skill."):
            resume_capability_id = interrupted_node.capability_id
        elif interrupt.source_agent.startswith("skill.") and self.capability_registry.get(interrupt.source_agent) is not None:
            resume_capability_id = interrupt.source_agent
        owner_conversation = await self.storage.get_conversation(task.conversation_id)
        if owner_conversation is None:
            raise ValueError(f"Unknown conversation: {task.conversation_id}")
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
                available_mcp_servers=await self.available_user_mcp_server_profiles(
                    owner_conversation.username,
                    execution_mode=task.mcp_execution_mode,
                ),
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
        raw_response = await self._call_skill_input_text_generator(
            prompt,
            metadata=_INTERRUPT_TURN_LLM_METADATA,
            reasoning_context=self._interrupt_reasoning_context(
                task=task,
                interrupt=interrupt,
                collection=collection,
                stage="interrupt_turn_understanding",
            ),
        )
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
        llm_metadata: Mapping[str, Any] | None = None,
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
        raw_response = await self._call_skill_input_text_generator(
            prompt,
            metadata=dict(llm_metadata or _INTERRUPT_TURN_LLM_METADATA),
            reasoning_context=self._interrupt_reasoning_context(
                task=task,
                interrupt=interrupt,
                collection=collection,
                stage="interrupt_resume_verification",
            ),
        )
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
            stream_status=INTERRUPT_VISIBLE_STREAM_STATUS,
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
        llm_metadata: Mapping[str, Any] | None = None,
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
        raw_response = await self._call_skill_input_text_generator(
            prompt,
            metadata=dict(llm_metadata or _INTERRUPT_TURN_LLM_METADATA),
            reasoning_context=self._interrupt_reasoning_context(
                task=task,
                interrupt=interrupt,
                collection=collection,
                stage="interrupt_clarification_answer",
            ),
        )
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
    def _interrupt_reasoning_context(
        *,
        task: Task,
        stage: str,
        interrupt: Interrupt | None = None,
        collection: SlotCollection | None = None,
    ) -> dict[str, Any]:
        return {
            "event_type": "interrupt.reasoning_delta",
            "task_id": task.task_id,
            "conversation_id": task.conversation_id,
            "node_id": (
                interrupt.node_id
                if interrupt is not None
                else collection.node_id
                if collection is not None
                else None
            ),
            "stage": stage,
            "response_role": "interrupt",
            "interrupt_id": interrupt.interrupt_id if interrupt is not None else None,
            "slot_collection_id": collection.collection_id if collection is not None else None,
            "capability_id": collection.capability_id if collection is not None else None,
            "skill_name": interrupt.source_agent if interrupt is not None else None,
            "reason_code": interrupt.reason_code if interrupt is not None else None,
        }

    @staticmethod
    def _slot_collection_reasoning_context(
        *,
        collection: SlotCollection,
        stage: str,
    ) -> dict[str, Any]:
        return {
            "event_type": "interrupt.reasoning_delta",
            "task_id": collection.task_id,
            "conversation_id": collection.conversation_id,
            "node_id": collection.node_id,
            "stage": stage,
            "response_role": "interrupt",
            "slot_collection_id": collection.collection_id,
            "capability_id": collection.capability_id,
        }

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

    @staticmethod
    def _slot_answer_planner_hint(part: InterruptOpenTurnPart | None) -> dict[str, object]:
        if part is None:
            return {}
        return {
            "source": "interrupt_open_turn_planner",
            "part_id": part.part_id,
            "target_slots": list(part.target_slots),
            "reason": part.reason,
            "confidence": part.confidence,
            "uses_uploads": part.uses_uploads,
        }

    async def _apply_v2_slot_answer(
        self,
        *,
        collection: SlotCollection,
        interrupt: Interrupt,
        planner_part: InterruptOpenTurnPart | None = None,
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
                planner_hint=self._slot_answer_planner_hint(planner_part),
            )
            if history_recall
            else build_normal_extraction_prompt(
                validating,
                current_user_answer=answer_text,
                artifact_summaries=artifact_summaries,
                planner_hint=self._slot_answer_planner_hint(planner_part),
            )
        )
        raw_response = ""
        if self._skill_input_text_generator is not None:
            try:
                llm_metadata = await self._task_accepted_llm_metadata(collection.task_id)
                generated = self._skill_input_text_generator(
                    prompt,
                    metadata=llm_metadata,
                    reasoning_context=self._slot_collection_reasoning_context(
                        collection=validating,
                        stage="slot_extraction",
                    ),
                )
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
            planner_target_slots=planner_part.target_slots if planner_part is not None else (),
            planner_reason=planner_part.reason if planner_part is not None else None,
        )
        extraction = merge_slot_extraction_results(extraction, backend_extraction, collection=validating)
        if not extraction.resolved:
            extraction = self._fallback_v2_slot_extraction(
                validating,
                schema,
                raw_answer=raw_answer,
                planner_part=planner_part,
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
        attachments = await self.storage.list_task_input_attachments_for_task(task_id)
        attachment_by_upload_id = {
            str(attachment.source_upload_id): attachment
            for attachment in attachments
            if attachment.source_upload_id
        }
        task = await self.storage.get_task(task_id)
        conversation = None
        if task is not None:
            conversation = await self.storage.get_conversation(task.conversation_id)
            if conversation is not None:
                upload_context = await self.resolve_conversation_uploads_for_message(
                    task.conversation_id,
                    conversation.username,
                )
                for artifact in upload_context.get("uploaded_artifacts", []):
                    if not isinstance(artifact, Mapping):
                        continue
                    upload_id = str(artifact.get("upload_id") or "").strip()
                    preview = artifact.get("preview") if isinstance(artifact.get("preview"), Mapping) else {}
                    attachment = attachment_by_upload_id.get(upload_id)
                    summaries.append(
                        {
                            "upload_id": upload_id,
                            "filename": str(artifact.get("filename") or ""),
                            "content_type": str(artifact.get("content_type") or ""),
                            "file_type": str(artifact.get("file_type") or ""),
                            "size_bytes": artifact.get("size_bytes"),
                            "sha256": str(artifact.get("sha256") or ""),
                            "selected_sheet": artifact.get("selected_sheet"),
                            "columns": list(preview.get("columns") or ()) if isinstance(preview.get("columns"), list | tuple) else None,
                            "row_count": preview.get("row_count"),
                            "column_count": preview.get("column_count"),
                            "source_kind": attachment.source_kind if attachment is not None else "conversation_file",
                            "source_message_id": attachment.source_message_id if attachment is not None else None,
                            "interrupt_answer_id": attachment.interrupt_answer_id if attachment is not None else None,
                            "created_at": (
                                attachment.created_at.isoformat()
                                if attachment is not None and attachment.created_at is not None
                                else artifact.get("created_at")
                            ),
                        }
                    )
                if summaries:
                    return summaries
        for attachment in attachments:
            if not await self._task_input_attachment_upload_is_active(attachment, task=task, conversation=conversation):
                continue
            prompt_artifact = attachment.prompt_artifact if isinstance(attachment.prompt_artifact, Mapping) else {}
            if self._artifact_item_marked_deleted(prompt_artifact):
                continue
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
        planner_part: InterruptOpenTurnPart | None = None,
    ) -> dict[str, dict[str, object]]:
        extraction = build_backend_slot_extraction(
            collection,
            schema,
            current_user_answer=self._v2_answer_text(raw_answer),
            current_upload_ids=self._v2_answer_upload_ids(raw_answer),
            planner_target_slots=planner_part.target_slots if planner_part is not None else (),
            planner_reason=planner_part.reason if planner_part is not None else None,
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
        planner_part: InterruptOpenTurnPart | None = None,
    ) -> SlotExtractionResult:
        candidates = {
            field: SlotExtractionCandidate(
                field=field,
                raw_value=dict(candidate["raw_value"]) if isinstance(candidate.get("raw_value"), Mapping) else candidate.get("raw_value"),
                value=dict(candidate["value"]) if isinstance(candidate.get("value"), Mapping) else candidate.get("value"),
                source=str(candidate.get("source") or "current_answer"),
            )
            for field, candidate in self._fallback_v2_slot_candidates(
                collection,
                schema,
                raw_answer=raw_answer,
                planner_part=planner_part,
            ).items()
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
            resource = await self.storage.get_conversation_file_resource(task.conversation_id, username, upload_id)
            if resource is None:
                existing_resource = await self.storage.get_conversation_file_resource_by_id(upload_id)
                if existing_resource is not None:
                    raise PermissionError(f"Upload does not belong to conversation: {upload_id}")
                raise UploadValidationError(f"Unknown or expired upload_id: {upload_id}")
            if resource.status == "deleted":
                raise UploadValidationError(f"Unknown or expired upload_id: {upload_id}")
            selected_sheet = sheet_selections.get(upload_id)
            if selected_sheet:
                resource = await self._apply_conversation_file_sheet_selection(resource, selected_sheet)
            record = self._upload_record_from_resource(
                resource,
                content_bytes=self.conversation_file_store.read_bytes(resource.storage_key),
                selected_sheet=selected_sheet,
            )
            attachment = self._attachment_from_upload_record(
                task=task,
                record=record,
                resource=resource,
                source_kind=source_kind,
                source_message_id=source_message_id,
                interrupt_answer_id=interrupt_answer_id,
                selected_sheet=selected_sheet,
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
                resource = await self.storage.get_conversation_file_resource(task.conversation_id, username, upload_id)
                if resource is None or resource.status == "deleted":
                    missing_upload_ids.append(upload_id)
                    continue
                if selected_sheet:
                    await self._apply_conversation_file_sheet_selection(resource, selected_sheet)
                    await self._update_task_input_attachment_sheet_selection(
                        existing_attachment,
                        selected_sheet=selected_sheet,
                        interrupt_answer_id=interrupt_answer_id,
                    )
                continue
            resource = await self.storage.get_conversation_file_resource(task.conversation_id, username, upload_id)
            if resource is None:
                existing_resource = await self.storage.get_conversation_file_resource_by_id(upload_id)
                if existing_resource is not None:
                    raise PermissionError(f"Upload does not belong to conversation: {upload_id}")
                missing_upload_ids.append(upload_id)
                continue
            if resource.status == "deleted":
                missing_upload_ids.append(upload_id)
                continue
            if selected_sheet:
                resource = await self._apply_conversation_file_sheet_selection(resource, selected_sheet)
            record = self._upload_record_from_resource(
                resource,
                content_bytes=self.conversation_file_store.read_bytes(resource.storage_key),
                selected_sheet=selected_sheet,
            )
            if record.requires_sheet_selection and not selected_sheet:
                raise UploadValidationError("Spreadsheet sheet selection is required before resuming the task")
            attachment = self._attachment_from_upload_record(
                task=task,
                record=record,
                resource=resource,
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
        resource: ConversationFileResource | None = None,
        source_kind: str,
        source_message_id: str | None,
        interrupt_answer_id: str | None,
        selected_sheet: str | None,
    ) -> TaskInputAttachment:
        now = self._utcnow_naive()
        skill_artifact = record.to_skill_artifact(selected_sheet=selected_sheet)
        if resource is not None:
            skill_artifact["storage_key"] = resource.storage_key
            skill_artifact["conversation_id"] = resource.conversation_id
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
            source_payload=self._source_payload_from_upload_record(record, resource=resource),
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
        storage_key = source_payload.get("storage_key")
        if isinstance(storage_key, str) and storage_key:
            content = self.conversation_file_store.read_bytes(storage_key)
        else:
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
        task = await self.storage.get_task(task_id)
        conversation = await self.storage.get_conversation(task.conversation_id) if task is not None else None
        inactive_upload_ids: set[str] = set()
        active_attachments: list[TaskInputAttachment] = []
        if task is None or conversation is None:
            inactive_upload_ids.update(
                str(attachment.source_upload_id or "").strip()
                for attachment in attachments
                if str(attachment.source_upload_id or "").strip()
            )
        else:
            for attachment in attachments:
                if await self._task_input_attachment_upload_is_active(
                    attachment,
                    task=task,
                    conversation=conversation,
                ):
                    active_attachments.append(attachment)
                    continue
                upload_id = str(attachment.source_upload_id or "").strip()
                if upload_id:
                    inactive_upload_ids.add(upload_id)
        if inactive_upload_ids:
            missing = ", ".join(sorted(inactive_upload_ids))
            raise UploadValidationError(
                f"Task-bound uploads are no longer available for execution: {missing}. "
                "Please re-upload the file or select another active file."
            )
        return {
            "uploaded_artifacts": [
                dict(attachment.prompt_artifact)
                for attachment in active_attachments
                if isinstance(attachment.prompt_artifact, Mapping) and attachment.prompt_artifact
                and not self._artifact_item_marked_deleted(attachment.prompt_artifact)
            ],
            "skill_artifacts": [
                dict(attachment.skill_artifact)
                for attachment in active_attachments
                if isinstance(attachment.skill_artifact, Mapping) and attachment.skill_artifact
                and not self._artifact_item_marked_deleted(attachment.skill_artifact)
            ],
        }

    async def _task_input_attachment_upload_is_active(
        self,
        attachment: TaskInputAttachment,
        *,
        task: Task | None,
        conversation: Conversation | None,
    ) -> bool:
        upload_id = str(attachment.source_upload_id or "").strip()
        if not upload_id:
            return True
        if task is None or conversation is None:
            return False
        resource = await self.storage.get_conversation_file_resource(task.conversation_id, conversation.username, upload_id)
        return resource is not None and resource.status != "deleted"

    @staticmethod
    def _task_input_attachment_id(task_id: str, upload_id: str) -> str:
        return f"{task_id}:input:{upload_id}"

    @staticmethod
    def _artifact_item_marked_deleted(item: Mapping[str, Any]) -> bool:
        status = str(item.get("file_status") or item.get("status") or "").strip().lower()
        return status == "deleted"

    @staticmethod
    def _prompt_artifact_from_skill_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
        prompt_artifact = dict(artifact)
        for raw_key in (
            "content",
            "content_base64",
            "encoding",
            "storage_key",
            "mount_path",
            "resource_manifest_path",
            "conversation_index_path",
            "input_dir",
        ):
            prompt_artifact.pop(raw_key, None)
        return prompt_artifact

    def _initial_file_description(self, record: UploadedFileRecord) -> tuple[str, str | None, str | None]:
        if record.file_type == "image":
            return "not_required", None, None
        summary = self._build_file_description_summary(record)
        description_ref = self.conversation_file_store.write_description(
            conversation_id=record.conversation_id,
            upload_id=record.upload_id,
            description={
                "version": 1,
                "upload_id": record.upload_id,
                "filename": record.filename,
                "file_type": record.file_type,
                "status": "ready",
                "summary": summary,
                "preview": dict(record.preview),
            },
        )
        return "ready", summary, description_ref

    @staticmethod
    def _build_file_description_summary(record: UploadedFileRecord) -> str:
        preview = dict(record.preview or {})
        if record.file_type in {"csv", "json", "spreadsheet"}:
            columns = preview.get("columns") or preview.get("original_columns") or []
            column_text = ", ".join(str(column) for column in columns[:20]) if isinstance(columns, list) else ""
            row_count = preview.get("row_count")
            if record.file_type == "spreadsheet" and preview.get("excel_sheets"):
                sheets = [
                    str(item.get("sheet_name"))
                    for item in preview.get("excel_sheets", [])
                    if isinstance(item, Mapping) and item.get("sheet_name")
                ]
                return f"这是一份电子表格文件，包含 sheets: {', '.join(sheets[:20]) or '未知'}。" + (f" 当前预览列包括: {column_text}。" if column_text else "")
            return f"这是一份结构化数据文件，行数约为 {row_count if row_count is not None else '未知'}，列包括: {column_text or '未知'}。"
        if record.file_type == "text":
            sample = (record.content_text or "").strip().replace("\n", " ")[:500]
            return f"这是一份文本文件，包含 {preview.get('line_count', '未知')} 行、{preview.get('char_count', '未知')} 个字符。开头内容摘要: {sample}"
        if record.file_type == "pdf":
            return "这是一份 PDF 文件；当前阶段记录基础文件元数据，后续可通过受控文本抽取或 OCR adapter 生成更详细结构描述。"
        if record.file_type == "vcf":
            return "这是一份 VCF/VCF.GZ 变异数据文件；脚本可读取原始文件进行样本、header 和 variant 解析。"
        return "这是一份可供 Skill 脚本处理的上传文件。"

    async def _rewrite_conversation_file_index(self, conversation_id: str, username: str) -> None:
        resources = await self.storage.list_conversation_file_resources(conversation_id, username, include_deleted=True)
        self._conversation_file_index_writer.write_index(conversation_id=conversation_id, resources=resources)

    async def _delete_request_upload_artifacts(self, *, conversation_id: str, username: str, upload_id: str) -> None:
        self.upload_store.delete(upload_id=upload_id, username=username, conversation_id=conversation_id)
        try:
            self.conversation_file_store.delete_resource_dir(conversation_id=conversation_id, upload_id=upload_id)
        except Exception as exc:
            if self._audit_sink is not None:
                await self._audit_sink.record(
                    "conversation_file.request_upload_cleanup_failed",
                    {"upload_id": upload_id, "error_type": exc.__class__.__name__},
                    conversation_id=conversation_id,
                )

    async def _compensate_failed_upload_after_index_error(
        self,
        *,
        conversation_id: str,
        username: str,
        upload_id: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        try:
            await self.storage.compensate_failed_conversation_file_upload(
                conversation_id,
                username,
                upload_id,
                reason_code=reason_code,
                now=now,
            )
        except Exception as exc:
            marker_error_type: str | None = None
            try:
                await self.storage.record_conversation_file_index_repair_required(
                    conversation_id,
                    reason_code="index_write_failed_compensation_failed",
                    affected_upload_ids=(upload_id,),
                    now=now,
                )
            except Exception as marker_exc:
                marker_error_type = marker_exc.__class__.__name__
            if self._audit_sink is not None:
                payload = {
                    "upload_id": upload_id,
                    "error_type": exc.__class__.__name__,
                    "reason_code": reason_code,
                    "repair_marker_recorded": marker_error_type is None,
                }
                if marker_error_type is not None:
                    payload["repair_marker_error_type"] = marker_error_type
                await self._audit_sink.record(
                    "conversation_file.upload_compensation_failed",
                    payload,
                    conversation_id=conversation_id,
                )
            raise

    async def _rewrite_conversation_file_index_with_repair(
        self,
        conversation_id: str,
        username: str,
        *,
        affected_upload_ids: Iterable[str] = (),
        reason_code: str,
    ) -> bool:
        try:
            await self._rewrite_conversation_file_index(conversation_id, username)
        except Exception:
            try:
                await self._rewrite_conversation_file_index(conversation_id, username)
            except Exception as retry_exc:
                marker = await self.storage.record_conversation_file_index_repair_required(
                    conversation_id,
                    reason_code=reason_code,
                    affected_upload_ids=affected_upload_ids,
                    now=self._utcnow_naive(),
                )
                if self._audit_sink is not None:
                    await self._audit_sink.record(
                        CONVERSATION_FILE_INDEX_REPAIR_REQUIRED_EVENT,
                        {
                            "repair_kind": marker.repair_kind,
                            "status": marker.status,
                            "reason_code": marker.reason_code,
                            "affected_upload_ids": list(marker.affected_upload_ids),
                            "attempt_count": marker.attempt_count,
                            "error_type": retry_exc.__class__.__name__,
                        },
                        conversation_id=conversation_id,
                    )
                return False
        await self._resolve_conversation_file_index_repair_marker_if_present(conversation_id)
        return True

    async def _resolve_conversation_file_index_repair_marker_if_present(self, conversation_id: str) -> None:
        marker = await self.storage.get_conversation_file_index_repair_marker(conversation_id)
        if marker is None or marker.status == "resolved":
            return
        resolved = await self.storage.mark_conversation_file_index_repair_resolved(
            conversation_id,
            now=self._utcnow_naive(),
        )
        if resolved is not None and self._audit_sink is not None:
            await self._audit_sink.record(
                CONVERSATION_FILE_INDEX_REPAIR_RESOLVED_EVENT,
                {
                    "repair_kind": resolved.repair_kind,
                    "status": resolved.status,
                    "affected_upload_ids": list(resolved.affected_upload_ids),
                    "attempt_count": resolved.attempt_count,
                },
                conversation_id=conversation_id,
            )

    async def _repair_conversation_file_index_if_due(self, conversation_id: str, username: str) -> bool:
        marker = await self.storage.get_conversation_file_index_repair_marker(conversation_id)
        if marker is None or marker.status not in {"pending", "failed"}:
            return True
        now = self._utcnow_naive()
        if marker.next_retry_at is not None and marker.next_retry_at > now:
            return False
        await self.storage.mark_conversation_file_index_repairing(conversation_id, now=now)
        try:
            await self._rewrite_conversation_file_index(conversation_id, username)
        except Exception as exc:
            failed = await self.storage.mark_conversation_file_index_repair_failed(
                conversation_id,
                reason_code="index_repair_failed",
                now=self._utcnow_naive(),
            )
            if failed is not None and self._audit_sink is not None:
                await self._audit_sink.record(
                    CONVERSATION_FILE_INDEX_REPAIR_FAILED_EVENT,
                    {
                        "repair_kind": failed.repair_kind,
                        "status": failed.status,
                        "reason_code": failed.reason_code,
                        "affected_upload_ids": list(failed.affected_upload_ids),
                        "attempt_count": failed.attempt_count,
                        "error_type": exc.__class__.__name__,
                    },
                    conversation_id=conversation_id,
                )
            return False
        await self._resolve_conversation_file_index_repair_marker_if_present(conversation_id)
        return True

    async def repair_due_conversation_file_indexes(self, *, limit: int | None = None) -> int:
        repaired = 0
        markers = await self.storage.list_due_conversation_file_index_repairs(now=self._utcnow_naive(), limit=limit)
        for marker in markers:
            conversation = await self.storage.get_conversation(marker.conversation_id)
            if conversation is None:
                await self.storage.mark_conversation_file_index_repair_failed(
                    marker.conversation_id,
                    reason_code="conversation_missing",
                    now=self._utcnow_naive(),
                    retryable=False,
                )
                continue
            if await self._repair_conversation_file_index_if_due(marker.conversation_id, conversation.username):
                repaired += 1
        return repaired

    async def refresh_file_upload_history_message(
        self,
        resource: ConversationFileResource,
        *,
        now: datetime | None = None,
    ) -> Message:
        return await self.storage.upsert_file_upload_message(
            build_file_upload_message_projection(resource),
            now=now or self._utcnow_naive(),
        )

    async def _apply_conversation_file_sheet_selection(
        self,
        resource: ConversationFileResource,
        selected_sheet: str,
    ) -> ConversationFileResource:
        sheet_name = str(selected_sheet or "").strip()
        if not sheet_name:
            return resource
        if resource.file_type != "spreadsheet":
            raise UploadValidationError(f"Sheet selection is only supported for spreadsheet uploads: {resource.file_id}")
        if resource.selected_sheet == sheet_name and resource.requires_sheet_selection is False:
            return resource
        normalized = normalize_selected_spreadsheet_sheet(
            filename=resource.original_filename,
            content_type=resource.content_type,
            content=self.conversation_file_store.read_bytes(resource.storage_key),
            selected_sheet=sheet_name,
        )
        updated = replace(
            resource,
            preview=dict(normalized.preview),
            normalized_filename=normalized.normalized_filename or resource.normalized_filename,
            normalized_content_type=normalized.normalized_content_type or resource.normalized_content_type,
            requires_sheet_selection=False,
            selected_sheet=normalized.selected_sheet or sheet_name,
            updated_at=self._utcnow_naive(),
        )
        saved = await self.storage.save_conversation_file_resource(updated)
        await self.refresh_file_upload_history_message(saved)
        await self._rewrite_conversation_file_index_with_repair(
            saved.conversation_id,
            saved.username,
            affected_upload_ids=(saved.file_id,),
            reason_code="sheet_selection_index_write_failed",
        )
        return saved

    def _upload_record_from_resource(
        self,
        resource: ConversationFileResource,
        *,
        content_bytes: bytes,
        selected_sheet: str | None = None,
    ) -> UploadedFileRecord:
        content_text: str | None = None
        normalized_content_type = resource.normalized_content_type
        normalized_filename = resource.normalized_filename
        requires_sheet_selection = resource.requires_sheet_selection
        selected_sheet_text = selected_sheet or resource.selected_sheet
        preview = dict(resource.preview)
        if content_bytes:
            if resource.file_type in {"json", "csv", "spreadsheet"}:
                normalized = normalize_table_upload(
                    filename=resource.original_filename,
                    content_type=resource.content_type,
                    content=content_bytes,
                    selected_sheet=selected_sheet_text,
                )
                content_text = normalized.normalized_content_text
                normalized_content_type = normalized.normalized_content_type
                normalized_filename = normalized.normalized_filename
                requires_sheet_selection = normalized.requires_sheet_selection
                selected_sheet_text = normalized.selected_sheet or selected_sheet_text
                preview = dict(normalized.preview)
            elif resource.file_type == "text":
                decoded_text, _source_encoding = _decode_plain_text_upload(content_bytes)
                content_text = decoded_text
                normalized_content_type = "text/plain"
                normalized_filename = resource.original_filename
        created_at = resource.created_at or self._utcnow_naive()
        return UploadedFileRecord(
            upload_id=resource.file_id,
            username=resource.username,
            conversation_id=resource.conversation_id,
            filename=resource.original_filename,
            content_type=resource.content_type,
            file_type=resource.file_type,
            size_bytes=resource.size_bytes,
            sha256=resource.sha256,
            content_bytes=content_bytes,
            content_text=content_text,
            preview=preview,
            created_at=created_at,
            expires_at=created_at + timedelta(days=3650),
            normalized_content_type=normalized_content_type,
            normalized_filename=normalized_filename,
            requires_sheet_selection=requires_sheet_selection,
            selected_sheet=selected_sheet_text,
            status=resource.status,
            description_status=resource.description_status,
        )

    @staticmethod
    def _source_payload_from_upload_record(
        record: UploadedFileRecord,
        *,
        resource: ConversationFileResource | None = None,
    ) -> dict[str, Any]:
        payload = {
            "encoding": "base64",
            "content_base64": base64.b64encode(record.content_bytes).decode("ascii"),
            "filename": record.filename,
            "content_type": record.content_type,
            "file_type": record.file_type,
            "normalized_filename": record.normalized_filename,
            "normalized_content_type": record.normalized_content_type,
        }
        if resource is not None:
            payload.update(
                {
                    "conversation_file_id": resource.file_id,
                    "storage_key": resource.storage_key,
                    "description_status": resource.description_status,
                }
            )
        return payload

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


    async def _run_mcp_continuation_commands_forever(self) -> None:
        while True:
            try:
                await self._run_mcp_continuation_commands_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.25)
                continue
            await asyncio.sleep(0.1)

    async def _run_mcp_continuation_commands_once(self) -> int:
        now = self._utcnow_naive()
        for abandoned in await self.storage.abandon_expired_mcp_remote_task_continuations(
            now=now, limit=100
        ):
            await self._converge_abandoned_mcp_continuation(abandoned, now=now)
            completed = await self.storage.complete_abandoned_mcp_remote_task_continuation(
                abandoned.outbox_id,
                expected_revision=abandoned.continuation_revision,
                completed_at=now,
            )
            if completed is None:
                raise RuntimeError("mcp_continuation_abandonment_completion_lost")
        claim_token = uuid4().hex
        commands = await self.storage.claim_mcp_remote_task_continuations(
            claim_owner=self._mcp_continuation_consumer_id,
            claim_token=claim_token,
            now=now,
            lease_expires_at=now + timedelta(seconds=30),
            limit=100,
        )
        for command in commands:
            await self._consume_mcp_continuation_command(command, now=now)
        return len(commands)

    async def _consume_mcp_continuation_command(self, command, *, now: datetime) -> None:
        running = await self.storage.begin_mcp_remote_task_continuation(
            command.outbox_id,
            claim_owner=str(command.continuation_claim_owner or ""),
            claim_token=str(command.continuation_claim_token or ""),
            expected_revision=command.continuation_revision,
            started_at=now,
        )
        if running is None:
            return
        if str(running.payload.get("call_status") or "") != "completed":
            await _mark_remote_continuation_dispatched(
                self.storage, running, self._utcnow_naive()
            )
            return
        task = await self.storage.get_task(running.task_id)
        if task is None or task.status != TaskStatus.RUNNING:
            await _mark_remote_continuation_dispatched(
                self.storage, running, self._utcnow_naive()
            )
            return
        conversation = await self.storage.get_conversation(task.conversation_id)
        root_message = await self.storage.get_message(task.root_message_id)
        if conversation is None:
            raise RuntimeError("mcp_continuation_conversation_missing")
        raw_plan = running.payload.get("continuation_plan")
        if not isinstance(raw_plan, Mapping):
            raise RuntimeError("mcp_continuation_plan_missing")
        persisted_plan = _deserialize_mcp_continuation_plan(raw_plan)
        if persisted_plan.task_id != task.task_id:
            raise RuntimeError("mcp_continuation_plan_task_mismatch")
        result_ref = str(running.payload.get("result_ref") or "").strip()
        if not result_ref or self.user_mcp_result_store is None:
            raise RuntimeError("mcp_continuation_result_missing")
        result_descriptor = self.user_mcp_result_store.resolve_ref(result_ref)
        result_bytes = b"".join(
            [
                chunk
                async for chunk in self.user_mcp_result_store.iter_bytes(
                    result_descriptor
                )
            ]
        )
        continuation_result = json.loads(result_bytes)
        if not isinstance(continuation_result, dict):
            raise RuntimeError("mcp_continuation_result_invalid")
        owned_node_ids = tuple(
            sorted(
                node.node_id
                for node in await self.storage.list_task_nodes_for_task(task.task_id)
                if node.node_id != running.node_id
                and node.status
                not in {
                    NodeStatus.COMPLETED,
                    NodeStatus.FAILED,
                    NodeStatus.CANCELLED,
                    NodeStatus.BLOCKED_BY_CANCELLATION,
                    NodeStatus.ORPHANED,
                }
            )
        )
        scoped = await self.storage.renew_mcp_remote_task_continuation(
            running.outbox_id,
            claim_owner=str(running.continuation_claim_owner or ""),
            claim_token=str(running.continuation_claim_token or ""),
            expected_revision=running.continuation_revision,
            lease_expires_at=self._utcnow_naive() + timedelta(seconds=30),
            node_ids=owned_node_ids,
            updated_at=self._utcnow_naive(),
        )
        if scoped is None:
            raise RuntimeError("mcp_continuation_scope_claim_lost")
        running = scoped
        handle = await self._schedule_execution(
            OrchestrationRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=(
                    root_message.content if root_message is not None else task.summary or ""
                ),
                requested_capability_id=task.requested_capability_id,
                metadata={
                    **self._mcp_task_assignment_metadata(task),
                    "mcp_remote_task_continuation_id": running.outbox_id,
                    "mcp_remote_task_continuation_claim_token": (
                        running.continuation_claim_token
                    ),
                    "mcp_remote_task_source_node_id": running.node_id,
                    "mcp_remote_task_result_ref": result_ref,
                    "mcp_remote_task_result": continuation_result,
                    "mcp_remote_task_continuation_plan": dict(raw_plan),
                },
                available_mcp_servers=await self.available_user_mcp_server_profiles(
                    conversation.username,
                    execution_mode=task.mcp_execution_mode,
                ),
            )
        )
        while not handle.done():
            try:
                await asyncio.wait_for(asyncio.shield(handle), timeout=10)
            except asyncio.TimeoutError:
                renewed_at = self._utcnow_naive()
                renewed = await self.storage.renew_mcp_remote_task_continuation(
                    running.outbox_id,
                    claim_owner=str(running.continuation_claim_owner or ""),
                    claim_token=str(running.continuation_claim_token or ""),
                    expected_revision=running.continuation_revision,
                    lease_expires_at=renewed_at + timedelta(seconds=30),
                    updated_at=renewed_at,
                )
                if renewed is None:
                    raise RuntimeError("mcp_continuation_lease_lost")
                running = renewed
        await asyncio.shield(handle)
        await _mark_remote_continuation_dispatched(
            self.storage, running, self._utcnow_naive()
        )

    async def _converge_abandoned_mcp_continuation(self, command, *, now: datetime) -> None:
        owned_node_ids = set(command.continuation_node_ids)
        for node in await self.storage.list_task_nodes_for_task(command.task_id):
            if node.node_id in owned_node_ids and node.status == NodeStatus.RUNNING:
                saved_node = await self.storage.compare_and_set_task_node(
                    replace(node, status=NodeStatus.FAILED, finished_at=node.finished_at or now),
                    expected_from_status=NodeStatus.RUNNING,
                )
                if saved_node is None:
                    current_node = await self.storage.get_task_node(node.node_id)
                    if current_node is None or current_node.status != NodeStatus.FAILED:
                        raise RuntimeError(
                            "mcp_continuation_abandonment_node_convergence_lost"
                        )
        task = await self.storage.get_task(command.task_id)
        if task is not None and task.status == TaskStatus.RUNNING:
            saved_task = await self.storage.compare_and_set_task(
                replace(task, status=TaskStatus.FAILED, updated_at=now),
                expected_from_status=TaskStatus.RUNNING,
            )
            if saved_task is None:
                current_task = await self.storage.get_task(command.task_id)
                if current_task is None or current_task.status != TaskStatus.FAILED:
                    raise RuntimeError(
                        "mcp_continuation_abandonment_task_convergence_lost"
                    )

    async def start(self) -> None:
        await self._admit_mcp_rollout_instance()
        if self.user_mcp_audit_service is not None:
            self._mcp_audit_retention_task = asyncio.create_task(
                self.user_mcp_audit_service.run_retention_forever(),
                name="mcp-audit-retention",
            )
        if self.user_mcp_presence_service is not None and self.auth_invalidation_bus is not None:
            self._mcp_auth_invalidation_queue = self.auth_invalidation_bus.subscribe()
            self._mcp_auth_invalidation_task = asyncio.create_task(
                self._run_mcp_auth_invalidation_listener(),
                name="mcp-auth-invalidation-listener",
            )
        if self.mcp_credential_cipher is not None:
            await self.mcp_credential_cipher.create_or_verify_sentinel(self.storage)
        if self.mcp_remote_task_recovery_worker is not None:
            await self._recover_user_mcp_calls()
            await self.mcp_remote_task_recovery_worker.start()
            self._mcp_continuation_consumer_task = asyncio.create_task(
                self._run_mcp_continuation_commands_forever(),
                name="mcp-continuation-consumer",
            )
        if self.user_mcp_health_runner is not None:
            await self.user_mcp_health_runner.start()
        if self.user_mcp_gateway is not None:
            await self.storage.expire_user_mcp_scope_leases(now=self._utcnow_naive())
        if self.user_mcp_result_janitor is not None and self.user_mcp_result_store is not None:
            await self.user_mcp_result_janitor.cleanup_orphans(
                active_task_keys=self.user_mcp_result_store.active_task_keys()
            )
        if self.user_mcp_config_service is not None:
            await self.user_mcp_config_service.start()
        if self.postgres_mcp_invalidation_bus is not None:
            try:
                await self.postgres_mcp_invalidation_bus.start()
            except Exception:
                pass
        if self.postgres_auth_invalidation_bus is not None:
            await self.postgres_auth_invalidation_bus.start()
        await self.recover_deleting_conversations()
        if (
            self._mcp_rollout_metric_recorder is not None
            and self._mcp_rollout_zero_series_task is None
        ):
            self._mcp_rollout_zero_series_task = asyncio.create_task(
                self._mcp_rollout_metric_recorder.run_continuous_zero_series(),
                name="mcp-rollout-zero-series",
            )

    async def _admit_mcp_rollout_instance(self) -> None:
        admission = self._mcp_rollout_instance_admission
        if admission is None:
            return
        now = self._utcnow_naive()
        if self._mcp_rollout_instance_lease_created_at is None:
            self._mcp_rollout_instance_lease_created_at = now
        await self._save_mcp_rollout_instance_lease(now=now)
        self._mcp_rollout_instance_admission_error = None
        if (
            self._mcp_rollout_instance_lease_task is None
            or self._mcp_rollout_instance_lease_task.done()
        ):
            self._mcp_rollout_instance_lease_task = asyncio.create_task(
                self._run_mcp_rollout_instance_lease_renewal(),
                name="mcp-rollout-instance-lease-renewal",
            )

    async def _save_mcp_rollout_instance_lease(self, *, now: datetime) -> None:
        admission = self._mcp_rollout_instance_admission
        created_at = self._mcp_rollout_instance_lease_created_at
        if admission is None or created_at is None:
            raise RuntimeError("mcp_rollout_instance_admission_not_initialized")
        lease_expires_at = now + timedelta(
            seconds=self._mcp_rollout_lease_duration_seconds
        )
        await self.storage.save_mcp_rollout_instance_config_lease(
            MCPRolloutInstanceConfigLease(
                instance_config_id=(
                    f"mcp-rollout-instance:{admission.environment_id}:"
                    f"{admission.deployment_id}:{admission.instance_id}"
                ),
                environment_id=admission.environment_id,
                deployment_id=admission.deployment_id,
                instance_id=admission.instance_id,
                stage=admission.stage,
                config_fingerprint=self.mcp_rollout_config.fingerprint,
                activation_id=admission.activation_id,
                lease_expires_at=lease_expires_at,
                created_at=created_at,
                updated_at=now,
            )
        )
        self._mcp_rollout_instance_lease_valid_until = lease_expires_at

    async def _run_mcp_rollout_instance_lease_renewal(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._mcp_rollout_lease_renew_interval_seconds)
                await self._save_mcp_rollout_instance_lease(now=self._utcnow_naive())
        except asyncio.CancelledError:
            raise
        except Exception:
            self._mcp_rollout_instance_admission_error = "lease_renewal_failed"
            self._record_mcp_rollout_instance_admission_event(
                status="lost",
                reason_code="lease_renewal_failed",
            )

    def _ensure_mcp_rollout_instance_admitted(self) -> None:
        if self._mcp_rollout_instance_admission is None:
            return
        now = self._utcnow_naive()
        valid_until = self._mcp_rollout_instance_lease_valid_until
        reason_code = self._mcp_rollout_instance_admission_error
        if reason_code is None and (valid_until is None or now >= valid_until):
            reason_code = "lease_expired"
            self._mcp_rollout_instance_admission_error = reason_code
        if reason_code is None:
            return
        self._record_mcp_rollout_instance_admission_event(
            status="rejected",
            reason_code=reason_code,
        )
        raise RuntimeError("mcp_rollout_instance_admission_lost")

    def _record_mcp_rollout_instance_admission_event(
        self,
        *,
        status: str,
        reason_code: str,
    ) -> None:
        admission = self._mcp_rollout_instance_admission
        if admission is None or self._audit_sink is None:
            return
        try:
            self._audit_sink.record_sync(
                "mcp.rollout.instance_admission_lost",
                {
                    "status": status,
                    "reason_code": reason_code,
                    "stage": admission.stage,
                },
            )
        except Exception:
            pass

    async def _stop_mcp_rollout_instance_lease_renewal(self) -> None:
        task = self._mcp_rollout_instance_lease_task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._mcp_rollout_instance_lease_task = None

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

    async def _recover_user_mcp_calls(self) -> None:
        await self.storage.reconcile_unpublished_mcp_remote_task_bindings(
            now=self._utcnow_naive(),
            limit=1000,
        )
        while True:
            converged = await self.storage.converge_dispatched_mcp_calls_to_unknown(
                now=self._utcnow_naive(),
                limit=1000,
            )
            for call in converged:
                task = await self.storage.get_task(call.task_id)
                if task is None:
                    continue
                try:
                    await self._record_recovered_unknown_metrics(call, task)
                except Exception:
                    await self._record_mcp_metric_gap_best_effort(
                        task=task,
                        call=call,
                        metric_family="ordinary_restart_unknown",
                    )
                await self._record_event(
                    self._make_event(
                        task_id=call.task_id,
                        conversation_id=task.conversation_id,
                        node_id=call.node_id,
                        event_type="mcp.execution_status_unknown",
                        payload={
                            "safe_call_ref": call.call_ref,
                            "status": "unknown",
                            "error_code": "execution_status_unknown",
                        },
                    )
                )
            if len(converged) < 1000:
                return

    async def _record_recovered_unknown_metrics(self, call, task) -> None:
        recorder = self._mcp_rollout_metric_recorder
        if recorder is None:
            return
        if (
            task.mcp_execution_mode != MCPExecutionPath.USER_SCOPED.value
            or task.mcp_rollout_mode != MCPRoutingMode.ENFORCE.value
            or task.mcp_rollout_config_version
            != self.mcp_rollout_config.config_version
        ):
            raise RuntimeError("mcp_unknown_metric_assignment_mismatch")
        server = await self.storage.get_user_mcp_server(
            call.owner_user_id, call.server_id
        )
        if (
            server is None
            or call.server_security_version != server.security_version
            or call.protocol_version is None
        ):
            raise RuntimeError("mcp_unknown_metric_security_version_mismatch")
        transport = {
            UserMCPTransport.STREAMABLE_HTTP: MCPMetricTransport.STREAMABLE_HTTP,
            UserMCPTransport.LEGACY_HTTP_SSE: MCPMetricTransport.LEGACY_HTTP_SSE,
        }[server.transport]
        protocol_version = MCPMetricProtocolVersion(call.protocol_version)
        adapter = (
            MCPMetricAdapter.PYTHON_2026
            if protocol_version is MCPMetricProtocolVersion.V2026_07_28
            else MCPMetricAdapter.PYTHON_LEGACY
        )
        labels = MCPMetricLabels(
            execution_path=MCPMetricExecutionPath.USER_SCOPED,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
            transport=transport,
            protocol_version=protocol_version,
            adapter=adapter,
            result_category=MCPMetricResultCategory.UNKNOWN,
            error_category=MCPMetricErrorCategory.UNKNOWN,
            call_kind=MCPCallKind.ORDINARY,
        )
        terminal_at = call.terminal_at or self._utcnow_naive()
        terminal_at = (
            terminal_at.replace(tzinfo=timezone.utc)
            if terminal_at.tzinfo is None
            else terminal_at.astimezone(timezone.utc)
        )
        bucket_started_at = terminal_at.replace(second=0, microsecond=0)
        bucket_ended_at = bucket_started_at + timedelta(minutes=1)
        created_at = call.created_at or terminal_at
        created_at = (
            created_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None
            else created_at.astimezone(timezone.utc)
        )
        writes = (
            (
                "ordinary_restart_unknown_total",
                lambda: recorder.record_count(
                    MCPMetricName.TOOL_CALLS_TOTAL,
                    labels=labels,
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                ),
            ),
            (
                "ordinary_restart_unknown_counter",
                lambda: recorder.record_count(
                    MCPMetricName.TOOL_CALL_UNKNOWN_TOTAL,
                    labels=labels,
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                ),
            ),
            (
                "ordinary_restart_unknown_duration",
                lambda: recorder.record_latency(
                    MCPMetricName.TOOL_CALL_DURATION_SECONDS,
                    duration_seconds=max(
                        0.0,
                        (terminal_at - created_at).total_seconds(),
                    ),
                    labels=labels,
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                ),
            ),
        )
        for metric_family, write in writes:
            try:
                await write()
            except Exception:
                await self._record_mcp_metric_gap_best_effort(
                    task=task,
                    call=call,
                    metric_family=metric_family,
                )

    async def _record_mcp_metric_gap_best_effort(
        self,
        *,
        task: Task,
        call: Any,
        metric_family: str,
    ) -> None:
        try:
            await self._record_event(
                self._make_event(
                    task_id=call.task_id,
                    conversation_id=task.conversation_id,
                    node_id=call.node_id,
                    event_type="mcp.rollout_metric_gap",
                    payload={
                        "safe_call_ref": call.call_ref,
                        "metric_family": metric_family,
                        "gap_reason": "metric_recording_failed",
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        except Exception:
            return

    async def shutdown(self) -> None:
        await self._stop_mcp_rollout_instance_lease_renewal()
        if self._mcp_continuation_consumer_task is not None:
            self._mcp_continuation_consumer_task.cancel()
            await asyncio.gather(
                self._mcp_continuation_consumer_task, return_exceptions=True
            )
            self._mcp_continuation_consumer_task = None
        if self._mcp_rollout_zero_series_task is not None:
            self._mcp_rollout_zero_series_task.cancel()
            await asyncio.gather(
                self._mcp_rollout_zero_series_task,
                return_exceptions=True,
            )
            self._mcp_rollout_zero_series_task = None
        pending = [
            *self._running_tasks.values(),
            *self._running_title_tasks,
            *self._running_mcp_shadow_tasks,
            *self._conversation_delete_tasks.values(),
        ]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)
            except asyncio.TimeoutError:
                for handle in pending:
                    handle.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._mysql_adapter is not None:
            await self._mysql_adapter.aclose()
        if self._mcp_audit_retention_task is not None:
            self._mcp_audit_retention_task.cancel()
            await asyncio.gather(self._mcp_audit_retention_task, return_exceptions=True)
            self._mcp_audit_retention_task = None
        if self._mcp_auth_invalidation_task is not None:
            self._mcp_auth_invalidation_task.cancel()
            await asyncio.gather(self._mcp_auth_invalidation_task, return_exceptions=True)
            self._mcp_auth_invalidation_task = None
        if self._mcp_auth_invalidation_queue is not None and self.auth_invalidation_bus is not None:
            self.auth_invalidation_bus.unsubscribe(self._mcp_auth_invalidation_queue)
            self._mcp_auth_invalidation_queue = None
        if self.user_mcp_config_service is not None:
            await self.user_mcp_config_service.aclose()
        if self.mcp_remote_task_recovery_worker is not None:
            await self.mcp_remote_task_recovery_worker.aclose()
        if self.user_mcp_presence_service is not None:
            await self.user_mcp_presence_service.aclose()
        if self.user_mcp_health_runner is not None:
            await self.user_mcp_health_runner.aclose()
        if self.user_mcp_gateway is not None:
            await self.user_mcp_gateway.aclose()
        if self.postgres_mcp_invalidation_bus is not None:
            await self.postgres_mcp_invalidation_bus.aclose()
        if self._mcp_runtime_state is not None:
            await self._mcp_runtime_state.aclose()
        if self.postgres_auth_invalidation_bus is not None:
            await self.postgres_auth_invalidation_bus.aclose()
        if self._mcp_rollout_engine is not None:
            await asyncio.to_thread(self._mcp_rollout_engine.dispose)
        await asyncio.to_thread(self._engine.dispose)

    async def _run_mcp_auth_invalidation_listener(self) -> None:
        queue = self._mcp_auth_invalidation_queue
        presence = self.user_mcp_presence_service
        if queue is None or presence is None:
            return
        while True:
            event = await queue.get()
            await presence.invalidate_owner(
                event.username,
                reason=f"auth_{event.reason}",
            )

    @staticmethod
    def _normalize_conversation_file_selector_mode(raw_mode: object) -> str:
        mode = str(raw_mode or "disabled").strip().lower()
        if mode in CONVERSATION_FILE_SELECTOR_ALLOWED_MODES:
            return mode
        return "disabled"

    def _record_invalid_conversation_file_selector_mode(self, raw_mode: object, normalized_mode: str) -> None:
        mode = str(raw_mode or "disabled").strip().lower()
        if mode in CONVERSATION_FILE_SELECTOR_ALLOWED_MODES or self._audit_sink is None:
            return
        invalid_mode = ConversationFileSelectionRuntimeMixin._safe_audit_text(str(raw_mode or ""))
        self._audit_sink.record_sync(
            CONVERSATION_FILE_SELECTOR_CONFIG_INVALID_EVENT,
            {
                "config_key": "MAF_CONVERSATION_FILE_SELECTOR_MODE",
                "invalid_mode": invalid_mode or "[redacted]",
                "normalized_mode": normalized_mode,
                "reason_code": "invalid_conversation_file_selector_mode",
                "allowed_modes": sorted(CONVERSATION_FILE_SELECTOR_ALLOWED_MODES),
            },
        )

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

    def _ensure_mcp_capability_matches_assignment(
        self,
        capability_id: str | None,
        *,
        execution_mode: str,
    ) -> None:
        if capability_id is None:
            return
        descriptor = self.capability_registry.get(capability_id)
        is_user_scoped = capability_id == "mcp.dispatch"
        is_legacy = descriptor is not None and _is_mcp_descriptor(descriptor)
        if not is_user_scoped and not is_legacy:
            return
        expected_mode = (
            MCPExecutionPath.USER_SCOPED.value
            if is_user_scoped
            else MCPExecutionPath.LEGACY.value
        )
        if execution_mode != expected_mode:
            raise ValueError(
                "mcp_route_assignment_mismatch: requested MCP capability does not "
                "match the task execution path"
            )

    @staticmethod
    def _mcp_task_assignment_metadata(task: Task) -> dict[str, object]:
        values = (
            task.mcp_execution_mode,
            task.mcp_shadow_enabled,
            task.mcp_rollout_config_version,
            task.mcp_route_reason_code,
            task.mcp_rollout_mode,
        )
        if all(value is None for value in values):
            return {}
        if any(value is None for value in values):
            raise ValueError("mcp_task_route_assignment_corrupt")
        return {
            "mcp_execution_mode": task.mcp_execution_mode,
            "mcp_shadow_enabled": task.mcp_shadow_enabled,
            "mcp_rollout_config_version": task.mcp_rollout_config_version,
            "mcp_route_reason_code": task.mcp_route_reason_code,
            "mcp_rollout_mode": task.mcp_rollout_mode,
        }

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

        async def _still_new_conversation() -> bool:
            if existing_conversation is not None:
                tasks = await self.storage.list_tasks_for_conversation(conversation_id)
                if tasks:
                    return False
            return True

        await self._refresh_skills_if_changed(
            reason="conversation_start",
            raise_on_refresh_error=True,
            pre_refresh_check=_still_new_conversation,
        )

    async def refresh_skills_for_capabilities_list(self) -> SkillRuntimeRefreshResult | None:
        return await self._refresh_skills_if_changed(
            reason="capabilities_list",
            raise_on_refresh_error=False,
        )

    async def _refresh_skills_if_changed(
        self,
        *,
        reason: str,
        raise_on_refresh_error: bool,
        pre_refresh_check: Callable[[], Any] | None = None,
    ) -> SkillRuntimeRefreshResult | None:
        if self._skill_runtime_state is None:
            return None
        async with self._skill_refresh_lock:
            if pre_refresh_check is not None:
                should_refresh = pre_refresh_check()
                if inspect.isawaitable(should_refresh):
                    should_refresh = await should_refresh
                if not should_refresh:
                    return None
            self._record_skill_refresh_started(reason)
            previous_revision = self._skill_runtime_state.active_revision
            self._skill_runtime_state.retain_revision(previous_revision)
            try:
                result = self._skill_runtime_state.refresh_if_changed(reason=reason)
                if result.status == "failed":
                    self._record_skill_refresh_audit(result)
                    if raise_on_refresh_error:
                        raise RuntimeError(f"Skill runtime refresh failed: {result.error_type or 'unknown'}")
                    return result
                if result.status == "completed":
                    try:
                        self._sync_skill_capability_registry()
                    except Exception as exc:
                        self._skill_runtime_state.activate_revision(previous_revision)
                        failed_result = self._skill_refresh_sync_failed_result(
                            result,
                            previous_revision=previous_revision,
                            error_type=type(exc).__name__,
                        )
                        self._record_skill_refresh_audit(failed_result)
                        if raise_on_refresh_error:
                            raise
                        return failed_result
                    if self._audit_sink is not None:
                        _record_skill_capability_startup_audit(
                            self._audit_sink,
                            self._skill_runtime_state.active_bundle.skill_capabilities,
                        )
                self._record_skill_refresh_audit(result)
                return result
            finally:
                self._skill_runtime_state.release_revision(previous_revision)

    def _skill_refresh_sync_failed_result(
        self,
        result: SkillRuntimeRefreshResult,
        *,
        previous_revision: str,
        error_type: str,
    ) -> SkillRuntimeRefreshResult:
        active = (
            self._skill_runtime_state.active_bundle
            if self._skill_runtime_state is not None
            else None
        )
        registered_count = result.registered_count
        skipped_count = result.skipped_count
        script_package_snapshot = result.script_package_snapshot
        if active is not None:
            registered_count = len(active.skill_capabilities.descriptors)
            skipped_count = len(active.skill_capabilities.diagnostics)
            script_package_snapshot = active.script_package_snapshot
        return SkillRuntimeRefreshResult(
            status="failed",
            reason=result.reason,
            previous_revision=previous_revision,
            active_revision=previous_revision,
            registered_count=registered_count,
            skipped_count=skipped_count,
            duration_ms=result.duration_ms,
            script_package_snapshot=script_package_snapshot,
            error_type=error_type,
        )

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
        if (
            request.metadata.get("mcp_execution_mode")
            != MCPExecutionPath.LEGACY.value
            or self._mcp_runtime_state is None
            or request.task_id in self._task_mcp_bundle_revisions
        ):
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


def _split_env_values(raw: str | None) -> tuple[str, ...]:
    return tuple(value.strip() for value in str(raw or "").split(",") if value.strip())


def _positive_required_env_int(name: str, *, allow_default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None and allow_default is not None:
        return allow_default
    try:
        value = int(str(raw or ""))
    except ValueError as exc:
        raise ValueError(f"{name} must be explicitly configured as a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be explicitly configured as a positive integer")
    return value


def _resolve_postgres_mcp_rollout_app_runtime(
    *,
    rollout_ledger_active: bool,
    env: Mapping[str, str],
) -> tuple[Engine, Any] | None:
    if not rollout_ledger_active:
        return None

    forbidden_dsn_names = tuple(
        name
        for name in (
            "MAF_MCP_ROLLOUT_SNAPSHOT_DSN",
            "MAF_MCP_ROLLOUT_EVALUATOR_DSN",
            "MAF_MCP_ROLLOUT_OPERATOR_DSN",
            "MAF_MCP_ROLLOUT_DRILL_DSN",
            "MAF_MCP_LEGACY_MIGRATION_DSN",
        )
        if str(env.get(name) or "").strip()
    )
    if forbidden_dsn_names:
        raise ValueError(
            "API runtime must not receive MCP rollout privileged role credentials: "
            + ", ".join(forbidden_dsn_names)
        )

    raw_dsn = env.get("MAF_MCP_ROLLOUT_APP_DSN")
    if raw_dsn is None or not raw_dsn.strip():
        raise ValueError(
            "MAF_MCP_ROLLOUT_APP_DSN is required for canonical MCP rollout on PostgreSQL"
        )
    if raw_dsn != raw_dsn.strip():
        raise ValueError("MAF_MCP_ROLLOUT_APP_DSN is invalid")

    rollout_engine: Engine | None = None
    try:
        rollout_engine = create_postgres_engine(raw_dsn)
        validate_mcp_rollout_connection_role(rollout_engine, "app")
        return rollout_engine, create_postgres_session_factory(rollout_engine)
    except Exception:
        if rollout_engine is not None:
            try:
                rollout_engine.dispose()
            except Exception:
                pass
        raise RuntimeError(
            "MCP rollout PostgreSQL app role is invalid or unavailable"
        ) from None


def _mcp_rollout_ledger_is_active(
    *,
    config: MCPRolloutConfig,
    deployment_env: str,
    env: Mapping[str, str],
) -> bool:
    if (
        deployment_env in {"prod", "production"}
        and config.routing_mode is not MCPRoutingMode.OFF
    ):
        return True
    return any(
        str(env.get(name) or "").strip()
        for name in (
            "MCP_ROLLOUT_ENVIRONMENT_ID",
            "MCP_ROLLOUT_DEPLOYMENT_ID",
            "MCP_ROLLOUT_STAGE",
            "MCP_ROLLOUT_ACTIVATION_ID",
        )
    )


def _resolve_user_mcp_rollout_config(
    *,
    enable_user_mcp: bool | None,
    enable_user_mcp_routing: bool | None,
    env: Mapping[str, str],
) -> tuple[MCPRolloutConfig, bool]:
    """Resolve canonical rollout config without inferring enforce from env flags."""

    canonical_configured = mcp_rollout_env_is_configured(env)
    deprecated_gateway = _optional_deprecated_bool(
        env.get("MAF_USER_MCP_ENABLED"), "MAF_USER_MCP_ENABLED"
    )
    deprecated_routing = _optional_deprecated_bool(
        env.get("MAF_USER_MCP_ROUTING_ENABLED"),
        "MAF_USER_MCP_ROUTING_ENABLED",
    )
    if canonical_configured:
        config = MCPRolloutConfig.from_env(env)
        if enable_user_mcp is not None and enable_user_mcp != config.gateway_enabled:
            raise ValueError(
                "enable_user_mcp conflicts with MCP_USER_SCOPED_GATEWAY_ENABLED"
            )
        if deprecated_gateway is not None and deprecated_gateway != config.gateway_enabled:
            raise ValueError(
                "MAF_USER_MCP_ENABLED conflicts with MCP_USER_SCOPED_GATEWAY_ENABLED"
            )
        routing_enabled = config.routing_mode is not MCPRoutingMode.OFF
        if (
            enable_user_mcp_routing is not None
            and enable_user_mcp_routing != routing_enabled
        ):
            raise ValueError("enable_user_mcp_routing conflicts with MCP_ROUTING_MODE")
        if deprecated_routing is not None and deprecated_routing != routing_enabled:
            raise ValueError(
                "MAF_USER_MCP_ROUTING_ENABLED conflicts with MCP_ROUTING_MODE"
            )
        return config, config.gateway_enabled

    if deprecated_routing:
        raise ValueError(
            "MAF_USER_MCP_ROUTING_ENABLED=true requires an explicit MCP_ROUTING_MODE"
        )
    subsystem_enabled = (
        enable_user_mcp if enable_user_mcp is not None else bool(deprecated_gateway)
    )
    routing_enabled = bool(enable_user_mcp_routing)
    if routing_enabled and not subsystem_enabled:
        raise ValueError("User-scoped MCP routing requires the user MCP subsystem")
    if routing_enabled:
        # Explicit Python arguments are retained for local/test compatibility;
        # deployment environment flags must use the canonical closed contract.
        return (
            MCPRolloutConfig(
                gateway_enabled=True,
                routing_mode=MCPRoutingMode.ENFORCE,
                legacy_enabled=True,
                enforce_percent=100,
                enforce_hash_salt="programmatic-compat-v1",
            ),
            True,
        )
    return MCPRolloutConfig.from_env({}), subsystem_enabled


def _optional_deprecated_bool(raw: str | None, name: str) -> bool | None:
    if raw is None or not raw.strip():
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _resolve_mcp_rollout_instance_admission(
    *,
    config: MCPRolloutConfig,
    deployment_env: str,
    instance_id: str | None,
    env: Mapping[str, str],
) -> _MCPRolloutInstanceAdmission | None:
    names = (
        "MCP_ROLLOUT_ENVIRONMENT_ID",
        "MCP_ROLLOUT_DEPLOYMENT_ID",
        "MCP_ROLLOUT_STAGE",
        "MCP_ROLLOUT_ACTIVATION_ID",
    )
    values = {name: str(env.get(name) or "").strip() for name in names}
    configured = any(values.values())
    required = (
        deployment_env in {"prod", "production"}
        and config.routing_mode is not MCPRoutingMode.OFF
    )
    if not configured and not required:
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(
            "MCP rollout instance admission requires: " + ", ".join(missing)
        )
    if instance_id is None:
        raise ValueError("MCP rollout instance admission requires the user MCP gateway")
    stage = values["MCP_ROLLOUT_STAGE"]
    if config.routing_mode is MCPRoutingMode.SHADOW and stage != "internal_shadow":
        raise ValueError("MCP_ROUTING_MODE=shadow requires MCP_ROLLOUT_STAGE=internal_shadow")
    if config.routing_mode is MCPRoutingMode.ENFORCE:
        allowed = {
            "internal_enforce",
            "cohort_enforce",
            "full_enforce",
            "legacy_assembly_off",
        }
        if stage not in allowed:
            raise ValueError("MCP_ROUTING_MODE=enforce requires an enforce rollout stage")
        if not config.legacy_enabled and stage != "legacy_assembly_off":
            raise ValueError(
                "legacy assembly disabled requires MCP_ROLLOUT_STAGE=legacy_assembly_off"
            )
        if config.legacy_enabled and stage == "legacy_assembly_off":
            raise ValueError(
                "legacy_assembly_off stage requires MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false"
            )
    resolved_instance_id = str(env.get("MCP_ROLLOUT_INSTANCE_ID") or instance_id).strip()
    if not resolved_instance_id:
        raise ValueError("MCP_ROLLOUT_INSTANCE_ID must not be empty")
    return _MCPRolloutInstanceAdmission(
        environment_id=values["MCP_ROLLOUT_ENVIRONMENT_ID"],
        deployment_id=values["MCP_ROLLOUT_DEPLOYMENT_ID"],
        stage=stage,
        activation_id=values["MCP_ROLLOUT_ACTIVATION_ID"],
        instance_id=resolved_instance_id,
    )


def _load_mcp_shadow_runtime_contract(
    *,
    config: MCPRolloutConfig,
    admission: _MCPRolloutInstanceAdmission | None,
    env: Mapping[str, str],
) -> tuple[
    VerifiedShadowScenarioManifest | None,
    Mapping[str, ShadowScenario],
    str | None,
]:
    if (
        config.routing_mode is not MCPRoutingMode.SHADOW
        or admission is None
        or admission.stage != "internal_shadow"
    ):
        return None, {}, None
    manifest_path = str(env.get("MAF_MCP_SHADOW_MANIFEST_PATH") or "").strip()
    keyring_path = str(
        env.get("MAF_MCP_ROLLOUT_ATTESTATION_KEYRING_PATH") or ""
    ).strip()
    raw_bindings = str(
        env.get("MAF_MCP_SHADOW_SCENARIO_BINDINGS_JSON") or ""
    ).strip()
    if not all(
        (
            manifest_path,
            keyring_path,
            raw_bindings,
        )
    ):
        return None, {}, "shadow_verified_manifest_missing"
    try:
        keyring_file = Path(keyring_path)
        keyring_stat = keyring_file.lstat()
        if (
            not keyring_file.is_file()
            or keyring_file.is_symlink()
            or keyring_stat.st_size > 64 * 1024
            or (keyring_stat.st_mode & 0o777) & ~0o440
        ):
            raise ValueError("unsafe shadow keyring file")
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate shadow contract key")
                result[key] = value
            return result

        keyring_raw = json.loads(
            keyring_file.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
        )
        if (
            not isinstance(keyring_raw, dict)
            or set(keyring_raw) != {"schema", "keys"}
            or keyring_raw.get("schema")
            != "maf.user_mcp_phase3_attestation_keyring.v1"
            or not isinstance(keyring_raw.get("keys"), dict)
        ):
            raise ValueError("invalid shadow keyring")
        trusted_keys: dict[str, bytes] = {}
        for key_id, encoded in keyring_raw["keys"].items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise ValueError("invalid shadow keyring entry")
            decoded = base64.b64decode(encoded, validate=True)
            if base64.b64encode(decoded).decode("ascii") != encoded:
                raise ValueError("non-canonical shadow key")
            trusted_keys[key_id] = decoded
        binding_document = json.loads(
            raw_bindings,
            object_pairs_hook=unique_object,
        )
        if not isinstance(binding_document, dict) or not binding_document:
            raise ValueError("invalid shadow scenario bindings")
        bindings = {
            str(capability_id): ShadowScenario(str(scenario))
            for capability_id, scenario in binding_document.items()
            if isinstance(capability_id, str) and capability_id.strip()
        }
        if len(bindings) != len(binding_document):
            raise ValueError("invalid shadow scenario binding")
        fixture_fingerprint = shadow_fixture_bindings_fingerprint(bindings)
        raw_mapping_fingerprint = str(
            env.get("MAF_MCP_SHADOW_MAPPING_SET_FINGERPRINT") or ""
        ).strip()
        if not raw_mapping_fingerprint:
            raise ValueError("shadow mapping-set fingerprint missing")
        manifest = load_signed_shadow_manifest_file(
            manifest_path,
            trusted_attestation_keys=trusted_keys,
            expected_config_fingerprint=config.fingerprint,
            expected_fixture_fingerprint=fixture_fingerprint,
            expected_mapping_fingerprint=raw_mapping_fingerprint,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ShadowManifestError):
        return None, {}, "shadow_verified_manifest_invalid"
    return manifest, bindings, None


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
    conversation_file_store_path: str | Path | None = None,
    runtime_sidecar_client: Any | None = None,
    skill_sandbox_client: Any | None = None,
    enable_user_mcp: bool | None = None,
    enable_user_mcp_routing: bool | None = None,
    user_mcp_credential_key_file: str | Path | None = None,
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

    canonical_mcp_rollout_configured = mcp_rollout_env_is_configured(os.environ)
    mcp_rollout_config, user_mcp_enabled = _resolve_user_mcp_rollout_config(
        enable_user_mcp=enable_user_mcp,
        enable_user_mcp_routing=enable_user_mcp_routing,
        env=os.environ,
    )
    user_mcp_routing_enabled = user_mcp_enabled and (
        mcp_rollout_config.routing_mode in {MCPRoutingMode.SHADOW, MCPRoutingMode.ENFORCE}
    )
    mcp_credential_cipher = (
        CredentialCipher.from_key_file(
            user_mcp_credential_key_file,
            require_read_only=deployment_env in {"prod", "production"},
        )
        if user_mcp_enabled
        else None
    )
    user_mcp_capacity_values = (
        (
            _positive_required_env_int("MAF_USER_MCP_MAX_ACTIVE_CALLS"),
            _positive_required_env_int(
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES"
            ),
        )
        if user_mcp_enabled
        else None
    )

    _bootstrap_state_platform_config_env()
    state_config = build_state_platform_runtime_config(
        env=os.environ,
        require_driver=True,
    )
    canonical_task_authority_mode = runtime_sidecar_mode_for_component(
        "runtime_store"
    )
    if canonical_task_authority_mode == "enforce":
        migration_contract = migration_policy()
        evidence_path_value = os.environ.get(
            migration_contract["task_authority_evidence_path_env"], ""
        ).strip()
        key_path_value = os.environ.get(
            migration_contract["task_authority_hmac_key_path_env"], ""
        ).strip()
        if not evidence_path_value or not key_path_value:
            raise RuntimeError(
                "runtime_store_migration_blocked: Rust runtime sidecar enforce authority "
                "requires authenticated Task migration evidence"
            )
        load_runtime_sidecar_migration_evidence_artifact(
            Path(evidence_path_value),
            authentication_key_path=Path(key_path_value),
        )
    resolved_runtime_sidecar_client = runtime_sidecar_client or _resolve_runtime_sidecar_client_from_env(
        require_runtime_store_attestation=(
            canonical_task_authority_mode == "enforce"
            or (
                canonical_mcp_rollout_configured
                and mcp_rollout_config.routing_mode is MCPRoutingMode.ENFORCE
            )
        )
    )
    if (
        (
            canonical_task_authority_mode in {"shadow", "enforce"}
            or (
                canonical_mcp_rollout_configured
                and mcp_rollout_config.routing_mode
                in {MCPRoutingMode.SHADOW, MCPRoutingMode.ENFORCE}
            )
        )
        and resolved_runtime_sidecar_client is None
    ):
        configured_mode = (
            f"MAF_RUST_RUNTIME_STORE_MODE={canonical_task_authority_mode}"
            if canonical_task_authority_mode in {"shadow", "enforce"}
            else f"MCP_ROUTING_MODE={mcp_rollout_config.routing_mode.value}"
        )
        raise RuntimeError(
            "runtime_store_unavailable: "
            f"{configured_mode} requires a Rust runtime sidecar client"
        )
    audit_sink = JsonlAuditSink(audit_log_path)
    auth_generation_cache = AuthGenerationCache()
    auth_invalidation_bus = InMemoryAuthInvalidationBus()
    postgres_auth_invalidation_bus = None
    mcp_rollout_engine: Engine | None = None
    if state_config.backend == StatePlatformBackend.POSTGRESQL:
        engine = create_postgres_engine(state_config.dsn or "")
        bootstrap_postgres_database(engine)
        mcp_rollout_runtime = (
            _resolve_postgres_mcp_rollout_app_runtime(
                rollout_ledger_active=_mcp_rollout_ledger_is_active(
                    config=mcp_rollout_config,
                    deployment_env=deployment_env,
                    env=os.environ,
                ),
                env=os.environ,
            )
        )
        mcp_rollout_storage_kwargs: dict[str, Any] = {}
        if mcp_rollout_runtime is not None:
            mcp_rollout_engine, mcp_rollout_session_factory = mcp_rollout_runtime
            mcp_rollout_storage_kwargs = {
                "mcp_rollout_session_factory": mcp_rollout_session_factory,
                "mcp_rollout_role": "app",
            }
        storage = PostgreSQLStorage(
            create_postgres_session_factory(engine),
            runtime_sidecar_client=resolved_runtime_sidecar_client,
            runtime_sidecar_shadow_sink=_build_runtime_sidecar_shadow_diff_sink(audit_sink),
            mcp_task_authority_mode=canonical_task_authority_mode,
            **mcp_rollout_storage_kwargs,
        )
        if isinstance(engine, Engine):
            postgres_auth_invalidation_bus = PostgresAuthInvalidationBus(engine, auth_generation_cache)
            postgres_auth_invalidation_bus.check_permission()
            postgres_auth_invalidation_bus.reconcile_once()
        artifact_file_store = LocalArtifactFileStore(artifact_store_path or (Path(database_path).parent / "artifacts"))
        conversation_file_store = LocalConversationFileStore(
            conversation_file_store_path or (Path(database_path).parent / "conversation_files")
        )
    else:
        engine = create_sqlite_engine(database_path)
        bootstrap_sqlite_database(engine)
        storage = SQLiteStorage(
            create_sqlite_session_factory(engine),
            runtime_sidecar_client=resolved_runtime_sidecar_client,
            runtime_sidecar_shadow_sink=_build_runtime_sidecar_shadow_diff_sink(audit_sink),
            mcp_task_authority_mode=canonical_task_authority_mode,
        )
        artifact_file_store = LocalArtifactFileStore(artifact_store_path or (Path(database_path).parent / "artifacts"))
        conversation_file_store = LocalConversationFileStore(
            conversation_file_store_path or (Path(database_path).parent / "conversation_files")
        )

    user_mcp_config_service = None
    user_mcp_health_runner = None
    user_mcp_gateway = None
    mcp_invalidation_bus = None
    postgres_mcp_invalidation_bus = None
    user_mcp_result_store = None
    user_mcp_result_janitor = None
    user_mcp_presence_service = None
    user_mcp_audit_service = None
    mcp_remote_task_recovery_worker = None
    user_mcp_instance_id = None
    if user_mcp_enabled:
        assert mcp_credential_cipher is not None
        assert user_mcp_capacity_values is not None
        endpoint_policy = EndpointPolicy(
            allowlist=EndpointAllowlist.from_values(
                domains=_split_env_values(os.environ.get("MAF_USER_MCP_ALLOWLIST_DOMAINS")),
                cidrs=_split_env_values(os.environ.get("MAF_USER_MCP_ALLOWLIST_CIDRS")),
            )
        )
        credential_resolver = UserMCPCredentialResolver(storage, mcp_credential_cipher)
        recovery_service = MCPRecoveryService(storage, mcp_credential_cipher)
        user_client_factory = UserMCPClientFactory(
            endpoint_policy,
            recovery_service=recovery_service,
        )
        instance_id = f"mcp-instance-{uuid4().hex}"
        user_mcp_instance_id = instance_id

        async def load_remote_task_recovery_server(binding):
            server = await storage.get_user_mcp_server(
                binding.owner_user_id,
                binding.server_id,
            )
            if server is None:
                raise RuntimeError("mcp_recovery_server_unavailable")
            call = await storage.get_mcp_call_record(
                binding.owner_user_id,
                binding.task_id,
                binding.call_ref,
            )
            if (
                call is None
                or call.server_id != binding.server_id
                or call.protocol_version != binding.protocol_version
                or call.server_security_version != server.security_version
            ):
                raise RuntimeError("mcp_recovery_server_security_version_changed")
            return server

        async def create_remote_task_recovery_client(binding):
            server = await load_remote_task_recovery_server(binding)
            request_headers = await credential_resolver.request_headers_for(server)
            return await user_client_factory.create_task_recovery(
                server,
                request_headers,
                protocol_version=binding.protocol_version,
            )

        async def record_remote_task_recovery_event(binding, status: str) -> None:
            task = await storage.get_task(binding.task_id)
            if task is None:
                return
            await record_live_event(
                EventRecord(
                    event_id=f"evt-{uuid4().hex[:12]}",
                    conversation_id=task.conversation_id,
                    task_id=binding.task_id,
                    node_id=binding.node_id,
                    event_type="mcp.remote_task_status_changed",
                    payload={
                        "safe_remote_task_ref": binding.safe_remote_task_ref,
                        "status": status,
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )

        result_root = Path(
            os.environ.get("MAF_USER_MCP_TEMPORARY_RESULT_ROOT")
            or (Path(database_path).parent / "user_mcp_results")
        )
        user_mcp_result_store = MCPTemporaryResultStore(
            result_root,
            memory_threshold_bytes=_positive_required_env_int(
                "MAF_USER_MCP_MEMORY_RESULT_THRESHOLD_BYTES", allow_default=1024 * 1024
            ),
        )
        user_mcp_result_janitor = MCPTemporaryResultJanitor(
            result_root,
            safe_age_seconds=float(
                os.environ.get("MAF_USER_MCP_ORPHAN_SAFE_AGE_SECONDS") or 3600
            ),
        )
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(
                max_active_user_mcp_calls_per_instance=user_mcp_capacity_values[0],
                temporary_disk_low_watermark_bytes=user_mcp_capacity_values[1],
            ),
            storage_root=result_root,
        )
        user_mcp_health_runner = MCPHealthRunner(
            storage=storage,
            instance_id=instance_id,
            client_factory=user_client_factory.create,
            credential_loader=credential_resolver.request_headers_for,
            now_fn=ApiRuntime._utcnow_naive,
        )
        user_mcp_gateway = MCPGateway(
            storage=storage,
            gateway_instance_id=instance_id,
            credential_loader=credential_resolver.request_headers_for,
            client_factory=user_client_factory.create,
            endpoint_revalidator=user_client_factory.revalidate_endpoint,
            readonly_shadow_client_factory=(
                user_client_factory.create_readonly_shadow
            ),
            result_store=user_mcp_result_store,
            capacity=capacity,
            now_fn=ApiRuntime._utcnow_naive,
        )

        async def cancel_offline_user_mcp(
            owner_user_id: str,
            platform_task_id: str,
            reason: str,
        ) -> None:
            del owner_user_id
            await user_mcp_gateway.close_task(platform_task_id, reason)

        user_mcp_presence_service = MCPTaskPresenceService(
            cancel_mcp_task=cancel_offline_user_mcp,
            grace_period_seconds=float(
                os.environ.get("MAF_USER_MCP_SSE_OFFLINE_GRACE_SECONDS") or 300
            ),
            storage=storage,
            instance_id=instance_id,
            now_fn=ApiRuntime._utcnow_naive,
        )
        mcp_invalidation_bus = InMemoryMCPInvalidationBus()

        async def handle_mcp_invalidation(event) -> None:
            version = (
                event.security_version
                if event.action is MCPInvalidationAction.SECURITY_UPDATED
                else None
            )
            await asyncio.gather(
                user_mcp_health_runner.cancel_server(
                    event.owner_user_id,
                    event.server_id,
                    reason=str(event.action),
                    invalidate_before_security_version=version,
                ),
                user_mcp_gateway.invalidate_server(event),
                return_exceptions=True,
            )

        mcp_invalidation_bus.subscribe(handle_mcp_invalidation)
        if state_config.backend == StatePlatformBackend.POSTGRESQL and isinstance(engine, Engine):
            postgres_mcp_invalidation_bus = PostgresMCPInvalidationBus(
                engine, handle_mcp_invalidation
            )
        user_mcp_config_service = UserMCPConfigService(
            storage=storage,
            credential_cipher=mcp_credential_cipher,
            endpoint_policy=endpoint_policy,
            health_runner=user_mcp_health_runner,
            invalidation_bus=CompositeMCPInvalidationPublisher(
                mcp_invalidation_bus, postgres_mcp_invalidation_bus
            ),
            now_fn=ApiRuntime._utcnow_naive,
        )
        user_mcp_audit_service = MCPAuditService(
            storage=storage,
            now_fn=ApiRuntime._utcnow_naive,
        )
    mcp_rollout_instance_admission = _resolve_mcp_rollout_instance_admission(
        config=mcp_rollout_config,
        deployment_env=deployment_env,
        instance_id=user_mcp_instance_id,
        env=os.environ,
    )
    (
        mcp_shadow_manifest,
        mcp_shadow_scenario_bindings,
        mcp_shadow_manifest_gap_reason,
    ) = _load_mcp_shadow_runtime_contract(
        config=mcp_rollout_config,
        admission=mcp_rollout_instance_admission,
        env=os.environ,
    )
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
    if not mcp_rollout_config.legacy_enabled and mcp_runtime_state is not None and mcp_runtime_state.config.enabled:
        raise RuntimeError("MCP legacy runtime was supplied while legacy assembly is disabled")
    resolved_mcp_runtime_state = None
    if mcp_rollout_config.legacy_enabled:
        resolved_mcp_runtime_state = mcp_runtime_state or MCPRuntimeState(
            config=_resolve_mcp_runtime_config(mcp_config),
            client_factory=mcp_client_factory,
            sidecar_client=mcp_sidecar_client,
            reserved_capability_ids=[descriptor.capability_id for descriptor in capability_registry.list()],
        )
    if resolved_mcp_runtime_state is not None and resolved_mcp_runtime_state.config.enabled:
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
    if user_mcp_routing_enabled:
        _register_capability_descriptors(
            capability_registry,
            (MCP_DISPATCH_CAPABILITY_DESCRIPTOR,),
            planner_payload_policies={
                MCP_DISPATCH_CAPABILITY_DESCRIPTOR.capability_id:
                    MCP_DISPATCH_PLANNER_PAYLOAD_POLICY,
            },
        )
        instance_registry.register(build_local_mcp_dispatch_instance())
    event_broker = InMemoryEventBroker(
        audit_sink=audit_sink,
        event_observer=(
            user_mcp_audit_service.observe_event
            if user_mcp_audit_service is not None
            else None
        ),
    )
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
        reasoning_event_publisher=publish_transient_event,
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
        conversation_file_store=conversation_file_store,
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
        reasoning_event_publisher=publish_transient_event,
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
    mcp_dispatch_executor = None
    mcp_dispatch_workflow_provider = None
    mcp_shadow_observer = None
    mcp_rollout_metric_recorder = None
    mcp_dispatch_metric_context = None
    mcp_safety_detectors = None
    if mcp_rollout_instance_admission is not None:
        mcp_rollout_metric_recorder = MCPRolloutMetricRecorder(
            storage,
            MCPRolloutMetricContext(
                environment_id=mcp_rollout_instance_admission.environment_id,
                deployment_id=mcp_rollout_instance_admission.deployment_id,
                stage=MCPRolloutStage(mcp_rollout_instance_admission.stage),
                config_fingerprint=mcp_rollout_config.fingerprint,
            ),
        )
        mcp_dispatch_metric_context = MCPDispatchMetricContext(
            routing_mode=MCPMetricRoutingMode(mcp_rollout_config.routing_mode.value)
        )
        if user_mcp_audit_service is None or user_mcp_gateway is None:
            raise RuntimeError(
                "MCP rollout safety detectors require audit and Gateway boundaries"
            )

        async def record_mcp_safety_metric_gap(gap: MCPSafetyMetricGap) -> None:
            red_line = None if gap.red_line is None else gap.red_line.value
            await user_mcp_audit_service.record(
                owner_user_id="rollout-system",
                event_type="mcp.rollout_metric_gap",
                occurred_at=gap.bucket_ended_at,
                safe_payload={
                    "metric_family": "safety_red_line",
                    "gap_reason": gap.reason_code,
                    **({"reason": red_line} if red_line is not None else {}),
                },
                source_ref=(
                    "mcp-safety-gap:"
                    f"{gap.bucket_started_at.isoformat()}:"
                    f"{gap.bucket_ended_at.isoformat()}:"
                    f"{gap.reason_code}:{red_line or 'all'}"
                ),
            )

        mcp_safety_registry = AuthoritativeMCPSafetyDetectorRegistry(
            mcp_rollout_metric_recorder,
            gap_sink=record_mcp_safety_metric_gap,
            routing_mode=mcp_dispatch_metric_context.routing_mode,
        )
        mcp_safety_detectors = register_authoritative_mcp_safety_detectors(
            mcp_safety_registry
        )
        mcp_rollout_metric_recorder.configure_safety_detector_registry(
            mcp_safety_registry
        )
        user_mcp_gateway.configure_safety_detectors(mcp_safety_detectors)
        user_mcp_audit_service.configure_safety_detector(
            mcp_safety_detectors[MCPSafetyRedLine.SECRET_EXPOSURE]
        )
        if user_mcp_gateway is not None:
            user_mcp_gateway.configure_rollout_metrics(
                mcp_rollout_metric_recorder,
                mcp_dispatch_metric_context.routing_mode,
            )
        if user_mcp_presence_service is not None:
            async def record_disconnect_lease_expired_metric() -> None:
                observed_at = datetime.now(timezone.utc)
                bucket_started_at = observed_at.replace(second=0, microsecond=0)
                await mcp_rollout_metric_recorder.record_count(
                    MCPMetricName.DISCONNECT_LEASE_EXPIRED_TOTAL,
                    labels=MCPMetricLabels(
                        execution_path=MCPMetricExecutionPath.USER_SCOPED,
                        routing_mode=mcp_dispatch_metric_context.routing_mode,
                        result_category=MCPMetricResultCategory.CANCELLED,
                        error_category=MCPMetricErrorCategory.NONE,
                    ),
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_started_at + timedelta(minutes=1),
                )

            user_mcp_presence_service.configure_lease_expired_observer(
                record_disconnect_lease_expired_metric
            )
    if user_mcp_enabled:
        mcp_runtime_holder: dict[str, ApiRuntime] = {}

        async def persist_remote_task_result(
            binding,
            result: Mapping[str, Any],
        ) -> str:
            if user_mcp_result_store is None:
                raise RuntimeError("mcp_remote_task_result_store_unavailable")
            sink = user_mcp_result_store.create_sink(binding.task_id, durable=True)
            try:
                encoded = json.dumps(
                    dict(result),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                await sink.write(encoded)
                persisted = await sink.finalize()
            except BaseException:
                await sink.abort()
                raise
            return persisted.ref

        async def continue_remote_task(
            outbox,
        ) -> MCPContinuationAdmissionResult:
            runtime = mcp_runtime_holder.get("runtime")
            if runtime is None:
                raise RuntimeError("mcp_continuation_runtime_unavailable")
            admitted = outbox
            admission_status = "already_admitted"
            if outbox.continuation_admitted_at is None:
                admitted = await storage.admit_mcp_remote_task_continuation(
                    outbox.outbox_id,
                    claim_owner=str(outbox.claim_owner or ""),
                    claim_token=str(outbox.claim_token or ""),
                    expected_revision=outbox.revision,
                    admitted_at=runtime._utcnow_naive(),
                )
                if admitted is None:
                    raise RuntimeError("mcp_continuation_admission_lost")
                admission_status = "admitted_new"
            # Admission is the durable hand-off boundary. The recovery worker
            # never invokes orchestration directly; ApiRuntime's supervised
            # command consumer owns scheduling and restart reconciliation.
            return MCPContinuationAdmissionResult(admission_status, admitted)

        async def record_remote_task_terminal_metric(
            sample: MCPRemoteTaskTerminalMetricSample,
        ) -> None:
            if mcp_rollout_metric_recorder is None:
                return
            task = await storage.get_task(sample.binding.task_id)
            if (
                task is None
                or task.mcp_execution_mode != MCPExecutionPath.USER_SCOPED.value
                or task.mcp_rollout_mode != MCPRoutingMode.ENFORCE.value
                or task.mcp_rollout_config_version != mcp_rollout_config.config_version
            ):
                raise RuntimeError("mcp_remote_task_metric_assignment_mismatch")
            server = await load_remote_task_recovery_server(sample.binding)
            transport = {
                UserMCPTransport.STREAMABLE_HTTP: MCPMetricTransport.STREAMABLE_HTTP,
                UserMCPTransport.LEGACY_HTTP_SSE: MCPMetricTransport.LEGACY_HTTP_SSE,
            }[server.transport]
            protocol_version = MCPMetricProtocolVersion(
                sample.binding.protocol_version
            )
            adapter = (
                MCPMetricAdapter.PYTHON_2026
                if protocol_version is MCPMetricProtocolVersion.V2026_07_28
                else MCPMetricAdapter.PYTHON_LEGACY
            )
            labels = MCPMetricLabels(
                execution_path=MCPMetricExecutionPath.USER_SCOPED,
                routing_mode=MCPMetricRoutingMode.ENFORCE,
                transport=transport,
                protocol_version=protocol_version,
                adapter=adapter,
                result_category=sample.result_category,
                error_category=sample.error_category,
                call_kind=MCPCallKind.REMOTE_TASK,
            )
            terminal_at = (
                sample.terminal_at.replace(tzinfo=timezone.utc)
                if sample.terminal_at.tzinfo is None
                else sample.terminal_at.astimezone(timezone.utc)
            )
            bucket_started_at = terminal_at.replace(second=0, microsecond=0)
            bucket_ended_at = bucket_started_at + timedelta(minutes=1)
            await mcp_rollout_metric_recorder.record_count(
                MCPMetricName.TOOL_CALLS_TOTAL,
                labels=labels,
                bucket_started_at=bucket_started_at,
                bucket_ended_at=bucket_ended_at,
            )
            if sample.result_category is MCPMetricResultCategory.UNKNOWN:
                await mcp_rollout_metric_recorder.record_count(
                    MCPMetricName.TOOL_CALL_UNKNOWN_TOTAL,
                    labels=labels,
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_ended_at,
                )
            await mcp_rollout_metric_recorder.record_latency(
                MCPMetricName.TOOL_CALL_DURATION_SECONDS,
                duration_seconds=sample.duration_seconds,
                labels=labels,
                bucket_started_at=bucket_started_at,
                bucket_ended_at=bucket_ended_at,
            )

        async def record_remote_task_metric_gap(binding, reason: str) -> None:
            task = await storage.get_task(binding.task_id)
            if task is None:
                return
            await record_live_event(
                EventRecord(
                    event_id=f"evt-{uuid4().hex[:12]}",
                    conversation_id=task.conversation_id,
                    task_id=binding.task_id,
                    node_id=binding.node_id,
                    event_type="mcp.rollout_metric_gap",
                    payload={
                        "safe_call_ref": binding.call_ref,
                        "metric_family": "remote_task_terminal",
                        "gap_reason": reason,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )

        async def record_remote_tasks_active_metric() -> None:
            if mcp_rollout_metric_recorder is None:
                return
            observed_at = datetime.now(timezone.utc)
            bucket_started_at = observed_at.replace(second=0, microsecond=0)
            for raw_version, metric_version, adapter in (
                (
                    MCP_PROTOCOL_VERSION_2025_11_25,
                    MCPMetricProtocolVersion.V2025_11_25,
                    MCPMetricAdapter.PYTHON_LEGACY,
                ),
                (
                    MCP_PROTOCOL_VERSION_2026_07_28,
                    MCPMetricProtocolVersion.V2026_07_28,
                    MCPMetricAdapter.PYTHON_2026,
                ),
            ):
                active_count = await storage.count_active_mcp_remote_task_bindings(
                    rollout_config_version=mcp_rollout_config.config_version,
                    protocol_version=raw_version,
                )
                await mcp_rollout_metric_recorder.record_gauge(
                    MCPMetricName.REMOTE_TASKS_ACTIVE,
                    labels=MCPMetricLabels(
                        execution_path=MCPMetricExecutionPath.USER_SCOPED,
                        routing_mode=MCPMetricRoutingMode.ENFORCE,
                        transport=MCPMetricTransport.STREAMABLE_HTTP,
                        protocol_version=metric_version,
                        adapter=adapter,
                        call_kind=MCPCallKind.REMOTE_TASK,
                    ),
                    bucket_started_at=bucket_started_at,
                    bucket_ended_at=bucket_started_at + timedelta(minutes=1),
                    value=active_count,
                )

        async def record_remote_task_global_metric_gap(reason: str) -> None:
            if audit_sink is None:
                return
            try:
                audit_sink.record_sync(
                    "mcp.rollout_metric_gap",
                    {
                        "metric_family": "remote_tasks_active",
                        "gap_reason": reason,
                    },
                )
            except Exception:
                return

        mcp_remote_task_recovery_worker = MCPRemoteTaskRecoveryWorker(
            storage=storage,
            client_factory=create_remote_task_recovery_client,
            instance_id=instance_id,
            event_sink=record_remote_task_recovery_event,
            terminal_metric_sink=record_remote_task_terminal_metric,
            metric_gap_sink=record_remote_task_metric_gap,
            active_metric_sink=record_remote_tasks_active_metric,
            global_metric_gap_sink=record_remote_task_global_metric_gap,
            result_persister=persist_remote_task_result,
            continuation_sink=continue_remote_task,
            now_fn=ApiRuntime._utcnow_naive,
        )
        if user_mcp_gateway is not None:
            user_mcp_gateway.configure_remote_task_canceller(
                mcp_remote_task_recovery_worker.cancel_remote_task
            )
    if user_mcp_routing_enabled:
        if user_mcp_gateway is None:
            raise RuntimeError("User-scoped MCP routing requires the user MCP Gateway")
        if resolved_planner_text_generator is None:
            raise RuntimeError("User-scoped MCP routing requires the LLM planner")
        mcp_tool_selector = MCPToolSelector(
            text_generator=resolved_planner_text_generator
        )
        mcp_server_router = MCPServerRouter(
            text_generator=resolved_planner_text_generator
        )
        mcp_dispatch_coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=user_mcp_gateway,
            selector=mcp_tool_selector,
            server_router=mcp_server_router,
            live_event_recorder=record_live_event,
            now_fn=ApiRuntime._utcnow_naive,
            metric_recorder=mcp_rollout_metric_recorder,
            metric_context=mcp_dispatch_metric_context,
            safety_detectors=mcp_safety_detectors,
        )
        if mcp_rollout_metric_recorder is not None:
            mcp_rollout_metric_recorder.configure_safety_interval_probes(
                user_mcp_audit_service.attest_safety_interval,
                user_mcp_gateway.attest_safety_interval,
                mcp_dispatch_coordinator.attest_safety_interval,
            )
        mcp_dispatch_executor = MCPDispatchExecutor(
            coordinator=mcp_dispatch_coordinator
        )
        mcp_dispatch_workflow_provider = MCPDispatchWorkflowProvider()
        if mcp_rollout_config.routing_mode is MCPRoutingMode.SHADOW:
            assert mcp_credential_cipher is not None
            mcp_shadow_observer = MCPShadowRuntimeObserver(
                storage=storage,
                gateway=user_mcp_gateway,
                server_router=mcp_server_router,
                selector=mcp_tool_selector,
                endpoint_policy=endpoint_policy,
                digest_key=derive_shadow_catalog_digest_key(
                    mcp_credential_cipher,
                    config_fingerprint=mcp_rollout_config.fingerprint,
                ),
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
                    default_model_edition=default_model_edition(main_agent_llm_runtime.config_snapshot()),
                    model_reasoning_configs=main_agent_llm_runtime.model_reasoning_configs(),
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
                *([mcp_dispatch_executor] if mcp_dispatch_executor is not None else []),
                *(
                    [
                        MCPToolExecutor(
                            runtime_state=resolved_mcp_runtime_state,
                            live_event_recorder=record_live_event,
                            metric_recorder=mcp_rollout_metric_recorder,
                            metric_routing_mode=(
                                None
                                if mcp_dispatch_metric_context is None
                                else mcp_dispatch_metric_context.routing_mode
                            ),
                        )
                    ]
                    if resolved_mcp_runtime_state is not None
                    else []
                ),
            ]
        ),
        completion_policy=CompletionPolicy(),
        backpressure=BackpressureGuard(max_active_tasks=DEFAULT_MAX_ACTIVE_TASKS),
        event_sink=event_broker,
        runtime_replanner=resolved_runtime_replanner,
    )

    runtime = ApiRuntime(
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
            mcp_provider=mcp_dispatch_workflow_provider,
        ),
        mysql_adapter=resolved_mysql_adapter,
        username_token_service=username_token_service,
        conversation_title_generator=resolved_conversation_title_generator,
        upload_store=upload_store,
        conversation_memory_builder=resolved_conversation_memory_builder,
        artifact_file_store=artifact_file_store,
        conversation_file_store=conversation_file_store,
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
        user_mcp_config_service=user_mcp_config_service,
        user_mcp_health_runner=user_mcp_health_runner,
        user_mcp_gateway=user_mcp_gateway,
        mcp_credential_cipher=mcp_credential_cipher,
        mcp_remote_task_recovery_worker=mcp_remote_task_recovery_worker,
        mcp_invalidation_bus=mcp_invalidation_bus,
        postgres_mcp_invalidation_bus=postgres_mcp_invalidation_bus,
        user_mcp_result_store=user_mcp_result_store,
        user_mcp_result_janitor=user_mcp_result_janitor,
        user_mcp_presence_service=user_mcp_presence_service,
        user_mcp_audit_service=user_mcp_audit_service,
        mcp_shadow_observer=mcp_shadow_observer,
        mcp_shadow_manifest=mcp_shadow_manifest,
        mcp_shadow_scenario_bindings=mcp_shadow_scenario_bindings,
        mcp_shadow_manifest_gap_reason=mcp_shadow_manifest_gap_reason,
        user_mcp_routing_enabled=user_mcp_routing_enabled,
        mcp_rollout_config=mcp_rollout_config,
        mcp_rollout_instance_admission=mcp_rollout_instance_admission,
        mcp_rollout_metric_recorder=mcp_rollout_metric_recorder,
        mcp_rollout_engine=mcp_rollout_engine,
    )
    if user_mcp_enabled:
        mcp_runtime_holder["runtime"] = runtime
    return runtime


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

def _resolve_runtime_sidecar_client_from_env(
    *,
    require_runtime_store_attestation: bool = False,
) -> RuntimeSidecarGrpcClient | None:
    endpoint = os.environ.get("MAF_RUNTIME_SIDECAR_ENDPOINT", "").strip()
    if not endpoint:
        return None
    artifact_provenance, allowed_artifact_checksums, allowed_cargo_lock_digests = (
        _resolve_runtime_sidecar_artifact_trust_from_env(
            require_runtime_store_attestation=require_runtime_store_attestation,
        )
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


def _resolve_runtime_sidecar_artifact_trust_from_env(
    *,
    require_runtime_store_attestation: bool = False,
) -> tuple[dict[str, Any] | None, tuple[str, ...], tuple[str, ...]]:
    manifest_path = os.environ.get("MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH", "").strip()
    allowlist_path = os.environ.get("MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH", "").strip()
    if not manifest_path and not allowlist_path:
        if _runtime_sidecar_enforce_enabled(
            require_runtime_store_attestation=require_runtime_store_attestation,
        ):
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


def _runtime_sidecar_enforce_enabled(
    *,
    require_runtime_store_attestation: bool = False,
) -> bool:
    return require_runtime_store_attestation or any(
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
        options = _resolve_runtime_llm_request_options(runtime, _metadata_from_llm_kwargs(kwargs))
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
        options = _resolve_runtime_llm_request_options(main_agent_llm_runtime, _metadata_from_llm_kwargs(kwargs))
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
    reasoning_event_publisher: Callable[[EventRecord], Any] | None,
) -> SkillInputTextGenerator | None:
    if skill_input_text_generator is not None:
        return skill_input_text_generator
    if not enable_skill_input_llm:
        return None

    call_ordinal = 0

    async def generate(prompt: str, **kwargs: Any) -> str:
        nonlocal call_ordinal
        call_ordinal += 1
        call_id = call_ordinal
        options = _resolve_runtime_llm_request_options(main_agent_llm_runtime, _metadata_from_llm_kwargs(kwargs))
        reasoning_context = kwargs.get("reasoning_context")
        reasoning_ordinal = 0

        async def publish_reasoning(delta: str) -> None:
            nonlocal reasoning_ordinal
            if reasoning_event_publisher is None or not isinstance(reasoning_context, Mapping):
                return
            if not delta:
                return
            task_id = _reasoning_context_text(reasoning_context, "task_id")
            conversation_id = _reasoning_context_text(reasoning_context, "conversation_id")
            if not task_id or not conversation_id:
                return
            reasoning_ordinal += 1
            event_type = _reasoning_context_text(reasoning_context, "event_type") or "interrupt.reasoning_delta"
            stage = _reasoning_context_text(reasoning_context, "stage") or "skill_input"
            node_id = _reasoning_context_text(reasoning_context, "node_id")
            maybe_result = reasoning_event_publisher(
                EventRecord(
                    event_id=f"{task_id}:{event_type}:{stage}:{call_id}:{reasoning_ordinal}",
                    conversation_id=conversation_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=event_type,
                    payload={
                        **_reasoning_context_payload(reasoning_context),
                        "delta": delta,
                        "ordinal": reasoning_ordinal,
                        "stage": stage,
                        "call_id": call_id,
                        "response_role": _reasoning_context_text(reasoning_context, "response_role") or "interrupt",
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )
            if inspect.isawaitable(maybe_result):
                await maybe_result

        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
            on_reasoning_delta=(
                publish_reasoning
                if reasoning_event_publisher is not None and isinstance(reasoning_context, Mapping)
                else None
            ),
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
    reasoning_event_publisher: Callable[[EventRecord], Any] | None,
    model_edition_config: Mapping[str, Any] | None = None,
) -> ConversationMemoryBuilder | None:
    if conversation_memory_builder is not None:
        return conversation_memory_builder
    if not enable_conversation_memory:
        return None

    async def generate_summary(prompt: str, **kwargs: Any) -> str:
        summary_metadata = _metadata_from_llm_kwargs(kwargs)
        summary_metadata.pop("main_agent_reasoning_effort", None)
        summary_metadata.pop("main_agent_thinking_enabled", None)
        summary_metadata["deep_thinking"] = False
        options = _resolve_runtime_llm_request_options(
            main_agent_llm_runtime,
            summary_metadata,
        )
        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
        )

    async def generate_resolution(prompt: str, **kwargs: Any) -> str:
        options = _resolve_runtime_llm_request_options(main_agent_llm_runtime, _metadata_from_llm_kwargs(kwargs))
        request = kwargs.get("request")
        reasoning_ordinal = 0

        async def publish_reasoning(delta: str) -> None:
            nonlocal reasoning_ordinal
            if reasoning_event_publisher is None or not isinstance(request, OrchestrationRequest):
                return
            if not delta:
                return
            reasoning_ordinal += 1
            maybe_result = reasoning_event_publisher(
                EventRecord(
                    event_id=f"{request.task_id}:memory.reasoning_delta:resolution:{reasoning_ordinal}",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id="conversation.memory",
                    event_type="memory.reasoning_delta",
                    payload={
                        "delta": delta,
                        "ordinal": reasoning_ordinal,
                        "stage": "conversation_memory_resolution",
                        "response_role": "memory",
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )
            if inspect.isawaitable(maybe_result):
                await maybe_result

        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
            on_reasoning_delta=(
                publish_reasoning
                if reasoning_event_publisher is not None and isinstance(request, OrchestrationRequest)
                else None
            ),
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


def _resolve_runtime_llm_request_options(
    runtime: SharedLLMRuntime,
    metadata: Mapping[str, Any] | None,
    *,
    fallback_reasoning_effort: ReasoningEffort | None = None,
) -> LLMRequestOptions:
    return resolve_llm_request_options(
        metadata,
        fallback_reasoning_effort=fallback_reasoning_effort,
        model_reasoning_configs=runtime.model_reasoning_configs(),
        default_model_edition=runtime.default_model_edition(),
    )


def _reasoning_context_text(context: Mapping[str, Any], key: str) -> str:
    value = context.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _reasoning_context_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("interrupt_id", "slot_collection_id", "capability_id", "skill_name", "reason_code"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()
    return payload


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
    return (
        descriptor.capability_id != "mcp.dispatch"
        and (
            descriptor.kind == "mcp_tool"
            or descriptor.source == "mcp"
            or descriptor.capability_id.startswith("mcp.")
        )
    )


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
        options = _resolve_runtime_llm_request_options(
            main_agent_llm_runtime,
            metadata,
            fallback_reasoning_effort=planner_reasoning_effort,
        )
        reasoning_ordinal = 0

        async def publish_planner_reasoning(delta: str) -> None:
            nonlocal reasoning_ordinal
            if request is None or reasoning_event_publisher is None:
                return
            if not delta:
                return
            reasoning_ordinal += 1
            maybe_result = reasoning_event_publisher(
                EventRecord(
                    event_id=f"{request.task_id}:planner.{stage}.reasoning:{call_id}:{reasoning_ordinal}",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id="main_agent.orchestrator",
                    event_type="planner.reasoning_delta",
                    payload={
                        "delta": delta,
                        "ordinal": reasoning_ordinal,
                        "stage": stage,
                        "call_id": call_id,
                        "response_role": "planner",
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )
            if inspect.isawaitable(maybe_result):
                await maybe_result

        return await main_agent_llm_runtime.generate_text(
            prompt,
            thinking=options.thinking,
            reasoning_effort=options.reasoning_effort,
            model_edition=options.model_edition,
            on_reasoning_delta=(
                publish_planner_reasoning
                if reasoning_event_publisher is not None and request is not None
                else None
            ),
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
