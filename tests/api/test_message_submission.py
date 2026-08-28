from __future__ import annotations

import asyncio
import json

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.enums import NodeStatus
from src.core.errors import MessageIdentityConflictError
from src.core.models import (
    Conversation,
    PendingSkillContext,
    SubmissionProjectionAcknowledgementRequest,
)
from src.storage.sqlalchemy_models import ConversationRow, MessageRow, TaskRow

from tests.api.support import (
    APITestCase,
    InMemoryTaskRuntimeSidecar,
    blocking_mysql_adapter,
)


class _IdentityRejectingSidecar(InMemoryTaskRuntimeSidecar):
    async def reserve_message_identity(self, **payload: object) -> dict[str, object]:
        self.calls.append(("message_identity_reserve", dict(payload)))
        raise AssertionError("disabled identity authority must not reserve")


class _SubmissionAuthoritySidecar(InMemoryTaskRuntimeSidecar):
    def __init__(self) -> None:
        super().__init__()
        self.admission: dict[str, object] | None = None
        self.omit_replay_claim = False

    def admit_submission(self, **request: object) -> dict[str, object]:
        if self.admission is not None:
            disposition = (
                "idempotent_replay"
                if self.admission["request_fingerprint"]
                == request["request_fingerprint"]
                else "message_id_conflict"
            )
            return self._response(disposition, request)
        task = dict(request["task"])
        task_id = str(request["task_id"])
        self.tasks[task_id] = task
        self.admission = {
            "message_id": request["message_id"],
            "task_id": task_id,
            "conversation_id": request["conversation_id"],
            "username": request["username"],
            "request_fingerprint": request["request_fingerprint"],
            "conversation_projection_json": request["conversation_projection_json"],
            "message_projection_json": request["message_projection_json"],
            "projection_sha256": request["projection_sha256"],
            "continuation_json": request["continuation_json"],
            "continuation_sha256": request["continuation_sha256"],
            "projection_state": "pending",
            "preparation_state": "pending",
            "prepared_execution_json": None,
            "prepared_execution_sha256": None,
            "handoff_state": "pending",
            "handoff_kind": None,
            "handoff_identity": None,
            "created_at_ms": request["now_ms"],
            "updated_at_ms": request["now_ms"],
            "closed": False,
            "task": task,
            "idempotency_key": request["idempotency_key"],
        }
        return self._response("created", request)

    def acknowledge_submission_projection(
        self, **_request: object
    ) -> dict[str, object]:
        assert self.admission is not None
        self.admission = {**self.admission, "projection_state": "projected"}
        return {
            "operation": "submission_projection_acknowledge",
            "admission": self.admission,
            "duplicate": False,
            "error": None,
        }

    def _response(
        self, disposition: str, request: dict[str, object]
    ) -> dict[str, object]:
        return {
            "operation": "submission_admit",
            "disposition": disposition,
            "admission": self.admission,
            "claim": (
                None
                if disposition == "idempotent_replay" and self.omit_replay_claim
                else {
                    "owner": request["workflow_owner"],
                    "token": "claim-secret",
                    "expires_at_ms": int(request["now_ms"])
                    + int(request["claim_ttl_ms"]),
                }
            ),
            "error": None,
        }


class MessageSubmissionAPITest(APITestCase):
    async def test_hint_routing_shape_errors_return_422_before_submission_state(self) -> None:
        with self.runtime.storage._session_factory() as session:  # noqa: SLF001
            before = (
                session.query(MessageRow).count(),
                session.query(TaskRow).count(),
            )
        invalid = (
            {"routing_mode": "hint", "capability_id": None},
            {"routing_mode": "auto", "capability_id": "skill.generic_data_lookup"},
            {"routing_mode": "force_capability", "capability_id": None},
            {"routing_mode": "unknown", "capability_id": None},
        )
        for index, routing in enumerate(invalid):
            with self.subTest(routing=routing):
                response = await self.client.post(
                    "/api/v1/conversations/chat-messages",
                    json={
                        "conversation_id": "conv-1",
                        "content": "hello",
                        "client_message_id": f"invalid-hint-{index}",
                        "metadata": {},
                        **routing,
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)
        with self.runtime.storage._session_factory() as session:  # noqa: SLF001
            self.assertEqual(
                (session.query(MessageRow).count(), session.query(TaskRow).count()),
                before,
            )

    async def test_non_skill_hint_returns_low_sensitivity_409_without_submission_state(self) -> None:
        with self.runtime.storage._session_factory() as session:  # noqa: SLF001
            before = (
                session.query(MessageRow).count(),
                session.query(TaskRow).count(),
            )
        for index, capability_id in enumerate(
            ("mcp.dispatch", "skill.not_available")
        ):
            with self.subTest(capability_id=capability_id):
                response = await self.client.post(
                    "/api/v1/conversations/chat-messages",
                    json={
                        "conversation_id": "conv-1",
                        "content": "what can this do",
                        "routing_mode": "hint",
                        "capability_id": capability_id,
                        "client_message_id": f"unavailable-hint-{index}",
                        "metadata": {},
                    },
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["detail"],
                    {"code": "skill_hint_unavailable"},
                )
        with self.runtime.storage._session_factory() as session:  # noqa: SLF001
            self.assertEqual(
                (session.query(MessageRow).count(), session.query(TaskRow).count()),
                before,
            )

    async def test_skill_hint_strips_forged_activation_authority_metadata(self) -> None:
        forged = {
            "profile": {"capability_id": "skill.private"},
            "profile_digest": "f" * 64,
            "pinned_bundle_revision": "forged-revision",
            "skill_activation": {"binding_mode": "force"},
        }
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-forged-hint",
                "content": "what can this skill do",
                "routing_mode": "hint",
                "capability_id": "skill.generic_data_lookup",
                "client_message_id": "forged-hint-authority",
                "metadata": forged,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        message = await self.runtime.storage.get_message(
            response.json()["message_id"]
        )
        self.assertTrue(set(forged).isdisjoint(message.metadata))
        run = await self.runtime.agent_run_repository.get_run_for_task(task_id)
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        activation = json.loads(items[1].payload_json)
        self.assertNotEqual(activation["profile_digest"], forged["profile_digest"])
        self.assertNotEqual(
            activation["pinned_bundle_revision"],
            forged["pinned_bundle_revision"],
        )

    async def test_public_skill_hint_initializes_user_and_activation_before_202(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-1",
                "content": "what can this skill do",
                "routing_mode": "hint",
                "capability_id": "skill.generic_data_lookup",
                "client_message_id": "public-skill-hint",
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        repository = self.runtime.agent_loop_orchestrator._runs  # noqa: SLF001
        run = await repository.get_run_for_task(task_id)
        assert run is not None
        items = await repository.list_items(run.run_id)
        self.assertGreaterEqual(len(items), 2)
        self.assertEqual([item.kind.value for item in items[:2]], ["user_message", "skill_activation"])
        activation = json.loads(items[1].payload_json)
        self.assertEqual(activation["binding_mode"], "hint")
        self.assertEqual(
            activation["profile"]["capability_id"],
            "skill.generic_data_lookup",
        )
        events = await self.runtime.storage.list_events_for_task(task_id)
        hint_bound = [event for event in events if event.event_type == "skill.hint_bound"]
        self.assertEqual(len(hint_bound), 1)
        self.assertEqual(
            set(hint_bound[0].payload),
            {"capability_id", "safe_revision_ref", "profile_digest"},
        )
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual(
            [node.capability_id for node in nodes],
            ["agent.final_output"],
        )

    async def test_skill_hint_supersedes_legacy_pending_without_copying_its_facts(self) -> None:
        now = self.runtime._utcnow_naive()  # noqa: SLF001
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-hint-pending",
                username="acc-1",
                created_at=now,
                updated_at=now,
            )
        )
        await self.runtime.storage.save_pending_skill_context(
            PendingSkillContext(
                context_id="legacy-hint-context",
                conversation_id="conv-hint-pending",
                username="acc-1",
                capability_id="skill.generic_data_lookup",
                skill_name="generic-data-lookup",
                source_task_id="legacy-task",
                source_message_id="legacy-message",
                original_user_message="private old question",
                missing_requirements=("private-field",),
                assistant_message="private old answer",
                created_at=now,
                updated_at=now,
            )
        )

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-hint-pending",
                "content": "what can this skill do",
                "routing_mode": "hint",
                "capability_id": "skill.generic_data_lookup",
                "client_message_id": "hint-supersedes-pending",
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        pending = await self.runtime.storage.get_pending_skill_context(
            "legacy-hint-context"
        )
        self.assertEqual(pending.status, "superseded")
        events = await self.runtime.storage.list_events_for_task(task_id)
        transition = next(
            event
            for event in events
            if event.event_type == "pending_skill_context.superseded"
        )
        self.assertEqual(transition.payload["reason"], "new_skill_hint")
        self.assertEqual(transition.payload["count"], 1)
        self.assertNotIn("legacy-hint-context", json.dumps(transition.payload))
        task = await self.runtime.storage.get_task(task_id)
        self.assertNotIn("continued_from_pending_skill_context", task.summary)

    async def test_auto_consumes_one_legacy_pending_context_before_agent_initialization(self) -> None:
        now = self.runtime._utcnow_naive()  # noqa: SLF001
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-auto-pending",
                username="acc-1",
                created_at=now,
                updated_at=now,
            )
        )
        legacy = PendingSkillContext(
            context_id="legacy-auto-context",
            conversation_id="conv-auto-pending",
            username="acc-1",
            capability_id="skill.generic_data_lookup",
            skill_name="generic-data-lookup",
            source_task_id="legacy-task",
            source_message_id="legacy-message",
            original_user_message="lookup the selected data",
            missing_requirements=("query",),
            assistant_message="please provide the query",
            created_at=now,
            updated_at=now,
        )
        await self.runtime.storage.save_pending_skill_context(legacy)

        response = await self.submit_message(
            conversation_id="conv-auto-pending",
            content="query is rice",
            capability_id=None,
            client_message_id="auto-consumes-pending",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        pending = await self.runtime.storage.get_pending_skill_context(
            legacy.context_id
        )
        self.assertEqual(pending.status, "consumed")
        task = await self.runtime.storage.get_task(task_id)
        self.assertEqual(str(task.routing_mode), "auto")
        self.assertEqual(task.requested_capability_id, legacy.capability_id)
        events = await self.runtime.storage.list_events_for_task(task_id)
        transition = next(
            event
            for event in events
            if event.event_type == "pending_skill_context.consumed"
        )
        self.assertEqual(transition.payload["reason"], "legacy_pending_continued")
        self.assertEqual(transition.payload["count"], 1)

    async def test_concurrent_handed_off_wakeups_share_first_failure(self) -> None:
        task_id = "task-wakeup-singleflight-failure"
        identity = f"agent-run:{task_id}"
        record = SimpleNamespace(task_id=task_id, conversation_id="conv-1")
        initialized = SimpleNamespace(request=SimpleNamespace(task_id=task_id))
        self.runtime._submission_initialized_agent_runs[task_id] = initialized  # noqa: SLF001
        entered = asyncio.Event()
        release = asyncio.Event()
        schedule_calls = 0

        async def fail_schedule(_request) -> None:
            nonlocal schedule_calls
            schedule_calls += 1
            entered.set()
            await release.wait()
            raise RuntimeError("schedule_failed")

        with patch.object(self.runtime, "_schedule_execution", new=fail_schedule):
            first = asyncio.create_task(self.runtime.wakeup_agent(record, identity))
            await entered.wait()
            second = asyncio.create_task(self.runtime.wakeup_agent(record, identity))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release.set()
            first_result, second_result = await asyncio.gather(
                first,
                second,
                return_exceptions=True,
            )

        self.assertIsInstance(first_result, RuntimeError)
        self.assertIs(second_result, first_result)
        self.assertEqual(str(first_result), "schedule_failed")
        self.assertEqual(schedule_calls, 1)
        self.assertIn(task_id, self.runtime._submission_initialized_agent_runs)  # noqa: SLF001
        self.assertNotIn(identity, self.runtime._submission_woken_agent_ids)  # noqa: SLF001
        self.assertNotIn(identity, self.runtime._submission_wakeup_flights)  # noqa: SLF001

    async def test_concurrent_handed_off_wakeups_share_first_success(self) -> None:
        task_id = "task-wakeup-singleflight-success"
        identity = f"agent-run:{task_id}"
        record = SimpleNamespace(task_id=task_id, conversation_id="conv-1")
        initialized = SimpleNamespace(request=SimpleNamespace(task_id=task_id))
        self.runtime._submission_initialized_agent_runs[task_id] = initialized  # noqa: SLF001
        entered = asyncio.Event()
        release = asyncio.Event()
        schedule_calls = 0

        async def schedule(_request) -> None:
            nonlocal schedule_calls
            schedule_calls += 1
            entered.set()
            await release.wait()

        with patch.object(self.runtime, "_schedule_execution", new=schedule):
            first = asyncio.create_task(self.runtime.wakeup_agent(record, identity))
            await entered.wait()
            second = asyncio.create_task(self.runtime.wakeup_agent(record, identity))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release.set()
            await asyncio.gather(first, second)
            await self.runtime.wakeup_agent(record, identity)

        self.assertEqual(schedule_calls, 1)
        self.assertNotIn(task_id, self.runtime._submission_initialized_agent_runs)  # noqa: SLF001
        self.assertIn(identity, self.runtime._submission_woken_agent_ids)  # noqa: SLF001
        self.assertNotIn(identity, self.runtime._submission_wakeup_flights)  # noqa: SLF001

    async def test_handed_off_wakeup_retries_after_shared_failure(self) -> None:
        task_id = "task-wakeup-singleflight-retry"
        identity = f"agent-run:{task_id}"
        record = SimpleNamespace(task_id=task_id, conversation_id="conv-1")
        initialized = SimpleNamespace(request=SimpleNamespace(task_id=task_id))
        self.runtime._submission_initialized_agent_runs[task_id] = initialized  # noqa: SLF001
        entered = asyncio.Event()
        release = asyncio.Event()
        schedule_calls = 0

        async def fail_then_succeed(_request) -> None:
            nonlocal schedule_calls
            schedule_calls += 1
            if schedule_calls == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("schedule_failed")

        with patch.object(
            self.runtime,
            "_schedule_execution",
            new=fail_then_succeed,
        ):
            first = asyncio.create_task(self.runtime.wakeup_agent(record, identity))
            await entered.wait()
            second = asyncio.create_task(self.runtime.wakeup_agent(record, identity))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release.set()
            first_result, second_result = await asyncio.gather(
                first,
                second,
                return_exceptions=True,
            )
            self.assertIsInstance(first_result, RuntimeError)
            self.assertIs(second_result, first_result)
            await self.runtime.wakeup_agent(record, identity)
            await self.runtime.wakeup_agent(record, identity)

        self.assertEqual(schedule_calls, 2)
        self.assertNotIn(task_id, self.runtime._submission_initialized_agent_runs)  # noqa: SLF001
        self.assertIn(identity, self.runtime._submission_woken_agent_ids)  # noqa: SLF001
        self.assertNotIn(identity, self.runtime._submission_wakeup_flights)  # noqa: SLF001

    async def test_enforce_http_replay_projects_without_sql_task(self) -> None:
        sidecar = _SubmissionAuthoritySidecar()
        sidecar.omit_replay_claim = True
        self.runtime.storage._mcp_task_authority_mode = "enforce"  # noqa: SLF001
        self.runtime.storage._runtime_sidecar_client = sidecar  # noqa: SLF001
        coordinator = self.runtime._submission_admission_coordinator  # noqa: SLF001
        self.assertIsNotNone(coordinator)

        async def project_only(admitted, **_kwargs) -> object:
            if admitted.handle is not None:
                await self.runtime.storage.acknowledge_submission_projection(
                    SubmissionProjectionAcknowledgementRequest(
                        handle=admitted.handle,
                        projection_sha256=admitted.record.projection_sha256,
                        acknowledged_at=admitted.record.created_at,
                    )
                )
            return SimpleNamespace(recovered_count=int(admitted.handle is not None))

        message_id = "client-enforce-http-replay"
        with patch.object(
            coordinator,
            "continue_admitted",
            new=AsyncMock(side_effect=project_only),
        ) as continue_admitted:
            first = await self.submit_message(client_message_id=message_id)
            replay = await self.submit_message(client_message_id=message_id)

        self.assertEqual(continue_admitted.await_count, 2)
        self.assertIsNone(continue_admitted.await_args_list[1].args[0].handle)

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json(), first.json())
        with self.runtime.storage._session_factory() as session:  # noqa: SLF001
            self.assertIsNotNone(session.get(ConversationRow, "conv-1"))
            self.assertIsNotNone(session.get(MessageRow, message_id))
            self.assertIsNone(session.get(TaskRow, first.json()["task_id"]))

    async def test_same_message_id_in_other_conversation_conflicts_first(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        message_id = "client-submission-cross-conversation"

        first = await self.submit_message(client_message_id=message_id)
        original = await self.runtime.storage.get_message(message_id)
        with (
            patch.object(
                self.runtime,
                "_refresh_skills_for_new_conversation_if_needed",
                new=AsyncMock(side_effect=AssertionError("foreign skill refresh")),
            ),
            patch.object(
                self.runtime,
                "_refresh_mcp_for_new_conversation_if_needed",
                new=AsyncMock(side_effect=AssertionError("foreign MCP refresh")),
            ),
            patch.object(
                self.runtime.storage,
                "get_active_pending_skill_context",
                new=AsyncMock(side_effect=AssertionError("foreign pending read")),
            ),
        ):
            conflict = await self.submit_message(
                conversation_id="conv-other",
                client_message_id=message_id,
            )
        release.set()

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(
            conflict.json(), {"detail": {"code": "message_id_conflict"}}
        )
        self.assertEqual(await self.runtime.storage.get_message(message_id), original)
        self.assertEqual(
            await self.runtime.storage.list_tasks_for_conversation("conv-other"), []
        )
        await self.wait_for_terminal_task(first.json()["task_id"])

    async def test_same_message_id_from_other_owner_is_low_sensitive_conflict(
        self,
    ) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        message_id = "client-submission-cross-owner"

        first = await self.submit_message(client_message_id=message_id)
        original = await self.runtime.storage.get_message(message_id)
        await self.login("acc-2")
        with (
            patch.object(
                self.runtime,
                "_refresh_skills_for_new_conversation_if_needed",
                new=AsyncMock(side_effect=AssertionError("foreign skill refresh")),
            ),
            patch.object(
                self.runtime,
                "_refresh_mcp_for_new_conversation_if_needed",
                new=AsyncMock(side_effect=AssertionError("foreign MCP refresh")),
            ),
            patch.object(
                self.runtime.storage,
                "get_active_pending_skill_context",
                new=AsyncMock(side_effect=AssertionError("foreign pending read")),
            ),
        ):
            conflict = await self.submit_message(
                conversation_id="conv-other-owner",
                client_message_id=message_id,
            )
        release.set()

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(
            conflict.json(), {"detail": {"code": "message_id_conflict"}}
        )
        self.assertEqual(await self.runtime.storage.get_message(message_id), original)
        await self.login("acc-1")
        await self.wait_for_terminal_task(first.json()["task_id"])

    async def test_foreign_empty_conversation_rejects_before_context_side_effects(
        self,
    ) -> None:
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id="conv-private-empty",
                username="acc-1",
                created_at=self.runtime._utcnow_naive(),  # noqa: SLF001
                updated_at=self.runtime._utcnow_naive(),  # noqa: SLF001
            )
        )
        await self.runtime.storage.save_pending_skill_context(
            PendingSkillContext(
                context_id="private-pending-context",
                conversation_id="conv-private-empty",
                username="acc-1",
                capability_id="skill.private-owner-only",
                skill_name="private",
                source_task_id="private-source-task",
                source_message_id="private-source-message",
                original_user_message="private original text",
                missing_requirements=("private-field",),
                assistant_message="private assistant text",
                created_at=self.runtime._utcnow_naive(),  # noqa: SLF001
                updated_at=self.runtime._utcnow_naive(),  # noqa: SLF001
            )
        )
        await self.login("acc-2")

        with (
            patch.object(
                self.runtime,
                "_refresh_skills_for_new_conversation_if_needed",
                new=AsyncMock(side_effect=AssertionError("foreign skill refresh")),
            ),
            patch.object(
                self.runtime,
                "_refresh_mcp_for_new_conversation_if_needed",
                new=AsyncMock(side_effect=AssertionError("foreign MCP refresh")),
            ),
            patch.object(
                self.runtime.storage,
                "get_active_pending_skill_context",
                new=AsyncMock(side_effect=AssertionError("foreign pending read")),
            ),
        ):
            response = await self.submit_message(
                conversation_id="conv-private-empty",
                client_message_id="client-foreign-empty",
                capability_id=None,
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("private-owner-only", response.text)
        self.assertNotIn("private original text", response.text)
        self.assertNotIn("private assistant text", response.text)
        self.assertIsNone(
            await self.runtime.storage.get_message("client-foreign-empty")
        )
        self.assertEqual(
            await self.runtime.storage.list_tasks_for_conversation(
                "conv-private-empty"
            ),
            [],
        )

    async def test_concurrent_exact_submissions_converge_to_one_task(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        message_id = "client-submission-concurrent-exact"

        first, second = await asyncio.gather(
            self.submit_message(client_message_id=message_id),
            self.submit_message(client_message_id=message_id),
        )
        release.set()

        self.assertEqual([first.status_code, second.status_code], [202, 202])
        self.assertEqual(first.json(), second.json())
        tasks = await self.runtime.storage.list_tasks_for_conversation("conv-1")
        self.assertEqual(len(tasks), 1)
        events = await self.runtime.storage.list_events_for_task(tasks[0].task_id)
        for event_type in (
            "task.accepted",
            "mcp.rollout.route_assigned",
            "conversation.memory_built",
        ):
            self.assertEqual(
                sum(event.event_type == event_type for event in events),
                1,
                event_type,
            )
        await self.wait_for_terminal_task(tasks[0].task_id)

    async def test_concurrent_exact_replay_schedules_title_once(self) -> None:
        blocking_adapter, release_execution = blocking_mysql_adapter()
        release_title = asyncio.Event()

        async def generate_title(*_args, **_kwargs) -> str:
            await release_title.wait()
            return "基因型查询"

        title_generator = AsyncMock(side_effect=generate_title)
        await self.reconfigure_runtime(
            mysql_adapter=blocking_adapter,
            conversation_title_generator=title_generator,
        )
        message_id = "client-submission-concurrent-title"

        first, second = await asyncio.gather(
            self.submit_message(client_message_id=message_id),
            self.submit_message(client_message_id=message_id),
        )

        self.assertEqual([first.status_code, second.status_code], [202, 202])
        self.assertEqual(first.json(), second.json())

        async def title_started() -> bool:
            return title_generator.await_count == 1

        await self.wait_for_condition(title_started)
        self.assertEqual(title_generator.await_count, 1)

        release_title.set()
        release_execution.set()
        await self.wait_for_terminal_task(first.json()["task_id"])

    async def test_concurrent_different_message_ids_have_one_busy_winner(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        responses = await asyncio.gather(
            self.submit_message(client_message_id="client-concurrent-a"),
            self.submit_message(client_message_id="client-concurrent-b"),
        )
        release.set()

        self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
        accepted = next(response for response in responses if response.status_code == 202)
        self.assertEqual(
            len(await self.runtime.storage.list_tasks_for_conversation("conv-1")),
            1,
        )
        await self.wait_for_terminal_task(accepted.json()["task_id"])

    async def test_concurrent_same_id_with_drift_has_one_conflict(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        message_id = "client-concurrent-drift"

        responses = await asyncio.gather(
            self.submit_message(client_message_id=message_id, content="first"),
            self.submit_message(client_message_id=message_id, content="second"),
        )
        release.set()

        self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
        accepted = next(response for response in responses if response.status_code == 202)
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(
            conflict.json(), {"detail": {"code": "message_id_conflict"}}
        )
        stored = await self.runtime.storage.get_message(message_id)
        self.assertIn(stored.content, {"first", "second"})
        self.assertEqual(
            len(await self.runtime.storage.list_tasks_for_conversation("conv-1")),
            1,
        )
        await self.wait_for_terminal_task(accepted.json()["task_id"])

    async def test_exact_replay_while_task_is_active_returns_first_ids(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        message_id = "client-submission-exact-replay"

        schedule_initialized = self.runtime._schedule_initialized_execution  # noqa: SLF001
        with patch.object(
            self.runtime,
            "_schedule_initialized_execution",
            new=AsyncMock(wraps=schedule_initialized),
        ) as schedule:
            first = await self.submit_message(client_message_id=message_id)
            replay = await self.submit_message(client_message_id=message_id)
            self.assertEqual(schedule.await_count, 1)
            release.set()

            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertEqual(replay.json(), first.json())
            tasks = await self.runtime.storage.list_tasks_for_conversation("conv-1")
            messages = await self.runtime.storage.list_messages_for_conversation("conv-1")
            self.assertEqual(len(tasks), 1)
            self.assertEqual(
                [message.message_id for message in messages if str(message.role) == "user"],
                [message_id],
            )

            await self.wait_for_terminal_task(first.json()["task_id"])
            terminal_replay = await self.submit_message(client_message_id=message_id)
            self.assertEqual(terminal_replay.status_code, 202, terminal_replay.text)
            self.assertEqual(terminal_replay.json(), first.json())
            self.assertEqual(schedule.await_count, 1)

    async def test_exact_replay_with_open_interrupt_does_not_answer_it(self) -> None:
        message_id = "client-submission-replay-open-interrupt"
        content = "帮我查询一下"
        first = await self.submit_message(
            client_message_id=message_id,
            content=content,
            capability_id="skill.generic_data_lookup",
        )
        self.assertEqual(first.status_code, 202, first.text)
        payload = first.json()

        async def has_open_interrupt() -> bool:
            interrupts = await self.runtime.list_interrupts(payload["task_id"])
            return any(item["status"] == "open" for item in interrupts)

        await self.wait_for_condition(has_open_interrupt)
        before_interrupts = await self.runtime.list_interrupts(payload["task_id"])
        before_messages = await self.runtime.storage.list_messages_for_conversation(
            payload["conversation_id"]
        )

        replay = await self.submit_message(
            client_message_id=message_id,
            content=content,
            capability_id="skill.generic_data_lookup",
        )

        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(
            await self.runtime.list_interrupts(payload["task_id"]),
            before_interrupts,
        )
        self.assertEqual(
            await self.runtime.storage.list_messages_for_conversation(
                payload["conversation_id"]
            ),
            before_messages,
        )
        open_interrupt = next(
            item for item in before_interrupts if item["status"] == "open"
        )
        self.assertEqual(
            await self.runtime.storage.list_interrupt_answers(
                open_interrupt["interrupt_id"]
            ),
            [],
        )

    async def test_terminal_exact_replay_without_prior_active_retry_is_202(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        message_id = "client-submission-terminal-direct-replay"

        first = await self.submit_message(client_message_id=message_id)
        release.set()
        await self.wait_for_terminal_task(first.json()["task_id"])
        replay = await self.submit_message(client_message_id=message_id)

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json(), first.json())

    async def test_off_prepared_handoff_loader_uses_frozen_sql_facts(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message(
            client_message_id="client-submission-off-prepared",
        )
        payload = response.json()
        task = await self.runtime.storage.get_task(payload["task_id"])
        message = await self.runtime.storage.get_message(payload["message_id"])
        prepared = await self.runtime._prepared_agent_recovery_loader.load(  # noqa: SLF001
            username="acc-1",
            conversation_id=payload["conversation_id"],
            task_id=payload["task_id"],
            message_id=payload["message_id"],
            root_message_content=message.content,
        )
        release.set()

        self.assertEqual(response.status_code, 202, response.text)
        self.assertIsNotNone(task)
        self.assertIsNotNone(prepared)
        self.assertIsNotNone(prepared.initial_required_tool_name)
        self.assertEqual(
            prepared.bundle_revisions["skill_bundle_revision"],
            self.runtime._task_skill_bundle_revisions[payload["task_id"]],  # noqa: SLF001
        )
        self.assertEqual(prepared.available_mcp_servers, ())
        await self.wait_for_terminal_task(payload["task_id"])

    async def test_same_message_id_with_content_drift_conflicts_without_mutation(
        self,
    ) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)
        message_id = "client-submission-content-conflict"

        first = await self.submit_message(
            client_message_id=message_id,
            content="查询某个品种的基因型信息",
        )
        conflict = await self.submit_message(
            client_message_id=message_id,
            content="不同正文",
        )
        release.set()

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(
            conflict.json(),
            {"detail": {"code": "message_id_conflict"}},
        )
        self.assertEqual(conflict.status_code, 409)
        stored = await self.runtime.storage.get_message(message_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.content, "查询某个品种的基因型信息")
        self.assertEqual(
            len(await self.runtime.storage.list_tasks_for_conversation("conv-1")),
            1,
        )

        await self.wait_for_terminal_task(first.json()["task_id"])

    async def test_enforce_submission_fails_closed_without_admission_surface(
        self,
    ) -> None:
        sidecar = _IdentityRejectingSidecar()
        self.runtime.storage._mcp_task_authority_mode = "enforce"  # noqa: SLF001
        self.runtime.storage._runtime_sidecar_client = sidecar  # noqa: SLF001

        with patch.object(
            self.runtime,
            "_schedule_execution",
            new=AsyncMock(),
        ):
            response = await self.submit_message()

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json(),
            {"detail": {"code": "submission_admission_unavailable"}},
        )
        self.assertEqual(
            await self.runtime.storage.list_messages_for_conversation("conv-1"),
            [],
        )
        self.assertNotIn(
            "message_identity_reserve",
            [operation for operation, _payload in sidecar.calls],
        )

    async def test_message_identity_conflict_returns_low_sensitive_conflict(self) -> None:
        conflict = MessageIdentityConflictError()
        conflict.existing_conversation_id = "private-conversation"
        conflict.existing_task_id = "private-task"

        with patch.object(
            self.runtime,
            "submit_chat_message",
            side_effect=conflict,
        ):
            response = await self.submit_message()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"detail": {"code": "message_id_conflict"}},
        )

    async def test_message_submission_returns_accepted_and_rejects_busy_conversation(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        first = await self.submit_message()
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()
        self.assertEqual(first_payload["conversation_id"], "conv-1")
        self.assertEqual(first_payload["status"], "accepted")
        self.assertIn("task_id", first_payload)
        self.assertIn("message_id", first_payload)

        second = await self.submit_message(content="再来一条消息")
        self.assertEqual(second.status_code, 409)

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(first_payload["task_id"])
            return task is not None and str(task.status) == "running"

        await self.wait_for_condition(task_running)
        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": first_payload['task_id']})
        self.assertEqual(cancel_response.status_code, 202)
        release.set()
        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "cancelled")

    async def test_terminal_task_observed_before_pointer_clear_allows_next_submission(self) -> None:
        original_clear = self.runtime._clear_conversation_current_task
        clear_started = asyncio.Event()
        release_clear = asyncio.Event()
        blocked_once = False

        async def delayed_first_clear(conversation_id: str, task_id: str) -> None:
            nonlocal blocked_once
            if not blocked_once:
                blocked_once = True
                clear_started.set()
                await release_clear.wait()
            await original_clear(conversation_id, task_id)

        self.runtime._clear_conversation_current_task = delayed_first_clear  # type: ignore[method-assign]
        try:
            first = await self.submit_message(
                conversation_id="conv-terminal-pointer-interleave",
                content="first",
            )
            self.assertEqual(first.status_code, 202, first.text)
            await asyncio.wait_for(clear_started.wait(), timeout=2)
            first_task_id = first.json()["task_id"]
            first_task = await self.runtime.storage.get_task(first_task_id)
            self.assertIn(str(first_task.status), {"completed", "failed"})

            second = await self.submit_message(
                conversation_id="conv-terminal-pointer-interleave",
                content="second",
            )

            self.assertEqual(second.status_code, 202, second.text)
            self.assertNotEqual(second.json()["task_id"], first_task_id)
        finally:
            release_clear.set()
            self.runtime._clear_conversation_current_task = original_clear  # type: ignore[method-assign]

        await self.wait_for_terminal_task(first_task_id)
        await self.wait_for_terminal_task(second.json()["task_id"])

    async def test_waiting_input_task_can_be_answered_and_resumed(self) -> None:
        first = await self.submit_message(content="帮我查询一下", capability_id="skill.generic_data_lookup")
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()

        async def has_waiting_input_node() -> bool:
            nodes = await self.runtime.storage.list_task_nodes_for_task(first_payload["task_id"])
            return any(node.status == NodeStatus.WAITING_FOR_INPUT for node in nodes)

        await self.wait_for_condition(has_waiting_input_node)

        async def has_open_interrupt() -> bool:
            response = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
            return response.status_code == 200 and any(
                interrupt["status"] == "open" for interrupt in response.json()["interrupts"]
            )

        await self.wait_for_condition(has_open_interrupt)

        interrupts = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
        self.assertEqual(interrupts.status_code, 200)
        open_interrupt = interrupts.json()["interrupts"][0]
        self.assertEqual(open_interrupt["status"], "open")
        self.assertEqual(open_interrupt["reason_code"], "lookup_target_missing")
        events = await self.runtime.storage.list_events_for_task(first_payload["task_id"])
        waiting_events = [event for event in events if event.event_type == "node.waiting_for_input"]
        self.assertEqual(len(waiting_events), 1)
        self.assertEqual(waiting_events[0].node_id, open_interrupt["node_id"])
        self.assertEqual(waiting_events[0].payload["interrupt_id"], open_interrupt["interrupt_id"])
        self.assertEqual(waiting_events[0].payload["reason_code"], open_interrupt["reason_code"])

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": first_payload["conversation_id"],
                "content": "龙粳33",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-chat-interrupt-answer-legacy-test",
                "metadata": {"interrupt_id": open_interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        self.assertEqual(answer.json()["action"], "interrupt_resumed")

        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "completed")

    async def test_chat_message_answers_waiting_interrupt_without_creating_new_task(self) -> None:
        first = await self.submit_message(content="帮我查询一下", capability_id="skill.generic_data_lookup")
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()

        async def has_open_interrupt() -> bool:
            response = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
            return response.status_code == 200 and any(
                interrupt["status"] == "open" for interrupt in response.json()["interrupts"]
            )

        await self.wait_for_condition(has_open_interrupt)
        interrupts = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
        open_interrupt = interrupts.json()["interrupts"][0]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": first_payload["conversation_id"],
                "content": "龙粳33",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-chat-interrupt-answer-1",
                "metadata": {"interrupt_id": open_interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["conversation_id"], first_payload["conversation_id"])
        self.assertEqual(payload["task_id"], first_payload["task_id"])
        self.assertEqual(payload["message_id"], "client-chat-interrupt-answer-1")
        self.assertEqual(payload["action"], "interrupt_resumed")
        self.assertEqual(payload["interrupt_id"], open_interrupt["interrupt_id"])

        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "completed")
        conversations = await self.runtime.storage.list_tasks_for_conversation(first_payload["conversation_id"])
        self.assertEqual([task.task_id for task in conversations], [first_payload["task_id"]])

    async def test_chat_message_with_stale_interrupt_id_does_not_create_new_task(self) -> None:
        first = await self.submit_message(content="你好")
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()
        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "completed")

        stale = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": first_payload["conversation_id"],
                "content": "这本来想回答一个旧 interrupt",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"interrupt_id": "interrupt-stale"},
            },
        )
        self.assertEqual(stale.status_code, 400)
        self.assertIn("No active task is waiting for interrupt", stale.text)
        tasks = await self.runtime.storage.list_tasks_for_conversation(first_payload["conversation_id"])
        self.assertEqual([task.task_id for task in tasks], [first_payload["task_id"]])

    async def test_legacy_generic_data_lookup_native_capability_id_is_rejected(self) -> None:
        response = await self.submit_message(content="查询龙粳33", capability_id="legacy.query")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported capability_id", response.text)
