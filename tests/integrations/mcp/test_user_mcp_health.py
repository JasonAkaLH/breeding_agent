from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.integrations.mcp.client import MCPClientError
from src.integrations.mcp.health import (
    MCPHealthRunner,
    discover_healthy_tools,
    run_health_discovery,
)


class _Client:
    def __init__(self, *, capabilities=None, tools=None, error=None) -> None:
        self.server_capabilities = dict(capabilities or {})
        self.tools = list(tools or [])
        self.error = error
        self.closed = False

    async def initialize(self):
        if self.error is not None:
            raise self.error

    async def list_tools(self):
        return self.tools

    async def close(self):
        self.closed = True


class UserMCPHealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_capability_and_empty_list_are_unavailable(self) -> None:
        no_capability = await discover_healthy_tools(_Client())
        empty = await discover_healthy_tools(_Client(capabilities={"tools": {}}, tools=[]))

        self.assertEqual(no_capability.error_code, "no_tools_capability")
        self.assertEqual(empty.error_code, "empty_tool_list")
        self.assertFalse(no_capability.available)
        self.assertFalse(empty.available)

    async def test_at_least_one_valid_tool_is_available(self) -> None:
        result = await discover_healthy_tools(
            _Client(capabilities={"tools": {}}, tools=[{"name": "lookup", "inputSchema": {"type": "object"}}])
        )
        self.assertTrue(result.available)
        self.assertEqual(result.tool_count, 1)

    async def test_transient_failure_retries_once_with_independent_client(self) -> None:
        clients = [
            _Client(error=MCPClientError("temporary", code="mcp_transport_error", retriable=True)),
            _Client(capabilities={"tools": {}}, tools=[{"name": "lookup", "inputSchema": {"type": "object"}}]),
        ]
        sleeps: list[float] = []

        result = await run_health_discovery(
            lambda: clients.pop(0),
            timeout_seconds=1,
            retry_delay_seconds=0.1,
            sleep=lambda seconds: _record_sleep(sleeps, seconds),
        )

        self.assertTrue(result.available)
        self.assertEqual(sleeps, [0.1])

    async def test_timeout_budget_includes_async_client_factory(self) -> None:
        async def blocked_factory():
            await asyncio.Event().wait()

        result = await run_health_discovery(
            blocked_factory,
            timeout_seconds=0.01,
            retry_delay_seconds=0,
            sleep=lambda seconds: _record_sleep([], seconds),
        )

        self.assertFalse(result.available)
        self.assertEqual(result.error_code, "discovery_timeout")

    async def test_external_cancellation_is_not_swallowed_by_cleanup(self) -> None:
        close_started = asyncio.Event()

        class BlockingCloseClient(_Client):
            async def close(self):
                close_started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(
            run_health_discovery(
                lambda: BlockingCloseClient(
                    capabilities={"tools": {}},
                    tools=[{"name": "lookup", "inputSchema": {"type": "object"}}],
                ),
                timeout_seconds=1,
                cleanup_timeout_seconds=10,
            )
        )
        await asyncio.wait_for(close_started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_each_retry_attempt_has_its_own_bounded_cleanup_budget(self) -> None:
        close_calls = 0

        class RetriableBlockingCloseClient(_Client):
            async def close(self):
                nonlocal close_calls
                close_calls += 1
                await asyncio.Event().wait()

        clients = [
            RetriableBlockingCloseClient(
                error=MCPClientError(
                    "temporary",
                    code="mcp_transport_error",
                    retriable=True,
                )
            ),
            RetriableBlockingCloseClient(
                error=MCPClientError(
                    "temporary",
                    code="mcp_transport_error",
                    retriable=True,
                )
            ),
        ]
        result = await asyncio.wait_for(
            run_health_discovery(
                lambda: clients.pop(0),
                timeout_seconds=1,
                retry_delay_seconds=0,
                cleanup_timeout_seconds=0.001,
            ),
            timeout=0.1,
        )
        self.assertFalse(result.available)
        self.assertEqual(close_calls, 2)

    async def test_cancel_server_cancels_inflight_discovery_before_return(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class BlockingClient(_Client):
            async def initialize(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        class Storage:
            async def claim_user_mcp_health_attempt(self, attempt):
                return True

            async def complete_user_mcp_health_attempt(self, *args, **kwargs):
                return None

            async def release_user_mcp_health_attempt(self, *args, **kwargs):
                return True

        runner = MCPHealthRunner(
            storage=Storage(),
            instance_id="runner-1",
            client_factory=lambda server, credentials: BlockingClient(),
            credential_loader=lambda server: {},
            now_fn=lambda: datetime(2026, 8, 12, 12, 0, 0),
        )
        server = SimpleNamespace(
            owner_user_id="alice",
            server_id="server-1",
            config_version=1,
            security_version=1,
        )
        await runner.start_test(server)
        await asyncio.wait_for(started.wait(), timeout=1)

        await runner.cancel_server("alice", "server-1", reason="deleted")

        self.assertTrue(cancelled.is_set())
        await runner.aclose()

    async def test_recovery_coordinator_keeps_sweeping_after_transient_failure(self) -> None:
        recovered = asyncio.Event()

        class Storage:
            def __init__(self) -> None:
                self.calls = 0

            async def expire_user_mcp_health_attempts(self, *, now):
                del now
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("temporary database error")
                if self.calls >= 3:
                    recovered.set()
                return 0

        storage = Storage()
        runner = MCPHealthRunner(
            storage=storage,
            instance_id="runner-recovery",
            client_factory=lambda server, credentials: _Client(),
            credential_loader=lambda server: {},
            now_fn=lambda: datetime(2026, 8, 12, 12, 0, 0),
        )
        with patch(
            "src.integrations.mcp.health.HEALTH_RECOVERY_INTERVAL_SECONDS", 0.01
        ):
            await runner.start()
            await asyncio.wait_for(recovered.wait(), timeout=1)
            await runner.aclose()

        self.assertGreaterEqual(storage.calls, 3)


async def _record_sleep(target: list[float], seconds: float) -> None:
    target.append(seconds)
