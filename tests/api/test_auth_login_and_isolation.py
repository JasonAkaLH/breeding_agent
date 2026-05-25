from __future__ import annotations

from httpx_sse import aconnect_sse

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



    async def test_interrupt_answer_rejects_reserved_identity_payload(self) -> None:
        response = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": "task-any",
                "interrupt_id": "interrupt-any",
                "answer_payload": {"nested": {"USERNAME": "mallory"}},
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

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

    async def test_delete_conversation_auto_cancels_running_task_before_purge(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        await self.login("alice")
        response = await self.submit_message(conversation_id="conv-running-delete", content="查询龙粳33", capability_id="skill.generic_data_lookup")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            return task is not None and str(task.status) == "running"

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
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        await self.login("alice")
        response = await self.submit_message(conversation_id="conv-alice", content="查询龙粳33", capability_id="skill.generic_data_lookup")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            return task is not None and str(task.status) == "running"

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
