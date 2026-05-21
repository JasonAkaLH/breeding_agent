from __future__ import annotations

from httpx_sse import aconnect_sse

from tests.api.support import APITestCase, blocking_mysql_adapter


class AuthLoginAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()
        await self.runtime.create_user("alice", "alice-password1")

    async def test_login_requires_valid_password_and_captcha_then_me_restores_user(self) -> None:
        captcha = await self.client.post("/api/v1/auth/captcha")
        self.assertEqual(captcha.status_code, 200)
        captcha_payload = captcha.json()
        self.assertIn("captcha_id", captcha_payload)
        self.assertIn("image_svg", captcha_payload)

        wrong = await self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "alice",
                "password": "wrong-password",
                "captcha_id": captcha_payload["captcha_id"],
                "captcha_code": "1234",
            },
        )
        self.assertEqual(wrong.status_code, 401)

        captcha = await self.client.post("/api/v1/auth/captcha")
        login = await self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "alice",
                "password": "alice-password1",
                "captcha_id": captcha.json()["captcha_id"],
                "captcha_code": "1234",
            },
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["username"], "alice")
        self.assertIn("__Host-maf_session", self.client.cookies)
        session_cookie = self.client.cookies.get("__Host-maf_session")
        self.assertIsInstance(session_cookie, str)
        self.assertNotIn("alice", session_cookie)
        self.assertNotIn("username", session_cookie.lower())

        me = await self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["username"], "alice")

    async def test_captcha_is_single_use(self) -> None:
        captcha = await self.client.post("/api/v1/auth/captcha")
        captcha_id = captcha.json()["captcha_id"]
        first = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password1", "captcha_id": captcha_id, "captcha_code": "1234"},
        )
        self.assertEqual(first.status_code, 200)
        await self.logout()

        second = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password1", "captcha_id": captcha_id, "captcha_code": "1234"},
        )
        self.assertEqual(second.status_code, 401)

    async def test_business_api_requires_login(self) -> None:
        response = await self.submit_message(conversation_id="conv-no-login", content="你好", capability_id=None)
        self.assertEqual(response.status_code, 401)

    async def test_register_creates_user_and_logs_in(self) -> None:
        captcha = await self.client.post("/api/v1/auth/captcha")
        captcha_id = captcha.json()["captcha_id"]

        response = await self.client.post(
            "/api/v1/auth/register",
            json={
                "username": "charlie",
                "password": "charlie1",
                "captcha_id": captcha_id,
                "captcha_code": "1234",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["username"], "charlie")
        self.assertIn("__Host-maf_session", self.client.cookies)
        stored_user = await self.runtime.storage.get_auth_user("charlie")
        self.assertIsNotNone(stored_user)
        self.assertNotEqual(stored_user.password_hash, "charlie1")
        me = await self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["username"], "charlie")

    async def test_register_rejects_duplicate_username(self) -> None:
        captcha = await self.client.post("/api/v1/auth/captcha")

        response = await self.client.post(
            "/api/v1/auth/register",
            json={
                "username": "alice",
                "password": "alice-password2",
                "captcha_id": captcha.json()["captcha_id"],
                "captcha_code": "1234",
            },
        )

        self.assertEqual(response.status_code, 409)

    async def test_register_requires_letter_and_digit_password(self) -> None:
        captcha = await self.client.post("/api/v1/auth/captcha")
        no_digit = await self.client.post(
            "/api/v1/auth/register",
            json={
                "username": "letters",
                "password": "letters-only",
                "captcha_id": captcha.json()["captcha_id"],
                "captcha_code": "1234",
            },
        )
        self.assertEqual(no_digit.status_code, 400)

        captcha = await self.client.post("/api/v1/auth/captcha")
        no_letter = await self.client.post(
            "/api/v1/auth/register",
            json={
                "username": "digits",
                "password": "12345678",
                "captcha_id": captcha.json()["captcha_id"],
                "captcha_code": "1234",
            },
        )
        self.assertEqual(no_letter.status_code, 400)

    async def test_register_requires_valid_captcha(self) -> None:
        captcha = await self.client.post("/api/v1/auth/captcha")

        response = await self.client.post(
            "/api/v1/auth/register",
            json={
                "username": "captchauser",
                "password": "captcha1",
                "captcha_id": captcha.json()["captcha_id"],
                "captcha_code": "0000",
            },
        )

        self.assertEqual(response.status_code, 401)


class AuthIsolationAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.reconfigure_runtime(main_agent_stream_generator=lambda _prompt: ["已收到。"])
        await self.logout()
        await self.runtime.create_user("alice", "alice-password1")
        await self.runtime.create_user("bob", "bob-password1")

    async def test_authenticated_submit_ignores_body_account_id_and_history_is_user_scoped(self) -> None:
        await self.login("alice", "alice-password1")
        response = await self.submit_message(
            conversation_id="conv-alice",
            account_id="mallory-body-spoof",
            content="你好",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)

        conversation = await self.runtime.storage.get_conversation("conv-alice")
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.account_id, "alice")

        conversations = await self.client.get("/api/v1/conversations")
        self.assertEqual(conversations.status_code, 200)
        self.assertEqual([item["conversation_id"] for item in conversations.json()["conversations"]], ["conv-alice"])

        messages = await self.client.get("/api/v1/conversations/conv-alice/messages")
        self.assertEqual(messages.status_code, 200)
        roles = [message["role"] for message in messages.json()["messages"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

        await self.login("bob", "bob-password1")
        bob_conversations = await self.client.get("/api/v1/conversations")
        self.assertEqual(bob_conversations.status_code, 200)
        self.assertEqual(bob_conversations.json()["conversations"], [])
        self.assertEqual((await self.client.get("/api/v1/conversations/conv-alice/messages")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}/graph")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")).status_code, 404)
        self.assertEqual((await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})).status_code, 404)

    async def test_delete_conversation_is_owner_scoped_and_purges_history(self) -> None:
        await self.login("alice", "alice-password1")
        response = await self.submit_message(
            conversation_id="conv-delete",
            account_id="mallory-body-spoof",
            content="你好",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)
        self.assertIsNotNone(await self.runtime.storage.get_conversation("conv-delete"))
        self.assertGreater(len(await self.runtime.storage.list_messages_for_conversation("conv-delete")), 0)

        await self.login("bob", "bob-password1")
        forbidden = await self.client.request("DELETE", "/api/v1/conversations", json={"conversation_id": "conv-delete"})
        self.assertEqual(forbidden.status_code, 404)

        await self.login("alice", "alice-password1")
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
        self.assertIsNotNone(await self.runtime.storage.get_auth_user("alice"))

    async def test_delete_conversation_auto_cancels_running_task_before_purge(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        await self.runtime.create_user("alice", "alice-password1")
        await self.runtime.create_user("bob", "bob-password1")

        await self.login("alice", "alice-password1")
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
        await self.runtime.create_user("alice", "alice-password1")
        await self.runtime.create_user("bob", "bob-password1")

        await self.login("alice", "alice-password1")
        response = await self.submit_message(conversation_id="conv-alice", content="查询龙粳33", capability_id="skill.generic_data_lookup")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            return task is not None and str(task.status) == "running"

        await self.wait_for_condition(task_running)

        await self.login("bob", "bob-password1")
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

        await self.login("alice", "alice-password1")
        cancel = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel.status_code, 202)
        release.set()
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "cancelled")
