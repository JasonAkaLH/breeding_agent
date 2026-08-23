from __future__ import annotations

import asyncio
import contextlib
import unittest

from src.api.routes.tasks import SseConnectionContext, _iter_authorized_frontend_events
from src.core.enums import EventVisibility, TaskStatus
from src.core.models import Conversation, EventRecord, Task
from tests.api.support import APITestCase


class SseAuthGenerationInvalidationTest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(login.status_code, 200, login.text)
        self.alice_token = login.json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {self.alice_token}"
        self.token_record = await self.runtime.storage.get_auth_user_token("alice")
        await self.runtime.storage.save_conversation(Conversation(conversation_id="conv-sse-auth", username="alice"))
        await self.runtime.storage.save_task(
            Task(
                task_id="task-sse-auth",
                conversation_id="conv-sse-auth",
                root_message_id="msg-sse-auth",
                status=TaskStatus.RUNNING,
            )
        )

    def _context(self) -> SseConnectionContext:
        return SseConnectionContext(
            username="alice",
            conversation_id="conv-sse-auth",
            task_id="task-sse-auth",
            auth_generation_at_connect=self.token_record.auth_generation,
            connected_at=self.runtime._utcnow_naive(),
            connection_id="sse-test",
        )

    async def test_sse_event_loop_does_not_query_token_storage_per_event(self) -> None:
        lookup_count = 0
        touch_count = 0
        original_lookup = self.runtime.storage.get_auth_user_token_by_hash
        original_touch = self.runtime.storage.touch_auth_user_token_last_used

        async def counting_lookup(*args, **kwargs):
            nonlocal lookup_count
            lookup_count += 1
            return await original_lookup(*args, **kwargs)

        async def counting_touch(*args, **kwargs):
            nonlocal touch_count
            touch_count += 1
            return await original_touch(*args, **kwargs)

        self.runtime.storage.get_auth_user_token_by_hash = counting_lookup  # type: ignore[method-assign]
        self.runtime.storage.touch_auth_user_token_last_used = counting_touch  # type: ignore[method-assign]
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            self._context(),
            revalidation_interval_seconds=10,
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.05)
        try:
            for index in range(10):
                await self.runtime._publish_transient_event(
                    EventRecord(
                        event_id=f"evt-sse-auth-{index}",
                        conversation_id="conv-sse-auth",
                        task_id="task-sse-auth",
                        event_type="agent.reasoning_delta",
                        payload={"delta": f"chunk-{index}"},
                        visibility=EventVisibility.FRONTEND,
                    )
                )
                event = await asyncio.wait_for(pending, timeout=1)
                self.assertEqual(event.event_type, "agent.reasoning_delta")
                pending = asyncio.create_task(iterator.__anext__())
        finally:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(RuntimeError):
                    await aclose()
        self.assertEqual(lookup_count, 0)
        self.assertEqual(touch_count, 0)

    async def test_token_refresh_closes_existing_sse_with_non_persistent_control_event(self) -> None:
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            self._context(),
            revalidation_interval_seconds=10,
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.05)
        refresh = await self.client.post("/api/v1/auth/refresh-token")
        self.assertEqual(refresh.status_code, 200, refresh.text)
        await self.runtime._publish_transient_event(
            EventRecord(
                event_id="evt-after-refresh",
                conversation_id="conv-sse-auth",
                task_id="task-sse-auth",
                event_type="agent.reasoning_delta",
                payload={"delta": "late"},
                visibility=EventVisibility.FRONTEND,
            )
        )
        event = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(event.event_type, "auth.invalidated")
        self.assertEqual(event.payload["reason"], "auth_generation_mismatch")
        persisted = await self.runtime.storage.list_events_for_task("task-sse-auth")
        self.assertFalse(any(record.event_type == "auth.invalidated" for record in persisted))
        with contextlib.suppress(StopAsyncIteration):
            await asyncio.wait_for(iterator.__anext__(), timeout=1)

    async def test_logout_closes_existing_sse_without_task_cancellation(self) -> None:
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            self._context(),
            revalidation_interval_seconds=10,
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.05)
        logout = await self.client.post("/api/v1/auth/logout")
        self.assertEqual(logout.status_code, 200, logout.text)
        await self.runtime._publish_transient_event(
            EventRecord(
                event_id="evt-after-logout",
                conversation_id="conv-sse-auth",
                task_id="task-sse-auth",
                event_type="agent.reasoning_delta",
                payload={"delta": "late"},
                visibility=EventVisibility.FRONTEND,
            )
        )
        event = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(event.event_type, "auth.invalidated")
        task = await self.runtime.storage.get_task("task-sse-auth")
        self.assertEqual(task.status, TaskStatus.RUNNING)

    async def test_cache_miss_closes_existing_sse_and_does_not_query_db(self) -> None:
        self.runtime.auth_generation_cache.reconcile({})
        lookup_count = 0

        async def counting_lookup(*_args, **_kwargs):
            nonlocal lookup_count
            lookup_count += 1
            return None

        self.runtime.storage.get_auth_user_token_by_hash = counting_lookup  # type: ignore[method-assign]
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            self._context(),
            revalidation_interval_seconds=10,
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.05)
        await self.runtime._publish_transient_event(
            EventRecord(
                event_id="evt-cache-miss",
                conversation_id="conv-sse-auth",
                task_id="task-sse-auth",
                event_type="agent.reasoning_delta",
                payload={"delta": "late"},
                visibility=EventVisibility.FRONTEND,
            )
        )
        event = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(event.event_type, "auth.invalidated")
        self.assertEqual(event.payload["reason"], "auth_generation_unknown")
        self.assertEqual(lookup_count, 0)

    async def test_unhealthy_postgres_invalidation_bus_closes_sse_fail_closed(self) -> None:
        class _UnhealthyBus:
            class _Health:
                ready = False

            health = _Health()

            async def aclose(self) -> None:
                return None

        self.runtime.postgres_auth_invalidation_bus = _UnhealthyBus()  # type: ignore[assignment]
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            self._context(),
            revalidation_interval_seconds=10,
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.05)
        await self.runtime._publish_transient_event(
            EventRecord(
                event_id="evt-auth-bus-unhealthy",
                conversation_id="conv-sse-auth",
                task_id="task-sse-auth",
                event_type="agent.reasoning_delta",
                payload={"delta": "late"},
                visibility=EventVisibility.FRONTEND,
            )
        )
        event = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(event.event_type, "auth.invalidated")
        self.assertEqual(event.payload["reason"], "auth_generation_unavailable")
        persisted = await self.runtime.storage.list_events_for_task("task-sse-auth")
        self.assertFalse(any(record.event_type == "auth.invalidated" for record in persisted))


if __name__ == "__main__":
    unittest.main()
