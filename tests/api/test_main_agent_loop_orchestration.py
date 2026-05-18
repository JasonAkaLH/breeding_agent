from __future__ import annotations

import json
import textwrap
from typing import Any

from src.core.enums import ArtifactType, EventVisibility, MessageRole, TaskStatus
from src.core.models import Artifact, Conversation, EventRecord, Task
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from tests.api.support import APITestCase


class MainAgentLoopOrchestrationAPITest(APITestCase):
    async def test_assistant_history_sync_uses_final_event_for_roleless_artifacts(self) -> None:
        await self.runtime.storage.save_conversation(Conversation("conv-history-final-event", "acc-1"))
        await self.runtime.storage.save_task(
            Task(
                "task-history-final-event",
                "conv-history-final-event",
                root_message_id="msg-history-final-event",
                status=TaskStatus.COMPLETED,
            )
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                "aaa-intermediate",
                "task-history-final-event",
                "node-intermediate",
                ArtifactType.TEXT,
                "局部回答",
                is_complete=True,
            )
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                "zzz-final",
                "task-history-final-event",
                "node-final",
                ArtifactType.TEXT,
                "全局汇总",
                is_complete=True,
            )
        )
        await self.runtime.storage.append_event(
            EventRecord(
                "evt-final",
                "conv-history-final-event",
                "task-history-final-event",
                node_id="node-final",
                event_type="main_agent.output_final",
                payload={"response_role": "final"},
                visibility=EventVisibility.FRONTEND,
            )
        )

        await self.runtime.sync_assistant_history_message_for_task("task-history-final-event", "conv-history-final-event")

        message = await self.runtime.storage.get_message("task-history-final-event:assistant")
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "全局汇总")

    async def test_assistant_history_sync_tolerates_duplicate_write_race_only_after_message_exists(self) -> None:
        await self.runtime.storage.save_conversation(Conversation("conv-history-race", "acc-1"))
        await self.runtime.storage.save_task(
            Task(
                "task-history-race",
                "conv-history-race",
                root_message_id="msg-history-race",
                status=TaskStatus.COMPLETED,
            )
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                "art-final",
                "task-history-race",
                "node-final",
                ArtifactType.TEXT,
                "全局汇总",
                is_complete=True,
            )
        )
        await self.runtime.storage.append_event(
            EventRecord(
                "evt-final-race",
                "conv-history-race",
                "task-history-race",
                node_id="node-final",
                event_type="main_agent.output_final",
                payload={"response_role": "final"},
                visibility=EventVisibility.FRONTEND,
            )
        )

        original_save_message = self.runtime.storage.save_message
        calls = 0

        async def save_then_raise_once(message):
            nonlocal calls
            calls += 1
            if calls == 1:
                await original_save_message(message)
                raise RuntimeError("simulated duplicate write race")
            return await original_save_message(message)

        self.runtime.storage.save_message = save_then_raise_once

        await self.runtime.sync_assistant_history_message_for_task("task-history-race", "conv-history-race")

        message = await self.runtime.storage.get_message("task-history-race:assistant")
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "全局汇总")
        self.assertEqual(calls, 1)

    async def test_assistant_history_sync_reraises_save_failure_when_message_is_absent(self) -> None:
        await self.runtime.storage.save_conversation(Conversation("conv-history-save-fail", "acc-1"))
        await self.runtime.storage.save_task(
            Task(
                "task-history-save-fail",
                "conv-history-save-fail",
                root_message_id="msg-history-save-fail",
                status=TaskStatus.COMPLETED,
            )
        )
        await self.runtime.storage.save_artifact(
            Artifact(
                "art-final",
                "task-history-save-fail",
                "node-final",
                ArtifactType.TEXT,
                "全局汇总",
                is_complete=True,
            )
        )

        async def fail_without_write(_message):
            raise RuntimeError("simulated persistent storage failure")

        self.runtime.storage.save_message = fail_without_write

        with self.assertRaisesRegex(RuntimeError, "simulated persistent storage failure"):
            await self.runtime.sync_assistant_history_message_for_task(
                "task-history-save-fail",
                "conv-history-save-fail",
            )

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

    async def test_multi_skill_plan_streams_only_final_answer_and_persists_global_summary(self) -> None:
        prompts: list[str] = []

        def main_agent_streamer(prompt: str, **_kwargs):
            prompts.append(prompt)
            if "回答角色：全局最终汇总" in prompt:
                self.assertIn("已查询", prompt)
            return "全局汇总"

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
        self.assertEqual([node.capability_id for node in nodes].count("main_agent.respond"), 1)
        self.assertEqual(len(prompts), 1)
        events = await self.runtime.storage.list_events_for_task(task_id)
        final_events = [event for event in events if event.event_type == "main_agent.output_final"]
        self.assertEqual(
            [event.payload.get("response_role") for event in final_events],
            ["final"],
        )

        messages = await self.runtime.storage.list_messages_for_conversation("conv-multi-skill-final")
        assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
        self.assertEqual(len(assistant_messages), 1)
        self.assertEqual(assistant_messages[0].content, "全局汇总")

    async def test_multi_skill_finalizer_sees_answer_only_skill_response_text(self) -> None:
        skill_dir = self.default_project_skill_root / "rcbd-answer"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "answer.py").write_text(
            textwrap.dedent(
                """\
                import json
                import sys

                json.load(sys.stdin)
                print(json.dumps({"answer": "RCBD 设计已完成：共 30 行 fieldbook"}, ensure_ascii=False))
                """
            ),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: rcbd-answer
                capability_id: skill.rcbd_answer
                description: 测试用 RCBD answer-only Skill。
                triggers:
                  - 随机区组
                scripts:
                  - name: answer
                    path: scripts/answer.py
                    runtime: python
                    auto_run: true
                    outputs:
                      required:
                        - answer
                execution:
                  mode: python_subprocess
                  answer_mode: requires_finalizer
                outputs:
                  required:
                    - answer
                ---

                # RCBD Answer
                """
            ),
            encoding="utf-8",
        )
        prompts: list[str] = []
        def main_agent_streamer(prompt: str, **_kwargs):
            prompts.append(prompt)
            if "回答角色：全局最终汇总" in prompt:
                self.assertIn("已查询", prompt)
                self.assertIn("RCBD 设计已完成：共 30 行 fieldbook", prompt)
                self.assertIn("response_text", prompt)
            return "全局汇总"

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)
            ),
            planner_text_generator=lambda _prompt: json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"},
                        {"node_id": "design_data", "capability_id": "skill.rcbd_answer"},
                    ]
                },
                ensure_ascii=False,
            ),
            main_agent_stream_generator=main_agent_streamer,
        )

        response = await self.submit_message(
            conversation_id="conv-multi-skill-answer-only-finalizer",
            content="先查龙粳33，再做随机区组",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertIn("skill.rcbd_answer", {node.capability_id for node in nodes})
        self.assertEqual([node.capability_id for node in nodes].count("main_agent.respond"), 1)
        self.assertFalse(any("回答范围：skill:skill.rcbd_answer" in prompt for prompt in prompts))
        self.assertTrue(any("回答角色：全局最终汇总" in prompt for prompt in prompts))

    async def test_multi_skill_with_direct_skill_persists_global_final_answer(self) -> None:
        skill_dir = self.default_project_skill_root / "direct-answer"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "answer.py").write_text(
            textwrap.dedent(
                """\
                import json
                import sys

                json.load(sys.stdin)
                print(json.dumps({"answer": "直接回答不应成为最终聊天正文"}, ensure_ascii=False))
                """
            ),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: direct-answer
                capability_id: skill.direct_answer
                description: 测试用 direct answer Skill。
                triggers:
                  - direct
                scripts:
                  - name: answer
                    path: scripts/answer.py
                    runtime: python
                    auto_run: true
                    outputs:
                      required:
                        - answer
                execution:
                  mode: python_subprocess
                  answer_mode: direct
                outputs:
                  required:
                    - answer
                ---

                # Direct Answer
                """
            ),
            encoding="utf-8",
        )
        prompts: list[str] = []

        def main_agent_streamer(prompt: str, **_kwargs):
            prompts.append(prompt)
            if "回答角色：全局最终汇总" in prompt:
                self.assertIn("已查询", prompt)
                self.assertIn("直接回答不应成为最终聊天正文", prompt)
            return "全局汇总"

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)
            ),
            planner_text_generator=lambda _prompt: json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"},
                        {"node_id": "direct_answer", "capability_id": "skill.direct_answer"},
                    ]
                },
                ensure_ascii=False,
            ),
            main_agent_stream_generator=main_agent_streamer,
        )

        response = await self.submit_message(
            conversation_id="conv-multi-skill-direct-global-final",
            content="先查龙粳33，再 direct",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes].count("main_agent.respond"), 1)
        self.assertTrue(any("回答角色：全局最终汇总" in prompt for prompt in prompts))
        messages = await self.runtime.storage.list_messages_for_conversation("conv-multi-skill-direct-global-final")
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
