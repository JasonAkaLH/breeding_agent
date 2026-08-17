from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import uuid4

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError

from ...core.models import UserMCPHealthAttempt
from .client import MCPClientError, MCPProtocolError
from .endpoint_policy import EndpointPolicyError


HEALTH_ATTEMPT_TIMEOUT_SECONDS = 60.0
HEALTH_TRANSIENT_RETRY_DELAY_SECONDS = 0.25
HEALTH_CLEANUP_TIMEOUT_SECONDS = 1.0
HEALTH_LEASE_TTL_SECONDS = 30.0
HEALTH_LEASE_RENEW_INTERVAL_SECONDS = 10.0
HEALTH_RECOVERY_INTERVAL_SECONDS = 10.0


class MCPHealthError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MCPHealthClient(Protocol):
    @property
    def server_capabilities(self) -> Mapping[str, Any]: ...

    async def initialize(self) -> Any: ...

    async def list_tools(self) -> list[Mapping[str, Any]]: ...

    async def close(self) -> None: ...


@dataclass(slots=True, frozen=True)
class MCPHealthCheckResult:
    available: bool
    error_code: str | None
    tool_count: int


async def discover_healthy_tools(client: MCPHealthClient) -> MCPHealthCheckResult:
    await client.initialize()
    capabilities = client.server_capabilities
    if "tools" not in capabilities:
        return MCPHealthCheckResult(False, "no_tools_capability", 0)
    tools = await client.list_tools()
    _validate_tool_catalog(tools)
    if not tools:
        return MCPHealthCheckResult(False, "empty_tool_list", 0)
    return MCPHealthCheckResult(True, None, len(tools))


async def run_health_discovery(
    client_factory: Callable[[], MCPHealthClient | Awaitable[MCPHealthClient]],
    *,
    timeout_seconds: float = HEALTH_ATTEMPT_TIMEOUT_SECONDS,
    retry_delay_seconds: float = HEALTH_TRANSIENT_RETRY_DELAY_SECONDS,
    cleanup_timeout_seconds: float = HEALTH_CLEANUP_TIMEOUT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> MCPHealthCheckResult:
    for ordinal in range(2):
        client: MCPHealthClient | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                created = client_factory()
                client = await created if inspect.isawaitable(created) else created
                return await discover_healthy_tools(client)
        except TimeoutError as exc:
            error = MCPHealthError("discovery_timeout")
            retriable = True
            error.__cause__ = exc
        except EndpointPolicyError as exc:
            error = MCPHealthError(exc.code)
            retriable = False
            error.__cause__ = exc
        except MCPClientError as exc:
            error = MCPHealthError(_safe_discovery_error_code(exc))
            retriable = bool(exc.retriable) and exc.mcp_error_code not in {
                "mcp_auth_required",
                "mcp_scope_required",
                "mcp_protocol_error",
            }
            error.__cause__ = exc
        finally:
            if client is not None:
                try:
                    async with asyncio.timeout(cleanup_timeout_seconds):
                        await client.close()
                except Exception:
                    pass
        if ordinal == 0 and retriable:
            await sleep(retry_delay_seconds)
            continue
        return MCPHealthCheckResult(False, error.code, 0)
    raise AssertionError("unreachable")


def _validate_tool_catalog(tools: list[Mapping[str, Any]]) -> None:
    names: set[str] = set()
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            raise MCPProtocolError("MCP tool name is required.")
        if name in names:
            raise MCPProtocolError("MCP tool names must be unique.")
        names.add(name)
        schema = tool.get("inputSchema")
        if not isinstance(schema, Mapping):
            raise MCPProtocolError("MCP tool inputSchema must be an object.")
        try:
            validator = Draft202012Validator if "$schema" in schema else Draft7Validator
            validator.check_schema(dict(schema))
        except SchemaError as exc:
            raise MCPProtocolError("MCP tool inputSchema must be valid JSON Schema.") from exc


def _safe_discovery_error_code(exc: MCPClientError) -> str:
    if exc.mcp_error_code in {"mcp_auth_required", "mcp_scope_required"}:
        return "authentication_failed"
    if exc.mcp_error_code == "mcp_protocol_error":
        return "tool_discovery_invalid"
    if exc.mcp_error_code == "mcp_timeout":
        return "discovery_timeout"
    return "tool_discovery_failed"


class MCPHealthRunner:
    """Tracks lease-backed health jobs; storage CAS is the final authority."""

    def __init__(
        self,
        *,
        storage: Any,
        instance_id: str,
        endpoint_revalidator: Callable[[Any], Any | Awaitable[Any]],
        client_factory: Callable[
            [Any, Mapping[str, str], Any],
            MCPHealthClient | Awaitable[MCPHealthClient],
        ],
        credential_loader: Callable[[Any], Mapping[str, str] | Awaitable[Mapping[str, str]]],
        now_fn: Callable[[], datetime],
        endpoint_security_observer: Callable[[Any, Any], Any | Awaitable[Any]]
        | None = None,
        lease_ttl_seconds: float = HEALTH_LEASE_TTL_SECONDS,
        lease_renew_interval_seconds: float = HEALTH_LEASE_RENEW_INTERVAL_SECONDS,
    ) -> None:
        self._storage = storage
        self._instance_id = instance_id
        self._endpoint_revalidator = endpoint_revalidator
        self._client_factory = client_factory
        self._credential_loader = credential_loader
        self._endpoint_security_observer = endpoint_security_observer
        self._now = now_fn
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lease_renew_interval_seconds = lease_renew_interval_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._attempts: dict[str, UserMCPHealthAttempt] = {}
        self._recovery_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start_test(self, server: Any) -> UserMCPHealthAttempt:
        await self.cancel_server(
            server.owner_user_id,
            server.server_id,
            reason="superseded",
        )
        attempt_id = f"mcp-health-{uuid4().hex}"
        now = self._now()
        attempt = UserMCPHealthAttempt(
            attempt_id=attempt_id,
            owner_user_id=server.owner_user_id,
            server_id=server.server_id,
            config_version=server.config_version,
            security_version=server.security_version,
            runner_instance_id=self._instance_id,
            lease_expires_at=now + timedelta(seconds=self._lease_ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        if not await self._storage.claim_user_mcp_health_attempt(attempt):
            raise MCPHealthError("health_attempt_conflict")
        task = asyncio.create_task(
            self._run_claimed(server, attempt), name=f"user-mcp-health:{server.server_id}"
        )
        self._tasks[attempt_id] = task
        self._attempts[attempt_id] = attempt
        task.add_done_callback(lambda _task, key=attempt_id: self._tasks.pop(key, None))
        return attempt

    async def _run_claimed(self, server: Any, attempt: UserMCPHealthAttempt) -> None:
        attempt_id = attempt.attempt_id
        renew_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        renewer = asyncio.create_task(
            self._renew(attempt, renew_stop, lease_lost),
            name=f"user-mcp-health-renew:{attempt_id}",
        )
        discovery: asyncio.Task[MCPHealthCheckResult] | None = None
        lost_wait: asyncio.Task[bool] | None = None
        try:
            discovery = asyncio.create_task(
                self._discover(server), name=f"user-mcp-health-discovery:{attempt_id}"
            )
            lost_wait = asyncio.create_task(lease_lost.wait())
            done, _ = await asyncio.wait(
                {discovery, lost_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if lost_wait in done and lease_lost.is_set():
                discovery.cancel()
                await asyncio.gather(discovery, return_exceptions=True)
                return
            lost_wait.cancel()
            await asyncio.gather(lost_wait, return_exceptions=True)
            result = await discovery
            await self._complete_attempt(server, attempt_id, result)
        except asyncio.CancelledError:
            if discovery is not None and not discovery.done():
                discovery.cancel()
                await asyncio.gather(discovery, return_exceptions=True)
            await self._storage.complete_user_mcp_health_attempt(
                attempt_id,
                server.owner_user_id,
                server.server_id,
                runner_instance_id=self._instance_id,
                config_version=server.config_version,
                security_version=server.security_version,
                health_status="unavailable",
                error_code="test_interrupted",
                completed_at=self._now(),
            )
            raise
        except Exception:
            await self._storage.complete_user_mcp_health_attempt(
                attempt_id,
                server.owner_user_id,
                server.server_id,
                runner_instance_id=self._instance_id,
                config_version=server.config_version,
                security_version=server.security_version,
                health_status="unavailable",
                error_code="tool_discovery_failed",
                completed_at=self._now(),
            )
        finally:
            for pending in (discovery, lost_wait):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(pending for pending in (discovery, lost_wait) if pending is not None),
                return_exceptions=True,
            )
            renew_stop.set()
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            await self._storage.release_user_mcp_health_attempt(
                attempt.attempt_id,
                attempt.owner_user_id,
                attempt.server_id,
                runner_instance_id=attempt.runner_instance_id,
                config_version=attempt.config_version,
                security_version=attempt.security_version,
            )
            self._attempts.pop(attempt_id, None)

    async def _discover(self, server: Any) -> MCPHealthCheckResult:
        async def create_client() -> MCPHealthClient:
            validated_endpoint = self._endpoint_revalidator(server)
            validated_endpoint = (
                await validated_endpoint
                if inspect.isawaitable(validated_endpoint)
                else validated_endpoint
            )
            if self._endpoint_security_observer is not None:
                observed = self._endpoint_security_observer(
                    server,
                    validated_endpoint,
                )
                if inspect.isawaitable(observed):
                    await observed
            credentials = self._credential_loader(server)
            resolved_credentials = (
                await credentials if inspect.isawaitable(credentials) else credentials
            )
            value = self._client_factory(
                server,
                resolved_credentials,
                validated_endpoint,
            )
            return await value if inspect.isawaitable(value) else value

        return await run_health_discovery(create_client)

    async def _complete_attempt(
        self, server: Any, attempt_id: str, result: MCPHealthCheckResult
    ) -> None:
        await self._storage.complete_user_mcp_health_attempt(
            attempt_id,
            server.owner_user_id,
            server.server_id,
            runner_instance_id=self._instance_id,
            config_version=server.config_version,
            security_version=server.security_version,
            health_status="available" if result.available else "unavailable",
            error_code=result.error_code,
            completed_at=self._now(),
        )

    async def _renew(
        self, attempt: UserMCPHealthAttempt, stop: asyncio.Event, lease_lost: asyncio.Event
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._lease_renew_interval_seconds)
                return
            except TimeoutError:
                pass
            now = self._now()
            try:
                renewed = await self._storage.renew_user_mcp_health_attempt(
                    attempt.attempt_id,
                    attempt.owner_user_id,
                    attempt.server_id,
                    runner_instance_id=self._instance_id,
                    config_version=attempt.config_version,
                    security_version=attempt.security_version,
                    lease_expires_at=now + timedelta(seconds=self._lease_ttl_seconds),
                    updated_at=now,
                )
            except Exception:
                renewed = False
            if not renewed:
                lease_lost.set()
                return

    async def recover_expired(self) -> int:
        return int(await self._storage.expire_user_mcp_health_attempts(now=self._now()))

    async def start(self) -> None:
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._closing = False
        await self.recover_expired()
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(), name="user-mcp-health-recovery"
        )

    async def _recovery_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(HEALTH_RECOVERY_INTERVAL_SECONDS)
            try:
                await self.recover_expired()
            except Exception:
                continue

    async def cancel_server(
        self,
        owner_user_id: str,
        server_id: str,
        *,
        reason: str,
        invalidate_before_security_version: int | None = None,
    ) -> None:
        del reason
        selected = [
            attempt_id
            for attempt_id, attempt in self._attempts.items()
            if attempt.owner_user_id == owner_user_id
            and attempt.server_id == server_id
            and (
                invalidate_before_security_version is None
                or attempt.security_version < invalidate_before_security_version
            )
        ]
        for attempt_id in selected:
            task = self._tasks.get(attempt_id)
            if task is not None:
                task.cancel()
        if selected:
            await asyncio.gather(
                *(self._tasks[attempt_id] for attempt_id in selected if attempt_id in self._tasks),
                return_exceptions=True,
            )

    async def aclose(self) -> None:
        self._closing = True
        recovery = self._recovery_task
        if recovery is not None:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        self._recovery_task = None
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
