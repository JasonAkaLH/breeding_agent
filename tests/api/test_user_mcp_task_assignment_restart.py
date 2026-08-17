from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.api.dto import SubmitMessageRequest
from src.api.runtime import build_api_runtime
from src.core.enums import NodeStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import Interrupt, TaskNode, UserMCPServer
from src.orchestration.models import OrchestrationRequest
from src.storage.sqlite.repositories import SQLiteStorage
from tests.api.support import InMemoryTaskRuntimeSidecar


class UserMCPTaskAssignmentRestartTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.database_path = self.root / "runtime.sqlite3"
        self.audit_log_path = self.root / "audit.jsonl"
        self.key_path = self.root / "mcp.key"
        self.key_path.write_text(
            "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
            encoding="ascii",
        )
        self.key_path.chmod(0o600)
        self.runtime_sidecar = InMemoryTaskRuntimeSidecar()

    async def test_user_scoped_task_keeps_persisted_assignment_after_rollout_config_changes(self) -> None:
        runtime = self._build_runtime(self._enforce_env(percent=100, salt="first-salt"))
        scheduled: list[OrchestrationRequest] = []

        async def capture_execution(request: OrchestrationRequest) -> None:
            scheduled.append(request)

        runtime._schedule_execution = capture_execution
        await self._create_available_server(runtime)
        _, task = await runtime.submit_message(
            "conv-user-scoped",
            SubmitMessageRequest(
                conversation_id="conv-user-scoped",
                content="查询 CRM",
                routing_mode="force_capability",
                capability_id="mcp.dispatch",
                metadata={
                    "mcp_server_binding": {"server_id": "server-a"},
                },
            ),
            authenticated_username="alice",
        )
        original_config_version = task.mcp_rollout_config_version
        await runtime.storage.save_task_node(
            TaskNode(
                node_id="node-user-scoped",
                task_id=task.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
        )
        await runtime.interrupt_service.open_interrupt(
            Interrupt(
                interrupt_id="interrupt-user-scoped",
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                node_id="node-user-scoped",
                source_agent="mcp.dispatch",
                source_message_id=task.root_message_id,
                question="允许调用 CRM 吗？",
                reason_code="approval_required",
                required_fields={"server_id": "server-a"},
                created_at=datetime(2026, 8, 13, 1, 0, 1),
            )
        )
        await runtime.shutdown()

        restarted = self._build_runtime(
            self._enforce_env(percent=0, salt="replacement-salt")
        )
        resumed: list[OrchestrationRequest] = []

        async def capture_resume(request: OrchestrationRequest) -> None:
            resumed.append(request)

        restarted._schedule_execution = capture_resume
        try:
            self.assertEqual(
                restarted.mcp_rollout_config.assign_authenticated_user("alice").real_path,
                "legacy",
            )
            await restarted.answer_interrupt(
                task.task_id,
                "interrupt-user-scoped",
                {"approved": True},
            )

            self.assertEqual(len(resumed), 1)
            request = resumed[0]
            self.assertEqual(request.metadata["mcp_execution_mode"], "user_scoped")
            self.assertEqual(
                request.metadata["mcp_rollout_config_version"],
                original_config_version,
            )
            self.assertNotEqual(
                request.metadata["mcp_rollout_config_version"],
                restarted.mcp_rollout_config.config_version,
            )
            self.assertEqual(request.metadata["mcp_binding_mode"], "explicit_command")
            self.assertEqual(request.metadata["mcp_dispatch_server_id"], "server-a")
            self.assertEqual([profile.server_id for profile in request.available_mcp_servers], ["server-a"])
            visible = restarted.capability_registry.list_for_request(
                request,
                public_only=True,
            )
            self.assertIn("mcp.dispatch", {item.capability_id for item in visible})
            plan = restarted.workflow_provider.build_plan(request)
            self.assertEqual(plan.nodes[0].capability_id, "mcp.dispatch")
            self.assertEqual(plan.nodes[0].metadata["mcp_binding_mode"], "explicit_command")
        finally:
            await restarted.shutdown()

    async def test_new_task_uses_legacy_assignment_when_restarted_with_routing_off(self) -> None:
        runtime = self._build_runtime(self._off_env())
        scheduled: list[OrchestrationRequest] = []

        async def capture_execution(request: OrchestrationRequest) -> None:
            scheduled.append(request)

        runtime._schedule_execution = capture_execution
        try:
            _, task = await runtime.submit_message(
                "conv-new-off",
                SubmitMessageRequest(
                    conversation_id="conv-new-off",
                    content="普通请求",
                    capability_id="main_agent.respond",
                ),
                authenticated_username="alice",
            )

            stored = await runtime.storage.get_task(task.task_id)
            self.assertEqual(stored.mcp_execution_mode, "legacy")
            self.assertEqual(stored.mcp_rollout_mode, "off")
            self.assertEqual(scheduled[0].metadata["mcp_execution_mode"], "legacy")
        finally:
            await runtime.shutdown()

    async def test_legacy_task_keeps_persisted_assignment_after_enforce_restart(self) -> None:
        runtime = self._build_runtime(self._off_env())
        scheduled: list[OrchestrationRequest] = []

        async def capture_execution(request: OrchestrationRequest) -> None:
            scheduled.append(request)

        runtime._schedule_execution = capture_execution
        _, task = await runtime.submit_message(
            "conv-legacy",
            SubmitMessageRequest(
                conversation_id="conv-legacy",
                content="普通请求",
                capability_id="main_agent.respond",
            ),
            authenticated_username="alice",
        )
        original_config_version = task.mcp_rollout_config_version
        node = TaskNode(
            node_id="node-legacy",
            task_id=task.task_id,
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )
        await runtime.storage.save_task_node(node)
        await runtime.interrupt_service.open_interrupt(
            Interrupt(
                interrupt_id="interrupt-legacy",
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                node_id="node-legacy",
                source_agent="main_agent.respond",
                source_message_id=task.root_message_id,
                question="继续吗？",
                reason_code="input_required",
                created_at=datetime(2026, 8, 13, 2, 0, 0),
            )
        )
        backfill_storage = SQLiteStorage(
            runtime.storage._session_factory,
            runtime_sidecar_client=self.runtime_sidecar,
            runtime_sidecar_shadow_sink=lambda _payload: None,
            mcp_task_authority_mode="shadow",
        )
        backfilled_node = await runtime.storage.get_task_node(node.node_id)
        self.assertIsNotNone(backfilled_node)
        await backfill_storage.save_task(task)
        await backfill_storage.save_task_node(backfilled_node)
        await runtime.shutdown()

        restarted = self._build_runtime(self._enforce_env(percent=100, salt="enforce-salt"))
        resumed: list[OrchestrationRequest] = []

        async def capture_resume(request: OrchestrationRequest) -> None:
            resumed.append(request)

        restarted._schedule_execution = capture_resume
        try:
            self.assertEqual(
                restarted.mcp_rollout_config.assign_authenticated_user("alice").real_path,
                "user_scoped",
            )
            await restarted.answer_interrupt(
                task.task_id,
                "interrupt-legacy",
                {"answer": "继续"},
            )

            self.assertEqual(len(resumed), 1)
            request = resumed[0]
            self.assertEqual(request.metadata["mcp_execution_mode"], "legacy")
            self.assertEqual(
                request.metadata["mcp_rollout_config_version"],
                original_config_version,
            )
            self.assertEqual(request.available_mcp_servers, ())
            visible = restarted.capability_registry.list_for_request(
                request,
                public_only=True,
            )
            self.assertNotIn("mcp.dispatch", {item.capability_id for item in visible})
        finally:
            await restarted.shutdown()

    async def _create_available_server(self, runtime) -> None:
        now = datetime(2026, 8, 13, 0, 0, 0)
        await runtime.storage.create_user_mcp_server(
            UserMCPServer(
                server_id="server-a",
                owner_user_id="alice",
                display_name="CRM",
                routing_description="查询客户资料",
                endpoint_url="https://crm.invalid/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
        )

    def _build_runtime(self, rollout_env: dict[str, str]):
        environment = {
            "MAF_STATE_STORE_BACKEND": "sqlite",
            "MAF_API_ENV": "test",
            "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
            "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            **rollout_env,
        }
        patcher = patch.dict(os.environ, environment, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return build_api_runtime(
            database_path=self.database_path,
            audit_log_path=self.audit_log_path,
            master_key_bytes=b"a" * 32,
            mcp_config={"enabled": False},
            planner_text_generator=lambda _prompt, **_kwargs: '{"action":"finish"}',
            main_agent_stream_generator=lambda _prompt, **_kwargs: "done",
            main_agent_llm_config={
                "model_editions": {
                    "default": "test",
                    "options": [
                        {
                            "value": "test",
                            "label": "Test",
                            "reasoning_efforts": {
                                "default": "minimal",
                                "disabled_default": "minimal",
                                "options": [
                                    {
                                        "value": "minimal",
                                        "label": "Minimal",
                                        "allow_when_thinking_disabled": True,
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
            enable_platform_llm=False,
            enable_conversation_title_llm=False,
            enable_conversation_memory=False,
            runtime_sidecar_client=self.runtime_sidecar,
        )

    @staticmethod
    def _enforce_env(*, percent: int, salt: str) -> dict[str, str]:
        return {
            "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
            "MCP_ROUTING_MODE": "enforce",
            "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            "MCP_ENFORCE_COHORTS": "",
            "MCP_ENFORCE_PERCENT": str(percent),
            "MCP_ENFORCE_HASH_SALT": salt,
            "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
        }

    @staticmethod
    def _off_env() -> dict[str, str]:
        return {
            "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
            "MCP_ROUTING_MODE": "off",
            "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            "MCP_ENFORCE_COHORTS": "",
            "MCP_ENFORCE_PERCENT": "0",
            "MCP_ENFORCE_HASH_SALT": "",
            "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
        }
