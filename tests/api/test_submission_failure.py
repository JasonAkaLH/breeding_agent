from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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
