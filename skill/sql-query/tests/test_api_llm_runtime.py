from __future__ import annotations

import _bootstrap  # noqa: F401

import json
from typing import Any

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from support import SQLQueryAPITestCase


def _sqlquery_prompt_name(prompt: str) -> str:
    for name in ("intent_route", "sql_generate", "result_filtering"):
        if name in prompt:
            return name
    return "unknown"


class SQLQueryLLMRuntimeAPITest(SQLQueryAPITestCase):
    async def test_sqlquery_skill_uses_main_agent_provider_adapter(self) -> None:
        main_calls: list[dict[str, Any]] = []
        class FakeMainAgentLLMClient:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal", **_kwargs: Any) -> str:
                main_calls.append({"prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort, "kwargs": self.kwargs})
                if "阶段：intent_route" in prompt:
                    return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
                if "当前阶段：sql_generate" in prompt:
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
                if '"stage": "result_filtering"' in prompt:
                    return json.dumps({"keep_row_indexes": [0], "filter_reason": "只保留龙粳33。"}, ensure_ascii=False)
                raise AssertionError(f"unexpected prompt: {prompt[:200]}")

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                if "Skill 软绑定判断器" in prompt:
                    yield {
                        "answer": '{"decision":"execute","target_capability_id":"skill.sql_query","confidence":0.95,"reason_code":"ready_to_execute"}',
                        "reasoning": None,
                    }
                    return
                yield {"answer": "主代理汇总。", "reasoning": None}

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake-main", "config_source": config_source, "reasoning_effort": reasoning_effort}

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            self.assertIn("LIKE '%龙粳33%'", sql)
            return ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"}, {"variety_name": "龙粳331"}), row_count=2)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            main_agent_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-main"},
            main_agent_llm_client_factory=FakeMainAgentLLMClient,
            enable_platform_llm=True,
            skill_roots=None,
        )

        response = await self.submit_message(
            conversation_id="conv-sqlquery-main-provider",
            content="查询品种龙粳33的基因型信息",
            capability_id="skill.sql_query",
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        prompt_names = [_sqlquery_prompt_name(call["prompt"]) for call in main_calls]
        self.assertEqual(prompt_names, ["intent_route", "sql_generate", "result_filtering"])
        self.assertEqual([call["thinking"] for call in main_calls], [False, False, False])
        self.assertTrue(all(call["reasoning_effort"] == "minimal" for call in main_calls))

        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        filtered = next(artifact for artifact in artifacts if "filtered_query_result" in artifact.artifact_id)
        filtered_payload = json.loads(filtered.storage_ref)
        self.assertEqual(filtered_payload["filter_source"], "llm")
        self.assertEqual(filtered_payload["rows"], [{"variety_name": "龙粳33"}])

    async def test_sqlquery_llm_adapter_inherits_selected_model_and_thinking_options(self) -> None:
        main_calls: list[dict[str, Any]] = []

        class FakeMainAgentLLMClient:
            def __init__(self, **kwargs: Any) -> None:
                self.model_edition = kwargs["config"]["model_edition"]

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
                **_kwargs: Any,
            ) -> str:
                main_calls.append(
                    {
                        "prompt": prompt,
                        "thinking": thinking,
                        "reasoning_effort": reasoning_effort,
                        "model_edition": self.model_edition,
                    }
                )
                if "阶段：intent_route" in prompt:
                    return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
                if "当前阶段：sql_generate" in prompt:
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
                if '"stage": "result_filtering"' in prompt:
                    return json.dumps({"keep_row_indexes": [0], "filter_reason": "只保留龙粳33。"}, ensure_ascii=False)
                raise AssertionError(f"unexpected prompt: {prompt[:200]}")

            async def generate_text_with_thinking(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ):
                if "Skill 软绑定判断器" in prompt:
                    yield {
                        "answer": '{"decision":"execute","target_capability_id":"skill.sql_query","confidence":0.95,"reason_code":"ready_to_execute"}',
                        "reasoning": None,
                    }
                    return
                yield {"answer": "主代理汇总。", "reasoning": "前端可见主代理推理。"}

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake-main", "config_source": config_source, "reasoning_effort": reasoning_effort}

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            self.assertIn("LIKE '%龙粳33%'", sql)
            return ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            main_agent_llm_config={
                "api_key": "test",
                "base_url": "http://example.test",
                "model_editions": {
                    "default": "flash",
                    "options": [
                        {"value": "flash", "label": "Flash"},
                        {"value": "pro", "label": "Pro"},
                    ],
                },
            },
            main_agent_llm_client_factory=FakeMainAgentLLMClient,
            enable_platform_llm=True,
            skill_roots=None,
        )

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-sqlquery-model-thinking",
                "content": "查询品种龙粳33的基因型信息",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "model_edition": "pro",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/sql-query",
                    "soft_skill_binding": {"capability_id": "skill.sql_query", "command": "/sql-query"},
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "max",
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        prompt_names = [_sqlquery_prompt_name(call["prompt"]) for call in main_calls]
        self.assertEqual(prompt_names, ["intent_route", "sql_generate", "result_filtering"])
        self.assertEqual([call["thinking"] for call in main_calls], [True, True, True])
        self.assertEqual([call["reasoning_effort"] for call in main_calls], ["max", "max", "max"])
        self.assertEqual([call["model_edition"] for call in main_calls], ["pro", "pro", "pro"])

    async def test_sqlquery_skill_ignores_reasoning_content_from_main_provider_adapter(self) -> None:
        class FakeMainAgentLLMClient:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal", **_kwargs: Any) -> dict[str, str]:
                if "阶段：intent_route" in prompt:
                    return {"answer": json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False), "reasoning_content": "隐藏推理"}
                if "当前阶段：sql_generate" in prompt:
                    return {
                        "reasoning_content": "隐藏推理",
                        "answer": json.dumps(
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
                        ),
                    }
                if '"stage": "result_filtering"' in prompt:
                    return {"answer": json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False), "reasoning_content": "隐藏推理"}
                raise AssertionError("unexpected prompt")

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                if "Skill 软绑定判断器" in prompt:
                    yield {
                        "answer": '{"decision":"execute","target_capability_id":"skill.sql_query","confidence":0.95,"reason_code":"ready_to_execute"}',
                        "reasoning": None,
                    }
                    return
                yield {"answer": "主代理汇总。", "reasoning": None}

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake-main"}

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)
            ),
            main_agent_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-main"},
            main_agent_llm_client_factory=FakeMainAgentLLMClient,
            enable_platform_llm=True,
            skill_roots=None,
        )
        response = await self.submit_message(content="查询品种龙粳33的基因型信息", capability_id="skill.sql_query")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        joined = "\n".join(artifact.storage_ref for artifact in artifacts)
        self.assertNotIn("隐藏推理", joined)
