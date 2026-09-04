from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, AsyncContextManager, Protocol
from uuid import uuid4

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError

from src.core.contracts import (
    ConversationStoragePort,
    MCPRemoteTaskStoragePort,
    TaskStoragePort,
    UserMCPConfigurationStoragePort,
)
from src.core.enums import TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import MCPRemoteTaskBinding, UserMCPScopeLease, UserMCPServer

from .adapter_2025_tasks import MCP2025TaskCreatedOutcome
from .adapter_2026 import (
    MCPCompletedOutcome,
    MCPInputRequiredOutcome,
    MCPTaskCreatedOutcome,
)
from .argument_validation import (
    MCPToolArgumentValidationError,
    validate_mcp_tool_arguments,
)
from .client import MCPClientError, MCPProtocolError, MCPRemoteError
from .credentials import MCPRecoveryCallContext
from .endpoint_policy import (
    EndpointPolicyError,
    EndpointPolicyProvenance,
    ValidatedEndpoint,
)
from .gateway_models import (
    CancelOutcome,
    ContinueOutcome,
    MCPCallOutcome,
    MCPCallOutcomeKind,
    MCPCancelStatus,
    MCPContinueStatus,
    MCPTaskServerScope,
    MCPToolDescriptor,
    ToolCatalogSnapshot,
)
from .invalidation import MCPInvalidationAction, MCPServerInvalidated
from .attachment_materialization import MCPJobWorkflowKind
from .job_workflows import extract_ocr_text_projection, run_ocr_async_job_workflow
from .rollout_evidence import (
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
    MCPSafetyRedLine,
)
from .safety_detectors import AuthoritativeMCPSafetyDetector
from .temporary_results import (
    MCPAdmissionLease,
    MCPTemporaryResultCapacity,
    MCPTemporaryResultError,
    MCPTemporaryResultRef,
    MCPTemporaryResultStore,
)
from .result_parsing.models import MCPResultDecodeRequest, MCPResultSource
from .result_parsing.projection_store import (
    MCPProjectionStagingHandle,
    MCPPublishedProjection,
)
from .result_parsing.service import (
    MCPIsolatedResultService,
    MCPResultProjectionCandidate,
    MCPResultWorkerError,
)
from .result_parsing.worker import MCPValidatedResultCheckpoint


SCOPE_LEASE_TTL_SECONDS = 30.0
SCOPE_LEASE_RENEW_INTERVAL_SECONDS = 10.0
CALL_HEARTBEAT_INTERVAL_SECONDS = 120.0
SCOPE_DISCOVERY_TIMEOUT_SECONDS = 60.0
SCOPE_DISCOVERY_RETRY_DELAY_SECONDS = 0.25
_GATEWAY_RECOVERY_NODE_ID = "mcp-gateway"


def _workflow_control_result(
    result: Mapping[str, Any],
) -> Mapping[str, Any]:
    structured = result.get("structuredContent")
    control: dict[str, Any] = {}
    if isinstance(structured, Mapping):
        for key in ("job_id", "status", "result_receipt"):
            value = structured.get(key)
            if isinstance(value, str):
                control[key] = value[:4_096]
        error = structured.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            if isinstance(code, str):
                control["error"] = {"code": code[:256]}
    return {
        "content": [],
        "structuredContent": control,
        "isError": result.get("isError") is True,
    }


class MCPGatewayError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MCPResultTerminalError(MCPRemoteError):
    def __init__(
        self,
        *,
        safe_error_code: str,
        checkpoint: MCPValidatedResultCheckpoint,
    ) -> None:
        super().__init__(
            "MCP Tool result failed validated terminal parsing.",
            remote_code=None,
            retriable=False,
        )
        self.safe_error_code = safe_error_code
        self.checkpoint = checkpoint
        self.terminal_result_source = checkpoint.source


@dataclass(frozen=True, slots=True)
class _ResultParseContext:
    owner_user_id: str
    task_id: str
    node_id: str
    call_ref: str
    protocol_version: str
    output_schema: Mapping[str, Any] | None
    output_schema_sha256: str | None


@dataclass(frozen=True, slots=True)
class _GatewayParsedResult:
    raw_result_ref: MCPTemporaryResultRef
    checkpoint: MCPValidatedResultCheckpoint
    projection_candidate: MCPResultProjectionCandidate | None


class MCPAuthenticatedPrincipal(Protocol):
    username: str


class MCPTaskCallGuard(Protocol):
    """Cluster-capable contract for serializing calls within a platform task."""

    def admit(
        self,
        owner_user_id: str,
        platform_task_id: str,
        call_ref: str,
    ) -> AsyncContextManager[None]: ...


class MCPGatewayMetricRecorder(Protocol):
    async def record_count(
        self,
        metric_name: MCPMetricName,
        *,
        labels: MCPMetricLabels,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
        value: int = 1,
    ) -> Any: ...

    async def record_latency(
        self,
        metric_name: MCPMetricName,
        *,
        duration_seconds: float,
        labels: MCPMetricLabels,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
    ) -> Any: ...

    async def record_gauge(
        self,
        metric_name: MCPMetricName,
        *,
        labels: MCPMetricLabels,
        bucket_started_at: datetime,
        bucket_ended_at: datetime,
        value: int,
    ) -> Any: ...


RemoteTaskCanceller = Callable[
    [MCPRemoteTaskBinding, str], bool | None | Awaitable[bool | None]
]


class MCPGatewayStoragePort(
    UserMCPConfigurationStoragePort,
    ConversationStoragePort,
    TaskStoragePort,
    MCPRemoteTaskStoragePort,
    Protocol,
):
    pass


@dataclass(slots=True)
class _TaskGuardEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


class MCPInMemoryTaskCallGuard:
    """Single-process default; production may inject a storage-backed guard."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _TaskGuardEntry] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def admit(
        self,
        owner_user_id: str,
        platform_task_id: str,
        call_ref: str,
    ) -> AsyncIterator[None]:
        del call_ref
        key = (str(owner_user_id), str(platform_task_id))
        async with self._lock:
            entry = self._entries.setdefault(key, _TaskGuardEntry())
            entry.references += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._lock:
                entry.references -= 1
                if entry.references == 0 and not entry.lock.locked():
                    self._entries.pop(key, None)


@dataclass(frozen=True, slots=True)
class MCPCallCallbacks:
    on_registered: Callable[[str], Awaitable[None] | None] | None = None
    on_heartbeat: Callable[[str], Awaitable[None] | None] | None = None
    on_created: Callable[[str], Awaitable[None] | None] | None = None


@dataclass(slots=True)
class _CallState:
    call_ref: str
    task: asyncio.Task[MCPCallOutcome] | None = None
    remote_request_id: str | int | None = None
    dispatched: bool = False
    start_allowed: asyncio.Event = field(default_factory=asyncio.Event)
    heartbeat_reset: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _ScopeState:
    public: MCPTaskServerScope
    server: UserMCPServer
    adapter: Any
    catalog: ToolCatalogSnapshot
    renew_stop: asyncio.Event
    admission_lease: MCPAdmissionLease
    renew_task: asyncio.Task[None] | None = None
    calls: dict[str, _CallState] = field(default_factory=dict)
    terminal_calls: set[str] = field(default_factory=set)
    accepting_calls: bool = True
    closing: bool = False


class MCPReadonlyShadowSession:
    """Ephemeral discovery-only session isolated from enforce scope state."""

    __slots__ = (
        "_adapter",
        "_admission_lease",
        "_catalog",
        "_cleanup_failed",
        "_close_lock",
        "_close_task",
        "_closed",
        "_endpoint_policy_provenance",
        "_scope",
    )

    def __init__(
        self,
        *,
        scope: MCPTaskServerScope,
        catalog: ToolCatalogSnapshot,
        adapter: Any,
        admission_lease: MCPAdmissionLease,
        endpoint_policy_provenance: EndpointPolicyProvenance,
        cleanup_failed: Callable[[], Awaitable[None]],
        closed: Callable[["MCPReadonlyShadowSession"], Awaitable[None]],
    ) -> None:
        if not isinstance(endpoint_policy_provenance, EndpointPolicyProvenance):
            raise ValueError("endpoint policy provenance must use the closed enum")
        self._scope = scope
        self._catalog = catalog
        self._adapter = adapter
        self._admission_lease = admission_lease
        self._endpoint_policy_provenance = endpoint_policy_provenance
        self._cleanup_failed = cleanup_failed
        self._closed = closed
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def scope(self) -> MCPTaskServerScope:
        return self._scope

    @property
    def catalog(self) -> ToolCatalogSnapshot:
        return self._catalog

    @property
    def endpoint_policy_provenance(self) -> EndpointPolicyProvenance:
        return self._endpoint_policy_provenance

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._close_resources(),
                    name=f"user-mcp-shadow-close:{self._scope.scope_id}",
                )
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close_resources(self) -> None:
        try:
            if not await _safe_close(self._adapter):
                await self._cleanup_failed()
                raise MCPGatewayError("mcp_shadow_cleanup_failed")
        finally:
            try:
                await self._admission_lease.release()
            finally:
                await self._closed(self)


class MCPGateway:
    def __init__(
        self,
        *,
        storage: MCPGatewayStoragePort,
        gateway_instance_id: str,
        credential_loader: Callable[[UserMCPServer], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
        client_factory: Callable[
            [UserMCPServer, Mapping[str, Any], ValidatedEndpoint],
            Any | Awaitable[Any],
        ],
        endpoint_revalidator: Callable[[UserMCPServer], Any | Awaitable[Any]],
        readonly_shadow_client_factory: Callable[
            [UserMCPServer, Mapping[str, Any], ValidatedEndpoint],
            Any | Awaitable[Any],
        ]
        | None = None,
        result_store: MCPTemporaryResultStore,
        capacity: MCPTemporaryResultCapacity,
        now_fn: Callable[[], datetime] | None = None,
        lease_ttl_seconds: float = SCOPE_LEASE_TTL_SECONDS,
        lease_renew_interval_seconds: float = SCOPE_LEASE_RENEW_INTERVAL_SECONDS,
        heartbeat_interval_seconds: float = CALL_HEARTBEAT_INTERVAL_SECONDS,
        result_service: MCPIsolatedResultService | None = None,
        discovery_timeout_seconds: float = SCOPE_DISCOVERY_TIMEOUT_SECONDS,
        discovery_retry_delay_seconds: float = SCOPE_DISCOVERY_RETRY_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        task_call_guard: MCPTaskCallGuard | None = None,
        heartbeat_waiter: Callable[[asyncio.Event, float], Awaitable[bool]] | None = None,
        metric_recorder: MCPGatewayMetricRecorder | None = None,
        metric_routing_mode: MCPMetricRoutingMode | None = None,
        remote_task_canceller: RemoteTaskCanceller | None = None,
        endpoint_security_observer: Callable[
            [UserMCPServer, ValidatedEndpoint], Any | Awaitable[Any]
        ]
        | None = None,
        safety_detectors: Mapping[
            MCPSafetyRedLine, AuthoritativeMCPSafetyDetector
        ]
        | None = None,
    ) -> None:
        self._storage = storage
        self._instance_id = gateway_instance_id
        self._credential_loader = credential_loader
        self._client_factory = client_factory
        self._endpoint_revalidator = endpoint_revalidator
        self._readonly_shadow_client_factory = readonly_shadow_client_factory
        self._result_store = result_store
        self._result_service = result_service
        self._capacity = capacity
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lease_renew_interval_seconds = lease_renew_interval_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._discovery_retry_delay_seconds = discovery_retry_delay_seconds
        self._sleep = sleep
        self._task_call_guard = task_call_guard or MCPInMemoryTaskCallGuard()
        self._heartbeat_waiter = heartbeat_waiter or _wait_for_signal
        if (metric_recorder is None) != (metric_routing_mode is None):
            raise ValueError(
                "metric_recorder and metric_routing_mode must be provided together"
            )
        self._metric_recorder = metric_recorder
        self._metric_routing_mode = metric_routing_mode
        self._remote_task_canceller = remote_task_canceller
        self._endpoint_security_observer = endpoint_security_observer
        self._safety_detectors = dict(safety_detectors or {})
        self._safety_admission_checker: Callable[[], Awaitable[bool]] | None = None
        self._scopes: dict[tuple[str, str], _ScopeState] = {}
        self._opening: dict[tuple[str, str], asyncio.Task[_ScopeState]] = {}
        self._opening_owners: dict[tuple[str, str], str] = {}
        self._terminal_call_tasks: dict[str, str] = {}
        self._closing_tasks: set[str] = set()
        self._metric_scope_dimensions: set[
            tuple[
                MCPMetricTransport,
                MCPMetricProtocolVersion,
                MCPMetricAdapter,
            ]
        ] = set()
        self._metric_dimension_by_scope_id: dict[
            str,
            tuple[
                MCPMetricTransport,
                MCPMetricProtocolVersion,
                MCPMetricAdapter,
            ],
        ] = {}
        self._metric_spill_bytes_by_scope_id: dict[str, int] = {}
        self._readonly_shadow_openings: dict[
            asyncio.Task[MCPReadonlyShadowSession],
            tuple[str, str, str],
        ] = {}
        self._readonly_shadow_sessions: set[MCPReadonlyShadowSession] = set()
        self._lock = asyncio.Lock()
        if metric_recorder is not None and metric_routing_mode is not None:
            self._configure_result_store_metrics()

    def configure_rollout_metrics(
        self,
        metric_recorder: MCPGatewayMetricRecorder,
        routing_mode: MCPMetricRoutingMode,
    ) -> None:
        if not isinstance(routing_mode, MCPMetricRoutingMode):
            raise ValueError("MCP Gateway metric routing mode must be closed")
        self._metric_recorder = metric_recorder
        self._metric_routing_mode = routing_mode
        self._configure_result_store_metrics()

    def configure_remote_task_canceller(
        self, canceller: RemoteTaskCanceller
    ) -> None:
        self._remote_task_canceller = canceller

    def configure_safety_detectors(
        self,
        detectors: Mapping[MCPSafetyRedLine, AuthoritativeMCPSafetyDetector],
    ) -> None:
        self._safety_detectors = dict(detectors)

    def configure_safety_admission_checker(
        self, checker: Callable[[], Awaitable[bool]]
    ) -> None:
        self._safety_admission_checker = checker

    async def _require_safety_admission(self) -> None:
        if (
            self._safety_admission_checker is not None
            and not await self._safety_admission_checker()
        ):
            raise MCPGatewayError("mcp_cp7_runtime_not_ready")

    def attest_safety_interval(
        self, bucket_started_at: datetime, bucket_ended_at: datetime
    ) -> None:
        for red_line in (
            MCPSafetyRedLine.CROSS_USER_ACCESS,
            MCPSafetyRedLine.UNAUTHORIZED_TOOL_CALL,
            MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS,
            MCPSafetyRedLine.SHADOW_TOOL_CALL,
            MCPSafetyRedLine.PERSISTENT_RESOURCE_LEAK,
        ):
            detector = self._safety_detectors.get(red_line)
            if detector is None:
                raise RuntimeError("MCP Gateway safety detector is not configured")
            detector.attest_interval(bucket_started_at, bucket_ended_at)

    async def open_scope(
        self,
        authenticated_user: MCPAuthenticatedPrincipal,
        platform_task_id: str,
        server_id: str,
        *,
        on_queue_entered: Callable[[int], Awaitable[None]] | None = None,
        on_queue_left: Callable[[], Awaitable[None]] | None = None,
    ) -> MCPTaskServerScope:
        await self._require_safety_admission()
        owner_user_id = str(authenticated_user.username)
        await self._require_task_owner(owner_user_id, platform_task_id)
        key = (platform_task_id, server_id)
        async with self._lock:
            if platform_task_id in self._closing_tasks:
                raise MCPGatewayError("mcp_task_not_found")
            existing = self._scopes.get(key)
            if existing is not None:
                if existing.public.owner_user_id != owner_user_id:
                    raise MCPGatewayError("mcp_scope_not_found")
                if existing.accepting_calls:
                    return existing.public
                raise MCPGatewayError("mcp_scope_closed")
            opening = self._opening.get(key)
            if opening is None:
                opening = asyncio.create_task(
                    self._bootstrap_scope(
                        owner_user_id,
                        platform_task_id,
                        server_id,
                        on_queue_entered=on_queue_entered,
                        on_queue_left=on_queue_left,
                    ),
                    name=f"user-mcp-open:{platform_task_id}:{server_id}",
                )
                self._opening[key] = opening
                self._opening_owners[key] = owner_user_id
        try:
            state = await asyncio.shield(opening)
            return state.public
        finally:
            if opening.done():
                async with self._lock:
                    if self._opening.get(key) is opening:
                        self._opening.pop(key, None)
                        self._opening_owners.pop(key, None)

    async def open_readonly_shadow_session(
        self,
        authenticated_user: MCPAuthenticatedPrincipal,
        platform_task_id: str,
        server_id: str,
        *,
        on_queue_entered: Callable[[int], Awaitable[None]] | None = None,
        on_queue_left: Callable[[], Awaitable[None]] | None = None,
    ) -> MCPReadonlyShadowSession:
        """Open an isolated zero-call session without durable runtime mutation."""

        owner_user_id = str(authenticated_user.username)
        async with self._lock:
            if platform_task_id in self._closing_tasks:
                raise MCPGatewayError("mcp_task_not_found")
            opening = asyncio.create_task(
                self._bootstrap_readonly_shadow_session(
                    owner_user_id,
                    platform_task_id,
                    server_id,
                    on_queue_entered=on_queue_entered,
                    on_queue_left=on_queue_left,
                ),
                name=(
                    f"user-mcp-shadow-open:{platform_task_id}:{server_id}:"
                    f"{uuid4().hex}"
                ),
            )
            self._readonly_shadow_openings[opening] = (
                owner_user_id,
                platform_task_id,
                server_id,
            )
        try:
            return await opening
        finally:
            async with self._lock:
                self._readonly_shadow_openings.pop(opening, None)

    async def _bootstrap_readonly_shadow_session(
        self,
        owner_user_id: str,
        platform_task_id: str,
        server_id: str,
        *,
        on_queue_entered: Callable[[int], Awaitable[None]] | None = None,
        on_queue_left: Callable[[], Awaitable[None]] | None = None,
    ) -> MCPReadonlyShadowSession:
        await self._require_task_owner(owner_user_id, platform_task_id)
        server = await self._storage.get_user_mcp_server(owner_user_id, server_id)
        if (
            server is None
            or not server.enabled
            or server.health_status is not UserMCPHealthStatus.AVAILABLE
        ):
            raise MCPGatewayError("mcp_server_unavailable")
        if self._readonly_shadow_client_factory is None:
            raise MCPGatewayError("mcp_readonly_shadow_client_factory_unavailable")

        admission_lease = await self._capacity.acquire(
            owner_user_id,
            f"mcp-shadow-admission-{uuid4().hex}",
            on_queued=on_queue_entered,
            on_admitted=on_queue_left,
        )
        adapter = None
        session: MCPReadonlyShadowSession | None = None
        try:
            for ordinal in range(2):
                candidate = None
                try:
                    async with asyncio.timeout(self._discovery_timeout_seconds):
                        validated_endpoint = await self._revalidate_endpoint(server)
                        if (
                            not isinstance(validated_endpoint, ValidatedEndpoint)
                            or validated_endpoint.normalized_url
                            != server.endpoint_url
                        ):
                            raise MCPGatewayError("endpoint_policy_rejected")
                        await self._observe_endpoint_security(
                            server,
                            validated_endpoint,
                        )
                        credentials = await _await_maybe(
                            self._credential_loader(server)
                        )
                        candidate = await _await_maybe(
                            self._readonly_shadow_client_factory(
                                server,
                                credentials,
                                validated_endpoint,
                            )
                        )
                        await candidate.initialize()
                        if "tools" not in candidate.server_capabilities:
                            raise MCPGatewayError("no_tools_capability")
                        tools = await candidate.list_tools()
                        catalog = _freeze_catalog(server, candidate, tools)
                        if not catalog.tools:
                            raise MCPGatewayError("empty_tool_list")
                        adapter = candidate
                        scope = MCPTaskServerScope(
                            scope_id=f"mcp-shadow-scope-{uuid4().hex}",
                            owner_user_id=owner_user_id,
                            platform_task_id=platform_task_id,
                            server_id=server_id,
                            config_version=server.config_version,
                            security_version=server.security_version,
                        )
                        session = MCPReadonlyShadowSession(
                            scope=scope,
                            catalog=catalog,
                            adapter=adapter,
                            admission_lease=admission_lease,
                            endpoint_policy_provenance=(
                                validated_endpoint.policy_provenance
                            ),
                            cleanup_failed=lambda: self._record_cleanup_failure(
                                server, adapter
                            ),
                            closed=self._discard_readonly_shadow_session,
                        )
                        async with self._lock:
                            self._readonly_shadow_sessions.add(session)
                        return session
                except asyncio.CancelledError:
                    raise
                except TimeoutError as exc:
                    error: BaseException = MCPGatewayError("discovery_timeout")
                    error.__cause__ = exc
                    retriable = True
                except MCPClientError as exc:
                    error = exc
                    retriable = bool(exc.retriable) and exc.mcp_error_code not in {
                        "mcp_auth_required",
                        "mcp_scope_required",
                        "mcp_protocol_error",
                    }
                except BaseException as exc:
                    error = exc
                    retriable = False
                finally:
                    if candidate is not None and candidate is not adapter:
                        if not await _safe_close(candidate):
                            await self._record_cleanup_failure(server, candidate)
                            raise MCPGatewayError("mcp_shadow_cleanup_failed")
                if ordinal == 0 and retriable:
                    await self._sleep(self._discovery_retry_delay_seconds)
                    continue
                raise error
            raise AssertionError("unreachable")
        except BaseException:
            if session is not None:
                await session.aclose()
            else:
                if adapter is not None and not await _safe_close(adapter):
                    await self._record_cleanup_failure(server, adapter)
                await admission_lease.release()
            raise

    async def _bootstrap_scope(
        self,
        owner_user_id: str,
        platform_task_id: str,
        server_id: str,
        *,
        on_queue_entered: Callable[[int], Awaitable[None]] | None = None,
        on_queue_left: Callable[[], Awaitable[None]] | None = None,
    ) -> _ScopeState:
        server = await self._storage.get_user_mcp_server(owner_user_id, server_id)
        if (
            server is None
            or not server.enabled
            or server.health_status is not UserMCPHealthStatus.AVAILABLE
        ):
            raise MCPGatewayError("mcp_server_unavailable")
        admission_ref = f"mcp-admission-{uuid4().hex}"
        admission_lease = await self._capacity.acquire(
            owner_user_id,
            admission_ref,
            on_queued=on_queue_entered,
            on_admitted=on_queue_left,
        )
        now = self._now()
        scope = MCPTaskServerScope(
            scope_id=f"mcp-scope-{uuid4().hex}",
            owner_user_id=owner_user_id,
            platform_task_id=platform_task_id,
            server_id=server_id,
            config_version=server.config_version,
            security_version=server.security_version,
        )
        lease = UserMCPScopeLease(
            scope_id=scope.scope_id,
            owner_user_id=owner_user_id,
            server_id=server_id,
            security_version=server.security_version,
            gateway_instance_id=self._instance_id,
            lease_expires_at=now + timedelta(seconds=self._lease_ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        try:
            lease_acquired = await self._storage.acquire_user_mcp_scope_lease(lease)
        except BaseException:
            await admission_lease.release()
            raise
        if not lease_acquired:
            await admission_lease.release()
            raise MCPGatewayError("mcp_scope_lease_unavailable")
        adapter = None
        bootstrap_renew_stop = asyncio.Event()
        bootstrap_lease_lost = asyncio.Event()
        bootstrap_renewer = asyncio.create_task(
            self._renew_bootstrap_scope(
                scope, bootstrap_renew_stop, bootstrap_lease_lost
            ),
            name=f"user-mcp-open-renew:{scope.scope_id}",
        )
        initialization: asyncio.Task[tuple[Any, ToolCatalogSnapshot]] | None = None
        lost_wait: asyncio.Task[bool] | None = None
        try:
            async def initialize_scope() -> tuple[Any, ToolCatalogSnapshot]:
                nonlocal adapter
                for ordinal in range(2):
                    candidate = None
                    connected = False
                    error: BaseException | None = None
                    connect_started_at = monotonic()
                    try:
                        async with asyncio.timeout(self._discovery_timeout_seconds):
                            validated_endpoint = await self._revalidate_endpoint(server)
                            if (
                                not isinstance(validated_endpoint, ValidatedEndpoint)
                                or validated_endpoint.normalized_url
                                != server.endpoint_url
                            ):
                                raise MCPGatewayError("endpoint_policy_rejected")
                            await self._observe_endpoint_security(
                                server,
                                validated_endpoint,
                            )
                            credentials = await _await_maybe(
                                self._credential_loader(server)
                            )
                            candidate = await _await_maybe(
                                self._client_factory(
                                    server,
                                    credentials,
                                    validated_endpoint,
                                )
                            )
                            await candidate.initialize()
                            connected = True
                            await self._record_gateway_latency(
                                MCPMetricName.GATEWAY_CONNECT_DURATION_SECONDS,
                                server=server,
                                adapter=candidate,
                                duration_seconds=max(
                                    0.0, monotonic() - connect_started_at
                                ),
                                result_category=MCPMetricResultCategory.SUCCEEDED,
                                error_category=MCPMetricErrorCategory.NONE,
                            )
                            await self._record_gateway_count(
                                MCPMetricName.PROTOCOL_NEGOTIATION_TOTAL,
                                server=server,
                                adapter=candidate,
                                result_category=MCPMetricResultCategory.SUCCEEDED,
                                error_category=MCPMetricErrorCategory.NONE,
                            )
                            capabilities = candidate.server_capabilities
                            if "tools" not in capabilities:
                                await self._mark_unavailable(
                                    server, "no_tools_capability"
                                )
                                raise MCPGatewayError("no_tools_capability")
                            tools_list_started_at = monotonic()
                            try:
                                tools = await candidate.list_tools()
                            except BaseException as exc:
                                await self._record_tools_list_attempt(
                                    server=server,
                                    adapter=candidate,
                                    duration_seconds=max(
                                        0.0,
                                        monotonic() - tools_list_started_at,
                                    ),
                                    result_category=(
                                        MCPMetricResultCategory.CANCELLED
                                        if isinstance(exc, asyncio.CancelledError)
                                        else MCPMetricResultCategory.FAILED
                                    ),
                                    error_category=_metric_error_category(exc),
                                )
                                raise
                            await self._record_tools_list_attempt(
                                server=server,
                                adapter=candidate,
                                duration_seconds=max(
                                    0.0, monotonic() - tools_list_started_at
                                ),
                                result_category=MCPMetricResultCategory.SUCCEEDED,
                                error_category=MCPMetricErrorCategory.NONE,
                            )
                            catalog = _freeze_catalog(server, candidate, tools)
                            if not catalog.tools:
                                await self._mark_unavailable(server, "empty_tool_list")
                                raise MCPGatewayError("empty_tool_list")
                            adapter = candidate
                            return candidate, catalog
                    except asyncio.CancelledError as exc:
                        error = exc
                        retriable = False
                    except TimeoutError as exc:
                        error: Exception = MCPGatewayError("discovery_timeout")
                        error.__cause__ = exc
                        retriable = True
                    except MCPClientError as exc:
                        error = exc
                        retriable = bool(exc.retriable) and exc.mcp_error_code not in {
                            "mcp_auth_required",
                            "mcp_scope_required",
                            "mcp_protocol_error",
                        }
                    except BaseException as exc:
                        error = exc
                        retriable = False
                    finally:
                        if not connected:
                            result_category = (
                                MCPMetricResultCategory.CANCELLED
                                if isinstance(error, asyncio.CancelledError)
                                else MCPMetricResultCategory.FAILED
                            )
                            error_category = (
                                MCPMetricErrorCategory.NONE
                                if isinstance(error, asyncio.CancelledError)
                                else (
                                    _metric_error_category(error)
                                    if error is not None
                                    else MCPMetricErrorCategory.UNKNOWN
                                )
                            )
                            await self._record_gateway_latency(
                                MCPMetricName.GATEWAY_CONNECT_DURATION_SECONDS,
                                server=server,
                                adapter=candidate,
                                duration_seconds=max(
                                    0.0, monotonic() - connect_started_at
                                ),
                                result_category=result_category,
                                error_category=error_category,
                            )
                            await self._record_gateway_count(
                                MCPMetricName.PROTOCOL_NEGOTIATION_TOTAL,
                                server=server,
                                adapter=candidate,
                                result_category=result_category,
                                error_category=error_category,
                            )
                        if candidate is not None and candidate is not adapter:
                            if not await _safe_close(candidate):
                                await self._record_cleanup_failure(server, candidate)
                    if ordinal == 0 and retriable:
                        await self._sleep(self._discovery_retry_delay_seconds)
                        continue
                    raise error
                raise AssertionError("unreachable")

            initialization = asyncio.create_task(
                initialize_scope(), name=f"user-mcp-open-discovery:{scope.scope_id}"
            )
            lost_wait = asyncio.create_task(bootstrap_lease_lost.wait())
            done, _ = await asyncio.wait(
                {initialization, lost_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if lost_wait in done and bootstrap_lease_lost.is_set():
                initialization.cancel()
                await asyncio.gather(initialization, return_exceptions=True)
                raise MCPGatewayError("mcp_scope_lease_lost")
            lost_wait.cancel()
            await asyncio.gather(lost_wait, return_exceptions=True)
            adapter, catalog = await initialization
            bootstrap_renew_stop.set()
            bootstrap_renewer.cancel()
            await asyncio.gather(bootstrap_renewer, return_exceptions=True)
            state = _ScopeState(
                public=scope,
                server=server,
                adapter=adapter,
                catalog=catalog,
                renew_stop=asyncio.Event(),
                admission_lease=admission_lease,
            )
            state.renew_task = asyncio.create_task(
                self._renew_scope(state), name=f"user-mcp-renew:{scope.scope_id}"
            )
            async with self._lock:
                self._scopes[(platform_task_id, server_id)] = state
                self._metric_scope_dimensions.add(
                    _metric_dimension(server, adapter)
                )
                self._metric_dimension_by_scope_id[scope.scope_id] = (
                    _metric_dimension(server, adapter)
                )
            await self._record_active_scope_gauge()
            if not await self._renew_scope_lease_once(scope):
                await self.close_scope(scope, "lease_lost_before_open")
                raise MCPGatewayError("mcp_scope_lease_lost")
            return state
        except BaseException:
            for pending in (initialization, lost_wait):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(pending for pending in (initialization, lost_wait) if pending is not None),
                return_exceptions=True,
            )
            bootstrap_renew_stop.set()
            bootstrap_renewer.cancel()
            await asyncio.gather(bootstrap_renewer, return_exceptions=True)
            if adapter is not None:
                if not await _safe_close(adapter):
                    await self._record_cleanup_failure(server, adapter)
            try:
                await self._storage.release_user_mcp_scope_lease(
                    scope.scope_id, gateway_instance_id=self._instance_id
                )
            finally:
                await admission_lease.release()
            raise

    async def list_tools(self, scope: MCPTaskServerScope) -> ToolCatalogSnapshot:
        return self._require_scope(scope).catalog

    async def verify_durable_result(
        self,
        scope: MCPTaskServerScope,
        *,
        node_id: str,
        call_ref: str,
        result_ref: str,
        size_bytes: int,
        sha256: str,
        store_kind: str,
    ) -> MCPTemporaryResultRef:
        state = self._require_scope(scope)
        if not sha256.startswith("sha256:"):
            raise MCPTemporaryResultError(
                "Durable MCP result digest must use the sha256 prefix."
            )
        return await self._result_store.verify_durable_ref(
            result_ref,
            owner_user_id=state.public.owner_user_id,
            task_id=state.public.platform_task_id,
            node_id=node_id,
            call_ref=call_ref,
            scope_id=state.public.scope_id,
            expected_size_bytes=size_bytes,
            expected_sha256=sha256.removeprefix("sha256:"),
            expected_store_kind=store_kind,
        )

    async def call_tool(
        self,
        scope: MCPTaskServerScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        callbacks: MCPCallCallbacks | None = None,
        *,
        node_id: str | None = None,
        input_responses: Mapping[str, Any] | None = None,
        sealed_request_state_ref: str | None = None,
        continuation_plan: Mapping[str, Any] | None = None,
        pending_action_id: str | None = None,
        arguments_payload_ref: str | None = None,
        arguments_sha256: str | None = None,
        authorization_verified: bool = False,
        workflow_kind: MCPJobWorkflowKind | None = None,
    ) -> MCPCallOutcome:
        await self._require_safety_admission()
        state = self._require_scope(scope)
        if self._safety_detectors and not authorization_verified:
            await self._report_safety_violation(
                MCPSafetyRedLine.UNAUTHORIZED_TOOL_CALL,
                "permission_denied_boundary",
            )
            raise MCPGatewayError("mcp_tool_authorization_required")
        if not state.accepting_calls:
            raise MCPGatewayError("mcp_scope_closed")
        descriptor = state.catalog.get(tool_name)
        if descriptor is None:
            raise MCPGatewayError("mcp_tool_not_found")
        try:
            validate_mcp_tool_arguments(descriptor.input_schema, arguments)
        except MCPToolArgumentValidationError as exc:
            raise MCPGatewayError("mcp_tool_arguments_invalid") from exc
        call_ref = f"mcp-call-{uuid4().hex}"
        call_state = _CallState(call_ref=call_ref)
        state.calls[call_ref] = call_state
        await self._record_active_call_gauge()
        call_task = asyncio.create_task(
            self._execute_call(
                state,
                call_state,
                tool_name,
                arguments,
                callbacks,
                node_id=node_id,
                input_responses=input_responses,
                sealed_request_state_ref=sealed_request_state_ref,
                continuation_plan=continuation_plan,
                pending_action_id=pending_action_id,
                arguments_payload_ref=arguments_payload_ref,
                arguments_sha256=arguments_sha256,
                workflow_kind=workflow_kind,
            ),
            name=f"user-mcp-call:{call_ref}",
        )
        call_state.task = call_task
        if callbacks is not None and callbacks.on_created is not None:
            try:
                await _await_maybe(callbacks.on_created(call_ref))
            except BaseException:
                call_task.cancel()
                await asyncio.gather(call_task, return_exceptions=True)
                state.calls.pop(call_ref, None)
                await self._record_active_call_gauge()
                raise
        call_state.start_allowed.set()
        try:
            return await call_task
        finally:
            state.calls.pop(call_ref, None)
            await self._record_active_call_gauge()
            state.terminal_calls.add(call_ref)
            self._terminal_call_tasks[call_ref] = state.public.platform_task_id
            if len(self._terminal_call_tasks) > 4096:
                self._terminal_call_tasks.pop(next(iter(self._terminal_call_tasks)))

    async def _execute_call(
        self,
        state: _ScopeState,
        call_state: _CallState,
        tool_name: str,
        arguments: Mapping[str, Any],
        callbacks: MCPCallCallbacks | None,
        *,
        node_id: str | None,
        input_responses: Mapping[str, Any] | None,
        sealed_request_state_ref: str | None,
        continuation_plan: Mapping[str, Any] | None,
        pending_action_id: str | None,
        arguments_payload_ref: str | None,
        arguments_sha256: str | None,
        workflow_kind: MCPJobWorkflowKind | None,
    ) -> MCPCallOutcome:
        await call_state.start_allowed.wait()
        async with self._task_call_guard.admit(
            state.public.owner_user_id,
            state.public.platform_task_id,
            call_state.call_ref,
        ):
            if not state.accepting_calls:
                raise MCPGatewayError("mcp_scope_closed")
            task = await self._storage.get_task(state.public.platform_task_id)
            if (
                MCPSafetyRedLine.SHADOW_TOOL_CALL in self._safety_detectors
                and (
                    task is None
                    or bool(getattr(task, "mcp_shadow_enabled", False))
                    or getattr(task, "mcp_execution_mode", None) != "user_scoped"
                )
            ):
                await self._report_safety_violation(
                    MCPSafetyRedLine.SHADOW_TOOL_CALL,
                    "shadow_call_blocked",
                )
                raise MCPGatewayError("mcp_shadow_tool_call_forbidden")
            sink = self._result_store.create_sink(
                state.public.platform_task_id,
                scope_id=state.public.scope_id,
                durable=True,
                owner_user_id=state.public.owner_user_id,
                node_id=node_id or _GATEWAY_RECOVERY_NODE_ID,
                call_ref=call_state.call_ref,
            )
            tool_descriptor = state.catalog.get(tool_name)
            if tool_descriptor is None:
                await sink.abort()
                raise MCPGatewayError("mcp_tool_not_found")
            business_result_descriptor = tool_descriptor
            if workflow_kind is MCPJobWorkflowKind.OCR_ASYNC_JOB_V1:
                final_descriptor = state.catalog.get("get_parse_job")
                if final_descriptor is None:
                    await sink.abort()
                    raise MCPGatewayError(
                        "mcp_job_workflow_final_tool_missing"
                    )
                business_result_descriptor = final_descriptor
            result_context = _ResultParseContext(
                owner_user_id=state.public.owner_user_id,
                task_id=state.public.platform_task_id,
                node_id=node_id or _GATEWAY_RECOVERY_NODE_ID,
                call_ref=call_state.call_ref,
                protocol_version=state.catalog.effective_protocol_version,
                output_schema=business_result_descriptor.output_schema,
                output_schema_sha256=(
                    business_result_descriptor.output_schema_sha256
                ),
            )

            def registered(request_id: str | int) -> None:
                call_state.remote_request_id = request_id

            if callbacks is not None and callbacks.on_registered is not None:
                await _await_maybe(callbacks.on_registered(call_state.call_ref))
            call_state.dispatched = True
            call_kwargs: dict[str, Any] = {
                "request_registered_callback": registered,
                "result_sink": sink,
            }
            if bool(
                getattr(
                    state.adapter,
                    "supports_durable_recovery_context",
                    False,
                )
            ):
                call_kwargs.update(
                    recovery_context=MCPRecoveryCallContext(
                        owner_user_id=state.public.owner_user_id,
                        task_id=state.public.platform_task_id,
                        node_id=node_id or _GATEWAY_RECOVERY_NODE_ID,
                        call_ref=call_state.call_ref,
                        continuation_plan=continuation_plan,
                        pending_action_id=pending_action_id,
                        arguments_payload_ref=arguments_payload_ref,
                        arguments_sha256=arguments_sha256,
                    ),
                    input_responses=input_responses,
                    sealed_request_state_ref=sealed_request_state_ref,
                )
            if workflow_kind is None:
                invocation = asyncio.create_task(
                    state.adapter.call_tool(tool_name, arguments, **call_kwargs)
                )
            elif workflow_kind is MCPJobWorkflowKind.OCR_ASYNC_JOB_V1:
                if tool_name != "start_parse_job":
                    await sink.abort()
                    raise MCPGatewayError("mcp_job_workflow_tool_invalid")
                workflow_call_sequence = 0

                async def workflow_tool_invoker(
                    internal_tool_name: str,
                    internal_arguments: Mapping[str, Any],
                    internal_registered_callback: Callable[[str | int], None]
                    | None,
                ) -> Mapping[str, Any]:
                    nonlocal workflow_call_sequence
                    workflow_call_sequence += 1
                    kwargs: dict[str, Any] = {}
                    if internal_registered_callback is not None:
                        kwargs["request_registered_callback"] = (
                            internal_registered_callback
                        )
                    internal_result = await state.adapter.call_tool(
                        internal_tool_name,
                        dict(internal_arguments),
                        **kwargs,
                    )
                    if not isinstance(internal_result, Mapping):
                        raise MCPGatewayError(
                            "mcp_job_workflow_control_result_invalid"
                        )
                    normalized_result = dict(internal_result)
                    if self._result_service is None:
                        raise MCPResultWorkerError(
                            "mcp_result_parser_service_unavailable"
                        )
                    descriptor = state.catalog.get(internal_tool_name)
                    encoded = json.dumps(
                        normalized_result,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    parser_payload: Mapping[str, Any] = normalized_result
                    output_schema = (
                        None if descriptor is None else descriptor.output_schema
                    )
                    output_schema_sha256 = (
                        None
                        if descriptor is None
                        else descriptor.output_schema_sha256
                    )
                    if len(encoded) > 64 * 1024:
                        parser_payload = _workflow_control_result(normalized_result)
                        encoded = json.dumps(
                            parser_payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        output_schema = None
                        output_schema_sha256 = None
                    checked = await self._result_service.parse(
                        owner_user_id=state.public.owner_user_id,
                        task_id=state.public.platform_task_id,
                        node_id=node_id or _GATEWAY_RECOVERY_NODE_ID,
                        call_ref=(
                            f"{call_state.call_ref}:workflow:"
                            f"{workflow_call_sequence}"
                        ),
                        request=MCPResultDecodeRequest(
                            protocol_version=(
                                state.catalog.effective_protocol_version
                            ),
                            source=MCPResultSource.TOOLS_CALL,
                            payload=parser_payload,
                            output_schema=output_schema,
                            output_schema_sha256=output_schema_sha256,
                        ),
                        measured_mapping_bytes=len(encoded),
                    )
                    if checked.checkpoint.outcome == "malformed":
                        raise MCPProtocolError(
                            "MCP workflow control result is malformed."
                        )
                    if (
                        checked.checkpoint.outcome == "succeeded"
                        and checked.projection_candidate is None
                    ):
                        raise MCPProtocolError(
                            "MCP workflow control result is unavailable."
                        )
                    return normalized_result

                async def persist_workflow_result(
                    workflow_result: Mapping[str, Any],
                ) -> MCPCallOutcome:
                    encoded = json.dumps(
                        dict(workflow_result),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    await sink.write(encoded)
                    persisted = await sink.finalize()
                    result_with_descriptor = {
                        **dict(workflow_result),
                        "_mcpResultRef": persisted.as_payload(),
                    }
                    normalized = await self._normalize_outcome(
                        result_with_descriptor,
                        sink,
                        external_text=extract_ocr_text_projection(
                            workflow_result
                        ),
                        result_context=result_context,
                    )
                    if normalized.kind is not MCPCallOutcomeKind.COMPLETED:
                        raise MCPProtocolError(
                            "MCP workflow final result is not completed."
                        )
                    return normalized

                invocation = asyncio.create_task(
                    run_ocr_async_job_workflow(
                        state.adapter,
                        arguments,
                        request_registered_callback=registered,
                        sleep=self._sleep,
                        result_persisted_callback=persist_workflow_result,
                        tool_invoker=workflow_tool_invoker,
                    )
                )
            else:
                await sink.abort()
                raise MCPGatewayError("mcp_job_workflow_unsupported")
            heartbeat = asyncio.create_task(
                self._heartbeat(call_state, invocation, callbacks)
            )
            try:
                raw = await invocation
            except BaseException:
                await sink.abort()
                raise
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        if not state.accepting_calls:
            raise MCPGatewayError("mcp_scope_closed")
        outcome = (
            raw
            if isinstance(raw, MCPCallOutcome)
            else await self._normalize_outcome(
                raw, sink, result_context=result_context
            )
        )
        if not state.accepting_calls:
            raise MCPGatewayError("mcp_scope_closed")
        return outcome

    async def _normalize_outcome(
        self,
        raw: Any,
        sink: Any,
        *,
        external_text: str | None = None,
        result_context: _ResultParseContext,
    ) -> MCPCallOutcome:
        if isinstance(raw, MCPInputRequiredOutcome):
            await sink.abort()
            requests = tuple(dict(value) for value in raw.input_requests.values())
            return MCPCallOutcome.input_required(
                requests,
                raw.sealed_request_state_ref,
            )
        if isinstance(raw, MCPTaskCreatedOutcome | MCP2025TaskCreatedOutcome):
            await sink.abort()
            return MCPCallOutcome.task_created(raw.safe_remote_task_ref, status=raw.status)
        result = raw.result if isinstance(raw, MCPCompletedOutcome) else raw
        if not isinstance(result, Mapping):
            await sink.abort()
            raise MCPProtocolError("MCP Tool result must be a JSON object.")
        if self._result_service is None:
            await sink.abort()
            raise MCPResultWorkerError("mcp_result_parser_service_unavailable")
        try:
            parsed = await self._parse_completed_result(
                result,
                result_context=result_context,
            )
        except BaseException:
            try:
                await sink.abort()
            except RuntimeError:
                pass
            raise
        if parsed.checkpoint.outcome != "succeeded":
            await self._result_store.discard(parsed.raw_result_ref)
            raise MCPResultTerminalError(
                safe_error_code=(
                    "mcp_tool_error"
                    if parsed.checkpoint.outcome == "tool_error"
                    else "mcp_result_malformed"
                ),
                checkpoint=parsed.checkpoint,
            )
        return MCPCallOutcome.completed(
            parsed.raw_result_ref.ref,
            content_type="application/json",
            byte_size=parsed.raw_result_ref.size_bytes,
            result_content_sha256=parsed.checkpoint.raw_sha256,
            result_store_kind="durable_content_addressed",
            external_text=external_text,
            terminal_result_source=parsed.checkpoint.source,
            validated_checkpoint=parsed.checkpoint,
            projection_candidate=parsed.projection_candidate,
        )

    async def _parse_completed_result(
        self,
        result: Mapping[str, Any],
        *,
        result_context: _ResultParseContext,
    ) -> _GatewayParsedResult:
        embedded = result.get("_mcpResultRef")
        if not isinstance(embedded, Mapping):
            raise MCPResultWorkerError(
                "mcp_result_live_payload_descriptor_required"
            )
        ref_value = embedded.get("ref")
        size_value = embedded.get("sizeBytes")
        sha_value = embedded.get("sha256")
        storage_value = embedded.get("storage")
        if (
            not isinstance(ref_value, str)
            or isinstance(size_value, bool)
            or not isinstance(size_value, int)
            or not isinstance(sha_value, str)
            or len(sha_value) != 64
            or any(character not in "0123456789abcdef" for character in sha_value)
            or storage_value != "file"
        ):
            raise MCPResultWorkerError("mcp_result_raw_descriptor_invalid")
        resolved = self._result_store.resolve_ref(ref_value)
        if (
            resolved.size_bytes != size_value
            or resolved.sha256 != sha_value
            or resolved.storage != "file"
        ):
            raise MCPResultWorkerError("mcp_result_raw_descriptor_invalid")
        descriptor = self._result_store.result_parser_descriptor(resolved)
        assert self._result_service is not None
        parsed = await self._result_service.parse(
            owner_user_id=result_context.owner_user_id,
            task_id=result_context.task_id,
            node_id=result_context.node_id,
            call_ref=result_context.call_ref,
            request=MCPResultDecodeRequest(
                protocol_version=result_context.protocol_version,
                source=MCPResultSource.TOOLS_CALL,
                payload=descriptor,
                output_schema=result_context.output_schema,
                output_schema_sha256=result_context.output_schema_sha256,
            ),
        )
        if parsed.checkpoint.raw_sha256 != "sha256:" + resolved.sha256:
            raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
        return _GatewayParsedResult(
            raw_result_ref=resolved,
            checkpoint=parsed.checkpoint,
            projection_candidate=parsed.projection_candidate,
        )

    async def finalize_result_assets(
        self,
        outcome: MCPCallOutcome,
        *,
        model_edition: str,
    ) -> tuple[MCPPublishedProjection | None, MCPProjectionStagingHandle | None]:
        if not outcome.result_ref:
            return None, None
        result_ref = self._result_store.resolve_ref(outcome.result_ref)
        self._result_store.mark_promoted(result_ref)
        if (
            outcome.projection_candidate is None
            or self._result_service is None
        ):
            return None, None
        handle = await self._result_service.stage_projection(
            outcome.projection_candidate,
            model_edition=model_edition,
        )
        try:
            published = self._result_service.publish_projection(handle)
        except BaseException:
            self._result_service.discard_projection(handle)
            raise
        return published, handle

    async def continue_call(
        self, scope: MCPTaskServerScope, call_ref: str
    ) -> ContinueOutcome:
        state = self._require_scope(scope)
        call = state.calls.get(call_ref)
        if call is not None and call.task is not None and not call.task.done():
            call.heartbeat_reset.set()
            return ContinueOutcome(MCPContinueStatus.RESET)
        if self._terminal_call_tasks.get(call_ref) == scope.platform_task_id or (
            call is not None and call.task is not None and call.task.done()
        ):
            return ContinueOutcome(MCPContinueStatus.ALREADY_TERMINAL)
        return ContinueOutcome(MCPContinueStatus.UNKNOWN_CALL)

    async def continue_call_for_task(
        self, platform_task_id: str, call_ref: str
    ) -> ContinueOutcome:
        state = self._find_call_scope(platform_task_id, call_ref)
        if state is not None:
            return await self.continue_call(state.public, call_ref)
        if self._terminal_call_tasks.get(call_ref) == platform_task_id:
            return ContinueOutcome(MCPContinueStatus.ALREADY_TERMINAL)
        return ContinueOutcome(MCPContinueStatus.UNKNOWN_CALL)

    async def cancel_call(
        self, scope: MCPTaskServerScope, call_ref: str, reason: str
    ) -> CancelOutcome:
        state = self._require_scope(scope)
        call = state.calls.get(call_ref)
        if call is None:
            if self._terminal_call_tasks.get(call_ref) == scope.platform_task_id:
                return CancelOutcome(MCPCancelStatus.ALREADY_TERMINAL, True)
            return CancelOutcome(MCPCancelStatus.UNKNOWN_CALL, False)
        if call.task is not None and call.task.done():
            return CancelOutcome(MCPCancelStatus.ALREADY_TERMINAL, True)
        if not call.dispatched:
            if call.task is not None:
                call.task.cancel()
                await asyncio.gather(call.task, return_exceptions=True)
            return CancelOutcome(MCPCancelStatus.CANCELLED, True)
        cancel_request = getattr(state.adapter, "cancel_request", None)
        if callable(cancel_request) and call.remote_request_id is not None:
            try:
                confirmed = await cancel_request(
                    call.remote_request_id, reason=reason
                )
                if confirmed is True:
                    if call.task is not None:
                        call.task.cancel()
                    return CancelOutcome(MCPCancelStatus.CANCELLED, True)
            except Exception:
                pass
        await self.close_scope(scope, reason)
        return CancelOutcome(MCPCancelStatus.REMOTE_STOP_UNKNOWN, False)

    async def cancel_call_for_task(
        self, platform_task_id: str, call_ref: str, reason: str
    ) -> CancelOutcome:
        state = self._find_call_scope(platform_task_id, call_ref)
        if state is not None:
            return await self.cancel_call(state.public, call_ref, reason)
        durable = await self._durable_remote_task_for_call(
            platform_task_id, call_ref
        )
        if durable is not None:
            if durable.terminal_at is not None:
                return CancelOutcome(MCPCancelStatus.ALREADY_TERMINAL, True)
            if self._remote_task_canceller is None:
                return CancelOutcome(MCPCancelStatus.REMOTE_STOP_UNKNOWN, False)
            try:
                confirmed = await _await_maybe(
                    self._remote_task_canceller(durable, reason)
                )
            except Exception:
                confirmed = None
            if confirmed is True:
                return CancelOutcome(MCPCancelStatus.CANCELLED, True)
            return CancelOutcome(MCPCancelStatus.REMOTE_STOP_UNKNOWN, False)
        if self._terminal_call_tasks.get(call_ref) == platform_task_id:
            return CancelOutcome(MCPCancelStatus.ALREADY_TERMINAL, True)
        return CancelOutcome(MCPCancelStatus.UNKNOWN_CALL, False)

    async def _durable_remote_task_for_call(
        self, platform_task_id: str, call_ref: str
    ) -> MCPRemoteTaskBinding | None:
        task = await self._storage.get_task(platform_task_id)
        if task is None:
            return None
        conversation = await self._storage.get_conversation(task.conversation_id)
        if conversation is None or not conversation.username:
            return None
        return await self._storage.get_mcp_remote_task_binding_for_call(
            conversation.username,
            platform_task_id,
            call_ref,
        )

    async def close_scope(self, scope: MCPTaskServerScope, reason: str) -> None:
        del reason
        key = (scope.platform_task_id, scope.server_id)
        async with self._lock:
            state = self._scopes.get(key)
            if state is None or state.public.scope_id != scope.scope_id or state.closing:
                return
            state.closing = True
            state.accepting_calls = False
        current = asyncio.current_task()
        pending = [call.task for call in state.calls.values() if call.task and call.task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            if not await _safe_close(state.adapter):
                await self._record_cleanup_failure(state.server, state.adapter)
        finally:
            state.renew_stop.set()
            if state.renew_task is not None and state.renew_task is not current:
                state.renew_task.cancel()
                await asyncio.gather(state.renew_task, return_exceptions=True)
            try:
                await self._storage.release_user_mcp_scope_lease(
                    scope.scope_id, gateway_instance_id=self._instance_id
                )
            finally:
                await state.admission_lease.release()
                async with self._lock:
                    if self._scopes.get(key) is state:
                        self._scopes.pop(key, None)
                await self._record_active_scope_gauge()

    async def close_task(self, platform_task_id: str, reason: str) -> None:
        async with self._lock:
            self._closing_tasks.add(platform_task_id)
            openings = [
                (key, task)
                for key, task in self._opening.items()
                if key[0] == platform_task_id
            ]
            shadow_openings = [
                task
                for task, (_, task_id, _) in self._readonly_shadow_openings.items()
                if task_id == platform_task_id
            ]
            shadow_sessions = tuple(
                session
                for session in self._readonly_shadow_sessions
                if session.scope.platform_task_id == platform_task_id
            )
        try:
            for _, task in openings:
                task.cancel()
            for task in shadow_openings:
                task.cancel()
            if openings:
                await asyncio.gather(
                    *(task for _, task in openings), return_exceptions=True
                )
            if shadow_openings:
                await asyncio.gather(*shadow_openings, return_exceptions=True)
            async with self._lock:
                for key, task in openings:
                    if self._opening.get(key) is task:
                        self._opening.pop(key, None)
                        self._opening_owners.pop(key, None)
            scopes = [
                state.public
                for (task_id, _), state in tuple(self._scopes.items())
                if task_id == platform_task_id
            ]
            await asyncio.gather(
                *(self.close_scope(scope, reason) for scope in scopes),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(session.aclose() for session in shadow_sessions),
                return_exceptions=True,
            )
            try:
                await self._result_store.cleanup_task(platform_task_id)
            except BaseException:
                await self._record_cleanup_failure()
                raise
            finally:
                for scope in scopes:
                    self._metric_dimension_by_scope_id.pop(scope.scope_id, None)
                    self._metric_spill_bytes_by_scope_id.pop(scope.scope_id, None)
        finally:
            async with self._lock:
                self._closing_tasks.discard(platform_task_id)

    async def invalidate_server(self, event: MCPServerInvalidated) -> None:
        async with self._lock:
            openings = [
                (key, task)
                for key, task in self._opening.items()
                if key[1] == event.server_id
                and self._opening_owners.get(key) == event.owner_user_id
                and event.action
                in {MCPInvalidationAction.DISABLED, MCPInvalidationAction.DELETED}
            ]
            shadow_openings = [
                task
                for task, (owner_user_id, _, server_id) in (
                    self._readonly_shadow_openings.items()
                )
                if owner_user_id == event.owner_user_id
                and server_id == event.server_id
            ]
            shadow_sessions = tuple(
                session
                for session in self._readonly_shadow_sessions
                if session.scope.owner_user_id == event.owner_user_id
                and session.scope.server_id == event.server_id
                and (
                    event.action
                    in {MCPInvalidationAction.DISABLED, MCPInvalidationAction.DELETED}
                    or session.scope.security_version < event.security_version
                )
            )
        for _, task in openings:
            task.cancel()
        for task in shadow_openings:
            task.cancel()
        if openings:
            await asyncio.gather(*(task for _, task in openings), return_exceptions=True)
        if shadow_openings:
            await asyncio.gather(*shadow_openings, return_exceptions=True)
        async with self._lock:
            for key, task in openings:
                if self._opening.get(key) is task:
                    self._opening.pop(key, None)
                    self._opening_owners.pop(key, None)
        scopes = [
            state.public
            for state in tuple(self._scopes.values())
            if state.public.owner_user_id == event.owner_user_id
            and state.public.server_id == event.server_id
            and (
                event.action in {MCPInvalidationAction.DISABLED, MCPInvalidationAction.DELETED}
                or state.public.security_version < event.security_version
            )
        ]
        await asyncio.gather(
            *(self.close_scope(scope, str(event.action)) for scope in scopes),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(session.aclose() for session in shadow_sessions),
            return_exceptions=True,
        )

    async def aclose(self) -> None:
        async with self._lock:
            openings = list(self._opening.items())
            shadow_openings = tuple(self._readonly_shadow_openings)
            shadow_sessions = tuple(self._readonly_shadow_sessions)
        for _, task in openings:
            task.cancel()
        for task in shadow_openings:
            task.cancel()
        if openings:
            await asyncio.gather(*(task for _, task in openings), return_exceptions=True)
        if shadow_openings:
            await asyncio.gather(*shadow_openings, return_exceptions=True)
        scopes = [state.public for state in tuple(self._scopes.values())]
        await asyncio.gather(
            *(self.close_scope(scope, "gateway_shutdown") for scope in scopes),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(session.aclose() for session in shadow_sessions),
            return_exceptions=True,
        )

    async def _discard_readonly_shadow_session(
        self, session: MCPReadonlyShadowSession
    ) -> None:
        async with self._lock:
            self._readonly_shadow_sessions.discard(session)

    async def _renew_scope(self, state: _ScopeState) -> None:
        while not state.renew_stop.is_set():
            try:
                await asyncio.wait_for(
                    state.renew_stop.wait(), timeout=self._lease_renew_interval_seconds
                )
                return
            except TimeoutError:
                pass
            now = self._now()
            renewed = await self._renew_scope_lease_once(state.public, now=now)
            if not renewed:
                state.accepting_calls = False
                asyncio.create_task(self.close_scope(state.public, "lease_lost"))
                return

    async def _renew_bootstrap_scope(
        self,
        scope: MCPTaskServerScope,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._lease_renew_interval_seconds
                )
                return
            except TimeoutError:
                pass
            if not await self._renew_scope_lease_once(scope):
                lease_lost.set()
                return

    async def _renew_scope_lease_once(
        self,
        scope: MCPTaskServerScope,
        *,
        now: datetime | None = None,
    ) -> bool:
        renewed_at = now or self._now()
        try:
            return bool(
                await self._storage.renew_user_mcp_scope_lease(
                    scope.scope_id,
                    scope.owner_user_id,
                    scope.server_id,
                    gateway_instance_id=self._instance_id,
                    security_version=scope.security_version,
                    lease_expires_at=renewed_at
                    + timedelta(seconds=self._lease_ttl_seconds),
                    updated_at=renewed_at,
                )
            )
        except Exception:
            return False

    async def _heartbeat(
        self,
        call_state: _CallState,
        invocation: asyncio.Task[Any],
        callbacks: MCPCallCallbacks | None,
    ) -> None:
        while not invocation.done():
            reset = await self._heartbeat_waiter(
                call_state.heartbeat_reset,
                self._heartbeat_interval_seconds,
            )
            if invocation.done():
                return
            if reset:
                continue
            if callbacks is not None and callbacks.on_heartbeat is not None:
                try:
                    await _await_maybe(callbacks.on_heartbeat(call_state.call_ref))
                except Exception:
                    pass

    async def _require_task_owner(self, owner_user_id: str, task_id: str) -> None:
        task = await self._storage.get_task(task_id)
        if task is None or task.status not in {
            TaskStatus.ACCEPTED,
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
        }:
            raise MCPGatewayError("mcp_task_not_found")
        conversation = await self._storage.get_conversation(task.conversation_id)
        if conversation is None or conversation.username != owner_user_id:
            await self._report_safety_violation(
                MCPSafetyRedLine.CROSS_USER_ACCESS,
                "task_owner_mismatch",
            )
            raise MCPGatewayError("mcp_task_not_found")

    async def _revalidate_endpoint(self, server: UserMCPServer) -> Any:
        try:
            return await _await_maybe(self._endpoint_revalidator(server))
        except EndpointPolicyError as exc:
            await self._mark_unavailable(server, exc.code)
            await self._report_safety_violation(
                MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS,
                "endpoint_policy_rejected",
            )
            raise
        except Exception:
            await self._report_safety_violation(
                MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS,
                "endpoint_policy_rejected",
            )
            raise

    async def _observe_endpoint_security(
        self,
        server: UserMCPServer,
        endpoint: ValidatedEndpoint,
    ) -> None:
        if self._endpoint_security_observer is None:
            return
        await _await_maybe(self._endpoint_security_observer(server, endpoint))

    def _require_scope(self, scope: MCPTaskServerScope) -> _ScopeState:
        state = self._scopes.get((scope.platform_task_id, scope.server_id))
        if state is None or state.public.scope_id != scope.scope_id:
            raise MCPGatewayError("mcp_scope_not_found")
        return state

    def _find_call_scope(
        self, platform_task_id: str, call_ref: str
    ) -> _ScopeState | None:
        return next(
            (
                state
                for (task_id, _), state in self._scopes.items()
                if task_id == platform_task_id and call_ref in state.calls
            ),
            None,
        )

    def _configure_result_store_metrics(self) -> None:
        configure = getattr(self._result_store, "configure_metric_observers", None)
        if callable(configure):
            configure(
                spill_observer=self._record_temp_spill,
                cleanup_failure_observer=self._record_cleanup_failure,
            )

    async def _record_tools_list_attempt(
        self,
        *,
        server: UserMCPServer,
        adapter: Any,
        duration_seconds: float,
        result_category: MCPMetricResultCategory,
        error_category: MCPMetricErrorCategory,
    ) -> None:
        await self._record_gateway_count(
            MCPMetricName.TOOLS_LIST_ATTEMPTS_TOTAL,
            server=server,
            adapter=adapter,
            result_category=result_category,
            error_category=error_category,
        )
        await self._record_gateway_latency(
            MCPMetricName.TOOLS_LIST_DURATION_SECONDS,
            server=server,
            adapter=adapter,
            duration_seconds=duration_seconds,
            result_category=result_category,
            error_category=error_category,
        )

    async def _record_active_scope_gauge(self) -> None:
        for transport, protocol_version, adapter in self._metric_scope_dimensions:
            await self._record_gateway_gauge(
                MCPMetricName.GATEWAY_ACTIVE_SCOPES,
                value=sum(
                    _metric_dimension(state.server, state.adapter)
                    == (transport, protocol_version, adapter)
                    for state in self._scopes.values()
                ),
                transport=transport,
                protocol_version=protocol_version,
                adapter=adapter,
            )

    async def _record_active_call_gauge(self) -> None:
        for transport, protocol_version, adapter in self._metric_scope_dimensions:
            await self._record_gateway_gauge(
                MCPMetricName.TOOL_CALLS_ACTIVE,
                value=sum(
                    len(state.calls)
                    for state in self._scopes.values()
                    if _metric_dimension(state.server, state.adapter)
                    == (transport, protocol_version, adapter)
                ),
                transport=transport,
                protocol_version=protocol_version,
                adapter=adapter,
                call_kind=MCPCallKind.ORDINARY,
            )

    async def _record_temp_spill(
        self,
        scope_id: str | None,
        byte_count: int,
    ) -> None:
        metric_scope_id = scope_id or "__not_applicable__"
        self._metric_spill_bytes_by_scope_id[metric_scope_id] = byte_count
        dimension = self._metric_dimension_for_scope_id(scope_id)
        await self._record_gateway_gauge(
            MCPMetricName.TEMP_SPILL_BYTES,
            value=sum(
                value
                for candidate_scope_id, value in self._metric_spill_bytes_by_scope_id.items()
                if self._metric_dimension_for_scope_id(
                    None
                    if candidate_scope_id == "__not_applicable__"
                    else candidate_scope_id
                )
                == dimension
            ),
            transport=dimension[0],
            protocol_version=dimension[1],
            adapter=dimension[2],
        )

    async def _record_cleanup_failure(
        self,
        server: UserMCPServer | str | None = None,
        adapter: Any | None = None,
    ) -> None:
        await self._report_safety_violation(
            MCPSafetyRedLine.PERSISTENT_RESOURCE_LEAK,
            "cleanup_failed",
        )
        if isinstance(server, str):
            state = self._state_for_scope_id(server)
            server = None if state is None else state.server
            adapter = None if state is None else state.adapter
        await self._record_gateway_count(
            MCPMetricName.RESOURCE_CLEANUP_FAILURES_TOTAL,
            server=server,
            adapter=adapter,
            result_category=MCPMetricResultCategory.FAILED,
            error_category=MCPMetricErrorCategory.CLEANUP,
        )

    async def _report_safety_violation(
        self, red_line: MCPSafetyRedLine, reason_code: str
    ) -> None:
        detector = self._safety_detectors.get(red_line)
        if detector is None:
            return
        await detector.report_violation(reason_code=reason_code)

    async def _record_gateway_count(
        self,
        metric_name: MCPMetricName,
        *,
        server: UserMCPServer | None = None,
        adapter: Any | None = None,
        result_category: MCPMetricResultCategory,
        error_category: MCPMetricErrorCategory,
        call_kind: MCPCallKind | None = None,
        value: int = 1,
    ) -> None:
        recorder = self._metric_recorder
        if recorder is None:
            return
        started_at, ended_at = _metric_bucket_window()
        try:
            await recorder.record_count(
                metric_name,
                labels=self._metric_labels(
                    server=server,
                    adapter=adapter,
                    result_category=result_category,
                    error_category=error_category,
                    call_kind=call_kind,
                ),
                bucket_started_at=started_at,
                bucket_ended_at=ended_at,
                value=value,
            )
        except Exception:
            return

    async def _record_gateway_latency(
        self,
        metric_name: MCPMetricName,
        *,
        server: UserMCPServer,
        adapter: Any | None,
        duration_seconds: float,
        result_category: MCPMetricResultCategory,
        error_category: MCPMetricErrorCategory,
    ) -> None:
        recorder = self._metric_recorder
        if recorder is None:
            return
        started_at, ended_at = _metric_bucket_window()
        try:
            await recorder.record_latency(
                metric_name,
                duration_seconds=duration_seconds,
                labels=self._metric_labels(
                    server=server,
                    adapter=adapter,
                    result_category=result_category,
                    error_category=error_category,
                ),
                bucket_started_at=started_at,
                bucket_ended_at=ended_at,
            )
        except Exception:
            return

    async def _record_gateway_gauge(
        self,
        metric_name: MCPMetricName,
        *,
        value: int,
        transport: MCPMetricTransport = MCPMetricTransport.NOT_APPLICABLE,
        protocol_version: MCPMetricProtocolVersion = MCPMetricProtocolVersion.NOT_APPLICABLE,
        adapter: MCPMetricAdapter = MCPMetricAdapter.NOT_APPLICABLE,
        call_kind: MCPCallKind | None = None,
    ) -> None:
        recorder = self._metric_recorder
        routing_mode = self._metric_routing_mode
        if recorder is None or routing_mode is None:
            return
        started_at, ended_at = _metric_bucket_window()
        try:
            await recorder.record_gauge(
                metric_name,
                labels=MCPMetricLabels(
                    execution_path=MCPMetricExecutionPath.USER_SCOPED,
                    routing_mode=routing_mode,
                    transport=transport,
                    protocol_version=protocol_version,
                    adapter=adapter,
                    call_kind=call_kind,
                ),
                bucket_started_at=started_at,
                bucket_ended_at=ended_at,
                value=value,
            )
        except Exception:
            return

    def _metric_labels(
        self,
        *,
        server: UserMCPServer | None,
        adapter: Any | None,
        result_category: MCPMetricResultCategory,
        error_category: MCPMetricErrorCategory,
        call_kind: MCPCallKind | None = None,
    ) -> MCPMetricLabels:
        routing_mode = self._metric_routing_mode or MCPMetricRoutingMode.NOT_APPLICABLE
        protocol_version = _metric_protocol_version(server, adapter)
        return MCPMetricLabels(
            execution_path=MCPMetricExecutionPath.USER_SCOPED,
            routing_mode=routing_mode,
            transport=_metric_transport(server),
            protocol_version=protocol_version,
            adapter=_metric_adapter(protocol_version),
            result_category=result_category,
            error_category=error_category,
            call_kind=call_kind,
        )

    def _state_for_scope_id(self, scope_id: str | None) -> _ScopeState | None:
        if scope_id is None:
            return None
        return next(
            (
                state
                for state in self._scopes.values()
                if state.public.scope_id == scope_id
            ),
            None,
        )

    def _metric_dimension_for_scope_id(
        self,
        scope_id: str | None,
    ) -> tuple[
        MCPMetricTransport,
        MCPMetricProtocolVersion,
        MCPMetricAdapter,
    ]:
        if scope_id is None:
            return (
                MCPMetricTransport.NOT_APPLICABLE,
                MCPMetricProtocolVersion.NOT_APPLICABLE,
                MCPMetricAdapter.NOT_APPLICABLE,
            )
        return self._metric_dimension_by_scope_id.get(
            scope_id,
            (
                MCPMetricTransport.NOT_APPLICABLE,
                MCPMetricProtocolVersion.NOT_APPLICABLE,
                MCPMetricAdapter.NOT_APPLICABLE,
            ),
        )

    async def _mark_unavailable(self, server: UserMCPServer, error_code: str) -> None:
        await self._storage.update_user_mcp_server(
            server.owner_user_id,
            server.server_id,
            changes={
                "health_status": UserMCPHealthStatus.UNAVAILABLE,
                "last_test_error_code": error_code,
                "last_tested_at": self._now(),
            },
            expected_config_version=server.config_version,
            expected_security_version=server.security_version,
            updated_at=self._now(),
        )


def _freeze_catalog(server: UserMCPServer, adapter: Any, tools: list[Mapping[str, Any]]) -> ToolCatalogSnapshot:
    descriptors: list[MCPToolDescriptor] = []
    names: set[str] = set()
    for raw in tools:
        name = str(raw.get("name") or "").strip()
        schema = raw.get("inputSchema")
        if not name or name in names or not isinstance(schema, Mapping):
            raise MCPProtocolError("MCP tool catalog is invalid.")
        names.add(name)
        _validate_schema(schema)
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
        output_schema = raw.get("outputSchema")
        try:
            frozen_output_schema, output_schema_sha256 = _freeze_output_schema(
                output_schema
            )
        except MCPProtocolError:
            # An invalid output contract excludes only this Tool; valid siblings remain usable.
            continue
        descriptors.append(
            MCPToolDescriptor(
                name=name,
                description=str(raw.get("description") or ""),
                input_schema=dict(schema),
                input_schema_sha256=hashlib.sha256(canonical).hexdigest(),
                output_schema=frozen_output_schema,
                output_schema_sha256=output_schema_sha256,
                annotations=dict(raw.get("annotations") or {})
                if isinstance(raw.get("annotations"), Mapping)
                else {},
            )
        )
    session = getattr(adapter, "negotiated_session", None)
    version = (
        session.negotiated_protocol_version
        if session is not None
        else str(server.protocol_preference)
    )
    return ToolCatalogSnapshot(server.server_id, version, tuple(descriptors))


def _validate_schema(schema: Mapping[str, Any]) -> None:
    try:
        validator = Draft202012Validator if "$schema" in schema else Draft7Validator
        validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise MCPProtocolError("MCP tool inputSchema is invalid.") from exc


def _freeze_output_schema(
    schema: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if schema is None:
        return None, None
    if not isinstance(schema, Mapping):
        raise MCPProtocolError("MCP tool outputSchema must be an object.")
    snapshot = dict(schema)
    dialect = snapshot.get("$schema")
    if dialect is None:
        validator = Draft202012Validator
    elif dialect == "http://json-schema.org/draft-07/schema#":
        validator = Draft7Validator
    elif dialect in {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }:
        validator = Draft202012Validator
    else:
        raise MCPProtocolError("MCP tool outputSchema dialect is unsupported.")
    try:
        validator.check_schema(snapshot)
        canonical = json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (SchemaError, TypeError, ValueError) as exc:
        raise MCPProtocolError("MCP tool outputSchema is invalid.") from exc
    if len(canonical) > 256 * 1024:
        raise MCPProtocolError("MCP tool outputSchema is too large.")

    def reject_external_refs(value: object) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key == "$ref" and (
                    not isinstance(nested, str) or not nested.startswith("#")
                ):
                    raise MCPProtocolError(
                        "MCP tool outputSchema contains an external reference."
                    )
                reject_external_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_external_refs(nested)

    reject_external_refs(snapshot)
    return snapshot, "sha256:" + hashlib.sha256(canonical).hexdigest()


def _metric_bucket_window() -> tuple[datetime, datetime]:
    started_at = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return started_at, started_at + timedelta(minutes=1)


def _metric_transport(server: UserMCPServer | None) -> MCPMetricTransport:
    if server is None:
        return MCPMetricTransport.NOT_APPLICABLE
    return {
        UserMCPTransport.STREAMABLE_HTTP: MCPMetricTransport.STREAMABLE_HTTP,
        UserMCPTransport.LEGACY_HTTP_SSE: MCPMetricTransport.LEGACY_HTTP_SSE,
    }[server.transport]


def _metric_protocol_version(
    server: UserMCPServer | None,
    adapter: Any | None,
) -> MCPMetricProtocolVersion:
    session = getattr(adapter, "negotiated_session", None)
    raw = getattr(session, "negotiated_protocol_version", None)
    if not raw and server is not None:
        raw = str(server.protocol_preference)
    try:
        return MCPMetricProtocolVersion(str(raw))
    except ValueError:
        return MCPMetricProtocolVersion.NOT_APPLICABLE


def _metric_adapter(protocol_version: MCPMetricProtocolVersion) -> MCPMetricAdapter:
    if protocol_version is MCPMetricProtocolVersion.V2026_07_28:
        return MCPMetricAdapter.PYTHON_2026
    if protocol_version is not MCPMetricProtocolVersion.NOT_APPLICABLE:
        return MCPMetricAdapter.PYTHON_LEGACY
    return MCPMetricAdapter.NOT_APPLICABLE


def _metric_dimension(
    server: UserMCPServer,
    adapter: Any,
) -> tuple[MCPMetricTransport, MCPMetricProtocolVersion, MCPMetricAdapter]:
    protocol_version = _metric_protocol_version(server, adapter)
    return (
        _metric_transport(server),
        protocol_version,
        _metric_adapter(protocol_version),
    )


def _metric_error_category(exc: BaseException) -> MCPMetricErrorCategory:
    if isinstance(exc, asyncio.CancelledError):
        return MCPMetricErrorCategory.NONE
    code = str(
        getattr(exc, "code", "")
        or getattr(exc, "mcp_error_code", "")
        or exc
    ).lower()
    for fragments, category in (
        (("credential", "authentication", "unauthenticated"), MCPMetricErrorCategory.AUTHENTICATION),
        (("authorization", "permission", "forbidden"), MCPMetricErrorCategory.AUTHORIZATION),
        (("endpoint", "ssrf", "dns"), MCPMetricErrorCategory.ENDPOINT_POLICY),
        (("timeout", "deadline"), MCPMetricErrorCategory.TIMEOUT),
        (("transport", "connection", "disconnect"), MCPMetricErrorCategory.TRANSPORT),
        (("protocol", "adapter", "session"), MCPMetricErrorCategory.PROTOCOL),
        (("argument", "schema", "validation", "tool_not_found"), MCPMetricErrorCategory.VALIDATION),
        (("server",), MCPMetricErrorCategory.SERVER),
    ):
        if any(fragment in code for fragment in fragments):
            return category
    return MCPMetricErrorCategory.UNKNOWN


async def _safe_close(value: Any) -> bool:
    try:
        await _await_maybe(value.close())
    except Exception:
        return False
    return True


async def _wait_for_signal(signal: asyncio.Event, timeout_seconds: float) -> bool:
    try:
        await asyncio.wait_for(signal.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    signal.clear()
    return True


async def _await_maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _spawn_callback(value: Any) -> None:
    if inspect.isawaitable(value):
        asyncio.create_task(value)
