from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError

from src.orchestration.models import CapabilityDescriptor
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy

from .client import MCPClient
from .config import MCPRuntimeConfig, MCPServerConfig, MCPToolConfig
from .protocol import MCP_PROTOCOL_VERSION
from .transport_http import StreamableHTTPTransport

MCP_CAPABILITY_KIND = "mcp_tool"
MCP_CAPABILITY_SOURCE = "mcp"
_SUPPORTED_SCHEMA_DIALECTS = ("2020-12", "draft/2020-12", "draft-07", "draft7")


@dataclass(slots=True, frozen=True)
class MCPToolBinding:
    capability_id: str
    server_id: str
    tool_name: str
    planner_allowed_fields: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None
    max_output_bytes: int = 65_536
    risk_level: str = "read_only"


@dataclass(slots=True, frozen=True)
class MCPRuntimeDiagnostic:
    server_id: str
    reason: str
    message: str
    tool_name: str = ""
    capability_id: str = ""


@dataclass(slots=True, frozen=True)
class MCPRuntimeBundle:
    revision: str
    created_at: datetime
    descriptors: tuple[CapabilityDescriptor, ...] = ()
    payload_policies: Mapping[str, CapabilityPayloadPolicy] = field(default_factory=dict)
    bindings: Mapping[str, MCPToolBinding] = field(default_factory=dict)
    diagnostics: tuple[MCPRuntimeDiagnostic, ...] = ()


@dataclass(slots=True, frozen=True)
class MCPRuntimeRefreshResult:
    status: str
    reason: str
    previous_revision: str
    active_revision: str
    registered_count: int
    skipped_count: int
    duration_ms: int
    error_type: str = ""


@dataclass(slots=True, frozen=True)
class MCPRuntimePendingActivation:
    result: MCPRuntimeRefreshResult
    bundle: MCPRuntimeBundle
    clients: Mapping[str, Any] = field(default_factory=dict)


MCPClientFactory = Callable[[MCPServerConfig], Any]


class MCPRuntimeState:
    """Process-local MCP discovery bundle and client registry."""

    def __init__(
        self,
        *,
        config: MCPRuntimeConfig | Mapping[str, Any] | None = None,
        client_factory: MCPClientFactory | None = None,
        reserved_capability_ids: tuple[str, ...] | list[str] | set[str] = (),
    ) -> None:
        self._config = config if isinstance(config, MCPRuntimeConfig) else MCPRuntimeConfig.from_mapping(config)
        self._client_factory = client_factory or _default_client_factory(self._config.default_timeout_seconds)
        self._reserved_capability_ids = tuple(reserved_capability_ids)
        self._revision_counter = 0
        self._retained_counts: dict[str, int] = {}
        self._clients: dict[str, Any] = {}
        self._bundles: dict[str, MCPRuntimeBundle] = {}
        empty = self._make_bundle(descriptors=(), payload_policies={}, bindings={}, diagnostics=())
        self._active_revision = empty.revision
        self._bundles[empty.revision] = empty

    @property
    def config(self) -> MCPRuntimeConfig:
        return self._config

    @property
    def active_revision(self) -> str:
        return self._active_revision

    @property
    def active_bundle(self) -> MCPRuntimeBundle:
        return self._bundles[self._active_revision]

    def bundle_for_revision(self, revision: str | None = None) -> MCPRuntimeBundle:
        if not revision:
            return self.active_bundle
        try:
            return self._bundles[revision]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP bundle revision: {revision}") from exc

    def active_mcp_capability_ids(self) -> tuple[str, ...]:
        return tuple(self.active_bundle.bindings)

    def binding_for_capability(self, capability_id: str, revision: str | None = None) -> MCPToolBinding:
        bundle = self.bundle_for_revision(revision)
        try:
            return bundle.bindings[capability_id]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP capability: {capability_id}") from exc

    def retain_revision(self, revision: str | None) -> None:
        if not revision:
            return
        self.bundle_for_revision(revision)
        self._retained_counts[revision] = self._retained_counts.get(revision, 0) + 1

    def release_revision(self, revision: str | None) -> None:
        if not revision:
            return
        count = self._retained_counts.get(revision, 0)
        if count <= 1:
            self._retained_counts.pop(revision, None)
        else:
            self._retained_counts[revision] = count - 1
        self._evict_unretained_inactive_bundles()

    def refresh_sync(self, *, reason: str, force: bool = False) -> MCPRuntimeRefreshResult:
        return _run_coroutine_blocking(self.refresh(reason=reason, force=force))

    async def refresh(self, *, reason: str, force: bool = False) -> MCPRuntimeRefreshResult:
        pending = await self.prepare_refresh(reason=reason, force=force)
        if pending.result.status == "completed":
            await self.commit_activation(pending)
        else:
            await self.discard_activation(pending)
        return pending.result

    def prepare_refresh_sync(self, *, reason: str, force: bool = False) -> MCPRuntimePendingActivation:
        return _run_coroutine_blocking(self.prepare_refresh(reason=reason, force=force))

    def commit_activation_sync(self, pending: MCPRuntimePendingActivation) -> None:
        _run_coroutine_blocking(self.commit_activation(pending))

    def discard_activation_sync(self, pending: MCPRuntimePendingActivation) -> None:
        _run_coroutine_blocking(self.discard_activation(pending))

    async def prepare_refresh(self, *, reason: str, force: bool = False) -> MCPRuntimePendingActivation:
        started = time.monotonic()
        previous = self.active_bundle
        if not self._config.enabled:
            return MCPRuntimePendingActivation(
                result=self._refresh_result(status="skipped", reason="disabled", previous_revision=previous.revision, active_revision=previous.revision, started=started),
                bundle=previous,
            )
        if not force and previous.descriptors:
            return MCPRuntimePendingActivation(
                result=self._refresh_result(status="skipped", reason="unchanged", previous_revision=previous.revision, active_revision=previous.revision, started=started),
                bundle=previous,
            )

        next_clients: dict[str, Any] = {}
        diagnostics: list[MCPRuntimeDiagnostic] = []
        descriptors: dict[str, CapabilityDescriptor] = {}
        policies: dict[str, CapabilityPayloadPolicy] = {}
        bindings: dict[str, MCPToolBinding] = {}
        discovery_failed = False
        fatal_error_type = ""

        try:
            for server in self._config.servers:
                if not server.enabled:
                    continue
                server_error = server.validation_error()
                if server_error:
                    diagnostics.append(MCPRuntimeDiagnostic(server_id=server.server_id, reason="invalid_server_config", message=server_error))
                    if server.required:
                        raise RuntimeError(server_error)
                    continue
                client = self._client_factory(server)
                try:
                    raw_tools = await _maybe_await(client.list_tools())
                except Exception as exc:
                    discovery_failed = True
                    fatal_error_type = type(exc).__name__
                    diagnostics.append(MCPRuntimeDiagnostic(server_id=server.server_id, reason="server_discovery_failed", message=type(exc).__name__))
                    await _close_client(client)
                    if server.required:
                        raise
                    continue
                next_clients[server.server_id] = client
                tool_by_name = {str(tool.get("name") or ""): tool for tool in raw_tools if isinstance(tool, Mapping)}
                self._merge_server_tools(
                    server,
                    tool_by_name=tool_by_name,
                    descriptors=descriptors,
                    policies=policies,
                    bindings=bindings,
                    diagnostics=diagnostics,
                )
        except Exception as exc:
            for client in next_clients.values():
                await _close_client(client)
            return MCPRuntimePendingActivation(
                result=self._refresh_result(
                    status="failed",
                    reason=reason,
                    previous_revision=previous.revision,
                    active_revision=previous.revision,
                    started=started,
                    error_type=type(exc).__name__,
                ),
                bundle=previous,
            )

        if discovery_failed and not descriptors:
            for client in next_clients.values():
                await _close_client(client)
            return MCPRuntimePendingActivation(
                result=self._refresh_result(
                    status="failed",
                    reason=reason,
                    previous_revision=previous.revision,
                    active_revision=previous.revision,
                    started=started,
                    error_type=fatal_error_type or "MCPDiscoveryError",
                ),
                bundle=previous,
            )

        bundle = self._make_bundle(
            descriptors=tuple(descriptors.values()),
            payload_policies=policies,
            bindings=bindings,
            diagnostics=tuple(diagnostics),
        )
        result = MCPRuntimeRefreshResult(
            status="completed",
            reason=reason,
            previous_revision=previous.revision,
            active_revision=bundle.revision,
            registered_count=len(bundle.descriptors),
            skipped_count=len(bundle.diagnostics),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return MCPRuntimePendingActivation(result=result, bundle=bundle, clients=next_clients)

    async def commit_activation(self, pending: MCPRuntimePendingActivation) -> None:
        if pending.result.status != "completed":
            return
        old_clients = self._clients
        next_clients = dict(pending.clients)
        self._clients = next_clients
        self._bundles[pending.bundle.revision] = pending.bundle
        self._active_revision = pending.bundle.revision
        self._evict_unretained_inactive_bundles()
        for server_id, client in old_clients.items():
            if next_clients.get(server_id) is not client:
                await _close_client(client)

    async def discard_activation(self, pending: MCPRuntimePendingActivation) -> None:
        if pending.result.status != "completed":
            return
        current_clients = set(self._clients.values())
        for client in pending.clients.values():
            if client not in current_clients:
                await _close_client(client)

    def _merge_server_tools(
        self,
        server: MCPServerConfig,
        *,
        tool_by_name: Mapping[str, Mapping[str, Any]],
        descriptors: dict[str, CapabilityDescriptor],
        policies: dict[str, CapabilityPayloadPolicy],
        bindings: dict[str, MCPToolBinding],
        diagnostics: list[MCPRuntimeDiagnostic],
    ) -> None:
        reserved = set(self._reserved_capability_ids) | set(descriptors)
        for tool_config in server.tools:
            if not tool_config.expose:
                continue
            capability_id = tool_config.effective_capability_id(server.server_id)
            tool_name = tool_config.tool_name
            if tool_config.risk_level != "read_only":
                diagnostics.append(_diagnostic(server, tool_config, "unsupported_risk_level", "Generic public MCP tools must be read_only in Phase 1.", capability_id))
                continue
            if not tool_name:
                diagnostics.append(_diagnostic(server, tool_config, "missing_tool_name", "MCP public tool requires tool_name.", capability_id))
                continue
            if tool_name not in tool_by_name:
                diagnostics.append(_diagnostic(server, tool_config, "tool_not_discovered", "Configured MCP tool was not discovered from server.", capability_id))
                continue
            if not MCPToolConfig.valid_capability_id(capability_id) or capability_id in reserved:
                diagnostics.append(_diagnostic(server, tool_config, "reserved_or_duplicate_capability_id", f"Invalid or reserved MCP capability id: {capability_id}", capability_id))
                continue
            if not tool_config.public_name or not tool_config.public_description:
                diagnostics.append(_diagnostic(server, tool_config, "missing_public_descriptor", "Public MCP capability requires local public_name and public_description.", capability_id))
                continue
            discovered_tool = tool_by_name[tool_name]
            input_schema = _coerce_schema(tool_config.input_schema or discovered_tool.get("inputSchema") or {"type": "object"})
            output_schema = _coerce_optional_schema(tool_config.output_schema or discovered_tool.get("outputSchema"))
            schema_error = _validate_supported_schema(input_schema)
            if schema_error:
                diagnostics.append(_diagnostic(server, tool_config, "unsupported_input_schema", schema_error, capability_id))
                continue
            if output_schema:
                output_schema_error = _validate_supported_schema(output_schema)
                if output_schema_error:
                    diagnostics.append(_diagnostic(server, tool_config, "unsupported_output_schema", output_schema_error, capability_id))
                    continue
            allowlist_error = _validate_planner_allowlist(tool_config.planner_allowed_fields, input_schema)
            if allowlist_error:
                diagnostics.append(_diagnostic(server, tool_config, "invalid_planner_allowlist", allowlist_error, capability_id))
                continue

            binding = MCPToolBinding(
                capability_id=capability_id,
                server_id=server.server_id,
                tool_name=tool_name,
                planner_allowed_fields=tool_config.planner_allowed_fields,
                input_schema=input_schema,
                output_schema=output_schema,
                max_output_bytes=tool_config.max_output_bytes or server.limits.max_output_bytes,
                risk_level=tool_config.risk_level,
            )
            descriptors[capability_id] = CapabilityDescriptor(
                capability_id=capability_id,
                name=tool_config.public_name,
                description=tool_config.public_description,
                version="1",
                enabled=True,
                public=True,
                kind=MCP_CAPABILITY_KIND,
                source=MCP_CAPABILITY_SOURCE,
                source_path=f"{server.server_id}/{tool_name}",
            )
            policies[capability_id] = CapabilityPayloadPolicy(planner_allowed_fields=tool_config.planner_allowed_fields)
            bindings[capability_id] = binding
            reserved.add(capability_id)

    async def call_tool(self, capability_id: str, arguments: Mapping[str, Any], revision: str | None = None) -> Mapping[str, Any]:
        binding = self.binding_for_capability(capability_id, revision)
        client = self._clients.get(binding.server_id)
        if client is None:
            raise KeyError(f"MCP client is not active for server: {binding.server_id}")
        allowed = set(binding.planner_allowed_fields)
        filtered_arguments = {
            key: value
            for key, value in dict(arguments).items()
            if key in allowed
        }
        result = client.call_tool(binding.tool_name, filtered_arguments)
        return await _maybe_await(result)

    async def aclose(self) -> None:
        clients = self._clients
        self._clients = {}
        for client in clients.values():
            await _close_client(client)

    def _make_bundle(
        self,
        *,
        descriptors: tuple[CapabilityDescriptor, ...],
        payload_policies: Mapping[str, CapabilityPayloadPolicy],
        bindings: Mapping[str, MCPToolBinding],
        diagnostics: tuple[MCPRuntimeDiagnostic, ...],
    ) -> MCPRuntimeBundle:
        self._revision_counter += 1
        digest = _bundle_digest(descriptors, bindings, diagnostics)
        return MCPRuntimeBundle(
            revision=f"mcprev-{self._revision_counter:06d}-{digest}",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            descriptors=descriptors,
            payload_policies=dict(payload_policies),
            bindings=dict(bindings),
            diagnostics=diagnostics,
        )

    def _evict_unretained_inactive_bundles(self) -> None:
        for revision in list(self._bundles):
            if revision == self._active_revision:
                continue
            if self._retained_counts.get(revision, 0) > 0:
                continue
            self._bundles.pop(revision, None)

    def _refresh_result(
        self,
        *,
        status: str,
        reason: str,
        previous_revision: str,
        active_revision: str,
        started: float,
        registered_count: int | None = None,
        skipped_count: int | None = None,
        error_type: str = "",
    ) -> MCPRuntimeRefreshResult:
        active = self._bundles[active_revision]
        return MCPRuntimeRefreshResult(
            status=status,
            reason=reason,
            previous_revision=previous_revision,
            active_revision=active_revision,
            registered_count=len(active.descriptors) if registered_count is None else registered_count,
            skipped_count=len(active.diagnostics) if skipped_count is None else skipped_count,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_type=error_type,
        )


def _default_client_factory(default_timeout_seconds: float) -> MCPClientFactory:
    def _factory(server: MCPServerConfig) -> MCPClient:
        return MCPClient(
            server_id=server.server_id,
            transport=StreamableHTTPTransport(endpoint=server.endpoint, auth=server.auth),
            protocol_version=server.protocol_version or MCP_PROTOCOL_VERSION,
            timeout_seconds=server.limits.timeout_seconds or default_timeout_seconds,
            client_capabilities={},
        )

    return _factory


def _diagnostic(server: MCPServerConfig, tool: MCPToolConfig, reason: str, message: str, capability_id: str) -> MCPRuntimeDiagnostic:
    return MCPRuntimeDiagnostic(
        server_id=server.server_id,
        tool_name=tool.tool_name,
        capability_id=capability_id,
        reason=reason,
        message=message,
    )


def _coerce_schema(value: Any) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {"type": "object"}


def _coerce_optional_schema(value: Any) -> Mapping[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _validate_supported_schema(schema: Mapping[str, Any]) -> str:
    schema_uri = str(schema.get("$schema") or "").lower()
    if schema_uri and not any(token in schema_uri for token in _SUPPORTED_SCHEMA_DIALECTS):
        return f"Unsupported JSON Schema dialect: {schema.get('$schema')}"
    validator_cls = Draft7Validator if "draft-07" in schema_uri or "draft7" in schema_uri else Draft202012Validator
    try:
        validator_cls.check_schema(dict(schema))
    except SchemaError as exc:
        return f"Invalid JSON Schema: {exc.message}"
    return ""


def _validate_planner_allowlist(fields: tuple[str, ...], schema: Mapping[str, Any]) -> str:
    if not fields:
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and properties:
            return "Public generic MCP tool requires explicit planner_allowed_fields."
        return ""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return ""
    unknown = sorted(set(fields) - {str(key) for key in properties})
    if unknown:
        return f"Planner allowlist fields are not present in inputSchema: {', '.join(unknown)}"
    return ""


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_client(client: Any) -> None:
    close = getattr(client, "close", None) or getattr(client, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


def _run_coroutine_blocking(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_holder: dict[str, Any] = {}
    error_holder: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result_holder["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive handoff
            error_holder["error"] = exc

    thread = threading.Thread(target=_runner, name="mcp-runtime-refresh", daemon=True)
    thread.start()
    thread.join()
    if "error" in error_holder:
        raise error_holder["error"]
    return result_holder.get("value")


def _bundle_digest(
    descriptors: tuple[CapabilityDescriptor, ...],
    bindings: Mapping[str, MCPToolBinding],
    diagnostics: tuple[MCPRuntimeDiagnostic, ...],
) -> str:
    raw = "\n".join(
        [*(descriptor.capability_id for descriptor in descriptors), *(f"{cap}:{binding.server_id}/{binding.tool_name}" for cap, binding in bindings.items()), *(f"{diag.server_id}:{diag.tool_name}:{diag.reason}" for diag in diagnostics)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
