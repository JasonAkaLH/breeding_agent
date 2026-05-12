from __future__ import annotations

import json
from typing import Any

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from tests.api.support import APITestCase


def _sqlquery_prompt_kind(prompt: str) -> str:
    if "sql_query.intent_route" in prompt:
        return "intent_route"
    if "sql_query.sql_generate" in prompt:
        return "sql_generate"
    if "sql_query.result_filtering" in prompt:
        return "result_filtering"
    if "受边界约束的高层工作流规划器" in prompt:
        return "planner"
    return "unknown"


class MainAgentLoopOrchestrationAPITest(APITestCase):
    async def test_sqlquery_skill_reuses_main_agent_llm_provider_non_streaming_without_reasoning_content(self) -> None:
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
                    return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.sql_query"}]}, ensure_ascii=False)
                if "sql_query.intent_route" in prompt:
                    return json.dumps({"intent": "database", "route_id": "genotype_db", "reasoning_content": "不能泄露"}, ensure_ascii=False)
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
                            "reasoning_content": "不能泄露",
                        },
                        ensure_ascii=False,
                    )
                if "sql_query.result_filtering" in prompt:
                    return json.dumps({"keep_row_indexes": [0], "filter_reason": "命中", "reasoning_content": "不能泄露"}, ensure_ascii=False)
                raise AssertionError(f"unexpected generate_text prompt: {prompt[:200]}")

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                self.calls.append({"method": "generate_text_with_thinking", "prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                if "受边界约束的高层工作流规划器" in prompt:
                    yield {"answer": json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.sql_query"}]}, ensure_ascii=False), "reasoning": None}
                    return
                yield {"answer": "主代理汇总。", "reasoning": None}

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake_main", "model": "fake-main-agent", "config_source": config_source, "reasoning_effort": reasoning_effort}

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            return ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            main_agent_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-main-agent"},
            main_agent_llm_client_factory=FakeMainAgentLLM,
            enable_llm_planner=True,
            enable_sql_query_llm=True,
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-sql-skill-main-provider",
            content="查询品种龙粳33的基因型信息",
            capability_id=None,
            metadata={"deep_thinking": True, "main_agent_reasoning_effort": "high"},
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(len(FakeMainAgentLLM.instances), 1)

        calls = FakeMainAgentLLM.instances[0].calls
        sql_calls = [call for call in calls if _sqlquery_prompt_kind(call["prompt"]) in {"intent_route", "sql_generate", "result_filtering"}]
        self.assertEqual([_sqlquery_prompt_kind(call["prompt"]) for call in sql_calls], ["intent_route", "sql_generate", "result_filtering"])
        self.assertTrue(all(call["method"] == "generate_text" for call in sql_calls))
        self.assertTrue(all(call["thinking"] is False for call in sql_calls))
        self.assertTrue(all(call["reasoning_effort"] == "minimal" for call in sql_calls))

        artifacts = await self.runtime.storage.list_artifacts_for_task(response.json()["task_id"])
        joined_artifacts = "\n".join(artifact.storage_ref for artifact in artifacts)
        self.assertNotIn("不能泄露", joined_artifacts)

    async def test_planner_skill_sqlquery_plan_has_single_finalizer(self) -> None:
        def sql_runner(sql: str) -> ReadonlyQueryResult:
            return ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            planner_text_generator=lambda _prompt: json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.sql_query"},
                        {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_data"]},
                    ]
                },
                ensure_ascii=False,
            ),
            main_agent_stream_generator=lambda _prompt, **_kwargs: "主代理汇总。",
            skill_roots=None,
        )
        response = await self.submit_message(conversation_id="conv-single-finalizer", content="查询龙粳33", capability_id=None)
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        nodes = await self.runtime.storage.list_task_nodes_for_task(response.json()["task_id"])
        self.assertEqual([node.capability_id for node in nodes].count("main_agent.respond"), 1)
        self.assertEqual({node.capability_id for node in nodes}, {"skill.sql_query", "main_agent.respond"})
