from __future__ import annotations

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
    UserMCPDispatchCoordinator,
)
from src.integrations.mcp.gateway_models import (
    MCPCallOutcome,
    MCPTaskServerScope,
    MCPToolDescriptor,
    ToolCatalogSnapshot,
)


NOW = datetime(2026, 8, 12, 12, 0, 0)


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
    def __init__(self, *outcomes: MCPCallOutcome) -> None:
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

    async def call_tool(self, scope, tool_name, arguments, callbacks):
        call_ref = f"safe-call-{len(self.calls) + 1}"
        await callbacks.on_created(call_ref)
        self.callback_order.append("reserved")
        await callbacks.on_registered(call_ref)
        self.callback_order.append("registered")
        if callbacks.on_heartbeat is not None:
            await callbacks.on_heartbeat(call_ref)
        self.calls.append((tool_name, dict(arguments)))
        return self.outcomes.pop(0)

    async def close_scope(self, scope, reason: str):
        del reason
        self.closed.append(scope.scope_id)


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
        self.assertTrue(approval_event.payload["safe_call_ref"].startswith("mcp-approval-call-"))
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
        self.assertEqual(live_events[0].payload["safe_call_ref"], "safe-call-1")
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
        self.assertEqual(gateway.calls, [("lookup", {"query": "Alice"})])
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
        self.assertEqual(gateway.calls, [("lookup", {"query": "first"})])
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


if __name__ == "__main__":
    unittest.main()
