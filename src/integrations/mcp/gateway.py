from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncContextManager, Protocol
from uuid import uuid4

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError, ValidationError

from src.core.enums import TaskStatus, UserMCPHealthStatus
from src.core.models import UserMCPScopeLease, UserMCPServer

from .adapter_2026 import (
    MCPCompletedOutcome,
    MCPInputRequiredOutcome,
    MCPTaskCreatedOutcome,
)
from .client import MCPClientError, MCPProtocolError
from .gateway_models import (
    CancelOutcome,
    ContinueOutcome,
    MCPCallOutcome,
    MCPCancelStatus,
    MCPContinueStatus,
    MCPTaskServerScope,
    MCPToolDescriptor,
    ToolCatalogSnapshot,
)
from .invalidation import MCPInvalidationAction, MCPServerInvalidated
from .temporary_results import (
    MCPAdmissionLease,
    MCPTemporaryResultCapacity,
    MCPTemporaryResultStore,
)


SCOPE_LEASE_TTL_SECONDS = 30.0
SCOPE_LEASE_RENEW_INTERVAL_SECONDS = 10.0
CALL_HEARTBEAT_INTERVAL_SECONDS = 120.0
SCOPE_DISCOVERY_TIMEOUT_SECONDS = 60.0
SCOPE_DISCOVERY_RETRY_DELAY_SECONDS = 0.25


class MCPGatewayError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


class MCPGateway:
    def __init__(
        self,
        *,
        storage: Any,
        gateway_instance_id: str,
        credential_loader: Callable[[UserMCPServer], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
        client_factory: Callable[[UserMCPServer, Mapping[str, Any]], Any | Awaitable[Any]],
        endpoint_revalidator: Callable[[UserMCPServer], Any | Awaitable[Any]],
        result_store: MCPTemporaryResultStore,
        capacity: MCPTemporaryResultCapacity,
        now_fn: Callable[[], datetime] | None = None,
        lease_ttl_seconds: float = SCOPE_LEASE_TTL_SECONDS,
        lease_renew_interval_seconds: float = SCOPE_LEASE_RENEW_INTERVAL_SECONDS,
        heartbeat_interval_seconds: float = CALL_HEARTBEAT_INTERVAL_SECONDS,
        discovery_timeout_seconds: float = SCOPE_DISCOVERY_TIMEOUT_SECONDS,
        discovery_retry_delay_seconds: float = SCOPE_DISCOVERY_RETRY_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        task_call_guard: MCPTaskCallGuard | None = None,
        heartbeat_waiter: Callable[[asyncio.Event, float], Awaitable[bool]] | None = None,
    ) -> None:
        self._storage = storage
        self._instance_id = gateway_instance_id
        self._credential_loader = credential_loader
        self._client_factory = client_factory
        self._endpoint_revalidator = endpoint_revalidator
        self._result_store = result_store
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
        self._scopes: dict[tuple[str, str], _ScopeState] = {}
        self._opening: dict[tuple[str, str], asyncio.Task[_ScopeState]] = {}
        self._opening_owners: dict[tuple[str, str], str] = {}
        self._terminal_call_tasks: dict[str, str] = {}
        self._closing_tasks: set[str] = set()
        self._lock = asyncio.Lock()

    async def open_scope(
        self,
        authenticated_user: MCPAuthenticatedPrincipal,
        platform_task_id: str,
        server_id: str,
        *,
        on_queue_entered: Callable[[int], Awaitable[None]] | None = None,
        on_queue_left: Callable[[], Awaitable[None]] | None = None,
    ) -> MCPTaskServerScope:
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
                    try:
                        async with asyncio.timeout(self._discovery_timeout_seconds):
                            await _await_maybe(self._endpoint_revalidator(server))
                            credentials = await _await_maybe(
                                self._credential_loader(server)
                            )
                            candidate = await _await_maybe(
                                self._client_factory(server, credentials)
                            )
                            await candidate.initialize()
                            capabilities = candidate.server_capabilities
                            if "tools" not in capabilities:
                                await self._mark_unavailable(
                                    server, "no_tools_capability"
                                )
                                raise MCPGatewayError("no_tools_capability")
                            tools = await candidate.list_tools()
                            catalog = _freeze_catalog(server, candidate, tools)
                            if not catalog.tools:
                                await self._mark_unavailable(server, "empty_tool_list")
                                raise MCPGatewayError("empty_tool_list")
                            adapter = candidate
                            return candidate, catalog
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
                    finally:
                        if candidate is not None and candidate is not adapter:
                            await _safe_close(candidate)
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
                await _safe_close(adapter)
            try:
                await self._storage.release_user_mcp_scope_lease(
                    scope.scope_id, gateway_instance_id=self._instance_id
                )
            finally:
                await admission_lease.release()
            raise

    async def list_tools(self, scope: MCPTaskServerScope) -> ToolCatalogSnapshot:
        return self._require_scope(scope).catalog

    async def call_tool(
        self,
        scope: MCPTaskServerScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        callbacks: MCPCallCallbacks | None = None,
    ) -> MCPCallOutcome:
        state = self._require_scope(scope)
        if not state.accepting_calls:
            raise MCPGatewayError("mcp_scope_closed")
        descriptor = state.catalog.get(tool_name)
        if descriptor is None:
            raise MCPGatewayError("mcp_tool_not_found")
        _validate_arguments(descriptor.input_schema, arguments)
        call_ref = f"mcp-call-{uuid4().hex}"
        call_state = _CallState(call_ref=call_ref)
        state.calls[call_ref] = call_state
        call_task = asyncio.create_task(
            self._execute_call(state, call_state, tool_name, arguments, callbacks),
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
                raise
        call_state.start_allowed.set()
        try:
            return await call_task
        finally:
            state.calls.pop(call_ref, None)
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
    ) -> MCPCallOutcome:
        await call_state.start_allowed.wait()
        async with self._task_call_guard.admit(
            state.public.owner_user_id,
            state.public.platform_task_id,
            call_state.call_ref,
        ):
            if not state.accepting_calls:
                raise MCPGatewayError("mcp_scope_closed")
            sink = self._result_store.create_sink(
                state.public.platform_task_id, scope_id=state.public.scope_id
            )

            def registered(request_id: str | int) -> None:
                call_state.remote_request_id = request_id

            if callbacks is not None and callbacks.on_registered is not None:
                await _await_maybe(callbacks.on_registered(call_state.call_ref))
            call_state.dispatched = True
            invocation = asyncio.create_task(
                state.adapter.call_tool(
                    tool_name,
                    arguments,
                    request_registered_callback=registered,
                    result_sink=sink,
                )
            )
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
        outcome = await self._normalize_outcome(raw, sink)
        if not state.accepting_calls:
            raise MCPGatewayError("mcp_scope_closed")
        return outcome

    async def _normalize_outcome(self, raw: Any, sink: Any) -> MCPCallOutcome:
        if isinstance(raw, MCPInputRequiredOutcome):
            await sink.abort()
            requests = tuple(dict(value) for value in raw.input_requests.values())
            return MCPCallOutcome.input_required(
                requests,
                raw.sealed_request_state_ref,
            )
        if isinstance(raw, MCPTaskCreatedOutcome):
            await sink.abort()
            return MCPCallOutcome.task_created(raw.safe_remote_task_ref, status=raw.status)
        result = raw.result if isinstance(raw, MCPCompletedOutcome) else raw
        if not isinstance(result, Mapping):
            result = {"value": result}
        embedded = result.get("_mcpResultRef")
        if isinstance(embedded, Mapping):
            size = embedded.get("sizeBytes")
            return MCPCallOutcome.completed(
                str(embedded["ref"]),
                content_type=str(embedded.get("contentType") or "application/json"),
                byte_size=(
                    size
                    if isinstance(size, int) and not isinstance(size, bool)
                    else None
                ),
            )
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        await sink.write(encoded)
        ref = await sink.finalize()
        return MCPCallOutcome.completed(
            ref.ref,
            content_type="application/json",
            byte_size=ref.size_bytes,
        )

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
        if self._terminal_call_tasks.get(call_ref) == platform_task_id:
            return CancelOutcome(MCPCancelStatus.ALREADY_TERMINAL, True)
        return CancelOutcome(MCPCancelStatus.UNKNOWN_CALL, False)

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
            await _safe_close(state.adapter)
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

    async def close_task(self, platform_task_id: str, reason: str) -> None:
        async with self._lock:
            self._closing_tasks.add(platform_task_id)
            openings = [
                (key, task)
                for key, task in self._opening.items()
                if key[0] == platform_task_id
            ]
        try:
            for _, task in openings:
                task.cancel()
            if openings:
                await asyncio.gather(
                    *(task for _, task in openings), return_exceptions=True
                )
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
            await self._result_store.cleanup_task(platform_task_id)
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
        for _, task in openings:
            task.cancel()
        if openings:
            await asyncio.gather(*(task for _, task in openings), return_exceptions=True)
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

    async def aclose(self) -> None:
        async with self._lock:
            openings = list(self._opening.items())
        for _, task in openings:
            task.cancel()
        if openings:
            await asyncio.gather(*(task for _, task in openings), return_exceptions=True)
        scopes = [state.public for state in tuple(self._scopes.values())]
        await asyncio.gather(
            *(self.close_scope(scope, "gateway_shutdown") for scope in scopes),
            return_exceptions=True,
        )

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
            raise MCPGatewayError("mcp_task_not_found")

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
        if output_schema is not None and not isinstance(output_schema, Mapping):
            raise MCPProtocolError("MCP tool outputSchema must be an object.")
        descriptors.append(
            MCPToolDescriptor(
                name=name,
                description=str(raw.get("description") or ""),
                input_schema=dict(schema),
                input_schema_sha256=hashlib.sha256(canonical).hexdigest(),
                output_schema=dict(output_schema) if isinstance(output_schema, Mapping) else None,
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


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    try:
        validator = Draft202012Validator if "$schema" in schema else Draft7Validator
        validator(dict(schema)).validate(dict(arguments))
    except (SchemaError, ValidationError) as exc:
        raise MCPGatewayError("mcp_tool_arguments_invalid") from exc


async def _safe_close(value: Any) -> None:
    try:
        await _await_maybe(value.close())
    except Exception:
        pass


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
