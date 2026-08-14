from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime

from src.capabilities.mcp_dispatch.models import (
    MCPSelectorAction,
    MCPSelectorActionType,
    MCPServerRouteAction,
    MCPServerRouteActionType,
)
from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import (
    Conversation,
    InterruptAnswer,
    MCPBranchRecord,
    MCPCallRecord,
    Task,
    UserMCPServer,
    UserMCPToolGrant,
)
from src.integrations.mcp.dispatch_coordinator import (
    EXTERNAL_CONTENT_NOTICE,
    MCPDispatchMetricContext,
    UserMCPDispatchCoordinator as _UserMCPDispatchCoordinator,
    build_mcp_call_fingerprint,
)
from tests.master_key_support import audit_reference_signer
from src.integrations.mcp.gateway import MCPGatewayError
from src.integrations.mcp.client import MCPRemoteError
from src.integrations.mcp.gateway_models import (
    MCPCallOutcome,
    MCPCallOutcomeKind,
    MCPTaskServerScope,
    MCPToolDescriptor,
    ToolCatalogSnapshot,
)
from src.integrations.mcp.rollout_evidence import (
    MCPCallKind,
    MCPMetricAdapter,
    MCPMetricErrorCategory,
    MCPMetricExecutionPath,
    MCPMetricName,
    MCPMetricProtocolVersion,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
    MCPMetricTransport,
    MCPSafetyRedLine,
)


NOW = datetime(2026, 8, 12, 12, 0, 0)


def UserMCPDispatchCoordinator(**kwargs):
    kwargs.setdefault("audit_reference_signer", audit_reference_signer(b"a" * 32))
    return _UserMCPDispatchCoordinator(**kwargs)


class _SequenceSelector:
    def __init__(self, *actions: MCPSelectorAction) -> None:
        self.actions = list(actions)
        self.contexts = []

    async def select(self, context):
        self.contexts.append(context)
        if not self.actions:
            return MCPSelectorAction(MCPSelectorActionType.FINISH, reason="done")
        return self.actions.pop(0)


class _FakeGateway:
    def __init__(self, *outcomes: MCPCallOutcome | BaseException) -> None:
        self.outcomes = list(outcomes) or [MCPCallOutcome.completed("result-safe")]
        self.opened: list[tuple[str, str, str]] = []
        self.closed: list[str] = []
        self.listed: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.callback_order: list[str] = []
        self.catalog = ToolCatalogSnapshot(
            server_id="server-a",
            effective_protocol_version="2026-07-28",
            tools=(
                MCPToolDescriptor(
                    name="lookup",
                    description="Lookup a record",
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                    input_schema_sha256="schema-v1",
                ),
            ),
        )

    async def open_scope(
        self,
        principal,
        platform_task_id: str,
        server_id: str,
        **_callbacks,
    ):
        self.opened.append((principal.username, platform_task_id, server_id))
        return MCPTaskServerScope(
            "scope-a", principal.username, platform_task_id, server_id, 1, 1
        )

    async def list_tools(self, scope):
        self.listed.append(scope.server_id)
        return replace(self.catalog, server_id=scope.server_id)

    async def call_tool(self, scope, tool_name, arguments, callbacks, **kwargs):
        call_ref = f"safe-call-{len(self.calls) + 1}"
        await callbacks.on_created(call_ref)
        self.callback_order.append("reserved")
        await callbacks.on_registered(call_ref)
        self.callback_order.append("registered")
        if callbacks.on_heartbeat is not None:
            await callbacks.on_heartbeat(call_ref)
        self.calls.append((tool_name, dict(arguments), dict(kwargs)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close_scope(self, scope, reason: str):
        del reason
        self.closed.append(scope.scope_id)


class _FakeMetricRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.counts = []
        self.latencies = []

    async def record_count(self, metric_name, **kwargs):
        if self.fail:
            raise RuntimeError("Bearer secret-metric-error")
        self.counts.append((metric_name, kwargs))

    async def record_latency(self, metric_name, **kwargs):
        if self.fail:
            raise RuntimeError("Bearer secret-metric-error")
        self.latencies.append((metric_name, kwargs))


class _SafetyDetector:
    def __init__(self) -> None:
        self.violations = []

    async def report_violation(self, **kwargs) -> None:
        self.violations.append(kwargs)


class _PreDispatchFailureGateway(_FakeGateway):
    async def call_tool(self, scope, tool_name, arguments, callbacks, **kwargs):
        del scope, tool_name, arguments, callbacks, kwargs
        raise MCPGatewayError("mcp_transport_failed")


class _DiscoveryFailureGateway(_FakeGateway):
    async def open_scope(self, *args, **kwargs):
        del args, kwargs
        raise MCPGatewayError("mcp_transport_failed")


class _FakeStorage:
    def __init__(self) -> None:
        self.conversation = Conversation("conv-a", "alice")
        self.task = Task("task-a", "conv-a", "message-a")
        self.servers = {
            "server-a": UserMCPServer(
                server_id="server-a",
                owner_user_id="alice",
                display_name="CRM",
                routing_description="Lookup CRM records",
                endpoint_url="https://secret.invalid/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=NOW,
                updated_at=NOW,
            )
        }
        self.branches: dict[str, MCPBranchRecord] = {}
        self.calls: dict[str, MCPCallRecord] = {}
        self.grants: list[UserMCPToolGrant] = []
        self.interrupts = []
        self.answers: dict[str, list[InterruptAnswer]] = {}
        self.lifecycle: list[str] = []

    async def delete_mcp_sealed_state(self, owner_user_id, task_id, sealed_state_ref):
        del owner_user_id, task_id, sealed_state_ref
        return True

    async def get_task(self, task_id):
        return self.task if task_id == self.task.task_id else None

    async def get_conversation(self, conversation_id):
        return self.conversation if conversation_id == self.conversation.conversation_id else None

    async def get_user_mcp_server(self, owner_user_id, server_id):
        server = self.servers.get(server_id)
        return server if server and server.owner_user_id == owner_user_id else None

    async def list_user_mcp_servers(self, owner_user_id):
        return [server for server in self.servers.values() if server.owner_user_id == owner_user_id]

    async def save_mcp_branch_record(self, record):
        self.branches[record.branch_id] = record
        return record

    async def get_mcp_branch_record(self, owner_user_id, task_id, branch_id):
        record = self.branches.get(branch_id)
        if record and (record.owner_user_id, record.task_id) == (owner_user_id, task_id):
            return record
        return None

    async def list_mcp_call_records(self, owner_user_id, task_id, *, branch_id=None):
        return sorted(
            [
                call
                for call in self.calls.values()
                if call.owner_user_id == owner_user_id
                and call.task_id == task_id
                and (branch_id is None or call.branch_id == branch_id)
            ],
            key=lambda call: call.call_sequence,
        )

    async def reserve_mcp_call(self, record):
        branch = self.branches.get(record.branch_id)
        if (
            branch is None
            or branch.active_call_ref is not None
            or branch.tool_call_count >= branch.max_tool_calls
            or record.call_sequence != branch.tool_call_count + 1
        ):
            return False
        self.calls[record.call_ref] = record
        self.branches[branch.branch_id] = replace(
            branch,
            status="active",
            tool_call_count=record.call_sequence,
            active_call_ref=record.call_ref,
            updated_at=record.updated_at,
        )
        self.lifecycle.append("reserve")
        return True

    async def mark_mcp_call_may_have_dispatched(
        self, owner_user_id, task_id, call_ref, *, updated_at
    ):
        call = self.calls.get(call_ref)
        if call is None or (call.owner_user_id, call.task_id) != (owner_user_id, task_id):
            return False
        self.calls[call_ref] = replace(
            call, may_have_dispatched=True, status="active", updated_at=updated_at
        )
        self.lifecycle.append("registered")
        return True

    async def finish_mcp_call(
        self,
        owner_user_id,
        task_id,
        call_ref,
        *,
        status,
        terminal_at,
        result_ref=None,
        output_size_bytes=None,
        safe_error_code=None,
    ):
        call = self.calls.get(call_ref)
        if call is None or call.terminal_at is not None:
            return None
        saved = replace(
            call,
            status=status,
            result_ref=result_ref,
            output_size_bytes=output_size_bytes,
            safe_error_code=safe_error_code,
            updated_at=terminal_at,
            terminal_at=terminal_at,
        )
        self.calls[call_ref] = saved
        branch = self.branches[call.branch_id]
        self.branches[call.branch_id] = replace(
            branch, active_call_ref=None, updated_at=terminal_at
        )
        self.lifecycle.append("finish")
        return saved

    async def get_valid_user_mcp_tool_grant(
        self,
        owner_user_id,
        server_id,
        tool_name,
        *,
        server_security_version,
        input_schema_sha256,
    ):
        return next(
            (
                grant
                for grant in self.grants
                if grant.owner_user_id == owner_user_id
                and grant.server_id == server_id
                and grant.tool_name == tool_name
                and grant.server_security_version == server_security_version
                and grant.input_schema_sha256 == input_schema_sha256
                and grant.invalidated_at is None
            ),
            None,
        )

    async def save_user_mcp_tool_grant(self, grant):
        self.grants.append(grant)
        return grant

    async def save_interrupt(self, interrupt):
        self.interrupts.append(interrupt)
        return interrupt

    async def list_interrupts_for_task(self, task_id):
        return [interrupt for interrupt in self.interrupts if interrupt.task_id == task_id]

    async def list_interrupt_answers(self, interrupt_id):
        return self.answers.get(interrupt_id, [])


class _RouteSecondServer:
    async def route(self, **kwargs):
        self.kwargs = kwargs
        return MCPServerRouteAction(
            MCPServerRouteActionType.ROUTE_SERVER, server_id="server-b"
        )


def _request(*, conversation_id: str = "conv-a") -> CapabilityExecutionRequest:
    return CapabilityExecutionRequest(
        capability_id="mcp.dispatch",
        conversation_id=conversation_id,
        task_id="task-a",
        node_id="node-a",
        input_payload={"server_id": "server-a"},
        metadata={"user_message": "Find Alice's CRM record"},
    )


def _call(arguments=None) -> MCPSelectorAction:
    return MCPSelectorAction(
        MCPSelectorActionType.CALL_TOOL,
        tool_name="lookup",
        arguments=arguments or {"query": "Alice"},
    )


class UserMCPDispatchCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_durable_active_call_conflict_records_dual_call_red_line(self) -> None:
        storage = _FakeStorage()
        branch = MCPBranchRecord(
            branch_id="branch-active",
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            status="active",
            initial_server_id="server-a",
            tool_call_count=1,
            active_call_ref="active-call",
            created_at=NOW,
            updated_at=NOW,
        )
        await storage.save_mcp_branch_record(branch)
        storage.calls["active-call"] = MCPCallRecord(
            call_ref="active-call",
            branch_id=branch.branch_id,
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            server_id="server-a",
            tool_name="lookup",
            status="reserved",
            call_sequence=1,
            arguments_sha256="existing-fingerprint",
            server_security_version=1,
            input_schema_sha256="schema-v1",
            protocol_version="2026-07-28",
            created_at=NOW,
            updated_at=NOW,
        )
        detector = _SafetyDetector()
        gateway = _FakeGateway()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=_SequenceSelector(),
            safety_detectors={MCPSafetyRedLine.DUAL_TOOL_CALL: detector},
        )
        fingerprint = build_mcp_call_fingerprint(
            server_id="server-a",
            tool_name="lookup",
            arguments={"query": "Alice"},
        )

        with self.assertRaisesRegex(RuntimeError, "mcp_call_budget_or_concurrency_exhausted"):
            await coordinator._call_tool(
                _request(),
                branch=branch,
                server=storage.servers["server-a"],
                scope=MCPTaskServerScope("scope-a", "alice", "task-a", "server-a", 1, 1),
                tool_name="lookup",
                tool_display_name="Lookup",
                arguments={"query": "Alice"},
                input_schema_sha256="schema-v1",
                protocol_version="2026-07-28",
                fingerprint=fingerprint,
                events=[],
                selector_step=1,
            )

        self.assertEqual(gateway.calls, [])
        self.assertEqual(
            detector.violations,
            [{"reason_code": "call_idempotency_conflict"}],
        )

    async def test_unknown_may_have_dispatched_call_cannot_be_replayed(self) -> None:
        storage = _FakeStorage()
        branch = MCPBranchRecord(
            branch_id="branch-unknown",
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            status="active",
            initial_server_id="server-a",
            tool_call_count=1,
            created_at=NOW,
            updated_at=NOW,
        )
        await storage.save_mcp_branch_record(branch)
        fingerprint = build_mcp_call_fingerprint(
            server_id="server-a",
            tool_name="lookup",
            arguments={"query": "Alice"},
        )
        storage.calls["unknown-call"] = MCPCallRecord(
            call_ref="unknown-call",
            branch_id=branch.branch_id,
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            server_id="server-a",
            tool_name="lookup",
            status="unknown",
            call_sequence=1,
            arguments_sha256=fingerprint,
            server_security_version=1,
            input_schema_sha256="schema-v1",
            protocol_version="2026-07-28",
            may_have_dispatched=True,
            created_at=NOW,
            updated_at=NOW,
            terminal_at=NOW,
        )
        detector = _SafetyDetector()
        gateway = _FakeGateway()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=_SequenceSelector(),
            safety_detectors={MCPSafetyRedLine.UNKNOWN_RESULT_REPLAY: detector},
        )

        with self.assertRaisesRegex(RuntimeError, "mcp_unknown_result_replay_forbidden"):
            await coordinator._call_tool(
                _request(),
                branch=branch,
                server=storage.servers["server-a"],
                scope=MCPTaskServerScope("scope-a", "alice", "task-a", "server-a", 1, 1),
                tool_name="lookup",
                tool_display_name="Lookup",
                arguments={"query": "Alice"},
                input_schema_sha256="schema-v1",
                protocol_version="2026-07-28",
                fingerprint=fingerprint,
                events=[],
                selector_step=1,
            )

        self.assertEqual(gateway.calls, [])
        self.assertEqual(
            detector.violations,
            [{"reason_code": "unknown_replay_blocked"}],
        )

    async def test_derives_owner_from_task_conversation_and_rejects_spoofed_request(self) -> None:
        storage = _FakeStorage()
        gateway = _FakeGateway()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage, gateway=gateway, selector=_SequenceSelector()
        )

        outcome = await coordinator.dispatch(_request(conversation_id="conv-spoofed"), server_id="server-a")

        self.assertEqual(outcome.error.code, "mcp_task_not_found")
        self.assertEqual(gateway.opened, [])

    async def test_rejects_disabled_or_unavailable_server_before_network(self) -> None:
        storage = _FakeStorage()
        storage.servers["server-a"] = replace(storage.servers["server-a"], enabled=False)
        gateway = _FakeGateway()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage, gateway=gateway, selector=_SequenceSelector()
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.error.code, "mcp_server_not_available")
        self.assertEqual(gateway.opened, [])

    async def test_records_failed_server_discovery_duration(self) -> None:
        recorder = _FakeMetricRecorder()
        coordinator = UserMCPDispatchCoordinator(
            storage=_FakeStorage(),
            gateway=_DiscoveryFailureGateway(),
            selector=_SequenceSelector(),
            metric_recorder=recorder,
            metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.error.code, "mcp_transport_failed")
        discovery = next(
            kwargs
            for name, kwargs in recorder.latencies
            if name is MCPMetricName.SERVER_DISCOVER_DURATION_SECONDS
        )
        self.assertEqual(
            discovery["labels"].result_category,
            MCPMetricResultCategory.FAILED,
        )
        self.assertEqual(
            discovery["labels"].error_category,
            MCPMetricErrorCategory.TRANSPORT,
        )

    async def test_missing_grant_returns_safe_approval_interrupt_without_calling_tool(self) -> None:
        storage = _FakeStorage()
        gateway = _FakeGateway()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage, gateway=gateway, selector=_SequenceSelector(_call())
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.output_payload["mcp_status"], "approval_required")
        self.assertEqual(outcome.interrupt.reason_code, "mcp_tool_approval_required")
        self.assertNotIn("endpoint", str(outcome.interrupt.required_fields).lower())
        self.assertNotIn("Alice", str(outcome.interrupt.required_fields))
        self.assertEqual(gateway.calls, [])
        self.assertEqual(len(gateway.closed), 1)
        approval_event = next(
            event for event in outcome.events if event.event_type == "mcp.tool_approval_required"
        )
        self.assertEqual(approval_event.payload["interrupt_id"], outcome.interrupt.interrupt_id)
        self.assertEqual(approval_event.payload["server_display_name"], "CRM")
        self.assertEqual(approval_event.payload["tool_display_name"], "lookup")
        self.assertEqual(len(approval_event.payload["safe_call_ref"]), 64)
        self.assertNotIn("mcp-approval-call-", approval_event.payload["safe_call_ref"])
        routed = next(event for event in outcome.events if event.event_type == "mcp.server_routed")
        discovery = next(
            event for event in outcome.events if event.event_type == "mcp.discovery_completed"
        )
        self.assertEqual(routed.payload, {"server_display_name": "CRM"})
        self.assertEqual(
            discovery.payload, {"server_display_name": "CRM", "tool_count": 1}
        )

    async def test_resumes_accepted_always_allow_and_persists_exact_grant_and_call_barriers(self) -> None:
        storage = _FakeStorage()
        gateway = _FakeGateway(MCPCallOutcome.completed("result-safe", byte_size=17))
        first = UserMCPDispatchCoordinator(
            storage=storage, gateway=gateway, selector=_SequenceSelector(_call())
        )
        pending = await first.dispatch(_request(), server_id="server-a")
        storage.answers[pending.interrupt.interrupt_id] = [
            InterruptAnswer(
                "answer-a",
                pending.interrupt.interrupt_id,
                {"mcp_tool_approval": "always_allow"},
                accepted=True,
                created_at=NOW,
                accepted_at=NOW,
            )
        ]
        selector = _SequenceSelector(
            _call(), MCPSelectorAction(MCPSelectorActionType.FINISH, reason="CRM lookup completed")
        )
        coordinator = UserMCPDispatchCoordinator(
            storage=storage, gateway=gateway, selector=selector
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.output_payload["mcp_status"], "completed")
        self.assertEqual(outcome.output_payload["result_ref"], "result-safe")
        self.assertEqual(outcome.output_payload["external_content_notice"], EXTERNAL_CONTENT_NOTICE)
        self.assertEqual(len(storage.grants), 1)
        grant = storage.grants[0]
        self.assertEqual(
            (
                grant.owner_user_id,
                grant.server_id,
                grant.tool_name,
                grant.server_security_version,
                grant.input_schema_sha256,
            ),
            ("alice", "server-a", "lookup", 1, "schema-v1"),
        )
        call = next(iter(storage.calls.values()))
        self.assertTrue(call.may_have_dispatched)
        self.assertEqual(call.status, "completed")
        self.assertEqual(call.result_ref, "result-safe")
        self.assertEqual(call.input_field_names, ("query",))
        self.assertEqual(storage.lifecycle[0:2], ["reserve", "registered"])
        self.assertEqual(gateway.listed[-1:], ["server-a"])

    async def test_live_recorder_gets_started_after_reserve_and_heartbeat_only(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        gateway = _FakeGateway(MCPCallOutcome.completed("result-safe"))
        live_events = []

        async def record(event):
            live_events.append(event)

        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=_SequenceSelector(
                _call(), MCPSelectorAction(MCPSelectorActionType.FINISH, reason="done")
            ),
            live_event_recorder=record,
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(
            [event.event_type for event in live_events],
            ["mcp.tool_call_started", "mcp.tool_call_still_running"],
        )
        self.assertEqual(storage.lifecycle[0], "reserve")
        self.assertEqual(len(live_events[0].payload["safe_call_ref"]), 64)
        self.assertNotEqual(live_events[0].payload["safe_call_ref"], "safe-call-1")
        self.assertEqual(live_events[0].payload["server_display_name"], "CRM")
        self.assertEqual(live_events[0].payload["tool_display_name"], "lookup")
        self.assertNotIn(
            "mcp.tool_call_started", [event.event_type for event in outcome.events]
        )

    async def test_allow_once_answer_cannot_authorize_a_second_identical_remote_call(self) -> None:
        storage = _FakeStorage()
        gateway = _FakeGateway(MCPCallOutcome.completed("result-safe"))
        pending = await UserMCPDispatchCoordinator(
            storage=storage, gateway=gateway, selector=_SequenceSelector(_call())
        ).dispatch(_request(), server_id="server-a")
        storage.answers[pending.interrupt.interrupt_id] = [
            InterruptAnswer(
                "answer-once",
                pending.interrupt.interrupt_id,
                {"mcp_tool_approval": "allow_once"},
                accepted=True,
                created_at=NOW,
                accepted_at=NOW,
            )
        ]
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=_SequenceSelector(_call(), _call()),
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.output_payload["mcp_status"], "approval_required")
        self.assertNotEqual(outcome.interrupt.interrupt_id, pending.interrupt.interrupt_id)
        self.assertEqual(gateway.calls[0][:2], ("lookup", {"query": "Alice"}))
        self.assertEqual(storage.grants, [])

    async def test_persistent_budget_stops_twenty_first_remote_call(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        gateway = _FakeGateway(MCPCallOutcome.completed("result-one"))
        selector = _SequenceSelector(_call({"query": "first"}), _call({"query": "second"}))
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=selector,
            max_tool_calls=1,
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.error.code, "mcp_call_budget_or_concurrency_exhausted")
        self.assertEqual(gateway.calls[0][:2], ("lookup", {"query": "first"}))
        branch = next(iter(storage.branches.values()))
        self.assertEqual(branch.tool_call_count, 1)
        self.assertEqual(gateway.opened, [("alice", "task-a", "server-a")])

    async def test_route_another_server_uses_only_available_owner_scoped_profiles(self) -> None:
        storage = _FakeStorage()
        storage.servers["server-b"] = replace(
            storage.servers["server-a"],
            server_id="server-b",
            display_name="ERP",
            routing_description="Lookup orders",
        )
        selector = _SequenceSelector(
            MCPSelectorAction(MCPSelectorActionType.ROUTE_ANOTHER_SERVER),
            MCPSelectorAction(MCPSelectorActionType.FINISH, reason="routed safely"),
        )
        gateway = _FakeGateway()
        router = _RouteSecondServer()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=selector,
            server_router=router,
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.output_payload["mcp_status"], "completed")
        self.assertEqual(
            gateway.opened,
            [("alice", "task-a", "server-a"), ("alice", "task-a", "server-b")],
        )
        self.assertEqual(
            [profile.server_id for profile in router.kwargs["remaining_servers"]],
            ["server-b"],
        )

    async def test_input_required_and_remote_task_results_expose_only_safe_references(self) -> None:
        cases = (
            (
                MCPCallOutcome.input_required(({"message": "enter value"},), "sealed-safe"),
                "input_required",
                "sealed_request_state_ref",
                "sealed-safe",
            ),
            (
                MCPCallOutcome.task_created("remote-safe", status="working", next_poll_at="later"),
                "remote_task_created",
                "safe_remote_task_ref",
                "remote-safe",
            ),
        )
        for remote_outcome, status, key, value in cases:
            with self.subTest(status=status):
                storage = _FakeStorage()
                storage.grants.append(
                    UserMCPToolGrant(
                        "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
                    )
                )
                gateway = _FakeGateway(remote_outcome)
                coordinator = UserMCPDispatchCoordinator(
                    storage=storage, gateway=gateway, selector=_SequenceSelector(_call())
                )

                outcome = await coordinator.dispatch(_request(), server_id="server-a")

                self.assertEqual(outcome.output_payload["mcp_status"], status)
                self.assertEqual(outcome.output_payload[key], value)
                event_types = {event.event_type for event in outcome.events}
                expected_event = (
                    "mcp.input_required"
                    if status == "input_required"
                    else "mcp.remote_task_status_changed"
                )
                self.assertIn(expected_event, event_types)
                self.assertNotIn("mcp.tool_call_completed", event_types)
                serialized = str(outcome.output_payload)
                self.assertNotIn("remote_task_id", serialized)
                self.assertNotIn("requestState", serialized)
                self.assertNotIn("https://secret.invalid", serialized)
                if status == "input_required":
                    self.assertIsNotNone(outcome.interrupt)
                    self.assertEqual(outcome.interrupt.reason_code, "mcp_input_required")
                    self.assertEqual(
                        outcome.interrupt.required_fields["sealed_request_state_ref"],
                        "sealed-safe",
                    )
                    self.assertEqual(gateway.calls[0][2]["node_id"], "node-a")

    async def test_mrtr_resume_uses_durable_safe_ref_and_answer_without_replanning_path(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        gateway = _FakeGateway(
            MCPCallOutcome.input_required(({"message": "confirm"},), "sealed-safe"),
            MCPCallOutcome.completed("result-safe"),
        )
        selector = _SequenceSelector(
            _call({"query": "Alice"}),
            _call({"query": "Alice"}),
            MCPSelectorAction(MCPSelectorActionType.FINISH, reason="done"),
        )
        coordinator = UserMCPDispatchCoordinator(
            storage=storage, gateway=gateway, selector=selector
        )

        pending = await coordinator.dispatch(_request(), server_id="server-a")
        storage.answers[pending.interrupt.interrupt_id] = [
            InterruptAnswer(
                "answer-input",
                pending.interrupt.interrupt_id,
                {"mcp_input_responses": {"confirm": {"approved": True}}},
                accepted=True,
                created_at=NOW,
                accepted_at=NOW,
            )
        ]
        resumed_request = replace(
            _request(),
            metadata={
                "user_message": "Find Alice's CRM record",
                "mcp_input_responses": {"confirm": {"approved": True}},
            },
        )

        completed = await coordinator.dispatch(resumed_request, server_id="server-a")

        self.assertEqual(completed.output_payload["mcp_status"], "completed")
        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(gateway.calls[1][2]["sealed_request_state_ref"], "sealed-safe")
        self.assertEqual(
            gateway.calls[1][2]["input_responses"],
            {"confirm": {"approved": True}},
        )

    async def test_ambiguous_mrtr_continuation_is_never_replayed(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        gateway = _FakeGateway(
            MCPCallOutcome.input_required(({"message": "confirm"},), "sealed-safe"),
            TimeoutError("ambiguous after dispatch"),
            MCPCallOutcome.completed("must-not-be-reached"),
        )
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=_SequenceSelector(
                _call({"query": "Alice"}),
                _call({"query": "Alice"}),
                _call({"query": "Alice"}),
            ),
        )
        pending = await coordinator.dispatch(_request(), server_id="server-a")
        storage.answers[pending.interrupt.interrupt_id] = [
            InterruptAnswer(
                "answer-input-ambiguous",
                pending.interrupt.interrupt_id,
                {"mcp_input_responses": {"confirm": {"approved": True}}},
                accepted=True,
                created_at=NOW,
                accepted_at=NOW,
            )
        ]
        resumed = replace(
            _request(),
            metadata={
                "user_message": "Find Alice's CRM record",
                "mcp_input_responses": {"confirm": {"approved": True}},
            },
        )
        with self.assertRaises(TimeoutError):
            await coordinator.dispatch(resumed, server_id="server-a")

        retried = await coordinator.dispatch(resumed, server_id="server-a")

        self.assertEqual(retried.error.code, "mcp_unknown_result_replay_forbidden")
        self.assertEqual(len(gateway.calls), 2)

    async def test_mrtr_resume_rejects_metadata_that_differs_from_accepted_answer(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        gateway = _FakeGateway(
            MCPCallOutcome.input_required(({"message": "confirm"},), "sealed-safe")
        )
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=gateway,
            selector=_SequenceSelector(_call({"query": "Alice"}), _call({"query": "Alice"})),
        )
        pending = await coordinator.dispatch(_request(), server_id="server-a")
        storage.answers[pending.interrupt.interrupt_id] = [
            InterruptAnswer(
                "answer-input",
                pending.interrupt.interrupt_id,
                {"mcp_input_responses": {"confirm": {"approved": True}}},
                accepted=True,
                created_at=NOW,
                accepted_at=NOW,
            )
        ]

        outcome = await coordinator.dispatch(
            replace(
                _request(),
                metadata={
                    "user_message": "Find Alice's CRM record",
                    "mcp_input_responses": {"confirm": {"approved": False}},
                },
            ),
            server_id="server-a",
        )

        self.assertEqual(outcome.error.code, "mcp_input_responses_invalid")
        self.assertEqual(len(gateway.calls), 1)

    async def test_records_only_real_terminal_tool_call_metrics_with_closed_labels(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        recorder = _FakeMetricRecorder()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_FakeGateway(MCPCallOutcome.completed("result-safe")),
            selector=_SequenceSelector(
                _call(), MCPSelectorAction(MCPSelectorActionType.FINISH, reason="done")
            ),
            metric_recorder=recorder,
            metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.output_payload["mcp_status"], "completed")
        self.assertEqual(
            [item[0] for item in recorder.counts],
            [
                MCPMetricName.PERMISSION_DECISIONS_TOTAL,
                MCPMetricName.TOOL_CALLS_TOTAL,
            ],
        )
        self.assertEqual(
            [item[0] for item in recorder.latencies],
            [
                MCPMetricName.SERVER_DISCOVER_DURATION_SECONDS,
                MCPMetricName.TOOL_CALL_DURATION_SECONDS,
            ],
        )
        labels = recorder.counts[-1][1]["labels"]
        self.assertEqual(labels.execution_path, MCPMetricExecutionPath.USER_SCOPED)
        self.assertEqual(labels.routing_mode, MCPMetricRoutingMode.ENFORCE)
        self.assertEqual(labels.transport, MCPMetricTransport.STREAMABLE_HTTP)
        self.assertEqual(labels.protocol_version, MCPMetricProtocolVersion.V2026_07_28)
        self.assertEqual(labels.adapter, MCPMetricAdapter.PYTHON_2026)
        self.assertEqual(labels.result_category, MCPMetricResultCategory.SUCCEEDED)
        self.assertEqual(labels.error_category, MCPMetricErrorCategory.NONE)
        self.assertEqual(labels.call_kind, MCPCallKind.ORDINARY)

    async def test_nonterminal_protocol_outcomes_do_not_enter_terminal_metrics(self) -> None:
        for gateway_outcome in (
            MCPCallOutcome.input_required(({"message": "confirm"},), "sealed-safe"),
            MCPCallOutcome.task_created("remote-safe", status="working"),
        ):
            with self.subTest(kind=gateway_outcome.kind):
                storage = _FakeStorage()
                storage.grants.append(
                    UserMCPToolGrant(
                        "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
                    )
                )
                recorder = _FakeMetricRecorder()
                coordinator = UserMCPDispatchCoordinator(
                    storage=storage,
                    gateway=_FakeGateway(gateway_outcome),
                    selector=_SequenceSelector(_call()),
                    metric_recorder=recorder,
                    metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
                )

                await coordinator.dispatch(_request(), server_id="server-a")

                self.assertNotIn(
                    MCPMetricName.TOOL_CALLS_TOTAL,
                    [item[0] for item in recorder.counts],
                )
                self.assertNotIn(
                    MCPMetricName.TOOL_CALL_DURATION_SECONDS,
                    [item[0] for item in recorder.latencies],
                )
                self.assertEqual(
                    MCPMetricName.MRTR_ROUNDS_TOTAL in {
                        item[0] for item in recorder.counts
                    },
                    gateway_outcome.kind is MCPCallOutcomeKind.INPUT_REQUIRED,
                )

    async def test_permission_metrics_cover_prompt_and_denial_without_remote_call(
        self,
    ) -> None:
        storage = _FakeStorage()
        recorder = _FakeMetricRecorder()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_FakeGateway(),
            selector=_SequenceSelector(_call()),
            metric_recorder=recorder,
            metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
        )

        pending = await coordinator.dispatch(_request(), server_id="server-a")

        prompt_labels = next(
            kwargs["labels"]
            for metric, kwargs in recorder.counts
            if metric is MCPMetricName.PERMISSION_DECISIONS_TOTAL
        )
        self.assertEqual(
            prompt_labels.result_category,
            MCPMetricResultCategory.INPUT_REQUIRED,
        )
        storage.answers[pending.interrupt.interrupt_id] = [
            InterruptAnswer(
                "answer-deny",
                pending.interrupt.interrupt_id,
                {"mcp_tool_approval": "deny"},
                accepted=True,
                created_at=NOW,
                accepted_at=NOW,
            )
        ]
        denial_recorder = _FakeMetricRecorder()
        denied = await UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_FakeGateway(),
            selector=_SequenceSelector(_call()),
            metric_recorder=denial_recorder,
            metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
        ).dispatch(_request(), server_id="server-a")

        self.assertEqual(denied.output_payload["mcp_status"], "stopped")
        denial_labels = next(
            kwargs["labels"]
            for metric, kwargs in denial_recorder.counts
            if metric is MCPMetricName.PERMISSION_DECISIONS_TOTAL
        )
        self.assertEqual(
            denial_labels.result_category,
            MCPMetricResultCategory.PERMISSION_DENIED,
        )
        self.assertEqual(
            denial_labels.error_category,
            MCPMetricErrorCategory.AUTHORIZATION,
        )

    async def test_post_dispatch_transport_failure_converges_to_unknown(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        recorder = _FakeMetricRecorder()

        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_FakeGateway(MCPGatewayError("mcp_transport_failed")),
            selector=_SequenceSelector(_call()),
            metric_recorder=recorder,
            metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.error.code, "mcp_transport_failed")
        labels = recorder.counts[-1][1]["labels"]
        self.assertEqual(labels.result_category, MCPMetricResultCategory.UNKNOWN)
        self.assertEqual(labels.error_category, MCPMetricErrorCategory.TRANSPORT)
        self.assertEqual(
            next(iter(storage.calls.values())).safe_error_code,
            "execution_status_unknown",
        )
        self.assertEqual(next(iter(storage.calls.values())).status, "unknown")

    async def test_validated_remote_error_after_dispatch_is_failed(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_FakeGateway(MCPRemoteError("tool rejected", remote_code=-32001)),
            selector=_SequenceSelector(_call()),
        )

        with self.assertRaises(MCPRemoteError):
            await coordinator.dispatch(_request(), server_id="server-a")

        call = next(iter(storage.calls.values()))
        self.assertEqual(call.status, "failed")
        self.assertEqual(call.safe_error_code, "mcp_call_failed")

    async def test_post_dispatch_cancellation_converges_to_unknown(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_FakeGateway(asyncio.CancelledError()),
            selector=_SequenceSelector(_call()),
        )

        with self.assertRaises(asyncio.CancelledError):
            await coordinator.dispatch(_request(), server_id="server-a")

        call = next(iter(storage.calls.values()))
        self.assertEqual(call.status, "unknown")
        self.assertEqual(call.safe_error_code, "execution_status_unknown")

    async def test_pre_dispatch_failure_is_excluded_from_terminal_metrics(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        recorder = _FakeMetricRecorder()
        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_PreDispatchFailureGateway(),
            selector=_SequenceSelector(_call()),
            metric_recorder=recorder,
            metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.error.code, "mcp_transport_failed")
        self.assertNotIn(
            MCPMetricName.TOOL_CALLS_TOTAL,
            [item[0] for item in recorder.counts],
        )
        self.assertNotIn(
            MCPMetricName.TOOL_CALL_DURATION_SECONDS,
            [item[0] for item in recorder.latencies],
        )

    async def test_metric_failure_is_only_safe_gap_and_does_not_change_call_result(self) -> None:
        storage = _FakeStorage()
        storage.grants.append(
            UserMCPToolGrant(
                "grant-a", "alice", "server-a", "lookup", 1, "schema-v1", NOW
            )
        )
        live_events = []

        async def record_live(event):
            live_events.append(event)

        coordinator = UserMCPDispatchCoordinator(
            storage=storage,
            gateway=_FakeGateway(MCPCallOutcome.completed("result-safe")),
            selector=_SequenceSelector(
                _call(), MCPSelectorAction(MCPSelectorActionType.FINISH, reason="done")
            ),
            live_event_recorder=record_live,
            metric_recorder=_FakeMetricRecorder(fail=True),
            metric_context=MCPDispatchMetricContext(MCPMetricRoutingMode.ENFORCE),
        )

        outcome = await coordinator.dispatch(_request(), server_id="server-a")

        self.assertEqual(outcome.output_payload["mcp_status"], "completed")
        gaps = [event for event in outcome.events if event.event_type == "mcp.rollout_metric_gap"]
        self.assertEqual(len(gaps), 3)
        gap = next(
            event
            for event in gaps
            if event.payload["metric_family"] == "tool_call_terminal"
        )
        self.assertEqual(
            gap.payload,
            {"metric_family": "tool_call_terminal", "gap_reason": "recording_failed"},
        )
        self.assertNotIn("secret", str(gap.payload).lower())
        self.assertEqual(
            len([event for event in live_events if event.event_type == "mcp.rollout_metric_gap"]),
            3,
        )


if __name__ == "__main__":
    unittest.main()
