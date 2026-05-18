from __future__ import annotations

import json
from typing import Any

from src.core.enums import MessageRole
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from tests.api.support import APITestCase


class MainAgentLoopOrchestrationAPITest(APITestCase):
    async def test_platform_skill_plan_uses_single_shared_main_agent_runtime_and_finalizer(self) -> None:
        class FakeMainAgentLLM:
            instances: list["FakeMainAgentLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.calls: list[dict[str, Any]] = []
                FakeMainAgentLLM.instances.append(self)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
                on_reasoning_delta=None,
            ) -> str:
                self.calls.append({"method": "generate_text", "prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "受边界约束的高层工作流规划器" in prompt:
                    return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}]}, ensure_ascii=False)
                raise AssertionError(f"unexpected generate_text prompt: {prompt[:200]}")

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                self.calls.append({"method": "generate_text_with_thinking", "prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "受边界约束的高层工作流规划器" in prompt:
                    yield {
                        "answer": json.dumps(
                            {"nodes": [{"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}]},
                            ensure_ascii=False,
                        ),
                        "reasoning": None,
                    }
                    return
                yield {"answer": "主代理汇总。", "reasoning": None}

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake_main", "model": "fake-main-agent", "config_source": config_source, "reasoning_effort": reasoning_effort}

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)
            ),
            main_agent_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-main-agent"},
            main_agent_llm_client_factory=FakeMainAgentLLM,
            enable_llm_planner=True,
        )
        response = await self.submit_message(
            conversation_id="conv-platform-skill-main-provider",
            content="查询品种龙粳33的基因型信息",
            capability_id=None,
            metadata={"deep_thinking": True, "main_agent_reasoning_effort": "high"},
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(len(FakeMainAgentLLM.instances), 1)

        nodes = await self.runtime.storage.list_task_nodes_for_task(response.json()["task_id"])
        self.assertEqual([node.capability_id for node in nodes].count("main_agent.respond"), 1)
        self.assertEqual({node.capability_id for node in nodes}, {"skill.generic_data_lookup", "main_agent.respond"})

        calls = FakeMainAgentLLM.instances[0].calls
        self.assertEqual([call["method"] for call in calls], ["generate_text_with_thinking", "generate_text_with_thinking"])
        self.assertEqual(calls[-1]["thinking"], True)
        self.assertEqual(calls[-1]["reasoning_effort"], "high")

    async def test_multi_skill_plan_streams_intermediate_answers_and_persists_global_summary(self) -> None:
        responses = iter(("中间回答A", "中间回答B", "全局汇总"))

        def main_agent_streamer(prompt: str, **_kwargs):
            if "回答角色：全局最终汇总" in prompt:
                self.assertIn("中间回答A", prompt)
                self.assertIn("中间回答B", prompt)
                self.assertIn("已查询", prompt)
            return next(responses)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)
            ),
            planner_text_generator=lambda _prompt: json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"},
                        {"node_id": "design_data", "capability_id": "skill.generic_data_lookup"},
                    ]
                },
                ensure_ascii=False,
            ),
            main_agent_stream_generator=main_agent_streamer,
        )

        response = await self.submit_message(
            conversation_id="conv-multi-skill-final",
            content="先查龙粳33，再做随机区组",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes].count("main_agent.respond"), 3)
        events = await self.runtime.storage.list_events_for_task(task_id)
        final_events = [event for event in events if event.event_type == "main_agent.output_final"]
        self.assertEqual(
            [event.payload.get("response_role") for event in final_events],
            ["intermediate", "intermediate", "final"],
        )

        messages = await self.runtime.storage.list_messages_for_conversation("conv-multi-skill-final")
        assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
        self.assertEqual(len(assistant_messages), 1)
        self.assertEqual(assistant_messages[0].content, "全局汇总")

    async def test_planner_skill_plan_has_single_finalizer(self) -> None:
        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)
            ),
            planner_text_generator=lambda _prompt: json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"},
                        {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_data"]},
                    ]
                },
                ensure_ascii=False,
            ),
            main_agent_stream_generator=lambda _prompt, **_kwargs: "主代理汇总。",
        )
        response = await self.submit_message(conversation_id="conv-single-finalizer", content="查询龙粳33", capability_id=None)
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        nodes = await self.runtime.storage.list_task_nodes_for_task(response.json()["task_id"])
        self.assertEqual([node.capability_id for node in nodes].count("main_agent.respond"), 1)
        self.assertEqual({node.capability_id for node in nodes}, {"skill.generic_data_lookup", "main_agent.respond"})
