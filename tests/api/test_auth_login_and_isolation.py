from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx_sse import aconnect_sse

from src.core.enums import ConversationStatus, MessageRole, TaskStatus
from src.core.models import (
    Conversation,
    ConversationAdmissionCloseDisposition,
    ConversationAdmissionCloseResult,
    Message,
    Task,
)

from tests.api.support import APITestCase, blocking_mysql_adapter


class AuthLoginAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()

    async def test_login_accepts_username_only_and_me_uses_returned_bearer(self) -> None:
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})

        self.assertEqual(login.status_code, 200, login.text)
        payload = login.json()
        self.assertEqual(payload["user"]["username"], "alice")
        self.assertTrue(payload["access_token"].startswith("maf_tok_"))
        self.assertNotIn("__Host-maf_session", self.client.cookies)
        self.assertNotIn("set-cookie", {key.lower(): value for key, value in login.headers.items()})

        me_without_bearer = await self.client.get("/api/v1/auth/me")
        self.assertEqual(me_without_bearer.status_code, 401)

        me = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {payload['access_token']}"})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["username"], "alice")


    async def test_login_rejects_password_captcha_and_other_extra_fields(self) -> None:
        response = await self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "alice",
                "password": "old-password",
                "captcha_id": "cap-old",
                "captcha_code": "1234",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    async def test_relogin_and_refresh_invalidate_previous_token(self) -> None:
        first = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(first.status_code, 200)
        first_token = first.json()["access_token"]

        second = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(second.status_code, 200)
        second_token = second.json()["access_token"]
        self.assertNotEqual(first_token, second_token)

        expired_me = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {first_token}"})
        self.assertEqual(expired_me.status_code, 401)

        refreshed = await self.client.post("/api/v1/auth/refresh-token", headers={"Authorization": f"Bearer {second_token}"})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        refreshed_token = refreshed.json()["access_token"]
        self.assertNotEqual(second_token, refreshed_token)
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {second_token}"})).status_code, 401)
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refreshed_token}"})).status_code, 200)

    async def test_business_api_requires_login(self) -> None:
        response = await self.submit_message(conversation_id="conv-no-login", content="你好", capability_id=None)
        self.assertEqual(response.status_code, 401)

    async def test_removed_password_auth_routes_are_not_available(self) -> None:
        for method, path, kwargs in (
            ("POST", "/api/v1/auth/captcha", {}),
            ("POST", "/api/v1/auth/register", {"json": {"username": "charlie", "password": "charlie1"}}),
            ("POST", "/api/v1/auth/api-tokens", {"json": {"client_name": "client", "scopes": ["conversation:read"]}}),
            ("GET", "/api/v1/auth/api-tokens", {}),
            ("DELETE", "/api/v1/auth/api-tokens", {"json": {"token_id": "tok-old"}}),
        ):
            response = await self.client.request(method, path, **kwargs)
            self.assertIn(response.status_code, {404, 410}, f"{method} {path}: {response.text}")



    async def test_removed_interrupt_answer_route_is_not_available(self) -> None:
        response = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": "task-any",
                "interrupt_id": "interrupt-any",
                "answer_payload": {"nested": {"USERNAME": "mallory"}},
            },
        )
        self.assertEqual(response.status_code, 404, response.text)

class AuthIsolationAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.reconfigure_runtime(main_agent_stream_generator=lambda _prompt: ["已收到。"])
        await self.logout()

    async def test_authenticated_submit_ignores_body_username_and_history_is_user_scoped(self) -> None:
        await self.login("alice")
        spoof = await self.submit_message(
            conversation_id="conv-alice",
            metadata={"username": "mallory-body-spoof"},
            content="你好",
            capability_id=None,
        )
        self.assertEqual(spoof.status_code, 422)
        response = await self.submit_message(
            conversation_id="conv-alice",
            content="你好",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)

        conversation = await self.runtime.storage.get_conversation("conv-alice")
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.username, "alice")

        conversations = await self.client.get("/api/v1/conversations")
        self.assertEqual(conversations.status_code, 200)
        self.assertEqual([item["conversation_id"] for item in conversations.json()["conversations"]], ["conv-alice"])

        messages = await self.client.get("/api/v1/conversations/conv-alice/messages")
        self.assertEqual(messages.status_code, 200)
        roles = [message["role"] for message in messages.json()["messages"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

        await self.login("bob")
        bob_conversations = await self.client.get("/api/v1/conversations")
        self.assertEqual(bob_conversations.status_code, 200)
        self.assertEqual(bob_conversations.json()["conversations"], [])
        self.assertEqual((await self.client.get("/api/v1/conversations/conv-alice/messages")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}/graph")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")).status_code, 404)
        self.assertEqual((await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})).status_code, 404)

    async def test_delete_conversation_is_owner_scoped_and_purges_history(self) -> None:
        await self.login("alice")
        response = await self.submit_message(
            conversation_id="conv-delete",
            content="你好",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)
        self.assertIsNotNone(await self.runtime.storage.get_conversation("conv-delete"))
        self.assertGreater(len(await self.runtime.storage.list_messages_for_conversation("conv-delete")), 0)

        await self.login("bob")
        forbidden = await self.client.request("DELETE", "/api/v1/conversations", json={"conversation_id": "conv-delete"})
        self.assertEqual(forbidden.status_code, 404)

        await self.login("alice")
        deleted = await self.client.request("DELETE", "/api/v1/conversations", json={"conversation_id": "conv-delete"})
        self.assertEqual(deleted.status_code, 200)
        payload = deleted.json()
        self.assertEqual(payload["conversation_id"], "conv-delete")
        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["cancelled_task_ids"], [])
        self.assertGreaterEqual(payload["deleted_counts"]["conversation"], 1)
        self.assertGreaterEqual(payload["deleted_counts"]["message"], 1)
        self.assertGreaterEqual(payload["deleted_counts"]["task"], 1)

        conversations = await self.client.get("/api/v1/conversations")
        self.assertEqual(conversations.status_code, 200)
        self.assertNotIn("conv-delete", [item["conversation_id"] for item in conversations.json()["conversations"]])
        self.assertEqual((await self.client.get("/api/v1/conversations/conv-delete/messages")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}")).status_code, 404)
        self.assertIsNone(await self.runtime.storage.get_conversation("conv-delete"))
        self.assertIsNone(await self.runtime.storage.get_task(task_id))
        self.assertEqual(await self.runtime.storage.list_events_for_task(task_id), [])
        self.assertIsNotNone(await self.runtime.storage.get_auth_user_token("alice"))

    async def test_deleting_conversation_is_hidden_from_ordinary_routes(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        await self.login("alice")
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-deleting",
                username="alice",
                status=ConversationStatus.DELETING,
                current_task_id="task-deleting",
                title="Deleting",
                delete_runner_id="delete-test",
                delete_requested_at=now,
                delete_phase="deleting_db",
                created_at=now,
                updated_at=now,
            )
        )
        await self.runtime.storage.save_message(
            Message(
                message_id="msg-deleting",
                conversation_id="conv-deleting",
                role=MessageRole.USER,
                content="hidden",
                task_id="task-deleting",
                created_at=now,
            )
        )
        await self.runtime.storage.save_task(
            Task(
                task_id="task-deleting",
                conversation_id="conv-deleting",
                root_message_id="msg-deleting",
                status=TaskStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
        )

        conversations = await self.client.get("/api/v1/conversations")
        self.assertEqual(conversations.status_code, 200)
        self.assertNotIn("conv-deleting", [item["conversation_id"] for item in conversations.json()["conversations"]])
        self.assertEqual((await self.client.get("/api/v1/conversations/conv-deleting/messages")).status_code, 404)
        self.assertEqual((await self.client.patch("/api/v1/conversations", json={"conversation_id": "conv-deleting", "title": "Nope"})).status_code, 404)
        self.assertEqual((await self.submit_message(conversation_id="conv-deleting", content="should fail", capability_id=None)).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/tasks/task-deleting")).status_code, 404)
        self.assertEqual((await self.client.post("/api/v1/tasks/cancel", json={"task_id": "task-deleting"})).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/conversations/conv-deleting/uploads")).status_code, 404)

    async def test_delete_runner_continues_after_waiter_cancellation(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-cancel-waiter",
                username="alice",
                title="cancel waiter",
                created_at=now,
                updated_at=now,
            )
        )
        original_delete_physical = self.runtime.storage.delete_conversation_physical
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()

        async def slow_delete(conversation_id: str) -> dict[str, int]:
            delete_started.set()
            await release_delete.wait()
            return await original_delete_physical(conversation_id)

        self.runtime.storage.delete_conversation_physical = slow_delete  # type: ignore[method-assign]
        waiter = asyncio.create_task(self.runtime.delete_conversation("conv-cancel-waiter", username="alice"))
        await delete_started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        release_delete.set()

        async def physically_deleted() -> bool:
            return await self.runtime.storage.get_conversation("conv-cancel-waiter") is None

        await self.wait_for_condition(physically_deleted)

    async def test_startup_recovery_reenters_deleting_conversation_runner(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-recover-delete",
                username="alice",
                status=ConversationStatus.DELETING,
                title="recover delete",
                delete_runner_id="delete-recover",
                delete_requested_at=now,
                delete_started_at=now,
                delete_phase="deleting_db",
                created_at=now,
                updated_at=now,
            )
        )

        await self.runtime.recover_deleting_conversations()

        async def physically_deleted() -> bool:
            return await self.runtime.storage.get_conversation("conv-recover-delete") is None

        await self.wait_for_condition(physically_deleted)

    async def test_delete_closes_admission_before_task_file_and_physical_mutation(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-delete-close-order",
                username="alice",
                created_at=now,
                updated_at=now,
            )
        )
        calls: list[str] = []
        original_close = self.runtime.storage.close_conversation_admission
        original_list_tasks = self.runtime.storage.list_tasks_for_conversation
        original_delete_files = self.runtime._delete_conversation_file_artifacts
        original_delete_physical = self.runtime.storage.delete_conversation_physical

        async def close(request):
            calls.append("close")
            return await original_close(request)

        async def list_tasks(*args, **kwargs):
            calls.append("list_tasks")
            return await original_list_tasks(*args, **kwargs)

        async def delete_files(conversation_id: str) -> None:
            calls.append("delete_files")
            await original_delete_files(conversation_id)

        async def delete_physical(conversation_id: str) -> dict[str, int]:
            calls.append("delete_physical")
            return await original_delete_physical(conversation_id)

        self.runtime.storage.close_conversation_admission = close  # type: ignore[method-assign]
        self.runtime.storage.list_tasks_for_conversation = list_tasks  # type: ignore[method-assign]
        self.runtime._delete_conversation_file_artifacts = delete_files  # type: ignore[method-assign]
        self.runtime.storage.delete_conversation_physical = delete_physical  # type: ignore[method-assign]

        await self.runtime.delete_conversation(
            "conv-delete-close-order", username="alice"
        )

        self.assertEqual(
            calls,
            ["close", "list_tasks", "delete_files", "delete_physical"],
        )

    async def test_delete_close_operation_identity_has_unambiguous_components(self) -> None:
        self.assertNotEqual(
            self.runtime._conversation_admission_close_operation_id("a\0b", "c"),
            self.runtime._conversation_admission_close_operation_id("a", "b\0c"),
        )

    async def test_delete_close_conflict_stops_before_business_mutation(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-delete-close-conflict",
                username="alice",
                created_at=now,
                updated_at=now,
            )
        )
        self.runtime.storage.close_conversation_admission = AsyncMock(
            return_value=ConversationAdmissionCloseResult(
                disposition=ConversationAdmissionCloseDisposition.CONFLICT,
                conversation_id="conv-delete-close-conflict",
            )
        )
        self.runtime.storage.list_tasks_for_conversation = AsyncMock(return_value=[])  # type: ignore[method-assign]
        self.runtime._delete_conversation_file_artifacts = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.runtime.storage.delete_conversation_physical = AsyncMock(  # type: ignore[method-assign]
            return_value={"conversation": 1}
        )

        with self.assertRaisesRegex(
            RuntimeError, "conversation_admission_close_failed"
        ):
            await self.runtime.delete_conversation(
                "conv-delete-close-conflict", username="alice"
            )

        self.runtime.storage.list_tasks_for_conversation.assert_not_awaited()
        self.runtime._delete_conversation_file_artifacts.assert_not_awaited()
        self.runtime.storage.delete_conversation_physical.assert_not_awaited()
        conflicted = await self.runtime.storage.get_conversation(
            "conv-delete-close-conflict"
        )
        self.assertIsNotNone(conflicted)
        assert conflicted is not None
        self.assertEqual(conflicted.status, ConversationStatus.DELETING_FAILED)

    async def test_startup_delete_recovery_replays_close_before_physical_cleanup(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        close_requests = []
        for conversation_id, phase in (
            (
                "conv-recover-before-close",
                "closing_admission",
            ),
            (
                "conv-recover-after-close",
                "deleting_db",
            ),
        ):
            await self.runtime.storage.save_conversation(
                Conversation(
                    conversation_id=conversation_id,
                    username="alice",
                    status=ConversationStatus.DELETING,
                    delete_runner_id=f"runner:{conversation_id}",
                    delete_requested_at=now,
                    delete_started_at=now,
                    delete_phase=phase,
                    created_at=now,
                    updated_at=now,
                )
            )

        async def close(request):
            close_requests.append(request)
            disposition = (
                ConversationAdmissionCloseDisposition.CLOSED
                if request.conversation_id == "conv-recover-before-close"
                else ConversationAdmissionCloseDisposition.EXACT_REPLAY
            )
            return ConversationAdmissionCloseResult(
                disposition=disposition,
                conversation_id=request.conversation_id,
            )

        self.runtime.storage.close_conversation_admission = close  # type: ignore[method-assign]

        await self.runtime.recover_deleting_conversations()

        self.assertIsNone(
            await self.runtime.storage.get_conversation("conv-recover-before-close")
        )
        self.assertIsNone(
            await self.runtime.storage.get_conversation("conv-recover-after-close")
        )
        self.assertEqual(len(close_requests), 2)
        self.assertEqual(
            {
                request.operation_id
                for request in close_requests
            },
            {
                self.runtime._conversation_admission_close_operation_id(
                    "alice", "conv-recover-before-close"
                ),
                self.runtime._conversation_admission_close_operation_id(
                    "alice", "conv-recover-after-close"
                ),
            },
        )
        self.assertNotIn(
            "runner:conv-recover-before-close",
            {request.operation_id for request in close_requests},
        )

    async def test_transient_delete_phase_failures_remain_startup_recoverable(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        for stage in ("close", "cancel", "files", "physical"):
            with self.subTest(stage=stage):
                conversation_id = f"conv-delete-transient-{stage}"
                await self.runtime.storage.save_conversation(
                    Conversation(
                        conversation_id=conversation_id,
                        username="alice",
                        created_at=now,
                        updated_at=now,
                    )
                )
                original_close = self.runtime.storage.close_conversation_admission
                original_list_tasks = self.runtime.storage.list_tasks_for_conversation
                original_cancel = self.runtime.cancel_task
                original_delete_files = self.runtime._delete_conversation_file_artifacts
                original_delete_physical = (
                    self.runtime.storage.delete_conversation_physical
                )
                close_operation_ids: list[str] = []
                failed = False

                async def close(request):
                    nonlocal failed
                    close_operation_ids.append(request.operation_id)
                    if stage == "close" and not failed:
                        failed = True
                        raise OSError("transient close failure")
                    return await original_close(request)

                async def list_tasks(*args, **kwargs):
                    if stage == "cancel" and not failed:
                        return [SimpleNamespace(task_id="task-transient-cancel")]
                    return await original_list_tasks(*args, **kwargs)

                async def cancel_task(task_id: str):
                    nonlocal failed
                    if stage == "cancel" and not failed:
                        failed = True
                        raise OSError("transient cancel failure")
                    return await original_cancel(task_id)

                async def delete_files(target_conversation_id: str) -> None:
                    nonlocal failed
                    if stage == "files" and not failed:
                        failed = True
                        raise OSError("transient file failure")
                    await original_delete_files(target_conversation_id)

                async def delete_physical(
                    target_conversation_id: str,
                ) -> dict[str, int]:
                    nonlocal failed
                    if stage == "physical" and not failed:
                        failed = True
                        raise OSError("transient physical failure")
                    return await original_delete_physical(target_conversation_id)

                self.runtime.storage.close_conversation_admission = close  # type: ignore[method-assign]
                self.runtime.storage.list_tasks_for_conversation = list_tasks  # type: ignore[method-assign]
                self.runtime.cancel_task = cancel_task  # type: ignore[method-assign]
                self.runtime._delete_conversation_file_artifacts = delete_files  # type: ignore[method-assign]
                self.runtime.storage.delete_conversation_physical = delete_physical  # type: ignore[method-assign]
                try:
                    with self.assertRaises(OSError):
                        await self.runtime.delete_conversation(
                            conversation_id,
                            username="alice",
                        )

                    pending = await self.runtime.storage.get_conversation(
                        conversation_id
                    )
                    self.assertIsNotNone(pending)
                    assert pending is not None
                    self.assertEqual(pending.status, ConversationStatus.DELETING)
                    runner_id = pending.delete_runner_id

                    recovered = await self.runtime.delete_conversation(
                        conversation_id,
                        username="alice",
                    )

                    self.assertIsNone(
                        await self.runtime.storage.get_conversation(conversation_id)
                    )
                    self.assertTrue(recovered["deleted"])
                    self.assertEqual(len(close_operation_ids), 2)
                    self.assertEqual(
                        len(set(close_operation_ids)),
                        1,
                    )
                    self.assertEqual(
                        close_operation_ids[0],
                        self.runtime._conversation_admission_close_operation_id(
                            "alice", conversation_id
                        ),
                    )
                    self.assertIsNotNone(runner_id)
                finally:
                    self.runtime.storage.close_conversation_admission = original_close  # type: ignore[method-assign]
                    self.runtime.storage.list_tasks_for_conversation = original_list_tasks  # type: ignore[method-assign]
                    self.runtime.cancel_task = original_cancel  # type: ignore[method-assign]
                    self.runtime._delete_conversation_file_artifacts = original_delete_files  # type: ignore[method-assign]
                    self.runtime.storage.delete_conversation_physical = original_delete_physical  # type: ignore[method-assign]

    async def test_deleting_conversation_with_external_runner_still_waits(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        conversation_id = "conv-delete-external-runner"
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id=conversation_id,
                username="alice",
                status=ConversationStatus.DELETING,
                delete_runner_id="external-runner",
                delete_requested_at=now,
                delete_started_at=now,
                delete_phase="deleting_db",
                created_at=now,
                updated_at=now,
            )
        )
        self.runtime._wait_for_external_conversation_delete = AsyncMock(  # type: ignore[method-assign]
            return_value={"conversation_id": conversation_id, "deleted": True}
        )
        self.runtime._run_conversation_delete = AsyncMock()  # type: ignore[method-assign]

        result = await self.runtime.delete_conversation(
            conversation_id,
            username="alice",
        )

        self.assertTrue(result["deleted"])
        self.runtime._wait_for_external_conversation_delete.assert_awaited_once_with(
            conversation_id,
            runner_id="external-runner",
        )
        self.runtime._run_conversation_delete.assert_not_awaited()

    async def test_startup_delete_recovery_failure_leaves_no_background_delete(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        for conversation_id in ("conv-recover-a-fails", "conv-recover-b-pending"):
            await self.runtime.storage.save_conversation(
                Conversation(
                    conversation_id=conversation_id,
                    username="alice",
                    status=ConversationStatus.DELETING,
                    delete_runner_id=f"runner:{conversation_id}",
                    delete_requested_at=now,
                    delete_started_at=now,
                    delete_phase="closing_admission",
                    created_at=now,
                    updated_at=now,
                )
            )
        close_requests = []

        async def close(request):
            close_requests.append(request)
            return ConversationAdmissionCloseResult(
                disposition=ConversationAdmissionCloseDisposition.CONFLICT,
                conversation_id=request.conversation_id,
            )

        self.runtime.storage.close_conversation_admission = close  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            RuntimeError,
            "conversation_admission_close_failed",
        ):
            await self.runtime.recover_deleting_conversations()
        await asyncio.sleep(0)

        self.assertEqual(
            [request.conversation_id for request in close_requests],
            ["conv-recover-a-fails"],
        )
        pending = await self.runtime.storage.get_conversation(
            "conv-recover-b-pending"
        )
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.status, ConversationStatus.DELETING)
        self.assertFalse(
            any(
                not task.done()
                for task in self.runtime._conversation_delete_tasks.values()
            )
        )

    async def test_delete_conversation_auto_cancels_running_task_before_purge(self) -> None:
        query_started = threading.Event()
        blocking_adapter, release = blocking_mysql_adapter(started=query_started)
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        await self.login("alice")
        response = await self.submit_message(conversation_id="conv-running-delete", content="查询龙粳33", capability_id="skill.generic_data_lookup")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            return (
                task is not None
                and str(task.status) == "running"
                and query_started.is_set()
            )

        await self.wait_for_condition(task_running)

        deleted = await self.client.request("DELETE", "/api/v1/conversations", json={"conversation_id": "conv-running-delete"})
        release.set()
        self.assertEqual(deleted.status_code, 200)
        payload = deleted.json()
        self.assertEqual(payload["conversation_id"], "conv-running-delete")
        self.assertIn(task_id, payload["cancelled_task_ids"])
        self.assertGreaterEqual(payload["deleted_counts"]["task"], 1)
        self.assertIsNone(await self.runtime.storage.get_conversation("conv-running-delete"))
        self.assertIsNone(await self.runtime.storage.get_task(task_id))
        self.assertEqual(await self.runtime.storage.list_messages_for_conversation("conv-running-delete"), [])
        self.assertEqual(await self.runtime.storage.list_events_for_task(task_id), [])

    async def test_non_owner_cannot_subscribe_to_sse_or_cancel_other_users_task(self) -> None:
        query_started = threading.Event()
        blocking_adapter, release = blocking_mysql_adapter(started=query_started)
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        await self.login("alice")
        response = await self.submit_message(conversation_id="conv-alice", content="查询龙粳33", capability_id="skill.generic_data_lookup")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            return (
                task is not None
                and str(task.status) == "running"
                and query_started.is_set()
            )

        await self.wait_for_condition(task_running)

        await self.login("bob")
        stolen_submit = await self.submit_message(
            conversation_id="conv-alice",
            content="试图写入他人的忙碌会话",
            capability_id="skill.generic_data_lookup",
        )
        self.assertEqual(stolen_submit.status_code, 404)
        self.assertNotIn(task_id, stolen_submit.text)
        self.assertEqual((await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})).status_code, 404)
        async with aconnect_sse(self.client, "GET", f"/api/v1/tasks/{task_id}/events") as event_source:
            self.assertEqual(event_source.response.status_code, 404)

        await self.login("alice")
        cancel = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel.status_code, 202)
        release.set()
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "cancelled")
    async def test_submit_does_not_revive_conversation_marked_deleting_mid_request(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        await self.login("alice")
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-race-delete",
                username="alice",
                title="race",
                created_at=now,
                updated_at=now,
            )
        )
        original_refresh_skills = self.runtime._refresh_skills_for_new_conversation_if_needed

        async def mark_deleting_during_submit(conversation_id, existing_conversation):
            await original_refresh_skills(conversation_id, existing_conversation)
            await self.runtime.storage.mark_conversation_deleting(
                conversation_id,
                runner_id="delete-race",
                requested_at=now,
                started_at=now,
                phase="marking",
            )

        self.runtime._refresh_skills_for_new_conversation_if_needed = mark_deleting_during_submit  # type: ignore[method-assign]

        response = await self.submit_message(conversation_id="conv-race-delete", content="should not revive", capability_id=None)

        self.assertEqual(response.status_code, 404, response.text)
        current = await self.runtime.storage.get_conversation("conv-race-delete")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, ConversationStatus.DELETING)
        self.assertIsNone(current.current_task_id)
        self.assertEqual(await self.runtime.storage.list_messages_for_conversation("conv-race-delete"), [])
        self.assertEqual(await self.runtime.storage.list_tasks_for_conversation("conv-race-delete"), [])

    async def test_transient_delete_failure_after_waiter_cancellation_is_recoverable(self) -> None:
        now = datetime(2026, 5, 26, 12, 0, 0)
        await self.login("alice")
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-delete-fails-after-cancel",
                username="alice",
                title="failure after cancellation",
                created_at=now,
                updated_at=now,
            )
        )
        original_delete_physical = self.runtime.storage.delete_conversation_physical
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        unhandled_contexts: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()

        def capture_unhandled(_loop, context):
            unhandled_contexts.append(dict(context))

        async def failing_delete(conversation_id: str) -> dict[str, int]:
            delete_started.set()
            await release_delete.wait()
            raise ValueError(f"simulated physical delete failure for {conversation_id}")

        self.runtime.storage.delete_conversation_physical = failing_delete  # type: ignore[method-assign]
        loop.set_exception_handler(capture_unhandled)
        try:
            waiter = asyncio.create_task(self.runtime.delete_conversation("conv-delete-fails-after-cancel", username="alice"))
            await delete_started.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter

            release_delete.set()

            async def delete_runner_stopped() -> bool:
                task = self.runtime._conversation_delete_tasks.get(
                    "conv-delete-fails-after-cancel"
                )
                return task is None or task.done()

            await self.wait_for_condition(delete_runner_stopped)
            current = await self.runtime.storage.get_conversation(
                "conv-delete-fails-after-cancel"
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.status, ConversationStatus.DELETING)
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)
            self.runtime.storage.delete_conversation_physical = original_delete_physical  # type: ignore[method-assign]

        await self.runtime.recover_deleting_conversations()
        self.assertIsNone(
            await self.runtime.storage.get_conversation(
                "conv-delete-fails-after-cancel"
            )
        )
        self.assertEqual(
            [context.get("message") for context in unhandled_contexts],
            [],
        )
