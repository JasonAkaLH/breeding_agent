from __future__ import annotations

import json

from src.core.enums import MessageRole, TaskStatus
from src.core.models import Conversation, Message, Task
from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentItemKind,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentToolCall,
    AgentUsage,
    AgentUserMessageCommit,
)

from tests.api.support import GENERIC_DATA_SKILL_ID
from tests.e2e.support import E2EAPITestCase


class AgentLoopCutoverE2ETest(E2EAPITestCase):
    async def test_startup_retires_missing_prepared_authority_before_call_recovery(self) -> None:
        task_id = "task-agent-startup-recovery"
        conversation_id = "conv-agent-startup-recovery"
        message_id = "msg-agent-startup-recovery"
        now = self.runtime._utcnow_naive()
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id=conversation_id,
                username="acc-1",
                current_task_id=task_id,
                created_at=now,
                updated_at=now,
            )
        )
        await self.runtime.storage.save_message(
            Message(
                message_id=message_id,
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content="不要重放已经可能执行过的调用",
                task_id=task_id,
                created_at=now,
            )
        )
        await self.runtime.storage.save_task(
            Task(
                task_id=task_id,
                conversation_id=conversation_id,
                root_message_id=message_id,
                status=TaskStatus.RUNNING,
                summary="不要重放已经可能执行过的调用",
                created_at=now,
                updated_at=now,
            )
        )
        binding = AgentModelBinding("api-test", reasoning_effort="minimal")
        run = await self.runtime.agent_run_repository.create_run(
            AgentRun(
                run_id=f"agent-run:{task_id}",
                task_id=task_id,
                conversation_id=conversation_id,
                status=AgentRunStatus.RUNNING,
                binding=binding,
            )
        )
        initialized = await self.runtime.agent_run_repository.commit_agent_user_message(
            AgentUserMessageCommit(
                run_id=run.run_id,
                expected_revision=run.revision,
                expected_claim_token=None,
                text="不要重放已经可能执行过的调用",
            )
        )
        await self.runtime.agent_run_repository.commit_agent_sample(
            AgentSampleCommit(
                run_id=run.run_id,
                expected_revision=initialized.run.revision,
                expected_claim_token=None,
                sample=AgentSample(
                    sample_id="sample-startup-recovery",
                    binding=binding,
                    visible_text="",
                    tool_calls=(
                        AgentToolCall(
                            "call-startup-recovery",
                            "skill_generic_data_lookup_1dfb3374f284",
                            '{"query":"龙粳33"}',
                            0,
                        ),
                    ),
                    usage=AgentUsage(status="usage_unavailable"),
                    finish=AgentFinishMetadata("tool_calls", 1),
                ),
                capability_ids_by_tool_name={
                    "skill_generic_data_lookup_1dfb3374f284": GENERIC_DATA_SKILL_ID
                },
            )
        )

        await self.runtime.start()

        recovered = await self.runtime.storage.get_task(task_id)
        self.assertEqual(recovered.status, TaskStatus.FAILED)
        recovered_run = await self.runtime.agent_run_repository.get_run(run.run_id)
        assert recovered_run is not None
        self.assertEqual(recovered_run.status, AgentRunStatus.FAILED)
        self.assertEqual(
            recovered_run.terminal_reason_code,
            "agent_skill_bundle_revision_retired",
        )
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        results = [
            item
            for item in items
            if item.kind is AgentItemKind.TOOL_RESULT
            and item.state.value == "committed"
        ]
        self.assertEqual(results, [])
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertNotIn(
            "skill.execution_started",
            {event.event_type for event in events},
        )

    async def test_ordinary_agent_can_choose_skill_from_catalog(self) -> None:
        def agent_fixture(prompt: str, **_kwargs):
            if '"outcome":"completed"' in prompt:
                return "已根据 Skill 结果完成。"
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "capability_id": GENERIC_DATA_SKILL_ID,
                            "arguments": {"query": "龙粳33"},
                        }
                    ]
                },
                ensure_ascii=False,
            )

        await self.reconfigure_runtime(main_agent_stream_generator=agent_fixture)
        response = await self.submit_message(
            conversation_id="conv-agent-cutover-auto-skill",
            content="查询龙粳33",
            capability_id=None,
        )
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])

        self.assertEqual(terminal["status"], "completed")
        nodes = await self.runtime.storage.list_task_nodes_for_task(
            response.json()["task_id"]
        )
        self.assertEqual(
            [node.capability_id for node in nodes],
            [GENERIC_DATA_SKILL_ID, "agent.final_output"],
        )

    async def test_ordinary_submit_uses_one_agent_run_and_atomic_final(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-agent-cutover-ordinary",
            content="你好，请直接回答。",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        run = await self.runtime.agent_task_projection.get_agent_run(task_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.status, AgentRunStatus.COMPLETED)
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        self.assertEqual(items[0].kind, AgentItemKind.USER_MESSAGE)
        self.assertTrue(any(item.kind is AgentItemKind.ASSISTANT_MESSAGE for item in items))
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes], ["agent.final_output"])
        self.assertFalse(hasattr(self.runtime, "workflow_provider"))
        self.assertFalse(hasattr(self.runtime, "orchestration_service"))

    async def test_explicit_skill_is_required_first_call_in_same_run(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-agent-cutover-skill",
            content="查询龙粳33",
            capability_id=GENERIC_DATA_SKILL_ID,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        capability_ids = [node.capability_id for node in nodes]
        self.assertIn(GENERIC_DATA_SKILL_ID, capability_ids)
        self.assertIn("agent.final_output", capability_ids)
        self.assertNotIn("main_agent.respond", capability_ids)
        run = await self.runtime.agent_task_projection.get_agent_run(task_id)
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        self.assertEqual(
            sum(item.kind is AgentItemKind.TOOL_CALL for item in items),
            1,
        )
        self.assertEqual(
            sum(item.kind is AgentItemKind.TOOL_RESULT for item in items),
            1,
        )

    async def test_graph_projection_returns_compatibility_empty_edges(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-agent-cutover-graph",
            content="直接回答。",
            capability_id=None,
        )
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)

        graph = await self.client.get(f"/api/v1/tasks/{task_id}/graph")

        self.assertEqual(graph.status_code, 200, graph.text)
        self.assertEqual(graph.json()["edges"], [])
