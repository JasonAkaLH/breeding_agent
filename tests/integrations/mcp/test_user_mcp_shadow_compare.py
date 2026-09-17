from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.capabilities.mcp_dispatch.models import (
    MCPSelectorAction,
    MCPSelectorActionType,
    MCPServerRouteAction,
    MCPServerRouteActionType,
)
from src.integrations.mcp.gateway_models import MCPToolDescriptor, ToolCatalogSnapshot
from src.integrations.mcp.config import MCPRuntimeConfig, MCPServerConfig
from src.integrations.mcp.gateway import MCPGatewayError
from src.integrations.mcp.endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyProvenance,
)
from src.integrations.mcp.legacy_migration import (
    LegacyConsumerScope,
    LegacyDisposition,
    LegacyServerClassification,
    plan_legacy_mcp_config_migration,
)
from src.integrations.mcp.runtime_state import MCPToolBinding
from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import UserMCPServer
from tests.master_key_support import audit_reference_signer, credential_cipher

from src.integrations.mcp.shadow_compare import (
    ApprovedVerifiedMapping,
    CURRENT_SHADOW_SCENARIOS,
    ShadowAuthenticationError,
    ShadowCleanupCounts,
    ShadowComparison,
    ShadowControlPlaneObserver,
    ShadowManifestError,
    ShadowMappingDisposition,
    MCPShadowRuntimeObserver,
    ShadowObservation,
    ShadowObserverGapError,
    ShadowObserverCallbacks,
    ShadowOutcome,
    ShadowReadChecks,
    ShadowRouteDecision,
    ShadowSafeSummary,
    ShadowSample,
    ShadowScenario,
    ShadowScenarioExpectation,
    ShadowScenarioManifest,
    ShadowSelection,
    ShadowServerProfile,
    ShadowTool,
    approved_shadow_mapping_set_fingerprint,
    canonical_shadow_manifest_attestation_signature,
    compare_live_shadow_sample,
    compare_shadow_sample,
    derive_shadow_catalog_digest_key,
    deterministic_migrated_server_id,
    legacy_migration_source_fingerprint,
    load_signed_shadow_manifest,
    load_signed_shadow_manifest_file,
    migration_target_credential_digest,
    resolve_approved_migration_mapping,
    shadow_fixture_bindings_fingerprint,
    validate_shadow_samples,
)
from src.orchestration.models import UserMCPServerProfile


_TRANSPORTS = {
    ShadowScenario.HTTPS_STREAMABLE_SUCCESS: "streamable_http",
    ShadowScenario.HTTPS_LEGACY_SSE_SUCCESS: "legacy_http_sse",
    ShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS: "legacy_http_sse",
    ShadowScenario.AUTHENTICATION_FAILURE: "streamable_http",
    ShadowScenario.TIMEOUT: "streamable_http",
    ShadowScenario.PERMISSION_DENIAL: "streamable_http",
    ShadowScenario.LARGE_OUTPUT: "streamable_http",
}

_POLICIES = {scenario: "runtime_enforced" for scenario in CURRENT_SHADOW_SCENARIOS}

_FIXTURE_BINDINGS = {
    "mcp.legacy.search": ShadowScenario.HTTPS_STREAMABLE_SUCCESS,
}


def _manifest() -> ShadowScenarioManifest:
    outcomes = {
        ShadowScenario.HTTPS_STREAMABLE_SUCCESS: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.CONTROL_PLANE_READY,
        ),
        ShadowScenario.HTTPS_LEGACY_SSE_SUCCESS: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.CONTROL_PLANE_READY,
        ),
        ShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.CONTROL_PLANE_READY,
        ),
        ShadowScenario.AUTHENTICATION_FAILURE: (
            ShadowOutcome.AUTHENTICATION_FAILED,
            ShadowOutcome.AUTHENTICATION_FAILED,
        ),
        ShadowScenario.TIMEOUT: (ShadowOutcome.TIMEOUT, ShadowOutcome.TIMEOUT),
        ShadowScenario.PERMISSION_DENIAL: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
        ),
        ShadowScenario.LARGE_OUTPUT: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED_LARGE_RESULT,
            ShadowOutcome.CONTROL_PLANE_READY,
        ),
    }
    return ShadowScenarioManifest(
        manifest_id="window-a",
        config_fingerprint="config-v7",
        fixture_fingerprint=shadow_fixture_bindings_fingerprint(_FIXTURE_BINDINGS),
        mapping_fingerprint=approved_shadow_mapping_set_fingerprint((_mapping(),)),
        expectations=tuple(
            ShadowScenarioExpectation(
                scenario=scenario,
                legacy_outcome=outcomes[scenario][0],
                shadow_outcome=outcomes[scenario][1],
                transport=_TRANSPORTS[scenario],
                endpoint_policy=_POLICIES[scenario],
                timeout_checkpoint="list"
                if scenario is ShadowScenario.TIMEOUT
                else None,
                expected_policy_delta=(
                    "legacy_success_shadow_permission_denial"
                    if scenario is ShadowScenario.PERMISSION_DENIAL
                    else None
                ),
            )
            for scenario in CURRENT_SHADOW_SCENARIOS
        ),
    )


def _summary(
    scenario: ShadowScenario, *, grant_exists: bool = True
) -> ShadowSafeSummary:
    return ShadowSafeSummary(
        route="user-server",
        transport=_TRANSPORTS[scenario],
        endpoint_policy=_POLICIES[scenario],
        catalog_count=1,
        catalog_names_hmac="catalog-hmac",
        schema_fingerprints=("schema-fingerprint",),
        selected_tool_hmac="tool-hmac",
        schema_valid=True,
        endpoint_policy_allowed=True,
        ownership_verified=True,
        grant_exists=grant_exists,
        latency_buckets={},
        cleanup=ShadowCleanupCounts(),
    )


def _mapping(
    *, disposition: ShadowMappingDisposition = ShadowMappingDisposition.RETAIN
) -> ApprovedVerifiedMapping:
    return ApprovedVerifiedMapping(
        legacy_route="legacy-server",
        user_server_id="user-server"
        if disposition is ShadowMappingDisposition.RETAIN
        else None,
        source_fingerprint="mapping-v4",
        config_fingerprint="config-v7",
        approved=True,
        verified=True,
        disposition=disposition,
    )


def _sample(scenario: ShadowScenario, *, nonce: str | None = None) -> ShadowSample:
    manifest = _manifest()
    expectation = manifest.expectation_for(scenario)
    grant_exists = scenario is not ShadowScenario.PERMISSION_DENIAL
    summary = _summary(scenario, grant_exists=grant_exists)
    return ShadowSample(
        nonce=nonce or f"nonce-{scenario.value}",
        scenario=scenario,
        legacy_outcome=expectation.legacy_outcome,
        observation=ShadowObservation(
            outcome=expectation.shadow_outcome,
            summary=summary,
            timeout_checkpoint=expectation.timeout_checkpoint,
        ),
        legacy_summary=replace(summary, grant_exists=True),
        legacy_route="legacy-server",
        mapping=_mapping(),
        manifest_fingerprint=manifest.fingerprint,
        config_fingerprint=manifest.config_fingerprint,
        fixture_fingerprint=manifest.fixture_fingerprint,
        mapping_set_fingerprint=manifest.mapping_fingerprint,
        mapping_in_approved_set=True,
    )


def _signed_manifest_document(
    manifest: ShadowScenarioManifest,
    *,
    key_id: str,
    key: bytes,
) -> dict[str, object]:
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
            for item in manifest.expectations
        ],
        "attestation_key_id": key_id,
        "attestation_signature": canonical_shadow_manifest_attestation_signature(
            manifest,
            key_id=key_id,
            key=key,
        ),
    }


class _Session:
    def __init__(self, owner: "_FakeControlPlane") -> None:
        self._owner = owner
        self.cleanup_counts = ShadowCleanupCounts(
            clients=1, connections=1, temporary_files=1
        )

    async def aclose(self) -> None:
        self._owner.close_count += 1
        if self._owner.cleanup_error:
            raise RuntimeError("cleanup fixture detail must be suppressed")
        self.cleanup_counts = ShadowCleanupCounts()


class _FakeControlPlane:
    """Hostile fake intentionally has no call_tool or mutation API."""

    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.cleanup_error = failure == "cleanup"
        self.close_count = 0
        self.grant_writes = 0
        self.interrupt_writes = 0
        self.event_writes = 0
        self.answer_writes = 0

    async def router(self, profiles):
        if self.failure == "router":
            raise RuntimeError("router fixture detail")
        return ShadowRouteDecision(profiles[0].server_id)

    async def discover(self, route):
        if self.failure == "auth":
            raise ShadowAuthenticationError("raw provider error")
        return _Session(self)

    async def list_tools(self, session):
        if self.failure == "timeout":
            raise TimeoutError("list")
        return (
            ShadowTool(
                "secret.tool.name", {"type": "object", "secret": "schema-value"}
            ),
        )

    async def selector(self, route, tools):
        if self.failure == "selector":
            raise RuntimeError("selector raw detail")
        return ShadowSelection(tools[0].name, schema_valid=True)

    async def read_grant(self, route, selection):
        return ShadowReadChecks(
            endpoint_policy_allowed=True,
            ownership_verified=True,
            grant_exists=self.failure != "denial",
        )

    def callbacks(self) -> ShadowObserverCallbacks:
        return ShadowObserverCallbacks(
            router=self.router,
            discover=self.discover,
            list_tools=self.list_tools,
            selector=self.selector,
            read_grant=self.read_grant,
        )


class _RuntimeShadowGateway:
    def __init__(
        self,
        discovery_error: Exception | None = None,
        *,
        endpoint_policy_provenance: object = (
            EndpointPolicyProvenance.RUNTIME_ENFORCED
        ),
        cleanup_error: bool = False,
    ) -> None:
        self.call_tool_count = 0
        self.close_count = 0
        self.discovery_error = discovery_error
        self.endpoint_policy_provenance = endpoint_policy_provenance
        self.cleanup_error = cleanup_error
        self.catalog = ToolCatalogSnapshot(
            server_id="server-1",
            effective_protocol_version="2025-06-18",
            tools=(
                MCPToolDescriptor(
                    name="search",
                    description="Search",
                    input_schema={"type": "object"},
                    input_schema_sha256="schema-sha",
                ),
            ),
        )

    async def open_readonly_shadow_session(self, principal, task_id, server_id):
        if self.discovery_error is not None:
            raise self.discovery_error
        gateway = self

        class _Session:
            scope = SimpleNamespace(
                owner_user_id=principal.username,
                platform_task_id=task_id,
                server_id=server_id,
                security_version=1,
            )
            catalog = gateway.catalog
            endpoint_policy_provenance = gateway.endpoint_policy_provenance

            async def aclose(self):
                gateway.close_count += 1
                if gateway.cleanup_error:
                    raise RuntimeError("readonly shadow cleanup failed")

        return _Session()

    async def open_scope(self, *args, **kwargs):
        raise AssertionError("shadow must not open a mutable gateway scope")

    async def list_tools(self, *args, **kwargs):
        raise AssertionError("readonly session owns its immutable catalog")

    async def close_scope(self, *args, **kwargs):
        raise AssertionError("shadow must not close a mutable gateway scope")

    async def call_tool(self, *args, **kwargs):
        self.call_tool_count += 1
        raise AssertionError("shadow must not call tools/call")


class _RuntimeShadowRouter:
    async def route(self, **kwargs):
        return MCPServerRouteAction(
            MCPServerRouteActionType.ROUTE_SERVER,
            server_id="server-1",
        )


class _RuntimeShadowSelector:
    async def select(self, context):
        return MCPSelectorAction(
            MCPSelectorActionType.CALL_TOOL,
            tool_name="search",
            arguments={"q": "ignored"},
        )


class _RuntimeShadowStorage:
    def __init__(
        self,
        *,
        granted: bool,
        endpoint_url: str = "https://mcp.example.test/rpc",
    ) -> None:
        self.granted = granted
        self.grant_reads = 0
        self.endpoint_url = endpoint_url

    async def get_valid_user_mcp_tool_grant(self, *args, **kwargs):
        self.grant_reads += 1
        return object() if self.granted else None

    async def get_user_mcp_server(self, owner_user_id: str, server_id: str):
        return UserMCPServer(
            server_id=server_id,
            owner_user_id=owner_user_id,
            display_name="Server",
            routing_description="Search",
            endpoint_url=self.endpoint_url,
            transport=UserMCPTransport.STREAMABLE_HTTP,
            health_status=UserMCPHealthStatus.AVAILABLE,
        )


class _RuntimeEndpointResolver:
    def resolve(self, hostname: str, port: int):
        del hostname, port
        return ("8.8.8.8",)


def _runtime_endpoint_policy() -> EndpointPolicy:
    return EndpointPolicy(resolver=_RuntimeEndpointResolver())


class UserMCPShadowCompareTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_adapter_maps_closed_gateway_discovery_errors_without_call(
        self,
    ) -> None:
        cases = (
            (
                "discovery_timeout",
                ShadowOutcome.TIMEOUT,
                "shadow_timeout",
                "discover",
            ),
            (
                "mcp_auth_required",
                ShadowOutcome.AUTHENTICATION_FAILED,
                "shadow_authentication_failed",
                None,
            ),
            (
                "endpoint_policy_rejected",
                ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
                "shadow_endpoint_policy_denied",
                None,
            ),
        )
        profiles = (
            UserMCPServerProfile(
                server_id="server-1",
                display_name="Server",
                routing_description="Search",
                transport="streamable_http",
            ),
        )

        for code, outcome, error_code, timeout_checkpoint in cases:
            with self.subTest(code=code):
                gateway = _RuntimeShadowGateway(MCPGatewayError(code))
                observer = MCPShadowRuntimeObserver(
                    storage=_RuntimeShadowStorage(granted=True),
                    gateway=gateway,
                    server_router=_RuntimeShadowRouter(),
                    selector=_RuntimeShadowSelector(),
                    endpoint_policy=_runtime_endpoint_policy(),
                    digest_key=b"digest-key",
                )

                result = await observer.observe_task(
                    owner_user_id="owner-1",
                    task_id="task-1",
                    user_request="search",
                    profiles=profiles,
                )

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.timeout_checkpoint, timeout_checkpoint)
                self.assertEqual(gateway.call_tool_count, 0)
                self.assertEqual(gateway.close_count, 0)

    async def test_runtime_adapter_keeps_unknown_gateway_error_as_observer_gap(
        self,
    ) -> None:
        gateway = _RuntimeShadowGateway(MCPGatewayError("discovery_timeout_extra"))
        observer = MCPShadowRuntimeObserver(
            storage=_RuntimeShadowStorage(granted=True),
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )

        result = await observer.observe_task(
            owner_user_id="owner-1",
            task_id="task-1",
            user_request="search",
            profiles=(
                UserMCPServerProfile(
                    server_id="server-1",
                    display_name="Server",
                    routing_description="Search",
                    transport="streamable_http",
                ),
            ),
        )

        self.assertEqual(result.outcome, ShadowOutcome.OBSERVER_FAILED)
        self.assertEqual(result.error_code, "shadow_observer_failed")
        self.assertEqual(gateway.call_tool_count, 0)
        self.assertEqual(gateway.close_count, 0)

    async def test_runtime_adapter_is_zero_call_and_closes_scope(self) -> None:
        gateway = _RuntimeShadowGateway()
        storage = _RuntimeShadowStorage(granted=True)
        observer = MCPShadowRuntimeObserver(
            storage=storage,
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )

        result = await observer.observe_task(
            owner_user_id="owner-1",
            task_id="task-1",
            user_request="search",
            profiles=(
                UserMCPServerProfile(
                    server_id="server-1",
                    display_name="Server",
                    routing_description="Search",
                    transport="streamable_http",
                ),
            ),
        )

        self.assertEqual(result.outcome, ShadowOutcome.CONTROL_PLANE_READY)
        self.assertEqual(storage.grant_reads, 1)
        self.assertEqual(gateway.call_tool_count, 0)
        self.assertEqual(gateway.close_count, 1)

    async def test_runtime_adapter_uses_runtime_enforced_public_http_provenance(self) -> None:
        gateway = _RuntimeShadowGateway(
            endpoint_policy_provenance=EndpointPolicyProvenance.RUNTIME_ENFORCED
        )
        observer = MCPShadowRuntimeObserver(
            storage=_RuntimeShadowStorage(
                granted=True,
                endpoint_url="http://mcp.example.test/rpc",
            ),
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )

        result = await observer.observe_task(
            owner_user_id="owner-1",
            task_id="task-public-http",
            user_request="search",
            profiles=(
                UserMCPServerProfile(
                    server_id="server-1",
                    display_name="Server",
                    routing_description="Search",
                    transport="legacy_http_sse",
                ),
            ),
        )

        self.assertEqual(result.outcome, ShadowOutcome.CONTROL_PLANE_READY)
        self.assertEqual(
            result.summary.endpoint_policy,
            "runtime_enforced",
        )
        self.assertEqual(gateway.call_tool_count, 0)
        self.assertEqual(gateway.close_count, 1)

    async def test_runtime_adapter_rejects_missing_or_tampered_provenance(
        self,
    ) -> None:
        for provenance in (
            None,
            "runtime_enforced",
        ):
            with self.subTest(provenance=provenance):
                gateway = _RuntimeShadowGateway(
                    endpoint_policy_provenance=provenance
                )
                observer = MCPShadowRuntimeObserver(
                    storage=_RuntimeShadowStorage(granted=True),
                    gateway=gateway,
                    server_router=_RuntimeShadowRouter(),
                    selector=_RuntimeShadowSelector(),
                    endpoint_policy=_runtime_endpoint_policy(),
                    digest_key=b"digest-key",
                )

                result = await observer.observe_task(
                    owner_user_id="owner-1",
                    task_id="task-policy-provenance",
                    user_request="search",
                    profiles=(
                        UserMCPServerProfile(
                            server_id="server-1",
                            display_name="Server",
                            routing_description="Search",
                            transport="streamable_http",
                        ),
                    ),
                )

                self.assertEqual(result.outcome, ShadowOutcome.OBSERVER_FAILED)
                self.assertEqual(result.error_code, "shadow_observer_failed")
                self.assertEqual(gateway.call_tool_count, 0)
                self.assertEqual(gateway.close_count, 1)

    async def test_runtime_cleanup_failure_cannot_produce_matched_evidence(
        self,
    ) -> None:
        gateway = _RuntimeShadowGateway(cleanup_error=True)
        observer = MCPShadowRuntimeObserver(
            storage=_RuntimeShadowStorage(granted=True),
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )
        binding = MCPToolBinding(
            capability_id="mcp.legacy.search",
            server_id="legacy-server",
            tool_name="search",
            input_schema={"type": "object"},
        )

        result = await observer.compare_task(
            owner_user_id="owner-1",
            task_id="task-cleanup-failure",
            user_request="search",
            profiles=(
                UserMCPServerProfile(
                    server_id="server-1",
                    display_name="Server",
                    routing_description="Search",
                    transport="streamable_http",
                ),
            ),
            legacy_binding=binding,
            legacy_server_bindings=(binding,),
            legacy_transport="streamable_http",
            legacy_endpoint_url="https://legacy.example.test/mcp",
            mapping=ApprovedVerifiedMapping(
                legacy_route="legacy-server",
                user_server_id="server-1",
                source_fingerprint="sha256:" + "a" * 64,
                config_fingerprint="config-v1",
                approved=True,
                verified=True,
            ),
            config_fingerprint="config-v1",
        )

        self.assertEqual(result.comparison, ShadowComparison.MISMATCHED)
        self.assertFalse(result.promotion_eligible)
        self.assertIsNotNone(result.observation)
        self.assertEqual(result.observation.outcome, ShadowOutcome.CLEANUP_FAILED)
        self.assertFalse(result.observation.summary.cleanup.clean)
        self.assertIn("cleanup_incomplete", result.blockers)
        self.assertEqual(gateway.call_tool_count, 0)

    async def test_runtime_compare_uses_explicit_mapping_and_emits_matched_or_mismatched(
        self,
    ) -> None:
        gateway = _RuntimeShadowGateway()
        observer = MCPShadowRuntimeObserver(
            storage=_RuntimeShadowStorage(granted=True),
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )
        binding = MCPToolBinding(
            capability_id="mcp.legacy.search",
            server_id="legacy-server",
            tool_name="search",
            input_schema={"type": "object"},
        )
        mapping = ApprovedVerifiedMapping(
            legacy_route="legacy-server",
            user_server_id="server-1",
            source_fingerprint="sha256:" + "a" * 64,
            config_fingerprint="config-v1",
            approved=True,
            verified=True,
        )
        profiles = (
            UserMCPServerProfile(
                server_id="server-1",
                display_name="Server",
                routing_description="Search",
                transport="streamable_http",
            ),
        )

        matched = await observer.compare_task(
            owner_user_id="owner-1",
            task_id="task-1",
            user_request="search",
            profiles=profiles,
            legacy_binding=binding,
            legacy_server_bindings=(binding,),
            legacy_transport="streamable_http",
            legacy_endpoint_url="https://legacy.example.test/mcp",
            mapping=mapping,
            config_fingerprint="config-v1",
        )
        mismatched = await observer.compare_task(
            owner_user_id="owner-1",
            task_id="task-2",
            user_request="search",
            profiles=profiles,
            legacy_binding=replace(binding, input_schema={"type": "string"}),
            legacy_server_bindings=(replace(binding, input_schema={"type": "string"}),),
            legacy_transport="streamable_http",
            legacy_endpoint_url="https://legacy.example.test/mcp",
            mapping=mapping,
            config_fingerprint="config-v1",
        )

        self.assertEqual(matched.comparison, ShadowComparison.MATCHED)
        self.assertTrue(matched.promotion_eligible)
        self.assertIsNotNone(matched.observation)
        self.assertEqual(matched.legacy_summary.route, "legacy-server")
        self.assertIs(matched.mapping, mapping)
        self.assertEqual(mismatched.comparison, ShadowComparison.MISMATCHED)
        self.assertIn("schema_fingerprints_mismatch", mismatched.blockers)
        self.assertFalse(mismatched.promotion_eligible)
        self.assertEqual(gateway.call_tool_count, 0)

    async def test_runtime_observer_infrastructure_failure_is_a_gap_not_a_mismatch(
        self,
    ) -> None:
        class _FailingSelector:
            async def select(self, context):
                del context
                raise RuntimeError("provider detail must not become mismatch evidence")

        gateway = _RuntimeShadowGateway()
        observer = MCPShadowRuntimeObserver(
            storage=_RuntimeShadowStorage(granted=True),
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_FailingSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )
        binding = MCPToolBinding(
            capability_id="mcp.legacy.search",
            server_id="legacy-server",
            tool_name="search",
            input_schema={"type": "object"},
        )
        mapping = ApprovedVerifiedMapping(
            legacy_route="legacy-server",
            user_server_id="server-1",
            source_fingerprint="sha256:" + "a" * 64,
            config_fingerprint="config-v1",
            approved=True,
            verified=True,
        )

        with self.assertRaisesRegex(ShadowObserverGapError, "shadow_observer_failed"):
            await observer.compare_task(
                owner_user_id="owner-1",
                task_id="task-gap",
                user_request="search",
                profiles=(
                    UserMCPServerProfile(
                        server_id="server-1",
                        display_name="Server",
                        routing_description="Search",
                        transport="streamable_http",
                    ),
                ),
                legacy_binding=binding,
                legacy_server_bindings=(binding,),
                legacy_transport="streamable_http",
                legacy_endpoint_url="https://legacy.example.test/mcp",
                mapping=mapping,
                config_fingerprint="config-v1",
            )
        self.assertEqual(gateway.call_tool_count, 0)
        self.assertEqual(gateway.close_count, 1)

    async def test_runtime_compare_missing_mapping_is_blocked_without_network_and_retire_is_approved_only(
        self,
    ) -> None:
        gateway = _RuntimeShadowGateway()
        observer = MCPShadowRuntimeObserver(
            storage=_RuntimeShadowStorage(granted=True),
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )
        binding = MCPToolBinding(
            capability_id="mcp.legacy.search",
            server_id="legacy-server",
            tool_name="search",
            input_schema={"type": "object"},
        )

        missing = await observer.compare_task(
            owner_user_id="owner-1",
            task_id="task-missing",
            user_request="search",
            profiles=(),
            legacy_binding=binding,
            legacy_server_bindings=(binding,),
            legacy_transport="streamable_http",
            mapping=None,
            config_fingerprint="config-v1",
        )
        retired = await observer.compare_task(
            owner_user_id="owner-1",
            task_id="task-retired",
            user_request="search",
            profiles=(),
            legacy_binding=binding,
            legacy_server_bindings=(binding,),
            legacy_transport="streamable_http",
            mapping=ApprovedVerifiedMapping(
                legacy_route="legacy-server",
                user_server_id=None,
                source_fingerprint="sha256:" + "b" * 64,
                config_fingerprint="config-v1",
                approved=True,
                verified=True,
                disposition=ShadowMappingDisposition.RETIRE,
            ),
            config_fingerprint="config-v1",
        )
        invalid = await observer.compare_task(
            owner_user_id="owner-1",
            task_id="task-invalid",
            user_request="search",
            profiles=(),
            legacy_binding=binding,
            legacy_server_bindings=(binding,),
            legacy_transport="streamable_http",
            mapping=None,
            mapping_blockers=("verified_mapping_invalid",),
            config_fingerprint="config-v1",
        )

        self.assertEqual(missing.comparison, ShadowComparison.NOT_COMPARABLE)
        self.assertIn("verified_mapping_missing", missing.blockers)
        self.assertEqual(missing.legacy_summary.route, "legacy-server")
        self.assertIsNone(missing.observation)
        self.assertFalse(missing.promotion_eligible)
        self.assertEqual(retired.comparison, ShadowComparison.NOT_COMPARABLE)
        self.assertEqual(retired.blockers, ("approved_verified_retire",))
        self.assertFalse(retired.promotion_eligible)
        self.assertEqual(invalid.comparison, ShadowComparison.MISMATCHED)
        self.assertEqual(invalid.blockers, ("verified_mapping_invalid",))
        self.assertIsNone(invalid.observation)
        self.assertEqual(gateway.close_count, 0)
        self.assertEqual(gateway.call_tool_count, 0)

    def test_runtime_mapping_requires_exact_cp4_provenance_and_deterministic_target(
        self,
    ) -> None:
        legacy_server = MCPServerConfig.from_mapping(
            {
                "server_id": "legacy-server",
                "endpoint": "https://legacy.example.test/mcp",
                "transport": "streamable_http",
            }
        )
        target_id = deterministic_migrated_server_id("legacy-server", "owner-1")
        source_fingerprint = legacy_migration_source_fingerprint(legacy_server)
        migration_plan = plan_legacy_mcp_config_migration(
            MCPRuntimeConfig(enabled=True, servers=(legacy_server,)),
            (
                LegacyServerClassification(
                    server_id="legacy-server",
                    disposition=LegacyDisposition.MIGRATE_OWNER,
                    consumer_scope=LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="owner-1",
                    target_consumer_refs=("hmac-sha256:" + "a" * 64,),
                ),
            ),
        )
        provenance = {
            "schema": "legacy_mcp_migration_provenance.v1",
            "source_server_id": "legacy-server",
            "source_fingerprint": source_fingerprint,
            "owner_user_id": "owner-1",
            "target_server_id": target_id,
            "credential_digest": "hmac-sha256:" + "c" * 64,
            "credential_security_version": 1,
            "validator_provenance": "builtin-user-mcp-health-v1",
            "observed_at": "2026-08-13T00:00:00",
            "expires_at": "2026-08-13T00:02:00",
        }
        migrated = UserMCPServer(
            server_id=target_id,
            owner_user_id="owner-1",
            display_name="Legacy",
            routing_description="Migrated",
            endpoint_url="https://legacy.example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            auth_metadata={"migration_provenance": provenance},
            health_status=UserMCPHealthStatus.AVAILABLE,
            last_tested_at=datetime(2026, 8, 13),
        )
        cipher = credential_cipher(b"c" * 32)
        signer = audit_reference_signer(b"c" * 32)
        credential_digest = migration_target_credential_digest(
            cipher,
            signer,
            server=migrated,
            credential_record=None,
            source_fingerprint=source_fingerprint,
        )
        assert credential_digest is not None
        migrated = replace(
            migrated,
            auth_metadata={
                "migration_provenance": {
                    **provenance,
                    "credential_digest": credential_digest,
                }
            },
        )

        resolved = resolve_approved_migration_mapping(
            legacy_server_id="legacy-server",
            owner_user_id="owner-1",
            legacy_server=legacy_server,
            user_servers=(migrated,),
            target_credential_digests={target_id: credential_digest},
            config_fingerprint="config-v1",
        )
        inferred_only = resolve_approved_migration_mapping(
            legacy_server_id="legacy-server",
            owner_user_id="owner-1",
            legacy_server=legacy_server,
            user_servers=(replace(migrated, auth_metadata={}),),
            target_credential_digests={target_id: credential_digest},
            config_fingerprint="config-v1",
        )
        tampered = resolve_approved_migration_mapping(
            legacy_server_id="legacy-server",
            owner_user_id="owner-1",
            legacy_server=legacy_server,
            user_servers=(
                replace(
                    migrated,
                    auth_metadata={
                        "migration_provenance": {
                            **provenance,
                            "source_fingerprint": "sha256:" + "d" * 64,
                        }
                    },
                ),
            ),
            target_credential_digests={target_id: credential_digest},
            config_fingerprint="config-v1",
        )
        changed_target = resolve_approved_migration_mapping(
            legacy_server_id="legacy-server",
            owner_user_id="owner-1",
            legacy_server=legacy_server,
            user_servers=(
                replace(
                    migrated,
                    endpoint_url="https://changed.example.test/mcp",
                    config_version=2,
                    security_version=2,
                ),
            ),
            target_credential_digests={target_id: credential_digest},
            config_fingerprint="config-v1",
        )
        forged_credential = resolve_approved_migration_mapping(
            legacy_server_id="legacy-server",
            owner_user_id="owner-1",
            legacy_server=legacy_server,
            user_servers=(migrated,),
            target_credential_digests={target_id: "hmac-sha256:" + "f" * 64},
            config_fingerprint="config-v1",
        )
        window_tampered_server = replace(
            migrated,
            auth_metadata={
                "migration_provenance": {
                    **migrated.auth_metadata["migration_provenance"],
                    "expires_at": "2026-08-13T00:03:00",
                }
            },
        )
        window_tampered_digest = migration_target_credential_digest(
            cipher,
            signer,
            server=window_tampered_server,
            credential_record=None,
            source_fingerprint=source_fingerprint,
        )
        assert window_tampered_digest is not None
        window_tampered = resolve_approved_migration_mapping(
            legacy_server_id="legacy-server",
            owner_user_id="owner-1",
            legacy_server=legacy_server,
            user_servers=(window_tampered_server,),
            target_credential_digests={target_id: window_tampered_digest},
            config_fingerprint="config-v1",
        )

        self.assertIsNotNone(resolved.mapping)
        self.assertEqual(resolved.mapping.user_server_id, target_id)
        self.assertEqual(
            source_fingerprint,
            migration_plan.mapping_candidates[0].source_fingerprint,
        )
        self.assertEqual(
            target_id,
            migration_plan.mapping_candidates[0].target_server_id,
        )
        self.assertEqual(inferred_only.blockers, ("verified_mapping_missing",))
        self.assertEqual(tampered.blockers, ("verified_mapping_invalid",))
        self.assertEqual(changed_target.blockers, ("verified_mapping_invalid",))
        self.assertEqual(
            forged_credential.blockers,
            ("verified_mapping_invalid",),
        )
        self.assertEqual(
            window_tampered.blockers,
            ("verified_mapping_invalid",),
        )

    def test_shadow_catalog_digest_key_is_secret_and_config_bound(self) -> None:
        signer = audit_reference_signer(b"k" * 32)

        first = derive_shadow_catalog_digest_key(
            signer,
            config_fingerprint="rollout-fingerprint-1",
        )
        repeated = derive_shadow_catalog_digest_key(
            signer,
            config_fingerprint="rollout-fingerprint-1",
        )
        changed = derive_shadow_catalog_digest_key(
            signer,
            config_fingerprint="rollout-fingerprint-2",
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(first, b"rollout-fingerprint-1")
        self.assertEqual(len(first), 32)

    async def test_runtime_adapter_suppresses_permission_without_writes_or_call(
        self,
    ) -> None:
        gateway = _RuntimeShadowGateway()
        storage = _RuntimeShadowStorage(granted=False)
        observer = MCPShadowRuntimeObserver(
            storage=storage,
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )

        result = await observer.observe_task(
            owner_user_id="owner-1",
            task_id="task-1",
            user_request="search",
            profiles=(
                UserMCPServerProfile(
                    server_id="server-1",
                    display_name="Server",
                    routing_description="Search",
                    transport="streamable_http",
                ),
            ),
        )

        self.assertEqual(result.outcome, ShadowOutcome.PERMISSION_DENIED_SUPPRESSED)
        self.assertEqual(gateway.call_tool_count, 0)
        self.assertEqual(gateway.close_count, 1)

    async def test_runtime_adapter_validates_selector_arguments_without_calling_tool(
        self,
    ) -> None:
        gateway = _RuntimeShadowGateway()
        gateway.catalog = ToolCatalogSnapshot(
            server_id="server-1",
            effective_protocol_version="2025-06-18",
            tools=(
                MCPToolDescriptor(
                    name="search",
                    description="Search",
                    input_schema={
                        "type": "object",
                        "properties": {"q": {"type": "integer"}},
                        "required": ["q"],
                    },
                    input_schema_sha256="schema-sha",
                ),
            ),
        )
        observer = MCPShadowRuntimeObserver(
            storage=_RuntimeShadowStorage(granted=True),
            gateway=gateway,
            server_router=_RuntimeShadowRouter(),
            selector=_RuntimeShadowSelector(),
            endpoint_policy=_runtime_endpoint_policy(),
            digest_key=b"digest-key",
        )

        result = await observer.observe_task(
            owner_user_id="owner-1",
            task_id="task-invalid-arguments",
            user_request="search",
            profiles=(
                UserMCPServerProfile(
                    server_id="server-1",
                    display_name="Server",
                    routing_description="Search",
                    transport="streamable_http",
                ),
            ),
        )

        self.assertEqual(result.outcome, ShadowOutcome.OBSERVER_FAILED)
        self.assertFalse(result.summary.schema_valid)
        self.assertEqual(gateway.call_tool_count, 0)
        self.assertEqual(gateway.close_count, 1)

    def test_manifest_is_closed_versioned_and_has_stable_fingerprint(self) -> None:
        manifest = _manifest()
        reordered = replace(
            manifest, expectations=tuple(reversed(manifest.expectations))
        )

        self.assertEqual(manifest.fingerprint, reordered.fingerprint)
        self.assertEqual(len(manifest.expectations), 7)
        with self.assertRaisesRegex(ShadowManifestError, "schema_version"):
            replace(manifest, schema_version="user_mcp_shadow_manifest.v2")
        with self.assertRaisesRegex(ShadowManifestError, "every closed scenario"):
            replace(manifest, expectations=manifest.expectations[:-1])
        with self.assertRaisesRegex(ShadowManifestError, "closed ShadowScenario"):
            replace(manifest.expectations[0], scenario="future_scenario")  # type: ignore[arg-type]
        denial = manifest.expectation_for(ShadowScenario.PERMISSION_DENIAL)
        with self.assertRaisesRegex(
            ShadowManifestError, "closed expected policy delta"
        ):
            replace(
                manifest,
                expectations=tuple(
                    replace(item, expected_policy_delta=None)
                    if item is denial
                    else item
                    for item in manifest.expectations
                ),
            )

    def test_signed_manifest_loader_verifies_attestation_and_exact_startup_bindings(
        self,
    ) -> None:
        manifest = _manifest()
        key_id = "shadow-key-v1"
        key = b"s" * 32
        document = _signed_manifest_document(manifest, key_id=key_id, key=key)

        verified = load_signed_shadow_manifest(
            json.dumps(document),
            trusted_attestation_keys={key_id: key},
            expected_config_fingerprint=manifest.config_fingerprint,
            expected_fixture_fingerprint=manifest.fixture_fingerprint,
            expected_mapping_fingerprint=manifest.mapping_fingerprint,
        )

        self.assertEqual(verified.manifest, manifest)
        self.assertEqual(verified.fingerprint, manifest.fingerprint)
        self.assertEqual(verified.attestation_key_id, key_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shadow-manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            from_file = load_signed_shadow_manifest_file(
                path,
                trusted_attestation_keys={key_id: key},
                expected_config_fingerprint=manifest.config_fingerprint,
                expected_fixture_fingerprint=manifest.fixture_fingerprint,
                expected_mapping_fingerprint=manifest.mapping_fingerprint,
            )
            symlink = Path(temp_dir) / "shadow-manifest-link.json"
            symlink.symlink_to(path)
            with self.assertRaisesRegex(ShadowManifestError, "must not be a symlink"):
                load_signed_shadow_manifest_file(
                    symlink,
                    trusted_attestation_keys={key_id: key},
                    expected_config_fingerprint=manifest.config_fingerprint,
                    expected_fixture_fingerprint=manifest.fixture_fingerprint,
                    expected_mapping_fingerprint=manifest.mapping_fingerprint,
                )
        self.assertEqual(from_file, verified)

        live = compare_live_shadow_sample(
            verified_manifest=verified,
            scenario=ShadowScenario.HTTPS_STREAMABLE_SUCCESS,
            nonce="live-sample-1",
            legacy_outcome=ShadowOutcome.TOOL_CALL_SUCCEEDED,
            observation=_sample(ShadowScenario.HTTPS_STREAMABLE_SUCCESS).observation,
            legacy_summary=_sample(
                ShadowScenario.HTTPS_STREAMABLE_SUCCESS
            ).legacy_summary,
            legacy_route="legacy-server",
            mapping=_mapping(),
            approved_mappings=(_mapping(),),
        )
        self.assertEqual(live.result.comparison, ShadowComparison.MATCHED)
        self.assertTrue(live.promotion_eligible)
        self.assertEqual(
            live.sample.fixture_fingerprint,
            manifest.fixture_fingerprint,
        )

    def test_signed_manifest_loader_rejects_tamper_unknown_fields_and_unbound_fixtures(
        self,
    ) -> None:
        manifest = _manifest()
        key_id = "shadow-key-v1"
        key = b"s" * 32
        document = _signed_manifest_document(manifest, key_id=key_id, key=key)

        cases = (
            (
                {**document, "fixture_fingerprint": "different-fixture"},
                {key_id: key},
                manifest.fixture_fingerprint,
                "binding mismatch",
            ),
            (
                {**document, "unexpected": "field"},
                {key_id: key},
                manifest.fixture_fingerprint,
                "closed schema",
            ),
            (
                document,
                {"other-key": key},
                manifest.fixture_fingerprint,
                "not trusted",
            ),
            (
                {**document, "attestation_signature": "0" * 64},
                {key_id: key},
                manifest.fixture_fingerprint,
                "attestation is invalid",
            ),
        )
        for candidate, keys, fixture, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ShadowManifestError, message):
                    load_signed_shadow_manifest(
                        candidate,
                        trusted_attestation_keys=keys,
                        expected_config_fingerprint=manifest.config_fingerprint,
                        expected_fixture_fingerprint=fixture,
                        expected_mapping_fingerprint=manifest.mapping_fingerprint,
                    )

        encoded = json.dumps(document)
        duplicated = encoded.replace(
            '"manifest_id": "window-a",',
            '"manifest_id": "window-a", "manifest_id": "window-b",',
            1,
        )
        with self.assertRaisesRegex(ShadowManifestError, "duplicate JSON key"):
            load_signed_shadow_manifest(
                duplicated,
                trusted_attestation_keys={key_id: key},
                expected_config_fingerprint=manifest.config_fingerprint,
                expected_fixture_fingerprint=manifest.fixture_fingerprint,
                expected_mapping_fingerprint=manifest.mapping_fingerprint,
            )

        relabeled_bindings = {
            "mcp.legacy.search": ShadowScenario.PERMISSION_DENIAL,
        }
        self.assertNotEqual(
            shadow_fixture_bindings_fingerprint(_FIXTURE_BINDINGS),
            shadow_fixture_bindings_fingerprint(relabeled_bindings),
        )
        with self.assertRaisesRegex(
            ShadowManifestError, "fixture_fingerprint binding mismatch"
        ):
            load_signed_shadow_manifest(
                document,
                trusted_attestation_keys={key_id: key},
                expected_config_fingerprint=manifest.config_fingerprint,
                expected_fixture_fingerprint=shadow_fixture_bindings_fingerprint(
                    relabeled_bindings
                ),
                expected_mapping_fingerprint=manifest.mapping_fingerprint,
            )

    def test_live_sample_binds_the_complete_approved_mapping_set(self) -> None:
        first = _mapping()
        second = ApprovedVerifiedMapping(
            legacy_route="legacy-second",
            user_server_id="user-second",
            source_fingerprint="source-second",
            config_fingerprint="config-v7",
            approved=True,
            verified=True,
        )
        aggregate = approved_shadow_mapping_set_fingerprint((second, first))
        self.assertEqual(
            aggregate,
            approved_shadow_mapping_set_fingerprint((first, second)),
        )
        manifest = replace(_manifest(), mapping_fingerprint=aggregate)
        key_id = "shadow-key-v1"
        key = b"s" * 32
        verified = load_signed_shadow_manifest(
            _signed_manifest_document(manifest, key_id=key_id, key=key),
            trusted_attestation_keys={key_id: key},
            expected_config_fingerprint=manifest.config_fingerprint,
            expected_fixture_fingerprint=manifest.fixture_fingerprint,
            expected_mapping_fingerprint=aggregate,
        )
        source = _sample(ShadowScenario.HTTPS_STREAMABLE_SUCCESS)
        compared = compare_live_shadow_sample(
            verified_manifest=verified,
            scenario=source.scenario,
            nonce="two-mapping-set",
            legacy_outcome=source.legacy_outcome,
            observation=source.observation,
            legacy_summary=source.legacy_summary,
            legacy_route=source.legacy_route,
            mapping=first,
            approved_mappings=(first, second),
        )
        self.assertEqual(compared.result.comparison, ShadowComparison.MATCHED)

        tampered = replace(second, user_server_id="user-second-tampered")
        rejected = compare_live_shadow_sample(
            verified_manifest=verified,
            scenario=source.scenario,
            nonce="tampered-mapping-set",
            legacy_outcome=source.legacy_outcome,
            observation=source.observation,
            legacy_summary=source.legacy_summary,
            legacy_route=source.legacy_route,
            mapping=first,
            approved_mappings=(first, tampered),
        )
        self.assertEqual(rejected.result.comparison, ShadowComparison.MISMATCHED)
        self.assertIn("mapping_set_fingerprint_mismatch", rejected.result.blockers)

        omitted = compare_live_shadow_sample(
            verified_manifest=verified,
            scenario=source.scenario,
            nonce="omitted-route-mapping",
            legacy_outcome=source.legacy_outcome,
            observation=source.observation,
            legacy_summary=source.legacy_summary,
            legacy_route=source.legacy_route,
            mapping=first,
            approved_mappings=(second,),
        )
        self.assertEqual(omitted.result.comparison, ShadowComparison.MISMATCHED)
        self.assertIn("verified_mapping_not_in_approved_set", omitted.result.blockers)

    async def test_success_summary_is_hmac_and_schema_fingerprint_only(self) -> None:
        fake = _FakeControlPlane()
        observer = ShadowControlPlaneObserver(
            digest_key=b"test-key", callbacks=fake.callbacks()
        )
        profile = ShadowServerProfile("user-server", "streamable_http", "allowed", True)

        result = await observer.observe(profiles=(profile,))

        self.assertEqual(result.outcome, ShadowOutcome.CONTROL_PLANE_READY)
        self.assertEqual(result.summary.catalog_count, 1)
        self.assertNotIn("secret.tool.name", repr(result.summary))
        self.assertNotIn("schema-value", repr(result.summary))
        self.assertTrue(result.summary.cleanup.clean)
        self.assertEqual(fake.close_count, 1)
        self.assertEqual(
            set(ShadowObserverCallbacks.__dataclass_fields__),
            {"router", "discover", "list_tools", "selector", "read_grant"},
        )
        self._assert_zero_mutation_surface(fake)

    async def test_all_failure_and_denial_branches_remain_zero_call(self) -> None:
        expected = {
            "auth": ShadowOutcome.AUTHENTICATION_FAILED,
            "timeout": ShadowOutcome.TIMEOUT,
            "denial": ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
            "selector": ShadowOutcome.OBSERVER_FAILED,
            "cleanup": ShadowOutcome.CLEANUP_FAILED,
        }
        profile = ShadowServerProfile("user-server", "streamable_http", "allowed", True)

        for failure, expected_outcome in expected.items():
            with self.subTest(failure=failure):
                fake = _FakeControlPlane(failure)
                observer = ShadowControlPlaneObserver(
                    digest_key=b"test-key", callbacks=fake.callbacks()
                )
                result = await observer.observe(profiles=(profile,))
                self.assertEqual(result.outcome, expected_outcome)
                self.assertNotIn("raw", result.error_code or "")
                self.assertEqual(fake.close_count, 0 if failure == "auth" else 1)
                self._assert_zero_mutation_surface(fake)

    def test_all_seven_expected_result_scenarios_match(self) -> None:
        manifest = _manifest()
        for scenario in CURRENT_SHADOW_SCENARIOS:
            with self.subTest(scenario=scenario):
                result = compare_shadow_sample(_sample(scenario), manifest)
                self.assertEqual(result.comparison, ShadowComparison.MATCHED)

    def test_permission_denial_is_the_only_lane_specific_success_delta(self) -> None:
        manifest = _manifest()
        denial = _sample(ShadowScenario.PERMISSION_DENIAL)
        self.assertEqual(
            compare_shadow_sample(denial, manifest).comparison, ShadowComparison.MATCHED
        )

        generalized = replace(
            _sample(ShadowScenario.HTTPS_STREAMABLE_SUCCESS),
            observation=replace(
                _sample(ShadowScenario.HTTPS_STREAMABLE_SUCCESS).observation,
                outcome=ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
            ),
        )
        result = compare_shadow_sample(generalized, manifest)
        self.assertEqual(result.comparison, ShadowComparison.MISMATCHED)
        self.assertIn("shadow_outcome_mismatch", result.blockers)

    def test_not_comparable_requires_an_approved_verified_retire_mapping(self) -> None:
        manifest = _manifest()
        sample = _sample(ShadowScenario.HTTPS_STREAMABLE_SUCCESS)
        retired = replace(
            sample, mapping=_mapping(disposition=ShadowMappingDisposition.RETIRE)
        )

        self.assertEqual(
            compare_shadow_sample(retired, manifest).comparison,
            ShadowComparison.NOT_COMPARABLE,
        )
        missing = compare_shadow_sample(replace(sample, mapping=None), manifest)
        self.assertEqual(missing.comparison, ShadowComparison.NOT_COMPARABLE)
        self.assertIn("verified_mapping_missing", missing.blockers)
        invalid = compare_shadow_sample(
            replace(sample, mapping=replace(_mapping(), verified=False)), manifest
        )
        self.assertEqual(invalid.comparison, ShadowComparison.MISMATCHED)

    def test_validator_checks_every_eligible_sample_and_cannot_dilute_invalid_evidence(
        self,
    ) -> None:
        manifest = _manifest()
        samples = [
            _sample(scenario, nonce=f"{scenario.value}-{copy}")
            for scenario in CURRENT_SHADOW_SCENARIOS
            for copy in range(3)
        ]
        passed = validate_shadow_samples(samples, manifest)
        self.assertTrue(passed.allowed, passed.blockers)

        mismatched = replace(
            samples[0],
            observation=replace(
                samples[0].observation, outcome=ShadowOutcome.OBSERVER_FAILED
            ),
        )
        duplicate = replace(samples[1], nonce=samples[2].nonce)
        invalid = replace(samples[3], digest_valid=False)
        combined = [mismatched, duplicate, invalid, *samples[4:], *samples]
        failed = validate_shadow_samples(combined, manifest)

        self.assertFalse(failed.allowed)
        self.assertEqual(len(failed.comparisons), len(combined))
        self.assertTrue(
            any("shadow_outcome_mismatch" in item for item in failed.blockers)
        )
        self.assertTrue(any("duplicate_nonce" in item for item in failed.blockers))
        self.assertTrue(any("digest_invalid" in item for item in failed.blockers))

    def _assert_zero_mutation_surface(self, fake: _FakeControlPlane) -> None:
        self.assertFalse(hasattr(fake, "call_tool"))
        self.assertEqual(fake.grant_writes, 0)
        self.assertEqual(fake.interrupt_writes, 0)
        self.assertEqual(fake.event_writes, 0)
        self.assertEqual(fake.answer_writes, 0)


if __name__ == "__main__":
    unittest.main()
