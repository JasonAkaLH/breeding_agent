from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from src.capabilities.mcp_dispatch.models import (
    MCPSelectorAction,
    MCPSelectorActionType,
    MCPServerRouteAction,
    MCPServerRouteActionType,
)
from src.core.enums import EventVisibility, NodeStatus, TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import (
    Conversation,
    EventRecord,
    Task,
    TaskNode,
    UserMCPServer,
    UserMCPToolGrant,
)
from src.integrations.mcp.audit import MCPAuditService
from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyProvenance,
)
from src.integrations.mcp.gateway_models import MCPToolDescriptor, ToolCatalogSnapshot
from src.integrations.mcp.runtime_state import (
    MCPRuntimeBundle,
    MCPToolBinding,
)
from src.integrations.mcp.shadow_compare import (
    ApprovedVerifiedMapping,
    CURRENT_SHADOW_SCENARIOS,
    MCPShadowRuntimeObserver,
    RuntimeShadowComparisonResult,
    RuntimeShadowMappingResolution,
    SHADOW_SCENARIO_EXPECTATIONS,
    ShadowCleanupCounts,
    ShadowComparison,
    ShadowObservation,
    ShadowOutcome,
    ShadowSafeSummary,
    ShadowScenario,
    ShadowScenarioExpectation,
    ShadowScenarioManifest,
    VerifiedShadowScenarioManifest,
    approved_shadow_mapping_set_fingerprint,
    deterministic_migrated_server_id,
    legacy_migration_source_fingerprint,
    migration_target_credential_digest,
    resolve_approved_migration_mapping,
    shadow_fixture_bindings_fingerprint,
)
from tests.master_key_support import (
    audit_reference_signer,
    credential_cipher as make_credential_cipher,
)
from src.orchestration.models import (
    OrchestrationRequest,
    OrchestrationRunResult,
    UserMCPServerProfile,
    WorkflowNodePlan,
    WorkflowPlan,
)
from tests.api.support import APITestCase


class _PinnedLegacyRuntimeState:
    def __init__(
        self,
        *,
        config: MCPRuntimeConfig,
        pinned_bundle: MCPRuntimeBundle,
        active_bundle: MCPRuntimeBundle,
    ) -> None:
        self.config = config
        self.active_bundle = active_bundle
        self.requested_revisions: list[str] = []
        self._bundles = {
            pinned_bundle.revision: pinned_bundle,
            active_bundle.revision: active_bundle,
        }

    def bundle_for_revision(self, revision: str) -> MCPRuntimeBundle:
        self.requested_revisions.append(revision)
        return self._bundles[revision]


class _UserConfigSource:
    def __init__(self, servers: tuple[UserMCPServer, ...]) -> None:
        self.servers = servers

    async def list_servers(self, owner_user_id: str) -> list[UserMCPServer]:
        return [
            server
            for server in self.servers
            if server.owner_user_id == owner_user_id
        ]


class _ShadowObserver:
    def __init__(
        self,
        timeline: list[str],
        comparison: ShadowComparison | tuple[ShadowComparison, ...],
    ) -> None:
        self.timeline = timeline
        self.comparisons = (
            comparison if isinstance(comparison, tuple) else (comparison,)
        )
        self.calls: list[dict[str, object]] = []

    async def compare_task(self, **kwargs) -> RuntimeShadowComparisonResult:
        self.timeline.append("shadow")
        self.calls.append(kwargs)
        comparison = self.comparisons[len(self.calls) - 1]
        mapping = kwargs["mapping"]
        legacy_summary = ShadowSafeSummary(
            route=kwargs["legacy_binding"].server_id,
            transport=kwargs["legacy_transport"],
            endpoint_policy="runtime_enforced",
            catalog_count=1,
            catalog_names_hmac="catalog",
            schema_fingerprints=("schema",),
            selected_tool_hmac="tool",
            schema_valid=True,
            endpoint_policy_allowed=True,
            ownership_verified=True,
            grant_exists=None,
            latency_buckets={},
            cleanup=ShadowCleanupCounts(),
        )
        shadow_summary = replace(
            legacy_summary,
            route=mapping.user_server_id,
            catalog_count=(
                2 if comparison is ShadowComparison.MISMATCHED else 1
            ),
        )
        return RuntimeShadowComparisonResult(
            comparison=comparison,
            blockers=(
                ()
                if comparison is ShadowComparison.MATCHED
                else ("catalog_count_mismatch",)
            ),
            observation=ShadowObservation(
                outcome=ShadowOutcome.CONTROL_PLANE_READY,
                summary=shadow_summary,
            ),
            legacy_summary=legacy_summary,
            mapping=mapping,
        )


class _FailingShadowObserver:
    async def compare_task(self, **kwargs):
        del kwargs
        raise RuntimeError("observer unavailable")


class _PublicHTTPResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return ("93.184.216.34",)


class _ReadonlyPublicHTTPGateway:
    def __init__(self, *, server_id: str, cleanup_error: bool = False) -> None:
        self.server_id = server_id
        self.cleanup_error = cleanup_error
        self.readonly_open_count = 0
        self.close_count = 0
        self.call_tool_count = 0
        self.catalog = ToolCatalogSnapshot(
            server_id=server_id,
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

    async def open_readonly_shadow_session(
        self,
        principal,
        task_id: str,
        server_id: str,
    ):
        self.readonly_open_count += 1
        if server_id != self.server_id:
            raise AssertionError("shadow routed to an unexpected server")
        gateway = self

        class _Session:
            scope = SimpleNamespace(
                owner_user_id=principal.username,
                platform_task_id=task_id,
                server_id=server_id,
                security_version=1,
            )
            catalog = gateway.catalog
            endpoint_policy_provenance = EndpointPolicyProvenance.RUNTIME_ENFORCED

            async def aclose(self) -> None:
                gateway.close_count += 1
                if gateway.cleanup_error:
                    raise RuntimeError("readonly shadow cleanup failed")

        return _Session()

    async def open_scope(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("shadow must not open a mutable gateway scope")

    async def call_tool(self, *args, **kwargs):
        del args, kwargs
        self.call_tool_count += 1
        raise AssertionError("shadow must not call tools/call")


class _StaticShadowRouter:
    def __init__(self, server_id: str) -> None:
        self.server_id = server_id

    async def route(self, **kwargs) -> MCPServerRouteAction:
        del kwargs
        return MCPServerRouteAction(
            MCPServerRouteActionType.ROUTE_SERVER,
            server_id=self.server_id,
        )


class _SearchShadowSelector:
    async def select(self, context) -> MCPSelectorAction:
        del context
        return MCPSelectorAction(
            MCPSelectorActionType.CALL_TOOL,
            tool_name="search",
            arguments={},
        )


class _MetricRecorder:
    def __init__(self) -> None:
        self.shadow_mismatches = 0

    async def record_shadow_mismatch(self, observed_at=None) -> None:
        self.shadow_mismatches += 1


class _CapturingAuditService(MCPAuditService):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.errors: list[Exception] = []

    async def record_shadow_sample(self, sample):
        try:
            return await super().record_shadow_sample(sample)
        except Exception as exc:
            self.errors.append(exc)
            raise


class _PlanProvider:
    def __init__(self, timeline: list[str], plan: WorkflowPlan) -> None:
        self.timeline = timeline
        self.plan = plan

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        self.timeline.append("plan")
        return self.plan


class _ExecutionService:
    def __init__(self, timeline: list[str], storage) -> None:
        self.timeline = timeline
        self.storage = storage
        self.calls: list[tuple[OrchestrationRequest, WorkflowPlan]] = []

    async def execute_request(
        self,
        request: OrchestrationRequest,
        plan: WorkflowPlan,
        *,
        active_task_count: int,
    ) -> OrchestrationRunResult:
        self.timeline.append("execute")
        self.calls.append((request, plan))
        for node in plan.nodes:
            await self.storage.append_event(
                EventRecord(
                    event_id=f"evt-{node.node_id}",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id=node.node_id,
                    event_type="mcp.tool_call_completed",
                    payload={"output_size_bytes": 10, "truncated": False},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        return OrchestrationRunResult(
            task=None,
            nodes=tuple(
                TaskNode(
                    node.node_id,
                    request.task_id,
                    node.capability_id,
                    status=NodeStatus.COMPLETED,
                )
                for node in plan.nodes
            ),
            completion_status=str(TaskStatus.COMPLETED),
        )


def _verified_manifest(
    *, config_fingerprint: str, mapping_fingerprint: str
) -> VerifiedShadowScenarioManifest:
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
        ShadowScenario.TIMEOUT: (
            ShadowOutcome.TIMEOUT,
            ShadowOutcome.TIMEOUT,
        ),
        ShadowScenario.PERMISSION_DENIAL: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED,
            ShadowOutcome.PERMISSION_DENIED_SUPPRESSED,
        ),
        ShadowScenario.LARGE_OUTPUT: (
            ShadowOutcome.TOOL_CALL_SUCCEEDED_LARGE_RESULT,
            ShadowOutcome.CONTROL_PLANE_READY,
        ),
    }
    manifest = ShadowScenarioManifest(
        manifest_id="runtime-test",
        config_fingerprint=config_fingerprint,
        fixture_fingerprint="fixture-v1",
        mapping_fingerprint=mapping_fingerprint,
        expectations=tuple(
            ShadowScenarioExpectation(
                scenario=scenario,
                legacy_outcome=legacy,
                shadow_outcome=shadow,
                transport=SHADOW_SCENARIO_EXPECTATIONS[scenario][2],
                endpoint_policy=SHADOW_SCENARIO_EXPECTATIONS[scenario][3],
                timeout_checkpoint=(
                    "list" if scenario is ShadowScenario.TIMEOUT else None
                ),
                expected_policy_delta=(
                    "legacy_success_shadow_permission_denial"
                    if scenario is ShadowScenario.PERMISSION_DENIAL
                    else None
                ),
            )
            for scenario, (legacy, shadow) in outcomes.items()
        ),
    )
    return VerifiedShadowScenarioManifest(
        manifest=manifest,
        attestation_key_id="test-key",
        attestation_signature="0" * 64,
    )


class UserMCPLiveShadowRuntimeTest(APITestCase):
    def test_fixture_relabel_and_mapping_set_tamper_change_signed_bindings(self) -> None:
        original_fixture = shadow_fixture_bindings_fingerprint(
            {"mcp.legacy.search": ShadowScenario.HTTPS_STREAMABLE_SUCCESS}
        )
        relabeled_fixture = shadow_fixture_bindings_fingerprint(
            {"mcp.legacy.search": ShadowScenario.AUTHENTICATION_FAILURE}
        )
        self.assertNotEqual(original_fixture, relabeled_fixture)

        first = ApprovedVerifiedMapping(
            legacy_route="legacy-a",
            user_server_id="user-a",
            source_fingerprint="source-a",
            config_fingerprint="config-v1",
            approved=True,
            verified=True,
        )
        second = ApprovedVerifiedMapping(
            legacy_route="legacy-b",
            user_server_id="user-b",
            source_fingerprint="source-b",
            config_fingerprint="config-v1",
            approved=True,
            verified=True,
        )
        self.assertNotEqual(
            approved_shadow_mapping_set_fingerprint((first, second)),
            approved_shadow_mapping_set_fingerprint((first,)),
        )

    async def test_missing_verified_manifest_records_gap_without_observing(self) -> None:
        request = OrchestrationRequest(
            task_id="task-manifest-gap",
            conversation_id="conv-manifest-gap",
            root_message_id="msg-manifest-gap",
            user_message="search",
            metadata={
                "mcp_execution_mode": "legacy",
                "mcp_shadow_enabled": True,
            },
        )
        gaps: list[str] = []
        original = self.runtime._record_mcp_shadow_setup_failure

        async def capture_gap(_request, *, reason_code: str) -> None:
            gaps.append(reason_code)

        self.runtime._record_mcp_shadow_setup_failure = capture_gap
        try:
            handle = await self.runtime._begin_mcp_shadow_observation(
                request=request,
                plan=WorkflowPlan(task_id=request.task_id, nodes=()),
            )
        finally:
            self.runtime._record_mcp_shadow_setup_failure = original
        self.assertIsNone(handle)
        self.assertEqual(gaps, ["shadow_verified_manifest_missing"])

    async def test_shadow_timeout_cancels_observer_before_cleanup_returns(self) -> None:
        cancelled = asyncio.Event()

        async def block() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        observer_task = asyncio.create_task(block())
        request = OrchestrationRequest("task-timeout", "conv", "msg", "search")
        handle = SimpleNamespace(
            request=request,
            observations=(SimpleNamespace(task=observer_task),),
        )
        gaps: list[str] = []
        original_gap = self.runtime._record_mcp_shadow_setup_failure
        original_timeout = self.runtime._mcp_shadow_terminal_timeout_seconds

        async def capture_gap(_request, *, reason_code: str) -> None:
            gaps.append(reason_code)

        self.runtime._record_mcp_shadow_setup_failure = capture_gap
        self.runtime._mcp_shadow_terminal_timeout_seconds = 0.01
        try:
            await self.runtime._finish_mcp_shadow_observation(handle, None)
        finally:
            self.runtime._record_mcp_shadow_setup_failure = original_gap
            self.runtime._mcp_shadow_terminal_timeout_seconds = original_timeout
        self.assertTrue(observer_task.cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(gaps, ["shadow_observation_timeout"])

    async def test_shadow_terminal_finalize_failure_records_closed_gap(self) -> None:
        timeline: list[str] = []
        request = OrchestrationRequest(
            "task-finalize-gap",
            "conv-finalize-gap",
            "msg-finalize-gap",
            "hello",
        )
        await self.runtime.storage.save_conversation(
            Conversation("conv-finalize-gap", "owner-1")
        )
        await self.runtime.storage.save_task(
            Task("task-finalize-gap", "conv-finalize-gap", "msg-finalize-gap")
        )
        originals = (
            self.runtime.workflow_provider,
            self.runtime.orchestration_service,
            self.runtime._conversation_memory_builder,
            self.runtime._finish_mcp_shadow_observation,
            self.runtime._record_mcp_shadow_setup_failure,
        )
        self.runtime.workflow_provider = _PlanProvider(
            timeline,
            WorkflowPlan(task_id=request.task_id, nodes=()),
        )
        self.runtime.orchestration_service = _ExecutionService(
            timeline,
            self.runtime.storage,
        )
        self.runtime._conversation_memory_builder = None
        gaps: list[str] = []

        async def fail_finalize(handle, result) -> None:
            del handle, result
            raise RuntimeError("finalize failed")

        async def capture_gap(_request, *, reason_code: str) -> None:
            gaps.append(reason_code)

        self.runtime._finish_mcp_shadow_observation = fail_finalize
        self.runtime._record_mcp_shadow_setup_failure = capture_gap
        try:
            await self.runtime._run_execution(request, active_task_count=0)
        finally:
            (
                self.runtime.workflow_provider,
                self.runtime.orchestration_service,
                self.runtime._conversation_memory_builder,
                self.runtime._finish_mcp_shadow_observation,
                self.runtime._record_mcp_shadow_setup_failure,
            ) = originals

        self.assertEqual(gaps, ["shadow_terminal_finalize_failed"])

    async def test_shadow_terminal_finalize_cancellation_is_not_swallowed(self) -> None:
        timeline: list[str] = []
        request = OrchestrationRequest(
            "task-finalize-cancelled",
            "conv-finalize-cancelled",
            "msg-finalize-cancelled",
            "hello",
        )
        await self.runtime.storage.save_conversation(
            Conversation("conv-finalize-cancelled", "owner-1")
        )
        await self.runtime.storage.save_task(
            Task(
                "task-finalize-cancelled",
                "conv-finalize-cancelled",
                "msg-finalize-cancelled",
            )
        )
        originals = (
            self.runtime.workflow_provider,
            self.runtime.orchestration_service,
            self.runtime._conversation_memory_builder,
            self.runtime._finish_mcp_shadow_observation,
        )
        self.runtime.workflow_provider = _PlanProvider(
            timeline,
            WorkflowPlan(task_id=request.task_id, nodes=()),
        )
        self.runtime.orchestration_service = _ExecutionService(
            timeline,
            self.runtime.storage,
        )
        self.runtime._conversation_memory_builder = None

        async def cancel_finalize(handle, result) -> None:
            del handle, result
            raise asyncio.CancelledError()

        self.runtime._finish_mcp_shadow_observation = cancel_finalize
        try:
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime._run_execution(request, active_task_count=0)
        finally:
            (
                self.runtime.workflow_provider,
                self.runtime.orchestration_service,
                self.runtime._conversation_memory_builder,
                self.runtime._finish_mcp_shadow_observation,
            ) = originals

    async def test_legacy_failure_without_closed_error_evidence_is_excluded(self) -> None:
        await self.runtime.storage.append_event(
            EventRecord(
                event_id="evt-generic-failure",
                conversation_id="conv-failure",
                task_id="task-failure",
                node_id="node-failure",
                event_type="mcp.tool_call_failed",
                payload={"error_type": "RuntimeError"},
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        outcome, terminal = await self.runtime._mcp_shadow_legacy_outcome(
            "task-failure",
            "node-failure",
            TaskNode(
                "node-failure",
                "task-failure",
                "mcp.legacy.search",
                status=NodeStatus.FAILED,
            ),
            excluded_fallback=ShadowOutcome.AUTHENTICATION_FAILED,
        )
        self.assertEqual(outcome, ShadowOutcome.AUTHENTICATION_FAILED)
        self.assertFalse(terminal)

    async def test_legacy_failure_uses_closed_auth_and_timeout_error_codes(self) -> None:
        for suffix, error_code, expected in (
            ("auth", "mcp_auth_required", ShadowOutcome.AUTHENTICATION_FAILED),
            ("timeout", "mcp_timeout", ShadowOutcome.TIMEOUT),
        ):
            task_id = f"task-{suffix}"
            node_id = f"node-{suffix}"
            await self.runtime.storage.append_event(
                EventRecord(
                    event_id=f"evt-{suffix}",
                    conversation_id=f"conv-{suffix}",
                    task_id=task_id,
                    node_id=node_id,
                    event_type="mcp.tool_call_failed",
                    payload={"error_code": error_code},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            outcome, terminal = await self.runtime._mcp_shadow_legacy_outcome(
                task_id,
                node_id,
                TaskNode(
                    node_id,
                    task_id,
                    "mcp.legacy.search",
                    status=NodeStatus.FAILED,
                ),
                excluded_fallback=ShadowOutcome.TOOL_CALL_SUCCEEDED,
            )
            self.assertEqual(outcome, expected)
            self.assertTrue(terminal)

    async def test_semantic_comparison_is_recorded_when_sample_persistence_fails(self) -> None:
        mapping = ApprovedVerifiedMapping(
            legacy_route="legacy-crm",
            user_server_id="user-crm",
            source_fingerprint="source-crm",
            config_fingerprint="config-v1",
            approved=True,
            verified=True,
        )
        manifest = _verified_manifest(
            config_fingerprint="config-v1",
            mapping_fingerprint=approved_shadow_mapping_set_fingerprint((mapping,)),
        )
        summary = ShadowSafeSummary(
            route="legacy-crm",
            transport="streamable_http",
            endpoint_policy="runtime_enforced",
            catalog_count=1,
            catalog_names_hmac="catalog",
            schema_fingerprints=("schema",),
            selected_tool_hmac="tool",
            schema_valid=True,
            endpoint_policy_allowed=True,
            ownership_verified=True,
            grant_exists=None,
            latency_buckets={},
            cleanup=ShadowCleanupCounts(),
        )
        task_id = "task-persist-failure"
        node_id = "node-persist-failure"
        await self.runtime.storage.append_event(
            EventRecord(
                event_id="evt-persist-failure",
                conversation_id="conv-persist-failure",
                task_id=task_id,
                node_id=node_id,
                event_type="mcp.tool_call_completed",
                payload={"output_size_bytes": 10, "truncated": False},
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

        class FailingAudit:
            async def record_shadow_sample(self, sample) -> None:
                del sample
                raise RuntimeError("storage unavailable")

        comparisons: list[ShadowComparison] = []
        gaps: list[str] = []
        originals = (
            self.runtime._mcp_shadow_manifest,
            self.runtime.user_mcp_audit_service,
            self.runtime._mcp_rollout_instance_admission,
            self.runtime._record_mcp_shadow_terminal_comparison_event,
            self.runtime._record_mcp_shadow_setup_failure,
        )

        async def capture_comparison(_request, *, comparison, **_kwargs) -> None:
            comparisons.append(comparison)

        async def capture_gap(_request, *, reason_code: str) -> None:
            gaps.append(reason_code)

        self.runtime._mcp_shadow_manifest = manifest
        self.runtime.user_mcp_audit_service = FailingAudit()
        self.runtime._mcp_rollout_instance_admission = SimpleNamespace(
            environment_id="production",
            deployment_id="deployment-1",
            stage="internal_shadow",
        )
        self.runtime._record_mcp_shadow_terminal_comparison_event = capture_comparison
        self.runtime._record_mcp_shadow_setup_failure = capture_gap
        try:
            await self.runtime._record_terminal_mcp_shadow_sample(
                handle=SimpleNamespace(
                    request=OrchestrationRequest(
                        task_id,
                        "conv-persist-failure",
                        "msg-persist-failure",
                        "search",
                    ),
                    owner_user_id="owner-1",
                    approved_mappings=(mapping,),
                ),
                context=SimpleNamespace(
                    node_id=node_id,
                    scenario=ShadowScenario.HTTPS_STREAMABLE_SUCCESS,
                    binding=SimpleNamespace(server_id="legacy-crm"),
                ),
                shadow_result=RuntimeShadowComparisonResult(
                    comparison=ShadowComparison.MISMATCHED,
                    blockers=("catalog_count_mismatch",),
                    observation=ShadowObservation(
                        outcome=ShadowOutcome.CONTROL_PLANE_READY,
                        summary=replace(summary, route="user-crm", catalog_count=2),
                    ),
                    legacy_summary=summary,
                    mapping=mapping,
                ),
                terminal_node=TaskNode(
                    node_id,
                    task_id,
                    "mcp.legacy.search",
                    status=NodeStatus.COMPLETED,
                ),
            )
        finally:
            (
                self.runtime._mcp_shadow_manifest,
                self.runtime.user_mcp_audit_service,
                self.runtime._mcp_rollout_instance_admission,
                self.runtime._record_mcp_shadow_terminal_comparison_event,
                self.runtime._record_mcp_shadow_setup_failure,
            ) = originals
        self.assertEqual(comparisons, [ShadowComparison.MISMATCHED])
        self.assertEqual(gaps, ["shadow_sample_persistence_failed"])

    async def test_shadow_observer_failure_does_not_count_as_mismatch(self) -> None:
        metric = _MetricRecorder()
        original = (
            self.runtime.mcp_shadow_observer,
            self.runtime._mcp_rollout_metric_recorder,
        )
        self.runtime.mcp_shadow_observer = _FailingShadowObserver()
        self.runtime._mcp_rollout_metric_recorder = metric
        try:
            await self.runtime._compare_and_record_mcp_shadow_route(
                request=OrchestrationRequest(
                    task_id="task-shadow-failure",
                    conversation_id="conv-shadow-failure",
                    root_message_id="msg-shadow-failure",
                    user_message="search",
                ),
                task=Task(
                    "task-shadow-failure",
                    "conv-shadow-failure",
                    "msg-shadow-failure",
                    mcp_rollout_mode="shadow",
                ),
                node_id="node-shadow-failure",
                owner_user_id="owner-1",
                profiles=(),
                binding=object(),
                server_bindings=(),
                legacy_transport="streamable_http",
                mapping_resolution=RuntimeShadowMappingResolution(
                    None, ("mapping_missing",)
                ),
                config_fingerprint="config-v1",
            )
        finally:
            (
                self.runtime.mcp_shadow_observer,
                self.runtime._mcp_rollout_metric_recorder,
            ) = original

        self.assertEqual(metric.shadow_mismatches, 0)

    async def test_shadow_uses_real_plan_and_pinned_legacy_revision_without_changing_execution(self) -> None:
        timeline: list[str] = []
        legacy_config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "legacy-crm",
                        "endpoint": "https://legacy.example.test/mcp",
                        "transport": "streamable_http",
                    }
                ],
            }
        )
        selected_binding = MCPToolBinding(
            capability_id="mcp.legacy_crm.search",
            server_id="legacy-crm",
            tool_name="search",
            input_schema={"type": "object"},
        )
        second_binding = replace(
            selected_binding,
            capability_id="mcp.legacy_crm.update",
            tool_name="update",
        )
        active_binding = replace(
            selected_binding,
            capability_id="mcp.active_only.search",
            server_id="active-only",
        )
        pinned_bundle = MCPRuntimeBundle(
            revision="pinned-revision",
            created_at=self.runtime._utcnow_naive(),
            bindings={
                selected_binding.capability_id: selected_binding,
                second_binding.capability_id: second_binding,
            },
        )
        active_bundle = MCPRuntimeBundle(
            revision="active-revision",
            created_at=self.runtime._utcnow_naive(),
            bindings={active_binding.capability_id: active_binding},
        )
        runtime_state = _PinnedLegacyRuntimeState(
            config=legacy_config,
            pinned_bundle=pinned_bundle,
            active_bundle=active_bundle,
        )
        target_id = deterministic_migrated_server_id("legacy-crm", "owner-1")
        source_fingerprint = legacy_migration_source_fingerprint(
            legacy_config.servers[0]
        )
        credential_cipher = make_credential_cipher(b"d" * 32)
        audit_signer = audit_reference_signer(b"d" * 32)
        migrated_server = UserMCPServer(
            server_id=target_id,
            owner_user_id="owner-1",
            display_name="Migrated CRM",
            routing_description="CRM",
            endpoint_url="https://legacy.example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            auth_metadata={
                "migration_provenance": {
                    "schema": "legacy_mcp_migration_provenance.v1",
                    "source_server_id": "legacy-crm",
                    "source_fingerprint": source_fingerprint,
                    "owner_user_id": "owner-1",
                    "target_server_id": target_id,
                    "credential_digest": "hmac-sha256:" + "a" * 64,
                    "credential_security_version": 1,
                    "validator_provenance": "builtin-user-mcp-health-v1",
                    "observed_at": "2026-08-13T00:00:00",
                    "expires_at": "2026-08-13T00:02:00",
                }
            },
            health_status=UserMCPHealthStatus.AVAILABLE,
            last_tested_at=datetime(2026, 8, 13, 0, 1),
        )
        credential_digest = migration_target_credential_digest(
            credential_cipher,
            audit_signer,
            server=migrated_server,
            credential_record=None,
            source_fingerprint=source_fingerprint,
        )
        assert credential_digest is not None
        migrated_server = replace(
            migrated_server,
            auth_metadata={
                "migration_provenance": {
                    **migrated_server.auth_metadata["migration_provenance"],
                    "credential_digest": credential_digest,
                }
            },
        )
        plan = WorkflowPlan(
            task_id="task-shadow",
            nodes=(
                WorkflowNodePlan(
                    node_id="legacy-node",
                    capability_id=selected_binding.capability_id,
                ),
                WorkflowNodePlan(
                    node_id="legacy-node-2",
                    capability_id=second_binding.capability_id,
                ),
            ),
        )
        request = OrchestrationRequest(
            task_id="task-shadow",
            conversation_id="conv-shadow",
            root_message_id="msg-shadow",
            user_message="search crm",
            metadata={
                "mcp_execution_mode": "legacy",
                "mcp_shadow_enabled": True,
                "mcp_rollout_config_version": "config-v1",
                "mcp_route_reason_code": "shadow_enabled",
                "mcp_rollout_mode": "shadow",
                "mcp_bundle_revision": "pinned-revision",
            },
        )
        await self.runtime.storage.save_conversation(
            Conversation("conv-shadow", "owner-1")
        )
        await self.runtime.storage.save_task(
            Task(
                "task-shadow",
                "conv-shadow",
                "msg-shadow",
                status=TaskStatus.ACCEPTED,
                mcp_execution_mode="legacy",
                mcp_shadow_enabled=True,
                mcp_rollout_config_version="config-v1",
                mcp_route_reason_code="shadow_enabled",
                mcp_rollout_mode="shadow",
            )
        )

        observer = _ShadowObserver(
            timeline,
            (ShadowComparison.MATCHED, ShadowComparison.MISMATCHED),
        )
        metric = _MetricRecorder()
        execution = _ExecutionService(timeline, self.runtime.storage)
        original = (
            self.runtime._mcp_runtime_state,
            self.runtime.user_mcp_config_service,
            self.runtime.mcp_shadow_observer,
            self.runtime.mcp_credential_cipher,
            self.runtime._mcp_audit_reference_signer,
            self.runtime._mcp_rollout_metric_recorder,
            self.runtime.workflow_provider,
            self.runtime.orchestration_service,
            self.runtime._conversation_memory_builder,
            self.runtime.user_mcp_audit_service,
            self.runtime._mcp_shadow_manifest,
            self.runtime._mcp_shadow_scenario_bindings,
            self.runtime._mcp_rollout_instance_admission,
        )
        self.runtime._mcp_runtime_state = runtime_state
        self.runtime.user_mcp_config_service = _UserConfigSource((migrated_server,))
        self.runtime.mcp_shadow_observer = observer
        self.runtime.mcp_credential_cipher = credential_cipher
        self.runtime._mcp_audit_reference_signer = audit_signer
        self.runtime._mcp_rollout_metric_recorder = metric
        self.runtime.workflow_provider = _PlanProvider(timeline, plan)
        self.runtime.orchestration_service = execution
        self.runtime._conversation_memory_builder = None
        audit_service = _CapturingAuditService(
            storage=self.runtime.storage
        )
        self.runtime.user_mcp_audit_service = audit_service
        self.runtime._mcp_shadow_manifest = _verified_manifest(
            config_fingerprint="config-v1",
            mapping_fingerprint=approved_shadow_mapping_set_fingerprint(
                (
                    resolve_approved_migration_mapping(
                        legacy_server_id="legacy-crm",
                        owner_user_id="owner-1",
                        legacy_server=legacy_config.servers[0],
                        user_servers=(migrated_server,),
                        target_credential_digests={target_id: credential_digest},
                        config_fingerprint="config-v1",
                    ).mapping,
                )
            ),
        )
        manifest_fingerprint = self.runtime._mcp_shadow_manifest.fingerprint
        self.runtime._mcp_shadow_scenario_bindings = {
            selected_binding.capability_id: ShadowScenario.HTTPS_STREAMABLE_SUCCESS,
            second_binding.capability_id: ShadowScenario.HTTPS_STREAMABLE_SUCCESS,
        }
        self.runtime._mcp_rollout_instance_admission = SimpleNamespace(
            environment_id="production",
            deployment_id="deployment-1",
            stage="internal_shadow",
        )
        gaps: list[str] = []
        original_gap = self.runtime._record_mcp_shadow_setup_failure

        async def capture_gap(_request, *, reason_code: str) -> None:
            gaps.append(reason_code)

        self.runtime._record_mcp_shadow_setup_failure = capture_gap
        try:
            await self.runtime._run_execution(request, active_task_count=0)
        finally:
            self.runtime._record_mcp_shadow_setup_failure = original_gap
            (
                self.runtime._mcp_runtime_state,
                self.runtime.user_mcp_config_service,
                self.runtime.mcp_shadow_observer,
                self.runtime.mcp_credential_cipher,
                self.runtime._mcp_audit_reference_signer,
                self.runtime._mcp_rollout_metric_recorder,
                self.runtime.workflow_provider,
                self.runtime.orchestration_service,
                self.runtime._conversation_memory_builder,
                self.runtime.user_mcp_audit_service,
                self.runtime._mcp_shadow_manifest,
                self.runtime._mcp_shadow_scenario_bindings,
                self.runtime._mcp_rollout_instance_admission,
            ) = original

        self.assertEqual(timeline.count("plan"), 1)
        self.assertEqual(timeline.count("shadow"), 2)
        self.assertEqual(timeline.count("execute"), 1)
        self.assertLess(timeline.index("plan"), timeline.index("shadow"))
        self.assertEqual(runtime_state.requested_revisions, ["pinned-revision"])
        self.assertEqual(len(observer.calls), 2)
        self.assertIs(observer.calls[0]["legacy_binding"], selected_binding)
        self.assertEqual(observer.calls[0]["mapping"].user_server_id, target_id)
        self.assertEqual(metric.shadow_mismatches, 1)
        self.assertIs(execution.calls[0][0], request)
        self.assertIs(execution.calls[0][1], plan)
        events = await self.runtime.storage.list_events_for_task("task-shadow")
        compared = [
            event
            for event in events
            if event.event_type == "mcp.rollout.shadow_compared"
        ]
        self.assertEqual(
            [event.payload["diff_category"] for event in compared],
            ["matched", "mismatched"],
        )
        samples = await self.runtime.storage.list_mcp_shadow_audit_samples(
            "production",
            "deployment-1",
            "internal_shadow",
            window_started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            window_ended_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(audit_service.errors, [])
        self.assertEqual(gaps, [])
        self.assertEqual(len(samples), 2)
        self.assertTrue(
            all(sample.manifest_fingerprint == manifest_fingerprint for sample in samples)
        )

    async def test_public_http_live_shadow_persists_matched_sample_without_tool_call(
        self,
    ) -> None:
        timeline: list[str] = []
        endpoint_url = "http://mcp.example.test/rpc"
        legacy_config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "legacy-public-http",
                        "endpoint": endpoint_url,
                        "transport": "legacy_http_sse",
                    }
                ],
            }
        )
        selected_binding = MCPToolBinding(
            capability_id="mcp.legacy_public_http.search",
            server_id="legacy-public-http",
            tool_name="search",
            input_schema={"type": "object"},
        )
        pinned_bundle = MCPRuntimeBundle(
            revision="public-http-pinned",
            created_at=self.runtime._utcnow_naive(),
            bindings={selected_binding.capability_id: selected_binding},
        )
        runtime_state = _PinnedLegacyRuntimeState(
            config=legacy_config,
            pinned_bundle=pinned_bundle,
            active_bundle=MCPRuntimeBundle(
                revision="public-http-active",
                created_at=self.runtime._utcnow_naive(),
            ),
        )

        target_id = deterministic_migrated_server_id(
            "legacy-public-http",
            "owner-public-http",
        )
        source_fingerprint = legacy_migration_source_fingerprint(
            legacy_config.servers[0]
        )
        credential_cipher = make_credential_cipher(b"e" * 32)
        audit_signer = audit_reference_signer(b"e" * 32)
        migrated_server = UserMCPServer(
            server_id=target_id,
            owner_user_id="owner-public-http",
            display_name="Migrated public HTTP",
            routing_description="Search over public HTTP",
            endpoint_url=endpoint_url,
            transport=UserMCPTransport.LEGACY_HTTP_SSE,
            auth_metadata={
                "migration_provenance": {
                    "schema": "legacy_mcp_migration_provenance.v1",
                    "source_server_id": "legacy-public-http",
                    "source_fingerprint": source_fingerprint,
                    "owner_user_id": "owner-public-http",
                    "target_server_id": target_id,
                    "credential_digest": "hmac-sha256:" + "a" * 64,
                    "credential_security_version": 1,
                    "validator_provenance": "builtin-user-mcp-health-v1",
                    "observed_at": "2026-08-13T00:00:00",
                    "expires_at": "2026-08-13T00:02:00",
                }
            },
            health_status=UserMCPHealthStatus.AVAILABLE,
            last_tested_at=datetime(2026, 8, 13, 0, 1),
        )
        credential_digest = migration_target_credential_digest(
            credential_cipher,
            audit_signer,
            server=migrated_server,
            credential_record=None,
            source_fingerprint=source_fingerprint,
        )
        self.assertIsNotNone(credential_digest)
        migrated_server = replace(
            migrated_server,
            auth_metadata={
                "migration_provenance": {
                    **migrated_server.auth_metadata["migration_provenance"],
                    "credential_digest": credential_digest,
                }
            },
        )
        await self.runtime.storage.create_user_mcp_server(migrated_server)
        await self.runtime.storage.save_user_mcp_tool_grant(
            UserMCPToolGrant(
                grant_id="grant-public-http-search",
                owner_user_id="owner-public-http",
                server_id=target_id,
                tool_name="search",
                server_security_version=1,
                input_schema_sha256="schema-sha",
                granted_at=datetime(2026, 8, 13, 0, 1),
            )
        )

        mapping_resolution = resolve_approved_migration_mapping(
            legacy_server_id="legacy-public-http",
            owner_user_id="owner-public-http",
            legacy_server=legacy_config.servers[0],
            user_servers=(migrated_server,),
            target_credential_digests={target_id: credential_digest},
            config_fingerprint="config-public-http",
        )
        self.assertIsNotNone(mapping_resolution.mapping)
        manifest = _verified_manifest(
            config_fingerprint="config-public-http",
            mapping_fingerprint=approved_shadow_mapping_set_fingerprint(
                (mapping_resolution.mapping,)
            ),
        )
        plan = WorkflowPlan(
            task_id="task-public-http",
            nodes=(
                WorkflowNodePlan(
                    node_id="node-public-http",
                    capability_id=selected_binding.capability_id,
                ),
            ),
        )
        request = OrchestrationRequest(
            task_id="task-public-http",
            conversation_id="conv-public-http",
            root_message_id="msg-public-http",
            user_message="search over the migrated server",
            metadata={
                "mcp_execution_mode": "legacy",
                "mcp_shadow_enabled": True,
                "mcp_rollout_config_version": "config-public-http",
                "mcp_route_reason_code": "shadow_enabled",
                "mcp_rollout_mode": "shadow",
                "mcp_bundle_revision": "public-http-pinned",
            },
        )
        await self.runtime.storage.save_conversation(
            Conversation("conv-public-http", "owner-public-http")
        )
        await self.runtime.storage.save_task(
            Task(
                "task-public-http",
                "conv-public-http",
                "msg-public-http",
                status=TaskStatus.ACCEPTED,
                mcp_execution_mode="legacy",
                mcp_shadow_enabled=True,
                mcp_rollout_config_version="config-public-http",
                mcp_route_reason_code="shadow_enabled",
                mcp_rollout_mode="shadow",
            )
        )

        gateway = _ReadonlyPublicHTTPGateway(server_id=target_id)
        endpoint_policy = EndpointPolicy(resolver=_PublicHTTPResolver())
        observer = MCPShadowRuntimeObserver(
            storage=self.runtime.storage,
            gateway=gateway,
            server_router=_StaticShadowRouter(target_id),
            selector=_SearchShadowSelector(),
            endpoint_policy=endpoint_policy,
            digest_key=b"public-http-shadow-digest",
        )
        execution = _ExecutionService(timeline, self.runtime.storage)
        audit_service = _CapturingAuditService(storage=self.runtime.storage)
        originals = (
            self.runtime._mcp_runtime_state,
            self.runtime.user_mcp_config_service,
            self.runtime.mcp_shadow_observer,
            self.runtime.mcp_credential_cipher,
            self.runtime._mcp_audit_reference_signer,
            self.runtime.workflow_provider,
            self.runtime.orchestration_service,
            self.runtime._conversation_memory_builder,
            self.runtime.user_mcp_audit_service,
            self.runtime._mcp_shadow_manifest,
            self.runtime._mcp_shadow_scenario_bindings,
            self.runtime._mcp_rollout_instance_admission,
        )
        self.runtime._mcp_runtime_state = runtime_state
        self.runtime.user_mcp_config_service = _UserConfigSource(
            (migrated_server,)
        )
        self.runtime.mcp_shadow_observer = observer
        self.runtime.mcp_credential_cipher = credential_cipher
        self.runtime._mcp_audit_reference_signer = audit_signer
        self.runtime.workflow_provider = _PlanProvider(timeline, plan)
        self.runtime.orchestration_service = execution
        self.runtime._conversation_memory_builder = None
        self.runtime.user_mcp_audit_service = audit_service
        self.runtime._mcp_shadow_manifest = manifest
        self.runtime._mcp_shadow_scenario_bindings = {
            selected_binding.capability_id: (
                ShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS
            )
        }
        self.runtime._mcp_rollout_instance_admission = SimpleNamespace(
            environment_id="production",
            deployment_id="deployment-public-http",
            stage="internal_shadow",
        )
        gaps: list[str] = []
        original_gap = self.runtime._record_mcp_shadow_setup_failure

        async def capture_gap(_request, *, reason_code: str) -> None:
            gaps.append(reason_code)

        self.runtime._record_mcp_shadow_setup_failure = capture_gap
        try:
            await self.runtime._run_execution(request, active_task_count=0)
        finally:
            self.runtime._record_mcp_shadow_setup_failure = original_gap
            (
                self.runtime._mcp_runtime_state,
                self.runtime.user_mcp_config_service,
                self.runtime.mcp_shadow_observer,
                self.runtime.mcp_credential_cipher,
                self.runtime._mcp_audit_reference_signer,
                self.runtime.workflow_provider,
                self.runtime.orchestration_service,
                self.runtime._conversation_memory_builder,
                self.runtime.user_mcp_audit_service,
                self.runtime._mcp_shadow_manifest,
                self.runtime._mcp_shadow_scenario_bindings,
                self.runtime._mcp_rollout_instance_admission,
            ) = originals

        samples = await self.runtime.storage.list_mcp_shadow_audit_samples(
            "production",
            "deployment-public-http",
            "internal_shadow",
            window_started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            window_ended_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(gaps, [])
        self.assertEqual(audit_service.errors, [])
        self.assertEqual(runtime_state.requested_revisions, ["public-http-pinned"])
        self.assertEqual(gateway.readonly_open_count, 1)
        self.assertEqual(gateway.close_count, 1)
        self.assertEqual(gateway.call_tool_count, 0)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].comparison, "matched")
        self.assertEqual(samples[0].blockers, ())
        self.assertEqual(samples[0].transport, "legacy_http_sse")
        self.assertEqual(
            samples[0].endpoint_policy,
            "runtime_enforced",
        )

        cleanup_task_id = "task-public-http-cleanup"
        cleanup_node_id = "node-public-http-cleanup"
        await self.runtime.storage.append_event(
            EventRecord(
                event_id="evt-public-http-cleanup",
                conversation_id="conv-public-http",
                task_id=cleanup_task_id,
                node_id=cleanup_node_id,
                event_type="mcp.tool_call_completed",
                payload={"output_size_bytes": 10, "truncated": False},
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        cleanup_gateway = _ReadonlyPublicHTTPGateway(
            server_id=target_id,
            cleanup_error=True,
        )
        cleanup_observer = MCPShadowRuntimeObserver(
            storage=self.runtime.storage,
            gateway=cleanup_gateway,
            server_router=_StaticShadowRouter(target_id),
            selector=_SearchShadowSelector(),
            endpoint_policy=endpoint_policy,
            digest_key=b"public-http-shadow-digest",
        )
        cleanup_result = await cleanup_observer.compare_task(
            owner_user_id="owner-public-http",
            task_id=cleanup_task_id,
            user_request="search over the migrated server",
            profiles=(
                UserMCPServerProfile(
                    server_id=target_id,
                    display_name=migrated_server.display_name,
                    routing_description=migrated_server.routing_description,
                    transport=migrated_server.transport.value,
                ),
            ),
            legacy_binding=selected_binding,
            legacy_server_bindings=(selected_binding,),
            legacy_transport="legacy_http_sse",
            legacy_endpoint_url=endpoint_url,
            mapping=mapping_resolution.mapping,
            config_fingerprint="config-public-http",
        )
        self.assertEqual(cleanup_result.comparison, ShadowComparison.MISMATCHED)
        self.assertEqual(
            cleanup_result.observation.outcome,
            ShadowOutcome.CLEANUP_FAILED,
        )
        self.assertIn("cleanup_incomplete", cleanup_result.blockers)

        cleanup_audit = _CapturingAuditService(storage=self.runtime.storage)
        cleanup_originals = (
            self.runtime._mcp_shadow_manifest,
            self.runtime.user_mcp_audit_service,
            self.runtime._mcp_rollout_instance_admission,
        )
        self.runtime._mcp_shadow_manifest = manifest
        self.runtime.user_mcp_audit_service = cleanup_audit
        self.runtime._mcp_rollout_instance_admission = SimpleNamespace(
            environment_id="production",
            deployment_id="deployment-public-http",
            stage="internal_shadow",
        )
        try:
            await self.runtime._record_terminal_mcp_shadow_sample(
                handle=SimpleNamespace(
                    request=OrchestrationRequest(
                        cleanup_task_id,
                        "conv-public-http",
                        "msg-public-http-cleanup",
                        "search over the migrated server",
                    ),
                    owner_user_id="owner-public-http",
                    approved_mappings=(mapping_resolution.mapping,),
                ),
                context=SimpleNamespace(
                    node_id=cleanup_node_id,
                    scenario=(
                        ShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS
                    ),
                    binding=SimpleNamespace(
                        server_id="legacy-public-http"
                    ),
                ),
                shadow_result=cleanup_result,
                terminal_node=TaskNode(
                    cleanup_node_id,
                    cleanup_task_id,
                    selected_binding.capability_id,
                    status=NodeStatus.COMPLETED,
                ),
            )
        finally:
            (
                self.runtime._mcp_shadow_manifest,
                self.runtime.user_mcp_audit_service,
                self.runtime._mcp_rollout_instance_admission,
            ) = cleanup_originals

        persisted = await self.runtime.storage.list_mcp_shadow_audit_samples(
            "production",
            "deployment-public-http",
            "internal_shadow",
            window_started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            window_ended_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        cleanup_samples = [
            sample
            for sample in persisted
            if sample.shadow_outcome == ShadowOutcome.CLEANUP_FAILED.value
        ]
        self.assertEqual(cleanup_audit.errors, [])
        self.assertEqual(len(cleanup_samples), 1)
        self.assertEqual(cleanup_samples[0].comparison, "mismatched")
        self.assertIn("cleanup_incomplete", cleanup_samples[0].blockers)
        self.assertEqual(cleanup_gateway.call_tool_count, 0)

    async def test_non_mcp_plan_does_not_start_shadow_observer(self) -> None:
        request = OrchestrationRequest(
            task_id="task-no-mcp",
            conversation_id="conv-no-mcp",
            root_message_id="msg-no-mcp",
            user_message="hello",
            metadata={
                "mcp_execution_mode": "legacy",
                "mcp_shadow_enabled": True,
                "mcp_rollout_config_version": "config-v1",
                "mcp_route_reason_code": "shadow_enabled",
                "mcp_rollout_mode": "shadow",
                "mcp_bundle_revision": "pinned-revision",
            },
        )
        plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id="respond",
                    capability_id="main_agent.respond",
                ),
            ),
        )
        runtime_state = _PinnedLegacyRuntimeState(
            config=MCPRuntimeConfig.from_mapping({"enabled": False}),
            pinned_bundle=MCPRuntimeBundle(
                revision="pinned-revision",
                created_at=self.runtime._utcnow_naive(),
            ),
            active_bundle=MCPRuntimeBundle(
                revision="active-revision",
                created_at=self.runtime._utcnow_naive(),
            ),
        )
        observer = _ShadowObserver([], ShadowComparison.MATCHED)
        await self.runtime.storage.save_conversation(
            Conversation("conv-no-mcp", "owner-1")
        )
        await self.runtime.storage.save_task(
            Task(
                "task-no-mcp",
                "conv-no-mcp",
                "msg-no-mcp",
                mcp_execution_mode="legacy",
                mcp_shadow_enabled=True,
                mcp_rollout_config_version="config-v1",
                mcp_route_reason_code="shadow_enabled",
                mcp_rollout_mode="shadow",
            )
        )
        original = (
            self.runtime._mcp_runtime_state,
            self.runtime.user_mcp_config_service,
            self.runtime.mcp_shadow_observer,
        )
        self.runtime._mcp_runtime_state = runtime_state
        self.runtime.user_mcp_config_service = _UserConfigSource(())
        self.runtime.mcp_shadow_observer = observer
        try:
            handle = await self.runtime._begin_mcp_shadow_observation(
                request=request,
                plan=plan,
            )
        finally:
            (
                self.runtime._mcp_runtime_state,
                self.runtime.user_mcp_config_service,
                self.runtime.mcp_shadow_observer,
            ) = original

        self.assertEqual(observer.calls, [])
        self.assertIsNone(handle)
