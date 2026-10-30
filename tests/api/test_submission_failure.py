from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from src.api.dto import SubmitMessageRequest
from src.storage.sqlalchemy_models import MessageRow
from src.core.models import TaskNode
from tests.api.support import APITestCase


class SubmissionFailureAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.reconfigure_runtime(
            main_agent_stream_generator=lambda _prompt: ["完成。"]
        )
        self.conversation_id = "conv-submission-failure"
        uploaded = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": self.conversation_id},
            files={"file": ("materials.csv", "id,value\nA,1\n", "text/csv")},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.upload_id = uploaded.json()["upload_id"]

    async def _submit(self, message_id="submission-failure-message"):
        return await self.submit_message(
            conversation_id=self.conversation_id,
            capability_id=None,
            content="分析文件",
            client_message_id=message_id,
            metadata={"upload_ids": [self.upload_id]},
        )

    async def test_partial_interrupt_failure_closes_the_preparation_node(self):
        self.runtime._conversation_file_selector_mode = "enforce_narrow"
        original = self.runtime.storage.save_task

        async def fail_running_task(task, *args, **kwargs):
            if str(task.status) == "running":
                raise RuntimeError("fault after preparation node creation")
            return await original(task, *args, **kwargs)

        with patch.object(
            self.runtime.storage, "save_task", side_effect=fail_running_task
        ):
            response = await self.submit_message(
                conversation_id="conv-partial-interrupt-failure",
                capability_id=None,
                client_message_id="partial-interrupt-failure",
                content="请分析材料文件。",
                metadata={
                    "file_requirement_profile": {
                        "required": True,
                        "expected_content": ["材料表"],
                    }
                },
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "failed")
        nodes = await self.runtime.storage.list_task_nodes_for_task(
            response.json()["task_id"]
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(str(nodes[0].status), "failed")
        self.assertIsNotNone(nodes[0].finished_at)
        with self.assertRaisesRegex(RuntimeError, "submission_preparation_closed"):
            await self.runtime.storage.save_task_node(
                TaskNode(
                    node_id=f"{response.json()['task_id']}:sheet_selection",
                    task_id=response.json()["task_id"],
                    capability_id="agent.sheet_selection",
                    status="running",
                )
            )

    async def test_failure_before_interrupt_open_closes_running_preparation(self):
        self.runtime._conversation_file_selector_mode = "enforce_narrow"
        with patch.object(
            self.runtime.interrupt_service,
            "open_interrupt",
            new=AsyncMock(side_effect=RuntimeError("interrupt not opened")),
        ):
            failed = await self.submit_message(
                conversation_id="conv-interrupt-not-opened",
                capability_id=None,
                client_message_id="interrupt-not-opened",
                content="请分析材料文件。",
                metadata={"file_requirement_profile": {"required": True}},
            )
        self.assertEqual(failed.status_code, 202, failed.text)
        task_id = failed.json()["task_id"]
        self.assertEqual(failed.json()["status"], "failed")
        self.assertEqual(
            str((await self.runtime.storage.get_task(task_id)).status), "failed"
        )
        self.assertIsNone(
            (
                await self.runtime.storage.get_conversation("conv-interrupt-not-opened")
            ).current_task_id
        )
        self.assertEqual(
            await self.runtime.storage.list_interrupts_for_task(task_id), []
        )
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([str(node.status) for node in nodes], ["failed"])

    async def test_failure_before_run_commit_ends_task_and_is_terminal_after_restart(
        self,
    ):
        with patch.object(
            self.runtime.agent_run_repository,
            "create_run",
            new=AsyncMock(side_effect=RuntimeError("Run not committed")),
        ):
            failed = await self._submit()
        self.assertEqual(failed.status_code, 202, failed.text)
        self.assertEqual(failed.json()["status"], "failed")
        task_id = failed.json()["task_id"]
        self.assertIsNone(
            await self.runtime.agent_run_repository.get_run_for_task(task_id)
        )
        await self.reconfigure_runtime(
            main_agent_stream_generator=lambda _: ["不应执行。"]
        )
        with patch.object(
            self.runtime.agent_run_repository,
            "create_run",
            new=AsyncMock(side_effect=AssertionError("failed task executed")),
        ):
            await self.runtime.start()
            replay = await self._submit()
        self.assertEqual(replay.json(), failed.json())
        uploads = await self.runtime.storage.list_conversation_file_resources(
            self.conversation_id, "acc-1"
        )
        self.assertEqual(
            [(file.file_id, file.status) for file in uploads],
            [(self.upload_id, "active")],
        )
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(
            task_id
        )
        self.assertEqual(
            [item.source_upload_id for item in attachments], [self.upload_id]
        )

    async def test_restart_initializes_run_created_before_task_transition(self):
        original = self.runtime.agent_run_repository.create_run

        async def commit_run_then_exit(run):
            await original(run)
            raise asyncio.CancelledError

        with patch.object(
            self.runtime.agent_run_repository,
            "create_run",
            side_effect=commit_run_then_exit,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self._submit()
        message = await self.runtime.storage.get_message("submission-failure-message")
        task = await self.runtime.storage.get_task(message.task_id)
        run = await self.runtime.agent_run_repository.get_run_for_task(task.task_id)
        self.assertEqual(str(task.status), "accepted")
        self.assertEqual(
            await self.runtime.agent_run_repository.list_items(run.run_id), ()
        )
        with self.runtime.storage._session_factory() as session:
            row = session.get(MessageRow, message.message_id)
            metadata = dict(row.message_metadata)
            state = dict(metadata["__maf_private_submission_state_v1"])
            state["claim_expires_at"] = (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            ).isoformat()
            row.message_metadata = {
                **metadata,
                "__maf_private_submission_state_v1": state,
            }
            session.commit()
        samples = []

        def generator(_prompt):
            samples.append(True)
            return ["完成。"]

        await self.reconfigure_runtime(main_agent_stream_generator=generator)
        await self.runtime.start()
        await self.runtime._await_existing_execution(task.task_id)
        self.assertEqual(samples, [True])
        self.assertEqual(
            str((await self.runtime.storage.get_task(task.task_id)).status), "completed"
        )
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        self.assertEqual(sum(str(item.kind) == "user_message" for item in items), 1)
        replay = await self._submit()
        self.assertEqual(replay.json()["task_id"], task.task_id)
        self.assertEqual(samples, [True])

    async def test_other_worker_recovers_handoff_committed_before_owner_exit(self):
        samples = []

        def generator(_prompt):
            samples.append(True)
            return ["完成。"]

        await self.reconfigure_runtime(main_agent_stream_generator=generator)
        other = self.build_runtime(main_agent_stream_generator=generator)
        other._agent_run_recovery_fatal_exit = Mock()
        original_ack = self.runtime.storage.acknowledge_submission_handoff

        async def commit_then_exit(request):
            await original_ack(request)
            raise asyncio.CancelledError

        try:
            with patch.object(
                self.runtime.storage,
                "acknowledge_submission_handoff",
                side_effect=commit_then_exit,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self._submit()
            request = SubmitMessageRequest(
                conversation_id=self.conversation_id,
                content="分析文件",
                client_message_id="submission-failure-message",
                metadata={"upload_ids": [self.upload_id]},
            )
            replay = await other.submit_chat_message(
                self.conversation_id, request, authenticated_username="acc-1"
            )
            run_id = f"agent-run:{replay['task_id']}"
            recovery = other._agent_run_lease_retry_tasks.get(run_id)
            self.assertIsNotNone(recovery)
            await asyncio.wait_for(asyncio.shield(recovery), timeout=5)
            self.assertEqual(samples, [True])
            task = await self.runtime.storage.get_task(replay["task_id"])
            self.assertEqual(str(task.status), "completed")
            items = await other.agent_run_repository.list_items(run_id)
            self.assertEqual(sum(str(item.kind) == "user_message" for item in items), 1)
            await other.submit_chat_message(
                self.conversation_id, request, authenticated_username="acc-1"
            )
            self.assertEqual(samples, [True])
            other._agent_run_recovery_fatal_exit.assert_not_called()
        finally:
            await other.shutdown()

    async def test_other_worker_retry_preserves_the_live_execution_lease(self):
        entered = asyncio.Event()
        finish_model = asyncio.Event()
        observing_lease = asyncio.Event()
        recheck_lease = asyncio.Event()
        samples = []

        async def generator(_prompt):
            samples.append(True)
            entered.set()
            await finish_model.wait()
            return ["完成。"]

        async def observe_until_owner_finishes(_delay):
            observing_lease.set()
            await recheck_lease.wait()

        await self.reconfigure_runtime(main_agent_stream_generator=generator)
        other = self.build_runtime(main_agent_stream_generator=generator)
        other._agent_run_recovery_fatal_exit = Mock()
        other._agent_run_lease_retry_sleep = observe_until_owner_finishes
        try:
            accepted = await self._submit()
            await asyncio.wait_for(entered.wait(), 5)
            task_id = accepted.json()["task_id"]
            run = await self.runtime.agent_run_repository.get_run_for_task(task_id)
            request = SubmitMessageRequest(
                conversation_id=self.conversation_id,
                content="分析文件",
                client_message_id="submission-failure-message",
                metadata={"upload_ids": [self.upload_id]},
            )
            replay = await other.submit_chat_message(
                self.conversation_id, request, authenticated_username="acc-1"
            )
            recovery = other._agent_run_lease_retry_tasks[run.run_id]
            await asyncio.wait_for(observing_lease.wait(), 5)
            self.assertEqual(replay["task_id"], task_id)
            self.assertEqual(samples, [True])
            latest = await other.agent_run_repository.get_run(run.run_id)
            self.assertEqual(latest.claim_token, run.claim_token)
            self.assertEqual(str(latest.status), "running")
            finish_model.set()
            await self.runtime._await_existing_execution(task_id)
            recheck_lease.set()
            await asyncio.wait_for(asyncio.shield(recovery), 5)
            self.assertEqual(samples, [True])
            self.assertEqual(
                str((await other.storage.get_task(task_id)).status), "completed"
            )
            other._agent_run_recovery_fatal_exit.assert_not_called()
        finally:
            finish_model.set()
            recheck_lease.set()
            await other.shutdown()

    async def test_partial_interrupt_write_is_recoverable_without_failing_the_task(
        self,
    ):
        self.runtime._conversation_file_selector_mode = "enforce_narrow"
        original = self.runtime.storage.save_interrupt

        for committed in (False, True):
            with self.subTest(committed=committed):
                conversation_id = f"partial-interrupt-{committed}"
                request = SubmitMessageRequest(
                    conversation_id=conversation_id,
                    content="请分析材料文件。",
                    client_message_id=conversation_id,
                    metadata={"file_requirement_profile": {"required": True}},
                )

                async def fail_interrupt(interrupt):
                    if committed:
                        await original(interrupt)
                    raise TimeoutError("interrupt write outcome unavailable")

                with patch.object(
                    self.runtime.storage, "save_interrupt", side_effect=fail_interrupt
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "submission_admission_unavailable"
                    ):
                        await self.runtime.submit_chat_message(
                            conversation_id, request, authenticated_username="acc-1"
                        )
                message = await self.runtime.storage.get_message(conversation_id)
                task = await self.runtime.storage.get_task(message.task_id)
                self.assertEqual(str(task.status), "running")
                replay = await self.runtime.submit_chat_message(
                    conversation_id, request, authenticated_username="acc-1"
                )
                self.assertEqual(replay["task_id"], task.task_id)
                interrupts = await self.runtime.storage.list_interrupts_for_task(
                    task.task_id
                )
                self.assertEqual([str(item.status) for item in interrupts], ["open"])
                nodes = await self.runtime.storage.list_task_nodes_for_task(
                    task.task_id
                )
                self.assertEqual(
                    [str(item.status) for item in nodes], ["waiting_for_input"]
                )
                events = await self.runtime.storage.list_events_for_task(task.task_id)
                self.assertNotIn("task.failed", [event.event_type for event in events])

    async def test_creator_waits_when_other_worker_claims_first(self):
        other = self.build_runtime(main_agent_stream_generator=lambda _: ["完成。"])
        created = asyncio.Event()
        creator_may_claim = asyncio.Event()
        creator_waiting = asyncio.Event()
        replay_claimed = asyncio.Event()
        replay_may_continue = asyncio.Event()
        replay_finished = asyncio.Event()
        original_create = self.runtime.storage._run_submission_admission
        original_replay = other.storage.admit_submission

        async def pause_creator(request):
            result = await original_create(request)
            created.set()
            await creator_may_claim.wait()
            return result

        async def pause_replay(request):
            result = await original_replay(request)
            self.assertIsNotNone(result.handle)
            replay_claimed.set()
            await replay_may_continue.wait()
            return result

        async def wait_for_replay(_deadline):
            creator_waiting.set()
            await replay_finished.wait()

        request = SubmitMessageRequest(
            conversation_id=self.conversation_id,
            content="分析文件",
            client_message_id="creator-claim-race",
            metadata={"upload_ids": [self.upload_id]},
        )
        pending = []
        try:
            with (
                patch.object(
                    self.runtime.storage,
                    "_run_submission_admission",
                    side_effect=pause_creator,
                ),
                patch.object(
                    other.storage, "admit_submission", side_effect=pause_replay
                ),
                patch.object(
                    self.runtime._submission_admission_coordinator,
                    "_wait_until",
                    side_effect=wait_for_replay,
                ),
            ):
                first = asyncio.create_task(
                    self.runtime.submit_chat_message(
                        self.conversation_id, request, authenticated_username="acc-1"
                    )
                )
                pending.append(first)
                await asyncio.wait_for(created.wait(), 5)
                second = asyncio.create_task(
                    other.submit_chat_message(
                        self.conversation_id, request, authenticated_username="acc-1"
                    )
                )
                pending.append(second)
                await asyncio.wait_for(replay_claimed.wait(), 5)
                creator_may_claim.set()
                await asyncio.wait_for(creator_waiting.wait(), 5)
                replay_may_continue.set()
                second_result = await asyncio.wait_for(second, 5)
                replay_finished.set()
                first_result = await asyncio.wait_for(first, 5)
            self.assertEqual(first_result["task_id"], second_result["task_id"])
            await other._await_existing_execution(second_result["task_id"])
            self.assertEqual(
                len(
                    await other.storage.list_tasks_for_conversation(
                        self.conversation_id
                    )
                ),
                1,
            )
        finally:
            creator_may_claim.set()
            replay_may_continue.set()
            replay_finished.set()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await other.shutdown()

    async def test_preparation_failure_ends_task_preserves_upload_and_releases_conversation(
        self,
    ):
        with (
            patch.object(
                self.runtime,
                "compute_selector_decision",
                new=AsyncMock(side_effect=RuntimeError("selector failed")),
            ),
            patch.object(
                self.runtime.event_broker, "publish", new=AsyncMock()
            ) as publish,
        ):
            failed = await self._submit()
        self.assertEqual(
            [call.args[0].event_type for call in publish.await_args_list],
            ["task.failed"],
        )
        self.assertEqual(failed.status_code, 202, failed.text)
        task_id = failed.json()["task_id"]
        self.assertEqual(failed.json()["status"], "failed")
        task = await self.runtime.storage.get_task(task_id)
        self.assertEqual(str(task.status), "failed")
        conversation = await self.runtime.storage.get_conversation(self.conversation_id)
        self.assertIsNone(conversation.current_task_id)
        uploads = await self.runtime.storage.list_conversation_file_resources(
            self.conversation_id, "acc-1"
        )
        self.assertEqual(
            [(file.file_id, file.status) for file in uploads],
            [(self.upload_id, "active")],
        )
        self.assertIsNone(
            await self.runtime.agent_run_repository.get_run_for_task(task_id)
        )
        with patch.object(
            self.runtime,
            "compute_selector_decision",
            new=AsyncMock(side_effect=AssertionError("terminal submission replayed")),
        ):
            replay = await self._submit()
        self.assertEqual(replay.json(), failed.json())
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertEqual([event.event_type for event in events], ["task.failed"])

        fresh = await self._submit("fresh-after-failure")
        self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assertNotEqual(fresh.json()["task_id"], task_id)
        await self.runtime._await_existing_execution(fresh.json()["task_id"])
        self.assertEqual(
            str((await self.runtime.storage.get_task(fresh.json()["task_id"])).status),
            "completed",
        )

    async def test_lost_admission_response_retries_frozen_request_once(self):
        original = self.runtime.storage.admit_submission

        async def lose_response(request):
            await original(request)
            raise TimeoutError("response lost after commit")

        with patch.object(
            self.runtime.storage, "admit_submission", side_effect=lose_response
        ):
            lost = await self._submit()
        self.assertEqual(lost.status_code, 503)
        first = await self.runtime.storage.get_message("submission-failure-message")
        self.assertIsNotNone(first)
        accepted = await self._submit()
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["task_id"], first.task_id)
        await self.runtime._await_existing_execution(first.task_id)
        replay = await self._submit()
        self.assertEqual(replay.json()["task_id"], first.task_id)
        self.assertEqual(
            len(
                await self.runtime.storage.list_tasks_for_conversation(
                    self.conversation_id
                )
            ),
            1,
        )
        items = await self.runtime.agent_run_repository.list_items(
            f"agent-run:{first.task_id}"
        )
        self.assertEqual(sum(str(item.kind) == "user_message" for item in items), 1)

    async def test_lost_failure_response_replays_terminal_task_without_reexecution(
        self,
    ):
        original = self.runtime.storage.fail_submission

        async def lose_response(request):
            await original(request)
            raise TimeoutError("failure response lost after commit")

        with patch.object(
            self.runtime,
            "compute_selector_decision",
            new=AsyncMock(side_effect=RuntimeError("selector failed")),
        ):
            with patch.object(
                self.runtime.storage, "fail_submission", side_effect=lose_response
            ):
                lost = await self._submit()
        self.assertEqual(lost.status_code, 503)
        message = await self.runtime.storage.get_message("submission-failure-message")
        self.assertEqual(lost.json()["detail"]["task_id"], message.task_id)
        with patch.object(
            self.runtime,
            "compute_selector_decision",
            new=AsyncMock(side_effect=AssertionError("terminal task executed twice")),
        ):
            replay = await self._submit()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["task_id"], message.task_id)
        self.assertEqual(replay.json()["status"], "failed")
        self.assertEqual(
            len(await self.runtime.storage.list_events_for_task(message.task_id)), 1
        )
        self.assertIsNone(
            await self.runtime.agent_run_repository.get_run_for_task(message.task_id)
        )

    async def test_error_after_agent_run_creation_preserves_run_and_retries_once(self):
        original = self.runtime.initialize_agent_handoff

        async def lose_response(*args):
            await original(*args)
            raise TimeoutError("run response lost")

        with patch.object(
            self.runtime, "initialize_agent_handoff", side_effect=lose_response
        ):
            lost = await self._submit()
        self.assertEqual(lost.status_code, 503, lost.text)
        message = await self.runtime.storage.get_message("submission-failure-message")
        run = await self.runtime.agent_run_repository.get_run_for_task(message.task_id)
        self.assertIsNotNone(run)
        self.assertNotEqual(
            str((await self.runtime.storage.get_task(message.task_id)).status), "failed"
        )
        accepted = await self._submit()
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["task_id"], message.task_id)
        await self.runtime._await_existing_execution(message.task_id)
        self.assertEqual(
            str((await self.runtime.storage.get_task(message.task_id)).status),
            "completed",
        )
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        self.assertEqual(sum(str(item.kind) == "user_message" for item in items), 1)

    async def test_restart_recovers_partial_sql_submission_from_frozen_upload_refs(
        self,
    ):
        with patch.object(
            self.runtime,
            "compute_selector_decision",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self._submit()
        message = await self.runtime.storage.get_message("submission-failure-message")
        with self.runtime.storage._session_factory() as session:
            row = session.get(MessageRow, message.message_id)
            metadata = dict(row.message_metadata)
            state = dict(metadata["__maf_private_submission_state_v1"])
            state["claim_expires_at"] = (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            ).isoformat()
            row.message_metadata = {
                **metadata,
                "__maf_private_submission_state_v1": state,
            }
            session.commit()
        await self.reconfigure_runtime(
            main_agent_stream_generator=lambda _prompt: ["完成。"]
        )
        await self.runtime.start()
        await self.runtime._await_existing_execution(message.task_id)
        task = await self.runtime.storage.get_task(message.task_id)
        self.assertEqual(str(task.status), "completed")
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(
            message.task_id
        )
        self.assertEqual(
            [item.source_upload_id for item in attachments], [self.upload_id]
        )
        self.assertEqual(
            len(
                await self.runtime.storage.list_tasks_for_conversation(
                    self.conversation_id
                )
            ),
            1,
        )
