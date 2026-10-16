from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from src.core.enums import ConversationStatus
from tests.api.support import APITestCase


class KnowledgeSelectionTest(APITestCase):
    async def _library(self, name="育种"):
        return (await self.client.post("/api/v1/knowledge/libraries", json={"name": name})).json()["library_id"]

    async def test_selection_before_first_message_is_persistent_and_owner_bound(self):
        library_id = await self._library()
        url = "/api/v1/conversations/before-first/knowledge-selection"
        initial = await self.client.get(url)
        self.assertEqual(initial.json()["mode"], "auto")
        self.assertIsNone(await self.runtime.storage.get_conversation("before-first"))
        selected = await self.client.put(url, json={"mode": "selected", "library_ids": [library_id], "revision": 0})
        self.assertEqual(selected.status_code, 200, selected.text)
        conversation = await self.runtime.storage.get_conversation("before-first")
        self.assertEqual(conversation.username, "acc-1")
        self.assertEqual((await self.client.get(url)).json()["library_ids"], [library_id])
        self.assertEqual((await self.client.get("/api/v1/conversations/brand-new/knowledge-selection")).json()["mode"], "auto")
        stale = await self.client.put(url, json={"mode": "auto", "library_ids": [], "revision": 0})
        self.assertEqual(stale.status_code, 409)
        await self.login("bob")
        self.assertEqual((await self.client.put(url, json={"mode": "auto", "library_ids": []})).status_code, 409)
        self.assertEqual((await self.runtime.storage.get_conversation("before-first")).username, "acc-1")

    async def test_empty_selected_and_unavailable_ids_cannot_become_auto(self):
        url = "/api/v1/conversations/choice/knowledge-selection"
        for payload in ({"mode": "selected", "library_ids": []}, {"mode": "auto", "library_ids": ["other"]}):
            self.assertEqual((await self.client.put(url, json=payload)).status_code, 422)
        self.assertEqual((await self.client.put(url, json={"mode": "selected", "library_ids": ["other"]})).status_code, 409)
        self.assertIsNone(await self.runtime.storage.get_conversation("choice"))

    async def test_frozen_scope_does_not_follow_new_selection_or_new_library(self):
        first, second = await self._library("first"), await self._library("second")
        url = "/api/v1/conversations/frozen/knowledge-selection"
        await self.client.put(url, json={"mode": "selected", "library_ids": [first]})
        repository = self.runtime.knowledge_repository
        scope = await asyncio.to_thread(repository.freeze_scope, "acc-1", "frozen", "root-one")
        await self.client.put(url, json={"mode": "selected", "library_ids": [second]})
        third = await self._library("third")
        replay = await asyncio.to_thread(repository.freeze_scope, "acc-1", "frozen", "root-one")
        self.assertEqual(replay, scope)
        self.assertEqual(replay["candidate_library_ids"], [first])
        self.assertNotIn(third, replay["candidate_library_ids"])
        task = SimpleNamespace(task_id="t", conversation_id="frozen", root_message_id="root-one")
        with patch.object(self.runtime.storage, "get_task", return_value=task):
            loaded = await self.runtime._knowledge_scope_for_run(SimpleNamespace(task_id="t", conversation_id="frozen"))
        self.assertEqual(loaded["candidate_library_ids"], [first])

    async def test_selection_reports_revoked_ids_and_cannot_expand_fixed_scope(self):
        first, second = await self._library("first"), await self._library("second")
        await self.login("bob")
        await self.login("acc-1")
        for library_id in (first, second):
            await self.client.post(f"/api/v1/knowledge/libraries/{library_id}/grants", json={"subject_id": "bob"})
        await self.login("bob")
        url = "/api/v1/conversations/shared-choice/knowledge-selection"
        await self.client.put(url, json={"mode": "selected", "library_ids": [first, second]})
        scope = await asyncio.to_thread(self.runtime.knowledge_repository.freeze_scope, "bob", "shared-choice", "fixed")
        await self.login("acc-1")
        await self.client.delete(f"/api/v1/knowledge/libraries/{first}/grants/user:bob")
        await self.login("bob")
        self.assertEqual((await self.client.get(url)).json()["unavailable_library_ids"], [first])
        result = await self.runtime.knowledge_service.list_for_scope("bob", scope)
        self.assertEqual([item["library_id"] for item in result["libraries"]], [second])
        self.assertFalse(result["requires_reselection"])
        await self.login("acc-1")
        await self.client.delete(f"/api/v1/knowledge/libraries/{second}/grants/user:bob")
        result = await self.runtime.knowledge_service.list_for_scope("bob", scope)
        self.assertTrue(result["requires_reselection"])
        self.assertEqual(result["libraries"], [])

    async def test_snapshot_failure_prevents_admission(self):
        with patch.object(self.runtime.knowledge_repository, "freeze_scope", side_effect=OSError("disk")), patch.object(
            self.runtime.storage, "admit_submission",
        ) as admission:
            response = await self.submit_message(conversation_id="failed-scope", content="hello", capability_id=None)
        self.assertEqual(response.status_code, 503, response.text)
        admission.assert_not_called()

    async def test_deleted_conversation_is_not_revived_by_selection(self):
        await self.client.put("/api/v1/conversations/deleting/knowledge-selection", json={"mode": "auto"})
        conversation = await self.runtime.storage.get_conversation("deleting")
        await self.runtime.storage.save_conversation(replace(conversation, status=ConversationStatus.DELETING))
        response = await self.client.put("/api/v1/conversations/deleting/knowledge-selection", json={"mode": "auto"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.runtime.storage.get_conversation("deleting")).status, ConversationStatus.DELETING)

    async def test_old_message_replay_does_not_create_a_missing_scope(self):
        await self.reconfigure_runtime(main_agent_stream_generator=lambda _prompt, **_kwargs: ["已收到。"])
        first = await self.submit_message(
            conversation_id="legacy-scope", content="hello", capability_id=None,
            client_message_id="legacy-message",
        )
        self.assertEqual(first.status_code, 202, first.text)
        await self.wait_for_terminal_task(first.json()["task_id"])
        # Simulate an admitted legacy message/Run which never had knowledge state.
        await asyncio.to_thread(self.runtime.knowledge_repository.delete_conversation_context, "legacy-scope")
        library_id = await self._library()
        await self.client.put("/api/v1/conversations/legacy-scope/knowledge-selection", json={
            "mode": "selected", "library_ids": [library_id],
        })
        replay = await self.submit_message(
            conversation_id="legacy-scope", content="hello", capability_id=None,
            client_message_id="legacy-message",
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["task_id"], first.json()["task_id"])
        self.assertIsNone(self.runtime.knowledge_repository.get_scope("legacy-message"))
        run = await self.runtime.agent_run_repository.get_run_for_task(first.json()["task_id"])
        self.assertIsNone(await self.runtime._knowledge_scope_for_run(run))
