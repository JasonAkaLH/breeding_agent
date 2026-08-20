from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError

from src.orchestration.models import CapabilityDescriptor
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy

from .adapter import PythonLegacyMCPClientAdapter
from .client import MCPClient, MCPClientError
from .config import MCPRuntimeConfig, MCPServerConfig, MCPToolConfig
from .protocol import MCP_PROTOCOL_VERSION, MCP_TRANSPORT_LEGACY_HTTP_SSE, MCP_TRANSPORT_STREAMABLE_HTTP
from .rust_contract import contract_value as mcp_contract_value
from .rust_contract import status_list as mcp_status_list
from .sidecar import MCPSidecarMode
from .tasks import InMemoryMCPTaskRegistry, is_create_task_result, validate_related_task_result_metadata
from .transport_http import StreamableHTTPTransport
from .transport_legacy_http_sse import LegacyHTTPSSETransport

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
    output_schema_sha256: str | None = None
    protocol_version: str = "2025-11-25"
    max_output_bytes: int = 65_536
    risk_level: str = "read_only"
    task_support: str = "forbidden"
    task_augmented_mode: str = "disabled"
    task_augmented_call: bool = False
    task_ttl_ms: int = 60000
    task_max_polls: int = 20
    transport_security: str = ""
    header_names: tuple[str, ...] = ()
    credential_over_plaintext_http: bool = False

    def __post_init__(self) -> None:
        if self.output_schema is not None and self.output_schema_sha256 is None:
            canonical = json.dumps(
                self.output_schema,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            object.__setattr__(
                self,
                "output_schema_sha256",
                "sha256:" + hashlib.sha256(canonical).hexdigest(),
            )


@dataclass(slots=True, frozen=True)
class MCPRuntimeDiagnostic:
    server_id: str
    reason: str
    message: str
    tool_name: str = ""
    capability_id: str = ""
    requested_protocol_version: str = ""
    negotiated_protocol_version: str = ""
    transport_family: str = ""
    transport_security: str = ""
    header_names: tuple[str, ...] = ()
    required: bool = False


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


@dataclass(slots=True, frozen=True)
class MCPInflightRequest:
    platform_task_id: str
    server_id: str
    tool_name: str
    request_id: str | int
    safe_ref: str
    client: Any


MCPClientFactory = Callable[[MCPServerConfig], Any]


class MCPRuntimeState:
    """Process-local MCP discovery bundle and client registry."""

    def __init__(
        self,
        *,
        config: MCPRuntimeConfig | Mapping[str, Any] | None = None,
        client_factory: MCPClientFactory | None = None,
        sidecar_client: Any | None = None,
        task_registry: InMemoryMCPTaskRegistry | None = None,
        reserved_capability_ids: tuple[str, ...] | list[str] | set[str] = (),
    ) -> None:
        self._config = config if isinstance(config, MCPRuntimeConfig) else MCPRuntimeConfig.from_mapping(config)
        self._client_factory = client_factory or _default_client_factory(self._config.default_timeout_seconds)
        self._sidecar_client = sidecar_client
        self._task_registry = task_registry or InMemoryMCPTaskRegistry()
        self._reserved_capability_ids = tuple(reserved_capability_ids)
        self._revision_counter = 0
        self._retained_counts: dict[str, int] = {}
        self._clients: dict[str, Any] = {}
        self._bundles: dict[str, MCPRuntimeBundle] = {}
        self._last_refresh_diagnostics: tuple[MCPRuntimeDiagnostic, ...] = ()
        self._inflight_by_platform_task: dict[str, dict[str, MCPInflightRequest]] = {}
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

    @property
    def last_refresh_diagnostics(self) -> tuple[MCPRuntimeDiagnostic, ...]:
        return self._last_refresh_diagnostics

    def binding_for_capability(self, capability_id: str, revision: str | None = None) -> MCPToolBinding:
        bundle = self.bundle_for_revision(revision)
        try:
            return bundle.bindings[capability_id]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP capability: {capability_id}") from exc

    def metric_dimension_for_capability(
        self,
        capability_id: str,
        revision: str | None = None,
    ) -> tuple[str, str]:
        """Return the closed transport/protocol dimension for legacy telemetry."""

        binding = self.binding_for_capability(capability_id, revision)
        server = next(
            item
            for item in self._config.servers
            if item.server_id == binding.server_id
        )
        client = self._clients.get(binding.server_id)
        session = getattr(client, "negotiated_session", None)
        negotiated = str(
            getattr(session, "negotiated_protocol_version", "") or ""
        ).strip()
        return server.transport, negotiated or server.protocol_version

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
            self._last_refresh_diagnostics = ()
            return MCPRuntimePendingActivation(
                result=self._refresh_result(
                    status="skipped",
                    reason="disabled",
                    previous_revision=previous.revision,
                    active_revision=previous.revision,
                    started=started,
                ),
                bundle=previous,
            )
        if not force and previous.descriptors:
            self._last_refresh_diagnostics = ()
            return MCPRuntimePendingActivation(
                result=self._refresh_result(
                    status="skipped",
                    reason="unchanged",
                    previous_revision=previous.revision,
                    active_revision=previous.revision,
                    started=started,
                ),
                bundle=previous,
            )

        next_clients: dict[str, Any] = {}
        diagnostics: list[MCPRuntimeDiagnostic] = []
        descriptors: dict[str, CapabilityDescriptor] = {}
        policies: dict[str, CapabilityPayloadPolicy] = {}
        bindings: dict[str, MCPToolBinding] = {}
        discovery_failed = False
        fatal_error_type = ""

        sidecar_gate = await self._check_sidecar_gate(
            diagnostics=diagnostics,
            previous=previous,
            reason=reason,
            started=started,
        )
        if sidecar_gate is not None:
            self._last_refresh_diagnostics = tuple(diagnostics)
            return sidecar_gate

        try:
            for server in self._config.servers:
                if not server.enabled:
                    continue
                server_error = server.validation_error()
                if server_error:
                    diagnostics.append(
                        MCPRuntimeDiagnostic(
                            server_id=server.server_id,
                            reason="invalid_server_config",
                            message=server_error,
                            requested_protocol_version=server.protocol_version,
                            transport_family=server.transport,
                            transport_security=server.transport_security,
                            header_names=server.request_header_names,
                            required=server.required,
                        )
                    )
                    if server.required:
                        raise RuntimeError(server_error)
                    continue
                client = None
                try:
                    client = self._client_factory(server)
                    raw_tools = await _maybe_await(client.list_tools())
                except Exception as exc:
                    discovery_failed = True
                    fatal_error_type = type(exc).__name__
                    exc_metadata = getattr(exc, "metadata", {}) if isinstance(getattr(exc, "metadata", {}), Mapping) else {}
                    error_code = getattr(exc, "mcp_error_code", "")
                    diagnostics.append(
                        MCPRuntimeDiagnostic(
                            server_id=server.server_id,
                            reason=error_code if str(error_code).startswith("legacy_") else "server_discovery_failed",
                            message=type(exc).__name__,
                            requested_protocol_version=str(
                                exc_metadata.get("requested_protocol_version") or server.protocol_version
                            ),
                            negotiated_protocol_version=str(exc_metadata.get("negotiated_protocol_version") or ""),
                            transport_family=str(exc_metadata.get("transport_family") or server.transport),
                            transport_security=server.transport_security,
                            header_names=server.request_header_names,
                            required=server.required,
                        )
                    )
                    if client is not None:
                        await _close_client(client)
                    if server.required:
                        raise
                    continue
                next_clients[server.server_id] = client
                tool_by_name = {str(tool.get("name") or ""): tool for tool in raw_tools if isinstance(tool, Mapping)}
                self._merge_server_tools(
                    server,
                    tool_by_name=tool_by_name,
                    server_capabilities=_server_capabilities(client),
                    descriptors=descriptors,
                    policies=policies,
                    bindings=bindings,
                    diagnostics=diagnostics,
                    protocol_version=(
                        str(
                            getattr(
                                getattr(client, "negotiated_session", None),
                                "negotiated_protocol_version",
                                "",
                            )
                            or server.protocol_version
                        )
                    ),
                )
        except Exception as exc:
            for client in next_clients.values():
                await _close_client(client)
            self._last_refresh_diagnostics = tuple(diagnostics)
            bundle = self._make_bundle(
                descriptors=previous.descriptors,
                payload_policies=previous.payload_policies,
                bindings=previous.bindings,
                diagnostics=tuple(previous.diagnostics) + tuple(diagnostics),
            )
            return MCPRuntimePendingActivation(
                result=MCPRuntimeRefreshResult(
                    status="failed",
                    reason=reason,
                    previous_revision=previous.revision,
                    active_revision=previous.revision,
                    registered_count=len(previous.descriptors),
                    skipped_count=len(bundle.diagnostics),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_type=type(exc).__name__,
                ),
                bundle=bundle,
            )

        if discovery_failed and not descriptors and previous.descriptors:
            for client in next_clients.values():
                await _close_client(client)
            bundle = self._make_bundle(
                descriptors=previous.descriptors,
                payload_policies=previous.payload_policies,
                bindings=previous.bindings,
                diagnostics=tuple(previous.diagnostics) + tuple(diagnostics),
            )
            self._last_refresh_diagnostics = tuple(diagnostics)
            return MCPRuntimePendingActivation(
                result=MCPRuntimeRefreshResult(
                    status="completed",
                    reason="optional_discovery_failed",
                    previous_revision=previous.revision,
                    active_revision=bundle.revision,
                    registered_count=len(bundle.descriptors),
                    skipped_count=len(bundle.diagnostics),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_type=fatal_error_type or "MCPDiscoveryError",
                ),
                bundle=bundle,
                clients=dict(self._clients),
            )

        bundle = self._make_bundle(
            descriptors=tuple(descriptors.values()),
            payload_policies=policies,
            bindings=bindings,
            diagnostics=tuple(diagnostics),
        )
        self._last_refresh_diagnostics = tuple(diagnostics)
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


    async def _check_sidecar_gate(
        self,
        *,
        diagnostics: list[MCPRuntimeDiagnostic],
        previous: MCPRuntimeBundle,
        reason: str,
        started: float,
    ) -> MCPRuntimePendingActivation | None:
        settings = self._config.rust_runtime
        if settings.mode == MCPSidecarMode.OFF:
            return None

        config_error = settings.validation_error()
        if config_error:
            diagnostics.append(MCPRuntimeDiagnostic(server_id="mcp_sidecar", reason="sidecar_invalid_config", message=config_error))
            if settings.mode == MCPSidecarMode.ENFORCE:
                return MCPRuntimePendingActivation(
                    result=self._refresh_result(
                        status="failed",
                        reason=reason,
                        previous_revision=previous.revision,
                        active_revision=previous.revision,
                        started=started,
                        error_type="MCPSidecarInvalidConfig",
                    ),
                    bundle=previous,
                )
            return None

        if self._sidecar_client is None:
            diagnostics.append(
                MCPRuntimeDiagnostic(
                    server_id="mcp_sidecar",
                    reason="sidecar_unavailable",
                    message="MCP Rust sidecar client is not configured; Python legacy path remains user-visible in shadow mode.",
                )
            )
            if settings.mode == MCPSidecarMode.ENFORCE:
                return MCPRuntimePendingActivation(
                    result=self._refresh_result(
                        status="failed",
                        reason=reason,
                        previous_revision=previous.revision,
                        active_revision=previous.revision,
                        started=started,
                        error_type="MCPSidecarUnavailable",
                    ),
                    bundle=previous,
                )
            return None

        try:
            await self._sidecar_client.handshake(required_features=settings.required_features)
        except Exception as exc:
            diagnostics.append(
                MCPRuntimeDiagnostic(
                    server_id="mcp_sidecar",
                    reason="sidecar_incompatible",
                    message=type(exc).__name__,
                )
            )
            if settings.mode == MCPSidecarMode.ENFORCE:
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
        if settings.mode == MCPSidecarMode.ENFORCE and not _sidecar_runtime_operations_available(self._sidecar_client):
            diagnostics.append(
                MCPRuntimeDiagnostic(
                    server_id="mcp_sidecar",
                    reason="sidecar_runtime_unavailable",
                    message="MCP Rust sidecar does not expose canonical runtime operations; enforce mode fails closed.",
                )
            )
            return MCPRuntimePendingActivation(
                result=self._refresh_result(
                    status="failed",
                    reason=reason,
                    previous_revision=previous.revision,
                    active_revision=previous.revision,
                    started=started,
                    error_type="MCPSidecarRuntimeUnavailable",
                ),
                bundle=previous,
            )
        return None

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
        server_capabilities: Mapping[str, Any],
        descriptors: dict[str, CapabilityDescriptor],
        policies: dict[str, CapabilityPayloadPolicy],
        bindings: dict[str, MCPToolBinding],
        diagnostics: list[MCPRuntimeDiagnostic],
        protocol_version: str,
    ) -> None:
        reserved = set(self._reserved_capability_ids) | set(descriptors)
        for tool_config in server.tools:
            if not tool_config.expose:
                continue
            capability_id = tool_config.effective_capability_id(server.server_id)
            tool_name = tool_config.tool_name
            if tool_config.risk_level != "read_only":
                diagnostics.append(
                    _diagnostic(
                        server,
                        tool_config,
                        "unsupported_risk_level",
                        "Generic public MCP tools must be read_only in Phase 1.",
                        capability_id,
                    )
                )
                continue
            if not tool_name:
                diagnostics.append(
                    _diagnostic(
                        server,
                        tool_config,
                        "missing_tool_name",
                        "MCP public tool requires tool_name.",
                        capability_id,
                    )
                )
                continue
            if tool_name not in tool_by_name:
                diagnostics.append(
                    _diagnostic(
                        server,
                        tool_config,
                        "tool_not_discovered",
                        "Configured MCP tool was not discovered from server.",
                        capability_id,
                    )
                )
                continue
            if not MCPToolConfig.valid_capability_id(capability_id) or capability_id in reserved:
                diagnostics.append(
                    _diagnostic(
                        server,
                        tool_config,
                        "reserved_or_duplicate_capability_id",
                        f"Invalid or reserved MCP capability id: {capability_id}",
                        capability_id,
                    )
                )
                continue
            if not tool_config.public_name or not tool_config.public_description:
                diagnostics.append(
                    _diagnostic(
                        server,
                        tool_config,
                        "missing_public_descriptor",
                        "Public MCP capability requires local public_name and public_description.",
                        capability_id,
                    )
                )
                continue
            discovered_tool = tool_by_name[tool_name]
            task_support = _tool_task_support(discovered_tool)
            task_decision = _task_augmented_decision(
                server_capabilities=server_capabilities,
                task_support=task_support,
                mode=tool_config.task_augmented_mode,
            )
            if task_decision == "fail_closed":
                diagnostics.append(
                    _diagnostic(
                        server,
                        tool_config,
                        _task_diagnostic_reason(server_capabilities, task_support, tool_config.task_augmented_mode),
                        "MCP task augmentation is required but not negotiated for this tool.",
                        capability_id,
                    )
                )
                continue
            if task_decision == "plain_call" and tool_config.task_augmented_mode == "preferred":
                diagnostics.append(
                    _diagnostic(
                        server,
                        tool_config,
                        "task_augmentation_unavailable",
                        "MCP task augmentation is preferred but not negotiated; using ordinary tools/call.",
                        capability_id,
                    )
                )
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
                output_schema_sha256=(
                    "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            output_schema,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    if output_schema is not None
                    else None
                ),
                protocol_version=protocol_version,
                max_output_bytes=tool_config.max_output_bytes or server.limits.max_output_bytes,
                risk_level=tool_config.risk_level,
                task_support=task_support,
                task_augmented_mode=tool_config.task_augmented_mode,
                task_augmented_call=task_decision in {"task_augmented", "task_augmented_preferred"},
                task_ttl_ms=tool_config.task_ttl_ms,
                task_max_polls=tool_config.task_max_polls,
                transport_security=server.transport_security,
                header_names=server.request_header_names,
                credential_over_plaintext_http=server.credential_over_plaintext_http,
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

    async def call_tool(
        self,
        capability_id: str,
        arguments: Mapping[str, Any],
        revision: str | None = None,
        event_callback=None,
        request_context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
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
        context = dict(request_context or {})
        platform_task_id = str(context.get("task_id") or "")
        registered_request_ids: list[str | int] = []

        def _register_request(request_id: str | int) -> None:
            registered_request_ids.append(request_id)
            self._register_inflight_request(
                platform_task_id=platform_task_id,
                server_id=binding.server_id,
                tool_name=binding.tool_name,
                client=client,
                request_id=request_id,
            )

        if binding.task_augmented_call:
            progress_token = self._task_registry.make_progress_token(
                server_id=binding.server_id,
                tool_name=binding.tool_name,
            )
            call_kwargs: dict[str, Any] = {
                "task_augmented": True,
                "progress_token": progress_token,
                "task_ttl_ms": binding.task_ttl_ms,
            }
            if _callable_accepts_keyword(getattr(client, "call_tool", None), "request_registered_callback"):
                call_kwargs["request_registered_callback"] = _register_request
            try:
                result = await _maybe_await(
                    client.call_tool(
                        binding.tool_name,
                        filtered_arguments,
                        **call_kwargs,
                    )
                )
            except asyncio.CancelledError:
                await self._cancel_registered_inflight_before_reraising(platform_task_id)
                raise
            finally:
                self._complete_registered_inflight(platform_task_id, registered_request_ids)
            if isinstance(result, Mapping) and is_create_task_result(result):
                return await self._resolve_task_result(
                    client,
                    binding,
                    result,
                    progress_token,
                    event_callback=event_callback,
                    request_context=context,
                )
            return result
        call_kwargs: dict[str, Any] = {}
        if _callable_accepts_keyword(getattr(client, "call_tool", None), "request_registered_callback"):
            call_kwargs["request_registered_callback"] = _register_request
        try:
            result = client.call_tool(binding.tool_name, filtered_arguments, **call_kwargs)
            return await _maybe_await(result)
        except asyncio.CancelledError:
            await self._cancel_registered_inflight_before_reraising(platform_task_id)
            raise
        finally:
            self._complete_registered_inflight(platform_task_id, registered_request_ids)

    async def _resolve_task_result(
        self,
        client: Any,
        binding: MCPToolBinding,
        create_task_result: Mapping[str, Any],
        progress_token: str | int,
        event_callback=None,
        request_context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        raw_task_id = str(create_task_result.get("taskId") or "").strip()
        record = self._task_registry.create_record(
            server_id=binding.server_id,
            tool_name=binding.tool_name,
            capability_id=binding.capability_id,
            mcp_task_id=raw_task_id,
            progress_token=progress_token,
            status_payload=create_task_result,
            poll_interval_ms=_positive_int_or_none(create_task_result.get("pollInterval")),
            platform_task_id=str((request_context or {}).get("task_id") or ""),
            platform_node_id=str((request_context or {}).get("node_id") or ""),
            conversation_id=str((request_context or {}).get("conversation_id") or ""),
        )
        await _emit_mcp_event(
            event_callback,
            "mcp.long_task_started",
            {
                "server_id": binding.server_id,
                "tool_name": binding.tool_name,
                "capability_id": binding.capability_id,
                "safe_ref": record.safe_ref,
            },
        )
        record = await self._emit_client_task_notifications(client, record, event_callback)
        cancelled_status = mcp_contract_value("task_cancelled_state")
        completed_status = mcp_contract_value("task_completed_state")
        failed_status = mcp_contract_value("task_failed_state")
        input_required_status = mcp_contract_value("task_input_required_state")
        for _ in range(binding.task_max_polls):
            status_payload = await _maybe_await(client.tasks_get(raw_task_id))
            record = self._task_registry.update_status(raw_task_id, status_payload)
            record = await self._emit_client_task_notifications(client, record, event_callback)
            await _emit_mcp_event(
                event_callback,
                "mcp.long_task_status",
                {
                    "status": record.status,
                    "status_message": record.status_message,
                    "safe_ref": record.safe_ref,
                },
            )
            if record.status == input_required_status:
                raise MCPClientError(
                    "MCP task input_required is unsupported.",
                    code="mcp_runtime_task_input_required_unsupported",
                    retriable=False,
                )
            if record.status in {failed_status, cancelled_status}:
                raise MCPClientError(
                    f"MCP task ended with {record.status}.",
                    code=f"mcp_runtime_task_{record.status}",
                    retriable=False,
                )
            if record.status == completed_status:
                result = await _maybe_await(client.tasks_result(raw_task_id))
                if isinstance(result, Mapping):
                    validate_related_task_result_metadata(result, raw_task_id)
                    self._task_registry.update_status(
                        raw_task_id,
                        {"status": {"state": completed_status, "message": record.status_message}},
                    )
                    output_size = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
                    await _emit_mcp_event(
                        event_callback,
                        "mcp.long_task_completed",
                        {
                            "safe_ref": record.safe_ref,
                            "duration_ms": 0,
                            "output_size_bytes": output_size,
                            "truncated": False,
                        },
                    )
                    return result
                raise MCPClientError(
                    "MCP task result is unavailable.",
                    code="mcp_runtime_task_result_unavailable",
                    retriable=True,
                )
        raise MCPClientError(
            "MCP task result is unavailable.",
            code="mcp_runtime_task_result_unavailable",
            retriable=True,
        )

    async def cancel_platform_task(self, task_id: str, *, reason: str = "user_requested") -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        inflight = list(self._inflight_by_platform_task.get(task_id, {}).values())
        cancelled_status = mcp_contract_value("task_cancelled_state")
        for request in inflight:
            events.append(
                {
                    "event_type": "mcp.long_task_cancel_requested",
                    "payload": {"safe_ref": request.safe_ref, "reason": reason},
                }
            )
            try:
                cancel_request = getattr(request.client, "cancel_request", None)
                if callable(cancel_request):
                    await _maybe_await(cancel_request(request.request_id, reason=reason))
                else:
                    send_notification = getattr(request.client, "send_notification", None)
                    if not callable(send_notification):
                        raise AttributeError("MCP client cannot send cancellation notification.")
                    await _maybe_await(
                        send_notification(
                            "notifications/cancelled",
                            {"requestId": request.request_id, "reason": reason},
                        )
                    )
            except Exception:
                events.append(
                    _mcp_task_failure_event(
                        safe_ref=request.safe_ref,
                        error_code="mcp_runtime_request_cancel_failed",
                    )
                )
                continue
            events.append(_mcp_task_cancelled_event(safe_ref=request.safe_ref, status=cancelled_status))
        for record in self._task_registry.records_for_platform_task(task_id):
            if record.status in mcp_status_list("task_terminal_states"):
                continue
            events.append(
                {
                    "event_type": "mcp.long_task_cancel_requested",
                    "payload": {"safe_ref": record.safe_ref, "reason": reason},
                }
            )
            client = self._clients.get(record.server_id)
            if client is None:
                events.append(
                    _mcp_task_failure_event(
                        safe_ref=record.safe_ref,
                        error_code="mcp_runtime_task_cancel_failed",
                    )
                )
                continue
            try:
                await _maybe_await(client.tasks_cancel(record.mcp_task_id, reason=reason))
            except Exception:
                events.append(
                    _mcp_task_failure_event(
                        safe_ref=record.safe_ref,
                        error_code="mcp_runtime_task_cancel_failed",
                    )
                )
                continue
            self._task_registry.update_status(record.mcp_task_id, {"status": {"state": cancelled_status, "message": reason}})
            events.append(_mcp_task_cancelled_event(safe_ref=record.safe_ref, status=cancelled_status))
        if inflight:
            self._inflight_by_platform_task.pop(task_id, None)
        return events

    def _register_inflight_request(
        self,
        *,
        platform_task_id: str,
        server_id: str,
        tool_name: str,
        client: Any,
        request_id: str | int,
    ) -> None:
        if not platform_task_id:
            return
        safe_ref = self._task_registry.make_safe_ref(server_id=server_id, tool_name=tool_name)
        record = MCPInflightRequest(
            platform_task_id=platform_task_id,
            server_id=server_id,
            tool_name=tool_name,
            request_id=request_id,
            safe_ref=safe_ref,
            client=client,
        )
        self._inflight_by_platform_task.setdefault(platform_task_id, {})[str(request_id)] = record

    def _complete_registered_inflight(self, platform_task_id: str, request_ids: list[str | int]) -> None:
        if not platform_task_id or not request_ids:
            return
        entries = self._inflight_by_platform_task.get(platform_task_id)
        if not entries:
            return
        for request_id in request_ids:
            entries.pop(str(request_id), None)
        if not entries:
            self._inflight_by_platform_task.pop(platform_task_id, None)

    async def _cancel_registered_inflight_before_reraising(self, platform_task_id: str) -> None:
        if not platform_task_id:
            return
        try:
            await self.cancel_platform_task(platform_task_id, reason="platform_cancelled")
        except Exception:
            return

    async def _emit_client_task_notifications(self, client: Any, record, event_callback) -> Any:
        notifications = getattr(client, "last_stream_notifications", ())
        if not notifications:
            return record
        current = record
        for notification in notifications:
            if not isinstance(notification, Mapping):
                continue
            method = str(notification.get("method") or "")
            params = notification.get("params") if isinstance(notification.get("params"), Mapping) else {}
            if method == "notifications/progress":
                if params.get("progressToken") != current.progress_token:
                    continue
                progress = params.get("progress")
                if not isinstance(progress, (int, float)):
                    continue
                self._task_registry.record_progress(current.progress_token, progress)
                payload: dict[str, Any] = {"safe_ref": current.safe_ref, "progress": progress}
                total = params.get("total")
                if isinstance(total, (int, float)):
                    payload["total"] = total
                message = str(params.get("message") or "").strip()
                if message:
                    payload["message"] = message
                await _emit_mcp_event(event_callback, "mcp.long_task_progress", payload)
            elif method == "notifications/tasks/status":
                if str(params.get("taskId") or "") != current.mcp_task_id:
                    continue
                current = self._task_registry.update_status(current.mcp_task_id, params)
                await _emit_mcp_event(
                    event_callback,
                    "mcp.long_task_status",
                    {"safe_ref": current.safe_ref, "status": current.status, "status_message": current.status_message},
                )
        return current

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
        if server.transport == MCP_TRANSPORT_STREAMABLE_HTTP:
            transport = StreamableHTTPTransport(
                endpoint=server.endpoint,
                auth=server.auth,
                request_headers=server.request_headers,
            )
        elif server.transport == MCP_TRANSPORT_LEGACY_HTTP_SSE:
            transport = LegacyHTTPSSETransport(
                endpoint=server.endpoint,
                auth=server.auth,
                request_headers=server.request_headers,
            )
        else:
            raise MCPClientError(
                f"MCP transport {server.transport} is not implemented by the Python default client factory.",
                code="mcp_transport_unsupported",
                retriable=False,
                metadata={"server_id": server.server_id, "transport": server.transport},
            )
        client = MCPClient(
            server_id=server.server_id,
            transport=transport,
            protocol_version=server.protocol_version or MCP_PROTOCOL_VERSION,
            pinned_protocol_version=server.protocol_version_pinned,
            transport_family=server.transport,
            timeout_seconds=server.limits.timeout_seconds or default_timeout_seconds,
            client_capabilities={},
        )
        return PythonLegacyMCPClientAdapter(client)

    return _factory


def _diagnostic(
    server: MCPServerConfig,
    tool: MCPToolConfig,
    reason: str,
    message: str,
    capability_id: str,
) -> MCPRuntimeDiagnostic:
    return MCPRuntimeDiagnostic(
        server_id=server.server_id,
        tool_name=tool.tool_name,
        capability_id=capability_id,
        reason=reason,
        message=message,
        requested_protocol_version=server.protocol_version,
        transport_family=server.transport,
        transport_security=server.transport_security,
        header_names=server.request_header_names,
        required=server.required,
    )


def _mcp_task_failure_event(*, safe_ref: str, error_code: str) -> dict[str, Any]:
    return {
        "event_type": "mcp.long_task_failed",
        "payload": {"safe_ref": safe_ref, "error_code": error_code, "retriable": False},
    }


def _mcp_task_cancelled_event(*, safe_ref: str, status: str) -> dict[str, Any]:
    return {
        "event_type": "mcp.long_task_cancelled",
        "payload": {"safe_ref": safe_ref, "status": status},
    }


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
        [
            *(descriptor.capability_id for descriptor in descriptors),
            *(f"{cap}:{binding.server_id}/{binding.tool_name}" for cap, binding in bindings.items()),
            *(f"{diag.server_id}:{diag.tool_name}:{diag.reason}" for diag in diagnostics),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _server_capabilities(client: Any) -> Mapping[str, Any]:
    value = getattr(client, "server_capabilities", {})
    return dict(value) if isinstance(value, Mapping) else {}


def _sidecar_runtime_operations_available(client: Any) -> bool:
    required = ("list_tools", "call_tool", "cancel_platform_task")
    return all(callable(getattr(client, name, None)) for name in required)


def _callable_accepts_keyword(target: Any, keyword: str) -> bool:
    if not callable(target):
        return False
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return False
    return keyword in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _server_supports_task_augmented_tools_call(capabilities: Mapping[str, Any]) -> bool:
    tasks = capabilities.get("tasks") if isinstance(capabilities.get("tasks"), Mapping) else {}
    requests = tasks.get("requests") if isinstance(tasks.get("requests"), Mapping) else {}
    if "tools.call" in requests:
        return True
    tools = requests.get("tools") if isinstance(requests.get("tools"), Mapping) else {}
    return "call" in tools


def _tool_task_support(discovered_tool: Mapping[str, Any]) -> str:
    execution = discovered_tool.get("execution") if isinstance(discovered_tool.get("execution"), Mapping) else {}
    support = str(execution.get("taskSupport") or "forbidden").strip().lower()
    return support if support in {"required", "optional", "forbidden"} else "forbidden"


def _task_augmented_decision(*, server_capabilities: Mapping[str, Any], task_support: str, mode: str) -> str:
    normalized_mode = mode if mode in {"required", "preferred", "disabled"} else "disabled"
    server_support = _server_supports_task_augmented_tools_call(server_capabilities)
    if not server_support:
        return "fail_closed" if normalized_mode == "required" else "plain_call"
    if task_support == "required":
        return "fail_closed" if normalized_mode == "disabled" else "task_augmented"
    if task_support == "optional":
        if normalized_mode == "required":
            return "task_augmented"
        if normalized_mode == "preferred":
            return "task_augmented_preferred"
        return "plain_call"
    return "fail_closed" if normalized_mode == "required" else "plain_call"


def _task_diagnostic_reason(server_capabilities: Mapping[str, Any], task_support: str, mode: str) -> str:
    if not _server_supports_task_augmented_tools_call(server_capabilities):
        return "task_required_unavailable"
    if task_support == "forbidden":
        return "task_support_forbidden"
    if task_support == "required" and mode == "disabled":
        return "task_required_disabled"
    return "task_required_unavailable"

def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


async def _emit_mcp_event(event_callback, event_type: str, payload: Mapping[str, Any]) -> None:
    if event_callback is None:
        return
    await event_callback(event_type, payload)
