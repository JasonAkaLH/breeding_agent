from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import stat
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence, cast

from jsonschema import (
    Draft202012Validator,
    Draft7Validator,
    SchemaError,
    ValidationError,
)

from src.capabilities.mcp_dispatch.models import (
    MCPBindingMode,
    MCPSelectorActionType,
    MCPServerRouteActionType,
    MCPToolProfile,
    build_mcp_selector_context,
)
from src.core.contracts import UserMCPConfigurationStoragePort
from src.orchestration.models import UserMCPServerProfile

from .legacy_migration import (
    deterministic_migrated_server_id,
    legacy_migration_credential_provenance_digest,
    legacy_migration_source_fingerprint,
)
from .endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyProvenance,
    ValidatedEndpoint,
)


SHADOW_MANIFEST_SCHEMA_VERSION = "user_mcp_shadow_manifest.v1"
SHADOW_MANIFEST_ATTESTATION_DOMAIN = "user-mcp-shadow-manifest-v1"
SHADOW_MANIFEST_MAX_BYTES = 64 * 1024
APPROVED_VERIFIED_RETIRE_BLOCKER = "approved_verified_retire"
_SHADOW_GATEWAY_TIMEOUT_CODES = frozenset({"discovery_timeout", "mcp_timeout"})
_SHADOW_GATEWAY_AUTHENTICATION_CODES = frozenset(
    {"mcp_auth_required", "mcp_scope_required"}
)
_SHADOW_GATEWAY_POLICY_CODES = frozenset({"endpoint_policy_rejected"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_ID_RE = re.compile(r"^mcp\.[a-z0-9][a-z0-9_.-]{0,250}$")
_SIGNED_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "config_fingerprint",
        "fixture_fingerprint",
        "mapping_fingerprint",
        "expectations",
        "attestation_key_id",
        "attestation_signature",
    }
)
_EXPECTATION_FIELDS = frozenset(
    {
        "scenario",
        "legacy_outcome",
        "shadow_outcome",
        "transport",
        "endpoint_policy",
        "timeout_checkpoint",
        "expected_policy_delta",
    }
)
_MIGRATION_PROVENANCE_FIELDS = frozenset(
    {
        "schema",
        "source_server_id",
        "source_fingerprint",
        "owner_user_id",
        "target_server_id",
        "credential_digest",
        "credential_security_version",
        "validator_provenance",
        "observed_at",
        "expires_at",
    }
)


class ShadowScenario(StrEnum):
    HTTPS_STREAMABLE_SUCCESS = "https_streamable_success"
    HTTPS_LEGACY_SSE_SUCCESS = "https_legacy_sse_success"
    PUBLIC_HTTP_LEGACY_SSE_SUCCESS = "public_http_legacy_sse_success"
    ALLOWLISTED_HTTP_LEGACY_SSE_SUCCESS = "allowlisted_http_legacy_sse_success"
    AUTHENTICATION_FAILURE = "authentication_failure"
    TIMEOUT = "timeout"
    PERMISSION_DENIAL = "permission_denial"
    LARGE_OUTPUT = "large_output"


CURRENT_SHADOW_SCENARIOS = (
    ShadowScenario.HTTPS_STREAMABLE_SUCCESS,
    ShadowScenario.HTTPS_LEGACY_SSE_SUCCESS,
    ShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS,
    ShadowScenario.AUTHENTICATION_FAILURE,
    ShadowScenario.TIMEOUT,
    ShadowScenario.PERMISSION_DENIAL,
    ShadowScenario.LARGE_OUTPUT,
)


class ShadowOutcome(StrEnum):
    TOOL_CALL_SUCCEEDED = "tool_call_succeeded"
    TOOL_CALL_SUCCEEDED_LARGE_RESULT = "tool_call_succeeded_large_result"
    CONTROL_PLANE_READY = "control_plane_ready"
    AUTHENTICATION_FAILED = "authentication_failed"
    TIMEOUT = "timeout"
    PERMISSION_DENIED_SUPPRESSED = "permission_denied_suppressed"
    OBSERVER_FAILED = "observer_failed"
    CLEANUP_FAILED = "cleanup_failed"


class ShadowComparison(StrEnum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    NOT_COMPARABLE = "not_comparable"
    EXCLUDED = "excluded"


SHADOW_SCENARIO_EXPECTATIONS = MappingProxyType(
    {
        ShadowScenario.HTTPS_STREAMABLE_SUCCESS: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.CONTROL_PLANE_READY,
            "streamable_http",
            "runtime_enforced",
        ),
        ShadowScenario.HTTPS_LEGACY_SSE_SUCCESS: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.CONTROL_PLANE_READY,
            "legacy_http_sse",
            "runtime_enforced",
        ),
        ShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.CONTROL_PLANE_READY,
            "legacy_http_sse",
            "runtime_enforced",
        ),
        ShadowScenario.ALLOWLISTED_HTTP_LEGACY_SSE_SUCCESS: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.CONTROL_PLANE_READY,
            "legacy_http_sse",
            "allowed_by_enterprise_allowlist",
        ),
        ShadowScenario.AUTHENTICATION_FAILURE: (
            ShadowOutcome.AUTHENTICATION_FAILED,
            ShadowOutcome.AUTHENTICATION_FAILED,
            "streamable_http",
            "runtime_enforced",
        ),
        ShadowScenario.TIMEOUT: (
            ShadowOutcome.TIMEOUT,
            ShadowOutcome.TIMEOUT,
            "streamable_http",
            "runtime_enforced",
        ),
        ShadowScenario.PERMISSION_DENIAL: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
            "streamable_http",
            "runtime_enforced",
        ),
        ShadowScenario.LARGE_OUTPUT: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED_LARGE_RESULT,
            ShadowOutcome.CONTROL_PLANE_READY,
            "streamable_http",
            "runtime_enforced",
        ),
    }
)


class ShadowMappingDisposition(StrEnum):
    RETAIN = "retain"
    RETIRE = "retire"


class ShadowManifestError(ValueError):
    pass


class ShadowAuthenticationError(RuntimeError):
    """Normalized authentication failure raised by a shadow callback."""


class _ShadowEndpointPolicyError(RuntimeError):
    """Normalized endpoint-policy rejection raised by a shadow callback."""


class ShadowObserverGapError(RuntimeError):
    """An observer infrastructure gap that must not enter mismatch metrics."""


@dataclass(slots=True, frozen=True)
class ShadowScenarioExpectation:
    scenario: ShadowScenario
    legacy_outcome: ShadowOutcome
    shadow_outcome: ShadowOutcome
    transport: str
    endpoint_policy: str
    timeout_checkpoint: str | None = None
    expected_policy_delta: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, ShadowScenario):
            raise ShadowManifestError(
                "shadow scenario must use the closed ShadowScenario enum"
            )
        if not isinstance(self.legacy_outcome, ShadowOutcome) or not isinstance(
            self.shadow_outcome, ShadowOutcome
        ):
            raise ShadowManifestError(
                "shadow outcomes must use the closed ShadowOutcome enum"
            )
        if not self.transport or not self.endpoint_policy:
            raise ShadowManifestError(
                "shadow transport and endpoint_policy must not be empty"
            )


@dataclass(slots=True, frozen=True)
class ShadowScenarioManifest:
    manifest_id: str
    config_fingerprint: str
    fixture_fingerprint: str
    mapping_fingerprint: str
    expectations: tuple[ShadowScenarioExpectation, ...]
    schema_version: str = SHADOW_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_shadow_manifest(self)

    @property
    def fingerprint(self) -> str:
        return shadow_manifest_fingerprint(self)

    def expectation_for(self, scenario: ShadowScenario) -> ShadowScenarioExpectation:
        return next(item for item in self.expectations if item.scenario is scenario)


@dataclass(slots=True, frozen=True)
class VerifiedShadowScenarioManifest:
    """A closed manifest whose external attestation and bindings were verified."""

    manifest: ShadowScenarioManifest
    attestation_key_id: str
    attestation_signature: str

    @property
    def fingerprint(self) -> str:
        return self.manifest.fingerprint


@dataclass(slots=True, frozen=True)
class ApprovedVerifiedMapping:
    legacy_route: str
    user_server_id: str | None
    source_fingerprint: str
    config_fingerprint: str
    approved: bool
    verified: bool
    disposition: ShadowMappingDisposition = ShadowMappingDisposition.RETAIN

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, ShadowMappingDisposition):
            raise ValueError("shadow mapping disposition must use the closed enum")
        if (
            not self.legacy_route
            or not self.source_fingerprint
            or not self.config_fingerprint
        ):
            raise ValueError(
                "shadow mapping identity and fingerprints must not be empty"
            )

    def validate_for(self, manifest: ShadowScenarioManifest) -> None:
        self.validate_route()
        if self.config_fingerprint != manifest.config_fingerprint:
            raise ValueError(
                "shadow mapping config fingerprint does not match manifest"
            )

    def validate_route(self) -> None:
        if self.approved is not True or self.verified is not True:
            raise ValueError("shadow mapping must be approved and verified")
        if (
            self.disposition is ShadowMappingDisposition.RETAIN
            and not self.user_server_id
        ):
            raise ValueError("retained shadow mapping requires user_server_id")
        if (
            self.disposition is ShadowMappingDisposition.RETIRE
            and self.user_server_id is not None
        ):
            raise ValueError("retired shadow mapping cannot target a user server")


@dataclass(slots=True, frozen=True)
class ShadowServerProfile:
    server_id: str
    transport: str
    endpoint_policy: str
    owner_matches: bool


@dataclass(slots=True, frozen=True)
class ShadowTool:
    name: str
    input_schema: Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class ShadowRouteDecision:
    server_id: str


@dataclass(slots=True, frozen=True)
class ShadowSelection:
    tool_name: str | None
    schema_valid: bool


@dataclass(slots=True, frozen=True)
class ShadowReadChecks:
    endpoint_policy_allowed: bool
    ownership_verified: bool
    grant_exists: bool


@dataclass(slots=True, frozen=True)
class ShadowCleanupCounts:
    clients: int = 0
    connections: int = 0
    temporary_files: int = 0

    @property
    def clean(self) -> bool:
        return self.clients == self.connections == self.temporary_files == 0


class ShadowDiscoverySession(Protocol):
    @property
    def cleanup_counts(self) -> ShadowCleanupCounts: ...

    async def aclose(self) -> None: ...


ShadowRouter = Callable[
    [tuple[ShadowServerProfile, ...]], Awaitable[ShadowRouteDecision]
]
ShadowDiscover = Callable[[ShadowRouteDecision], Awaitable[ShadowDiscoverySession]]
ShadowListTools = Callable[[ShadowDiscoverySession], Awaitable[Sequence[ShadowTool]]]
ShadowSelector = Callable[
    [ShadowRouteDecision, tuple[ShadowTool, ...]], Awaitable[ShadowSelection]
]
ShadowGrantReader = Callable[
    [ShadowRouteDecision, ShadowSelection], Awaitable[ShadowReadChecks]
]


@dataclass(slots=True, frozen=True)
class ShadowObserverCallbacks:
    router: ShadowRouter
    discover: ShadowDiscover
    list_tools: ShadowListTools
    selector: ShadowSelector
    read_grant: ShadowGrantReader


@dataclass(slots=True, frozen=True)
class ShadowSafeSummary:
    route: str | None
    transport: str | None
    endpoint_policy: str | None
    catalog_count: int
    catalog_names_hmac: str | None
    schema_fingerprints: tuple[str, ...]
    selected_tool_hmac: str | None
    schema_valid: bool | None
    endpoint_policy_allowed: bool | None
    ownership_verified: bool | None
    grant_exists: bool | None
    latency_buckets: Mapping[str, str]
    cleanup: ShadowCleanupCounts


@dataclass(slots=True, frozen=True)
class ShadowObservation:
    outcome: ShadowOutcome
    summary: ShadowSafeSummary
    error_code: str | None = None
    timeout_checkpoint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ShadowOutcome):
            raise ValueError("shadow observation outcome must use the closed enum")


@dataclass(slots=True, frozen=True)
class ShadowSample:
    nonce: str
    scenario: ShadowScenario
    legacy_outcome: ShadowOutcome
    observation: ShadowObservation
    legacy_summary: ShadowSafeSummary
    legacy_route: str
    mapping: ApprovedVerifiedMapping | None
    manifest_fingerprint: str
    config_fingerprint: str
    fixture_fingerprint: str
    mapping_set_fingerprint: str
    mapping_in_approved_set: bool
    terminal: bool = True
    in_window: bool = True
    audit_complete: bool = True
    digest_valid: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, ShadowScenario):
            raise ValueError("shadow sample scenario must use the closed enum")
        if not isinstance(self.legacy_outcome, ShadowOutcome):
            raise ValueError("legacy outcome must use the closed enum")


@dataclass(slots=True, frozen=True)
class ShadowComparisonResult:
    comparison: ShadowComparison
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.comparison, ShadowComparison):
            raise ValueError("shadow comparison must use the closed enum")


@dataclass(slots=True, frozen=True)
class LiveShadowSampleComparison:
    sample: ShadowSample
    result: ShadowComparisonResult

    @property
    def promotion_eligible(self) -> bool:
        return self.result.comparison is ShadowComparison.MATCHED


@dataclass(slots=True, frozen=True)
class RuntimeShadowMappingResolution:
    mapping: ApprovedVerifiedMapping | None
    blockers: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class RuntimeShadowComparisonResult:
    comparison: ShadowComparison
    blockers: tuple[str, ...]
    observation: ShadowObservation | None = None
    legacy_summary: ShadowSafeSummary | None = None
    mapping: ApprovedVerifiedMapping | None = None

    @property
    def promotion_eligible(self) -> bool:
        return self.comparison is ShadowComparison.MATCHED


@dataclass(slots=True, frozen=True)
class ShadowValidationResult:
    allowed: bool
    blockers: tuple[str, ...]
    comparisons: tuple[ShadowComparisonResult, ...]
    matched_by_scenario: Mapping[ShadowScenario, int]


class ShadowControlPlaneObserver:
    """Runs only side-effect-free MCP control-plane callbacks.

    The accepted callback surface deliberately has no tool execution, approval,
    interrupt, event, answer, or grant-mutation capability.
    """

    def __init__(
        self, *, digest_key: bytes, callbacks: ShadowObserverCallbacks
    ) -> None:
        if not digest_key:
            raise ValueError("digest_key must not be empty")
        self._digest_key = bytes(digest_key)
        self._callbacks = callbacks

    async def observe(
        self,
        *,
        profiles: tuple[ShadowServerProfile, ...],
    ) -> ShadowObservation:
        route: ShadowRouteDecision | None = None
        session: ShadowDiscoverySession | None = None
        tools: tuple[ShadowTool, ...] = ()
        selection: ShadowSelection | None = None
        checks: ShadowReadChecks | None = None
        cleanup = ShadowCleanupCounts()
        latencies: dict[str, str] = {}
        outcome = ShadowOutcome.CONTROL_PLANE_READY
        error_code: str | None = None
        timeout_checkpoint: str | None = None

        try:
            route = await self._timed(
                "router", latencies, self._callbacks.router(profiles)
            )
            session = await self._timed(
                "discover", latencies, self._callbacks.discover(route)
            )
            listed = await self._timed(
                "list", latencies, self._callbacks.list_tools(session)
            )
            tools = tuple(listed)
            selection = await self._timed(
                "selector", latencies, self._callbacks.selector(route, tools)
            )
            checks = await self._timed(
                "grant", latencies, self._callbacks.read_grant(route, selection)
            )
            if not checks.grant_exists:
                outcome = ShadowOutcome.PERMISSION_DENIED_SUPPRESSED
            elif (
                not selection.schema_valid
                or not checks.endpoint_policy_allowed
                or not checks.ownership_verified
            ):
                outcome = ShadowOutcome.OBSERVER_FAILED
                error_code = "shadow_control_check_failed"
        except ShadowAuthenticationError:
            outcome = ShadowOutcome.AUTHENTICATION_FAILED
            error_code = "shadow_authentication_failed"
        except _ShadowEndpointPolicyError:
            outcome = ShadowOutcome.PERMISSION_DENIED_SUPPRESSED
            error_code = "shadow_endpoint_policy_denied"
        except TimeoutError as exc:
            outcome = ShadowOutcome.TIMEOUT
            timeout_checkpoint = str(exc) or "unknown"
            error_code = "shadow_timeout"
        except Exception:
            outcome = ShadowOutcome.OBSERVER_FAILED
            error_code = "shadow_observer_failed"
        finally:
            if session is not None:
                try:
                    await session.aclose()
                    cleanup = session.cleanup_counts
                    if not cleanup.clean:
                        outcome = ShadowOutcome.CLEANUP_FAILED
                        error_code = "shadow_cleanup_incomplete"
                except Exception:
                    cleanup = session.cleanup_counts
                    if cleanup.clean:
                        cleanup = ShadowCleanupCounts(clients=1, connections=1)
                    outcome = ShadowOutcome.CLEANUP_FAILED
                    error_code = "shadow_cleanup_failed"

        return ShadowObservation(
            outcome=outcome,
            error_code=error_code,
            timeout_checkpoint=timeout_checkpoint,
            summary=ShadowSafeSummary(
                route=route.server_id if route is not None else None,
                transport=_profile_value(profiles, route, "transport"),
                endpoint_policy=_profile_value(profiles, route, "endpoint_policy"),
                catalog_count=len(tools),
                catalog_names_hmac=_hmac_values(
                    self._digest_key, (tool.name for tool in tools)
                )
                if tools
                else None,
                schema_fingerprints=tuple(
                    sorted(_schema_fingerprint(tool.input_schema) for tool in tools)
                ),
                selected_tool_hmac=(
                    _hmac_values(self._digest_key, (selection.tool_name,))
                    if selection is not None and selection.tool_name is not None
                    else None
                ),
                schema_valid=selection.schema_valid if selection is not None else None,
                endpoint_policy_allowed=checks.endpoint_policy_allowed
                if checks is not None
                else None,
                ownership_verified=checks.ownership_verified
                if checks is not None
                else None,
                grant_exists=checks.grant_exists if checks is not None else None,
                latency_buckets=MappingProxyType(dict(latencies)),
                cleanup=cleanup,
            ),
        )

    @staticmethod
    async def _timed(
        name: str, buckets: dict[str, str], awaitable: Awaitable[Any]
    ) -> Any:
        started = time.monotonic()
        try:
            return await awaitable
        finally:
            buckets[name] = _latency_bucket((time.monotonic() - started) * 1000)


@dataclass(slots=True, frozen=True)
class _ShadowPrincipal:
    username: str


class _RuntimeShadowSession:
    def __init__(self, session: Any) -> None:
        provenance = getattr(session, "endpoint_policy_provenance", None)
        if not isinstance(provenance, EndpointPolicyProvenance):
            raise ShadowObserverGapError(
                "shadow_endpoint_policy_provenance_invalid"
            )
        self._session = session
        self.scope = session.scope
        self.catalog = session.catalog
        self.endpoint_policy_provenance = provenance
        self.cleanup_counts = ShadowCleanupCounts(clients=1, connections=1)

    async def aclose(self) -> None:
        await self._session.aclose()
        self.cleanup_counts = ShadowCleanupCounts()


def _closed_shadow_gateway_error_code(exc: BaseException) -> str:
    for attribute in ("mcp_error_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str):
            return value
    return ""


class MCPShadowRuntimeObserver:
    """Runtime adapter for the zero-call control-plane observer.

    This surface intentionally exposes no ``call_tool`` operation. It can only
    route, open an isolated read-only shadow session, inspect its catalog,
    select, read a grant, and close the session.
    """

    def __init__(
        self,
        *,
        storage: UserMCPConfigurationStoragePort,
        gateway: Any,
        server_router: Any,
        selector: Any,
        endpoint_policy: EndpointPolicy,
        digest_key: bytes,
    ) -> None:
        if not digest_key:
            raise ValueError("shadow digest key must not be empty")
        self._storage = storage
        self._gateway = gateway
        self._server_router = server_router
        self._selector = selector
        self._endpoint_policy = endpoint_policy
        self._digest_key = bytes(digest_key)

    async def observe_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        user_request: str,
        profiles: tuple[UserMCPServerProfile, ...],
    ) -> ShadowObservation:
        profiles_by_id = {profile.server_id: profile for profile in profiles}
        endpoint_policies = await self._validated_profile_endpoint_policies(
            owner_user_id=owner_user_id,
            profiles=profiles,
        )
        sessions: dict[str, _RuntimeShadowSession] = {}

        async def route(
            safe_profiles: tuple[ShadowServerProfile, ...],
        ) -> ShadowRouteDecision:
            remaining = tuple(
                profiles_by_id[profile.server_id]
                for profile in safe_profiles
                if profile.server_id in profiles_by_id
            )
            action = await self._server_router.route(
                user_request=user_request,
                remaining_servers=remaining,
            )
            if (
                action.action is not MCPServerRouteActionType.ROUTE_SERVER
                or not action.server_id
            ):
                raise RuntimeError("shadow_router_stopped")
            return ShadowRouteDecision(action.server_id)

        async def discover(decision: ShadowRouteDecision) -> _RuntimeShadowSession:
            try:
                readonly_session = await self._gateway.open_readonly_shadow_session(
                    _ShadowPrincipal(owner_user_id),
                    task_id,
                    decision.server_id,
                )
            except Exception as exc:
                gateway_code = _closed_shadow_gateway_error_code(exc)
                if gateway_code in _SHADOW_GATEWAY_TIMEOUT_CODES:
                    raise TimeoutError("discover") from exc
                if gateway_code in _SHADOW_GATEWAY_AUTHENTICATION_CODES:
                    raise ShadowAuthenticationError(
                        "shadow_authentication_failed"
                    ) from exc
                if gateway_code in _SHADOW_GATEWAY_POLICY_CODES:
                    raise _ShadowEndpointPolicyError(
                        "shadow_endpoint_policy_denied"
                    ) from exc
                raise
            try:
                session = _RuntimeShadowSession(readonly_session)
            except Exception:
                await readonly_session.aclose()
                raise
            sessions[decision.server_id] = session
            return session

        async def list_tools(session: ShadowDiscoverySession) -> tuple[ShadowTool, ...]:
            runtime_session = cast(_RuntimeShadowSession, session)
            return tuple(
                ShadowTool(tool.name, tool.input_schema)
                for tool in runtime_session.catalog.tools
            )

        async def select(
            decision: ShadowRouteDecision,
            tools: tuple[ShadowTool, ...],
        ) -> ShadowSelection:
            profile = profiles_by_id[decision.server_id]
            action = await self._selector.select(
                build_mcp_selector_context(
                    user_request=user_request,
                    server=profile,
                    tools=tuple(
                        MCPToolProfile(
                            name=tool.name,
                            input_schema=tool.input_schema,
                        )
                        for tool in tools
                    ),
                    binding_mode=MCPBindingMode.AUTOMATIC,
                )
            )
            if action.action is not MCPSelectorActionType.CALL_TOOL:
                return ShadowSelection(None, schema_valid=True)
            selected_tool = next(
                (tool for tool in tools if tool.name == action.tool_name),
                None,
            )
            return ShadowSelection(
                action.tool_name,
                schema_valid=bool(
                    selected_tool is not None
                    and _shadow_arguments_match_schema(
                        selected_tool.input_schema,
                        action.arguments,
                    )
                ),
            )

        async def read_grant(
            decision: ShadowRouteDecision,
            selection: ShadowSelection,
        ) -> ShadowReadChecks:
            profile = profiles_by_id[decision.server_id]
            session = sessions[decision.server_id]
            if (
                session.endpoint_policy_provenance
                is not endpoint_policies[decision.server_id]
            ):
                raise ShadowObserverGapError(
                    "shadow_endpoint_policy_provenance_changed"
                )
            if selection.tool_name is None:
                grant_exists = True
            else:
                descriptor = session.catalog.get(selection.tool_name)
                grant_exists = bool(
                    descriptor is not None
                    and await self._storage.get_valid_user_mcp_tool_grant(
                        owner_user_id,
                        decision.server_id,
                        selection.tool_name,
                        server_security_version=session.scope.security_version,
                        input_schema_sha256=descriptor.input_schema_sha256,
                    )
                    is not None
                )
            return ShadowReadChecks(
                endpoint_policy_allowed=True,
                ownership_verified=profile.server_id == session.scope.server_id
                and session.scope.owner_user_id == owner_user_id,
                grant_exists=grant_exists,
            )

        observer = ShadowControlPlaneObserver(
            digest_key=self._digest_key,
            callbacks=ShadowObserverCallbacks(
                router=route,
                discover=discover,
                list_tools=list_tools,
                selector=select,
                read_grant=read_grant,
            ),
        )
        return await observer.observe(
            profiles=tuple(
                ShadowServerProfile(
                    server_id=profile.server_id,
                    transport=profile.transport,
                    endpoint_policy=endpoint_policies[profile.server_id].value,
                    owner_matches=True,
                )
                for profile in profiles
            )
        )

    async def compare_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        user_request: str,
        profiles: tuple[UserMCPServerProfile, ...],
        legacy_binding: Any,
        legacy_server_bindings: Sequence[Any],
        legacy_transport: str,
        legacy_endpoint_url: str | None = None,
        mapping: ApprovedVerifiedMapping | None,
        config_fingerprint: str,
        mapping_blockers: tuple[str, ...] = (),
    ) -> RuntimeShadowComparisonResult:
        """Compare one selected legacy route without exposing a tool-call surface."""

        legacy_route = str(getattr(legacy_binding, "server_id", "") or "").strip()
        mapping_error = _runtime_mapping_error(
            mapping,
            legacy_route=legacy_route,
            config_fingerprint=config_fingerprint,
        )
        if mapping_error is not None:
            blockers = (
                tuple(dict.fromkeys(mapping_blockers))
                if mapping is None and mapping_blockers
                else tuple(dict.fromkeys((*mapping_blockers, mapping_error)))
            )
            return RuntimeShadowComparisonResult(
                comparison=(
                    ShadowComparison.NOT_COMPARABLE
                    if mapping is None and set(blockers) == {"verified_mapping_missing"}
                    else ShadowComparison.MISMATCHED
                ),
                blockers=blockers,
                legacy_summary=self.build_legacy_safe_summary(
                    selected_binding=legacy_binding,
                    server_bindings=legacy_server_bindings,
                    transport=legacy_transport,
                    endpoint_policy_provenance=None,
                ),
                mapping=mapping,
            )
        assert mapping is not None
        if mapping.disposition is ShadowMappingDisposition.RETIRE:
            return RuntimeShadowComparisonResult(
                comparison=ShadowComparison.NOT_COMPARABLE,
                blockers=(APPROVED_VERIFIED_RETIRE_BLOCKER,),
                legacy_summary=self.build_legacy_safe_summary(
                    selected_binding=legacy_binding,
                    server_bindings=legacy_server_bindings,
                    transport=legacy_transport,
                    endpoint_policy_provenance=None,
                ),
                mapping=mapping,
            )

        legacy_policy_provenance = await self._validated_endpoint_provenance(
            legacy_endpoint_url
        )
        legacy_summary = self.build_legacy_safe_summary(
            selected_binding=legacy_binding,
            server_bindings=legacy_server_bindings,
            transport=legacy_transport,
            endpoint_policy_provenance=legacy_policy_provenance,
        )

        observation = await self.observe_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            user_request=user_request,
            profiles=profiles,
        )
        if (
            observation.outcome is ShadowOutcome.OBSERVER_FAILED
            and observation.error_code == "shadow_observer_failed"
        ):
            raise ShadowObserverGapError("shadow_observer_failed")
        compared = compare_runtime_shadow_observation(
            legacy_summary=legacy_summary,
            observation=observation,
            mapping=mapping,
            config_fingerprint=config_fingerprint,
        )
        return RuntimeShadowComparisonResult(
            comparison=compared.comparison,
            blockers=compared.blockers,
            observation=observation,
            legacy_summary=legacy_summary,
            mapping=mapping,
        )

    def build_legacy_safe_summary(
        self,
        *,
        selected_binding: Any,
        server_bindings: Sequence[Any],
        transport: str,
        endpoint_policy_provenance: EndpointPolicyProvenance | None,
    ) -> ShadowSafeSummary:
        bindings = tuple(server_bindings)
        tool_names = tuple(
            str(getattr(binding, "tool_name", "") or "") for binding in bindings
        )
        schemas = tuple(
            dict(getattr(binding, "input_schema", {}) or {}) for binding in bindings
        )
        selected_tool = str(getattr(selected_binding, "tool_name", "") or "").strip()
        return ShadowSafeSummary(
            route=str(getattr(selected_binding, "server_id", "") or "").strip(),
            transport=str(transport or "").strip() or None,
            endpoint_policy=(
                endpoint_policy_provenance.value
                if endpoint_policy_provenance is not None
                else None
            ),
            catalog_count=len(bindings),
            catalog_names_hmac=(
                _hmac_values(self._digest_key, tool_names) if tool_names else None
            ),
            schema_fingerprints=tuple(
                sorted(_schema_fingerprint(schema) for schema in schemas)
            ),
            selected_tool_hmac=(
                _hmac_values(self._digest_key, (selected_tool,))
                if selected_tool
                else None
            ),
            schema_valid=True,
            endpoint_policy_allowed=endpoint_policy_provenance is not None,
            ownership_verified=True,
            grant_exists=None,
            latency_buckets=MappingProxyType({}),
            cleanup=ShadowCleanupCounts(),
        )

    async def _validated_profile_endpoint_policies(
        self,
        *,
        owner_user_id: str,
        profiles: tuple[UserMCPServerProfile, ...],
    ) -> dict[str, EndpointPolicyProvenance]:
        policies: dict[str, EndpointPolicyProvenance] = {}
        for profile in profiles:
            try:
                server = await self._storage.get_user_mcp_server(
                    owner_user_id,
                    profile.server_id,
                )
            except Exception as exc:
                raise ShadowObserverGapError(
                    "shadow_endpoint_policy_provenance_unavailable"
                ) from exc
            if (
                server is None
                or str(getattr(server, "owner_user_id", "")) != owner_user_id
                or str(getattr(server, "server_id", "")) != profile.server_id
            ):
                raise ShadowObserverGapError(
                    "shadow_endpoint_policy_provenance_unavailable"
                )
            policies[profile.server_id] = await self._validated_endpoint_provenance(
                str(getattr(server, "endpoint_url", "") or "")
            )
        return policies

    async def _validated_endpoint_provenance(
        self,
        endpoint_url: str | None,
    ) -> EndpointPolicyProvenance:
        if not isinstance(endpoint_url, str) or not endpoint_url.strip():
            raise ShadowObserverGapError(
                "shadow_endpoint_policy_provenance_unavailable"
            )
        try:
            endpoint = await asyncio.to_thread(
                self._endpoint_policy.validate,
                endpoint_url,
            )
        except Exception as exc:
            raise ShadowObserverGapError(
                "shadow_endpoint_policy_provenance_unavailable"
            ) from exc
        if not isinstance(endpoint, ValidatedEndpoint) or not isinstance(
            endpoint.policy_provenance,
            EndpointPolicyProvenance,
        ):
            raise ShadowObserverGapError(
                "shadow_endpoint_policy_provenance_invalid"
            )
        return endpoint.policy_provenance


def resolve_approved_migration_mapping(
    *,
    legacy_server_id: str,
    owner_user_id: str,
    legacy_server: Any,
    user_servers: Sequence[Any],
    target_credential_digests: Mapping[str, str],
    config_fingerprint: str,
) -> RuntimeShadowMappingResolution:
    """Resolve only the approval-gated CP-4 DB ledger; never infer a mapping."""

    source_id = str(legacy_server_id or "").strip()
    owner = str(owner_user_id or "").strip()
    config = str(config_fingerprint or "").strip()
    if not source_id or not owner or not config:
        return RuntimeShadowMappingResolution(
            None,
            ("verified_mapping_input_invalid",),
        )

    expected_source_fingerprint = legacy_migration_source_fingerprint(legacy_server)
    expected_target_id = deterministic_migrated_server_id(source_id, owner)
    expected_credential_digest = target_credential_digests.get(expected_target_id)
    matching: list[ApprovedVerifiedMapping] = []
    invalid_candidate = False
    for server in user_servers:
        metadata = getattr(server, "auth_metadata", {})
        provenance = (
            metadata.get("migration_provenance")
            if isinstance(metadata, Mapping)
            else None
        )
        if not isinstance(provenance, Mapping):
            continue
        if str(provenance.get("source_server_id") or "").strip() != source_id:
            continue
        candidate_valid = (
            set(provenance) == _MIGRATION_PROVENANCE_FIELDS
            and provenance.get("schema") == "legacy_mcp_migration_provenance.v1"
            and str(provenance.get("owner_user_id") or "").strip() == owner
            and str(getattr(server, "owner_user_id", "") or "").strip() == owner
            and str(getattr(server, "server_id", "") or "").strip()
            == expected_target_id
            and str(provenance.get("target_server_id") or "").strip()
            == expected_target_id
            and str(provenance.get("source_fingerprint") or "").strip()
            == expected_source_fingerprint
            and int(getattr(server, "config_version", 0) or 0) == 1
            and provenance.get("credential_security_version") == 1
            and int(getattr(server, "security_version", 0) or 0)
            == provenance.get("credential_security_version")
            and _target_matches_cp4_source(server, legacy_server)
            and expected_credential_digest is not None
            and hmac.compare_digest(
                str(provenance.get("credential_digest") or ""),
                expected_credential_digest,
            )
            and bool(str(provenance.get("validator_provenance") or "").strip())
            and _valid_migration_validation_window(server, provenance)
        )
        if not candidate_valid:
            invalid_candidate = True
            continue
        matching.append(
            ApprovedVerifiedMapping(
                legacy_route=source_id,
                user_server_id=expected_target_id,
                source_fingerprint=expected_source_fingerprint,
                config_fingerprint=config,
                approved=True,
                verified=True,
            )
        )

    if len(matching) == 1 and not invalid_candidate:
        return RuntimeShadowMappingResolution(matching[0])
    if len(matching) > 1:
        return RuntimeShadowMappingResolution(
            None,
            ("verified_mapping_ambiguous",),
        )
    if invalid_candidate:
        return RuntimeShadowMappingResolution(
            None,
            ("verified_mapping_invalid",),
        )
    return RuntimeShadowMappingResolution(None, ("verified_mapping_missing",))


def compare_runtime_shadow_observation(
    *,
    legacy_summary: ShadowSafeSummary,
    observation: ShadowObservation,
    mapping: ApprovedVerifiedMapping | None,
    config_fingerprint: str,
) -> ShadowComparisonResult:
    mapping_error = _runtime_mapping_error(
        mapping,
        legacy_route=str(legacy_summary.route or ""),
        config_fingerprint=config_fingerprint,
    )
    if mapping_error is not None:
        return ShadowComparisonResult(ShadowComparison.MISMATCHED, (mapping_error,))
    assert mapping is not None
    if mapping.disposition is ShadowMappingDisposition.RETIRE:
        return ShadowComparisonResult(
            ShadowComparison.NOT_COMPARABLE,
            (APPROVED_VERIFIED_RETIRE_BLOCKER,),
        )

    blockers: list[str] = []
    if observation.summary.route != mapping.user_server_id:
        blockers.append("shadow_route_mapping_mismatch")
    if observation.outcome is not ShadowOutcome.CONTROL_PLANE_READY:
        blockers.append("shadow_outcome_not_ready")
    if not observation.summary.cleanup.clean:
        blockers.append("cleanup_incomplete")
    for field_name in (
        "transport",
        "endpoint_policy",
        "catalog_count",
        "catalog_names_hmac",
        "schema_fingerprints",
        "selected_tool_hmac",
        "schema_valid",
        "endpoint_policy_allowed",
        "ownership_verified",
    ):
        if getattr(legacy_summary, field_name) != getattr(
            observation.summary,
            field_name,
        ):
            blockers.append(f"{field_name}_mismatch")
    return ShadowComparisonResult(
        ShadowComparison.MATCHED if not blockers else ShadowComparison.MISMATCHED,
        tuple(blockers),
    )


def derive_shadow_catalog_digest_key(
    audit_reference_signer: Any,
    *,
    config_fingerprint: str,
) -> bytes:
    """Derive a secret, config-bound key without exposing credential key bytes."""

    fingerprint = str(config_fingerprint or "").strip()
    if audit_reference_signer is None or not fingerprint:
        raise ValueError("shadow catalog digest key inputs must not be empty")
    encoded = audit_reference_signer.safe_reference(
        fingerprint,
        context="mcp-shadow-catalog-hmac-v1",
    )
    try:
        digest_key = bytes.fromhex(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("shadow catalog digest key derivation failed") from exc
    if len(digest_key) != hashlib.sha256().digest_size:
        raise ValueError("shadow catalog digest key derivation failed")
    return digest_key


def migration_target_credential_digest(
    credential_cipher: Any,
    audit_reference_signer: Any,
    *,
    server: Any,
    credential_record: Any | None,
    source_fingerprint: str,
) -> str | None:
    """Recompute CP-4's keyed credential binding for the current target row."""

    owner = str(getattr(server, "owner_user_id", "") or "").strip()
    server_id = str(getattr(server, "server_id", "") or "").strip()
    source = str(source_fingerprint or "").strip()
    if (
        credential_cipher is None
        or audit_reference_signer is None
        or not owner
        or not server_id
        or not source
    ):
        return None
    try:
        if credential_record is None:
            if bool(getattr(server, "credential_configured", False)):
                return None
            credential_values = None
        else:
            if (
                not bool(getattr(server, "credential_configured", False))
                or getattr(credential_record, "owner_user_id", None) != owner
                or getattr(credential_record, "server_id", None) != server_id
            ):
                return None
            values = credential_cipher.decrypt(
                credential_record,
                owner_user_id=owner,
                server_id=server_id,
                auth_type=str(getattr(server, "auth_type", "")),
            )
            credential_values = values
        provenance = getattr(server, "auth_metadata", {}).get("migration_provenance")
        if not isinstance(provenance, Mapping):
            return None
        return legacy_migration_credential_provenance_digest(
            audit_reference_signer,
            credential_values=credential_values,
            owner_user_id=owner,
            target_server_id=server_id,
            source_fingerprint=source,
            provenance=provenance,
        )
    except Exception:
        return None


def _runtime_mapping_error(
    mapping: ApprovedVerifiedMapping | None,
    *,
    legacy_route: str,
    config_fingerprint: str,
) -> str | None:
    if mapping is None:
        return "verified_mapping_missing"
    if mapping.approved is not True or mapping.verified is not True:
        return "verified_mapping_invalid"
    if mapping.legacy_route != legacy_route:
        return "legacy_route_mapping_mismatch"
    if mapping.config_fingerprint != config_fingerprint:
        return "mapping_config_fingerprint_mismatch"
    if (
        mapping.disposition is ShadowMappingDisposition.RETAIN
        and not mapping.user_server_id
    ):
        return "verified_mapping_invalid"
    if (
        mapping.disposition is ShadowMappingDisposition.RETIRE
        and mapping.user_server_id is not None
    ):
        return "verified_mapping_invalid"
    return None


def _shadow_arguments_match_schema(
    schema: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> bool:
    schema_uri = str(schema.get("$schema") or "").lower()
    validator = (
        Draft7Validator
        if "draft-07" in schema_uri or "draft7" in schema_uri
        else Draft202012Validator
    )
    try:
        validator.check_schema(dict(schema))
        validator(dict(schema)).validate(dict(arguments))
    except (SchemaError, ValidationError, TypeError, ValueError):
        return False
    return True


def _valid_migration_validation_window(
    server: Any,
    provenance: Mapping[str, Any],
) -> bool:
    try:
        observed_at = datetime.fromisoformat(str(provenance["observed_at"]))
        expires_at = datetime.fromisoformat(str(provenance["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    last_tested_at = getattr(server, "last_tested_at", None)
    if not isinstance(last_tested_at, datetime):
        return False
    try:
        return observed_at <= last_tested_at <= expires_at
    except TypeError:
        return False


def _target_matches_cp4_source(server: Any, legacy_server: Any) -> bool:
    source_headers = getattr(legacy_server, "request_headers", {})
    source_auth = getattr(legacy_server, "auth", None)
    source_auth_type = str(getattr(source_auth, "type", "none") or "none")
    expected_auth_metadata: dict[str, Any]
    if source_headers:
        expected_auth_type = "static_headers"
        expected_auth_metadata = {
            "header_names": sorted(
                {str(name).strip().lower() for name in source_headers}
            )
        }
        credential_configured = True
    elif source_auth_type == "none":
        expected_auth_type = "none"
        expected_auth_metadata = {}
        credential_configured = False
    elif source_auth_type == "bearer_env":
        expected_auth_type = "bearer"
        expected_auth_metadata = {}
        credential_configured = True
    elif source_auth_type == "api_key_env":
        expected_auth_type = "api_key_header"
        expected_auth_metadata = {
            "header_name": str(getattr(source_auth, "header_name", "") or "")
            .strip()
            .lower()
        }
        credential_configured = True
    else:
        return False
    target_metadata = {
        str(key): value
        for key, value in dict(getattr(server, "auth_metadata", {}) or {}).items()
        if key != "migration_provenance"
    }
    expected_protocol = (
        str(getattr(legacy_server, "protocol_version", "") or "")
        if bool(getattr(legacy_server, "protocol_version_pinned", False))
        else "auto"
    )
    return bool(
        str(getattr(server, "endpoint_url", "") or "").strip()
        == str(getattr(legacy_server, "endpoint", "") or "").strip()
        and str(getattr(server, "transport", "") or "")
        == str(getattr(legacy_server, "transport", "") or "")
        and str(getattr(server, "protocol_preference", "") or "") == expected_protocol
        and str(getattr(server, "auth_type", "") or "") == expected_auth_type
        and target_metadata == expected_auth_metadata
        and bool(getattr(server, "credential_configured", False))
        is credential_configured
        and bool(getattr(server, "enabled", False))
        is bool(getattr(legacy_server, "enabled", False))
        and not bool(getattr(server, "deletion_pending", False))
        and str(getattr(server, "health_status", "") or "") == "available"
    )


def validate_shadow_manifest(manifest: ShadowScenarioManifest) -> None:
    if manifest.schema_version != SHADOW_MANIFEST_SCHEMA_VERSION:
        raise ShadowManifestError("unsupported shadow manifest schema_version")
    for name, value in (
        ("manifest_id", manifest.manifest_id),
        ("config_fingerprint", manifest.config_fingerprint),
        ("fixture_fingerprint", manifest.fixture_fingerprint),
        ("mapping_fingerprint", manifest.mapping_fingerprint),
    ):
        if not str(value).strip():
            raise ShadowManifestError(f"shadow manifest {name} must not be empty")
    by_scenario = {item.scenario: item for item in manifest.expectations}
    if len(by_scenario) != len(manifest.expectations):
        raise ShadowManifestError("shadow manifest contains duplicate scenarios")
    if set(by_scenario) != set(CURRENT_SHADOW_SCENARIOS):
        raise ShadowManifestError(
            "shadow manifest must define every closed scenario exactly once"
        )

    for scenario in CURRENT_SHADOW_SCENARIOS:
        expected = SHADOW_SCENARIO_EXPECTATIONS[scenario]
        item = by_scenario[scenario]
        if (
            item.legacy_outcome,
            item.shadow_outcome,
            item.transport,
            item.endpoint_policy,
        ) != expected:
            raise ShadowManifestError(
                f"shadow manifest expectation is invalid for {scenario.value}"
            )
        if scenario is ShadowScenario.TIMEOUT and not item.timeout_checkpoint:
            raise ShadowManifestError(
                "timeout scenario requires a fixed timeout_checkpoint"
            )
        if scenario is ShadowScenario.PERMISSION_DENIAL:
            if item.expected_policy_delta != "legacy_success_shadow_permission_denial":
                raise ShadowManifestError(
                    "permission denial requires the closed expected policy delta"
                )
        elif item.expected_policy_delta is not None:
            raise ShadowManifestError(
                "expected policy delta is only valid for permission_denial"
            )


def load_signed_shadow_manifest(
    document: bytes | str | Mapping[str, Any],
    *,
    trusted_attestation_keys: Mapping[str, bytes],
    expected_config_fingerprint: str,
    expected_fixture_fingerprint: str,
    expected_mapping_fingerprint: str,
) -> VerifiedShadowScenarioManifest:
    """Load one signed, closed scenario manifest and bind it to startup inputs."""

    raw = _load_shadow_manifest_document(document)
    _require_exact_fields(raw, _SIGNED_MANIFEST_FIELDS, "shadow manifest")
    key_id = _manifest_identifier(raw["attestation_key_id"], "attestation_key_id")
    signature = raw["attestation_signature"]
    if not isinstance(signature, str) or _SHA256_RE.fullmatch(signature) is None:
        raise ShadowManifestError("shadow manifest attestation_signature is invalid")
    key = trusted_attestation_keys.get(key_id)
    if not isinstance(key, bytes) or len(key) < hashlib.sha256().digest_size:
        raise ShadowManifestError("shadow manifest attestation key is not trusted")

    manifest = _parse_shadow_manifest(raw)
    bindings = (
        (
            "config_fingerprint",
            manifest.config_fingerprint,
            expected_config_fingerprint,
        ),
        (
            "fixture_fingerprint",
            manifest.fixture_fingerprint,
            expected_fixture_fingerprint,
        ),
        (
            "mapping_fingerprint",
            manifest.mapping_fingerprint,
            expected_mapping_fingerprint,
        ),
    )
    for name, actual, expected in bindings:
        if not isinstance(expected, str) or not expected.strip() or actual != expected:
            raise ShadowManifestError(f"shadow manifest {name} binding mismatch")

    expected_signature = canonical_shadow_manifest_attestation_signature(
        manifest,
        key_id=key_id,
        key=key,
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise ShadowManifestError("shadow manifest attestation is invalid")
    return VerifiedShadowScenarioManifest(
        manifest=manifest,
        attestation_key_id=key_id,
        attestation_signature=signature,
    )


def shadow_fixture_bindings_fingerprint(
    bindings: Mapping[str, ShadowScenario | str],
) -> str:
    """Hash the exact closed capability-to-scenario fixture document."""

    if not isinstance(bindings, Mapping) or not bindings:
        raise ShadowManifestError("shadow fixture bindings must be a nonempty object")
    normalized: list[dict[str, str]] = []
    for capability_id, raw_scenario in bindings.items():
        if (
            not isinstance(capability_id, str)
            or _CAPABILITY_ID_RE.fullmatch(capability_id) is None
        ):
            raise ShadowManifestError("shadow fixture capability_id is invalid")
        try:
            scenario = ShadowScenario(raw_scenario)
        except (TypeError, ValueError):
            raise ShadowManifestError("shadow fixture scenario is invalid") from None
        normalized.append({"capability_id": capability_id, "scenario": scenario.value})
    payload = {
        "schema": "user_mcp_shadow_fixture_bindings.v1",
        "bindings": sorted(normalized, key=lambda item: item["capability_id"]),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def approved_shadow_mapping_set_fingerprint(
    mappings: Iterable[ApprovedVerifiedMapping],
) -> str:
    """Hash a complete approved mapping set without secret or descriptive fields."""

    normalized: list[dict[str, str | None]] = []
    seen_routes: set[str] = set()
    for mapping in mappings:
        if not isinstance(mapping, ApprovedVerifiedMapping):
            raise ValueError(
                "approved shadow mapping set must contain ApprovedVerifiedMapping values"
            )
        mapping.validate_route()
        if mapping.legacy_route in seen_routes:
            raise ValueError(
                "approved shadow mapping set contains duplicate legacy routes"
            )
        seen_routes.add(mapping.legacy_route)
        normalized.append(
            {
                "legacy_route": mapping.legacy_route,
                "source_fingerprint": mapping.source_fingerprint,
                "user_server_id": mapping.user_server_id,
                "disposition": mapping.disposition.value,
                "config_fingerprint": mapping.config_fingerprint,
            }
        )
    payload = {
        "schema": "user_mcp_shadow_approved_mapping_set.v1",
        "mappings": sorted(normalized, key=lambda item: str(item["legacy_route"])),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def load_signed_shadow_manifest_file(
    path: str | Path,
    *,
    trusted_attestation_keys: Mapping[str, bytes],
    expected_config_fingerprint: str,
    expected_fixture_fingerprint: str,
    expected_mapping_fingerprint: str,
) -> VerifiedShadowScenarioManifest:
    """Filesystem startup seam for a signed scenario manifest."""

    resolved = Path(path)
    try:
        metadata = resolved.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ShadowManifestError("shadow manifest path must not be a symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise ShadowManifestError("shadow manifest path must be a regular file")
        if metadata.st_size > SHADOW_MANIFEST_MAX_BYTES:
            raise ShadowManifestError("shadow manifest exceeds the size limit")
        document = resolved.read_bytes()
    except ShadowManifestError:
        raise
    except OSError as exc:
        raise ShadowManifestError("shadow manifest file is unavailable") from exc
    return load_signed_shadow_manifest(
        document,
        trusted_attestation_keys=trusted_attestation_keys,
        expected_config_fingerprint=expected_config_fingerprint,
        expected_fixture_fingerprint=expected_fixture_fingerprint,
        expected_mapping_fingerprint=expected_mapping_fingerprint,
    )


def canonical_shadow_manifest_attestation_signature(
    manifest: ShadowScenarioManifest,
    *,
    key_id: str,
    key: bytes,
) -> str:
    """Return the canonical HMAC used by the external CP-7 manifest producer."""

    if not isinstance(manifest, ShadowScenarioManifest):
        raise TypeError("manifest must be ShadowScenarioManifest")
    normalized_key_id = _manifest_identifier(key_id, "attestation_key_id")
    if not isinstance(key, bytes) or len(key) < hashlib.sha256().digest_size:
        raise ValueError("shadow manifest attestation key must be at least 32 bytes")
    identity = {
        "domain": SHADOW_MANIFEST_ATTESTATION_DOMAIN,
        "attestation_key_id": normalized_key_id,
        "manifest": _shadow_manifest_payload(manifest),
    }
    return hmac.new(
        key,
        _canonical_json(identity).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def shadow_manifest_fingerprint(manifest: ShadowScenarioManifest) -> str:
    return hashlib.sha256(
        _canonical_json(_shadow_manifest_payload(manifest)).encode("utf-8")
    ).hexdigest()


def _shadow_manifest_payload(manifest: ShadowScenarioManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "manifest_id": manifest.manifest_id,
        "config_fingerprint": manifest.config_fingerprint,
        "fixture_fingerprint": manifest.fixture_fingerprint,
        "mapping_fingerprint": manifest.mapping_fingerprint,
        "expectations": [
            {
                "scenario": item.scenario.value,
                "legacy_outcome": item.legacy_outcome.value,
                "shadow_outcome": item.shadow_outcome.value,
                "transport": item.transport,
                "endpoint_policy": item.endpoint_policy,
                "timeout_checkpoint": item.timeout_checkpoint,
                "expected_policy_delta": item.expected_policy_delta,
            }
            for item in sorted(
                manifest.expectations, key=lambda value: value.scenario.value
            )
        ],
    }


def compare_live_shadow_sample(
    *,
    verified_manifest: VerifiedShadowScenarioManifest,
    scenario: ShadowScenario,
    nonce: str,
    legacy_outcome: ShadowOutcome,
    observation: ShadowObservation,
    legacy_summary: ShadowSafeSummary,
    legacy_route: str,
    mapping: ApprovedVerifiedMapping | None,
    approved_mappings: Sequence[ApprovedVerifiedMapping],
    terminal: bool = True,
    in_window: bool = True,
    audit_complete: bool = True,
    digest_valid: bool = True,
) -> LiveShadowSampleComparison:
    """Build and compare a live sample using only a verified manifest binding."""

    if not isinstance(verified_manifest, VerifiedShadowScenarioManifest):
        raise TypeError("verified_manifest must be VerifiedShadowScenarioManifest")
    manifest = verified_manifest.manifest
    approved_mapping_set = tuple(approved_mappings)
    sample = ShadowSample(
        nonce=nonce,
        scenario=scenario,
        legacy_outcome=legacy_outcome,
        observation=observation,
        legacy_summary=legacy_summary,
        legacy_route=legacy_route,
        mapping=mapping,
        manifest_fingerprint=manifest.fingerprint,
        config_fingerprint=manifest.config_fingerprint,
        fixture_fingerprint=manifest.fixture_fingerprint,
        mapping_set_fingerprint=approved_shadow_mapping_set_fingerprint(
            approved_mapping_set
        ),
        mapping_in_approved_set=(mapping is None or mapping in approved_mapping_set),
        terminal=terminal,
        in_window=in_window,
        audit_complete=audit_complete,
        digest_valid=digest_valid,
    )
    return LiveShadowSampleComparison(
        sample=sample,
        result=compare_shadow_sample(sample, manifest),
    )


def compare_shadow_sample(
    sample: ShadowSample, manifest: ShadowScenarioManifest
) -> ShadowComparisonResult:
    exclusion_reasons: list[str] = []
    if not sample.terminal:
        exclusion_reasons.append("sample_not_terminal")
    if not sample.in_window:
        exclusion_reasons.append("sample_outside_window")
    if exclusion_reasons:
        return ShadowComparisonResult(
            ShadowComparison.EXCLUDED, tuple(exclusion_reasons)
        )

    blockers: list[str] = []
    if not sample.nonce:
        blockers.append("sample_nonce_missing")
    if sample.manifest_fingerprint != manifest.fingerprint:
        blockers.append("manifest_fingerprint_mismatch")
    if sample.config_fingerprint != manifest.config_fingerprint:
        blockers.append("config_fingerprint_mismatch")
    if sample.fixture_fingerprint != manifest.fixture_fingerprint:
        blockers.append("fixture_fingerprint_mismatch")
    if sample.mapping_set_fingerprint != manifest.mapping_fingerprint:
        blockers.append("mapping_set_fingerprint_mismatch")
    if not sample.mapping_in_approved_set:
        blockers.append("verified_mapping_not_in_approved_set")
    if not sample.audit_complete:
        blockers.append("audit_incomplete")
    if not sample.digest_valid:
        blockers.append("digest_invalid")

    mapping = sample.mapping
    if mapping is None:
        if blockers:
            blockers.append("verified_mapping_missing")
            return ShadowComparisonResult(
                ShadowComparison.MISMATCHED,
                tuple(blockers),
            )
        blockers.append("verified_mapping_missing")
        return ShadowComparisonResult(
            ShadowComparison.NOT_COMPARABLE,
            tuple(blockers),
        )
    else:
        try:
            mapping.validate_for(manifest)
        except ValueError:
            blockers.append("verified_mapping_invalid")
        else:
            if mapping.legacy_route != sample.legacy_route:
                blockers.append("legacy_route_mapping_mismatch")
            if mapping.disposition is ShadowMappingDisposition.RETIRE:
                if blockers:
                    return ShadowComparisonResult(
                        ShadowComparison.MISMATCHED, tuple(blockers)
                    )
                return ShadowComparisonResult(
                    ShadowComparison.NOT_COMPARABLE,
                    (APPROVED_VERIFIED_RETIRE_BLOCKER,),
                )
            if sample.observation.summary.route != mapping.user_server_id:
                blockers.append("shadow_route_mapping_mismatch")

    expectation = manifest.expectation_for(sample.scenario)
    if sample.legacy_outcome is not expectation.legacy_outcome:
        blockers.append("legacy_outcome_mismatch")
    if sample.observation.outcome is not expectation.shadow_outcome:
        blockers.append("shadow_outcome_mismatch")
    if sample.observation.summary.transport != expectation.transport:
        blockers.append("transport_mismatch")
    if sample.observation.summary.endpoint_policy != expectation.endpoint_policy:
        blockers.append("endpoint_policy_mismatch")
    if (
        sample.scenario is ShadowScenario.TIMEOUT
        and sample.observation.timeout_checkpoint != expectation.timeout_checkpoint
    ):
        blockers.append("timeout_checkpoint_mismatch")
    if not sample.observation.summary.cleanup.clean:
        blockers.append("cleanup_incomplete")
    if not sample.legacy_summary.cleanup.clean:
        blockers.append("legacy_cleanup_incomplete")
    for field_name in (
        "catalog_count",
        "catalog_names_hmac",
        "schema_fingerprints",
        "selected_tool_hmac",
        "schema_valid",
        "endpoint_policy_allowed",
        "ownership_verified",
    ):
        if getattr(sample.legacy_summary, field_name) != getattr(
            sample.observation.summary, field_name
        ):
            blockers.append(f"{field_name}_mismatch")
    if sample.scenario is not ShadowScenario.PERMISSION_DENIAL:
        legacy_grant = sample.legacy_summary.grant_exists
        shadow_grant = sample.observation.summary.grant_exists
        if (
            legacy_grant is not None
            and shadow_grant is not None
            and legacy_grant != shadow_grant
        ):
            blockers.append("grant_check_mismatch")

    comparison = (
        ShadowComparison.MATCHED if not blockers else ShadowComparison.MISMATCHED
    )
    return ShadowComparisonResult(comparison, tuple(blockers))


def validate_shadow_samples(
    samples: Sequence[ShadowSample],
    manifest: ShadowScenarioManifest,
    *,
    minimum_matches_per_scenario: int = 3,
) -> ShadowValidationResult:
    if minimum_matches_per_scenario < 1:
        raise ValueError("minimum_matches_per_scenario must be positive")
    comparisons: list[ShadowComparisonResult] = []
    blockers: list[str] = []
    matched: Counter[ShadowScenario] = Counter()
    seen_nonces: set[str] = set()

    for index, sample in enumerate(samples):
        result = compare_shadow_sample(sample, manifest)
        sample_blockers = list(result.blockers)
        if sample.terminal and sample.in_window:
            if sample.nonce in seen_nonces:
                sample_blockers.append("duplicate_nonce")
                result = ShadowComparisonResult(
                    ShadowComparison.MISMATCHED, tuple(sample_blockers)
                )
            seen_nonces.add(sample.nonce)
        comparisons.append(result)
        if result.comparison is ShadowComparison.MATCHED:
            matched[sample.scenario] += 1
        elif result.comparison is ShadowComparison.MISMATCHED:
            blockers.extend(f"sample_{index}:{reason}" for reason in result.blockers)
        elif result.comparison is ShadowComparison.NOT_COMPARABLE:
            # Approved retire is valid evidence but never satisfies a scenario quota.
            if (
                sample.mapping is None
                or sample.mapping.disposition is not ShadowMappingDisposition.RETIRE
            ):
                blockers.append(f"sample_{index}:not_comparable_without_retire")

    for scenario in CURRENT_SHADOW_SCENARIOS:
        if matched[scenario] < minimum_matches_per_scenario:
            blockers.append(f"scenario_samples_below_threshold:{scenario.value}")

    return ShadowValidationResult(
        allowed=not blockers,
        blockers=tuple(blockers),
        comparisons=tuple(comparisons),
        matched_by_scenario=MappingProxyType(dict(matched)),
    )


def _load_shadow_manifest_document(
    document: bytes | str | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(document, Mapping):
        return dict(document)
    if isinstance(document, str):
        encoded = document.encode("utf-8")
    elif isinstance(document, bytes):
        encoded = document
    else:
        raise TypeError("shadow manifest document must be bytes, str, or Mapping")
    if not encoded or len(encoded) > SHADOW_MANIFEST_MAX_BYTES:
        raise ShadowManifestError("shadow manifest document size is invalid")
    try:
        decoded = encoded.decode("utf-8")
        raw = json.loads(decoded, object_pairs_hook=_unique_shadow_manifest_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowManifestError("shadow manifest is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ShadowManifestError("shadow manifest must be a JSON object")
    return raw


def _unique_shadow_manifest_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ShadowManifestError("shadow manifest contains a duplicate JSON key")
        result[key] = value
    return result


def _parse_shadow_manifest(raw: Mapping[str, Any]) -> ShadowScenarioManifest:
    expectations = raw["expectations"]
    if not isinstance(expectations, list):
        raise ShadowManifestError("shadow manifest expectations must be an array")
    parsed: list[ShadowScenarioExpectation] = []
    for value in expectations:
        if not isinstance(value, Mapping):
            raise ShadowManifestError("shadow manifest expectation must be an object")
        item = dict(value)
        _require_exact_fields(item, _EXPECTATION_FIELDS, "shadow manifest expectation")
        try:
            scenario = ShadowScenario(item["scenario"])
            legacy_outcome = ShadowOutcome(item["legacy_outcome"])
            shadow_outcome = ShadowOutcome(item["shadow_outcome"])
        except (TypeError, ValueError):
            raise ShadowManifestError(
                "shadow manifest contains an unknown closed enum"
            ) from None
        timeout_checkpoint = _optional_manifest_identifier(
            item["timeout_checkpoint"],
            "timeout_checkpoint",
        )
        expected_policy_delta = _optional_manifest_identifier(
            item["expected_policy_delta"],
            "expected_policy_delta",
        )
        parsed.append(
            ShadowScenarioExpectation(
                scenario=scenario,
                legacy_outcome=legacy_outcome,
                shadow_outcome=shadow_outcome,
                transport=_manifest_identifier(item["transport"], "transport"),
                endpoint_policy=_manifest_identifier(
                    item["endpoint_policy"],
                    "endpoint_policy",
                ),
                timeout_checkpoint=timeout_checkpoint,
                expected_policy_delta=expected_policy_delta,
            )
        )
    return ShadowScenarioManifest(
        manifest_id=_manifest_identifier(raw["manifest_id"], "manifest_id"),
        config_fingerprint=_manifest_identifier(
            raw["config_fingerprint"],
            "config_fingerprint",
        ),
        fixture_fingerprint=_manifest_identifier(
            raw["fixture_fingerprint"],
            "fixture_fingerprint",
        ),
        mapping_fingerprint=_manifest_identifier(
            raw["mapping_fingerprint"],
            "mapping_fingerprint",
        ),
        expectations=tuple(parsed),
        schema_version=_manifest_identifier(raw["schema_version"], "schema_version"),
    )


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ShadowManifestError(f"{label} fields do not match the closed schema")


def _manifest_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ShadowManifestError(f"shadow manifest {name} is invalid")
    return value


def _optional_manifest_identifier(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _manifest_identifier(value, name)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _schema_fingerprint(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(_canonical_schema_value(schema)).encode("utf-8")
    ).hexdigest()


def _canonical_schema_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_schema_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_schema_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _hmac_values(key: bytes, values: Iterable[str]) -> str:
    material = _canonical_json(sorted(str(value) for value in values))
    return hmac.new(key, material.encode("utf-8"), hashlib.sha256).hexdigest()


def _latency_bucket(milliseconds: float) -> str:
    if milliseconds < 10:
        return "lt_10ms"
    if milliseconds < 50:
        return "10_49ms"
    if milliseconds < 250:
        return "50_249ms"
    if milliseconds < 1000:
        return "250_999ms"
    return "gte_1000ms"


def _profile_value(
    profiles: tuple[ShadowServerProfile, ...],
    route: ShadowRouteDecision | None,
    field_name: str,
) -> str | None:
    if route is None:
        return None
    profile = next(
        (item for item in profiles if item.server_id == route.server_id), None
    )
    return str(getattr(profile, field_name)) if profile is not None else None
