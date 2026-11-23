from datetime import datetime, timezone
from unittest.mock import patch

from src.core.models import Conversation
from tests.api.support import APITestCase


class ConversationModelSettingsAPITest(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.runtime.storage.save_conversation(
            Conversation("prefs", "acc-1", title="keep", current_task_id="task-running")
        )
        self.path = "/api/v1/conversations/prefs/model-settings"
        self.body = dict(
            model_edition="api-test",
            reasoning_effort="max",
            deep_thinking=True,
            expected_revision=0,
        )

    async def test_get_missing_put_restore_and_conflict(self):
        self.assertEqual(
            (await self.client.get(self.path)).json(),
            {"conversation_id": "prefs", "settings": None},
        )
        parent = await self.runtime.storage.get_conversation("prefs")
        response = await self.client.put(self.path, json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["settings"]["revision"], 1)
        self.assertEqual((await self.client.get(self.path)).json(), response.json())
        self.assertEqual(
            (await self.client.put(self.path, json=self.body)).status_code, 409
        )
        self.assertEqual(await self.runtime.storage.get_conversation("prefs"), parent)
        await self.login("acc-1")
        self.assertEqual((await self.client.get(self.path)).json(), response.json())

    async def test_ownership_auth_and_no_parent_creation(self):
        await self.login("other")
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        self.assertEqual(
            (await self.client.put(self.path, json=self.body)).status_code, 404
        )
        self.assertEqual(
            (
                await self.client.put(
                    self.path.replace("prefs", "absent"), json=self.body
                )
            ).status_code,
            404,
        )
        self.assertIsNone(await self.runtime.storage.get_conversation("absent"))
        await self.logout()
        self.assertEqual((await self.client.get(self.path)).status_code, 401)

    async def test_invalid_combinations_types_and_deleting(self):
        for changes, status in [
            ({"model_edition": "missing"}, 400),
            ({"reasoning_effort": "invalid"}, 400),
            ({"deep_thinking": False}, 400),
            ({"deep_thinking": "false"}, 422),
            ({"expected_revision": -1}, 422),
            ({"expected_revision": True}, 422),
            ({"extra": "forbidden"}, 422),
        ]:
            response = await self.client.put(self.path, json={**self.body, **changes})
            self.assertEqual(response.status_code, status, response.text)
        await self.runtime.storage.mark_conversation_deleting(
            "prefs",
            runner_id="delete",
            requested_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        self.assertEqual(
            (await self.client.put(self.path, json=self.body)).status_code, 404
        )

    async def test_first_text_admission_initializes_preferences_from_request(self):
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "new-text",
                "client_message_id": "first-text",
                "content": "hello",
                "routing_mode": "auto",
                "capability_id": None,
                "model_edition": "api-test",
                "metadata": {
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "max",
                },
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        run = await self.runtime.agent_run_repository.get_run_for_task(
            response.json()["task_id"]
        )
        self.assertIsNotNone(run)
        original_binding = run.binding
        saved = await self.client.put(
            "/api/v1/conversations/new-text/model-settings",
            json={
                "model_edition": "api-test",
                "reasoning_effort": "minimal",
                "deep_thinking": False,
                "expected_revision": 1,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(
            (
                await self.runtime.agent_run_repository.get_run_for_task(
                    response.json()["task_id"]
                )
            ).binding,
            original_binding,
        )
        self.assertEqual(
            (original_binding.reasoning_effort, original_binding.thinking_enabled),
            ("max", True),
        )
        value = (
            await self.client.get("/api/v1/conversations/new-text/model-settings")
        ).json()["settings"]
        self.assertEqual(
            (
                value["model_edition"],
                value["reasoning_effort"],
                value["deep_thinking"],
                value["revision"],
            ),
            ("api-test", "minimal", False, 2),
        )

    async def test_first_upload_then_settings_and_failed_upload_cleanup(self):
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "new-upload"},
            files={"file": ("settings.csv", "a\n1\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        path = "/api/v1/conversations/new-upload/model-settings"
        self.assertIsNone((await self.client.get(path)).json()["settings"])
        saved = await self.client.put(path, json=self.body)
        self.assertEqual(saved.status_code, 200, saved.text)
        await self.runtime.storage.delete_conversation_physical("new-upload")
        self.assertEqual((await self.client.put(path, json=self.body)).status_code, 404)
        with patch.object(
            self.runtime.storage,
            "save_conversation_file_resource_with_upload_message",
            side_effect=RuntimeError("upload rollback"),
        ):
            failed = await self.client.post(
                "/api/v1/conversations/uploads",
                data={"conversation_id": "failed-upload"},
                files={"file": ("settings.csv", "a\n1\n", "text/csv")},
            )
        self.assertEqual(failed.status_code, 400, failed.text)
        self.assertIsNone(await self.runtime.storage.get_conversation("failed-upload"))
        self.assertEqual(
            (
                await self.client.put(
                    "/api/v1/conversations/failed-upload/model-settings", json=self.body
                )
            ).status_code,
            404,
        )
