from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import os
from unittest.mock import AsyncMock, patch

from src.core.enums import NodeStatus, TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import Interrupt, TaskNode, UserMCPServer
from tests.api.support import APITestCase


class MCPServerSoftBindingAPITest(APITestCase):
    def build_runtime(self, **kwargs):
        kwargs.setdefault(
            "planner_text_generator",
            lambda _prompt, **_options: '{"action":"finish","reason":"done"}',
        )
        kwargs.setdefault("enable_llm_planner", True)
        with patch.dict(
            os.environ,
            {
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            return super().build_runtime(
                **kwargs,
                enable_user_mcp=True,
                enable_user_mcp_routing=True,
            )

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        now = datetime(2026, 8, 17, 9, 0, 0)
        servers = (
            UserMCPServer(
                server_id="mcp-available",
                owner_user_id="acc-1",
                display_name="OCR服务",
                routing_description="OCR",
                endpoint_url="https://mcp.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            ),
            UserMCPServer(
                server_id="mcp-disabled",
                owner_user_id="acc-1",
                display_name="Disabled",
                routing_description="",
                endpoint_url="https://disabled.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                enabled=False,
                health_status=UserMCPHealthStatus.DISABLED,
                created_at=now,
                updated_at=now,
            ),
            UserMCPServer(
                server_id="mcp-unavailable",
                owner_user_id="acc-1",
                display_name="Unavailable",
                routing_description="",
                endpoint_url="https://unavailable.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.UNAVAILABLE,
                created_at=now,
                updated_at=now,
            ),
            UserMCPServer(
                server_id="mcp-deleting",
                owner_user_id="acc-1",
                display_name="Deleting",
                routing_description="",
                endpoint_url="https://deleting.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                deletion_pending=True,
                created_at=now,
                updated_at=now,
            ),
            UserMCPServer(
                server_id="mcp-deleted",
                owner_user_id="acc-1",
                display_name="Deleted",
                routing_description="",
                endpoint_url="https://deleted.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                deleted_at=now,
                created_at=now,
                updated_at=now,
            ),
            UserMCPServer(
                server_id="mcp-other-owner",
                owner_user_id="other-user",
                display_name="Other",
                routing_description="",
                endpoint_url="https://other.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            ),
        )
        for server in servers:
            await self.runtime.storage.create_user_mcp_server(server)
        await self.runtime.storage.mark_user_mcp_server_deleted(
            "acc-1",
            "mcp-deleting",
            deleted_at=now,
        )
        await self.runtime.storage.mark_user_mcp_server_deleted(
            "acc-1",
            "mcp-deleted",
            deleted_at=now,
        )
        await self.runtime.storage.finalize_user_mcp_server_delete(
            "acc-1",
            "mcp-deleted",
            now=now,
        )

    @staticmethod
    def _payload(conversation_id: str, server_id: str = "mcp-available") -> dict[str, object]:
        return {
            "conversation_id": conversation_id,
            "content": "识别这份材料",
            "routing_mode": "force_capability",
            "capability_id": "mcp.dispatch",
            "metadata": {
                "mcp_server_binding": {"server_id": server_id},
                "deep_thinking": False,
            },
        }

    async def test_binding_structure_errors_return_422(self) -> None:
        payload = self._payload("conv-structure")
        payload["metadata"] = {
            "mcp_server_binding": {"server_id": "mcp-available"},
            "endpoint": "https://evil.example.test",
        }
        response = await self.client.post("/api/v1/conversations/chat-messages", json=payload)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(await self.runtime.storage.list_messages_for_conversation("conv-structure"), [])
        self.assertEqual(await self.runtime.storage.list_tasks_for_conversation("conv-structure"), [])

    async def test_unavailable_and_cross_owner_bindings_return_same_409_without_writes(self) -> None:
        for server_id in (
            "mcp-missing",
            "mcp-other-owner",
            "mcp-disabled",
            "mcp-unavailable",
            "mcp-deleting",
            "mcp-deleted",
        ):
            conversation_id = f"conv-{server_id}"
            with self.subTest(server_id=server_id):
                response = await self.client.post(
                    "/api/v1/conversations/chat-messages",
                    json=self._payload(conversation_id, server_id),
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json(),
                    {"detail": {"code": "mcp_bound_server_unavailable"}},
                )
                self.assertIsNone(await self.runtime.storage.get_conversation(conversation_id))
                self.assertEqual(await self.runtime.storage.list_messages_for_conversation(conversation_id), [])
                self.assertEqual(await self.runtime.storage.list_tasks_for_conversation(conversation_id), [])

    async def test_runtime_unavailable_returns_503_without_writes(self) -> None:
        self.runtime.user_mcp_routing_enabled = False
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json=self._payload("conv-feature-off"),
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": {"code": "mcp_feature_unavailable"}})
        self.assertIsNone(await self.runtime.storage.get_conversation("conv-feature-off"))

    async def test_valid_binding_persists_private_context_and_public_badge(self) -> None:
        scheduler = AsyncMock()
        with patch.object(self.runtime, "_schedule_execution", scheduler):
            response = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json=self._payload("conv-valid"),
            )

        self.assertEqual(response.status_code, 202, response.text)
        task = await self.runtime.storage.get_task(response.json()["task_id"])
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.requested_capability_id, "mcp.dispatch")
        self.assertEqual(task.mcp_execution_mode, "user_scoped")
        message = await self.runtime.storage.get_message(response.json()["message_id"])
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(
            message.metadata["mcp_server_binding_context"],
            {
                "server_id": "mcp-available",
                "server_config_version": 1,
                "server_security_version": 1,
                "binding_mode": "explicit_command",
            },
        )
        self.assertEqual(
            message.metadata["mcp_server_badge"],
            {
                "server_id": "mcp-available",
                "display_name": "OCR服务",
                "command": "$OCR服务",
                "binding_mode": "explicit_command",
            },
        )
        scheduler.assert_awaited_once()
        orchestration_request = scheduler.await_args.args[0]
        self.assertEqual(orchestration_request.available_mcp_servers, ())
        self.assertEqual(orchestration_request.metadata["mcp_dispatch_server_id"], "mcp-available")
        self.assertNotIn("mcp_server_binding", orchestration_request.metadata)
        self.assertNotIn("mcp_server_binding_context", orchestration_request.metadata)
        events = await self.runtime.storage.list_events_for_task(task.task_id)
        binding_event = next(
            event
            for event in events
            if event.event_type == "mcp.server_binding_resolved"
        )
        self.assertEqual(binding_event.payload["binding_mode"], "explicit_command")
        self.assertEqual(binding_event.payload["status"], "accepted")
        self.assertNotEqual(binding_event.payload["safe_server_ref"], "mcp-available")
        self.assertNotIn("display_name", binding_event.payload)

        history = await self.client.get("/api/v1/conversations/conv-valid/messages")
        self.assertEqual(history.status_code, 200, history.text)
        public = history.json()["messages"][0]["metadata"]
        self.assertEqual(public, {"mcp_server_badge": message.metadata["mcp_server_badge"]})

    async def test_binding_does_not_answer_an_existing_interrupt(self) -> None:
        scheduler = AsyncMock()
        with patch.object(self.runtime, "_schedule_execution", scheduler):
            first = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json=self._payload("conv-interrupt"),
            )
        self.assertEqual(first.status_code, 202, first.text)
        task_id = first.json()["task_id"]
        node = TaskNode(
            node_id=f"{task_id}:waiting",
            task_id=task_id,
            capability_id="mcp.dispatch",
            status=NodeStatus.RUNNING,
        )
        await self.runtime.storage.save_task_node(node)
        task = await self.runtime.storage.get_task(task_id)
        assert task is not None
        await self.runtime.storage.save_task(replace(task, status=TaskStatus.RUNNING))
        interrupt = Interrupt(
            interrupt_id=f"{node.node_id}:interrupt",
            conversation_id="conv-interrupt",
            task_id=task_id,
            node_id=node.node_id,
            source_agent="mcp.dispatch",
            source_message_id=first.json()["message_id"],
            question="补充信息",
            reason_code="mcp_input_required",
            required_fields={"answer": {"type": "string"}},
        )
        await self.runtime.interrupt_service.open_interrupt(interrupt)
        before_messages = await self.runtime.storage.list_messages_for_conversation("conv-interrupt")

        second = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json=self._payload("conv-interrupt"),
        )

        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(
            await self.runtime.storage.list_interrupt_answers(interrupt.interrupt_id),
            [],
        )
        self.assertEqual(
            await self.runtime.storage.list_messages_for_conversation("conv-interrupt"),
            before_messages,
        )

    async def test_mcp_interrupt_resume_restores_explicit_binding_from_root_message(self) -> None:
        scheduler = AsyncMock()
        with patch.object(self.runtime, "_schedule_execution", scheduler):
            first = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json=self._payload("conv-resume"),
            )
        self.assertEqual(first.status_code, 202, first.text)
        task_id = first.json()["task_id"]
        task = await self.runtime.storage.get_task(task_id)
        assert task is not None
        await self.runtime.storage.save_task(replace(task, status=TaskStatus.RUNNING))
        node = TaskNode(
            node_id=f"{task_id}:mcp_dispatch",
            task_id=task_id,
            capability_id="mcp.dispatch",
            status=NodeStatus.RUNNING,
        )
        await self.runtime.storage.save_task_node(node)
        interrupt = Interrupt(
            interrupt_id=f"{node.node_id}:interrupt",
            conversation_id="conv-resume",
            task_id=task_id,
            node_id=node.node_id,
            source_agent="mcp.dispatch",
            source_message_id=first.json()["message_id"],
            question="请补充",
            reason_code="mcp_input_required",
            required_fields={"server_id": "mcp-available"},
        )
        await self.runtime.interrupt_service.open_interrupt(interrupt)
        scheduler.reset_mock()

        with patch.object(self.runtime, "_schedule_execution", scheduler):
            answer = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json={
                    "conversation_id": "conv-resume",
                    "content": "继续",
                    "routing_mode": "auto",
                    "capability_id": None,
                    "metadata": {
                        "interrupt_id": interrupt.interrupt_id,
                        "mcp_input_responses": {},
                    },
                },
            )

        self.assertEqual(answer.status_code, 202, answer.text)
        scheduler.assert_awaited_once()
        resume_request = scheduler.await_args.args[0]
        self.assertEqual(resume_request.metadata["mcp_dispatch_server_id"], "mcp-available")
        self.assertEqual(resume_request.metadata["mcp_binding_mode"], "explicit_command")
        self.assertEqual(resume_request.metadata["mcp_command"], "$OCR服务")
