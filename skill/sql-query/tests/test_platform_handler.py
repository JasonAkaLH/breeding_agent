from __future__ import annotations

import json
import unittest

import _bootstrap  # noqa: F401
import yaml
from src.integrations.codex_skills import SkillPlatformExecutionContext
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from sql_query_skill.platform_handler import SQLQueryPlatformHandler, build_handler


class SQLQueryPlatformHandlerTest(unittest.IsolatedAsyncioTestCase):
    def test_build_handler_returns_platform_handler(self) -> None:
        handler = build_handler()

        self.assertIsInstance(handler, SQLQueryPlatformHandler)

    def test_skill_manifest_uses_generic_non_stream_llm_service(self) -> None:
        contract = yaml.safe_load(_bootstrap.SKILL_ROOT.joinpath("skill.contract.yaml").read_text(encoding="utf-8"))
        runtime = contract["runtime"]
        entrypoint = contract["entrypoints"]["query"]

        self.assertEqual(runtime["handler_module"], "runtime/sql_query_skill/platform_handler.py")
        self.assertEqual(runtime["handler_factory"], "build_handler")
        self.assertIn("llm.non_stream", runtime["services"])
        self.assertEqual(entrypoint["handler_factory"], "build_handler")
        self.assertNotIn("llm." + "sql_query", json.dumps(contract, ensure_ascii=False))

    async def test_platform_handler_streams_progress_events_through_progress_service(self) -> None:
        recorded_progress = []

        async def record_progress(event):
            recorded_progress.append(event)

        async def llm_text_generator(prompt: str, **_: object) -> str:
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
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            if '"stage": "result_filtering"' in prompt:
                return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)
            raise AssertionError("unexpected SQLQuery LLM prompt")

        adapter = MySQLReadonlyAdapter(
            runner=lambda _sql: type(
                "Result",
                (),
                {"columns": ("variety_name",), "rows": ({"variety_name": "龙粳33"},), "row_count": 1},
            )()
        )
        handler = SQLQueryPlatformHandler()

        result = await handler(
            SkillPlatformExecutionContext(
                capability_id="skill.sql_query",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:skill_execute",
                manifest=None,
                skill_bundle_revision="skillrev-test",
                input_payload={"query": "查询品种龙粳33的基因型信息"},
                artifact_context=(),
                dependency_outputs={},
                safe_metadata={},
                services={
                    "mysql_readonly": adapter,
                    "llm.non_stream": llm_text_generator,
                    "progress_events": record_progress,
                },
            )
        )

        self.assertIsNone(result.error)
        self.assertGreaterEqual(len(recorded_progress), 6)
        self.assertTrue(all(event.event_type == "skill.progress" for event in recorded_progress))
        self.assertFalse(any(event.event_type == "skill.progress" for event in result.events))

    async def test_platform_handler_redacts_sql_errors_and_keeps_output_contract(self) -> None:
        def failing_runner(_sql: str):
            raise RuntimeError("connection refused password=secret.internal.host")

        handler = SQLQueryPlatformHandler()
        result = await handler(
            SkillPlatformExecutionContext(
                capability_id="skill.sql_query",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:skill_execute",
                manifest=None,
                skill_bundle_revision="skillrev-test",
                input_payload={"query": "查询品种龙粳33的基因型信息"},
                artifact_context=(),
                dependency_outputs={},
                safe_metadata={},
                services={
                    "mysql_readonly": MySQLReadonlyAdapter(runner=failing_runner),
                    "llm.non_stream": None,
                },
            )
        )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.message, "服务器内部错误，请稍后重试。")
        serialized = json.dumps(result.output_payload, ensure_ascii=False)
        self.assertIn("filtered_query_result", result.output_payload)
        self.assertEqual(result.output_payload["summary"], "服务器内部错误，请稍后重试。")
        self.assertNotIn("secret.internal.host", serialized)


if __name__ == "__main__":
    unittest.main()
