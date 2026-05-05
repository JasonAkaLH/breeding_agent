from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tests.api.support import APITestCase
from src.api.runtime import build_api_runtime
from src.integrations.llm_client import CONFIG_ENV_PREFIX
from src.integrations.token_counter import get_num_of_tokens_from_messages
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult


@contextmanager
def _isolated_config_env():
    saved = {key: value for key, value in os.environ.items() if key.startswith(CONFIG_ENV_PREFIX)}
    for key in list(os.environ):
        if key.startswith(CONFIG_ENV_PREFIX):
            del os.environ[key]
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(CONFIG_ENV_PREFIX):
                del os.environ[key]
        os.environ.update(saved)


class SQLQueryLLMRuntimeAPITest(APITestCase):
    async def test_configured_sqlquery_llm_client_is_used_by_generate_and_filtering(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeSQLQueryLLMClient:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                calls.append(
                    {
                        "prompt": prompt,
                        "thinking": thinking,
                        "reasoning_effort": reasoning_effort,
                        "kwargs": self.kwargs,
                    }
                )
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
                    return json.dumps({"keep_row_indexes": [0], "filter_reason": "只保留龙粳33。"}, ensure_ascii=False)
                raise AssertionError("unexpected SQLQuery prompt")

        def sql_runner(sql: str) -> ReadonlyQueryResult:
            self.assertIn("LIKE '%龙粳33%'", sql)
            return ReadonlyQueryResult(
                columns=("variety_name",),
                rows=(
                    {"variety_name": "龙粳33"},
                    {"variety_name": "龙粳331"},
                ),
                row_count=2,
            )

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            sql_query_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-sql"},
            sql_query_llm_client_factory=FakeSQLQueryLLMClient,
            main_agent_reasoning_effort="low",
            enable_sql_query_llm=True,
            skill_roots=[],
        )

        response = await self.submit_message(
            conversation_id="conv-sqlquery-llm-runtime",
            content="查询品种龙粳33的基因型信息",
            capability_id="sql_query.query",
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        prompt_names = ["sql_query.sql_generate" if "sql_query.sql_generate" in call["prompt"] else "sql_query.result_filtering" for call in calls]
        self.assertEqual(prompt_names, ["sql_query.sql_generate", "sql_query.result_filtering"])
        self.assertTrue(all(call["reasoning_effort"] == "low" for call in calls))
        self.assertTrue(all(call["thinking"] is False for call in calls))
        self.assertTrue(all(call["kwargs"]["config"]["model"] == "fake-sql" for call in calls))

        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        filtered = next(artifact for artifact in artifacts if "filtered_query_result" in artifact.artifact_id)
        filtered_payload = json.loads(filtered.storage_ref)
        self.assertEqual(filtered_payload["filter_source"], "llm")
        self.assertEqual(filtered_payload["rows"], [{"variety_name": "龙粳33"}])

        events = await self.runtime.storage.list_events_for_task(task_id)
        llm_call_nodes = [
            event.payload.get("node_name")
            for event in events
            if event.event_type == "sql_query.llm_call"
        ]
        self.assertIn("sql_query.sql_generate", llm_call_nodes)
        self.assertIn("sql_query.result_filtering", llm_call_nodes)

    async def test_sqlquery_llm_uses_request_thinking_settings_and_ignores_reasoning_content(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeSQLQueryLLMClient:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> dict[str, str]:
                calls.append(
                    {
                        "prompt": prompt,
                        "thinking": thinking,
                        "reasoning_effort": reasoning_effort,
                    }
                )
                if "sql_query.sql_generate" in prompt:
                    return {
                        "reasoning_content": "这里的推理内容不能进入 SQL 解析。",
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
                if "sql_query.result_filtering" in prompt:
                    return {
                        "reasoning_content": "这里的推理内容不能进入筛选 JSON。",
                        "answer": json.dumps({"keep_row_indexes": [0], "filter_reason": "只保留龙粳33。"}, ensure_ascii=False),
                    }
                raise AssertionError("unexpected SQLQuery prompt")

        def sql_runner(_sql: str) -> ReadonlyQueryResult:
            return ReadonlyQueryResult(
                columns=("variety_name",),
                rows=(
                    {"variety_name": "龙粳33"},
                    {"variety_name": "龙粳331"},
                ),
                row_count=2,
            )

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            sql_query_llm_config={"api_key": "test", "base_url": "http://example.test", "model": "fake-sql"},
            sql_query_llm_client_factory=FakeSQLQueryLLMClient,
            main_agent_reasoning_effort="low",
            enable_sql_query_llm=True,
            skill_roots=[],
        )

        response = await self.submit_message(
            conversation_id="conv-sqlquery-llm-request-thinking",
            content="查询品种龙粳33的基因型信息",
            capability_id="sql_query.query",
            metadata={"deep_thinking": True, "main_agent_reasoning_effort": "medium"},
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual([call["reasoning_effort"] for call in calls], ["medium", "medium"])
        self.assertEqual([call["thinking"] for call in calls], [True, True])

        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        generated = next(artifact for artifact in artifacts if "generated_sql" in artifact.artifact_id)
        generated_payload = json.loads(generated.storage_ref)
        self.assertEqual(generated_payload["generation_source"], "llm")
        self.assertNotIn("reasoning_content", generated.storage_ref)

    async def test_sqlquery_result_filtering_uses_configured_trim_max_tokens(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeSQLQueryLLMClient:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                calls.append({"prompt": prompt, "kwargs": self.kwargs})
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
                    return json.dumps({"keep_row_indexes": [0, 1]}, ensure_ascii=False)
                raise AssertionError("unexpected SQLQuery prompt")

        rows = [
            {"variety_name": "old-row", "detail": "older data " * 20},
            {"variety_name": "middle-row", "detail": "middle data"},
            {"variety_name": "new-row", "detail": "new data"},
        ]
        latest_two_token_budget = sum(
            get_num_of_tokens_from_messages([json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)])
            for row in rows[-2:]
        )

        def sql_runner(_sql: str) -> ReadonlyQueryResult:
            return ReadonlyQueryResult(
                columns=("variety_name", "detail"),
                rows=tuple(rows),
                row_count=len(rows),
            )

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
            sql_query_llm_config={
                "api_key": "test",
                "base_url": "http://example.test",
                "model": "fake-sql",
                "trim_max_tokens": latest_two_token_budget,
            },
            sql_query_llm_client_factory=FakeSQLQueryLLMClient,
            enable_sql_query_llm=True,
            skill_roots=[],
        )

        response = await self.submit_message(
            conversation_id="conv-sqlquery-trim-runtime",
            content="查询品种龙粳33的基因型信息",
            capability_id="sql_query.query",
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        filtering_prompt = calls[-1]["prompt"]
        self.assertIn("sql_query.result_filtering", filtering_prompt)
        self.assertIn("new-row", filtering_prompt)
        self.assertIn("middle-row", filtering_prompt)
        self.assertNotIn("old-row", filtering_prompt)

        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        filtered = next(artifact for artifact in artifacts if "filtered_query_result" in artifact.artifact_id)
        filtered_payload = json.loads(filtered.storage_ref)
        self.assertTrue(filtered_payload["token_trim_applied"])
        self.assertEqual(filtered_payload["token_trim_removed_row_count"], 1)

    async def test_runtime_rejects_multiple_distinct_llm_config_paths(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            first_path = Path(tmpdir) / "sql-query.yaml"
            first_path.write_text(
                "\n".join(
                    [
                        "api_key: first",
                        "base_url: http://first.example.test",
                        "model: first-model",
                    ]
                ),
                encoding="utf-8",
            )
            second_path = Path(tmpdir) / "planner.yaml"
            second_path.write_text(
                "\n".join(
                    [
                        "api_key: second",
                        "base_url: http://second.example.test",
                        "model: second-model",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "same startup config file"):
                build_api_runtime(
                    database_path=self.workspace / "multi-path.sqlite3",
                    audit_log_path=self.workspace / "multi-path-audit.jsonl",
                    sql_query_llm_config_path=first_path,
                    planner_llm_config_path=second_path,
                    skill_roots=[],
                )

    async def test_sqlquery_config_path_is_bootstrapped_to_env_at_runtime_build(self) -> None:
        init_kwargs: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []

        class FakeSQLQueryLLMClient:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = dict(kwargs)
                init_kwargs.append(self.kwargs)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                calls.append({"prompt": prompt, "kwargs": self.kwargs})
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
                    return json.dumps({"keep_row_indexes": [0, 1]}, ensure_ascii=False)
                raise AssertionError("unexpected SQLQuery prompt")

        rows = [
            {"variety_name": "old-row", "detail": "older data " * 20},
            {"variety_name": "middle-row", "detail": "middle data"},
            {"variety_name": "new-row", "detail": "new data"},
        ]
        latest_two_token_budget = sum(
            get_num_of_tokens_from_messages([json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)])
            for row in rows[-2:]
        )

        def sql_runner(_sql: str) -> ReadonlyQueryResult:
            return ReadonlyQueryResult(
                columns=("variety_name", "detail"),
                rows=tuple(rows),
                row_count=len(rows),
            )

        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "api_key: test",
                        "base_url: http://example.test",
                        "model: fake-sql",
                        f"trim_max_tokens: {latest_two_token_budget}",
                    ]
                ),
                encoding="utf-8",
            )
            await self.reconfigure_runtime(
                mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
                sql_query_llm_config_path=config_path,
                sql_query_llm_client_factory=FakeSQLQueryLLMClient,
                enable_sql_query_llm=True,
                skill_roots=[],
            )
            config_path.unlink()

            response = await self.submit_message(
                conversation_id="conv-sqlquery-env-config-path",
                content="查询品种龙粳33的基因型信息",
                capability_id="sql_query.query",
            )
            self.assertEqual(response.status_code, 202)
            task_id = response.json()["task_id"]
            terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(init_kwargs, [{}])
        filtering_prompt = calls[-1]["prompt"]
        self.assertIn("new-row", filtering_prompt)
        self.assertIn("middle-row", filtering_prompt)
        self.assertNotIn("old-row", filtering_prompt)
