from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from tests.api.support import APITestCase


def _sqlquery_prompt_kind(prompt: str) -> str:
    if "sql_query.intent_route" in prompt:
        return "intent_route"
    if "sql_query.sql_generate" in prompt:
        return "sql_generate"
    if "sql_query.result_filtering" in prompt:
        return "result_filtering"
    return "unknown"


class MainAgentLoopOrchestrationAPITest(APITestCase):
    async def test_sqlquery_default_llm_does_not_reuse_main_agent_llm_override(self) -> None:
        class FakeMainAgentLLM:
            instances: list["FakeMainAgentLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.calls: list[str] = []
                FakeMainAgentLLM.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                raise AssertionError(f"SQLQuery must not call the main-agent text generator: {prompt[:200]}")

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                if "受边界约束的高层工作流规划器" in prompt:
                    self.calls.append("plan")
                    yield {
                        "answer": json.dumps(
                            {
                                "nodes": [
                                    {"node_id": "query_data", "capability_id": "sql_query.query"},
                                    {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_data"]},
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        "reasoning": None,
                    }
                    return
                if "你是小奥 Agent 的主代理" in prompt:
                    self.calls.append("answer")
                    yield {"answer": "主代理汇总。", "reasoning": None}
                    return
                raise AssertionError(f"unexpected main-agent prompt: {prompt[:200]}")

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake_main", "model": "fake-main-agent", "config_source": config_source, "reasoning_effort": reasoning_effort}

        class FakeDefaultSQLQueryLLM:
            instances: list["FakeDefaultSQLQueryLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.calls: list[dict[str, Any]] = []
                FakeDefaultSQLQueryLLM.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.calls.append({"prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "sql_query.intent_route" in prompt:
                    return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
                if "sql_query.sql_generate" in prompt:
                    return json.dumps(
                        {
                            "mode": "answer",
                            "route_id": "genotype_db",
                            "schema_profile_id": "genotype_profile",
                            "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
                            "tables_used": ["variety"],
                            "columns_used": ["variety.variety_name"],
                            "column_types_used": {"variety.variety_name": "varchar(100)"},
                        },
                        ensure_ascii=False,
                    )
                if "sql_query.result_filtering" in prompt:
                    return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)
                raise AssertionError(f"unexpected SQLQuery prompt: {prompt[:200]}")

            async def generate_text_with_thinking(self, *args: Any, **kwargs: Any):
                raise AssertionError("SQLQuery default runtime must not use streaming thinking")

            async def stream_text(self, *args: Any, **kwargs: Any):
                raise AssertionError("SQLQuery default runtime must not stream")

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            return ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)

        with patch("src.api.runtime.LLMClient", FakeDefaultSQLQueryLLM):
            await self.reconfigure_runtime(
                mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
                main_agent_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-main-agent"},
                main_agent_llm_client_factory=FakeMainAgentLLM,
                enable_llm_planner=True,
                enable_sql_query_llm=True,
                skill_roots=[],
            )
            response = await self.client.post(
                "/api/v1/conversations/conv-main-sql-separate-default/messages",
                json={
                    "account_id": "acc-1",
                    "content": "查询品种龙粳33的基因型信息",
                    "routing_mode": "auto",
                    "capability_id": None,
                    "metadata": {"deep_thinking": True, "main_agent_reasoning_effort": "high"},
                },
            )
            self.assertEqual(response.status_code, 202)
            terminal = await self.wait_for_terminal_task(response.json()["task_id"])

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(len(FakeMainAgentLLM.instances), 1)
        self.assertEqual(FakeMainAgentLLM.instances[0].calls, ["plan", "answer"])
        self.assertEqual(len(FakeDefaultSQLQueryLLM.instances), 1)
        sql_calls = FakeDefaultSQLQueryLLM.instances[0].calls
        self.assertEqual(
            [_sqlquery_prompt_kind(call["prompt"]) for call in sql_calls],
            ["intent_route", "sql_generate", "result_filtering"],
        )
        self.assertEqual([call["thinking"] for call in sql_calls], [False, True, True])
        self.assertTrue(all(call["reasoning_effort"] == "high" for call in sql_calls))

    async def test_default_auto_flow_separates_main_agent_and_sqlquery_llm_instances(self) -> None:
        call_log: list[str] = []

        class FakeMainAgentLLM:
            instances: list["FakeMainAgentLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)
                self.calls: list[dict[str, Any]] = []
                FakeMainAgentLLM.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                raise AssertionError(f"main-agent LLM should not be used for SQLQuery text calls: {prompt[:200]}")

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                self.calls.append({"method": "generate_text_with_thinking", "prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "受边界约束的高层工作流规划器" in prompt:
                    call_log.append("main:plan")
                    yield {"reasoning": "先判断需要查数据库再汇总。", "answer": None}
                    yield {
                        "reasoning": None,
                        "answer": json.dumps(
                            {
                                "nodes": [
                                    {"node_id": "query_data", "capability_id": "sql_query.query"},
                                    {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_data"]},
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    }
                    return
                if "你是小奥 Agent 的主代理" in prompt:
                    call_log.append("main:answer")
                    yield {"reasoning": "基于 SQLQuery 结果组织答案。", "answer": None}
                    yield {"reasoning": None, "answer": "龙粳33 查询完成。"}
                    return
                raise AssertionError(f"unexpected stream prompt: {prompt[:200]}")

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake_main", "model": "fake-main-agent", "config_source": config_source, "reasoning_effort": reasoning_effort}

        class FakeSQLQueryLLM:
            instances: list["FakeSQLQueryLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)
                self.calls: list[dict[str, Any]] = []
                FakeSQLQueryLLM.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.calls.append({"method": "generate_text", "prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "sql_query.intent_route" in prompt:
                    call_log.append("sql:intent_route")
                    return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
                if "sql_query.sql_generate" in prompt:
                    call_log.append("sql:sql_generate")
                    return json.dumps(
                        {
                            "mode": "answer",
                            "route_id": "genotype_db",
                            "schema_profile_id": "genotype_profile",
                            "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
                            "tables_used": ["variety"],
                            "columns_used": ["variety.variety_name"],
                            "column_types_used": {"variety.variety_name": "varchar(100)"},
                        },
                        ensure_ascii=False,
                    )
                if "sql_query.result_filtering" in prompt:
                    call_log.append("sql:result_filtering")
                    return json.dumps({"keep_row_indexes": [0], "filter_reason": "保留精确品种。"}, ensure_ascii=False)
                raise AssertionError(f"unexpected SQLQuery prompt: {prompt[:200]}")

            async def generate_text_with_thinking(self, *args: Any, **kwargs: Any):
                raise AssertionError("SQLQuery internal LLM must be non-streaming")

            async def stream_text(self, *args: Any, **kwargs: Any):
                raise AssertionError("SQLQuery internal LLM must be non-streaming")

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            self.assertIn("LIKE '%龙粳33%'", sql)
            return ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            main_agent_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-main-agent"},
            main_agent_llm_client_factory=FakeMainAgentLLM,
            sql_query_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-sql-query"},
            sql_query_llm_client_factory=FakeSQLQueryLLM,
            enable_llm_planner=True,
            enable_sql_query_llm=True,
            skill_roots=[],
        )

        response = await self.client.post(
            "/api/v1/conversations/conv-unified-main-agent/messages",
            json={
                "account_id": "acc-1",
                "content": "查询品种龙粳33的基因型信息",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"deep_thinking": True, "main_agent_reasoning_effort": "high"},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(len(FakeMainAgentLLM.instances), 1)
        self.assertEqual(len(FakeSQLQueryLLM.instances), 1)
        main_client = FakeMainAgentLLM.instances[0]
        sql_client = FakeSQLQueryLLM.instances[0]
        main_prompt_kinds = []
        for call in main_client.calls:
            prompt = call["prompt"]
            if "受边界约束的高层工作流规划器" in prompt:
                main_prompt_kinds.append("plan")
            elif "你是小奥 Agent 的主代理" in prompt:
                main_prompt_kinds.append("answer")
        sql_prompt_kinds = [_sqlquery_prompt_kind(call["prompt"]) for call in sql_client.calls]
        self.assertEqual(main_prompt_kinds, ["plan", "answer"])
        self.assertEqual(sql_prompt_kinds, ["intent_route", "sql_generate", "result_filtering"])
        self.assertEqual(call_log, ["main:plan", "sql:intent_route", "sql:sql_generate", "sql:result_filtering", "main:answer"])
        self.assertEqual([call["thinking"] for call in sql_client.calls], [False, True, True])
        self.assertTrue(all(call["reasoning_effort"] == "high" for call in sql_client.calls))

        events = await self.runtime.storage.list_events_for_task(task_id)
        planner_reasoning = [
            event
            for event in events
            if event.event_type == "main_agent.reasoning_delta" and event.payload.get("stage") == "orchestration_plan"
        ]
        self.assertEqual([event.payload["delta"] for event in planner_reasoning], ["先判断需要查数据库再汇总。"])
        plan_event_index = next(index for index, event in enumerate(events) if event.event_type == "workflow.plan_built")
        reasoning_index = events.index(planner_reasoning[0])
        self.assertLess(reasoning_index, plan_event_index)
        planner_call = next(call for call in main_client.calls if "受边界约束的高层工作流规划器" in call["prompt"])
        self.assertTrue(planner_call["thinking"])
        self.assertEqual(planner_call["reasoning_effort"], "high")


class MainAgentLoopRuntimeReplanAPITest(APITestCase):
    async def test_observe_replan_and_final_answer_share_main_agent_llm_but_not_sqlquery_llm(self) -> None:
        call_log: list[str] = []

        class FakeMainAgentLLM:
            instances: list["FakeMainAgentLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.calls: list[dict[str, Any]] = []
                FakeMainAgentLLM.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                raise AssertionError(f"main-agent LLM should not be used for SQLQuery text calls: {prompt[:200]}")

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                self.calls.append({"method": "generate_text_with_thinking", "prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "受边界约束的高层工作流规划器" in prompt:
                    call_log.append("main:plan")
                    yield {
                        "answer": json.dumps(
                            {
                                "nodes": [
                                    {"node_id": "query_data", "capability_id": "sql_query.query"},
                                    {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_data"]},
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        "reasoning": None,
                    }
                    return
                if "运行时重编排决策器" in prompt:
                    call_log.append("main:replan")
                    yield {"reasoning": "第一次结果为空，需要追加一次查询再总结。", "answer": None}
                    yield {
                        "reasoning": None,
                        "answer": json.dumps(
                            {
                                "action": "replan",
                                "reason": "empty first SQLQuery result",
                                "nodes": [
                                    {"node_id": "runtime_query_1", "capability_id": "sql_query.query"},
                                    {"node_id": "runtime_answer_1", "capability_id": "main_agent.respond", "depends_on": ["runtime_query_1"]},
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    }
                    return
                if "你是小奥 Agent 的主代理" in prompt:
                    call_log.append("main:answer")
                    yield {"answer": "重排后查到了龙粳33。", "reasoning": None}
                    return
                raise AssertionError(f"unexpected stream prompt: {prompt[:200]}")

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake_main", "model": "fake-main-agent", "config_source": config_source, "reasoning_effort": reasoning_effort}

        class FakeSQLQueryLLM:
            instances: list["FakeSQLQueryLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.calls: list[dict[str, Any]] = []
                FakeSQLQueryLLM.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.calls.append({"method": "generate_text", "prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "sql_query.intent_route" in prompt:
                    call_log.append("sql:intent_route")
                    return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
                if "sql_query.sql_generate" in prompt:
                    call_log.append("sql:sql_generate")
                    return json.dumps(
                        {
                            "mode": "answer",
                            "route_id": "genotype_db",
                            "schema_profile_id": "genotype_profile",
                            "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
                            "tables_used": ["variety"],
                            "columns_used": ["variety.variety_name"],
                            "column_types_used": {"variety.variety_name": "varchar(100)"},
                        },
                        ensure_ascii=False,
                    )
                if "sql_query.result_filtering" in prompt:
                    call_log.append("sql:result_filtering")
                    return json.dumps({"keep_row_indexes": [0], "filter_reason": "第二次查询命中。"}, ensure_ascii=False)
                raise AssertionError(f"unexpected SQLQuery prompt: {prompt[:200]}")

            async def generate_text_with_thinking(self, *args: Any, **kwargs: Any):
                raise AssertionError("SQLQuery internal LLM must be non-streaming")

            async def stream_text(self, *args: Any, **kwargs: Any):
                raise AssertionError("SQLQuery internal LLM must be non-streaming")

        sql_calls = 0

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            nonlocal sql_calls
            sql_calls += 1
            if sql_calls == 1:
                return ReadonlyQueryResult(columns=("variety_name",), rows=(), row_count=0)
            return ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            main_agent_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-main-agent"},
            main_agent_llm_client_factory=FakeMainAgentLLM,
            sql_query_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-sql-query"},
            sql_query_llm_client_factory=FakeSQLQueryLLM,
            enable_llm_planner=True,
            enable_sql_query_llm=True,
            skill_roots=[],
        )
        response = await self.client.post(
            "/api/v1/conversations/conv-unified-replan/messages",
            json={
                "account_id": "acc-1",
                "content": "查询品种龙粳33的基因型信息",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"deep_thinking": True, "main_agent_reasoning_effort": "high"},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(sql_calls, 2)
        self.assertEqual(len(FakeMainAgentLLM.instances), 1)
        self.assertEqual(len(FakeSQLQueryLLM.instances), 1)
        main_client = FakeMainAgentLLM.instances[0]
        sql_client = FakeSQLQueryLLM.instances[0]
        main_prompt_kinds = []
        for call in main_client.calls:
            prompt = call["prompt"]
            if "受边界约束的高层工作流规划器" in prompt:
                main_prompt_kinds.append("plan")
            elif "运行时重编排决策器" in prompt:
                main_prompt_kinds.append("replan")
            elif "你是小奥 Agent 的主代理" in prompt:
                main_prompt_kinds.append("answer")
        sql_prompt_kinds = [_sqlquery_prompt_kind(call["prompt"]) for call in sql_client.calls]
        self.assertEqual(main_prompt_kinds, ["plan", "replan", "answer"])
        self.assertEqual(sql_prompt_kinds, ["intent_route", "sql_generate", "intent_route", "sql_generate", "result_filtering"])
        self.assertEqual(call_log, ["main:plan", "sql:intent_route", "sql:sql_generate", "main:replan", "sql:intent_route", "sql:sql_generate", "sql:result_filtering", "main:answer"])
        self.assertEqual([call["thinking"] for call in sql_client.calls], [False, True, False, True, True])
        self.assertTrue(all(call["reasoning_effort"] == "high" for call in sql_client.calls))

        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertTrue(any(event.event_type == "task.replanned" and event.payload.get("metadata", {}).get("replan_source") == "main_agent_llm_runtime" for event in events))
        replan_reasoning = [event.payload["delta"] for event in events if event.event_type == "main_agent.reasoning_delta" and event.payload.get("stage") == "orchestration_replan"]
        self.assertEqual(replan_reasoning, ["第一次结果为空，需要追加一次查询再总结。"])
