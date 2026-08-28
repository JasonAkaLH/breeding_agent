from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from weakref import WeakValueDictionary

from sqlalchemy import Engine

from src.auth import (
    AuthGenerationCache,
    AuthTokenHasher,
    AuthTokenValidationError,
    InMemoryAuthInvalidationBus,
    UsernameTokenService,
)
from src.integrations.master_key import (
    MasterKeyDeriver,
    MasterKeyDomain,
    MasterKeyError,
)
from src.capabilities.main_agent import (
    SkillOutputArtifactManager,
    StreamGenerator,
)
from src.capabilities.main_agent.prompt_builder import (
    MAIN_AGENT_SKILL_DOCUMENT_GROUNDING_CONSTRAINT,
    MAIN_AGENT_SYSTEM_CONTRACT_LINES,
)
from src.capabilities.mcp_dispatch import (
    MCP_DISPATCH_CAPABILITY_DESCRIPTOR,
    MCPDispatchExecutor,
    MCPServerRouter,
    MCPToolSelector,
    build_local_mcp_dispatch_instance,
)
from src.capabilities.mcp_tool import build_local_mcp_tool_instance
from src.capabilities.skill_tool import SkillExecutor, build_local_skill_executor_instance
from src.core.enums import ConversationStatus, EventVisibility, InterruptStatus, MessageRole, NodeStatus, RoutingMode, TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.contracts import MCPRemoteTaskStoragePort
from src.core.errors import MessageIdentityConflictError
from src.core.models import (
    Conversation,
    ConversationAdmissionCloseDisposition,
    ConversationAdmissionCloseRequest,
    ConversationFileResource,
    ConversationMemorySummary,
    EventRecord,
    Interrupt,
    InterruptAnswer,
    Message,
    MCPShadowAuditSample,
    MCPRolloutInstanceConfigLease,
    MCPNoServerConvergenceResult,
    MCPValidatedTerminalResultCandidate,
    MCPTerminalState,
    MessageIdentityDisposition,
    MessageIdentityKind,
    MessageIdentityReservationRequest,
    PendingSkillContext,
    SlotCollection,
    SlotEvent,
    SubmissionAdmissionDisposition,
    SubmissionPreparationReceipt,
    SubmissionRecoveryRecord,
    Task,
    TaskInputAttachment,
    TaskNode,
)
from src.api.file_selection import (
    FileRequirementProfile,
    candidate_from_resource,
    render_file_selection_question,
)
from src.api.file_selection_runtime import (
    ConversationFileSelectionRuntimeMixin,
    FileSelectionComputation,
)
from src.api.upload_runtime import ConversationUploadRuntimeMixin
from src.integrations.audit_logger import JsonlAuditSink
from src.integrations.agent_skills import (
    PROJECT_SKILL_BUNDLE_DIGEST_ENV,
    ProjectSkillBundleDigestError,
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
    build_public_skill_profile,
    build_history_recall_prompt,
    build_normal_extraction_prompt,
    initialize_input_collection,
    load_input_schemas_for_contract,
    merge_slot_extraction_results,
    parse_slot_extraction_response,
    resolve_skill_execution_config,
    schema_from_snapshot,
    select_input_schema,
    should_trigger_history_recall,
    transition_slot_collection,
    validate_project_skill_bundle_digest,
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
from src.integrations.agent_model_gate import (
    agent_ready_model_edition_options,
    validate_agent_model_edition,
    validate_agent_model_gate,
)
from src.integrations.llm_client import DEFAULT_CONFIG_PATH, LLMClient, ReasoningEffort, bootstrap_config_env, load_config
from src.integrations.llm_request_options import (
    LLMRequestOptions,
    resolve_llm_model_edition,
    resolve_llm_reasoning_effort,
    resolve_llm_request_options,
    resolve_llm_thinking_enabled,
)
from src.integrations.llm_runtime import SharedLLMRuntime
from src.integrations.stream_agent_model_adapter import StreamAgentModelAdapter
from src.integrations.model_editions import (
    config_for_model_edition,
    default_model_edition,
    model_reasoning_effort_configs,
    trim_max_tokens_for_model_edition,
    validate_model_reasoning_effort_configs,
)
from src.integrations.token_counter import get_num_of_tokens_from_messages_async
from src.integrations.mcp import MCPRuntimeBundle, MCPRuntimeConfig, MCPRuntimeRefreshResult, MCPRuntimeState, load_mcp_server_config
from src.integrations.mcp.credentials import (
    MCPAuditReferenceSigner,
    MCPCredentialCipher,
    MCPRecoveryCipher,
    MCPRequestStateEvidenceAuthority,
    MCPRecoveryService,
    MasterKeySentinelCipher,
)
from src.integrations.mcp.audit import MCPAuditService
from src.integrations.mcp.aggregate_recovery import (
    MCPAggregateRecoveryStages,
    MCPAggregateStartupReconciler,
)
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.gateway import MCPGateway
from src.integrations.mcp.dispatch_coordinator import (
    MCPDispatchMetricContext,
    UserMCPDispatchCoordinator,
)
from src.integrations.mcp.cp7_terminal_results import (
    MCPTerminalCandidateSnapshotAuthority,
    TERMINAL_CANDIDATE_WARNING_THRESHOLD,
    enumerate_unconsumed_terminal_result_candidates,
    terminal_now_utc_second,
    seal_terminal_result_candidate,
    secure_read_terminal_result_candidate,
    secure_read_terminal_result_candidate_active_or_archive,
)
from src.integrations.mcp.cp7_terminal_lifecycle import (
    MCPTerminalCandidateLifecycleManager,
)
from src.integrations.mcp.durable_result_lifecycle import (
    MCPDurableResultLifecycleManager,
)
from src.integrations.mcp.result_artifact_projection import (
    MCP_RESULT_ARTIFACT_PROJECTION_EVENT,
    MCPResultArtifactProjectionObservation,
    MCPResultArtifactProjector,
    fold_mcp_result_artifact_projection_payloads,
)
from src.integrations.mcp.result_parsing import (
    MCPHistoricalResultReprojector,
    MCPIsolatedResultService,
    MCPProjectionStore,
    MCPResultDecodeRequest,
    MCPResultParserObservation,
    MCPResultSource,
    MCPRawResultAuthorityResolver,
)
from src.integrations.mcp.result_parsing.json_values import canonical_json_bytes
from src.integrations.mcp.pending_action_payloads import (
    MAX_PENDING_ACTION_ARGUMENT_BYTES,
    MCPPendingActionPayloadCipher,
    MCPPendingActionPayloadStore,
)
from src.integrations.mcp.selector_context import (
    MCPDurableSelectorContextBuilder,
    MCPPublishedAgentProjectionAuthority,
)
from src.integrations.mcp.cp7_artifacts import (
    mcp_dispatch_resume_outbox_id,
    mcp_terminal_receipt_id,
    mcp_terminal_candidate_id,
    canonical_sha256,
    mcp_no_server_intent_id,
)
from src.integrations.mcp.resume_envelope import (
    MCPDispatchResumeEnvelopeError,
    mcp_dispatch_resume_envelope_version,
    validate_mcp_dispatch_resume_envelope_v2,
)
from src.integrations.mcp.cp7_safety import (
    CP7BoundaryEvidence,
    CP7LocalSafetyFacade,
    CP7PredecessorClose,
    CP7RuntimeIdentity,
    CP7SafetyFatalPersistenceError,
    cp7_runtime_safety_wiring,
)
from src.integrations.mcp.health import MCPHealthRunner
from src.integrations.mcp.recovery_worker import (
    MCPContinuationAdmissionResult,
    MCPRemoteTaskProcessedResult,
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
    MCPDurableResultSnapshotAuthority,
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
from src.lifecycle.agent_run_recovery import (
    AgentAuthorityResolution,
    AgentRunRecoveryCoordinator,
)
from src.lifecycle.conversation_guard import ConversationSerialGuard
from src.lifecycle.errors import ConversationBusyError
from src.lifecycle.interrupt_service import InterruptService
from src.lifecycle.mcp_presence import MCPTaskPresenceService
from src.orchestration.answer_selection import select_final_text_artifact
from src.orchestration.capability_fallback import (
    CAPABILITY_MISSING_FALLBACK_EVENT,
    CAPABILITY_MISSING_FALLBACK_KEY,
    merge_capability_missing_fallback_metadata,
)
from src.orchestration.backpressure import DEFAULT_MAX_ACTIVE_TASKS, BackpressureGuard
from src.orchestration.agent_loop import (
    AgentCallExecution,
    AgentContextBudget,
    AgentContextCandidateBuilder,
    AgentContextBuilder,
    AgentContextRules,
    AgentCancellationToken,
    AgentCallOutcomeStatus,
    AgentContinuationLocatorService,
    AgentCompactionService,
    AgentExecutionRequest,
    AgentFinalOutputPublisher,
    AgentLoopOrchestrator,
    AgentLoopRunner,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentStorageConflict,
    RunBoundMCPTextGenerator,
    AgentToolCatalogBuilder,
    AgentSkillResultArtifactStager,
    AgentSkillResultArtifactJanitor,
    AgentTransientSkillResultStore,
    AgentTransientSkillResultResolver,
    CapabilityInvocationService,
    CapabilityVisibilityContext,
    DelegatedSkillActivationService,
    build_canonical_skill_activation,
    build_delegated_skill_instruction_result,
    build_agent_terminal_event,
    default_agent_invocation_policy,
)
from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.capability_invoker import (
    AgentCapabilityInvoker,
    AgentInvocationContextStore,
)
from src.orchestration.agent_loop.observability import (
    AgentResultProjectionObservation,
    build_agent_result_projected_event,
)
from src.orchestration.agent_loop.task_projection import (
    AgentTaskInvocationCommitPort,
)
from src.orchestration.agent_loop.repository import AgentRunRepository
from src.orchestration.conversation_memory import (
    ConversationMemoryBuilder,
    ConversationMemoryConfig,
    ConversationMemoryContext,
    ConversationMemoryStoragePort,
    ResolutionGenerator,
)
from src.orchestration.models import CapabilityDescriptor, UserMCPServerProfile
from src.orchestration.visible_message_history import (
    INTERRUPT_VISIBLE_STREAM_STATUS,
    interrupt_visible_message_id,
    persist_interrupt_question_message,
)
from src.orchestration.composite_executor import CompositeExecutor
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.instance_selector import InstanceSelector
from src.storage import StoragePort
from src.storage.rust_contract import (
    error_policy,
    load_runtime_sidecar_contract,
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
from src.storage.sqlite import SQLiteAgentRepository, SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory
from src.storage.postgres import PostgreSQLAgentRepository, PostgreSQLStorage, bootstrap_postgres_database, create_postgres_engine, create_postgres_session_factory
from src.storage.runtime_sidecar_agent_repository import RuntimeSidecarAgentRepository
from src.storage.postgres.session import validate_mcp_rollout_connection_role
from src.auth.postgres_invalidation_bus import PostgresAuthInvalidationBus
from src.state.runtime_factory import StatePlatformBackend, build_state_platform_runtime_config
from src.storage.artifact_files import (
    LocalArtifactFileStore,
    is_active_managed_output_file,
    parse_file_storage_ref,
)
from src.storage.conversation_files import (
    ConversationFileIndexWriter,
    LocalConversationFileStore,
    build_file_upload_message_projection,
)
from src.api.submission_admission import (
    build_submission_admission_request,
    DurableSubmissionHandoff,
    PreparedAgentRecoveryContext,
    PreparedAgentRecoveryLoader,
    SubmissionAdmissionCoordinator,
    SubmissionPreparedAgentRecoveryLoader,
    submission_interrupt_handoff_id,
    submission_memory_event_id,
)

from .conversation_titles import (
    ConversationTitleGenerator,
    build_conversation_title_prompt,
    build_conversation_title_source,
    call_title_generator,
    normalize_generated_conversation_title,
    validate_conversation_title,
)
from .agent_projection import AgentTaskProjectionService
from .dto import SubmitMessageRequest
from .mcp_binding import (
    MCP_BINDING_MODE_EXPLICIT_COMMAND,
    MCPBindingFeatureUnavailableError,
    MCPBoundServerUnavailableError,
    MCPPersistedBindingError,
    MCP_SERVER_BADGE_METADATA_KEY,
    MCP_SERVER_BINDING_CONTEXT_METADATA_KEY,
    MCP_SERVER_BINDING_METADATA_KEY,
    ResolvedMCPServerBinding,
    build_resolved_mcp_server_binding,
    normalize_mcp_server_id,
    parse_persisted_mcp_server_binding_context,
)
from .sse import InMemoryEventBroker, is_frontend_event
from .table_upload_normalizer import normalize_selected_spreadsheet_sheet, normalize_table_upload
from .upload_store import InMemoryUploadStore, UploadedFileRecord, UploadValidationError, _decode_plain_text_upload


logger = logging.getLogger(__name__)


class SubmissionAdmissionUnavailableError(RuntimeError):
    code = "submission_admission_unavailable"


class SkillHintUnavailableError(RuntimeError):
    code = "skill_hint_unavailable"


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
        "mcp_dispatch_server_id",
    }
)
SYSTEM_MANAGED_METADATA_KEYS = frozenset(
    {
        "skill_bundle_revision",
        "bundle_revision",
        "pinned_bundle_revision",
        "profile",
        "public_skill_profile",
        "profile_digest",
        "activation",
        "skill_activation",
        "skill_activation_payload",
        "skill_activation_payload_sha256",
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
        MCP_SERVER_BINDING_METADATA_KEY,
        MCP_SERVER_BINDING_CONTEXT_METADATA_KEY,
        MCP_SERVER_BADGE_METADATA_KEY,
        "mcp_binding_mode",
        "forced_by_mcp_command",
        "mcp_command",
    }
)


async def _mark_remote_continuation_dispatched(
    storage: MCPRemoteTaskStoragePort,
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
_SUBMISSION_INPUT_METADATA_KEY = "__maf_private_submission_input_v1"
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


@dataclass(frozen=True, slots=True)
class _ReservedInterruptMessage:
    message: Message
    request: MessageIdentityReservationRequest


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
class _MCPShadowInvocationHandle:
    task: Task
    node_id: str
    owner_user_id: str
    user_request: str
    scenario: ShadowScenario
    binding: Any
    approved_mappings: tuple[Any, ...]
    observer_task: asyncio.Task[Any]


class ApiRuntime(
    ConversationFileSelectionRuntimeMixin,
    ConversationUploadRuntimeMixin,
):
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
        agent_loop_orchestrator: AgentLoopOrchestrator,
        agent_run_repository: AgentRunRepository,
        agent_task_projection: AgentTaskProjectionService,
        agent_capability_invoker: AgentCapabilityInvoker,
        agent_invocation_contexts: AgentInvocationContextStore,
        agent_run_recovery: AgentRunRecoveryCoordinator,
        main_agent_llm_runtime: SharedLLMRuntime,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
        username_token_service: UsernameTokenService | None = None,
        master_key_sentinel_cipher: MasterKeySentinelCipher,
        mcp_audit_reference_signer: MCPAuditReferenceSigner,
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
        mcp_credential_cipher: MCPCredentialCipher | None = None,
        mcp_remote_task_recovery_worker: MCPRemoteTaskRecoveryWorker | None = None,
        mcp_invalidation_bus: InMemoryMCPInvalidationBus | None = None,
        postgres_mcp_invalidation_bus: PostgresMCPInvalidationBus | None = None,
        user_mcp_result_store: MCPTemporaryResultStore | None = None,
        mcp_pending_action_payload_store: MCPPendingActionPayloadStore | None = None,
        mcp_terminal_candidate_snapshot_authority: (
            MCPTerminalCandidateSnapshotAuthority | None
        ) = None,
        mcp_durable_result_snapshot_authority: (
            MCPDurableResultSnapshotAuthority | None
        ) = None,
        mcp_terminal_candidate_lifecycle_manager: (
            MCPTerminalCandidateLifecycleManager | None
        ) = None,
        mcp_durable_result_lifecycle_manager: (
            MCPDurableResultLifecycleManager | None
        ) = None,
        mcp_result_artifact_projector: MCPResultArtifactProjector | None = None,
        mcp_projection_store: MCPProjectionStore | None = None,
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
        mcp_terminal_result_root: Path | None = None,
        mcp_legacy_retirement_binding: tuple[str, str] | None = None,
        mcp_cp7_safety_facade: CP7LocalSafetyFacade | None = None,
        mcp_cp7_open_boundary: CP7BoundaryEvidence | None = None,
        mcp_cp7_boundary_provider: Callable[[], Any] | None = None,
        mcp_cp7_fatal_exit: Callable[[int], None] = os._exit,
        mcp_cp7_safety_probes: tuple[Callable[[datetime, datetime], None], ...] = (),
        mcp_cp7_predecessor_close: CP7PredecessorClose | None = None,
        mcp_cp7_verifier_authorized: bool = False,
        mcp_cp7_maintenance_authorization: object | None = None,
        mcp_cp7_maintenance_authorizer: Callable[[object], bool] | None = None,
        submission_admission_coordinator: SubmissionAdmissionCoordinator | None = None,
        prepared_agent_recovery_loader: PreparedAgentRecoveryLoader | None = None,
        expected_submission_authority_receipt_sha256: str | None = None,
    ) -> None:
        self._engine = engine
        self._mcp_rollout_engine = mcp_rollout_engine
        self.storage = storage
        self.capability_registry = capability_registry
        self.instance_registry = instance_registry
        self.event_broker = event_broker
        self.cancellation_service = cancellation_service
        self.interrupt_service = interrupt_service
        self.agent_loop_orchestrator = agent_loop_orchestrator
        self.agent_run_repository = agent_run_repository
        self.agent_task_projection = agent_task_projection
        self._agent_capability_invoker = agent_capability_invoker
        self._agent_invocation_contexts = agent_invocation_contexts
        self._agent_run_recovery = agent_run_recovery
        self._main_agent_llm_runtime = main_agent_llm_runtime
        self._mysql_adapter = mysql_adapter
        self.username_token_service = username_token_service
        self._master_key_sentinel_cipher = master_key_sentinel_cipher
        self._mcp_audit_reference_signer = mcp_audit_reference_signer
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
        self._mcp_pending_action_payload_store = mcp_pending_action_payload_store
        self._mcp_terminal_candidate_snapshot_authority = (
            mcp_terminal_candidate_snapshot_authority
        )
        self._mcp_durable_result_snapshot_authority = (
            mcp_durable_result_snapshot_authority
        )
        self._mcp_terminal_candidate_lifecycle_manager = (
            mcp_terminal_candidate_lifecycle_manager
        )
        self._mcp_durable_result_lifecycle_manager = (
            mcp_durable_result_lifecycle_manager
        )
        self._mcp_result_artifact_projector = mcp_result_artifact_projector
        self._mcp_projection_store = mcp_projection_store
        self._mcp_agent_projection_authority = (
            None
            if mcp_projection_store is None
            else MCPPublishedAgentProjectionAuthority(
                storage=storage,
                projection_store=mcp_projection_store,
            )
        )
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
        self._mcp_terminal_result_root = mcp_terminal_result_root
        self._mcp_startup_terminal_candidates: tuple[Any, ...] | None = None
        self._mcp_legacy_retirement_binding = mcp_legacy_retirement_binding
        self._mcp_cp7_safety_facade = mcp_cp7_safety_facade
        self._mcp_cp7_open_boundary = mcp_cp7_open_boundary
        self._mcp_cp7_boundary_provider = mcp_cp7_boundary_provider
        self._mcp_cp7_fatal_exit = mcp_cp7_fatal_exit
        self._mcp_cp7_safety_probes = mcp_cp7_safety_probes
        self._mcp_cp7_predecessor_close = mcp_cp7_predecessor_close
        self._mcp_cp7_verifier_authorized = mcp_cp7_verifier_authorized
        self._mcp_cp7_maintenance_authorization = mcp_cp7_maintenance_authorization
        self._mcp_cp7_maintenance_authorizer = mcp_cp7_maintenance_authorizer
        self._submission_admission_coordinator = submission_admission_coordinator
        self._prepared_agent_recovery_loader = prepared_agent_recovery_loader
        self._agent_skill_result_janitor: AgentSkillResultArtifactJanitor | None = None
        self._submission_claim_owner = f"api-submission:{uuid4().hex}"
        self._submission_claim_ttl = timedelta(seconds=30)
        self._expected_submission_authority_receipt_sha256 = (
            expected_submission_authority_receipt_sha256
        )
        self._mcp_cp7_requests_stopped = False
        self._mcp_cp7_minute_task: asyncio.Task[None] | None = None
        self._mcp_cp7_clock = lambda: datetime.now(timezone.utc)
        self._mcp_cp7_sleep = asyncio.sleep
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
        validate_agent_model_gate(self._model_edition_config)
        self._model_reasoning_configs = model_reasoning_effort_configs(self._model_edition_config)
        self._default_model_edition = default_model_edition(self._model_edition_config)
        self._runtime_sidecar_shadow_sink = _build_runtime_sidecar_shadow_diff_sink(audit_sink)
        configure_safety_shadow_sink(_build_safety_kernel_shadow_diff_sink(audit_sink))
        self._conversation_guard = ConversationSerialGuard(storage)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._execution_generations: dict[str, int] = {}
        self._execution_durable_starts: dict[str, asyncio.Future[None]] = {}
        self._submission_initialized_agent_runs: dict[str, Any] = {}
        self._submission_woken_agent_ids: set[str] = set()
        self._submission_wakeup_flights: dict[str, asyncio.Future[None]] = {}
        self._submission_file_selection_computations: dict[
            str, FileSelectionComputation
        ] = {}
        self._submission_selector_facts: dict[str, dict[str, Any]] = {}
        self._execution_wait_timeout_seconds = 30.0
        self._conversation_delete_tasks: dict[str, asyncio.Task[dict[str, object]]] = {}
        self._conversation_delete_local_runner_ids: dict[str, str] = {}
        self._locally_cancelled_task_ids = local_cancelled_task_ids if local_cancelled_task_ids is not None else set()
        self._agent_cancellation_tokens: dict[str, AgentCancellationToken] = {}
        self._agent_run_lease_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._agent_run_lease_retry_errors: dict[str, str] = {}
        self._agent_run_lease_retry_sleep = asyncio.sleep
        self._agent_run_recovery_fatal_exit = os._exit
        self._running_title_tasks: set[asyncio.Task[None]] = set()
        self._running_title_conversation_ids: set[str] = set()
        self._running_mcp_shadow_tasks: set[asyncio.Task[None]] = set()
        self._task_skill_bundle_revisions: dict[str, str] = {}
        self._task_mcp_bundle_revisions: dict[str, str] = {}
        self._task_sheet_selection_resume_metadata: dict[str, dict[str, Any]] = {}
        self._task_file_selection_resume_metadata: dict[str, dict[str, Any]] = {}
        self._assistant_history_sync_failure_task_ids: set[str] = set()
        self._assistant_history_sync_failure_lock = asyncio.Lock()
        self._interrupt_answer_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._mcp_auth_invalidation_queue = None
        self._mcp_auth_invalidation_task: asyncio.Task[None] | None = None
        self._mcp_audit_retention_task: asyncio.Task[None] | None = None
        self._mcp_continuation_consumer_task: asyncio.Task[None] | None = None
        self._mcp_post_ready_recovery_task: asyncio.Task[None] | None = None
        self._mcp_post_ready_recovery_error: BaseException | None = None
        self._mcp_result_artifact_reconciler_task: asyncio.Task[None] | None = None
        self._mcp_result_artifact_reconciler_error: BaseException | None = None
        self._mcp_result_artifact_reconciler_sleep = asyncio.sleep
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

    @staticmethod
    def _agent_owner_scope(username: str) -> str:
        return hashlib.sha256(
            f"agent-owner-scope-v1\0{username}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _conversation_admission_close_operation_id(
        username: str,
        conversation_id: str,
    ) -> str:
        identity = json.dumps(
            [username, conversation_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(
            b"maf.conversation.admission-close.v1\0" + identity
        ).hexdigest()
        return f"conversation-close:{digest}"

    def _agent_cancellation_token(self, task_id: str) -> AgentCancellationToken:
        token = self._agent_cancellation_tokens.get(task_id)
        if token is None:
            token = AgentCancellationToken()
            self._agent_cancellation_tokens[task_id] = token
        return token

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
        options = agent_ready_model_edition_options(self._model_edition_config)
        return {
            "default_model_edition": self._default_model_edition,
            "options": [
                {
                    "value": option.value,
                    "label": option.label,
                    "reasoning_efforts": (
                        {
                            "options": [
                                {
                                    "value": effort.value,
                                    "label": effort.label,
                                }
                                for effort in option.reasoning_efforts.options
                            ],
                            "thinking": {
                                "enabled": {
                                    "default": option.reasoning_efforts.thinking.enabled.default,
                                    "supported": list(option.reasoning_efforts.thinking.enabled.supported),
                                },
                                "disabled": {
                                    "default": option.reasoning_efforts.thinking.disabled.default,
                                    "supported": list(option.reasoning_efforts.thinking.disabled.supported),
                                },
                            },
                        }
                        if option.reasoning_efforts is not None
                        else None
                    ),
                }
                for option in options
            ],
        }

    def _validate_requested_model_edition(self, model_edition: str | None) -> str | None:
        return validate_agent_model_edition(model_edition, config=self._model_edition_config)

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
        root_submission_replay = await self._is_existing_root_submission(
            request.client_message_id
        )
        if (
            MCP_SERVER_BINDING_METADATA_KEY not in request.metadata
            and not root_submission_replay
        ):
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

    async def _is_existing_root_submission(
        self,
        message_id: str | None,
    ) -> bool:
        if not message_id:
            return False
        message = await self.storage.get_message(message_id)
        if message is None or message.task_id is None:
            return False
        task = await self.storage.get_task(message.task_id)
        return task is not None and task.root_message_id == message_id

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
        now = self._utcnow_naive()
        message_id = request.client_message_id or self._make_id("msg")
        task_id = self._make_id("task")
        bound_server_id = self._mcp_server_binding_id(request)
        cp7_safety_facade = getattr(self, "_mcp_cp7_safety_facade", None)
        if (
            getattr(self, "_mcp_cp7_requests_stopped", False)
            or cp7_safety_facade is not None
            and not await cp7_safety_facade.ensure_ready()
        ):
            if bound_server_id is not None:
                raise MCPBindingFeatureUnavailableError("mcp_cp7_runtime_not_ready")
            raise RuntimeError("mcp_cp7_runtime_not_ready")
        if bound_server_id is None:
            self._ensure_mcp_rollout_instance_admitted()
        selected_model_edition = self._validate_requested_model_edition(request.model_edition)
        existing_conversation = await self.storage.get_conversation(conversation_id)
        existing_message = await self.storage.get_message(message_id)
        conversation_context_authorized = (
            existing_conversation is not None
            and existing_conversation.username == authenticated_username
            and existing_conversation.status == ConversationStatus.ACTIVE
        )
        if existing_message is None and existing_conversation is not None:
            if not conversation_context_authorized:
                raise PermissionError(
                    f"Conversation is not available: {conversation_id}"
                )
        may_resolve_conversation_context = (
            existing_message is None
            or (
                existing_message.conversation_id == conversation_id
                and conversation_context_authorized
            )
        )
        resolved_mcp_binding = (
            await self._resolve_mcp_server_binding_preflight(
                authenticated_username,
                bound_server_id,
            )
            if bound_server_id is not None
            else None
        )
        if resolved_mcp_binding is not None:
            self._ensure_mcp_rollout_instance_admitted()
        if may_resolve_conversation_context:
            await self._refresh_skills_for_new_conversation_if_needed(
                conversation_id, existing_conversation
            )
        routing_mode = self._routing_mode(request.routing_mode)
        requested_capability_id = self._canonical_capability_id(request.capability_id)
        skill_activation = None
        if routing_mode == RoutingMode.HINT:
            skill_activation = self._build_skill_hint_activation(
                requested_capability_id
            )
        else:
            self._ensure_supported_capability(requested_capability_id)
        explicit_force_capability = routing_mode == RoutingMode.FORCE_CAPABILITY and requested_capability_id is not None
        continued_pending_context: PendingSkillContext | None = None
        if (
            may_resolve_conversation_context
            and resolved_mcp_binding is None
            and not explicit_force_capability
            and requested_capability_id is None
        ):
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
        if (
            resolved_mcp_binding is not None
            and mcp_assignment.real_path is not MCPExecutionPath.USER_SCOPED
        ):
            raise MCPBindingFeatureUnavailableError("mcp_user_scoped_runtime_unavailable")
        if (
            may_resolve_conversation_context
            and mcp_assignment.real_path is MCPExecutionPath.LEGACY
        ):
            await self._refresh_mcp_for_new_conversation_if_needed(
                conversation_id,
                existing_conversation,
            )
        self._ensure_mcp_capability_matches_assignment(
            requested_capability_id,
            execution_mode=mcp_assignment.real_path.value,
        )
        explicit_upload_ids = self._normalize_upload_ids(
            request.metadata.get("upload_ids") or ()
        )
        request_sheet_selections = self._normalize_upload_sheet_selections(
            request.metadata.get("upload_sheet_selections")
        )
        selected_upload_refs: tuple[Mapping[str, Any], ...] = ()
        if existing_message is None:
            upload_resolution = await self.resolve_uploads_for_submission(
                conversation_id,
                authenticated_username,
                explicit_upload_ids,
                upload_sheet_selections=request_sheet_selections,
            )
            self._raise_missing_uploads(
                upload_resolution.missing_upload_ids,
                context="message submission",
            )
            conversation_upload_resolution = (
                await self.resolve_conversation_uploads_for_submission(
                    conversation_id,
                    authenticated_username,
                    upload_sheet_selections=request_sheet_selections,
                )
            )
            selected_resolution = (
                upload_resolution
                if resolved_mcp_binding is not None
                else conversation_upload_resolution
            )
            selected_upload_refs = tuple(
                item.to_continuation_dict()
                for item in selected_resolution.upload_refs
            )
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
        explicit_mcp_dispatch = (
            explicit_force_capability and requested_capability_id == "mcp.dispatch"
        )
        metadata = self._drop_user_supplied_system_metadata(request.metadata)
        if resolved_mcp_binding is not None:
            metadata.update(
                self._mcp_resolved_binding_runtime_metadata(resolved_mcp_binding)
            )
        if selected_model_edition:
            metadata["model_edition"] = selected_model_edition
        else:
            metadata.pop("model_edition", None)
        if request.capability_id != requested_capability_id and request.capability_id is not None:
            metadata["requested_capability_alias"] = request.capability_id
            metadata["canonical_capability_id"] = requested_capability_id
        if continued_pending_context is not None:
            metadata.update(self._pending_skill_continuation_metadata(continued_pending_context))
        if continued_pending_context is not None:
            metadata["defer_task_completed_until_pending_skill_context_processed"] = True
        if self._skill_runtime_state is not None:
            metadata["skill_bundle_revision"] = self._skill_runtime_state.active_revision
        metadata.update(self._mcp_task_assignment_metadata(task))
        if (
            task.mcp_execution_mode == MCPExecutionPath.LEGACY.value
            and self._mcp_runtime_state is not None
        ):
            metadata["mcp_bundle_revision"] = self._mcp_runtime_state.active_revision
        visible_mcp_servers = await self.available_user_mcp_server_profiles(
            authenticated_username,
            execution_mode=task.mcp_execution_mode,
        )
        available_mcp_servers = (
            tuple(
                profile
                for profile in visible_mcp_servers
                if profile.server_id == resolved_mcp_binding.server_id
            )
            if resolved_mcp_binding is not None
            else visible_mcp_servers
        )
        if resolved_mcp_binding is not None and len(available_mcp_servers) != 1:
            raise MCPBoundServerUnavailableError("mcp_bound_server_unavailable")
        model_options = self._resolve_llm_request_options(metadata)
        model_options_payload = {
            "model_edition": model_options.model_edition,
            "reasoning_effort": model_options.reasoning_effort,
            "thinking_enabled": model_options.thinking,
        }
        bundle_revisions = {
            "skill_bundle_revision": metadata.get("skill_bundle_revision"),
            "mcp_bundle_revision": metadata.get("mcp_bundle_revision"),
        }
        alias = (
            request.capability_id
            if request.capability_id is not None
            and request.capability_id != requested_capability_id
            else None
        )
        execution_metadata = {
            "requested_capability_alias": alias,
            "canonical_capability_id": requested_capability_id if alias else None,
            "mcp_dispatch_server_id": (
                resolved_mcp_binding.server_id if resolved_mcp_binding else None
            ),
            "mcp_binding_mode": (
                resolved_mcp_binding.binding_mode if resolved_mcp_binding else None
            ),
            "mcp_command": (
                resolved_mcp_binding.command if resolved_mcp_binding else None
            ),
            "mcp_execution_mode": mcp_assignment.real_path.value,
            "mcp_rollout_config_version": mcp_assignment.config_version,
            "mcp_route_reason_code": mcp_assignment.reason_code.value,
            "mcp_rollout_mode": mcp_assignment.routing_mode.value,
            "defer_task_completed_until_pending_skill_context_processed": (
                True
                if continued_pending_context is not None
                else None
            ),
            "forced_by_mcp_command": (
                True if resolved_mcp_binding is not None else None
            ),
            "mcp_shadow_enabled": mcp_assignment.shadow_enabled,
        }
        binding_payload = (
            {
                "server_id": resolved_mcp_binding.server_id,
                "server_config_version": resolved_mcp_binding.server_config_version,
                "server_security_version": resolved_mcp_binding.server_security_version,
                "display_name": resolved_mcp_binding.display_name,
                "command": resolved_mcp_binding.command,
                "binding_mode": resolved_mcp_binding.binding_mode,
            }
            if resolved_mcp_binding is not None
            else None
        )
        assignment_payload = {
            "execution_mode": mcp_assignment.real_path.value,
            "shadow_enabled": mcp_assignment.shadow_enabled,
            "rollout_config_version": mcp_assignment.config_version,
            "route_reason_code": mcp_assignment.reason_code.value,
            "rollout_mode": mcp_assignment.routing_mode.value,
        }
        pending_payload = (
            {
                "context_id": continued_pending_context.context_id,
                "capability_id": continued_pending_context.capability_id,
                "original_user_message": continued_pending_context.original_user_message,
                "assistant_message": continued_pending_context.assistant_message,
                "missing_requirements": sorted(
                    set(continued_pending_context.missing_requirements)
                ),
            }
            if continued_pending_context is not None
            else None
        )
        frozen_file_profile = self._file_requirement_profile_for_request(
            request,
            metadata=request.metadata,
            requested_capability_id=requested_capability_id,
            continued_pending_context=continued_pending_context,
        )
        selector_metadata: dict[str, Any] = {}
        if frozen_file_profile.is_meaningful():
            selector_metadata["file_selection"] = {
                "source": frozen_file_profile.source,
                "required": frozen_file_profile.required,
                "allow_multiple": frozen_file_profile.allow_multiple,
                "expected_content": list(frozen_file_profile.expected_content),
                "supported_file_types": list(
                    frozen_file_profile.supported_file_types
                ),
                "helpful_columns": list(frozen_file_profile.helpful_columns),
                "disambiguation_hint": frozen_file_profile.disambiguation_hint,
                "user_file_reference": frozen_file_profile.user_file_reference,
                "context_notes": list(frozen_file_profile.context_notes),
            }
        message_metadata: dict[str, Any] = {
            _SUBMISSION_INPUT_METADATA_KEY: {
                "explicit_upload_ids": list(explicit_upload_ids),
                "selector_metadata": selector_metadata,
            }
        }
        if resolved_mcp_binding is not None:
            message_metadata.update(
                {
                    MCP_SERVER_BINDING_CONTEXT_METADATA_KEY: resolved_mcp_binding.private_context(),
                    MCP_SERVER_BADGE_METADATA_KEY: resolved_mcp_binding.public_badge(),
                }
            )
        admission_request = build_submission_admission_request(
            username=authenticated_username,
            conversation_id=conversation_id,
            message_id=message_id,
            content=request.content,
            task=task,
            conversation_created_at=(
                existing_conversation.created_at
                if existing_conversation is not None
                and existing_conversation.created_at is not None
                else now
            ),
            conversation_updated_at=now,
            create_conversation_if_missing=existing_conversation is None,
            message_created_at=now,
            message_type="chat",
            message_metadata=message_metadata,
            model_options=model_options_payload,
            bundle_revisions=bundle_revisions,
            execution_metadata=execution_metadata,
            explicit_upload_ids=explicit_upload_ids,
            request_sheet_selections=request_sheet_selections,
            upload_refs=selected_upload_refs,
            mcp_binding=binding_payload,
            mcp_assignment=assignment_payload,
            available_mcp_servers=available_mcp_servers,
            pending_context=pending_payload,
            initial_no_server_eligible=(
                explicit_mcp_dispatch and resolved_mcp_binding is None
            ),
            claim_owner=self._submission_claim_owner,
            claim_expires_at=now + self._submission_claim_ttl,
            skill_activation=skill_activation,
        )
        try:
            admitted = await self.storage.admit_submission(admission_request)
        except Exception as exc:
            raise SubmissionAdmissionUnavailableError() from exc
        if admitted.disposition is SubmissionAdmissionDisposition.CONVERSATION_BUSY:
            raise ConversationBusyError(
                f"Conversation {conversation_id} already has an active task."
            )
        if admitted.disposition is SubmissionAdmissionDisposition.MESSAGE_ID_CONFLICT:
            raise MessageIdentityConflictError()
        if admitted.disposition is SubmissionAdmissionDisposition.CONVERSATION_NOT_AVAILABLE:
            raise PermissionError(f"Conversation is not available: {conversation_id}")
        if admitted.disposition not in {
            SubmissionAdmissionDisposition.CREATED,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        }:
            raise SubmissionAdmissionUnavailableError()
        record = admitted.record
        if admitted.message_id is None or admitted.task_id is None:
            raise SubmissionAdmissionUnavailableError()
        if record is None:
            if (
                admitted.disposition
                is not SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY
                or admitted.handle is not None
            ):
                raise SubmissionAdmissionUnavailableError()
            message = await self.storage.get_message(admitted.message_id)
            canonical_task = await self.storage.get_task(admitted.task_id)
            if (
                message is None
                or canonical_task is None
                or message.conversation_id != admitted.conversation_id
                or message.task_id != admitted.task_id
                or canonical_task.conversation_id != admitted.conversation_id
                or canonical_task.root_message_id != admitted.message_id
            ):
                raise SubmissionAdmissionUnavailableError()
            return message, canonical_task
        if (
            admitted.message_id != record.message_id
            or admitted.task_id != record.task_id
            or admitted.conversation_id != record.conversation_id
            or admitted.phase != record.phase
            or admitted.disposition is SubmissionAdmissionDisposition.CREATED
            and admitted.handle is None
        ):
            raise SubmissionAdmissionUnavailableError()
        coordinator = self._submission_admission_coordinator
        if coordinator is None:
            raise SubmissionAdmissionUnavailableError()

        async def reclaim_exact(
            expected: SubmissionRecoveryRecord,
        ) -> Any:
            reclaim_at = self._utcnow_naive()
            reclaimed = await self.storage.admit_submission(
                replace(
                    admission_request,
                    message_created_at=reclaim_at,
                    claim_expires_at=reclaim_at + self._submission_claim_ttl,
                )
            )
            if reclaimed.message_id != expected.message_id:
                raise RuntimeError("submission_exact_reclaim_identity_mismatch")
            return reclaimed

        try:
            recovery = await coordinator.continue_admitted(
                admitted,
                reclaim_exact=reclaim_exact,
            )
        except Exception as exc:
            raise SubmissionAdmissionUnavailableError() from exc
        if recovery.recovered_count:
            title_metadata = self._drop_user_supplied_system_metadata(
                request.metadata
            )
            if selected_model_edition:
                title_metadata["model_edition"] = selected_model_edition
            await self._maybe_schedule_conversation_title_generation(
                conversation_id,
                metadata=title_metadata,
            )

        message = await self.storage.get_message(record.message_id)
        if message is None:
            projection = json.loads(record.message_projection.decode("utf-8"))
            projected_metadata = dict(projection["metadata"])
            projected_metadata.pop(_SUBMISSION_INPUT_METADATA_KEY, None)
            message_created_at = datetime.fromisoformat(
                str(projection["message_created_at"]).replace("Z", "+00:00")
            ).replace(tzinfo=None)
            message = Message(
                message_id=record.message_id,
                conversation_id=record.conversation_id,
                role=MessageRole.USER,
                content=str(projection["content"]),
                task_id=record.task_id,
                stream_status=str(projection["stream_status"]),
                created_at=message_created_at,
                message_type=str(projection["message_type"]),
                metadata=projected_metadata,
                updated_at=datetime.fromisoformat(
                    str(projection["updated_at"]).replace("Z", "+00:00")
                ).replace(tzinfo=None),
            )
        canonical_task = await self.storage.get_task(record.task_id)
        if canonical_task is None:
            canonical_task = replace(
                task,
                task_id=record.task_id,
                root_message_id=record.message_id,
                created_at=admitted.task_created_at or record.created_at,
                updated_at=admitted.task_created_at or record.created_at,
            )
        return message, canonical_task

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

    async def _mcp_invocation_shadow_hook(self, *, phase: str, **values: Any):
        if phase == "finish":
            handle = values.get("handle")
            if isinstance(handle, _MCPShadowInvocationHandle):
                await self._finish_mcp_invocation_shadow(
                    handle,
                    values.get("result"),
                )
            return None
        task = values.get("task")
        node = values.get("node")
        capability_id = str(values.get("capability_id") or "")
        metadata = values.get("metadata")
        if (
            not isinstance(task, Task)
            or not isinstance(node, TaskNode)
            or not isinstance(metadata, Mapping)
            or task.mcp_shadow_enabled is not True
            or task.mcp_execution_mode != MCPExecutionPath.LEGACY.value
            or task.mcp_rollout_mode != MCPRoutingMode.SHADOW.value
        ):
            return None
        try:
            if (
                self.mcp_shadow_observer is None
                or self._mcp_shadow_manifest is None
                or self._mcp_runtime_state is None
                or self.user_mcp_config_service is None
            ):
                raise RuntimeError("shadow_runtime_unavailable")
            revision = str(metadata.get("mcp_bundle_revision") or "").strip()
            if not revision:
                raise RuntimeError("shadow_pinned_revision_missing")
            bundle = self._mcp_runtime_state.bundle_for_revision(revision)
            binding = bundle.bindings.get(capability_id)
            if binding is None:
                raise RuntimeError("shadow_invocation_binding_missing")
            scenario = self._mcp_shadow_scenario_bindings.get(capability_id)
            if not isinstance(scenario, ShadowScenario):
                raise RuntimeError("shadow_scenario_binding_missing")
            conversation = await self.storage.get_conversation(task.conversation_id)
            if conversation is None or not conversation.username:
                raise RuntimeError("shadow_conversation_owner_missing")
            user_servers = tuple(
                await self.user_mcp_config_service.list_servers(
                    conversation.username
                )
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
            target_digests = await self._mcp_shadow_credential_digests(
                user_servers
            )
            legacy_servers = {
                server.server_id: server
                for server in self._mcp_runtime_state.config.servers
            }
            config_fingerprint = (
                self._mcp_shadow_manifest.manifest.config_fingerprint
            )
            resolutions = {
                server.server_id: resolve_approved_migration_mapping(
                    legacy_server_id=server.server_id,
                    owner_user_id=conversation.username,
                    legacy_server=server,
                    user_servers=user_servers,
                    target_credential_digests=target_digests,
                    config_fingerprint=config_fingerprint,
                )
                for server in self._mcp_runtime_state.config.servers
            }
            approved_mappings = tuple(
                resolution.mapping
                for resolution in resolutions.values()
                if resolution.mapping is not None
            )
            if (
                approved_shadow_mapping_set_fingerprint(approved_mappings)
                != self._mcp_shadow_manifest.manifest.mapping_fingerprint
            ):
                raise RuntimeError("shadow_mapping_set_fingerprint_mismatch")
            resolution = resolutions.get(
                binding.server_id,
                RuntimeShadowMappingResolution(
                    None,
                    ("legacy_source_config_missing",),
                ),
            )
            legacy_server = legacy_servers.get(binding.server_id)
            observer_task = asyncio.create_task(
                self.mcp_shadow_observer.compare_task(
                    owner_user_id=conversation.username,
                    task_id=task.task_id,
                    user_request=str(
                        self._agent_invocation_contexts.current_user_input(
                            values["run"]
                        )
                        or task.summary
                        or ""
                    ),
                    profiles=profiles,
                    legacy_binding=binding,
                    legacy_server_bindings=tuple(
                        candidate
                        for candidate in bundle.bindings.values()
                        if candidate.server_id == binding.server_id
                    ),
                    legacy_transport=(
                        legacy_server.transport
                        if legacy_server is not None
                        else "not_applicable"
                    ),
                    legacy_endpoint_url=(
                        legacy_server.endpoint
                        if legacy_server is not None
                        else None
                    ),
                    mapping=resolution.mapping,
                    config_fingerprint=config_fingerprint,
                    mapping_blockers=resolution.blockers,
                ),
                name=f"mcp-shadow-invocation:{task.task_id}:{node.node_id}",
            )
            self._running_mcp_shadow_tasks.add(observer_task)
            observer_task.add_done_callback(self._running_mcp_shadow_tasks.discard)
            return _MCPShadowInvocationHandle(
                task=task,
                node_id=node.node_id,
                owner_user_id=conversation.username,
                user_request=str(task.summary or ""),
                scenario=scenario,
                binding=binding,
                approved_mappings=approved_mappings,
                observer_task=observer_task,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_mcp_shadow_metric_gap(
                reason_code=str(exc)
                if re.fullmatch(r"[a-z0-9_.-]+", str(exc))
                else "shadow_observation_setup_failed"
            )
            return None

    async def _finish_mcp_invocation_shadow(
        self,
        handle: _MCPShadowInvocationHandle,
        invocation_result: Any,
    ) -> None:
        try:
            shadow_result = await asyncio.wait_for(
                handle.observer_task,
                timeout=self._mcp_shadow_terminal_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_observer_failed"
            )
            return
        comparison = shadow_result.comparison
        blockers = tuple(shadow_result.blockers)
        observation = shadow_result.observation
        await self._record_mcp_shadow_comparison_event(
            handle,
            comparison=comparison,
            blockers=blockers,
            result_category=(
                observation.outcome.value
                if observation is not None
                else comparison.value
            ),
        )
        manifest = self._mcp_shadow_manifest
        if (
            manifest is None
            or self.user_mcp_audit_service is None
            or observation is None
            or shadow_result.legacy_summary is None
        ):
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_terminal_evidence_incomplete"
            )
            return
        terminal_node = getattr(invocation_result, "node", None)
        expected = manifest.manifest.expectation_for(handle.scenario)
        legacy_outcome, terminal = await self._mcp_shadow_legacy_outcome(
            handle.task.task_id,
            handle.node_id,
            terminal_node,
            excluded_fallback=expected.legacy_outcome,
        )
        nonce = hashlib.sha256(
            f"{manifest.fingerprint}:{handle.task.task_id}:{handle.node_id}".encode()
        ).hexdigest()
        try:
            compared = compare_live_shadow_sample(
                verified_manifest=manifest,
                scenario=handle.scenario,
                nonce=nonce,
                legacy_outcome=legacy_outcome,
                observation=observation,
                legacy_summary=shadow_result.legacy_summary,
                legacy_route=str(getattr(handle.binding, "server_id", "") or ""),
                mapping=shadow_result.mapping,
                approved_mappings=handle.approved_mappings,
                terminal=terminal,
            )
        except Exception:
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_terminal_comparison_failed"
            )
            return
        admission = self._mcp_rollout_instance_admission
        if admission is None or admission.stage != "internal_shadow":
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_audit_scope_unavailable"
            )
            return
        observed_at = datetime.now(timezone.utc)
        sample = seal_shadow_audit_sample(
            MCPShadowAuditSample(
                sample_id="mcp-shadow-"
                + hashlib.sha256(
                    f"{manifest.fingerprint}:{handle.task.task_id}:{handle.node_id}:{nonce}".encode()
                ).hexdigest()[:32],
                environment_id=admission.environment_id,
                deployment_id=admission.deployment_id,
                stage=admission.stage,
                config_fingerprint=manifest.manifest.config_fingerprint,
                manifest_fingerprint=manifest.fingerprint,
                fixture_fingerprint=manifest.manifest.fixture_fingerprint,
                mapping_fingerprint=manifest.manifest.mapping_fingerprint,
                scenario=handle.scenario.value,
                nonce=nonce,
                legacy_outcome=legacy_outcome.value,
                shadow_outcome=observation.outcome.value,
                transport=str(observation.summary.transport or ""),
                endpoint_policy=str(observation.summary.endpoint_policy or ""),
                comparison=compared.result.comparison.value,
                blockers=compared.result.blockers,
                payload_digest="",
                observed_at=observed_at,
                recorded_at=observed_at,
                expires_at=observed_at + MCP_SHADOW_SAMPLE_RETENTION,
            )
        )
        try:
            await self.user_mcp_audit_service.record_shadow_sample(sample)
        except Exception:
            await self._record_mcp_shadow_metric_gap(
                reason_code="shadow_sample_persistence_failed"
            )

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
        events = await self.storage.list_events_for_task_filtered(
            task_id,
            event_types={"mcp.tool_call_completed", "mcp.tool_call_failed"},
            node_id=node_id,
            visibility=EventVisibility.AUDIT_ONLY,
        )
        if len(events) != 1:
            return excluded_fallback, False
        event = events[0]
        if terminal_node.status == NodeStatus.COMPLETED and event.event_type == "mcp.tool_call_completed":
            if event.payload.get("truncated") is True:
                return ShadowOutcome.TOOL_CALL_SUCCEEDED_LARGE_RESULT, True
            return ShadowOutcome.TOOL_CALL_SUCCEEDED, True
        if terminal_node.status != NodeStatus.FAILED:
            return excluded_fallback, False
        return (
            {
                "mcp_auth_required": ShadowOutcome.AUTHENTICATION_FAILED,
                "mcp_timeout": ShadowOutcome.TIMEOUT,
                "mcp_permission_denied": ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
            }.get(str(event.payload.get("error_code") or ""), excluded_fallback),
            str(event.payload.get("error_code") or "")
            in {"mcp_auth_required", "mcp_timeout", "mcp_permission_denied"},
        )

    async def _record_mcp_shadow_comparison_event(
        self,
        handle: _MCPShadowInvocationHandle,
        *,
        comparison: ShadowComparison,
        blockers: tuple[str, ...],
        result_category: str,
    ) -> None:
        if comparison is ShadowComparison.MISMATCHED:
            recorder = self._mcp_rollout_metric_recorder
            if recorder is not None:
                try:
                    await recorder.record_shadow_mismatch()
                except Exception:
                    await self._record_mcp_shadow_metric_gap(
                        reason_code="shadow_mismatch_recording_failed"
                    )
        await self._record_event(
            self._make_event(
                task_id=handle.task.task_id,
                conversation_id=handle.task.conversation_id,
                node_id=handle.node_id,
                event_type="mcp.rollout.shadow_compared",
                payload={
                    "safe_task_ref": hashlib.sha256(
                        f"{self.mcp_rollout_config.fingerprint}:{handle.task.task_id}".encode()
                    ).hexdigest(),
                    "config_version": self.mcp_rollout_config.fingerprint,
                    "rollout_mode": MCPRoutingMode.SHADOW.value,
                    "diff_category": comparison.value,
                    "result_category": result_category,
                    "status": comparison.value,
                    **({"reason_code": blockers[0]} if blockers else {}),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

    async def _mcp_shadow_credential_digests(
        self,
        user_servers: tuple[Any, ...],
    ) -> dict[str, str]:
        if self.mcp_credential_cipher is None:
            return {}
        digests: dict[str, str] = {}
        for server in user_servers:
            provenance = (
                server.auth_metadata.get("migration_provenance")
                if isinstance(server.auth_metadata, Mapping)
                else None
            )
            if not isinstance(provenance, Mapping):
                continue
            credential = await self.storage.get_user_mcp_credential(
                server.owner_user_id,
                server.server_id,
            )
            if credential is None:
                continue
            digest = migration_target_credential_digest(
                self.mcp_credential_cipher,
                self._mcp_audit_reference_signer,
                server=server,
                credential_record=credential,
                source_fingerprint=str(
                    provenance.get("source_fingerprint") or ""
                ).strip(),
            )
            if digest is not None:
                digests[server.server_id] = digest
        return digests

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

    @staticmethod
    def _mcp_server_binding_id(request: SubmitMessageRequest) -> str | None:
        value = request.metadata.get(MCP_SERVER_BINDING_METADATA_KEY)
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != {"server_id"}:
            raise ValueError("metadata.mcp_server_binding must contain exactly server_id")
        return normalize_mcp_server_id(value.get("server_id"))

    async def _resolve_mcp_server_binding_preflight(
        self,
        authenticated_username: str,
        server_id: str,
    ) -> ResolvedMCPServerBinding:
        if not self.user_mcp_routing_enabled or self.user_mcp_config_service is None:
            raise MCPBindingFeatureUnavailableError("mcp_user_scoped_runtime_unavailable")
        server = await self.storage.get_user_mcp_server(
            authenticated_username,
            server_id,
        )
        if (
            server is None
            or not server.enabled
            or server.health_status != UserMCPHealthStatus.AVAILABLE
            or server.deletion_pending
            or server.deleted_at is not None
        ):
            raise MCPBoundServerUnavailableError("mcp_bound_server_unavailable")
        try:
            return build_resolved_mcp_server_binding(server)
        except ValueError as exc:
            raise MCPBoundServerUnavailableError("mcp_bound_server_unavailable") from exc

    @staticmethod
    def _mcp_resolved_binding_runtime_metadata(
        binding: ResolvedMCPServerBinding,
    ) -> dict[str, object]:
        return {
            "mcp_dispatch_server_id": binding.server_id,
            "mcp_binding_mode": MCP_BINDING_MODE_EXPLICIT_COMMAND,
            "forced_by_mcp_command": True,
            "mcp_command": binding.command,
        }

    async def _resolve_persisted_mcp_server_binding(
        self,
        task: Task,
        *,
        node_id: str | None = None,
    ) -> ResolvedMCPServerBinding | None:
        root_message = await self.storage.get_message(task.root_message_id)
        if root_message is None:
            raise MCPPersistedBindingError("mcp_binding_root_message_missing")
        raw_context = root_message.metadata.get(
            MCP_SERVER_BINDING_CONTEXT_METADATA_KEY
        )
        if raw_context is None:
            if MCP_SERVER_BADGE_METADATA_KEY in root_message.metadata:
                raise MCPPersistedBindingError("mcp_server_binding_context_missing")
            return None
        context = parse_persisted_mcp_server_binding_context(raw_context)
        conversation = await self.storage.get_conversation(task.conversation_id)
        if conversation is None or not conversation.username:
            raise MCPPersistedBindingError("mcp_binding_conversation_missing")
        server = await self.storage.get_user_mcp_server(
            conversation.username,
            context.server_id,
        )
        if (
            server is None
            or not server.enabled
            or server.health_status != UserMCPHealthStatus.AVAILABLE
            or server.deletion_pending
            or server.deleted_at is not None
            or server.config_version != context.server_config_version
            or server.security_version != context.server_security_version
        ):
            raise MCPBoundServerUnavailableError("mcp_bound_server_unavailable")
        if node_id is not None:
            intent = await self.storage.get_mcp_no_server_intent(
                mcp_no_server_intent_id(task.task_id, node_id=node_id)
            )
            if intent is not None:
                envelope = dict(intent.resume_envelope_json or {})
                envelope_version = mcp_dispatch_resume_envelope_version(envelope)
                authority_mismatch = (
                    intent.requested_server_id != context.server_id
                    or intent.requested_server_config_version
                    != context.server_config_version
                    or intent.requested_server_security_version
                    != context.server_security_version
                )
                if envelope_version == "v2":
                    validate_mcp_dispatch_resume_envelope_v2(envelope)
                    authority_mismatch = authority_mismatch or (
                        envelope.get("server_id") != context.server_id
                    )
                else:
                    envelope_metadata = envelope.get("metadata")
                    authority_mismatch = authority_mismatch or (
                        not isinstance(envelope_metadata, Mapping)
                        or envelope_metadata.get("mcp_binding_mode")
                        != MCP_BINDING_MODE_EXPLICIT_COMMAND
                    )
                if authority_mismatch:
                    raise MCPPersistedBindingError(
                        "mcp_server_binding_resume_authority_mismatch"
                    )
        resolved = build_resolved_mcp_server_binding(server)
        if (
            resolved.server_id != context.server_id
            or resolved.server_config_version != context.server_config_version
            or resolved.server_security_version != context.server_security_version
        ):
            raise MCPPersistedBindingError("mcp_server_binding_context_mismatch")
        return resolved

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

    async def mcp_terminal_projection_for_task(
        self, task: Task
    ) -> dict[str, object] | None:
        conversation = await self.storage.get_conversation(task.conversation_id)
        if conversation is None:
            return None
        projections = []
        for call in await self.storage.list_mcp_call_records(
            conversation.username, task.task_id
        ):
            projection = await self.storage.get_mcp_execution_terminal_projection(
                call.call_ref
            )
            if projection is not None:
                projections.append(projection)
        if not projections:
            return None
        if len(projections) != 1:
            raise RuntimeError("mcp_terminal_projection_task_fork")
        projection = projections[0]
        return {
            "projection_id": projection.projection_id,
            "status": str(projection.status),
            "revision": projection.revision,
            "no_replay": projection.no_replay,
            "reason_code": str(projection.reason_code),
            "unknown_event_id": projection.unknown_event_id,
            "task_failed_event_id": projection.task_failed_event_id,
            "result_receipt_id": projection.result_receipt_id,
            "result_payload_sha256": projection.result_payload_sha256,
            "terminal_state": (
                str(projection.resolved_terminal_state)
                if projection.resolved_terminal_state is not None
                else None
            ),
            "safe_result_ref": projection.safe_result_ref,
            "safe_error_code": projection.safe_error_code,
            "resolution_event_id": projection.resolution_event_id,
            "correction_event_id": projection.correction_event_id,
            "unknown_terminal_at": projection.unknown_terminal_at,
            "resolved_at": projection.resolved_at,
        }

    async def mcp_result_artifact_projections_for_task(
        self, task_id: str
    ) -> list[dict[str, object]]:
        try:
            events = await self.storage.list_events_for_task_filtered(
                task_id,
                event_types={MCP_RESULT_ARTIFACT_PROJECTION_EVENT},
                visibility=EventVisibility.FRONTEND,
                limit=121,
            )
        except Exception:
            await self._record_mcp_result_artifact_history_failure(
                "event_read_failed"
            )
            return []
        if len(events) >= 121:
            await self._record_mcp_result_artifact_history_failure(
                "event_limit_exceeded"
            )
            return []
        try:
            folded = fold_mcp_result_artifact_projection_payloads(
                event.payload for event in events
            )
        except (TypeError, ValueError):
            await self._record_mcp_result_artifact_history_failure(
                "event_contract_invalid"
            )
            return []
        return [item.as_payload() for item in folded]

    async def _record_mcp_result_artifact_history_failure(
        self, reason_code: str
    ) -> None:
        if self._audit_sink is None:
            return
        try:
            await self._audit_sink.record(
                "mcp.result_artifact_projection_history_failed",
                {
                    "status": "failed",
                    "reason_code": reason_code,
                },
            )
        except Exception:
            logger.error("mcp_result_artifact_projection_observation_failed")

    async def _resume_llm_metadata(
        self,
        task: Task,
        request_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        metadata = await self._task_accepted_llm_metadata(task.task_id)
        if request_metadata is not None:
            metadata.update(self._llm_request_metadata(request_metadata, include_defaults=True))
        return metadata

    @staticmethod
    def _skill_file_selection_metadata(contract: Any, *, source: str = "skill_contract") -> dict[str, Any]:
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

    @staticmethod
    def _submission_component_value(canonical_component: bytes) -> Any:
        return json.loads(canonical_component.decode("utf-8"))

    @staticmethod
    def _submission_event_id(task_id: str, event_kind: str) -> str:
        digest = hashlib.sha256(
            canonical_json_bytes({"event_kind": event_kind, "task_id": task_id})
        ).hexdigest()
        return f"submission-event:v1:{task_id}:{digest}"

    @staticmethod
    def _submission_pending_context(
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
    ) -> PendingSkillContext | None:
        pending = continuation.get("pending_context")
        if not isinstance(pending, Mapping):
            return None
        capability_id = str(pending["capability_id"])
        return PendingSkillContext(
            context_id=str(pending["context_id"]),
            conversation_id=record.conversation_id,
            username=record.username,
            capability_id=capability_id,
            skill_name=capability_id.removeprefix("skill."),
            source_task_id=record.task_id,
            source_message_id=record.message_id,
            original_user_message=str(pending["original_user_message"]),
            missing_requirements=tuple(pending["missing_requirements"]),
            assistant_message=str(pending["assistant_message"]),
            created_at=record.created_at,
            updated_at=record.created_at,
        )

    async def _append_submission_event_exact(self, event: EventRecord) -> bool:
        saved, duplicate = await self.storage.append_event_exact(event)
        if not duplicate:
            await self.event_broker.publish(saved)
        return not duplicate

    @staticmethod
    def _submission_task(
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
    ) -> Task:
        assignment = continuation.get("mcp_assignment")
        assignment = assignment if isinstance(assignment, Mapping) else {}
        content = json.loads(record.message_projection.decode("utf-8"))["content"]
        return Task(
            task_id=record.task_id,
            conversation_id=record.conversation_id,
            root_message_id=record.message_id,
            status=TaskStatus.ACCEPTED,
            routing_mode=RoutingMode(str(continuation["routing_mode"])),
            requested_capability_id=continuation.get("requested_capability_id"),
            summary=str(content),
            created_at=record.created_at,
            updated_at=record.created_at,
            mcp_execution_mode=assignment.get("execution_mode"),
            mcp_shadow_enabled=assignment.get("shadow_enabled"),
            mcp_rollout_config_version=assignment.get("rollout_config_version"),
            mcp_route_reason_code=assignment.get("route_reason_code"),
            mcp_rollout_mode=assignment.get("rollout_mode"),
        )

    async def settle_route_decision_exact(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
        written_at: datetime,
    ) -> SubmissionPreparationReceipt:
        return await self.storage.settle_submission_route_decision_exact(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            requires_user_scoped_server=bool(
                continuation["initial_no_server_eligible"]
            ),
            written_at=written_at,
        )

    async def _submission_agent_request(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
        *,
        user_message: str,
        memory_context: Mapping[str, Any] | None = None,
        available_mcp_servers: tuple[UserMCPServerProfile, ...] | None = None,
        expected_upload_ids: Iterable[str] | None = None,
    ) -> AgentExecutionRequest:
        metadata = {
            key: value
            for source in (
                continuation["execution_metadata"],
                continuation["model_options"],
                continuation["bundle_revisions"],
            )
            for key, value in source.items()
            if value is not None
        }
        model_options = continuation["model_options"]
        thinking_enabled = bool(model_options.get("thinking_enabled", False))
        reasoning_effort = str(
            model_options.get("reasoning_effort") or "minimal"
        )
        metadata.update(
            {
                "deep_thinking": thinking_enabled,
                "main_agent_thinking_enabled": thinking_enabled,
                "main_agent_reasoning_effort": reasoning_effort,
                "requested_reasoning_effort": reasoning_effort,
            }
        )
        attachment_metadata = await self._prepared_task_input_attachment_metadata(
            record.task_id,
            upload_refs=continuation["upload_refs"],
            expected_upload_ids=expected_upload_ids,
        )
        metadata.update(attachment_metadata)
        if memory_context is not None:
            metadata["conversation_memory"] = dict(memory_context)
        profiles = available_mcp_servers
        if profiles is None:
            profiles = tuple(
                UserMCPServerProfile(**item)
                for item in continuation["available_mcp_servers"]
            )
        metadata["available_mcp_server_ids"] = [
            profile.server_id for profile in profiles
        ]
        activation = continuation.get("skill_activation")
        return AgentExecutionRequest(
            task_id=record.task_id,
            conversation_id=record.conversation_id,
            root_message_id=record.message_id,
            user_message=user_message,
            owner_scope=self._agent_owner_scope(record.username),
            requested_capability_id=continuation.get("requested_capability_id"),
            metadata=metadata,
            current_user_message=(
                memory_context.get("current_user_message")
                if memory_context is not None
                else None
            ),
            resolved_user_message=(
                memory_context.get("resolved_user_message")
                if memory_context is not None
                else None
            ),
            memory_context=memory_context,
            available_mcp_servers=profiles,
            skill_activation_payload_json=(
                str(activation["payload"])
                if isinstance(activation, Mapping)
                else None
            ),
            skill_activation_payload_sha256=(
                str(activation["payload_sha256"])
                if isinstance(activation, Mapping)
                else None
            ),
        )

    async def _prepared_task_input_attachment_metadata(
        self,
        task_id: str,
        *,
        upload_refs: Iterable[Mapping[str, Any]] | None = None,
        expected_upload_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        expected_ids = (
            None
            if expected_upload_ids is None
            else {str(item) for item in expected_upload_ids}
        )
        attachments = await self.storage.list_task_input_attachments_for_task(
            task_id
        )
        if not attachments:
            if expected_ids:
                raise RuntimeError("submission_attachment_selection_drift")
            return {}
        refs_by_id = (
            {
                str(item["upload_id"]): dict(item)
                for item in upload_refs
            }
            if upload_refs is not None
            else None
        )
        uploaded_artifacts: list[dict[str, Any]] = []
        skill_artifacts: list[dict[str, Any]] = []
        upload_ids: list[str] = []
        observed_ids = {
            str(attachment.source_upload_id or "").strip()
            for attachment in attachments
            if str(attachment.source_upload_id or "").strip()
        }
        if expected_ids is not None and observed_ids != expected_ids:
            raise RuntimeError("submission_attachment_selection_drift")
        for attachment in attachments:
            if attachment.task_id != task_id:
                raise RuntimeError("submission_attachment_task_drift")
            upload_id = str(attachment.source_upload_id or "").strip()
            if refs_by_id is not None and upload_id:
                ref = refs_by_id.get(upload_id)
                if (
                    ref is None
                    or attachment.conversation_id != ref["conversation_id"]
                    or attachment.sha256 != ref["sha256"]
                    or attachment.size_bytes != ref["size_bytes"]
                    or attachment.selected_sheet != ref["selected_sheet"]
                ):
                    raise RuntimeError("submission_attachment_upload_ref_drift")
            prompt_artifact = attachment.prompt_artifact
            skill_artifact = attachment.skill_artifact
            if not isinstance(prompt_artifact, Mapping) or not prompt_artifact:
                raise RuntimeError("submission_attachment_prompt_artifact_missing")
            if not isinstance(skill_artifact, Mapping) or not skill_artifact:
                raise RuntimeError("submission_attachment_skill_artifact_missing")
            if upload_id:
                upload_ids.append(upload_id)
            uploaded_artifacts.append(dict(prompt_artifact))
            skill_artifacts.append(dict(skill_artifact))
        return {
            **({"upload_ids": upload_ids} if upload_ids else {}),
            "uploaded_artifacts": uploaded_artifacts,
            "skill_artifacts": skill_artifacts,
        }

    async def compute_memory_context(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
    ) -> object:
        if self._conversation_memory_builder is None:
            return None
        content = str(
            json.loads(record.message_projection.decode("utf-8"))["content"]
        )
        request = await self._submission_agent_request(
            record,
            continuation,
            user_message=content,
        )
        fallback_error: Exception | None = None
        try:
            prepare = getattr(self._conversation_memory_builder, "prepare", None)
            if callable(prepare):
                preparation = await prepare(request, username=record.username)
                context = preparation.context
                prepared_summary = preparation.summary_write
            else:
                context = await self._conversation_memory_builder.build(
                    request,
                    username=record.username,
                )
                prepared_summary = None
        except PermissionError:
            raise
        except Exception as exc:
            fallback_error = exc
            context = ConversationMemoryContext(
                conversation_id=record.conversation_id,
                root_message_id=record.message_id,
                source_message_count=0,
                current_user_message=content,
            )
            prepared_summary = None
        prompt_payload = {
            key: value
            for key, value in context.to_prompt_payload().items()
            if value is not None
        }
        summary_write: dict[str, Any] | None = None
        memory_identity_sha256 = hashlib.sha256(b"null").hexdigest()
        if prepared_summary is not None:
            summary = replace(
                prepared_summary,
                created_at=record.created_at,
                updated_at=record.created_at,
            )
            summary_subject = {
                "schema": "maf.submission.memory_summary_write.v1",
                "summary_id": summary.summary_id,
                "conversation_id": summary.conversation_id,
                "username": summary.username,
                "covered_until_turn_id": summary.covered_until_turn_id,
                "covered_until_message_id": summary.covered_until_message_id,
                "covered_until_created_at": (
                    summary.covered_until_created_at.isoformat()
                    if summary.covered_until_created_at is not None
                    else None
                ),
                "summary_text": summary.summary_text,
                "source_message_count": summary.source_message_count,
                "source_message_ids_hash": summary.source_message_ids_hash,
                "estimated_tokens": summary.estimated_tokens,
                "summary_version": summary.summary_version,
                "compression_policy_version": summary.compression_policy_version,
                "model_metadata_safe": dict(summary.model_metadata_safe),
                "created_at": record.created_at.isoformat(),
                "updated_at": record.created_at.isoformat(),
            }
            memory_identity_sha256 = hashlib.sha256(
                b"maf.submission.memory_summary_write.v1\0"
                + canonical_json_bytes(summary_subject)
            ).hexdigest()
            summary_write = {
                **summary_subject,
                "summary_sha256": memory_identity_sha256,
            }
        event_type = (
            "conversation.memory_fallback"
            if fallback_error is not None
            else "conversation.memory_built"
        )
        event_payload = (
            {
                "fallback_reason": "memory_builder_failed",
                "error_type": type(fallback_error).__name__,
            }
            if fallback_error is not None
            else context.to_audit_payload()
        )
        event_business = {
            "schema": "maf.submission.memory_event_write.v1",
            "memory_identity_sha256": memory_identity_sha256,
            "conversation_id": record.conversation_id,
            "task_id": record.task_id,
            "node_id": None,
            "agent_id": None,
            "event_type": event_type,
            "payload": event_payload,
            "visibility": str(EventVisibility.AUDIT_ONLY),
            "created_at": record.created_at.isoformat(),
        }
        event_subject_sha256 = hashlib.sha256(
            b"maf.submission.memory_event.subject.v1\0"
            + canonical_json_bytes(event_business)
        ).hexdigest()
        event_subject = {
            **event_business,
            "event_id": submission_memory_event_id(
                record.task_id,
                event_type,
                event_subject_sha256,
            ),
            "event_subject_sha256": event_subject_sha256,
        }
        event_write = {
            **event_subject,
            "event_sha256": hashlib.sha256(
                b"maf.submission.memory_event_write.v1\0"
                + canonical_json_bytes(event_subject)
            ).hexdigest(),
        }
        return {
            "schema": "maf.submission.memory_preparation.v1",
            "prompt_payload": prompt_payload,
            "summary_write": summary_write,
            "event_write": event_write,
        }

    def _submission_selector_inputs(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...], PendingSkillContext | None]:
        upload_refs = [dict(item) for item in continuation["upload_refs"]]
        refs_by_id = {str(item["upload_id"]): item for item in upload_refs}
        projection = json.loads(record.message_projection.decode("utf-8"))
        projection_metadata = dict(projection.get("metadata") or {})
        private_input = projection_metadata.get(_SUBMISSION_INPUT_METADATA_KEY)
        if not isinstance(private_input, Mapping) or set(private_input) != {
            "explicit_upload_ids",
            "selector_metadata",
        }:
            raise RuntimeError("submission_selector_private_input_invalid")
        selector_metadata = private_input["selector_metadata"]
        if (
            not isinstance(selector_metadata, Mapping)
            or not set(selector_metadata)
            <= {"file_requirement_profile", "file_selection"}
            or any(not isinstance(value, Mapping) for value in selector_metadata.values())
        ):
            raise RuntimeError("submission_selector_private_input_invalid")
        normalized_explicit_upload_ids = self._normalize_upload_ids(
            private_input["explicit_upload_ids"]
        )
        if list(normalized_explicit_upload_ids) != sorted(
            set(normalized_explicit_upload_ids)
        ):
            raise RuntimeError("submission_selector_private_input_invalid")
        explicit_upload_ids = tuple(
            upload_id
            for upload_id in normalized_explicit_upload_ids
            if upload_id in refs_by_id
        )
        if len(explicit_upload_ids) != len(normalized_explicit_upload_ids):
            raise RuntimeError("submission_selector_upload_ref_missing")
        pending = continuation.get("pending_context")
        continued_pending_context = None
        if isinstance(pending, Mapping):
            capability_id = str(pending["capability_id"])
            continued_pending_context = PendingSkillContext(
                context_id=str(pending["context_id"]),
                conversation_id=record.conversation_id,
                username=record.username,
                capability_id=capability_id,
                skill_name=capability_id.removeprefix("skill."),
                source_task_id=record.task_id,
                source_message_id=record.message_id,
                original_user_message=str(pending["original_user_message"]),
                missing_requirements=tuple(pending["missing_requirements"]),
                assistant_message=str(pending["assistant_message"]),
                created_at=record.created_at,
                updated_at=record.created_at,
            )
        return dict(selector_metadata), explicit_upload_ids, continued_pending_context

    async def _submission_pending_sheet_selections(
        self,
        record: SubmissionRecoveryRecord,
        refs_by_id: Mapping[str, Mapping[str, Any]],
        upload_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for upload_id in upload_ids:
            ref = refs_by_id[upload_id]
            resource = await self.storage.get_conversation_file_resource(
                record.conversation_id,
                record.username,
                upload_id,
            )
            if (
                resource is None
                or resource.status == "deleted"
                or resource.conversation_id != ref["conversation_id"]
                or resource.sha256 != ref["sha256"]
                or resource.size_bytes != ref["size_bytes"]
            ):
                raise RuntimeError("submission_selector_upload_ref_drift")
            if resource.requires_sheet_selection and ref["selected_sheet"] is None:
                upload_record = self._upload_record_from_resource(
                    resource,
                    content_bytes=self._read_conversation_file_resource_bytes_exact(
                        resource
                    ),
                )
                pending.append(upload_record.sheet_selection_payload())
        return pending

    async def _prepare_submission_selector(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        upload_refs = [dict(item) for item in continuation["upload_refs"]]
        refs_by_id = {str(item["upload_id"]): item for item in upload_refs}
        projection = json.loads(record.message_projection.decode("utf-8"))
        request_metadata, explicit_upload_ids, continued_pending_context = (
            self._submission_selector_inputs(record, continuation)
        )
        task = self._submission_task(record, continuation)
        request = SubmitMessageRequest.model_construct(
            conversation_id=record.conversation_id,
            content=str(projection["content"]),
            routing_mode=str(continuation["routing_mode"]),
            capability_id=continuation.get("requested_capability_id"),
            client_message_id=record.message_id,
            model_edition=continuation["model_options"]["model_edition"],
            metadata=request_metadata,
        )
        computed = await self._compute_conversation_file_selection(
            task=task,
            username=record.username,
            request=request,
            metadata=request_metadata,
            requested_capability_id=continuation.get("requested_capability_id"),
            continued_pending_context=continued_pending_context,
            explicit_upload_ids=explicit_upload_ids,
        )
        if computed is not None:
            self._submission_file_selection_computations[record.task_id] = computed
        selected = None if computed is None else computed.decision
        if explicit_upload_ids and (computed is None or not computed.triggered):
            decision = "select"
            reason_code = "frozen_upload_refs"
            upload_ids = list(explicit_upload_ids)
            interrupt_kind = None
        elif computed is None or not computed.triggered or computed.mode == "shadow":
            decision = "continue"
            reason_code = (
                "selector_shadow_observed"
                if computed is not None and computed.mode == "shadow"
                else "selector_not_triggered"
            )
            upload_ids = []
            interrupt_kind = None
        elif selected is None:
            raise RuntimeError("submission_selector_computation_incomplete")
        elif selected.decision in {"select_one", "select_many"}:
            upload_ids = list(selected.upload_ids)
            if any(upload_id not in refs_by_id for upload_id in upload_ids):
                raise RuntimeError("submission_selector_upload_ref_missing")
            decision = selected.decision
            reason_code = selected.reason_code or selected.decision
            interrupt_kind = None
        elif selected.decision == "no_file_needed":
            decision = "continue"
            reason_code = selected.reason_code or selected.decision
            upload_ids = []
            interrupt_kind = None
        else:
            decision = "interrupt"
            reason_code = selected.reason_code or selected.decision
            upload_ids = [
                upload_id
                for upload_id in selected.upload_ids
                if upload_id in refs_by_id
            ]
            interrupt_kind = "file_selection"
        upload_ids = sorted(set(upload_ids))
        sheet_candidate_ids = (
            upload_ids
            if upload_ids or interrupt_kind == "file_selection"
            else sorted(refs_by_id)
        )
        pending_sheet_selections = await self._submission_pending_sheet_selections(
            record,
            refs_by_id,
            sheet_candidate_ids,
        )
        if pending_sheet_selections:
            decision = "interrupt"
            reason_code = "sheet_selection_required"
            upload_ids = sorted(
                {
                    str(upload_id)
                    for pending in pending_sheet_selections
                    for upload_id in pending["required_upload_ids"]
                }
            )
            interrupt_kind = "sheet_selection"
        winner = {
            "decision": decision,
            "reason_code": reason_code,
            "resume_action": "resume",
            "upload_ids": upload_ids,
            "interrupt_kind": interrupt_kind,
        }
        facts = {
            "schema": "maf.submission.selector_materialization.v1",
            "explicit_upload_ids": list(explicit_upload_ids),
            "upload_refs": upload_refs,
            "pending_sheet_selections": pending_sheet_selections,
            "computation": (
                None
                if computed is None
                else {
                    "mode": computed.mode,
                    "triggered": computed.triggered,
                    "trigger_reason": computed.trigger_reason,
                    "profile": {
                        "source": computed.profile.source,
                        "required": computed.profile.required,
                        "allow_multiple": computed.profile.allow_multiple,
                        "expected_content": list(computed.profile.expected_content),
                        "supported_file_types": list(
                            computed.profile.supported_file_types
                        ),
                        "helpful_columns": list(computed.profile.helpful_columns),
                        "disambiguation_hint": computed.profile.disambiguation_hint,
                    },
                    "candidates": [
                        candidate.to_prompt_safe_dict()
                        for candidate in computed.candidates
                    ],
                    "decision": (
                        None
                        if computed.decision is None
                        else {
                            "decision": computed.decision.decision,
                            "upload_ids": list(computed.decision.upload_ids),
                            "confidence": computed.decision.confidence,
                            "reason_code": computed.decision.reason_code,
                        }
                    ),
                    "invoked_payload": computed.invoked_payload,
                    "invalid_output_payload": computed.invalid_output_payload,
                    "decision_payload": computed.decision_payload,
                }
            ),
            "winner": winner,
        }
        candidate_digest = hashlib.sha256(
            b"maf.submission.selector_winner.v1\0" + canonical_json_bytes(facts)
        ).hexdigest()
        self._submission_selector_facts[record.task_id] = facts
        return {**winner, "candidate_digest": candidate_digest}, facts

    async def compute_selector_decision(
        self,
        record: SubmissionRecoveryRecord,
        continuation: Mapping[str, Any],
    ) -> object:
        winner, _facts = await self._prepare_submission_selector(
            record, continuation
        )
        return {
            "decision": winner["decision"],
            "reason_code": winner["reason_code"],
            "candidate_digest": winner["candidate_digest"],
            "resume_action": winner["resume_action"],
            "upload_ids": winner["upload_ids"],
            "interrupt_kind": winner["interrupt_kind"],
        }

    async def materialize_route_decision(
        self,
        record: SubmissionRecoveryRecord,
        canonical_component: bytes,
    ) -> None:
        continuation = json.loads(record.continuation.decode("utf-8"))
        task = self._submission_task(record, continuation)
        existing = await self.storage.get_task(record.task_id)
        if existing is None:
            raise RuntimeError("submission_task_materialization_missing")
        if (
            existing.task_id != task.task_id
            or existing.conversation_id != task.conversation_id
            or existing.root_message_id != task.root_message_id
            or existing.routing_mode != task.routing_mode
            or existing.requested_capability_id != task.requested_capability_id
            or existing.summary != task.summary
            or existing.created_at != task.created_at
            or existing.mcp_execution_mode != task.mcp_execution_mode
            or existing.mcp_shadow_enabled != task.mcp_shadow_enabled
            or existing.mcp_rollout_config_version
            != task.mcp_rollout_config_version
            or existing.mcp_route_reason_code != task.mcp_route_reason_code
            or existing.mcp_rollout_mode != task.mcp_rollout_mode
        ):
            raise RuntimeError("submission_task_materialization_conflict")

        accepted = EventRecord(
            event_id=self._submission_event_id(record.task_id, "task.accepted"),
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            event_type="task.accepted",
            payload={
                "message_id": record.message_id,
                "status": str(TaskStatus.ACCEPTED),
                **(
                    {"model_edition": continuation["model_options"]["model_edition"]}
                    if continuation["model_options"]["model_edition"] is not None
                    else {}
                ),
                "deep_thinking": continuation["model_options"]["thinking_enabled"],
                "main_agent_thinking_enabled": continuation["model_options"]["thinking_enabled"],
                "main_agent_reasoning_effort": continuation["model_options"]["reasoning_effort"],
                "requested_reasoning_effort": continuation["model_options"]["reasoning_effort"],
            },
            visibility=EventVisibility.FRONTEND,
            created_at=record.created_at,
        )
        await self._append_submission_event_exact(accepted)
        assignment = continuation.get("mcp_assignment")
        assignment = assignment if isinstance(assignment, Mapping) else {}
        route = EventRecord(
            event_id=self._submission_event_id(
                record.task_id, "mcp.rollout.route_assigned"
            ),
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            event_type="mcp.rollout.route_assigned",
            payload={
                "safe_owner_ref": self._mcp_audit_reference_signer.safe_owner_reference(
                    record.username,
                    context=str(assignment.get("rollout_config_version") or "submission"),
                ),
                "safe_task_ref": hashlib.sha256(
                    f"{assignment.get('rollout_config_version') or 'submission'}:{record.task_id}".encode()
                ).hexdigest(),
                "real_path": assignment.get("execution_mode"),
                "shadow_enabled": assignment.get("shadow_enabled"),
                "config_version": assignment.get("rollout_config_version"),
                "reason_code": assignment.get("route_reason_code"),
                "rollout_mode": assignment.get("rollout_mode"),
            },
            visibility=EventVisibility.AUDIT_ONLY,
            created_at=record.created_at,
        )
        if await self._append_submission_event_exact(route):
            await self._record_mcp_route_assignment_metric(task)
        binding = continuation.get("mcp_binding")
        if isinstance(binding, Mapping):
            await self._append_submission_event_exact(
                EventRecord(
                    event_id=self._submission_event_id(
                        record.task_id, "mcp.server_binding_resolved"
                    ),
                    conversation_id=record.conversation_id,
                    task_id=record.task_id,
                    event_type="mcp.server_binding_resolved",
                    payload={
                        "safe_server_ref": self._mcp_audit_reference_signer.safe_reference(
                            str(binding["server_id"]),
                            context="mcp-server-binding-v1",
                        ),
                        "binding_mode": binding["binding_mode"],
                        "status": "accepted",
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                    created_at=record.created_at,
                )
            )
        pending_context = self._submission_pending_context(record, continuation)
        transition: tuple[str, str, PendingSkillContext | None] | None = None
        if pending_context is not None:
            transition = ("consumed", "legacy_pending_continued", pending_context)
        elif isinstance(binding, Mapping):
            transition = ("superseded", "new_mcp_binding", None)
        elif continuation["routing_mode"] == str(RoutingMode.HINT):
            transition = ("superseded", "new_skill_hint", None)
        elif continuation["routing_mode"] == str(RoutingMode.FORCE_CAPABILITY):
            transition = ("superseded", "new_forced_capability", None)
        if transition is not None:
            if record.prepared_execution_sha256 is None:
                raise RuntimeError("submission_pending_transition_prepared_missing")
            target_status, reason, prepared_pending_context = transition
            receipt_event, duplicate = (
                await self.storage.materialize_submission_pending_skill_transition_exact(
                    username=record.username,
                    conversation_id=record.conversation_id,
                    task_id=record.task_id,
                    prepared_execution_sha256=record.prepared_execution_sha256,
                    target_status=target_status,
                    reason=reason,
                    pending_context=prepared_pending_context,
                    occurred_at=record.created_at,
                )
            )
            if not duplicate:
                try:
                    await self.event_broker.publish(receipt_event)
                except Exception:
                    logger.warning(
                        "pending skill transition audit publication failed for task %s",
                        record.task_id,
                        exc_info=True,
                    )
        if continuation["routing_mode"] == str(RoutingMode.HINT):
            activation_wrapper = continuation.get("skill_activation")
            if not isinstance(activation_wrapper, Mapping):
                raise RuntimeError("submission_hint_activation_missing")
            activation = json.loads(str(activation_wrapper["payload"]))
            pinned_revision = str(activation["pinned_bundle_revision"])
            safe_revision_ref = hashlib.sha256(
                b"maf.skill.hint_bound.revision.v1\0" + pinned_revision.encode("utf-8")
            ).hexdigest()
            await self._append_submission_event_exact(
                EventRecord(
                    event_id=self._submission_event_id(
                        record.task_id, "skill.hint_bound"
                    ),
                    conversation_id=record.conversation_id,
                    task_id=record.task_id,
                    event_type="skill.hint_bound",
                    payload={
                        "capability_id": continuation["requested_capability_id"],
                        "safe_revision_ref": safe_revision_ref,
                        "profile_digest": activation["profile_digest"],
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                    created_at=record.created_at,
                )
            )

    async def materialize_memory_context(
        self,
        record: SubmissionRecoveryRecord,
        canonical_component: bytes,
    ) -> None:
        value = self._submission_component_value(canonical_component)
        if value is None:
            return
        summary = value["summary_write"]
        if summary is not None:
            materialized = ConversationMemorySummary(
                summary_id=summary["summary_id"],
                conversation_id=summary["conversation_id"],
                username=summary["username"],
                covered_until_turn_id=summary["covered_until_turn_id"],
                covered_until_message_id=summary["covered_until_message_id"],
                covered_until_created_at=(
                    datetime.fromisoformat(summary["covered_until_created_at"])
                    if summary["covered_until_created_at"] is not None
                    else None
                ),
                summary_text=summary["summary_text"],
                source_message_count=summary["source_message_count"],
                source_message_ids_hash=summary["source_message_ids_hash"],
                estimated_tokens=summary["estimated_tokens"],
                summary_version=summary["summary_version"],
                compression_policy_version=summary["compression_policy_version"],
                model_metadata_safe=summary["model_metadata_safe"],
                created_at=datetime.fromisoformat(summary["created_at"]),
                updated_at=datetime.fromisoformat(summary["updated_at"]),
            )
            await self.storage.materialize_conversation_memory_summary_exact(
                materialized
            )
        event = value["event_write"]
        if event is not None:
            await self._append_submission_event_exact(
                EventRecord(
                    event_id=event["event_id"],
                    conversation_id=event["conversation_id"],
                    task_id=event["task_id"],
                    node_id=event["node_id"],
                    agent_id=event["agent_id"],
                    event_type=event["event_type"],
                    payload=event["payload"],
                    visibility=EventVisibility(event["visibility"]),
                    created_at=datetime.fromisoformat(event["created_at"]),
                )
            )

    async def materialize_selector_decision(
        self,
        record: SubmissionRecoveryRecord,
        canonical_component: bytes,
    ) -> None:
        winner = self._submission_component_value(canonical_component)
        if winner is None:
            return
        prepared = json.loads(record.continuation.decode("utf-8"))
        computed = self._submission_file_selection_computations.get(record.task_id)
        refs = {item["upload_id"]: item for item in prepared["upload_refs"]}
        _metadata, frozen_explicit_upload_ids, _pending = (
            self._submission_selector_inputs(record, prepared)
        )
        explicit_upload_ids = set(frozen_explicit_upload_ids)
        selected_upload_ids = [
            upload_id
            for upload_id in winner["upload_ids"]
            if winner["interrupt_kind"] is None or upload_id in explicit_upload_ids
        ]
        for upload_id in selected_upload_ids:
            ref = refs.get(upload_id)
            if ref is None:
                raise RuntimeError("submission_selector_upload_ref_missing")
            resource = await self.storage.get_conversation_file_resource(
                record.conversation_id,
                record.username,
                upload_id,
            )
            if (
                resource is None
                or resource.status == "deleted"
                or resource.conversation_id != ref["conversation_id"]
                or resource.sha256 != ref["sha256"]
                or resource.size_bytes != ref["size_bytes"]
            ):
                raise RuntimeError("submission_selector_upload_ref_drift")
            selected_sheet = ref["selected_sheet"]
            if selected_sheet is not None and resource.selected_sheet != selected_sheet:
                resource = await self._apply_conversation_file_sheet_selection(
                    resource, selected_sheet
                )
            upload_record = self._upload_record_from_resource(
                resource,
                content_bytes=self._read_conversation_file_resource_bytes_exact(
                    resource
                ),
                selected_sheet=selected_sheet,
            )
            task = await self.storage.get_task(record.task_id)
            if task is None:
                raise RuntimeError("submission_selector_task_missing")
            attachment = replace(
                self._attachment_from_upload_record(
                    task=task,
                    record=upload_record,
                    resource=resource,
                    source_kind=(
                        "message_upload"
                        if upload_id in explicit_upload_ids
                        else "file_selector"
                    ),
                    source_message_id=record.message_id,
                    interrupt_answer_id=None,
                    selected_sheet=selected_sheet,
                ),
                updated_at=record.created_at,
            )
            existing = {
                item.attachment_id: item
                for item in await self.storage.list_task_input_attachments_for_task(
                    record.task_id
                )
            }.get(attachment.attachment_id)
            if existing is None:
                await self.storage.save_task_input_attachment(attachment)
            elif existing != attachment:
                raise RuntimeError("submission_selector_attachment_conflict")
        if computed is not None and computed.triggered:
            event_payloads = [
                (
                    "conversation_file.file_selector_invoked",
                    computed.invoked_payload,
                ),
                (
                    "conversation_file.file_selector_invalid_output",
                    computed.invalid_output_payload,
                ),
                (
                    "conversation_file.file_selector_decision_recorded",
                    computed.decision_payload,
                ),
            ]
            for event_type, payload in event_payloads:
                if payload is None:
                    continue
                await self._append_submission_event_exact(
                    EventRecord(
                        event_id=self._submission_event_id(record.task_id, event_type),
                        conversation_id=record.conversation_id,
                        task_id=record.task_id,
                        event_type=event_type,
                        payload=dict(payload),
                        visibility=EventVisibility.AUDIT_ONLY,
                        created_at=record.created_at,
                    )
                )
        if (
            selected_upload_ids
            and winner["interrupt_kind"] is None
            and computed is not None
            and computed.triggered
        ):
            auto_bound_payload: dict[str, Any] = {
                "selected_upload_ids": list(selected_upload_ids),
                "source": "submit_message",
            }
            if len(selected_upload_ids) > 1:
                auto_bound_payload["multi_select_resolution"] = (
                    "multi_select_auto_bound"
                )
            await self._append_submission_event_exact(
                EventRecord(
                    event_id=self._submission_event_id(
                        record.task_id,
                        "conversation_file.file_selector_auto_bound",
                    ),
                    conversation_id=record.conversation_id,
                    task_id=record.task_id,
                    event_type="conversation_file.file_selector_auto_bound",
                    payload=auto_bound_payload,
                    visibility=EventVisibility.AUDIT_ONLY,
                    created_at=record.created_at,
                )
            )

    async def initialize_agent_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
        context: PreparedAgentRecoveryContext,
    ) -> DurableSubmissionHandoff:
        if record.task_id in self._submission_initialized_agent_runs:
            self._submission_selector_facts.pop(record.task_id, None)
            self._submission_file_selection_computations.pop(record.task_id, None)
            return DurableSubmissionHandoff("agent_run", f"agent-run:{record.task_id}")
        metadata_continuation = {
            "execution_metadata": context.execution_metadata,
            "model_options": context.model_options,
            "bundle_revisions": context.bundle_revisions,
            "upload_refs": prepared["upload_refs"],
            "available_mcp_servers": [
                {
                    "server_id": item.server_id,
                    "display_name": item.display_name,
                    "routing_description": item.routing_description,
                    "transport": item.transport,
                }
                for item in context.available_mcp_servers
            ],
            "requested_capability_id": prepared["requested_capability_id"],
            "skill_activation": prepared.get("skill_activation"),
        }
        request = await self._submission_agent_request(
            record,
            metadata_continuation,
            user_message=context.current_user_input,
            memory_context=context.memory_context,
            available_mcp_servers=context.available_mcp_servers,
            expected_upload_ids=context.selected_upload_ids,
        )
        self._restore_prepared_bundle_revisions(
            task_id=record.task_id,
            skill_revision=context.bundle_revisions.get("skill_bundle_revision"),
            mcp_revision=context.bundle_revisions.get("mcp_bundle_revision"),
        )
        initialized = await self.agent_loop_orchestrator.initialize_run(request)
        self._submission_initialized_agent_runs[record.task_id] = initialized
        self._submission_selector_facts.pop(record.task_id, None)
        self._submission_file_selection_computations.pop(record.task_id, None)
        return DurableSubmissionHandoff("agent_run", f"agent-run:{record.task_id}")

    async def materialize_interrupt_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
    ) -> DurableSubmissionHandoff:
        selector_sha256 = prepared["preparation_receipt"]["selector_decision_sha256"]
        identity = submission_interrupt_handoff_id(record.task_id, selector_sha256)
        receipt = await self.storage.get_submission_preparation_receipt(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
        )
        if receipt is None or receipt.selector_decision is None:
            raise RuntimeError("submission_interrupt_selector_missing")
        selector = self._submission_component_value(receipt.selector_decision)
        continuation = json.loads(record.continuation.decode("utf-8"))
        selector_metadata, explicit_upload_ids, _pending = (
            self._submission_selector_inputs(record, continuation)
        )
        refs_by_id = {
            str(item["upload_id"]): dict(item)
            for item in continuation["upload_refs"]
        }
        persisted_profile = next(
            (
                selector_metadata[key]
                for key in ("file_requirement_profile", "file_selection")
                if isinstance(selector_metadata.get(key), Mapping)
            ),
            {},
        )
        now = record.created_at
        interrupt_kind = selector["interrupt_kind"]
        if interrupt_kind == "file_selection":
            candidates = []
            for upload_id, ref in refs_by_id.items():
                resource = await self.storage.get_conversation_file_resource(
                    record.conversation_id,
                    record.username,
                    upload_id,
                )
                if (
                    resource is None
                    or resource.status == "deleted"
                    or resource.conversation_id != ref["conversation_id"]
                    or resource.sha256 != ref["sha256"]
                    or resource.size_bytes != ref["size_bytes"]
                    or resource.selected_sheet != ref["selected_sheet"]
                ):
                    raise RuntimeError("submission_selector_upload_ref_drift")
                candidates.append(candidate_from_resource(resource))
            profile = FileRequirementProfile.from_mapping(
                persisted_profile,
                source="metadata",
            )
            node_id = f"{record.task_id}:file_selection"
            capability_id = (
                prepared["requested_capability_id"] or "agent.file_selection"
            )
            required_fields = {
                "_file_selection": {
                    "presentation": "natural_language",
                    "reason_code": selector["reason_code"],
                    "candidate_upload_ids": [
                        candidate.upload_id for candidate in candidates
                    ],
                    "candidates": [
                        candidate.to_prompt_safe_dict()
                        for candidate in candidates
                    ],
                    "profile": {
                        "source": profile.source,
                        "required": profile.required,
                        "allow_multiple": profile.allow_multiple,
                        "expected_content": list(profile.expected_content),
                        "supported_file_types": list(
                            profile.supported_file_types
                        ),
                        "helpful_columns": list(profile.helpful_columns),
                        "disambiguation_hint": profile.disambiguation_hint,
                    },
                },
                "file_selection_answer": {
                    "type": "string",
                    "description": "请说明要使用哪个文件，或回复不用文件。",
                },
                "replacement_file": {
                    "type": "artifact",
                    "accepts_upload": True,
                    "required": False,
                    "description": "也可以重新上传要使用的文件。",
                },
            }
            question = render_file_selection_question(
                candidates,
                reason_code=str(selector["reason_code"]),
            )
            reason_code = "file_selection_ambiguous"
        elif interrupt_kind == "sheet_selection":
            node_id = f"{record.task_id}:sheet_selection"
            capability_id = (
                prepared["requested_capability_id"] or "agent.sheet_selection"
            )
            required_upload_ids: list[str] = []
            options_by_upload_id: dict[str, list[str]] = {}
            labels_by_upload_id: dict[str, str] = {}
            details_by_upload_id: dict[str, Any] = {}
            pending_sheet_selections = await self._submission_pending_sheet_selections(
                record,
                refs_by_id,
                selector["upload_ids"],
            )
            for pending in pending_sheet_selections:
                required_upload_ids.extend(pending["required_upload_ids"])
                options_by_upload_id.update(pending["options_by_upload_id"])
                labels_by_upload_id.update(pending["labels_by_upload_id"])
                details_by_upload_id.update(pending["details_by_upload_id"])
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
            question = self._sheet_selection_question(
                labels_by_upload_id, options_by_upload_id
            )
            reason_code = "sheet_selection_required"
        else:
            raise RuntimeError("submission_interrupt_kind_invalid")
        expected_interrupt = Interrupt(
            interrupt_id=identity,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            node_id=node_id,
            source_agent=capability_id,
            source_message_id=record.message_id,
            question=question,
            reason_code=reason_code,
            required_fields=required_fields,
            created_at=now,
        )
        memory_component = self._submission_component_value(receipt.memory_context)
        memory_context = (
            dict(memory_component["prompt_payload"])
            if isinstance(memory_component, Mapping)
            else None
        )
        root_content = str(
            json.loads(record.message_projection.decode("utf-8"))["content"]
        )
        resume_metadata = dict(
            (
                await self._submission_agent_request(
                    record,
                    continuation,
                    user_message=root_content,
                    memory_context=memory_context,
                )
            ).metadata
        )
        if interrupt_kind == "file_selection":
            self._task_file_selection_resume_metadata[record.task_id] = (
                resume_metadata
            )
        else:
            self._task_sheet_selection_resume_metadata[record.task_id] = {
                **resume_metadata,
                "_file_selection_pending_upload_ids": list(
                    selector["upload_ids"]
                ),
                "_file_selection_pending_source_kind": (
                    "file_selector"
                    if explicit_upload_ids
                    or str(persisted_profile.get("source") or "")
                    in {"metadata", "skill_contract", "input_schema"}
                    else "interrupt_answer_upload"
                ),
            }
        existing = await self.storage.get_interrupt(identity)
        if existing is None:
            node = await self.storage.get_task_node(node_id)
            if node is None:
                node = await self.storage.save_task_node(
                    TaskNode(
                        node_id=node_id,
                        task_id=record.task_id,
                        capability_id=capability_id,
                        status=NodeStatus.RUNNING,
                        started_at=now,
                    )
                )
            elif (
                node.task_id != record.task_id
                or node.capability_id != capability_id
                or node.started_at != now
                or node.status
                not in {NodeStatus.RUNNING, NodeStatus.WAITING_FOR_INPUT}
            ):
                raise RuntimeError("submission_interrupt_node_conflict")
            task = await self.storage.get_task(record.task_id)
            if task is None:
                raise RuntimeError("submission_interrupt_task_missing")
            if task.status == TaskStatus.ACCEPTED:
                await self.storage.save_task(
                    replace(task, status=TaskStatus.RUNNING, updated_at=now),
                    expected_from_status=TaskStatus.ACCEPTED,
                )
            elif task.status != TaskStatus.RUNNING:
                raise RuntimeError("submission_interrupt_task_conflict")
            if node.status == NodeStatus.RUNNING:
                existing = await self.interrupt_service.open_interrupt(
                    expected_interrupt, now=now
                )
            else:
                existing = await self.storage.save_interrupt(expected_interrupt)
        if existing != expected_interrupt:
            raise RuntimeError("submission_interrupt_materialization_conflict")
        if interrupt_kind == "file_selection":
            await self._append_submission_event_exact(
                EventRecord(
                    event_id=self._submission_event_id(
                        record.task_id,
                        "conversation_file.file_selector_clarification_requested",
                    ),
                    conversation_id=record.conversation_id,
                    task_id=record.task_id,
                    event_type=(
                        "conversation_file.file_selector_clarification_requested"
                    ),
                    payload={
                        "reason_code": selector["reason_code"],
                        "candidate_count": len(candidates),
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                    created_at=now,
                )
            )
        visible_message = await persist_interrupt_question_message(
            self.storage, existing, created_at=now
        )
        if (
            visible_message is None
            or visible_message.message_id
            != interrupt_visible_message_id(expected_interrupt)
            or visible_message.conversation_id != record.conversation_id
            or visible_message.task_id != record.task_id
            or visible_message.role != MessageRole.ASSISTANT
            or visible_message.content != expected_interrupt.question
            or visible_message.stream_status != INTERRUPT_VISIBLE_STREAM_STATUS
            or visible_message.created_at != now
            or visible_message.message_type != "chat"
            or dict(visible_message.metadata) != {}
        ):
            raise RuntimeError("submission_interrupt_visible_message_conflict")
        await self._append_submission_event_exact(
            EventRecord(
                event_id=self._submission_event_id(
                    record.task_id, "node.waiting_for_input"
                ),
                conversation_id=record.conversation_id,
                task_id=record.task_id,
                node_id=node_id,
                event_type="node.waiting_for_input",
                payload={
                    "reason": existing.reason_code,
                    "reason_code": existing.reason_code,
                    "interrupt_id": existing.interrupt_id,
                    **(
                        {"required_upload_ids": required_upload_ids}
                        if interrupt_kind == "sheet_selection"
                        else {}
                    ),
                },
                created_at=now,
            )
        )
        self._submission_selector_facts.pop(record.task_id, None)
        self._submission_file_selection_computations.pop(record.task_id, None)
        return DurableSubmissionHandoff("interrupt", identity)

    async def materialize_no_server_intent_handoff(
        self,
        record: SubmissionRecoveryRecord,
        prepared: Mapping[str, Any],
    ) -> DurableSubmissionHandoff:
        outcome = await self.storage.converge_submission_no_server_handoff_exact(
            username=record.username,
            conversation_id=record.conversation_id,
            task_id=record.task_id,
            occurred_at=record.created_at,
        )
        if outcome not in {
            MCPNoServerConvergenceResult.CONVERGED,
            MCPNoServerConvergenceResult.ALREADY_CONVERGED,
            MCPNoServerConvergenceResult.ALREADY_TERMINAL,
        }:
            raise RuntimeError(f"submission_no_server_convergence_failed:{outcome}")
        return DurableSubmissionHandoff(
            "no_server_intent", mcp_no_server_intent_id(record.task_id)
        )

    async def wakeup_agent(
        self,
        record: SubmissionRecoveryRecord,
        handoff_identity: str,
    ) -> None:
        if handoff_identity != f"agent-run:{record.task_id}":
            raise RuntimeError("submission_agent_handoff_identity_conflict")
        owns_flight = False
        async with self._lock:
            if handoff_identity in self._submission_woken_agent_ids:
                return
            flight = self._submission_wakeup_flights.get(handoff_identity)
            if flight is None:
                initialized = self._submission_initialized_agent_runs.get(
                    record.task_id
                )
                if initialized is None:
                    run = await self.agent_run_repository.get_run_for_task(
                        record.task_id
                    )
                    if (
                        run is None
                        or run.run_id != handoff_identity
                        or run.task_id != record.task_id
                        or run.conversation_id != record.conversation_id
                    ):
                        raise RuntimeError("submission_initialized_agent_missing")
                    self._submission_woken_agent_ids.add(handoff_identity)
                    return
                flight = asyncio.get_running_loop().create_future()
                self._submission_wakeup_flights[handoff_identity] = flight
                owns_flight = True
        if not owns_flight:
            await asyncio.shield(flight)
            return
        try:
            await self._schedule_execution(initialized.request)
            async with self._lock:
                self._submission_woken_agent_ids.add(handoff_identity)
                self._submission_wakeup_flights.pop(handoff_identity, None)
                if (
                    self._submission_initialized_agent_runs.get(record.task_id)
                    is initialized
                ):
                    self._submission_initialized_agent_runs.pop(record.task_id, None)
                flight.set_result(None)
        except BaseException as exc:
            async with self._lock:
                if self._submission_wakeup_flights.get(handoff_identity) is flight:
                    self._submission_wakeup_flights.pop(handoff_identity, None)
                if not flight.done():
                    flight.set_exception(exc)
            if not flight.cancelled():
                flight.exception()
            raise


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
        if conversation_id in self._running_title_conversation_ids:
            return
        expected_user_message_count = len(user_messages)
        self._running_title_conversation_ids.add(conversation_id)
        task = asyncio.create_task(
            self._generate_and_store_conversation_title(
                conversation_id,
                title_source,
                expected_user_message_count=expected_user_message_count,
                metadata=dict(metadata or {}),
            )
        )
        self._running_title_tasks.add(task)

        def release_title_generation(completed: asyncio.Task[None]) -> None:
            self._running_title_tasks.discard(completed)
            self._running_title_conversation_ids.discard(conversation_id)

        task.add_done_callback(release_title_generation)

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
            await self.storage.compare_and_set_conversation(
                replace(conversation, title=title, updated_at=self._utcnow_naive()),
                expected_current_task_id=conversation.current_task_id,
                expected_updated_at=conversation.updated_at,
            )

    async def _schedule_execution(
        self,
        request: AgentExecutionRequest,
        *,
        await_durable_start: bool = False,
    ) -> asyncio.Task[None]:
        initialized = getattr(
            self, "_submission_initialized_agent_runs", {}
        ).get(request.task_id)
        if initialized is not None and initialized.request == request:
            return await self._schedule_initialized_execution(initialized)
        self._retain_task_skill_revision(request)
        self._retain_task_mcp_revision(request)
        async with self._lock:
            existing = self._running_tasks.get(request.task_id)
            if existing is not None and not existing.done():
                handle = existing
                durable_start = self._execution_durable_starts.get(request.task_id)
            else:
                if existing is not None:
                    self._running_tasks.pop(request.task_id, None)
                active_task_count = sum(
                    1 for item in self._running_tasks.values() if not item.done()
                )
                generation = self._execution_generations.get(request.task_id, 0) + 1
                self._execution_generations[request.task_id] = generation
                durable_start = (
                    asyncio.get_running_loop().create_future()
                    if await_durable_start
                    else None
                )
                if durable_start is not None:
                    self._execution_durable_starts[request.task_id] = durable_start
                execution = (
                    self._run_execution(
                        request,
                        active_task_count=active_task_count,
                        execution_generation=generation,
                    )
                    if durable_start is None
                    else self._run_execution(
                        request,
                        active_task_count=active_task_count,
                        execution_generation=generation,
                        durable_start=durable_start,
                    )
                )
                handle = asyncio.create_task(execution)
                self._running_tasks[request.task_id] = handle
        if await_durable_start:
            if durable_start is not None:
                await asyncio.shield(durable_start)
            run = await self.agent_run_repository.get_run_for_task(request.task_id)
            while run is None and not handle.done():
                await asyncio.sleep(0.01)
                run = await self.agent_run_repository.get_run_for_task(request.task_id)
            if run is None:
                raise RuntimeError("agent_run_durable_start_missing")
        return handle

    async def _schedule_initialized_execution(self, initialized: Any) -> asyncio.Task[None]:
        request = initialized.request
        async with self._lock:
            existing = self._running_tasks.get(request.task_id)
            if existing is not None and not existing.done():
                return existing
            if existing is not None:
                self._running_tasks.pop(request.task_id, None)
            active_task_count = sum(
                1 for item in self._running_tasks.values() if not item.done()
            )
            BackpressureGuard(
                max_active_tasks=DEFAULT_MAX_ACTIVE_TASKS
            ).ensure_can_accept(active_task_count=active_task_count)
            generation = self._execution_generations.get(request.task_id, 0) + 1
            self._execution_generations[request.task_id] = generation
            handle = asyncio.create_task(
                self._run_initialized_execution(
                    initialized,
                    execution_generation=generation,
                )
            )
            self._running_tasks[request.task_id] = handle
            return handle

    async def _run_initialized_execution(
        self,
        initialized: Any,
        *,
        execution_generation: int,
    ) -> None:
        request = initialized.request
        try:
            await self._fail_if_effective_uploads_inactive_for_execution(request)
            await self.agent_loop_orchestrator.run_initialized(
                initialized,
                cancellation=self._agent_cancellation_token(request.task_id),
            )
            restored_cancelled_task = await self._restore_cancelled_task_if_requested(
                request.task_id,
                request.conversation_id,
            )
            if restored_cancelled_task is not None:
                return
        except Exception as exc:
            if (
                isinstance(exc, AgentStorageConflict)
                and str(exc) == "agent_task_lease_held"
            ):
                return
            restored_cancelled_task = await self._restore_cancelled_task_if_requested(
                request.task_id,
                request.conversation_id,
            )
            if restored_cancelled_task is None:
                await self._mark_task_failed(request, exc)
        finally:
            try:
                await self._clear_conversation_current_task(
                    request.conversation_id,
                    request.task_id,
                )
                await self._release_task_skill_revision_if_terminal(request.task_id)
                await self._release_task_mcp_revision_if_terminal(request.task_id)
            finally:
                self._locally_cancelled_task_ids.discard(request.task_id)
                async with self._lock:
                    current_handle = self._running_tasks.get(request.task_id)
                    if (
                        current_handle is asyncio.current_task()
                        and self._execution_generations.get(request.task_id)
                        == execution_generation
                    ):
                        self._running_tasks.pop(request.task_id, None)

    async def _run_execution(
        self,
        request: AgentExecutionRequest,
        *,
        active_task_count: int,
        execution_generation: int | None = None,
        durable_start: asyncio.Future[None] | None = None,
    ) -> None:
        try:
            request = await self._scrub_deleted_file_context_for_execution(request)
            request = await self._attach_conversation_memory(request)
            BackpressureGuard(
                max_active_tasks=DEFAULT_MAX_ACTIVE_TASKS
            ).ensure_can_accept(active_task_count=active_task_count)
            if durable_start is None:
                await self.agent_loop_orchestrator.start_or_resume(
                    request,
                    cancellation=self._agent_cancellation_token(request.task_id),
                )
            else:
                initialized = await self.agent_loop_orchestrator.initialize_run(request)
                if not durable_start.done():
                    durable_start.set_result(None)
                await self.agent_loop_orchestrator.run_initialized(
                    initialized,
                    cancellation=self._agent_cancellation_token(request.task_id),
                )
            restored_cancelled_task = await self._restore_cancelled_task_if_requested(
                request.task_id,
                request.conversation_id,
            )
            if restored_cancelled_task is not None:
                return
        except Exception as exc:
            if durable_start is not None and not durable_start.done():
                durable_start.set_exception(exc)
            if (
                isinstance(exc, AgentStorageConflict)
                and str(exc) == "agent_task_lease_held"
            ):
                return
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
                    current_handle = self._running_tasks.get(request.task_id)
                    if (
                        current_handle is asyncio.current_task()
                        and (
                            execution_generation is None
                            or self._execution_generations.get(request.task_id)
                            == execution_generation
                        )
                    ):
                        self._running_tasks.pop(request.task_id, None)
                        self._execution_durable_starts.pop(request.task_id, None)
    async def _scrub_deleted_file_context_for_execution(self, request: AgentExecutionRequest) -> AgentExecutionRequest:
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

    async def _fail_if_effective_uploads_inactive_for_execution(self, request: AgentExecutionRequest) -> None:
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
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return None
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

    async def _attach_conversation_memory(self, request: AgentExecutionRequest) -> AgentExecutionRequest:
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

    async def _mark_task_failed(self, request: AgentExecutionRequest, exc: Exception) -> None:
        task = await self.storage.get_task(request.task_id)
        if task is None or task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED} or task.cancel_requested_at is not None:
            return
        failed_run = await self.agent_loop_orchestrator.fail(
            request.task_id,
            error_code="execution_crash",
        )
        if failed_run is None:
            failed = replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive())
            await self.storage.save_task(failed)
        payload: dict[str, Any] = {
            "code": "execution_crash",
            "message": "Task execution failed safely.",
            "error_type": type(exc).__name__,
        }
        if isinstance(exc, AgentStorageConflict) and re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,127}", str(exc)
        ):
            payload["agent_error_code"] = str(exc)
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
        existing_messages = await self.storage.list_messages_for_conversation(
            conversation_id
        )
        if any(
            message.task_id == task_id
            and str(message.role) == str(MessageRole.ASSISTANT)
            and message.stream_status in {"complete", "completed"}
            for message in existing_messages
        ):
            return
        artifacts = await self.storage.list_artifacts_for_task(task_id)
        events = await self._list_final_answer_events(task_id)
        fallback_metadata = await self._assistant_history_fallback_metadata(task_id)
        text_artifact = select_final_text_artifact(artifacts, events=events)
        if text_artifact is None:
            return
        task = await self.storage.get_task(task_id)
        message_created_at = text_artifact.created_at
        if message_created_at is None and task is not None:
            message_created_at = task.updated_at or task.created_at
        if message_created_at is None:
            raise RuntimeError("assistant_history_message_created_at_missing")
        message = Message(
            message_id=message_id,
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=text_artifact.storage_ref,
            task_id=task_id,
            stream_status="complete",
            created_at=message_created_at,
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
            event_types={"agent.final_output"},
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

    @staticmethod
    def _metadata_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    async def _clear_conversation_current_task(self, conversation_id: str, task_id: str) -> None:
        async with self._lock:
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
            await self.storage.compare_and_set_conversation(
                replace(conversation, current_task_id=None, updated_at=self._utcnow_naive()),
                expected_current_task_id=task_id,
                expected_updated_at=conversation.updated_at,
            )

    async def cancel_task(self, task_id: str) -> Task:
        existing_task = await self.storage.get_task(task_id)
        if existing_task is not None and existing_task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            self._locally_cancelled_task_ids.add(task_id)
            self._agent_cancellation_token(task_id).cancel()
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
        await self.agent_loop_orchestrator.cancel(
            task_id,
            reason_code="user_cancelled",
        )
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
                    latest_interrupt = await self.storage.get_interrupt_for_node(task_id, collection.node_id)
                    if latest_interrupt is not None and latest_interrupt.status == InterruptStatus.OPEN:
                        await self.storage.save_interrupt(
                            replace(
                                latest_interrupt,
                                status=InterruptStatus.ANSWERED,
                                answered_at=self._utcnow_naive(),
                            )
                        )
                    recovery_fields = slot_collection_required_fields_ref(collection)
                    if latest_interrupt is not None:
                        raw_locator = latest_interrupt.required_fields.get("_agent_continuation")
                        if isinstance(raw_locator, Mapping):
                            recovery_fields["_agent_continuation"] = dict(raw_locator)
                    recovery_interrupt = Interrupt(
                        interrupt_id=f"{collection.collection_id}:interrupt:ready_recovery",
                        conversation_id=collection.conversation_id,
                        task_id=collection.task_id,
                        node_id=collection.node_id,
                        source_agent=collection.capability_id,
                        source_message_id="",
                        question=collection.last_question or "",
                        reason_code="ready_v2_slot_recovered",
                        required_fields=recovery_fields,
                        status=InterruptStatus.ANSWERED,
                        created_at=self._utcnow_naive(),
                        answered_at=self._utcnow_naive(),
                    )
                    await self._schedule_v2_slot_resume(
                        task=task,
                        interrupt=recovery_interrupt,
                        collection=collection,
                        raw_answer={},
                    )
                    await self._mark_v2_slot_script_scheduled(collection)
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
            local_runner_id = self._conversation_delete_local_runner_ids.get(
                conversation_id
            )
            if local_runner_id == conversation.delete_runner_id:
                task = self._start_conversation_delete_task(
                    conversation,
                    local_runner_id,
                    name=f"delete-conversation-local-recovery:{conversation_id}",
                )
                try:
                    return await asyncio.shield(task)
                finally:
                    if task.done():
                        self._conversation_delete_tasks.pop(conversation_id, None)
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
        task = self._start_conversation_delete_task(
            marked,
            runner_id,
            name=f"delete-conversation:{conversation_id}",
        )
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
        task = self._start_conversation_delete_task(
            marked,
            runner_id,
            name=f"delete-conversation-retry:{conversation_id}",
        )
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

    def _start_conversation_delete_task(
        self,
        conversation: Conversation,
        runner_id: str,
        *,
        name: str,
    ) -> asyncio.Task[dict[str, object]]:
        self._conversation_delete_local_runner_ids[
            conversation.conversation_id
        ] = runner_id
        task = asyncio.create_task(
            self._run_conversation_delete(conversation, runner_id),
            name=name,
        )
        self._track_conversation_delete_task(conversation.conversation_id, task)
        return task

    async def _run_conversation_delete(self, conversation: Conversation, runner_id: str) -> dict[str, object]:
        conversation_id = conversation.conversation_id
        cancelled_task_ids: list[str] = []
        started_at = conversation.delete_started_at or self._utcnow_naive()
        permanent_conflict = False
        try:
            await self.storage.update_conversation_delete_phase(
                conversation_id,
                phase="closing_admission",
                updated_at=self._utcnow_naive(),
                runner_id=runner_id,
            )
            close_result = await self.storage.close_conversation_admission(
                ConversationAdmissionCloseRequest(
                    username=conversation.username,
                    conversation_id=conversation_id,
                    operation_id=self._conversation_admission_close_operation_id(
                        conversation.username,
                        conversation_id,
                    ),
                    closed_at=self._utcnow_naive(),
                )
            )
            if (
                close_result.conversation_id != conversation_id
                or close_result.disposition
                not in {
                    ConversationAdmissionCloseDisposition.CLOSED,
                    ConversationAdmissionCloseDisposition.EXACT_REPLAY,
                }
            ):
                permanent_conflict = True
                raise RuntimeError("conversation_admission_close_failed")
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
            if (
                self._conversation_delete_local_runner_ids.get(conversation_id)
                == runner_id
            ):
                self._conversation_delete_local_runner_ids.pop(
                    conversation_id, None
                )
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
            if not permanent_conflict:
                raise
            if (
                self._conversation_delete_local_runner_ids.get(conversation_id)
                == runner_id
            ):
                self._conversation_delete_local_runner_ids.pop(
                    conversation_id, None
                )
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
            if is_active_managed_output_file(metadata):
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
            saved = await self.storage.compare_and_set_conversation(
                updated,
                expected_current_task_id=conversation.current_task_id,
                expected_updated_at=conversation.updated_at,
            )
            if saved is None:
                raise ValueError(f"Unknown conversation: {conversation_id}")
            return saved

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

    @staticmethod
    def _interrupt_answer_fingerprint(
        interrupt_id: str,
        answer_payload: Mapping[str, object],
    ) -> str:
        canonical = json.dumps(
            {
                "schema": "maf.interrupt.answer.identity.v1",
                "interrupt_id": interrupt_id,
                "answer_payload": dict(answer_payload),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _interrupt_answer_id(interrupt_id: str, source_message_id: str) -> str:
        digest = hashlib.sha256(
            f"maf.interrupt.answer.v1:{interrupt_id}:{source_message_id}".encode("utf-8")
        ).hexdigest()
        return f"interrupt-answer:{digest}"

    @staticmethod
    def _interrupt_user_message_content(
        interrupt: Interrupt,
        answer_payload: Mapping[str, object],
    ) -> str:
        raw_answer = answer_payload.get("answer")
        if slot_collection_ref_from_required_fields(interrupt.required_fields) is not None and isinstance(raw_answer, Mapping):
            return ApiRuntime._format_v2_answer_message(raw_answer) or ApiRuntime._v2_answer_text(raw_answer)
        if interrupt.reason_code == "file_selection_ambiguous":
            answer_text = str(answer_payload.get("file_selection_answer") or answer_payload.get("answer") or "").strip()
            if isinstance(raw_answer, Mapping):
                answer_text = str(raw_answer.get("text") or answer_text).strip()
            return answer_text or ApiRuntime._format_answer_message(answer_payload)
        return ApiRuntime._format_answer_message(answer_payload)

    async def _reserve_interrupt_user_message(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        answer_payload: Mapping[str, object],
        source_message_id: str | None,
    ) -> _ReservedInterruptMessage:
        message_id = str(
            source_message_id
            or answer_payload.get("client_request_id")
            or self._make_id("msg")
        ).strip()
        if not message_id:
            raise MessageIdentityConflictError()
        conversation = await self.storage.get_conversation(task.conversation_id)
        if conversation is None:
            raise ValueError(f"Unknown conversation: {task.conversation_id}")
        existing_message = await self.storage.get_message(message_id)
        reserved_at = self._utcnow_naive()
        requested_created_at = (
            existing_message.created_at
            if existing_message is not None and existing_message.created_at is not None
            else reserved_at
        )
        reservation_request = MessageIdentityReservationRequest(
            username=conversation.username,
            conversation_id=task.conversation_id,
            message_id=message_id,
            identity_kind=MessageIdentityKind.INTERRUPT,
            role=MessageRole.USER,
            message_type="chat",
            message_created_at=requested_created_at,
            task_id=task.task_id,
            request_fingerprint=self._interrupt_answer_fingerprint(
                interrupt.interrupt_id,
                answer_payload,
            ),
            reserved_at=reserved_at,
        )
        reservation = await self.storage.reserve_message_identity(reservation_request)
        if reservation.disposition == MessageIdentityDisposition.CONFLICT:
            raise MessageIdentityConflictError()
        if reservation.disposition == MessageIdentityDisposition.CONVERSATION_NOT_AVAILABLE:
            raise PermissionError(f"Conversation is not available: {task.conversation_id}")
        if reservation.message_created_at is None:
            raise RuntimeError("interrupt_message_identity_missing_created_at")
        return _ReservedInterruptMessage(
            message=Message(
                message_id=message_id,
                conversation_id=task.conversation_id,
                role=MessageRole.USER,
                content=self._interrupt_user_message_content(interrupt, answer_payload),
                task_id=task.task_id,
                created_at=reservation.message_created_at,
            ),
            request=replace(
                reservation_request,
                message_created_at=reservation.message_created_at,
            ),
        )

    async def _ensure_reserved_interrupt_user_message(
        self,
        reserved: _ReservedInterruptMessage,
    ) -> Message:
        return await self.storage.save_message(
            reserved.message,
            identity_reservation=reserved.request,
        )

    async def _completed_interrupt_replay_response(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        answer_payload: Mapping[str, object],
        reserved_message: _ReservedInterruptMessage,
        allow_open: bool = False,
    ) -> dict[str, object] | None:
        if interrupt.status == InterruptStatus.OPEN and not allow_open:
            return None
        expected_fingerprint = self._interrupt_answer_fingerprint(
            interrupt.interrupt_id,
            answer_payload,
        )
        existing_answers = await self.storage.list_interrupt_answers(interrupt.interrupt_id)
        matching_answer = next(
            (
                answer
                for answer in existing_answers
                if answer.source_message_id == reserved_message.message.message_id
                and self._interrupt_answer_fingerprint(
                    interrupt.interrupt_id,
                    answer.answer_payload,
                )
                == expected_fingerprint
            ),
            None,
        )
        if matching_answer is None:
            if existing_answers:
                raise MessageIdentityConflictError()
            if interrupt.status == InterruptStatus.OPEN:
                return None
            raise MessageIdentityConflictError()
        if interrupt.status == InterruptStatus.OPEN:
            return None
        await self._ensure_reserved_interrupt_user_message(reserved_message)
        if interrupt.reason_code == "mcp_remote_task_input_required":
            action = (
                "mcp_remote_task_cancel_submitted"
                if answer_payload.get("mcp_remote_task_cancel") is True
                else "mcp_remote_task_input_submitted"
            )
            return {
                "task_id": task.task_id,
                "action": action,
                "interrupt_id": interrupt.interrupt_id,
                "source_message_id": reserved_message.message.message_id,
            }
        action: str | None = None
        if interrupt.reason_code == "file_selection_ambiguous":
            action = (
                "sheet_selection_required"
                if await self._has_open_interrupt(task.task_id)
                else "resumed"
            )
        return {
            "interrupt_id": interrupt.interrupt_id,
            "status": str(interrupt.status),
            "node_id": interrupt.node_id,
            "answer_payload": dict(answer_payload),
            "source_message_id": reserved_message.message.message_id,
            **({"action": action} if action is not None else {}),
        }

    async def _interrupt_continuation_receipt(
        self,
        *,
        task_id: str,
        interrupt_id: str,
        source_message_id: str,
        request_fingerprint: str,
    ) -> Mapping[str, object] | None:
        for event in await self.storage.list_events_for_task_filtered(
            task_id,
            event_types=("task.interrupt_continuation_completed",),
        ):
            if (
                event.event_type == "task.interrupt_continuation_completed"
                and str(event.payload.get("interrupt_id") or "") == interrupt_id
                and str(event.payload.get("source_message_id") or "") == source_message_id
            ):
                if str(event.payload.get("request_fingerprint") or "") != request_fingerprint:
                    raise MessageIdentityConflictError()
                response = event.payload.get("response")
                if not isinstance(response, Mapping):
                    raise RuntimeError("interrupt_continuation_receipt_invalid")
                return response
        return None

    async def _record_interrupt_continuation_completed(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        reserved_message: _ReservedInterruptMessage,
        response: Mapping[str, object],
    ) -> None:
        if await self._interrupt_continuation_receipt(
            task_id=task.task_id,
            interrupt_id=interrupt.interrupt_id,
            source_message_id=reserved_message.message.message_id,
            request_fingerprint=reserved_message.request.request_fingerprint or "",
        ) is not None:
            return
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=interrupt.node_id,
                event_type="task.interrupt_continuation_completed",
                payload={
                    "interrupt_id": interrupt.interrupt_id,
                    "source_message_id": reserved_message.message.message_id,
                    "request_fingerprint": reserved_message.request.request_fingerprint or "",
                    "response": dict(response),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

    async def answer_interrupt(
        self,
        task_id: str,
        interrupt_id: str,
        answer_payload: dict[str, object],
        *,
        source_message_id: str | None = None,
        request_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        lock = self._interrupt_answer_locks.get(interrupt_id)
        if lock is None:
            lock = asyncio.Lock()
            self._interrupt_answer_locks[interrupt_id] = lock
        async with lock:
            return await self._answer_interrupt_unlocked(
                task_id,
                interrupt_id,
                answer_payload,
                source_message_id=source_message_id,
                request_metadata=request_metadata,
            )

    async def _answer_interrupt_unlocked(
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
        mcp_approval_action = None
        mcp_approval_decision: str | None = None
        mcp_mrtr_answer = False
        if interrupt.reason_code == "mcp_input_required":
            mrtr_intent = await self.storage.get_mcp_no_server_intent(
                mcp_no_server_intent_id(task.task_id, node_id=interrupt.node_id)
            )
            mrtr_outbox = (
                None
                if mrtr_intent is None
                else await self.storage.get_mcp_dispatch_resume_outbox(
                    mcp_dispatch_resume_outbox_id(mrtr_intent.intent_id)
                )
            )
            mcp_mrtr_answer = (
                mrtr_outbox is not None
                and str(mrtr_outbox.status) == "waiting_input"
            )
        if interrupt.reason_code == "mcp_tool_approval_required":
            mcp_approval_decision = str(
                answer_payload.get("mcp_tool_approval") or ""
            ).strip()
            if mcp_approval_decision not in {
                "allow_once",
                "always_allow",
                "deny",
            }:
                raise ValueError(
                    "mcp_tool_approval must be allow_once, always_allow, or deny"
                )
            mcp_approval_action = (
                await self.storage.get_mcp_pending_tool_action_for_interrupt(
                    interrupt_id
                )
            )
        if interrupt.reason_code == "mcp_remote_task_input_required":
            responses = answer_payload.get("mcp_input_responses")
            cancel_requested = answer_payload.get("mcp_remote_task_cancel") is True
            if not cancel_requested and not isinstance(responses, Mapping):
                raise ValueError(
                    "mcp_input_responses must be an object for remote task input"
                )
            reserved_message = await self._reserve_interrupt_user_message(
                task=task,
                interrupt=interrupt,
                answer_payload=answer_payload,
                source_message_id=source_message_id,
            )
            replay_response = await self._completed_interrupt_replay_response(
                task=task,
                interrupt=interrupt,
                answer_payload=answer_payload,
                reserved_message=reserved_message,
                allow_open=True,
            )
            if replay_response is not None:
                return replay_response
            answer = InterruptAnswer(
                interrupt_answer_id=self._interrupt_answer_id(
                    interrupt_id,
                    reserved_message.message.message_id,
                ),
                interrupt_id=interrupt_id,
                answer_payload=dict(answer_payload),
                source_message_id=reserved_message.message.message_id,
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
            await self._ensure_reserved_interrupt_user_message(reserved_message)
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
                "source_message_id": answer.source_message_id,
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
            return await self._answer_v2_slot_interrupt(
                task=task,
                interrupt=recovered_interrupt,
                answer_payload=answer_payload,
                source_message_id=source_message_id,
                request_metadata=request_metadata,
            )

        reserved_message = await self._reserve_interrupt_user_message(
            task=task,
            interrupt=interrupt,
            answer_payload=answer_payload,
            source_message_id=source_message_id,
        )
        existing_interrupt_answers = await self.storage.list_interrupt_answers(interrupt_id)
        exact_answer = next(
            (
                existing
                for existing in existing_interrupt_answers
                if existing.source_message_id == reserved_message.message.message_id
                and dict(existing.answer_payload) == dict(answer_payload)
            ),
            None,
        )
        if existing_interrupt_answers and exact_answer is None:
            raise MessageIdentityConflictError()
        continuation_receipt = await self._interrupt_continuation_receipt(
            task_id=task.task_id,
            interrupt_id=interrupt_id,
            source_message_id=reserved_message.message.message_id,
            request_fingerprint=reserved_message.request.request_fingerprint or "",
        )
        if exact_answer is not None and continuation_receipt is not None:
            await self._ensure_reserved_interrupt_user_message(reserved_message)
            return dict(continuation_receipt)
        existing_answer_payloads = await self._task_interrupt_answer_payloads(task.task_id)
        answer_payloads = (
            existing_answer_payloads
            if exact_answer is not None
            else (*existing_answer_payloads, dict(answer_payload))
        )
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
            interrupt_answer_id=self._interrupt_answer_id(
                interrupt_id,
                reserved_message.message.message_id,
            ),
            interrupt_id=interrupt_id,
            answer_payload=dict(answer_payload),
            source_message_id=reserved_message.message.message_id,
            accepted=mcp_approval_action is not None or mcp_mrtr_answer,
            created_at=self._utcnow_naive(),
            accepted_at=(
                self._utcnow_naive()
                if mcp_approval_action is not None or mcp_mrtr_answer
                else None
            ),
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
        if (
            root_message is None
            or MCP_SERVER_BINDING_CONTEXT_METADATA_KEY not in root_message.metadata
        ):
            resume_metadata.update(
                await self._conversation_file_context_metadata_for_task(
                    task,
                    upload_sheet_selections=resume_metadata.get("upload_sheet_selections"),
                )
            )
        if exact_answer is not None:
            await self._ensure_reserved_interrupt_user_message(reserved_message)
        mcp_approval_result: str | None = None
        if exact_answer is not None:
            saved_interrupt = await self.interrupt_service.record_answer(exact_answer)
        elif mcp_approval_action is not None:
            assert mcp_approval_decision is not None
            mcp_approval_result = str(
                await self.storage.accept_mcp_tool_approval(
                    interrupt_id,
                    answer,
                    mcp_approval_decision,
                    self._utcnow_naive(),
                )
            )
            if mcp_approval_result in {"conflict", "invalidated"}:
                raise ValueError("MCP tool approval is no longer pending")
            saved_interrupt = await self.storage.get_interrupt(interrupt_id)
            if saved_interrupt is None:
                raise RuntimeError("mcp_approval_interrupt_missing_after_accept")
            await self._ensure_reserved_interrupt_user_message(reserved_message)
            if mcp_approval_result == "accepted":
                resumed_node = await self.storage.get_task_node(interrupt.node_id)
                if resumed_node is None:
                    raise RuntimeError("mcp_approval_node_missing_after_accept")
                await self._record_event(
                    self._make_event(
                        task_id=task.task_id,
                        conversation_id=task.conversation_id,
                        node_id=interrupt.node_id,
                        event_type="node.ready_to_resume",
                        payload={
                            "interrupt_id": interrupt_id,
                            "status": str(resumed_node.status),
                            "capability_id": resumed_node.capability_id,
                        },
                    )
                )
        elif mcp_mrtr_answer:
            mcp_mrtr_result = str(
                await self.storage.accept_mcp_mrtr_answer(
                    interrupt_id,
                    answer,
                    self._utcnow_naive(),
                )
            )
            if mcp_mrtr_result in {"conflict", "invalidated"}:
                raise ValueError("MCP input request is no longer pending")
            saved_interrupt = await self.storage.get_interrupt(interrupt_id)
            if saved_interrupt is None:
                raise RuntimeError("mcp_mrtr_interrupt_missing_after_accept")
            await self._ensure_reserved_interrupt_user_message(reserved_message)
            resumed_node = await self.storage.get_task_node(interrupt.node_id)
            if resumed_node is None:
                raise RuntimeError("mcp_mrtr_node_missing_after_accept")
            await self._record_event(
                self._make_event(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    node_id=interrupt.node_id,
                    event_type="node.ready_to_resume",
                    payload={
                        "interrupt_id": interrupt_id,
                        "status": str(resumed_node.status),
                        "capability_id": resumed_node.capability_id,
                    },
                )
            )
        else:
            saved_interrupt = await self.interrupt_service.record_answer(answer)
            await self._ensure_reserved_interrupt_user_message(reserved_message)

        if exact_answer is None:
            await self._record_event(
                self._make_event(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    node_id=interrupt.node_id,
                    event_type="task.interrupt_answered",
                    payload={"interrupt_id": interrupt_id, "answer_payload": dict(answer_payload)},
                )
            )

        if mcp_approval_result == "denied_finalized" or (
            exact_answer is not None and mcp_approval_decision == "deny"
        ):
            response = {
                "interrupt_id": saved_interrupt.interrupt_id,
                "status": str(saved_interrupt.status),
                "node_id": saved_interrupt.node_id,
                "answer_payload": dict(answer_payload),
                "source_message_id": answer.source_message_id,
            }
            await self._record_interrupt_continuation_completed(
                task=task,
                interrupt=interrupt,
                reserved_message=reserved_message,
                response=response,
            )
            return response

        if await self._resume_agent_interrupt(
            task=task,
            interrupt=interrupt,
            answer_payload=answer_payload,
            resume_metadata=resume_metadata,
        ):
            response = {
                "interrupt_id": saved_interrupt.interrupt_id,
                "status": str(saved_interrupt.status),
                "node_id": saved_interrupt.node_id,
                "answer_payload": dict(answer_payload),
                "source_message_id": answer.source_message_id,
            }
            await self._record_interrupt_continuation_completed(
                task=task,
                interrupt=interrupt,
                reserved_message=reserved_message,
                response=response,
            )
            return response

        await self._await_existing_execution(task.task_id)
        interrupted_node = await self.storage.get_task_node(interrupt.node_id)
        if (
            interrupted_node is not None
            and interrupted_node.capability_id == "mcp.dispatch"
        ):
            resume_metadata["resume_interrupted_node_id"] = interrupted_node.node_id
            persisted_binding = await self._resolve_persisted_mcp_server_binding(
                task,
                node_id=interrupted_node.node_id,
            )
            if persisted_binding is not None:
                resume_metadata.update(
                    self._mcp_resolved_binding_runtime_metadata(persisted_binding)
                )
            else:
                server_id = str(interrupt.required_fields.get("server_id") or "").strip()
                if server_id:
                    resume_metadata["mcp_dispatch_server_id"] = server_id
        elif interrupted_node is not None and interrupted_node.capability_id.startswith("skill."):
            resume_metadata["resume_interrupted_node_id"] = interrupted_node.node_id
        owner_conversation = await self.storage.get_conversation(task.conversation_id)
        if owner_conversation is None:
            raise ValueError(f"Unknown conversation: {task.conversation_id}")
        resume_metadata.update(self._mcp_task_assignment_metadata(task))
        await self._schedule_execution(
            AgentExecutionRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=combined_message,
                owner_scope=self._agent_owner_scope(owner_conversation.username),
                requested_capability_id=None,
                metadata=resume_metadata,
                available_mcp_servers=await self.available_user_mcp_server_profiles(
                    owner_conversation.username,
                    execution_mode=task.mcp_execution_mode,
                ),
            ),
            await_durable_start=True,
        )
        if sheet_selection_resume_metadata:
            self._task_sheet_selection_resume_metadata.pop(task.task_id, None)
        response = {
            "interrupt_id": saved_interrupt.interrupt_id,
            "status": str(saved_interrupt.status),
            "node_id": saved_interrupt.node_id,
            "answer_payload": dict(answer_payload),
            "source_message_id": answer.source_message_id,
        }
        await self._record_interrupt_continuation_completed(
            task=task,
            interrupt=interrupt,
            reserved_message=reserved_message,
            response=response,
        )
        return response

    async def _resume_agent_interrupt(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        answer_payload: Mapping[str, Any],
        resume_metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        raw_locator = interrupt.required_fields.get("_agent_continuation")
        if isinstance(raw_locator, Mapping):
            locator = AgentContinuationLocatorService().from_safe_dict(raw_locator)
        else:
            if await self.agent_run_repository.get_run_for_task(task.task_id) is None:
                return False
            try:
                locator = await self._agent_locator_for_node(
                    task_id=task.task_id,
                    node_id=interrupt.node_id,
                )
            except RuntimeError:
                return False
        conversation = await self.storage.get_conversation(task.conversation_id)
        if conversation is None:
            raise ValueError(f"Unknown conversation: {task.conversation_id}")
        answer_digest = hashlib.sha256(
            json.dumps(
                dict(answer_payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        self._agent_invocation_contexts.merge(
            locator.run_id,
            metadata=dict(resume_metadata or {}),
            current_user_input=self._format_answer_message(answer_payload),
        )

        async def resolve(_locator):
            execution = await self._agent_capability_invoker.resume(locator)
            return AgentAuthorityResolution(
                authority_digest=locator.authority_digest,
                status=execution.status,
                safe_result_payload=execution.safe_result_payload,
                safe_continuation_facts={"answer_digest": answer_digest},
                safe_error_code=execution.safe_error_code,
                staged_artifacts=execution.staged_artifacts,
            )

        await self.interrupt_service.record_agent_continuation(
            locator,
            owner_scope=self._agent_owner_scope(conversation.username),
            authority_digest=locator.authority_digest,
            resolve_authority=resolve,
        )
        await self._clear_conversation_current_task(
            task.conversation_id,
            task.task_id,
        )
        return True

    async def _agent_locator_for_node(
        self,
        *,
        task_id: str,
        node_id: str,
    ):
        for interrupt in await self.storage.list_interrupts_for_task(task_id):
            if interrupt.node_id != node_id:
                continue
            raw = interrupt.required_fields.get("_agent_continuation")
            if isinstance(raw, Mapping):
                return AgentContinuationLocatorService().from_safe_dict(raw)
        run = await self.agent_run_repository.get_run_for_task(task_id)
        if run is not None:
            items = await self.agent_run_repository.list_items(run.run_id)
            call_ids = {
                item.item_id
                for item in items
                if item.item_id in run.waiting_call_item_ids
                and json.loads(item.payload_json).get("node_id") == node_id
            }
            for item in items:
                if item.source_call_item_id not in call_ids:
                    continue
                payload = json.loads(item.payload_json)
                safe_result = payload.get("safe_result")
                raw = (
                    safe_result.get("continuation_locator")
                    if isinstance(safe_result, Mapping)
                    else None
                )
                if isinstance(raw, Mapping):
                    return AgentContinuationLocatorService().from_safe_dict(raw)
        raise RuntimeError("agent_continuation_locator_missing")

    async def _recover_agent_mcp_dispatch(
        self,
        *,
        task: Task,
        locator,
        owner_user_id: str,
        metadata: Mapping[str, Any],
        authoritative_payload: Mapping[str, Any] | None = None,
    ) -> None:
        root_message = await self.storage.get_message(task.root_message_id)
        self._agent_invocation_contexts.merge(
            locator.run_id,
            metadata=metadata,
            current_user_input=(
                root_message.content if root_message is not None else task.summary or ""
            ),
        )

        async def resolve(_locator):
            if authoritative_payload is not None:
                return AgentAuthorityResolution(
                    authority_digest=locator.authority_digest,
                    status=AgentCallOutcomeStatus.COMPLETED,
                    safe_result_payload=dict(authoritative_payload),
                    safe_continuation_facts={"mcp_recovery": "authoritative_result"},
                )
            execution = await self._agent_capability_invoker.resume(locator)
            return AgentAuthorityResolution(
                authority_digest=locator.authority_digest,
                status=execution.status,
                safe_result_payload=execution.safe_result_payload,
                safe_continuation_facts={"mcp_recovery": "continued"},
                safe_error_code=execution.safe_error_code,
                staged_artifacts=execution.staged_artifacts,
            )

        recover = (
            self.interrupt_service.recover_agent_result
            if authoritative_payload is not None
            else self.interrupt_service.record_agent_continuation
        )
        await recover(
            locator,
            owner_scope=self._agent_owner_scope(owner_user_id),
            authority_digest=locator.authority_digest,
            resolve_authority=resolve,
        )

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

        reserved_message = await self._reserve_interrupt_user_message(
            task=task,
            interrupt=interrupt,
            answer_payload=answer_payload,
            source_message_id=source_message_id,
        )
        stored_interrupt = await self.storage.get_interrupt(interrupt.interrupt_id)
        if (
            stored_interrupt is not None
            and slot_collection_ref_from_required_fields(stored_interrupt.required_fields) is None
        ):
            await self.storage.save_interrupt(interrupt)
            await persist_interrupt_question_message(self.storage, interrupt)

        return await self._process_v2_interrupt_open_turn(
            task=task,
            interrupt=interrupt,
            collection=collection,
            raw_answer=dict(raw_answer),
            client_request_id=client_request_id,
            reserved_message=reserved_message,
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
        reserved_message: _ReservedInterruptMessage,
        request_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        turn_key = f"interrupt_turn:{interrupt.interrupt_id}:{client_request_id}"
        request_fingerprint = reserved_message.request.request_fingerprint or ""
        planned_receipt = await self._load_v2_interrupt_turn_plan_receipt(
            task_id=task.task_id,
            interrupt_id=interrupt.interrupt_id,
            client_request_id=client_request_id,
            source_message_id=reserved_message.message.message_id,
            request_fingerprint=request_fingerprint,
        )
        if planned_receipt is None:
            legacy_answers = [
                answer
                for answer in await self.storage.list_interrupt_answers(interrupt.interrupt_id)
                if isinstance(answer.answer_payload, Mapping)
                and str(answer.answer_payload.get("client_request_id") or "").strip()
                == client_request_id
            ]
            if legacy_answers and not any(
                answer.source_message_id == reserved_message.message.message_id
                and dict(answer.answer_payload)
                == {"client_request_id": client_request_id, "answer": dict(raw_answer)}
                for answer in legacy_answers
            ):
                raise MessageIdentityConflictError()
        exact_accepted_answer = next(
            (
                answer
                for answer in await self.storage.list_interrupt_answers(interrupt.interrupt_id)
                if answer.accepted
                and answer.source_message_id == reserved_message.message.message_id
                and dict(answer.answer_payload)
                == {"client_request_id": client_request_id, "answer": dict(raw_answer)}
            ),
            None,
        )
        current_collection = await self.storage.get_slot_collection(collection.collection_id) or collection
        existing_summary = await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, turn_key)
        if existing_summary is not None and existing_summary.event_type == "slot.interrupt_turn_processed":
            payload = dict(existing_summary.payload)
            summary_source_message_id = str(payload.get("source_message_id") or "")
            if summary_source_message_id != reserved_message.message.message_id:
                raise MessageIdentityConflictError()
            user_message = await self._ensure_reserved_interrupt_user_message(reserved_message)
            await self._ensure_v2_interrupt_turn_processed_event(
                task=task,
                interrupt=interrupt,
                summary=payload,
                created_at=user_message.created_at,
            )
            return self._interrupt_open_turn_response_from_summary(
                interrupt=interrupt,
                summary=payload,
                fallback_source_message_id=reserved_message.message.message_id,
            )

        if (
            exact_accepted_answer is not None
            and current_collection.status in {"ready", "script_scheduled"}
        ):
            await self._ensure_reserved_interrupt_user_message(reserved_message)
            interrupt = await self.interrupt_service.record_answer(
                exact_accepted_answer
            )
            if current_collection.status == "ready":
                await self._schedule_v2_slot_resume(
                    task=task,
                    interrupt=interrupt,
                    collection=current_collection,
                    raw_answer=raw_answer,
                    request_metadata=request_metadata,
                )
                current_collection, _ = await self._mark_v2_slot_script_scheduled(
                    current_collection
                )
            collection = current_collection

        # Compatibility with pre-query-split idempotency keys. New turns are always
        # persisted with the turn-level key above; legacy keys are read only.
        legacy_answer_key = f"answer:{interrupt.interrupt_id}:{client_request_id}"
        existing_legacy_event = await self.storage.get_slot_event_by_idempotency_key(collection.collection_id, legacy_answer_key)
        if existing_legacy_event is not None and planned_receipt is None:
            legacy_source_message_id = str(existing_legacy_event.payload.get("source_message_id") or "")
            if legacy_source_message_id and legacy_source_message_id != reserved_message.message.message_id:
                raise MessageIdentityConflictError()
            await self._ensure_reserved_interrupt_user_message(reserved_message)
            if existing_legacy_event.event_type == "slot.clarification_answered":
                return {
                    "interrupt_id": interrupt.interrupt_id,
                    "status": str(interrupt.status),
                    "node_id": interrupt.node_id,
                    "answer_payload": {"client_request_id": client_request_id},
                    "action": "clarification_answer",
                    "assistant_message": str(existing_legacy_event.payload.get("assistant_message") or ""),
                    "source_message_id": str(existing_legacy_event.payload.get("source_message_id") or reserved_message.message.message_id),
                }
            current_collection = await self.storage.get_slot_collection(collection.collection_id) or collection
            return {
                "interrupt_id": interrupt.interrupt_id,
                "status": str(interrupt.status),
                "node_id": interrupt.node_id,
                "answer_payload": {"client_request_id": client_request_id},
                "action": "resumed" if current_collection.status in {"script_scheduled", "completed"} else None,
                "source_message_id": str(existing_legacy_event.payload.get("source_message_id") or reserved_message.message.message_id),
            }

        if interrupt.status != InterruptStatus.OPEN and planned_receipt is None:
            replay_response = await self._completed_interrupt_replay_response(
                task=task,
                interrupt=interrupt,
                answer_payload={"client_request_id": client_request_id, "answer": dict(raw_answer)},
                reserved_message=reserved_message,
            )
            if replay_response is not None:
                return replay_response
            raise UploadValidationError(
                "v2 slot interrupt is not open; retry with the original client_request_id to replay an accepted turn"
            )

        turn_llm_metadata = await self._resume_llm_metadata(task, request_metadata)
        if planned_receipt is None:
            plan = await self._plan_v2_interrupt_open_turn(
                task=task,
                interrupt=interrupt,
                collection=collection,
                raw_answer=raw_answer,
                client_request_id=client_request_id,
                llm_metadata=turn_llm_metadata,
            )
            plan = await self._record_v2_interrupt_turn_planned(
                task=task,
                interrupt=interrupt,
                collection=collection,
                client_request_id=client_request_id,
                plan=plan,
                source_message_id=reserved_message.message.message_id,
                request_fingerprint=request_fingerprint,
            )
        else:
            plan = planned_receipt
        user_message = await self._ensure_reserved_interrupt_user_message(reserved_message)

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
            verifier_key = f"{turn_key}:substep:verifier"
            verifier_receipt = await self._v2_substep_receipt(
                collection.collection_id,
                verifier_key,
            )
            if verifier_receipt is None:
                verifier_block_candidate = await self._verify_v2_interrupt_resume(
                    task=task,
                    interrupt=interrupt,
                    collection=collection,
                    raw_answer=raw_answer,
                    decision=verifier_decision,
                    llm_metadata=turn_llm_metadata,
                )
                await self._record_v2_substep_receipt(
                    collection=collection,
                    key=verifier_key,
                    payload={
                        "allow_resume": verifier_block_candidate.allow_resume,
                        "confidence": verifier_block_candidate.confidence,
                        "reason": verifier_block_candidate.reason,
                        "clarification_answer": verifier_block_candidate.clarification_answer,
                    },
                )
            else:
                verifier_block_candidate = InterruptResumeVerification(
                    allow_resume=bool(verifier_receipt.get("allow_resume")),
                    confidence=float(verifier_receipt.get("confidence") or 0.0),
                    reason=str(verifier_receipt.get("reason") or ""),
                    clarification_answer=str(verifier_receipt.get("clarification_answer") or ""),
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
            question_key = f"{turn_key}:substep:question:{part.part_id}"
            question_receipt = await self._v2_substep_receipt(
                latest_collection.collection_id,
                question_key,
            )
            if question_receipt is not None:
                answer = str(question_receipt.get("answer") or "")
            else:
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
                await self._record_v2_substep_receipt(
                    collection=latest_collection,
                    key=question_key,
                    payload={"answer": answer},
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
            clarification_key = f"{turn_key}:substep:clarification:{part.part_id}"
            clarification_receipt = await self._v2_substep_receipt(
                latest_collection.collection_id,
                clarification_key,
            )
            if clarification_receipt is not None:
                message = str(clarification_receipt.get("answer") or "")
            else:
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
                await self._record_v2_substep_receipt(
                    collection=latest_collection,
                    key=clarification_key,
                    payload={"answer": message},
                )
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
        will_resume = latest_collection.status in {"script_scheduled", "completed"}
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
                await self._schedule_v2_slot_resume(
                    task=task,
                    interrupt=saved_interrupt,
                    collection=latest_collection,
                    raw_answer=raw_answer,
                    request_metadata=request_metadata,
                )
                latest_collection, _ = await self._mark_v2_slot_script_scheduled(latest_collection)
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
                message_id=f"{user_message.message_id}:interrupt-response",
                conversation_id=task.conversation_id,
                role=MessageRole.ASSISTANT,
                content=assistant_message,
                task_id=task.task_id,
                stream_status=INTERRUPT_VISIBLE_STREAM_STATUS,
                created_at=user_message.created_at,
            )
            existing_assistant_message = await self.storage.get_message(
                assistant_message_record.message_id
            )
            if existing_assistant_message is None:
                await self.storage.save_message(assistant_message_record)
            else:
                assistant_message = existing_assistant_message.content

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
        await self._ensure_v2_interrupt_turn_processed_event(
            task=task,
            interrupt=interrupt,
            summary=summary_payload,
            created_at=user_message.created_at,
        )
        return self._interrupt_open_turn_response_from_summary(
            interrupt=saved_interrupt,
            summary=summary_payload,
            fallback_source_message_id=user_message.message_id,
        )

    async def _ensure_v2_interrupt_turn_processed_event(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        summary: Mapping[str, object],
        created_at: datetime,
    ) -> None:
        client_request_id = str(summary.get("client_request_id") or "")
        slot_collection_id = str(summary.get("active_slot_collection_id") or "")
        event_payload = {
            "interrupt_id": interrupt.interrupt_id,
            "slot_collection_id": slot_collection_id,
            "client_request_id": client_request_id,
            "action": summary.get("action"),
            "will_resume": bool(summary.get("will_resume")),
            "requires_confirmation": bool(summary.get("requires_confirmation")),
        }
        for existing in await self.storage.list_events_for_task_filtered(
            task.task_id,
            event_types=("task.interrupt_turn_processed",),
        ):
            existing_payload = dict(existing.payload)
            if all(
                existing_payload.get(key) == event_payload[key]
                for key in (
                    "interrupt_id",
                    "slot_collection_id",
                    "client_request_id",
                )
            ):
                if existing_payload != event_payload:
                    raise RuntimeError(
                        "runtime_store_idempotency_conflict: interrupt turn event differs"
                    )
                return
        identity_digest = hashlib.sha256(
            json.dumps(
                [interrupt.interrupt_id, slot_collection_id, client_request_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        event = self._make_event(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            node_id=interrupt.node_id,
            event_type="task.interrupt_turn_processed",
            payload=event_payload,
            created_at=created_at,
        )
        event = replace(
            event,
            event_id=f"slot-interrupt-turn-processed:v1:{identity_digest}",
        )
        saved, duplicate = await self.storage.append_event_exact(event)
        if not duplicate:
            await self.event_broker.publish(saved)

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
                    reason="interrupt_open_turn_interpreter_invalid",
                    blocks_resume=True,
                    block_reason="",
                ),
            ),
            confidence=1.0,
            reason="interrupt_open_turn_interpreter_invalid",
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
        source_message_id: str,
        request_fingerprint: str,
    ) -> InterruptOpenTurnPlan:
        for event in await self.storage.list_events_for_task_filtered(
            task.task_id,
            event_types=("task.interrupt_turn_planned",),
        ):
            if (
                event.event_type == "task.interrupt_turn_planned"
                and str(event.payload.get("interrupt_id") or "") == interrupt.interrupt_id
                and str(event.payload.get("client_request_id") or "") == client_request_id
            ):
                if "source_message_id" not in event.payload or "request_fingerprint" not in event.payload:
                    continue
                if (
                    str(event.payload.get("source_message_id") or "") != source_message_id
                    or str(event.payload.get("request_fingerprint") or "") != request_fingerprint
                ):
                    raise MessageIdentityConflictError()
                stored_plan = event.payload.get("plan")
                return (
                    self._interrupt_open_turn_plan_from_payload(stored_plan)
                    if isinstance(stored_plan, Mapping)
                    else plan
                )
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
                    "source_message_id": source_message_id,
                    "request_fingerprint": request_fingerprint,
                    "plan": self._interrupt_open_turn_plan_payload(plan),
                    "part_kinds": [part.kind for part in plan.parts],
                    "confidence": plan.confidence,
                    "fallback": plan.fallback,
                    "fallback_reason": plan.fallback_reason,
                },
            )
        )
        return plan

    async def _load_v2_interrupt_turn_plan_receipt(
        self,
        *,
        task_id: str,
        interrupt_id: str,
        client_request_id: str,
        source_message_id: str,
        request_fingerprint: str,
    ) -> InterruptOpenTurnPlan | None:
        events = await self.storage.list_events_for_task_filtered(
            task_id,
            event_types=("task.interrupt_turn_planned",),
        )
        for event in reversed(events):
            if (
                event.event_type != "task.interrupt_turn_planned"
                or str(event.payload.get("interrupt_id") or "") != interrupt_id
                or str(event.payload.get("client_request_id") or "") != client_request_id
            ):
                continue
            if "source_message_id" not in event.payload or "request_fingerprint" not in event.payload:
                continue
            if (
                str(event.payload.get("source_message_id") or "") != source_message_id
                or str(event.payload.get("request_fingerprint") or "") != request_fingerprint
            ):
                raise MessageIdentityConflictError()
            stored_plan = event.payload.get("plan")
            if not isinstance(stored_plan, Mapping):
                return None
            return self._interrupt_open_turn_plan_from_payload(stored_plan)
        return None

    @staticmethod
    def _interrupt_open_turn_plan_payload(
        plan: InterruptOpenTurnPlan,
    ) -> dict[str, object]:
        return {
            "parts": [
                {
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
                    "assistant_message": part.assistant_message,
                }
                for part in plan.parts
            ],
            "confidence": plan.confidence,
            "reason": plan.reason,
            "fallback": plan.fallback,
            "fallback_reason": plan.fallback_reason,
        }

    @staticmethod
    def _interrupt_open_turn_plan_from_payload(
        payload: Mapping[str, object],
    ) -> InterruptOpenTurnPlan:
        raw_parts = payload.get("parts")
        if not isinstance(raw_parts, list):
            raise RuntimeError("interrupt_turn_plan_receipt_invalid")
        parts: list[InterruptOpenTurnPart] = []
        try:
            for raw_part in raw_parts:
                if not isinstance(raw_part, Mapping):
                    raise TypeError
                raw_target_slots = raw_part.get("target_slots")
                if not isinstance(raw_target_slots, list):
                    raise TypeError
                parts.append(
                    InterruptOpenTurnPart(
                        part_id=str(raw_part["part_id"]),
                        kind=str(raw_part["kind"]),
                        text=str(raw_part.get("text") or ""),
                        target_slots=tuple(str(item) for item in raw_target_slots),
                        target_schema_id=(
                            None
                            if raw_part.get("target_schema_id") is None
                            else str(raw_part["target_schema_id"])
                        ),
                        reuse_decision=str(raw_part.get("reuse_decision") or "unspecified"),
                        execution_confirmation=bool(raw_part.get("execution_confirmation")),
                        execution_confirmation_confidence=float(raw_part.get("execution_confirmation_confidence") or 0.0),
                        uses_uploads=bool(raw_part.get("uses_uploads")),
                        confidence=float(raw_part.get("confidence") or 0.0),
                        reason=str(raw_part.get("reason") or ""),
                        blocks_resume=bool(raw_part.get("blocks_resume")),
                        block_reason=str(raw_part.get("block_reason") or ""),
                        assistant_message=str(raw_part.get("assistant_message") or ""),
                    )
                )
            return InterruptOpenTurnPlan(
                parts=tuple(parts),
                confidence=float(payload.get("confidence") or 0.0),
                reason=str(payload.get("reason") or ""),
                fallback=bool(payload.get("fallback")),
                fallback_reason=str(payload.get("fallback_reason") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("interrupt_turn_plan_receipt_invalid") from exc

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
            turn_part=part,
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
    ) -> InterruptAnswer:
        answer = InterruptAnswer(
            interrupt_answer_id=self._interrupt_answer_id(
                interrupt.interrupt_id,
                source_message_id,
            ),
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
        answer_payload = {"client_request_id": client_request_id, "answer": dict(raw_answer)}
        existing = await self.storage.list_interrupt_answers(interrupt.interrupt_id)
        matching_client_answers = [
            answer
            for answer in existing
            if isinstance(answer.answer_payload, Mapping)
            and str(answer.answer_payload.get("client_request_id") or "").strip() == client_request_id
        ]
        if matching_client_answers:
            if any(
                answer.source_message_id == source_message_id
                and dict(answer.answer_payload) == answer_payload
                for answer in matching_client_answers
            ):
                return
            raise MessageIdentityConflictError()
        answer_key = hashlib.sha256(
            f"v2-interrupt-answer:{interrupt.interrupt_id}:{client_request_id}".encode("utf-8")
        ).hexdigest()
        await self.storage.save_interrupt_answer(
            InterruptAnswer(
                interrupt_answer_id=f"interrupt-answer:{answer_key}",
                interrupt_id=interrupt.interrupt_id,
                answer_payload=answer_payload,
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
        await self._record_v2_interrupt_skill_question_audit(
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

    async def _v2_substep_receipt(
        self,
        collection_id: str,
        key: str,
    ) -> Mapping[str, object] | None:
        event = await self.storage.get_slot_event_by_idempotency_key(collection_id, key)
        if event is None:
            return None
        if event.event_type != "slot.interrupt_substep_completed":
            raise RuntimeError("interrupt_substep_receipt_invalid")
        return event.payload

    async def _record_v2_substep_receipt(
        self,
        *,
        collection: SlotCollection,
        key: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        existing = await self._v2_substep_receipt(collection.collection_id, key)
        if existing is not None:
            return existing
        await self.storage.append_slot_event(
            SlotEvent(
                slot_event_id=f"{collection.collection_id}:event:substep:{hashlib.sha256(key.encode('utf-8')).hexdigest()}",
                collection_id=collection.collection_id,
                task_id=collection.task_id,
                node_id=collection.node_id,
                conversation_id=collection.conversation_id,
                event_type="slot.interrupt_substep_completed",
                round=collection.round,
                revision=collection.revision,
                idempotency_key=key,
                payload=dict(payload),
                created_at=self._utcnow_naive(),
            )
        )
        return payload

    async def _record_v2_interrupt_skill_question_audit(
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
                event_type="skill.question_answered",
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
            turn_target_slots=part.target_slots,
            turn_reason=part.reason,
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
            required_fields=self._slot_collection_required_fields_for_interrupt(
                active,
                interrupt,
            ),
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
            required_fields=self._slot_collection_required_fields_for_interrupt(
                active,
                interrupt,
            ),
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
        if await self._resume_agent_interrupt(
            task=task,
            interrupt=interrupt,
            answer_payload=raw_answer,
            resume_metadata={
                **self._resume_skill_revision_metadata(task.task_id),
                SLOT_COLLECTION_METADATA_KEY: self._slot_collection_resume_metadata(collection),
                "slot_collection_id": collection.collection_id,
                "slot_collection_revision": collection.revision,
                "resume_interrupted_node_id": interrupt.node_id,
            },
        ):
            return
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
        owner_conversation = await self.storage.get_conversation(task.conversation_id)
        if owner_conversation is None:
            raise ValueError(f"Unknown conversation: {task.conversation_id}")
        await self._schedule_execution(
            AgentExecutionRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=self._combine_v2_resume_message(
                    root_message.content if root_message is not None else task.summary or "",
                    raw_answer,
                ),
                owner_scope=self._agent_owner_scope(owner_conversation.username),
                requested_capability_id=None,
                metadata=resume_metadata,
                available_mcp_servers=await self.available_user_mcp_server_profiles(
                    owner_conversation.username,
                    execution_mode=task.mcp_execution_mode,
                ),
            ),
            await_durable_start=True,
        )

    @staticmethod
    def _slot_collection_required_fields_for_interrupt(
        collection: SlotCollection,
        interrupt: Interrupt,
    ) -> dict[str, object]:
        fields = slot_collection_required_fields_ref(collection)
        locator = interrupt.required_fields.get("_agent_continuation")
        if isinstance(locator, Mapping):
            fields["_agent_continuation"] = dict(locator)
        return fields

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
    def _slot_answer_turn_hint(part: InterruptOpenTurnPart | None) -> dict[str, object]:
        if part is None:
            return {}
        return {
            "source": "interrupt_open_turn_interpreter",
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
        turn_part: InterruptOpenTurnPart | None = None,
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
                turn_hint=self._slot_answer_turn_hint(turn_part),
            )
            if history_recall
            else build_normal_extraction_prompt(
                validating,
                current_user_answer=answer_text,
                artifact_summaries=artifact_summaries,
                turn_hint=self._slot_answer_turn_hint(turn_part),
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
            turn_target_slots=turn_part.target_slots if turn_part is not None else (),
            turn_reason=turn_part.reason if turn_part is not None else None,
        )
        extraction = merge_slot_extraction_results(extraction, backend_extraction, collection=validating)
        if not extraction.resolved:
            extraction = self._fallback_v2_slot_extraction(
                validating,
                schema,
                raw_answer=raw_answer,
                turn_part=turn_part,
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
        turn_part: InterruptOpenTurnPart | None = None,
    ) -> dict[str, dict[str, object]]:
        extraction = build_backend_slot_extraction(
            collection,
            schema,
            current_user_answer=self._v2_answer_text(raw_answer),
            current_upload_ids=self._v2_answer_upload_ids(raw_answer),
            turn_target_slots=turn_part.target_slots if turn_part is not None else (),
            turn_reason=turn_part.reason if turn_part is not None else None,
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
        turn_part: InterruptOpenTurnPart | None = None,
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
                turn_part=turn_part,
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
                content_bytes=self._read_conversation_file_resource_bytes_exact(
                    resource
                ),
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
                content_bytes=self._read_conversation_file_resource_bytes_exact(
                    resource
                ),
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
        if (
            len(content) != attachment.size_bytes
            or hashlib.sha256(content).hexdigest() != attachment.sha256
        ):
            raise UploadValidationError("task_input_attachment_blob_drift")
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
            content=self._read_conversation_file_resource_bytes_exact(resource),
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
        saved = await self.storage.apply_conversation_file_sheet_selection_exact(
            resource,
            updated,
        )
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
        terminal_event_types = {
            "task.completed",
            "task.failed",
            "task.cancelled",
            "agent.run.completed",
            "agent.run.failed",
            "agent.run.cancelled",
        }
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
        continuation_projection: str | None = None
        if str(command.payload.get("call_status") or "") == "completed":
            if self._mcp_agent_projection_authority is None:
                return
            call = await self.storage.get_mcp_call_record(
                command.owner_user_id, command.task_id, command.call_ref
            )
            receipt = await self.storage.get_mcp_terminal_result_receipt_for_call(
                command.call_ref
            )
            if call is None or receipt is None:
                return
            try:
                continuation_projection = (
                    await self._mcp_agent_projection_authority.load_agent_projection(
                        call=call,
                        receipt=receipt,
                    )
                )
            except Exception:
                # Keep the command claimed. Lease expiry/reconciler may repair the
                # projection; never read raw or invoke MCP as compensation.
                return
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
        if conversation is None:
            raise RuntimeError("mcp_continuation_conversation_missing")
        raw_locator = running.payload.get("continuation_plan")
        if not isinstance(raw_locator, Mapping):
            raise RuntimeError("agent_continuation_locator_missing")
        try:
            locator = AgentContinuationLocatorService().from_safe_dict(raw_locator)
        except ValueError as exc:
            raise RuntimeError("agent_continuation_locator_invalid") from exc
        if locator.task_id != task.task_id or locator.node_id != running.node_id:
            raise RuntimeError("agent_continuation_locator_identity_mismatch")
        if continuation_projection is None:
            raise RuntimeError("mcp_continuation_projection_missing")

        async def resolve_authority(_locator):
            return AgentAuthorityResolution(
                authority_digest=locator.authority_digest,
                status=AgentCallOutcomeStatus.COMPLETED,
                safe_result_payload={
                    "status": "completed",
                    "projection": continuation_projection,
                },
                safe_continuation_facts={"mcp_remote_task": "completed"},
            )

        async def acknowledge() -> None:
            await _mark_remote_continuation_dispatched(
                self.storage, running, self._utcnow_naive()
            )

        await self.interrupt_service.record_agent_continuation(
            locator,
            owner_scope=self._agent_owner_scope(conversation.username),
            authority_digest=locator.authority_digest,
            resolve_authority=resolve_authority,
            acknowledge=acknowledge,
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
        try:
            await self._master_key_sentinel_cipher.create_or_verify_sentinel(
                self.storage
            )
        except Exception:
            self._engine.dispose()
            raise
        await self._verify_submission_authority_binding()
        await self.recover_deleting_conversations()
        submission_projection_pending = False
        coordinator = getattr(self, "_submission_admission_coordinator", None)
        if coordinator is not None:
            await coordinator.project_pending()
            submission_projection_pending = True
        try:
            await self._admit_mcp_rollout_instance()
            aggregate_reconciler = MCPAggregateStartupReconciler(
                MCPAggregateRecoveryStages(
                    repair_lifecycle_markers=(
                        self._repair_mcp_terminal_candidate_lifecycle
                    ),
                    enumerate_terminal_candidates=(
                        self._strict_enumerate_mcp_terminal_candidates
                    ),
                    reconcile_terminal_candidates=(
                        self._reconcile_mcp_terminal_candidates
                    ),
                    reconcile_remote_bindings=self._reconcile_mcp_remote_bindings,
                    reconcile_mrtr_evidence=(
                        self._validate_mcp_mrtr_recovery_evidence
                    ),
                    reconcile_pending_actions=(
                        self._validate_mcp_pending_action_recovery_evidence
                    ),
                    reconcile_resume_envelopes=(
                        self._validate_mcp_resume_envelope_authority
                    ),
                    recover_expired_claims=(
                        self._recover_expired_mcp_dispatch_claims
                    ),
                    converge_unknown_no_replay=(
                        self._converge_inactive_and_unknown_mcp_dispatches
                    ),
                    validate_invariants=self._validate_mcp_aggregate_invariants,
                )
            )
            await aggregate_reconciler.run()
            await self._reconcile_mcp_dispatch_recovery()
            if coordinator is not None:
                await coordinator.recover_projected_handoffs()
                submission_projection_pending = False
        except BaseException:
            if submission_projection_pending and coordinator is not None:
                await coordinator.abort_pending()
            raise
        try:
            await self._recover_agent_runs()
            agent_skill_result_janitor = getattr(
                self, "_agent_skill_result_janitor", None
            )
            if agent_skill_result_janitor is not None:
                await agent_skill_result_janitor.run_once()
            if self._mcp_cp7_safety_facade is not None:
                if self._mcp_cp7_open_boundary is None:
                    raise RuntimeError("mcp_cp7_open_boundary_missing")
                if not self._mcp_cp7_safety_facade.opened:
                    try:
                        await self._mcp_cp7_safety_facade.open_epoch(
                            self._mcp_cp7_open_boundary,
                            predecessor=self._mcp_cp7_predecessor_close,
                            verifier_authorized=self._mcp_cp7_verifier_authorized,
                        )
                    except CP7SafetyFatalPersistenceError:
                        self._mcp_cp7_fatal_exit(70)
                        raise
                if self._mcp_cp7_boundary_provider is None:
                    raise RuntimeError("mcp_cp7_boundary_provider_missing")
                self._mcp_cp7_minute_task = asyncio.create_task(
                    self._run_cp7_safety_minutes(), name="mcp-cp7-safety-minute-producer"
                )
                self._mcp_cp7_minute_task.add_done_callback(
                    self._handle_cp7_safety_minute_exit
                )
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
            if self.mcp_remote_task_recovery_worker is not None:
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
            if (
                self._mcp_rollout_metric_recorder is not None
                and self._mcp_rollout_zero_series_task is None
            ):
                self._mcp_rollout_zero_series_task = asyncio.create_task(
                    self._mcp_rollout_metric_recorder.run_continuous_zero_series(),
                    name="mcp-rollout-zero-series",
                )
            if (
                self._mcp_post_ready_recovery_task is None
                or self._mcp_post_ready_recovery_task.done()
            ):
                self._mcp_post_ready_recovery_error = None
                self._mcp_post_ready_recovery_task = asyncio.create_task(
                    self._reconcile_mcp_dispatch_recovery(),
                    name="mcp-post-ready-dispatch-recovery",
                )
                self._mcp_post_ready_recovery_task.add_done_callback(
                    self._handle_mcp_post_ready_recovery_exit
                )
            if (
                self._mcp_result_artifact_projector is not None
                and self._mcp_durable_result_lifecycle_manager is not None
                and (
                    self._mcp_result_artifact_reconciler_task is None
                    or self._mcp_result_artifact_reconciler_task.done()
                )
            ):
                self._mcp_result_artifact_reconciler_error = None
                self._mcp_result_artifact_reconciler_task = asyncio.create_task(
                    self._run_mcp_result_artifact_reconciler_forever(),
                    name="mcp-result-artifact-reconciler",
                )
                self._mcp_result_artifact_reconciler_task.add_done_callback(
                    self._handle_mcp_result_artifact_reconciler_exit
                )
        except BaseException:
            await self._cancel_agent_run_lease_retries()
            raise

    async def _verify_submission_authority_binding(self) -> None:
        expected_receipt = getattr(
            self, "_expected_submission_authority_receipt_sha256", None
        )
        if expected_receipt is None:
            return
        client = self._runtime_sidecar_client
        response = client.claim_pending_submission(
            workflow_owner="api-startup-authority-probe",
            now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            claim_ttl_ms=1,
            after_created_at_ms=9_223_372_036_854_775_807,
            after_message_id="",
        )
        if inspect.isawaitable(response):
            response = await response
        envelope = validate_runtime_sidecar_response(
            "submission_pending_claim", response
        )
        if (
            envelope.get("error") is not None
            or envelope.get("found") is not False
            or envelope.get("pending_count") != 0
            or envelope.get("authority_state") != "finalized"
            or envelope.get("finalization_receipt_sha256") != expected_receipt
        ):
            raise RuntimeError("submission_authority_receipt_mismatch")

    async def _repair_mcp_terminal_candidate_lifecycle(self) -> None:
        candidate_manager = self._mcp_terminal_candidate_lifecycle_manager
        if candidate_manager is not None:
            while (
                await candidate_manager.repair_incomplete(limit=1000) == 1000
            ):
                pass
            await candidate_manager.run_once(limit=1000)
        result_manager = self._mcp_durable_result_lifecycle_manager
        if result_manager is not None:
            while await result_manager.repair_incomplete(limit=1000) == 1000:
                pass

    async def _recover_agent_runs(self) -> None:
        for run in await self.agent_run_repository.list_recoverable_runs():
            try:
                await self._recover_agent_run(run)
            except AgentStorageConflict as exc:
                if str(exc) != "agent_task_lease_held":
                    raise
                self._schedule_agent_run_lease_retry(run.run_id)

    async def _recover_agent_run(self, run: AgentRun) -> None:
        task = await self.storage.get_task(run.task_id)
        if task is None or task.conversation_id != run.conversation_id:
            raise RuntimeError("agent_startup_task_identity_mismatch")
        conversation = await self.storage.get_conversation(run.conversation_id)
        if conversation is None:
            raise RuntimeError("agent_startup_conversation_missing")
        await self._reconcile_agent_terminal_events(run)
        list_agent_items = getattr(
            getattr(self, "agent_run_repository", None), "list_items", None
        )
        uninitialized_run = bool(
            callable(list_agent_items)
            and not await list_agent_items(run.run_id)
        )
        root_message = await self.storage.get_message(task.root_message_id)
        prepared = None
        prepared_loader = getattr(self, "_prepared_agent_recovery_loader", None)
        if prepared_loader is not None:
            prepared = await prepared_loader.load(
                username=conversation.username,
                conversation_id=conversation.conversation_id,
                task_id=task.task_id,
                message_id=task.root_message_id,
                root_message_content=(
                    root_message.content if root_message is not None else None
                ),
            )
        if uninitialized_run and prepared is None:
            raise RuntimeError("agent_startup_initialization_authority_missing")
        if prepared is None:
            user_message = (
                root_message.content
                if root_message is not None
                else task.summary or ""
            )
            metadata = {
                **await self._task_accepted_llm_metadata(task.task_id),
                **self._mcp_task_assignment_metadata(task),
            }
            if self._skill_runtime_state is not None:
                metadata["skill_bundle_revision"] = (
                    self._skill_runtime_state.active_revision
                )
            profiles = await self.available_user_mcp_server_profiles(
                conversation.username,
                execution_mode=task.mcp_execution_mode,
            )
            owner_scope = self._agent_owner_scope(conversation.username)
            initial_required_tool_name = None
        else:
            (
                metadata,
                profiles,
                owner_scope,
                user_message,
                initial_required_tool_name,
            ) = await self._prepared_agent_recovery_values(
                run=run,
                task=task,
                conversation=conversation,
                prepared=prepared,
            )
        metadata["available_mcp_server_ids"] = [
            profile.server_id for profile in profiles
        ]
        if uninitialized_run:
            assert prepared is not None
            memory_context = prepared.memory_context
            initialization_request = AgentExecutionRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=user_message,
                owner_scope=owner_scope,
                requested_capability_id=task.requested_capability_id,
                metadata=metadata,
                current_user_message=(
                    str(memory_context.get("current_user_message"))
                    if memory_context is not None
                    and memory_context.get("current_user_message") is not None
                    else None
                ),
                resolved_user_message=(
                    str(memory_context.get("resolved_user_message"))
                    if memory_context is not None
                    and memory_context.get("resolved_user_message") is not None
                    else None
                ),
                memory_context=memory_context,
                available_mcp_servers=profiles,
                skill_activation_payload_json=(
                    prepared.skill_activation_payload_json
                ),
                skill_activation_payload_sha256=(
                    prepared.skill_activation_payload_sha256
                ),
            )
            await self._fail_if_effective_uploads_inactive_for_execution(
                initialization_request
            )
            initialized = await self.agent_loop_orchestrator.initialize_run(
                initialization_request
            )
            run = initialized.run
        if run.status in {
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentRunStatus.WAITING_FOR_DEPENDENCY,
        }:
            self._agent_invocation_contexts.merge(
                run.run_id,
                metadata={
                    **metadata,
                    "agent_owner_scope": owner_scope,
                },
                current_user_input=user_message,
            )
            return
        if prepared is not None and not uninitialized_run:
            await self._fail_if_effective_uploads_inactive_for_execution(
                AgentExecutionRequest(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    root_message_id=task.root_message_id,
                    user_message=user_message,
                    owner_scope=owner_scope,
                    requested_capability_id=task.requested_capability_id,
                    metadata=metadata,
                    available_mcp_servers=profiles,
                )
            )
        self._agent_invocation_contexts.merge(
            run.run_id,
            metadata={
                **metadata,
                "agent_owner_scope": owner_scope,
            },
            current_user_input=user_message,
        )
        trusted = tuple(
            json.dumps(
                {key: metadata[key]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            for key in (
                "capability_missing_fallback",
                "conversation_memory",
                "mcp_remote_task_result_projection",
                "slot_collection",
                "uploaded_artifacts",
            )
            if key in metadata
        )
        recovery_result = await self._agent_run_recovery.recover_crashed_run(
            run.run_id,
            initial_required_tool_name=initial_required_tool_name,
            trusted_facts=trusted,
            visibility_context=CapabilityVisibilityContext(
                authenticated_owner_scope=owner_scope,
                execution_path=str(task.mcp_execution_mode or "default"),
                pinned_skill_bundle_revision=str(
                    metadata.get("skill_bundle_revision") or ""
                ).strip()
                or None,
                safe_mcp_server_profiles=profiles,
            ),
            cancellation=self._agent_cancellation_token(task.task_id),
        )
        await self._reconcile_agent_terminal_events(recovery_result.run)
        if recovery_result.run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            await self._clear_conversation_current_task(
                task.conversation_id,
                task.task_id,
            )
            await self._release_task_skill_revision_if_terminal(task.task_id)
            await self._release_task_mcp_revision_if_terminal(task.task_id)

    async def _reconcile_agent_terminal_events(self, run: AgentRun) -> None:
        repository = getattr(self, "agent_run_repository", None)
        list_items = getattr(repository, "list_items", None)
        if not callable(list_items):
            return
        items = await list_items(run.run_id)
        calls = {
            item.item_id: item
            for item in items
            if item.kind.value == "tool_call"
        }
        for result in items:
            if (
                result.kind.value != "tool_result"
                or result.state.value != "committed"
                or result.source_call_item_id not in calls
            ):
                continue
            call = calls[str(result.source_call_item_id)]
            event = build_agent_terminal_event(
                run=run,
                call_item=call,
                result_item=result,
            )
            node = (
                await self.storage.get_task_node(event.node_id)
                if event.node_id is not None
                else None
            )
            expected_status = (
                NodeStatus.COMPLETED
                if event.event_type == "node.completed"
                else NodeStatus.FAILED
            )
            if node is None or str(node.status) != str(expected_status):
                raise RuntimeError("agent_terminal_event_node_conflict")
            saved, duplicate = await self.storage.append_event_exact(event)
            if not duplicate:
                await self.event_broker.publish(saved)

    async def _prepared_agent_recovery_values(
        self,
        *,
        run: AgentRun,
        task: Task,
        conversation: Conversation,
        prepared: PreparedAgentRecoveryContext,
    ) -> tuple[
        dict[str, Any],
        tuple[UserMCPServerProfile, ...],
        str,
        str,
        str | None,
    ]:
        if prepared.username != conversation.username:
            raise RuntimeError("agent_prepared_owner_mismatch")
        model_options = dict(prepared.model_options)
        prepared_model = str(model_options.get("model_edition") or "").strip()
        if (
            (prepared_model and prepared_model != run.binding.model_edition)
            or model_options.get("reasoning_effort") != run.binding.reasoning_effort
            or model_options.get("thinking_enabled") is not run.binding.thinking_enabled
        ):
            raise RuntimeError("agent_prepared_model_binding_mismatch")
        expected_assignment = self._prepared_task_mcp_assignment(task)
        if prepared.mcp_assignment != expected_assignment:
            raise RuntimeError("agent_prepared_mcp_assignment_mismatch")

        metadata = {
            key: value
            for key, value in prepared.execution_metadata.items()
            if value is not None
        }
        if prepared_model:
            metadata["model_edition"] = prepared_model
        metadata.update(
            {
                "deep_thinking": bool(model_options["thinking_enabled"]),
                "main_agent_thinking_enabled": bool(
                    model_options["thinking_enabled"]
                ),
                "main_agent_reasoning_effort": str(
                    model_options["reasoning_effort"]
                ),
                "requested_reasoning_effort": str(
                    model_options["reasoning_effort"]
                ),
            }
        )
        for key, value in prepared.bundle_revisions.items():
            if value is not None:
                metadata[key] = value
        if prepared.memory_context is not None:
            metadata["conversation_memory"] = dict(prepared.memory_context)
        metadata.update(
            await self._prepared_task_input_attachment_metadata(
                task.task_id,
                upload_refs=prepared.upload_refs,
                expected_upload_ids=prepared.selected_upload_ids,
            )
        )

        self._restore_prepared_bundle_revisions(
            task_id=task.task_id,
            skill_revision=prepared.bundle_revisions.get("skill_bundle_revision"),
            mcp_revision=prepared.bundle_revisions.get("mcp_bundle_revision"),
        )
        return (
            metadata,
            tuple(prepared.available_mcp_servers),
            self._agent_owner_scope(prepared.username),
            prepared.current_user_input,
            prepared.initial_required_tool_name,
        )

    @staticmethod
    def _prepared_task_mcp_assignment(task: Task) -> dict[str, object] | None:
        values = (
            task.mcp_execution_mode,
            task.mcp_shadow_enabled,
            task.mcp_rollout_config_version,
            task.mcp_route_reason_code,
            task.mcp_rollout_mode,
        )
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise RuntimeError("agent_prepared_task_mcp_assignment_corrupt")
        return {
            "execution_mode": task.mcp_execution_mode,
            "shadow_enabled": task.mcp_shadow_enabled,
            "rollout_config_version": task.mcp_rollout_config_version,
            "route_reason_code": task.mcp_route_reason_code,
            "rollout_mode": task.mcp_rollout_mode,
        }

    def _restore_prepared_bundle_revisions(
        self,
        *,
        task_id: str,
        skill_revision: object,
        mcp_revision: object,
    ) -> None:
        candidates = (
            (
                "skill",
                str(skill_revision or "").strip(),
                self._skill_runtime_state,
                self._task_skill_bundle_revisions,
            ),
            (
                "mcp",
                str(mcp_revision or "").strip(),
                self._mcp_runtime_state,
                self._task_mcp_bundle_revisions,
            ),
        )
        pending: list[tuple[str, str, Any, dict[str, str]]] = []
        for kind, revision, state, retained in candidates:
            if not revision:
                continue
            if state is None:
                raise RuntimeError(f"agent_prepared_{kind}_bundle_runtime_missing")
            existing = retained.get(task_id)
            if existing is not None:
                if existing != revision:
                    raise RuntimeError(
                        f"agent_prepared_{kind}_bundle_revision_drift"
                    )
                continue
            state.bundle_for_revision(revision)
            pending.append((kind, revision, state, retained))

        restored: list[tuple[str, Any, dict[str, str]]] = []
        try:
            for _kind, revision, state, retained in pending:
                state.retain_revision(revision)
                retained[task_id] = revision
                restored.append((revision, state, retained))
        except BaseException:
            for revision, state, retained in reversed(restored):
                retained.pop(task_id, None)
                state.release_revision(revision)
            raise

    def _schedule_agent_run_lease_retry(self, run_id: str) -> None:
        existing = self._agent_run_lease_retry_tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        self._agent_run_lease_retry_errors.pop(run_id, None)
        retry = asyncio.create_task(
            self._observe_agent_run_lease(run_id),
            name=f"agent-run-lease-retry:{run_id}",
        )
        self._agent_run_lease_retry_tasks[run_id] = retry
        retry.add_done_callback(
            lambda handle, identity=run_id: self._finish_agent_run_lease_retry(
                identity, handle
            )
        )

    async def _observe_agent_run_lease(self, run_id: str) -> None:
        terminal = {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
        waiting = {
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentRunStatus.WAITING_FOR_DEPENDENCY,
        }
        while True:
            run = await self.agent_run_repository.get_run(run_id)
            if run is None:
                raise RuntimeError("agent_startup_run_missing")
            if run.status in terminal:
                task = await self.storage.get_task(run.task_id)
                if task is not None:
                    await self._clear_conversation_current_task(
                        task.conversation_id,
                        task.task_id,
                    )
                await self._release_task_skill_revision_if_terminal(run.task_id)
                await self._release_task_mcp_revision_if_terminal(run.task_id)
                return
            if run.status in waiting:
                return
            expires_at = run.lease_expires_at
            if expires_at is not None:
                now = self._utcnow_naive()
                if expires_at.tzinfo is not None:
                    now = now.replace(tzinfo=timezone.utc)
                delay = (expires_at - now).total_seconds()
                if delay > 0:
                    await self._agent_run_lease_retry_sleep(delay)
                    continue
            try:
                await self._recover_agent_run(run)
                return
            except AgentStorageConflict as exc:
                if str(exc) == "agent_task_lease_held":
                    continue
                self._fail_agent_run_recovery_process(run.run_id, exc)
                return
            except Exception as exc:
                self._fail_agent_run_recovery_process(run.run_id, exc)
                return

    def _fail_agent_run_recovery_process(
        self, run_id: str, exc: BaseException
    ) -> None:
        error_type = type(exc).__name__
        self._agent_run_lease_retry_errors[run_id] = error_type
        logger.error(
            "agent_run_lease_retry_failed",
            extra={"run_id": run_id, "error_type": error_type},
        )
        self._agent_run_recovery_fatal_exit(70)

    def _finish_agent_run_lease_retry(
        self, run_id: str, handle: asyncio.Task[None]
    ) -> None:
        if self._agent_run_lease_retry_tasks.get(run_id) is handle:
            self._agent_run_lease_retry_tasks.pop(run_id, None)
        if handle.cancelled():
            return
        error = handle.exception()
        if error is not None:
            self._fail_agent_run_recovery_process(run_id, error)

    async def _cancel_agent_run_lease_retries(self) -> None:
        retry_tasks = getattr(self, "_agent_run_lease_retry_tasks", None)
        if retry_tasks is None:
            return
        retries = tuple(retry_tasks.values())
        retry_tasks.clear()
        for retry in retries:
            retry.cancel()
        if retries:
            await asyncio.gather(*retries, return_exceptions=True)

    async def _strict_enumerate_mcp_terminal_candidates(self) -> None:
        root = self._mcp_terminal_result_root
        if root is None:
            self._mcp_startup_terminal_candidates = ()
            return
        sealed = await asyncio.to_thread(
            enumerate_unconsumed_terminal_result_candidates,
            root,
        )
        if len(sealed) >= TERMINAL_CANDIDATE_WARNING_THRESHOLD:
            if self._audit_sink is not None:
                await self._audit_sink.record(
                    "mcp.terminal_candidate_capacity_warning",
                    {
                        "active_candidate_count": len(sealed),
                        "status": "warning",
                    },
                )
            manager = self._mcp_terminal_candidate_lifecycle_manager
            while (
                manager is not None
                and len(sealed) >= TERMINAL_CANDIDATE_WARNING_THRESHOLD
            ):
                _repaired, archived, _deleted = await manager.run_once(limit=1000)
                if archived == 0:
                    break
                sealed = await asyncio.to_thread(
                    enumerate_unconsumed_terminal_result_candidates,
                    root,
                )
        self._mcp_startup_terminal_candidates = sealed

    async def _reconcile_mcp_terminal_candidates(self) -> None:
        sealed = self._mcp_startup_terminal_candidates
        if sealed is None:
            await self._strict_enumerate_mcp_terminal_candidates()
            sealed = self._mcp_startup_terminal_candidates or ()
        for item in sealed:
            candidate = item.candidate
            aggregate_snapshot_ready = bool(
                self._mcp_terminal_candidate_snapshot_authority is not None
                and (
                    candidate.terminal_state is not MCPTerminalState.COMPLETED
                    or (
                        self._mcp_durable_result_snapshot_authority is not None
                        and candidate.safe_result_ref is not None
                        and candidate.safe_result_size_bytes is not None
                        and candidate.safe_result_content_sha256 is not None
                        and candidate.safe_result_store_kind is not None
                    )
                )
            )
            if aggregate_snapshot_ready:
                candidate_snapshot = (
                    self._mcp_terminal_candidate_snapshot_authority.snapshot(item)
                )
                if candidate.terminal_state is MCPTerminalState.COMPLETED:
                    async with self._mcp_durable_result_snapshot_authority.open_snapshot(
                        result_ref=str(candidate.safe_result_ref),
                        owner_user_id=candidate.owner_user_id,
                        task_id=candidate.task_id,
                        node_id=candidate.node_id,
                        call_id=candidate.call_id,
                        expected_size_bytes=int(candidate.safe_result_size_bytes),
                        expected_content_sha256=str(
                            candidate.safe_result_content_sha256
                        ),
                        expected_store_kind=str(
                            candidate.safe_result_store_kind
                        ),
                    ) as result_snapshot:
                        result = await self.storage.recover_mcp_terminal_candidate(
                            candidate_snapshot,
                            result_snapshot,
                            self._utcnow_naive(),
                        )
                else:
                    result = await self.storage.recover_mcp_terminal_candidate(
                        candidate_snapshot,
                        None,
                        self._utcnow_naive(),
                    )
            else:
                result = await self.storage.commit_authoritative_mcp_terminal_result(
                    candidate.call_id,
                    candidate.candidate_id,
                    self._utcnow_naive(),
                )
            if str(result) == "conflict":
                raise RuntimeError("mcp_terminal_candidate_reconciliation_conflict")
            await self.storage.finish_mcp_remote_task_binding_from_receipt(
                candidate.call_id,
                mcp_terminal_receipt_id(
                    candidate.call_id,
                    candidate.result_payload_sha256,
                ),
                self._utcnow_naive(),
            )
        result_manager = self._mcp_durable_result_lifecycle_manager
        if result_manager is not None:
            await result_manager.reconcile_untracked(limit=1000)

    async def _run_mcp_result_artifact_reconciler_forever(self) -> None:
        manager = self._mcp_durable_result_lifecycle_manager
        if manager is None or self._mcp_result_artifact_projector is None:
            return
        while True:
            try:
                await manager.reconcile_artifacts_and_gc_once(limit=1000)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("mcp_result_artifact_reconciler_cycle_failed")
                if self._audit_sink is not None:
                    try:
                        await self._audit_sink.record(
                            "mcp.result_artifact_reconciler_cycle_failed",
                            {
                                "status": "deferred",
                                "reason_code": "projection_failed",
                            },
                        )
                    except Exception:
                        logger.error(
                            "mcp_result_artifact_projection_observation_failed"
                        )
            await self._mcp_result_artifact_reconciler_sleep(60)

    async def _reconcile_cp7_mcp_authority(self) -> None:
        """Compatibility entry point for focused recovery tests and operators."""

        await self._strict_enumerate_mcp_terminal_candidates()
        await self._reconcile_mcp_terminal_candidates()
        await self._reconcile_mcp_dispatch_recovery()

    async def _reconcile_mcp_dispatch_recovery(self) -> None:
        sealed_by_intent: dict[str, list[MCPValidatedTerminalResultCandidate]] = {}
        for item in self._mcp_startup_terminal_candidates or ():
            sealed_by_intent.setdefault(item.candidate.intent_id, []).append(
                item.candidate
            )

        async for intent in self._iter_mcp_no_server_intents(
            statuses=("armed", "available", "unavailable", "dispatched")
        ):
            if str(intent.status) == "armed":
                await self.storage.resolve_user_mcp_target_intent(
                    intent.intent_id, self._utcnow_naive()
                )
                intent = (
                    await self.storage.get_mcp_no_server_intent(intent.intent_id)
                    or intent
                )
            if str(intent.status) == "unavailable":
                outcome = await self.storage.converge_user_mcp_no_server(
                    intent.task_id, self._utcnow_naive()
                )
                if outcome is MCPNoServerConvergenceResult.TRUSTED_TERMINAL_RESULT_REQUIRES_COMMIT:
                    raise RuntimeError("mcp_terminal_candidate_reconciliation_incomplete")
                if outcome is MCPNoServerConvergenceResult.UNKNOWN_REQUIRES_NO_REPLAY:
                    reconciled_intent = await self.storage.get_mcp_no_server_intent(
                        intent.intent_id
                    )
                    if reconciled_intent is None:
                        raise RuntimeError("mcp_unknown_reconciled_intent_missing")
                    await self._validate_terminal_cp7_mcp_authority(
                        [reconciled_intent]
                    )
                continue
            if str(intent.status) == "available":
                expected_id = mcp_dispatch_resume_outbox_id(intent.intent_id)
                outbox = await self.storage.get_mcp_dispatch_resume_outbox(
                    expected_id
                )
                if outbox is None or outbox.outbox_id != expected_id:
                    raise RuntimeError("mcp_dispatch_resume_outbox_missing")
                envelope = dict(intent.resume_envelope_json or {})
                envelope_sha = canonical_sha256(envelope)
                if (
                    envelope_sha != intent.resume_envelope_sha256
                    or envelope_sha != outbox.resume_envelope_sha256
                ):
                    raise RuntimeError("mcp_dispatch_resume_envelope_digest_mismatch")
                envelope_version = mcp_dispatch_resume_envelope_version(envelope)
                if envelope_version == "v2":
                    validate_mcp_dispatch_resume_envelope_v2(envelope)
                outbox_payload_sha = canonical_sha256(
                    {
                        "intent_id": intent.intent_id,
                        "node_id": intent.node_id,
                        "owner_user_id": intent.owner_user_id,
                        "resume_envelope_sha256": envelope_sha,
                        "server_id": intent.requested_server_id,
                        "task_id": intent.task_id,
                    }
                )
                if outbox_payload_sha != outbox.payload_sha256:
                    raise RuntimeError("mcp_dispatch_resume_outbox_payload_mismatch")
                task = await self.storage.get_task(intent.task_id)
                node = (
                    await self.storage.get_task_node(str(intent.node_id))
                    if intent.node_id is not None
                    else None
                )
                if (
                    task is None
                    or node is None
                    or envelope.get("task_id") != task.task_id
                    or envelope.get("node_id") != intent.node_id
                    or envelope.get("server_id") != intent.requested_server_id
                    or not envelope.get("root_message_id")
                ):
                    raise RuntimeError("mcp_dispatch_resume_envelope_corrupt")
                if (
                    task.status != TaskStatus.RUNNING
                    or task.cancel_requested_at is not None
                    or node.status
                    in {
                        NodeStatus.COMPLETED,
                        NodeStatus.FAILED,
                        NodeStatus.CANCELLED,
                        NodeStatus.ORPHANED,
                        NodeStatus.BLOCKED_BY_CANCELLATION,
                    }
                ):
                    continue
                if node.status == NodeStatus.WAITING_FOR_INPUT:
                    interrupts = await self.storage.list_interrupts_for_task(
                        task.task_id
                    )
                    if any(
                        interrupt.node_id == node.node_id
                        and interrupt.status == InterruptStatus.OPEN
                        for interrupt in interrupts
                    ):
                        continue
                now = self._utcnow_naive()
                if str(outbox.status) == "claimed":
                    if outbox.lease_expires_at is None or outbox.lease_expires_at > now:
                        raise RuntimeError("mcp_dispatch_resume_claim_supervised_elsewhere")
                    outbox = await self.storage.reclaim_mcp_dispatch_resume_outbox(
                        outbox.outbox_id, outbox.revision, now
                    )
                    if outbox is None or str(outbox.status) != "pending":
                        raise RuntimeError("mcp_dispatch_resume_reclaim_lost")
                if str(outbox.status) == "pending":
                    outbox = await self.storage.claim_mcp_dispatch_resume_outbox(
                        outbox.outbox_id,
                        f"startup:{intent.task_id}:{intent.node_id}",
                        uuid4().hex,
                        now,
                        now + timedelta(seconds=30),
                    )
                if outbox is None or str(outbox.status) != "claimed":
                    raise RuntimeError("mcp_dispatch_resume_claim_lost")
                task = await self.storage.get_task(intent.task_id)
                node = (
                    await self.storage.get_task_node(str(intent.node_id))
                    if intent.node_id is not None
                    else None
                )
                if (
                    task is None
                    or node is None
                    or envelope.get("task_id") != task.task_id
                    or envelope.get("node_id") != intent.node_id
                    or envelope.get("server_id") != intent.requested_server_id
                    or not envelope.get("root_message_id")
                ):
                    raise RuntimeError("mcp_dispatch_resume_envelope_corrupt")
                if (
                    task.status != TaskStatus.RUNNING
                    or task.cancel_requested_at is not None
                    or node.status
                    in {
                        NodeStatus.COMPLETED,
                        NodeStatus.FAILED,
                        NodeStatus.CANCELLED,
                        NodeStatus.ORPHANED,
                        NodeStatus.BLOCKED_BY_CANCELLATION,
                    }
                ):
                    continue
                root_message = await self.storage.get_message(task.root_message_id)
                persisted_binding = await self._resolve_persisted_mcp_server_binding(
                    task,
                    node_id=str(intent.node_id or "") or None,
                )
                trusted_binding_metadata = (
                    self._mcp_resolved_binding_runtime_metadata(persisted_binding)
                    if persisted_binding is not None
                    else {}
                )
                user_message = (
                    root_message.content
                    if root_message is not None
                    else task.summary or ""
                )
                envelope_runtime_metadata = (
                    {
                        "mcp_binding_mode": "automatic",
                        "user_message": user_message,
                    }
                    if envelope_version == "v2"
                    else dict(envelope.get("metadata") or {})
                )
                try:
                    locator = await self._agent_locator_for_node(
                        task_id=task.task_id,
                        node_id=str(intent.node_id),
                    )
                    await self._recover_agent_mcp_dispatch(
                        task=task,
                        locator=locator,
                        owner_user_id=intent.owner_user_id,
                        metadata={
                            **self._mcp_task_assignment_metadata(task),
                            **envelope_runtime_metadata,
                            "mcp_dispatch_resume_envelope": envelope,
                            "mcp_dispatch_server_id": intent.requested_server_id,
                            "resume_interrupted_node_id": intent.node_id,
                            **trusted_binding_metadata,
                        },
                    )
                except MCPDispatchResumeEnvelopeError as exc:
                    if exc.code not in {
                        "mcp_dispatch_resume_attachment_unavailable",
                        "mcp_dispatch_resume_dependency_unrecoverable",
                    }:
                        raise
                    finalized = await self.storage.finalize_mcp_dispatch_no_call(
                        intent.intent_id,
                        outbox.outbox_id,
                        str(intent.node_id),
                        "failed",
                        exc.code,
                        self._utcnow_naive(),
                    )
                    if str(finalized) == "conflict":
                        raise RuntimeError(
                            "mcp_dispatch_resume_no_call_finalize_conflict"
                        ) from exc
                    continue
                continue
            if str(intent.status) == "dispatched":
                candidates = sorted(
                    sealed_by_intent.get(intent.intent_id, ()),
                    key=lambda item: item.call_id,
                )
                if candidates:
                    task = await self.storage.get_task(intent.task_id)
                    if task is None:
                        raise RuntimeError("mcp_dispatch_recovery_task_missing")
                    root_message = await self.storage.get_message(task.root_message_id)
                    envelope = dict(intent.resume_envelope_json or {})
                    envelope_sha = canonical_sha256(envelope)
                    envelope_version = mcp_dispatch_resume_envelope_version(envelope)
                    if envelope_version == "v2":
                        validate_mcp_dispatch_resume_envelope_v2(envelope)
                    outbox = await self.storage.get_mcp_dispatch_resume_outbox(
                        mcp_dispatch_resume_outbox_id(intent.intent_id)
                    )
                    if (
                        outbox is None
                        or envelope_sha != intent.resume_envelope_sha256
                        or envelope_sha != outbox.resume_envelope_sha256
                        or canonical_sha256(
                            {
                                "intent_id": intent.intent_id,
                                "node_id": intent.node_id,
                                "owner_user_id": intent.owner_user_id,
                                "resume_envelope_sha256": envelope_sha,
                                "server_id": intent.requested_server_id,
                                "task_id": intent.task_id,
                            }
                        )
                        != outbox.payload_sha256
                    ):
                        raise RuntimeError("mcp_dispatch_resume_authority_digest_mismatch")
                    receipt_ids = [
                        mcp_terminal_receipt_id(
                            candidate.call_id, candidate.result_payload_sha256
                        )
                        for candidate in candidates
                    ]
                    persisted_binding = await self._resolve_persisted_mcp_server_binding(
                        task,
                        node_id=str(intent.node_id or "") or None,
                    )
                    trusted_binding_metadata = (
                        self._mcp_resolved_binding_runtime_metadata(persisted_binding)
                        if persisted_binding is not None
                        else {}
                    )
                    user_message = (
                        root_message.content
                        if root_message is not None
                        else task.summary or ""
                    )
                    envelope_runtime_metadata = (
                        {
                            "mcp_binding_mode": "automatic",
                            "user_message": user_message,
                        }
                        if envelope_version == "v2"
                        else dict(envelope.get("metadata") or {})
                    )
                    locator = await self._agent_locator_for_node(
                        task_id=task.task_id,
                        node_id=str(intent.node_id),
                    )
                    result_refs = [
                        candidate.safe_result_ref for candidate in candidates
                    ]
                    await self._recover_agent_mcp_dispatch(
                        task=task,
                        locator=locator,
                        owner_user_id=intent.owner_user_id,
                        metadata={
                            **self._mcp_task_assignment_metadata(task),
                            **envelope_runtime_metadata,
                            "mcp_dispatch_server_id": intent.requested_server_id,
                            "resume_interrupted_node_id": intent.node_id,
                            **trusted_binding_metadata,
                        },
                        authoritative_payload={
                            "mcp_status": "completed",
                            "result_receipt_ids": receipt_ids,
                            "safe_result_refs": result_refs,
                        },
                    )
                    continue
                outcome = await self.storage.converge_user_mcp_no_server(
                    intent.task_id, self._utcnow_naive()
                )
                if outcome is MCPNoServerConvergenceResult.TRUSTED_TERMINAL_RESULT_REQUIRES_COMMIT:
                    raise RuntimeError("mcp_terminal_candidate_reconciliation_incomplete")
                if outcome is MCPNoServerConvergenceResult.UNKNOWN_REQUIRES_NO_REPLAY:
                    reconciled_intent = await self.storage.get_mcp_no_server_intent(
                        intent.intent_id
                    )
                    if reconciled_intent is None:
                        raise RuntimeError("mcp_unknown_reconciled_intent_missing")
                    await self._validate_terminal_cp7_mcp_authority(
                        [reconciled_intent]
                    )

        binding = self._mcp_legacy_retirement_binding
        if binding is not None:
            inventory_id, inventory_sha256 = binding
            for task_id in await self.storage.list_mcp_legacy_retirement_task_ids(
                inventory_id, inventory_sha256
            ):
                await self.storage.converge_legacy_runtime_retirement(
                    task_id,
                    inventory_id,
                    inventory_sha256,
                    f"legacy-retire:v1:{task_id}:{inventory_sha256}",
                    self._utcnow_naive(),
                )

    async def _validate_terminal_cp7_mcp_authority(self, intents: list[Any]) -> None:
        for intent in intents:
            status = str(intent.status)
            if status not in {"unknown", "converged", "resolved"}:
                continue
            task = await self.storage.get_task(intent.task_id)
            node = (
                await self.storage.get_task_node(intent.node_id)
                if intent.node_id is not None
                else None
            )
            if task is None or intent.terminal_at is None:
                raise RuntimeError("mcp_terminal_intent_authority_incomplete")
            events = {
                event.event_id: event
                for event in await self.storage.list_events_for_task(intent.task_id)
            }
            outbox = await self.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent.intent_id)
            )
            if status == "unknown":
                calls = await self.storage.list_mcp_call_records(
                    intent.owner_user_id, intent.task_id
                )
                projections = [
                    await self.storage.get_mcp_execution_terminal_projection(
                        call.call_ref
                    )
                    for call in calls
                    if call.may_have_dispatched
                ]
                authoritative = [projection for projection in projections if projection is not None]
                projection = authoritative[0] if len(authoritative) == 1 else None
                expected_terminal_event_type = (
                    "task.cancelled"
                    if projection is not None
                    and projection.task_terminal_status == "cancelled"
                    else "task.failed"
                )
                if (
                    projection is None
                    or str(task.status) != projection.task_terminal_status
                    or node is None
                    or str(node.status) != projection.node_terminal_status
                    or not projections
                    or any(
                        projection is None
                        or not projection.no_replay
                        or str(projection.status) not in {"unknown", "late_result_resolved"}
                        for projection in projections
                    )
                    or projection.unknown_event_id not in events
                    or projection.task_failed_event_id not in events
                    or events[projection.unknown_event_id].event_type
                    != "mcp.execution_status_unknown"
                    or events[projection.task_failed_event_id].event_type
                    != expected_terminal_event_type
                    or outbox is None
                    or str(outbox.status) != "completed"
                    or outbox.completion_mode != "unknown_no_replay"
                    or outbox.result_receipt_id is not None
                ):
                    raise RuntimeError("mcp_unknown_authority_incomplete")
                if self.user_mcp_audit_service is not None:
                    await self.user_mcp_audit_service.record(
                        owner_user_id=intent.owner_user_id,
                        event_type="mcp.authority_terminal_reconciled",
                        occurred_at=projection.unknown_terminal_at,
                        task_id=intent.task_id,
                        node_id=intent.node_id,
                        safe_payload={
                            "status": "unknown",
                            "reason": "terminal_task_authority_reconciled",
                            "error_code": "execution_status_unknown",
                        },
                        source_ref=(
                            "mcp-authority-terminal-reconciled:"
                            f"{projection.projection_id}"
                        ),
                    )
            elif status == "resolved":
                calls = await self.storage.list_mcp_call_records(
                    intent.owner_user_id, intent.task_id
                )
                receipts = [
                    await self.storage.get_mcp_terminal_result_receipt_for_call(call.call_ref)
                    for call in calls if call.may_have_dispatched
                ]
                no_call = bool(
                    outbox is not None
                    and (
                        str(outbox.status) == "aborted"
                        or (
                            str(outbox.status) == "completed"
                            and outbox.completion_mode == "completed"
                            and not any(
                                call.may_have_dispatched for call in calls
                            )
                        )
                    )
                )
                if no_call:
                    expected_node_status = {
                        "completed": NodeStatus.COMPLETED,
                        "stopped_no_call": NodeStatus.COMPLETED,
                        "failed_no_call": NodeStatus.FAILED,
                        "cancelled_no_call": NodeStatus.CANCELLED,
                    }.get(outbox.completion_mode)
                    no_call_events = [
                        event for event in events.values()
                        if (
                            event.event_type == "mcp.dispatch_no_call"
                            and event.event_id.startswith(
                                f"mcp-dispatch-no-call:v1:{intent.intent_id}:"
                            )
                            and event.payload.get("intent_id") == intent.intent_id
                        )
                        or (
                            event.event_type == "mcp.dispatch_finalized"
                            and event.event_id.startswith(
                                f"mcp-dispatch-finalized:v1:{intent.intent_id}:"
                            )
                            and event.payload.get("completion_mode")
                            == outbox.completion_mode
                        )
                    ]
                    if (
                        outbox.completion_mode
                        not in {
                            "completed",
                            "stopped_no_call",
                            "failed_no_call",
                            "cancelled_no_call",
                        }
                        or outbox.result_receipt_id is not None
                        or len(no_call_events) != 1
                        or any(call.may_have_dispatched for call in calls)
                        or node is None
                        or node.status != expected_node_status
                    ):
                        raise RuntimeError("mcp_resolved_no_call_authority_incomplete")
                elif (
                    node is None
                    or node.status not in {NodeStatus.COMPLETED, NodeStatus.FAILED}
                    or outbox is None
                    or str(outbox.status) != "completed"
                    or outbox.completion_mode
                    not in {
                        "completed",
                        "stopped_after_call",
                        "failed_after_call",
                        "cancelled_after_call",
                    }
                    or outbox.result_receipt_id is None
                    or not receipts
                    or any(receipt is None for receipt in receipts)
                    or outbox.result_receipt_id not in {
                        receipt.result_receipt_id for receipt in receipts if receipt is not None
                    }
                ):
                    raise RuntimeError("mcp_resolved_authority_incomplete")
            else:
                receipt = await self.storage.get_mcp_no_server_convergence_receipt(
                    intent.task_id
                )
                if (
                    task.status != TaskStatus.FAILED
                    or receipt is None
                    or receipt.intent_id != intent.intent_id
                    or receipt.runtime_unavailable_event_id not in events
                    or receipt.task_failed_event_id not in events
                    or events[receipt.runtime_unavailable_event_id].event_type
                    != "mcp.runtime_unavailable"
                    or events[receipt.task_failed_event_id].event_type != "task.failed"
                    or (outbox is not None and (
                        str(outbox.status) != "aborted"
                        or outbox.completion_mode != "failed_no_call"
                        or outbox.result_receipt_id is not None
                    ))
                ):
                    raise RuntimeError("mcp_converged_authority_incomplete")

    async def complete_cp7_safety_minute(
        self, started_at: datetime, ended_at: datetime
    ) -> None:
        if self._mcp_cp7_safety_facade is None:
            raise RuntimeError("mcp_cp7_safety_not_configured")
        try:
            await self._mcp_cp7_safety_facade.complete_minute(started_at, ended_at)
        except CP7SafetyFatalPersistenceError:
            self._mcp_cp7_fatal_exit(70)
            raise

    async def mark_cp7_ready(self, evidence: CP7BoundaryEvidence) -> None:
        if self._mcp_cp7_safety_facade is None:
            raise RuntimeError("mcp_cp7_safety_not_configured")
        try:
            await self._mcp_cp7_safety_facade.mark_ready(evidence)
        except CP7SafetyFatalPersistenceError:
            self._mcp_cp7_fatal_exit(70)
            raise

    async def _run_cp7_safety_minutes(self) -> None:
        facade = self._mcp_cp7_safety_facade
        provider = self._mcp_cp7_boundary_provider
        if facade is None or provider is None:
            return
        opened_at = self._mcp_cp7_open_boundary.boundary_at
        started_at = opened_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if started_at < opened_at:
            started_at += timedelta(minutes=1)
        try:
            while True:
                ended_at = started_at + timedelta(minutes=1)
                delay = max(0.0, (ended_at - datetime.now(timezone.utc)).total_seconds())
                await asyncio.sleep(delay)
                for probe in self._mcp_cp7_safety_probes:
                    probe(started_at, ended_at)
                await facade.complete_minute(started_at, ended_at)
                if not facade.ready:
                    evidence = provider()
                    if inspect.isawaitable(evidence):
                        evidence = await evidence
                    if not isinstance(evidence, CP7BoundaryEvidence):
                        raise RuntimeError("mcp_cp7_boundary_evidence_invalid")
                    await facade.mark_ready(evidence)
                started_at = ended_at
        except asyncio.CancelledError:
            raise
        except BaseException:
            try:
                await facade.record_unplanned_process_exit(
                    datetime.now(timezone.utc)
                )
            except BaseException:
                pass
            self._mcp_cp7_fatal_exit(70)
            raise

    def _handle_cp7_safety_minute_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if (
            error is not None
            and self._mcp_cp7_safety_facade is not None
            and self._mcp_cp7_safety_facade.ready
        ):
            self._mcp_cp7_fatal_exit(70)

    def _handle_mcp_post_ready_recovery_exit(
        self, task: asyncio.Task[None]
    ) -> None:
        if task.cancelled():
            return
        try:
            self._mcp_post_ready_recovery_error = task.exception()
        except asyncio.CancelledError:
            return

    def _handle_mcp_result_artifact_reconciler_exit(
        self, task: asyncio.Task[None]
    ) -> None:
        if task.cancelled():
            return
        try:
            self._mcp_result_artifact_reconciler_error = task.exception()
        except asyncio.CancelledError:
            return

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
            existing = self._conversation_delete_tasks.get(
                conversation.conversation_id
            )
            if existing is not None and not existing.done():
                await asyncio.shield(existing)
                continue
            runner_id = conversation.delete_runner_id or self._make_id("delete")
            task = self._start_conversation_delete_task(
                conversation,
                runner_id,
                name=f"delete-conversation-recovery:{conversation.conversation_id}",
            )
            await asyncio.shield(task)

    async def _recover_user_mcp_calls(self) -> None:
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
                            "safe_call_ref": self._mcp_audit_reference_signer.safe_reference(
                                call.call_ref,
                                context="mcp-call-reference-v1",
                            ),
                            "status": "unknown",
                            "error_code": "execution_status_unknown",
                        },
                    )
                )
            if len(converged) < 1000:
                return

    async def _converge_inactive_and_unknown_mcp_dispatches(self) -> None:
        async for intent in self._iter_mcp_no_server_intents(
            statuses=("available", "dispatched")
        ):
            task = await self.storage.get_task(intent.task_id)
            if task is None:
                raise RuntimeError("mcp_startup_dispatch_task_missing")
            if (
                task.status == TaskStatus.RUNNING
                and task.cancel_requested_at is None
            ):
                continue
            if intent.node_id is None:
                raise RuntimeError("mcp_startup_dispatch_node_missing")
            result = await self.storage.converge_inactive_mcp_dispatch(
                intent.intent_id,
                mcp_dispatch_resume_outbox_id(intent.intent_id),
                intent.node_id,
                self._utcnow_naive(),
            )
            if str(result) == "conflict":
                raise RuntimeError("mcp_startup_inactive_dispatch_conflict")
        await self._recover_user_mcp_calls()

    async def _reconcile_mcp_remote_bindings(self) -> None:
        await self.storage.reconcile_unpublished_mcp_remote_task_bindings(
            now=self._utcnow_naive(),
            limit=1000,
        )

    async def _iter_mcp_no_server_intents(
        self, *, statuses: tuple[str, ...]
    ) -> AsyncIterator[Any]:
        after_updated_at: datetime | None = None
        after_intent_id: str | None = None
        while True:
            page = await self.storage.list_mcp_no_server_intents(
                statuses=statuses,
                after_updated_at=after_updated_at,
                after_intent_id=after_intent_id,
                limit=1000,
            )
            for intent in page:
                yield intent
            if len(page) < 1000:
                return
            last = page[-1]
            after_updated_at = last.updated_at
            after_intent_id = last.intent_id

    async def _iter_mcp_dispatch_resume_outboxes(
        self, *, statuses: tuple[str, ...]
    ) -> AsyncIterator[Any]:
        after_updated_at: datetime | None = None
        after_outbox_id: str | None = None
        while True:
            page = await self.storage.list_mcp_dispatch_resume_outboxes(
                statuses=statuses,
                after_updated_at=after_updated_at,
                after_outbox_id=after_outbox_id,
                limit=1000,
            )
            for outbox in page:
                yield outbox
            if len(page) < 1000:
                return
            last = page[-1]
            after_updated_at = last.updated_at
            after_outbox_id = last.outbox_id

    async def _validate_mcp_mrtr_recovery_evidence(self) -> None:
        async for outbox in self._iter_mcp_dispatch_resume_outboxes(
            statuses=("waiting_input",)
        ):
            task = await self.storage.get_task(outbox.task_id)
            node = await self.storage.get_task_node(outbox.node_id)
            if (
                task is None
                or node is None
                or task.status != TaskStatus.RUNNING
                or task.cancel_requested_at is not None
            ):
                continue
            interrupts = [
                item
                for item in await self.storage.list_interrupts_for_task(
                    outbox.task_id
                )
                if item.node_id == outbox.node_id
                and item.reason_code == "mcp_input_required"
                and item.status == InterruptStatus.OPEN
            ]
            if len(interrupts) != 1:
                raise RuntimeError("mcp_startup_mrtr_interrupt_authority_invalid")
            sealed_ref = str(
                interrupts[0].required_fields.get("sealed_request_state_ref") or ""
            )
            sealed = (
                None
                if not sealed_ref
                else await self.storage.get_mcp_sealed_state(
                    outbox.owner_user_id,
                    outbox.task_id,
                    sealed_ref,
                )
            )
            if (
                sealed is None
                or sealed.task_id != outbox.task_id
                or sealed.node_id != outbox.node_id
                or sealed.owner_user_id != outbox.owner_user_id
                or node.status != NodeStatus.WAITING_FOR_INPUT
            ):
                raise RuntimeError("mcp_startup_mrtr_evidence_authority_invalid")

    async def _validate_mcp_pending_action_recovery_evidence(self) -> None:
        async for outbox in self._iter_mcp_dispatch_resume_outboxes(
            statuses=("waiting_approval",)
        ):
            task = await self.storage.get_task(outbox.task_id)
            node = await self.storage.get_task_node(outbox.node_id)
            if (
                task is None
                or node is None
                or task.status != TaskStatus.RUNNING
                or task.cancel_requested_at is not None
            ):
                continue
            interrupts = [
                item
                for item in await self.storage.list_interrupts_for_task(
                    outbox.task_id
                )
                if item.node_id == outbox.node_id
                and item.reason_code == "mcp_tool_approval_required"
                and item.status == InterruptStatus.OPEN
            ]
            action = (
                None
                if len(interrupts) != 1
                else await self.storage.get_mcp_pending_tool_action_for_interrupt(
                    interrupts[0].interrupt_id
                )
            )
            if (
                len(interrupts) != 1
                or action is None
                or str(action.status) != "waiting_approval"
                or action.owner_user_id != outbox.owner_user_id
                or action.task_id != outbox.task_id
                or action.node_id != outbox.node_id
                or action.server_id != outbox.server_id
                or node.status != NodeStatus.WAITING_FOR_INPUT
            ):
                raise RuntimeError(
                    "mcp_startup_pending_action_authority_invalid"
                )

    async def _validate_mcp_resume_envelope_authority(self) -> None:
        async for intent in self._iter_mcp_no_server_intents(
            statuses=("armed",)
        ):
            await self.storage.resolve_user_mcp_target_intent(
                intent.intent_id,
                self._utcnow_naive(),
            )
        async for intent in self._iter_mcp_no_server_intents(
            statuses=("available",)
        ):
            outbox = await self.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent.intent_id)
            )
            envelope = dict(intent.resume_envelope_json or {})
            digest = canonical_sha256(envelope)
            if (
                outbox is None
                or outbox.outbox_id != mcp_dispatch_resume_outbox_id(
                    intent.intent_id
                )
                or digest != intent.resume_envelope_sha256
                or digest != outbox.resume_envelope_sha256
                or canonical_sha256(
                    {
                        "intent_id": intent.intent_id,
                        "node_id": intent.node_id,
                        "owner_user_id": intent.owner_user_id,
                        "resume_envelope_sha256": digest,
                        "server_id": intent.requested_server_id,
                        "task_id": intent.task_id,
                    }
                )
                != outbox.payload_sha256
            ):
                raise RuntimeError("mcp_startup_resume_envelope_authority_invalid")
            if mcp_dispatch_resume_envelope_version(envelope) == "v2":
                validate_mcp_dispatch_resume_envelope_v2(envelope)

    async def _recover_expired_mcp_dispatch_claims(self) -> None:
        now = self._utcnow_naive()
        async for outbox in self._iter_mcp_dispatch_resume_outboxes(
            statuses=("claimed", "active")
        ):
            if (
                str(outbox.status) not in {"claimed", "active"}
                or outbox.lease_expires_at is None
                or outbox.lease_expires_at > now
            ):
                continue
            await self.storage.release_or_recover_mcp_dispatch_claim(
                outbox.outbox_id,
                outbox.revision,
                now,
            )

    async def _validate_mcp_aggregate_invariants(self) -> None:
        async for intent in self._iter_mcp_no_server_intents(
            statuses=("unknown", "converged", "resolved")
        ):
            await self._validate_terminal_cp7_mcp_authority([intent])
        async for outbox in self._iter_mcp_dispatch_resume_outboxes(
            statuses=(
                "pending",
                "claimed",
                "active",
                "waiting_approval",
                "waiting_input",
                "remote_pending",
            )
        ):
            task = await self.storage.get_task(outbox.task_id)
            if task is None:
                raise RuntimeError("mcp_startup_dispatch_task_missing")
            if (
                task.status != TaskStatus.RUNNING
                or task.cancel_requested_at is not None
            ):
                raise RuntimeError("mcp_startup_inactive_dispatch_not_converged")
            claimed = str(outbox.status) in {"claimed", "active"}
            claim_fields_present = bool(
                outbox.claim_owner
                and outbox.claim_token
                and outbox.lease_expires_at is not None
            )
            if claimed != claim_fields_present:
                raise RuntimeError("mcp_startup_dispatch_claim_shape_invalid")
        await self._validate_mcp_mrtr_recovery_evidence()
        await self._validate_mcp_pending_action_recovery_evidence()

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
                        "safe_call_ref": self._mcp_audit_reference_signer.safe_reference(
                            call.call_ref,
                            context="mcp-call-reference-v1",
                        ),
                        "metric_family": metric_family,
                        "gap_reason": "metric_recording_failed",
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        except Exception:
            return

    async def shutdown(self) -> None:
        coordinator = getattr(self, "_submission_admission_coordinator", None)
        try:
            if coordinator is not None:
                await coordinator.abort_pending()
        finally:
            await self._cancel_agent_run_lease_retries()
        await self._quiesce_cp7_for_shutdown()
        if self._mcp_result_artifact_reconciler_task is not None:
            if not self._mcp_result_artifact_reconciler_task.done():
                self._mcp_result_artifact_reconciler_task.cancel()
            await asyncio.gather(
                self._mcp_result_artifact_reconciler_task,
                return_exceptions=True,
            )
            self._mcp_result_artifact_reconciler_task = None
        if self._mcp_post_ready_recovery_task is not None:
            if not self._mcp_post_ready_recovery_task.done():
                self._mcp_post_ready_recovery_task.cancel()
            await asyncio.gather(
                self._mcp_post_ready_recovery_task,
                return_exceptions=True,
            )
            self._mcp_post_ready_recovery_task = None
        if self._mcp_cp7_minute_task is not None:
            self._mcp_cp7_minute_task.cancel()
            await asyncio.gather(self._mcp_cp7_minute_task, return_exceptions=True)
            self._mcp_cp7_minute_task = None
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
        await self._close_cp7_safety()
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
        await self._main_agent_llm_runtime.aclose()
        if self._mcp_rollout_engine is not None:
            await asyncio.to_thread(self._mcp_rollout_engine.dispose)
        self._engine.dispose()

    async def _quiesce_cp7_for_shutdown(self) -> None:
        facade = self._mcp_cp7_safety_facade
        authorization = self._mcp_cp7_maintenance_authorization
        authorizer = self._mcp_cp7_maintenance_authorizer
        authorized = bool(
            facade is not None
            and facade.ready
            and authorization is not None
            and authorizer is not None
            and authorizer(authorization)
        )
        if authorized:
            now = self._mcp_cp7_clock().astimezone(timezone.utc)
            boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            await self._mcp_cp7_sleep(max(0.0, (boundary - now).total_seconds()))
            await asyncio.sleep(0)
        self._mcp_cp7_requests_stopped = True

    async def _close_cp7_safety(self) -> None:
        facade = self._mcp_cp7_safety_facade
        provider = self._mcp_cp7_boundary_provider
        if facade is None or not facade.ready or provider is None:
            return
        evidence = provider()
        if inspect.isawaitable(evidence):
            evidence = await evidence
        if not isinstance(evidence, CP7BoundaryEvidence):
            raise RuntimeError("mcp_cp7_boundary_evidence_invalid")
        authorization = self._mcp_cp7_maintenance_authorization
        authorizer = self._mcp_cp7_maintenance_authorizer
        authorized = bool(
            authorization is not None
            and authorizer is not None
            and authorizer(authorization)
        )
        if authorized:
            try:
                await facade.begin_verifier_maintenance(
                    evidence,
                    verifier_authorized=True,
                    requests_stopped=self._mcp_cp7_requests_stopped,
                )
            except CP7SafetyFatalPersistenceError:
                self._mcp_cp7_fatal_exit(70)
                raise
            return
        try:
            await facade.record_unplanned_process_exit(evidence.boundary_at)
        except CP7SafetyFatalPersistenceError:
            self._mcp_cp7_fatal_exit(70)
            raise
        except Exception:
            pass

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
            return None
        return capability_id

    def _ensure_supported_capability(self, capability_id: str | None) -> None:
        if capability_id is None:
            return
        descriptor = self.capability_registry.get(capability_id)
        if descriptor is not None and descriptor.public:
            return
        raise ValueError(f"Unsupported capability_id: {capability_id}")

    def _build_skill_hint_activation(
        self,
        capability_id: str | None,
    ) -> dict[str, str]:
        state = self._skill_runtime_state
        if state is None or capability_id is None:
            raise SkillHintUnavailableError()
        try:
            bundle = state.active_bundle
            descriptor = bundle.skill_capabilities.descriptors_by_id.get(
                capability_id
            )
            if (
                descriptor is None
                or not descriptor.public
                or not descriptor.enabled
                or descriptor.kind != "skill"
                or not capability_id.startswith("skill.")
            ):
                raise SkillHintUnavailableError()
            skill_name = bundle.skill_capabilities.skill_name_by_capability_id.get(
                capability_id
            )
            manifest = next(
                (
                    skill
                    for skill in bundle.catalog.skills
                    if skill.name == skill_name
                ),
                None,
            )
            if manifest is None:
                raise SkillHintUnavailableError()
            profile = build_public_skill_profile(
                manifest,
                capability_id=capability_id,
                descriptor=descriptor,
            )
            activation = build_canonical_skill_activation(
                binding_mode="hint",
                profile=profile,
                pinned_bundle_revision=bundle.revision,
                resolved_bundle_revision=bundle.revision,
            )
        except SkillHintUnavailableError:
            raise
        except Exception as exc:
            raise SkillHintUnavailableError() from exc
        return {
            "payload": activation.payload_json,
            "payload_sha256": activation.payload_sha256,
        }

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

    def _retain_task_skill_revision(self, request: AgentExecutionRequest) -> None:
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

    def _retain_task_mcp_revision(self, request: AgentExecutionRequest) -> None:
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
        try:
            await asyncio.wait_for(
                asyncio.shield(handle),
                timeout=self._execution_wait_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        async with self._lock:
            if self._running_tasks.get(task_id) is handle and handle.done():
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


_LEGACY_MASTER_KEY_AUTHORITY_ENV_KEYS = (
    "MCP_CREDENTIAL_KEY_FILE_HOST",
    "MCP_CREDENTIAL_KEY_FILE",
    "MAF_AUTH_TOKEN_HASH_SECRET",
    "MAF_AUTH_TOKEN_HASH_SECRET_REQUIRED",
)


def _resolve_master_key_deriver(
    *,
    master_key_file: str | Path | None,
    master_key_bytes: bytes | None,
    env: Mapping[str, str],
) -> MasterKeyDeriver:
    if any(key in env for key in _LEGACY_MASTER_KEY_AUTHORITY_ENV_KEYS):
        raise MasterKeyError("maf_master_key_legacy_authority_configured")
    if master_key_file is not None and master_key_bytes is not None:
        raise ValueError("master_key_file and master_key_bytes are mutually exclusive")
    if master_key_bytes is not None:
        return MasterKeyDeriver.from_bytes(master_key_bytes)
    resolved_path = master_key_file
    if resolved_path is None:
        raw_path = env.get("MAF_MASTER_KEY_FILE", "")
        resolved_path = raw_path if raw_path else None
    if resolved_path is None:
        raise MasterKeyError("maf_master_key_file_missing")
    return MasterKeyDeriver.from_file(resolved_path)


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
    project_skill_bundle_digest: str | None = None,
    mcp_config: Mapping[str, Any] | None = None,
    mcp_client_factory: Callable[..., Any] | None = None,
    mcp_sidecar_client: Any | None = None,
    mcp_runtime_state: MCPRuntimeState | None = None,
    master_key_file: str | Path | None = None,
    master_key_bytes: bytes | None = None,
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
    user_mcp_terminal_result_store_path: str | Path | None = None,
    mcp_legacy_retirement_inventory_id: str | None = None,
    mcp_legacy_retirement_inventory_sha256: str | None = None,
    mcp_cp7_runtime_identity: CP7RuntimeIdentity | None = None,
    mcp_cp7_open_boundary: CP7BoundaryEvidence | None = None,
    mcp_cp7_boundary_provider: Callable[[], Any] | None = None,
    mcp_cp7_fatal_exit: Callable[[int], None] = os._exit,
    mcp_cp7_predecessor_close: CP7PredecessorClose | None = None,
    mcp_cp7_verifier_authorized: bool = False,
    mcp_cp7_maintenance_authorization: object | None = None,
    mcp_cp7_maintenance_authorizer: Callable[[object], bool] | None = None,
) -> ApiRuntime:
    master_key_deriver = _resolve_master_key_deriver(
        master_key_file=master_key_file,
        master_key_bytes=master_key_bytes,
        env=os.environ,
    )
    mcp_credential_cipher = MCPCredentialCipher(
        master_key_deriver.derive(MasterKeyDomain.MCP_CREDENTIAL)
    )
    mcp_recovery_cipher = MCPRecoveryCipher(
        master_key_deriver.derive(MasterKeyDomain.MCP_RECOVERY)
    )
    mcp_request_state_evidence_authority = MCPRequestStateEvidenceAuthority(
        mcp_recovery_cipher
    )
    auth_token_hasher = AuthTokenHasher(
        master_key_deriver.derive(MasterKeyDomain.AUTH_TOKEN)
    )
    mcp_audit_reference_signer = MCPAuditReferenceSigner(
        master_key_deriver.derive(MasterKeyDomain.MCP_AUDIT_REFERENCE)
    )
    master_key_sentinel_cipher = MasterKeySentinelCipher(
        master_key_deriver.derive(MasterKeyDomain.KEY_VALIDATION)
    )
    _bootstrap_runtime_config_env(
        platform_llm_text_generator=platform_llm_text_generator,
        platform_llm_config=platform_llm_config,
        platform_llm_config_path=platform_llm_config_path,
        platform_llm_client_factory=platform_llm_client_factory,
        enable_platform_llm=enable_platform_llm,
        main_agent_stream_generator=main_agent_stream_generator,
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
        enable_conversation_title_llm=enable_conversation_title_llm,
        enable_conversation_memory=enable_conversation_memory,
    )

    deployment_env = (
        os.environ.get("MAF_API_ENV")
        or os.environ.get("MAF_ENV")
        or os.environ.get("APP_ENV")
        or ""
    ).strip().lower()
    canonical_mcp_rollout_configured = mcp_rollout_env_is_configured(os.environ)
    mcp_rollout_config, user_mcp_enabled = _resolve_user_mcp_rollout_config(
        enable_user_mcp=enable_user_mcp,
        enable_user_mcp_routing=enable_user_mcp_routing,
        env=os.environ,
    )
    user_mcp_routing_enabled = user_mcp_enabled and (
        mcp_rollout_config.routing_mode in {MCPRoutingMode.SHADOW, MCPRoutingMode.ENFORCE}
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
    expected_submission_authority_receipt_sha256: str | None = None
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
        migration_evidence = load_runtime_sidecar_migration_evidence_artifact(
            Path(evidence_path_value),
            authentication_key_path=Path(key_path_value),
        )
        expected_submission_authority_receipt_sha256 = migration_evidence[
            "finalization_receipt_sha256"
        ]
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
    if canonical_task_authority_mode == "enforce":
        required_agent_methods = (
            "commit_agent_state",
            "get_agent_run",
            "get_agent_run_for_task",
            "list_agent_runs",
            "list_agent_items",
        )
        if not all(
            callable(getattr(resolved_runtime_sidecar_client, method, None))
            for method in required_agent_methods
        ):
            raise RuntimeError(
                "agent_runtime_store_unavailable: enforce mode requires Runtime Sidecar Agent authority"
            )
        required_submission_methods = (
            "admit_submission",
            "claim_pending_submission",
            "renew_submission_claim",
            "acknowledge_submission_projection",
            "prepare_submission_handoff",
            "get_submission_preparation",
            "acknowledge_submission_handoff",
            "close_conversation_admission",
            "reserve_message_identity",
        )
        if (
            "submission_admission"
            not in load_runtime_sidecar_contract()["supported_features"]
            or not all(
                callable(getattr(resolved_runtime_sidecar_client, method, None))
                for method in required_submission_methods
            )
        ):
            raise RuntimeError(
                "submission_runtime_store_unavailable: enforce mode requires "
                "Runtime Sidecar submission admission authority"
            )
    audit_sink = JsonlAuditSink(audit_log_path)
    skill_assembly_injected = skill_roots is not None or public_skill_roots is not None
    roots = tuple(skill_roots) if skill_roots is not None else _default_skill_roots()
    resolved_public_skill_roots = (
        tuple(public_skill_roots) if public_skill_roots is not None else roots[:1]
    )
    _validate_project_skill_bundle_startup(
        public_skill_roots=resolved_public_skill_roots,
        expected_digest=project_skill_bundle_digest,
        skill_assembly_injected=skill_assembly_injected,
        audit_sink=audit_sink,
        env=os.environ,
    )
    auth_generation_cache = AuthGenerationCache()
    auth_invalidation_bus = InMemoryAuthInvalidationBus()
    postgres_auth_invalidation_bus = None
    mcp_rollout_engine: Engine | None = None
    terminal_result_root = (
        Path(user_mcp_terminal_result_store_path)
        if user_mcp_terminal_result_store_path is not None
        else Path(database_path).parent / "user_mcp_terminal_results"
    )
    if user_mcp_enabled and not terminal_result_root.exists():
        terminal_result_root.mkdir(mode=0o700)
    user_mcp_result_store: MCPTemporaryResultStore | None = None
    mcp_durable_result_snapshot_authority: MCPDurableResultSnapshotAuthority | None = None
    mcp_terminal_candidate_snapshot_authority: (
        MCPTerminalCandidateSnapshotAuthority | None
    ) = None
    mcp_terminal_candidate_lifecycle_manager: (
        MCPTerminalCandidateLifecycleManager | None
    ) = None
    mcp_durable_result_lifecycle_manager: (
        MCPDurableResultLifecycleManager | None
    ) = None
    mcp_result_artifact_projector: MCPResultArtifactProjector | None = None
    mcp_pending_action_payload_store: MCPPendingActionPayloadStore | None = None
    mcp_result_service: MCPIsolatedResultService | None = None
    mcp_projection_store: MCPProjectionStore | None = None
    result_root: Path | None = None
    if user_mcp_enabled:
        assert user_mcp_capacity_values is not None
        result_root = Path(
            os.environ.get("MAF_USER_MCP_TEMPORARY_RESULT_ROOT")
            or (Path(database_path).parent / "user_mcp_results")
        )
        user_mcp_result_store = MCPTemporaryResultStore(
            result_root,
            memory_threshold_bytes=_positive_required_env_int(
                "MAF_USER_MCP_MEMORY_RESULT_THRESHOLD_BYTES",
                allow_default=1024 * 1024,
            ),
        )
        projection_root = Path(
            os.environ.get("MAF_USER_MCP_RESULT_PROJECTION_ROOT")
            or (Path(database_path).parent / "user_mcp_result_projections")
        )
        mcp_projection_store = MCPProjectionStore(projection_root)
        mcp_result_service = MCPIsolatedResultService(
            projection_store=mcp_projection_store
        )
        mcp_durable_result_snapshot_authority = MCPDurableResultSnapshotAuthority(
            user_mcp_result_store
        )
        mcp_terminal_candidate_snapshot_authority = (
            MCPTerminalCandidateSnapshotAuthority(terminal_result_root)
        )
        pending_action_root = Path(
            os.environ.get("MAF_USER_MCP_PENDING_ACTION_ROOT")
            or (Path(database_path).parent / "user_mcp_pending_actions")
        )

        def pending_action_disk_available(root: Path) -> bool:
            values = os.statvfs(root)
            free_bytes = values.f_bavail * values.f_frsize
            return free_bytes >= (
                user_mcp_capacity_values[1]
                + MAX_PENDING_ACTION_ARGUMENT_BYTES
                + 64 * 1024
            )

        mcp_pending_action_payload_store = MCPPendingActionPayloadStore(
            pending_action_root,
            cipher=MCPPendingActionPayloadCipher(
                master_key_deriver.derive(MasterKeyDomain.MCP_RECOVERY)
            ),
            disk_available=pending_action_disk_available,
        )

    def read_terminal_candidate(call_id: str, candidate_id: str):
        sealed = secure_read_terminal_result_candidate_active_or_archive(
            terminal_result_root, candidate_id
        )
        if sealed.candidate.call_id != call_id:
            raise RuntimeError("mcp_terminal_candidate_call_binding_conflict")
        return sealed.candidate

    def resolve_terminal_candidate(call_id: str):
        matches = [
            item.candidate
            for item in enumerate_unconsumed_terminal_result_candidates(
                terminal_result_root
            )
            if item.candidate.call_id == call_id
        ]
        if len(matches) > 1:
            raise RuntimeError("mcp_terminal_candidate_call_fork")
        return matches[0] if matches else None

    if state_config.backend == StatePlatformBackend.POSTGRESQL:
        engine = create_postgres_engine(state_config.dsn or "")
        bootstrap_postgres_database(engine)
        primary_session_factory = create_postgres_session_factory(engine)
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
            primary_session_factory,
            message_identity_authority_enabled=(
                canonical_task_authority_mode == "enforce"
            ),
            runtime_sidecar_client=resolved_runtime_sidecar_client,
            runtime_sidecar_shadow_sink=_build_runtime_sidecar_shadow_diff_sink(audit_sink),
            mcp_task_authority_mode=canonical_task_authority_mode,
            mcp_terminal_candidate_reader=read_terminal_candidate,
            mcp_terminal_candidate_resolver=resolve_terminal_candidate,
            mcp_pending_action_payload_reader=mcp_pending_action_payload_store,
            mcp_terminal_candidate_snapshot_reader=(
                mcp_terminal_candidate_snapshot_authority
            ),
            mcp_durable_result_snapshot_reader=(
                mcp_durable_result_snapshot_authority
            ),
            mcp_mrtr_request_state_evidence_reader=(
                mcp_request_state_evidence_authority
            ),
            **mcp_rollout_storage_kwargs,
        )
        agent_repository = PostgreSQLAgentRepository(primary_session_factory)
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
        primary_session_factory = create_sqlite_session_factory(engine)
        storage = SQLiteStorage(
            primary_session_factory,
            message_identity_authority_enabled=(
                canonical_task_authority_mode == "enforce"
            ),
            runtime_sidecar_client=resolved_runtime_sidecar_client,
            runtime_sidecar_shadow_sink=_build_runtime_sidecar_shadow_diff_sink(audit_sink),
            mcp_task_authority_mode=canonical_task_authority_mode,
            mcp_terminal_candidate_reader=read_terminal_candidate,
            mcp_terminal_candidate_resolver=resolve_terminal_candidate,
            mcp_pending_action_payload_reader=mcp_pending_action_payload_store,
            mcp_terminal_candidate_snapshot_reader=(
                mcp_terminal_candidate_snapshot_authority
            ),
            mcp_durable_result_snapshot_reader=(
                mcp_durable_result_snapshot_authority
            ),
            mcp_mrtr_request_state_evidence_reader=(
                mcp_request_state_evidence_authority
            ),
        )
        agent_repository = SQLiteAgentRepository(primary_session_factory)
        artifact_file_store = LocalArtifactFileStore(artifact_store_path or (Path(database_path).parent / "artifacts"))
        conversation_file_store = LocalConversationFileStore(
            conversation_file_store_path or (Path(database_path).parent / "conversation_files")
        )

    if canonical_task_authority_mode == "enforce":
        agent_repository = RuntimeSidecarAgentRepository(
            resolved_runtime_sidecar_client
        )

    if user_mcp_enabled:
        mcp_terminal_candidate_lifecycle_manager = (
            MCPTerminalCandidateLifecycleManager(storage, terminal_result_root)
        )
        assert mcp_durable_result_snapshot_authority is not None
        mcp_durable_result_lifecycle_manager = MCPDurableResultLifecycleManager(
            storage,
            mcp_durable_result_snapshot_authority,
            artifact_file_store=artifact_file_store,
        )

    user_mcp_config_service = None
    user_mcp_health_runner = None
    user_mcp_gateway = None
    mcp_invalidation_bus = None
    postgres_mcp_invalidation_bus = None
    user_mcp_result_janitor = None
    user_mcp_presence_service = None
    user_mcp_audit_service = None
    mcp_remote_task_recovery_worker = None
    user_mcp_instance_id = None
    if user_mcp_enabled:
        assert mcp_credential_cipher is not None
        assert user_mcp_capacity_values is not None
        endpoint_policy = EndpointPolicy()
        credential_resolver = UserMCPCredentialResolver(storage, mcp_credential_cipher)
        recovery_service = MCPRecoveryService(storage, mcp_recovery_cipher)
        user_client_factory = UserMCPClientFactory(
            endpoint_policy,
            recovery_service=recovery_service,
        )
        instance_id = f"mcp-instance-{uuid4().hex}"
        user_mcp_instance_id = instance_id
        user_mcp_audit_service = MCPAuditService(
            storage=storage,
            now_fn=ApiRuntime._utcnow_naive,
        )

        async def record_endpoint_security(server, endpoint) -> None:
            plaintext_http = bool(getattr(endpoint, "plaintext_http", False))
            try:
                await user_mcp_audit_service.record(
                    owner_user_id=server.owner_user_id,
                    event_type="mcp.endpoint_security_validated",
                    server_id=server.server_id,
                    safe_payload={
                        "plaintext_http": plaintext_http,
                        "credential_over_plaintext_http": (
                            plaintext_http and str(server.auth_type) != "none"
                        ),
                    },
                )
            except Exception:
                return

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
            validated_endpoint = await user_client_factory.revalidate_endpoint(server)
            await record_endpoint_security(server, validated_endpoint)
            request_headers = await credential_resolver.request_headers_for(server)
            return await user_client_factory.create_task_recovery(
                server,
                request_headers,
                validated_endpoint,
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

        assert result_root is not None
        assert user_mcp_result_store is not None
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
            endpoint_revalidator=user_client_factory.revalidate_endpoint,
            client_factory=user_client_factory.create_from_validated_endpoint,
            credential_loader=credential_resolver.request_headers_for,
            now_fn=ApiRuntime._utcnow_naive,
            endpoint_security_observer=record_endpoint_security,
        )
        user_mcp_gateway = MCPGateway(
            storage=storage,
            gateway_instance_id=instance_id,
            credential_loader=credential_resolver.request_headers_for,
            client_factory=user_client_factory.create_from_validated_endpoint,
            endpoint_revalidator=user_client_factory.revalidate_endpoint,
            readonly_shadow_client_factory=(
                user_client_factory.create_readonly_shadow
            ),
            result_store=user_mcp_result_store,
            capacity=capacity,
            now_fn=ApiRuntime._utcnow_naive,
            endpoint_security_observer=record_endpoint_security,
            result_service=mcp_result_service,
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
        token_hasher=auth_token_hasher,
        auth_generation_cache=auth_generation_cache,
        auth_invalidation_bus=auth_invalidation_bus,
    )

    capability_registry = CapabilityRegistry()
    skill_runtime_state = SkillRuntimeState(
        skill_roots=roots,
        public_skill_roots=resolved_public_skill_roots,
        reserved_capability_ids=[descriptor.capability_id for descriptor in capability_registry.list()],
        initial_catalog=skill_catalog,
        refresh_enabled=skill_catalog is None,
    )

    instance_registry = InstanceRegistry()
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
    if resolved_mcp_runtime_state is not None and mcp_result_service is None:
        legacy_projection_root = Path(
            os.environ.get("MAF_USER_MCP_RESULT_PROJECTION_ROOT")
            or (Path(database_path).parent / "user_mcp_result_projections")
        )
        mcp_projection_store = MCPProjectionStore(legacy_projection_root)
        mcp_result_service = MCPIsolatedResultService(
            projection_store=mcp_projection_store
        )
    if user_mcp_routing_enabled:
        _register_capability_descriptors(
            capability_registry,
            (MCP_DISPATCH_CAPABILITY_DESCRIPTOR,),
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

    async def record_initialization_event_exact(event: EventRecord) -> bool:
        event = _ensure_event_created_at(event)
        saved, duplicate = await storage.append_event_exact(event)
        if not duplicate:
            await event_broker.publish(saved)
        return duplicate

    if user_mcp_enabled:
        assert user_mcp_capacity_values is not None
        assert mcp_durable_result_lifecycle_manager is not None

        async def observe_mcp_result_artifact_projection(
            observation: MCPResultArtifactProjectionObservation,
        ) -> None:
            try:
                await audit_sink.record(
                    "mcp.result_artifact_projection_observed",
                    {
                        "status": observation.status.value,
                        "reason_code": observation.reason_code.value,
                        "source": observation.source,
                        "projection_latency_ms": observation.elapsed_ms,
                    },
                )
            except Exception:
                logger.error(
                    "mcp_result_artifact_projection_observation_failed"
                )

        mcp_result_artifact_projector = MCPResultArtifactProjector(
            storage=storage,
            lifecycle_manager=mcp_durable_result_lifecycle_manager,
            artifact_file_store=artifact_file_store,
            audit_reference_signer=mcp_audit_reference_signer,
            artifact_disk_low_watermark_bytes=user_mcp_capacity_values[1],
            event_sink=record_live_event,
            observer=observe_mcp_result_artifact_projection,
            now_fn=ApiRuntime._utcnow_naive,
        )
        mcp_durable_result_lifecycle_manager.configure_result_projector(
            mcp_result_artifact_projector
        )
        assert mcp_result_service is not None
        assert mcp_projection_store is not None
        assert mcp_durable_result_snapshot_authority is not None
        mcp_durable_result_lifecycle_manager.configure_business_reprojector(
            MCPHistoricalResultReprojector(
                storage=storage,
                authority_resolver=MCPRawResultAuthorityResolver(
                    storage=storage,
                    snapshot_authority=mcp_durable_result_snapshot_authority,
                    artifact_file_store=artifact_file_store,
                ),
                result_service=mcp_result_service,
                projection_store=mcp_projection_store,
                projection_attacher=(
                    mcp_result_artifact_projector.attach_published_projection
                ),
            )
        )

    async def publish_transient_event(event: EventRecord) -> None:
        event = _ensure_event_created_at(event)
        await event_broker.publish_transient(event)

    main_agent_llm_runtime = _resolve_main_agent_llm_runtime(
        main_agent_llm_config=main_agent_llm_config,
        main_agent_llm_config_path=main_agent_llm_config_path,
        main_agent_llm_client_factory=main_agent_llm_client_factory,
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
    agent_model_port = (
        StreamAgentModelAdapter(main_agent_stream_generator)
        if main_agent_stream_generator is not None
        else main_agent_llm_runtime
    )
    run_bound_mcp_generator = RunBoundMCPTextGenerator(
        runs=agent_repository,
        model=agent_model_port,
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
            platform_llm_config=platform_llm_config,
        ),
    )

    mcp_dispatch_executor = None
    mcp_shadow_observer = None
    mcp_rollout_metric_recorder = None
    mcp_dispatch_metric_context = None
    mcp_safety_detectors = None
    mcp_cp7_safety_facade = None
    mcp_cp7_safety_probes: tuple[Callable[[datetime, datetime], None], ...] = ()
    if mcp_cp7_runtime_identity is not None and mcp_rollout_instance_admission is not None:
        raise RuntimeError("CP7 local safety cannot share production rollout admission")
    if mcp_cp7_runtime_identity is not None:
        mcp_cp7_safety_facade, mcp_safety_detectors = cp7_runtime_safety_wiring(
            storage,
            mcp_cp7_runtime_identity,
            fatal_exit=mcp_cp7_fatal_exit,
        )
        if user_mcp_gateway is None or user_mcp_audit_service is None:
            raise RuntimeError("CP7 safety wiring requires MCP Gateway and audit")
        user_mcp_gateway.configure_safety_detectors(mcp_safety_detectors)
        user_mcp_gateway.configure_safety_admission_checker(
            mcp_cp7_safety_facade.ensure_ready
        )
        user_mcp_audit_service.configure_safety_detector(
            mcp_safety_detectors[MCPSafetyRedLine.SECRET_EXPOSURE]
        )
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

        if mcp_safety_detectors is None:
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
    if mcp_result_service is not None:
        async def observe_mcp_result_parser(
            observation: MCPResultParserObservation,
        ) -> None:
            call_kind = (
                MCPCallKind.ORDINARY
                if observation.source == MCPResultSource.TOOLS_CALL.value
                else MCPCallKind.REMOTE_TASK
            )
            if observation.outcome == "succeeded" and observation.reason == "none":
                result_category = MCPMetricResultCategory.SUCCEEDED
                error_category = MCPMetricErrorCategory.NONE
            elif observation.outcome == "malformed":
                result_category = MCPMetricResultCategory.FAILED
                error_category = MCPMetricErrorCategory.VALIDATION
            elif observation.outcome == "tool_error":
                result_category = MCPMetricResultCategory.FAILED
                error_category = MCPMetricErrorCategory.SERVER
            else:
                result_category = MCPMetricResultCategory.FAILED
                error_category = MCPMetricErrorCategory.CLEANUP
            try:
                metric_protocol = MCPMetricProtocolVersion(
                    observation.protocol_version
                )
            except ValueError:
                metric_protocol = MCPMetricProtocolVersion.NOT_APPLICABLE
            if mcp_rollout_metric_recorder is not None:
                observed_at = datetime.now(timezone.utc)
                started_at = observed_at.replace(second=0, microsecond=0)
                labels = MCPMetricLabels(
                    execution_path=MCPMetricExecutionPath.USER_SCOPED,
                    routing_mode=MCPMetricRoutingMode.ENFORCE,
                    transport=MCPMetricTransport.NOT_APPLICABLE,
                    protocol_version=metric_protocol,
                    adapter=(
                        MCPMetricAdapter.PYTHON_2026
                        if metric_protocol
                        is MCPMetricProtocolVersion.V2026_07_28
                        else MCPMetricAdapter.PYTHON_LEGACY
                    ),
                    result_category=result_category,
                    error_category=error_category,
                    call_kind=call_kind,
                )
                try:
                    await mcp_rollout_metric_recorder.record_count(
                        MCPMetricName.RESULT_PARSER_OUTCOMES_TOTAL,
                        labels=labels,
                        bucket_started_at=started_at,
                        bucket_ended_at=started_at + timedelta(minutes=1),
                    )
                    await mcp_rollout_metric_recorder.record_latency(
                        MCPMetricName.RESULT_PARSER_DURATION_SECONDS,
                        duration_seconds=observation.duration_seconds,
                        labels=labels,
                        bucket_started_at=started_at,
                        bucket_ended_at=started_at + timedelta(minutes=1),
                    )
                except Exception:
                    logger.error("mcp_result_parser_metric_record_failed")
        mcp_result_service.configure_observer(observe_mcp_result_parser)
    if user_mcp_enabled:
        mcp_runtime_holder: dict[str, ApiRuntime] = {}
        remote_parsed_results: dict[str, Any] = {}

        async def persist_remote_task_result(
            binding,
            result: Mapping[str, Any],
        ) -> str:
            if user_mcp_result_store is None:
                raise RuntimeError("mcp_remote_task_result_store_unavailable")
            sink = user_mcp_result_store.create_sink(
                binding.task_id,
                durable=True,
                owner_user_id=binding.owner_user_id,
                node_id=binding.node_id,
                call_ref=binding.call_ref,
            )
            try:
                encoded = json.dumps(
                    dict(result),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                await sink.write(encoded)
                persisted = await sink.finalize()
            except BaseException:
                await sink.abort()
                raise
            return persisted.ref

        async def process_remote_task_result(
            binding,
            result: Mapping[str, Any],
            result_source: str,
        ) -> MCPRemoteTaskProcessedResult:
            if user_mcp_result_store is None or mcp_result_service is None:
                raise RuntimeError("mcp_remote_task_result_service_unavailable")
            result_ref = await persist_remote_task_result(binding, result)
            descriptor = user_mcp_result_store.resolve_ref(result_ref)
            call = await storage.get_mcp_call_record(
                binding.owner_user_id, binding.task_id, binding.call_ref
            )
            if call is None or call.protocol_version != binding.protocol_version:
                await user_mcp_result_store.discard(descriptor)
                raise RuntimeError("mcp_remote_task_result_authority_invalid")
            try:
                parsed = await mcp_result_service.parse(
                    owner_user_id=binding.owner_user_id,
                    task_id=binding.task_id,
                    node_id=binding.node_id,
                    call_ref=binding.call_ref,
                    request=MCPResultDecodeRequest(
                        protocol_version=call.protocol_version,
                        source=MCPResultSource(result_source),
                        payload=user_mcp_result_store.result_parser_descriptor(
                            descriptor
                        ),
                        output_schema=call.output_schema,
                        output_schema_sha256=call.output_schema_sha256,
                    ),
                )
            except BaseException:
                await user_mcp_result_store.discard(descriptor)
                raise
            checkpoint = parsed.checkpoint
            remote_parsed_results[binding.call_ref] = parsed
            if checkpoint.outcome == "succeeded":
                return MCPRemoteTaskProcessedResult(
                    "completed", None, result_ref
                )
            await user_mcp_result_store.discard(descriptor)
            return MCPRemoteTaskProcessedResult(
                "failed",
                (
                    "mcp_tool_error"
                    if checkpoint.outcome == "tool_error"
                    else "mcp_result_malformed"
                ),
                None,
            )

        async def seal_remote_task_terminal(
            binding,
            call_status: str,
            result_ref: str | None,
            safe_error_code: str | None,
        ) -> None:
            task = await storage.get_task(binding.task_id)
            call = await storage.get_mcp_call_record(
                binding.owner_user_id, binding.task_id, binding.call_ref
            )
            intent = await storage.get_mcp_no_server_intent(
                f"mcp-no-server-intent:v1:{binding.task_id}:{binding.node_id}"
            )
            if task is None or call is None:
                raise RuntimeError("mcp_remote_task_terminal_binding_missing")
            if task.mcp_execution_mode != MCPExecutionPath.USER_SCOPED.value:
                return
            if intent is None or call.server_config_version is None:
                raise RuntimeError("mcp_remote_task_terminal_authority_corrupt")
            terminal_state = MCPTerminalState(call_status)
            parsed_outcome = remote_parsed_results.get(binding.call_ref)
            checkpoint = (
                parsed_outcome.checkpoint
                if parsed_outcome is not None
                else None
            )
            if checkpoint is not None:
                expected_source = (
                    "tasks_result"
                    if binding.protocol_version == "2025-11-25"
                    else "tasks_get"
                )
                if (
                    checkpoint.call_ref != binding.call_ref
                    or checkpoint.protocol_version != call.protocol_version
                    or checkpoint.source != expected_source
                    or checkpoint.output_schema_sha256
                    != call.output_schema_sha256
                    or (
                        checkpoint.outcome == "succeeded"
                        and call_status != "completed"
                    )
                    or (
                        checkpoint.outcome in {"tool_error", "malformed"}
                        and call_status != "failed"
                    )
                ):
                    raise RuntimeError(
                        "mcp_remote_task_result_checkpoint_invalid"
                    )
            result_descriptor = (
                user_mcp_result_store.resolve_ref(result_ref)
                if result_ref is not None
                else None
            )
            if result_descriptor is not None:
                result_descriptor = await user_mcp_result_store.verify_durable_ref(
                    result_ref,
                    owner_user_id=binding.owner_user_id,
                    task_id=binding.task_id,
                    node_id=binding.node_id,
                    call_ref=binding.call_ref,
                    scope_id=None,
                    expected_size_bytes=result_descriptor.size_bytes,
                    expected_sha256=result_descriptor.sha256,
                    expected_store_kind="durable_content_addressed",
                )
                if (
                    checkpoint is not None
                    and checkpoint.raw_sha256
                    != f"sha256:{result_descriptor.sha256}"
                ):
                    raise RuntimeError(
                        "mcp_remote_task_result_checkpoint_invalid"
                    )
            payload_sha = canonical_sha256(
                {
                    "safe_result_ref": result_ref,
                    "safe_error_code": safe_error_code,
                    "terminal_state": call_status,
                }
            )
            await asyncio.to_thread(
                seal_terminal_result_candidate,
                terminal_result_root,
                MCPValidatedTerminalResultCandidate(
                    candidate_id=mcp_terminal_candidate_id(
                        binding.call_ref, payload_sha
                    ),
                    owner_user_id=binding.owner_user_id,
                    conversation_id=task.conversation_id,
                    task_id=binding.task_id,
                    node_id=binding.node_id,
                    intent_id=intent.intent_id,
                    call_id=binding.call_ref,
                    server_id=binding.server_id,
                    server_config_version=call.server_config_version,
                    server_security_version=call.server_security_version,
                    terminal_state=terminal_state,
                    result_payload_sha256=payload_sha,
                    safe_result_ref=result_ref,
                    safe_result_ref_sha256=(
                        canonical_sha256(result_ref) if result_ref is not None else None
                    ),
                    safe_error_code=safe_error_code,
                    sealed_at=terminal_now_utc_second(),
                    safe_result_content_sha256=(
                        f"sha256:{result_descriptor.sha256}"
                        if result_descriptor is not None
                        else None
                    ),
                    safe_result_size_bytes=(
                        result_descriptor.size_bytes
                        if result_descriptor is not None
                        else None
                    ),
                    safe_result_store_kind=(
                        "durable_content_addressed"
                        if result_descriptor is not None
                        else None
                    ),
                    result_parser_revision=(
                        checkpoint.parser_revision
                        if checkpoint is not None
                        else None
                    ),
                    validated_checkpoint_sha256=(
                        checkpoint.checkpoint_sha256
                        if checkpoint is not None
                        else None
                    ),
                    parsed_model_sha256=(
                        checkpoint.parsed_model_sha256
                        if checkpoint is not None
                        else None
                    ),
                    terminal_result_source=(
                        checkpoint.source if checkpoint is not None else None
                    ),
                ),
            )
        async def commit_remote_task_result(
            binding, result_ref: str | None
        ) -> str | None:
            candidate = resolve_terminal_candidate(binding.call_ref)
            if candidate is None:
                task = await storage.get_task(binding.task_id)
                if task is not None and task.mcp_execution_mode != MCPExecutionPath.USER_SCOPED.value:
                    return None
                raise RuntimeError("mcp_remote_task_terminal_candidate_missing")
            if candidate.safe_result_ref != result_ref:
                raise RuntimeError("mcp_remote_task_terminal_candidate_missing")
            if (
                mcp_terminal_candidate_snapshot_authority is None
                or mcp_durable_result_snapshot_authority is None
            ):
                raise RuntimeError("mcp_remote_task_snapshot_authority_missing")
            sealed = secure_read_terminal_result_candidate(
                terminal_result_root, candidate.candidate_id
            )
            candidate_snapshot = (
                mcp_terminal_candidate_snapshot_authority.snapshot(sealed)
            )
            intent_id = mcp_no_server_intent_id(
                binding.task_id, node_id=binding.node_id
            )
            outbox = await storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent_id)
            )
            if outbox is None:
                raise RuntimeError("mcp_remote_task_dispatch_outbox_missing")
            commit_kwargs = {
                "remote_binding_ref": binding.safe_remote_task_ref,
                "remote_claim_owner": binding.claim_owner,
                "remote_claim_token": binding.claim_token,
                "remote_expected_revision": binding.revision,
            }
            if result_ref is None:
                committed = await storage.commit_mcp_call_terminal(
                    binding.call_ref,
                    candidate.candidate_id,
                    outbox.outbox_id,
                    outbox.revision,
                    None,
                    None,
                    candidate_snapshot,
                    None,
                    ApiRuntime._utcnow_naive(),
                    **commit_kwargs,
                )
            else:
                if (
                    candidate.safe_result_size_bytes is None
                    or candidate.safe_result_content_sha256 is None
                    or candidate.safe_result_store_kind is None
                ):
                    raise RuntimeError("mcp_remote_task_result_snapshot_missing")
                async with mcp_durable_result_snapshot_authority.open_snapshot(
                    result_ref=result_ref,
                    owner_user_id=binding.owner_user_id,
                    task_id=binding.task_id,
                    node_id=binding.node_id,
                    call_id=binding.call_ref,
                    expected_size_bytes=candidate.safe_result_size_bytes,
                    expected_content_sha256=(
                        candidate.safe_result_content_sha256
                    ),
                    expected_store_kind=candidate.safe_result_store_kind,
                ) as result_snapshot:
                    committed = await storage.commit_mcp_call_terminal(
                        binding.call_ref,
                        candidate.candidate_id,
                        outbox.outbox_id,
                        outbox.revision,
                        None,
                        None,
                        candidate_snapshot,
                        result_snapshot,
                        ApiRuntime._utcnow_naive(),
                        **commit_kwargs,
                    )
            if str(committed) == "conflict":
                raise RuntimeError("mcp_remote_task_terminal_commit_conflict")
            parsed_outcome = remote_parsed_results.pop(binding.call_ref, None)
            published_projection = None
            if result_ref is not None:
                user_mcp_result_store.mark_promoted(
                    user_mcp_result_store.resolve_ref(result_ref)
                )
            if (
                parsed_outcome is not None
                and parsed_outcome.projection_staging_handle is not None
                and mcp_result_service is not None
            ):
                try:
                    published_projection = mcp_result_service.publish_projection(
                        parsed_outcome.projection_staging_handle
                    )
                except Exception:
                    pass
            if (
                result_ref is not None
                and candidate.terminal_state is MCPTerminalState.COMPLETED
                and mcp_result_artifact_projector is not None
            ):
                try:
                    projected = await mcp_result_artifact_projector.project_completed_result(
                        result_ref,
                        source="immediate",
                    )
                    if (
                        projected.artifact is not None
                        and published_projection is not None
                        and parsed_outcome is not None
                        and parsed_outcome.projection_staging_handle is not None
                    ):
                        await mcp_result_artifact_projector.attach_published_projection(
                            projected.artifact,
                            published=published_projection,
                            staging_handle=(
                                parsed_outcome.projection_staging_handle
                            ),
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            return mcp_terminal_receipt_id(
                binding.call_ref, candidate.result_payload_sha256
            )

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
                        "safe_call_ref": mcp_audit_reference_signer.safe_reference(
                            binding.call_ref,
                            context="mcp-call-reference-v1",
                        ),
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
            result_processor=process_remote_task_result,
            result_committer=commit_remote_task_result,
            terminal_sealer=seal_remote_task_terminal,
            continuation_sink=continue_remote_task,
            now_fn=ApiRuntime._utcnow_naive,
        )
        if mcp_safety_detectors is not None:
            mcp_remote_task_recovery_worker.configure_safety_detectors(
                mcp_safety_detectors
            )
        if user_mcp_gateway is not None:
            user_mcp_gateway.configure_remote_task_canceller(
                mcp_remote_task_recovery_worker.cancel_remote_task
            )
    if user_mcp_routing_enabled:
        if user_mcp_gateway is None:
            raise RuntimeError("User-scoped MCP routing requires the user MCP Gateway")
        if (
            mcp_pending_action_payload_store is None
            or mcp_terminal_candidate_snapshot_authority is None
            or mcp_durable_result_snapshot_authority is None
        ):
            raise RuntimeError(
                "User-scoped MCP routing requires durable dispatch authorities"
            )
        mcp_tool_selector = MCPToolSelector(
            run_bound_generator=run_bound_mcp_generator
        )
        mcp_server_router = MCPServerRouter(
            run_bound_generator=run_bound_mcp_generator
        )

        async def project_mcp_result_with_business_projection(
            result_ref: str,
            published_projection: object | None,
            staging_handle: object | None,
        ):
            if mcp_result_artifact_projector is None:
                return None
            projected = await mcp_result_artifact_projector.project_completed_result(
                result_ref,
                source="immediate",
            )
            if (
                projected.artifact is not None
                and published_projection is not None
                and staging_handle is not None
            ):
                await mcp_result_artifact_projector.attach_published_projection(
                    projected.artifact,
                    published=published_projection,
                    staging_handle=staging_handle,
                )
            return projected

        mcp_dispatch_coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=user_mcp_gateway,
            selector=mcp_tool_selector,
            audit_reference_signer=mcp_audit_reference_signer,
            selector_context_builder=MCPDurableSelectorContextBuilder(
                storage=storage,
                projection_authority=MCPPublishedAgentProjectionAuthority(
                    storage=storage,
                    projection_store=mcp_projection_store,
                ),
            ),
            pending_action_payload_store=mcp_pending_action_payload_store,
            terminal_candidate_snapshot_authority=(
                mcp_terminal_candidate_snapshot_authority
            ),
            durable_result_snapshot_authority=(
                mcp_durable_result_snapshot_authority
            ),
            server_router=mcp_server_router,
            live_event_recorder=record_live_event,
            result_artifact_projector=(
                None
                if mcp_result_artifact_projector is None
                else project_mcp_result_with_business_projection
            ),
            now_fn=ApiRuntime._utcnow_naive,
            metric_recorder=mcp_rollout_metric_recorder,
            metric_context=mcp_dispatch_metric_context,
            safety_detectors=mcp_safety_detectors,
            terminal_result_root=terminal_result_root,
            cp7_candidate_id=(
                None if mcp_cp7_runtime_identity is None
                else mcp_cp7_runtime_identity.candidate_id
            ),
            cp7_epoch_id=(
                None if mcp_cp7_runtime_identity is None
                else mcp_cp7_runtime_identity.epoch_id
            ),
        )
        if mcp_cp7_safety_facade is not None:
            if mcp_remote_task_recovery_worker is None:
                raise RuntimeError("CP7 safety wiring requires MCP recovery")
            mcp_cp7_safety_probes = (
                user_mcp_audit_service.attest_safety_interval,
                user_mcp_gateway.attest_safety_interval,
                mcp_dispatch_coordinator.attest_safety_interval,
            )
        if mcp_rollout_metric_recorder is not None:
            if mcp_remote_task_recovery_worker is None:
                raise RuntimeError("MCP rollout safety requires MCP recovery")
            mcp_rollout_metric_recorder.configure_safety_interval_probes(
                user_mcp_audit_service.attest_safety_interval,
                user_mcp_gateway.attest_safety_interval,
                mcp_dispatch_coordinator.attest_safety_interval,
            )
        mcp_dispatch_executor = MCPDispatchExecutor(
            coordinator=mcp_dispatch_coordinator
        )
        if mcp_rollout_config.routing_mode is MCPRoutingMode.SHADOW:
            assert mcp_credential_cipher is not None
            mcp_shadow_observer = MCPShadowRuntimeObserver(
                storage=storage,
                gateway=user_mcp_gateway,
                server_router=mcp_server_router,
                selector=mcp_tool_selector,
                endpoint_policy=endpoint_policy,
                digest_key=derive_shadow_catalog_digest_key(
                    mcp_audit_reference_signer,
                    config_fingerprint=mcp_rollout_config.fingerprint,
                ),
            )
    resolved_model_edition_config = _resolve_model_edition_config(
        main_agent_llm_config=main_agent_llm_config,
        platform_llm_config=platform_llm_config,
    )
    cancellation_service = CancellationService(
        storage,
        event_sink=event_broker,
        audit_sink=audit_sink,
        runtime_sidecar_client=resolved_runtime_sidecar_client,
    )
    interrupt_service = InterruptService(
        storage,
        event_sink=event_broker,
        audit_sink=audit_sink,
    )
    composite_executor = CompositeExecutor(
        [
            SkillExecutor(
                runtime_state=skill_runtime_state,
                script_runner=skill_script_runner,
                skill_input_text_generator=resolved_skill_input_text_generator,
                platform_handler_registry=resolved_skill_platform_handler_registry,
                service_registry=resolved_skill_service_registry,
            ),
            *([mcp_dispatch_executor] if mcp_dispatch_executor is not None else []),
        ]
    )

    def make_agent_event(
        *,
        task_id: str,
        conversation_id: str,
        event_type: str,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
        visibility: EventVisibility = EventVisibility.FRONTEND,
    ) -> EventRecord:
        return EventRecord(
            event_id=f"evt-{uuid4().hex[:24]}",
            conversation_id=conversation_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload or {},
            visibility=visibility,
            created_at=ApiRuntime._utcnow_naive(),
        )

    invocation_contexts = AgentInvocationContextStore()
    invocation_commit_port = AgentTaskInvocationCommitPort(
        storage=storage,
        runs=agent_repository,
        make_event=make_agent_event,
        record_event=record_live_event,
    )
    invocation_service = CapabilityInvocationService(
        instance_selector=InstanceSelector(instance_registry),
        executor=composite_executor,
        commit_port=invocation_commit_port,
        now_fn=ApiRuntime._utcnow_naive,
    )

    delegated_skill_activation = DelegatedSkillActivationService(None)
    agent_skill_result_manifest_root = (
        Path(database_path).parent / "agent_skill_result_stage_manifests"
    )
    agent_skill_result_stager = AgentSkillResultArtifactStager(
        file_store=artifact_file_store,
        manifest_root=agent_skill_result_manifest_root,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    agent_transient_skill_result_store = AgentTransientSkillResultStore(
        Path(database_path).parent / "agent_transient_skill_results",
        now_fn=lambda: datetime.now(timezone.utc),
    )
    runtime_holder: dict[str, ApiRuntime] = {}

    async def agent_invocation_hook(**values: Any):
        runtime = runtime_holder.get("runtime")
        if runtime is None:
            return None
        return await runtime._mcp_invocation_shadow_hook(**values)

    async def observe_agent_result_projection(
        *,
        run: AgentRun,
        call_item: Any,
        observation: AgentResultProjectionObservation,
    ) -> None:
        await record_initialization_event_exact(
            build_agent_result_projected_event(
                run=run,
                call_item=call_item,
                observation=observation,
            )
        )

    async def activate_delegated_skill(
        run,
        capability_id: str,
        metadata: Mapping[str, Any],
    ) -> AgentCallExecution | None:
        pinned_revision = str(metadata.get("skill_bundle_revision") or "").strip()
        try:
            bundle = skill_runtime_state.bundle_for_revision(
                pinned_revision or None
            )
        except KeyError:
            return AgentCallExecution(
                AgentCallOutcomeStatus.FAILED,
                safe_error_code="skill_bundle_revision_missing",
            )
        skill_name = bundle.skill_capabilities.skill_name_by_capability_id.get(
            capability_id
        )
        manifest = next(
            (skill for skill in bundle.catalog.skills if skill.name == skill_name),
            None,
        )
        if manifest is None:
            return None
        try:
            execution = resolve_skill_execution_config(manifest)
        except Exception:
            return AgentCallExecution(
                AgentCallOutcomeStatus.FAILED,
                safe_error_code="skill_execution_config_invalid",
            )
        if execution.mode != "delegated_main_agent":
            return None
        if not pinned_revision:
            return AgentCallExecution(
                AgentCallOutcomeStatus.FAILED,
                safe_error_code="agent_skill_pinned_revision_missing",
            )
        descriptor = bundle.skill_capabilities.descriptors_by_id.get(capability_id)
        profile = build_public_skill_profile(
            manifest,
            capability_id=capability_id,
            descriptor=descriptor,
        )
        try:
            expected_activation = build_canonical_skill_activation(
                binding_mode="hint",
                profile=profile,
                pinned_bundle_revision=pinned_revision,
                resolved_bundle_revision=bundle.revision,
            )
        except (TypeError, ValueError):
            return AgentCallExecution(
                AgentCallOutcomeStatus.FAILED,
                safe_error_code="agent_skill_activation_invalid",
            )
        existing_activation = None
        for candidate in await agent_repository.list_items(run.run_id):
            if candidate.kind.value != "skill_activation":
                continue
            try:
                payload = json.loads(candidate.payload_json)
                candidate_capability_id = str(
                    payload.get("profile", {}).get("capability_id") or ""
                )
            except (AttributeError, TypeError, ValueError):
                return AgentCallExecution(
                    AgentCallOutcomeStatus.FAILED,
                    safe_error_code="delegated_skill_instruction_invalid",
                )
            if candidate_capability_id != capability_id:
                continue
            if (
                payload.get("binding_mode") not in {"hint", "delegated"}
                or payload.get("pinned_bundle_revision") != pinned_revision
                or payload.get("profile_digest")
                != expected_activation.profile_digest
                or existing_activation is not None
            ):
                return AgentCallExecution(
                    AgentCallOutcomeStatus.FAILED,
                    safe_error_code="delegated_skill_instruction_invalid",
                )
            existing_activation = candidate
        item = None
        if existing_activation is None:
            try:
                item, _profile_digest = delegated_skill_activation.build_item(
                    run=run,
                    profile=profile,
                    sequence=run.next_item_sequence,
                    pinned_bundle_revision=pinned_revision,
                    resolved_bundle_revision=bundle.revision,
                )
            except (TypeError, ValueError):
                return AgentCallExecution(
                    AgentCallOutcomeStatus.FAILED,
                    safe_error_code="delegated_skill_instruction_invalid",
                )
        try:
            safe_result = build_delegated_skill_instruction_result(
                capability_id=capability_id,
                pinned_bundle_revision=pinned_revision,
                profile_digest=expected_activation.profile_digest,
                instruction_body=manifest.body,
            )
        except (TypeError, ValueError):
            return AgentCallExecution(
                AgentCallOutcomeStatus.FAILED,
                safe_error_code="delegated_skill_instruction_invalid",
            )
        return AgentCallExecution(
            AgentCallOutcomeStatus.COMPLETED,
            safe_result_payload=safe_result,
            skill_activation_item=item,
        )

    async def publish_agent_reasoning(run, delta: str, ordinal: int) -> None:
        await publish_transient_event(
            EventRecord(
                event_id=f"{run.run_id}:reasoning:{run.revision}:{ordinal}",
                conversation_id=run.conversation_id,
                task_id=run.task_id,
                node_id=run.active_sample_item_id,
                event_type="agent.reasoning_delta",
                payload={
                    "delta": delta,
                    "ordinal": ordinal,
                    "sample_id": f"agent-sample:{run.run_id}:r{run.revision}",
                },
                visibility=EventVisibility.FRONTEND,
            )
        )

    async def publish_agent_reasoning_reset(
        run,
        sample_id: str,
        reset_ordinal: int,
    ) -> None:
        await publish_transient_event(
            EventRecord(
                event_id=(
                    f"{run.run_id}:reasoning-reset:{run.revision}:{reset_ordinal}"
                ),
                conversation_id=run.conversation_id,
                task_id=run.task_id,
                node_id=run.active_sample_item_id,
                event_type="agent.reasoning_reset",
                payload={"sample_id": sample_id},
                visibility=EventVisibility.FRONTEND,
            )
        )
    agent_invoker = AgentCapabilityInvoker(
        invocation_service=invocation_service,
        runs=agent_repository,
        task_loader=storage.get_task,
        node_loader=storage.get_task_node,
        request_metadata_loader=invocation_contexts.request_metadata,
        current_user_input_loader=invocation_contexts.current_user_input,
        continuation_loader=invocation_commit_port.continuation_locator_for_call,
        delegated_skill_activator=activate_delegated_skill,
        legacy_result_artifact_stager=agent_skill_result_stager.stage,
        transient_result_stager=agent_transient_skill_result_store.stage,
        result_projection_observer=observe_agent_result_projection,
        invocation_hook=agent_invocation_hook,
    )
    lease_controller = AgentLeaseController(agent_repository, ttl_seconds=30)
    agent_transient_result_resolver = AgentTransientSkillResultResolver(
        agent_transient_skill_result_store
    )
    agent_context_builder = AgentContextBuilder(
        AgentContextRules(
            stable_rules=(
                "\n".join(
                    (
                        *MAIN_AGENT_SYSTEM_CONTRACT_LINES,
                        "根据当前公开Tool catalog选择Tool，观察结果并继续；"
                        "不需要Tool时直接给出最终回答。",
                    )
                )
            ),
            safe_tool_rules=(
                "只能调用本轮catalog中的Tool；不得伪造Tool结果、凭据、隐藏路径或内部状态。"
            ),
            final_guard="最终回答必须面向用户，且不得包含隐藏推理或原始敏感结果。",
        ),
        transient_result_resolver=agent_transient_result_resolver,
    )

    async def count_agent_context_tokens(fragments, binding):
        return await get_num_of_tokens_from_messages_async(
            fragments,
            config=config_for_model_edition(
                resolved_model_edition_config,
                binding.model_edition,
            ),
        )

    agent_context_candidate_builder = AgentContextCandidateBuilder(
        context_builder=agent_context_builder,
        token_counter=count_agent_context_tokens,
    )
    agent_compaction_service = AgentCompactionService(
        runs=agent_repository,
        writer=agent_repository,
        model=agent_model_port,
        lease_controller=lease_controller,
        candidate_builder=agent_context_candidate_builder,
        transient_result_resolver=agent_transient_result_resolver,
    )
    agent_runner = AgentLoopRunner(
        runs=agent_repository,
        writer=agent_repository,
        model=agent_model_port,
        context_builder=agent_context_builder,
        catalog_builder=AgentToolCatalogBuilder(capability_registry),
        visibility_context=CapabilityVisibilityContext("runtime-default"),
        lease_controller=lease_controller,
        invoker=agent_invoker,
        owner_id=f"api-agent:{uuid4().hex}",
        reasoning_delta_sink=publish_agent_reasoning,
        reasoning_reset_sink=publish_agent_reasoning_reset,
        terminal_event_recorder=record_initialization_event_exact,
        context_candidate_builder=agent_context_candidate_builder,
        compaction_service=agent_compaction_service,
    )
    final_output_publisher = AgentFinalOutputPublisher(
        runs=agent_repository,
        writer=agent_repository,
        lease_controller=lease_controller,
    )

    def build_agent_model_binding(
        request: AgentExecutionRequest,
    ) -> AgentModelBinding:
        options = resolve_llm_request_options(
            request.metadata,
            model_reasoning_configs=model_reasoning_effort_configs(
                resolved_model_edition_config
            ),
            default_model_edition=default_model_edition(
                resolved_model_edition_config
            ),
        )
        if options.model_edition is None:
            raise ValueError("agent_model_edition_missing")
        option_payload = json.dumps(
            {
                "reasoning_effort": options.reasoning_effort,
                "thinking_enabled": options.thinking,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return AgentModelBinding(
            model_edition=options.model_edition,
            reasoning_effort=options.reasoning_effort,
            thinking_enabled=options.thinking,
            option_digests={
                "model_options": hashlib.sha256(
                    option_payload.encode("utf-8")
                ).hexdigest()
            },
        )

    def build_agent_context_budget(
        binding: AgentModelBinding,
    ) -> AgentContextBudget:
        model_context_window_tokens = trim_max_tokens_for_model_edition(
            binding.model_edition,
            config=resolved_model_edition_config,
        )
        if model_context_window_tokens is None:
            raise ValueError("agent_context_budget_invalid")
        return AgentContextBudget.from_model_context_window(
            model_context_window_tokens
        )

    agent_loop_orchestrator = AgentLoopOrchestrator(
        runs=agent_repository,
        writer=agent_repository,
        runner=agent_runner,
        final_output=final_output_publisher,
        contexts=invocation_contexts,
        task_loader=storage.get_task,
        task_cas=storage.compare_and_set_task,
        binding_factory=build_agent_model_binding,
        context_budget_factory=build_agent_context_budget,
        record_event=record_live_event,
        make_event=make_agent_event,
        initialization_event_recorder=record_initialization_event_exact,
    )
    agent_recovery = AgentRunRecoveryCoordinator(
        runs=agent_repository,
        writer=agent_repository,
        lease_store=agent_repository,
        resumer=agent_loop_orchestrator,
        owner_id=f"api-agent-recovery:{uuid4().hex}",
    )
    interrupt_service = InterruptService(
        storage,
        event_sink=event_broker,
        audit_sink=audit_sink,
        agent_recovery=agent_recovery,
    )
    agent_task_projection = AgentTaskProjectionService(
        runs=agent_repository,
        tasks=storage,
    )

    runtime = ApiRuntime(
        engine=engine,
        storage=storage,
        capability_registry=capability_registry,
        instance_registry=instance_registry,
        event_broker=event_broker,
        cancellation_service=cancellation_service,
        interrupt_service=interrupt_service,
        agent_loop_orchestrator=agent_loop_orchestrator,
        agent_run_repository=agent_repository,
        agent_task_projection=agent_task_projection,
        agent_capability_invoker=agent_invoker,
        agent_invocation_contexts=invocation_contexts,
        agent_run_recovery=agent_recovery,
        main_agent_llm_runtime=main_agent_llm_runtime,
        mysql_adapter=resolved_mysql_adapter,
        username_token_service=username_token_service,
        master_key_sentinel_cipher=master_key_sentinel_cipher,
        mcp_audit_reference_signer=mcp_audit_reference_signer,
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
        model_edition_config=resolved_model_edition_config,
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
        mcp_pending_action_payload_store=mcp_pending_action_payload_store,
        mcp_terminal_candidate_snapshot_authority=(
            mcp_terminal_candidate_snapshot_authority
        ),
        mcp_durable_result_snapshot_authority=(
            mcp_durable_result_snapshot_authority
        ),
        mcp_terminal_candidate_lifecycle_manager=(
            mcp_terminal_candidate_lifecycle_manager
        ),
        mcp_durable_result_lifecycle_manager=(
            mcp_durable_result_lifecycle_manager
        ),
        mcp_result_artifact_projector=mcp_result_artifact_projector,
        mcp_projection_store=mcp_projection_store,
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
        mcp_terminal_result_root=(terminal_result_root if user_mcp_enabled else None),
        mcp_legacy_retirement_binding=(
            (
                mcp_legacy_retirement_inventory_id,
                mcp_legacy_retirement_inventory_sha256,
            )
            if mcp_legacy_retirement_inventory_id
            and mcp_legacy_retirement_inventory_sha256
            else None
        ),
        mcp_cp7_safety_facade=mcp_cp7_safety_facade,
        mcp_cp7_open_boundary=mcp_cp7_open_boundary,
        mcp_cp7_boundary_provider=mcp_cp7_boundary_provider,
        mcp_cp7_fatal_exit=mcp_cp7_fatal_exit,
        mcp_cp7_safety_probes=mcp_cp7_safety_probes,
        mcp_cp7_predecessor_close=mcp_cp7_predecessor_close,
        mcp_cp7_verifier_authorized=mcp_cp7_verifier_authorized,
        mcp_cp7_maintenance_authorization=mcp_cp7_maintenance_authorization,
        mcp_cp7_maintenance_authorizer=mcp_cp7_maintenance_authorizer,
        expected_submission_authority_receipt_sha256=(
            expected_submission_authority_receipt_sha256
        ),
    )
    runtime._agent_skill_result_janitor = AgentSkillResultArtifactJanitor(
        file_store=artifact_file_store,
        manifest_root=agent_skill_result_manifest_root,
        storage=storage,
        runs=agent_repository,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    async def wait_until_submission_claim_deadline(deadline: datetime) -> None:
        now = runtime._utcnow_naive()
        if deadline.tzinfo is not None:
            now = now.replace(tzinfo=timezone.utc)
        await asyncio.sleep(max(0.0, (deadline - now).total_seconds()))

    runtime._prepared_agent_recovery_loader = SubmissionPreparedAgentRecoveryLoader(
        admission=storage,
        receipts=storage,
    )
    runtime._submission_admission_coordinator = SubmissionAdmissionCoordinator(
        admission=storage,
        receipts=storage,
        callbacks=runtime,
        claim_owner=runtime._submission_claim_owner,
        now=runtime._utcnow_naive,
        wait_until=wait_until_submission_claim_deadline,
        expected_finalization_receipt_sha256=(
            expected_submission_authority_receipt_sha256
        ),
        claim_ttl=runtime._submission_claim_ttl,
    )
    runtime_holder["runtime"] = runtime
    if user_mcp_enabled:
        mcp_runtime_holder["runtime"] = runtime
    return runtime


def _resolve_model_edition_config(
    *,
    main_agent_llm_config: Mapping[str, Any] | None,
    platform_llm_config: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    for config in (main_agent_llm_config, platform_llm_config):
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

    if (
        enable_conversation_memory
        and main_agent_llm_config is None
        and main_agent_llm_config_path is None
        and main_agent_llm_client_factory is None
    ):
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
        ) and not _dev_local_runtime_sidecar_attestation_bypass_allowed():
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


def _dev_local_runtime_sidecar_attestation_bypass_allowed() -> bool:
    if os.environ.get("MAF_API_ENV", "").strip().lower() != "dev":
        return False
    if (
        os.environ.get("MAF_RUNTIME_SIDECAR_ENDPOINT", "").strip()
        != "unix:///run/maf-runtime-sidecar/runtime.sock"
    ):
        return False
    if os.environ.get("MCP_USER_SCOPED_GATEWAY_ENABLED", "").strip().lower() != "true":
        return False
    if os.environ.get("MCP_ROUTING_MODE", "").strip().lower() != "enforce":
        return False
    if os.environ.get("MCP_LEGACY_GLOBAL_RUNTIME_ENABLED", "").strip().lower() != "false":
        return False
    if os.environ.get("MCP_ENFORCE_COHORTS", "").strip():
        return False
    if os.environ.get("MCP_ENFORCE_PERCENT", "").strip() != "100":
        return False
    if os.environ.get("MCP_ENFORCE_HASH_SALT", "").strip() != "main-cp7a-user-scoped-v1":
        return False
    if os.environ.get("MCP_ENFORCE_COHORT_CONFIG_FILE", "").strip():
        return False
    return all(
        runtime_sidecar_mode_for_component(component) == "off"
        for component in ("runtime_store", "event_log", "task_dispatcher")
    )


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
) -> SharedLLMRuntime:
    config = main_agent_llm_config
    factory = main_agent_llm_client_factory or LLMClient
    if main_agent_llm_config is not None:
        config_source = "injected_config"
    elif main_agent_llm_config_path is not None:
        config_source = "environment"
    elif main_agent_llm_client_factory is not None:
        config_source = "main_agent_factory_default"
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
    storage: ConversationMemoryStoragePort,
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
            if reasoning_event_publisher is None or not isinstance(request, AgentExecutionRequest):
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
                if reasoning_event_publisher is not None and isinstance(request, AgentExecutionRequest)
                else None
            ),
        )

    def resolve_memory_config(request: AgentExecutionRequest) -> ConversationMemoryConfig:
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
) -> None:
    for descriptor in descriptors:
        capability_registry.register(
            descriptor,
            invocation_policy=default_agent_invocation_policy(descriptor),
        )


def _sync_skill_capability_registry(
    capability_registry: CapabilityRegistry,
    instance_registry: InstanceRegistry,
    runtime_state: SkillRuntimeState,
) -> None:
    registry = runtime_state.active_bundle.skill_capabilities
    previous_descriptors = [descriptor for descriptor in capability_registry.list() if _is_skill_descriptor(descriptor)]
    previous_invocation_policies = capability_registry.invocation_policies()
    try:
        for descriptor in previous_descriptors:
            capability_registry.unregister(descriptor.capability_id)
        _register_capability_descriptors(
            capability_registry,
            registry.descriptors,
        )
        instance_registry.register(build_local_skill_executor_instance(runtime_state.known_skill_capability_ids()))
    except Exception:
        for descriptor in list(capability_registry.list()):
            if _is_skill_descriptor(descriptor):
                capability_registry.unregister(descriptor.capability_id)
        for descriptor in previous_descriptors:
            capability_registry.register(
                descriptor,
                invocation_policy=previous_invocation_policies.get(
                    descriptor.capability_id
                ),
            )
        instance_registry.register(build_local_skill_executor_instance(runtime_state.known_skill_capability_ids()))
        raise


def _sync_mcp_capability_registry(
    capability_registry: CapabilityRegistry,
    instance_registry: InstanceRegistry,
    bundle: MCPRuntimeBundle,
) -> None:
    previous_descriptors = [descriptor for descriptor in capability_registry.list() if _is_mcp_descriptor(descriptor)]
    try:
        for descriptor in previous_descriptors:
            capability_registry.unregister(descriptor.capability_id)
        _register_capability_descriptors(
            capability_registry,
            bundle.descriptors,
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


def _validate_project_skill_bundle_startup(
    *,
    public_skill_roots: tuple[str | Path, ...],
    expected_digest: str | None,
    skill_assembly_injected: bool,
    audit_sink: JsonlAuditSink,
    env: Mapping[str, str],
) -> None:
    project_root = Path(public_skill_roots[0]) if public_skill_roots else None
    resolved_expected = expected_digest
    if not skill_assembly_injected and resolved_expected is None:
        configured = env.get(PROJECT_SKILL_BUNDLE_DIGEST_ENV, "").strip()
        resolved_expected = configured or None
    has_project_skills = _project_skill_root_has_manifests(project_root)
    if resolved_expected is None:
        if not skill_assembly_injected and has_project_skills:
            error = ProjectSkillBundleDigestError(
                "project_skill_bundle_digest_required", "missing"
            )
            _record_project_skill_bundle_validation_failure(audit_sink, error)
            raise error
        return
    if project_root is None:
        error = ProjectSkillBundleDigestError(
            "project_skill_bundle_unsafe_entry", "root_missing"
        )
        _record_project_skill_bundle_validation_failure(audit_sink, error)
        raise error
    try:
        result = validate_project_skill_bundle_digest(project_root, resolved_expected)
    except ProjectSkillBundleDigestError as exc:
        _record_project_skill_bundle_validation_failure(audit_sink, exc)
        raise
    audit_sink.record_sync(
        "skill.project_bundle_validated",
        {
            "result": "valid",
            "file_count": result.file_count,
            "total_bytes": result.total_bytes,
            "duration_ms": result.duration_ms,
            "digest_prefix": result.digest[:19],
        },
    )


def _project_skill_root_has_manifests(root: Path | None) -> bool:
    if root is None or not root.is_dir():
        return False
    try:
        return any(path.is_file() for path in root.glob("*/SKILL.md"))
    except OSError:
        return False


def _record_project_skill_bundle_validation_failure(
    audit_sink: JsonlAuditSink,
    error: ProjectSkillBundleDigestError,
) -> None:
    audit_sink.record_sync(
        "skill.project_bundle_validated",
        {
            "result": "failed",
            "code": error.code,
            "reason": error.reason,
        },
    )
