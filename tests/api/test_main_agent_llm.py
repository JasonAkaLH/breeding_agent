from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from tests.api.support import APITestCase


class MainAgentLLMAPITest(APITestCase):
    async def test_default_message_uses_main_agent_and_streams_output_events(self) -> None:
        async def streamer(prompt: str):
            yield "你好"
            yield "，我是主代理"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        response = await self.client.post(
            "/api/v1/conversations/conv-main/messages",
            json={
                "account_id": "acc-1",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 1)

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        seen_types = set()
        deltas: list[str] = []
        while "task.completed" not in seen_types:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            seen_types.add(event.event_type)
            if event.event_type == "main_agent.output_delta":
                deltas.append(event.payload["delta"])

        self.assertEqual(deltas, ["你好", "，我是主代理"])
        self.assertIn("main_agent.output_final", seen_types)

    async def test_main_agent_reasoning_content_is_exposed_as_frontend_events(self) -> None:
        async def streamer(prompt: str, *, reasoning_effort: str = "minimal", thinking: bool = False):
            yield {"reasoning": "先分析", "answer": None}
            yield {"answer": "最终回答", "reasoning": None}

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        response = await self.client.post(
            "/api/v1/conversations/conv-main-reasoning/messages",
            json={
                "account_id": "acc-1",
                "content": "请深度思考",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"deep_thinking": True},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        frontend_reasoning = [
            event.payload["delta"]
            for event in events
            if event.event_type == "main_agent.reasoning_delta" and str(event.visibility) == "frontend"
        ]
        frontend_answer = [
            event.payload["delta"]
            for event in events
            if event.event_type == "main_agent.output_delta" and str(event.visibility) == "frontend"
        ]

        self.assertEqual(frontend_reasoning, ["先分析"])
        self.assertEqual(frontend_answer, ["最终回答"])

    async def test_explicit_sql_query_capability_runs_internal_filtering_node(self) -> None:
        response = await self.submit_message(content="查询品种龙粳33的基因型信息", capability_id="skill.sql_query")
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)

    async def test_explicit_sql_query_capability_bypasses_llm_planner(self) -> None:
        def planner(_prompt: str) -> str:
            raise AssertionError("Explicit capability routing must not call the LLM planner.")

        async def streamer(_prompt: str):
            yield "unused"

        await self.reconfigure_runtime(
            planner_text_generator=planner,
            main_agent_stream_generator=streamer,
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-explicit-sql-bypass",
            content="查询品种龙粳33的基因型信息",
            capability_id="skill.sql_query",
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)

    async def test_default_database_question_auto_builds_sqlquery_then_main_agent_dag(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "这是主代理整理后的数据库答案"

        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.sql_query"}]})

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, planner_text_generator=planner, skill_roots=None)
        response = await self.client.post(
            "/api/v1/conversations/conv-auto-sql/messages",
            json={
                "account_id": "acc-1",
                "content": "查询龙粳33的详细审定信息",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        nodes_by_capability = {node.capability_id: node for node in nodes}
        self.assertIn("skill.sql_query", nodes_by_capability)
        self.assertIn("main_agent.respond", nodes_by_capability)
        edges = await self.runtime.storage.list_task_edges(task_id)
        self.assertIn(
            (nodes_by_capability["skill.sql_query"].node_id, nodes_by_capability["main_agent.respond"].node_id),
            {(edge.from_node_id, edge.to_node_id) for edge in edges},
        )
        self.assertIn("上游能力结果上下文", prompts[-1])
        self.assertIn("龙粳33", prompts[-1])
        self.assertIn("rows", prompts[-1])
        self.assertIn("filter_source", prompts[-1])

    async def test_default_database_question_uses_injected_llm_planner(self) -> None:
        prompts: list[str] = []
        planner_prompts: list[str] = []

        async def planner(prompt: str) -> str:
            planner_prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.sql_query"},
                        {
                            "node_id": "answer_user",
                            "capability_id": "main_agent.respond",
                            "depends_on": ["query_data"],
                        },
                    ]
                }
            )

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "这是 LLM Planner 规划后的数据库答案"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.client.post(
            "/api/v1/conversations/conv-llm-planner-sql/messages",
            json={
                "account_id": "acc-1",
                "content": "查询龙粳33的详细审定信息",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)
        self.assertIn("skill.sql_query", planner_prompts[0])
        self.assertNotIn("sql_query.sql_generate", planner_prompts[0])
        events = await self.runtime.storage.list_events_for_task(task_id)
        plan_event = next(event for event in events if event.event_type == "workflow.plan_built")
        self.assertEqual(plan_event.payload["metadata"]["route"], "llm_planner")
        self.assertFalse(plan_event.payload["metadata"]["planner_fallback_used"])
        self.assertIn("上游能力结果上下文", prompts[-1])

    async def test_default_message_uses_llm_planner_single_main_agent_plan(self) -> None:
        prompts: list[str] = []

        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]})

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "planner 单主代理回答"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.client.post(
            "/api/v1/conversations/conv-llm-planner-chat/messages",
            json={
                "account_id": "acc-1",
                "content": "你好，介绍一下你能做什么",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 1)
        self.assertIn("你好，介绍一下你能做什么", prompts[0])

    async def test_invalid_llm_planner_output_fails_after_repair_without_deterministic_auto_route(self) -> None:
        planner_prompts: list[str] = []

        async def streamer(prompt: str):
            yield "fallback answer"

        def planner(prompt: str) -> str:
            planner_prompts.append(prompt)
            return json.dumps({"nodes": [{"node_id": "bad", "capability_id": "sql_query.sql_generate"}]})

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.client.post(
            "/api/v1/conversations/conv-llm-planner-fail/messages",
            json={
                "account_id": "acc-1",
                "content": "查询龙粳33",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["completed_node_count"], 0)
        self.assertEqual(len(planner_prompts), 2)
        self.assertIn("上一轮 Planner 输出未通过校验", planner_prompts[1])

        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertFalse(any(event.event_type == "workflow.plan_built" for event in events))
        failed_event = next(event for event in events if event.event_type == "task.failed")
        self.assertEqual(failed_event.payload["code"], "planning_failed")
        self.assertEqual(failed_event.payload["planner_reason"], "WorkflowPlanValidationError")

    async def test_llm_planner_cannot_replace_user_input_or_skip_sql_dependency(self) -> None:
        prompts: list[str] = []

        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "query_data",
                            "capability_id": "skill.sql_query",
                            "input_payload": {"user_question": "恶意替换查询"},
                        },
                        {
                            "node_id": "answer_user",
                            "capability_id": "main_agent.respond",
                            "input_payload": {"user_message": "恶意替换回答"},
                        },
                    ]
                }
            )

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "安全整合回答"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-planner-boundary",
            content="查询龙粳33",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        nodes_by_capability = {node.capability_id: node for node in nodes}
        edges = await self.runtime.storage.list_task_edges(task_id)
        self.assertIn(
            (nodes_by_capability["skill.sql_query"].node_id, nodes_by_capability["main_agent.respond"].node_id),
            {(edge.from_node_id, edge.to_node_id) for edge in edges},
        )
        self.assertIn("查询龙粳33", prompts[-1])
        self.assertIn("上游能力结果上下文", prompts[-1])
        self.assertNotIn("恶意替换", prompts[-1])

    async def test_runtime_can_bind_llm_planner_factory_without_network(self) -> None:
        factory_kwargs: list[dict] = []
        planner_prompts: list[str] = []
        planner_reasoning_efforts: list[str] = []

        class FakePlannerLLMClient:
            def __init__(self, **kwargs) -> None:
                factory_kwargs.append(kwargs)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                planner_prompts.append(prompt)
                planner_reasoning_efforts.append(reasoning_effort)
                return json.dumps({"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]})

        async def streamer(prompt: str):
            yield "planner factory answer"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_llm_config={
                "api_key": "secret-test-key",
                "base_url": "https://example.test/v1",
                "model": "fake-planner-model",
            },
            planner_llm_client_factory=FakePlannerLLMClient,
            planner_reasoning_effort="low",
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-planner-factory",
            content="你好，主代理",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")

        self.assertEqual(factory_kwargs[0]["config"]["model"], "fake-planner-model")
        self.assertEqual(planner_reasoning_efforts, ["low"])
        self.assertIn("main_agent.respond", planner_prompts[0])

    async def test_rejects_old_and_internal_sql_query_capability_ids(self) -> None:
        for capability_id in ("nl2sql", "nl2sql.sql_generate", "sqlquery.query", "sql_query.sql_generate"):
            response = await self.submit_message(content="查询品种龙粳33的基因型信息", capability_id=capability_id)
            self.assertEqual(response.status_code, 400, capability_id)
            self.assertIn("Unsupported capability_id", response.json()["detail"])

    async def test_default_main_agent_uses_skill_catalog_from_configured_roots(self) -> None:
        skill_dir = self.workspace / "skills" / "report"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: report-writer
description: 生成周报
triggers:
  - 周报
---

# Report Writer
请使用汇报格式。
""",
            encoding="utf-8",
        )
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "ok"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=[self.workspace / "skills"])
        response = await self.client.post(
            "/api/v1/conversations/conv-skill/messages",
            json={
                "account_id": "acc-1",
                "content": "帮我写周报",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertIn("请使用汇报格式", prompts[0])

    async def test_runtime_can_bind_main_agent_real_llm_factory_without_network(self) -> None:
        factory_kwargs: list[dict] = []
        prompts: list[str] = []
        reasoning_efforts: list[str] = []

        class FakeLLMClient:
            def __init__(self, **kwargs) -> None:
                factory_kwargs.append(kwargs)
                self.model = kwargs["config"]["model"]

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict:
                return {
                    "provider": "openai_compatible",
                    "model": self.model,
                    "config_source": config_source,
                    "reasoning_effort": reasoning_effort,
                    "base_url_configured": True,
                }

            async def stream_text(self, prompt: str, *, reasoning_effort: str = "minimal") -> AsyncIterator[str]:
                prompts.append(prompt)
                reasoning_efforts.append(reasoning_effort)
                yield "真实"
                yield "LLM"

        await self.reconfigure_runtime(
            main_agent_llm_config={
                "api_key": "secret-test-key",
                "base_url": "https://example.test/v1",
                "model": "fake-main-agent-model",
            },
            main_agent_llm_client_factory=FakeLLMClient,
            main_agent_reasoning_effort="low",
            skill_roots=None,
        )

        response = await self.submit_message(
            conversation_id="conv-main-real-llm",
            content="你好，主代理",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        llm_event = next(event for event in events if event.event_type == "main_agent.llm_call")

        self.assertEqual(factory_kwargs[0]["config"]["model"], "fake-main-agent-model")
        self.assertEqual(reasoning_efforts, ["low"])
        self.assertIn("你好，主代理", prompts[0])
        self.assertEqual(llm_event.payload["model"], "fake-main-agent-model")
        self.assertEqual(llm_event.payload["config_source"], "injected_config")
        self.assertEqual(llm_event.payload["reasoning_effort"], "low")
        self.assertNotIn("api_key", llm_event.payload)
        self.assertNotIn("secret-test-key", str(llm_event.payload))

    async def test_metadata_controls_main_agent_thinking_and_reasoning_effort_per_request(self) -> None:
        reasoning_efforts: list[str] = []
        thinking_flags: list[bool] = []

        class FakeLLMClient:
            def __init__(self, **kwargs) -> None:
                self.model = kwargs["config"]["model"]

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict:
                return {
                    "provider": "openai_compatible",
                    "model": self.model,
                    "config_source": config_source,
                    "reasoning_effort": reasoning_effort,
                }

            async def stream_text(
                self,
                prompt: str,
                *,
                reasoning_effort: str = "minimal",
                thinking: bool = False,
            ) -> AsyncIterator[str]:
                reasoning_efforts.append(reasoning_effort)
                thinking_flags.append(thinking)
                yield "深度回答"

        await self.reconfigure_runtime(
            main_agent_llm_config={
                "api_key": "secret-test-key",
                "base_url": "https://example.test/v1",
                "model": "fake-main-agent-model",
            },
            main_agent_llm_client_factory=FakeLLMClient,
            main_agent_reasoning_effort="minimal",
            skill_roots=None,
        )

        response = await self.client.post(
            "/api/v1/conversations/conv-main-deep/messages",
            json={
                "account_id": "acc-1",
                "content": "请深入分析",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"deep_thinking": True, "main_agent_reasoning_effort": "medium"},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        llm_event = next(event for event in events if event.event_type == "main_agent.llm_call")
        self.assertEqual(reasoning_efforts, ["medium"])
        self.assertEqual(thinking_flags, [True])
        self.assertEqual(llm_event.payload["reasoning_effort"], "medium")
        self.assertTrue(llm_event.payload["thinking_enabled"])


if __name__ == "__main__":
    import unittest

    unittest.main()
